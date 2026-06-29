THREAD — a ONE-OP helical thread cut on a cylindrical face. This is the easy way to
thread a shaft or a bore: you do NOT build the helix + profile + sweep yourself (that
manual chain is in helix.md). The `thread` feature does it all, with robustness built in.

REQUIRED:
  length      — the thread RUN in mm (how far the thread extends along the axis). Threads
                start from the lower end of the target face and run `length` up the axis.
  refs.target_face — the CYLINDRICAL face to thread. Address it in plain words:
                {"of":"face","surf":"cylindrical"} (the round face). For external threads
                this is the shaft's outer cylinder; for internal, the hole's bore.

SIZE (give ONE of these two):
  size        — an ISO metric designation, e.g. "M8". Looks up the coarse pitch + major Ø
                from the standard table (M1.6, M2, M2.5, M3, M4, M5, M6, M8, M10, M12, M14,
                M16, M18, M20, M22, M24). This is the normal way.
  major_dia + pitch — a CUSTOM thread: bare major diameter (mm) + pitch (mm per turn).

OPTIONAL params:
  style       — "external" (default — a shaft, cuts a groove inward) or
                "internal" (a bore/tapped hole, cuts outward into the wall).
  starts      — number of thread starts (default 1). Multi-start multiplies the lead.
  handedness  — "right" (default) or "left".
  turns       — hard cap on swept turns (robustness; defaults to length/lead, max 30).
  mode        — "cut" (default — REAL geometry, a helical V-groove) or
                "cosmetic" (a lightweight representation; NO geometry change, always
                succeeds with a warning. Use it for very fine/long threads, or when you
                only need the callout, not the cut. Drawing-callout integration is a
                follow-up — for now cosmetic just means "no geometry, don't fail").

WHAT IT DOES (mode "cut"): looks up pitch + major Ø, derives the ISO pitch diameter
(minor ≈ major − 1.0825·pitch), builds a helix on the target cylinder's axis at the pitch
diameter, sweeps a SIMPLIFIED rounded ISO-60° thread profile along it, and BOOLEAN-CUTS
the swept coil out of the body. External = a V-groove cut into the shaft; internal = cut
into the bore wall.

ROBUSTNESS (important — helical booleans are fragile in OCCT): the feature caps the turn
count, uses a rounded (not razor-sharp) profile, and BRepCheck-validates the cut. If the
real cut fails for ANY reason (OCCT throw, empty/invalid result), it DEGRADES GRACEFULLY:
the body is left UNTHREADED (the plain cylinder) and a warning is reported — the part is
NEVER broken. If you see "thread: real geometry failed … left the body unthreaded",
reduce `length` or use a coarser pitch, or switch to mode:"cosmetic".

TYPICAL USAGE — an M8 external thread on a Ø8 × 20 shaft:
1. Build the shaft: cylinder radius 4, height 20 (a sketch+revolve or extrude also works).
2. Thread it: type "thread", size "M8", length 18, target_face = the cylindrical face.

Full op JSON (external M8):
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Shaft",
  "params":{"radius":{"value":4,"expr":null},"height":{"value":20,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"thread","name":"M8Thread",
  "params":{"size":"M8","length":{"value":18,"expr":null},"style":"external","mode":"cut"},
  "refs":{"target_face":{"of":"face","surf":"cylindrical"}}}}
]

Internal (tapped hole) — first bore a hole, then thread the bore wall:
[
 ... drill a Ø6.8 hole (the M8 tapping drill) into the part ...
 {"op":"add_feature","feature":{"type":"thread","name":"M8Tap",
  "params":{"size":"M8","length":{"value":12,"expr":null},"style":"internal","mode":"cut"},
  "refs":{"target_face":{"of":"face","surf":"cylindrical"}}}}
]
(For internal threads address the BORE's cylindrical face — if the part has several round
faces, narrow the query, e.g. add "inner" or pick the smaller-radius round face.)

COMMON MISTAKES:
1. target_face must be CYLINDRICAL. A flat end-cap or a box face errors clearly. Use
   {"of":"face","surf":"cylindrical"}.
2. `length` is the thread RUN, not the part length — it can be shorter than the shaft.
3. For a custom thread give BOTH major_dia AND pitch; for a standard one give just `size`.
4. A failed cut is NOT an error — it warns and leaves the plain cylinder. That is by
   design. To force success without geometry, use mode:"cosmetic".

Confirmed against: validate.cpp FEATURE_TYPES `thread` entry, document.cpp thread handler,
geom/thread.cpp (cut_thread), and test_document.cpp case 12 (M8 external + cosmetic).
