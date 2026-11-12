"""One normally paid base radar, with native power and overlapping defense gates."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .industrial import PlanningBudget
from .planning import overlap, preflight
from .power_grid import plan_connection as power_connection
from .resource_policy import mining_reservations
from .routing import VECTORS
from .smelting import merge_entities
from .transport import unbuilt_external_keepouts, xy


BASE_BLOCK = "radar_base"
FUTURE_BLOCK = "radar_oil_future"
DEFENSE_SCOPE = "radar:network"
ENEMY_EXCLUSION = 32
MINIMUM_FULL_MAGAZINES = 5


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def _key(spec):
    return spec["name"] + ":" + str(spec["position"]["x"]) + ":" + str(spec["position"]["y"])


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _parameters(value):
    if value is not None and (not isinstance(value, dict) or value):
        raise ValueError("radar_network accepts only empty parameters")


def radar_core(catalog, x, y):
    proto = catalog.entities["radar"]
    if (
        proto.get("type") != "radar"
        or not proto.get("electric")
        or proto.get("burner")
        or not _finite(proto.get("energy_usage"))
        or proto["energy_usage"] <= 0
        or any(not _finite(v) or v % 1 != 0.5 for v in (x, y))
    ):
        raise ValueError("finite half-tile electric radar geometry required")
    box = proto["collision_box"]
    if (
        any(not _finite(v) for p in box.values() for v in p.values())
        or not -1.5 < box["left_top"]["x"] < 0 < box["right_bottom"]["x"] < 1.5
        or not -1.5 < box["left_top"]["y"] < 0 < box["right_bottom"]["y"] < 1.5
    ):
        raise ValueError("installed radar must fit the native three by three body")
    radar = dict(entity("radar", x, y), _width=3, _height=3)
    pole = entity("small-electric-pole", x + 2, y + 1)
    service = [
        entity("transport-belt", x + dx, y + dy)
        for dx in range(-2, 3)
        for dy in range(-2, 3)
        if max(abs(dx), abs(dy)) == 2 and (dx, dy) != (2, 1)
    ]
    return _plan(
        [radar, pole],
        [],
        radar=radar,
        power_pole=pole,
        service_keepouts=service,
        reserved_area=[[x - 3.5, y - 3.5], [x + 3.5, y + 3.5]],
        watts=proto["energy_usage"] * 60,
        layout_policy="paid-defended-base-radar-v1",
    )


def radar_candidates(obs, board, posts, *, oil=False, effective_range=16, keepout_cells=(), extra_areas=()):
    """Bounded exterior sites inside two native defense disks, filtered before cap."""
    if not _finite(effective_range) or not 3.5 < effective_range <= 64:
        return []
    actual = {e.get("unit_number"): e for e in obs.get("entities", [])}
    known = [
        p
        for p in posts
        if type(p.get("unit_number")) is int
        and p["unit_number"] > 0
        and actual.get(p["unit_number"], {}).get("name") == "gun-turret"
        and actual[p["unit_number"]].get("position") == p.get("position")
        and _finite(actual[p["unit_number"]].get("health"))
        and _finite(actual[p["unit_number"]].get("max_health"))
        and actual[p["unit_number"]]["max_health"] > 0
        and actual[p["unit_number"]]["health"] >= actual[p["unit_number"]]["max_health"]
    ]
    machines = [
        e
        for e in obs.get("entities", [])
        if e["name"] in (("oil-refinery", "pumpjack") if oil else ("lab", "steam-engine", "assembling-machine-1"))
    ]
    if not machines:
        return []
    anchor = {axis: sum(e["position"][axis] for e in machines) / len(machines) for axis in ("x", "y")}
    pairs = []
    for i, first in enumerate(known):
        for second in known[i + 1 :]:
            if math.dist(xy(first), xy(second)) > 2 * (effective_range - 3.5):
                continue
            midpoint = {axis: (first["position"][axis] + second["position"][axis]) / 2 for axis in ("x", "y")}
            if oil and math.dist(tuple(midpoint.values()), tuple(anchor.values())) > 36:
                continue
            pairs.append((sum((midpoint[k] - anchor[k]) ** 2 for k in ("x", "y")), midpoint, first, second))
    pairs.sort(key=lambda row: (row[0], row[1]["x"], row[1]["y"]))
    areas = [b["reserved_area"] for b in board.get("blocks", {}).values() if b.get("reserved_area")]
    areas += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
    areas += list(extra_areas)
    hard = set(keepout_cells)
    seen = set()
    pools = []
    checked = 0
    limit = 8 if oil else 16

    def diverse_sites(extra=()):
        groups = pools + ([list(extra)] if extra else [])
        result = []
        for index in range(limit):
            for group in groups:
                if index < len(group):
                    result.append(group[index])
                if len(result) == limit:
                    return result
        return result

    radius = effective_range - 3.5
    for _, point, first, second in pairs[:32]:
        sites = []
        lo_x = math.ceil(max(first["position"]["x"], second["position"]["x"]) - radius - 0.5)
        hi_x = math.floor(min(first["position"]["x"], second["position"]["x"]) + radius - 0.5)
        lo_y = math.ceil(max(first["position"]["y"], second["position"]["y"]) - radius - 0.5)
        hi_y = math.floor(min(first["position"]["y"], second["position"]["y"]) + radius - 0.5)
        pool = [(x + 0.5, y + 0.5) for x in range(lo_x, hi_x + 1) for y in range(lo_y, hi_y + 1)]
        pool.sort(
            key=lambda p: (
                (p[0] - anchor["x"]) ** 2 + (p[1] - anchor["y"]) ** 2,
                (p[0] - point["x"]) ** 2 + (p[1] - point["y"]) ** 2,
                p,
            )
        )
        for x, y in pool:
            if (x, y) in seen:
                continue
            seen.add((x, y))
            checked += 1
            if checked > 16384:
                return diverse_sites(sites)
            area = [[x - 3.5, y - 3.5], [x + 3.5, y + 3.5]]
            if any(overlap(area, a) for a in areas):
                continue
            if any((x + dx, y + dy) in hard for dx in range(-3, 4) for dy in range(-3, 4)):
                continue
            if any(
                any(
                    math.hypot(cx - p["position"]["x"], cy - p["position"]["y"]) > effective_range
                    for cx in (area[0][0], area[1][0])
                    for cy in (area[0][1], area[1][1])
                )
                for p in (first, second)
            ):
                continue
            sites.append((x, y))
            if len(sites) >= limit:
                break
        if sites:
            pools.append(sites)
    return diverse_sites()


def planned_power_watts(catalog, obs, board):
    """Charge known unbuilt electrical equipment, never assume its future idle load."""
    actual = {(e["name"], *xy(e)) for e in obs.get("entities", [])}
    missing = {}
    for name, block in board.get("blocks", {}).items():
        if name in (BASE_BLOCK, FUTURE_BLOCK):
            continue
        for spec in block.get("entities", []):
            key = (spec["name"], *xy(spec))
            if key in actual:
                continue
            proto = catalog.entities.get(spec["name"])
            if proto is None:
                raise ValueError("planned entity absent from pinned catalog")
            if not proto.get("electric"):
                continue
            usage = proto.get("energy_usage")
            if not _finite(usage) or usage < 0:
                raise ValueError("planned native energy usage unavailable")
            missing[key] = usage * 60
    return sum(missing.values())


class RadarNetwork:
    def __init__(self, game, builder, catalog, perimeter):
        self.game, self.builder, self.catalog, self.perimeter = game, builder, catalog, perimeter
        self.path = Path(game.cfg.runtime_dir) / "radar-network.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.last_proof_failure = None

    def _identity(self, obs, value):
        return (
            obs.get("ok", True)
            and isinstance(obs.get("world_id"), str)
            and bool(obs["world_id"])
            and value.get("world_id") == obs["world_id"]
            and value.get("catalog_fingerprint") == self.catalog.fingerprint
        )

    def _board(self, obs):
        path = self.path.parent / "pcb-board.json"
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not self._identity(obs, value):
            raise ValueError("radar_board_identity_changed")
        return value

    def _keepouts(self, obs, board):
        blocks = {k: v for k, v in board["blocks"].items() if k not in (BASE_BLOCK, FUTURE_BLOCK)}
        cells = unbuilt_external_keepouts(self.builder, obs, blocks)
        ports = [
            p for block in list(blocks.values()) + [board.get("shared_main_bus", {})] for p in block.get("ports", [])
        ]
        for filename in ("material-sources.json", "fluid-sources.json"):
            path = self.path.parent / filename
            if not path.exists():
                continue
            registry = json.loads(path.read_text(encoding="utf-8"))
            if not self._identity(obs, registry):
                raise ValueError("radar_source_registry_identity_changed")
            ports += list(registry.get("sources", {}).values())
        for port in ports:
            if port.get("facing") not in VECTORS or port.get("direction") not in ("input", "output"):
                continue
            dx, dy = VECTORS[port["facing"]]
            if port["direction"] == "input":
                dx, dy = -dx, -dy
            cells.update((port["position"]["x"] + i * dx, port["position"]["y"] + i * dy) for i in range(5))
        areas = [b["reserved_area"] for b in blocks.values() if b.get("reserved_area")]
        areas += [r["area"] for r in board.get("shared_main_bus", {}).get("reservations", []) if r.get("area")]
        return cells, areas

    def _geometry(self, obs, board, core, entities=(), extra_areas=()):
        cells, areas = self._keepouts(obs, board)
        area = core["reserved_area"]
        if any(overlap(area, other) for other in areas + list(extra_areas)):
            return {"ok": False, "reason": "radar_reserved_area_conflict"}
        if any(
            area[0][0] < x + 0.49 and x - 0.49 < area[1][0] and area[0][1] < y + 0.49 and y - 0.49 < area[1][1]
            for x, y in cells
        ):
            return {"ok": False, "reason": "radar_future_transport_or_fixed_port_conflict"}
        for spec in entities:
            occupied = self.builder._occupied_by_plan([spec])
            if occupied & cells:
                return {"ok": False, "reason": "radar_power_crosses_future_transport_or_fixed_port"}
            if any(
                any(
                    x + 0.49 > a[0][0] and x - 0.49 < a[1][0] and y + 0.49 > a[0][1] and y - 0.49 < a[1][1]
                    for x, y in occupied
                )
                for a in areas + list(extra_areas)
            ):
                return {"ok": False, "reason": "radar_power_crosses_producer_reservation"}
        return {"ok": True, "foreign_areas": areas}

    def _site_guard(self, game, obs, core, board):
        posts = [
            {k: p[k] for k in ("position", "unit_number") if k in p}
            for p in getattr(self.perimeter, "state", {}).get("posts", [])
            if type(p.get("unit_number")) is int
        ]
        _, areas = self._keepouts(obs, board)
        return game.query(
            RADAR_SITE_LUA.replace(
                "PAYLOAD",
                _payload(
                    {
                        "world_id": obs["world_id"],
                        "core": core,
                        "posts": posts,
                        "zones": mining_reservations(self.game, obs["world_id"]),
                        "foreign_areas": areas,
                        "enemy_radius": ENEMY_EXCLUSION,
                        "minimum_full_magazines": MINIMUM_FULL_MAGAZINES,
                    }
                ),
            )
        )

    def _local_coverage(self, site):
        """Private existing-post evidence; unrelated perimeter upkeep is separate."""
        if not isinstance(site, dict) or not site.get("ok"):
            return None
        registered = {
            p.get("unit_number"): p
            for p in self.perimeter.state.get("posts", [])
            if type(p.get("unit_number")) is int and p["unit_number"] > 0
        }
        posts = site.get("covering_posts")
        if not isinstance(posts, list):
            return None
        selected = []
        seen = set()
        for post in posts:
            unit = post.get("unit_number")
            known = registered.get(unit)
            if (
                type(unit) is not int
                or unit in seen
                or not known
                or post.get("position") != known.get("position")
                or not _finite(post.get("full_magazines"))
                or post["full_magazines"] < MINIMUM_FULL_MAGAZINES
            ):
                return None
            seen.add(unit)
            selected.append(dict(post, name=known["name"]))
        if len(selected) < 2:
            return None
        return {
            "tick": site["tick"],
            "posts": selected,
            "minimum_full_normal_magazines": MINIMUM_FULL_MAGAZINES,
            "health_policy": "native full-health check",
            "area_coverage": "two exact registered guns cover the whole reserved area",
            "access_scope": site.get("access_scope"),
            "scope": "fresh local native coverage; unrelated perimeter state is not a prerequisite",
        }

    def _power_guard(self, game, obs, board, source, preserved, additional_radars):
        return game.query(
            RADAR_POWER_LUA.replace(
                "PAYLOAD",
                _payload(
                    {
                        "world_id": obs["world_id"],
                        "source": source,
                        "preserved_poles": preserved,
                        "scheduled_watts": planned_power_watts(self.catalog, obs, board),
                        "additional_watts": additional_radars * self.catalog.entities["radar"]["energy_usage"] * 60,
                    }
                ),
            )
        )

    def _choose(self, budget, obs, board, *, oil=False, prior=None):
        registered = getattr(self.perimeter, "state", {}).get("posts", [])
        report = self.perimeter.inspect(obs)
        if not report.get("ok"):
            return {"ok": False, "reason": "radar_current_defense_inspection_required"}
        live = {p.get("name"): p for p in report.get("posts", [])}
        # Apply the native stock gate before the bounded candidate cap. Otherwise
        # one unstocked pair can occupy every candidate and hide ready rear sites.
        posts = [
            p
            for p in registered
            if p.get("name") in live
            and live[p["name"]].get("unit_number") == p.get("unit_number")
            and not live[p["name"]].get("missing")
            and not live[p["name"]].get("identity_changed")
            and live[p["name"]].get("full_magazines", 0) >= MINIMUM_FULL_MAGAZINES
        ]
        native_range = getattr(self.perimeter, "state", {}).get("range")
        if not _finite(native_range):
            return {"ok": False, "reason": "radar_verified_native_defense_range_required"}
        extra = [prior["core"]["reserved_area"]] if prior else []
        if prior:
            extra += [
                [[px - 0.5, py - 0.5], [px + 0.5, py + 0.5]]
                for px, py in self.builder._occupied_by_plan(prior["plan"]["entities"])
            ]
        cells, _ = self._keepouts(obs, board)
        sites = radar_candidates(
            obs, board, posts, oil=oil, effective_range=native_range - 2, keepout_cells=cells, extra_areas=extra
        )
        failures = []
        for x, y in sites:
            if budget.expired():
                break
            core = radar_core(self.catalog, x, y)
            geometry = self._geometry(obs, board, core, extra_areas=extra)
            if not geometry["ok"]:
                failures.append(geometry["reason"])
                continue
            actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
            if any((e["name"], *xy(e)) in actual for e in core["entities"]):
                failures.append("radar_site_contains_existing_asset")
                continue
            site = self._site_guard(budget.game, obs, core, board)
            if not site.get("ok"):
                failures.append(site.get("reason", "radar_site_survey_failed"))
                continue
            reserved = [core["radar"]] + core["service_keepouts"]
            if prior:
                reserved += prior["plan"]["entities"]
            bridge = power_connection(budget.game, self.builder, obs, core["power_pole"]["position"], reserved)
            if not bridge.get("ok"):
                failures.append(bridge.get("reason", "radar_power_connection_failed"))
                continue
            source_live = next(
                (
                    e
                    for e in obs["entities"]
                    if e.get("unit_number") == bridge.get("source_unit_number")
                    and e["name"] == "small-electric-pole"
                    and e["position"] == bridge.get("source_position")
                ),
                None,
            )
            if not source_live or type(source_live.get("electric_network_id")) is not int:
                failures.append("radar_exact_power_source_unavailable")
                continue
            source = {
                k: source_live[k] for k in ("name", "position", "unit_number", "electric_network_id", "direction")
            }
            source["world_id"] = obs["world_id"]
            preserved = [source]
            new_poles = []
            incompatible = False
            for spec in bridge["entities"]:
                old = actual.get((spec["name"], *xy(spec)))
                if old:
                    if old.get("electric_network_id") != source["electric_network_id"]:
                        incompatible = True
                        break
                    preserved.append(
                        {k: old[k] for k in ("name", "position", "unit_number", "electric_network_id", "direction")}
                    )
                    preserved[-1]["world_id"] = obs["world_id"]
                else:
                    new_poles.append(spec)
            if incompatible:
                failures.append("radar_power_bridge_would_adopt_another_network")
                continue
            es = merge_entities(core["entities"], new_poles)
            geometry = self._geometry(obs, board, core, new_poles, extra_areas=extra)
            if not geometry["ok"]:
                failures.append(geometry["reason"])
                continue
            power = self._power_guard(budget.game, obs, board, source, preserved, 2 if oil else 1)
            if not power.get("ok"):
                failures.append(power.get("reason", "radar_power_margin_unproven"))
                continue
            if type(power.get("force_index")) is not int or power["force_index"] < 1:
                failures.append("radar_native_power_force_unverified")
                continue
            for spec in preserved:
                spec["force_index"] = power["force_index"]
            # Service cells reserve walking space; they are not belts to build.
            # The native site guard proves their actual walk access separately.
            whole = preflight(budget.game, self.builder, obs, es)
            if not whole.get("ok"):
                failures.append(whole.get("reason", "radar_whole_preflight_failed"))
                continue
            plan = _plan(
                es,
                [],
                reserved_area=core["reserved_area"],
                defense_scope_id=DEFENSE_SCOPE,
                clear_obstacles=whole["clear_obstacles"],
                clearance_world_id=obs["world_id"],
                layout_policy=core["layout_policy"],
                reserved_only=oil,
            )
            return {
                "ok": True,
                "core": core,
                "plan": plan,
                "power_source": source,
                "preserved_poles": preserved,
                "power_preview": power,
                "site_preview": site,
            }
        return {
            "ok": False,
            "reason": "no_safe_powered_oil_radar_reservation" if oil else "no_safe_powered_base_radar_site",
            "evidence": {"candidate_count": len(sites), "failures": failures[-6:]},
        }

    def prepare(self, obs, parameters=None):
        _parameters(parameters)
        if not self._identity(obs, {"world_id": obs.get("world_id"), "catalog_fingerprint": self.catalog.fingerprint}):
            return {"ok": False, "reason": "radar_requires_valid_world_observation"}
        if self.state:
            return (
                deepcopy(self.state)
                if self._identity(obs, self.state)
                else {"ok": False, "reason": "radar_world_or_catalog_changed"}
            )
        if not obs.get("technologies", {}).get("radar") or not all(
            obs.get("enabled_recipes", {}).get(k) for k in ("radar", "small-electric-pole")
        ):
            return {"ok": False, "reason": "native_radar_research_and_recipes_required"}
        try:
            board = self._board(obs)
            if any(k in board["blocks"] for k in (BASE_BLOCK, FUTURE_BLOCK)):
                return {"ok": False, "reason": "radar_reservation_without_owned_checkpoint"}
            if any(e["name"] == "radar" for e in obs.get("entities", [])):
                return {"ok": False, "reason": "existing_radar_requires_explicit_ownership_reconciliation"}
            budget = PlanningBudget(self.game)
            base = self._choose(budget, obs, board)
            if not base.get("ok"):
                return base
            future = self._choose(budget, obs, board, oil=True, prior=base)
            areas = [base["core"]["reserved_area"]]
            defense = self._local_coverage(base["site_preview"])
            if not defense:
                return {"ok": False, "reason": "radar_local_native_defense_preview_required"}
            if budget.expired():
                return budget.failure(stage="radar_complete_preparation")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "radar_native_geometry_or_context_invalid", "detail": str(exc)}
        return {
            "ok": True,
            "schema_version": 1,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "phase": "build",
            **{
                k: base[k] for k in ("core", "plan", "power_source", "preserved_poles", "power_preview", "site_preview")
            },
            "future_oil": dict(future, reserved=bool(future.get("ok")), coverage_status="preview_only_not_live_proof"),
            "defense_scope_id": DEFENSE_SCOPE,
            "defense_areas": areas,
            "defense_preview": defense,
            "bindings": {},
            "prepared_tick": obs.get("tick"),
        }

    def _save(self):
        _atomic_json(self.path, self.state)

    def _bind(self, obs):
        actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
        changed = False
        for spec in self.state["plan"]["entities"]:
            live = actual.get((spec["name"], *xy(spec)))
            old = self.state["bindings"].get(_key(spec))
            if old and (not live or live.get("unit_number") != old):
                return {"status": "blocked", "reason": "radar_owned_asset_missing_or_replaced"}
            if live:
                if (
                    type(live.get("unit_number")) is not int
                    or live["unit_number"] < 1
                    or live.get("direction") != spec.get("direction", 0)
                ):
                    return {"status": "blocked", "reason": "radar_owned_asset_identity_or_direction_changed"}
                if old is None:
                    self.state["bindings"][_key(spec)] = live["unit_number"]
                    changed = True
        if changed:
            self._save()
        return None

    def _reserve(self, obs, board):
        wanted = {BASE_BLOCK: self.state["plan"]}
        if self.state["future_oil"]["reserved"]:
            wanted[FUTURE_BLOCK] = self.state["future_oil"]["plan"]
        for name, plan in wanted.items():
            old = board["blocks"].get(name)
            if old and old != plan:
                return {"status": "blocked", "reason": "radar_registered_plan_changed"}
            if any(
                k not in wanted and p.get("reserved_area") and overlap(plan["reserved_area"], p["reserved_area"])
                for k, p in board["blocks"].items()
            ):
                return {"status": "blocked", "reason": "radar_reservation_conflict"}
            core = self.state["core"] if name == BASE_BLOCK else self.state["future_oil"]["core"]
            geometry = self._geometry(obs, board, core, [e for e in plan["entities"] if e != core["radar"]])
            if not geometry.get("ok"):
                return {
                    "status": "blocked",
                    "reason": "radar_reservation_or_power_keepout_changed",
                    "evidence": geometry,
                }
        if any(board["blocks"].get(k) != v for k, v in wanted.items()):
            board["blocks"].update(wanted)
            _atomic_json(self.path.parent / "pcb-board.json", board)
        return None

    def step(self, obs, parameters=None):
        _parameters(parameters)
        if not self.state:
            ready = self.prepare(obs)
            if not ready.get("ok"):
                return {"status": "blocked", **ready}
            self.state = ready
            self._save()
        if not self._identity(obs, self.state):
            return {"status": "blocked", "reason": "radar_world_or_catalog_changed"}
        result = self._bind(obs)
        if result:
            return result
        try:
            board = self._board(obs)
        except (OSError, ValueError):
            return {"status": "blocked", "reason": "radar_board_identity_changed"}
        try:
            result = self._reserve(obs, board)
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "blocked", "reason": "radar_reservation_context_changed"}
        if result:
            return result
        core = self.state["core"]
        try:
            geometry = self._geometry(
                obs, board, core, [e for e in self.state["plan"]["entities"] if e != core["radar"]]
            )
            if not geometry.get("ok"):
                return {"status": "blocked", "reason": "radar_site_or_power_corridor_changed", "evidence": geometry}
            site = self._site_guard(self.game, obs, core, board)
            if not self._local_coverage(site):
                return {"status": "blocked", "reason": "radar_security_or_service_changed", "evidence": site}
            installed = _key(core["radar"]) in self.state["bindings"]
            additional = int(not installed)
            power = self._power_guard(
                self.game, obs, board, self.state["power_source"], self.state["preserved_poles"], additional
            )
            if not power.get("ok"):
                return {"status": "blocked", "reason": "radar_native_power_margin_changed", "evidence": power}
            check = preflight(self.game, self.builder, obs, self.state["plan"]["entities"])
            if not check.get("ok"):
                return {"status": "blocked", "reason": "radar_whole_preflight_changed", "evidence": check}
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "blocked", "reason": "radar_fresh_native_context_changed"}
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        proof = self.proof(obs)
        if not proof:
            return {
                "status": "waiting",
                "reason": "waiting_for_exact_powered_working_radar",
                "evidence": self.last_proof_failure,
            }
        self.state.update(phase="verified", proof=proof)
        self._save()
        return {"status": "succeeded", "reason": "one normally paid defended base radar is working", "evidence": proof}

    def proof(self, obs, parameters=None):
        _parameters(parameters)
        self.last_proof_failure = None
        if not self.state or not self._identity(obs, self.state):
            return None
        actual = {(e["name"], *xy(e)): e for e in obs.get("entities", [])}
        specs = []
        for spec in self.state["plan"]["entities"]:
            unit = self.state["bindings"].get(_key(spec))
            live = actual.get((spec["name"], *xy(spec)))
            if not unit or not live or live.get("unit_number") != unit:
                return None
            specs.append(dict(spec, unit_number=unit))
        try:
            board = self._board(obs)
        except (OSError, ValueError):
            self.last_proof_failure = {"reason": "radar_board_identity_changed"}
            return None
        if board["blocks"].get(BASE_BLOCK) != self.state["plan"] or (
            self.state["future_oil"]["reserved"]
            and board["blocks"].get(FUTURE_BLOCK) != self.state["future_oil"]["plan"]
        ):
            self.last_proof_failure = {"reason": "radar_registered_plan_changed"}
            return None
        try:
            geometry = self._geometry(
                obs,
                board,
                self.state["core"],
                [e for e in self.state["plan"]["entities"] if e != self.state["core"]["radar"]],
            )
            if not geometry.get("ok"):
                self.last_proof_failure = geometry
                return None
            site = self._site_guard(self.game, obs, self.state["core"], board)
            defense = self._local_coverage(site)
            if not defense:
                self.last_proof_failure = (
                    site if not site.get("ok") else {"reason": "radar_local_native_defense_proof_required"}
                )
                return None
            power = self._power_guard(
                self.game, obs, board, self.state["power_source"], self.state["preserved_poles"], 0
            )
            if not power.get("ok"):
                self.last_proof_failure = power
                return None
        except (OSError, ValueError, KeyError, TypeError):
            self.last_proof_failure = {"reason": "radar_fresh_native_context_changed"}
            return None
        native = self.game.query(
            RADAR_PROOF_LUA.replace(
                "PAYLOAD",
                _payload(
                    {
                        "world_id": obs["world_id"],
                        "specs": specs,
                        "source": self.state["power_source"],
                        "radar_watts": self.state["core"]["watts"],
                    }
                ),
            )
        )
        if not native.get("ok"):
            self.last_proof_failure = native
            return None
        return {
            "tick": native["tick"],
            "radar_count": 1,
            "radar": native,
            "power": power,
            "defense_coverage": defense,
            "future_oil_reserved": self.state["future_oil"]["reserved"],
            "future_oil_coverage_status": "reservation preview; no current native defense or power proof claimed",
            "scope": "exact powered working base radar and bounded chart sample; no full-map or sustained scan claim",
        }


RADAR_POWER_LUA = r"""
local args=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=args.world_id then return {ok=false,reason="radar_power_world_changed"}end
local function exact(q)local e=target(q.position,q.name)
 if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or q.world_id~=args.world_id
  or(q.force_index and q.force_index~=f.index)or e.direction~=q.direction
  or e.position.x~=q.position.x or e.position.y~=q.position.y or e.electric_network_id~=q.electric_network_id then return nil end
 return e
