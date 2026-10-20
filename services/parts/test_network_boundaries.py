"""Consumer-visible parts/research acquisition security boundaries; no external traffic."""
import hashlib
import importlib
import html
from pathlib import Path

import pytest

from services.agent.request_context import bind_context
from services.parts import gui_rpc, embed_rpc
from services.net import safe_http
from services.browser import session
from services.browser import adapters


def cell(actor="fixture-a"):
    return bind_context({"cell": True, "workspace_key": "team:fixture/team", "actor_key": actor,
                         "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
                         "right": "editor", "epoch": 1})


def test_cell_credentials_and_session_import_refuse_before_shared_sinks(monkeypatch):
    from services import paths
    monkeypatch.setattr(paths, "find_config", lambda: pytest.fail("shared config accessed"))
    monkeypatch.setattr(embed_rpc, "_push_cookies_to_cef_daemon",
                        lambda *_: pytest.fail("shared browser accessed"))
    for actor in ("fixture-a", "fixture-b"):
        with cell(actor):
            credentials = gui_rpc._vendor_creds_set(
                {"provider": "octopart", "creds": {"api_key": "dummy"}}, lambda _: None)
            cookies = embed_rpc._browser_session_import(
                {"site": "fixture", "cookies": [{"name": "session", "value": "dummy"}]}, lambda _: None)
        assert credentials["reason"] == cookies["reason"] == "credential_isolation_unsupported"


def test_artifact_path_confined_before_native_import(monkeypatch, tmp_path):
    from services.mcp import core_bridge
    imported = []
    monkeypatch.setattr(core_bridge, "call", lambda method, params: imported.append(params) or {"ok": True})
    with cell():
        denied = gui_rpc._parts_import_artifact({"artifact_path": str(tmp_path / "foreign.step")}, lambda _: None)
        allowed = gui_rpc._parts_import_artifact({"artifact_path": "part.step"}, lambda _: None)
    assert denied["ok"] is False and "cell_refused" in denied["reason"]
    assert allowed["ok"] is True
    assert imported == [{"path": "/work/part.step", "as_solid": False}]


def test_thumbnail_does_not_proxy_successful_nonimage_or_invalid_image(monkeypatch):
    monkeypatch.setattr(safe_http, "get", lambda url, **_: safe_http.Response(200, {}, url, b"isolated-marker"))
    assert gui_rpc._fetch_image_bytes("https://public.fixture/marker", "catalog:fixture::p") is None
    monkeypatch.setattr(safe_http, "get", lambda url, **_: safe_http.Response(200, {}, url, b"\x89PNG\r\n\x1a\ninvalid"))
    assert gui_rpc._fetch_image_bytes("https://public.fixture/marker", "catalog:fixture::p") is None


@pytest.mark.parametrize("body", [b"isolated-marker", b"\x89PNG\r\n\x1a\ninvalid",
                                  b"x" * (4 * 1024 * 1024 + 1)])
def test_old_thumbnail_cache_cannot_proxy_invalid_or_oversized_body(monkeypatch, tmp_path, body):
    image = tmp_path / "fixture.img"
    image.write_bytes(body)
    monkeypatch.setattr(gui_rpc, "_thumb_cache_paths",
                        lambda _: (image, tmp_path / "fixture.miss"))
    assert gui_rpc._thumb_cached("catalog:fixture::p") is None
    assert not image.exists()


def test_pdf_private_target_refused_without_request(monkeypatch):
    module = importlib.import_module("services.research.datasheet_fetch")
    monkeypatch.setattr(module, "is_offline", lambda: False)
    monkeypatch.setattr(module, "_load_extractor", lambda: (lambda _: "fixture PDF", "fixture"))
    monkeypatch.setattr(safe_http, "_connect", lambda *_: pytest.fail("private HTTP attempted"))
    out = module.datasheet_fetch("http://127.0.0.1/audit.pdf")
    assert out["ok"] is False and out["reason"] == "network"


