"""Actor death: the client-free respawn emulation, the paid recovery of the corpse's items, death records.

A connected player whose character dies is respawned by the engine: after the
respawn delay it gets a new, empty character at the force spawn; the dead
character's items stay in a ``character-corpse`` where it died. The framework
then rebinds that successor (``recover-actor``) and recovers the corpse stacks
(``recover-belongings``, ``corpse_recovery``).

A client-free TEST run (``actor-client.json`` ``client: none``) has no player,
so nothing respawns. The respawn emulation is OPTIONAL and OFF by default
(``actor-client.json`` ``respawn: true``, ``fleet.py start --respawn``): it needs
the owner's approval as part of the written test-mode exception, not yet given.
Off, a death is recorded (``record_unrecovered``) and the run stops
(``errored: agent_dead``). On, ``respawn_client_free_actor`` reproduces exactly what the
engine gives a player and nothing more (part of the presence emulation,
docs/v2/HEADLESS.md): it waits ``RESPAWN_DELAY_TICKS`` after the death, creates
a new ``character`` at the force spawn with an empty inventory and binds it as
the adopted actor. The corpse keeps every item. Freeplay's respawn items
(pistol and ten magazines, ``on_player_respawned``) are items and are not
reproduced, like its crash site and starting items.

``recover_client_free_belongings`` is the paid way back to those items: the
actor goes to the corpse (an assisted move is a teleport, as every assisted
move; the character backend must already be in reach), takes the stacks into
its main inventory with counts conserved, and returns. It refuses while
enemies stand within ``BELONGINGS_THREAT_RADIUS`` of the corpse: the run then
continues from base stock and tries again later.

Every death is recorded in the world (``d.client_free_deaths``) and in
``actor-client.json`` ``deaths`` (``record_respawn`` / ``record_belongings``),
read by ``fleet.py report`` and ``waste_report.py`` (``deaths``).
"""

from __future__ import annotations

import json
from pathlib import Path
import time

from .actor_client import DETACHED_ACTOR_LUA, NONE, OPTION_FILE, mode, read, respawn_enabled
from .core.deterministic_state import _atomic_json

RESPAWN_ACTION = "respawn_client_free_actor"
BELONGINGS_ACTION = "recover_client_free_belongings"
# Factorio's respawn time of a dead player (LuaPlayer.ticks_to_respawn): 10 s.
RESPAWN_DELAY_TICKS = 600
# Corpse search: around the last position the presence emulation recorded, else the spawn.
LAST_POSITION_RADIUS = 64
SPAWN_RADIUS = 256
# Enemies this close to the corpse make the trip unsafe (a biter group's aggro reach).
BELONGINGS_THREAT_RADIUS = 32
DEATHS_KEPT = 64
AGENT_DEAD = "agent_dead"

