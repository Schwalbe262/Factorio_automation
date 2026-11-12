"""Narrow, resumable cutover of the owned additive-steam coal corridor."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .bus_factory import identity, trace_corridor
from .planning import preflight
from .resource_policy import mark_mining_core
from .smelting import merge_entities
from .transport import VECTORS, plan_connection, underground_proof, xy


def _pairs(plan):
    return {(tuple(p["input"]), tuple(p["output"]), p["direction"]) for p in plan.get("underground_pairs", [])}


def _signature(plan):
    return sorted([list(identity(e)) for e in plan["entities"]])


def _bound(spec, live):
    return {k: live[k] for k in ("name", "position", "direction", "unit_number")} | (
        {"belt_to_ground_type": live["belt_to_ground_type"]} if "belt_to_ground_type" in live else {}
    )


def _near_mouth(a, b):
    return (
        a["name"] == b["name"] == "underground-belt"
        and a["direction"] == b["direction"]
        and ((xy(a)[0] == xy(b)[0]) if a["direction"] % 8 == 0 else (xy(a)[1] == xy(b)[1]))
        and abs(xy(a)[0] - xy(b)[0]) + abs(xy(a)[1] - xy(b)[1]) <= 5
    )


def _pair_geometry(plan):
    mouths = [e for e in plan["entities"] if e["name"] == "underground-belt"]
    for first, last, direction in _pairs(plan):
        vx, vy = VECTORS[direction]
        span = (last[0] - first[0]) * vx + (last[1] - first[1]) * vy
        if not 2 <= span <= 5 or last != (first[0] + span * vx, first[1] + span * vy):
            return False
        for e in mouths:
            p = xy(e)
            if p in (first, last) or e["direction"] != direction:
                continue
            ahead = (p[0] - first[0]) * vx + (p[1] - first[1]) * vy
            if 0 < ahead < span and (p[0] - first[0]) * vy == (p[1] - first[1]) * vx:
                return False
    return True


def partition_routes(old_route, new_route, builder):
    """Retain complete native pairs; defer every old/new cutover interaction."""
    old, new = old_route["entities"], new_route["entities"]
    old_by_id, new_by_id = {identity(e): e for e in old}, {identity(e): e for e in new}
    retained = set(old_by_id) & set(new_by_id)
    shared_pairs = _pairs(old_route) & _pairs(new_route)
    for plan in (old_route, new_route):
        for first, last, direction in _pairs(plan) - shared_pairs:
            for spec in plan["entities"]:
                if spec["name"] == "underground-belt" and xy(spec) in (first, last):
                    retained.discard(identity(spec))
    added = [e for e in new if identity(e) not in retained]
    changed = set()
    for old in old_route["entities"]:
        if identity(old) in retained:
            continue
        box = builder._occupied_by_plan([old])
        if any(box & builder._occupied_by_plan([e]) or _near_mouth(old, e) for e in added):
            changed.add(identity(old))
    # Removing only half an old pair could steal a new mouth or leave a live
    # endpoint with a changed neighbour. Couple the complete old pair.
    for first, last, direction in _pairs(old_route) - shared_pairs:
        members = [e for e in old_route["entities"] if e["name"] == "underground-belt" and xy(e) in (first, last)]
        if any(identity(e) in changed for e in members):
            changed.update(identity(e) for e in members)
    conflicts = [e for e in old_route["entities"] if identity(e) in changed]
    old_positions = {xy(e): e for e in old_route["entities"]}
    new_positions = {xy(e): e for e in new_route["entities"]}
    deferred = []
    for spec in added:
        box = builder._occupied_by_plan([spec])
        unsafe = any(box & builder._occupied_by_plan([e]) or _near_mouth(e, spec) for e in conflicts)
        for old in old_route["entities"]:
            if old.get("belt_to_ground_type") == "input":
                continue
            vx, vy = VECTORS[old["direction"]]
            if (xy(old)[0] + vx, xy(old)[1] + vy) == xy(spec) and identity(old) not in retained:
                unsafe = True
        if spec.get("belt_to_ground_type") != "input":
            vx, vy = VECTORS[spec["direction"]]
            recipient = xy(spec)[0] + vx, xy(spec)[1] + vy
            if recipient in old_positions and (
                recipient not in new_positions or identity(old_positions[recipient]) not in retained
            ):
                unsafe = True
        if unsafe:
            deferred.append(spec)
    deferred_ids = {identity(e) for e in deferred}
    return {
        "retained": [e for e in old_route["entities"] if identity(e) in retained],
        "conflicts": conflicts,
        "prebuild": [e for e in added if identity(e) not in deferred_ids],
        "deferred": list(reversed(deferred)),
        "obsolete": [e for e in old_route["entities"] if identity(e) not in retained and identity(e) not in changed],
        "added": added,
    }


def _replace_route(plan, old_route, new_route):
    removed = {identity(e) for e in old_route["entities"]}
    entities = merge_entities([e for e in plan["entities"] if identity(e) not in removed], new_route["entities"])
    removed_pairs = _pairs(old_route)
    pairs = [
        p
        for p in plan.get("underground_pairs", [])
        if (tuple(p["input"]), tuple(p["output"]), p["direction"]) not in removed_pairs
    ]
    pairs += new_route.get("underground_pairs", [])
    extra = {
        k: v
        for k, v in plan.items()
        if k not in ("ok", "reason", "entities", "ports", "required_items", "underground_pairs", "clear_obstacles")
    }
    return _plan(
        entities, plan["ports"], **extra, underground_pairs=pairs, clear_obstacles=new_route.get("clear_obstacles", [])
    )


class SteamCoalReroute:
    def __init__(self, game, builder, catalog, steam, outposts):
        self.game, self.builder, self.catalog, self.steam, self.outposts = game, builder, catalog, steam, outposts
        self.path = Path(game.cfg.runtime_dir) / "steam-coal-reroute.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _load(self, component):
        path = Path(component.path)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else deepcopy(component.state)

    def _save(self):
        _atomic_json(self.path, self.state)

    def _canonical(self, saved, source):
        p = xy(source["plan"]["ports"][0])
        drills = {xy(e) for e in source["plan"]["entities"] if e["name"] == "electric-mining-drill"}
        return [
            b
            for b in saved.get("banks", [])
            if b["plan"].get("resource") == "coal"
            and xy(b["plan"]["ports"][0]) == p
            and {xy(e) for e in b["plan"]["entities"] if e["name"] == "electric-mining-drill"} == drills
        ]

    def prepare(self, obs, parameters=None):
        if parameters not in (None, {}):
            return {"ok": False, "reason": "steam_coal_reroute_takes_no_parameters"}
        steam, saved = self._load(self.steam), self._load(self.outposts)
        if (
            steam.get("world_id") != obs["world_id"]
            or steam.get("catalog_fingerprint") != self.catalog.fingerprint
            or saved.get("world_id") != obs["world_id"]
            or saved.get("catalog_fingerprint", self.catalog.fingerprint) != self.catalog.fingerprint
        ):
            return {"ok": False, "reason": "steam_reroute_source_identity_changed"}
        canonical = self._canonical(saved, steam["bank"])
        if len(canonical) != 1 or _signature(canonical[0]["plan"]) != _signature(steam["bank"]["plan"]):
            return {"ok": False, "reason": "canonical_power_coal_bank_missing_or_changed"}
        bank = deepcopy(steam["bank"])
        source, destination = deepcopy(bank["plan"]["ports"][0]), deepcopy(steam["core"]["ports"][0])
        if source["item"] != "coal" or source["facing"] != 4 or destination["item"] != "coal":
            return {"ok": False, "reason": "unsupported_steam_coal_port_geometry"}
        trace = trace_corridor(steam["plan"], source, destination)
        if not trace.get("ok"):
            return dict(trace, stage="trace_exact_old_steam_coal")
        traced_positions = {xy(e) for e in trace["entities"]}
        old_pairs = [
            p
            for p in steam["plan"].get("underground_pairs", [])
            if tuple(p["input"]) in traced_positions and tuple(p["output"]) in traced_positions
        ]
        old_route = {"entities": trace["entities"], "underground_pairs": old_pairs}
        if not underground_proof(obs, old_route):
            return {"ok": False, "reason": "old_steam_coal_underground_pair_not_verified"}
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        bindings = {}
        for spec in steam["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            direction_ok = live and (
                live.get("direction", 0) % 8 == spec.get("direction", 0) % 8
                if spec["name"] == "steam-engine"
                else live.get("direction", 0) == spec.get("direction", 0)
            )
            if (
                not live
                or not direction_ok
                or live.get("belt_to_ground_type") != spec.get("belt_to_ground_type")
                or type(live.get("unit_number")) is not int
            ):
                return {"ok": False, "reason": "old_steam_asset_missing_or_changed", "position": spec["position"]}
            bindings[identity(spec)] = _bound(spec, live)
        # Only the owned drill rows, their collector and their local poles get
        # a mining scope. The power bridge and old long transport never do.
        drills = [e for e in bank["plan"]["entities"] if e["name"] == "electric-mining-drill"]
        drill_positions = {xy(e) for e in drills}
        pole_positions = {(x + 2, y) for x, y in drill_positions}
        if not drills or len(drills) % 2:
            return {"ok": False, "reason": "owned_paired_coal_core_required"}
        top_y = min(y for _, y in drill_positions)
        top = {xy(e)[0] for e in drills if xy(e)[1] == top_y and e["direction"] == 8}
        bottom = {xy(e)[0] for e in drills if xy(e)[1] == top_y + 4 and e["direction"] == 0}
        if not top or top != bottom or len(top) + len(bottom) != len(drills) or xy(source) != (max(top) + 2, top_y + 2):
            return {"ok": False, "reason": "steam_coal_source_is_not_direct_owned_paired_collector"}
        left_x = min(x for x, _ in drill_positions) - 1
        core = [
            e
            for e in bank["plan"]["entities"]
            if (e["name"] == "electric-mining-drill" and xy(e) in drill_positions)
            or (e["name"] == "small-electric-pole" and xy(e) in pole_positions)
            or (e["name"] == "transport-belt" and xy(e)[1] == xy(source)[1] and left_x <= xy(e)[0] <= xy(source)[0])
        ]
        footprint = mark_mining_core(core, "coal")
        annotated = {identity(e): e for e in core}
        fixed = [
            deepcopy(annotated.get(identity(e), e)) for e in steam["plan"]["entities"] if xy(e) not in traced_positions
        ]
        source["mining_resource"] = "coal"
        source["unit_number"] = bindings[identity(trace["entities"][0])]["unit_number"]
        source["world_id"] = obs["world_id"]
        # The collector remains live, owned and east-facing throughout cutover.
        # It is a source anchor, not an asset hypothetically removed for routing.
        owned_route_units = {bindings[identity(e)]["unit_number"] for e in trace["entities"]}
        ignored = sorted(owned_route_units - {source["unit_number"]})
        virtual = dict(obs, entities=[e for e in obs["entities"] if e.get("unit_number") not in set(ignored)])
        adapter = SimpleNamespace(
            _occupied_by_plan=self.builder._occupied_by_plan,
            can_place=lambda es: preflight(self.game, self.builder, obs, es, ignored_units=ignored),
        )
        route = plan_connection(self.game, adapter, virtual, source, destination, fixed, ignored_units=ignored)
        if not route.get("ok"):
            return dict(route, stage="bounded_resource_avoiding_coal_route")
        metrics = route.get("route_metrics", {})
        if (
            metrics.get("turns", 7) > 6
            or metrics.get("length", float("inf")) > 1.35 * metrics.get("manhattan_distance", 0) + 4
        ):
            return {"ok": False, "reason": "coal_reroute_exceeds_existing_corridor_quality_gate", "metrics": metrics}
        if not _pair_geometry(route):
            return {"ok": False, "reason": "alternative_coal_underground_pairs_can_steal_neighbours"}
        connected = trace_corridor(route, source, destination)
        if (
            not connected.get("ok")
            or len(connected["entities"]) != len(route["entities"])
            or connected["entities"][0]["direction"] != source["facing"]
            or connected["entities"][-1]["direction"] != destination["facing"]
        ):
            return {"ok": False, "reason": "alternative_coal_route_is_not_one_complete_typed_corridor"}
        for spec in route["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            if live and live["unit_number"] not in owned_route_units:
                return {
                    "ok": False,
                    "reason": "alternative_coal_route_would_claim_foreign_existing_asset",
                    "position": spec["position"],
                }
        portions = partition_routes(old_route, route, self.builder)
        if len(portions["conflicts"]) > 8:
            return {
                "ok": False,
                "reason": "coal_reroute_changes_more_than_eight_owned_conflicts",
                "conflicts": len(portions["conflicts"]),
            }
        # A scoped exit must be validated as one complete ordered prefix.
        # Keep its new on-patch belts for the paid cutover, rather than trying
        # to prebuild a fragment while the old collector corner is still live.
        exit_ids = {identity(e) for e in portions["prebuild"] if e.get("mining_exit")}
        if exit_ids:
            portions["prebuild"] = [e for e in portions["prebuild"] if identity(e) not in exit_ids]
            deferred_ids = {identity(e) for e in portions["deferred"]} | exit_ids
            portions["deferred"] = [e for e in reversed(route["entities"]) if identity(e) in deferred_ids]
        if not portions["added"]:
            return {"ok": False, "reason": "coal_reroute_has_no_added_path_to_prove"}
        final = _replace_route(steam["plan"], old_route, route)
        final["entities"] = [deepcopy(annotated.get(identity(e), e)) for e in final["entities"]]
        bank_plan = _replace_route(bank["plan"], old_route, route)
        bank_plan["entities"] = [deepcopy(annotated.get(identity(e), e)) for e in bank_plan["entities"]]
        bank_plan["mining_footprint"] = footprint
        check = preflight(self.game, self.builder, obs, final["entities"], ignored_units=ignored)
        if not check.get("ok"):
            return dict(check, stage="whole_alternate_steam_plan")
        coexist = merge_entities(steam["plan"]["entities"], portions["prebuild"])
        check = preflight(self.game, self.builder, obs, coexist)
        if not check.get("ok"):
            return dict(check, stage="old_route_and_clear_prebuild_coexistence")
        protected = [
            bindings[identity(e)]
            for e in steam["plan"]["entities"]
            if identity(e) not in {identity(r) for r in trace["entities"]}
        ]
        protected += [bindings[identity(e)] for e in portions["retained"]]
        protected_units = {e["unit_number"] for e in protected}
        for unit in steam.get("preserved_engine_units", []):
            engine = next(
                (e for e in obs["entities"] if e.get("unit_number") == unit and e["name"] == "steam-engine"), None
            )
            if not engine:
                return {"ok": False, "reason": "preserved_original_steam_engine_missing"}
            if unit not in protected_units:
                protected.append(_bound(engine, engine))
        return {
            "ok": True,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "phase": "prebuild",
            "old_plan_signature": _signature(steam["plan"]),
            "old_bank_signature": _signature(bank["plan"]),
            "final_plan": final,
            "final_bank_plan": bank_plan,
            "route": route,
            "source_port": source,
            "destination_port": destination,
            "protected": protected,
            "new_units": [],
            "samples": [],
            **{k: [bindings[identity(e)] for e in portions[k]] for k in ("conflicts", "obsolete")},
            **{k: portions[k] for k in ("prebuild", "deferred", "added")},
            "prepared_tick": obs["tick"],
        }

    def _exact(self, obs, wanted):
        live = next((e for e in obs["entities"] if e["name"] == wanted["name"] and xy(e) == xy(wanted)), None)
        return (
            live
            if live and live.get("unit_number") == wanted["unit_number"] and identity(live) == identity(wanted)
            else None
        )

    def _check(self, obs):
        if any(not self._exact(obs, e) for e in self.state["protected"]):
            return {"status": "blocked", "reason": "fixed_steam_or_retained_coal_identity_changed"}
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        previous = {(e["name"], *xy(e)): e for e in self.state["new_units"]}
        old_units = {e["unit_number"] for e in self.state["conflicts"]}
        for spec in self.state["added"]:
            key = spec["name"], *xy(spec)
            live, old = actual.get(key), previous.get(key)
            if old and (not live or live.get("unit_number") != old["unit_number"] or identity(live) != identity(spec)):
                return {"status": "blocked", "reason": "new_steam_coal_route_identity_changed"}
            if live and live.get("unit_number") not in old_units and identity(live) == identity(spec) and not old:
                self.state["new_units"].append(_bound(spec, live))
        return None

    def _stock(self, obs):
        needed = dict(Counter(e.get("item", e["name"]) for e in self.state["deferred"]))
        if not needed:
            return None
        payload = json.dumps(json.dumps(list(needed), separators=(",", ":")))
        stock = self.game.query(
            "local names=helpers.json_to_table("
            + payload
            + """);local out={};local inv=a.get_main_inventory()
