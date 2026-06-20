"""cpp_overlay_parity.py — Python-vs-C++ parity for the FEA result OVERLAY (P55b).

The full surface overlay (engine/sim/overlay.static_overlay/modal_overlay) cannot
byte-match Python end-to-end: the C++ OCCT BRepMesh surface tessellation produces a
different vertex order/count than build123d's. So this gate proves the PORTABLE,
deterministic pieces that constitute the byte CONTRACT + the mapping logic:

  (a) ENCODER byte-parity (the contract): for crafted (values, vmin, vmax) and
      (scale_hint, vectors), assert the C++ aicm::scalar_chunk / disp_chunk bytes ==
      engine.doc.tessellate.scalar_chunk / disp_chunk bytes EXACTLY. This is the
      SCLR/DISP byte-framing proof — the two ends of the host<->engine contract.

  (b) MAP value-parity: for crafted surf_xyz + vol_xyz + vol_values (scalar AND
      vector), assert C++ sim_map_to_surface == Python overlay._map_to_surface
      exactly (same nearest-node selection + values).

  (c) LOADER round-trip: produce a static_overlay AICM (when build123d is available;
      else skip) and confirm it parses back to SCLR x3 + DISP with the right counts,
      proving the host GUI will read it. (The host loader's byte agreement with this
      framing is independently proven by (a) + the test_geom_aicm round-trip.)

Run under the runtime venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_overlay_parity.py

Exit code = number of mismatched checks (0 = full parity). The encoder byte-parity
is the hard contract and is NOT weakened.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


def _hex(b: bytes) -> str:
    return b.hex()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[overlay-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[overlay-parity] libaicad ABI={core.abi()}")

    from engine.doc import tessellate as tess
    from engine.sim import overlay

    mism = 0
    checks = 0

    # ── (a) ENCODER byte-parity (the SCLR/DISP byte contract) ──────────────────
    print("[overlay-parity] (a) encoder byte-parity (SCLR/DISP)")
    scalar_cases = [
        # (body_id, name, values, vmin, vmax)
        (1, "von_mises", [0.0, 12.5, 250.0, 3.14159, 99.0], 0.0, 250.0),
        (1, "displacement_mag", [0.0, 0.001, 0.5, 1.25], 0.0, 1.25),
        (1, "fos", [15.0, 2.0, 0.0, 5.0], 0.0, 5.0),
        (7, "", [], 0.0, 0.0),                      # empty values + empty name
        (3, "mode_amplitude", [1e-7, -2.5, 1e9], -2.5, 1e9),
        (1, "neg_range", [-5.0, 0.0, 5.0], -10.0, 10.0),
    ]
    for body_id, name, vals, vmin, vmax in scalar_cases:
        py = tess.scalar_chunk(body_id, name, np.asarray(vals, dtype=np.float32),
                               vmin=vmin, vmax=vmax)
        rc, r = core.call("aicm_scalar_chunk",
                          {"body_id": body_id, "name": name, "values": list(vals),
                           "vmin": vmin, "vmax": vmax})
        checks += 1
        label = f"SCLR body={body_id} name='{name}' n={len(vals)}"
        if rc != 0 or r.get("hex", "") != _hex(py):
            print(f"  {label:<44} DIFF rc={rc} cpp={r.get('hex','')[:40]} py={_hex(py)[:40]}")
            mism += 1
        else:
            print(f"  {label:<44} ok ({len(py)} bytes)")

    disp_cases = [
        # (body_id, scale_hint, vectors flat 3*n)
        (1, 0.1234, [0.0, 0.0, 0.0, 1.0, -2.0, 3.5, 0.001, 0.002, 0.003]),
        (1, 1.0, []),                                # empty
        (2, 0.15, [1.0, 2.0, 3.0]),                  # single vertex
        (5, 2.71828, [float(i) * 0.5 for i in range(30)]),  # 10 verts
    ]
    for body_id, sh, vecs in disp_cases:
        py = tess.disp_chunk(body_id, sh, np.asarray(vecs, dtype=np.float32))
        rc, r = core.call("aicm_disp_chunk",
                          {"body_id": body_id, "scale_hint": sh, "vectors": list(vecs)})
        checks += 1
        label = f"DISP body={body_id} sh={sh} n={len(vecs)//3}"
        if rc != 0 or r.get("hex", "") != _hex(py):
            print(f"  {label:<44} DIFF rc={rc} cpp={r.get('hex','')[:40]} py={_hex(py)[:40]}")
            mism += 1
        else:
            print(f"  {label:<44} ok ({len(py)} bytes)")

    # ── (b) MAP value-parity (nearest-volume-node) ─────────────────────────────
    print("[overlay-parity] (b) map value-parity (nearest-node)")
    rng = np.random.default_rng(12345)
    map_cases = []
    # Small hand-crafted case with a deliberate tie (first-min tie-break).
    surf0 = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.5, 0.5, 0.0]], dtype=np.float64)
    vol0 = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.5, 0.5, 0.5]], dtype=np.float64)
    vals0_s = np.array([10.0, 20.0, 30.0], dtype=np.float64)
    vals0_v = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    map_cases.append(("tie-break scalar", surf0, vol0, vals0_s, 1))
    map_cases.append(("tie-break vector", surf0, vol0, vals0_v, 3))
    # Tie case: surf point equidistant to two vol nodes -> argmin picks the first.
    surf_tie = np.array([[0.5, 0.0, 0.0]], dtype=np.float64)
    vol_tie = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float64)
    map_cases.append(("exact tie scalar", surf_tie, vol_tie,
                      np.array([111.0, 222.0]), 1))
    # Larger random cases (cross the 2048 Python chunk boundary).
    for (m, n) in [(50, 40), (2050, 60), (300, 300)]:
        surf = rng.uniform(-5, 5, size=(m, 3))
        vol = rng.uniform(-5, 5, size=(n, 3))
        map_cases.append((f"rand scalar m={m} n={n}", surf, vol, rng.uniform(0, 100, size=n), 1))
        map_cases.append((f"rand vector m={m} n={n}", surf, vol, rng.uniform(-1, 1, size=(n, 3)), 3))

    for label, surf, vol, vals, dim in map_cases:
        py = overlay._map_to_surface(surf, vol, np.asarray(vals, dtype=np.float64))
        py_flat = np.asarray(py, dtype=np.float64).reshape(-1)
        rc, r = core.call("sim_map_to_surface",
                          {"surf": surf.tolist(), "vol": vol.tolist(),
                           "values": np.asarray(vals).tolist(), "dim": dim})
        checks += 1
        cpp_flat = np.asarray(r.get("mapped", []), dtype=np.float64)
        if rc != 0 or cpp_flat.shape != py_flat.shape or not np.array_equal(cpp_flat, py_flat):
            nmax = float(np.max(np.abs(cpp_flat - py_flat))) if cpp_flat.shape == py_flat.shape else float("nan")
            print(f"  {label:<26} DIFF rc={rc} shape cpp={cpp_flat.shape} py={py_flat.shape} maxabs={nmax}")
            mism += 1
        else:
            print(f"  {label:<26} ok ({py_flat.size} values, exact)")

    # ── (c) LOADER round-trip (structural readback of a real overlay) ──────────
    print("[overlay-parity] (c) static_overlay round-trip (SCLR x3 + DISP)")
    try:
        import tempfile
        from build123d import Box, export_step

        # Build a small STEP and a fake volume result, run BOTH overlays, parse back.
        with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as tf:
            step_path = tf.name
        box = Box(20, 10, 6)
        export_step(box, step_path)

        # Fake volume result on an arbitrary node cloud.
        vol_pts = np.array([[0, 0, 0], [20, 10, 6], [10, 5, 3], [5, 2, 1]], dtype=np.float64)
        vm = np.array([10.0, 200.0, 50.0, 5.0])
        dm = np.array([0.0, 1.2, 0.4, 0.05])
        dvec = np.array([[0, 0, 0], [0, 0, -1.2], [0, 0, -0.4], [0, 0, -0.05]], dtype=np.float64)
        summary = {"max_von_mises_mpa": 200.0, "max_disp_mm": 1.2}

        rc, r = core.call("sim_static_overlay", {
            "step_path": step_path,
            "points": vol_pts.tolist(),
            "von_mises": vm.tolist(),
            "disp_magnitude": dm.tolist(),
            "displacement": dvec.tolist(),
            "yield_mpa": 250.0,
            "summary": summary,
        })
        checks += 1
        ov = r.get("overlay_aicm", "")
        if rc != 0 or not ov or not Path(ov).exists():
            print(f"  static_overlay              DIFF rc={rc} resp={str(r)[:80]}")
            mism += 1
        else:
            doc = tess.read_aicm(Path(ov).read_bytes())
            scalars = doc.get("scalars", [])
            disps = doc.get("displacements", [])
            names = [s["name"] for s in scalars]
            n_surf = doc["bodies"][0]["n_verts"] if doc.get("bodies") else 0
            ok = (len(scalars) == 3 and len(disps) == 1
                  and names == ["von_mises", "displacement_mag", "fos"]
                  and all(len(s["values"]) == n_surf for s in scalars)
                  and len(disps[0]["vectors"]) == n_surf)
            # vmin/vmax + scale_hint sanity.
            ok = ok and abs(scalars[0]["vmax"] - 200.0) < 1e-3 and scalars[2]["vmax"] == 5.0
            if ok:
                print(f"  static_overlay              ok (surf_verts={n_surf}, "
                      f"SCLR={names}, DISP scale_hint={disps[0]['scale_hint']:.4g})")
            else:
                print(f"  static_overlay              DIFF scalars={names} "
                      f"n_surf={n_surf} disps={len(disps)}")
                mism += 1

        # Modal overlay round-trip.
        mode_shape = np.array([[0, 0, 0.1], [0, 0, 1.0], [0, 0, 0.5], [0, 0, 0.2]], dtype=np.float64)
        rc, r = core.call("sim_modal_overlay", {
            "step_path": step_path,
            "points": vol_pts.tolist(),
            "mode_shape": mode_shape.tolist(),
            "freq_hz": 1234.5,
        })
        checks += 1
        ov = r.get("overlay_aicm", "")
        if rc != 0 or not ov or not Path(ov).exists():
            print(f"  modal_overlay               DIFF rc={rc} resp={str(r)[:80]}")
            mism += 1
        else:
            doc = tess.read_aicm(Path(ov).read_bytes())
            scalars = doc.get("scalars", [])
            disps = doc.get("displacements", [])
            n_surf = doc["bodies"][0]["n_verts"] if doc.get("bodies") else 0
            ok = (len(scalars) == 1 and scalars[0]["name"] == "mode_amplitude"
                  and len(disps) == 1 and len(scalars[0]["values"]) == n_surf
                  and len(disps[0]["vectors"]) == n_surf)
            if ok:
                print(f"  modal_overlay               ok (surf_verts={n_surf}, "
                      f"SCLR=['mode_amplitude'], DISP scale_hint={disps[0]['scale_hint']:.4g})")
            else:
                print(f"  modal_overlay               DIFF scalars={[s['name'] for s in scalars]} "
                      f"n_surf={n_surf} disps={len(disps)}")
                mism += 1
    except ImportError as e:
        print(f"  (c) SKIPPED — build123d unavailable ({e})")

    print(f"[overlay-parity] {checks - mism}/{checks} checks pass ({mism} mismatches)")
    return mism


if __name__ == "__main__":
    sys.exit(main())
