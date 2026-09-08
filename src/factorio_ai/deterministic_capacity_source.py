"""Reuse an owned mining cell when discovery outlives the starter source."""
from copy import deepcopy


def adopt_capacity_source(factory, obs: dict, item: str, cell: dict, primary: dict):
    """Maintain the cell's existing paid intake and output instead of duplicating it."""
    from .deterministic_factory import _ready, _report

    prefix = f"source:{item}:capacity:"
    receiver, drill = cell.get("receiver", {}), cell.get("drill", {})
    candidates = []
    for key, plan in factory.state.get("blocks", {}).items():
        if not key.startswith(prefix) or not key[len(prefix):].isdigit() or plan.get("resource_cell") is not True:
            continue
        def matches(expected):
            return [row for row in plan.get("entities", [])
                    if row.get("name") == expected.get("name") and row.get("position") == expected.get("position")
                    and row.get("direction", 0) == expected.get("direction", 0)]
        if len(matches(receiver)) == 1 and len(matches(drill)) == 1:
            candidates.append((key, plan))
    if not candidates:
        return None
    if (len(candidates) != 1 or not cell.get("ok") or not cell.get("complete")
            or obs.get("world_id") != factory.state.get("world_id")):
        return _report("blocked", "replacement capacity cell ownership is ambiguous", item=item)
    key, plan = candidates[0]
    outputs = [port for port in plan.get("ports", []) if port.get("kind") == "item" and port.get("direction") == "output"]
    inputs = [port for port in plan.get("ports", []) if port.get("kind") == "item" and port.get("direction") == "input"]
    if (len(outputs) != 1 or outputs[0].get("item") != item
            or any(port.get("item") != "coal" for port in inputs)
            or not primary.get("ports") or primary["ports"][0].get("item") != item):
        return _report("blocked", "replacement capacity cell has incompatible material ports", item=item)
    for block_key, block in (("source:" + item, primary), (key, plan)):
        result = factory.builder.ensure_plan(obs, block)
        if not _ready(result):
            return result
        result = factory.ensure_power_connection(obs, block_key, block)
        if not _ready(result):
            return result
    if inputs:
        coal = factory.ensure_product(obs, "coal")
        if not _ready(coal):
            return coal
        for port in inputs:
            result = factory.connect_input(obs, coal["evidence"]["ports"][0], port, key + ":fuel")
            if not _ready(result):
                return result
        if receiver.get("name") == "stone-furnace":
            owned = factory.state.setdefault("automated_burners", [])
            identity = factory._entity_key(receiver)
            if identity not in owned:
                owned.append(identity)
                factory._save()
    result = factory._merge_output(obs, outputs[0], primary["ports"][0], key + ":output")
    if not _ready(result):
        return result
    association = {"extraction_block": key,
                   "receiver": {"name": receiver["name"], "position": deepcopy(receiver["position"])},
                   "drill": deepcopy(drill)}
    if primary.get("active_source") != association:
        primary["active_source"] = association
        factory._save()
    return _report("succeeded", "existing capacity cell supplies the established material bus",
                   ports=primary["ports"], active_source=association, relocated=True,
                   reused_capacity_cell=True, flow_verified=False)
