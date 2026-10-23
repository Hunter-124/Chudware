"""Modal + frequency-response regression suite (domain fea_modal_freq).

Drives the REAL in-process engine (CHUDWARE_ENGINE_DAEMON=0, services/dist lib via
services.mcp.core_bridge) exactly like the MCP tools do:

  * Cantilever natural frequencies vs analytic Euler-Bernoulli modes. A RECTANGULAR
    (60 x 20 mm) section is used so the first three modes are three DISTINCT
    Euler-Bernoulli values (1st bending thin-dir, 1st bending wide-dir, 2nd bending
    thin-dir) — a square section would collapse modes into near-degenerate pairs and
    weaken the ordering check.
  * Free/unconstrained solve: exactly 6 rigid-body modes near 0 Hz (3 translations +
    3 rotations of a free solid), then the free-free elastic modes.
    REGRESSION for the degenerate-cluster bug: the C++ shift-invert Lanczos with a
    small mode request converged only part of the 6-fold-degenerate 0 Hz cluster and
    silently substituted elastic modes into the "lowest n" list (observed raw
    sim_run_modal n_modes=8 -> 3 rigid + 5 elastic). run_fea_modal now over-requests
    + verifies + truncates; the tool-level test locks that in.
  * Frequency response (multiphysics worker path,
    engine_oracle/sim/multiphysics_rpc.sim_freq_response): the harmonic response
    must peak at the first natural frequency, rigid-body modes must not amplify,
    and Miles' random leg + spec verdict must be wired.

TOLERANCE (modal vs Euler-Bernoulli): 5 %.
  Sources of legitimate deviation: coarse-tet10 discretization (measured ~0.1-0.5 %
  here), Euler-Bernoulli's neglect of shear deformation + rotary inertia (~1-3 % at
  these slendernesses: L/h = 50 for the thin direction, L/b = 16.7 for the wide
  direction / 2nd mode). What 5 % still catches: a lumped-mass regression (reads
  10-20 % low — Bug N-1), tet4-style stiffness bias (~80 % high observed on this
  beam), mode mis-ordering (adjacent analytic modes differ by 3x / 2.1x), and any
  unit slip (>= 1000x).

Skips honestly when the loaded libchudware.so lacks the FEA RPCs.

Run:  CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest \
          services/mcp/test_fea_modal_freq.py -v
"""
from __future__ import annotations

import json
import math
import os
import tempfile

import pytest

os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

# ---------------------------------------------------------------------------
# Case constants + analytic references (every value derived here, with citation)
# ---------------------------------------------------------------------------
# Beam: x = axis (length L), y = width B (wide), z = height H (thin).
L_MM, B_MM, H_MM = 1000.0, 60.0, 20.0
STEEL = {"name": "modal-freq-steel", "E_mpa": 200000.0, "nu": 0.3,
         "density_g_cm3": 7.85}

_E_PA = STEEL["E_mpa"] * 1e6
_RHO = STEEL["density_g_cm3"] * 1000.0          # kg/m^3
_L_M = L_MM * 1e-3
_A_M2 = (B_MM * 1e-3) * (H_MM * 1e-3)
_IZ_M4 = (B_MM * 1e-3) * (H_MM * 1e-3) ** 3 / 12.0   # bending in z (thin dir)
_IY_M4 = (H_MM * 1e-3) * (B_MM * 1e-3) ** 3 / 12.0   # bending in y (wide dir)

# Euler-Bernoulli beam natural frequency f = (beta*L)^2 / (2 pi L^2) * sqrt(E I / rho A)
# (Rao, "Mechanical Vibrations" 5th ed., Sec. 8.5).  Eigenvalue roots (Rao Table 8.1 /
# Craig & Kurdila 2e Table 8.1):
#   clamped-free : beta1*L = 1.87510, beta2*L = 4.69409
#   free-free    : beta1*L = 4.73004 (first ELASTIC mode; 6 rigid modes at 0 precede it)
_BETA1_CF = 1.87510
_BETA2_CF = 4.69409
_BETA1_FF = 4.73004


def _eb_freq_hz(beta_l: float, i_m4: float) -> float:
    return (beta_l ** 2 / (2.0 * math.pi * _L_M ** 2)) * math.sqrt(
        _E_PA * i_m4 / (_RHO * _A_M2))