for _,name in ipairs(names) do out[name]=inv.get_item_count{name=name,quality="normal"} end;return {stock=out}
"""
        ).get("stock", {})
        for item, count in needed.items():
            if stock.get(item, 0) < count:
                normal = dict(obs, inventory=dict(obs.get("inventory", {})))
                normal["inventory"][item] = stock.get(item, 0)
                return self.builder.bootstrap.ensure_item(normal, item, count)
        return None

    def _recover(self, obs, key, *, batch):
        pending = self.state.get("pending_recovery", [])
        wanted = self.state[key]
        pending_units = {e["unit_number"] for e in pending}
        for e in wanted:
            if e["unit_number"] not in pending_units and not self._exact(obs, e):
                return {"status": "blocked", "reason": "unstarted_old_coal_recovery_identity_changed"}
        if pending:
            remaining = []
            for e in pending:
                live = next((x for x in obs["entities"] if x["name"] == e["name"] and xy(x) == xy(e)), None)
                if live and not self._exact(obs, e):
                    return {"status": "blocked", "reason": "coal_recovery_will_not_retarget_new_unit"}
                if live:
                    remaining.append(e)
            removed = pending_units - {e["unit_number"] for e in remaining}
            self.state[key] = [e for e in wanted if e["unit_number"] not in removed]
            self.state["pending_recovery"] = remaining
            self._save()
            if remaining:
                return self._mine(remaining, obs["world_id"], batch)
        if self.state[key]:
            pending = self.state[key][: 8 if batch else 1]
            self.state["pending_recovery"] = pending
            self._save()
            return self._mine(pending, obs["world_id"], batch)
        self.state.pop("pending_recovery", None)
        self._save()
        return None

    @staticmethod
    def _mine(rows, world, batch):
        actions = [
            {
                "type": "mine",
                "name": e["name"],
                "position": e["position"],
                "count": 1,
                "expected_entity_unit": e["unit_number"],
                "expected_entity_world_id": world,
                "reason": "normally recover only the exact owned superseded coal route asset",
            }
            for e in rows
        ]
        return (
            {"type": "mine_many", "actions": actions, "reason": "normally retire proven obsolete steam coal corridor"}
            if batch
            else actions[0]
        )

    def _publish(self, obs):
        steam, saved = self._load(self.steam), self._load(self.outposts)
        if (
            steam.get("world_id") != obs["world_id"]
            or saved.get("world_id") != obs["world_id"]
            or steam.get("catalog_fingerprint") != self.catalog.fingerprint
            or saved.get("catalog_fingerprint", self.catalog.fingerprint) != self.catalog.fingerprint
        ):
            return {"status": "blocked", "reason": "steam_reroute_publication_world_changed"}
        if _signature(steam["plan"]) not in (self.state["old_plan_signature"], _signature(self.state["final_plan"])):
            return {"status": "blocked", "reason": "steam_plan_changed_during_coal_reroute"}
        matches = self._canonical(saved, steam["bank"])
        if len(matches) != 1 or _signature(matches[0]["plan"]) not in (
            self.state["old_bank_signature"],
            _signature(self.state["final_bank_plan"]),
        ):
            return {"status": "blocked", "reason": "canonical_coal_bank_changed_during_reroute"}
        bank = deepcopy(matches[0])
        bank["plan"] = deepcopy(self.state["final_bank_plan"])
        saved["banks"][saved["banks"].index(matches[0])] = bank
        steam.update(plan=deepcopy(self.state["final_plan"]), bank=bank, samples=[])
        board_path = self.path.parent / "pcb-board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "steam_reroute_board_world_changed"}
        board["blocks"]["steam_expansion"] = dict(steam["plan"], reserved_area=steam["core"]["reserved_area"])
        self.steam.state, self.outposts.state = steam, saved
        _atomic_json(self.steam.path, steam)
        _atomic_json(self.outposts.path, saved)
        _atomic_json(board_path, board)
        result = self.outposts.publish_reservations(obs)
        if result:
            return result
        self.state["published"] = True
        self._save()
        return None

    def _health(self, obs):
        if not underground_proof(obs, self.state["route"]):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
        if any(
            (e["name"], *xy(e)) not in actual or identity(actual[(e["name"], *xy(e))]) != identity(e)
            for e in self.state["route"]["entities"]
        ):
            return None
        units = [{**e, "unit_number": actual[(e["name"], *xy(e))]["unit_number"]} for e in self.state["added"]]
        payload = json.dumps(json.dumps(units, separators=(",", ":")))
        coal = self.game.query(
            "local rows=helpers.json_to_table("
            + payload
            + """);local count=0
