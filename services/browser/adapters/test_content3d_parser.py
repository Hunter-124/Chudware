"""Offline model-card and direct-download fixtures for 3D ContentCentral."""
from pathlib import Path
import zipfile

import pytest

from services.browser import adapters, session
from services.browser.adapters import content3d


SEARCH = """
<div class="model-card">
  <a href="/parts/supplier/608"><img data-src="/images/608.png" alt="Bearing"></a>
  <a href="/parts/supplier/608" class="model-title">608 bearing <span class="supplier">SKF</span></a>
</div>
<div class="model-card">
  <a class="model-title" href="/parts/supplier/623">623 bearing
    <img srcset="/images/623-small.png 1x, /images/623-big.png 2x" alt="Bearing"></a>
</div>
"""
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
IGES = b" " * 72 + b"S      1\n"
DETAIL = '''<span class="supplier">SKF</span>
<a href="/download/608.step?token=1">STEP</a>
<a href="/download/608.igs?token=1">IGES</a>'''


@pytest.fixture
def root(tmp_path, monkeypatch):
    from services import paths
    monkeypatch.setattr(paths, "BROWSER_DOWNLOADS", tmp_path / "downloads")
    return tmp_path


def test_search_preserves_card_images_model_kind_and_encoded_query(monkeypatch):
    seen = []
    def fetch(url, site):
        seen.append((url, site))
        return {"ok": True, "content": SEARCH}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    rows = content3d.ADAPTER.search("M3 & #1/2")
    assert seen == [(content3d.SEARCH_URL_TMPL.format(query="M3+%26+%231%2F2"), content3d.SITE)]
    assert [row["kind"] for row in rows] == ["model", "model"]
    assert rows[0]["manufacturer"] == "SKF" and rows[0]["price"] is None
    assert [row["thumbnail"] for row in rows] == [
        "https://www.3dcontentcentral.com/images/608.png",
        "https://www.3dcontentcentral.com/images/623-small.png"]
    assert adapters.get_adapter("3dcontentcentral") is content3d.ADAPTER
    assert content3d.ADAPTER.hosts == ("3dcontentcentral.com",)


def test_rendered_search_cards(monkeypatch):
    # Trimmed from the rendered "hex bolt" results; descriptions omitted.
    html = """
<div class="model-item"><div class="model-item-image">
    <a href="/download-model.aspx?catalogid=3607&amp;id=413375">
        <div class="model-item-image-border">
            <div class="model-item-image-preview" style="background-image: url('/showmodels/SIERRAPACIFIC/357/35798.jpg');"></div>
        </div>
    </a>
</div><div class="model-item-contents"><div class="model-item-contents-main"><div class="model-item-contents-pair model-item-contents-pair-bold">
    <div class="model-item-contents-pair-title" id="dvtitle_Name">Name</div>
    <div class="model-item-contents-pair-value" id="dvValue_Name"><a href="/download-model.aspx?catalogid=3607&amp;id=413375" class="model-item-contents-link">357</a></div>
</div>
<div class="model-item-contents-pair">
    <div class="model-item-contents-pair-title" id="dvtitle_Supplier">Supplier</div>
    <div class="model-item-contents-pair-value" id="dvValue_Supplier"><a href="/parts/supplier/Sierra-Pacific-Engineering-and-Products.aspx" class="model-item-contents-link">Sierra Pacific Engineering and Products</a></div>
</div></div></div></div>
<div class="model-item"><div class="model-item-image">
    <a href="/secure/Download-Partner-Model.aspx?partner=TraceParts&amp;name=Hex+Bolt">
        <div class="model-item-image-border">
            <div class="model-item-image-preview" style="background-image: url('/showmodels/TraceParts/Parts/TE_CO_CID_7376/90-26072023-063757/Pictures/90-26072023-063757M.gif');"></div>
        </div>
    </a>
</div><div class="model-item-contents"><div class="model-item-contents-main"><div class="model-item-contents-pair model-item-contents-pair-bold">
    <div class="model-item-contents-pair-title" id="dvtitle_Name">Name</div>
    <div class="model-item-contents-pair-value" id="dvValue_Name"><a href="/secure/Download-Partner-Model.aspx?partner=TraceParts&amp;name=Hex+Bolt" class="model-item-contents-link">Hex Bolt</a></div>
</div>
<div class="model-item-contents-pair">
    <div class="model-item-contents-pair-title" id="dvtitle_Supplier">Supplier</div>
    <div class="model-item-contents-pair-value" id="dvValue_Supplier"><a href="/parts/supplier/TE-CO.aspx" class="model-item-contents-link">TE-CO</a></div>
</div></div></div></div>
"""
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": html})
    rows = content3d.ADAPTER.search("hex bolt")
    assert [row["title"] for row in rows] == ["357", "Hex Bolt"]
    assert [row["manufacturer"] for row in rows] == ["Sierra Pacific Engineering and Products", "TE-CO"]
    assert rows[0]["url"] == content3d.BASE_URL + "/download-model.aspx?catalogid=3607&id=413375"
    assert rows[1]["url"].startswith(content3d.BASE_URL + "/secure/Download-Partner-Model.aspx?")
    assert rows[0]["thumbnail"] == content3d.BASE_URL + "/showmodels/SIERRAPACIFIC/357/35798.jpg"
    assert all(row["kind"] == "model" for row in rows)


