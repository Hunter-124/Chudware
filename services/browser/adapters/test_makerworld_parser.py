"""Offline MakerWorld search and authenticated acquisition fixtures."""
from __future__ import annotations

import html
import io
import json
from pathlib import Path
import re
import zipfile

import pytest

from services.browser.adapters import makerworld

PAGE = "https://makerworld.com/en/models/42-servo"
STL = b"solid test\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid test\n"


def model_3mf():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", "<model><resources/><build/></model>")
    return stream.getvalue()


def capture(body, status=200):
    text = html.escape(json.dumps({"status": status, "data": body}))
    return {"ok": True, "content": f'<div id="__makerworld_capture">{text}</div>'}


@pytest.fixture
def metadata():
    return {"id": 42, "title": "Servo mount", "license": "CC BY",
            "instances": [{"id": 81, "profileId": 910, "title": "A1 profile"}],
            "designExtension": {"model_files": []}}


def detail(monkeypatch, metadata):
    calls = []
    def fetch(url, site, **kwargs):
        calls.append((url, site, kwargs))
        return {"ok": True, "content": json.dumps(metadata), "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_fetch", fetch)
    def interact(url, site, script, **kwargs):
        assert site == "makerworld" and kwargs["sentinel"] == "__makerworld_capture"
        assert "credentials:'include'" in script and "X-BBL-Client-Type" in script
        match = re.search(r"/instance/(\d+)/f3mf", script)
        assert match
        return capture({"url": f"https://s3.us-west-1.amazonaws.com/bucket/{match.group(1)}.3mf?signature=abc"})
    monkeypatch.setattr(makerworld.session, "browser_interact", interact)
    return calls


def destination(monkeypatch, tmp_path):
    monkeypatch.setattr(makerworld, "download_path", lambda _site, _url, filename: (tmp_path / filename, None))


def test_search_uses_model_cards_and_absolute_product_images(monkeypatch):
    content = '''
    <a href="/en/models/42-servo" title="Servo mount"><img data-src="/images/servo.jpg" alt="Servo mount"></a>
    <a href="/en/models/42-servo">duplicate</a>
    <a href="https://makerworld.com/en/models/43-gear"><img src="https://cdn.example/gear.png" alt="Gear"></a>
    <a href="/en/models/44-other"><img src="/images/logo.svg" alt="Other"></a>
    '''
    calls = []
    monkeypatch.setattr(makerworld.session, "browser_fetch", lambda url, site: calls.append((url, site)) or
                        {"ok": True, "content": content, "final_url": "https://makerworld.com/en/search"})
    hits = makerworld.ADAPTER.search("servo & horn/2")
    assert calls[0] == ("https://makerworld.com/en/search/models?keyword=servo+%26+horn%2F2", "makerworld")
    assert len(hits) == 3 and all(hit["kind"] == "model" for hit in hits)
    assert hits[0]["title"] == "Servo mount" and hits[0]["url"] == PAGE
    assert hits[0]["thumbnail"] == "https://makerworld.com/images/servo.jpg"
    assert hits[1]["thumbnail"] == "https://cdn.example/gear.png"
    assert hits[2]["thumbnail"] is None and hits[0]["price"] is None
    assert hits[0]["model"]["available"] is None


def test_card_thumbnail_is_associated_with_title_not_creator(monkeypatch):
    content = '''
    <div class="js-design-card">
      <img class="avatar" src="/avatars/designer.png">
      <div><img src="/models/42/photo.jpg"></div>
      <div><a href="/en/models/42-servo">Servo mount</a></div>
    </div>
    <div class="js-design-card">
      <img class="avatar" src="/avatars/other.png">
      <a href="/en/models/43-gear">Gear</a>
    </div>
    '''
    monkeypatch.setattr(makerworld.session, "browser_fetch",
                        lambda *_: {"ok": True, "content": content})
    first, second = makerworld.ADAPTER.search("servo")
    assert first["title"] == "Servo mount"
    assert first["thumbnail"] == "https://makerworld.com/models/42/photo.jpg"
    assert second["title"] == "Gear" and second["thumbnail"] is None


