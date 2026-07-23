"""Fixture-based parser test for the FreeCAD-library search adapter.

FCL_TREE_HTML holds a VERBATIM slice of the GitHub REST git-tree API response
(wrapped in the ``<pre>`` container CEF's headless fetch renders around JSON) for
``/repos/FreeCAD/FreeCAD-library/git/trees/master?recursive=1`` fetched live
2026-07-10 (HTTP 200, ~11k entries, ``truncated:false``). The old GitHub HTML
code-search was JS-rendered and unreliable headless; this adapter reads the
deterministic tree API instead and filters blob paths client-side by query tokens
+ a CAD extension. The fixture holds two real ``servo-screw*.step`` blobs, one
non-matching ``.FCStd`` blob (must be filtered out for query "screw") and a
``type:tree`` dir entry (must be skipped).

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


FCL_TREE_HTML = r"""<html><head></head><body><pre>{
 "sha": "deadbeef",
 "tree": [
  {
   "path": "Electrical Parts/Servos/Emax-ES08A/servo-screw-1.7x6.2mm.step",
   "mode": "100755",
   "type": "blob",
   "sha": "a4099ea9194e893ea99c832c5d37e232f122ecf2",
   "size": 55248,
   "url": "https://api.github.com/repos/FreeCAD/FreeCAD-library/git/blobs/a4099ea9194e893ea99c832c5d37e232f122ecf2"
  },
  {
   "path": "Electrical Parts/Servos/Emax-ES08A/servo-screw-2x7_5mm.step",
   "mode": "100755",
   "type": "blob",
   "sha": "c0171c821c568935ccd35bfa1490b858387a5185",
   "size": 55082,
   "url": "https://api.github.com/repos/FreeCAD/FreeCAD-library/git/blobs/c0171c821c568935ccd35bfa1490b858387a5185"
  },
  {
   "path": "Architectural Parts/Bathroom/Bathroom_cabinet_sink.FCStd",
   "mode": "100644",
   "type": "blob",
   "sha": "5f61aa45ae42fa4d4464d660f67cb2c71582c6bb",
   "size": 1043469,
   "url": "https://api.github.com/repos/FreeCAD/FreeCAD-library/git/blobs/5f61aa45ae42fa4d4464d660f67cb2c71582c6bb"
  },
  {
   "path": "Architectural Parts",
   "mode": "040000",
   "type": "tree",
   "sha": "793c47a468fa548f3445cc706692976b469c0842",
   "url": "https://api.github.com/repos/FreeCAD/FreeCAD-library/git/trees/793c47a468fa548f3445cc706692976b469c0842"
  }
 ],
 "truncated": false
}</pre><div class="json-formatter-container"></div></body></html>"""


@pytest.fixture()
def fcl_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import freecadlibrary
    importlib.reload(freecadlibrary)
    yield session, freecadlibrary
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_fcl_search_filters_tree_by_filename(fcl_mod, monkeypatch):
    session, fcl = fcl_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": FCL_TREE_HTML})

    hits = fcl.ADAPTER.search("screw")

    # Two .step screws match; the .FCStd sink and the tree dir are excluded.
    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "freecadlibrary" for h in hits)

    first = hits[0]
    assert first["title"] == "servo-screw-1.7x6.2mm.step"
    assert first["path"] == (
        "Electrical Parts/Servos/Emax-ES08A/servo-screw-1.7x6.2mm.step")
    assert first["url"] == (
        "https://github.com/FreeCAD/FreeCAD-library/blob/master/"
        "Electrical%20Parts/Servos/Emax-ES08A/servo-screw-1.7x6.2mm.step")
    assert first["raw_url"] == (
        "https://raw.githubusercontent.com/FreeCAD/FreeCAD-library/master/"
        "Electrical%20Parts/Servos/Emax-ES08A/servo-screw-1.7x6.2mm.step")

    assert hits[1]["title"] == "servo-screw-2x7_5mm.step"


def test_fcl_search_url_is_git_tree_api():
    from services.browser.adapters import freecadlibrary
    assert freecadlibrary.SEARCH_URL_TMPL == (
        "https://api.github.com/repos/FreeCAD/FreeCAD-library/"
        "git/trees/master?recursive=1")


def test_fcl_search_no_match_is_honest(fcl_mod, monkeypatch):
    session, fcl = fcl_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": FCL_TREE_HTML})
    assert fcl.ADAPTER.search("nonexistentwidget") == []
