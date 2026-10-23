"""Tests for RE-29: web/part-number research ingestion.

All network, research, and browser tools are MOCKED — no real HTTP calls.

Coverage:
  1. reveng_research_source assembles a bundle into datasheet/ with all three
     input modes (query, part_number, url).
  2. candidate_mpns and candidate_dims are extracted with correct provenance.
  3. mission_seed is present and well-formed.
  4. Honest degradation when tools are absent (offline / backend_not_provisioned).
  5. reveng_start_session with research= seed calls ingest automatically.
  6. Existing test_mcp_freshness-style import smoke — module loads cleanly.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(result: str) -> dict:
    """Parse JSON string returned by MCP tools."""
    return json.loads(result)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_session(tmp_path: Path):
    """Create a minimal reveng session directory and return (session_id, root)."""
    sid = "re_test_29aa"
    root = tmp_path / sid
    root.mkdir()
    (root / "session.json").write_text(json.dumps({
        "schema": "aicad.reveng_session/1",
        "session_id": sid,
        "name": "Test session RE-29",
        "created_at": "2026-07-07T00:00:00Z",
        "updated_at": "2026-07-07T00:00:00Z",
        "status": "capturing",
        "part_ref": None,
        "stages": {},
        "scale": {},
        "artifacts": {"images": 0},
        "honesty": {},
        "provenance": [],
    }))
    return sid, root


@pytest.fixture()
def mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point CHUDWARE_REVENG_DIR at tmp_path so sessions land there."""
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# research_and_ingest unit tests (engine_oracle layer)
# ---------------------------------------------------------------------------

