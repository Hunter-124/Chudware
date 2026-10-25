"""Container admission and lifecycle regressions; no quota-admission bypasses."""
import asyncio
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

DEPLOY = Path(__file__).resolve().parents[1]
WEB = DEPLOY.parent
sys.path.insert(0, str(WEB))
from gateway import cells, engine_bridge
from gateway.config import CellBackend, Settings, settings

SPEC = importlib.util.spec_from_file_location("container_runtime", DEPLOY / "container/runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("child did not reach its expected lifecycle state")
        time.sleep(0.01)


@pytest.fixture
def container_layout(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="cw-container-", dir="/tmp") as raw:
        base = Path(raw)
        home, work, state, run = (base / name for name in ("data", "work", "state", "run"))
        for path in (home, work, state, run):
            path.mkdir(mode=0o700)
        image_marker = base / "image-marker"
        image_marker.write_text("chudware-container-v1\n")
        image_marker.chmod(0o444)
        monkeypatch.setattr(settings, "home", home)
        monkeypatch.setattr(settings, "workspace_root", work)
        monkeypatch.setattr(settings, "operator", "operator")
        monkeypatch.setattr(settings, "cell_backend", CellBackend.CONTAINER)
        monkeypatch.setattr(cells, "CONTAINER_STATE", state)
        monkeypatch.setattr(cells, "CONTAINER_MARKER", run / "runtime.json")
        monkeypatch.setattr(cells, "CONTAINER_IMAGE_MARKER", image_marker)
        # Model only image-layer ownership and the same-UID container PID 1.
        # Keep real descriptor walks, marker contents/modes and socket checks.
        image_info = image_marker.stat()
        image_identity = (image_info.st_dev, image_info.st_ino)
        image_owner = SimpleNamespace(uid=0)
        original_open = cells.open_beneath

        def open_image(root, relative, **kwargs):
            fd, info = original_open(root, relative, **kwargs)
            if (info.st_dev, info.st_ino) == image_identity:
                info = SimpleNamespace(st_uid=image_owner.uid, st_mode=info.st_mode)
            return fd, info

        monkeypatch.setattr(cells, "open_beneath", open_image)

        def supervisor_alive(pid, signum):
            # The host's root-owned PID 1 is not the container supervisor.
            assert (pid, signum) == (1, 0)

        monkeypatch.setattr(cells.os, "kill", supervisor_alive)
        principal = cells.resolve_workspace()
        directory = cells.prepare_cell(principal)
        listener = runtime.PrivateListener(directory, "gw.sock", cells)
        marker = {"schema": "chudware-container-v1", "supervisor_pid": 1, "uid": os.getuid(),
                  "home": str(home), "root": str(work), "state": str(state),
                  "cell_id": principal.cell_id, "workspace_key": principal.workspace_key,
                  "socket_device": listener.identity[0], "socket_inode": listener.identity[1]}
        runtime.write_json(run, "runtime.json", marker, cells)
        try:
            yield SimpleNamespace(base=base, home=home, work=work, state=state, run=run,
                                  principal=principal, directory=directory, listener=listener,
                                  marker=marker, image=image_marker, image_owner=image_owner)
        finally:
            listener.close()


def test_systemd_remains_default_and_container_requires_explicit_selection(monkeypatch):
    monkeypatch.delenv("CHUDWARE_WEB_CELL_BACKEND", raising=False)
    assert Settings().cell_backend is CellBackend.SYSTEMD
    monkeypatch.setenv("CHUDWARE_WEB_CELL_BACKEND", "container")
    assert Settings().cell_backend is CellBackend.CONTAINER


@pytest.mark.parametrize("value", ["host", "none", "Container", "container,systemd", "../../daemon"])
def test_unknown_backend_fails_startup(monkeypatch, value):
    monkeypatch.setenv("CHUDWARE_WEB_CELL_BACKEND", value)
    with pytest.raises(ValueError, match="must be systemd or container"):
        Settings()


def test_container_connection_skips_systemd_and_retains_pinned_listener(container_layout, monkeypatch):
    layout = container_layout

    async def forbidden(_cell_id):
        raise AssertionError("container connection attempted systemd activation")

    monkeypatch.setattr(cells, "start_cell", forbidden)

    async def connect():
        reader, writer = await cells.connect_cell(layout.principal)
        writer.write(b"private-cell\n")
        await writer.drain()
        peer, _ = layout.listener.socket.accept()
        with peer:
            assert peer.recv(128) == b"private-cell\n"
        writer.close()
        await writer.wait_closed()

    asyncio.run(connect())


@pytest.mark.parametrize("change", [
    {"actor_key": "another"}, {"workspace_key": "user:another"}, {"right": "viewer"},
    {"epoch": 1}, {"cell_id": "a" * 32},
])
def test_container_refuses_nonfixed_principals(container_layout, change):
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(replace(container_layout.principal, **change)))
    assert error.value.status_code == 403


