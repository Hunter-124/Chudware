"""services/browser/adapters — the SourceAdapter contract + registry.

Every adapter searches a source site (under the user's own saved browser
session) for candidate parts/documents and acquires a chosen result,
landing the file locally with a provenance envelope styled on the
``aicad.calc/1`` envelope (``contracts/advisor.md`` §7) and the ``research_*``
answer envelope (``contracts/research_tools.md`` §1): every acquired artifact
carries ``{source_url, fetched_ts, session_profile_used, license_note}``.

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

from pathlib import Path
from typing import Any, Protocol

from services import paths
from services.research.common import now_ts

from ..profiles import slug


class SourceAdapter(Protocol):
    key: str

    def search(self, query: str) -> list[dict]: ...

    def acquire(self, result: dict) -> dict: ...


_REGISTRY: dict[str, "SourceAdapter"] = {}


def register(adapter: "SourceAdapter") -> "SourceAdapter":
    """Register ``adapter`` under its ``key`` and return it (use as a decorator-ish call
    at module load: ``ADAPTER = register(GrabCADAdapter())``)."""
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


def download_public_cad(url: str, dest: Path, *, max_bytes: int = 200 * 1024 * 1024) -> dict:
    """Stream a public HTTPS CAD file without a browser, with a hard byte cap.

    Follow only validated public HTTPS redirects (the research HTTP policy);
    never issue a request to a private or credential-bearing endpoint.
    """
    from services.net.safe_http import download, OutboundPolicyError, ResponseTooLarge

    saved = False
    try:
        response = download(url, dest, max_bytes=max_bytes, idle_timeout=30.0,
                            max_redirects=6, schemes=("https",))
        if response.status_code >= 400:
            return {"ok": False, "reason": f"http_{response.status_code}",
                    "status": response.status_code}
        saved = True
        return {"ok": True, "dest": str(dest), "url": response.url,
                "status": response.status_code, "bytes": response.bytes_written}
    except ResponseTooLarge as exc:
        return {"ok": False, "reason": "too_large", "detail": str(exc)}
    except OutboundPolicyError as exc:
        return {"ok": False, "reason": "unsupported_format", "detail": str(exc)}
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "reason": "no_cad_link", "detail": str(exc)}
    finally:
        if not saved:
            dest.unlink(missing_ok=True)


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
               wch, hdsc, dfrobot, seeedstudio, futureelectronics, mouser,  # electronics distributors
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