def test_cell_rendering_and_download_refuse_before_profile_access(monkeypatch, tmp_path):
    monkeypatch.setattr(session.profiles, "ensure_profile", lambda *_: pytest.fail("browser profile touched"))
    with cell():
        assert session.browser_fetch("https://public.fixture/", "fixture")["reason"] == "cell_refused"
        assert session.browser_download("https://public.fixture/a.step", "fixture", tmp_path / "a.step")["reason"] == "cell_refused"


_HREF_DOWNLOAD_ADAPTERS = [
    ("anaheimautomation", ".step"), ("boschrexroth", ".step"),
    ("componentsearchengine", ".step"), ("content3d", ".step"),
    ("gbstandards", ".pdf"), ("grabcad", ".step"), ("hiwin", ".step"),
    ("hydraforce", ".step"), ("igus", ".step"), ("lcsc", ".pdf"),
    ("maxon", ".step"), ("misumi", ".step"), ("orientalmotor", ".step"),
    ("parker", ".step"), ("snapeda", ".step"), ("thk", ".step"),
    ("thomson", ".step"), ("traceparts", ".step"), ("ultralibrarian", ".step"),
    ("grainger", ".pdf"),  # Real RetailAdapter consumer, not the shared helper alone.
]


@pytest.fixture
def adapter_download_root(monkeypatch, tmp_path):
    root = tmp_path / "downloads"
    monkeypatch.setattr(adapters.paths, "BROWSER_DOWNLOADS", root)
    return root


def _acquire_catalog_link(monkeypatch, module_name, href):
    module = importlib.import_module("services.browser.adapters." + module_name)
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": f'<a href="{href}">Datasheet CAD download</a>'})
    return module.ADAPTER.acquire({"url": module.BASE_URL + "/fixture"})


@pytest.mark.parametrize("module_name, extension", _HREF_DOWNLOAD_ADAPTERS)
@pytest.mark.parametrize("stem", [
    r"..\..\outside", "C:outside", "C:%5coutside", "%2e%2e%2foutside",
    "%2foutside", "%5c%5cserver%5cshare%5coutside", "part:stream",
    "CON", "nul", "COM%C2%B9",
])
def test_catalog_adapters_refuse_unsafe_leaf_before_download(
        monkeypatch, adapter_download_root, module_name, extension, stem):
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("unsafe filename reached download sink"))
    out = _acquire_catalog_link(
        monkeypatch, module_name, "https://catalog.fixture/files/" + stem + extension)
    assert out["ok"] is False and out["reason"] == "unsafe_filename"
    assert not list(adapter_download_root.rglob("*.*"))


@pytest.mark.parametrize("module_name, extension", _HREF_DOWNLOAD_ADAPTERS)
def test_catalog_query_and_fragment_never_control_destination(
        monkeypatch, adapter_download_root, module_name, extension):
    seen = []
    name = "轴 bracket" + extension
    href = ("https://catalog.fixture/files/%E8%BD%B4%20bracket" + extension
            + r"?signature=a%2Fb&next=/..\..\evil.zip#C:\escape")

    def download(url, site, dest):
        expected = adapter_download_root / site / name
        assert dest == expected
        dest.write_bytes(b"isolated CAD fixture")
        seen.append(dest)
        return {"ok": True, "dest": str(dest), "session_profile_used": site}

    monkeypatch.setattr(session, "browser_download", download)
    out = _acquire_catalog_link(monkeypatch, module_name, href)
    assert out["ok"] is True
    assert Path(out["artifact_path"]) == seen[0]
    assert seen[0].read_bytes() == b"isolated CAD fixture"
    assert list(adapter_download_root.rglob("*" + extension)) == seen


