# Public blueprint references (v2)

These sources are research leads and structural examples. A decoded blueprint is **UNVERIFIED** until it is placed in the pinned game and its connections, recipes, technology availability, and production are checked.

## Sources inspected

| Topic | Public source | What it actually provides / caveat |
|---|---|---|
| Steam power | [Autosaved: Space Age blueprints](https://autosaved.org/factorio/blueprints), “Steam Power” | Fulgora-oriented tileable design, not a starter Nauvis plant: sample includes lightning-rod, requester chests, and chemical plants. Author says chests/roboports may be present in early-game prints as planning aids. |
| Scalable smelting | [Autosaved: Space Age blueprints](https://autosaved.org/factorio/blueprints), “Smelting Station” | Train-fed electric smelting station, not an early belt-only furnace array. Sample has 96 electric furnaces, 156 rails, 9 roboports and 1,386 total entities. Author notes roboports and provider chests may be optional/early-inaccessible. |
| Early mall / science | [Factorio Blueprint Tools & Calculators](https://factorio.iampavel.dev/) and [Factorio Prints: Mall Hubs (Space Age)](https://www.factorio.school/view/-OQjrfHQQkJcwAV99o48) | Candidate research sources for early, belt-fed mall/science layouts; not imported into this experiment. Confirm tech and recipes in game before use. |
| Nauvis red science | [Autosaved: Space Age blueprints](https://autosaved.org/factorio/blueprints), “Automation Science” → “Assembling Machine 1” | Imported `Automation Science AM1`, 303 entities. Five assemblers are set to automation science, one to iron gears, with 30 labs. Two item filters point at copper plate and iron plate. The live catalog recipe confirms red science consumes copper plate + iron gear; gear consumes iron plate. This is a plausible Nauvis red-science layout, but not a minimal start and not live-validated. |
| Rail | [Autosaved: Space Age blueprints](https://autosaved.org/factorio/blueprints), “Two-way Rail Turn” | Small reusable turn segment. The page also describes early two-way rail as cheaper with modest train throughput; this piece alone is not a complete working rail network. |
| Additional archive | [FactorioBin post 1o4z16/31](https://factoriobin.com/post/1o4z16/31), [Refactorio saves](https://refactorio.de/saves) | Candidate raw blueprint and save sources. Save files can represent scenarios or large maps, so inspect metadata and size before acquiring; no save was downloaded. |

## Local ignored samples

The three `.txt` samples under `runtime/reference-imports/` are intentionally ignored by git. Each is the exact public exchange string from the named Autosaved page section. SHA-256 is recorded in `experiments/EXP-0003-blueprint-references.md`. Original third-party strings are not copied into tracked files.

The additional `automation-science-am1.txt` sample is also ignored. Its 303 entities include 6 assembler-1, 30 labs, 135 yellow belts, 22 underground belts, 18 ordinary inserters, 35 long-handed inserters, 22 fast inserters, 31 small poles, 2 splitters, and 2 roboports. The author's planning note permits omitting early roboports. Module/inserter substitutions are our proposed adaptations, each requiring a new functional test. Fast and long-handed inserters and splitters may be beyond the intended unlock point; verify unlock timing against the run's tech state. Do not assume simpler belts or inserters preserve routing or throughput. Filter assignments also need checking if filter-capable inserters are replaced.

The parser accepts only version-prefix `0` exchange strings, caps compressed input at 2 MB, decompressed data at 8 MB, nested book depth at 12, and combined entity count at 20,000. Its summaries count prototypes and report names absent from the supplied live catalog; they do not simulate fluid networks, belt lanes, power, recipes, or production.

The runtime catalog is Factorio 2.1.20. Public references may have been authored under 2.0; exchange data does not establish game-version compatibility. The Autosaved samples structurally use names present in the local 2.1.20 catalog, but placement and operation remain unverified.