@pytest.mark.parametrize("content,reason", [
    ("<html>unexpected markup</html>", "parse_failed"),
    ("<html>Access Denied</html>", "blocked"),
    ("<html>Please complete the security check</html>", "captcha"),
    ("<html>You must be logged in to download</html>", "login_required"),
])
def test_search_reports_real_failure(monkeypatch, content, reason):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    notice = content3d.ADAPTER.search("bearing")[0]
    assert notice["ok"] is False and notice["site"] == content3d.SITE
    assert notice["op"] == "search" and notice["reason"] == reason
    if reason == "login_required":
        assert notice["ask_user"]


def test_confirmed_empty_search_and_transport_failure(monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": "No results found"})
    assert content3d.ADAPTER.search("nothing") == []
    failure = {"ok": False, "site": content3d.SITE, "op": "fetch", "reason": "timeout"}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: failure)
    assert content3d.ADAPTER.search("bearing") == [failure]
    assert content3d.ADAPTER.acquire({"url": content3d.BASE_URL + "/parts/supplier/608"}) == failure


def test_acquire_selects_requested_format_and_validates(root, monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": DETAIL})
    seen = []
    def download(url, site, dest):
        seen.append(url)
        dest.write_bytes(IGES if ".igs?" in url else STEP)
        return {"ok": True, "dest": str(dest), "session_profile_used": site}
    monkeypatch.setattr(session, "browser_download", download)
    result = content3d.ADAPTER.acquire({"url": content3d.BASE_URL + "/parts/supplier/608", "format": "igs"})
    assert seen == [content3d.BASE_URL + "/download/608.igs?token=1"]
    assert result["ok"] and result["artifact_kind"] == "cad" and result["format"] == "iges"
    assert result["supplier"] == "SKF" and Path(result["artifact_path"]).read_bytes() == IGES
    assert content3d.ADAPTER.acquire({"url": content3d.BASE_URL + "/parts/supplier/608", "format": "stl"})["reason"] == "unsupported"


def test_different_direct_files_require_user_choice(root, monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/download/a.step">A</a><a href="/download/b.step">B</a>'})
    seen = []

    def download(url, site, dest):
        seen.append(url)
        dest.write_bytes(STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = {"url": content3d.BASE_URL + "/parts/supplier/608"}
    refusal = content3d.ADAPTER.acquire(result)
    assert refusal["reason"] == "unsupported" and refusal["candidates"] == ["a.step", "b.step"]
    assert refusal["ask_user"]["kind"] == "choice" and refusal["ask_user"]["suggested"] == "cancel"
    assert refusal["selection_field"] == "filename"
    assert not seen
    selected = content3d.ADAPTER.acquire({**result, "filename": "b.step"})
    assert selected["ok"] and seen == [content3d.BASE_URL + "/download/b.step"]


def test_direct_then_archive_choice_preserves_selection(root, monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/download/a.step">A</a><a href="/download/models.zip">ZIP</a>'})

    def download(url, site, dest):
        assert url == content3d.BASE_URL + "/download/models.zip"
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("one.step", STEP)
            archive.writestr("two.step", STEP)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = {"url": content3d.BASE_URL + "/parts/supplier/608"}
    direct_choice = content3d.ADAPTER.acquire(result)
    assert direct_choice["selection_field"] == "filename"
    result[direct_choice["selection_field"]] = "models.zip"
    archive_choice = content3d.ADAPTER.acquire(result)
    assert archive_choice["selection_field"] == "archive_member"
    result[archive_choice["selection_field"]] = "two.step"
    selected = content3d.ADAPTER.acquire(result)
    assert selected["ok"] and selected["format"] == "step"
    assert Path(selected["artifact_path"]).read_bytes() == STEP


def test_acquire_unpacks_validated_archive_and_rejects_html(root, monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/download/model.zip">Download</a>'})
    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("model/part.step", STEP)
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    model = content3d.ADAPTER.acquire({"url": content3d.BASE_URL + "/parts/supplier/608"})
    assert model["ok"] and model["artifact_kind"] == "cad" and model["format"] == "step"
    assert Path(model["artifact_path"]).suffix == ".step"
    assert Path(model["artifact_path"]).read_bytes() == STEP
    assert content3d.ADAPTER.acquire(
        {"url": content3d.BASE_URL + "/parts/supplier/608", "format": "stl"}
    )["reason"] == "unsupported"
    def html_download(url, site, dest):
        dest.write_bytes(b"<html>not a zip</html>")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", html_download)
    assert content3d.ADAPTER.acquire({"url": content3d.BASE_URL + "/parts/supplier/608"})["reason"] == "parse_failed"


@pytest.mark.parametrize("second, payload, expected", [
    ("two.step", STEP, "step"), ("two.igs", IGES, "iges"),
])
def test_multiple_archive_models_require_explicit_member(root, monkeypatch, second, payload, expected):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/download/model.zip">Download</a>'})

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("one.step", STEP)
            archive.writestr(second, payload)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = {"url": content3d.BASE_URL + "/parts/supplier/608"}
    refusal = content3d.ADAPTER.acquire(result)
    assert refusal["reason"] == "unsupported" and set(refusal["candidates"]) == {"one.step", second}
    assert refusal["ask_user"]["kind"] == "choice" and refusal["ask_user"]["suggested"] == "cancel"
    assert refusal["selection_field"] == "archive_member"
    selected = content3d.ADAPTER.acquire({**result, "archive_member": second})
    assert selected["ok"] and selected["format"] == expected
    assert Path(selected["artifact_path"]).read_bytes() == payload


def test_same_model_alternate_formats_prefer_step(root, monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/download/model.zip">Download</a>'})

    def download(url, site, dest):
        with zipfile.ZipFile(dest, "w") as archive:
            archive.writestr("part.step", STEP)
            archive.writestr("part.igs", IGES)
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    selected = content3d.ADAPTER.acquire({"url": content3d.BASE_URL + "/parts/supplier/608"})
    assert selected["ok"] and selected["format"] == "step"
    assert Path(selected["artifact_path"]).read_bytes() == STEP


def test_acquire_invalid_replacement_preserves_previous_file(root, monkeypatch):
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": DETAIL})
    payload = [STEP]
    def download(url, site, dest):
        dest.write_bytes(payload[0])
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(session, "browser_download", download)
    result = content3d.ADAPTER.acquire({"url": content3d.BASE_URL + "/parts/supplier/608"})
    saved = Path(result["artifact_path"])
    payload[0] = b"<html>session expired</html>"
    assert content3d.ADAPTER.acquire(
        {"url": content3d.BASE_URL + "/parts/supplier/608"}
    )["reason"] == "parse_failed"
    assert saved.read_bytes() == STEP
