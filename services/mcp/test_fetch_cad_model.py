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
    def download(url, dest, *, max_bytes=200 * 1024 * 1024, **kwargs):
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


def test_explicit_workspace_stays_within_marked_ancestor(monkeypatch, tmp_path):
    (tmp_path / ".chudware").mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _mock_download(monkeypatch, STEP)

    for key in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"):
        monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
        monkeypatch.setenv(key, str(workspace))
        result = tools.fetch_cad_model_impl("https://files.example.org/widget.step",
                                            name="widget", import_part=False)
        assert result["ok"]
        assert Path(result["artifact_path"]).parent == workspace / "reference" / "widget"
        assert not (tmp_path / "reference").exists()


def test_workspace_priority_and_cwd_fallback(monkeypatch, tmp_path):
    (tmp_path / ".chudware").mkdir()
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(primary))
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(secondary))
    assert tools._cad_project() == primary
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path / "missing"))
    assert tools._cad_project() == secondary
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR")
    monkeypatch.delenv("CHUDWARE_FS_ROOT")
    monkeypatch.chdir(primary)
    from services.agent import project_context
    monkeypatch.setattr(project_context, "resolve_project_root",
                        lambda start: tmp_path if start == primary else primary)
    assert tools._cad_project() == tmp_path


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


