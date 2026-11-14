"""Move a built small electric pole off a tile a route must pass (the remedy ladder's ``clear`` rung).

aud-1114-0526 seed 2446866888: the iron bank's power bridge put a pole at (23.5,10.5), the tile in
front of the iron lane head; the bus feed found no corridor for 25 minutes. Prevention is
``power_grid._plan_corridors``; this module repairs a pole that already stands. Owner rule
(2026-11-13, AGENTS.md "A built block is not frozen"): paid guarded removal and rebuild, the plan
republished first, never a consumer cut off without a replacement.

Order, one phase per step (``PoleRelocation.step``):

1. ``plan``: a world-bound read (``NETWORK_LUA``) finds what the pole feeds without it: the poles
   wired to it that lose every path to a generator (``cut``) and the machines only its supply area
   covers (``orphans``; a relocation never plans a new supply area for machines: refused). Each cut
   pole gets a pole-hop chain from the poles that keep power (``power_grid.plan_connection`` with
   ``sources``), off the pole's tile and the caller's ``keep_out``. The owner's plan is republished
   at once (:func:`republish`: the pole out, the chain in), so its owner never builds it back.
2. ``build``: the chain through the builder's ``ensure_plan`` (paid).
3. ``verify``: the same read again: nothing may be cut or orphaned any more.
4. ``mine``: a guarded ``mine`` of exactly that pole (unit number, world).

State ``pole-relocations.json``: ``{world_id, active, done}``; at most ``MAX_RELOCATIONS`` per world.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from .core.deterministic_builder import _plan
from .core.deterministic_state import _atomic_json

STATE_FILE = "pole-relocations.json"
POLE = "small-electric-pole"
MAX_RELOCATIONS = 4
FAULT = "pole_on_route_tile"
BOARD_FILE = "pcb-board.json"
OUTPOSTS_FILE = "mining-outposts.json"
# Run files that mention entities without owning a plan of them.
NOT_PLANS = frozenset(
    {
        STATE_FILE,
        "jobs.json",
        "work_queue.json",
        "catalog.json",
        "site-map.json",
        "bottleneck-samples.json",
        "rate-samples.json",
        "controller-state.json",
    }
)

NETWORK_LUA = r"""
local x=helpers.json_to_table(%s)
if not d or d.world_id~=x.world_id then return {ok=false,reason="pole_relocation_world_changed"} end
local p=target(x.pole.position,"small-electric-pole")
if not p or not p.valid or p.unit_number~=x.pole.unit_number or p.force~=f then return {ok=false,reason="pole_relocation_pole_changed"} end
local function copper(e)
 local c=e.get_wire_connector(defines.wire_connector_id.pole_copper,false);local out={}
 if c then for _,v in pairs(c.connections) do local o=v.target.owner
  if o and o.valid and o.type=="electric-pole" and o.force==f then out[#out+1]=o end
 end end
 return out
end
local function covers(q,b)
 local r=q.prototype.get_supply_area_distance()
 return q.position.x+r>b.left_top.x and q.position.x-r<b.right_bottom.x and q.position.y+r>b.left_top.y and q.position.y-r<b.right_bottom.y
end
local reach={};local todo={}
for _,g in pairs(s.find_entities_filtered{type={"generator","burner-generator","solar-panel"},force=f}) do
 local b=g.bounding_box
 for _,q in pairs(s.find_entities_filtered{type="electric-pole",force=f,area={{b.left_top.x-8,b.left_top.y-8},{b.right_bottom.x+8,b.right_bottom.y+8}}}) do
  if q~=p and not reach[q.unit_number] and covers(q,b) then reach[q.unit_number]=q;todo[#todo+1]=q end
 end
end
local i=1
while i<=#todo do local e=todo[i];i=i+1
 for _,o in ipairs(copper(e)) do if o~=p and not reach[o.unit_number] then reach[o.unit_number]=o;todo[#todo+1]=o end end
end
local cut={}
for _,o in ipairs(copper(p)) do if not reach[o.unit_number] then cut[#cut+1]={name=o.name,position=pos(o.position),unit_number=o.unit_number} end end
local r=p.prototype.get_supply_area_distance();local orphans={}
for _,e in pairs(s.find_entities_filtered{force=f,area={{p.position.x-r,p.position.y-r},{p.position.x+r,p.position.y+r}}}) do
 if e~=p and e.type~="electric-pole" and e.prototype.electric_energy_source_prototype then
  local b=e.bounding_box;local covered=false
  for _,q in pairs(s.find_entities_filtered{type="electric-pole",force=f,area={{b.left_top.x-8,b.left_top.y-8},{b.right_bottom.x+8,b.right_bottom.y+8}}}) do
   if reach[q.unit_number] and covers(q,b) then covered=true;break end
  end
  if not covered then orphans[#orphans+1]={name=e.name,position=pos(e.position)} end
 end
end
local reached={}
for _,q in pairs(reach) do reached[#reached+1]=pos(q.position) end
return {ok=true,cut=cut,orphans=orphans,reached=reached}
"""


def _xy(position: Mapping) -> tuple:
    return (position["x"], position["y"])


def pole_faults(obs: Mapping, tiles: Iterable) -> list[dict]:
    """Built small poles of ours on ``tiles`` (``(x, y)``): ``[{fault, position, found, unit_number}]``."""
    wanted = {tuple(t) for t in tiles}
    faults = []
    for e in obs.get("entities", ()):
        if e.get("name") != POLE or not isinstance(e.get("position"), Mapping):
            continue
        if _xy(e["position"]) in wanted and type(e.get("unit_number")) is int:
            faults.append(
                {"fault": FAULT, "position": dict(e["position"]), "found": POLE, "unit_number": e["unit_number"]}
            )
    return faults


def tile_keep_out(position: Mapping) -> list:
    """The 3x3 tiles around ``position``: a replacement pole never stands on or beside the cleared tile."""
    x, y = _xy(position)
    return [[[x - 1, y - 1], [x + 1, y + 1]]]


# ----- owner plans ---------------------------------------------------------------
def _is_pole(row, position) -> bool:
    return isinstance(row, Mapping) and row.get("name") == POLE and row.get("position") == position


def _mentions(value, position) -> bool:
    if _is_pole(value, position):
        return True
    if isinstance(value, Mapping):
        return any(_mentions(v, position) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, position) for v in value)
    return False


def _swap(entities: list, position, added: list) -> list:
    kept = [e for e in entities if not _is_pole(e, position)]
    have = {(e.get("name"), *_xy(e["position"])) for e in kept if isinstance(e, Mapping) and e.get("position")}
    return kept + [dict(e) for e in added if (e["name"], *_xy(e["position"])) not in have]


def republish(root, obs: Mapping, pole: Mapping, added: list) -> str | None:
    """Every owner plan of ``pole``: the pole out, ``added`` in; a refusal reason, or None when done.

    Supported owners: board blocks (``pcb-board.json``) and mining banks (``mining-outposts.json``:
    plan, pending extension, retained units). A plan of the pole in any other run file is refused
    before anything is written (its owner would build the pole back).
    """
    root = Path(root)
    position = dict(pole["position"])
    for path in sorted(root.glob("*.json")):
        if path.name in NOT_PLANS or path.name in (BOARD_FILE, OUTPOSTS_FILE):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _mentions(value, position):
            return "pole_relocation_owner_not_republishable:" + path.name
    board_path, outposts_path = root / BOARD_FILE, root / OUTPOSTS_FILE
    board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
    banks = json.loads(outposts_path.read_text(encoding="utf-8")) if outposts_path.exists() else {}
    for doc in (board, banks):
        if doc and doc.get("world_id") != obs.get("world_id"):
            return "pole_relocation_owner_world_changed"
    board_changed = banks_changed = False
    for block in (board.get("blocks") or {}).values():
        if isinstance(block, dict) and any(_is_pole(e, position) for e in block.get("entities", [])):
            block["entities"] = _swap(block["entities"], position, added)
            board_changed = True
    unit = pole.get("unit_number")
    for bank in banks.get("banks", []):
        for holder in (bank, bank.get("pending_extension")):
            plan = holder.get("plan") if isinstance(holder, dict) else None
            if isinstance(plan, dict) and any(_is_pole(e, position) for e in plan.get("entities", [])):
                plan["entities"] = _swap(plan["entities"], position, added)
                banks_changed = True
            if isinstance(holder, dict) and isinstance(holder.get("retained_units"), list):
                rows = [
                    r for r in holder["retained_units"] if not (_is_pole(r, position) and r.get("unit_number") == unit)
                ]
                if len(rows) != len(holder["retained_units"]):
                    holder["retained_units"] = rows
                    banks_changed = True
    if banks_changed:
        _atomic_json(outposts_path, banks)
    if board_changed:
        _atomic_json(board_path, board)
    return None


# ----- controller ----------------------------------------------------------------
def _blocked(reason: str, **evidence) -> dict:
    return {"status": "blocked", "reason": reason, "evidence": evidence}


class PoleRelocation:
    """One pole moved at a time, phase by phase (module doc)."""

    def __init__(self, game, builder, catalog, *, connect=None):
        from .power_grid import plan_connection

        self.game, self.builder, self.catalog = game, builder, catalog
        self.connect = connect or plan_connection
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE

    def _load(self, obs) -> dict:
        state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if state.get("world_id") != obs.get("world_id"):
            state = {"world_id": obs.get("world_id"), "active": None, "done": []}
        return state

    def _save(self, state) -> None:
        _atomic_json(self.path, state)

    def active(self, obs) -> bool:
        return bool(self._load(obs).get("active"))

    def network(self, obs, pole) -> dict:
        payload = json.dumps(json.dumps({"world_id": obs.get("world_id"), "pole": pole}, separators=(",", ":")))
        result = self.game.query(NETWORK_LUA % payload)
        return result if isinstance(result, dict) else {"ok": False, "reason": "pole_relocation_read_failed"}

    def step(self, obs, fault: Mapping | None, *, keep_out=()) -> dict | None:
        """The next action or status of the relocation; None when no relocation is (or remains) to do."""
        state = self._load(obs)
        active = state.get("active")
        if not active:
            if fault is None:
                return None
            if len(state.get("done", [])) >= MAX_RELOCATIONS:
                return _blocked("pole_relocation_limit_reached", relocations=len(state["done"]))
            pole = {"name": POLE, "position": dict(fault["position"]), "unit_number": fault.get("unit_number")}
            planned = self._plan(obs, pole, list(keep_out) + tile_keep_out(pole["position"]))
            if planned.get("status") == "blocked":
                return planned
            active = {"pole": pole, "chain": planned["chain"], "phase": "build"}
            state["active"] = active
            self._save(state)
        if active["phase"] == "build":
            if active["chain"]:
                plan = _plan(active["chain"], [], template="pole_relocation")
                result = self.builder.ensure_plan(obs, plan)
                if result.get("status") != "succeeded":
                    return result
            active["phase"] = "verify"
            self._save(state)
        if active["phase"] == "verify":
            check = self.network(obs, active["pole"])
            if not check.get("ok"):
                return self._finish_if_gone(obs, state, check)
            if check.get("cut") or check.get("orphans"):
                return _blocked(
                    "pole_relocation_replacement_not_connected", cut=check.get("cut"), orphans=check.get("orphans")
                )
            active["phase"] = "mine"
            self._save(state)
        return self._mine(obs, state)

    def _plan(self, obs, pole, keep_out) -> dict:
        check = self.network(obs, pole)
        if not check.get("ok"):
            return _blocked(check.get("reason") or "pole_relocation_read_failed")
        if check.get("orphans"):
            return _blocked("pole_relocation_would_unpower", orphans=check["orphans"][:4])
        reached = {_xy(p) for p in check.get("reached", [])}
        observed = {(e["name"], *_xy(e["position"])) for e in obs.get("entities", ()) if e.get("position")}
        chain = []
        for cut in check.get("cut", []):
            found = self.connect(
                self.game, self.builder, obs, dict(cut["position"]), [dict(pole)] + chain, keep_out=keep_out,
                sources=reached,
            )  # fmt: skip
            if not found.get("ok"):
                return _blocked("pole_relocation_no_replacement_connection", detail=found.get("reason"), pole=cut)
            chain += [e for e in found["entities"] if (e["name"], *_xy(e["position"])) not in observed]
        refused = republish(self.root, obs, pole, chain)
        if refused:
            return _blocked(refused)
        return {"status": "planned", "chain": chain}

    def _mine(self, obs, state) -> dict | None:
        from .obsolete import guarded_mine, room_action, slots_needed

        pole = state["active"]["pole"]
        live = next(
            (
                e
                for e in obs.get("entities", ())
                if e.get("name") == POLE
                and e.get("position") == pole["position"]
                and e.get("unit_number") == pole["unit_number"]
            ),
            None,
        )
        if live is not None:
            room = room_action(obs, slots_needed(self.catalog, [live]))
            if room is not None:
                return room
            action = guarded_mine(live, obs.get("world_id"), "relocate a power pole off a planned route tile")
            return action or _blocked("pole_relocation_pole_has_no_unit")
        return self._finish(state)

    def _finish_if_gone(self, obs, state, check) -> dict | None:
        """The verify read failed: the pole is gone (finished) or something changed (blocked)."""
        if check.get("reason") == "pole_relocation_pole_changed":
            return self._mine(obs, state)
        return _blocked(check.get("reason") or "pole_relocation_read_failed")

    def _finish(self, state) -> None:
        state.setdefault("done", []).append(state["active"])
        state["active"] = None
        self._save(state)
        return None
