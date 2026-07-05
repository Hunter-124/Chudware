"""Ledger MCP tools — ``ledger_get`` and ``ledger_note``.

Family: ``ledger``.  Risk classes are auto-detected from tool names:
  - ``ledger_get``  → classified as ``read``  (added to _READ_EXACT in tool_registry)
  - ``ledger_note`` → classified as ``write`` (default)

Schema: contracts/advisor.md §2 (``aicad.ledger/1``).
"""
from __future__ import annotations

from typing import Any


def register_ledger_tools(mcp) -> None:
    """Register ``ledger_get`` and ``ledger_note`` under the ``ledger`` family."""

    @mcp.tool()
    def ledger_get(
        run_id: str,
        section: str | None = None,
        budget_tokens: int = 800,
    ) -> dict:
        """Return the design ledger for *run_id*, rendered within *budget_tokens*.

        ``section`` filters to a single section (brief | requirements | decisions |
        dimensions | questions | stage_log | artifacts); omit for the full ledger.
        ``budget_tokens`` controls the rendering budget (default 800 for local-24k tier).
        """
        try:
            from services.agent.ledger import Ledger, ledger_render
        except ImportError:
            from agent.ledger import Ledger, ledger_render  # type: ignore

        ledger = Ledger.load(run_id)
        if section:
            # Return raw state section as JSON (no token budget needed)
            state = ledger.state
            allowed = {"brief", "requirements", "decisions", "dimensions",
                       "questions", "stage_log", "artifacts"}
            if section not in allowed:
                return {"ok": False, "error": f"unknown section {section!r}",
                        "allowed": sorted(allowed)}
            return {"ok": True, "run_id": run_id, "section": section,
                    "data": state.get(section)}
        rendered = ledger_render(ledger, budget_tokens)
        return {"ok": True, "run_id": run_id, "rendered": rendered,
                "event_count": len(ledger.events)}

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
                       "requirement", "stage", "artifact"}
        if kind not in VALID_KINDS:
            return {"ok": False, "error": f"unknown kind {kind!r}",
                    "valid_kinds": sorted(VALID_KINDS)}

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
