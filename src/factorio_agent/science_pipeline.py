"""One approved science batch ahead: back-to-back finite bootstrap batches.

Rule (PLANNERS.md "Back-to-back bootstrap batches"): while a finite
bootstrap batch (the owner) is fully funded and its research runs, the next
approved bootstrap node in the work queue (highest priority first; any
finite red-only technology the temporary cell can serve, ``bootstrap_research``
or ``bootstrap_logistics``) is reserved and funded from plates no other
critical job waits for, so its packs are made while the owner is still
researched and the labs roll straight on. The next batch's counter baselines
are the owner's batch end (``BootstrapLogistics.funding_complete``).
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json
from .industry import BootstrapLogistics, _red_only_fixed

OWNER_CAPABILITIES = ("bootstrap_research", "starter_research", "bootstrap_logistics")


def job_technology(row) -> str | None:
    """The technology a bootstrap job or work-queue node researches (``parameters.technology`` or STEP_TECHNOLOGY)."""
    from .progression import STEP_TECHNOLOGY

    parameters = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
    technology = parameters.get("technology") or STEP_TECHNOLOGY.get(row.get("capability"))
    return technology if isinstance(technology, str) else None


class SciencePipeline:
    def __init__(self, game, bootstrap, builder, catalog):
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.path = Path(game.cfg.runtime_dir) / "science-pipeline.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.last_diagnostic = None
        self.target_controller = None

    def _read(self, name):
        path = self.path.parent / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _save(self):
        _atomic_json(self.path, self.state)

    def _decline(self, reason, **evidence):
        self.last_diagnostic = dict(reason=reason, **evidence)
        return None

    @staticmethod
    def _target(node):
        return {k: deepcopy(node[k]) for k in ("id", "job_key", "capability", "parameters")}

    def _claimed(self, obs, jobs, exclude):
        """``{item: units or None}`` that active critical jobs other than ``exclude`` wait for (critical_path claims)."""
        from .critical_path import critical_claims, critical_jobs, critical_path, known_items
        from .jobs import CAPABILITIES

        world = {k: j for k, j in (jobs or {}).items() if j.get("world_id", obs["world_id"]) == obs["world_id"]}
        path = critical_path(world.values(), obs, CAPABILITIES, catalog=self.catalog)
        known = known_items(self.catalog, obs)
        keys = [k for k in critical_jobs(world, path, known_items=known) if k not in set(exclude)]
        return {item: row.get("units") for item, row in critical_claims(world, keys, known).items()}

    def _surplus(self, obs, controller, jobs=None, exclude=()):
        """Plates the next batch may take: held plus public furnace output, minus other critical jobs' claims.

        The next batch is itself on the critical path (the next research
        step), so it is not held back by the lookahead construction bill of
        later steps, only by units another active critical job waits for
        (a claim without a unit count keeps the whole item).
        """
        claimed = self._claimed(obs, jobs, exclude)
        _, inputs = controller.requirements()
        result = {}
        for item in inputs:
            filtered = obs
            filter_ = getattr(self.bootstrap, "_unreserved_stock_observation", None)
            if callable(filter_):
                filtered = filter_(obs, item)
            seen = set()
            public = 0
            for e in filtered.get("entities", []):
                if e.get("name") not in ("stone-furnace", "steel-furnace"):
                    continue
                unit = e.get("unit_number")
                if type(unit) is not int or unit in seen:
                    continue
                seen.add(unit)
                amount = e.get("output_inventory", {}).get(item, 0)
                if type(amount) is int and amount > 0:
                    public += amount
            held = obs.get("inventory", {}).get(item, 0)
            if type(held) is not int or held < 0:
                raise ValueError("observed integer actor plate inventory required")
            if item not in claimed:
                kept = 0
            elif claimed[item] is None:
                kept = public + held
            else:
                kept = math.ceil(claimed[item])
            result[item] = max(0, public + held - kept)
        return result

    def _supported(self, node) -> str | None:
        """The technology of a node the temporary cell can serve next, else None."""
        technology = job_technology(node)
        if node.get("capability") == "bootstrap_logistics" and node.get("parameters") == {}:
            return technology
        if (
            node.get("capability") == "bootstrap_research"
            and isinstance(node.get("parameters"), dict)
            and set(node["parameters"]) == {"technology"}
            and _red_only_fixed(self.catalog, technology)
        ):
            return technology
        return None

    def next_action(self, obs, owner, *, owner_key=None, surplus_budget=None):
        self.last_diagnostic = None
        if not isinstance(owner, BootstrapLogistics):
            return self._decline("science_pipeline_owner_not_a_bootstrap_batch")
        if not obs.get("ok", True) or not obs.get("world_id"):
            return self._decline("science_pipeline_observation_invalid")
        if any((self.path.parent / name).exists() for name in ("stop.json", "supervisor-stop.json")):
            return self._decline("science_pipeline_stop_requested")
        complete = owner.funding_complete(obs)
        if not complete or obs.get("research") != owner.technology or obs.get("technologies", {}).get(owner.technology):
            return self._decline("science_pipeline_current_research_has_priority")
        try:
            ledger = self._read("jobs.json")
            queue = self._read("work_queue.json")
            if (
                ledger.get("schema_version") != 2
                or queue.get("schema_version") != 1
                or ledger.get("world_id") != obs["world_id"]
                or queue.get("world_id") != obs["world_id"]
            ):
                return self._decline("science_pipeline_queue_or_ledger_world_changed")
            jobs = ledger.get("jobs", {})
            if any(
                "pending_action" in j or "outcome_unknown" in str(j.get("reason", ""))
                for j in jobs.values()
                if j.get("world_id") == obs["world_id"]
            ):
                return self._decline("science_pipeline_unresolved_world_action")

            def owns(job):
                return (
                    job.get("world_id") == obs["world_id"]
                    and job.get("capability") in OWNER_CAPABILITIES
                    and job_technology(job) == owner.technology
                    and job.get("status") in ("running", "waiting")
                )

            if owner_key is None:
                owners = [k for k, j in jobs.items() if owns(j)]
                if len(owners) != 1:
                    return self._decline("science_pipeline_unique_active_owner_required")
                owner_key = owners[0]
            if not owns(jobs.get(owner_key, {})):
                return self._decline("science_pipeline_owner_job_changed")
            nodes = {q["id"]: q for q in queue.get("nodes", [])}
            owner_nodes = [n for n in nodes.values() if n.get("job_key") == owner_key and not n.get("paused")]
            if len(owner_nodes) != 1:
                return self._decline("science_pipeline_approved_owner_node_required")
            owner_id = owner_nodes[0]["id"]
            if self.state:
                reserved = job_technology(self.state.get("target", {}))
                # The reservation was consumed: its target became the owner
                # (or was researched). The next pair starts a fresh reservation.
                if reserved == owner.technology or obs.get("technologies", {}).get(reserved):
                    self.state = {}
            eligible = []
            for node in nodes.values():
                technology = self._supported(node)
                if (
                    technology is None
                    or technology == owner.technology
                    or obs.get("technologies", {}).get(technology)
                    or node.get("paused")
                    or node.get("status") not in ("pending", "ready")
                    or type(node.get("priority")) is not int
                    or not isinstance(node.get("job_key"), str)
                    or not node["job_key"]
                ):
                    continue
                if any(
                    d not in nodes or d != owner_id and nodes[d].get("status") != "completed"
                    for d in node.get("depends_on", [])
                ):
                    continue
                future = jobs.get(node["job_key"])
                if future and (
                    future.get("world_id") != obs["world_id"]
                    or future.get("status") in ("paused", "blocked", "failed", "succeeded")
                ):
                    continue
                eligible.append(node)
            if self.state:
                if (
                    self.state.get("world_id") != obs["world_id"]
                    or self.state.get("catalog_fingerprint") != self.catalog.fingerprint
                    or self.state.get("owner_key") != owner_key
                    or self.state.get("owner_guard") != complete["owner_guard"]
                ):
                    return self._decline("science_pipeline_reserved_owner_changed")
                node = next((q for q in eligible if self._target(q) == self.state["target"]), None)
                if node is None:
                    return self._decline("science_pipeline_reserved_target_not_approved_or_paused")
            else:
                if not eligible:
                    return self._decline("science_pipeline_no_approved_successor")
                node = min(eligible, key=lambda q: (-q["priority"], q.get("created_at", 0), q["id"]))
            controller = BootstrapLogistics(
                self.game, self.bootstrap, self.builder, self.catalog, technology=self._supported(node)
            )
            self.target_controller = controller
            if controller.state and not controller.state.get("prewarm"):
                return self._decline("science_pipeline_existing_target_requires_primary_resume")
            if surplus_budget is None:
                budget = self._surplus(obs, controller, jobs, exclude=(owner_key, node["job_key"]))
            else:
                budget = surplus_budget
            if not self.state:
                self.state = dict(
                    schema_version=1,
                    world_id=obs["world_id"],
                    catalog_fingerprint=self.catalog.fingerprint,
                    owner_key=owner_key,
                    owner_guard=complete["owner_guard"],
                    target=self._target(node),
                    phase="prefill",
                )
                self._save()
            result = controller.prewarm(obs, owner=owner, surplus_budget=budget)
            self.last_diagnostic = result
            if result.get("type"):
                return result
            return None
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return self._decline("science_pipeline_context_invalid", detail=str(exc))

    def _target_controller(self):
        technology = job_technology(self.state.get("target", {})) or "logistics"
        return BootstrapLogistics(self.game, self.bootstrap, self.builder, self.catalog, technology=technology)

    def record_action(self, action, receipt):
        if not self.state:
            return
        controller = self._target_controller()
        if controller.state.get("pending_transfer", {}).get("action") != action:
            return
        if (
            controller.state.get("world_id") != self.state.get("world_id")
            or controller.state.get("catalog_fingerprint") != self.catalog.fingerprint
        ):
            raise ValueError("science pipeline pending world/catalog changed; reconcile")
        warm = controller.state.get("prewarm", {})
        if warm.get("owner_guard") != self.state.get("owner_guard"):
            raise ValueError("science pipeline pending owner changed; reconcile")
        controller.record_action(action, receipt)
        self.target_controller = controller
        if controller.state["allocated_inputs"] == controller.state["required_inputs"]:
            self.state["phase"] = "funded"
            self._save()
