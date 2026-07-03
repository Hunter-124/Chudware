"""
samples/structural_frame/build.py — build a simple welded steel frame.

Generates:
  samples/structural_frame/frame.aicm    (native doc)
  samples/structural_frame/thumbnail.png  (headless shot)

The part: rectangular portal frame (two uprights + top beam) made from 40×40×2 square
tube (rect_tube profile). Uses the weld_member feature with a 3-segment sketch path.

Verify contract:
  regen_and_verify() -> dict with keys: ok, volume_mm3, body_count, feature_count
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

SAMPLE_DIR = Path(__file__).parent
OUTPUT_AICM = SAMPLE_DIR / "frame.aicm"
OUTPUT_PNG  = SAMPLE_DIR / "thumbnail.png"

EXPECTED_VOLUME_MM3 = 50_000.0   # rough — 3 segments × 40×40×2 tube


def dim(v: float) -> dict:
    return {"value": float(v), "expr": None}


def build_frame(cb) -> dict:
    """
    Build a portal frame:
      - Left upright: vertical 200 mm segment
      - Right upright: vertical 200 mm segment
      - Top beam: horizontal 300 mm connecting the tops
    Profile: rect_tube 40×40×2 mm, steel.
    """
    doc_id = cb.call("doc_new", {"name": "PortalFrame"})["doc_id"]

    # ── Sketch: the 3-segment portal path ─────────────────────────────────────
    # Origin at bottom-left of left upright.
    # Left leg: (0,0)→(0,200); beam: (0,200)→(300,200); right leg: (300,200)→(300,0)
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": -1, "background": False,
        "ops": [
            {"op": "add_feature", "feature": {
                "type": "sketch", "name": "FramePath",
                "refs": {"plane": "plane.front"},
            }},
            # Left upright: vertical
            {"op": "sketch_add_entity", "sketch": "$0", "entity": {
                "type": "line",
                "p1": [0.0, 0.0], "p2": [0.0, 200.0],
            }, "constraints": []},
            # Top beam: horizontal
            {"op": "sketch_add_entity", "sketch": "$0", "entity": {
                "type": "line",
                "p1": [0.0, 200.0], "p2": [300.0, 200.0],
            }, "constraints": []},
            # Right upright: vertical (going down)
            {"op": "sketch_add_entity", "sketch": "$0", "entity": {
                "type": "line",
                "p1": [300.0, 200.0], "p2": [300.0, 0.0],
            }, "constraints": []},
        ],
    })
    rev = r["revision"]

    # Get the assigned feature id for the sketch (id_map: "$0" → "f1")
    sketch_id = r.get("id_map", {}).get("$0", "f1")

    # ── Weld member along the portal path ─────────────────────────────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": rev, "background": False,
        "ops": [
            {"op": "add_feature", "feature": {
                "type": "weld_member", "name": "FrameTube",
                "params": {
                    "profile": "rect_tube",
                    "w": dim(40), "h": dim(40), "thickness": dim(2),
                },
                "refs": {
                    "sketch": sketch_id,   # feature-id of the source sketch
                },
            }},
        ],
    })
    rev = r["revision"]

    # ── Set material (steel) ──────────────────────────────────────────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": rev, "background": False,
        "ops": [
            {"op": "set_material", "key": "steel"},
        ],
    })

    # ── Mass eval ──────────────────────────────────────────────────────────────
    mass = cb.call("eval_mass", {"doc_id": doc_id})
    volume = mass.get("volume_mm3", 0.0)
    body_count = mass.get("body_count", 1)

    # ── Save ──────────────────────────────────────────────────────────────────
    cb.call("doc_save", {"doc_id": doc_id, "path": str(OUTPUT_AICM)})
    cb.call("doc_close", {"doc_id": doc_id})

    feature_count = len(r.get("rebuild", {}).get("feature_status", []))
    return {
        "ok": True,
        "volume_mm3": volume,
        "body_count": body_count,
        "feature_count": feature_count,
        "aicm_path": str(OUTPUT_AICM),
        "rebuild": r.get("rebuild", {}),
    }


def _find_binary() -> "Path | None":
    candidates = [
        REPO / "ui" / "imgui" / "build" / "bin" / "AI-CAD2",
        REPO / "build" / "linux-app" / "bin" / "AI-CAD2",
        REPO.parent / "CAD" / "ui" / "imgui" / "build" / "bin" / "AI-CAD2",
        REPO.parent / "CAD" / "build" / "linux-app" / "bin" / "AI-CAD2",
    ]
    return next((b for b in candidates if b.exists()), None)


def make_thumbnail() -> bool:
    binary = _find_binary()
    if binary is None:
        print("  [SKIP] AI-CAD2 binary not found")
        return False
    env = os.environ.copy()
    so_candidates = [
        REPO / "core" / "build-linux" / "bin" / "libaicad.so",
        REPO.parent / "CAD" / "core" / "build-linux" / "bin" / "libaicad.so",
    ]
    so = next((s for s in so_candidates if s.exists()), None)
    if so:
        env.setdefault("AI_CAD_LIBAICAD", str(so))
    env["AI_CAD_NO_FILE_DIALOGS"] = "1"
    env["AI_CAD_OPEN_FILE"] = str(OUTPUT_AICM)
    try:
        subprocess.run(
            ["xvfb-run", "-a", str(binary), "--shot", str(OUTPUT_PNG)],
            env=env, capture_output=True, timeout=60,
        )
        if OUTPUT_PNG.exists():
            print(f"  [OK] thumbnail → {OUTPUT_PNG}")
            return True
        print("  [WARN] shot ran but no PNG written")
        return False
    except Exception as e:
        print(f"  [WARN] thumbnail failed: {e}")
        return False


def regen_and_verify() -> dict:
    from mcp_server import core_bridge as cb
    if not cb.available():
        return {"ok": False, "error": "libaicad not available"}
    result = build_frame(cb)
    vol = result["volume_mm3"]
    if vol <= 0:
        # weld_member may produce multi-body (one per segment) — accept either
        # zero volume means structural failed
        result["ok"] = False
        result["error"] = f"volume={vol} (expected > 0)"
    return result


if __name__ == "__main__":
    from mcp_server import core_bridge as cb
    if not cb.available():
        print("FATAL: libaicad not available.")
        sys.exit(1)
    print("Building structural_frame/frame sample...")
    result = build_frame(cb)
    print(f"  volume:        {result['volume_mm3']:.0f} mm³")
    print(f"  body_count:    {result['body_count']}")
    print(f"  feature_count: {result['feature_count']}")
    print(f"  aicm:          {result['aicm_path']}")
    print("Rendering thumbnail...")
    make_thumbnail()
    print("Done.")
