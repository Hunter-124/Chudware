## Mechanism proof (kinematics)

Check the actual requested assembly/revision and each joint's range. For every
computed `trajectory` frame inspect `converged`, `max_residual`, `worst_mate`,
`tolerance`, `dof`, `residual_dof`, `pose_check` and `bodies` (component/name/world
transform). Inspect `collision_detail` and `collisions`: stable `component_a` /
`component_b`, `overlap_volume_mm3` for overlaps, or `distance` (mm) and warning for
below-minimum-clearance pairs. `collisions` also includes clearance hits, with
zero overlap volume; zero volume does not mean clear.

Frame `interference` is true for known hits, false only for a complete clear check,
and null when unchecked or incomplete without a known hit. Read
`interference_complete`, `pairs_checked`, `pairs_reused`, `pair_failures` and
`geometry_failures` (check fields exist before using them on unchecked frames).
Reused pairs retain exact results for unchanged relative poses; they are not skipped
evidence. Known hits survive incomplete checks. A sampled-clear claim requires
`check_interference=True`, top-level `interference.complete:true`,
`frames_checked == steps`, all computed frames converged/pose-valid and no hits.
Failed poses, missing geometry or pair failures are incomplete, never clear.
`loop=True` appends a playback copy, not another computed check. Report ranges,
sample count, clearance threshold and all findings; do not report a measured global
minimum distance from a sweep that only checked overlaps or threshold violations.

- **Envelope:** `sweep_envelope(mate_id, start, stop, steps, components)` bounds each
  component's 8 transformed bbox corners at sampled poses, not a measured clearance.
  `fuse=True` unions real placed solids (one `transform` copy per step fused into
  `<component>_sweep`) and returns volume/bbox when a bbox over-reports. Both are
  sampled envelopes, not a continuous swept-volume proof.
- **No dimensional mate to drive:** a steering assembly poses its uprights with
  concentric/coincident mates about measured kingpin axes and owns no angle/distance
  mate, so `simulate_motion` has nothing to sweep. `sweep_envelope` and
  `steering_turning_radius` take a `steer` spec instead —
  `steer={"components":["upright_FL","upright_FR"],
  "axis":{"point":[x,y,z],"dir":[x,y,z]}, "angle_deg":30}` (or `"mate_id":"<the
  concentric kingpin mate>"` to reuse the axis it already resolves, or
  `"angles_deg":[inner,outer]` for a true Ackermann pair). The named components are
  rotated rigidly about that axis on a DETACHED clone and the mate network is re-solved
  around them, so the live document keeps its revision and its mate-owned poses.
  Keep a straight rear axle non-steering for the vehicle workflow; `rear_steer`
  supports explicitly requested 4WS studies, not an assumed vehicle configuration.
- **Turning radius:** `steering_turning_radius(steer=… | steer_mate_id, …)` intersects
  the POSED wheel axle lines — a rolling wheel turns about a point on its own axle line —
  and reports `turning_radius_m`/`turning_radius_ft` plus per-wheel `steer_angle_deg`
  and radius, inner/outer roles, and `turn_center_station_mm` (0 on the rear axle for
  2WS, mid-wheelbase for equal-and-opposite 4WS). `axle_line_residual_mm` is the
  Ackermann inconsistency: ~0 for a correct linkage, `(track/2)·sin(steer)` for parallel
  steer. Read its `assumptions` and `derived_from`: an unstated wheelbase/track is
  DERIVED from the solved axle centres, so pass `wheelbase_mm`/`track_mm` when the
  geometry is not a two-axle vehicle. Quote the turning radius with
  `outer_swept_radius_mm` — a corridor claim is about the swept corner, not the
  centreline — and `check_interference=True` for the steering lock the geometry imposes
  (`first_interference_deg`).
- **Load case selection:** the worst joint reaction is not always the worst stress, so
  `motion_fea_handoff_sweep(component, fixtures, positions|start/stop/steps, ...)`
  batches the single-frame handoff across positions and names the governing one. Each
  row is a real static solve; a row with `ok:false` proves nothing about that position.
