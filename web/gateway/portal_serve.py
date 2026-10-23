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
_FONT_DIR = "fonts"
# packaging/ is a sibling of Webservice/ in the checkout the gateway runs from
# (chudware-gateway.service sets WorkingDirectory to
# ~/Repositories/CAD/Webservice/gateway), so the script served at /install.sh is
# the same file the Linux packaging pipeline is built against — there is no
# second copy to keep in sync.
_INSTALL_SCRIPT = (
    Path(__file__).resolve().parents[2] / "packaging" / "linux" / "install.sh"
)

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
        "Vary": "Accept-Encoding",
    }
    if encoding:
        headers["Content-Encoding"] = encoding
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


@router.get("/marketplace", response_model=None)
async def marketplace_page(request: Request) -> Response:
    """The 3D model storefront.

    Served to ANONYMOUS callers on purpose. ``auth.PUBLIC_PATHS`` covers
    ``/marketplace`` so the middleware does not demand a session, because a
    marketplace nobody can link to or crawl is not a marketplace. The page then
    calls ``/api/marketplace/*``, whose read routes use
    ``marketplace_authz.optional_user`` and personalise themselves only when a
    session happens to exist.

    Revalidated HTML, immutable hashed assets — the same split as the portal
    index and ``/bucket``.
    """
    return _file_response(_resolve_under_app("marketplace.html"), request, immutable=False)


@router.get("/studio", response_model=None)
async def studio_page(request: Request) -> Response:
    """The seller studio.

    NOT public, and deliberately not listed in ``auth.PUBLIC_PATHS``: every
    ``/api/studio/*`` route requires a session, so serving the shell to an
    anonymous caller would render a page whose every request 401s. The
    middleware redirects them to sign-in instead.
    """
    return _file_response(_resolve_under_app("studio.html"), request, immutable=False)


@router.get("/marketplace-assets/{asset_path:path}", response_model=None)
async def marketplace_asset(asset_path: str, request: Request) -> Response:
    """Immutable hashed storefront assets, reachable without a session.

    A separate route from ``/portal-assets`` purely to keep the public surface
    minimal. ``auth.PUBLIC_PATHS`` has to name a prefix, and naming
    ``/portal-assets`` would expose the account dashboard's and the file
    bucket's bundles as well to reach these two files. The ``marketplace``
    filename requirement is the same restriction ``/auth-assets`` applies to
    ``login``, and it is what stops this public prefix from being a general
    reader for every hashed asset on disk.
    """
    target = _resolve_under_app(asset_path)
    if not _HASHED_ASSET_RE.fullmatch(target.name) or not target.name.startswith(
        "marketplace"
    ):
        raise HTTPException(status_code=404, detail="hashed marketplace asset not found")
    return _file_response(target, request, immutable=True)


@router.get("/portal-assets/{asset_path:path}", response_model=None)
async def portal_asset(asset_path: str, request: Request) -> Response:
    target = _resolve_under_app(asset_path)
    if not _HASHED_ASSET_RE.fullmatch(target.name):
        raise HTTPException(status_code=404, detail="hashed portal asset not found")
    return _file_response(target, request, immutable=True)


@router.get("/brand-assets/{asset_path:path}", response_model=None)
async def brand_asset(asset_path: str, request: Request) -> Response:
    """The two self-hosted typefaces, reachable without a session.

    A third public asset prefix exists because neither of the other two can
    serve a font to an anonymous visitor: ``/portal-assets`` is not in
    ``auth.PUBLIC_PATHS`` at all, and ``/auth-assets`` additionally requires the
    filename to start with "login". The sign-in page, the storefront and the
    seller studio all link ``base.css``, which asks for Chudware Sans and
    Chudware Mono by family name — so without a public font route every one of
    those pages renders its headings in whatever the system happens to pick,
    which is exactly the "off-brand typography" this surface was rebuilt to fix.

    The surface opened is two WOFF2 files. This route will not serve anything
    else: the name must be a bare content-hashed ``*.woff2`` basename, it is
    resolved only inside ``static/fonts/``, and any directory component,
    traversal attempt or other suffix is a 404 rather than a hint that the file
    exists. Fonts are content-hashed, so they are immutable like every other
    hashed asset.
    """
    name = asset_path.strip("/")
    if (
        "/" in name
        or not name.lower().endswith(".woff2")
        or not _HASHED_ASSET_RE.fullmatch(name)
    ):
        raise HTTPException(status_code=404, detail="brand asset not found")
    target = _resolve_under_app(f"{_FONT_DIR}/{name}")
    return _file_response(target, request, immutable=True)


@router.get("/install.sh", response_model=None)
async def install_script(request: Request) -> Response:
    """The Linux one-line installer, served as plain text.

    Public and anonymous on purpose: the command a customer is told to run is
    ``curl -fsSL …/install.sh | sh -s -- --token <token>``, and the token is
    what authorises the DOWNLOAD, not the fetch of the script. Requiring a
    session here would only mean the pipe silently received an HTML sign-in page
    instead of a script.

    ``text/plain`` rather than a shell MIME type so a browser shows it — a
    script people are asked to pipe into a shell must be readable first — and
    five minutes of caching so a fix reaches the next visitor quickly.
    """
    if not _INSTALL_SCRIPT.is_file():
        raise HTTPException(status_code=503, detail="install script is not deployed")
    return FileResponse(
        str(_INSTALL_SCRIPT),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Security-Policy": _PORTAL_CSP,
            "Cache-Control": "public, max-age=300",
            # Read in a browser, never rendered: this is text, and nothing may
            # sniff it into something executable.
            "X-Content-Type-Options": "nosniff",
        },
    )


# Fixed-name site files every crawler and browser asks the host itself for.
# Google Search shows a result's own icon only if it can crawl one linked from
# the hostname's home page — a data: URI does not count, and before these were
# public /favicon.ico answered 401, so every app.chudware.com result wore the
# generic globe. Generated with chudware.com's set by
# public-site/src/scripts/make-favicons.sh. Not content-hashed, so a day of
# caching rather than immutable.
_SITE_FILES: dict[str, str] = {
    "/favicon.ico": "icons/favicon.ico",
    "/favicon.svg": "icons/favicon.svg",
    "/icon-192.png": "icons/icon-192.png",
    "/apple-touch-icon.png": "icons/apple-touch-icon.png",
    "/robots.txt": "robots.txt",
}


async def _site_file(request: Request) -> Response:
    target = _resolve_under_app(_SITE_FILES[request.url.path])
    return FileResponse(
        str(target),
        media_type=_media_type(target),
        headers={"Cache-Control": "public, max-age=86400"},
    )


for _site_path in _SITE_FILES:
    router.add_api_route(_site_path, _site_file, methods=["GET"], response_model=None)


__all__ = ["router"]
