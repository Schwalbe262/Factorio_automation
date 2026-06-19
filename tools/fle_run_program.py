"""Run a single Claude-authored FLE program against the LIVE no-mod RCON game.

This is the "Claude-as-program-generator" harness from the approved plan: it binds the proven
FactorioApi + run_program sandbox (src/factorio_ai/code_agent.py) to the live ModlessFactorioController
(same act/observe RCON path the autopilot uses), runs ONE program file, and prints a compact
before/after live digest so the next program can be written from ground truth.

Usage:
    PYTHONPATH=src python tools/fle_run_program.py <program.py> [timeout_seconds]

It deliberately does NOT loop or call any cluster LLM -- Claude writes each program; this just
executes it and reports. Repeatable per program.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from factorio_ai.code_agent import FactorioApi, run_program
from factorio_ai.config import load_config
from factorio_ai.controller import ModlessFactorioController

SKILL_TOOLS = [
    "produce_iron_plate", "produce_copper_plate", "setup_coal_supply", "setup_power",
    "research_automation", "bootstrap_build_item_mall", "build_gear_belt_mall_logistics",
    "build_iron_plate_logistic_line_to_gear_mall", "expand_iron_smelting",
    "produce_electronic_circuit", "research_logistics",
]


def _digest(obs: dict) -> str:
    lines = []
    player = obs.get("player") if isinstance(obs.get("player"), dict) else {}
    pos = player.get("position") if isinstance(player.get("position"), dict) else {}
    lines.append(f"tick={obs.get('tick')} agent@({pos.get('x')},{pos.get('y')}) virtual={player.get('character_valid') is False}")
    inv = obs.get("inventory") if isinstance(obs.get("inventory"), dict) else {}
    top = ", ".join(f"{k}:{v}" for k, v in sorted(inv.items(), key=lambda kv: -kv[1])[:14]) or "(empty)"
    lines.append(f"inventory: {top}")
    ents = obs.get("entities") if isinstance(obs.get("entities"), list) else []
    counts = Counter(str(e.get("name")) for e in ents if isinstance(e, dict))
    keys = ("assembling-machine-1", "assembling-machine-2", "lab", "boiler", "steam-engine",
            "offshore-pump", "stone-furnace", "burner-mining-drill", "electric-mining-drill",
            "transport-belt", "small-electric-pole", "rocket-silo")
    lines.append("entities: " + ", ".join(f"{k}:{counts[k]}" for k in keys if counts.get(k)))
    for e in ents:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "")
        if name.startswith("assembling-machine") or name in ("lab", "boiler", "steam-engine", "rocket-silo"):
            st = e.get("status_name") or e.get("status")
            extra = f" recipe={e.get('recipe')}" if e.get("recipe") else ""
            lines.append(f"  {name} @({e['position'].get('x')},{e['position'].get('y')}) status={st}{extra}" if isinstance(e.get("position"), dict) else f"  {name} status={st}{extra}")
    drills = [e for e in ents if isinstance(e, dict) and str(e.get("name")) == "burner-mining-drill"]
    dstat = Counter(str(d.get("status_name") or d.get("status")) for d in drills)
    if drills:
        lines.append("burner-drills: " + ", ".join(f"{k}:{v}" for k, v in dstat.items()))
    fur = [e for e in ents if isinstance(e, dict) and str(e.get("name")) == "stone-furnace"]
    fstat = Counter(str(f.get("status_name") or f.get("status")) for f in fur)
    if fur:
        lines.append("stone-furnaces: " + ", ".join(f"{k}:{v}" for k, v in fstat.items()))
    research = obs.get("research") if isinstance(obs.get("research"), dict) else {}
    lines.append(f"research: current={research.get('current') or research.get('current_research')}")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python tools/fle_run_program.py <program.py> [timeout_seconds]")
        raise SystemExit(2)
    program = Path(sys.argv[1]).read_text(encoding="utf-8")
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0

    cfg = load_config()
    controller = ModlessFactorioController(cfg)
    player_name = controller._configured_agent_player_name()

    def _act(action):
        return controller._modless.act(action, player_name=player_name)

    skill_tools = [s for s in SKILL_TOOLS if controller._skill_run_config(s) is not None]

    def _run_skill(name, max_steps):
        if name not in skill_tools:
            return {"ok": False, "reason": f"unknown skill '{name}'", "skill": name}
        try:
            summary = controller.run_strategy_step(override_skill=name, max_steps=int(max_steps))
            return {"ok": bool(summary.ok), "reason": str(summary.reason)[:200], "skill": name}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "skill": name}

    api = FactorioApi(_act, controller.observe, run_skill=_run_skill, skill_names=skill_tools)

    print("=== BEFORE ===")
    print(_digest(api.refresh()))
    print("\n=== PROGRAM ===")
    print(program.strip()[:2000])
    result = run_program(program, api, timeout_seconds=timeout)
    print("\n=== RESULT ===")
    print(f"ok={result.ok} actions_run={result.actions_run}")
    if result.output.strip():
        print("--- output ---\n" + result.output[:4000])
    if result.error.strip():
        print("--- error ---\n" + result.error[:4000])
    print("\n=== AFTER ===")
    print(_digest(api.refresh()))


if __name__ == "__main__":
    main()
