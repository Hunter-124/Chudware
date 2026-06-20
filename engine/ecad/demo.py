"""AI-ECAD comprehensive pipeline walkthrough demo.

Narrative end-to-end exercise of the whole ECAD suite:
  Step 1:  AI design (orchestrator recipe fallback)
  Step 2:  Schematic build via ops
  Step 3:  Netlist + ERC summary
  Step 4:  Simulation suite  (op, transient, AC -3dB, .step sweep, Monte-Carlo)
  Step 5:  PCB: populate from schematic via sync.update_board_from_sch
  Step 6:  Autoroute (ratsnest before -> after)
  Step 7:  Zone fill (GND copper pour)
  Step 8:  DRC summary
  Step 9:  Fabrication: Gerber + drill + BOM + CPL -> temp dir
  Step 10: 3D: board_to_step (OCP-guarded)
  Step 11: Interop: write .ecad+ and reopen; KiCad inline import

Run:
    python -m engine.ecad.demo
"""
from __future__ import annotations

import copy
import math
import os
import sys
import tempfile

# ---------------------------------------------------------------------------
# Console helpers -- ASCII-only (Windows cp1252 safe)
# ---------------------------------------------------------------------------

def _hr(title: str = "") -> None:
    width = 70
    if title:
        pad = max(1, (width - len(title) - 2) // 2)
        line = "-" * pad + " " + title + " " + "-" * pad
        # trim to exactly width if rounding made it longer
        print(line[:width])
    else:
        print("-" * width)


def _section(n: int, title: str) -> None:
    print()
    _hr(f"Step {n}: {title}")


def _pass(msg: str) -> None:
    print(f"  PASS  {msg}")


def _skip(msg: str) -> None:
    print(f"  SKIP  {msg}")


def _info(msg: str) -> None:
    print(f"  {msg}")


# ===========================================================================
# Step 1 -- AI design via orchestrator (recipe fallback, no provider/network)
# ===========================================================================

def step1_ai_design():
    """run_design with provider=None forces the recipe fallback.

    The orchestrator still goes through the full deterministic state machine
    (BUILD -> ERC -> SIMULATE -> REPORT) and returns a verified DesignRun.
    """
    from engine.ecad.orchestrator import run_design

    _section(1, "AI Design  (orchestrator, recipe fallback, no network)")
    request = "5V divider with 2.5V output"
    _info(f"request: {request!r}")
    _info("provider=None  =>  recipe fallback  =>  voltage_divider(5.0, 2.5)")

    run = run_design(request, provider=None)

    _info(f"stop_reason:  {run.stop_reason}")
    _info(f"recipe_used:  {run.recipe_used}")
    _info(f"ok:           {run.ok}")
    _info(f"ops_applied:  {run.ops_applied}")
    _info(f"ERC:          {run.erc['errors']} errors, {run.erc['warnings']} warnings")
    sim = run.sim
    _info(f"Sim:          {sim.get('analysis')}  "
          f"{sim.get('probe')}={sim.get('got'):.4g} V  "
          f"(expect {sim.get('expect'):.3g} V)  "
          f"{'PASS' if sim.get('pass') else 'MISS'}")

    if run.ok:
        _pass("AI design produced a verified schematic + sim PASS")
    else:
        _info("Design run not fully ok -- continuing demo")

    # Return the stages for reference
    return run


# ===========================================================================
# Step 2 -- Build schematic via ops
# ===========================================================================

def step2_schematic():
    from engine.ecad import schema
    from engine.ecad.ops import apply_ops
    from engine.ecad.library.starter import SYMBOLS

    _section(2, "Build Schematic via ops")
    _info("Circuit: V1=5V --[R1=10k]-- VMID --[R2=10k]-- GND")
    _info("Method:  sch_add_symbol ops + power ports + net labels")

    doc = schema.new_schematic("voltage_divider_demo")
    for libid in ("Simulation_SPICE:V_source", "Device:R"):
        doc["lib_cache"][libid] = copy.deepcopy(SYMBOLS[libid])

    res = apply_ops(doc, [{"op": "sch_add_symbol", "libid": "Simulation_SPICE:V_source",
                            "pos": [0.0, 0.0], "ref": "V1", "value": "5"}])
    doc = res["doc"]
    _info(f"Placed V1  (id={res['id_map']['$0']})")

    res = apply_ops(doc, [{"op": "sch_add_symbol", "libid": "Device:R",
                            "pos": [20.0, 5.0], "ref": "R1", "value": "10k"}])
    doc = res["doc"]
    _info(f"Placed R1  (id={res['id_map']['$0']})")

    res = apply_ops(doc, [{"op": "sch_add_symbol", "libid": "Device:R",
                            "pos": [40.0, 5.0], "ref": "R2", "value": "10k"}])
    doc = res["doc"]
    _info(f"Placed R2  (id={res['id_map']['$0']})")

    res = apply_ops(doc, [
        {"op": "sch_add_power", "net": "+5V", "pos": [0.0,   5.08]},
        {"op": "sch_add_power", "net": "GND",  "pos": [0.0,  -5.08]},
        {"op": "sch_add_power", "net": "+5V", "pos": [14.92, 5.0]},
        {"op": "sch_add_power", "net": "GND",  "pos": [45.08, 5.0]},
    ])
    doc = res["doc"]

    res = apply_ops(doc, [
        {"op": "sch_add_label", "text": "VMID", "pos": [25.08, 5.0], "scope": "local"},
        {"op": "sch_add_label", "text": "VMID", "pos": [34.92, 5.0], "scope": "local"},
    ])
    doc = res["doc"]
    _info("Added power ports (+5V, GND) + VMID net labels")

    errs = schema.validate_schematic(doc)
    if not errs:
        _pass("Schema validation OK (0 errors)")
    else:
        _info(f"Schema warnings: {errs[:3]}")
    return doc


# ===========================================================================
# Step 3 -- Netlist + ERC
# ===========================================================================

def step3_netlist_erc(doc):
    from engine.ecad.netlist import build_netlist
    from engine.ecad.erc import run_erc

    _section(3, "Netlist + ERC")
    nl = build_netlist(doc)
    nets = nl["nets"]
    components = nl["components"]
    _info(f"Components ({len(components)}):")
    for c in components:
        pins_summary = ", ".join(f"{p}={n}" for p, n in list(c["pins"].items())[:4])
        _info(f"  {c['ref']:4s}  {c['libid']:35s}  value={c['value']!r}  "
              f"pins=[{pins_summary}]")
    _info(f"Nets ({len(nets)}):")
    for net_name, members in sorted(nets.items()):
        refs = ", ".join(f"{m['ref']}.{m['pin']}" for m in members)
        _info(f"  {net_name:8s}  ({len(members)} pins)  {refs}")

    violations = run_erc(doc, nl)
    errors   = [v for v in violations if v["severity"] == "error"]
    warnings = [v for v in violations if v["severity"] == "warning"]
    _info(f"ERC: {len(errors)} errors, {len(warnings)} warnings")
    if errors:
        for v in errors[:3]:
            _info(f"  [ERROR] {v['rule']}: {v['message']}")
    if not errors:
        _pass("ERC clean -- design is electrically sound")
    return nl


# ===========================================================================
# Step 4 -- Simulation suite
# ===========================================================================

def step4_simulate_suite(nl):
    """Demonstrate op, transient, AC (-3dB), .step sweep, and Monte-Carlo.

    Two separate small circuits are used:
    * Divider (from the netlist): op-point  V(VMID) == 2.5 V
    * RC low-pass (built inline): tran + AC + .step + Monte-Carlo
    """
    from engine.sim.spice import netlist_to_spice_circuit
    from engine.sim.circuit import simulate

    _section(4, "Simulation Suite  (op / tran / AC / .step / Monte-Carlo)")

    # ---- 4a. DC operating point on the divider netlist ----
    _info("4a. DC operating point -- voltage divider (expected V(VMID) = 2.5 V)")
    circuit, warns = netlist_to_spice_circuit(nl)
    _info(f"    Circuit: {len(circuit['devices'])} devices, nodes={circuit['nodes']}")

    probes = ["V(VMID)", "V(+5V)"]
    study = {"analyses": [{"type": "op"}], "probes": probes}
    result = simulate(circuit, study)
    converged = result["convergence"].get("op", False)
    op = result.get("op", {})
    vmid_val = op.get("V(VMID)", float("nan"))
    expected_op = 2.5
    err_pct = abs(vmid_val - expected_op) / expected_op * 100 if expected_op else float("nan")
    _info(f"    Converged: {converged}")
    _info(f"    V(VMID) = {vmid_val:.6g} V   expected {expected_op:.3g} V   "
          f"error = {err_pct:.5f}%")
    if converged and err_pct < 0.01:
        _pass("OP: V(VMID) matches expected within 0.01%")
    else:
        _info(f"    OP: converged={converged}, err={err_pct:.4f}%")

    # ---- 4b. Transient + AC on an RC low-pass circuit ----
    # R=1k, C=159.15 nF => fc = 1/(2*pi*RC) = 1000 Hz
    _info("4b. RC low-pass  R=1k C=159.15n  (fc ~= 1000 Hz)")
    rc_circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
             "value": 0.0, "model": None,
             "params": {"ac": {"mag": 1.0, "phase": 0.0},
                        "waveform": {"type": "PULSE", "v1": 0.0, "v2": 1.0,
                                     "td": 0.0, "tr": 1e-6, "tf": 1e-6,
                                     "pw": 5e-4, "per": 1e-3}}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"],
             "value": 1000.0, "model": None, "params": {}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"],
             "value": 159.15e-9, "model": None, "params": {}},
        ],
        "models": {},
        "params": {"TEMP": 27},
    }
    fc_hz = 1.0 / (2.0 * math.pi * 1000.0 * 159.15e-9)
    _info(f"    Computed fc = {fc_hz:.1f} Hz")

    # Transient: 3 ms, 1 us step, capture settling of V(OUT)
    tran_study = {
        "analyses": [{"type": "tran", "tstep": 1e-5, "tstop": 3e-3, "uic": True}],
        "probes": ["V(OUT)"],
    }
    tran_res = simulate(rc_circuit, tran_study)
    tran_ok = tran_res["convergence"].get("tran", False)
    tran_data = tran_res.get("tran", {})
    traces = tran_data.get("traces", {})
    vout_trace = traces.get("V(OUT)", [])
    v_final = vout_trace[-1] if vout_trace else float("nan")
    _info(f"    Transient: converged={tran_ok}  V(OUT) final = {v_final:.4g} V  "
          f"(expected ~1.0 V at t=3ms, well past tau=159us)")
    if tran_ok:
        _pass("Transient: converged")
    else:
        _info("    Transient: did not converge")

    # AC: sweep 10 Hz -> 100 kHz, check |H| at fc ~= 0.707
    ac_study = {
        "analyses": [{"type": "ac", "sweep": "dec", "points": 20,
                      "fstart": 10.0, "fstop": 100e3}],
        "probes": ["V(OUT)"],
    }
    ac_res = simulate(rc_circuit, ac_study)
    ac_ok = ac_res["convergence"].get("ac", False)
    ac_data = ac_res.get("ac", {})
    freqs = ac_data.get("x_f", [])
    mag_out = ac_data.get("mag", {}).get("V(OUT)", [])
    # Interpolate magnitude at fc
    h_at_fc = float("nan")
    if freqs and mag_out:
        for i in range(1, len(freqs)):
            if freqs[i] >= fc_hz:
                x0, x1 = freqs[i - 1], freqs[i]
                y0, y1 = mag_out[i - 1], mag_out[i]
                if x1 > x0:
                    h_at_fc = y0 + (y1 - y0) * (fc_hz - x0) / (x1 - x0)
                break
    expected_h = 1.0 / math.sqrt(2.0)
    h_err_pct = abs(h_at_fc - expected_h) / expected_h * 100 if math.isfinite(h_at_fc) else float("nan")
    _info(f"    AC: converged={ac_ok}  |H(fc)| = {h_at_fc:.4g}  "
          f"(expected {expected_h:.4g} = -3 dB)  error = {h_err_pct:.2f}%")
    if ac_ok and h_err_pct < 5.0:
        _pass("AC: -3 dB point verified within 5%")
    else:
        _info(f"    AC: converged={ac_ok}")

    # .step sweep: sweep R1 over [500, 1000, 2000] and check DC divider result
    _info("4c. .step sweep  -- R1 over [500, 1000, 2000] ohm with V1=1V (DC)")
    step_circuit = {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
             "value": 1.0, "model": None, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"],
             "value": 1000.0, "model": None, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"],
             "value": 1000.0, "model": None, "params": {}},
        ],
        "models": {},
        "params": {"TEMP": 27},
    }
    step_study = {
        "analyses": [
            {"type": "step", "param": "R1", "values": [500.0, 1000.0, 2000.0]},
            {"type": "op"},
        ],
        "probes": ["V(OUT)"],
    }
    step_res = simulate(step_circuit, step_study)
    step_ok = step_res["convergence"].get("step", False)
    step_data = step_res.get("step", {})
    runs = step_data.get("runs", [])
    _info(f"    .step: ok={step_ok}  {len(runs)} runs")
    # Expected V(OUT) = V1 * R2/(R1+R2): [1*1k/1.5k, 1*1k/2k, 1*1k/3k] = [0.667, 0.5, 0.333]
    expected_vout = [1000.0 / (r + 1000.0) for r in [500.0, 1000.0, 2000.0]]
    for i, run in enumerate(runs):
        v = run.get("op", {}).get("V(OUT)", float("nan"))
        exp = expected_vout[i] if i < len(expected_vout) else float("nan")
        ok_flag = abs(v - exp) / abs(exp) < 0.01 if math.isfinite(v) and exp else False
        _info(f"    R1={run.get('value'):.0f}  V(OUT)={v:.4g}  "
              f"expected={exp:.4g}  {'ok' if ok_flag else 'MISS'}")
    if step_ok:
        _pass(".step: all parameter values converged")

    # Monte-Carlo: 50 runs, 1% R tolerance, V(VMID) on divider
    _info("4d. Monte-Carlo  -- 50 runs, 1% R tolerance, V(VMID) on divider")
    mc_circuit = {
        "nodes": ["0", "IN", "VMID"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"],
             "value": 5.0, "model": None, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "VMID"],
             "value": 10000.0, "model": None, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["VMID", "0"],
             "value": 10000.0, "model": None, "params": {}},
        ],
        "models": {},
        "params": {"TEMP": 27},
    }
    mc_study = {
        "analyses": [
            {"type": "mc", "runs": 50, "probe": "V(VMID)",
             "tol": {"R": 0.01}, "seed": 42,
             "analysis": {"type": "op"}},
        ],
        "probes": ["V(VMID)"],
    }
    mc_res = simulate(mc_circuit, mc_study)
    mc_ok = mc_res["convergence"].get("mc", False)
    mc_data = mc_res.get("mc", {})
    mc_mean = mc_data.get("mean", float("nan"))
    mc_std  = mc_data.get("std", float("nan"))
    mc_runs = mc_data.get("runs", 0)
    _info(f"    MC: ok={mc_ok}  runs={mc_runs}  "
          f"V(VMID) mean={mc_mean:.4g} V  std={mc_std:.4g} V  "
          f"(expected mean~2.5, std~R_tol*mean/sqrt(2)~0.0177)")
    if mc_ok and math.isfinite(mc_mean):
        _pass(f"Monte-Carlo: {mc_runs} runs converged, mean={mc_mean:.4g} V")

    return nl  # pass through so step5 can reuse it


