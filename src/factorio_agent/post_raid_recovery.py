"""Normal tool repair and conserved recovery of one completed paid raid wave."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import uuid

from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.core.deterministic_repair_control import pending_repair
from .infrastructure_repair import _hash, _read, _payload, _same_frame

FILE = "post-raid-recovery.json"
ACTION = "recover_raid_post"
AMMO = ("firearm-magazine", "piercing-rounds-magazine")


def _position(q):
    p = q.get("position")
    return (
        isinstance(p, dict)
        and set(p) == {"x", "y"}
        and all(type(v) in (int, float) and math.isfinite(v) for v in p.values())
    )


def _source(root, world, catalog):
    doc = _read(root / "nest-sweep.json")
    wave = doc["wave"]
    proof = doc["verification"]
    board = _read(root / "pcb-board.json")
    if (
        not _same_frame(doc, world, catalog)
        or not _same_frame(board, world, catalog)
        or doc.get("phase") != "verified"
        or board["blocks"].get(wave["block"]) != wave["plan"]
        or proof.get("world_id") != world
        or proof.get("target_set_hash") != doc["target_set_hash"]
        or _hash(doc["targets"]) != doc["target_set_hash"]
        or proof.get("group_id") != wave["group_id"]
        or proof.get("single_wave") is not True
        or proof.get("targets_cleared") != len(doc["targets"])
        or not 1 <= len(doc["targets"]) <= 12
        or not 6 <= len(wave["posts"]) <= 12
        or type(doc.get("actor_unit")) is not int
    ):
        raise ValueError("exact completed single-wave victory and registered plan required")
    seen = set()
    posts = []
    for post in wave["posts"]:
        name = post["name"]
        unit = wave["bindings"].get(name)
        if (
            name in seen
            or not _position(post)
            or type(unit) is not int
            or unit < 1
            or post.get("ammo_item") not in AMMO
            or name not in wave["deployment_ids"]
        ):
            raise ValueError("exact paid wave bindings required")
        seen.add(name)
        posts.append(
            dict(
                name=name,
                position=post["position"],
                unit_number=unit,
                ammo_item=post["ammo_item"],
                deployment_id=wave["deployment_ids"][name],
            )
        )
    return doc, dict(
        operation_id=doc["id"],
        original_actor=doc["actor_unit"],
        target_set_hash=doc["target_set_hash"],
        targets=doc["targets"],
        group_id=wave["group_id"],
        posts=posts,
        victory=proof,
        source_revision=_hash(doc),
        plan_revision=_hash(wave["plan"]),
    )


def _covers(root, world, catalog, posts):
    registry = _read(root / "defense-perimeter.json")
    if not _same_frame(registry, world, catalog):
        raise ValueError("registered base coverage world changed")
    covers = []
    raid_units = {q["unit_number"] for q in posts}
    for q in registry.get("posts", []):
        unit = q.get("unit_number")
        if unit in raid_units:
            raise ValueError("raid post is now referenced by persistent perimeter")
        if (
            type(unit) is int
            and unit > 0
            and _position(q)
            and any(
                math.dist(tuple(q["position"][k] for k in ("x", "y")), tuple(p["position"][k] for k in ("x", "y")))
                <= 24
                for p in posts
            )
        ):
            covers.append({k: deepcopy(q[k]) for k in ("name", "position", "unit_number")})
    return covers


def _context(game, action):
    fields = {
        "type",
        "recovery_id",
        "post_name",
        "expected_world_id",
        "expected_actor_unit",
        "expected_catalog_fingerprint",
    }
    if (
        not isinstance(action, dict)
        or set(action) - fields - {"reason"}
        or not fields <= set(action)
        or action.get("type") != ACTION
        or any(
            not isinstance(action[k], str) or not action[k]
            for k in ("recovery_id", "post_name", "expected_world_id", "expected_catalog_fingerprint")
        )
        or len(action["recovery_id"]) != 64
        or any(c not in "0123456789abcdef" for c in action["recovery_id"])
        or type(action["expected_actor_unit"]) is not int
        or action["expected_actor_unit"] < 1
        or action["expected_world_id"] != getattr(game, "expected_world", None)
        or getattr(game, "backend", None) != "assisted"
    ):
        raise ValueError("exact registered raid recovery envelope required")
    root = Path(game.cfg.runtime_dir)
    ledger = _read(root / FILE)
    run = ledger["operations"][action["recovery_id"]]
    p = run["authority"]
    if (
        not _same_frame(ledger, action["expected_world_id"], action["expected_catalog_fingerprint"])
        or _hash(p) != action["recovery_id"]
        or p["frame"]
        != {k: action[k] for k in ("expected_world_id", "expected_actor_unit", "expected_catalog_fingerprint")}
        or action["post_name"] not in {q["name"] for q in p["posts"]}
    ):
        raise ValueError("immutable raid recovery authority changed")
    doc, source = _source(root, action["expected_world_id"], action["expected_catalog_fingerprint"])
    if source["source_revision"] != p["source_revision"]:
        raise ValueError("original completed sweep checkpoint changed")
    covers = _covers(root, action["expected_world_id"], action["expected_catalog_fingerprint"], p["posts"])
    if any(q not in covers for q in p["covers"]):
        raise ValueError("known local base coverage identity changed")
    return dict(p, recovery_id=action["recovery_id"], post_name=action["post_name"])


def _valid_receipt(r, p):
    try:
        if (
            not isinstance(r, dict)
            or r.get("ok") is not True
            or r.get("kind") != "ordinary_full_health_raid_mine_v1"
            or r.get("recovery_id") != p["recovery_id"]
            or r.get("post_name") != p["post_name"]
            or r.get("world_id") != p["frame"]["expected_world_id"]
            or r.get("catalog_fingerprint") != p["frame"]["expected_catalog_fingerprint"]
            or type(r.get("actor_unit")) is not int
            or r["actor_unit"] != p["frame"]["expected_actor_unit"]
            or r.get("inventory_scope") != "character_main+character_ammo"
            or type(r.get("recovered_turrets")) is not int
            or r.get("recovered_turrets") != 1
            or r.get("full_health_before") is not True
            or type(r.get("tick")) is not int
            or r["tick"] < 0
            or type(r.get("intent_tick")) is not int
            or not 0 <= r["intent_tick"] <= r["tick"]
        ):
            return False
        post = next(q for q in p["posts"] if q["name"] == p["post_name"])
        if r["unit_number"] != post["unit_number"] or r["position"] != post["position"]:
            return False
        before, after = r["before"], r["after"]
        if any(
            not isinstance(q, dict)
            or type(q.get("gun_turrets")) is not int
            or q["gun_turrets"] < 0
            or not isinstance(q.get("other_totals"), (dict, list))
            for q in (before, after)
        ):
            return False
        if not isinstance(r.get("magazine_sizes"), dict) or any(
            type(r["magazine_sizes"].get(item)) is not int or r["magazine_sizes"][item] < 1 for item in AMMO
        ):
            return False
        if after["gun_turrets"] != before["gun_turrets"] + 1:
            return False
        for item in AMMO:
            b, a, stock = before["ammo"][item], after["ammo"][item], r["old_ammo"][item]
            for q in (b, a, stock):
                if not isinstance(q, dict) or any(
                    type(q.get(k)) is not int or q[k] < 0 for k in ("magazines", "rounds")
                ):
                    return False
                if not q["magazines"] <= q["rounds"] <= q["magazines"] * r["magazine_sizes"][item]:
                    return False
            if a["magazines"] != b["magazines"] + stock["magazines"] or a["rounds"] != b["rounds"] + stock["rounds"]:
                return False
        return before["other_totals"] == after["other_totals"]
    except (KeyError, TypeError, StopIteration):
        return False


def execute_recover_raid_post(game, action):
    p = _context(game, action)
    try:
        r = game.query("local x=helpers.json_to_table(" + _payload(p) + ");" + RECOVERY_GUARD_LUA + RECOVER_POST_LUA)
    except Exception as exc:
        return dict(ok=False, reason="post_raid_recovery_outcome_unknown", original_reason=str(exc)[:300])
    if not isinstance(r, dict) or type(r.get("ok")) is not bool:
        return dict(
            ok=False, reason="post_raid_recovery_outcome_unknown", original_reason="invalid native recovery receipt"
        )
    if r["ok"] and not _valid_receipt(r, p):
        return dict(
            r, ok=False, reason="post_raid_recovery_outcome_unknown", original_reason="invalid conservation receipt"
        )
    if not r["ok"] and (r.get("effects_started") or type(r.get("effects_started")) is not bool):
        return dict(r, reason="post_raid_recovery_outcome_unknown", original_reason=r.get("reason"))
    return r


class PostRaidRecovery:
    def __init__(self, game, builder, catalog, *, sweep, perimeter=None):
        self.game, self.builder, self.catalog, self.sweep, self.perimeter = game, builder, catalog, sweep, perimeter
        self.path = Path(game.cfg.runtime_dir) / FILE
        self.last_diagnostic = None

    def _ledger(self, obs):
        if (
            not obs.get("ok")
            or type(obs.get("actor_unit_number")) is not int
            or obs.get("world_id") != self.game.expected_world
        ):
            raise ValueError("fresh bound raid recovery observation required")
        ledger = (
            _read(self.path)
            if self.path.exists()
            else dict(world_id=obs["world_id"], catalog_fingerprint=self.catalog.fingerprint, operations={})
        )
        if not _same_frame(ledger, obs["world_id"], self.catalog.fingerprint):
            raise ValueError("raid recovery ledger context changed")
        return ledger

    def _inspect(self, p):
        return self.game.query(
            "local x=helpers.json_to_table(" + _payload(p) + ");" + RECOVERY_GUARD_LUA + INSPECT_RECOVERY_LUA
        )

    def prepare(self, obs):
        try:
            ledger = self._ledger(obs)
            doc, p = _source(self.path.parent, obs["world_id"], self.catalog.fingerprint)
            prior = [
                q["authority"]
                for q in ledger["operations"].values()
                if q["authority"]["source_revision"] == p["source_revision"]
            ]
            if len(prior) > 1:
                raise ValueError("multiple recovery authorities for one completed wave")
            if prior:
                p = prior[0]
                if p["frame"]["expected_actor_unit"] != obs["actor_unit_number"]:
                    raise ValueError("recorded raid recovery actor changed; explicit continuation required")
            else:
                p.update(
                    frame=dict(
                        expected_world_id=obs["world_id"],
                        expected_actor_unit=obs["actor_unit_number"],
                        expected_catalog_fingerprint=self.catalog.fingerprint,
                    ),
                    covers=_covers(self.path.parent, obs["world_id"], self.catalog.fingerprint, p["posts"]),
                )
            key = _hash(p)
            native = self._inspect(dict(p, recovery_id=key))
            if not native.get("ok"):
                return native
            return dict(ok=True, recovery_id=key, authority=p, native=native)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return dict(ok=False, reason=str(exc))

    def step(self, obs):
        try:
            cleanup = pending_repair(obs)
            if cleanup:
                return cleanup
            ledger = self._ledger(obs)
            pending = [(k, q) for k, q in ledger["operations"].items() if q["phase"] != "verified"]
            if len(pending) > 1:
                raise ValueError("multiple unfinished raid recovery operations")
            if pending:
                key, run = pending[0]
            else:
                ready = self.prepare(obs)
                if not ready.get("ok"):
                    self.last_diagnostic = ready
                    return None
                key = ready["recovery_id"]
                if key in ledger["operations"]:
                    self.last_diagnostic = dict(ok=True, reason="completed raid recovery already recorded")
                    return None
                run = dict(phase="recover", authority=ready["authority"], history=[], repair_requests=[])
                ledger["operations"][key] = run
                _atomic_json(self.path, ledger)
            p = run["authority"]
            post = p["posts"][0]
            action = dict(type=ACTION, recovery_id=key, post_name=post["name"], **p["frame"])
            _context(self.game, action)
            native = self._inspect(dict(p, recovery_id=key))
            if not native.get("ok"):
                raise ValueError(native.get("reason", "native raid recovery guard failed"))
            recorded = {q["post_name"] for q in run["history"]}
            for row in native["posts"]:
                if row.get("receipt") and row["name"] not in recorded:
                    receipt = row["receipt"]
                    if not _valid_receipt(receipt, dict(p, recovery_id=key, post_name=row["name"])):
                        raise ValueError("native raid receipt changed")
                    run["history"].append(dict(post_name=row["name"], kind="recovered", receipt=receipt))
                    recorded.add(row["name"])
                elif row.get("lost_tick") is not None and row["name"] not in recorded:
                    run["history"].append(
                        dict(
                            post_name=row["name"],
                            kind="combat_loss",
                            unit_number=row["unit_number"],
                            tick=row["lost_tick"],
                            recovered_turrets=0,
                        )
                    )
                    recorded.add(row["name"])
            _atomic_json(self.path, ledger)
            active = [q for q in native["posts"] if q["name"] not in recorded]
            if not active:
                run.update(phase="verified", completed_tick=native["tick"])
                _atomic_json(self.path, ledger)
                self.last_diagnostic = dict(
                    ok=True, reason="normally repaired and recovered completed raid wave", history=run["history"]
                )
                return None
            for row in active:
                if row.get("pending_intent"):
                    return dict(
                        action,
                        post_name=row["name"],
                        reason="finish exact ordinary raid mining intent with current conserved inventory evidence",
                    )
            ready = [q for q in active if q.get("safe") and q.get("covered")]
            if not ready:
                self.last_diagnostic = dict(reason="waiting_for_safe_covered_raid_recovery", posts=active)
                return None
            damaged = [q for q in active if q["health"] < q["max_health"]]
            if damaged:
                ready = [q for q in ready if q["health"] < q["max_health"]]
                if not ready:
                    self.last_diagnostic = dict(reason="waiting_for_covered_native_raid_repairs")
                    return None
            else:
                ready = [q for q in ready if q.get("remaining_cover")]
                if not ready:
                    self.last_diagnostic = dict(reason="waiting_for_armed_rear_path_before_recovery")
                    return None
            ready.sort(key=lambda q: (q["health"] / q["max_health"], -q["rear_distance"], q["name"]))
            target = ready[0]
            if target["health"] < target["max_health"]:
                if not obs.get("enabled_recipes", {}).get("repair-pack"):
                    self.last_diagnostic = dict(reason="normal_repair_pack_research_required")
                    return None
                count = obs.get("inventory", {}).get("repair-pack", 0)
                if count > 1:
                    self.last_diagnostic = dict(reason="exactly_one_carried_native_repair_pack_required")
                    return None
                stock = self.builder.bootstrap.ensure_item(obs, "repair-pack", 1)
                if not isinstance(stock, dict):
                    self.last_diagnostic = dict(reason="waiting_for_normal_repair_pack")
                    return None
                if stock.get("type"):
                    return stock
                if stock.get("status") != "succeeded":
                    self.last_diagnostic = stock
                    return None
                if not target.get("reachable"):
                    return dict(
                        type="move",
                        position=target["approach"],
                        reason="approach safe exact paid raid gun for native tool repair",
                    )
                request = str(uuid.uuid4())
                run["repair_requests"].append(
                    dict(
                        request=request,
                        unit_number=target["unit_number"],
                        health_before=target["health"],
                        tick=native["tick"],
                    )
                )
                _atomic_json(self.path, ledger)
                return dict(
                    type="repair",
                    request=request,
                    name="gun-turret",
                    position=target["position"],
                    expected_entity_unit=target["unit_number"],
                    expected_world_id=obs["world_id"],
                    expected_actor_unit=obs["actor_unit_number"],
                    return_to_spawn=True,
                    reason="repair exact paid raid gun to full health before normal recovery",
                )
            if not target.get("capacity"):
                self.last_diagnostic = dict(
                    reason="aggregate_raid_recovery_inventory_capacity_missing",
                    required_empty_slots=target["required_empty_slots"],
                )
                return None
            return dict(
                action,
                post_name=target["name"],
                reason="normally mine full-health completed raid gun preserving every partial magazine round",
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.last_diagnostic = dict(ok=False, reason=str(exc))
            return None

    def victory_evidence(self, obs):
        """Historical victory plus current paid recovery facts; never standing fiction."""
        ready = self.prepare(obs)
        if not ready.get("ok"):
            return None
        native = ready["native"]
        p = ready["authority"]
        return dict(
            victory=deepcopy(p["victory"]),
            tick=native["tick"],
            recovery_id=ready["recovery_id"],
            surviving_posts=[q for q in native["posts"] if q.get("present")],
            recovered_posts=[q["receipt"] for q in native["posts"] if q.get("receipt")],
            combat_losses=[q for q in native["posts"] if q.get("lost_tick") is not None],
            scope="original exact projectile victory retained; normal recovery and actual current standing units reported separately",
        )

    def proof(self, obs):
        try:
            ledger = self._ledger(obs)
            evidence = self.victory_evidence(obs)
            if not evidence:
                return None
            run = ledger["operations"].get(evidence["recovery_id"])
            return (
                dict(evidence, history=run["history"])
                if run and run["phase"] == "verified" and not evidence["surviving_posts"]
                else None
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None


RECOVERY_GUARD_LUA = r"""
local marker=nil
local function reject(reason)return {ok=false,reason=reason,effects_started=marker~=nil}end
local frame=x.frame
if not d or d.world_id~=frame.expected_world_id or not a or not a.valid or a.unit_number~=frame.expected_actor_unit or a.force~=f or a.surface~=s then return reject("raid_recovery_actor_or_world_changed")end
local player=a.player
if not player or player.name~="FactoryAutomaton"or not player.connected or player.character~=a or d.actor~=a
 or d.crafting_player_index~=player.index or d.crafting_actor_unit_number~=a.unit_number then return reject("raid_recovery_pinned_owner_changed")end
