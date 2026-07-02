---
id: T-H5
title: Training data store - consent-filtered exporter, DVC versioning, Argilla audit gate
workstream: H
owner: unassigned
status: todo
branch: task/T-H5-training-data-store
day: 12
depends_on: [T-H1]
---

## Context

Pillar 16 (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §5.1–5.3). `tools/flywheel/export.py`
scans `<appdata>/AI-CAD/cache/runs/**` + session dirs, applies consent filter
(off|meta|full; offline mode ⇒ nothing), scrubs (keys/paths/usernames stripped, machine
ids hashed), derives SFT / preference-pair / correction-trace records per the
telemetry contract, and writes provenance-stamped JSONL shards into a DVC-tracked
data repo (S3-compatible remote — the fleet spans states; remote storage is the sync
point). Export is user-initiated or visibly scheduled with a local staging dir the
user can inspect before upload — never silent. Human audit gate #1 (data approval)
runs through self-hosted Argilla: telemetry → Argilla queue → approved dataset
snapshot. HUMAN-GATED: remote bucket choice + Argilla hosting box are driver decisions
before first upload.

## Files / areas touched

- `tools/flywheel/export.py` — scanner, consent filter, scrubber, derivations
- `tools/flywheel/README.md` — data repo layout, DVC remote setup, provenance schema
- Argilla docker-compose + loader/exporter scripts (approved records → dataset snapshot)
- training-data repo scaffold (separate repo or `training/` — document the choice)

## Definition of done

- [ ] Export run over real team dogfood runs produces valid, scrubbed, provenance-
      stamped shards; consent=off and offline runs provably excluded (test)
- [ ] DVC round-trip: push/pull of a dataset snapshot by hash from a second machine
- [ ] Argilla loop demonstrated: 50 records queued, approvals produce
      `corpus/<ip>/v1` snapshot; rejected records never enter a snapshot
- [ ] Legal-cleanliness field on every record (telemetry-consented | synthetic |
      licensed-<name>)
- [ ] `graphify update .`; gate green
