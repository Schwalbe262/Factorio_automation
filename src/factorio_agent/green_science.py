"""Green science: on a planned world the audited green candidates (the direct-insertion cluster, the
split row; ``green_layout``) on bus taps, the split's reserved pack slots grown by ``grow``
(``science_growth {pack, machines}``, ``pack_growth.py``); without a site service the
development-map ``green_core`` at fixed anchors. Packs join the lab belt lane."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .manufacturing import green_core, raw_demand
from .manufacturing import connect_power_groups, lab_green_entry, lab_green_entry_outside
from .planning import preflight, overlap, survey_slots
from .science import nominal_plate_supply
from .science_growth import built_labs
from .plate_supply import bus_budget, plate_bus
from .planned_tap import tap_budget
from . import planned_tap, remedy
from .smelting import merge_entities
from .site.placement import allocated_placements, settle
from .site.transform import Transform, apply as apply_transform, in_canonical, plan_transform
from .site.transform import point as transform_point
from .transport import plan_connection, xy, underground_proof


def validate_rate(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"rate_per_minute"}:
        raise ValueError("logistic_science requires rate_per_minute")
    r = parameters["rate_per_minute"]
    if type(r) not in (int, float) or not math.isfinite(r) or not 0 < r <= 30:
        raise ValueError("green science target must be finite in (0,30]")


# Development-map anchors, only for a controller built without a site service.
LEGACY_GREEN_ANCHORS = (
    (40.5, -72.5),
    (56.5, -72.5),
    (72.5, -72.5),
    (40.5, -8.5),
    (-96.5, -72.5),
    (-96.5, -8.5),
    (104.5, -72.5),
)


GREEN_PLATES = ("iron-plate", "copper-plate")
GREEN_TREE = ("inserter", "transport-belt", "iron-gear-wheel", "electronic-circuit", "copper-cable")


def green_cluster(catalog, origin, output_side=None):
    """The first green science unit (``blocks.cluster``): one pack machine and one machine per
    intermediate, direct insertion, plates from one two-lane belt, packs on a one-tile belt port."""
    from .blocks.cluster import cluster_block

    each = (
        60
        * catalog.entities["assembling-machine-1"]["crafting_speed"]
        / catalog.recipes["logistic-science-pack"]["energy"]
    )
    plan = cluster_block(
        catalog,
        "logistic-science-pack",
        each,
        sourced=GREEN_PLATES.__contains__,
        output_side=output_side,
        origin=origin,
    )
    return plan


SPLIT_NAME = "logistic_science_split"  # allocation name of the split geometry (held beside the cluster's)


def ceiling(descriptor) -> float:
    """Packs/minute a candidate reaches without a new block: its start or its in-place growth (the
    reserved slots ``science_growth`` builds). A second cluster is not stackable on the site (it would
    need a second lab entry), so a cluster's ceiling is its start."""
    growth = descriptor.get("growth") or {}
    return max(float(descriptor["rate_now"]), float(growth.get("max_rate_in_place") or 0.0))


def planned_green_layouts(site, world_id, catalog, lab_port, *, rate, tick=None, root=None, horizon=None):
    """Green cores of every audited candidate (``green_layout``: the cluster, the split row), best first.

    Each candidate is probed on one allocation: ``layout_selection.score`` with the plate ports'
    ``site.fit`` cost plus the distance from the pack port to the lab entry (``W_PORT`` per tile).
    The horizon is ``max(rate, horizon)``: the request, or what the built labs consume
    (``LogisticScience.prepare``: ``research_capacity.lab_pack_rate``). Reachability first: a
    candidate whose ceiling (``ceiling``) is below that horizon is never an option while another
    candidate reaches it (a 5/min cluster for a 30/min request is a dead end: eval-1575138435);
    it stays listed in the record's ``excluded``. Exploration (``layout_selection.settings``) may
    pick a close runner-up. Every core carries its ``layout`` record; the caller keeps one
    allocation (``settle_green``).
    """
    from . import green_layout as gl
    from . import layout_selection as selection
    from .blocks.cluster_audit import audit_cluster

    need = max(float(rate), float(horizon or 0.0))
    demand = selection.demand_for(rate, tick=tick)
    demand = selection.Demand(demand.now, need, demand.plates_scarce)
    target = (lab_port["position"]["x"], lab_port["position"]["y"])

    def distance(plan):
        p = plan["ports"][-1]["position"]
        return abs(p["x"] - target[0]) + abs(p["y"] - target[1])

    candidates = []
    probe = green_cluster(catalog, (0.5, 0.5))
    if not audit_cluster(catalog, probe):
        record = green_layout_record(catalog, probe)
        descriptor = dict(gl.describe(catalog, gl.GreenGenome("cluster"), probe, rate), id=record["variant"])
        candidates.append(("cluster", _Genome(record["genome"]), descriptor))
    try:
        split = gl.split_block(catalog)
        findings = gl.audit_split(catalog, split)
    except ValueError as exc:
        split, findings = None, [str(exc)[:120]]
    if split is not None and not findings:
        genome = gl.GreenGenome("split", frame=split["frame"])
        candidates.append(("split", genome, gl.describe(catalog, genome, split, rate)))
    options = []
    for form, genome, descriptor in candidates:
        cores = (
            planned_green_clusters(site, world_id, catalog, lab_port, count=1)
            if form == "cluster"
            else planned_green_split(site, world_id, catalog, count=1)
        )
        settle_green(site, world_id, None)
        if not cores:
            continue
        cost = cores[0].get("port_cost", 0.0) + selection.W_PORT * distance(cores[0])
        scored = descriptor
        if descriptor["growth"].get("capability") == "not_implemented":
            # Reserved slots nothing builds earn no ceiling credit (the record keeps the real one).
            scored = dict(descriptor, growth=dict(descriptor["growth"], max_rate_in_place=descriptor["rate_now"]))
        candidate = {
            "genome": genome,
            "descriptor": scored,
            "compact": gl.compact(descriptor),
            "niche": f"{form}/{descriptor['port_relation']}",
            "source": "generated",
        }
        options.append(selection.option(candidate, demand, cost, form=form, ceiling=ceiling(scored)))
    reaching = [o for o in options if o["ceiling"] + 1e-9 >= need]
    excluded = (
        []
        if not reaching
        else [{"variant": o["id"], "ceiling": o["ceiling"], "needed": need} for o in options if o not in reaching]
    )
    options = selection.ranked(reaching or options)
    if not options:
        return []
    config = selection.settings(root) if root is not None else selection.Settings()
    index, pool = selection.choose(
        [o["score"] for o in options], config, selection.seed_for(world_id, "logistic_science")
    )
    # Remedy ladder rung "layout:next_form" (remedy.RELAXATIONS): the next form is tried first.
    index = (index + int(remedy.option("layout_rotate", 0) or 0)) % len(options)
    chosen = options[index]
    found = []
    for option in [chosen] + [o for o in options if o is not chosen]:
        if option["form"] == "cluster":
            cores = planned_green_clusters(site, world_id, catalog, lab_port)
        else:
            cores = planned_green_split(site, world_id, catalog)
        record = selection.record(
            options, option, config=config, pool_size=pool, tick=tick, horizon=need, excluded=excluded or None
        )
        for core in cores:
            core["layout"] = dict(record, source="generated")
        found += cores
    return found