# ===========================================================================
# Step 5 -- PCB: populate from schematic via sync.update_board_from_sch
# ===========================================================================

def step5_pcb_from_sch(doc, nl):
    """Demonstrate the sch->PCB back-annotation path (sync.update_board_from_sch).

    The voltage-divider components (V1, R1, R2) only get footprints if the
    schematic assigns them one. V1 (Simulation_SPICE:V_source) and the
    power symbols have no footprint in the starter library, so they are skipped
    (sim-only). R1/R2 use Resistor_SMD:R_0603, which is in the starter library.

    We fall back to the manual footprint placement used in the original demo
    if the sch->PCB path reports no added footprints, so the demo always
    produces a routable board.
    """
    from engine.ecad import schema
    from engine.ecad.ops import apply_ops
    from engine.ecad.library.starter import FOOTPRINTS
    from engine.ecad.drc import ratsnest
    from engine.ecad.sync import update_board_from_sch

    _section(5, "PCB Layout  (sync.update_board_from_sch + manual footprints)")

    board = schema.new_board("divider_pcb")
    board["nets"].append({"name": "+5V",  "class": "default"})
    board["nets"].append({"name": "VMID", "class": "default"})

    # 30 x 20 mm rectangular outline
    res = apply_ops(board, [{"op": "pcb_set_outline",
        "shapes": [
            {"type": "line", "p1": [0, 0],  "p2": [30, 0]},
            {"type": "line", "p1": [30, 0], "p2": [30, 20]},
            {"type": "line", "p1": [30, 20], "p2": [0, 20]},
            {"type": "line", "p1": [0, 20],  "p2": [0, 0]},
        ], "closed": True}])
    board = res["doc"]
    _info("Board outline: 30 mm x 20 mm")

    # Try the sch->PCB sync path
    sync_result = update_board_from_sch(board, nl)
    synced_board = sync_result["mutated_board"]
    report = sync_result["report"]
    _info(f"sync.update_board_from_sch: added={report['added']}  "
          f"updated={report['updated']}  unresolved={report['unresolved']}  "
          f"nets_bound={report['nets_bound']}")

    if report["added"]:
        board = synced_board
        _pass(f"Populated {len(report['added'])} footprint(s) from schematic: {report['added']}")
    else:
        _info("Sync: no footprints resolved (V_source + power syms have no fpid) -- "
              "placing R_0603 footprints manually")
        fp0603 = FOOTPRINTS["Resistor_SMD:R_0603"]

        r1_pads = copy.deepcopy(fp0603["pads"])
        for pad in r1_pads:
            pad["net"] = "+5V" if str(pad.get("number")) == "1" else "VMID"
        res = apply_ops(board, [{"op": "pcb_add_footprint",
                                  "fpid": "Resistor_SMD:R_0603",
                                  "ref": "R1", "value": "10k",
                                  "pos": [8.0, 10.0], "side": "top",
                                  "pads": r1_pads, "def": fp0603}])
        board = res["doc"]
        _info(f"Placed R1  (id={res['id_map']['$0']})")

        r2_pads = copy.deepcopy(fp0603["pads"])
        for pad in r2_pads:
            pad["net"] = "VMID" if str(pad.get("number")) == "1" else "GND"
        res = apply_ops(board, [{"op": "pcb_add_footprint",
                                  "fpid": "Resistor_SMD:R_0603",
                                  "ref": "R2", "value": "10k",
                                  "pos": [20.0, 10.0], "side": "top",
                                  "pads": r2_pads, "def": fp0603}])
        board = res["doc"]
        _info(f"Placed R2  (id={res['id_map']['$0']})")

    rats_before = ratsnest(board)
    _info(f"Ratsnest (before autoroute): {len(rats_before)} airwire(s)")
    for r in rats_before:
        _info(f"  net={r['net']:8s}  {r['p1']} -> {r['p2']}")

    return board


