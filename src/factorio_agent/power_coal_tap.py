"""Power's boiler coal as an output-priority tap on the coal patch trunk; the starter feed retires.

The mine-trunk rule (docs/v2/LAYOUT_PRINCIPLES.md "Mine output trunk") says a
patch has one mining block and every consumer takes from its trunk. The
starter boiler feed (``power_coal_feed``: its own drill and belt, made
electric by ``electric_coal``) was the last private group on the coal patch.

Rules:

1. Trigger (``tap_receipt`` in the ``mine_trunks`` observe receipt,
   :func:`power_coal_tap_nodes`): the boiler is belt-fed, ``electric_coal``
   succeeded, the coal patch has a proven bank (``mine_trunk.patch_bank``)
   and its trunk is observed. Before that the starter feed stays as it is
   (hand-fed, then its own drill).
2. Plan (``PowerCoalTap.prepare``, no ``act``): a ``serve="first"`` splitter
   tap on the trunk (the branch has output priority: the boilers are served
   before any downstream consumer) and one belt from the tap port that
   side-loads onto a straight belt of the existing feed (the *join*). The
   feed's belts from the join to the boiler are kept; everything upstream
   of the join (the old drill, its fuel arm, its belts) is the retired
   part. Poles are kept (they may carry the grid on).
3. Build: tap cut-over (``mine_trunk.cutover``, the trunk belt's owner
   republished first), then the branch. The old feed keeps running.
4. Proof of the replacement: trunk coal on the branch's last belt, boiler
   fuel, a powered bank drill and both engines connected, held for
   ``VERIFY_TICKS``. Only then the builder's ``coal_plan`` is replaced by
   the adopted plan (branch + kept belts + poles; its ``drill`` is a bank
   drill, so ``power_evidence`` and repairs keep working) and the old one
   is kept in this state file.
5. Retire: one guarded ``mine`` per retired entity (unit number + world),
   each only while the boiler holds fuel; never rebuilt (the adopted plan
   no longer lists them).
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from . import mine_trunk
from .core.deterministic_builder import BELT_FED, _plan, coal_feed_mode, power_flow_ready
from .core.deterministic_state import _atomic_json
from .transport import xy

CAPABILITY = "power_coal_tap"
STATE_FILE = "power-coal-tap.json"
TEMPLATE = "power_trunk_tap"
TAP_NAME = "power:coal"
BOARD_BLOCK = "power_coal_tap"
FEED_BLOCK = "power_coal_feed"  # power_coal_feed.BLOCK
VERIFY_TICKS = 1800  # 30 game seconds, as every power proof
JOIN_CANDIDATES = 4  # joins tried per tap (nearest the tap port first)
ROUTE_ATTEMPTS = 6  # native routes per prepare
NODE_PRIORITY = -60  # below plate and trunk growth: a replacement, not new capacity
VECTORS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
KEPT_NAMES = frozenset({"small-electric-pole", "medium-electric-pole"})


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _key(e) -> tuple:
    return (e["name"], *xy(e))


def _find(obs, spec):
    p = spec["position"]
    return next(
        (
            e
            for e in obs.get("entities", [])
            if e.get("name") == spec["name"]
            and abs(e["position"]["x"] - p["x"]) < 0.2
            and abs(e["position"]["y"] - p["y"]) < 0.2
        ),
        None,
    )


# ----- pure geometry -------------------------------------------------------------------
def feed_trace(coal_plan: dict, power_plan: dict) -> dict:
    """The saved feed's belt path from its source port to the boiler's coal input."""
    from .bus_factory import trace_corridor

    destination = next(
        (p for p in power_plan.get("ports", []) if p.get("item") == "coal" and p.get("direction") == "input"), None
    )
    if destination is None or not coal_plan.get("ports"):
        return {"ok": False, "reason": "power_coal_tap_feed_ports_unknown"}
    # An in-line splitter of the feed (the wood burner feed's, wood_burner_feed.py) replaced a
    # feed belt on one of its two tiles; the path passes it.
    passthrough = {}
    for e in coal_plan.get("entities", []):
        if e["name"] == "splitter" and e.get("direction") in VECTORS:
            vx, vy = VECTORS[e["direction"]]
            x, y = xy(e)
            for s in (0.5, -0.5):
                passthrough[(x - vy * s, y + vx * s)] = e
    traced = trace_corridor(coal_plan, coal_plan["ports"][0], destination, passthrough=passthrough)
    if not traced.get("ok"):
        return {"ok": False, "reason": "power_coal_tap_feed_trace_failed", "trace": traced}
    return {"ok": True, "entities": traced["entities"], "destination": destination}


def join_rank(tap, joins) -> tuple:
    """Sort key of a tap: (faces away from its nearest join, tiles to that join); joins absent: (0, 0)."""
    if not joins:
        return (0, 0)
    tx, ty = xy(tap["port"])
    qx, qy = min((xy(j["port"]) for j in joins), key=lambda q: abs(q[0] - tx) + abs(q[1] - ty))
    vx, vy = VECTORS.get(tap["port"].get("facing"), (0, 0))
    away = (qx - tx) * vx + (qy - ty) * vy < 0
    return (int(away), abs(qx - tx) + abs(qy - ty))


def join_candidates(rows, occupied) -> list[dict]:
    """Straight feed belts a branch may side-load onto: ``{"index", "target", "port"}``.

    A join is a belt with the same-direction belt right behind and ahead of it
    (so a side-load fills a lane of a straight run, never a curve); the branch
    ends on a free tile beside it, facing it. ``occupied`` holds the tiles of
    every planned or observed entity.
    """
    found = []
    # Joins lie upstream of any in-line splitter (the wood burner feed's), which is then kept.
    limit = min((i for i, e in enumerate(rows) if e["name"] == "splitter"), default=len(rows) - 1)
    for index in range(1, min(limit, len(rows) - 1)):
        before, current, after = rows[index - 1 : index + 2]
        direction = current.get("direction", 0)
        if direction not in VECTORS or any(
            q["name"] != "transport-belt" or q.get("direction", 0) != direction for q in (before, current, after)
        ):
            continue
        vx, vy = VECTORS[direction]
        x, y = xy(current)
        if xy(before) != (x - vx, y - vy) or xy(after) != (x + vx, y + vy):
            continue
        for sx, sy in ((-vy, vx), (vy, -vx)):
            side = (x + sx, y + sy)
            if side in occupied:
                continue
            facing = next(d for d, v in VECTORS.items() if v == (-sx, -sy))
            port = {
                "kind": "item",
                "item": "coal",
                "direction": "input",
                "position": {"x": side[0], "y": side[1]},
                "facing": facing,
            }
            found.append({"index": index, "target": deepcopy(current), "port": port})
    return found


def adopted_plan(old: dict, branch: dict, kept, *, bank_drill: dict, tap: dict, world_id: str) -> dict:
    """The builder's new ``coal_plan``: branch + kept feed belts + old poles, measured on a bank drill."""
    kept_tiles = {xy(e) for e in kept}
    poles = [deepcopy(e) for e in old.get("entities", []) if e["name"] in KEPT_NAMES]
    entities = list(branch["entities"]) + [deepcopy(e) for e in kept] + poles
    pairs = list(branch.get("underground_pairs", [])) + [
        deepcopy(p) for p in old.get("underground_pairs", []) if tuple(p["input"]) in kept_tiles
    ]
    return _plan(
        entities,
        [deepcopy(tap["port"])],
        drill=deepcopy(bank_drill),
        template=TEMPLATE,
        trunk=tap["trunk"],
        underground_pairs=pairs,
        clear_obstacles=[],
        clearance_world_id=world_id,
        replaced_feed={"template": old.get("template"), "drill": deepcopy(old.get("drill"))},
    )


