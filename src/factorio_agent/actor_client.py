"""Client mode of a run: ``always`` (default), ``none`` (client-free test runs) or ``until_lab_trigger``.

``actor-client.json`` in the runtime folder holds the option and, once it
happened, the recorded transition. A missing file is ``always``: the
interactive map and every older runtime behave as before.

``none`` (client-free test runs, ``tools/fleet.py start`` default): no client ever.
``adopt`` records the server-created character (no player) as the actor
(``d.detached_actor`` with ``mode="none"``). The engine does not credit a hand
craft of a player-less character, so a ``craft-item`` trigger that needs a hand
craft never fires. The written test-mode exception to "nothing granted"
(AGENTS.md) has two parts: (a) after the real craft (recorded in the world by
the ``craft`` action with the items it consumed), ``credit_hand_craft_trigger``
marks exactly that technology researched; (b) ``emulate_player_presence``
charts (recorded, since the engine charts nothing for a force without players)
and generates the map around the actor as a connected player would. The run is
labelled ``test_mode: client_free`` (``run.json``, this file, reports) with
every credit and its tick and the presence radii.

``until_lab_trigger`` (fleet runs, ``tools/fleet.py start``): the run starts as
today (client connected, actor bound to its player). A hand craft is credited
to the force (production statistics, ``craft-item`` triggers) only while a
connected player controls the character (docs/v2/HEADLESS.md). Once
``automation-science-pack`` is researched (the "craft 1 lab" trigger fired),
no other trigger on the roadmap needs a hand craft and the crafting queue is
empty, ``detach`` sets the player's controller to spectator (a guarded
``detach_actor`` action with a receipt, ``d.detached_actor`` in the world),
closes the run's client by pid + creation time, checks the actor is still
valid and unowned after the disconnect and records the transition. The run
continues server-only. Checks that pinned the connected player accept the
detached actor through ``DETACHED_ACTOR_LUA`` (same unit number and world id
as recorded at the detach).

Death after the detach (``until_lab_trigger``): the corpse keeps the inventory
but nothing respawns without a player, so the supervisor records
``actor_dead_without_player`` and stops the run (no replacement character, no
granted items). In client-free mode the engine's player respawn is emulated as
part of the presence emulation (``actor_death``: a new empty character at the
spawn after the respawn delay; the corpse keeps every item).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .core.deterministic_state import _atomic_json

OPTION_FILE = "actor-client.json"
ALWAYS = "always"
NONE = "none"
UNTIL_LAB_TRIGGER = "until_lab_trigger"
MODES = (ALWAYS, NONE, UNTIL_LAB_TRIGGER)
TEST_MODE = "client_free"
CREDIT_ACTION = "credit_hand_craft_trigger"
PRESENCE_ACTION = "emulate_player_presence"
# What a connected player causes around itself (measured on detach-gui-1193005910
# against detach-none-1193005910, HEADLESS.md "Player presence"):
# - freeplay's on_player_created charts the starting area once: a square of
#   storage.chart_distance (default 200) tiles around the force spawn (196 chunks)
#   and sets the daytime to 0.7; its crash site holds items and is not recreated;
# - the map generator generates chunks around the player progressively: sampled
#   every 10 s on detach-gui2 (player at the spawn) 419 chunks at tick 2.5k, 767
#   at 18.4k and 1964 (46x46) at 78.7k, i.e. a radius of about
#   10 + tick / 6900 chunks, capped at 22; a script request generates at once,
#   so the requested radius follows that curve;
# - a player charts around its character as it moves; the assisted actor never
#   leaves the spawn chunk in those runs, so the radius is the commonly cited
#   3 chunks (inside the start square, it adds nothing there).
START_CHART_TILES = 200
JOIN_DAYTIME = 0.7
GENERATE_RADIUS_CHUNKS = 22
GENERATE_BASE_CHUNKS = 10
GENERATE_TICKS_PER_CHUNK = 6900
MOVE_CHART_RADIUS_CHUNKS = 3
LAB_TRIGGER = "automation-science-pack"
DEAD_WITHOUT_PLAYER = "actor_dead_without_player"
DISCONNECT_WAIT_SECONDS = 30

# True only for the actor recorded at the detach: same world, same unit number,
# still the framework actor and controlled by no player. Snippets that pinned
# the connected player include it and accept this as the equivalent identity.
DETACHED_ACTOR_LUA = r"""
local function detached_actor()
 local r=d and d.detached_actor
 return type(r)=="table" and r.kind=="detached-actor-v1" and r.world_id==d.world_id and a~=nil and a.valid
  and a.unit_number==r.unit_number and d.crafting_actor_unit_number==r.unit_number and d.actor==a and a.player==nil or false
