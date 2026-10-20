"""CAD URL acquisition boundaries and actionable refusal regressions."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from services.browser import adapters
from services.browser.adapters import makerworld, printables
from services.parts import model_matcher
from services.mcp import parts_tools as tools

STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PAGE = "https://www.printables.com/model/569094-micro-servo-mg90s"


def _workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)


def _download_bytes(payload: bytes):
    def download(url, dest, *, max_bytes=200 * 1024 * 1024):
        dest.write_bytes(payload)
        return {"ok": True, "dest": str(dest)}
    return download


def _mock_download(monkeypatch, payload):
    fake = _download_bytes(payload)
    monkeypatch.setattr(adapters, "download_public_cad", fake)
    monkeypatch.setattr(printables, "download_public_cad", fake)


def test_direct_step_is_saved_with_verifiable_provenance(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    _mock_download(monkeypatch, STEP)
    result = tools.fetch_cad_model_impl("https://files.example.org/download/mg90s.step?token=abc",
                                        name="MG90S", import_part=False)
    path = Path(result["artifact_path"])
    assert result["ok"] and result["format"] == "step"
    assert path.parent == tmp_path / "reference" / "mg90s"
    assert path.read_bytes() == STEP
    assert result["provenance"]["sha256"] == hashlib.sha256(STEP).hexdigest()
    assert json.loads(path.with_name(path.name + ".provenance.json").read_text()) == result["provenance"]


def test_static_page_prefers_step_and_captures_license(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    chosen = []
    from importlib import import_module
    fetch_page = import_module("services.research.fetch_page")
    content = ('<meta name="license" content="CC-BY-4.0"><a href="/model.stl">STL</a>'
               '<a href="/model.step?dl=1">STEP</a>')
    monkeypatch.setattr(fetch_page, "_http_get",
                        lambda url: ("", url, "", content))

    def download(url, dest, *, max_bytes=200 * 1024 * 1024):
        chosen.append(url)
        return _download_bytes(STEP)(url, dest, max_bytes=max_bytes)
    monkeypatch.setattr(adapters, "download_public_cad", download)
    result = tools.fetch_cad_model_impl("https://parts.example.org/mg90s", import_part=False)
    assert result["ok"] and chosen == ["https://parts.example.org/model.step?dl=1"]
    assert result["provenance"]["license"] == "CC-BY-4.0"


def test_html_shell_is_never_accepted_as_step(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    _mock_download(monkeypatch, b"<!doctype html><div>Log in</div>")
    result = tools.fetch_cad_model_impl("https://files.example.org/servo.step", import_part=False)
    assert result["ok"] is False and result["reason"] == "unsupported_format"
    assert result["candidates"] == ["https://files.example.org/servo.step"]


def test_registered_login_walls_are_actionable(monkeypatch):
    grabcad = "https://grabcad.com/library/mg90s-servo-1"
    monkeypatch.setattr("services.browser.adapters.grabcad.ADAPTER.acquire",
                        lambda _: {"ok": False, "reason": "no_download_link", "hint": "No link in SPA"})
    for url in (grabcad, "https://makerworld.com/en/models/3193033-mg90s"):
        out = tools.fetch_cad_model_impl(url, import_part=False)
        assert out["ok"] is False and out["reason"] == "login_required"
        assert url in out["detail"] and "drop" in out["detail"]
        assert url in out["candidates"]
    assert makerworld.ADAPTER.key == "makerworld"


def test_public_printables_file_is_step_with_license(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    def query(operation, vars):
        assert vars["id"] == "569094"
        return {"data": {"print": {"name": "Micro Servo MG90S",
                  "license": {"name": "GPL-3.0"}, "stls": [
                      {"id": "1", "name": "Servo.stl", "fileSize": 100},
                      {"id": "2413858", "name": "MicroServoMG90S.step", "fileSize": len(STEP)}]}}}
    monkeypatch.setattr(printables, "_query", query)
    monkeypatch.setattr(printables, "_download_link", lambda mid, fid: {
        "data": {"getDownloadLink": {"ok": True, "output": {
            "link": "https://files.printables.com/media/prints/569094/model.step"}}}})
    _mock_download(monkeypatch, STEP)
    result = tools.fetch_cad_model_impl(PAGE, import_part=False)
    assert result["ok"] and result["format"] == "step"
    assert result["provenance"]["license"] == "GPL-3.0"
    assert Path(result["artifact_path"]).read_bytes() == STEP


def test_printables_guest_download_refusal_names_page_and_manual_path(monkeypatch):
    monkeypatch.setattr(printables, "_query", lambda *_: {
        "data": {"print": {"stls": [{"id": "2413858", "name": "mg90s.step", "fileSize": 900}]}}})
    monkeypatch.setattr(printables, "_download_link", lambda *_: {
        "data": {"getDownloadLink": {"ok": False, "errors": [{"messages": ["login_required"]}]}}})
    result = tools.fetch_cad_model_impl(PAGE, import_part=False)
    assert result["reason"] == "login_required"
    assert PAGE in result["detail"] and "drop" in result["detail"]
    assert result["candidates"] == ["mg90s.step"]


def test_zip_chooses_step_over_mesh(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    archive = tmp_path / "model.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("mesh.stl", b"solid mesh\nendsolid mesh")
        bundle.writestr("servo.step", STEP)
    _mock_download(monkeypatch, archive.read_bytes())
    out = tools.fetch_cad_model_impl("https://cdn.example.org/mg90s.zip", import_part=False)
    assert out["ok"] and out["format"] == "step"
    assert Path(out["artifact_path"]).read_bytes() == STEP


def test_oversized_file_refuses_without_publishing_artifact(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "_CAD_MAX", len(STEP) - 1)
    _mock_download(monkeypatch, STEP)
    out = tools.fetch_cad_model_impl("https://example.com/servo.step", import_part=False)
    assert out["reason"] == "too_large"
    assert not list((tmp_path / "reference").rglob("*.step"))


def test_non_https_cad_url_refuses_by_name():
    result = tools.fetch_cad_model_impl("http://example.com/servo.step", import_part=False)
    assert result["ok"] is False
    assert result["reason"] == "unsupported_format"


def test_mg90s_keeps_standard_geometry_while_searching_public_models(monkeypatch):
    class PublicModels:
        key = "printables"

        def search(self, query):
            assert query == "MG90S"
            return [{"title": "Micro Servo MG90S", "site": self.key,
                     "url": PAGE}]

    monkeypatch.setattr(model_matcher, "_standard_match", lambda product: {
        "part_id": "standard:MG90S", "title": "MG90S servo", "site": "standard_hardware",
        "confidence": "exact", "score": 4.0, "attach": True,
        "artifact_plan": {"how": "make_standard_part", "family": "hobby_servo",
                          "size": "MG90S"}})
    monkeypatch.setattr(model_matcher, "_cad_adapters", lambda: [PublicModels()])
    result = model_matcher.match_model({"mpn": "MG90S"})
    assert result["best"]["part_id"] == "standard:MG90S"
    assert result["coverage"]["sites_searched"] == 1
    assert result["matches"][1]["site"] == "printables"
    assert result["matches"][1]["artifact_plan"]["how"] == "scrape_import"


def test_imported_step_reports_document_bbox(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    _mock_download(monkeypatch, STEP)
    calls = []

    def native_call(method, params):
        calls.append((method, params))
        if method == "part_import_file":
            return {"ok": True, "doc_id": "doc-mg90s"}
        if method == "doc_set_meta":
            return {"ok": True}
        if method == "doc_bbox":
            return {"size": [22.8, 12.2, 28.5]}
        raise AssertionError(method)

    from services.mcp import core_bridge
    monkeypatch.setattr(core_bridge, "call", native_call)

    class ToolSink:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register

    sink = ToolSink()
    tools.register_parts_tools(sink)
    result = json.loads(sink.tools["fetch_cad_model"](
        "https://files.example.org/mg90s.step"))
    assert result["ok"] and result["doc_id"] == "doc-mg90s"
    assert result["bbox_mm"] == [22.8, 12.2, 28.5]
    assert [method for method, _ in calls] == ["part_import_file", "doc_set_meta", "doc_bbox"]
