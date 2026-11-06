"""Offline fixtures for the Printables GraphQL and acquisition contract."""
from __future__ import annotations

import io
import json
import threading
import zipfile
from pathlib import Path

import pytest

from services.browser.adapters import printables
from services.net.safe_http import Response

STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
STL = b"solid test\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid test\n"
PAGE = "https://www.printables.com/model/42-servo"


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def pacing(monkeypatch):
    monkeypatch.setattr(printables.profiles, "ensure_profile", lambda *_: None)
    monkeypatch.setattr(printables.profiles, "profile_lock", lambda *_: threading.Lock())
    monkeypatch.setattr(printables.ratelimit, "reserve_request", lambda *_: None)
    monkeypatch.setattr(printables.ratelimit, "wait", lambda *_, **__: 0)
    monkeypatch.setattr(printables.ratelimit, "note_result", lambda *_, **__: None)


def respond(monkeypatch, payload, status=200, headers=None):
    requests = []
    def request(url, **kwargs):
        requests.append((url, kwargs))
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return Response(status, headers or {"content-type": "application/json"}, url, body)
    monkeypatch.setattr(printables.safe_http, "request", request)
    return requests


def test_search_requests_images_and_preserves_model_metadata(monkeypatch, pacing):
    requests = respond(monkeypatch, {"data": {"searchPrints2": {"items": [
        {"id": "42", "slug": "servo", "name": "Servo & horn", "license": {"name": "CC BY"},
         "image": {"filePath": "media/prints/42/images/servo.jpg"}},
        {"id": "43", "slug": "mount", "name": "Mount", "license": None, "image": None},
    ]}}})
    hits = printables.ADAPTER.search("servo & horn")
    url, kwargs = requests[0]
    assert url == printables._API and kwargs["method"] == "POST"
    body = json.loads(kwargs["body"])
    assert body["variables"]["q"] == "servo & horn" and "image{filePath}" in body["query"]
    assert kwargs["schemes"] == ("https",) and kwargs["max_bytes"] > 0
    assert hits[0]["kind"] == "model" and hits[0]["url"] == PAGE
    assert hits[0]["thumbnail"] == "https://files.printables.com/media/prints/42/images/servo.jpg"
    assert hits[0]["license"] == "CC BY" and hits[0]["model_id"] == "42"
    assert hits[0]["price"] is None and hits[0]["model"]["available"] is None
    assert hits[1]["thumbnail"] is None and hits[1]["license"] is None


def test_search_empty_and_unparseable_are_distinct(monkeypatch):
    monkeypatch.setattr(printables, "_query", lambda *_: {"data": {"searchPrints2": {"items": []}}})
    assert printables.ADAPTER.search("none") == []
    monkeypatch.setattr(printables, "_query", lambda *_: {"data": {"searchPrints2": {"unexpected": []}}})
    assert printables.ADAPTER.search("bad")[0]["reason"] == "parse_failed"


@pytest.mark.parametrize("error,reason", [
    ({"message": "Authentication required", "extensions": {"code": "UNAUTHENTICATED"}}, "login_required"),
    ({"message": "captcha required"}, "captcha"),
    ({"message": "Access denied"}, "blocked"),
    ({"message": "Too many requests"}, "rate_limited"),
    ({"message": "Unknown field"}, "parse_failed"),
])
def test_graphql_errors_are_not_empty_searches(monkeypatch, pacing, error, reason):
    respond(monkeypatch, {"errors": [error], "data": {"searchPrints2": None}})
    result = printables.ADAPTER.search("test")[0]
    assert result["ok"] is False and result["reason"] == reason and result["op"] == "search"
    if reason == "login_required":
        assert result["ask_user"]["kind"] == "approval"


@pytest.mark.parametrize("status,reason", [(401, "login_required"), (403, "blocked"), (404, "not_found"),
                                           (429, "rate_limited"), (503, "transport_failed")])
def test_http_failures_keep_status(monkeypatch, pacing, status, reason):
    respond(monkeypatch, {}, status, {"content-type": "application/json", "retry-after": "12"})
    failure = printables.ADAPTER.search("test")[0]
    assert failure["reason"] == reason and failure["status"] == status and failure["retry_after_s"] == 12


def test_invalid_json_and_network_policy(monkeypatch, pacing):
    respond(monkeypatch, b"not json")
    assert printables.ADAPTER.search("test")[0]["reason"] == "parse_failed"
    def refuse(*_, **__):
        raise printables.safe_http.OutboundPolicyError("private address")
    monkeypatch.setattr(printables.safe_http, "request", refuse)
    assert printables.ADAPTER.search("test")[0]["reason"] == "policy_refused"
    def timeout(*_, **__):
        raise TimeoutError("request timed out")
    monkeypatch.setattr(printables.safe_http, "request", timeout)
    assert printables.ADAPTER.search("test")[0]["reason"] == "timeout"


