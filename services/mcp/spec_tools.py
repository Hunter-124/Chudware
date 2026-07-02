"""Spec-sheet matching MCP tools (deterministic BOLTS/KiCad lookup, workstream B / T-B4).

Thin MCP adapter over :mod:`engine.spec_sheet_matcher`. ``match_part_number`` takes a purchased
manufacturer/vendor part number, strips vendor packaging suffixes, and resolves it against the
clean-room ISO/DIN standard-part tables (fasteners, bearings) — falling back to the user's own
installed KiCad symbol libraries — then builds real editable geometry via the ``parts_make``
core generator. Deterministic and offline: no network, no keys. A part that cannot be matched
comes back ``matched: false`` with ``params``/``model_path`` null (contract "unknown"), never a
fabricated part; a match with the native core unavailable returns the spec with ``model_path``
null.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err


def register_spec_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def match_part_number(mpn: str) -> str:
        """Match a purchased part number to a standard part + build its parametric model.

        Strips vendor/packaging suffixes (e.g. `M3x8-ND` → `M3x8`, `608-2RS-CT-ND` → `608-2RS`),
        then looks the designation up deterministically: clean-room ISO/DIN fastener + bearing
        tables first (BOLTS-format, bundled), then the user's own installed KiCad symbol libraries
        (via AICAD_KICAD_SYM_PATH; KiCad's official libs are never vendored). On a hit it also
        builds real EDITABLE geometry from the `core/parts` catalog and returns the saved
        `model_path`. Returns {ok, matched, source ("bolts"|"kicad"|null), normalized, catalog_id,
        params, model_path, family, size, length_mm}. A part that cannot be matched comes back
        matched=false with params/model_path null (never a guessed part); a match on a host
        without the native core returns the spec with model_path null.
        """
        try:
            from engine import spec_sheet_matcher as ssm
            result = ssm.match_part_number(mpn)
            return _dumps({"ok": True, **result})
        except Exception as e:  # noqa: BLE001
            return _err(e)
