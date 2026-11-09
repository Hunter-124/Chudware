# Contract: `aicad.harness/1` — v1

> **Frozen seam.** This section is append-only; changes to it require the versioning
> policy in [README.md](README.md). Executable model and persistence:
> `services/harness/model.py`. This is a document study, not a separate document kind.

## 1. Study and model

A document may contain multiple studies with `type: "harness"`, `name` equal to the
harness id and `engine: "py-harness"`. The study stores the model at
`params.harness`, with `params.schema: "aicad.harness/1"`; `results.summary` stores a
summary. `sim_study_list`, `sim_study_add`, `sim_study_edit` and `sim_study_delete`
provide persistence. Load without an id is accepted only when exactly one harness
exists. Save validates references and requires write confirmation; an unconfirmed edit
is not reported as saved. Loading reconstructs derived routing state.

```jsonc
{
  "schema": "aicad.harness/1", "id": "harness_id", "name": "Harness",
  "defaults": {"packing_factor": 0.6944444444, "bend_radius_x_od": null,
               "clearance_mm": 2.0, "service_loop_mm": 0.0, "strip_mm": 0.0},
  "packing_factor_basis": "estimate",
  "connectors": [], "wires": [], "nodes": [], "segments": [],
  "keepouts": [], "loads": [], "geometry": {}, "analyses": {}
}
```

