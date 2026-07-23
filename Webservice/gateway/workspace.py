"""Shared-workspace file API + aicm mesh fetch for the web-native client.

/workspace/* is rooted at settings.workspace_root (the team CAD_Build dir) and
serves the browser file dialog: list/upload/download/mkdir/rename/delete/copy +
durable recents + shared copilot chat history. The daemon reads and writes the
SAME paths directly (doc_open/doc_save take server-side paths) — bytes only
cross the wire for import/export.

/files/aicm serves tessellation payloads the daemon publishes for its clients:
either plain files (tmp chudware_cpp_docs / state dir / workspace) or POSIX shm
objects (aicm-shm://name -> /dev/shm/<name>). Enforcement is server-side; the
allowlist is the security boundary, the client clamp is only UX.
"""

from __future__ import annotations
import json

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

try:
    from .config import settings
except ImportError:  # uvicorn app:app from package dir
    from config import settings

log = logging.getLogger("chudware-web.workspace")

router = APIRouter()

# POSIX shm objects published by aicm_handoff (shm_object_name: "chudware_" + sanitized key).
_SHM_NAME_RE = re.compile(r"^chudware_[A-Za-z0-9_.\-]{1,200}$")
# Revision-stable AICM keys end in _r<digits> (aicm_handoff::key_for). Immutable for a year.
_AICM_REVID_RE = re.compile(r"_r\d+")


def _aicm_cache_headers(name: str) -> dict[str, str]:
    """Revisioned doc meshes are content-addressed; non-revisioned stay no-store."""
    if name and _AICM_REVID_RE.search(name):
        return {"Cache-Control": "private, max-age=31536000, immutable"}
    return {"Cache-Control": "no-store"}

_SHM_URI_PREFIX = "aicm-shm://"

# Durable recent-files list for the WASM client (config.json lives in /tmp there).
_RECENTS_NAME = ".chudware_recents.json"
_RECENTS_MAX = 10
# Shared copilot chat store (same shape as client chat_history.json: root[key] = entry).
_CHAT_NAME = ".chudware_chat_history.json"
_CHAT_MAX_BODY = 5 * 1024 * 1024  # reject PUT bodies larger than 5 MiB


def _ws_root() -> Path:
    root = settings.workspace_root
    if not root or not Path(root).is_dir():
        raise HTTPException(status_code=503, detail="workspace root not configured")
    return Path(root).resolve()


