"""Bounded current-risk defense targets, distinct from all-factory coverage."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

from .defense_coverage import coverage_cells, native_candidate_survey


PRODUCERS = {
    "mining-drill",
    "furnace",
    "assembling-machine",
    "lab",
    "boiler",
    "generator",
    "offshore-pump",
    "mining-drill",
}
SURVEY_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=x.world_id or s.name~="nauvis"then return {ok=false,reason="defense_policy_world_or_surface_changed"}end
local enemy=game.forces.enemy;local proto=prototypes.entity["gun-turret"]
if not enemy or not proto or not proto.attack_parameters then return {ok=false,reason="defense_policy_native_fields_missing"}end
local kinds={["mining-drill"]=true,furnace=true,["assembling-machine"]=true,lab=true,boiler=true,generator=true,["offshore-pump"]=true}
local assets={};local guns={};local truncated=false;local ammo_machine=false
for _,e in pairs(s.find_entities_filtered{force=f})do
 if e.valid and e.unit_number and kinds[e.type]then
  assets[#assets+1]={unit_number=e.unit_number,name=e.name,type=e.type,position={x=e.position.x,y=e.position.y},health=e.health,max_health=e.max_health}
  if e.type=="assembling-machine"and e.energy>0 then
   local recipe=e.get_recipe();if recipe and recipe.name=="firearm-magazine"and e.products_finished>0 then ammo_machine=true end
  end
 elseif e.valid and e.name=="gun-turret"then
  guns[#guns+1]={unit_number=e.unit_number,position={x=e.position.x,y=e.position.y}}
 end
end
table.sort(assets,function(a,b)return a.unit_number<b.unit_number end)
while #assets>128 do assets[#assets]=nil;truncated=true end
local targets={};local seen={}
local hostile=not f.get_friend(enemy)and not f.get_cease_fire(enemy)
if hostile then for _,asset in ipairs(assets)do
 for _,e in pairs(s.find_entities_filtered{position=asset.position,radius=96,force=enemy,type={"unit","unit-spawner","turret"},limit=129})do
  if e.valid and not seen[e.unit_number]and f.is_chunk_charted(s,{x=math.floor(e.position.x/32),y=math.floor(e.position.y/32)})then
   if #targets>=128 then truncated=true;break end
   seen[e.unit_number]=true
   local attack=e.prototype.attack_parameters
   targets[#targets+1]={unit_number=e.unit_number,name=e.name,type=e.type,position={x=e.position.x,y=e.position.y},health=e.health,max_health=e.max_health,range=attack and attack.range or 0}
  end
 end
end end
local technologies={};for _,name in ipairs{"automation","military","military-2","logistic-science-pack","chemical-science-pack"}do
 technologies[name]=f.technologies[name]and f.technologies[name].researched or false
end
return {ok=true,world_id=d.world_id,tick=game.tick,surface_index=s.index,force_index=f.index,assets=assets,guns=guns,targets=targets,
 evolution_factor=enemy.get_evolution_factor(s),gun_range=proto.attack_parameters.range,technologies=technologies,truncated=truncated,ammo_machine_observed=ammo_machine}
"""


