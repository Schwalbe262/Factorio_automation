"""Second lane of a mine output trunk and its 2-2 input-balanced splitter (``mine_trunk.lane_plan``).

Proposed once per resource by ``mine_trunk.mine_trunk_nodes`` when the trunk
receipt says ``second_lane_required`` (the planned demand needs two belts).
The lane is recorded in ``mine-trunks.json`` and published as a board block
before anything is built; the first lane's belt the splitter replaces is
released from its owner's plan first, every item is carried, then the one
belt is mined by unit and the splitter and lane belts are built
(``mine_trunk.cutover``). Its head port is where the next row lane of the
patch joins (``MiningOutposts.prepare_connected_bank``).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import mine_trunk

CAPABILITY = mine_trunk.LANE_CAPABILITY
RESOURCES = ("iron-ore", "copper-ore", "coal", "stone")


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"resource"} or parameters["resource"] not in RESOURCES:
        raise ValueError("mine_trunk_lane requires resource iron-ore|copper-ore|coal|stone")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class TrunkLane:
    def __init__(self, game, builder, catalog, *, outposts=None, smelting=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.outposts, self.smelting = outposts, smelting
        self.root = Path(game.cfg.runtime_dir)

    def _bank(self, obs, resource):
        mining = _read(self.root / "mining-outposts.json")
        if mining.get("world_id") != obs["world_id"]:
            return None
        return mine_trunk.patch_bank(mining.get("banks"), resource, mine_trunk.patch_seed(obs, resource))

    def _lane(self, obs, bank):
        trunk = mine_trunk.ledger(self.root, obs["world_id"])["trunks"].get(mine_trunk.trunk_key(bank), {})
        return (trunk.get("lanes") or {}).get("1")

    def _block(self, lane):
        belts = [e for e in lane["entities"] if e["name"] == "transport-belt"]
        plan = mine_trunk.tap_plan(lane)
        area = mine_trunk.tap_area(belts or lane["entities"])
        return "mine_trunk_lane:" + lane["trunk"], dict(
            plan, reserved_area=area, owner="mine_trunk", trunk=lane["trunk"]
        )

    def prepare(self, obs, bank) -> dict:
        from .board import blocks_without_bus, surface_conflict
        from .planning import preflight

        board = _read(self.root / "pcb-board.json")
        blocks = blocks_without_bus(board) if board.get("world_id") == obs["world_id"] else {}
        failures = []

        def accept(lane):
            if surface_conflict(self.builder, obs, blocks, lane["entities"]) is not None:
                failures.append({"reason": "mine_trunk_lane_meets_planned_hardware", "along": lane["along"]})
                return False
            check = preflight(self.game, self.builder, obs, lane["entities"], ignored_units=tuple(lane["remove_units"]))
            if not check.get("ok"):
                failures.append({"reason": check.get("reason"), "along": lane["along"]})
                return False
            return True

        key = mine_trunk.trunk_key(bank)
        planned = mine_trunk.lane_plan(
            bank, obs, stations=mine_trunk.stations(self.root, obs["world_id"], key), accept=accept
        )
        return dict(planned, failures=failures[-6:]) if not planned.get("ok") else planned

    def step(self, obs, parameters):
        resource = parameters["resource"]
        bank = self._bank(obs, resource)
        if bank is None:
            return {"status": "blocked", "reason": "mine_trunk_lane_patch_bank_missing"}
        lane = self._lane(obs, bank)
        if lane is None:
            planned = self.prepare(obs, bank)
            if not planned.get("ok"):
                return {"status": "blocked", "reason": planned["reason"], "evidence": planned}
            lane = planned["lane"]
        result = mine_trunk.record_lane(self.root, obs["world_id"], lane, block=self._block(lane))
        if result:
            return result
        changed = mine_trunk.release_trunk_belts(self.root, obs["world_id"], lane)
        if "smelting_district" in changed and self.smelting is not None:
            self.smelting.state = _read(self.root / "smelting-district.json")
        if any(owner != "smelting_district" for owner in changed) and self.outposts is not None:
            self.outposts.state = _read(self.root / "mining-outposts.json")
        action = mine_trunk.cutover(self.builder, obs, lane)
        if lane.get("built"):
            mine_trunk.record_lane(self.root, obs["world_id"], lane)
        if action is not None:
            return action
        proof = self.proof(obs, parameters)
        if proof is None:
            return {"status": "waiting", "reason": "waiting for the second trunk lane and its balanced splitter"}
        return {"status": "succeeded", "reason": "second trunk lane with a 2-2 balanced splitter", "evidence": proof}

    def proof(self, obs, parameters):
        bank = self._bank(obs, parameters["resource"])
        lane = self._lane(obs, bank) if bank else None
        if not lane or not lane.get("built") or not mine_trunk.tap_standing(obs, lane):
            return None
        return {
            "tick": obs.get("tick"),
            "trunk": lane["trunk"],
            "along": lane["along"],
            "lanes_built": 2,
            "balancer": lane["balancer"],
            "head_port": lane["port"],
        }