F1_REF_HZ = _eb_freq_hz(_BETA1_CF, _IZ_M4)   # 1st bending, thin dir  ~ 16.31 Hz
F2_REF_HZ = _eb_freq_hz(_BETA1_CF, _IY_M4)   # 1st bending, wide dir  ~ 48.92 Hz (= 3x F1)
F3_REF_HZ = _eb_freq_hz(_BETA2_CF, _IZ_M4)   # 2nd bending, thin dir  ~102.20 Hz
FF1_REF_HZ = _eb_freq_hz(_BETA1_FF, _IZ_M4)  # free-free 1st elastic  ~103.77 Hz

MODAL_TOL = 0.05          # 5 % — justified in the module docstring
RIGID_HZ = 0.5            # rigid-body modes read ~1e-3 Hz; 1st elastic is >100 Hz

# Exact box mass: V = L*B*H mm^3; 1 mm^3 = 1e-3 cm^3; m = V_cm3 * rho_g_cm3 / 1000 kg.
MASS_KG = (L_MM * B_MM * H_MM * 1e-3) * STEEL["density_g_cm3"] / 1000.0   # 9.42 kg


# ---------------------------------------------------------------------------
# Engine driver (one beam, one clamped modal solve, one free-free solve; cached)
# ---------------------------------------------------------------------------
_CACHE: dict = {}


def _engine():
    """Import core_bridge and verify the FEA plugin is present, else skip."""
    if "cb" in _CACHE:
        return _CACHE["cb"]
    from services.mcp import core_bridge as cb
    from services.mcp.core_bridge import CoreError
    try:
        cb.call("sim_run_modal", {"study": {}})
    except CoreError as exc:
        if "unknown method" in str(exc):
            pytest.skip("loaded libchudware.so lacks sim_run_modal "
                        "(Polyhedral-FEA not built)")
        raise
    _CACHE["cb"] = cb
    return cb


def _beam():
    """Build the beam once; return (doc_id, step_path, left_face_points)."""
    if "beam" in _CACHE:
        return _CACHE["beam"]
    cb = _engine()
    doc = cb.call("doc_new", {"kind": "part",
                              "name": "modal_freq_beam"})["doc_id"]
    r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "Beam", "params": {
            "length": {"value": L_MM}, "width": {"value": B_MM},
            "height": {"value": H_MM}}}}]})
    fs = (r.get("rebuild") or {}).get("feature_status", [])
    assert fs and all(f.get("ok") for f in fs), f"beam rebuild failed: {fs}"
    step = tempfile.mktemp(suffix=".step", prefix="modal_freq_")
    cb.call("doc_export_step", {"doc_id": doc, "path": step})
    q = cb.call("doc_query", {"doc_id": doc,
                              "query": {"of": "face", "side": "left"}})
    faces = [m for m in q.get("matches", []) if m.get("kind") == "face"]
    assert faces, "left (root) face not found"
    fp = cb.call("doc_face_points", {"doc_id": doc, "selector": faces[0]})
    _CACHE["beam"] = (doc, step, fp)
    return _CACHE["beam"]


def _modal_clamped():
    if "clamped" not in _CACHE:
        cb = _engine()
        _doc, step, fp = _beam()
        res = cb.call("sim_run_modal", {"study": {
            "step_path": step, "material": STEEL, "fixtures": [fp],
            "mesh_order": 2, "fineness": 0.4}, "n_modes": 6})
        assert res.get("ok"), f"clamped modal solve failed: {res.get('error')}"
        _CACHE["clamped"] = res
    return _CACHE["clamped"]


def _modal_free_free(n_modes: int = 12):
    key = ("free", n_modes)
    if key not in _CACHE:
        cb = _engine()
        _doc, step, _fp = _beam()
        res = cb.call("sim_run_modal", {"study": {
            "step_path": step, "material": STEEL, "fixtures": [],
            "mesh_order": 2, "fineness": 0.4}, "n_modes": n_modes})
        assert res.get("ok"), f"free-free modal solve failed: {res.get('error')}"
        _CACHE[key] = res
    return _CACHE[key]


def _rel_err(actual: float, ref: float) -> float:
    return abs(actual - ref) / abs(ref)


# ---------------------------------------------------------------------------
# Modal: cantilever vs Euler-Bernoulli
# ---------------------------------------------------------------------------

def test_cantilever_first_three_modes_match_euler_bernoulli():
    freqs = _modal_clamped()["freqs_hz"]
    assert len(freqs) >= 3
    for i, (f, ref) in enumerate(zip(freqs, (F1_REF_HZ, F2_REF_HZ, F3_REF_HZ))):
        assert _rel_err(f, ref) < MODAL_TOL, (
            f"mode {i + 1}: {f:.2f} Hz vs Euler-Bernoulli {ref:.2f} Hz "
            f"({_rel_err(f, ref):.1%} > {MODAL_TOL:.0%})")