RESPAWN_LUA = r"""
local x=helpers.json_to_table(PAYLOAD)
local function refuse(reason)return {ok=false,status="blocked",reason=reason,tick=game.tick}end
if not d or d.world_id~=x.world_id then return refuse("respawn_world_changed")end
local r=d.detached_actor
if type(r)~="table"or r.kind~="detached-actor-v1"or r.mode~="none"or r.world_id~=d.world_id then
 return refuse("respawn_requires_client_free_actor")end
if d.crafting_actor_unit_number~=r.unit_number then return refuse("respawn_actor_record_mismatch")end
if a and a.valid then
 if a.unit_number==r.unit_number and d.actor==a and a.player==nil then
  return {ok=true,status="succeeded",reason="actor_alive",alive=true,unit_number=a.unit_number,tick=game.tick}end
 return refuse("respawn_actor_record_mismatch")
end
local old=r.unit_number
for _,e in pairs(s.find_entities_filtered{type="character",limit=65})do
 if e.valid and e.unit_number==old then return refuse("respawn_old_actor_still_live")end end
if d.client_free_deaths==nil then d.client_free_deaths={}end
local deaths=d.client_free_deaths
local death=d.client_free_death
if type(death)~="table"or death.old_unit~=old then
 local last=d.actor_last_known_position
 local center=f.get_spawn_position(s);local radius=x.spawn_radius
 if type(last)=="table"and type(last.x)=="number"and type(last.y)=="number"then center=last;radius=x.last_position_radius end
 local known={}
 for _,row in pairs(deaths)do if row.corpse then known[row.tick..":"..row.corpse.x..":"..row.corpse.y]=true end end
 local best=nil
 for _,e in pairs(s.find_entities_filtered{position=center,radius=radius,type="character-corpse",limit=33})do
  local t=e.valid and e.character_corpse_tick_of_death
  if t and t<=game.tick and not known[t..":"..e.position.x..":"..e.position.y]
   and(not best or t>best.character_corpse_tick_of_death)then best=e end
 end
 local items=0;local stacks=0;local enemies={};local enemy_count=0
 if best then
  local inv=best.get_inventory(defines.inventory.character_corpse)
  if inv then for i=1,#inv do local st=inv[i];if st.valid_for_read then items=items+st.count;stacks=stacks+1 end end end
  for _,e in pairs(s.find_entities_filtered{position=best.position,radius=x.threat_radius,force="enemy",
   type={"unit","unit-spawner","turret"},limit=64})do
   if e.valid then enemies[e.name]=(enemies[e.name]or 0)+1;enemy_count=enemy_count+1 end end
 end
 death={kind="client-free-actor-death-v1",old_unit=old,tick=best and best.character_corpse_tick_of_death or game.tick,
  tick_source=best and"corpse"or"detected",detected_tick=game.tick,corpse=best and pos(best.position)or nil,
  corpse_items=items,corpse_stacks=stacks,enemies_near_corpse=enemies,enemy_count_near_corpse=enemy_count,
  last_known_position=last}
 d.client_free_death=death
end
local ready=death.tick+x.respawn_delay_ticks
if game.tick<ready then return {ok=true,status="waiting",reason="respawn_delay",ready_tick=ready,death=death,tick=game.tick}end
local p=s.find_non_colliding_position("character",f.get_spawn_position(s),32,0.5)
if not p then return refuse("respawn_no_free_spawn_position")end
local e=s.create_entity{name="character",position=p,force=f}
if not e or not e.valid then return refuse("respawn_character_creation_failed")end
local main=e.get_main_inventory()
d.actor=e;d.crafting_actor_unit_number=e.unit_number;r.unit_number=e.unit_number;r.respawns=(r.respawns or 0)+1
death.respawn_tick=game.tick;death.new_unit=e.unit_number;death.respawn_position=pos(e.position)
while #deaths>=x.deaths_kept do table.remove(deaths,1)end
deaths[#deaths+1]=death;d.client_free_death=nil
d.actor_last_known_position=pos(e.position)
return {ok=true,status="succeeded",reason="client_free_actor_respawned",death=death,unit_number=e.unit_number,
 inventory_empty=main==nil or main.is_empty(),tick=game.tick,
 scope="emulated engine respawn: new empty character at the force spawn after the respawn delay; corpse items untouched"}
"""

