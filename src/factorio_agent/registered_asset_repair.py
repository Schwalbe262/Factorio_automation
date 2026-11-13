"""Trusted known-plan adapters and paid batches for simple owned asset losses."""

from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json
from .infrastructure_repair import _hash, _key, _read, _payload, _same_frame
from .registered_belt_repair import BELT_GUARD_LUA
from .bus_factory import trace_corridor
from .compact_green import _transport
from .routing import VECTORS
from .planning import overlap
from .resource_policy import RESOURCE_CHECK_LUA, policy_specs, mining_reservations
from .smelting import merge_entities
from .transport import PIPE_WITNESS_LUA, xy

FILE = "registered-asset-repair.json"
ACTION = "repair_registered_assets"
KINDS = {"pipe", "transport-belt", "small-electric-pole"}


@dataclass(frozen=True)
class OwnerSpec:
    filename: str
    selector: tuple
    block: str


# Selectors are code-owned; actions cannot provide filenames, coordinates or paths.
OWNERS = {
    "chemical:plastic-bar": OwnerSpec("chemical-production.json", ("products", "plastic-bar"), "chemical_plastic-bar")
}


def _selected(document, spec):
    value = document
    for key in spec.selector:
        value = value[key]
    return value


def _authority(root, name, world, catalog):
    if name not in OWNERS:
        raise ValueError("unregistered repair owner")
    spec = OWNERS[name]
    document = _read(root / spec.filename)
    entry = _selected(document, spec)
    board = _read(root / "pcb-board.json")
    if (
        not all(_same_frame(q, world, catalog) for q in (document, entry, board))
        or entry.get("phase") != "verified"
        or board["blocks"].get(spec.block) != entry["plan"]
        or entry.get("product") != "plastic-bar"
    ):
        raise ValueError("known owner, selector, verified plan or PCB authority changed")
    return document, entry, board


def _fluid_authority(root, entry, world, catalog):
    registry = _read(root / "fluid-sources.json")
    if not _same_frame(registry, world, catalog):
        raise ValueError("fluid source registry identity changed")
    for fluid, h in entry["fluid_handoffs"].items():
        row = registry.get("handoffs", {}).get(h["consumer_id"], {}).get(fluid, {})
        neighbor = dict(h["first_neighbor"], unit_number=entry["bindings"].get(_key(h["first_neighbor"])))
        if (
            not _same_frame(row, world, catalog)
            or row.get("phase") != "bound"
            or row.get("source") != h["source"]
            or row.get("first_neighbor") != neighbor
            or h["source"] not in registry.get("sources", {}).values()
        ):
            raise ValueError("exact existing source or bound consumer handoff changed")
    return registry


def _protected(entry):
    return (
        {xy(q) for q in entry["plan"]["ports"]}
        | {xy(q) for q in entry["sources"].values()}
        | {xy(h["first_neighbor"]) for h in entry["fluid_handoffs"].values()}
    )