def setup_model(monkeypatch, tmp_path, files, data=STEP):
    model = {"name": "Servo", "license": {"name": "CC BY"}, "stls": files, "otherFiles": []}
    monkeypatch.setattr(printables, "_query", lambda *_: {"data": {"print": model}})
    monkeypatch.setattr(printables, "_paced", lambda _op, _url, request: (request(), None))
    monkeypatch.setattr(printables, "_download_link", lambda *_: {"data": {"getDownloadLink": {
        "ok": True, "output": {"link": "https://files.printables.com/download/model"}}}})
    monkeypatch.setattr(printables, "download_path", lambda _site, _url, filename: (tmp_path / filename, None))
    def download(_url, dest, **_kwargs):
        dest.write_bytes(data)
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(printables, "download_public_cad", download)
    def download_archive(url, dest, **kwargs):
        assert kwargs["schemes"] == ("https",) and kwargs["max_bytes"] == 200 * 1024 * 1024
        dest.write_bytes(data)
        return Response(200, {"content-type": "application/zip"}, url)
    monkeypatch.setattr(printables.safe_http, "download", download_archive)
    return model


@pytest.mark.parametrize("filename,data,expected", [
    ("model.step", STEP, "step"),
    ("model.stl", STL, "stl"),
    ("model.3mf", archive({"[Content_Types].xml": "<Types/>",
                          "3D/3dmodel.model": "<model><resources/><build/></model>"}), "3mf"),
    ("model.zip", archive({"models/part.step": STEP}), "step"),
])
def test_acquire_validates_and_classifies_cad(monkeypatch, tmp_path, filename, data, expected):
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": filename, "fileSize": len(data), "note": "original"}], data)
    out = printables.ADAPTER.acquire({"url": PAGE})
    assert out["ok"] is True and out["artifact_kind"] == "cad" and out["format"] == expected
    assert out["source_url"] == PAGE and out["license_note"] == "CC BY"
    assert out["session_profile_used"] == "anonymous" and out["file"]["note"] == "original"
    assert Path(out["artifact_path"]).is_file()
    if filename.endswith(".zip"):
        assert out["member"] == "models/part.step" and Path(out["archive_path"]).is_file()


def test_requested_format_and_ambiguous_files(monkeypatch, tmp_path):
    setup_model(monkeypatch, tmp_path, [{"id": "1", "name": "servo.stl"}, {"id": "2", "name": "servo.step"}])
    ambiguous = printables.ADAPTER.acquire({"url": PAGE})
    assert ambiguous["reason"] == "unsupported" and len(ambiguous["candidates"]) == 2
    assert ambiguous["ask_user"]["kind"] == "choice" and ambiguous["ask_user"]["suggested"] == "cancel"
    assert [option["value"] for option in ambiguous["ask_user"]["options"]] == ["cancel", "1", "2"]
    assert ambiguous["selection_field"] == "file_id"
    selected = printables.ADAPTER.acquire({"url": PAGE,
                                           ambiguous["selection_field"]: ambiguous["ask_user"]["options"][2]["value"]})
    assert selected["ok"] and selected["file"]["id"] == "2"
    out = printables.ADAPTER.acquire({"url": PAGE, "format": "step"})
    assert out["ok"] and out["file"]["id"] == "2" and out["format"] == "step"
    assert printables.ADAPTER.acquire({"url": PAGE, "format": "iges"})["reason"] == "not_found"


def test_duplicate_ids_use_unique_file_names_when_types_differ(monkeypatch, tmp_path):
    model = setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "arm.step"}])
    model["otherFiles"] = [{"id": "7", "name": "horn.step"}]
    calls = []
    monkeypatch.setattr(printables, "_download_link", lambda *args: calls.append(args) or {
        "data": {"getDownloadLink": {"ok": True, "output": {"link": "https://files.printables.com/model"}}}})
    failure = printables.ADAPTER.acquire({"url": PAGE})
    assert failure["selection_field"] == "file_name"
    values = [option["value"] for option in failure["ask_user"]["options"]]
    assert values == ["cancel", "arm.step", "horn.step"]
    chosen = printables.ADAPTER.acquire({"url": PAGE, failure["selection_field"]: values[2]})
    assert chosen["ok"] and chosen["file"]["name"] == "horn.step"
    assert calls == [("42", "7", "other")]


def test_indistinguishable_file_ids_never_offer_wrong_choice(monkeypatch, tmp_path):
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "arm.step"},
                                       {"id": "7", "name": "horn.step"}])
    failure = printables.ADAPTER.acquire({"url": PAGE})
    assert failure["reason"] == "unsupported" and failure["ask_user"] is None
    assert "selection_field" not in failure and len(failure["candidates"]) == 2


