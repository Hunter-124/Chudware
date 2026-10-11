SHELL - hollow a solid to a wall thickness, optionally opening faces.

Op shape: one `add_feature` of type `shell`. params: `thickness` (dim object), `outward` (bool, default false = hollow inward). refs.remove names the faces to OPEN (omit for a closed hollow shell).

BEST WAY to name the open face(s) - a geometry QUERY under refs.remove:
- {"of":"face","side":"+z"}      open the TOP face (the usual "open box")   ("-z"=bottom, etc.)
- {"of":"face","surf":"plane","side":"+z"}  same, restricted to planar faces
- {"of":"face","near":[x,y,z],"count":1}    open the face nearest a point
- [{"of":"face","side":"top"},{"of":"face","side":"bottom"}]  a LIST opens several faces (top AND bottom = a tube)

Example A - hollow the current body to 2mm walls, open the top:
[
 {"op":"add_feature","feature":{"type":"shell","name":"Shell1","params":{"thickness":{"value":2,"expr":null},"outward":false},"refs":{"remove":{"of":"face","side":"+z"}}}}
]

Example B - closed hollow shell (no opening), 1.5mm walls:
[
 {"op":"add_feature","feature":{"type":"shell","name":"Hollow","params":{"thickness":{"value":1.5,"expr":null},"outward":false}}}
]

3 common mistakes:
1. Shell BEFORE adding fillets/holes usually succeeds where after fails - shell early.
2. Thickness must be smaller than half the smallest dimension, or the build fails.
3. refs.remove takes one query OBJECT or a LIST of queries/selectors; omit refs.remove entirely for a closed shell.

CURVED / FREEFORM bodies: a revolved or swept body usually shells fine (every result is
validated). A rectangle-to-circle LOFT (a duct/transition, collars included) shells too when
the opened faces are its flat ends - open top and/or bottom with the LIST form and shell
INWARD: the kernel's offset refuses its corner seams (crease into tangency), so the shell
hollows it section by section instead and checks the wall. What still errors ("shell is not
supported on this lofted ... body - ...") is such a body opened on a non-end face, shelled
OUTWARD, or with a step/ledge across the axis. For those build the wall INTO the profile
instead: sketch the wall cross-section as a thin closed loop (an inner curve + an outer curve
offset from it) and LOFT or REVOLVE that, or loft a second set of sections inset by the wall
and cut it. See revolve.md "hollow body" for the ring-profile recipe.