class _Genome:
    """A stored genome dict with the ``to_dict`` the layout record calls."""

    def __init__(self, data):
        self.data = dict(data)

    def to_dict(self):
        return dict(self.data)


def settle_green(site, world_id, keep):
    """Keep allocation ``keep`` among both green candidates' allocations (None: release all)."""
    settle(site, world_id, "logistic_science", keep)
    settle(site, world_id, SPLIT_NAME, keep)


def planned_green_split(site, world_id, catalog, *, count=None):
    """Allocator blocks for the split green row (``green_layout.split_block``): plate ports to the bus edge."""
    from .green_layout import split_block
    from .site.placement import DEFAULT_COUNT

    probe = split_block(catalog)
    (x0, y0), (x1, y1) = probe["reserved_area"]
    size = (int(x1 - x0), int(y1 - y0))

    def build(bx, by):
        return split_block(catalog, origin=(bx, by))

    def inputs(plan):
        return [p for p in plan["ports"] if p["direction"] == "input"]

    placements = []
    for shape in ((size[1], size[0]), size):
        placements = allocated_placements(
            site,
            world_id,
            kind="science",
            name=SPLIT_NAME,
            size=size,
            block=shape,
            build=build,
            inputs=inputs,
            count=count or DEFAULT_COUNT,
        )
        if placements:
            break
    found = []
    for allocation, placement in placements:
        core = placement.plan
        core["site_transform"] = placement.transform.to_dict()
        core["allocation"] = allocation
        core["port_cost"] = placement.score
        core["connections"] = []
        found.append(core)
    return found


def planned_green_clusters(site, world_id, catalog, lab_port, count=None):
    """Allocator blocks for the green unit; plate ports face the bus edge, the product side is the one
    (among the compact layout's free sides) whose port lands nearest the lab entry ``lab_port``."""
    from .site.placement import DEFAULT_COUNT

    from .production_layout import _even_turn

    probe = green_cluster(catalog, (0.5, 0.5))
    area = _even_turn(probe["reserved_area"])
    (cx0, cy0), (cx1, cy1) = area
    size = (int(cx1 - cx0), int(cy1 - cy0))

    def build_side(side):
        def build(x0, y0):
            plan = green_cluster(catalog, (0.5 + x0 - cx0, 0.5 + y0 - cy0), side)
            plan["reserved_area"] = [[x0, y0], [x0 + size[0], y0 + size[1]]]
            return plan

        return build

    def inputs(plan):
        return [p for p in plan["ports"] if p["direction"] == "input"]

    found = []
    placements = allocated_placements(
        site,
        world_id,
        kind="science",
        name="logistic_science",
        size=size,
        block=(size[1], size[0]),
        build=build_side(probe["output_side"]),
        inputs=inputs,
        count=count or DEFAULT_COUNT,
    )
    target = (lab_port["position"]["x"], lab_port["position"]["y"])
    for allocation, placement in placements:
        rect = placement.target
        corner = (int(rect.x0 + rect.width / 2 - size[0] / 2), int(rect.y0 + rect.height / 2 - size[1] / 2))
        best = None
        for side in probe["output_sides"]:
            plan = apply_transform(placement.transform, build_side(side)(*corner))
            out = plan["ports"][-1]["position"]
            score = abs(out["x"] - target[0]) + abs(out["y"] - target[1])
            if best is None or score < best[0]:
                best = (score, plan)
        core = best[1]
        core["site_transform"] = placement.transform.to_dict()
        core["allocation"] = allocation
        core["port_cost"] = placement.score
        core["layout"] = green_layout_record(catalog, core)
        core["connections"] = []
        core["cells"] = {"cluster": {"entities": core["entities"]}}
        found.append(core)
    return found


