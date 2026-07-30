"""tool_guidance.cad — curated options for the cad tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "analyze_stl_geometry": {
        "prompt_note": (
            "Alias of check_geometry_health. Takes doc_id, not a file path "
            "— import_part the mesh first, then call this on the document."
        ),
    },
    "assess_mesh_quality": {
        "prompt_note": (
            "Alias of check_geometry_health. We are a B-rep kernel, so this "
            "reports SOLID topology health, not triangle quality."
        ),
    },
    "check_geometry_health": {
        "prompt_note": (
            "Is this solid actually sound? Reports watertight/manifold plus "
            "the COUNTS AND SIDS that justify it (free edges, non-manifold "
            "edges, shells, solids, sliver faces, orientation). Run it "
            "before an expensive FEA mesh, a CAM toolpath or a STEP/STL "
            "export. `self_intersection` is always checked:false — the "
            "kernel has no single-body test; use check_interference "
            "instead. `volume_mm3` is null for a body with no solid. "
            "repair/fill_holes/fix_normals/fix_winding are REFUSED: fix the "
            "feature that made the bad body, using the sids it just listed."
        ),
    },
    "design_audit": {
        "prompt_note": (
            "Alias of check_geometry_health — solid soundness only. It does "
            "NOT check DFM, tolerances or drawings; those are dfm_check, "
            "stackup_analyze and review_drawing."
        ),
    },
    "health_check": {
        "prompt_note": (
            "Alias of check_geometry_health for one body. For the "
            "whole-part review battery call design_review; for sketch "
            "state, sketch_health."
        ),
    },
    "is_watertight": {
        "prompt_note": (
            "Alias of check_geometry_health (same tool, trimesh's name). "
            "Returns the full report, not a bare bool — read `watertight` "
            "plus free_edges/non_manifold_edges for why."
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
    "add_datum": {
        "prompt_note": (
            "Read feature/label points from review_drawing first (sheet mm); declare "
            "datums before any add_gdt frame that references them."
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
            "For whole-view auto-dimensioning prefer make_drawing's model_items; use "
            "this for one-offs, snapping a,b to vertices read from review_drawing."
        ),
    },
    "add_drawing_view": {
        "prompt_note": (
            "One view per call on an existing drawing_doc_id (from make_drawing); the "
            "first/anchor view should be view_type='base', x,y are sheet mm."
        ),
    },
    "add_features": {
        "prompt_note": (
            "Batch related ops in ONE call and read `interpreted` + per-feature status; "
            "a heavy op may return {job_id} — poll get_job. Verify with render_part."
        ),
    },
    "add_gdt": {
        "prompt_note": (
            "Place from review_drawing sheet-mm coordinates; declare referenced datums "
            "with add_datum first, diameter=True adds the ⌀ zone."
        ),
    },
    "add_section_view": {
        "prompt_note": (
            "Cuts the parent view through its centre only — no sheet math needed; a "
            "magnified local region is add_detail_view instead."
        ),
    },
    "add_surface_finish": {
        "prompt_note": (
            "a/pos are sheet mm from review_drawing; ra is a string (\"1.6\"). One "
            "symbol per call — review_drawing again to check placement."
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
            "query must resolve to ONE face/edge — narrow with pick/near/count."
        ),
    },
    "assembly_plan_review": {
        "prompt_note": (
            "Run at PLANNING time, before a large build — it advises subassembly "
            "grouping; deep products should be mated subassemblies, not one giant file."
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
            "Call FIRST in any modeling session — 'overview' for the op cheatsheet, a "
            "feature name ('hole', 'sweep') for its exact param shape."
        ),
    },
    "check_feature_overlap": {
        "prompt_note": (
            "add_features already runs this per batch (overlap_warnings) — call to "
            "re-scan the whole tree or a features=[...] subset; bodies → check_interference."
        ),
    },
    "check_interference": {
        "prompt_note": (
            "Needs 2+ bodies — solve_mates first on an assembly so placements are real; "
            "overlap INSIDE one part's features → check_feature_overlap."
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
            "Require dof 0 before extruding/sweeping; 'under' lists what's loose, "
            "'over' means drop a conflicting/redundant constraint — then re-check."
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
            "Confirms an intended mirror really is symmetric (deviation_mm3); spell the "
            "plane as axis pair ('XY') or normal ('+z') — Z-up datums."
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
            "Fast multi-angle self-check after a mutation; full manufacturing "
            "6-ortho+iso grid → render_views, one feature's close-up → inspect_part."
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
            "Mold-release check along pull_dir — the SIGN matters (-Z ≠ +Z); flagged "
            "faces come back with centroid+area so you know where to add draft."
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
    "export_drawing": {
        "prompt_note": (
            "2D drawings only (doc_id = the drawing_doc_id) — 3D parts go through "
            "export_part; review_drawing first so you don't export a broken sheet."
        ),
    },
    "export_part": {
        "prompt_note": (
            "Interchange/mesh only — no feature tree or studies survive; to deliver an "
            "editable part use save_part (.step+). fmt='step+' routes to the native saver."
        ),
    },
    "get_job": {
        "prompt_note": (
            "Poll the job_id a backgrounded heavy call returned (add_features / "
            "run_fea_* / run_cfd_flow) — never re-issue the original call."
        ),
    },
    "get_part": {
        "prompt_note": (
            "summary=True on big/imported docs (elides BREP blobs); the full tree is "
            "for round-trip param editing. Feature ids here feed edits and config overrides."
        ),
    },
    "import_part": {
        "prompt_note": (
            "into=doc_id adds to the current part; omit for a new doc. .sldprt rebuilds "
            "the SW tree (needs local SolidWorks); mesh → as_solid=True to sew a solid."
        ),
        "timeout_s": 600,
    },
    "inspect_feature": {
        "prompt_note": (
            "One-call post-mutation feature check (render + dims + describe excerpt) — "
            "prefer over hand-chaining inspect_part + measure; whole-part → render_part."
        ),
    },
    "inspect_part": {
        "prompt_note": (
            "Close-up of ONE face/edge/feature via a plain-word target; orbit the same "
            "target → look_around, one-call with dims/describe → inspect_feature."
        ),
    },
    "inspect_section": {
        "prompt_note": (
            "The cut always passes through the ORIGIN on a reserved plane — returns "
            "area/centroid/Ixx/Iyy; an offset cut belongs in a drawing (add_section_view)."
        ),
    },
    "interface_check": {
        "prompt_note": (
            "Catches hole-pattern/coaxiality misalignment that passes check_interference "
            "— solve_mates first, run BOTH; fastener='M6' adds a pass-through check."
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
            "Orbiting close-ups of one feature when a single angle is ambiguous; for "
            "dims + describe excerpt in the same call use inspect_feature."
        ),
    },
    "make_drawing": {
        "prompt_note": (
            "Empty docs are rejected — build geometry first; returns drawing_doc_id + "
            "view map, then review_drawing and refine with the add_* drawing tools."
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
            "Each query must resolve to exactly ONE face/edge — narrow with pick/side "
            "or run query_geometry first; omit b_query for intrinsic area/length/radius."
        ),
    },
    "measure_part": {
        "prompt_note": (
            "Combined mass props over ALL bodies (per-body under mass.bodies[]); pass "
            "density_g_cm3 for non-steel; also sanity-checks the solid is closed/valid."
        ),
    },
    "new_assembly": {
        "prompt_note": (
            "Components come from FILES: build parts, save_part each, then "
            "new_assembly → asm_add_component → asm_mate → solve_mates."
        ),
    },
    "new_part": {
        "prompt_note": (
            "Returns the new doc_id and makes it current — follow with add_features; "
            "record a layout_plan first if the design has critical dims/interfaces."
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
            "Plain-word phrases work for faces AND edges ('the vertical edges', 'the top "
            "face') — confirm a pick resolves before referencing it in a feature; zero or "
            ">1 match on a slot needing one → narrow with pick/near/count/index. Fully "
            "open-ended explore → describe_part + hover instead."
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
    "redo": {
        "prompt_note": (
            "Usually denied for agents (undo_denied) — roll the doc back with "
            "plm_timeline then plm_rollback instead; if it does run, render_part "
            "to confirm the restored state."
        ),
    },
    "regen_diagnose": {
        "prompt_note": (
            "For a FAILED rebuild — pass the failed doc JSON (+ last-good reference_json "
            "for a drift check); record the verdict with regen_resolve. Needs a model provider."
        ),
        "timeout_s": 300,
    },
    "regen_resolve": {
        "prompt_note": (
            "Second half of regen_diagnose: accept/edit/reject the fix card; 'edit' "
            "re-applies your edit_ops_json to the ORIGINAL broken document."
        ),
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
            "Default self-review after every mutation (labelled iso); highlight= points "
            "at a target without reframing, target=/emphasize crops to a feature."
        ),
    },
    "render_sketch": {
        "prompt_note": (
            "Renders the SOLID context + sketch health caption — there is no isolated "
            "flat view; the sketch entities themselves → sketch_preview_text."
        ),
    },
    "render_views": {
        "prompt_note": (
            "Full 6-ortho+iso grid — catches back/bottom features a single iso hides; "
            "lighter 4-tile alternative → contact_sheet."
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
            "SEE the sheet before refining or exporting — fix overlaps with set_view, "
            "add add_dimension/add_gdt, then review again in a loop."
        ),
    },
    "save_part": {
        "prompt_note": (
            "PREFER for delivery — the only format keeping the editable tree + studies; "
            "save before close_part/session end (AI docs get no unsaved warning)."
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
        # The old note named "run_cfd_flow's material=" — run_cfd_flow
        # (cad_sim_tools.py:382) has NO material argument, so the guidance itself
        # taught a call that always fails. It also omitted set_material's own
        # accepted shape, which is what a live run probed for across FOUR retries
        # ({material}, {ops:[{op:set_material,material}]}, {...params}, {...name})
        # before one landed. State the real shape and the real solver entry point.
        "prompt_note": (
            "VISUAL appearance only — set_material(material='steel' | "
            "{'color':[r,g,b],'metallic':...,'roughness':...}, body='b1'|None, "
            "doc_id=None) sets render colour/finish and NOTHING else. There is no "
            "ops=[...] form. Simulation density/stiffness goes to run_fea_static / "
            "run_fea_modal's material= instead (a registry name or an E_mpa/nu dict); "
            "run_cfd_flow takes fluid='air'|'water' and has no material argument."
        ),
    },
    "set_titleblock": {
        "prompt_note": (
            "fields: title/drawn_by/date/material/finish/scale/rev/sheet/part_number/"
            "company; verify the filled block with review_drawing."
        ),
    },
    "set_view": {
        "prompt_note": (
            "Fix overlapping views reported by review_drawing — x,y are sheet mm; "
            "scale 1.0 = 1:1, 0.5 = 1:2, 2.0 = 2:1."
        ),
    },
    "show_in_app": {
        "prompt_note": (
            "Force-push the doc to the open desktop viewport; modeling tools already do "
            "this per change — a silent no-op when the app isn't running."
        ),
    },
    "sketch3d_add_constraints": {
        "prompt_note": (
            "Point handles are 'e3.p1'/'e3.p2'/'e3.pm'; tangent/symmetric/equal/midpoint "
            "are NOT in v1 (validator rejects). Re-check with check_sketch3d."
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
    "sketch3d_from_ledger": {
        "prompt_note": (
            "DEPRECATED migration shim for old session payloads — new work never needs "
            "this; sketch3d features persist with the document."
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
            "features that save with the part, no session store."
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
    "sketch3d_to_ledger": {
        "prompt_note": (
            "DEPRECATED — sketch3d features save with the document; this only reports "
            "them so old workflows don't dead-end."
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
            "by default — call after bulk edits, then list_world_bodies."
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
            "Dimension plan for an existing sketch; size mating bores with fit_calc "
            "FIRST (they come back needs_user_confirm). process drives the IT grade."
        ),
    },
    "undo": {
        "prompt_note": (
            "Usually denied for agents (undo_denied) — roll the doc back with "
            "plm_timeline then plm_rollback instead; if it does run, render_part "
            "to confirm the rolled-back state."
        ),
    },
    "verify_battery": {
        "prompt_note": (
            "The done-gate before declaring a design finished — omitted checks = all "
            "subsecond ones; check_metadata lists names/costs. Narrated verdict → design_review."
        ),
    },
    "wall_thickness": {
        "prompt_note": (
            "Headline is min_thickness_mm vs your process floor (~1 mm injection, "
            "~0.8 mm FDM); null means an open/sheet body — confirm a solid with measure_part."
        ),
    },
}
