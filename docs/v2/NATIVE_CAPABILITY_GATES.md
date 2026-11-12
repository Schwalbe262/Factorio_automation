# Native prerequisites and investment stages

`observe.native_capability_gates` reports current recipe locks for mining,
smelting and red/green science. The required recipes come from those controllers;
unlocking research comes from the installed catalog's `technologies[].unlocks`.
Only the current native enabled-recipe observation clears a gate. Catalog
prototype presence or old export-time research flags never enable construction.

Each gate lists missing recipes, their possible research unlocks and uniquely
required technologies. It certifies recipe availability only; power, producer
proofs and capability dependencies still apply. Missing unlock metadata stays
unresolved. Alternate research unlocks remain choices.

Proposed queue changes are rejected atomically when research depends on a
factory whose recipe that same research must unlock. Intermediate dependency
changes count too. Existing unrelated legacy graph defects do not block an
independent queue update. Green science requires both `logistic-science-pack`
and `fast-inserter` research before its factory; both use existing red science.

Starter capacity checks its existing native completion proof before unrelated
fuel chores. After native electric mining unlocks, new bulk iron/copper burner
targets above two cells stop with an explicit electric-mining remedy. Existing
paid cells and partial work remain; a target already proved complete still
succeeds. Fresh early worlds and bounded two-cell recovery remain supported.

Validation includes synthetic fresh-world A/B/C gates, transactional direct and
indirect research cycles, alternative unlocks, and starter completion/upkeep
ordering. A separate actual GPT-6 Luna medium read-only planning evaluation
queued both recipe-unlock studies before green-science construction. This is a
planning-interface check, not a fresh-seed speed or autonomous rocket result.
