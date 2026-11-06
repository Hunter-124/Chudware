"""services/browser/adapters — the SourceAdapter contract + registry.

Every adapter searches a source site (under the user's own saved browser
session) for candidate parts/documents and acquires a chosen result,
landing the file locally with a provenance envelope styled on the
``aicad.calc/1`` envelope (``contracts/advisor.md`` §7) and the ``research_*``
answer envelope (``contracts/research_tools.md`` §1): every acquired artifact
carries ``{source_url, fetched_ts, session_profile_used, license_note}``.

Adapter authors: build search rows with ``make_hit`` and absolute product-image
URLs, and return ``[fail(...)]`` on search failure. Use ``quote_query`` for
search terms, ``abs_url`` for links, and ``_retail.card_image`` for card photos.
Acquisition envelopes identify ``artifact_kind`` and ``format``; a datasheet
is a document, never CAD. Report sign-in, captcha and blocking distinctly.

Adapters are DATA-heavy, CODE-light — nearly everything that differs between
GrabCAD and TraceParts lives in each module's site config (URLs, regexes,
license note), not in divergent control flow. No adapter imports Playwright
directly; only :mod:`services.browser.session`'s three entry points.

This package intentionally does NOT import ``services.agent`` (the agent
layer's surface) — the ``login_required`` question below matches
the frozen ``contracts/interaction.md`` §1/§2 shape by hand so adapters stay
decoupled from the agent loop's implementation.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote_plus, urljoin, urlparse

from services import paths
from services.research.common import now_ts

from ..profiles import slug


HIT_KINDS = ("product", "series", "category", "document", "model", "project", "page")
FAIL_REASONS = ("blocked", "captcha", "login_required", "not_found", "parse_failed", "timeout",
                "transport_failed", "policy_refused", "unsupported", "terms_required", "rate_limited",
                "credentials_required", "internal_error")


def quote_query(q: str) -> str:
    return quote_plus(q)


def abs_url(base: str, href: str | None) -> str | None:
    if not isinstance(href, str) or not href.strip():
        return None
    try:
        url = urljoin(base, href.strip())
        parsed = urlparse(url)
        return url if parsed.scheme.lower() in ("http", "https") and parsed.hostname else None
    except (TypeError, ValueError):
        return None


def make_hit(site: str, *, title: str, url: str, kind: str = "product",
             sku: str | None = None, mpn: str | None = None, manufacturer: str | None = None,
             price: float | None = None, currency: str | None = None, stock: int | None = None,
             material: str | None = None, thumbnail: str | None = None,
             model_formats: list[str] | None = None, model_available: bool | None = None,
             license: str | None = None, extra: dict | None = None) -> dict:
    thumbnail = abs_url(url, thumbnail)
    if thumbnail and re.search(
            r"(?:^|[/_.-])(?:logo|sprite|placeholder|favicon|icon|icons|lazy|loading|spinner|missing|pdf|pdfhtml|datasheet)(?:[/_.?-]|$)",
            urlparse(thumbnail).path.lower()):
        thumbnail = None
    return {"site": site, "title": title, "url": url, "kind": kind, "sku": sku,
            "mpn": mpn, "manufacturer": manufacturer, "price": price,
            "currency": currency, "stock": stock, "material": material,
            "thumbnail": thumbnail,
            "model": {"available": model_available, "formats": list(model_formats or [])},
            "license": license, **(extra or {})}


def fail(site: str, op: str, reason: str, hint: str = "", *, url: str | None = None,
         status: int | None = None, retry_after_s: float | None = None,
         ask_user: str | None = None) -> dict:
    return {"ok": False, "site": site, "op": op, "reason": reason, "hint": hint,
            "url": url, "status": status, "retry_after_s": retry_after_s, "ask_user": ask_user}


def model_selection_question(site: str, url: str, candidates: list[str]) -> dict:
    """Ask before choosing between different model files; default to no download."""
    identity = hashlib.sha256("\0".join((site, url, *candidates)).encode("utf-8")).hexdigest()[:16]
    return {"id": f"browser_model_selection.{identity}", "kind": "choice",
            "prompt": f"Which {site} model should be acquired?",
            "suggested": "cancel", "required": True,
            "why": "The source lists different models or configurations.",
            "options": [{"value": "cancel", "label": "Do not download"}] +
                       [{"value": name, "label": name} for name in candidates]}


def source_failure(site: str, op: str, result: dict, *, url: str | None = None) -> dict:
    """Enrich an upstream failure, retaining details and any legacy reason."""
    original = result.get("reason") or "transport_failed"
    aliases = {"unsafe_filename": "policy_refused", "cell_refused": "policy_refused",
               "cooldown_active": "rate_limited", "bot_walled": "blocked",
               "challenge_page": "captcha", "browser_fetch_failed": "transport_failed",
               "browser_download_failed": "transport_failed", "too_large": "policy_refused",
               "unsupported_format": "unsupported"}
    status = result.get("status")
    reason = aliases.get(original, original)
    if original == "http_error" or (original.startswith("http_") and original[5:].isdigit()):
        if status is None and original.startswith("http_") and original[5:].isdigit():
            status = int(original[5:])
        reason = {403: "blocked", 404: "not_found", 429: "rate_limited"}.get(
            status, "transport_failed" if isinstance(status, int) and status >= 500 else original)
    envelope = fail(site, op, reason, result.get("hint") or result.get("detail") or "",
                    url=result.get("url") or url, status=status,
                    retry_after_s=result.get("retry_after_s"), ask_user=result.get("ask_user"))
    extra = {"original_reason": original} if reason != original else {}
    return {**result, **envelope, **extra, "site": site, "op": op}


class SourceAdapter(Protocol):
    key: str
    hosts: tuple[str, ...]
    result_kinds: tuple[str, ...]
    cad_acquisition: bool
    login_required: bool
    api: bool

    def search(self, query: str) -> list[dict]: ...

    def acquire(self, result: dict) -> dict: ...


_REGISTRY: dict[str, "SourceAdapter"] = {}

# Defaults document existing implementations; adapters can override these class
# attributes as their capabilities change. Hosts come from each module's BASE_URL.
_CAD_SITES = frozenset({
    "3dcontentcentral", "grabcad", "traceparts", "mcmaster", "misumi", "snapeda",
    "parker", "ultralibrarian", "componentsearchengine", "maxon", "orientalmotor",
    "anaheimautomation", "thk", "thomson", "igus", "boschrexroth", "hydraforce",
    "hiwin", "freecadlibrary", "printables",
})
_API_SITES = frozenset({"printables", "freecadlibrary", "futureelectronics"})
_SPECIAL_HOSTS = {"makerworld": ("makerworld.com",), "printables": ("printables.com",),
                  "cui": ("sameskydevices.com",)}
_RESULT_KINDS = {
    "3dcontentcentral": ("model",), "grabcad": ("model",), "printables": ("model",),
    "makerworld": ("model",), "freecadlibrary": ("model",), "pcbway": ("project",),
    "mcmaster": ("product", "series", "category"), "misumi": ("series", "category"),
    "digikey": ("product", "category"), "wch": ("document",), "datasheet4u": ("document",),
    "hdsc": ("page",), "meanwell": ("series",), "applerubber": ("page",),
}


def register(adapter: "SourceAdapter") -> "SourceAdapter":
    """Register an adapter; a module reload may replace the same adapter class."""
    previous = _REGISTRY.get(adapter.key)
    cls = type(adapter)
    if previous is not None and (type(previous).__module__, type(previous).__qualname__) != (
            cls.__module__, cls.__qualname__):
        raise ValueError(f"duplicate adapter key: {adapter.key}")
    module = sys.modules.get(cls.__module__)
    base = getattr(adapter, "base_url", "") or getattr(module, "BASE_URL", "")
    hostname = (urlparse(base).hostname or "").lower().removeprefix("www.")
    defaults = {
        "hosts": tuple(dict.fromkeys(([hostname] if hostname else []) +
                                     list(_SPECIAL_HOSTS.get(adapter.key, ())))),
        "result_kinds": _RESULT_KINDS.get(adapter.key, ("product",)),
        "cad_acquisition": adapter.key in _CAD_SITES,
        "login_required": adapter.key in _LOGIN_WIZARD_SITES,
        "api": adapter.key in _API_SITES,
    }
    for name, value in defaults.items():
        if name not in cls.__dict__:
            setattr(cls, name, value)
    _REGISTRY[adapter.key] = adapter
    return adapter


def get_adapter(key: str) -> "SourceAdapter | None":
    return _REGISTRY.get(key)


def all_adapters() -> list["SourceAdapter"]:
    return list(_REGISTRY.values())


# Sites where the login wizard provisions a session needed to **download importable
# CAD/ECAD artifacts** (STEP, IGES, symbol libraries, etc.). Commerce / link-out
# vendors (Amazon, retail adapters, PCB quoting) are intentionally excluded —
# they do not gate geometry acquisition behind a user login.
_LOGIN_WIZARD_SITES: frozenset[str] = frozenset({
    "3dcontentcentral",
    "grabcad",
    "mcmaster",
    "misumi",
    "snapeda",
    "makerworld",
    "traceparts",
    # Wave 3/4 CAD-download-behind-login sites (STEP/IGES/symbol libs behind a
    # free account). Commerce/datasheet + open sources (freecadlibrary, gbstandards,
    # the datasheet DBs, materials/composites storefronts) are intentionally absent.
    "parker",
    "ultralibrarian",
    "componentsearchengine",
    "cadenas",
    "maxon",
    "orientalmotor",
    "anaheimautomation",
    "thk",
    "thomson",
    "igus",
})

# Distributor-only manufacturers: they publish CAD/datasheets but sell exclusively
# through distributors and have NO individual customer accounts. Offering a "Sign
# in" here is a dead end (confirmed by the user for all three). They are kept OUT of
# _LOGIN_WIZARD_SITES so the wizard never proposes a login; if a fetch hits a wall,
# the adapter returns :func:`distributor_only` (an informational result, not a login
# prompt). CAD/datasheet fetches that are publicly reachable still proceed normally.
_DISTRIBUTOR_ONLY_SITES: frozenset[str] = frozenset({
    "boschrexroth",   # B2B only — no individual purchase
    "hydraforce",     # sells through distributors
    "hiwin",          # sells through distributors
})


def login_wizard_sites() -> list[str]:
    """Registered adapters that need a saved login to download models/libraries."""
    return [a.key for a in all_adapters() if a.key in _LOGIN_WIZARD_SITES]


def provenance_envelope(*, source_url: str, session_profile_used: str, license_note: str,
                        artifact_path: str | None = None, fetched_ts: str | None = None,
                        **extra: Any) -> dict:
    """The acquisition envelope. Additive fields pass through ``extra`` and sit
    alongside the required four, matching the "helpers add fields, never rename them"
    rule shared by ``contracts/advisor.md`` §7 and ``contracts/research_tools.md`` §1."""
    env: dict[str, Any] = {
        "ok": True,
        "source_url": source_url,
        "fetched_ts": fetched_ts or now_ts(),
        "session_profile_used": session_profile_used,
        "license_note": license_note,
    }
    if artifact_path is not None:
        env["artifact_path"] = artifact_path
    env.update(extra)
    return env


def login_required(site: str, url: str) -> dict:
    """The clean "sign in needed" degradation shape — an `approval`-kind question per
    ``contracts/interaction.md`` §1/§2, embedded in an ``ok:false`` envelope. Never
    raised; a caller decides how/whether to route ``ask_user`` to the user."""
    question = {
        "id": f"browser_login.{slug(site)}",
        "kind": "approval",
        "prompt": f"Sign into {site} to continue? A browser window will open for you.",
        "suggested": True,
        "required": True,
        "why": f"{url} requires an active {site} session; none is saved yet.",
        "proposal": f"Open a browser window for you to log into {site}.",
    }
    return {"ok": False, "reason": "login_required", "hint": question["prompt"], "ask_user": question}


def distributor_only(site: str, url: str, *, distributors_url: str | None = None) -> dict:
    """Honest degradation for manufacturers that sell ONLY through distributors and
    have no individual customer accounts. Unlike :func:`login_required`, this offers
    NO sign-in (there is nothing to sign into) — it's an informational envelope so a
    caller can point the user at the distributor locator instead of a dead-end login
    window. ``ask_user`` is omitted deliberately: no approval to collect."""
    return {
        "ok": False,
        "reason": "distributor_only",
        "hint": f"{site} sells through distributors — no individual account to sign into.",
        "distributors_url": distributors_url,
        "source_url": url,
    }


def distributor_only_sites() -> list[str]:
    """Registered adapters that are distributor-only (no customer login exists)."""
    return [a.key for a in all_adapters() if a.key in _DISTRIBUTOR_ONLY_SITES]


def terms_confirmation_required(site: str, url: str, terms_summary: str) -> dict:
    """Same shape as :func:`login_required` for a per-download terms click-through this
    adapter must not accept on the user's behalf (e.g. TraceParts)."""
    question = {
        "id": f"browser_terms.{slug(site)}",
        "kind": "approval",
        "prompt": f"Accept {site}'s terms to download this file?",
        "suggested": False,
        "required": True,
        "why": f"{url} — {terms_summary}",
        "proposal": f"Accept the terms shown on {url} to proceed with this download.",
    }
    return {"ok": False, "reason": "terms_confirmation_required", "hint": question["prompt"],
            "ask_user": question}


