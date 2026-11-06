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

import json
import zipfile
from pathlib import Path

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
    from services import paths
    from services.browser import session
    from services.browser.adapters import freecadlibrary

    monkeypatch.setattr(paths, "BROWSER_DOWNLOADS", tmp_path / "downloads")
    return session, freecadlibrary


def test_fcl_search_filters_tree_by_filename(fcl_mod, monkeypatch):
    session, fcl = fcl_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": FCL_TREE_HTML})

    hits = fcl.ADAPTER.search("screw")

    # Two .step screws match; the .FCStd sink and the tree dir are excluded.
    assert len(hits) == 2
    assert all(h["kind"] == "model" and h["site"] == "freecadlibrary" for h in hits)
    assert all(h["model"] == {"available": True, "formats": ["step"]} for h in hits)
    assert all(h["thumbnail"] is None and h["price"] is None for h in hits)

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


def test_fcl_search_previews_are_associated_by_exact_basename(fcl_mod, monkeypatch):
    session, fcl = fcl_mod
    # Paths observed in the live recursive GitHub tree; only the first has a preview.
    folder = "Architectural Parts/Living room/"
    paths = [folder + "FCBL_table_parametric.FCStd", folder + "FCBL_table_parametric.png",
             folder + "FCBL_table_other.FCStd"]
    payload = {"tree": [{"type": "blob", "path": path} for path in paths], "truncated": False}
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *_: {"ok": True, "content": json.dumps(payload)})
    hits = fcl.ADAPTER.search("table")
    assert len(hits) == 2
    assert hits[0]["thumbnail"] == (
        "https://raw.githubusercontent.com/FreeCAD/FreeCAD-library/master/"
        "Architectural%20Parts/Living%20room/FCBL_table_parametric.png")
    assert hits[0]["model"]["formats"] == ["fcstd"]
    assert hits[1]["thumbnail"] is None


def test_fcl_recovers_from_truncated_tree(fcl_mod, monkeypatch):
    session, fcl = fcl_mod
    first_sha, nested_sha = "a" * 40, "b" * 40
    responses = {
        fcl.SEARCH_URL_TMPL: {"tree": [], "truncated": True},
        fcl._TREE_URL + "master": {
            "tree": [{"type": "tree", "path": "Mechanical Parts", "sha": first_sha}],
            "truncated": False},
        fcl._TREE_URL + first_sha + "?recursive=1": {"tree": [], "truncated": True},
        fcl._TREE_URL + first_sha: {
            "tree": [{"type": "tree", "path": "Screws", "sha": nested_sha}], "truncated": False},
        fcl._TREE_URL + nested_sha + "?recursive=1": {
            "tree": [{"type": "blob", "path": "part.step"},
                     {"type": "blob", "path": "part.png"}], "truncated": False},
    }
    calls = []

    def fetch(url, site):
        calls.append(url)
        return {"ok": True, "content": json.dumps(responses[url])}

    monkeypatch.setattr(session, "browser_fetch", fetch)
    hits = fcl.ADAPTER.search("part")
    assert len(hits) == 1 and hits[0]["path"] == "Mechanical Parts/Screws/part.step"
    assert hits[0]["thumbnail"].endswith("/Mechanical%20Parts/Screws/part.png")
    assert calls == list(responses)


def test_fcl_failed_subtree_never_looks_like_empty_search(fcl_mod, monkeypatch):
    session, fcl = fcl_mod
    failure = {"ok": False, "site": fcl.SITE, "op": "fetch", "reason": "rate_limited"}

    def fetch(url, site):
        if url == fcl.SEARCH_URL_TMPL:
            return {"ok": True, "content": '{"tree":[],"truncated":true}'}
        return failure

    monkeypatch.setattr(session, "browser_fetch", fetch)
    assert fcl.ADAPTER.search("anything") == [failure]
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "not JSON"})
    assert fcl.ADAPTER.search("anything")[0]["reason"] == "parse_failed"


def test_fcl_validates_and_preserves_existing_cad_on_bad_download(fcl_mod, monkeypatch):
    session, fcl = fcl_mod
    url = fcl._blob_url("Electrical Parts/Servos/example.step")
    content = [b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"]

    def download(raw_url, site, dest):
        dest.write_bytes(content[0])
        return {"ok": True, "dest": str(dest), "session_profile_used": site}

    monkeypatch.setattr(session, "browser_download", download)
    acquired = fcl.ADAPTER.acquire({"url": url})
    assert acquired["ok"] and acquired["artifact_kind"] == "cad" and acquired["format"] == "step"
    saved = Path(acquired["artifact_path"])
    assert saved.read_bytes() == content[0]
    content[0] = b"<html>not a model</html>"
    assert fcl.ADAPTER.acquire({"url": url})["reason"] == "parse_failed"
    assert saved.read_bytes().startswith(b"ISO-10303-21;")
    assert fcl.ADAPTER.acquire({"url": url, "format": "iges"})["reason"] == "unsupported"
    assert fcl.ADAPTER.acquire({"raw_url": "https://evil.example/part.step"})["reason"] == "unsupported"


def test_fcl_validates_native_freecad_files(fcl_mod, monkeypatch):
    session, fcl = fcl_mod

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("Document.xml", "<Document><Objects/></Document>")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    acquired = fcl.ADAPTER.acquire({"url": fcl._blob_url("sample.FCStd")})
    assert acquired["ok"] and acquired["artifact_kind"] == "cad" and acquired["format"] == "fcstd"
    assert zipfile.is_zipfile(acquired["artifact_path"])
