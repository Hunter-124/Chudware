"""Authenticated, read-only operator deployment status and logs.

Deployment and recovery run through the qualified host-side operator workflow;
browser administrators cannot launch builds or restart services.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

try:
    from .config import settings
    from .auth import get_current_user
except ImportError:  # uvicorn app:app from package dir
    from config import settings
    from auth import get_current_user

router = APIRouter(prefix="/api/admin", tags=["deploy"])


def _require_admin(request: Request):
    user = get_current_user(request)
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _status_path() -> Path:
    return settings.logs_dir / "deploy-status.json"


def _log_path() -> Path:
    return settings.logs_dir / "update.log"


def _default_status() -> dict[str, Any]:
    return {"running": False, "kind": None, "started_at": None, "last": None}


def _read_status() -> dict[str, Any]:
    path = _status_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _default_status()
    if not isinstance(raw, dict):
        return _default_status()
    out = _default_status()
    out["running"] = bool(raw.get("running"))
    kind = raw.get("kind")
    out["kind"] = kind if isinstance(kind, str) else None
    started = raw.get("started_at")
    out["started_at"] = started if isinstance(started, (int, float)) else None
    last = raw.get("last")
    out["last"] = last if isinstance(last, dict) else None
    return out


@router.get("/deploy/status")
async def deploy_status(request: Request) -> dict[str, Any]:
    _require_admin(request)
    st = _read_status()
    return {
        "ok": True,
        "running": bool(st.get("running")),
        "kind": st.get("kind"),
        "started_at": st.get("started_at"),
        "last": st.get("last"),
    }


@router.get("/deploy/log")
async def deploy_log(
    request: Request,
    lines: int = 200,
) -> PlainTextResponse:
    _require_admin(request)
    n = max(10, min(int(lines), 2000))
    path = _log_path()
    if not path.is_file():
        return PlainTextResponse("")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"log read failed: {exc}") from exc
    if not text:
        return PlainTextResponse("")
    parts = text.splitlines()
    tail = parts[-n:] if len(parts) > n else parts
    body = "\n".join(tail)
    if body and not body.endswith("\n"):
        body += "\n"
    return PlainTextResponse(body)
