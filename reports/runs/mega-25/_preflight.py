#!/usr/bin/env python3
"""One-shot preflight for mega-25 (account pin + spawn_probe + auto_pick G18/Y + Y25/Z25 + U24/V24/S23/T23 + mega-22/21 green regression)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

spec = importlib.util.spec_from_file_location(
    "run_mega25", ROOT / "reports/runs/mega-25/run_mega.py"
)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

print("RUN_ID", mod.RUN_ID)
print("BASELINE", mod.BASELINE_COMMIT)
print("OUT", mod.OUT)
print("TARGET_XAI", mod.TARGET_XAI_ACCOUNT)

mod._patch_effort_maximum()

print("[preflight] subscription_accounts (pin 06dc3359)")
try:
    acct = mod._prefer_newest_xai_account()
    print(
        json.dumps(
            {
                k: acct.get(k)
                for k in (
                    "n_accounts",
                    "active_before_or_after",
                    "chosen_account_id",
                    "target_account_id",
                    "target_present",
                    "target_is_active",
                    "switched",
                )
            },
            indent=2,
        )
    )
except Exception as e:
    print("acct FAIL", e)
    traceback.print_exc()
    acct = {}

print("[preflight] spawn_probe")
try:
    probe = mod._write_spawn_probe()
    print("verdict", probe.get("verdict"))
    print("cap_sep", probe.get("capability_separation_ok"))
    print("leaf_exec", probe.get("leaf_feature_modeler_keeps_exec"))
    print("leg_roles", probe.get("leg_roles_present"))
    print("leg_tools", probe.get("leg_roles_have_domain_tools"))
    print("fix8", (probe.get("fix8_hard_ecad_chain_gate") or {}).get("verdict"))
    print("fix_wire", (probe.get("fix_wire_ecad_wire_power") or {}).get("verdict"))
    print("ecad_chain", probe.get("ecad_chain_tools_present"))
    print(
        "budget",
        probe.get("budget_tokens_preflight", {}).get("all_required_rendered_ok"),
    )
except Exception as e:
    print("probe FAIL", e)
    traceback.print_exc()
    probe = {}

print("[preflight] auto_pick_smoke")
try:
    smoke = mod._auto_pick_smoke()
    print("verdict", smoke.get("verdict"))
    print("axis_G18", smoke.get("axis_G18"))
    print("axis_Y", smoke.get("axis_Y"))
    print("hard_xai_grok45", smoke.get("hard_depth0_xai_grok45"))
    print("leaf_non_xai", smoke.get("leaf_depth2_non_xai"))
    print("leaf_local", smoke.get("leaf_depth2_local"))
    for c in smoke.get("cases") or []:
        print(" case", c)
except Exception as e:
    print("smoke FAIL", e)
    traceback.print_exc()
    smoke = {}

# no other xai consumer live (mega-17 is openrouter-only if still running)
import subprocess

other_xai = []
try:
    out = subprocess.check_output(
        ["pgrep", "-af", "run_mega|mission"], text=True, errors="ignore"
    )
    for line in out.splitlines():
        if "mega-23" in line:
            continue
        if "xai" in line.lower() or "grok" in line.lower():
            other_xai.append(line.strip())
except Exception:
    pass
# also note live mega processes
live_megas = []
try:
    out = subprocess.check_output(["pgrep", "-af", "run_mega"], text=True, errors="ignore")
    for line in out.splitlines():
        live_megas.append(line.strip())
except Exception:
    pass

active = str(acct.get("active_before_or_after") or "")
target = str(mod.TARGET_XAI_ACCOUNT)
fix8 = (probe.get("fix8_hard_ecad_chain_gate") or {}) if isinstance(probe, dict) else {}
fix_wire = (probe.get("fix_wire_ecad_wire_power") or {}) if isinstance(probe, dict) else {}
fix_m22 = (probe.get("fix_mega22_p22_q22_r22") or {}) if isinstance(probe, dict) else {}
fix_m23 = (probe.get("fix_mega23_s23_t23") or {}) if isinstance(probe, dict) else {}
fix_m25 = (probe.get("fix_mega25_y25_z25") or {}) if isinstance(probe, dict) else {}
summary = {
    "account_active": active,
    "account_target": target,
    "account_matches_target": active == target or active.startswith("06dc3359"),
    "target_is_active": bool(acct.get("target_is_active")),
    "spawn_probe": probe.get("verdict"),
    "auto_pick_smoke": smoke.get("verdict"),
    "fix8_hard_gate": fix8.get("verdict"),
    "fix8_detail": {
        k: fix8.get(k)
        for k in (
            "source_gate_present",
            "unit_test_present",
            "ecad_engineer_has_chain",
            "verdict",
        )
    },
    "fix_wire_gate": fix_wire.get("verdict"),
    "fix_wire_detail": {
        k: fix_wire.get(k)
        for k in (
            "source_has_real_wire",
            "source_force_wire_order",
            "unit_test_wire_before_board",
            "ecad_engineer_has_wire_power",
            "verdict",
        )
    },
    "fix_mega22": {
        "verdict": fix_m22.get("verdict"),
        "p22_source": fix_m22.get("p22_source"),
        "q22_source": fix_m22.get("q22_source"),
        "r22_source": fix_m22.get("r22_source"),
        "unit_test_present": fix_m22.get("unit_test_present"),
        "mega22_bound": probe.get("mega22_bound"),
        "detail": fix_m22.get("detail"),
    },
    "fix_mega23": {
        "verdict": fix_m23.get("verdict"),
        "s23_source": fix_m23.get("s23_source"),
        "t23_source": fix_m23.get("t23_source"),
        "unit_test_present": fix_m23.get("unit_test_present"),
        "mega23_bound": probe.get("mega23_bound"),
        "detail": fix_m23.get("detail"),
    },
    "axis_Y": smoke.get("axis_Y"),
    "axis_G18": smoke.get("axis_G18"),
    "hard_xai_grok45": smoke.get("hard_depth0_xai_grok45"),
    "hard_openrouter_paid": smoke.get("hard_depth0_openrouter_paid"),
    "leaf_local": smoke.get("leaf_depth2_local"),
    "live_mega_processes": live_megas,
    "other_xai_consumers": other_xai,
    "no_other_xai_consumer": len(other_xai) == 0,
    "preflight_ready_for_launch": all(
        [
            probe.get("verdict") == "PASS",
            smoke.get("verdict") == "PASS",
            fix8.get("verdict") == "PASS",
            fix_wire.get("verdict") == "PASS",
            smoke.get("axis_G18") == "PASS",
            bool(acct.get("target_is_active")),
            len(other_xai) == 0,
        ]
    ),
    "mega22_fixes_bound_probe": fix_m22.get("verdict") == "PASS",
    "mega23_fixes_bound_probe": fix_m23.get("verdict") == "PASS",
    "fix_mega25": {
        "verdict": fix_m25.get("verdict"),
        "y25_source": fix_m25.get("y25_source"),
        "z25_source": fix_m25.get("z25_source"),
        "unit_test_present": fix_m25.get("unit_test_present"),
        "mega25_bound": probe.get("mega25_bound"),
        "detail": fix_m25.get("detail"),
    },
    "mega25_fixes_bound_probe": fix_m25.get("verdict") == "PASS",
}
(mod.OUT / "preflight_summary.json").write_text(json.dumps(summary, indent=2))
print("SUMMARY", json.dumps(summary, indent=2))
