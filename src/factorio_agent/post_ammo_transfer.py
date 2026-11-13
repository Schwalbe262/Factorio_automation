"""Guarded existing-post ammunition conversion; never recreate partial stacks."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

YELLOW = "firearm-magazine"
RED = "piercing-rounds-magazine"
ITEMS = (YELLOW, RED)
ACTION = "upgrade_post_ammunition"
POLICY_FILE = "defense-ammo-policy.json"
POST_FIELDS = ("name", "position", "unit_number", "coverage_scope", "local_walk_access")


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def signature(post):
    return {k: post.get(k) for k in POST_FIELDS}


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def active_reservations(root, world, catalog):
    """Reserve only ammunition for a currently unfired registered operation."""
    witnesses = []
    count = 0
    for filename in ("nest-sweep.json", "nest-assault.json"):
        path = Path(root) / filename
        if not path.exists():
            continue
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("world_id") != world or state.get("catalog_fingerprint") != catalog:
            raise ValueError("active ammunition reservation world or catalog changed")
        if state.get("phase") not in ("stock", "register", "deploy"):
            continue
        if state.get("ammo_item") != RED:
            continue
        if filename == "nest-sweep.json":
            wave = state.get("wave", {})
            if state.get("phase") == "deploy" and wave.get("bindings"):
                continue
            amount = state.get("requirements", {}).get(RED)
        else:
            stages = state.get("stages", [])
            index = state.get("active_stage")
            if type(index) is not int or not 0 <= index < len(stages):
                raise ValueError("active assault reservation stage missing")
            stage = stages[index]
            amount = (
                state.get("requirements", {}).get(RED)
                if not state.get("started_tick")
                else 20 * sum(p.get("name") not in stage.get("bindings", {}) for p in stage.get("posts", []))
            )
        if type(amount) is not int or not 0 <= amount <= 240:
            raise ValueError("active ammunition reservation must be explicit and bounded")
        count += amount
        witnesses.append(dict(file=filename, id=state.get("id"), phase=state["phase"], amount=amount))
    if count > 480:
        raise ValueError("aggregate active ammunition reservations exceed bounded actor target")
    return dict(count=count, revision=revision(witnesses), operations=witnesses)


def validate_action(action, world=None):
    required = {
        "type",
        "name",
        "post_name",
        "position",
        "item",
        "count",
        "expected_entity_unit",
        "expected_entity_world_id",
        "expected_actor_unit",
        "expected_catalog_fingerprint",
        "transaction_id",
    }
    if (
        not isinstance(action, dict)
        or set(action) - required - {"reason"}
        or not required <= set(action)
        or action.get("type") != ACTION
        or action.get("name") != "gun-turret"
        or action.get("item") != RED
        or not isinstance(action.get("post_name"), str)
        or not action["post_name"]
        or any(type(action.get(k)) is not int or action[k] < 1 for k in ("expected_entity_unit", "expected_actor_unit"))
        or type(action.get("count")) is not int
        or not 1 <= action["count"] <= 10
        or any(
            not isinstance(action.get(k), str) or not action[k]
            for k in ("expected_entity_world_id", "expected_catalog_fingerprint")
        )
        or not isinstance(action.get("transaction_id"), str)
        or len(action["transaction_id"]) != 64
        or any(c not in "0123456789abcdef" for c in action["transaction_id"])
        or ("reason" in action and not isinstance(action["reason"], str))
    ):
        raise ValueError("exact registered post ammunition transaction required")
    p = action.get("position")
    if (
        not isinstance(p, dict)
        or set(p) != {"x", "y"}
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in p.values())
    ):
        raise ValueError("finite exact post position required")
    if world is not None and action["expected_entity_world_id"] != world:
        raise ValueError("post ammunition world changed")


def _context(game, action):
    validate_action(action, getattr(game, "expected_world", None))
    if getattr(game, "backend", None) not in ("assisted", "character"):
        raise ValueError("ordinary post ammunition backend required")
    root = Path(game.cfg.runtime_dir)
    state = json.loads((root / POLICY_FILE).read_text(encoding="utf-8"))
    perimeter = json.loads((root / "defense-perimeter.json").read_text(encoding="utf-8"))
    world, catalog = action["expected_entity_world_id"], action["expected_catalog_fingerprint"]
    if any(q.get("world_id") != world or q.get("catalog_fingerprint") != catalog for q in (state, perimeter)):
        raise ValueError("post ammunition registered world or catalog changed")
    txn = state.get("transactions", {}).get(action["transaction_id"], {})
    payload = txn.get("payload", {})
    if (
        txn.get("phase") not in ("pending", "complete")
        or revision(payload) != action["transaction_id"]
        or payload.get("action") != {k: v for k, v in action.items() if k not in ("transaction_id", "reason")}
        or state.get("preferences", {}).get(action["post_name"], {}).get("item") != RED
    ):
        raise ValueError("immutable paid ammunition conversion intent missing or changed")
    posts = [p for p in perimeter.get("posts", []) if p.get("name") == action["post_name"]]
    if (
        len(posts) != 1
        or signature(posts[0]) != payload.get("post")
        or posts[0].get("unit_number") != action["expected_entity_unit"]
    ):
        raise ValueError("registered post identity or geometry changed")
    supports = payload.get("supports")
    if not isinstance(supports, list) or len(supports) != 2 or len({p.get("unit_number") for p in supports}) != 2:
        raise ValueError("two distinct registered supporting posts required")
    for support in supports:
        matches = [p for p in perimeter.get("posts", []) if p.get("name") == support.get("name")]
        if len(matches) != 1 or signature(matches[0]) != support or support["name"] == action["post_name"]:
            raise ValueError("registered supporting post changed")
    reservation = active_reservations(root, world, catalog)
    if reservation != payload.get("reservation"):
        raise ValueError("active operation ammunition reservation changed")
    stock = payload.get("stock")
    if stock is not None and (
        stock.get("name") not in ITEMS
        or type(stock.get("count")) is not int
        or stock["count"] < 1
        or type(stock.get("ammo")) is not int
        or stock["ammo"] < 1
    ):
        raise ValueError("exact prior turret stack required")
    if (stock is None or stock.get("name") == YELLOW) and action["count"] != 10:
        raise ValueError("conversion must prepay ten full red magazines")
    return dict(
        action, supports=supports, stock=stock, reservation=reservation, character_backend=game.backend == "character"
    )


def execute_upgrade_post_ammunition(game, action):
    """One native tick, durable intent/receipt, no turret removal or health change."""
    context = _context(game, action)
    return game.query("local x=helpers.json_to_table(" + _payload(context) + ");" + UPGRADE_POST_AMMUNITION_LUA)


def valid_receipt(receipt, action):
    """Validate a native complete marker without substituting current combat stock."""
    if not isinstance(receipt, dict):
        return False
    fields = {
        "transaction_id": "transaction_id",
        "world_id": "expected_entity_world_id",
        "catalog_fingerprint": "expected_catalog_fingerprint",
        "actor_unit": "expected_actor_unit",
        "unit_number": "expected_entity_unit",
        "post_name": "post_name",
        "item": "item",
        "moved": "count",
    }
    if (
        any(receipt.get(k) != action.get(v) for k, v in fields.items())
        or receipt.get("ok") is not True
        or type(receipt.get("start_tick")) is not int
        or receipt["start_tick"] != receipt.get("finish_tick")
        or type(receipt.get("magazine_size")) is not int
        or receipt["magazine_size"] < 1
        or receipt.get("loaded_rounds") != action["count"] * receipt["magazine_size"]
        or receipt.get("quality") != "normal"
    ):
        return False
    try:
        for name in ITEMS:
            snapshots = [receipt[k][name] for k in ("before_main", "before_post", "after_main", "after_post")]
            if any(
                type(q.get(field)) is not int or q[field] < 0
                for q in snapshots
                for field in ("count", "rounds", "full")
            ):
                return False
            if any(q["full"] > q["count"] for q in snapshots):
                return False
            for field in ("count", "rounds"):
                if snapshots[0][field] + snapshots[1][field] != snapshots[2][field] + snapshots[3][field]:
                    return False
        return (
            receipt["after_main"][RED]["count"] == receipt["before_main"][RED]["count"] - action["count"]
            and receipt["after_post"][RED]["rounds"] == receipt["before_post"][RED]["rounds"] + receipt["loaded_rounds"]
            and receipt.get("yellow_recovered") == receipt["before_post"][YELLOW]
        )
    except (KeyError, TypeError):
        return False


AMMUNITION_LUA = r"""
local function reject(reason)return {ok=false,status="blocked",reason=reason,moved=0}end
if not d or d.world_id~=x.expected_entity_world_id then return reject("post_ammunition_world_changed")end
if not a or not a.valid or a.force~=f or a.surface~=s or a.unit_number~=x.expected_actor_unit then return reject("post_ammunition_actor_changed")end
local sizes={};for _,name in ipairs{"firearm-magazine","piercing-rounds-magazine"}do
 local p=prototypes.item[name];if not p or p.type~="ammo"or not p.magazine_size or p.magazine_size<1 or not p.stack_size or p.stack_size<10
  or not p.ammo_category or p.ammo_category.name~="bullet"then return reject("post_ammunition_prototype_changed")end
 sizes[name]=p.magazine_size
