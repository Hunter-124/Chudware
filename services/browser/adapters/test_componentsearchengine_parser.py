"""Offline Component Search Engine card and artifact-contract fixtures."""
from pathlib import Path
from zipfile import ZipFile

import pytest

from services.browser import session
from services.browser.adapters import componentsearchengine as source


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
DETAIL = source.BASE_URL + "/part-view/ABC-1/Example"


def test_search_rows_keep_identity_and_real_card_images(monkeypatch):
    content = '''<form hidden><input id="id_username"></form><table>
      <tr id="row-1"><td><img data-src="images/abc.png" alt="ABC-1"></td>
        <td><a class="part-number" href="/part-view/ABC-1/Example">ABC-1</a>
        <span class="grey-text">Example &amp; Co</span></td></tr>
      <tr id="row-2"><td><img class="logo" src="/logo.png"></td>
        <td><a href="/part-view/XYZ-2/Other" class="part-number">XYZ-2</a>
        <span class="grey-text">Other</span></td></tr>
    </table>'''
    seen = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        seen.append((url, site)) or {"ok": True, "content": content}))
    hits = source.ADAPTER.search("A&B / #1")
    assert seen == [(source.BASE_URL + "/search?term=A%26B+%2F+%231", source.SITE)]
    assert [hit["kind"] for hit in hits] == ["model", "model"]
    assert hits[0]["mpn"] == "ABC-1"
    assert hits[0]["manufacturer"] == "Example & Co"
    assert hits[0]["url"] == DETAIL
    assert hits[0]["thumbnail"] == source.BASE_URL + "/images/abc.png"
    assert hits[1]["thumbnail"] is None
    assert hits[0]["price"] is None and hits[0]["currency"] is None
    assert hits[0]["model"] == {"available": None, "formats": []}
    assert source.ADAPTER.result_kinds == ("model",)
    assert source.ADAPTER.login_required and source.ADAPTER.cad_acquisition


def test_live_markup_lazy_image_and_model_availability(monkeypatch):
    # Trimmed from the live LM358 results page, November 2026.
    content = '''<table class="search-results-table"><tr id="row-7" data-uid="11889135">
    <td><img class="j-part-image thumb-image lazy" data-lazy-loaded="false"
    src="https://media.componentsearchengine.com/images/no-image.svg?v=0.78.0"
    data-images="[&quot;https://componentsearchengine.com/Images/9/LM358AM.jpg&quot;,null]"></td>
    <td><a href="/part-view/LM358AM/Texas%20Instruments" class="part-number">LM358AM</a>
    <span class="grey-text">Texas Instruments</span></td>
    <td><div class="small-tooltip hide">3D model available</div></td></tr>
    <tr><td><img src="https://media.componentsearchengine.com/images/no-image.svg?v=0.78.0"
    data-images="[null,null]"></td><td><a class="part-number" href="/part-view/ABC/Example">ABC</a></td></tr>
    </table>'''
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    hits = source.ADAPTER.search("LM358")
    assert hits[0]["thumbnail"] == "https://componentsearchengine.com/Images/9/LM358AM.jpg"
    assert hits[0]["manufacturer"] == "Texas Instruments"
    assert hits[0]["model"] == {"available": True, "formats": []}
    assert hits[1]["thumbnail"] is None
    assert hits[1]["model"]["available"] is None


def test_search_failure_distinctions_and_confirmed_empty(monkeypatch):
    for content, reason in (("Access Denied", "blocked"), ("verify you are human", "captcha"),
                            ('<input id="id_username">', "login_required"),
                            ("<html>unrecognized</html>", "parse_failed")):
        monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
        failure = source.ADAPTER.search("part")[0]
        assert failure["reason"] == reason and failure["site"] == source.SITE
        assert failure["op"] == "search" and failure["url"]
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "No results found"})
    assert source.ADAPTER.search("part") == []
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "timeout", "hint": "backend timeout", "status": 503, "retry_after_s": 4})
    failure = source.ADAPTER.search("part")[0]
    assert (failure["reason"], failure["hint"], failure["status"], failure["retry_after_s"]) == (
        "timeout", "backend timeout", 503, 4)


