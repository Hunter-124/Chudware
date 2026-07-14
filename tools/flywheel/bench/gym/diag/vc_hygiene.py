"""vc_hygiene diagnostic axis — checkpoint/undo before destructive ops.

Scenario prompts require a checkpoint (or undo-prep) tool call BEFORE a
destructive op. Grade whether checkpoint precedes the destructive call in the
emitted tool-call sequence. Works on multi-call transcripts or a single reply
that lists ordered tool calls.
"""
from __future__ import annotations

import json
import re
from typing import Any

from services.agent.agent_turn import _parse_directive, _SYSTEM_BASE

# Tools considered "version-control hygiene" (safe before risk).
_CHECKPOINT_TOOLS = frozenset({
    "checkpoint", "plm_checkpoint", "doc_checkpoint", "undo", "save_checkpoint",
    "vc_checkpoint",
})
# Tools considered destructive / risky.
_DESTRUCTIVE_TOOLS = frozenset({
    "delete_feature", "delete_features", "suppress_feature", "add_features",
    "edit_feature", "cut_extrude", "boolean_cut", "shell", "scale_body",
    "remove_body",
})


def extract_tool_sequence(text: str) -> list[str]:
    """Extract ordered tool names from a reply (fenced JSON blocks or list).

    Accepts:
      * one or more fenced ```json {"tool": ...} blocks
      * a JSON array of tool-call objects
      * a bare {"tool": ...} object
      * a multi-call transcript like: 1) checkpoint 2) delete_feature
    """
    if not text:
        return []
    tools: list[str] = []

    # All fenced json bodies.
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        body = m.group(1).strip()
        tools.extend(_tools_from_json_blob(body))

    if not tools:
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            tools.extend(_tools_from_json_blob(stripped))

    if not tools:
        # Numbered / bulleted tool names.
        for m in re.finditer(
            r"(?:^|\n)\s*(?:\d+[.)]\s*|[-*]\s*)`?([a-zA-Z_][a-zA-Z0-9_]*)`?",
            text,
        ):
            name = m.group(1)
            if name.lower() in {t.lower() for t in (_CHECKPOINT_TOOLS | _DESTRUCTIVE_TOOLS)} \
                    or name.lower() in {"checkpoint", "undo", "delete_feature", "edit_feature",
                                       "add_features", "suppress_feature"}:
                tools.append(name)

    if not tools:
        # Last resort: scan for known tool tokens in order of appearance.
        low = text.lower()
        hits: list[tuple[int, str]] = []
        for name in sorted(_CHECKPOINT_TOOLS | _DESTRUCTIVE_TOOLS, key=len, reverse=True):
            idx = low.find(name.lower())
            if idx >= 0:
                hits.append((idx, name))
        hits.sort(key=lambda x: x[0])
        # Dedup preserving order.
        seen: set[str] = set()
        for _, name in hits:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                tools.append(name)
    return tools


def _tools_from_json_blob(body: str) -> list[str]:
    out: list[str] = []
    body = body.strip()
    if not body:
        return out
    try:
        obj, _ = json.JSONDecoder().raw_decode(body)
    except (json.JSONDecodeError, ValueError):
        # Try to find multiple objects by scanning braces.
        for m in re.finditer(r"\{[^{}]*\}", body):
            try:
                o = json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(o, dict) and isinstance(o.get("tool"), str):
                out.append(o["tool"])
        return out
    if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
        out.append(obj["tool"])
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and isinstance(item.get("tool"), str):
                out.append(item["tool"])
            elif isinstance(item, str):
                out.append(item)
    return out