end
"""

DETACH_LUA = (
    r"""
local x=helpers.json_to_table(PAYLOAD)
"""
    + DETACHED_ACTOR_LUA
    + r"""
local function blocked(reason,t)t=t or {};t.ok=false;t.status="blocked";t.reason=reason;t.tick=game.tick;return t end
if not d or d.world_id~=x.world_id then return blocked("actor_detach_world_changed")end
local owner=type(d.crafting_player_index)=="number"and game.get_player(d.crafting_player_index)or nil
if d.detached_actor~=nil then
 if detached_actor()then return {ok=true,status="succeeded",reason="actor_already_detached",already_detached=true,
  record=d.detached_actor,tick=game.tick,owner_connected=owner and owner.connected or false}end
 return blocked("actor_detach_record_mismatch")
end
if not a or not a.valid then return blocked("actor_detach_actor_missing")end
local lab=f.technologies[x.lab_trigger]
if not lab or not lab.researched then return blocked("actor_detach_lab_trigger_pending")end
for _,name in ipairs(x.hand_triggers or {})do
 local t=f.technologies[name]
 if not t or not t.researched then return blocked("actor_detach_hand_trigger_pending",{technology=name})end
end
if a.crafting_queue_size>0 then return {ok=true,status="waiting",reason="actor_detach_crafting_queue_busy",
 crafting_queue_size=a.crafting_queue_size,tick=game.tick}end
if not owner or not owner.valid or owner.name~=(d.crafting_player_name or"FactoryAutomaton")or not owner.connected
 or owner.character~=a or a.player~=owner or d.crafting_actor_unit_number~=a.unit_number or d.actor~=a then
 return blocked("actor_detach_pinned_owner_changed")end
owner.set_controller{type=defines.controllers.spectator}
if not a.valid or a.player~=nil or owner.character~=nil then
 return blocked("actor_detach_controller_change_failed",{effects_started=true})end
d.detached_actor={kind="detached-actor-v1",world_id=d.world_id,unit_number=a.unit_number,player_index=owner.index,
 player_name=owner.name,force_index=f.index,surface_index=s.index,tick=game.tick}
return {ok=true,status="succeeded",reason="actor_detached",already_detached=false,record=d.detached_actor,
 tick=game.tick,position=pos(a.position)}
"""
)

ADOPT_LUA = (
    r"""
local x=helpers.json_to_table(PAYLOAD)
"""
    + DETACHED_ACTOR_LUA
    + r"""
local function blocked(reason)return {ok=false,status="blocked",reason=reason,tick=game.tick}end
if not d or d.world_id~=x.world_id then return blocked("client_free_world_changed")end
if d.detached_actor~=nil then
 if d.detached_actor.mode=="none"and detached_actor()then return {ok=true,status="succeeded",
  reason="client_free_actor_already_adopted",already=true,record=d.detached_actor,tick=game.tick}end
 return blocked("client_free_record_mismatch")
