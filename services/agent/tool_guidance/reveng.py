"""tool_guidance.reveng — curated options for the reverse-engineering tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "describe_reveng_result": {
        "prompt_note": (
            "Read `state`, not just match_pct: built+scored=false means the "
            "compare degraded, not a zero fit; state=running means poll again later."
        ),
    },
    "export_reveng_bundle": {
        "prompt_note": (
            "Archival/hand-off only — zips the whole session dir (images, mesh, "
            "fits); not part of the in-app flow. dest's parent dir is created."
        ),
    },
    "get_reveng_coverage": {
        "prompt_note": (
            "Check before reveng_reconstruct: capture the `missing` shot slots "
            "first, and always surface `method` — count_proxy is an estimate until a sparse model exists."
        ),
    },
    "get_reveng_deviation": {
        "prompt_note": (
            "Post-build quality check; ref_path is required (the brep_doc_id "
            "lane errors as not-yet-implemented). Bigger n_samples on dense scans."
        ),
        "timeout_s": 180,
    },
    "get_reveng_session": {
        "prompt_note": (
            "The polling read while a mission runs — session.run mirrors run "
            "progress. Find ids with list_reveng_sessions first."
        ),
    },
    "import_scan_mesh": {
        "prompt_note": (
            "Bypasses photogrammetry when a scan mesh already exists; pass "
            "scale_mm if known, else calibrate afterwards with reveng_set_scale."
        ),
    },
    "list_reveng_dims": {
        "prompt_note": (
            "Read before a dimension gate: rows arrive strongest-evidence-first "
            "and `key` is what the confirm ledger expects back. Empty list means nothing measured yet."
        ),
    },
    "list_reveng_sessions": {
        "prompt_note": (
            "Discovery read — find session_ids and statuses before calling any "
            "other reveng_* tool."
        ),
    },
    "reveng_add_images": {
        "prompt_note": (
            "Capture step for multi-view stills; pass paths OR dir (neither "
            "silently adds 0). Verify with get_reveng_coverage before reconstructing."
        ),
    },
    "reveng_add_video": {
        "prompt_note": (
            "Alternative to reveng_add_images for an orbit video — extracts "
            "blur-culled stills synchronously; drop max_frames on long clips."
        ),
        "timeout_s": 300,
    },
    "reveng_attach_result": {
        "prompt_note": (
            "Final step: makes the result placeable via part_place under "
            "part_ref. Run after reveng_build_parametric (or at least a mesh) exists."
        ),
    },
    "reveng_build_parametric": {
        "prompt_note": (
            "Run after reveng_fit_primitives (fits feed the hints). Single-photo "
            "sessions are refused unless strategy='override_approximate'; score with describe_reveng_result."
        ),
        "timeout_s": 300,
    },
    "reveng_fit_primitives": {
        "prompt_note": (
            "Needs a mesh first (reveng_reconstruct / reveng_single_photo / "
            "import_scan_mesh) and open3d provisioned; persists fits.json for reveng_build_parametric."
        ),
        "timeout_s": 300,
    },
    "reveng_gate_status": {
        "prompt_note": (
            "Poll a mission by session_id; a non-null `gate` means the run is "
            "suspended — answer it with reveng_resolve_gate, never by relaunching."
        ),
    },
    "reveng_ingest_datasheet": {
        "prompt_note": (
            "Datasheet lane; needs docling provisioned (backend_not_provisioned "
            "otherwise). Extracted tables surface as dim candidates in list_reveng_dims."
        ),
        "timeout_s": 300,
    },
    "reveng_make_part_from_library": {
        "prompt_note": (
            "One-call orchestrator for an un-modeled library part. On "
            "model_present=false with pending_dims, confirm the fit-critical dims — re-calling never forces them."
        ),
        "timeout_s": 300,
    },
    "reveng_print_scale_mat": {
        "prompt_note": (
            "Print before capture when no known dimension exists — the "
            "aruco_mat solver in reveng_set_scale defaults to exactly this mat's marker spec."
        ),
    },
    "reveng_reconstruct": {
        "prompt_note": (
            "Async: returns run_id immediately. Add images first, then poll "
            "get_reveng_session / reveng_gate_status — the run PAUSES at the scale-confirm gate."
        ),
    },
    "reveng_research_source": {
        "prompt_note": (
            "No-photo lane: needs part_number, query, or url (one required); "
            "offline returns honest ok:false. reveng_start_session(research=...) does this in one step."
        ),
        "timeout_s": 180,
    },
    "reveng_resolve_gate": {
        "prompt_note": (
            "decision has no default — pass 'approve' or 'reject' explicitly. "
            "Reject stops the run but leaves it resumable from the same checkpoint."
        ),
    },
    "reveng_set_scale": {
        "prompt_note": (
            "aruco_mat needs a reconstruction + printed mat; two_point needs "
            "registered-image pixels; known_dim takes your ratio with a provenance source. All leave confirmed=false."
        ),
    },
    "reveng_single_photo": {
        "prompt_note": (
            "Preview tier when only one photo exists — uses the first image in "
            "images/ and force-tags the session approximate, so build_parametric will refuse without the override."
        ),
        "timeout_s": 600,
    },
    "reveng_start_session": {
        "prompt_note": (
            "First call of any RE flow; pass research= (MPN/query/URL) to "
            "auto-populate the datasheet lane or part_ref to seed photos/datasheet from the parts hub."
        ),
    },
    "reveng_status": {
        "prompt_note": (
            "Call first — probes backends (COLMAP etc.) before promising a "
            "reconstruction; pure and offline-safe, with fix-it banners for what's missing."
        ),
    },
}