def is_login_wall(content: str, markers: tuple[str, ...]) -> bool:
    """True when fetched HTML looks like a login page rather than the requested content.

    A simple substring heuristic (each site config supplies its own marker
    strings) — real markup verification is the gated integration suite's job,
    not something this v1 can confirm against a live site without a login.
    """
    return any(marker in content for marker in markers)


def download_public_cad(url: str, dest: Path, *, max_bytes: int = 200 * 1024 * 1024,
                        requested_format: str | None = None, requested_member: str | None = None) -> dict:
    """Stream a public HTTPS CAD file without a browser, with a hard byte cap.

    Follow only validated public HTTPS redirects (the research HTTP policy);
    never issue a request to a private or credential-bearing endpoint.
    """
    from services.browser.cad_validate import validate_cad
    from services.net.safe_http import download, OutboundPolicyError, ResponseTooLarge

    staging = None
    try:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".cad-download-",
                                         suffix=dest.suffix, delete=False) as temp:
            staging = Path(temp.name)
        response = download(url, staging, max_bytes=max_bytes, idle_timeout=30.0,
                            max_redirects=6, schemes=("https",))
        if response.status_code >= 400:
            return source_failure(urlparse(url).hostname or "", "download",
                                  {"ok": False, "reason": f"http_{response.status_code}",
                                   "status": response.status_code}, url=url)
        checked = validate_cad(staging, requested_format=requested_format, requested_member=requested_member)
        if not checked["ok"]:
            site = urlparse(url).hostname or ""
            candidates = checked.get("candidates")
            return source_failure(site, "download",
                                  {"ok": False, "reason": "unsupported_format", "detail": checked["reason"],
                                   "candidates": candidates,
                                   "selection_field": "archive_member" if candidates else None,
                                   "ask_user": model_selection_question(site, url, candidates) if candidates else None},
                                  url=url)
        os.replace(staging, dest)
        return {"ok": True, "dest": str(dest), "url": response.url,
                "status": response.status_code, "bytes": response.bytes_written}
    except ResponseTooLarge as exc:
        return source_failure(urlparse(url).hostname or "", "download",
                              {"ok": False, "reason": "too_large", "detail": str(exc)}, url=url)
    except OutboundPolicyError as exc:
        return fail(urlparse(url).hostname or "", "download", "policy_refused", str(exc), url=url)
    except TimeoutError as exc:
        return fail(urlparse(url).hostname or "", "download", "timeout", str(exc), url=url)
    except (OSError, RuntimeError) as exc:
        return fail(urlparse(url).hostname or "", "download", "transport_failed", str(exc), url=url)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def download_dir(site: str) -> Path:
    """Local landing directory for artifacts acquired from ``site``.

    A sibling of ``vendor_parts`` under the app data dir (``services.paths``).
    This is where acquired bytes land before parts-registry ingestion.
    """
    d = paths.BROWSER_DOWNLOADS / slug(site)
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_path(site: str, url: str, *, default="download.bin",
                  filename: str | None = None) -> tuple[Path | None, dict | None]:
    """Return a canonical destination or the adapters' unsafe-filename refusal."""
    from services.net.safe_http import OutboundPolicyError, safe_download_path

    try:
        return safe_download_path(download_dir(site), url, default=default,
                                  filename=filename), None
    except OutboundPolicyError as exc:
        return None, {"ok": False, "reason": "unsafe_filename", "hint": str(exc)}


