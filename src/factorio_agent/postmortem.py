"""Post-mortem snapshot of a paused run: read-only queries and dry-run planners, nothing built.

``snapshot(root, ...)`` writes one folder ``<run>/postmortem/<stamp>-t<tick>/``
and nothing else (docs/v2/DEBUGGING_A_STALL.md "Post-mortem of a fleet run"):

- ``observe-raw.json``: ``game.observe()`` (every entity, inventory, research);
- ``observe.json``: the full ``python -m factorio_agent observe`` receipt with
  the planner receipts (supply options, demand, research capacity, ...);
- ``layout.svg``: the dashboard's layout map, when a renderer is passed in;
- ``probes/<job>.json``: per blocked/failed (or, for a stalled run, waiting)
  job: its ledger row, a dry run of its planner (``DRY_RUN``: the pure
  ``prepare`` with evidence capture on, then the controller's own ``step``),
  every evidence note (``evidence_capture``: candidates with the check each
  failed, preflight and route failures with entity and position), the queries
  the planner sent, an entity/resource/water/cliff dump of the areas around
  the failure positions, the entities found on those positions and a short
  ``cause``; ``probes/<job>-<mode>-queries.jsonl`` holds the query receipts;
- ``manifest.json``: what was written, timings, errors, and ``run_files_changed``
  (files of the run outside ``postmortem/`` whose size or mtime changed while
  the snapshot ran; empty when nothing was written).

Read-only by construction: the planners run against ``ReadOnlyGame`` (``act``,
``save`` and ``initialize`` refuse; a query body matching ``WRITE_PATTERN``
refuses before it is sent) and against a sandbox copy of the run's state files
under the snapshot folder, removed afterwards; a write a planner would need is
recorded as ``read_only_violation``. Queries stay world-bound (the live game's
``expected_world``). The caller pauses the world first (``tools/fleet.py``:
supervisor safe stop, ``game.tick_paused``); ``world.tick_paused`` is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import traceback
from typing import Callable

from . import evidence_capture

PROBE_SECONDS = 90.0  # planning budget of one dry run (industrial / mining layout PLANNING_SECONDS)
MAX_PROBED_JOBS = 4
MAX_AREAS = 6
AREA_SIDE = 128  # tiles; a larger focus area is cut around its centre
QUERY_ROW_CHARS = 2_000_000
STATE_DIRS = ("logs",)  # sub-folders whose *.json state is copied into a sandbox
SKIP_FINGERPRINT = ("postmortem", "vanilla", "agent-client")
# Lua that changes the world. Planning queries are reads (jobs._advance_job: "Planning is read-only");
# a body that matches is refused and recorded, never sent.
WRITE_PATTERN = re.compile(
    r"create_entity|\.destroy\s*\(|\.die\s*\(|mine_entity|mine_tile|\.set_tiles\s*\(|begin_crafting|"
    r"cancel_crafting|\.teleport\s*\(|(?<!table)\.insert\s*[({]|\.remove_item\s*[({]|\.clear_items_inside|"
    r"set_controller|game\.tick_paused\s*=(?!=)|game\.speed\s*=(?!=)|\.researched\s*=(?!=)|add_research|"
    r"cancel_current_research|\.chart\s*\(|request_to_generate_chunks|order_deconstruction|order_upgrade|"
    r"\.rotate\s*\(|\.set_recipe\s*\(|\.walking_state\s*=(?!=)|\.active\s*=(?!=)|storage\.\w[\w.]*\s*=(?!=)|"
    r"\bd\.\w[\w.]*\s*=(?!=)|game\.(auto_)?save|helpers\.write_file"
)
ACTIVE = ("pending", "running", "waiting")


class ReadOnlyViolation(RuntimeError):
    """A dry run needed a world write (an action, a save or a writing query)."""


def write_hit(body: str) -> str | None:
    found = WRITE_PATTERN.search(body or "")
    return found.group(0) if found else None


def _label(body: str) -> str:
    text = " ".join(str(body).split())
    if text.startswith("if not d or d.world_id~="):
        text = text.partition("end; ")[2]
    return text[:96]


class ReadOnlyGame:
    """Query-only view of a live (world-bound) game whose ``cfg.runtime_dir`` is a sandbox.

    ``inner`` is the run's ``WorldBoundGame`` (or a test fake with ``cfg``,
    ``query`` and ``observe``). Planner state files resolve to ``sandbox``;
    RCON settings stay the run's. ``recorded`` keeps every planner query with
    its receipt, ``refused`` every refused write.
    """

    REFUSED = frozenset({"act", "save", "initialize", "_build_many", "_record_action", "_execute"})

    def __init__(self, inner, sandbox):
        self.inner = inner
        sandbox = Path(sandbox)
        self.cfg = replace(inner.cfg, runtime_dir=sandbox, log_dir=sandbox / "logs")
        self.expected_world = getattr(inner, "expected_world", None)
        self.backend = getattr(inner, "backend", "assisted")
        self.observation_metrics = {}
        self.act_epoch = 0
        self.recorded: list[dict] = []
        self.refused: list[dict] = []

    def query(self, body: str) -> dict:
        hit = write_hit(body)
        if hit:
            self.refused.append({"kind": "query", "pattern": hit, "script": _label(body)})
            raise ReadOnlyViolation("query_would_write:" + hit)
        started = time.perf_counter()
        result = self.inner.query(body)
        self.recorded.append(
            {"script": _label(body), "seconds": round(time.perf_counter() - started, 3), "result": result}
        )
        return result

    def observe(self, radius: float = 384) -> dict:
        # The fixed observation Lua of DeterministicGame is a read (no planner text).
        return self.inner.observe(radius)

    def act(self, action):
        kind = action.get("type") if isinstance(action, dict) else None
        self.refused.append({"kind": "act", "action": kind})
        raise ReadOnlyViolation("action_refused_in_postmortem:" + str(kind))

    def save(self):
        self.refused.append({"kind": "save"})
        raise ReadOnlyViolation("save_refused_in_postmortem")

    def initialize(self):
        self.refused.append({"kind": "initialize"})
        raise ReadOnlyViolation("initialize_refused_in_postmortem")

    def close(self):
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name):
        inner = self.__dict__.get("inner")
        if inner is None or name in ReadOnlyGame.REFUSED or name.startswith("__"):
            raise AttributeError(name)
        return getattr(inner, name)


# ----- sandbox and read-only proof -------------------------------------------------------
def make_sandbox(root: Path, dest: Path) -> Path:
    """Copy the run's ``*.json`` state (top level and ``logs/``) into ``dest``; logs, saves and locks stay."""
    root, dest = Path(root), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in root.iterdir():
        if path.is_file() and path.suffix == ".json":
            shutil.copy2(path, dest / path.name)
    for name in STATE_DIRS:
        source = root / name
        if source.is_dir():
            (dest / name).mkdir(exist_ok=True)
            for path in source.iterdir():
                if path.is_file() and path.suffix == ".json":
                    shutil.copy2(path, dest / name / path.name)
    return dest


