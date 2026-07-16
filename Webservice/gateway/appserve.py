"""Serve the WASM web client static tree at /app with COOP/COEP headers.

Browser SharedArrayBuffer / pthread builds need:
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
on every asset under /app (html, js, wasm, data, …). Auth is enforced by
AuthMiddleware — /app is intentionally NOT on the public allowlist.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

try:
    from .config import settings
except ImportError:  # uvicorn app:app from package dir
    from config import settings

log = logging.getLogger("billet-web.appserve")

router = APIRouter(tags=["appserve"])

# Explicit overrides — mimetypes is unreliable for .wasm / Emscripten .data.
_MIME_OVERRIDES: dict[str, str] = {
    ".wasm": "application/wasm",
    ".data": "application/octet-stream",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}


def _app_root() -> Path:
    root = settings.app_dir
    if not root:
        raise HTTPException(status_code=503, detail="app dir not configured")
    return Path(root).expanduser().resolve()


def _media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _cache_control(path: Path) -> str:
    # The Emscripten bundle files are UNHASHED (fixed names Billet.js/.wasm/.data) and MUST
    # update in lockstep — a cached old Billet.js against a new Billet.wasm is a fatal
    # LinkError (mismatched imports). Force revalidation so js/wasm never skew across deploys.
    if path.suffix.lower() in {".html", ".htm", ".js", ".wasm", ".data", ".mjs"}:
        return "no-cache"
    return "max-age=3600"


def _coop_headers(path: Path) -> dict[str, str]:
    return {
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Embedder-Policy": "require-corp",
        "Cache-Control": _cache_control(path),
    }


def _resolve_under_app(rel: str) -> Path:
    """Clamp *rel* under app_dir; reject traversal and missing files."""
    root = _app_root()
    rel = (rel or "").strip().lstrip("/").replace("\\", "/")
    if rel in {"", "."}:
        target = root / "index.html"
    else:
        parts = [p for p in rel.split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise HTTPException(status_code=400, detail="bad path")
        target = (root.joinpath(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes app dir") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return target


def _file_response(target: Path) -> FileResponse:
    return FileResponse(
        path=str(target),
        media_type=_media_type(target),
        headers=_coop_headers(target),
    )


@router.get("/app", response_model=None)
@router.get("/app/", response_model=None)
async def app_index(request: Request) -> Response:
    """Serve the WASM client entry (index.html)."""
    return _file_response(_resolve_under_app(""))


@router.get("/app/{asset_path:path}", response_model=None)
async def app_asset(asset_path: str, request: Request) -> Response:
    """Serve a file from the WASM client tree."""
    return _file_response(_resolve_under_app(asset_path))


__all__ = ["router"]