def test_container_refuses_workspace_subdirectory_principal(container_layout):
    layout = container_layout
    child = layout.work / "child"
    child.mkdir()
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(replace(layout.principal, root=child)))
    assert error.value.status_code == 403


def test_container_fails_closed_without_supervisor_marker(container_layout):
    cells.CONTAINER_MARKER.unlink()
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(container_layout.principal))
    assert error.value.status_code == 503
    assert "entrypoint" in error.value.detail


@pytest.mark.parametrize("attack", ["missing", "contents", "writable", "symlink", "owner"])
def test_container_refuses_untrusted_image_marker(container_layout, attack):
    layout = container_layout
    if attack == "missing":
        layout.image.unlink()
    elif attack == "contents":
        layout.image.chmod(0o600)
        layout.image.write_text("not the supplied image\n")
        layout.image.chmod(0o444)
    elif attack == "writable":
        layout.image.chmod(0o666)
    elif attack == "symlink":
        original = layout.base / "original-image-marker"
        layout.image.rename(original)
        layout.image.symlink_to(original)
    else:
        layout.image_owner.uid = os.getuid() or 10001
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(layout.principal))
    assert error.value.status_code == 503


@pytest.mark.parametrize("field", [
    "schema", "supervisor_pid", "uid", "home", "root", "state", "cell_id", "workspace_key",
])
def test_container_refuses_mismatched_supervisor_marker(container_layout, field):
    layout = container_layout
    runtime.write_json(layout.run, "runtime.json", {**layout.marker, field: None},
                       cells, replace=True)
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(layout.principal))
    assert error.value.status_code == 503


@pytest.mark.parametrize("attack", ["contents", "public", "symlink", "owner"])
def test_container_refuses_unsafe_supervisor_marker(container_layout, monkeypatch, attack):
    layout = container_layout
    marker = layout.run / "runtime.json"
    if attack == "contents":
        marker.write_text("{")
    elif attack == "public":
        marker.chmod(0o644)
    elif attack == "symlink":
        original = layout.run / "original-runtime.json"
        marker.rename(original)
        marker.symlink_to(original)
    else:
        original_open = cells.open_beneath

        def foreign_owner(root, relative, **kwargs):
            fd, info = original_open(root, relative, **kwargs)
            if root / relative == marker:
                info = SimpleNamespace(st_uid=os.getuid() + 1, st_mode=info.st_mode)
            return fd, info

        monkeypatch.setattr(cells, "open_beneath", foreign_owner)
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(layout.principal))
    assert error.value.status_code == 503


@pytest.mark.parametrize("failure", [PermissionError, ProcessLookupError])
def test_container_fails_closed_when_supervisor_is_unavailable(container_layout, monkeypatch, failure):
    def unavailable(pid, signum):
        assert (pid, signum) == (1, 0)
        raise failure("supervisor unavailable")

    monkeypatch.setattr(cells.os, "kill", unavailable)
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(container_layout.principal))
    assert error.value.status_code == 503


def test_container_refuses_replaced_socket_even_for_same_uid(container_layout):
    layout = container_layout
    os.unlink(layout.directory / "gw.sock")
    replacement = runtime.PrivateListener(layout.directory, "gw.sock", cells)
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(cells.connect_cell(layout.principal))
        assert error.value.status_code == 403
        assert "identity changed" in error.value.detail
    finally:
        replacement.close()


def test_container_refuses_public_or_symlinked_socket(container_layout):
    layout = container_layout
    (layout.directory / "gw.sock").chmod(0o666)
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(layout.principal))
    assert error.value.status_code == 403
    (layout.directory / "gw.sock").unlink()
    (layout.directory / "gw.sock").symlink_to(layout.base / "outside.sock")
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(layout.principal))
    assert error.value.status_code == 403


def test_container_refuses_foreign_socket_owner(container_layout, monkeypatch):
    original = cells.os.fstat
    identity = container_layout.listener.identity

    def foreign_owner(fd):
        info = original(fd)
        if (info.st_dev, info.st_ino) == identity:
            return SimpleNamespace(st_uid=os.getuid() + 1, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(cells.os, "fstat", foreign_owner)
    with pytest.raises(HTTPException) as error:
        asyncio.run(cells.connect_cell(container_layout.principal))
    assert error.value.status_code == 403


def test_container_mesh_uses_durable_state_without_symlink_escape(container_layout):
    layout = container_layout
    mesh = layout.state / "mesh"
    mesh.mkdir()
    name = "a" * 32 + ".aicm"
    (mesh / name).write_bytes(b"published mesh")
    root, relative = engine_bridge._mesh_location(layout.principal, "/state/mesh/" + name)
    assert root == mesh and relative == name
    fd, _ = cells.open_beneath(root, relative)
    with os.fdopen(fd, "rb") as handle:
        assert handle.read() == b"published mesh"
    outside = layout.base / "outside"
    outside.mkdir()
    mesh.rename(layout.state / "old-mesh")
    mesh.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException):
        cells.open_beneath(root, relative)


