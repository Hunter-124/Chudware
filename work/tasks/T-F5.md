---
id: T-F5
title: Image ingestion v1 - real files, provider image slots, fiducial metrology
workstream: F
owner: opus-sage
status: done
branch: task/T-F5-image-ingestion
day: 7
depends_on: []
---

## Context

Pillar 10 v1: close the loop `request_part_photos` only gestures at — real pixels into
the agent, deterministic code does the measuring. READ `docs/AI_TOOLING_MASTER_PLAN.md`
§Pillar 10 (architecture law: VLMs pick pixels, code measures; images ≤1024px/<1MB to
agents with resource_link to full-res; all deps Apache/BSD, no GPU).

## Files / areas touched

- `engine/vision/` — new module (ingestion, ArUco/ChArUco via OpenCV objdetect,
  `measure_from_photo`)
- `services/mcp/` — extend `submit_part_photos` to register real files/photo IDs;
  new `measure_from_photo(photo_id, px_a, px_b)`
- `services/agent/providers/` — image slot in the provider message schema

## Steps

1. Accept images by path/drag-drop; photo-ID registry; downscale pipeline.
2. Printable ChArUco/ArUco calibration sheet generator.
3. `measure_from_photo`: fiducial homography + calibrated pixel scale; marker pixel-area
   drives confidence; out-of-plane flagged low-confidence.
4. Route extracted dimensions through the existing intake confidence tiers — vision
   never silently writes geometry.
5. UI honesty string: "±2–5% in-plane with the marker flat on the measurement plane."

## Definition of done

- [x] Photo in → agent sees downscaled image + photo ID; full-res linked
- [x] Known-artifact test: printed sheet + synthetic photo measures within tolerance
- [x] Dimensions land in the intake ledger, never directly in geometry
- [x] Tests green; `bash scripts/gate.sh`; `graphify update .`

## Landed (opus-sage)

- `engine/vision/` (physically `engine_oracle/vision/`, via the `engine`→`engine_oracle`
  symlink): `ingest` (photo-ID registry + ≤1024px/<1MB downscale, Pillow-only, imports
  without cv2), `calib_sheet` (printable ChArUco sheet + ArUco marker), `fiducial`
  (ChArUco detection + px→mm homography; px_per_mm + marker-area CoV → confidence/planarity),
  `measure` (`measure_from_photo` + `HONESTY_STRING` + `to_claim`).
- `services/mcp/intake_tools.py`: `submit_part_photos(photos=[...])` registers real files;
  new `measure_from_photo(photo_id, [x,y], [x,y], feature, dim, fit?, board?)` lands the
  fiducial measurement in the intake ledger at its marker-area tier with
  `provenance.method="photo_fiducial"` — never geometry. The intake §5 policy then applies:
  a photo dim on a tolerance-sensitive fit is downgraded-and-flagged (never a typed
  `measured`), and any low / out-of-plane read stays demanded via `confirm_dimensions`.
  Prior caliper confirmations are preserved across re-measure. cv2-absent ⇒ contract-shaped
  `{ok:false,reason,hint}`.
- `services/agent/providers/`: the neutral `image_part` gained an optional `mime`; the
  anthropic/openai/gemini adapters honor it (default png) so downscaled JPEG previews ride
  the existing image slot correctly. **Seam for T-F9:** the C++ provider image slot
  (`core/agent/providers.cpp`) is unchanged — documented, not built, per the driver note.
- Dep: `opencv-python-headless==4.11.0.86` (Apache-2.0, core objdetect, no contrib/GPU).
- Verified: synthetic ChArUco 90 mm → 89.95 mm (0.06%), tier=high; vision suite 6 passed
  with cv2, 4 passed / 2 skipped without cv2 (graceful degradation).

## Driver notes (Fable, 2026-07-02)

- **Extend, don't replace:** `submit_part_photos` at
  `services/mcp/intake_tools.py:174-225` already scores claims and builds
  MeasuredPartProfile (`engine_oracle/intake/intake.py` — MeasuredFeature has
  confidence tiers low/medium/high/measured, TIER_UNCERT_MM table). Your photo IDs
  attach real files to that existing flow; `measure_from_photo` results enter as
  claims with `provenance.method: "photo_fiducial"` and tier by marker pixel area.
- **Provider image slots (Python) already exist:** the neutral message format is
  `[{role, content: [{type: text|image, text/path}]}]` — `anthropic_provider.py:39-59`
  consumes image content (the build loop sends contact sheets today). So the Python
  side needs only the downscale + registry; the C++ provider image slot
  (`core/agent/providers.cpp`) is a SEAM to document, not build (note it for T-F9).
  For synthetic test photos, render a known part (`render_part`) and composite a
  generated ArUco marker at known scale — no camera needed in CI.
- **OpenCV dep:** `cv2.aruco` lives in core OpenCV `objdetect` (Apache-2.0) —
  `opencv-python-headless` in requirements; no GUI extras, no contrib needed for
  ArUco (ChArUco boards are also in objdetect since 4.7).
- **Size caps are a hard contract:** agent-facing images ≤1024 px and <1 MB (Claude
  Desktop tool-result cap), `resource_link` to full-res — master plan Pillar 10.
- **Scope fence:** no phone/QR upload page, no photogrammetry — that's T-F6.
