"""Team file-drop bucket (/bucket page + /api/bucket/* API).

Rooted at settings.bucket_root (default ~/ME489) — a general-purpose shared
folder any logged-in user can browse, organize, and upload/download files in.
Independent of /workspace/* (the CAD engine's live doc tree + .aicm mesh
cache): keeping uploads out of that tree avoids colliding with autosave/mesh
cache housekeeping there. Any authenticated user may use every endpoint here
(AuthMiddleware already gates all non-/healthz paths; no additional
role check — same trust model /workspace/* already uses for the CAD tree).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

try:
    from .config import settings
    from .auth import get_current_user
except ImportError:  # uvicorn app:app from package dir
    from config import settings
    from auth import get_current_user

log = logging.getLogger("chudware-web.bucket")

router = APIRouter()


def _bucket_root() -> Path:
    root = settings.bucket_root
    if not root or not Path(root).is_dir():
        raise HTTPException(status_code=503, detail="bucket root not configured")
    return Path(root).resolve()


def _resolve(rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under the bucket root (same rule as /workspace/*)."""
    root = _bucket_root()
    rel = (rel or "").strip().lstrip("/").replace("\\", "/")
    if any(part in {"..", ""} for part in rel.split("/")) and rel != "":
        raise HTTPException(status_code=400, detail="bad path")
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes bucket")
    if must_exist and not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return target


@router.get("/api/bucket/list")
async def bucket_list(request: Request, dir: str = "") -> dict[str, Any]:
    get_current_user(request)
    base = _resolve(dir)
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    root = _bucket_root()
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
            "dirs": dirs[:5000], "files": files[:5000]}


@router.get("/api/bucket/download")
async def bucket_download(request: Request, path: str) -> FileResponse:
    get_current_user(request)
    target = _resolve(path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    return FileResponse(str(target), filename=target.name,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/bucket/upload")
async def bucket_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    dir: str = Form(""),
) -> dict[str, Any]:
    user = get_current_user(request)
    base = _resolve(dir, must_exist=False)
    base.mkdir(parents=True, exist_ok=True)
    root = _bucket_root()
    uploaded: list[dict[str, Any]] = []
    for file in files:
        name = Path(file.filename or "upload.bin").name  # strip any client dirs
        if name.startswith(".") or not name:
            raise HTTPException(status_code=400, detail=f"bad filename: {file.filename!r}")
        target = base / name
        size = 0
        with target.open("wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                out.write(chunk)
        log.info("bucket upload: %s (%d bytes) by %s", target, size, user.email)
        uploaded.append({"path": str(target.relative_to(root)), "bytes": size})
    return {"ok": True, "uploaded": uploaded}


@router.post("/api/bucket/mkdir")
async def bucket_mkdir(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = get_current_user(request)
    target = _resolve(str(body.get("path", "")), must_exist=False)
    target.mkdir(parents=True, exist_ok=True)
    log.info("bucket mkdir: %s by %s", target, user.email)
    return {"ok": True, "path": str(target.relative_to(_bucket_root()))}


@router.post("/api/bucket/rename")
async def bucket_rename(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = get_current_user(request)
    src = _resolve(str(body.get("src", "")))
    new_name = Path(str(body.get("name", ""))).name
    if not new_name or new_name.startswith("."):
        raise HTTPException(status_code=400, detail="bad name")
    dst = src.parent / new_name
    if dst.exists():
        raise HTTPException(status_code=409, detail="already exists")
    src.rename(dst)
    log.info("bucket rename: %s -> %s by %s", src, dst, user.email)
    return {"ok": True, "path": str(dst.relative_to(_bucket_root()))}


@router.post("/api/bucket/delete")
async def bucket_delete(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = get_current_user(request)
    target = _resolve(str(body.get("path", "")))
    if target == _bucket_root():
        raise HTTPException(status_code=400, detail="cannot delete bucket root")
    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    else:
        target.unlink()
    log.info("bucket delete: %s by %s", target, user.email)
    return {"ok": True}


@router.get("/bucket", response_model=None)
async def bucket_page(request: Request) -> HTMLResponse:
    get_current_user(request)  # AuthMiddleware already enforces this; explicit for clarity
    static_dir = Path(__file__).resolve().parent / "static"
    return HTMLResponse((static_dir / "bucket.html").read_text())