def test_search_failures_and_empty(monkeypatch):
    error = {"ok": False, "site": "makerworld", "op": "fetch", "reason": "captcha", "status": 403}
    monkeypatch.setattr(makerworld.session, "browser_fetch", lambda *_: error)
    assert makerworld.ADAPTER.search("servo") == [error]
    monkeypatch.setattr(makerworld.session, "browser_fetch", lambda *_: {"ok": True, "content": "No results"})
    assert makerworld.ADAPTER.search("none") == []
    monkeypatch.setattr(makerworld.session, "browser_fetch", lambda *_: {"ok": True, "content": "<div>shell</div>"})
    assert makerworld.ADAPTER.search("bad")[0]["reason"] == "parse_failed"
    monkeypatch.setattr(makerworld.session, "browser_fetch", lambda *_: {
        "ok": True, "content": "<form><input type='password'></form>",
        "final_url": "https://makerworld.com/en/login"})
    wall = makerworld.ADAPTER.search("private")[0]
    assert wall["reason"] == "login_required" and wall["ask_user"]["kind"] == "approval"


def test_authenticated_session_download_is_validated_and_preserves_license(monkeypatch, tmp_path, metadata):
    calls = detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    payload = model_3mf()
    downloads = []
    def download(url, site, dest, **kwargs):
        downloads.append((url, site, kwargs))
        dest.write_bytes(payload)
        return {"ok": True, "dest": str(dest), "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    out = makerworld.ADAPTER.acquire({"url": PAGE, "format": "3mf"})
    assert calls[0] == ("https://makerworld.com/api/v1/design-service/design/42", "makerworld", {"no_cache": True})
    assert downloads == [("https://s3.us-west-1.amazonaws.com/bucket/81.3mf?signature=abc",
                          "makerworld", {"public_only": True})]
    assert out["ok"] and out["artifact_kind"] == "cad" and out["format"] == "3mf"
    assert Path(out["artifact_path"]).read_bytes() == payload
    assert out["source_url"] == PAGE and out["license_note"] == "CC BY"
    assert out["session_profile_used"] == "makerworld" and out["file"]["profileId"] == 910


def test_login_is_requested_only_after_authentication_failure(monkeypatch, tmp_path, metadata):
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    failure = {"ok": False, "site": "makerworld", "op": "download", "reason": "login_required",
               "status": 401, "url": PAGE, "ask_user": None}
    monkeypatch.setattr(makerworld.session, "browser_download", lambda *_, **__: failure)
    out = makerworld.ADAPTER.acquire({"url": PAGE})
    assert out["reason"] == "login_required" and out["status"] == 401
    assert out["ask_user"]["kind"] == "approval"
    blocked = {"ok": False, "reason": "blocked", "status": 403, "hint": "bot wall"}
    monkeypatch.setattr(makerworld.session, "browser_download", lambda *_, **__: blocked)
    assert makerworld.ADAPTER.acquire({"url": PAGE}) == blocked


@pytest.mark.parametrize("status,body,reason", [
    (401, {"message": "Please log in"}, "login_required"),
    (403, {"message": "Access denied"}, "blocked"),
    (418, {}, "captcha"),
    (200, {"captchaId": "challenge"}, "captcha"),
    (404, {}, "not_found"),
    (429, {}, "rate_limited"),
    (503, {}, "transport_failed"),
    (200, {}, "parse_failed"),
])
def test_profile_link_failures_keep_their_meaning(monkeypatch, tmp_path, metadata, status, body, reason):
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    monkeypatch.setattr(makerworld.session, "browser_interact",
                        lambda *_, **__: capture(body, status))
    monkeypatch.setattr(makerworld.session, "browser_download",
                        lambda *_, **__: pytest.fail("failed profile link must not download"))
    out = makerworld.ADAPTER.acquire({"url": PAGE})
    assert out["reason"] == reason
    if reason == "login_required":
        assert out["status"] == 401 and out["ask_user"]["kind"] == "approval"


def test_unselected_multiple_profiles_are_not_arbitrarily_chosen(monkeypatch, tmp_path, metadata):
    metadata["instances"].append({"id": 82, "profileId": 911, "title": "P1 profile"})
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    monkeypatch.setattr(makerworld.session, "browser_download", lambda *_, **__: pytest.fail("unexpected download"))
    result = makerworld.ADAPTER.acquire({"url": PAGE})
    assert result["reason"] == "unsupported" and len(result["candidates"]) == 2
    assert result["ask_user"]["kind"] == "choice" and result["ask_user"]["suggested"] == "cancel"
    assert [option["value"] for option in result["ask_user"]["options"]] == ["cancel", "81", "82"]
    assert result["selection_field"] == "instance_id"
    assert makerworld.ADAPTER.acquire({"url": PAGE, "profile_id": "999"})["reason"] == "not_found"
    assert makerworld.ADAPTER.acquire({"url": PAGE, "format": "step"})["reason"] == "not_found"


def test_profile_fragment_selects_the_requested_instance(monkeypatch, tmp_path, metadata):
    metadata["instances"].append({"id": 82, "profileId": 911, "title": "P1 profile"})
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    calls = []
    def download(url, _site, dest, **_kwargs):
        calls.append(url)
        dest.write_bytes(model_3mf())
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    out = makerworld.ADAPTER.acquire({"url": PAGE + "#profileId-911"})
    assert out["ok"] and out["file"]["id"] == 82 and "/82.3mf" in calls[0]
    by_instance = makerworld.ADAPTER.acquire({"url": PAGE + "#profileId-82"})
    assert by_instance["ok"] and by_instance["file"]["id"] == 82
    assert makerworld.ADAPTER.acquire({"url": PAGE + "#profileId-82", "format": "step"})["reason"] == "unsupported"


def test_requested_raw_file_is_selected_and_downloaded(monkeypatch, tmp_path, metadata):
    metadata["designExtension"]["model_files"] = [
        {"unikey": "a", "modelName": "arm.stl", "modelSize": len(STL),
         "modelUrl": "https://makerworld.bblmw.com/files/arm.stl"},
        {"unikey": "b", "modelName": "horn.stl", "modelSize": len(STL),
         "modelUrl": "https://makerworld.bblmw.com/files/horn.stl"},
    ]
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    downloads = []
    def download(url, _site, dest, **_kwargs):
        downloads.append(url)
        dest.write_bytes(STL)
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    ambiguous = makerworld.ADAPTER.acquire({"url": PAGE, "format": "stl"})
    assert ambiguous["reason"] == "unsupported" and ambiguous["ask_user"]["kind"] == "choice"
    assert [option["value"] for option in ambiguous["ask_user"]["options"]] == ["cancel", "a", "b"]
    assert ambiguous["selection_field"] == "file_id"
    out = makerworld.ADAPTER.acquire({"url": PAGE, "format": "stl", "file_id": "b"})
    assert out["ok"] and out["format"] == "stl" and out["file"]["unikey"] == "b"
    assert downloads == ["https://makerworld.bblmw.com/files/horn.stl"]
    assert makerworld.ADAPTER.acquire({"url": PAGE, "format": "step", "file_id": "b"})["reason"] == "not_found"


def test_missing_file_ids_use_working_unique_file_names(monkeypatch, tmp_path, metadata):
    metadata["designExtension"]["model_files"] = [
        {"unikey": "a", "modelName": "arm.stl",
         "modelUrl": "https://makerworld.bblmw.com/files/arm.stl"},
        {"modelName": "horn.stl", "modelUrl": "https://makerworld.bblmw.com/files/horn.stl"},
    ]
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    downloads = []
    def download(url, _site, dest, **_kwargs):
        downloads.append(url)
        dest.write_bytes(STL)
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    failure = makerworld.ADAPTER.acquire({"url": PAGE, "format": "stl"})
    assert failure["selection_field"] == "file_name"
    values = [option["value"] for option in failure["ask_user"]["options"]]
    assert values == ["cancel", "arm.stl", "horn.stl"]
    out = makerworld.ADAPTER.acquire({"url": PAGE, "format": "stl", failure["selection_field"]: values[2]})
    assert out["ok"] and out["file"]["modelName"] == "horn.stl"
    assert downloads == ["https://makerworld.bblmw.com/files/horn.stl"]


def test_indistinguishable_files_do_not_offer_unusable_choice(monkeypatch, metadata):
    metadata["designExtension"]["model_files"] = [
        {"modelName": "part.stl", "modelUrl": "https://makerworld.bblmw.com/files/a.stl"},
        {"modelName": "part.stl", "modelUrl": "https://makerworld.bblmw.com/files/b.stl"},
    ]
    detail(monkeypatch, metadata)
    failure = makerworld.ADAPTER.acquire({"url": PAGE, "format": "stl"})
    assert failure["reason"] == "unsupported" and failure["ask_user"] is None
    assert "selection_field" not in failure and len(failure["candidates"]) == 2


def test_indistinguishable_profile_ids_do_not_offer_unusable_choice(monkeypatch, metadata):
    metadata["instances"].append({"id": 81, "profileId": 911, "title": "Duplicate"})
    detail(monkeypatch, metadata)
    failure = makerworld.ADAPTER.acquire({"url": PAGE})
    assert failure["reason"] == "unsupported" and failure["ask_user"] is None
    assert "selection_field" not in failure


def test_requested_zip_stays_an_archive(monkeypatch, tmp_path, metadata):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("parts/model.stl", STL)
        z.writestr("parts/another.stl", STL)
    metadata["designExtension"]["model_files"] = [{
        "unikey": "archive", "modelName": "parts.zip",
        "modelUrl": "https://makerworld.bblmw.com/files/parts.zip",
    }]
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    def download(_url, _site, dest, **_kwargs):
        dest.write_bytes(data.getvalue())
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    out = makerworld.ADAPTER.acquire({"url": PAGE, "format": "zip"})
    assert out["ok"] and out["artifact_kind"] == "archive" and out["format"] == "zip"
    assert Path(out["artifact_path"]).read_bytes() == data.getvalue()


def test_requested_unsafe_archive_is_rejected(monkeypatch, tmp_path, metadata):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("../outside.stl", STL)
    metadata["designExtension"]["model_files"] = [{
        "unikey": "archive", "modelName": "parts.zip",
        "modelUrl": "https://makerworld.bblmw.com/files/parts.zip",
    }]
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    def download(_url, _site, dest, **_kwargs):
        dest.write_bytes(data.getvalue())
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    out = makerworld.ADAPTER.acquire({"url": PAGE, "format": "zip"})
    assert out["reason"] == "unsupported"
    assert not (tmp_path / "42-parts.zip").exists()


def test_archive_requires_explicit_member_for_ambiguous_models(monkeypatch, tmp_path, metadata):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("parts/one.stl", STL)
        z.writestr("parts/two.stl", STL)
    metadata["designExtension"]["model_files"] = [{
        "unikey": "archive", "modelName": "parts.zip",
        "modelUrl": "https://makerworld.bblmw.com/files/parts.zip",
    }]
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    def download(_url, _site, dest, **_kwargs):
        dest.write_bytes(data.getvalue())
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    failure = makerworld.ADAPTER.acquire({"url": PAGE, "file_id": "archive"})
    assert failure["reason"] == "unsupported"
    assert set(failure["candidates"]) == {"parts/one.stl", "parts/two.stl"}
    assert failure["ask_user"]["kind"] == "choice" and failure["ask_user"]["suggested"] == "cancel"
    assert {option["value"] for option in failure["ask_user"]["options"][1:]} == set(failure["candidates"])
    assert failure["selection_field"] == "archive_member"
    selected = makerworld.ADAPTER.acquire({"url": PAGE, "file_id": "archive",
                                           "format": "stl", "archive_member": "parts/two.stl"})
    assert selected["ok"] and selected["format"] == "stl" and selected["member"] == "parts/two.stl"
    assert Path(selected["artifact_path"]).read_bytes() == STL


def test_signed_url_is_fetched_through_pinned_session(monkeypatch, tmp_path, metadata):
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    signed = "https://s3.us-west-1.amazonaws.com/bucket/model?signature=abc"
    monkeypatch.setattr(makerworld.session, "browser_interact",
                        lambda *_, **__: capture({"url": signed}))
    calls = []
    def download(url, _site, dest, **kwargs):
        calls.append((url, kwargs))
        dest.write_bytes(model_3mf())
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    out = makerworld.ADAPTER.acquire({"url": PAGE})
    assert out["ok"] and out["raw_url"] == signed
    assert calls == [(signed, {"public_only": True})]


def test_invalid_geometry_does_not_replace_existing_artifact(monkeypatch, tmp_path, metadata):
    detail(monkeypatch, metadata)
    destination(monkeypatch, tmp_path)
    existing = tmp_path / "42-81.3mf"
    existing.write_bytes(b"previous")
    def download(_url, _site, dest, **_kwargs):
        dest.write_bytes(b"<html>login page</html>")
        return {"ok": True, "session_profile_used": "makerworld"}
    monkeypatch.setattr(makerworld.session, "browser_download", download)
    assert makerworld.ADAPTER.acquire({"url": PAGE})["reason"] == "unsupported"
    assert existing.read_bytes() == b"previous"


def test_metadata_transport_failure_is_preserved(monkeypatch):
    error = {"ok": False, "reason": "timeout", "status": None, "hint": "timed out"}
    monkeypatch.setattr(makerworld.session, "browser_fetch", lambda *_, **__: error)
    assert makerworld.ADAPTER.acquire({"url": PAGE}) == error
