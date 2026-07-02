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

    @mcp.tool()
    def spec_sheet_match(text_or_url: str, mpn: str, template: str) -> str:
        """AI-path spec-sheet matching: datasheet text/URL -> parametric model -> VendorPart.

        The long-tail lane for parts with no catalog match. An LLM reads a pasted datasheet (or a
        fetched listing URL) and extracts a dimension dict keyed to a parametric `template`
        (`capacitor_radial`, `capacitor_axial`, `cylinder`, `box` — see `list_templates`). The dims
        fill the template's `script`-feature ops to build real EDITABLE geometry, a GSL text preview
        is rendered for confirmation, and the result is registered as a VendorPart with
        `source="spec_sheet"`. The LLM call runs through the providers contract wrapped in the
        logging provider (flywheel-logged, IP-16). Dimensions are never fabricated: a datasheet that
        omits a REQUIRED dim comes back `matched=false` with `missing_required` naming the gaps and
        nothing is built or registered. Offline (no native core) a matched spec still returns with
        `model_path`/`registered` null (the "no model" contract state), never a traceback. Returns
        {ok, matched, source, mpn, template, params, missing_required, model_path, preview,
        registered, vendor_part}.
        """
        try:
            from engine import spec_sheet_matcher as ssm
            result = ssm.spec_sheet_match(text_or_url, mpn, template)
            return _dumps({"ok": True, **result})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_spec_templates() -> str:
        """List the parametric templates the AI spec-sheet path (`spec_sheet_match`) can fill.

        Returns {ok, templates:[{name, required, optional, description}]} — the dimension keys an
        LLM must extract for each template. `required` keys gate the build (never defaulted).
        """
        try:
            from engine import spec_sheet_matcher as ssm
            return _dumps({"ok": True, "templates": ssm.list_templates()})
        except Exception as e:  # noqa: BLE001
            return _err(e)
