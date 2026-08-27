"""tool_guidance.ecad — curated options for the ecad tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "ecad_annotate": {
        "prompt_note": (
            "Use after placement or before BOM/export. Exact dialect: "
            "ecad_annotate(scope='all'|'sheet', mode='keep'|'reset', "
            "sheet='<sheet-id>'|None, doc_id=''). 'keep' fills unresolved "
            "refs; 'reset' renumbers the selected scope. Next: ecad_status "
            "to read the assigned refs."
        ),
    },
    "ecad_bom": {
        "prompt_note": (
            "Schematic BOM writer, not the PCB fabrication ecad_export. Exact "
            "dialect: ecad_bom(doc_id='', fmt='csv'|'json', "
            "exclude_dnp=True, path=None). path writes the full returned text "
            "client-side. Next: ecad_plot for the drawing deliverable."
        ),
    },
    "ecad_bus": {
        "prompt_note": (
            "Create the named bus polyline. Exact dialect: "
            "ecad_bus(name='D[0..7]', points=[[x,y],[x,y],...], doc_id=''); "
            "coordinates are schematic mm. Next: ecad_bus_entry for each used "
            "member, then ecad_lint_schematic."
        ),
    },
    "ecad_bus_entry": {
        "prompt_note": (
            "Connect one wire to one expanded bus member. Exact dialect: "
            "ecad_bus_entry(x, y, bus='D[0..7]', member='D0', doc_id=''); "
            "(x,y) is the bus-side endpoint. Next: ecad_netlist to confirm "
            "the member binding; lint catches unresolved entries."
        ),
    },
    "ecad_delete": {
        "prompt_note": (
            "Delete a schematic entity by internal id, or a symbol by ref. "
            "Exact dialect: ecad_delete(id_or_ref='w3'|'R1', doc_id=''). "
            "Use render_schematic_ascii's index rather than guessing ids. "
            "Next: read the returned net/ERC delta and re-render."
        ),
    },
    "ecad_edit_symbol": {
        "prompt_note": (
            "Edit in place by ref or symbol id; omitted slots stay unchanged. "
            "Exact dialect: ecad_edit_symbol(ref_or_id, value=None, ref=None, "
            "footprint=None, dnp=None, in_bom=None, on_board=None, "
            "datasheet=None, unit=None, angle=None, mirror=None, x=None, "
            "y=None, fields=None, doc_id=''). Next: ecad_status and read the "
            "returned pin/net/ERC readback."
        ),
    },
    "ecad_export_kicad": {
        "prompt_note": (
            "Write a KiCad 7 .kicad_sch artifact. Exact dialect: "
            "ecad_export_kicad(path='design.kicad_sch', doc_id=''). The "
            "write is client-side and returns confirmed bytes/path. Next: "
            "ecad_plot(fmt='pdf') for a human-readable deliverable."
        ),
    },
    "ecad_import_kicad": {
        "prompt_note": (
            "Import an existing .kicad_sch into the active schematic. Exact "
            "dialect: ecad_import_kicad(path, mode='merge'|'replace', "
            "doc_id=''). The source path must exist. Next: ecad_annotate, "
            "ecad_status, then ecad_lint_schematic."
        ),
    },
    "ecad_junction": {
        "prompt_note": (
            "Electrically join crossing/T wires at exact world coordinates. "
            "Exact dialect: ecad_junction(x, y, doc_id=''). Do not use it "
            "for a visual crossing that should remain separate. Next: "
            "ecad_netlist to confirm membership."
        ),
    },
    "ecad_netlist": {
        "prompt_note": (
            "Compiled connectivity readback. Exact dialect: "
            "ecad_netlist(doc_id='', fmt='summary'|'json'|'kicad', "
            "path=None). summary is bounded names/counts; json is the full "
            "structured netlist; kicad uses the native writer and path writes "
            "the full text. Next: ecad_bom and ecad_plot."
        ),
    },
    "ecad_no_connect": {
        "prompt_note": (
            "Mark one intentionally open pin at its exact world xy from the "
            "placed pin list. Exact dialect: ecad_no_connect(x, y, doc_id=''). "
            "Never use it to hide an unknown connection. Next: ecad_status "
            "and confirm that specific pin clears honestly."
        ),
    },
    "ecad_note": {
        "prompt_note": (
            "Add non-electrical sheet text. Exact dialect: "
            "ecad_note(x, y, text, size=1.27, doc_id=''). It does not name or "
            "connect a net; use ecad_label for that. Next: ecad_plot to verify "
            "the note in the deliverable."
        ),
    },
    "ecad_plot": {
        "prompt_note": (
            "Produce the deliverable vector drawing. Exact dialect: "
            "ecad_plot(doc_id='', fmt='svg'|'pdf', path=None, sheet=None, "
            "title_block=True). It always writes confirmed artifact path(s); "
            "sheet=None plots all sheets. Next: include returned paths/bytes "
            "in the handoff."
        ),
    },
    "ecad_symbols": {
        "prompt_note": (
            "Native catalog discovery; use this instead of guessing libids. "
            "Exact dialect: ecad_symbols(query='', limit=20), limit 1..100. "
            "Results carry exact libid/pin metadata. Next: feed an exact libid "
            "to ecad_place_symbol."
        ),
    },
    "ecad_lint_schematic": {
        "prompt_note": (
            "Drawing/bus self-check BEFORE ecad_run_erc, which catches "
            "electrical faults: symbol overlap, dangling ends, wires through "
            "bodies, floating labels, unresolved bus entries, bus labels "
            "without member wires, and unnamed buses. Names exact ids/xy/text; "
            "next action is fix each finding and rerun lint."
        ),
    },
    "ecad_make_part": {
        "prompt_note": (
            "Creates a real symbol+footprint from a pinout YOU supply — "
            "this is what ecad_find_part routes to when it refuses to "
            "substitute a >3-pin part. Requires name + pins (each "
            "{number,name,type,side}) + package "
            "(chip/soic/qfn/qfp/sot/dip/header/module_castellated) with its "
            "pitch/body dims. It will NOT invent a pinout from a part name: "
            "no pins means refusal. Pin type must be the exact KiCad enum — "
            "'power'/'vcc'/'gnd' are refused as ambiguous, use power_in or "
            "power_out. Then ecad_place_symbol by the returned libid."
        ),
    },
    "ecad_route_pad_to_pad": {
        "prompt_note": (
            "Hand-routes the nets ecad_autoroute reported as failed. Needs "
            "from_ref/from_pad/to_ref/to_pad. The net is READ off the pads "
            "— pads on different nets are refused, never shorted. Width "
            "comes from the ecad_stackup_solve net class unless you pass "
            "width. Set to_layer to change layer and it inserts a via. Real "
            "tracks, so ecad_run_drc sees them."
        ),
    },
    "board_from_schematic": {
        "prompt_note": (
            "One-call layout on the active wired schematic — pass ERC first; "
            "read residual_drc honestly, then ecad_run_dfm / ecad_run_pdn."
        ),
        "timeout_s": 300,
    },
    "ecad_add_power": {
        "prompt_note": (
            "Drop on a pin's world xy (from ecad_place_symbol's pin list) to "
            "bond it to the rail wire-free; undriven-power ERC clears only "
            "via a port."
        ),
    },
    "ecad_add_zone": {
        "prompt_note": (
            "Common case: net='GND' with no geometry floods the board "
            "outline. The pour is preview-only until ecad_fill_zones — fill "
            "before export/DRC."
        ),
    },
    "ecad_airflow_handoff": {
        "prompt_note": (
            "Minutes-long OpenFOAM CHT — only when convection presets look "
            "wrong (enclosure/fan). dry_run=True inspects the case; verify "
            "after with ecad_verify_airflow."
        ),
        "timeout_s": 600,
    },
    "ecad_autoplace": {
        "prompt_note": (
            "Run after ecad_update_from_sch and before routing; "
            "deterministic at seed 0. board_from_schematic already includes "
            "this step."
        ),
    },
    "ecad_autoroute": {
        "prompt_note": (
            "Needs a ratsnest — ecad_update_from_sch first. Failed nets are "
            "named; give them room or route by hand. ecad_optimize_traces "
            "polishes after."
        ),
        "timeout_s": 180,
    },
    "ecad_check_diff_impedance": {
        "prompt_note": (
            "Both nets must already be routed. To CHANGE geometry toward a "
            "target, solve first with ecad_stackup_solve — this only "
            "measures."
        ),
    },
    "ecad_check_impedance": {
        "prompt_note": (
            "Omit net_or_ref to scan all routed nets for "
            "controlled-impedance candidates. Same IPC-2141A models "
            "ecad_stackup_solve inverts, so solved traces read back "
            "on-target."
        ),
    },
    "ecad_design": {
        "prompt_note": (
            "Front door for a new circuit: plain-English request -> "
            "verified schematic + sim, offline via recipes. It becomes the "
            "active doc; board_from_schematic is next."
        ),
        "timeout_s": 300,
    },
    "ecad_export": {
        "prompt_note": (
            "Run ecad_fill_zones first so Gerbers carry the true pour. "
            "kind='constraints' emits FPGA XDC/LPF — pair with "
            "ecad_fpga_check."
        ),
    },
    "ecad_fill_zones": {
        "prompt_note": (
            "Required before Gerber export — the post-edit preview fill is "
            "coarse and not what ships. Re-run after any copper edit."
        ),
    },
    "ecad_find_part": {
        "prompt_note": (
            "Always search before placing — never invent a libid/fpid. Feed "
            "the exact libid of a hit to ecad_place_symbol."
        ),
    },
    "ecad_fpga_check": {
        "prompt_note": (
            "Schematic-side (not board) bank/pin checks; pass io= to declare "
            "IOSTANDARDs or VCCIO inference is a guess. Export constraints "
            "after via ecad_export."
        ),
    },
    "ecad_label": {
        "prompt_note": (
            "Same text+scope merges nets — the clean wire-free connect "
            "across a sheet (scope='global' crosses sheets). Names feed "
            "ERC, netlist, and SPICE probes."
        ),
    },
    "ecad_make_board": {
        "prompt_note": (
            "One-shot sync->route->fill->DRC from the active schematic "
            "(pass doc_id when several are open); hostcheck=True folds in "
            "the MCU rule pack."
        ),
        "timeout_s": 300,
    },
    "ecad_new_pcb": {
        "prompt_note": (
            "Manual board path: new PCB -> footprints (fpids via "
            "ecad_find_part) -> route -> ecad_run_drc. For a "
            "schematic-driven board use board_from_schematic instead."
        ),
    },
    "ecad_new_schematic": {
        "prompt_note": (
            "Becomes the active doc — omit doc_id afterwards. Next moves: "
            "ecad_find_part, then ecad_place_symbol with value set."
        ),
    },
    "ecad_optimize_traces": {
        "prompt_note": (
            "Post-route polish: run after autoroute, before fill/DRC. Pass "
            "targets={net: ohms} to retune widths; idempotent and "
            "DRC-guarded, safe to re-run."
        ),
        "timeout_s": 180,
    },
    "ecad_place_symbol": {
        "prompt_note": (
            "libid must come from ecad_symbols or ecad_find_part. Set value "
            "now ('1k', '5') for simulation, and keep the returned pin xy "
            "list — ecad_wire needs it."
        ),
    },
    "ecad_run_dfm": {
        "prompt_note": (
            "Fab-readiness pass once DRC is clean; fix courtyard_overlap "
            "first, then drill_min. Advisory — warnings never block "
            "editing."
        ),
    },
    "ecad_run_drc": {
        "prompt_note": (
            "Advisory spacing/size check after routing — the by_rule "
            "grouping says widen, move, or re-space. Pair with "
            "ecad_run_dfm before export."
        ),
    },
    "ecad_run_erc": {
        "prompt_note": (
            "Schematic gate before board sync: errors (output shorts, "
            "undriven power) are real bugs; warnings are usually unwired "
            "pins. Never blocks edits."
        ),
    },
    "ecad_run_hostcheck": {
        "prompt_note": (
            "Catches board-bricking MCU/FPGA issues DRC can't (decoupling, "
            "sequencing, crystal, straps); needs eng metadata — seed via "
            "sim_params.eng."
        ),
    },
    "ecad_run_pdn": {
        "prompt_note": (
            "DC IR-drop on routed copper. Currents come from rail metadata "
            "or sink_overrides — unknown draws are reported, never "
            "modelled as 0 A."
        ),
        "timeout_s": 180,
    },
    "ecad_run_sicheck": {
        "prompt_note": (
            "Fires only from ecad_set_net_class declarations — declare "
            "match/skew/termination first or nothing runs. Heuristics, not "
            "a field solver."
        ),
    },
    "ecad_set_net_class": {
        "prompt_note": (
            "THE SI-rule declaration path (params merge additively, same "
            "schema ecad_stackup_solve writes); ecad_run_sicheck reads "
            "these classes."
        ),
    },
    "ecad_simulate": {
        "prompt_note": (
            "Start with 'op', then tran/ac; omitting probes returns a "
            "bounded ~12-node set. ngspice unlocks vendor models but "
            "degrades honestly to mna."
        ),
        "timeout_s": 180,
    },
    "ecad_stackup_solve": {
        "prompt_note": (
            "Solve BEFORE routing so autoroute/DRC inherit solved "
            "widths/gaps via net classes; net_map binds existing nets. "
            "Works boardless for design-ahead."
        ),
    },
    "ecad_status": {
        "prompt_note": (
            "First call when unsure — compact counts plus prioritized "
            "next_actions. erc_available=false means ERC failed and is NEVER "
            "a clean result. Drill down with detail='list'/'full' + "
            "include='pins,members' only when wiring."
        ),
    },
    "ecad_sweep": {
        "prompt_note": (
            "One parameter, explicit values list (no start/stop/step); "
            "re-runs the analysis per value — keep the list short on "
            "transient runs."
        ),
        "timeout_s": 180,
    },
    "ecad_tf": {
        "prompt_note": (
            "DC gain + Rin + Rout in one shot — the amp gain/loading "
            "check. backend='ngspice' only matters for vendor .SUBCKT "
            "parts."
        ),
    },
    "ecad_thermal_handoff": {
        "prompt_note": (
            "Board->thermal FEA in one call; unpowered components get no "
            "source (coverage.unpowered_refs). dry_run inspects BCs; "
            "ecad_verify_thermal gives the verdict."
        ),
        "timeout_s": 300,
    },
    "ecad_to_3d": {
        "prompt_note": (
            "Run once the board has an outline; writes STEP plus the "
            "boardphys sidecar that thermal/PDN/power_budget consume. "
            "Import the STEP for enclosure design."
        ),
    },
    "ecad_update_from_sch": {
        "prompt_note": (
            "The schematic->board sync: run after ERC-clean wiring, before "
            "routing; re-run to re-sync after schematic edits. Sim "
            "sources/power symbols skip by design."
        ),
    },
    "ecad_verify_airflow": {
        "prompt_note": (
            "Follow-up to ecad_airflow_handoff (uses the session's last "
            "solves by default); an optimistic preset returns an "
            "effective-h to re-run with."
        ),
    },
    "ecad_verify_thermal": {
        "prompt_note": (
            "Verdict step after ecad_thermal_handoff: Tj vs limits + "
            "theta_ja cross-check. The 75% tolerance is deliberate — "
            "catches setup blunders, not layout accuracy."
        ),
    },
    "ecad_vibe_check": {
        "prompt_note": (
            "Screening, not sign-off: default flat PSD is a NAVMAT-class "
            "level — pass psd_breakpoints for a spec-true run. dry_run "
            "shows mounts/mass model first."
        ),
        "timeout_s": 300,
    },
    "ecad_wire": {
        "prompt_note": (
            "Pins accept numbers or names from ecad_place_symbol's pin "
            "list; confirm the join in the returned net_members. For rails "
            "prefer ecad_add_power."
        ),
    },
    "heatsink_select": {
        "prompt_note": (
            "Run ecad_thermal_handoff FIRST — this re-solves on its "
            "cached mesh, one solve per candidate. Impossible targets "
            "name the best achievable, never fake a win."
        ),
        "timeout_s": 600,
    },
    "power_budget": {
        "prompt_note": (
            "Bookkeeping, not SPICE: feed board_path from ecad_to_3d's "
            "sidecar and power_profile_path from fw_power_profile, else "
            "it runs degraded (labelled)."
        ),
    },
}
