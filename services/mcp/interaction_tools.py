"""MCP elicitation transport for the interaction contract (contracts/interaction.md §8).

Exposes `ask_user` as an MCP tool. On an external client, each typed question is turned
into an MCP *elicitation* (`Context.elicit`, spec-stable in mcp 1.27.2) so the client
prompts its own user; the returned answers flow back through the ONE interaction core
(`services/agent/interaction.py`) for normalization, provenance, persistence and replay.

Import-safe: no elicitation happens at import time; the whole tool body is guarded so a
client/API mismatch returns an error string instead of crashing the shared server.
"""
from __future__ import annotations
import json

from typing import Any, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP

from mcp_server.cad_tools import _dumps, _err

# The interaction core (canonical import path: services.agent.*).
from services.agent.interaction import (
    FileLedger, InAppTransport, MemoryLedger, QuestionBatch, make_batch)


def _model_for(q):
    """Build a primitive-only Pydantic model for one question's elicitation schema."""
    from pydantic import create_model
    sug = q.suggested if isinstance(q.suggested, dict) else {}
    if q.kind == "dimension":
        return create_model("DimensionAnswer",
                            value=(float, sug.get("value", 0.0)),
                            unit=(str, sug.get("unit", "mm")))
    if q.kind == "choice":
        values = [o["value"] for o in (q.options or [])]
        if q.multi:
            # Primitive-only: multi-select as comma-joined values.
            default = ",".join(str(v) for v in (q.suggested or [])) if isinstance(
                q.suggested, list) else str(q.suggested or "")
            return create_model("ChoiceMultiAnswer", value=(str, default))
        typ = Literal[tuple(values)] if values else str  # type: ignore[valid-type]
        return create_model("ChoiceAnswer", value=(typ, q.suggested))
    if q.kind == "approval":
        return create_model("ApprovalAnswer", value=(bool, bool(q.suggested)))
    if q.kind == "file_request":
        return create_model("FileAnswer", paths=(str, ""))
    if q.kind == "geometry_select":
        # No viewport on pure MCP clients — free-form JSON of a selection object/array.
        default = json.dumps(q.suggested) if q.suggested is not None else ("[]" if q.multi else "{}")
        return create_model("GeometryAnswer", value=(str, default))
    return create_model("TextAnswer", value=(str, str(q.suggested or "")))


async def _elicit_raw(ctx: Context, q) -> Optional[Any]:
    """Elicit one question through the MCP client; None on decline/cancel/error."""
    try:
        res = await ctx.elicit(message=q.prompt, schema=_model_for(q))
    except Exception:  # noqa: BLE001 - a client without elicitation ⇒ core defaults
        return None
    if getattr(res, "action", None) != "accept":
        return None
    data = getattr(res, "data", None)
    if data is None:
        return None
    if q.kind == "dimension":
        return {"value": getattr(data, "value", None), "unit": getattr(data, "unit", None)}
    if q.kind == "file_request":
        paths = getattr(data, "paths", "") or ""
        return {"paths": [p.strip() for p in str(paths).split(",") if p.strip()]}
    if q.kind == "choice" and q.multi:
        raw = getattr(data, "value", None)
        if isinstance(raw, list):
            return raw
        return [p.strip() for p in str(raw or "").split(",") if p.strip()]
    if q.kind == "geometry_select":
        raw = getattr(data, "value", None)
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(str(raw))
        except (json.JSONDecodeError, TypeError):
            return None
    return getattr(data, "value", None)


def register_interaction_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    async def ask_user(questions: list, ctx: Context, scope: str = "run",
                       run_id: Optional[str] = None, doc_id: Optional[str] = None,
                       revision: Optional[int] = None, blocking: bool = True) -> str:
        """Ask the human a typed batch of <=4 questions and return machine-readable answers.

        This is the ONE interaction seam (contracts/interaction.md). Each `questions[i]` is
        `{id, kind, prompt, suggested, required?, ...}` with kind in
        choice|dimension|text|file_request|approval|geometry_select. Prefer NOT asking when a
        sensible default exists — set `required:false` + typed `suggested` and only surface
        decisions that change the build. Design decisions use kind=choice with options (set
        multi=true for multi-select). Geometry the model cannot see uses kind=geometry_select
        with targets=[face|edge|vertex|feature|body|component|point] (suggested may be null/{}
        so you do not invent sids). On an MCP client the questions surface as elicitations.
        Answers persist to the run/doc record and replay on resume — a resumed agent never
        re-asks. Returns `{id: {value, canonical?, source, at}}`.
        """
        from services.agent import interaction as core
        try:
            batch = make_batch(questions, scope=scope, run_id=run_id, doc_id=doc_id,
                              revision=revision, blocking=blocking)
            core._validate(batch)
            led = (FileLedger(batch.run_id) if batch.scope == "run" else MemoryLedger())
            raw: dict[str, Any] = {}
            for q in batch.questions:
                if led.answered(q.id) is None and blocking:
                    ans = await _elicit_raw(ctx, q)
                    if ans is not None:
                        raw[q.id] = ans
            answers = core.ask_user(batch, transport=InAppTransport(lambda b: raw),
                                    ledger=led)
            return _dumps(answers)
        except Exception as e:  # noqa: BLE001
            return _err(e)
