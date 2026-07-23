"""Deterministic parser test for the GrabCAD search() adapter.

The fixture below is a VERBATIM slice of a real GrabCAD library-results page
fetched live 2026-07-07 from https://grabcad.com/library?query=gear. It is a
byte-for-byte contiguous span through a real card boundary: the first card's
`modelLink` anchor + wrapped `previewImage` img, its real `</a>` close, and the
start of the *second* card's `modelLink` anchor + img. The tab/newline
whitespace GrabCAD emits (AngularJS-rendered) is preserved exactly. Only the
tail of the second card is truncated (mid-card) — everything present is
verbatim. This locks the parser to the real live DOM, not a speculative fixture.

Like the rest of the adapter suite, no Playwright / no network: we monkeypatch
`services.browser.session.browser_fetch` (which the adapter calls positionally
as `browser_fetch(url, site)`).
"""
from __future__ import annotations

import importlib

import pytest


# --- VERBATIM contiguous real slice from the 2026-07-07 grabcad.html capture ------
GRABCAD_REAL_CARDS = (
    '<a href="/library/gear-fidget-assembly-2" class="modelLink">\n'
    '\t\t\t\t\t\t\t<img class="previewImage" '
    'ng-src="https://grabcad.com/screenshots/pics/5d30fa0808ec8d8a6c668fc53e070956/card.jpg" '
    'err-src="https://grabcad.com/screenshots/pics/missing_small.png" '
    'alt="Gear Fidget assembly" '
    'src="https://grabcad.com/screenshots/pics/5d30fa0808ec8d8a6c668fc53e070956/card.jpg" '
    'style="display: block;">\n'
    '\t\t\t\t\t\t</a>\n'
    '\t\t\t\t\t\t<div class="actions">\n'
    '\t\t\t\t\t\t\t<a href="/library/gear-fidget-assembly-2" class="modelCreated">\n'
    '\t\t\t\t\t\t\t\t<span class="ng-binding">July 7th, 2026</span>\n'
    '\t\t\t\t\t\t\t</a>\n'
    '\t\t\t\t\t\t</div>\n'
    '\t\t\t\t\t\t<div class="modelName text-bold">'
    '<a href="/library/gear-fidget-assembly-2">'
    '<span class="name ng-binding">Gear Fidget assembly</span></a></div>\n'
    '\t\t\t\t\t\t<a href="/library/gear-wheel-30" class="modelLink">\n'
    '\t\t\t\t\t\t\t<img class="previewImage" '
    'ng-src="https://grabcad.com/screenshots/pics/4e345b393baa7f201655845a8fb33145/card.jpg" '
    'err-src="https://grabcad.com/screenshots/pics/missing_small.png" '
    'alt="Gear wheel" '
    'src="https://grabcad.com/screenshots/pics/4e345b393baa7f201655845a8fb33145/card.jpg" '
    'style="display: block;">\n'
    '\t\t\t\t\t\t</a>\n'
)


@pytest.fixture()
def grabcad_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import grabcad
    importlib.reload(grabcad)
    yield grabcad, session
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_search_parses_real_modellink_cards(grabcad_mod, monkeypatch):
    grabcad, session = grabcad_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": True, "content": GRABCAD_REAL_CARDS})

    hits = grabcad.ADAPTER.search("gear")

    assert len(hits) == 2
    assert hits[0]["title"] == "Gear Fidget assembly"
    assert hits[0]["url"] == "https://grabcad.com/library/gear-fidget-assembly-2"
    assert hits[0]["site"] == "grabcad"
    assert hits[1]["title"] == "Gear wheel"
    assert hits[1]["url"] == "https://grabcad.com/library/gear-wheel-30"


def test_search_dedupes_and_skips_upload_link(grabcad_mod, monkeypatch):
    # Duplicate href + the /library/new upload utility link must be dropped.
    # /library/new given a modelLink class on purpose so it passes the regex's
    # class gate and must be dropped by the explicit href == "/library/new" guard
    # (not merely by lacking the class).
    html = (
        GRABCAD_REAL_CARDS
        + '<a href="/library/new" class="modelLink">'
        '<img class="previewImage" alt="Upload your model"></a>'
        + '<a href="/library/gear-wheel-30" class="modelLink">'
        '<img class="previewImage" alt="Gear wheel"></a>'
    )
    grabcad, session = grabcad_mod
    monkeypatch.setattr(
        session, "browser_fetch", lambda url, site: {"ok": True, "content": html})

    hits = grabcad.ADAPTER.search("gear")

    urls = [h["url"] for h in hits]
    assert len(hits) == 2  # dupe collapsed, /library/new excluded
    assert "https://grabcad.com/library/new" not in urls
    assert urls.count("https://grabcad.com/library/gear-wheel-30") == 1


def test_search_propagates_fetch_failure(grabcad_mod, monkeypatch):
    grabcad, session = grabcad_mod
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = grabcad.ADAPTER.search("gear")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]
