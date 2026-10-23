"""Per-source browser integration tests.

These exercise the REAL adapter code paths (search/acquire → browser_fetch under a
saved login) against the REAL sites. They are OPT-IN only:

    CHUDWARE_BROWSER_INTEGRATION_TESTS=1  +  a real logged-in profile per source

CI never sets the flag and never has profiles, so the live cases skip with a clear
"provision required" message — never a real login, never a committed credential.
See README.md for how a developer opts in.

Anti-all-skip posture (per the offline-ml-track-discipline lesson): this module is
NOT structurally incapable of running. Two cases ALWAYS run and prove the adapter
code is real and reachable — the registry is wired, and an adapter's real search()
executes end-to-end offline (browser_fetch monkeypatched to a captured page). Only
the live-network cases are gated.
"""
from __future__ import annotations

import os

import pytest

from services.browser import adapters

#: Every source with real adapter code. Kept in sync with the registry.
EXPECTED_SITES = ("amazon", "grabcad", "jlcpcb", "mcmaster", "pcbway", "traceparts",
                  "misumi", "lcsc", "digikey", "snapeda", "3dcontentcentral",
                  # retail wave (Parts Library V2)
                  "homedepot", "lowes", "acehardware", "grainger", "fastenal",
                  "harborfreight", "tractorsupply", "onlinemetals", "metalsdepot")

_ENABLED = os.environ.get("CHUDWARE_BROWSER_INTEGRATION_TESTS") == "1"
_gated = pytest.mark.skipif(
    not _ENABLED,
    reason="opt-in only: set CHUDWARE_BROWSER_INTEGRATION_TESTS=1 and provide a logged-in "
           "profile per source — see services/browser/integration/README.md",
)


# --- ALWAYS-RUN: prove the suite exercises real code (not vaporware, not all-skip) ---

def test_expected_adapters_are_registered():
    """The live cases below dispatch through this registry — assert every expected
    source actually resolves to a callable adapter (so a skip is a provisioning gap,
    never a missing-code gap)."""
    registered = {a.key for a in adapters.all_adapters()}
    assert set(EXPECTED_SITES) <= registered, f"missing adapters: {set(EXPECTED_SITES) - registered}"
    for key in EXPECTED_SITES:
        a = adapters.get_adapter(key)
        assert callable(getattr(a, "search", None)), f"{key}.search not callable"
        assert callable(getattr(a, "acquire", None)), f"{key}.acquire not callable"


def test_adapter_search_executes_real_code_offline(monkeypatch):
    """Run a REAL adapter's search() end-to-end with browser_fetch faked to a captured
    login-wall page — proves the adapter's own fetch→parse→login-wall branch executes
    (the code path the live test hits), with zero network."""
    from services.browser import session
    from services.browser.adapters import grabcad

    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site, **kw: {"ok": True, "status": 200,
                                 "content": "<html><body>Log in to GrabCAD</body></html>"},
    )
    out = grabcad.GrabCADAdapter().search("bracket")
    assert isinstance(out, list) and out, "search returned no result envelope"
    assert isinstance(out[0], dict)
    # The real login-wall branch fired → a login_required ask (contracts/interaction.md).
    assert out[0].get("reason") == "login_required"
    assert str(out[0].get("ask_user", {}).get("id", "")).startswith("browser_login")


# --- GATED: real network, real login, opt-in only -------------------------------

@_gated
@pytest.mark.parametrize("key", EXPECTED_SITES)
def test_live_search_exercises_real_site(key):
    from services.browser import profiles

    if not profiles.has_profile(key):
        pytest.skip(f"no saved profile for '{key}' — run browser_login('{key}') first (README)")

    adapter = adapters.get_adapter(key)
    out = adapter.search("bracket")
    # Any of these is a genuine, real-code outcome: parsed results, a login-required
    # ask (session expired), or a captcha/2FA abort surfaced from session.browser_fetch.
    # We never assert a live site returns products — only that real code ran and
    # produced the contract's list-of-dict envelope.
    assert isinstance(out, list)
    assert all(isinstance(x, dict) for x in out)
