"""An external MCP client's disconnect must persist the documents it built.

``cad_common.save_touched_docs()`` is the only code that turns an AI-authored document
into a real file under ``CHUDWARE_PROJECT_SAVE_DIR``. The in-app agent calls it at its
turn boundary (mission_dispatch.run_linear_role_turn, worker_rpc._agent_admin_turn);
``services/mcp/server.py`` — the standalone server an external client drives — never
did, so a client that built parts and disconnected left them in hostd memory with only
a crash-recovery autosave it could no longer reach. These tests pin the four shutdown
paths that now sweep, the once-only rule between them, and the bound that stops a slow
save from turning a disconnect into a server that will not die.

DOCUMENT HYGIENE: nothing here mints a CAD document. Every test replaces
``cad_common.save_touched_docs`` with a fake, so ``_TOUCHED_DOCS`` is never read or
written and no engine call is made. The live end-to-end proof (a real assembly built
over stdio and durably saved on disconnect) is a manual probe, not a unit test: it needs
a private ``chudware-hostd`` and ~6 s per shutdown path.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_common as C  # noqa: E402
from services.mcp import server as S  # noqa: E402
from services.mcp import tool_registry as reg  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sweep_state(monkeypatch, tmp_path):
    """A fresh once-only latch per test, and never the user's real project folder."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path / "Projects"))
    monkeypatch.setattr(S, "_SAVE_STARTED", False)
    monkeypatch.setattr(S, "_SAVE_TIMED_OUT", False)
    S._SAVE_FINISHED.clear()
    S._SAVE_STATUS.clear()
    yield
    S._SAVE_FINISHED.clear()
    S._SAVE_STATUS.clear()


def _fake_sweep(monkeypatch, result=None, *, delay=0.0, boom=None):
    """Install a fake save_touched_docs and return the list of reasons it saw."""
    calls: list = []

    def fake() -> dict:
        calls.append(time.time())
        if delay:
            time.sleep(delay)
        if boom is not None:
            raise boom
        return result if result is not None else {"saved": [], "errors": [],
                                                  "persistence_failure": None}

    monkeypatch.setattr(C, "save_touched_docs", fake)
    return calls


def test_terminal_save_runs_the_sweep_and_returns_its_status(monkeypatch):
    calls = _fake_sweep(monkeypatch, {"saved": [{"doc_id": "d1", "name": "bracket",
                                                 "path": "/p/bracket.step+"}],
                                      "errors": [], "persistence_failure": None})

    out = S.terminal_save("stdio_eof")

    assert len(calls) == 1
    assert out["saved"] == [{"doc_id": "d1", "name": "bracket",
                             "path": "/p/bracket.step+"}]
    assert out["reason"] == "stdio_eof"


def test_the_sweep_runs_ONCE_however_many_shutdown_paths_fire(monkeypatch):
    """A SIGTERM that then closes the pipes must not save twice.

    Every path calls the same entry point, so the once-only latch is the only thing
    standing between a signal + EOF + atexit shutdown and three concurrent doc_save
    storms against the same documents.
    """
    calls = _fake_sweep(monkeypatch)

    first = S.terminal_save("signal:SIGTERM")
    second = S.terminal_save("stdio_eof")
    third = S.terminal_save("atexit")

    assert len(calls) == 1
    # Later callers still learn what happened — they read the published status.
    assert first["reason"] == second["reason"] == third["reason"] == "signal:SIGTERM"


def test_a_hanging_save_still_lets_the_process_exit(monkeypatch):
    """The bound is the whole reason the sweep is safe to put on the exit path.

    save_touched_docs talks to hostd and can drain a Box mirror with a 90 s budget of
    its own. Waiting for that is, from the client's side, a server that will not die.
    """
    _fake_sweep(monkeypatch, delay=30.0)

    t0 = time.time()
    out = S.terminal_save("orphan_watchdog", budget_s=0.4)
    elapsed = time.time() - t0

    assert out == {"reason": "orphan_watchdog", "timeout": True}
    assert elapsed < 5.0, f"shutdown blocked for {elapsed:.1f}s on a hung save"

    # A second shutdown path on the same wedged sweep must not spend a fresh budget:
    # signal -> KeyboardInterrupt -> main()'s finally -> atexit is three callers.
    t1 = time.time()
    again = S.terminal_save("stdio_eof", budget_s=30.0)
    assert again == {"reason": "stdio_eof", "timeout": True}
    assert time.time() - t1 < 1.0, "a later caller re-waited on the same hung sweep"