def retired_entities(old: dict, keep) -> list[dict]:
    """Old feed entities the adopted plan drops (drills first, then arms, then belts in plan order)."""
    keep = {_key(e) for e in keep}
    rows = [deepcopy(e) for e in old.get("entities", []) if _key(e) not in keep and e["name"] not in KEPT_NAMES]
    order = {"burner-mining-drill": 0, "electric-mining-drill": 0}
    return sorted(rows, key=lambda e: order.get(e["name"], 2 if "belt" in e["name"] else 1))


# ----- receipt and planner rule --------------------------------------------------------
def tap_receipt(root, obs) -> dict | None:
    """Whether the boiler feed can move onto the coal trunk (``mine_trunks.power_tap``), or None."""
    root = Path(root)
    world = obs.get("world_id")
    builder = _read(root / "factory-builder.json")
    plan = builder.get("coal_plan")
    if builder.get("world_id") != world or not isinstance(plan, dict) or coal_feed_mode(builder) != BELT_FED:
        return None
    if plan.get("template") == TEMPLATE:
        return {"ready": False, "adopted": True, "trunk": plan.get("trunk")}
    mining = _read(root / "mining-outposts.json")
    if mining.get("world_id") != world:
        return {"ready": False, "reason": "no_coal_patch_bank"}
    bank = mine_trunk.patch_bank(mining.get("banks"), "coal", mine_trunk.patch_seed(obs, "coal"))
    if bank is None:
        return {"ready": False, "reason": "no_coal_patch_bank"}
    key = mine_trunk.trunk_key(bank)
    if not isinstance(obs.get("entities"), list) or not mine_trunk.trace_trunk(obs, bank["plan"]["ports"][0]):
        return {"ready": False, "reason": "coal_trunk_not_observed", "trunk": key}
    return {"ready": True, "trunk": key}


