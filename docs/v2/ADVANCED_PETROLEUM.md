# Parallel advanced petroleum

`AdvancedPetroleum(game, builder, catalog, perimeter)` exposes `prepare(obs)`,
`step(obs)` and `proof(obs)`. The checkpoint is `advanced-petroleum.json`; the PCB
block is `advanced_petroleum`. Preparation and proof read the world. Only `step`
persists its own checkpoint/PCB/handoff/output metadata and emits ordinary paid
builder actions or the guarded configuration action below.

The block contains one refinery, one heavy cracker, one light cracker and three
25,000-unit tanks. It preserves the basic refinery, its crude connection, all
existing fluid aliases and consumer ports. It never changes an existing machine
recipe, recovers old pipes, inserts fluids or changes research.

## Inputs and prerequisites

Native `advanced-oil-processing` and `circuit-network` research, all three actual
process recipes, and all construction recipes must be enabled before preparation.
Published, filled, exact-unit sources are required under these registry keys:

| Key | Fluid | Ownership |
| --- | --- | --- |
| `advanced-crude` | `crude-oil` | Independent normally paid source; unclaimed consumer edge |
| `advanced-water` | `water` | Independent normally paid intake; unclaimed consumer edge |

The namespaced `OilGathering(..., source_key='advanced-crude')` and
`WaterGathering(..., source_key='advanced-water')` source profiles supply these
ports. This module does not manufacture aliases, assume they exist, tee a claimed
canonical source or orchestrate an upstream controller. The root dispatcher must
provision and commission the two source profiles first. Missing aliases return
`advanced_independent_fluid_source_required` with `source_key` and `fluid`.

Both inputs use the existing exact `fluid_handoffs` declaration/binding protocol
with consumer ID `advanced-petroleum`. Source descriptors remain immutable.

## Layout and controls

The refinery faces north. Water and crude enter from the west on distinct rows;
heavy/light/gas leave on separate fixed ports. Three bounded underground water
pairs cross the internal oil channels. All surface connections and active recipe
fluid boxes are audited separately. A southern second-refinery footprint is
reserved; it is not built, connected or included in capacity.

Each cracking plant reads only its own oil tank over a separate red-wire network.
It enables when that tank reports **more than 500** units. This is a native circuit
threshold, not a guarantee of a precise minimum after machine input buffers and
crafts in progress. Tank storage and both cracker capacities are sufficient to
avoid permanent heavy/light saturation while gas has downstream demand. A full
gas network causes ordinary backpressure; no sink destroys or voids fluids.

The controller builds the complete isolated network with the first pipe after
each input source omitted. It requires empty new fluid networks and zero product
counters before saving the three exact machine baselines. It then emits:

`configure_advanced_petroleum` →
`execute_configure_advanced_petroleum(game, action)`

The action carries `expected_world_id`, `expected_actor_unit`,
`expected_catalog_fingerprint` and `plan_sha256`. The handler reloads the fixed
checkpoint, pinned catalog and exact PCB plan. Native checks validate all units,
force/surface/positions, normal quality, recipes, cold state and reciprocal fluid
edges before connecting normal circuit wires. Unexpected configuration outcomes
return `advanced_configuration_outcome_unknown`; the job runner must retain the
pending intent and require reconciliation. It must not automatically retry.

Only a fresh successful control read permits connection of the two source gaps.
Accepted ordinary builds and later proofs bind exact units; missing/replaced
paid assets block rather than silently receiving new identities.

## Capacity and outputs

For the installed 2.1.20 recipe/prototype catalog, nameplate values are:

| Quantity | Per game minute |
| --- | ---: |
| Crude input | 1,200 |
| Water at full surplus cracking | 1,590 |
| Gross heavy / light / petroleum gas | 300 / 540 / 660 |
| Gas if all heavy/light are cracked | 1,170 |

Process nameplate electric load is 840 kW. The existing native power gate checks
scheduled loads and source headroom. These are recipe-derived capacities, not
measured rates. Heavy, light and gas share the same refinery/crude supply; a heavy
or light consumer reduces the amount available for cracking. Capacities are kept
outside immutable port descriptors.

After all three machine counters advance and all native typed output networks
contain their expected fluid, publication appends:

| Registry key | Actual fluid | Port relative to refinery center |
| --- | --- | --- |
| `heavy-oil` | `heavy-oil` | (-20, -7), west |
| `light-oil` | `light-oil` | (-2, -21), north |
| `advanced-petroleum-gas` | `petroleum-gas` | (20, -4), east |

Canonical `petroleum-gas` remains the basic refinery's port. Each new port has
three protected outward approach cells. A downstream consumer uses the existing
handoff protocol. Multiple consumers require a normally built distributor.

## Validation and limits

Preparation checks finite candidate reservations, native land/resource policy,
both bounded input corridors, buried peer isolation, source pipeline extents,
future/unbuilt external transport, power and defense. It retains the existing
6-turn, 1.35-detour, 200,000-expanded-node pipe gates. Planning queries share the
existing 20-second budget; coverage preview/setup is a separate bounded helper,
so this is not an end-to-end wall-time guarantee.

The focused suite covers pure layout/capacity, source ownership, whole fake
preflight, cold gaps, counter/identity guards, append-only publication, unknown
configuration receipts, and executable Lua circuit/peer/runtime-box cases.
An actual native complete preparation/deployment has **not** been performed for
this module. Current source/research prerequisites must be commissioned before
the root's native preview. The tank control API follows the official
[single-fluid-box behavior](https://lua-api.factorio.com/latest/classes/LuaSingleFluidBoxControlBehavior.html)
and cracker control uses the official
[assembling-machine behavior](https://lua-api.factorio.com/latest/classes/LuaAssemblingMachineControlBehavior.html);
native 2.1.20 acceptance remains a deployment gate.