class TestResearchAndIngest:
    """Unit-test the orchestrator directly with mocked seams."""

    def _make_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Import research_ingest with seams patched."""
        # Stub out the heavy optional imports before importing the module
        fake_research = types.ModuleType("services.research")
        fake_research.web_search = mock.Mock(return_value={
            "results": [
                {"url": "https://example.com/6204-2RS.pdf", "title": "Bearing datasheet"},
            ],
        })
        fake_research.fetch_page = mock.Mock(return_value={
            "ok": True, "content": "OD: 47 mm  ID: 20 mm  Width: 14 mm  PN: 6204-2RS",
            "confidence": "web",
        })
        fake_research.datasheet_fetch = mock.Mock(return_value={
            "ok": True, "content": "OD: 47 mm  ID: 20 mm  Width: 14 mm",
            "confidence": "datasheet",
        })

        # Patch sys.modules so research_ingest picks them up
        monkeypatch.setitem(sys.modules, "services.research", fake_research)

        # Stub spec_sheet_matcher
        fake_ssm = types.ModuleType("engine_oracle.spec_sheet_matcher")
        fake_ssm.normalize_mpn = lambda x: str(x).strip().upper()
        monkeypatch.setitem(sys.modules, "engine_oracle.spec_sheet_matcher", fake_ssm)

        # Stub datasheet_ingest
        fake_di = types.ModuleType("services.vision.datasheet_ingest")
        fake_di.ingest_datasheet = mock.Mock(return_value={
            "ok": True,
            "dim_rows": [
                {"od_mm": 47.0, "id_mm": 20.0, "width_mm": 14.0},
            ],
        })
        monkeypatch.setitem(sys.modules, "services.vision.datasheet_ingest", fake_di)

        # Remove cached module if present
        monkeypatch.delitem(sys.modules, "services.vision.research_ingest", raising=False)

        from services.vision import research_ingest
        return research_ingest

    def test_query_mode_assembles_bundle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        ri = self._make_module(tmp_path, monkeypatch)
        session_root = tmp_path / "re_qtest"
        session_root.mkdir()
        result = ri.research_and_ingest(session_root, query="SKF 6204-2RS bearing")
        assert result["ok"], result
        assert (session_root / "datasheet" / "research_bundle.json").exists()
        assert isinstance(result["candidate_dims"], list)
        assert isinstance(result["sources"], list)
        assert "mission_seed" in result

    def test_part_number_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        ri = self._make_module(tmp_path, monkeypatch)
        session_root = tmp_path / "re_pntest"
        session_root.mkdir()
        result = ri.research_and_ingest(session_root, part_number="6204-2RS")
        assert result["ok"], result
        # normalised MPN should appear
        assert any("6204" in mpn for mpn in result["candidate_mpns"])

    def test_url_mode_pdf(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        ri = self._make_module(tmp_path, monkeypatch)
        session_root = tmp_path / "re_urltest"
        session_root.mkdir()
        result = ri.research_and_ingest(
            session_root, url="https://example.com/6204-2RS.pdf"
        )
        assert result["ok"], result
        assert len(result["sources"]) >= 1

    def test_dims_carry_provenance(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        ri = self._make_module(tmp_path, monkeypatch)
        session_root = tmp_path / "re_provtest"
        session_root.mkdir()
        result = ri.research_and_ingest(session_root, query="bearing 6204")
        assert result["ok"], result
        for dim in result["candidate_dims"]:
            assert dim.get("provenance") in ("datasheet", "web", "vendor"), dim

    def test_mission_seed_well_formed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        ri = self._make_module(tmp_path, monkeypatch)
        session_root = tmp_path / "re_seedtest"
        session_root.mkdir()
        result = ri.research_and_ingest(session_root, query="bearing 6204")
        seed = result.get("mission_seed", {})
        assert "datasheet_dir" in seed
        assert "bundle_path" in seed
        assert "provenance_method" in seed

    def test_no_input_returns_honest_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        ri = self._make_module(tmp_path, monkeypatch)
        session_root = tmp_path / "re_noinput"
        session_root.mkdir()
        result = ri.research_and_ingest(session_root)
        assert result["ok"] is False
        assert result["reason"] == "no_input"
        assert "hint" in result

    def test_offline_tools_absent_degrades_honestly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Remove all optional seams so the module has nothing
        for mod in [
            "services.research",
            "engine_oracle.spec_sheet_matcher",
            "services.vision.datasheet_ingest",
            "services.browser.session",
        ]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.delitem(sys.modules, "services.vision.research_ingest", raising=False)

        # Patch builtins import to raise for the optional deps
        real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        _offline_mods = {"services.research", "services.browser.session"}

        def _patched_import(name, *args, **kwargs):
            if name in _offline_mods:
                raise ImportError(f"offline mock: {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=_patched_import):
            # Just test the orchestrator logic with no seams available
            from services.vision import research_ingest as ri2
            # Patch module-level refs to None
            ri2._web_search = None
            ri2._fetch_page = None
            ri2._datasheet_fetch = None
            ri2._ingest_datasheet = None
            ri2._HAS_BROWSER = False

        session_root = tmp_path / "re_offline"
        session_root.mkdir()
        result = ri2.research_and_ingest(session_root, query="bearing 6204")
        assert result["ok"] is False
        assert result.get("reason") in ("no_data_found", "offline", "backend_not_provisioned")
        assert "hint" in result


# ---------------------------------------------------------------------------
import services.mcp.reveng_common as rc_mod  # noqa: E402  ingest internals moved


# MCP tool integration tests (reveng_tools layer)
# ---------------------------------------------------------------------------

class TestRevengResearchSourceTool:
    """Integration tests for the reveng_research_source MCP tool."""

    @pytest.fixture()
    def mcp_tools(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Register reveng_tools on a minimal FastMCP and return a tool-call dict."""
        from mcp.server.fastmcp import FastMCP
        import services.mcp.reveng_tools as rt_mod

        reveng_dir = tmp_path / "reveng"
        reveng_dir.mkdir()
        monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(reveng_dir))

        self._session_id = "re_test_mcp29"
        dest = reveng_dir / self._session_id
        dest.mkdir()
        (dest / "session.json").write_text(json.dumps({
            "schema": "aicad.reveng_session/1",
            "session_id": self._session_id,
            "name": "MCP test RE-29",
            "created_at": "2026-07-07T00:00:00Z",
            "updated_at": "2026-07-07T00:00:00Z",
            "status": "capturing",
            "part_ref": None,
            "stages": {},
            "scale": {},
            "artifacts": {"images": 0},
            "honesty": {},
            "provenance": [],
        }))

        # Patch _research_and_ingest in reveng_tools
        self._mock_ingest = mock.Mock(return_value={
            "ok": True,
            "datasheet_dir": str(dest / "datasheet"),
            "candidate_mpns": ["6204-2RS"],
            "candidate_dims": [
                {"label": "OD", "mm": 47.0, "raw": "47 mm", "provenance": "web"},
                {"label": "ID", "mm": 20.0, "raw": "20 mm", "provenance": "datasheet"},
            ],
            "sources": [{"url": "https://example.com/6204", "kind": "url_direct", "ts": "2026-07-07T00:00:00Z"}],
            "mission_seed": {
                "datasheet_dir": str(dest / "datasheet"),
                "part_number": "6204-2RS",
                "bundle_path": str(dest / "datasheet" / "research_bundle.json"),
                "provenance_method": "web",
            },
        })
        monkeypatch.setattr(rc_mod, "_research_and_ingest", self._mock_ingest)

        mcp = FastMCP("test-reveng")
        rt_mod.register_reveng_tools(mcp)

        # Collect tools by name
        tools: dict = {}
        for tool in mcp._tool_manager.list_tools():
            tools[tool.name] = tool
        return tools, rt_mod, dest

    def test_tool_registered(self, mcp_tools):
        tools, _, _ = mcp_tools
        assert "reveng_research_source" in tools

    def test_returns_bundle_with_mpns_and_dims(self, mcp_tools):
        tools, rt_mod, dest = mcp_tools
        # Call the function directly via the registered tool fn
        fn = tools["reveng_research_source"].fn
        raw = fn(session_id=self._session_id, part_number="6204-2RS")
        result = _parse(raw)
        assert result["ok"], result
        assert "6204-2RS" in result["candidate_mpns"]
        assert len(result["candidate_dims"]) >= 1
        assert "mission_seed" in result

    def test_dims_have_provenance(self, mcp_tools):
        tools, _, _ = mcp_tools
        fn = tools["reveng_research_source"].fn
        raw = fn(session_id=self._session_id, query="SKF bearing")
        result = _parse(raw)
        for dim in result.get("candidate_dims", []):
            assert "provenance" in dim, f"dim missing provenance: {dim}"

    def test_session_not_found_returns_error(self, mcp_tools):
        tools, _, _ = mcp_tools
        fn = tools["reveng_research_source"].fn
        raw = fn(session_id="re_nonexistent_99", query="bearing")
        result = _parse(raw)
        assert result["ok"] is False

    def test_ingest_error_returns_honest_envelope(self, mcp_tools, monkeypatch):
        tools, rt_mod, _ = mcp_tools
        monkeypatch.setattr(rc_mod, "_research_and_ingest", mock.Mock(return_value={
            "ok": False,
            "reason": "offline",
            "hint": "No network available",
        }))
        fn = tools["reveng_research_source"].fn
        raw = fn(session_id=self._session_id, query="bearing")
        result = _parse(raw)
        assert result["ok"] is False
        assert result["reason"] == "offline"
        assert "hint" in result

    def test_ingest_absent_returns_backend_not_provisioned(self, mcp_tools, monkeypatch):
        tools, rt_mod, _ = mcp_tools
        monkeypatch.setattr(rc_mod, "_research_and_ingest", None)
        fn = tools["reveng_research_source"].fn
        raw = fn(session_id=self._session_id, query="bearing")
        result = _parse(raw)
        assert result["ok"] is False
        assert result["reason"] == "backend_not_provisioned"

    def test_start_session_with_research_seed(self, mcp_tools):
        tools, rt_mod, _ = mcp_tools
        fn = tools["reveng_start_session"].fn
        raw = fn(name="research seeded session", research="6204-2RS")
        result = _parse(raw)
        assert result["ok"], result
        assert "session_id" in result
        # research_result should be present and ok
        assert result.get("research_result", {}).get("ok"), result

    def test_mission_seed_ready_for_reveng_datasheet(self, mcp_tools):
        """mission_seed must contain the fields the reveng_datasheet mission needs."""
        tools, _, _ = mcp_tools
        fn = tools["reveng_research_source"].fn
        raw = fn(session_id=self._session_id, part_number="6204-2RS")
        result = _parse(raw)
        seed = result.get("mission_seed", {})
        assert "datasheet_dir" in seed
        assert "bundle_path" in seed
        assert "provenance_method" in seed


