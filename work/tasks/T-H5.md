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

## Driver notes (Fable, 2026-07-02 — next-wave prep)

- T-H1 landed everything you consume: contracts/telemetry.md (derivation rules
  section = your exporter spec), consent_tier() in core/agent/config.cpp AND
  services/agent/config.py, per-doc journals (.cam_journal.jsonl/.ecad_journal.jsonl
  next to containers), ai_artifact events in cache/runs/*/events.jsonl.
- consent enforcement is already stamped per-event — your exporter FILTERS on it;
  test that consent=off and offline runs are provably excluded.
- HUMAN-GATED (add to FOREMAN "For Erik" when you get there): DVC remote bucket
  choice + Argilla hosting box. Build everything against a local directory remote so
  the human decision doesn't block the code.
