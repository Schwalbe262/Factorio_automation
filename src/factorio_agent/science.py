"""Permanent belt-fed red science, powered labs and model-selected research."""

from __future__ import annotations

import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .smelting import port, merge_entities
from .transport import plan_connection as belt_connection, xy, underground_proof
from .power_grid import plan_connection as power_connection
from .planning import overlap, survey_slots, preflight
from .site.placement import settle
from .plate_supply import bus_budget, plate_bus, plate_sources
from . import planned_tap
from . import science_growth

# Board block holding the plan's routes, taps and power links outside the two
# template blocks while they are unbuilt (see ``routes_block``).
ROUTES_BLOCK = "automation_science_routes"


def validate_rate(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"rate_per_minute"}:
        raise ValueError("automation_science requires rate_per_minute")
    rate = parameters["rate_per_minute"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 30:
        raise ValueError("red science target must be finite in (0,30]")


def science_row(x, y, count):
    es = []
    for i in range(count):
        dx = 4 * i
        es += [
            entity("assembling-machine-1", x + dx, y, recipe="automation-science-pack"),
            entity("inserter", x + dx, y - 2, 0),
            entity("inserter", x + dx, y + 2, 0),
            entity("small-electric-pole", x + dx + 2, y),
        ]
    es += [entity("transport-belt", x + dx, y - 3, 4) for dx in range(-4, 23)]
    es += [entity("transport-belt", x + dx, y + 3, 4) for dx in range(23)]
    es += [
        entity("transport-belt", x - 2, y - 5, 8),
        entity("transport-belt", x - 2, y - 4, 8),
        entity("transport-belt", x - 3, y - 1, 0),
        entity("transport-belt", x - 3, y - 2, 0),
    ]
    es += [
        entity("assembling-machine-1", x - 4, y + 6, recipe="iron-gear-wheel"),
        entity("inserter", x - 6, y + 5, 12),
        entity("inserter", x - 6, y + 7, 12),
        entity("inserter", x - 4, y + 4, 8),
        entity("small-electric-pole", x - 6, y + 6),
        # Links the gear pole to the row poles: they are 10 tiles apart, past
        # small-pole wire reach, and the gear cell stayed unpowered on a fresh map.
        entity("small-electric-pole", x - 2, y + 4),
    ]
    es += [entity("transport-belt", x - 7, y + dy, 0) for dy in range(4, 9)]
    es += [entity("transport-belt", x - 4, y + dy, 0 if dy > -1 else 4) for dy in range(-1, 4)]
    ports = [
        port("iron-plate", x - 7, y + 8, 0),
        port("copper-plate", x - 2, y - 5, 8),
        port("automation-science-pack", x + 22, y + 3, 4, "output"),
    ]
    return _plan(
        es,
        ports,
        template="permanent_red_science_row",
        count=count,
        reserved_area=[[x - 8.5, y - 6.5], [x + 23.5, y + 9.5]],
    )


def lab_row(x, y, count=8):
    es = []
    for i in range(count):
        dx = 4 * i
        es += [
            entity("lab", x + dx, y),
            entity("inserter", x + dx, y - 2, 0),
            entity("small-electric-pole", x + dx + 2, y),
        ]
    es += [entity("transport-belt", x + dx, y - 3, 12) for dx in range(-1, 31)]
    return _plan(
        es,
        [port("automation-science-pack", x + 30, y - 3, 12)],
        template="belt_fed_lab_row",
        count=count,
        reserved_area=[[x - 1.5, y - 8.5], [x + 30.5, y + 7.5]],
    )


SCIENCE_BLOCK = (32, 32)  # science row (south half) + lab row (north half), canonical


def science_block(x0, y0, count):
    """Canonical science + lab rows filling the tile square ``[x0, x0 + 32) x [y0, y0 + 32)``.

    Labs take the north half, the science row the south half; plates enter
    on the west, science flows east to the labs' westbound feed.
    """
    return {"science": science_row(x0 + 8.5, y0 + 22.5, count), "labs": lab_row(x0 + 1.5, y0 + 8.5)}


def nominal_plate_supply(catalog, mining, smelting, items):
    """Nominal plates per minute the district delivers for each of ``items``.

    The smaller of the ready mining banks' ore rate and the furnace bank's
    smelting rate (``mining-outposts.json`` / ``smelting-district.json`` states).
    """
    available = {}
    for item in items:
        bank = next(b for b in smelting["banks"] if b["product"] == item)
        ore = sum(
            b["nominal_rate_per_minute"]
            for b in mining["banks"]
            if b.get("ready") and b["plan"]["resource"] == bank["resource"]
        )
        plates = (
            bank["count"] * catalog.entities["stone-furnace"]["crafting_speed"] * 60 / catalog.recipes[item]["energy"]
        )
        available[item] = min(ore, plates)
    return available


def legacy_science_candidates(count):
    """The development-map candidate list, used when no master plan site is available."""
    found = []
    for left, top in ((-48, -72), (-80, -72), (-48, -104), (-80, -104), (-16, -104), (16, -104), (-112, -72)):
        found.append(science_block(left, top - 16, count))
    return found


def planned_science_candidates(site, world_id, count):
    """Allocator blocks with the template turned so plate inputs face the bus edge."""
    from .site.placement import allocated_placements

    def inputs(block):
        return [p for p in block["science"]["ports"] if p["direction"] == "input"]

    found = []
    placements = allocated_placements(
        site,
        world_id,
        kind="science",
        name="automation_science",
        size=SCIENCE_BLOCK,
        build=lambda x0, y0: science_block(x0, y0, count),
        inputs=inputs,
    )
    for allocation, placement in placements:
        block = placement.plan
        for key in ("science", "labs"):
            block[key]["site_transform"] = placement.transform.to_dict()
        block["allocation"] = allocation
        found.append(block)
    return found


class AutomaticScience:
    def __init__(self, game, builder, catalog, site=None):
        self.game, self.builder, self.catalog = game, builder, catalog
        self.site = site
        self.path = Path(game.cfg.runtime_dir) / "automation-science.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def prepare(self, obs, parameters):
        validate_rate(parameters)
        root = self.path.parent
        board = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"ok": False, "reason": "science_supply_world_or_catalog_changed"}
        # Plates come from bus taps when the planned bus exists, else from the
        # smelting district's registered output ports (plate_supply).
        bus = plate_bus(board, obs["world_id"]) if self.site is not None else None
        source_ports, span = {}, None
        if bus is None:
            supply = plate_sources(root, board, obs["world_id"], self.catalog.fingerprint, owner="automation_science")
            if not supply.get("ok"):
                return supply
            source_ports = supply["ports"]
        else:
            span = planned_tap.max_span(self.game)
            if span is None:
                return {"ok": False, "reason": "planned_bus_underground_span_unknown"}
        if not all(obs.get("enabled_recipes", {}).get(name) for name in ("automation-science-pack", "iron-gear-wheel")):
            return {"ok": False, "reason": "science_machine_recipes_not_unlocked"}
        speed = self.catalog.entities["assembling-machine-1"]["crafting_speed"]
        nominal_each = speed * 60 / self.catalog.recipes["automation-science-pack"]["energy"]
        count = math.ceil(parameters["rate_per_minute"] * 1.15 / nominal_each)
        if count > science_growth.SITE_MACHINE_SLOTS:
            return {"ok": False, "reason": "red_science_row_exceeds_six_slots"}
        # The full growth frame is reserved (research_capacity on the research bill ahead,
        # bounded by the row's six slots); plates and labs decide how many are built first.
        try:
            frame_targets = self._queue_targets(obs, horizon=science_growth.FRAME_HORIZON)
            slots = science_growth.frame_size(self.catalog, frame_targets, requested=count)
        except (KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
            slots = count
        mining = json.loads((root / "mining-outposts.json").read_text(encoding="utf-8"))
        smelting = json.loads((root / "smelting-district.json").read_text(encoding="utf-8"))
        if mining.get("world_id") != obs["world_id"] or smelting.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "science_raw_supply_world_changed"}
        red = self.catalog.recipes["automation-science-pack"]
        gear = self.catalog.recipes["iron-gear-wheel"]
        ingredients = {v["name"]: v["amount"] for v in red["ingredients"]}
        gear_yield = sum(v["amount"] for v in gear["products"] if v["name"] == "iron-gear-wheel")
        iron_per_gear = next(v["amount"] for v in gear["ingredients"] if v["name"] == "iron-plate") / gear_yield
        per_machine = {
            "iron-plate": nominal_each * ingredients["iron-gear-wheel"] * iron_per_gear,
            "copper-plate": nominal_each * ingredients["copper-plate"],
        }
        available = nominal_plate_supply(self.catalog, mining, smelting, per_machine)
        if bus is not None:  # taps share the trunk (planned_tap supply rule)
            available = bus_budget(bus, available, owner="automation_science")
        # Size the row to what the connected supply carries instead of blocking:
        # a smaller row that runs beats a full row that cannot be planned.
        count = min(count, *(int(available[item] / rate + 1e-9) for item, rate in per_machine.items()))
        if count < 1:
            item = min(per_machine, key=lambda name: available[name] / per_machine[name])
            return {
                "ok": False,
                "reason": "science_needs_connected_supply_headroom",
                "item": item,
                "needed": per_machine[item],
                "available_nominal": available[item],
            }
        candidates = []
        # Sites come from the master plan; the fixed development-map list is
        # only for a controller built without a site service.
        if self.site is not None:
            blocks = planned_science_candidates(self.site, obs["world_id"], slots)
        else:
            blocks = legacy_science_candidates(slots)
        for block in blocks:
            science, labs = block["science"], block["labs"]
            areas = [science["reserved_area"], labs["reserved_area"]]
            if any(overlap(area, b["reserved_area"]) for area in areas for b in board["blocks"].values()):
                continue
            candidates.append({"science": science, "labs": labs, "areas": areas, "allocation": block.get("allocation")})
        survey = survey_slots(self.game, [c["areas"] for c in candidates])
        if not survey.get("ok"):
            settle(self.site, obs["world_id"], "automation_science", None)
            return survey
        failures = []
        for index in survey.get("legal", []):
            site = candidates[index - 1]
            science, labs = site["science"], site["labs"]
            es = merge_entities(science["entities"], labs["entities"])
            routes = []
            clearance = preflight(self.game, self.builder, obs, es)
            if not clearance.get("ok"):
                failures.append(clearance)
                continue
            obstacles = {(e["name"], *xy(e)): e for e in clearance["clear_obstacles"]}
            inputs = [p for p in science["ports"] if p["direction"] == "input"]
            taps, ignored, sources = [], [], dict(source_ports)
            if bus is not None:
                demand = {item: count * rate for item, rate in per_machine.items()}
                tapped = self._taps(obs, bus, site, inputs, es, span, demand)
                if not tapped["ok"]:
                    failures.append(tapped)
                    continue
                es, taps, ignored, sources = (
                    tapped["entities"],
                    tapped["taps"],
                    tapped["ignored_units"],
                    tapped["sources"],
                )
                routes += tapped["routes"]
                for route in tapped["routes"]:
                    obstacles.update({(e["name"], *xy(e)): e for e in route["clear_obstacles"]})
            connections = [(sources[p["item"]], p) for p in inputs]
            connections += [(science["ports"][-1], labs["ports"][0])]
            # Tap routes are already planned with their taps; the rest is routed here.
            pending = connections[-1:] if bus is not None else connections
            good = True
            for source, destination in pending:
                route = belt_connection(self.game, self.builder, obs, source, destination, es, ignored_units=ignored)
                if not route.get("ok"):
                    failures.append(
                        {"reason": route.get("reason"), "item": source["item"], "area": science["reserved_area"]}
                    )
                    good = False
                    break
                routes.append(route)
                es = merge_entities(es, route["entities"])
                obstacles.update({(e["name"], *xy(e)): e for e in route["clear_obstacles"]})
            if not good:
                continue
            for block in (science, labs):
                pole = next(e for e in block["entities"] if e["name"] == "small-electric-pole")
                power = power_connection(
                    self.game,
                    self.builder,
                    obs,
                    pole["position"],
                    [e for e in es if e["name"] != "small-electric-pole"],
                )
                if not power.get("ok"):
                    failures.append(power)
                    good = False
                    break
                es = merge_entities(es, power["entities"])
            if not good:
                continue
            final = preflight(self.game, self.builder, obs, es, ignored_units=ignored)
            if not final.get("ok"):
                failures.append(final)
                continue
            obstacles.update({(e["name"], *xy(e)): e for e in final["clear_obstacles"]})
            pairs = [pair for route in routes for pair in route["underground_pairs"]] + planned_tap.tap_pairs(taps)
            plan = _plan(
                es,
                [science["ports"][-1]],
                underground_pairs=pairs,
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                layout_policy="pcb-permanent-science-v1",
            )
            settle(self.site, obs["world_id"], "automation_science", site.get("allocation"))
            start = science_growth.start_machines(self._queue_targets(obs), nominal_each, count)
            stage = science_growth.new_stage(
                {"science": science, "labs": labs},
                labs=science_growth.START_LABS,
                machines=start,
                machine_limit=count,
            )
            return {
                "ok": True,
                "stage": stage,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "requested_rate": parameters["rate_per_minute"],
                "nominal_machine_rate": count * nominal_each,
                "science": science,
                "labs": labs,
                "plan": plan,
                "connections": [
                    {
                        "item": a["item"],
                        "source": a["position"],
                        "destination": b["position"],
                        "owner": "automation_science",
                    }
                    for a, b in connections
                ],
                "route_metrics": [r["route_metrics"] for r in routes],
                "allocation": site.get("allocation"),
                "taps": taps,
            }
        settle(self.site, obs["world_id"], "automation_science", None)
        return {
            "ok": False,
            "reason": "no_reserved_science_and_lab_slots_with_clean_connections",
            "failures": failures,
            "legal_sites": len(survey.get("legal", [])),
        }

    def _taps(self, obs, bus, site, inputs, entities, span, demand):
        """Bus taps and their routes for one candidate block (``planned_tap.connect_inputs``)."""
        allocation = self.site.allocation(site["allocation"], obs["world_id"]) if site.get("allocation") else None
        if allocation is None:
            return {"ok": False, "reason": "science_bus_tap_needs_site_allocation"}
        return planned_tap.connect_inputs(
            self.game,
            self.builder,
            obs,
            bus,
            owner="automation_science",
            allocation=allocation,
            inputs=inputs,
            entities=entities,
            demand=demand,
            span=span,
            route=belt_connection,
        )

    def step(self, obs, parameters):
        validate_rate(parameters)
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            _atomic_json(self.path, self.state)
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "science_world_or_catalog_changed"}
        if parameters["rate_per_minute"] > self.state["requested_rate"]:
            return {"status": "blocked", "reason": "science_rate_expansion_requires_new_reserved_row"}
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "science_board_identity_changed"}
        changed = self._republish_blocks(board)
        if changed is None:
            return {"status": "blocked", "reason": "science_reserved_block_changed"}
        # Routes and power links are published with the blocks, before anything
        # is built, so every other planner sees them (``routes_block``).
        routes = routes_block(self.state)
        if routes is not None and board["blocks"].get(ROUTES_BLOCK) != routes:
            board["blocks"][ROUTES_BLOCK] = routes
            changed = True
        for connection in self.state["connections"]:
            if connection not in board.setdefault("connections", []):
                board["connections"].append(connection)
                changed = True
        if changed:
            _atomic_json(path, board)
        taps = self.state.get("taps", [])
        blocked = planned_tap.publish(self.path.parent, obs["world_id"], taps)
        if blocked:
            return blocked
        clear = self.state["plan"].get("clear_obstacles", [])
        cut = planned_tap.cutover(self.path.parent, self.builder, obs, taps, clear_obstacles=clear)
        if cut is not None:
            return cut
        result = self.builder.ensure_plan(obs, science_growth.build_plan(self.state))
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if proof:
            # Built routes protect themselves as observed belts from here on.
            self._retire_routes_block(obs)
            return {
                "status": "succeeded",
                "reason": "normally belt-fed machine science reaches powered labs",
                "evidence": proof,
            }
        return {"status": "waiting", "reason": "waiting for every science assembler and automatic lab inputs"}

    def proof(self, obs):
        if (
            not self.state
            or self.state["world_id"] != obs["world_id"]
            or not underground_proof(obs, self.state["plan"])
        ):
            return None
        actual = _built(obs, science_growth.build_plan(self.state))
        if actual is None:
            return None
        machines = [actual[(e["name"], *xy(e))] for e in science_growth.built_machines(self.state)]
        if any(e.get("products_finished", 0) < 1 for e in machines):
            return None
        labs = [actual[(e["name"], *xy(e))] for e in science_growth.built_labs(self.state)]
        if not any(
            e.get("inventory", {}).get("automation-science-pack", 0) > 0 or e.get("status_name") == "working"
            for e in labs
        ):
            return None
        return {
            "tick": obs["tick"],
            "science_units": [e["unit_number"] for e in machines],
            "machine_products_finished": [e["products_finished"] for e in machines],
            "lab_units": [e["unit_number"] for e in labs],
            "lab_science_stock": sum(e.get("inventory", {}).get("automation-science-pack", 0) for e in labs),
            "nominal_machine_rate_per_minute": self.state["nominal_machine_rate"],
            "stage": {k: v for k, v in (self.state.get("stage") or {}).items() if k != "growth_entities"},
            "scope": "normal continuous raw-belt supply and machine science into labs; rated capacity is not sustained measured science consumption",
        }

    def _queue_targets(self, obs, horizon=4):
        """Research queue for sizing (running research and next roadmap technologies of this world's ledger)."""
        from .research_capacity import queue_targets

        jobs = []
        ledger = self.path.parent / "jobs.json"
        try:
            data = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("world_id") == obs.get("world_id"):
            jobs = list((data.get("jobs") or {}).values())
        try:
            return queue_targets(self.catalog, jobs, obs, horizon=horizon)
        except (KeyError, TypeError, ValueError, AttributeError):
            return []

    def _retire_routes_block(self, obs):
        """Drop the routes block once everything it lists is built (observed hardware replaces it)."""
        path = self.path.parent / "pcb-board.json"
        try:
            board = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if board.get("world_id") == obs["world_id"] and ROUTES_BLOCK in board.get("blocks", {}):
            del board["blocks"][ROUTES_BLOCK]
            _atomic_json(path, board)

    def grow(self, obs, parameters):
        """Build reserved slots up to ``parameters`` ``labs``/``machines`` (capability ``science_growth``).

        Only slots of the saved block are built (``science_growth.build_plan``);
        machines beyond the reserved slots first widen the reservation to the
        frame (``_extend_frame``). The built stage is committed once every new
        entity is observed and powered. Never shrinks, never exceeds the frame
        or the lab slots (the plate limit is the receipt's, read live).
        """
        validate_growth(parameters)
        stage = (self.state or {}).get("stage")
        if not stage:
            return {"status": "blocked", "reason": "science_growth_needs_staged_science_block"}
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "science_world_or_catalog_changed"}
        labs, machines = parameters["labs"], parameters["machines"]
        if labs > stage["lab_slots"] or machines > science_growth.frame_capacity(self.state):
            return {"status": "blocked", "reason": "science_growth_exceeds_reserved_slots"}
        if machines > stage["machine_slots"]:
            # A block reserved with fewer slots than its frame (eval-4177156614: 2): widen the
            # reservation first (plan republished), then build into it.
            blocked = self._extend_frame(obs, machines)
            if blocked is not None:
                return blocked
            stage = self.state["stage"]
        labs, machines = max(labs, stage["labs"]), max(machines, stage["machines"])
        result = self.builder.ensure_plan(obs, science_growth.build_plan(self.state, labs=labs, machines=machines))
        if result.get("status") != "succeeded":
            return result
        grown = dict(self.state, stage=dict(stage, labs=labs, machines=machines))
        actual = _built(obs, science_growth.build_plan(grown))
        if actual is None:
            return {"status": "waiting", "reason": "waiting for grown science slots to be observed and powered"}
        if (labs, machines) != (stage["labs"], stage["machines"]):
            grown["stage"]["growth_entities"] = science_growth.growth_entities(grown)
            self.state = grown
            _atomic_json(self.path, self.state)
        return {"status": "succeeded", "reason": "reserved science slots built", "evidence": self.growth_proof(obs)}

    def _republish_blocks(self, board):
        """Put the state's science and lab blocks on ``board`` (in memory): True changed, False same, None refused.

        A board block another state wrote is refused; one this block's frame
        extension replaced (``frame.replaces`` fingerprint) is republished.
        """
        replaced = set((self.state.get("frame") or {}).get("replaces") or ())
        changed = False
        for key, plan in (("automation_science", self.state["science"]), ("laboratories", self.state["labs"])):
            old = board["blocks"].get(key)
            if old == plan:
                continue
            if old is not None and science_growth._fingerprint(old) not in replaced:
                return None
            board["blocks"][key] = plan
            changed = True
        return changed

    def _extend_frame(self, obs, machines):
        """Reserve the frame's further machine slots (``science_growth.extended_state``); None when reserved.

        Tries the whole frame first, then fewer slots down to ``machines``:
        the new slot entities must pass ``preflight`` on the live map (trees
        are cleared as the block's obstacles). The extended state is saved and
        the board republished before any slot is built. When no width holds
        ``machines`` the frame is recorded as blocked (``frame.extension_blocked``),
        so the growth receipt turns to the second-block decision instead.
        """
        old = self.state
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "science_board_identity_changed"}
        reserved = {(e["name"], *xy(e)) for e in old["plan"]["entities"]}
        failure = None
        for slots in range(science_growth.frame_capacity(old), machines - 1, -1):
            grown = science_growth.extended_state(old, slots)
            added = [e for e in grown["plan"]["entities"] if (e["name"], *xy(e)) not in reserved]
            if not added:
                continue
            clearance = preflight(self.game, self.builder, obs, added)
            if not clearance.get("ok"):
                failure = clearance
                continue
            plan = dict(grown["plan"])
            obstacles = {(e["name"], *xy(e)): e for e in plan.get("clear_obstacles", [])}
            obstacles.update({(e["name"], *xy(e)): e for e in clearance.get("clear_obstacles", [])})
            plan["clear_obstacles"] = list(obstacles.values())
            self.state = dict(grown, plan=plan)
            # The extended state is saved first: a board still holding the old block is then
            # republished by fingerprint (``_republish_blocks``), never refused.
            _atomic_json(self.path, self.state)
            if self._republish_blocks(board) is None:
                return {"status": "blocked", "reason": "science_reserved_block_changed"}
            _atomic_json(path, board)
            return None
        self.state = dict(old, frame=dict(old.get("frame") or {}, extension_blocked=(failure or {}).get("reason")))
        self.state["frame"]["extension_blocked"] = self.state["frame"]["extension_blocked"] or "no_free_frame_slot"
        _atomic_json(self.path, self.state)
        return {
            "status": "blocked",
            "reason": "science_frame_extension_blocked",
            "failure": failure,
            "next_step": "second science block or partial rebuild (science_growth receipt next_step)",
        }

    def growth_proof(self, obs, parameters=None):
        """Every entity of the built stage is observed and powered, and the stage reaches ``parameters``."""
        stage = (self.state or {}).get("stage")
        if not stage or self.state["world_id"] != obs["world_id"]:
            return None
        if parameters and (
            stage["labs"] < parameters.get("labs", 0) or stage["machines"] < parameters.get("machines", 0)
        ):
            return None
        actual = _built(obs, science_growth.build_plan(self.state))
        if actual is None:
            return None
        return {
            "tick": obs["tick"],
            "labs": stage["labs"],
            "machines": stage["machines"],
            "lab_units": [actual[(e["name"], *xy(e))]["unit_number"] for e in science_growth.built_labs(self.state)],
            "science_units": [
                actual[(e["name"], *xy(e))]["unit_number"] for e in science_growth.built_machines(self.state)
            ],
            "free_slots": {
                "labs": stage["lab_slots"] - stage["labs"],
                "machines": stage["machine_slots"] - stage["machines"],
            },
        }