# ===========================================================================
# Step 6 -- Autoroute
# ===========================================================================

def step6_autoroute(board):
    from engine.ecad import autoroute as autoroute_mod
    from engine.ecad.drc import ratsnest

    _section(6, "Autoroute  (2-layer Lee/maze grid router)")
    before = ratsnest(board)
    _info(f"Ratsnest before: {len(before)} airwire(s)")

    result = autoroute_mod.autoroute(board, {})
    report = result["report"]
    board["tracks"] = list(board.get("tracks", [])) + result["tracks"]
    board["vias"]   = list(board.get("vias", []))   + result["vias"]

    after = ratsnest(board)
    _info(f"Routed nets:  {report['routed'] or '(none)'}")
    _info(f"Failed nets:  {report['failed'] or '(none)'}")
    _info(f"Tracks added: {len(result['tracks'])}  vias: {len(result['vias'])}  "
          f"length: {report['length_mm']} mm")
    _info(f"Grid: pitch={report['grid'].get('pitch')} mm  "
          f"{report['grid'].get('nx')}x{report['grid'].get('ny')} cells")
    _info(f"Ratsnest after: {len(after)} airwire(s)")
    if not report["failed"]:
        _pass("All routable airwires connected")
    else:
        _info(f"Note: {len(report['failed'])} nets not routed "
              "(expected for single-terminal supply nets without both endpoints on the board)")
    return board


