# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; Part 132 - local-LLM self-development (skill foundry) wired into the unattended loop.
- Startup context: read this file and targeted `goal.md`; never read `note.md`/`insight.md` in full.
- Codex/Claude are not required to run gameplay anymore: when strategy picks a skill with no executor, `skill_foundry.py` has the local Qwen author one, validated by Gate 1 (AST allowlist + `py_compile`), Gate 2 (offline replay), and Gate 3 (sandbox dry-run on a COPY of the live save), then registers it under `src/factorio_ai/generated_skills/`.
- `run_factorio_no_mod_unattended_llm.ps1` now starts/keeps-alive a 4th managed process `run-no-mod-skill-foundry-loop` (LLM-ready gated, `--require-idle`, sandbox gate ON), so the missing-skill queue is actually consumed unattended. Standalone: `run_factorio_no_mod_skill_foundry_loop.bat`.
- Dashboard has a new "Generated Skills (self-developed)" panel (registered / queue / failures / foundry heartbeat), bilingual EN/KR.
- A blocked strategy no longer stalls: it records codex-wait, enqueues the skill for the foundry, and redirects to a progressing skill.
- Validation: `PYTHONPATH=src python -m unittest discover -s tests` -> 739 passed.
- Next: run the unattended supervisor and watch `runtime/skill-foundry-loop.json` + the dashboard panel as Qwen authors real executors; raise foundry codegen to the 27B worker if 9B's success rate is low.
