PATTERNS - repeat earlier feature(s). linear_pattern (grid along directions) or circular_pattern (around an axis). The patterned features must come BEFORE this op.

linear_pattern params: `count1` (int), `spacing1` (dim), `count2` (int, default 1), `spacing2` (dim). refs.dir1 = axis selector, refs.dir2? = axis selector, refs.features = [feature id, ...].
circular_pattern params: `count` (int), `angle` (dim, total, default 360), `equal_spacing` (bool). refs.axis = axis selector, refs.features = [feature id, ...].

Example A - 2x2 grid of hole feature f3, 30mm x 20mm pitch along X and Y datum axes:
[
 {"op":"add_feature","feature":{"type":"linear_pattern","name":"Grid","params":{"count1":2,"spacing1":{"value":30,"expr":null},"count2":2,"spacing2":{"value":20,"expr":null}},"refs":{"dir1":{"kind":"datum","ref":"X"},"dir2":{"kind":"datum","ref":"Y"},"features":["f3"]}}}
]

Example B - 6 copies of boss f2 evenly around the Z axis:
[
 {"op":"add_feature","feature":{"type":"circular_pattern","name":"Ring","params":{"count":6,"angle":{"value":360,"expr":null},"equal_spacing":true},"refs":{"axis":{"kind":"datum","ref":"Z"},"features":["f2"]}}}
]

3 common mistakes:
1. `features` lists the feature IDs to repeat (they must already exist earlier in the tree), not new geometry.
2. `count1`/`count` are plain INTEGERS; spacings/angle are dim objects.
3. A pattern transforms the seed feature's tool body - a CUT seed repeats the cut, a BOSS seed repeats the boss.
