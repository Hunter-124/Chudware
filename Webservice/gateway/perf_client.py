"""Client-side performance beacon: POST /perf/client.

Authed browsers post a small JSON sample {boot_ms, fps, rpc_ms, ua}; we append
one JSON line (plus server ts + user email) to Webservice/perf/traces/client.jsonl.
Bodies larger than 4 KiB are rejected.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

try:
    from .auth import get_current_user
    from .config import settings
except ImportError:  # uvicorn app:app from package dir
    from auth import get_current_user
    from config import settings

log = logging.getLogger("chudware-web.perf")

router = APIRouter(tags=["perf"])

_MAX_BODY = 4096  # 4 KiB hard cap


@router.post("/perf/client")
async def perf_client(request: Request) -> dict[str, Any]:
    """Append one client perf sample to the traces jsonl."""
    user = get_current_user(request)

    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > _MAX_BODY:
                raise HTTPException(status_code=413, detail="body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="bad content-length") from None

    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise HTTPException(status_code=413, detail="body too large")

    try:
        data = json.loads(raw.decode("utf-8") if raw else b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="json object required")

    record = {
        "ts": time.time(),
        "email": user.email,
        "boot_ms": data.get("boot_ms"),
        "fps": data.get("fps"),
        "rpc_ms": data.get("rpc_ms"),
        "ua": data.get("ua"),
    }

    out_dir = settings.perf_traces_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "client.jsonl"
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        log.exception("failed to write perf trace %s", out_path)
        raise HTTPException(status_code=500, detail="write failed") from exc

    return {"ok": True}


__all__ = ["router"]