end
local function plain(st)
 return st.valid_for_read and sizes[st.name]and st.quality.name=="normal"and st.prototype.type=="ammo"
  and st.health==1 and st.spoil_tick==0 and not st.is_item_with_tags and not st.is_item_with_inventory
  and not st.is_item_with_entity_data and st.count>0 and st.count%1==0
  and st.ammo>=1 and st.ammo<=sizes[st.name]and st.ammo%1==0
end
local function totals(inv)
 local out={};for name,_ in pairs(sizes)do out[name]={count=0,rounds=0,full=0}end
 for i=1,#inv do local st=inv[i];if st.valid_for_read and sizes[st.name]then
  if not plain(st)then return nil end
  local q=out[st.name];local size=sizes[st.name];q.count=q.count+st.count
  q.rounds=q.rounds+(st.count-1)*size+st.ammo;q.full=q.full+st.count-(st.ammo<size and 1 or 0)
 end end;return out
end
local function owned(q)
 local e=target(q.position,"gun-turret")
 if not e or not e.valid or e.force~=f or e.surface~=s or e.unit_number~=(q.unit_number or q.expected_entity_unit)
  or e.position.x~=q.position.x or e.position.y~=q.position.y or e.quality.name~="normal"or e.health<=0 then return nil end
 return e
