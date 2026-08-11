"""Live regression gate: the shared daemon can resolve its agent role prompts.

Guards the failure class fixed 2026-07-16 (git 0d8fb76f): the chudware-hostd unit had
no WorkingDirectory, so roles.cpp's relative "services/agent/prompts" fallback resolved against
nothing and EVERY agent_run_v2 threw "role prompt unreadable/empty: services/agent/prompts/
intake.md" — the web/local/agent part-builder was instant-fail on prod. install_webd.sh
now deploys the prompt tree and stamps CHUDWARE_PROMPTS_DIR into the unit.

This is a FAST gate (agent_load_prompt, no LLM call) so a prompts-deploy/env regression
surfaces in CI instead of when a user asks the agent to build a part. Env-gated like
live_smoke.py — needs a running daemon (CHUDWARE_LIVE=1).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the tests/ dir is importable regardless of pytest's import mode so the
# shared HostdClient (live_smoke.py) resolves whether run as a file or via the suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_smoke import SOCK, HostdClient  # noqa: E402

_LIVE = os.environ.get("CHUDWARE_LIVE") == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set CHUDWARE_LIVE=1 to run against live hostd socket",
)

# The ROLE prompts the C++ orchestrator (agent_run_v2) loads per stage. If any is
# unresolvable the whole run instant-fails, so assert the load-bearing ones resolve.
_ROLES = ("intake", "decomposer", "executor", "reviewer", "fixer")


def _hello(c: HostdClient) -> None:
    h = c.call("hello", {"client": "gui", "pid": 1, "proto": 1})
    assert (h.get("result") or {}).get("state") == "ready", h


def test_daemon_resolves_agent_role_prompts() -> None:
    if not os.path.exists(SOCK):
        pytest.skip(f"hostd socket missing: {SOCK}")
    c = HostdClient(SOCK, "prompts")
    try:
        _hello(c)
        for role in _ROLES:
            r = c.call("agent_load_prompt", {"role": role})
            assert r.get("error") is None, f"{role}: {r.get('error')}"
            res = r.get("result") or {}
            text = res.get("text", "")
            source = res.get("source", "")
            # Non-empty prompt body resolved from a real file — the exact condition
            # roles.cpp checks before throwing "role prompt unreadable/empty".
            assert text.strip(), f"{role}: empty prompt body (source={source!r})"
            assert source, f"{role}: no source path resolved"
    finally:
        c.close()
