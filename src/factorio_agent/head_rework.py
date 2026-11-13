"""Feed head rework: a built smelting district bank's shared feed head replaced by the split head.

Why (docs/v2/LAYOUT_PRINCIPLES.md "Belt lanes and bandwidth"): the shared head of
``smelting.paired_furnace_bank`` side-loads ore and coal onto one short belt and then splits
it, so each furnace row gets a quarter belt of ore (225/min, 12 stone furnaces at 18.75/min,
10 per row with the flow model's margin). New districts take ``feed_head.split_feed_head``
(task: ``smelting_site.district_head_choice``); districts built before keep the shared head
until this capability replaces it in place. Owner rule (2026-11-13, AGENTS.md): a built block
may be partly rebuilt by paid guarded removal; the plan is republished first, the replacement
stands before the removal, a critical consumer is never cut off without a replacement.

Rules:

1. Trigger (:func:`receipt`, observe receipt ``feed_head_rework``; :func:`feed_head_rework_nodes`):
   a district bank with the shared head whose built rows already need more than the head
   delivers (``head_supports`` < built per row), or whose growth was refused with
   ``feed_head_bandwidth_below_next_slot_demand`` (``SmeltingDistrict._grow``). Wait vs spend:
   ``spend = TRIP + SECONDS_PER_ENTITY x (added + removed) + cut-over stop`` (the stop is the
   bank's lost output, counted as seconds of a stopped bank) against the alternative that
   delivers the same rows, a second bank: ``TRIP + SECONDS_PER_ENTITY x (bank entities for the
   rows the head cannot feed + SECOND_BANK_ROUTE_ENTITIES)``. Proposed once per bank when
   ``spend + MIN_SAVING_SECONDS <= second bank`` and splitters and undergrounds are enabled.
2. Plan (:func:`district_rework`, pure): the district's canonical shared and split heads
   (``smelting_site.district_candidate``) are compared in the head columns of this bank and
   its half of the coal band; the difference is ``remove`` (must equal the saved plan's
   entities, else blocked) and ``add`` (must not meet other district hardware). The bank and
   district plan with the split head are republished (state, then board block
   ``smelting_district``) before anything is built; growth waits while a rework runs.
3. Build order (canonical tiles of the iron bank, the copper bank mirrored): first every
   added entity that does not stand where an old one stands (old head keeps running);
   then the cut-over groups in ``CUTOVER`` order, each a guarded ``mine`` of the old
   entities on its tiles followed by building their replacements (every item carried and
   inventory room for every mined entity before the first mine, every built furnace of the
   bank holding ore so the rows keep smelting through the short stop): the coal branch
   switches to the new coal port, the old coal side-load is removed (coal never reaches a
   lane that will carry ore), the raw splitter and the ore branch are switched, the old
   splitter and the old lower feed column are replaced, the upper coal riser is turned.
   Then the remaining old belts are mined; every mined item returns to the inventory and is
   used again by later builds.
4. Proof: every new head entity stands as planned, no removed one stands, and the bank's
   input rows carry ore on one lane and coal on the other (``smelting.native_feed_lanes``).

Steel and brick blocks (``planned_furnaces``) are not reworked yet: their coal port moves
to the head's inner edge, so their coal route has to be planned again; the growth guard
(``planned_furnaces.growth_evidence``) still stops them where the head runs short.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .core.deterministic_builder import FactoryBuilder, _plan
from .core.deterministic_state import _atomic_json

CAPABILITY = "feed_head_rework"
STATE_FILE = "feed-head-rework.json"
NODE_PREFIX = "auto_feed_head_rework_"
GROWTH_REFUSED = "feed_head_bandwidth_below_next_slot_demand"
RESOURCES = ("iron-ore", "copper-ore")
HEAD_NAMES = frozenset({"transport-belt", "splitter", "underground-belt"})
HEAD_COLUMNS = 8
# Cut-over groups, canonical tiles of the iron bank relative to the district corner (rule 3).
CUTOVER = (
    ((3, 15),),  # coal branch onto the new coal port
    ((3, 2), (3, 3)),  # old coal side-load: no coal reaches the stub that will carry ore
    ((1, 0),),  # raw splitter
    ((4, 0),),  # ore branch along row 0 to the upper row
    ((7, 4), (7, 3), (5, 1)),  # old lower feed column and the old splitter -> lower ore branch
    ((7, 2),),  # upper coal riser
)
# Wait vs spend (rule 1). Estimates, same actor-time model as throughput_growth.
SECOND_BANK_ROUTE_ENTITIES = 60  # ore and coal feed routes of a second bank (estimate)
MIN_SAVING_SECONDS = 60.0
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
_CAPACITY_CACHE: dict = {}


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _key(e) -> tuple:
    p = e["position"]
    return (e["name"], p["x"], p["y"], e.get("direction", 0), e.get("belt_to_ground_type"))


def _tiles(entities) -> set:
    return set(GEOMETRY._occupied_by_plan(list(entities))) if entities else set()


# ----- flow model ---------------------------------------------------------------------------
def district_shortfalls(bank: dict, per_row: int) -> list:
    """belt_flow shortfalls of ``bank`` with ``per_row`` furnaces built per row (district rates)."""
    from .bandwidth_audit import COAL_JOULES, FURNACE_WATTS, PLATE_SECONDS, SHORTFALL, furnace_bank
    from .smelting import grown_bank

    grown = grown_bank(bank, max(1, min(per_row, bank["capacity_slots"])))
    ore = 60 / PLATE_SECONDS
    audit = furnace_bank(
        grown["entities"],
        grown["ports"],
        raw=bank["resource"],
        raw_per_furnace=ore,
        coal_per_furnace=FURNACE_WATTS["stone-furnace"] * 60 / COAL_JOULES,
        product=bank.get("product"),
        product_per_furnace=ore,
    )
    return [f for f in audit["findings"] if f["kind"] in SHORTFALL]


def head_supports(bank: dict) -> int:
    """The most furnaces per row the bank's head feeds without a flow-model shortfall (cached)."""
    key = json.dumps([bank.get("ports"), sorted(map(_key, bank.get("entities") or []), key=str)], sort_keys=True)
    if key not in _CAPACITY_CACHE:
        best = 0
        for per_row in range(1, int(bank["capacity_slots"]) + 1):
            if district_shortfalls(bank, per_row):
                break
            best = per_row
        if len(_CAPACITY_CACHE) > 16:
            _CAPACITY_CACHE.clear()
        _CAPACITY_CACHE[key] = best
    return _CAPACITY_CACHE[key]


