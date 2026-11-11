# EXP-0003: Public blueprint reference import

- Date: 2026-11-11 (Asia/Seoul)
- Part: v2 blueprint research / bounded importer
- Goal: inspect small public examples for steam, smelting, and rail; establish a safe structural reader and identify useful early mall/science sources.
- Hypothesis: version-0 exchange strings can be decoded with hard byte/entity limits, and the live prototype catalog can separate unknown names from recognized ones without claiming a design works.
- Sources: [Autosaved Space Age blueprints](https://autosaved.org/factorio/blueprints); [FactorioBin post](https://factoriobin.com/post/1o4z16/31); [Refactorio saves](https://refactorio.de/saves); [Factorio Blueprint Tools](https://factorio.iampavel.dev/); [Factorio School Mall Hubs](https://www.factorio.school/view/-OQjrfHQQkJcwAV99o48).
- Actions: downloaded only three small exchange strings into ignored `runtime/reference-imports/`; implemented bounded decode, recursive book flattening, and catalog-aware structural summaries in `src/factorio_agent/blueprints.py`; added malformed, decompression-size, entity-limit, book, and real-asset tests.
- Candidates / metrics:
  - `smelting-station.txt`: 13,757 exchange characters; SHA-256 `33c37f4b70b6e928fd44b82a9fd11841e912011ce30b6764173a4241aabcd043`; label “Smelting Station”; 1,386 entities, including 96 electric furnaces, 156 rails, 9 roboports; all names found in 2.1.20 catalog.
  - `steam-power.txt`: 1,057 chars; SHA-256 `ad69c05d688313d715a94d0b0683fd8f5a332d88fdc72011c3709ef4accc55ca`; label “Steam Power (Tileable)”; 61 entities, including 2 chemical plants, 2 requester chests, and a lightning rod. This is not an early Nauvis starter plant.
  - `two-way-rail-turn.txt`: 681 chars; SHA-256 `e38e392c15a61dfe5ac467715bb51215c490fb96af25256000bf9ce25fabca3d`; label “2Way Turn Segment”; 24 entities, including rails/signals/roboport.
  - All summaries say `UNVERIFIED_STRUCTURE_ONLY`. Local runtime catalog: 2.1.20. Autosaved describes Space Age v3 and may still contain later-game infrastructure in early-design examples.
- Result: five focused tests pass, including the real imported smelting asset and oversized compressed/entity payload rejection. Sources for early mall/science were identified but not imported. No saves or large maps acquired.
- Failure reason: a likely “steam starter” candidate is instead Fulgora-specific; the obvious “smelting” sample is a late electric train station. Neither can safely stand in for early-game production.
- Next action: obtain genuinely early Nauvis belt/furnace, starter steam, and red/green science references, then validate placement and actual operation in Factorio 2.1.20 before using any as construction guidance.
- Token usage: unavailable from this worker runtime.

## Follow-up: early Nauvis science candidate

- Date: 2026-11-11 (Asia/Seoul)
- Source: [Autosaved Space Age blueprints](https://autosaved.org/factorio/blueprints), “Automation Science” section, “Assembling Machine 1” variant. The author describes the science designs as expandable and producing at the same rate.
- Imported exact exchange string to ignored `runtime/reference-imports/automation-science-am1.txt`; 3,521 characters; SHA-256 `8889235277c05fba7e4f0255e8cdc27a1f011eed1315a01b462ffb0f0faa6e6e`.
- Structural evidence: label `Automation Science AM1`, 303 entities; 6 assembler-1 (five automation-science recipe, one iron-gear-wheel), 30 labs, 135 transport belts, 22 underground belts, 18 inserters, 35 long-handed inserters, 22 fast inserters, 31 small electric poles, 2 splitters, 2 roboports. Two inserter filters name copper plate and iron plate. Every entity name appears in the local 2.1.20 catalog.
- Catalog supports candidate material chain: automation science recipe consumes copper plate + iron gear; iron gear recipe consumes iron plate. This supports copper/iron plate as candidate feed inputs and automation science packs as intended output. It does not prove the placed belt/inserter routing actually supplies them or that any rate is achieved.
- Substitutions / limits: page author says roboports are included for planning and can be omitted early; author also says efficiency modules are included in many Nauvis designs. Remove/omit modules if unavailable. This layout still contains fast and long-handed inserters and splitters; unlock availability depends on current research. Replacing them with ordinary inserters or simpler belts changes reach/routing and must be checked. Thus it is an early science reference, not a guaranteed first-hour starter blueprint.
- Result: added a focused real-asset test that checks label, assembler/lab counts, recipe assignments, and catalog resolution. Candidate marked `UNVERIFIED_STRUCTURE_ONLY`; no game placement or production test performed.

## Parent review and delivery

- Reader additionally rejects non-blueprint roots and excessive JSON recursion.
  Seven blueprint-reader tests pass locally, including two actual imported assets.
- Four original samples remain ignored; no saves or profiles were published.
- Child usage counters unavailable. Parent research/orchestration shares the
  measured implementation interval documented in EXP-0002; do not add it again
  or estimate an exact child/weekly-quota share.
