"""Offline Ultra Librarian card, authentication and artifact-contract fixtures."""
from pathlib import Path
from zipfile import ZipFile

import pytest

from services.browser import session
from services.browser.adapters import ultralibrarian as source


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
DETAIL = source.BASE_URL + "/details/123/Example/ABC-1"


def test_search_nested_cards_keep_only_part_preview(monkeypatch):
    content = '''<form hidden><input id="id_username"></form><div class="result-card"><div class="info">
        <img src="/images/symbols/3dyes.svg" alt="3D model available">
        <img class="manufacturer-logo" src="/logo.png">
        <img class="part-image" data-src="/part-images/abc.png" alt="ABC-1">
        <a href="/details/123/Example/ABC-1">ABC-1</a>
        <span class="manufacturer">Example &amp; Co</span></div></div>
      <div class="result-card"><div><img src="/images/symbols/3dno.svg" alt="3D not available">
        <img src="/placeholder.png"><a href="/details/456/Other/XYZ-2">XYZ-2</a>
        <span class="manufacturer">Other</span></div></div>'''
    seen = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        seen.append(url) or {"ok": True, "content": content}))
    hits = source.ADAPTER.search("A&B / #1")
    assert seen == [source.BASE_URL + "/search?queryText=A%26B+%2F+%231"]
    assert [hit["kind"] for hit in hits] == ["model", "model"]
    assert hits[0]["title"] == hits[0]["mpn"] == "ABC-1"
    assert hits[0]["url"] == DETAIL and hits[0]["manufacturer"] == "Example & Co"
    assert hits[0]["thumbnail"] == source.BASE_URL + "/part-images/abc.png"
    assert hits[1]["thumbnail"] is None
    assert hits[0]["model"] == {"available": True, "formats": []}
    assert hits[1]["model"] == {"available": False, "formats": []}
    assert hits[0]["price"] is None and hits[0]["currency"] is None
    assert source.ADAPTER.result_kinds == ("model",) and source.ADAPTER.login_required


def test_search_handles_real_sso_redirect_and_other_failures(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "final_url": "https://sso.ultralibrarian.com/Account/Login?ReturnUrl=abc", "content": "Sign in"})
    out = source.ADAPTER.search("part")[0]
    assert out["reason"] == "login_required" and out["site"] == source.SITE and out["ask_user"]
    for content, reason in (("Access Denied", "blocked"), ("verify you are human", "captcha"),
                            ('<input id="id_username">', "login_required"),
                            ("<html>unrecognized</html>", "parse_failed")):
        monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
        failure = source.ADAPTER.search("part")[0]
        assert failure["reason"] == reason and failure["op"] == "search" and failure["url"]
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "No results found"})
    assert source.ADAPTER.search("part") == []
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "timeout", "hint": "fetch timeout", "status": 503, "retry_after_s": 5})
    failure = source.ADAPTER.search("part")[0]
    assert (failure["reason"], failure["hint"], failure["status"], failure["retry_after_s"]) == (
        "timeout", "fetch timeout", 503, 5)


@pytest.mark.parametrize("members, kind, fmt", [
    ({"part.kicad_sym": "(kicad_symbol_lib)"}, "ecad", "zip"),
    ({"part.bxl": "library", "3d/part.step": STEP}, "cad", "step"),
    ({"part.bxl": "library", "3d/part.step": b"<html>login</html>"}, "ecad", "zip"),
    ({"readme.txt": "No model included"}, "archive", "zip"),
])
def test_acquire_classifies_library_and_model_archives(monkeypatch, tmp_path, members, kind, fmt):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "final_url": DETAIL,
        "content": '<span class="manufacturer">Example</span><a href="/files/part.zip?token=1">Download</a>'})
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
    assert out["manufacturer"] == "Example" and out["license_note"]
    if kind == "cad":
        assert Path(out["artifact_path"]).suffix == ".step"
        assert Path(out["artifact_path"]).read_bytes() == STEP
        assert out["member"] == "3d/part.step" and out["archive_path"] == str(tmp_path / "part.zip")
    else:
        assert out["artifact_path"] == str(tmp_path / "part.zip")


def test_acquire_rejects_invalid_content_and_ambiguous_exports(monkeypatch, tmp_path):
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
    out = source.ADAPTER.acquire({"url": DETAIL})
    assert out["reason"] == "unsupported" and len(out["candidates"]) == 2
    assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"
    assert {option["value"] for option in out["ask_user"]["options"]} == {"cancel", *out["candidates"]}
    assert out["selection_field"] == "download_url"
    assert source.ADAPTER.acquire({"url": DETAIL, "kind": "series"})["reason"] == "unsupported"


def test_acquire_preserves_download_failure_and_sso_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": '<a href="/part.zip">part</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.zip", None))
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 30, "hint": "slow down"})
    out = source.ADAPTER.acquire({"url": DETAIL})
    assert (out["reason"], out["status"], out["retry_after_s"]) == ("rate_limited", 429, 30)
    assert out["op"] == "acquire" and out["site"] == source.SITE
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "final_url": "https://sso.ultralibrarian.com/Account/Login", "content": "Sign in"})
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
