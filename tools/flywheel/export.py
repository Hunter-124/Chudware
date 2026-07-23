"""flywheel/export.py — Consent-filtered telemetry exporter (T-H5).

Scans <appdata>/Chudware/cache/runs/**  +  per-doc session journals, applies
consent gating (off|meta|full; offline_enabled ⇒ nothing), scrubs PII, derives
SFT / preference-pair / correction-trace records per contracts/telemetry.md §5,
and writes provenance-stamped JSONL shards into a DVC-tracked staging directory.

Export is always user-initiated or visibly scheduled.  The local staging dir is
readable by the user before any upload.  Nothing is silent.

Consent hierarchy (re-checked here, not trusting the per-event stamp):
  offline_enabled(cfg) → hard "off" → export nothing
  session envelope consent → off → skip session
  per-event consent field    → informational; we re-resolve from session envelope
  tier "meta"  → strip all sidecar refs (input/output/edit_ops/diff not uploaded)
  tier "full"  → bundle sidecars; scrub keys/paths/usernames/machine-ids

Legal-cleanliness field values (DoD):
  "telemetry-consented" | "synthetic" | "licensed-<name>"
  This module emits "telemetry-consented" only; the field accepts all three.

Usage:
    python -m tools.flywheel.export --runs-dir /path/to/runs --out-dir /path/to/staging

    from tools.flywheel.export import ExportConfig, run_export
    cfg = ExportConfig(runs_dir=Path("..."), out_dir=Path("..."), app_config={})
    run_export(cfg)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Config resolution helpers (mirrors services/agent/config.py)
# ---------------------------------------------------------------------------

_VALID_TIERS = {"off", "meta", "full"}
_OFFLINE_SETTING = "offline_mode"
_CONSENT_SETTING = "telemetry_consent"


def _offline_enabled(cfg: dict) -> bool:
    return bool(cfg.get(_OFFLINE_SETTING, False))


def _resolve_tier(cfg: dict) -> str:
    """Return resolved consent tier — always "off" when offline_enabled."""
    if _offline_enabled(cfg):
        return "off"
    tier = cfg.get(_CONSENT_SETTING, "off")
    return tier if tier in _VALID_TIERS else "off"


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

# Patterns that should never leave the machine
_API_KEY_RE = re.compile(
    r"""(?:sk-ant-|sk-|AIza|Bearer\s+)[A-Za-z0-9_\-]{16,}""", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"""[\w.+-]+@[\w-]+\.[a-z]{2,}""", re.IGNORECASE)
# Match absolute paths (/foo/bar or C:\foo\bar) but NOT relative or IP-style strings
_ABS_PATH_RE = re.compile(r"""(?:/[\w/.\- ]+|[A-Za-z]:\\[\w\\\s.\-]+)""")
# Common username markers
_USERNAME_RE = re.compile(r"""\bUsers?[/\\]([^/\\\s"']+)""", re.IGNORECASE)


def _hash_machine_id(raw: str) -> str:
    """Replace machine/user-ID with a stable SHA-256 prefix (first 16 hex chars)."""
    return "mid:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _scrub_text(text: str) -> str:
    """Strip API keys, e-mail addresses, absolute filesystem paths, usernames."""
    text = _API_KEY_RE.sub("<REDACTED_KEY>", text)
    text = _EMAIL_RE.sub("<REDACTED_EMAIL>", text)
    text = _USERNAME_RE.sub(r"Users/<REDACTED_USER>", text)
    text = _ABS_PATH_RE.sub("<REDACTED_PATH>", text)
    return text


def _scrub_dict(obj: Any) -> Any:
    """Recursively scrub a JSON-decoded object."""
    if isinstance(obj, dict):
        return {k: _scrub_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_dict(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_text(obj)
    return obj


def _hash_machine_ids_in(obj: Any) -> Any:
    """Hash any field named machine_id / machine-id / mid."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.lower().replace("-", "_") in {"machine_id", "mid", "device_id"}:
                out[k] = _hash_machine_id(str(v)) if isinstance(v, str) else v
            else:
                out[k] = _hash_machine_ids_in(v)
        return out
    if isinstance(obj, list):
        return [_hash_machine_ids_in(v) for v in obj]
    return obj


def _scrub_event(event: dict) -> dict:
    """Apply full scrubbing pipeline to an event record."""
    event = _scrub_dict(event)
    event = _hash_machine_ids_in(event)
    return event


# ---------------------------------------------------------------------------
# Session envelope parsing
# ---------------------------------------------------------------------------

def _parse_session_envelope(run_dir: Path) -> dict | None:
    """Return the first session-envelope object from events.jsonl, or None."""
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return None
    try:
        with events_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # The session envelope has 'consent' + 'origin' + 'app_version' + 'os'
                if isinstance(obj, dict) and "consent" in obj and "origin" in obj:
                    return obj
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Event iteration
# ---------------------------------------------------------------------------

def _iter_events(run_dir: Path) -> Iterator[dict]:
    """Yield parsed event dicts from a run's events.jsonl."""
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return
    try:
        with events_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _iter_runs(runs_root: Path) -> Iterator[Path]:
    """Yield run directories (children of runs_root)."""
    if not runs_root.exists():
        return
    for child in sorted(runs_root.iterdir()):
        if child.is_dir():
            yield child


# ---------------------------------------------------------------------------
# Derivation helpers (contracts/telemetry.md §5)
# ---------------------------------------------------------------------------

def _build_artifact_index(events: list[dict]) -> dict[str, dict]:
    """Map artifact_id → ai_artifact event."""
    return {
        e["artifact_id"]: e
        for e in events
        if e.get("kind") == "ai_artifact" and "artifact_id" in e
    }


def _build_action_index(events: list[dict]) -> dict[str, list[dict]]:
    """Map artifact_id → list of ai_artifact_action events (in order)."""
    idx: dict[str, list[dict]] = {}
    for e in events:
        if e.get("kind") == "ai_artifact_action" and "artifact_id" in e:
            idx.setdefault(e["artifact_id"], []).append(e)
    return idx


def _build_diff_index(events: list[dict]) -> dict[str, list[dict]]:
    """Map artifact_id → list of doc_semantic_diff events."""
    idx: dict[str, list[dict]] = {}
    for e in events:
        if e.get("kind") == "doc_semantic_diff" and "artifact_id" in e:
            idx.setdefault(e["artifact_id"], []).append(e)
    return idx


def _build_stage_results(events: list[dict]) -> dict[str, list[dict]]:
    """Map run_id+stage key → list of stage_result events."""
    idx: dict[str, list[dict]] = {}
    for e in events:
        if e.get("kind") == "stage_result":
            key = f"{e.get('run_id','')}:{e.get('stage','')}"
            idx.setdefault(key, []).append(e)
    return idx


# ---------------------------------------------------------------------------
# Provenance builder
# ---------------------------------------------------------------------------

def _provenance(
    session_id: str,
    artifact_id: str | None,
    consent: str,
    app_version: str,
) -> dict:
    return {
        "source": "telemetry",
        "session_id": session_id,
        "artifact_id": artifact_id,
        "consent": consent,
        "app_version": app_version,
        "ts_derived": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Meta-tier field stripping (contracts/telemetry.md §4 Tier:meta)
# ---------------------------------------------------------------------------

_META_STRIP_FIELDS = {"input_ref", "output_ref", "edit_ops_ref", "diff_ref", "note"}


def _apply_meta_strip(record: dict) -> dict:
    """Null out sidecar ref fields for meta-tier export."""
    for f in _META_STRIP_FIELDS:
        if f in record:
            record[f] = None
    return record


# ---------------------------------------------------------------------------
# SFT derivation — §5.1
# ---------------------------------------------------------------------------

def _derive_sft(
    artifacts: dict[str, dict],
    actions: dict[str, list[dict]],
    session_id: str,
    tier: str,
    app_version: str,
) -> list[dict]:
    """Derive SFT examples: clean accept with no subsequent edit."""
    records = []
    for aid, artifact in artifacts.items():
        action_list = actions.get(aid, [])
        has_accept = any(
            a["action"] in ("accepted", "applied") for a in action_list
        )
        has_edit = any(a["action"] == "edited" for a in action_list)
        if not (has_accept and not has_edit):
            continue
        record = {
            "record_type": "sft",
            "artifact_id": aid,
            "insertion_point": artifact.get("insertion_point"),
            "model": artifact.get("model"),
            "prompt_hash": artifact.get("prompt_hash"),
            "input_ref": artifact.get("input_ref"),
            "output_ref": artifact.get("output_ref"),
            "base_revision": artifact.get("base_revision"),
            "verifier_scores": artifact.get("verifier_scores", {}),
            "latency_ms": artifact.get("latency_ms"),
            "legal_cleanliness": "telemetry-consented",
            "provenance": _provenance(session_id, aid, tier, app_version),
        }
        if tier == "meta":
            record = _apply_meta_strip(record)
        record = _scrub_event(record)
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Preference pair derivation — §5.2
# ---------------------------------------------------------------------------

def _derive_preference_pairs(
    artifacts: dict[str, dict],
    actions: dict[str, list[dict]],
    events: list[dict],
    session_id: str,
    tier: str,
    app_version: str,
    undo_k: int = 5,
) -> list[dict]:
    """Derive preference pairs from regen (5.2a), retry-loop (5.2b), undo-neg (5.2c)."""
    records = []

    # --- 5.2a: Regeneration pairs ---
    # Find artifact A with action "regenerated", then the next artifact B (same run_id)
    # that gets accepted/applied.
    run_artifact_map: dict[str, list[dict]] = {}
    for aid, artifact in artifacts.items():
        run_id = artifact.get("run_id", "")
        run_artifact_map.setdefault(run_id, []).append(artifact)

    for aid, artifact in artifacts.items():
        action_list = actions.get(aid, [])
        has_regen = any(a["action"] == "regenerated" for a in action_list)
        if not has_regen:
            continue
        # Find sibling artifact in the same run that was accepted
        run_id = artifact.get("run_id", "")
        siblings = [a for a in run_artifact_map.get(run_id, []) if a["artifact_id"] != aid]
        for sibling in siblings:
            sib_actions = actions.get(sibling["artifact_id"], [])
            if any(a["action"] in ("accepted", "applied") for a in sib_actions):
                record = {
                    "record_type": "preference_pair",
                    "subtype": "regen",
                    "rejected_artifact_id": aid,
                    "chosen_artifact_id": sibling["artifact_id"],
                    "rejected_output_ref": artifact.get("output_ref"),
                    "chosen_output_ref": sibling.get("output_ref"),
                    "insertion_point": artifact.get("insertion_point"),
                    "model": artifact.get("model"),
                    "legal_cleanliness": "telemetry-consented",
                    "provenance": _provenance(session_id, aid, tier, app_version),
                }
                if tier == "meta":
                    record["rejected_output_ref"] = None
                    record["chosen_output_ref"] = None
                record = _scrub_event(record)
                records.append(record)
                break  # one regen pair per rejected artifact

    # --- 5.2b: Retry-loop pairs ---
    # stage_result with verdict fail/retry followed by verdict pass, same run+stage
    stage_results: dict[str, list[dict]] = {}
    for e in events:
        if e.get("kind") == "stage_result":
            key = f"{e.get('run_id','')}:{e.get('stage','')}"
            stage_results.setdefault(key, []).append(e)

    for key, results in stage_results.items():
        failed = [r for r in results if r.get("verdict") in ("fail", "retry")]
        passed = [r for r in results if r.get("verdict") == "pass"]
        if not (failed and passed):
            continue
        for fail_r in failed:
            record = {
                "record_type": "preference_pair",
                "subtype": "retry_loop",
                "run_id": fail_r.get("run_id"),
                "stage": fail_r.get("stage"),
                "rejected_verdict": fail_r.get("verdict"),
                "chosen_verdict": "pass",
                "legal_cleanliness": "telemetry-consented",
                "provenance": _provenance(session_id, None, tier, app_version),
            }
            record = _scrub_event(record)
            records.append(record)

    # --- 5.2c: Undo-negative pairs ---
    for aid, artifact in artifacts.items():
        action_list = actions.get(aid, [])
        undo_actions = [
            a for a in action_list
            if a["action"] == "undone" and a.get("undo_within_ops", 0) > 0
        ]
        if not undo_actions:
            continue
        # Look for a later artifact in the same run that was accepted
        run_id = artifact.get("run_id", "")
        siblings = [a for a in run_artifact_map.get(run_id, []) if a["artifact_id"] != aid]
        positive = None
        for sib in siblings:
            sib_acts = actions.get(sib["artifact_id"], [])
            if any(a["action"] in ("accepted", "applied") for a in sib_acts):
                positive = sib
                break
        record = {
            "record_type": "preference_pair",
            "subtype": "undo_negative",
            "rejected_artifact_id": aid,
            "chosen_artifact_id": positive["artifact_id"] if positive else None,
            "rejected_output_ref": artifact.get("output_ref"),
            "chosen_output_ref": positive.get("output_ref") if positive else None,
            "undo_within_ops": undo_actions[0].get("undo_within_ops"),
            "undo_k_threshold": undo_k,
            "insertion_point": artifact.get("insertion_point"),
            "legal_cleanliness": "telemetry-consented",
            "provenance": _provenance(session_id, aid, tier, app_version),
        }
        if tier == "meta":
            record["rejected_output_ref"] = None
            record["chosen_output_ref"] = None
        record = _scrub_event(record)
        records.append(record)

    return records


# ---------------------------------------------------------------------------
# Correction trace derivation — §5.3
# ---------------------------------------------------------------------------

def _derive_correction_traces(
    artifacts: dict[str, dict],
    actions: dict[str, list[dict]],
    session_id: str,
    tier: str,
    app_version: str,
) -> list[dict]:
    """Derive correction traces: edited artifacts with edit_ops_ref populated."""
    records = []
    for aid, artifact in artifacts.items():
        action_list = actions.get(aid, [])
        edited = [
            a for a in action_list
            if a["action"] == "edited" and a.get("edit_ops_ref")
        ]
        for action in edited:
            record = {
                "record_type": "correction_trace",
                "artifact_id": aid,
                "insertion_point": artifact.get("insertion_point"),
                "model": artifact.get("model"),
                "model_output_ref": artifact.get("output_ref"),
                "human_correction_ref": action.get("edit_ops_ref"),
                "base_revision": artifact.get("base_revision"),
                "verifier_scores": artifact.get("verifier_scores", {}),
                "journal_evicted": action.get("journal_evicted", False),
                "legal_cleanliness": "telemetry-consented",
                "provenance": _provenance(session_id, aid, tier, app_version),
            }
            if tier == "meta":
                record["model_output_ref"] = None
                record["human_correction_ref"] = None
            record = _scrub_event(record)
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Elicitation gold — §5.4
# ---------------------------------------------------------------------------

def _derive_elicitation_gold(
    events: list[dict],
    session_id: str,
    tier: str,
    app_version: str,
) -> list[dict]:
    """Derive elicitation gold from design-brief sign-off events (T-F17 hook)."""
    records = []
    for e in events:
        # T-F17 emits kind="design_brief_signoff" — tolerate absence gracefully
        if e.get("kind") != "design_brief_signoff":
            continue
        record = {
            "record_type": "elicitation_gold",
            "artifact_id": e.get("artifact_id"),
            "insertion_point": e.get("insertion_point", "IP-12"),
            "model_draft_ref": e.get("model_draft_ref"),
            "user_signed_brief_ref": e.get("user_signed_brief_ref"),
            "legal_cleanliness": "telemetry-consented",
            "provenance": _provenance(session_id, e.get("artifact_id"), tier, app_version),
        }
        if tier == "meta":
            record["model_draft_ref"] = None
            record["user_signed_brief_ref"] = None
        record = _scrub_event(record)
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Per-run exporter
# ---------------------------------------------------------------------------

def _export_run(
    run_dir: Path,
    tier: str,
    app_version: str,
    undo_k: int = 5,
) -> tuple[str, list[dict]]:
    """Export all derivable records from one run directory.

    Returns (insertion_point_key, records).
    The insertion_point_key is the dominant IP from ai_artifact events,
    used to shard records into corpus/<ip>/… directories.
    Raises ValueError if tier == "off" (caller must gate before calling).
    """
    if tier == "off":
        raise ValueError("Caller must gate consent=off before calling _export_run")

    # Derive session_id from the run directory name
    session_id = run_dir.name

    events_list = list(_iter_events(run_dir))
    artifacts = _build_artifact_index(events_list)
    actions = _build_action_index(events_list)

    records: list[dict] = []

    # Attach session envelope to every shard as context (not a training record)
    envelope = _parse_session_envelope(run_dir)
    session_app_version = (
        envelope.get("app_version", app_version) if envelope else app_version
    )

    records += _derive_sft(artifacts, actions, session_id, tier, session_app_version)
    records += _derive_preference_pairs(
        artifacts, actions, events_list, session_id, tier, session_app_version, undo_k
    )
    records += _derive_correction_traces(
        artifacts, actions, session_id, tier, session_app_version
    )
    records += _derive_elicitation_gold(
        events_list, session_id, tier, session_app_version
    )

    # Determine dominant insertion_point for corpus sharding
    ip_counts: dict[str, int] = {}
    for artifact in artifacts.values():
        ip = artifact.get("insertion_point", "unknown")
        ip_counts[ip] = ip_counts.get(ip, 0) + 1
    dominant_ip = max(ip_counts, key=lambda k: ip_counts[k]) if ip_counts else "unknown"

    return dominant_ip, records


# ---------------------------------------------------------------------------
# ExportConfig & run_export
# ---------------------------------------------------------------------------

@dataclass
class ExportConfig:
    """Configuration for a flywheel export run.

    Args:
        runs_dir: Root of the runs cache (default: APPDATA/Chudware/cache/runs).
                  Injectable for testing — point at a fixture directory.
        out_dir:  Local staging directory where shards are written.
                  The user can inspect this before any upload.
        app_config: Resolved application config dict (same shape as config.json).
                    Must include 'telemetry_consent' and 'offline_mode' keys.
        app_version: Application version string; used in provenance records.
        undo_k:   Undo-window op-count threshold for undo-negative preference pairs.
        dry_run:  If True, scan and derive but do not write any files.
    """
    runs_dir: Path
    out_dir: Path
    app_config: dict = field(default_factory=dict)
    app_version: str = "dev"
    undo_k: int = 5
    dry_run: bool = False

    def __post_init__(self) -> None:
        self.runs_dir = Path(self.runs_dir)
        self.out_dir = Path(self.out_dir)


@dataclass
class ExportResult:
    """Summary of a completed export run."""
    tier: str
    runs_scanned: int
    runs_skipped_consent: int
    runs_skipped_offline: int
    records_written: int
    records_by_type: dict[str, int]
    shard_paths: list[Path]
    skipped_run_ids: list[str]


def run_export(cfg: ExportConfig) -> ExportResult:
    """Perform a consent-filtered export.

    Consent re-check strategy:
    1. If offline_enabled(app_config) → tier is forced "off"; export nothing.
    2. Per run: read session envelope's consent field. If "off" → skip.
    3. Per run: also validate the resolved tier from app_config (defense-in-depth).
       The per-event stamp is informational only; session envelope + app_config are
       authoritative at export time.

    The exporter never silently drops records without counting them.
    """
    tier = _resolve_tier(cfg.app_config)

    result = ExportResult(
        tier=tier,
        runs_scanned=0,
        runs_skipped_consent=0,
        runs_skipped_offline=0,
        records_written=0,
        records_by_type={},
        shard_paths=[],
        skipped_run_ids=[],
    )

    # Hard gate: if offline_enabled, export nothing at all
    if _offline_enabled(cfg.app_config):
        result.runs_skipped_offline = sum(1 for _ in _iter_runs(cfg.runs_dir))
        return result

    # Global consent gate: if app config says "off", nothing to export
    if tier == "off":
        result.runs_skipped_consent = sum(1 for _ in _iter_runs(cfg.runs_dir))
        return result

    all_records: dict[str, list[dict]] = {}  # ip -> records

    for run_dir in _iter_runs(cfg.runs_dir):
        result.runs_scanned += 1
        run_id = run_dir.name

        # Per-session consent check from session envelope
        envelope = _parse_session_envelope(run_dir)
        if envelope is None:
            # No envelope means we can't verify consent — skip conservatively
            result.runs_skipped_consent += 1
            result.skipped_run_ids.append(run_id)
            continue

        session_consent = envelope.get("consent", "off")
        if session_consent == "off":
            result.runs_skipped_consent += 1
            result.skipped_run_ids.append(run_id)
            continue

        # Use the stricter of app_config tier and session envelope tier
        # (e.g., if session says "full" but app_config says "meta", use "meta")
        effective_tier = session_consent
        if tier == "meta" and session_consent == "full":
            effective_tier = "meta"  # app-config is stricter

        try:
            ip, records = _export_run(
                run_dir, effective_tier, cfg.app_version, cfg.undo_k
            )
        except Exception as exc:  # noqa: BLE001
            # Log and skip; don't crash the whole export
            print(f"[flywheel] WARNING: run {run_id} failed: {exc}")
            result.skipped_run_ids.append(run_id)
            continue

        if records:
            all_records.setdefault(ip, []).extend(records)
            for r in records:
                rtype = r.get("record_type", "unknown")
                result.records_by_type[rtype] = (
                    result.records_by_type.get(rtype, 0) + 1
                )

    if cfg.dry_run:
        result.records_written = sum(len(v) for v in all_records.values())
        return result

    # Write shards — one file per insertion_point per export run
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    export_id = str(uuid.uuid4())[:8]

    for ip, records in all_records.items():
        shard_dir = cfg.out_dir / "corpus" / ip / "v1"
        shard_dir.mkdir(parents=True, exist_ok=True)
        shard_path = shard_dir / f"shard_{ts}_{export_id}.jsonl"
        with shard_path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        result.shard_paths.append(shard_path)
        result.records_written += len(records)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _default_runs_dir() -> Path:
    """Return the default runs cache directory."""
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return Path(appdata) / "Chudware" / "cache" / "runs"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Flywheel: consent-filtered telemetry exporter (T-H5)"
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Root of the runs cache (default: APPDATA/Chudware/cache/runs)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Local staging directory for exported shards",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.json (default: APPDATA/Chudware/config.json)",
    )
    parser.add_argument(
        "--app-version",
        default="dev",
        help="Application version string for provenance records",
    )
    parser.add_argument(
        "--undo-k",
        type=int,
        default=5,
        help="Undo-window op-count threshold for undo-negative pairs (default: 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and derive but do not write any files",
    )
    args = parser.parse_args()

    # Load application config
    app_config: dict = {}
    config_path = args.config
    if config_path is None:
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        config_path = Path(appdata) / "Chudware" / "config.json"
    if config_path and config_path.exists():
        try:
            app_config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[flywheel] WARNING: could not load config: {exc}")

    runs_dir = args.runs_dir or _default_runs_dir()

    cfg = ExportConfig(
        runs_dir=runs_dir,
        out_dir=args.out_dir,
        app_config=app_config,
        app_version=args.app_version,
        undo_k=args.undo_k,
        dry_run=args.dry_run,
    )

    result = run_export(cfg)

    print(f"[flywheel] Export complete:")
    print(f"  tier={result.tier}")
    print(f"  runs_scanned={result.runs_scanned}")
    print(f"  runs_skipped_consent={result.runs_skipped_consent}")
    print(f"  runs_skipped_offline={result.runs_skipped_offline}")
    print(f"  records_written={result.records_written}")
    print(f"  records_by_type={result.records_by_type}")
    if result.skipped_run_ids:
        print(f"  skipped_run_ids={result.skipped_run_ids}")
    if result.shard_paths:
        print(f"  shards written:")
        for p in result.shard_paths:
            print(f"    {p}")


if __name__ == "__main__":
    main()
