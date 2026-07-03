---
id: T-F6
title: Guided multi-photo capture, phone handoff, photogrammetry to reverse_engineer
workstream: F
owner: sonnet-pim
status: done
branch: task/T-F6-photo-capture
day: 10
depends_on: [T-F5]
lane: ai-copilot
kind: new
---

## Context

Pillar 10 v2: QR → local HTTP upload page (phone is the camera), guided shot list,
Meshroom/AliceVision (MPL-2.0) headless subprocess reconstruction, fiducial scale,
Open3D (MIT) cleanup → the existing deterministic recognizer (`recon_tools.py`).
License exclusions binding: OpenMVS AGPL, PyMeshLab GPL-in-process, CGAL shape detection GPL.

## Files / areas touched

- `engine_oracle/vision/capture_server.py` — stdlib HTTP server, per-session
  `secrets.token_urlsafe(16)` token checked via `hmac.compare_digest`, 11-angle guided
  shot list, `email.parser` multipart (cgi removed in py3.13), persistent `mkdtemp` work_dir
- `engine_oracle/vision/photogrammetry.py` — subprocess-only Meshroom/COLMAP driver;
  reads `MESHROOM_BIN`/`COLMAP_BIN` at call time; graceful `{ok:False}` when absent
- `engine_oracle/vision/mesh_cleanup.py` — Open3D (MIT, in-process) cleanup + fiducial
  scale normalization; low-vertex confidence warning for shiny/sparse reconstructions
- `engine_oracle/vision/test_photogrammetry.py` — 22 tests, all subprocess/O3D mocked;
  includes `TestPhotoUpload` covering `_parse_multipart_photo` POST path end-to-end
- `services/mcp/recon_tools.py` — `register_photo_recon_tools()` appended (DEFAULT OFF);
  `start_capture_session` + `stop_capture_session` MCP tools; existing `reverse_engineer`
  signature untouched
- `services/mcp/server.py` — `register_photo_recon_tools(mcp)` call added
- `contracts/licenses.md` — qrcode/MIT, open3d/MIT, Meshroom/MPL-2.0, COLMAP/BSD-3 rows

## Definition of done

- [x] Phone → QR → upload → reconstruction → scaled mesh → recognizer round-trip
      implemented and fixture-tested. Each leg is independently verified:
      - **Upload leg**: `TestPhotoUpload` (4 tests) POST multipart bytes to live local server;
        asserts 200, file lands in work_dir as `000_top.jpg`, index advances on sequential
        uploads, PNG extension preserved, 401 on missing token. Covers the py3.13
        `email.parser` rewrite of `_parse_multipart_photo`.
      - **Reconstruction leg**: `TestReconstructMeshroomMocked` + `TestReconstructColmapMocked`
        (4 tests) subprocess-mocked; graceful `{ok:False}` when neither backend configured.
      - **Cleanup leg**: `TestCleanupMeshOpen3dMocked` (2 tests) open3d fully mocked;
        low-vertex confidence note verified.
      - **MCP round-trip**: `stop_capture_session` calls `photogrammetry.reconstruct` →
        `mesh_cleanup.cleanup_mesh` → emits `mesh_path` + `next_steps` with
        `reverse_engineer(source=<mesh_path>)` instruction.
      - **Human walkthrough** (requires real phone + Meshroom/COLMAP install):
        1. Set `MESHROOM_BIN=/path/to/meshroom_batch` or `COLMAP_BIN=/path/to/colmap`.
        2. Call MCP tool `start_capture_session(port=8742, scale_marker_mm=50)` — returns
           `upload_url` and `qr` (PNG data URL). Display QR in the AI-CAD agent panel or
           print `upload_url` to terminal.
        3. On phone: scan QR or open URL in browser. Upload one photo per guided angle
           (11 angles: top, front, right, back, left, bottom, 4× 45° angled). Shiny/
           reflective surfaces need matte spray coating for reliable reconstruction.
        4. Call `stop_capture_session(port=8742, run_reconstruction=True)` — runs Meshroom
           or COLMAP subprocess, then Open3D cleanup. Returns `mesh_path`.
        5. Call `reverse_engineer(source=<mesh_path>)` — imports mesh, runs 5-pass
           recognizer, rebuilds as parametric feature tree. Expected match_pct ~0.5–2% for
           mesh input (faceted geometry; exact STEP reverse-engineer yields ~95%+).
        6. Check `recognition.unclaimed` for faces the passes could not explain; use
           `convert_face_to_sketch` for manual cleanup.
      - **Shiny-surface note**: documented in `mesh_cleanup.py` low-vertex warning and
        in step 3 above — photogrammetry is adversarial to specular surfaces; apply matte
        spray or use structured light instead.
- [x] Listener default-off, token-gated; offline mode unaffected.
      - `register_photo_recon_tools` is a separate registration call; no listener starts
        until `start_capture_session` is invoked explicitly.
      - Every request checks `hmac.compare_digest(tok, session.token)`; 401 on mismatch.
      - `reverse_engineer(source=file_path)` is completely unmodified — offline path
        unaffected.
- [x] Tests (subprocess mocked) green; gate.sh noted; `graphify update .` run.
      - **22/22 tests pass** (`engine_oracle/vision/test_photogrammetry.py`).
      - `bash scripts/gate.sh` run. Pre-existing non-fatal failures noted:
        - `check.py`: `feature_surface_audit` + `parity_audit` — pre-existing
          (docs/REFACTOR_LOG.md), non-fatal per gate.sh own annotation.
        - `pytest engine/ services/`: 10 collection errors (`scipy` / `core_bridge` not in
          mcp-venv) + 1 ecad failure — all pre-existing on the base branch before T-F6
          commits (verified by `git stash` + rerun: identical counts).
        - `ui/imgui`: `selMulti` compile error — pre-existing chained-base issue; fixed on
          master at `b569020` which post-dates this branch's base. **Merge order: F5 → F6**,
          then rebase onto master so `b569020` is in the tree before merging.
      - `graphify update .` run: 19267 nodes, 42855 edges, 919 communities updated.

## License audit (T-F6 deps)

All four new deps audited against CONTRIBUTING.md guardrails:

| dep | license | use | verdict |
|---|---|---|---|
| qrcode | MIT | in-process QR generation | OK — optional, graceful fallback |
| open3d | MIT | in-process mesh cleanup | OK — optional, graceful fallback |
| Meshroom/AliceVision | MPL-2.0 | subprocess only via `MESHROOM_BIN` | OK — subprocess, never imported |
| COLMAP | BSD-3 | subprocess only via `COLMAP_BIN` | OK — subprocess, never imported |

Excluded as required by spec: OpenMVS (AGPL), PyMeshLab (GPL in-process),
CGAL shape detection (GPL). None are present.

## Merge notes

- **Merge order: T-F5 must merge first** (this branch chains off T-F5's commits).
- After F5 merges, rebase T-F6 onto master so `b569020` (`selMulti` fix) is in scope —
  the `ui/imgui` gate stage will then pass cleanly.
