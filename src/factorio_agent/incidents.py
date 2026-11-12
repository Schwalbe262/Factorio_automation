"""Incidents: the situations the framework cannot resolve by its own rules.

Routine sequencing belongs to the framework (autopilot, auto-retry, dispatch).
The model, when enabled, is consulted only for an open incident, with a small
document and a constrained menu of remedies. Everything here is pure: the
supervisor passes jobs, queue nodes, the observation receipt and timing facts
in, and logs what comes out.

Kinds, in severity order (most severe first):

- ``empty_queue``: no active job, no pending/ready node, roadmap not finished,
  for at least ``idle_grace_seconds`` (autopilot runs before the check, so an
  empty queue means it proposed nothing).
- ``blocked_job``: a job stayed blocked for at least ``blocked_grace_seconds``.
  An auto-retry makes the job leave the blocked state and restarts the timer,
  so the incident opens only once auto-retry did not apply or is exhausted.
- ``no_progress``: jobs are active, no job succeeded for ``progress_minutes``
  and the recent action sample is repetitive (top two actions at least
  ``repeat_share`` of the sample).
- ``planner_review``: optional periodic review every ``review_minutes``.

An episode closes ``resolved`` when its condition clears, or ``expired`` after
``outcome_minutes``; a condition that persists opens a new episode.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping

SEVERITY = ("empty_queue", "blocked_job", "no_progress", "planner_review")
ACTIVE = frozenset({"pending", "running", "waiting"})
QUEUED = frozenset({"pending", "ready"})
REMEDY_OPERATION = {"retry": "retry", "pause": "pause", "add_nodes": "plan", "wait": "wait"}
MAX_NEW_NODES = 3
# About 6,000 tokens at four characters per token.
DOCUMENT_CHAR_CAP = 24000
TRUNCATED = "...<truncated>"


@dataclass(frozen=True)
class IncidentConfig:
    """Incident thresholds; each default is the documented rule.

    - ``blocked_grace_seconds`` (60): a blocked job becomes an incident after this.
    - ``idle_grace_seconds`` (60): an empty queue becomes an incident after this.
    - ``progress_minutes`` (10): no success for this long, with repetitive actions.
    - ``repeat_share`` (0.5): share of the last 100 actions held by the top two.
    - ``incident_retry_seconds`` (300): one consultation per incident per window.
    - ``outcome_minutes`` (10): an episode still open after this is ``expired``.
    - ``review_minutes`` (None): periodic ``planner_review``; off by default.
    - ``rejection_log_seconds`` (300): one ``decision_rejected`` per incident per window.
    """

    blocked_grace_seconds: float = 60
    idle_grace_seconds: float = 60
    progress_minutes: float = 10
    repeat_share: float = 0.5
    incident_retry_seconds: float = 300
    outcome_minutes: float = 10
    review_minutes: float | None = None
    rejection_log_seconds: float = 300


@dataclass(frozen=True)
class Remedy:
    """One allowed answer: ``retry``/``pause`` a queue task, ``add_nodes`` or ``wait``."""

    kind: str
    target_task_id: str | None = None
    capability: str | None = None
    capabilities: Mapping[str, Any] | None = None
    parameters: Mapping[str, Any] | None = None

    def to_dict(self) -> dict:
        row: dict[str, Any] = {"remedy": self.kind, "operation": REMEDY_OPERATION[self.kind]}
        if self.target_task_id is not None:
            row.update(
                target_task_id=self.target_task_id,
                capability=self.capability,
                parameters_json=json.dumps(dict(self.parameters or {}), separators=(",", ":"), sort_keys=True),
            )
        if self.kind == "add_nodes":
            row.update(max_nodes=MAX_NEW_NODES, capabilities=dict(self.capabilities or {}))
        if self.kind == "wait":
            row["reason_required"] = True
        return row


@dataclass
class Incident:
    """One open episode; ``id`` is stable for the same kind and subject."""

    id: str
    kind: str
    subject: str
    since: float
    facts: dict
    remedies: list[Remedy]
    opened_at: float = 0.0
    capability: str | None = None
    remedy: str | None = None

    def to_dict(self) -> dict:
        return dict(
            id=self.id,
            kind=self.kind,
            subject=self.subject,
            since=self.since,
            facts=self.facts,
            remedies=[r.to_dict() for r in self.remedies],
        )


@dataclass
class Inputs:
    """What the supervisor knows at one check; ``intents`` is read only when needed."""

    now: float
    jobs: list[dict]
    nodes: list[dict]
    observation: Mapping
    capabilities: Mapping
    auto_retries: Mapping[str, int] = field(default_factory=dict)
    intents: Callable[[], list[dict]] | None = None
    roadmap: Iterable | None = None
    recent_technologies: list[str] = field(default_factory=list)


@dataclass
class Changes:
    opened: list[Incident] = field(default_factory=list)
    closed: list[tuple[Incident, str, float]] = field(default_factory=list)


def incident_id(kind: str, subject: str) -> str:
    return hashlib.sha256((kind + "\n" + subject).encode()).hexdigest()[:16]


def _text(value, limit=160) -> str:
    return str(value if value is not None else "")[:limit]


def unknown_outcome(job: Mapping) -> bool:
    """An action whose result is unknown is never retried by the framework or the model."""
    return job.get("pending_action") is not None or "outcome_unknown" in str(job.get("reason", ""))


def repeat_summary(intents: list[Mapping], capability_by_key: Mapping, top: int = 3) -> tuple[list[dict], float]:
    """Most common recent actions and the share held by the top two (0 for no actions)."""
    counts = Counter(
        (
            capability_by_key.get(r.get("key")),
            (r.get("action") or {}).get("type"),
            (r.get("action") or {}).get("item") or (r.get("action") or {}).get("name"),
            (r.get("action") or {}).get("count"),
        )
        for r in intents
    ).most_common(top)
    rows = [{"job": j, "type": t, "item": i, "count": c, "times": n} for (j, t, i, c), n in counts]
    share = round(sum(n for _, n in counts[:2]) / len(intents), 2) if intents else 0.0
    return rows, share


def _names(value) -> set[str]:
    if isinstance(value, Mapping):
        return {k for k, v in value.items() if v}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {v for v in value if isinstance(v, str)}
    return set()


def roadmap_position(jobs, nodes, observation: Mapping, capabilities: Mapping, steps, limit: int = 5) -> list[dict]:
    """The next unsatisfied roadmap steps and what each waits on (empty list: roadmap finished).

    A step is satisfied by a succeeded job with the same capability and
    parameters, or by its technology being researched. ``waiting_on`` lists
    missing ``requires``, unsatisfied earlier steps, locked native recipes,
    unavailable research, a missing capability, or the open/blocked job or node.
    """
    researched = _names(observation.get("technologies"))
    succeeded = {j.get("capability") for j in jobs if j.get("status") == "succeeded"}

    def same(row, step):
        return row.get("capability") == step.capability and (row.get("parameters") or {}) == dict(step.parameters)

    def satisfied(step):
        if step.parameters.get("technology") in researched:
            return True
        return any(same(j, step) and j.get("status") == "succeeded" for j in jobs)

    by_id = {step.id: step for step in steps}
    gates = observation.get("native_capability_gates")
    available = observation.get("available_research")
    listed = None
    if isinstance(available, Mapping) and isinstance(available.get("technologies"), list):
        listed = {row.get("technology") for row in available["technologies"] if isinstance(row, Mapping)}
    rows = []
    for step in steps:
        if satisfied(step):
            continue
        waiting = []
        if step.capability not in capabilities:
            waiting.append("capability not offered")
        else:
            missing = [r for r in capabilities[step.capability].get("requires", []) if r not in succeeded]
            if missing:
                waiting.append("requires " + ",".join(missing))
            gate = gates.get(step.capability) if isinstance(gates, Mapping) else None
            if isinstance(gate, Mapping) and gate.get("native_recipes_ready") is False:
                waiting.append("native recipes locked")
        after = [name for name in step.after if name in by_id and not satisfied(by_id[name])]
        if after:
            waiting.append("after " + ",".join(after))
        technology = step.parameters.get("technology")
        if technology and listed is not None and technology not in listed:
            waiting.append("technology " + technology + " not researchable yet")
        job = next((j for j in jobs if same(j, step)), None)
        if job is not None:
            waiting.append("job " + _text(job.get("status"), 20) + ": " + _text(job.get("reason"), 80))
        elif any(same(q, step) and q.get("status") != "completed" for q in nodes):
            waiting.append("queued")
        rows.append(dict(step=step.id, capability=step.capability, ready=not waiting, waiting_on=waiting))
        if len(rows) >= limit:
            break
    return rows


def addable_capabilities(jobs, capabilities: Mapping, observation: Mapping) -> dict:
    """Capabilities a new node may use: ``requires`` satisfied and native recipes not locked."""
    succeeded = {j.get("capability") for j in jobs if j.get("status") == "succeeded"}
    gates = observation.get("native_capability_gates")
    allowed = {}
    for name, spec in sorted(capabilities.items()):
        if not set(spec.get("requires", [])) <= succeeded:
            continue
        gate = gates.get(name) if isinstance(gates, Mapping) else None
        if isinstance(gate, Mapping) and gate.get("native_recipes_ready") is False:
            continue
        allowed[name] = {"parameters": spec.get("parameters", {})}
    return allowed


class IncidentTracker:
    """Opens and closes incident episodes from successive checks; holds no IO.

    ``summarize`` turns a job into bounded evidence and ``text`` redacts one
    diagnostic string; the supervisor passes its redacting helpers.
    """

    def __init__(self, config: IncidentConfig | None = None, *, summarize=None, text=None, started_at: float = 0.0):
        self.config = config or IncidentConfig()
        self.summarize = summarize or (lambda job: {"reason": _text(job.get("reason"))})
        self.text = text or _text
        self.open: dict[str, Incident] = {}
        self.consulted: dict[str, float] = {}
        self.blocked_since: dict[str, float] = {}
        self.idle_since: float | None = None
        self.successes: int | None = None
        self.last_success_at = started_at
        self.last_review_at = started_at

    # -- detection -----------------------------------------------------------------

    def update(self, inputs: Inputs) -> Changes:
        """Close resolved/expired episodes, then open newly qualifying ones."""
        now = inputs.now
        jobs = inputs.jobs
        succeeded = sum(j.get("status") == "succeeded" for j in jobs)
        progressed = self.successes is not None and succeeded > self.successes
        if self.successes is None or progressed:
            self.successes = succeeded
            if progressed:
                self.last_success_at = now
        blocked = {j["key"]: j for j in jobs if j.get("status") == "blocked"}
        for key in list(self.blocked_since):
            if key not in blocked:
                del self.blocked_since[key]
        for key in blocked:
            self.blocked_since.setdefault(key, now)
        active = [j for j in jobs if j.get("status") in ACTIVE]
        queued = [q for q in inputs.nodes if q.get("status") in QUEUED]
        roadmap = None
        if inputs.roadmap is not None:
            roadmap = roadmap_position(jobs, inputs.nodes, inputs.observation, inputs.capabilities, inputs.roadmap)
        idle = not active and not queued and roadmap != []
        if not idle:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now

        changes = Changes()
        for incident in list(self.open.values()):
            outcome = self._outcome(incident, now, blocked, active, queued, progressed)
            if outcome:
                del self.open[incident.id]
                changes.closed.append((incident, outcome, round(now - incident.opened_at, 1)))
        for incident in self._detect(inputs, blocked, active, roadmap):
            if incident.id not in self.open:
                incident.opened_at = now
                self.open[incident.id] = incident
                changes.opened.append(incident)
            else:
                # Keep facts current for the next consultation; identity stays.
                self.open[incident.id].facts = incident.facts
                self.open[incident.id].remedies = incident.remedies
        return changes

    def _outcome(self, incident, now, blocked, active, queued, progressed) -> str | None:
        if incident.kind == "blocked_job" and incident.subject not in blocked:
            return "resolved"
        if incident.kind == "empty_queue" and (active or queued):
            return "resolved"
        if incident.kind in ("no_progress", "planner_review") and progressed:
            return "resolved"
        if now - incident.opened_at >= self.config.outcome_minutes * 60:
            return "expired"
        return None

    def _detect(self, inputs: Inputs, blocked, active, roadmap) -> list[Incident]:
        now = inputs.now
        config = self.config
        found = []
        nodes_by_key = {q.get("job_key"): q for q in inputs.nodes}
        addable = Remedy(
            "add_nodes", capabilities=addable_capabilities(inputs.jobs, inputs.capabilities, inputs.observation)
        )
        wait = Remedy("wait")

        def controls(kind, rows, limit=4):
            """``retry``/``pause`` entries for jobs with a queue node; unknown outcomes are never retried."""
            out = []
            for job in rows:
                node = nodes_by_key.get(job["key"])
                if node and not (kind == "retry" and unknown_outcome(job)):
                    out.append(Remedy(kind, node["id"], job.get("capability"), parameters=node.get("parameters")))
            return out[:limit]

        def retry_remedies(rows):
            return controls("retry", rows)

        def pause_remedies(rows):
            return controls("pause", rows)

        if self.idle_since is not None and now - self.idle_since >= config.idle_grace_seconds:
            facts = dict(
                idle_seconds=round(now - self.idle_since),
                roadmap=roadmap,
                blocked_jobs=[self._job_line(j) for j in list(blocked.values())[:6]],
                paused_jobs=sum(j.get("status") == "paused" for j in inputs.jobs),
            )
            found.append(
                Incident(
                    incident_id("empty_queue", "queue"),
                    "empty_queue",
                    "queue",
                    self.idle_since,
                    facts,
                    [*retry_remedies(blocked.values()), addable, wait],
                )
            )
        for key, job in blocked.items():
            since = self.blocked_since[key]
            if now - since < config.blocked_grace_seconds:
                continue
            node = nodes_by_key.get(key)
            reason = str(job.get("reason", ""))
            eligible = not unknown_outcome(job) and "rollback" not in reason and "deadline" not in reason
            dependents = [
                q["id"]
                for q in inputs.nodes
                if q.get("status") != "completed"
                and (
                    (node and node["id"] in q.get("depends_on", []))
                    or job.get("capability") in q.get("missing_prerequisites", [])
                )
            ][:8]
            facts = dict(
                capability=job.get("capability"),
                parameters=job.get("parameters", {}),
                task_id=node["id"] if node else None,
                reason=self.text(reason),
                evidence=self.summarize(job),
                blocked_seconds=round(now - since),
                auto_retries=int(inputs.auto_retries.get(key, 0)),
                auto_retry_eligible=eligible,
                unknown_outcome=unknown_outcome(job),
                dependent_task_ids=dependents,
            )
            found.append(
                Incident(
                    incident_id("blocked_job", key),
                    "blocked_job",
                    key,
                    since,
                    facts,
                    [*retry_remedies([job]), addable, wait],
                    capability=job.get("capability"),
                )
            )
        if active and now - self.last_success_at >= config.progress_minutes * 60 and inputs.intents is not None:
            window = now - config.progress_minutes * 60
            intents = [r for r in inputs.intents() if isinstance(r.get("time"), (int, float))][-100:]
            capability = {j["key"]: j.get("capability") for j in inputs.jobs}
            top, share = repeat_summary(intents, capability)
            if intents and share >= config.repeat_share:
                in_window = Counter(r.get("key") for r in intents if r["time"] >= window)
                facts = dict(
                    minutes_since_success=round((now - self.last_success_at) / 60, 1),
                    repeat_share=share,
                    top_repeated_action=top[0] if top else None,
                    active_jobs=[
                        dict(
                            capability=j.get("capability"),
                            reason=self.text(j.get("reason")),
                            actions_in_window=in_window.get(j["key"], 0),
                        )
                        for j in active[:4]
                    ],
                )
                found.append(
                    Incident(
                        incident_id("no_progress", "queue"),
                        "no_progress",
                        "queue",
                        self.last_success_at,
                        facts,
                        [*pause_remedies(active), addable, wait],
                    )
                )
        review = config.review_minutes
        if review and now - self.last_review_at >= review * 60:
            self.last_review_at = now
            facts = dict(
                minutes_since_success=round((now - self.last_success_at) / 60, 1),
                active_jobs=[self._job_line(j) for j in active[:4]],
                blocked_jobs=[self._job_line(j) for j in list(blocked.values())[:6]],
            )
            found.append(
                Incident(
                    incident_id("planner_review", "queue"),
                    "planner_review",
                    "queue",
                    now,
                    facts,
                    [*pause_remedies(active), *retry_remedies(blocked.values()), addable, wait],
                )
            )
        return found

    def _job_line(self, job) -> str:
        return " ".join(
            (
                _text(job.get("capability"), 40),
                _text(job.get("status"), 12),
                self.text(job.get("reason"))[:100],
            )
        )

    # -- consultation --------------------------------------------------------------

    def due(self, now: float) -> Incident | None:
        """The most severe open incident not consulted within ``incident_retry_seconds``."""
        rows = [
            q
            for q in self.open.values()
            if now - self.consulted.get(q.id, float("-inf")) >= self.config.incident_retry_seconds
        ]
        rows.sort(key=lambda q: (SEVERITY.index(q.kind), q.since, q.id))
        return rows[0] if rows else None

    def mark_consulted(self, incident: Incident, now: float):
        self.consulted[incident.id] = now


def check_decision(incident: Incident, decision: Mapping) -> str | None:
    """Why a validated decision is outside the incident's remedy menu, or None when it is allowed.

    ``retry``/``pause`` must name a menu target; ``plan`` adds 1..3 nodes of
    menu capabilities; ``wait`` needs a reason; ``request``/``continue`` are
    never on the menu.
    """
    operation = decision.get("operation")
    kinds = {r.kind: r for r in incident.remedies}
    if operation in ("retry", "pause"):
        targets = {r.target_task_id for r in incident.remedies if r.kind == operation}
        if decision.get("target_task_id") not in targets:
            return operation + " target is not on the menu"
        if decision.get("queue"):
            return operation + " must not add queue nodes"
        return None
    if operation == "plan":
        remedy = kinds.get("add_nodes")
        if remedy is None:
            return "add_nodes is not on the menu"
        nodes = decision.get("queue") or []
        if not 1 <= len(nodes) <= MAX_NEW_NODES:
            return "add_nodes needs 1.." + str(MAX_NEW_NODES) + " nodes"
        allowed = remedy.capabilities or {}
        outside = sorted({str(q.get("capability")) for q in nodes if q.get("capability") not in allowed})
        if outside:
            return "capability not on the menu: " + ",".join(outside)[:200]
        if decision.get("capability") is not None or decision.get("parameters"):
            return "plan carries nodes only"
        return None
    if operation == "wait":
        if "wait" not in kinds:
            return "wait is not on the menu"
        if not str(decision.get("reason", "")).strip():
            return "wait needs a reason"
        if decision.get("queue"):
            return "wait must not add queue nodes"
        return None
    return str(operation) + " is not on the menu"


def remedy_of(decision: Mapping) -> str:
    """The menu remedy a decision chose, by its operation."""
    reverse = {v: k for k, v in REMEDY_OPERATION.items()}
    return reverse.get(decision.get("operation"), str(decision.get("operation")))


# -- document ----------------------------------------------------------------------


def factory_status(
    observation: Mapping, jobs, roadmap_rows, recent_technologies, *, text=_text, limit_jobs: int = 12
) -> dict:
    """Short status: research, top entities, supply evidence, open jobs, next roadmap steps."""
    technologies = observation.get("technologies") or []
    entities = observation.get("entities") or {}
    counts = {}
    if isinstance(entities, Mapping):
        counts = dict(
            sorted(
                ((k, v) for k, v in entities.items() if isinstance(v, (int, float))),
                key=lambda kv: (-kv[1], kv[0]),
            )[:15]
        )
    supply = {}
    options = observation.get("resource_supply_options")
    if isinstance(options, Mapping):
        for item, row in list(options.items())[:12]:
            evidence = row.get("evidence") if isinstance(row, Mapping) else None
            if isinstance(evidence, Mapping):
                supply[item] = {
                    k: evidence.get(k)
                    for k in ("shortfall", "produced_per_minute", "consumed_per_minute", "net_per_minute")
                }
    lines = [
        " ".join((_text(j.get("capability"), 40), _text(j.get("status"), 12), text(j.get("reason"))[:100]))
        for j in jobs
        if j.get("status") in ACTIVE | {"blocked", "paused"}
    ][:limit_jobs]
    return dict(
        researched_technologies=len(technologies),
        recent_technologies=list(recent_technologies)[-10:],
        entity_counts=counts,
        supply=supply,
        jobs=lines,
        roadmap=roadmap_rows,
    )


def _shrink(value, items: int, chars: int, depth: int = 0):
    """Bound lists, mappings and strings, marking every cut."""
    if depth > 6:
        return TRUNCATED
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + TRUNCATED
    if isinstance(value, list):
        rows = [_shrink(v, items, chars, depth + 1) for v in value[:items]]
        return rows + [TRUNCATED] if len(value) > items else rows
    if isinstance(value, Mapping):
        keys = list(value)[: items * 3]
        out = {str(k): _shrink(value[k], items, chars, depth + 1) for k in keys}
        if len(value) > len(keys):
            out["_truncated"] = len(value) - len(keys)
        return out
    return value


def document(incident: Incident, factory: Mapping, *, cap: int = DOCUMENT_CHAR_CAP) -> dict:
    """The consultation input: incident, remedy menu and factory status, at most ``cap`` characters.

    Factory status shrinks first, then facts; the menu is kept whole when it
    fits on its own, otherwise its contract strings shrink too.
    """
    menu = [r.to_dict() for r in incident.remedies]
    head = dict(id=incident.id, kind=incident.kind, subject=incident.subject, since=incident.since)
    for items, chars in ((64, 400), (16, 160), (8, 80), (4, 40), (2, 20)):
        doc = dict(
            incident=dict(head, facts=_shrink(incident.facts, items, chars)),
            remedies=menu,
            factory=_shrink(factory, items, chars),
        )
        if len(_encode(doc)) <= cap:
            return doc
    for chars in (80, 24, 8):
        doc = dict(
            incident=dict(head, facts=TRUNCATED),
            remedies=_shrink(menu, 64, chars),
            factory=TRUNCATED,
        )
        if len(_encode(doc)) <= cap:
            return doc
    return dict(incident=dict(head, facts=TRUNCATED), remedies=TRUNCATED, factory=TRUNCATED)


def _encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
