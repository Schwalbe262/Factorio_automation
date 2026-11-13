"""The temporary startup science cell is dismantled once the permanent red science block does its work.

Early research runs on a hand-funded startup cell (``board.startup_science_cell``:
gear machine, 1-2 science machines, chests, inserters; its lab slots; the
starter lab of ``starter_research``). After ``automation_science`` the
permanent row researches from bus-fed machine packs and the cell stands idle
holding labs (10 circuits, 10 gears, 4 belts each) and assemblers the
permanent row would craft again. Rules (PLANNERS.md "Obsolete blocks"),
built on the shared ``obsolete`` helpers:

1. Trigger (``permanent_science``): ``automation_science`` succeeded and the
   ``science_growth`` receipt shows research running, a built permanent lab
   working and its machines observed (the evidence growth reads).
2. Guards: no job of ``TEMP_RESEARCH`` is open, every roadmap step of those
   capabilities is satisfied, and no bootstrap batch is funded and unfinished
   (``industry.batch_refusal``: never dismantle a cell with paid plates in it).
3. Job ``startup_science_retirement`` (node once, ``RETIRE_PRIORITY``; just above
   ``science_growth`` while growth wants hardware, so labs are moved, not crafted):
   records the cell's non-pole entities, slot labs, the starter lab and the
   batch-bound labs once (never an entity of another board block), makes
   inventory room (``InventoryStorage``), mines inserters, machines, labs,
   chests (paid, guarded; contents return to the inventory), releases the
   board block, records ``phase: retired`` and delivers carried red packs to
   the permanent labs. Poles stay (other chains may hang on them).
4. Reuse: recovered labs and assemblers are carried stock; ``ensure_item``
   counts the inventory before crafting, so ``science_growth`` places them in
   reserved slots when its evidence rule grows the row. Until then they stay in
   the inventory (labs are not in ``inventory_storage.KEEP``: never deposited).
   While this job is dismantling and a recorded lab/assembler still stands,
   ``science_growth`` waits (``holds_for_growth``) instead of crafting.
5. Never rebuilt: ``industry`` refuses to reserve or build the cell once
   ``dismantled``.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import obsolete
from .core.deterministic_state import _atomic_json

CAPABILITY = "startup_science_retirement"
STATE_FILE = "startup-science-retirement.json"
NODE_ID = "auto_startup_science_retirement"
BLOCK = "startup_science"
SUCCESSOR = "automation_science"
TEMP_RESEARCH = ("starter_research", "bootstrap_research", "bootstrap_logistics")
RED = "automation-science-pack"
POLE = "small-electric-pole"
ORDER = {"inserter": 0, "assembling-machine-1": 1, "lab": 2}  # chests last
REUSED = ("lab", "assembling-machine-1")  # items the permanent row's slots take
LAB_PACK_STACK = 200
RETIRE_PRIORITY = obsolete.RETIRE_PRIORITY


def _key(e):
    p = e.get("position") or {}
    return (e.get("name"), p.get("x"), p.get("y"))


def _names(technologies):
    if isinstance(technologies, dict):
        return {k for k, v in technologies.items() if v}
    return set(technologies or ())


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dismantled(root, world_id) -> bool:
    """The cell of this world is being or was dismantled: it is never reserved or built again."""
    state = _read(Path(root) / STATE_FILE)
    return state.get("world_id") == world_id and state.get("phase") in ("dismantling", "retired")


# ----- planner ----------------------------------------------------------------
def permanent_science(observation) -> bool:
    """The permanent row researches from machine-made packs (``science_growth`` receipt evidence)."""
    row = observation.get("science_growth")
    evidence = row.get("evidence") if isinstance(row, dict) else None
    research = observation.get("research")
    if not isinstance(evidence, dict) or not isinstance(research, str) or not research:
        return False
    return (
        evidence.get("labs_powered", 0) >= 1
        and evidence.get("machines_observed", 0) >= 1
        and "working" in (evidence.get("lab_status") or [])
    )


def temp_research_done(jobs, observation) -> bool:
    """No temporary research job is open and every roadmap step that uses the cell is satisfied."""
    jobs = list(jobs)
    if obsolete.active_jobs(jobs, capabilities=TEMP_RESEARCH, statuses=obsolete.ACTIVE + ("paused",)):
        return False
    from .progression import ROADMAP

    researched = _names(observation.get("technologies"))
    done = {
        (j.get("capability"), json.dumps(j.get("parameters") or {}, sort_keys=True))
        for j in jobs
        if j.get("status") == "succeeded"
    }
    for step in ROADMAP:
        if step.capability not in TEMP_RESEARCH:
            continue
        technology = step.parameters.get("technology", "logistics")
        if technology in researched or (step.capability, json.dumps(step.parameters, sort_keys=True)) in done:
            continue
        return False
    return True


def science_retirement_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One retirement node per map once permanent science is verified and the temporary research is over."""
    if CAPABILITY not in capabilities:
        return []
    jobs = list(jobs)
    succeeded = obsolete.succeeded(jobs)
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded or SUCCESSOR not in succeeded:
        return []
    if not succeeded & set(TEMP_RESEARCH):  # no temporary science hardware was ever built
        return []
    if obsolete.proposed(jobs, queue_nodes, CAPABILITY):
        return []
    if not permanent_science(observation) or not temp_research_done(jobs, observation):
        return []
    growth = observation.get("science_growth") or {}
    if growth.get("grow"):
        from .progression import SCIENCE_GROWTH_PRIORITY

        # Move, not craft: dismantle before the growth job asks for new labs/machines.
        priority, note = SCIENCE_GROWTH_PRIORITY + 1, "; growth wants labs/machines: move them"
    else:
        priority, note = RETIRE_PRIORITY, ""
    return [
        obsolete.retire_node(
            NODE_ID,
            CAPABILITY,
            {},
            "permanent red science researches from machine packs: dismantle the startup science cell" + note,
            priority,
        )
    ]


