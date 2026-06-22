SWEEP a closed profile sketch along a path sketch (pipe, rail, handle, gasket).

Op shape: one `add_feature` of type `sweep`. params: `mode` ("boss"). refs.profile = [a region
selector for the CLOSED cross-section sketch]. refs.path = a selector naming the PATH sketch (an
OPEN line/arc/spline trace). Both sketches must already exist (earlier steps); name them by feature id.

Selectors: region = {"kind":"region","sketch":"<id>","centroid":[x,y],"hint_index":0}. The path ref
takes the same selector shape naming the path sketch.

Keep the profile sketch on a plane whose normal points ALONG the start of the path (e.g. profile on
"plane.right", path on "plane.top") so the section sweeps perpendicular to the path.

Example - sweep a circle profile (f1, on plane.right) along an L-path (f2, on plane.top):
[
 {"op":"add_feature","feature":{"type":"sweep","name":"Pipe","params":{"mode":"boss"},"refs":{"profile":[{"kind":"region","sketch":"f1","centroid":[0,0],"hint_index":0}],"path":{"kind":"region","sketch":"f2","centroid":[20,0],"hint_index":0}}}}
]

3 common mistakes:
1. The PROFILE must be a closed loop; the PATH is an open trace (do not close it).
2. profile is an ARRAY of selectors; path is a SINGLE selector.
3. Orient the profile plane across the path's start direction, or the sweep self-intersects.
