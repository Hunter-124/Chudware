# Modeling-Op Combination Battery — Chudware Feature Engine

**Generated:** 2026-07-31T13:45:25Z  
**Script:** `scripts/modeling_battery.py` (T-BL19)  
**Engine:** `/home/hunter/Repositories/CAD/core/build-occt-linux/bin/libchudware.so`  
**Method:** each case drives the real C++ feature engine via core_bridge (doc_new + doc_apply_ops) and asserts on live geometry (rebuild status, manifoldness, volume sign, body count).  

## Summary

| Metric | Count |
|--------|-------|
| Cases total | 52 |
| Executed | 52 |
| PASS | 52 |
| FAIL | 0 |
| XFAIL (known bug) | 0 |
| ERROR | 0 |
| Elapsed | 3.9s |

## Coverage

Registered modeling op-types (core/doc/validate.cpp): **49**. Executed by battery: **31** (**63.3%** of registered).

**NOT covered (with reason):**

- `script` — raw script escape-hatch feature — arbitrary code, intentionally out of scope.
- `sm_flat_pattern` — unfold step; only meaningful after edge_flange bends land (v2).
- `sm_hem` — as sm_edge_flange (resolved-edge slot).
- `sm_jog` — as sm_edge_flange (resolved-edge slot).
- `surf_boundary` — needs resolved boundary curves; v2.
- `surf_delete_face` — needs a resolved solid-face ref; v2.
- `surf_extrude` — surfacing covered by surf_planar+surf_thicken; other surf_* builders share the thicken/knit sink — representative coverage only.
- `surf_fill` — needs >=2 resolved boundary edges; v2.
- `surf_knit` — needs >=2 pre-built surface bodies; v2.
- `surf_loft` — see surf_extrude.
- `surf_offset` — needs a resolved solid-face ref; v2.
- `surf_replace_face` — needs a resolved face + spanning surface; v2.
- `surf_revolve` — see surf_extrude.
- `surf_ruled` — needs two resolved edge refs; v2.
- `surf_sweep` — see surf_extrude.
- `surf_trim` — needs a resolved tool face; v2.
- `weld_end_cap` — secondary weldment op on an existing member end; v2.
- `weld_gusset` — secondary weldment op between two members; v2.

## Cases

| id | category | kind | verdict | detail |
|----|----------|------|---------|--------|
| box | primitive | single | PASS | all expectations met |
| cylinder | primitive | single | PASS | all expectations met |
| sphere | primitive | single | PASS | all expectations met |
| cone | primitive | single | PASS | all expectations met |
| torus | primitive | single | PASS | all expectations met |
| extrude_boss | sketch2d | single | PASS | all expectations met |
| extrude_cut | sketch2d | single | PASS | all expectations met |
| revolve | sketch2d | single | PASS | all expectations met |
| loft | sketch2d | single | PASS | all expectations met |
| sweep | sketch2d | single | PASS | all expectations met |
| helix_spring | curve | single | PASS | all expectations met |
| fillet | dress | single | PASS | all expectations met |
| chamfer | dress | single | PASS | all expectations met |
| shell | dress | single | PASS | all expectations met |
| draft | dress | single | PASS | all expectations met |
| rib | structural | single | PASS | all expectations met |
| split | structural | single | PASS | all expectations met |
| boolean_fuse | structural | single | PASS | all expectations met |
| boolean_cut | structural | single | PASS | all expectations met |
| boolean_fuse_touching_face | structural | single | PASS | all expectations met |
| hole_simple | intent | single | PASS | all expectations met |
| hole_cbore | intent | single | PASS | all expectations met |
| bolt_circle | intent | single | PASS | all expectations met |
| linear_pattern | pattern | single | PASS | all expectations met |
| circular_pattern | pattern | single | PASS | all expectations met |
| mirror | pattern | single | PASS | all expectations met |
| transform_move | transform | single | PASS | all expectations met |
| transform_copy | transform | single | PASS | all expectations met |
| sm_base_flange | sheetmetal | single | PASS | all expectations met |
| sm_edge_flange | sheetmetal | sequence | PASS | all expectations met |
| weld_member | weldment | single | PASS | all expectations met |
| surf_thicken | surfacing | single | PASS | all expectations met |
| thread | thread | single | PASS | all expectations met |
| text_engrave | text | single | PASS | all expectations met |
| fillet_then_shell | sequence | sequence | PASS | all expectations met |
| shell_then_fillet | sequence | sequence | PASS | all expectations met |
| draft_then_shell | sequence | sequence | PASS | all expectations met |
| hole_then_chamfer | sequence | sequence | PASS | all expectations met |
| pattern_of_hole | sequence | sequence | PASS | all expectations met |
| pattern_of_cut | sequence | sequence | PASS | all expectations met |
| mirror_of_cut | sequence | sequence | PASS | all expectations met |
| boolean_then_fillet | sequence | sequence | PASS | all expectations met |
| revolve_then_shell | sequence | sequence | PASS | all expectations met |
| bolt_circle_then_shell | sequence | sequence | PASS | all expectations met |
| recipe_mount_bracket | recipe | recipe | PASS | all expectations met |
| recipe_pulley | recipe | recipe | PASS | all expectations met |
| recipe_enclosure | recipe | recipe | PASS | all expectations met |
| nc_cut_misses_body | negative | negative | PASS | all expectations met |
| nc_open_surface_nonmanifold | negative | negative | PASS | all expectations met |
| nc_pattern_of_explicit_boolean | negative | negative | PASS | all expectations met |
| nc_disjoint_fuse | negative | negative | PASS | all expectations met |
| nc_disjoint_cut | negative | negative | PASS | all expectations met |

## No-all-skip contract

This battery FAILS (non-zero exit) if the engine is unavailable or zero cases execute — an all-skip run is a failure, not a pass (fleet-recovery-patterns memory). Negative-control cases prove each check can actually fail.