if a.unit_number~=x.original_actor then
 local key=d.world_id..":"..player.index..":"..x.original_actor..":"..a.unit_number
 local change=d.actor_successions and d.actor_successions[key]
 if not change or change.kind~="owner-character-succession-v1"or change.old_unit~=x.original_actor or change.new_unit~=a.unit_number
  or change.world_id~=d.world_id or change.owner_index~=player.index or change.force_index~=f.index or change.surface_index~=s.index
  or change.old_pointer_invalid~=true or change.old_character_absent~=true then return reject("raid_recovery_owner_succession_missing")end
end
local function same(q,r)
 if type(q)~=type(r)then return false end;if type(q)~="table"then return q==r end
 for k,v in pairs(q)do if not same(v,r[k])then return false end end
 for k in pairs(r)do if q[k]==nil then return false end end;return true
end
local function plain(st,name)return st and st.valid_for_read and st.name==name and st.quality.name=="normal"and st.health==1 and st.spoil_tick==0
 and not st.is_item_with_tags and not st.is_item_with_inventory and not st.is_item_with_entity_data end
local sizes={};for _,name in ipairs({"firearm-magazine","piercing-rounds-magazine"})do local p=prototypes.item[name]
 if not p or p.type~="ammo"or not p.magazine_size or p.magazine_size<=0 or not p.stack_size or p.stack_size<=0 then return reject("raid_recovery_ammo_prototype_missing")end
 sizes[name]=p.magazine_size