# ----- rule 2: the plan ---------------------------------------------------------------------
def _fail(reason, **extra) -> dict:
    return {"ok": False, "reason": reason, **extra}


def _canonical_corner(plan: dict):
    from .site.transform import Transform
    from .smelting_site import DISTRICT_SIZE

    t = Transform.from_dict(plan.get("site_transform"))
    area = plan.get("reserved_area")
    if not area:
        return None
    x0, y0, x1, y1 = t.inverse().rect(area[0][0], area[0][1], area[1][0], area[1][1])
    if (x1 - x0, y1 - y0) != DISTRICT_SIZE or not float(x0).is_integer() or not float(y0).is_integer():
        return None
    return t, int(x0), int(y0)


def _region(entities, x0, y0, index) -> list:
    """Head-column belts of bank ``index`` (0 iron rows 0-15, 1 copper rows 16-31), canonical."""
    out = []
    for e in entities:
        if e["name"] not in HEAD_NAMES:
            continue
        rx, ry = e["position"]["x"] - x0, e["position"]["y"] - y0
        if rx < HEAD_COLUMNS and (ry < 16) == (index == 0):
            out.append(e)
    return out


def _group_tiles(index, x0, y0) -> list:
    """``CUTOVER`` tiles of bank ``index`` as canonical tile centres (copper mirrored)."""
    rows = []
    for group in CUTOVER:
        rows.append({(x0 + x + 0.5, y0 + (y if index == 0 else 31 - y) + 0.5) for x, y in group})
    return rows


