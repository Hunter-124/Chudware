"""mission_criteria.py — evidence-gradeable AC declarations for multi-domain missions.

mega-11: AC2/AC4–AC9 were manual/prose in mega-9/10 manifests. This module is the
single source of check.tool + pass_rule shapes that ``reviewer_exec.evaluate_pass_rule``
can grade from real tool evidence (no silent weaken).

Mega run scripts (reports/runs/mega-N/run_mega.py) SHOULD import
``rocket_plane_flight_computer_acs`` / ``apply_rocket_plane_checks`` rather than
inlining manual criteria.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


# Default legs for the flight-computer + airframe mission report card (AC9).
ROCKET_PLANE_LEGS: list[str] = [
    "plan",
    "parts",
    "schematic",
    "sim",
    "board",
    "trace_opt",
    "antenna",
    "checks",
    "firmware",
    "sensor_loop",
    "power",
    "airframe",
]


def rocket_plane_flight_computer_acs() -> dict[str, list[dict[str, Any]]]:
    """Return ``{deliverable_id: [acceptance_criterion, ...]}`` for the FC rocket mission.

    Shapes match reports/runs/mega-10/fixes.md §AC1–AC9 gradeability proposals:
      AC1 verify_battery / PASS
      AC2 measure_part|query_geometry / bay envelope >= board outline (mechanical)
      AC3 check_interference / interferences empty or volume==0
      AC4 ecad_run_drc+ecad_run_erc / errors==0
      AC5 ecad_simulate / V(3V3) in [3.135, 3.465]
      AC6 ecad_antenna_analyze / PASS|SKIP with provision note
      AC7 fw_build / returncode==0
      AC8 fw_sim_run / PASS|SKIP(renode)
      AC9 report_card / all legs have PASS|WARN|FAIL|SKIP
    """
    return {
        "DL1": [
            {
                "id": "AC1",
                "text": "airframe geometry coherent (verify_battery PASS)",
                "check": {
                    "tool": "verify_battery",
                    "params_hint": {},
                    "pass_rule": "PASS",
                },
            },
            {
                "id": "AC2",
                "text": "payload bay volume fits flight-computer board envelope",
                "check": {
                    "tool": "measure_part",
                    "params_hint": {
                        "feature": "payload_bay",
                        # Named dims the mechanical bay-vs-board rule consumes when
                        # the tool/ledger evidence carries them.
                        "bay_length": "payload_bay_length",
                        "board_length": "fc_board_length",
                    },
                    "pass_rule": "bay envelope >= board outline",
                },
            },
        ],
        "DL2": [
            {
                "id": "AC3",
                "text": "no solid interference between board and airframe",
                "check": {
                    "tool": "check_interference",
                    "params_hint": {},
                    "pass_rule": "interferences empty or volume==0",
                },
            },
        ],
        "DL3": [
            {
                "id": "AC4",
                "text": "schematic + board DRC/ERC clean (errors==0)",
                "check": {
                    "tool": "ecad_run_drc+ecad_run_erc",
                    "tools": ["ecad_run_drc", "ecad_run_erc"],
                    "params_hint": {
                        "legs": ["parts", "schematic", "board", "trace_opt", "checks"],
                    },
                    "pass_rule": "errors==0",
                },
            },
            {
                "id": "AC5",
                "text": "SPICE OP 3V3 rail within 5%",
                "check": {
                    "tool": "ecad_simulate",
                    "params_hint": {
                        "analysis": "op",
                        "probes": ["V(3V3)"],
                        "leg": "sim",
                    },
                    "pass_rule": "V(3V3) in [3.135, 3.465]",
                },
            },
            {
                "id": "AC6",
                "text": "antenna design produced (openEMS or analytic degrade documented)",
                "check": {
                    "tool": "ecad_antenna_analyze",
                    "params_hint": {
                        "kind": "ifa",
                        "freq_hz": 2.4e9,
                        "leg": "antenna",
                    },
                    "pass_rule": "PASS|SKIP",
                },
            },
        ],
        "DL4": [
            {
                "id": "AC7",
                "text": "fw_build succeeds (ELF produced)",
                "check": {
                    "tool": "fw_build",
                    "params_hint": {"leg": "firmware"},
                    "pass_rule": "returncode==0",
                },
            },
            {
                "id": "AC8",
                "text": "Renode sensor loop PASS or honest SKIP if unprovisioned",
                "check": {
                    "tool": "fw_sim_run",
                    "params_hint": {"leg": "sensor_loop"},
                    "pass_rule": "PASS|SKIP(renode)",
                },
            },
        ],
        "DL5": [
            {
                "id": "AC9",
                "text": "report lists every leg with a verdict",
                "check": {
                    "tool": "report_card",
                    "params_hint": {"legs": list(ROCKET_PLANE_LEGS)},
                    "pass_rule": "all legs have PASS|WARN|FAIL|SKIP",
                },
            },
        ],
    }


def apply_rocket_plane_checks(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied manifest with DL1–DL5 ACs replaced by gradeable shapes."""
    out = deepcopy(manifest)
    acs = rocket_plane_flight_computer_acs()
    for d in out.get("deliverables") or []:
        if not isinstance(d, dict):
            continue
        did = d.get("id")
        if did in acs:
            d["acceptance_criteria"] = deepcopy(acs[did])
    # Drop the stale "manual AC4–AC9" assumption; criteria are now tool-mapped.
    assumptions = out.get("assumptions")
    if isinstance(assumptions, list):
        kept = []
        for a in assumptions:
            if not isinstance(a, dict):
                kept.append(a)
                continue
            text = str(a.get("text") or "")
            if "Manual acceptance criteria (AC4" in text or "AC4–AC9" in text or "AC4-AC9" in text:
                continue
            kept.append(a)
        kept.append({
            "text": (
                "AC2/AC4–AC9 are evidence-graded from named tools "
                "(measure_part, ecad_run_drc+erc, ecad_simulate, ecad_antenna_analyze, "
                "fw_build, fw_sim_run, report_card). Honest SKIP only with provision-absence "
                "evidence (Renode/openEMS/toolchain)."
            ),
            "basis": "mega-11 gradeability (reports/runs/mega-10/fixes.md)",
            "needs_confirm": False,
        })
        out["assumptions"] = kept
    return out


def gradeable_ac_ids() -> list[str]:
    """AC ids that have tool-mapped mechanical pass_rules (AC1–AC9)."""
    return ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "AC7", "AC8", "AC9"]