def test_a_raising_save_is_reported_not_propagated(monkeypatch):
    """Shutdown paths include a signal handler and an atexit hook; neither may raise."""
    _fake_sweep(monkeypatch, boom=RuntimeError("daemon gone"))

    out = S.terminal_save("stdio_eof")

    assert out["saved"] == []
    assert any("daemon gone" in e for e in out["errors"]), out


def test_main_sweeps_when_the_transport_returns(monkeypatch):
    """stdio EOF is the ordinary disconnect: the client closed our pipes and mcp.run
    returned. main() must sweep on the way out, not just on a signal."""
    calls = _fake_sweep(monkeypatch)
    served: list = []

    monkeypatch.setattr(S, "_reexec_with_fast_malloc", lambda: None)
    monkeypatch.setattr(S, "_install_shutdown_savers", lambda: None)  # tested separately
    monkeypatch.setattr(S, "_start_orphan_watchdog", lambda: None)
    monkeypatch.setattr(S, "_warm_core", lambda: None)
    monkeypatch.setattr(S, "_install_clean_stdout", lambda: None)
    monkeypatch.setattr(S.mcp, "run", lambda: served.append("ran"))

    S.main()

    assert served == ["ran"]
    assert len(calls) == 1, "main() returned without persisting the client's work"


def test_main_sweeps_even_when_the_transport_raises(monkeypatch):
    """A transport that dies mid-session is still a disconnect with work to save."""
    calls = _fake_sweep(monkeypatch)

    monkeypatch.setattr(S, "_reexec_with_fast_malloc", lambda: None)
    monkeypatch.setattr(S, "_install_shutdown_savers", lambda: None)
    monkeypatch.setattr(S, "_start_orphan_watchdog", lambda: None)
    monkeypatch.setattr(S, "_warm_core", lambda: None)
    monkeypatch.setattr(S, "_install_clean_stdout", lambda: None)

    def boom():
        raise BrokenPipeError("client vanished")

    monkeypatch.setattr(S.mcp, "run", boom)

    with pytest.raises(BrokenPipeError):
        S.main()

    assert len(calls) == 1


def test_signal_savers_sweep_and_then_chain_to_the_previous_handler(monkeypatch):
    """SIGTERM/SIGINT/SIGHUP must persist BEFORE the process dies, and must not swallow
    the signal: the previous disposition still runs, so exit codes stay honest."""
    calls = _fake_sweep(monkeypatch)
    chained: list = []
    saved_handlers = {}
    names = [n for n in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, n)]

    for name in names:
        sig = getattr(signal, name)
        saved_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda signum, frame, _n=name: chained.append(_n))
    try:
        S._install_shutdown_savers()
        for name in names:
            installed = signal.getsignal(getattr(signal, name))
            assert callable(installed) and installed.__name__ == "_saver", name
        # Fire one of them by hand: no real signal needed, the handler is a callable.
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        for sig, prev in saved_handlers.items():
            signal.signal(sig, prev)
        atexit.unregister(S.terminal_save)

    assert len(calls) == 1, "the signal handler did not persist the client's work"
    assert chained == ["SIGTERM"], "the previous handler was skipped"