def test_state_identity_is_stable_and_does_not_replace_saved_data(container_layout):
    layout = container_layout
    saved = layout.work / "part.step+"
    saved.write_bytes(b"saved model")
    identity = {"schema": "chudware-container-state-v1", "workspace_key": "user:operator"}
    runtime.write_json(layout.state, "container.json", identity, cells)
    runtime.write_json(layout.state, "container.json", identity, cells)
    assert saved.read_bytes() == b"saved model"
    with pytest.raises(RuntimeError, match="keep operator and volumes together"):
        runtime.write_json(layout.state, "container.json", {**identity, "workspace_key": "user:changed"}, cells)
    assert json.loads((layout.state / "container.json").read_text()) == identity


def test_runtime_rejects_host_invocation_before_starting_children(monkeypatch):
    monkeypatch.setattr(runtime.os, "getpid", lambda: 12345)
    with pytest.raises(RuntimeError, match="non-root PID 1"):
        runtime.guard_container(cells)


def test_runtime_reports_missing_complete_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "PAYLOAD", tmp_path)
    with pytest.raises(RuntimeError, match="required runtime payload missing.*chudware-hostd"):
        runtime.require_payload(cells)


def test_clean_environment_never_forwards_ambient_authority():
    environment = runtime.clean_environment({
        "OPENAI_API_KEY": "secret", "AWS_ACCESS_KEY_ID": "secret", "SSH_AUTH_SOCK": "/host/agent",
        "HOME": "/host/home", "PYTHONPATH": "/host/imports", "LD_PRELOAD": "/host/override.so",
        "HTTPS_PROXY": "http://host-proxy", "CHUDWARE_WORKER_BIN": "/host/worker",
        "CHUDWARE_WEB_OPERATOR": "alice", "CHUDWARE_WEB_ALLOWED_HOSTS": "cad.example.org",
    })
    assert not {"OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "SSH_AUTH_SOCK", "PYTHONPATH", "LD_PRELOAD",
                "CHUDWARE_WORKER_BIN"} & environment.keys()
    assert environment["HOME"] == "/state/home"
    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert environment["CHUDWARE_WEB_OPERATOR"] == "alice"
    assert environment["CHUDWARE_WEB_ALLOWED_HOSTS"] == "cad.example.org"


def start_supervisor(supervisor):
    failures = []

    def run():
        try:
            supervisor.run()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, failures


@pytest.mark.parametrize("exit_code", [0, 7])
def test_supervisor_restarts_idle_and_failed_hostd_with_same_listener(tmp_path, monkeypatch, exit_code):
    monkeypatch.setattr(runtime, "WORK", tmp_path)
    pinned_listener = runtime.PrivateListener(tmp_path, "listener.sock", cells)
    listener = pinned_listener.socket
    events = tmp_path / "launches"
    command = [sys.executable, "-c", (
        "import os,socket,sys,time; "
        f"s=socket.socket(fileno={listener.fileno()}); "
        "assert s.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN); "
        f"f=open({str(events)!r},'a'); f.write(str(os.getpid())+'\\n'); f.close(); "
        f"time.sleep(.03); sys.exit({exit_code})"
    )]
    stop = runtime.Stop()
    supervisor = runtime.Supervisor(command, [sys.executable, "-c", "import time;time.sleep(60)"],
                                    {}, listener, stop, restart_delay=0.01, shutdown_timeout=0.2)
    thread, failures = start_supervisor(supervisor)
    try:
        wait_for(lambda: events.exists() and len(events.read_text().splitlines()) >= 2)
    finally:
        stop.requested = True
        thread.join(5)
        pinned_listener.close()
    assert not thread.is_alive()
    assert failures == []
    assert supervisor.children == {}


def test_shutdown_reaps_own_children_and_leaves_unrelated_process_running(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WORK", tmp_path)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ready = tmp_path / "ready"
    child = [sys.executable, "-c", (
        "import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    )]
    stop = runtime.Stop()
    supervisor = runtime.Supervisor(child, [sys.executable, "-c", "import time;time.sleep(60)"],
                                    {}, listener, stop, shutdown_timeout=0.1)
    thread, failures = start_supervisor(supervisor)
    try:
        wait_for(ready.exists)
        children = list(supervisor.children.values())
        stop.signal(signal.SIGTERM, None)
        thread.join(5)
        assert not thread.is_alive()
        assert failures == []
        assert all(process.poll() is not None for process in children)
        assert unrelated.poll() is None
    finally:
        stop.requested = True
        thread.join(5)
        listener.close()
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_gateway_failure_stops_and_reaps_hostd(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "WORK", tmp_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    supervisor = runtime.Supervisor([sys.executable, "-c", "import time;time.sleep(60)"],
                                    [sys.executable, "-c", "import time;time.sleep(.1);raise SystemExit(9)"],
                                    {}, listener, runtime.Stop(), shutdown_timeout=0.2)
    try:
        with pytest.raises(RuntimeError, match="gateway exited.*9"):
            supervisor.run()
        assert supervisor.children == {}
    finally:
        listener.close()
