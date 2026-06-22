PATTERNS - repeat earlier feature(s). linear_pattern (grid along directions) or circular_pattern (around an axis). The patterned features must come BEFORE this op.

linear_pattern params: `count1` (int), `spacing1` (dim), `count2` (int, default 1), `spacing2` (dim). refs.dir1 = axis datum, refs.dir2? = axis datum, refs.features = [feature id, ...].
circular_pattern params: `count` (int), `angle` (dim, total, default 360), `equal_spacing` (bool). refs.axis = axis datum, refs.features = [feature id, ...].
A datum axis selector is {"kind":"datum","feature":"axis.x"|"axis.y"|"axis.z"} (NOT "ref"). The seed in `features` must be a sketch-based feature with geometry to copy (an extrude/cut/hole/revolve) - a bare primitive box/cylinder is NOT patternable.

Example A - 2x2 grid of hole feature f3, 30mm x 20mm pitch along X and Y datum axes:
[
 {"op":"add_feature","feature":{"type":"linear_pattern","name":"Grid","params":{"count1":2,"spacing1":{"value":30,"expr":null},"count2":2,"spacing2":{"value":20,"expr":null}},"refs":{"dir1":{"kind":"datum","feature":"axis.x"},"dir2":{"kind":"datum","feature":"axis.y"},"features":["f3"]}}}
]

Example B - 6 copies of extruded boss f2 evenly around the Z axis:
[
 {"op":"add_feature","feature":{"type":"circular_pattern","name":"Ring","params":{"count":6,"angle":{"value":360,"expr":null},"equal_spacing":true},"refs":{"axis":{"kind":"datum","feature":"axis.z"},"features":["f2"]}}}
]

3 common mistakes:
1. `features` lists the feature IDs to repeat (extrude/cut/hole/revolve that exist earlier) - NOT a primitive, and not new geometry.
2. `count1`/`count` are plain INTEGERS; spacings/angle are dim objects.
3. A pattern transforms the seed feature's tool body - a CUT seed repeats the cut, a BOSS seed repeats the boss.