def power_coal_tap_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One ``power_coal_tap`` node, once, when the receipt says the feed can move onto the trunk."""
    if CAPABILITY not in capabilities:
        return []
    receipt = (observation.get("mine_trunks") or {}).get("power_tap")
    if not isinstance(receipt, dict) or not receipt.get("ready"):
        return []
    jobs = list(jobs)
    succeeded = {j.get("capability") for j in jobs if j.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded:
        return []
    if any(r.get("capability") == CAPABILITY for r in jobs + list(queue_nodes)):
        return []
    return [
        {
            "id": "auto_" + CAPABILITY,
            "capability": CAPABILITY,
            "parameters": {},
            "priority": NODE_PRIORITY,
            "depends_on": [],
            "reason": f"boiler coal moves onto the coal patch trunk {receipt.get('trunk')}; the starter feed retires"[
                :300
            ],
        }
    ]


# ----- controller ----------------------------------------------------------------------
class PowerCoalTap:
    def __init__(self, game, builder, catalog, *, outposts=None, smelting=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.outposts, self.smelting = outposts, smelting
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE
        self.state = _read(self.path)

    def _save(self):
        _atomic_json(self.path, self.state)

    def _mining(self):
        return _read(self.root / "mining-outposts.json")

    def prepare(self, obs) -> dict:
        from .board import blocks_without_bus, surface_conflict
        from .core.deterministic_builder import boiler_coal_per_minute
        from .planning import preflight
        from .transport import plan_connection

        builder = self.builder.state or {}
        power, coal = builder.get("power_plan"), builder.get("coal_plan")
        if builder.get("world_id") != obs["world_id"] or not power or not coal:
            return {"ok": False, "reason": "power_coal_tap_requires_built_power_and_feed"}
        if coal_feed_mode(builder) != BELT_FED:
            return {"ok": False, "reason": "power_coal_tap_requires_belt_fed_boiler"}
        if coal.get("template") == TEMPLATE:
            return {"ok": False, "reason": "power_coal_tap_already_adopted"}
        mining = self._mining()
        if mining.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "power_coal_tap_no_coal_patch_bank"}
        bank = mine_trunk.patch_bank(mining.get("banks"), "coal", mine_trunk.patch_seed(obs, "coal"))
        if bank is None:
            return {"ok": False, "reason": "power_coal_tap_no_coal_patch_bank"}
        trace = feed_trace(coal, power)
        if not trace.get("ok"):
            return trace
        rows = trace["entities"]
        drills = [e for e in bank["plan"]["entities"] if e["name"] == "electric-mining-drill"]
        if not drills:
            return {"ok": False, "reason": "power_coal_tap_bank_has_no_electric_drill"}
        board = _read(self.root / "pcb-board.json")
        blocks = blocks_without_bus(board) if board.get("world_id") == obs["world_id"] else {}
        reserved = list(power["entities"]) + list(coal["entities"])
        live = [e for e in obs.get("entities", []) if isinstance(e.get("position"), dict) and e.get("name")]
        occupied = self.builder._occupied_by_plan(reserved + live)
        joins = join_candidates(rows, occupied)
        if not joins:
            return {"ok": False, "reason": "power_coal_tap_feed_has_no_straight_join"}
        boilers = sum(e["name"] == "boiler" for e in power["entities"]) or 1
        demand = boiler_coal_per_minute(self.catalog) * boilers
        key = mine_trunk.trunk_key(bank)
        chosen, failures, attempts = {}, [], [0]

        def accept(tap):
            if chosen or attempts[0] >= ROUTE_ATTEMPTS:
                return False
            if surface_conflict(self.builder, obs, blocks, tap["entities"]) is not None:
                failures.append({"reason": "mine_trunk_tap_meets_planned_hardware", "along": tap["along"]})
                return False
            check = preflight(self.game, self.builder, obs, tap["entities"], ignored_units=tuple(tap["remove_units"]))
            if not check.get("ok"):
                failures.append({"reason": check.get("reason"), "along": tap["along"], "side": tap["side"]})
                return False
            tx, ty = xy(tap["port"])
            ranked = sorted(joins, key=lambda j: (abs(xy(j["port"])[0] - tx) + abs(xy(j["port"])[1] - ty), -j["index"]))
            for join in ranked[:JOIN_CANDIDATES]:
                if attempts[0] >= ROUTE_ATTEMPTS:
                    break
                attempts[0] += 1
                route = plan_connection(
                    self.game, self.builder, obs, tap["port"], join["port"], reserved + list(tap["entities"])
                )
                if route.get("ok"):
                    chosen.update(tap=tap, route=route, join=join)
                    return True
                failures.append({"reason": route.get("reason"), "stage": "route", "join": join["index"]})
            return False

        # Every feasible tap row first, then routed nearest a join and facing it first: the route
        # budget is spent where a branch is short (seed 3111291866: two south-facing taps used all
        # six routes; the north-facing ones beside the feed were never routed).
        taps = []
        planned = mine_trunk.plan_tap(
            bank,
            obs,
            owner="power",
            name=TAP_NAME,
            demand=demand,
            serve="first",
            stations=mine_trunk.stations(self.root, obs["world_id"], key, exclude=TAP_NAME),
            accept=lambda tap: taps.append(tap) and False,
        )
        for tap in sorted(taps, key=lambda t: join_rank(t, joins)):
            if accept(tap) or attempts[0] >= ROUTE_ATTEMPTS:
                break
        if not chosen:
            failure = planned if not planned.get("ok") else {"reason": "power_coal_tap_no_route"}
            return dict(failure, ok=False, failures=failures[-6:], route_attempts=attempts[0])
        tap, route, join = chosen["tap"], chosen["route"], chosen["join"]
        branch = _plan(
            route["entities"],
            [deepcopy(tap["port"]), deepcopy(join["port"])],
            underground_pairs=route.get("underground_pairs", []),
            clear_obstacles=route.get("clear_obstacles", []),
            clearance_world_id=obs["world_id"],
            layout_policy="pcb-power-coal-tap-v1",
        )
        kept = rows[join["index"] :]
        return {
            "ok": True,
            "world_id": obs["world_id"],
            "catalog_fingerprint": getattr(self.catalog, "fingerprint", None),
            "phase": "build",
            "trunk": key,
            "tap": tap,
            "branch": branch,
            "join": join,
            "kept": deepcopy(kept),
            "bank_drill": deepcopy(drills[0]),
            "demand_per_minute": round(demand, 3),
        }

    def _block(self):
        tap = self.state["tap"]
        plan = mine_trunk.tap_plan(tap, self.state["branch"]["entities"])
        area = mine_trunk.tap_area([e for e in tap["entities"] if e["name"] != "splitter"] or tap["entities"])
        return BOARD_BLOCK, dict(plan, reserved_area=area, owner="power", trunk=self.state["trunk"])

    def _refresh_owners(self, changed):
        if "smelting_district" in changed and self.smelting is not None:
            self.smelting.state = _read(self.root / "smelting-district.json")
        if any(owner != "smelting_district" for owner in changed) and self.outposts is not None:
            self.outposts.state = self._mining()

    def _boiler_fuelled(self, obs) -> bool:
        power = (self.builder.state or {}).get("power_plan") or {}
        boilers = [e for e in power.get("entities", []) if e["name"] == "boiler"]
        for spec in boilers:
            live = _find(obs, spec)
            held = sum(int(n or 0) for n in ((live or {}).get("inventory") or {}).values())  # coal or wood
            if live is None or (held <= 0 and float(live.get("remaining_burning_fuel", 0) or 0) <= 0):
                return False
        return bool(boilers)

    def step(self, obs, parameters=None):
        if self.state and self.state.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "power_coal_tap_world_changed"}
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", "reason": prepared["reason"], "evidence": prepared}
            self.state = prepared
            self._save()
        phase = self.state["phase"]
        if phase == "build":
            return self._build(obs)
        if phase == "verify":
            return self._verify(obs)
        if phase == "retire":
            return self._retire(obs)
        return self._done(obs)

    def _build(self, obs):
        bank = mine_trunk.find_bank(self._mining().get("banks"), self.state["trunk"])
        if bank is None or not bank.get("ready"):
            return {"status": "blocked", "reason": "mine_trunk_patch_bank_missing", "trunk": self.state["trunk"]}
        tap = self.state["tap"]
        result = mine_trunk.record_tap(self.root, obs["world_id"], tap, block=self._block())
        if result:
            return result
        self._refresh_owners(mine_trunk.release_trunk_belts(self.root, obs["world_id"], tap))
        action = mine_trunk.cutover(self.builder, obs, tap)
        if tap.get("built"):
            mine_trunk.record_tap(self.root, obs["world_id"], tap)
        self._save()
        if action is not None:
            return action
        result = self.builder.ensure_plan(obs, self.state["branch"])
        if result.get("status") != "succeeded":
            return result
        self.state["phase"] = "verify"
        self._save()
        return self._verify(obs)

    def _verify(self, obs):
        builder = self.builder.state
        last = [e for e in self.state["branch"]["entities"] if xy(e) == xy(self.state["join"]["port"])]
        evidence = self.builder.power_evidence(
            builder["power_plan"], {"drill": self.state["bank_drill"], "entities": last}
        )
        if not evidence.get("ok"):
            return {"status": "blocked", "reason": "power_coal_tap_evidence_query_failed", "evidence": evidence}
        if not power_flow_ready(evidence):
            self.state.pop("sample_tick", None)
            self._save()
            return {
                "status": "waiting",
                "reason": "waiting for trunk coal at the boiler feed join",
                "evidence": evidence,
            }
        tick = int(evidence.get("tick", obs.get("tick", 0)))
        first = self.state.setdefault("sample_tick", tick)
        self._save()
        if tick - first < VERIFY_TICKS:
            return {"status": "waiting", "reason": "verifying trunk-fed boiler coal over 30 game seconds"}
        # The replacement is proven: the adopted plan is saved before anything old is mined.
        old = deepcopy(builder["coal_plan"])
        keep = list(self.state["kept"]) + list(self.state["branch"]["entities"])
        self.state.update(
            phase="retire",
            replacement_evidence=evidence,
            retired_coal_plan=old,
            retire=retired_entities(old, keep),
        )
        self._save()
        builder["coal_plan"] = adopted_plan(
            old,
            self.state["branch"],
            self.state["kept"],
            bank_drill=self.state["bank_drill"],
            tap=self.state["tap"],
            world_id=obs["world_id"],
        )
        self.builder._save()
        from .obsolete import release_block

        release_block(self.root / "pcb-board.json", obs["world_id"], FEED_BLOCK)
        return self._retire(obs)

    def _retire(self, obs):
        from .obsolete import guarded_mine, room_action, slots_needed

        for spec in self.state.get("retire", []):
            live = _find(obs, spec)
            if live is None:
                continue
            if not self._boiler_fuelled(obs):
                return {"status": "waiting", "reason": "power_coal_tap_boiler_unfuelled_before_retirement"}
            room = room_action(obs, slots_needed(self.catalog, [live]))
            if room is not None:
                return room
            action = guarded_mine(live, obs["world_id"], "retire the starter boiler feed a proven trunk tap replaced")
            if action is None:
                return {"status": "blocked", "reason": "power_coal_tap_retired_entity_has_no_unit", "entity": spec}
            return action
        self.state["phase"] = "done"
        self.state["retired_tick"] = obs.get("tick")
        self._save()
        return self._done(obs)

    def _done(self, obs):
        proof = self.proof(obs)
        if proof is None:
            return {"status": "waiting", "reason": "waiting for the trunk-fed boiler feed to stand"}
        return {"status": "succeeded", "reason": "boiler coal comes from the coal patch trunk", "evidence": proof}

    def proof(self, obs, parameters=None):
        """Fresh-observation proof: the adopted plan, the tap with its priority, the branch, no old feed, boiler fuel."""
        state = self.state
        if state.get("phase") != "done" or state.get("world_id") != obs.get("world_id"):
            return None
        plan = (self.builder.state or {}).get("coal_plan") or {}
        if plan.get("template") != TEMPLATE or plan.get("trunk") != state["trunk"]:
            return None
        if not mine_trunk.tap_standing(obs, state["tap"]):
            return None
        if any(_find(obs, e) is None for e in state["branch"]["entities"]):
            return None
        if any(_find(obs, e) is not None for e in state.get("retire", [])):
            return None
        if not self._boiler_fuelled(obs):
            return None
        return {
            "tick": obs.get("tick"),
            "trunk": state["trunk"],
            "tap": state["tap"]["name"],
            "priority": mine_trunk.priority_side(state["tap"]),
            "branch_entities": len(state["branch"]["entities"]),
            "retired_entities": len(state.get("retire", [])),
            "demand_per_minute": state.get("demand_per_minute"),
        }