def green_layout_record(catalog, plan) -> dict:
    """The ``layout`` record of the green unit (fleet report): its genome and a few metrics.

    The green tree has one cluster topology under ``blocks.cluster`` (the pack machine
    needs two belt-fed neighbours and a free side: only the 4 x 2 turned grid); the
    output side is chosen per placement (nearest the lab entry). The other candidate is
    the split row (``green_layout``); ``planned_green_layouts`` ranks both and replaces
    this record with the ranked one.
    """
    from .layout_variants import Genome, energy_metrics

    genome = Genome(
        "cluster",
        plan["machine"],
        grid=(plan["grid"]["columns"], plan["grid"]["rows"]),
        turned=bool(plan["grid"]["turned_belt"]),
        cells=tuple(sorted((n["recipe"], tuple(n["cell"])) for n in plan["nodes"])),
        output_side=plan["output_side"],
    )
    (x0, y0), (x1, y1) = plan["reserved_area"]
    names = [e["name"] for e in plan["entities"]]
    return {
        "variant": genome.key(),
        "genome": genome.to_dict(),
        "descriptor": {
            "footprint": [int(x1 - x0), int(y1 - y0)],
            "counts": {
                "machines": sum(1 for e in plan["entities"] if e.get("recipe")),
                "belts": names.count("transport-belt"),
            },
            "rate_now": plan.get("rated_per_minute"),
            **energy_metrics(catalog, genome, plan),
        },
        "source": "generated",
        "explored": False,
    }


INGREDIENT_SHORT = frozenset({"item_ingredient_shortage", "no_ingredients"})


def starved_inputs(machines) -> dict:
    """``{"starved_recipes"}``: recipes of the block's machines that wait for ingredients (native status)."""
    recipes = sorted({m.get("recipe") for m in machines if m.get("status_name") in INGREDIENT_SHORT} - {None})
    return {"starved_recipes": recipes} if recipes else {}


