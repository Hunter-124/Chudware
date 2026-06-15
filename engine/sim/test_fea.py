"""FEA pack tests — plain main() runner with asserts (house style).

Tests:
  1. cantilever deflection vs Euler-Bernoulli (within 20%)
  2. reaction sanity: max Von Mises near the fixed end
  3. modal: fixed-base first frequency positive and finite
  4. worker subprocess: ping + sim_mesh + shutdown over real pipes

Runtime budget: ~3 min.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest


# ── module-level temp dir so tests can share the STEP / mesh files ──────────
_TMPDIR: tempfile.TemporaryDirectory | None = None
_STEP_PATH: str = ""
_MESH_CACHE = None  # engine.sim.mesher.MeshResult


# ── pytest entry point ──────────────────────────────────────────────────────
# These tests share module-level state (_STEP_PATH, _MESH_CACHE) that is
# created by _setup_tmpdir().  The plain main() runner below calls that setup
# explicitly, but under pytest the test_* functions are collected and run
# directly, so without this fixture _STEP_PATH stays "" and mesh_step("")
# raises ValueError("WindowsPath('.') has an empty name").  This autouse,
# module-scoped fixture runs the same setup/teardown once for the module.
@pytest.fixture(scope="module", autouse=True)
def _fea_step_fixture():
    _setup_tmpdir()
    try:
        yield
    finally:
        _teardown_tmpdir()


def _setup_tmpdir() -> None:
    global _TMPDIR, _STEP_PATH
    _TMPDIR = tempfile.TemporaryDirectory()
    _STEP_PATH = str(Path(_TMPDIR.name) / "cantilever.step")
    try:
        import build123d as bd
        bd.export_step(bd.Box(100, 10, 10), _STEP_PATH)
    except Exception as e:
        raise RuntimeError(f"build123d export failed: {e}") from e


def _teardown_tmpdir() -> None:
    global _TMPDIR
    if _TMPDIR is not None:
        _TMPDIR.cleanup()
        _TMPDIR = None


# ─── test 1 + 2: static cantilever ───────────────────────────────────────────

def test_cantilever_static() -> None:
    """Euler-Bernoulli cantilever: 100×10×10 mm steel, 100 N tip load.

    Theory: delta_tip = P L^3 / (3 E I)
      P = 100 N
      L = 100 mm
      E = 200 000 MPa
      I = b h^3 / 12 = 10 × 10^3 / 12 = 833.33 mm^4
      delta_tip = 100 * 100^3 / (3 * 200000 * 833.33) ≈ 2.0 mm

    Note: tet4 is locking-prone in bending; 20% error threshold is intentional.
    P2 enrichment in a later card will tighten this.
    """
    global _MESH_CACHE

    from engine.sim.mesher import mesh_step
    from engine.sim.fea_static import run_static, FaceSet, Load

    print("[test_cantilever_static] Meshing at density=0.5 ...")
    t0 = time.perf_counter()
    mesh = mesh_step(_STEP_PATH, density=0.5)
    _MESH_CACHE = mesh
    mesh_time = time.perf_counter() - t0
    print(f"  mesh: {mesh.n_nodes} nodes, {mesh.n_elems} tets, "
          f"char_len={mesh.char_len_mm:.2f} mm  ({mesh_time:.1f}s)")

    pts = mesh.points
    # Box from build123d centred at origin: x in [-50, 50]
    x_min = float(pts[:, 0].min())
    x_max = float(pts[:, 0].max())

    tol_fix = max(2.0, mesh.char_len_mm)
    fix_nodes = pts[np.abs(pts[:, 0] - x_min) < tol_fix]
    tip_nodes = pts[np.abs(pts[:, 0] - x_max) < tol_fix]

    print(f"  fix face: {len(fix_nodes)} nodes  tip face: {len(tip_nodes)} nodes")
    assert len(fix_nodes) >= 3, "Need at least 3 fixed nodes"
    assert len(tip_nodes) >= 3, "Need at least 3 load nodes"

    fixture = FaceSet(points=fix_nodes, tol_mm=tol_fix)
    load = Load(
        faceset=FaceSet(points=tip_nodes, tol_mm=tol_fix),
        type="force",
        vector=np.array([0.0, 0.0, -100.0]),
    )

    steel = {
        "E_mpa": 200_000.0,
        "nu": 0.29,
        "density_g_cm3": 7.87,
        "yield_mpa": 370.0,
    }

    print("[test_cantilever_static] Running static solve...")
    t1 = time.perf_counter()
    result = run_static(mesh, steel, [fixture], [load])
    solve_time = time.perf_counter() - t1

    print(f"  n_dof={result.n_dof}  solve={result.solve_time_s:.2f}s  "
          f"total_incl_assembly={solve_time:.2f}s")
    print(f"  solver notes: {result.solver_notes}")
    print(f"  summary: {result.summary}")

    # ── Euler-Bernoulli check ─────────────────────────────────────────────────
    E_theory = 200_000.0  # MPa
    L = x_max - x_min     # ≈ 100 mm
    b = 10.0; h = 10.0
    I = b * h ** 3 / 12.0
    P = 100.0
    delta_eb = P * L ** 3 / (3.0 * E_theory * I)

    max_disp = result.summary["max_disp_mm"]
    error_pct = abs(max_disp - delta_eb) / delta_eb * 100.0

    print(f"  tip deflection: FEA={max_disp:.4f} mm  EB={delta_eb:.4f} mm  "
          f"error={error_pct:.1f}%")

    assert error_pct < 20.0, (
        f"Cantilever tip deflection error {error_pct:.1f}% exceeds 20% tolerance. "
        f"FEA={max_disp:.4f} mm, EB={delta_eb:.4f} mm"
    )

    # ── Reaction sanity: max von Mises near the fixed end ────────────────────
    band = max(5.0, mesh.char_len_mm * 2)
    fixed_band_ids = np.where(np.abs(pts[:, 0] - x_min) < band)[0]
    tip_band_ids = np.where(np.abs(pts[:, 0] - x_max) < band)[0]

    vm_fixed = result.von_mises[fixed_band_ids]
    vm_tip = result.von_mises[tip_band_ids]

    max_vm_fixed = float(np.max(vm_fixed)) if len(vm_fixed) else 0.0
    max_vm_tip = float(np.max(vm_tip)) if len(vm_tip) else 0.0

    print(f"  max VM near fixed={max_vm_fixed:.2f} MPa  near tip={max_vm_tip:.2f} MPa")
    assert max_vm_fixed > max_vm_tip * 0.5, (
        f"Expected max Von Mises near fixed end ({max_vm_fixed:.2f}) "
        f"to exceed tip ({max_vm_tip:.2f})"
    )

    print("[test_cantilever_static] PASSED")


# ─── test 3: modal ────────────────────────────────────────────────────────────

def test_modal() -> None:
    """Modal: free-free runs; fixed-base first frequency positive and finite."""
    from engine.sim.mesher import mesh_step
    from engine.sim.fea_modal import run_modal
    from engine.sim.fea_static import FaceSet

    steel = {
        "E_mpa": 200_000.0,
        "nu": 0.29,
        "density_g_cm3": 7.87,
        "yield_mpa": 370.0,
    }

    # Re-use cached mesh if available; otherwise mesh at lower density for speed
    mesh = _MESH_CACHE
    if mesh is None:
        from engine.sim.mesher import mesh_step
        print("[test_modal] Meshing at density=0.3 (no cache)...")
        mesh = mesh_step(_STEP_PATH, density=0.3)
    print(f"[test_modal] mesh: {mesh.n_nodes} nodes, {mesh.n_elems} tets")

    pts = mesh.points
    x_min = float(pts[:, 0].min())
    tol_fix = max(2.0, mesh.char_len_mm)

    fix_nodes = pts[np.abs(pts[:, 0] - x_min) < tol_fix]
    fixture = FaceSet(points=fix_nodes, tol_mm=tol_fix)

    print("[test_modal] Running fixed-base modal (6 modes)...")
    t0 = time.perf_counter()
    result_fixed = run_modal(mesh, steel, [fixture], n_modes=6)
    solve_time = time.perf_counter() - t0

    print(f"  n_dof={result_fixed.n_dof}  solve={result_fixed.solve_time_s:.2f}s  "
          f"total={solve_time:.2f}s")
    print(f"  fixed-base frequencies (Hz): {[f'{f:.2f}' for f in result_fixed.freqs_hz]}")

    assert len(result_fixed.freqs_hz) >= 1, "Expected at least 1 mode"
    assert float(result_fixed.freqs_hz[0]) > 0.0, (
        f"First fixed-base frequency must be positive, got {result_fixed.freqs_hz[0]:.4f} Hz"
    )
    assert np.isfinite(result_fixed.freqs_hz[0]), "First frequency must be finite"

    # Free-free modal (no fixtures): should not crash
    print("[test_modal] Running free-free modal (6 modes)...")
    t1 = time.perf_counter()
    result_free = run_modal(mesh, steel, [], n_modes=6)
    print(f"  free-free frequencies (Hz): {[f'{f:.2f}' for f in result_free.freqs_hz]}")
    print(f"  free-free solve time: {time.perf_counter() - t1:.2f}s")

    assert len(result_free.freqs_hz) >= 1, "Free-free modal must return modes"

    print("[test_modal] PASSED")


# ─── test 4: worker subprocess ────────────────────────────────────────────────

def test_worker_subprocess() -> None:
    """Spawn sim worker over real pipes: ping + sim_mesh on cantilever STEP + shutdown."""

    print("[test_worker_subprocess] Starting sim worker subprocess...")

    p = subprocess.Popen(
        [sys.executable, "-m", "engine.sim.worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).parent.parent.parent),  # repo root
    )

    def rpc(obj: dict) -> dict:
        """Send a request and collect event/response lines until the final response."""
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        events: list[dict] = []
        while True:
            raw = p.stdout.readline()
            if not raw:
                raise EOFError("Worker closed stdout unexpectedly")
            parsed = json.loads(raw.strip())
            if "event" in parsed:
                events.append(parsed)
            else:
                return parsed

    try:
        # ping
        r = rpc({"id": 1, "method": "ping", "params": {}})
        assert r["id"] == 1 and r["ok"], f"ping failed: {r}"
        res = r["result"]
        assert res["pong"] is True, "ping result must have pong=true"
        assert res["protocol"] == 2, "protocol must be 2"
        assert res["sim"] is True, "sim must be true"
        print(f"  ping OK: {res}")

        # sim_mesh on the cantilever STEP
        r = rpc({"id": 2, "method": "sim_mesh",
                 "params": {"step_path": _STEP_PATH, "density": 0.3}})
        assert r["id"] == 2 and r["ok"], f"sim_mesh failed: {r}"
        res = r["result"]
        assert res["nodes"] > 0, "sim_mesh must return nodes"
        assert res["elems"] > 0, "sim_mesh must return elems"
        assert Path(res["mesh_path"]).exists(), "mesh_path must exist on disk"
        print(f"  sim_mesh OK: {res['nodes']} nodes, {res['elems']} elems")

        # shutdown
        r = rpc({"id": 3, "method": "shutdown", "params": {}})
        assert r["id"] == 3 and r["ok"], f"shutdown failed: {r}"
        print("  shutdown OK")

    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=30)

    print("[test_worker_subprocess] PASSED")


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    t_start = time.perf_counter()

    print("=" * 60)
    print("C08 FEA PACK TESTS")
    print("=" * 60)

    _setup_tmpdir()
    try:
        print("\n--- Test 1+2: Cantilever static FEA ---")
        test_cantilever_static()

        print("\n--- Test 3: Modal FEA ---")
        test_modal()

        print("\n--- Test 4: Worker subprocess ---")
        test_worker_subprocess()
    finally:
        _teardown_tmpdir()

    total_time = time.perf_counter() - t_start
    print(f"\nALL C08 FEA TESTS PASSED  ({total_time:.1f}s)")


if __name__ == "__main__":
    main()