end
local op=d.nest_assaults and d.nest_assaults[x.operation_id]
local group=d.defense_deployment_groups and d.defense_deployment_groups[x.group_id];local g=group and group.receipt
if not op or op.world_id~=d.world_id or op.catalog_fingerprint~=frame.expected_catalog_fingerprint or op.actor_unit~=x.original_actor
 or op.player_force_index~=f.index or op.surface_index~=s.index
 or op.target_set_hash~=x.target_set_hash or op.wave_group_id~=x.group_id or not group or group.phase~="complete"
 or group.owner_kind~="nest_sweep"or group.actor_unit~=x.original_actor or group.world_id~=d.world_id or group.catalog_fingerprint~=frame.expected_catalog_fingerprint
 or group.plan_revision~=x.plan_revision or not g or g.group_id~=x.group_id or not sizes[g.ammo_item]
 or g.actor_unit~=x.original_actor or g.world_id~=d.world_id or g.catalog_fingerprint~=frame.expected_catalog_fingerprint or g.plan_revision~=x.plan_revision
 or g.paid_turrets~=#x.posts or g.loaded_magazines~=#x.posts*20 or g.loaded_rounds~=#x.posts*20*sizes[g.ammo_item]
 or g.same_tick~=true then return reject("raid_recovery_original_paid_victory_changed")end