def fingerprint(root: Path, skip=SKIP_FINGERPRINT) -> dict:
    """``relative path -> (size, mtime_ns)`` of the run's files outside ``skip`` folders and ``*.log``."""
    root = Path(root)
    out = {}
    for folder, dirs, files in os.walk(root):
        here = Path(folder)
        if here == root:
            dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name.endswith(".log"):
                continue
            path = here / name
            try:
                stat = path.stat()
            except OSError:
                continue
            out[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return out


def changed_files(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def _planning_budget(seconds):
    """Raise the planners' wall-clock budgets for one dry run (a full candidate sweep), then restore."""
    import contextlib

    from . import industrial, mining_layout_policy

    @contextlib.contextmanager
    def scope():
        old = industrial.PLANNING_SECONDS, mining_layout_policy.PLANNING_SECONDS
        industrial.PLANNING_SECONDS = mining_layout_policy.PLANNING_SECONDS = seconds
        try:
            yield
        finally:
            industrial.PLANNING_SECONDS, mining_layout_policy.PLANNING_SECONDS = old

    return scope()


# ----- dry-run planners --------------------------------------------------------------------
@dataclass(frozen=True)
class DryRun:
    """``prepare(ex, obs, parameters)``: the planner's pure plan; ``step``: its controller step without
    starter upkeep; ``reset``: sandbox files removed first (resumable search cursors: a full sweep)."""

    prepare: Callable
    step: Callable | None = None
    reset: tuple = ()


MINING_RATE_LUA = (
    'return {ok=true,rate=prototypes.entity["electric-mining-drill"].mining_speed*60/prototypes.entity[%s]'
    ".mineable_properties.mining_time}"
)


def _outpost_prepare(ex, obs, parameters):
    """``MiningOutposts.prepare`` with the drill count ``MiningOutposts.step`` would ask for now."""
    resource, target = parameters["resource"], parameters["rate_per_minute"]
    meta = ex.game.query(MINING_RATE_LUA % json.dumps(resource))
    per_drill = meta.get("rate") if isinstance(meta, dict) else None
    if not isinstance(per_drill, (int, float)) or per_drill <= 0:
        return {"ok": False, "reason": "mining_rate_query_failed", "receipt": meta}
    rate = 0.0
    for bank in ex.outposts.state.get("banks", []):
        if bank["plan"]["resource"] != resource or bank.get("material_supply_id") or not bank.get("ready"):
            continue
        proof = ex.outposts.bank_proof(obs, bank)
        rate += proof["nominal_rate_per_minute"] if proof else 0
    from .remedy import scaled_count  # the shrink rung of a what-if (remedy.what_if) applies here too

    count = scaled_count(max(1, min(8, math.ceil((target - rate) / per_drill))))
    return dict(ex.outposts.prepare(obs, resource, count), dry_run_count=count, dry_run_active_rate=rate)


def _supply_prepare(ex, obs, parameters):
    if parameters.get("retire") or "retire_product" in parameters:
        return {"ok": False, "reason": "dry_run_not_applicable_to_supply_cell_retirement"}
    return ex.supply_cell.prepare(obs, parameters["products"])


DRY_RUN = {
    "mining_outpost": DryRun(_outpost_prepare, lambda ex, obs, p: ex.outposts.step(obs, p)),
    "planned_main_bus": DryRun(
        lambda ex, obs, p: ex.planned_bus.prepare(obs), lambda ex, obs, p: ex.planned_bus.step(obs, p)
    ),
    "smelting_district": DryRun(
        lambda ex, obs, p: ex.smelting.prepare(obs),
        lambda ex, obs, p: ex.smelting.step(obs),
        reset=("smelting-district-search.json",),
    ),
    "produce": DryRun(
        lambda ex, obs, p: ex.production_rows.prepare(obs, p), lambda ex, obs, p: ex.production_rows.step(obs, p)
    ),
    "supply_cell": DryRun(_supply_prepare, lambda ex, obs, p: ex.supply_cell.step(obs, p)),
    "dedicated_materials": DryRun(
        lambda ex, obs, p: ex.material_supply.prepare(obs, p), lambda ex, obs, p: ex.material_supply.step(obs, p)
    ),
    "logistic_science": DryRun(lambda ex, obs, p: ex.green.prepare(obs, p)),
    "brick_production": DryRun(lambda ex, obs, p: _furnace_prepare(ex, obs, "brick")),
    "steel_production": DryRun(lambda ex, obs, p: _furnace_prepare(ex, obs, "steel")),
    # steel_supply plans the same planned furnace block (planned_furnaces.supply_step) on a planned world.
    "steel_supply": DryRun(lambda ex, obs, p: _furnace_prepare(ex, obs, "steel")),
    # Engines and magazines are produce rows on a planned world (planned_products).
    "engine_production": DryRun(lambda ex, obs, p: _row_prepare(ex, obs, "ENGINE_ROW")),
    "ammo_production": DryRun(lambda ex, obs, p: _row_prepare(ex, obs, "AMMO_ROW")),
    "steam_expansion": DryRun(lambda ex, obs, p: ex.steam_expansion.prepare(obs)),
    "trunk_return_feed": DryRun(lambda ex, obs, p: ex.trunk_return_feed.prepare(obs, p["resource"])),
    "oil_gathering": DryRun(
        lambda ex, obs, p: ex.oil_gathering.prepare(obs), lambda ex, obs, p: ex.oil_gathering.step(obs)
    ),
    # The rocket tail (rocket_stages / rocket_feed): the silo site and the stage-ii belt feed.
    "rocket_silo": DryRun(
        lambda ex, obs, p: ex._rocket_silo().prepare(obs, p), lambda ex, obs, p: ex._rocket_silo().step(obs, p)
    ),
    "rocket_parts": DryRun(
        lambda ex, obs, p: ex._rocket_parts().prepare_feed(obs, p), lambda ex, obs, p: ex._rocket_parts().step(obs, p)
    ),
}


def _furnace_prepare(ex, obs, kind):
    """The planned furnace block (``planned_furnaces``) on a planned world, else the legacy industrial plan."""
    planned = ex.industrial_operations.planned(kind, obs)
    if planned is not None:
        return planned.prepare(obs)
    return getattr(ex.industrial_operations, f"prepare_{kind}_production")(obs)


def _row_prepare(ex, obs, name):
    """The fixed produce row of ``planned_products`` on a planned world; no answer on the development map."""
    from . import planned_products

    request = getattr(planned_products, name)
    if not planned_products.active(ex.production_rows, obs, request["item"]):
        return {"reason": "no_planned_world_row"}  # not a plan result: fits None
    return ex.production_rows.prepare(obs, dict(request))


def _handler_step(ex, obs, parameters, capability):
    from .capability_registry import handler_for

    return handler_for(capability).step(ex, obs, parameters)


def _default_executors(game, catalog):
    from .executors import StarterExecutors

    return StarterExecutors(game, catalog)


# ----- area survey -------------------------------------------------------------------------
AREA_LUA = r"""
local x=helpers.json_to_table(%s);local area=x.area
local ents={};local res={};local by={};local cliffs={};local n=0
for _,e in pairs(s.find_entities_filtered{area=area}) do
 if e.valid then
  if e.type=="resource" then
   by[e.name]=(by[e.name] or 0)+e.amount
   if #res<x.max_resources then res[#res+1]={name=e.name,position=pos(e.position),amount=e.amount} end
  elseif e.type=="cliff" then cliffs[#cliffs+1]={position=pos(e.position),orientation=e.cliff_orientation}
  elseif n<x.max_entities then n=n+1
   ents[#ents+1]={name=e.name,type=e.type,force=e.force.name,position=pos(e.position),direction=e.direction,
    unit_number=e.unit_number}
  end
 end
end
local names={};for name,_ in pairs(prototypes.tile) do if string.find(name,"water",1,true) then names[#names+1]=name end end
local water={};if #names>0 then for _,t in pairs(s.find_tiles_filtered{area=area,name=names}) do
 if #water<x.max_tiles then water[#water+1]=pos(t.position) end end end
local charted=0;local chunks=0
for cx=math.floor(area[1][1]/32),math.floor(area[2][1]/32) do for cy=math.floor(area[1][2]/32),math.floor(area[2][2]/32) do
 chunks=chunks+1;if f.is_chunk_charted(s,{cx,cy}) then charted=charted+1 end end end
return {ok=true,tick=game.tick,area=area,entities=ents,entity_limit_reached=n>=x.max_entities,resources=res,
 resource_amounts=by,cliffs=cliffs,water=water,chunks=chunks,charted_chunks=charted}
"""


def clamp_area(area, side=AREA_SIDE):
    (x0, y0), (x1, y1) = area
    x0, x1 = sorted((float(x0), float(x1)))
    y0, y1 = sorted((float(y0), float(y1)))
    if x1 - x0 > side:
        cx = (x0 + x1) / 2
        x0, x1 = cx - side / 2, cx + side / 2
    if y1 - y0 > side:
        cy = (y0 + y1) / 2
        y0, y1 = cy - side / 2, cy + side / 2
    return [[math.floor(x0), math.floor(y0)], [math.ceil(x1), math.ceil(y1)]]


def area_dump(game, area, *, max_entities=3000, max_resources=1500, max_tiles=1500) -> dict:
    args = dict(area=clamp_area(area), max_entities=max_entities, max_resources=max_resources, max_tiles=max_tiles)
    return game.query(AREA_LUA % json.dumps(json.dumps(args)))


def _point(value):
    if (
        isinstance(value, dict)
        and isinstance(value.get("x"), (int, float))
        and isinstance(value.get("y"), (int, float))
    ):
        return float(value["x"]), float(value["y"])
    return None


def failure_points(job: dict, notes: list) -> list[tuple]:
    """(label, (x, y)) of every failed check that names a position: job evidence, preflight, routes."""
    points = []
    evidence = job.get("evidence") if isinstance(job.get("evidence"), dict) else {}
    if _point(evidence.get("position")):
        points.append(("job evidence", _point(evidence["position"])))
    for n in notes:
        outcome = n.get("outcome") if isinstance(n.get("outcome"), dict) else {}
        if outcome.get("ok") is not False:
            continue
        if _point(outcome.get("position")):
            points.append((f"{n['stage']} {outcome.get('reason')}", _point(outcome["position"])))
        elif n.get("stage") == "route":
            for end in ("source", "destination"):
                if _point(n.get(end)):
                    points.append((f"route {end} {outcome.get('reason')}", _point(n[end])))
    seen, out = set(), []
    for label, p in points:
        if p not in seen:
            seen.add(p)
            out.append((label, p))
    return out


def focus_areas(job: dict, notes: list, limit=MAX_AREAS) -> list[dict]:
    """The areas worth an entity dump: failure positions, failed route spans, candidate and patch areas."""
    areas = []

    def add(label, area):
        if area and len(areas) < limit:
            areas.append({"label": label, "area": clamp_area(area)})

    for label, (x, y) in failure_points(job, notes)[:3]:
        add(label, [[x - 12, y - 12], [x + 12, y + 12]])
    for n in notes:
        outcome = n.get("outcome") if isinstance(n.get("outcome"), dict) else {}
        if (
            n.get("stage") == "route"
            and outcome.get("ok") is False
            and _point(n.get("source"))
            and _point(n.get("destination"))
        ):
            (ax, ay), (bx, by) = _point(n["source"]), _point(n["destination"])
            add(
                f"route {n.get('item')} {outcome.get('reason')}",
                [[min(ax, bx) - 8, min(ay, by) - 8], [max(ax, bx) + 8, max(ay, by) + 8]],
            )
        elif n.get("stage") == "mining_bank_candidates" and n.get("area"):
            add(f"mining patch {n.get('resource')} side {n.get('site_exit')}", n["area"])
        elif n.get("stage") == "smelting_district_candidates":
            for area in (n.get("land_illegal") or [])[:2]:
                add("smelting candidate rejected by the land survey", area)
    return areas


def blockers_at(points, dumps, radius=1.5) -> list[dict]:
    """Per failure position: the entities, resources, water tiles and cliffs within ``radius`` tiles."""
    out = []
    for label, (x, y) in points:
        near = lambda p: _point(p) and abs(_point(p)[0] - x) <= radius and abs(_point(p)[1] - y) <= radius  # noqa: E731
        row = {"at": [x, y], "check": label, "entities": [], "resources": set(), "water": 0, "cliffs": 0}
        for dump in dumps:
            receipt = dump.get("receipt") or {}
            if not isinstance(receipt, dict) or not receipt.get("ok"):
                continue
            row["entities"] += [
                f"{e.get('name')}@{e['position']['x']},{e['position']['y']}"
                for e in receipt.get("entities") or []
                if isinstance(e, dict) and near(e.get("position"))
            ]
            row["resources"] |= {r.get("name") for r in receipt.get("resources") or [] if near(r.get("position"))}
            row["water"] += sum(1 for p in receipt.get("water") or [] if near(p))
            row["cliffs"] += sum(1 for c in receipt.get("cliffs") or [] if near(c.get("position")))
        row["entities"] = sorted(set(row["entities"]))[:12]
        row["resources"] = sorted(r for r in row["resources"] if r)
        out.append(row)
    return out


# ----- cause ---------------------------------------------------------------------------------
def _xy(p):
    q = _point(p)
    return f"({q[0]:g},{q[1]:g})" if q else "?"


def cause_of(probe: dict) -> dict:
    """A short concrete cause from a probe: the deepest failed check with what stands on its tile."""
    concrete, details = [], []
    blockers = {tuple(b["at"]): b for b in probe.get("blockers") or []}

    def standing(p):
        q = _point(p)
        b = blockers.get(q) if q else None
        if not b:
            return ""
        parts = b["entities"][:3] + [f"{r} ore" for r in b["resources"][:1]]
        parts += [f"{b['water']} water tiles"] if b["water"] else []
        parts += [f"{b['cliffs']} cliffs"] if b["cliffs"] else []
        return " [" + ", ".join(parts) + "]" if parts else " [nothing there now]"

    for entry in probe.get("dry_runs") or []:
        mode = entry.get("mode")
        if entry.get("error"):
            details.append(f"{mode}: {entry['error']}"[:200])
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        if entry.get("would_act"):
            details.append(
                f"{mode}: would act now: {entry['would_act'].get('type')} ({entry['would_act'].get('reason')})"[:200]
            )
        elif result.get("ok") is False or result.get("status") in ("blocked", "failed", "waiting"):
            details.append(f"{mode}: {result.get('reason')}"[:200])
        for n in entry.get("notes") or []:
            stage, outcome = n.get("stage"), n.get("outcome") if isinstance(n.get("outcome"), dict) else {}
            if stage == "preflight" and outcome.get("ok") is False:
                text = f"preflight {outcome.get('reason')}: {outcome.get('entity') or ''} at {_xy(outcome.get('position'))}"
                concrete.append(text + standing(outcome.get("position")))
            elif stage == "route" and outcome.get("ok") is False:
                where = outcome.get("position") or n.get("source")
                text = f"route {n.get('item')} {_xy(n.get('source'))}->{_xy(n.get('destination'))}: {outcome.get('reason')}"
                concrete.append(text + standing(where))
            elif stage == "mining_bank_candidates":
                rows = n.get("candidates") or []
                failed = {}
                for row in rows:
                    failed[str(row.get("failure"))] = failed.get(str(row.get("failure")), 0) + 1
                details.append(
                    f"mining bank {n.get('resource')} side {n.get('site_exit')}: {len(rows)} candidates on "
                    f"{n.get('patch_tiles')} patch tiles, failures {failed or 'none (no candidate site)'}"
                )
            elif stage == "mining_patch_receipt":
                details.append(
                    f"patch survey receipt rejected: tiles {n.get('tiles_type')} x{n.get('tiles')} in {n.get('area')}"
                )
            elif stage == "smelting_district_candidates":
                stages = {}
                for f in n.get("failures") or []:
                    if isinstance(f, dict):
                        stages[f.get("stage")] = stages.get(f.get("stage"), 0) + 1
                details.append(
                    f"smelting: {n.get('offered')} offered, {n.get('after_block_filters')} after block filters, "
                    f"{len(n.get('land_legal') or [])} land-legal, failures {stages}"
                )
    if probe.get("refused"):
        details.append("read-only refusals: " + ", ".join(str(r) for r in probe["refused"][:3]))
    # A failed check with something standing on its tile names the cause best.
    concrete.sort(key=lambda text: not text.endswith("]") or text.endswith("[nothing there now]"))
    ordered = list(dict.fromkeys(concrete + details))
    job = probe.get("job") or {}
    summary = (concrete or details or [f"{job.get('capability')}: {job.get('reason')}"])[0]
    return {"summary": str(summary)[:240], "details": [str(d)[:240] for d in ordered[:14]]}


# ----- probe and snapshot ------------------------------------------------------------------------
def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, default=str), encoding="utf-8")


