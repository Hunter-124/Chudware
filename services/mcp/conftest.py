"""Give the MCP test suites their own engine daemon, so they cannot touch the user's app.

`core_bridge` defaults to daemon mode and connects to whatever `chudware-hostd` is already
listening — which on a developer box is the daemon behind the user's open Chudware window.
Three things then went wrong, all observed live on 2026-07-28:

  * Every part a test minted fanned out `doc.opened`, and the GUI opened a mirror tab for
    it. One `pytest services/mcp` run buried the user's tab strip in "AI: SwingArm",
    "AI: SwingArm2" ... "AI: SwingArm7", plus plate7/9/10, atom/atom2, sk/sk2 and ~30 more.
  * Those documents outlive the run, so the NEXT run's `new_part("SwingArm")` collided with
    a stale one and got a uniquified "SwingArm8" — the duplication compounding itself.
  * A run started right after `./build.sh daemon` saw a daemon older than the binary on
    disk and, per the bridge's version drain-replace, told it to DRAIN — killing the daemon
    out from under the window the user was working in.

The invariant that prevents all three is simply: a test process must never attach to a
daemon it did not start. Enforcing it here rather than in the daemon keeps hostd's wire
semantics untouched — `services/mcp/test_hostd_registry.py` and friends assert real
`origin_kind`, fanout and drain-replace behaviour, and must keep seeing exactly that.

The spawn itself lives in `services/mcp/private_hostd.py` because the ledger replay
(`tools/flywheel/replay_ledger_failures.py`) mints documents the same way and needs the
same isolation — one implementation, so a fix to the instance-key trap reaches both.
"""
from __future__ import annotations

import pytest

from services.mcp.private_hostd import private_daemon


@pytest.fixture(scope="session", autouse=True)
def _private_engine_daemon():
    with private_daemon():
        yield
