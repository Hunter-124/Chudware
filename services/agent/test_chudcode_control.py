from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import subprocess
import shlex
import threading
import time
from pathlib import Path

import pytest

from services import worker_main
from services.agent.chudcode_control import MAX_AUTH_FAILURES, ControlServer


@pytest.fixture
def runtime_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="chudcode-control-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _request(info: dict, body: dict, *, raw: bytes | None = None) -> tuple[dict, bytes]:
    if info["transport"] == "unix":
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(info["endpoint"])
    elif info["transport"] == "named_pipe":
        from services.ipc.windows_pipe import WindowsPipe
        client = WindowsPipe.connect(info["endpoint"])
    else:
        raise AssertionError("unsupported control transport")
    try:
        payload = raw if raw is not None else json.dumps(body).encode("utf-8") + b"\n"
        client.sendall(payload)
        received = bytearray()
        while not received.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            received.extend(chunk)
        return json.loads(received), bytes(received)
    finally:
        client.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX Unix socket permissions")
def test_posix_endpoint_is_private_and_authenticated(runtime_dir: Path) -> None:
    calls: list[tuple[str, dict]] = []
    server = ControlServer(
        lambda action, params: calls.append((action, params)) or {"state": "idle"},
        runtime_dir,
    ).start()
    try:
        info = server.info()
        assert info.keys() == {"transport", "endpoint", "token"}
        assert info["transport"] == "unix"
        assert len(info["token"]) == 64
        endpoint = Path(info["endpoint"])
        assert endpoint.parent == runtime_dir
        assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600

        denied, wire = _request(
            info, {"token": "wrong", "action": "status", "params": {}}
        )
        assert denied == {
            "ok": False,
            "error": {"code": "unauthorized", "message": "unauthorized"},
        }
        assert info["token"].encode() not in wire
        assert calls == []

        accepted, _ = _request(
            info,
            {"token": info["token"], "action": "status", "params": {"run_id": "r1"}},
        )
        assert accepted == {"ok": True, "result": {"state": "idle"}}
        assert calls == [("status", {"run_id": "r1"})]
    finally:
        endpoint = Path(server.info()["endpoint"])
        server.stop()
    assert not endpoint.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX Unix socket path limits")
def test_long_runtime_keeps_private_authenticated_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="cwctl-test-", dir="/tmp") as root:
        name = f"chudcode-worker-{os.getppid()}-{os.getpid()}.sock"
        # Characters fit sun_path, but the encoded path does not.
        component = "é" * (102 - len(os.fsencode(Path(root) / name)))
        runtime = Path(root) / component
        assert len(str(runtime / name)) == 103
        assert len(os.fsencode(runtime / name)) > 103
        monkeypatch.setattr(tempfile, "tempdir", str(runtime))
        server = ControlServer(
            lambda _action, _params: {"state": "idle"}, runtime
        ).start()
        try:
            info = server.info()
            endpoint = Path(info["endpoint"])
            assert len(os.fsencode(endpoint)) <= 103
            assert stat.S_IMODE(endpoint.parent.stat().st_mode) == 0o700
            assert endpoint.parent.stat().st_uid == os.geteuid()
            assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600
            denied, _ = _request(info, {"token": "wrong", "action": "status"})
            assert denied["error"]["code"] == "unauthorized"
            accepted, _ = _request(
                info, {"token": info["token"], "action": "status", "params": {}}
            )
            assert accepted == {"ok": True, "result": {"state": "idle"}}
        finally:
            server.stop()
        assert not endpoint.parent.exists()
        assert runtime.is_dir()


def test_release_action_dispatches_to_the_session_host(runtime_dir, monkeypatch):
    from services.agent import worker_rpc
    from services.agent.chudcode_host import ChudcodeSessionHost
    from services.agent.test_chudcode_host import FakeFactory, run

    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    monkeypatch.setattr(worker_rpc, "get_default_chudcode_host", lambda: host)
    server = ControlServer(worker_rpc.chudcode_control, runtime_dir).start()
    try:
        assert run(host, "a", "a1")[0]["ok"]
        info = server.info()
        request = {
            "token": info["token"],
            "action": "release",
            "params": {"session_id": "a"},
        }

        released, _ = _request(info, request)

        assert released == {"ok": True, "result": {"ok": True, "released": True}}
        assert factory.clients[0].stop_count == 1
        repeated, _ = _request(info, request)
        assert repeated == {"ok": True, "result": {"ok": True, "released": False}}
        assert factory.clients[0].stop_count == 1
    finally:
        server.stop()
        host.shutdown()