def _safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:80]


def _dry_run(mode, func, root, live, catalog, obs, job, folder, *, reset, budget_seconds, executors_factory):
    key = job.get("key") or "job"
    sandbox = make_sandbox(root, folder / "sandbox" / f"{_safe_name(key)}-{mode}")
    for name in reset:
        target = sandbox / name
        if target.exists():
            target.unlink()
    copied = fingerprint(sandbox, skip=())
    game = ReadOnlyGame(live, sandbox)
    entry = {"mode": mode, "sandbox_reset": list(reset)}
    started = time.perf_counter()
    with evidence_capture.capturing() as notes, _planning_budget(budget_seconds):
        try:
            executors = executors_factory(game, catalog)
            result = func(executors, obs, dict(job.get("parameters") or {}))
            entry["result"] = evidence_capture.bounded(result)
            if isinstance(result, dict) and result.get("type"):
                entry["would_act"] = {k: result.get(k) for k in ("type", "reason", "name", "item", "position")}
        except ReadOnlyViolation as exc:
            entry["error"] = f"read_only_violation: {exc}"
        except Exception as exc:  # a planner fault is evidence too
            entry["error"] = f"{type(exc).__name__}: {exc}"[:400]
            entry["traceback"] = traceback.format_exc()[-1500:]
    entry["seconds"] = round(time.perf_counter() - started, 2)
    entry["notes"] = notes
    entry["refused"] = list(game.refused)
    entry["queries"] = len(game.recorded)
    entry["sandbox_writes"] = changed_files(copied, fingerprint(sandbox, skip=()))
    rows = folder / "probes" / f"{_safe_name(key)}-{mode}-queries.jsonl"
    rows.parent.mkdir(parents=True, exist_ok=True)
    with rows.open("w", encoding="utf-8") as out:
        for row in game.recorded:
            text = json.dumps(row, default=str)
            if len(text) > QUERY_ROW_CHARS:
                text = json.dumps({"script": row["script"], "seconds": row["seconds"], "truncated_chars": len(text)})
            out.write(text + "\n")
    entry["queries_file"] = rows.name
    shutil.rmtree(sandbox, ignore_errors=True)
    return entry, game