end
local source=exact(args.source)
if not source then return {ok=false,reason="radar_power_source_changed"}end
for _,q in ipairs(args.preserved_poles)do if not exact(q)then return {ok=false,reason="radar_preserved_power_pole_changed"}end end
local sub=source.electric_network;local net=sub and sub.parent_network
if not net or not net.valid then return {ok=false,reason="radar_native_electric_network_unavailable"}end
local flow=net.flow_last_tick;local offered=(flow.primary_output+flow.secondary_output+flow.solar_output)*60
local demand=flow.maximum_consumption*60;local stats=net.statistics;local input=0;local output=0
for name in pairs(stats.input_counts)do input=input+stats.get_flow_count{name=name,category="input",precision_index=defines.flow_precision_index.five_seconds}*60 end
for name in pairs(stats.output_counts)do output=output+stats.get_flow_count{name=name,category="output",precision_index=defines.flow_precision_index.five_seconds}*60 end
local measured=math.max(input,output);local reserve=math.max(300000,offered*.2)
local headroom=offered-math.max(demand,measured)-args.scheduled_watts-args.additional_watts
if offered<=0 or flow.consumption_satisfaction<.999 or headroom<reserve then
 return {ok=false,reason="radar_measured_power_headroom_insufficient",offered_watts=offered,demand_watts=demand,
 measured_5s_watts=measured,scheduled_watts=args.scheduled_watts,additional_watts=args.additional_watts,reserve_watts=reserve,headroom_watts=headroom}
