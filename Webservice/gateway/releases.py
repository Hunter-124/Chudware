"""Admin-only release installer downloads.

Lists files dropped into CHUDWARE_RELEASES_DIR (default ~/chudware-releases)
and serves them for download from the /admin dashboard's Releases card, so
testers can grab the latest Windows/etc. installer without SSH.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
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


def _safe_name(name: str) -> str:
    """Basename only; reject path separators and dotfiles."""
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="invalid release name")
    if os.path.basename(name) != name:
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
