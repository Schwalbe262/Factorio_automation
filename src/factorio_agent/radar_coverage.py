"""Chunk-based factory visibility using normally paid, powered, defended radars."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .defense_coverage import coverage_report as coverage_geometry
from .defense_perimeter import DefensePerimeter
from .industrial import PlanningBudget
from .planning import overlap, preflight
from .power_grid import plan_connection as power_connection
from .radar_network import (
    radar_core,
    RADAR_SITE_LUA,
    RADAR_POWER_LUA,
    planned_power_watts,
    ENEMY_EXCLUSION,
    MINIMUM_FULL_MAGAZINES,
    _key,
    _payload,
    _finite,
)
from .resource_policy import mining_reservations
from .routing import VECTORS
from .smelting import merge_entities
from .transport import xy, unbuilt_external_keepouts


CHUNK_SIZE = 32
PREFIX = "radar_coverage_"
MAX_TARGET_CHUNKS = 2048
MAX_CANDIDATES = 96
MAX_FULL_ATTEMPTS = 12
MAX_NEW_RADARS = 4
IGNORED_TYPES = {
    "resource",
    "character",
    "corpse",
    "entity-ghost",
    "tile-ghost",
    "item-entity",
    "item-request-proxy",
    "projectile",
    "artillery-projectile",
    "explosion",
    "fire",
    "smoke-with-trigger",
    "particle-source",
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def chunk_at(position):
    x, y = (
        xy(position)
        if isinstance(position, dict) and "position" in position
        else (position["x"], position["y"])
        if isinstance(position, dict)
        else position
    )
    if not all(_finite(v) for v in (x, y)):
        raise ValueError("finite world coordinates required")
    return math.floor(x / CHUNK_SIZE), math.floor(y / CHUNK_SIZE)


def area_chunks(area):
    if (
        len(area) != 2
        or any(len(p) != 2 for p in area)
        or any(not _finite(v) for p in area for v in p)
        or not area[0][0] < area[1][0]
        or not area[0][1] < area[1][1]
    ):
        raise ValueError("positive finite half-open area required")
    # A right/bottom edge exactly on a chunk boundary belongs to the next area,
    # not to the footprint being covered. Negative boundaries use floor too.
    left, top = (math.floor(v / CHUNK_SIZE) for v in area[0])
    right, bottom = (math.ceil(v / CHUNK_SIZE) - 1 for v in area[1])
    if (right - left + 1) * (bottom - top + 1) > MAX_TARGET_CHUNKS:
        raise ValueError("factory chunk footprint exceeds budget")
    return {(x, y) for x in range(left, right + 1) for y in range(top, bottom + 1)}


def radar_chunks(position, radius):
    if type(radius) is not int or not 1 <= radius <= 16:
        raise ValueError("bounded native nearby chunk radius required")
    x, y = chunk_at(position)
    return {(x + dx, y + dy) for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)}


def factory_chunks(builder, obs, board, posts=(), sources=()):
    """Union accepted areas/assets, native owned utilities, tunnels and exits."""
    owners = {}

    def add(cells, owner):
        for p in cells:
            owners.setdefault(p, set()).add(owner)
        if len(owners) > MAX_TARGET_CHUNKS:
            raise ValueError("factory radar target budget exceeded")

    def plan(block, owner):
        if block.get("reserved_area"):
            add(area_chunks(block["reserved_area"]), owner)
        for spec in block.get("entities", []):
            cells = builder._occupied_by_plan([spec])
            add({chunk_at(p) for p in cells}, owner)
        for pair in block.get("underground_pairs", []):
            a, b = (
                tuple(pair["input"] if "input" in pair else pair["first"]),
                tuple(pair["output"] if "output" in pair else pair["last"]),
            )
            dx, dy = VECTORS[pair["direction"]]
            span = pair["span"]
            if type(span) is not int or not 1 <= span <= 32 or b != (a[0] + span * dx, a[1] + span * dy):
                raise ValueError("accepted underground geometry invalid")
            add({chunk_at((a[0] + i * dx, a[1] + i * dy)) for i in range(span + 1)}, owner)
        for port in block.get("ports", []):
            port_exit(port, owner)

    def port_exit(port, owner):
        add({chunk_at(port["position"])}, owner)
        if port.get("facing") in VECTORS and port.get("direction") in ("input", "output"):
            dx, dy = VECTORS[port["facing"]]
            if port["direction"] == "input":
                dx, dy = -dx, -dy
            x, y = xy(port)
            add({chunk_at((x + i * dx, y + i * dy)) for i in range(5)}, owner)

    for name, block in sorted(board.get("blocks", {}).items()):
        # Our added radar itself must not recursively create a new factory goal.
        if not name.startswith(PREFIX):
            plan(block, name)
    bus = board.get("shared_main_bus", {})
    plan(bus, "shared_main_bus")
    for row in bus.get("reservations", []):
        if row.get("area"):
            add(area_chunks(row["area"]), "shared_main_bus_growth")
    for e in obs.get("entities", []):
        if e.get("type") in IGNORED_TYPES or e["name"] == "radar":
            continue
        add({chunk_at(p) for p in builder._occupied_by_plan([e])}, "native_owned:" + e.get("type", e["name"]))
    for p in posts:
        add({chunk_at(p)}, "defense_posts")
    for p in sources:
        port_exit(p, "published_" + p.get("item", "source"))
    return [dict(x=p[0], y=p[1], owners=sorted(v)) for p, v in sorted(owners.items())]


def select_radar_cover(chunks, candidates, *, maximum=MAX_NEW_RADARS):
    """Deterministic greedy set-cover with a final redundant-site removal."""
    missing = set(chunks)
    remaining = list(candidates)
    chosen = []
    while missing and remaining and len(chosen) < maximum:
        best = min(
            remaining,
            key=lambda c: (
                -len(set(map(tuple, c["chunks"])) & missing),
                c.get("power_distance", math.inf),
                c["position"]["x"],
                c["position"]["y"],
            ),
        )
        if not set(map(tuple, best["chunks"])) & missing:
            break
        chosen.append(best)
        remaining.remove(best)
        missing -= set(map(tuple, best["chunks"]))
    for c in reversed(chosen[:]):
        others = set().union(*(set(map(tuple, q["chunks"])) for q in chosen if q is not c))
        if set(chunks) - missing <= others:
            chosen.remove(c)
    return chosen, sorted(missing)


def coverage_candidates(
    obs, board, posts, chunks, radius, *, effective_range=16, hard=(), extra_areas=(), minimum_cover=2
):
    """Two existing gun disks; diversify sites by radar chunk before the cap."""
    if not _finite(effective_range) or not 4 < effective_range <= 64:
        return []
    targets = set(chunks)
    hard = set(hard)
    areas = [q["reserved_area"] for q in board.get("blocks", {}).values() if q.get("reserved_area")] + list(extra_areas)
    areas += [q["area"] for q in board.get("shared_main_bus", {}).get("reservations", []) if q.get("area")]
    poles = [
        q for q in obs["entities"] if q["name"] == "small-electric-pole" and type(q.get("electric_network_id")) is int
    ]
    if not poles:
        return []
    found = {}
    checked = 0
    if minimum_cover not in (1, 2):
        raise ValueError("one planning anchor or two current cover guns required")
    for i, a in enumerate(posts):
        for b in posts[i + 1 :] if minimum_cover == 2 else [a]:
            if math.dist(xy(a), xy(b)) > 2 * (effective_range - 3.5):
                continue
            bound = effective_range - 3.5
            left = math.ceil(max(xy(a)[0], xy(b)[0]) - bound - 0.5)
            right = math.floor(min(xy(a)[0], xy(b)[0]) + bound - 0.5)
            top = math.ceil(max(xy(a)[1], xy(b)[1]) - bound - 0.5)
            bottom = math.floor(min(xy(a)[1], xy(b)[1]) + bound - 0.5)
            for ix in range(left, right + 1):
                for iy in range(top, bottom + 1):
                    p = (ix + 0.5, iy + 0.5)
                    checked += 1
                    if checked > 32768:
                        break
                    if p in found:
                        continue
                    gain = radar_chunks(p, radius) & targets
                    if not gain:
                        continue
                    area = [[p[0] - 3.5, p[1] - 3.5], [p[0] + 3.5, p[1] + 3.5]]
                    if any(overlap(area, q) for q in areas):
                        continue
                    if any((p[0] + dx, p[1] + dy) in hard for dx in range(-3, 4) for dy in range(-3, 4)):
                        continue
                    if any(
                        any(
                            math.hypot(x - xy(q)[0], y - xy(q)[1]) > effective_range
                            for x in (area[0][0], area[1][0])
                            for y in (area[0][1], area[1][1])
                        )
                        for q in (a, b)
                    ):
                        continue
                    found[p] = dict(
                        position={"x": p[0], "y": p[1]},
                        chunks=sorted(gain),
                        power_distance=min(math.dist(p, xy(q)) for q in poles),
                    )
                if checked > 32768:
                    break
            if checked > 32768:
                break
        if checked > 32768:
            break
    groups = {}
    for row in found.values():
        groups.setdefault(chunk_at(row["position"]), []).append(row)
    for key, rows in groups.items():
        rows.sort(key=lambda c: (c["power_distance"], c["position"]["x"], c["position"]["y"]))
        # Adjacent centers beside the same pole/pipe repeat the same service
        # obstruction. Keep nearby four-tile cells diverse before the cap.
        seen = set()
        diverse = []
        rest = []
        for row in rows:
            p = row["position"]
            cell = (math.floor(p["x"] / 4), math.floor(p["y"] / 4))
            if cell in seen:
                rest.append(row)
            else:
                seen.add(cell)
                diverse.append(row)
        groups[key] = diverse + rest
    ordered = sorted(groups, key=lambda k: (-len(set(groups[k][0]["chunks"])), groups[k][0]["power_distance"], k))
    result = []
    for index in range(8):
        for key in ordered:
            if index < len(groups[key]):
                result.append(groups[key][index])
            if len(result) >= MAX_CANDIDATES:
                return result
    return result


RADAR_COVERAGE_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id then return {ok=false,reason="radar_coverage_world_changed"}end
local proto=prototypes.entity.radar
local radius=proto and proto.get_max_distance_of_nearby_sector_revealed("normal")
if type(radius)~="number"or radius%1~=0 or radius<1 or radius>16 or proto.get_max_energy_usage("normal")*60~=x.radar_watts then
 return {ok=false,reason="radar_coverage_native_radius_or_energy_unsupported"}end
local radars={};local working={};local seen={}
for _,q in ipairs(x.radars)do local e=target(q.position,"radar")
 if not e or e.force~=f or e.surface~=s or e.unit_number~=q.unit_number or e.position.x~=q.position.x or e.position.y~=q.position.y
  or e.quality.name~="normal"or seen[e.unit_number]then return {ok=false,reason="radar_coverage_existing_unit_changed"}end
 seen[e.unit_number]=true
 local active=e.is_connected_to_electric_network()and e.energy>0 and e.status==defines.entity_status.working
 local progress=e.radar_scan_progress
 if type(progress)~="number"or progress<0 or progress>1 then return {ok=false,reason="radar_coverage_scan_progress_unsupported"}end
 local row={unit_number=e.unit_number,position=pos(e.position),world_id=d.world_id,surface_index=s.index,force_index=f.index,
  radius_chunks=radius,watts=x.radar_watts,powered_working=active,scan_progress=progress,network_id=e.electric_network_id}
 radars[#radars+1]=row;if active then working[#working+1]=row end
end
local chunks={};local visible=0;local charted=0;local covered=0
for _,q in ipairs(x.chunks)do local by={}
 for _,r in ipairs(working)do if math.abs(math.floor(r.position.x/32)-q.x)<=radius and math.abs(math.floor(r.position.y/32)-q.y)<=radius then by[#by+1]=r.unit_number end end
 local v=f.is_chunk_visible(s,{x=q.x,y=q.y});local c=f.is_chunk_charted(s,{x=q.x,y=q.y})
 if #by>0 then covered=covered+1;if v then visible=visible+1 end end;if c then charted=charted+1 end
 chunks[#chunks+1]={x=q.x,y=q.y,radar_units=by,native_visible=v,charted=c}
end
return {ok=true,tick=game.tick,world_id=d.world_id,surface_index=s.index,force_index=f.index,radius_chunks=radius,
 radar_watts=x.radar_watts,radars=radars,chunks=chunks,target_chunks=#x.chunks,geometric_covered_chunks=covered,
 radar_visible_chunks=visible,charted_chunks=charted,complete=#x.chunks>0 and visible==#x.chunks,
 scope="current powered normal radar footprint and native visible chunks; charted history alone is not coverage"}
"""