end
"""


UPGRADE_POST_AMMUNITION_LUA = (
    AMMUNITION_LUA
    + r"""
local e=owned(x);if not e then return reject("post_ammunition_owned_turret_changed")end
if x.character_backend and not a.can_reach_entity(e)then return reject("post_ammunition_out_of_reach")end
local main=a.get_main_inventory();local dest=e.get_inventory(defines.inventory.turret_ammo)
if not main or not main.valid or not dest or not dest.valid or #dest~=1 then return reject("post_ammunition_inventory_changed")end
local marker=d.defense_ammo_upgrades and d.defense_ammo_upgrades[x.transaction_id]
if marker then
 if marker.world_id~=x.expected_entity_world_id or marker.actor_unit~=x.expected_actor_unit
  or marker.unit_number~=e.unit_number then return reject("post_ammunition_marker_identity_changed")end
 if marker.phase~="complete"or not marker.receipt then return reject("post_ammunition_uncertain_prior_effects")end
 local r={};for k,v in pairs(marker.receipt)do r[k]=v end;r.reused_receipt=true;return r
end
local recipe=f.recipes["piercing-rounds-magazine"]
if not recipe or not recipe.enabled then return reject("post_ammunition_piercing_not_unlocked")end
local seen={};local range=prototypes.entity["gun-turret"].attack_parameters.range-2
for _,q in ipairs(x.supports)do local back=owned(q)
 if not back or seen[back.unit_number]or back.unit_number==e.unit_number or back.health<back.max_health*.5
  or(back.position.x-e.position.x)^2+(back.position.y-e.position.y)^2>range^2 then return reject("post_ammunition_support_changed")end
 local ammo=totals(back.get_inventory(defines.inventory.turret_ammo))
 if not ammo or ammo["firearm-magazine"].full+ammo["piercing-rounds-magazine"].full<1 then return reject("post_ammunition_support_unarmed")end
 seen[back.unit_number]=true
