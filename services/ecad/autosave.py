"""Crash-recovery autosave for ECAD sessions (the ECAD twin of mcp_server.cad_tools autosave).

ECAD documents live ONLY in memory — the :mod:`engine.ecad.session` registry has no on-disk
backing. A heavy MCP tool (ecad_simulate / ecad_make_board / ecad_design / autoroute / fill / dfm)
can overrun the MCP client's per-call transport timeout; the client then KILLS the server and every
open schematic/board is gone. C++ part docs survive that because cad_tools autosaves them to
``%APPDATA%/Chudware/mcp_autosave`` and ``recover_autosaved`` reloads them on restart. This module
gives ECAD the same protection: every session is persisted to
``%APPDATA%/Chudware/ecad_autosave/<doc_id>.json`` after every mutation, and
:func:`recover_ecad_autosaved` reopens them at server startup.

Design constraints:
  * PURE ENGINE — knows only the document JSON + a directory; no MCP/ctypes deps, so it is testable
    with a standalone script (build a session, mutate, recover).
  * BEST-EFFORT — a write/read failure is swallowed; autosave must never break the op that succeeded.
  * DISABLED BY DEFAULT — unit tests don't touch disk. The MCP server calls :func:`enable` at
    startup; tests enable() with an env-overridden directory (``CHUDWARE_ECAD_AUTOSAVE_DIR``).
  * ATOMIC writes (tmp + os.replace) so a kill mid-write can't leave a half-written JSON that fails
    to reload.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

# Module state: whether autosave is armed + this process's session id (so recovery can tell our own
# fresh autosaves apart from a prior crashed session's, exactly like cad_tools._SESSION_ID does).
_STATE: dict = {"enabled": False, "session_id": None}

# Autosaves older than this are pruned on recovery so the directory can't grow forever.
try:
    _MAX_AGE_DAYS = float(os.environ.get("CHUDWARE_ECAD_AUTOSAVE_MAX_AGE_DAYS", "") or 7.0)
except (TypeError, ValueError):
    _MAX_AGE_DAYS = 7.0


def enable(session_id: Optional[str] = None) -> None:
    """Arm autosave for this process. ``session_id`` tags every file so recovery can exclude our
    own freshly-written autosaves (only a PRIOR session's docs are the crash we're recovering)."""
    _STATE["enabled"] = True
    if session_id is not None:
        _STATE["session_id"] = session_id


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


def save_doc(doc_id: Optional[str], doc: dict, *, kind: Optional[str] = None,
             name: Optional[str] = None, revision: Optional[int] = None,
             path: Optional[str] = None) -> None:
    """Persist one ECAD document (atomically) to ``<doc_id>.json``. No-op when disabled. Never
    raises — a persistence failure must not break the mutating op that already succeeded."""
    if not _STATE["enabled"] or not doc_id or not isinstance(doc, dict):
        return
    try:
        d = autosave_dir()
        payload = {
            "session": _STATE["session_id"],
            "doc_id": doc_id,
            "kind": kind,
            "name": name,
            "revision": revision,
            "path": path,
            "saved_at": time.time(),
            "doc": doc,
        }
        tmp = d / f".{doc_id}.json.tmp"
        final = d / f"{doc_id}.json"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, final)  # atomic on POSIX + Windows
    except Exception:  # noqa: BLE001 - best-effort
        pass


def autosave_session(session) -> None:
    """Persist an :class:`engine.ecad.session.EcadSession`. Called from the session mutation points
    (apply_ops / swap_doc / undo / redo / new / open). No-op when disabled."""
    if not _STATE["enabled"]:
        return
    try:
        save_doc(session.doc_id, session.doc, kind=session.kind,
                 name=session.doc.get("name"), revision=session.revision, path=session.path)
    except Exception:  # noqa: BLE001
        pass


def remove(doc_id: Optional[str]) -> None:
    """Delete a doc's autosave (called when a session is deliberately closed, so a closed doc is
    never resurrected on the next restart). Best-effort."""
    if not doc_id:
        return
    try:
        (autosave_dir() / f"{doc_id}.json").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# Hard cap on the number of autosave files kept, regardless of age. Recovery reopens prior-session
# docs under FRESH ids and re-saves them, so without a cap the directory grows by ~N docs every
# restart and old-session files (younger than the age cutoff) never get cleaned. Keep only the most
# recent few dozen by mtime.
try:
    _MAX_FILES = int(os.environ.get("CHUDWARE_ECAD_AUTOSAVE_MAX_FILES", "") or 40)
except (TypeError, ValueError):
    _MAX_FILES = 40
if _MAX_FILES < 1:
    _MAX_FILES = 40


def prune(max_age_days: float = _MAX_AGE_DAYS, max_files: int = _MAX_FILES) -> int:
    """Delete autosaves older than ``max_age_days`` (by mtime), then cap the directory to the
    ``max_files`` most-recent files. Returns the count removed."""
    n = 0
    cutoff = time.time() - max_age_days * 86400.0
    try:
        survivors: list = []
        for p in autosave_dir().glob("*.json"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt < cutoff:
                p.unlink(missing_ok=True)
                n += 1
            else:
                survivors.append((mt, p))
        # Count-cap: drop the oldest survivors beyond the limit so the dir can't grow unbounded
        # across restarts (the recovery-churn fix).
        if max_files and len(survivors) > max_files:
            survivors.sort(key=lambda t: t[0], reverse=True)
            for _mt, p in survivors[max_files:]:
                try:
                    p.unlink(missing_ok=True)
                    n += 1
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return n


def recover_ecad_autosaved(scope: str = "recent") -> list:
    """Reopen autosaved ECAD docs into the live session registry. Returns
    ``[{doc_id, kind, name, original_doc_id}]`` for each that reloaded (with a FRESH doc_id, exactly
    like cad_tools.recover_autosaved gives reopened C++ parts).

    ``scope="recent"`` (default): only the SINGLE most-recent PRIOR session's docs — the crash we
    are recovering from — never the whole history. ``scope="all"``: every autosave on disk. Prunes
    stale autosaves first. Best-effort: a bad/partial file is skipped, never fatal.
    """
    # Lazy imports: this module is imported BY session.py, so importing it back at module load would
    # be a cycle. At call time the cycle is long resolved.
    from services.mcp.ecad_common import schema as _schema
    from services.mcp.ecad_common import ecad_session as _sess

    prune()
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
        doc = payload.get("doc")
        if not isinstance(doc, dict):
            continue
        if not (_schema.is_schematic(doc) or _schema.is_board(doc)):
            continue
        try:
            session = _sess.open_session(doc, path=payload.get("path"))
            out.append({"doc_id": session.doc_id, "kind": session.kind,
                        "name": doc.get("name", ""),
                        "original_doc_id": payload.get("doc_id")})
            # MIGRATE the file: persist the reopened doc under its NEW id (current session tag) and
            # delete the stale prior-session source. Without this, every restart leaves the old file
            # behind AND writes a new one, so the directory grows by ~N docs per restart forever.
            try:
                if _STATE["enabled"]:
                    autosave_session(session)  # write the doc under its new id (current session)
                    if p.name != f"{session.doc_id}.json":
                        p.unlink(missing_ok=True)  # only after the new file is safely written
            except Exception:  # noqa: BLE001 — migration is best-effort; never fail recovery
                pass
        except Exception:  # noqa: BLE001
            continue
    return out