BELONGINGS_LUA = (
    DETACHED_ACTOR_LUA
    + r"""
local x=helpers.json_to_table(PAYLOAD)
local function refuse(reason)return {ok=false,status="blocked",reason=reason,tick=game.tick}end
if not d or d.world_id~=x.world_id then return refuse("belongings_world_changed")end
local r=d.detached_actor
if type(r)~="table"or r.mode~="none"or not detached_actor()then return refuse("belongings_requires_client_free_actor")end
if a.unit_number~=x.actor_unit then return refuse("belongings_actor_unit_changed")end
local death=nil
for _,row in pairs(d.client_free_deaths or {})do if row.tick==x.death_tick then death=row end end
if not death then return refuse("belongings_death_record_missing")end
local done=death.belongings
if type(done)=="table"and(done.status=="recovered"or done.status=="lost")then
 return {ok=true,status="succeeded",reason="belongings_already_"..done.status,already=true,belongings=done,tick=game.tick}end
if type(done)=="table"and done.status=="blocked"then return refuse("belongings_prior_conservation_failure")end
local recovered=type(done)=="table"and done.recovered_items or 0
local function lost(reason)
 death.belongings={status="lost",reason=reason,recovered_items=recovered,lost_items=(death.corpse_items or 0)-recovered,tick=game.tick}
 return {ok=true,status="succeeded",reason="belongings_lost_"..reason,belongings=death.belongings,tick=game.tick}
end
if not death.corpse then return lost("no_corpse")end
local corpse=nil
for _,e in pairs(s.find_entities_filtered{position=death.corpse,radius=0.5,type="character-corpse",limit=8})do
 if e.valid and e.character_corpse_tick_of_death==death.tick then corpse=e end end
if not corpse then return lost("corpse_gone")end
local enemies=s.count_entities_filtered{position=corpse.position,radius=x.threat_radius,force="enemy",
 type={"unit","unit-spawner","turret"}}
if enemies>0 then return {ok=true,status="waiting",reason="belongings_unsafe_enemies_near_corpse",enemies=enemies,
 corpse=pos(corpse.position),tick=game.tick}end
if a.crafting_queue_size>0 then return {ok=true,status="waiting",reason="belongings_actor_crafting",tick=game.tick}end
local inv=corpse.get_inventory(defines.inventory.character_corpse)
local main=a.get_main_inventory()
if not inv or not main then return refuse("belongings_inventory_missing")end
local function total(i)
 local t={};local c=0
 for k=1,#i do local st=i[k];if st.valid_for_read then local key=st.name.."@"..st.quality.name
  t[key]=(t[key]or 0)+st.count;c=c+st.count end end
 return t,c
end
local corpse_before,cb=total(inv);local actor_before,ab=total(main)
local home=pos(a.position)
if x.backend=="assisted"then
 local p=s.find_non_colliding_position("character",corpse.position,8,0.5)
 if not p then return refuse("belongings_corpse_position_blocked")end
 if not a.teleport(p)then return refuse("belongings_move_failed")end
elseif not a.can_reach_entity(corpse)then
 return {ok=true,status="waiting",reason="belongings_corpse_out_of_reach",corpse=pos(corpse.position),tick=game.tick}
end
local moved=0
for k=1,#inv do local st=inv[k]
 if st.valid_for_read then
  local count=st.count;local n=main.insert(st)
  if n>0 then if n>=count then st.clear()else st.count=count-n end;moved=moved+n end
 end
end
if x.backend=="assisted"then
 local q=s.find_non_colliding_position("character",home,8,0.5);if q then a.teleport(q)end
end
local corpse_after,ca=total(inv);local actor_after,aa=total(main)
local conserved=cb+ab==ca+aa
for key,v in pairs(corpse_before)do if v+(actor_before[key]or 0)~=(corpse_after[key]or 0)+(actor_after[key]or 0)then conserved=false end end
for key,v in pairs(corpse_after)do if v+(actor_after[key]or 0)~=(corpse_before[key]or 0)+(actor_before[key]or 0)then conserved=false end end
if not conserved then
 death.belongings={status="blocked",reason="not_conserved",recovered_items=recovered,tick=game.tick}
 return refuse("belongings_not_conserved")
end
recovered=recovered+moved
death.belongings={status=ca==0 and"recovered"or"partial",recovered_items=recovered,remaining_items=ca,
 lost_items=0,tick=game.tick}
return {ok=true,status=ca==0 and"succeeded"or"waiting",reason=ca==0 and"belongings_recovered"or"belongings_main_inventory_full",
 moved=moved,remaining_items=ca,belongings=death.belongings,actor_position=pos(a.position),corpse=pos(corpse.position),
 tick=game.tick,scope="paid stack transfer from the actor's own corpse; counts conserved; enemies near the corpse defer it"}
"""
)


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def _world(root):
    try:
        return json.loads((Path(root) / "world-settings.json").read_text(encoding="utf-8")).get("world_id")
    except (OSError, ValueError):
        return None


def respawn_action(world):
    return dict(
        type=RESPAWN_ACTION,
        world_id=world,
        respawn_delay_ticks=RESPAWN_DELAY_TICKS,
        last_position_radius=LAST_POSITION_RADIUS,
        spawn_radius=SPAWN_RADIUS,
        threat_radius=BELONGINGS_THREAT_RADIUS,
        deaths_kept=DEATHS_KEPT,
    )


def execute_respawn(game, action) -> dict:
    """Body of ``respawn_client_free_actor`` (``WorldBoundGame.act``): client-free mode only, fixed constants."""
    root = Path(game.cfg.runtime_dir).resolve()
    world = getattr(game, "expected_world", None) or _world(root)
    if action != respawn_action(action.get("world_id")) or action.get("world_id") != world:
        return dict(ok=False, status="blocked", reason="respawn_action_shape")
    if mode(root) != NONE:
        return dict(ok=False, status="blocked", reason="respawn_requires_client_free_mode")
    if not respawn_enabled(root):
        return dict(ok=False, status="blocked", reason="respawn_emulation_off")
    return game.query(RESPAWN_LUA.replace("PAYLOAD", _payload(action)))


def belongings_action(world, actor_unit, death_tick, backend):
    return dict(
        type=BELONGINGS_ACTION,
        world_id=world,
        actor_unit=actor_unit,
        death_tick=death_tick,
        threat_radius=BELONGINGS_THREAT_RADIUS,
        backend=backend,
    )


