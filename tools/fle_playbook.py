"""Autonomous Claude-authored FLE playbook.

Sustains production + research toward the rocket by running the PROVEN operation sequence in a loop
against the LIVE no-mod RCON game -- no cluster LLM, no rigid-skill strategy. It reuses the FLE
FactorioApi bound to the live ModlessFactorioController (same act/observe path the autopilot uses).
The virtual agent teleports, so all hand-carry across the far-resource gap is free.

Each cycle (adaptive -- skips what isn't applicable):
  1. keep burners fueled (drills/furnaces/boiler) from the big coal stock
  2. smelt iron: take stockpiled iron-ore from iron drills -> insert into iron furnaces
  3. gather iron-plate from iron furnaces -> craft gears
  4. feed the science assembler (gears on hand + copper from the copper furnace)
  5. deliver science packs from the science assembler -> the lab
  6. ensure a research is queued (pick the next not-done tech)

Usage:  PYTHONPATH=src python tools/fle_playbook.py [cycles] [sleep_seconds]
        cycles=0 -> run forever.  Logs to logs/fle-playbook.jsonl + runtime/fle-playbook-heartbeat.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from factorio_ai import knowledge
from factorio_ai.code_agent import FactorioApi
from factorio_ai.config import load_config
from factorio_ai.controller import ModlessFactorioController

_TECHS = knowledge._load_game_data()[1]  # {name: Technology(science_packs, prerequisites, ...)}

# the assembly cluster sits near spawn (negative coords); production is far (y>50 iron, copper east).
SCIENCE_ASM = (-40.5, -27.5)
LAB = (-36.5, -34.5)
# research preference order toward the rocket. logistic-science-pack FIRST: it is red-science-only and
# UNLOCKS green-science production (the gate to almost every later tech). ensure_research only ever
# queues a tech whose science we can currently PRODUCE + whose prereqs are researched, so stalled
# green/blue-gated techs are skipped until we build that tier.
RESEARCH_PREF = [
    "steel-processing", "logistic-science-pack", "fast-inserter", "automation-2",
    "advanced-material-processing", "electric-energy-distribution-1", "fluid-handling",
    "oil-processing", "sulfur-processing", "plastics", "advanced-circuit", "engine",
    "chemical-science-pack", "battery", "electric-engine", "modules", "advanced-material-processing-2",
    "low-density-structure", "rocket-fuel", "rocket-control-unit", "production-science-pack",
    "utility-science-pack", "rocket-silo",
]


def _producible_science(api):
    """Science packs we can currently PRODUCE: red always; a tier is only claimed once an assembler is
    actually set to make it (so we never queue a tech we can't finish). Extend as new science lines
    are built."""
    packs = {"automation-science-pack"}
    for a in api.entities("assembling-machine-1") + api.entities("assembling-machine-2"):
        rec = a.get("recipe")
        if rec in ("logistic-science-pack", "military-science-pack", "chemical-science-pack",
                   "production-science-pack", "utility-science-pack"):
            packs.add(rec)
    return packs


def _researched_set(api):
    techs = api.research().get("technologies") if isinstance(api.research().get("technologies"), dict) else {}
    return {k for k, v in techs.items() if isinstance(v, dict) and v.get("researched")}


def _completable(name, producible, researched):
    t = _TECHS.get(name)
    if t is None:
        return False
    if not set(t.science_packs).issubset(producible):
        return False
    return all((p in researched) or (p not in _TECHS) for p in t.prerequisites)


def _coal(api):
    return api.inventory("coal")


def keep_fueled(api):
    fueled = 0
    for name in ("boiler", "stone-furnace", "burner-mining-drill"):
        for e in api.entities(name):
            pos = e.get("position")
            if not isinstance(pos, dict):
                continue
            if api.entity_item_count(e, "coal") < 4 and _coal(api) > 6:
                api.move_to(pos["x"], pos["y"])
                r = api.insert("coal", 12, pos["x"], pos["y"])
                if isinstance(r, dict) and r.get("ok"):
                    fueled += 1
    return fueled


def _iron_furnaces(api):
    return [f for f in api.entities("stone-furnace") if isinstance(f.get("position"), dict) and (f["position"].get("y") or 0) > 50]


def smelt_iron(api):
    """Hand-carry iron-ore stockpiled in the iron drills into the iron furnaces so they make plates."""
    moved = 0
    furnaces = _iron_furnaces(api)
    if not furnaces:
        return 0
    for d in api.entities("burner-mining-drill"):
        pos = d.get("position")
        if not isinstance(pos, dict) or (pos.get("y") or 0) <= 50:
            continue
        ore = api.entity_item_count(d, "iron-ore")
        if ore <= 0:
            continue
        api.move_to(pos["x"], pos["y"])
        api.take("iron-ore", ore, pos["x"], pos["y"])
    api.refresh()
    have_ore = api.inventory("iron-ore")
    if have_ore > 0:
        # spread the ore across the iron furnaces
        per = max(1, have_ore // len(furnaces))
        for f in furnaces:
            fp = f["position"]
            api.move_to(fp["x"], fp["y"])
            api.insert("iron-ore", per, fp["x"], fp["y"])
            moved += per
    return moved


def gather_iron_and_craft(api):
    """Pull iron-plate out of the iron furnaces and hand-craft gears."""
    for f in _iron_furnaces(api):
        ip = api.entity_item_count(f, "iron-plate")
        if ip > 0:
            fp = f["position"]
            api.move_to(fp["x"], fp["y"])
            api.take("iron-plate", ip, fp["x"], fp["y"])
    api.refresh()
    iron = api.inventory("iron-plate")
    if iron >= 2:
        api.craft("iron-gear-wheel", iron // 2)
    api.refresh()
    return api.inventory("iron-gear-wheel")


def feed_science(api):
    """Top up copper from the copper furnace, then feed gears+copper into the science assembler."""
    if api.inventory("copper-plate") < 5:
        for f in api.entities("stone-furnace"):
            pos = f.get("position")
            if isinstance(pos, dict) and (pos.get("y") or 0) <= 50 and api.entity_item_count(f, "copper-plate") > 0:
                api.move_to(pos["x"], pos["y"])
                api.take("copper-plate", api.entity_item_count(f, "copper-plate"), pos["x"], pos["y"])
    api.refresh()
    g = api.inventory("iron-gear-wheel")
    cp = api.inventory("copper-plate")
    if g <= 0 and cp <= 0:
        return 0
    api.move_to(*SCIENCE_ASM)
    if g > 0:
        api.insert("iron-gear-wheel", g, *SCIENCE_ASM)
    if cp > 0:
        api.insert("copper-plate", cp, *SCIENCE_ASM)
    return g + cp


def deliver_science(api):
    api.move_to(*SCIENCE_ASM)
    api.take("automation-science-pack", 100, *SCIENCE_ASM)
    api.refresh()
    sci = api.inventory("automation-science-pack")
    if sci > 0:
        api.move_to(*LAB)
        api.insert("automation-science-pack", sci, *LAB)
    return sci


def ensure_research(api):
    producible = _producible_science(api)
    researched = _researched_set(api)
    cur = api.research().get("current") or api.research().get("current_research")
    # if the current research is completable with science we can make, leave it running
    if cur and _completable(cur, producible, researched):
        return cur
    techs = api.research().get("technologies") if isinstance(api.research().get("technologies"), dict) else {}
    notdone = {k for k, v in techs.items() if isinstance(v, dict) and not v.get("researched")}
    # Drive from RESEARCH_PREF via knowledge (the observation's tech list is a curated subset that omits
    # e.g. logistic-science-pack); "not researched" + completable is enough to queue it on the server.
    candidates = [t for t in RESEARCH_PREF if t not in researched and _completable(t, producible, researched)]
    candidates += [t for t in sorted(notdone) if t not in candidates and t not in researched and _completable(t, producible, researched)]
    for t in candidates:
        api.research_tech(t)
        api.refresh()
        if api.research().get("current") == t:
            return t
    return cur  # nothing completable with current science (need to build the next science tier)


def run_cycle(api):
    fueled = keep_fueled(api)
    smelted = smelt_iron(api)
    gears = gather_iron_and_craft(api)
    fed = feed_science(api)
    delivered = deliver_science(api)
    research = ensure_research(api)
    return {"fueled": fueled, "ore_to_furnace": smelted, "gears": gears, "fed": fed,
            "science_to_lab": delivered, "research": research,
            "researched": sum(1 for v in (api.research().get("technologies") or {}).values()
                              if isinstance(v, dict) and v.get("researched"))}


def main():
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    sleep_s = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    cfg = load_config()
    controller = ModlessFactorioController(cfg)
    player_name = controller._configured_agent_player_name()

    def _act(action):
        return controller._modless.act(action, player_name=player_name)

    api = FactorioApi(_act, controller.observe)
    log_path = Path("logs/fle-playbook.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path = Path("runtime/fle-playbook-heartbeat.json")
    hb_path.parent.mkdir(parents=True, exist_ok=True)

    i = 0
    while cycles <= 0 or i < cycles:
        i += 1
        try:
            api.refresh()
            summary = run_cycle(api)
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the loop
            summary = {"error": f"{type(exc).__name__}: {exc}"}
        rec = {"time": datetime.now(timezone.utc).isoformat(), "cycle": i, **summary}
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            hb_path.write_text(json.dumps(rec), encoding="utf-8")
        except OSError:
            pass
        print(json.dumps(rec, ensure_ascii=False))
        time.sleep(max(0.0, sleep_s))


if __name__ == "__main__":
    main()
