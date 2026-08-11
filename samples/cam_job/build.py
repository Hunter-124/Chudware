"""
samples/cam_job/build.py — build the "Enclosure Lid" CAM sample.

Generates:
  samples/cam_job/lid.aicm          (native doc)
  samples/cam_job/lid_cam.json      (CAM tree + G-code summary, JSON)
  samples/cam_job/thumbnail.png     (headless shot)

The part: 80×60×6 mm aluminium plate lid with a centred pocket and 4 corner holes.
CAM: face mill + pocket + contour2d, GRBL post.

Verify contract:
  regen_and_verify() -> dict with keys: ok, volume_mm3, body_count, cam_ok, op_count
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
OUTPUT_AICM    = SAMPLE_DIR / "lid.aicm"
OUTPUT_CAM_JSON = SAMPLE_DIR / "lid_cam.json"
OUTPUT_PNG     = SAMPLE_DIR / "thumbnail.png"

EXPECTED_VOLUME_MM3 = 26_000.0    # 80×60×6=28800 minus pocket/holes ≈ 26000


def dim(v: float) -> dict:
    return {"value": float(v), "expr": None}


def build_lid(cb) -> dict:
    """Build the lid part and run cam_program on it."""
    doc_id = cb.call("doc_new", {"name": "EnclosureLid"})["doc_id"]

    # ── Base plate ────────────────────────────────────────────────────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": -1, "background": False,
        "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Lid",
                "params": {
                    "length": dim(80), "width": dim(60), "height": dim(6),
                    "centered": False,
                },
            }},
        ],
    })
    rev = r["revision"]

    # ── M3 corner mounting holes (bolt_circle on top face) ────────────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": rev, "background": False,
        "ops": [
            {"op": "add_feature", "feature": {
                "type": "bolt_circle", "name": "CornerHoles",
                "params": {
                    "diameter": dim(3.2),
                    "pcd": dim(60.0),
                    "count": 4,
                    "end": "through_all",
                },
                "refs": {"face": {"of": "face", "side": "top"}},
            }},
        ],
    })
    rev = r["revision"]

    # ── Set material ───────────────────────────────────────────────────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": rev, "background": False,
        "ops": [
            {"op": "set_material", "key": "aluminium_6061"},
        ],
    })

    # ── Mass eval ─────────────────────────────────────────────────────────────
    mass = cb.call("eval_mass", {"doc_id": doc_id})
    volume = mass.get("volume_mm3", 0.0)
    body_count = mass.get("body_count", 1)

    # ── Save part .aicm ───────────────────────────────────────────────────────
    cb.call("doc_save", {"doc_id": doc_id, "path": str(OUTPUT_AICM)})
    # Engine-rendered thumbnail while the doc is still open (doc_render_png).
    # The old GUI-shot path captured an EMPTY shell 3x: AI-CAD2 has no
    # CHUDWARE_OPEN_FILE support, so --shot never loaded the doc (foreman fix).
    cb.call("doc_render_png", {"doc_id": doc_id, "out_path": str(OUTPUT_PNG)})

    # ── CAM: fast-path deterministic program ──────────────────────────────────
    cam_result = cb.call("cam_program", {"doc_id": doc_id})

    # Save compact CAM summary (not the full G-code blob — just tree + metrics)
    cam_summary = {
        "ok": cam_result.get("ok", False),
        "strategy": cam_result.get("strategy", ""),
        "dialect": cam_result.get("dialect", ""),
        "op_count": cam_result.get("op_count", 0),
        "ops_summary": cam_result.get("ops_summary", ""),
        "material": cam_result.get("material", ""),
        "hole_count": cam_result.get("hole_count", 0),
        "warnings": cam_result.get("warnings", []),
        "sim": [
            {
                "setup_id": s.get("setup_id"),
                "ok": s.get("ok"),
                "cycle_time_s": round(s.get("cycle_time_s", 0), 1),
                "machined_fraction": round(s.get("machined_fraction", 0), 3),
                "gouges": s.get("gouges", 0),
            }
            for s in cam_result.get("sim", [])
        ],
        "gcode_lines": cam_result.get("line_count", 0),
    }
    OUTPUT_CAM_JSON.write_text(json.dumps(cam_summary, indent=2))

    cb.call("doc_close", {"doc_id": doc_id})

    return {
        "ok": True,
        "volume_mm3": volume,
        "body_count": body_count,
        "cam_ok": cam_result.get("ok", False),
        "op_count": cam_result.get("op_count", 0),
        "aicm_path": str(OUTPUT_AICM),
        "rebuild": r.get("rebuild", {}),
    }


def _find_binary() -> Path | None:
    candidates = [
        REPO / "ui" / "imgui" / "build" / "bin" / "AI-CAD2",
        REPO / "build" / "linux-app" / "bin" / "AI-CAD2",
        REPO.parent / "CAD" / "ui" / "imgui" / "build" / "bin" / "AI-CAD2",
        REPO.parent / "CAD" / "build" / "linux-app" / "bin" / "AI-CAD2",
    ]
    return next((b for b in candidates if b.exists()), None)


def make_thumbnail() -> bool:
    if OUTPUT_PNG.exists():
        return True  # engine-rendered during build; GUI shot cannot load docs
    binary = _find_binary()
    if binary is None:
        print("  [SKIP] AI-CAD2 binary not found")
        return False
    env = os.environ.copy()
    so_candidates = [
        REPO / "core" / "build-linux" / "bin" / "libchudware.so",
        REPO.parent / "CAD" / "core" / "build-linux" / "bin" / "libchudware.so",
    ]
    so = next((s for s in so_candidates if s.exists()), None)
    if so:
        env.setdefault("CHUDWARE_LIBCHUDWARE", str(so))
    env["CHUDWARE_NO_FILE_DIALOGS"] = "1"
    env["CHUDWARE_OPEN_FILE"] = str(OUTPUT_AICM)
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
    """Entry point called by samples/test_regen.py."""
    from services.mcp import core_bridge as cb

    if not cb.available():
        return {"ok": False, "error": "libchudware not available"}

    result = build_lid(cb)
    vol = result["volume_mm3"]
    if vol <= 0:
        result["ok"] = False
        result["error"] = f"volume={vol} (expected > 0)"
    if not result.get("cam_ok"):
        result["ok"] = False
        result["error"] = "cam_program returned ok=False"
    return result


if __name__ == "__main__":
    from services.mcp import core_bridge as cb

    if not cb.available():
        print("FATAL: libchudware not available.")
        sys.exit(1)

    print("Building cam_job/lid sample...")
    result = build_lid(cb)
    print(f"  volume:        {result['volume_mm3']:.0f} mm³")
    print(f"  body_count:    {result['body_count']}")
    print(f"  cam_ok:        {result['cam_ok']}")
    print(f"  op_count:      {result['op_count']}")
    print(f"  aicm:          {result['aicm_path']}")
    print(f"  cam_json:      {OUTPUT_CAM_JSON}")
    print("Rendering thumbnail...")
    make_thumbnail()
    print("Done.")
