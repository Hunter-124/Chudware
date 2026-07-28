"""Per-account file-drop bucket (/bucket page + /api/bucket/* API).

Rooted at the CALLER's per-account tree
(``settings.user_bucket_root(user_slug(email))``), created on demand. With
``*.ua.edu`` auto-approval the bucket MUST be tenant-isolated: every path is
clamped under the caller's own root, uploads carry per-account rate limits,
size caps, and disk quotas, and only an admin may opt into the LEGACY shared
tree (``settings.bucket_root`` itself) with ``?scope=shared`` — legacy content
is never moved or deleted. Independent of /workspace/* (the CAD engine's live
doc tree + .aicm mesh cache): keeping uploads out of that tree avoids
colliding with autosave/mesh cache housekeeping there.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

try:
    from .config import settings
    from .auth import User, get_current_user
    from .limits import (
        api_writes,
        enforce_disk_quota,
        enforce_upload_size,
        uploads,
        user_slug,
    )
except ImportError:  # uvicorn app:app from package dir
    from config import settings
    from auth import User, get_current_user
    from limits import (
        api_writes,
        enforce_disk_quota,
        enforce_upload_size,
        uploads,
        user_slug,
    )

log = logging.getLogger("chudware-web.bucket")

router = APIRouter()


def _bucket_root() -> Path:
    """Legacy single-tenant shared tree. Only reachable via admin scope=shared."""
    root = settings.bucket_root
    if not root or not Path(root).is_dir():
        raise HTTPException(status_code=503, detail="bucket root not configured")
    return Path(root).resolve()


def _caller_root(request: Request, scope: str = "") -> tuple[User, Path]:
    """Resolve the one root this caller is allowed to address.

    Default is the account's own tree, created on demand. ``scope=shared`` is
    an admin-only escape hatch onto the legacy shared tree; any other scope
    value is rejected so a typo cannot silently widen access.
    """
    user = get_current_user(request)
    if scope == "shared":
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="scope=shared requires admin")
        return user, _bucket_root()
    if scope:
        raise HTTPException(status_code=400, detail="unknown scope")
    _bucket_root()  # base must still be configured (503 on misconfig)
    root = settings.user_bucket_root(user_slug(user.email))
    root.mkdir(parents=True, exist_ok=True)
    return user, root.resolve()


def _resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under ``root`` (the caller's own tree).

    Containment is checked on the FULLY RESOLVED path, and a final-component
    symlink is always rejected, so a symlink planted inside the jail cannot be
    used to escape it.
    """
    rel = (rel or "").strip().replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise HTTPException(status_code=400, detail="absolute paths not allowed")
    if any(part in {"..", ""} for part in rel.split("/")) and rel != "":
        raise HTTPException(status_code=400, detail="bad path")
    if not rel:
        return root
    candidate = root / rel
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail="symlink not allowed")
    target = candidate.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes bucket")
    if must_exist and not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return target


def _content_length(request: Request) -> int | None:
    """Best-effort Content-Length; None when absent (chunked) or malformed."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


@router.get("/api/bucket/list")
async def bucket_list(request: Request, dir: str = "", scope: str = "") -> dict[str, Any]:
    _, root = _caller_root(request, scope)
    base = _resolve(root, dir)
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
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
    # "root" stays a key for client compatibility but is always the RELATIVE
    # path of the jail root ("") — absolute host paths never leave the server.
    return {"root": "", "dir": str(base.relative_to(root)) if base != root else "",
            "dirs": dirs[:5000], "files": files[:5000]}


@router.get("/api/bucket/download")
async def bucket_download(request: Request, path: str, scope: str = "") -> FileResponse:
    _, root = _caller_root(request, scope)
    target = _resolve(root, path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    return FileResponse(str(target), filename=target.name,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/bucket/upload")
async def bucket_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    dir: str = Form(""),
    scope: str = "",
) -> dict[str, Any]:
    """Accept flat files or a whole dropped/selected folder tree.

    Each UploadFile's filename may be a nested relative path
    (``sub/dir/name.ext``, forward-slashed) when the client uploaded a
    folder — the browser File API and our frontend both preserve that via
    ``File.webkitRelativePath`` / ``FormData.append(name, file, path)``.
    Path segments are clamped under the target dir the same way ``_resolve``
    clamps client dir paths; anything that tries to escape is skipped rather
    than aborting the whole batch, so one bad entry in a big folder drop
    doesn't lose everything else.
    """
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    uploads.enforce(user.email, "uploads")
    declared = _content_length(request)
    cap = enforce_upload_size(declared)
    enforce_disk_quota(root, declared or 0)
    base = _resolve(root, dir, must_exist=False)
    base.mkdir(parents=True, exist_ok=True)
    uploaded: list[dict[str, Any]] = []
    skipped: list[str] = []
    total = 0
    for file in files:
        raw = (file.filename or "").strip().replace("\\", "/").lstrip("/")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            skipped.append(file.filename or "")
            continue
        candidate = base.joinpath(*parts)
        if candidate.is_symlink():
            skipped.append(raw)
            continue
        target = candidate.resolve()
        if target != root and root not in target.parents:
            skipped.append(raw)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with target.open("wb") as out:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    total += len(chunk)
                    # Chunked uploads have no Content-Length — enforce the cap
                    # DURING streaming and leave no partial file behind.
                    if size > cap or total > cap:
                        raise HTTPException(
                            status_code=413,
                            detail=f"upload too large — {cap // (1024 * 1024)} MiB max",
                        )
                    out.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        log.info("bucket upload: %s (%d bytes) by %s", target, size, user.email)
        uploaded.append({"path": str(target.relative_to(root)), "bytes": size})
    if not uploaded and skipped:
        raise HTTPException(status_code=400, detail=f"no valid files (skipped: {', '.join(skipped[:5])})")
    return {"ok": True, "uploaded": uploaded, "skipped": skipped}


@router.post("/api/bucket/mkdir")
async def bucket_mkdir(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    target = _resolve(root, str(body.get("path", "")), must_exist=False)
    target.mkdir(parents=True, exist_ok=True)
    log.info("bucket mkdir: %s by %s", target, user.email)
    return {"ok": True, "path": str(target.relative_to(root))}


@router.post("/api/bucket/rename")
async def bucket_rename(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    src = _resolve(root, str(body.get("src", "")))
    new_name = Path(str(body.get("name", ""))).name
    if not new_name or new_name.startswith("."):
        raise HTTPException(status_code=400, detail="bad name")
    dst = src.parent / new_name
    if dst.is_symlink():
        raise HTTPException(status_code=400, detail="symlink not allowed")
    if dst.exists():
        raise HTTPException(status_code=409, detail="already exists")
    src.rename(dst)
    log.info("bucket rename: %s -> %s by %s", src, dst, user.email)
    return {"ok": True, "path": str(dst.relative_to(root))}


@router.post("/api/bucket/delete")
async def bucket_delete(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    target = _resolve(root, str(body.get("path", "")))
    if target == root:
        raise HTTPException(status_code=400, detail="cannot delete bucket root")
    if target.is_dir():
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
