#!/usr/bin/env python3
"""agent_events_audit — drift check for the aicad.agent_events vocabulary.

The frozen `aicad.agent_events/2` vocabulary (prose contract: contracts/agent_events.md) is
emitted by the Python agent runtime (services/agent/agent_events.py); other producers
(services/dag, the ask/interaction paths, OMP event mapping) live outside that file. The
canonical machine-readable kind list is contracts/agent_events.json. This gate FAILS when:

  * a kind is emitted in services/agent/agent_events.py but absent from the JSON, or
  * a JSON kind has no emitter there — warnings only for kinds marked "optional": true
    (reserved kinds, or kinds whose producer lives outside the scanned emitter:
    services/dag, services/agent/omp_events.py, the ask/interaction paths).

Usage:
  python scripts/agent_events_audit.py     # text report, exit 0 (clean) / 1 (drift)

Exit code: 0 = no drift, 1 = drift found, 2 = parse/invocation error. Stdlib only.
Literal-string scanning is sufficient — the emitter names kinds with string literals.
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_JSON = os.path.join(REPO, "contracts", "agent_events.json")
PY_EMITTER = os.path.join(REPO, "services", "agent", "agent_events.py")

# A kind literal: dotted vocabulary (`agent.tool_call`, `dag.run.done`, `doc.preview`,
# `mission.brief.signed`) or one of the flat legacy/telemetry kind names.
KIND_RE = re.compile(
    r'["\']('
    r"(?:agent|dag|mission|doc)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*"
    r"|ai_artifact(?:_action)?|doc_semantic_diff|plan_state"
    r')["\']'
)


def harvest_kinds(path: str) -> set[str]:
    """Kind string literals in one emitter file."""
    with open(path, encoding="utf-8") as fh:
        return set(KIND_RE.findall(fh.read()))


def main() -> int:
    try:
        with open(SPEC_JSON, encoding="utf-8") as fh:
            spec = json.load(fh)
        entries = {e["kind"]: e for e in spec["kinds"]}
        emitted = harvest_kinds(PY_EMITTER)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"agent_events_audit: parse error: {exc}")
        return 2

    failures: list[str] = []
    warnings: list[str] = []

    for kind in sorted(emitted - set(entries)):
        failures.append(
            f"kind '{kind}' emitted in services/agent/agent_events.py but absent from "
            f"contracts/agent_events.json"
        )
    for kind, entry in sorted(entries.items()):
        if kind in emitted:
            continue
        note = entry.get("emitter_note", "")
        msg = f"JSON kind '{kind}' has no emitter in the scanned runtime ({note})"
        if entry.get("optional"):
            warnings.append(msg)
        else:
            failures.append(msg)

    print(f"canonical kinds: {len(entries)} "
          f"({sum(1 for e in entries.values() if not e.get('optional'))} required, "
          f"{sum(1 for e in entries.values() if e.get('optional'))} optional)")
    print(f"emitted in services/agent/agent_events.py: {len(emitted)} kinds")
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
