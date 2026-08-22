"""Serve the ordinary authenticated customer portal and its hashed assets.

This deliberately does not import ``appserve``. The WASM client has a broader
CSP (WebAssembly evaluation and blob workers), COOP/COEP, and un-hashed bundle
cache rules that must never leak onto the account or billing surface.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

router = APIRouter(tags=["portal"])

_PORTAL_ROOT = Path(__file__).resolve().parent / "static"

# Copied from appserve.py: explicit suffixes avoid OS-dependent MIME databases.
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

_PORTAL_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)
_INLINE_SCRIPT_RE = re.compile(
    rb"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_csp_cache: dict[tuple[str, int, int], str] = {}
_HASHED_ASSET_RE = re.compile(r"^[A-Za-z0-9_-]+\.[0-9a-f]{12,64}\.[A-Za-z0-9]+$")


def _app_root() -> Path:
    return _PORTAL_ROOT.resolve()


def _media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


# Copied from appserve.py, with the ordinary-page CSP as its base.
def _csp_for_html(path: Path) -> str:
    """_PORTAL_CSP plus a sha256 source for each inline script in *path*."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return _PORTAL_CSP
    cached = _csp_cache.get(key)
    if cached is not None:
        return cached
    try:
        body = path.read_bytes()
    except OSError:
        return _PORTAL_CSP
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(match.group(1)).digest()).decode() + "'"
        for match in _INLINE_SCRIPT_RE.finditer(body)
    )
    csp = _PORTAL_CSP.replace(
        "script-src 'self'", f"script-src 'self' {hashes}".rstrip()
    ) if hashes else _PORTAL_CSP
    if len(_csp_cache) > 64:
        _csp_cache.clear()
    _csp_cache[key] = csp
    return csp


# Copied from appserve.py: containment is resolved after separator normalization.
def _resolve_under_app(rel: str) -> Path:
    """Clamp *rel* under the portal static root; reject traversal and misses."""
    root = _app_root()
    rel = (rel or "").strip().lstrip("/").replace("\\", "/")
    if rel in {"", "."}:
        target = root / "index.html"
    else:
        parts = [part for part in rel.split("/") if part and part != "."]
        if any(part == ".." for part in parts):
            raise HTTPException(status_code=400, detail="bad path")
        target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes app dir") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return target


# Copied from appserve.py: only fresh precompressed siblings may be selected.
def _fresh_variant(target: Path, suffix: str) -> Path | None:
    variant = target.with_name(target.name + suffix)
    try:
        if variant.is_file() and variant.stat().st_mtime >= target.stat().st_mtime:
            return variant
    except OSError:
        return None
    return None


# Copied from appserve.py: prefer Brotli, then gzip, preserving the original MIME.
def _negotiate(target: Path, request: Request) -> tuple[Path, str | None]:
    accept = request.headers.get("accept-encoding", "")
    if "br" in accept:
        variant = _fresh_variant(target, ".br")
        if variant is not None:
            return variant, "br"
    if "gzip" in accept:
        variant = _fresh_variant(target, ".gz")
        if variant is not None:
            return variant, "gzip"
    return target, None


def _file_response(target: Path, request: Request, *, immutable: bool) -> FileResponse:
    served, encoding = _negotiate(target, request)
    headers = {
        "Content-Security-Policy": _csp_for_html(target) if target.suffix.lower() in {".html", ".htm"} else _PORTAL_CSP,
        "Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache",
    }
    if encoding:
        headers["Content-Encoding"] = encoding
        headers["Vary"] = "Accept-Encoding"
    return FileResponse(str(served), media_type=_media_type(target), headers=headers)


@router.get("/signin", response_model=None)
@router.get("/signup", response_model=None)
@router.get("/forgot", response_model=None)
@router.get("/reset", response_model=None)
async def auth_page(request: Request) -> Response:
    return _file_response(_resolve_under_app("login.html"), request, immutable=False)


@router.get("/auth-assets/{asset_path:path}", response_model=None)
async def auth_asset(asset_path: str, request: Request) -> Response:
    target = _resolve_under_app(asset_path)
    if not _HASHED_ASSET_RE.fullmatch(target.name) or not target.name.startswith("login"):
        raise HTTPException(status_code=404, detail="hashed sign-in asset not found")
    return _file_response(target, request, immutable=True)


@router.get("/", response_model=None)
async def portal_index(request: Request) -> Response:
    return _file_response(_resolve_under_app(""), request, immutable=False)


@router.get("/bucket", response_model=None)
async def bucket_page(request: Request) -> Response:
    """The files workspace. Same CSP and revalidated-HTML rules as the portal
    index; its hashed CSS/JS ride the immutable /portal-assets pipeline."""
    return _file_response(_resolve_under_app("bucket.html"), request, immutable=False)


@router.get("/portal-assets/{asset_path:path}", response_model=None)
async def portal_asset(asset_path: str, request: Request) -> Response:
    target = _resolve_under_app(asset_path)
    if not _HASHED_ASSET_RE.fullmatch(target.name):
        raise HTTPException(status_code=404, detail="hashed portal asset not found")
    return _file_response(target, request, immutable=True)


__all__ = ["router"]