def probe_job(root, live, catalog, obs, job, folder, *, budget_seconds=PROBE_SECONDS, executors_factory=None):
    """Dry-run one job's planner(s) read-only, dump the areas around its failures, name a cause."""
    root, folder = Path(root), Path(folder)
    executors_factory = executors_factory or _default_executors
    capability = job.get("capability")
    spec = DRY_RUN.get(capability)
    probe = {
        "job": {
            k: job.get(k)
            for k in (
                "key",
                "capability",
                "parameters",
                "status",
                "reason",
                "evidence",
                "last_tick",
                "blocked_at_tick",
                "progress_at_tick",
            )
        },
        "dry_run_supported": spec is not None,
        "dry_runs": [],
        "refused": [],
    }
    common = dict(budget_seconds=budget_seconds, executors_factory=executors_factory)
    last_game = None
    if spec is not None:
        entry, last_game = _dry_run(
            "prepare", spec.prepare, root, live, catalog, obs, job, folder, reset=spec.reset, **common
        )
        probe["dry_runs"].append(entry)
        step = spec.step
    else:
        probe["prepare"] = (
            f"not supported: {capability} has no pure prepare in postmortem.DRY_RUN; controller step only"
        )
        step = None
    if step is None:
        step = lambda ex, o, p: _handler_step(ex, o, p, capability)  # noqa: E731
    entry, game = _dry_run(
        "step", step, root, live, catalog, obs, job, folder, reset=spec.reset if spec else (), **common
    )
    probe["dry_runs"].append(entry)
    last_game = last_game or game
    for e in probe["dry_runs"]:
        probe["refused"] += e["refused"]
    notes = [n for e in probe["dry_runs"] for n in e["notes"]]
    dumps = []
    for area in focus_areas(job, notes):
        try:
            receipt = area_dump(ReadOnlyGame(live, folder), area["area"])
        except Exception as exc:  # one area failing must not lose the rest
            receipt = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
        dumps.append(dict(area, receipt=receipt))
    probe["areas"] = dumps
    probe["blockers"] = blockers_at(failure_points(job, notes)[:8], dumps)
    probe["cause"] = cause_of(probe)
    return probe


