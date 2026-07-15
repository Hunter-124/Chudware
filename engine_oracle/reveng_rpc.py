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
    """Return full session manifest including artifacts.mesh_path / splat_path.

    Paths are absolute under AICAD_REVENG_DIR/<sid>/ when present. GUI resolves
    relative paths under the same root if ever relative.
    """
    try:
        t = _tools()
        sid = str((params or {}).get("session_id", ""))
        s = t._load_session(sid)
        if s is None:
            return _fail("no_such_session", f"session {sid!r} not found")
        # Ensure artifacts keys the GUI reads always exist (None when absent).
        arts = s.setdefault("artifacts", {})
        for key in ("mesh_path", "splat_path", "sparse_dir", "doc_id", "images",
                    "fits_path"):
            arts.setdefault(key, None if key != "images" else arts.get("images", 0))
        # Prefer absolute paths for mesh/splat so the GUI can load without join.
        root = t._session_path(sid)
        for key in ("mesh_path", "splat_path", "sparse_dir", "fits_path"):
            p = arts.get(key)
            if isinstance(p, str) and p and not Path(p).is_absolute():
                cand = root / p
                if cand.exists() or key.endswith("_dir"):
                    arts[key] = str(cand)
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
        from .parts_hub import get as _hub_get
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


# ── §2b product-media hand-off ───────────────────────────────────────────────

def _site_of(url_or_part_id: str) -> str:
    """Best-effort session-profile key for a download (mirrors product_media)."""
    s = (url_or_part_id or "").strip()
    if s.startswith("catalog:"):
        return s[len("catalog:"):].split("::", 1)[0]
    try:
        from urllib.parse import urlparse
        netloc = urlparse(s).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        parts = netloc.split(".")
        return parts[-2] if len(parts) >= 2 else (parts[0] if parts else netloc)
    except Exception:  # noqa: BLE001
        return "product"


def _download_into(url: str, site: str, dest_dir: Path) -> Path | None:
    """Download one media URL into ``dest_dir`` under the user's session; return
    the local path, or None on any failure (best-effort — never raises)."""
    try:
        from services.browser import session as _bsession
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "file"
        dest = dest_dir / name
        out = _bsession.browser_download(url, site, dest)
        if out.get("ok") and dest.is_file():
            return dest
    except Exception:  # noqa: BLE001
        pass
    return None


def _reveng_start_from_product(params: dict, _emit: Callable) -> dict:
    """§2b hand-off: harvest a product's images+documents, then stand up an RE
    session pre-populated with them. A zero-media harvest STILL creates a session
    (the user adds their own photos). Each download/ingest is guarded — one
    failure never fails the whole call."""
    try:
        t = _tools()
        p = params or {}
        ref = str(p.get("part_id") or p.get("url") or "").strip()
        if not ref:
            return _fail("no_reference", "pass a part_id or url")

        from .parts_hub.product_media import harvest_product_media
        harvest = harvest_product_media(ref)
        images = harvest.get("images", []) if harvest.get("ok") else []
        documents = harvest.get("documents", []) if harvest.get("ok") else []
        source_url = harvest.get("source_url") if harvest.get("ok") else (
            ref if ref.startswith("http") else None)
        site = _site_of(ref)

        # Start the session seeded with the product's identity (RE-23 seed lane).
        seed_bits = []
        for key in ("title", "mpn", "brand"):
            if p.get(key):
                seed_bits.append(f"{key}={p[key]}")
        if source_url:
            seed_bits.append(source_url)
        started = _reveng_start_session({
            "name": p.get("title") or None,
            "research": " ".join(seed_bits) if seed_bits else None,
        }, _emit)
        if not started.get("ok"):
            return started
        sid = started["session_id"]

        # Download images into <session>/images, then thread through reveng_add_images.
        images_added = 0
        if images:
            img_dir = t._session_path(sid) / "_harvest_images"
            local_imgs = [str(pth) for u in images
                          if (pth := _download_into(u, site, img_dir)) is not None]
            if local_imgs:
                added = _reveng_add_images({"session_id": sid, "paths": local_imgs}, _emit)
                if added.get("ok"):
                    images_added = added.get("added", 0)

        # Download each document, then ingest via the existing datasheet lane.
        documents_ingested = 0
        for doc_url in documents:
            local = _download_into(doc_url, site, t._session_path(sid) / "_harvest_docs")
            if local is None:
                continue
            ing = _reveng_ingest_datasheet({"session_id": sid, "path": str(local)}, _emit)
            if ing.get("ok"):
                documents_ingested += 1

        return {"ok": True, "session_id": sid,
                "images_added": images_added,
                "documents_ingested": documents_ingested,
                "source_url": source_url,
                "activeWorkspace": "ReverseEng"}
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


# ── phone capture (mirrors recon_tools.register_photo_recon_tools) ───────────

# port -> CaptureSession; process-local (same lifetime as the worker).
_CAPTURE_SESSIONS: dict = {}


