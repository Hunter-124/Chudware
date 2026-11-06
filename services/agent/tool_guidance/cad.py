"""tool_guidance.cad — curated options for the cad tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "scad_status": {"prompt_note": "Check the real renderer and isolation prerequisites; see cad_guide('scad')."},
    "scad_inspect": {"prompt_note": "Inspect typed SCAD controls without rendering; lengths are canonical mm. See cad_guide('scad')."},
    "scad_convert": {"prompt_note": "Atomic managed native/SCAD projection only; changed source and unsupported graphs refuse. See cad_guide('scad')."},
    "part_library_ingest": {"prompt_note": "Register a rights-aware local manifest; source replacement requires explicit authorization. See cad_guide('parts')."},
    "part_library_sync": {"prompt_note": "Explicit HTTPS manifest sync requires a pinned SHA-256. See cad_guide('parts')."},
    "part_acquire": {"prompt_note": "Verify cached geometry or explicitly acquire it; offline=True forbids fetching. See cad_guide('parts')."},
    "add_mesh_reference": {
        "prompt_note": (
            "Embed a PLY, STL or OBJ as a persistent non-solid reference in doc_id. "
            "The transform maps source units to mm; mark uncertain geometry approximate."
        ),
    },
    "list_mesh_references": {
        "prompt_note": (
            "List the document's embedded mesh references, including their IDs, placement "
            "and bounds in mm, before updating, measuring or removing one."
        ),
    },
    "update_mesh_reference": {
        "prompt_note": (
            "Use the reference id and doc_id to change its transform, visibility, "
            "opacity, color or name; omitted fields remain unchanged."
        ),
    },
    "remove_mesh_reference": {
        "prompt_note": (
            "Remove the reference identified by doc_id and id without changing solid features."
        ),
    },
    "reveng_attach_reference": {
        "prompt_note": (
            "Embed a reconstruction from session_id in an existing doc_id as a non-solid "
            "reference. Confirm scale first, or explicitly set approximate=True when the "
            "reference is approximate; the reconstruction transform is applied once."
        ),
    },
    "measure_mesh_reference": {
        "prompt_note": (
            "Measure sampled deviation in mm from an embedded mesh reference to the "
            "part's B-rep using doc_id and reference id. n_samples controls sampling; "
            "alignment defaults to world. Sampled deviation is not an exact comparison."
        ),
    },
    "inspect_native_files": {
        "prompt_note": (
            "Inventory .step+ metadata without import/open/compile/solve. Pass absolute "
            "project-root paths and a NEW .json output_path for large inventories; full "
            "results live there, not in the bounded chat summary. document_hash is native "
            "canonical FNV, not SHA256. Unsupported dependencies and failures are evidence, "
            "not permission to unzip or hand-edit CAD containers."
        ),
    },
    "read_native_file": {
        "prompt_note": (
            "Read a saved .step+ tree (features with params/refs, components, mates, "
            "materials, appearances, meta) without opening it — use this, never unzip/jq. "
            "Narrow with sections=[...] / feature_ids / component_ids; embedded BREP and "
            "other long strings come back as '<N chars omitted>'. Read-only: to change the "
            "file, import_part it and edit the document."
        ),
    },
    "relocate_native_files": {
        "prompt_note": (
            "Metadata-only COPY via the native serializer, not a geometry operation. "
            "Supply plan_path to JSON containing entries (absolute path/destination, optional "
            "document name/component_names) and an absolute old-to-new path_map. Inspect "
            "first; never rename feature labels or rebuild/solve to organize files. "
            "Sources remain unchanged and destination overwrites are refused. Relative "
            "references, including unmapped dependencies, are rebased. Check the reply's "
            "missing required direct-reference count and preview; later-batch mappings may "
            "still be missing, and direct availability is not transitive closure. The unique "
            "result JSON beside the plan records every native outcome; partial failure may "
            "mean some copies exist. Read it before retrying; report failure does not prove "
            "that no native files were written."
        ),
    },
    "check_geometry_health": {
        "prompt_note": (
            "Is this solid actually sound? Reports watertight/manifold plus "
            "the COUNTS AND SIDS that justify it (free edges, non-manifold "
            "edges, shells, solids, sliver faces, orientation). Run it "
            "before an expensive FEA mesh, a CAM toolpath or a STEP/STL "
            "export. `self_intersection` is a REAL kernel check now "
            "(BOPAlgo_CheckerSI, 20 s per-body budget): a body it flags is "
            "NOT sound; checked:false + a reason means it could not run. "
            "Body-vs-body overlap is still check_interference. "
            "`volume_mm3` is null for a body with no solid. "
            "repair/fill_holes/fix_normals/fix_winding are REFUSED here: for "
            "a body YOUR features made, fix the feature using the sids it "
            "just listed; for an IMPORTED body that arrived broken, call "
            "heal_body (a recorded ShapeFix repair)."
        ),
    },
    "inspect_model_integrity": {
        "prompt_note": (
            "Read-only census of EVERY solid, including disconnected children inside "
            "assembly components. Inspect complete, source_failures, pair_failures, "
            "isolated_solids and nearest; ok only means the query ran. proximity_mm is "
            "a search radius, not approved clearance; a positive gap is not contact. "
            "Increase pair_budget_s (maximum60) when exact checks time out, never "
            "treat unknown evidence as clean. Geometric islands, overlaps and fixed "
            "flags do not establish fastening, mechanical capture or a load path."
        ),
    },
    "check_model_history": {
        "prompt_note": (
            "Read-only advisory history inspection, not geometry or solver certification. "
            "Inspect findings, safe_fixes and blocked_fixes before cleanup. Repeated "
            "dimensions or placements do not prove a relation, pattern or redundant "
            "feature; preserve authored design intent."
        ),
    },
    "simplify_part": {
        "prompt_note": (
            "Apply only still-eligible native history cleanup candidates; the engine "
            "rechecks references, external consumers, geometry and revision atomically. "
            "Blocked or inconclusive candidates must remain. Keep editable sketches, "
            "relations and critical dimensions; do not replace the history with an "
            "import or delete features merely to reduce its length. A successful "
            "cleanup is one undo step."
        ),
    },
    "heal_body": {
        "prompt_note": (
            "The repair check_geometry_health refuses — use it ONLY on a body "
            "that arrived broken from an import (bad_orientation_of_subshape, "
            "not_closed, free_edge, negative volume). It appends a RECORDED "
            "`heal` feature, so never 'repair' geometry with a BREP/STL "
            "round-trip or a self-boolean instead. `target` is a feature "
            "id/name or a 1-based body number; omit for a single-body part. "
            "A bare call runs ShapeFix and clears most imports; when it does "
            "not, climb ONE rung per call and read the evidence: "
            "sew_tolerance_mm (stored edge sharing is wrong), then "
            "fix_orientation:true (flag-only, keeps every surface exactly), "
            "then rebuild_solid:true (+unify_faces) which is REQUIRED when the "
            "returned orientation.conflicts > 0 — that graph is self-"
            "contradictory and no flag repair can satisfy it. Raise "
            "tolerance_mm at most one decade at a time; above the part's real "
            "feature size it merges geometry and gets REFUSED. A heal that "
            "would cost more than max_volume_change_pct (0.5%) of the "
            "material FAILS and leaves the body untouched — read "
            "material_change_pct and bad_edges_before/after, not just `fixed`. "
            "A defect your OWN features caused must be fixed at the feature; "
            "healing over it returns on the next parameter edit."
        ),
    },
    "activate_configuration": {
        "prompt_note": (
            "add_configuration does NOT switch the variant in — call this after "
            "tailoring it; confirm the switch via the returned body count/volume."
        ),
    },
    "add_configuration": {
        "prompt_note": (
            "Creates the variant but never activates it — follow with "
            "set_config_override / set_config_suppression, then activate_configuration."
        ),
    },
    "add_balloon": {
        "prompt_note": (
            "Link item to a drawing_bom row, then anchor a/pos from review_drawing "
            "sheet mm; re-review so the leader and balloon do not obscure geometry."
        ),
    },
    "add_chamfer_dimension": {
        "prompt_note": (
            "Create a semantic chamfer callout from the ledger's distance/angle; use "
            "review_drawing sheet-mm anchors and include it in audit_drawing spec coverage."
        ),
    },
    "add_datum": {
        "prompt_note": (
            "Read feature/label points from review_drawing first (sheet mm); declare "
            "datums before any add_gdt frame that references them."
        ),
    },
    "add_datum_target": {
        "prompt_note": (
            "Use for a controlled point/line/area target (not a whole datum feature); "
            "read a/pos from review_drawing and reference the target honestly in GD&T."
        ),
    },
    "add_detail_view": {
        "prompt_note": (
            "cx,cy,radius are the PARENT view's sheet mm — read them from "
            "review_drawing; for a cut through the whole part use add_section_view."
        ),
    },
    "add_dimension": {
        "prompt_note": (
            "Shop drawings should start make_drawing(model_items=False); place each "
            "ledger-driven dimension explicitly with tol_type/tol_upper/tol_lower at "
            "creation. view_id must name an existing view (make_drawing's view map or "
            "add_drawing_view); read sizes from measure_part/describe_part. "
            "dtype='note' takes one anchor plus required text='...'."
        ),
    },
    "add_drawing_view": {
        "prompt_note": (
            "Add one base/projected view to a make_drawing doc; x/y are sheet mm. "
            "Set show_hidden deliberately and use orientation controls only when the "
            "canonical directions do not expose the specified feature."
        ),
    },
    "add_features": {
        "prompt_note": (
            "Batch related ops in ONE call. For existing features use minimal edit_feature "
            "params/refs patches (cad_guide('edit_part')), preserving untouched values and "
            "requerying topology after geometry changes. Read the completed reply's interpreted, "
            "per-feature status, changes, overlap and attachment warnings. A status=running "
            "reply with job_id is a handoff, not final body_count; wait_job and inspect its result. "
            "For costly edits from Eval, use background=True and print job_id immediately; "
            "wait_job must be a direct call outside Eval. Cancellation of Eval is not proof "
            "the mutation stopped: recover its job or inspect the current revision before retrying. "
            "Blind cuts start along the sketch normal; params.flip reverses it. Only a "
            "simple blind cut without explicit direction/flip can retry the other side, "
            "and only when the first removes no material. A cut into the wrong side "
            "does not auto-correct. Display volume/deltas are approximate; missing "
            "revision-correct baselines omit deltas. bore_faces_delta counts faces, not "
            "holes; body_count is physical solids and logical_member_count compiled entries. "
            "sketch_fillet radius is driving and clamps to the valid corner range "
            "(op_reports requested_radius/applied_radius); clamped expressions retain the "
            "original inside min/max bounds snapshotted from the corner, which later geometry "
            "changes can invalidate. Conflicts reject atomically. Open rib/sweep "
            "paths are intentional; a revolve may close to the axis. "
            "Sketches: relations first (never `fix`), then only critical dimensions; "
            "sketch_probe before a driving dimension; auto_define (default on, "
            "auto_define=false opts out) fills the rest as auto constraints "
            "(authored_dimensions vs auto_dimensions). Use origin/axis.h/axis.v refs, "
            "construction sketch_convert references, true arcs/slots and symmetric "
            "centerlines; check_sketch (dof 0, not pinned_only) before extruding. "
            "op rename relabels a FEATURE by id; no op renames the part — name it in "
            "new_part(name=…) and its file in save_part(path=…)."
        ),
    },
    "add_gdt": {
        "prompt_note": (
            "Declare add_datum/add_datum_target references first; row2 creates a "
            "composite frame and datum/material modifiers must match the ledger. "
            "Re-check placement with review_drawing and coverage with audit_drawing."
        ),
    },
    "add_hole_callout": {
        "prompt_note": (
            "Use a=[x,y] in sheet mm at or near a projected circle or its centre; "
            "model SIDs are not supported. Specify simple/tapped/counterbore/"
            "countersink semantics, full thread where applicable, count and THRU/depth "
            "from the ledger; retain view_id and annotation_id for audit coverage."
        ),
    },
    "add_ordinate_dimensions": {
        "prompt_note": (
            "Use a shared datum origin for patterns/locations that must not accumulate "
            "chain error; every ordinate value and tolerance must trace to the ledger."
        ),
    },
    "add_section_view": {
        "prompt_note": (
            "Use axis for a centre cut or plane/offset/depth/multipoint for a specified "
            "cut; sections must expose hidden bores, threads, pockets or welds that need "
            "controlled annotations."
        ),
    },
    "add_surface_finish": {
        "prompt_note": (
            "a/pos are sheet mm from review_drawing; ra is a string (\"1.6\"). One "
            "symbol per call — review_drawing again to check placement."
        ),
    },
    "add_weld_symbol": {
        "prompt_note": (
            "Place the structured weld requirement (type/size/side plus intermittent "
            "length/pitch, contour/finish, field/all-around, tail or full penetration) "
            "from the ledger; never replace it with a free note."
        ),
    },
    "add_drawing_sheet": {
        "prompt_note": (
            "Add a controlled sheet to an existing drawing package, then "
            "set_active_drawing_sheet before adding its views/tables/annotations."
        ),
    },
    "arrange_drawing": {
        "prompt_note": (
            "Re-lay the sheet (projection third|first, standard scale, paper size) instead "
            "of hand-moving views; dimensions and section cuts follow the views."
        ),
    },
    "add_drawing_table": {
        "prompt_note": (
            "Place kind=bom|revision|holes|notes|cutlist on the active sheet; use "
            "auto-population where supported and verify row content plus layout."
        ),
    },
    "asm_add_component": {
        "prompt_note": (
            "Places a FILE — save_part each part first; the FIRST component is fixed "
            "by default. Returns the 'k<N>' id that asm_mate refs use."
        ),
    },
    "asm_mate": {
        "prompt_note": (
            "Standard seat: concentric (shank↔bore) + coincident (head↔plate); each "
            "query must resolve to ONE source-part face/edge. For planar coincident, "
            "align='aligned' requires opposite outward normals; parallel aligned requires "
            "the same direction. Planar distance obeys normal · (point_b − point_a) = "
            "sign × value, aligned=+1 and anti=-1; normal is the first planar reference's, "
            "else the second's. Inspect returned directions, residual/tolerance, new mate "
            "status and total/omitted counts. Unresolved source-frame candidates are "
            "suggestions, never automatic remaps; see cad_guide('assembly')."
        ),
    },
    "assembly_plan_review": {
        "prompt_note": (
            "Run at PLANNING time, before a large build — it advises subassembly "
            "grouping; deep products should be mated subassemblies, not one giant file."
        ),
    },
    "audit_drawing": {
        "prompt_note": (
            "Release gate after current review_drawing: doc_id is the SOURCE part/assembly "
            "and drawing_id its DRAWING doc. Pair view_id with annotation_id. Pass real "
            "manufacturing requirements, or the unchanged mfg_plan_get(view='audit_spec') "
            "result; an empty spec is refused. Release requires verdict=release_ready with "
            "missing_critical empty. Draft export does not require a release audit."
        ),
    },
    "cad_graph": {
        "prompt_note": (
            "blast before editing a shared dimension (what breaks downstream), tree for "
            "assembly structure; project_dir is the shared working folder."
        ),
    },
    "cad_graph_link": {
        "prompt_note": (
            "Record a relationship the extractor can't see (e.g. two parts sharing a "
            "bore diameter); persisted as origin='ai' in the project overlay."
        ),
    },
    "cad_guide": {
        "prompt_note": (
            "Omit topic for the compact topic index. Request the narrow feature topic "
            "('hole', 'sweep', 'sketch'), 'edit_part' for existing native files, "
            "'feature_history' for insertion/reorder/rollback, or the needed domain. "
            "2D sketch operations such as 'sketch_fully_define' resolve to 'sketch'. "
            "Hyphens and underscores are interchangeable. 'overview' and 'ops' contain "
            "the full cheatsheet; 'add_features' is the complete mutation reference. "
            "The reply is plain text; Eval's details.xdev.inner is already decoded. "
            "A truncated text reply includes its complete-result path; the JSON file "
            "decodes to the original string."
        ),
    },
    "cad_skill": {
        "prompt_note": (
            "Fetches a pack listed in the [SKILLS] catalog by name — the full "
            "procedure, not the one-line summary in your role card. Call it BEFORE "
            "the first tool of that workflow (cad_skill(name=\"assembly-merge\") "
            "before import_part on foreign CAD, cad_skill(name=\"simulate\") before "
            "an FEA release run). path=\"<file>\" reads a resource inside the pack. "
            "The reply is plain text; use Eval's already-decoded details.xdev.inner directly."
        ),
    },
    "check_feature_overlap": {
        "prompt_note": (
            "add_features runs a subset per batch; re-scan the tree or features=[...] here. "
            "pairs are raw pre-boolean tool intersections, not measured material removal; "
            "legacy boss_swallowed does not prove material loss. dead_features means no "
            "surviving owned faces, not why. Read complete/unavailable_features/failed_pairs "
            "and inspect actual geometry. Separate bodies → check_interference."
        ),
    },
    "check_interference": {
        "prompt_note": (
            "Solve mates first on an assembly so placements are real; intra-part tool "
            "overlap → check_feature_overlap. Read clear for the verdict: ok=true only "
            "means the check completed. Read clearance_status and clearance_tolerance_mm; "
            "within_tolerance (±0.000001 mm) is not a definite pass or violation."
        ),
    },
    "check_metadata": {
        "prompt_note": (
            "Read this before verify_battery to pick check names and see cost/platform "
            "classes — it is the registry of everything the battery can run."
        ),
    },
    "check_sketch": {
        "prompt_note": (
            "Before consuming a profile require closed, dof == 0, pinned_only == false, "
            "no conflicts/skipped constraints. Blanket fix is not design intent; fixed "
            "external references are legitimate. Read findings/free variables, add "
            "relations then driving dimensions, sketch_fully_define, and re-check."
        ),
    },
    "check_sketch3d": {
        "prompt_note": (
            "sketch3d twin of check_sketch; a sweep-spine path needn't reach dof 0, but "
            "pinned paths survive edits. wires[k] maps to the sweep ref."
        ),
    },
    "check_symmetry": {
        "prompt_note": (
            "Checks a body against a datum plane or custom origin/normal, optionally shifted "
            "with offset; Boolean failure is an error, not an asymmetry verdict."
        ),
    },
    "close_part": {
        "prompt_note": (
            "save_part deliverables BEFORE closing — unsaved AI-owned docs return "
            "unsaved_changes and stay open; discard=True is for scratch only."
        ),
    },
    "code_check": {
        "prompt_note": (
            "doc_path must be an aicad.structure/1 JSON doc; post_size/connection_type "
            "run standalone without it. Preliminary check — not sealed engineering."
        ),
        "timeout_s": 300,
    },
    "contact_sheet": {
        "prompt_note": (
            "Same planner 2x2 grid as render_part without a view; pass views=[...] only "
            "when you need specific named angles (a name that is not a view is an error "
            "listing the valid ones). section=\"top\"|{plane,offset}|{origin,normal} cuts "
            "every tile on that plane. Failed tiles are reported in the caption."
        ),
    },
    "design_review": {
        "prompt_note": (
            "One-call narrated battery (interference→dfm→layout_coverage); scope with "
            "checks=[...]. Machine-readable {scores,findings} twin → verify_battery."
        ),
        "timeout_s": 300,
    },
    "doc_diff_tool": {
        "prompt_note": (
            "Both args are full-document JSON STRINGS (e.g. get_part output), not "
            "doc_ids; µm-level float noise is suppressed so unchanged rebuilds diff empty."
        ),
    },
    "doc_sketch_contact_sheet": {
        "prompt_note": (
            "One image of the doc PLUS every in-progress sketch's health — the "
            "pre-commit check that catches under-defined sketches before an extrude fails."
        ),
    },
    "draft_analysis": {
        "prompt_note": (
            "Mesh-based absolute draft threshold on the last body only; it does not check "
            "undercuts or prove moldability. Flagged faces include centroid and area."
        ),
    },
    "engine_profile": {
        "prompt_note": (
            "Perf triage for THIS engine process: start → work → stop/report; "
            "'contention' is the multi-agent g_mtx bottleneck the CPU sampler can't see."
        ),
    },
    "engine_profile_tree": {
        "prompt_note": (
            "Scoped profile read — start at depth=1/top=8, drill with path=... instead "
            "of ingesting the whole report; source='phases' needs no start."
        ),
    },
    "drawing_bom": {
        "prompt_note": (
            "Read live assembly item/name/quantity/source rows before add_balloon or "
            "a BOM table; preserve item-number linkage instead of retyping a prose list."
        ),
    },
    "edit_drawing_annotation": {
        "prompt_note": (
            "Patch/delete by view_id + annotation_id from review_drawing. To remove "
            "displayed tolerance, pass the tol_type argument as 'none'; null patches "
            "are refused atomically. Re-review after changes and re-audit before release."
        ),
    },
    "export_drawing": {
        "prompt_note": (
            "Review before export. A requested non-release draft may be exported without "
            "audit; label it as a draft. For release, audit_drawing against actual "
            "requirements must return release_ready with missing_critical empty; empty "
            "specs refuse. PDF supports companions=['dxf','svg']; sheet='all' exports "
            "the package. Relative paths use the selected project root; absolute paths "
            "are accepted except credential/key destinations, including companions."
        ),
    },
    "export_part": {
        "prompt_note": (
            "To save a standalone native drawing use fmt='draw+', doc_id=<drawing_id>, "
            "path='sheet.draw+'; save_part takes its open source part/assembly instead. "
            "fmt='step' is machine-shop exchange; fmt='step+' is the editable native ZIP. "
            "Assembly exports resolve nested children or refuse missing paths. STL "
            "validation_stages reports facet checks and a skipped B-rep round-trip, "
            "not an independent mesh reimport."
        ),
    },
    "export_flat_dxf": {
        "prompt_note": (
            "Add sm_flat_pattern first, then export DXF/DWG with OUTLINE, BEND and CUTS "
            "layers (PDF is unlayered). Model reliefs as editable through-cuts; legacy "
            "display-only caches without flat metadata are refused."
        ),
    },
    "fit_cylinder_axis": {
        "prompt_note": (
            "Least-squares bore/shaft axis around near=[x,y,z] — the way to find a hub "
            "bore or axle on a TESSELLATED import; judge rms_residual_mm against "
            "radius_mm before placing anything on the axis "
            "(cad_skill(name=\"assembly-merge\"))."
        ),
    },
    "get_job": {
        "prompt_note": (
            "Poll the job_id a backgrounded heavy call returned (add_features / "
            "run_fea_* / run_cfd_flow) — never re-issue the original call."
        ),
    },
    "wait_job": {
        "prompt_note": (
            "Block on a get_job/sim job_id until it finishes (timeout_s ≤ 900, default "
            "120) instead of sleep/ps loops or repeated get_job polls. still_running:true "
            "means call it again; waiting never cancels the job (sim_cancel does). Call it "
            "directly, never inside eval: the cell timeout counts the wait and can expire "
            "before the job finishes. Retrieve its result with get_job, then use a short eval."
        ),
        "timeout_s": 960,
    },
    "get_part": {
        "prompt_note": (
            "summary=True orients but elides values and refs. Use get_part(feature_ids=[...]) for "
            "complete selected feature records; recover any truncated result before use. "
            "Both forms put features at result['tree']['features']; ids feed edits. "
            "An active tree.rollback_after remains visible: later features exist but are not built."
        ),
    },
    "import_part": {
        "prompt_note": (
            "into=doc_id adds an import feature there; omit for a NEW independent doc "
            "(resume=False default), even if the source is open. resume=True reuses an "
            "open document or binds the existing native file for editing, preserving history. "
            "Use the returned doc_id and its feature ids. "
            ".sldprt prefers the SW tree; mesh → as_solid=True to sew a solid; an open mesh over "
            "50,000 triangles lands as a mesh reference (import_fidelity='mesh_reference'). "
            "Multi-body source: describe_part rosters the bodies, keep_bodies/"
            "exclude_bodies (ordinals or names) drop the ones you don't want. "
            "On a format/units error confirm the path/extension and the document units first."
        ),
        "timeout_s": 600,
    },
    "inspect_feature": {
        "prompt_note": (
            "Planner close-ups (hero/alt/alt2, a section for internal features) + whole-part "
            "context of one feature — only when render_part(target=...)'s grid is ambiguous. "
            "A panel that fails to render is listed in the caption; section numbers in it "
            "are for the plane the panel shows."
        ),
    },
    "inspect_part": {
        "prompt_note": (
            "Single close-up = render_part(target=..., view=\"auto\") plus framing knobs "
            "(zoom_factor, margin, cutaway); same target and view vocabulary. "
            "section=\"top\"|{plane,offset}|{origin,normal} cuts on a plane YOU name "
            "(datum planes pass through the world origin; give origin+normal through the "
            "feature); it cannot be combined with cutaway."
        ),
    },
    "inspect_section": {
        "prompt_note": (
            "Section properties cover the whole plane. A named datum can be shifted by "
            "offset, or pass origin and normal for a custom plane; target_center is refused."
        ),
    },
    "interface_check": {
        "prompt_note": (
            "Catches hole-pattern/coaxiality misalignment that passes check_interference "
            "— solve_mates first, run BOTH; fastener='M6' adds a pass-through check. Read "
            "`status` (pass|fail|skip) for the verdict: a misalignment keeps ok=true."
        ),
    },
    "assembly_dof": {
        "prompt_note": "Inspect remaining degrees of freedom and solve evidence; intended motion is not automatically a defect.",
    },
    "fastener_check": {
        "prompt_note": (
            "Inspects detected hardware and nearby bores. Distinguish measured candidate "
            "geometry from evaluated requirements; inspect not_evaluated results. "
            "Thread calculations require supplied requirements."
        ),
    },
    "tool_access_check": {
        "prompt_note": "Screens illustrative socket or hex-key envelopes. A modeled hit does not prove an actual tool is blocked; actual-tool access remains not_evaluated.",
    },
    "disassembly_check": {
        "prompt_note": "Tests bounded straight-line directions and reports blockers and a greedy removal order. Untested paths and rotations are not proven impossible.",
    },
    "mate_fit_check": {
        "prompt_note": (
            "Checks concentric mates and reports measured fit character and nearest ISO candidate. "
            "The candidate is not proof of the original designation or tolerance conformance; "
            "acceptance against an ISO fit requires explicit nominal_mm and fit. Ambiguity is not_evaluated."
        ),
    },
    "bom_consistency_check": {
        "prompt_note": (
            "Compare the assembly BOM with components, patterns, source metadata and detected "
            "hardware coverage. Undetected fasteners and repeated source lines are not proof "
            "of missing hardware or duplicate placements; inspect not_evaluated and source evidence."
        ),
    },
    "layout_plan": {
        "prompt_note": (
            "Intake-time, BEFORE add_features — records envelope/critical dims/interfaces "
            "as requirements so the layout_coverage gate can check them at sign-off."
        ),
    },
    "layout_plan_report": {
        "prompt_note": (
            "Read back the plan plus verification coverage (verified vs outstanding); "
            "the doc must have a layout_plan recorded first."
        ),
    },
    "list_artifacts": {
        "prompt_note": (
            "Newest-first artifact registry (reports/decks/workbooks); filter by "
            "run_id — an unknown run_id is count:0, not an error."
        ),
    },
    "list_configurations": {
        "prompt_note": (
            "See the variants and which is active before editing; build with "
            "add_configuration + set_config_override, switch with activate_configuration."
        ),
    },
    "list_drawing_sheets": {
        "prompt_note": (
            "Inspect sheet ids, names and active state before set_active_drawing_sheet "
            "or export_drawing(sheet='all'); never assume the first sheet is the package."
        ),
    },
    "list_parts": {
        "prompt_note": (
            "Open docs in this session; after a server restart this is empty — "
            "recover_parts reloads the autosaved docs (with fresh doc_ids)."
        ),
    },
    "list_standard_parts": {
        "prompt_note": (
            "Catalog browse for make_standard_part (family keys, sizes, lengths) — "
            "standard-before-custom: check here before modeling hardware by hand."
        ),
    },
    "list_world_bodies": {
        "prompt_note": (
            "World-placed bboxes per body/component — read right after solve_mates to "
            "confirm where everything landed; pair with check_interference for fit."
        ),
    },
    "look_around": {
        "prompt_note": (
            "Tiled close-ups of ONE target (planner set, or ranked=False for an even orbit) — "
            "only when render_part(target=...)'s grid leaves the shape ambiguous. "
            "n_views is 1-8; more is an error, not a clamp."
        ),
    },
    "make_drawing": {
        "prompt_note": (
            "Build geometry first, then start shop drawings with model_items=False and "
            "a real annotation plan. Returns drawing_doc_id + view map. Set geometric "
            "scale, not just title text; refine and review_drawing. Release requires "
            "audit_drawing against real requirements, but draft export does not."
        ),
        "timeout_s": 300,
    },
    "make_presentation": {
        "prompt_note": (
            "Load-bearing numbers must reference attached_results tool outputs — a bare "
            "numeral is a validation error; spec shape in cad_guide('deliverables')."
        ),
    },
    "make_report": {
        "prompt_note": (
            "Same spec envelope as make_presentation; format docx (default) or pdf. "
            "Provenance is enforced BEFORE any file is written."
        ),
    },
    "make_weldment_traveler": {
        "prompt_note": (
            "Controlled weld shop handoff (kind/template weldment_traveler): cut and "
            "joint lists, provenance-backed WPS values, fit-up/distortion controls, "
            "visual/NDT inspection, signoff; format docx or pdf."
        ),
    },
    "make_work_instruction": {
        "prompt_note": (
            "Controlled machining routing (kind/template work_instruction): every machined "
            "operation needs setup datum/WCS, fixturing, machine/tool, provenance-backed "
            "process values, and in-process inspection; format docx or pdf."
        ),
    },
    "make_standard_part": {
        "prompt_note": (
            "list_standard_parts first for family/size keys; makes the new doc CURRENT "
            "— save_part it before asm_add_component; the returned datum aids placement."
        ),
    },
    "make_workbook": {
        "prompt_note": (
            "Rows mix prose labels, provenance value cells and live formulas "
            "('=SUM(B2:B5)') that recompute on open; spec in cad_guide('deliverables')."
        ),
    },
    "measure": {
        "prompt_note": (
            "Colour-coded multi-element probe returning image + caption; for a pure "
            "text distance/angle reading without a render use measure_between."
        ),
    },
    "measure_between": {
        "prompt_note": (
            "Each side must resolve to exactly one element, including after pick/count/index. "
            "Cylinders report diameter; two cylinder axes report pitch as axis_distance."
        ),
    },
    "measure_part": {
        "prompt_note": (
            "Applied materials supply density; missing or unresolved material appears in "
            "warnings. density_g_cm3 is an override (steel 7.85, not 7850). body_id selects "
            "one physical solid. Inertia is g·mm² about the centre of mass, world-aligned."
        ),
    },
    "mfg_plan_check": {
        "prompt_note": (
            "Run only after mfg_plan_upsert, and again after geometry/manufacturing fixes; "
            "pass doc_id when geometry exists. not_evaluated is honest missing evidence, "
            "not a pass. Drawing release still requires audit_drawing separately."
        ),
    },
    "mfg_plan_get": {
        "prompt_note": (
            "Read the stable ledger plan by run_id/plan_id. view='audit_spec' returns the "
            "exact {requirements:[...]} object to pass unchanged to audit_drawing; "
            "view='work_instructions' projects the same controlled route for the shop."
        ),
    },
    "mfg_plan_render": {
        "prompt_note": (
            "Render an already-upserted plan as controlled json or md; validate first "
            "with mfg_plan_check and never treat the rendered artifact as a second "
            "editable source of manufacturing truth."
        ),
    },
    "mfg_plan_upsert": {
        "prompt_note": (
            "Persist one stable plan id before modeling; update it with a monotonic "
            "revision instead of plan_v2 siblings. Numeric critical rows need provenance "
            "(and derived_from/derivation when derived), then mfg_plan_check must run."
        ),
    },
    "new_assembly": {
        "prompt_note": (
            "Components come from FILES: build parts, save_part each, then "
            "new_assembly → asm_add_component → asm_mate → solve_mates. An open "
            "assembly of that name is RESUMED with its components (reused, "
            "component_count); fresh=True makes a separate empty one."
        ),
    },
    "new_part": {
        "prompt_note": (
            "Returns the new doc_id and makes it current — follow with add_features. "
            "ONE DESIGN IS ONE DOCUMENT: iterate on an existing part IN PLACE, never "
            "bump the name (holster_v2, bracket-rev4) — versions live in history "
            "(plm_timeline/plm_rollback), and a bumped name is resumed, not minted."
        ),
    },
    "preflight_ops": {
        "prompt_note": (
            "Dry-run on a shadow clone — never mutates the live doc; a 'fail' verdict "
            "is advisory and does NOT block add_features. Cheap insurance before big edits."
        ),
    },
    "query_geometry": {
        "prompt_note": (
            "Plain-word phrases work for faces AND edges; confirm a pick resolves before "
            "referencing it. {of:'face',sid:'...'} is accepted only when the live SID "
            "matches that kind; contradictory kinds and extra predicates are refused. "
            "Narrow ambiguous matches with pick/near/count/index."
        ),
    },
    "recover_parts": {
        "prompt_note": (
            "After a server restart reloads the LAST dropped session's autosaves with "
            "FRESH doc_ids (re-map your handles); all=True reaches the full history. "
            "BOUNDED to the newest 8 docs; `skipped` counts what stayed on disk. Raise "
            "limit= only if the part you want is not among them, because reopening a big "
            "pile leaves nothing able to resolve 'the part'."
        ),
    },
    "regen_diagnose": {
        "prompt_note": (
            "For a failed rebuild, pass doc_id or failed document_json; supply a known-good "
            "reference_json to compare geometry. A rebuild alone is unverified."
        ),
        "timeout_s": 300,
    },
    "regen_resolve": {
        "prompt_note": (
            "Second half of regen_diagnose: accept/edit/reject the fix card; 'edit' "
            "re-applies your edit_ops_json to the ORIGINAL broken document."
        ),
    },
    "render_animation": {
        "prompt_note": (
            "The moving deliverable — a .gif/.mp4 a review PLAYS, not a still: "
            "kind='turntable' orbits a part, 'explode' flies an assembly's components "
            "apart (factor), 'motion' films a SOLVED trajectory, 'fea' warps a solved "
            "vtu_path by field. A motion film takes exactly one drive: mate_id "
            "(+start/stop) sweeps a dimensional mate, steer={'components':[...],"
            "'axis':{'point':[..],'dir':[..]},'angle_deg':N} (+rear_steer) ramps a steer "
            "sweep 0->angle for an assembly with no drivable mate — same spec as "
            "steering_turning_radius — and poses=[{component:[16 numbers]},...] films an "
            "explicit frame list; collision samples are marked. save_path is a REQUIRED "
            "absolute .gif/.mp4 in the project; frames/fps set duration and frames_dir "
            "collects the PNG sequence for a multi-segment film. Needs ffmpeg; a single "
            "still is still render_part."
        ),
        "timeout_s": 900,
    },
    "render_document": {
        "prompt_note": (
            "Previews a saved artifact (pptx/docx/xlsx/pdf/…); office formats need "
            "LibreOffice on PATH else converter_missing — the artifact itself is untouched."
        ),
        "timeout_s": 180,
    },
    "render_part": {
        "prompt_note": (
            "Check numbers first; don't render after every mutation. One call = a 2x2 grid of "
            "the 4 most useful complementary shots (measured, SID-tagged, caption lists what "
            "each tile shows and faces no tile sees); target= the feature id to frame it "
            "(emphasize= is the same thing — pass one, not both); view= only for one "
            "specific single shot (\"auto\" = planner's best camera; an unknown name is an "
            "error). section=\"top\"|{plane,offset}|{origin,normal} cuts on a plane you name."
        ),
    },
    "render_sketch": {
        "prompt_note": (
            "Renders the SOLID context + sketch health caption — there is no isolated "
            "flat view; the sketch entities themselves → sketch_preview_text."
        ),
    },
    "render_study_setup": {
        "prompt_note": (
            "The fixture/load FIGURE an FEA report needs beside its stress plot: fixtures "
            "painted magenta, load faces violet, rest desaturated, plus ONE ARROW PER LOAD "
            "ENTRY (tip on that entry's face centroid, along its own force vector, scaled "
            "to its magnitude) and the resultant burned into a band at the bottom, so two "
            "cases on the same faces are different figures. fixtures/load are "
            "EXACTLY run_fea_static's dialect and are resolved by the same gates, so pass "
            "the identical arguments and draw this BEFORE the solve — a refusal here is "
            "the setup error the solver would have raised. Omit view to let the shot "
            "planner pick the camera that shows the most of those faces; save_path also "
            "writes the embeddable PNG."
        ),
    },
    "render_views": {
        "prompt_note": (
            "Planner set of 7 complementary views (or views=[...] for a fixed drawing set) "
            "— heavier than render_part's 4-tile grid; only when four views leave a face unseen."
        ),
    },
    "req_create": {
        "prompt_note": (
            "doc_id embeds the brief in the .step+, req_path writes a standalone "
            ".req.json; then add needs/requirements with req_update."
        ),
    },
    "req_report": {
        "prompt_note": (
            "markdown (default) is the read-only diff-stable view; format='json' "
            "returns the raw aicad.req/1 object for programmatic reads."
        ),
    },
    "req_status": {
        "prompt_note": (
            "Agents may NOT set user_confirmed or waived — verifier outcomes go to "
            "verified/failed with an evidence dict attached."
        ),
    },
    "req_update": {
        "prompt_note": (
            "add_need first, then add_requirement with need_id + kind + verify_by — the "
            "verifier names are what req_verify_by_id will later run."
        ),
    },
    "req_verify_by_id": {
        "prompt_note": (
            "Runs the requirement's verify_by verifiers and writes status+evidence "
            "back; confirm_dimensions/dfm_check/fit_report are skipped (unsupported in v1)."
        ),
    },
    "review_drawing": {
        "prompt_note": (
            "Inspect attached pixels plus sheet-mm JSON and clear layout/title/material "
            "warnings after the last mutation. layout_clean is not release certification; "
            "for release follow with audit_drawing against real requirements. A requested "
            "non-release draft may be exported without audit."
        ),
    },
    "save_part": {
        "prompt_note": (
            "Native editable part/assembly only (.step+ ZIP); not ISO STEP. A drawing id "
            "is refused: use export_part(fmt='draw+', doc_id=<drawing_id>, path=...) for "
            "a standalone drawing, or save its open source part to embed drawings. For "
            "external manufacturing also export_part(fmt='step', path='...step')."
        ),
    },
    "set_active_drawing_sheet": {
        "prompt_note": (
            "Select the destination sheet_id before adding views/tables/annotations; "
            "confirm with list_drawing_sheets and re-review the changed sheet."
        ),
    },
    "set_config_override": {
        "prompt_note": (
            "Edits one param in ONE config (base untouched); vector params take a list "
            "(transform translate:[x,y,z]); clear=True removes the override."
        ),
    },
    "set_config_suppression": {
        "prompt_note": (
            "Per-variant feature on/off without forking the tree; activate_configuration "
            "+ render_part to confirm the variant looks right."
        ),
    },
    "set_material": {
        # Keep document metadata/appearance distinct from explicit solve properties.
        "prompt_note": (
            "set_material(material='A36 steel'|'4140'|'6061-T6', body='b1'|None, "
            "doc_id=...) stores the registry material key AND matching appearance. "
            "An explicit {'color':[r,g,b],...} is appearance-only. There is no ops=[...] "
            "form. Mass/FEA still take their own material=/density input so each analysis "
            "case is explicit; run_cfd_flow takes fluid= and has no material argument."
        ),
    },
    "set_titleblock": {
        "prompt_note": (
            "Fill title/drawn_by/date/material/finish/scale/rev/sheet/part_number/"
            "company; review material must match the source and audit_drawing decides "
            "release completeness."
        ),
    },
    "set_view": {
        "prompt_note": (
            "Use review_drawing sheet mm to fix layout; x/y/scale move or resize, while "
            "show_hidden/orientation change the projection. Re-review and re-audit after."
        ),
    },
    "show_in_app": {
        "prompt_note": (
            "Daemon mode: the open app already mirrors every part live (reply live:true); "
            "it cannot switch the user's active tab. Legacy engine: writes the handoff file."
        ),
    },
    "sketch3d_add_constraints": {
        "prompt_note": (
            "Point handles: e3.p1/e3.p2/e3.pm, spline e3.pK, external vertex xN. "
            "Use tangent/equal/midpoint/collinear/concentric/coradial and plane-based "
            "on_plane/parallel_plane/normal_plane/symmetric; dimensions drive size. "
            "Avoid blanket fix pins; re-check check_sketch3d."
        ),
    },
    "sketch3d_add_curves": {
        "prompt_note": (
            "Arcs are THREE-POINT [start,mid,end]; endpoints within 1e-4 mm chain into "
            "wires automatically. Simple routes → sketch3d_path_from_points instead."
        ),
    },
    "sketch3d_as_sweep_guide": {
        "prompt_note": (
            "Returns the ready-to-paste sweep ref (path vs guides[] entry); a guide must "
            "be a DIFFERENT feature than the path, one guide per sweep."
        ),
    },
    "sketch3d_clear": {
        "prompt_note": (
            "Wipes entities but keeps the feature id, so downstream sweep refs survive "
            "(they fail until the sketch has a wire again)."
        ),
    },
    "sketch3d_get": {
        "prompt_note": (
            "Reads authored constraints + SOLVED entity coordinates (what the GUI "
            "shows) + health; use before referencing a wire in a sweep."
        ),
    },
    "sketch3d_list": {
        "prompt_note": (
            "All sketch3d features with wire/status/length summary — persistent doc "
            "features that save with the part."
        ),
    },
    "sketch3d_new": {
        "prompt_note": (
            "Needs a current part (new_part first); returns the feature id every other "
            "sketch3d_* tool takes — populate via sketch3d_add_curves."
        ),
    },
    "sketch3d_path_from_points": {
        "prompt_note": (
            "One-call route through world points: polyline = crisp bends, spline = "
            "smooth; close=True for a closed sweep path. Then sketch3d_pipe."
        ),
    },
    "sketch3d_pipe": {
        "prompt_note": (
            "'Run a 12 mm tube along this path' in one call — pass diameter_mm XOR "
            "profile_sketch; a zero-volume error names the pierce gap to fix."
        ),
    },
    "sketch3d_preview": {
        "prompt_note": (
            "Text-only 'see the path' (solved coords, wire refs, health) — the headless "
            "check before sweeping; 2D sketches → sketch_preview_text."
        ),
    },
    "sketch3d_ref_vertex": {
        "prompt_note": (
            "Mints a model-vertex point handle ('x1') usable in sketch3d_add_constraints "
            "refs; it tracks the vertex across rebuilds and warns if it goes stale."
        ),
    },
    "sketch3d_sample_at": {
        "prompt_note": (
            "Point + tangent at arc-length fraction t∈[0,1] of the SOLVED path — for "
            "placing profile planes or datums along a route."
        ),
    },
    "sketch3d_section_at": {
        "prompt_note": (
            "ENGINE LIMIT: the cut is always at the world ORIGIN — t only picks the "
            "plane orientation from the tangent; the on-path point → sketch3d_sample_at."
        ),
    },
    "sketch_preview_text": {
        "prompt_note": (
            "No-vision full text of a 2D sketch (entities/constraints/regions/DOF) — the "
            "readable alternative to render_sketch; 3D → sketch3d_preview."
        ),
    },
    "solve_mates": {
        "prompt_note": (
            "Assembly docs only ('not an assembly' on a part); asm_mate already solves "
            "by default — call after bulk edits, then list_world_bodies. Over- or "
            "under-constrained mates: review the mate list and run interface_check."
        ),
    },
    "stackup_analyze": {
        "prompt_note": (
            "tol is the ± HALF-tolerance and direction ±1 the chain sign; invented "
            "tolerances MUST set assumed=True so they surface in assumed_dims."
        ),
    },
    "suggest_dimension_scheme": {
        "prompt_note": (
            "Live authored sketch and process are required. Review proposed geometric "
            "relation rows first, apply them and re-plan from solver health before adding "
            "critical dimensions. Size mating bores with fit_calc."
        ),
    },
    "suggest_drawing_dimension_scheme": {
        "prompt_note": (
            "Plan explicit shop-drawing coverage from live authored doc_id + process; "
            "unavailable geometry is refused, not replaced by a fixture. It proposes a "
            "scheme but never places annotations. Rows marked placeable=false require a "
            "view and resolved sheet attachment before invoking the suggested tool."
        ),
    },
    "verify_battery": {
        "prompt_note": (
            "Deterministic checks for THIS doc; the default set includes feature_attachment "
            "(floating / overhanging / edge-touching features, each with its close-up call) "
            "and solid health. PASS covers only keys in scores; measurements are "
            "informational. Drawings, fits, loads, tolerances and manufacturability need "
            "their own checks."
        ),
    },
    "wall_thickness": {
        "prompt_note": (
            "While modelling printed parts, run this AND dfm_check. Inspect the default "
            "RED process-minimum heat map; fix each red region at its named face/feature "
            "or explicitly justify it before export/slicing. min_wall_mm defaults to the "
            "bound process/FDM profile; resolution is optional mm spacing; image=False "
            "returns JSON. Read the inscribed-sphere basis/spacing; null is unmeasured "
            "(no opposing wall found), never evidence of safety."
        ),
    },
}