@pytest.mark.parametrize("members, kind, fmt", [
    ({"part.kicad_mod": "(footprint)"}, "ecad", "zip"),
    ({"part.kicad_mod": "(footprint)", "3d/part.step": b"<html>login</html>"}, "ecad", "zip"),
    ({"part.kicad_sym": "(kicad_symbol_lib)", "3d/part.step": STEP}, "cad", "step"),
    ({"readme.txt": "No model included"}, "archive", "zip"),
])
def test_acquire_classifies_archives_and_preserves_provenance(monkeypatch, tmp_path, members, kind, fmt):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "final_url": DETAIL, "content": '<a href="/files/part.zip?token=1">Download</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.zip", None))

    def download(url, site, dest):
        with ZipFile(dest, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return {"ok": True, "dest": str(dest), "session_profile_used": "profile-1", "fetched_ts": "2026-01-01"}

    monkeypatch.setattr(session, "browser_download", download)
    out = source.ADAPTER.acquire({"url": DETAIL, "title": "ABC-1", "mpn": "ABC-1"})
    assert out["ok"] and (out["artifact_kind"], out["format"]) == (kind, fmt)
    assert out["source_url"] == DETAIL and out["download_url"] == source.BASE_URL + "/files/part.zip?token=1"
    assert out["session_profile_used"] == "profile-1" and out["fetched_ts"] == "2026-01-01"
    assert out["license_note"] and out["mpn"] == "ABC-1"
    if kind == "cad":
        assert Path(out["artifact_path"]).suffix == ".step"
        assert Path(out["artifact_path"]).read_bytes() == STEP
        assert out["member"] == "3d/part.step" and out["archive_path"] == str(tmp_path / "part.zip")
    else:
        assert out["artifact_path"] == str(tmp_path / "part.zip")


def test_acquire_rejects_invalid_content_and_ambiguous_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.step", None))
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": '<a href="/part.step">STEP</a>'})
    def invalid_download(_url, _site, dest):
        dest.write_bytes(b"<html>login page</html>")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", invalid_download)
    assert source.ADAPTER.acquire({"url": DETAIL})["reason"] == "parse_failed"
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content":
        '<a href="/a.zip">KiCad</a><a href="/b.zip">Altium</a>'})
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("ambiguous archive downloaded"))
    failure = source.ADAPTER.acquire({"url": DETAIL})
    assert failure["reason"] == "unsupported" and len(failure["candidates"]) == 2
    assert failure["ask_user"]["kind"] == "choice" and failure["ask_user"]["suggested"] == "cancel"
    assert {option["value"] for option in failure["ask_user"]["options"]} == {"cancel", *failure["candidates"]}
    assert failure["selection_field"] == "download_url"
    assert source.ADAPTER.acquire({"url": DETAIL, "kind": "series"})["reason"] == "unsupported"


def test_acquire_propagates_download_failure_and_login_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": '<a href="/part.zip">part</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.zip", None))
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 30, "hint": "slow down"})
    failure = source.ADAPTER.acquire({"url": DETAIL})
    assert (failure["reason"], failure["status"], failure["retry_after_s"]) == ("rate_limited", 429, 30)
    assert failure["op"] == "acquire" and failure["site"] == source.SITE
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "final_url": source.BASE_URL + "/login", "content": "Sign in"})
    assert source.ADAPTER.acquire({"url": DETAIL})["reason"] == "login_required"


def test_acquire_respects_explicit_cad_choice(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content":
        '<a href="/part.zip">Library</a><a href="/part.step">STEP</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.step", None))
    downloaded = []
    def download(url, _site, dest):
        downloaded.append(url)
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    choice = source.ADAPTER.acquire({"url": DETAIL})
    assert choice["reason"] == "unsupported" and choice["selection_field"] == "download_url"
    selected = next(option["value"] for option in choice["ask_user"]["options"]
                    if option["value"] == source.BASE_URL + "/part.step")
    assert source.ADAPTER.acquire({"url": DETAIL, "format": "kicad"})["reason"] == "unsupported"
    out = source.ADAPTER.acquire({"url": DETAIL, "format": "step"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "step"
    assert downloaded == [source.BASE_URL + "/part.step"]
    assert Path(out["artifact_path"]).read_bytes() == STEP
    out = source.ADAPTER.acquire({"url": DETAIL, choice["selection_field"]: selected})
    assert out["ok"] and out["format"] == "step"
    assert source.ADAPTER.acquire({"url": DETAIL, "format": "zip",
                                   "download_url": source.BASE_URL + "/part.step"})["reason"] == "unsupported"
    assert source.ADAPTER.acquire({"url": DETAIL, "format": "step", "member": "other.step"})["reason"] == "unsupported"


def test_requested_3d_format_rejects_ecad_only_archive(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": '<a href="/part.zip">Library</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.zip", None))
    def download(_url, _site, dest):
        with ZipFile(dest, "w") as archive:
            archive.writestr("part.kicad_mod", "(footprint)")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    assert source.ADAPTER.acquire({"url": DETAIL, "format": "step"})["reason"] == "unsupported"


def test_multiple_archived_models_require_explicit_member(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": '<a href="/part.zip">Library</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.zip", None))
    def download(_url, _site, dest):
        with ZipFile(dest, "w") as archive:
            archive.writestr("3d/first.step", STEP)
            archive.writestr("3d/second.step", STEP)
            archive.writestr("3d/third.stl", b"solid part\nfacet normal 0 0 1\nouter loop\n"
                             b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
                             b"endloop\nendfacet\nendsolid part\n")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    out = source.ADAPTER.acquire({"url": DETAIL})
    assert out["reason"] == "unsupported"
    assert set(out["candidates"]) == {"3d/first.step", "3d/second.step", "3d/third.stl"}
    assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"
    assert {option["value"] for option in out["ask_user"]["options"]} == {"cancel", *out["candidates"]}
    assert out["selection_field"] == "member"
    selected = next(option["value"] for option in out["ask_user"]["options"]
                    if option["value"] == "3d/second.step")
    filtered = source.ADAPTER.acquire({"url": DETAIL, "format": "step"})
    assert set(filtered["candidates"]) == {"3d/first.step", "3d/second.step"}
    assert filtered["ask_user"]["kind"] == "choice" and filtered["ask_user"]["suggested"] == "cancel"
    assert filtered["selection_field"] == "member"
    out = source.ADAPTER.acquire({"url": DETAIL, out["selection_field"]: selected})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["member"] == "3d/second.step"
    assert Path(out["artifact_path"]).read_bytes() == STEP
    out = source.ADAPTER.acquire({"url": DETAIL, "format": "stl"})
    assert out["ok"] and out["format"] == "stl" and out["member"] == "3d/third.stl"
