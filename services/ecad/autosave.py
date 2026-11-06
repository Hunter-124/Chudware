"""Crash-recovery snapshots for native ECAD sessions.

Snapshots are private to a process session and committed durably before an older
recovery copy is removed. They are not a substitute for saving user documents.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import hashlib
import uuid
from pathlib import Path
from typing import Optional

_STATE: dict = {"enabled": False, "session_id": None, "namespace": uuid.uuid4().hex,
                "snapshots": {}}
_WRITE_LOCK = threading.Lock()


def enable(session_id: Optional[str] = None) -> None:
    """Arm autosave for this process. ``session_id`` tags every file so recovery can exclude our
    own freshly-written autosaves (only a PRIOR session's docs are the crash we're recovering)."""
    _STATE["enabled"] = True
    if session_id is not None:
        _STATE["session_id"] = session_id
        _STATE["namespace"] = uuid.uuid4().hex
        _STATE["snapshots"] = {}
    elif _STATE["session_id"] is None:
        _STATE["session_id"] = uuid.uuid4().hex


def disable() -> None:
    _STATE["enabled"] = False


def is_enabled() -> bool:
    return bool(_STATE["enabled"])


def session_id() -> Optional[str]:
    return _STATE["session_id"]


def autosave_dir() -> Path:
    """The directory autosaves live in. ``CHUDWARE_ECAD_AUTOSAVE_DIR`` overrides it wholesale (tests),
    else ``%APPDATA%/Chudware/ecad_autosave`` (falling back to the temp dir when APPDATA is unset)."""
    override = os.environ.get("CHUDWARE_ECAD_AUTOSAVE_DIR")
    if override:
        d = Path(override)
    else:
        base = os.environ.get("APPDATA") or tempfile.gettempdir()
        d = Path(base) / "Chudware" / "ecad_autosave"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_path(doc_id: str, kind: str = "sch", token: str = "") -> Path:
    identity = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
    suffix = f"-{token}" if token else ""
    return autosave_dir() / f"{os.getpid()}-{_STATE['namespace']}-{identity}{suffix}.kicad_{kind}"


def autosave_session(session) -> bool:
    """Persist a private KiCad recovery snapshot without rebinding the live session."""
    if not _STATE["enabled"] or not session.doc_id or session.kind not in ("sch", "pcb"):
        return False
    from services.mcp.ecad_common import _ECAD_OPERATION_LOCK, _rpc
    metadata = {
        "origin": "agent", "session": _STATE["session_id"], "pid": os.getpid(),
        "doc_id": session.doc_id, "kind": session.kind, "name": session.doc.get("name", ""),
        "path": session.path or "", "saved_at": time.time(),
        "recovery_dir": str(autosave_dir().resolve()),
    }
    snapshot = _snapshot_path(session.doc_id, session.kind, uuid.uuid4().hex)
    try:
        with _ECAD_OPERATION_LOCK, _WRITE_LOCK:
            _rpc("ecad_save", {"doc_id": session.doc_id, "path": str(snapshot),
                               "recovery": metadata})
            previous = _STATE["snapshots"].get(session.doc_id)
            _STATE["snapshots"][session.doc_id] = snapshot
            if previous and previous != snapshot:
                Path(str(previous) + ".recovery.json").unlink(missing_ok=True)
                previous.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001
        return False



def recover_ecad_autosaved(scope: str = "recent") -> list:
    """Reopen autosaved ECAD docs into the live session registry. Returns
    ``[{doc_id, kind, name, original_doc_id}]`` for each that reloaded (with a FRESH doc_id, exactly
    like cad_tools.recover_autosaved gives reopened C++ parts).

    ``scope="recent"`` selects the most recent prior session; ``scope="all"``
    includes all prior snapshots. Invalid files are retained.
    """
    from services.mcp.ecad_common import ecad_session as _sess

    out: list = []
    try:
        files = sorted(autosave_dir().glob("*.json"))
    except Exception:  # noqa: BLE001
        return out

    info: list = []  # (path, payload, session_tag, saved_at)
    for p in files:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - skip a half-written/corrupt file
            continue
        if not isinstance(payload, dict):
            continue
        is_recovery = p.name.endswith(".recovery.json")
        if is_recovery and payload.get("origin") != "agent":
            continue
        if not is_recovery and not isinstance(payload.get("doc"), dict):
            continue
        pid = payload.get("pid")
        if isinstance(pid, int) and pid > 0 and pid != os.getpid():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except OSError:
                continue
            else:
                continue
        try:
            ts = float(payload.get("saved_at", p.stat().st_mtime))
        except Exception:  # noqa: BLE001
            ts = 0.0
        info.append((p, payload, payload.get("session"), ts))

    if scope != "all":
        prior = [t for t in info if t[2] != _STATE["session_id"]]  # exclude our own session
        if prior:
            latest = max(prior, key=lambda t: t[3])[2]  # most-recent prior session's tag
            if latest is None:
                # untagged/legacy autosaves — take the newest few by mtime.
                prior.sort(key=lambda t: t[3], reverse=True)
                info = prior[:8]
            else:
                info = [t for t in prior if t[2] == latest]
        else:
            info = []

    for p, payload, _tag, _ts in info:
        try:
            if p.name.endswith(".recovery.json"):
                snapshot = Path(str(p)[:-len(".recovery.json")])
                if not snapshot.is_file():
                    continue
                session = _sess.open_path(str(snapshot))
            else:
                # Legacy JSON recovery files remain readable but are never written again.
                doc = payload["doc"]
                session = _sess.open_session(doc)
            out.append({"doc_id": session.doc_id, "kind": session.kind,
                        "name": session.doc.get("name", ""),
                        "original_doc_id": payload.get("doc_id")})
            if autosave_session(session):
                p.unlink(missing_ok=True)
                if p.name.endswith(".recovery.json"):
                    snapshot.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            continue
    return out
