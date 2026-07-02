---
id: T-F5
title: Image ingestion v1 - real files, provider image slots, fiducial metrology
workstream: F
owner: unassigned
status: todo
branch: task/T-F5-image-ingestion
day: 2
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

- [ ] Photo in → agent sees downscaled image + photo ID; full-res linked
- [ ] Known-artifact test: printed sheet + synthetic photo measures within tolerance
- [ ] Dimensions land in the intake ledger, never directly in geometry
- [ ] Tests green; `bash scripts/gate.sh`; `graphify update .`
