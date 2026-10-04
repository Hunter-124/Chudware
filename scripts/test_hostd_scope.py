"""hostd_scope must find and stop only the keyed run's own chudware-hostd."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostd_scope  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "hostd_scope.py"

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc + pidfd")


def _daemon_binary(directory: Path, name: str) -> Path:
    sleep = shutil.which("sleep")
    if sleep is None:
        pytest.skip("no sleep binary")
    binary = directory / name
    shutil.copy(sleep, binary)
    if subprocess.run([binary, "0"], check=False).returncode != 0:
        pytest.skip("sleep is a multi-call binary; a renamed copy does not run")
    return binary


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, check=False, timeout=60)


def test_only_this_runs_keyed_daemon_is_listed_and_reaped(tmp_path: Path) -> None:
    hostd = _daemon_binary(tmp_path, "chudware-hostd")
    impostor = _daemon_binary(tmp_path, "chudware-hostd-other")
    key = "gateimg-" + uuid.uuid4().hex
    procs: dict[str, subprocess.Popen[bytes]] = {}

    def spawn(name: str, binary: Path, env_key: str) -> None:
        procs[name] = subprocess.Popen([binary, "300"], env={"CHUDWARE_INSTANCE_KEY": env_key})

    try:
        # Same key, but alive before this run started: an earlier run's daemon.
        spawn("earlier", hostd, key)
        time.sleep(0.1)
        mark = _cli("now")
        assert mark.returncode == 0, mark.stderr
        since = mark.stdout.strip()
        time.sleep(0.05)
        spawn("own", hostd, key)
        spawn("foreign", hostd, key + "x")  # another run / the user's daemon
        spawn("prefix", hostd, key[:-1])
        spawn("not-hostd", impostor, key)
        time.sleep(0.2)

        listed = _cli("list", "--key", key, "--since", since)
        assert listed.returncode == 0, listed.stderr
        assert [line.split()[0] for line in listed.stdout.splitlines()] == [str(procs["own"].pid)]

        reaped = _cli("reap", "--key", key, "--since", since, "--grace", "5")
        assert reaped.returncode == 0, reaped.stderr
        assert reaped.stdout.split()[:2] == ["stopped", str(procs["own"].pid)]
        assert procs["own"].wait(timeout=10) == -15
        for name in ("earlier", "foreign", "prefix", "not-hostd"):
            assert procs[name].poll() is None, f"{name} daemon was touched"
        assert _cli("list", "--key", key, "--since", since).stdout == ""
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def test_reap_rechecks_identity_before_signalling(tmp_path: Path) -> None:
    hostd = _daemon_binary(tmp_path, "chudware-hostd")
    since = hostd_scope.start_ticks("self")
    proc = subprocess.Popen([hostd, "300"], env={"CHUDWARE_INSTANCE_KEY": "other-key"})
    try:
        time.sleep(0.1)
        # The identity re-check under the pidfd refuses a process with another key.
        assert hostd_scope.reap(str(proc.pid), "gate-key", since, os.geteuid(), 1.0)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)