def test_cantilever_modes_sorted_and_no_rigid_modes():
    freqs = _modal_clamped()["freqs_hz"]
    assert freqs == sorted(freqs), "mode list must be ascending"
    # A clamped part has NO rigid-body modes — everything must be elastic.
    assert min(freqs) > RIGID_HZ


def test_modal_result_carries_shapes_for_frequency_response():
    res = _modal_clamped()
    n_modes = len(res["freqs_hz"])
    n_nodes = int(res["n_nodes"])
    shapes = res.get("mode_shapes") or []
    assert n_nodes > 0
    assert len(shapes) == n_modes * n_nodes * 3, \
        "mode_shapes must be flat (n_modes * n_nodes * 3) for the freq-response leg"
    # Unit-max normalization: every mode's max |component| is exactly 1.
    for m in range(n_modes):
        block = shapes[m * n_nodes * 3:(m + 1) * n_nodes * 3]
        assert max(abs(v) for v in block) == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Modal: free-free rigid-body modes
# ---------------------------------------------------------------------------

def test_free_free_has_six_rigid_body_modes():
    """A free solid body has exactly 6 zero-frequency modes (3 translations +
    3 rotations — rigid-body kinematics). n_modes=12 gives the eigensolver enough
    Krylov room to resolve the whole degenerate cluster."""
    freqs = _modal_free_free(12)["freqs_hz"]
    n_rigid = sum(1 for f in freqs if f < RIGID_HZ)
    assert n_rigid == 6, f"expected 6 rigid-body modes, got {n_rigid}: {freqs}"


def test_free_free_first_elastic_matches_euler_bernoulli():
    """Mode 7 = free-free 1st elastic bending, beta1*L = 4.73004 (Rao Table 8.1)."""
    freqs = _modal_free_free(12)["freqs_hz"]
    elastic = [f for f in freqs if f >= RIGID_HZ]
    assert elastic, "no elastic modes returned"
    assert _rel_err(elastic[0], FF1_REF_HZ) < MODAL_TOL, (
        f"free-free 1st elastic {elastic[0]:.2f} Hz vs {FF1_REF_HZ:.2f} Hz")


# ---------------------------------------------------------------------------
# Tool-level regression: run_fea_modal free-free must report ALL 6 rigid modes
# even for small n_modes (the raw solver drops part of the degenerate cluster).
# ---------------------------------------------------------------------------

