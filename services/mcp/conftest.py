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

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from services.mcp.private_hostd import private_daemon

# Opt OUT of the root conftest's in-process engine isolation. That fixture forces
# CHUDWARE_ENGINE_DAEMON=0 for every other suite (no shared daemon can exist, so no
# document pollution is possible), but THIS suite asserts real daemon wire semantics —
# test_hostd_registry.py and friends check origin_kind, fanout and drain-replace — so it
# needs an actual daemon, just a PRIVATE one. Set at import time, which is before any
# fixture runs, so the root fixture sees it and stands down.
os.environ.setdefault("CHUDWARE_TEST_WANTS_DAEMON", "1")


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
    """Give this suite its OWN hostd, or SKIP — never run against the user's.

    ``private_daemon()`` yields False when it could not isolate: no hostd binary built,
    or it failed to come up within the timeout. That flag used to be DISCARDED here, so a
    missing or broken binary silently handed the suite the user's live daemon — the exact
    outcome this fixture exists to prevent, in the one path nobody would look at. Every
    test then passed while depositing documents into the window the user was working in.

    The degrade is a SKIP naming the missing prerequisite, not a fallback, because neither
    alternative is honest. Attaching to the user's daemon corrupts their app. Falling back
    to in-process shares nothing (good) but is NOT equivalent for a full-suite run: these
    2000+ tests then share ONE process-wide document pool for the whole session instead of
    a daemon's, and the accumulation makes doc resolution ambiguous — measured 2026-07-29
    in a worktree with no daemon build, where ~44 ECAD/FEA/CFD tests failed in batch while
    every one of them passed in isolation. Reporting a build prerequisite as 44 unrelated
    geometry failures is worse than saying what is missing.
    """
    import os

    with private_daemon() as isolated:
        if not isolated and not os.environ.get("CHUDWARE_HOSTD_SOCKET") \
                and os.environ.get("CHUDWARE_ENGINE_DAEMON") != "0":
            pytest.skip(
                "services/mcp needs a PRIVATE chudware-hostd and one could not be "
                "started, so this suite would otherwise attach to the user's live "
                "daemon and deposit documents in their open app. Build the daemon "
                "(./build.sh daemon, giving core/build-occt-linux/bin/chudware-hostd), "
                "or set CHUDWARE_ENGINE_DAEMON=0 to accept in-process mode explicitly "
                "(single shared document pool — expect order-dependent failures).",
                allow_module_level=True)
        yield isolated


@pytest.fixture(autouse=True)
def _no_publisher_outlives_its_test():
    """Stop deferred handoff publishes at the end of every test.

    ``cad_common._publish_current`` coalesces an edit burst onto a daemon Timer named
    ``chudware-publish-<doc_id>`` whose body is a WHOLE publish — a full ``doc_save``
    plus the ``doc_list``/``doc_close`` calls behind the name lookup, the closed-doc
    prune and the same-name supersede — and nothing cancels it when the test that
    triggered it ends. Measured 2026-09-06 under ``pytest -q services/mcp``: a publish
    armed by ``test_sketch3d_tools.py`` was still issuing engine calls inside
    ``test_terminal_save_sweep.py``, whose bridge is a fake that records every
    ``doc_save`` — so the terminal sweep was credited with a save it never made and a
    DIFFERENT test of that file failed on each gate run. Bounded, so a wedged engine
    call cannot hang the suite; the publish thread is a daemon either way.
    """
    yield
    from services.mcp import cad_common as _cad_common

    _cad_common.quiesce_publishes(10.0)


_MCP_DIR = Path(__file__).resolve().parent


def _still_inside_mcp(nextitem) -> bool:
    if nextitem is None:
        return False
    try:
        return Path(str(nextitem.path)).resolve().parent == _MCP_DIR
    except Exception:  # noqa: BLE001 — an unresolvable next item means "we are leaving"
        return False


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    """Hand the NEXT package an empty engine document pool.

    This suite mints documents by the thousand and deliberately leaves them open —
    that is the shape of the tools it tests, and under the private daemon above the
    documents die with the daemon, so it costs nothing. Under
    ``CHUDWARE_ENGINE_DAEMON=0`` (in-process legacy, which is how the combined
    ``pytest services/mcp services/agent services/dag`` run is invoked) there is no
    daemon to die: the pool is a process global inside ``libchudware``, shared with
    every suite that runs after this one.

    ``cad_common.save_touched_docs()`` — the terminal sweep every linear role turn
    runs — claims any open doc whose ``origin_kind`` is ``mcp``/``agent`` OR that is
    listed in the module-global ``cad_common._TOUCHED_DOCS``. So an unsaved part left
    behind here makes a LATER, unrelated turn report ``no_project_save_dir`` and come
    back ``success=False, stop_reason="persistence_failed"`` with its handoff rewritten
    to ``partial``. Measured with ``pytest -q services/mcp/test_cad_tools.py
    services/agent/test_harness_integrity.py``: two green-in-isolation tests fail, and
    the full combined run turns eight red across four ``services/agent`` files.

    Same defect class, same remedy as
    ``services/agent/test_arg_reconciliation.py::_discard_documents_minted_by_this_file``
    — a minter cleans up after itself — hoisted to the package boundary because doing
    it per test would break the ``_CUR``/resume continuity these tools are tested for.
    ``trylast`` so function-scoped teardown (notably the publish quiesce above) has
    already run and cannot re-touch a doc after the sweep.
    """
    if _still_inside_mcp(nextitem):
        return
    from services.mcp import cad_common as _cad_common
    from services.mcp import core_bridge as _cb

    try:
        rows = (_cb.call("doc_list", {}) or {}).get("docs", []) or []
    except Exception:  # noqa: BLE001 — no engine (or a torn-down daemon) is already clean
        rows = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get("doc_id")
        # `gui` docs are the user's own; the suite never minted one and must not
        # close one even if a stray daemon put it in view.
        if not doc_id or entry.get("origin_kind") == "gui":
            continue
        try:
            _cb.call("doc_close", {"doc_id": doc_id})
        except Exception:  # noqa: BLE001 — cleanup never fails a test
            pass
    _cad_common._TOUCHED_DOCS.clear()
    _cad_common._PROCESS_DOCS.clear()
    _cad_common._CUR["doc_id"] = None