def district_rework(state: dict, resource: str) -> dict:
    """The republished bank and district plan plus the ordered build/cut-over/clear lists (rule 2, 3)."""
    from .site.transform import apply
    from .smelting_site import district_candidate

    plan, banks = state.get("plan") or {}, state.get("banks") or []
    index = next((i for i, b in enumerate(banks) if b.get("resource") == resource), None)
    if index is None or index > 1:
        return _fail("feed_head_rework_bank_missing", resource=resource)
    bank = banks[index]
    if bank.get("feed_head", "shared") != "shared":
        return _fail("feed_head_already_split", resource=resource)
    frame = _canonical_corner(plan)
    if frame is None:
        return _fail("feed_head_rework_district_frame_unknown")
    t, x0, y0 = frame
    counts = {"iron-ore": 2, "copper-ore": 2}
    shared = district_candidate(x0 + 8, y0 + 8, counts, head="shared")
    split = district_candidate(x0 + 8, y0 + 8, counts, head="split")
    old, new = _region(shared["entities"], x0, y0, index), _region(split["entities"], x0, y0, index)
    old_keys, new_keys = {_key(e) for e in old}, {_key(e) for e in new}
    remove = [e for e in old if _key(e) not in new_keys]
    add = [e for e in new if _key(e) not in old_keys]
    removed_tiles = _tiles(remove)
    groups, grouped_adds, grouped_removes = [], set(), set()
    for tiles in _group_tiles(index, x0, y0):
        mines = [e for e in remove if _tiles([e]) & tiles and _key(e) not in grouped_removes]
        mined = _tiles(mines)
        builds = [e for e in add if _tiles([e]) & mined and _key(e) not in grouped_adds]
        grouped_removes |= {_key(e) for e in mines}
        grouped_adds |= {_key(e) for e in builds}
        groups.append({"mine": mines, "build": builds})
    first = [e for e in add if not _tiles([e]) & removed_tiles]
    if {_key(e) for e in first} | grouped_adds != new_keys - old_keys:
        return _fail("feed_head_rework_cutover_incomplete")
    clear = [e for e in remove if _key(e) not in grouped_removes]

    def to_map(value):
        return apply(t, value) if not t.identity else copy.deepcopy(value)

    first, clear = to_map(first), to_map(clear)
    groups = [{"mine": to_map(g["mine"]), "build": to_map(g["build"])} for g in groups]
    remove_map, add_map = to_map(remove), to_map(add)
    planned = {_key(e): e for e in plan.get("entities") or []}
    missing = [e for e in remove_map if _key(e) not in planned]
    if missing:
        return _fail("feed_head_rework_plan_differs_from_shared_head", missing=missing[:4])
    removed_keys = {_key(e) for e in remove_map}
    rest = [e for e in plan["entities"] if _key(e) not in removed_keys]
    meets = _tiles(add_map) & _tiles(rest)
    if meets:
        return _fail("feed_head_rework_head_meets_district_hardware", tiles=sorted(meets)[:6])
    # The new bank: built slots and furnaces as saved, the head and coal port of the split head.
    split_bank = to_map(split["banks"][index])
    split_keys = {_key(e) for e in split_bank["entities"]}
    new_bank = copy.deepcopy(bank)
    new_bank["entities"] = [e for e in bank["entities"] if _key(e) not in removed_keys] + [
        e for e in add_map if _key(e) in split_keys
    ]
    new_bank["ports"] = [bank["ports"][0], split_bank["ports"][1], *bank["ports"][2:]]
    new_bank["underground_pairs"] = split_bank["underground_pairs"]
    new_bank["feed_head"] = "split"
    new_bank["required_items"] = _plan(new_bank["entities"], [])["required_items"]
    extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    extra["underground_pairs"] = list(plan.get("underground_pairs") or []) + list(split_bank["underground_pairs"])
    new_plan = _plan(rest + add_map, plan["ports"], **extra)
    new_banks = [new_bank if i == index else b for i, b in enumerate(banks)]
    return {
        "ok": True,
        "resource": resource,
        "bank": new_bank,
        "banks": new_banks,
        "plan": new_plan,
        "remove": remove_map,
        "add": add_map,
        "first": first,
        "groups": groups,
        "clear": clear,
    }


