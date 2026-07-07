"""services.plm.history — history-entry semantics (contract §3).

Pure helpers for the four entry kinds and the per-item ``meta.json`` (§3.4).
The vault engine (``vault.py``) cuts commits; this module owns the metadata
shapes and the parsing of a commit back into a timeline entry.
"""
from __future__ import annotations

import json
from typing import Any

# the four entry kinds (contract §3.2)
KIND_AUTO = "auto"
KIND_CHECKPOINT = "checkpoint"
KIND_RELEASE = "release"
KIND_RESTORE = "restore"
ENTRY_KINDS = {KIND_AUTO, KIND_CHECKPOINT, KIND_RELEASE, KIND_RESTORE}

_KIND_TRAILER = "aicad-plm-kind:"
_SCOPE_TRAILER = "aicad-plm-scope:"

# entry scope: design history (the timeline users watch) vs planning-store
# mutations (tasks/issues/milestones), so the default timeline stays curated.
SCOPE_DESIGN = "design"
SCOPE_PLANNING = "planning"
SCOPES = {SCOPE_DESIGN, SCOPE_PLANNING}


def make_meta(
    *,
    item_id: str,
    entry_kind: str,
    authored_at: str,
    author_kind: str,
    author_id: str,
    author_display: str,
    message: str,
    doc_uuid: str,
    rev: str | None,
    lifecycle: str,
    ai: dict[str, Any] | None = None,
) -> bytes:
    """Build the per-item ``meta.json`` blob (contract §3.4).

    Timestamps + attribution live ONLY here (and in commit metadata), NEVER in
    ``document.json`` (the byte-stability law, §0).
    """
    meta = {
        "item_id": item_id,
        "entry_kind": entry_kind,
        "authored_at": authored_at,
        "author": {"kind": author_kind, "id": author_id, "display": author_display},
        "message": message,
        "ai": ai,  # None for user entries; §3.4 block for AI entries
        "doc_uuid": doc_uuid,
        "rev": rev,
        "lifecycle": lifecycle,
    }
    return json.dumps(meta, indent=2, sort_keys=True).encode("utf-8")


def make_ai_block(
    *,
    provider: str,
    model: str,
    run_id: str | None,
    artifact_id: str | None,
    adapter: str | None = None,
) -> dict[str, Any]:
    """The §3.4 ``ai`` attribution block (joins T-H1 telemetry). reversible=True in v1."""
    return {
        "provider": provider,
        "model": model,
        "adapter": adapter,
        "run_id": run_id or "",
        "artifact_id": artifact_id or "",
        "reversible": True,
    }


def kind_from_commit_message(message: str) -> str:
    """Extract the entry kind from a commit message trailer (default 'auto')."""
    for line in message.splitlines():
        line = line.strip()
        if line.startswith(_KIND_TRAILER):
            k = line[len(_KIND_TRAILER):].strip()
            if k in ENTRY_KINDS:
                return k
    return KIND_AUTO


def scope_from_commit_message(message: str) -> str:
    """Extract the entry scope from a commit trailer (default 'design')."""
    for line in message.splitlines():
        line = line.strip()
        if line.startswith(_SCOPE_TRAILER):
            s = line[len(_SCOPE_TRAILER):].strip()
            if s in SCOPES:
                return s
    return SCOPE_DESIGN


def user_message_from_commit(message: str) -> str:
    """Return the user-facing message (commit body with the trailers stripped)."""
    lines = [ln for ln in message.splitlines()
             if not ln.strip().startswith(_KIND_TRAILER)
             and not ln.strip().startswith(_SCOPE_TRAILER)]
    return "\n".join(lines).strip()
