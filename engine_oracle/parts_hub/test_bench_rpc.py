"""Unit tests for bench_rpc (GUI-launched gym benchmarks) — subprocess mocked."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def bench_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate bench_rpc status/logs under tmp and mock subprocess."""
    import engine_oracle.parts_hub.bench_rpc as br

    repo = tmp_path / "repo"
    reports = repo / "tools" / "flywheel" / "bench" / "gym" / "reports"
    reports.mkdir(parents=True)
    (repo / "benchmarks" / "leaderboard").mkdir(parents=True)

    monkeypatch.setattr(br, "_repo_root", lambda: repo)
    # Reset process-global state between tests.
    with br._state_lock:
        br._active_proc = None
        br._active_thread = None
    status = reports / "bench_status.json"
    if status.exists():
        status.unlink()

    class FakeProc:
        def __init__(self, rc: int = 0, delay: float = 0.05):
            self.pid = 4242
            self._rc = rc
            self._delay = delay
            self._done = threading.Event()
            self._thread = threading.Thread(target=self._finish, daemon=True)
            self._thread.start()

        def _finish(self) -> None:
            time.sleep(self._delay)
            self._done.set()

        def poll(self) -> int | None:
            return self._rc if self._done.is_set() else None

        def wait(self) -> int:
            self._done.wait(timeout=5.0)
            return self._rc

    calls: list[dict[str, Any]] = []

    def fake_popen(cmd, **kwargs):
        calls.append({"kind": "popen", "cmd": list(cmd), "kwargs": kwargs})
        rc = 0
        # Allow tests to force failure via env on the monkeypatched module.
        if getattr(br, "_TEST_FORCE_RC", 0):
            rc = int(br._TEST_FORCE_RC)
        return FakeProc(rc=rc, delay=getattr(br, "_TEST_DELAY", 0.05))

    def fake_run(cmd, **kwargs):
        calls.append({"kind": "run", "cmd": list(cmd), "kwargs": kwargs})
        # Write a minimal leaderboard so last_result_path resolves.
        lb = repo / "benchmarks" / "leaderboard" / "leaderboard.json"
        lb.write_text(
            json.dumps({"schema": "aicad.leaderboard/1", "models": []}),
            encoding="utf-8",
        )
        rc = int(getattr(br, "_TEST_LB_RC", 0))

        class R:
            returncode = rc

        return R()

    monkeypatch.setattr(br.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(br.subprocess, "run", fake_run)
    br._TEST_FORCE_RC = 0
    br._TEST_LB_RC = 0
    br._TEST_DELAY = 0.05

    return br, repo, reports, calls


def _emit(_data: dict) -> None:
    pass


def test_bench_run_returns_immediately(bench_env):
    br, _repo, reports, calls = bench_env
    out = br._bench_run({"mode": "diag", "tiers": ["xai:grok-4-5"]}, _emit)
    assert out["ok"] is True
    assert out["in_progress"] is True
    assert out["mode"] == "diag"
    assert out["tiers"] == ["xai:grok-4-5"]
    # Must not have blocked until the fake process finished.
    st = br._bench_status({}, _emit)
    assert st["ok"] is True
    assert st["running"] is True
    assert st["stage"] in ("starting", "running", "building_leaderboard", "done")


def test_bench_status_idle_when_never_run(bench_env):
    br, *_ = bench_env
    st = br._bench_status({}, _emit)
    assert st["ok"] is True
    assert st["running"] is False
    assert st["stage"] == "idle"


def test_bench_run_diag_cmd_and_leaderboard(bench_env):
    br, repo, reports, calls = bench_env
    br._TEST_DELAY = 0.02
    out = br._bench_run({"mode": "diag", "tiers": ["xai:grok-4-5", "local-24k"]}, _emit)
    assert out["ok"] is True

    # Wait for daemon thread to finish.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        st = br._bench_status({}, _emit)
        if not st.get("running") and st.get("stage") in ("done", "error"):
            break
        time.sleep(0.03)
    st = br._bench_status({}, _emit)
    assert st["running"] is False
    assert st["stage"] == "done"
    assert st["last_error"] is None
    assert st["last_result_path"]
    assert Path(st["last_result_path"]).exists()

    popens = [c for c in calls if c["kind"] == "popen"]
    runs = [c for c in calls if c["kind"] == "run"]
    assert len(popens) == 1
    cmd = popens[0]["cmd"]
    assert "tools.flywheel.bench.gym.cli" in cmd
    assert "diag" in cmd
    assert "--tiers" in cmd
    assert "xai:grok-4-5,local-24k" in cmd
    assert popens[0]["kwargs"].get("start_new_session") is True
    assert len(runs) == 1
    assert "tools.flywheel.bench.gym.leaderboard" in runs[0]["cmd"]
    assert "build" in runs[0]["cmd"]


def test_bench_run_smoke_cmd(bench_env):
    br, _repo, _reports, calls = bench_env
    br._TEST_DELAY = 0.02
    out = br._bench_run({"mode": "smoke", "tiers": ["local-24k"]}, _emit)
    assert out["ok"] is True
    deadline = time.time() + 3.0
    while time.time() < deadline:
        st = br._bench_status({}, _emit)
        if not st.get("running"):
            break
        time.sleep(0.03)
    popens = [c for c in calls if c["kind"] == "popen"]
    assert len(popens) == 1
    cmd = popens[0]["cmd"]
    assert "run" in cmd
    assert "--real" in cmd
    assert "--challenges" in cmd
    assert "C1" in cmd
    assert "--no-adjust" in cmd


def test_bench_run_rejects_bad_mode(bench_env):
    br, *_ = bench_env
    out = br._bench_run({"mode": "full", "tiers": ["local-24k"]}, _emit)
    assert out["ok"] is False
    assert "mode" in out["reason"]


def test_bench_run_already_running(bench_env):
    br, *_rest, calls = bench_env
    br._TEST_DELAY = 0.4
    first = br._bench_run({"mode": "diag", "tiers": ["local-24k"]}, _emit)
    assert first["ok"] is True
    second = br._bench_run({"mode": "diag", "tiers": ["local-24k"]}, _emit)
    assert second["ok"] is True
    assert second.get("already") is True or second.get("in_progress") is True
    # Wait so the daemon doesn't outlive the fixture.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not br._bench_status({}, _emit).get("running"):
            break
        time.sleep(0.03)


def test_bench_run_process_failure(bench_env):
    br, *_ = bench_env
    br._TEST_FORCE_RC = 7
    br._TEST_DELAY = 0.02
    br._bench_run({"mode": "diag", "tiers": ["local-24k"]}, _emit)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        st = br._bench_status({}, _emit)
        if not st.get("running"):
            break
        time.sleep(0.03)
    st = br._bench_status({}, _emit)
    assert st["running"] is False
    assert st["stage"] == "error"
    assert st["exit_code"] == 7
    assert st["last_error"]


def test_bench_methods_registered():
    from engine_oracle.parts_hub.bench_rpc import BENCH_METHODS

    assert "bench_run" in BENCH_METHODS
    assert "bench_status" in BENCH_METHODS


def test_worker_rpc_imports_bench_methods():
    from engine_oracle.parts_hub.worker_rpc import WORKER_METHODS

    assert "bench_run" in WORKER_METHODS
    assert "bench_status" in WORKER_METHODS
