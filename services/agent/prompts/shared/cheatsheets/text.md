TEXT — a ONE-OP emboss/engrave of a string onto a PLANAR face. This is how you stamp a
part number, a logo line, a "THIS SIDE UP", a serial, or any lettering onto a model. You
do NOT build glyph outlines or booleans yourself — the `text` feature renders real letters
and cuts/raises them, with robustness built in.

REQUIRED:
  text             — the string to mark (e.g. "PN-42", "Chudware", "v1.0"). Aliases: the
                     compiler also accepts "string" or "value".
  refs.target_face — the PLANAR face to mark. Address it in plain words, e.g.
                     {"of":"face","side":"top"} or {"of":"face","side":"front"}, or the
                     largest flat face: {"of":"face","side":"top"} etc. Must be FLAT — a
                     curved/cylindrical/spherical face errors clearly (mark a flat boss
                     or a flat you add first).

OPTIONAL params:
  height   — cap/letter height in mm (default 5). This is the tall-letter height.
  depth    — engrave depth or emboss height in mm (default 0.5). Keep it modest
             (0.3–2 mm) relative to the part wall; an engrave deeper than the wall, or a
             tiny height with a big depth, can fail the boolean (it then falls back, see
             ROBUSTNESS).
  mode     — "engrave" (DEFAULT — a recessed mark, CUTS material in) or "emboss" (a raised
             mark, ADDS material out). Aliases: cut/recess/sunk → engrave; raised/raise/
             boss/proud → emboss.
  font     — a friendly name ("rubik"/"sans" default, "mono"/"jetbrains", "bold",
             "medium") or an absolute .ttf path. Defaults to the bundled Rubik-Regular.
  halign   — "left" / "center" (default) / "right" — horizontal placement about the face
             centre (offset point).
  valign   — "baseline" / "center" (default) / "top" — vertical placement.
  offset   — [du, dv] in mm: shift the text in-plane from the face centre (du along the
             reading direction, dv up). Use it to place several labels on one face.

WHAT IT DOES: renders the string's glyph outlines to real B-rep faces (FreeType via the
bundled fonts), lays them flat on the target face (centred by default), extrudes them
along the face normal by `depth`, and BOOLEANS them onto the solid — CUT inward for an
engrave (recessed), FUSED outward for an emboss (proud). The result is a normal editable
solid you can measure, render, and export.

ROBUSTNESS (font rendering + booleans are fragile): the feature BRepCheck-validates the
result and confirms the volume actually changed in the right direction. If ANYTHING fails
(no font found, a glyph the font lacks, a degenerate boolean, an OCCT throw), it DEGRADES
GRACEFULLY: the body is left UNMARKED (the clean part) and a warning is reported — the
part is NEVER broken. If you see "text: … left the part unmarked", reduce `depth`, raise
`height`, pick a clearly-flat face, or simplify the string.

TYPICAL USAGE — engrave a part number on the top of a block:
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate",
  "params":{"length":{"value":60,"expr":null},"width":{"value":40,"expr":null},
            "height":{"value":10,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"text","name":"PartNo",
  "params":{"text":"PN-42","height":{"value":8,"expr":null},
            "depth":{"value":1.0,"expr":null},"mode":"engrave"},
  "refs":{"target_face":{"of":"face","side":"top"}}}}
]

Emboss a raised logo on a face, shifted up-left with a second label below:
[
 {"op":"add_feature","feature":{"type":"text","name":"Logo",
  "params":{"text":"Chudware","height":{"value":6,"expr":null},
            "depth":{"value":0.8,"expr":null},"mode":"emboss",
            "offset":[0,8]},
  "refs":{"target_face":{"of":"face","side":"front"}}}},
 {"op":"add_feature","feature":{"type":"text","name":"Rev",
  "params":{"text":"REV B","height":{"value":4,"expr":null},
            "depth":{"value":0.6,"expr":null},"mode":"engrave","offset":[0,-6]},
  "refs":{"target_face":{"of":"face","side":"front"}}}}
]

COMMON MISTAKES:
1. target_face must be PLANAR (flat). A cylindrical/curved face errors — add a flat boss
   or pad first, or choose a real flat face ({"of":"face","side":"top"}).
2. Engrave too deep (deeper than the wall) or height too small for the depth → the cut may
   fall back to "unmarked". Use depth ≈ 0.3–2 mm with a sensible height.
3. mode defaults to "engrave" (recessed). Pass mode:"emboss" for raised lettering.
4. A failed mark is NOT an error — it warns and leaves the clean part. That is by design.