class _CaptureMCP:
    """Minimal FastMCP stand-in capturing @mcp.tool() registrations (same pattern
    as test_fea_advanced.py — register_cad_tools never touches FastMCP internals)."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def dec(fn):
            self.tools[kwargs.get("name", fn.__name__)] = fn
            return fn
        return dec


def test_run_fea_modal_free_free_small_n_reports_rigid_modes():
    _engine()
    doc, _step, _fp = _beam()
    from services.mcp.cad_tools import register_cad_tools
    m = _CaptureMCP()
    register_cad_tools(m)
    out = json.loads(m.tools["run_fea_modal"](
        fixtures=[], n_modes=8, material=STEEL, mesh_fineness=0.4, doc_id=doc))
    assert out.get("ok"), out
    freqs = out["freqs_hz"]
    assert len(freqs) == 8
    n_rigid = sum(1 for f in freqs if f < RIGID_HZ)
    # Raw sim_run_modal(n_modes=8) returned only 3 rigid modes here — the tool's
    # over-request + verify + truncate must restore the true lowest 8 (6 rigid +
    # first two elastic modes).
    assert n_rigid == 6, f"free-free n_modes=8 must contain 6 rigid modes: {freqs}"
    elastic = [f for f in freqs if f >= RIGID_HZ]
    assert _rel_err(elastic[0], FF1_REF_HZ) < MODAL_TOL


# ---------------------------------------------------------------------------
# Frequency response — multiphysics worker path
# ---------------------------------------------------------------------------

def _freq_response(res: dict, **over):
    from services.sim.multiphysics_rpc import WORKER_METHODS
    n_modes = len(res["freqs_hz"])
    params = {
        "freqs_hz": res["freqs_hz"],
        "mode_shapes": res["mode_shapes"][:n_modes * int(res["n_nodes"]) * 3],
        "n_nodes": int(res["n_nodes"]),
        "total_mass_kg": MASS_KG,
        "excitation_dir": [0.0, 0.0, 1.0],   # thin direction = 1st bending plane
        "zeta": 0.02,
    }
    params.update(over)
    return WORKER_METHODS["sim_freq_response"](params, lambda *_: None)


def test_frequency_response_peaks_at_first_mode():
    """The harmonic transmissibility of a base-excited structure peaks at the first
    natural frequency (Harris & Piersol 5e, Ch. 2 Eq. 2.41: |T| max at r ~= 1).
    Sweep grid: 200 log-spaced points over 5-200 Hz -> adjacent points are
    (200/5)^(1/199) - 1 = 1.87 % apart, so peak_freq must land within 3 % of f1."""
    res = _modal_clamped()
    f1 = res["freqs_hz"][0]
    sweep = [5.0 * (200.0 / 5.0) ** (i / 199.0) for i in range(200)]
    out = _freq_response(res, sweep_hz=sweep)
    assert out.get("ok"), out
    harm = out["harmonic"]
    assert _rel_err(harm["peak_freq_hz"], f1) < 0.03, (
        f"response peak {harm['peak_freq_hz']:.2f} Hz not at f1 = {f1:.2f} Hz")
    # At resonance the response is (Gamma/m) * |T(1)|, |T(1)| = sqrt(1+4 zeta^2)/
    # (2 zeta) = 25.02 for zeta = 0.02 (Harris & Piersol Eq. 2.41). With unit-max
    # mode shapes, Gamma/m for the uniform clamped-free fundamental is the classic
    # participation ratio  integral(phi) / integral(phi^2) = 0.3915/0.25 = 1.566
    # (Craig & Kurdila 2e, Sec. 12.3 / Table 8.1 mode shape), so the expected peak
    # is ~1.566 * 25.02 = 39.2 g per 1 g base input. Allow +/-15 % for the 3D
    # (non-slender-theory) mode shape and the sweep-grid offset from exact r = 1.
    assert _rel_err(harm["peak_accel_g"], 1.566 * 25.02) < 0.15, harm["peak_accel_g"]
    # per-mode bookkeeping: mode 1 must be present at f1.
    assert any(abs(pm["freq_hz"] - f1) < 1e-6 for pm in harm["per_mode"])


def test_frequency_response_miles_and_verdict_wiring():
    res = _modal_clamped()
    f1 = res["freqs_hz"][0]
    out = _freq_response(
        res,
        sweep_hz=[f1 * 0.5, f1, f1 * 2.0],
        psd_breakpoints=[[10.0, 0.04], [2000.0, 0.04]],
        vibration_spec={"max_peak_accel_g": 1e9, "max_grms": 1e9})
    assert out.get("ok"), out
    rand = out["random"]
    assert rand.get("ok") is True
    assert rand["total_grms"] > 0.0
    # Miles per-mode GRMS = (Gamma/m) * sqrt(pi/2 * fn * Q * W) (Steinberg 3e Eq. 6.6);
    # the first retained mode must sit at f1.
    assert rand["per_mode"][0]["freq_hz"] == pytest.approx(f1)
    # Spec verdict wiring (huge limits -> must pass, with both checks recorded).
    assert out["verdict"]["passed"] is True
    assert len(out["verdict"]["checks"]) == 2
    # Honesty: damping is an assumption and must be echoed back.
    assert "zeta=0.02" in out["damping_assumption"]


def test_frequency_response_skips_rigid_body_modes():
    """Free-free input: the ~0 Hz rigid modes must NOT amplify (they'd otherwise
    dominate the SRSS with a divide-by-near-zero transmissibility)."""
    res = _modal_free_free(12)
    f_el = [f for f in res["freqs_hz"] if f >= RIGID_HZ][0]
    out = _freq_response(res, sweep_hz=[f_el * 0.5, f_el, f_el * 2.0])
    assert out.get("ok"), out
    retained = out["harmonic"]["per_mode"]
    assert retained, "elastic modes must be retained"
    assert all(pm["freq_hz"] >= RIGID_HZ for pm in retained)


def test_frequency_response_honest_degrade_without_psd():
    res = _modal_clamped()
    out = _freq_response(res, sweep_hz=[10.0, 20.0])
    assert out.get("ok"), out
    assert out["random"]["ok"] is False
    assert out["random"]["reason"] == "no_psd_provided"


# ---------------------------------------------------------------------------
# Teardown: close the shared doc so the in-process engine exits clean.
# ---------------------------------------------------------------------------

def teardown_module(_mod):
    beam = _CACHE.get("beam")
    if beam and "cb" in _CACHE:
        doc, step, _fp = beam
        try:
            _CACHE["cb"].call("doc_close", {"doc_id": doc})
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        try:
            os.unlink(step)
        except OSError:
            pass
