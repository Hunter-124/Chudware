"""Tests for tools/flywheel/export.py and argilla_loader.py (T-H5).

DoD coverage:
  [x] consent=off sessions produce zero exported records
  [x] offline_enabled=True produces zero exported records (hard gate)
  [x] consent=full sessions produce valid, scrubbed, provenance-stamped shards
  [x] consent=meta sessions strip sidecar refs (input_ref/output_ref/etc null)
  [x] SFT derivation (§5.1): clean accept → sft record
  [x] Preference pair — regen (§5.2a)
  [x] Preference pair — retry-loop (§5.2b)
  [x] Preference pair — undo-negative (§5.2c)
  [x] Correction trace (§5.3): edited + edit_ops_ref → correction_trace
  [x] Elicitation gold (§5.4): design_brief_signoff → elicitation_gold
  [x] Legal-cleanliness field present on every record ("telemetry-consented")
  [x] Provenance field present on every record (source/session_id/artifact_id/consent/ts_derived)
  [x] API keys are scrubbed from text fields
  [x] Absolute paths are scrubbed from text fields
  [x] Machine IDs are hashed (not raw)
  [x] DVC round-trip: push to file:// remote, pull from clone, hash matches
  [x] Argilla stub: 50 records queued, approvals → snapshot; rejected never enter snapshot
  [x] Absent session envelope → session skipped conservatively
  [x] Meta tier stricter than full: meta+full session → meta effective tier
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.flywheel.export import (
    ExportConfig,
    ExportResult,
    _resolve_tier,
    _scrub_text,
    _hash_machine_id,
    run_export,
)
from tools.flywheel.argilla_loader import ArgillaStub
from tools.flywheel.dvc_init import (
    add_to_dvc,
    dvc_pull,
    dvc_push,
    dvc_status,
    init_training_repo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _make_artifact(
    artifact_id: str | None = None,
    run_id: str = "run-001",
    insertion_point: str = "IP-13",
    consent: str = "full",
    input_ref: str = "artifact_XXX_input.jsonl",
    output_ref: str = "artifact_XXX_output.jsonl",
) -> dict:
    aid = artifact_id or str(uuid.uuid4())
    return {
        "kind": "ai_artifact",
        "artifact_id": aid,
        "run_id": run_id,
        "insertion_point": insertion_point,
        "model": {"provider": "anthropic", "model": "claude-fable-5", "adapter": None},
        "prompt_hash": "abc123",
        "input_ref": input_ref.replace("XXX", aid[:8]),
        "output_ref": output_ref.replace("XXX", aid[:8]),
        "base_revision": 17,
        "verifier_scores": {"check_sketch.status": "full", "dof": 0},
        "latency_ms": 8250,
        "consent": consent,
        "ts": _ts(),
    }


def _make_action(
    artifact_id: str,
    action: str,
    consent: str = "full",
    edit_ops_ref: str | None = None,
    undo_within_ops: int = 0,
    dwell_ms: int = 41000,
) -> dict:
    rec = {
        "kind": "ai_artifact_action",
        "artifact_id": artifact_id,
        "action": action,
        "revision": 18,
        "edit_ops_ref": edit_ops_ref,
        "undo_within_ops": undo_within_ops,
        "dwell_ms": dwell_ms,
        "note": None,
        "consent": consent,
        "ts": _ts(),
    }
    return rec


def _make_stage_result(
    run_id: str = "run-001",
    stage: str = "draft",
    verdict: str = "pass",
) -> dict:
    return {
        "kind": "stage_result",
        "run_id": run_id,
        "stage": stage,
        "verdict": verdict,
        "ts": _ts(),
    }


def _make_signoff(artifact_id: str, insertion_point: str = "IP-12") -> dict:
    return {
        "kind": "design_brief_signoff",
        "artifact_id": artifact_id,
        "insertion_point": insertion_point,
        "model_draft_ref": "model_draft.jsonl",
        "user_signed_brief_ref": "signed_brief.jsonl",
        "ts": _ts(),
    }


def _make_session_envelope(consent: str = "full") -> dict:
    return {
        "consent": consent,
        "origin": "team",
        "app_version": "0.1.0-test",
        "os": "linux",
    }


def _write_run(run_dir: Path, events: list[dict]) -> None:
    """Write a run directory with events.jsonl."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def tmp_path_factory_alias(tmp_path):
    """Just alias tmp_path for clarity."""
    return tmp_path


