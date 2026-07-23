"""tool_validity diagnostic axis — fenced-JSON tool-call schema compliance.

Replay N canned prompts through ``provider.complete`` with the agent_turn
fenced-JSON tool schema and score parseable + schema-valid tool calls.
Deterministic graders; fixed seeds; honest failures.
"""
from __future__ import annotations

import json
from typing import Any

from services.agent.agent_turn import _parse_directive, _SYSTEM_BASE

# Allowed tools referenced by the canned prompts (must appear in args schema).
_ALLOWED = ("describe_part", "add_features", "measure_part", "undo", "checkpoint",
            "render_part", "edit_feature")

# Schema: a valid tool directive is {"tool": str, "args": dict}.
# A valid done directive is {"done": true, "handoff": dict}.
def is_schema_valid_tool_call(obj: dict | None) -> bool:
    """True when *obj* is a parseable, schema-valid tool call or done handoff."""
    if not isinstance(obj, dict):
        return False
    if obj.get("done") is True:
        handoff = obj.get("handoff")
        return handoff is None or isinstance(handoff, dict)
    tool = obj.get("tool")
    args = obj.get("args")
    if not isinstance(tool, str) or not tool.strip():
        return False
    if args is None:
        return True  # bare tool name is accepted (args default {})
    return isinstance(args, dict)


# ~5 fixed tasks: (id, user prompt, expected_tool or None for free-form validity)
TOOL_VALIDITY_TASKS: list[dict[str, Any]] = [
    {
        "id": "tv1_describe",
        "prompt": (
            "Inspect the current part. Call describe_part with no extra args. "
            "Reply with exactly one fenced json tool call."
        ),
        "expected_tool": "describe_part",
    },
    {
        "id": "tv2_measure",
        "prompt": (
            "Measure the bore diameter. Call measure_part with args "
            "{\"query\": \"bore diameter\"}. One fenced json tool call only."
        ),
        "expected_tool": "measure_part",
    },
    {
        "id": "tv3_add_features",
        "prompt": (
            "Add a 20 mm cylinder boss. Call add_features with args "
            "{\"ops\": [{\"op\": \"extrude\", \"depth\": 20}]}. One fenced json."
        ),
        "expected_tool": "add_features",
    },
    {
        "id": "tv4_done_handoff",
        "prompt": (
            "The node's goal is already met. Finish with a done handoff: "
            "{\"done\": true, \"handoff\": {\"summary\": \"ok\"}}."
        ),
        "expected_tool": None,  # done handoff, not a tool
        "expect_done": True,
    },
    {
        "id": "tv5_nested_args",
        "prompt": (
            "Edit feature F2. Call edit_feature with nested args "
            "{\"feature_id\": \"F2\", \"params\": {\"depth\": 12.5, \"draft\": 2}}. "
            "One fenced json tool call."
        ),
        "expected_tool": "edit_feature",
    },
]


def grade_tool_validity(reply: str, task: dict[str, Any]) -> dict[str, Any]:
    """Grade a single model reply for tool-call validity.

    Returns ``{passed: bool, parseable: bool, schema_valid: bool, detail: str, parsed: ...}``.
    """
    parsed = _parse_directive(reply)
    parseable = parsed is not None
    schema_valid = is_schema_valid_tool_call(parsed)
    detail_parts: list[str] = []

    if not parseable:
        detail_parts.append("unparseable: no fenced/bare JSON directive")
        return {
            "passed": False,
            "parseable": False,
            "schema_valid": False,
            "detail": "; ".join(detail_parts),
            "parsed": None,
        }

    if not schema_valid:
        detail_parts.append(f"schema-invalid: {json.dumps(parsed, default=str)[:200]}")
        return {
            "passed": False,
            "parseable": True,
            "schema_valid": False,
            "detail": "; ".join(detail_parts),
            "parsed": parsed,
        }

    if task.get("expect_done"):
        ok = bool(parsed.get("done") is True)
        if not ok:
            detail_parts.append("expected done=true handoff")
        return {
            "passed": ok,
            "parseable": True,
            "schema_valid": True,
            "detail": "; ".join(detail_parts) or "done handoff ok",
            "parsed": parsed,
        }

    expected = task.get("expected_tool")
    if expected:
        got = parsed.get("tool")
        ok = got == expected
        if not ok:
            detail_parts.append(f"expected tool={expected!r}, got={got!r}")
        return {
            "passed": ok,
            "parseable": True,
            "schema_valid": True,
            "detail": "; ".join(detail_parts) or f"tool {expected} ok",
            "parsed": parsed,
        }

    return {
        "passed": True,
        "parseable": True,
        "schema_valid": True,
        "detail": "schema-valid tool call",
        "parsed": parsed,
    }


def run_tool_validity(provider: Any, *, seed: int = 1) -> dict[str, Any]:
    """Run all tool_validity tasks against *provider*. Returns axis result dict."""
    from .transport import (
        TransportExhausted,
        complete_with_retry,
        finalize_axis_result,
        transport_error_task,
    )

    system = (
        "You are an Chudware role agent. Allowed tools: "
        + ", ".join(_ALLOWED)
        + ".\n\n"
        + _SYSTEM_BASE
    )
    results: list[dict[str, Any]] = []
    # Fixed seed is recorded for reproducibility; tasks themselves are deterministic.
    _ = seed
    for task in TOOL_VALIDITY_TASKS:
        try:
            reply, meta = complete_with_retry(
                provider,
                system,
                [{"role": "user", "content": [{"type": "text", "text": task["prompt"]}]}],
                max_tokens=512,
            )
        except TransportExhausted as exc:
            results.append(transport_error_task(
                task["id"],
                exc,
                extra={"parseable": False, "schema_valid": False},
            ))
            continue
        except Exception as exc:  # noqa: BLE001 — non-transport provider failure
            results.append({
                "id": task["id"],
                "passed": False,
                "parseable": False,
                "schema_valid": False,
                "detail": f"provider error: {type(exc).__name__}: {exc}",
                "reply": "",
                "error": False,
            })
            continue
        graded = grade_tool_validity(str(reply or ""), task)
        graded["id"] = task["id"]
        graded["reply"] = str(reply or "")[:500]
        graded["attempts"] = meta.get("attempts", 1)
        results.append(graded)

    n_parseable = sum(1 for r in results if r.get("parseable") and not r.get("error"))
    n_schema = sum(1 for r in results if r.get("schema_valid") and not r.get("error"))
    return finalize_axis_result(
        "tool_validity",
        results,
        extra={"n_parseable": n_parseable, "n_schema_valid": n_schema},
    )