def test_static_page_with_different_models_requests_selection(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    from importlib import import_module
    fetch_page = import_module("services.research.fetch_page")
    monkeypatch.setattr(fetch_page, "_http_get", lambda url: (
        "", url, "", '<a href="/servo.step">Servo</a><a href="/mount.stl">Mount</a>'))
    def unexpected(*args, **kwargs):
        raise AssertionError("must not download an unselected model")
    monkeypatch.setattr(adapters, "download_public_cad", unexpected)
    result = tools.fetch_cad_model_impl("https://parts.example.org/models", import_part=False)
    assert result["ok"] is False and result["ask_user"]["kind"] == "choice"
    assert set(result["candidates"]) == {"https://parts.example.org/servo.step",
                                          "https://parts.example.org/mount.stl"}


def test_html_shell_is_never_accepted_as_step(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    _mock_download(monkeypatch, b"<!doctype html><div>Log in</div>")
    result = tools.fetch_cad_model_impl("https://files.example.org/servo.step", import_part=False)
    assert result["ok"] is False and result["reason"] == "unsupported_format"
    assert result["candidates"] == ["https://files.example.org/servo.step"]


def test_adapter_failures_keep_their_actual_reason(monkeypatch):
    grabcad = "https://grabcad.com/library/mg90s-servo-1"
    monkeypatch.setattr("services.browser.adapters.grabcad.ADAPTER.acquire",
                        lambda _: {"ok": False, "reason": "no_download_link", "hint": "No link in SPA"})
    out = tools.fetch_cad_model_impl(grabcad, import_part=False)
    assert out["ok"] is False and out["reason"] == "no_download_link"
    assert out["detail"] == "No link in SPA" and out["candidates"] == [grabcad]
    assert makerworld.ADAPTER.key == "makerworld"


def test_public_printables_file_is_step_with_license(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    def query(operation, vars):
        assert vars["id"] == "569094"
        return {"data": {"print": {"name": "Micro Servo MG90S",
                  "license": {"name": "GPL-3.0"}, "stls": [
                      {"id": "2413858", "name": "MicroServoMG90S.step", "fileSize": len(STEP)}]}}}
    monkeypatch.setattr(printables, "_query", query)
    monkeypatch.setattr(printables, "_download_link", lambda mid, fid: {
        "data": {"getDownloadLink": {"ok": True, "output": {
            "link": "https://files.printables.com/media/prints/569094/model.step"}}}})
    monkeypatch.setattr(printables, "_paced", lambda op, url, request: (request(), None))
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
    assert result["site"] == "printables"
    assert result["ask_user"] and result["candidates"] == [PAGE]


def test_zip_with_one_valid_model_is_extracted(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    archive = tmp_path / "model.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manual.txt", b"documentation")
        bundle.writestr("servo.step", STEP)
    _mock_download(monkeypatch, archive.read_bytes())
    out = tools.fetch_cad_model_impl("https://cdn.example.org/mg90s.zip", import_part=False)
    assert out["ok"] and out["format"] == "step"
    assert Path(out["artifact_path"]).read_bytes() == STEP


def test_zip_with_multiple_models_is_not_guessed(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    archive = tmp_path / "models.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("part.step", STEP)
        bundle.writestr("mesh.stl", b"solid mesh\nfacet normal 0 0 1\nvertex 0 0 0\nendsolid mesh\n")
    _mock_download(monkeypatch, archive.read_bytes())
    out = tools.fetch_cad_model_impl("https://cdn.example.org/models.zip", import_part=False)
    assert out["ok"] is False and out["reason"] == "unsupported_format"
    assert out["ask_user"]["kind"] == "choice"
    selected = tools.fetch_cad_model_impl("https://cdn.example.org/models.zip", import_part=False,
                                          requested_member="part.step")
    assert selected["ok"] and Path(selected["artifact_path"]).read_bytes() == STEP
    assert selected["provenance"]["member"] == "part.step"


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


def test_3dcontentcentral_dispatches_to_registered_adapter(monkeypatch):
    url = "https://www.3dcontentcentral.com/Download-Model.aspx?catalogid=1"
    calls = []
    adapter = adapters.get_adapter("3dcontentcentral")
    def acquire(result):
        calls.append(result)
        return {"ok": False, "reason": "blocked", "status": 403, "hint": "Vendor blocked request"}
    monkeypatch.setattr(adapter, "acquire", acquire)
    result = tools.fetch_cad_model_impl(url, import_part=False)
    assert calls[0]["url"] == url
    assert result["reason"] == "blocked" and result["status"] == 403


def test_zip_without_cad_is_rejected(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    archive = tmp_path / "docs.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manual.pdf", b"%PDF-1.7\ntest")
    _mock_download(monkeypatch, archive.read_bytes())
    result = tools.fetch_cad_model_impl("https://cdn.example.org/docs.zip", import_part=False)
    assert result["ok"] is False and result["reason"] == "unsupported_format"


def test_part_get_propagates_acquisition_failure(monkeypatch):
    import services.parts
    failure = {"ok": False, "site": "grabcad", "reason": "captcha", "hint": "Human action needed"}
    monkeypatch.setattr(services.parts, "get", lambda *args, **kwargs: failure)
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
    out = json.loads(sink.tools["part_get"]("catalog:grabcad::https://grabcad.com/x", acquire=True))
    assert out == failure


def test_queue_tools_preserve_structured_notices(monkeypatch):
    from services.browser import queue
    failure = {"ok": False, "site": "grabcad", "op": "search", "reason": "rate_limited",
               "hint": "try later", "status": 429, "retry_after_s": 12, "ask_user": None}
    monkeypatch.setattr(queue, "enqueue", lambda *_: failure)
    monkeypatch.setattr(queue, "queue_status", lambda: {"ok": True, "jobs": [{"error": failure}]})
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
    assert json.loads(sink.tools["scrape_enqueue"]("grabcad", "bearing")) == failure
    assert json.loads(sink.tools["scrape_queue_status"]())["jobs"][0]["error"] == failure
    selections = []
    monkeypatch.setattr(queue, "select_member", lambda job_id, member, fmt:
                        selections.append((job_id, member, fmt)) or {"ok": True, "id": "retry"})
    result = json.loads(sink.tools["scrape_enqueue"](job_id="job1", member="part.step", format="step"))
    assert result["id"] == "retry"
    assert selections == [("job1", "part.step", "step")]


def test_import_artifact_refuses_pdf_before_native_import(monkeypatch, tmp_path):
    from services.mcp import core_bridge
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.7\nnot geometry")
    def unexpected(*args, **kwargs):
        raise AssertionError("non-CAD must not reach native importer")
    monkeypatch.setattr(core_bridge, "call", unexpected)
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
    result = json.loads(sink.tools["import_artifact"](str(pdf)))
    assert result["ok"] is False and result["reason"] == "unsupported_format"


def test_selected_zip_member_is_used_for_import(monkeypatch, tmp_path):
    from services.mcp import core_bridge
    archive = tmp_path / "variants.zip"
    second = b"ISO-10303-21;\nHEADER;\n-- second model\nENDSEC;\n"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("a.step", STEP)
        bundle.writestr("b.step", second)
    imported = []
    def call(method, params):
        if method == "part_import_file":
            imported.append(Path(params["path"]).read_bytes())
            return {"ok": True, "doc_id": "part"}
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
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
    result = json.loads(sink.tools["import_artifact"](
        str(archive), source={"artifact_kind": "cad", "format": "step",
                              "member": "b.step", "cad_validated": True}))
    assert result["ok"] and imported == [second]
    assert result["vendor_source"]["member"] == "b.step"
    assert result["vendor_source"]["format"] == "step"


def test_freecad_file_download_does_not_claim_native_import(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    fcstd = tmp_path / "part.FCStd"
    with zipfile.ZipFile(fcstd, "w") as bundle:
        bundle.writestr("Document.xml", '<Document><Objects Count="0"/></Document>')
    _mock_download(monkeypatch, fcstd.read_bytes())
    def unexpected(*args):
        raise AssertionError("FCStd must not reach native importer")
    result = tools.fetch_cad_model_impl("https://example.com/part.FCStd", importer=unexpected)
    assert result["ok"] is False and result["reason"] == "unsupported_format"
    assert Path(result["artifact_path"]).read_bytes() == fcstd.read_bytes()


def test_adapter_choice_selector_reaches_source(monkeypatch):
    url = "https://grabcad.com/library/example"
    chosen = []
    adapter = adapters.get_adapter("grabcad")
    def acquire(hit):
        chosen.append(hit)
        return {"ok": False, "reason": "blocked", "hint": "vendor blocked request"}
    monkeypatch.setattr(adapter, "acquire", acquire)
    out = tools.fetch_cad_model_impl(url, import_part=False, selection_field="filename", selection="bracket.step")
    assert chosen[0]["filename"] == "bracket.step"
    assert out["reason"] == "blocked"
    assert out["selectors"] == {"filename": "bracket.step"}
    tools.fetch_cad_model_impl(url, import_part=False, selectors=out["selectors"],
                               selection_field="archive_member", selection="inside.step")
    assert chosen[1]["filename"] == "bracket.step"
    assert chosen[1]["archive_member"] == "inside.step"
