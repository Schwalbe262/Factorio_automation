"""Engines and magazines on a planned world: generic ``produce`` rows (hand-over).

On a world with a site service and a published planned bus,
``EngineProduction`` (``engine_production``) and ``AmmoProduction``
(``ammo_production``) delegate siting, supply, layout, build and proof to
``production_rows.ProductionRows`` with a fixed row request, the way
``industrial.IndustrialOperations.planned`` hands steel to
``planned_furnaces``. Their development-map paths (``BusTap``,
``free_tap_parameters``, fixed anchors) stay for worlds without a planned bus.

* engines: ``engine-unit`` from steel (the planned steel block's free board
  output port, routed like any board port), and gears + pipes made in-line by
  twin sub-rows from one planned-bus iron tap (``production_layout``);
* magazines: ``firearm-magazine`` from one planned-bus iron tap. The
  commissioned row's stock chest is the turret refill source
  (``AmmoProduction.ensure_full_magazines``).

Both rows reserve their growth slots (``production_layout``: as many main
slots again as built), publish the whole plan on the board before building
(``ProductionRows._publish``) and take the bus tap with the block
(``planned_tap.connect_inputs``). No defense coverage is placed here: the
perimeter is planned separately (``defense_perimeter``).
"""

from __future__ import annotations

from . import planned_tap

# 6 engines/min: the old engine district's rating; one built main machine at
# assembling-machine-2 speed covers 4.5/min, so the row starts with two.
ENGINE_ROW = {"item": "engine-unit", "rate_per_minute": 6}
# 10 magazines/min: one assembler; the turret refill draws from its stock chest.
AMMO_ROW = {"item": "firearm-magazine", "rate_per_minute": 10}


def active(rows, obs, item: str) -> bool:
    """True on a planned world: a site service and a planned bus for this world, or a saved row."""
    if rows is None or getattr(rows, "site", None) is None:
        return False
    state = rows.state(item)
    if state and state.get("world_id") == obs.get("world_id"):
        return True
    board = rows._board(obs)
    return board is not None and planned_tap.published(board, obs["world_id"]) is not None


def step(rows, obs, request: dict) -> dict:
    """``rows.step`` for the fixed request; a recipe a coming research unlocks waits, never blocks."""
    result = rows.step(obs, dict(request))
    if result.get("status") == "blocked" and result.get("reason") == "production_row_recipes_locked":
        missing = [str(name) for name in result.get("missing", [])]
        return {
            "status": "waiting",
            "reason": f"waiting for research to unlock {', '.join(missing) or 'recipes'} for the {request['item']} row",
            "missing": missing,
        }
    return result


def commissioned(rows, item: str) -> bool:
    """The produce row for ``item`` passed its proof once (``phase`` verified)."""
    return rows is not None and rows.state(item).get("phase") == "verified"