end
if d.crafting_player_index~=nil then return blocked("client_free_actor_already_bound_to_a_player")end
if not a or not a.valid or a.name~="character"or a.force~=f or a.surface~=s then return blocked("client_free_actor_missing")end
if a.player~=nil then return blocked("client_free_actor_has_a_player")end
d.crafting_actor_unit_number=a.unit_number
d.detached_actor={kind="detached-actor-v1",mode="none",world_id=d.world_id,unit_number=a.unit_number,
 force_index=f.index,surface_index=s.index,tick=game.tick}
return {ok=true,status="succeeded",reason="client_free_actor_adopted",already=false,record=d.detached_actor,tick=game.tick}
"""
)

# Spliced into the craft action (deterministic_game) around begin_crafting.
# Client-free mode only: a craft whose product is the item of a craft-item
# trigger that is researchable now (prerequisites done) is recorded in the
# world with the items it consumed; the credit action requires this record.
TRIGGER_CRAFT_BEFORE_LUA = r"""
local trigger_track=d.detached_actor~=nil and d.detached_actor.mode=="none"
local trigger_before=trigger_track and contents(inv) or nil
"""

TRIGGER_CRAFT_RECORD_LUA = r"""
if trigger_track then
 for _,p in pairs(recipe.products)do if p.type=="item"then
  for name,t in pairs(f.technologies)do
   local tr=not t.researched and t.prototype.research_trigger
   local it=tr and tr.type=="craft-item"and tr.item
   local item=type(it)=="table"and it.name or it
   local ready=item==p.name
   if ready then for _,q in pairs(t.prerequisites)do if not q.researched then ready=false end end end
   if ready then
    local after=contents(inv);local used={};local any=false
    for k,v in pairs(trigger_before)do local left=after[k]or 0;if left<v then used[k]=v-left;any=true end end
    d.trigger_crafts=d.trigger_crafts or {}
    if #d.trigger_crafts>=16 then table.remove(d.trigger_crafts,1)end
    d.trigger_crafts[#d.trigger_crafts+1]={technology=name,item=item,recipe=x.recipe,count=n,tick=game.tick,
     item_count_before=trigger_before[item]or 0,consumed=used,consumed_any=any,
     min_craft_ticks=math.ceil(recipe.energy*60*n/(1+a.character_crafting_speed_modifier))}
   end
  end
 end end
end
"""

CREDIT_LUA = (
    r"""
local x=helpers.json_to_table(PAYLOAD)
"""
    + DETACHED_ACTOR_LUA
    + r"""
local function refuse(reason)return {ok=false,status="blocked",reason=reason,tick=game.tick}end
local r=d and d.detached_actor
if not d or d.world_id~=x.world_id then return refuse("trigger_credit_world_changed")end
if type(r)~="table"or r.mode~="none"or not detached_actor()then return refuse("trigger_credit_requires_client_free_actor")end
local t=f.technologies[x.technology]
if not t then return refuse("trigger_credit_unknown_technology")end
if t.researched then return refuse("trigger_credit_already_researched")end
local tr=t.prototype.research_trigger
if not tr or tr.type~="craft-item"then return refuse("trigger_credit_not_a_craft_item_trigger")end
local it=tr.item;local item=type(it)=="table"and it.name or it
if item~=x.item then return refuse("trigger_credit_item_mismatch")end
for _,q in pairs(t.prerequisites)do if not q.researched then return refuse("trigger_credit_prerequisites_pending")end end
local c=nil
for i=#(d.trigger_crafts or {}),1,-1 do local row=d.trigger_crafts[i]
 if row.technology==x.technology and row.item==x.item then c=row;break end end
if not c or c.count<(tr.count or 1)or not c.consumed_any then return refuse("trigger_credit_craft_receipt_missing")end
if game.tick<c.tick+c.min_craft_ticks then return refuse("trigger_credit_craft_time_not_elapsed")end
for _,q in pairs(a.crafting_queue or {})do if q.recipe==c.recipe then return refuse("trigger_credit_craft_still_queued")end end
if a.get_item_count(x.item)<c.item_count_before+1 then return refuse("trigger_credit_crafted_item_not_in_inventory")end
t.researched=true
if not t.researched then return refuse("trigger_credit_engine_refused")end
d.trigger_credits=d.trigger_credits or {}
local credit={kind="script-credited-hand-craft-trigger-v1",test_mode="client_free",technology=x.technology,item=x.item,
 tick=game.tick,craft=c,unlocked={}}