def _start_capture_session(params: dict, _emit: Callable) -> dict:
    """Start local phone-upload capture server.

    Returns {ok, port, upload_url, qr, shot_names, photo_count?, instructions?}.
    ``qr`` is a PNG data-URL when the qrcode package is available, else the raw URL.
    """
    try:
        p = params or {}
        port = int(p.get("port", 8742) or 8742)
        scale_marker_mm = p.get("scale_marker_mm")
        if scale_marker_mm is not None:
            try:
                scale_marker_mm = float(scale_marker_mm)
            except (TypeError, ValueError):
                scale_marker_mm = None

        # Prefer engine_oracle path; fall back to engine symlink.
        try:
            from .vision.capture_server import start_session
        except ImportError:
            from engine.vision.capture_server import start_session  # type: ignore

        if port in _CAPTURE_SESSIONS:
            existing = _CAPTURE_SESSIONS[port]
            return {
                "ok": False,
                "reason": f"a capture session is already running on port {port}",
                "hint": "call stop_capture_session first, or use a different port",
                "port": port,
                "upload_url": existing.upload_url,
                "qr": existing.qr,
                "shot_names": list(existing.shot_names),
                "photo_count": len(existing.received),
            }

        session = start_session(port=port)
        port = int(session.port)  # resolved (port=0 -> ephemeral)
        session._scale_marker_mm = scale_marker_mm  # type: ignore[attr-defined]
        _CAPTURE_SESSIONS[port] = session
        return {
            "ok": True,
            "port": port,
            "upload_url": session.upload_url,
            "qr": session.qr,
            "shot_names": list(session.shot_names),
            "photo_count": len(session.received),
            "scale_marker_mm": scale_marker_mm,
            "instructions": (
                "Scan the QR code with your phone (or open the upload_url in a mobile "
                f"browser). Upload one photo per guided angle ({len(session.shot_names)} "
                "angles). When done, call stop_capture_session(port="
                f"{port}) to collect photos."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _stop_capture_session(params: dict, _emit: Callable) -> dict:
    """Stop capture server; return photos. Optional photogrammetry when requested.

    Returns {ok, photo_count, photos:[paths], mesh_path?}.
    Default run_reconstruction=false (GUI adds photos to a reveng session itself).
    """
    try:
        p = params or {}
        port = int(p.get("port", 8742) or 8742)
        run_reconstruction = bool(p.get("run_reconstruction", False))

        try:
            from .vision.capture_server import stop_session
        except ImportError:
            from engine.vision.capture_server import stop_session  # type: ignore

        session = _CAPTURE_SESSIONS.pop(port, None)
        if session is None:
            return _fail(
                f"no capture session is running on port {port}",
                "call start_capture_session first",
            )

        photos = list(session.received)
        photo_count = len(photos)
        work_dir = getattr(session, "work_dir", None)
        scale_mm = getattr(session, "_scale_marker_mm", None)
        stop_session(session)

        result: dict = {
            "ok": True,
            "photo_count": photo_count,
            "photos": photos,
            "mesh_path": None,
        }

        if not run_reconstruction or photo_count == 0:
            return result

        # Optional photogrammetry (legacy MCP default was True; GUI uses False).
        try:
            try:
                from .vision import photogrammetry as _pg
                from .vision import mesh_cleanup as _mc
            except ImportError:
                from engine.vision import photogrammetry as _pg  # type: ignore
                from engine.vision import mesh_cleanup as _mc  # type: ignore
            import os as _os
            import tempfile

            recon_dir = tempfile.mkdtemp(prefix="aicad_recon_")
            recon = _pg.reconstruct(
                image_dir=str(work_dir) if work_dir else "",
                out_dir=recon_dir,
                scale_mm=scale_mm,
            )
            result["reconstruction"] = recon
            if recon.get("ok") and recon.get("mesh_path"):
                raw_mesh = recon["mesh_path"]
                cleaned_path = _os.path.join(recon_dir, "cleaned_mesh.obj")
                cleanup = _mc.cleanup_mesh(raw_mesh, cleaned_path, scale_mm=scale_mm)
                if cleanup.get("ok"):
                    result["mesh_path"] = cleanup.get("mesh_path")
                else:
                    result["mesh_path"] = raw_mesh
                    result["cleanup_skipped"] = cleanup.get("reason")
            else:
                result["mesh_path"] = None
        except Exception as exc:  # noqa: BLE001
            result["reconstruction"] = {
                "ok": False,
                "reason": f"reconstruction failed: {exc}",
            }
        return result
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


REVENG_WORKER_METHODS: dict[str, Callable] = {
    "list_reveng_sessions": _reveng_list_sessions,
    "get_reveng_session": _reveng_get_session,
    "reveng_start_session": _reveng_start_session,
    "reveng_add_images": _reveng_add_images,
    "reveng_ingest_datasheet": _reveng_ingest_datasheet,
    "reveng_start_from_product": _reveng_start_from_product,
    "start_capture_session": _start_capture_session,
    "stop_capture_session": _stop_capture_session,
}
