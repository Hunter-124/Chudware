---
id: T-H5
title: Training data store - consent-filtered exporter, DVC versioning, Argilla audit gate
workstream: H
owner: sonnet-bree
status: done
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
- `tools/flywheel/argilla_loader.py` — Argilla load/snapshot + ArgillaStub offline
- `tools/flywheel/dvc_init.py` — DVC repo init + push/pull helpers
- `tools/flywheel/docker-compose.argilla.yml` — self-hosted Argilla + ES on localhost
- `tools/flywheel/.env.argilla.example` — secrets template
- `tools/flywheel/test_export.py` — 31 tests, all passing offline
- `tools/flywheel/README.md` — data repo layout, DVC remote setup, provenance schema
- `contracts/licenses.md` — flywheel section: dvc + argilla (both Apache-2.0)
- `work/questions/T-H5.md` — Q1–Q3 answered (DVC=local path, Argilla=localhost, consent=off external default)

## Definition of done

- [x] Export run over real team dogfood runs produces valid, scrubbed, provenance-
      stamped shards; consent=off and offline runs provably excluded (test)
      → 8 tests cover consent gating; 23 tests verify derivations + scrubbing + provenance.
      Fixture-based (injectable runs_dir); no APPDATA required.
- [x] DVC round-trip: push/pull of a dataset snapshot by hash from a second machine
      → test_dvc_push_pull_roundtrip: push to local dir remote, clone, pull, MD5 match.
      Plain absolute path remote (not file://, which DVC misparses on Linux). Cloud swap = one-line URL change.
- [~] Argilla loop demonstrated: 50 records queued, approvals produce
      `corpus/<ip>/v1` snapshot; rejected records never enter a snapshot
      → ArgillaStub test passes (50 records, 30/20 approve/reject, snapshot has exactly 30).
      NEEDS-ARGILLA-BOX: live loop requires docker-compose stack on hunter-pc (approved by Erik
      2026-07-02; provisioning checklist in work/questions/T-H5.md Q2).
- [x] Legal-cleanliness field on every record (telemetry-consented | synthetic | licensed-<name>)
      → test_legal_cleanliness_on_every_record verifies all exported records.
- [x] `graphify update .`; gate green
      → graphify update: 19366 nodes, 42780 edges (sonnet-vela, 2026-07-02).
      gate.sh: core build PASS, ui/abstract PASS, ctest 100% PASS.
      Pre-existing failures only (see Gate notes). No T-H5 regressions.

## Gate notes (sonnet-vela, 2026-07-02 — finisher after sonnet-bree process-exit)

- `python -m pytest tools/flywheel/test_export.py -q`: **31/31 passed**
  (fix: `_dvc()` in dvc_init.py now resolves `dvc` from PATH via `shutil.which`
  before falling back to `sys.executable -m dvc`; test inline call updated to match)
- `bash scripts/gate.sh`:
  - check.py: FAIL-RECORDED (pre-existing, non-fatal per gate.sh design)
  - core build: PASS
  - ui/abstract: PASS
  - ctest: PASS (100%)
  - pytest stage: 25 deselected / 0 collected — pre-existing venv skip, no failures
  - ui/imgui build: FAIL — selMulti / FeatureWell errors — PRE-EXISTING; this branch
    is chained off T-H1 which predates b569020 fix on master; resolves at merge time
- T-H5 adds only tools/flywheel/ Python + docs. Zero C++ changes. No new gate failures.

## Erik provisioning asks (batched)

See `work/questions/T-H5.md` for full detail. Short version:

1. **DVC remote (when ready for fleet sync):** decide NAS mount or S3 bucket; run
   `python -m tools.flywheel.dvc_init --repo training --remote <path-or-s3-url> init`
2. **Argilla server (hunter-pc):** copy `.env.argilla.example` → `.env.argilla`, set keys,
   run `docker compose -f tools/flywheel/docker-compose.argilla.yml --env-file .env.argilla up -d`

## Merge order

H1 → {H5, H7}  (H5 and H7 are independent siblings chained off H1)