# Side-effecting: each site module calls register(...) at import time. Imported here
# (not left to whoever happens to import a specific site module first) so all_adapters()
# is reliably populated as soon as this package's top level is imported.
from . import (amazon, content3d, digikey_web, grabcad, jlcpcb, lcsc, mcmaster,  # noqa: E402,F401
               misumi, pcbway, snapeda, traceparts)
from . import makerworld, printables  # noqa: E402,F401 — public CAD communities
from . import (acehardware, fastenal, grainger, harborfreight, homedepot,  # noqa: E402,F401
               lowes, metalsdepot, onlinemetals, tractorsupply)
# Wave 3 (2026-07-07): supplier expansion — hydraulics/fluid-power, Chinese &
# foreign electronics, materials, engineered-products CAD portals + the RE-feed
# datasheet/standards DBs.
from . import (boschrexroth, hydraforce, parker,  # hydraulics CAD  # noqa: E402,F401
               speedymetals, coremarkmetals, professionalplastics,  # metals/plastics
               rockwest, acpcomposites, fibreglast,  # composites
               surpluscenter, baileyhydraulics,  # hydraulics commerce
               wch, hdsc, dfrobot, seeedstudio, futureelectronics, mouser, nexar,  # electronics distributors
               datasheet4u,  # datasheet DB (RE feed)
               ultralibrarian, componentsearchengine,  # ECAD CAD libraries
               freecadlibrary,  # open git STEP repo (no login)
               cadenas,  # CADENAS 3Dfindit/PARTcommunity portal engine
               gbstandards)  # free GB/T full-text standards (RE feed)
