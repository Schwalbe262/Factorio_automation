# Factorio Automation CLI Handoff

Last updated: 2026-06-14 19:55 KST
Repository: `C:\Users\NEC\Documents\Factorio`
GitHub: `https://github.com/Schwalbe262/Factorio_automation`
Current branch: `master`
Latest Part 66 feature commit at this handoff: `c3f7c5b Part 66: sandbox validate layout candidates`

## Goal

Build a vanilla-compatible Factorio autoplayer that can eventually launch a rocket under LLM high-level control.

The intended control split is:

- LLM: high-level strategy, bottleneck reasoning, site layout critique, candidate layout design, failure analysis.
- Deterministic skills: exact walking, mining, crafting, building, inserter/belt placement, power connection, research execution.
- Web monitor: production targets, estimated rates, site graph, logistics links, threats, power networks, token usage, LLM logs, layout candidates.
- Slurm worker: run local Qwen models and background layout simulations while the deterministic executor is busy.

Important user preferences:

- Show GUI demonstrations when needed, but headless/no-GUI is acceptable for fast implementation tests.
- Vanilla/no-mod compatibility is preferred for multiplayer compatibility. Current path uses no-custom-mod RCON/Lua command execution rather than a Factorio mod folder.
- Do not use instant teleport/mining for GUI demos; GUI should look like real gameplay.
- User wants `/factorio` and `/팩토리오` links from Telegram/Kakao bots, with external URL `http://27.115.156.173:8787/factorio?lang=ko&objective=launch_rocket_program`.
- Completed parts should be committed and pushed to GitHub.
- Each completed task should mention approximate Codex token usage delta.

## Current Runtime State

Processes observed before this handoff:

- No-mod Factorio server wrapper: `python -m factorio_ai.cli start-no-mod-server`
- Factorio server: `factorio.exe --start-server ... --rcon-port 27015 --rcon-password factorio-ai`
- Web dashboard: `python -m factorio_ai.cli web --host 0.0.0.0 --port 18889`
- Local web URL: `http://127.0.0.1:18889/factorio?lang=ko&objective=launch_rocket_program`
- External web URL through existing forwarding/proxy: `http://27.115.156.173:8787/factorio?lang=ko&objective=launch_rocket_program`

If the CLI session starts fresh, verify processes first:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'factorio.exe|python.exe' -and ($_.CommandLine -like '*factorio*' -or $_.CommandLine -like '*Factorio*') } |
  Select-Object ProcessId,Name,CommandLine | Format-List
```

Start web dashboard if missing:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli web --host 0.0.0.0 --port 18889
```

Start no-mod server if missing:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli start-no-mod-server
```

## Recent Completed Work

Part 64 introduced:

- Background idle layout loop so LLM/Slurm can keep doing simulation-only site layout work while deterministic skills are blocked or idle.
- Before/after blueprint copy buttons for layout simulation candidates.
- `/factorio/blueprint` and `/api/factorio/blueprint` copy endpoints.
- External copy smoke tests against `27.115.156.173:8787`.

Part 65 fixed the latest blueprint issues and is already pushed:

- `src/factorio_ai/monitor.py`
  - Site blueprint export now filters entities by site kind.
  - Circuit site blueprints no longer pull nearby steam power, labs, or unrelated machines just because they are spatially near.

- `src/factorio_ai/planner.py`
  - Before-blueprint exports now use a compact representative local cluster when candidate sites are spread far apart.
  - This prevents the "tiny entities in a huge empty blueprint preview" problem.
  - Green-circuit simulation candidate now includes a static operability report.
  - Green-circuit candidate changed to a belt-mediated 3 cable / 2 circuit pattern with output chests.

- `src/factorio_ai/slurm_worker.py`
  - Compact layout payload sent to LLM now includes candidate `validation`.
  - Heuristic layout selection avoids static validation failures and surfaces validation errors as risks.
  - Rules explicitly tell the LLM to treat validation failures as feedback and not mark failed designs build-ready.

- `src/factorio_ai/web_dashboard.py`
  - Layout candidate cards now show validation status (`pass`, `warning`, `fail`) and checked machine count.

- Tests updated:
  - Green-circuit before blueprint does not include power-site entities.
  - Smelting before blueprint uses a compact representative cluster.
  - Green-circuit candidate static validation passes.
  - Dashboard renders validation status.

Part 66 work implements the sandbox validation path:

- `src/factorio_ai/layout_validation.py`
  - New disposable sandbox validator for simulation candidate blueprints.
  - Decodes the candidate blueprint, places entities on a temporary surface, powers the sandbox with substations/solar plus a pole grid, feeds inferred terminal inputs onto belts, waits real server ticks, inspects machine/inserter status, observed items, power, build failures, and output counts, then cleans up the surface.
  - Writes feedback rows to `logs/layout-validation-feedback.jsonl`.
  - Merges latest sandbox feedback back into layout candidate dictionaries as `sandbox_validation`, timestamp, and lesson.

- `src/factorio_ai/cli.py`
  - Adds:

    ```powershell
    $env:PYTHONPATH='src'
    python -m factorio_ai.cli validate-layout-candidate --candidate-id green-circuit-3-cable-2-circuit-cell --variant after --ticks 3600 --player r1jae
    ```

  - The command exits nonzero when the sandbox result is `fail`; this is intentional so CI/operators notice failed candidates.

- `src/factorio_ai/controller.py`, `src/factorio_ai/remote_slurm.py`, `src/factorio_ai/slurm_worker.py`
  - Background layout requests now include compact `layout_validation_feedback`.
  - Slurm compact payloads include candidate `sandbox_validation`, recent lesson text, observed outputs, machine count, inserter count, and failure reasons.
  - Heuristic layout selection avoids sandbox-validation failures and surfaces sandbox reasons as risks.

- `src/factorio_ai/web_dashboard.py`
  - Dashboard candidate cards now show a separate `Sandbox` validation row when feedback exists.
  - The dashboard state merges latest rows from `logs/layout-validation-feedback.jsonl` into current candidates.

- Tests added/updated:
  - `tests/test_layout_validation.py`
  - `tests/test_slurm_worker.py`
  - `tests/test_web_dashboard.py`

Current Part 66 live finding:

- Static validation still reports `green-circuit-3-cable-2-circuit-cell` as `pass`.
- Sandbox validation reports `fail`.
- Latest live smoke wrote this useful lesson to `logs/layout-validation-feedback.jsonl`:
  - expected output `electronic-circuit` was not observed.
  - sandbox fed `copper-plate` and `iron-plate`, all 5 assemblers and all 12 inserters were powered.
  - inserters still waited for source items, so pickup lane, inserter orientation, or belt side is likely unreachable.
  - Do not mark this green-circuit candidate build-ready until a revised layout passes sandbox ticks.

## Verification Already Run

Focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_planner.py::PlannerTests::test_factory_layout_simulates_green_circuit_pattern_without_applying_it `
  tests\test_planner.py::PlannerTests::test_green_circuit_before_blueprint_does_not_pull_adjacent_power_site `
  tests\test_planner.py::PlannerTests::test_smelting_before_blueprint_uses_compact_representative_cluster `
  tests\test_web_dashboard.py::WebDashboardTests::test_dashboard_html_has_monitor_sections_and_item_icons
