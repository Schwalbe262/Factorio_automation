"""A fake game that answers the survey Lua from a :class:`WorldMap` via lupa.

It mimics just the read-only Factorio API the survey uses: ``s`` with
``find_tiles_filtered{area, name}`` and ``find_entities_filtered{area, type,
force}``, ``prototypes.tile`` / ``prototypes.entity``, the player force ``f``
and actor ``a``. The real survey body runs unchanged (joined onto one line, as
the RCON client does), so ``survey_area(FakeSurveyGame(world), area)`` should
rebuild ``world`` exactly (plans and zones excluded: the game has no such
thing). Requires the optional ``lupa`` package.
"""

from __future__ import annotations

from .worldmap import UNBUILDABLE, WATER, WorldMap

_MOCK_LUA = r"""
f={name="player"}
a={name="character",type="character",force=f}
local tiles={}
local entities={a}
prototypes={entity={},tile={}}
prototypes.entity["assembling-machine-1"]={collision_mask={layers={item=true,object=true,player=true,water_tile=true}}}
prototypes.entity["character"]={collision_mask={layers={player=true}}}
prototypes.tile["grass-1"]={collision_mask={layers={ground_tile=true}}}
prototypes.tile["water"]={collision_mask={layers={water_tile=true,item=true,player=true}}}
prototypes.tile["out-of-map"]={collision_mask={layers={ground_tile=true,water_tile=true,item=true,object=true,player=true}}}
a.prototype=prototypes.entity["character"]
a.bounding_box={left_top={x=0.1,y=0.1},right_bottom={x=0.9,y=0.9}}
function add_tile(name,x,y) tiles[#tiles+1]={name=name,position={x=x,y=y}} end
function add_entity(name,kind,neutral,x,y,lx,ly,rx,ry,amount,minable)
 local proto=prototypes.entity[name]
 if not proto then
  proto={collision_box={left_top={x=lx-x,y=ly-y},right_bottom={x=rx-x,y=ry-y}},
   collision_mask={layers={object=true}},mineable_properties={minable=minable}}
  prototypes.entity[name]=proto
 end
 local force=f
 if neutral then force={name="neutral"} end
 entities[#entities+1]={name=name,type=kind,force=force,position={x=x,y=y},amount=amount,prototype=proto,
  bounding_box={left_top={x=lx,y=ly},right_bottom={x=rx,y=ry}}}
end
local function within(p,A) return p.x>=A.left_top.x and p.x<A.right_bottom.x and p.y>=A.left_top.y and p.y<A.right_bottom.y end
local function touches(b,A)
 return b.left_top.x<A.right_bottom.x and A.left_top.x<b.right_bottom.x and b.left_top.y<A.right_bottom.y and A.left_top.y<b.right_bottom.y
end
s={}
function s.find_tiles_filtered(q)
 local wanted={}
 for _,n in pairs(q.name) do wanted[n]=true end
 local out={}
 for _,t in ipairs(tiles) do if wanted[t.name] and within(t.position,q.area) then out[#out+1]=t end end
 return out
end
function s.find_entities_filtered(q)
 local out={}
 for _,e in ipairs(entities) do
  if touches(e.bounding_box,q.area) and (q.type==nil or q.type==e.type) and (q.force==nil or q.force==e.force) then out[#out+1]=e end
 end
 return out
end
"""

_KIND_TYPES = {
    "tree": "tree",
    "rock": "simple-entity",
    "fixed": "simple-entity",
    "cliff": "cliff",
    "entity": "container",
}
_TILE_NAMES = {WATER: "water", UNBUILDABLE: "out-of-map"}


def _to_python(lupa_module, value):
    """Convert a Lua result the way helpers.table_to_json would."""
    if lupa_module.lua_type(value) != "table":
        return value
    keys = list(value.keys())
    if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [_to_python(lupa_module, value[k]) for k in sorted(keys)]
    return {str(k): _to_python(lupa_module, value[k]) for k in keys}


class FakeSurveyGame:
    """Answers ``query(body)`` by running ``body`` in lupa against ``world``."""

    def __init__(self, world: WorldMap):
        import lupa

        self._lupa = lupa
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(_MOCK_LUA)
        self.queries = 0
        self._load(world)

    def _load(self, world: WorldMap) -> None:
        add_tile = self.lua.globals().add_tile
        add_entity = self.lua.globals().add_entity
        for (x, y), terrain in world.terrain_tiles().items():
            add_tile(_TILE_NAMES[terrain], x, y)
        for (x, y), (name, amount) in world.ore_tiles().items():
            add_entity(name, "resource", True, x + 0.5, y + 0.5, x + 0.4, y + 0.4, x + 0.6, y + 0.6, amount, True)
        for well in world.wells():
            cx, cy = well.centre[0] + 0.5, well.centre[1] + 0.5
            half = well.size / 2 - 0.1
            add_entity(
                well.name, "resource", True, cx, cy, cx - half, cy - half, cx + half, cy + half, well.amount, True
            )
        for feature in world.features():
            if feature.kind == "plan":
                continue
            r = feature.rect
            cx, cy = r.centre()
            neutral = feature.kind != "entity"
            minable = feature.kind != "fixed"
            box = (r.x0 + 0.1, r.y0 + 0.1, r.x1 - 0.1, r.y1 - 0.1)
            add_entity(feature.name, _KIND_TYPES[feature.kind], neutral, cx, cy, *box, None, minable)

    def query(self, body: str):
        """Run a query body the way DeterministicGame does (one joined line)."""
        self.queries += 1
        line = " ".join(body.splitlines())
        result = self.lua.execute("return (function() " + line + " end)()")
        return _to_python(self._lupa, result)
