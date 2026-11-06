"""Offline fixtures for rendered SnapEDA rows and downloaded library artifacts."""
from pathlib import Path
from zipfile import ZipFile

import pytest

from services.browser import session
from services.browser.adapters import snapeda as source


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
DETAIL = source.BASE_URL + "/parts/ABC-1/Example/view-part/"


def test_rendered_rows_keep_model_metadata_and_product_image(monkeypatch):
    content = '''<form hidden><input id="id_username"></form><table>
      <tr class="search-result-row"><td><img class="manufacturer_mobile_img" src="/brand.png" alt="Example">
        <img class="part-image" src="https://images.example/ABC-1.jpg" alt="ABC-1"></td>
        <td><a href="/parts/ABC-1/Example/view-part/?ref=search" data-manufacturer="Example">ABC-1</a></td>
        <td class="part-result price-result"><p title="Average price from distributors">$3.25</p></td>
        <td><img class="data-icon" src="/static/img/search_icons/3d_model_orange.png" alt="3D model available"></td></tr>
      <tr class="search-result-row"><td><img class="part-image" src="/placeholder.png"></td>
        <td><a href="/parts/XYZ-2/Other/view-part/" data-manufacturer="Other">XYZ-2</a></td>
        <td><img class="data-icon" src="/static/img/search_icons/3d_outline.png" alt="3D not available"></td></tr>
    </table>'''
    seen = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        seen.append(url) or {"ok": True, "content": content}))
    hits = source.ADAPTER.search("A&B / #1")
    assert seen == [source.BASE_URL + "/search/?q=A%26B+%2F+%231"]
    assert [hit["kind"] for hit in hits] == ["model", "model"]
    assert hits[0]["title"] == hits[0]["mpn"] == "ABC-1"
    assert hits[0]["url"] == DETAIL + "?ref=search"
    assert hits[0]["manufacturer"] == "Example"
    assert hits[0]["thumbnail"] == "https://images.example/ABC-1.jpg"
    assert hits[1]["thumbnail"] is None
    assert hits[0]["model"] == {"available": True, "formats": []}
    assert hits[1]["model"] == {"available": False, "formats": []}
    assert hits[0]["average_distributor_price_usd"] == 3.25
    assert hits[0]["price"] is None and hits[0]["currency"] is None
    assert source.ADAPTER.result_kinds == ("model",) and source.ADAPTER.api is False


def test_search_failure_distinctions_and_login_redirect(monkeypatch):
    for content, reason in (("Access Denied", "blocked"), ("verify you are human", "captcha"),
                            ('<input id="id_username">', "login_required"),
                            ("<html>unexpected search shell</html>", "parse_failed")):
        monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
        failure = source.ADAPTER.search("part")[0]
        assert failure["reason"] == reason and failure["site"] == source.SITE
        assert failure["op"] == "search" and failure["url"]
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "No results found"})
    assert source.ADAPTER.search("part") == []
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "timeout", "hint": "fetch timed out", "status": 503, "retry_after_s": 5})
    out = source.ADAPTER.search("part")[0]
    assert (out["reason"], out["hint"], out["status"], out["retry_after_s"]) == (
        "timeout", "fetch timed out", 503, 5)
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "final_url": source.BASE_URL + "/login", "content": "Sign in"})
    assert source.ADAPTER.search("part")[0]["reason"] == "login_required"


@pytest.mark.parametrize("members, kind, fmt", [
    ({"part.kicad_sym": "(kicad_symbol_lib)"}, "ecad", "zip"),
    ({"part.kicad_sym": "(kicad_symbol_lib)", "3d/part.step": b"<html>login</html>"}, "ecad", "zip"),
    ({"part.kicad_mod": "(footprint)", "3d/part.stp": STEP}, "cad", "step"),
    ({"readme.txt": "No model included"}, "archive", "zip"),
])
def test_acquire_distinguishes_ecad_and_validated_geometry(monkeypatch, tmp_path, members, kind, fmt):
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
        assert Path(out["artifact_path"]).suffix == ".stp"
        assert Path(out["artifact_path"]).read_bytes() == STEP
        assert out["member"] == "3d/part.stp" and out["archive_path"] == str(tmp_path / "part.zip")
    else:
        assert out["artifact_path"] == str(tmp_path / "part.zip")


def test_acquire_rejects_unsafe_zip_and_unspecified_export(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": '<a href="/part.zip">part</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.zip", None))
    def unsafe_download(_url, _site, dest):
        with ZipFile(dest, "w") as archive:
            archive.writestr("../outside.step", STEP)
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", unsafe_download)
    assert source.ADAPTER.acquire({"url": DETAIL})["reason"] == "parse_failed"
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content":
        '<a href="/a.zip">KiCad</a><a href="/b.zip">Altium</a>'})
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("ambiguous download"))
    out = source.ADAPTER.acquire({"url": DETAIL})
    assert out["reason"] == "unsupported" and len(out["candidates"]) == 2
    assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"
    assert {option["value"] for option in out["ask_user"]["options"]} == {"cancel", *out["candidates"]}
    assert out["selection_field"] == "download_url"
    assert source.ADAPTER.acquire({"url": DETAIL, "kind": "category"})["reason"] == "unsupported"


def test_acquire_propagates_download_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": '<a href="/part.zip">part</a>'})
    monkeypatch.setattr(source, "download_path", lambda *_args, **_kw: (tmp_path / "part.zip", None))
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 30, "hint": "slow down"})
    out = source.ADAPTER.acquire({"url": DETAIL})
    assert (out["reason"], out["status"], out["retry_after_s"]) == ("rate_limited", 429, 30)
    assert out["op"] == "acquire" and out["site"] == source.SITE


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
