"""Shared-workspace file API + aicm mesh fetch for the web-native client.

/workspace/* is rooted at settings.workspace_root (the team CAD_Build dir) and
serves the browser file dialog: list/upload/download/mkdir/rename/delete. The
daemon reads and writes the SAME paths directly (doc_open/doc_save take
server-side paths) — bytes only cross the wire for import/export.

/files/aicm serves tessellation payloads the daemon publishes for its clients:
either plain files (tmp aicad_cpp_docs / state dir / workspace) or POSIX shm
objects (aicm-shm://name -> /dev/shm/<name>). Enforcement is server-side; the
allowlist is the security boundary, the client clamp is only UX.
"""

from __future__ import annotations

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

log = logging.getLogger("billet-web.workspace")

router = APIRouter()

# POSIX shm objects published by aicm_handoff (shm_object_name: "aicad_" + sanitized key).
_SHM_NAME_RE = re.compile(r"^aicad_[A-Za-z0-9_.\-]{1,200}$")
_SHM_URI_PREFIX = "aicm-shm://"


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


def _aicm_allowlist() -> list[Path]:
    dirs = [
        Path(tempfile.gettempdir()) / "aicad_cpp_docs",  # aicm_handoff publish_file
        Path.home() / ".local" / "share" / "AI-CAD",     # hostd state dir (autosave etc.)
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
                            headers={"Cache-Control": "no-store"})
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
                        headers={"Cache-Control": "no-store"})