for _,q in ipairs(x.targets)do local death=op.deaths[tostring(q.unit_number)]
 if not death or death.own_gun_projectile~=true or death.unit_number~=q.unit_number or death.name~=q.name
  or death.position.x~=q.position.x or death.position.y~=q.position.y or target(q.position,q.name)then return reject("raid_recovery_native_target_victory_required")end
end
local function exact(q)local e=target(q.position,"gun-turret")
 if not e or not e.valid or e.unit_number~=q.unit_number or e.force~=f or e.surface~=s or e.quality.name~="normal"
  or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end;return e
end
local function ammo(inv)
 if not inv or not inv.valid then return nil end
 local result={};for name in pairs(sizes)do result[name]={magazines=0,rounds=0,full=0}end
 for i=1,#inv do local st=inv[i];if st.valid_for_read then local value=result[st.name];local size=sizes[st.name]
  if not value or not plain(st,st.name)or st.prototype.type~="ammo"or st.count%1~=0 or st.count<1
   or type(st.ammo)~="number"or st.ammo%1~=0 or st.ammo<1 or st.ammo>size then return nil end
  value.magazines=value.magazines+st.count;value.rounds=value.rounds+(st.count-1)*size+st.ammo;value.full=value.full+st.count-(st.ammo<size and 1 or 0)
 end end;return result
