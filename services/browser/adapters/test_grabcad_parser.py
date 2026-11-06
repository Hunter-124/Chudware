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
import zipfile
from pathlib import Path

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
    assert hits[0]["kind"] == "model"
    assert hits[0]["thumbnail"] == (
        "https://grabcad.com/screenshots/pics/5d30fa0808ec8d8a6c668fc53e070956/card.jpg")
    assert hits[0]["model"]["available"] is None and hits[0]["price"] is None
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
    assert hits[0]["reason"] == "no_profile" and hits[0]["hint"] == "x"
    assert hits[0]["site"] == "grabcad" and hits[0]["op"] == "search"


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"
IGES = b" " * 72 + b"S0000001\n"
MODEL = "https://grabcad.com/library/widget-1"


def _search_page(monkeypatch, session, html):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": html})


def _downloads_at(monkeypatch, tmp_path):
    from services.browser import adapters
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", tmp_path / "downloads")


def test_search_encodes_query_and_rejects_placeholder(grabcad_mod, monkeypatch):
    grabcad, session = grabcad_mod
    seen = []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: (
        seen.append(url) or {"ok": True, "content": (
            '<a class="modelLink" href="/library/part-1"><img alt="Part" '
            'src="/screenshots/pics/missing_small.png" class="previewImage"></a>')}))
    hit = grabcad.ADAPTER.search("gear A&B #1/2")[0]
    assert "A%26B+%231%2F2" in seen[0]
    assert hit["kind"] == "model" and hit["thumbnail"] is None


@pytest.mark.parametrize("page, reason", [
    ("<html>unrecognized layout</html>", "parse_failed"),
    ('<form class="login-form">Log in to GrabCAD</form>', "login_required"),
    ('<div class="g-recaptcha"></div>', "captcha"),
])
def test_search_reports_page_failures(grabcad_mod, monkeypatch, page, reason):
    grabcad, session = grabcad_mod
    _search_page(monkeypatch, session, page)
    failure = grabcad.ADAPTER.search("part")[0]
    assert failure["ok"] is False and failure["reason"] == reason
    assert failure["site"] == "grabcad" and failure["op"] == "search"


def test_search_explicit_no_results(grabcad_mod, monkeypatch):
    grabcad, session = grabcad_mod
    _search_page(monkeypatch, session, "No results")
    assert grabcad.ADAPTER.search("unknown") == []


def test_acquire_selects_requested_format_with_session_and_provenance(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, (
        '<a href="/downloads/widget.step">widget.step</a>'
        '<a href="/downloads/widget.igs?token=one&amp;other=two">widget.igs</a>'))
    seen = []

    def download(url, site, dest):
        seen.append((url, site))
        dest.write_bytes(IGES)
        return {"ok": True, "dest": str(dest), "session_profile_used": "grabcad-user"}

    monkeypatch.setattr(session, "browser_download", download)
    out = grabcad.ADAPTER.acquire({"url": MODEL, "title": "Widget", "format": "IGS"})
    assert seen == [("https://grabcad.com/downloads/widget.igs?token=one&other=two", "grabcad")]
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "iges"
    assert Path(out["artifact_path"]).read_bytes() == IGES
    assert out["source_url"] == MODEL and out["session_profile_used"] == "grabcad-user"
    assert out["license_note"] and out["fetched_ts"]


