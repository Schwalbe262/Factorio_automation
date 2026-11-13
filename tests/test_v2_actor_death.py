"""Actor death: client-free respawn emulation, paid belongings recovery, the supervisor death state,
the watcher stall timeout, death reports and the threat rules (all-1114-0107-1075833632)."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent import actor_client, actor_death
from factorio_agent.core.deterministic_game import TRIP_THREAT_LUA, DeterministicGame
from factorio_agent.defense_policy import assess_risk
from factorio_agent.errors import CLIError
from factorio_agent.progression import URGENT_DEFENSE_PRIORITY, autopilot_nodes, defense_urgency_nodes
from factorio_agent.supervisor import ActorDeathPending, Supervisor

from test_v2_supervisor import Clock, FakeCLI

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))


def _tool(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet = _tool("fleet")
fleet_triage = _tool("fleet_triage")


def runtime_dir(directory, world="w"):
    root = Path(directory)
    (root / "run.json").write_text(json.dumps({"schema_version": 2, "root": str(root), "seed": 1}), encoding="utf-8")
    (root / "world-settings.json").write_text(json.dumps({"world_id": world}), encoding="utf-8")
    return root


LUA_WORLD = """
f={index=1};s={index=1,name="nauvis"};defines={inventory={character_corpse=1}}
game={tick=1200}
function pos(p)return{x=p.x,y=p.y}end
function mkstack(name,count)
 local st={valid_for_read=true,name=name,count=count,quality={name="normal"}}
 st.clear=function()st.valid_for_read=false;st.count=0 end
 return st
end
function mkinv(cap)
 local inv={}
 for k=1,cap do inv[k]={valid_for_read=false,count=0} end
 inv.insert=function(st)
  for k=1,cap do if not inv[k].valid_for_read then inv[k]=mkstack(st.name,st.count);return st.count end end
  return 0
 end
 inv.is_empty=function()for k=1,cap do if inv[k].valid_for_read then return false end end return true end
 return inv
end
corpse_inv=mkinv(4);corpse_inv[1]=mkstack("iron-plate",50);corpse_inv[2]=mkstack("transport-belt",7)
corpse={valid=true,character_corpse_tick_of_death=1000,position={x=40,y=-11}}
corpse.get_inventory=function(i)return corpse_inv end
corpses={corpse};characters={};enemies={};next_unit=40;teleports={}
f.get_spawn_position=function()return{x=0,y=0}end
s.find_entities_filtered=function(q)
 if q.type=="character-corpse"then return corpses end
 if q.type=="character"then return characters end
 if q.force=="enemy"then return enemies end
 return {}
