"""Construct the owned upstream prefixes feeding persisted input side taps."""
from __future__ import annotations

from collections import deque
from copy import deepcopy

from .factory_templates import DIRECTIONS


def _point(entity: dict) -> tuple:
    return entity["position"]["x"], entity["position"]["y"]


def _identity(entity: dict) -> tuple:
    return entity["name"], _point(entity), entity.get("direction", 0)


def _ready(result: dict) -> bool:
    return result.get("status") == "succeeded" and "type" not in result


def _compatible(plan: dict, source: dict) -> bool:
    consumer = plan.get("consumer_port") or {}
    return (source.get("kind") == "item" and bool(source.get("item"))
            and plan.get("source_port") == source
            and consumer.get("kind") == source["kind"]
            and consumer.get("item") == source["item"])


def _geometry(plan: dict) -> tuple:
    """Return directed belt edges and exact standard inserter pickup/drop ends."""
    entities = plan.get("entities") or []
    if len(entities) > 4096:
        raise ValueError("input route exceeds dependency geometry budget")
    belts, arms = {}, []
    for entity in entities:
        name = entity["name"]
        if name == "transport-belt":
            point = _point(entity)
            if entity.get("direction", 0) not in DIRECTIONS:
                raise ValueError("input route has unsupported belt direction")
            if point in belts and _identity(belts[point]) != _identity(entity):
                raise ValueError("input route has contradictory belt directions")
            belts[point] = entity
        elif name in {"inserter", "long-handed-inserter"}:
            delta = DIRECTIONS.get(entity.get("direction", 0))
            if delta is None:
                raise ValueError("input route has unsupported inserter direction")
            reach = 2 if name == "long-handed-inserter" else 1
            x, y = _point(entity)
            dx, dy = delta
            arms.append((entity, (x + dx * reach, y + dy * reach),
                         (x - dx * reach, y - dy * reach)))
    edges = {point: [] for point in belts}
    for point, belt in belts.items():
        direction = belt.get("direction", 0)
        dx, dy = DIRECTIONS[direction]
        destination = point[0] + dx, point[1] + dy
        other = belts.get(destination)
        if other and other.get("direction", 0) != (direction + 8) % 16:
            edges[point].append((destination, None))
    inlets = []
    for arm, pickup, drop in arms:
        if drop in belts:
            if pickup in belts:
                edges[pickup].append((drop, arm))
            else:
                inlets.append((arm, pickup, drop))
    return belts, edges, inlets


def _path(belts: dict, edges: dict, start: tuple, target: tuple) -> list | None:
    if start not in belts or target not in belts:
        return None
    queue, previous = deque([start]), {start: None}
    while queue:
        point = queue.popleft()
        if point == target:
            entities = [belts[point]]
            while previous[point] is not None:
                point, arm = previous[point]
                if arm is not None:
                    entities.append(arm)
                entities.append(belts[point])
            return list(reversed(entities))
        for destination, arm in edges[point]:
            if destination not in previous:
                previous[destination] = point, arm
                queue.append(destination)
    return None


def _powered_prefix(plan: dict, entities: list) -> dict:
    """Keep only supplying poles for the arms used before the chosen belt."""
    poles = [entity for entity in plan["entities"] if entity["name"] == "small-electric-pole"]
    required = list(entities)
    for arm in entities:
        if arm["name"] not in {"inserter", "long-handed-inserter"}:
            continue
        x, y = _point(arm)
        # The factory's standard small-pole placements use integer offsets up
        # to two tiles, entirely inside their 2.5-tile supply square.
        covering = [pole for pole in poles if max(abs(_point(pole)[0] - x),
                                                 abs(_point(pole)[1] - y)) <= 2]
        if not covering:
            raise ValueError("upstream input inserter has no reserved covering power pole")
        pole = min(covering, key=lambda row: (abs(_point(row)[0] - x) + abs(_point(row)[1] - y),
                                             _point(row)))
        if pole not in required:
            required.append(pole)
    return {**plan, "entities": deepcopy(required), "ports": []}


def _direct_intake(factory, plan: dict, provenance: dict) -> None:
    """Validate an explicitly reserved arm consuming from an owned belt."""
    intake = provenance["intake"]
    block = factory.state.get("blocks", {}).get(intake["block_key"], {})
    arm, receiver, belt = intake["inserter"], intake["receiver"], provenance["belt"]
    consumer = plan["consumer_port"]
    if (consumer.get("item") != "coal" or consumer.get("direction") != "input"
            or _point(consumer) != _point(belt) or consumer.get("facing") != belt.get("direction", 0)
            or consumer not in block.get("ports", [])
            or arm not in block.get("entities", [])
            or arm.get("name") != "inserter" or arm.get("direction") not in DIRECTIONS
            or receiver.get("name") not in {"stone-furnace", "steel-furnace", "burner-mining-drill"}
            or len(plan.get("entities", [])) != 1 or _identity(plan["entities"][0]) != _identity(belt)):
        raise ValueError("direct input intake does not match its reserved block and consumer")
    x, y = _point(arm)
    dx, dy = DIRECTIONS[arm["direction"]]
    rx, ry = _point(receiver)
    if ((x + dx, y + dy) != _point(belt)
            or abs(x - dx - rx) >= 1 or abs(y - dy - ry) >= 1):
        raise ValueError("direct input intake pickup or receiver geometry is invalid")
    _powered_prefix(block, [arm])