def execute_belongings(game, action) -> dict:
    """Body of ``recover_client_free_belongings``: the paid trip to the actor's own corpse."""
    root = Path(game.cfg.runtime_dir).resolve()
    world = getattr(game, "expected_world", None) or _world(root)
    actor, tick = action.get("actor_unit"), action.get("death_tick")
    if (
        any(type(v) is not int or v < 0 for v in (actor, tick))
        or action != belongings_action(action.get("world_id"), actor, tick, getattr(game, "backend", "assisted"))
        or action.get("world_id") != world
    ):
        return dict(ok=False, status="blocked", reason="belongings_action_shape")
    if mode(root) != NONE:
        return dict(ok=False, status="blocked", reason="belongings_requires_client_free_mode")
    return game.query(BELONGINGS_LUA.replace("PAYLOAD", _payload(action)))


def respawn(root, game) -> dict:
    """``respawn-actor``: one guarded step of the client-free respawn (waiting during the respawn delay)."""
    world = getattr(game, "expected_world", None) or _world(root)
    return game.act(respawn_action(world))


def recover(root, game, actor_unit, death_tick) -> dict:
    """``recover-belongings`` in client-free mode."""
    world = getattr(game, "expected_world", None) or _world(root)
    return game.act(belongings_action(world, actor_unit, death_tick, getattr(game, "backend", "assisted")))


# ---------------------------------------------------------------- records


def _deaths(value):
    rows = value.get("deaths")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def record_respawn(root, receipt, *, detected_at=None, mode_name=NONE) -> dict:
    """Append (once per death tick) the respawn receipt's death to ``actor-client.json`` ``deaths``."""
    death = receipt.get("death") or {}
    value = read(root)
    rows = _deaths(value)
    row = next((r for r in rows if r.get("death_tick") == death.get("tick")), None)
    if row is None:
        row = dict(death_tick=death.get("tick"))
        rows.append(row)
    row.update(
        mode=mode_name,
        tick_source=death.get("tick_source"),
        detected_tick=death.get("detected_tick"),
        detected_at=detected_at,
        respawn_tick=death.get("respawn_tick") or receipt.get("tick"),
        old_unit=death.get("old_unit"),
        new_unit=death.get("new_unit") or receipt.get("unit_number"),
        corpse=death.get("corpse"),
        corpse_items=death.get("corpse_items"),
        enemies_near_corpse=death.get("enemies_near_corpse") or {},
        cause=cause_of(death),
        recorded_at=time.time(),
    )
    row.setdefault("belongings", dict(status="pending"))
    value["deaths"] = rows[-DEATHS_KEPT:]
    _atomic_json(Path(root) / OPTION_FILE, value)
    return row


def record_belongings(root, death_tick, receipt) -> dict | None:
    """The belongings outcome of one recorded death (``recovered``, ``lost``, ``deferred`` or ``blocked``)."""
    value = read(root)
    rows = _deaths(value)
    row = next((r for r in rows if r.get("death_tick") == death_tick), None)
    if row is None:
        return None
    found = receipt.get("belongings") or {}
    status = found.get("status") or {"succeeded": "recovered", "waiting": "deferred"}.get(
        receipt.get("status"), "blocked"
    )
    row["belongings"] = dict(
        status=status,
        reason=receipt.get("reason"),
        recovered_items=found.get("recovered_items", receipt.get("total_transferred_items")),
        lost_items=found.get("lost_items"),
        remaining_items=found.get("remaining_items", receipt.get("remaining_items")),
        tick=receipt.get("tick"),
    )
    value["deaths"] = rows
    _atomic_json(Path(root) / OPTION_FILE, value)
    return row


def record_unrecovered(root, last_tick, reason, *, detected_at=None) -> dict:
    """A death no rule respawns (client-free with the respawn emulation off): the run ends ``errored: agent_dead``."""
    value = read(root)
    rows = _deaths(value)
    tick = last_tick if type(last_tick) is int else 0
    row = next((r for r in rows if r.get("death_tick") == tick), None)
    if row is None:
        row = dict(death_tick=tick)
        rows.append(row)
    row.update(
        mode=mode(root),
        tick_source="last_observed",
        detected_at=detected_at,
        respawn_tick=None,
        cause=f"unknown ({reason}; no corpse survey)",
        belongings=dict(status="not_attempted", reason=reason),
        recorded_at=time.time(),
    )
    value["deaths"] = rows[-DEATHS_KEPT:]
    _atomic_json(Path(root) / OPTION_FILE, value)
    return row