def validate_parameters(parameters):
    if parameters not in ({}, None):
        raise ValueError("startup_science_retirement takes no parameters")


# ----- job --------------------------------------------------------------------
class StartupScienceRetirement:
    def __init__(self, game, catalog, storage=None):
        self.game, self.catalog, self.storage = game, catalog, storage
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE

    def _state(self, world):
        state = _read(self.path)
        return state if state.get("world_id") == world else {"world_id": world, "phase": "retiring"}

    def _save(self, state):
        _atomic_json(self.path, state)

    def _jobs(self, world):
        ledger = _read(self.root / "jobs.json")
        if ledger.get("world_id") != world:
            return []
        return [dict(j, key=k) for k, j in (ledger.get("jobs") or {}).items() if j.get("world_id", world) == world]

    def _science(self, world):
        state = _read(self.root / "automation-science.json")
        return state if state.get("world_id") == world and state.get("labs") and state.get("science") else None

    def _permanent_labs(self, obs, science):
        from .science_growth import built_labs

        actual = {_key(e): e for e in obs.get("entities", [])}
        rows = [actual.get(_key(e)) for e in built_labs(science)]
        return [
            e for e in rows if e and e.get("electric_network_connected") is not False and (e.get("energy") or 0) > 0
        ]

    def _verified(self, obs, science):
        from .science_growth import lab_activity

        if science is None or not isinstance(obs.get("research"), str) or not obs.get("research"):
            return False
        activity = lab_activity(science, obs)
        return activity["machines_observed"] >= 1 and "working" in activity["lab_status"]

    def _record(self, world):
        """Entities of the temporary science hardware (cell without poles, slot labs, starter and batch labs)."""
        board = _read(self.root / "pcb-board.json")
        if board.get("world_id") != world:
            board = {}
        blocks = board.get("blocks") or {}
        plan = blocks.get(BLOCK) or {}
        foreign = set()
        for name, block in list(blocks.items()) + [("shared_main_bus", board.get("shared_main_bus") or {})]:
            if name != BLOCK:
                foreign.update(_key(e) for e in block.get("entities", []))
        specs = [e for e in plan.get("entities", []) if e.get("name") != POLE]
        if plan:
            from .board import lab_slot_entities

            specs += [slot["lab"] for slot in lab_slot_entities(plan)]
        site = (_read(self.root / "starter-research.json").get("lab_site") or {}).get("position")
        if isinstance(site, dict):
            specs.append({"name": "lab", "position": site})
        for path in sorted(self.root.glob("bootstrap-research-*.json")) + [self.root / "bootstrap-logistics.json"]:
            data = _read(path)
            if data.get("world_id") != world:
                continue
            specs += [e for e in [data.get("lab")] + list(data.get("labs") or []) if isinstance(e, dict)]
        rows, seen = [], set()
        for e in specs:
            key = _key(e)
            if None in key or key in seen or key in foreign:
                continue
            seen.add(key)
            rows.append({"name": e["name"], "position": dict(e["position"])})
        return sorted(rows, key=lambda e: ORDER.get(e["name"], 3))

    def step(self, obs, parameters=None):
        world = obs.get("world_id")
        state = self._state(world)
        if state.get("phase") != "retired":
            jobs = self._jobs(world)
            open_jobs = obsolete.active_jobs(jobs, capabilities=TEMP_RESEARCH, statuses=obsolete.ACTIVE + ("paused",))
            if open_jobs:
                return {
                    "status": "waiting",
                    "reason": "startup_science_retirement_waits_for_research_jobs",
                    "evidence": {"jobs": open_jobs},
                }
            from .industry import batch_refusal

            refusal = batch_refusal(self.root, obs)
            if refusal:
                return {
                    "status": "blocked",
                    "reason": "startup_science_retirement_batch_unfinished",
                    "evidence": {"batch": refusal},
                }
            science = self._science(world)
            if state.get("phase") == "retiring" and not self._verified(obs, science):
                return {"status": "blocked", "reason": "startup_science_retirement_without_permanent_science"}
            if "entities" not in state:
                state["entities"] = self._record(world)
                state["recorded_tick"] = obs.get("tick")
                self._save(state)
            by_key = {_key(e): e for e in obs.get("entities", [])}
            live = [by_key[_key(e)] for e in state["entities"] if _key(e) in by_key]
            if live:
                room = obsolete.room_action(obs, obsolete.slots_needed(self.catalog, live), self.storage)
                if room is not None:
                    return room
                for entity in live:
                    action = obsolete.guarded_mine(entity, world, "dismantle the obsolete startup science cell")
                    if action is None:
                        continue
                    if state.get("phase") != "dismantling":
                        state["phase"] = "dismantling"
                        state["dismantling_tick"] = obs.get("tick")
                        self._save(state)
                    return action
                return {"status": "blocked", "reason": "startup_science_retirement_entity_identity_missing"}
            released = obsolete.release_block(self.root / "pcb-board.json", world, BLOCK)
            obsolete.mark_retired(state, obs.get("tick"))
            state["block_released"] = released or obsolete.block_released(self.root / "pcb-board.json", BLOCK)
            self._save(state)
        delivery = self._deliver(obs)
        if delivery is not None:
            return delivery
        evidence = self.proof(obs)
        if evidence is None:
            return {"status": "waiting", "reason": "startup_science_retirement_not_observed_yet"}
        return {
            "status": "succeeded",
            "reason": "startup science cell dismantled, block released, labs and assemblers carried for reuse",
            "evidence": evidence,
        }

    def _deliver(self, obs):
        """Carried red packs (leftovers of the cell: no temporary research job is open) go to a permanent lab."""
        held = int((obs.get("inventory") or {}).get(RED, 0) or 0)
        science = self._science(obs.get("world_id"))
        if held <= 0 or science is None:
            return None
        room = [
            (int((e.get("inventory") or {}).get(RED, 0) or 0), e)
            for e in self._permanent_labs(obs, science)
            if int((e.get("inventory") or {}).get(RED, 0) or 0) < LAB_PACK_STACK
        ]
        if not room:
            return None  # every permanent lab is full: the packs stay carried for them
        stock, lab = min(room, key=lambda r: (r[0], _key(r[1])))
        return {
            "type": "insert",
            "name": "lab",
            "position": dict(lab["position"]),
            "inventory": "lab_input",
            "item": RED,
            "count": min(held, LAB_PACK_STACK - stock),
            "reason": "deliver leftover startup science packs to the permanent labs",
        }

    def proof(self, obs, parameters=None):
        state = self._state(obs.get("world_id"))
        if state.get("phase") != "retired":
            return None
        standing = {_key(e) for e in obs.get("entities", [])}
        if any(_key(e) in standing for e in state.get("entities", [])):
            return None
        if not obsolete.block_released(self.root / "pcb-board.json", BLOCK):
            return None
        counts = {}
        for e in state.get("entities", []):
            counts[e["name"]] = counts.get(e["name"], 0) + 1
        return {"tick": obs.get("tick"), "retired_tick": state.get("retired_tick"), "dismantled": counts}

    def holds_for_growth(self, obs):
        """A wait for ``science_growth`` while this job is dismantling labs/assemblers it can reuse, else None."""
        world = obs.get("world_id")
        state = self._state(world)
        if state.get("phase") != "dismantling":
            return None
        if not obsolete.active_jobs(self._jobs(world), capabilities=(CAPABILITY,)):
            return None  # a blocked retirement never holds growth back
        standing = {_key(e) for e in obs.get("entities", [])}
        left = [e["name"] for e in state.get("entities", []) if e["name"] in REUSED and _key(e) in standing]
        if not left:
            return None
        return {
            "status": "waiting",
            "reason": "science_growth_waits_for_moved_startup_hardware",
            "evidence": {"standing": sorted(set(left)), "count": len(left)},
        }