@pytest.mark.parametrize("module_name, extension", _HREF_DOWNLOAD_ADAPTERS)
def test_catalog_adapters_refuse_existing_symlink_escape(
        monkeypatch, tmp_path, adapter_download_root, module_name, extension):
    module = importlib.import_module("services.browser.adapters." + module_name)
    root = adapter_download_root / module.ADAPTER.key
    root.mkdir(parents=True)
    outside = tmp_path / ("outside" + extension)
    outside.write_bytes(b"must remain unchanged")
    (root / ("part" + extension)).symlink_to(outside)
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("symlink escape reached download sink"))
    out = _acquire_catalog_link(
        monkeypatch, module_name, "https://catalog.fixture/files/part" + extension)
    assert out["ok"] is False and out["reason"] == "unsafe_filename"
    assert outside.read_bytes() == b"must remain unchanged"


def _acquire_mcmaster_file(monkeypatch, file_path):
    from services.browser.adapters import mcmaster
    monkeypatch.setattr(session, "browser_interact", lambda *_: {
        "ok": True, "content": '<div id="__aicad_capture" data-status="ok" data-fp="'
        + html.escape(file_path, quote=True) + '"></div>'})
    return mcmaster.ADAPTER.acquire(
        {"url": "https://www.mcmaster.com/91290A115/", "part_number": "91290A115"})


@pytest.mark.parametrize("file_path", [
    r"/CadFiles/..\..\outside.step", "/CadFiles/C:outside.step",
    r"/CadFiles/\\server\share\outside.step", "/CadFiles/part.step:stream",
    "/CadFiles/%2e%2e%2foutside.step", "/CadFiles/%5coutside.step",
    "/CadFiles/CON.step", "/CadFiles/NUL.step",
])
def test_mcmaster_refuses_unsafe_product_filepath(
        monkeypatch, adapter_download_root, file_path):
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("unsafe McMaster filename downloaded"))
    out = _acquire_mcmaster_file(monkeypatch, file_path)
    assert out["ok"] is False and out["reason"] == "unsafe_filename"
    assert not list(adapter_download_root.rglob("*.*"))


def test_mcmaster_product_filepath_preserves_unicode_and_extension(
        monkeypatch, adapter_download_root):
    expected = adapter_download_root / "mcmaster" / "轴 bracket.STEP"

    def download(url, site, dest):
        assert dest == expected
        dest.write_bytes(b"isolated STEP fixture")
        return {"ok": True, "dest": str(dest), "session_profile_used": site}

    monkeypatch.setattr(session, "browser_download", download)
    out = _acquire_mcmaster_file(monkeypatch, "/CadFiles/轴 bracket.STEP")
    assert out["ok"] is True and Path(out["artifact_path"]) == expected
    assert expected.read_bytes() == b"isolated STEP fixture"


@pytest.mark.parametrize("leaf", [
    r"..\..\outside.stp", "C:outside.stp", "%2e%2e%2foutside.stp", "NUL.stp",
])
def test_freecadlibrary_refuses_unsafe_raw_destination(
        monkeypatch, adapter_download_root, leaf):
    from services.browser.adapters import freecadlibrary
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("unsafe FreeCAD filename downloaded"))
    out = freecadlibrary.ADAPTER.acquire(
        {"raw_url": "https://raw.githubusercontent.com/FreeCAD/FreeCAD-library/main/" + leaf})
    assert out["ok"] is False and out["reason"] == "unsafe_filename"


def test_freecadlibrary_raw_query_never_controls_destination(
        monkeypatch, adapter_download_root):
    from services.browser.adapters import freecadlibrary
    expected = adapter_download_root / "freecadlibrary" / "轴 bracket.stp"

    def download(url, site, dest):
        assert dest == expected
        dest.write_bytes(b"isolated STEP fixture")
        return {"ok": True, "dest": str(dest), "session_profile_used": site}

    monkeypatch.setattr(session, "browser_download", download)
    out = freecadlibrary.ADAPTER.acquire({
        "raw_url": "https://raw.githubusercontent.com/FreeCAD/FreeCAD-library/main/"
                   "%E8%BD%B4%20bracket.stp?next=/..\\..\\evil.zip#C:\\escape"})
    assert out["ok"] is True and Path(out["artifact_path"]) == expected
    assert out["title"] == expected.name
    assert expected.read_bytes() == b"isolated STEP fixture"