def _distance(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def assess_risk(facts, world_id):
    if (
        not facts.get("ok")
        or facts.get("world_id") != world_id
        or type(facts.get("tick")) is not int
        or type(facts.get("surface_index")) is not int
        or type(facts.get("force_index")) is not int
        or type(facts.get("evolution_factor")) not in (int, float)
        or not math.isfinite(facts["evolution_factor"])
        or not 0 <= facts["evolution_factor"] <= 1
        or type(facts.get("gun_range")) not in (int, float)
        or not math.isfinite(facts["gun_range"])
        or not 2 < facts["gun_range"] <= 128
    ):
        raise ValueError("current native defense risk fields unavailable")
    assets = facts.get("assets")
    targets = facts.get("targets")
    if (
        not isinstance(assets, list)
        or not assets
        or len(assets) > 128
        or not isinstance(targets, list)
        or len(targets) > 128
    ):
        raise ValueError("bounded current producer/threat survey required")
    for row in assets + targets + facts.get("guns", []):
        p = row.get("position", {})
        if (
            type(row.get("unit_number")) is not int
            or row["unit_number"] <= 0
            or set(p) != {"x", "y"}
            or any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) for k in p)
        ):
            raise ValueError("native producer/threat identity or position unavailable")
    for row in assets + targets:
        if (
            any(
                type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] < 0
                for k in ("health", "max_health")
            )
            or row["max_health"] <= 0
        ):
            raise ValueError("native producer/threat health unavailable")
    units = [r for r in targets if r["type"] == "unit"]
    nests = [r for r in targets if r["type"] == "unit-spawner"]
    nearest = lambda rows: min((_distance(a["position"], t["position"]) for a in assets for t in rows), default=None)
    near_unit = nearest(units)
    near_nest = nearest(nests)
    near_any = nearest(targets)
    damaged = [a["unit_number"] for a in assets if a["health"] < a["max_health"] * 0.95]
    strongest = max((r["max_health"] for r in units), default=0)
    tech = facts.get("technologies", {})
    tier = (
        "advanced"
        if tech.get("chemical-science-pack")
        else "mid"
        if tech.get("military-2") or tech.get("logistic-science-pack")
        else "early"
    )
    level = 0
    if (
        facts.get("truncated")
        or (near_unit is not None and near_unit <= 64)
        or (near_nest is not None and near_nest <= 64)
    ):
        level = 1
    pressure = bool(facts.get("recent_damage_units")) or (near_any is not None and near_any <= 64)
    if (
        (damaged and pressure)
        or (near_unit is not None and near_unit <= 40)
        or (near_any is not None and near_any <= 32)
    ):
        level = 2
    if (near_unit is not None and near_unit <= 24 and strongest >= 75) or (
        pressure and any(a["health"] < a["max_health"] * 0.5 for a in assets)
    ):
        level = 3
    if facts["evolution_factor"] >= 0.4 and near_any is not None and near_any <= 64:
        level = max(level, 2)
    minimum = (0, 2, 3, 4)[level]
    if tier == "mid" and level == 0:
        minimum = 1
    if tier == "advanced" and level == 0:
        minimum = 2
    return dict(
        risk=("quiet", "approaching", "high", "critical")[level],
        level=level,
        minimum_posts=minimum,
        maximum_posts=minimum,
        ammo_target=10 if level < 2 else 20,
        refill_below=5 if level < 2 else 10,
        native_tier=tier,
        evolution_factor=facts["evolution_factor"],
        nearest_unit=near_unit,
        nearest_spawner=near_nest,
        maximum_observed_unit_health=strongest,
        damaged_producer_units=damaged,
        truncated=bool(facts.get("truncated")),
        native_gun_range=facts["gun_range"],
        maximum_enemy_range=max((t.get("range", 0) for t in targets), default=0),
        firepower_gap=any(t["type"] == "turret" and t.get("range", 0) >= facts["gun_range"] for t in targets),
        ammo_supply="native_machine_observed" if facts.get("ammo_machine_observed") else "finite_normal_stock",
        producer_count=len(assets),
        scope="current-risk armed targets; not whole-factory coverage or a combat guarantee",
    )