def problem_jobs(jobs: dict, wanted=(), limit=MAX_PROBED_JOBS) -> list[dict]:
    """``wanted`` keys first (triage order), then blocked/failed jobs newest first; waiting jobs only if named."""
    rows = [dict(job, key=key) for key, job in jobs.items() if isinstance(job, dict)]
    by_key = {r["key"]: r for r in rows}
    chosen = [by_key[k] for k in wanted if k in by_key]
    stuck = [r for r in rows if r.get("status") in ("blocked", "failed") and r["key"] not in wanted]
    stuck.sort(key=lambda r: -(r.get("blocked_at_tick") or r.get("last_tick") or 0))
    return (chosen + stuck)[:limit]


def load_catalog(root: Path, game):
    """The run's catalog file; without one the catalog is read from the world, never written to the run."""
    from .core.world_catalog import WorldCatalog

    path = Path(root) / "catalog.json"
    if path.exists():
        return WorldCatalog.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return WorldCatalog.from_game(game.query)


class _Services:
    """``__main__.execute`` services bound to the sandbox's read-only game."""

    def __init__(self, game, catalog, executors_factory):
        self._game, self._catalog, self._factory = game, catalog, executors_factory

    def game(self, root):
        return self._game

    def catalog(self, root, game):
        return self._catalog

    def executors(self, root, game, catalog):
        return self._factory(game, catalog)