end
return {ok=true,tick=game.tick,network_id=source.electric_network_id,force_index=f.index,offered_watts=offered,
 demand_watts=demand,measured_5s_watts=measured,scheduled_watts=args.scheduled_watts,additional_watts=args.additional_watts,
 reserve_watts=reserve,headroom_watts=headroom,satisfaction=flow.consumption_satisfaction,
 scope="native currently offered production and current/five-second measured load, with explicit planned-load and reserve deductions"}
"""


RADAR_SITE_LUA = r"""
local args=helpers.json_to_table(PAYLOAD);local c=args.core;local p=c.radar.position;local area=c.reserved_area
if not d or d.world_id~=args.world_id then return {ok=false,reason="radar_site_world_changed"}end
local proto=prototypes.entity.radar
if not proto or proto.tile_width~=3 or proto.tile_height~=3 or proto.get_max_energy_usage("normal")*60~=c.watts
 or not f.technologies.radar or not f.technologies.radar.researched or not f.recipes.radar.enabled then
 return {ok=false,reason="radar_native_prototype_or_unlock_changed"}
end
if s.count_entities_filtered{position=p,radius=args.enemy_radius,force="enemy",type={"unit","unit-spawner"}}>0 then
 return {ok=false,reason="radar_nearby_enemies_require_security_first"}