def test_the_orphan_watchdog_saves_before_its_hard_exit(monkeypatch):
    """``os._exit(0)`` bypasses atexit entirely, which is precisely why the watchdog used
    to be the one exit path that guaranteed nothing was persisted. It must sweep first,
    and it must still hard-exit afterwards.

    The watchdog body is captured and run in THIS thread so the ordering assertion is
    exact and the hard exit surfaces as a catchable SystemExit instead of a race.
    """
    order: list = []
    captured: dict = {}
    real_thread = threading.Thread

    def fake_sweep() -> dict:
        order.append("save")
        return {"saved": [], "errors": [], "persistence_failure": None}

    def fake_exit(code):
        order.append(f"_exit({code})")
        raise SystemExit(code)

    def capture_thread(*args, target=None, name=None, **kwargs):
        if name == "chudware-orphan-watchdog":
            captured["target"] = target
            return SimpleNamespace(start=lambda: None)
        # the terminal-save worker is a real thread; only the poll loop is captured
        return real_thread(*args, target=target, name=name, **kwargs)

    ppids = iter([4242, 1])  # armed with 4242, then reparented to init

    monkeypatch.setattr(C, "save_touched_docs", fake_sweep)
    monkeypatch.setattr(S.os, "getppid", lambda: next(ppids))
    monkeypatch.setattr(S.os, "_exit", fake_exit)
    # Patch server.py's own bindings, not the stdlib modules.
    monkeypatch.setattr(S, "time", SimpleNamespace(sleep=lambda _s: None))
    monkeypatch.setattr(S, "threading", SimpleNamespace(
        Thread=capture_thread, Lock=threading.Lock, Event=threading.Event,
        current_thread=threading.current_thread, main_thread=threading.main_thread))
    monkeypatch.delenv("CHUDWARE_ORPHAN_WATCHDOG", raising=False)

    S._start_orphan_watchdog()
    assert "target" in captured, "the watchdog never armed"

    with pytest.raises(SystemExit):
        captured["target"]()

    assert order == ["save", "_exit(0)"], order


# --------------------------------------------------------------------------- registry

def test_task_list_reaches_the_external_registry_and_delegate_deliberately_does_not():
    """``task_list`` was registered ONLY into reviewer_exec's private throwaway FastMCP, so
    no external MCP client in any profile could publish a task list. It is a cheap
    always-on helper that spawns nothing, so it belongs on the shared registry.

    ``delegate`` is the opposite case and must STAY absent. It is modelled on purpose as a
    mission-scoped pseudo-tool with no registry callable so that, outside a mission,
    ``_unresolved_tool_error`` can name mission scope precisely instead of binding a
    callable that has no delegation tree behind it — see
    test_tool_binding_gaps.py::test_delegate_is_correctly_diagnosed_as_mission_scoped_not_dead
    and test_prompt_preview_autolearn.py::test_tool_catalog_covers_registry_and_guidance,
    both written after a live incident where a fabricated cause sent an engineer to mission
    plumbing and away from the real break. Registering it would only change the
    out-of-mission case, and only for the worse: MissionDispatch.resolve owns the
    run-scoped handler and is consulted first (agent_turn.py:1802-1817).
    """
    tools = S.mcp._tool_manager._tools
    assert "task_list" in tools
    assert reg.catalog()["task_list"]["family"] == "workspace"
    assert "delegate" not in tools, (
        "delegate must stay out of the shared registry; an external-facing mission entry "
        "point is the real fix, not a registration line")


def test_lite_keeps_the_design_ledger_and_the_task_list():
    """`lite` is the profile setup_mcp_clients.py recommends for capable external clients
    — the longest sessions, the ones that most need the anti-context-loss note store.
    `delegate` is absent here as it is in every profile — it is a mission-scoped pseudo-tool
    by design, not merely gated. See the registry test above."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import json; from services.mcp import server as S;"
         "t = S.mcp._tool_manager._tools;"
         "print(json.dumps({'n': len(t), 'names': sorted(t)}))"],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=300,
        env=dict(os.environ, CHUDWARE_MCP_PROFILE="lite", PYTHONPATH=_REPO_ROOT,
                 CHUDWARE_ORPHAN_WATCHDOG="0"),
    )
    assert out.returncode == 0, out.stderr[-2000:]
    names = set(json.loads(out.stdout.strip().splitlines()[-1])["names"])

    assert {"ledger_get", "ledger_note", "task_list"} <= names
    assert "delegate" not in names
    assert "dag_plan" not in names and "mission_start" not in names  # still gated
