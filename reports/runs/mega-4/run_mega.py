#!/usr/bin/env python3
"""Mega-mission driver mega-4 (xAI grok-4.5 only). Writes under reports/runs/mega-4/.

Flight computer + model rocket plane multi-domain exercise of the infinitely-
branching subagent mode (org layer ON, spawn-depth policy active, maximum effort).
Relaunch on current modules after Fixer3: phase-gate reconciliation, delegation
on SPECIFY/PLAN/BUILD roles, and the xAI concurrency semaphore.
Auth: SuperGrok live. No source edits — harness consumer only.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)

# xAI-only; never start local models.
os.environ.setdefault("AI_CAD_ADVISOR", "1")
os.environ["AICAD_ORG_LAYER"] = "1"  # infinitely-branching subagent mode
os.environ["AICAD_OVERSEER"] = "1"  # overseer audits when available
os.environ["AICAD_BUILD_COUNCIL"] = "0"  # avoid multi-provider council (xai-only)
os.environ["AI_CAD_TIER"] = "frontier"
os.environ.setdefault("AI_CAD_ECAD_HEAVY_BUDGET_S", "180")
os.environ.setdefault("AICAD_FC_SIM_BUDGET_S", "120")

RUN_ID = "mega-4-fc-rocket-20260714"
TIER = "xai:grok-4-5"

REQUEST = (
    "Design a model rocket plane (boost-glide airframe) with an integrated central "
    "flight computer: MCU (STM32F4-class) + 3V3 buck, MPU6050 IMU, BMP280 baro, "
    "LIS3MDL mag, NEO-6M GPS. Full multi-domain mission:\n"
    "1) PLAN the system (airframe + avionics architecture, interfaces, budgets).\n"
    "2) PART SOURCING — resolve every IC/connector/fastener via the parts library "
    "and/or ecad_find_part; record BOM with provenance.\n"
    "3) SCHEMATIC — ecad_new_schematic / place / wire the flight computer.\n"
    "4) SPICE — ecad_simulate OP on the 3V3 front-end; confirm rails.\n"
    "5) PCB LAYOUT + TRACE OPT — board_from_schematic, ecad_optimize_traces, DRC/DFM.\n"
    "6) ANTENNA/RF — ecad_antenna_design + analyze for 2.4 GHz IFA and GPS patch; "
    "use openEMS path when provisioned, else document honest degrade/SKIP.\n"
    "7) FIRMWARE — fw_new_project(stm32f4) + pinmap from netlist + fw_build.\n"
    "8) RENODE SENSOR-IN-THE-LOOP — climb_turn_profile → fw_sensor_scenario → "
    "fw_sim_run + fw_verify_board; SKIP honestly if Renode absent.\n"
    "9) MECHANICAL — model the rocket-plane airframe (body tube, fins, wing/glide "
    "surface, payload bay for the FC) as editable CAD with verify_battery PASS.\n"
    "10) Deliver a consolidated mission report card (PASS/WARN/FAIL/SKIP per leg) "
    "and power_budget rollup.\n"
    "Use subagent delegation freely (infinitely-branching, spawn-depth policy active) "
    "to parallelize mechanical vs ECAD vs firmware vs RF workstreams. Autonomy: auto."
)

MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim": REQUEST,
    "deliverables": [
        {
            "id": "DL1",
            "kind": "part",
            "name": "rocket_plane_airframe",
            "description": (
                "Boost-glide model rocket plane airframe: body, fins, wing, "
                "payload bay sized for the flight-computer board."
            ),
            "acceptance_criteria": [
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
                        "params_hint": {"feature": "payload_bay"},
                        "pass_rule": "bay envelope >= board outline",
                    },
                },
            ],
            "depends_on": [],
            "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL2",
            "kind": "assembly",
            "name": "avionics_integration",
            "description": (
                "Airframe + flight-computer board integration; no interference; "
                "sensor FOV / antenna keepouts respected."
            ),
            "acceptance_criteria": [
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
            "depends_on": ["DL1", "DL3"],
            "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL3",
            "kind": "analysis",
            "name": "flight_computer_ecad",
            "description": (
                "Flight-computer E2E: part sourcing, schematic, SPICE OP, PCB "
                "place/route/trace-opt, antenna/RF, DRC/DFM/PDN checks."
            ),
            "acceptance_criteria": [
                {
                    "id": "AC4",
                    "text": "schematic + board artifacts exist with DRC clean or WARN-only",
                    "check": {
                        "tool": "manual",
                        "params_hint": {
                            "legs": [
                                "parts",
                                "schematic",
                                "board",
                                "trace_opt",
                                "checks",
                            ]
                        },
                        "pass_rule": "legs parts/schematic/board not FAIL",
                    },
                },
                {
                    "id": "AC5",
                    "text": "SPICE OP 3V3 rail within 5%",
                    "check": {
                        "tool": "manual",
                        "params_hint": {"leg": "sim"},
                        "pass_rule": "V(3V3) in [3.135, 3.465]",
                    },
                },
                {
                    "id": "AC6",
                    "text": "antenna design produced (openEMS or analytic degrade documented)",
                    "check": {
                        "tool": "manual",
                        "params_hint": {"leg": "antenna"},
                        "pass_rule": "PASS or SKIP with provision note",
                    },
                },
            ],
            "depends_on": [],
            "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL4",
            "kind": "analysis",
            "name": "firmware_sensor_loop",
            "description": (
                "STM32F4 firmware project from pinmap, build, Renode sensor-in-the-loop "
                "climb_turn_profile scenario."
            ),
            "acceptance_criteria": [
                {
                    "id": "AC7",
                    "text": "fw_build succeeds (ELF produced)",
                    "check": {
                        "tool": "manual",
                        "params_hint": {"leg": "firmware"},
                        "pass_rule": "fw_build returncode==0",
                    },
                },
                {
                    "id": "AC8",
                    "text": "Renode sensor loop PASS or honest SKIP if unprovisioned",
                    "check": {
                        "tool": "manual",
                        "params_hint": {"leg": "sensor_loop"},
                        "pass_rule": "PASS|SKIP(renode)",
                    },
                },
            ],
            "depends_on": ["DL3"],
            "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL5",
            "kind": "report",
            "name": "mission_report_card",
            "description": (
                "Consolidated PASS/WARN/FAIL/SKIP card for all legs + power_budget rollup."
            ),
            "acceptance_criteria": [
                {
                    "id": "AC9",
                    "text": "report lists every leg with a verdict",
                    "check": {
                        "tool": "manual",
                        "params_hint": {
                            "legs": [
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
                        },
                        "pass_rule": "all legs have PASS|WARN|FAIL|SKIP",
                    },
                },
            ],
            "depends_on": ["DL1", "DL2", "DL3", "DL4"],
            "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
    ],
    "ambiguities": [],
    "assumptions": [
        {
            "text": (
                "Manual acceptance criteria (AC4–AC9) are satisfied when the corresponding "
                "MCP leg tools return non-FAIL results or honest SKIP with provision notes; "
                "reviewer confirms from ledger artifacts."
            ),
            "basis": "flight_computer E2E bench protocol (benchmarks/flight_computer)",
            "needs_confirm": True,
        },
        {
            "text": (
                "MCU family STM32F4; sensors I2C MPU6050@0x68, BMP280@0x76, "
                "LIS3MDL, UART GPS"
            ),
            "basis": "B1 flight-computer benchmark BOM",
            "needs_confirm": False,
        },
        {
            "text": "openEMS/Renode may be absent — degrade with SKIP, never fabricate results",
            "basis": "contracts honesty rule",
            "needs_confirm": False,
        },
    ],
}


def _patch_effort_maximum() -> None:
    """In-process: maximum effort + deep spawn rails without touching disk config."""
    from services.agent import config as cfg

    real_load = cfg.load_config

    def load_config_max():
        c = dict(real_load() or {})
        c["ai_effort"] = {
            "level": "maximum",
            "mode": "simple",
            "overrides": {
                "all": {
                    "deliberation": "full",
                    "ladder": "mandatory",
                    "council": "always",
                    "delegation_depth": 8,
                    "worker_fan_out": 4,
                    "context_pack_verbosity": "full",
                },
                "frontier": {
                    "deliberation": "full",
                    "delegation_depth": 8,
                    "worker_fan_out": 4,
                },
            },
        }
        policy = dict(c.get("ai_roles_policy") or {})
        policy["max_spawn_depth"] = 8
        c["ai_roles_policy"] = policy
        return c

    cfg.load_config = load_config_max  # type: ignore[assignment]


def _event_sink(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a", encoding="utf-8")

    def on_event(*args, **kwargs) -> None:
        try:
            if args and isinstance(args[0], dict) and not kwargs:
                rec = dict(args[0])
            elif kwargs and not args:
                rec = dict(kwargs)
            elif len(args) == 2 and callable(args[0]) is False:
                # dag executor style: emit(kind, payload) or emit(event, ...)
                rec = {"arg0": args[0], "arg1": args[1], **kwargs}
            else:
                rec = {"args": [a for a in args], **kwargs}
            rec["_wall"] = time.time()
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
        except Exception:  # noqa: BLE001
            pass

    on_event._fh = fh  # type: ignore[attr-defined]
    return on_event


def main() -> int:
    started = time.time()
    meta = {
        "run_id": RUN_ID,
        "tier": TIER,
        "effort": "maximum",
        "org_layer": True,
        "max_spawn_depth": 8,
        "baseline_commit": "d80b5b46",
        "cycle": "mega-4",
        "request_preview": REQUEST[:240],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "known_gaps": [
            "verify_phase_gate_tracks_dag_progress",
            "verify_delegate_spawn_counts_and_depth",
            "measure_build_artifact_depth",
        ],
    }
    (OUT / "mission_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    (OUT / "request.txt").write_text(REQUEST, encoding="utf-8")

    # Prefer mcp-venv (has langgraph). Skip gym re-exec which expects ``-m``.
    try:
        from tools.flywheel.bench.gym.runner import _dag_deps_importable

        if not _dag_deps_importable():
            print(
                "[fatal] langgraph not importable in this interpreter; "
                "re-run with ~/.local/share/AI-CAD/mcp-venv/bin/python",
                flush=True,
            )
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] dag deps probe: {exc}", flush=True)

    _patch_effort_maximum()

    events_path = OUT / "events.jsonl"
    if events_path.exists():
        events_path.unlink()
    on_event = _event_sink(events_path)

    try:
        from services.agent.ledger import run_ledger_path

        mp = run_ledger_path(RUN_ID)
        if mp.exists():
            mp.unlink()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] ledger cleanup: {exc}", flush=True)

    status = {
        "state": "starting",
        "run_id": RUN_ID,
        "tier": TIER,
        "pid": os.getpid(),
        "started_wall": started,
        "cycle": "mega-4",
    }
    (OUT / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

    result_payload: dict = {}
    try:
        from services.agent.tiers import provider_for_tier
        from services.agent import loop

        provider = provider_for_tier(TIER)
        print(
            f"[mega-4] provider={type(provider).__name__} "
            f"model={getattr(provider, 'model', '?')} run_id={RUN_ID}",
            flush=True,
        )

        def intake_fn(_req: str) -> dict:
            return json.loads(json.dumps(MANIFEST))

        mr = loop.run_mission(
            REQUEST,
            provider,
            name=RUN_ID,
            intake_fn=intake_fn,
            on_event=on_event,
            execute_dag=True,
            autonomy_default="auto",
        )
        result_payload = {
            "status": mr.status,
            "phase": mr.phase,
            "phases_completed": list(mr.phases_completed or []),
            "stop_reason": mr.stop_reason,
            "findings": list(mr.findings or []),
            "ledger_path": mr.ledger_path,
            "run_id": mr.run_id,
            "mode": "real",
            "wall_s": round(time.time() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001
        result_payload = {
            "status": "error",
            "phase": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "mode": "real",
            "wall_s": round(time.time() - started, 2),
        }
        print(result_payload["traceback"], flush=True)

    try:
        from services.agent.ledger import run_ledger_path
        import shutil

        mp = run_ledger_path(RUN_ID)
        if mp.exists():
            shutil.copy2(mp, OUT / "ledger.json")
            result_payload["mission_ledger_src"] = str(mp)
        else:
            result_payload["mission_ledger_src"] = None
            result_payload["mission_ledger_missing"] = True
    except Exception as exc:  # noqa: BLE001
        result_payload["ledger_copy_error"] = str(exc)

    (OUT / "mission.json").write_text(
        json.dumps(result_payload, indent=2, default=str), encoding="utf-8"
    )
    status.update(
        {
            "state": "finished",
            "finished_wall": time.time(),
            "wall_s": result_payload.get("wall_s"),
            "result": {
                k: result_payload.get(k)
                for k in ("status", "phase", "stop_reason", "error")
            },
        }
    )
    (OUT / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(result_payload, indent=2, default=str), flush=True)
    try:
        on_event._fh.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
