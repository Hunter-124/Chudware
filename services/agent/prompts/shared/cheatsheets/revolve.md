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
1. The axis must lie OUTSIDE the profile region (offset the profile from the axis), or the revolve fails. It must also lie IN the sketch's plane: revolving about a plane's own NORMAL sweeps nothing and is rejected as zero-volume. Sketch on `plane.top` for `axis.x`, `plane.right` for `axis.y`, `plane.front` for `axis.z`.
2. `angle` is a dim object; 360 makes a full body, smaller angles make a wedge.
3. profile is an ARRAY of selectors; axis is a SINGLE selector, not an array.

SOLID vs HOLLOW (vase / nozzle / tube / bottle):
- A profile that TOUCHES THE AXIS at both ends (points at radius 0) revolves into a CLOSED,
  SOLID body that comes to a pole at each end (like a sphere or a spinning top) - one continuous
  freeform face, NO flat end caps and NO internal cavity. That is why shell then "finds no flat
  faces" - there are none. Use this only when you actually want a solid blob.
- For a HOLLOW revolved body, sketch the WALL cross-section as a closed loop that stays OFF the
  axis: an inner-wall curve + an outer-wall curve (a few mm apart) closed by a short line at each
  end, with the whole loop offset from the axis (min radius > 0). Revolve that 360 deg and you get
  a true tube/nozzle open at both ends - no shell needed. (Reserve `shell` for prismatic solids;
  it does NOT work on revolved/lofted bodies - build the wall into the profile instead.)