def _acquire_printables_file(monkeypatch, *, file_id="7", name="轴 bracket.STEP",
                             direct="https://files.printables.com/download?signature=a%2Fb"):
    from services.browser.adapters import printables
    monkeypatch.setattr(printables, "_query", lambda *_: {"data": {"print": {
        "name": "Fixture", "stls": [{"id": file_id, "name": name, "fileSize": 24}]}}})
    monkeypatch.setattr(printables, "_download_link", lambda *_: {"data": {
        "getDownloadLink": {"ok": True, "output": {"link": direct}}}})
    return printables.ADAPTER.acquire({"url": "https://www.printables.com/model/42-fixture"})


@pytest.mark.parametrize("selection", [
    {"file_id": "../7"}, {"file_id": r"..\..\7"}, {"file_id": "C:7"},
    {"name": "%2e%2e%2ffile.step"}, {"name": r"..\..\file.step"},
    {"name": "part:stream.step"}, {"direct": "https://files.printables.com/%5coutside.step"},
    {"direct": "https://files.printables.com/CON.step"},
])
def test_printables_catalog_names_ids_and_url_share_destination_boundary(
        monkeypatch, adapter_download_root, selection):
    from services.browser.adapters import printables
    monkeypatch.setattr(printables, "download_public_cad",
                        lambda *_: pytest.fail("unsafe Printables destination downloaded"))
    out = _acquire_printables_file(monkeypatch, **selection)
    assert out["ok"] is False and out["reason"] == "unsafe_filename"
    assert not list(adapter_download_root.rglob("*.*"))


def test_printables_catalog_selection_preserves_unicode_and_cad_extension(
        monkeypatch, adapter_download_root):
    from services.browser.adapters import printables
    expected = adapter_download_root / "printables" / "42-7-轴 bracket.STEP"

    def download(url, dest):
        assert dest == expected
        dest.write_bytes(b"isolated STEP fixture")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(printables, "download_public_cad", download)
    out = _acquire_printables_file(
        monkeypatch, direct=r"https://files.printables.com/download?next=/..\..\evil.zip#C:\escape")
    assert out["ok"] is True and Path(out["artifact_path"]) == expected
    assert expected.read_bytes() == b"isolated STEP fixture"


@pytest.mark.parametrize("leaf", [
    r"..\..\outside.jpg", "C:outside.jpg", "%2e%2e%2foutside.pdf",
    "%5c%5cserver%5cshare%5coutside.jpg", "image.jpg:stream", "CON.jpg", "NUL.pdf",
])
def test_reveng_download_into_refuses_unsafe_destination_before_write(
        monkeypatch, tmp_path, leaf):
    from services.reveng import worker_rpc
    root = tmp_path / "harvest"
    monkeypatch.setattr(session, "browser_download",
                        lambda *_: pytest.fail("unsafe harvest filename downloaded"))
    assert worker_rpc._download_into(
        "https://catalog.fixture/media/" + leaf, "fixture", root) == (None, None)
    assert not root.exists()


def test_reveng_download_into_uses_only_url_path(
        monkeypatch, tmp_path):
    from services.reveng import worker_rpc
    root = tmp_path / "harvest"
    expected = root / "轴 bracket.pdf"

    def download(url, site, dest):
        assert dest == expected
        dest.write_bytes(b"isolated PDF fixture")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    path, refusal = worker_rpc._download_into(
        "https://catalog.fixture/media/%E8%BD%B4%20bracket.pdf"
        "?next=/..\\..\\evil.pdf#C:\\escape", "fixture", root)
    assert path == expected and refusal is None
    assert expected.read_bytes() == b"isolated PDF fixture"
