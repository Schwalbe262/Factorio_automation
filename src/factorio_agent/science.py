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
from .plate_supply import plate_sources


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
        # Plates come from the smelting district's registered output ports.
        supply = plate_sources(root, board, obs["world_id"], self.catalog.fingerprint, owner="automation_science")
        if not supply.get("ok"):
            return supply
        source_ports = supply["ports"]
        if not all(obs.get("enabled_recipes", {}).get(name) for name in ("automation-science-pack", "iron-gear-wheel")):
            return {"ok": False, "reason": "science_machine_recipes_not_unlocked"}
        speed = self.catalog.entities["assembling-machine-1"]["crafting_speed"]
        nominal_each = speed * 60 / self.catalog.recipes["automation-science-pack"]["energy"]
        count = math.ceil(parameters["rate_per_minute"] * 1.15 / nominal_each)
        if count > 6:
            return {"ok": False, "reason": "red_science_row_exceeds_six_slots"}
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
        available = {}
        for item in per_machine:
            bank = next(b for b in smelting["banks"] if b["product"] == item)
            ore = sum(
                b["nominal_rate_per_minute"]
                for b in mining["banks"]
                if b.get("ready") and b["plan"]["resource"] == bank["resource"]
            )
            plates = (
                bank["count"]
                * self.catalog.entities["stone-furnace"]["crafting_speed"]
                * 60
                / self.catalog.recipes[item]["energy"]
            )
            available[item] = min(ore, plates)
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
            blocks = planned_science_candidates(self.site, obs["world_id"], count)
        else:
            blocks = legacy_science_candidates(count)
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
            connections = [(source_ports[p["item"]], p) for p in science["ports"] if p["direction"] == "input"]
            connections += [(science["ports"][-1], labs["ports"][0])]
            good = True
            for source, destination in connections:
                route = belt_connection(self.game, self.builder, obs, source, destination, es)
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
            final = preflight(self.game, self.builder, obs, es)
            if not final.get("ok"):
                failures.append(final)
                continue
            obstacles.update({(e["name"], *xy(e)): e for e in final["clear_obstacles"]})
            pairs = [pair for route in routes for pair in route["underground_pairs"]]
            plan = _plan(
                es,
                [science["ports"][-1]],
                underground_pairs=pairs,
                clear_obstacles=list(obstacles.values()),
                clearance_world_id=obs["world_id"],
                layout_policy="pcb-permanent-science-v1",
            )
            settle(self.site, obs["world_id"], "automation_science", site.get("allocation"))
            return {
                "ok": True,
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
            }
        settle(self.site, obs["world_id"], "automation_science", None)
        return {
            "ok": False,
            "reason": "no_reserved_science_and_lab_slots_with_clean_connections",
            "failures": failures,
            "legal_sites": len(survey.get("legal", [])),
        }

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
        changed = False
        for key, plan in (("automation_science", self.state["science"]), ("laboratories", self.state["labs"])):
            if board["blocks"].get(key) != plan:
                if key in board["blocks"]:
                    return {"status": "blocked", "reason": "science_reserved_block_changed"}
                board["blocks"][key] = plan
                changed = True
        for connection in self.state["connections"]:
            if connection not in board.setdefault("connections", []):
                board["connections"].append(connection)
                changed = True
        if changed:
            _atomic_json(path, board)
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if proof:
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
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for wanted in self.state["plan"]["entities"]:
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
        machines = [
            actual[(e["name"], *xy(e))]
            for e in self.state["science"]["entities"]
            if e.get("recipe") == "automation-science-pack"
        ]
        if any(e.get("products_finished", 0) < 1 for e in machines):
            return None
        labs = [actual[(e["name"], *xy(e))] for e in self.state["labs"]["entities"] if e["name"] == "lab"]
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
            "scope": "normal continuous raw-belt supply and machine science into labs; rated capacity is not sustained measured science consumption",
        }