end
local main=a.get_main_inventory();local equipped=a.get_inventory(defines.inventory.character_ammo)
if not main or not main.valid or not equipped or not equipped.valid or main==equipped or #main>2000 or #equipped>100 then return reject("raid_recovery_actor_inventory_missing")end
local function totals()
 local result={gun_turrets=0,ammo={},snapshot={},other_totals={}}
 for name in pairs(sizes)do result.ammo[name]={magazines=0,rounds=0}end
 for index,inv in ipairs({main,equipped})do local slots={};result.snapshot[index]=slots;local other={};result.other_totals[index]=other
  for i=1,#inv do local st=inv[i];local row={slot=i};slots[i]=row
   if st.valid_for_read then row.name=st.name;row.count=st.count;row.quality=st.quality.name
    if st.name=="gun-turret"then if not plain(st,st.name)or st.prototype.type~="item"then return nil end;result.gun_turrets=result.gun_turrets+st.count
    elseif sizes[st.name]then
     if not plain(st,st.name)or st.prototype.type~="ammo"or st.ammo<1 or st.ammo>sizes[st.name]or st.ammo%1~=0 then return nil end
     row.magazine_rounds=st.ammo;local v=result.ammo[st.name];v.magazines=v.magazines+st.count;v.rounds=v.rounds+(st.count-1)*sizes[st.name]+st.ammo
    else local names=other[st.name]or{};other[st.name]=names;names[st.quality.name]=(names[st.quality.name]or 0)+st.count end
   else row.empty=true end
  end
 end;return result