def _foreign_pins(root, units, spec):
    hits = []
    allowed = spec.filename + "." + ".".join(spec.selector) + ".bindings"

    def walk(v, path):
        if isinstance(v, dict):
            for key, item in v.items():
                if key == "asset_repair_history":
                    continue
                if key == "bindings":
                    if path + ".bindings" != allowed:
                        for node, unit in item.items():
                            if type(unit) is int and unit in units:
                                hits.append(path + ".bindings." + node)
                    continue
                if "unit" in key and type(item) is int and item in units:
                    hits.append(path + "." + key)
                walk(item, path + "." + key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(item, path + "[" + str(i) + "]")

    for path in root.glob("*.json"):
        if path.name in (FILE, "jobs.json", "catalog.json"):
            continue
        walk(_read(path), path.name)
    return hits[:16]


def _adapter(entry, obs, catalog):
    sources = list(entry["sources"].values())
    es = merge_entities(
        entry["plan"]["entities"],
        [
            dict(name=q["name"], position=q["position"], direction=q.get("facing", 0) if q["kind"] == "item" else 0)
            for q in sources
        ],
    )
    nodes = []
    for q in es:
        unit = entry["bindings"].get(_key(q)) or next((s["unit_number"] for s in sources if _key(s) == _key(q)), None)
        if type(unit) is not int or unit < 1:
            raise ValueError("known plan node lacks exact unit binding")
        nodes.append(dict(q, unit_number=unit, direction=0 if q["name"] == "pipe" else q.get("direction", 0)))
    typed = {}
    coal = next(q for q in sources if q["item"] == "coal")
    core = entry["core"]
    for source, destination, item in [(coal, core["coal_port"], "coal")]:
        route = trace_corridor(dict(entry["plan"], entities=es), source, destination)
        if not route.get("ok"):
            raise ValueError("known typed feed corridor is not traceable")
        for q in route["entities"]:
            typed[_key(q)] = [item]
    arm = core["input_arm"]
    vx, vy = VECTORS[arm["direction"]]
    pickup = (arm["position"]["x"] + vx, arm["position"]["y"] + vy)
    by = {xy(q): q for q in es if q["name"] in ("transport-belt", "underground-belt")}
    route = trace_corridor(
        dict(entry["plan"], entities=es), core["coal_port"], dict(position=dict(x=pickup[0], y=pickup[1]))
    )
    if not route.get("ok"):
        raise ValueError("known local coal header is not traceable")
    for q in route["entities"]:
        typed[_key(q)] = ["coal"]
    out = core["output_arm"]
    vx, vy = VECTORS[out["direction"]]
    drop = (xy(out)[0] - vx, xy(out)[1] - vy)
    route = trace_corridor(
        dict(entry["plan"], entities=es), dict(position=dict(x=drop[0], y=drop[1])), core["output_port"]
    )
    if not route.get("ok"):
        raise ValueError("known product collector is not traceable")
    for q in route["entities"]:
        typed[_key(q)] = ["plastic-bar"]
    _, graph = _transport(es, entry["plan"].get("underground_pairs", []))
    edges = [dict(a=_key(by[a]), b=_key(by[b])) for a, (b, _) in graph.items()]
    fluid = entry["fluid_plans"]["petroleum-gas"]
    fluid_nodes = [
        dict(q, unit_number=next(n["unit_number"] for n in nodes if _key(n) == _key(q))) for q in fluid["nodes"]
    ]
    powered = {
        q.get("electric_network_id") for q in obs["entities"] if q["name"] == "steam-engine" and q.get("energy", 0) > 0
    }
    anchors = [
        dict(name=q["name"], position=q["position"], unit_number=q["unit_number"], network=q["electric_network_id"])
        for q in obs["entities"]
        if q["name"] == "small-electric-pole" and q.get("electric_network_id") in powered
    ]
    owned = {n["unit_number"] for n in nodes if n["name"] == "small-electric-pole"}
    anchors.sort(key=lambda q: (q["unit_number"] not in owned, q["unit_number"]))
    if not anchors:
        raise ValueError("known live steam power anchor required")
    preserved = [p for pair in entry["plan"].get("underground_pairs", []) for p in pair.get("preserved_pipes", [])]
    return dict(
        nodes=nodes,
        typed=typed,
        edges=edges,
        pairs=entry["plan"].get("underground_pairs", []),
        kinds={k: True for k in KINDS},
        fluid_nodes=fluid_nodes,
        fluid_edges=fluid["edges"],
        machine=_key(core["machine"]),
        prototype_box_count=len(catalog.entities["chemical-plant"]["fluidbox_prototypes"]),
        physical_boxes={str(q["fluidbox_index"]): name for name, q in core["fluidbox_bindings"].items()},
        fluid_source=_key(fluid["source"]),
        anchor=anchors[0],
        preserved_pipes=preserved,
    )


def _validate(action, game):
    fields = {
        "type",
        "owner",
        "transaction_id",
        "expected_world_id",
        "expected_actor_unit",
        "expected_catalog_fingerprint",
    }
    if (
        not isinstance(action, dict)
        or set(action) - fields - {"reason"}
        or not fields <= set(action)
        or action.get("type") != ACTION
        or action.get("owner") not in OWNERS
        or not isinstance(action.get("expected_world_id"), str)
        or not action["expected_world_id"]
        or action.get("expected_world_id") != getattr(game, "expected_world", None)
        or not isinstance(action.get("expected_catalog_fingerprint"), str)
        or not action["expected_catalog_fingerprint"]
        or type(action.get("expected_actor_unit")) is not int
        or action["expected_actor_unit"] < 1
        or not isinstance(action.get("transaction_id"), str)
        or len(action["transaction_id"]) != 64
        or any(c not in "0123456789abcdef" for c in action["transaction_id"])
        or getattr(game, "backend", None) != "assisted"
    ):
        raise ValueError("exact registered paid asset repair envelope required")


def _context(game, action):
    _validate(action, game)
    root = Path(game.cfg.runtime_dir)
    state = _read(root / FILE)
    txn = state["transactions"][action["transaction_id"]]
    p = txn["payload"]
    if (
        not _same_frame(state, action["expected_world_id"], action["expected_catalog_fingerprint"])
        or _hash(p) != action["transaction_id"]
        or p["action"] != {k: v for k, v in action.items() if k not in ("reason", "transaction_id")}
    ):
        raise ValueError("immutable asset repair intent changed")
    doc, entry, board = _authority(
        root, action["owner"], action["expected_world_id"], action["expected_catalog_fingerprint"]
    )
    registry = _fluid_authority(root, entry, action["expected_world_id"], action["expected_catalog_fingerprint"])
    if (
        _hash(board) != p["board_revision"]
        or _hash(registry) != p["fluid_registry_revision"]
        or _hash(doc) not in (p["document_revision"], txn.get("publication", {}).get("after_revision"))
    ):
        raise ValueError("owner document or PCB changed during repair")
    if (
        txn.get("phase") not in ("prepared", "native_complete", "verified")
        or not 1 <= len(p["repairs"]) <= 8
        or len({q["old_unit"] for q in p["repairs"]}) != len(p["repairs"])
        or len({_key(q["entity"]) for q in p["repairs"]}) != len(p["repairs"])
    ):
        raise ValueError("bounded exact distinct asset losses required")
    ports = _protected(entry)
    cost = {}
    for q in p["repairs"]:
        entity = q["entity"]
        item = entity["name"]
        unit = q["old_unit"]
        if (
            item not in KINDS
            or entity not in entry["plan"]["entities"]
            or xy(entity) in ports
            or type(unit) is not int
            or unit < 1
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in xy(entity))
        ):
            raise ValueError("only registered internal simple assets may be repaired")
        cost[item] = cost.get(item, 0) + 1
    if p["cost"] != cost:
        raise ValueError("repair cost differs from exact plan losses")
    if _foreign_pins(root, {q["old_unit"] for q in p["repairs"]}, OWNERS[action["owner"]]):
        raise ValueError("lost asset is referenced by a foreign current witness")
    return dict(p, transaction_id=action["transaction_id"])