def test_acquire_download_endpoint_with_filename_label(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/files/123/download">widget.STEP</a>')

    def download(url, site, dest):
        assert url.endswith("/files/123/download") and dest.suffix == ".step"
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = grabcad.ADAPTER.acquire({"url": MODEL, "requested_format": "step"})
    assert out["ok"] and out["format"] == "step" and out["artifact_kind"] == "cad"


def test_acquire_extracts_only_the_requested_format_from_archive(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/downloads/widget.zip">Archive</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("model.step", STEP)
            archive.writestr("model.igs", IGES)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = grabcad.ADAPTER.acquire({"url": MODEL, "format": "iges"})
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "iges"
    assert out["member"] == "model.igs" and Path(out["archive_path"]).suffix == ".zip"
    assert Path(out["artifact_path"]).read_bytes() == IGES


def test_acquire_refuses_ambiguous_files_and_unavailable_format(grabcad_mod, monkeypatch):
    grabcad, session = grabcad_mod
    _search_page(monkeypatch, session, (
        '<a href="/a.step">a.step</a><a href="/b.stp">b.stp</a>'))
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("unexpected download"))
    ambiguous = grabcad.ADAPTER.acquire({"url": MODEL, "format": "step"})
    missing = grabcad.ADAPTER.acquire({"url": MODEL, "format": "iges"})
    assert ambiguous["reason"] == "unsupported" and "filename" in ambiguous["hint"]
    assert ambiguous["candidates"] == ["a.step", "b.stp"]
    assert ambiguous["selection_field"] == "filename"
    assert ambiguous["ask_user"]["kind"] == "choice" and ambiguous["ask_user"]["suggested"] == "cancel"
    assert [option["value"] for option in ambiguous["ask_user"]["options"]] == [
        "cancel", "a.step", "b.stp"]
    assert missing["reason"] == "unsupported"


def test_acquire_rejects_html_and_preserves_prior_file(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    root = tmp_path / "downloads" / "grabcad"
    root.mkdir(parents=True)
    old = root / "widget.step"
    old.write_bytes(STEP)
    _search_page(monkeypatch, session, '<a href="/downloads/widget.step">Widget</a>')

    def download(url, site, dest):
        dest.write_bytes(b"<html>sign in</html>")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = grabcad.ADAPTER.acquire({"url": MODEL, "format": "step"})
    assert out["ok"] is False and out["reason"] == "parse_failed"
    assert old.read_bytes() == STEP and sorted(root.iterdir()) == [old]


@pytest.mark.parametrize("reason", ["timeout", "rate_limited", "blocked"])
def test_acquire_preserves_download_failures(grabcad_mod, monkeypatch, tmp_path, reason):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/downloads/widget.step">Widget</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: {
        "ok": False, "reason": reason, "status": 429, "retry_after_s": 23, "hint": "failed"})
    out = grabcad.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == reason and out["op"] == "acquire"
    assert out["status"] == 429 and out["retry_after_s"] == 23


@pytest.mark.parametrize("stem", [
    r"..\..\outside", "C:outside", "C:%5coutside", "%2e%2e%2foutside",
    "%2foutside", "%5c%5cserver%5cshare%5coutside", "part:stream", "CON", "nul", "COM%C2%B9",
])
def test_acquire_refuses_unsafe_filename(grabcad_mod, monkeypatch, tmp_path, stem):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, f'<a href="https://catalog.fixture/files/{stem}.step">file</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("unsafe download"))
    out = grabcad.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == "policy_refused" and out["original_reason"] == "unsafe_filename"


