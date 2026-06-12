REVOLVE a sketch region about an axis into a solid of revolution.

Op shape: one `add_feature` of type `revolve`. params: `mode` ("boss"|"cut"), `angle` (dim, default 360). refs.profile = [region selector], refs.axis = an axis selector (a sketch line edge or a datum axis). The axis must NOT cross the profile interior.

A region selector: {"kind":"region","sketch":"<sketch id>","centroid":[x,y],"hint_index":0}.

Example A - revolve sketch f1's region a full 360 about a datum axis edge:
[
 {"op":"add_feature","feature":{"type":"revolve","name":"Revolve1","params":{"mode":"boss","angle":{"value":360,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"f1","centroid":[10,0],"hint_index":0}],"axis":{"kind":"edge","surf":"line","centroid":[0,0,0],"axis":[0,0,1],"area_or_len":20,"owner_feature":"f1","hint_index":0}}}}
]

Example B - revolve 180 degrees (half):
[
 {"op":"add_feature","feature":{"type":"revolve","name":"Half","params":{"mode":"boss","angle":{"value":180,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"f1","centroid":[10,0],"hint_index":0}],"axis":{"kind":"edge","surf":"line","centroid":[0,0,0],"axis":[0,0,1],"area_or_len":20,"owner_feature":"f1","hint_index":0}}}}
]

3 common mistakes:
1. The axis must lie OUTSIDE the profile region (offset the profile from the axis), or the revolve fails.
2. `angle` is a dim object; 360 makes a full body, smaller angles make a wedge.
3. profile is an ARRAY of selectors; axis is a SINGLE selector, not an array.
