"""Worker-RPC bridge for the Reverse Engineering suite (GUI ↔ reveng backend).

The reverse-engineering session ops live as MCP tools in
``services.mcp.reveng_tools`` (RE-01..RE-29), but the desktop GUI talks to the
worker over the newline-JSON ``WorkerClient``/``AsyncRpc`` seam — NOT the MCP
transport. Without a worker surface the RE tab's ``list_reveng_sessions`` call
lands on "method not found" and the workspace shows "Reverse Eng tools
unavailable". This module is the thin worker layer that fixes that, exactly
mirroring :mod:`engine_oracle.embed_rpc` (browser) and
:mod:`engine_oracle.parts_hub.gui_rpc` (parts).

It reuses the SAME session-store helpers the MCP tools use
(``services.mcp.reveng_tools`` module-level funcs), so both transports read/write
one on-disk session format — no divergence. It is the delivery seam Hunter asked
for: the Parts Library "Reverse-engineer this part" action calls
``reveng_start_session`` here to hand an un-modeled part's photos/datasheet (or a
part number / vendor URL research seed) to a fresh RE session.

Envelopes all degrade to ``{ok:false, reason, hint}`` — never a traceback.
Registered append-only from ``engine_oracle/worker.py`` (same try/except pattern
as the parts + embed method tables).

NOTE (layering): the session-store logic currently lives under services/mcp; a
future RE-suite cleanup may relocate it into engine_oracle and have both the MCP
tools and this bridge import from there. Until then this bridge imports those
helpers directly — no behaviour is duplicated, only re-exposed on the worker seam.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable


def _fail(reason: str, hint: str = "") -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _wrap_exc(exc: Exception) -> dict:
    if isinstance(exc, ImportError):
        return _fail("reveng_unavailable",
                     "reverse-engineering tools not importable; check engine install")
    return _fail(f"reveng_rpc_failed: {exc}", "")


def _tools():
    """Lazy-import the reveng session-store helpers (keeps the worker importable
    even if the RE suite is absent)."""
    from services.mcp import reveng_tools as t
    return t


# ── §8.1 READ ────────────────────────────────────────────────────────────────

def _reveng_list_sessions(params: dict, _emit: Callable) -> dict:
    try:
        t = _tools()
        root = t._reveng_dir()
        sessions = []
        if root.exists():
            for d in sorted(root.iterdir()):
                if d.is_dir() and (d / "session.json").exists():
                    s = t._load_session(d.name)
                    if s:
                        sessions.append(t._session_summary(s))
        return {"ok": True, "sessions": sessions}
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _reveng_get_session(params: dict, _emit: Callable) -> dict:
    try:
        t = _tools()
        sid = str((params or {}).get("session_id", ""))
        s = t._load_session(sid)
        if s is None:
            return _fail("no_such_session", f"session {sid!r} not found")
        return {"ok": True, "session": s}
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


# ── §8.2 WRITE (the delivery seam) ───────────────────────────────────────────

def _seed_from_part_ref(t, session: dict, sid: str, part_ref: str) -> dict:
    """Copy any photos/datasheet recorded on a parts-hub PartRecord into the
    session (mirrors reveng_tools.reveng_start_session's RE-23 seed block).
    Best-effort: never raises."""
    seeded: dict = {}
    try:
        from engine_oracle.parts_hub import get as _hub_get
        record = _hub_get(part_ref)
        if not record:
            return seeded
        meta = record.get("metadata") or {}
        photos = meta.get("photos") or []
        datasheet = meta.get("datasheet")
        sess_dir = t._session_path(sid)
        if photos:
            images_dir = sess_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for p in photos:
                src = Path(p).expanduser()
                if src.is_file():
                    shutil.copy2(str(src), str(images_dir / src.name))
                    copied += 1
            if copied:
                session["artifacts"]["images"] = copied
                session["stages"]["capture"] = "done"
                seeded["images"] = copied
        if datasheet:
            ds_dir = sess_dir / "datasheet"
            ds_dir.mkdir(parents=True, exist_ok=True)
            src = Path(datasheet).expanduser()
            if src.is_file():
                shutil.copy2(str(src), str(ds_dir / src.name))
                seeded["datasheet"] = str(ds_dir / src.name)
        if seeded:
            session["provenance"].append({
                "step": "seeded_from_parts_hub",
                "part_ref": part_ref,
                "seeded": seeded,
                "ts": t._now_iso(),
            })
    except Exception:  # noqa: BLE001  (seeding is best-effort)
        pass
    return seeded


_URL_PAT = re.compile(r"^https?://", re.IGNORECASE)
_MPN_PAT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{2,20}$")


def _reveng_start_session(params: dict, _emit: Callable) -> dict:
    """Create a session, optionally seeded from a parts-hub part and/or a
    research string (part number | vendor URL | free query). This is what the
    Parts Library "Reverse-engineer this part" button calls."""
    try:
        t = _tools()
        p = params or {}
        name = p.get("name") or None
        part_ref = p.get("part_ref") or None
        research = p.get("research") or None

        sid = t._new_session_id()
        label = name or f"RE session {t._now_iso()}"
        session = t._make_session(sid, label, part_ref)
        t._save_session(session)

        seeded = _seed_from_part_ref(t, session, sid, part_ref) if part_ref else {}
        if seeded:
            t._save_session(session)

        result: dict = {"ok": True, "session_id": sid, "session": session, "seeded": seeded}
        if research:
            research = str(research).strip()
            if _URL_PAT.match(research):
                result["research_result"] = t._research_ingest(session_id=sid, url=research)
            elif _MPN_PAT.match(research):
                result["research_result"] = t._research_ingest(session_id=sid, part_number=research)
            else:
                result["research_result"] = t._research_ingest(session_id=sid, query=research)
        return result
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _reveng_add_images(params: dict, _emit: Callable) -> dict:
    """Deliver still photos into a session (paths list and/or a directory)."""
    try:
        t = _tools()
        p = params or {}
        sid = str(p.get("session_id", ""))
        s = t._load_session(sid)
        if s is None:
            return _fail("no_such_session", f"session {sid!r} not found")
        images_dir = t._session_path(sid) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        sources: list[Path] = []
        if p.get("dir"):
            src = Path(str(p["dir"])).expanduser()
            if not src.is_dir():
                return _fail("dir_not_found", f"directory not found: {p['dir']}")
            for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
                sources.extend(src.glob(ext))
        for path in (p.get("paths") or []):
            sources.append(Path(str(path)).expanduser())
        added = 0
        for src_path in sources:
            if src_path.is_file():
                shutil.copy2(str(src_path), str(images_dir / src_path.name))
                added += 1
        s["artifacts"]["images"] = s["artifacts"].get("images", 0) + added
        s["stages"]["capture"] = "done" if s["artifacts"]["images"] > 0 else "idle"
        t._save_session(s)
        return {"ok": True, "added": added, "session": s}
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _reveng_ingest_datasheet(params: dict, _emit: Callable) -> dict:
    """Deliver a PDF datasheet into a session (docling extract; degrades to a
    backend_not_provisioned envelope when docling is absent)."""
    try:
        import os
        t = _tools()
        p = params or {}
        sid = str(p.get("session_id", ""))
        path = str(p.get("path", ""))
        s = t._load_session(sid)
        if s is None:
            return _fail("no_such_session", f"session {sid!r} not found")
        from services.mcp.reveng_tools import _ingest_pdf  # same extractor as the MCP tool
        datasheet_dir = str(t._session_path(sid) / "datasheet")
        os.makedirs(datasheet_dir, exist_ok=True)
        result = _ingest_pdf(path, datasheet_dir)
        if not result.get("ok"):
            return result
        s["provenance"].append({
            "step": "ingest_datasheet", "path": path,
            "tables": len(result.get("tables", [])), "ts": t._now_iso(),
        })
        t._save_session(s)
        return {"ok": True, "tables": result.get("tables", []),
                "crops": result.get("crops", []), "session": s}
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


REVENG_WORKER_METHODS: dict[str, Callable] = {
    "list_reveng_sessions": _reveng_list_sessions,
    "get_reveng_session": _reveng_get_session,
    "reveng_start_session": _reveng_start_session,
    "reveng_add_images": _reveng_add_images,
    "reveng_ingest_datasheet": _reveng_ingest_datasheet,
}