def record_player_death(root, death_tick, *, old_unit, new_unit, corpse=None, detected_at=None) -> dict:
    """A connected player's death and its engine respawn (rebound by ``recover-actor``)."""
    value = read(root)
    rows = _deaths(value)
    row = next((r for r in rows if r.get("death_tick") == death_tick), None)
    if row is None:
        row = dict(death_tick=death_tick, belongings=dict(status="pending"))
        rows.append(row)
    row.update(mode="player", old_unit=old_unit, new_unit=new_unit, corpse=corpse, detected_at=detected_at)
    value["deaths"] = rows[-DEATHS_KEPT:]
    _atomic_json(Path(root) / OPTION_FILE, value)
    return row


def cause_of(death) -> str:
    """Short cause text: the enemies seen at the corpse when the death was detected, else unknown."""
    enemies = death.get("enemies_near_corpse") if isinstance(death, dict) else None
    if isinstance(enemies, dict) and enemies:
        return "enemies at corpse: " + ", ".join(f"{n}x{k}" for k, n in sorted(enemies.items(), key=lambda kv: -kv[1]))
    if isinstance(death, dict) and death.get("corpse") is None and death.get("tick_source") == "detected":
        return "unknown (no corpse found)"
    return "unknown (no enemy near the corpse when found)"


def _events(root):
    path = Path(root) / "logs" / "controller-events.jsonl"
    try:
        with path.open(encoding="utf-8") as lines:
            for line in lines:
                if '"actor_' not in line and "agent_dead" not in line and '"slice"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def last_event_tick(root, tail_bytes=262144):
    """The newest ``tick`` in the bounded tail of ``job-events.jsonl`` (the game kept ticking after a death)."""
    path = Path(root) / "job-events.jsonl"
    try:
        with path.open("rb") as source:
            source.seek(0, 2)
            size = source.tell()
            source.seek(max(0, size - tail_bytes))
            lines = source.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    best = None
    for line in lines[1:] if size > tail_bytes else lines:
        try:
            tick = json.loads(line).get("tick")
        except (ValueError, AttributeError):
            continue
        if type(tick) is int and (best is None or tick > best):
            best = tick
    return best


def deaths(root, *, end_tick=None) -> list[dict]:
    """Every actor death of a run: ``death_tick``, ``recovered_tick``, ``lost_seconds``, ``items_lost``, ``cause``.

    Recorded deaths come from ``actor-client.json`` ``deaths``. A run without
    such records (older code, or a death the supervisor never recovered) is read
    from ``logs/controller-events.jsonl``: the last slice tick before the first
    ``agent_dead`` error is the death, unrecovered until ``end_tick``.
    """
    try:
        recorded = _deaths(read(root))
    except (OSError, ValueError):
        recorded = []
    if end_tick is None:
        end_tick = last_event_tick(root)
    rows = []
    for r in recorded:
        tick = r.get("death_tick")
        if type(tick) is not int:
            continue
        belongings = r.get("belongings") or {}
        recovered = r.get("respawn_tick")
        if belongings.get("status") in ("recovered", "lost") and type(belongings.get("tick")) is int:
            recovered = max(recovered or 0, belongings["tick"])
        lost = belongings.get("lost_items")
        if lost is None and belongings.get("status") in ("pending", "deferred", "blocked", "partial"):
            remaining = belongings.get("remaining_items")
            lost = remaining if type(remaining) is int else r.get("corpse_items")
        rows.append(
            dict(
                death_tick=tick,
                recovered_tick=recovered,
                lost_seconds=round(((recovered if type(recovered) is int else end_tick or tick) - tick) / 60, 1),
                items_lost=lost,
                belongings=belongings.get("status"),
                cause=r.get("cause") or cause_of(r),
                mode=r.get("mode"),
            )
        )
    if rows:
        return rows
    last_slice = None
    for row in _events(root):
        if row.get("event") == "slice" and type(row.get("tick")) is int:
            last_slice = row["tick"]
        elif row.get("event") in ("error", "call") and AGENT_DEAD in str(row.get("reason") or row.get("error") or ""):
            tick = last_slice or 0
            end = end_tick if type(end_tick) is int else tick
            return [
                dict(
                    death_tick=tick,
                    recovered_tick=None,
                    lost_seconds=round(max(0, end - tick) / 60, 1),
                    items_lost=None,
                    belongings=None,
                    cause="unknown (unrecovered agent_dead; no death record)",
                    mode=None,
                )
            ]
    return rows
