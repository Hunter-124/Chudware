"""Serve the WASM web client static tree at /app with COOP/COEP headers.

Browser SharedArrayBuffer / pthread builds need:
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
on every asset under /app (html, js, wasm, data, …). Auth is enforced by
AuthMiddleware — /app is intentionally NOT on the public allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

try:
    from . import orgs
    from . import authn
    from .accounts import get_account
    from .auth import get_current_user
    from .config import settings
    from .plans import limits_for
except ImportError:  # uvicorn app:app from package dir
    import orgs
    import authn
    from accounts import get_account
    from auth import get_current_user
    from config import settings
    from plans import limits_for

log = logging.getLogger("chudware-web.appserve")

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
    # The Emscripten bundle files are UNHASHED (fixed names Chudware.js/.wasm/.data) and MUST
    # update in lockstep — a cached old Chudware.js against a new Chudware.wasm is a fatal
    # LinkError (mismatched imports). Force revalidation so js/wasm never skew across deploys.
    if path.suffix.lower() in {".html", ".htm", ".js", ".wasm", ".data", ".mjs"}:
        return "no-cache"
    return "max-age=3600"


# The /app bundle is Emscripten WASM, so the site-wide CSP set in app.py is
# wrong for it in two ways and silently kills the whole client:
#   * Chrome refuses WebAssembly.instantiate under a bare `script-src 'self'`
#     — compiling wasm needs 'wasm-unsafe-eval'.
#   * Chudware.js starts a worker from a createObjectURL blob, and worker-src
#     falls back to default-src, so 'self' alone blocks it.
# app.py's security middleware uses setdefault, so this value wins for /app.
_APP_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "
    "worker-src 'self' blob:; "
    "child-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' ws: wss: blob: data:; "
    "font-src 'self' data:; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)


# The app's index.html carries inline bootstrap <script> blocks. Rather than
# weakening script-src with 'unsafe-inline' (which would forfeit the XSS
# protection on an authenticated surface), hash them and name the hashes. The
# bundle is rebuilt by install_webd.sh, so the hashes are computed from the
# file on disk and re-derived whenever its mtime/size changes — a rebuild can
# never silently lock the client out again.
_INLINE_SCRIPT_RE = re.compile(
    rb"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_csp_cache: dict[tuple[str, int, int], str] = {}


def _csp_for_html(path: Path) -> str:
    """_APP_CSP plus a sha256 source for each inline script in *path*."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return _APP_CSP
    cached = _csp_cache.get(key)
    if cached is not None:
        return cached
    try:
        body = path.read_bytes()
    except OSError:
        return _APP_CSP
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m.group(1)).digest()).decode() + "'"
        for m in _INLINE_SCRIPT_RE.finditer(body)
    )
    csp = _APP_CSP.replace(
        "script-src 'self' 'wasm-unsafe-eval'",
        f"script-src 'self' 'wasm-unsafe-eval' {hashes}".rstrip(),
    ) if hashes else _APP_CSP
    # Bounded: one entry per app html file, re-keyed on mtime.
    if len(_csp_cache) > 64:
        _csp_cache.clear()
    _csp_cache[key] = csp
    return csp


def _coop_headers(path: Path) -> dict[str, str]:
    csp = _csp_for_html(path) if path.suffix.lower() in {".html", ".htm"} else _APP_CSP
    return {
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Embedder-Policy": "require-corp",
        "Content-Security-Policy": csp,
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


# Precompressed-variant negotiation. build.sh stages .br/.gz siblings next to
# the bundle files. Preserve the ORIGINAL media type and vary on Accept-Encoding
# so shared caches cannot mix variants.
def _fresh_variant(target: Path, suffix: str) -> Path | None:
    """A precompressed sibling, but only if it isn't stale w.r.t. the original.
    Guards the js/wasm-skew failure mode: a restaged bundle whose .br/.gz wasn't
    regenerated must fall back to raw, never serve outdated compressed bytes."""
    v = target.with_name(target.name + suffix)
    try:
        if v.is_file() and v.stat().st_mtime >= target.stat().st_mtime:
            return v
    except OSError:
        return None
    return None


def _negotiate(target: Path, request: Request) -> tuple[Path, str | None]:
    accept = request.headers.get("accept-encoding", "")
    if "br" in accept:
        br = _fresh_variant(target, ".br")
        if br is not None:
            return br, "br"
    if "gzip" in accept:
        gz = _fresh_variant(target, ".gz")
        if gz is not None:
            return gz, "gzip"
    return target, None
def _file_response(target: Path, request: Request) -> FileResponse:
    served, encoding = _negotiate(target, request)
    headers = _coop_headers(target)  # cache policy keys on the ORIGINAL suffix
    # Even the identity response varies: a CDN must not reuse it for a later
    # Brotli-capable client (nor serve compressed bytes to an identity client).
    headers["Vary"] = "Accept-Encoding"
    if encoding:
        headers["Content-Encoding"] = encoding
    return FileResponse(
        path=str(served),
        media_type=_media_type(target),  # original type, not .br/.gz
        headers=headers,
    )


def _upgrade_response(request: Request) -> HTMLResponse | None:
    """Return the cloud-plan upgrade page, or None when /app is entitled."""
    user = get_current_user(request)
    account = get_account(user.email)
    if (orgs.university_eligible(user.email)
            and orgs.selected_workspace_team(request, user.email) == settings.university_team_id):
        return None
    try:
        if (account is not None and limits_for(account.plan).cloud
                and not (account.plan == "trial" and authn.trial_expired(user.email))):
            return None
    except ValueError:
        pass
    return HTMLResponse(
        status_code=403,
        headers={"Cache-Control": "no-store"},
        content="""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cloud workspace upgrade required · Chudware</title></head>
<body><main><h1>Your plan does not include the cloud workspace</h1>
<p>The Desktop plan keeps the native app enabled. Upgrade to Studio to use
Chudware in this browser.</p>
<p><a href="/#pricing">Compare plans</a></p></main></body></html>""",
    )


@router.get("/app", response_model=None)
async def app_redirect(request: Request) -> Response:
    """Redirect entitled callers to the canonical slash form."""
    upgrade = _upgrade_response(request)
    if upgrade is not None:
        return upgrade
    index = _resolve_under_app("")
    return RedirectResponse(
        url="/app/" + ("?" + request.url.query if request.url.query else ""),
        status_code=307,
        headers=_coop_headers(index),
    )


@router.get("/app/", response_model=None)
async def app_index(request: Request) -> Response:
    """Serve the WASM client entry (index.html)."""
    upgrade = _upgrade_response(request)
    if upgrade is not None:
        return upgrade
    response = _file_response(_resolve_under_app(""), request)
    selection = request.query_params.get("team")
    if selection is not None:
        user = get_current_user(request)
        if selection != "personal":
            orgs.resolve_team_bucket(user.email, selection, write=False)
        response.set_cookie("chudware_workspace", selection, httponly=True,
                            secure=request.url.scheme == "https", samesite="lax", path="/")
    return response


@router.get("/app/{asset_path:path}", response_model=None)
async def app_asset(asset_path: str, request: Request) -> Response:
    """Serve a file from the WASM client tree."""
    upgrade = _upgrade_response(request)
    if upgrade is not None:
        return upgrade
    return _file_response(_resolve_under_app(asset_path), request)


__all__ = ["router"]
