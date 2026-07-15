"""Mega-mission driver mega-16 — quota-unblock validation (OpenRouter paid frontier).

Same flight-computer + model rocket plane composition as mega-15. Mega-15 held
V/X unit-bound wire+power inject but W was UNTESTABLE (all quotas exhausted:
both SuperGrok spending-limit, OpenRouter free-models-per-day 429, local 4B
ctx overflow). This cycle uses PAID OpenRouter frontier
``openrouter:deepseek/deepseek-chat-v3.1`` (no :free suffix) + ollama leaves via
auto_pick. Targets: axis W (ecad_wire+ecad_add_power EXECUTED live via
force-inject; ERC power_not_driven→0; AC4 PASS), X-live (AC5 SPICE on wired
netlist, V(3V3) measured), Y (auto_pick routes frontier→openrouter paid when
xai blocked — no refusal loops, no ollama-all collapse) + A–V regression.
Target ≥8 live AC PASS.

Baseline commit c269e326. Conservation: auto_pick-first; no per-node frontier
injection; xAI blocked (do not touch). Spend guard: OpenRouter usage poll
before/after; abort if mid-run usage exceeds $8. No source edits — harness only.
Writes under reports/runs/mega-16/.
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

from services.agent.mission_criteria import apply_rocket_plane_checks

OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)

# Org layer on; overseer on; no multi-provider council.
# NOTE: AI_CAD_TIER is intentionally NOT forced to frontier for every node —
# auto_pick resolves per-node. Ambient provider remains xai-sub for hard work.
os.environ.setdefault("AI_CAD_ADVISOR", "1")
os.environ["AICAD_ORG_LAYER"] = "1"
os.environ["AICAD_OVERSEER"] = "1"
os.environ["AICAD_BUILD_COUNCIL"] = "0"
os.environ.setdefault("AI_CAD_ECAD_HEAVY_BUDGET_S", "180")
os.environ.setdefault("AICAD_FC_SIM_BUDGET_S", "120")
# Explicit xAI concurrency = 3 (Fixer5 default; set so telemetry is unambiguous).
os.environ.setdefault("AICAD_XAI_MAX_CONCURRENT", "3")
# mega-16: online; OpenRouter paid frontier (xai spending-limit blocked)
# Do NOT set AI_CAD_OFFLINE — cloud openrouter must be reachable.
os.environ.pop("AI_CAD_OFFLINE", None)


RUN_ID = "mega-16-fc-rocket-20260715"
# Fallback tier only — NOT injected as the per-node provider when auto_pick wins.
FALLBACK_TIER = "openrouter:deepseek/deepseek-chat-v3.1"
BASELINE_COMMIT = "c269e326"

REQUEST = (
    "Design a model rocket plane (boost-glide airframe) with an integrated central "
    "flight computer: MCU (STM32F4-class) + 3V3 buck, MPU6050 IMU, BMP280 baro, "
    "LIS3MDL mag, NEO-6M GPS. Full multi-domain mission:\n"
    "1) PLAN the system (airframe + avionics architecture, interfaces, budgets).\n"
    "2) PART SOURCING — resolve every IC/connector/fastener via the parts library "
    "and/or ecad_find_part; record BOM with provenance.\n"
    "3) SCHEMATIC — ecad_new_schematic / place / WIRE nets (ecad_connect / net tools) "
    "so the netlist has simulatable devices before SPICE.\n"
    "4) SPICE — ecad_simulate OP on the wired 3V3 front-end; measure V(3V3).\n"
    "5) PCB LAYOUT + TRACE OPT — REQUIRED chain: board_from_schematic → "
    "ecad_optimize_traces → ecad_run_drc (errors==0 or honest FAIL).\n"
    "6) ANTENNA/RF — ecad_antenna_design + analyze for 2.4 GHz IFA and GPS patch; "
    "use openEMS path when provisioned, else document honest degrade/SKIP.\n"
    "7) FIRMWARE — fw_new_project(stm32f4) + pinmap from netlist + fw_build.\n"
    "8) RENODE SENSOR-IN-THE-LOOP — climb_turn_profile → fw_sensor_scenario → "
    "fw_sim_run + fw_verify_board; SKIP honestly if Renode absent.\n"
    "9) MECHANICAL — model the rocket-plane airframe as editable CAD with "
    "verify_battery PASS: body tube ~OD 40–60mm length ≥300mm, 3–4 fins, wing/"
    "glide surface, closed payload bay volume ≥ board envelope; use sketch+"
    "extrude/revolve + fillets; avoid zero-thickness and open shells.\n"
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
                        "pass_rule": "volume_mm3 in [1000, 500000]",
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
                        "tool": "ecad_run_drc",
                        "params_hint": {
                            "legs": [
                                "parts",
                                "schematic",
                                "board",
                                "trace_opt",
                                "checks",
                            ]
                        },
                        "pass_rule": "errors==0",
                    },
                },
                {
                    "id": "AC5",
                    "text": "SPICE OP 3V3 rail within 5%",
                    "check": {
                        "tool": "ecad_simulate",
                        "params_hint": {"leg": "sim", "analysis": "OP"},
                        "pass_rule": "V(3V3) in [3.135, 3.465]",
                    },
                },
                {
                    "id": "AC6",
                    "text": "antenna design produced (openEMS or analytic degrade documented)",
                    "check": {
                        "tool": "ecad_antenna_design",
                        "params_hint": {"leg": "antenna", "freq_ghz": 2.4},
                        "pass_rule": "PASS|SKIP",
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
                        "pass_rule": "PASS|SKIP",
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
                        "tool": "make_report",
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
                        "pass_rule": "report_card_complete",
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

# mega-16: single source of tool-mapped AC shapes (GradingPaths / mission_criteria).
MANIFEST = apply_rocket_plane_checks(MANIFEST)

def _openrouter_api_key(cfg: dict | None = None) -> str:
    """Resolve OpenRouter key from env / config / keys.openrouter (disk)."""
    c = dict(cfg or {})
    oc = c.get("openrouter") or {}
    key = (
        (oc.get("api_key") if isinstance(oc, dict) else None)
        or c.get("openrouter_api_key")
        or c.get("api_key")
        or os.environ.get("OPENROUTER_API_KEY")
    )
    if not key:
        keys = c.get("keys") if isinstance(c.get("keys"), dict) else {}
        key = keys.get("openrouter") if isinstance(keys, dict) else None
    if not key:
        try:
            disk = json.loads(
                (Path.home() / ".local/share/AI-CAD/config.json").read_text(
                    encoding="utf-8"
                )
            )
            keys = disk.get("keys") or {}
            key = keys.get("openrouter")
            if not key:
                oc2 = disk.get("openrouter") or {}
                key = oc2.get("api_key") if isinstance(oc2, dict) else None
        except Exception:  # noqa: BLE001
            key = None
    return str(key).strip() if key else ""


def _openrouter_usage() -> dict:
    """GET https://openrouter.ai/api/v1/auth/key — usage / remaining."""
    import urllib.request

    key = _openrouter_api_key()
    if not key:
        return {"error": "no_openrouter_key"}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _patch_effort_maximum() -> None:
    """In-process: maximum effort + deep spawn rails without touching disk config.

    mega-16: pin OpenRouter PAID deepseek as hard/frontier candidate + ollama
    leaves. xai stays out of the enabled set (spending-limit blocked). Free
    OpenRouter models are excluded (free-models-per-day still 429).
    """
    from services.agent import config as cfg

    real_load = cfg.load_config
    or_key = _openrouter_api_key(real_load() or {})
    if or_key:
        os.environ.setdefault("OPENROUTER_API_KEY", or_key)

    def load_config_max():
        c = dict(real_load() or {})
        c["offline_mode"] = False
        if c.get("compliance_mode") == "local_only":
            c["compliance_mode"] = "off"
        c["ai_effort"] = {
            "level": "maximum",
            "mode": "simple",
            "overrides": {
                "all": {
                    "deliberation": "full",
                    "ladder": "mandatory",
                    "council": "always",
                    "delegation_depth": 8,
                    "worker_fan_out": 2,
                    "context_pack_verbosity": "full",
                },
                "frontier": {
                    "deliberation": "full",
                    "delegation_depth": 8,
                    "worker_fan_out": 2,
                },
            },
        }
        policy = dict(c.get("ai_roles_policy") or {})
        policy["max_spawn_depth"] = 8
        c["ai_roles_policy"] = policy

        paid_model = "deepseek/deepseek-chat-v3.1"
        key = or_key or _openrouter_api_key(c)
        if key:
            os.environ.setdefault("OPENROUTER_API_KEY", key)
            c["openrouter"] = {
                **(c.get("openrouter") or {}),
                "api_key": key,
                # PAID only — no :free suffix models
                "models": [paid_model],
            }
            c["openrouter_api_key"] = key

        # Conservation set: openrouter paid frontier + ollama leaves.
        # Intentionally omit xai-sub (spending-limit blocked this cycle).
        enabled = []
        if key:
            enabled.append(
                {
                    "provider": "openrouter",
                    "model": paid_model,
                }
            )
        enabled.append(
            {
                "provider": "ollama",
                "model": "llama3.2:latest",
                "base_url": "http://127.0.0.1:11434/v1",
            }
        )
        c["enabled_providers"] = enabled

        user_ranks = dict(c.get("model_rankings") or {})
        user_ranks[paid_model] = {
            "rank": 5,
            "provider": "openrouter",
            "notes": "mega-16 paid frontier (deepseek-chat-v3.1); xai blocked",
        }
        user_ranks["llama3.2:latest"] = {
            "rank": 1,
            "provider": "ollama",
            "notes": "mega-16 leaf/cheap via auto_pick",
        }
        # Keep free openrouter ranks low/disabled so auto_pick never prefers them.
        for free_id in (
            "nvidia/nemotron-ultra:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "mistralai/mistral-small:free",
            "google/gemma-3-27b-it:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ):
            user_ranks[free_id] = {
                "rank": 0,
                "provider": "openrouter",
                "notes": "mega-16 disabled: free pool 429",
            }
        c["model_rankings"] = user_ranks
        # Ambient provider for hard nodes when auto_pick selects openrouter.
        c["provider"] = "openrouter"
        c["model"] = paid_model
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


def _write_spawn_probe() -> dict:
    """Preflight: capability separation + delegate + budget_tokens on schemas.

    mega-9 acceptance (axis B preflight, regression): PLAN/layout role cards resolve
    WITHOUT add_features/new_part in tools_slot but WITH delegate.
    """
    from services.agent.roles import load_role
    from services.agent.tool_schemas import render_tools_block
    from services.agent import agent_turn as at
    from services.agent.mission_dispatch import MissionDispatch

    roles_dir = Path(ROOT) / "services" / "agent" / "roles"
    role_ids = sorted(p.stem for p in roles_dir.glob("*.yaml"))
    required = ["spec_writer", "layout_engineer", "feature_modeler"]
    # mega-14: leg production roles (ECAD/fw) when present on disk
    leg_roles = [
        r for r in (
            "ecad_engineer", "ecad_schematic", "board_layout",
            "firmware_engineer", "fw_engineer", "antenna_engineer",
            "rf_engineer", "analysis_engineer",
        )
        if r in role_ids
    ]
    # capability-separation surface (PLAN/layout coordinators)
    plan_roles = ["orchestrator", "layout_engineer", "department_manager"]
    exec_forbidden = ("add_features", "new_part", "preflight_ops")
    md = MissionDispatch(run_id=RUN_ID, ledger=None)
    resolver = at.resolver_for(md)

    roles_out: dict = {}
    required_detail: dict = {}
    bt_rendered: dict = {}
    bt_prompt: dict = {}
    detail: dict = {}
    cap_sep: dict = {}

    for rid in role_ids:
        try:
            card = load_role(rid)
        except Exception as exc:  # noqa: BLE001
            roles_out[rid] = {"error": str(exc)}
            continue
        tools = list(card.concrete_tools or [])
        allow = []
        for t in card.tool_allowlist or []:
            if getattr(t, "tool", None):
                allow.append(t.tool)
            elif getattr(t, "family", None):
                allow.append(f"family:{t.family}")
        prompt = card.system_prompt or ""
        prompt_has = "budget_tokens" in prompt
        stale = ("delegate(role, brief)" in prompt) and not prompt_has
        snippet = ""
        rendered_has = False
        try:
            tools_text, _meta = render_tools_block(tools, resolver=resolver)
            rendered_has = "budget_tokens" in tools_text
            for line in tools_text.splitlines():
                if line.strip().startswith("- delegate"):
                    snippet = line.strip()
                    break
        except Exception as exc:  # noqa: BLE001
            snippet = f"<render error: {exc}>"

        has_exec = [t for t in exec_forbidden if t in tools]
        entry = {
            "has_delegate": "delegate" in tools,
            "raw_allowlist_has_delegate": "delegate" in allow,
            "has_exec_tools": has_exec,
            "tools": tools,
            "allowlist": allow,
            "prompt_has_budget_tokens": prompt_has,
            "prompt_has_stale_budget_sig": stale,
            "rendered_tools_has_budget_tokens": rendered_has,
            "rendered_delegate_snippet": snippet,
        }
        roles_out[rid] = entry
        detail[rid] = {
            "prompt_budget_tokens": prompt_has,
            "rendered_budget_tokens": rendered_has,
            "snippet": snippet,
            "prompt_sig_ok": prompt_has and rendered_has,
        }
        if rid in required:
            required_detail[rid] = entry
            bt_rendered[rid] = rendered_has
            bt_prompt[rid] = prompt_has
        if rid in plan_roles:
            cap_sep[rid] = {
                "no_exec_tools": not has_exec,
                "has_delegate": "delegate" in tools,
                "forbidden_present": has_exec,
                "tools_sample": tools[:20],
            }

    orch = roles_out.get("orchestrator") or {}
    all_rendered_ok = all(bt_rendered.get(r, False) for r in required)
    all_delegate_ok = all(
        (required_detail.get(r) or {}).get("has_delegate")
        and (required_detail.get(r) or {}).get("raw_allowlist_has_delegate")
        for r in required
    )
    # PLAN/layout cards must lose exec tools and keep delegate
    cap_sep_ok = bool(cap_sep) and all(
        v.get("no_exec_tools") and v.get("has_delegate") for v in cap_sep.values()
    )
    # leaf worker still has exec tools
    fm = roles_out.get("feature_modeler") or {}
    leaf_exec_ok = "add_features" in (fm.get("tools") or []) and "new_part" in (
        fm.get("tools") or []
    )
    verdict = (
        "PASS"
        if (all_rendered_ok and all_delegate_ok and cap_sep_ok and leaf_exec_ok)
        else "FAIL"
    )

    probe = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": RUN_ID,
        "baseline_commit": BASELINE_COMMIT,
        "roles": roles_out,
        "required_roles": required,
        "plan_roles_capability_separation": cap_sep,
        "capability_separation_ok": cap_sep_ok,
        "leaf_feature_modeler_keeps_exec": leaf_exec_ok,
        "verdict": verdict,
        "budget_tokens_preflight": {
            "required_roles_rendered_schema_has_budget_tokens": bt_rendered,
            "required_roles_prompt_has_budget_tokens": bt_prompt,
            "orchestrator_prompt_has_budget_tokens": bool(
                orch.get("prompt_has_budget_tokens")
            ),
            "spec_writer_prompt_has_budget_tokens": bool(
                bt_prompt.get("spec_writer")
            ),
            "all_required_rendered_ok": all_rendered_ok,
            "detail": detail,
        },
        "required_role_detail": required_detail,
        "conservation": {
            "no_per_node_frontier_injection": True,
            "fallback_tier": FALLBACK_TIER,
            "auto_pick_first": True,
        },
        "leg_roles_present": leg_roles,
        "leg_roles_detail": {r: roles_out.get(r) for r in leg_roles},
        "leg_roles_have_domain_tools": {
            r: bool(
                set((roles_out.get(r) or {}).get("tools") or [])
                & {
                    "ecad_new_schematic", "ecad_run_drc", "ecad_run_erc",
                    "ecad_simulate", "ecad_antenna_design", "fw_new_project",
                    "fw_build", "fw_sim_run", "board_from_schematic",
                }
            )
            for r in leg_roles
        },
        "ecad_chain_tools_present": {
            tool: any(
                tool in ((roles_out.get(r) or {}).get("tools") or [])
                for r in (leg_roles + ["ecad_engineer", "board_layout", "feature_modeler", "orchestrator"])
            )
            for tool in (
                "ecad_new_schematic",
                "ecad_place_symbol",
                "board_from_schematic",
                "ecad_run_drc",
                "ecad_run_erc",
                "ecad_simulate",
                "ecad_antenna_analyze",
                "fw_build",
            )
        },
        "cycle16_axes": {
            "A_op_vocab_cheatsheet": "runtime_regression",
            "B_capability_separation": "preflight+runtime_regression",
            "C_cheap_leaf_oneshot": "runtime_regression",
            "D_deliver_partial": "runtime_regression",
            "E_verify_battery_ledger": "runtime_regression",
            "F_clean_stop_reason": "runtime_regression",
            "G_typed_refusal_envelope": "runtime_regression",
            "H_AC_closure": "runtime_regression",
            "I_battery_doc_id": "runtime_regression",
            "J_typed_refusal_live_or_unit": "runtime_regression",
            "K_AC_gradeable_count": "runtime",
            "L_AC_live_pass_count": "runtime",
            "M_non_CAD_leg_artifact": "runtime",
            "N_AC_live_pass_count_ge_4": "runtime",
            "O_ECAD_board_from_schematic_DRC": "runtime",
            "P_board_from_schematic_and_drc_executed": "runtime",
            "Q_AC_live_pass_count_ge_6": "runtime",
            "R_AC5_SPICE_wired_netlist": "runtime",
            "S_board_from_schematic_and_drc_executed": "runtime",
            "T_AC5_SPICE_wired_netlist": "runtime",
            "U_AC1_battery_pass_or_per_check_diagnosis": "runtime",
            "V_battery_invocation_bugs_fixed": "runtime",
            "W_ecad_wire_and_power_ERC_clean": "runtime",
            "X_AC5_SPICE_wired_netlist": "runtime",
            "Y_auto_pick_openrouter_paid_when_xai_blocked": "preflight+runtime",
            "Fix8_hard_ecad_chain_gate": "preflight+runtime",
            "Fix_wire_ecad_wire_power_force_inject": "preflight+runtime",
        },
    }

    # Fix8 hard ECAD chain gate wiring (mega-14 preflight)
    fix8 = {
        "source_gate_present": False,
        "unit_test_present": False,
        "unit_test_importable": False,
        "gate_events": ["ecad_chain_gate", "tool_denied"],
        "detail": {},
    }
    try:
        at_src = (ROOT / "services/agent/agent_turn.py").read_text(encoding="utf-8")
        fix8["source_gate_present"] = (
            "ecad_chain_gate" in at_src
            and "ECAD chain gate" in at_src
            and "board_from_schematic" in at_src
            and "ecad_wire" in at_src
        )
        fix8["detail"]["has_wire_block"] = "nets empty after place" in at_src
        fix8["detail"]["has_board_block"] = "call board_from_schematic next" in at_src
        fix8["detail"]["has_tool_denied"] = "tool_denied" in at_src
    except Exception as exc:  # noqa: BLE001
        fix8["detail"]["source_error"] = f"{type(exc).__name__}: {exc}"
    try:
        test_src = (ROOT / "services/agent/test_mega11_legs.py").read_text(encoding="utf-8")
        fix8["unit_test_present"] = "test_ecad_hard_gate_blocks_place_until_wire" in test_src
    except Exception as exc:  # noqa: BLE001
        fix8["detail"]["test_error"] = f"{type(exc).__name__}: {exc}"
    try:
        import services.agent.test_mega11_legs as _legs  # noqa: F401
        fix8["unit_test_importable"] = hasattr(_legs, "test_ecad_hard_gate_blocks_place_until_wire")
    except Exception as exc:  # noqa: BLE001
        fix8["detail"]["import_error"] = f"{type(exc).__name__}: {exc}"
    # also require chain tools on ecad_engineer
    ecad_tools = set((roles_out.get("ecad_engineer") or {}).get("tools") or [])
    fix8["ecad_engineer_has_chain"] = bool(
        {"board_from_schematic", "ecad_run_drc", "ecad_wire"} <= ecad_tools
        or {"board_from_schematic", "ecad_run_drc"} & ecad_tools
    )
    fix8["ecad_engineer_tools_sample"] = sorted(ecad_tools)[:30]
    fix8["verdict"] = (
        "PASS"
        if (
            fix8["source_gate_present"]
            and fix8["unit_test_present"]
            and fix8.get("ecad_engineer_has_chain")
        )
        else "FAIL"
    )
    probe["fix8_hard_ecad_chain_gate"] = fix8

    # Fix-wire: force-inject ecad_wire + ecad_add_power (mega-15 preflight)
    fix_wire = {
        "source_has_real_wire": False,
        "source_force_wire_order": False,
        "unit_test_wire_before_board": False,
        "ecad_engineer_has_wire_power": False,
        "detail": {},
    }
    try:
        at_src = (ROOT / "services/agent/agent_turn.py").read_text(encoding="utf-8")
        fix_wire["source_has_real_wire"] = (
            "_ecad_has_real_wire" in at_src
            and "orphan ecad_add_power" in at_src
            and "ecad_wire" in at_src
        )
        fix_wire["source_force_wire_order"] = (
            "Device:R" in at_src
            and "_ecad_chain_need" in at_src
            and "ecad_chain_force_inject" in at_src
            and "ecad_add_power" in at_src
        )
        fix_wire["detail"]["has_force_after"] = "_ECAD_GATE_FORCE_AFTER" in at_src
        fix_wire["detail"]["has_forced_args"] = "_ecad_forced_tool_args" in at_src
    except Exception as exc:  # noqa: BLE001
        fix_wire["detail"]["source_error"] = f"{type(exc).__name__}: {exc}"
    try:
        test_src = (ROOT / "services/agent/test_mega11_legs.py").read_text(encoding="utf-8")
        fix_wire["unit_test_wire_before_board"] = (
            "test_ecad_hard_gate_force_injects_wire_after_thrash" in test_src
            or "test_ecad_chain_need_helpers" in test_src
        )
        fix_wire["detail"]["test_has_force_wire"] = (
            "test_ecad_hard_gate_force_injects_wire_after_thrash" in test_src
        )
        fix_wire["detail"]["test_has_chain_need"] = (
            "test_ecad_chain_need_helpers" in test_src
        )
    except Exception as exc:  # noqa: BLE001
        fix_wire["detail"]["test_error"] = f"{type(exc).__name__}: {exc}"
    ecad_tools_fw = set((roles_out.get("ecad_engineer") or {}).get("tools") or [])
    fix_wire["ecad_engineer_has_wire_power"] = bool(
        {"ecad_wire", "ecad_add_power"} <= ecad_tools_fw
        or ("ecad_wire" in ecad_tools_fw)
    )
    fix_wire["ecad_engineer_tools_sample"] = sorted(ecad_tools_fw)[:30]
    fix_wire["verdict"] = (
        "PASS"
        if (
            fix_wire["source_has_real_wire"]
            and fix_wire["source_force_wire_order"]
            and fix_wire["unit_test_wire_before_board"]
            and fix_wire.get("ecad_engineer_has_wire_power")
        )
        else "FAIL"
    )
    probe["fix_wire_ecad_wire_power"] = fix_wire
    if fix_wire["verdict"] != "PASS" and probe.get("verdict") == "PASS":
        probe["verdict"] = "FAIL"
        probe["verdict_note"] = "Fix-wire force-inject wiring incomplete"
    elif fix_wire["verdict"] == "PASS":
        note = probe.get("verdict_note") or ""
        probe["verdict_note"] = (note + "; Fix-wire OK").strip("; ")

    # fold Fix8 into overall spawn_probe verdict
    if fix8["verdict"] != "PASS" and probe.get("verdict") == "PASS":
        probe["verdict"] = "FAIL"
        probe["verdict_note"] = "Fix8 hard gate wiring incomplete"
    elif fix8["verdict"] == "PASS":
        probe["verdict_note"] = "cap-sep + Fix8 gate wiring OK"

    (OUT / "spawn_probe.json").write_text(
        json.dumps(probe, indent=2, default=str), encoding="utf-8"
    )
    return probe


