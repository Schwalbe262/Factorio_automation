"""Prove a one-belt continuation of an owned, declared material input."""
from __future__ import annotations

from copy import deepcopy
import json
import math

from .factory_templates import DIRECTIONS


def _point(row):
    position = row["position"]
    values = position["x"], position["y"]
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in values):
        raise ValueError("consumer entry has invalid coordinates")
    return values


def _observed(obs, expected):
    rows = [row for row in obs.get("entities", [])
            if row.get("name") == expected["name"] and row.get("position") == expected["position"]]
    if (len(rows) != 1 or type(rows[0].get("unit_number")) is not int or rows[0]["unit_number"] < 1
            or rows[0].get("direction") != expected.get("direction", 0)):
        raise ValueError("consumer entry entity is missing or its observed identity changed")
    return {"name": expected["name"], "position": deepcopy(expected["position"]),
            "direction": expected.get("direction", 0), "unit_number": rows[0]["unit_number"]}


def validate_consumer_entry(factory, obs, canonical_port, provenance):
    """Validate saved ownership and fresh identities before using an inner belt.

    The logical consumer remains its declared port. Only its immediate forward
    belt, already feeding the indexed machine through a normal inserter, may
    become the physical endpoint. This never grants ownership of an entire
    block containing several materials.
    """
    failure = {"ok": False, "reason": "invalid owned consumer entry"}
    try:
        if (not isinstance(provenance, dict) or set(provenance) != {"owner_key", "entry_port"}
                or not isinstance(provenance["owner_key"], str)
                or not isinstance(canonical_port, dict)
                or canonical_port.get("kind") != "item" or canonical_port.get("direction") != "input"
                or not isinstance(canonical_port.get("item"), str) or not canonical_port["item"]
                or type(canonical_port.get("facing")) is not int or canonical_port["facing"] not in DIRECTIONS
                or type(canonical_port.get("machine_index")) is not int or canonical_port["machine_index"] < 0
                or not obs.get("world_id") or obs["world_id"] != factory.state.get("world_id")):
            return failure
        facing = canonical_port["facing"]
        dx, dy = DIRECTIONS[facing]
        x, y = _point(canonical_port)
        entry_point, approach = (x + dx, y + dy), (x - dx, y - dy)
        expected_entry = {**canonical_port, "position": {"x": entry_point[0], "y": entry_point[1]}}
        if provenance["entry_port"] != expected_entry:
            return {**failure, "reason": "consumer entry is not the identical one-forward input port"}
        blocks = factory.state.get("blocks", {})
        owner = blocks.get(provenance["owner_key"], {})
        if canonical_port not in owner.get("ports", []):
            return {**failure, "reason": "consumer entry has no declared canonical owner"}
        for key, block in blocks.items():
            for port in block.get("ports", []):
                if key == provenance["owner_key"] and port == canonical_port:
                    continue
                if port.get("kind") != "item" or port.get("facing") not in DIRECTIONS:
                    continue
                px, py = _point(port)
                vx, vy = DIRECTIONS[port["facing"]]
                sign = -1 if port.get("direction") == "input" else 1
                if (px + sign * vx, py + sign * vy) == approach:
                    return {**failure, "reason": "consumer entry approach is reserved by another material port"}
        entities = owner.get("entities", [])
        owned_belts = []
        for position in ((x, y), entry_point):
            rows = [row for row in entities if _point(row) == position]
            if not rows or any(row.get("name") != "transport-belt" or row.get("direction", 0) != facing
                               for row in rows):
                return {**failure, "reason": "consumer entry does not match both reserved belt facings"}
            owned_belts.append(rows[0])
        machine_names = {
            "labs_row": {"lab"},
            "assembler_row": {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"},
            "furnace_row": {"stone-furnace", "steel-furnace"},
        }.get(owner.get("template"), set())
        machines = [row for row in entities if row.get("name") in machine_names]
        index = canonical_port["machine_index"]
        if index >= len(machines):
            return {**failure, "reason": "consumer entry has no unambiguous indexed template machine"}
        machine = machines[index]
        mx, my = _point(machine)
        size = 2 if machine["name"] in {"stone-furnace", "steel-furnace"} else 3
        arms = []
        for row in entities:
            if row.get("name") not in {"inserter", "fast-inserter"} or row.get("direction") not in DIRECTIONS:
                continue
            ax, ay = _point(row)
            vx, vy = DIRECTIONS[row["direction"]]
            if (ax + vx, ay + vy) == entry_point:
                if abs(ax - vx - mx) >= size / 2 or abs(ay - vy - my) >= size / 2:
                    return {**failure, "reason": "consumer entry inserter does not feed its indexed machine"}
                arms.append(row)
        if len(arms) != 1:
            return {**failure, "reason": "consumer entry requires one owned normal intake inserter"}
        from .deterministic_input_links import _geometry, _path
        belts, edges, _ = _geometry(owner)
        if _path(belts, edges, (x, y), entry_point) is None:
            return {**failure, "reason": "canonical input does not reach its consumer entry"}
        for port in owner.get("ports", []):
            if (port.get("kind") == "item" and port.get("item") != canonical_port["item"]
                    and _path(belts, edges, _point(port), entry_point) is not None):
                return {**failure, "reason": "another material port reaches the consumer entry"}
        for plan in factory.state.get("links", {}).values():
            if not any((plan.get(field) or {}).get("item") not in (None, canonical_port["item"])
                       for field in ("source_port", "consumer_port")):
                continue
            for row in plan.get("entities", []):
                if row.get("name") != "transport-belt":
                    continue
                px, py = _point(row)
                vx, vy = DIRECTIONS[row.get("direction", 0)]
                if (px, py) in {(x, y), entry_point} or (px + vx, py + vy) in {(x, y), entry_point}:
                    return {**failure, "reason": "foreign material reservation reaches the consumer entry"}
        canonical, entry = [_observed(obs, row) for row in owned_belts]
        arm, receiver = _observed(obs, arms[0]), _observed(obs, machine)
        if machine.get("recipe"):
            receiver["recipe"] = machine["recipe"]
        payload = json.dumps(json.dumps({"world": obs["world_id"], "item": canonical_port["item"],
            "canonical": canonical, "entry": entry, "arm": arm, "receiver": receiver}, separators=(",", ":")))
    except (KeyError, TypeError, ValueError, IndexError):
        return failure
    proof = factory.game.query('''
--[[ owned consumer continuation proof ]]
local x=helpers.json_to_table(''' + payload + ''')
if not d or d.world_id~=x.world then return {ok=false,reason="consumer entry world changed"} end
local function exact(row)
 local e=target(row.position,row.name)
 if not e or e.force~=f or e.unit_number~=row.unit_number or e.direction~=row.direction then return nil end
 return e
end
local first=exact(x.canonical);local entry=exact(x.entry);local arm=exact(x.arm);local receiver=exact(x.receiver)
if not first or not entry or not arm or not receiver then return {ok=false,reason="consumer entry identity changed"} end
if arm.name=="fast-inserter" then
 local basic=prototypes.entity["inserter"];local fast=arm.prototype
 local function same(a,b) return a and b and math.abs((a.x or a[1])-(b.x or b[1]))<.01
  and math.abs((a.y or a[2])-(b.y or b[2]))<.01 end
 if not basic or not same(basic.inserter_pickup_position,fast.inserter_pickup_position)
  or not same(basic.inserter_drop_position,fast.inserter_drop_position)
  then return {ok=false,reason="consumer entry fast inserter prototype geometry changed"} end
end
for _,belt in ipairs{first,entry} do for lane=1,2 do
 for _,row in pairs(belt.get_transport_line(lane).get_contents()) do
  if row.count>0 and row.name~=x.item then return {ok=false,reason="consumer entry carries another material"} end
 end
end end
local function inside(p,box)
 return p.x>box.left_top.x and p.x<box.right_bottom.x and p.y>box.left_top.y and p.y<box.right_bottom.y
end
if not inside(arm.pickup_position,entry.bounding_box) or not inside(arm.drop_position,receiver.bounding_box)
 then return {ok=false,reason="consumer entry intake geometry changed"} end
if x.receiver.recipe then local recipe=receiver.get_recipe()
 if not recipe or recipe.name~=x.receiver.recipe then return {ok=false,reason="consumer entry receiver recipe changed"} end
end
return {ok=true,consumer_entry_verified=true}
''')
    if not isinstance(proof, dict) or not proof.get("ok") or proof.get("consumer_entry_verified") is not True:
        return {**failure, "reason": proof.get("reason", "consumer entry was not verified")
                if isinstance(proof, dict) else "consumer entry was not verified"}
    return {"ok": True, "entry_port": deepcopy(expected_entry),
            "canonical_approach": {"x": approach[0], "y": approach[1]}}
