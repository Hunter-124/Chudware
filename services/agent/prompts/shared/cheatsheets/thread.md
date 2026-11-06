THREAD — one feature for a helical thread on a cylindrical face. Use refs.target_face
for the shaft's outer cylinder or the bore wall, e.g. {"of":"face","surf":"cylindrical"}.
Inspect matches first and narrow the selector when there are multiple cylinders.

SIZE: give either params.size (ISO coarse e.g. "M8", metric fine e.g. "M8x1", or a
supported Unified designation e.g. "1/4-20") OR both params.major_dia and params.pitch
in mm. A string size takes precedence over custom dimensions; omit size for custom.
In the GUI, Custom size chooses which fields are sent; it is not a substitute for the
actual dimensions.

RUN: omit length (or use a nonpositive value) to span from the start to the far end of
the selected face. A positive length specifies the axial run in mm. By default the start
is the lower face end; refs.from can select a start vertex, edge, face or datum, and
params.flip:true reverses the run. The GUI's Full length control omits length when
checked; when authoring ops, omit length rather than relying on full_length to override
an explicit length. The real operation bounds the run to the body's axial extent.

OPTIONAL params:
  style       "external" (default, shaft) or "internal" (bore).
  starts      number of starts (default 1); lead = pitch × starts.
  handedness  "right" (default) or "left".
  form        "v" (default 60°), "acme", "trap", or "square".
  mode        "cut" (default, real helical groove); "raised" (real fused ridge,
              selected cylinder is the root diameter); or explicit "cosmetic"
              (unchanged geometry with a warning, not a physical thread).
  turns       optional positive cap on swept turns, default 24 when omitted or nonpositive.
              The bounded run requires run/lead + 2 turns, including end extensions;
              if that exceeds the cap, the operation is rejected, not shortened. Increase
              the cap if appropriate rather than changing the requested thread.

Real cut/raised geometry is constructed with a helical sweep and boolean. A failed real
construction, invalid result, or insufficient turn cap is a visible rejected operation; it
is not a successful plain-body thread. Do not silently change the requested length, pitch,
or mode to cosmetic. Cosmetic is an intentional no-geometry choice, not a recovery from
failure. params.preview:true (or draft:true) produces a provisional turned-band proxy,
not a helical thread; omit it for the final operation and check the completed result.

EXAMPLE — M8 external thread on a Ø8 × 20 shaft, 18 mm run:
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Shaft",
  "params":{"radius":{"value":4,"expr":null},"height":{"value":20,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"thread","name":"M8Thread",
  "params":{"size":"M8","length":{"value":18,"expr":null},"style":"external","mode":"cut"},
  "refs":{"target_face":{"of":"face","surf":"cylindrical"}}}}
]
For a tapped bore, first make the appropriate tap-drill hole, select its cylindrical
wall, and use style:"internal". For a custom thread omit size and supply positive
major_dia and pitch. A planar target is not a valid real-thread face.