end
local hostile_range=0
for _,foe in pairs(prototypes.entity)do if foe.type=="turret"then
 local attack=foe.attack_parameters
 if not attack or type(attack.range)~="number"or attack.range<0 or attack.range>128 then
  return {ok=false,reason="radar_hostile_turret_range_profile_unsupported"}
 end
 hostile_range=math.max(hostile_range,attack.range)
end end
for _,foe in pairs(s.find_entities_filtered{position=p,radius=hostile_range+7,force="enemy",type="turret"})do
 local attack=foe.prototype.attack_parameters
 if not attack or type(attack.range)~="number"then return {ok=false,reason="radar_hostile_turret_range_profile_unsupported"}end
 local q=foe.position
 local dx=math.max(area[1][1]-q.x,0,q.x-area[2][1]);local dy=math.max(area[1][2]-q.y,0,q.y-area[2][2])
 if dx*dx+dy*dy<=(attack.range+2)^2 then return {ok=false,reason="radar_service_inside_hostile_turret_range"}end
end
local function overlaps(a,b)return a[1][1]<b[2][1]and a[2][1]>b[1][1]and a[1][2]<b[2][2]and a[2][2]>b[1][2]end
if s.count_entities_filtered{area=area,type="resource"}>0 then return {ok=false,reason="radar_body_or_service_over_resource"}end
for _,zone in ipairs(args.zones)do if overlaps(area,zone.area)then return {ok=false,reason="radar_body_or_service_over_mining_growth"}end end
local armed={};local seen={};local range=prototypes.entity["gun-turret"].attack_parameters.range-2
for _,q in ipairs(args.posts)do local e=target(q.position,"gun-turret")
 if e and e.valid and e.force==f and e.surface==s and e.unit_number==q.unit_number and not seen[e.unit_number]
  and e.health>=e.max_health and e.position.x==q.position.x and e.position.y==q.position.y then
  seen[e.unit_number]=true;local covers=true
  for _,x in ipairs{area[1][1],area[2][1]}do for _,y in ipairs{area[1][2],area[2][2]}do
   if (e.position.x-x)^2+(e.position.y-y)^2>range^2 then covers=false end
  end end
  local inv=e.get_inventory(defines.inventory.turret_ammo);local full=0
  if inv then for i=1,#inv do local stack=inv[i]
   if stack.valid_for_read and stack.name=="firearm-magazine"and stack.quality.name=="normal"then
    full=full+stack.count-(stack.ammo<stack.prototype.magazine_size and 1 or 0)
   end
  end end
  if covers and full>=args.minimum_full_magazines then armed[#armed+1]={unit_number=e.unit_number,position=pos(e.position),full_magazines=full}end
 end
end
if #armed<2 then return {ok=false,reason="radar_requires_two_healthy_armed_covering_posts",covering_posts=#armed}end
local left=math.floor(area[1][1])-3;local right=math.ceil(area[2][1])+2
local top=math.floor(area[1][2])-3;local bottom=math.ceil(area[2][2])+2
local cache={};local reached={};local queue={};local head=1
local function key(x,y)return x..":"..y end
local function walk(x,y)
 if x<left or x>right or y<top or y>bottom then return false end
 local k=key(x,y);if cache[k]~=nil then return cache[k]end
 local q={x=x+.5,y=y+.5};local tile={{x,y},{x+1,y+1}}
 local legal=not(math.abs(q.x-p.x)<=1 and math.abs(q.y-p.y)<=1)
  and not(q.x==c.power_pole.position.x and q.y==c.power_pole.position.y)
 for _,a in ipairs(args.foreign_areas)do if overlaps(tile,a)then legal=false;break end end
 if legal then legal=s.can_place_entity{name="character",position=q,force=f}end
 cache[k]=legal;return legal
end
for x=left,right do for _,y in ipairs{top,bottom}do if walk(x,y)then local k=key(x,y);reached[k]=true;queue[#queue+1]={x,y}end end end
for y=top+1,bottom-1 do for _,x in ipairs{left,right}do if walk(x,y)then local k=key(x,y);reached[k]=true;queue[#queue+1]={x,y}end end end
while head<=#queue do local q=queue[head];head=head+1
 for _,v in ipairs{{1,0},{-1,0},{0,1},{0,-1}}do local x=q[1]+v[1];local y=q[2]+v[2];local k=key(x,y)
  if not reached[k]and walk(x,y)then reached[k]=true;queue[#queue+1]={x,y}end
 end
end
for _,v in ipairs{{0,-2},{-2,0},{0,2},{2,0}}do
 if not reached[key(math.floor(p.x+v[1]),math.floor(p.y+v[2]))]then return {ok=false,reason="radar_service_walk_access_unavailable"}end
end
local charted=0;for dx=-1,1 do for dy=-1,1 do if f.is_chunk_charted(s,{x=math.floor(p.x/32)+dx,y=math.floor(p.y/32)+dy})then charted=charted+1 end end end
return {ok=true,tick=game.tick,covering_posts=armed,enemy_exclusion_radius=args.enemy_radius,
 local_walk_cells=#queue,charted_sample_chunks=charted,sampled_chunks=9,
 access_scope="native walkable local component from outside the service reservation to all four radar service sides"}
"""


RADAR_PROOF_LUA = r"""
local args=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=args.world_id then return {ok=false,reason="radar_proof_world_changed"}end
local radar=nil
for _,q in ipairs(args.specs)do local e=target(q.position,q.name)
 if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.direction~=(q.direction or 0)
  or e.position.x~=q.position.x or e.position.y~=q.position.y then return {ok=false,reason="radar_native_bound_asset_changed"}end
 if e.electric_network_id~=args.source.electric_network_id then return {ok=false,reason="radar_native_network_changed"}end
 if q.name=="radar"then radar=e end
end
if not radar or not radar.is_connected_to_electric_network()or radar.energy<=0 or radar.status~=defines.entity_status.working
 or radar.prototype.get_max_energy_usage("normal")*60~=args.radar_watts then return {ok=false,reason="radar_native_powered_working_required"}end
local progress=radar.radar_scan_progress
if type(progress)~="number"or progress<=0 or progress>1 then return {ok=false,reason="radar_native_scan_progress_required"}end
local charted=0;for dx=-1,1 do for dy=-1,1 do if f.is_chunk_charted(s,{x=math.floor(radar.position.x/32)+dx,y=math.floor(radar.position.y/32)+dy})then charted=charted+1 end end end
return {ok=true,tick=game.tick,unit_number=radar.unit_number,position=pos(radar.position),world_id=d.world_id,
 force_index=f.index,network_id=radar.electric_network_id,energy=radar.energy,status="working",scan_progress=progress,
 charted_sample_chunks=charted,sampled_chunks=9,scope="current native scan progress and existing chart sample, not attribution of all charted chunks to this radar"}
"""
