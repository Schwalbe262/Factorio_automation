import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent import actor_client
from factorio_agent.supervisor import ActorDeadWithoutPlayer, actor_binding

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog(**extra):
    technologies = {
        "electronics": dict(research_trigger={"type": "craft-item", "item": {"name": "copper-plate"}, "count": 10}),
        "steam-power": dict(research_trigger={"type": "craft-item", "item": {"name": "iron-plate"}, "count": 50}),
        "automation-science-pack": dict(
            prerequisites=["electronics", "steam-power"],
            research_trigger={"type": "craft-item", "item": {"name": "lab"}, "count": 1},
        ),
        "automation": dict(prerequisites=["automation-science-pack"]),
        "oil-processing": dict(research_trigger={"type": "mine-entity", "entities": ["crude-oil"]}),
        "oil-gathering": dict(prerequisites=["oil-processing"]),
    }
    technologies.update(extra)
    recipes = {
        "lab": dict(category="crafting", categories=["crafting"], products=[{"name": "lab", "type": "item"}]),
        "iron-plate": dict(category="smelting", categories=["smelting"], products=[{"name": "iron-plate"}]),
        "copper-plate": dict(category="smelting", categories=["smelting"], products=[{"name": "copper-plate"}]),
    }
    entities = {"character": {"crafting_categories": ["crafting", "hand-crafting"]}}
    return SimpleNamespace(technologies=technologies, recipes=recipes, entities=entities)


def runtime_dir(directory, world="w"):
    root = Path(directory)
    (root / "run.json").write_text(json.dumps({"schema_version": 2, "root": str(root), "seed": 1}), encoding="utf-8")
    (root / "world-settings.json").write_text(json.dumps({"world_id": world}), encoding="utf-8")
    return root


