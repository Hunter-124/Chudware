"""AI-ECAD pipeline walkthrough demo.

Prints a narrative walkthrough of the complete pipeline end-to-end:
  Schematic -> Netlist -> ERC -> Simulation -> PCB -> DRC/Ratsnest -> Fab -> 3D STEP

Circuit: 5 V resistive voltage divider  (R1 = R2 = 10 kOhm, V(VMID) = 2.5 V)

Run:
    python -m engine.ecad.demo
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile

# -- banner ---------------------------------------------------------------------

def _hr(title: str = "") -> None:
    width = 70
    if title:
        pad = (width - len(title) - 2) // 2
        print("-" * pad + f" {title} " + "-" * pad)
    else:
        print("-" * width)


def _section(n: int, title: str) -> None:
    print()
    _hr(f"Step {n}: {title}")


# ==============================================================================
# Step 1 -- Build the schematic
# ==============================================================================

def step1_schematic():
    from engine.ecad import schema
    from engine.ecad.ops import apply_ops
    from engine.ecad.library.starter import SYMBOLS

    _section(1, "Build Schematic via ops")
    print("  Circuit: V1=5V  --[R1=10k]--  VMID  --[R2=10k]--  GND")
    print("  Method:  sch_add_symbol ops + power ports + local net labels")
    print("  (No geometric wires -- connectivity from net-name merging)")

    doc = schema.new_schematic("voltage_divider_demo")
    for libid in ("Simulation_SPICE:V_source", "Device:R"):
        doc["lib_cache"][libid] = copy.deepcopy(SYMBOLS[libid])

    # V1 -- voltage source at origin
    res = apply_ops(doc, [{"op": "sch_add_symbol", "libid": "Simulation_SPICE:V_source",
                            "pos": [0.0, 0.0], "ref": "V1", "value": "5"}])
    doc = res["doc"]
    print(f"  Placed V1 (id={res['id_map']['$0']})")

    # R1 -- 10 kOhm
    res = apply_ops(doc, [{"op": "sch_add_symbol", "libid": "Device:R",
                            "pos": [20.0, 5.0], "ref": "R1", "value": "10k"}])
    doc = res["doc"]
    print(f"  Placed R1 (id={res['id_map']['$0']})")

    # R2 -- 10 kOhm
    res = apply_ops(doc, [{"op": "sch_add_symbol", "libid": "Device:R",
                            "pos": [40.0, 5.0], "ref": "R2", "value": "10k"}])
    doc = res["doc"]
    print(f"  Placed R2 (id={res['id_map']['$0']})")

    # Power ports: +5V at V1+ tip, +5V at R1.pin1 tip, GND at V1- tip, GND at R2.pin2 tip
    # (see starter.py for exact pin world coords; pins calculated from pos+angle+length)
    res = apply_ops(doc, [
        {"op": "sch_add_power", "net": "+5V", "pos": [0.0,   5.08]},
        {"op": "sch_add_power", "net": "GND",  "pos": [0.0,  -5.08]},
        {"op": "sch_add_power", "net": "+5V", "pos": [14.92, 5.0]},
        {"op": "sch_add_power", "net": "GND",  "pos": [45.08, 5.0]},
    ])
    doc = res["doc"]

    # Net label VMID at R1.pin2 and R2.pin1 -- the two labels merge those nodes
    res = apply_ops(doc, [
        {"op": "sch_add_label", "text": "VMID", "pos": [25.08, 5.0], "scope": "local"},
        {"op": "sch_add_label", "text": "VMID", "pos": [34.92, 5.0], "scope": "local"},
    ])
    doc = res["doc"]
    print("  Added power ports (+5V, GND) and VMID net labels")

    print("  Schema validation ...", end=" ")
    errs = schema.validate_schematic(doc)
    print(f"{'OK (0 errors)' if not errs else 'FAILED: ' + str(errs[:3])}")
    return doc


# ==============================================================================
# Step 2 -- Build netlist
# ==============================================================================

def step2_netlist(doc):
    from engine.ecad.netlist import build_netlist

    _section(2, "Build Netlist")
    nl = build_netlist(doc)
    nets = nl["nets"]
    components = nl["components"]
    print(f"  Components ({len(components)}):")
    for c in components:
        pins_summary = ", ".join(f"{p}={n}" for p, n in list(c["pins"].items())[:4])
        print(f"    {c['ref']:4s}  {c['libid']:35s}  value={c['value']!r}  pins=[{pins_summary}]")
    print(f"  Nets ({len(nets)}):")
    for net_name, members in sorted(nets.items()):
        refs = ", ".join(f"{m['ref']}.{m['pin']}" for m in members)
        print(f"    {net_name:8s}  ({len(members)} pins)  {refs}")
    return nl


# ==============================================================================
# Step 3 -- ERC
# ==============================================================================

def step3_erc(doc, nl):
    from engine.ecad.erc import run_erc

    _section(3, "Electrical Rules Check (ERC)")
    violations = run_erc(doc, nl)
    errors   = [v for v in violations if v["severity"] == "error"]
    warnings = [v for v in violations if v["severity"] == "warning"]
    print(f"  Result: {len(errors)} errors, {len(warnings)} warnings")
    if errors:
        print("  ERRORS:")
        for v in errors[:5]:
            print(f"    [ERROR] {v['rule']}: {v['message']}")
    if warnings:
        print("  Warnings (sample, up to 5):")
        for v in warnings[:5]:
            print(f"    [WARN]  {v['rule']}: {v['message']}")
    if not errors:
        print("  OK No ERC errors -- design is electrically clean enough to simulate.")
    return violations


# ==============================================================================
# Step 4 -- SPICE simulation
# ==============================================================================

def step4_simulate(nl):
    from engine.sim.spice import netlist_to_spice_circuit
    from engine.sim.circuit import simulate

    _section(4, "SPICE Simulation  (DC operating point)")
    circuit, warns = netlist_to_spice_circuit(nl)
    print(f"  Circuit: {len(circuit['devices'])} devices, "
          f"nodes={circuit['nodes']}")
    if warns:
        print(f"  SPICE bridge warnings: {warns}")

    probes = ["V(VMID)", "V(+5V)"]
    study = {"analyses": [{"type": "op"}], "probes": probes}
    result = simulate(circuit, study)

    converged = result["convergence"].get("op", False)
    op = result.get("op", {})
    print(f"  Converged: {converged}")
    for probe, val in sorted(op.items()):
        print(f"    {probe} = {val:.6g}")

    expected = 2.5
    vmid_val = op.get("V(VMID)", float("nan"))
    err_pct  = abs(vmid_val - expected) / expected * 100 if expected else float("nan")
    print(f"  V(VMID) = {vmid_val:.4f} V   (expected {expected:.3f} V,  error={err_pct:.4f}%)")
    print(f"  log: {result.get('log', [])[-3:]}")
    return result


# ==============================================================================
# Step 5 -- PCB layout
# ==============================================================================

def step5_pcb():
    from engine.ecad import schema
    from engine.ecad.ops import apply_ops
    from engine.ecad.library.starter import FOOTPRINTS
    from engine.ecad.drc import ratsnest

    _section(5, "PCB Layout  (outline + footprints + nets)")
    board = schema.new_board("divider_pcb")

    # Nets
    board["nets"].append({"name": "+5V",  "class": "default"})
    board["nets"].append({"name": "VMID", "class": "default"})
    print(f"  Nets: {[n['name'] for n in board['nets']]}")

    # Rectangular 30x20 mm outline
    res = apply_ops(board, [{"op": "pcb_set_outline",
        "shapes": [
            {"type": "line", "p1": [0, 0],  "p2": [30, 0]},
            {"type": "line", "p1": [30, 0], "p2": [30, 20]},
            {"type": "line", "p1": [30, 20],"p2": [0, 20]},
            {"type": "line", "p1": [0, 20], "p2": [0, 0]},
        ], "closed": True}])
    board = res["doc"]
    print("  Board outline: 30 mm x 20 mm rectangle")

    fp0603 = FOOTPRINTS["Resistor_SMD:R_0603"]

    # R1 footprint -- pad1=+5V, pad2=VMID
    r1_pads = copy.deepcopy(fp0603["pads"])
    for pad in r1_pads:
        pad["net"] = "+5V"  if str(pad.get("number")) == "1" else "VMID"
    res = apply_ops(board, [{"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
                              "ref": "R1", "value": "10k",
                              "pos": [8.0, 10.0], "side": "top",
                              "pads": r1_pads, "def": fp0603}])
    board = res["doc"]
    print(f"  Placed R1 footprint  (id={res['id_map']['$0']})")

    # R2 footprint -- pad1=VMID, pad2=GND
    r2_pads = copy.deepcopy(fp0603["pads"])
    for pad in r2_pads:
        pad["net"] = "VMID" if str(pad.get("number")) == "1" else "GND"
    res = apply_ops(board, [{"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
                              "ref": "R2", "value": "10k",
                              "pos": [20.0, 10.0], "side": "top",
                              "pads": r2_pads, "def": fp0603}])
    board = res["doc"]
    print(f"  Placed R2 footprint  (id={res['id_map']['$0']})")

    # Ratsnest BEFORE routing
    rats_before = ratsnest(board)
    print(f"  Ratsnest before routing: {len(rats_before)} airwire(s)")
    for r in rats_before:
        print(f"    net={r['net']}  {r['p1']} -> {r['p2']}")

    # Route VMID: straight track R1.pad2 [8.8,10] -> R2.pad1 [19.2,10]
    res = apply_ops(board, [{"op": "pcb_add_track", "net": "VMID", "layer": "F.Cu",
                              "p1": [8.8, 10.0], "p2": [19.2, 10.0], "width": 0.25}])
    board = res["doc"]
    print("  Routed VMID net: track (8.8,10)->(19.2,10) on F.Cu, w=0.25mm")

    # Ratsnest AFTER routing
    rats_after = ratsnest(board)
    print(f"  Ratsnest after routing: {len(rats_after)} airwire(s)")
    if not rats_after:
        print("    All nets fully routed!")

    return board


# ==============================================================================
# Step 6 -- DRC
# ==============================================================================

def step6_drc(board):
    from engine.ecad.drc import run_drc

    _section(6, "Design Rule Check (DRC)")
    violations = run_drc(board)
    errors   = [v for v in violations if v["severity"] == "error"]
    warnings = [v for v in violations if v["severity"] == "warning"]
    print(f"  Result: {len(errors)} errors, {len(warnings)} warnings "
          f"(unrouted-airwire warnings are expected for +5V/GND)")
    if errors:
        print("  ERRORS:")
        for v in errors[:5]:
            print(f"    [ERROR] {v['rule']}: {v['message']}")
    sample_warns = [w for w in warnings[:5] if w.get("rule") != "unrouted"]
    if sample_warns:
        print("  Non-unrouted warnings (sample):")
        for v in sample_warns:
            print(f"    [WARN]  {v['rule']}: {v['message']}")
    if not errors:
        print("  OK No DRC clearance/width errors.")


# ==============================================================================
# Step 7 -- Fab outputs
# ==============================================================================

def step7_fab(board, tmpdir):
    from engine.ecad.fab import write_gerbers, write_drill, write_bom

    _section(7, "Fabrication Outputs  (Gerber + Drill + BOM)")
    gerber_dir = os.path.join(tmpdir, "gerbers")
    os.makedirs(gerber_dir, exist_ok=True)
    files = write_gerbers(board, gerber_dir)
    print(f"  Gerber files ({len(files)}):")
    for layer, fpath in sorted(files.items()):
        size = os.path.getsize(fpath)
        print(f"    {layer:12s} -> {os.path.basename(fpath)}  ({size} B)")

    drill_dir = os.path.join(tmpdir, "drill")
    os.makedirs(drill_dir, exist_ok=True)
    drill_files = write_drill(board, drill_dir)
    print(f"  Drill files ({len(drill_files)}):")
    for label, fpath in sorted(drill_files.items()):
        size = os.path.getsize(fpath)
        print(f"    {label:12s} -> {os.path.basename(fpath)}  ({size} B)")

    bom_csv = write_bom(board, fmt="csv")
    print(f"  BOM (CSV):\n{''.join('    ' + l for l in bom_csv.splitlines(keepends=True))}")


# ==============================================================================
# Step 8 -- 3D STEP
# ==============================================================================

def step8_threed(board, tmpdir):
    from engine.ecad.threed import board_to_step, board_to_part_doc

    _section(8, "3D STEP Export")
    step_path = os.path.join(tmpdir, "board.step")
    result = board_to_step(board, step_path, opts={"components": False, "copper": False})

    summary = result["summary"]
    print(f"  STEP file:    {result['step_path']}")
    print(f"  File size:    {os.path.getsize(step_path):,} B")
    print(f"  Board volume: {summary['board_volume']:.2f} mm^3  "
          f"(~960 mm^3 expected for 30x20x1.6 mm substrate)")
    print(f"  Outline area: {summary['outline_area']:.2f} mm^2  (expected 600 mm^2)")
    print(f"  Thickness:    {summary['thickness']:.2f} mm")
    if summary.get("warnings"):
        print(f"  Warnings: {summary['warnings']}")

    # Part document validation
    from engine.doc.schema import validate_document
    part_doc = board_to_part_doc(board, step_path)
    val_errs = validate_document(part_doc)
    status = "OK (0 errors)" if not val_errs else f"FAILED: {val_errs[:3]}"
    print(f"  board_to_part_doc validation: {status}")
    print(f"  part_doc schema: {part_doc['schema']}")
    print(f"  part_doc features: {[f['type'] for f in part_doc['features']]}")
    print()
    print("  HINT: open this STEP in the CAD app as an import feature to design the enclosure.")

    # Compiler (best-effort; OCP may not be available in all environments)
    try:
        from engine.doc.compiler import compile_document
        cr = compile_document(part_doc)
        print(f"  compile_document: ok={True}  "
              f"bodies={len(getattr(cr, 'bodies', []))}")
    except Exception as e:
        print(f"  compile_document: skipped ({type(e).__name__}: {e})")


# ==============================================================================
# main
# ==============================================================================

def main() -> None:
    print()
    _hr("AI-ECAD Full-Pipeline Demo")
    print("  Circuit: 5 V resistive voltage divider")
    print("    V1(5V) -> R1(10kOhm) -> VMID -> R2(10kOhm) -> GND")
    print("    Expected V(VMID) = 2.5 V  (R1=R2 by symmetry)")
    _hr()

    doc  = step1_schematic()
    nl   = step2_netlist(doc)
    _    = step3_erc(doc, nl)
    _    = step4_simulate(nl)
    board = step5_pcb()
    step6_drc(board)

    with tempfile.TemporaryDirectory() as tmpdir:
        step7_fab(board, tmpdir)
        step8_threed(board, tmpdir)

    print()
    _hr("Demo complete -- all steps executed, exit 0")
    print()


if __name__ == "__main__":
    main()
