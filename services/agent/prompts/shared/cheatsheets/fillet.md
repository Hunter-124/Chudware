FILLET - round selected edges. `radius` and `radius2` are dimension parameters in mm.
Use `params.type:"constant"` for one radius, or `params.type:"variable"` for endpoint
radii (`radius` and `radius2`). Both explicit modes require `refs.edges`. Other fillet
types are unsupported.

Select edges with a structured geometry query under `refs.edges`, or with explicit
edge selectors; a list can combine queries and selectors:
- {"of":"edge"}                       every edge of the body (round the whole part)
- {"of":"edge","parallel":"z"}        the vertical edges only (the 4 corners of a block); "x"/"y" too
- {"of":"edge","side":"top"}          the TOP edges / top rim   (bottom/left/right/front/back too)
- {"of":"edge","surf":"round"}        the round edges (a cylinder's rims)
- {"of":"edge","pick":"longest"}      the longest edge(s) ("shortest" too)
- {"of":"edge","near":[20,0,5],"count":1}  the single edge nearest a point
Direction words map to the standard views: top=+Z, bottom=-Z, right=+X, left=-X, front=-Y, back=+Y
(the signed-axis forms "+z"/"-x"/.. still work if you prefer them).

Example A - round ALL edges of the current body r=2:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Fillet1","params":{"type":"constant","radius":{"value":2,"expr":null}},"refs":{"edges":{"of":"edge"}}}}
]

Example B - round just the 4 vertical edges of a block r=5:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Round","params":{"type":"constant","radius":{"value":5,"expr":null}},"refs":{"edges":{"of":"edge","parallel":"z"}}}}
]

Example C - round the top rim of a round flange r=1:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"Rim","params":{"type":"constant","radius":{"value":1,"expr":null}},"refs":{"edges":{"of":"edge","side":"top"}}}}
]

VARIABLE-RADIUS fillet: with `params.type:"variable"`, `radius` and `radius2` specify
the selected edges' start and end radii; the radius law follows the contour spine.
Orientation is determined by topology, not a world-axis convention; inspect the result
if taper direction matters. Selected edges on a multi-edge contour are supported when
their endpoint constraints agree at shared vertices. Unequal radii on one closed edge,
contradictory shared-vertex radii, and variable contact joins are unsupported.
Individually resolvable positive unequal endpoints separated by at most the kernel
radius-difference precision (about 1e-7 mm) are refused instead of silently averaged.
An endpoint below its local B-rep resolution bound can enter lower correction; each
effective endpoint must exceed that bound and an unequal law must remain distinguishable.
Direction queries such as
`{"of":"edge","parallel":"z"}` work too; explicit picks are not required.
Example D - taper vertical edges from 3mm to 1mm:
[
 {"op":"add_feature","feature":{"type":"fillet","name":"TaperFillet","params":{"type":"variable","radius":{"value":3,"expr":null},"radius2":{"value":1,"expr":null}},"refs":{"edges":{"of":"edge","parallel":"z"}}}}
]

Omitting `params.type` keeps the legacy mode: absent `refs.edges` selects eligible
edges, optionally filtered by `dir`; with `refs.edges`, a nonzero `radius2` unequal
to `radius` selects a variable fillet. A negative endpoint is eligible for correction;
absent, zero or equal `radius2` retains legacy constant behavior. Prefer an explicit
mode and edge selection. Explicit variable mode stays variable even for equal or
out-of-range finite endpoints; `radius2` is required.

CORRECTION AND READBACK: resolvable positive requests that pass input/topology checks are
tried unchanged first. A bounded search can correct construction or completed validation
failures and finite nonpositive or unresolvable inputs, accepting only a valid solid
covering EVERY selected edge. A downward correction of a constant radius stops at min(0.05 mm,
0.1% of the body's thinnest extent, a tenth of the request): if only a smaller radius builds, the fillet fails instead
of producing a micro-blend. Incomplete validation stops the search, not evidence
for reducing a radius; a previously fully validated all-edge candidate is retained
when the 30 s search budget runs out. One probe may use the whole budget, so a slow
but sound build of a large spline body is not cut short. Variable endpoints are refined
in deterministic order with the other endpoint held fixed; this is not a global
optimum or a guarantee of success.
RUN-OFF: an inside-corner constant fillet wider than the planar face it rolls on (a boss or
rib standing closer to a plate's edge than the radius) is built at the REQUESTED radius and
ends flush with that face's open edge, like a SolidWorks overflow. This is tried after the
unchanged request fails and before any radius reduction, so such a fillet reports
`adjustment:"none"`. It applies only to inside corners whose support face ends at a
perpendicular convex edge and to a radius no larger than the body; outside-edge fillets
and slanted or rounded rims keep the correction search.
No selected edge is silently dropped and no unchanged no-op counts as a fillet.
Inspect the completed result, not just `ok` or a warning:
- MCP `add_features`: `features[i].data.fillet`.
- Native `doc_apply_ops` / `doc_rebuild`: `rebuild.feature_status[i].data.fillet`;
  `doc_compile_cpp`: `statuses[i].data.fillet`.
- `requested` and `effective` contain `radius` and `radius2` in canonical mm
  (`radius2` is null for constant); `adjustment` is `none`, `lower`, `upper` or
  `both`: `lower` means an endpoint was raised, `upper` means one was reduced, and
  `both` means endpoints moved in both directions. `selected_edges`, `covered_edges`,
  `tolerance_mm`, and `probes` report coverage and search. After an interrupted
  refinement, the tolerance is conservative rather than a converged resolution.
  A successful fillet reports this even when unchanged.
- Corrected literal parameters are written in the accepted revision; expressions stay
  authored. When an expression needs correction, the feature tree can additionally carry
  `data.fillet_effective` as derived requested/effective/adjustment state, not authored
  input. Read the effective value before relying on the final geometry.
  The accepting edit's status retains the original requested values; a later rebuild
  reports its current authored inputs (corrected literals or re-evaluated expressions).

Invalid or dangling selectors, zero matches, nonfinite radii, unsupported topology,
kernel crashes and a search that cannot produce a valid all-edge result are explicit
failures. A budget stop before finding a valid candidate is also a failure, not proof
that a radius is too large; its message gives the failure at the requested radius. The
search has finite tolerance and probes and may not find every feasible solution. If a
part batch is rejected, its feature rows describe the rejected candidate, not the
unchanged live geometry.

READING A FAILURE: build failures name the failing contour by selected-edge order and a
point ("the blend along selected edge 2 near (x, y, z) failed"),
or a corner that could not be closed; invalid results and self-intersections name where.
When the search reports the same location at the requested and the smallest radius, the
cause does not depend on size: a smaller radius will not fix it. Inspect what meets the
selected edge there (query_geometry near that point): typically an earlier fillet or round
the new blend must run into, or a sliver face/step narrower than the radius. Fix the
construction instead: put the fillet before the round it collides with (the add_feature
op's "before":"fN"), round the meeting edges together in one feature, or remove the
sliver at its source.

Common mistakes:
1. Put the query under `refs.edges` (an OBJECT with "of"), not at the feature top level.
2. Do not assume the requested radius survived correction; read the effective result.
3. To discover available edges, call `query_geometry` with {"of":"edge",...} first.

The final solid must pass native B-rep, zero-area-face and self-intersection checks
before it replaces the preceding body. A kernel build alone is not success.
An invalid result rejects the edit atomically with its native reason; inspect the
selected edges and adjacent material before changing the radius or construction.
Do not repeat the mutation after a result-reporting exception without reading its
recorded reply and current tree.
