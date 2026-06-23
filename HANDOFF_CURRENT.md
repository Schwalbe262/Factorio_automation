# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; pivot is dual-track builder architecture with no-mod as primary path.
- Part 1 complete: added `build_block` substrate, `BuilderResult`, builder registry, no-mod validation, and Lua dispatch.
- Supported names: direct feed smelter, coal cluster, steam bank, mining/smelter/bus/assembly/labs, factory map, diagnose, repair.
- Lua `factory_map`/`diagnose_factory` are read-only diagnostics; concrete placement builders return `builder_not_implemented`.
- Validation: 28 targeted tests OK; 833 selected regression tests OK; full unittest discover passed 1312 tests.
- Full test emitted one existing socket `ResourceWarning` in controller tests; no failures.
- Token usage: unavailable because Codex state sqlite DB is malformed; weekly quota unavailable.
- Next: Part 2 implement `direct_feed_smelter_set`, `coal_bootstrap_cluster`, fuel batching, and obsolete teardown markers.
