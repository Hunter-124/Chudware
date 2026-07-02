---
id: T-F6
title: Guided multi-photo capture, phone handoff, photogrammetry to reverse_engineer
workstream: F
owner: unassigned
status: todo
branch: task/T-F6-photogrammetry
day: 10
depends_on: [T-F5]
---

## Context

Pillar 10 v2: QR → local HTTP upload page (phone is the camera), guided shot list,
Meshroom/AliceVision (MPL-2.0) headless subprocess reconstruction, fiducial scale,
Open3D (MIT) cleanup → PCL (BSD) RANSAC → the existing deterministic recognizer
(`recon_tools.py`). READ `docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 10 v2 (license
exclusions binding: OpenMVS AGPL, PyMeshLab GPL-in-process, CGAL shape detection GPL).

## Files / areas touched

- `engine/vision/capture_server.py` — local listener (DEFAULT OFF; QR carries port +
  per-session token)
- `engine/vision/photogrammetry.py` — Meshroom subprocess driver (COLMAP alternate)
- `engine/vision/mesh_cleanup.py` — Open3D + PCL pipeline
- `services/mcp/` — `reverse_engineer` gains a photos entry path

## Definition of done

- [ ] Phone → QR → upload → reconstruction → scaled mesh → recognizer round-trip on a
      textured test part (expectation ~0.5–2%; shiny parts documented as needing spray)
- [ ] Listener default-off, token-gated; offline mode unaffected
- [ ] Tests (subprocess mocked) green; `bash scripts/gate.sh`; `graphify update .`
