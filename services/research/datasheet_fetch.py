"""``datasheet_fetch(source)`` — PDF -> markdown (text + best-effort table layout) for
datasheets. Contract ``contracts/research_tools.md`` §1/§3/§9.

Pillar 8 legal (CONTRIBUTING.md, ``contracts/licenses.md``): the PDF extractor must be
**MIT/permissive** — ``pdfminer.six`` (MIT) is used when present. **PyMuPDF / pymupdf4llm
are AGPL and are never imported in-process here.** The dep is *optional and
feature-detected*: with no PDF tool installed the helper returns the §6 degradation shape
(``reason:"no_pdf_tool"``) — CI stays green with zero new hard dependencies.

Remote PDFs use connection-pinned public HTTP with a 32 MiB wire/decoded budget.
On a 401/403, desktop callers may retry once with a matching browser profile's
cookies through that same public client; this never renders a page or triggers
login. Tenant cells refuse browser-profile fallback. See ``_try_browser_fallback``
and ``contracts/research_tools.md`` §9.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .common import HIGH, MEDIUM, envelope, fail, is_offline, now_ts

_MAX_MD = 200_000  # cap markdown to protect the model window (matches fetch_page §3)

#: Browser-fallback refusal reasons that must reach the model VERBATIM instead of
#: being flattened into the generic ``network`` degradation. ``cooldown_active``
#: means the site's 429/403 backoff is armed for longer than this tool's whole
#: 180 s harness budget (``tool_guidance/research.py``), so it is not a transient
#: fetch error the model should retry — it is a wall it must route around, and the
#: refusal already carries ``retry_after_s`` and the remedy.
_ACTIONABLE_BROWSER_REASONS = frozenset({"cooldown_active"})


def _load_extractor():
    """Return a ``(text_fn, name)`` for an installed MIT PDF extractor, or ``None``.
    Only permissive-licensed tools are considered; AGPL PyMuPDF is intentionally excluded."""
    try:
        from pdfminer.high_level import extract_text  # type: ignore  # MIT
        return extract_text, "pdfminer.six"
    except Exception:  # noqa: BLE001 - optional dep absent
        return None


def _to_markdown(text: str) -> str:
    """Best-effort text -> markdown: collapse runaway blank lines, keep line structure so
    tabular whitespace survives. (True cell-grid recovery is a later enhancement.)"""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip():
            out.append(ln)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    md = "\n".join(out).strip()
    return md[:_MAX_MD]


def _try_browser_fallback(
    url: str,
) -> tuple[Optional[bytes], Optional[str], Optional[dict]]:
    """On a 401/403 from a plain fetch, retry ``url`` via the authenticated browser
    session — ONLY when a matching site profile already exists (never triggers a login
    from this path; a missing profile just falls through to the existing degradation).

    ``services.browser`` is an optional peer package (Playwright is one of its own lazy
    runtime deps) — any import failure here degrades to "no fallback available", same
    posture as the ``_load_extractor`` optional-dependency check above.

    Returns ``(data, session_profile_used, refusal)``. ``refusal`` carries the browser's
    own §6 envelope when it declined for a reason the model must ACT on rather than see
    flattened into ``network`` — today that is ``cooldown_active``: the site's 429/403
    backoff outlasts this tool's 180 s budget, so "could not fetch: HTTP 403" would be a
    lie and burying it behind ``raise_for_status`` costs the caller its whole turn.
    """
    try:
        from services.browser import profiles as _profiles
        from services.browser.session import browser_download
    except Exception:  # noqa: BLE001 - services.browser / playwright not available
        return None, None, None

    site = urlparse(url).netloc
    if not site:
        return None, None, None
    # No has_profile gate: anonymous downloads are allowed (browser_download
    # auto-creates a profile); gated files fail via the result envelope below.
    _ = _profiles  # kept for the availability probe in the import above

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "datasheet.pdf"
        result = browser_download(url, site, dest, public_only=True)
        if not result.get("ok"):
            return None, None, (result if result.get("reason") in _ACTIONABLE_BROWSER_REASONS
                                else None)
        try:
            data = dest.read_bytes()
        except OSError:
            return None, None, None
        return data, result.get("session_profile_used"), None


def _read_bytes(source: str) -> tuple[Optional[bytes], Optional[dict], Optional[str]]:
    """Load PDF bytes from a local path or URL.

    Returns ``(data, None, session_profile_used)`` on success or ``(None, degradation, None)``
    on failure. ``session_profile_used`` is non-``None`` only when the authenticated-
    browser fallback served the bytes (additive; ``None`` for the plain-fetch and local-file
    paths). When the fallback refuses with an actionable
    reason (:data:`_ACTIONABLE_BROWSER_REASONS`) that envelope IS the degradation returned,
    so ``cooldown_active`` + ``retry_after_s`` survive to the tool boundary instead of being
    replaced by ``raise_for_status``'s "HTTP 403" a line later.
    """
    s = str(source).strip()
    if s.lower().startswith(("http://", "https://")):
        if is_offline():
            return None, fail("offline", "Offline mode blocks fetching a remote PDF; pass a local path."), None
        try:
            from services.net.safe_http import get
            r = get(s, max_bytes=32 * 1024 * 1024, idle_timeout=30.0,
                    headers={"User-Agent": "Chudware-research/1.0"})
            if r.status_code in (401, 403):
                data, session_profile_used, refusal = _try_browser_fallback(s)
                if data is not None:
                    return data, None, session_profile_used
                if refusal is not None:
                    return None, refusal, None
            r.raise_for_status()
            return r.content, None, None
        except Exception as exc:  # noqa: BLE001
            return None, fail("network", f"Could not fetch {s!r}: {exc}."), None
    if not os.path.isfile(s):
        return None, fail("bad_url", f"No such PDF file: {s!r} (pass a local path or http(s) URL)."), None
    try:
        with open(s, "rb") as fh:
            return fh.read(), None, None
    except OSError as exc:
        return None, fail("bad_url", f"Could not read {s!r}: {exc}."), None


def datasheet_fetch(source: str, tier: str = "datasheet") -> dict:
    """Extract a datasheet PDF (local path or URL) to markdown, returning the §1 envelope
    plus ``url``/``title``/``content``. Requires an installed MIT PDF tool
    (``pdfminer.six``); its absence degrades (§6 ``no_pdf_tool``) rather than raising."""
    if not source or not str(source).strip():
        return fail("bad_url", "Provide a PDF path or http(s) URL.")

    extractor = _load_extractor()
    if extractor is None:
        return fail("no_pdf_tool",
                    "No permissive PDF extractor installed. Add MIT-licensed 'pdfminer.six' "
                    "(pip install pdfminer.six). PyMuPDF/pymupdf4llm are AGPL and excluded.")
    extract_text, tool = extractor

    data, err, session_profile_used = _read_bytes(source)
    if err is not None:
        return err

    try:
        text = extract_text(io.BytesIO(data)) or ""
    except Exception as exc:  # noqa: BLE001 - never raise across the boundary
        return fail("network", f"PDF extraction failed via {tool}: {exc}.")

    md = _to_markdown(text)
    if not md:
        return fail("not_found", f"{source!r} yielded no extractable text (scanned/image PDF?).")

    is_url = str(source).lower().startswith(("http://", "https://"))
    title = os.path.basename(str(source).split("?")[0]) or "datasheet.pdf"
    return envelope(
        f"{title} — datasheet extracted to markdown ({len(md)} chars via {tool})",
        units=None,
        source_url=source if is_url else None,
        quote=None,
        fetched_ts=now_ts(),
        confidence=MEDIUM if len(md) < len(text or "") else HIGH,
        url=source if is_url else None,
        title=title,
        content=md,
        extractor=tool,
        tier=tier,
        session_profile_used=session_profile_used,
    )
