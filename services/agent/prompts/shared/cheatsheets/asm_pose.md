ASSEMBLY COMPONENT POSE - asm_add_component(at, rotate, angle) and the asm_edit_component op.

A component pose is ONE rigid transform of the component's part coordinates:
  world = R * part + at
  rotate  [ax,ay,az] rotation axis through the PART'S OWN ORIGIN (normalised for you; a
          zero-length axis is refused).
  angle   DEGREES about `rotate` (right-hand rule). angle needs rotate, rotate needs a
          nonzero angle; either alone is refused.
  at      [x,y,z] mm translation, applied AFTER the rotation.
So rotate/angle first, then at. Stored as a row-major 4x4 `transform` (16 numbers).

INSERT: asm_add_component(path="parts/wheel.step+", name="wheel_L", at=[0,250,0],
        rotate=[1,0,0], angle=90) -> returns the component id ("k2").
RE-POSE an existing component with add_features:
  {"op":"asm_edit_component","id":"k2","at":[0,250,40],"rotate":[0,0,1],"angle":15}
  add_features converts at/rotate/angle into `transform`; an explicit 16-value "transform"
  beside them wins and the at/rotate keys are dropped (reported in the reply notes).
  The new pose REPLACES the old one (absolute, not incremental): repeat the rotation when
  you only change `at`. For a relative world-space move use a transform feature with
  body:"k2" (cad_guide("transform")); mate-owned poses refuse it.
Other asm_edit_component keys: name (rename a component - the feature `rename` op does not
apply to components), fixed, suppressed, hidden, source. "id" is required.

A raw pose is not a fit: prefer asm_mate (cad_guide("assembly")), or derive at/rotate from
measured references (fit_cylinder_axis, measure_between). Then verify with
check_interference(members=[...]) and measure_between; save_part/export_part report
placement_unverified for transform-placed components nothing has re-checked.
