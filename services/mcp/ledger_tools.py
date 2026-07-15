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
        features | geometry_specs | linkages | build_nodes | manifest); omit or
        pass ``all``/``*`` for the full ledger render. ``budget_tokens`` controls
        the rendering budget (default 800).
        """
        try:
            from services.agent.ledger import Ledger, ledger_render
        except ImportError:
            from agent.ledger import Ledger, ledger_render  # type: ignore

        ledger = Ledger.load(run_id)
        # mega-2: models pass section="all" meaning full render, not a named key.
        if section in ("all", "*", "full", "entire", "everything"):
            section = None
        if section in ("events", "event_log", "history"):
            return {
                "ok": True, "run_id": run_id, "section": "events",
                "data": list(ledger.events),
                "event_count": len(ledger.events),
            }
        if section:
            # mega-3 residual: interfaces/results/notes model thrash.
            _SECTION_ALIASES = {
                "interfaces": "dimensions", "interface": "dimensions",
                "fits": "dimensions",
                "results": "artifacts", "result": "artifacts",
                "outcomes": "artifacts",
                "notes": "stage_log", "note": "stage_log", "log": "stage_log",
                "plan": "build_nodes",
                "acceptance": "artifacts", "acceptance_criteria": "artifacts",
                "ac": "artifacts",
                "mission_brief": "brief",
                "criterion_result": "criterion_results",
                "criterion_results": "criterion_results",
                "ac_results": "criterion_results",
                "verification": "criterion_results",
                "verifications": "criterion_results",
            }
            section = _SECTION_ALIASES.get(section, section)
            # Return raw state section as JSON (no token budget needed)
            state = ledger.state
            allowed = {"brief", "requirements", "decisions", "dimensions",
                       "questions", "stage_log", "artifacts", "criterion_results",
                       "features", "geometry_specs", "linkages", "build_nodes",
                       "manifest", "events"}
            if section not in allowed:
                return {"ok": False, "error": f"unknown section {section!r}",
                        "allowed": sorted(allowed) + ["all"],
                        "aliases": dict(_SECTION_ALIASES)}
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
        The GUI transparency panel (T-DP11) uses this to render the live
        action stream: advisory/audit/tool events, council votes, delegation
        transitions, and auto-applied fixes.
        """
        try:
            from services.agent.ledger import Ledger
        except ImportError:
            from agent.ledger import Ledger  # type: ignore

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

        ``kind`` selects the event type:
          - ``brief``       — {title, summary, success_criteria}
          - ``decision``    — {id, stage, text, rationale}
          - ``dimension``   — {id, name, value, unit, driver, provenance, tolerance?, status?, refs?}
          - ``question``    — {id, text, blocking?}
          - ``requirement`` — {req_id, text, source?, status?}
          - ``stage``       — {stage, entered?, exited?, exit_gate?}
          - ``artifact``    — {kind, id, path, stage, note?}

        ``actor`` identifies the writing role (role id, "human", or "foreman").

        Dimension events MUST include a ``provenance`` field (tool result / calc /
        table / user answer) — contract rule.
        """
        try:
            from services.agent.ledger import Ledger
        except ImportError:
            from agent.ledger import Ledger  # type: ignore

        VALID_KINDS = {"brief", "decision", "dimension", "question",
                       "requirement", "stage", "artifact", "note",
                       "feature", "geometry_spec", "linkage", "build_node",
                       "criterion_result"}
        KIND_ALIASES = {
            "verdict": "decision", "review_verdict": "decision",
            "audit": "decision", "ruling": "decision", "spec": "decision",
            "manifest_confirm": "decision", "confirm": "decision",
            "confirmation": "decision",
            "deliverables_manifest": "artifact", "manifest": "artifact",
            "report": "artifact", "battery": "artifact",
            "criterion": "criterion_result", "ac_result": "criterion_result",
            "acceptance": "criterion_result",
            "assumption": "decision",
            "verification": "artifact", "verification_result": "artifact",
        }
        raw_kind = (kind or "").strip().lower() if isinstance(kind, str) else ""
        kind = KIND_ALIASES.get(raw_kind, raw_kind)
        if kind not in VALID_KINDS:
            return {"ok": False, "error": f"unknown kind {raw_kind!r}",
                    "valid_kinds": sorted(VALID_KINDS | set(KIND_ALIASES)),
                    "aliased_to": KIND_ALIASES}

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

        if kind == "dimension" and not payload.get("provenance"):
            return {
                "ok": False,
                "error": (
                    "dimension events require a 'provenance' field "
                    "(tool result / calc / table / user answer)"
                ),
            }

        try:
            ledger = Ledger.load(run_id)
            event = ledger.append(actor, kind, payload)
            return {"ok": True, "seq": event["seq"], "run_id": run_id,
                    "kind": kind, "ts": event["ts"]}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": f"ledger write failed: {exc}"}
