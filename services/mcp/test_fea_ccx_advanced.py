"""Advanced FEA regression tests — CalculiX contact and engine fatigue.

Two legs (simsuite fea_ccx_advanced domain; anchors: core/tests/test_fatigue.cpp,
scripts/vv_benchmark.py FAMILY 10, engine_oracle/sim/multiphysics_rpc.py):

1. CONTACT — the two-block *NLGEOM *CONTACT PAIR proxy: base reaction must equal
   the applied load (static equilibrium / Newton III), and the slave-interface
   settlement must obey Hooke uniaxial compression (sigma = E*epsilon) plus the
   penalty penetration bound delta = p/K of the LINEAR pressure-overclosure law
   (CalculiX *SURFACE BEHAVIOR, PRESSURE-OVERCLOSURE=LINEAR: p = K*delta).
   Regression-pins the parse_dat_nodal last-block fix: averaging RF across all
   NLGEOM increments under-reported the reaction by ~53%.

2. FATIGUE — the C++ engine `sim_run_fatigue` (core/sim/fea_fatigue.cpp) on the
   same inline 6-tet cube as core/tests/test_fatigue.cpp (cross-language parity),
   plus the host-mirror S-N life vs a hand-computed Basquin/Shigley case.

Harness robustness (the ccx 2.23 strict-.inp trap): missing binary -> clean skip,
malformed .inp -> clean error not a fabricated result (ccx exits rc=0 with only
*WARNINGs and a ZERO-BYTE .dat on an unparseable deck!), tiny timeout -> clean
timeout error, no hang.

Live ccx tests skip honestly when ccx is absent (CHUDWARE_CCX or PATH). The engine
fatigue leg runs core_bridge in a fresh subprocess (one .so bind per process —
same pattern as test_fea_live_bridge.py).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from services.mcp import fea_advanced as fa

# ---------------------------------------------------------------------------
# ccx discovery: honor the documented install location when PATH lacks ccx.
# ---------------------------------------------------------------------------
_DEFAULT_CCX = Path.home() / ".local" / "bin" / "ccx"
if fa.ccx_path() is None and _DEFAULT_CCX.exists():
    os.environ.setdefault("CHUDWARE_CCX", str(_DEFAULT_CCX))

_HAS_CCX = fa.ccx_path() is not None
needs_ccx = pytest.mark.skipif(
    not _HAS_CCX, reason="CalculiX (ccx) not found — set CHUDWARE_CCX or add to PATH")


# ---------------------------------------------------------------------------
# Contact — two-block compression: force balance + penetration bounds
# ---------------------------------------------------------------------------
CT_L, CT_B, CT_H = 50.0, 50.0, 20.0
CT_E, CT_NU, CT_P = 200000.0, 0.3, 1.0            # 1 MPa on 50x50 -> 2500 N


@needs_ccx
def test_contact_force_balance():
    """Static equilibrium (Newton III): the vertical reaction summed over the fixed
    base must equal the applied load p*L*b transmitted across the contact pair.
    +-1% (the final increment balances exactly; the pre-fix increment-averaging
    bug read 53% low)."""
    gen = fa.build_contact_block_inp(CT_L, CT_B, CT_H, CT_E, CT_NU, CT_P)
    run = fa.run_ccx(gen["inp"], job="ct", timeout=300.0)
    assert run.get("ok") and run.get("dat_path"), run.get("stderr", "")[-300:]
    rf = fa.parse_dat_nodal(run["dat_path"], "RF", gen["base_nodes"], component=3)
    assert rf is not None, "no RF parsed from .dat"
    reaction = abs(rf * len(gen["base_nodes"]))
    applied = gen["applied_load_n"]
    assert applied == CT_P * CT_L * CT_B
    rel = abs(reaction - applied) / applied
    assert rel < 0.01, f"base reaction {reaction:.2f} N vs applied {applied:.2f} N"


@needs_ccx
def test_contact_interface_settlement_and_penetration_bound():
    """Slave-interface settlement vs closed form. The lower block is in uniform
    uniaxial compression sigma = p, so its top face (= the contact interface)
    settles delta_el = p*h/E (Hooke, sigma = E*epsilon). The slave nodes settle
    delta_el + delta_pen where the penalty law *SURFACE BEHAVIOR,
    PRESSURE-OVERCLOSURE=LINEAR gives p = K*delta_pen with K = 10*E (the
    generator's stiffness), i.e. delta_pen = p/(10*E) — three orders below
    delta_el. Assert settlement within [0.5x, 1.5x] of delta_el and penetration
    bounded by 10x the penalty prediction."""
    gen = fa.build_contact_block_inp(CT_L, CT_B, CT_H, CT_E, CT_NU, CT_P)
    # Add a displacement print for the slave set (NUPB is emitted by the generator).
    inp = gen["inp"].replace("*NODE PRINT, NSET=NBASE",
                             "*NODE PRINT, NSET=NUPB\nU\n*NODE PRINT, NSET=NBASE")
    run = fa.run_ccx(inp, job="ctu", timeout=300.0)
    assert run.get("ok") and run.get("dat_path"), run.get("stderr", "")[-300:]
    # Slave set (upper-block bottom, k=0 layer) reconstructed from the generator's
    # own outputs: node ids advance by nxy=(nx+1)*(ny+1) per z-layer and the
    # default mesh has nz=4 layers, so slave = top_nodes shifted down 4 layers.
    # (base_nodes is the lower block's k=0 layer -> len(base_nodes) == nxy.)
    nxy = len(gen["base_nodes"])
    slave = [n - 4 * nxy for n in gen["top_nodes"]]
    w = fa.parse_dat_nodal(run["dat_path"], "U", slave, component=3)
    assert w is not None, "no slave-face U parsed"
    delta_el = CT_P * CT_H / CT_E                   # Hooke: 1*20/200000 = 1e-4 mm
    delta_pen_ref = CT_P / (10.0 * CT_E)            # penalty p=K*delta, K=10E
    settlement = -w                                  # downward
    assert 0.5 * delta_el < settlement < 1.5 * delta_el + 10.0 * delta_pen_ref, \
        f"slave settlement {settlement:.3e} mm vs Hooke {delta_el:.3e} mm"
    # Penetration = settlement - elastic settlement of the master face; bound it.
    penetration = settlement - delta_el
    assert penetration < 10.0 * delta_pen_ref, \
        f"penetration {penetration:.3e} mm exceeds 10x penalty bound " \
        f"{delta_pen_ref:.3e} mm"


def test_parse_dat_nodal_uses_last_increment_block(tmp_path):
    """Multi-increment NLGEOM .dat: one RF block per converged increment. The
    parser must return the FINAL block (full load), not the average of all
    partial-load increments (the pre-fix behavior: 1.5 here, 53% low live)."""
    dat = tmp_path / "j.dat"
    dat.write_text(
        " forces (fx,fy,fz) for set NBASE and time  0.5000000E+00\n\n"
        "         1  0.0  0.0  1.000000E+00\n"
        "         2  0.0  0.0  1.000000E+00\n\n"
        " forces (fx,fy,fz) for set NBASE and time  0.1000000E+01\n\n"
        "         1  0.0  0.0  2.000000E+00\n"
        "         2  0.0  0.0  2.000000E+00\n\n")
    val = fa.parse_dat_nodal(str(dat), "RF", [1, 2], component=3)
    assert val is not None and math.isclose(val, 2.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Fatigue — engine sim_run_fatigue parity cube + hand-computed Basquin life
# ---------------------------------------------------------------------------
# Hand Basquin/Shigley case (Shigley 10th Eq 6-14..6-16, f=0.9), independent of
# the vv FAT1 numbers:
#   Se = 150 MPa, Sut = 500 MPa, sa_eq = 300 MPa (finite-life regime)
#   a = (0.9*500)^2 / 150 = 450^2/150 = 1350.0 MPa
#   b = -(1/3)*log10(450/150) = -(1/3)*log10(3) = -0.1590404
#   N = (300/1350)^(1/b) = (0.2222222)^(-6.287712) = 1.2804e4 cycles
BASQUIN_SE, BASQUIN_SUT, BASQUIN_SA = 150.0, 500.0, 300.0
BASQUIN_N = (BASQUIN_SA / 1350.0) ** (1.0 / (-(1.0 / 3.0) * math.log10(3.0)))


def test_sn_life_matches_hand_basquin():
    N = fa.sn_life(BASQUIN_SA, BASQUIN_SE, BASQUIN_SUT)
    assert math.isclose(N, BASQUIN_N, rel_tol=1e-9)          # same formula, exact
    assert math.isclose(N, 1.2804e4, rel_tol=1e-3)           # hand-typed anchor


def test_fatigue_summarize_hand_case_regimes_and_life():
    theories = {"goodman": {"sa_eq_mpa": BASQUIN_SA},        # finite: 1.28e4
                "gerber": {"sa_eq_mpa": 100.0}}              # 100 < Se -> runout
    out = fa.fatigue_summarize(theories, BASQUIN_SE, BASQUIN_SUT, theory="goodman")
    g = out["theories"]["goodman"]
    assert g["regime"] == "finite"
    assert math.isclose(g["cycles"], BASQUIN_N, rel_tol=1e-3)
    assert math.isclose(g["fos_endurance"], BASQUIN_SE / BASQUIN_SA, rel_tol=1e-6)
    assert out["theories"]["gerber"]["regime"] == "infinite"
    assert out["theories"]["gerber"]["cycles"] is None
    assert out["cycles"] == g["cycles"]                       # selected theory echoed


# --- engine leg: subprocess driver (one .so bind per process, house pattern) ---

_CUBE_STUDY = {
    # Inline 6-tet unit cube, the SAME fixture as core/tests/test_fatigue.cpp:
    # fixed z=0 face, +z force on the z=1 face, fully-reversed cycle.
    "points": [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
               0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1],
    "tets": [0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
             0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6],
    "material": {"E_mpa": 200000.0, "nu": 0.29, "yield_mpa": 370.0,
                 "ultimate_mpa": 440.0, "endurance_mpa": 220.0},
    "fixtures": [{"points": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
                  "tol_mm": 0.01}],
    "loads": [{"type": "force", "vector": [0.0, 0.0, 50000.0],
               "faces": {"points": [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]],
                         "tol_mm": 0.01}}],
    "fatigue": {"se_corrected_mpa": 200.0, "ultimate_mpa": 440.0,
                "yield_mpa": 370.0, "lmin": -1.0, "lmax": 1.0},
}

_DRIVER = r"""
import json, sys
sys.path.insert(0, {repo!r})
from services.mcp import core_bridge as cb
from services.mcp.core_bridge import CoreError
study = json.loads(sys.argv[1])
try:
    res = cb.call("sim_run_fatigue", {{"study": study}})
except CoreError as e:
    res = {{"ok": False, "unknown_method": "unknown method" in str(e),
            "error": str(e)}}
print(json.dumps(res))
"""


def _run_engine_fatigue(study: dict) -> dict:
    repo = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, CHUDWARE_ENGINE_DAEMON="0")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _DRIVER.format(repo=repo),
         json.dumps(study)],
        capture_output=True, text=True, timeout=300, cwd=repo, env=env)
    if proc.returncode != 0:
        pytest.skip(f"engine driver failed rc={proc.returncode}: "
                    f"{(proc.stderr or '')[-300:]}")
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    if res.get("unknown_method"):
        pytest.skip("this libchudware.so lacks sim_run_fatigue (no FEA plugin)")
    return res


def test_engine_fatigue_fully_reversed_parity_cube():
    """Cross-language parity with core/tests/test_fatigue.cpp: a fully-reversed
    cycle (lmin=-1, lmax=1) has zero mean stress, so EVERY mean-stress theory's
    sa_eq collapses to the raw alternating stress crit_sigma_a (Goodman/Gerber/
    Soderberg/ASME/Morrow all reduce to sa at sigma_m = 0)."""
    res = _run_engine_fatigue(_CUBE_STUDY)
    assert res.get("ok"), res.get("error")
    s = res["summary"]
    assert math.isclose(s["crit_sigma_m"], 0.0, abs_tol=1e-9)
    sa = s["crit_sigma_a"]
    assert sa > 0.0
    for name in ("goodman", "gerber", "soderberg", "asme", "morrow"):
        assert math.isclose(s["theories"][name]["sa_eq_mpa"], sa,
                            rel_tol=1e-6), name


def test_engine_fatigue_plus_host_life_hand_checked():
    """Engine sa_eq -> host sn_life chain: with the cube's huge peak stress
    (~5e4 MPa >> Sut) the selected theory must land in static_overload, and a
    scaled-down load (stress ~ load: linear FEA) must reach runout when
    sa_eq < Se. Both regimes hand-derivable from the S-N line definition."""
    res = _run_engine_fatigue(_CUBE_STUDY)
    assert res.get("ok"), res.get("error")
    sa = res["summary"]["crit_sigma_a"]
    out = fa.fatigue_summarize({"goodman": {"sa_eq_mpa": sa}}, 200.0, 440.0,
                               theory="goodman")
    assert out["regime"] == "static_overload"                # sa ~5e4 >= Sut=440
    # Linear static FEA: stress scales with load. Scale the load so the peak
    # alternating stress sits below Se -> infinite life (runout).
    scale = 100.0 / sa                                       # target 100 < Se=200
    study2 = json.loads(json.dumps(_CUBE_STUDY))
    study2["loads"][0]["vector"][2] *= scale
    res2 = _run_engine_fatigue(study2)
    assert res2.get("ok"), res2.get("error")
    sa2 = res2["summary"]["crit_sigma_a"]
    assert math.isclose(sa2, 100.0, rel_tol=1e-3)            # linearity check
    out2 = fa.fatigue_summarize({"goodman": {"sa_eq_mpa": sa2}}, 200.0, 440.0)
    assert out2["theories"]["goodman"]["regime"] == "infinite"


# ---------------------------------------------------------------------------
# Harness robustness — missing binary / malformed .inp / timeout
# ---------------------------------------------------------------------------

def test_missing_ccx_skips_cleanly_via_contact_endpoint(monkeypatch):
    """The legacy contact proxy refuses honestly when its solver is absent."""
    from services.sim import multiphysics_rpc as mp
    monkeypatch.setattr(fa, "ccx_path", lambda: None)
    out = mp._sim_run_contact({})
    assert out.get("ok") is False and out.get("skipped") is True, out


@needs_ccx
def test_malformed_inp_clean_error_not_fabricated_result():
    """ccx 2.23 exits rc=0 on an unparseable deck (*WARNING only, 'Job finished')
    and touches a ZERO-BYTE .dat. run_ccx must not surface that as a result file
    (dat_path=None), so every caller falls into its honest error branch instead
    of returning a 'solved' study with no numbers. Must return fast — no hang."""
    t0 = time.time()
    r = fa.run_ccx("*GARBAGE\nthis is not an inp\n", job="bad", timeout=60.0)
    assert time.time() - t0 < 30.0, "malformed deck must not hang"
    assert r.get("skipped") is None or r.get("skipped") is False
    assert r.get("dat_path") is None, "zero-byte .dat must not be a result"


@needs_ccx
def test_ccx_timeout_clean_error():
    """A too-small timeout must come back as {ok:false, error:'timed out...'}
    promptly (subprocess.run kills the child) — the long-solve guard rail."""
    gen = fa.build_contact_block_inp(CT_L, CT_B, CT_H, CT_E, CT_NU, CT_P)
    t0 = time.time()
    r = fa.run_ccx(gen["inp"], job="slow", timeout=0.01)
    assert time.time() - t0 < 10.0
    assert r.get("ok") is False and "timed out" in r.get("error", ""), r


# ---------------------------------------------------------------------------
# Layered timeouts — the inner CalculiX clock must stay strictly inside the
# outer harness clock (services/agent/tool_guidance/sim.py timeout_s enforced
# by agent_turn._dispatch_tool's ThreadPoolExecutor).
# ---------------------------------------------------------------------------
# Both clocks were 300 s, i.e. EQUAL, and the outer one starts EARLIER (at
# dispatch, before deck generation / mesh build / tempdir setup), so it always
# expired first: run_ccx's actionable "ccx timed out" branch was unreachable
# through the harness and the model only ever got "abandoned in the background,
# may still be running". These pin the INVARIANT (inner < outer, margin covers
# pre-solve work), not the particular seconds.

_RUN_FEA_TOOLS = (
    "run_fea_adaptive", "run_fea_buckling", "run_fea_contact",
    "run_fea_convergence", "run_fea_fatigue", "run_fea_frequency_response",
    "run_fea_modal", "run_fea_static", "run_fea_thermal",
)


def test_every_run_fea_tool_outer_ceiling_exceeds_the_inner_ccx_clock():
    """For EVERY run_fea_* tool the harness advertises, the inner subprocess
    budget must be strictly smaller than the outer dispatch ceiling, by the
    declared setup margin. A new family member that forgets timeout_s (as
    run_fea_adaptive did) fails here rather than silently inheriting the global
    default and racing its own solver."""
    from services.agent.tool_guidance import sim as sim_guidance

    assert fa.HARNESS_CCX_TIMEOUT_S < fa.HARNESS_FEA_TIMEOUT_S, (
        "inner ccx clock must be strictly shorter than the outer harness clock")
    assert (fa.HARNESS_FEA_TIMEOUT_S - fa.HARNESS_CCX_TIMEOUT_S
            == fa.CCX_SETUP_MARGIN_S)
    # The margin has to be able to absorb real pre-solve work (mesh build +
    # autosave + STEP export), not a token epsilon.
    assert fa.CCX_SETUP_MARGIN_S >= 60.0, fa.CCX_SETUP_MARGIN_S

    advertised = sorted(k for k in sim_guidance.ENTRIES if k.startswith("run_fea_"))
    assert advertised == sorted(_RUN_FEA_TOOLS), advertised
    for tool in advertised:
        outer = sim_guidance.ENTRIES[tool].get("timeout_s")
        assert outer is not None, f"{tool} has no outer timeout_s"
        # A multi-leg member (run_fea_convergence's mesh ladder) resolves its
        # setup once and then burns one inner clock per leg, so the solver term
        # scales and the margin does not. Single-solve members are legs=1, i.e.
        # exactly HARNESS_FEA_TIMEOUT_S.
        legs = sim_guidance.FEA_SOLVER_LEGS.get(tool, 1)
        inner = legs * fa.HARNESS_CCX_TIMEOUT_S
        assert float(outer) == inner + fa.CCX_SETUP_MARGIN_S, (
            f"{tool} outer ceiling {outer}s is out of lockstep with {legs} x "
            f"fea_advanced.HARNESS_CCX_TIMEOUT_S={fa.HARNESS_CCX_TIMEOUT_S}s + "
            f"CCX_SETUP_MARGIN_S={fa.CCX_SETUP_MARGIN_S}s")
        assert inner < float(outer), (
            f"{tool}: {legs} inner ccx clock(s) totalling {inner}s are not inside "
            f"outer {outer}s — CalculiX's own timeout diagnostic is unreachable")


@needs_ccx
def test_slow_solve_surfaces_ccx_timeout_not_harness_abandonment(monkeypatch, tmp_path):
    """END TO END through the REAL harness dispatcher, with both clocks scaled by
    the same factor so the test costs ~2 s instead of 7 minutes.

    A solve that outruns its budget must come back as CalculiX's own
    ``{ok: false, error: "ccx timed out after Ns"}`` — the payload that tells the
    model to coarsen the mesh — and NOT as agent_turn's ``timed_out: True``
    "abandoned in the background" payload, which names no remedy. Setup work
    (here a real sleep standing in for deck generation and the mesh build) runs
    inside the outer window before the inner clock starts, which is exactly why
    equal budgets could never surface the inner message."""
    from services.agent import agent_turn as at
    from services.agent import config as agent_config

    outer = 2.0                                     # scaled HARNESS_FEA_TIMEOUT_S
    scale = outer / fa.HARNESS_FEA_TIMEOUT_S
    inner = fa.HARNESS_CCX_TIMEOUT_S * scale        # 1.43 s at scale 2/420
    setup = fa.CCX_SETUP_MARGIN_S * scale * 0.5     # 0.29 s of pre-solve work

    # A ccx that never converges: the inner subprocess timeout is the only thing
    # that can end it. `exec` matters — subprocess.run kills the pid it spawned,
    # and a non-exec /bin/sh would fork `sleep` and leak it past the test.
    fake_ccx = tmp_path / "ccx_slow"
    fake_ccx.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    fake_ccx.chmod(0o755)
    monkeypatch.setattr(fa, "ccx_path", lambda: str(fake_ccx))

    # The outer clock comes from the real per-tool option resolver, so this
    # exercises the same layering the live harness uses.
    monkeypatch.setattr(agent_config, "load_config", lambda *a, **k: {
        "agent": {"tool_options": {"run_fea_thermal": {"timeout_s": int(outer)}}}})

    def _tool_body(_tool, _args):
        time.sleep(setup)                            # deck generation + mesh build
        gen = fa.build_contact_block_inp(20.0, 20.0, 10.0, 200000.0, 0.29, 5.0)
        return fa.run_ccx(gen["inp"], job="thermal", timeout=inner)

    t0 = time.time()
    result = at._dispatch_tool("run_fea_thermal", {}, _inner=_tool_body)
    spent = time.time() - t0

    assert result.get("ok") is False, result
    assert "timed out" in str(result.get("error", "")), result
    assert result.get("backend", "").startswith("calculix"), result
    # The harness abandonment shape must NOT be what came back.
    assert result.get("timed_out") is not True, result
    assert "abandoned" not in str(result.get("error", "")), result
    # And the whole call finished inside the outer ceiling.
    assert spent < outer, f"{spent:.2f}s >= outer ceiling {outer}s"