# ---------------------------------------------------------------------------
# Freshness smoke — ensure the module imports cleanly
# ---------------------------------------------------------------------------

# Both smokes force a FRESH import to prove the module still loads standalone. Re-importing
# leaves a SECOND module object in sys.modules, and the original is what every already-imported
# test module still holds a reference to — services/mcp/__init__.py deliberately unifies
# `services.mcp.*` and `services.mcp.*` onto ONE object, so replacing it silently splits that
# identity for the rest of the process. That is why a later suite could see a reveng/cad module
# that was not the one it had bound to. Restore the original object once the check is done.
def _fresh_import_restoring(name: str):
    import importlib
    saved = sys.modules.get(name)
    sys.modules.pop(name, None)
    try:
        return importlib.import_module(name)
    finally:
        if saved is not None:
            sys.modules[name] = saved
        else:
            sys.modules.pop(name, None)


def test_reveng_research_ingest_module_importable():
    """Smoke: research_ingest module loads without error (seams may be absent)."""
    mod = _fresh_import_restoring("services.vision.research_ingest")
    assert hasattr(mod, "research_and_ingest")
    assert callable(mod.research_and_ingest)


def test_reveng_tools_module_importable():
    """Smoke: reveng_tools still imports cleanly after RE-29 additions."""
    mod = _fresh_import_restoring("services.mcp.reveng_tools")
    assert hasattr(mod, "register_reveng_tools")
    assert callable(mod.register_reveng_tools)