for _,e in pairs(t.prototype.effects or {})do if e.type=="unlock-recipe"then
 credit.unlocked[#credit.unlocked+1]={recipe=e.recipe,enabled=f.recipes[e.recipe]and f.recipes[e.recipe].enabled or false}end end
d.trigger_credits[x.technology]=credit
return {ok=true,status="succeeded",reason="hand_craft_trigger_credited_test_mode",credit=credit,tick=game.tick}
"""
)

PRESENCE_LUA = (
    r"""
local x=helpers.json_to_table(PAYLOAD)
"""
    + DETACHED_ACTOR_LUA
    + r"""
local function refuse(reason)return {ok=false,status="blocked",reason=reason,tick=game.tick}end
local r=d and d.detached_actor
if not d or d.world_id~=x.world_id then return refuse("presence_world_changed")end
if type(r)~="table"or r.mode~="none"or not detached_actor()then return refuse("presence_requires_client_free_actor")end
local p=d.presence_emulation
local first=false
local function mark(x0,y0,x1,y1)
 for i=x0,x1 do for j=y0,y1 do local k=i..","..j
  if not p.charted[k]then p.charted[k]=true;p.charted_count=p.charted_count+1 end end end
end
if not p then
 first=true
 local range=storage.chart_distance or x.start_chart_tiles
 local o=f.get_spawn_position(s)
 f.chart(s,{{o.x-range,o.y-range},{o.x+range,o.y+range}})
 s.daytime=x.join_daytime
 p={kind="player-presence-emulation-v1",test_mode="client_free",start_tick=game.tick,start_chart_tiles=range,
  spawn=pos(o),join_daytime=x.join_daytime,generate_radius_chunks=x.generate_radius_chunks,
  move_chart_radius_chunks=x.move_chart_radius_chunks,updates=0,charted={},charted_count=0}
 d.presence_emulation=p
 mark(math.floor((o.x-range)/32),math.floor((o.y-range)/32),math.floor((o.x+range)/32),math.floor((o.y+range)/32))
end
d.actor_last_known_position=pos(a.position)
local cx=math.floor(a.position.x/32);local cy=math.floor(a.position.y/32);local m=x.move_chart_radius_chunks
local g=math.min(x.generate_radius_chunks,math.floor(x.generate_base_chunks+game.tick/x.generate_ticks_per_chunk))
s.request_to_generate_chunks(a.position,g);p.generate_radius_now=g
f.chart(s,{{(cx-m)*32,(cy-m)*32},{(cx+m+1)*32-1,(cy+m+1)*32-1}})
mark(cx-m,cy-m,cx+m,cy+m)
p.updates=p.updates+1;p.last_tick=game.tick;p.last_chunk={x=cx,y=cy}
local summary={}
for k,v in pairs(p)do if k~="charted"then summary[k]=v end end
return {ok=true,status="succeeded",reason=first and"presence_emulation_started"or"presence_emulation_updated",
 first=first,record=summary,tick=game.tick}
"""
)

# Factorio processes chart requests only for forces with players: in client-free
# mode force.chart stays requested forever. Every reader of "charted" uses this
# helper: the engine chart, or (client-free mode only) the chunks the presence
# emulation recorded as charted by a player at that radius.
CHUNK_CHARTED_LUA = r"""
local function chunk_charted(cx,cy)
 if f.is_chunk_charted(s,{x=cx,y=cy})then return true end
 local r=d and d.detached_actor;local p=d and d.presence_emulation
 return type(r)=="table"and r.mode=="none"and type(p)=="table"and type(p.charted)=="table"
  and p.charted[cx..","..cy]==true or false
end
"""

VERIFY_LUA = (
    DETACHED_ACTOR_LUA
    + r"""
local owner=type(d.crafting_player_index)=="number"and game.get_player(d.crafting_player_index)or nil
return {ok=true,tick=game.tick,detached=detached_actor(),actor_valid=a~=nil and a.valid or false,
 actor_unit=a and a.valid and a.unit_number or nil,actor_owned=a and a.valid and a.player~=nil or false,
 owner_connected=owner and owner.connected or false,connected_players=#game.connected_players}
"""
)


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def read(root) -> dict:
    path = Path(root) / OPTION_FILE
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(OPTION_FILE + " must be an object")
    return value


def mode(root) -> str:
    value = read(root).get("client", ALWAYS)
    if value not in MODES:
        raise ValueError("actor client mode must be one of " + ", ".join(MODES))
    return value


def set_mode(root, client: str) -> dict:
    """Written once, before the run starts; a recorded transition is never replaced.

    ``none`` also labels ``run.json`` with ``test_mode: client_free``."""
    if client not in MODES:
        raise ValueError("actor client mode must be one of " + ", ".join(MODES))
    value = read(root)
    if value.get("detached") or value.get("detach_intent") or value.get("adopted"):
        raise ValueError("actor client mode is fixed after the transition started")
    value.update(schema_version=1, client=client)
    if client == NONE:
        value.update(test_mode=TEST_MODE, script_credited_triggers=[])
    _atomic_json(Path(root) / OPTION_FILE, value)
    if client == NONE:
        _label_run(root, [])
    return value


def _label_run(root, credits, presence=None):
    path = Path(root) / "run.json"
    if not path.exists():
        return
    run = json.loads(path.read_text(encoding="utf-8"))
    run.update(test_mode=TEST_MODE, script_credited_triggers=credits)
    if presence is not None:
        run["presence_emulation"] = presence
    _atomic_json(path, run)


def test_mode(root) -> dict | None:
    """``{"test_mode": "client_free", "script_credited_triggers": [...]}`` for a client-free run, else None."""
    try:
        value = read(root)
    except (OSError, ValueError):
        return None
    if value.get("client") != NONE:
        return None
    return dict(
        test_mode=TEST_MODE,
        script_credited_triggers=list(value.get("script_credited_triggers") or []),
        presence_emulation=value.get("presence_emulation"),
    )


def _world(root):
    try:
        return json.loads((Path(root) / "world-settings.json").read_text(encoding="utf-8")).get("world_id")
    except (OSError, ValueError):
        return None


def detached(root) -> dict | None:
    """The completed transition of this world, or None."""
    try:
        record = read(root).get("detached")
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("world_id") != _world(root):
        return None
    return record


def closed_client(root) -> dict | None:
    """pid + creation time of the client that the transition closed."""
    record = detached(root)
    value = record and record.get("closed_client")
    return value if isinstance(value, dict) else None


def is_closed_client(root, client_record) -> bool:
    closed = closed_client(root)
    return bool(
        closed
        and isinstance(client_record, dict)
        and client_record.get("pid") == closed.get("pid")
        and client_record.get("creation_time") == closed.get("creation_time")
    )


def detach_due(root, observation) -> bool:
    """Cheap supervisor gate; ``detach`` checks everything again in the world."""
    if mode(root) != UNTIL_LAB_TRIGGER or detached(root):
        return False
    return LAB_TRIGGER in (observation.get("technologies") or ())


def _technology_names():
    from .progression import ROADMAP, STEP_TECHNOLOGY

    names = set()
    for step in ROADMAP:
        name = step.parameters.get("technology") or STEP_TECHNOLOGY.get(step.capability)
        if name:
            names.add(name)
    return names


def needs_hand_credit(trigger, recipes, hand_categories) -> bool:
    """Whether a research trigger can be fired only by a credited hand action.

    craft-item: when any recipe making the item is hand-craftable (a machine
    craft of a smelting product is credited without a player, HEADLESS.md).
    mine-entity: drills and pumpjacks mine those resources. Any other trigger
    type is treated as needing a player (it keeps the client)."""
    kind = (trigger or {}).get("type")
    if kind == "mine-entity":
        return False
    if kind != "craft-item":
        return True
    item = ((trigger.get("item") or {}).get("name")) if isinstance(trigger.get("item"), dict) else trigger.get("item")
    makers = [
        r
        for r in recipes.values()
        if any(p.get("name") == item and p.get("type", "item") == "item" for p in r.get("products") or [])
    ]
    if not makers:
        return True
    return any(set(r.get("categories") or [r.get("category")]) & hand_categories for r in makers)


def hand_trigger_technologies(catalog, roots=None) -> list[str]:
    """Technologies on the roadmap (and their prerequisites) whose trigger needs a credited hand action."""
    technologies = getattr(catalog, "technologies", None) or {}
    recipes = getattr(catalog, "recipes", None) or {}
    character = (getattr(catalog, "entities", None) or {}).get("character") or {}
    hand = set(character.get("crafting_categories") or ["crafting"])
    todo, seen = list(roots if roots is not None else _technology_names()), set()
    while todo:
        name = todo.pop()
        if name in seen or name not in technologies:
            continue
        seen.add(name)
        todo.extend(technologies[name].get("prerequisites") or [])
    return sorted(
        name
        for name in seen
        if technologies[name].get("research_trigger")
        and needs_hand_credit(technologies[name]["research_trigger"], recipes, hand)
    )


def execute_detach(game, action) -> dict:
    """Body of the ``detach_actor`` action (``WorldBoundGame.act``); the caller holds the writer lock."""
    if (
        set(action) != {"type", "world_id", "lab_trigger", "hand_triggers"}
        or not isinstance(action["world_id"], str)
        or action["lab_trigger"] != LAB_TRIGGER
        or not isinstance(action["hand_triggers"], list)
        or not all(isinstance(n, str) for n in action["hand_triggers"])
    ):
        raise ValueError("detach_actor needs world_id, the lab trigger and the hand trigger list")
    return game.query(DETACH_LUA.replace("PAYLOAD", _payload(action)))


def adopt(root, game) -> dict:
    """client-free: record the player-less server-created actor (``adopt_playerless_actor``). Re-entrant."""
    root = Path(root).resolve()
    if mode(root) != NONE:
        return dict(ok=False, status="blocked", reason="actor_client_mode_is_not_none")
    world = getattr(game, "expected_world", None) or _world(root)
    receipt = game.act(dict(type="adopt_playerless_actor", world_id=world))
    if receipt.get("status") == "succeeded":
        value = read(root)
        if not isinstance(value.get("adopted"), dict) or value["adopted"].get("world_id") != world:
            value["adopted"] = dict(receipt["record"], adopted_at=time.time())
            _atomic_json(root / OPTION_FILE, value)
    return receipt


def adopted(root) -> dict | None:
    try:
        record = read(root).get("adopted")
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("world_id") == _world(root) else None


def execute_adopt(game, action) -> dict:
    if set(action) != {"type", "world_id"} or not isinstance(action["world_id"], str):
        raise ValueError("adopt_playerless_actor needs exactly world_id")
    return game.query(ADOPT_LUA.replace("PAYLOAD", _payload(action)))


def credit_refusal(root, catalog, action) -> str | None:
    """Python half of the refusal conditions of ``credit_hand_craft_trigger`` (the Lua half checks the world)."""
    if set(action) - {"type", "technology", "item", "reason"} or not all(
        isinstance(action.get(k), str) and action.get(k) for k in ("technology", "item")
    ):
        return "trigger_credit_action_shape"
    if mode(root) != NONE:
        return "trigger_credit_requires_client_free_mode"
    technology = (getattr(catalog, "technologies", None) or {}).get(action["technology"]) or {}
    trigger = technology.get("research_trigger") or {}
    item = trigger.get("item")
    item = item.get("name") if isinstance(item, dict) else item
    if trigger.get("type") != "craft-item" or item != action["item"]:
        return "trigger_credit_not_this_craft_item_trigger"
    if action["technology"] not in hand_trigger_technologies(catalog):
        return "trigger_credit_trigger_needs_no_hand_craft"
    return None


def execute_credit(game, action) -> dict:
    """Body of ``credit_hand_craft_trigger`` (``WorldBoundGame.act``): the single test-mode exception."""
    from . import runtime

    root = Path(game.cfg.runtime_dir).resolve()
    refusal = credit_refusal(root, runtime.catalog_for(root, game), action)
    if refusal:
        return dict(ok=False, status="blocked", reason=refusal)
    world = getattr(game, "expected_world", None) or _world(root)
    payload = dict(world_id=world, technology=action["technology"], item=action["item"])
    receipt = game.query(CREDIT_LUA.replace("PAYLOAD", _payload(payload)))
    if receipt.get("status") == "succeeded":
        credit = receipt.get("credit") or {}
        row = dict(
            technology=action["technology"],
            item=action["item"],
            tick=credit.get("tick"),
            craft_tick=(credit.get("craft") or {}).get("tick"),
        )
        value = read(root)
        rows = [r for r in value.get("script_credited_triggers") or [] if r.get("technology") != row["technology"]]
        rows.append(row)
        value["script_credited_triggers"] = rows
        _atomic_json(root / OPTION_FILE, value)
        _label_run(root, rows, presence=value.get("presence_emulation"))
    return receipt


def presence_action(world):
    return dict(
        type=PRESENCE_ACTION,
        world_id=world,
        start_chart_tiles=START_CHART_TILES,
        join_daytime=JOIN_DAYTIME,
        generate_radius_chunks=GENERATE_RADIUS_CHUNKS,
        generate_base_chunks=GENERATE_BASE_CHUNKS,
        generate_ticks_per_chunk=GENERATE_TICKS_PER_CHUNK,
        move_chart_radius_chunks=MOVE_CHART_RADIUS_CHUNKS,
    )


def execute_presence(game, action) -> dict:
    """Body of ``emulate_player_presence`` (``WorldBoundGame.act``): exception (b) of the test mode.

    Refuses outside client-free mode; charts/generates only what a connected
    player would (fixed radii above); never grants items or research."""
    root = Path(game.cfg.runtime_dir).resolve()
    world = getattr(game, "expected_world", None) or _world(root)
    if action != presence_action(action.get("world_id")) or action.get("world_id") != world:
        return dict(ok=False, status="blocked", reason="presence_action_shape")
    if mode(root) != NONE:
        return dict(ok=False, status="blocked", reason="presence_requires_client_free_mode")
    return game.query(PRESENCE_LUA.replace("PAYLOAD", _payload(action)))


def presence(root, game) -> dict:
    """Client-free: keep the map charted/generated around the actor as a connected player would."""
    root = Path(root).resolve()
    world = getattr(game, "expected_world", None) or _world(root)
    receipt = game.act(presence_action(world))
    if receipt.get("status") == "succeeded" and receipt.get("first"):
        record = receipt.get("record") or {}
        block = dict(
            on=True,
            start_tick=record.get("start_tick"),
            start_chart_tiles=record.get("start_chart_tiles"),
            join_daytime=JOIN_DAYTIME,
            generate_radius_chunks=f"min({GENERATE_RADIUS_CHUNKS}, {GENERATE_BASE_CHUNKS} + tick/{GENERATE_TICKS_PER_CHUNK})",
            move_chart_radius_chunks=MOVE_CHART_RADIUS_CHUNKS,
            charting="emulated: force.chart is not processed without players; readers use chunk_charted",
        )
        value = read(root)
        value["presence_emulation"] = block
        _atomic_json(root / OPTION_FILE, value)
        _label_run(root, value.get("script_credited_triggers") or [], presence=block)
    return receipt


def detach(root, game, catalog, *, stop_client=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Detach the actor, close the client, verify, record. Re-entrant: every step is checked first.

    A lost receipt is reconciled by the next call: the world record
    (``d.detached_actor``) says whether the controller change happened."""
    root = Path(root).resolve()
    done = detached(root)
    if done:
        return dict(ok=True, status="succeeded", reason="actor_already_detached", record=done)
    if mode(root) != UNTIL_LAB_TRIGGER:
        return dict(ok=False, status="blocked", reason="actor_client_mode_keeps_client")
    world = getattr(game, "expected_world", None) or _world(root)
    triggers = hand_trigger_technologies(catalog)
    value = read(root)
    value["detach_intent"] = dict(world_id=world, requested_at=time.time(), hand_triggers=triggers)
    _atomic_json(root / OPTION_FILE, value)
    action = dict(type="detach_actor", world_id=world, lab_trigger=LAB_TRIGGER, hand_triggers=triggers)
    receipt = game.act(action)
    if receipt.get("status") != "succeeded":
        return receipt
    if stop_client is None:
        from .core.deterministic_character import stop_crafting_client as stop_client
    registry = root / "agent-client" / "client-process.json"
    client = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
    stopped = stop_client(game.cfg)
    if stopped.get("status") != "stopped":
        return dict(ok=False, status="blocked", reason="actor_detach_client_close_failed", client_stop=stopped)
    deadline = clock() + DISCONNECT_WAIT_SECONDS
    while True:
        check = game.query(VERIFY_LUA)
        if not check.get("ok") or not check.get("detached") or check.get("actor_owned"):
            return dict(ok=False, status="blocked", reason="actor_detach_actor_lost_after_disconnect", verify=check)
        if not check.get("owner_connected") or clock() >= deadline:
            break
        sleep(1)
    if check.get("owner_connected"):
        return dict(ok=True, status="waiting", reason="actor_detach_waiting_for_disconnect", verify=check)
    record = dict(
        receipt["record"],
        closed_client={k: client.get(k) for k in ("pid", "creation_time")},
        client_stop=stopped,
        verified=check,
        detach_receipt_tick=receipt.get("tick"),
        hand_triggers=triggers,
        detached_at=time.time(),
    )
    value = read(root)
    value.pop("detach_intent", None)
    value["detached"] = record
    _atomic_json(root / OPTION_FILE, value)
    return dict(ok=True, status="succeeded", reason="actor_detached", record=record)


def record_dead(root, binding_value) -> dict:
    """Write the terminal ``actor_dead_without_player`` state once."""
    value = read(root)
    if not isinstance(value.get("actor_dead"), dict):
        value["actor_dead"] = dict(
            reason=DEAD_WITHOUT_PLAYER,
            world_id=binding_value.get("world_id"),
            tick=binding_value.get("tick"),
            unit_number=(binding_value.get("detached_record") or {}).get("unit_number"),
            recorded_at=time.time(),
            rule="no player can respawn the detached actor; the corpse keeps the inventory; the run stops",
        )
        _atomic_json(Path(root) / OPTION_FILE, value)
    return value["actor_dead"]


def client_free(root) -> bool:
    """No client is expected now: client-free mode, or after the recorded detach."""
    try:
        return mode(root) == NONE or detached(root) is not None
    except (OSError, ValueError):
        return False


def client_state(root, client_alive: bool) -> str:
    """'live', 'none' (client-free mode), 'detached' (closed by the transition) or 'down'."""
    if client_alive:
        return "live"
    try:
        if mode(root) == NONE:
            return "none"
    except (OSError, ValueError):
        return "down"
    return "detached" if detached(root) else "down"