def _receipt_valid(r, p, key):
    if (
        not isinstance(r, dict)
        or r.get("ok") is not True
        or r.get("transaction_id") != key
        or r.get("world_id") != p["action"]["expected_world_id"]
        or r.get("catalog_fingerprint") != p["action"]["expected_catalog_fingerprint"]
        or type(r.get("actor_unit")) is not int
        or r.get("actor_unit") != p["action"]["expected_actor_unit"]
        or r.get("witness_version") != 2
        or r.get("post_tick_health_required") is not True
    ):
        return False
    rows = r.get("replacements", [])
    if (
        not isinstance(rows, list)
        or len(rows) != len(p["repairs"])
        or any(not isinstance(q, dict) or type(q.get("unit_number")) is not int or q["unit_number"] < 1 for q in rows)
    ):
        return False
    if (
        any(type(r.get(k)) is not int or r[k] < 0 for k in ("start_tick", "tick", "paid_count"))
        or r["start_tick"] != r["tick"]
        or r["paid_count"] != len(rows)
    ):
        return False
    if r.get("paid_items") != p["cost"] or len({q["unit_number"] for q in rows}) != len(rows):
        return False
    try:
        for when in ("before", "after"):
            snapshot = r[when]
            if not isinstance(snapshot["main_items"], dict) or set(snapshot["main_items"]) != KINDS:
                return False
            if not isinstance(snapshot["wires"], (dict, list)):
                return False
            for counts in (
                snapshot["main_items"],
                *(snapshot["goods"][part] for part in ("combined", "floor", "transport")),
            ):
                if not isinstance(counts, dict) or any(
                    not isinstance(k, str) or type(n) is not int or n < 0 for k, n in counts.items()
                ):
                    return False
        if r["before"]["goods"]["combined"] != r["after"]["goods"]["combined"]:
            return False
        for item in KINDS:
            if r["before"]["main_items"][item] - r["after"]["main_items"][item] != p["cost"].get(item, 0):
                return False
        return all(
            q.get("key") == _key(old["entity"])
            and q.get("old_unit") == old["old_unit"]
            and q.get("name") == old["entity"]["name"]
            and q.get("position") == old["entity"]["position"]
            and type(q.get("direction")) is int
            and (
                old["entity"]["name"] in ("pipe", "small-electric-pole")
                or q["direction"] == old["entity"].get("direction", 0)
            )
            and q["unit_number"] != old["old_unit"]
            for q, old in zip(rows, p["repairs"])
        )
    except (KeyError, TypeError):
        return False


def execute_repair_registered_assets(game, action):
    p = _context(game, action)
    try:
        r = game.query("local x=helpers.json_to_table(" + _payload(p) + ");" + ASSET_GUARD_LUA + BUILD_ASSETS_LUA)
    except Exception as exc:
        return dict(ok=False, reason="infrastructure_repair_outcome_unknown", original_reason=str(exc))
    if not isinstance(r, dict) or type(r.get("ok")) is not bool:
        return dict(ok=False, reason="infrastructure_repair_outcome_unknown", original_reason="invalid_asset_receipt")
    if r["ok"] and not _receipt_valid(r, p, action["transaction_id"]):
        return dict(
            r, ok=False, reason="infrastructure_repair_outcome_unknown", original_reason="paid_asset_receipt_invalid"
        )
    if not r["ok"] and (r.get("effects_started") or type(r.get("effects_started")) is not bool):
        return dict(r, reason="infrastructure_repair_outcome_unknown", original_reason=r.get("reason"))
    return r