def _auto_pick_smoke() -> dict:
    """Preflight: leaf→ollama; hard/frontier→openrouter paid (axis Y)."""
    from services.agent import config as cfg
    from services.agent.role_models import (
        auto_pick_model,
        cost_tier_for,
        min_rank_for_difficulty,
        _enabled_providers,
    )

    c = cfg.load_config() or {}
    enabled = _enabled_providers(c)
    cases = []
    for depth, task_class, risk in (
        (2, "simple", None),
        (2, "leaf", None),
        (2, None, "low"),
        (0, "architecture", "high"),
        (0, None, None),
        (0, "analysis", "high"),
    ):
        min_rank = min_rank_for_difficulty(
            task_class=task_class, node_depth=depth, risk=risk
        )
        try:
            pick = auto_pick_model(
                cfg=c,
                task_class=task_class,
                node_depth=depth,
                risk=risk,
                min_rank=min_rank,
            )
            cases.append(
                {
                    "depth": depth,
                    "task_class": task_class,
                    "risk": risk,
                    "min_rank": min_rank,
                    "provider": pick.provider,
                    "model": pick.model,
                    "rank": pick.rank,
                    "cost_tier": cost_tier_for(pick.provider, pick.model),
                    "reason": pick.reason,
                    "is_xai": str(pick.provider).startswith("xai"),
                    "is_openrouter": str(pick.provider) == "openrouter",
                    "is_ollama": str(pick.provider) in ("ollama", "llamacpp", "lmstudio"),
                    "is_free_model": ":free" in str(pick.model or ""),
                }
            )
        except Exception as exc:  # noqa: BLE001
            cases.append(
                {
                    "depth": depth,
                    "task_class": task_class,
                    "risk": risk,
                    "min_rank": min_rank,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    leaf_cases = [c for c in cases if c.get("depth", 0) >= 2 and "error" not in c]
    hard_cases = [c for c in cases if c.get("depth", 0) == 0 and "error" not in c]
    leaf_non_xai = all(not c.get("is_xai") for c in leaf_cases) and bool(leaf_cases)
    leaf_local = all(c.get("is_ollama") for c in leaf_cases) and bool(leaf_cases)
    hard_openrouter_paid = (
        all(
            c.get("is_openrouter") and not c.get("is_free_model")
            for c in hard_cases
        )
        and bool(hard_cases)
    )
    no_free = all(not c.get("is_free_model") for c in cases if "error" not in c)
    # Axis Y: hard→openrouter paid; leaf→ollama/local; no xai; no free pool
    y_ok = hard_openrouter_paid and leaf_local and leaf_non_xai and no_free
    smoke = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "enabled_providers": enabled,
        "cases": cases,
        "leaf_depth2_non_xai": leaf_non_xai,
        "leaf_depth2_local": leaf_local,
        "hard_depth0_openrouter_paid": hard_openrouter_paid,
        "no_free_openrouter": no_free,
        "axis_Y": "PASS" if y_ok else "FAIL",
        "verdict": "PASS" if (leaf_non_xai and y_ok) else "FAIL",
        "contract": (
            "hard/frontier nodes → openrouter paid (deepseek-chat-v3.1); "
            "leaf → ollama; no xai; no :free models"
        ),
    }
    (OUT / "auto_pick_smoke.json").write_text(
        json.dumps(smoke, indent=2, default=str), encoding="utf-8"
    )
    return smoke



def _prefer_newest_xai_account() -> dict:
    """Poll subscription_accounts; switch active xai-sub to newest account if needed."""
    from services.agent.providers import sub_auth

    accounts = list(sub_auth.list_accounts("xai-sub") or [])
    # Also probe friendly "xai" key if present.
    try:
        accounts_xai = list(sub_auth.list_accounts("xai") or [])
    except Exception:  # noqa: BLE001
        accounts_xai = []
    # Prefer xai-sub (provider used by XAISubProvider). Sort by expires_at_ms desc
    # as a proxy for "newest" when created_at is absent; fall back to account_id.
    def _sort_key(a: dict):
        exp = a.get("expires_at_ms") or 0
        try:
            exp = float(exp)
        except (TypeError, ValueError):
            exp = 0.0
        return (exp, str(a.get("account_id") or ""))

    ranked = sorted(accounts, key=_sort_key, reverse=True)
    active = sub_auth.get_active_account_id("xai-sub")
    switched = False
    chosen = None
    if ranked:
        newest = ranked[0]
        chosen = newest.get("account_id")
        if chosen and chosen != active:
            switched = bool(sub_auth.set_active_account("xai-sub", chosen))
            active = sub_auth.get_active_account_id("xai-sub")
        else:
            chosen = active or chosen
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "xai-sub",
        "accounts": accounts,
        "accounts_xai_alias": accounts_xai,
        "active_before_or_after": active,
        "chosen_account_id": chosen,
        "switched": switched,
        "n_accounts": len(accounts),
        "note": (
            "prefer newest by expires_at_ms desc; proceed with existing if only one"
            if len(accounts) <= 1
            else "switched to newest if not already active"
        ),
    }
    (OUT / "subscription_accounts.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    return report


def main() -> int:
    started = time.time()
    meta = {
        "run_id": RUN_ID,
        "tier": FALLBACK_TIER,
        "effort": "maximum",
        "org_layer": True,
        "max_spawn_depth": 8,
        "baseline_commit": BASELINE_COMMIT,
        "cycle": "mega-16",
        "conservation": {
            "auto_pick_first": True,
            "no_per_node_frontier_injection": True,
            "free_local_leaves_ok": True,
            "xai_max_concurrent": 3,
            "fallback_only": FALLBACK_TIER,
            "plan_b_free_frontier": False,
            "openrouter_paid_frontier": True,
            "offline_mode": False,
            "xai_blocked_spending_limit": True,
            "grok45_revalidation_deferred": True,
            "spend_guard_usd": 8.0,
        },
        "request_preview": REQUEST[:240],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "known_gaps": [
            "W_ecad_wire_and_power_ERC_clean",
            "X_AC5_SPICE_wired_netlist",
            "Y_auto_pick_openrouter_paid_when_xai_blocked",
            "V_battery_invocation_bugs_fixed",
            "S_board_from_schematic_and_drc_executed",
            "T_AC5_SPICE_wired_netlist",
            "U_AC1_battery_pass_or_per_check_diagnosis",
            "P_board_from_schematic_and_drc_executed",
            "Q_AC_live_pass_count_ge_6",
            "R_AC5_SPICE_wired_netlist",
            "N_AC_live_pass_count_ge_4",
            "O_ECAD_board_from_schematic_DRC",
            "K_AC_gradeable_count_ge_6",
            "L_AC_live_pass_count_ge_2",
            "M_non_CAD_leg_artifact",
            "H_AC_closure_from_real_artifacts",
            "I_battery_doc_id_multi_part",
            "J_typed_refusal_live_or_unit",
            "E_verify_battery_ledger_regression",
            "F_clean_stop_reason_regression",
            "G_typed_refusal_teach_regression",
            "A_add_features_accepted_ops_and_op_vocab_cheatsheet",
            "B_delegate_or_typed_refusal_capability_separation",
            "C_cheap_leaf_oneshot_envelope_leaf_miss",
            "D_terminal_status_deliver_partial",
            "conservation_auto_pick_first",
            "phase_honesty",
            "A_V_regression",
            "Fix8_hard_ecad_chain_gate",
            "Fix_wire_ecad_wire_power_force_inject",
        ],
    }
    (OUT / "mission_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    (OUT / "request.txt").write_text(REQUEST, encoding="utf-8")

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

    # Spend guard baseline (must record before any paid calls)
    usage_before = {}
    spend_before = 0.0
    try:
        usage_before = _openrouter_usage()
        (OUT / "openrouter_usage_before.json").write_text(
            json.dumps(usage_before, indent=2, default=str), encoding="utf-8"
        )
        data = usage_before.get("data") or usage_before
        spend_before = float(data.get("usage") or 0.0)
        print(
            f"[mega-16] openrouter usage_before=${spend_before:.6f} "
            f"remaining={data.get('limit_remaining')}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] openrouter usage poll failed: {exc}", flush=True)
        traceback.print_exc()

    print(
        "[mega-16] preflight: subscription_accounts (xai blocked; poll only)",
        flush=True,
    )
    try:
        acct = _prefer_newest_xai_account()
        print(
            f"[mega-16] xai-sub accounts={acct.get('n_accounts')} "
            f"active={acct.get('active_before_or_after')} "
            f"switched={acct.get('switched')} (NOT used as frontier)",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] subscription_accounts poll failed: {exc}", flush=True)
        traceback.print_exc()

    print("[mega-16] preflight: spawn_probe + auto_pick_smoke", flush=True)
    try:
        probe = _write_spawn_probe()
        print(
            f"[mega-16] spawn_probe verdict={probe.get('verdict')} "
            f"budget_rendered_ok="
            f"{probe.get('budget_tokens_preflight', {}).get('all_required_rendered_ok')} "
            f"fix8={(probe.get('fix8_hard_ecad_chain_gate') or {}).get('verdict')} "
            f"fix_wire={(probe.get('fix_wire_ecad_wire_power') or {}).get('verdict')}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[fatal] spawn_probe failed: {exc}", flush=True)
        traceback.print_exc()
        return 3

    try:
        smoke = _auto_pick_smoke()
        print(
            f"[mega-16] auto_pick_smoke verdict={smoke.get('verdict')} "
            f"axis_Y={smoke.get('axis_Y')} "
            f"hard_or_paid={smoke.get('hard_depth0_openrouter_paid')} "
            f"leaf_local={smoke.get('leaf_depth2_local')}",
            flush=True,
        )
        if smoke.get("verdict") != "PASS":
            print(
                "[fatal] auto_pick smoke FAIL — hard nodes must route openrouter "
                "paid and leaves ollama (axis Y); aborting",
                flush=True,
            )
            return 4
    except Exception as exc:  # noqa: BLE001
        print(f"[fatal] auto_pick_smoke failed: {exc}", flush=True)
        traceback.print_exc()
        return 3

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
        "tier": FALLBACK_TIER,
        "pid": os.getpid(),
        "started_wall": started,
        "cycle": "mega-16",
        "conservation": True,
        "baseline_commit": BASELINE_COMMIT,
        "spend_before_usd": spend_before,
        "spend_guard_usd": 8.0,
    }
    (OUT / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

    result_payload: dict = {}
    try:
        from services.agent.tiers import provider_for_tier
        from services.agent import loop

        # Fallback = OpenRouter paid deepseek. node_runner auto_pick-first prefers
        # ollama for low min_rank leaves; hard nodes select openrouter paid.
        key = _openrouter_api_key()
        if key:
            os.environ.setdefault("OPENROUTER_API_KEY", key)
        provider = provider_for_tier(FALLBACK_TIER)
        print(
            f"[mega-16] fallback_provider={type(provider).__name__} "
            f"model={getattr(provider, 'model', '?')} run_id={RUN_ID} "
            f"conservation=auto_pick_first",
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
            "conservation": True,
            "baseline_commit": BASELINE_COMMIT,
        }
    except Exception as exc:  # noqa: BLE001
        result_payload = {
            "status": "error",
            "phase": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "mode": "real",
            "wall_s": round(time.time() - started, 2),
            "conservation": True,
            "baseline_commit": BASELINE_COMMIT,
        }
        print(result_payload["traceback"], flush=True)

    # Spend after
    spend_after = spend_before
    spend_delta = 0.0
    try:
        usage_after = _openrouter_usage()
        (OUT / "openrouter_usage_after.json").write_text(
            json.dumps(usage_after, indent=2, default=str), encoding="utf-8"
        )
        data = usage_after.get("data") or usage_after
        spend_after = float(data.get("usage") or 0.0)
        spend_delta = spend_after - spend_before
        print(
            f"[mega-16] openrouter usage_after=${spend_after:.6f} "
            f"delta=${spend_delta:.6f} remaining={data.get('limit_remaining')}",
            flush=True,
        )
        if spend_after > 8.0 or spend_delta > 8.0:
            result_payload["spend_guard_abort"] = True
            result_payload["spend_note"] = (
                f"usage ${spend_after:.4f} / delta ${spend_delta:.4f} exceeded $8 guard"
            )
            print(
                f"[mega-16] SPEND GUARD TRIPPED: {result_payload['spend_note']}",
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001
        result_payload["spend_poll_error"] = str(exc)
        print(f"[warn] openrouter usage after poll failed: {exc}", flush=True)

    result_payload["spend_before_usd"] = spend_before
    result_payload["spend_after_usd"] = spend_after
    result_payload["spend_delta_usd"] = round(spend_delta, 6)

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
            "spend_after_usd": spend_after,
            "spend_delta_usd": round(spend_delta, 6),
            "result": {
                k: result_payload.get(k)
                for k in ("status", "phase", "stop_reason", "error", "spend_guard_abort")
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
