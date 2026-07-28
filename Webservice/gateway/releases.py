"""Admin-only release installer downloads.

Lists files dropped into CHUDWARE_RELEASES_DIR (default ~/chudware-releases)
and serves them for download from the /admin dashboard's Releases card, so
testers can grab the latest Windows/etc. installer without SSH. Admins can
also upload/delete releases from the same card (e.g. a Chudware-Setup-*.exe
built on a Windows box, since this Linux deploy host cannot produce one).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

try:
    from .auth import get_current_user
except ImportError:  # uvicorn app:app from package dir
    from auth import get_current_user

router = APIRouter(prefix="/api/admin", tags=["releases"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _releases_dir() -> Path:
    env = (os.environ.get("CHUDWARE_RELEASES_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "chudware-releases"


# Strict allowlist: the name flows into FileResponse(filename=...) and thus
# into a Content-Disposition header, so anything outside [A-Za-z0-9._-] (quotes,
# semicolons, newlines) is a header-injection vector, not just a path concern.
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _safe_name(name: str) -> str:
    """Release basename: strict charset, no dotfiles, no separators."""
    if not name or name.startswith(".") or not _SAFE_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid release name")
    return name


@router.get("/releases")
async def list_releases(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    d = _releases_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in d.iterdir():
        if not entry.is_file() or entry.name.startswith("."):
            continue
        st = entry.stat()
        out.append({
            "name": entry.name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    out.sort(key=lambda r: r["name"])
    return out


@router.get("/releases/download/{name}")
async def download_release(name: str, request: Request) -> FileResponse:
    _require_admin(request)
    safe = _safe_name(name)
    path = _releases_dir() / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="release not found")
    return FileResponse(path, filename=safe)


@router.post("/releases/upload")
async def upload_release(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    _require_admin(request)
    safe = _safe_name(file.filename or "")
    d = _releases_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / safe
    # Stream to a .part sibling first (chunked — never buffers the whole upload in memory,
    # which matters under the gateway unit's MemoryMax=1G) then atomically rename, so a
    # client abort or crash mid-upload can never leave a half-written file visible to
    # list_releases/download.
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    tmp.replace(dest)
    st = dest.stat()
    return {
        "name": dest.name,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


@router.delete("/releases/{name}")
async def delete_release(name: str, request: Request) -> dict[str, Any]:
    _require_admin(request)
    safe = _safe_name(name)
    path = _releases_dir() / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="release not found")
    path.unlink()
    return {"ok": True}
