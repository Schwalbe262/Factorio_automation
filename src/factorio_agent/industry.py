"""Normal machine-built science to unlock basic logistics, with finite funding.

This small fixed-port cell is a bootstrap bridge. It is not sustained 30 SPM or
automated mining input. Gear/science crafting and internal transfers are real.
"""

from __future__ import annotations

import json
import math
import hashlib
import re
from copy import deepcopy
from pathlib import Path

from factorio_agent.core.deterministic_state import _atomic_json
from .board import (
    MAX_STARTUP_LABS,
    MAX_STARTUP_SCIENCE_MACHINES,
    Board,
    _plan as _builder_plan,
    extended_science_cell,
    lab_slot_entities,
    lab_slot_plan,
    science_extension_entities,
    science_extension_error,
    science_machine_count,
)


BOOTSTRAP_RESEARCH_MAX_UNITS = 100
DIVERSION_STALL_TICKS = 1800
SCIENCE_COURIER_BATCH = 5
# Funding moves at least this many plates per insert (or the batch's whole
# remainder): the machines start on the first plates instead of waiting for
# the full batch, without one-plate insert loops.
FUND_CHUNK = 10
# A prewarmed next batch is funded in packets of this many packs (or its whole
# remainder): never the one-pack packets of 1 copper + 2 iron seen live.
PREWARM_PACKET = 5
# Closed-loop input top-up (BOOTSTRAP_RESEARCH.md, closed-loop top-up): once the
# fixed bill is funded, a cell that has stood still this long with fewer
# deliverable packs than the technology still needs gets the shortfall's raw
# inputs. Ingredients split over parallel machines (one holds a copper plate,
# the other a gear) are not deliverable (seed 1298193051: logistics 19 of 20).
TOPUP_STALL_TICKS = 1800
# The technology whose research unlocks the cell's machines; the cell is
# prebuilt while it runs (``BootstrapLogistics.prebuild``).
CELL_UNLOCK_TECHNOLOGY = "automation"
# One sizing decision per finite target batch, kept outside the bound state.
CAPACITY_FILE = "startup-science-capacity.json"
# Further red-only roadmap technologies that justify one expansion.
CAPACITY_QUEUE_HORIZON = 3
RED_PACK = "automation-science-pack"


def _red_only_fixed(catalog, name):
    """True when ``name`` is a finite red-only technology this cell can research."""
    tech = (getattr(catalog, "technologies", None) or {}).get(name)
    return bool(
        isinstance(tech, dict)
        and not tech.get("research_trigger")
        and not tech.get("count_formula")
        and tech.get("ingredients") == [{"name": RED_PACK, "type": "item", "amount": 1}]
        and type(tech.get("unit_count")) is int
        and 1 <= tech["unit_count"] <= BOOTSTRAP_RESEARCH_MAX_UNITS
    )


def bootstrap_technologies(roadmap=None) -> list[str]:
    """Technologies of the roadmap's finite bootstrap steps, in roadmap order (what the temporary cell researches)."""
    from .progression import ROADMAP, STEP_TECHNOLOGY

    names = []
    for step in ROADMAP if roadmap is None else roadmap:
        if step.capability not in ("bootstrap_research", "bootstrap_logistics"):
            continue
        technology = step.parameters.get("technology") or STEP_TECHNOLOGY.get(step.capability)
        if isinstance(technology, str) and technology not in names:
            names.append(technology)
    return names


def _sampled_rates(root, world_id):
    """Read-only plate rates from the observer's ``rate-samples.json``: item -> (produced, consumed)/min."""
    path = Path(root) / "rate-samples.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("world_id") != world_id:
        return {}
    samples = data.get("samples") or []
    if len(samples) < 2 or samples[-1]["tick"] - samples[0]["tick"] < 300:
        return {}
    first, last = samples[0], samples[-1]
    minutes = (last["tick"] - first["tick"]) / 3600
    rates = {}
    for name, row in last["production"].items():
        base = first["production"].get(name, {})
        rates[name] = tuple((row.get(k, 0) - base.get(k, 0)) / minutes for k in ("produced", "consumed"))
    return rates


def start_floor(row, targets, pack_rate_each, config):
    """The startup cell's decision ``row`` raised to the start size: ``START_LABS`` labs and the machines they consume.

    Owner rule "2 labs first" (``science_growth.START_LABS``, as the permanent red
    block), limited to the labs the cell's ``max_assemblers`` machines keep busy
    (``floor(max_assemblers * pack_rate_each / lab_pack_rate(queue, 1))``: a second
    lab adds nothing to a pack-limited queue). The machines follow the labs:
    ``ceil(lab_pack_rate(queue, labs) / pack_rate_each)``. The time comparison still decides above
    the floor. The floor is the planner's own option ``(a, l)`` from ``row`` and is
    taken only when that option is finite (fundable); else ``row`` is kept.

    Why: the planner priced the second lab and machine as funded and built before
    any research and kept 1 + 1 on 4 of 12 seeds of fleet all-1114-0256
    ("+1 assemblers +1 labs takes 486s" against a 560 s wait); those seeds finished
    logistics at 22.4-23.6 min against 17.6-20.3 with two labs, and the district's
    splitters waited for logistics.
    """
    from .research_capacity import lab_pack_rate
    from .science_growth import START_LABS

    labs_now = int(row.get("current_labs") or 1)
    machines_now = int(row.get("current_assemblers") or 1)
    each = float(pack_rate_each or 0)
    per_lab = lab_pack_rate(targets, 1) if targets else None
    if not per_lab or each <= 0:
        return row
    # Only labs the cell's machines keep busy: 2 machines (12 packs/min) feed 2 labs on
    # 10-15 s units (EMD, logistics: 4.9 packs/min per lab) but only 1 on 5 s units.
    fed = math.floor(config.max_assemblers * each / per_lab + 1e-9)
    labs = min(START_LABS, int(config.max_labs), max(1, fed))
    if int(row.get("target_labs") or labs_now) >= labs or labs <= labs_now:
        return row
    rate = lab_pack_rate(targets, labs)
    machines = math.ceil(rate / each - 1e-9)
    machines = max(
        machines_now, int(row.get("target_assemblers") or machines_now), min(machines, config.max_assemblers)
    )
    wanted = (machines - machines_now, labs - labs_now)
    option = next(
        (
            o
            for o in row.get("options") or ()
            if isinstance(o, dict) and (o.get("extra_assemblers"), o.get("extra_labs")) == wanted
        ),
        None,
    )
    if option is None or option.get("seconds") is None:
        return row
    wait = row.get("wait_seconds")
    out = dict(row)
    out.update(
        target_assemblers=machines,
        target_labs=labs,
        chosen_seconds=option["seconds"],
        seconds_saved=round(wait - option["seconds"], 1) if isinstance(wait, (int, float)) else None,
        bottleneck_after=option.get("bottleneck", row.get("bottleneck_after")),
        expand=True,
        start_floor={
            "labs": labs,
            "machines": machines,
            "planner_target": [row.get("target_assemblers"), row.get("target_labs")],
        },
        reason=f"start {labs} labs and the {machines} machines they consume (owner: 2 labs first); planner: "
        + str(row.get("reason") or ""),
    )
    return out


def batch_refusal(root, obs):
    """Why the startup science cell must not change now (grow or retire), or None.

    No bootstrap batch state of this world may have an unresolved transfer, a
    prewarm whose technology is not yet researched, or a bound batch whose
    technology is not yet natively researched (funded-and-unfinished,
    including legacy logistics). An unreadable state refuses (fail closed).
    """
    root = Path(root)
    researched = obs.get("technologies", {})
    paths = sorted(root.glob("bootstrap-research-*.json"))
    paths.append(root / "bootstrap-logistics.json")
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "science_cell_extension_batch_state_unreadable"
        if not isinstance(data, dict):
            return "science_cell_extension_batch_state_unreadable"
        if not data or data.get("world_id") != obs["world_id"]:
            continue
        technology = data.get("technology", "logistics")
        if data.get("pending_transfer"):
            return "science_cell_extension_transfer_unresolved"
        if data.get("prewarm") and not researched.get(technology):
            return "science_cell_extension_prewarm_in_progress"
        if not researched.get(technology):
            return "science_cell_extension_batch_unfinished"
    return None


def validate_bootstrap_research(parameters):
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"technology"}
        or not isinstance(parameters.get("technology"), str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", parameters["technology"]) is None
    ):
        raise ValueError(
            "bootstrap_research requires a safe catalog technology identifier; finite red-only admission is checked against native rules"
        )


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _idle_powered_labs(obs):
    """Powered labs standing idle without packs while research runs (labs are not the bottleneck: add none).

    Between batches no research runs and the cell may still grow by its time
    model; during research, a built lab without packs that is not working
    means the labs already outnumber the packs reaching them.
    """
    if not obs.get("research"):
        return []
    return [
        e
        for e in obs.get("entities", [])
        if e.get("name") == "lab"
        and _powered(e)
        and e.get("status_name") not in (None, "working")
        and not (e.get("inventory") or {}).get(RED_PACK)
    ]


