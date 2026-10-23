#!/usr/bin/env python3
"""Aggregate standards gate for Chudware — runs the deterministic source-level checks and exits
non-zero if any fail. One command for pre-commit / CI.

    python scripts/check.py

Interpreter: Python >= 3.11 with the runtime audit imports available. The source parsers
need no engine/OCCT build, but advertised_tool_audit imports the live services.mcp.tool_registry
and services.agent catalog (including their transitive dependencies). Use the runtime venv
provisioned by scripts/provision_venv.sh; CI supplies CHECK_PYTHON. requirements.txt is the
runtime dependency authority, not a promise that this aggregate gate is standard-library only.

Gates:
  - gui_lint icons+theme  : icon names exist in the atlas; no applyTheme(.,1.0f) HiDPI-collapse bug.
                            (The heuristic `scale` rule is a manual ratchet against
                            scripts/gui_lint_baseline.json; it is not wired into any gate.)
  - feature_surface_audit : every visible ribbon command routes to a real handler (no dead buttons).
  - parity_audit          : every explicit SW_PARITY_SPEC evidence pointer (compiler / op / rpc /
                            ribbon / pm_form) resolves to live code; a missing evidence source
                            or an empty harvest fails the gate.
  - agent_events_audit    : the aicad.agent_events/2 kind list (contracts/agent_events.json) matches
                            both emitters (core C++ + Python services), and the kRoles prompt catalog
                            matches services/agent/prompts/*.md (no C++<->Python agent drift).
  - oracle_import_audit   : no import of the deleted engine/engine_oracle packages in services/,
                            scripts/ or tools/ outside scripts/oracle_import_allowlist.txt
                            (intentionally empty; thin-client ratchet).
  - tool_panel_sync       : the Settings → AI → Tools fallback catalog tables
                            (host/src/ui/tool_panel_tables.h) match their snapshot
                            (tools/gen_tool_panel_tables.py --check).
  - advertised_tool_audit : every tool NAMED to the model in a prompt_note / hint / guidance
                            string is actually reachable (in a CATEGORY_PACKS pack, a mode
                            floor or a role loadout) and actually exists.
  - portable_paths_audit  : every tracked path can actually be checked out on Windows and
                            macOS — no NTFS-reserved characters, no trailing dot/space, no
                            reserved device names, no case-only collisions.
  - ui_shared_audit       : frozen UI vocabulary, icons and font assets agree with their manifest.

The Python service suites are NOT run here; scripts/gate.sh runs them per unit under services/,
tools/, scripts/ and assets/ with the runtime venv, e.g.:
    .venv-mcp/bin/python -X utf8 -m pytest services/agent -q
Pre-commit install: see scripts/hooks/pre-commit.
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
    ("oracle_import_audit (services/scripts/tools thin-client ratchet)", ["scripts/oracle_import_audit.py"]),
    ("tool_panel_sync (Settings → AI → Tools fallback tables)", ["tools/gen_tool_panel_tables.py", "--check"]),
    ("advertised_tool_audit (named tools are reachable)", ["scripts/advertised_tool_audit.py"]),
    ("portable_paths_audit (tracked paths check out on Windows/macOS)", ["scripts/portable_paths_audit.py"]),
    ("ui_shared_audit (shared UI manifest and assets)", ["scripts/verify-ui-shared.py"]),
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
