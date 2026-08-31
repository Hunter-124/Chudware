from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from services import worker_main
from services.agent.chudcode_control import MAX_AUTH_FAILURES, ControlServer


@pytest.fixture
def runtime_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="chudcode-control-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _request(info: dict, body: dict, *, raw: bytes | None = None) -> tuple[dict, bytes]:
    if info["transport"] == "unix":
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address: str | tuple[str, int] = info["endpoint"]
    else:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host, port = info["endpoint"].rsplit(":", 1)
        address = (host, int(port))
    client.settimeout(2.0)
    try:
        client.connect(address)
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
                {"token": f"wrong-{index}", "action": "status", "params": {}},
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


def test_mocked_windows_endpoint_uses_random_loopback_tcp(tmp_path: Path) -> None:
    server = ControlServer(
        lambda _action, _params: {"cancelled": True}, tmp_path, platform="nt"
    ).start()
    try:
        info = server.info()
        assert info["transport"] == "tcp"
        host, port = info["endpoint"].rsplit(":", 1)
        assert host == "127.0.0.1"
        assert 0 < int(port) < 65536
        response, _ = _request(
            info, {"token": info["token"], "action": "cancel", "params": {}}
        )
        assert response["ok"] is True
        assert not list(tmp_path.glob("*.sock"))
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


def test_stale_socket_is_replaced_and_non_socket_is_preserved(
    runtime_dir: Path,
) -> None:
    endpoint = runtime_dir / f"chudcode-worker-{os.getppid()}.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(endpoint))
    stale.close()
    assert endpoint.exists()

    server = ControlServer(lambda _action, _params: {}, runtime_dir).start()
    try:
        assert Path(server.info()["endpoint"]).exists()
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
    monkeypatch.setattr(worker_main.paths, "RUNTIME", runtime_dir)
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


class _YieldingStdout:
    def __init__(self) -> None:
        self.value = ""

    def write(self, text: str) -> None:
        for char in text:
            self.value += char
            time.sleep(0)

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
        thread.join(timeout=2.0)
    frames = [json.loads(line) for line in output.value.splitlines()]
    assert {frame["id"] for frame in frames} == set(range(8))
    assert len(frames) == 8


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
    assert (
        worker_main._METHODS["worker_control_info"] is worker_main._worker_control_info
    )
