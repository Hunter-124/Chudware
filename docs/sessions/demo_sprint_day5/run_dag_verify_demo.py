#!/usr/bin/env python3
"""T-C5 DAG-wiring demo: a firmware node whose end_condition is checked via a REAL fw_build ->
fw_sim_run call, run through the real services.dag.node_runner.run().

Scope note: T-A4 ("Verify nodes and DAG MCP tools") is still unclaimed/todo on the board — there
is no dedicated "verify node type" with programmatic end-condition checking yet. Today,
node_runner.run() is generic: it hands the node's end_condition to an agent (LLM) as part of the
request text and trusts the agent's own stop_reason/success self-report (see node_runner.py
_build_request/_map_verdict — confirmed by reading the source, not assumed). This script proves
that mechanism actually carries a firmware end_condition through correctly, using an `agent_fn`
that calls the REAL fw_build/fw_sim_run tools instead of a live LLM (node_runner.run()'s own
docstring: "Tests inject a mock here") — i.e. this is a real integration test of the wiring, with
the live-LLM half of the loop substituted the same documented way the project's own test suite
does it, not a mocked SimResult.

Usage: AI_CAD_FIRMWARE_DIR=<scratch dir> python3 docs/sessions/demo_sprint_day5/run_dag_verify_demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from engine.firmware import mcp_tools  # noqa: E402
from services.dag import node_runner  # noqa: E402


def firmware_agent_fn(request: str, context_pack: dict) -> SimpleNamespace:
    """Stand-in for the live LLM loop (node_runner.run()'s documented test-injection seam):
    actually drives fw_build -> fw_sim_run against the already-built T-C5 demo project and
    reports success based on the REAL SimResult, the same pass/fail an LLM agent reading the
    end_condition ("fw_sim_run assertions pass") would be expected to arrive at itself."""
    project_id = context_pack["node_inputs"]["project_id"]
    assert "fw_sim_run" in request or "assertions" in request, (
        "end_condition did not make it into the agent request text"
    )

    build = mcp_tools.fw_build({"project_id": project_id})
    if not build.get("success"):
        return SimpleNamespace(stop_reason="build_failed", success=False,
                                final_code=None, result=build)

    sim = mcp_tools.fw_sim_run({"project_id": project_id, "assertions": ["SOIL:"], "timeout_s": 15})
    return SimpleNamespace(
        stop_reason="done" if sim.get("passed") else "assertion_failed",
        success=bool(sim.get("passed")),
        final_code=None,
        result=sim,
    )


def main() -> int:
    # Reuse the project docs/sessions/demo_sprint_day5/run_demo.py already built+wrote main.c
    # into (run that script first if this project id doesn't exist).
    firmware_root = Path(__import__("os").environ.get("AI_CAD_FIRMWARE_DIR", ""))
    projects = sorted(firmware_root.glob("*/project.json")) if firmware_root.is_dir() else []
    if not projects:
        print("no firmware project found — run run_demo.py first with the same "
              "AI_CAD_FIRMWARE_DIR", file=sys.stderr)
        return 1
    project_id = json.loads(projects[-1].read_text(encoding="utf-8"))["project_id"]

    node = {
        "id": "n_firmware_verify",
        "type": "firmware_verify",
        "name": "Soil-moisture firmware: build + emulate + assert UART",
        "inputs": {},
        "on_fail": "escalate",
        "autonomy": "auto",
        "tools": ["fw_build", "fw_sim_run"],
        "end_condition": "fw_sim_run assertions pass: UART log contains 'SOIL:'",
    }
    context_pack = {
        "node_inputs": {"project_id": project_id},
        "retry_remaining": 1,
        "end_condition": node["end_condition"],
    }

    verdict, output_state = node_runner.run(node, context_pack, agent_fn=firmware_agent_fn)
    print(json.dumps({"verdict": verdict, "output_state": output_state}, indent=2, default=str))
    return 0 if verdict == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
