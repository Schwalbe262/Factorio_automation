"""Paid, staged electric upgrade of the dedicated coal source, preserving trunk.

The upgrade accepts a saved coal plan by what it is. Every supported cell has
an east-facing burner drill whose electric replacement is centred half a tile
up-left of it, so the electric drill drops onto the same belt tile as the
burner did. The cells differ only in where their burner fuel inserter stands:

- ``legacy_trunk``: plans regularised by ``RegularizePower`` (policy
  ``pcb-trunks-v1``). Behaviour is unchanged from before variants existed.
- ``boundary_compact``: the boundary cell the v2 power job builds on new maps
  (``compact_coal_cell``); the fuel inserter is outside the 3x3 footprint and
  is recovered after the output proof, like the legacy cell.
- ``inner_fuel_arm``: the base ``FactoryBuilder._coal_plan`` cell; the fuel
  inserter is inside the 3x3 footprint, so it leaves the plan and is mined
  (identity-guarded) before the electric drill is placed.

Any other layout is refused with ``electric_coal_unsupported_coal_layout``.

Power loop (fleet aud-1114-0930): the electric drill feeds the boiler that powers it. Order of
operations: pole chain built and on a generating network, coal reserve buffered
(``CUTOVER_BOILER_FUEL_J``, ``CUTOVER_BELT_COAL``), then the burner drill is mined and the
electric one built. Afterwards a brownout is not a failure (``drill_power``); a dead plant is
restarted by ``power_coal_feed.dead_plant_restart`` and otherwise blocks with a ``power`` need.

Remedy ladder (``remedy.RELAXATIONS["electric_coal"]``, what-if ``ElectricCoal.plan``): replan,
then the site rung (the legacy cell tries the whole pole goal ring), then the clear rung (a pole
of ours in the footprint is relocated by ``pole_relocation``). Unowned starter cell parts in the
footprint are mined without a rung (``_clearable_starter``); an entity of another live block
is never removed: its owner is named in the ``preflight`` need.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from .board import entity
from .power_grid import plan_connection


EAST = 4
# Starter cell entities (burner drill -> furnace or chest) that may be cleared out of the
# electric footprint (ElectricCoal._clearable_starter), at most this many.
STARTER_CELL_NAMES = frozenset({"burner-mining-drill", "stone-furnace", "wooden-chest", "iron-chest"})
MAX_CLEARED_STARTER = 4
# Remedy ladder options (remedy.RELAXATIONS["electric_coal"]): the site rung lets the legacy
# cell try the whole pole goal ring; the clear rung relocates a pole of ours out of the footprint.
POWER_RING_OPTION = "electric_coal_power_ring"
RELOCATE_POLE_OPTION = "electric_coal_relocate_pole"
# Coal buffered for the switch-over (the burner drill is mined before the electric one is built,
# a few seconds without a feed drill): boiler fuel and coal already on the feed belts.
CUTOVER_BOILER_FUEL_J = 8_000_000
CUTOVER_BELT_COAL = 8
LEGACY_TRUNK_POLICY = "pcb-trunks-v1"
# Template, layout policy and fuel inserter (dx, dy, direction) relative to
# the burner drill centre. Both generators only ever emit east-facing cells.
COAL_LAYOUTS = {
    "boundary_compact": {
        "template": "dedicated_boundary_coal_feed_v2",
        "layout_policy": "pcb-starter-boundary-coal-v2",
        "fuel_inserter": (1.5, 0.5, 4),
    },
    "inner_fuel_arm": {
        "template": "dedicated_coal_feed",
        "layout_policy": None,
        "fuel_inserter": (-1.5, -0.5, 12),
    },
}
# The electric drill centre relative to the burner drill centre.
REPLACEMENT_OFFSET = (-0.5, -0.5)
# The belt both drills drop onto, relative to the burner drill centre.
DROP_BELT_OFFSET = (1.5, -0.5)
# The first power pole goal relative to the electric drill centre. Variant
# layouts also try the other tiles of the ring touching the 3x3 footprint.
POWER_GOAL_OFFSET = (2, -2)
POWER_GOAL_RING = tuple(
    [POWER_GOAL_OFFSET]
    + [
        (dx, dy)
        for dy in (-2, -1, 0, 1, 2)
        for dx in (-2, -1, 0, 1, 2)
        if max(abs(dx), abs(dy)) == 2 and (dx, dy) != POWER_GOAL_OFFSET
    ]
    # One tile further out still supplies the drill (small-pole supply reaches
    # 2.5 tiles). Needed when the drill sits inside the patch and the whole
    # inner ring is ore, where a pole may not stand (seed 283895142).
    + [(dx, dy) for dy in range(-3, 4) for dx in range(-3, 4) if max(abs(dx), abs(dy)) == 3]
)


def replacement_for(drill_position):
    """The electric drill that replaces a burner drill at ``drill_position``."""
    return entity(
        "electric-mining-drill",
        drill_position["x"] + REPLACEMENT_OFFSET[0],
        drill_position["y"] + REPLACEMENT_OFFSET[1],
        EAST,
    )


def inside_footprint(center, position):
    """True when a one-tile entity centre lies inside the 3x3 drill footprint."""
    return abs(position["x"] - center["x"]) < 1.5 and abs(position["y"] - center["y"]) < 1.5


def east_drop(center, vector):
    """Drop point of an east-facing drill, from its north-facing place vector.

    Mirrors the survey Lua: rotating (vx, vy) clockwise gives (-vy, vx).
    """
    return {"x": center["x"] - vector[1], "y": center["y"] + vector[0]}


def matching_belt(entities, drop):
    """The plan belt whose tile contains ``drop``, or None."""
    return next(
        (
            e
            for e in entities
            if e["name"] == "transport-belt"
            and abs(e["position"]["x"] - drop["x"]) < 0.49
            and abs(e["position"]["y"] - drop["y"]) < 0.49
        ),
        None,
    )


def _same_spec(a, b):
    return a["name"] == b["name"] and a["position"] == b["position"]


def drill_power(obs, drill) -> str:
    """Power of the observed electric coal drill: ``powered``, ``brownout``, ``dead`` or ``unconnected``.

    Fleet aud-1114-0930 (2118231430, 3317405034, 3416914852): one boiler, twenty electric drills; the
    plant was overloaded, the drill read ``low_power`` with its buffer sometimes empty, and one
    ``energy <= 0`` sample blocked the upgrade for good although coal still arrived. A brownout is the
    plant's size, not this drill's fault: the drill mines at the satisfied fraction. ``dead`` (no
    generating engine on its network: the boiler ran dry, 2363694308) needs the plant restarted.
    """
    from .power_grid import supplied_networks

    if not drill.get("electric_network_connected"):
        return "unconnected"
    if drill.get("status_name") == "low_power":
        return "brownout"
    if float(drill.get("energy", 0) or 0) > 0:
        return "powered"
    if drill.get("electric_network_id") is not None and drill["electric_network_id"] in supplied_networks(obs):
        return "brownout"  # engines generate at their limit; the drill's buffer reads empty this tick
    return "dead"


def poles_on_plant(obs, power_plan) -> bool:
    """True when an observed pole of ``power_plan`` stands on a network with a generating engine.

    The cutover mines the burner drill (the plant's only coal feed until then) before the electric
    drill stands, so the pole chain must be proven live first. A plan without poles counts as live
    (the drill sits on an existing pole's supply area).
    """
    from .power_grid import supplied_networks

    poles = [e for e in (power_plan or {}).get("entities") or () if e.get("name") == "small-electric-pole"]
    if not poles:
        return True
    networks = supplied_networks(obs)
    wanted = {(p["position"]["x"], p["position"]["y"]) for p in poles}
    return any(
        e.get("name") == "small-electric-pole"
        and (e["position"]["x"], e["position"]["y"]) in wanted
        and e.get("electric_network_id") in networks
        and e.get("electric_network_id") is not None
        for e in obs.get("entities", ())
    )


def classify_coal_layout(plan):
    """Decide which supported coal cell a saved plan is, from the plan alone.

    Rules:
    - a missing plan is refused with ``starter_coal_plan_required``;
    - ``pcb-trunks-v1`` is the legacy variant: every burner inserter is
      obsolete and recovered after proof, exactly as before;
    - otherwise template and layout policy must match one ``COAL_LAYOUTS``
      row, the drill must be an east-facing burner drill, the plan must hold
      exactly one burner inserter at that row's position and direction, no
      other plan entity may stand inside the electric footprint, and a plan
      belt must sit on the drop tile;
    - the fuel inserter is ``displaced`` (removed before placement) when it is
      inside the electric footprint and ``obsolete`` (removed after proof)
      otherwise.
    """
    if not plan or not plan.get("drill") or not plan.get("entities"):
        return {"ok": False, "reason": "starter_coal_plan_required"}
    entities = plan["entities"]
    inserters = [e for e in entities if e["name"] == "burner-inserter"]
    if plan.get("layout_policy") == LEGACY_TRUNK_POLICY:
        return {"ok": True, "variant": "legacy_trunk", "displaced": [], "obsolete": inserters}
    identity = {"template": plan.get("template"), "layout_policy": plan.get("layout_policy")}
    variant = next(
        (
            name
            for name, row in COAL_LAYOUTS.items()
            if row["template"] == identity["template"] and row["layout_policy"] == identity["layout_policy"]
        ),
        None,
    )
    if variant is None:
        return {"ok": False, "reason": "electric_coal_unsupported_coal_layout", **identity}
    drill = plan["drill"]
    if drill["name"] == "electric-mining-drill":
        return {"ok": False, "reason": "coal_drill_already_upgraded"}
    if drill["name"] != "burner-mining-drill" or drill.get("direction", 0) != EAST:
        return {"ok": False, "reason": "electric_coal_drill_facing_unsupported", "variant": variant, **identity}
    x, y = drill["position"]["x"], drill["position"]["y"]
    dx, dy, direction = COAL_LAYOUTS[variant]["fuel_inserter"]
    expected = {"x": x + dx, "y": y + dy}
    if len(inserters) != 1 or inserters[0]["position"] != expected or inserters[0].get("direction", 0) != direction:
        return {
            "ok": False,
            "reason": "electric_coal_fuel_inserter_not_at_template_position",
            "variant": variant,
            "expected": expected,
            "found": [e["position"] for e in inserters],
        }
    center = replacement_for(drill["position"])["position"]
    conflicts = [
        e
        for e in entities
        if not _same_spec(e, drill) and e["name"] != "burner-inserter" and inside_footprint(center, e["position"])
    ]
    if conflicts:
        return {
            "ok": False,
            "reason": "electric_coal_footprint_holds_retained_plan_entity",
            "variant": variant,
            "conflicts": conflicts,
        }
    drop = {"x": x + DROP_BELT_OFFSET[0], "y": y + DROP_BELT_OFFSET[1]}
    if matching_belt(entities, drop) is None:
        return {"ok": False, "reason": "electric_coal_drop_belt_missing_from_plan", "variant": variant}
    displaced = [e for e in inserters if inside_footprint(center, e["position"])]
    obsolete = [e for e in inserters if not inside_footprint(center, e["position"])]
    return {"ok": True, "variant": variant, "displaced": displaced, "obsolete": obsolete}


class ElectricCoal:
    def __init__(self, game, bootstrap, builder):
        self.game, self.bootstrap, self.builder = game, bootstrap, builder
        self.path = Path(game.cfg.runtime_dir) / "electric-coal.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @staticmethod
    def find(obs, part):
        return next(
            (e for e in obs["entities"] if e["name"] == part["name"] and e["position"] == part["position"]), None
        )

    def prepare(self, obs):
        """``plan`` and, when it fits, save its state (the cutover starts in ``fund_and_power``)."""
        result = self.plan(obs)
        if not result.get("ok"):
            return result
        self.state = result.pop("state")
        _atomic_json(self.path, self.state)
        return result

    def plan(self, obs):
        """The cutover plan without writing anything (the remedy what-if, ``postmortem.DRY_RUN``).

        ``{"ok": True, "state": ..., ...}`` or a failure; an obstructed footprint that is not ours
        to clear carries ``plan_needs`` (kind ``preflight``, the blocker and its owner).
        """
        self.builder._sync(obs)
        old = self.builder.state.get("coal_plan")
        layout = classify_coal_layout(old)
        if not layout.get("ok"):
            return layout
        original = self.find(obs, old["drill"])
        if not original:
            return {"ok": False, "reason": "owned_old_drill_missing"}
        if original["name"] != "burner-mining-drill":
            return {"ok": False, "reason": "coal_drill_already_upgraded"}
        replacement = replacement_for(original["position"])
        displaced = [
            {"spec": deepcopy(spec), "unit_number": (self.find(obs, spec) or {}).get("unit_number")}
            for spec in layout["displaced"]
        ]
        # Only the old drill and the displaced fuel inserter, both removed by
        # this controller, may stand in the electric footprint, and starter cell
        # entities no block owns (``_clearable_starter``), mined first as well.
        ignore = [original["unit_number"]] + [row["unit_number"] for row in displaced if row["unit_number"]]
        relocate = []
        for _ in range(MAX_CLEARED_STARTER + 1):
            survey = self._footprint_survey(ignore, replacement)
            clear = self._clearable_starter(obs, survey)
            if clear is None:
                pole = self._relocatable_pole(obs, survey)
                if pole is None:
                    break
                # Clear rung: a pole of ours is moved (pole_relocation) before the cutover.
                relocate.append({"position": deepcopy(pole["position"]), "unit_number": pole["unit_number"]})
                ignore.append(pole["unit_number"])
                continue
            spec = {"name": clear["name"], "position": deepcopy(clear["position"])}
            displaced.append({"spec": spec, "unit_number": clear["unit_number"]})
            ignore.append(clear["unit_number"])
        if not survey.get("ok"):
            return self._with_needs(obs, survey)
        recipient = matching_belt(old["entities"], survey["drop"])
        if recipient is None:
            return {"ok": False, "reason": "electric_drop_does_not_match_retained_belt"}
        power = self._plan_power(obs, layout, old, replacement)
        if not power.get("ok"):
            return dict(
                power,
                plan_needs={
                    "kind": "power",
                    "reason": power.get("reason"),
                    "at": replacement["position"],
                    "change": "a pole chain from the generating grid to the electric coal drill",
                },
            )
        state = {
            "world_id": obs["world_id"],
            "old": old["drill"],
            "old_unit_number": original["unit_number"],
            "replacement": replacement,
            "power_plan": power,
            "receiver": recipient,
            "phase": "fund_and_power",
            "layout_variant": layout["variant"],
            "displaced_inserters": displaced,
            "obsolete_inserters": [self.find(obs, e) for e in layout["obsolete"]],
            "coal_plan_before": deepcopy(old),
        }
        if relocate:
            state["relocate_poles"] = relocate
        return {
            "ok": True,
            "state": state,
            "wire_hops": power["wire_hops"],
            "replacement": replacement,
            "retained_drop_belt": recipient,
            "layout_variant": layout["variant"],
        }

    def _footprint_survey(self, ignore, replacement):
        """Native survey of the electric footprint: the first obstruction, terrain, ore and the drop tile."""
        payload = json.dumps(json.dumps({"ignore": ignore, "new": replacement}, separators=(",", ":")))
        return self.game.query(
            """
local args=helpers.json_to_table("""
            + payload
            + """);local n=args.new;local proto=prototypes.entity[n.name]
local ignore={};for _,id in ipairs(args.ignore) do ignore[id]=true end
local left=n.position.x-1.5;local top=n.position.y-1.5;local right=left+3;local bottom=top+3
for _,e in pairs(s.find_entities_filtered{area={{left+.01,top+.01},{right-.01,bottom-.01}}}) do
 if e.type~="resource" and not (e.unit_number and ignore[e.unit_number]) and e~=a and not e.prototype.collision_mask.colliding_with_tiles_only then
  return {ok=false,reason="electric_upgrade_footprint_obstructed",entity=e.name,position={x=e.position.x,y=e.position.y}}
 end
end
for x=left,right-1 do for y=top,bottom-1 do
 for layer in pairs(proto.collision_mask.layers) do if s.get_tile(x,y).collides_with(layer) then return {ok=false,reason="electric_upgrade_terrain_obstructed"} end end
end end
local radius=proto.mining_drill_radius;local amount=0
for _,ore in pairs(s.find_entities_filtered{area={{n.position.x-radius,n.position.y-radius},{n.position.x+radius,n.position.y+radius}},type="resource"}) do
 if ore.name~="coal" then return {ok=false,reason="electric_upgrade_mixed_resource"} end;amount=amount+ore.amount
end
if amount<=0 then return {ok=false,reason="electric_upgrade_no_coal"} end
local v=proto.vector_to_place_result;local drop={x=n.position.x-(v.y or v[2]),y=n.position.y+(v.x or v[1])}
return {ok=true,drop=drop,remaining=amount}
"""
        )

    def _clearable_starter(self, obs, survey):
        """The observed starter cell entity obstructing the footprint, when no block owns it; else None.

        Fleet aud-1114-0841 seed 1971789367: a starter cell's burner drill placed before the feed's
        upgrade site was reserved (``board.coal_upgrade_area``) stood in the footprint, and
        electric_coal blocked for good with ``electric_upgrade_footprint_obstructed``. A starter
        cell is hand-served busywork the planner may rebuild elsewhere; it is mined (paid,
        identity-guarded) with the displaced fuel inserter. Board blocks and power plans are kept.
        """
        live = self._obstruction(obs, survey)
        if live is None or live["name"] not in STARTER_CELL_NAMES:
            return None
        return None if self._owner(obs, live) else live

    def _obstruction(self, obs, survey):
        """The observed entity of ours (an int unit number) the footprint survey stopped at, or None."""
        if survey.get("ok") or survey.get("reason") != "electric_upgrade_footprint_obstructed":
            return None
        live = self.find(obs, {"name": survey.get("entity"), "position": survey.get("position")})
        return live if live is not None and type(live.get("unit_number")) is int else None

    def _owner(self, obs, live):
        """The plan or board block that lists ``live`` (``"power_plan"``, ``"coal_plan"``, a block key), or None."""
        key = (live["name"], live["position"]["x"], live["position"]["y"])
        owners = [(name, self.builder.state.get(name) or {}) for name in ("power_plan", "coal_plan")]
        root = Path(self.game.cfg.runtime_dir)
        try:
            board = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            board = {}
        blocks = board.get("blocks") if board.get("world_id") == obs.get("world_id") else None
        owners += [(name, block) for name, block in (blocks or {}).items() if isinstance(block, dict)]
        for name, plan in owners:
            if any((e["name"], e["position"]["x"], e["position"]["y"]) == key for e in plan.get("entities") or ()):
                return name
        return None

    def _relocatable_pole(self, obs, survey):
        """Under the clear rung (``remedy`` option ``RELOCATE_POLE_OPTION``): our pole in the footprint, else None.

        The pole may carry power on to other poles or machines, so it is never simply mined:
        ``pole_relocation`` links what it feeds first (paid, verified), republishes its owner's plan,
        then mines it guarded. A pole of another live block is moved, not the block.
        """
        from .remedy import option

        if not option(RELOCATE_POLE_OPTION):
            return None
        live = self._obstruction(obs, survey)
        return live if live is not None and live["name"] == "small-electric-pole" else None

    def _with_needs(self, obs, survey):
        """A failed footprint survey with ``plan_needs`` naming the blocker and its owner (remedy ladder)."""
        if survey.get("reason") != "electric_upgrade_footprint_obstructed":
            return survey
        live = self._obstruction(obs, survey)
        owner = self._owner(obs, live) if live is not None else None
        blocker = {"entity": survey.get("entity"), "position": survey.get("position"), "owner": owner}
        change = (
            f"the {survey.get('entity')} of {owner} moved out of the electric coal footprint"
            if owner
            else f"the {survey.get('entity')} removed from the electric coal footprint"
        )
        return dict(
            survey,
            plan_needs={
                "kind": "preflight",
                "reason": survey["reason"],
                "check": "electric_coal_footprint",
                "blockers": [blocker],
                "change": change,
            },
        )

    def _plan_power(self, obs, layout, old, replacement):
        """Plan the pole chain to the electric drill with ``plan_connection``.

        The legacy variant keeps its single goal at ``POWER_GOAL_OFFSET``, unless the ladder's
        site rung (``POWER_RING_OPTION``) is active. Other variants try the ring of tiles
        touching the 3x3 footprint, that offset first, skipping tiles a retained plan entity
        occupies; the first candidate's failure is reported when none connects.
        """
        from .remedy import option

        center = replacement["position"]
        if layout["variant"] == "legacy_trunk" and not option(POWER_RING_OPTION):
            goals = [POWER_GOAL_OFFSET]
        else:
            retained = [
                e
                for e in old["entities"]
                if not _same_spec(e, old["drill"]) and not any(_same_spec(e, d) for d in layout["displaced"])
            ]
            occupied = self.builder._occupied_by_plan(retained)
            goals = [
                offset
                for offset in POWER_GOAL_RING
                if (center["x"] + offset[0], center["y"] + offset[1]) not in occupied
            ]
        first = None
        for dx, dy in goals:
            destination = {"x": center["x"] + dx, "y": center["y"] + dy}
            power = plan_connection(self.game, self.builder, obs, destination, [replacement])
            if power.get("ok"):
                return power
            first = first or power
        if first is None:
            return {"ok": False, "reason": "electric_coal_no_free_power_goal"}
        return dict(first, power_goal_candidates=len(goals))

    def _rewrite_coal_plan(self, keep):
        """Replace the saved coal plan with its entities that pass ``keep``.

        Required items are recomputed; every other plan field is preserved.
        """
        saved = self.builder.state["coal_plan"]
        entities = [e for e in saved["entities"] if keep(e)]
        if len(entities) == len(saved["entities"]):
            return
        extra = {k: v for k, v in saved.items() if k not in {"entities", "ports", "required_items", "ok", "reason"}}
        self.builder.state["coal_plan"] = _plan(entities, saved["ports"], **extra)
        self.builder._save()

    def _displaced_inserter_action(self, obs):
        """Remove fuel inserters standing in the electric footprint.

        Rule: the inserter first leaves the saved coal plan, so no power job
        rebuilds it, then leaves the world by ordinary mining guarded by the
        unit number observed at prepare. A different unit blocks.
        """
        rows = self.state.get("displaced_inserters") or []
        if not rows:
            return None
        self.builder._sync(obs)
        specs = [row["spec"] for row in rows]
        self._rewrite_coal_plan(lambda e: not any(_same_spec(e, spec) for spec in specs))
        for row in rows:
            actual = self.find(obs, row["spec"])
            if actual is None:
                continue
            if row.get("unit_number") is None or actual["unit_number"] != row["unit_number"]:
                return {"status": "blocked", "reason": "displaced_fuel_inserter_identity_changed"}
            return {
                "type": "mine",
                "name": actual["name"],
                "position": actual["position"],
                "count": 1,
                "expected_entity_unit": actual["unit_number"],
                "expected_entity_world_id": obs["world_id"],
                "reason": f"recover {actual['name']} inside the electric drill footprint before cutover",
            }
        return None

    def _mark_cutover(self, obs):
        """Tell the builder that the drill site belongs to this cutover.

        ``ensure_power`` waits instead of rebuilding the burner drill while the
        old drill is absent and the coal plan still names it.
        """
        self.builder._sync(obs)
        marker = {"old": deepcopy(self.state["old"]), "new": deepcopy(self.state["replacement"])}
        if self.builder.state.get("electric_coal_cutover") != marker:
            self.builder.state["electric_coal_cutover"] = marker
            self.builder._save()

    def _unpowered(self, obs, actual):
        """None while the built drill has power (a brownout counts: it still mines); else its remedy.

        ``unconnected``: the saved pole chain is re-ensured (a missing pole is rebuilt, paid);
        ``dead``: the plant that this drill feeds ran dry, the coal -> boiler -> drill loop. Its
        provider is ``power_coal_feed.dead_plant_restart`` (one hand fill: carried coal, wood, or
        coal collected from a starter chest); when nothing can be fetched the job blocks with a
        ``power`` need naming that restart.
        """
        state = drill_power(obs, actual)
        if state in ("powered", "brownout"):
            return None
        if state == "unconnected":
            result = self.builder.ensure_plan(obs, self.state["power_plan"])
            if result.get("status") != "succeeded":
                return result
            reason = "replacement_electric_drill_not_connected"
            change = "the electric coal drill's pole chain connected to the grid"
        else:
            from .power_coal_feed import dead_plant_restart

            restart = dead_plant_restart(self.builder, self.bootstrap, getattr(self.builder, "catalog", None), obs)
            if restart is not None:
                return restart
            reason = "replacement_electric_drill_has_no_power"
            change = "the boiler restarted with one fuel fill (dead_plant_restart: carried coal, wood or a chest)"
        return {
            "status": "blocked",
            "reason": reason,
            "evidence": {"drill_power": state, "drill_status": actual.get("status_name")},
            "plan_needs": {"kind": "power", "reason": reason, "at": actual["position"], "change": change},
        }

    def _relocate_poles(self, obs):
        """The clear rung's pole moves (``relocate_poles``, from ``plan``) before the cutover; None when done."""
        rows = self.state.get("relocate_poles") or []
        if not rows:
            return None
        from .pole_relocation import PoleRelocation

        relocation = PoleRelocation(self.game, self.builder, getattr(self.builder, "catalog", None))
        center = self.state["replacement"]["position"]
        footprint = [[[center["x"] - 1.5, center["y"] - 1.5], [center["x"] + 1.5, center["y"] + 1.5]]]
        for row in rows:
            live = self.find(obs, {"name": "small-electric-pole", "position": row["position"]})
            fault = None
            if live is not None:
                if live.get("unit_number") != row["unit_number"]:
                    return {"status": "blocked", "reason": "footprint_pole_identity_changed"}
                fault = {"position": dict(row["position"]), "unit_number": row["unit_number"]}
            elif not relocation.active(obs):
                continue
            result = relocation.step(obs, fault, keep_out=footprint)
            if result is not None:
                return result
        return None

    def step(self, obs):
        if not obs.get("technologies", {}).get("electric-mining-drill"):
            return {"status": "blocked", "reason": "electric_mining_research_required"}
        if not self.state:
            result = self.prepare(obs)
            if not result.get("ok"):
                evidence = {k: result[k] for k in ("entity", "position") if k in result}
                return {
                    "status": "blocked",
                    "reason": result.get("reason", "electric_upgrade_preflight_failed"),
                    **({"evidence": evidence} if evidence else {}),
                    **({"plan_needs": result["plan_needs"]} if result.get("plan_needs") else {}),
                }
        if self.state["world_id"] != obs["world_id"]:
            return {"status": "blocked", "reason": "electric_upgrade_world_changed"}
        new = self.state["replacement"]
        actual = self.find(obs, new)
        if actual is None:
            if self.state["phase"] == "fund_and_power":
                moved = self._relocate_poles(obs)
                if moved is not None:
                    return moved
                if obs.get("inventory", {}).get("electric-mining-drill", 0) < 1:
                    return self.bootstrap.ensure_item(obs, "electric-mining-drill", 1)
                result = self.builder.ensure_plan(obs, self.state["power_plan"])
                if result.get("status") != "succeeded":
                    return result
                # Order of operations: the burner drill is the plant's only coal feed until the
                # cutover, so the new pole chain must be on a generating network before it goes.
                if not poles_on_plant(obs, self.state["power_plan"]):
                    return {
                        "status": "waiting",
                        "reason": "electric coal poles wait for a generating network before the burner feed is mined",
                    }
                self.builder._sync(obs)
                evidence = self.builder.power_evidence(
                    self.builder.state["power_plan"], self.builder.state["coal_plan"]
                )
                if (
                    not evidence.get("ok")
                    or evidence.get("boiler_fuel", 0) < CUTOVER_BOILER_FUEL_J
                    or evidence.get("coal_on_belts", 0) < CUTOVER_BELT_COAL
                ):
                    return {
                        "status": "waiting",
                        "reason": "wait for ordinary coal reserve before paid electric cutover",
                    }
                self.state["phase"] = "replace"
                _atomic_json(self.path, self.state)
            removal = self._displaced_inserter_action(obs)
            if removal is not None:
                return removal
            self._mark_cutover(obs)
            old = self.find(obs, self.state["old"])
            if old is not None:
                if old["unit_number"] != self.state["old_unit_number"]:
                    return {"status": "blocked", "reason": "old_coal_drill_identity_changed"}
                return {
                    "type": "mine",
                    "name": old["name"],
                    "position": old["position"],
                    "count": 1,
                    "expected_entity_unit": old["unit_number"],
                    "expected_entity_world_id": obs["world_id"],
                    "reason": "paid electric upgrade with ordinary reserve and prebuilt powered poles",
                }
            return {
                "type": "build",
                **new,
                "reason": "install normally crafted electric coal drill on preserved output",
            }
        self.builder._sync(obs)
        saved = self.builder.state["coal_plan"]
        if saved["drill"]["name"] == "burner-mining-drill":
            saved["entities"] = [
                new if e["name"] == "burner-mining-drill" and e["position"] == self.state["old"]["position"] else e
                for e in saved["entities"]
                if e["name"] != "burner-inserter"
            ]
            saved["drill"] = deepcopy(new)
            self.builder._save()
        if saved.get("required_items", {}).get("burner-mining-drill"):
            extra = {k: v for k, v in saved.items() if k not in {"entities", "ports", "required_items", "ok", "reason"}}
            self.builder.state["coal_plan"] = _plan(saved["entities"], saved["ports"], **extra)
            self.builder._save()
        if self.builder.state.pop("electric_coal_cutover", None) is not None:
            self.builder._save()
        unpowered = self._unpowered(obs, actual)
        if unpowered is not None:
            return unpowered
        if self.state["phase"] != "verify_output":
            self.state["phase"] = "verify_output"
            self.state["sample_started_tick"] = obs["tick"]
            self.state["replacement_unit_number"] = actual["unit_number"]
            _atomic_json(self.path, self.state)
            return {
                "type": "take",
                "name": "transport-belt",
                "position": self.state["receiver"]["position"],
                "item": "coal",
                "count": 100,
                "reason": "normal recovery clears output sample tile for native electric mining proof",
            }
        proof = self.proof(obs)
        if proof:
            for old in self.state.get("obsolete_inserters", []):
                if old and self.find(obs, old):
                    if self.find(obs, old)["unit_number"] != old["unit_number"]:
                        return {"status": "blocked", "reason": "obsolete_fuel_inserter_identity_changed"}
                    return {
                        "type": "mine",
                        "name": old["name"],
                        "position": old["position"],
                        "count": 1,
                        "expected_entity_unit": old["unit_number"],
                        "expected_entity_world_id": obs["world_id"],
                        "reason": "recover obsolete burner-only fuel inserter after electric output proof",
                    }
            return {
                "status": "succeeded",
                "reason": "electric coal drill produces onto preserved regular trunk",
                "evidence": proof,
            }
        return {"status": "waiting", "reason": "verify native electric coal output and retained power supply"}

    def proof(self, obs):
        if (
            not self.state
            or self.state.get("phase") != "verify_output"
            or obs["tick"] - self.state["sample_started_tick"] < 300
        ):
            return None
        actual = self.find(obs, self.state["replacement"])
        # A brownout (``low_power``) is no failed upgrade: the drill mines at the satisfied fraction,
        # and the coal on its output below is the proof (drill_power).
        if not actual or drill_power(obs, actual) not in ("powered", "brownout"):
            return None
        if self.state.get("replacement_unit_number") not in (None, actual["unit_number"]):
            return None
        if actual.get("status_name") in {"no_minable_resources", "no_mineable_resources", "no_power"}:
            return None
        receiver = self.find(obs, self.state["receiver"])
        if not receiver:
            return None
        coal = receiver.get("belt_inventory", {}).get("coal", 0)
        if coal > 0:
            self.state["output_evidence"] = {
                "tick": obs["tick"],
                "coal": coal,
                "drill_unit": actual["unit_number"],
                "belt_unit": receiver["unit_number"],
            }
            _atomic_json(self.path, self.state)
        evidence = self.state.get("output_evidence")
        if (
            not evidence
            or evidence["drill_unit"] != actual["unit_number"]
            or evidence["belt_unit"] != receiver["unit_number"]
            or not 0 <= obs["tick"] - evidence["tick"] <= 600
        ):
            return None
        return {
            "tick": obs["tick"],
            "electric_drill_unit": actual["unit_number"],
            "power_connected": True,
            "drill_power": drill_power(obs, actual),
            "observed_coal_on_direct_output": evidence["coal"],
            "output_evidence_tick": evidence["tick"],
            "coal_on_direct_output_now": coal,
            "wire_hops": self.state["power_plan"]["wire_hops"],
            "verification_ticks": obs["tick"] - self.state["sample_started_tick"],
            "scope": "paid dedicated-source upgrade and native output, not full30SPM or full-load energy proof",
        }
