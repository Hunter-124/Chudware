"""Native STEP+ inspection and background job waiting without an engine document."""
from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from services.mcp import cad_common, cad_doc_tools


@pytest.fixture()
def tools():
    mcp = FastMCP("native-read-wait-test")
    cad_doc_tools.register_cad_doc_tools(mcp)
    return {name: mcp._tool_manager._tools[name].fn
            for name in ("read_native_file", "wait_job", "get_job")}


@pytest.fixture()
def native_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    document = {
        "schema": "aicad.asm/1", "id": "asm-1", "name": "fixture",
        "units": {"system": "MMGS"},
        "features": [
            {"id": "f1", "type": "import", "name": "body", "suppressed": False,
             "params": {"embedded_brep": "x" * 2001, "exact": "y" * 2000},
             "refs": {"plane": "plane.top"}, "folder": "sources"},
            {"id": "f2", "type": "box", "name": "second", "suppressed": True,
             "params": {"length": 8}},
        ],
        "components": [
            {"id": "k1", "name": "fixed", "source": {"path": "fixed.step+"},
             "transform": [1] * 16, "fixed": True, "suppressed": False, "hidden": True},
            {"id": "k2", "name": "free", "source": {"path": "free.step+"},
             "transform": [0] * 16, "fixed": False, "suppressed": False},
        ],
        "mates": [{"id": "m1", "type": "distance", "align": "aligned",
                   "suppressed": False, "refs": [{"component": "k1"}],
                   "value": {"value": 5, "expr": None}}],
        "explode": [{"component": "k2", "distance": 5}],
        "material": {"key": "steel"}, "body_materials": {"b1": {"key": "alu"}},
        "appearances": {"b1": {"color": [1, 0, 0]}},
        "face_appearances": {"b1": [{"selector": "sid", "color": [0, 1, 0]}]},
        "meta": {"notes": ["z" * 2001]}, "equations": {"globals": []},
        "configurations": [{"name": "Default", "param_overrides": {}}],
        "active_configuration": "Default", "rollback_after": "f1",
        "studies": [{"id": "s1", "name": "analysis", "type": "fea",
                     "result": "s" * 2001}],
    }
    file = tmp_path / "assembly.step+"
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("document.json", json.dumps(document))
    return file


def _read(tools, **kwargs):
    return json.loads(tools["read_native_file"](**kwargs))


def test_native_sections_filters_and_bounded_nested_strings(tools, native_file):
    result = _read(tools, path=native_file.name, feature_ids=["f1"],
                   component_ids=["k2"])
    assert result["ok"] is True
    assert result["path"] == str(native_file)
    assert (result["schema"], result["id"], result["name"], result["units"],
            result["kind"]) == ("aicad.asm/1", "asm-1", "fixture",
                                 {"system": "MMGS"}, "assembly")
    assert result["counts"] == {"features": 2, "components": 2, "mates": 1,
                                "bodies_with_material": 1, "appearances": 1}
    assert [feature["id"] for feature in result["features"]] == ["f1"]
    assert result["features"][0]["folder"] == "sources"
    assert result["features"][0]["refs"] == {"plane": "plane.top"}
    assert result["features"][0]["params"] == {
        "embedded_brep": "<2001 chars omitted>", "exact": "y" * 2000}
    assert [component["id"] for component in result["components"]] == ["k2"]
    assert result["mates"][0]["value"]["value"] == 5
    assert result["meta"]["notes"] == ["<2001 chars omitted>"]
    assert result["studies"] == [{"id": "s1", "name": "analysis", "kind": "fea"}]
    assert result["active_configuration"] == "Default"
    assert result["rollback_after"] == "f1"


def test_native_section_selection_omits_params(tools, native_file):
    result = _read(tools, path=str(native_file), sections=["features", "configurations"],
                   feature_ids=["f2"], include_params=False)
    assert set(result) == {"ok", "path", "schema", "id", "name", "units", "kind",
                           "counts", "features", "configurations", "active_configuration"}
    assert result["features"] == [{"id": "f2", "type": "box", "name": "second",
                                   "suppressed": True}]
    assert result["configurations"][0]["name"] == "Default"
    assert _read(tools, path=str(native_file), sections=[])["counts"]["features"] == 2