for _,spec in ipairs(rows) do local e=target(spec.position,spec.name)
 if not e or e.force~=f or e.unit_number~=spec.unit_number or e.direction~=spec.direction then return {ok=false} end
 for lane=1,2 do count=count+e.get_transport_line(lane).get_item_count{name="coal",quality="normal"} end
end;return {ok=true,tick=game.tick,count=count}
"""
        )
        if coal.get("ok") and coal.get("count", 0) > 0:
            self.state["new_coal_witness"] = coal
        witness = self.state.get("new_coal_witness")
        if not witness or not 0 <= coal.get("tick", obs["tick"]) - witness["tick"] <= 600:
            return None
        sample = self.steam.sample(obs)
        if not sample or not sample.get("ok") or len(sample.get("engines", [])) != 2:
            return None
        history = self.state["samples"]
        if history and (
            not 0 < sample["tick"] - history[-1]["tick"] <= 600
            or [e["unit_number"] for e in sample["engines"]] != [e["unit_number"] for e in history[-1]["engines"]]
        ):
            history.clear()
        history.append(sample)
        while len(history) > 2 and history[1]["tick"] <= sample["tick"] - 1800:
            history.pop(0)
        self.steam.state["samples"] = deepcopy(history)
        _atomic_json(self.steam.path, self.steam.state)
        self._save()
        if len(history) < 2 or history[-1]["tick"] - history[0]["tick"] < 1800:
            return {"sampling": True}
        return {
            "tick": sample["tick"],
            "from_tick": history[0]["tick"],
            "to_tick": history[-1]["tick"],
            "added_path_coal_witness": deepcopy(witness),
            "latest_native_steam_sample": sample,
            "route_metrics": self.state["route"]["route_metrics"],
            "scope": "coal on added resource-avoiding route and both loaded engines over thirty sampled game seconds",
        }

    def step(self, obs, parameters=None):
        if parameters not in (None, {}):
            return {"status": "blocked", "reason": "steam_coal_reroute_takes_no_parameters"}
        if not self.state:
            prepared = self.prepare(obs, parameters)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "steam_coal_reroute_world_or_catalog_changed"}
        result = self._check(obs)
        if result:
            return result
        self._save()
        phase = self.state["phase"]
        if phase not in ("retire", "verified") and any(not self._exact(obs, e) for e in self.state["obsolete"]):
            return {"status": "blocked", "reason": "old_coal_buffer_asset_changed_before_new_route_proof"}
        if phase == "prebuild":
            if any(not self._exact(obs, e) for e in self.state["conflicts"]):
                return {"status": "blocked", "reason": "old_cutover_asset_changed_before_prebuild_finished"}
            if self.state["prebuild"]:
                plan = _plan(
                    self.state["prebuild"],
                    [],
                    clear_obstacles=self.state["route"].get("clear_obstacles", []),
                    clearance_world_id=obs["world_id"],
                )
                result = self.builder.ensure_plan(obs, plan)
                if result.get("status") != "succeeded":
                    return result
            self.state["phase"] = "cut"
            self._save()
        if self.state["phase"] == "cut":
            result = self._stock(obs)
            if result:
                return result
            result = self._recover(obs, "conflicts", batch=False)
            if result:
                return result
            self.state["phase"] = "build"
            self._save()
        if self.state["phase"] == "build":
            if self.state["deferred"]:
                # Include retained anchors and the complete exit scope in the
                # policy check; keep missing replacements first in build order.
                final = self.state["final_plan"]
                plan = dict(final, entities=merge_entities(self.state["deferred"], final["entities"]))
                result = self.builder.ensure_plan(obs, plan)
                if result.get("status") != "succeeded":
                    return result
            if not underground_proof(obs, self.state["route"]):
                return {"status": "waiting", "reason": "waiting for native alternative underground pairs"}
            result = self._publish(obs)
            if result:
                return result
            self.state["phase"] = "sample"
            self._save()
        proof = self._health(obs)
        if proof and proof.get("sampling"):
            return {
                "status": "waiting",
                "reason": "sampling added-path coal and loaded generation for thirty game seconds",
            }
        if not proof:
            self.state["samples"] = []
            self._save()
            if self.state.get("published"):
                self.steam.state["samples"] = []
                _atomic_json(self.steam.path, self.steam.state)
            return {
                "status": "waiting",
                "reason": "waiting for added-path coal and thirty seconds of loaded generation before retirement",
            }
        self.state["evidence"] = proof
        if self.state["phase"] == "sample":
            self.state["phase"] = "retire"
            self._save()
        if self.state["phase"] == "retire":
            result = self._recover(obs, "obsolete", batch=True)
            if result:
                return result
            self.state["phase"] = "verified"
            self._save()
        return {
            "status": "succeeded",
            "reason": "resource-avoiding steam coal route verified and only superseded owned belts recovered",
            "evidence": proof,
        }

    def proof(self, obs):
        if (
            not self.state
            or self.state["world_id"] != obs["world_id"]
            or self.state["catalog_fingerprint"] != self.catalog.fingerprint
            or self.state["phase"] != "verified"
            or self._check(obs)
        ):
            return None
        proof = self._health(obs)
        return proof if proof and not proof.get("sampling") else None
