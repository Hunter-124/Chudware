"""Tests for services.piping.cfd_handoff (3D CFD handoff for pipe internals).

Test groups:
  1. internal_volume — bore solid extraction (kernel-skipped when build123d absent).
  2. cfd_boundary_from_solve — pure-Python BC translation (always runs).
  3. verify_pressure_drop — pure-Python DAG verify node (always runs).
  4. MCP tool smoke test (piping_internal_volume, piping_cfd_handoff, piping_verify_cfd).

Run from repo root:
    python -m services.piping.test_cfd_handoff
    # or via pytest:
    python -m pytest services/piping/test_cfd_handoff.py -q
"""

from __future__ import annotations

import json
import os
import tempfile

try:
    import pytest
except Exception:  # noqa: BLE001
    pytest = None

from services.piping import catalog, schema
from services.piping import cfd_handoff as ch

# ── skip markers ──────────────────────────────────────────────────────────────

def _have_kernel() -> bool:
    """Evaluate lazily each time — not at module load.

    Root cause: module-load-time HAVE_KERNEL=False (build123d absent at
    collection) but call-time _have_kernel() returns True (build123d appears in
    sys.modules from a prior import in the combined run), causing
    test_internal_volume_no_kernel_raises_piping_cfd_error to run but not raise.
    Using importlib.util.find_spec avoids sys.modules state entirely.
    """
    if not catalog.have_fluids():
        return False
    import importlib.util
    return importlib.util.find_spec("build123d") is not None


