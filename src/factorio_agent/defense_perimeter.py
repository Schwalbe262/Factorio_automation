"""Proactive strategic gun posts, normally paid ammunition and bounded upkeep.

The initial perimeter is an actor-supplied bootstrap. It is not an automatic
ammunition factory or evidence of a framework combat victory.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re

from factorio_agent.core.deterministic_state import _atomic_json
from .defense_coverage import COVERAGE_MARGIN, coverage_cells, coverage_report, native_candidate_survey, select_cover
from .defense_policy import outside_plan, plan_exclusions


AMMO_TARGET = 10
REFILL_BELOW = 5
# Risk level (``defense_policy.assess_risk``) at which other jobs' background
# step adds posts at once; below it new posts wait for a ``defense_growth`` node.
URGENT_RISK_LEVEL = 2
SITE_PLAN_FILE = "site-plan.json"
# Development-map layout: used only on a world without a site plan
# (``_legacy_prepare``, ``_coverage_reservations``); never on a planned world.
POSTS = (
    ("west_smelter", (-50.5, -26.5), (-44, -26)),
    ("north_smelter", (-32.5, -43.5), (-32, -32)),
    ("coal_south", (-54.5, 31.5), (-56, 17)),
    ("main_bus_middle", (-20.5, -65.5), (-11.5, -65.5)),
    ("iron_north", (14.5, -81.5), (16.5, -71.5)),
    ("steam_south", (43.5, 18.5), (38, 8)),
    ("labs_north", (32.5, -127.5), (32.5, -111.5)),
    ("compact_green_east", (77.5, -68.5), (65.5, -68.5)),
)
FUTURE_AREAS = (
    [[-16.5, -119.5], [-1.5, -10.5]],
    [[39, -82], [71, -54]],
)


def _point(pair):
    return {"x": pair[0], "y": pair[1]}


def _result(status, reason, **evidence):
    return {"status": status, "reason": reason, "evidence": evidence}


def _plan_box(rect):
    # Site rects are half-open [x0, y0, x1, y1] tiles; defense boxes are [[x0, y0], [x1, y1]].
    return [[rect[0], rect[1]], [rect[2], rect[3]]]


def site_plan_areas(root, world_id):
    """Planned land of this world's site plan, or ``None`` when the world has no site service.

    Rows ``{"kind", "name", "area"}``: every zone (bus band, smelting, power,
    mining, outposts, rail stations), district strips (growth slots),
    corridors and allocations with their tap bands. Read-only.
    """
    path = Path(root) / SITE_PLAN_FILE
    if not path.exists():
        return None
    stored = json.loads(path.read_text(encoding="utf-8"))
    if stored.get("world_id") != world_id:
        return None
    plan = stored.get("plan") or {}
    rows = [{"kind": z["kind"], "name": z["name"], "area": _plan_box(z["rect"])} for z in plan.get("zones", [])]
    rows += [
        {"kind": "strip", "name": "strip:" + s["side"], "area": _plan_box(s["rect"])} for s in plan.get("strips", [])
    ]
    rows += [
        {"kind": "corridor", "name": c["name"], "area": _plan_box(segment["rect"])}
        for c in plan.get("corridors", [])
        for segment in c.get("segments", [])
    ]
    for a in plan.get("allocations", []):
        rows.append({"kind": "allocation", "name": a["name"], "area": _plan_box(a["rect"])})
        if a.get("tap_rect"):
            rows.append({"kind": "corridor", "name": a["name"] + ":tap", "area": _plan_box(a["tap_rect"])})
    return rows


def growth_receipt(root, obs):
    """Observe receipt ``defense_growth``: does the current risk want more posts than the policy holds?

    Read-only; ``obs["adaptive_defense_risk"]`` is the observe-time survey.
    ``None`` while there is no risk or the perimeter state belongs to another world.
    """
    risk = obs.get("adaptive_defense_risk")
    if not isinstance(risk, dict) or type(risk.get("minimum_posts")) is not int:
        return None
    path = Path(root) / "defense-perimeter.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state and state.get("world_id") != obs.get("world_id"):
        return None
    active = len(state.get("adaptive_policy", {}).get("active_posts", []))
    target = risk["minimum_posts"]
    return {
        "grow": target > active,
        "target_posts": target,
        "active_posts": active,
        "risk": risk.get("risk"),
        "level": risk.get("level"),
        "evolution_factor": risk.get("evolution_factor"),
        "nearest_spawner": risk.get("nearest_spawner"),
        "nearest_unit": risk.get("nearest_unit"),
        "reason": risk.get("sizing", "")[:200],
    }


def protected_factory_areas(game, world_id, catalog_fingerprint, *, runtime_dir=None):
    """Protect future blocks, external planned hardware and fixed-port exits."""
    from factorio_agent.core.deterministic_builder import FactoryBuilder

    root = runtime_dir if runtime_dir is not None else getattr(getattr(game, "cfg", None), "runtime_dir", None)
    if root is None:
        return []
    root = Path(root)
    path = root / "pcb-board.json"
    if not path.exists():
        return []
    board = json.loads(path.read_text(encoding="utf-8"))
    if board.get("world_id") != world_id or board.get("catalog_fingerprint") != catalog_fingerprint:
        raise ValueError("defense_board_identity_changed")
    blocks = list(board.get("blocks", {}).values())
    bus = board.get("shared_main_bus", {})
    areas = [deepcopy(b["reserved_area"]) for b in blocks if b.get("reserved_area")]
    areas += [deepcopy(r["area"]) for r in bus.get("reservations", []) if r.get("area")]
    geometry = FactoryBuilder.__new__(FactoryBuilder)
    cells = geometry._occupied_by_plan([e for b in blocks + [bus] for e in b.get("entities", [])])
    ports = [p for b in blocks + [bus] for p in b.get("ports", [])]
    for filename in ("material-sources.json", "fluid-sources.json"):
        registry_path = root / filename
        if not registry_path.exists():
            continue
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        if registry.get("world_id") != world_id or registry.get("catalog_fingerprint") != catalog_fingerprint:
            raise ValueError("defense_source_registry_identity_changed")
        ports += list(registry.get("sources", {}).values())
    vectors = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
    for port in ports:
        if port.get("facing") not in vectors or port.get("direction") not in ("input", "output"):
            continue
        p = port["position"]
        dx, dy = vectors[port["facing"]]
        if port["direction"] == "input":
            dx, dy = -dx, -dy
        containing = [a for a in areas if a[0][0] <= p["x"] <= a[1][0] and a[0][1] <= p["y"] <= a[1][1]]
        length = 2
        for step in range(1, 5):
            q = {"x": p["x"] + step * dx, "y": p["y"] + step * dy}
            if containing and not any(
                q["x"] + 0.49 > a[0][0]
                and q["x"] - 0.49 < a[1][0]
                and q["y"] + 0.49 > a[0][1]
                and q["y"] - 0.49 < a[1][1]
                for a in containing
            ):
                length = max(length, step)
                break
        cells.update((p["x"] + i * dx, p["y"] + i * dy) for i in range(length + 1))
    rows = {}
    for x, y in cells:
        rows.setdefault(y, set()).add(x)
    active = {}
    last = None
    for y, xs in sorted(rows.items()):
        if last is not None and y != last + 1:
            areas.extend(active.values())
            active = {}
        runs = []
        for x in sorted(xs):
            if runs and x == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], x)
            else:
                runs.append((x, x))
        remaining = {}
        for lo, hi in runs:
            run = (lo, hi)
            rectangle = active.pop(run, None)
            if rectangle:
                rectangle[1][1] = y + 0.5
            else:
                rectangle = [[lo - 0.5, y - 0.5], [hi + 0.5, y + 0.5]]
            remaining[run] = rectangle
        areas.extend(active.values())
        active = remaining
        last = y
    areas.extend(active.values())
    return areas


class DefensePerimeter:
    def __init__(self, game, bootstrap, catalog):
        self.game, self.bootstrap, self.catalog = game, bootstrap, catalog
        self.ammo_source = None
        self.path = Path(game.cfg.runtime_dir) / "defense-perimeter.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        from .defense_policy import DefensePolicy

        self.adaptive_enabled = True
        self.policy_engine = DefensePolicy(game, catalog)

    def _identity_error(self, obs):
        if not obs.get("ok", True) or not isinstance(obs.get("world_id"), str) or not obs["world_id"]:
            return "defense_requires_valid_world_observation"
        if self.state and (
            self.state.get("world_id") != obs["world_id"]
            or self.state.get("catalog_fingerprint") != self.catalog.fingerprint
        ):
            return "defense_perimeter_world_or_catalog_changed"
        return None

    def _plan_areas(self, obs):
        """This world's planned land (``site_plan_areas``); ``None`` without a site service."""
        return site_plan_areas(self.path.parent, obs.get("world_id"))

    def prepare(self, obs):
        error = self._identity_error(obs)
        if error:
            return {"ok": False, "reason": error}
        try:
            plan = self._plan_areas(obs)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return {"ok": False, "reason": "defense_site_plan_unreadable", "detail": str(exc)[:200]}
        if not getattr(self, "adaptive_enabled", False) and plan is None:
            return self._legacy_prepare(obs)
        try:
            reserved = protected_factory_areas(
                self.game, obs["world_id"], self.catalog.fingerprint, runtime_dir=self.path.parent
            )
            reserved += [
                deepcopy(scope["areas"][i])
                for scope in self.state.get("coverage_scopes", {}).values()
                for i in range(len(scope["areas"]))
            ]
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "reason": "adaptive_defense_policy_unavailable", "detail": str(exc)[:200]}
        if plan is None:
            preview = self.policy_engine.prepare(obs, self.state, reserved)
        else:
            preview = self.policy_engine.prepare(obs, self.state, reserved, plan_areas=plan)
        if not preview.get("ok"):
            return preview
        if preview["risk"]["minimum_posts"] and not all(
            obs.get("enabled_recipes", {}).get(name) for name in ("gun-turret", "firearm-magazine")
        ):
            return {"ok": False, "reason": "native_gun_turret_and_normal_magazine_recipes_required"}
        state = (
            deepcopy(self.state)
            if self.state
            else dict(
                schema_version=1,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                prepared_tick=obs["tick"],
                posts=[],
                phase="deploy",
                ammo_target=AMMO_TARGET,
                refill_below=REFILL_BELOW,
                scope="adaptive current-risk armed targets; not whole-factory coverage or combat certification",
            )
        )
        state["posts"] += deepcopy(preview["new_posts"])
        state["range"] = preview["range"]
        state["adaptive_policy"] = deepcopy(preview["policy"])
        active = set(preview["policy"]["active_posts"])
        for post in state["posts"]:
            if not post.get("unit_number") and not post.get("lost_unit_number"):
                post["deferred_by_policy"] = post["name"] not in active
        if not self.state:
            state["reserved_areas"] = reserved
            state["requirements"] = {
                "gun-turret": len(preview["new_posts"]),
                "firearm-magazine": len(preview["new_posts"]) * preview["risk"]["ammo_target"],
            }
        return {**state, "ok": True}

    def _legacy_prepare(self, obs):
        """Read-only all-post native land/range/resource/reservation survey."""
        error = self._identity_error(obs)
        if error:
            return {"ok": False, "reason": error}
        if self._plan_areas(obs) is not None:
            return {"ok": False, "reason": "legacy_fixed_posts_refused_on_planned_world"}
        if not all(obs.get("enabled_recipes", {}).get(item) for item in ("gun-turret", "firearm-magazine")):
            return {"ok": False, "reason": "native_gun_turret_and_normal_magazine_recipes_required"}
        reserved = deepcopy(list(FUTURE_AREAS))
        board_path = self.path.parent / "pcb-board.json"
        if board_path.exists():
            board = json.loads(board_path.read_text(encoding="utf-8"))
            if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
                return {"ok": False, "reason": "defense_board_identity_changed"}
            reserved.extend(b["reserved_area"] for b in board.get("blocks", {}).values() if "reserved_area" in b)
        posts = [{"name": name, "anchor": _point(anchor), "focus": _point(focus)} for name, anchor, focus in POSTS]
        payload = json.dumps(json.dumps({"posts": posts, "reserved": reserved}, separators=(",", ":")))
        result = self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);local proto=prototypes.entity["gun-turret"]