class RegisteredAssetRepair:
    def __init__(self, game, builder, catalog, *, chemical=None):
        self.game, self.builder, self.catalog, self.chemical = game, builder, catalog, chemical
        self.path = Path(game.cfg.runtime_dir) / FILE
        self.last_diagnostic = None

    def _ledger(self, obs):
        if not obs.get("ok", True) or type(obs.get("actor_unit_number")) is not int:
            raise ValueError("fresh actor observation required")
        value = (
            _read(self.path)
            if self.path.exists()
            else dict(world_id=obs["world_id"], catalog_fingerprint=self.catalog.fingerprint, transactions={})
        )
        if not _same_frame(value, obs["world_id"], self.catalog.fingerprint):
            raise ValueError("asset repair ledger world or catalog changed")
        return value

    def _query(self, p, mode):
        return self.game.query(
            "local x=helpers.json_to_table(" + _payload(dict(p, mode=mode)) + ");" + ASSET_GUARD_LUA + SCAN_ASSETS_LUA
        )

    def prepare(self, obs, owner="chemical:plastic-bar"):
        try:
            self._ledger(obs)
            doc, entry, board = _authority(self.path.parent, owner, obs["world_id"], self.catalog.fingerprint)
            registry = _fluid_authority(self.path.parent, entry, obs["world_id"], self.catalog.fingerprint)
            p = _adapter(entry, obs, self.catalog)
            p.update(
                action=dict(
                    type=ACTION,
                    owner=owner,
                    expected_world_id=obs["world_id"],
                    expected_actor_unit=obs["actor_unit_number"],
                    expected_catalog_fingerprint=self.catalog.fingerprint,
                ),
                repairs=[],
                specs=[],
                cost={},
                zones=mining_reservations(self.game, obs["world_id"]),
            )
            native = self._query(p, "scan")
            if not native.get("ok"):
                return native
            by = {_key(q): q for q in entry["plan"]["entities"]}
            ports = _protected(entry)
            for missing in native.get("missing", []) or []:
                q = by.get(missing["key"])
                if not q or q["name"] not in KINDS or xy(q) in ports:
                    raise ValueError("missing asset is not internal simple infrastructure")
                p["repairs"].append(dict(entity=q, old_unit=entry["bindings"][_key(q)]))
                p["cost"][q["name"]] = p["cost"].get(q["name"], 0) + 1
            if not p["repairs"]:
                return dict(ok=True, needed=False, native=native)
            if len(p["repairs"]) > 8:
                raise ValueError("simple repair loss bound exceeded")
            pins = _foreign_pins(self.path.parent, {q["old_unit"] for q in p["repairs"]}, OWNERS[owner])
            if pins:
                raise ValueError("lost asset has foreign current identity pins: " + str(pins))
            for q in p["repairs"]:
                x, y = xy(q["entity"])
                area = [[x - 0.5, y - 0.5], [x + 0.5, y + 0.5]]
                if any(
                    overlap(area, b["reserved_area"])
                    for k, b in board["blocks"].items()
                    if k != OWNERS[owner].block and b.get("reserved_area")
                ):
                    raise ValueError("repair footprint conflicts with foreign future production")
            p["specs"] = policy_specs(self.builder, obs, [q["entity"] for q in p["repairs"]])
            positions = [xy(q["entity"]) for q in p["repairs"] if q["entity"]["name"] == "small-electric-pole"]
            candidates = [
                dict(name=e["name"], position=e["position"], unit_number=e["unit_number"])
                for e in obs["entities"]
                if e["name"] == "small-electric-pole" and any(math.dist(xy(e), point) <= 7.5 for point in positions)
            ]
            p.update(
                power_candidates=candidates,
                document_revision=_hash(doc),
                board_revision=_hash(board),
                fluid_registry_revision=_hash(registry),
                created_tick=native["tick"],
            )
            ready = self._query(p, "preflight")
            if not ready.get("ok"):
                return ready
            return dict(ok=True, needed=True, payload=p, transaction_id=_hash(p), cost=p["cost"], native=ready)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return dict(ok=False, reason=str(exc))

    def _publish(self, state, key, txn):
        p = txn["payload"]
        spec = OWNERS[p["action"]["owner"]]
        path = self.path.parent / spec.filename
        doc = _read(path)
        if "publication" not in txn:
            if _hash(doc) != p["document_revision"]:
                raise ValueError("asset publication baseline changed")
            after = deepcopy(doc)
            entry = _selected(after, spec)
            for row in txn["receipt"]["replacements"]:
                if entry["bindings"].get(row["key"]) != row["old_unit"]:
                    raise ValueError("asset old binding changed")
                entry["bindings"][row["key"]] = row["unit_number"]
            entry.setdefault("asset_repair_history", []).append(
                dict(transaction_id=key, receipt=deepcopy(txn["receipt"]))
            )
            txn["publication"] = dict(before_revision=_hash(doc), after_revision=_hash(after), value=after)
            _atomic_json(self.path, state)
        intent = txn["publication"]
        current = _hash(doc)
        if current == intent["before_revision"]:
            _atomic_json(path, intent["value"])
        elif current != intent["after_revision"]:
            raise ValueError("asset publication compare-and-swap changed")
        txn["phase"] = "verified"
        _atomic_json(self.path, state)
        if self.chemical is not None:
            self.chemical.state = _read(path)

    def step(self, obs):
        try:
            state = self._ledger(obs)
            pending = [(k, v) for k, v in state["transactions"].items() if v["phase"] != "verified"]
            if len(pending) > 1:
                raise ValueError("multiple unfinished asset repairs")
            if pending:
                key, txn = pending[0]
                action = dict(txn["payload"]["action"], transaction_id=key)
                p = _context(self.game, action)
                native = self._query(p, "inspect")
                if not native.get("ok"):
                    raise ValueError(native.get("reason", "native asset proof failed"))
                if native.get("receipt"):
                    if not _receipt_valid(native["receipt"], p, key):
                        raise ValueError("native asset receipt invalid")
                    txn.update(phase="native_complete", receipt=native["receipt"])
                    _atomic_json(self.path, state)
                    if not native.get("healthy") or native["tick"] <= native["receipt"]["tick"]:
                        self.last_diagnostic = dict(reason="waiting_for_normal_post_tick_asset_health", evidence=native)
                        return None
                    self._publish(state, key, txn)
                    self.last_diagnostic = dict(
                        ok=True, reason="known-plan assets normally repaired", receipt=txn["receipt"]
                    )
                    return None
                costs = txn["payload"]["cost"]
                for item, count in costs.items():
                    if native["actor_items"].get(item, 0) < count:
                        return self._fund(obs, item, count)
                return dict(action, reason="normally restore exact registered simple asset losses")
            ready = self.prepare(obs)
            if not ready.get("ok") or not ready.get("needed"):
                self.last_diagnostic = ready
                return None
            for item, count in ready["cost"].items():
                if ready["native"]["actor_items"].get(item, 0) < count:
                    return self._fund(obs, item, count)
            key = ready["transaction_id"]
            state["transactions"][key] = dict(phase="prepared", payload=ready["payload"])
            _atomic_json(self.path, state)
            return dict(
                ready["payload"]["action"],
                transaction_id=key,
                reason="normally restore exact registered simple asset losses",
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.last_diagnostic = dict(ok=False, reason=str(exc))
            return None

    def _fund(self, obs, item, count):
        result = self.builder.bootstrap.ensure_item(obs, item, count)
        self.last_diagnostic = dict(reason="normally fund registered assets", item=item, count=count)
        return result if result and result.get("type") else None

    def proof(self, obs):
        ready = self.prepare(obs)
        return (
            ready["native"] if ready.get("ok") and not ready.get("needed") and ready["native"].get("healthy") else None
        )


def _replace(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError("reviewed shared guard fragment changed")
    return text.replace(old, new, 1)


# Reuse the audited whole-world identity, typed belt, resource and V2 goods
# primitives; this fixed adapter adds fluid and electrical proofs below.
_BASE = BELT_GUARD_LUA.replace("registered_belt_repairs", "registered_asset_repairs")
_BASE = _replace(_BASE, 'if q.name~="transport-belt"or marker then', "if not x.kinds[q.name]or marker then")
_BASE = _replace(
    _BASE,
    'q.name~="small-electric-pole"and e.direction',
    'q.name~="small-electric-pole"and q.name~="pipe"and e.direction',
)
_BASE = _replace(
    _BASE,
    'target(p,"transport-belt")or not s.can_place_entity{name="transport-belt"',
    "target(p,q.entity.name)or not s.can_place_entity{name=q.entity.name",
)
_BASE = _replace(
    _BASE,
    'if not allowed then return false,"registered_belt_spill_wrong_typed_item"end',
    'if q.entity.name=="transport-belt"and not allowed then return false,"registered_belt_spill_wrong_typed_item"end',
)
begin = _BASE.index("local function stock()")
end = _BASE.index("local belts=stock();", begin)
_BASE = (
    _BASE[:begin]
    + r"""
local function stock()
 local values={pipe=0,["transport-belt"]=0,["small-electric-pole"]=0}
 for i=1,#main do local st=main[i];if st.valid_for_read and values[st.name]~=nil then
  if st.prototype.type~="item"or st.quality.name~="normal"or st.health~=1 or st.spoil_tick~=0
   or st.is_item_with_tags or st.is_item_with_inventory or st.is_item_with_entity_data then return nil end
  values[st.name]=values[st.name]+st.count
 end end;return values
end
"""
    + _BASE[end:]
)
begin = _BASE.index("local function belt_proof()")
end = _BASE.index("local function observe_goods()", begin)
section = _BASE[begin:end]
section = _replace(
    section,
    "for _,q in ipairs(x.repairs)do local k=key(q.entity);local e=live[k]",
    'for _,q in ipairs(x.repairs)do if q.entity.name=="transport-belt"then local k=key(q.entity);local e=live[k]',
)
section = _replace(section, " end;return true", " end end;return true")
_BASE = _BASE[:begin] + section + _BASE[end:]
begin = _BASE.index("  local vectors=")
end = _BASE.index("\n end;return true", begin)
_BASE = _BASE[:begin] + '  if q.entity.name=="transport-belt"then\n' + _BASE[begin:end] + "\n  end" + _BASE[end:]
_BASE = _BASE.replace(
    "paid_belts=marker and marker.paid_belts or 0",
    "paid_count=marker and marker.paid_count or 0,paid_items=marker and marker.paid_items or nil",
)

ASSET_GUARD_LUA = (
    "local args={world_id=x.action.expected_world_id};"
    + RESOURCE_CHECK_LUA
    + PIPE_WITNESS_LUA
    + _BASE
    + r"""
local machine=live[x.machine]
local function exact(q)
 local e=target(q.position,q.name)
 if not e or not e.valid or e.unit_number~=q.unit_number or e.force~=f or e.surface~=s or e.position.x~=q.position.x or e.position.y~=q.position.y then return nil end;return e
end
local anchor=exact(x.anchor)
if not anchor or anchor.electric_network_id~=x.anchor.network then return reject("asset_known_power_anchor_changed")end
local function copper(e)
 local c=e.get_wire_connector(defines.wire_connector_id.pole_copper,false);local out={}
 if c then for _,v in pairs(c.connections)do local other=v.target.owner
  if not other or not other.valid or other.force~=f or other.surface~=s then return nil end
  out[#out+1]={name=other.name,position={x=other.position.x,y=other.position.y},unit_number=other.unit_number}
 end end;return out
end
local function wire_snapshot()
 local rows={};local seen={}
 for _,q in ipairs(x.nodes)do if q.name=="small-electric-pole"then local e=live[key(q)]
  if e then local peers=copper(e);if not peers then return nil end
   for _,other in ipairs(peers)do local id=math.min(e.unit_number,other.unit_number)..":"..math.max(e.unit_number,other.unit_number)
    if not seen[id]then seen[id]=true;rows[#rows+1]={a={name=e.name,position={x=e.position.x,y=e.position.y},unit_number=e.unit_number},b=other}end
   end
  end
 end end;return rows
end
local function fluid_proof(allow_missing)
 local expected={};local mapping={}
 if machine then for i=1,x.prototype_box_count do local ok,proto=pcall(function()return machine.get_fluid_box_prototype(i)end)
  if ok and proto then for _,p in pairs(proto.index and{proto}or proto)do
   if mapping[tostring(p.index)]then return false,"asset_fluid_storage_mapping_invalid"end;mapping[tostring(p.index)]=i
  end end
 end end
 local function idx(q)if key(q)==x.machine then return mapping[tostring(q.fluidbox_index or 1)]end;return q.fluidbox_index or 1 end
 for _,edge in ipairs(x.fluid_edges)do local ea,eb=live[key(edge.a)],live[key(edge.b)]
  if ea and eb then local ai,bi=idx(edge.a),idx(edge.b);if not ai or not bi then return false,"asset_active_fluid_box_missing"end
   local forward,reverse=false,false
   for _,p in pairs(ea.get_fluid_box_neighbours(ai)or{})do if p.entity==eb and p.index==bi then forward=true end end
   for _,p in pairs(eb.get_fluid_box_neighbours(bi)or{})do if p.entity==ea and p.index==ai then reverse=true end end
   if not forward or not reverse then return false,"asset_native_fluid_peer_missing"end
   expected[key(edge.a).."@"..ai]=expected[key(edge.a).."@"..ai]or{};expected[key(edge.a).."@"..ai][eb.unit_number..":"..bi]=true
   expected[key(edge.b).."@"..bi]=expected[key(edge.b).."@"..bi]or{};expected[key(edge.b).."@"..bi][ea.unit_number..":"..ai]=true
  elseif not allow_missing then return false,"asset_fluid_node_missing"end
 end
 for _,q in ipairs(x.fluid_nodes)do local k=key(q);local e=live[k]
  if e then
   for name in pairs(e.get_fluid_contents())do if name~="petroleum-gas"then return false,"asset_fluid_contaminated"end end
   if k~=x.fluid_source then for _,p in pairs(e.get_fluid_box_neighbours(1)or{})do
    if not(expected[k.."@1"]or{})[p.entity.unit_number..":"..p.index]then return false,"asset_foreign_fluid_peer"end
   end end
   local b=e.get_fluid_segment_extent_bounding_box(1);local limit=prototypes.utility_constants.default_pipeline_extent
   if b.right_bottom.x-b.left_top.x>limit or b.right_bottom.y-b.left_top.y>limit then return false,"asset_pipeline_extent_exceeded"end
  elseif not allow_missing then return false,"asset_fluid_node_missing"end
 end
 if machine then for physical,fluid in pairs(x.physical_boxes)do local i=mapping[physical];if not i then return false,"asset_machine_fluid_box_missing"end
  local content=machine.get_fluid(i);if content and content.amount>0 and content.name~=fluid then return false,"asset_machine_wrong_fluid"end
  for _,p in pairs(machine.get_fluid_box_neighbours(i)or{})do if not(expected[x.machine.."@"..i]or{})[p.entity.unit_number..":"..p.index]then return false,"asset_machine_foreign_fluid_peer"end end
 end end
 return true
end
local function preserved_pipes()
 for _,q in ipairs(x.preserved_pipes)do local fresh=surface_pipe_witness(target(q.position,"pipe"),q)
  if not fresh or q.force_index~=f.index or #fresh.peers~=#q.peers then return false end
  local wanted={};for _,p in ipairs(q.peers)do wanted[p.unit_number..":"..p.fluidbox_index]=p end
  for _,p in ipairs(fresh.peers)do local old=wanted[p.unit_number..":"..p.fluidbox_index]
   if not old or old.name~=p.name or old.position.x~=p.position.x or old.position.y~=p.position.y then return false end
  end
 end;return true
end
local function fluid_sites()
 for _,q in ipairs(x.repairs)do if q.entity.name=="pipe"then local k=key(q.entity);local p=q.entity.position
  for _,other in pairs(s.find_entities_filtered{area={{p.x-1.6,p.y-1.6},{p.x+1.6,p.y+1.6}}})do
   if other.valid and next(other.prototype.fluidbox_prototypes or{})and not live[key(other)]then return false,"asset_new_foreign_fluid_neighbour"end
   if other.valid and other.name=="pipe"and math.abs(other.position.x-p.x)+math.abs(other.position.y-p.y)==1 then
    local declared=false
    for _,edge in ipairs(x.fluid_edges)do if(key(edge.a)==k and key(edge.b)==key(other))or(key(edge.b)==k and key(edge.a)==key(other))then declared=true end end
    if not declared then return false,"asset_new_undeclared_fluid_peer"end
   end
  end
 end end;return true
end
local function structure()
 local good,why=belt_proof();if not good then return false,why end
 local good,why=fluid_proof(false);if not good then return false,why end
 if not preserved_pipes()then return false,"asset_preserved_foreign_pipe_changed"end
 if anchor.electric_network_id~=x.anchor.network then return false,"asset_known_network_changed"end
 for _,q in ipairs(x.repairs)do if q.entity.name=="small-electric-pole"then local e=live[key(q.entity)]
  if not e or e.electric_network_id~=anchor.electric_network_id then return false,"asset_new_pole_not_connected_to_known_network"end
  local peers=copper(e);if not peers or #peers==0 then return false,"asset_new_pole_has_no_native_wire"end
  for _,other in ipairs(peers)do local allowed=false
   for _,candidate in ipairs(x.power_candidates)do if same(candidate,other)then allowed=true end end
   for _,repair in ipairs(x.repairs)do local new=live[key(repair.entity)];if new and new.unit_number==other.unit_number and repair.entity.name=="small-electric-pole"then allowed=true end end
   if not allowed then return false,"asset_new_pole_unreviewed_wire_peer"end
  end
 end end
 return true
end
local function health()
 if not machine or machine.products_finished<=0 or not machine.is_connected_to_electric_network()or machine.electric_network_id~=anchor.electric_network_id or machine.energy<=0 then return false end
 for _,q in ipairs(x.nodes)do if q.name=="fast-inserter"or q.name=="inserter"then local e=live[key(q)]
  if not e or not e.is_connected_to_electric_network()or e.electric_network_id~=anchor.electric_network_id or e.energy<=0 then return false end
 end end
 local fluid=machine.get_fluid_contents();return(fluid["petroleum-gas"]or 0)>0
end
"""
)

SCAN_ASSETS_LUA = r"""
local good,why=fluid_proof(not marker);if not good then return reject(why)end
if not preserved_pipes()then return reject("asset_preserved_pipe_changed")end
if x.mode=="preflight"then local good,why=site_checks();if not good then return reject(why)end
 local good,why=fluid_sites();if not good then return reject(why)end
 for _,q in ipairs(x.power_candidates)do if not exact(q)then return reject("asset_known_power_candidate_changed")end end
end
if marker then local good,why=structure();if not good then return reject(why)end end
return {ok=true,tick=game.tick,missing=missing,actor_items=belts,healthy=health(),receipt=effective_receipt,debris_count=#debris,damaged=damaged}
"""

BUILD_ASSETS_LUA = r"""
if marker then local good,why=structure();if not good then return reject(why)end;return marker.receipt end
if #missing~=#x.repairs then return reject("asset_all_exact_losses_must_be_absent")end
for item,n in pairs(x.cost)do if(belts[item]or 0)<n then return reject("asset_all_normal_materials_must_be_prepaid")end end
local good,why=site_checks();if not good then return reject(why)end
local good,why=fluid_sites();if not good then return reject(why)end
local good,why=fluid_proof(true);if not good then return reject(why)end
if not preserved_pipes()then return reject("asset_preserved_pipe_changed")end
for _,q in ipairs(x.power_candidates)do if not exact(q)then return reject("asset_power_candidate_changed")end end
local wires=wire_snapshot();local goods=observe_goods();if not wires or not goods then return reject("asset_full_before_snapshot_failed")end
local fluids={};for _,q in ipairs(x.fluid_nodes)do local e=live[key(q)];if e then fluids[key(q)]=e.get_fluid_contents()end end
d.registered_asset_repairs=d.registered_asset_repairs or{}
marker={version=2,phase="building",world_id=frame.expected_world_id,actor_unit=frame.expected_actor_unit,paid_count=0,paid_items={},replacements={},tick=game.tick,
 before={main_items=belts,goods=goods,wires=wires,fluids=fluids,corpses={},machine_energy=machine.energy,products_finished=machine.products_finished}}
for _,q in ipairs(corpses)do marker.before.corpses[#marker.before.corpses+1]={name=q.name,position=q.position}end
d.registered_asset_repairs[x.transaction_id]=marker
for _,q in ipairs(x.repairs)do local item=q.entity.name;local index=nil
 for i=1,#main do if main[i].valid_for_read and main[i].name==item and main[i].count>0 then index=i;break end end
 if not index then return reject("asset_source_changed_inside_paid_operation")end
 main[index].count=main[index].count-1;marker.paid_count=marker.paid_count+1;marker.paid_items[item]=(marker.paid_items[item]or 0)+1
 local e=s.create_entity{name=item,position=q.entity.position,direction=item=="pipe"and 0 or(q.entity.direction or 0),force=f,quality="normal",raise_built=true,player=a.player,preserve_ghosts_and_corpses=true}
 if not e then return reject("asset_paid_build_outcome_uncertain")end
 local k=key(q.entity);live[k]=e
 marker.replacements[#marker.replacements+1]={key=k,name=item,position=q.entity.position,old_unit=q.old_unit,unit_number=e.unit_number,direction=e.direction}
end
local after_goods=observe_goods();local after_items=stock();local after_wires=wire_snapshot()
marker.after={goods=after_goods,main_items=after_items,wires=after_wires}
if not after_goods or not after_items or not after_wires or not same(goods.combined,after_goods.combined)or game.tick~=marker.tick then return reject("asset_goods_conservation_failed")end
for item,n in pairs(x.cost)do if belts[item]-after_items[item]~=n then return reject("asset_material_conservation_failed")end end
for _,q in ipairs(corpses)do if not q.entity.valid or q.entity.name~=q.name or q.entity.position.x~=q.position.x or q.entity.position.y~=q.position.y then return reject("asset_existing_remnant_changed")end end
for _,wire in ipairs(wires)do local ea,eb=exact(wire.a),exact(wire.b);if not ea or not eb then return reject("asset_old_wire_identity_changed")end
 local forward=false;for _,other in ipairs(copper(ea)or{})do if other.unit_number==eb.unit_number then forward=true end end
 if not forward then return reject("asset_preexisting_native_wire_removed")end
end
local good,why=structure();if not good then return reject(why)end
marker.phase="complete";marker.receipt={ok=true,status="succeeded",transaction_id=x.transaction_id,world_id=frame.expected_world_id,catalog_fingerprint=frame.expected_catalog_fingerprint,
 actor_unit=frame.expected_actor_unit,paid_count=marker.paid_count,paid_items=marker.paid_items,replacements=marker.replacements,start_tick=marker.tick,tick=game.tick,
 before=marker.before,after=marker.after,witness_version=2,post_tick_health_required=true}
return marker.receipt
"""
