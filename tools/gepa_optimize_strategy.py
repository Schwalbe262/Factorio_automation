"""GEPA-based optimizer for the Factorio autopilot's STRATEGY prompt (gepa-ai/gepa).

Reflectively evolves the strategy *instruction text* to reduce infeasible skill picks. Telemetry
(logs/llm_decisions.jsonl) shows the deterministic feasibility guardrail ADJUSTS ~24% of the LLM's
strategy decisions ("LLM strategy adjusted by deterministic feasibility guardrail") -- i.e. the LLM
keeps choosing skills whose prerequisites don't exist. GEPA can rewrite the prompt to stop that.

OFFLINE METRIC (no game needed): a candidate prompt's chosen skill scores 1.0 if it SURVIVES
`reconcile_strategy_decision` (the same guardrail), else 0.0. Feedback = why the guardrail rejected it.
This makes evaluation deterministic + cheap; only the task LM (running candidate prompts) and the
reflection LM (proposing new prompts) need the model.

DATASET: logs/observation_samples.jsonl (banked live observations).

RUNNING:
  Dry-run (plumbing test, stub LM = the deterministic heuristic):
      python tools/gepa_optimize_strategy.py --dry-run
  Real optimization (needs an OpenAI-compatible endpoint for the 27B model, e.g. an SSH tunnel to the
  cluster vLLM). Do this when the autopilot is IDLE so it does not compete for the strategy GPU:
      FACTORIO_AI_LLM_BASE_URL=http://127.0.0.1:8000 FACTORIO_AI_LLM_MODEL=QuantTrio/Qwen3.6-27B-AWQ \
      python tools/gepa_optimize_strategy.py --run --max-metric-calls 150
  Output: the best evolved strategy_instructions -> paste into slurm_worker._strategy_prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from factorio_ai.strategy import (  # noqa: E402
    heuristic_strategy,
    make_strategy_payload,
    reconcile_strategy_decision,
    skill_catalog_payload,
)

OBJECTIVE = "launch_rocket_program"
_CATALOG = [s for s in skill_catalog_payload() if isinstance(s, dict) and s.get("name")]
ALLOWED = [str(s["name"]) for s in _CATALOG]

# The component GEPA evolves. Seeded from the spirit of the current slurm_worker._strategy_prompt.
SEED_INSTRUCTIONS = (
    "You are the strategic layer for a Factorio autoplayer building toward a rocket. "
    "Choose the single next high-level skill from the allowed list. Prefer a skill whose "
    "prerequisites already exist in the observation so it can act this cycle; do not pick a skill "
    "that would stall because its inputs/structures are missing."
)


def _summary(obs: dict) -> str:
    inv = obs.get("inventory") or {}
    ents = obs.get("entities") or []
    names = {}
    for e in ents:
        if isinstance(e, dict):
            names[e.get("name")] = names.get(e.get("name"), 0) + 1
    rs = obs.get("research") or {}
    auto = (rs.get("technologies") or {}).get("automation") or {}
    keep = ("boiler", "steam-engine", "assembling-machine-1", "lab", "burner-mining-drill",
            "stone-furnace", "transport-belt", "electric-mining-drill")
    mach = {k: names[k] for k in keep if names.get(k)}
    return (
        f"automation_researched={bool(auto.get('researched'))} "
        f"machines={mach} "
        f"inv_iron_plate={inv.get('iron-plate')} inv_copper_plate={inv.get('copper-plate')} "
        f"inv_gears={inv.get('iron-gear-wheel')} inv_coal={inv.get('coal')}"
    )


def _llm_select_skill(instructions: str, obs: dict) -> str:
    """Run a candidate prompt to pick a skill. Uses an OpenAI-compatible endpoint if configured,
    else a deterministic heuristic stub (for --dry-run plumbing tests)."""
    base = os.getenv("FACTORIO_AI_LLM_BASE_URL", "").strip()
    if not base:
        h = heuristic_strategy(OBJECTIVE, obs, {})
        return str(h.get("selected_skill") or h.get("selected_goal") or "")
    payload = make_strategy_payload(OBJECTIVE, obs, {})
    prompt = (
        instructions
        + "\nReturn STRICT JSON only: {\"selected_skill\": \"<one of allowed>\"}."
        + "\nAllowed skills: " + ", ".join(ALLOWED)
        + "\nObservation payload: " + json.dumps(payload, ensure_ascii=False)[:7000]
    )
    body = json.dumps({
        "model": os.getenv("FACTORIO_AI_LLM_MODEL", "default"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 120,
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 - configured endpoint
        data = json.loads(resp.read())
    content = str(data["choices"][0]["message"]["content"])
    m = re.search(r'"selected_skill"\s*:\s*"([^"]+)"', content)
    return m.group(1) if m else ""


def _score(obs: dict, chosen: str) -> tuple[float, str]:
    """1.0 if the chosen skill survives the feasibility guardrail (would act), else 0.0 + feedback."""
    if not chosen:
        return 0.0, "no skill chosen (invalid/empty LLM output)"
    if chosen not in ALLOWED:
        return 0.0, f"'{chosen}' is not an allowed skill name; choose only from the allowed list"
    decision = {"selected_skill": chosen, "source": "llm", "reason": "", "blockers": [],
                "evidence": [], "expected_effect": ""}
    reconciled = reconcile_strategy_decision(decision, OBJECTIVE, obs, {})
    final = str(reconciled.get("selected_skill") or "")
    if final == chosen:
        return 1.0, "feasible: the guardrail kept this pick"
    return 0.0, (
        f"picked '{chosen}' but the feasibility guardrail switched to '{final}' "
        f"(prereqs missing/blocked): {str(reconciled.get('reason') or '')[:140]}"
    )


def _build_adapter():
    import gepa
    from gepa.core.adapter import EvaluationBatch, GEPAAdapter

    class StrategyAdapter(GEPAAdapter):
        def evaluate(self, batch, candidate, capture_traces=False):
            instr = candidate["strategy_instructions"]
            outputs, scores, trajs = [], [], []
            for obs in batch:
                try:
                    chosen = _llm_select_skill(instr, obs)
                except Exception as exc:  # noqa: BLE001
                    chosen = ""
                score, feedback = _score(obs, chosen)
                outputs.append(chosen)
                scores.append(score)
                trajs.append({"summary": _summary(obs), "chosen": chosen, "feedback": feedback})
            return EvaluationBatch(
                outputs=outputs, scores=scores,
                trajectories=trajs if capture_traces else None, objective_scores=None,
            )

        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            rows = []
            for traj, score in zip(eval_batch.trajectories or [], eval_batch.scores):
                rows.append({
                    "Inputs": traj["summary"],
                    "Generated Outputs": traj["chosen"],
                    "Feedback": f"score={score}: {traj['feedback']}",
                })
            return {"strategy_instructions": rows}

    return gepa, StrategyAdapter()


def _load_samples(limit: int) -> list[dict]:
    path = ROOT / "logs" / "observation_samples.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:] if limit else rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="plumbing test of the adapter+metric (stub LM)")
    ap.add_argument("--run", action="store_true", help="run the GEPA optimization (needs LLM env)")
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--max-metric-calls", type=int, default=150)
    args = ap.parse_args()

    samples = _load_samples(args.samples)
    print(f"loaded {len(samples)} observation samples; {len(ALLOWED)} allowed skills")
    gepa, adapter = _build_adapter()

    if args.dry_run or not args.run:
        batch = samples[: min(10, len(samples))]
        result = adapter.evaluate(batch, {"strategy_instructions": SEED_INSTRUCTIONS}, capture_traces=True)
        feasible = sum(1 for s in result.scores if s >= 1.0)
        print(f"DRY-RUN seed prompt feasibility: {feasible}/{len(result.scores)} "
              f"(stub LM = heuristic; this validates the adapter+metric plumbing)")
        for traj, sc in list(zip(result.trajectories, result.scores))[:4]:
            print(f"  score={sc} chose={traj['chosen']!r} :: {traj['feedback'][:90]}")
        if not args.run:
            print("\n(use --run with FACTORIO_AI_LLM_BASE_URL set to evolve the prompt for real)")
            return

    train = samples[: int(len(samples) * 0.75)]
    val = samples[int(len(samples) * 0.75):]
    reflection_lm = os.getenv("FACTORIO_AI_LLM_MODEL", "default")
    print(f"running GEPA: train={len(train)} val={len(val)} max_metric_calls={args.max_metric_calls}")
    res = gepa.optimize(
        seed_candidate={"strategy_instructions": SEED_INSTRUCTIONS},
        trainset=train, valset=val, adapter=adapter,
        reflection_lm=reflection_lm, max_metric_calls=args.max_metric_calls,
    )
    print("\n=== BEST EVOLVED strategy_instructions ===\n")
    print((res.best_candidate or {}).get("strategy_instructions"))


if __name__ == "__main__":
    main()
