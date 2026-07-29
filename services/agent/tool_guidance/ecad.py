"""tool_guidance.ecad — curated options for the ecad tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "ecad_lint_schematic": {
        "prompt_note": (
            "Geometry self-check BEFORE ecad_run_erc, which only catches "
            "electrical faults: symbol-body overlap, dangling wire ends, "
            "wires crossing a body, labels attached to nothing. Names the "
            "offending refdes/coordinates/label text. Power rails with no "
            "drawn body are skipped and listed, not guessed."
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
            "libid must come from ecad_find_part. Set value now ('1k', '5') "
            "for simulation, and keep the returned pin xy list — ecad_wire "
            "needs it."
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
            "next_actions. Drill down with detail='list'/'full' + "
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