class LogisticScience:
    def __init__(self, game, builder, catalog, smelting, science, site=None):
        self.game, self.builder, self.catalog, self.smelting, self.science = game, builder, catalog, smelting, science
        self.site = site
        self.path = Path(game.cfg.runtime_dir) / "logistic-science.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def prepare(self, obs, parameters):
        validate_rate(parameters)
        if not self.science.proof(obs):
            return {"ok": False, "reason": "commissioned_red_science_required"}
        planned = self.site is not None
        enabled = {k for k, v in obs.get("enabled_recipes", {}).items() if v}
        if planned:
            # The cluster uses plain inserters; every recipe of the tree must be settable.
            missing = sorted({"logistic-science-pack", *GREEN_TREE} - enabled)
            if missing:
                return {"ok": False, "reason": "native_green_science_research_required", "missing": missing}
        elif not {"logistic-science-pack", "fast-inserter"}.issubset(enabled):
            return {"ok": False, "reason": "native_green_science_and_fast_inserter_research_required"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or self.smelting.state.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "green_supply_world_changed"}
        each = (
            self.catalog.entities["assembling-machine-1"]["crafting_speed"]
            * 60
            / self.catalog.recipes["logistic-science-pack"]["energy"]
        )
        count = math.ceil(parameters["rate_per_minute"] * 1.15 / each)
        if count > 7:
            return {"ok": False, "reason": "green_science_row_exceeds_seven_slots"}
        # Plates come from bus taps on a planned world; the splitter branch at
        # the district below is only for a world without a site service.
        bus = plate_bus(board, obs["world_id"]) if planned else None
        if planned and bus is None:
            return {"ok": False, "reason": "green_science_needs_planned_bus"}
        sized = self.supply_sized_count(count, each, bus=bus)
        if not sized.get("ok"):
            return sized
        # Start small (one pack machine fed by one machine per intermediate); a
        # higher rate is a later block, decided by blocks.cluster.grow_decision.
        count = 1 if planned else sized["count"]
        demand = raw_demand(self.catalog, "logistic-science-pack", count * each)
        for recipe, equivalent in demand["machine_equivalents"].items():
            if recipe != "logistic-science-pack" and equivalent > 1:
                return {"ok": False, "reason": "one_intermediate_cell_insufficient", "recipe": recipe}
        span = None
        if bus is not None:
            span = planned_tap.max_span(self.game)
            if span is None:
                return {"ok": False, "reason": "planned_bus_underground_span_unknown"}
        source_plan = deepcopy(self.smelting.state["plan"])
        removed = []
        branches = []
        sources = {}
        # The branch is defined in the district's canonical frame (plates
        # leaving east) and turned like the district (identity when unturned).
        turn = Transform.from_dict(source_plan.get("site_transform"))
        canonical = apply_transform(turn.inverse(), {"reserved_area": source_plan["reserved_area"]})
        district_x1 = canonical["reserved_area"][1][0]  # east edge, where the plate ports sit
        taken = self.builder._occupied_by_plan(obs["entities"] + source_plan["entities"])

        def mapped(x, y):
            return xy({"position": transform_point(turn, {"x": x, "y": y})})

        for p in source_plan["ports"] if bus is None else []:
            if p["item"] not in ("iron-plate", "copper-plate"):
                continue
            x, y = xy(apply_transform(turn.inverse(), p))
            anchor = mapped(x - 1, y)
            heading = turn.direction(4)
            old = next(
                (
                    e
                    for e in obs["entities"]
                    if e["name"] == "transport-belt" and xy(e) == anchor and e["direction"] == heading
                ),
                None,
            )
            if not old:
                return {"ok": False, "reason": "owned_plate_branch_anchor_missing", "item": p["item"]}
            if not any(e["name"] == "transport-belt" and xy(e) == xy(old) for e in source_plan["entities"]):
                return {"ok": False, "reason": "plate_branch_anchor_not_owned"}
            # The splitter's second lane runs east beside the port row until
            # its first tile outside the district reservation: a route may
            # only start from a registered, built port inside a reservation.
            stub = [x]
            while stub[-1] - 0.49 < district_x1:
                stub.append(stub[-1] + 1)
            side = next(
                (s for s in (-1, 1) if not any(mapped(bx, y + s) in taken for bx in [x - 1, *stub])),
                None,
            )
            if side is None:
                return {"ok": False, "reason": "plate_branch_lane_blocked", "item": p["item"]}
            removed.append(old)
            branches += apply_transform(
                turn,
                [dict(entity("splitter", x - 1, y + side * 0.5, 4), _width=2, _height=1)]
                + [entity("transport-belt", bx, y + side, 4) for bx in stub],
            )
            sources[p["item"]] = apply_transform(turn, dict(p, position={"x": stub[-1], "y": y + side}, facing=4))
        ignored = {e["unit_number"] for e in removed}
        source_plan["entities"] = [
            e for e in source_plan["entities"] if not any(e["name"] == r["name"] and xy(e) == xy(r) for r in removed)
        ]
        source_plan = _plan(
            merge_entities(source_plan["entities"], branches),
            source_plan["ports"],
            **{
                k: v for k, v in source_plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")
            },
        )
        labs_plan = self.science.state["labs"]
        if self.site is not None:
            entry = in_canonical(labs_plan, lab_green_entry_outside)
        else:  # development-map lab rows without a board reservation contract
            entry = in_canonical(labs_plan, lab_green_entry)
            entry["pairs"] = [entry["pair"]]
        lab_heading = plan_transform(labs_plan).direction(12)
        jx, jy = xy({"position": entry["join"]})
        join = next(
            (
                e
                for e in obs["entities"]
                if e["name"] == "transport-belt" and xy(e) == (jx, jy) and e["direction"] == lab_heading
            ),
            None,
        )
        if not join:
            return {"ok": False, "reason": "straight_lab_lane_join_anchor_missing"}
        lab_port = entry["port"]
        join_es = entry["entities"]
        join_pairs = entry["pairs"]
        virtual = {**obs, "entities": [e for e in obs["entities"] if e.get("unit_number") not in ignored]}
        adapter = SimpleNamespace(
            _occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda es: preflight(self.game, self.builder, obs, es, ignored_units=ignored),
        )
        candidates = []
        # Sites come from the master plan; the fixed development-map anchors
        # are only for a controller built without a site service.
        if self.site is not None:
            cores = planned_green_layouts(
                self.site,
                obs["world_id"],
                self.catalog,
                lab_port,
                rate=parameters["rate_per_minute"],
                tick=obs.get("tick"),
                root=self.path.parent,
                horizon=self.lab_demand(obs),
            )
            if not cores:
                return self.no_allocation(obs["world_id"])
        else:
            cores = [green_core(x, y, count) for x, y in LEGACY_GREEN_ANCHORS]
        for core in cores:
            if not any(overlap(core["reserved_area"], b["reserved_area"]) for b in board["blocks"].values()):
                candidates.append(core)
        survey = survey_slots(self.game, [[c["reserved_area"]] for c in candidates])
        if not survey.get("ok"):
            settle_green(self.site, obs["world_id"], None)
            return survey
        failures = []
        for index in survey.get("legal", []):
            core = candidates[index - 1]
            es = merge_entities(core["entities"], branches, join_es)
            routes, taps = [], []
            units, route_obs, route_builder = ignored, virtual, adapter
            check = preflight(self.game, self.builder, obs, es, ignored_units=units)
            if not check.get("ok"):
                failures.append(check)
                continue
            obstacles = {(e["name"], *xy(e)): e for e in check["clear_obstacles"]}
            inputs = [p for p in core["ports"] if p["direction"] == "input"]
            connections = core["connections"] + [(sources[p["item"]], p) for p in inputs if bus is None]
            if bus is not None:
                tapped = self._taps(obs, bus, core, inputs, es, span, demand["raw_items_per_minute"])
                if not tapped["ok"]:
                    failures.append(tapped)
                    continue
                es, taps, routes = tapped["entities"], tapped["taps"], list(tapped["routes"])
                units = frozenset(tapped["ignored_units"])
                route_obs = {**obs, "entities": [e for e in obs["entities"] if e.get("unit_number") not in units]}
                route_builder = SimpleNamespace(
                    _occupied_by_plan=self.builder._occupied_by_plan,
                    can_place=lambda es, units=units: preflight(self.game, self.builder, obs, es, ignored_units=units),
                )
                for route in routes:
                    obstacles.update({(e["name"], *xy(e)): e for e in route["clear_obstacles"]})
            connections.append((core["ports"][-1], lab_port))
            valid = True
            for source, destination in connections:
                route = plan_connection(
                    self.game, route_builder, route_obs, source, destination, es, ignored_units=units
                )
                if not route.get("ok"):
                    failures.append(
                        {
                            "reason": route.get("reason"),
                            "item": source["item"],
                            "source": source["position"],
                            "destination": destination["position"],
                            "area": core["reserved_area"],
                        }
                    )
                    valid = False
                    break
                routes.append(route)
                es = merge_entities(es, route["entities"])
                obstacles.update({(e["name"], *xy(e)): e for e in route["clear_obstacles"]})
            if not valid:
                continue
            power = connect_power_groups(self.game, self.builder, obs, list(core["cells"].values()), es)
            if not power.get("ok"):
                failures.append(power)
                continue
            es = merge_entities(es, power["entities"])
            check = preflight(self.game, self.builder, obs, es, ignored_units=units)
            if not check.get("ok"):
                failures.append(check)
                continue
            obstacles.update({(e["name"], *xy(e)): e for e in check["clear_obstacles"]})
            pairs = join_pairs + [p for r in routes for p in r["underground_pairs"]] + planned_tap.tap_pairs(taps)
            plan = _plan(
                es,
                core["ports"],
                reserved_area=core["reserved_area"],
                underground_pairs=pairs,
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                layout_policy="pcb-green-science-cells-v1",
            )
            settle_green(self.site, obs["world_id"], core.get("allocation"))
            return {
                "ok": True,
                "world_id": obs["world_id"],
                "catalog_fingerprint": self.catalog.fingerprint,
                "phase": "recover",
                "plan": plan,
                "core": core,
                "source_plan": source_plan,
                "remove": removed,
                "lab_join": {k: join[k] for k in ("name", "position", "direction", "unit_number")},
                "requested_rate": parameters["rate_per_minute"],
                "nominal_machine_rate": count * each,
                "rated_demand": demand,
                "route_metrics": [r["route_metrics"] for r in routes],
                "taps": taps,
                "layout": dict(core.get("layout") or {}, chosen_tick=obs.get("tick")) if core.get("layout") else None,
            }
        settle_green(self.site, obs["world_id"], None)
        return {"ok": False, "reason": "no_green_science_site_with_clean_connections", "failures": failures}

    def lab_demand(self, obs):
        """Green packs/minute the built labs consume over the research queue (``research_capacity.lab_pack_rate``),
        at most the step's 30/min; None without a staged lab row or a queued technology using green."""
        from .research_capacity import GREEN_PACK, lab_pack_rate

        try:
            labs = len(built_labs(self.science.state))
            rate = lab_pack_rate(self.science._queue_targets(obs), labs, GREEN_PACK) if labs else None
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return None if rate is None else round(min(30.0, rate), 3)

    def _taps(self, obs, bus, core, inputs, entities, span, demand):
        """Bus taps and their routes for one candidate core (``planned_tap.connect_inputs``).

        An input port that states its own draw (``per_pack``: the split's sub-clusters) declares
        that draw at the built rate; otherwise the item's whole draw (one input per item)."""
        name = core.get("allocation")
        allocation = self.site.allocation(name, obs["world_id"]) if name else None
        if allocation is None:
            return {"ok": False, "reason": "green_bus_tap_needs_site_allocation"}
        packs = float(core.get("rated_per_minute") or 0.0)
        inputs = [
            dict(p, demand_per_minute=round(p["per_pack"] * packs, 3)) if p.get("per_pack") and packs else p
            for p in inputs
        ]
        return planned_tap.connect_inputs(
            self.game,
            self.builder,
            obs,
            bus,
            owner="logistic_science",
            allocation=allocation,
            inputs=inputs,
            entities=entities,
            demand={p["item"]: demand.get(p["item"], 0) for p in inputs},
            span=span,
        )

    def supply_sized_count(self, count, each, bus=None):
        """Green machines the connected plates can feed, at most ``count``.

        Without a bus the splitter branch shares each district plate belt
        with red science: green may use what red's row leaves, and never less
        than the half a saturated splitter hands each branch. With the planned
        bus the same rule runs on the trunk with every other tap's declared
        demand (``planned_tap.tap_budget``). Like red science, the row is
        sized down to that budget; only a budget below one machine blocks.
        """
        root = self.path.parent
        mining = json.loads((root / "mining-outposts.json").read_text(encoding="utf-8"))
        smelting = self.smelting.state
        if mining.get("world_id") != smelting.get("world_id"):
            return {"ok": False, "reason": "green_raw_supply_world_changed"}
        per_machine = raw_demand(self.catalog, "logistic-science-pack", each)["raw_items_per_minute"]
        red = raw_demand(self.catalog, "automation-science-pack", self.science.state.get("nominal_machine_rate", 0))
        available = nominal_plate_supply(self.catalog, mining, smelting, per_machine)
        if bus is not None:
            budget = bus_budget(bus, available, owner="logistic_science")
        else:
            budget = {
                item: tap_budget(available[item], red["raw_items_per_minute"].get(item, 0)) for item in per_machine
            }
        fit = min(count, *(int(budget[item] / rate + 1e-9) for item, rate in per_machine.items()))
        if fit < 1:
            item = min(per_machine, key=lambda name: budget[name] / per_machine[name])
            return {
                "ok": False,
                "reason": "green_science_needs_connected_supply_headroom",
                "item": item,
                "needed": per_machine[item],
                "available_nominal": budget[item],
            }
        return {"ok": True, "count": fit}

    def no_allocation(self, world_id):
        """Blocked reason when the master plan has no block for the core."""
        try:
            depth = self.site.plan(world_id).district_depth
        except Exception:  # noqa: BLE001 - the reason is diagnostic only
            depth = None
        from .production_layout import _even_turn

        (x0, y0), (x1, y1) = _even_turn(green_cluster(self.catalog, (0.5, 0.5))["reserved_area"])
        along, needed = int(y1 - y0), int(x1 - x0)
        reason = "green_science_needs_deeper_district" if depth is not None and depth < needed else None
        return {
            "ok": False,
            "reason": reason or "no_green_science_district_block",
            "block_along": along,
            "block_depth": needed,
            "district_depth": depth,
        }

    def repair_lab_join(self, obs):
        """Migrate an early development join that omitted the upstream lab."""
        if not plan_transform(self.science.state["labs"]).identity:
            return None  # only canonical development-map lab rows have the old join
        entry = lab_green_entry(self.science.state["labs"])
        new_x = entry["join"]["x"]
        old = self.state["lab_join"]
        old_x, y = xy(old)
        if old_x == new_x:
            return None
        if new_x - old_x != 4:
            return {"status": "blocked", "reason": "unsupported_lab_join_migration_geometry"}
        obsolete = {(old_x, y + 6), (old_x, y + 5), (old_x, y + 2), (old_x, y + 1)}
        wanted = [e for e in self.state["plan"]["entities"] if xy(e) in obsolete]
        remove = [e for e in obs["entities"] if any(e["name"] == w["name"] and xy(e) == xy(w) for w in wanted)]
        join = next(
            (
                e
                for e in obs["entities"]
                if e["name"] == "transport-belt" and xy(e) == (new_x, y) and e["direction"] == 12
            ),
            None,
        )
        if not join:
            return {"status": "blocked", "reason": "upstream_lab_join_anchor_missing"}
        es = [e for e in self.state["plan"]["entities"] if not (xy(e) in obsolete)]
        es += [entity("transport-belt", old_x + i, y + 6, 4 if i < 4 else 0) for i in range(5)]
        es += [
            dict(entity("underground-belt", new_x, y + 5, 0), belt_to_ground_type="input"),
            dict(entity("underground-belt", new_x, y + 2, 0), belt_to_ground_type="output"),
            entity("transport-belt", new_x, y + 1, 0),
        ]
        check = preflight(self.game, self.builder, obs, es, ignored_units=[e["unit_number"] for e in remove])
        if not check.get("ok"):
            return {"status": "blocked", **check}
        pairs = [p for p in self.state["plan"]["underground_pairs"] if p["input"] != [old_x, y + 5]]
        pairs.append({"input": [new_x, y + 5], "output": [new_x, y + 2], "direction": 0, "span": 3})
        extra = {
            k: v
            for k, v in self.state["plan"].items()
            if k not in ("ok", "reason", "entities", "ports", "required_items", "underground_pairs")
        }
        self.state["plan"] = _plan(es, self.state["plan"]["ports"], **extra, underground_pairs=pairs)
        self.state["lab_join"] = {k: join[k] for k in ("name", "position", "direction", "unit_number")}
        self.state["pending_recovery"] = remove
        self.state["phase"] = "build"
        self.state.pop("lab_lane_evidence", None)
        self.state.pop("commissioned_lab_units", None)
        _atomic_json(self.path, self.state)
        path = self.path.parent / "pcb-board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        board["blocks"]["logistic_science"] = self.state["plan"]
        _atomic_json(path, board)
        return None

    def step(self, obs, parameters):
        validate_rate(parameters)
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            _atomic_json(self.path, self.state)
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "green_science_world_or_catalog_changed"}
        repair = self.repair_lab_join(obs)
        if repair:
            return repair
        for wanted in self.state.get("pending_recovery", []):
            current = next(
                (e for e in obs["entities"] if e["name"] == wanted["name"] and e["position"] == wanted["position"]),
                None,
            )
            if not current:
                continue
            if current["unit_number"] != wanted["unit_number"]:
                return {"status": "blocked", "reason": "green_lab_join_recovery_identity_changed"}
            return {
                "type": "mine",
                "name": current["name"],
                "position": current["position"],
                "count": 1,
                "expected_entity_unit": current["unit_number"],
                "expected_entity_world_id": obs["world_id"],
                "reason": "normally recover only the obsolete green lab entry hardware",
            }
        if self.state.pop("pending_recovery", None) is not None:
            _atomic_json(self.path, self.state)
        if parameters["rate_per_minute"] > self.state["requested_rate"]:
            return {"status": "blocked", "reason": "green_science_expansion_requires_new_row"}
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "green_science_board_world_changed"}
        if not self.state.get("published"):
            if self.state["remove"]:  # district splitter branch (fallback without a planned bus)
                self.smelting.state["plan"] = self.state["source_plan"]
                _atomic_json(self.smelting.path, self.smelting.state)
                board["blocks"]["smelting_district"] = self.state["source_plan"]
            board["blocks"]["logistic_science"] = self.state["plan"]
            _atomic_json(board_path, board)
            self.state["published"] = True
            _atomic_json(self.path, self.state)
        if self.state["phase"] == "recover":
            for wanted in self.state["remove"]:
                current = next(
                    (e for e in obs["entities"] if e["name"] == wanted["name"] and e["position"] == wanted["position"]),
                    None,
                )
                if not current:
                    continue
                if current["unit_number"] != wanted["unit_number"]:
                    return {"status": "blocked", "reason": "green_plate_branch_recovery_identity_changed"}
                return {
                    "type": "mine",
                    "name": current["name"],
                    "position": current["position"],
                    "count": 1,
                    "expected_entity_unit": current["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "normally recover one owned plate belt for a paid shared-consumer splitter",
                }
            self.state["phase"] = "build"
            _atomic_json(self.path, self.state)
        taps = self.state.get("taps", [])
        blocked = planned_tap.publish(self.path.parent, obs["world_id"], taps)
        if blocked:
            return blocked
        clear = self.state["plan"].get("clear_obstacles", [])
        cut = planned_tap.cutover(self.path.parent, self.builder, obs, taps, clear_obstacles=clear)
        if cut is not None:
            return cut
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if not proof:
            return {
                "status": "waiting",
                "reason": "waiting for native green intermediates, every science machine and automatic lab delivery",
            }
        self.state["phase"] = "verified"
        if isinstance(self.state.get("layout"), dict) and self.state["layout"].get("verified_tick") is None:
            self.state["layout"]["verified_tick"] = obs.get("tick")
        _atomic_json(self.path, self.state)
        return {
            "status": "succeeded",
            "reason": "normally belt-fed green science reaches the free lab belt lane",
            "evidence": proof,
        }

    def plan_growth(self, obs, parameters):
        """The grown plan for ``parameters["machines"]`` pack machines, without writing anything (dry run).

        ``{"ok": True, "plan", "added", "machines", "built"}`` (``added`` empty when nothing is to build),
        or a blocked result with ``plan_needs`` (no slot left: the next unit; a slot meets something:
        the preflight blocker).
        """
        from . import plan_needs
        from .pack_growth import block_view

        machines = parameters.get("machines")
        if parameters.get("pack") != "logistic-science-pack" or type(machines) is not int:
            return {"ok": False, "status": "blocked", "reason": "green_growth_parameters_invalid"}
        if not self.state or self.state.get("world_id") != obs["world_id"]:
            return {"ok": False, "status": "blocked", "reason": "green_science_world_or_catalog_changed"}
        if self.state.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"ok": False, "status": "blocked", "reason": "green_science_world_or_catalog_changed"}
        if self.state.get("phase") != "verified":
            return {"ok": False, "status": "blocked", "reason": "green_growth_needs_verified_block"}
        view = block_view(self.state, "logistic-science-pack")
        if view is None or machines > view["frame"]:
            form = (view or {}).get("form")
            return {
                "ok": False,
                "status": "blocked",
                "reason": "green_growth_has_no_reserved_slot",
                "form": form,
                "frame": (view or {}).get("frame"),
                "plan_needs": plan_needs.make(
                    "site",
                    "green_growth_has_no_reserved_slot",
                    block="logistic_science",
                    change="a second green unit or the rebuild cluster -> split row (pack_growth next_step)",
                ),
            }
        added = [dict(e) for group in view["slots"][: max(0, machines - view["machines"])] for e in group]
        if not added:
            return {
                "ok": True,
                "plan": self.state["plan"],
                "added": [],
                "machines": view["machines"],
                "built": view["machines"],
            }
        clearance = preflight(self.game, self.builder, obs, added)
        if not clearance.get("ok"):
            return {
                "ok": False,
                "status": "blocked",
                "reason": "green_growth_slot_preflight_failed",
                "detail": clearance.get("reason"),
                **{k: clearance.get(k) for k in ("failed_check", "blocked", "first_blocker") if clearance.get(k)},
            }
        plan = self.state["plan"]
        obstacles = {(e["name"], *xy(e)): e for e in plan.get("clear_obstacles", [])}
        obstacles.update({(e["name"], *xy(e)): e for e in clearance.get("clear_obstacles", [])})
        extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
        extra["clear_obstacles"] = list(obstacles.values())
        grown = _plan(merge_entities(plan["entities"], added), plan["ports"], **extra)
        return {"ok": True, "plan": grown, "added": added, "machines": machines, "built": view["machines"]}

    def grow(self, obs, parameters):
        """Build reserved pack slots of the split row up to ``parameters["machines"]`` (``science_growth``
        with a ``pack``; decided by ``pack_growth.growth_decision``).

        The grown plan (``plan_growth``: planned entities + the slot groups, slots in order so poles
        stay one chain) is saved first, then the board block republished (only over this block's own
        previous plan), then built; the block's ``core`` commits the new count once every new entity
        stands powered. Never shrinks; a cluster or a full frame has no slot (``pack_growth`` names the
        next step).
        """
        from .science import _built
        from .science_growth import _fingerprint

        pending = (self.state or {}).get("growth")
        if not pending or pending.get("machines") != parameters.get("machines"):
            planned = self.plan_growth(obs, parameters)
            if not planned.get("ok"):
                return {k: v for k, v in planned.items() if k != "ok"}
            if not planned["added"]:
                return {
                    "status": "succeeded",
                    "reason": "reserved green pack slots built",
                    "evidence": self.growth_proof(obs),
                }
            replaces = list((pending or {}).get("replaces") or []) + [_fingerprint(self.state["plan"])]
            if pending:  # an earlier target's plan may already stand on the board
                replaces.append(_fingerprint(pending["plan"]))
            self.state["growth"] = {
                "machines": planned["machines"],
                "built": planned["built"],
                "plan": planned["plan"],
                "added": planned["added"],
                "replaces": replaces,
            }
            _atomic_json(self.path, self.state)  # saved before the board: a restart republishes it
            pending = self.state["growth"]
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "green_science_board_world_changed"}
        old = board["blocks"].get("logistic_science")
        if old != pending["plan"]:
            if old is not None and old != self.state["plan"] and _fingerprint(old) not in pending["replaces"]:
                return {"status": "blocked", "reason": "green_reserved_block_changed"}
            board["blocks"]["logistic_science"] = pending["plan"]
            _atomic_json(board_path, board)
        result = self.builder.ensure_plan(obs, pending["plan"])
        if result.get("status") != "succeeded":
            return result
        if _built(obs, pending["plan"]) is None:
            return {"status": "waiting", "reason": "waiting for grown green slots to be observed and powered"}
        core = dict(self.state["core"])
        built, machines = pending["built"], pending["machines"]
        core["entities"] = merge_entities(core.get("entities") or [], pending["added"])
        core["growth_slots"] = list(core.get("growth_slots") or [])[machines - built :]
        core["count"] = machines
        each = float(core.get("rated_per_minute") or 0.0) / max(1, built)
        core["rated_per_minute"] = round(each * machines, 3)
        self.state.update(plan=pending["plan"], core=core, nominal_machine_rate=core["rated_per_minute"])
        # Grown slots count for the supply proof once built and powered (a first craft may wait for plates).
        grown_keys = {tuple(k) for k in self.state.get("grown_machines") or ()}
        grown_keys |= {(e["name"], *xy(e)) for e in pending["added"] if e.get("recipe")}
        self.state["grown_machines"] = sorted(list(k) for k in grown_keys)
        self.state.pop("growth", None)
        _atomic_json(self.path, self.state)
        return {"status": "succeeded", "reason": "reserved green pack slots built", "evidence": self.growth_proof(obs)}

    def growth_proof(self, obs, parameters=None):
        """Every planned entity stands powered and the block holds ``parameters["machines"]`` pack machines."""
        from .pack_growth import block_view
        from .science import _built

        if not self.state or self.state.get("world_id") != obs["world_id"] or self.state.get("growth"):
            return None
        view = block_view(self.state, "logistic-science-pack")
        if view is None or (parameters and view["machines"] < int(parameters.get("machines") or 0)):
            return None
        if _built(obs, self.state["plan"]) is None:
            return None
        return {
            "tick": obs.get("tick"),
            "pack": "logistic-science-pack",
            "machines": view["machines"],
            "free_slots": len(view["slots"]),
            "rated_per_minute": self.state["core"].get("rated_per_minute"),
        }

    def proof(self, obs):
        """Commissioned green supply evidence, or None with ``last_proof_failure`` naming the check."""
        self.last_proof_failure = None
        found = self._proof(obs)
        if found is None and self.last_proof_failure is None:
            self.last_proof_failure = {"reason": "green_proof_failed"}
        return found

    def _fail(self, reason, **evidence):
        self.last_proof_failure = {"reason": reason, **evidence}
        return None

    def _proof(self, obs):
        if not self.state or self.state["world_id"] != obs["world_id"]:
            return self._fail("green_state_missing_or_other_world")
        if not underground_proof(obs, self.state["plan"]):
            return self._fail("green_underground_pair_changed")
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        # A trunk belt of this block's own bus lane extension that a later tap replaced is that tap's
        # (planned_tap.released_tiles); it is not missing.
        released = planned_tap.released_tiles(self.path.parent, obs["world_id"])
        machines = []
        grown = {tuple(k) for k in self.state.get("grown_machines") or ()}
        for wanted in self.state["plan"]["entities"]:
            e = actual.get((wanted["name"], *xy(wanted)))
            if not e and planned_tap.released_belt_stands(actual, released, wanted):
                continue
            if not e or e["direction"] != wanted.get("direction", 0):
                return self._fail("green_planned_entity_missing", entity=wanted["name"], position=wanted["position"])
            if wanted.get("recipe"):
                first = 0 if (wanted["name"], *xy(wanted)) in grown else 1
                if e.get("recipe") != wanted["recipe"] or e.get("products_finished", 0) < first:
                    return self._fail("green_machine_not_producing", position=wanted["position"])
                machines.append(e)
            if e["name"] in ("assembling-machine-1", "inserter", "fast-inserter") and (
                not e.get("electric_network_connected") or e.get("energy", 0) <= 0
            ):
                return self._fail("green_hardware_unpowered", entity=e["name"], position=wanted["position"])
        join = self.state["lab_join"]
        live = actual.get((join["name"], *xy(join)))
        if not live or live["unit_number"] != join["unit_number"] or live["direction"] != join["direction"]:
            return self._fail("green_lab_join_changed")
        # Only the built stage of a staged lab row (``science_growth.built_labs``); reserved slots come later.
        labs = [actual.get(("lab", *xy(e))) for e in built_labs(self.science.state)]
        if any(not e or not e.get("electric_network_connected") or e.get("energy", 0) <= 0 for e in labs):
            return self._fail("labs_unpowered")
        green_stock = sum(e.get("inventory", {}).get("logistic-science-pack", 0) for e in labs)
        if not green_stock:
            return self._fail("labs_without_green_packs", **starved_inputs(machines))
        lab_units = [e["unit_number"] for e in labs]
        if all(e.get("inventory", {}).get("logistic-science-pack", 0) > 0 for e in labs):
            self.state["commissioned_lab_units"] = lab_units
        if self.state.get("commissioned_lab_units") != lab_units:
            return self._fail("labs_changed_since_commissioning")
        turn = plan_transform(self.science.state["labs"])
        heading = turn.direction(12)
        belts = [e for e in self.science.state["labs"]["entities"] if e["name"] == "transport-belt"]
        # Green side-loads onto the lab belt's left lane in the canonical
        # (westbound) frame; a mirrored lab row carries it on the right lane.
        green_lane, red_lane = ("right", "left") if turn.mirror else ("left", "right")
        payload = json.dumps(json.dumps({"join": join, "belts": belts, "heading": heading}, separators=(",", ":")))
        lane = self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);local spec=args.join;local e=target(spec.position,spec.name)