def test_native_part_document_kind_and_empty_sections(tools, native_file):
    part = native_file.with_name("part.step+")
    with zipfile.ZipFile(part, "w") as archive:
        archive.writestr("document.json", json.dumps({
            "schema": "aicad.part/1", "id": "part-1", "name": "body",
            "units": {"system": "IPS"}, "features": [],
        }))
    result = _read(tools, path=part.name, sections=["features", "components", "mates"])
    assert result["kind"] == "part"
    assert result["counts"] == {"features": 0, "components": 0, "mates": 0,
                                "bodies_with_material": 0, "appearances": 0}
    assert result["features"] == result["components"] == result["mates"] == []


def test_native_invalid_sections_and_corrupt_files(tools, native_file, tmp_path):
    result = _read(tools, path=str(native_file), sections=["not_a_section"])
    assert result["ok"] is False and result["code"] == "unknown_section"
    assert "not_a_section" in result["error"]
    assert {"features", "meta", "studies"} <= set(result["valid_sections"])
    bad = tmp_path / "plain.step+"
    bad.write_text("not zip")
    assert _read(tools, path=str(bad))["code"] == "invalid_zip"
    missing = tmp_path / "missing.step+"
    invalid = tmp_path / "invalid.step+"
    for file, member, data in ((missing, "else.json", "{}"),
                               (invalid, "document.json", "{broken")):
        with zipfile.ZipFile(file, "w") as archive:
            archive.writestr(member, data)
    assert _read(tools, path=str(missing))["code"] == "missing_document"
    assert _read(tools, path=str(invalid))["code"] == "bad_document_json"


def test_native_refuses_paths_outside_project(tools, native_file, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.step+")
    result = _read(tools, path=str(outside))
    assert result["ok"] is False
    assert result["code"] == "outside_workspace_root"
    relative = _read(tools, path="../" + outside.name)
    assert relative["code"] == "outside_workspace_root"
    link = tmp_path / "link.step+"
    link.symlink_to(outside)
    assert _read(tools, path=str(link))["code"] == "outside_workspace_root"


@pytest.fixture()
def running_job():
    release = threading.Event()
    started = threading.Event()

    def run():
        started.set()
        release.wait(5)
        return {"answer": 42}

    jid = cad_common._start_job("test", run)
    assert started.wait(1)
    try:
        yield jid, release
    finally:
        release.set()


def test_wait_job_done_returns_same_snapshot_immediately(tools, running_job):
    jid, release = running_job
    release.set()
    deadline = time.monotonic() + 1
    while json.loads(tools["get_job"](job_id=jid))["status"] != "done":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    result = json.loads(tools["wait_job"](job_id=jid, timeout_s=2, poll_s=0.5))
    snapshot = json.loads(tools["get_job"](job_id=jid))
    assert result.pop("waited_s") < 0.5
    assert result == snapshot
    assert result["status"] == "done" and result["result"] == {"answer": 42}


def test_wait_job_times_out_with_snapshot_and_hint(tools, running_job):
    jid, _ = running_job
    start = time.monotonic()
    result = json.loads(tools["wait_job"](job_id=jid, timeout_s=0.02, poll_s=0))
    assert time.monotonic() - start < 0.5
    assert result["status"] == "running" and result["still_running"] is True
    assert "wait_job" in result["hint"] and result["waited_s"] >= 0.02
    assert result["kind"] == "test"


def test_wait_job_cancel_interrupts_only_wait(tools, running_job):
    jid, release = running_job
    count = 0

    def cancel():
        nonlocal count
        count += 1
        return count >= 3

    result = json.loads(tools["wait_job"](job_id=jid, timeout_s=5,
                                           poll_s=30, cancel=cancel))
    assert count >= 3 and result["cancelled_wait"] is True
    assert result["status"] == "running" and result["waited_s"] < 1
    release.set()
    assert json.loads(tools["wait_job"](job_id=jid, timeout_s=2,
                                         poll_s=0.5))["status"] == "done"


def test_wait_job_unknown_id_does_not_wait(tools):
    start = time.monotonic()
    unknown = json.loads(tools["wait_job"](job_id="job-unknown", timeout_s=900))
    original = json.loads(tools["get_job"](job_id="job-unknown"))
    assert time.monotonic() - start < 0.5
    assert unknown.pop("waited_s") < 0.5
    assert unknown == original