# ----- rule 1: wait vs spend ----------------------------------------------------------------
def decide(bank: dict, rework: dict, *, refused: bool) -> dict:
    """Whether the rework pays now (module rule 1), with the numbers."""
    from .smelting import paired_furnace_bank
    from .throughput_growth import SECONDS_PER_ENTITY, TRIP_SECONDS

    supports = head_supports(bank)
    built = int(bank.get("per_side_count") or 0)
    slots = int(bank.get("capacity_slots") or 0)
    short = built > supports
    cutover = sum(len(g["mine"]) + len(g["build"]) for g in rework["groups"])
    spend = (
        TRIP_SECONDS + SECONDS_PER_ENTITY * (len(rework["add"]) + len(rework["remove"])) + SECONDS_PER_ENTITY * cutover
    )
    unfed = max(1, slots - supports)
    second = TRIP_SECONDS + SECONDS_PER_ENTITY * (
        len(paired_furnace_bank(0, 0, min(unfed, 12), bank["resource"])["entities"]) + SECOND_BANK_ROUTE_ENTITIES
    )
    if not (refused or short):
        reason = "head_feeds_every_built_and_next_row"
    elif spend + MIN_SAVING_SECONDS > second:
        reason = "second_bank_cheaper_than_the_rework"
    else:
        reason = "rework_pays"
    return {
        "head_supports_per_row": supports,
        "built_per_row": built,
        "capacity_slots": slots,
        "growth_refused": refused,
        "built_rows_short": short,
        "spend_seconds": round(spend, 1),
        "cutover_stop_seconds": round(SECONDS_PER_ENTITY * cutover, 1),
        "second_bank_seconds": round(second, 1),
        "added": len(rework["add"]),
        "removed": len(rework["remove"]),
        "ready": reason == "rework_pays",
        "reason": reason,
    }


def _refused(jobs, resource) -> bool:
    return any(
        j.get("capability") == "smelting_capacity"
        and j.get("status") == "blocked"
        and j.get("reason") == GROWTH_REFUSED
        and ((j.get("request") or {}).get("parameters") or j.get("parameters") or {}).get("resource") == resource
        for j in jobs
    )


def receipt(root, obs, jobs) -> dict | None:
    """Observe receipt ``feed_head_rework``: per district bank, the head, its row capacity and the decision."""
    root = Path(root)
    state = _read(root / "smelting-district.json")
    if not state or state.get("world_id") != obs.get("world_id"):
        return None
    reworks = (
        (_read(root / STATE_FILE).get("reworks") or {})
        if _read(root / STATE_FILE).get("world_id") == obs.get("world_id")
        else {}
    )
    rows = {}
    for bank in state.get("banks") or []:
        resource = bank.get("resource")
        if resource not in RESOURCES or "capacity_slots" not in bank:
            continue
        head = bank.get("feed_head", "shared")
        row = {"head": head, "phase": (reworks.get(resource) or {}).get("phase")}
        if head == "shared":
            rework = district_rework(state, resource)
            if rework.get("ok"):
                row.update(decide(bank, rework, refused=_refused(jobs, resource)))
            else:
                row.update(ready=False, reason=rework.get("reason"))
        else:
            row.update(ready=False, reason="split_head", head_supports_per_row=head_supports(bank))
        rows[resource] = row
    return {"banks": rows} if rows else None