class ModeAndTriggerTests(unittest.TestCase):
    def test_missing_option_is_always_and_none_labels_run_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = runtime_dir(directory)
            self.assertEqual(actor_client.mode(root), "always")
            self.assertIsNone(actor_client.test_mode(root))
            actor_client.set_mode(root, "none")
            self.assertEqual(actor_client.mode(root), "none")
            run = json.loads((root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["test_mode"], "client_free")
            self.assertEqual(run["script_credited_triggers"], [])
            self.assertEqual(actor_client.test_mode(root)["test_mode"], "client_free")
            self.assertTrue(actor_client.client_free(root))
            self.assertEqual(actor_client.client_state(root, False), "none")
            with self.assertRaises(ValueError):
                actor_client.set_mode(root, "sometimes")

    def test_only_hand_craftable_trigger_items_need_credit(self):
        roots = ["automation", "oil-gathering"]
        self.assertEqual(actor_client.hand_trigger_technologies(catalog(), roots), ["automation-science-pack"])

    def test_unknown_trigger_type_is_treated_as_needing_a_player(self):
        extra = dict(odd=dict(research_trigger={"type": "build-entity", "entity": {"name": "x"}}))
        self.assertIn("odd", actor_client.hand_trigger_technologies(catalog(**extra), ["odd"]))

    def test_real_roadmap_needs_only_the_lab(self):
        base = Path(__file__).resolve().parents[1] / "runtime" / "v2"
        for candidate in sorted(base.glob("*/catalog.json"))[:1] if base.exists() else []:
            if candidate.exists():
                value = json.loads(candidate.read_text(encoding="utf-8"))
                found = SimpleNamespace(**{k: value[k] for k in ("technologies", "recipes", "entities")})
                self.assertEqual(actor_client.hand_trigger_technologies(found), ["automation-science-pack"])
                return
        self.skipTest("no recorded catalog")

    def test_credit_refusals_before_any_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = runtime_dir(directory)
            action = dict(type="credit_hand_craft_trigger", technology="automation-science-pack", item="lab")
            self.assertEqual(
                actor_client.credit_refusal(root, catalog(), action), "trigger_credit_requires_client_free_mode"
            )
            actor_client.set_mode(root, "none")
            self.assertIsNone(actor_client.credit_refusal(root, catalog(), action))
            for change, reason in (
                (dict(item="gear"), "trigger_credit_not_this_craft_item_trigger"),
                (dict(technology="steam-power", item="iron-plate"), "trigger_credit_trigger_needs_no_hand_craft"),
                (dict(technology="automation", item="lab"), "trigger_credit_not_this_craft_item_trigger"),
                (dict(count=3), "trigger_credit_action_shape"),
            ):
                with self.subTest(change=change):
                    self.assertEqual(actor_client.credit_refusal(root, catalog(), dict(action, **change)), reason)


class BindingTests(unittest.TestCase):
    def value(self, **change):
        value = dict(
            ok=True,
            world_id="w",
            surface_index=1,
            pinned_owner_index=None,
            owner=None,
            old_unit=7,
            old_pointer=dict(valid=True, unit_number=7, surface_index=1, force_index=1),
            successor=dict(valid=False),
            detached=True,
            detached_record=dict(unit_number=7, world_id="w", surface_index=1, force_index=1, mode="none"),
        )
        value.update(change)
        return value

    def test_client_free_actor_binds_by_unit_and_world(self):
        binding = actor_binding(self.value(), "w")
        self.assertEqual((binding["unit_number"], binding["detached"], binding["client_mode"]), (7, True, "none"))
        self.assertIsNone(binding["owner_index"])

    def test_detached_owner_must_match_the_record(self):
        record = dict(unit_number=7, world_id="w", surface_index=1, force_index=1, player_index=1)
        good = self.value(pinned_owner_index=1, owner=dict(index=1, name="FactoryAutomaton"), detached_record=record)
        self.assertEqual(actor_binding(good, "w")["owner_index"], 1)
        for change in (
            dict(owner=dict(index=2, name="x")),
            dict(detached=False),
            dict(old_unit=8),
            dict(old_pointer=dict(valid=True, unit_number=7, surface_index=1, force_index=1, player_index=1)),
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                actor_binding(dict(good, **change), "w")

    def test_dead_detached_actor_is_the_terminal_reason(self):
        with self.assertRaises(ActorDeadWithoutPlayer):
            actor_binding(self.value(old_pointer=dict(valid=False)), "w")

    def test_pinned_player_path_is_unchanged_without_a_record(self):
        value = self.value(detached_record=None, detached=False)
        with self.assertRaises(ValueError) as caught:
            actor_binding(value, "w")
        self.assertNotIsInstance(caught.exception, ActorDeadWithoutPlayer)


class DetachFlowTests(unittest.TestCase):
    def test_detach_records_the_transition_after_the_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = runtime_dir(directory)
            actor_client.set_mode(root, "until_lab_trigger")
            (root / "agent-client").mkdir()
            (root / "agent-client" / "client-process.json").write_text(
                json.dumps({"pid": 5, "creation_time": 9}), encoding="utf-8"
            )
            record = dict(unit_number=7, world_id="w", player_index=1, tick=10)
            checks = iter([dict(ok=True, detached=True, owner_connected=True), dict(ok=True, detached=True)])
            game = SimpleNamespace(
                expected_world="w",
                cfg=None,
                act=Mock(return_value=dict(status="succeeded", record=record, tick=10)),
                query=Mock(side_effect=lambda body: next(checks)),
            )
            result = actor_client.detach(
                root, game, catalog(), stop_client=lambda cfg: {"status": "stopped"}, sleep=lambda s: None
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(game.act.call_args[0][0]["hand_triggers"], ["automation-science-pack"])
            self.assertTrue(actor_client.is_closed_client(root, {"pid": 5, "creation_time": 9}))
            self.assertFalse(actor_client.is_closed_client(root, {"pid": 5, "creation_time": 10}))
            self.assertEqual(actor_client.client_state(root, False), "detached")
            again = actor_client.detach(root, game, catalog())
            self.assertEqual(again["reason"], "actor_already_detached")
            self.assertEqual(game.act.call_count, 1)

    def test_detach_refuses_in_the_default_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = runtime_dir(directory)
            game = SimpleNamespace(act=Mock())
            self.assertEqual(actor_client.detach(root, game, catalog())["status"], "blocked")
            game.act.assert_not_called()
            self.assertFalse(actor_client.detach_due(root, {"technologies": ["automation-science-pack"]}))


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class LuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(value):
            if isinstance(value, dict):
                return self.lua.table_from({k: table(v) for k, v in value.items()})
            if isinstance(value, list):
                return self.lua.table_from([table(v) for v in value])
            return value

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute("""
f={index=1};s={index=1};defines={controllers={spectator=0,character=1}}
inventory={["iron-gear-wheel"]=10,["electronic-circuit"]=10,["transport-belt"]=4}
a={valid=true,name="character",unit_number=7,force=f,surface=s,player=nil,crafting_queue_size=0,crafting_queue={},
 character_crafting_speed_modifier=0,position={x=1,y=2}}
a.get_item_count=function(name)return inventory[name]or 0 end
d={world_id="w",actor=a}
labtech={researched=false,prerequisites={{researched=true},{researched=true}},
 prototype={research_trigger={type="craft-item",item={name="lab"},count=1},effects={{type="unlock-recipe",recipe="automation-science-pack"}}}}
f.technologies={["automation-science-pack"]=labtech}
f.recipes={["automation-science-pack"]={enabled=false}}
setmetatable(labtech,{__newindex=function(t,k,v)rawset(t,k,v);if k=="researched"and v then f.recipes["automation-science-pack"].enabled=true end end})
labtech.researched=nil
game={tick=100,connected_players={},get_player=function(i)return nil end}
function pos(p)return{x=p.x,y=p.y}end
function contents(inv)local out={};for k,v in pairs(inventory)do out[k]=v end;return out end
function success(t)t=t or {};t.ok=true;return t end
function failure(r)return{ok=false,reason=r}end
""")

    def run_lua(self, script, payload=None):
        if payload is not None:
            script = script.replace("PAYLOAD", json.dumps(json.dumps(payload)))
        return dict(self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()").items())

    def craft_lab(self):
        body = (
            "local x={recipe='lab'};local inv={};local recipe={energy=2,products={{type='item',name='lab'}}};"
            + actor_client.TRIGGER_CRAFT_BEFORE_LUA
            + " inventory['iron-gear-wheel']=0;inventory['electronic-circuit']=0;inventory['transport-belt']=0;local n=1;"
            + actor_client.TRIGGER_CRAFT_RECORD_LUA
            + " return {ok=true}"
        )
        self.run_lua(body)

    def credit(self):
        return self.run_lua(
            actor_client.CREDIT_LUA, dict(world_id="w", technology="automation-science-pack", item="lab")
        )

    def test_adopt_then_credit_only_after_the_real_finished_craft(self):
        adopted = self.run_lua(actor_client.ADOPT_LUA, dict(world_id="w"))
        self.assertEqual(adopted["status"], "succeeded")
        self.assertTrue(self.lua.eval("d.crafting_actor_unit_number==7 and d.detached_actor.mode=='none'"))
        self.assertEqual(self.credit()["reason"], "trigger_credit_craft_receipt_missing")
        self.craft_lab()
        self.assertEqual(self.credit()["reason"], "trigger_credit_craft_time_not_elapsed")
        self.lua.execute("game.tick=220;a.crafting_queue={{recipe='lab',count=1}}")
        self.assertEqual(self.credit()["reason"], "trigger_credit_craft_still_queued")
        self.lua.execute("a.crafting_queue={}")
        self.assertEqual(self.credit()["reason"], "trigger_credit_crafted_item_not_in_inventory")
        self.lua.execute("inventory.lab=1")
        credited = self.credit()
        self.assertEqual(credited["status"], "succeeded")
        self.assertTrue(self.lua.eval("rawget(labtech,'researched')==true"))
        self.assertTrue(self.lua.eval("f.recipes['automation-science-pack'].enabled"))
        self.assertTrue(self.lua.eval("inventory.lab==1 and inventory['iron-gear-wheel']==0"))
        self.assertEqual(self.credit()["reason"], "trigger_credit_already_researched")

    def test_credit_refuses_without_client_free_actor_or_prerequisites(self):
        self.assertEqual(self.credit()["reason"], "trigger_credit_requires_client_free_actor")
        self.run_lua(actor_client.ADOPT_LUA, dict(world_id="w"))
        self.lua.execute("labtech.prerequisites={{researched=false}}")
        self.assertEqual(self.credit()["reason"], "trigger_credit_prerequisites_pending")
        self.lua.execute("labtech.prerequisites={};labtech.prototype.research_trigger={type='mine-entity'}")
        self.assertEqual(self.credit()["reason"], "trigger_credit_not_a_craft_item_trigger")

    def test_a_player_bound_actor_is_never_adopted(self):
        self.lua.execute("d.crafting_player_index=1")
        self.assertEqual(
            self.run_lua(actor_client.ADOPT_LUA, dict(world_id="w"))["reason"],
            "client_free_actor_already_bound_to_a_player",
        )
        self.lua.execute("d.crafting_player_index=nil;a.player={index=1}")
        self.assertEqual(
            self.run_lua(actor_client.ADOPT_LUA, dict(world_id="w"))["reason"], "client_free_actor_has_a_player"
        )

    def test_detach_sets_spectator_and_records_the_actor(self):
        self.lua.execute("""owner={index=1,name="FactoryAutomaton",valid=true,connected=true}
owner.character=a;a.player=owner;d.crafting_player_index=1;d.crafting_actor_unit_number=7
owner.set_controller=function(q)owner.character=nil;a.player=nil;owner.controller=q.type end
game.get_player=function(i)if i==1 then return owner end end
labtech.researched=true""")
        payload = dict(world_id="w", lab_trigger="automation-science-pack", hand_triggers=["automation-science-pack"])
        self.lua.execute("a.crafting_queue_size=1")
        self.assertEqual(self.run_lua(actor_client.DETACH_LUA, payload)["status"], "waiting")
        self.lua.execute("a.crafting_queue_size=0")
        result = self.run_lua(actor_client.DETACH_LUA, payload)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(self.lua.eval("owner.controller==0 and d.detached_actor.unit_number==7 and a.player==nil"))
        self.assertTrue(self.run_lua(actor_client.DETACH_LUA, payload)["already_detached"])
        verify = self.run_lua(actor_client.VERIFY_LUA)
        self.assertTrue(verify["detached"])
        self.assertFalse(verify["actor_owned"])


if __name__ == "__main__":
    unittest.main()
