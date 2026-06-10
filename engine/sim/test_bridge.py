"""C19 sim-bridge tests — plain main() runner with asserts (house style).

Tests:
  1. bridge.export_step / bridge.face_points off a live doc session (box 60x40x10).
  2. sim worker subprocess: ping + doc-exported STEP -> sim_mesh -> sim_run_static with a study
     (fix one end, 100 N on the far face, steel) -> assert overlay_aicm exists and parses with
     SCLR von_mises + DISP chunks (the C19 result overlay).

Runtime budget: ~3 min (coarse mesh density).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


_REPO = Path(__file__).parent.parent.parent


# ─── test 1: bridge off a live doc session ────────────────────────────────────

def test_bridge_session():
    from engine.doc import session as docsess
    from engine.sim import bridge

    s = docsess.new_session("part", "BridgeTest")
    box_op = {"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 60, "expr": None},
                   "width": {"value": 40, "expr": None},
                   "height": {"value": 10, "expr": None}},
        "centered": True}}
    docsess.apply_ops(s, 0, [box_op])

    tmp = tempfile.TemporaryDirectory()
    step_path = str(Path(tmp.name) / "bridge_box.step")
    out = bridge.export_step(s, step_path)
    assert Path(out).exists(), f"export_step did not write {out}"
    assert Path(out).stat().st_size > 0, "exported STEP is empty"
    print(f"[test_bridge_session] export_step -> {Path(out).stat().st_size} bytes")

    # Resolve a face selector for the +X end face (centroid x ~ +30).
    result = docsess._ensure_built(s)
    from engine.doc import selectors as sel
    body = list(result.bodies)[-1]
    infos = sel.describe_faces(body.shape.wrapped)
    # pick the face whose centroid has the largest +X (one end of the bar)
    end_idx = int(np.argmax([i.centroid[0] for i in infos]))
    selector = sel.make_selector(infos[end_idx])

    fp = bridge.face_points(s, selector, max_n=200)
    pts = np.array(fp["points"], dtype=float)
    print(f"[test_bridge_session] face_points -> {len(pts)} pts, tol={fp['tol_mm']:.2f} mm")
    assert len(pts) >= 3, "face_points must sample at least 3 nodes"
    assert pts.shape[1] == 3, "points must be xyz triples"
    # all sampled points should sit on the +X end (x close to +30 within a tolerance band)
    assert np.all(pts[:, 0] > 20.0), "sampled points should lie on the +X end face"
    assert fp["tol_mm"] > 0.0

    tmp.cleanup()
    print("[test_bridge_session] PASSED")


# ─── test 2: sim worker static run + overlay packaging ────────────────────────

def test_static_overlay_subprocess():
    # Build a STEP via build123d directly (the bridge path is covered in test 1).
    tmp = tempfile.TemporaryDirectory()
    step_path = str(Path(tmp.name) / "cantilever.step")
    import build123d as bd
    bd.export_step(bd.Box(60, 20, 20), step_path)

    p = subprocess.Popen(
        [sys.executable, "-m", "engine.sim.worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=str(_REPO))

    def rpc(obj):
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        while True:
            raw = p.stdout.readline()
            if not raw:
                raise EOFError("worker closed stdout")
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                # Non-JSON noise on stdout (e.g. a library print) — log and skip.
                print(f"  [worker stdout noise] {raw[:120]}")
                continue
            if "event" in parsed:
                continue
            return parsed

    try:
        r = rpc({"id": 1, "method": "ping", "params": {}})
        assert r["ok"] and r["result"]["sim"], r
        print(f"  ping OK: {r['result']}")

        # mesh (coarse for budget)
        r = rpc({"id": 2, "method": "sim_mesh",
                 "params": {"step_path": step_path, "density": 0.3}})
        assert r["ok"], r
        mesh_path = r["result"]["mesh_path"]
        print(f"  sim_mesh OK: {r['result']['nodes']} nodes")

        # Static study: fix the -X end, 100 N -Z on the +X end. Sample node sets by reading the
        # mesh extents (the panel uses doc_face_points; here we sample directly for a self-contained
        # test) — but to exercise the contract we provide face point clouds.
        import meshio
        m = meshio.read(mesh_path)
        pts = m.points.astype(float)
        xmin, xmax = float(pts[:, 0].min()), float(pts[:, 0].max())
        fix_pts = pts[np.abs(pts[:, 0] - xmin) < 1.0].tolist()
        load_pts = pts[np.abs(pts[:, 0] - xmax) < 1.0].tolist()

        study = {
            "job_id": "test_static",
            "mesh_path": mesh_path,
            "step_path": step_path,       # drives the C19 overlay packaging
            "material": {"E_mpa": 200000, "nu": 0.29, "density_g_cm3": 7.87, "yield_mpa": 370},
            "fixtures": [{"points": fix_pts, "tol_mm": 2.0}],
            "loads": [{"faces": {"points": load_pts, "tol_mm": 2.0},
                       "type": "force", "vector": [0, 0, -100]}],
        }
        r = rpc({"id": 3, "method": "sim_run_static", "params": {"study": study}})
        assert r["ok"], r
        res = r["result"]
        print(f"  sim_run_static OK: summary={res['summary']}")
        assert "overlay_aicm" in res, "static result must carry overlay_aicm (C19)"
        ov = res["overlay_aicm"]
        assert Path(ov).exists(), f"overlay_aicm not on disk: {ov}"

        # Parse the overlay and assert the SCLR/DISP chunks landed.
        from engine.doc.tessellate import read_aicm
        parsed = read_aicm(Path(ov).read_bytes())
        assert len(parsed["bodies"]) == 1, parsed
        nverts = parsed["bodies"][0]["pos"].shape[0]
        scalar_names = {sc["name"] for sc in parsed.get("scalars", [])}
        print(f"  overlay: {nverts} surface verts, scalars={sorted(scalar_names)}, "
              f"disp_fields={len(parsed.get('displacements', []))}")
        assert "von_mises" in scalar_names, "overlay missing von_mises SCLR"
        assert "displacement_mag" in scalar_names, "overlay missing displacement_mag SCLR"
        assert "fos" in scalar_names, "overlay missing fos SCLR"
        assert len(parsed.get("displacements", [])) >= 1, "overlay missing DISP chunk"
        # Each scalar must have one value per surface vertex.
        for sc in parsed["scalars"]:
            assert len(sc["values"]) == nverts, (sc["name"], len(sc["values"]), nverts)

        r = rpc({"id": 9, "method": "shutdown", "params": {}})
        assert r["ok"], r
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=30)
    tmp.cleanup()
    print("[test_static_overlay_subprocess] PASSED")


def main():
    t0 = time.perf_counter()
    print("=" * 60)
    print("C19 SIM BRIDGE TESTS")
    print("=" * 60)
    print("\n--- Test 1: bridge.export_step / face_points (live session) ---")
    test_bridge_session()
    print("\n--- Test 2: sim worker static run + overlay packaging ---")
    test_static_overlay_subprocess()
    print(f"\nALL C19 SIM BRIDGE TESTS PASSED  ({time.perf_counter() - t0:.1f}s)")


if __name__ == "__main__":
    main()