class RadarCoverage:
    def __init__(self, game, builder, catalog, perimeter, *, radars=None):
        self.game, self.builder, self.catalog, self.perimeter, self.radars = game, builder, catalog, perimeter, radars
        self.path = Path(game.cfg.runtime_dir) / "radar-coverage.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.last_proof_failure = None
        self._active_block = None

    def _save(self):
        _atomic_json(self.path, self.state)

    def _identity(self, obs, value):
        return (
            value.get("world_id") == obs.get("world_id")
            and value.get("catalog_fingerprint") == self.catalog.fingerprint
        )

    def _context(self, obs):
        board = json.loads((self.path.parent / "pcb-board.json").read_text())
        if not self._identity(obs, board) or (self.state and not self._identity(obs, self.state)):
            raise ValueError("radar coverage world/catalog changed")
        sources = []
        for name in ("material-sources.json", "fluid-sources.json"):
            path = self.path.parent / name
            if path.exists():
                doc = json.loads(path.read_text())
                if not self._identity(obs, doc):
                    raise ValueError("published source registry frame changed")
                sources += list(doc.get("sources", {}).values())
        chunks = factory_chunks(self.builder, obs, board, self.perimeter.state.get("posts", []), sources)
        return board, sources, chunks

    def coverage_report(self, obs, *, _game=None):
        try:
            _, _, chunks = self._context(obs)
            radars = [{k: e[k] for k in ("position", "unit_number")} for e in obs["entities"] if e["name"] == "radar"]
            if len(radars) > 64 or any(type(q["unit_number"]) is not int or q["unit_number"] < 1 for q in radars):
                raise ValueError("bounded exact radar identities required")
            actual = {(e["name"], *xy(e)): e for e in obs["entities"]}
            for q in self.state.get("preserved_radars", []):
                e = actual.get(("radar", *xy(q)))
                if not e or e.get("unit_number") != q["unit_number"]:
                    return {
                        "ok": False,
                        "reason": "radar_coverage_original_radar_missing_or_replaced",
                        "unit_number": q["unit_number"],
                    }
            native = (_game or self.game).query(
                RADAR_COVERAGE_LUA.replace(
                    "PAYLOAD",
                    _payload(
                        dict(
                            world_id=obs["world_id"],
                            radar_watts=self.catalog.entities["radar"]["energy_usage"] * 60,
                            radars=radars,
                            chunks=[dict(x=q["x"], y=q["y"]) for q in chunks],
                        )
                    ),
                )
            )
            if not native.get("ok"):
                return native
            units = {q["unit_number"] for q in radars}
            returned = native.get("radars", [])
            if (
                len(native.get("chunks", [])) != len(chunks)
                or len(returned) != len(radars)
                or {q["unit_number"] for q in returned} != units
                or {(q["x"], q["y"]) for q in native["chunks"]} != {(q["x"], q["y"]) for q in chunks}
                or any(
                    type(q.get("native_visible")) is not bool
                    or type(q.get("charted")) is not bool
                    or not set(q["radar_units"]) <= units
                    for q in native["chunks"]
                )
                or any(type(q.get("powered_working")) is not bool for q in returned)
            ):
                return {"ok": False, "reason": "radar_coverage_native_receipt_incomplete"}
            owner = {(q["x"], q["y"]): q["owners"] for q in chunks}
            for q in native["chunks"]:
                q["owners"] = owner[(q["x"], q["y"])]
            native["uncovered_chunks"] = [q for q in native["chunks"] if not q["radar_units"]]
            native["not_visible_chunks"] = [
                q for q in native["chunks"] if not q["radar_units"] or not q["native_visible"]
            ]
            native["complete"] = bool(chunks) and not native["not_visible_chunks"]
            return native
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "radar_coverage_context_invalid", "detail": str(exc)}

    def _keepouts(self, obs, board, sources, *, ignore=None):
        blocks = {k: v for k, v in board["blocks"].items() if k != ignore}
        cells = unbuilt_external_keepouts(self.builder, obs, blocks)
        areas = [q["reserved_area"] for q in blocks.values() if q.get("reserved_area")]
        bus = board.get("shared_main_bus", {})
        areas += [q["area"] for q in bus.get("reservations", []) if q.get("area")]
        ports = list(sources) + [p for block in list(blocks.values()) + [bus] for p in block.get("ports", [])]
        for p in ports:
            if p.get("facing") not in VECTORS or p.get("direction") not in ("input", "output"):
                continue
            dx, dy = VECTORS[p["facing"]]
            if p["direction"] == "input":
                dx, dy = -dx, -dy
            cells.update((xy(p)[0] + i * dx, xy(p)[1] + i * dy) for i in range(5))
        return cells, areas

    def _geometry(self, obs, board, sources, core, entities=(), *, ignore=None):
        cells, areas = self._keepouts(obs, board, sources, ignore=ignore)
        area = core["reserved_area"]
        if any(overlap(area, q) for q in areas):
            return {"ok": False, "reason": "radar_coverage_reserved_area_conflict"}
        if any(
            area[0][0] < x + 0.49 and x - 0.49 < area[1][0] and area[0][1] < y + 0.49 and y - 0.49 < area[1][1]
            for x, y in cells
        ):
            return {"ok": False, "reason": "radar_coverage_future_transport_or_fixed_exit_conflict"}
        for e in entities:
            occupied = self.builder._occupied_by_plan([e])
            if occupied & cells:
                return {"ok": False, "reason": "radar_coverage_power_crosses_future_transport_or_exit"}
            if any(
                any(
                    x + 0.49 > a[0][0] and x - 0.49 < a[1][0] and y + 0.49 > a[0][1] and y - 0.49 < a[1][1]
                    for x, y in occupied
                )
                for a in areas
            ):
                return {"ok": False, "reason": "radar_coverage_power_crosses_producer_reservation"}
        return {"ok": True}

    def _posts(self):
        rows = [
            {k: q[k] for k in ("name", "position", "unit_number", "ammo_item") if k in q}
            for q in self.perimeter.state.get("posts", [])
            if type(q.get("unit_number")) is int and q["unit_number"] > 0
        ]
        if any(
            q.get("ammo_item", "firearm-magazine") not in ("firearm-magazine", "piercing-rounds-magazine") for q in rows
        ):
            raise ValueError("registered radar cover has an unsupported ammo tier")
        return rows

    def _site(self, game, obs, board, sources, core, *, ignore=None, planning_anchor_only=False):
        _, areas = self._keepouts(obs, board, sources, ignore=ignore)
        # Reuse the proven native land/worm/walk/whole-area two-gun guard. The
        # selected per-post normal ammo tier is immutable, never mixed here.
        script = RADAR_SITE_LUA.replace(
            'stack.name=="firearm-magazine"', 'stack.name==(q.ammo_item or"firearm-magazine")'
        )
        if planning_anchor_only:
            # This is read-only selection of an existing anchor, never the
            # deployment/proof gate. A distinct second paid gun is still needed.
            script = script.replace("if #armed<2 then", "if #armed<1 then")
        return game.query(
            script.replace(
                "PAYLOAD",
                _payload(
                    dict(
                        world_id=obs["world_id"],
                        core=core,
                        posts=self._posts(),
                        zones=mining_reservations(self.game, obs["world_id"]),
                        foreign_areas=areas,
                        enemy_radius=ENEMY_EXCLUSION,
                        minimum_full_magazines=MINIMUM_FULL_MAGAZINES,
                    )
                ),
            )
        )

    def _power(self, game, obs, board, row, remaining):
        external = dict(board, blocks={k: v for k, v in board["blocks"].items() if not k.startswith(PREFIX)})
        return game.query(
            RADAR_POWER_LUA.replace(
                "PAYLOAD",
                _payload(
                    dict(
                        world_id=obs["world_id"],
                        source=row["power_source"],
                        preserved_poles=row["preserved_poles"],
                        scheduled_watts=planned_power_watts(self.catalog, obs, external),
                        additional_watts=remaining * self.catalog.entities["radar"]["energy_usage"] * 60,
                    )
                ),
            )
        )

    def _pair_preview(self, game, obs, core, plan, site, scope_id):
        """Standard RO coverage planner, excluding just one existing anchor."""
        anchors = site.get("covering_posts", [])
        if len(anchors) >= 2:
            return {"ok": True, "required": False}
        if len(anchors) != 1:
            return {"ok": False, "reason": "radar_coverage_one_exact_existing_anchor_required"}
        original = deepcopy(self.perimeter.state)
        anchor = anchors[0]
        known = next(
            (
                q
                for q in original["posts"]
                if q.get("unit_number") == anchor["unit_number"] and q.get("position") == anchor["position"]
            ),
            None,
        )
        if known is None:
            return {"ok": False, "reason": "radar_coverage_anchor_registration_changed"}
        clone = DefensePerimeter(game, self.perimeter.bootstrap, self.catalog)
        clone.state = deepcopy(original)
        clone.state["posts"] = [q for q in clone.state["posts"] if q["name"] != known["name"]]
        base_reservations = clone._coverage_reservations
        cells = self.builder._occupied_by_plan(plan["entities"])
        x, y = xy(known)
        protected = [[[px - 0.5, py - 0.5], [px + 0.5, py + 0.5]] for px, py in cells] + [
            [[x - 2, y - 2], [x + 2, y + 2]]
        ]
        clone._coverage_reservations = lambda observation, areas: base_reservations(observation, areas) + protected
        preview = clone.prepare_coverage(obs, scope_id, [core["reserved_area"]])
        if not preview.get("ok"):
            return preview
        posts = preview.get("new_posts", [])
        radius = preview["scope"]["effective_range"]
        area = core["reserved_area"]

        def covers(q):
            return all(
                math.hypot(x - xy(q)[0], y - xy(q)[1]) <= radius
                for x in (area[0][0], area[1][0])
                for y in (area[0][1], area[1][1])
            )

        if len(posts) != 1 or not covers(known) or not covers(posts[0]) or math.dist(xy(known), xy(posts[0])) < 3:
            return {"ok": False, "reason": "radar_coverage_second_distinct_gun_must_cover_whole_core"}
        scope = deepcopy(preview["scope"])
        if known["name"] not in scope["covering_posts"]:
            scope["covering_posts"].append(known["name"])
        scope["geometry"] = coverage_geometry(scope["cells"], original["posts"] + posts, radius)
        return dict(
            ok=True,
            required=True,
            scope_id=scope_id,
            scope=scope,
            new_posts=posts,
            base_state=original,
            base_revision=_hash(original),
            anchor=known,
            plan=_plan([entity("gun-turret", *xy(q)) for q in posts], []),
            requirements=preview.get("requirements"),
        )

    def prepare(self, obs, parameters=None):
        if parameters not in (None, {}):
            raise ValueError("radar_coverage accepts no coordinates or parameters")
        if not obs.get("technologies", {}).get("radar") or not all(
            obs.get("enabled_recipes", {}).get(k) for k in ("radar", "small-electric-pole")
        ):
            return {"ok": False, "reason": "native_radar_research_and_normal_recipes_required"}
        try:
            budget = PlanningBudget(self.game)
            board, sources, targets = self._context(obs)
            report = self.coverage_report(obs, _game=budget.game)
            if not report.get("ok"):
                return report
            if self.state and self.state.get("phase") != "verified":
                return {"ok": False, "reason": "radar_coverage_existing_build_must_resume"}
            missing = {(q["x"], q["y"]) for q in report["uncovered_chunks"]}
            previous = deepcopy(self.state.get("radars", []))
            chosen = []
            failures = []
            attempts = 0
            profile = budget.game.query(
                READY_POSTS_LUA.replace("PAYLOAD", _payload(dict(world_id=obs["world_id"], posts=self._posts())))
            )
            if not profile.get("ok"):
                return profile
            cells, areas = self._keepouts(obs, board, sources)
            candidates = coverage_candidates(
                obs,
                board,
                profile["posts"],
                missing,
                report["radius_chunks"],
                effective_range=profile["effective_range"],
                hard=cells,
            )
            legal_count = 0

            def land(rows):
                nonlocal legal_count
                survey = (
                    budget.game.query(
                        CANDIDATE_LAND_LUA.replace(
                            "PAYLOAD",
                            _payload(
                                dict(
                                    world_id=obs["world_id"],
                                    cores=[radar_core(self.catalog, **q["position"]) for q in rows],
                                    zones=mining_reservations(self.game, obs["world_id"]),
                                )
                            ),
                        )
                    )
                    if rows
                    else {"ok": True, "legal": []}
                )
                if not survey.get("ok"):
                    return [], survey
                legal = survey.get("legal", [])
                if len(legal) != len(set(legal)) or any(type(i) is not int or not 1 <= i <= len(rows) for i in legal):
                    raise ValueError("invalid radar candidate survey receipt")
                legal_count += len(legal)
                return [q for i, q in enumerate(rows, 1) if i in legal], None

            candidates, error = land(candidates)
            if error:
                return error
            fallback = False
            while missing and len(chosen) < MAX_NEW_RADARS and attempts < MAX_FULL_ATTEMPTS:
                if budget.expired():
                    break
                candidates = [q for q in candidates if set(map(tuple, q["chunks"])) & missing]
                if not candidates and not fallback:
                    # Do not let already covered base groups consume every
                    # single-anchor slot. Generate that finite family only for
                    # the remaining gaps, under the SAME time/attempt budget.
                    fallback = True
                    single = coverage_candidates(
                        obs,
                        board,
                        profile["posts"],
                        missing,
                        report["radius_chunks"],
                        effective_range=profile["effective_range"],
                        hard=cells,
                        minimum_cover=1,
                    )
                    candidates, error = land([dict(q, needs_pair=True) for q in single])
                    if error:
                        return error
                if not candidates:
                    break
                ranked, _ = select_radar_cover(missing, candidates, maximum=1)
                if not ranked:
                    break
                c = ranked[0]
                candidates.remove(c)
                attempts += 1
                core = radar_core(self.catalog, **c["position"])
                prior_es = [
                    e
                    for r in chosen
                    for e in r["plan"]["entities"] + r.get("defense", {}).get("plan", {}).get("entities", [])
                ]
                if any(
                    overlap(core["reserved_area"], r["core"]["reserved_area"]) for r in chosen
                ) or self.builder._occupied_by_plan(core["entities"]) & self.builder._occupied_by_plan(prior_es):
                    continue
                site = self._site(
                    budget.game, obs, board, sources, core, planning_anchor_only=bool(c.get("needs_pair"))
                )
                if not site.get("ok"):
                    failures.append(dict(stage="site", position=c["position"], reason=site.get("reason")))
                    continue
                bridge = power_connection(
                    budget.game,
                    self.builder,
                    obs,
                    core["power_pole"]["position"],
                    merge_entities([core["radar"]], core["service_keepouts"], prior_es),
                )
                if not bridge.get("ok"):
                    failures.append(dict(stage="power_route", position=c["position"], reason=bridge.get("reason")))
                    continue
                actual = {(q["name"], *xy(q)): q for q in obs["entities"]}
                source_live = next(
                    (
                        q
                        for q in obs["entities"]
                        if q["name"] == "small-electric-pole"
                        and q.get("unit_number") == bridge.get("source_unit_number")
                        and q["position"] == bridge.get("source_position")
                    ),
                    None,
                )
                if not source_live or type(source_live.get("electric_network_id")) is not int:
                    failures.append(dict(stage="power_source", position=c["position"]))
                    continue

                def pin(q):
                    return {
                        k: q[k] for k in ("name", "position", "direction", "unit_number", "electric_network_id")
                    } | {"world_id": obs["world_id"]}

                source = pin(source_live)
                preserved = [source]
                poles = []
                valid = True
                for q in bridge["entities"]:
                    live = actual.get((q["name"], *xy(q)))
                    if live:
                        if live.get("electric_network_id") != source["electric_network_id"]:
                            valid = False
                            break
                        preserved.append(pin(live))
                    else:
                        poles.append(q)
                if not valid:
                    failures.append(dict(stage="power_network", position=c["position"]))
                    continue
                es = merge_entities(core["entities"], poles)
                geometry = self._geometry(obs, board, sources, core, poles)
                if not geometry.get("ok"):
                    failures.append(dict(stage="geometry", position=c["position"], reason=geometry["reason"]))
                    continue
                row = dict(core=core, power_source=source, preserved_poles=preserved, bindings={})
                power = self._power(budget.game, obs, board, row, len(chosen) + 1)
                if not power.get("ok"):
                    failures.append(dict(stage="power_budget", position=c["position"], reason=power.get("reason")))
                    continue
                if type(power.get("force_index")) is not int:
                    raise ValueError("native radar source force required")
                for p in preserved:
                    p["force_index"] = power["force_index"]
                scope_id = "radar:coverage:" + str(len(previous) + len(chosen) + 1)
                defense = self._pair_preview(budget.game, obs, core, _plan(es, []), site, scope_id)
                if not defense.get("ok"):
                    failures.append(dict(stage="second_gun", position=c["position"], reason=defense.get("reason")))
                    continue
                gun_es = defense.get("plan", {}).get("entities", [])
                whole = preflight(budget.game, self.builder, obs, merge_entities(prior_es, es, gun_es))
                if not whole.get("ok"):
                    failures.append(dict(stage="whole", position=c["position"], reason=whole.get("reason")))
                    continue
                row.update(
                    block=PREFIX + str(len(previous) + len(chosen) + 1),
                    chunks=c["chunks"],
                    power_preview=power,
                    site_preview=site,
                    defense=defense,
                    plan=_plan(
                        es,
                        [],
                        reserved_area=core["reserved_area"],
                        layout_policy="paid-factory-radar-coverage-v1",
                        clear_obstacles=whole["clear_obstacles"],
                        clearance_world_id=obs["world_id"],
                    ),
                )
                chosen.append(row)
                missing -= radar_chunks(c["position"], report["radius_chunks"])
            diagnostics = dict(
                candidate_count=legal_count,
                full_attempts=attempts,
                failures=failures[-6:],
                remaining_chunks=sorted(missing),
                existing_radars=len(report["radars"]),
                currently_visible=report["radar_visible_chunks"],
                target_chunks=len(targets),
                ready_registered_posts=len(profile["posts"]),
                planning_queries=budget.queries,
            )
            if missing and not chosen:
                return dict(
                    ok=False,
                    reason="radar_coverage_no_safe_paired_powered_service_site",
                    evidence=diagnostics,
                    coverage=report,
                )
            preserved = [dict(position=q["position"], unit_number=q["unit_number"]) for q in report["radars"]]
            # Full target geometry remains dynamic; accepted factory growth will
            # be noticed by the next prepare/upkeep without moving old radars.
            return dict(
                ok=True,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                phase="build",
                radars=previous + chosen,
                preserved_radars=preserved,
                baseline_report=report,
                target_revision=_hash(targets),
                radius_chunks=report["radius_chunks"],
                expected_complete=not missing,
                diagnostics=diagnostics,
                prepared_tick=obs["tick"],
                foreign_board_revision=_hash(
                    dict(board, blocks={k: v for k, v in board["blocks"].items() if not k.startswith(PREFIX)})
                ),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": "radar_coverage_preparation_invalid", "detail": str(exc)}

    def _bind(self, obs):
        actual = {(q["name"], *xy(q)): q for q in obs["entities"]}
        for row in self.state["radars"]:
            for spec in row["plan"]["entities"]:
                live = actual.get((spec["name"], *xy(spec)))
                unit = row["bindings"].get(_key(spec))
                if unit and (not live or live.get("unit_number") != unit):
                    return {
                        "status": "blocked",
                        "reason": "radar_coverage_paid_asset_missing_or_replaced",
                        "entity": spec,
                    }
                if live:
                    if type(live.get("unit_number")) is not int or live.get("direction") != spec.get("direction", 0):
                        return {
                            "status": "blocked",
                            "reason": "radar_coverage_asset_configuration_changed",
                            "entity": spec,
                        }
                    row["bindings"][_key(spec)] = live["unit_number"]
        return None

    def _reserve(self, obs, board, sources):
        foreign = _hash(dict(board, blocks={k: v for k, v in board["blocks"].items() if not k.startswith(PREFIX)}))
        if self.state.get("foreign_board_revision") and foreign != self.state["foreign_board_revision"]:
            return {"status": "blocked", "reason": "radar_coverage_foreign_board_changed_before_construction"}
        changed = False
        for row in self.state["radars"]:
            name = row["block"]
            plan = row["plan"]
            old = board["blocks"].get(name)
            if old and old != plan:
                return {"status": "blocked", "reason": "radar_coverage_canonical_plan_changed"}
            check = self._geometry(
                obs,
                board,
                sources,
                row["core"],
                [q for q in plan["entities"] if q != row["core"]["radar"]],
                ignore=name,
            )
            if not check.get("ok"):
                return dict(check, status="blocked")
            if not old:
                board["blocks"][name] = deepcopy(plan)
                changed = True
        if changed:
            all_entities = []
            for row in self.state["radars"]:
                defense = row.get("defense", {})
                site = self._site(
                    self.game,
                    obs,
                    board,
                    sources,
                    row["core"],
                    ignore=row["block"],
                    planning_anchor_only=bool(defense.get("required")),
                )
                if not site.get("ok"):
                    return {
                        "status": "blocked",
                        "reason": "radar_coverage_service_changed_before_reservation",
                        "evidence": site,
                    }
                all_entities += row["plan"]["entities"] + defense.get("plan", {}).get("entities", [])
            whole = preflight(self.game, self.builder, obs, merge_entities(all_entities))
            if not whole.get("ok"):
                return dict(whole, status="blocked")
            _atomic_json(self.path.parent / "pcb-board.json", board)
        return None

    def _adopt_pair(self, obs, row):
        defense = row.get("defense", {})
        if not defense.get("required"):
            return None
        path = self.path.parent / "defense-perimeter.json"
        disk = json.loads(path.read_text())
        if not self._identity(obs, disk):
            return {"status": "blocked", "reason": "radar_coverage_perimeter_frame_changed"}
        if defense.get("published"):
            scope = disk.get("coverage_scopes", {}).get(defense["scope_id"])
            if not scope or any(
                scope.get(k) != defense["scope"].get(k)
                for k in ("areas", "cells", "effective_range", "covering_posts", "new_posts")
            ):
                return {"status": "blocked", "reason": "radar_coverage_adopted_scope_changed"}
            self.perimeter.state = disk
            return None
        if not defense.get("publication_intent"):
            base = defense["base_state"]
            # Only this controller's previously CAS-published scopes may extend
            # the original prefix. Every pre-existing post/scope stays exact.
            owned = [
                r["defense"]
                for r in self.state["radars"]
                if r is not row
                and r.get("defense", {}).get("published")
                and r["defense"]["scope_id"] not in base.get("coverage_scopes", {})
            ]
            post_specs = {p["name"]: p for q in owned for p in q["new_posts"]}
            scope_specs = {q["scope_id"]: q["scope"] for q in owned}
            extras = disk.get("posts", [])[len(base["posts"]) :]
            scopes = disk.get("coverage_scopes", {})
            old_scopes = base.get("coverage_scopes", {})
            if (
                _hash(base) != defense["base_revision"]
                or disk.get("posts", [])[: len(base["posts"])] != base["posts"]
                or {k: v for k, v in disk.items() if k not in ("posts", "coverage_scopes")}
                != {k: v for k, v in base.items() if k not in ("posts", "coverage_scopes")}
                or any(scopes.get(k) != v for k, v in old_scopes.items())
                or set(scopes) - set(old_scopes) != set(scope_specs)
                or {p["name"] for p in extras} != set(post_specs)
                or len(extras) != len(post_specs)
                or any(
                    {k: v for k, v in p.items() if k not in ("unit_number", "commissioned")}
                    != {k: v for k, v in post_specs[p["name"]].items() if k != "commissioned"}
                    for p in extras
                )
                or any(
                    {k: v for k, v in scopes[k].items() if k not in ("commissioned", "commissioned_tick")}
                    != {k: v for k, v in scope_specs[k].items() if k not in ("commissioned", "commissioned_tick")}
                    for k in scope_specs
                )
            ):
                return {"status": "blocked", "reason": "radar_coverage_perimeter_changed_before_pair_adoption"}
            new = deepcopy(disk)
            names = {q["name"] for q in new["posts"]}
            if defense["scope_id"] in new.get("coverage_scopes", {}) or any(
                q["name"] in names for q in defense["new_posts"]
            ):
                return {"status": "blocked", "reason": "radar_coverage_pair_registration_conflict"}
            new["posts"] += deepcopy(defense["new_posts"])
            new.setdefault("coverage_scopes", {})[defense["scope_id"]] = deepcopy(defense["scope"])
            defense["publication_intent"] = dict(old_hash=_hash(disk), new_hash=_hash(new), value=new)
            self._save()
        intent = defense["publication_intent"]
        revision = _hash(disk)
        if revision not in (intent["old_hash"], intent["new_hash"]):
            return {"status": "blocked", "reason": "radar_coverage_pair_compare_and_swap_failed"}
        if revision == intent["old_hash"]:
            _atomic_json(path, intent["value"])
        self.perimeter.state = deepcopy(intent["value"])
        defense["published"] = True
        self._save()
        return None

    def step(self, obs, parameters=None):
        if parameters not in (None, {}):
            raise ValueError("radar_coverage accepts no coordinates or parameters")
        report = None
        if self.state.get("phase") == "verified":
            report = self.coverage_report(obs)
            if not report.get("ok"):
                return dict(report, status="blocked")
            board, sources, _ = self._context(obs)
            issue = self._owned_health(obs, board, sources, report)
            if issue:
                return issue
            if report["complete"]:
                return {
                    "status": "succeeded",
                    "reason": "all accepted factory chunks have current radar visibility",
                    "evidence": report,
                }
        if not self.state or self.state.get("phase") == "verified":
            ready = self.prepare(obs)
            if not ready.get("ok"):
                return dict(ready, status="blocked")
            self.state = ready
            self._save()
        try:
            board, sources, _ = self._context(obs)
            report = self.coverage_report(obs)
            if not report.get("ok"):
                return dict(report, status="blocked")
            issue = self._bind(obs)
            if issue:
                return issue
            self._save()
            issue = self._reserve(obs, board, sources)
            if issue:
                return issue
            remaining = sum(_key(r["core"]["radar"]) not in r["bindings"] for r in self.state["radars"])
            for row in self.state["radars"]:
                self._active_block = row["block"]
                defense = row.get("defense", {})
                if defense.get("required"):
                    # Original planner/current world/catalog and combined paid
                    # radar+gun+power preflight must still hold before adoption.
                    combined = merge_entities(row["plan"]["entities"], defense["plan"]["entities"])
                    hard, areas = self._keepouts(obs, board, sources)
                    for spec in defense["plan"]["entities"]:
                        occupied = self.builder._occupied_by_plan([spec])
                        if occupied & hard or any(
                            any(
                                x + 0.49 > a[0][0] and x - 0.49 < a[1][0] and y + 0.49 > a[0][1] and y - 0.49 < a[1][1]
                                for x, y in occupied
                            )
                            for a in areas
                        ):
                            return {
                                "status": "blocked",
                                "reason": "radar_coverage_second_gun_future_factory_conflict",
                                "position": spec["position"],
                            }
                    check = preflight(self.game, self.builder, obs, combined)
                    if not check.get("ok"):
                        return dict(check, status="blocked")
                    anchor = self.game.query(
                        READY_POSTS_LUA.replace(
                            "PAYLOAD", _payload(dict(world_id=obs["world_id"], posts=[defense["anchor"]]))
                        )
                    )
                    if (
                        not anchor.get("ok")
                        or len(anchor.get("posts", [])) != 1
                        or anchor["posts"][0]["unit_number"] != defense["anchor"]["unit_number"]
                    ):
                        return {
                            "status": "waiting",
                            "reason": "radar_coverage_restore_exact_original_armed_anchor_first",
                            "evidence": anchor,
                        }
                    result = self._adopt_pair(obs, row)
                    if result:
                        return result
                    result = self.perimeter.ensure_coverage(obs, defense["scope_id"], [row["core"]["reserved_area"]])
                    if result is not None:
                        return result
                site = self._site(self.game, obs, board, sources, row["core"], ignore=row["block"])
                if not site.get("ok"):
                    return {
                        "status": "waiting",
                        "reason": "radar_coverage_restore_existing_local_security_first",
                        "evidence": site,
                    }
                power = self._power(self.game, obs, board, row, remaining)
                if not power.get("ok"):
                    return {
                        "status": "waiting",
                        "reason": "radar_coverage_restore_native_power_headroom_first",
                        "evidence": power,
                    }
                whole = preflight(self.game, self.builder, obs, row["plan"]["entities"])
                if not whole.get("ok"):
                    return dict(whole, status="blocked")
                result = self.builder.ensure_plan(obs, row["plan"])
                if result.get("status") != "succeeded":
                    return result
            report = self.coverage_report(obs)
            if not report.get("ok"):
                return dict(report, status="blocked")
            # Require all normally paid new bodies to be working even if other
            # radars happen to make their footprints redundant in the meantime.
            active = {q["unit_number"] for q in report["radars"] if q["powered_working"]}
            if any(r["bindings"].get(_key(r["core"]["radar"])) not in active for r in self.state["radars"]):
                return {
                    "status": "waiting",
                    "reason": "waiting_for_every_paid_radar_to_scan_on_native_power",
                    "evidence": report,
                }
            self.state["phase"] = "verified"
            self.state["last_report"] = report
            self._save()
            if not report["complete"]:
                return {
                    "status": "blocked",
                    "reason": "radar_coverage_remaining_factory_chunks_need_more_safe_service_sites",
                    "evidence": report,
                    "planning": self.state["diagnostics"],
                }
            return {
                "status": "succeeded",
                "reason": "all accepted factory chunks have current radar visibility",
                "evidence": report,
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "blocked", "reason": "radar_coverage_live_context_changed", "detail": str(exc)}

    def proof(self, obs, parameters=None):
        if parameters not in (None, {}):
            raise ValueError("radar_coverage accepts no coordinates or parameters")
        report = self.coverage_report(obs)
        self.last_proof_failure = None
        if not report.get("ok") or not report.get("complete"):
            self.last_proof_failure = report
            return None
        try:
            board, sources, _ = self._context(obs)
            if self.state and self.state.get("phase") != "verified":
                self.last_proof_failure = {"reason": "radar_coverage_paid_extension_not_commissioned"}
                return None
            issue = self._owned_health(obs, board, sources, report)
            if issue:
                self.last_proof_failure = issue
                return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.last_proof_failure = {"reason": "radar_coverage_proof_context_changed", "detail": str(exc)}
            return None
        return report

    def _owned_health(self, obs, board, sources, report):
        actual = {(q["name"], *xy(q)): q for q in obs["entities"]}
        active = {q["unit_number"] for q in report["radars"] if q["powered_working"]}
        for row in self.state.get("radars", []):
            if board["blocks"].get(row["block"]) != row["plan"]:
                return {"status": "blocked", "reason": "radar_coverage_owned_plan_changed"}
            geometry = self._geometry(
                obs,
                board,
                sources,
                row["core"],
                [q for q in row["plan"]["entities"] if q != row["core"]["radar"]],
                ignore=row["block"],
            )
            if not geometry.get("ok"):
                return dict(geometry, status="blocked")
            for spec in row["plan"]["entities"]:
                e = actual.get((spec["name"], *xy(spec)))
                unit = row["bindings"].get(_key(spec))
                if not unit or not e or e.get("unit_number") != unit or e.get("direction") != spec.get("direction", 0):
                    return {
                        "status": "blocked",
                        "reason": "radar_coverage_bound_paid_asset_missing_or_changed",
                        "entity": spec,
                    }
            if row["bindings"].get(_key(row["core"]["radar"])) not in active:
                return {"status": "waiting", "reason": "radar_coverage_paid_radar_not_working"}
            site = self._site(self.game, obs, board, sources, row["core"], ignore=row["block"])
            if not site.get("ok"):
                return {
                    "status": "waiting",
                    "reason": "radar_coverage_local_security_or_access_missing",
                    "evidence": site,
                }
            power = self._power(self.game, obs, board, row, 0)
            if not power.get("ok"):
                return {
                    "status": "waiting",
                    "reason": "radar_coverage_native_power_headroom_missing",
                    "evidence": power,
                }
        return None

    def upkeep(self, obs):
        """Ordinary actions for known coverage; notices newly registered growth."""
        return self.step(obs)


READY_POSTS_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id then return {ok=false,reason="radar_coverage_defense_world_changed"}end
local range=prototypes.entity["gun-turret"].attack_parameters.range-2
if type(range)~="number"or range<=4 or range>64 then return {ok=false,reason="radar_coverage_defense_range_unsupported"}end
local rows={};local seen={}
for _,q in ipairs(x.posts)do local item=q.ammo_item or"firearm-magazine"
 if item~="firearm-magazine"and item~="piercing-rounds-magazine"then return {ok=false,reason="radar_coverage_post_ammo_tier_unsupported"}end
 local e=target(q.position,"gun-turret")
 if e and e.force==f and e.surface==s and e.unit_number==q.unit_number and not seen[e.unit_number]and e.quality.name=="normal"
  and e.position.x==q.position.x and e.position.y==q.position.y and e.health>=e.max_health then
  seen[e.unit_number]=true;local inv=e.get_inventory(defines.inventory.turret_ammo);local full=0
  if inv then for i=1,#inv do local st=inv[i];if st.valid_for_read and st.name==item and st.quality.name=="normal"then
   full=full+st.count-(st.ammo<st.prototype.magazine_size and 1 or 0)
  end end end
  if full>=5 then rows[#rows+1]={name=q.name,unit_number=e.unit_number,position=pos(e.position),full_magazines=full,ammo_item=item}end
 end
end
return {ok=true,tick=game.tick,effective_range=range,posts=rows}
"""


CANDIDATE_LAND_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id then return {ok=false,reason="radar_coverage_land_world_changed"}end
local function overlaps(a,b)return a[1][1]<b[2][1]and a[2][1]>b[1][1]and a[1][2]<b[2][2]and a[2][2]>b[1][2]end
local legal={}
for i,c in ipairs(x.cores)do local good=s.count_entities_filtered{area=c.reserved_area,type="resource"}==0
 for _,z in ipairs(x.zones)do if overlaps(c.reserved_area,z.area)then good=false;break end end
 if good then for _,q in ipairs(c.entities)do if not s.can_place_entity{name=q.name,position=q.position,direction=q.direction or 0,force=f,
  build_check_type=defines.build_check_type.manual_ghost,forced=true}then good=false;break end end end
 if good then legal[#legal+1]=i end
end
return {ok=true,legal=legal}
"""