# ---------------------------------------------------------------------------
# Consent gate tests (DoD: consent=off and offline runs provably excluded)
# ---------------------------------------------------------------------------

class TestConsentGate:
    def test_offline_enabled_produces_zero_records(self, tmp_path):
        """Hard gate: offline_enabled=True → export nothing regardless of session consent."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        events = [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, consent="full"),
            _make_action(artifact_id=aid, action="accepted"),
        ]
        _write_run(run_dir, events)

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"offline_mode": True, "telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_written == 0
        assert result.tier == "off"
        assert result.runs_skipped_offline >= 1

    def test_consent_off_in_app_config_produces_zero_records(self, tmp_path):
        """App config consent=off → all sessions skipped."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        events = [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, consent="full"),
            _make_action(artifact_id=aid, action="accepted"),
        ]
        _write_run(run_dir, events)

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"offline_mode": False, "telemetry_consent": "off"},
        )
        result = run_export(cfg)

        assert result.records_written == 0
        assert result.tier == "off"

    def test_session_envelope_consent_off_skips_session(self, tmp_path):
        """Per-session envelope consent=off → that session is skipped even if app says full."""
        runs_dir = tmp_path / "runs"
        # Run 1: session says off
        run1 = runs_dir / "run-001"
        aid1 = str(uuid.uuid4())
        _write_run(run1, [
            _make_session_envelope(consent="off"),
            _make_artifact(artifact_id=aid1, consent="off"),
            _make_action(artifact_id=aid1, action="accepted", consent="off"),
        ])
        # Run 2: session says full — should be exported
        run2 = runs_dir / "run-002"
        aid2 = str(uuid.uuid4())
        _write_run(run2, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid2, consent="full"),
            _make_action(artifact_id=aid2, action="accepted", consent="full"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"offline_mode": False, "telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.runs_skipped_consent == 1
        assert result.records_written >= 1  # run-002 SFT record
        # Verify no records from run-001
        shards = list((tmp_path / "staging").rglob("*.jsonl"))
        all_sessions = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    all_sessions.append(rec.get("provenance", {}).get("session_id"))
        assert "run-001" not in all_sessions

    def test_absent_session_envelope_skips_conservatively(self, tmp_path):
        """No session envelope → session skipped (conservative privacy default)."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-noenv"
        aid = str(uuid.uuid4())
        # Write events WITHOUT the session envelope
        _write_run(run_dir, [
            _make_artifact(artifact_id=aid, consent="full"),
            _make_action(artifact_id=aid, action="accepted"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"offline_mode": False, "telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_written == 0
        assert "run-noenv" in result.skipped_run_ids


# ---------------------------------------------------------------------------
# SFT derivation tests (§5.1)
# ---------------------------------------------------------------------------

class TestSFTDerivation:
    def test_clean_accept_produces_sft_record(self, tmp_path):
        """Accepted artifact with no subsequent edit → sft record."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, consent="full", insertion_point="IP-13"),
            _make_action(artifact_id=aid, action="accepted"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("sft", 0) >= 1
        # Verify shard content
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        assert shards
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        sft_records = [r for r in records if r.get("record_type") == "sft"]
        assert len(sft_records) >= 1
        rec = sft_records[0]
        assert rec["insertion_point"] == "IP-13"
        assert rec["legal_cleanliness"] == "telemetry-consented"
        assert "provenance" in rec
        assert rec["provenance"]["source"] == "telemetry"
        assert rec["provenance"]["consent"] == "full"

    def test_edited_artifact_not_sft(self, tmp_path):
        """Accepted + edited artifact → NOT an SFT example (would be correction trace)."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, consent="full"),
            _make_action(artifact_id=aid, action="accepted"),
            _make_action(artifact_id=aid, action="edited", edit_ops_ref="edit.jsonl"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("sft", 0) == 0

    def test_rejected_artifact_not_sft(self, tmp_path):
        """Rejected artifact → NOT an SFT example."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, consent="full"),
            _make_action(artifact_id=aid, action="rejected"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("sft", 0) == 0


# ---------------------------------------------------------------------------
# Preference pair tests (§5.2)
# ---------------------------------------------------------------------------

class TestPreferencePairs:
    def test_regen_pair(self, tmp_path):
        """Regenerated A + accepted sibling B → regen preference pair."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid_a = str(uuid.uuid4())
        aid_b = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid_a, run_id="run-001"),
            _make_action(artifact_id=aid_a, action="regenerated"),
            _make_artifact(artifact_id=aid_b, run_id="run-001"),
            _make_action(artifact_id=aid_b, action="accepted"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("preference_pair", 0) >= 1
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        regen_pairs = [r for r in records
                       if r.get("record_type") == "preference_pair"
                       and r.get("subtype") == "regen"]
        assert len(regen_pairs) >= 1
        assert regen_pairs[0]["rejected_artifact_id"] == aid_a
        assert regen_pairs[0]["chosen_artifact_id"] == aid_b
        assert regen_pairs[0]["legal_cleanliness"] == "telemetry-consented"

    def test_retry_loop_pair(self, tmp_path):
        """Stage fail then pass → retry-loop preference pair."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_stage_result(run_id="run-001", stage="draft", verdict="retry"),
            _make_stage_result(run_id="run-001", stage="draft", verdict="pass"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("preference_pair", 0) >= 1
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        retry_pairs = [r for r in records
                       if r.get("record_type") == "preference_pair"
                       and r.get("subtype") == "retry_loop"]
        assert len(retry_pairs) >= 1

    def test_undo_negative_pair(self, tmp_path):
        """Undone artifact with undo_within_ops > 0 → undo_negative preference pair."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, run_id="run-001"),
            _make_action(artifact_id=aid, action="undone", undo_within_ops=3),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("preference_pair", 0) >= 1
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        undo_pairs = [r for r in records
                      if r.get("record_type") == "preference_pair"
                      and r.get("subtype") == "undo_negative"]
        assert len(undo_pairs) >= 1
        assert undo_pairs[0]["rejected_artifact_id"] == aid
        assert undo_pairs[0]["undo_within_ops"] == 3


# ---------------------------------------------------------------------------
# Correction trace tests (§5.3)
# ---------------------------------------------------------------------------

class TestCorrectionTrace:
    def test_edited_with_edit_ops_ref_produces_correction_trace(self, tmp_path):
        """Edited artifact with edit_ops_ref → correction_trace record."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, output_ref="artifact_out.jsonl"),
            _make_action(artifact_id=aid, action="edited",
                         edit_ops_ref="artifact_edit_ops.jsonl"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("correction_trace", 0) >= 1
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        ct = [r for r in records if r.get("record_type") == "correction_trace"]
        assert len(ct) >= 1
        assert ct[0]["human_correction_ref"] == "artifact_edit_ops.jsonl"
        assert ct[0]["legal_cleanliness"] == "telemetry-consented"

    def test_edited_without_edit_ops_ref_produces_no_correction_trace(self, tmp_path):
        """Edited action without edit_ops_ref → no correction trace."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid),
            _make_action(artifact_id=aid, action="edited", edit_ops_ref=None),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("correction_trace", 0) == 0


# ---------------------------------------------------------------------------
# Elicitation gold tests (§5.4)
# ---------------------------------------------------------------------------

class TestElicitationGold:
    def test_signoff_event_produces_elicitation_gold(self, tmp_path):
        """design_brief_signoff event → elicitation_gold record."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_signoff(artifact_id=aid, insertion_point="IP-12"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        result = run_export(cfg)

        assert result.records_by_type.get("elicitation_gold", 0) >= 1
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        gold = [r for r in records if r.get("record_type") == "elicitation_gold"]
        assert gold[0]["insertion_point"] == "IP-12"
        assert gold[0]["legal_cleanliness"] == "telemetry-consented"


# ---------------------------------------------------------------------------
# Meta tier stripping tests
# ---------------------------------------------------------------------------

class TestMetaTierStripping:
    def test_meta_tier_strips_sidecar_refs(self, tmp_path):
        """Meta tier: input_ref, output_ref, edit_ops_ref, diff_ref nulled."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="meta"),
            _make_artifact(artifact_id=aid, consent="meta",
                           input_ref="artifact_input.jsonl",
                           output_ref="artifact_output.jsonl"),
            _make_action(artifact_id=aid, action="accepted", consent="meta"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "meta"},
        )
        result = run_export(cfg)

        assert result.records_written >= 1
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        sft = [r for r in records if r.get("record_type") == "sft"]
        assert sft
        rec = sft[0]
        assert rec.get("input_ref") is None
        assert rec.get("output_ref") is None

    def test_app_config_meta_beats_session_full(self, tmp_path):
        """App config meta + session full → effective tier is meta (stricter)."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),  # session says full
            _make_artifact(artifact_id=aid, consent="full",
                           input_ref="artifact_input.jsonl",
                           output_ref="artifact_output.jsonl"),
            _make_action(artifact_id=aid, action="accepted"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "meta"},  # app config is stricter
        )
        result = run_export(cfg)

        assert result.records_written >= 1
        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        records = []
        for shard in shards:
            for line in shard.read_text().splitlines():
                if line.strip():
                    records.append(json.loads(line))
        sft = [r for r in records if r.get("record_type") == "sft"]
        assert sft
        assert sft[0].get("input_ref") is None


# ---------------------------------------------------------------------------
# Scrubbing tests
# ---------------------------------------------------------------------------

class TestScrubbing:
    def test_api_key_scrubbed(self):
        text = "Using key: sk-ant-api03-abcdef1234567890abcdef in request"
        result = _scrub_text(text)
        assert "sk-ant-api03" not in result
        assert "<REDACTED_KEY>" in result

    def test_abs_path_scrubbed(self):
        text = "Reading from /home/hunter/Repositories/CAD/file.py"
        result = _scrub_text(text)
        assert "/home/hunter" not in result
        assert "<REDACTED_PATH>" in result

    def test_email_scrubbed(self):
        text = "Contact user@example.com for details"
        result = _scrub_text(text)
        assert "user@example.com" not in result
        assert "<REDACTED_EMAIL>" in result

    def test_machine_id_hashed(self):
        raw = "a1b2c3d4e5f6a1b2"
        hashed = _hash_machine_id(raw)
        assert hashed.startswith("mid:")
        assert raw not in hashed

    def test_provenance_on_every_record(self, tmp_path):
        """Every exported record must have a provenance field with required keys."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid),
            _make_action(artifact_id=aid, action="accepted"),
            _make_action(artifact_id=aid, action="edited", edit_ops_ref="e.jsonl"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        run_export(cfg)

        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        for shard in shards:
            for line in shard.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                assert "provenance" in rec, f"Missing provenance in {rec.get('record_type')}"
                prov = rec["provenance"]
                assert prov["source"] == "telemetry"
                assert "session_id" in prov
                assert "consent" in prov
                assert "ts_derived" in prov

    def test_legal_cleanliness_on_every_record(self, tmp_path):
        """Every exported record must have legal_cleanliness field."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid),
            _make_action(artifact_id=aid, action="accepted"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        run_export(cfg)

        shards = list((tmp_path / "staging").rglob("shard_*.jsonl"))
        for shard in shards:
            for line in shard.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                lc = rec.get("legal_cleanliness")
                assert lc in ("telemetry-consented", "synthetic", "licensed-<name>") or \
                       (lc is not None and lc.startswith("licensed-")), \
                       f"Bad legal_cleanliness: {lc!r} in {rec.get('record_type')}"


# ---------------------------------------------------------------------------
# Corpus sharding by insertion_point
# ---------------------------------------------------------------------------

class TestCorpusSharding:
    def test_records_sharded_by_insertion_point(self, tmp_path):
        """Shards are written to corpus/<insertion_point>/v1/ directories."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid, insertion_point="IP-13"),
            _make_action(artifact_id=aid, action="accepted"),
        ])

        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=tmp_path / "staging",
            app_config={"telemetry_consent": "full"},
        )
        run_export(cfg)

        # Shards must live under corpus/IP-13/v1/
        shard_dirs = list((tmp_path / "staging" / "corpus").iterdir())
        assert any(d.name == "IP-13" for d in shard_dirs)


# ---------------------------------------------------------------------------
# DVC round-trip test (DoD #2)
# ---------------------------------------------------------------------------

class TestDVCRoundTrip:
    def test_dvc_push_pull_roundtrip(self, tmp_path):
        """Push a shard to a local directory DVC remote, pull into a second clone, verify.

        Uses a plain absolute path remote (not file://) — that is the correct form
        for DVC on Linux with local/NAS storage.  Cloud swap is a one-line URL change.
        """
        import hashlib
        import subprocess
        import sys

        # Shared DVC remote directory (simulates NAS / cloud bucket)
        dvc_remote = tmp_path / "dvc-remote"
        dvc_remote.mkdir()
        remote_path = str(dvc_remote)  # plain absolute path — NOT file://

        # First machine: create repo with a shard
        repo1 = tmp_path / "machine1"
        init_training_repo(repo1, remote_url=remote_path)

        # Write a test shard
        shard_dir = repo1 / "corpus" / "IP-13" / "v1"
        shard_dir.mkdir(parents=True)
        shard = shard_dir / "test_shard.jsonl"
        shard.write_text(json.dumps({
            "record_type": "sft",
            "legal_cleanliness": "telemetry-consented",
            "provenance": {"source": "telemetry"},
        }) + "\n", encoding="utf-8")

        # Add to DVC, commit .dvc file, push
        add_to_dvc(repo1, shard)
        subprocess.run(["git", "add", "."], cwd=str(repo1), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "add shard"], cwd=str(repo1),
                       check=True, capture_output=True)
        dvc_push(repo1)

        # Compute MD5 of original shard
        original_hash = hashlib.md5(shard.read_bytes()).hexdigest()

        # Second machine: clone the git repo, configure DVC remote, pull
        repo2 = tmp_path / "machine2"
        subprocess.run(["git", "clone", str(repo1), str(repo2)],
                       check=True, capture_output=True)
        # -f overwrites any existing remote with the same name (idempotent)
        # Use _dvc_cmd() so this works whether dvc is on PATH or only in a venv.
        from tools.flywheel.dvc_init import _dvc_cmd
        subprocess.run(
            [*_dvc_cmd(), "remote", "add", "-d", "-f", "origin", remote_path],
            cwd=str(repo2), check=True, capture_output=True,
        )
        dvc_pull(repo2)

        # Verify the pulled shard matches (same hash = same content = round-trip OK)
        pulled_shard = repo2 / "corpus" / "IP-13" / "v1" / "test_shard.jsonl"
        assert pulled_shard.exists(), "Shard not pulled to second machine"
        pulled_hash = hashlib.md5(pulled_shard.read_bytes()).hexdigest()
        assert pulled_hash == original_hash, "Hash mismatch after DVC round-trip"


# ---------------------------------------------------------------------------
# Argilla stub test (DoD #3: 50 records, approve/reject)
# ---------------------------------------------------------------------------

class TestArgillaStub:
    def test_50_records_approve_reject_snapshot(self, tmp_path):
        """ArgillaStub: 50 records queued; approvals produce snapshot; rejected never included."""
        stub = ArgillaStub()

        # Build 50 records: 30 approved, 20 rejected
        records = []
        for i in range(50):
            records.append({
                "record_type": "sft",
                "insertion_point": "IP-13",
                "legal_cleanliness": "telemetry-consented",
                "provenance": {
                    "source": "telemetry",
                    "session_id": f"session-{i}",
                    "artifact_id": str(uuid.uuid4()),
                    "consent": "full",
                    "ts_derived": _ts(),
                },
                "_index": i,
            })

        loaded = stub.load(records)
        assert loaded == 50

        # Annotate: approve first 30, reject last 20
        def decide(rec: dict) -> str:
            return "approve" if rec["_index"] < 30 else "reject"

        counts = stub.annotate(decide)
        assert counts["approved"] == 30
        assert counts["rejected"] == 20

        # Snapshot: only approved records for IP-13 should appear
        snap_path = stub.snapshot(tmp_path, insertion_point="IP-13")
        assert snap_path.exists()

        snap_records = []
        for line in snap_path.read_text().splitlines():
            if line.strip():
                snap_records.append(json.loads(line))

        assert len(snap_records) == 30, f"Expected 30 approved, got {len(snap_records)}"

        # Rejected records must never appear in the snapshot
        for rec in snap_records:
            assert rec.get("_index", 0) < 30, "Rejected record found in snapshot!"

        # Internal decision field stripped from snapshot
        for rec in snap_records:
            assert "_decision" not in rec

    def test_rejected_records_never_in_snapshot(self, tmp_path):
        """Explicitly verify: rejected records are absent from snapshot."""
        stub = ArgillaStub()
        records = [
            {"record_type": "sft", "insertion_point": "IP-13",
             "legal_cleanliness": "telemetry-consented",
             "provenance": {"source": "telemetry", "session_id": "s", "artifact_id": "a",
                            "consent": "full", "ts_derived": _ts()},
             "_id": "good"},
            {"record_type": "sft", "insertion_point": "IP-13",
             "legal_cleanliness": "telemetry-consented",
             "provenance": {"source": "telemetry", "session_id": "s", "artifact_id": "b",
                            "consent": "full", "ts_derived": _ts()},
             "_id": "bad"},
        ]
        stub.load(records)
        stub.annotate(lambda r: "approve" if r["_id"] == "good" else "reject")
        snap = stub.snapshot(tmp_path, insertion_point="IP-13")

        snap_records = [json.loads(l) for l in snap.read_text().splitlines() if l.strip()]
        assert len(snap_records) == 1
        assert snap_records[0].get("_id") == "good"


# ---------------------------------------------------------------------------
# Dry run test
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_does_not_write_files(self, tmp_path):
        """Dry run scans and derives but writes no shard files."""
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "run-001"
        aid = str(uuid.uuid4())
        _write_run(run_dir, [
            _make_session_envelope(consent="full"),
            _make_artifact(artifact_id=aid),
            _make_action(artifact_id=aid, action="accepted"),
        ])

        out_dir = tmp_path / "staging"
        cfg = ExportConfig(
            runs_dir=runs_dir,
            out_dir=out_dir,
            app_config={"telemetry_consent": "full"},
            dry_run=True,
        )
        result = run_export(cfg)

        assert result.records_written >= 1  # counted but not written
        assert not out_dir.exists() or not list(out_dir.rglob("*.jsonl"))


# ---------------------------------------------------------------------------
# Resolve tier unit tests
# ---------------------------------------------------------------------------

class TestResolveTier:
    def test_offline_forces_off(self):
        assert _resolve_tier({"offline_mode": True, "telemetry_consent": "full"}) == "off"

    def test_unknown_tier_defaults_off(self):
        assert _resolve_tier({"telemetry_consent": "unknown_value"}) == "off"

    def test_missing_key_defaults_off(self):
        assert _resolve_tier({}) == "off"

    def test_meta_respected(self):
        assert _resolve_tier({"telemetry_consent": "meta"}) == "meta"

    def test_full_respected(self):
        assert _resolve_tier({"telemetry_consent": "full"}) == "full"
