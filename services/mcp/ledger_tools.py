"""Ledger MCP tools — ``ledger_get``, ``ledger_note``, and ``ledger_events``.

Family: ``ledger``.  Risk classes are auto-detected from tool names:
  - ``ledger_get``    → classified as ``read``  (added to _READ_EXACT in tool_registry)
  - ``ledger_events`` → classified as ``read``  (added to _READ_EXACT in tool_registry)
  - ``ledger_note``   → classified as ``write`` (default)

Schema: contracts/advisor.md §2 (``aicad.ledger/1``).
"""
from __future__ import annotations

import json
from typing import Any


def register_ledger_tools(mcp) -> None:
    """Register ``ledger_get``, ``ledger_note``, and ``ledger_events``."""

    @mcp.tool()
    def ledger_get(
        run_id: str,
        section: str | None = None,
        budget_tokens: int = 800,
    ) -> dict:
        """Return the design ledger for *run_id*, rendered within *budget_tokens*.

        ``section`` filters to a single section (brief | requirements | decisions |
        dimensions | questions | stage_log | artifacts | criterion_results |
        features | geometry_specs | linkages | build_nodes | mfg_plans | manifest);
        omit or pass ``all``/``*`` for the full ledger render. ``budget_tokens`` controls
        the rendering budget (default 800).
        """
        from services.agent.ledger import Ledger, ledger_render
        from services.agent.ledger_vocab import (
            resolve_section as _resolve_section,
            section_error as _section_error,
        )

        ledger = Ledger.load(run_id)
        # Resolve the model's spelling to a canonical section (shared vocabulary
        # with services/agent/mission_dispatch.py — the mission-scoped shadow of
        # this tool): normalises decoration/case/separators, walks path-like
        # values ("/project/mission-123/design/specs/" → "specs" →
        # geometry_specs), then aliases and plural variants.  "all"/"*"/"full"
        # mean the whole rendered ledger.  Only an unresolvable value errors, and
        # the error carries did_you_mean plus the full accepted set.
        if section is not None and str(section).strip():
            resolved = _resolve_section(section)
            if resolved is None:
                return _section_error(section)
            section = None if resolved == "all" else resolved
        else:
            section = None
        # mega-2: models ask for section="events" (raw event log).
        if section == "events":
            return {
                "ok": True, "run_id": run_id, "section": "events",
                "data": list(ledger.events),
                "event_count": len(ledger.events),
            }
        if section:
            state = ledger.state
            if section == "manifest":
                arts = state.get("artifacts") or []
                manifests = [a for a in arts
                             if (a.get("kind") == "manifest"
                                 or a.get("id") == "manifest")]
                data = manifests[0] if manifests else None
                if isinstance(data, dict) and data.get("manifest"):
                    data = data["manifest"]
                return {"ok": True, "run_id": run_id, "section": "manifest",
                        "data": data}
            return {"ok": True, "run_id": run_id, "section": section,
                    "data": state.get(section)}
        rendered = ledger_render(ledger, budget_tokens)
        return {"ok": True, "run_id": run_id, "rendered": rendered,
                "event_count": len(ledger.events)}

    @mcp.tool()
    def ledger_events(
        run_id: str,
        kinds: list[str] | None = None,
        after_seq: int = -1,
        limit: int = 200,
    ) -> dict:
        """Return raw ledger events for *run_id*, for the GUI transparency stream.

        ``kinds`` filters to specific event kinds (e.g. ["advisory", "audit",
        "delegation", "trigger_fired"]).  Omit to return all kinds.
        ``after_seq`` returns only events with seq > after_seq (cursor for live
        polling; pass the last known seq to get only new events).
        ``limit`` caps the number of events returned (default 200).

        Each event has: seq, ts, actor, kind, payload.
        The GUI transparency panel uses this to render the live
        action stream: advisory/audit/tool events, council votes, delegation
        transitions, and auto-applied fixes.
        """
        from services.agent.ledger import Ledger

        try:
            ledger = Ledger.load(run_id)
        except Exception as exc:
            return {"ok": False, "error": f"ledger load failed: {exc}",
                    "run_id": run_id, "events": []}

        kinds_set = set(kinds) if kinds else None
        result: list[dict] = []
        for ev in ledger.events:
            seq = ev.get("seq", 0)
            if seq <= after_seq:
                continue
            if kinds_set is not None and ev.get("kind") not in kinds_set:
                continue
            result.append(ev)
            if len(result) >= limit:
                break

        total = len(ledger.events)
        last_seq = result[-1]["seq"] if result else after_seq
        return {
            "ok": True,
            "run_id": run_id,
            "events": result,
            "count": len(result),
            "total_events": total,
            "last_seq": last_seq,
        }

    @mcp.tool()
    def ledger_note(
        run_id: str,
        kind: str,
        payload: dict,
        actor: str = "foreman",
    ) -> dict:
        """Append a structured event to the design ledger for *run_id*.

        ``kind`` selects any shared ledger event kind, including typed
        ``feature``, ``geometry_spec``, ``linkage``, ``build_node``, and
        ``mfg_plan`` records in addition to the core brief/decision/dimension/
        question/requirement/stage/artifact kinds.

        ``actor`` identifies the writing role (role id, "human", or "foreman").
        Dimension and geometry-spec events require top-level provenance. Numeric
        manufacturing-plan critical rows require row-level provenance. The same
        validator is used by the mission-scoped shadow.
        """
        from services.agent.ledger import Ledger
        from services.agent.ledger_vocab import (
            kind_error as _kind_error,
            resolve_kind as _resolve_kind,
        )

        # Shared vocabulary with services/agent/mission_dispatch.py (the
        # mission-scoped shadow of this tool): normalisation + aliases + plural
        # variants resolve the spellings models actually emit (verdict,
        # design_spec, …); only an unresolvable kind is rejected, with
        # did_you_mean alongside the full valid set.
        raw_kind = (kind or "note").strip().lower() if isinstance(kind, str) else "note"
        resolved_kind = _resolve_kind(raw_kind)
        if resolved_kind is None:
            return _kind_error(kind if isinstance(kind, str) else raw_kind)
        kind = resolved_kind

        # mega-24: models (esp. leaves) send payload as a JSON string.
        if isinstance(payload, str):
            text = payload.strip()
            if not text:
                payload = {}
            else:
                try:
                    parsed = json.loads(text)
                    payload = parsed if isinstance(parsed, dict) else {"value": parsed}
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {"text": text}
        payload = dict(payload or {})
        if kind == "artifact" and raw_kind != kind:
            payload.setdefault("id", str(
                payload.get("deliverable") or payload.get("name") or raw_kind
            ))
            payload.setdefault("kind", raw_kind)

        if kind in {
            "decision", "dimension", "question", "artifact", "feature",
            "geometry_spec", "linkage", "build_node", "mfg_plan",
            "criterion_result",
        } and not payload.get("id"):
            for alt in (
                "name", "decision_id", "dim_id", "req_id", "artifact_id",
                "feature_id", "spec_id", "node_id", "plan_id", "deliverable",
                "criterion_id",
            ):
                if payload.get(alt):
                    payload["id"] = str(payload[alt])
                    break
            if not payload.get("id") and kind == "mfg_plan":
                payload["id"] = "main"
        try:
            from services.agent.ledger import validate_ledger_payload
            validate_ledger_payload(kind, payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        try:
            ledger = Ledger.load(run_id)
            event = ledger.append(actor, kind, payload)
            return {"ok": True, "seq": event["seq"], "run_id": run_id,
                    "kind": kind, "ts": event["ts"]}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": f"ledger write failed: {exc}"}
