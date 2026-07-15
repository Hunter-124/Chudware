#!/usr/bin/env python3
"""One-shot preflight for mega-13 (account + spawn_probe + auto_pick)."""
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
    "run_mega13", ROOT / "reports/runs/mega-13/run_mega.py"
)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

print("RUN_ID", mod.RUN_ID)
print("BASELINE", mod.BASELINE_COMMIT)
print("OUT", mod.OUT)

mod._patch_effort_maximum()

print("[preflight] subscription_accounts")
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
    print("leaf_non_xai", smoke.get("leaf_depth2_non_xai"))
    for c in smoke.get("cases") or []:
        print(" case", c)
except Exception as e:
    print("smoke FAIL", e)
    traceback.print_exc()
    smoke = {}

active = str(acct.get("active_before_or_after") or "")
summary = {
    "account_active": active,
    "account_target_prefix": "17d17fb1",
    "account_matches_target": active.startswith("17d17fb1"),
    "spawn_probe": probe.get("verdict"),
    "auto_pick_smoke": smoke.get("verdict"),
    "preflight_ready_for_launch": all(
        [
            probe.get("verdict") == "PASS",
            smoke.get("verdict") == "PASS",
            bool(active),
        ]
    ),
}
(mod.OUT / "preflight_summary.json").write_text(json.dumps(summary, indent=2))
print("SUMMARY", json.dumps(summary, indent=2))
