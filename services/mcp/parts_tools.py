"""Unified parts-library MCP tools — aicad.parts/1 (T-P1).

Exposes two tools over the federated parts hub:

- ``part_search(query, domain?, limit?)`` — FTS5-ranked search across all indexed providers.
- ``part_get(part_id)`` — retrieve a single PartRecord by its stable part_id.

The hub is lazy-initialized on the first call; subsequent calls reuse the in-memory FTS index.
No network, no keys, no core dependency for the fastener/bearing adapter (they run on the
bundled clean-room YAML tables). The core-families adapter degrades gracefully when libaicad
is absent.

See contracts/parts.md for the full PartRecord envelope spec.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Ensure repo root is on sys.path so engine_oracle is importable from the MCP server process.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _ok(payload) -> str:
    return json.dumps({"ok": True, **payload} if isinstance(payload, dict) else {"ok": True, "result": payload},
                      ensure_ascii=False)


def _err(reason: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "reason": str(reason), "hint": str(hint)}, ensure_ascii=False)


def register_parts_tools(mcp: FastMCP) -> None:
    """Register part_search and part_get MCP tools (append-only)."""

    @mcp.tool()
    def part_search(query: str, domain: str = "", limit: int = 20) -> str:
        """Search the unified standard-parts library (fasteners, bearings, core families, …).

        Returns a ranked list of PartRecord dicts (aicad.parts/1). Ranking: exact
        designation match first (e.g. "6205"), then standard prefix, then FTS score.

        query  : free-text, e.g. "M8 socket head", "bearing 6205", "hex nut M6"
        domain : optional filter — fastener | bearing | mech_family | section |
                 fitting | hydraulic | ecad (leave blank for all domains)
        limit  : max results (default 20)

        Examples:
          part_search("M8 socket head")       → ISO 4762 M8 shcs records
          part_search("bearing 6205")         → ISO 15 6205 bearing record
          part_search(domain="mech_family")   → all core parametric families
        """
        try:
            from engine_oracle.parts_hub import search as _search
            results = _search(
                query,
                domain=domain or None,
                limit=max(1, min(int(limit), 200)),
            )
            return json.dumps({"ok": True, "count": len(results), "results": results},
                              ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"part_search failed: {exc}",
                        "Check that engine_oracle/parts_hub is importable.")

    @mcp.tool()
    def part_get(part_id: str) -> str:
        """Retrieve a single PartRecord by its stable part_id (aicad.parts/1).

        part_id is in the form "<domain>:<catalog_id>", e.g.:
          "fastener:ISO4762:M8"    — ISO 4762 M8 socket head cap screw
          "bearing:ISO15:6205"     — ISO 15 6205 deep-groove ball bearing
          "mech_family:shcs:M5"   — core parametric shcs M5

        Returns the PartRecord dict on success, or {"ok": false, "reason": ...} when
        the part_id is unknown.
        """
        try:
            from engine_oracle.parts_hub import get as _get
            rec = _get(part_id)
            if rec is None:
                return _err(f"part_id not found: {part_id!r}",
                            "Use part_search to discover valid part_ids.")
            return json.dumps({"ok": True, "part": rec}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"part_get failed: {exc}",
                        "Check that engine_oracle/parts_hub is importable.")
