# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor PID `75296`, autopilot PID `74328`, vLLM service `QuantTrio/Qwen3.6-27B-AWQ` id `12917`.
- Part189 fixed the post-Automation iron loop: active expanded iron smelting now causes `produce_iron_plate` to wait/yield instead of walking to distant direct coal, and fuel-logistics blocks route to `connect_coal_fuel_feed`.
- Part189 also fixed strategy guardrail overreach: stale no-fuel direct iron drills no longer preempt `automate_electronic_circuit_line` when fueled expanded iron smelting is active.
- Live guardrail smoke now maps `plan_factory_site` + nonexecutable copper site input to `automate_electronic_circuit_line`, not `produce_iron_plate`.
- Live after restart: Qwen/LLM selected `automate_electronic_circuit_line`; entities rose `808 -> 909`; latest skill stopped at step 46 yielding on starter stone output while coal fuel feed repair is the next blocker.
- Human layout learning is already wired: operator deltas go to `operator-intervention-layout-learning.jsonl` as pending/retracted candidates; direct gear-to-belt transfer feature extraction tests pass.
- Validation: targeted iron/strategy tests OK; `tests.test_planner` 442 OK; `tests.test_strategy` 166 OK; controller+human-layout+run-health 106 OK; full discover 1200 OK with existing `controller.py:1277` ResourceWarning.
- Token sample: `28,334,215` absolute, `624,176` since Part188 sample; weekly quota unavailable.
- Dirty archives: `note.md`/`insight.md` have large pre-existing append-only changes; append only concise events and avoid staging wholesale.
