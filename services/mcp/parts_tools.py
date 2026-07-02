"""Unified parts-library MCP tools — aicad.parts/1 (T-P1 + T-P2 + T-P3).

Exposes three tools over the federated parts hub:

- ``part_search(query, domain?, limit?)`` — FTS5-ranked search across all indexed providers.
  T-P2: domain filter extended to section | fitting | hydraulic | ecad.
  Live vendor results (ecad_vendor) carry source:"live" in each PartRecord and are
  returned as a fallback when the local index has no hits.
- ``part_get(part_id)`` — retrieve a single PartRecord by its stable part_id.
- ``part_place(part_id, doc?, params?)`` — T-P3: insert a standard part into a doc,
  routing geometry_ref to the correct domain path.  Always returns editable doc-schema
  ops (R1 parity).  Every domain: core_family/generate_model → new mech part doc;
  symbol_footprint → sch_add_symbol; section_profile → structural element;
  fitting_id → piping fitting; fmu_component → hydraulic component.

The hub is lazy-initialized on the first call; subsequent calls reuse the in-memory FTS index.
No network, no keys, no core dependency for the fastener/bearing adapter (they run on the
bundled clean-room YAML tables). The core-families adapter degrades gracefully when libaicad
is absent. All T-P2 adapters degrade gracefully (empty results, not tracebacks) when their
backing data or credentials are absent — CI-safe.

Source attribution: each PartRecord's ``source`` field is "live" for live vendor results and
absent (local) for index results. ``part_search`` also reports ``has_live`` in the response
envelope so callers can distinguish mixed result sets.

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
    """Register part_search, part_get, and part_place MCP tools (append-only)."""

    @mcp.tool()
    def part_search(query: str, domain: str = "", limit: int = 20) -> str:
        """Search the unified standard-parts library (all domains federated).

        Returns a ranked list of PartRecord dicts (aicad.parts/1). Ranking: exact
        designation match first (e.g. "M3"), then standard prefix, then FTS score.
        Cross-domain: "M3" surfaces fastener records AND ecad footprints in one call.

        query  : free-text, e.g. "M8 socket head", "bearing 6205", "W8x31", "elbow NPS1",
                 "pump hydraulic", "M3" (cross-domain: fasteners + ecad), "resistor"
        domain : optional filter — fastener | bearing | mech_family | section |
                 fitting | hydraulic | ecad (leave blank for all domains)
        limit  : max results (default 20)

        Source attribution: results from live vendor search carry source:"live".
        Local indexed results carry no source field (local is the default).

        Examples:
          part_search("M3")                → M3 fasteners + M3 mounting hole ecad footprint
          part_search("M8 socket head")    → ISO 4762 M8 shcs records
          part_search("W8x31")             → AISC W8x31 steel section
          part_search("elbow NPS1")        → ASME B16.9 elbow NPS 1 fitting
          part_search("pump", domain="hydraulic") → hydraulic pump component
          part_search("resistor", domain="ecad")  → KiCad resistor symbols
        """
        try:
            from engine_oracle.parts_hub import search as _search
            results = _search(
                query,
                domain=domain or None,
                limit=max(1, min(int(limit), 200)),
            )
            has_live = any(r.get("source") == "live" for r in results)
            return json.dumps({
                "ok": True,
                "count": len(results),
                "has_live": has_live,
                "results": results,
            }, ensure_ascii=False)
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

    @mcp.tool()
    def part_place(part_id: str, doc: str = "", params: str = "") -> str:
        """Place a standard part into a document — universal insertion across all domains (T-P3).

        Looks up the PartRecord for ``part_id``, reads its geometry_ref.kind, and routes
        to the correct domain insertion path.  ALWAYS returns an editable document-schema
        op — never an opaque solid body.  Standard-before-custom: prefer part_search +
        part_place over add_features for standard hardware.

        part_id : stable part id, e.g. "fastener:ISO4762:M8", "ecad:Device:R",
                  "bearing:ISO 15:6205", "section:AISC:W8x31", "fitting:elbow:NPS1",
                  "hydraulic:pump:gear-1cc"
        doc     : JSON string of the open document that receives the placement, OR "" for
                  mechanical parts (core_family / generate_model) which create a new doc.
        params  : optional JSON dict with domain-specific placement overrides:
                  - mech:        {"length_mm": 25}
                  - ecad:        {"pos": {"x": 10, "y": 20}, "value": "1k", "ref": "R1"}
                  - structural:  {"kind": "beam", "length_mm": 3000, "material": "steel"}
                  - piping:      {"type": "elbow", "nps": 1.0}
                  - hydraulic:   {"type": "pump", "displacement_cc_rev": 2.5}

        Returns a PlacementResult dict:
          {ok, domain, doc_id, entity_id, editable: true, detail: {...}}
        or {"ok": false, "reason": ..., "hint": ...} on failure.

        Workflow:
          1. part_search("M8 socket head") → pick part_id "fastener:ISO4762:M8"
          2. part_place("fastener:ISO4762:M8") → creates editable mech part doc_id
          3. For ECAD: part_place("ecad:Device:R", doc=json.dumps(sch_doc), params='{"pos":...}')
        """
        import json as _json

        try:
            from engine_oracle.parts_hub.place import place as _place
        except Exception as exc:  # noqa: BLE001
            return _err(f"part_place import failed: {exc}",
                        "Check that engine_oracle/parts_hub/place.py is present.")

        # Parse doc and params from JSON strings (MCP tools only accept scalars).
        doc_dict: dict | None = None
        params_dict: dict | None = None
        if doc and doc.strip():
            try:
                doc_dict = _json.loads(doc)
            except Exception:  # noqa: BLE001
                return _err("doc argument is not valid JSON.",
                            "Pass the document as a JSON string: json.dumps(my_doc).")
        if params and params.strip():
            try:
                params_dict = _json.loads(params)
            except Exception:  # noqa: BLE001
                return _err("params argument is not valid JSON.",
                            "Pass params as a JSON string: '{\"pos\": {\"x\": 0, \"y\": 0}}'.")

        try:
            result = _place(part_id, doc=doc_dict, params=params_dict)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"part_place failed: {exc}",
                        "Check part_id with part_get first.")
