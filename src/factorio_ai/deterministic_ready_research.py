"""Start useful idle research from existing automatic science, without building."""
from __future__ import annotations

import math

from .deterministic_builder import plan_observed
from .deterministic_input_links import _geometry, _path
from .deterministic_machine_ports import cell_capacity
from .factory_templates import DIRECTIONS


def _positive(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _fast_needed(factory, obs: dict, roadmap: dict) -> bool:
    if obs.get("enabled_recipes", {}).get("fast-inserter"):
        return False
    packs = factory.state.get("capacity_science") or ["automation-science-pack"]
    packs = [item for item in packs if item in roadmap["bom"]["science_packs"]]
    cycles, _ = factory.graph._continuous_rates(packs)
    for name, cycles_per_minute in cycles.items():
        recipe = factory.catalog.recipes[name]
        plan = factory.state["blocks"].get("recipe:" + name)
        if not plan or not plan_observed(obs, plan) or any(p.get("type") == "fluid" for p in recipe["ingredients"]):
            continue
        for product in recipe["products"]:
            item = product["name"]
            rate = cycles_per_minute * product["amount"]
            fast = cell_capacity(factory, obs, plan, recipe, item, prefer_fast=True)
            ordinary = cell_capacity(factory, obs, plan, recipe, item)
            if rate / max(1, math.ceil(rate / fast)) > ordinary + 1e-9:
                return True
    return False


def ready_research(factory, obs: dict) -> dict | None:
    """Use only this observation; persist only the priority of an emitted action."""
    state, catalog = factory.state, factory.catalog
    if (obs.get("ok") is not True or obs.get("research") is not None or type(obs.get("enemies")) is not int or obs["enemies"] != 0
            or not obs.get("world_id") or obs["world_id"] != state.get("world_id")
            or state.get("catalog_fingerprint") != catalog.fingerprint or state.get("schema_version") != 1
            or type(obs.get("tick")) is not int or obs["tick"] <= 0
            or type(state.get("last_tick", 0)) is not int or obs["tick"] < state.get("last_tick", 0)):
        return None
    try:
        # Observation entities are force-filtered by the adapter; reject any
        # explicit contradictory force and require exact, unique live identities.
        entities = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs["entities"]}
        if len(entities) != len(obs["entities"]):
            return None
        def live(e):
            return entities[(e["name"], e["position"]["x"], e["position"]["y"])]
        def powered(e):
            return _positive(e.get("energy")) and e.get("electric_network_connected") is True
        def observed(plan):
            return plan_observed(obs, plan) and all(
                type(live(e).get("unit_number")) is int and live(e)["unit_number"] > 0
                and live(e).get("force", "player") == "player"
                and ("inserter" not in e["name"] or powered(live(e))) for e in plan["entities"])

        lab_plan = state["blocks"]["research:labs"]
        labs = [live(e) for e in lab_plan["entities"] if e["name"] == "lab"]
        if len(labs) != 1 or not observed(lab_plan) or not powered(labs[0]):
            return None
        lab = labs[0]
        migration = state.get("lab_migration")
        if migration and (migration.get("state") != "retired" or migration.get("world_id") != obs["world_id"]
                or migration.get("replacement_unit_number") != lab["unit_number"]
                or any(e.get("unit_number") == migration.get("old_unit_number") for e in obs["entities"])):
            return None

        def science_ready(ingredient):
            item, amount = ingredient["name"], ingredient["amount"]
            stock = lab["inventory"].get(item, 0)
            if (ingredient.get("type", "item") != "item" or not _positive(amount)
                    or not _positive(stock) or stock < amount):
                return False
            recipe = catalog.recipe_for_product(item)
            producer = state["blocks"].get("recipe:" + recipe["name"], {})
            link = state["links"].get("lab:" + item, {})
            if not observed(producer) or not observed(link) or link.get("upstream_tap") or link.get("consumer_entry"):
                return False
            machines = [live(e) for e in producer["entities"] if e.get("recipe") == recipe["name"]]
            source, consumer = link["source_port"], link["consumer_port"]
            if (len(machines) != 1 or not powered(machines[0]) or not _positive(machines[0].get("products_finished"))
                    or source not in producer["ports"] or consumer not in lab_plan["ports"]
                    or any(p.get("kind") != "item" or p.get("item") != item for p in (source, consumer))
                    or source.get("direction") != "output" or consumer.get("direction") != "input"):
                return False
            ends = []
            for port, sign, machine in ((source, -1, machines[0]), (consumer, 1, lab)):
                dx, dy = DIRECTIONS[port["facing"]]
                x, y = port["position"]["x"], port["position"]["y"]
                arms = [live(e) for plan in (producer, lab_plan) for e in plan["entities"]
                        if "inserter" in e["name"] and e["position"] == {"x": x + sign * 2 * dx, "y": y + sign * 2 * dy}]
                if (len(arms) != 1 or arms[0]["name"] not in {"inserter", "fast-inserter"}
                        or arms[0]["direction"] != (port["facing"] + 8) % 16
                        or max(abs(x + sign * 3 * dx - machine["position"]["x"]),
                               abs(y + sign * 3 * dy - machine["position"]["y"])) > 1):
                    return False
                ends.append((x + sign * dx, y + sign * dy))
            route = {"entities": producer["entities"] + link["entities"] + lab_plan["entities"]}
            belts, edges, _ = _geometry(route)
            path = _path(belts, edges, *ends)
            checked = (path or []) + link["entities"]
            return bool(path) and all(isinstance(live(e).get("belt_inventory"), dict)
                and all(type(count) in (int, float) and math.isfinite(count) and count >= 0
                        and (name == item or count == 0) for name, count in live(e)["belt_inventory"].items())
                for e in checked if e["name"] == "transport-belt")

        roadmap = factory.graph.for_first_rocket()
        allowed = list(roadmap["technology_order"])
        if _fast_needed(factory, obs, roadmap):
            allowed += sorted(name for name, tech in catalog.technologies.items() if "fast-inserter" in tech.get("unlocks", []))
        done = obs["technologies"]
        if not isinstance(done, dict) or any(type(value) is not bool for value in done.values()):
            return None
        priorities = factory.priority_research + state.get("capability_research", [])
        if any(name not in allowed and not done.get(name) for name in priorities):
            return None
        for name in dict.fromkeys(priorities + allowed):
            tech = catalog.technologies[name]
            if (done.get(name) or not tech.get("enabled", True) or tech.get("research_trigger")
                    or not all(done.get(p) for p in tech["prerequisites"])
                    or not _positive(tech.get("unit_count")) or not _positive(tech.get("unit_energy"))
                    or not tech.get("ingredients") or not all(science_ready(i) for i in tech["ingredients"])):
                continue
            pending = state.setdefault("capability_research", [])
            if name not in pending:
                pending.append(name)
                factory._save()
            return {"type": "research", "technology": name, "reason": "start ready automatic science while construction continues"}
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return None
