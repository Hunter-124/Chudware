"""RE-33: tests for ``reveng_make_part_from_library`` one-call orchestrator.

All research + mission runners are MOCKED — no network, no C++ engine, no DAG
executor required.  The three scenarios from the acceptance criteria:

1. Metadata-only part with known MPN → model present, ``part_place`` succeeds.
2. Part with unresolved fit-critical dims → ``pending_dims`` returned, no fabrication.
3. Offline (research backend absent) → honest ``{ok: false, reason, hint}`` envelope.

Existing sibling tests (test_reveng_tools.py, test_reveng_parts_roundtrip.py,
test_reveng_research_ingest.py, test_mcp_freshness.py) must continue to pass
separately; this file is self-contained.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
import importlib

import pytest


# ---------------------------------------------------------------------------
# Helpers to load reveng_tools in isolation
# ---------------------------------------------------------------------------

def _load_reveng_tools(tmp_path: Path, monkeypatch) -> Any:
    """Import reveng_tools with CHUDWARE_REVENG_DIR pointed at *tmp_path*."""
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "reveng"))

    # Reload to pick up the env var
    import services.mcp.reveng_tools as rt
    importlib.reload(rt)
    return rt


def _make_fake_mcp():
    """Minimal FastMCP stub that collects registered tools."""
    tools: dict[str, Any] = {}

    class _FakeMCP:
        def tool(self):
            def _dec(fn):
                tools[fn.__name__] = fn
                return fn
            return _dec

    return _FakeMCP(), tools


def _call(tools: dict, name: str, **kwargs) -> dict:
    raw = tools[name](**kwargs)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def reveng_tmp(tmp_path, monkeypatch):
    d = tmp_path / "reveng"
    d.mkdir()
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(d))
    return d


@pytest.fixture()
def parts_hub_fake(monkeypatch):
    """Inject a minimal parts-hub record for a metadata-only bearing part."""
    PART_ID = "catalog:test:bearing-6204-2RS"
    record = {
        "part_id": PART_ID,
        "name": "SKF 6204-2RS Deep Groove Ball Bearing",
        "geometry_ref": None,
        "metadata": {
            "photos": [],
            "datasheet": None,
            "model_present": False,
        },
    }

    fake_hub = ModuleType("services.parts")
    fake_hub.get = lambda ref: record if ref == PART_ID else None  # type: ignore[attr-defined]

    def _fake_reg(adapter):
        pass

    fake_hub.register_provider = _fake_reg  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "services.parts", fake_hub)
    return PART_ID, record


def _inject_research_mock(monkeypatch, result: dict):
    """Patch _research_and_ingest to return *result*."""
    import services.mcp.reveng_common as rc  # lookup happens in reveng_common
    monkeypatch.setattr(rc, "_research_and_ingest", lambda *a, **kw: result)


def _inject_mission_mock(monkeypatch, result: dict):
    """Patch _launch_reveng_mission to return *result*."""
    import services.mcp.reveng_build_tools as rb  # tools look it up here
    monkeypatch.setattr(rb, "_launch_reveng_mission", lambda *a, **kw: result)


def _assert_placeable_record(part_id: str) -> None:
    """After attach, the RevengResultAdapter must serve a record the C++ part_place
    dispatcher (core/parts/place.cpp) accepts: an import_file geometry_ref pointing at
    the mission mesh."""
    from services.parts.adapters.reveng_result_adapter import RevengResultAdapter
    rec = RevengResultAdapter().get(part_id)
    assert rec is not None, "RevengResultAdapter has no record — attach may have failed"
    gref = rec.get("geometry_ref")
    assert gref and gref.get("kind") == "import_file" and gref.get("path"), \
        f"record not place-shaped after attach: {rec}"


# ---------------------------------------------------------------------------
# Scenario 1: known MPN → model present, part_place succeeds
# ---------------------------------------------------------------------------

def test_known_mpn_model_present_and_place(
    reveng_tmp, parts_hub_fake, monkeypatch
):
    """Metadata-only part + known MPN → pipeline completes, model_present=True."""
    PART_ID, _ = parts_hub_fake

    # Research returns specs + candidate MPNs
    _inject_research_mock(monkeypatch, {
        "ok": True,
        "candidate_mpns": ["6204-2RS"],
        "candidate_dims": {"bore_mm": 20.0, "od_mm": 47.0, "width_mm": 14.0},
        "sources": [{"url": "https://example.com/6204-2RS-ds.pdf", "kind": "datasheet"}],
        "mission_seed": {"mpn": "6204-2RS"},
    })

    # Mission succeeds and injects a mesh path into the session
    def _mission_with_mesh(session_id, template, stage_map, **kw):
        from services.mcp import reveng_tools as rt  # noqa: PLC0415
        from pathlib import Path as _P  # noqa: PLC0415
        import os  # noqa: PLC0415
        sess_dir = _P(os.environ["CHUDWARE_REVENG_DIR"]) / session_id
        mesh_dir = sess_dir / "mesh"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        mesh_file = mesh_dir / "part.ply"
        mesh_file.write_text("ply\n")
        # Update session artifacts
        s = rt._load_session(session_id)
        if s:
            s["artifacts"]["mesh_path"] = str(mesh_file)
            rt._save_session(s)
        return {"ok": True, "status": "done", "interrupted": False, "pending_dims": []}

    import services.mcp.reveng_build_tools as rb  # tools look it up here
    import services.mcp.reveng_tools as rt
    monkeypatch.setattr(rb, "_launch_reveng_mission", _mission_with_mesh)

    mcp, tools = _make_fake_mcp()
    rt.register_reveng_tools(mcp)

    result = _call(tools, "reveng_make_part_from_library",
                   part_ref=PART_ID, part_number="6204-2RS")

    assert result["ok"] is True, f"Expected ok=True, got: {result}"
    assert result["model_present"] is True, f"Expected model_present=True, got: {result}"
    assert result["part_ref"] == PART_ID
    sid = result["session_id"]
    assert sid.startswith("re_")

    # Sidecar must exist
    sidecar_path = Path(reveng_tmp) / sid / "reveng_result.json"
    assert sidecar_path.exists(), "reveng_result.json sidecar missing"
    sc = json.loads(sidecar_path.read_text())
    assert sc["part_ref"] == PART_ID
    assert sc["schema"] == "aicad.reveng_result/1"

    # part_place seam: the attached record must be place-shaped for the C++
    # dispatcher (the actual core placement of a reveng result is covered by
    # test_reveng_parts_roundtrip.py::test_part_place_succeeds_after_attach).
    # Drop the fake hub first: the adapter import needs the REAL services.parts package.
    monkeypatch.delitem(sys.modules, "services.parts", raising=False)
    _assert_placeable_record(PART_ID)


# ---------------------------------------------------------------------------
# Scenario 2: unresolved fit-critical dims → pending_dims, no fabrication
# ---------------------------------------------------------------------------

def test_pending_dims_returned_no_fabrication(
    reveng_tmp, parts_hub_fake, monkeypatch
):
    """Mission pauses on fit-critical dims → pending_dims in result, model_present=False."""
    PART_ID, _ = parts_hub_fake

    _inject_research_mock(monkeypatch, {
        "ok": True,
        "candidate_mpns": ["6204-2RS"],
        "candidate_dims": {},
        "sources": [],
        "mission_seed": {},
    })

    # Mission returns interrupted with pending_dims
    def _mission_pending(session_id, template, stage_map, **kw):
        return {
            "ok": True,
            "status": "interrupted",
            "interrupted": True,
            "pending_dims": [
                {"dim": "bore_mm", "provenance": "datasheet", "honesty": "medium",
                 "value_mm": None, "fit_critical": True},
                {"dim": "od_mm", "provenance": "datasheet", "honesty": "medium",
                 "value_mm": None, "fit_critical": True},
            ],
        }

    import services.mcp.reveng_build_tools as rb  # tools look it up here
    import services.mcp.reveng_tools as rt
    monkeypatch.setattr(rb, "_launch_reveng_mission", _mission_pending)

    mcp, tools = _make_fake_mcp()
    rt.register_reveng_tools(mcp)

    result = _call(tools, "reveng_make_part_from_library",
                   part_ref=PART_ID, part_number="6204-2RS")

    assert result["ok"] is True, f"Expected ok=True (resumable), got: {result}"
    assert result["model_present"] is False
    assert "pending_dims" in result
    assert len(result["pending_dims"]) == 2

    # Verify no dimension was fabricated: all value_mm are None
    for dim in result["pending_dims"]:
        assert dim.get("value_mm") is None, (
            f"Dimension {dim['dim']} was fabricated with value {dim['value_mm']!r}"
        )

    # Session must still be on disk (resumable)
    sid = result["session_id"]
    assert (Path(reveng_tmp) / sid / "session.json").exists()

    # No sidecar — not attached
    assert not (Path(reveng_tmp) / sid / "reveng_result.json").exists()


# ---------------------------------------------------------------------------
# Scenario 3: offline / research backend absent → honest envelope
# ---------------------------------------------------------------------------

def test_offline_honest_envelope(
    reveng_tmp, parts_hub_fake, monkeypatch
):
    """When the research ingest module is absent, return honest {ok:false} — no traceback."""
    PART_ID, _ = parts_hub_fake

    import services.mcp.reveng_common as rc
    monkeypatch.setattr(rc, "_research_and_ingest", None)

    import services.mcp.reveng_tools as rt

    mcp, tools = _make_fake_mcp()
    rt.register_reveng_tools(mcp)

    result = _call(tools, "reveng_make_part_from_library",
                   part_ref=PART_ID, part_number="6204-2RS")

    assert result["ok"] is False
    assert "reason" in result
    assert "hint" in result
    # Must not be a Python traceback
    assert "Traceback" not in result.get("reason", "")
    assert "Traceback" not in result.get("hint", "")
    # part_ref preserved for context
    assert result.get("part_ref") == PART_ID


# ---------------------------------------------------------------------------
# Scenario 4: offline with no part_number/query → session created, skips research
# ---------------------------------------------------------------------------

def test_no_query_skips_research_and_fails_mission_gracefully(
    reveng_tmp, parts_hub_fake, monkeypatch
):
    """No part_number/query → research skipped; mission unavailable → honest fail."""
    PART_ID, _ = parts_hub_fake

    # Mission runner absent
    import services.mcp.reveng_build_tools as rb  # tools look it up here
    import services.mcp.reveng_tools as rt

    def _mission_unavailable(session_id, template, stage_map, **kw):
        raise RuntimeError("DAG executor not available in test environment")

    monkeypatch.setattr(rb, "_launch_reveng_mission", _mission_unavailable)

    mcp, tools = _make_fake_mcp()
    rt.register_reveng_tools(mcp)

    result = _call(tools, "reveng_make_part_from_library", part_ref=PART_ID)

    # Should get an honest failure (mission unavailable), not an exception
    assert result["ok"] is False
    assert result.get("reason") == "mission_runner_unavailable"
    assert "Traceback" not in result.get("hint", "")
