"""Wave 2 CAM audit / machine-profile MCP tests — fake bridges only (no live engine).

Covers:
  * cam_audit thin wrapper passes params and returns the envelope
  * cam_job_diff over a saved-then-modified .cam+ ZIP
  * feeds_speeds(thread=...) → tap_drill_mm
  * feeds_speeds(machine=...) clamps rpm envelope
  * cam_add_setup(machine_profile=...) merges machine max_rpm

Run:
  ~/.local/share/Chudware/mcp-venv/bin/python -X utf8 -m pytest services/mcp/test_cam_audit.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import zipfile

import pytest

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _unshadow_mcp_sdk() -> None:
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _HERE])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()


# --------------------------------------------------------------------------- helpers
def _write_cam_plus(path: str, job: dict) -> None:
    """Write a .cam+ ZIP the same way core cam_save does (job.json + MANIFEST)."""
    data = json.dumps(job).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("job.json", data)
        zf.writestr("MANIFEST", "chudware-cam/1\n")


def _sample_job() -> dict:
    return {
        "id": "cam_test",
        "name": "DiffJob",
        "units": "mm",
        "post": "grbl",
        "tools": {
            "tools": [
                {"number": 1, "name": "T1 endmill", "type": "endmill_flat", "diameter": 6.0},
                {"number": 2, "name": "T2 drill", "type": "drill", "diameter": 5.0},
            ]
        },
        "setups": [
            {
                "id": "su1",
                "name": "Setup1",
                "machine": {"kind": "mill3", "max_rpm": 15000},
                "stock": {"type": "box", "min": [0, 0, 0], "max": [50, 50, 10],
                          "material": "aluminum"},
                "wcs": {"origin": [0, 0, 10]},
                "operations": [
                    {
                        "id": "op_face_1",
                        "name": "Face",
                        "type": "face",
                        "tool_number": 1,
                        "feed": 800.0,
                        "rpm": 10000.0,
                        "stepover": 3.0,
                        "stepdown": 1.0,
                        "heights": {"clearance": 15.0, "retract": 5.0, "top": 10.0, "bottom": 9.0},
                        "params": {"feed": 800.0, "rpm": 10000.0},
                    },
                    {
                        "id": "op_pocket_1",
                        "name": "Pocket",
                        "type": "pocket2d",
                        "tool_number": 1,
                        "feed": 600.0,
                        "rpm": 9000.0,
                        "heights": {"clearance": 15.0, "retract": 5.0, "top": 10.0, "bottom": 5.0},
                    },
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- feeds_speeds
def test_feeds_speeds_thread_m10_tap_drill():
    from services.research.machining import feeds_speeds

    r = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing", thread="M10x1.5")
    assert "answer" in r, r
    assert r["answer"].get("tap_drill_mm") == 8.5, r["answer"]


def test_feeds_speeds_machine_haas_clamps_rpm():
    from services.research.machining import feeds_speeds, machine_profile

    prof = machine_profile("haas_vf2")
    assert float(prof["max_rpm"]) == 8100.0

    r = feeds_speeds("alu_6061", "endmill_flat", 6.0, "roughing", machine="haas_vf2")
    assert "answer" in r, r
    rpm = r["answer"]["rpm"]
    # Envelope must sit inside the Haas clamp (table mid for alu/endmill is often >> 8100).
    assert rpm["max"] <= 8100.0 + 1e-6, rpm
    assert rpm["mid"] <= 8100.0 + 1e-6, rpm
    assert any("haas_vf2" in n for n in (r.get("notes") or [])), r.get("notes")
    # machine_limits carried in inputs should show the profile clamp
    limits = (r.get("inputs") or {}).get("machine_limits") or {}
    assert float(limits.get("max_rpm", 0)) == 8100.0, limits


def test_machine_profile_unknown_lists_ids():
    from services.research.machining import machine_profile

    with pytest.raises(ValueError) as ei:
        machine_profile("not_a_real_machine")
    msg = str(ei.value)
    assert "haas_vf2" in msg and "tormach_770" in msg


# --------------------------------------------------------------------------- cam_audit wrapper
def test_cam_audit_wrapper_passes_params(monkeypatch):
    from mcp_server import cam_audit as ca
    from mcp_server import cam_tools as ct

    calls: list[tuple[str, dict]] = []
    envelope = {
        "schema": "aicad.cam_audit/1",
        "ok": True,
        "errors": [],
        "warnings": ["rapid below clearance"],
        "stats": {
            "lines": 42,
            "tool_changes": 1,
            "tools_used": [1],
            "bounds": {"x": [0, 10], "y": [0, 10], "z": [-5, 15]},
            "feed_range_mm_min": [100, 800],
            "rpm_range": [1000, 10000],
            "est_time_s": 12.5,
        },
        "provenance": {
            "dialect": "grbl",
            "machine_profile": "benchtop_vmc",
            "source": "supplied",
        },
    }

    def fake_call(method, params=None):
        calls.append((method, dict(params or {})))
        assert method == "cam_audit"
        return dict(envelope)

    monkeypatch.setattr(ca.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-cam-1")

    # Invoke the registered tool body by re-registering on a throwaway MCP.
    from mcp.server.fastmcp import FastMCP
    import asyncio

    mcp = FastMCP("test-cam-audit")
    ca.register_cam_audit_tools(mcp)

    async def _run():
        content, structured = await mcp.call_tool(
            "cam_audit",
            {
                "doc_id": "doc-cam-1",
                "gcode": "G21\nG0 X0 Y0 Z15\n",
                "dialect": "grbl",
                "machine": "haas_vf2",
                "setup_id": "su1",
            },
        )
        text = structured.get("result") if isinstance(structured, dict) else None
        return json.loads(text) if isinstance(text, str) else text

    out = asyncio.run(_run())
    assert out["schema"] == "aicad.cam_audit/1"
    assert out["ok"] is True
    assert out["stats"]["lines"] == 42
    assert len(calls) == 1
    method, params = calls[0]
    assert method == "cam_audit"
    assert params["doc_id"] == "doc-cam-1"
    assert params["gcode"].startswith("G21")
    assert params["dialect"] == "grbl"
    assert params["machine"] == "haas_vf2"
    assert params["setup_id"] == "su1"


# --------------------------------------------------------------------------- cam_job_diff
def test_cam_job_diff_added_removed_changed(monkeypatch, tmp_path):
    from mcp_server import cam_audit as ca
    from mcp_server import cam_tools as ct

    saved = _sample_job()
    cam_path = str(tmp_path / "job.cam+")
    _write_cam_plus(cam_path, saved)

    # Live tree: pocket removed, face feed/rpm changed, new drill op + tool 3, same setup.
    live_tree = {
        "id": "cam_test",
        "name": "DiffJob",
        "units": "mm",
        "post": "grbl",
        "tools": [
            {"number": 1, "name": "T1 endmill", "type": "endmill_flat", "diameter": 6.0},
            {"number": 2, "name": "T2 drill", "type": "drill", "diameter": 5.0},
            {"number": 3, "name": "T3 finish", "type": "endmill_flat", "diameter": 3.0},
        ],
        "setups": [
            {
                "id": "su1",
                "name": "Setup1",
                "machine": "mill3",
                "operations": [
                    {
                        "id": "op_face_1",
                        "name": "Face",
                        "type": "face",
                        "tool_number": 1,
                        "feed": 1200.0,   # changed
                        "rpm": 11000.0,  # changed
                        "params": {"feed": 1200.0, "rpm": 11000.0},
                        "heights": {"clearance": 15.0, "retract": 5.0, "top": 10.0, "bottom": 9.0},
                    },
                    {
                        "id": "op_drill_1",
                        "name": "Drill",
                        "type": "drill",
                        "tool_number": 2,
                    },
                ],
            }
        ],
    }

    def fake_call(method, params=None):
        assert method == "cam_tree"
        return {"revision": 3, "tree": live_tree}

    monkeypatch.setattr(ca.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-cam-diff")

    from mcp.server.fastmcp import FastMCP
    import asyncio

    mcp = FastMCP("test-cam-diff")
    ca.register_cam_audit_tools(mcp)

    async def _run():
        _content, structured = await mcp.call_tool("cam_job_diff", {"path": cam_path})
        text = structured.get("result") if isinstance(structured, dict) else None
        return json.loads(text) if isinstance(text, str) else text

    out = asyncio.run(_run())
    assert out["schema"] == "aicad.cam_job_diff/1"
    assert out["ok"] is True
    assert "op_drill_1" in out["ops_added"]
    assert "op_pocket_1" in out["ops_removed"]
    changed_ids = {c["op_id"]: c["fields"] for c in out["ops_changed"]}
    assert "op_face_1" in changed_ids
    assert "feed" in changed_ids["op_face_1"] or "rpm" in changed_ids["op_face_1"]
    assert 3 in out["tools_added"]
    assert out["tools_removed"] == []
    assert out["setups_added"] == []
    assert out["setups_removed"] == []
    assert "summary" in out and isinstance(out["summary"], str) and out["summary"]


def test_diff_cam_job_pure():
    """Unit-level diff without MCP registration."""
    from mcp_server.cam_audit import diff_cam_job

    saved = _sample_job()
    live = {
        "tools": [{"number": 1}, {"number": 9}],
        "setups": [
            {
                "id": "su1",
                "operations": [
                    {"id": "op_face_1", "tool_number": 9,  # tool changed
                     "params": {"feed": 800.0, "rpm": 10000.0},
                     "heights": saved["setups"][0]["operations"][0]["heights"]},
                ],
            },
            {"id": "su2", "operations": []},  # setup added
        ],
    }
    d = diff_cam_job(saved, live)
    assert "op_pocket_1" in d["ops_removed"]
    assert d["ops_added"] == []
    assert any(c["op_id"] == "op_face_1" and "tool_number" in c["fields"]
               for c in d["ops_changed"])
    assert 9 in d["tools_added"]
    assert 2 in d["tools_removed"]
    assert "su2" in d["setups_added"]


# --------------------------------------------------------------------------- cam_add_setup machine_profile
def test_cam_add_setup_machine_profile_haas(monkeypatch):
    from mcp_server import cam_tools as ct
    from mcp.server.fastmcp import FastMCP
    import asyncio

    captured: dict = {}

    def fake_call(method, params=None):
        params = dict(params or {})
        if method == "cam_machines":
            return {
                "machines": [
                    {
                        "id": "haas_vf2",
                        "label": "Haas VF-2",
                        "kind": "mill3",
                        "travel_mm": [762.0, 406.0, 508.0],
                        "rapid_xy_mm_min": 12000.0,
                        "rapid_z_mm_min": 12000.0,
                        "max_rpm": 8100.0,
                        "min_rpm": 100.0,
                        "max_feed_mm_min": 8000.0,
                        "tool_change_s": 3.5,
                        "accel_mm_s2": 0.0,
                        "post_dialect": "haas",
                        "notes": "",
                    }
                ]
            }
        if method == "cam_apply_ops":
            captured["ops"] = params.get("ops")
            return {"ok": True, "id_map": {"_s": "su_haas"}, "revision": 1}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-setup")

    mcp = FastMCP("test-cam-setup")
    ct.register_cam_tools(mcp)

    async def _run():
        _c, structured = await mcp.call_tool(
            "cam_add_setup",
            {"name": "VF2", "machine": "mill3", "material": "aluminum",
             "machine_profile": "haas_vf2"},
        )
        text = structured.get("result") if isinstance(structured, dict) else None
        return json.loads(text) if isinstance(text, str) else text

    out = asyncio.run(_run())
    assert out.get("setup_id") == "su_haas", out
    ops = captured.get("ops") or []
    assert ops and ops[0]["op"] == "add_setup"
    mach = ops[0]["setup"]["machine"]
    assert float(mach["max_rpm"]) == 8100.0, mach
    assert mach.get("kind") == "mill3"
    assert mach.get("travel") == [762.0, 406.0, 508.0]
    assert "post_dialect" not in mach
    assert "notes" not in mach
    assert "id" not in mach


def test_cam_add_setup_machine_profile_bridge_error(monkeypatch):
    from mcp_server import cam_tools as ct
    from mcp.server.fastmcp import FastMCP
    import asyncio

    def fake_call(method, params=None):
        if method == "cam_machines":
            raise RuntimeError("engine missing cam_machines")
        raise AssertionError(f"unexpected {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-setup")

    mcp = FastMCP("test-cam-setup-err")
    ct.register_cam_tools(mcp)

    async def _run():
        _c, structured = await mcp.call_tool(
            "cam_add_setup",
            {"machine_profile": "haas_vf2"},
        )
        text = structured.get("result") if isinstance(structured, dict) else None
        return json.loads(text) if isinstance(text, str) else text

    out = asyncio.run(_run())
    assert out.get("ok") is False
    assert "unavailable" in str(out.get("error", "")).lower() or "cam_machines" in str(out.get("error", ""))


# --------------------------------------------------------------------------- MCP feeds_speeds wrapper
def test_mcp_feeds_speeds_thread_and_machine(monkeypatch):
    from mcp_server import cam_tools as ct
    from mcp.server.fastmcp import FastMCP
    import asyncio

    # feeds_speeds is pure python — no cb.call needed; still register the suite.
    monkeypatch.setitem(ct._CAM, "doc_id", None)

    mcp = FastMCP("test-feeds")
    ct.register_cam_tools(mcp)

    async def _run():
        _c, structured = await mcp.call_tool(
            "feeds_speeds",
            {
                "material": "alu_6061",
                "tool_type": "endmill_flat",
                "tool_dia": 10.0,
                "operation": "roughing",
                "thread": "M10x1.5",
                "machine": "haas_vf2",
            },
        )
        text = structured.get("result") if isinstance(structured, dict) else None
        return json.loads(text) if isinstance(text, str) else text

    out = asyncio.run(_run())
    ans = out.get("answer") or {}
    assert ans.get("tap_drill_mm") == 8.5, out
    assert float((ans.get("rpm") or {}).get("max", 99999)) <= 8100.0 + 1e-6, ans
