LOFT a solid through 2 or more section sketches (each a closed profile on its own plane).

Op shape: one `add_feature` of type `loft`. params: `mode` ("boss"). refs.sections = an ARRAY of
region selectors, one per section sketch, IN ORDER along the loft. Each section sketch must already
exist (earlier steps) — name it by its feature id (e.g. "f1", "f2"), shown in the doc summary.

A region selector: {"kind":"region","sketch":"<sketch id>","centroid":[x,y],"hint_index":0}.

The sections are usually on parallel planes at different heights: make the lower sketch on
"plane.top", and the upper sketch on "plane.top" with an `offset` param (the height), e.g.
sketch params {"plane":"plane.top","offset":50}.

Example - loft a circle (bottom) up to a square (top, 50mm up). f1 = the circle sketch, f2 = the
square sketch:
[
 {"op":"add_feature","feature":{"type":"loft","name":"Blend","params":{"mode":"boss"},"refs":{"sections":[{"kind":"region","sketch":"f1","centroid":[0,0],"hint_index":0},{"kind":"region","sketch":"f2","centroid":[0,0],"hint_index":0}]}}}
]

3 common mistakes:
1. `sections` needs >= 2 region selectors naming DIFFERENT, already-existing closed sketches.
2. Put the sections on planes at different heights (use the sketch `offset` param), or the loft is flat.
3. Each section must be a CLOSED profile (a closed loop of entities), not an open path.
