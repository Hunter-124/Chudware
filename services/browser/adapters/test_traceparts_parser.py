"""Offline TraceParts result, configuration and acquisition fixtures."""
from pathlib import Path
import zipfile

import pytest

from services.browser import adapters, session
from services.browser.adapters import traceparts


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"
IGES = b" " * 72 + b"S0000001\n"
MODEL = ("https://www.traceparts.com/en/product/maker-bearing?"
         "CatalogPath=MAKE%3ACAT&Product=P1&PartNumber=608%20ZZ")
SEARCH = '''
<a href="/en/product/maker-bearing?CatalogPath=MAKE%3ACAT&amp;Product=P1&amp;PartNumber=608%20ZZ">
  <img src="/thumbnails/608.jpg" alt="Bearing preview"></a>
<a href="/en/product/maker-bearing?CatalogPath=MAKE%3ACAT&amp;Product=P1&amp;PartNumber=608%20ZZ">
  Bearing 608 ZZ</a>
<a href="/en/product/maker-bearing?CatalogPath=MAKE%3ACAT&amp;Product=P2">Bearing family</a>
'''


def _page(monkeypatch, content, **fields):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content, **fields})


def _root(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path / "downloads")


def test_search_merges_thumbnail_anchor_with_title_and_preserves_configuration(monkeypatch):
    seen = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        seen.append(url) or {"ok": True, "content": SEARCH}))
    hits = traceparts.ADAPTER.search("bearing A&B #1/2")
    assert "A%26B+%231%2F2" in seen[0]
    assert len(hits) == 2 and [hit["kind"] for hit in hits] == ["product", "series"]
    assert hits[0]["title"] == "Bearing 608 ZZ" and hits[0]["sku"] == "608 ZZ"
    assert hits[0]["thumbnail"] == "https://www.traceparts.com/thumbnails/608.jpg"
    assert hits[0]["url"] == MODEL and hits[1]["thumbnail"] is None
    assert hits[0]["price"] is None and hits[0]["model"]["available"] is None
    assert traceparts.ADAPTER.result_kinds == ("product", "series")


def test_search_thumbnail_only_uses_slug_and_not_logos(monkeypatch):
    _page(monkeypatch, '<a href="/en/product/supplier-pump"><img src="/logo.svg"></a>')
    hit = traceparts.ADAPTER.search("pump")[0]
    assert hit["title"] == "supplier pump" and hit["kind"] == "series"
    assert hit["thumbnail"] is None


@pytest.mark.parametrize("content, reason", [
    ("<html>unrecognized results layout</html>", "parse_failed"),
    ('<form class="tp-login">Sign in to TraceParts</form>', "login_required"),
    ('<div class="g-recaptcha"></div>', "captcha"),
])
def test_search_reports_page_failures(monkeypatch, content, reason):
    _page(monkeypatch, content)
    out = traceparts.ADAPTER.search("part")[0]
    assert out["ok"] is False and out["reason"] == reason
    assert out["site"] == "traceparts" and out["op"] == "search"


def test_search_explicit_no_results(monkeypatch):
    _page(monkeypatch, "No results")
    assert traceparts.ADAPTER.search("unknown") == []


@pytest.mark.parametrize("op", ["search", "acquire"])
def test_transport_failures_are_preserved(monkeypatch, op):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": False, "reason": "rate_limited", "status": 429,
        "retry_after_s": 29, "hint": "later", "detail": "quota"})
    out = traceparts.ADAPTER.search("bearing")[0] if op == "search" else traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == "rate_limited" and out["op"] == op
    assert out["site"] == "traceparts" and out["status"] == 429
    assert out["retry_after_s"] == 29 and out["detail"] == "quota"


def test_terms_prompt_takes_precedence_and_is_not_auto_accepted(monkeypatch):
    _page(monkeypatch, '<div class="supplier-terms">Accept the terms</div>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("terms not accepted"))
    out = traceparts.ADAPTER.acquire({"url": "https://www.traceparts.com/en/product/family"})
    assert out["reason"] == "terms_required"
    assert out["ask_user"]["kind"] == "approval" and not out["ask_user"]["suggested"]


@pytest.mark.parametrize("result, final_url", [
    ({"url": "https://www.traceparts.com/en/product/family?Product=P1"}, None),
    ({"url": MODEL, "sku": "OTHER"}, None),
    ({"url": MODEL}, "https://www.traceparts.com/en/product/family?Product=P1"),
    ({"url": MODEL}, MODEL.replace("608%20ZZ", "OTHER")),
])
def test_acquire_refuses_unselected_changed_or_mismatched_configuration(monkeypatch, result, final_url):
    _page(monkeypatch, '<a href="/downloads/file.step">File</a>', final_url=final_url)
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("wrong part download"))
    out = traceparts.ADAPTER.acquire(result)
    assert out["reason"] == "unsupported" and "PartNumber" in out["hint"]