end
local function conserved(before,after,stock)
 if not before or not after or after.gun_turrets~=before.gun_turrets+1 or not same(before.other_totals,after.other_totals)then return false end
 for name in pairs(sizes)do if after.ammo[name].magazines~=before.ammo[name].magazines+stock[name].magazines
  or after.ammo[name].rounds~=before.ammo[name].rounds+stock[name].rounds then return false end end;return true
end
local records=d.post_raid_recoveries and d.post_raid_recoveries[x.recovery_id]or{}
local live={};local states={}
for _,q in ipairs(x.posts)do
 local paid=d.defense_deployments and d.defense_deployments[q.deployment_id];local receipt=paid and paid.receipt
 if not paid or paid.phase~="complete"or paid.owner_kind~="nest_sweep"or paid.world_id~=d.world_id or paid.catalog_fingerprint~=frame.expected_catalog_fingerprint
  or paid.actor_unit~=x.original_actor or paid.post_name~=q.name or paid.unit_number~=q.unit_number or not receipt or receipt.paid_turrets~=1
  or paid.ammo_item~=q.ammo_item or receipt.deployment_id~=q.deployment_id or receipt.actor_unit~=x.original_actor
  or receipt.world_id~=d.world_id or receipt.catalog_fingerprint~=frame.expected_catalog_fingerprint
  or receipt.loaded_magazines~=20 or receipt.loaded_rounds~=20*sizes[q.ammo_item]or receipt.ammo_item~=q.ammo_item
  or receipt.position.x~=q.position.x or receipt.position.y~=q.position.y then return reject("raid_recovery_paid_post_identity_changed")end
 local e=target(q.position,"gun-turret");local prior=records[q.name]
 if e and not exact(q)then return reject("raid_recovery_post_replaced_or_foreign")end
 if prior and(prior.world_id~=d.world_id or prior.actor_unit~=a.unit_number or prior.old_unit~=q.unit_number or not same(prior.position,q.position))then return reject("raid_recovery_native_intent_changed")end
 if prior and prior.phase=="complete"and e then return reject("raid_recovery_retired_site_reoccupied")end
 if not e and not prior and not(op.lost_posts or{})[tostring(q.unit_number)]then return reject("raid_recovery_missing_post_without_exact_loss_or_intent")end
 if e then live[q.name]=e end
 states[q.name]=prior