def _resolve(rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under the workspace root."""
    root = _ws_root()
    rel = (rel or "").strip().lstrip("/").replace("\\", "/")
    if any(part in {"..", ""} for part in rel.split("/")) and rel != "":
        raise HTTPException(status_code=400, detail="bad path")
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes workspace")
    if must_exist and not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return target


@router.get("/workspace/list")
async def workspace_list(request: Request, dir: str = "") -> dict[str, Any]:
    base = _resolve(dir)
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    root = _ws_root()
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for p in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if p.name.startswith("."):
            continue
        rel = str(p.relative_to(root))
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_dir():
            dirs.append({"name": p.name, "path": rel})
        else:
            files.append({"name": p.name, "path": rel, "size": st.st_size,
                          "mtime": int(st.st_mtime)})
    return {"root": str(root), "dir": str(base.relative_to(root)) if base != root else "",
            "dirs": dirs[:2000], "files": files[:2000]}


@router.get("/workspace/download")
async def workspace_download(request: Request, path: str) -> FileResponse:
    target = _resolve(path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    return FileResponse(str(target), filename=target.name,
                        headers={"Cache-Control": "no-store"})


@router.post("/workspace/upload")
async def workspace_upload(
    request: Request,
    file: UploadFile = File(...),
    dir: str = Form(""),
) -> dict[str, Any]:
    base = _resolve(dir)
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    name = Path(file.filename or "upload.bin").name  # strip any client dirs
    if name.startswith(".") or not name:
        raise HTTPException(status_code=400, detail="bad filename")
    target = base / name
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            out.write(chunk)
    root = _ws_root()
    log.info("workspace upload: %s (%d bytes)", target, size)
    return {"ok": True, "path": str(target.relative_to(root)),
            "abs_path": str(target), "bytes": size}


@router.post("/workspace/mkdir")
async def workspace_mkdir(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    target = _resolve(str(body.get("path", "")), must_exist=False)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(target.relative_to(_ws_root()))}


@router.post("/workspace/rename")
async def workspace_rename(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    src = _resolve(str(body.get("src", "")))
    dst = _resolve(str(body.get("dst", "")), must_exist=False)
    if dst.exists():
        raise HTTPException(status_code=409, detail="destination exists")
    src.rename(dst)
    return {"ok": True, "path": str(dst.relative_to(_ws_root()))}


@router.post("/workspace/delete")
async def workspace_delete(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    target = _resolve(str(body.get("path", "")))
    if target == _ws_root():
        raise HTTPException(status_code=400, detail="refusing to delete root")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True}


@router.post("/workspace/copy")
async def workspace_copy(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Copy a file or (when recursive) a directory tree within the workspace."""
    src = _resolve(str(body.get("src", "")))
    dst = _resolve(str(body.get("dst", "")), must_exist=False)
    recursive = bool(body.get("recursive", True))
    if src == _ws_root():
        raise HTTPException(status_code=400, detail="refusing to copy root")
    if dst == _ws_root() or dst == src:
        raise HTTPException(status_code=400, detail="bad destination")
    if src.is_dir():
        if not recursive:
            raise HTTPException(status_code=400, detail="directory copy requires recursive=true")
        if dst.exists():
            raise HTTPException(status_code=409, detail="destination exists")
        # Ensure parent of dst exists; copytree creates dst itself.
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    else:
        if not src.is_file():
            raise HTTPException(status_code=400, detail="not a file")
        if dst.exists() and dst.is_dir():
            dst = dst / src.name
            if _ws_root() not in dst.resolve().parents and dst.resolve() != _ws_root():
                raise HTTPException(status_code=400, detail="path escapes workspace")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    root = _ws_root()
    log.info("workspace copy: %s -> %s", src, dst)
    return {"ok": True, "path": str(dst.relative_to(root)), "abs_path": str(dst)}


@router.get("/workspace/recents")
async def workspace_recents_get(request: Request) -> dict[str, Any]:
    """Load durable recent-files list (workspace-root .chudware_recents.json)."""
    path = _ws_root() / _RECENTS_NAME
    if not path.is_file():
        return {"files": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("recents read failed: %s", e)
        return {"files": []}
    files: list[str] = []
    raw = data.get("files", []) if isinstance(data, dict) else []
    if isinstance(raw, list):
        for e in raw:
            if isinstance(e, str) and e.strip():
                files.append(e.strip())
            if len(files) >= _RECENTS_MAX:
                break
    return {"files": files}


@router.put("/workspace/recents")
async def workspace_recents_put(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Persist durable recent-files list under the workspace root."""
    raw = body.get("files", []) if isinstance(body, dict) else []
    files: list[str] = []
    if isinstance(raw, list):
        seen: set[str] = set()
        for e in raw:
            if not isinstance(e, str):
                continue
            p = e.strip()
            if not p or p in seen:
                continue
            seen.add(p)
            files.append(p)
            if len(files) >= _RECENTS_MAX:
                break
    path = _ws_root() / _RECENTS_NAME
    path.write_text(json.dumps({"files": files}, indent=2), encoding="utf-8")
    log.info("workspace recents: wrote %d entries", len(files))
    return {"ok": True, "files": files}


def _chat_path() -> Path:
    return _ws_root() / _CHAT_NAME


def _load_chat_store() -> dict[str, Any]:
    """Load the shared chat store; missing/corrupt → empty object."""
    path = _chat_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("chat store read failed: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via tmp + replace so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


@router.get("/workspace/chat")
async def workspace_chat_get(request: Request) -> dict[str, Any]:
    """Full shared chat_history store (workspace-root .chudware_chat_history.json).

    Shape matches the WASM/desktop client file: root[docKey] = {active, conversations}.
    Missing file → empty object so clients can merge cleanly.
    """
    return _load_chat_store()


@router.put("/workspace/chat")
async def workspace_chat_put(request: Request) -> dict[str, Any]:
    """Merge a partial {key: entry} map into the shared chat store.

    Client PUTs one or a few keys; server deep-replaces only those keys
    (load → update → atomic write). Never replaces the whole store from the body.
    """
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            n = int(cl)
        except ValueError:
            n = -1
        if n > _CHAT_MAX_BODY:
            raise HTTPException(status_code=413, detail="chat body too large (max 5MB)")
    raw = await request.body()
    if len(raw) > _CHAT_MAX_BODY:
        raise HTTPException(status_code=413, detail="chat body too large (max 5MB)")
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        body = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    # Only accept object-valued entries (skip null / scalar noise).
    patch: dict[str, Any] = {}
    for k, v in body.items():
        if not isinstance(k, str) or not k:
            continue
        if isinstance(v, dict):
            patch[k] = v
    if not patch:
        raise HTTPException(status_code=400, detail="no valid chat entries in body")
    store = _load_chat_store()
    for k, v in patch.items():
        store[k] = v  # replace key wholesale (entry object)
    _atomic_write_json(_chat_path(), store)
    log.info("workspace chat: merged %d key(s), store size %d", len(patch), len(store))
    return {"ok": True, "merged": list(patch.keys()), "keys": len(store)}


def _aicm_allowlist() -> list[Path]:
    dirs = [
        Path(tempfile.gettempdir()) / "chudware_cpp_docs",  # aicm_handoff publish_file
        Path.home() / ".local" / "share" / "Chudware",     # hostd state dir (autosave etc.)
    ]
    try:
        dirs.append(_ws_root())
    except HTTPException:
        pass
    return [d.resolve() for d in dirs if d.exists()]


@router.get("/files/aicm")
async def files_aicm(request: Request, path: str = "", shm: str = "") -> FileResponse:
    """Serve one daemon-published tessellation payload (file or shm object)."""
    if path.startswith(_SHM_URI_PREFIX):
        shm, path = path[len(_SHM_URI_PREFIX):], ""
    if shm:
        if not _SHM_NAME_RE.match(shm):
            raise HTTPException(status_code=400, detail="bad shm name")
        target = Path("/dev/shm") / shm
        if not target.is_file():
            raise HTTPException(status_code=404, detail="shm object gone")
        return FileResponse(str(target), media_type="application/octet-stream",
                            headers=_aicm_cache_headers(shm))
    if not path:
        raise HTTPException(status_code=400, detail="path or shm required")
    try:
        target = Path(path).resolve(strict=True)
    except OSError:
        raise HTTPException(status_code=404, detail="not found")
    if not any(target == d or d in target.parents for d in _aicm_allowlist()):
        raise HTTPException(status_code=403, detail="path not allowlisted")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    return FileResponse(str(target), media_type="application/octet-stream",
                        headers=_aicm_cache_headers(target.name))
