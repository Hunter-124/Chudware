#!/usr/bin/env python3
"""Aggregate standards gate for Chudware — runs the deterministic source-level checks and exits
non-zero if any fail. One command for pre-commit / CI.

    python scripts/check.py

Gates (all stdlib-only — they parse source/docs, so they need NO engine/OCCT deps and run fast):
  - gui_lint icons+theme  : icon names exist in the atlas; no applyTheme(.,1.0f) HiDPI-collapse bug.
                            (The heuristic `scale` rule is a manual ratchet against
                            scripts/gui_lint_baseline.json; it is not wired into any gate.)
  - feature_surface_audit : every visible ribbon command routes to a real handler (no dead buttons).
  - parity_audit          : every machine-checkable SW_PARITY_SPEC done/beyond claim resolves to real
                            code (no spec<->code drift).
  - agent_events_audit    : the aicad.agent_events/2 kind list (contracts/agent_events.json) matches
                            both emitters (core C++ + Python services), and the kRoles prompt catalog
                            matches services/agent/prompts/*.md (no C++<->Python agent drift).
  - oracle_import_audit   : no engine/engine_oracle imports in services/ outside
                            scripts/oracle_import_allowlist.txt (thin-client ratchet, T-RF20).
  - tool_panel_sync       : the Settings → AI → Tools fallback catalog tables
                            (host/src/ui/tool_panel_tables.h) match their snapshot
                            (tools/gen_tool_panel_tables.py --check).
  - advertised_tool_audit : every tool NAMED to the model in a prompt_note / hint / guidance
                            string is actually reachable (in a CATEGORY_PACKS pack, a mode
                            floor or a role loadout) and actually exists. Closes the
                            run-42508173312214 class: the harness told the model to "execute
                            with dag_run(...)" while dag_run was in no pack at all.
  - portable_paths_audit  : every tracked path can actually be checked out on Windows and
                            macOS — no NTFS-reserved characters, no trailing dot/space, no
                            reserved device names, no case-only collisions. Closes the class
                            where a ':' in a generated directory name made `git clone` fail
                            with "invalid path" and took the entire Windows CI leg down
                            before a single step ran.

The engine + agent pytest suites are NOT run here (they need the runtime venv); run them separately:
    <venv>/python -X utf8 -m pytest engine/doc services/agent -q
See docs/GUI_TOOLING.md for the full standards workflow + how to install this as a git pre-commit hook.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GATES = [
    ("gui_lint (icons)", ["scripts/gui_lint.py", "--rule", "icons"]),
    ("gui_lint (theme)", ["scripts/gui_lint.py", "--rule", "theme"]),
    ("feature_surface_audit (ribbon -> handlers)", ["scripts/feature_surface_audit.py"]),
    ("parity_audit (spec <-> code drift)", ["scripts/parity_audit.py"]),
    ("agent_events_audit (event kinds + role catalog)", ["scripts/agent_events_audit.py"]),
    ("oracle_import_audit (services thin-client ratchet)", ["scripts/oracle_import_audit.py"]),
    ("tool_panel_sync (Settings → AI → Tools fallback tables)", ["tools/gen_tool_panel_tables.py", "--check"]),
    ("advertised_tool_audit (named tools are reachable)", ["scripts/advertised_tool_audit.py"]),
    ("portable_paths_audit (tracked paths check out on Windows/macOS)", ["scripts/portable_paths_audit.py"]),
]


def main() -> int:
    failures: list[tuple[str, int]] = []
    for name, args in GATES:
        print(f"\n=== {name} ===", flush=True)
        rc = subprocess.run([sys.executable, *args], cwd=REPO).returncode
        if rc != 0:
            failures.append((name, rc))
    print("\n" + "=" * 64)
    if failures:
        for name, rc in failures:
            print(f"  FAIL  {name} (exit {rc})")
        print(f"\ncheck: {len(failures)} of {len(GATES)} gate(s) FAILED")
        return 1
    print(f"check: all {len(GATES)} gates PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
