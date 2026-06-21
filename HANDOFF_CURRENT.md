# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor PID `75296`, current autopilot PID `49792`, scheduler Qwen is active.
- Part170: circuit automation now falls back to a powered sidecar site when `automation_sites` is absent/stale.
- Part171: non-executable site-input routes now repair missing source prerequisites instead of reselecting the same route executor.
- Part172: copper/iron expansion fuel failures now route through direct plate recovery before repeating `expand_*_smelting`.
- Validation: `tests.test_planner tests.test_controller tests.test_strategy` passed 648; full `PYTHONPATH=src python -m unittest discover -s tests` passed 1148 with existing socket ResourceWarning.
- Live validation: `expand_copper_smelting` fuel loop broke; copper plate reached 121; `build_site_input_logistic_line` ran OK; latest live skill `produce_copper_plate` reports target reached.
- Current live: researched `5`, current research empty, key items include copper plate `121`, gears `149`, belts `25`, coal `19`, circuits `7`.
- Next blocker to watch: stop over-selecting copper once route is built, resume red-science/electric-mining-drill path, then retire burner mining.
- Operator layout traces are captured as pending/retracted review examples, not model-weight learning or auto-promoted skills yet; weekly quota unavailable.