def test_acquire_selects_requested_format_and_ignores_other_part_links(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/downloads/other.igs?PartNumber=OTHER">Other</a>'
                       '<a href="/downloads/bearing.step">STEP</a>'
                       '<a href="/downloads/bearing.igs?PartNumber=608%20ZZ">IGES</a>')
    seen = []

    def download(url, site, dest):
        seen.append((url, site))
        dest.write_bytes(IGES)
        return {"ok": True, "dest": str(dest), "session_profile_used": "user-traceparts"}

    monkeypatch.setattr(session, "browser_download", download)
    out = traceparts.ADAPTER.acquire({"url": MODEL, "sku": "608 ZZ", "format": "igs", "title": "Bearing"})
    assert seen == [("https://www.traceparts.com/downloads/bearing.igs?PartNumber=608%20ZZ", "traceparts")]
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "iges"
    assert out["sku"] == "608 ZZ" and out["source_url"] == MODEL
    assert out["session_profile_used"] == "user-traceparts" and out["license_note"] and out["fetched_ts"]
    assert Path(out["artifact_path"]).read_bytes() == IGES


def test_acquire_validates_requested_member_from_archive(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/downloads/bearing.zip">CAD files</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("part.step", STEP)
            archive.writestr("part.igs", IGES)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = traceparts.ADAPTER.acquire({"url": MODEL, "requested_format": "iges"})
    assert out["ok"] and out["format"] == "iges" and out["artifact_kind"] == "cad"
    assert out["member"] == "part.igs" and Path(out["artifact_path"]).read_bytes() == IGES
    assert Path(out["archive_path"]).suffix == ".zip"


def test_acquire_refuses_multiple_matching_files_and_unavailable_format(monkeypatch):
    _page(monkeypatch, '<a href="/a.step">a.step</a><a href="/b.stp">b.stp</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("ambiguous file"))
    ambiguous = traceparts.ADAPTER.acquire({"url": MODEL, "format": "step"})
    missing = traceparts.ADAPTER.acquire({"url": MODEL, "format": "iges"})
    assert ambiguous["reason"] == "unsupported" and "filename" in ambiguous["hint"]
    assert ambiguous["candidates"] == ["a.step", "b.stp"]
    assert ambiguous["selection_field"] == "filename"
    assert ambiguous["ask_user"]["kind"] == "choice" and ambiguous["ask_user"]["suggested"] == "cancel"
    assert [option["value"] for option in ambiguous["ask_user"]["options"]] == [
        "cancel", "a.step", "b.stp"]
    assert missing["reason"] == "unsupported"


def test_acquire_rejects_html_and_preserves_existing_file(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    root = tmp_path / "downloads" / "traceparts"
    root.mkdir(parents=True)
    old = root / "bearing.step"
    old.write_bytes(STEP)
    _page(monkeypatch, '<a href="/downloads/bearing.step">CAD</a>')

    def download(url, site, dest):
        dest.write_bytes(b"<html>login required</html>")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == "parse_failed" and out["ok"] is False
    assert old.read_bytes() == STEP and sorted(root.iterdir()) == [old]


def test_acquire_preserves_download_failure(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/downloads/bearing.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": "timeout", "status": 503, "retry_after_s": 13, "hint": "slow"})
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == "timeout" and out["site"] == "traceparts" and out["op"] == "acquire"
    assert out["status"] == 503 and out["retry_after_s"] == 13


@pytest.mark.parametrize("stem", [
    r"..\..\outside", "C:outside", "C:%5coutside", "%2e%2e%2foutside",
    "%2foutside", "%5c%5cserver%5cshare%5coutside", "part:stream", "CON", "nul", "COM%C2%B9",
])
def test_acquire_refuses_unsafe_filename(monkeypatch, tmp_path, stem):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, f'<a href="https://catalog.fixture/files/{stem}.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("unsafe download"))
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == "policy_refused" and out["original_reason"] == "unsafe_filename"


def test_query_fragment_cannot_control_filename(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    href = (r"https://catalog.fixture/files/%E8%BD%B4%20bracket.step"
            r"?signature=a%2Fb&next=/..\..\evil.zip#C:\escape")
    _page(monkeypatch, f'<a href="{href}">CAD</a>')

    def download(url, site, dest):
        assert dest.parent == tmp_path / "downloads" / "traceparts"
        assert dest.name.startswith("轴 bracket-") and dest.suffix == ".step"
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["ok"] and Path(out["artifact_path"]).read_bytes() == STEP


def test_acquire_refuses_existing_symlink_escape(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    root = tmp_path / "downloads" / "traceparts"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.step"
    outside.write_bytes(b"preserve")
    (root / "bearing.step").symlink_to(outside)
    _page(monkeypatch, '<a href="/downloads/bearing.step">CAD</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("escaped download"))
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == "policy_refused" and out["original_reason"] == "unsafe_filename"
    assert outside.read_bytes() == b"preserve"


def test_acquire_selects_exact_named_file(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/a.step">a.step</a><a href="/b.stp">b.stp</a>')

    def download(url, site, dest):
        assert url == "https://www.traceparts.com/b.stp"
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = traceparts.ADAPTER.acquire({"url": MODEL, "format": "step", "filename": "b.stp"})
    assert out["ok"] and out["format"] == "step"


def test_acquire_selects_format_from_explicit_download_label(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/download/42">Download STEP</a>'
                       '<a href="/download/43">Download IGES</a>')

    def download(url, site, dest):
        assert url == "https://www.traceparts.com/download/43"
        assert dest.suffix == ".iges"
        dest.write_bytes(IGES)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = traceparts.ADAPTER.acquire({"url": MODEL, "format": "iges"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "iges"


@pytest.mark.parametrize("names, reason", [
    (["one.step", "two.step"], "unsupported"),
    (["../escape.step"], "parse_failed"),
])
def test_archive_refuses_ambiguous_or_unsafe_members(monkeypatch, tmp_path, names, reason):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/downloads/model.zip">Archive</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            for name in names:
                archive.writestr(name, STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = traceparts.ADAPTER.acquire({"url": MODEL, "format": "step"})
    assert out["reason"] == reason and not out["ok"]
    if reason == "unsupported":
        assert set(out["candidates"]) == {"one.step", "two.step"}
        assert out["selection_field"] == "archive_member"
        assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"
        assert {option["value"] for option in out["ask_user"]["options"]} == {
            "cancel", "one.step", "two.step"}
    else:
        assert out["ask_user"] is None
    assert not list((tmp_path / "downloads" / "traceparts").iterdir())


def test_archive_member_selection_is_explicit(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/downloads/model.zip">Archive</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("one.step", STEP)
            archive.writestr("two.step", STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    ambiguous = traceparts.ADAPTER.acquire({"url": MODEL})
    assert ambiguous["selection_field"] == "archive_member"
    for option in ambiguous["ask_user"]["options"][1:]:
        name = option["value"]
        out = traceparts.ADAPTER.acquire({"url": MODEL, ambiguous["selection_field"]: name})
        assert out["ok"] and out["member"] == name and out["format"] == "step"


def test_archive_missing_requested_format_is_unsupported(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/downloads/model.zip">Archive</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("part.step", STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    assert traceparts.ADAPTER.acquire({"url": MODEL, "format": "iges"})["reason"] == "unsupported"


def test_mixed_formats_prompt_and_selected_file_round_trips(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/first.step">first.step</a><a href="/second.igs">second.igs</a>')
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["candidates"] == ["first.step", "second.igs"]
    assert out["selection_field"] == "filename"
    assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"

    def download(url, site, dest):
        assert url in ("https://www.traceparts.com/first.step", "https://www.traceparts.com/second.igs")
        dest.write_bytes(IGES if url.endswith(".igs") else STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    for option in out["ask_user"]["options"][1:]:
        name = option["value"]
        chosen = traceparts.ADAPTER.acquire({"url": MODEL, out["selection_field"]: name})
        assert chosen["ok"] and chosen["format"] == ("iges" if name.endswith(".igs") else "step")


def test_duplicate_filenames_offer_distinct_paths(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/one/a.step">a.step</a><a href="/two/a.step">a.step</a>')
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["candidates"] == ["/one/a.step", "/two/a.step"]

    def download(url, site, dest):
        assert url == "https://www.traceparts.com/two/a.step"
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    assert traceparts.ADAPTER.acquire({"url": MODEL, "filename": out["candidates"][1]})["ok"]


def test_direct_file_and_archive_prompt_and_archive_member_selects_zip(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _page(monkeypatch, '<a href="/a.step">a.step</a><a href="/all.zip">all.zip</a>')
    out = traceparts.ADAPTER.acquire({"url": MODEL})
    assert out["candidates"] == ["a.step", "all.zip"]
    assert out["ask_user"]["kind"] == "choice"

    def download(url, site, dest):
        assert url == "https://www.traceparts.com/all.zip"
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("one.step", STEP)
            archive.writestr("two.step", STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    selected = traceparts.ADAPTER.acquire({"url": MODEL, "archive_member": "two.step"})
    assert selected["ok"] and selected["member"] == "two.step"


def test_unconfigured_family_offers_only_explicit_part_variants(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    family = "https://www.traceparts.com/en/product/bearing-family?Product=P1"
    second = MODEL.replace("608%20ZZ", "609%20ZZ")

    def fetch(url, site):
        if url == family:
            return {"ok": True, "content": (
                f'<a href="{MODEL}">608</a><a href="{second}">609</a>'
                f'<a href="{MODEL}">Duplicate</a>'
                '<a href="https://other.example/en/product/x?PartNumber=bad">Other site</a>')}
        assert url in (MODEL, second)
        number = "608" if url == MODEL else "609"
        return {"ok": True, "content": f'<a href="/{number}.step">{number}.step</a>'}

    monkeypatch.setattr(session, "browser_fetch", fetch)
    out = traceparts.ADAPTER.acquire({"url": family})
    assert out["candidates"] == [MODEL, second]
    assert out["selection_field"] == "url"
    assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"

    def download(url, site, dest):
        assert url in ("https://www.traceparts.com/608.step", "https://www.traceparts.com/609.step")
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    for option in out["ask_user"]["options"][1:]:
        chosen_url = option["value"]
        selected = traceparts.ADAPTER.acquire({out["selection_field"]: chosen_url})
        assert selected["ok"] and selected["sku"] == ("608 ZZ" if chosen_url == MODEL else "609 ZZ")