def validate_growth(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"labs", "machines"}:
        raise ValueError("science_growth requires labs and machines")
    for key in ("labs", "machines"):
        value = parameters[key]
        if type(value) is not int or not 1 <= value <= 8:
            raise ValueError("science_growth " + key + " must be an integer in [1,8]")


def _built(obs, plan):
    """Observed entities by key when every entity of ``plan`` stands as planned and machines are powered, else None."""
    actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
    for wanted in plan["entities"]:
        e = actual.get((wanted["name"], *xy(wanted)))
        if not e:
            return None
        if wanted.get("recipe") and e.get("recipe") != wanted["recipe"]:
            return None
        if (
            wanted["name"] in ("transport-belt", "underground-belt", "inserter", "assembling-machine-1")
            and e["direction"] != wanted["direction"]
        ):
            return None
        if wanted["name"] in ("lab", "inserter", "assembling-machine-1") and (
            not e.get("electric_network_connected") or e.get("energy", 0) <= 0
        ):
            return None
    return actual


def routes_block(state):
    """Board block of the plan entities outside the science and lab blocks (routes, taps, power links), or None.

    ``board.planned_unbuilt_hardware`` and ``transport.unbuilt_external_keepouts``
    read every block's entities and underground pairs, so routes published here
    are foreign hardware for every later planner before they are built. Its
    ``reserved_area`` is the one tile under the first science machine (inside
    the science block, so no other reservation or port apron changes) because
    readers expect every block to have one; the routes lie outside it.
    """
    own = {(e["name"], *xy(e)) for e in state["science"]["entities"] + state["labs"]["entities"]}
    # Tap hardware (splitter, dives, trunk extension) is published with the bus (``planned_tap.publish``).
    own |= {(e["name"], *xy(e)) for tap in state.get("taps") or [] for e in tap.get("entities", [])}
    entities = [e for e in state["plan"]["entities"] if (e["name"], *xy(e)) not in own]
    if not entities:
        return None
    tap_pairs = planned_tap.tap_pairs(state.get("taps") or [])
    machine = next(e for e in state["science"]["entities"] if e["name"] == "assembling-machine-1")
    x, y = xy(machine)
    return _plan(
        entities,
        [],
        template=ROUTES_BLOCK,
        owner="automation_science",
        reserved_area=[[x - 0.5, y - 0.5], [x + 0.5, y + 0.5]],
        underground_pairs=[p for p in state["plan"].get("underground_pairs", []) if p not in tap_pairs],
        layout_policy="pcb-permanent-science-routes-v1",
    )
