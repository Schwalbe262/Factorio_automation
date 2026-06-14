# Training Trace Archive

The project keeps raw gameplay logs under `logs/` and generated trace bundles under `runtime/trace_archives/`.
Both directories are intentionally ignored by Git. Git tracks the exporter, tests, and this operating procedure.

## Why This Exists

- Layout-improvement loops are future GEPA and Qwen LoRA training data.
- Strategy failures are useful negative examples when the selected skill was wrong or an executor was missing.
- Sandbox layout validation is supervised evidence for "build-ready" versus "do not build".
- `note.md` and `insight.md` add human context that raw JSONL traces do not always capture.
- Human/operator factory edits must be preserved as before/after comparison data. The agent should only accept those edits as insights when measured layout quality improves.

## Archive Command

```powershell
python -m factorio_ai.cli archive-training-traces --label "part75-scattered-map-traces"
```

Useful variants:

```powershell
python -m factorio_ai.cli archive-training-traces --label "dry-run-manifest" --no-copy-raw --limit 20
python -m factorio_ai.cli trace-archive-summary
```

Default output:

```text
runtime/trace_archives/YYYYMMDD-HHMMSS-<label>/
  manifest.json
  index.jsonl
  README.md
  raw/
```

## High-Value Sources

- `logs/layout-improvement-background.jsonl`
- `logs/layout-validation-feedback.jsonl`
- `logs/strategy-layout-*.jsonl`
- `logs/strategy-*.jsonl`
- `logs/llm_decisions.jsonl`
- `logs/run-notes.jsonl`
- `logs/run-insights.jsonl`
- `note.md`
- `insight.md`
- future `logs/operator-intervention-*.jsonl`
- future `logs/manual-layout-*.jsonl`

## Fine-Tuning Conversion Rules

- Keep raw traces immutable once archived; create separate derived datasets for GEPA or LoRA.
- Redact credentials, local paths that should not leave the machine, and large blueprint strings unless the dataset explicitly needs them.
- Preserve failures with the reason and next action. Failed layouts are useful counterexamples.
- Promote an `insight.md` entry only after a metric improves, such as throughput per tile, bottleneck removal, lower power draw, lower pollution, shorter logistics distance, or sandbox validation pass.
- For human edits, compare the agent's previous layout snapshot with the changed layout before accepting the intervention as a reusable lesson.

## Human Intervention Comparison

When a person edits an agent-built site:

1. Save the agent's pre-edit site snapshot.
2. Save the post-edit site snapshot after the human change.
3. Measure compactness, rate, input/output bottlenecks, energy demand, pollution, and expansion corridors.
4. Write a loop note with actions and metrics.
5. Append an insight only if the post-edit design is measurably better or fixes a confirmed blocker.
6. Archive the comparison as `logs/operator-intervention-*.jsonl` or `logs/manual-layout-*.jsonl`.

The current no-custom-mod observation path does not yet populate `factory_events`, so automatic human-edit detection still needs an executor/snapshot feature. Until that exists, use explicit before/after snapshots and archive them with the naming above.