class DefensePolicy:
    def __init__(self, game, catalog):
        self.game, self.catalog = game, catalog
        self.cache = None

    def _key(self, obs):
        rows = [
            (e.get("unit_number"), e.get("position"), e.get("health"), e.get("max_health"))
            for e in obs.get("entities", [])
            if e.get("type") in PRODUCERS
        ]
        return hashlib.sha256(
            json.dumps([obs["world_id"], rows, obs.get("technologies", {})], sort_keys=True).encode()
        ).hexdigest()

    def survey(self, obs):
        key = self._key(obs)
        if (
            self.cache
            and self.cache["key"] == key
            and 0 <= obs["tick"] - self.cache["observed_tick"] < self.cache["interval"]
        ):
            return deepcopy(self.cache["facts"])
        payload = json.dumps(json.dumps({"world_id": obs["world_id"]}, separators=(",", ":")))
        facts = self.game.query(SURVEY_LUA.replace("PAYLOAD", payload))
        # Factorio serializes an empty Lua sequence as an empty JSON object.
        # No enemies is a valid quiet survey, not unavailable threat evidence.
        for field in ("assets", "guns", "targets"):
            if facts.get(field) == {}:
                facts[field] = []
        previous = {a["unit_number"]: a["health"] for a in self.cache["facts"].get("assets", [])} if self.cache else {}
        facts["recent_damage_units"] = [
            a["unit_number"]
            for a in facts.get("assets", [])
            if a.get("unit_number") in previous
            and type(a.get("health")) in (int, float)
            and a["health"] < previous[a["unit_number"]]
        ]
        risk = assess_risk(facts, obs["world_id"])
        self.cache = dict(
            key=key, observed_tick=obs["tick"], interval=120 if risk["level"] >= 2 else 600, facts=deepcopy(facts)
        )
        return facts

    def prepare(self, obs, state, reserved):
        try:
            facts = self.survey(obs)
            risk = assess_risk(facts, obs["world_id"])
            radius = facts["gun_range"] - 2
            assets = facts["assets"]
            posts = state.get("posts", [])
            live = {g["unit_number"]: g for g in facts.get("guns", [])}
            paid = [
                p for p in posts if p.get("unit_number") in live and p["position"] == live[p["unit_number"]]["position"]
            ]
            targets = facts["targets"]

            def rank(asset):
                near = min((_distance(asset["position"], t["position"]) for t in targets), default=10000)
                damaged = asset["health"] / asset["max_health"] < 0.95
                density = sum(_distance(asset["position"], a["position"]) <= radius for a in assets)
                return (not damaged, near, -density, asset["unit_number"])

            ranked = sorted(assets, key=rank)
            chosen = []
            additions = []
            focuses = []
            intent_key = hashlib.sha256(
                json.dumps(
                    [
                        obs["world_id"],
                        risk["level"],
                        risk["minimum_posts"],
                        [(a["unit_number"], a["position"]) for a in ranked],
                        reserved,
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            previous = state.get("adaptive_policy", {})
            by_name = {p["name"]: p for p in posts}
            if (
                previous.get("intent_key") == intent_key
                and len(previous.get("active_posts", [])) == risk["minimum_posts"]
                and all(name in by_name for name in previous["active_posts"])
            ):
                policy = deepcopy(previous)
                policy.update(surveyed_tick=facts["tick"], risk=risk)
                return {"ok": True, "range": facts["gun_range"], "policy": policy, "new_posts": [], "risk": risk}
            # Reuse quiet paid guards even if old geometry covers only part of
            # today's factory. Disclose uncovered assets rather than buy a ninth
            # future post or claim that a reserve guard protects every producer.
            if risk["level"] == 0 and paid:
                chosen = sorted(
                    paid,
                    key=lambda p: (
                        -sum(_distance(p["position"], a["position"]) <= radius for a in assets),
                        min(_distance(p["position"], a["position"]) for a in assets),
                        p["name"],
                    ),
                )[: risk["minimum_posts"]]
            for index in range(len(chosen), risk["minimum_posts"]):
                focus = next(
                    (a for a in ranked if all(_distance(a["position"], q["position"]) > radius for q in focuses)),
                    ranked[0],
                )
                focuses.append(focus)
                reusable = [
                    p for p in paid if p not in chosen and _distance(p["position"], focus["position"]) <= radius - 3
                ]
                if reusable:
                    chosen.append(min(reusable, key=lambda p: (_distance(p["position"], focus["position"]), p["name"])))
                    continue
                if len(posts) + len(additions) >= 128:
                    return {"ok": False, "reason": "adaptive_defense_registered_post_budget_reached", "risk": risk}
                area = [
                    [focus["position"]["x"] - 2, focus["position"]["y"] - 2],
                    [focus["position"]["x"] + 2, focus["position"]["y"] + 2],
                ]
                cells = coverage_cells([area])
                cx = round(focus["position"]["x"])
                cy = round(focus["position"]["y"])
                reach = math.floor(radius)
                candidates = []
                for x in range(cx - reach, cx + reach + 1, 2):
                    for y in range(cy - reach, cy + reach + 1, 2):
                        p = {"x": x, "y": y}
                        if any(_distance(p, q["position"]) < 3 for q in posts + additions):
                            continue
                        if not all(math.hypot(x - px, y - py) <= radius for c in cells for px, py in c["corners"]):
                            continue
                        if any(
                            t["type"] == "turret" and _distance(p, t["position"]) <= t.get("range", 0) + 2
                            for t in targets
                        ):
                            continue
                        candidates.append(p)
                candidates.sort(key=lambda p: (_distance(p, focus["position"]), p["x"], p["y"]))
                bounds = [[cx - reach - 3, cy - reach - 3], [cx + reach + 3, cy + reach + 3]]
                if (bounds[1][0] - bounds[0][0] + 1) * (bounds[1][1] - bounds[0][1] + 1) > 65536:
                    return {"ok": False, "reason": "adaptive_defense_survey_budget_exceeded", "risk": risk}
                native = native_candidate_survey(self.game, candidates[:256], [area], reserved, bounds)
                if not native.get("ok") or native.get("range") != facts["gun_range"] or not native.get("candidates"):
                    return {
                        "ok": False,
                        "reason": "adaptive_defense_no_clear_current_producer_site",
                        "risk": risk,
                        "focus_unit": focus["unit_number"],
                    }
                site = min(
                    native["candidates"],
                    key=lambda q: (_distance(q["position"], focus["position"]), q["position"]["x"], q["position"]["y"]),
                )
                name = "adaptive_" + str(focus["unit_number"]) + "_" + str(len(posts) + len(additions) + 1)
                post = dict(
                    site,
                    name=name,
                    anchor=deepcopy(focus["position"]),
                    focus=deepcopy(focus["position"]),
                    commissioned=False,
                )
                additions.append(post)
                chosen.append(post)
            covered = [
                a["unit_number"]
                for a in assets
                if any(_distance(a["position"], p["position"]) <= radius - 3 for p in chosen)
            ]
            policy = dict(
                schema_version=1,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                surveyed_tick=facts["tick"],
                risk=risk,
                active_posts=[p["name"] for p in chosen],
                focus_units=[a["unit_number"] for a in focuses],
                covered_producer_units=covered,
                uncovered_producer_units=[a["unit_number"] for a in assets if a["unit_number"] not in covered],
                ammo_item="firearm-magazine",
                status="deploy",
                intent_key=intent_key,
            )
            return {"ok": True, "range": facts["gun_range"], "policy": policy, "new_posts": additions, "risk": risk}
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            return {"ok": False, "reason": "adaptive_defense_policy_unavailable", "detail": str(exc)[:200]}
