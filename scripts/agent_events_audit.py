#!/usr/bin/env python3
"""agent_events_audit — drift check for the aicad.agent_events vocabulary + role prompt catalog.

The frozen `aicad.agent_events/2` vocabulary (prose contract: contracts/agent_events.md) is
emitted by TWO writers: the C++ agent runtime (core/agent/events.cpp) and the Python agent
runtime (services/agent/agent_events.py). The canonical machine-readable kind list is
contracts/agent_events.json. This gate FAILS when:

  * a kind is emitted in either runtime but absent from the JSON, or
  * a JSON kind has no emitter on either side — warnings only for kinds marked
    "optional": true (reserved kinds, or kinds whose producer lives outside the two
    scanned emitters: services/dag, core/agent/machine.cpp, the ask/interaction paths).

It also cross-checks the curated role catalog `kRoles` (core/agent/roles.cpp) against
services/agent/prompts/*.md:

  * a kRoles entry with no prompt file on disk is a stale order entry -> FAIL;
  * a prompts file not listed in kRoles is an allowed drop-in extra -> WARN
    (get_prompts_v2 appends it after the curated order; the Settings UX depends on
    kRoles staying the curated order, so extras are never an error).

Usage:
  python scripts/agent_events_audit.py     # text report, exit 0 (clean) / 1 (drift)

Exit code: 0 = no drift, 1 = drift found, 2 = parse/invocation error. Stdlib only.
Literal-string scanning is sufficient — both emitters name kinds with string literals.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_JSON = os.path.join(REPO, "contracts", "agent_events.json")
CPP_EMITTER = os.path.join(REPO, "core", "agent", "events.cpp")
PY_EMITTER = os.path.join(REPO, "services", "agent", "agent_events.py")
ROLES_CPP = os.path.join(REPO, "core", "agent", "roles.cpp")
PROMPTS_DIR = os.path.join(REPO, "services", "agent", "prompts")

# A kind literal: dotted vocabulary (`agent.tool_call`, `dag.run.done`, `doc.preview`,
# `mission.brief.signed`) or one of the flat legacy/telemetry kind names.
KIND_RE = re.compile(
    r'["\']('
    r"(?:agent|dag|mission|doc)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*"
    r"|ai_artifact(?:_action)?|doc_semantic_diff|plan_state|corporate"
    r')["\']'
)

# The kRoles block in roles.cpp: `const std::vector<std::string> kRoles = { ... };`
KROLES_RE = re.compile(r"kRoles\s*=\s*\{(.*?)\};", re.S)


def harvest_kinds(path: str) -> set[str]:
    """Kind string literals in one emitter file."""
    with open(path, encoding="utf-8") as fh:
        return set(KIND_RE.findall(fh.read()))


def harvest_kroles(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = KROLES_RE.search(text)
    if not m:
        raise RuntimeError(f"kRoles block not found in {path}")
    return re.findall(r'"([^"]+)"', m.group(1))


def harvest_prompt_files(prompts_dir: str) -> set[str]:
    """Top-level role prompt stems (shared/ cheatsheets are not roles)."""
    return {
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(prompts_dir, "*.md"))
    }


def main() -> int:
    try:
        with open(SPEC_JSON, encoding="utf-8") as fh:
            spec = json.load(fh)
        entries = {e["kind"]: e for e in spec["kinds"]}
        cpp_kinds = harvest_kinds(CPP_EMITTER)
        py_kinds = harvest_kinds(PY_EMITTER)
        kroles = harvest_kroles(ROLES_CPP)
        prompt_files = harvest_prompt_files(PROMPTS_DIR)
    except (OSError, json.JSONDecodeError, KeyError, RuntimeError) as exc:
        print(f"agent_events_audit: parse error: {exc}")
        return 2

    failures: list[str] = []
    warnings: list[str] = []
    emitted = cpp_kinds | py_kinds

    # --- Event-kind drift -----------------------------------------------------
    for kind in sorted(emitted - set(entries)):
        sides = []
        if kind in cpp_kinds:
            sides.append("core/agent/events.cpp")
        if kind in py_kinds:
            sides.append("services/agent/agent_events.py")
        failures.append(
            f"kind '{kind}' emitted in {' + '.join(sides)} but absent from "
            f"contracts/agent_events.json"
        )
    for kind, entry in sorted(entries.items()):
        if kind in emitted:
            continue
        note = entry.get("emitter_note", "")
        msg = f"JSON kind '{kind}' has no emitter in either scanned runtime ({note})"
        if entry.get("optional"):
            warnings.append(msg)
        else:
            failures.append(msg)

    # --- Role catalog <-> prompts dir -----------------------------------------
    for role in kroles:
        if role not in prompt_files:
            failures.append(
                f"kRoles entry '{role}' (core/agent/roles.cpp) has no "
                f"services/agent/prompts/{role}.md — stale catalog entry"
            )
    for stem in sorted(prompt_files - set(kroles)):
        warnings.append(
            f"services/agent/prompts/{stem}.md is not in kRoles — drop-in extra, "
            f"surfaced after the curated order (allowed)"
        )

    # --- Report -----------------------------------------------------------------
    print(f"canonical kinds: {len(entries)} "
          f"({sum(1 for e in entries.values() if not e.get('optional'))} required, "
          f"{sum(1 for e in entries.values() if e.get('optional'))} optional)")
    print(f"emitted: C++ {len(cpp_kinds)} kinds, Python {len(py_kinds)} kinds; "
          f"kRoles {len(kroles)} roles, prompts dir {len(prompt_files)} files")
    for w in warnings:
        print(f"  WARN  {w}")
    for f in failures:
        print(f"  FAIL  {f}")
    if failures:
        print(f"\nagent_events_audit: {len(failures)} drift item(s), "
              f"{len(warnings)} warning(s)")
        return 1
    print(f"\nagent_events_audit: clean ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