# Wave 4 (2026-07-07): electro-mechanical — electric motors, motion control,
# seals/gaskets, batteries, power supplies.
from . import (maxon, orientalmotor, anaheimautomation,  # motors  # noqa: E402,F401
               thk, hiwin, thomson, igus,  # motion control
               applerubber, marcorubber, globaloring,  # seals/gaskets
               meanwell, cui, batteryspace)  # power supplies / batteries
# Wave 5 (2026-07-08): student-robotics / rover-scale sourcing — structural +
# motion components sized for hobby robotics, small gearmotors/controllers with
# real spec sheets, and T-slot aluminum extrusion.
from . import (servocity, pololu,  # robotics structural + motion / gearmotors  # noqa: E402,F401
               eightytwenty)  # 80/20 T-slot extrusion (bot-walled — degrades honestly)

# --- Reload safety -----------------------------------------------------------
# ``importlib.reload(<this package>)`` re-executes this file, which resets
# ``_REGISTRY`` to empty — but the site-module imports above become no-ops on
# reload (each submodule is already in ``sys.modules``, so its module-level
# ``register(...)`` side-effect does NOT run again). That would leave a reloaded
# package with an empty registry, silently breaking ``all_adapters()`` /
# ``login_wizard_sites()`` / ``get_adapter()`` for whatever runs next. Test
# fixtures reload this package after tweaking ``services.paths``/``session``, so
# re-register every already-imported adapter submodule here to keep reload a
# no-op for the registry. On first import this is harmless (each key is already
# present); on reload it repopulates the emptied registry.
import sys as _sys  # noqa: E402
_pkg_prefix = __name__ + "."
for _mod in list(_sys.modules.values()):
    _adapter = getattr(_mod, "ADAPTER", None)
    if _adapter is not None and getattr(_mod, "__name__", "").startswith(_pkg_prefix):
        register(_adapter)
del _sys, _pkg_prefix
