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

import shutil
import tempfile
from pathlib import Path

import pytest

from services.mcp.private_hostd import private_daemon


@pytest.fixture(scope="session", autouse=True)
def _hermetic_config():
    """Point ``services.paths.CONFIG`` at a throwaway file for the whole session.

    Second half of the same "never touch the user's app" invariant as the private
    daemon below.  ``services/agent/config.py::load_config`` is consulted on EVERY tool
    dispatch (``get_tool_options`` + ``is_tool_family_enabled`` via
    ``tool_registry.deny_reason``), so the developer's own Settings became test input:
    with ``agent.tool_families.recon = false`` toggled off in Settings -> AI -> Tools,
    ``services/mcp/test_recon_tools.py`` and ``test_recon_hints.py`` went red on a box
    where nothing was broken.  A GUI toggle must never be able to turn this suite red.

    ``services/paths.py`` resolves ``ROOT``/``CONFIG`` at IMPORT time from
    ``XDG_DATA_HOME``, so exporting an env var from here is too late — every module that
    matters reads ``paths.CONFIG`` as an *attribute* on each call (verified: config.py,
    providers/sub_auth.py, research/common.py, serving/routing.py), so rebinding the
    attribute is both sufficient and immediate.  ``load_config``'s cache is keyed on
    ``str(paths.CONFIG)`` + mtime, so it invalidates itself; we still clear it on the way
    in and out so a config read from before/after the swap cannot leak across.

    The user's real config.json is never opened, read, or written by the suite.
    """
    import services.paths as paths
    from services.agent import config as agent_config

    tmp = Path(tempfile.mkdtemp(prefix="chudware-mcp-config-"))
    (tmp / "config.json").write_text("{}\n", encoding="utf-8")
    real = paths.CONFIG
    paths.CONFIG = tmp / "config.json"
    agent_config._cfg_cache = None
    agent_config._cfg_cache_path = None
    agent_config._cfg_cache_mtime_ns = None
    try:
        yield paths.CONFIG
    finally:
        paths.CONFIG = real
        agent_config._cfg_cache = None
        agent_config._cfg_cache_path = None
        agent_config._cfg_cache_mtime_ns = None
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _private_engine_daemon(_hermetic_config):
    with private_daemon():
        yield