def test_invalid_auth_attempts_are_rate_limited_without_blocking_parent(
    runtime_dir: Path,
) -> None:
    server = ControlServer(
        lambda _action, _params: {"state": "running"}, runtime_dir
    ).start()
    try:
        info = server.info()
        codes = []
        for index in range(MAX_AUTH_FAILURES + 2):
            response, _ = _request(
                info,
                {"token": "wrong-\N{SNOWMAN}" if index == 0 else f"wrong-{index}",
                 "action": "status", "params": {}},
            )
            codes.append(response["error"]["code"])
        assert codes[:MAX_AUTH_FAILURES] == ["unauthorized"] * MAX_AUTH_FAILURES
        assert codes[MAX_AUTH_FAILURES:] == ["rate_limited", "rate_limited"]

        response, _ = _request(
            info,
            {"token": info["token"], "action": "status", "params": {}},
        )
        assert response == {"ok": True, "result": {"state": "running"}}
    finally:
        server.stop()


@pytest.mark.skipif(os.name == "nt", reason="non-Windows fail-closed check")
def test_windows_transport_is_not_emulated_with_tcp(tmp_path: Path) -> None:
    server = ControlServer(lambda _action, _params: {}, tmp_path, platform="nt")
    with pytest.raises(OSError, match="require Windows"):
        server.start()
    with pytest.raises(RuntimeError, match="not running"):
        server.info()
    server.stop()


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows named pipes")
def test_windows_private_pipe_roundtrip_and_cleanup(tmp_path: Path) -> None:
    from services.ipc.windows_pipe import WindowsPipe
    server = ControlServer(lambda _action, _params: {"cancelled": True}, tmp_path).start()
    second = ControlServer(lambda _action, _params: {}, tmp_path).start()
    info = server.info()
    try:
        assert info["transport"] == "named_pipe"
        assert info["endpoint"].startswith("\\\\.\\pipe\\chudware-control-S-")
        assert info["endpoint"] != second.info()["endpoint"]
        denied, _ = _request(info, {"token": "wrong", "action": "cancel"})
        assert denied["error"]["code"] == "unauthorized"
        accepted, _ = _request(info, {"token": info["token"], "action": "cancel"})
        assert accepted == {"ok": True, "result": {"cancelled": True}}
    finally:
        server.stop()
        second.stop()
    with pytest.raises(OSError):
        WindowsPipe.connect(info["endpoint"], timeout=0.2)


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows security tokens")
def test_windows_pipe_denies_foreign_identity_and_name_squatting(monkeypatch):
    import ctypes
    from ctypes import wintypes
    from services.ipc import windows_pipe

    api = windows_pipe._api()
    api.kernel.GetCurrentThread.argtypes = []
    api.kernel.GetCurrentThread.restype = wintypes.HANDLE
    api.advapi.ImpersonateAnonymousToken.argtypes = [wintypes.HANDLE]
    api.advapi.ImpersonateAnonymousToken.restype = wintypes.BOOL
    api.advapi.RevertToSelf.argtypes = []
    api.advapi.RevertToSelf.restype = wintypes.BOOL
    monkeypatch.setattr(windows_pipe.secrets, "token_hex", lambda _count: "7" * 64)
    endpoint, pipes = windows_pipe.create_private_pipes(4)
    try:
        class Acl(ctypes.Structure):
            _fields_ = [("revision", wintypes.BYTE), ("reserved", wintypes.BYTE),
                        ("size", wintypes.WORD), ("count", wintypes.WORD),
                        ("reserved2", wintypes.WORD)]

        pointer = ctypes.POINTER(wintypes.LPVOID)
        api.advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                              pointer, pointer, pointer, pointer, pointer]
        api.advapi.GetSecurityInfo.restype = wintypes.DWORD
        api.advapi.GetSecurityDescriptorControl.argtypes = [wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        api.advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
        api.advapi.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, pointer]
        api.advapi.GetAce.restype = wintypes.BOOL
        owner, acl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
        assert api.advapi.GetSecurityInfo(pipes[0]._handle, 1, 5, ctypes.byref(owner), None,
            ctypes.byref(acl), None, ctypes.byref(descriptor)) == 0  # SE_FILE_OBJECT
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            assert api.advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control),
                                                           ctypes.byref(revision))
            assert control.value & 0x1000  # protected DACL
            user = api.token_user(api.kernel.GetCurrentProcess())
            assert api.advapi.EqualSid(owner, windows_pipe._TokenUser.from_buffer(user).sid)
            assert Acl.from_address(acl.value).count == 2
            identities = set()
            for index in range(2):
                ace = wintypes.LPVOID()
                assert api.advapi.GetAce(acl, index, ctypes.byref(ace))
                assert ctypes.c_ubyte.from_address(ace.value).value == 0  # allowed ACE
                sid = wintypes.LPVOID(ace.value + 8)
                if api.advapi.EqualSid(sid, owner):
                    identities.add("owner")
                else:
                    assert api.advapi.IsWellKnownSid(sid, 22)  # SYSTEM
                    identities.add("system")
            assert identities == {"owner", "system"}
        finally:
            api.kernel.LocalFree(descriptor)
        # Real OS admission refusal, before any capability token or handler I/O.
        assert api.advapi.ImpersonateAnonymousToken(api.kernel.GetCurrentThread())
        try:
            for _ in range(4):
                handle = api.kernel.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0x40000000, None)
                error = ctypes.get_last_error()
                assert handle == windows_pipe.INVALID_HANDLE and error == 5
        finally:
            assert api.advapi.RevertToSelf()
        with pytest.raises(OSError):
            windows_pipe.create_private_pipes(4)  # FIRST_PIPE_INSTANCE refuses collision
    finally:
        for pipe in pipes:
            pipe.close()


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows native client")
def test_windows_native_control_client_uses_private_pipe(tmp_path):
    compiler = os.environ.get("CXX") or shutil.which("cl") or shutil.which("c++")
    if not compiler:
        pytest.skip("native C++ compiler unavailable")
    command = shlex.split(compiler) if os.environ.get("CXX") else [compiler]
    root = Path(__file__).resolve().parents[2]
    binary = tmp_path / "control-client.exe"
    source = root / "services/ipc/test_windows_control_client.cpp"
    if Path(command[0]).name.lower() in {"cl", "cl.exe"}:
        command += ["/nologo", "/std:c++17", "/EHsc", str(source),
                    "/Fe:" + str(binary), "/link", "advapi32.lib"]
    else:
        command += ["-std=c++17", str(source), "-o", str(binary), "-ladvapi32"]
    compiled = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    server = ControlServer(lambda _action, _params: {"state": "idle"}, tmp_path).start()
    try:
        info = server.info()
        request = json.dumps({"token": info["token"], "action": "status", "params": {}}) + "\n"
        exercised = subprocess.run([str(binary), info["endpoint"]], input=request,
                                   capture_output=True, text=True, timeout=5)
        assert exercised.returncode == 0  # no request/token bytes in diagnostics
        assert exercised.stdout == "" and exercised.stderr == ""
    finally:
        server.stop()