def ensure_input_dependencies(factory, obs: dict, source_port: dict, link_key: str) -> dict:
    """Validate provenance, then observe/build only ancestor paths to their taps.

    New side-tap plans persist ``upstream_tap={"link_key": key, "belt": belt}``.
    Legacy plans infer that record only from an external inserter's exact
    pickup coordinate and a unique compatible owning link. All ancestors are
    validated before any builder action; successful construction is not a flow
    proof. The caller still constructs and powers its own downstream plan.
    """
    links = factory.state.get("links", {})
    jobs, recovered, visiting = [], {}, set()

    def prepare(key: str, target: tuple, *, root: bool = False) -> None:
        if key in visiting:
            raise ValueError("input tap dependency cycle")
        if len(visiting) >= 32:
            raise ValueError("input tap dependency depth exceeds 32 links")
        plan = links.get(key)
        if not isinstance(plan, dict) or not _compatible(plan, source_port):
            raise ValueError("input tap dependency has incompatible or missing material ports")
        visiting.add(key)
        belts, edges, inlets = _geometry(plan)
        provenance = plan.get("upstream_tap")
        source_belt = belts.get(_point(source_port))
        if source_belt and source_belt.get("direction", 0) != source_port.get("facing"):
            raise ValueError("upstream input belt contradicts its canonical source facing")
        if root and provenance is None and not inlets:
            if source_belt is None:
                raise ValueError("input route has neither its canonical source nor an external tap")
            visiting.remove(key)
            return  # Direct links retain their existing construction workflow.

        route, inlet, owner, pickup_belt = None, None, None, None
        if provenance is not None:
            if not isinstance(provenance, dict):
                raise ValueError("input tap provenance is malformed")
            owner = provenance.get("link_key")
            pickup_belt = provenance.get("belt")
            if not isinstance(owner, str) or not isinstance(pickup_belt, dict):
                raise ValueError("input tap provenance is incomplete")
            upstream = links.get(owner)
            if not isinstance(upstream, dict) or not _compatible(upstream, source_port):
                raise ValueError("input tap provenance refers to incompatible upstream material")
            if pickup_belt.get("name") != "transport-belt" or not any(
                    _identity(entity) == _identity(pickup_belt) for entity in upstream.get("entities", [])):
                raise ValueError("input tap provenance belt is absent or its direction changed")
            if "intake" in provenance:
                _direct_intake(factory, plan, provenance)
                prepare(owner, _point(pickup_belt))
                if not root:
                    jobs.append((key, {**plan, "entities": deepcopy(plan["entities"])}))
                visiting.remove(key)
                return
            matches = [row for row in inlets if row[1] == _point(pickup_belt)]
            if len(matches) != 1:
                raise ValueError("input tap provenance does not match one external inserter pickup")
            inlet = matches[0]
            route = _path(belts, edges, inlet[2], target)
        else:
            route = _path(belts, edges, _point(source_port), target)
            if route is None:
                candidates = [(row, path) for row in inlets
                              if (path := _path(belts, edges, row[2], target)) is not None]
                if len(candidates) != 1:
                    raise ValueError("legacy input tap has no unique connected external pickup")
                inlet, route = candidates[0]
                owners = [(other_key, entity) for other_key, other in links.items()
                          if other_key != key and _compatible(other, source_port)
                          for entity in other.get("entities", [])
                          if entity["name"] == "transport-belt" and _point(entity) == inlet[1]]
                # Repeated identical rows in one plan do not create another owner.
                owners = list({(other_key, _identity(entity)): (other_key, entity)
                               for other_key, entity in owners}.values())
                if len(owners) != 1:
                    raise ValueError("legacy input tap has no unique compatible upstream owner")
                owner, pickup_belt = owners[0]
                recovered[key] = {"link_key": owner, "belt": deepcopy(pickup_belt)}
        if route is None:
            raise ValueError("upstream input route does not physically reach its tap")
        if inlet is not None:
            prepare(owner, _point(pickup_belt))
            route = [inlet[0], *route]
        prefix = _powered_prefix(plan, route)
        if not root:
            jobs.append((key, prefix))
        visiting.remove(key)

    try:
        plan = links.get(link_key) or {}
        prepare(link_key, _point(plan.get("consumer_port") or {}), root=True)
    except (KeyError, TypeError, ValueError) as error:
        return {"status": "blocked", "reason": "input tap upstream dependency is invalid",
                "evidence": {"link": link_key, "dependency_error": str(error)}}

    if recovered:
        for key, provenance in recovered.items():
            links[key]["upstream_tap"] = provenance
        factory._save()
    for key, prefix in jobs:
        result = factory.builder.ensure_plan(obs, prefix)
        if not _ready(result):
            return result
        poles = [entity for entity in prefix["entities"] if entity["name"] == "small-electric-pole"]
        for pole in poles:
            power_key, power_plan = "tap:" + key, prefix
            if len(poles) > 1:
                # A cached connection to one pole cannot prove a distant
                # crossing is powered. Give each required pole its own route.
                x, y = _point(pole)
                power_key += f":upstream:{x:g},{y:g}"
                power_plan = {**prefix, "entities": [deepcopy(pole)]}
            result = factory.ensure_power_connection(obs, power_key, power_plan)
            if not _ready(result):
                return result
    return {"status": "succeeded", "reason": "input tap upstream construction observed",
            "evidence": {"link": link_key, "upstream_links": [key for key, _ in jobs],
                         "flow_verified": False}}