end
local before_main=totals(main);local before_post=totals(dest)
if not before_main or not before_post then return reject("post_ammunition_metadata_unsupported")end
local st=dest[1]
if st.valid_for_read then
 if not plain(st)or not x.stock or st.name~=x.stock.name or st.count>x.stock.count
  or(st.count-1)*sizes[st.name]+st.ammo>(x.stock.count-1)*sizes[st.name]+x.stock.ammo then return reject("post_ammunition_prior_stack_changed")end
end
if before_main["piercing-rounds-magazine"].full<x.count+x.reservation.count then return reject("post_ammunition_full_red_prepayment_or_reserved_stock_missing")end
local free=nil
if st.valid_for_read and st.name=="firearm-magazine"then
 for i=1,#main do if not main[i].valid_for_read then free=i;break end end
 if not free then return reject("post_ammunition_empty_yellow_recovery_slot_required")end
end
if st.valid_for_read and st.name=="piercing-rounds-magazine"and dest.get_insertable_count{name=st.name,quality="normal"}<x.count then return reject("post_ammunition_red_capacity_missing")end
d.defense_ammo_upgrades=d.defense_ammo_upgrades or{}
marker={phase="intent",world_id=x.expected_entity_world_id,actor_unit=a.unit_number,unit_number=e.unit_number,
 before_main=before_main,before_post=before_post,start_tick=game.tick,transaction_id=x.transaction_id}
d.defense_ammo_upgrades[x.transaction_id]=marker
if free then
 if not main[free].swap_stack(dest[1])then return reject("post_ammunition_yellow_swap_failed")end
 marker.phase="yellow_recovered";marker.yellow_slot=free
end
local moved=0
while moved<x.count do
 local index=nil;local amount=0
 for i=1,#main do local stack=main[i]
  if stack.valid_for_read and stack.name=="piercing-rounds-magazine"then
   if not plain(stack)then return reject("post_ammunition_red_source_changed")end
   local available=stack.count-(stack.ammo<sizes[stack.name]and 1 or 0)
   if available>0 then index=i;amount=math.min(x.count-moved,available);break end
  end
 end
 if not index then return reject("post_ammunition_red_source_exhausted")end
 local source=main[index];local original=source.count
 local inserted=dest.insert{name="piercing-rounds-magazine",quality="normal",count=amount}
 if type(inserted)~="number"or inserted<0 or inserted>amount or inserted%1~=0 then return reject("post_ammunition_native_insert_invalid")end
 source.count=original-inserted;moved=moved+inserted;marker.loaded=moved
 if inserted~=amount then return reject("post_ammunition_native_insert_incomplete")end
end
local after_main=totals(main);local after_post=totals(dest)
if not after_main or not after_post or game.tick~=marker.start_tick then return reject("post_ammunition_after_snapshot_invalid")end
for name,_ in pairs(sizes)do
 if before_main[name].count+before_post[name].count~=after_main[name].count+after_post[name].count
  or before_main[name].rounds+before_post[name].rounds~=after_main[name].rounds+after_post[name].rounds then return reject("post_ammunition_count_or_round_conservation_failed")end
end
marker.phase="complete";marker.receipt={ok=true,status="succeeded",transaction_id=x.transaction_id,world_id=x.expected_entity_world_id,
 catalog_fingerprint=x.expected_catalog_fingerprint,actor_unit=a.unit_number,unit_number=e.unit_number,post_name=x.post_name,
 item="piercing-rounds-magazine",moved=moved,loaded_rounds=moved*sizes["piercing-rounds-magazine"],
 magazine_size=sizes["piercing-rounds-magazine"],
 yellow_recovered=before_post["firearm-magazine"],before_main=before_main,before_post=before_post,after_main=after_main,after_post=after_post,
 start_tick=marker.start_tick,finish_tick=game.tick,quality="normal"}
return marker.receipt
"""
)