# ===========================================================================
# Step 7 -- Zone fill (GND copper pour)
# ===========================================================================

def step7_zone_fill(board):
    from engine.ecad.ops import apply_ops
    from engine.ecad import zonefill as zonefill_mod

    _section(7, "Copper Pour  (GND zone fill)")
    res = apply_ops(board, [{"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
                             "polygon": [[1, 1], [29, 1], [29, 19], [1, 19]]}])
    board = res["doc"]
    _info("Added GND pour on F.Cu (inset 1 mm from board edge)")

    filled = zonefill_mod.fill_zones(board)
    for zone in filled.get("zones", []):
        polys = zone.get("filled_polys", [])
        verts = sum(len(p) for p in polys)
        _info(f"  zone net={zone.get('net'):5s}  layer={zone.get('layer'):5s}"
              f"  -> {len(polys)} polygon(s), {verts} vertices")
    board["zones"] = filled.get("zones", board.get("zones", []))
    _pass("Copper pour computed (clipped to outline, cleared around other-net copper)")
    return board


# ===========================================================================
# Step 8 -- DRC
# ===========================================================================

def step8_drc(board):
    from engine.ecad.drc import run_drc

    _section(8, "Design Rule Check (DRC)")
    violations = run_drc(board)
    errors   = [v for v in violations if v["severity"] == "error"]
    warnings = [v for v in violations if v["severity"] == "warning"]
    _info(f"Result: {len(errors)} errors, {len(warnings)} warnings")
    if errors:
        for v in errors[:5]:
            _info(f"  [ERROR] {v['rule']}: {v['message']}")
    sample_warns = [w for w in warnings[:3] if w.get("rule") != "unrouted"]
    if sample_warns:
        _info("  Non-unrouted warnings (sample):")
        for v in sample_warns:
            _info(f"    [WARN]  {v['rule']}: {v['message']}")
    if not errors:
        _pass("No DRC clearance/width errors")
    else:
        _info("DRC errors present -- see above")


