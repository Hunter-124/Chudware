"""vision_fix diagnostic axis — detect wrong render + name the fix tool call.

Feed a render image (corpora fixture) + describe_part text, ask which is wrong
and what tool call fixes it. Grade against an expected answer set.
Providers with ``supports_vision=False`` score null (untested), never 0.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from services.agent.providers.base import image_part, text_part

_REPO = Path(__file__).resolve().parents[5]
_RENDER_A = _REPO / "corpora" / "reveng" / "bracket_fixture" / "render_000.png"
_RENDER_B = _REPO / "corpora" / "reveng" / "bracket_fixture" / "render_001.png"

# describe_part text fixtures (canned; no engine needed)
_DESCRIBE_GOOD = (
    "Part 'bracket': solid body. Features: BaseExtrude (box 80x40x8 mm), "
    "BossExtrude (cylinder d12 h20 on top face), Fillet_edges (R2 on vertical edges). "
    "Bounding box: 80 x 40 x 28 mm. No open shells."
)
_DESCRIBE_BAD_MISSING_BOSS = (
    "Part 'bracket': solid body. Features: BaseExtrude (box 80x40x8 mm), "
    "Fillet_edges (R2 on vertical edges). "
    "Bounding box: 80 x 40 x 8 mm. Missing the d12 boss on the top face."
)
_DESCRIBE_BAD_WRONG_FILLET = (
    "Part 'bracket': solid body. Features: BaseExtrude (box 80x40x8 mm), "
    "BossExtrude (cylinder d12 h20 on top face), Fillet_edges (R8 — TOO LARGE). "
    "Bounding box: 80 x 40 x 28 mm. Fillet swallowed the boss shoulder."
)

VISION_FIX_TASKS: list[dict[str, Any]] = [
    {
        "id": "vf1_missing_boss",
        "describe": _DESCRIBE_BAD_MISSING_BOSS,
        "image": str(_RENDER_A),
        "prompt": (
            "A render and describe_part text of a motor bracket are attached. "
            "The intended design has a d12 x 20 mm boss on the top face. "
            "Is the part wrong? If so, name the tool call that fixes it "
            "(e.g. add_features with an extrude boss). Reply briefly: "
            "WRONG/OK, then the tool name."
        ),
        "expected_wrong": True,
        "expected_tools": {"add_features", "edit_feature", "extrude"},
        "expected_keywords": {"boss", "extrude", "add_features", "missing"},
    },
    {
        "id": "vf2_good_part",
        "describe": _DESCRIBE_GOOD,
        "image": str(_RENDER_B),
        "prompt": (
            "A render and describe_part text of a motor bracket are attached. "
            "Intended: BaseExtrude 80x40x8, BossExtrude d12h20, Fillet R2. "
            "Is the part wrong? Reply WRONG or OK, and if wrong the fix tool."
        ),
        "expected_wrong": False,
        "expected_tools": set(),
        "expected_keywords": {"ok", "correct", "good", "fine", "matches"},
    },
    {
        "id": "vf3_fillet_too_large",
        "describe": _DESCRIBE_BAD_WRONG_FILLET,
        "image": str(_RENDER_A),
        "prompt": (
            "Render + describe_part attached. Fillet should be R2 but describe_part "
            "reports R8 swallowing the boss. What is wrong and which tool fixes it? "
            "Reply WRONG/OK + tool name."
        ),
        "expected_wrong": True,
        "expected_tools": {"edit_feature", "fillet", "undo", "add_features"},
        "expected_keywords": {"fillet", "edit_feature", "r2", "r8", "undo"},
    },
    {
        "id": "vf4_cut_vs_boss",
        "describe": (
            "Part 'bracket': Features: BaseExtrude, Cut-Extrude through top face "
            "(hole d12) where a BOSS was intended. Bounding box 80x40x8."
        ),
        "image": str(_RENDER_B),
        "prompt": (
            "The design called for a protruding boss, but describe_part shows a "
            "cut-extrude hole. Is it wrong? Which tool undoes/fixes it?"
        ),
        "expected_wrong": True,
        "expected_tools": {"undo", "edit_feature", "add_features", "delete_feature"},
        "expected_keywords": {"cut", "boss", "undo", "wrong", "hole"},
    },
    {
        "id": "vf5_mirror_missing",
        "describe": (
            "Part 'arm': Features: Base, Hole_left (d6). Intended symmetric arm "
            "with Hole_right mirrored — Hole_right is ABSENT."
        ),
        "image": str(_RENDER_A),
        "prompt": (
            "Symmetric arm should have mirrored d6 holes left+right. "
            "describe_part shows only Hole_left. Wrong? Fix tool?"
        ),
        "expected_wrong": True,
        "expected_tools": {"add_features", "mirror", "pattern_linear", "edit_feature"},
        "expected_keywords": {"mirror", "hole", "missing", "add_features", "pattern"},
    },
]


def grade_vision_fix(reply: str, task: dict[str, Any]) -> dict[str, Any]:
    """Grade a vision_fix reply against the expected answer set."""
    text = (reply or "").strip().lower()
    if not text:
        return {"passed": False, "detail": "empty reply", "reply": ""}

    expected_wrong = bool(task.get("expected_wrong"))
    # Detect WRONG vs OK stance.
    says_wrong = bool(re.search(r"\b(wrong|incorrect|missing|error|bad|broken|fail)\b", text))
    says_ok = bool(re.search(r"\b(ok|correct|good|fine|matches|looks right|no issue)\b", text)) and not says_wrong

    stance_ok = (expected_wrong and says_wrong) or ((not expected_wrong) and (says_ok or not says_wrong))
    # For expected-good, require not claiming wrong.
    if not expected_wrong:
        stance_ok = not says_wrong

    tool_hit = False
    expected_tools = set(task.get("expected_tools") or set())
    keywords = set(task.get("expected_keywords") or set())
    if expected_wrong:
        for t in expected_tools:
            if t.lower() in text:
                tool_hit = True
                break
        if not tool_hit:
            for kw in keywords:
                if kw.lower() in text:
                    tool_hit = True
                    break
        passed = stance_ok and tool_hit
        detail = (
            f"stance={'wrong' if says_wrong else 'ok/other'} "
            f"(expect wrong), tool/kw_hit={tool_hit}"
        )
    else:
        passed = stance_ok
        detail = f"stance={'wrong' if says_wrong else 'ok/other'} (expect ok)"

    return {
        "passed": bool(passed),
        "detail": detail,
        "reply": (reply or "")[:500],
        "says_wrong": says_wrong,
    }


def run_vision_fix(provider: Any, *, seed: int = 1) -> dict[str, Any]:
    """Run vision_fix tasks. Returns score null when provider lacks vision."""
    from .transport import (
        TransportExhausted,
        complete_with_retry,
        finalize_axis_result,
        transport_error_task,
    )

    _ = seed
    supports = bool(getattr(provider, "supports_vision", True))
    if not supports:
        return finalize_axis_result(
            "vision_fix",
            [],
            skipped=True,
            skip_reason="provider.supports_vision=False",
            extra={"n_tasks": len(VISION_FIX_TASKS)},
        )

    system = (
        "You are an Chudware vision reviewer. Look at the attached render and the "
        "describe_part text. Decide if the part matches the intended design. "
        "If wrong, name the tool call that would fix it (add_features, edit_feature, "
        "undo, fillet, mirror, …). Be brief."
    )
    results: list[dict[str, Any]] = []
    for task in VISION_FIX_TASKS:
        parts: list[dict] = [
            text_part(f"describe_part:\n{task['describe']}\n\n{task['prompt']}"),
        ]
        img = Path(task["image"])
        if img.is_file():
            parts.append(image_part(str(img)))
        try:
            reply, meta = complete_with_retry(
                provider,
                system,
                [{"role": "user", "content": parts}],
                max_tokens=256,
            )
        except TransportExhausted as exc:
            results.append(transport_error_task(task["id"], exc))
            continue
        except Exception as exc:  # noqa: BLE001 — non-transport provider failure
            results.append({
                "id": task["id"],
                "passed": False,
                "detail": f"provider error: {type(exc).__name__}: {exc}",
                "reply": "",
                "error": False,
            })
            continue
        graded = grade_vision_fix(str(reply or ""), task)
        graded["id"] = task["id"]
        graded["attempts"] = meta.get("attempts", 1)
        results.append(graded)

    return finalize_axis_result("vision_fix", results)
