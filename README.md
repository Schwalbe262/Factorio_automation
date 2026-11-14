# Factorio Space Age Autoplayer v2

Active development: `v2`. The previous implementation is preserved on the `v1` branch.

The first v2 goal is an autonomous, resource-backed Space Age rocket launch.
Routine decisions (roadmap order, capacity, research, retries) are rules in
code; a model (GPT-6 Luna, `controller.planner: luna`) is consulted only for
incidents, and `planner: none` runs without any model.
The game uses the default map/enemy rules and the installed 2.1.20 expansion.

Start from [AGENTS.md](AGENTS.md), [HANDOFF_CURRENT.md](HANDOFF_CURRENT.md),
[HANDOFF_DETAIL.md](HANDOFF_DETAIL.md) and [goal.md](goal.md).
V2 is under construction; an autonomous v2 rocket launch is **not verified**.

Capabilities are listed in `CAPABILITIES` (`src/factorio_agent/jobs.py`) and
ordered by `ROADMAP` (`src/factorio_agent/progression.py`). Fleets of fresh seeds reach the smelting district and the main bus; one map is
proven through plastic (oil, engines, remote smelting); later stages are unproven.
Maintainer documentation: [docs/v2/README.md](docs/v2/README.md).

Public blueprint examples and the bounded structural importer are documented in
[reference sources](docs/v2/REFERENCE_SOURCES.md). Imported examples remain
unverified until tested in the pinned game; original exchange strings stay ignored.

On Windows, open [Factorio.bat](Factorio.bat). Its Korean menu starts or resumes
automation, pauses the game and AI, creates a saved/backed-up new map, opens the
GUI, shows status/logs, edits settings, and saves/shuts down. Closing only the
menu keeps the current game state running. `Factorio.bat --help` prints the menu
without starting the game. See [controls](docs/v2/LIFECYCLE.md) and
[editable settings](docs/v2/AUTOMATION_CONFIG.md).

The seven individual command wrappers are in [tools/windows](tools/windows/README.md);
the root has one BAT entry point.

```powershell
$env:PYTHONPATH = 'src;tests;runtime/v2/lua-test-deps'
python -B -m factorio_agent.lifecycle --repo . status
python -B -m unittest discover -s tests -p 'test_v2_*.py' -q
```

New worlds use `create`, `start`, then `connect-player`; choose another runtime
directory and distinct ports. Never replace a running save to inspect references.