# ===========================================================================
# Step 9 -- Fabrication outputs
# ===========================================================================

def step9_fab(board, tmpdir):
    from engine.ecad.fab import write_gerbers, write_drill, write_bom, write_placement

    _section(9, "Fabrication Outputs  (Gerber + Drill + BOM + CPL)")

    gerber_dir = os.path.join(tmpdir, "gerbers")
    os.makedirs(gerber_dir, exist_ok=True)
    files = write_gerbers(board, gerber_dir)
    _info(f"Gerber files ({len(files)}):")
    for layer, fpath in sorted(files.items()):
        size = os.path.getsize(fpath)
        _info(f"  {layer:14s} -> {os.path.basename(fpath):<30s} ({size:,} B)")

    drill_dir = os.path.join(tmpdir, "drill")
    os.makedirs(drill_dir, exist_ok=True)
    drill_files = write_drill(board, drill_dir)
    _info(f"Drill files ({len(drill_files)}):")
    for label, fpath in sorted(drill_files.items()):
        size = os.path.getsize(fpath)
        _info(f"  {label:14s} -> {os.path.basename(fpath):<30s} ({size:,} B)")

    bom_csv = write_bom(board, fmt="csv")
    _info("BOM (CSV):")
    for line in bom_csv.splitlines():
        _info(f"  {line}")

    try:
        cpl_csv = write_placement(board, fmt="csv")
        _info("CPL / pick-and-place (CSV, first 4 lines):")
        for line in cpl_csv.splitlines()[:4]:
            _info(f"  {line}")
        _pass("Gerber + drill + BOM + CPL written")
    except Exception as exc:
        _info(f"CPL: {type(exc).__name__}: {exc}")
        _pass("Gerber + drill + BOM written (CPL optional)")


