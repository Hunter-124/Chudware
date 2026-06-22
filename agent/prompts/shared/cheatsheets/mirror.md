MIRROR a feature across a plane (make one side, then reflect it).

Op shape: one `add_feature` of type `mirror`. refs.plane = a datum plane
{"kind":"datum","feature":"plane.front"|"plane.top"|"plane.right"} (or a planar model face).
refs.features = the feature ids to reflect (e.g. ["f3"]).

The mirrored feature(s) MUST be earlier sketch-based features with geometry — an extrude boss/cut, a
hole, a revolve. A bare primitive box/cylinder is NOT mirrorable; build the thing to mirror with
sketch+extrude first.

Example - mirror an extruded boss f3 across the right datum plane:
[
 {"op":"add_feature","feature":{"type":"mirror","name":"Mirror1","params":{},"refs":{"plane":{"kind":"datum","feature":"plane.right"},"features":["f3"]}}}
]

3 common mistakes:
1. `features` lists EARLIER extrude/cut/hole feature ids, not a primitive and not new geometry.
2. The plane is a datum {"kind":"datum","feature":"plane.right"} (NOT "ref"), or a planar face selector.
3. A mirrored CUT reflects the cut; a mirrored BOSS reflects the boss — mirror the feature, not the body.
