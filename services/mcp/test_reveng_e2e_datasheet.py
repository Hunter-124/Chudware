"""RE-25 — E2E datasheet demo: 6205 bearing PDF → MPN fast-path → OD/ID/Width assertions.

Flow:
  1. reveng_start_session (via reveng_tools registry) — creates a session directory.
  2. reveng_ingest_datasheet — pdfplumber backend extracts text/tables from the fixture PDF,
     identifies 6205-2RS as an MPN candidate.
  3. match_part_number('6205-2RS') — deterministic BOLTS fast-path → bore/outer_d/width.
  4. Assert OD == 52, ID == 25, Width == 15 (ISO 6205 nominal).

The dim-confirm gate in the reveng_datasheet.json mission is auto-answered by reading
params directly from the deterministic BOLTS table (no LLM, no cloud, fully offline).

reveng_build_parametric requires a dense-mesh backend (not installed offline); that step
is isolated behind pytest.mark.skipif so it does not block the assertion suite.

Run:
    python3 -m pytest services/mcp/test_reveng_e2e_datasheet.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BEARING_PDF = _REPO_ROOT / "corpora" / "reveng" / "bearing_spec.pdf"

# Expected ISO 6205 nominal dimensions (mm)
_EXPECTED_OD = 52.0
_EXPECTED_ID = 25.0
_EXPECTED_WIDTH = 15.0


def _require_pdf():
    if not _BEARING_PDF.exists():
        pytest.skip(f"Bearing fixture not found: {_BEARING_PDF}")


# ---------------------------------------------------------------------------
# 1. Datasheet ingest (pdfplumber offline backend)
# ---------------------------------------------------------------------------

def test_ingest_datasheet_returns_ok():
    """ingest_datasheet() returns ok=True using pdfplumber (must be installed)."""
    _require_pdf()
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        pytest.skip("pdfplumber not installed")

    from services.vision.datasheet_ingest import ingest_datasheet

    with tempfile.TemporaryDirectory() as tmpdir:
        result = ingest_datasheet(str(_BEARING_PDF), tmpdir)
        assert result.get("ok"), f"ingest failed: {result}"
        assert result.get("backend") in ("pdfplumber", "docling"), (
            f"unexpected backend: {result.get('backend')}"
        )


def test_ingest_extracts_mpn_candidate():
    """ingest_datasheet() extracts '6205' or '6205-2RS' as an MPN candidate."""
    _require_pdf()
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        pytest.skip("pdfplumber not installed")

    from services.vision.datasheet_ingest import ingest_datasheet

    with tempfile.TemporaryDirectory() as tmpdir:
        result = ingest_datasheet(str(_BEARING_PDF), tmpdir)
        assert result.get("ok"), f"ingest failed: {result}"
        candidates = result.get("mpn_candidates", [])
        # At minimum the base part number 6205 should appear
        found = any("6205" in c for c in candidates)
        assert found, (
            f"Expected '6205' in mpn_candidates but got: {candidates}"
        )


def _match_part_number(mpn, generate=False):
    """engine_oracle.spec_sheet_matcher was deleted — call the C++ port."""
    from services.mcp import core_bridge as cb

    r = cb.call("match_part_number", {"mpn": mpn, "generate": generate})
    if isinstance(r, dict) and isinstance(r.get("match"), dict):
        return r["match"]
    return r


# ---------------------------------------------------------------------------
# 2. MPN fast-path — deterministic BOLTS lookup (offline, no LLM)
# ---------------------------------------------------------------------------

def test_match_part_number_6205_params():
    """match_part_number('6205-2RS') resolves OD/ID/Width from the BOLTS table."""
    result = _match_part_number("6205-2RS", generate=False)

    assert result.get("matched"), (
        f"6205-2RS should match in BOLTS bearing table but got: {result}"
    )
    assert result.get("family") == "bearing", (
        f"Expected family='bearing', got: {result.get('family')}"
    )

    params = result.get("params", {})
    assert params is not None, "params should not be None on a hit"

    # Core dimension assertions
    assert params["outer_d"] == _EXPECTED_OD, (
        f"OD: expected {_EXPECTED_OD}, got {params['outer_d']}"
    )
    assert params["bore"] == _EXPECTED_ID, (
        f"ID: expected {_EXPECTED_ID}, got {params['bore']}"
    )
    assert params["width"] == _EXPECTED_WIDTH, (
        f"Width: expected {_EXPECTED_WIDTH}, got {params['width']}"
    )


def test_match_part_number_6205_without_suffix():
    """'6205' (no seal code) also resolves the same dimensions."""
    result = _match_part_number("6205", generate=False)
    assert result.get("matched"), f"6205 should match: {result}"
    params = result.get("params", {})
    assert params["outer_d"] == _EXPECTED_OD
    assert params["bore"] == _EXPECTED_ID
    assert params["width"] == _EXPECTED_WIDTH


# ---------------------------------------------------------------------------
# 3. Full E2E flow (ingest → MPN → dim confirmation)
# ---------------------------------------------------------------------------

def test_e2e_ingest_then_mpn_dims():
    """Full pipeline: ingest PDF → find MPN candidate → resolve dims → assert OD/ID/Width."""
    _require_pdf()
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        pytest.skip("pdfplumber not installed")

    from services.vision.datasheet_ingest import ingest_datasheet

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: ingest datasheet
        ingest_result = ingest_datasheet(str(_BEARING_PDF), tmpdir)
        assert ingest_result.get("ok"), f"ingest failed: {ingest_result}"

        # Step 2: find a 6205-family MPN in candidates
        candidates = ingest_result.get("mpn_candidates", [])
        bearing_candidate = next(
            (c for c in candidates if "6205" in c),
            None,
        )
        # If not in candidates (sparse PDF text), fall back to the known MPN
        mpn = bearing_candidate or "6205-2RS"

        # Step 3: MPN fast-path resolution
        match_result = _match_part_number(mpn, generate=False)
        assert match_result.get("matched"), (
            f"MPN '{mpn}' from datasheet did not match in BOLTS table: {match_result}"
        )

        params = match_result.get("params", {})

        # Step 4: dim-confirm gate — assert the three fit-critical dims
        # (In the full mission this gate is answered by the agent reading params;
        # here we assert them directly as the auto-answer.)
        assert params["outer_d"] == _EXPECTED_OD, (
            f"OD mismatch: expected {_EXPECTED_OD} mm, got {params['outer_d']}"
        )
        assert params["bore"] == _EXPECTED_ID, (
            f"ID mismatch: expected {_EXPECTED_ID} mm, got {params['bore']}"
        )
        assert params["width"] == _EXPECTED_WIDTH, (
            f"Width mismatch: expected {_EXPECTED_WIDTH} mm, got {params['width']}"
        )


# ---------------------------------------------------------------------------
# 4. reveng_tools MCP session flow (start_session + ingest_datasheet via MCP tool)
# ---------------------------------------------------------------------------

def test_reveng_session_ingest_via_mcp():
    """reveng_start_session creates a valid session; reveng_ingest_datasheet is registered.

    NOTE: The MCP tool ``reveng_ingest_datasheet`` delegates to ``services.vision.ingest.
    ingest_pdf`` which does not yet exist (reveng_tools.py falls back to a docling-only stub).
    The session-create flow is tested live here; the ingest MCP path is skipped with a clear
    reason (contract gap: reveng_tools._ingest_pdf should wire to datasheet_ingest.ingest_datasheet).
    Pdfplumber ingest is covered end-to-end in test_e2e_ingest_then_mpn_dims above.
    """
    import os
    import tempfile
    import asyncio

    with tempfile.TemporaryDirectory() as reveng_dir:
        os.environ["CHUDWARE_REVENG_DIR"] = reveng_dir

        from mcp.server.fastmcp import FastMCP
        from services.mcp.reveng_tools import register_reveng_tools

        mcp = FastMCP("test-reveng")
        register_reveng_tools(mcp)

        tool_map = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert "reveng_start_session" in tool_map, (
            f"reveng_start_session not registered; got: {list(tool_map)}"
        )
        assert "reveng_ingest_datasheet" in tool_map

        def _parse(raw) -> dict:
            """call_tool may return Content objects or a char-list; normalize both."""
            if raw and hasattr(raw[0], "text"):
                return json.loads(raw[0].text)
            return json.loads("".join(str(c) for c in raw))

        async def _run():
            start_raw = await mcp._tool_manager.call_tool(
                "reveng_start_session",
                {"name": "6205 bearing e2e test"},
            )
            return _parse(start_raw)

        start_data = asyncio.run(_run())
        assert start_data.get("ok"), f"start_session failed: {start_data}"
        session = start_data["session"]
        assert session["schema"] == "aicad.reveng_session/1"
        assert session["session_id"].startswith("re_")

        # MCP ingest tool gated on docling (reveng_tools._ingest_pdf stub);
        # pdfplumber E2E path is covered by test_e2e_ingest_then_mpn_dims.
        try:
            import docling  # noqa: F401
            docling_available = True
        except ImportError:
            docling_available = False

        if not docling_available:
            pytest.skip(
                "reveng_ingest_datasheet MCP tool requires docling "
                "(reveng_tools._ingest_pdf is a docling-only stub; "
                "contract gap: should wire to datasheet_ingest.ingest_datasheet for pdfplumber fallback)"
            )

        del os.environ["CHUDWARE_REVENG_DIR"]


# ---------------------------------------------------------------------------
# 5. Build-parametric step (skipped when dense backend absent — honest gate)
# ---------------------------------------------------------------------------

def _dense_available() -> bool:
    try:
        from services.vision.reveng_provision import reveng_status
        caps = reveng_status().get("capabilities", {})
        return bool(caps.get("dense"))
    except Exception:
        return False


@pytest.mark.skipif(
    not _dense_available(),
    reason="Dense reconstruction backend not provisioned (OpenMVS/Brush absent) — "
           "reveng_build_parametric requires it; dim assertions above are sufficient for RE-25.",
)
def test_reveng_build_parametric_bearing():
    """When dense backend is available, reveng_build_parametric completes for a bearing session."""
    import tempfile
    import os
    from mcp.server.fastmcp import FastMCP
    from services.mcp.reveng_tools import register_reveng_tools
    import asyncio

    with tempfile.TemporaryDirectory() as reveng_dir:
        os.environ["CHUDWARE_REVENG_DIR"] = reveng_dir
        mcp = FastMCP("test-reveng-build")
        register_reveng_tools(mcp)

        def _parse(raw) -> dict:
            if raw and hasattr(raw[0], "text"):
                return json.loads(raw[0].text)
            return json.loads("".join(str(c) for c in raw))

        async def _run():
            start_raw = await mcp._tool_manager.call_tool(
                "reveng_start_session",
                {"name": "6205 build parametric"},
            )
            start_data = _parse(start_raw)
            session_id = start_data["session_id"]

            build_raw = await mcp._tool_manager.call_tool(
                "reveng_build_parametric",
                {"session_id": session_id},
            )
            build_data = _parse(build_raw)
            assert build_data.get("ok"), f"reveng_build_parametric failed: {build_data}"
            return build_data

        asyncio.run(_run())
        del os.environ["CHUDWARE_REVENG_DIR"]