# ===========================================================================
# Step 10 -- 3D STEP (OCP-guarded)
# ===========================================================================

def step10_threed(board, tmpdir):
    _section(10, "3D STEP Export  (OCP / build123d -- guarded)")
    step_path = os.path.join(tmpdir, "board.step")
    try:
        from engine.ecad.threed import board_to_step, board_to_part_doc
        result = board_to_step(board, step_path, opts={"components": False, "copper": False})
        summary = result["summary"]
        size = os.path.getsize(step_path)
        _info(f"STEP file:    {result['step_path']}")
        _info(f"File size:    {size:,} B")
        _info(f"Board volume: {summary['board_volume']:.2f} mm^3  "
              f"(~960 mm^3 expected for 30x20x1.6 mm)")
        _info(f"Outline area: {summary['outline_area']:.2f} mm^2  (expected 600 mm^2)")
        _info(f"Thickness:    {summary['thickness']:.2f} mm")
        if summary.get("warnings"):
            _info(f"Warnings: {summary['warnings']}")

        part_doc = board_to_part_doc(board, step_path)
        # Structural self-check (the doc compiles/validates in the C++ core now; the Python
        # doc engine is gone, so verify the envelope shape here instead).
        ok = (isinstance(part_doc, dict)
              and part_doc.get("schema") == "aicad.part/1"
              and isinstance(part_doc.get("features"), list)
              and len(part_doc["features"]) >= 1)
        _info(f"board_to_part_doc envelope: {'OK' if ok else 'MALFORMED'}")
        _info(f"part_doc features: {[f['type'] for f in part_doc['features']]}")

        _pass("3D STEP exported and part_doc built")
    except ImportError as e:
        _skip(f"OCP/build123d not available: {e}")
    except Exception as e:
        _skip(f"3D export skipped: {type(e).__name__}: {e}")