# _needs_kernel decorator evaluates _have_kernel() at decoration time (module load).
# This is acceptable for skip decorators: if the kernel becomes available between
# collection and execution in CI the test will simply run rather than skip — safe.
# The key fix (RC#3) is that test_internal_volume_no_kernel_raises_piping_cfd_error
# now calls _have_kernel() lazily inside the test body below.
_needs_kernel = (
    pytest.mark.skipif(not _have_kernel(), reason="build123d/fluids kernel not installed")
    if pytest else (lambda f: f)
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _simple_doc() -> dict:
    """A minimal two-segment pipe run: L-shaped, 2-inch schedule 40 steel."""
    doc = schema.new_piping("test-skid")
    doc["runs"].append(schema.new_pipe_run(
        "r1",
        path=[[0, 0, 0], [500, 0, 0], [500, 300, 0]],
        nps=2,
        schedule="40",
    ))
    return doc


def _mock_solve_result() -> dict:
    """A minimal solve result mimicking piping_solve output for a single-link network."""
    return {
        "ok": True,
        "converged": True,
        "iterations": 5,
        "max_imbalance_m3h": 0.0,
        "fluid": {"name": "water", "rho": 998.2, "mu": 1.002e-3},
        "nodes": [
            {"id": "n0", "xyz": [0, 0, 0], "fixed": True,
             "head_m": 30.0, "pressure_kpa": 300.0, "demand_m3h": 0.0},
            {"id": "n1", "xyz": [500, 300, 0], "fixed": False,
             "head_m": 28.0, "pressure_kpa": 280.0, "demand_m3h": 5.0},
        ],
        "links": [
            {"id": "r1", "from": "n0", "to": "n1",
             "length_mm": 800.0, "id_mm": 52.5,
             "flow_m3h": 5.0, "velocity_ms": 0.64, "reynolds": 33000.0,
             "friction_factor": 0.023, "headloss_m": 2.0, "dp_kpa": 19.6,
             "k_total": 0.3, "pump_head_m": 0.0},
        ],
        "warnings": [],
        "hint": None,
        "next_actions": ["call piping_report for the per-segment K-factor pressure-drop table"],
    }


def _mock_cfd_result(drag_n: float = 0.025, area_mm2: float = 2165.0,
                     velocity_ms: float = 0.64) -> dict:
    """A minimal run_cfd_flow-style result for a pipe bore CFD run."""
    return {
        "ok": True,
        "drag_n": drag_n,
        "frontal_area_mm2": area_mm2,
        "cd": 0.15,
        "velocity_ms": velocity_ms,
        "aoa_deg": 0.0,
        "fluid": "water",
        "converged": True,
        "residual": 0.01,
    }


# ── group 1: internal_volume (kernel-gated) ───────────────────────────────────

@_needs_kernel
def test_internal_volume_produces_step():
    """internal_volume extracts a watertight bore solid and writes a STEP file."""
    doc = _simple_doc()
    tmp_dir = tempfile.mkdtemp(prefix="piping_bore_test_")
    out = os.path.join(tmp_dir, "bore.step")
    res = ch.internal_volume(doc, out)

    assert res.get("ok"), f"internal_volume failed: {res}"
    assert os.path.isfile(res["step_path"]), "STEP file not written"
    assert os.path.getsize(res["step_path"]) > 0, "STEP file is empty"
    assert res["volume_mm3"] > 0, f"volume_mm3 should be positive, got {res['volume_mm3']}"
    assert "r1" in res["run_ids"], f"run r1 missing from run_ids: {res['run_ids']}"
    assert res["inlet_xyz"] is not None and len(res["inlet_xyz"]) == 3
    assert res["outlet_xyz"] is not None and len(res["outlet_xyz"]) == 3


@_needs_kernel
def test_internal_volume_single_run_filter():
    """run_id filter restricts extraction to the named run."""
    doc = schema.new_piping("test-filter")
    doc["runs"].append(schema.new_pipe_run(
        "rA", path=[[0, 0, 0], [300, 0, 0]], nps=2, schedule="40"))
    doc["runs"].append(schema.new_pipe_run(
        "rB", path=[[300, 0, 0], [300, 300, 0]], nps=1.5, schedule="40"))
    tmp_dir = tempfile.mkdtemp(prefix="piping_bore_filter_")
    res = ch.internal_volume(doc, os.path.join(tmp_dir, "bore_a.step"), run_id="rA")
    assert res.get("ok"), res
    assert res["run_ids"] == ["rA"], f"expected only rA, got {res['run_ids']}"


@_needs_kernel
def test_internal_volume_unknown_run_id():
    """Unknown run_id returns a soft-fail dict (never raises)."""
    doc = _simple_doc()
    tmp_dir = tempfile.mkdtemp(prefix="piping_bore_unk_")
    res = ch.internal_volume(doc, os.path.join(tmp_dir, "bore.step"), run_id="no_such")
    assert res.get("ok") is False
    assert "run_id" in res.get("reason", "").lower() or "not found" in res.get("reason", "")


@_needs_kernel
def test_end_elbow_keeps_full_lr_radius():
    """A routed run ends one LR take-out past its last corner (piping_route's nozzle stub):
    that elbow — and every other — is swept at the full bend radius, in both the bore wire
    (CFD fluid volume) and the pipe wire (3-D model). An end segment may be consumed whole;
    a segment shared by two bends is split half each."""
    from services.piping import threed
    R = 76.2  # NPS 2 LR take-out = bend radius for a 90° elbow
    pts = [[0, 0, 0], [300, 0, 0], [300, 0, -180], [600, 0, -180], [600, 0, 0],
           [600 + R, 0, 0]]
    for build in (ch._bore_wire, threed._path_wire):
        wire = build(pts, R)
        arcs = [e for e in wire.edges() if str(e.geom_type).upper().endswith("CIRCLE")]
        assert len(arcs) == 4, f"{build.__name__}: expected 4 bends, got {len(arcs)}"
        for e in arcs:
            assert abs(e.radius - R) < 1e-6, f"{build.__name__}: bend radius {e.radius} != {R}"
        end = wire.edges()[-1] @ 1
        assert abs(end.X - (600 + R)) < 1e-6 and abs(end.Z) < 1e-6, (build.__name__, end)


def test_internal_volume_no_kernel_raises_piping_cfd_error():
    """When the kernel is absent, internal_volume raises PipingCFDError (not bare exception).

    This test only runs when build123d is absent; if the kernel IS present it is skipped
    (we can't uninstall build123d in a live session).

    Fix: call _have_kernel() lazily here (not the module-level
    constant) so combined-run sys.modules state doesn't cause a mismatch between
    the skip decision and what internal_volume() actually does at call time.
    """
    if _have_kernel():
        if pytest:
            pytest.skip("kernel present — cannot test absent-kernel path")
        return

    doc = _simple_doc()
    raised = False
    try:
        with tempfile.TemporaryDirectory() as d:
            ch.internal_volume(doc, os.path.join(d, "bore.step"))
    except ch.PipingCFDError:
        raised = True
    assert raised, "expected PipingCFDError when kernel absent"


# ── group 2: cfd_boundary_from_solve ─────────────────────────────────────────

def test_cfd_bc_extracts_inlet_velocity():
    """cfd_boundary_from_solve extracts inlet velocity and pressure from solve result."""
    sr = _mock_solve_result()
    bc = ch.cfd_boundary_from_solve(sr)
    assert bc.get("ok"), bc
    assert bc["inlet_velocity_ms"] > 0, bc
    assert bc["inlet_pressure_kpa"] > bc["outlet_pressure_kpa"], \
        "inlet pressure should exceed outlet"
    assert bc["pressure_drop_1d_kpa"] > 0, bc
    assert bc["ref_diameter_mm"] > 0, bc


def test_cfd_bc_with_explicit_inlet_run_id():
    """When inlet_run_id is given, that link is used."""
    sr = _mock_solve_result()
    bc = ch.cfd_boundary_from_solve(sr, inlet_run_id="r1")
    assert bc.get("ok"), bc
    assert bc["inlet_link"] == "r1"


def test_cfd_bc_unknown_run_id_soft_fail():
    sr = _mock_solve_result()
    bc = ch.cfd_boundary_from_solve(sr, inlet_run_id="no_such")
    assert bc.get("ok") is False
    assert "not found" in bc.get("reason", "")


def test_cfd_bc_failed_solve_soft_fail():
    """A failed solve result returns a soft-fail dict."""
    sr = {"ok": False, "reason": "disconnected network"}
    bc = ch.cfd_boundary_from_solve(sr)
    assert bc.get("ok") is False


def test_cfd_bc_empty_links_soft_fail():
    sr = {"ok": True, "links": [], "nodes": [
        {"id": "n0", "xyz": [0, 0, 0], "pressure_kpa": 300.0}
    ], "fluid": {"rho": 998.2, "mu": 1.002e-3}}
    bc = ch.cfd_boundary_from_solve(sr)
    assert bc.get("ok") is False
    assert "links" in bc.get("reason", "")


# ── group 3: verify_pressure_drop ─────────────────────────────────────────────

def test_verify_pass_within_tolerance():
    """When CFD and 1D pressures agree within 20%, verdict is PASS."""
    sr = _mock_solve_result()
    # 1D dp from mock = 19.6 kPa; CFD: drag_n / area → match within 20%
    # craft matching CFD: dP = 19.6 kPa => F = 19.6e3 Pa * 2165e-6 m² ≈ 0.0424 N
    area_mm2 = 2165.0  # π * (52.5/2)² ≈ 2165 mm²
    target_dp_pa = 19600.0
    drag_n = target_dp_pa * area_mm2 * 1e-6  # N
    cr = _mock_cfd_result(drag_n=drag_n, area_mm2=area_mm2)
    res = ch.verify_pressure_drop(sr, cr, tolerance_pct=20.0)
    assert res.get("ok"), res
    assert res["pass"], f"expected PASS: {res['verdict']}"
    assert res["deviation_pct"] < 20.0
    assert res["dp_1d_kpa"] == pytest.approx(19.6, abs=0.1) if pytest else True


def test_verify_fail_outside_tolerance():
    """When CFD dp is far from 1D, verdict is FAIL."""
    sr = _mock_solve_result()
    # CFD result gives wildly different pressure drop
    cr = _mock_cfd_result(drag_n=10.0, area_mm2=2165.0)  # ~4.6 MPa — way off
    res = ch.verify_pressure_drop(sr, cr, tolerance_pct=20.0)
    assert res.get("ok"), res
    assert not res["pass"], f"expected FAIL: {res['verdict']}"
    assert res["deviation_pct"] > 20.0


def test_verify_velocity_limit_flagged():
    """Links exceeding 3 m/s are flagged in velocity_limits.violations."""
    sr = dict(_mock_solve_result())
    sr["links"] = [
        {**sr["links"][0], "velocity_ms": 5.0}
    ]
    cr = _mock_cfd_result()
    res = ch.verify_pressure_drop(sr, cr)
    assert res.get("ok"), res
    assert len(res["velocity_limits"]["violations"]) == 1
    assert res["velocity_limits"]["violations"][0]["velocity_ms"] == 5.0


def test_verify_failed_solve_soft_fail():
    sr = {"ok": False, "reason": "not converged"}
    cr = _mock_cfd_result()
    res = ch.verify_pressure_drop(sr, cr)
    assert res.get("ok") is False


def test_verify_failed_cfd_soft_fail():
    sr = _mock_solve_result()
    cr = {"ok": False, "reason": "LBM diverged"}
    res = ch.verify_pressure_drop(sr, cr)
    assert res.get("ok") is False


def test_verify_specific_run_id():
    """When run_id is given, only that link's dp_kpa is used as the 1D budget."""
    sr = _mock_solve_result()
    area_mm2 = 2165.0
    dp_pa = 19600.0
    drag_n = dp_pa * area_mm2 * 1e-6
    cr = _mock_cfd_result(drag_n=drag_n, area_mm2=area_mm2)
    res = ch.verify_pressure_drop(sr, cr, run_id="r1")
    assert res.get("ok"), res
    assert res["dp_1d_kpa"] == pytest.approx(19.6, abs=0.1) if pytest else True


def test_verify_unknown_run_id_soft_fail():
    sr = _mock_solve_result()
    cr = _mock_cfd_result()
    res = ch.verify_pressure_drop(sr, cr, run_id="no_such")
    assert res.get("ok") is False


def test_verify_cd_fallback_when_no_area():
    """When frontal_area_mm2 is absent, falls back to Cd × ½ρV²."""
    sr = _mock_solve_result()
    cr = {"ok": True, "cd": 0.5, "velocity_ms": 0.64}  # no drag_n / area
    res = ch.verify_pressure_drop(sr, cr)
    assert res.get("ok"), res
    assert res["dp_3d_kpa"] > 0, "should derive dp from Cd fallback"
    assert any("Cd" in w or "estimated" in w for w in res.get("warnings", [])), \
        "warning about Cd fallback expected"


# ── group 4: MCP tool smoke tests ─────────────────────────────────────────────

def _make_mcp_stub():
    """Minimal stub MCP to exercise register_piping_tools without a real MCP server."""
    tools: dict = {}

    class _MCP:
        def tool(self):
            def _deco(f):
                tools[f.__name__] = f
                return f
            return _deco

    return _MCP(), tools


def test_mcp_tools_register():
    """All five piping MCP tools register without error."""
    from services.mcp.piping_tools import register_piping_tools
    mcp, tools = _make_mcp_stub()
    register_piping_tools(mcp)
    for name in ("piping_solve", "piping_report",
                 "piping_internal_volume", "piping_cfd_handoff", "piping_verify_cfd"):
        assert name in tools, f"tool {name!r} not registered"


def test_mcp_piping_internal_volume_bad_doc():
    """piping_internal_volume soft-fails on bad input."""
    from services.mcp.piping_tools import register_piping_tools
    mcp, tools = _make_mcp_stub()
    register_piping_tools(mcp)
    fn = tools["piping_internal_volume"]
    res = json.loads(fn(doc='{"schema":"wrong/1"}'))
    assert res.get("ok") is False


def test_mcp_piping_verify_cfd_no_session():
    """piping_verify_cfd with no session state and None args returns soft-fail."""
    from services.mcp.piping_tools import register_piping_tools, _PIPING
    mcp, tools = _make_mcp_stub()
    register_piping_tools(mcp)
    # clear session
    _PIPING.update({"doc": None, "result": None, "boundary": None, "fluid": None, "pumps": None})
    fn = tools["piping_verify_cfd"]
    res = json.loads(fn(solve_result=None, cfd_result=None))
    assert res.get("ok") is False


def test_mcp_piping_verify_cfd_with_results():
    """piping_verify_cfd works with explicit solve + cfd dicts."""
    from services.mcp.piping_tools import register_piping_tools
    mcp, tools = _make_mcp_stub()
    register_piping_tools(mcp)
    fn = tools["piping_verify_cfd"]
    sr = _mock_solve_result()
    cr = _mock_cfd_result()
    res = json.loads(fn(solve_result=json.dumps(sr), cfd_result=json.dumps(cr)))
    assert res.get("ok"), res
    assert "pass" in res
    assert "dp_1d_kpa" in res


# ── main runner ───────────────────────────────────────────────────────────────

def main():
    print("test_cfd_handoff: running pure-Python tests...")

    # group 2
    test_cfd_bc_extracts_inlet_velocity()
    test_cfd_bc_with_explicit_inlet_run_id()
    test_cfd_bc_unknown_run_id_soft_fail()
    test_cfd_bc_failed_solve_soft_fail()
    test_cfd_bc_empty_links_soft_fail()
    print("  group 2 (cfd_boundary_from_solve): 5/5 OK")

    # group 3
    test_verify_failed_solve_soft_fail()
    test_verify_failed_cfd_soft_fail()
    test_verify_velocity_limit_flagged()
    test_verify_unknown_run_id_soft_fail()
    test_verify_cd_fallback_when_no_area()
    if pytest:
        test_verify_pass_within_tolerance()
        test_verify_fail_outside_tolerance()
        test_verify_specific_run_id()
        print("  group 3 (verify_pressure_drop): 8/8 OK")
    else:
        print("  group 3 (verify_pressure_drop): 5/5 pure-Python OK (approx skipped)")

    # group 4
    test_mcp_tools_register()
    test_mcp_piping_internal_volume_bad_doc()
    test_mcp_piping_verify_cfd_no_session()
    test_mcp_piping_verify_cfd_with_results()
    print("  group 4 (MCP smoke): 4/4 OK")

    if _have_kernel():
        print("  group 1 (internal_volume kernel): running...")
        test_internal_volume_produces_step()
        test_internal_volume_single_run_filter()
        test_internal_volume_unknown_run_id()
        test_end_elbow_keeps_full_lr_radius()
        print("  group 1 (internal_volume kernel): 4/4 OK")
    else:
        test_internal_volume_no_kernel_raises_piping_cfd_error()
        print("  group 1 (internal_volume kernel): SKIPPED (build123d absent); "
              "absent-kernel raise test: 1/1 OK")

    print("test_cfd_handoff: ALL OK")


if __name__ == "__main__":
    main()
