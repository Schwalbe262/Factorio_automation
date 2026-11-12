"""Incident decisions (one remedy from a menu) through the user's authenticated Codex CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from .incidents import DOCUMENT_CHAR_CAP


OPERATIONS = ("plan", "request", "continue", "retry", "pause", "wait")
PARAMETERS = {
    "type": "string",
    "description": "A JSON object of documented capability parameters; use {} for no parameters.",
}
NODE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "capability", "parameters_json", "priority", "depends_on", "reason"],
    "properties": {
        "id": {"type": "string", "maxLength": 64},
        "capability": {"type": "string"},
        "parameters_json": PARAMETERS,
        "priority": {"type": "integer", "minimum": 0, "maximum": 1000},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
}
DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operation", "capability", "parameters_json", "queue", "reason", "target_task_id"],
    "properties": {
        "operation": {"type": "string", "enum": list(OPERATIONS)},
        "capability": {"type": ["string", "null"]},
        "target_task_id": {
            "type": ["string", "null"],
            "maxLength": 64,
            "pattern": "^[A-Za-z0-9_-]{1,64}$",
            "description": "Existing stable work_queue ID for continue/retry/pause, otherwise null; never a job key.",
        },
        "parameters_json": PARAMETERS,
        "queue": {"type": "array", "items": NODE},
        "reason": {"type": "string"},
    },
}

PROMPT = """You are the exception handler of a Factorio Space Age automation framework.
The framework sequences routine work itself (roadmap autopilot, demand-driven
capacity, automatic retries, up to 4 concurrent jobs). You are asked only when it
is stuck without a rule. The document below describes one incident: its kind,
subject and facts, a menu of allowed remedies, and a short factory status.

Choose exactly ONE remedy from the menu and return it as the structured decision:
- retry: operation=retry, target_task_id and parameters_json copied from that menu
  entry, queue=[]. Only when the facts show the cause is gone.
- pause: operation=pause, target_task_id and parameters_json from the menu, queue=[].
- add_nodes: operation=plan, capability=null, parameters_json="{}", queue of 1-3
  nodes using only menu capabilities and their documented parameters; new ids.
- wait: operation=wait, queue=[], reason says what must change first.
Every other field is null, "{}" or [] as the schema allows. reason: one or two sentences.

