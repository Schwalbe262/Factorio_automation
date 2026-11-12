import ast
import inspect
from pathlib import Path
import textwrap
import unittest
from unittest.mock import Mock, patch

from factorio_agent import handlers
from factorio_agent.capability_registry import get_handler, handler_for, registered_names
from factorio_agent.executors import StarterExecutors
from factorio_agent.jobs import CAPABILITIES, Request

HANDLER_DIR = Path(handlers.__file__).parent
MAX_HANDLER_MODULE_LINES = 400
# The shared prelude of StarterExecutors.step: nest_assault (retired, answered
# there and deliberately absent from CAPABILITIES and the registry),
# starter_research already researched, the gun-turret bridge, and the two
# initial-defense conditions.
MAX_PRELUDE_CAPABILITY_COMPARISONS = 5


def _capability_literal_comparisons(function) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        names = any(isinstance(o, ast.Name) and o.id == "capability" for o in operands)
        literal = any(isinstance(o, ast.Constant) and isinstance(o.value, str) for o in operands)
        count += names and literal
    return count


class CapabilityRegistryTests(unittest.TestCase):
    def test_every_capability_has_exactly_one_handler(self):
        self.assertEqual(set(CAPABILITIES), set(registered_names()))
        self.assertNotIn("nest_assault", registered_names())

    def test_handler_modules_stay_small(self):
        for path in sorted(HANDLER_DIR.glob("*.py")):
            with self.subTest(module=path.name):
                lines = len(path.read_text(encoding="utf-8").splitlines())
                self.assertLessEqual(lines, MAX_HANDLER_MODULE_LINES)

    def test_every_handler_module_is_loaded(self):
        modules = {path.stem for path in HANDLER_DIR.glob("*.py")} - {"__init__", "upkeep"}
        self.assertEqual(modules, set(handlers.MODULES))

    def test_capabilities_with_documented_parameters_validate_them(self):
        for name, spec in CAPABILITIES.items():
            if spec["parameters"]:
                with self.subTest(capability=name):
                    self.assertIsNotNone(handler_for(name).validate)

    def test_dispatch_has_no_capability_chain(self):
        self.assertLessEqual(_capability_literal_comparisons(StarterExecutors.step), MAX_PRELUDE_CAPABILITY_COMPARISONS)
        self.assertEqual(_capability_literal_comparisons(StarterExecutors.proof), 0)
        self.assertEqual(_capability_literal_comparisons(StarterExecutors.record_action), 0)
        self.assertEqual(_capability_literal_comparisons(Request.validate), 0)

    def test_unknown_capability_raises_the_dispatch_error(self):
        ex = StarterExecutors.__new__(StarterExecutors)
        ex._approved_initial_defense = Mock(return_value=None)
        with patch("factorio_agent.core.deterministic_repair_control.pending_repair", return_value=None):
            with self.assertRaisesRegex(ValueError, "^no executor for capability: no_such_capability$"):
                ex.step("no_such_capability", {"tick": 1})
        self.assertIsNone(get_handler("no_such_capability"))
        with self.assertRaisesRegex(ValueError, "^no executor for capability: no_such_capability$"):
            handler_for("no_such_capability")

    def test_unknown_capability_request_is_refused_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, "^capability_not_implemented: no_such_capability$"):
            Request("k", "w", 1, "no_such_capability").validate()
        with self.assertRaisesRegex(ValueError, "^this capability accepts no parameters$"):
            Request("k", "w", 1, "power", {"x": 1}).validate()

    def test_jobs_still_exports_the_executors(self):
        from factorio_agent import jobs

        self.assertIs(jobs.StarterExecutors, StarterExecutors)


if __name__ == "__main__":
    unittest.main()
