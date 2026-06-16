"""Isolated package for locally-generated (Qwen-authored) skill executors.

Modules placed here are produced by :mod:`factorio_ai.skill_foundry` after a
generated candidate passes the static-safety, offline-replay, and (in later
phases) headless sandbox-save dry-run gates. They are kept in their own package
so generated capability stays isolated from the hand-written core, is
git-trackable, and is trivially reversible.

Generated modules MUST:

- use only absolute imports from ``factorio_ai.models`` and
  ``factorio_ai.planner`` plus the stdlib allowlist (``math``, ``dataclasses``,
  ``typing``);
- define a single skill class exposing ``next_action(self, observation) ->
  PlannerDecision``;
- emit only the validated action types in
  :data:`factorio_ai.models.ALLOWED_ACTION_TYPES`.

Nothing in this package is imported eagerly; the controller loads a generated
module by file path through :func:`factorio_ai.skill_foundry.load_generated_skill_class`
only after the module is recorded as ``registered`` in
``runtime/generated-skills.json``.
"""

from __future__ import annotations