Hard rules:
- Never invent capabilities, parameters, task ids or technology names.
- Never retry an unknown action outcome (unknown_outcome=true); it needs review.
- Never grant items or research, choose coordinates, or issue commands.
- Do not call tools, read files or run commands; the document is your whole context.
"""


def _codex_command():
    found = shutil.which("codex")
    if not found:
        raise RuntimeError("Codex CLI is not installed; sign in with codex login before starting")
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat"):
        # Run the executable directly: Windows shell metacharacters must never
        # interpret a model prompt, a file path, or snapshot data.
        content = path.read_text(encoding="utf-8-sig")
        match = re.search(r'^"([^"\r\n]+\.exe)"( -c cli_auth_credentials_store=file)? %\*\s*$', content, re.M | re.I)
        if not match or not Path(match[1]).is_file():
            raise RuntimeError("Unsupported Codex CLI wrapper; configure a native executable")
        return [match[1]] + (["-c", "cli_auth_credentials_store=file"] if match[2] else [])
    return [str(path)]


def _redact(value):
    if isinstance(value, dict):
        return {
            k: _redact(v)
            for k, v in value.items()
            if not re.search(r"password|secret|authorization|api.key|access.token|refresh.token", str(k), re.I)
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _parameters(raw):
    if not isinstance(raw, str) or len(raw) > 12000:
        raise ValueError("Luna parameters must be bounded JSON text")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Luna parameters must be an object")
    return value


def normalize_decision(value):
    if (
        not isinstance(value, dict)
        or value.get("operation") not in OPERATIONS
        or set(value) - {"operation", "capability", "parameters_json", "queue", "reason", "target_task_id"}
    ):
        raise ValueError("Invalid Luna decision envelope")
    operation = value["operation"]
    capability = value.get("capability")
    queue = value.get("queue", [])
    if capability is not None and (
        not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", capability)
    ):
        raise ValueError("Invalid Luna capability")
    if operation == "request" and not capability:
        raise ValueError("A request needs a capability")
    target = value.get("target_task_id")
    if target is not None and (
        operation not in ("continue", "retry", "pause")
        or not isinstance(target, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", target)
    ):
        raise ValueError("Invalid Luna target_task_id for job control")
    if not isinstance(queue, list) or len(queue) > 16:
        raise ValueError("Luna queue is limited to sixteen tasks")
    nodes = []
    for node in queue:
        if (
            not isinstance(node, dict)
            or set(node) != {"id", "capability", "parameters_json", "priority", "depends_on", "reason"}
            or not isinstance(node["id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node["id"])
            or not isinstance(node["capability"], str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", node["capability"])
            or type(node["priority"]) is not int
            or not 0 <= node["priority"] <= 1000
            or not isinstance(node["depends_on"], list)
            or len(node["depends_on"]) > 16
            or any(not isinstance(q, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", q) for q in node["depends_on"])
            or not isinstance(node["reason"], str)
            or len(node["reason"]) > 2000
        ):
            raise ValueError("Invalid Luna queue task")
        nodes.append(
            {k: v for k, v in node.items() if k != "parameters_json"}
            | {"parameters": _parameters(node["parameters_json"])}
        )
    reason = value.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ValueError("Invalid Luna reason")
    result = {
        "operation": operation,
        "capability": capability,
        "parameters": _parameters(value.get("parameters_json", "{}")),
        "queue": nodes,
        "reason": reason,
    }
    if "target_task_id" in value:
        result["target_task_id"] = target
    return result


class LunaDecisionClient:
    def __init__(self, model="gpt-6-luna", reasoning_effort="medium", timeout_seconds=120):
        if model != "gpt-6-luna" or reasoning_effort != "medium":
            raise ValueError("This runner is pinned to GPT-6 Luna medium")
        if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 300:
            raise ValueError("Bounded Luna timeout required")
        self.model, self.reasoning_effort, self.timeout_seconds = model, reasoning_effort, timeout_seconds
        self.last_usage = None
        self.last_session_id = None
        self._lock = threading.Lock()
        self._process = None
        self._cancelled = False

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()

    def _invoke(self, command, **kwargs):
        payload = kwargs.pop("input")
        timeout = kwargs.pop("timeout")
        with self._lock:
            if self._cancelled:
                raise RuntimeError("Luna client stopped; no decision was authorized")
            process = subprocess.Popen(command, stdin=subprocess.PIPE, **kwargs)
            self._process = process
        try:
            stdout, stderr = process.communicate(payload, timeout=timeout)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            with self._lock:
                self._process = None

    def decide(self, document):
        """One decision for one incident document; at most ``DOCUMENT_CHAR_CAP`` characters."""
        if not isinstance(document, dict):
            raise ValueError("A structured incident document is required")
        raw = json.dumps(_redact(document), ensure_ascii=False, separators=(",", ":"))
        if len(raw) > DOCUMENT_CHAR_CAP:
            raise ValueError("Incident document exceeds its character cap; it must be truncated first")
        self.last_usage = None
        with tempfile.TemporaryDirectory(prefix="factorio-luna-") as directory:
            root = Path(directory)
            schema = root / "decision-schema.json"
            output = root / "decision.json"
            schema.write_text(json.dumps(DECISION_SCHEMA), encoding="utf-8")
            command = _codex_command() + [
                "exec",
                "--model",
                self.model,
                "-c",
                'model_reasoning_effort="medium"',
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--skip-git-repo-check",
                "-C",
                str(root),
                "--json",
                "--output-schema",
                str(schema),
                "--output-last-message",
                str(output),
                "-",
            ]
            env = dict(os.environ)
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
                env.pop(key, None)
            if "cli_auth_credentials_store=file" in command:
                env["CODEX_HOME"] = str(Path.home() / ".codex")
            try:
                result = self._invoke(
                    command,
                    input=(PROMPT + "\nINCIDENT DOCUMENT:\n" + raw).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    cwd=root,
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Luna decision timed out; no game action was authorized") from exc
            if result.returncode:
                detail = result.stderr.decode("utf-8", errors="replace")[-1400:]
                raise RuntimeError("Codex Luna failed (no model fallback): " + detail)
            for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get("item", {})
                if item.get("type") in ("command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call"):
                    raise RuntimeError("Luna planner attempted a tool; its decision was not authorized")
                if event.get("type") == "thread.started":
                    self.last_session_id = event.get("thread_id")
                if event.get("type") == "turn.completed":
                    self.last_usage = event.get("usage")
            if not output.exists():
                raise RuntimeError("Luna did not produce a structured decision")
            return normalize_decision(json.loads(output.read_text(encoding="utf-8-sig")))