if not e or e.unit_number~=spec.unit_number or e.direction~=spec.direction then return {ok=false} end
local result={ok=true,tick=game.tick,left={},right={}}
for _,b in ipairs(args.belts) do
 local belt=target(b.position,b.name);if not belt or belt.direction~=args.heading then return {ok=false} end
 for _,row in ipairs({{"left",defines.transport_line.left_line},{"right",defines.transport_line.right_line}}) do
  for _,v in pairs(belt.get_transport_line(row[2]).get_contents()) do result[row[1]][v.name]=(result[row[1]][v.name] or 0)+v.count end
 end
end
return result
"""
        )
        if not lane.get("ok") or not isinstance(lane.get(green_lane), dict):
            return self._fail("lab_lane_query_failed")
        mixed = set(lane[green_lane]) - {"logistic-science-pack"}
        if mixed or set(lane.get(red_lane) or {}) - {"automation-science-pack"}:
            return self._fail(
                "lab_lane_mixed", items=sorted(mixed | (set(lane.get(red_lane) or {}) - {"automation-science-pack"}))
            )
        if lane[green_lane].get("logistic-science-pack", 0) > 0:
            self.state["lab_lane_evidence"] = dict(lane, receiver_unit=join["unit_number"])
            _atomic_json(self.path, self.state)
        recent = self.state.get("lab_lane_evidence")
        if (
            not recent
            or recent["receiver_unit"] != join["unit_number"]
            or not 0 <= lane["tick"] - recent["tick"] <= 600
        ):
            return self._fail("no_green_on_the_lab_lane", **starved_inputs(machines))
        greens = [e for e in machines if e.get("recipe") == "logistic-science-pack"]
        return {
            "tick": obs["tick"],
            "science_units": [e["unit_number"] for e in greens],
            "machine_products_finished": [e["products_finished"] for e in greens],
            "intermediate_units": [
                {"unit": e["unit_number"], "recipe": e["recipe"], "products_finished": e["products_finished"]}
                for e in machines
                if e not in greens
            ],
            "lab_units": [e["unit_number"] for e in labs],
            "lab_green_stock": green_stock,
            "native_lab_lane_join": lane,
            "recent_green_lane_evidence": recent,
            "requested_rate_per_minute": self.state["requested_rate"],
            "nominal_machine_rate_per_minute": self.state["nominal_machine_rate"],
            "rated_raw_demand": self.state["rated_demand"]["raw_items_per_minute"],
            "scope": "native automatic production and lab commissioning; throughput limited by actual raw supply/power and requires a separate loaded window",
        }