def test_requested_archive_and_format_mismatch_are_honest(monkeypatch, tmp_path):
    data = archive({"models/one.step": STEP, "models/two.step": STEP})
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "model.zip"}], data)
    out = printables.ADAPTER.acquire({"url": PAGE, "format": "zip"})
    assert out["ok"] and out["artifact_kind"] == "archive" and out["format"] == "zip"
    assert Path(out["artifact_path"]).read_bytes() == data
    before = Path(out["artifact_path"]).read_bytes()
    failure = printables.ADAPTER.acquire({"url": PAGE, "format": "stl", "file_id": "7"})
    assert failure["reason"] == "unsupported"
    assert Path(out["artifact_path"]).read_bytes() == before


def test_requested_unsafe_archive_is_rejected(monkeypatch, tmp_path):
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "model.zip"}],
                archive({"../outside.step": STEP}))
    out = printables.ADAPTER.acquire({"url": PAGE, "format": "zip"})
    assert out["reason"] == "unsupported"
    assert not (tmp_path / "42-7-model.zip").exists()


def test_archive_requires_unambiguous_member_and_forwards_selection(monkeypatch, tmp_path):
    data = archive({"parts/one.step": STEP, "parts/two.step": STEP, "parts/mesh.stl": STL})
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "model.zip"}], data)
    options = []
    def download(_url, dest, **kwargs):
        options.append(kwargs)
        dest.write_bytes(data)
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(printables, "download_public_cad", download)
    failure = printables.ADAPTER.acquire({"url": PAGE})
    assert failure["reason"] == "unsupported"
    assert set(failure["candidates"]) == {"parts/one.step", "parts/two.step", "parts/mesh.stl"}
    assert failure["ask_user"]["kind"] == "choice" and failure["ask_user"]["suggested"] == "cancel"
    assert {option["value"] for option in failure["ask_user"]["options"][1:]} == set(failure["candidates"])
    assert failure["selection_field"] == "archive_member"
    selected = printables.ADAPTER.acquire({"url": PAGE, "format": "step", "archive_member": "parts/two.step"})
    assert selected["ok"] and selected["format"] == "step" and selected["member"] == "parts/two.step"
    assert Path(selected["artifact_path"]).read_bytes() == STEP
    assert options[-1] == {"requested_format": "step", "requested_member": "parts/two.step"}
    mesh = printables.ADAPTER.acquire({"url": PAGE, "format": "stl"})
    assert mesh["ok"] and mesh["format"] == "stl" and mesh["member"] == "parts/mesh.stl"


def test_download_cooldown_and_transport_failure_are_preserved(monkeypatch, tmp_path):
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "servo.step"}])
    error = {"ok": False, "reason": "rate_limited", "status": 429, "retry_after_s": 23}
    monkeypatch.setattr(printables, "_paced", lambda *_: (None, error))
    out = printables.ADAPTER.acquire({"url": PAGE})
    assert out["reason"] == "rate_limited" and out["status"] == 429 and out["retry_after_s"] == 23


def test_download_helper_ambiguity_includes_question(monkeypatch, tmp_path):
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "model.zip"}])
    candidates = ["one.step", "two.step"]
    error = {"ok": False, "reason": "unsupported", "candidates": candidates}
    monkeypatch.setattr(printables, "_paced", lambda *_: (None, error))
    out = printables.ADAPTER.acquire({"url": PAGE})
    assert out["candidates"] == candidates and out["ask_user"]["suggested"] == "cancel"
    assert out["selection_field"] == "archive_member"
    assert [option["value"] for option in out["ask_user"]["options"]] == ["cancel", *candidates]


def test_other_files_use_other_graphql_type(monkeypatch, tmp_path):
    model = setup_model(monkeypatch, tmp_path, [])
    model["otherFiles"] = [{"id": "8", "name": "servo.step", "folder": "source"}]
    calls = []
    monkeypatch.setattr(printables, "_download_link", lambda *args: calls.append(args) or
                        {"data": {"getDownloadLink": {"ok": True, "output": {"link": "https://files.printables.com/model"}}}})
    out = printables.ADAPTER.acquire({"url": PAGE})
    assert out["ok"] and out["file"]["folder"] == "source" and calls == [("42", "8", "other")]


def test_bad_geometry_and_graphql_download_errors(monkeypatch, tmp_path):
    setup_model(monkeypatch, tmp_path, [{"id": "7", "name": "servo.step"}], b"<html>not CAD</html>")
    assert printables.ADAPTER.acquire({"url": PAGE})["reason"] == "unsupported"
    monkeypatch.setattr(printables, "_download_link", lambda *_: {"data": {"getDownloadLink": {
        "ok": False, "errors": [{"messages": ["login_required"]}]}}})
    failure = printables.ADAPTER.acquire({"url": PAGE})
    assert failure["reason"] == "login_required" and failure["ask_user"]["kind"] == "approval"
