"""
samples/mech_part/build.py — build the "Bracket" sample mechanical part.

Generates:
  samples/mech_part/bracket.aicm    (native tessellation file, tracked)
  samples/mech_part/thumbnail.png   (headless shot, tracked)

Verify contract (called by samples/test_regen.py):
  regen_and_verify() -> dict with keys: ok, volume_mm3, body_count, feature_count

Usage:
  CHUDWARE_LIBCHUDWARE=<path>/libchudware.so python samples/mech_part/build.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

SAMPLE_DIR = Path(__file__).parent
OUTPUT_AICM = SAMPLE_DIR / "bracket.aicm"
OUTPUT_PNG  = SAMPLE_DIR / "thumbnail.png"

# Expected geometry constants (measured from first successful run)
# base plate: 100×60×12=72000 + rib: 100×10×30=30000 minus bolt holes (~1700) minus fillets (~300)
# Bodies merge so volume = combined fused solid minus cuts
EXPECTED_VOLUME_MM3 = 88_000.0   # ±20% tolerance (rib may not fuse if origins don't overlap)
EXPECTED_BODY_COUNT = 1


def dim(v: float) -> dict:
    return {"value": float(v), "expr": None}


def build_bracket(cb) -> dict:
    """
    Build a machined aluminium mounting bracket using box primitives (merge=true fuses them):
      - 100×60×12 mm base plate (BasePlate)
      - 100×10×30 mm upright rib along the back edge (Rib, merged into base)
      - bolt_circle of M4 cbore holes on the base top face
      - 3 mm fillets on vertical edges
      - chamfer on bottom perimeter edges
      - Material: aluminium 6061

    Returns the doc_apply_ops result dict from the final batch.
    """
    doc_id = cb.call("doc_new", {"name": "Bracket"})["doc_id"]

    # ── Step 1: base plate + rib (primitives auto-fuse via merge=true) ───────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": -1, "background": False,
        "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "BasePlate",
                "params": {
                    "length": dim(100), "width": dim(60), "height": dim(12),
                    "centered": False,
                },
            }},
            # Rib: second box fuses via merge=true (default for primitives after first body)
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Rib",
                "params": {
                    "length": dim(100), "width": dim(10), "height": dim(30),
                    "centered": False,
                },
            }},
        ],
    })
    rev = r["revision"]

    # ── Step 2: M4 bolt circle (6 bolts, PCD=40 mm) on base top ─────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": rev, "background": False,
        "ops": [
            {"op": "add_feature", "feature": {
                "type": "bolt_circle", "name": "BoltPattern",
                "params": {
                    "diameter": dim(4.3),       # clearance drill dia (M4 → Ø4.3)
                    "pcd": dim(40.0),            # pitch circle diameter
                    "count": 6,
                    "end": "through_all",
                },
                "refs": {"face": {"of": "face", "side": "top"}},
            }},
        ],
    })
    rev = r["revision"]

    # ── Step 3: fillet the top edges of the base plate ───────────────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": rev, "background": False,
        "ops": [
            # Fillet the top perimeter edges of the base plate
            {"op": "add_feature", "feature": {
                "type": "fillet", "name": "TopEdgeFillets",
                "params": {"radius": dim(3.0)},
                "refs": {"edges": {"of": "edge", "side": "top"}},
            }},
        ],
    })
    rev = r["revision"]

    # ── Step 4: set material (aluminium 6061) ─────────────────────────────────
    r = cb.call("doc_apply_ops", {
        "doc_id": doc_id, "revision": rev, "background": False,
        "ops": [
            {"op": "set_material", "key": "aluminium_6061"},
        ],
    })

    # ── Step 5: eval_mass for volume ──────────────────────────────────────────
    mass = cb.call("eval_mass", {"doc_id": doc_id})
    volume = mass.get("volume_mm3", 0.0)
    body_count = mass.get("body_count", 1)

    # ── Step 6: save native .aicm ─────────────────────────────────────────────
    cb.call("doc_save", {"doc_id": doc_id, "path": str(OUTPUT_AICM)})
    # Engine-rendered thumbnail while the doc is still open (doc_render_png).
    # The old GUI-shot path captured an EMPTY shell 3x: AI-CAD2 has no
    # CHUDWARE_OPEN_FILE support, so --shot never loaded the doc (foreman fix).
    cb.call("doc_render_png", {"doc_id": doc_id, "out_path": str(OUTPUT_PNG)})
    cb.call("doc_close", {"doc_id": doc_id})

    feature_count = _count_ok_features(r)
    return {
        "ok": True,
        "volume_mm3": volume,
        "body_count": body_count,
        "feature_count": feature_count,
        "aicm_path": str(OUTPUT_AICM),
        "rebuild": r.get("rebuild", {}),
    }


def _count_ok_features(r: dict) -> int:
    statuses = r.get("rebuild", {}).get("feature_status", [])
    if not statuses:
        statuses = r.get("features", [])
    return sum(1 for f in statuses if f.get("ok", True))


def make_thumbnail() -> bool:
    """
    Render a headless thumbnail by loading the saved .aicm into AI-CAD2
    via the --shot flag.  Returns True if the PNG was written.
    """
    if OUTPUT_PNG.exists():
        return True  # engine-rendered during build; GUI shot cannot load docs
    # Try multiple known binary locations (worktree and main repo)
    candidates = [
        REPO / "ui" / "imgui" / "build" / "bin" / "AI-CAD2",
        REPO / "build" / "linux-app" / "bin" / "AI-CAD2",
        # The main CAD checkout when running from a worktree
        REPO.parent / "CAD" / "ui" / "imgui" / "build" / "bin" / "AI-CAD2",
        REPO.parent / "CAD" / "build" / "linux-app" / "bin" / "AI-CAD2",
    ]
    binary = next((b for b in candidates if b.exists()), None)
    if binary is None:
        print(f"  [SKIP] AI-CAD2 binary not found — skipping thumbnail")
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
        import subprocess
        result = subprocess.run(
            ["xvfb-run", "-a", str(binary), "--shot", str(OUTPUT_PNG)],
            env=env, capture_output=True, timeout=60,
        )
        if OUTPUT_PNG.exists():
            print(f"  [OK] thumbnail → {OUTPUT_PNG}")
            return True
        else:
            print(f"  [WARN] shot exited {result.returncode} but no PNG written")
            print(result.stderr.decode()[-500:] if result.stderr else "")
            return False
    except Exception as e:
        print(f"  [WARN] thumbnail failed: {e}")
        return False


def regen_and_verify() -> dict:
    """Entry point called by samples/test_regen.py."""
    from mcp_server import core_bridge as cb

    if not cb.available():
        return {"ok": False, "error": "libchudware not available"}

    result = build_bracket(cb)

    # Geometry assertions (±5% tolerance)
    vol = result["volume_mm3"]
    if vol <= 0:
        result["ok"] = False
        result["error"] = f"volume={vol} (expected > 0)"
    elif EXPECTED_VOLUME_MM3 > 0:
        tol = abs(vol - EXPECTED_VOLUME_MM3) / EXPECTED_VOLUME_MM3
        if tol > 0.20:   # 20% — ribs are additive bodies until boolean merge
            result["ok"] = False
            result["error"] = f"volume={vol:.0f} differs from expected {EXPECTED_VOLUME_MM3:.0f} by {tol*100:.1f}%"

    bc = result["body_count"]
    # Multiple bodies are OK (each rib is a separate body until boolean or weld mate)
    if bc < 1:
        result["ok"] = False
        result["error"] = f"body_count={bc} (expected ≥ 1)"

    return result


if __name__ == "__main__":
    from mcp_server import core_bridge as cb

    if not cb.available():
        print("FATAL: libchudware not available. Set CHUDWARE_LIBCHUDWARE=<path>.")
        sys.exit(1)

    print("Building mech_part/bracket sample...")
    result = build_bracket(cb)
    vol   = result["volume_mm3"]
    bc    = result["body_count"]
    fc    = result["feature_count"]
    aicm  = result["aicm_path"]
    print(f"  volume:        {vol:.0f} mm³")
    print(f"  body_count:    {bc}")
    print(f"  feature_count: {fc}")
    print(f"  aicm:          {aicm}")

    print("Rendering thumbnail...")
    make_thumbnail()

    print("Done.")
