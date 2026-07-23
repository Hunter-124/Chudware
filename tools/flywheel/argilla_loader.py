"""flywheel/argilla_loader.py — Argilla audit gate: load → approve → snapshot (T-H5).

Implements the human audit gate:
  1. Load exported JSONL shards into an Argilla dataset (queue for review).
  2. Poll for human approvals/rejections.
  3. Snapshot approved records only into corpus/<ip>/v1 DVC-tracked snapshots.
     Rejected records are never included in a snapshot.

The Argilla client is OPTIONAL — if argilla is not installed or the server
is not reachable, methods degrade gracefully (raises ArgillaUnavailable or
returns None). All code paths that matter for offline CI work without a
live Argilla server.

Argilla server: self-hosted (see tools/flywheel/docker-compose.argilla.yml).
  Remote URL + API key are injected via env vars:
    ARGILLA_API_URL   (default: http://localhost:6900)
    ARGILLA_API_KEY   (default: <see .env.argilla.example>)

Argilla client license: Apache-2.0 — in-process use is permitted.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Optional Argilla import — graceful degradation
# ---------------------------------------------------------------------------

try:
    import argilla as rg  # type: ignore[import]
    _ARGILLA_AVAILABLE = True
except ImportError:
    rg = None  # type: ignore[assignment]
    _ARGILLA_AVAILABLE = False


class ArgillaUnavailable(RuntimeError):
    """Raised when the Argilla client is not installed or server is unreachable."""


def _require_argilla() -> None:
    if not _ARGILLA_AVAILABLE:
        raise ArgillaUnavailable(
            "argilla package not installed. "
            "Run: pip install argilla  (Apache-2.0 license, in-process safe)"
        )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_ARGILLA_URL = "http://localhost:6900"
_DATASET_NAME = "chudware-flywheel"
_WORKSPACE = "admin"

# Labels used in the Argilla annotation task
_LABEL_APPROVE = "approve"
_LABEL_REJECT = "reject"


def _argilla_url() -> str:
    return os.environ.get("ARGILLA_API_URL", DEFAULT_ARGILLA_URL)


def _argilla_key() -> str:
    return os.environ.get("ARGILLA_API_KEY", "argilla.apikey")


# ---------------------------------------------------------------------------
# Loader: telemetry shards → Argilla queue
# ---------------------------------------------------------------------------

@dataclass
class LoadResult:
    records_loaded: int
    dataset_name: str
    workspace: str
    argilla_url: str


def load_shards_to_argilla(
    shard_paths: list[Path],
    dataset_name: str = _DATASET_NAME,
    workspace: str = _WORKSPACE,
) -> LoadResult:
    """Load JSONL shard records into Argilla for human review.

    Each record becomes an Argilla TextField showing the provenance + content,
    with a binary approve/reject label question.

    Args:
        shard_paths: List of JSONL shard files written by export.py.
        dataset_name: Target Argilla dataset name.
        workspace: Argilla workspace.

    Returns:
        LoadResult with counts.

    Raises:
        ArgillaUnavailable: if argilla not installed.
    """
    _require_argilla()

    client = rg.Argilla(
        api_url=_argilla_url(),
        api_key=_argilla_key(),
    )

    # Read all records from shards
    raw_records: list[dict] = []
    for shard_path in shard_paths:
        if not shard_path.exists():
            continue
        with shard_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        raw_records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    if not raw_records:
        return LoadResult(
            records_loaded=0,
            dataset_name=dataset_name,
            workspace=workspace,
            argilla_url=_argilla_url(),
        )

    # Get or create dataset
    try:
        dataset = client.datasets(name=dataset_name, workspace=workspace)
    except Exception:  # noqa: BLE001
        dataset = None

    if dataset is None:
        settings = rg.Settings(
            fields=[
                rg.TextField(name="content", title="Training Record"),
                rg.TextField(name="record_type", title="Record Type"),
                rg.TextField(name="insertion_point", title="Insertion Point"),
                rg.TextField(name="legal_cleanliness", title="Legal Cleanliness"),
            ],
            questions=[
                rg.LabelQuestion(
                    name="audit_decision",
                    title="Approve or reject this record for training?",
                    labels=[_LABEL_APPROVE, _LABEL_REJECT],
                    required=True,
                ),
                rg.TextQuestion(
                    name="reviewer_note",
                    title="Reviewer note (optional)",
                    required=False,
                ),
            ],
        )
        dataset = rg.Dataset(
            name=dataset_name,
            workspace=workspace,
            settings=settings,
            client=client,
        )
        dataset.create()

    # Build Argilla records
    argilla_records = []
    for rec in raw_records:
        content_str = json.dumps(rec, indent=2, ensure_ascii=False)
        ar = rg.Record(
            fields={
                "content": content_str[:10000],  # Argilla field size limit
                "record_type": str(rec.get("record_type", "unknown")),
                "insertion_point": str(rec.get("insertion_point", "unknown")),
                "legal_cleanliness": str(rec.get("legal_cleanliness", "unknown")),
            },
            metadata={
                "artifact_id": str(rec.get("artifact_id") or rec.get("provenance", {}).get("artifact_id", "")),
                "session_id": str(rec.get("provenance", {}).get("session_id", "")),
                "consent": str(rec.get("provenance", {}).get("consent", "")),
                "ts_derived": str(rec.get("provenance", {}).get("ts_derived", "")),
                "shard_source": str(shard_paths[0].name if shard_paths else ""),
            },
        )
        argilla_records.append(ar)

    dataset.records.log(argilla_records)

    return LoadResult(
        records_loaded=len(argilla_records),
        dataset_name=dataset_name,
        workspace=workspace,
        argilla_url=_argilla_url(),
    )


# ---------------------------------------------------------------------------
# Exporter: approved records → corpus snapshot
# ---------------------------------------------------------------------------

@dataclass
class SnapshotResult:
    snapshot_path: Path
    records_approved: int
    records_rejected: int
    records_unreviewed: int


def snapshot_approved(
    out_dir: Path,
    insertion_point: str,
    dataset_name: str = _DATASET_NAME,
    workspace: str = _WORKSPACE,
    version: str = "v1",
) -> SnapshotResult:
    """Fetch approved records from Argilla and write a corpus snapshot.

    Snapshot location: out_dir/corpus/<insertion_point>/<version>/approved_snapshot_<ts>.jsonl

    Rejected records NEVER enter the snapshot. Unreviewed records are skipped.

    Args:
        out_dir: Base output directory (DVC-tracked data repo root).
        insertion_point: IP label (e.g. "IP-13") to filter/shard by.
        dataset_name: Argilla dataset name.
        workspace: Argilla workspace.
        version: Corpus version label (default: "v1").

    Returns:
        SnapshotResult.

    Raises:
        ArgillaUnavailable: if argilla not installed.
    """
    _require_argilla()

    client = rg.Argilla(
        api_url=_argilla_url(),
        api_key=_argilla_key(),
    )

    dataset = client.datasets(name=dataset_name, workspace=workspace)
    if dataset is None:
        raise ValueError(f"Argilla dataset '{dataset_name}' not found in workspace '{workspace}'")

    approved_records = []
    n_rejected = 0
    n_unreviewed = 0

    for record in dataset.records(with_responses=True):
        # Filter by insertion_point
        record_ip = (record.fields or {}).get("insertion_point", "unknown")
        if record_ip != insertion_point:
            continue

        # Check annotation decision
        responses = getattr(record, "responses", None) or []
        decision = None
        for resp in responses:
            if hasattr(resp, "question_name") and resp.question_name == "audit_decision":
                decision = getattr(resp, "value", None)
                break

        if decision is None:
            n_unreviewed += 1
            continue

        if decision == _LABEL_REJECT:
            n_rejected += 1
            continue

        if decision == _LABEL_APPROVE:
            # Reconstruct the original record from the content field
            content_str = (record.fields or {}).get("content", "{}")
            try:
                original = json.loads(content_str)
            except json.JSONDecodeError:
                original = {"raw": content_str}
            # Stamp with audit metadata
            original["_argilla_audit"] = {
                "decision": _LABEL_APPROVE,
                "snapshot_ts": datetime.now(tz=timezone.utc).isoformat(),
            }
            approved_records.append(original)

    # Write snapshot
    snapshot_dir = out_dir / "corpus" / insertion_point / version
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap_id = str(uuid.uuid4())[:8]
    snapshot_path = snapshot_dir / f"approved_snapshot_{ts}_{snap_id}.jsonl"

    with snapshot_path.open("w", encoding="utf-8") as fh:
        for record in approved_records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return SnapshotResult(
        snapshot_path=snapshot_path,
        records_approved=len(approved_records),
        records_rejected=n_rejected,
        records_unreviewed=n_unreviewed,
    )


# ---------------------------------------------------------------------------
# Stub validator — for offline CI / testing without a live Argilla server
# ---------------------------------------------------------------------------

class ArgillaStub:
    """In-memory stub of the Argilla audit gate for offline testing.

    Simulates the load → review → snapshot cycle without a live server.
    Records are approved or rejected based on a provided decision_fn.
    """

    def __init__(self) -> None:
        self._queue: list[dict] = []

    def load(self, records: list[dict]) -> int:
        """Queue records for review. Returns count loaded."""
        self._queue.extend(records)
        return len(records)

    def annotate(self, decision_fn: Any) -> dict[str, int]:
        """Apply decision_fn(record) → 'approve'|'reject' to all queued records.

        Returns counts.
        """
        results = {"approved": 0, "rejected": 0}
        for rec in self._queue:
            decision = decision_fn(rec)
            rec["_decision"] = decision
            if decision == "approve":
                results["approved"] += 1
            else:
                results["rejected"] += 1
        return results

    def snapshot(self, out_dir: Path, insertion_point: str, version: str = "v1") -> Path:
        """Write approved records to a corpus snapshot. Returns snapshot path."""
        approved = [
            r for r in self._queue
            if r.get("_decision") == "approve"
            and r.get("insertion_point") == insertion_point
        ]
        snap_dir = out_dir / "corpus" / insertion_point / version
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = snap_dir / f"approved_snapshot_{ts}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for rec in approved:
                # Strip internal decision field before writing
                clean = {k: v for k, v in rec.items() if k != "_decision"}
                fh.write(json.dumps(clean, ensure_ascii=False) + "\n")
        return path
