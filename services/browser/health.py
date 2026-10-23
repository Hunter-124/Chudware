"""services/browser/health.py — vendor adapter health checks (Parts Library V2).

One cheap canary search per registered source adapter, timed and classified.
``run_health_check`` NEVER raises — every failure mode collapses into a
per-site result dict so ``vendors_health_check`` in ``services/parts/gui_rpc.py``
can render a status board without try/except gymnastics.

Classification:
- ok=True  — the canary search returned ≥1 hit carrying both title and url.
- ok=False, error="login_required", expected=True — the adapter degraded to a
  login/challenge ask or there is no saved profile yet. This is the EXPECTED
  state on a fresh machine, not a code failure.
- ok=False, error=<str> — a real failure (exception, empty parse, fetch error).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from contextvars import copy_context

from . import profiles
from .adapters import all_adapters, get_adapter, login_wizard_sites

#: One cheap, representative query per registered site — kept in sync with the
#: adapter registry (test_retail_adapters.py asserts full coverage).
CANARY_QUERIES: dict[str, str] = {
    # original wave
    "mcmaster": "m6 socket head cap screw",
    "grabcad": "nema 17 mount",
    "printables": "MG90S",  # public API includes model 569094-micro-servo-mg90s
    "makerworld": "MG90S",  # discovery may be browser-walled; acquire requires login
    "traceparts": "608 bearing",
    "amazon": "608zz bearing",
    "jlcpcb": "0603 resistor 10k",
    "pcbway": "esp32",
    # Generic single keywords route MISUMI to its flat keyword-result list;
    # multi-word/specific queries route to a category landing page with no flat
    # product list (see misumi.py), so the canary uses a plain keyword.
    "misumi": "bearing",
    "lcsc": "0603 resistor 10k",
    "digikey": "atmega328p",
    "snapeda": "usb-c connector",
    "3dcontentcentral": "hex bolt",
    # retail wave (Parts Library V2)
    "homedepot": "deck screws",
    "lowes": "2x4 lumber",
    "acehardware": "wd-40",
    "grainger": "6061 aluminum bar",
    "fastenal": "m8 hex bolt",
    "harborfreight": "digital caliper",
    "tractorsupply": "hitch pin",
    "onlinemetals": "6061 round tube",
    "metalsdepot": "steel angle",
    # student-robotics / rover-scale wave (2026-07-08)
    "servocity": "gearmotor",
    "pololu": "gearmotor",
    "8020": "1010 t-slot",
    # Wave 3/4/5 supplier expansion (2026-07-07/08) — queries are live-verified to
    # return hits on the sites that parse headless (vendor-adapter audit 2026-07-10);
    # for the genuinely-walled/SPA sites the query is a sensible category term (the
    # canary is only executed by the live health check, where walling is expected —
    # no test runs these live).
    "rockwest": "carbon fiber tube",
    "surpluscenter": "hydraulic cylinder",
    "baileyhydraulics": "hydraulic pump",
    "globaloring": "o-ring",
    "seeedstudio": "esp32",
    "cui": "dc fan",
    "datasheet4u": "lm358",
    "fibreglast": "fiberglass cloth",
    "acpcomposites": "carbon fiber",
    "speedymetals": "6061 aluminum",
    "professionalplastics": "acrylic sheet",
    "coremarkmetals": "steel plate",
    "maxon": "dc motor",
    "applerubber": "o-ring",
    "marcorubber": "o-ring",
    "meanwell": "LRS",  # MEAN WELL search is series-indexed (free text returns 0)
    "batteryspace": "18650 battery",
    "wch": "ch340",
    "hdsc": "HC32",  # EN search is a series/news index — generic terms return 0
    "dfrobot": "gravity sensor",  # SPA/XHR-only — degrades honestly headless
    "futureelectronics": "atmega328p",
    "mouser": "heatsink",  # catalog /c/?q= is Akamai-walled headless — degrades honestly
    "parker": "hydraulic valve",
    "boschrexroth": "linear guide",
    "hydraforce": "solenoid valve",
    "hiwin": "linear guide",
    "thk": "linear guide",
    "igus": "drylin bearing",
    "thomson": "linear actuator",
    "orientalmotor": "stepper motor",
    "anaheimautomation": "stepper motor",
    "cadenas": "hex bolt",
    "ultralibrarian": "atmega328p",
    "componentsearchengine": "atmega328p",
    "freecadlibrary": "screw",
    "gbstandards": "GB/T 70.1",
}

#: Failure reasons that mean "provision a login", not "the adapter is broken".
_EXPECTED_FAIL_REASONS = ("login_required", "no_profile", "terms_confirmation_required",
                          "playwright_not_installed", "challenge_page", "bot_walled")
_MAX_CONCURRENT_CANARIES = 3


def _check_site(site: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "site": site,
        "ok": False,
        "latency_ms": None,
        "logged_in": False,
        "hit_count": 0,
        "error": None,
    }
    try:
        result["logged_in"] = profiles.has_login(site)
    except Exception as exc:  # noqa: BLE001 — bookkeeping only, never fatal
        result["error"] = f"profile check failed: {exc}"

    adapter = get_adapter(site)
    if adapter is None:
        result["error"] = f"no adapter registered for '{site}'"
        return result

    query = CANARY_QUERIES.get(site, site)
    start = time.monotonic()
    try:
        hits = adapter.search(query)
    except Exception as exc:  # noqa: BLE001 — a health check must never raise
        result["latency_ms"] = round((time.monotonic() - start) * 1000.0, 1)
        result["error"] = str(exc) or type(exc).__name__
        return result
    result["latency_ms"] = round((time.monotonic() - start) * 1000.0, 1)

    if not isinstance(hits, list):
        result["error"] = f"search returned {type(hits).__name__}, expected list"
        return result

    good = [h for h in hits if isinstance(h, dict) and h.get("ok", True)
            and h.get("title") and h.get("url")]
    result["hit_count"] = len(good)
    if good:
        result["ok"] = True
        return result

    # No usable hits — classify the degradation.
    failure = next((h for h in hits if isinstance(h, dict) and h.get("ok") is False), None)
    if failure is not None:
        reason = str(failure.get("reason") or "unknown_failure")
        if reason in _EXPECTED_FAIL_REASONS:
            result["error"] = "login_required" if reason in ("login_required", "no_profile") else reason
            result["expected"] = True
            # Live signal beats the stamp: hitting a login wall means the saved
            # session is NOT authenticated (never was, or expired since).
            if reason in ("login_required", "no_profile"):
                result["logged_in"] = False
        else:
            result["error"] = reason
    else:
        result["error"] = "no hits parsed from canary search"
    return result


def run_health_check(sites: list[str] | None = None) -> list[dict[str, Any]]:
    """Run the canary search for each site (default: every registered adapter).

    Returns one ``{site, ok, latency_ms, logged_in, hit_count, error[, expected]}``
    dict per site, in input/registry order. Never raises.

    When the interactive login wizard is active the canary fan-out is deferred
    (it would race the headed session). Callers receive an honest
    ``needs_human_login``/``deferred`` row set — NEVER an empty all-clear.
    """
    if sites is None:
        # Only the vendors that gate CAD/datasheet/footprint downloads behind a
        # login (login_wizard_sites) — commerce/link-out sites (Ace, Amazon, Home
        # Depot, …) are not part of the "Test vendors" surface. Fall back to the
        # full registry if the wizard-site list is unavailable/empty.
        try:
            registered = login_wizard_sites() or [a.key for a in all_adapters()]
        except Exception:  # noqa: BLE001
            registered = list(CANARY_QUERIES.keys())
        # Registry order first, then any canary-only leftovers (defensive).
        sites = registered + [s for s in CANARY_QUERIES if s not in registered]

    # Active login wizard → do not thrash the same profiles with canaries.
    # Return deferred/needs_human_login rows so the status board is truthful.
    try:
        from .embed import get_manager
        if get_manager().wizard_active():
            return [
                {
                    "site": site,
                    "ok": False,
                    "latency_ms": None,
                    "logged_in": False,
                    "hit_count": 0,
                    "error": "needs_human_login",
                    "expected": True,
                    "deferred": True,
                    "reason": "login_wizard_active",
                }
                for site in (sites or [])
            ]
    except Exception:  # noqa: BLE001 — health must never raise
        pass

    def _safe_check(site: str) -> dict[str, Any]:
        try:
            return _check_site(site)
        except Exception as exc:  # noqa: BLE001 — belt and braces: never raise
            return {"site": site, "ok": False, "latency_ms": None, "logged_in": False,
                    "hit_count": 0, "error": str(exc) or type(exc).__name__}

    if not sites:
        return []
    # The native CEF host can queue more work, but six simultaneous navigations
    # measurably push ordinary supplier pages past its 10 s navigation deadline.
    # Three keeps the status board progressive while eliminating false failures.
    with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_CANARIES,
                            thread_name_prefix="vendor-health") as pool:
        futures = [pool.submit(copy_context().run, _safe_check, site) for site in sites]
        return [future.result() for future in futures]