def observe_receipt(sandbox, game, catalog, executors_factory=None):
    """The full ``observe`` receipt computed in the sandbox (its rate samples land there, not in the run)."""
    from .__main__ import execute

    services = _Services(game, catalog, executors_factory or _default_executors)
    return execute(["--runtime", str(sandbox), "observe"], services=services)


def snapshot(
    root,
    *,
    triage=None,
    game=None,
    catalog=None,
    layout=None,
    wanted_jobs=None,
    max_jobs=MAX_PROBED_JOBS,
    budget_seconds=PROBE_SECONDS,
    executors_factory=None,
    observe=observe_receipt,
    clock=time.time,
) -> dict:
    """Write one post-mortem folder for a paused run (module docstring); returns its manifest."""
    root = Path(root).resolve()
    before = fingerprint(root)
    started = time.perf_counter()
    if game is None:
        from . import runtime

        game = runtime.game_for(root)
        game.keep_connection = True
    base = root / "postmortem"
    errors = {}
    probe_game = ReadOnlyGame(game, base)
    world = probe_game.query("return {ok=true,tick=game.tick,tick_paused=game.tick_paused,world_id=d.world_id}")
    tick = world.get("tick") if isinstance(world, dict) else None
    folder = base / (time.strftime("%Y%m%d-%H%M%S", time.localtime(clock())) + f"-t{tick if tick is not None else 'x'}")
    folder.mkdir(parents=True, exist_ok=True)
    written = []

    def keep(name, value):
        _write(folder / name, value)
        written.append(name)

    if triage is not None:
        keep("triage.json", triage)
    obs = None
    try:
        obs = probe_game.observe()
        keep("observe-raw.json", obs)
    except Exception as exc:
        errors["observe_raw"] = f"{type(exc).__name__}: {exc}"[:300]
    try:
        catalog = catalog if catalog is not None else load_catalog(root, probe_game)
    except Exception as exc:
        errors["catalog"] = f"{type(exc).__name__}: {exc}"[:300]
    if catalog is not None and observe is not None:
        sandbox = make_sandbox(root, folder / "sandbox" / "observe")
        try:
            keep("observe.json", observe(sandbox, ReadOnlyGame(game, sandbox), catalog, executors_factory))
        except Exception as exc:
            errors["observe"] = f"{type(exc).__name__}: {exc}"[:300]
        shutil.rmtree(sandbox, ignore_errors=True)
    if layout is not None:
        try:
            svg = layout(root)
            if svg:
                (folder / "layout.svg").write_text(svg, encoding="utf-8")
                written.append("layout.svg")
        except Exception as exc:
            errors["layout"] = f"{type(exc).__name__}: {exc}"[:300]
    jobs = {}
    try:
        jobs = json.loads((root / "jobs.json").read_text(encoding="utf-8")).get("jobs", {})
    except (OSError, ValueError, AttributeError) as exc:
        errors["jobs"] = f"{type(exc).__name__}: {exc}"[:300]
    probes = []
    if obs is not None and obs.get("ok", True) and catalog is not None:
        for job in problem_jobs(jobs, wanted_jobs or (triage or {}).get("jobs") or (), max_jobs):
            try:
                probe = probe_job(
                    root,
                    game,
                    catalog,
                    obs,
                    job,
                    folder,
                    budget_seconds=budget_seconds,
                    executors_factory=executors_factory,
                )
            except Exception as exc:
                errors["probe:" + str(job.get("key"))] = f"{type(exc).__name__}: {exc}"[:300]
                continue
            name = f"probes/{_safe_name(job['key'])}.json"
            keep(name, probe)
            probes.append(
                dict(
                    key=job["key"],
                    capability=job.get("capability"),
                    reason=job.get("reason"),
                    file=name,
                    cause=probe["cause"]["summary"],
                )
            )
    shutil.rmtree(folder / "sandbox", ignore_errors=True)
    manifest = dict(
        kind="factorio-postmortem-v1",
        runtime=str(root),
        folder=str(folder),
        world=world,
        world_paused=bool(isinstance(world, dict) and world.get("tick_paused")),
        created_at=clock(),
        seconds=round(time.perf_counter() - started, 1),
        files=written,
        probes=probes,
        errors=errors,
        run_files_changed=changed_files(before, fingerprint(root)),
    )
    _write(folder / "manifest.json", manifest)
    _write(
        base / "latest.json",
        dict(
            folder=str(folder),
            tick=tick,
            created_at=manifest["created_at"],
            probes=probes,
            **{"class": (triage or {}).get("class")},
        ),
    )
    return manifest


def latest(root) -> dict | None:
    """``<run>/postmortem/latest.json`` (the newest snapshot), or None."""
    try:
        value = json.loads((Path(root) / "postmortem" / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def probe_cause(root, key) -> dict | None:
    """The ``cause`` of job ``key`` in the newest snapshot of the run, or None."""
    info = latest(root)
    if not info:
        return None
    path = Path(info["folder"]) / "probes" / f"{_safe_name(key)}.json"
    try:
        probe = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(probe.get("cause") or {}, file=str(path))
