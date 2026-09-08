"""Supply finite building ingredients from normally constructed production."""
from __future__ import annotations


def _report(status: str, reason: str, **evidence) -> dict:
    return {"status": status, "reason": reason, "evidence": evidence}


class ConstructionMaterials:
    """Bridge only a builder's proven machine-only handcraft dependency.

    The bootstrap keeps owning the final paid handcraft. This helper establishes
    the missing ingredient's producer, then ordinary bounded output collection
    resumes on the next observation. Nested producer construction cannot recurse
    through the same ingredient indefinitely.
    """

    def __init__(self, factory):
        self.factory = factory
        self._stack: tuple[str, ...] = ()

    def ensure(self, observation: dict, blocked: dict) -> dict:
        if (blocked.get("status") != "blocked"
                or blocked.get("reason") != "required item needs a production machine"):
            return blocked
        evidence = blocked.get("evidence", {})
        item, required = evidence.get("item"), evidence.get("need")
        if not isinstance(item, str) or not item or type(required) is not int or required < 1:
            return _report("blocked", "machine-made construction ingredient has no bounded demand", **evidence)
        if item in self._stack:
            return _report("blocked", "construction material production dependency cycle", item=item, stack=list(self._stack))
        self._stack = (*self._stack, item)
        try:
            result = self.factory.ensure_product(observation, item)
            if result.get("status") != "succeeded" or result.get("type"):
                return result
            have = int(observation.get("inventory", {}).get(item, 0))
            if have >= required:
                return _report("waiting", "reobserve available construction ingredient before continuing its handcraft",
                               item=item, have=have, need=required)
            action = self.factory.bootstrap._take_output(observation, item, required - have)
            return action or _report("waiting", "waiting for machine-made construction ingredient output",
                                     item=item, have=have, need=required)
        finally:
            self._stack = self._stack[:-1]
