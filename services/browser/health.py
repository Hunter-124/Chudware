"""services/browser/health.py — vendor adapter health checks (Parts Library V2).

One cheap canary search per registered source adapter, timed and classified.
``run_health_check`` NEVER raises — every failure mode collapses into a
per-site result dict so a caller (the ``vendors_health_check`` RPC on the
engine_oracle side) can render a status board without try/except gymnastics.

Classification:
- ok=True  — the canary search returned ≥1 hit carrying both title and url.
- ok=False, error="login_required", expected=True — the adapter degraded to a
  login/challenge ask or there is no saved profile yet. This is the EXPECTED
  state on a fresh machine, not a code failure.
- ok=False, error=<str> — a real failure (exception, empty parse, fetch error).
"""
from __future__ import annotations

import time
from typing import Any

from . import profiles
from .adapters import all_adapters, get_adapter, login_wizard_sites

#: One cheap, representative query per registered site — kept in sync with the
#: adapter registry (test_retail_adapters.py asserts full coverage).
CANARY_QUERIES: dict[str, str] = {
    # original wave (T-X2/T-X7)
    "mcmaster": "m6 socket head cap screw",
    "grabcad": "nema 17 mount",
    "traceparts": "608 bearing",
    "amazon": "608zz bearing",
    "jlcpcb": "0603 resistor 10k",
    "pcbway": "2 layer pcb prototype",
    "misumi": "linear shaft 8mm",
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
}

#: Failure reasons that mean "provision a login", not "the adapter is broken".
_EXPECTED_FAIL_REASONS = ("login_required", "no_profile", "terms_confirmation_required",
                          "playwright_not_installed", "captcha_or_2fa_detected")


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
    """
    try:
        from .embed import get_manager
        if get_manager().wizard_active():
            return []
    except Exception:  # noqa: BLE001 — health must never raise
        pass
    if sites is None:
        # Only the vendors that gate CAD/datasheet/footprint downloads behind a
        # login (login_wizard_sites) — commerce/link-out sites (Ace, Amazon, Home
        # Depot, …) are not part of the "Test vendors" surface. Fall back to the
        # full registry if the wizard-site list is unavailable/empty.
        registered = login_wizard_sites() or [a.key for a in all_adapters()]
        # Registry order first, then any canary-only leftovers (defensive).
        sites = registered + [s for s in CANARY_QUERIES if s not in registered]
    out = []
    for site in sites:
        try:
            out.append(_check_site(site))
        except Exception as exc:  # noqa: BLE001 — belt and braces: never raise
            out.append({"site": site, "ok": False, "latency_ms": None, "logged_in": False,
                        "hit_count": 0, "error": str(exc) or type(exc).__name__})
    return out