def permanent_science_due(root, obs):
    """Why the temporary science cell must not grow any more because the bus science block takes over, or None.

    Owner rule: build the factory, do not tend the transition. Once the
    planned bus exists (``planned_main_bus`` succeeded; the bus science block
    is the next step and fundable from bus taps) or the permanent block is
    planned (``automation-science.json`` of this world, or an
    ``automation_science`` job not failed), the cell keeps what it has.
    """
    root = Path(root)
    try:
        science = json.loads((root / "automation-science.json").read_text(encoding="utf-8"))
        if isinstance(science, dict) and science.get("world_id") == obs.get("world_id"):
            return "permanent_science_block_planned"
    except (OSError, ValueError):
        pass
    try:
        ledger = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(ledger, dict) or ledger.get("world_id") != obs.get("world_id"):
        return None
    for job in (ledger.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        if job.get("capability") == "automation_science" and job.get("status") != "failed":
            return "permanent_science_job_open"
        if job.get("capability") == "planned_main_bus" and job.get("status") == "succeeded":
            return "bus_science_fundable"
    return None


def _powered(entity):
    energy = (entity or {}).get("energy")
    return bool(
        entity
        and entity.get("electric_network_connected")
        and type(energy) in (int, float)
        and math.isfinite(energy)
        and energy > 0
    )


def _audit_gear_diversions(root, state, plan, obs):
    """Stream exact paired receipts; no inferred loss and no game/checkpoint effects."""
    from .construction_supply import validate_take_many

    ledger = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
    if ledger.get("schema_version") != 2 or ledger.get("world_id") != state["world_id"]:
        raise ValueError("diversion ledger world changed")
    jobs = ledger.get("jobs", {})
    capability = "bootstrap_logistics" if state["technology"] == "logistics" else "bootstrap_research"
    parameters = {} if state["technology"] == "logistics" else {"technology": state["technology"]}
    targets = [
        (key, j)
        for key, j in jobs.items()
        if j.get("capability") == capability
        and j.get("parameters") == parameters
        and j.get("world_id") == state["world_id"]
    ]
    if len(targets) != 1:
        raise ValueError("unique target research job required")
    target_key, target = targets[0]
    if target.get("pending_action"):
        raise ValueError("target job has an unresolved action")
    created = target.get("created_tick")
    if type(created) is not int or created < 0:
        raise ValueError("target job creation tick missing")
    gear = next(p for p in state["machines"] if p.get("recipe") == "iron-gear-wheel")
    ports = {
        p["item"]: next(pin for pin in state["ports"] if pin["position"] == p["position"])
        for p in plan["ports"]
        if p.get("item") in state["required_inputs"]
    }
    path = root / "job-events.jsonl"
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("diversion audit history exceeds bounded64MiB scan")
    pending = {}
    funding = None
    receipts = {}
    known = {r["id"] for e in state.get("diversion_recoveries", []) for r in e["authorization"]["receipts"]}

    def relevant(source):
        return source.get("unit_number") == gear["unit_number"] or (
            source.get("name") == gear["name"] and source.get("position") == gear["position"]
        )

    def job_valid(key):
        row = jobs.get(key, {})
        return row.get("key") == key and row.get("world_id") == state["world_id"]

    with path.open("rb") as stream:
        for index, raw in enumerate(stream):
            if index >= 200000 or len(raw) > 1024 * 1024:
                raise ValueError("diversion audit history exceeds bounded scan")
            row = json.loads(raw)
            key = row.get("key")
            event = row.get("event")
            tick = row.get("tick")
            if event not in ("action_intent", "action_result"):
                continue
            if type(tick) is not int or not created <= tick <= obs["tick"]:
                continue
            if event == "action_intent":
                if key in pending:
                    raise ValueError("unpaired relevant diversion intent")
                action = row.get("action", {})
                is_take = (
                    action.get("type") == "take_many"
                    and action.get("item") == "iron-gear-wheel"
                    and any(relevant(s) for s in action.get("sources", []))
                )
                is_fund = (
                    key == target_key
                    and action.get("type") == "insert"
                    and action.get("item") in ports
                    and action.get("reason") == "fund one finite machine research batch for " + state["technology"]
                )
                if is_take or is_fund:
                    pending[key] = (row, raw, is_take, is_fund)
                continue
            previous = pending.pop(key, None)
            result = row.get("result", {})
            if previous is None:
                if funding and any(relevant(s) and s.get("moved", 0) > 0 for s in result.get("sources", [])):
                    raise ValueError("diversion receipt has no matching intent")
                continue
            intent, intent_raw, is_take, is_fund = previous
            action = intent["action"]
            if is_take and not funding:
                continue
            if not job_valid(key) or tick != intent["tick"]:
                raise ValueError("diversion pair job/world/tick changed")
            moved = result.get("moved")
            count = action.get("count")
            if (
                result.get("ok") is not True
                or type(count) is not int
                or type(moved) is not int
                or not 0 <= moved <= count
                or result.get("world_id", state["world_id"]) != state["world_id"]
            ):
                raise ValueError("diversion transfer result unknown or invalid")
            intent_sha = hashlib.sha256(intent_raw).hexdigest()
            result_sha = hashlib.sha256(raw).hexdigest()
            receipt_id = hashlib.sha256(intent_raw + b"\0" + raw).hexdigest()
            if is_fund:
                pin = ports[action["item"]]
                if (
                    action.get("name") != pin["name"]
                    or action.get("position") != pin["position"]
                    or action.get("inventory") != "chest"
                    or count > state["required_inputs"][action["item"]]
                ):
                    raise ValueError("funding receipt does not match pinned input")
                if moved and funding is None:
                    funding = dict(
                        id=receipt_id,
                        tick=tick,
                        job_key=key,
                        item=action["item"],
                        source=deepcopy(pin),
                        action_sha256=intent_sha,
                        result_sha256=result_sha,
                    )
                continue
            if not funding or intent["tick"] < funding["tick"]:
                continue
            validate_take_many(action)
            if action["world_id"] != state["world_id"]:
                raise ValueError("diversion action world changed")
            rows = result.get("sources")
            specs = {s["unit_number"]: s for s in action["sources"]}
            if (
                not isinstance(rows, list)
                or result.get("requested") != count
                or len(rows) > len(specs)
                or len({s.get("unit_number") for s in rows}) != len(rows)
                or result.get("status") != ("succeeded" if moved else "waiting")
            ):
                raise ValueError("diversion source receipt malformed")
            total = 0
            diverted = 0
            for source in rows:
                spec = specs.get(source.get("unit_number"))
                n = source.get("moved")
                if (
                    not spec
                    or type(n) is not int
                    or n < 0
                    or source.get("name") != spec["name"]
                    or source.get("position") != spec["position"]
                ):
                    raise ValueError("diversion source identity or conservation changed")
                total += n
                if relevant(spec):
                    if (
                        any(spec.get(k) != gear[k] for k in ("name", "position", "unit_number"))
                        or spec.get("kind") != "output"
                    ):
                        raise ValueError("diversion is not the pinned gear output")
                    diverted += n
            if total != moved:
                raise ValueError("diversion aggregate count mismatch")
            if diverted and receipt_id not in known:
                receipts[receipt_id] = dict(
                    id=receipt_id,
                    action_sha256=intent_sha,
                    result_sha256=result_sha,
                    job_key=key,
                    tick=tick,
                    item="iron-gear-wheel",
                    moved=diverted,
                    source=deepcopy(gear),
                )
    if funding is None:
        raise ValueError("first successful target funding receipt missing")
    if pending:
        raise ValueError("unresolved relevant diversion intent remains")
    return target_key, funding, list(receipts.values())


def _audit_legacy_input_replacement(root, world, port, amount, tick):
    """Known paid replacement chain, explicitly not a quantity-of-goods loss proof."""
    ledger = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
    if ledger.get("schema_version") != 2 or ledger.get("world_id") != world:
        raise ValueError("legacy ledger world changed")
    if any(
        j.get("capability") == "bootstrap_logistics" and j.get("world_id") == world and j.get("pending_action")
        for j in ledger.get("jobs", {}).values()
    ):
        raise ValueError("legacy target has an unresolved native action")
    pending = {}
    birth = None
    funding = None
    chain = None
    path = root / "job-events.jsonl"
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("legacy history exceeds bounded scan")
    with path.open("rb") as stream:
        for index, raw in enumerate(stream):
            if index >= 200000 or len(raw) > 1024 * 1024:
                raise ValueError("legacy history exceeds bounded scan")
            row = json.loads(raw)
            event = row.get("event")
            key = row.get("key")
            at = row.get("tick")
            if event not in ("action_intent", "action_result") or type(at) is not int or not 0 <= at <= tick:
                continue
            if event == "action_intent":
                if key in pending:
                    raise ValueError("legacy relevant intent unresolved")
                action = row.get("action", {})
                if (
                    action.get("position") == port["position"]
                    and action.get("name") == port["name"]
                    and action.get("type") in ("build", "insert", "mine")
                ):
                    pending[key] = (row, raw)
                continue
            previous = pending.pop(key, None)
            if not previous:
                continue
            intent, first = previous
            action = intent["action"]
            result = row.get("result", {})
            job = ledger.get("jobs", {}).get(key, {})
            if (
                job.get("world_id") != world
                or job.get("key") != key
                or intent["tick"] != at
                or result.get("ok") is not True
                or result.get("world_id", world) != world
            ):
                raise ValueError("legacy pair world/identity/outcome unknown")
            ref = dict(
                id=hashlib.sha256(first + b"\0" + raw).hexdigest(),
                action_sha256=hashlib.sha256(first).hexdigest(),
                result_sha256=hashlib.sha256(raw).hexdigest(),
                tick=at,
                job_key=key,
            )
            if action["type"] == "build":
                unit = result.get("unit_number")
                if result.get("reused") is True or type(unit) is not int or unit < 1:
                    raise ValueError("legacy birth is not a paid new chest")
                current = dict(ref, unit_number=unit)
                if funding and unit == port["unit_number"] and unit != funding["original_chest"]["unit_number"]:
                    chain = dict(funding=funding, replacement=current)
                birth = current
            elif action["type"] == "mine" and funding:
                raise ValueError("legacy input was normally recovered; loss scope unsupported")
            elif (
                action["type"] == "insert"
                and action.get("item") == "iron-plate"
                and action.get("reason") == "fund finite normal machine-science inputs"
                and funding is None
            ):
                if (
                    job.get("capability") != "bootstrap_logistics"
                    or job.get("parameters") != {}
                    or not birth
                    or action.get("inventory") != "chest"
                    or action.get("count") != amount
                    or result.get("moved") != amount
                ):
                    raise ValueError("legacy original normal funding not fully proved")
                funding = dict(ref, original_chest=deepcopy(birth), item="iron-plate", moved=amount)
    if pending or chain is None:
        raise ValueError("legacy paid funding and current paid replacement chain missing")
    return chain


class BootstrapLogistics:
    def __init__(self, game, bootstrap, builder, catalog, technology="logistics"):
        if technology != "logistics":
            validate_bootstrap_research({"technology": technology})
        self.game, self.bootstrap, self.builder, self.catalog = game, bootstrap, builder, catalog
        self.technology = technology
        self.board = Board(game, builder, catalog)
        filename = (
            "bootstrap-logistics.json" if technology == "logistics" else "bootstrap-research-" + technology + ".json"
        )
        self.path = Path(game.cfg.runtime_dir) / filename
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs, part):
        return next(
            (e for e in obs["entities"] if e["name"] == part["name"] and e["position"] == part["position"]), None
        )

    def requirements(self):
        name = getattr(self, "technology", "logistics")
        tech = self.catalog.technologies[name]
        if tech.get("research_trigger") or tech["ingredients"] != [
            {"name": "automation-science-pack", "type": "item", "amount": 1}
        ]:
            raise ValueError(name + " science rules changed")
        if not self._is_legacy() and (
            type(tech.get("unit_count")) is not int
            or not 1 <= tech["unit_count"] <= BOOTSTRAP_RESEARCH_MAX_UNITS
            or tech.get("count_formula")
        ):
            raise ValueError("bootstrap research requires a fixed positive unit count no greater than100")
        count = int(tech["unit_count"])
        science = self.catalog.recipe_for_product("automation-science-pack")
        gears = self.catalog.recipe_for_product("iron-gear-wheel")

        def product(recipe, item):
            return sum(p["amount"] for p in recipe["products"] if p["name"] == item and p.get("probability", 1) == 1)

        if not self._is_legacy():
            if (
                {p["name"] for p in science["ingredients"]} != {"iron-gear-wheel", "copper-plate"}
                or {p["name"] for p in gears["ingredients"]} != {"iron-plate"}
                or any(
                    p.get("type", "item") != "item" or type(p.get("amount")) is not int or p["amount"] <= 0
                    for r in (science, gears)
                    for p in r["ingredients"]
                )
                or any(
                    type(product(r, item)) is not int or product(r, item) <= 0
                    for r, item in ((science, "automation-science-pack"), (gears, "iron-gear-wheel"))
                )
            ):
                raise ValueError("bootstrap machine recipe rules changed")
        science_cycles = math.ceil(count / product(science, "automation-science-pack"))
        ingredients = {p["name"]: p["amount"] * science_cycles for p in science["ingredients"]}
        gear_cycles = math.ceil(ingredients["iron-gear-wheel"] / product(gears, "iron-gear-wheel"))
        iron = next(p["amount"] for p in gears["ingredients"] if p["name"] == "iron-plate") * gear_cycles
        return count, {"iron-plate": int(iron), "copper-plate": int(ingredients["copper-plate"])}

    def step(self, obs):
        if self._is_legacy():
            if self.state.get("legacy_repurchase"):
                return self._legacy_repurchase_step(obs)
            return self._legacy_step(obs)
        return self._research_step(obs)

    def proof(self, obs):
        if self._is_legacy():
            if self.state.get("legacy_repurchase"):
                return self._legacy_repurchase_proof(obs)
            return self._legacy_proof(obs)
        return self._research_proof(obs)

    def _legacy_step(self, obs):
        if obs.get("technologies", {}).get("logistics"):
            return {"status": "succeeded", "reason": "logistics naturally researched", "evidence": self.proof(obs)}
        if not obs.get("technologies", {}).get("automation"):
            return {"status": "blocked", "reason": "automation_required_for_machine_science"}
        if self.state and self.state["world_id"] != obs["world_id"]:
            return {"status": "blocked", "reason": "industry_world_changed"}
        count, inputs = self.requirements()
        if not self.state:
            self.state = {
                "world_id": obs["world_id"],
                "allocated_inputs": {},
                "required_packs": count,
                "baseline_produced": obs["production"]["automation-science-pack"]["produced"],
                "baseline_consumed": obs["production"]["automation-science-pack"]["consumed"],
            }
            _atomic_json(self.path, self.state)
        plan = self.board.reserve_startup_science(obs)
        if not plan.get("ok"):
            return {"status": "blocked", "reason": plan.get("reason", "board_reservation_failed")}
        result = self.builder.ensure_plan(obs, plan)
        if result.get("status") != "succeeded":
            return result
        machines = [self.find(obs, e) for e in plan["entities"] if e["name"] == "assembling-machine-1"]
        if any(not e or not e.get("electric_network_connected") for e in machines):
            return {"status": "blocked", "reason": "science_cell_power_not_connected"}
        for item, total in inputs.items():
            allocated = self.state["allocated_inputs"].get(item, 0)
            remaining = total - allocated
            if remaining <= 0:
                continue
            if obs.get("inventory", {}).get(item, 0) < remaining:
                return self.bootstrap.ensure_item(obs, item, remaining)
            port = next(p for p in plan["ports"] if p["item"] == item)
            self.state["allocated_inputs"][item] = total
            _atomic_json(self.path, self.state)
            return {
                "type": "insert",
                "name": "wooden-chest",
                "position": port["position"],
                "inventory": "chest",
                "item": item,
                "count": remaining,
                "reason": "fund finite normal machine-science inputs",
            }
        labs = [e for e in obs["entities"] if e["name"] == "lab" and e.get("electric_network_connected")]
        if not labs:
            return {"status": "blocked", "reason": "powered_research_lab_missing"}
        if obs.get("research") not in (None, "logistics"):
            return {"status": "blocked", "reason": "another_research_is_running"}
        if obs.get("research") != "logistics":
            return {
                "type": "research",
                "technology": "logistics",
                "reason": "consume normally assembled science for logistics",
            }
        held = obs.get("inventory", {}).get("automation-science-pack", 0)
        if held:
            return {
                "type": "insert",
                "name": "lab",
                "position": labs[0]["position"],
                "inventory": "lab_input",
                "item": "automation-science-pack",
                "count": min(held, count),
                "reason": "supply machine-produced logistics science",
            }
        port = next(p for p in plan["ports"] if p["direction"] == "output")
        output = self.find(obs, {"name": "wooden-chest", "position": port["position"]})
        available = (output or {}).get("inventory", {}).get("automation-science-pack", 0)
        if available:
            return {
                "type": "take",
                "name": "wooden-chest",
                "position": port["position"],
                "inventory": "chest",
                "item": "automation-science-pack",
                "count": min(available, count),
                "reason": "collect real assembled bootstrap science",
            }
        recovery = self._legacy_repurchase_monitor(obs)
        if recovery:
            return recovery
        return {
            "status": "waiting",
            "reason": "waiting for machine science and natural logistics research",
            "evidence": {
                "required_packs": count,
                "machine_products_finished": [e.get("products_finished", 0) for e in machines],
                "research_progress": obs.get("research_progress", 0),
            },
        }

    def _legacy_proof(self, obs):
        if not obs.get("technologies", {}).get("logistics"):
            return None
        block = self.board.state.get("blocks", {}).get("startup_science")
        if block is None:
            return {"tick": obs["tick"], "technology": "logistics", "already_researched": True}
        red = next(e for e in block["entities"] if e.get("recipe") == "automation-science-pack")
        actual = self.find(obs, red)
        produced = (actual or {}).get("products_finished", 0)
        count = self.state.get("required_packs", 0)
        if produced < count:
            return None
        return {
            "tick": obs["tick"],
            "technology": "logistics",
            "normally_assembled_packs": produced,
            "research_science_consumed": obs["production"]["automation-science-pack"]["consumed"]
            - self.state["baseline_consumed"],
            "allocated_normal_inputs": self.state["allocated_inputs"],
            "reserved_area": block["reserved_area"],
            "scope": "finite starter machine funding; not sustained automated raw input or30SPM",
        }

    def _save(self):
        _atomic_json(self.path, self.state)

    def _is_legacy(self):
        state = getattr(self, "state", {})
        return (
            getattr(self, "technology", "logistics") == "logistics"
            and bool(state)
            and "baseline_produced" in state
            and "baseline_products_finished" not in state
        )

    def _legacy_original(self):
        return {
            k: deepcopy(self.state[k])
            for k in ("world_id", "required_packs", "allocated_inputs", "baseline_produced", "baseline_consumed")
        }

    def _legacy_repurchase_candidate(self, obs):
        if not self._is_legacy() or self.state.get("legacy_repurchase"):
            raise ValueError("legacy repurchase is limited to one epoch")
        count, inputs = self.requirements()
        plan = self._startup_plan(obs)
        if (
            plan is None
            or self.state["world_id"] != obs["world_id"]
            or self.state["required_packs"] != count
            or self.state["allocated_inputs"] != inputs
        ):
            raise ValueError("legacy original funding or registered plan changed")
        if (
            obs.get("research") != "logistics"
            or obs.get("technologies", {}).get("logistics")
            or type(obs.get("research_progress")) not in (int, float)
            or obs["research_progress"] != 0
        ):
            raise ValueError("legacy repurchase requires native zero-progress logistics")
        specs = [e for e in plan["entities"] if e["name"] in ("assembling-machine-1", "wooden-chest")]
        cell = [self.find(obs, e) for e in specs]
        labs = [e for e in obs["entities"] if e["name"] == "lab"]
        if (
            len(cell) != 5
            or any(not e or type(e.get("unit_number")) is not int for e in cell)
            or not labs
            or not _powered(labs[0])
        ):
            raise ValueError("legacy live cell/lab identity or power missing")
        machines = [e for e in cell if e["name"] == "assembling-machine-1"]
        if any(not _powered(e) for e in machines):
            raise ValueError("legacy live machine power missing")
        red = next(e for e in machines if e.get("recipe") == "automation-science-pack")
        gear = next(e for e in machines if e.get("recipe") == "iron-gear-wheel")
        stock = lambda name: sum(e.get("inventory", {}).get(name, 0) for e in cell)
        science = (
            stock("automation-science-pack")
            + obs.get("inventory", {}).get("automation-science-pack", 0)
            + sum(e.get("inventory", {}).get("automation-science-pack", 0) for e in labs)
        )
        if (
            stock("iron-plate")
            or stock("iron-gear-wheel")
            or science
            or stock("copper-plate") != inputs["copper-plate"]
        ):
            raise ValueError("legacy repurchase requires Fe/gears/science zero and preserved full copper batch")
        stats = obs["production"]["automation-science-pack"]
        counters = dict(
            red=red["products_finished"],
            gear=gear["products_finished"],
            produced=stats["produced"],
            consumed=stats["consumed"],
        )
        if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in counters.values()):
            raise ValueError("legacy current native counters required")
        pin = lambda e: {k: deepcopy(e[k]) for k in ("name", "position", "unit_number", "recipe") if k in e}
        return plan, dict(
            original=self._legacy_original(),
            world_id=obs["world_id"],
            catalog_fingerprint=self.catalog.fingerprint,
            plan_hash=_hash(plan),
            pins=[pin(e) for e in cell] + [pin(labs[0])],
            baseline=counters,
            required_packs=count,
            repurchase_inputs={"iron-plate": inputs["iron-plate"]},
            preserved_copper=inputs["copper-plate"],
        )

    def prepare_legacy_repurchase(self, obs):
        """Read-only known-replacement repurchase; original goods loss remains unknown."""
        try:
            plan, auth = self._legacy_repurchase_candidate(obs)
            port = next(
                p
                for p in auth["pins"]
                if p["position"] == next(q["position"] for q in plan["ports"] if q.get("item") == "iron-plate")
            )
            auth["receipt_chain"] = _audit_legacy_input_replacement(
                Path(self.game.cfg.runtime_dir),
                obs["world_id"],
                port,
                auth["repurchase_inputs"]["iron-plate"],
                obs["tick"],
            )
            auth.update(
                observed_tick=obs["tick"],
                historical_goods_loss_quantity="unknown",
                scope="one normally paid repurchase after known input replacement; no refund or original allocation reset",
            )
            return dict(
                ok=True,
                epoch=dict(
                    id=_hash(auth), authorization=auth, allocated_inputs={}, collected_packs=0, supplied_packs=0
                ),
            )
        except (OSError, KeyError, TypeError, ValueError, StopIteration) as exc:
            return dict(ok=False, reason="legacy_science_repurchase_audit_unavailable", detail=str(exc))

    def _legacy_epoch_error(self, obs, *, historical=False):
        """Why the legacy repurchase binding no longer holds, or None.

        ``historical`` (logistics natively researched) also accepts a plan
        the cell grew from, like ``_plan_hash_ok``.
        """
        try:
            epoch = self.state["legacy_repurchase"]
            auth = epoch["authorization"]
            plan = self._startup_plan(obs)
            if (
                epoch["id"] != _hash(auth)
                or auth["original"] != self._legacy_original()
                or auth["world_id"] != obs["world_id"]
                or auth["catalog_fingerprint"] != self.catalog.fingerprint
                or plan is None
                or not self._plan_hash_ok(auth["plan_hash"], plan, historical=historical)
            ):
                raise ValueError("legacy repurchase world/catalog/original/plan changed")
            if set(epoch["allocated_inputs"]) - set(auth["repurchase_inputs"]):
                raise ValueError("legacy extra funding item")
            if any(
                type(n) is not int or not 0 <= n <= auth["repurchase_inputs"][k]
                for k, n in epoch["allocated_inputs"].items()
            ):
                raise ValueError("legacy repurchase budget exceeded")
            if (
                any(type(epoch.get(k)) is not int for k in ("collected_packs", "supplied_packs"))
                or not 0 <= epoch["supplied_packs"] <= epoch["collected_packs"] <= auth["required_packs"]
            ):
                raise ValueError("legacy science transfer credits changed")
            for pin in auth["pins"]:
                actual = self.find(obs, pin)
                if not actual or any(actual.get(k) != v for k, v in pin.items()):
                    raise ValueError("legacy current unit or recipe changed")
                if pin["name"] in ("assembling-machine-1", "lab") and not _powered(actual):
                    raise ValueError("legacy power unavailable")
            red = next(self.find(obs, p) for p in auth["pins"] if p.get("recipe") == "automation-science-pack")
            gear = next(self.find(obs, p) for p in auth["pins"] if p.get("recipe") == "iron-gear-wheel")
            now = dict(
                red=red["products_finished"],
                gear=gear["products_finished"],
                **obs["production"]["automation-science-pack"],
            )
            if any(
                type(now[k]) not in (int, float) or not math.isfinite(now[k]) or now[k] < n
                for k, n in auth["baseline"].items()
            ):
                raise ValueError("legacy native counter rollback")
        except (KeyError, TypeError, ValueError, StopIteration):
            return "legacy_science_repurchase_binding_changed"
        return None

    def _legacy_repurchase_step(self, obs):
        finished = bool(obs.get("technologies", {}).get("logistics"))
        error = self._legacy_epoch_error(obs, historical=finished)
        if error:
            return dict(status="blocked", reason=error)
        if self.state.get("pending_transfer"):
            return dict(status="blocked", reason="bootstrap_research_transfer_outcome_requires_reconciliation")
        if self.state.get("transfer_error"):
            return dict(status="blocked", reason=self.state["transfer_error"])
        proof = self._legacy_repurchase_proof(obs)
        if proof:
            return dict(
                status="succeeded", reason="legacy logistics normally repurchased and researched", evidence=proof
            )
        epoch = self.state["legacy_repurchase"]
        auth = epoch["authorization"]
        item = "iron-plate"
        left = auth["repurchase_inputs"][item] - epoch["allocated_inputs"].get(item, 0)
        if left:
            if obs.get("inventory", {}).get(item, 0) < left:
                return self.bootstrap.ensure_item(obs, item, left)
            plan = self._startup_plan(obs)
            port = next(p for p in plan["ports"] if p.get("item") == item)
            return self._transfer(
                "legacy_repurchase_fund",
                dict(
                    type="insert",
                    name="wooden-chest",
                    position=port["position"],
                    inventory="chest",
                    item=item,
                    count=left,
                    reason="normally repurchase finite legacy logistics inputs after known chest replacement",
                ),
                epoch["id"],
            )
        if obs.get("research") not in (None, "logistics") and not obs.get("technologies", {}).get("logistics"):
            return dict(status="blocked", reason="another_research_is_running")
        if obs.get("research") != "logistics" and not obs.get("technologies", {}).get("logistics"):
            return dict(
                type="research", technology="logistics", reason="consume normally repurchased legacy machine science"
            )
        held = obs.get("inventory", {}).get("automation-science-pack", 0)
        credit = epoch["collected_packs"] - epoch["supplied_packs"]
        lab = next(p for p in auth["pins"] if p["name"] == "lab")
        if held and credit:
            return self._transfer(
                "legacy_repurchase_lab",
                dict(
                    type="insert",
                    name="lab",
                    position=lab["position"],
                    inventory="lab_input",
                    item="automation-science-pack",
                    count=min(held, credit),
                    reason="supply newly collected repurchase science",
                ),
                epoch["id"],
            )
        output = next(p for p in self._startup_plan(obs)["ports"] if p.get("direction") == "output")
        chest = self.find(obs, dict(name="wooden-chest", position=output["position"]))
        available = chest.get("inventory", {}).get("automation-science-pack", 0)
        remaining = auth["required_packs"] - epoch["collected_packs"]
        if available and remaining:
            return self._transfer(
                "legacy_repurchase_collect",
                dict(
                    type="take",
                    name="wooden-chest",
                    position=output["position"],
                    inventory="chest",
                    item="automation-science-pack",
                    count=min(available, remaining),
                    reason="collect newly made repurchase science",
                ),
                epoch["id"],
            )
        return dict(
            status="waiting",
            reason="waiting_for_new_legacy_repurchase_machine_science",
            evidence=self._legacy_repurchase_progress(obs),
        )

    def _legacy_repurchase_progress(self, obs):
        epoch = self.state["legacy_repurchase"]
        auth = epoch["authorization"]
        red = next(self.find(obs, p) for p in auth["pins"] if p.get("recipe") == "automation-science-pack")
        recipe = self.catalog.recipe_for_product("automation-science-pack")
        yield_count = sum(p["amount"] for p in recipe["products"] if p["name"] == "automation-science-pack")
        return dict(
            technology="logistics",
            normally_assembled_packs=(red["products_finished"] - auth["baseline"]["red"]) * yield_count,
            research_science_consumed=obs["production"]["automation-science-pack"]["consumed"]
            - auth["baseline"]["consumed"],
            original_allocated_inputs=deepcopy(self.state["allocated_inputs"]),
            repurchase_allocated_inputs=deepcopy(epoch["allocated_inputs"]),
            repurchase_id=epoch["id"],
            historical_goods_loss_quantity="unknown",
            **self._autonomous_wait_flags(obs),
        )

    def _legacy_repurchase_proof(self, obs):
        if (
            not obs.get("technologies", {}).get("logistics")
            or self._legacy_epoch_error(obs, historical=True)
            or self.state.get("pending_transfer")
            or self.state.get("transfer_error")
        ):
            return None
        evidence = self._legacy_repurchase_progress(obs)
        auth = self.state["legacy_repurchase"]["authorization"]
        if self.state["legacy_repurchase"]["allocated_inputs"] != auth["repurchase_inputs"]:
            return None
        if (
            evidence["normally_assembled_packs"] < auth["required_packs"]
            or evidence["research_science_consumed"] < auth["required_packs"]
        ):
            return None
        return dict(evidence, tick=obs["tick"], receipt_chain=auth["receipt_chain"], scope=auth["scope"])

    def _legacy_repurchase_monitor(self, obs):
        try:
            _, auth = self._legacy_repurchase_candidate(obs)
        except (KeyError, TypeError, ValueError, StopIteration):
            return None
        signature = _hash(auth)
        previous = self.state.get("legacy_repurchase_stall")
        if not previous or previous["signature"] != signature:
            self.state["legacy_repurchase_stall"] = dict(signature=signature, tick=obs["tick"])
            self._save()
            return None
        if obs["tick"] < previous["tick"]:
            return dict(status="blocked", reason="legacy_science_stall_tick_rollback")
        if obs["tick"] - previous["tick"] < DIVERSION_STALL_TICKS:
            return None
        proposal = self.prepare_legacy_repurchase(obs)
        if not proposal["ok"]:
            return dict(status="blocked", reason=proposal["reason"], evidence=proposal)
        self.state["legacy_repurchase"] = proposal["epoch"]
        self._save()
        return self._legacy_repurchase_step(obs)

    def _recovery_frame(self):
        frame = {
            key: deepcopy(self.state[key])
            for key in (
                "world_id",
                "catalog_fingerprint",
                "technology",
                "plan_hash",
                "required_packs",
                "required_inputs",
                "baseline_products_finished",
                "baseline_consumed",
                "machines",
                "red_machine",
                "ports",
                "lab",
            )
        }
        # Multi-machine/multi-lab bindings exist only in newer states, so an
        # older single-machine frame (and its hash) is unchanged.
        for key in ("red_machines", "labs"):
            if key in self.state:
                frame[key] = deepcopy(self.state[key])
        return frame

    def _red_pins(self):
        """Bound science machines; an older state binds exactly ``red_machine``."""
        return self.state.get("red_machines") or [self.state["red_machine"]]

    def _lab_pins(self):
        """Bound labs; an older state binds exactly ``lab``."""
        return self.state.get("labs") or [self.state["lab"]]

    def _finished(self, obs):
        """Summed ``products_finished`` of the bound science machines, or None if any is unreadable."""
        total = 0
        for pin in self._red_pins():
            value = (self.find(obs, pin) or {}).get("products_finished")
            if type(value) not in (int, float) or not math.isfinite(value):
                return None
            total += value
        return total

    def _replacement_inputs(self, gears):
        recipe = self.catalog.recipe_for_product("iron-gear-wheel")
        output = sum(
            p["amount"] for p in recipe["products"] if p["name"] == "iron-gear-wheel" and p.get("probability", 1) == 1
        )
        if type(output) is not int or output < 1 or {p["name"] for p in recipe["ingredients"]} != {"iron-plate"}:
            raise ValueError("deterministic normal gear replacement recipe required")
        amount = sum(p["amount"] for p in recipe["ingredients"])
        if type(amount) is not int or amount < 1:
            raise ValueError("positive integer normal gear inputs required")
        return {"iron-plate": math.ceil(gears / output) * amount}

    def _recovery_error(self):
        try:
            seen = set()
            total_gears = 0
            for epoch in self.state.get("diversion_recoveries", []):
                auth = epoch["authorization"]
                receipts = auth["receipts"]
                allocated = epoch["allocated_inputs"]
                if epoch["id"] != _hash(auth) or auth["frame"] != self._recovery_frame() or not receipts:
                    raise ValueError("recovery frame/hash changed")
                gears = 0
                for receipt in receipts:
                    if (
                        receipt["id"] in seen
                        or type(receipt["moved"]) is not int
                        or receipt["moved"] < 1
                        or any(
                            not isinstance(receipt[k], str) or len(receipt[k]) != 64
                            for k in ("id", "action_sha256", "result_sha256")
                        )
                    ):
                        raise ValueError("recovery receipt duplicated or invalid")
                    seen.add(receipt["id"])
                    gears += receipt["moved"]
                required = self._replacement_inputs(gears)
                if auth["replacement_inputs"] != required or set(allocated) - set(required):
                    raise ValueError("recovery budget changed")
                if any(type(n) is not int or not 0 <= n <= required[k] for k, n in allocated.items()):
                    raise ValueError("recovery funding exceeds audited allowance")
                total_gears += gears
            science = self.catalog.recipe_for_product("automation-science-pack")
            output = sum(p["amount"] for p in science["products"] if p["name"] == "automation-science-pack")
            required_gears = math.ceil(self.state["required_packs"] / output) * next(
                p["amount"] for p in science["ingredients"] if p["name"] == "iron-gear-wheel"
            )
            if total_gears > required_gears:
                raise ValueError("recovery exceeds original finite batch gear count")
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return "bootstrap_research_diversion_recovery_contract_changed"
        return None

    def prepare_diversion_recovery(self, obs):
        """Read-only audit proposal; step accepts it only after a native30s stall."""
        try:
            plan = self._startup_plan(obs)
            if not self.state or plan is None:
                raise ValueError("registered finite target required")
            error = self._binding_error(obs, plan)
            if error:
                raise ValueError(error)
            if self.state.get("pending_transfer") or self.state.get("transfer_error"):
                raise ValueError("unresolved target transfer")
            if any(self.state["allocated_inputs"].get(k, 0) != n for k, n in self.state["required_inputs"].items()):
                raise ValueError("original finite funding must finish first")
            job, funding, receipts = _audit_gear_diversions(Path(self.game.cfg.runtime_dir), self.state, plan, obs)
            if not receipts:
                return dict(ok=False, reason="no_new_audited_gear_diversion")
            gears = sum(r["moved"] for r in receipts)
            authorization = dict(
                frame=self._recovery_frame(),
                job_key=job,
                first_funding=funding,
                receipts=receipts,
                replacement_inputs=self._replacement_inputs(gears),
                observed_tick=obs["tick"],
                observed_progress=self._progress(obs),
            )
            epoch = dict(id=_hash(authorization), authorization=authorization, allocated_inputs={})
            trial = deepcopy(self.state)
            trial.setdefault("diversion_recoveries", []).append(epoch)
            prior = self.state
            try:
                self.state = trial
                error = self._recovery_error()
            finally:
                self.state = prior
            if error:
                raise ValueError(error)
            return dict(
                ok=True, recovery=epoch, diverted_gears=gears, replacement_inputs=authorization["replacement_inputs"]
            )
        except (OSError, KeyError, TypeError, ValueError, StopIteration) as exc:
            return dict(ok=False, reason="bootstrap_research_diversion_audit_unavailable", detail=str(exc))

    def _repair_funding(self, obs, plan):
        for epoch in self.state.get("diversion_recoveries", []):
            for item, total in epoch["authorization"]["replacement_inputs"].items():
                remaining = total - epoch["allocated_inputs"].get(item, 0)
                if remaining <= 0:
                    continue
                if obs.get("inventory", {}).get(item, 0) < remaining:
                    return self.bootstrap.ensure_item(obs, item, remaining)
                port = next(p for p in plan["ports"] if p["item"] == item)
                return self._transfer(
                    "repair_fund",
                    dict(
                        type="insert",
                        name="wooden-chest",
                        position=port["position"],
                        inventory="chest",
                        item=item,
                        count=remaining,
                        reason="fund audited diversion replacement raw inputs for " + self.technology,
                    ),
                    epoch["id"],
                )
        return None

    def _recover_stalled_diversion(self, obs, plan):
        gear = next(self.find(obs, p) for p in self.state["machines"] if p.get("recipe") == "iron-gear-wheel")
        reds = [self.find(obs, p) for p in self._red_pins()]

        def port(item):
            return next(
                self.find(obs, p)
                for p in self.state["ports"]
                if p["position"] == next(q["position"] for q in plan["ports"] if q.get("item") == item)
            )

        iron = port("iron-plate")
        # The hub cell buffers gears in the copper hub chest and the extended
        # cell in its gear relay chest: no planned chest may hold gears.
        chests = [self.find(obs, e) or {} for e in plan["entities"] if e["name"] == "wooden-chest"]
        recipe = self.catalog.recipe_for_product("automation-science-pack")
        yield_count = sum(p["amount"] for p in recipe["products"] if p["name"] == "automation-science-pack")
        short = (
            (self._finished(obs) - self.state["baseline_products_finished"]) * yield_count
            < self.state["required_packs"]
            and gear.get("inventory", {}).get("iron-gear-wheel", 0) == 0
            and gear.get("inventory", {}).get("iron-plate", 0) == 0
            and all(red.get("inventory", {}).get("iron-gear-wheel", 0) == 0 for red in reds)
            and all(chest.get("inventory", {}).get("iron-gear-wheel", 0) == 0 for chest in chests)
            and iron.get("inventory", {}).get("iron-plate", 0) == 0
        )
        if not short:
            return None
        finished = gear.get("products_finished")
        if type(finished) not in (int, float) or not math.isfinite(finished) or finished < 0:
            return None
        signature = _hash(dict(progress=self._progress(obs), gear_products_finished=finished))
        previous = self.state.get("diversion_stall")
        if not previous or previous["signature"] != signature:
            self.state["diversion_stall"] = dict(signature=signature, tick=obs["tick"])
            self._save()
            return None
        if obs["tick"] < previous["tick"]:
            return dict(status="blocked", reason="bootstrap_research_diversion_stall_tick_rollback")
        if obs["tick"] - previous["tick"] < DIVERSION_STALL_TICKS:
            return None
        proposal = self.prepare_diversion_recovery(obs)
        if not proposal["ok"]:
            return dict(
                status="waiting" if proposal["reason"] == "no_new_audited_gear_diversion" else "blocked",
                reason=proposal["reason"],
                evidence=proposal,
            )
        self.state.setdefault("diversion_recoveries", []).append(proposal["recovery"])
        self.state.pop("diversion_stall", None)
        self._save()
        return self._repair_funding(obs, plan)

    # Closed-loop input top-up (BOOTSTRAP_RESEARCH.md, closed-loop top-up).
    def _topups_unfunded(self):
        return any(
            e["allocated_inputs"].get(item, 0) < n
            for e in self.state.get("input_topups", [])
            for item, n in e["inputs"].items()
        )

    def _science_rules(self):
        """(pack yield, copper per cycle, gears per cycle, iron per gear cycle, gears per gear cycle)."""
        science = self.catalog.recipe_for_product(RED_PACK)
        gears = self.catalog.recipe_for_product("iron-gear-wheel")

        def amount(rows, item):
            return sum(p["amount"] for p in rows if p["name"] == item and p.get("probability", 1) == 1)

        rules = (
            amount(science["products"], RED_PACK),
            amount(science["ingredients"], "copper-plate"),
            amount(science["ingredients"], "iron-gear-wheel"),
            amount(gears["ingredients"], "iron-plate"),
            amount(gears["products"], "iron-gear-wheel"),
        )
        if any(type(n) is not int or n < 1 for n in rules):
            raise ValueError("deterministic red science and gear recipes required")
        return rules

    def _topup_inputs(self, cycles):
        """Raw plates for ``cycles`` science crafts, priced like ``requirements``."""
        _, copper, gears, iron, gear_yield = self._science_rules()
        return {"iron-plate": math.ceil(cycles * gears / gear_yield) * iron, "copper-plate": cycles * copper}

    def _cell_shortfall(self, obs, plan):
        """Packs this technology still needs from the cell, and packs the cell can still deliver.

        Needed is the bill minus packs already collected or waiting in an
        output, and never more than the research has left (units remaining
        from ``research_progress`` minus packs in the labs, in hand or in an
        output). Deliverable counts only complete crafts: each science machine
        on its own contents, plus the free plates and gears of the cell's
        chests and gear machine. An ingredient stranded in a machine without
        its partner is not deliverable.
        """
        pack_yield, copper, gears, iron, gear_yield = self._science_rules()

        def held(entity, item):
            value = (entity or {}).get("inventory", {}).get(item, 0)
            return int(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else 0

        count = self.state["required_packs"]
        reds = [self.find(obs, pin) for pin in self._red_pins()]
        gear = next(self.find(obs, p) for p in self.state["machines"] if p.get("recipe") == "iron-gear-wheel")
        chests = [self.find(obs, e) for e in plan["entities"] if e["name"] == "wooden-chest"]
        waiting = sum(held(e, RED_PACK) for e in chests + reds)
        in_labs = sum(held(self.find(obs, pin), RED_PACK) for pin in self._lab_pins())
        in_hand = obs.get("inventory", {}).get(RED_PACK, 0)
        in_hand = in_hand if type(in_hand) is int and in_hand > 0 else 0
        needed = count - self.state["collected_packs"] - waiting
        progress = obs.get("research_progress")
        if type(progress) in (int, float) and math.isfinite(progress) and 0 <= progress <= 1:
            left = count - math.floor(progress * count + 1e-6)
            needed = min(needed, left - in_labs - in_hand - waiting)
        cycles = sum(min(held(red, "copper-plate") // copper, held(red, "iron-gear-wheel") // gears) for red in reds)
        free_copper = sum(held(e, "copper-plate") for e in chests)
        free_iron = sum(held(e, "iron-plate") for e in chests) + held(gear, "iron-plate")
        free_gears = sum(held(e, "iron-gear-wheel") for e in chests + [gear]) + free_iron // iron * gear_yield
        cycles += min(free_copper // copper, free_gears // gears)
        return max(0, needed), cycles * pack_yield

    def _closed_loop_topup(self, obs, plan):
        """Top up the raw inputs of a stalled cell by its shortfall, never beyond one more bill.

        Runs only after the fixed bill and every audited repair are funded,
        while this technology researches. A shortfall must hold, with the
        cell and labs unchanged, for ``TOPUP_STALL_TICKS`` (inserter hands
        are not observed; a still cell has none in flight). The top-up buys
        whole crafts for the missing packs only, so a satisfied technology
        gets nothing and the total is capped at ``required_packs``.
        """
        state = self.state
        if (
            any(state["allocated_inputs"].get(k, 0) < n for k, n in state["required_inputs"].items())
            or any(
                e["allocated_inputs"] != e["authorization"]["replacement_inputs"]
                for e in state.get("diversion_recoveries", [])
            )
            or self._topups_unfunded()
            or obs.get("research") != self.technology
            or obs.get("technologies", {}).get(self.technology)
        ):
            return None
        try:
            needed, deliverable = self._cell_shortfall(obs, plan)
            pack_yield = self._science_rules()[0]
        except (KeyError, TypeError, ValueError, StopIteration):
            return None
        short = needed - deliverable
        if short <= 0:
            if state.pop("topup_stall", None) is not None:
                self._save()
            return None
        cell = [
            (e["name"], e["position"])
            for e in plan["entities"]
            if e["name"] in ("wooden-chest", "assembling-machine-1")
        ]
        signature = _hash(
            dict(
                progress=self._progress(obs),
                research_progress=obs.get("research_progress"),
                hand=obs.get("inventory", {}).get(RED_PACK, 0),
                cell=[
                    (
                        (self.find(obs, dict(name=n, position=p)) or {}).get("inventory"),
                        (self.find(obs, dict(name=n, position=p)) or {}).get("products_finished"),
                    )
                    for n, p in cell
                ],
                labs=[(self.find(obs, pin) or {}).get("inventory") for pin in self._lab_pins()],
            )
        )
        previous = state.get("topup_stall")
        if not previous or previous["signature"] != signature:
            state["topup_stall"] = dict(signature=signature, tick=obs["tick"])
            self._save()
            return None
        if obs["tick"] < previous["tick"]:
            return dict(status="blocked", reason="bootstrap_research_topup_stall_tick_rollback")
        if obs["tick"] - previous["tick"] < TOPUP_STALL_TICKS:
            return None
        cycles = math.ceil(short / pack_yield)
        packs = cycles * pack_yield
        used = sum(e["packs"] for e in state.get("input_topups", []))
        evidence = dict(needed_packs=needed, deliverable_packs=deliverable, topup_packs=packs, prior_topup_packs=used)
        if used + packs > state["required_packs"]:
            return dict(status="blocked", reason="bootstrap_research_topup_cap_reached", evidence=evidence)
        inputs = self._topup_inputs(cycles)
        epoch = dict(packs=packs, inputs=inputs, allocated_inputs={}, observed_tick=obs["tick"], **evidence)
        epoch["id"] = _hash(dict(epoch, sequence=len(state.get("input_topups", [])), signature=signature))
        state.setdefault("input_topups", []).append(epoch)
        state.pop("topup_stall", None)
        self._save()
        return self._topup_funding(obs, plan)

    def _topup_funding(self, obs, plan):
        for epoch in self.state.get("input_topups", []):
            for item in ("copper-plate", "iron-plate"):
                remaining = epoch["inputs"].get(item, 0) - epoch["allocated_inputs"].get(item, 0)
                if remaining <= 0:
                    continue
                if obs.get("inventory", {}).get(item, 0) < remaining:
                    return self.bootstrap.ensure_item(obs, item, remaining)
                port = next(p for p in plan["ports"] if p["item"] == item)
                return self._transfer(
                    "topup_fund",
                    dict(
                        type="insert",
                        name="wooden-chest",
                        position=port["position"],
                        inventory="chest",
                        item=item,
                        count=remaining,
                        reason="top up stalled machine science inputs by the shortfall for " + self.technology,
                    ),
                    epoch["id"],
                )
        return None

    def _plan_hash_ok(self, recorded, plan, *, historical):
        """Plan rule for a bound batch.

        A running, prewarmed or proving-to-continue batch must be bound to the
        cell's current plan. A finished batch (its technology natively
        researched, ``historical``) may instead be bound to a plan the cell
        grew from (listed in the board lineage); its bound entities are still
        checked one by one, and growth never removes or moves an entity.
        """
        if recorded == _hash(plan):
            return True
        lineage = getattr(self.board, "startup_lineage", None)
        return bool(historical and callable(lineage) and recorded in lineage())

    def _binding_error(self, obs, plan, *, historical=False):
        state = self.state
        if state.get("world_id") != obs["world_id"]:
            return "bootstrap_research_world_changed"
        if state.get("catalog_fingerprint") != self.catalog.fingerprint:
            return "bootstrap_research_catalog_changed"
        if state.get("technology") != self.technology:
            return "bootstrap_research_target_changed"
        if not self._plan_hash_ok(state.get("plan_hash"), plan, historical=historical):
            return "bootstrap_research_startup_plan_changed"
        for bound in self._red_pins():
            red = self.find(obs, bound)
            if not red or red.get("unit_number") != bound["unit_number"]:
                return "bootstrap_research_red_machine_unit_changed"
        for pin in state.get("machines", []) + state.get("ports", []) + self._lab_pins():
            actual = self.find(obs, pin)
            if not actual or actual.get("unit_number") != pin["unit_number"]:
                return "bootstrap_research_bound_entity_changed"
            if pin.get("recipe") and actual.get("recipe") != pin["recipe"]:
                return "bootstrap_research_machine_recipe_changed"
        # A batch prewarmed back to back has baselines at its owner's batch end,
        # which the counters reach only later: a rollback is a counter below
        # what was observed when the batch was reserved.
        warm = state.get("prewarm") or {}
        finished_floor = min(
            state["baseline_products_finished"],
            warm.get("observed_products_finished", state["baseline_products_finished"]),
        )
        consumed_floor = min(state["baseline_consumed"], warm.get("observed_consumed", state["baseline_consumed"]))
        finished = self._finished(obs)
        if finished is None or finished < finished_floor:
            return "bootstrap_research_machine_counter_rollback"
        consumed = obs.get("production", {}).get("automation-science-pack", {}).get("consumed")
        if type(consumed) not in (int, float) or not math.isfinite(consumed) or consumed < consumed_floor:
            return "bootstrap_research_science_counter_rollback"
        return self._recovery_error()

    def _startup_plan(self, obs):
        refresh = getattr(self.board, "refresh", None)
        if callable(refresh):
            refresh()
        board = self.board.state
        if board.get("world_id") != obs["world_id"] or board.get("catalog_fingerprint") != self.catalog.fingerprint:
            return None
        return board.get("blocks", {}).get("startup_science")

    def local_power_ready(self, obs):
        """Prove only this live science cell; never certify remote fuel flow."""
        plan = self._startup_plan(obs)
        if not plan:
            return False
        machines = [p for p in plan.get("entities", []) if p.get("name") == "assembling-machine-1"]
        count = science_machine_count(plan)
        if (
            count is None
            or len(machines) != 1 + count
            or {p.get("recipe") for p in machines} != {"iron-gear-wheel", "automation-science-pack"}
        ):
            return False
        for pin in machines:
            actual = self.find(obs, pin)
            if (
                not _powered(actual)
                or actual.get("recipe") != pin["recipe"]
                or type(actual.get("unit_number")) is not int
            ):
                return False
        return any(e.get("name") == "lab" and _powered(e) for e in obs.get("entities", []))

    def _capacity_path(self):
        return self.path.parent / CAPACITY_FILE

    def _saved_capacity(self, obs):
        path = self._capacity_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data.get("world_id") == obs["world_id"] else {}

    def _capacity_decision(self, obs, plan):
        """The research_capacity decision for this batch: computed once, then reused.

        It is kept in ``startup-science-capacity.json`` (one row per target),
        never in the bound batch state, so older states and their hashes are
        untouched. Missing evidence or catalog data returns None: keep the
        current size.
        """
        try:
            saved = self._saved_capacity(obs)
            row = saved.get("decisions", {}).get(self.technology)
            if row is not None:
                return row
            row = self._plan_capacity(obs, plan)
        except (OSError, AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
        if row is None:
            return None
        saved = dict(world_id=obs["world_id"], decisions=dict(saved.get("decisions", {})))
        saved["decisions"][self.technology] = row
        _atomic_json(self._capacity_path(), saved)
        return row

    def _plan_capacity(self, obs, plan):
        """Size science machines (a new cell, or a saved one-machine cell that can grow) and labs for this batch and the next red-only research.

        Evidence: held plates minus this batch's inputs (which are paid
        first), and plate rates from the observer's rate samples (a file
        read; no game query). The batch is pre-funded, so its packs consume
        no further plate rate and no materials cap applies.
        """
        from .progression import roadmap_wanted_technologies
        from .research_capacity import (
            ResearchCapacityConfig,
            ResearchHardware,
            ResearchMaterials,
            expansion_costs,
            plan_research_capacity,
            research_targets,
        )

        researched = {k for k, v in obs.get("technologies", {}).items() if v}
        names = [self.technology]
        ledger = self.path.parent / "jobs.json"
        jobs = []
        if ledger.exists():
            data = json.loads(ledger.read_text(encoding="utf-8"))
            if data.get("world_id") == obs["world_id"]:
                jobs = list(data.get("jobs", {}).values())
        # The queue is what this temporary cell will research: the upcoming
        # bootstrap steps (back-to-back batches), not red-only research the
        # permanent science serves later.
        served = bootstrap_technologies()
        for wanted in roadmap_wanted_technologies(jobs, researched):
            if len(names) > CAPACITY_QUEUE_HORIZON:
                break
            if (
                wanted not in names
                and wanted not in researched
                and wanted in served
                and _red_only_fixed(self.catalog, wanted)
            ):
                names.append(wanted)
        targets = research_targets(self.catalog, names)
        if not targets or targets[0].technology != self.technology:
            return None
        science = science_machine_count(plan) if plan else 1
        slots = lab_slot_entities(plan) if plan else [None] * (MAX_STARTUP_LABS - 1)
        built = [s for s in slots if s and _powered(self.find(obs, s["lab"]))]
        labs = 1 + len(built)
        costs = expansion_costs(self.catalog, RED_PACK, current_assemblers=science)
        # A saved one-machine cell can still grow by the west extension; its
        # bill is one machine and its five inserters (chests and pole are wood).
        growable = plan is not None and science_extension_error(plan) is None
        if growable:
            from .progression import raw_cost

            inserters = sum(e["name"] == "inserter" for e in science_extension_entities(plan))
            bill = raw_cost(self.catalog, {"assembling-machine-1": 1, "inserter": inserters})
            costs = dict(costs, assembler={k: round(v, 3) for k, v in bill.items()})
        _, inputs = self.requirements()
        rates = _sampled_rates(self.path.parent, obs["world_id"])
        evidence = {}
        for item in sorted(set(costs["assembler"]) | set(costs["lab"]) | set(inputs)):
            held = obs.get("inventory", {}).get(item, 0)
            row = {"available": max(0, held - inputs.get(item, 0))}
            if item in rates:
                produced, consumed = rates[item]
                row.update(produced_per_minute=produced, net_per_minute=produced - consumed)
            evidence[item] = row
        config = ResearchCapacityConfig(
            max_assemblers=MAX_STARTUP_SCIENCE_MACHINES if plan is None or growable else science,
            max_labs=1 + len(slots),
        )
        decision = plan_research_capacity(
            targets,
            ResearchHardware({RED_PACK: science}, {RED_PACK: costs["pack_rate_per_assembler"]}, labs),
            ResearchMaterials(
                evidence=evidence,
                assembler_cost={RED_PACK: costs["assembler"]},
                lab_cost=costs["lab"],
                machines_per_assembler={RED_PACK: costs["machines_per_assembler"]},
            ),
            config=config,
        )
        row = start_floor(decision.as_dict(), targets, costs["pack_rate_per_assembler"], config)
        row["decided_tick"] = obs["tick"]
        return row

    def _extend_labs(self, obs, plan):
        """Build the planned extra labs into the cell's lab slots before a new batch binds labs.

        Returns a builder action, or None to continue with the labs that are
        powered. A placement the builder reports as blocked is recorded and
        not retried for this target. The cell's bound machines, ports and
        plan hash are never changed: lab slots are a separate builder plan.
        """
        if _idle_powered_labs(obs) or permanent_science_due(self.path.parent, obs):
            return None
        decision = self._capacity_decision(obs, plan)
        if not decision or decision.get("lab_extension_blocked"):
            return None
        want = min(MAX_STARTUP_LABS, self._decided_labs(decision, plan)) - 1
        slots = lab_slot_entities(plan)[: max(0, want)]
        if not slots or all(self.find(obs, s["lab"]) for s in slots):
            return None
        result = self.builder.ensure_plan(obs, lab_slot_plan(plan, len(slots)))
        if result.get("type"):
            return result
        if result.get("status") == "succeeded":
            return None
        if result.get("status") not in ("blocked", "failed"):
            return result
        self._record_capacity_flag(obs, decision, "lab_extension_blocked", result.get("reason", "blocked"))
        return None

    @staticmethod
    def _decided_labs(decision, plan):
        """Target labs of ``decision`` for the cell ``plan`` actually has.

        When the decision wanted more science machines than the cell holds
        (the growth was refused or blocked), labs follow the best option
        without extra machines instead, so lab count never outruns pack supply.
        """
        from .research_capacity import labs_without_assemblers

        wanted = decision.get("target_assemblers")
        if type(wanted) is int and wanted > (science_machine_count(plan) or 1):
            return labs_without_assemblers(decision)
        return decision.get("target_labs", 1)

    def _record_capacity_flag(self, obs, decision, flag, reason):
        """Record a refusal on this target's saved decision so it is not retried for this technology."""
        saved = self._saved_capacity(obs)
        saved.setdefault("decisions", {}).setdefault(self.technology, dict(decision))
        saved["decisions"][self.technology][flag] = str(reason)[:200]
        saved["world_id"] = obs["world_id"]
        _atomic_json(self._capacity_path(), saved)

    def _extension_refusal(self, obs):
        """Why the shared science cell must not grow now, or None.

        Growth changes the cell plan, which every bound batch checks. It is
        allowed only between batches: no bootstrap batch state of this world
        may have an unresolved transfer, a prewarm whose technology is not yet
        researched, or a bound batch whose technology is not yet natively
        researched (funded-and-unfinished, including legacy logistics). An
        unreadable state refuses (fail closed).
        """
        return batch_refusal(self.path.parent, obs)

    def _grow_cell(self, obs, plan):
        """Grow a saved one-machine cell to two science machines before a new batch binds.

        Rules:
        - only when this target has no bound state, its decision wants more
          science machines than the cell has, the growth was not blocked
          before for this technology, and ``_extension_refusal`` is None;
        - the extension and the decision's extra labs are one builder plan,
          so assemblers and labs requested together are built together with
          real items from the actor's stock (the normal builder path);
        - a blocked or failed placement is recorded on the decision
          (``cell_extension_blocked``) and never retried for this technology;
          the batch then proceeds on the old cell;
        - only when every extension entity is observed is the board block
          replaced by the grown plan and the old hash added to the lineage
          (one atomic write). Nothing is removed or moved.

        Returns a builder action/status to return now, or None to continue
        with the (possibly grown) saved plan.
        """
        if permanent_science_due(self.path.parent, obs):  # the bus science block takes over: no transition tending
            return None
        decision = self._capacity_decision(obs, plan)
        if not decision or decision.get("cell_extension_blocked"):
            return None
        wanted = decision.get("target_assemblers")
        if type(wanted) is not int or wanted <= (science_machine_count(plan) or 1):
            return None
        if science_extension_error(plan) or self._extension_refusal(obs):
            return None
        grown = extended_science_cell(plan)
        want = min(MAX_STARTUP_LABS, decision.get("target_labs", 1)) - 1
        if _idle_powered_labs(obs):  # never add a lab while a built one stands idle
            want = 0
        slots = lab_slot_entities(grown)[: max(0, want)]
        entities = list(grown["entities"])
        if slots:
            entities += list(slots[0]["poles"]) + [slot["lab"] for slot in slots]
        build = _builder_plan(entities, [], template="startup_science_extension", layout_policy="pcb-block-v1")
        result = self.builder.ensure_plan(obs, build)
        if result.get("type"):
            return result
        if result.get("status") not in ("succeeded", "blocked", "failed"):
            return result
        if result.get("status") == "succeeded":
            missing = [e for e in science_extension_entities(plan) if not self.find(obs, e)]
            if not missing:
                committed = self.board.extend_startup_science(obs, plan, grown)
                if committed.get("ok"):
                    return None
                result = dict(status="blocked", reason=committed.get("reason"))
            else:
                result = dict(status="blocked", reason="science_cell_extension_not_observed_after_build")
        self._record_capacity_flag(obs, decision, "cell_extension_blocked", result.get("reason", "blocked"))
        return None

    def _batch_labs(self, obs, plan, labs):
        """Labs a new batch binds: one lab outside the slots (the starter lab) plus every powered slot lab."""
        slot_positions = [s["lab"]["position"] for s in lab_slot_entities(plan)]
        outside = [e for e in labs if e["position"] not in slot_positions]
        primary = outside[0] if outside else labs[0]
        extra = [e for position in slot_positions for e in labs if e["position"] == position and e is not primary]
        return [primary] + extra[: MAX_STARTUP_LABS - 1]

    def prebuild(self, obs):
        """Early start of the science cell while its unlock research runs (PLANNERS.md "Early start").

        Rules:
        - only for a batch without state (never bound, funded or legacy),
          while ``CELL_UNLOCK_TECHNOLOGY`` is the running research and not yet
          researched, and never for a retired cell;
        - the cell is reserved and sized exactly as the batch would size it
          (``_capacity_decision``: science machines and labs for the upcoming
          bootstrap queue), decided now, up front, instead of after the unlock;
        - every cell entity and the decided slot labs are crafted and placed
          except those whose recipe the running research unlocks (the
          assemblers; ``early_start.Deferral``), which go last in the build
          order so inserters, chests and poles do not wait behind them;
        - a blocked or failed placement is recorded once (``prebuild_blocked``)
          and the batch builds the rest itself after the unlock.

        Returns a builder action, or None. Binding, funding and research
        happen only in ``step`` once the unlock is researched.
        """
        from .early_start import Deferral
        from .science_retirement import dismantled

        unlock = CELL_UNLOCK_TECHNOLOGY
        technologies = obs.get("technologies", {})
        if (
            self.state
            or self._is_legacy()
            or obs.get("research") != unlock
            or technologies.get(unlock)
            or technologies.get(self.technology)
            or not obs.get("world_id")
            or dismantled(self.path.parent, obs["world_id"])
        ):
            return None
        plan = self._startup_plan(obs)
        if plan is None:
            decision = self._capacity_decision(obs, None)
            machines = min(MAX_STARTUP_SCIENCE_MACHINES, (decision or {}).get("target_assemblers", 1))
            if machines > 1:
                plan = self.board.reserve_startup_science(obs, science_machines=machines)
            else:
                plan = self.board.reserve_startup_science(obs)
            if not plan.get("ok"):
                return None
        decision = self._capacity_decision(obs, plan)
        if decision and decision.get("prebuild_blocked"):
            return None
        want = min(MAX_STARTUP_LABS, self._decided_labs(decision, plan)) - 1 if decision else 0
        entities = sorted(plan["entities"], key=lambda e: e["name"] == "assembling-machine-1")
        slots = lab_slot_entities(plan)[: max(0, want)]
        if slots:
            entities += list(slots[0]["poles"]) + [slot["lab"] for slot in slots]
        build = _builder_plan(entities, [], template="startup_science_prebuild", layout_policy="pcb-block-v1")
        result = self.builder.ensure_plan(obs, build, defer=Deferral(self.catalog, obs, coming={unlock}))
        if result.get("type"):
            return result
        if result.get("status") in ("blocked", "failed") and decision:
            self._record_capacity_flag(obs, decision, "prebuild_blocked", result.get("reason", "blocked"))
        return None

    def _research_step(self, obs):
        name = self.technology
        researched = obs.get("technologies", {}).get(name)
        if self.state:
            for field, expected, reason in (
                ("world_id", obs["world_id"], "bootstrap_research_world_changed"),
                ("catalog_fingerprint", self.catalog.fingerprint, "bootstrap_research_catalog_changed"),
                ("technology", name, "bootstrap_research_target_changed"),
            ):
                if self.state.get(field) != expected:
                    return {"status": "blocked", "reason": reason}
        if not self.state and researched:
            return {
                "status": "succeeded",
                "reason": "bootstrap target already natively researched",
                "evidence": self._research_proof(obs),
            }
        reserved = self._prewarmed_cell_guard(obs)
        if reserved:
            return reserved
        try:
            count, inputs = self.requirements()
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            return {
                "status": "blocked",
                "reason": "bootstrap_research_rules_unsupported",
                "evidence": {"technology": name, "detail": str(exc)},
            }
        tech = self.catalog.technologies[name]
        missing = [p for p in tech.get("prerequisites", []) if not obs.get("technologies", {}).get(p)]
        if not obs.get("technologies", {}).get("automation"):
            missing.append("automation")
        if missing:
            return {
                "status": "blocked",
                "reason": "bootstrap_research_native_prerequisites_missing",
                "evidence": {"missing_technologies": sorted(set(missing))},
            }
        if tech.get("enabled") is False:
            return {"status": "blocked", "reason": "bootstrap_research_technology_disabled"}
        recipes = {
            self.catalog.recipe_for_product(item)["name"] for item in ("iron-gear-wheel", "automation-science-pack")
        }
        recipes.update(("assembling-machine-1", "inserter", "lab"))
        locked = sorted(r for r in recipes if not obs.get("enabled_recipes", {}).get(r))
        if locked:
            return {
                "status": "blocked",
                "reason": "bootstrap_research_native_recipes_locked",
                "evidence": {"missing_recipes": locked},
            }
        if obs.get("research") not in (None, name):
            return {
                "status": "blocked",
                "reason": "another_research_is_running",
                "evidence": {"technology": obs["research"]},
            }
        plan = self._startup_plan(obs)
        from .science_retirement import dismantled

        if dismantled(self.path.parent, obs["world_id"]):  # an obsolete cell is never rebuilt
            return {"status": "blocked", "reason": "bootstrap_research_startup_cell_retired"}
        if plan is None:
            if self.state:
                return {"status": "blocked", "reason": "bootstrap_research_registered_cell_missing"}
            # A new cell is sized once, from this batch and the next red-only
            # roadmap research; a saved cell keeps its machines forever.
            decision = self._capacity_decision(obs, None)
            machines = min(MAX_STARTUP_SCIENCE_MACHINES, (decision or {}).get("target_assemblers", 1))
            if machines > 1:
                plan = self.board.reserve_startup_science(obs, science_machines=machines)
            else:
                plan = self.board.reserve_startup_science(obs)
            if not plan.get("ok"):
                needs = {"plan_needs": plan["plan_needs"]} if plan.get("plan_needs") else {}
                return {
                    "status": "blocked",
                    "reason": plan.get("reason", "bootstrap_science_reservation_failed"),
                    **needs,
                }
        if self.state:
            error = self._binding_error(obs, plan, historical=bool(researched))
            if error:
                return {"status": "blocked", "reason": error}
            if self.state.get("required_packs") != count or self.state.get("required_inputs") != inputs:
                return {"status": "blocked", "reason": "bootstrap_research_funding_contract_changed"}
            if self.state.get("pending_transfer"):
                return {"status": "blocked", "reason": "bootstrap_research_transfer_outcome_requires_reconciliation"}
            if self.state.get("transfer_error"):
                return {"status": "blocked", "reason": self.state["transfer_error"]}
        else:
            # Between batches a saved one-machine cell may grow first; the new
            # batch then binds the grown plan and all its science machines.
            growth = self._grow_cell(obs, plan)
            if growth is not None:
                return growth
            plan = self._startup_plan(obs) or plan
        result = self.builder.ensure_plan(obs, plan)
        if result.get("status") != "succeeded":
            return result
        science = science_machine_count(plan)
        if science is None:
            return {"status": "blocked", "reason": "bootstrap_research_startup_plan_unsupported"}
        machines = [self.find(obs, e) for e in plan["entities"] if e["name"] == "assembling-machine-1"]
        if len(machines) != 1 + science or any(not _powered(e) for e in machines):
            return {"status": "blocked", "reason": "science_cell_power_not_connected"}
        reds = [e for e in machines if e.get("recipe") == "automation-science-pack"]
        gear = next((e for e in machines if e.get("recipe") == "iron-gear-wheel"), None)
        if len(reds) != science or gear is None:
            return {"status": "blocked", "reason": "bootstrap_research_machine_recipe_changed"}
        labs = [e for e in obs["entities"] if e["name"] == "lab" and _powered(e)]
        if not labs:
            return {"status": "blocked", "reason": "powered_research_lab_missing"}
        if not self.state:
            extension = self._extend_labs(obs, plan)
            if extension is not None:
                return extension
            labs = self._batch_labs(obs, plan, labs)
            ports = [self.find(obs, {"name": "wooden-chest", "position": p["position"]}) for p in plan["ports"]]
            pins = machines + ports + labs
            if any(not e or type(e.get("unit_number")) is not int or e["unit_number"] < 1 for e in pins):
                return {"status": "blocked", "reason": "bootstrap_research_native_entity_identity_required"}
            finished = [red.get("products_finished") for red in reds]
            consumed = obs.get("production", {}).get("automation-science-pack", {}).get("consumed")
            if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in finished + [consumed]):
                return {"status": "blocked", "reason": "bootstrap_research_native_baseline_required"}

            def pin(e):
                return {k: deepcopy(e[k]) for k in ("name", "position", "unit_number", "recipe") if k in e}

            self.state = dict(
                schema_version=1,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                technology=name,
                plan_hash=_hash(plan),
                required_packs=count,
                required_inputs=inputs,
                allocated_inputs={},
                red_machine=pin(reds[0]),
                machines=[pin(e) for e in machines],
                ports=[pin(e) for e in ports],
                lab=pin(labs[0]),
                baseline_products_finished=sum(finished),
                baseline_consumed=consumed,
                collected_packs=0,
                supplied_packs=0,
                transfer_sequence=0,
            )
            # Only multi-machine/multi-lab batches carry the list bindings; a
            # one-machine, one-lab state keeps the original shape.
            if len(reds) > 1:
                self.state["red_machines"] = [pin(e) for e in reds]
            if len(labs) > 1:
                self.state["labs"] = [pin(e) for e in labs]
            self._save()
        proof = self._research_proof(obs)
        if proof:
            return {
                "status": "succeeded",
                "reason": "finite machine-built science naturally researched the target",
                "evidence": proof,
            }
        funding_wait = None
        if self.state.get("prewarm"):
            # A prewarmed batch starts its research on the packs it already
            # paid for; a missing remainder is a plate wait, not a reason to
            # leave the labs idle (back-to-back batches).
            balanced = self._balanced_funding(obs, acquire=True)
            if balanced and balanced.get("type"):
                return balanced
            if balanced:
                allocated = self.state["allocated_inputs"]
                needs = {k: n - allocated.get(k, 0) for k, n in inputs.items() if n > allocated.get(k, 0)}
                funding_wait = dict(balanced, evidence={**balanced.get("evidence", {}), "needs": needs})
        else:
            funding = self._incremental_funding(obs, plan, inputs, name)
            if funding is not None and funding.get("type"):
                return funding
            funding_wait = funding
        repair = self._repair_funding(obs, plan) or self._topup_funding(obs, plan)
        if repair:
            return repair
        if researched:
            return {
                "status": "waiting",
                "reason": "bootstrap_target_researched_waiting_for_fresh_machine_proof",
                "evidence": self._progress(obs),
            }
        if obs.get("research") != name:
            return {
                "type": "research",
                "technology": name,
                "reason": "consume normally machine-produced bootstrap science",
            }
        held = obs.get("inventory", {}).get("automation-science-pack", 0)
        collected = self.state["collected_packs"]
        supplied = self.state["supplied_packs"]
        lab_pins = self._lab_pins()
        if held and collected > supplied:
            pending = collected - supplied
            # Spread carried packs over the bound labs: the emptiest lab gets
            # an even share, never fewer than one courier batch per action.
            share = max(SCIENCE_COURIER_BATCH, math.ceil(pending / len(lab_pins)))
            target = min(
                enumerate(lab_pins),
                key=lambda row: (
                    (self.find(obs, row[1]) or {}).get("inventory", {}).get("automation-science-pack", 0),
                    row[0],
                ),
            )[1]
            return self._transfer(
                "lab",
                dict(
                    type="insert",
                    name="lab",
                    position=target["position"],
                    inventory="lab_input",
                    item="automation-science-pack",
                    count=min(held, pending, share),
                    reason="supply collected machine-built bootstrap science",
                ),
            )
        # An extended cell has one output chest per science machine: wait on
        # their total and take from the fullest (first on ties).
        stocked = []
        for port in (p for p in plan["ports"] if p["direction"] == "output"):
            output = self.find(obs, {"name": "wooden-chest", "position": port["position"]})
            stocked.append((output.get("inventory", {}).get("automation-science-pack", 0), port))
        available, port = max(stocked, key=lambda row: row[0])
        total = sum(row[0] for row in stocked)
        # Carrying each pack as it is made costs two actions per pack. Let a
        # small batch per lab pile up; the last few packs are still fetched together.
        batch = SCIENCE_COURIER_BATCH * len(lab_pins)
        if available and total >= min(batch, count - collected) and collected < count:
            return self._transfer(
                "collect",
                dict(
                    type="take",
                    name="wooden-chest",
                    position=port["position"],
                    inventory="chest",
                    item="automation-science-pack",
                    count=min(available, count - collected),
                    reason="collect owned machine-built bootstrap science",
                ),
            )
        if funding_wait is not None:
            # Partly funded: the machines already work on what arrived; an
            # empty cell here is the plate wait, not a diversion.
            return dict(funding_wait, evidence={**self._progress(obs), **funding_wait.get("evidence", {})})
        repair = self._recover_stalled_diversion(obs, plan)
        if repair and (repair.get("type") or repair.get("status") != "waiting"):
            return repair
        topup = self._closed_loop_topup(obs, plan)
        if topup:
            return topup
        if repair:
            return repair
        return {
            "status": "waiting",
            "reason": "waiting_for_finite_machine_science_and_native_research",
            "evidence": self._progress(obs),
        }

    def _incremental_funding(self, obs, plan, inputs, name):
        """Fund the batch as plates arrive (PLANNERS.md "Bootstrap research", incremental funding).

        Returns an insert of what the hands hold (at least ``FUND_CHUNK`` or
        the whole remainder, never more than the remaining allowance), else a
        collection action, else a waiting status naming every unfunded item
        in ``evidence.needs`` (the critical-path claim), or None once funded.
        The accounting is the same ``fund`` transfer receipt as before:
        ``allocated_inputs`` grows by the moved amount and never exceeds
        ``required_inputs``.
        """
        allocated = self.state["allocated_inputs"]
        pending = [(item, total - allocated.get(item, 0)) for item, total in inputs.items()]
        pending = [(item, remaining) for item, remaining in pending if remaining > 0]
        if not pending:
            return None
        inventory = obs.get("inventory", {})
        for item, remaining in pending:
            held = inventory.get(item, 0)
            if type(held) is int and held >= min(remaining, FUND_CHUNK):
                port = next(p for p in plan["ports"] if p["item"] == item)
                return self._transfer(
                    "fund",
                    dict(
                        type="insert",
                        name="wooden-chest",
                        position=port["position"],
                        inventory="chest",
                        item=item,
                        count=min(held, remaining),
                        reason="fund one finite machine research batch for " + name,
                    ),
                )
        needs = {item: remaining for item, remaining in pending}
        wait = None
        for item, remaining in pending:
            result = self.bootstrap.ensure_item(obs, item, remaining)
            if result.get("type"):
                return result
            wait = wait or result
        reason = str((wait or {}).get("reason") or "")
        if not reason.startswith("waiting"):
            reason = "waiting for plates to fund the batch: " + ", ".join(needs)
        return {"status": "waiting", "reason": reason, "evidence": {**(wait or {}).get("evidence", {}), "needs": needs}}

    def _transfer(self, kind, action, recovery_id=None):
        self.state["transfer_sequence"] = self.state.get("transfer_sequence", 0) + 1
        self.state["pending_transfer"] = dict(
            kind=kind, action=deepcopy(action), sequence=self.state["transfer_sequence"]
        )
        if recovery_id:
            self.state["pending_transfer"]["recovery_id"] = recovery_id
        self._save()
        return action

    def record_action(self, action, receipt):
        """Acknowledge only this target's exact known ordinary transfer receipt."""
        pending = self.state.get("pending_transfer")
        if not pending or pending["action"] != action:
            return
        moved = receipt.get("moved")
        if receipt.get("ok") is not True or type(moved) is not int or not 0 <= moved <= action["count"]:
            raise ValueError("bootstrap research transfer outcome unknown; retain durable intent")
        kind = pending["kind"]
        item = action["item"]
        if kind == "fund":
            self.state["allocated_inputs"][item] = self.state["allocated_inputs"].get(item, 0) + moved
        elif kind in ("legacy_repurchase_fund", "legacy_repurchase_collect", "legacy_repurchase_lab"):
            epoch = self.state.get("legacy_repurchase", {})
            auth = epoch.get("authorization", {})
            if (
                epoch.get("id") != pending.get("recovery_id")
                or epoch.get("id") != _hash(auth)
                or auth.get("original") != self._legacy_original()
            ):
                raise ValueError("legacy repurchase funding contract changed; retain durable intent")
            if kind == "legacy_repurchase_fund":
                total = epoch["allocated_inputs"].get(item, 0) + moved
                if total > auth["repurchase_inputs"].get(item, 0):
                    raise ValueError("legacy repurchase funding exceeds finite budget")
                epoch["allocated_inputs"][item] = total
            elif kind == "legacy_repurchase_collect":
                epoch["collected_packs"] += moved
            else:
                epoch["supplied_packs"] += moved
        elif kind == "repair_fund":
            epoch = next(
                (e for e in self.state.get("diversion_recoveries", []) if e["id"] == pending.get("recovery_id")), None
            )
            if epoch is None or self._recovery_error():
                raise ValueError("audited repair funding contract changed; retain durable intent")
            total = epoch["allocated_inputs"].get(item, 0) + moved
            if total > epoch["authorization"]["replacement_inputs"].get(item, 0):
                raise ValueError("repair funding exceeds audited allowance")
            epoch["allocated_inputs"][item] = total
        elif kind == "topup_fund":
            epoch = next((e for e in self.state.get("input_topups", []) if e["id"] == pending.get("recovery_id")), None)
            if epoch is None:
                raise ValueError("closed-loop top-up contract changed; retain durable intent")
            total = epoch["allocated_inputs"].get(item, 0) + moved
            if total > epoch["inputs"].get(item, 0):
                raise ValueError("top-up funding exceeds its shortfall allowance")
            epoch["allocated_inputs"][item] = total
        elif kind == "collect":
            self.state["collected_packs"] += moved
        elif kind == "lab":
            self.state["supplied_packs"] += moved
        else:
            raise ValueError("unknown bootstrap research transfer kind")
        self.state["last_transfer"] = dict(sequence=pending["sequence"], kind=kind, item=item, moved=moved)
        self.state.pop("pending_transfer")
        if not moved:
            self.state["transfer_error"] = "bootstrap_research_normal_transfer_moved_zero"
        self._save()

    def production_complete(self, obs):
        """Pure ownership boundary for sharing the cell after all packs were supplied."""
        if not self.state or not obs.get("ok", True) or not obs.get("world_id"):
            return None
        if self._is_legacy():
            epoch = self.state.get("legacy_repurchase")
            if (
                not epoch
                or self._legacy_epoch_error(obs)
                or "pending_transfer" in self.state
                or self.state.get("transfer_error")
            ):
                return None
            auth = epoch["authorization"]
            count = auth["required_packs"]
            if (
                epoch["allocated_inputs"] != auth["repurchase_inputs"]
                or epoch["collected_packs"] != count
                or epoch["supplied_packs"] != count
            ):
                return None
            pin = next(p for p in auth["pins"] if p.get("recipe") == "automation-science-pack")
            red = self.find(obs, pin)
            output = sum(
                q["amount"]
                for q in self.catalog.recipe_for_product("automation-science-pack")["products"]
                if q["name"] == "automation-science-pack"
            )
            packs = (red["products_finished"] - auth["baseline"]["red"]) * output
            if packs < count:
                return None
            return dict(
                technology=self.technology,
                world_id=obs["world_id"],
                catalog_fingerprint=self.catalog.fingerprint,
                owner_guard=epoch["id"],
                red_unit=red["unit_number"],
                products_finished=red["products_finished"],
                normally_assembled_packs=packs,
                collected_packs=count,
                supplied_packs=count,
            )
        plan = self._startup_plan(obs)
        if (
            plan is None
            or self._binding_error(obs, plan)
            or "pending_transfer" in self.state
            or self.state.get("transfer_error")
        ):
            return None
        count = self.state["required_packs"]
        if self.state.get("collected_packs") != count or self.state.get("supplied_packs") != count:
            return None
        if self.state["allocated_inputs"] != self.state["required_inputs"]:
            return None
        if (
            any(
                e["allocated_inputs"] != e["authorization"]["replacement_inputs"]
                for e in self.state.get("diversion_recoveries", [])
            )
            or self._topups_unfunded()
        ):
            return None
        if any(not _powered(self.find(obs, p)) for p in self.state["machines"] + self._lab_pins()):
            return None
        red = self.find(obs, self.state["red_machine"])
        finished = self._finished(obs)
        recipe = self.catalog.recipe_for_product("automation-science-pack")
        amount = sum(p["amount"] for p in recipe["products"] if p["name"] == "automation-science-pack")
        packs = (finished - self.state["baseline_products_finished"]) * amount
        if packs < count:
            return None
        return dict(
            technology=self.technology,
            world_id=obs["world_id"],
            catalog_fingerprint=self.catalog.fingerprint,
            owner_guard=_hash(self._recovery_frame()),
            red_unit=red["unit_number"],
            products_finished=finished,
            normally_assembled_packs=packs,
            collected_packs=count,
            supplied_packs=count,
        )

    def funding_complete(self, obs):
        """Ownership boundary for funding the next batch while this one still runs (back-to-back batches).

        This batch is bound to the current cell, has no unresolved transfer,
        holds its whole finite allowance (and every audited repair) and its
        machines and labs are powered. Its packs may still be in production:
        the next batch's counter baselines are this batch's end, computed
        (``products_finished`` = baseline + the cycles this batch paid for,
        ``consumed`` = baseline + its packs), never the counters observed
        while this batch still runs. Legacy states keep ``production_complete``.
        """
        if self._is_legacy():
            return self.production_complete(obs)
        if not self.state or not obs.get("ok", True) or not obs.get("world_id"):
            return None
        plan = self._startup_plan(obs)
        if (
            plan is None
            or self._binding_error(obs, plan)
            or "pending_transfer" in self.state
            or self.state.get("transfer_error")
            or self.state["allocated_inputs"] != self.state["required_inputs"]
            or any(
                e["allocated_inputs"] != e["authorization"]["replacement_inputs"]
                for e in self.state.get("diversion_recoveries", [])
            )
            or self._topups_unfunded()
            or any(not _powered(self.find(obs, p)) for p in self.state["machines"] + self._lab_pins())
        ):
            return None
        recipe = self.catalog.recipe_for_product("automation-science-pack")
        amount = sum(p["amount"] for p in recipe["products"] if p["name"] == "automation-science-pack")
        count = self.state["required_packs"]
        red = self.find(obs, self.state["red_machine"])
        return dict(
            technology=self.technology,
            world_id=obs["world_id"],
            catalog_fingerprint=self.catalog.fingerprint,
            owner_guard=_hash(self._recovery_frame()),
            red_unit=red["unit_number"],
            products_finished=self.state["baseline_products_finished"] + math.ceil(count / amount),
            consumed=self.state["baseline_consumed"] + count,
        )

    def _prewarmed_cell_guard(self, obs):
        """Refuse this batch while another target holds an unfinished prewarm of the shared cell.

        Any bootstrap target may be prewarmed (back-to-back batches); only
        its exact owner batch, or the prewarmed target itself, may use the
        cell until that target is natively researched.
        """
        root = self.path.parent
        paths = sorted(root.glob("bootstrap-research-*.json")) + [root / "bootstrap-logistics.json"]
        for path in paths:
            if path == self.path or not path.exists():
                continue
            try:
                target = json.loads(path.read_text(encoding="utf-8"))
                warm = target.get("prewarm")
                if not warm:
                    continue
                if (
                    target.get("world_id") != obs["world_id"]
                    or target.get("catalog_fingerprint") != self.catalog.fingerprint
                ):
                    return dict(status="blocked", reason="science_prewarm_reserved_cell_identity_changed")
                technology = target.get("technology", "logistics")
                if obs.get("technologies", {}).get(technology):
                    continue
                if (
                    self.technology == warm["owner_technology"]
                    and self.state
                    and _hash(self._recovery_frame()) == warm["owner_guard"]
                ):
                    continue
                return dict(status="blocked", reason="science_cell_reserved_for_approved_prewarmed_" + technology)
            except (OSError, KeyError, TypeError, ValueError):
                return dict(status="blocked", reason="science_prewarm_reserved_cell_identity_changed")
        return None

    def _autonomous_wait_flags(self, obs):
        baseline = (
            self.state.get("legacy_repurchase", {})
            .get("authorization", {})
            .get("baseline", {})
            .get("consumed", self.state.get("baseline_consumed"))
        )
        consumed = obs.get("production", {}).get("automation-science-pack", {}).get("consumed")
        safe = bool(
            self.production_complete(obs)
            and obs.get("research") == self.technology
            and not obs.get("technologies", {}).get(self.technology)
            and type(consumed) in (int, float)
            and math.isfinite(consumed)
            and type(baseline) in (int, float)
            and consumed > baseline
        )
        return dict(autonomous_wait=safe, background_work_safe=safe)

    def _balanced_funding(self, obs, *, surplus_budget=None, acquire=False):
        """At most five science packs per packet; copper first, actual ACK only."""
        required = self.state["required_inputs"]
        allocated = self.state["allocated_inputs"]
        count = self.state["required_packs"]
        if all(allocated.get(k, 0) >= n for k, n in required.items()):
            return None
        budget = (
            {k: max(0, n - allocated.get(k, 0)) for k, n in required.items()}
            if surplus_budget is None
            else surplus_budget
        )
        warm = self.state["prewarm"]
        level = warm.get("packet_level", 0)
        need = lambda item, n: max(0, math.ceil(required[item] * n / count) - allocated.get(item, 0))
        if not level or all(need(k, level) == 0 for k in required):
            completed = min(allocated.get(k, 0) * count // n for k, n in required.items())
            # Whole packets only (PREWARM_PACKET packs, or the remainder).
            level = min(count, completed + PREWARM_PACKET)
            if not all(need(k, level) <= budget.get(k, 0) for k in required):
                level = None
            if level is None:
                return dict(status="waiting", reason="science_prewarm_balanced_surplus_not_available")
            warm["packet_level"] = level
            self._save()
        candidates = [k for k in required if need(k, level) > 0]
        item = min(candidates, key=lambda k: (allocated.get(k, 0) / required[k], 0 if k == "copper-plate" else 1))
        amount = min(need(item, level), budget.get(item, 0))
        if amount <= 0:
            return dict(status="waiting", reason="science_prewarm_reserved_raw_unavailable", evidence={"item": item})
        held = obs.get("inventory", {}).get(item, 0)
        if held > 0:
            port = next(p for p in self._startup_plan(obs)["ports"] if p.get("item") == item)
            return self._transfer(
                "fund",
                dict(
                    type="insert",
                    name="wooden-chest",
                    position=port["position"],
                    inventory="chest",
                    item=item,
                    count=min(amount, held),
                    reason="fund balanced prewarmed machine science for " + self.technology,
                ),
            )
        if acquire:
            return self.bootstrap.ensure_item(obs, item, amount)
        output = self.bootstrap._take_output(obs, item, amount)
        return (
            output
            if output is not None
            else dict(status="waiting", reason="science_prewarm_existing_output_not_available", evidence={"item": item})
        )

    def prewarm(self, obs, *, owner, surplus_budget):
        """Fund the approved next batch without changing research or lab contents."""
        if (
            not isinstance(surplus_budget, dict)
            or set(surplus_budget) - {"iron-plate", "copper-plate"}
            or any(type(n) is not int or not 0 <= n <= 1000000 for n in surplus_budget.values())
        ):
            raise ValueError("prewarm requires bounded nonnegative raw-plate surplus")
        completed = owner.funding_complete(obs)
        if (
            not completed
            or owner.technology == self.technology
            or obs.get("research") != owner.technology
            or obs.get("technologies", {}).get(owner.technology)
            or obs.get("technologies", {}).get(self.technology)
        ):
            return dict(status="waiting", reason="science_prewarm_owner_not_consuming_completed_batch")
        consumed = obs.get("production", {}).get("automation-science-pack", {}).get("consumed")
        if (
            type(consumed) not in (int, float)
            or not math.isfinite(consumed)
            or consumed <= owner.state["baseline_consumed"]
        ):
            return dict(status="waiting", reason="science_prewarm_actual_owner_consumption_required")
        try:
            count, inputs = self.requirements()
            tech = self.catalog.technologies[self.technology]
            missing = [
                t
                for t in tech.get("prerequisites", [])
                if not obs.get("technologies", {}).get(t) and t != owner.technology
            ]
            recipes = {
                self.catalog.recipe_for_product(item)["name"] for item in ("iron-gear-wheel", "automation-science-pack")
            }
            if (
                missing
                or not obs.get("technologies", {}).get("automation")
                or tech.get("enabled") is False
                or any(not obs.get("enabled_recipes", {}).get(r) for r in recipes)
            ):
                return dict(
                    status="waiting",
                    reason="science_prewarm_native_prerequisites_missing",
                    evidence={"missing": missing},
                )
            plan = self._startup_plan(obs)
            if plan is None or _hash(plan) != owner.state["plan_hash"]:
                return dict(status="blocked", reason="science_prewarm_shared_cell_changed")
            if not self.state:
                # The baselines are the owner's batch end (``funding_complete``),
                # so the owner's plates still in the cell never count for this
                # batch and it may be reserved while the owner still produces.
                if not any(surplus_budget.get(k, 0) > 0 for k in inputs):
                    return dict(status="waiting", reason="science_prewarm_surplus_not_available")
                finished = owner._finished(obs) if not owner._is_legacy() else completed["products_finished"]
                self.state = dict(
                    schema_version=1,
                    world_id=obs["world_id"],
                    catalog_fingerprint=self.catalog.fingerprint,
                    technology=self.technology,
                    plan_hash=_hash(plan),
                    required_packs=count,
                    required_inputs=inputs,
                    allocated_inputs={},
                    red_machine=deepcopy(owner.state["red_machine"]),
                    machines=deepcopy(owner.state["machines"]),
                    ports=deepcopy(owner.state["ports"]),
                    lab=deepcopy(owner.state["lab"]),
                    baseline_products_finished=max(completed["products_finished"], finished),
                    baseline_consumed=max(completed.get("consumed", consumed), consumed),
                    collected_packs=0,
                    supplied_packs=0,
                    transfer_sequence=0,
                    prewarm=dict(
                        owner_technology=owner.technology,
                        owner_guard=completed["owner_guard"],
                        red_unit=completed["red_unit"],
                        packet_level=0,
                        reserved_tick=obs["tick"],
                        observed_products_finished=finished,
                        observed_consumed=consumed,
                    ),
                )
                # The prewarmed batch shares the owner's exact bindings.
                for key in ("red_machines", "labs"):
                    if key in owner.state:
                        self.state[key] = deepcopy(owner.state[key])
                self._save()
            warm = self.state.get("prewarm")
            if (
                not warm
                or warm["owner_technology"] != owner.technology
                or warm["owner_guard"] != completed["owner_guard"]
                or warm["red_unit"] != completed["red_unit"]
            ):
                return dict(status="blocked", reason="science_prewarm_existing_target_or_owner_changed")
            error = self._binding_error(obs, plan)
            if error:
                return dict(status="blocked", reason=error)
            if "pending_transfer" in self.state or self.state.get("transfer_error"):
                return dict(status="blocked", reason="science_prewarm_transfer_requires_reconciliation")
            action = self._balanced_funding(obs, surplus_budget=surplus_budget)
            return action or dict(
                status="waiting", reason="science_prewarm_raw_budget_funded_waiting_for_native_production"
            )
        except (KeyError, TypeError, ValueError) as exc:
            return dict(status="blocked", reason="science_prewarm_context_invalid", evidence={"detail": str(exc)})

    def _progress(self, obs):
        finished = self._finished(obs)
        recoveries = [
            dict(
                id=e["id"],
                replacement_inputs=deepcopy(e["authorization"]["replacement_inputs"]),
                allocated_inputs=deepcopy(e["allocated_inputs"]),
                receipt_ids=[r["id"] for r in e["authorization"]["receipts"]],
            )
            for e in self.state.get("diversion_recoveries", [])
        ]
        effective = deepcopy(self.state["required_inputs"])
        for epoch in recoveries:
            for item, count in epoch["replacement_inputs"].items():
                effective[item] = effective.get(item, 0) + count
        topups = self.state.get("input_topups", [])
        for epoch in topups:
            for item, count in epoch["inputs"].items():
                effective[item] = effective.get(item, 0) + count
        extra = (
            dict(
                input_topups=[dict(packs=e["packs"], allocated_inputs=deepcopy(e["allocated_inputs"])) for e in topups]
            )
            if topups
            else {}
        )
        return dict(
            **extra,
            technology=self.technology,
            required_packs=self.state["required_packs"],
            machine_products_finished_delta=finished - self.state["baseline_products_finished"],
            research_science_consumed=obs["production"]["automation-science-pack"]["consumed"]
            - self.state["baseline_consumed"],
            allocated_normal_inputs=deepcopy(self.state["allocated_inputs"]),
            effective_input_allowance=effective,
            diversion_recoveries=recoveries,
            **self._autonomous_wait_flags(obs),
        )

    def _research_proof(self, obs):
        if not obs.get("technologies", {}).get(self.technology):
            return None
        if not self.state:
            return {"tick": obs["tick"], "technology": self.technology, "already_researched": True}
        plan = self._startup_plan(obs)
        # The technology is researched: the batch is finished, so its proof
        # may validate against a plan the cell has since grown from.
        if plan is None or self._binding_error(obs, plan, historical=True):
            return None
        if self.state.get("pending_transfer") or self.state.get("transfer_error"):
            return None
        if any(not _powered(self.find(obs, pin)) for pin in self.state["machines"] + self._lab_pins()):
            return None
        evidence = self._progress(obs)
        recipe = self.catalog.recipe_for_product("automation-science-pack")
        output = sum(p["amount"] for p in recipe["products"] if p["name"] == "automation-science-pack")
        packs = evidence["machine_products_finished_delta"] * output
        if packs < self.state["required_packs"] or evidence["research_science_consumed"] <= 0:
            return None
        return dict(
            evidence,
            tick=obs["tick"],
            normally_assembled_packs=packs,
            red_machine_unit=self.state["red_machine"]["unit_number"],
            baseline_products_finished=self.state["baseline_products_finished"],
            plan_hash=self.state["plan_hash"],
            scope="finite assisted raw buffers; normal gear/science machines and native research, no sustained science rate claim",
        )
