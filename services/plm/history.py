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
KIND_MERGE = "merge"
ENTRY_KINDS = {KIND_AUTO, KIND_CHECKPOINT, KIND_RELEASE, KIND_RESTORE, KIND_MERGE}

_KIND_TRAILER = "chudware-plm-kind:"
_SCOPE_TRAILER = "chudware-plm-scope:"
PIN_TRAILER = "chudware-plm-pin:"

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
    participants: list[dict[str, Any]] | None = None,
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
    if participants is not None:
        meta["participants"] = participants
    return json.dumps(meta, indent=2, sort_keys=True).encode("utf-8")


def make_ai_block(
    *,
    provider: str,
    model: str,
    run_id: str | None,
    artifact_id: str | None,
    adapter: str | None = None,
) -> dict[str, Any]:
    """The §3.4 ``ai`` attribution block (joins agent telemetry). reversible=True in v1."""
    return {
        "provider": provider,
        "model": model,
        "adapter": adapter,
        "run_id": run_id or "",
        "artifact_id": artifact_id or "",
        "reversible": True,
    }


# A pre-rebrand vault wrote `aicad-plm-*` trailers. The rename to `chudware-plm-*`
# renamed the READERS too, so every legacy commit silently lost its kind and scope
# (a release rendered as `auto`) and leaked the raw trailer lines into the
# user-facing message on every timeline. Readers accept both spellings; writers
# emit only the current one.
_LEGACY_PREFIX = "aicad-plm-"
_CURRENT_PREFIX = "chudware-plm-"


def _trailer_value(message: str, trailer: str) -> str | None:
    """Return the value of `trailer` (current or legacy spelling), else None."""
    legacy = trailer.replace(_CURRENT_PREFIX, _LEGACY_PREFIX)
    for line in message.splitlines():
        line = line.strip()
        for prefix in (trailer, legacy):
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    return None


def _is_trailer_line(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(p) or s.startswith(p.replace(_CURRENT_PREFIX, _LEGACY_PREFIX))
               for p in (_KIND_TRAILER, _SCOPE_TRAILER, PIN_TRAILER))


def kind_from_commit_message(message: str) -> str:
    """Extract the entry kind from a commit message trailer (default 'auto')."""
    k = _trailer_value(message, _KIND_TRAILER)
    return k if k in ENTRY_KINDS else KIND_AUTO


def scope_from_commit_message(message: str) -> str:
    """Extract the entry scope from a commit trailer (default 'design')."""
    s = _trailer_value(message, _SCOPE_TRAILER)
    return s if s in SCOPES else SCOPE_DESIGN


def pin_from_commit_message(message: str) -> bool:
    """Return whether the commit is a release manifest back-fill pin."""
    return _trailer_value(message, PIN_TRAILER) == "1"


def user_message_from_commit(message: str) -> str:
    """Return the user-facing message (commit body with the trailers stripped)."""
    return "\n".join(ln for ln in message.splitlines()
                     if not _is_trailer_line(ln)).strip()
