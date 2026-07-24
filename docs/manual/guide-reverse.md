# Guide: Reverse

```text
REVERSE-ENGINEERING — get an EDITABLE parametric feature tree out of something you did not model.
Two lanes, depending on what you are starting from.

WHICH LANE?
  You have a FILE (STEP/IGES/BREP/STL/OBJ) or an already-imported doc  → LANE A. One call,
  deterministic, verified by boolean compare. Nothing to install.
  You have the PHYSICAL PART on your bench and a camera                → LANE B. A capture
  session: photos/video/webcam (or a datasheet), reconstruct, SET SCALE, fit primitives, build
  parametric. Needs external backends (see REQUIREMENTS) and two human confirm gates.
  Both: Lane B ends in geometry, so once a Lane B session has produced a solid you can run the
  Lane A tools (compare_parts, convert_face_to_sketch) on it like any other part.


================================================================================================
LANE A — YOU ALREADY HAVE GEOMETRY
================================================================================================

Turn an imported STEP/mesh body into an EDITABLE parametric feature tree, verified against the
source solid by boolean comparison. Deterministic recognition first; a learned model is
considered only after a measured residual (GEOMETRY_LLM_REPRESENTATION_REVIEW §3.4/§3.6) — and
the heuristics NEVER hide evidence: unclaimed source faces are always listed.

THE ONE-CALL PATH
  reverse_engineer(source) — source is a file path (STEP/IGES/BREP/STL/OBJ/…) or the doc_id of
  an already-imported part. It imports (meshes are sewn to a solid), recognizes, rebuilds the
  recognized features into a NEW part, and verifies EVERY batch with a boolean compare against
  the source. The new part is made current; the imported source doc stays open as the reference.

  What the recognizer can claim deterministically:
  * prismatic base — largest planar face + its antiparallel partner at distance d, with the
    perpendicular/parallel lateral faces explaining >=80% of the remaining area. The profile is
    traced from the face's boundary wires; corner arcs that match a detected fillet radius are
    un-rounded first so the fillet stays a SEPARATE editable feature instead of being baked in.
  * turned body — every curved face coaxial on a principal axis through the origin: the revolve
    profile is computed ANALYTICALLY from the cylinder radii + spans (area/(2*pi*r)) — no
    sectioning, no tessellation.
  * holes — bore-concavity cylinders become real `hole` features {d=2r, blind depth | through},
    entered from the planar cap the bore opens onto.
  * patterns — same-radius hole groups at uniform angular spacing become ONE seed hole + a
    circular_pattern (collinear equal spacing → linear_pattern). Editing the seed re-drives
    every instance.
  * fillets/chamfers — small convex cylinders/tori and 45°±2° planar bridges are CANDIDATES:
    applied only when a plain-word edge query resolves on the rebuilt body AND the match does
    not regress; otherwise they are returned under `candidates` for you to apply by hand.

READING THE RESULT
  match_pct     common_volume / max(volume) of the two solids. 100 ≈ identical. null means the
                boolean itself failed (sick geometry) and the compare DEGRADED to volume/bbox
                agreement — trust it less, say so.
  features      what was rebuilt (ids are live feature ids in the new part — edit them).
  rejected      batches that built but made the match WORSE (or failed to build): they were
                REVERTED, with before/after numbers. Nothing rejected is silently kept.
  candidates    recognized-but-not-applied dress-ups/holes, with radius/length + a world-space
                `near` point. Apply one with add_features, e.g.
                {"type":"fillet","params":{"radius":{"value":3,"expr":null}},
                 "refs":{"edges":{"of":"edge","near":[50,30,0],"count":1}}}
                then re-run compare_parts to prove it helped.
  unexplained   missing_mm3 (source material the rebuild lacks), extra_mm3 (rebuild material
                the source never had), and up to 10 residual REGIONS with centers — go look
                exactly there.
  recognition.unclaimed  source faces no pass claimed — hover(sid) them on the ref doc.

MANUAL PRIMITIVES (when the one-call path is not enough)
  compare_parts(doc_a, doc_b)          the same boolean verdict for ANY two docs. doc_a is the
                                       reference. WORLD-FRAME: no alignment/ICP is attempted —
                                       a moved copy scores low, by design.
  convert_face_to_sketch(face, ...)    "convert entities": trace one planar face's wires into
                                       an editable sketch ON that face, then extrude/cut it.
                                       Creates a NEW sketch positioned on the face's own plane.
  project_into_sketch(sketch, refs)    "convert entities" INTO AN EXISTING sketch — appends
                                       projected geometry (lines/arcs/circles/ellipses) to a
                                       sketch you are already building, mirroring SolidWorks
                                       Convert Entities workflow.  Distinct from
                                       convert_face_to_sketch (which makes a new sketch).
                                       refs: a datum string ("front"/"top"/"right"), a face sid
                                       ("Fdb31d2"), a side word ("top" for the top face — note
                                       "front"/"top"/"right" resolve to DATUM PLANES not faces),
                                       an edge sid ("E4a7c9"), or a ref dict; or a list of any
                                       mix.  A face ref projects ALL its boundary wires.  Each
                                       entity is pinned with a fix relation.  ONE-SHOT: converted
                                       geometry does NOT update when the source model changes.
                                       Use construction=True for reference-only geometry.
  describe_part()/hover(sid)           read the imported geometry as text (cad_guide("gsl")).

WORKFLOW FOR THE RESIDUAL
  1. reverse_engineer(path) → note match_pct.
  2. For each unexplained region: hover the nearby unclaimed sids on the ref doc, model the
     feature by hand on the rebuilt part (convert_face_to_sketch helps), compare_parts again.
  3. Only call the job done when match_pct is high AND you can name every unclaimed face.

CAVEATS — BE HONEST ABOUT THESE
  * Mesh input (STL/OBJ/…) is faceted: the exact-surface passes claim little. Expect a low
    deterministic yield and say so; the value is the imported solid + the honest residual map.
  * The rebuild is authored in the SOURCE's coordinates; everything assumes world-frame match.
  * A cbore/csink stack is recovered as separate coaxial simple holes, not one wizard hole.
  * Cone faces (turned chamfers), splines and freeform faces are not claimed deterministically
    — they surface in `unclaimed`/`candidates`, never silently approximated.


================================================================================================
LANE B — YOU HAVE THE PHYSICAL PART AND A CAMERA
================================================================================================

The part is on your bench. You want dimensionally-correct CAD you can print a replacement from,
modify, or design a mating part against. Everything lives in a SESSION (a folder of images,
runs, scale, fits and results) so you can stop, re-capture, and resume. The Reverse Engineering
workspace tab drives the same session as the tools below — use whichever you prefer.

THE HONEST CHAIN
  scan → mesh → AI → parametric, with a mesh-body fallback.
  There is NO direct scan→parametric step. Anything that claims one is lying. When the
  parametric build cannot explain the geometry you keep the cleaned mesh body and say so.

STEP 0 — CHECK WHAT YOU CAN ACTUALLY RUN
  reveng_status()  MANDATORY PRE-FLIGHT. Call it before you start a session, and before you
                   tell anyone a reconstruction is possible. It reports capabilities
                   {sparse, dense, splat, single_photo, webcam, gpu}, which backends are
                   provisioned, and human-readable `banners` naming what to install.
                   A stock install has NONE of the reconstruction backends (see REQUIREMENTS).
  Act on the answer — a false capability is a ROUTING decision, not something to retry:
    sparse=false        no photogrammetry at all. Do NOT call reveng_reconstruct. Say so, quote
                        the banner, and offer the lanes that still work: reveng_ingest_datasheet
                        / reveng_research_source (catalog part), import_scan_mesh (user already
                        has a scan), or Lane A (user can supply a STEP/mesh).
    dense=false, splat=false
                        sparse only: you get poses + a sparse cloud, not a surface to fit. Name
                        the missing env var (OPENMVS_BIN / BRUSH_BIN) rather than proceeding.
    single_photo=false  reveng_single_photo is unavailable — and it was never a measurement
                        path anyway (STEP 4).
    webcam=false        no live capture; the user supplies files via reveng_add_images /
                        reveng_add_video.
  Retrying a tool whose backend is absent will never succeed. Report the missing prerequisite
  and the fix-it hint; do not loop, and never substitute an approximate tier for a missing
  accurate one without saying that is what you did.

STEP 1 — START A SESSION
  reveng_start_session(name?, part_ref?)   → session_id. Everything else takes that session_id.
                                             part_ref seeds the session from a parts-hub entry.
  list_reveng_sessions() / get_reveng_session(session_id)   resume or inspect one later.

STEP 2 — PRINT THE SCALE MAT (do this BEFORE you shoot)
  reveng_print_scale_mat(size="A4" | "Letter")  → a print-ready ArUco mat PDF.
  * PRINT IT AT 100% / "actual size". "Fit to page", "shrink to fit" and borderless scaling
    silently resize the markers — the mat still LOOKS right, and every dimension you derive
    from it is then wrong by the printer's scale factor. Check a printed square with calipers
    before you trust a session.
  * Tape it flat. A curled or creased mat bends the plane fit and biases scale.
  * Stand the part ON the mat so markers and part are in the same views. Scale is triangulated
    from the markers ACROSS the posed views — a mat visible in only one photo is not enough.

STEP 3 — CAPTURE
  Lock the camera FIRST, then shoot the whole set without touching it:
  * Lock AUTOFOCUS. A focus hunt between frames changes the effective focal length, and SfM
    reads that as camera motion — registration degrades or the scale drifts across the set.
  * Lock AUTO-EXPOSURE (and white balance if you can). Per-frame exposure changes wreck feature
    matching on plain/machined surfaces.
  * Diffuse, even light. No hard specular hot-spots, no shadow that moves with the camera.
  * Matte the part if it is shiny/transparent — chalk spray or dry developer. Bare polished
    metal reconstructs badly at any quality setting.
  * Orbit the part; do not spin the part in front of a fixed camera (the background must stay
    still relative to the part). The default guided plan is an 11-shot orbit; 40–80 overlapping
    frames gives a far better sparse model. Aim for ~70% overlap between neighbours, two
    elevations (roughly 20° and 60°), plus close-ups of every bore, boss and threaded feature.
    Flip the part and shoot a second set for the underside.
  Feeding the session:
  reveng_add_images(session_id, paths=[...] | dir=...)     stills from any source, incl. webcam
  reveng_add_video(session_id, path, max_frames=120)       a slow orbit video, decimated
  reveng_ingest_datasheet(session_id, path)                PDF/image datasheet → tables + crops
                                                           (the fastest lane when the part is a
                                                           catalog item — no photogrammetry)
  import_scan_mesh(session_id, path, scale_mm?)            you already own a structured-light /
                                                           laser scan mesh; skip to STEP 5
  get_reveng_coverage(session_id)                          which of the 11 orbit positions are
                                                           captured/missing, per-shot blur flags,
                                                           and a good/partial/insufficient
                                                           verdict. Re-shoot before reconstruct.
  Webcam capture is live in the Reverse Engineering tab (V4L2 on Linux; Media Foundation on
  Windows). Frames flagged as synthetic/test-pattern are NOT real captures — never accept a
  session built from them.

STEP 4 — RECONSTRUCT
  reveng_reconstruct(session_id, quality="draft"|"normal"|"high", backend?) → run_id
  What the stages mean:
  * SPARSE — structure-from-motion (COLMAP). Solves where each camera was and produces a sparse
    point cloud. This is the step that decides whether the whole session is usable: if fewer
    than 60% of your images register, the pipeline falls back to a feed-forward model
    (VGGT / MapAnything) which recovers a pose+point estimate from hard image sets.
    Persistently low registration = go re-shoot with more overlap, not more compute.
  * DENSE — OpenMVS turns the posed sparse model into a dense point cloud and a watertight,
    textured mesh. This is the surface you will actually fit primitives to.
  * SPLAT — Brush trains a 3D Gaussian Splat instead, then exports a mesh. Better on shiny and
    thin/fine detail; heavier, needs a GPU. Alternative to DENSE, not a step after it.
  * CLEANUP — outlier removal, hole filling and decimation on whichever mesh came out.
  Neither dense nor splat is available unless you provisioned the backend — reveng_status()
  first, and expect an honest `backend_not_provisioned` envelope rather than a fake mesh.

  SINGLE-PHOTO TIER — reveng_single_photo(session_id, tier="triposr"|"trellis")
  One image → a plausible-looking mesh. It is force-tagged `approximate`. It is a REFERENCE /
  preview shape only and is NEVER dimensionally trustworthy — the model invents the unseen
  sides and has no metric scale at all. reveng_build_parametric REFUSES an approximate session
  unless you pass strategy="override_approximate", and if you do that you own the consequences.
  Never hand a single-photo result to a machinist or a print queue as a measured part.

STEP 5 — SET AND CONFIRM SCALE  ← the step that decides if the CAD is real
  reveng_set_scale(session_id, method, params)
    "aruco_mat"  params {images?: [...], board?: "A4"|"Letter"}   preferred: triangulates the
                 printed markers across the posed views.
    "two_point"  params {image, p1:[x,y], p2:[x,y], distance_mm}  one caliper measurement you
                 took between two points you can click in one image.
    "known_dim"  params {feature, value_mm, source}               a dimension you know for
                 certain (a bearing bore, a standard thread OD).
  HUMAN SCALE-CONFIRM GATE. The session will not proceed until a person accepts the scale.
  Do not wave it through: every downstream dimension is this number multiplied by a shape.
  Sanity-check it against a caliper reading on a feature you did NOT use to set it.

STEP 6 — FIT PRIMITIVES
  reveng_fit_primitives(session_id) → fits
  RANSAC over the mesh: planes, cylinders, spheres and cones, plus cross-sections, and a
  hole-vs-boss role for every cylinder (is this a bore you drill, or a spigot you turn?).
  Read the fits before you build — a cylinder classified as a boss when it is really a
  counterbore produces a solid model with material in the wrong place.

STEP 7 — REVIEW THE DIMENSIONS
  list_reveng_dims(session_id)                the dimensions the pipeline wants you to confirm
  confirm_dimensions([{key, value_mm}])       type in the caliper number for each demand
  measure_from_photo(...)                     read a dimension off an image when you cannot
                                              reach the feature with calipers
  HUMAN DIMENSION-CONFIRM GATE. Fit-critical dims (bores, shaft diameters, bolt-circle
  diameters, anything that mates) should carry a TYPED caliper number, not a photo estimate.

  HONESTY TIERS for photo-derived dimensions — quote these, do not round them away:
    high    ±2%   well-registered dense reconstruction, confirmed ArUco scale, in-plane feature
    medium  ±5%   usable reconstruction or an off-plane / partially occluded feature
    low     ±10%  sparse coverage, weak scale evidence, or an inferred feature
  A ±5% dimension on a 20 mm bore is ±1 mm. That is not a press fit. Confirm it.

STEP 8 — BUILD THE PARAMETRIC MODEL
  reveng_build_parametric(session_id, strategy?) → run_id
  Turns the fitted primitives + confirmed dimensions into a real feature tree (sketches,
  extrudes, revolves, holes, patterns) — editable CAD, not a decorated mesh. If it cannot
  explain the shape it falls back to a mesh body and says so; it does not fake features.

STEP 9 — VERIFY
  get_reveng_deviation(session_id)     modeled-vs-scan deviation: where and by how much the
                                       parametric model departs from the reconstructed surface.
  describe_reveng_result(session_id)   doc_id, match_pct, strategy, mesh_fallback, provenance.
  Loop: look at the worst deviation regions → fix or add the feature (Lane A tools work here) →
  re-check. Stop when the deviation is inside the tolerance the PART needs, not an arbitrary %.

STEP 10 — DELIVER
  reveng_attach_result(session_id, part_ref)   register the model against a parts-hub entry so
                                               part_place can drop it into assemblies.
  export_reveng_bundle(session_id, dest)       images + scale + runs + result, one archive, for
                                               the ledger or a colleague.

OTHER ENTRY POINTS
  reveng_research_source(session_id, query?|part_number?|url?)
                                       pull specs/datasheets/images for a catalog part from the
                                       web and route straight to modelling — no scale or
                                       reconstruct stage.
  reveng_make_part_from_library(part_ref, part_number?)
                                       one call for a metadata-only parts-library entry:
                                       seed session → research → build parametric → attach.
                                       Also the Parts Library "Generate 3D model" button.

MISSIONS (run the whole lane as a supervised DAG with the gates built in)
  tmpl-reveng-scan          photos → scale gate → reconstruct → fit → parametric gate → deliver
  tmpl-reveng-datasheet     ingest → MPN fast-path → dim gate → model → deliver
  tmpl-reveng-single-photo  single-photo tier → review gate → optional mesh export

REQUIREMENTS / AVAILABILITY — LANE B ONLY
  Lane A needs nothing. Lane B drives external reconstruction programs that are NOT bundled and
  are ABSENT in a stock install. They are discovered at call time from env vars (or PATH):
    COLMAP_BIN     sparse SfM (COLMAP). Also found as `colmap` on PATH. Without it: no sparse,
                   therefore no dense and no splat.
    OPENMVS_BIN    dense point cloud + mesh (OpenMVS). AGPL-3.0 — install it yourself; we run it
                   as a subprocess only and never bundle or link it.
    BRUSH_BIN      3D Gaussian Splatting trainer + mesh export (Brush).
    MESHROOM_BIN   an existing Meshroom install, used by the legacy photogrammetry path.
    VGGT_WEIGHTS   feed-forward pose/geometry fallback for hard image sets.
    TRIPOSR_WEIGHTS / TRELLIS_WEIGHTS   single-photo tier (approximate — see STEP 4).
  reveng_status() reports exactly what resolved, and the Reverse Engineering tab's capability
  panel shows the same thing with a fix-it hint per missing artifact. When a backend is missing
  the tools return an honest `backend_not_provisioned` envelope carrying a `hint` — surface that
  hint verbatim to the user instead of a silently degraded mesh or a vague failure. Treat it as
  a terminal, actionable condition (the user must install something), not a transient error.

LANE B CAVEATS — BE HONEST ABOUT THESE
  * Scale is the whole ballgame. A mat printed at 97% gives you CAD that is wrong by 3%
    everywhere, and nothing downstream can detect it.
  * Photogrammetry cannot see what the camera cannot see: internal cavities, blind bores past
    their opening, and under-cuts come out wrong or missing. Measure those by hand.
  * Shiny, transparent, black and featureless surfaces reconstruct poorly. Matte them.
  * Threads are never recovered as real threads — recover the major diameter and pitch by
    measurement/identification and author the thread feature deliberately.
  * A single-photo (TripoSR/TRELLIS) result is a picture of a shape, not a measurement.
  * Synthetic/test-pattern frames are flagged; a session built on them is a demo, not a part.


================================================================================================
TOOL QUICK-REF — EVERY TOOL THIS GUIDE NAMES
================================================================================================

LANE A (files / docs — nothing to install)
  import_part(path)                    open a STEP/IGES/BREP/STL/OBJ as a reference doc.
  reverse_engineer(source)             the one-call path: import + recognize + rebuild + verify.
  compare_parts(doc_a, doc_b)          boolean match verdict; doc_a is the reference, world-frame.
  convert_face_to_sketch(face)         trace one planar face's wires into a NEW sketch on it.
  project_into_sketch(sketch, refs)    append projected wires to an EXISTING sketch (one-shot).
  add_features(ops)                    hand-model a residual, or apply a recognizer candidate.
  describe_part() / hover(sid)         read the geometry as text (cad_guide('gsl')).
  verify_battery(doc_id)               the done gate before you report a rebuild finished.

LANE B — READ (never mutates a session; safe to call any time)
  reveng_status()                      capabilities + provisioned backends. CALL THIS FIRST.
  list_reveng_sessions()               every session in the store.
  get_reveng_session(session_id)       the full aicad.reveng_session/1 manifest.
  get_reveng_coverage(session_id)      orbit coverage, blur flags, good/partial/insufficient.
  list_reveng_dims(session_id)         the dimensions still awaiting a confirmed value.
  get_reveng_deviation(session_id)     modeled-vs-scan deviation, worst regions first.
  describe_reveng_result(session_id)   doc_id, match_pct, strategy, mesh_fallback, provenance.

LANE B — WRITE (pipeline order)
  reveng_start_session(name?, part_ref?)          → session_id; every other call takes it.
  reveng_print_scale_mat(size)                    ArUco mat PDF — print at 100%, tape it flat.
  reveng_add_images(session_id, paths|dir)        stills from webcam / phone / camera.
  reveng_add_video(session_id, path, max_frames?) a slow orbit video, decimated to frames.
  reveng_ingest_datasheet(session_id, path)       PDF/image datasheet → tables + crops.
  reveng_reconstruct(session_id, quality, ...)    sparse → dense | splat → cleanup.
  reveng_single_photo(session_id, tier)           APPROXIMATE tier — preview shape only.
  reveng_set_scale(session_id, method, params)    aruco_mat | two_point | known_dim → SCALE GATE.
  reveng_fit_primitives(session_id)               RANSAC planes/cylinders/spheres/cones + roles.
  reveng_build_parametric(session_id, strategy?)  fits + confirmed dims → editable feature tree.
  reveng_attach_result(session_id, part_ref)      register the model on a parts-hub entry.

DIMENSION REVIEW (shared with intake)
  confirm_dimensions([{key, value_mm}])           type in the caliper numbers → DIMENSION GATE.
  measure_from_photo(photo_id, px_a, px_b, ...)   read a dimension off an image when a caliper
                                                  cannot reach the feature.

EXTERNAL / CATALOG ENTRY POINTS
  import_scan_mesh(session_id, path, scale_mm?)   you already own a scan mesh — skip to fitting.
  export_reveng_bundle(session_id, dest)          images + scale + runs + result, one archive.
  reveng_research_source(session_id, query?|part_number?|url?)
                                                  web specs/datasheet/images → straight to model.
  reveng_make_part_from_library(part_ref, part_number?)
                                                  seed → research → build parametric → attach.

LEGACY FALLBACK — NOT THE PRIMARY CAPTURE PATH
  start_capture_session(port?, ...) / submit_part_photos(...)
                                                  the phone-QR HTTP upload page. Use it only to
                                                  get phone photos onto this machine, then feed
                                                  the files to reveng_add_images and drive the
                                                  session with the reveng_* tools above.

ROLE / HANDOFF VIEW
  The three-role RE pipeline (re_modeler → re_datasheet_reader → re_geometry_verifier), its
  handoff contract and the verifier's verdict thresholds are NOT in this guide. They live in
  the companion file services/agent/prompts/shared/cheatsheets/reveng.md — read that path
  directly; no cad_guide topic serves it. This guide is the MECHANICS, that sheet is WHO does
  what and when a rebuild is allowed to pass.
  Separately, cad_guide('reveng') is the Reverse Engineering GUI-workspace walkthrough (the
  tab's Source → Scale → Reconstruct → Model → Review → Deliver stages) — a different document
  from both of these, not a route to the role sheet.
```

