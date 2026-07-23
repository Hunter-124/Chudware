"""Real-HTML bot-wall regression tests for the hard-walled retail adapters
(homedepot, lowes, tractorsupply) — Parts Library V2 parser reverse-engineering,
2026-07-07.

An anonymous headless fetch of these sites' search URLs does NOT reach a
product-results page; each is fronted by an edge/bot-management layer that
serves a block shell with HTTP 200. The honest, sanctioned behavior (same as
grainger) is to degrade to the ``login_required`` ask — never solve or bypass
the wall, and never silently parse the block shell to an empty result list
("no hits parsed").

The fixtures below are VERBATIM captures of the real block pages returned on
2026-07-07 (lowes/tractorsupply are Akamai edgesuite "Access Denied" pages,
reproduced in full; homedepot is the 200-OK bot-management "Oops!! Something
went wrong" error shell, reproduced as the representative slice that carries
its detection markers). Offline style matches test_retail_adapters.py: no real
Playwright, no network — ``session.browser_fetch`` is monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest

# --- verbatim real block-page captures (2026-07-07) --------------------------

# https://www.lowes.com/search?searchTerm=drill+bit  → Akamai edgesuite, full body.
LOWES_ACCESS_DENIED_HTML = (
    "<html><head>\n"
    "<title>Access Denied</title>\n"
    "</head><body>\n"
    "<h1>Access Denied</h1>\n"
    " \n"
    "You don't have permission to access \"http://www.lowes.com/search?\" on this server.<p>\n"
    "Reference #18.c45ec817.1783444650.6aa0dcdf\n"
    "</p><p>https://errors.edgesuite.net/18.c45ec817.1783444650.6aa0dcdf</p>\n"
    "\n\n</body></html>"
)

# https://www.tractorsupply.com/tsc/search/bolt  → Akamai edgesuite, full body.
TRACTORSUPPLY_ACCESS_DENIED_HTML = (
    "<html><head>\n"
    "<title>Access Denied</title>\n"
    "</head><body>\n"
    "<h1>Access Denied</h1>\n"
    " \n"
    "You don't have permission to access \"http://www.tractorsupply.com/tsc/search/bolt\" on this server.<p>\n"
    "Reference #18.9789cc17.1783444657.4076a6e0\n"
    "</p><p>https://errors.edgesuite.net/18.9789cc17.1783444657.4076a6e0</p>\n"
    "\n\n</body></html>"
)

# https://www.homedepot.com/s/drill%20bit  → 200-OK bot-management error shell
# (title "Error Page"); this is the representative slice carrying the markers.
HOMEDEPOT_BOT_SHELL_HTML = (
    "<!DOCTYPE html><html><head>\n"
    '    <meta charset="UTF-8">\n'
    "    <title>Error Page</title>\n"
    "</head>\n<body>\n"
    '    <div class="hdr">#1 Home Improvement Retailer</div>\n'
    '    <main class="err">\n'
    '        <div class="msg">Oops!! Something went wrong. Please refresh page</div>\n'
    "    </main>\n"
    "    <script>\n"
    "        document.cookie='_bman_adv=2656fcTEcT634190902sEST8aaDvd3PR; path=/';\n"
    "    </script>\n"
    "</body></html>"
)


@pytest.fixture()
def walled_mods(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    # Reloading the package resets _REGISTRY — re-register EVERY site module (not
    # just the ones under test) so the registry stays whole for tests that run
    # after this fixture, mirroring test_retail_adapters.py's adapter_mods.
    from services.browser.adapters import (_retail, acehardware, amazon, content3d,
                                           digikey_web, fastenal, grabcad, grainger,
                                           harborfreight, homedepot, jlcpcb, lcsc, lowes,
                                           mcmaster, metalsdepot, misumi, onlinemetals,
                                           pcbway, snapeda, tractorsupply, traceparts)
    for mod in (amazon, grabcad, jlcpcb, mcmaster, pcbway, traceparts, misumi, lcsc,
                digikey_web, snapeda, content3d, _retail, homedepot, lowes, acehardware,
                grainger, fastenal, harborfreight, tractorsupply, onlinemetals, metalsdepot):
        importlib.reload(mod)
    mods = {"homedepot": homedepot, "lowes": lowes, "tractorsupply": tractorsupply}
    yield session, mods
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


_WALL_CASES = {
    "lowes": LOWES_ACCESS_DENIED_HTML,
    "tractorsupply": TRACTORSUPPLY_ACCESS_DENIED_HTML,
    "homedepot": HOMEDEPOT_BOT_SHELL_HTML,
}


@pytest.mark.parametrize("site", sorted(_WALL_CASES))
def test_real_bot_wall_degrades_to_login_required(walled_mods, monkeypatch, site):
    """The real block page must degrade to the login_required ask, never parse
    to a silent empty list and never be bypassed."""
    session, mods = walled_mods
    html = _WALL_CASES[site]
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": html, "session_profile_used": s})

    hits = mods[site].ADAPTER.search("anything")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"
    assert hits[0]["ask_user"]["id"].startswith("browser_login")


@pytest.mark.parametrize("site", sorted(_WALL_CASES))
def test_real_bot_wall_never_reaches_parse(walled_mods, monkeypatch, site):
    """Guard against a regression where a wall marker is dropped and the block
    shell silently parses to zero hits (the original "no hits parsed" bug)."""
    session, mods = walled_mods
    html = _WALL_CASES[site]
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": html, "session_profile_used": s})

    hits = mods[site].ADAPTER.search("anything")
    # A silent empty list (or a fabricated product hit) would both fail here.
    assert hits and hits[0].get("reason") == "login_required"
