"""Real private-artifact/socket regressions, never using an existing daemon."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from services.mcp import daemon_bridge
from services.mcp.daemon_bridge import DaemonClient

ROOT = Path(__file__).resolve().parents[2]
HOSTD = daemon_bridge.resolve_hostd_bin()


def test_native_ipc_privacy_and_socket_activation(tmp_path):
    """Compile the actual transport/discovery/logger, with no engine dependency.

    On Windows this exercises ACLs and private singleton handle exclusion; on
    POSIX it exercises 022 modes, symlink/hardlink refusal and adopted listeners.
    """
    configured = os.environ.get("CXX")
    compiler = configured or shutil.which("c++") or shutil.which("g++")
    if not compiler and sys.platform == "win32":
        compiler = shutil.which("cl")
    if not compiler:
        pytest.skip("native C++ compiler unavailable")
    sources = [
        ROOT / "services/ipc/test_ipc_privacy.cpp",
        ROOT / "services/ipc/ipc_transport.cpp",
        ROOT / "services/ipc/ipc_transport_win.cpp",
        ROOT / "services/ipc/ipc_discovery.cpp",
        ROOT / "services/ipc/ipc_discovery_win.cpp",
        ROOT / "core/hostd/hostd_log.cpp",
    ]
    binary = tmp_path / ("ipc-privacy.exe" if sys.platform == "win32" else "ipc-privacy")
    # Compiler discovery can return an MSVC path containing spaces; it is one
    # executable argument, not a shell command. Only explicit CXX accepts flags.
    command = shlex.split(compiler) if configured else [compiler]
    if Path(command[0]).name.lower() in {"cl", "cl.exe"}:
        command += ["/nologo", "/std:c++17", "/EHsc", "/DCHUDWARE_DEVELOPER_BUILD=1",
                    "/I" + str(ROOT / "core"), "/Fe:" + str(binary)]
        command += list(map(str, sources)) + ["/link", "advapi32.lib"]
    else:
        command += ["-std=c++17", "-pthread", "-DCHUDWARE_DEVELOPER_BUILD=1",
                    "-I" + str(ROOT / "core"), *map(str, sources), "-o", str(binary)]
        if sys.platform == "win32":
            command += ["-ladvapi32"]
    compiled = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    # Keep the Unix socket comfortably below sockaddr_un.sun_path's bound.
    with tempfile.TemporaryDirectory(prefix="ipc-privacy-") as directory:
        exercised = subprocess.run([str(binary), directory], capture_output=True,
                                   text=True, timeout=30)
        assert exercised.returncode == 0, exercised.stdout + exercised.stderr


@pytest.mark.skipif(HOSTD is None or sys.platform == "win32",
                    reason="POSIX built hostd required for mode regression")
def test_autosave_and_child_logs_private_under_022(monkeypatch, tmp_path):
    with tempfile.TemporaryDirectory(prefix="hostd-private-") as directory:
        root = Path(directory)
        # Deliberately traversable ancestors: the app, not our test umask/home,
        # must create the confidentiality boundary.
        root.chmod(0o755)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        data = root / "data"
        data.mkdir(mode=0o755)
        home = root / "home"
        home.mkdir(mode=0o755)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CHUDWARE_HOSTD_SOCKET", str(root / "hostd.sock"))
        monkeypatch.setenv("CHUDWARE_INSTANCE_KEY", "privacy-" + uuid.uuid4().hex)
        monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "60")
        monkeypatch.setenv("CHUDWARE_AUTOSAVE_INTERVAL_S", "0.2")
        monkeypatch.setenv("CHUDWARE_HOSTD_STALECHECK_S", "0")
        startup_log = root / "startup.log"
        with startup_log.open("wb") as stderr:
            process = subprocess.Popen([str(HOSTD), "--foreground"], env=dict(os.environ),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=stderr, umask=0o022)
        client = DaemonClient()
        try:
            # Attach-only connect fails immediately before Popen's child binds.
            # Retry only this isolated scratch socket; never spawn a replacement.
            deadline = time.monotonic() + 15
            last_error = "scratch socket not ready"
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    client.connect(spawn=False, budget_s=max(0.01, deadline - time.monotonic()))
                except daemon_bridge.DaemonError as error:
                    last_error = str(error)
                    client.close()
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                else:
                    break
            else:
                pytest.fail(
                    f"scratch daemon did not become ready: pid={process.pid}, "
                    f"returncode={process.poll()}, socket={root / 'hostd.sock'}, "
                    f"last connection error={last_error}\n"
                    f"stderr:\n{startup_log.read_text(errors='replace')}"
                )
            assert client.call("daemon_status")["pid"] == process.pid
            created = client.call("doc_new", {"name": "private-design-fixture"})
            assert not created.get("_is_error"), created
            doc_id = created["doc_id"]
            edited = client.call("doc_apply_ops", {"doc_id": doc_id, "ops": [
                {"op": "add_feature", "feature": {"type": "box", "name": "private-box",
                 "params": {"length": 10, "width": 10, "height": 10}, "refs": {}}},
            ]})
            assert not edited.get("_is_error"), edited
            spawned = client.call("agent_spawn", {"argv": [sys.executable, "-c",
                "import sys; print('private-child-output'); print('private-stderr', file=sys.stderr)"]})
            assert not spawned.get("_is_error"), spawned
            state = data / "Chudware"
            deadline = time.monotonic() + 20
            snapshots = []
            while time.monotonic() < deadline:
                snapshots = list(state.rglob(doc_id + ".step+"))
                if snapshots and snapshots[0].with_suffix(".json").exists():
                    break
                assert process.poll() is None, "scratch daemon exited during autosave"
                client.call("ping")
                time.sleep(0.1)
            assert snapshots, "dirty document was not autosaved"
            snapshot = snapshots[0]
            assert snapshot.with_suffix(".json").exists()
            assert "private-design-fixture" in snapshot.with_suffix(".json").read_text()
            child_log = Path(spawned["log"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and "private-stderr" not in child_log.read_text():
                time.sleep(0.05)
            assert "private-child-output" in child_log.read_text()
            assert "private-stderr" in child_log.read_text()
            assert stat.S_IMODE(state.stat().st_mode) == 0o700
            for path in state.rglob("*"):
                mode = stat.S_IMODE(path.stat().st_mode)
                assert mode == (0o700 if path.is_dir() else 0o600), (path, oct(mode))
            # Tightening the application leaf must not chmod user-owned ancestors.
            assert stat.S_IMODE(data.stat().st_mode) == 0o755
            assert stat.S_IMODE(home.stat().st_mode) == 0o755
        finally:
            client.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
