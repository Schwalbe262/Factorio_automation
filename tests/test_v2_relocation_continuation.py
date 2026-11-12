from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_v2_defense_relocation as fixture
from factorio_agent.defense_relocation import (
    DefenseRelocation,
    activate_relocation_continuation,
    execute_build_replacement,
    execute_retire_post,
    _parent_checkpoint,
    _hash,
    CONTINUATION_PROBE_LUA,
)


@unittest.skipUnless(fixture.LuaRuntime, "optional executable Lua runtime")
class RelocationContinuationTests(fixture.NativeRelocationTests):
    @staticmethod
    def marker_hash(marker):
        def plain(value):
            return {k: plain(v) for k, v in value.items()} if hasattr(value, "items") else value

        return _hash(plain(marker))

    def continuation_case(self):
        _, marker = self.legacy_case()
        proposal = self.c.prepare_legacy_retirement_reconciliation(self.obs, "engine-job")
        self.assertTrue(proposal["ok"], proposal)
        self.entry["legacy_retirement_reconciliation"] = proposal["intent"]
        self.c._save()
        self.parent = deepcopy(self.c.state)
        self.original_marker = self.marker_hash(marker)
        # A normally paid same-position repair, followed by the recorded owner succession.
        prior = deepcopy(self.entry["new"])
        current = dict(
            prior, unit_number=9100, lost_unit_number=prior["unit_number"], lost_unit_numbers=[prior["unit_number"]]
        )
        self.perimeter.state["posts"] = [
            current if q["name"] == current["name"] else q for q in self.perimeter.state["posts"]
        ]
        self.perimeter._save()
        self.perimeter.live[tuple(current["position"].values())]["unit_number"] = 9100
        self.lua.globals().turret(self.table(current["position"]), 9100, 10)
        action = dict(
            type="build",
            name="gun-turret",
            position=current["position"],
            direction=0,
            reason="normally build proactive strategic perimeter post " + current["name"],
        )
        events = [
            dict(event="action_intent", key="watch", tick=20, action=action),
            dict(
                event="action_result", key="watch", tick=20, result=dict(ok=True, status="succeeded", unit_number=9100)
            ),
        ]
        with self.events.open("a") as f:
            for q in events:
                f.write(json.dumps(q) + "\n")
        self.lua.execute("""
game.tick=100;f.index=1;s.index=1;a.unit_number=101
owner={index=1,name="FactoryAutomaton",valid=true,connected=true,force=f,character=a};a.player=owner
game.get_player=function(q)if q==1 or q=="FactoryAutomaton"then return owner end end
d.actor=a;d.crafting_player_index=1;d.crafting_actor_unit_number=101
d.actor_successions={["w:1:100:101"]={kind="owner-character-succession-v1",world_id="w",owner_index=1,
 owner_name="FactoryAutomaton",old_unit=100,new_unit=101,force_index=1,surface_index=1,tick=30,
 old_pointer_invalid=true,old_character_absent=true,scope="framework storage rebind only; prior actor receipts remain unchanged"}}
""")
        self.obs["actor_unit_number"] = 101
        self.obs["tick"] = 100
        self.obs["entities"] = [q for q in self.obs["entities"] if q["unit_number"] != self.entry["old"]["unit_number"]]
        return marker

    def activate(self):
        proposal = self.c.prepare_continuation(self.obs, "engine-job")
        self.assertTrue(proposal["ok"], proposal)
        result = activate_relocation_continuation(self.c, self.obs, proposal)
        self.assertTrue(result["ok"], result)
        return proposal

    def test_epoch_requires_explicit_activation_preserves_parent_native_marker_and_unknown_baseline(self):
        marker = self.continuation_case()
        before = self.c.path.read_bytes()
        proposal = self.c.prepare_continuation(self.obs, "engine-job")
        self.assertTrue(proposal["ok"], proposal)
        self.assertEqual(self.c.path.read_bytes(), before)
        self.assertEqual(self.c.step(self.obs)["reason"], "known_defense_post_or_scope_metadata_changed")
        result = activate_relocation_continuation(self.c, self.obs, proposal)
        self.assertTrue(result["ok"], result)
        document = json.loads(self.c.path.read_text())
        self.assertEqual(_parent_checkpoint(document), self.parent)
        self.assertEqual(self.marker_hash(marker), self.original_marker)
        h = self.c.state["historical_resolution"]
        self.assertFalse(h["baseline_available"])
        self.assertFalse(h["recovered_counts_available"])
        self.assertEqual(h["original_actor_unit"], 100)
        self.assertEqual(self.c.state["actor_unit"], 101)
        self.assertEqual([q["old"]["unit_number"] for q in self.c.state["entries"]], [7154])
        restarted = DefenseRelocation(
            self.game, self.bootstrap, self.builder, self.catalog, self.perimeter, self.engines
        )
        again = activate_relocation_continuation(restarted, self.obs, proposal)
        self.assertTrue(again["reused_activation"])
        self.assertEqual(json.loads(self.c.path.read_text()), document)

    def test_epoch_rejects_forged_or_reused_paid_repair_and_changed_owner(self):
        self.continuation_case()
        raw = self.events.read_text()
        rows = raw.splitlines()
        last = json.loads(rows[-1])
        last["result"]["reused"] = True
        rows[-1] = json.dumps(last)
        self.events.write_text("\n".join(rows) + "\n")
        self.assertIn("paid non-reused", self.c.prepare_continuation(self.obs, "engine-job")["detail"])
        self.events.write_text(raw)
        self.lua.execute('d.actor_successions["w:1:100:101"].old_unit=999')
        self.assertEqual(
            self.c.prepare_continuation(self.obs, "engine-job")["reason"], "continuation_owner_succession_changed"
        )
        self.assertEqual(json.loads(self.c.path.read_text()), self.parent)

    def test_activation_cas_rejects_new_scope_or_parent_or_source_changes_without_writes(self):
        self.continuation_case()
        proposal = self.c.prepare_continuation(self.obs, "engine-job")
        self.assertTrue(proposal["ok"], proposal)
        path = self.root / "pcb-board.json"
        board = json.loads(path.read_text())
        board["revision"] = "new"
        path.write_text(json.dumps(board))
        before = self.c.path.read_bytes()
        self.assertEqual(
            activate_relocation_continuation(self.c, self.obs, proposal)["reason"],
            "continuation_reviewed_authority_changed",
        )
        self.assertEqual(self.c.path.read_bytes(), before)
        self.engines.state["iron_source"]["unit_number"] = 99
        self.assertEqual(
            self.c.prepare_continuation(self.obs, "engine-job")["reason"],
            "defense_relocation_engine_plan_or_source_changed",
        )
        changed = json.loads(self.c.path.read_text())
        changed["phase"] = "changed"
        self.c.path.write_text(json.dumps(changed))
        self.assertEqual(
            activate_relocation_continuation(self.c, self.obs, proposal)["reason"],
            "continuation_activation_checkpoint_changed",
        )

    def test_native_current_repair_or_partial_scope_ammo_changes_fail_before_activation(self):
        self.continuation_case()
        self.lua.execute('world["gun-turret:135:-93"].unit_number=999')
        self.assertEqual(
            self.c.prepare_continuation(self.obs, "engine-job")["reason"],
            "continuation_historical_post_identity_changed",
        )
        self.lua.execute(
            'world["gun-turret:135:-93"].unit_number=9100;world["gun-turret:127:-116"].ammo_inventory[1].count=4'
        )
        self.assertEqual(
            self.c.prepare_continuation(self.obs, "engine-job")["reason"], "referencing_scope_post_unhealthy"
        )

    def test_native_changed_source_or_original_marker_rejects_review_before_activation(self):
        marker = self.continuation_case()
        marker.actor_unit = 999
        self.assertEqual(
            self.c.prepare_continuation(self.obs, "engine-job")["reason"], "continuation_original_paid_marker_changed"
        )
        marker.actor_unit = 100
        self.lua.execute('world["transport-belt:12.5:-111.5"].unit_number=999')
        self.assertEqual(
            self.c.prepare_continuation(self.obs, "engine-job")["reason"], "defense_relocation_engine_source_changed"
        )

    def test_paid_birth_and_interrupted_two_file_publication_resume_without_repaying(self):
        self.continuation_case()
        self.activate()
        action = self.c.step(self.obs)
        built = execute_build_replacement(self.game, action)
        self.assertTrue(built["ok"], built)
        self.c = DefenseRelocation(self.game, self.bootstrap, self.builder, self.catalog, self.perimeter, self.engines)
        self.assertEqual(self.c.step(self.obs)["reason"], "paid_replacement_unit_bound_from_atomic_receipt")
        entry = self.c.state["entries"][0]
        self.add_new(entry, built["unit_number"])
        self.lua.globals().load_new(built["unit_number"], 10)
        save = self.c._save
        calls = []

        def interrupted():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("crash after perimeter publication before epoch phase save")
            return save()

        with patch.object(self.c, "_save", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.c.step(self.obs)
        self.c = DefenseRelocation(self.game, self.bootstrap, self.builder, self.catalog, self.perimeter, self.engines)
        self.assertEqual(self.c.state["entries"][0]["phase"], "publish")
        self.assertEqual(self.c.step(self.obs)["reason"], "paid_armed_replacement_published_before_old_post_recovery")
        self.assertEqual(self.lua.globals().built_calls, 2)
        self.assertEqual(_parent_checkpoint(json.loads(self.c.path.read_text())), self.parent)

    def test_continuation_build_arm_publish_partial_retire_resume_and_final_proof_preserve_history(self):
        original = self.continuation_case()
        self.activate()
        entry = self.c.state["entries"][0]
        action = self.c.step(self.obs)
        self.assertEqual(action["type"], "build_defense_replacement")
        result = execute_build_replacement(self.game, action)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.c.step(self.obs)["reason"], "paid_replacement_unit_bound_from_atomic_receipt")
        self.add_new(entry, result["unit_number"])
        self.lua.globals().load_new(result["unit_number"], 10)
        self.assertEqual(self.c.step(self.obs)["reason"], "paid_armed_replacement_published_before_old_post_recovery")
        self.lua.execute("""world["gun-turret:146:-109"].ammo_inventory[1].ammo=7;autoequip=true
local original_mine=a.mine_entity
a.mine_entity=function(e)local held=equipped[1];equipped[1]=empty();local ok=original_mine(e)
 equipped[2]=equipped[1];equipped[1]=held;return ok end
""")
        action = self.c.step(self.obs)
        self.assertEqual(action["type"], "retire_defense_post")
        result = execute_retire_post(self.game, action)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["recovered_rounds"], 97)
        self.assertEqual(self.c.step(self.obs)["reason"], "old_post_recovered_from_exact_atomic_receipt")
        self.c = DefenseRelocation(self.game, self.bootstrap, self.builder, self.catalog, self.perimeter, self.engines)
        final = self.c.step(self.obs)
        self.assertEqual(final["status"], "succeeded", final)
        evidence = final["evidence"]["recovery_evidence"]
        self.assertFalse(evidence[0]["baseline_available"])
        self.assertEqual(evidence[1]["kind"], "ordinary_mine_conserved")
        self.assertEqual(_parent_checkpoint(json.loads(self.c.path.read_text())), self.parent)
        self.assertEqual(self.marker_hash(original), self.original_marker)
        self.assertEqual(self.lua.globals().built_calls, 2)  # one old paid replacement, one new epoch replacement

    def test_continuation_guard_is_flattenable_readonly(self):
        self.assertNotIn("--", CONTINUATION_PROBE_LUA)
        for forbidden in ("create_entity", "mine_entity", "destroy(", ".researched=", "insert{"):
            self.assertNotIn(forbidden, CONTINUATION_PROBE_LUA)


if __name__ == "__main__":
    unittest.main()