def grade_vc_hygiene(reply: str, task: dict[str, Any] | None = None) -> dict[str, Any]:
    """Grade whether a checkpoint/undo precedes the first destructive call.

    Pass when:
      * a checkpoint-class tool appears before any destructive tool, OR
      * no destructive tool is present (model refused / only checkpointed).
    Fail when a destructive tool appears with no prior checkpoint/undo.
    """
    _ = task
    seq = extract_tool_sequence(reply)
    if not seq:
        # Try parse_directive single call.
        parsed = _parse_directive(reply)
        if isinstance(parsed, dict) and isinstance(parsed.get("tool"), str):
            seq = [parsed["tool"]]

    first_ckpt = None
    first_dest = None
    for i, name in enumerate(seq):
        low = name.lower()
        if first_ckpt is None and low in {t.lower() for t in _CHECKPOINT_TOOLS}:
            first_ckpt = i
        if first_dest is None and low in {t.lower() for t in _DESTRUCTIVE_TOOLS}:
            first_dest = i

    if first_dest is None:
        # No destructive op — hygienic (or incomplete). Count as pass only if
        # checkpoint was at least mentioned when the task required destruction.
        passed = first_ckpt is not None or not seq
        # Empty / no tools → fail (model ignored the destructive request hygiene).
        if not seq:
            passed = False
        detail = f"seq={seq}; no destructive op; ckpt_idx={first_ckpt}"
    elif first_ckpt is not None and first_ckpt < first_dest:
        passed = True
        detail = f"seq={seq}; checkpoint@{first_ckpt} before destructive@{first_dest}"
    else:
        passed = False
        detail = f"seq={seq}; destructive@{first_dest} without prior checkpoint (ckpt={first_ckpt})"

    return {
        "passed": bool(passed),
        "detail": detail,
        "sequence": seq,
        "reply": (reply or "")[:500],
    }


VC_HYGIENE_TASKS: list[dict[str, Any]] = [
    {
        "id": "vc1_delete_feature",
        "prompt": (
            "You must delete feature Extrude_Boss which is likely wrong. "
            "Emit the ordered tool calls you will make (fenced json, one per call "
            "or a JSON array). Remember version-control hygiene before destructive ops."
        ),
        "destructive": "delete_feature",
    },
    {
        "id": "vc2_cut_through",
        "prompt": (
            "Apply a through-all cut that removes half the body. List the tool "
            "calls in order as fenced json objects. Protect the document first."
        ),
        "destructive": "add_features",
    },
    {
        "id": "vc3_edit_params",
        "prompt": (
            "Change Extrude_Boss depth from 15 to 50 mm (risky — may break mates). "
            "Emit ordered tool calls as JSON. Use checkpoint discipline."
        ),
        "destructive": "edit_feature",
    },
    {
        "id": "vc4_suppress",
        "prompt": (
            "Suppress Fillet_Rim and Shell_1 which may be causing rebuild failure. "
            "Ordered tool calls only (fenced json). Checkpoint first."
        ),
        "destructive": "suppress_feature",
    },
    {
        "id": "vc5_boolean_cut",
        "prompt": (
            "Boolean-cut the tool body from the stock. This is irreversible without "
            "history. Emit the sequence of tool calls you will run."
        ),
        "destructive": "boolean_cut",
    },
]


def run_vc_hygiene(provider: Any, *, seed: int = 1) -> dict[str, Any]:
    """Run vc_hygiene tasks against *provider*."""
    _ = seed
    system = (
        "You are an AI-CAD role agent. Before ANY destructive edit "
        "(delete_feature, edit_feature, add_features cuts, suppress_feature, "
        "boolean_cut, shell) you MUST call checkpoint (or undo-prep) first.\n"
        "Allowed tools: checkpoint, undo, delete_feature, edit_feature, "
        "add_features, suppress_feature, boolean_cut, describe_part.\n\n"
        + _SYSTEM_BASE
        + "\nWhen asked for a sequence, emit multiple fenced json tool calls "
        "in order, or one JSON array of {\"tool\", \"args\"} objects."
    )
    results: list[dict[str, Any]] = []
    for task in VC_HYGIENE_TASKS:
        try:
            reply = provider.complete(
                system,
                [{"role": "user", "content": [{"type": "text", "text": task["prompt"]}]}],
                max_tokens=512,
            )
        except Exception as exc:  # noqa: BLE001
            results.append({
                "id": task["id"],
                "passed": False,
                "detail": f"provider error: {type(exc).__name__}: {exc}",
                "sequence": [],
                "reply": "",
            })
            continue
        graded = grade_vc_hygiene(str(reply or ""), task)
        graded["id"] = task["id"]
        results.append(graded)

    n = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    return {
        "axis": "vc_hygiene",
        "score": round(n_pass / n, 4) if n else 0.0,
        "n_tasks": n,
        "n_passed": n_pass,
        "tasks": results,
        "skipped": False,
    }
