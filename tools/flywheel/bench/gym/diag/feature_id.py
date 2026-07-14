"""feature_id diagnostic axis — name the feature to edit from screenshot+text.

Screenshot (optional) + describe_part text → name the feature. Graded by
string/set match against an expected name set. Vision-optional: text alone
is enough for grading; if image is present and provider lacks vision the
image is simply omitted (axis still scored — unlike vision_fix).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from services.agent.providers.base import image_part, text_part

_REPO = Path(__file__).resolve().parents[5]
_RENDER = _REPO / "corpora" / "reveng" / "bracket_fixture" / "render_002.png"

FEATURE_ID_TASKS: list[dict[str, Any]] = [
    {
        "id": "fi1_bore",
        "describe": (
            "Features: BasePlate (box), Bore_H7 (hole d20 through plate), "
            "BoltCircle_M4 (4 holes d4.5 PCD 34), Chamfer_top (1x45)."
        ),
        "prompt": (
            "The sliding-fit bore is 0.1 mm undersize. Which feature should be "
            "edited to fix the bore diameter? Reply with the feature name only."
        ),
        "expected": {"bore_h7", "bore", "bore_h7 (hole d20 through plate)"},
        "image": str(_RENDER),
    },
    {
        "id": "fi2_fillet",
        "describe": (
            "Features: Housing (shell), ShaftBore (d30), Fillet_Rim (R3 on outer rim), "
            "MountHoles (pattern 6x M5)."
        ),
        "prompt": (
            "The outer rim radius should be R5 not R3. Name the feature to edit."
        ),
        "expected": {"fillet_rim", "fillet", "fillet_rim (r3 on outer rim)"},
        "image": str(_RENDER),
    },
    {
        "id": "fi3_bolt_circle",
        "describe": (
            "Features: Flange, HubBore, BoltCircle_4xM6 (PCD 48, currently 4 holes), "
            "Counterbore_each."
        ),
        "prompt": (
            "Need 6 bolts on the same PCD instead of 4. Which feature do you edit?"
        ),
        "expected": {"boltcircle_4xm6", "bolt_circle", "boltcircle", "boltcircle_4xm6 (pcd 48, currently 4 holes)"},
        "image": None,
    },
    {
        "id": "fi4_extrude_depth",
        "describe": (
            "Features: Sketch1, Extrude_Boss (depth 15 mm — should be 25), "
            "Draft_sides (2 deg)."
        ),
        "prompt": "Boss height is short by 10 mm. Name the feature whose depth to change.",
        "expected": {"extrude_boss", "extrude", "extrude_boss (depth 15 mm — should be 25)"},
        "image": None,
    },
    {
        "id": "fi5_cut",
        "describe": (
            "Features: Body, Pocket_Cut (rectangular pocket 20x10x5 on top), "
            "Rib_mid, Hole_pin (d3)."
        ),
        "prompt": (
            "The rectangular pocket is too deep (cuts through). Which feature to edit?"
        ),
        "expected": {"pocket_cut", "pocket", "cut", "pocket_cut (rectangular pocket 20x10x5 on top)"},
        "image": str(_RENDER),
    },
]


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def grade_feature_id(reply: str, task: dict[str, Any]) -> dict[str, Any]:
    """Grade by normalized string / set match against expected feature names."""
    text = (reply or "").strip()
    if not text:
        return {"passed": False, "detail": "empty reply", "reply": ""}

    # Take first line / first token-ish phrase for matching.
    candidate = text.split("\n")[0].strip().strip("`\"'.,;:")
    norm = _normalize(candidate)
    expected = {_normalize(e) for e in (task.get("expected") or set())}
    # Also allow the expected token to appear anywhere in the reply.
    full_norm = _normalize(text)
    hit = any(e and (e == norm or e in full_norm or e in norm) for e in expected)
    # Soft: if any expected raw substring appears case-insensitive in reply.
    if not hit:
        low = text.lower()
        for e in task.get("expected") or []:
            token = str(e).split()[0].lower()
            if token and token in low:
                hit = True
                break
    return {
        "passed": bool(hit),
        "detail": f"reply={candidate!r} expected_any={sorted(task.get('expected') or [])}",
        "reply": text[:500],
    }


def run_feature_id(provider: Any, *, seed: int = 1) -> dict[str, Any]:
    """Run feature_id tasks against *provider*."""
    _ = seed
    supports_vision = bool(getattr(provider, "supports_vision", True))
    system = (
        "You are an AI-CAD feature identifier. Given describe_part text "
        "(and optionally a render), name the ONE feature that should be edited. "
        "Reply with the feature name only — no prose."
    )
    results: list[dict[str, Any]] = []
    for task in FEATURE_ID_TASKS:
        parts: list[dict] = [
            text_part(f"describe_part:\n{task['describe']}\n\n{task['prompt']}"),
        ]
        img_path = task.get("image")
        if img_path and supports_vision and Path(img_path).is_file():
            parts.append(image_part(str(img_path)))
        try:
            reply = provider.complete(
                system,
                [{"role": "user", "content": parts}],
                max_tokens=128,
            )
        except Exception as exc:  # noqa: BLE001
            results.append({
                "id": task["id"],
                "passed": False,
                "detail": f"provider error: {type(exc).__name__}: {exc}",
                "reply": "",
            })
            continue
        graded = grade_feature_id(str(reply or ""), task)
        graded["id"] = task["id"]
        results.append(graded)

    n = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    return {
        "axis": "feature_id",
        "score": round(n_pass / n, 4) if n else 0.0,
        "n_tasks": n,
        "n_passed": n_pass,
        "tasks": results,
        "skipped": False,
    }