# ===========================================================================
# Step 11 -- Interop: .ecad+ round-trip + KiCad inline import
# ===========================================================================

def step11_interop(doc, tmpdir):
    """Write the schematic as a .ecad+ container, reopen it, verify the doc round-trips.
    Optionally import a tiny inline KiCad schematic string.
    """
    from engine.ecad import formats

    _section(11, "Interop  (.ecad+ round-trip + KiCad inline import)")

    # .ecad+ save + reopen
    ecad_path = os.path.join(tmpdir, "divider.ecad+")
    saved_path = formats.save(doc, ecad_path)
    size = os.path.getsize(saved_path)
    _info(f"Saved .ecad+: {os.path.basename(saved_path)}  ({size:,} B)")

    reopened_doc, warns = formats.open(saved_path)
    n_syms = len(reopened_doc.get("symbols", []))
    n_labels = len(reopened_doc.get("labels", []))
    _info(f"Reopened:  schema={reopened_doc.get('schema')}  "
          f"symbols={n_syms}  labels={n_labels}  "
          f"warnings={warns or '(none)'}")
    if reopened_doc.get("schema", "").startswith("aicad.sch"):
        _pass(".ecad+ round-trip: schematic schema preserved")
    else:
        _info(f".ecad+ round-trip: schema={reopened_doc.get('schema')!r}")

    # KiCad inline schematic import (minimal 2-component stub)
    kicad_sch_text = """\
(kicad_sch (version 20231120) (generator kicad)
  (lib_symbols
    (symbol "Device:R"
      (pin passive line (at 1.016 0 180) (length 0) (name "~") (number "1"))
      (pin passive line (at -1.016 0 0)  (length 0) (name "~") (number "2"))
    )
  )
  (symbol (lib_id "Device:R") (at 0 0 0) (unit 1)
    (property "Reference" "R1") (property "Value" "1k")
  )
  (symbol (lib_id "Device:R") (at 5 0 0) (unit 1)
    (property "Reference" "R2") (property "Value" "2k")
  )
)
"""
    try:
        kicad_doc = formats.import_kicad_sch(kicad_sch_text)
        n_kicad = len(kicad_doc.get("symbols", []))
        _info(f"KiCad inline import: {n_kicad} component(s) parsed  "
              f"schema={kicad_doc.get('schema')}")
        _pass(f"KiCad import: {n_kicad} components")
    except Exception as e:
        _info(f"KiCad import: {type(e).__name__}: {e}")


# ===========================================================================
# main
# ===========================================================================

def main() -> None:
    print()
    _hr("AI-ECAD Full-Pipeline Demo")
    _info("Circuit: 5V resistive voltage divider")
    _info("  V1(5V) -> R1(10kOhm) -> VMID -> R2(10kOhm) -> GND")
    _info("  Expected V(VMID) = 2.5 V  (R1=R2 by symmetry)")
    _hr()

    # Step 1 -- AI orchestrator (recipe fallback)
    run = step1_ai_design()

    # Step 2 -- Build schematic
    doc = step2_schematic()

    # Step 3 -- Netlist + ERC
    nl = step3_netlist_erc(doc)

    # Step 4 -- Simulation suite
    step4_simulate_suite(nl)

    # Step 5 -- PCB: sch->PCB sync + manual fallback
    board = step5_pcb_from_sch(doc, nl)

    # Step 6 -- Autoroute
    board = step6_autoroute(board)

    # Step 7 -- Zone fill
    board = step7_zone_fill(board)

    # Step 8 -- DRC
    step8_drc(board)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 9 -- Fabrication
        step9_fab(board, tmpdir)

        # Step 10 -- 3D STEP (OCP-guarded)
        step10_threed(board, tmpdir)

        # Step 11 -- Interop (.ecad+ + KiCad)
        step11_interop(doc, tmpdir)

    print()
    _hr("Demo complete -- all steps executed, exit 0")
    print()


if __name__ == "__main__":
    main()
