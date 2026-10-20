# Overview

```text
Chudware MODELING GUIDE (C++ parametric engine — same as the desktop app).

WORKFLOW
  1. new_part(name)                         start an editable part document.
  2. add_features([ ...ops... ])            build with standard features (vocabulary below).
                                            Returns per-feature status + body count + volume
                                            so you can see a dropped cut or a stray body.
                                            A `sketch` feature should carry geometric RELATIONS
                                            (coincident/horizontal/parallel/tangent/...) + DRIVING
                                            DIMENSIONS, not loose geometry; check_sketch("s1")
                                            reports dof + conflicts so you can reach fully-defined.
     preflight_ops(ops)                     OPTIONAL but recommended before big edits: dry-runs
                                            the planned ops on a shadow clone — checks dangling
                                            refs, impossible fillets, hole-through-nothing, and
                                            downstream feature breaks — WITHOUT touching the live
                                            doc. Returns aicad.preflight/1 with advisory_items.
                                            A fail verdict is advisory (never blocks); add_features
                                            runs preflight automatically on non-trivial batches.
                                            A suppressed run (trivial batch, CHUDWARE_PREFLIGHT=0,
                                            or a preflight error) returns verdict "skipped" with
                                            the reason in skipped/skipped_detail.
                                            An invalid fillet/chamfer solid is rejected even if
                                            OCCT reports construction success. Atomic failure keeps
                                            the prior live tree/revision; regen_diagnosis uses the
                                            rejected candidate and native feature error. Provider
                                            unavailability does not erase that native evidence.
  3. render_part()                          SEE it — ONE call returns a 2x2 grid of the four
                                            most useful complementary shots (the planner
                                            measures ~200 cameras in the engine and picks the
                                            set that shows the most), faces SID-tagged, and a
                                            caption: what each tile shows, faces NO tile sees
                                            (by sid), tag legend. render_part(target="f2")
                                            frames the grid on that feature (painted, rest
                                            dimmed; a SECTION tile with ghosted outline when it
                                            is internal). render_part(view="right" |
                                            {"azimuth":30,"elevation":20} | "auto") = one
                                            single shot instead. render_views() = the planner's
                                            7-view set (views=[...] for a fixed drawing set);
                                            contact_sheet() = the 4-view grid or named views.
                                            inspect_feature(target): planner close-ups +
                                            whole-part context with dims and a describe_part
                                            excerpt; inspect_part(target) one framed close-up;
                                            look_around(target) N planner angles. Numbers
                                            first — do not render after every mutation.
  4. measure_part() / query_geometry(q)     check mass/bbox, or explore faces/edges.
     set_material("aluminum" | {color,...})  store a named engineering material + matching
                                            appearance (explicit colour dicts are visual-only).
  5. run_fea_static(...) / run_cfd_flow(...) prove it (see cad_guide("simulation")).
  6. save_part()                            save the NATIVE .step+ — the editable feature tree
                                            PLUS its simulation studies + any drawings, in one
                                            file the desktop app reopens fully editable. This is
                                            Chudware's native format; save to it to hand the user a
                                            part they can keep working on (not a flattened mesh).
  7. export_part(fmt) / make_drawing()      hand off a NON-native interchange/mesh file
                                            (STEP/IGES/BREP exact, STL/OBJ/3MF mesh) or a
                                            dimensioned 2D drawing.

Address geometry in PLAIN WORDS (no coordinates, no face numbers): "the top face", "all
edges", "the vertical edges", "the largest round face". The engine resolves the words to
topology. cad_guide("review") explains how to read the annotated render. The full op
vocabulary, with worked examples, follows.

DOC OWNERSHIP & SAVE DISCIPLINE
  Docs you mint (new_part/new_assembly/import) are AI-OWNED. The user sees them live in a
  mirror tab while you work; when you finish (detach/close/disconnect) the app AUTO-CLOSES
  that live view unless the user is actively viewing or has adopted it by editing. So the
  responsibility to preserve work is YOURS, not the user's:
    * save_part(path=...) every DELIVERABLE before you finish — this is the hand-off.
    * close_part(discard=True) for SCRATCH docs you don't want kept.
  An unsaved AI doc that's just abandoned is retired to a crash-recovery tier — a safety
  net, NOT a delivery mechanism, and never surfaced to the user as a warning. If the user
  edits your doc they ADOPT it (it becomes theirs); you keep co-editing the same live doc.
  Explicit save_part/export_part relative paths (e.g. "parts/redesign/base.step+") use
  the selected workspace: CHUDWARE_PROJECT_DIR, then CHUDWARE_FS_ROOT, independent of
  worker cwd or continuation. A selected nested workspace is not widened to a parent
  project/repository. Without a valid project root, pass an absolute path. Absolute
  and "~" destinations may be outside the workspace, but credential/key locations
  are refused after resolving symlinks. Pathless saves keep an existing native file
  in place, otherwise use CHUDWARE_PROJECT_SAVE_DIR (temp only with no project).
  Native STL export refuses collapsed facets or a source-solid mesh that is not closed
  and consistently oriented in the serialized output. It does not heal geometry or
  weld nearby points. Keep the native error and inspect the source/export conditions
  rather than deleting triangles or claiming an unchecked mesh is print-ready.

PRIMITIVE PLACEMENT & ORIENTATION (box / cylinder / sphere / cone / torus)
  A primitive is built CENTRED ON THE ORIGIN and then placed. These are the placement keys,
  and they live at FEATURE level — beside "type"/"name"/"params", NOT inside params:
    at         [x,y,z] absolute, OR a geometry query to sit on existing geometry:
               {"of":"face","side":"top"} centres the primitive on that face.
    at_offset  [dx,dy,dz] world-axis nudge applied ON TOP of `at` (boss raised 10 mm above
               the face it sits on: at_offset:[0,0,10]).
    axis       [x,y,z] or "+x"/"-x"/"+y"/"-y"/"+z"/"-z" — orients the primitive's local +Z.
               Default [0,0,1] (straight up) = the historical behaviour. This is how you get
               a SIDEWAYS cylinder/cone/box without falling back to sketch+extrude.
    centered   false = the natural base/corner sits at `at` (box corner, cylinder/cone base
               centre) instead of the centroid. Default true.
    mode       "boss" fuse into the running body (default) | "cut" subtract it.
    merge      false = start a NEW body instead of fusing into the current one.
  Transform order: build centred -> rotate local +Z onto `axis` about the origin -> apply the
  `centered:false` base offset ALONG THE ROTATED AXIS -> translate by `at` -> add `at_offset`.

  WRONG  {"type":"cylinder","name":"DrainSpout","params":{...},
          "placement":{"origin":[200,0,40],"direction":[1,0,0]}, "boolean":"union"}
  RIGHT  {"type":"cylinder","name":"DrainSpout","params":{...},
          "at":[200,0,40], "axis":[1,0,0], "centered":false, "mode":"boss"}
  A handful of dialect spellings (placement.origin/position/location/point, placement.
  direction/axis/normal/dir, feature-level position/location/origin/direction/dir/normal/
  orientation, boolean/operation "union"|"cut"|"new") are rewritten into the canonical keys
  above for you — but write the canonical keys. ANY OTHER unrecognised feature-level key is a
  HARD ERROR that names the offending key and lists the accepted ones. It is never silently
  dropped, so a placement typo now fails loudly instead of quietly building the primitive at
  the origin, buried inside the part ("boss added no material").

  ── WORKED EXAMPLE: bucket body + SIDEWAYS drain spout ────────────────────────────────

    new_part('mud_saver_bucket')
    add_features([
      {"op":"add_feature","feature":{"type":"cylinder","name":"Body",
         "params":{"radius":{"value":165,"expr":null},"height":{"value":300,"expr":null}}}},
      {"op":"add_feature","feature":{"type":"cylinder","name":"DrainSpout",
         "params":{"radius":{"value":25,"expr":null},"height":{"value":60,"expr":null}},
         "at":[150,0,-100], "axis":[1,0,0], "centered":false, "mode":"boss"}}])
    render_part()                 # see the spout stick out the side

  The spout's `at` must START INSIDE the wall (x=150 < the body's r=165) so the boss actually
  intersects the body — a boss that adds no material is REJECTED, not silently ignored.

GEOMETRY QUERY GRAMMAR (query_geometry, refs.*, at:{...}, FEA fixtures/loads, asm_mate)
  A query is a STRUCTURED OBJECT (or a bare side word like "top"/"+z"), never a sentence:
    of             "face" | "edge".  OPTIONAL — defaults to "face".
    side           top | bottom | left | right | front | back, or "+z"/"-x"/... — which way
                   it faces. With pick "largest"/"smallest": the largest face facing that way.
    surf           faces "flat" | "round" (any curved), or cylindrical/conical/spherical/
                   toroidal; edges "straight" | "round" (or exact line/circle).
    parallel       "z" | "x" | "y" | [x,y,z] — the direction the geometry runs along.
    perpendicular  same value forms — keep only geometry perpendicular to that direction.
                   NOTE a face's direction is its NORMAL, an edge's is the way it RUNS. So a
                   VERTICAL WALL FACE is perpendicular:"z" (its normal is horizontal) while a
                   VERTICAL EDGE is parallel:"z". "Vertical"/"horizontal" are not query words.
    pick           largest | smallest | longest | shortest | inner | outer — narrow a
                   multi-match to the one you mean (also "index":[i]).
    near           [x,y,z] (+ "count") — the face/edge nearest a point ON it (or at its
                   centroid), measured to the actual geometry: a point on a flange rim picks
                   the rim, not a bolt hole whose centre is closer. On a shared edge of two
                   faces the face whose centroid is nearer wins.
  Examples:
    {"of":"face","side":"top"}                                the top face
    {"of":"face","surf":"round","pick":"outer"}               the outer cylindrical wall
    {"of":"edge","surf":"line","parallel":"z"}                the vertical straight edges
    {"of":"face","surf":"flat","perpendicular":"z"}           the upright flat wall faces
  A FREE-TEXT SENTENCE IS NOT A QUERY. "the outer cylindrical wall face at bottom near
  x=200,y=0" is not parseable geometry — EXPLORE first with describe_part() /
  query_geometry({"of":"face"}) / hover(sid) to see what the part actually has, then SELECT
  with a structured query built from the keys above.

================================================================================
DOC OPS CHEATSHEET. You emit a JSON array of document ops. Units mm / degrees.
Dimension params are objects: {"value": <num>, "expr": null}. A later op refers to a feature
created earlier IN THE SAME ARRAY as "$<index>" (0-based op position); refer to existing tree
features by their id (e.g. "f1").

NATIVE FORMAT — Chudware's native, editable file is .step+. It stores the WHOLE parametric feature
tree plus its simulation studies (and any 2D drawings) in one container the app reopens fully
editable. Save deliverables as .step+ so the user keeps an editable part; a plain STEP/STL is a
flattened export (geometry only — no feature history, no studies).

PICK THE RIGHT FEATURE — model like a person would, with standard editable features. Do NOT
approximate a curved or organic body with a stack of boxes.
  - Round/curved body that is the same all the way around an axis (bottle, vase, fuselage, nozzle,
    knob, wheel)            -> SKETCH the half-profile + REVOLVE it about an axis.
  - Shape that blends between different cross-sections (boat hull, wing, blended duct, turbine
    blade, transition)      -> SKETCH each cross-section on its own plane + LOFT through them.
  - Constant cross-section that follows a path (pipe, handle, gasket, extruded rail, spring/thread)
                            -> SKETCH the profile + a path + SWEEP (for a spring/coil/thread: a
                            HELIX feature as the path — no sketch needed for the coil path).
  - Thin support web bridging two walls (gusset, rib, stiffener)
                            -> open sketch contour (just the web outline) + RIB.
  - Taper a wall for moulding/casting (draft angle, tapered extrusion wall)
                            -> build the base shape first, then add a DRAFT feature on the wall faces.
  - Combine or subtract two separately-built bodies
                            -> BOOLEAN (fuse/cut/common) referencing the two feature ids.
  - Slab / boss / pocket with a flat 2D outline (bracket, plate, rib, gusset) -> SKETCH + EXTRUDE.
  - Repeated holes/bosses (bolt circle, vent grid)  -> make ONE, then LINEAR/CIRCULAR PATTERN it.
  - Symmetric feature                                -> make one side, then MIRROR it.
  - Folded constant-thickness metal (bracket, enclosure, chassis, tray) -> SHEET METAL: sm_base_flange
    then sm_edge_flange / sm_hem / sm_jog bends (see cheatsheets/sheetmetal).
  - Welded frame from stock tube/angle/channel/beam (chassis, rack, railing) -> WELDMENTS: sketch the
    member path, then weld_member with a profile (see cheatsheets/weldment).
  - Freeform / blended thin-wall shell or cover a solid can't make cleanly -> SURFACING: loft/sweep a
    surface, then surf_thicken it into a wall (see cheatsheets/surfacing).
  - Aerofoil / wing section                          -> AIRFOIL sketch entity (see cheatsheets/airfoil).
  - A plain rectangular/cylindrical block            -> a primitive (box/cylinder) is fine.
Use a raw `script` feature ONLY when no combination of the features below can express the geometry.

READ THE RESULT — add_feature's response carries a `changes` summary ("added 2 faces
(+2 cylinder); 2 new bore/hole(s); volume 24000→23435; part bbox 60x40x20 mm"), or
"no geometry change", or "volume effectively UNCHANGED ... too small to be real material",
or an "ENGINE WARNING:" prefix naming exactly what the kernel objected to. `changes.bbox_mm`
is the whole part's overall size — check it against the size you were asked for instead of
spending a measure_part call. Read all of this before building the next feature on the
mutation instead of assuming the op did what you meant.

----------------------------------------------------------------- ADD geometry (op "add_feature")

PRIMITIVES — box/cylinder/sphere/cone/torus. Centered on the origin by default; an optional
`at:[x,y,z]` MOVES the primitive (and `centered:false` puts its base/corner at `at`). `mode`
"boss"|"cut" and `merge` combine it with the running body (a cylinder mode:"cut" bores a hole).
  box {length,width,height}   cylinder {radius,height}   sphere {radius}
  cone {r1,r2,height}         torus {major_r,minor_r}     +optional {at:[x,y,z], centered:false}
  RELATIONAL `at` — instead of coordinates, `at` can be a face/edge QUERY: the primitive
  centres on that geometry. `at:{"of":"face","side":"top"}` = on the top face. Add
  `at_offset:[dx,dy,dz]` to nudge in world axes (e.g. a boss raised 10mm: at_offset:[0,0,10]).

SKETCH + a 2D-profile feature — this is how you make anything that is not a bare primitive.
  1) add_feature type "sketch", refs.plane = "plane.top" | "plane.front" | "plane.right"
     (NEVER "XY"/"XZ"/"YZ"). plane.top = XY (+Z up), plane.front = XZ, plane.right = YZ.
     SKETCH ON AN EXISTING FACE: set refs.plane to a face QUERY — e.g. {"of":"face","side":
     "top"} ("sketch on the top face"). Your 2D origin (0,0) is then the CENTRE of that face,
     so draw relative to it — no need to know its world height. Optional `offset`:<mm> lifts
     the sketch plane along the face/datum normal (a sketch N mm above a face).
     ⚠ For a BOSS on top of an existing part, sketch on the FACE ({"of":"face","side":"top"}),
     NOT "plane.top": primitives are CENTRED on the origin, so plane.top cuts through the
     MIDDLE of a box — a boss sketched there extrudes INTO the part and adds nothing.
  2) sketch_add_entity ops (refer to the sketch as "$<its op index>"). Entity types:
        circle {center:[x,y], radius}      line {p1:[x,y], p2:[x,y]}
        arc {center:[x,y], radius, start:<deg>, end:<deg>, ccw:<bool>}      point {p:[x,y]}
        rectangle {center:[x,y], width, height}  — PREFERRED way to make a rectangle (1 op,
                expands to 4 closed lines). Also {corner1:[x1,y1], corner2:[x2,y2]}. width/height
                accept bare numbers or {"value":N,"expr":null}. (4 separate lines still work.)
        spline {points:[[x,y],[x,y],...]}  — a smooth curve THROUGH the points (use for organic /
                aerofoil / cam outlines). Close a region by making the first and last point meet
                another edge.
        airfoil {naca:"2412" OR dat:"<path>", chord:<mm>, aoa:<deg>, points:50, closedTE:true}
                — expands to the upper+lower aerofoil splines as one closed loop (see airfoil sheet).
  3) Turn the sketch into a solid with ONE of:
     EXTRUDE  params {mode:"boss"|"cut", end:"blind"|"through_all", depth:<mm>},
              refs.profile = [{"kind":"region","sketch":"$<idx>"|"f<id>","centroid":[x,y],"hint_index":0}]
              (extrudes/cuts ALL the sketch's regions).
     REVOLVE  params {mode, angle:{value:360,...}}, refs.profile = [region],
              refs.axis = an axis datum {"kind":"datum","feature":"axis.x"|"axis.y"|"axis.z"}
              (or a sketch line edge). The profile must lie OUTSIDE / beside the axis (offset it).
     LOFT     params {mode}, refs.sections = [region, region, ...] (>= 2 region selectors, each
              naming a DIFFERENT sketch on its own plane, in order). Optional params.ruled:true.
     SWEEP    params {mode}, refs.profile = [region] (the cross-section),
              refs.path = a selector naming the PATH sketch (an open line/arc/spline, or a `helix`).

DRESS / TRANSFORM an existing body — name edges/faces with a geometry QUERY (no clicking
needed), same vocabulary for faces AND edges. INSPECT before you ACT: when you cannot name the
geometry with certainty, call query_geometry FIRST and build the pick from what it returns
rather than guessing a filter. You never need a face number, an edge number, or a coordinate.
  fillet  params {radius} refs.edges = {"of":"edge", ...}
          VARIABLE-RADIUS: add params.radius2 — a linear taper r→r2 along each edge (requires
          explicit edge picks, not a direction filter). e.g. r=3, radius2=1 tapers each selected
          edge from 3mm to 1mm.
  chamfer params {length} refs.edges = {...}
          ASYMMETRIC: add params.length2 — leg d1=length on one incident face, d2=length2 on the
          other (requires explicit edge picks). e.g. length=2, length2=0.5.
  shell   {thickness} refs.remove = {"of":"face","side":"top"}   (omit remove = closed shell)
  draft   params {angle (deg)}, refs.neutral = a planar datum OR a face query (the parting/neutral
          plane — the hinge about which faces tilt), refs.faces = the faces to taper {"of":"face",...}
          Optional refs.pull = an axis datum (pull direction; defaults to neutral plane normal).
          Positive angle = narrows in the pull direction (draft inward / SolidWorks default).
          e.g. 3° draft on all side walls of a block, neutral = bottom face:
          {"type":"draft","name":"Draft","params":{"angle":{"value":3,"expr":null}},
           "refs":{"neutral":{"of":"face","side":"bottom"},"faces":{"of":"face","surf":"flat"}}}
  Query predicates (combine freely; omit all = EVERY edge/face of `of`):
    "side": which side it's on — "top"/"bottom"/"left"/"right"/"front"/"back" (top=+Z, front=-Y;
            signed axes "+z"/"-x"/.. also work). Keeps the faces/edges furthest that way.
            EXCEPTION with a size pick: side+pick "largest"/"smallest" means "the largest/
            smallest face FACING that way" — side filters to that facing, pick chooses by area
            (so {"side":"top","pick":"largest"} finds a base's big top even when a taller
            sliver face sits higher).
    "surf": the shape — faces "flat"/"round" (round matches ANY curved face: cylinder/cone/sphere/
            torus); also "cylindrical"/"conical"/"spherical"/"toroidal" for specific curved types.
            Edges: "straight"/"round" (or exact: plane/cylinder/cone/sphere/torus, line/circle).
    "parallel": which way it runs — "z" (="vertical"), "x", "y", or [x,y,z]. parallel:"z" = the
            vertical corner edges of a block.
    "pick": the size — faces "largest"/"smallest", edges "longest"/"shortest"; or
            "inner"/"outer" to tell coaxial round geometry apart (tube inner vs outer
            wall, bore vs rim) — e.g. {"of":"face","surf":"round","pick":"outer"}.
    "near":[x,y,z] + "count": the N edges/faces nearest a point.  "index":[i,..]: pick by ordinal.
  On CURVED bodies: a REVOLVE/LOFT/SWEEP body is mostly ONE freeform (B-spline) face, so
    surf:"flat" matches it 0 times — that is correct, there are no planar faces. A `side` query on
    such a body returns the freeform wall (the only face facing that way), not a flat cap, because
    there is no cap. If you need selectable FLAT end faces (e.g. the rim of a tube), build the body
    profile. Query predicates match zero or more than one? Narrow with near/count/index rather
    than guessing which one you meant. Call query_geometry / doc_query FIRST whenever you are
    not certain what edges/faces a part has — it returns each edge/face's shape (surf) + side so
    you can see what will resolve before you commit a fillet/chamfer/shell/draft to it.

HELIX / COIL / SPRING PATH (type "helix"). A helix is a CURVE feature — it emits no body by
itself; use it as refs.path of a SWEEP to make a spring, coil, or thread.
  Required: refs.base = a datum plane ("plane.top" etc., the helix base), params.pitch (mm per
  turn), params.height (total axial height). Optional params: radius (default 10), taper (deg,
  narrows toward top; 0 = straight), turns (overrides height = turns*pitch), start_angle (deg),
  shape:"spiral" (flat Archimedean spiral — set turns, not height; start_radius optional).
  VARIABLE PITCH: instead of pitch+height, give pitch_table — an array of rows
  {turn:<float>, pitch:<mm>, dia?:<mm>} (or bare [turn, pitch, dia?] arrays) — the radius
  and pitch interpolate piecewise-linear between rows. Needs >= 2 rows.
  e.g. simple 3-turn compression spring: profile circle r=1.5 on plane.front at (8,0),
  helix base=plane.top radius=8 pitch=4 height=12, sweep profile over helix.
  Full example (spring coil, profile + helix + sweep):
  {"type":"helix","name":"SpringPath","params":{"pitch":{"value":4,"expr":null},
   "height":{"value":12,"expr":null},"radius":{"value":8,"expr":null}},
   "refs":{"base":"plane.top"}}

RIB — thin support web grown from an open sketch contour (type "rib"). The sketch must contain
  an OPEN line/arc/spline that bridges the body. The engine drops the contour inward toward the
  body centroid, thickens it by `thickness`, trims it to the body bbox, and fuses it.
  refs.profile = [region selector pointing at the open contour sketch]. Optional params.thickness
  (default 3mm). The sketch plane is typically a side face or datum plane; the rib web grows
  perpendicular to that plane.
  e.g. a 2mm web sketch on plane.right, bridging a bracket:
  {"type":"rib","name":"Gusset","params":{"thickness":{"value":2,"expr":null}},
   "refs":{"profile":[{"kind":"region","sketch":"$2","centroid":[0,10],"hint_index":0}]}}

SPLIT — cut a body into pieces along a planar tool (type "split"). Each piece becomes a
  separate body. Optional params.keep: "both" (default), "above", or "below" (relative to the
  tool plane normal). refs.tool = a datum plane or a face selector.
  e.g. split a body at the midplane, keep both halves:
  {"type":"split","name":"Halves","params":{},"refs":{"tool":"plane.right"}}
  e.g. keep only the top half:
  {"type":"split","name":"Top","params":{"keep":"above"},"refs":{"tool":"plane.top"}}

BOOLEAN — explicit fuse/cut/intersect of two named bodies (type "boolean"). Use when you have
  built two separate bodies and want to combine them without relying on the implicit
  mode:"boss"/"cut" merging in extrude/revolve/sweep. params.op: "fuse" (union),
  "cut" (subtract the tool from the target), "common" (intersection). Also accepts
  "combine"/"subtract"/"intersect" as op aliases.
  Canonical form: refs.targets = [{"kind":"body","owner_feature":"f1"}],
                  refs.tools   = [{"kind":"body","owner_feature":"f3"}].
  Shorthand: params.a = "f1" (target body feature id), params.b = "f3" (tool body feature id)
  — the engine accepts either form. Both bodies are consumed; a single new body replaces them.
  A fuse must produce ONE connected solid; a cut/common must also yield ONE solid piece
  (disjoint results fail — position the bodies so they actually overlap before cutting).
  e.g. subtract body f3 from body f1 (canonical):
  {"type":"boolean","name":"CutResult","params":{"op":"cut"},
   "refs":{"targets":[{"kind":"body","owner_feature":"f1"}],
           "tools":[{"kind":"body","owner_feature":"f3"}]}}
  e.g. union two bodies (shorthand):
  {"type":"boolean","name":"Union","params":{"op":"fuse","a":"f1","b":"f2"}}

INTENT FEATURES — one op that is a whole recipe. PREFER these over hand-built sketch+cut+pattern
for the common machining features; they take a face in plain words and need no positions math.
  hole  — drill into a planar face. params {style:"simple"|"cbore"|"csink"|"tap", end:"through_all"
          |"blind", diameter, depth?(blind), cbore_d?/cbore_depth?, csink_d?/csink_angle?}.
          Size the bore by `diameter` (mm) OR by fastener standard `"standard":"M3"` (the engine
          resolves the clearance bore — M3->3.4mm; tap-drill when style:"tap"). For an "M3 clearance
          bore" just give {"standard":"M3"}.
          refs.face = a face query e.g. {"of":"face","side":"top"}. Optional `positions`:[[u,v],..]
          for several holes; omit = one at the centre.
          POSITIONS FRAME (deterministic): origin = the centroid of the face's OUTER boundary
          (holes already cut in the face do NOT shift it); u = world +X projected into the face
          (world +Y if the face is perpendicular to X); v = n×u, right-handed around the outward
          normal. So on a top face u=+X,v=+Y; on a front (−Y) face u=+X,v=+Z; on a right (+X)
          face u=+Y,v=+Z. bolt_circle uses the same frame (start_angle measured from u).
          WORLD placement instead: params `world_points`:[[x,y,z],..] (mm, world frame) — each
          names a point on the bore AXIS; the engine projects it along the face normal onto
          the face it enters (with a word query like side:"top", whichever face facing that way
          the axis hits first — e.g. each of four boss tops; reported) and refuses one whose
          axis enters no such face. Never both positions and world_points.
  bolt_circle — a RING of holes in one feature (no per-hole trig). params {diameter, count, pcd
          (bolt-circle diameter), style?, end?, depth?(blind), start_angle?:<deg>,
          world_center?:[x,y,z]}. refs.face = a face query (defaults to the top face); the ring
          is centred on the face centre, or on world_center (a point on the ring's axis). e.g. 6×⌀5 on a 40mm circle:
          {"type":"bolt_circle","params":{"diameter":5,"count":6,"pcd":40},"refs":{"face":{"of":"face","side":"top"}}}
  rib    — a thin web from an open sketch profile. refs.profile = [region]. (See sheet if present.)
  standard_part — a catalog HARDWARE part expanded into normal editable tree features (same
          generators as parts_make). PREFER over hand-modeling stock hardware. params {family
          (required), size|thread ("M3", "1/4-20", bearing "608"), length_mm (screws/standoffs/
          rod/dowel/insert; nuts/washers/bearings ignore it)}. Families: shcs, bhcs, fhcs,
          hex_bolt, hex_nut, washer_flat, washer_lock, standoff_round, standoff_hex,
          threaded_rod, dowel_pin, heat_set_insert, bearing. Invalid family → error lists keys.
          {"type":"standard_part","name":"Standoff","params":{"family":"standoff_hex","thread":"M3","length_mm":22}}

ADVANCED FEATURE FAMILIES — three families the engine fully supports (watertight geometry). Each has
its own detailed cheatsheet; the essentials to invoke them:
  SHEET METAL (sm_*) — a folded constant-thickness part. sm_base_flange {thickness}, refs.profile =
    [region], makes the flat base AND marks the part as sheet metal; then bend walls off its straight
    top-rim edges with sm_edge_flange {length,angle} / sm_hem {hem_type} / sm_jog {offset,angle}, each
    refs.edge = ONE straight edge on a thickness face — query_geometry({"of":"edge","surf":"line",
    "near":[x,y,z],"count":1}) FIRST and pass the RESOLVED match back (this slot rejects a bare query);
    sm_flat_pattern unfolds the developed blank. Detail: cheatsheets/sheetmetal.
  WELDMENTS (weld_*) — a frame of stock members. Sketch the centerline PATH, then weld_member
    {profile, +per-profile dims, joint:"miter"|"butt"|"cope"} -> ONE member body per path segment.
    profiles: square_tube/rect_tube/round_tube/angle_L/flat_bar/c_channel/i_beam/t_section. ! refs.sketch
    is a BARE STRING sketch id ("$0"), NOT a region selector. Detail: cheatsheets/weldment.
  SURFACING (surf_*) — zero-thickness surfaces you thicken into solids. Build a surface (surf_planar/
    surf_extrude/surf_revolve/surf_sweep/surf_loft/surf_ruled/surf_fill), then surf_thicken {thickness},
    refs.surface = {"kind":"body","owner_feature":"$N"}, for a constant wall (or surf_knit panels
    watertight). surf_revolve axis = {"kind":"datum","feature":"axis.z"}. Detail: cheatsheets/surfacing.

REPEAT / SYMMETRY. The seed feature(s) in refs.features MUST be earlier sketch-based features
that carry geometry to copy — an EXTRUDE boss/cut, a HOLE, a REVOLVE/SWEEP/LOFT. A bare primitive
(box/cylinder) is NOT patternable; make the repeated thing with sketch+extrude, then pattern that.
  linear_pattern  params {count1:<int>, spacing1:<dim>, count2?:<int>, spacing2?:<dim>},
          refs.dir1 = {"kind":"datum","feature":"axis.x"}, refs.dir2? = a datum axis, refs.features = ["f3", ...].
  circular_pattern params {count:<int>, angle:<dim, total, dflt 360>, equal_spacing?:<bool>},
          refs.axis = {"kind":"datum","feature":"axis.z"}, refs.features = ["f2", ...].
  mirror  refs.plane = {"kind":"datum","feature":"plane.right"} (or a planar face), refs.features = ["f2", ...].
  `count*` are plain INTEGERS; spacings/angles are dim objects.
  Reserved datums: planes "plane.top"/"plane.front"/"plane.right", axes "axis.x"/"axis.y"/"axis.z",
  point "point.origin" (canonical; bare "origin" also accepted) — referenced as {"kind":"datum","feature":"point.origin"}.
  The world origin is selectable in the FeatureManager and renders as a glyph in the viewport.
  WHAT'S PATTERNABLE/MIRRORABLE: sketch-based features (extrude/revolve/sweep/loft boss or cut,
  hole) and transforms. Primitives (box/cylinder/...) are NOT — rebuild them as sketch+extrude.

MOVE / PLACE A BODY (op "add_feature", type "transform"). Translate and/or rotate ANY body — a
lofted fuselage, an extruded wing — not just a primitive's `at`. Use it to position separate
components into one coordinate frame for assembly (e.g. move a tail to meet the fuselage).
  params: translate:[dx,dy,dz] (mm) and/or angle:<dim deg> with rotate:[ax,ay,az] (rotation axis),
          pivot:[x,y,z] (point the rotation turns about, default origin), and copy:true to KEEP the
          original and add a MOVED DUPLICATE as a new body (instancing; default false = move in place).
  top-level: "body":"f3" targets THAT feature's body (default = the current running body). The
          named body is honored exactly — so in a multi-body assembly you can transform any named
          component (not just the last one), and several per-body transforms in one batch each land
          on their own body.
  Rotation is applied first (about pivot), then translation. e.g. move a wing +120 in X and up 8:
  {"type":"transform","name":"PlaceWing","params":{"translate":[120,0,8]},"body":"f4"}
  e.g. yaw a body 30° about Z through the origin:
  {"type":"transform","name":"Yaw","params":{"angle":{"value":30,"expr":null},"rotate":[0,0,1]}}
  e.g. two instances of a body 50 apart: a transform with params {"translate":[50,0,0],"copy":true}
  ASSEMBLY: import_part / make_standard_part each add a body whose BASE seats at z=0, axis +Z
          (make_standard_part also returns its bbox + datum). Position each with its own
          transform body:"fN" — e.g. import a screw (f2), washer (f3), nut (f4), then transform
          each by id to its seat. Build hollow CURVED bodies with a wall-profile revolve/loft, not
          shell (shell is prismatic-only).

PLANE / OFFSET CONVENTIONS (Z-up). plane.top = XY (normal +Z), plane.front = XZ (normal +Y),
plane.right = YZ (normal +X). A sketch `offset`:<mm> shifts the sketch plane ALONG that normal
(positive = +Z on top, +Y on front, +X on right); a negative offset goes the other way. When
sketching ON a face, `offset` lifts along that face's outward normal.

----------------------------------------------------------------- SKETCH OPS (append to an existing sketch feature)

sketch_add_entity   {"op":"sketch_add_entity","sketch":"$0","entity":{...}}
                    Add a line/circle/arc/spline/polygon/rectangle/airfoil/point entity to a sketch.

                    REGULAR POLYGON (hexagons, nuts, bolt-heads, prisms) — ALWAYS use this ONE
                    entity; never hand-draw the sides as separate lines (slow + usually fails to
                    close). It expands to N closed segments for you:
                      {"op":"sketch_add_entity","sketch":"$0",
                       "type":"hexagon","center":[0,0],"across_flats":16}
                    - type: "polygon"/"ngon" (needs "sides":N) OR a named n-gon that implies its
                      count: triangle, pentagon, hexagon, heptagon, octagon, nonagon, decagon,
                      dodecagon (an explicit "sides" still overrides).
                    - size — give exactly ONE: "across_flats" (wrench/spanner size, flat-to-flat =
                      the usual way hex stock/nuts are specified), "circumradius" (centre→vertex),
                      or "inradius"/"apothem" (centre→edge). "rotation":<deg> optional (default: a
                      vertex on +X; use rotation:90 to put a flat on top).
                    e.g. a hex prism 16mm across flats, 20mm tall:
                      [{"op":"add_feature","type":"sketch","refs":{"plane":"plane.top"}},
                       {"op":"sketch_add_entity","sketch":"$0","type":"hexagon","across_flats":16},
                       {"op":"add_feature","type":"extrude","params":{"mode":"boss","end":"blind",
                        "depth":20},"refs":{"profile":[{"kind":"region","sketch":"$0","hint_index":0}]}}]

                    RECTANGLE — PREFERRED over 4 hand-drawn lines (1 op, always closes):
                      {"op":"sketch_add_entity","sketch":"$0",
                       "entity":{"type":"rectangle","center":[0,0],"width":80,"height":50}}
                    - center-form: center:[x,y] (default [0,0]), width, height (both > 0). Dims
                      may be bare numbers or {"value":N,"expr":null}. Optional rotation:<deg>.
                    - two-corner form: corner1:[x1,y1], corner2:[x2,y2] (order-independent).
                    - alias type "rect" also accepted. Expands to 4 CCW line segments.
                    4 separate line entities remain valid if you already have corners pinned.

sketch_convert      {"op":"sketch_convert","sketch":"<sketch-fid>","refs":[Ref,...],"construction":false,"fix":true}
                    Project external geometry INTO an existing sketch (SolidWorks "Convert Entities").
                    Each Ref is {"datum":"front"|"top"|"right"} for a datum plane (intersection line)
                    OR {"kind":"face"|"edge","selector":{"sid":"<sid>"}} for solid geometry.
                    A face ref projects ALL its boundary wires (outer + hole loops); oblique circular
                    edges become ellipses; near-perpendicular edges collapse to a point.
                    construction:false (default) = real, extrudable geometry; true = blue reference lines.
                    fix:true (default) = each entity is pinned with a whole-entity fix relation.
                    ONE-SHOT: projected entities do NOT update when the source model changes.
                    id_map: first new entity → "$<op-index>"; rest → "$<op-index>#1", "$<op-index>#2", …
                    e.g. project the front datum plane and one face's wires into sketch s2:
                    {"op":"sketch_convert","sketch":"s2",
                     "refs":[{"datum":"front"},{"kind":"face","selector":{"sid":"Fdb31d2"}}]}
                    Use project_into_sketch() MCP tool for the ergonomic string-ref interface.

----------------------------------------------------------------- EDIT an existing feature
  edit_feature   {"op":"edit_feature","id":"f1","params":{"length":{"value":80,"expr":null}}}
  set_suppressed {"op":"set_suppressed","id":"f3","value":true}
  delete_feature {"op":"delete_feature","id":"f3","cascade":true}
  rename         {"op":"rename","id":"f1","name":"BasePlate"}

----------------------------------------------------------------- EXAMPLES

A — 40x40x20 block, then round ALL its edges 3mm:
[
 {"op":"add_feature","feature":{"type":"box","name":"Block","params":{"length":{"value":40,"expr":null},"width":{"value":40,"expr":null},"height":{"value":20,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"fillet","name":"Round","params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}
]

B — a 60x40 plate 6mm thick with four 3mm corner holes (sketch circles + extrude-cut):
[
 {"op":"add_feature","feature":{"type":"box","name":"Plate","params":{"length":{"value":60,"expr":null},"width":{"value":40,"expr":null},"height":{"value":6,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"Holes","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[24,-14],"radius":3}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[-24,-14],"radius":3}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Drill","params":{"mode":"cut","end":"through_all"},"refs":{"profile":[{"kind":"region","sketch":"$1","centroid":[0,0],"hint_index":0}]}}}
]

C — a vase as a SOLID OF REVOLUTION. The half-profile must be a CLOSED region: the curved outer
wall (a spline) plus a straight line back down the Z axis to close it. Revolve 360 about Z:
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Profile","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.front"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"spline","points":[[0,0],[15,0],[18,20],[10,45],[12,60],[0,60]]}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"line","p1":[0,60],"p2":[0,0]}},
 {"op":"add_feature","feature":{"type":"revolve","name":"Body","params":{"mode":"boss","angle":{"value":360,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[8,30],"hint_index":0}],"axis":{"kind":"datum","feature":"axis.z"}}}}
]

D — a blended duct by LOFTING a circle into a square (two sketches on parallel planes):
[
 {"op":"add_feature","feature":{"type":"sketch","name":"Bottom","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$0","entity":{"type":"circle","center":[0,0],"radius":20}},
 {"op":"add_feature","feature":{"type":"sketch","name":"Top","params":{"entities":[],"constraints":[],"offset":{"value":50,"expr":null}},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[-15,-15],"p2":[15,-15]}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[15,-15],"p2":[15,15]}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[15,15],"p2":[-15,15]}},
 {"op":"sketch_add_entity","sketch":"$2","entity":{"type":"line","p1":[-15,15],"p2":[-15,-15]}},
 {"op":"add_feature","feature":{"type":"loft","name":"Blend","params":{"mode":"boss"},"refs":{"sections":[{"kind":"region","sketch":"$0","centroid":[0,0],"hint_index":0},{"kind":"region","sketch":"$2","centroid":[0,0],"hint_index":0}]}}}
]

E — 6 bosses evenly around the Z axis: make ONE as a sketch+extrude boss (a primitive can't be
patterned), then circular-pattern that extrude feature about axis.z:
[
 {"op":"add_feature","feature":{"type":"cylinder","name":"Base","params":{"radius":{"value":40,"expr":null},"height":{"value":5,"expr":null}}}},
 {"op":"add_feature","feature":{"type":"sketch","name":"BossSk","params":{"entities":[],"constraints":[]},"refs":{"plane":"plane.top"}}},
 {"op":"sketch_add_entity","sketch":"$1","entity":{"type":"circle","center":[30,0],"radius":4}},
 {"op":"add_feature","feature":{"type":"extrude","name":"Boss","params":{"mode":"boss","end":"blind","depth":{"value":10,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"$1","centroid":[30,0],"hint_index":0}]}}},
 {"op":"add_feature","feature":{"type":"circular_pattern","name":"Ring","params":{"count":6,"angle":{"value":360,"expr":null},"equal_spacing":true},"refs":{"axis":{"kind":"datum","feature":"axis.z"},"features":["$3"]}}}
]

----------------------------------------------------------------- TOOL SCOPE
request_tools — meta-op (not applied to the doc). Ask for more vocabulary mid-run:
  [{"op":"request_tools","classes":["full_ops"|"sheetmetal"|"weldments"|"surfacing"]}]
Domain classes append cheatsheets/sheetmetal.md, weldment.md, or surfacing.md on the next turn.
You already have the full op set on this sheet; prefer emitting real ops. A second full_ops
request is a no-op.
```

