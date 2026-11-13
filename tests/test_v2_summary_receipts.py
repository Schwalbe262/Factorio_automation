"""Every observation key a proposal rule reads must survive ``jobs.summary``.

The supervisor plans with the ``observe`` summary, not the raw observation. A
receipt computed in observe but dropped by ``summary`` silently disables the
rule that reads it (mine_trunks, produce_plate_sources and rocket_plan were all
lost that way on 2026-11-14). This test finds the readers by a static scan:

* roots: every module-level ``*_nodes`` function with an ``observation``
  parameter, every function of ``growth_providers`` with one, and every
  ``Supervisor`` method that unpacks ``observation`` from its snapshot context;
* reads: ``observation.get("k")``, ``observation["k"]`` and ``"k" in observation``
  on the parameter that carries the observation;
* calls that pass the observation on (positionally or by keyword) to a function
  of the package are followed, with the callee's parameter name.

Each key found must be a key of ``summary(...)``. A new rule that reads a key the
summary does not pass fails here until the key is added to ``jobs.summary``.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path
import unittest

from factorio_agent.jobs import summary

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "factorio_agent"
OBSERVATION_NAMES = ("observation", "obs")


@functools.lru_cache(maxsize=1)
def _parsed():
    return tuple((path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(PACKAGE.rglob("*.py")))


def _modules():
    return _parsed()


def _functions():
    """``name -> [FunctionDef]`` of every module-level function and class method of the package."""
    found: dict[str, list] = {}
    for _, tree in _modules():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found.setdefault(item.name, []).append(item)
    return found


def _params(function) -> list[str]:
    args = function.args
    return [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]


def _string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _carries(node, carrier: str) -> bool:
    """``carrier`` itself, or ``carrier or {}`` / ``carrier and ...`` passed on."""
    if isinstance(node, ast.Name):
        return node.id == carrier
    if isinstance(node, ast.BoolOp):
        return any(_carries(value, carrier) for value in node.values)
    return False


def _reads(function, carrier: str, functions, seen, keys) -> None:
    """Keys read from ``carrier`` in ``function`` and in the package functions it passes ``carrier`` to."""
    marker = (id(function), carrier)
    if marker in seen:
        return
    seen.add(marker)
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            if isinstance(target, ast.Name) and target.id == carrier and node.func.attr == "get" and node.args:
                key = _string(node.args[0])
                if key:
                    keys.add(key)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == carrier:
            key = _string(node.slice)
            if key:
                keys.add(key)
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            if any(isinstance(c, ast.Name) and c.id == carrier for c in node.comparators):
                key = _string(node.left)
                if key:
                    keys.add(key)
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            for callee in functions.get(name, ()):
                params = _params(callee)
                if params and params[0] in ("self", "cls"):
                    params = params[1:]
                for index, arg in enumerate(node.args):
                    if _carries(arg, carrier) and index < len(params):
                        _reads(callee, params[index], functions, seen, keys)
                for keyword in node.keywords:
                    if _carries(keyword.value, carrier) and keyword.arg in params:
                        _reads(callee, keyword.arg, functions, seen, keys)


def _roots():
    roots = []
    for path, tree in _modules():
        module = path.stem
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = _params(node)
            carrier = next((p for p in OBSERVATION_NAMES if p in params), None)
            if carrier and (node.name.endswith("_nodes") or module == "growth_providers"):
                roots.append((f"{module}.{node.name}", node, carrier))
            if module == "supervisor" and any(
                isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Tuple)
                and any(isinstance(e, ast.Name) and e.id == "observation" for e in n.targets[0].elts)
                for n in ast.walk(node)
            ):
                roots.append((f"supervisor.{node.name}", node, "observation"))
    return roots


def rule_keys() -> dict[str, set[str]]:
    """``key -> {root functions reading it}`` (module doc)."""
    functions = _functions()
    found: dict[str, set[str]] = {}
    for label, node, carrier in _roots():
        keys: set[str] = set()
        _reads(node, carrier, functions, set(), keys)
        for key in keys:
            found.setdefault(key, set()).add(label)
    return found


class SummaryPassesRuleReceiptsTests(unittest.TestCase):
    def test_the_scan_finds_the_known_readers(self):
        found = rule_keys()
        for key in ("mine_trunks", "produce_plate_sources", "rocket_plan", "rocket_stages", "technologies"):
            self.assertIn(key, found, key)

    def test_every_key_a_rule_reads_is_passed_by_the_summary(self):
        found = rule_keys()
        ledger = type("L", (), {"data": {"jobs": {}}})()
        passed = set(summary({**{k: {} for k in found}, "world_id": "w", "tick": 0}, ledger))
        missing = {key: sorted(readers)[:3] for key, readers in found.items() if key not in passed}
        self.assertEqual(missing, {}, "jobs.summary drops observation keys these rules read")

    def test_a_dropped_receipt_is_caught(self):
        from unittest.mock import patch

        from factorio_agent import jobs

        real = jobs.summary

        def dropping(obs, ledger):
            return {k: v for k, v in real(obs, ledger).items() if k != "mine_trunks"}

        with patch.object(jobs, "summary", dropping), patch(f"{__name__}.summary", dropping):
            with self.assertRaises(AssertionError):
                self.test_every_key_a_rule_reads_is_passed_by_the_summary()


if __name__ == "__main__":
    unittest.main()