def test_malformed_and_oversized_input_are_bounded(runtime_dir: Path) -> None:
    server = ControlServer(
        lambda _action, _params: {}, runtime_dir, request_limit=256
    ).start()
    try:
        info = server.info()
        malformed, malformed_wire = _request(info, {}, raw=b"not-json\n")
        assert malformed["error"]["code"] == "malformed"
        assert len(malformed_wire) < 256

        oversized, oversized_wire = _request(info, {}, raw=b"x" * 257 + b"\n")
        assert oversized["error"]["code"] == "request_too_large"
        assert len(oversized_wire) < 256
    finally:
        server.stop()


@pytest.mark.skipif(os.name == "nt", reason="POSIX stale Unix socket cleanup")
def test_stale_socket_is_replaced_and_non_socket_is_preserved(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = runtime_dir / f"chudcode-worker-{os.getppid()}-{os.getpid()}.sock"
    dead_pid = 987654321
    dead_sibling = runtime_dir / f"chudcode-worker-{os.getppid()}-{dead_pid}.sock"
    live_sibling = runtime_dir / f"chudcode-worker-{os.getppid()}-{os.getppid()}.sock"
    non_socket = runtime_dir / f"chudcode-worker-{os.getppid()}-{dead_pid + 1}.sock"
    non_socket.write_text("leave intact", encoding="utf-8")
    for sibling in (dead_sibling, live_sibling):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sibling))
        sock.close()
    real_kill = os.kill

    def kill(pid: int, sig: int) -> None:
        if pid == dead_pid:
            raise ProcessLookupError
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", kill)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(endpoint))
    stale.close()
    assert endpoint.exists()

    server = ControlServer(lambda _action, _params: {}, runtime_dir).start()
    try:
        assert Path(server.info()["endpoint"]).exists()
        assert Path(server.info()["endpoint"]) == endpoint
        assert not dead_sibling.exists()
        assert live_sibling.exists()
        assert non_socket.read_text(encoding="utf-8") == "leave intact"
    finally:
        server.stop()
    assert not endpoint.exists()

    endpoint.write_text("not a socket", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ControlServer(lambda _action, _params: {}, runtime_dir).start()
    assert endpoint.read_text(encoding="utf-8") == "not a socket"


def test_response_size_and_error_details_are_bounded_and_secret_free(
    runtime_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    holder: dict[str, str] = {}

    def handler(_action: str, params: dict) -> dict:
        if params.get("raise"):
            raise RuntimeError(holder["token"])
        return {"payload": "x" * 4096}

    server = ControlServer(handler, runtime_dir, response_limit=256).start()
    try:
        info = server.info()
        holder["token"] = info["token"]
        too_large, wire = _request(
            info, {"token": info["token"], "action": "status", "params": {}}
        )
        assert too_large["error"]["code"] == "response_too_large"
        assert len(wire) <= 256

        failed, failed_wire = _request(
            info,
            {"token": info["token"], "action": "status", "params": {"raise": True}},
        )
        assert failed["error"] == {
            "code": "internal",
            "message": "control request failed",
        }
        assert info["token"].encode() not in failed_wire
        assert info["token"] not in caplog.text
    finally:
        server.stop()


def test_control_cancel_and_steer_run_while_stdio_turn_is_blocked(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    cancelled = threading.Event()
    actions: list[tuple[str, dict]] = []
    output = io.StringIO()

    def blocked_turn(_params: dict, emit) -> dict:
        entered.set()
        assert cancelled.wait(2.0)
        emit({"kind": "status", "state": "cancelled"})
        return {"cancelled": True}

    def control(action: str, params: dict) -> dict:
        actions.append((action, params))
        if action == "cancel":
            cancelled.set()
        return {"accepted": True}

    monkeypatch.setitem(worker_main._METHODS, "agent_admin_turn", blocked_turn)
    monkeypatch.setattr(worker_main, "_invoke_chudcode_control", control)
    monkeypatch.setattr(worker_main, "_shutdown_chudcode", lambda: None)
    monkeypatch.setitem(worker_main.paths.__dict__, "RUNTIME", runtime_dir)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps({"id": 7, "method": "agent_admin_turn", "params": {}}) + "\n"
        ),
    )
    monkeypatch.setattr(sys, "stdout", output)

    serve_thread = threading.Thread(target=worker_main.serve)
    serve_thread.start()
    assert entered.wait(2.0)
    server = worker_main._CONTROL_SERVER
    assert server is not None
    info = server.info()

    steered, _ = _request(
        info,
        {
            "token": info["token"],
            "action": "steer",
            "params": {"text": "stop after save"},
        },
    )
    cancelled_response, _ = _request(
        info, {"token": info["token"], "action": "cancel", "params": {"run_id": "r7"}}
    )
    assert steered["ok"] and cancelled_response["ok"]
    serve_thread.join(timeout=3.0)
    assert not serve_thread.is_alive()
    assert actions == [
        ("steer", {"text": "stop after save"}),
        ("cancel", {"run_id": "r7"}),
    ]
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames == [
        {"id": 7, "event": "status", "data": {"kind": "status", "state": "cancelled"}},
        {"id": 7, "ok": True, "result": {"cancelled": True}},
    ]
    assert worker_main._CONTROL_SERVER is None
    assert not Path(info["endpoint"]).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX accept-thread shutdown barrier")
def test_stop_drains_admitted_reply_after_retiring_listener(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    reading = threading.Event()
    reply_ready = threading.Event()
    listener_retired = threading.Event()
    server = ControlServer(
        lambda _action, _params: {"accepted": True}, runtime_dir, client_timeout=30
    ).start()
    info = server.info()
    read_request = server._read_request
    send_response = server._send_response
    accept_thread = server._accept_thread
    assert accept_thread is not None
    join_accept = accept_thread.join

    def observe_read(conn, deadline):
        reading.set()
        return read_request(conn, deadline)

    def hold_reply(conn, response):
        if response.get("ok"):
            reply_ready.set()
            assert listener_retired.wait(2)
        return send_response(conn, response)

    def retire_listener(timeout=None):
        join_accept(timeout=timeout)
        # The old shutdown closed every client before joining this thread.
        # Holding the reply until here deterministically exposes that truncation.
        listener_retired.set()

    monkeypatch.setattr(server, "_read_request", observe_read)
    monkeypatch.setattr(server, "_send_response", hold_reply)
    monkeypatch.setattr(accept_thread, "join", retire_listener)
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        silent.connect(info["endpoint"])
        silent.sendall(b"{")
        assert reading.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                _request, info, {"token": info["token"], "action": "cancel"}
            )
            try:
                assert reply_ready.wait(2)
                started = time.monotonic()
                server.stop()
                assert time.monotonic() - started < 1.5
                response, wire = pending.result(timeout=2)
            finally:
                listener_retired.set()
        assert response == {"ok": True, "result": {"accepted": True}}
        assert len(wire) <= server._response_limit
        assert info["token"].encode() not in wire
        assert not server._active_clients
        assert not server._clients
        assert not server._client_threads
        assert not accept_thread.is_alive()
        assert not Path(info["endpoint"]).exists()
        with pytest.raises(RuntimeError):
            server.info()
    finally:
        listener_retired.set()
        silent.close()
        server.stop()


class _YieldingStdout:
    def __init__(self) -> None:
        self.value = ""

    def write(self, text: str) -> None:
        # Yield inside the frame, not once per character: Windows scheduling
        # under a native build must not turn this integrity test into a timer test.
        self.value += text[:1]
        time.sleep(0)
        self.value += text[1:]

    def flush(self) -> None:
        time.sleep(0)


def test_stdout_frames_remain_intact_across_emitters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _YieldingStdout()
    monkeypatch.setattr(sys, "stdout", output)
    barrier = threading.Barrier(9)

    def emit(index: int) -> None:
        barrier.wait()
        worker_main._emit({"id": index, "event": "mapped", "data": {"text": "x" * 200}})

    threads = [threading.Thread(target=emit, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    frames = [json.loads(line) for line in output.value.splitlines()]
    assert {frame["id"] for frame in frames} == set(range(8))
    assert len(frames) == 8
    assert all(frame["data"] == {"text": "x" * 200} for frame in frames)


@pytest.mark.parametrize(
    "stdin_text",
    ["", json.dumps({"id": 3, "method": "shutdown", "params": {}}) + "\n"],
    ids=["eof", "shutdown"],
)
def test_eof_and_shutdown_stop_control_before_session_host(
    stdin_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class FakeControl:
        def __init__(self, _handler, _runtime) -> None:
            pass

        def start(self):
            order.append("start")
            return self

        def stop(self) -> None:
            order.append("control_stop")

        def info(self) -> dict:
            return {
                "transport": "unix",
                "endpoint": "/private/control",
                "token": "a" * 64,
            }

    import services.agent.chudcode_control as control_module

    monkeypatch.setattr(control_module, "ControlServer", FakeControl)
    monkeypatch.setattr(
        worker_main, "_shutdown_chudcode", lambda: order.append("chudcode_shutdown")
    )
    monkeypatch.setattr(worker_main, "_warm_prompt_cache", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    worker_main.serve()
    assert order == ["start", "control_stop", "chudcode_shutdown"]
    assert worker_main._CONTROL_SERVER is None


def test_turn_worker_starts_control_without_touching_primary_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class FakeControl:
        def __init__(self, _handler, _runtime) -> None:
            pass

        def start(self):
            order.append("control_start")
            return self

        def stop(self) -> None:
            order.append("control_stop")

    import services.agent.chudcode_control as control_module

    monkeypatch.setenv("CHUDWARE_HOSTD_WORKER_LANE", "agent-turn")
    monkeypatch.setattr(control_module, "ControlServer", FakeControl)
    for name in (
        "_reconcile_bench", "_shutdown_bench", "_warm_chudcode_service",
        "_warm_prompt_cache", "_shutdown_chudcode",
    ):
        monkeypatch.setattr(worker_main, name, lambda name=name: order.append(name))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    worker_main.serve()
    assert order == [
        "control_start", "_warm_prompt_cache", "control_stop", "_shutdown_chudcode",
    ]
    assert worker_main._CONTROL_SERVER is None


def test_signal_stops_control_and_session_host(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    installed: dict[int, object] = {}

    class FakeControl:
        def __init__(self, _handler, _runtime) -> None:
            pass

        def start(self):
            order.append("start")
            return self

        def stop(self) -> None:
            order.append("control_stop")

    class SignalInput:
        def __iter__(self):
            handler = installed[worker_main.signal.SIGTERM]
            assert callable(handler)
            handler(worker_main.signal.SIGTERM, None)
            return iter(())

    import services.agent.chudcode_control as control_module

    monkeypatch.setattr(control_module, "ControlServer", FakeControl)
    monkeypatch.setattr(
        worker_main, "_shutdown_chudcode", lambda: order.append("chudcode_shutdown")
    )
    monkeypatch.setattr(worker_main, "_warm_prompt_cache", lambda: None)
    monkeypatch.setattr(worker_main.signal, "getsignal", lambda _signum: "old-handler")
    monkeypatch.setattr(
        worker_main.signal,
        "signal",
        lambda signum, handler: installed.__setitem__(signum, handler),
    )
    monkeypatch.setattr(sys, "stdin", SignalInput())
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    with pytest.raises(SystemExit) as stopped:
        worker_main.serve()
    assert stopped.value.code == 128 + worker_main.signal.SIGTERM
    assert order == ["start", "control_stop", "chudcode_shutdown"]
    assert worker_main._CONTROL_SERVER is None


def test_worker_control_info_is_registered_and_served_over_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "transport": "unix",
        "endpoint": "/private/control.sock",
        "token": "b" * 64,
    }

    class FakeControl:
        def __init__(self, _handler, _runtime) -> None:
            pass

        def start(self):
            return self

        def stop(self) -> None:
            pass

        def info(self) -> dict:
            return dict(expected)

    import services.agent.chudcode_control as control_module

    output = io.StringIO()
    requests = (
        json.dumps({"id": 1, "method": "worker_control_info", "params": {}})
        + "\n"
        + json.dumps({"id": 2, "method": "shutdown", "params": {}})
        + "\n"
    )
    monkeypatch.setattr(control_module, "ControlServer", FakeControl)
    monkeypatch.setattr(worker_main, "_shutdown_chudcode", lambda: None)
    monkeypatch.setattr(worker_main, "_warm_prompt_cache", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(requests))
    monkeypatch.setattr(sys, "stdout", output)

    worker_main.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0] == {"id": 1, "ok": True, "result": expected}
    assert frames[1] == {"id": 2, "ok": True, "result": {"bye": True}}


@pytest.mark.parametrize("max_connections", [1, 4])
def test_slow_drip_has_absolute_request_deadline(runtime_dir, max_connections):
    calls = []
    server = ControlServer(lambda action, params: calls.append(action) or {},
                           runtime_dir, client_timeout=0.3,
                           max_connections=max_connections).start()
    connections = []
    try:
        info = server.info()
        for _ in range(max_connections):
            if info["transport"] == "named_pipe":
                from services.ipc.windows_pipe import WindowsPipe
                conn = WindowsPipe.connect(info["endpoint"])
            else:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(info["endpoint"])
            connections.append(conn)
            conn.settimeout(1)
            conn.sendall(b"{")
        time.sleep(0.08)
        for conn in connections:
            conn.sendall(b'"')
        time.sleep(0.08)
        for conn in connections:
            conn.sendall(b"t")
        for conn in connections:
            response = json.loads(conn.recv(4096))
            assert response["error"]["code"] == "timeout"
        assert calls == []
        # EOF proves that the one-shot worker recycled the connection, then a
        # legitimate request proves capacity was actually recovered.
        for conn in connections:
            if info["transport"] == "unix":
                assert conn.recv(1) == b""
            conn.close()
        accepted, _ = _request(info, {
            "token": info["token"], "action": "status", "params": {}})
        assert accepted == {"ok": True, "result": {}}
        assert calls == ["status"]
    finally:
        for conn in connections:
            conn.close()
        server.stop()


def test_stop_interrupts_silent_clients_and_retires_endpoint(runtime_dir):
    server = ControlServer(lambda _action, _params: {}, runtime_dir,
                           client_timeout=30).start()
    info = server.info()
    if info["transport"] == "named_pipe":
        from services.ipc.windows_pipe import WindowsPipe
        client = WindowsPipe.connect(info["endpoint"])
    else:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(info["endpoint"])
    try:
        client.sendall(b"{")
        time.sleep(0.05)
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 1.5
        assert not server._client_threads
        with pytest.raises(RuntimeError):
            server.info()
    finally:
        client.close()
        server.stop()


def test_nonreading_peer_cannot_hold_response_writer(runtime_dir):
    server = ControlServer(lambda _action, _params: {"payload": "x" * 200000},
                           runtime_dir, client_timeout=0.1)
    peer, client = socket.socketpair()
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    assert server._slots.acquire(blocking=False)
    thread = threading.Thread(target=server._serve_client, args=(peer,))
    with server._state_lock:
        server._clients.add(peer)
        server._client_threads.add(thread)
    try:
        thread.start()
        client.sendall(json.dumps({"token": server._token, "action": "status"}).encode() + b"\n")
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert not server._client_threads
    finally:
        client.close()
        server.stop()


def test_stop_bounds_admitted_nonreading_writer(runtime_dir: Path) -> None:
    writing = threading.Event()
    server = ControlServer(
        lambda _action, _params: {"payload": "x" * 200000},
        runtime_dir, client_timeout=30,
    )
    peer, client = socket.socketpair()
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)

    class ObservedWriter:
        def __getattr__(self, name):
            return getattr(peer, name)

        def sendall(self, payload):
            writing.set()
            peer.sendall(payload)

    conn = ObservedWriter()
    assert server._slots.acquire(blocking=False)
    thread = threading.Thread(target=server._serve_client, args=(conn,))
    with server._state_lock:
        server._clients.add(conn)
        server._client_threads.add(thread)
    try:
        thread.start()
        client.sendall(
            json.dumps({"token": server._token, "action": "status"}).encode() + b"\n"
        )
        assert writing.wait(2)
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 1.5
        assert not thread.is_alive()
        assert not server._active_clients
        assert not server._clients
        assert not server._client_threads
        assert peer.fileno() == -1
    finally:
        client.close()
        server.stop()
        thread.join(timeout=2)


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows pipe backpressure")
def test_windows_nonreading_client_releases_pipe_worker(tmp_path):
    from services.ipc.windows_pipe import WindowsPipe
    server = ControlServer(
        lambda _action, params: {"payload": "x" * 200000} if params.get("large") else {"state": "idle"},
        tmp_path, client_timeout=0.15, max_connections=1).start()
    info = server.info()
    peer = WindowsPipe.connect(info["endpoint"])
    try:
        peer.sendall(json.dumps({"token": info["token"], "action": "status",
                                 "params": {"large": True}}).encode() + b"\n")
        # Deliberately do not read. The finite write budget releases the sole
        # pipe instance without waiting for a failed writer's disconnect grace.
        response, _ = _request(info, {"token": info["token"], "action": "status"})
        assert response == {"ok": True, "result": {"state": "idle"}}
    finally:
        peer.close()
        server.stop()


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows pipe cancellation")
def test_windows_abandoned_clients_recycle_without_request_timeout(tmp_path):
    from services.ipc.windows_pipe import WindowsPipe

    server = ControlServer(lambda _action, _params: {"state": "idle"}, tmp_path,
                           client_timeout=5, max_connections=1).start()
    info = server.info()
    try:
        for partial in (b"", b"{", b"", b'{"token":'):
            abandoned = WindowsPipe.connect(info["endpoint"], timeout=0.5)
            if partial:
                abandoned.sendall(partial)
            abandoned.close()
            started = time.monotonic()
            response, _ = _request(info, {"token": info["token"], "action": "status"})
            assert response == {"ok": True, "result": {"state": "idle"}}
            assert time.monotonic() - started < 1.0
    finally:
        server.stop()


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows pipe concurrency")
def test_windows_silent_and_drip_clients_leave_control_capacity(tmp_path):
    from services.ipc.windows_pipe import WindowsPipe

    server = ControlServer(lambda action, _params: {"action": action}, tmp_path,
                           client_timeout=2, max_connections=4).start()
    info = server.info()
    peers = []
    try:
        for partial in (b"", b"{", b'{"token":'):
            peer = WindowsPipe.connect(info["endpoint"])
            peers.append(peer)
            if partial:
                peer.sendall(partial)
        started = time.monotonic()
        for action in ("cancel", "steer", "answer", "status"):
            response, _ = _request(info, {"token": info["token"], "action": action})
            assert response == {"ok": True, "result": {"action": action}}
        assert time.monotonic() - started < 1.0
    finally:
        for peer in peers:
            peer.close()
        server.stop()


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows pipe concurrency")
def test_windows_legitimate_handlers_run_concurrently_with_cancel(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    entered = threading.Event()
    lock = threading.Lock()
    waiting = 0

    def handler(action, _params):
        nonlocal waiting
        if action == "status":
            with lock:
                waiting += 1
                if waiting == 3:
                    entered.set()
            assert release.wait(3)
        elif action == "cancel":
            release.set()
        return {"action": action}

    server = ControlServer(handler, tmp_path, max_connections=4).start()
    info = server.info()
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            requests = [pool.submit(_request, info, {"token": info["token"], "action": "status"})
                        for _ in range(3)]
            try:
                assert entered.wait(1.5)
                cancelled, _ = _request(info, {"token": info["token"], "action": "cancel"})
                assert cancelled == {"ok": True, "result": {"action": "cancel"}}
                for request in requests:
                    assert request.result(timeout=2)[0] == {
                        "ok": True, "result": {"action": "status"}}
            finally:
                release.set()
    finally:
        server.stop()


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows pending write")
def test_windows_stop_cancels_nonreading_writer_and_listeners(tmp_path):
    from services.ipc.windows_pipe import WindowsPipe

    entered = threading.Event()

    def handler(_action, _params):
        entered.set()
        return {"payload": "x" * 200000}

    server = ControlServer(handler, tmp_path, client_timeout=30).start()
    info = server.info()
    peer = WindowsPipe.connect(info["endpoint"])
    try:
        peer.sendall(json.dumps({"token": info["token"], "action": "status"}).encode() + b"\n")
        assert entered.wait(1)
        time.sleep(0.05)  # let WriteFile reach real pipe backpressure
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 1.5
        assert not server._client_threads
        with pytest.raises(OSError):
            WindowsPipe.connect(info["endpoint"], timeout=0.2)
    finally:
        peer.close()
        server.stop()


@pytest.mark.skipif(os.name != "nt", reason="requires genuine Windows overlapped reads")
def test_windows_read_timeout_drains_cancellation_before_pipe_reuse():
    from concurrent.futures import ThreadPoolExecutor

    from services.ipc.windows_pipe import WindowsPipe, create_private_pipes

    endpoint, pipes = create_private_pipes(1)
    pipe = pipes[0]
    peer = WindowsPipe.connect(endpoint)
    try:
        pipe.accept(timeout=1)
        pipe.settimeout(0.05)
        for _ in range(3):
            with pytest.raises(TimeoutError):
                pipe.recv(4096)
        peer.sendall(b"after-cancellation")
        assert pipe.recv(4096) == b"after-cancellation"
        peer.close()
        assert pipe.recv(4096) == b""
        pipe.disconnect()
        # A reused instance is not available until ConnectNamedPipe re-arms it.
        # Run server acceptance concurrently, as ControlServer's pipe worker does.
        with ThreadPoolExecutor(max_workers=1) as pool:
            accepted = pool.submit(pipe.accept, timeout=1)
            peer = WindowsPipe.connect(endpoint, timeout=1)
            accepted.result(timeout=2)
        peer.sendall(b"reconnected")
        assert pipe.recv(4096) == b"reconnected"
    finally:
        peer.close()
        pipe.close()