end
s.count_entities_filtered=function(q)return #enemies end
s.find_non_colliding_position=function(name,p,r,step)return{x=p.x+0.5,y=p.y}end
s.create_entity=function(q)
 next_unit=next_unit+1
 local e={valid=true,name=q.name,unit_number=next_unit,position=q.position,player=nil,crafting_queue_size=0}
 local inv=mkinv(4)
 e.get_main_inventory=function()return inv end
 e.teleport=function(p)e.position=p;teleports[#teleports+1]=p;return true end
 e.can_reach_entity=function()return false end
 characters[#characters+1]=e
 return e
end
dead={valid=false}
d={world_id="w",actor=dead,crafting_actor_unit_number=5,actor_last_known_position={x=38,y=-10},
 detached_actor={kind="detached-actor-v1",mode="none",world_id="w",unit_number=5}}
a=d.actor
"""


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class RespawnLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(value):
            if isinstance(value, dict):
                return self.lua.table_from({k: table(v) for k, v in value.items()})
            if isinstance(value, list):
                return self.lua.table_from([table(v) for v in value])
            return value

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute(LUA_WORLD)

    def run_lua(self, script, payload):
        script = script.replace("PAYLOAD", json.dumps(json.dumps(payload)))
        return dict(self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()").items())

    def respawn(self):
        return self.run_lua(actor_death.RESPAWN_LUA, actor_death.respawn_action("w"))

    def belongings(self, unit, tick=1000, backend="assisted"):
        self.lua.execute("a=d.actor")
        return self.run_lua(actor_death.BELONGINGS_LUA, actor_death.belongings_action("w", unit, tick, backend))

    def test_respawn_waits_the_delay_then_binds_an_empty_character_at_the_spawn(self):
        waiting = self.respawn()
        self.assertEqual(
            (waiting["status"], waiting["reason"], waiting["ready_tick"]), ("waiting", "respawn_delay", 1600)
        )
        self.assertTrue(self.lua.eval("d.client_free_death.corpse.x==40 and d.client_free_death.corpse_items==57"))
        self.assertTrue(self.lua.eval("#characters==0"))
        self.lua.execute("game.tick=1600")
        done = self.respawn()
        self.assertEqual((done["status"], done["reason"]), ("succeeded", "client_free_actor_respawned"))
        self.assertTrue(done["inventory_empty"])
        self.assertTrue(self.lua.eval("d.actor.unit_number==41 and d.crafting_actor_unit_number==41"))
        self.assertTrue(self.lua.eval("d.detached_actor.unit_number==41 and d.detached_actor.respawns==1"))
        self.assertTrue(self.lua.eval("d.actor.position.x==0.5 and d.actor.position.y==0"))
        self.assertTrue(self.lua.eval("#d.client_free_deaths==1 and d.client_free_death==nil"))
        # The corpse keeps every item: a respawn is not a grant.
        self.assertTrue(self.lua.eval("corpse_inv[1].count==50 and corpse_inv[2].count==7"))
        self.lua.execute("a=d.actor")
        self.assertEqual(self.respawn()["reason"], "actor_alive")

    def test_respawn_refuses_outside_client_free_or_while_the_old_actor_lives(self):
        self.lua.execute("d.detached_actor.mode='until_lab_trigger'")
        self.assertEqual(self.respawn()["reason"], "respawn_requires_client_free_actor")
        self.lua.execute("d.detached_actor.mode='none';characters={{valid=true,unit_number=5}}")
        self.assertEqual(self.respawn()["reason"], "respawn_old_actor_still_live")

    def test_belongings_wait_while_enemies_are_near_then_transfer_conserved_and_return(self):
        self.lua.execute("game.tick=1600")
        self.respawn()
        self.lua.execute("enemies={{valid=true,name='small-biter'}}")
        unsafe = self.belongings(41)
        self.assertEqual((unsafe["status"], unsafe["reason"]), ("waiting", "belongings_unsafe_enemies_near_corpse"))
        self.assertTrue(self.lua.eval("#teleports==0 and corpse_inv[1].count==50"))
        self.lua.execute("enemies={}")
        done = self.belongings(41)
        self.assertEqual((done["status"], done["reason"], done["moved"]), ("succeeded", "belongings_recovered", 57))
        self.assertTrue(self.lua.eval("#teleports==2 and teleports[1].x==40.5 and teleports[2].x==1"))
        self.assertTrue(self.lua.eval("d.client_free_deaths[1].belongings.status=='recovered'"))
        self.assertTrue(self.lua.eval("d.actor.get_main_inventory()[1].count==50"))
        self.assertTrue(self.belongings(41)["already"])

    def test_belongings_lost_when_the_corpse_is_gone_and_reach_needed_in_character_mode(self):
        self.lua.execute("game.tick=1600")
        self.respawn()
        self.assertEqual(self.belongings(41, backend="character")["reason"], "belongings_corpse_out_of_reach")
        self.assertEqual(self.belongings(99)["reason"], "belongings_actor_unit_changed")
        self.assertEqual(self.belongings(41, tick=5)["reason"], "belongings_death_record_missing")
        self.lua.execute("corpses={}")
        lost = self.belongings(41)
        self.assertEqual(lost["reason"], "belongings_lost_corpse_gone")
        self.assertTrue(self.lua.eval("d.client_free_deaths[1].belongings.lost_items==57"))


class ActionGuardTests(unittest.TestCase):
    def game(self, root):
        return SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=root), expected_world="w", backend="assisted", query=Mock()
        )

    def test_respawn_and_belongings_refuse_outside_client_free_mode_or_with_other_constants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = runtime_dir(directory)
            game = self.game(root)
            self.assertEqual(
                actor_death.execute_respawn(game, actor_death.respawn_action("w"))["reason"],
                "respawn_requires_client_free_mode",
            )
            actor_client.set_mode(root, "none")
            faster = dict(actor_death.respawn_action("w"), respawn_delay_ticks=1)
            self.assertEqual(actor_death.execute_respawn(game, faster)["reason"], "respawn_action_shape")
            wider = dict(actor_death.belongings_action("w", 41, 1000, "assisted"), threat_radius=0)
            self.assertEqual(actor_death.execute_belongings(game, wider)["reason"], "belongings_action_shape")
            # Off by default (owner approval pending): no query at all.
            self.assertEqual(
                actor_death.execute_respawn(game, actor_death.respawn_action("w"))["reason"], "respawn_emulation_off"
            )
            game.query.assert_not_called()
            actor_client.set_respawn(root, True)
            self.assertTrue(actor_client.test_mode(root)["respawn_emulation"])
            actor_death.execute_respawn(game, actor_death.respawn_action("w"))
            game.query.assert_called_once()

    def test_every_move_carries_the_trip_threat_check(self):
        game = DeterministicGame.__new__(DeterministicGame)
        game.backend = "assisted"
        game.query = Mock(return_value={"ok": True, "status": "succeeded"})
        game._record_action = lambda action, result: result
        DeterministicGame.act.__wrapped__(game, {"type": "move", "position": {"x": 1, "y": 2}})
        body = game.query.call_args[0][0]
        self.assertIn("trip_target_threatened", body)
        self.assertLess(body.index("trip_target_threatened"), body.index("teleport"))

    @unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
    def test_trip_threat_lua_fails_a_move_into_enemies(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("n=0;s={count_entities_filtered=function(q)return n end};x={position={x=0,y=0}}")
        lua.execute("function failure(r)return{ok=false,reason=r}end")
        run = (
            "return(function() local TRIP_THREAT_RADIUS=32;"
            + " ".join(TRIP_THREAT_LUA.splitlines())
            + " return {ok=true} end)()"
        )
        self.assertTrue(lua.execute(run)["ok"])
        lua.execute("n=1")
        self.assertEqual(lua.execute(run)["reason"], "trip_target_threatened")


class RecordTests(unittest.TestCase):
    def receipt(self, items=57):
        death = dict(
            tick=1000,
            tick_source="corpse",
            detected_tick=1200,
            respawn_tick=1600,
            old_unit=5,
            new_unit=41,
            corpse={"x": 40, "y": -11},
            corpse_items=items,
            enemies_near_corpse={"small-biter": 3},
        )
        return dict(status="succeeded", death=death, unit_number=41, tick=1600)

    def test_records_and_lost_seconds_with_items_lost_until_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = runtime_dir(directory)
            actor_client.set_mode(root, "none")
            row = actor_death.record_respawn(root, self.receipt())
            self.assertEqual(row["cause"], "enemies at corpse: 3xsmall-biter")
            pending = actor_death.deaths(root, end_tick=7000)[0]
            self.assertEqual((pending["lost_seconds"], pending["items_lost"]), (10.0, 57))
            actor_death.record_belongings(
                root, 1000, dict(status="succeeded", tick=2200, belongings=dict(status="recovered", recovered_items=57))
            )
            done = actor_death.deaths(root, end_tick=7000)[0]
            self.assertEqual((done["recovered_tick"], done["lost_seconds"], done["items_lost"]), (2200, 20.0, None))

    def test_an_unrecovered_agent_dead_is_read_from_the_controller_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = runtime_dir(directory)
            (root / "logs").mkdir()
            rows = [
                dict(event="slice", tick=110025),
                dict(event="call", command="run", error="agent_dead"),
                dict(event="error", reason="agent_dead"),
            ]
            (root / "logs" / "controller-events.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
            )
            (root / "job-events.jsonl").write_text(json.dumps(dict(event="pause", tick=387703)) + "\n")
            found = actor_death.deaths(root)
            self.assertEqual(len(found), 1)
            self.assertIsNone(found[0]["recovered_tick"])
            self.assertEqual(found[0]["lost_seconds"], round((387703 - 110025) / 60, 1))
            self.assertEqual(fleet_triage._unrecovered_deaths(root), 1)


class DeathCLI(FakeCLI):
    def __init__(self, root, clock):
        super().__init__(root, clock)
        self.dead = False
        self.respawns = []
        self.belongings = []

    def invoke(self, args, *, stop_requested=None, on_stop=None):
        cmd = args[0]
        if cmd == "observe" and self.dead:
            self.calls.append(list(args))
            raise CLIError("agent_dead")
        if cmd in ("respawn-actor", "client-free-presence", "reconcile", "recover-belongings", "recover-actor"):
            self.calls.append(list(args))
            if cmd == "respawn-actor":
                receipt = self.respawns.pop(0)
                if receipt.get("status") == "succeeded":
                    self.dead = False
                return deepcopy(receipt)
            if cmd == "reconcile":
                job = self.jobs[args[args.index("--key") + 1]]
                job.update(status="waiting", reason="fresh_observation_before_replanning", pending_action=None)
                return deepcopy(job)
            if cmd == "recover-belongings":
                return deepcopy(self.belongings.pop(0))
            if cmd == "recover-actor":
                self.dead = False
                return {"ok": True, "status": "succeeded"}
            return {"ok": True, "status": "succeeded"}
        return super().invoke(args, stop_requested=stop_requested, on_stop=on_stop)


class SupervisorDeathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = runtime_dir(temp.name)
        self.clock = Clock()
        self.cli = DeathCLI(self.root, self.clock)
        autopilot = patch.object(Supervisor, "_load_autopilot", return_value=None)
        autopilot.start()
        self.addCleanup(autopilot.stop)
        self.s = Supervisor(
            self.root, None, planner="none", cli=self.cli, clock=self.clock, sleep=self.clock.advance, max_seconds=200
        )

    def events(self):
        path = self.root / "logs" / "controller-events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def kinds(self):
        return [e["event"] for e in self.events()]

    def respawned(self, items=57):
        return dict(RecordTests.receipt(None, items), reason="client_free_actor_respawned")

    def test_death_respawn_reconcile_belongings_resume(self):
        actor_client.set_mode(self.root, "none")
        actor_client.set_respawn(self.root, True)
        self.cli.jobs["k"] = dict(
            key="k",
            world_id="w",
            capability="starter",
            parameters={},
            status="blocked",
            reason="action_outcome_unknown_reconcile_required",
            pending_action={"type": "build"},
            priority=0,
            actions=3,
            last_tick=1,
        )
        self.cli.dead = True
        self.cli.respawns = [dict(status="waiting", reason="respawn_delay", ready_tick=1600), self.respawned()]
        self.cli.belongings = [
            dict(status="waiting", reason="belongings_unsafe_enemies_near_corpse", enemies=3, tick=1700),
            dict(
                status="succeeded",
                reason="belongings_recovered",
                moved=57,
                tick=4000,
                belongings=dict(status="recovered", recovered_items=57),
            ),
        ]
        with self.assertRaises(ActorDeathPending):
            self.s._cycle()
        self.assertEqual(self.s.state["status"], "actor_dead")
        self.assertEqual(self.s.errors, 0)
        self.s._cycle()
        # Reconciled (fresh observation, re-plan), then advanced by the slice: never retried.
        self.assertNotEqual(self.cli.jobs["k"]["status"], "blocked")
        self.assertIsNone(self.cli.jobs["k"]["pending_action"])
        commands = [c[0] for c in self.cli.calls]
        self.assertLess(commands.index("respawn-actor"), commands.index("reconcile"))
        self.assertNotIn("retry", commands)
        self.assertIsNone(self.s.state["actor_death"])
        self.assertEqual(self.s.state["belongings_due"]["attempts"], 1)
        self.clock.advance(31)
        self.s._cycle()
        self.assertIsNone(self.s.state["belongings_due"])
        kinds = self.kinds()
        for kind in (
            "actor_died",
            "actor_respawn_waiting",
            "actor_respawned",
            "death_reconcile",
            "belongings_deferred",
            "belongings_recovered",
        ):
            self.assertIn(kind, kinds)
        self.assertNotIn("error", kinds)
        row = actor_client.read(self.root)["deaths"][0]
        self.assertEqual((row["death_tick"], row["new_unit"], row["belongings"]["status"]), (1000, 41, "recovered"))
        commands = [c[0] for c in self.cli.calls]
        self.assertGreater(commands.index("run"), commands.index("respawn-actor"))  # the jobs resumed

    def test_dead_actor_never_enters_error_backoff_and_the_run_still_stops(self):
        actor_client.set_mode(self.root, "none")
        actor_client.set_respawn(self.root, True)
        self.cli.dead = True
        self.cli.respawns = [dict(status="waiting", reason="respawn_delay", ready_tick=9e9)] * 400
        self.s.max_seconds = 20
        result = self.s.run()
        self.assertEqual(result["status"], "safe_stopped")
        kinds = self.kinds()
        self.assertNotIn("error", kinds)
        self.assertEqual(kinds.count("actor_respawn_waiting"), 1)

    def test_a_blocked_respawn_stops_the_run(self):
        actor_client.set_mode(self.root, "none")
        actor_client.set_respawn(self.root, True)
        self.cli.dead = True
        self.cli.respawns = [dict(status="blocked", reason="respawn_no_free_spawn_position")]
        with self.assertRaises(ActorDeathPending):
            self.s._cycle()
        self.assertTrue(self.s.stopping)
        self.assertIn("actor_death_unrecoverable", self.kinds())

    def test_with_the_respawn_emulation_off_the_death_is_recorded_and_the_run_ends_errored(self):
        actor_client.set_mode(self.root, "none")
        self.assertFalse(actor_client.respawn_enabled(self.root))
        self.cli.dead = True
        self.s.state["last_observed_tick"] = 110048
        self.s.max_seconds = 20
        result = self.s.run()
        self.assertEqual(result["status"], "safe_stopped")
        self.assertNotIn("respawn-actor", [c[0] for c in self.cli.calls])
        self.assertIn("actor_death_recorded", self.kinds())
        self.assertNotIn("error", self.kinds())
        row = actor_client.read(self.root)["deaths"][0]
        self.assertEqual((row["death_tick"], row["belongings"]["status"]), (110048, "not_attempted"))
        self.assertTrue(fleet.death_stopped(self.root))
        self.assertEqual(fleet_triage._unrecovered_deaths(self.root), 1)
        verdict = fleet_triage.classify(fleet_triage.run_facts(self.root))
        self.assertEqual(verdict["class"], "errored")
        self.assertTrue(verdict["reasons"][0].startswith("errored: agent_dead"))

    def test_a_connected_player_is_rebound_to_its_engine_respawned_successor(self):
        self.cli.dead = True
        status = dict(
            ok=True,
            world_id="w",
            recovery_ready=False,
            classification="owner_or_character_unavailable",
            old_unit=18,
            successor={"valid": False},
        )
        self.cli.actor = status
        with self.assertRaises(ActorDeathPending):
            self.s._cycle()
        ready = dict(
            status,
            recovery_ready=True,
            successor={"valid": True, "unit_number": 77},
            matching_owner_corpses=[{"tick_of_death": 900, "position": {"x": 3, "y": 4}}],
        )
        self.cli.actor = ready
        self.cli.belongings = [dict(status="succeeded", reason="owned_belongings_transferred_conserved", tick=950)]
        with patch("factorio_agent.supervisor.actor_binding", return_value={"unit_number": 77}):
            self.s._cycle()
        rebind = next(c for c in self.cli.calls if c[0] == "recover-actor")
        self.assertEqual(rebind[rebind.index("--expected-new-unit") + 1], "77")
        self.assertEqual(actor_client.read(self.root)["deaths"][0]["mode"], "player")
        self.assertIn("belongings_recovered", self.kinds())


class WatcherStallTests(unittest.TestCase):
    def test_limit_from_the_fleet_pace_with_a_floor_and_a_fixed_override(self):
        runs = [dict(seed=s, started_at=0) for s in (1, 2, 3)]
        ticks = {1: 30 * 3600, 2: 30 * 3600, 3: 10}
        self.assertEqual(fleet.stall_limit_minutes(dict(runs=runs), ticks, 60 * 60), 10.0)
        self.assertEqual(fleet.stall_limit_minutes(dict(runs=runs), ticks, 60 * 90), 15.0)
        self.assertEqual(fleet.stall_limit_minutes(dict(runs=runs, stall_minutes=4), ticks, 0), 4.0)

    def test_a_run_whose_tick_stands_still_is_due_after_the_limit(self):
        manifest = dict(
            runs=[dict(seed=1, status="running", started_at=0), dict(seed=2, status="running", started_at=0)]
        )
        memory = {}
        due, _ = fleet.stalled_runs(manifest, {1: 100, 2: 100}, memory, 1000)
        self.assertEqual(due, [])
        due, _ = fleet.stalled_runs(manifest, {1: 100, 2: 500}, memory, 1000 + 9 * 60)
        self.assertEqual(due, [])
        due, limit = fleet.stalled_runs(manifest, {1: 100, 2: 900}, memory, 1000 + 11 * 60)
        self.assertEqual(([r["seed"] for r in due], limit), ([1], 10.0))

    def test_enforce_stall_finishes_the_stalled_run_and_triage_says_errored_agent_dead(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "v2" / "f-1"
            (root / "logs").mkdir(parents=True)
            (root / "logs" / "controller-state.json").write_text(
                json.dumps(dict(last_observed_tick=110048, last_error="agent_dead"))
            )
            (root / "logs" / "controller-events.jsonl").write_text(
                json.dumps(dict(event="slice", tick=110025))
                + "\n"
                + json.dumps(dict(event="error", reason="agent_dead"))
                + "\n"
            )
            f = fleet.Fleet(base)
            f.dir.mkdir(parents=True)
            manifest = dict(
                kind="factorio-fleet-v1",
                name="f",
                runs=[dict(seed=1, runtime=str(root), status="running", started_at=0)],
            )
            (f.dir / "f.json").write_text(json.dumps(manifest))
            f.finish_runs = Mock(return_value=["finished"])
            self.assertEqual(f.enforce_stall("f", now=1000), [])
            self.assertEqual(f.enforce_stall("f", now=1000 + 11 * 60), ["finished"])
            reason = f.finish_runs.call_args.kwargs["reason"]
            self.assertTrue(reason.startswith("stalled: no game-time progress"))
            self.assertEqual(f.load("f")["runs"][0]["stalled"]["tick"], 110048)
            verdict = fleet_triage.classify(fleet_triage.run_facts(root))
            self.assertEqual(verdict["class"], "errored")
            self.assertTrue(verdict["reasons"][0].startswith("errored: agent_dead"))

    def test_a_run_the_supervisor_stopped_after_a_death_ends_at_once(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "v2" / "f-1"
            (root / "logs").mkdir(parents=True)
            state = dict(state="stopped", status="safe_stopped", stop_reason="actor_death_unrecoverable:agent_dead")
            (root / "logs" / "controller-state.json").write_text(json.dumps(dict(state, last_observed_tick=5)))
            f = fleet.Fleet(base)
            f.dir.mkdir(parents=True)
            manifest = dict(
                kind="factorio-fleet-v1", name="f", runs=[dict(seed=1, runtime=str(root), status="running")]
            )
            (f.dir / "f.json").write_text(json.dumps(manifest))
            f.finish_runs = Mock(return_value=["finished"])
            self.assertEqual(f.enforce_stall("f", now=1000), ["finished"])
            self.assertTrue(f.finish_runs.call_args.kwargs["reason"].startswith("errored: agent_dead"))

    def test_report_counts_deaths_and_causes(self):
        runs = [
            dict(seed=1, deaths=[dict(cause="enemies at corpse: 3xsmall-biter", lost_seconds=40)]),
            dict(seed=2, deaths=[]),
        ]
        lines = fleet.death_lines(runs)
        self.assertTrue(lines[0].startswith("actor deaths: 1 in 1 runs, 40 game s lost"))
        self.assertIn("3xsmall-biter", lines[1])
        self.assertEqual(fleet.death_lines([dict(seed=1)]), ["actor deaths: 0"])


def risk_facts(**extra):
    asset = dict(
        unit_number=1, name="stone-furnace", type="furnace", position={"x": 0, "y": 0}, health=200, max_health=200
    )
    value = dict(
        ok=True,
        world_id="w",
        tick=1000,
        surface_index=1,
        force_index=1,
        evolution_factor=0.007,
        gun_range=18,
        assets=[asset],
        guns=[],
        targets=[],
        technologies={},
    )
    value.update(extra)
    return value


class ThreatRuleTests(unittest.TestCase):
    def test_a_polluted_nest_beyond_the_survey_radius_expects_an_attack(self):
        self.assertEqual(assess_risk(risk_facts(), "w")["minimum_posts"], 0)
        source = dict(
            unit_number=9,
            name="biter-spawner",
            type="unit-spawner",
            kind="polluted_spawner",
            position={"x": 250, "y": 0},
        )
        risk = assess_risk(risk_facts(attack_sources=[source]), "w")
        self.assertEqual((risk["risk"], risk["level"], risk["minimum_posts"]), ("approaching", 1, 2))
        self.assertTrue(risk["sizing"].startswith("attack expected: 1 polluted_spawner"))
        self.assertEqual(risk["nearest_attack_source"], 250)
        group = dict(source, type="unit", kind="moving_group", unit_number=10)
        self.assertEqual(assess_risk(risk_facts(attack_sources=[group]), "w")["level"], 1)
        with self.assertRaises(ValueError):
            assess_risk(risk_facts(attack_sources=[dict(kind="polluted_spawner", position={"x": None, "y": 0})]), "w")

    def observation(self, level=1, technologies=None):
        return dict(
            tick=100000,
            technologies=technologies or {"automation-science-pack": True},
            adaptive_defense_risk=dict(level=level, risk="approaching", sizing="attack expected: 1 polluted_spawner"),
            available_research={"technologies": [{"technology": "gun-turret"}]},
        )

    CAPS = {
        "research": {"parameters": {}, "requires": []},
        "defense_perimeter": {"parameters": {}, "requires": ["starter"]},
    }

    def test_expected_attack_pulls_gun_turret_research_then_the_perimeter_forward(self):
        jobs = [dict(capability="starter", status="succeeded", parameters={})]
        self.assertEqual(defense_urgency_nodes(jobs, [], self.observation(level=0), self.CAPS), [])
        nodes = defense_urgency_nodes(jobs, [], self.observation(), self.CAPS)
        self.assertEqual(
            [(n["id"], n["priority"]) for n in nodes], [("auto_research_gun_turret", URGENT_DEFENSE_PRIORITY)]
        )
        self.assertEqual(defense_urgency_nodes(jobs, nodes, self.observation(), self.CAPS), [])
        done = self.observation(technologies={"automation-science-pack": True, "gun-turret": True})
        self.assertEqual(
            [n["id"] for n in defense_urgency_nodes(jobs, [], done, self.CAPS)], ["auto_defense_perimeter"]
        )
        built = jobs + [dict(capability="defense_perimeter", status="succeeded", parameters={})]
        self.assertEqual(defense_urgency_nodes(built, [], done, self.CAPS), [])
        stalled = jobs + [dict(capability="defense_perimeter", status="blocked", parameters={})]
        self.assertEqual(defense_urgency_nodes(stalled, [], done, self.CAPS), [])

    def test_autopilot_proposes_the_urgent_node_once(self):
        jobs = [dict(capability="starter", status="succeeded", parameters={})]
        nodes = autopilot_nodes(jobs, [], self.observation(), self.CAPS, limit=5)
        ids = [n["id"] for n in nodes]
        self.assertEqual(ids.count("auto_research_gun_turret"), 1)
        self.assertEqual(
            next(n for n in nodes if n["id"] == "auto_research_gun_turret")["priority"], URGENT_DEFENSE_PRIORITY
        )


if __name__ == "__main__":
    unittest.main()
