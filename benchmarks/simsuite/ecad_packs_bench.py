"""simsuite bench: ECAD verification/physics packs (FIN workstream) on one reference board.

Reuses the reference MCU board fixture from services/mcp/test_ecad_packs.py (connector ->
regulator -> MCU on a +5V trace, GND zone, CLK trace, CLK_P/N pair) and times every pack
against it. Regression-only (ref=None) except:

  * pdn_ir_drop            ref = Pouillet's law hand calc, R = rho*L/(w*t) per segment,
                           drop(U2) = 2A*R(10mm) + 1A*R(20mm) (superposed segment currents)
  * impedance_microstrip   ref = IPC-2141A surface microstrip
                           Z0 = 87/sqrt(er+1.41) * ln(5.98*h/(0.8*w + t))
  * diff_impedance         ref = IPC/National edge-coupled de-rating
                           Z_diff = 2*Z0_se*(1 - 0.48*exp(-0.96*s/h))
  * stackup_solve          ref = the 50-ohm target read back through ecad_check_impedance
  * power_budget           ref = battery life_h = capacity_ah / i_avg_a (charge bookkeeping)

All packs are lightweight (the thermal solve is a coarse ~1 s tet solve), so quick and
full mode run the same case list; full mode adds a finer-grid PDN case and the
trace-heating thermal variant.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DOMAIN = "ecad_packs"


def _case(case: str, *, setup_s=0.0, solve_s=0.0, total_s=None, metric=None,
          ref=None, status="ok", detail="") -> dict:
    rel = None
    if ref not in (None, 0) and metric is not None:
        rel = abs(metric - ref) / abs(ref)
    return {"domain": DOMAIN, "case": case, "setup_s": round(setup_s, 4),
            "mesh_s": 0.0, "solve_s": round(solve_s, 4),
            "total_s": round(total_s if total_s is not None else setup_s + solve_s, 4),
            "metric": metric, "ref": ref, "rel_err": rel,
            "status": status, "detail": detail}


def _fail(case: str, exc: Exception, dt: float) -> dict:
    return _case(case, solve_s=dt, status="fail", detail=f"{type(exc).__name__}: {exc}")


def run(quick: bool = False) -> list[dict]:
    import os
    os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

    from services.mcp.test_ecad_packs import (
        register_pack_tools, build_reference_board, _call,
        REG_ENG, MCU_ENG, PWR_TRACE_W_MM, J1_X, U1_X, U2_X)
    from engine.ecad import session as ecad_session
    from engine.ecad.pdn import RHO_CU_OHM_M, DEFAULT_COPPER_T_MM
    from mcp_server.ecad_tools import _ECAD

    try:
        import build123d  # noqa: F401
        has_3d = True
    except ImportError:
        has_3d = False

    cases: list[dict] = []

    # ---- fixture -------------------------------------------------------------
    t0 = time.perf_counter()
    tools = register_pack_tools()
    board = build_reference_board(tools)
    setup_s = time.perf_counter() - t0
    cases.append(_case("board_setup", setup_s=setup_s, detail="reference board build"))

    def timed(case: str, fn, *, metric=None, ref=None, ok_pred=None, detail=""):
        t = time.perf_counter()
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            cases.append(_fail(case, e, time.perf_counter() - t))
            return None
        dt = time.perf_counter() - t
        m = metric(r) if callable(metric) else metric
        status = "ok"
        d = detail
        if ok_pred is not None and not ok_pred(r):
            status = "fail"
            d = (d + " | " if d else "") + f"predicate failed: {str(r)[:200]}"
        c = _case(case, solve_s=dt, metric=m, ref=ref, status=status, detail=d)
        if c["rel_err"] is not None and c["rel_err"] > _REL_TOL.get(case, 0.05):
            c["status"] = "fail"
            c["detail"] = (c["detail"] + " | " if c["detail"] else "") + \
                f"rel_err {c['rel_err']:.3f} > {_REL_TOL.get(case, 0.05)}"
        cases.append(c)
        return r

    # rel_err acceptance per analytic case (rasterised PDN gets a wide band; the
    # closed-form checks share the exact formula so they must be tight).
    _REL_TOL = {"pdn_ir_drop": 0.25, "pdn_ir_drop_fine": 0.25,
                "impedance_microstrip": 1e-3, "diff_impedance": 1e-3,
                "stackup_solve": 0.02, "power_budget": 1e-6}

    # ---- PDN: IR drop at U2 vs Pouillet's law hand calc ------------------------
    # R per mm of the 1.0 mm x 35 um foil trace: R = rho * L / (w * t).
    r_per_mm = RHO_CU_OHM_M * 1e-3 / (PWR_TRACE_W_MM * 1e-3 * DEFAULT_COPPER_T_MM * 1e-3)
    # segment currents: J1->U1 carries both sinks (2 A) over 10 mm, U1->U2 1 A over 20 mm
    drop_u2_ref = 2.0 * (U1_X - J1_X) * r_per_mm + 1.0 * (U2_X - U1_X) * r_per_mm

    def _pdn(res=None):
        kw = {"net": "+5V", "source_refs": "J1", "doc_id": board}
        if res:
            kw["resolution_mm"] = res
        return _call(tools, "ecad_run_pdn", **kw)

    timed("pdn_ir_drop", _pdn,
          metric=lambda r: next(d["drop_v"] for d in r["nets"][0]["drops"]
                                if d["ref"] == "U2"),
          ref=drop_u2_ref, ok_pred=lambda r: r.get("ok"),
          detail="V drop at U2, default grid")
    if not quick:
        timed("pdn_ir_drop_fine", lambda: _pdn(res=0.1),
              metric=lambda r: next(d["drop_v"] for d in r["nets"][0]["drops"]
                                    if d["ref"] == "U2"),
              ref=drop_u2_ref, ok_pred=lambda r: r.get("ok"),
              detail="V drop at U2, 0.1 mm grid")

    # ---- single-ended impedance vs IPC-2141A ----------------------------------
    def _z0_ref(r):
        # IPC-2141A surface microstrip: Z0 = 87/sqrt(er+1.41)*ln(5.98h/(0.8w+t))
        return 87.0 / math.sqrt(r["er"] + 1.41) * math.log(
            5.98 * r["h"] / (0.8 * r["width"] + r["t"]))

    t = time.perf_counter()
    try:
        rz = _call(tools, "ecad_check_impedance", net_or_ref="CLK", target=50.0,
                   doc_id=board)
        dt = time.perf_counter() - t
        cases.append(_case("impedance_microstrip", solve_s=dt, metric=rz.get("z0"),
                           ref=(_z0_ref(rz) if rz.get("ok") else None),
                           status=("ok" if rz.get("ok") else "fail"),
                           detail="ohm, CLK 0.3 mm on 1.53 mm core"))
    except Exception as e:  # noqa: BLE001
        cases.append(_fail("impedance_microstrip", e, time.perf_counter() - t))

    # ---- differential impedance vs the coupling de-rating ----------------------
    t = time.perf_counter()
    try:
        rd = _call(tools, "ecad_check_diff_impedance", net_p="CLK_P", net_n="CLK_N",
                   target=100.0, doc_id=board)
        dt = time.perf_counter() - t
        zd_ref = (2.0 * rd["z0_se"] * (1.0 - 0.48 * math.exp(
            -0.96 * rd["gap_mm"] / rd["h"])) if rd.get("ok") else None)
        cases.append(_case("diff_impedance", solve_s=dt, metric=rd.get("z_diff"),
                           ref=zd_ref, status=("ok" if rd.get("ok") else "fail"),
                           detail="ohm, 0.25 mm pair / 0.2 mm gap"))
    except Exception as e:  # noqa: BLE001
        cases.append(_fail("diff_impedance", e, time.perf_counter() - t))

    # ---- SI rule pack ----------------------------------------------------------
    _call(tools, "ecad_set_net_class", name="clk_grp",
          params={"match_tolerance_mm": 0.5}, nets=["CLK", "SHORTNET"], doc_id=board)
    timed("sicheck", lambda: _call(tools, "ecad_run_sicheck", doc_id=board),
          ok_pred=lambda r: r.get("ok") and any(
              g["rule"] == "si_match_length" for g in r["by_rule"]),
          detail="length-match group CLK/SHORTNET")

    # ---- host-board rule pack ---------------------------------------------------
    timed("hostcheck", lambda: _call(tools, "ecad_run_hostcheck", doc_id=board),
          ok_pred=lambda r: r.get("ok") and r.get("errors") == 0,
          detail="clean reference board")

    # ---- FPGA pack ---------------------------------------------------------------
    def _fpga():
        s = _call(tools, "ecad_new_schematic", name="ecad_packs_bench_fpga")
        _call(tools, "ecad_place_symbol", libid="FPGA:iCE40UP5K-SG48", x=100, y=100,
              doc_id=s["doc_id"])
        return _call(tools, "ecad_fpga_check", doc_id=s["doc_id"])

    timed("fpga_check", _fpga,
          ok_pred=lambda r: bool(r.get("devices")) and any(
              v["check"] == "config" for v in r.get("violations", [])),
          detail="bare iCE40 must flag CRESET_B")

    # ---- stackup advisor: solve + read back at target ---------------------------
    def _stackup():
        _call(tools, "ecad_new_pcb", name="ecad_packs_bench_stk")
        did = _ECAD["current_pcb"]
        sol = _call(tools, "ecad_stackup_solve", copper_layers=4, doc_id=did)
        if not sol.get("ok"):
            return sol
        w50 = sol["geometries"]["se_50"]["track_width"]
        doc = ecad_session.get_session(did).doc
        doc["nets"].append({"name": "Z50", "class": "default"})
        doc["tracks"].append({"id": "t1", "kind": "segment", "p1": [5.0, 5.0],
                              "p2": [25.0, 5.0], "width": w50, "net": "Z50",
                              "layer": "F.Cu"})
        return _call(tools, "ecad_check_impedance", net_or_ref="Z50", target=50.0,
                     doc_id=did)

    timed("stackup_solve", _stackup, metric=lambda r: r.get("z0"), ref=50.0,
          ok_pred=lambda r: r.get("ok") and r.get("within_tolerance") is True,
          detail="solved width read back on jlc_4layer_7628")

    # ---- thermal handoff --------------------------------------------------------
    if not has_3d:
        for c in ("thermal_dry", "thermal_solve", "verify_thermal", "heatsink_select",
                  "airflow_dry", "vibe_dry"):
            cases.append(_case(c, status="skip", detail="build123d/OCP not installed"))
    else:
        timed("thermal_dry",
              lambda: _call(tools, "ecad_thermal_handoff", doc_id=board, dry_run=True),
              ok_pred=lambda r: r.get("ok") and r["coverage"]["n_heat_sources"] == 2,
              detail="geometry + BCs + simhandoff envelope only")
        solved = timed(
            "thermal_solve",
            lambda: _call(tools, "ecad_thermal_handoff", doc_id=board,
                          mesh_fineness=0.35),
            ok_pred=lambda r: r.get("solved") and any(
                c["ref"] == "U1" and c["tj_c"] is not None
                for c in r.get("components", [])),
            detail="coarse live tet solve, 2.3 W still air")
        if solved and solved.get("solved"):
            timed("verify_thermal", lambda: _call(tools, "ecad_verify_thermal"),
                  ok_pred=lambda r: r.get("ok") and r.get("pass") is False,
                  detail="U1 Tj must bust its 125 C limit by design")
            timed("heatsink_select",
                  lambda: _call(tools, "heatsink_select", component_ref="U1",
                                tj_target_c=85.0),
                  ok_pred=lambda r: r.get("ok") and r.get("target_met") is True,
                  detail="library candidates re-solved to Tj<=85C")
            if not quick:
                timed("thermal_trace_heating",
                      lambda: _call(tools, "ecad_thermal_handoff", doc_id=board,
                                    include_trace_heating=True, mesh_fineness=0.35),
                      ok_pred=lambda r: r.get("solved"),
                      detail="PDN I2R map folded in as distributed flux")
        else:
            for c in ("verify_thermal", "heatsink_select"):
                cases.append(_case(c, status="skip", detail="thermal solve unavailable"))

        # ---- airflow (OpenFOAM optional -> dry run is the contract check) --------
        timed("airflow_dry",
              lambda: _call(tools, "ecad_airflow_handoff", doc_id=board, dry_run=True),
              ok_pred=lambda r: r.get("ok") and (r.get("simhandoff") or {}).get(
                  "schema") == "aicad.simhandoff/1",
              detail="STL solids + aicad.simhandoff/1 envelope, no CHT solve")

        # ---- vibration screen (dry run: mass model + mount fixtures) -------------
        timed("vibe_dry",
              lambda: _call(tools, "ecad_vibe_check", doc_id=board, dry_run=True,
                            mount_holes=[[3, 3], [37, 3], [37, 27], [3, 27]]),
              ok_pred=lambda r: r.get("ok") and len(r.get("mount_holes", [])) == 4
              and 2.0 < r["mass_model"]["board_mass_g"] < 8.0,
              detail="mass model + 4 mount fixtures")

    # ---- power budget: battery life hand calc ------------------------------------
    bp = {"schema": "aicad.boardphys/1", "board": {"name": "ecad_packs_ref"},
          "components": [{"ref": "U1", "supply": REG_ENG["supply"]},
                         {"ref": "U2", "supply": MCU_ENG["supply"]}]}
    # life_h = capacity_ah / i_avg_a = (2000 mAh / 1000) / (1 A + 1 A) = 1.0 h
    timed("power_budget",
          lambda: _call(tools, "power_budget",
                        source={"kind": "battery", "name": "+5V", "voltage_v": 5.0,
                                "capacity_mah": 2000.0},
                        board=bp),
          metric=lambda r: (r.get("battery") or {}).get("life_h"),
          ref=(2000.0 / 1000.0) / 2.0,
          ok_pred=lambda r: r.get("ok") and r.get("degraded") is True,
          detail="degraded always-active budget, life_h")

    return cases


if __name__ == "__main__":
    out = run(quick="--quick" in sys.argv)
    print(json.dumps(out, indent=1))