Distances and coordinates are world mm, angles degrees; vectors are `[x,y,z]`.
Defaults may be overridden. Zero service-loop or strip allowance means unspecified,
not evidence that no allowance is needed. `geometry` and `analyses` hold generated
geometry and analysis state; edits clear stale `analyses`. `loads` is available for
load records; electrical checks also accept explicit loads and do not persist their
results. Keepouts supplied to routing are retained; forms are described in
[worker RPC](worker_rpc.md#harness-routing-and-path-checks).

## 2. Connectors and wires

A connector has `id`, unique `ref`, canonical `family`, `family_label`, `pins`,
`frame: {origin, normal, up}` and `cavities: [{pin, signal, wire}]`; optional fields
are `part_number`, `pitch_mm`, `rows`, `variant`, `gender`, `component`, `attach`
and `exit_dir`. Cavities start numbered as strings from `1`; the wire field identifies
attached wire(s). One wire per cavity is the default; deliberate shared pins require
`allow_shared_pin`. Pin counts are 1–256. Family common counts warn rather than reject.
The frame origin is the wire exit; normal is the outward mounting-face normal;
`exit_dir` defaults to normal and points into free space. Normal and up form the
normalized frame. The connector routing node has the same id and origin.

Canonical families: `jst_ph`, `jst_xh`, `jst_gh`, `jst_sh`, `molex_microfit_3`,
`molex_minifit_jr`, `header_2p54`, `dsub_de9`, `deutsch_dt`, `m8`, `m12_a`,
`m12_b`, `m12_d`, `m12_x`, `phoenix_mstb_5p08`, `phoenix_mc_3p5`,
`terminal_block`, `generic`. Family table entries describe pitch and common pin
counts, not housing envelopes. Terminal block pitch must be supplied; a supplied pitch
for a fixed-pitch family must agree with its table.

Each wire has `id`, `from` and `to` endpoint objects `{connector, pin, treatment?}`,
`insulation_od_mm`, `kind`, `length_mm`, `service_loop_mm`, `strip_mm`,
`min_bend_radius_mm`, `bend_radius_basis`, `bend_radius_confidence`, and `segments`
(segment ids). Endpoints can be supplied as `REF:pin` and resolve to connector ids;
they must be on different connectors. Supply `gauge_awg` or `csa_mm2`, and actual
insulated OD; the latter cannot be inferred from conductor gauge. Optional fields:
`insulation`, `color`, `net`, `signal`, `group`, `signal_class`,
`shield_ground_end`, `temp_rating_c`, `stranded`, `conductor_class`,
`r20_ohm_per_km`, `insulation_thermal_conductivity_w_mk`, `emissivity`,
`part_number`, `notes`.

| Field | Values |
|---|---|
| `kind` | `wire`, `twisted_pair`, `shielded`, `coax`, `ribbon` |
| `signal_class` | `power`, `signal`, `data`, `shielded_drain`, `ground` |
| `shield_ground_end` | `from`, `to`, `both`, `none`; only shielded/coax or shielded drain |
| endpoint `treatment` | `crimp`, `ferrule`, `solder`, `tinned`, `bare` |

Ribbon requires positive `ribbon_width_mm` and `ribbon_thickness_mm`; its routing OD
is their hypotenuse. It is routed as a circular envelope, not an oriented rectangular
sweep. Each wire record is one conductor/connection; grouped cable construction must
be supplied rather than inferred.

## 3. Routing state

Nodes are `{id, kind, pos}`, with kind `connector`, `breakout`, `clamp`, `waypoint`
or `splice`. A clamp is a node, not a separate clamp list: `harness_edit` splits a
segment at a strictly interior distance or on-path point. Segments are
`{id, a, b, path, od_mm, min_bend_radius_mm, wires, length_mm}`; `path` is a dense
polyline ordered from node `a` to `b` with endpoints snapped to node positions.
Optional segment fields include `sleeve_id_mm`, `override_od_mm`,
`override_packing_factor`, `override_min_bend_radius_mm` and derived OD and bend
basis/confidence fields. The graph is a forest; wire paths use the unique tree path.
Recomputation rebuilds segment membership, wire routes and lengths, bundle ODs and
bend estimates. An unrouted wire has `length_mm: null`, not zero. Cut length is routed
length plus service loop plus `strip_mm`, the total termination allowance for both ends.
All bend radii and caller multipliers refer to the centerline.

The read-only engine RPC `harness_route` plans a centerline using endpoint positions
and outward directions, OD, radius, clearance, keepouts and optional routing controls;
`harness_path_check` checks length, local bends and clearance. See the exact RPC
parameters and limitations in [worker_rpc.md](worker_rpc.md#harness-routing-and-path-checks).
Clearance uses world display tessellation, not exact B-rep; no obstacle is not a
measured clearance. The MCP router uses those RPCs and persists the resulting graph.

## 4. MCP surface

`services/mcp/harness_tools.py` registers:

| Tool | Purpose |
|---|---|
| `harness_new` | Create a named harness study. |
| `harness_get` | Read a harness, or list with `harness_id="*"`. |
| `harness_add_connector` | Add a connector with world frame or face attachment. |
| `harness_add_wire` | Add a fully specified pin-to-pin wire. |
| `harness_connect` | Convenience pin-to-pin wire creation. |
| `harness_from_netlist` | Seed wires from explicit schematic endpoint mapping. |
| `harness_route` | Route wires through the engine, reusing and splitting the forest. |
| `harness_check` | Check connectivity, geometry, fill, exits, segregation and shielding; optional sampled motion. |
| `harness_build_geometry` | Build sweep bodies or a persistent assembly harness component. |
| `harness_report` | Read cut list, lengths, BOM and connector tables. |
| `harness_formboard` | Write SVG, DXF or PDF formboard and report overlap/length verification. |
| `harness_edit` | Edit wire/default/segment overrides, remove wire or add clamp. |

`services/mcp/harness_electrical_tools.py` registers read-only tools:

| Tool | Purpose |
|---|---|
| `harness_electrical_check` | Screen current, voltage drop, thermal/ampacity and protection from supplied evidence. |
| `harness_bus_check` | Screen I2C, CAN, CAN FD, RS485, UART or SPI signal integrity. |
| `harness_cable_model` | Estimate cable R/L/C, impedance and delay from construction or datasheet inputs. |
| `harness_to_bus_links` | Convert routes and complete templates into firmware simulation bus links. |

Outputs are bounded/paged. Formboard drawing-sheet integration is not supported.

## 5. Sources and scope

- Static bend estimates cite [NASA-STD-8739.4A §7.2.19 Table 7-1](https://s3vi.ndc.nasa.gov/ssri-kb/static/resources/nasa-std-8739.4a.pdf) and the [inside-radius convention](https://pickeringconnect.com/blog/minimum-bend-radius/): individual polyimide 10× OD, other individual wires 6× OD (extrapolated where NASA has no generic individual-wire row); ribbon 2× envelope inside radius per the cited IPC/WHMA report. Bundles use 10× for polyimide, 6× for coax/large/unknown conductor, otherwise 3×; centerline adds half OD and accommodates member radii. These are low-confidence installed static screens, not dynamic-flex or workmanship certification; use actual cable datasheets.
- Bundle estimates cite [RapidHarness](https://rapidharness.com/support/design-types/harnesses/bundle-diameters) and [One Pull Wire](https://onepullwire.com/5-steps-to-calculate-the-diameter-of-bundled-cable/). Default area packing factor is `1/(1.2**2)`, an example rather than universal packing; coverings and grouped/twisted construction are not inferred. Override from measurement or datasheet.
- `services/harness/wire_data.py` cites ASTM B258 for AWG, ASTM B3/B193 and IEC 60028 for copper properties, IEC 60228 for metric resistance, NEC 310.16 and 310.15 for building-wire ampacity/corrections, and NASA MSFC-STD-3012A for its stated spaceflight conditions. NEC tables are limited to their specified conditions and 14 AWG and larger; neither thermal estimates nor spaceflight tables certify chassis wiring.
- The same file's insulation table cites Paragon's wire chart, Midland fluoropolymer data, Testo emissivity, and the material-specific references recorded with each table entry. PVC, PTFE, FEP, ETFE, XLPE, silicone and TPE values are representative screening values, not specifications for an unidentified wire. Emissivity varies with surface and temperature; actual continuous rating and formulation require manufacturer data. Unknown/ambiguous material properties remain unknown.

## 6. Amendment (2026-11-06): routing precision, junctions, graph editing

Additive; it supersedes the earlier text only where stated.

**Full-precision geometry.** Segment `path` points and node `pos` values are stored
unrounded (rounding a dense arc shifts its sampled bend radius). A segment's end points
equal its nodes' `pos` exactly, and a node created by a split (`breakout`, `clamp`) sits
exactly on both halves. Derived scalars keep their rounding: `length_mm` 3 decimals,
`od_mm` and `min_bend_radius_mm` 4 decimals.

**One bundle requirement.** `harness_route` plans a joining segment for the OD and
centerline bend radius that `recompute_derived` stores for it (same rounding, packing
factor and segment overrides, computed by `model.bundle_requirements`), so the routed arc
radius meets the later check. The engine check accepts a bend when
`radius >= min_bend_radius_mm * (1 - 1e-6)` and ignores collinear interior vertices
([worker RPC](worker_rpc.md#harness-routing-and-path-checks)).

**Junction fillets.** A route endpoint on a connector node uses the connector exit
direction. A non-connector join node (`breakout`, `clamp`, `waypoint`, `splice`) gets no
router direction: a wire leaves a bundle with its own bend radius instead of merging
tangentially. The full-wire check (`harness_route` final check, `harness_check`) sends the
wire's concatenated centreline together with `junctions`, the indices where the wire changes
between two segments that are not the two halves of one split (`model.wire_route`, using a
node's `through`, below; omitted when there are none). A wire running straight through a
breakout or clamp along the trunk is therefore not at a junction, while a wire leaving along a
branch is; a node without `through` (legacy data) counts every join as a junction. The engine
judges a turning vertex at a junction index as a fillet over the adjacent straight runs
(`min(L_in, L_out) / tan(theta/2)`), never as a sampled arc or isolated kink, and reports
a failure as `junction_fillet` with `available_fillet_radius_mm`; junction fillets are not
clearance-checked ([worker RPC](worker_rpc.md#harness-routing-and-path-checks)).

**Pruning.** Removing a wire, removing a segment, editing or removing a connector and
`reroute` unroute wires, then: segments carrying no wire are deleted; non-connector nodes
with no incident segment are deleted; a `breakout` left with exactly the two halves its
`through` records is merged back into one segment when their non-derived fields (everything
except `id`, `a`, `b`, `path`, `od_mm`, `min_bend_radius_mm`, `wires`, `length_mm`,
`od_basis`, `bend_radius_basis`, `bend_radius_confidence`) are identical. A breakout left
with a branch and one trunk piece is kept, since merging them would put the junction corner
inside one bundle. Replies report
`removed_segments`, `removed_nodes` and `merged_segments`.

**`harness_route` additions.** `reroute: bool = false` first removes the segments carrying
the selected wires so they are routed afresh; without it wires whose connectors are already
joined are skipped. A segment that also carries unselected wires is refused with
`reroute_shared_segment` and `wires` listing them. Replies add `removed_segments` when
`reroute` is set; `segments` lists the segments this call created (split halves of them
included). When the final check fails on a segment the call did not create, the
`route_check_failed` error sets `predates_call: true`, names `segment` and `wires`, and
hints `reroute=True` with those wires. Violation reports are in `detail` (formerly
`result`).
`prefer_paths` sent to the engine are the centrelines of the two trees being joined,
simplified (Ramer-Douglas-Peucker, 0.5 mm) and ordered nearest the route endpoints first,
within the engine's 128-polyline / 256-leg limit; dropped centrelines add a warning.

**Join selection.** When joining two trees, the join is the closest attachment between them
in either direction: every joinable node of one tree, measured from its routing departure
point (a connector's origin plus `exit_dir` times twice the bundle requirement's bend
radius, the exit stub the router builds; any other node's position), against the nearest
attachment of the other tree, which is a joinable node or a point on a straight leg of one
of its segments (then split). A node or leg is joinable only if the joining wires reach
their own connectors from it without running through another connector (both of that
connector's segments leave along its exit direction, a 180 degree reversal): a connector is
a join end only when it is the own end of every wire using the join, and a segment can be
tapped only on the side that keeps this true. A leg point keeps the largest own
`min_bend_radius_mm` of the joining wires from both leg ends, so a wire turning at the
junction has room for its fillet (arc chords never qualify), and it lies beyond the exit
stub of a connector end of its segment (twice the bend radius the segment was routed for, or
its requirement if older): a junction inside a stub would make its wires turn or double back
within the straight the bundle must leave its connector with. Where the wires turn through a
large angle (arriving from the departure point, continuing toward their connectors) more run
is needed, `margin x tan(turn / 2)`: the point nearest the departure point yields to the best
of a few others on the leg when it falls short. The start node and the real target point
are what the router receives, so a connector that exits away from a bundle attaches ahead of
its exit rather than behind it. When neither tree offers an attachment the call fails with
`no_join_point`, naming the wire and the joining wires.

**Bundle growth.** A join can turn a bundle routed for fewer wires into one with a larger
requirement. `harness_route` therefore records, per segment it creates (split halves
inherit), the bundle its route was planned for and the index of the route that made it. The
k-th route of a call joins the same two trees in every pass, whatever it attaches to, so the
index identifies the route's own wires and keys a floor on its bundle. After the routing
loop it checks every segment; a failing segment that carries only selected wires, has no
author fields (overrides, sleeves) and whose requirement exceeds what its route was planned
for raises its route's floor to that requirement. The harness is then restored to its state
at the start of the call and every route is made again, each planned for at least its floor.
Every pass either raises a floor, which is bounded by the bundle of every selected wire, or
ends the call, so the call converges; the reply then adds `repathed_segments` (ids discarded
across passes) and `route_passes`. A route that cannot be made at its floor (no path, blocked
exit stub, search budget) fails at once with the engine's `code`, the wire, endpoints and
`detail` as for any route, plus `bundle_wires`, `od_mm`, `min_bend_radius_mm` and
`route_passes`, and a hint stating the wires, the radius and the exit stub length it needs.
A bundle that still fails at the size it was planned for fails with `route_check_failed`
(`segment`, `wires`, `od_mm`, `min_bend_radius_mm`, `route_passes`). A failing segment that
cannot be repaired by re-pathing (its requirement did not outgrow its route, it carries wires
outside `wire_ids`, it has author fields, or the call did not create it) fails at once:
`predates_call: true` when the call did not create it.

**Node provenance, `through`.** A `breakout` or `clamp` node created by splitting a segment
(`model.split_segment_at`) carries an optional `through: [first_half_id, second_half_id]`, the
two segments that were one continuous bundle. It is kept correct as the graph changes: when a
segment named in an end node's `through` is split, the half adjacent to that node replaces it;
when `prune_graph` merges a breakout (second segment dropped into the first), the first
replaces the second in the far node's `through`; a node whose two segments no longer both touch
it loses `through` (so a segment id that `next_id` later reuses can never match stale
provenance).

**Arc context.** A segment cut out of a router arc (by a breakout or clamp inside it) begins
or ends with a chord or two of that arc, which alone read as an isolated kink. The
per-segment bundle checks (`harness_route` final check, `harness_check`) therefore send the
segment's path extended with through-continuation context (`model.segment_check_path`): at
each end node, the continuation of this segment is its `through` partner when the node records
one; a branch (not in `through`) and any segment at a node without `through` have none, since
guessing from leg angles would lend a nearly tangent branch the trunk's points. The
continuation, oriented to continue the path, lends its
points (excluding the shared node) until 64 points or a length of pi times this segment's
`min_bend_radius_mm`. No continuation means no context on that side. `report_range`
`[first, last]` is the segment's own vertex indices inside the extended path and is omitted
when there is no context on either side. Only geometry is borrowed: the OD and bend
requirement are the segment's. The engine estimates bends on the whole path but reports
findings, `min_bend_radius_found_mm`, clearance and lengths for the range only
([worker RPC](worker_rpc.md#harness-routing-and-path-checks)); the tools return finding
`index` values and `length_mm` relative to the segment's own path, in `harness_check` and
in a route failure's `detail`.

**`harness_edit` additions.** Parameters `connector_id`, `frame` and `exit_dir`; actions:

| Action | Behaviour |
|---|---|
| `remove_segment` | `segment_id`; its wires become unrouted, then prune; reports `unrouted_wires`. |
| `edit_connector` | `connector_id` plus any of `frame{origin,normal,up}` (omitted parts keep their value) and `exit_dir`; validated like `harness_add_connector`. A changed frame drops the connector's stored `attach` face placement. Segments touching the connector are removed; reports `unrouted_wires`, `removed_segments` and `changed` (false, with nothing removed, when the values are unchanged). |
| `remove_connector` | Only when no wire references the connector, else `connector_in_use` with `wires`; also removes its node. |
| `delete_harness` | Deletes the harness study and returns `deleted_harness`; geometry built from it is not removed. |

**Error context.** When the engine router or a path check refuses inside `harness_route`,
the `HarnessError` keeps the engine `code`, prefixes the message with the wire id (or
`bundle sN` / `wire wN` for checks), adds `wire`, `start` and `end`
(`{node, kind, pos, connector?}`, `connector` being the ref), and carries the unchanged
engine reply in `detail`. Engine fields such as `endpoint`, `obstacle`, `distance_mm`,
`required_mm`, `inside_solid`, `stub_mm`, `free_mm`, `dir`, `expanded_nodes`, `grid_mm`
and `max_nodes` are read from there; for blocked endpoints, blocked exit stubs
(`exit_blocked`) and `budget` the `hint` states them. Fix a blocked connector with
`harness_edit(action="edit_connector")` rather than creating new harnesses or probing with
extra routes.