def feed_head_rework_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One node per district bank, once, when the receipt says the rework pays (rule 1)."""
    if CAPABILITY not in capabilities:
        return []
    rows = (observation.get("feed_head_rework") or {}).get("banks")
    enabled = set(observation.get("enabled_recipes") or ())
    if not isinstance(rows, dict) or not {"splitter", "underground-belt"} <= enabled:
        return []
    jobs = list(jobs)
    if "smelting_district" not in {j.get("capability") for j in jobs if j.get("status") == "succeeded"}:
        return []
    from .throughput_growth import CRITICAL_PRIORITY

    nodes = []
    for resource in RESOURCES:
        row = rows.get(resource)
        if not isinstance(row, dict) or not row.get("ready"):
            continue
        if any(
            r.get("capability") == CAPABILITY and (r.get("parameters") or {}).get("resource") == resource
            for r in jobs + list(queue_nodes)
        ):
            continue
        nodes.append(
            {
                "id": NODE_PREFIX + resource,
                "capability": CAPABILITY,
                "parameters": {"block": "smelting_district", "resource": resource},
                "priority": CRITICAL_PRIORITY,
                "depends_on": [],
                "reason": f"{resource} bank head feeds {row.get('head_supports_per_row')} furnaces per row "
                f"({'growth refused' if row.get('growth_refused') else 'built rows short'}): rework "
                f"{row.get('spend_seconds')}s < second bank {row.get('second_bank_seconds')}s"[:300],
            }
        )
    return nodes


def validate_parameters(parameters) -> None:
    if not isinstance(parameters, dict) or set(parameters) != {"block", "resource"}:
        raise ValueError("feed_head_rework takes exactly block and resource")
    if parameters["block"] != "smelting_district":
        raise ValueError("feed_head_rework block must be smelting_district (steel/brick blocks: not yet)")
    if parameters["resource"] not in RESOURCES:
        raise ValueError("feed_head_rework resource must be one of " + ", ".join(RESOURCES))


# ----- controller ---------------------------------------------------------------------------
class FeedHeadRework:
    """Replace one district bank's shared feed head in place (module rules 2-4)."""

    def __init__(self, game, builder, catalog, *, smelting):
        self.game, self.builder, self.catalog, self.smelting = game, builder, catalog, smelting
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE
        self.state = _read(self.path)

    def _save(self):
        _atomic_json(self.path, self.state)

    def _record(self, obs, resource) -> dict | None:
        if self.state.get("world_id") != obs.get("world_id"):
            return None
        return (self.state.get("reworks") or {}).get(resource)

    def _live(self, obs) -> dict:
        return {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])}

    def _stands(self, live, e) -> bool:
        found = live.get((e["name"], e["position"]["x"], e["position"]["y"]))
        return (
            found is not None
            and found.get("direction", 0) == e.get("direction", 0)
            and (not e.get("belt_to_ground_type") or found.get("belt_to_ground_type") == e["belt_to_ground_type"])
        )

    def _old(self, live, e):
        """The live old entity at ``e`` (same name and position, the old direction), else None."""
        found = live.get((e["name"], e["position"]["x"], e["position"]["y"]))
        return found if found is not None and found.get("direction", 0) == e.get("direction", 0) else None

    def step(self, obs, parameters) -> dict:
        resource = parameters["resource"]
        district = self.smelting.state
        if not district or district.get("world_id") != obs.get("world_id"):
            return {"status": "blocked", "reason": "current_world_smelting_district_required"}
        record = self._record(obs, resource)
        if record is None:
            started = self._start(obs, resource)
            if started is not None:
                return started
            record = self._record(obs, resource)
        phase = record["phase"]
        if phase == "build":
            return self._build(obs, record)
        if phase == "cutover":
            return self._cutover(obs, record)
        if phase == "clear":
            return self._clear(obs, record)
        return self._verify(obs, record)

    def _start(self, obs, resource) -> dict | None:
        from .early_start import ledger_jobs
        from .obsolete import active_jobs
        from .planning import preflight

        enabled = set(obs.get("enabled_recipes") or ())
        if not {"splitter", "underground-belt"} <= enabled:
            return {"status": "blocked", "reason": "feed_head_rework_requires_logistics"}
        jobs = ledger_jobs(self.root, obs["world_id"])
        busy = active_jobs(jobs, capabilities=("smelting_district", "smelting_capacity", "furnace_upgrade"))
        if busy:
            return {"status": "waiting", "reason": "feed_head_rework_waits_for_district_jobs", "jobs": busy[:4]}
        district = self.smelting.state
        bank = next((b for b in district.get("banks") or [] if b.get("resource") == resource), None)
        if bank is not None and bank.get("feed_head") == "split":
            return {"status": "succeeded", "reason": "district bank already has the split feed head"}
        rework = district_rework(district, resource)
        if not rework.get("ok"):
            return {"status": "blocked", **rework}
        if rework["first"]:
            check = preflight(self.game, self.builder, obs, rework["first"])
            if not check.get("ok"):
                return {
                    "status": "blocked",
                    "reason": "feed_head_rework_preflight_failed",
                    "detail": {k: check.get(k) for k in ("reason", "entity", "position") if check.get(k) is not None},
                }
        live = self._live(obs)
        units = {}
        for e in rework["remove"]:
            found = self._old(live, e)
            if found is not None and type(found.get("unit_number")) is int:
                units[json.dumps(_key(e))] = found["unit_number"]
        # Plan republished first (rule 2): district state, then its board block.
        board_path = self.root / "pcb-board.json"
        board = _read(board_path)
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "board_identity_or_catalog_changed"}
        district.update(plan=rework["plan"], banks=rework["banks"])
        district.setdefault("head_rework", {})[resource] = "in_progress"
        _atomic_json(self.smelting.path, district)
        board.setdefault("blocks", {})["smelting_district"] = rework["plan"]
        _atomic_json(board_path, board)
        self.state = {"world_id": obs["world_id"], "reworks": dict(self._reworks(obs))}
        self.state["reworks"][resource] = {
            "phase": "build",
            "resource": resource,
            "first": rework["first"],
            "groups": rework["groups"],
            "clear": rework["clear"],
            "remove": rework["remove"],
            "add": rework["add"],
            "units": units,
            "published_tick": obs.get("tick"),
        }
        self._save()
        return None

    def _reworks(self, obs) -> dict:
        return (self.state.get("reworks") or {}) if self.state.get("world_id") == obs.get("world_id") else {}

    def _set_phase(self, record, phase, obs):
        record["phase"] = phase
        record[phase + "_tick"] = obs.get("tick")
        self._save()

    def _build(self, obs, record) -> dict:
        if record["first"]:
            result = self.builder.ensure_plan(obs, _plan(record["first"], []))
            if result.get("status") != "succeeded":
                return result
        self._set_phase(record, "cutover", obs)
        return self._cutover(obs, record)

    def _mine(self, obs, record, e, live) -> dict:
        from .obsolete import guarded_mine

        found = self._old(live, e)
        unit = record["units"].get(json.dumps(_key(e)))
        if unit is not None and found.get("unit_number") != unit:
            return {"status": "blocked", "reason": "feed_head_rework_old_entity_identity_changed", "entity": e}
        action = guarded_mine(found, obs["world_id"], "rework the district feed head: replace one old head belt")
        return action or {"status": "blocked", "reason": "feed_head_rework_old_entity_has_no_unit", "entity": e}

    def _cutover(self, obs, record) -> dict:
        from .obsolete import room_action, slots_needed

        live = self._live(obs)
        if not record.get("cutover_started"):
            # Everything the cut-over builds is carried first, so the rows stop only between a
            # mine and the build of its replacement; room for everything mined.
            need = {}
            for group in record["groups"]:
                for e in group["build"]:
                    if not self._stands(live, e):
                        need[e["name"]] = need.get(e["name"], 0) + 1
            inventory = obs.get("inventory") or {}
            for name, count in sorted(need.items()):
                if int(inventory.get(name, 0)) < count:
                    result = self.builder.bootstrap.ensure_item(obs, name, count)
                    if result.get("type") or result.get("status") != "succeeded":
                        return result
            mined = [self._old(live, e) for g in record["groups"] for e in g["mine"]]
            room = room_action(obs, slots_needed(self.catalog, [e for e in mined if e]))
            if room is not None:
                return room
            if not self._rows_hold_ore(record, live):
                return {"status": "waiting", "reason": "feed_head_rework_waits_for_ore_in_every_built_furnace"}
            record["cutover_started"] = obs.get("tick")
            self._save()
        for group in record["groups"]:
            for e in group["mine"]:
                if self._old(live, e) is not None:
                    return self._mine(obs, record, e, live)
            if group["build"] and not all(self._stands(live, e) for e in group["build"]):
                result = self.builder.ensure_plan(obs, _plan(group["build"], []))
                if result.get("status") != "succeeded":
                    return result
        self._set_phase(record, "clear", obs)
        return self._clear(obs, record)

    def _rows_hold_ore(self, record, live) -> bool:
        resource = record["resource"]
        bank = next(b for b in self.smelting.state["banks"] if b.get("resource") == resource)
        furnaces = [e for row in (bank.get("producer_rows") or {}).values() for e in row]
        for e in furnaces:
            found = live.get((e["name"], e["position"]["x"], e["position"]["y"]))
            if found is None:
                continue
            if "inventory" in found and int((found.get("inventory") or {}).get(resource, 0)) <= 0:
                return False
        return True

    def _clear(self, obs, record) -> dict:
        from .obsolete import room_action, slots_needed

        live = self._live(obs)
        for e in record["clear"]:
            found = self._old(live, e)
            if found is None:
                continue
            room = room_action(obs, slots_needed(self.catalog, [found]))
            if room is not None:
                return room
            return self._mine(obs, record, e, live)
        self._set_phase(record, "verify", obs)
        return self._verify(obs, record)

    def _verify(self, obs, record) -> dict:
        resource = record["resource"]
        proof = self.proof(obs, {"resource": resource})
        if proof is None:
            return {"status": "waiting", "reason": "waiting for the reworked head to carry ore and coal one per lane"}
        if record.get("phase") != "done":
            record["phase"] = "done"
            record["done_tick"] = obs.get("tick")
            self._save()
            district = self.smelting.state
            (district.get("head_rework") or {}).pop(resource, None)
            if not district.get("head_rework"):
                district.pop("head_rework", None)
            _atomic_json(self.smelting.path, district)
        return {"status": "succeeded", "reason": "district bank feeds both rows a full lane of ore", "evidence": proof}

    def proof(self, obs, parameters=None) -> dict | None:
        from .smelting import feed_lanes_ok, native_feed_lanes

        resource = (parameters or {}).get("resource")
        record = self._record(obs, resource)
        if record is None or record.get("phase") not in ("verify", "done"):
            return None
        live = self._live(obs)
        built = record["first"] + [e for g in record["groups"] for e in g["build"]]
        if not all(self._stands(live, e) for e in built):
            return None
        added = {_key(e) for e in record["add"]}
        if any(self._old(live, e) is not None for e in record["remove"] if _key(e) not in added):
            return None
        bank = next((b for b in self.smelting.state.get("banks") or [] if b.get("resource") == resource), None)
        if bank is None:
            return None
        lanes = native_feed_lanes(self.game, [bank])
        if not feed_lanes_ok(lanes):
            return None
        return {
            "tick": obs.get("tick"),
            "resource": resource,
            "head": "split",
            "added": len(record["add"]),
            "removed": len(record["remove"]),
            "native_feed_lanes": lanes["banks"],
            "head_supports_per_row": head_supports(bank),
        }


def bank_growth_check(bank: dict, per_row: int) -> dict | None:
    """``SmeltingDistrict._grow`` guard: None, or the refusal when the head cannot feed ``per_row``."""
    if per_row <= int(bank.get("per_side_count") or 0):
        return None
    short = district_shortfalls(bank, per_row)
    if not short:
        return None
    return {
        "status": "blocked",
        "reason": GROWTH_REFUSED,
        "evidence": {
            "resource": bank.get("resource"),
            "feed_head": bank.get("feed_head", "shared"),
            "wanted_per_row": per_row,
            "head_supports_per_row": head_supports(bank),
            "findings": [{k: f.get(k) for k in ("kind", "item", "tile", "delivered_per_minute")} for f in short[:4]],
            "next": "feed_head_rework (head_rework.py) replaces the head; growth retries after it succeeds",
        },
    }
