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
            "Post-build quality check: pass ref_path or brep_doc_id. Confirm the "
            "metric scale first; alignment='icp' explicitly aligns surfaces rigidly."
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
            "Bypasses photogrammetry when a scan mesh already exists; scale_mm "
            "is millimetres per input unit, not the size of a physical feature."
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
            "Supply image paths or a directory. Each image must decode; failed "
            "sources return ok:false with full failed paths while valid images are retained."
        ),
    },
    "reveng_add_video": {
        "prompt_note": (
            "Extracts frames from an orbit video asynchronously without starting "
            "training. Poll get_reveng_session; wait=True is for a small clip."
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
            "Search for an existing model first with reveng_find_models. Run after "
            "reveng_fit_primitives (fits feed the hints). Approximate surfaces are refused "
            "unless strategy='override_approximate'; independently check critical dimensions."
        ),
        "timeout_s": 300,
    },
    "reveng_cancel_splat": {
        "prompt_note": (
            "Stops the owned Gaussian process tree while retaining the last valid "
            "preview; cancellation is not completion of the reconstruction."
        ),
    },
    "reveng_crop_splat": {
        "prompt_note": (
            "Stop training first. Bounds are [xmin,ymin,zmin,xmax,ymax,zmax] in "
            "reconstruction units, not calibrated CAD millimetres; null restores the original."
        ),
    },
    "reveng_finish_splat": {
        "prompt_note": (
            "Ends live acquisition by requesting a full-quality pass over accepted "
            "frames. Read reveng_splat_status for progress and the resulting preview."
        ),
    },
    "reveng_find_models": {
        "prompt_note": (
            "Search for an existing CAD model before researching dimensions or building; "
            "inspect candidates and their source before selecting one. The fan-out renders "
            "~20 vendor sites and can take a minute or two."
        ),
        "timeout_s": 300,
    },
    "reveng_use_model": {
        "prompt_note": (
            "Use a selected existing CAD model by URL or local path in the session; "
            "check its suitability and provenance rather than assuming it matches."
        ),
        "timeout_s": 180,
    },
    "reveng_fit_primitives": {
        "prompt_note": (
            "Needs a mesh and a confirmed scale. approximate=True allows exploratory "
            "unscaled fitting; residuals alone do not establish dimensional uncertainty."
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
            "Searches for existing models before research unless force_build is set. "
            "Inspect any candidates and use reveng_use_model when suitable. On "
            "model_present=false with pending_dims, confirm fit-critical dimensions; "
            "re-calling never forces them."
        ),
        "timeout_s": 300,
    },
    "reveng_print_scale_mat": {
        "prompt_note": (
            "Print before capture when no known dimension exists — print at 100% and check "
            "a square against 25 mm; the aruco_mat solver defaults to exactly this ChArUco board."
        ),
    },
    "reveng_reconstruct": {
        "prompt_note": (
            "Async: returns run_id immediately. The shared SfM dataset drives both Brush "
            "and OpenMVS; a splat-derived fallback mesh is explicitly approximate. "
            "Poll get_reveng_session / reveng_gate_status; scale confirmation pauses the run."
        ),
    },
    "reveng_research_source": {
        "prompt_note": (
            "Search first with reveng_find_models. No-photo lane: needs part_number, query, "
            "or url (one required); offline returns honest ok:false."
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
            "aruco_mat needs a reconstruction + printed mat; two_point needs pixels "
            "on an undistorted dataset image and image_space='undistorted'. known_dim "
            "needs value_mm plus recon_length or "
            "point_a and point_b in native 3D coordinates; the ratio is value_mm/recon_length. "
            "All leave confirmed=false."
        ),
    },
    "reveng_confirm_scale": {
        "prompt_note": "After a person verifies the solved scale, confirm it when no mission gate exists. "
                       "For an active mission use reveng_resolve_gate instead.",
    },
    "reveng_align_axes": {
        "prompt_note": (
            "Align an arbitrary SfM gauge before deterministic rebuilding: planes uses fitted "
            "orthogonal planes or mesh faces and falls back to PCA, pca uses mesh points, "
            "manual accepts an absolute proper 3x3 rotation. Alignment is not a verified datum. "
            "Alignment invalidates fits and model; fit again afterward."
        ),
    },
    "reveng_import_splat": {
        "prompt_note": "Import Gaussian PLY, .splat, .spz or .ksplat into a clean session; inspect conversion losses.",
    },
    "reveng_convert_splat": {
        "prompt_note": "Convert supported splat encodings and inspect reported losses.",
    },
    "reveng_splat_to_mesh": {
        "prompt_note": (
            "Prunes the splat to its subject first (adaptive opacity, camera-orbit region, floor, "
            "floaters) and writes points_path and pruned_splat_path, then meshes: auto uses TSDF "
            "only with proven matching undistorted poses, otherwise Poisson with fallback_reason; "
            "method='poisson' is explicit. TSDF fuses the full splat's median depth from up to "
            "120 views, removes the support plane (remove_floor) and takes minutes. No method "
            "makes Gaussian geometry a metrology-grade measurement."
        ),
        "timeout_s": 1800,
    },
    "reveng_sparse_preview": {
        "prompt_note": (
            "Inspect the bounded points array of [x,y,z,r,g,b] and camera frusta. "
            "Before scale confirmation points use reconstruction units; after confirmation "
            "they use mm. To supply native coordinates to reveng_set_scale, invert the "
            "confirmed recon_transform: point = rotation.T @ (point_mm - translation_mm) "
            "/ mm_per_unit."
        ),
    },
    "reveng_single_photo": {
        "prompt_note": (
            "Preview tier when only one photo exists — uses the first image in "
            "images/ and force-tags the session approximate, so build_parametric will refuse without the override."
        ),
        "timeout_s": 600,
    },
    "reveng_splat_status": {
        "prompt_note": (
            "Nonblocking durable job status and latest immutable Gaussian preview. "
            "A preview during training is not a finished full-quality result."
        ),
    },
    "reveng_start_session": {
        "prompt_note": (
            "First call of any RE flow; search for existing models before research or "
            "building. part_ref can seed photos/datasheet from the parts hub."
        ),
    },
    "reveng_start_splat": {
        "prompt_note": (
            "Starts local Gaussian reconstruction without CAD scale or mesh gates. "
            "Use live=true for arriving accepted images; object mode needs an explicit "
            "reconstruction-unit crop, not automatic segmentation."
        ),
    },
    "reveng_status": {
        "prompt_note": (
            "Call first — probes backends (COLMAP etc.) before promising a "
            "reconstruction; pure and offline-safe, with fix-it banners for what's missing."
        ),
    },
    "reveng_install_tools": {
        "prompt_note": (
            "Installs a missing Brush / OpenMVS (about 210 MB) in the background; poll "
            "reveng_status().install until running is false. Do not ask the user to run a script."
        ),
    },
}