if not proto or not proto.attack_parameters or proto.attack_parameters.range<=2 then return {ok=false,reason="native_turret_range_unavailable"} end
local range=proto.attack_parameters.range;local coverage=range-2;local chosen={}
for _,post in ipairs(args.posts) do
 local best=nil;local best_score=math.huge;local ax=math.floor(post.anchor.x+.5);local ay=math.floor(post.anchor.y+.5)
 for dx=-8,8 do for dy=-8,8 do
  local p={x=ax+dx,y=ay+dy};local legal=(p.x-post.focus.x)^2+(p.y-post.focus.y)^2<=coverage^2
  local area={{p.x-proto.tile_width/2,p.y-proto.tile_height/2},{p.x+proto.tile_width/2,p.y+proto.tile_height/2}}
  if legal then for _,box in ipairs(args.reserved) do
   if area[1][1]<box[2][1] and area[2][1]>box[1][1] and area[1][2]<box[2][2] and area[2][2]>box[1][2] then legal=false;break end
  end end
  if legal then for _,previous in ipairs(chosen) do
   if (p.x-previous.position.x)^2+(p.y-previous.position.y)^2<9 then legal=false;break end
  end end
  local old=legal and target(p,"gun-turret") or nil
  if old and (old.force~=f or old.position.x~=p.x or old.position.y~=p.y) then old=nil end
  if legal and (old or s.can_place_entity{name="gun-turret",position=p,direction=0,force=f})
   and s.count_entities_filtered{area=area,type="resource"}==0 then
   local score=(p.x-post.anchor.x)^2*100+(p.y-post.anchor.y)^2*100+(p.x-post.focus.x)^2+(p.y-post.focus.y)^2
   if old then score=score-100000 end
   if score<best_score then best_score=score;best={name=post.name,anchor=post.anchor,focus=post.focus,position=p,unit_number=old and old.unit_number or nil} end
  end
 end end
 if not best then return {ok=false,reason="no_clear_strategic_turret_site",post=post.name,anchor=post.anchor} end
 chosen[#chosen+1]=best
end
return {ok=true,range=range,coverage_margin=2,posts=chosen}
"""
        )
        if not result.get("ok"):
            return result
        return {
            **result,
            "schema_version": 1,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "prepared_tick": obs.get("tick"),
            "reserved_areas": reserved,
            "ammo_target": AMMO_TARGET,
            "refill_below": REFILL_BELOW,
            "phase": "deploy",
            "requirements": {
                "gun-turret": len(result["posts"]),
                "firearm-magazine": len(result["posts"]) * AMMO_TARGET,
            },
            "scope": "proactive actor-supplied strategic posts; no automatic ammo or combat acceptance",
        }

    def inspect(self, obs):
        """Read-only native status, including exact identity and normal ammo."""
        error = self._identity_error(obs)
        if error:
            return {"ok": False, "reason": error}
        if not self.state:
            return {"ok": True, "initialized": False, "posts": [], "missing": [], "unarmed": [], "refill": []}
        payload = json.dumps(json.dumps(self.state["posts"], separators=(",", ":")))
        result = self.game.query(
            """
local specs=helpers.json_to_table("""
            + payload
            + """);local posts={};local held=0;local partial=0
local main=a and a.valid and a.get_main_inventory() or nil
if main then for i=1,#main do local stack=main[i]
 if stack.valid_for_read and stack.name=="firearm-magazine" and stack.quality.name=="normal" then
  local used=stack.ammo<stack.prototype.magazine_size and 1 or 0
  held=held+stack.count-used;partial=partial+used
 end
end end
for _,spec in ipairs(specs) do
 local row={name=spec.name,position=spec.position,expected_unit=spec.unit_number}
 local e=target(spec.position,"gun-turret")
 if e and e.force==f and e.position.x==spec.position.x and e.position.y==spec.position.y then
  row.unit_number=e.unit_number;row.identity_changed=spec.unit_number and spec.unit_number~=e.unit_number or false
  row.health=e.health;row.max_health=e.max_health;row.status=e.status;row.ammo=0;row.ammo_rounds=0;row.full_magazines=0
  local inv=e.get_inventory(defines.inventory.turret_ammo)
  if inv then for i=1,#inv do local stack=inv[i]
   if stack.valid_for_read and stack.name=="firearm-magazine" and stack.quality.name=="normal" then
    row.ammo=row.ammo+stack.count;row.ammo_rounds=row.ammo_rounds+(stack.count-1)*stack.prototype.magazine_size+stack.ammo
    row.full_magazines=row.full_magazines+stack.count-(stack.ammo<stack.prototype.magazine_size and 1 or 0)
   end
  end end
 else row.missing=true;row.ammo=0;row.ammo_rounds=0;row.full_magazines=0 end
 row.nearby_enemies=s.count_entities_filtered{position=spec.position,radius=36,force="enemy",type={"unit","unit-spawner","turret"}}
 posts[#posts+1]=row
end
return {ok=true,tick=game.tick,posts=posts,actor_full_magazines=held,actor_partial_magazines=partial}
"""
        )
        if not result.get("ok"):
            return result
        if result.get("posts") == {}:  # an empty Lua sequence serializes as an object
            result["posts"] = []
        result.update(
            initialized=True,
            missing=[p["name"] for p in result["posts"] if p.get("missing")],
            unarmed=[p["name"] for p in result["posts"] if not p.get("missing") and p.get("ammo_rounds", 0) <= 0],
            refill=[
                p["name"]
                for p in result["posts"]
                if not p.get("missing") and p.get("full_magazines", -1) < REFILL_BELOW
            ],
            changed=[p["name"] for p in result["posts"] if p.get("identity_changed")],
            damaged=[p["name"] for p in result["posts"] if p.get("health", 0) < p.get("max_health", 0)],
        )
        return result

    def _save(self):
        _atomic_json(self.path, self.state)

    def _bind(self, report):
        changed = False
        for spec, live in zip(self.state["posts"], report["posts"]):
            if live.get("identity_changed"):
                continue
            if live.get("missing"):
                if spec.get("unit_number") is not None:
                    lost = spec.pop("unit_number")
                    history = list(spec.get("lost_unit_numbers", []))
                    for prior in (spec.get("lost_unit_number"), lost):
                        if type(prior) is int and prior > 0 and prior not in history:
                            history.append(prior)
                    spec.setdefault("lost_unit_origin", history[0])
                    spec["lost_unit_numbers"] = history[-16:]
                    spec["lost_unit_number"] = lost
                    spec["commissioned"] = False
                    changed = True
                continue
            if not spec.get("unit_number"):
                spec["unit_number"] = live["unit_number"]
                changed = True
            if live.get("full_magazines", -1) >= AMMO_TARGET and not spec.get("commissioned"):
                spec["commissioned"] = True
                changed = True
            policy = self.state.get("adaptive_policy", {})
            goal = policy.get("risk", {}).get("ammo_target", AMMO_TARGET)
            if (
                spec["name"] in policy.get("active_posts", [])
                and goal > AMMO_TARGET
                and live.get("full_magazines", -1) >= goal
                and spec.get("policy_loaded_target", 0) < goal
            ):
                spec["policy_loaded_target"] = goal
                changed = True
        if changed:
            self._save()

    def _refill(self, obs, report, *, initial=False):
        policy = self.state.get("adaptive_policy", {})
        active = set(policy.get("active_posts", []))
        for spec, live in zip(self.state["posts"], report["posts"]):
            if live.get("missing") or live.get("identity_changed"):
                continue
            selected = spec["name"] in active
            goal = policy.get("risk", {}).get("ammo_target", AMMO_TARGET) if selected else AMMO_TARGET
            loaded = max(spec.get("policy_loaded_target", 0), AMMO_TARGET if spec.get("commissioned") else 0)
            threshold = (
                (goal if initial and loaded < goal else policy.get("risk", {}).get("refill_below", REFILL_BELOW))
                if selected
                else (AMMO_TARGET if initial and not spec.get("commissioned") else REFILL_BELOW)
            )
            full = live.get("full_magazines")
            if type(full) is not int or full < 0:
                return _result("blocked", "native_full_magazine_count_required")
            if full >= threshold:
                continue
            needed = goal - full
            held = report["actor_full_magazines"]
            if not held:
                return self._ensure_magazines(obs, report, needed)
            return {
                "type": "insert",
                "name": "gun-turret",
                "position": spec["position"],
                "item": "firearm-magazine",
                "inventory": "turret_ammo",
                "count": min(held, needed),
                "expected_entity_unit": live["unit_number"],
                "expected_entity_world_id": obs["world_id"],
                "reason": "normally load or refill strategic perimeter post " + spec["name"],
            }
        return None

    @staticmethod
    def _ammo_observation(obs, report):
        # Aggregate inventory counts include partial/high-quality magazines.
        # Ordinary crafting must see only full normal actor stock for this job.
        return {**obs, "inventory": {**obs.get("inventory", {}), "firearm-magazine": report["actor_full_magazines"]}}

    def _ensure_magazines(self, obs, report, count):
        source = getattr(self, "ammo_source", None)
        if source is not None and source.commissioned:
            action = source.ensure_full_magazines(obs, count)
            return (
                action
                if action is not None
                else _result("waiting", "factory_magazines_available_reobserve_before_loading")
            )
        return self.bootstrap.ensure_item(self._ammo_observation(obs, report), "firearm-magazine", count)

    def upkeep(self, obs):
        """Refill known posts during other jobs; never start a new perimeter."""
        if not self.state:
            return None
        report = self.inspect(obs)
        if not report.get("ok"):
            return _result("blocked", report.get("reason", "native_defense_status_failed"))
        if report["changed"]:
            return _result("blocked", "defense_post_identity_changed", posts=report["changed"])
        self._bind(report)
        return self._refill(obs, report)

    def _deferred_growth(self, prepared):
        """Evidence when ``prepared`` wants more posts than the policy holds at a non-urgent risk."""
        risk = prepared.get("adaptive_policy", {}).get("risk", {})
        held = len(self.state.get("adaptive_policy", {}).get("active_posts", [])) if self.state else 0
        if risk.get("minimum_posts", 0) <= held or risk.get("level", URGENT_RISK_LEVEL) >= URGENT_RISK_LEVEL:
            return None
        return {
            "target_posts": risk["minimum_posts"],
            "active_posts": held,
            "risk": risk.get("risk"),
            "sizing": risk.get("sizing"),
        }

    def step(self, obs, *, required_posts=None, background=False):
        """Plan, build and arm the current-risk posts.

        ``background=True`` (another job's prelude): a non-urgent growth is not
        built here; known posts get upkeep and a ``defense_growth`` node adds
        the new ones off the critical path.
        """
        if not self.state or getattr(self, "adaptive_enabled", False):
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return _result("blocked", prepared.get("reason", "defense_preflight_failed"), native=prepared)
            deferred = self._deferred_growth(prepared) if background else None
            if deferred:
                self.growth_diagnostic = deferred
                upkeep = self.upkeep(obs) if self.state else None
                return upkeep or _result("succeeded", "defense_growth_deferred_to_low_priority_node", **deferred)
            if self.state != prepared:
                self.state = prepared
                self._save()
        report = self.inspect(obs)
        if not report.get("ok"):
            return _result("blocked", report.get("reason", "native_defense_status_failed"))
        if report["changed"]:
            return _result("blocked", "defense_post_identity_changed", posts=report["changed"])
        self._bind(report)
        action = self._refill(obs, report, initial=True)
        if action:
            return action
        for spec, live in zip(self.state["posts"], report["posts"]):
            if not live.get("missing"):
                continue
            policy = self.state.get("adaptive_policy")
            selected = policy and spec["name"] in policy["active_posts"]
            if policy and not selected and spec["name"] not in (required_posts or ()):
                continue
            goal = policy["risk"]["ammo_target"] if selected else AMMO_TARGET
            if report["actor_full_magazines"] < goal:
                return self._ensure_magazines(obs, report, goal)
            if int(obs.get("inventory", {}).get("gun-turret", 0)) < 1:
                return self.bootstrap.ensure_item(obs, "gun-turret", 1)
            payload = json.dumps(json.dumps(spec["position"], separators=(",", ":")))
            clear = self.game.query(
                """local p=helpers.json_to_table("""
                + payload
                + """);return {ok=s.can_place_entity{name="gun-turret",position=p,direction=0,force=f},resources=s.count_entities_filtered{area={{p.x-1,p.y-1},{p.x+1,p.y+1}},type="resource"}}"""
            )
            if not clear.get("ok") or clear.get("resources", 1) > 0:
                return _result("blocked", "prepared_defense_site_changed", post=spec["name"], position=spec["position"])
            return {
                "type": "build",
                "name": "gun-turret",
                "position": spec["position"],
                "direction": 0,
                "reason": "normally build proactive strategic perimeter post " + spec["name"],
            }
        proof = self._proof(report)
        if not proof:
            return _result("waiting", "waiting_for_initial_perimeter_magazines", native=report)
        if self.state.get("adaptive_policy"):
            if self.state["adaptive_policy"].get("status") != "ready":
                self.state["adaptive_policy"]["status"] = "ready"
                self._save()
            if not proof["active_post_names"]:
                return _result("succeeded", "quiet map, no posts required", **proof)
            return _result("succeeded", "current native risk defense targets are armed", **proof)
        if self.state.get("phase") != "armed":
            self.state["phase"] = "armed"
            self._save()
        return _result("succeeded", "declared strategic posts are normally armed", **proof)

    def _proof(self, report):
        if self.state.get("adaptive_policy"):
            return self._policy_proof(report, self.state["adaptive_policy"])
        if (
            not report.get("ok")
            or not self.state
            or len(report.get("posts", [])) != len(self.state["posts"])
            or report.get("missing")
            or report.get("changed")
            or report.get("unarmed")
            or report.get("refill")
            or any(not post.get("commissioned") for post in self.state["posts"])
        ):
            return None
        return {
            "tick": report["tick"],
            "posts": report["posts"],
            "native_range": self.state["range"],
            "ammo_target": AMMO_TARGET,
            "refill_below": REFILL_BELOW,
            "scope": "strategic armed-post commissioning; actor ammo bootstrap, no automatic-ammo or combat-stress certification",
        }

    def proof(self, obs):
        if getattr(self, "adaptive_enabled", False) and self.state:
            preview = self.prepare(obs)
            if not preview.get("ok"):
                return None
            return self._policy_proof(self.inspect(obs), preview["adaptive_policy"])
        return self._proof(self.inspect(obs)) if self.state else None

    def _policy_proof(self, report, policy):
        if (
            not report.get("ok")
            or report.get("changed")
            or policy.get("world_id") != self.state.get("world_id")
            or policy.get("catalog_fingerprint") != self.catalog.fingerprint
        ):
            return None
        names = policy.get("active_posts", [])
        rows = {p["name"]: p for p in report.get("posts", [])}
        specs = {p["name"]: p for p in self.state["posts"]}
        if len(names) != policy["risk"]["minimum_posts"]:
            return None
        for name in names:
            row = rows.get(name)
            spec = specs.get(name)
            if (
                not row
                or not spec
                or row.get("missing")
                or row.get("identity_changed")
                or not spec.get("commissioned")
                or row.get("full_magazines", -1) < policy["risk"]["refill_below"]
            ):
                return None
            loaded = max(spec.get("policy_loaded_target", 0), AMMO_TARGET if spec.get("commissioned") else 0)
            if loaded < policy["risk"]["ammo_target"] and row.get("full_magazines", -1) < policy["risk"]["ammo_target"]:
                return None
        return dict(
            tick=report["tick"],
            posts=[rows[name] for name in names],
            native_range=self.state["range"],
            ammo_target=policy["risk"]["ammo_target"],
            refill_below=policy["risk"]["refill_below"],
            risk=deepcopy(policy["risk"]),
            sizing=policy["risk"].get("sizing"),
            post_placement={
                p["name"]: specs[p["name"]].get("placement", "board") for p in report["posts"] if p["name"] in names
            },
            risk_observed_tick=policy["surveyed_tick"],
            active_post_names=list(names),
            retained_registered_posts=len(self.state["posts"]),
            covered_producer_units=policy["covered_producer_units"],
            uncovered_producer_units=policy["uncovered_producer_units"],
            scope="adaptive current-risk armed targets only; no whole-factory coverage, sustained-ammo or combat guarantee",
        )

    def _coverage_reservations(self, obs, areas):
        # Development-map future areas only without a site plan; a planned
        # world's land is filtered from the candidates (``prepare_coverage``).
        legacy = [] if self._plan_areas(obs) is not None else deepcopy(list(FUTURE_AREAS))
        reserved = legacy + deepcopy(areas)
        reserved.extend(
            protected_factory_areas(self.game, obs["world_id"], self.catalog.fingerprint, runtime_dir=self.path.parent)
        )
        for scope in self.state.get("coverage_scopes", {}).values():
            reserved.extend(deepcopy(scope["areas"]))
        return reserved

    def prepare_coverage(self, obs, scope_id, areas):
        """Read-only additive planning; every complete cell fits in one disk."""
        error = self._identity_error(obs)
        if error:
            return {"ok": False, "reason": error}
        if not isinstance(scope_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", scope_id):
            return {"ok": False, "reason": "invalid_defense_coverage_scope_id"}
        try:
            cells = coverage_cells(areas)
        except ValueError as exc:
            return {"ok": False, "reason": "invalid_defense_coverage_areas", "detail": str(exc)}
        if not self.state:
            return {"ok": False, "reason": "initial_defense_perimeter_required"}
        prior = self.state.get("coverage_scopes", {}).get(scope_id)
        if prior:
            if prior["areas"] != areas:
                return {"ok": False, "reason": "defense_coverage_scope_geometry_changed"}
            return {"ok": True, "scope": deepcopy(prior), "new_posts": [], "existing": True}
        if not all(obs.get("enabled_recipes", {}).get(item) for item in ("gun-turret", "firearm-magazine")):
            return {"ok": False, "reason": "native_gun_turret_and_normal_magazine_recipes_required"}
        report = self.inspect(obs)
        if not self._proof(report):
            return {"ok": False, "reason": "existing_defense_perimeter_not_healthy", "native": report}
        try:
            reserved = self._coverage_reservations(obs, areas)
            radius = self.state["range"] - COVERAGE_MARGIN
            if not 0 < radius <= 128:
                raise ValueError("native defense range outside bounded survey budget")
            bounds = [
                [
                    math.floor(min(a[0][0] for a in areas) - radius - 3),
                    math.floor(min(a[0][1] for a in areas) - radius - 3),
                ],
                [
                    math.ceil(max(a[1][0] for a in areas) + radius + 3),
                    math.ceil(max(a[1][1] for a in areas) + radius + 3),
                ],
            ]
            if (bounds[1][0] - bounds[0][0] + 1) * (bounds[1][1] - bounds[0][1] + 1) > 65536:
                raise ValueError("local defense walk-grid budget exceeded; split distant scopes")
            candidates = []
            for x in range(bounds[0][0] + 2, bounds[1][0] - 1):
                for y in range(bounds[0][1] + 2, bounds[1][1] - 1):
                    position = {"x": x, "y": y}
                    if any(all(math.hypot(cx - x, cy - y) <= radius for cx, cy in c["corners"]) for c in cells):
                        candidates.append(position)
            if len(candidates) > 12000:
                raise ValueError("defense placement candidate budget exceeded")
        except ValueError as exc:
            return {"ok": False, "reason": "defense_coverage_survey_budget_or_board_failed", "detail": str(exc)}
        native = native_candidate_survey(self.game, candidates, areas, reserved, bounds)
        if not native.get("ok"):
            return {"ok": False, "reason": native.get("reason", "native_defense_coverage_survey_failed")}
        if (
            native.get("range") != self.state["range"]
            or type(native.get("magazine_size")) not in (int, float)
            or native["magazine_size"] <= 0
        ):
            return {"ok": False, "reason": "native_defense_range_or_magazine_prototype_changed"}
        if self.state.get("adaptive_policy"):
            live = {p["name"]: p for p in report["posts"]}
            existing = deepcopy(
                [
                    p
                    for p in self.state["posts"]
                    if p.get("commissioned")
                    and not live.get(p["name"], {}).get("missing", True)
                    and live[p["name"]].get("unit_number") == p.get("unit_number")
                    and live[p["name"]].get("full_magazines", -1) >= REFILL_BELOW
                ]
            )
            candidates = [
                p
                for p in native.get("candidates", [])
                if all(
                    math.hypot(p["position"]["x"] - old["position"]["x"], p["position"]["y"] - old["position"]["y"])
                    >= 3
                    for old in self.state["posts"]
                )
            ]
        else:
            existing = deepcopy(self.state["posts"])
            candidates = native.get("candidates", [])
        # Planned world: off every planned area first; then the zone/strip that
        # hosts the scope may take a post on free land (bus/rail/corridors never).
        strict, relaxed = plan_exclusions(self._plan_areas(obs), areas)
        chosen = None
        for blocked in [strict] + ([relaxed] if relaxed != strict else []):
            chosen = select_cover(
                cells, existing, [p for p in candidates if outside_plan(p["position"], blocked)], radius
            )
            if chosen is not None:
                break
        if chosen is None:
            return {
                "ok": False,
                "reason": "no_clear_accessible_complete_defense_coverage",
                "native_candidates": len(native.get("candidates", [])),
                "effective_range": radius,
            }
        new_posts = []
        for i, site in enumerate(chosen):
            new_posts.append(
                {
                    **site,
                    "name": "coverage_" + scope_id + "_" + str(i + 1),
                    "coverage_scope": scope_id,
                    "commissioned": False,
                }
            )
        cover = select_cover(cells, [], existing + new_posts, radius, maximum_new_posts=len(existing) + len(new_posts))
        if cover is None:
            return {"ok": False, "reason": "defense_coverage_selection_failed"}
        scope = {
            "areas": deepcopy(areas),
            "cells": cells,
            "effective_range": radius,
            "native_range": native["range"],
            "coverage_margin": COVERAGE_MARGIN,
            "magazine_size": native["magazine_size"],
            "prepared_tick": obs.get("tick"),
            "covering_posts": [p["name"] for p in cover],
            "new_posts": [p["name"] for p in new_posts],
            "commissioned": False,
            "access_scope": native.get("access_scope"),
            "survey_bounds": bounds,
            "geometry": coverage_report(cells, existing + new_posts, radius),
        }
        return {
            "ok": True,
            "scope": scope,
            "new_posts": new_posts,
            "requirements": {"gun-turret": len(new_posts), "firearm-magazine": AMMO_TARGET * len(new_posts)},
        }

    def _coverage_evidence(self, report, scope, *, initial=False):
        if not self._proof(report):
            return None
        live = {p["name"]: p for p in report["posts"]}
        names = set(scope["covering_posts"]) | set(scope["new_posts"])
        selected = [live[name] for name in names if name in live]
        if len(selected) != len(names):
            return None
        if any(
            p.get("missing") or p.get("identity_changed") or p.get("full_magazines", -1) < REFILL_BELOW
            for p in selected
        ):
            return None
        if initial and any(p.get("full_magazines", -1) < AMMO_TARGET for p in selected):
            return None
        geometry = coverage_report(scope["cells"], selected, scope["effective_range"])
        if not geometry["complete"]:
            return None
        return {
            "tick": report["tick"],
            "geometry": geometry,
            "posts": selected,
            "initial_full_normal_magazines": AMMO_TARGET,
            "ongoing_refill_below": REFILL_BELOW,
            "access_scope": scope.get("access_scope"),
            "scope": "geometric armed-post coverage at this snapshot; no combat-stress certification",
        }

    def _quiet_scope_deferral(self, obs, scope_id, areas=None):
        if not getattr(self, "adaptive_enabled", False):
            return None
        if not isinstance(scope_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", scope_id):
            return None
        if areas is None:
            areas = self.state.get("deferred_scopes", {}).get(scope_id, {}).get("areas")
        if not areas:
            return None
        try:
            from .defense_policy import assess_risk

            risk = assess_risk(self.policy_engine.survey(obs), obs["world_id"])
            if risk["minimum_posts"] or risk["native_tier"] != "early":
                return None
            coverage_cells(areas)
            payload = json.dumps(json.dumps(areas, separators=(",", ":")))
            native = self.game.query(
                """local areas=helpers.json_to_table("""
                + payload
                + """);local count=0
for _,box in ipairs(areas)do
 local expanded={{box[1][1]-96,box[1][2]-96},{box[2][1]+96,box[2][2]+96}}
 count=count+s.count_entities_filtered{area=expanded,force=game.forces.enemy,type={"unit","unit-spawner","turret"},limit=1}
end
return {ok=true,world_id=d.world_id,tick=game.tick,nearby_enemies=count}"""
            )
            if (
                not native.get("ok")
                or native.get("world_id") != obs["world_id"]
                or type(native.get("tick")) is not int
                or native["tick"] < obs["tick"]
                or type(native.get("nearby_enemies")) is not int
                or native["nearby_enemies"] != 0
            ):
                return None
            return dict(
                tick=native["tick"],
                world_id=obs["world_id"],
                defense_required=False,
                risk=risk,
                scope="early quiet construction allowed after current producer and proposed-site threat surveys; no armed coverage claim",
            )
        except (KeyError, TypeError, ValueError, RuntimeError):
            return None

    def ensure_coverage(self, obs, scope_id, areas):
        """Ordinary paid deployment gate; None means this scope is ready."""
        error = self._identity_error(obs)
        if error:
            return _result("blocked", error)
        for group in ("deferred_scopes", "coverage_scopes"):
            prior = self.state.get(group, {}).get(scope_id) if isinstance(scope_id, str) else None
            if prior and prior.get("areas") != areas:
                return _result("blocked", "defense_coverage_scope_geometry_changed")
        deferred = self._quiet_scope_deferral(obs, scope_id, areas)
        if deferred:
            if not self.state:
                prepared = self.prepare(obs)
                if not prepared.get("ok"):
                    return _result("blocked", prepared.get("reason", "defense_risk_unavailable"))
                self.state = prepared
            stored = {"areas": deepcopy(areas)}
            if self.state.get("deferred_scopes", {}).get(scope_id) != stored:
                self.state.setdefault("deferred_scopes", {})[scope_id] = stored
                self._save()
            self.coverage_policy_diagnostic = deferred
            return None
        prepared = self.prepare_coverage(obs, scope_id, areas)
        if not self.state and prepared.get("reason") == "initial_defense_perimeter_required":
            action = self.step(obs)
            if action.get("status") == "succeeded":
                return _result("waiting", "initial_perimeter_ready_prepare_additive_coverage_next")
            return action
        if not prepared.get("ok"):
            return _result("blocked", prepared["reason"], native=prepared)
        if not prepared.get("existing"):
            self.state["posts"].extend(deepcopy(prepared["new_posts"]))
            self.state.setdefault("coverage_scopes", {})[scope_id] = deepcopy(prepared["scope"])
            self._save()
        scope = self.state["coverage_scopes"][scope_id]
        report = self.inspect(obs)
        if not report.get("ok"):
            return _result("blocked", report.get("reason", "native_defense_status_failed"))
        if report.get("changed"):
            return _result("blocked", "defense_post_identity_changed", posts=report["changed"])
        by_name = {p["name"]: p for p in report["posts"]}
        required = set(scope["covering_posts"]) | set(scope["new_posts"])
        active = set(self.state.get("adaptive_policy", {}).get("active_posts", []))
        for spec in self.state["posts"]:
            live = by_name.get(spec["name"], {})
            if (
                live.get("missing")
                and (spec.get("unit_number") or spec.get("lost_unit_number"))
                and (not self.state.get("adaptive_policy") or spec["name"] in required | active)
            ):
                return _result("blocked", "existing_defense_post_missing_requires_explicit_repair", post=spec["name"])
        self._bind(report)
        if not scope["commissioned"]:
            names = set(scope["covering_posts"]) | set(scope["new_posts"])
            for spec in self.state["posts"]:
                live = by_name.get(spec["name"], {})
                if spec["name"] not in names or live.get("missing"):
                    continue
                if "full_magazines" not in live:
                    return _result("blocked", "native_full_normal_magazine_evidence_required", post=spec["name"])
                needed = AMMO_TARGET - live["full_magazines"]
                if needed <= 0:
                    continue
                held = report["actor_full_magazines"]
                if not held:
                    return self._ensure_magazines(obs, report, needed)
                return {
                    "type": "insert",
                    "name": "gun-turret",
                    "position": spec["position"],
                    "item": "firearm-magazine",
                    "inventory": "turret_ammo",
                    "count": min(held, needed),
                    "expected_entity_unit": live["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "load ten full normal magazines before industrial coverage " + scope_id,
                }
        evidence = self._coverage_evidence(report, scope, initial=not scope["commissioned"])
        if evidence:
            if not scope["commissioned"]:
                scope["commissioned"] = True
                scope["commissioned_tick"] = report["tick"]
                self._save()
            return None
        action = self.step(obs, required_posts=required)
        if action.get("type") == "build":
            try:
                reserved = self._coverage_reservations(obs, areas)
            except ValueError as exc:
                return _result("blocked", str(exc))
            native = native_candidate_survey(self.game, [action["position"]], areas, reserved, scope["survey_bounds"])
            if not native.get("ok") or not any(
                p.get("position") == action["position"] for p in native.get("candidates", [])
            ):
                return _result(
                    "blocked", "prepared_defense_coverage_site_or_access_changed", position=action["position"]
                )
        if action.get("status") == "succeeded":
            return _result("waiting", "waiting_for_complete_native_coverage_proof")
        return action

    def coverage_proof(self, obs, scope_id):
        if self._identity_error(obs):
            return None
        deferred = self._quiet_scope_deferral(obs, scope_id)
        if deferred:
            return deferred
        scope = self.state.get("coverage_scopes", {}).get(scope_id)
        if not scope or not scope.get("commissioned"):
            return None
        return self._coverage_evidence(self.inspect(obs), scope)
