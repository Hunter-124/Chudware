#!/usr/bin/env python3
"""Mega-mission driver mega-8 — cycle-8 wave validation (op-vocab + capability sep).

Flight computer + model rocket plane multi-domain exercise of the infinitely-
branching subagent mode (org layer ON, spawn-depth policy active, maximum effort).

Baseline commit 7ef7b577 — first process where the cycle-8 wave binds:
  1) op-vocabulary registry + rich unknown-op errors + once-per-mission cheatsheet
  2) capability separation (PLAN/layout have NO exec tools — must delegate or typed-refuse)
  3) cheap-leaf one-shot envelope + leaf_miss telemetry
  4) deliver_partial terminal status

Conservation config (owner priority): do NOT force every node onto SuperGrok.
``run_mission`` still receives an xAI provider as *fallback* only; node_runner
``_resolve_provider`` priority is auto_pick → injected fallback → tier → ambient.
Leaf / low min_rank work must land on ollama or openrouter free.

Auth: SuperGrok live for hard top-level (prefer newest xai-sub account 17d17fb1).
GPU free for local leaf models. No source edits — harness consumer only.
Writes under reports/runs/mega-8/.
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

RUN_ID = "mega-8-fc-rocket-20260714"
# Fallback tier only — NOT injected as the per-node provider when auto_pick wins.
FALLBACK_TIER = "xai:grok-4-5"
BASELINE_COMMIT = "7ef7b577"

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
    """In-process: maximum effort + deep spawn rails without touching disk config.

    Also pin ``enabled_providers`` to ollama + xai-sub so auto_pick cannot select
    keyless openrouter free models (attempt-1 died 19/19 on HTTP 401 provider_auth).
    When OPENROUTER_API_KEY / config openrouter.api_key is present, free models are
    re-enabled so leaf routing can prefer them.
    """
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
        # Conservation candidate set: always include local ollama + SuperGrok.
        # OpenRouter free only when a key is actually present (attempt-1 wipe).
        enabled = [
            {
                "provider": "ollama",
                "model": "llama3.2:latest",
                "base_url": "http://127.0.0.1:11434/v1",
            },
            {"provider": "xai-sub", "model": "grok-4.5"},
        ]
        or_key = ""
        or_cfg = c.get("openrouter") or {}
        if isinstance(or_cfg, dict):
            or_key = str(or_cfg.get("api_key") or "")
        or_key = or_key or os.environ.get("OPENROUTER_API_KEY", "")
        if or_key:
            for mid in (
                "nvidia/nemotron-ultra:free",
                "mistralai/mistral-small:free",
                "google/gemma-3-27b-it:free",
            ):
                enabled.insert(
                    1, {"provider": "openrouter", "model": mid}
                )
        c["enabled_providers"] = enabled
        # Do NOT pin ai_roles to xai — leave auto so auto_pick can route leaves.
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

    mega-8 acceptance (axis B preflight): PLAN/layout role cards resolve
    WITHOUT add_features/new_part in tools_slot but WITH delegate.
    """
    from services.agent.roles import load_role
    from services.agent.tool_schemas import render_tools_block
    from services.agent import agent_turn as at
    from services.agent.mission_dispatch import MissionDispatch

    roles_dir = Path(ROOT) / "services" / "agent" / "roles"
    role_ids = sorted(p.stem for p in roles_dir.glob("*.yaml"))
    required = ["spec_writer", "layout_engineer", "feature_modeler"]
    # mega-8 capability-separation surface (PLAN/layout coordinators)
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
    # mega-8: PLAN/layout cards must lose exec tools and keep delegate
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
        "cycle8_axes": {
            "A_op_vocab_cheatsheet": "runtime",
            "B_capability_separation": "preflight+runtime",
            "C_cheap_leaf_oneshot": "runtime",
            "D_deliver_partial": "runtime",
        },
    }
    (OUT / "spawn_probe.json").write_text(
        json.dumps(probe, indent=2, default=str), encoding="utf-8"
    )
    return probe


def _auto_pick_smoke() -> dict:
    """Preflight: depth-2 low-difficulty must resolve to non-xai."""
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
    leaf_non_xai = all(not c.get("is_xai") for c in leaf_cases) and bool(leaf_cases)
    smoke = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "enabled_providers": enabled,
        "cases": cases,
        "leaf_depth2_non_xai": leaf_non_xai,
        "verdict": "PASS" if leaf_non_xai else "FAIL",
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
        "cycle": "mega-8",
        "conservation": {
            "auto_pick_first": True,
            "no_per_node_frontier_injection": True,
            "free_local_leaves_ok": True,
            "xai_max_concurrent": 3,
            "fallback_only": FALLBACK_TIER,
        },
        "request_preview": REQUEST[:240],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "known_gaps": [
            "A_add_features_accepted_ops_and_op_vocab_cheatsheet",
            "B_delegate_or_typed_refusal_capability_separation",
            "C_cheap_leaf_oneshot_envelope_leaf_miss",
            "D_terminal_status_deliver_partial",
            "conservation_auto_pick_first",
            "phase_honesty",
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

    print("[mega-8] preflight: subscription_accounts (prefer newest xai)", flush=True)
    try:
        acct = _prefer_newest_xai_account()
        print(
            f"[mega-8] xai-sub accounts={acct.get('n_accounts')} "
            f"active={acct.get('active_before_or_after')} "
            f"switched={acct.get('switched')}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] subscription_accounts poll failed: {exc}", flush=True)
        traceback.print_exc()

    print("[mega-8] preflight: spawn_probe + auto_pick_smoke", flush=True)
    try:
        probe = _write_spawn_probe()
        print(
            f"[mega-8] spawn_probe verdict={probe.get('verdict')} "
            f"budget_rendered_ok="
            f"{probe.get('budget_tokens_preflight', {}).get('all_required_rendered_ok')}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[fatal] spawn_probe failed: {exc}", flush=True)
        traceback.print_exc()
        return 3

    try:
        smoke = _auto_pick_smoke()
        print(
            f"[mega-8] auto_pick_smoke verdict={smoke.get('verdict')} "
            f"leaf_depth2_non_xai={smoke.get('leaf_depth2_non_xai')}",
            flush=True,
        )
        if smoke.get("verdict") != "PASS":
            print(
                "[fatal] auto_pick smoke FAIL — depth-2 low-difficulty still "
                "resolves to xai; aborting conservation run",
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
        "cycle": "mega-8",
        "conservation": True,
        "baseline_commit": BASELINE_COMMIT,
    }
    (OUT / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

    result_payload: dict = {}
    try:
        from services.agent.tiers import provider_for_tier
        from services.agent import loop

        # Fallback provider only. node_runner auto_pick-first prefers cheaper
        # models for low min_rank; this xAI instance is used when auto_pick fails
        # or for hard top-level nodes that select xai themselves.
        provider = provider_for_tier(FALLBACK_TIER)
        print(
            f"[mega-8] fallback_provider={type(provider).__name__} "
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