```

Result: `4 passed`

Broader tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_planner.py tests\test_web_dashboard.py tests\test_blueprints.py
```

Result: `130 passed`

Full test suite after the final Slurm payload tweaks:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `314 passed`

Part 66 focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_layout_validation.py tests\test_slurm_worker.py tests\test_web_dashboard.py
```

Result: `19 passed`

Part 66 full test suite:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `317 passed`

Part 66 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 35141439 --label "part66 sandbox layout validation"
```

Result: appended `delta_tokens=234991` to `logs/token_usage.jsonl`, which is read by the web dashboard token graph.

## Live Smoke Checks Already Run

Dashboard:

```powershell
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18889/factorio?lang=ko&objective=launch_rocket_program'
Invoke-WebRequest -UseBasicParsing 'http://27.115.156.173:8787/factorio?lang=ko&objective=launch_rocket_program'
```

Both returned HTTP `200`.

Current generated candidate inspection from dashboard state:

- `copper-plate-parallel-smelting-columns` before blueprint: `15` entities, width `24.0`, height `3.0`.
- `green-circuit-3-cable-2-circuit-cell` validation:
  - status `pass`
  - checked machines `5`
  - errors `[]`
  - after blueprint entities include `assembling-machine-1`, `inserter`, `iron-chest`, `small-electric-pole`, `transport-belt`
  - before blueprint names include `assembling-machine-1`, `inserter`, `small-electric-pole`
  - before blueprint no longer includes boiler/steam-engine/offshore-pump.

## Important Caveat: Blueprint Validation

Part 66 adds a first sandbox validation path, but it is still an early validator, not a complete production simulator.

Completed now:

1. Create a disposable sandbox validation path for blueprint candidates: done.
2. Import/place a candidate blueprint into a temporary isolated surface: done.
3. Feed controlled input items: done for inferred terminal inputs on source belt lanes.
4. Run ticks: done by waiting real server ticks.
5. Inspect machine status, item movement, output counts, power, collision/buildability, and belt/inserter throughput: partially done. Current output includes build failures, observed outputs, input insertions, machine statuses, inserter statuses, powered machine/inserter counts, and ticks.
6. Write results to `logs/layout-validation-feedback.jsonl`: done.
7. Add failed candidate feedback to future LLM payloads: done for background layout Slurm payloads.
8. Convert validation feedback into fine-tuning examples later: not done.

Proposed feedback JSONL shape:

```json
{
  "timestamp": "2026-06-14T00:00:00+09:00",
  "candidate_id": "green-circuit-3-cable-2-circuit-cell",
  "variant": "after",
  "static_validation": {"status": "pass"},
  "sandbox_validation": {
    "status": "fail",
    "reasons": [
      "electronic-circuit assembler at x=6,y=1 never received iron plate",
      "output inserter has no reachable sink"
    ],
    "observed_outputs": {"electronic-circuit": 0},
    "ticks": 3600
  },
  "lesson": "Do not mark green circuit layouts build-ready unless both iron-plate input and copper-cable transfer paths are proven by sandbox ticks."
}
```

This file can later be transformed into Qwen fine-tuning examples.

## Next Implementation Priority

If Part 66 is not yet committed/pushed, finish it first:

1. Record token usage for Part 66 with `python -m factorio_ai.cli record-token-usage ...`.
2. Commit and push Part 66:

```powershell
git add src/factorio_ai/layout_validation.py src/factorio_ai/cli.py src/factorio_ai/controller.py src/factorio_ai/remote_slurm.py src/factorio_ai/slurm_worker.py src/factorio_ai/web_dashboard.py tests/test_layout_validation.py tests/test_slurm_worker.py tests/test_web_dashboard.py docs/CLI_HANDOFF.md
git commit -m "Part 66: sandbox validate layout candidates"
git push origin master
```

Then use the new sandbox feedback to fix the green-circuit candidate itself:

1. Revise `green-circuit-3-cable-2-circuit-cell` so source belts, belt sides, inserter pickup positions, direct/belt cable transfer, output inserters, and power coverage actually produce `electronic-circuit` in sandbox ticks.
2. Rerun:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli validate-layout-candidate --candidate-id green-circuit-3-cable-2-circuit-cell --variant after --ticks 3600 --player r1jae
```

3. Only mark the candidate build-ready after sandbox validation passes.

## Core Commands

Start a fresh Codex CLI session from this handoff, without relying on the Desktop conversation:

```powershell
.\continue_factorio_cli.bat
```

The start prompt used by the bat is:

```text
Read docs\CLI_HANDOFF.md only as the handoff context. Do not assume the previous desktop conversation is available. Continue the Factorio automation project from that document: verify current git status, run tests, commit/push the current validated changes if still uncommitted, then implement the next highest-priority item described in the handoff.
```

Observe current game:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli no-mod-observe --player r1jae
```

Run strategy:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli no-mod-strategy --objective launch_rocket_program
```

Run one no-mod autopilot loop:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli run-no-mod-strategy --objective launch_rocket_program --max-steps 1
```

Run idle layout loop once:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli run-no-mod-idle-layout-loop --objective launch_rocket_program --cycles 1 --sleep-seconds 0 --stale-seconds 0
```

Open review GUI:

```powershell
.\run_factorio_watch_gui.bat
```

## Known Design Issues To Keep In Mind

- Factory sites have historically been too scattered. Site placement must prefer starter-local clusters until rail logistics exist.
- Power was previously placed too far from spawn, causing long pole corridor problems.
- Production blocks must avoid covering starter ore/coal patches unless unavoidable.
- Research automation must use assemblers and labs; hand-crafting science packs is not acceptable for sustained progress.
- Labs usually need daisy chain or belt-fed science distribution.
- Burner drills are only bootstrap; later replace with electric drills.
- Boiler fuel should become coal belt plus inserter, not manual coal insertion.
- Defense must build gun turrets plus firearm magazine supply around factory sites before trying to clear nests.
- Threat logic should distinguish nearby nests, pollution-triggered attacks, recent damage, and actual factory danger.
- Logistics links are site-to-site links, not individual belt segment links.
- Trains should be used for far resources/outposts once local logistics become too long.

## LLM Model Direction

User wants latest Qwen models where feasible:

- Prefer Qwen 3.5 / 3.6 family over Qwen 2.5.
- Compare smaller models first when GPU3 is busy.
- Keep a GPU3 Slurm job queued for larger 27B/28B FP8 model attempts.
- Do not exclude `n077` permanently; failures may be temporary.
- AUTO Slurm job should request up to 3 GPUs if needed, but smaller models can run on less busy GPU resources.

The LLM should receive current game state, production targets, monitor bottlenecks, site graph, layout issues, candidate validation, and failure feedback. It should not receive only a generic "how do I launch a rocket" prompt.

## Web Monitor Expectations

Dashboard should include:

- Desired production targets and user-editable rates.
- Estimated production / consumption / deficits.
- Factory sites grouped by site, not individual entities.
- Site-to-site logistics links.
- Power networks and unconnected consumers.
- Threats / defense and recent damage.
- LLM decision logs.
- Codex token usage graph with true time-axis spacing and KST timestamps.
- Layout issues, opportunities, simulation candidates, and before/after blueprint copy buttons.
- Candidate validation status and later sandbox validation results.

## Git Hygiene

Before finishing any CLI continuation:

```powershell
git status --short
$env:PYTHONPATH='src'; pytest -q
python -m factorio_ai.cli record-token-usage --tokens-used <goal_tokens_used> --label "partXX short label"
git add ...
git commit -m "Part XX: concise description"
git push origin master
```

Never revert user changes unless explicitly requested.