def test_acquire_query_fragment_cannot_control_filename(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    href = (r"https://catalog.fixture/files/%E8%BD%B4%20bracket.step"
            r"?signature=a%2Fb&next=/..\..\evil.zip#C:\escape")
    _search_page(monkeypatch, session, f'<a href="{href}">file</a>')

    def download(url, site, dest):
        assert dest.parent == tmp_path / "downloads" / "grabcad"
        assert dest.name.startswith("轴 bracket-") and dest.suffix == ".step"
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = grabcad.ADAPTER.acquire({"url": MODEL})
    assert out["ok"] and Path(out["artifact_path"]).read_bytes() == STEP


def test_acquire_refuses_existing_symlink_escape(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    root = tmp_path / "downloads" / "grabcad"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.step"
    outside.write_bytes(b"preserve")
    (root / "widget.step").symlink_to(outside)
    _search_page(monkeypatch, session, '<a href="/downloads/widget.step">file</a>')
    monkeypatch.setattr(session, "browser_download", lambda *_: pytest.fail("escaped download"))
    out = grabcad.ADAPTER.acquire({"url": MODEL})
    assert out["reason"] == "policy_refused" and out["original_reason"] == "unsafe_filename"
    assert outside.read_bytes() == b"preserve"


def test_acquire_selects_exact_named_file(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/a.step">a.step</a><a href="/b.stp">b.stp</a>')

    def download(url, site, dest):
        assert url == "https://grabcad.com/b.stp"
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = grabcad.ADAPTER.acquire({"url": MODEL, "format": "step", "filename": "b.stp"})
    assert out["ok"] and out["format"] == "step"


@pytest.mark.parametrize("names, reason", [
    (["one.step", "two.step"], "unsupported"),
    (["../escape.step"], "parse_failed"),
])
def test_archive_refuses_ambiguous_or_unsafe_members(grabcad_mod, monkeypatch, tmp_path, names, reason):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/downloads/model.zip">Archive</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            for name in names:
                archive.writestr(name, STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    out = grabcad.ADAPTER.acquire({"url": MODEL, "format": "step"})
    assert out["reason"] == reason and not out["ok"]
    if reason == "unsupported":
        assert set(out["candidates"]) == {"one.step", "two.step"}
        assert out["selection_field"] == "archive_member"
        assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"
        assert {option["value"] for option in out["ask_user"]["options"]} == {
            "cancel", "one.step", "two.step"}
    else:
        assert out["ask_user"] is None
    assert not list((tmp_path / "downloads" / "grabcad").iterdir())


def test_archive_member_selection_is_explicit(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/downloads/model.zip">Archive</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("one.step", STEP)
            archive.writestr("two.step", STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    ambiguous = grabcad.ADAPTER.acquire({"url": MODEL})
    assert ambiguous["selection_field"] == "archive_member"
    for option in ambiguous["ask_user"]["options"][1:]:
        name = option["value"]
        out = grabcad.ADAPTER.acquire({"url": MODEL, ambiguous["selection_field"]: name})
        assert out["ok"] and out["member"] == name and out["format"] == "step"


def test_search_accepts_apostrophes_in_model_title(grabcad_mod, monkeypatch):
    grabcad, session = grabcad_mod
    _search_page(monkeypatch, session, (
        '<a class="modelLink" href="/library/test"><img class="previewImage" '
        'alt="Designer\'s bracket" src="/images/bracket.jpg"></a>'))
    hit = grabcad.ADAPTER.search("bracket")[0]
    assert hit["title"] == "Designer's bracket"
    assert hit["thumbnail"] == "https://grabcad.com/images/bracket.jpg"


def test_archive_missing_requested_format_is_unsupported(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/downloads/model.zip">Archive</a>')

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("part.step", STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    assert grabcad.ADAPTER.acquire({"url": MODEL, "format": "iges"})["reason"] == "unsupported"


def test_mixed_formats_prompt_and_selected_file_round_trips(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/first.step">first.step</a>'
                                      '<a href="/second.igs">second.igs</a>')
    out = grabcad.ADAPTER.acquire({"url": MODEL})
    assert out["candidates"] == ["first.step", "second.igs"]
    assert out["selection_field"] == "filename"
    assert out["ask_user"]["kind"] == "choice" and out["ask_user"]["suggested"] == "cancel"

    def download(url, site, dest):
        assert url in ("https://grabcad.com/first.step", "https://grabcad.com/second.igs")
        dest.write_bytes(IGES if url.endswith(".igs") else STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    for option in out["ask_user"]["options"][1:]:
        name = option["value"]
        chosen = grabcad.ADAPTER.acquire({"url": MODEL, out["selection_field"]: name})
        assert chosen["ok"] and chosen["format"] == ("iges" if name.endswith(".igs") else "step")


def test_duplicate_filenames_offer_distinct_paths(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/one/a.step">a.step</a>'
                                      '<a href="/two/a.step">a.step</a>')
    out = grabcad.ADAPTER.acquire({"url": MODEL})
    assert out["candidates"] == ["/one/a.step", "/two/a.step"]

    def download(url, site, dest):
        assert url == "https://grabcad.com/two/a.step"
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    assert grabcad.ADAPTER.acquire({"url": MODEL, "filename": out["candidates"][1]})["ok"]


def test_direct_file_and_archive_prompt_and_archive_member_selects_zip(grabcad_mod, monkeypatch, tmp_path):
    grabcad, session = grabcad_mod
    _downloads_at(monkeypatch, tmp_path)
    _search_page(monkeypatch, session, '<a href="/a.step">a.step</a><a href="/all.zip">all.zip</a>')
    out = grabcad.ADAPTER.acquire({"url": MODEL})
    assert out["candidates"] == ["a.step", "all.zip"]
    assert out["ask_user"]["kind"] == "choice"

    def download(url, site, dest):
        assert url == "https://grabcad.com/all.zip"
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("one.step", STEP)
            archive.writestr("two.step", STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    selected = grabcad.ADAPTER.acquire({"url": MODEL, "archive_member": "two.step"})
    assert selected["ok"] and selected["member"] == "two.step"