end
local function quiet(p)
 if s.count_entities_filtered{position=p,radius=32,force="enemy",type={"unit","unit-spawner"}}>0 then return false end
 local maximum=0
 for _,proto in pairs(prototypes.entity)do if proto.type=="turret"then local attack=proto.attack_parameters
  if not attack or not attack.range or attack.range>124 then return false end;maximum=math.max(maximum,attack.range)
 end end
 for _,enemy in pairs(s.find_entities_filtered{position=p,radius=maximum+4,force="enemy",type="turret"})do
  local attack=enemy.prototype.attack_parameters
  if not attack or not attack.range or(enemy.position.x-p.x)^2+(enemy.position.y-p.y)^2<=(attack.range+3)^2 then return false end
 end;return true
end
local function armed(e,minimum)
 if not e or e.health<e.max_health*.5 then return false end
 local stock=ammo(e.get_inventory(defines.inventory.turret_ammo));if not stock then return false end
 local full=0;for _,v in pairs(stock)do full=full+v.full end;return full>=minimum
end
local function covered(q,approach)
 local candidates={}
 for _,post in ipairs(x.posts)do if post.name~=q.name then candidates[#candidates+1]={entity=live[post.name],minimum=1}end end
 for _,post in ipairs(x.covers)do candidates[#candidates+1]={entity=exact(post),minimum=5}end
 for _,candidate in ipairs(candidates)do local gun=candidate.entity
  if armed(gun,candidate.minimum)then local radius=gun.prototype.attack_parameters.range-2
   if radius>0 and(gun.position.x-q.position.x)^2+(gun.position.y-q.position.y)^2<=radius^2
    and(gun.position.x-approach.x)^2+(gun.position.y-approach.y)^2<=radius^2 then return true end
  end
 end;return false
end
local function capacity(stock)
 local required=1;local empty=0
 for i=1,#main do if not main[i].valid_for_read and(not main.supports_filters()or not main.get_filter(i))then empty=empty+1 end end
 for name,v in pairs(stock)do required=required+math.ceil(v.magazines/prototypes.item[name].stack_size)+(v.full<v.magazines and 1 or 0)
  if main.get_insertable_count{name=name,quality="normal"}<v.magazines then return false,required end
 end
 return empty>=required and main.get_insertable_count{name="gun-turret",quality="normal"}>=1,required
end
local function remaining_cover(removed)
 local reached={};local queue={};local rears={}
 for _,q in ipairs(x.covers)do local e=exact(q);if armed(e,5)then rears[#rears+1]=e end end
 local function supports(gun,e)local radius=gun.prototype.attack_parameters.range-2
  return radius>0 and(gun.position.x-e.position.x)^2+(gun.position.y-e.position.y)^2<=radius^2 end
 local function visit(name,e)
  reached[name]=true;if armed(e,1)then queue[#queue+1]=e end
 end
 for name,e in pairs(live)do if name~=removed then for _,back in ipairs(rears)do if supports(back,e)then visit(name,e);break end end end end
 local head=1;while head<=#queue do local gun=queue[head];head=head+1
  for name,e in pairs(live)do if name~=removed and not reached[name]and supports(gun,e)then visit(name,e)end end
 end
 for name in pairs(live)do if name~=removed and not reached[name]then return false end end;return true
end
"""

INSPECT_RECOVERY_LUA = r"""
local rows={};local rear={x=0,y=0}
for _,q in ipairs(x.covers)do rear.x=rear.x+q.position.x;rear.y=rear.y+q.position.y end
if #x.covers>0 then rear.x=rear.x/#x.covers;rear.y=rear.y/#x.covers end
for _,q in ipairs(x.posts)do local e=live[q.name];local prior=states[q.name];local row={name=q.name,unit_number=q.unit_number,position=q.position,present=not not e}
 if prior and prior.phase=="complete"then row.receipt=prior.receipt
 elseif not e then
  if prior then row.pending_intent=true else row.lost_tick=op.lost_posts[tostring(q.unit_number)]end
 else
  local stock=ammo(e.get_inventory(defines.inventory.turret_ammo));if not stock then return reject("raid_recovery_post_ammo_metadata_unsupported")end
  local approach=s.find_non_colliding_position("character",e.position,3,.5)
  row.health=e.health;row.max_health=e.max_health;row.ammo=stock;row.approach=approach
  row.safe=not not(approach and quiet(q.position)and quiet(approach));row.covered=not not(approach and covered(q,approach))
  row.reachable=a.can_reach_entity(e);row.capacity,row.required_empty_slots=capacity(stock)
  row.remaining_cover=remaining_cover(q.name)
  row.rear_distance=(q.position.x-rear.x)^2+(q.position.y-rear.y)^2
 end;rows[#rows+1]=row
end
return {ok=true,tick=game.tick,posts=rows}
"""

RECOVER_POST_LUA = r"""
local q=nil;for _,post in ipairs(x.posts)do if post.name==x.post_name then q=post end end
if not q then return reject("raid_recovery_unknown_registered_post")end
marker=states[q.name];local e=live[q.name]
if marker and marker.phase=="complete"then return marker.receipt end
local before=totals();if not before then return reject("raid_recovery_actor_ammo_metadata_unsupported")end
if not e then
 if not marker or marker.phase~="intent"or not conserved(marker.before,before,marker.old_ammo)then return reject("raid_recovery_missing_post_conservation_unproved")end
else
 local stock=ammo(e.get_inventory(defines.inventory.turret_ammo))
 if e.health~=e.max_health or not stock then return reject("raid_recovery_full_health_and_plain_ammo_required")end
 local approach=s.find_non_colliding_position("character",e.position,3,.5)
 if not approach or not quiet(q.position)or not quiet(approach)or not covered(q,approach)then return reject("raid_recovery_fresh_safety_or_cover_required")end
 if not remaining_cover(q.name)then return reject("raid_recovery_remaining_raid_posts_need_armed_rear_path")end
 local fits=capacity(stock);if not fits then return reject("raid_recovery_aggregate_inventory_capacity_missing")end
 if marker and(not same(marker.before,before)or not same(marker.old_ammo,stock))then return reject("raid_recovery_pre_mine_snapshot_changed")end
 if not marker then
  marker={phase="intent",world_id=d.world_id,catalog_fingerprint=frame.expected_catalog_fingerprint,actor_unit=a.unit_number,
   old_unit=q.unit_number,position=q.position,before=before,old_ammo=stock,full_health_before=true,tick=game.tick}
  d.post_raid_recoveries=d.post_raid_recoveries or{};d.post_raid_recoveries[x.recovery_id]=records;records[q.name]=marker
 end
 if not a.mine_entity(e)then return reject("raid_recovery_ordinary_mining_failed")end
 marker.native_mine_returned_true=true;before=totals()
 if target(q.position,"gun-turret")or not conserved(marker.before,before,marker.old_ammo)then return reject("raid_recovery_count_or_round_conservation_failed")end
end
marker.phase="complete";marker.receipt={ok=true,status="succeeded",kind="ordinary_full_health_raid_mine_v1",recovery_id=x.recovery_id,post_name=q.name,
 world_id=d.world_id,catalog_fingerprint=frame.expected_catalog_fingerprint,actor_unit=a.unit_number,unit_number=q.unit_number,position=q.position,
 recovered_turrets=1,old_ammo=marker.old_ammo,magazine_sizes=sizes,before=marker.before,after=before,
 full_health_before=true,inventory_scope="character_main+character_ammo",intent_tick=marker.tick,tick=game.tick,recovered_from_intent=not not(not e)}
return marker.receipt
"""
