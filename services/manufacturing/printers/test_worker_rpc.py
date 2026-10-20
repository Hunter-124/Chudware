from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from urllib.parse import urlsplit

import pytest

from fixtures.mfg.printers import VirtualPrinter
from services.manufacturing.printers.worker_rpc import WORKER_METHODS

_QUERY_PATH = "/printer/objects/query"


def call(method: str, params: dict) -> dict:
    return WORKER_METHODS[method](params, lambda event: None)


def open_adapter(
    server: VirtualPrinter,
    adapter_id: str,
    *,
    timeout_s: float = 0.5,
    api_key: str | None = None,
    expected_base_url: str | None = None,
) -> dict:
    return call(
        "mfg_printer_adapter_open",
        {
            "adapter_id": adapter_id,
            "kind": "moonraker",
            "endpoint": {
                "base_url": server.base_url,
                "expected_base_url": expected_base_url or server.base_url,
                "endpoint_is_physical": False,
                "origin": "virtual",
                "timeout_s": timeout_s,
                "address_class": "loopback",
            },
            "auth": {"mode": "api_key", "api_key": api_key} if api_key else {"mode": "trusted_host"},
        },
    )


def test_worker_open_requires_and_carries_commissioned_dns_class():
    params = {
        "adapter_id": "commissioned-dns-class",
        "kind": "moonraker",
        "endpoint": {
            "base_url": "http://printer.example",
            "expected_base_url": "http://printer.example",
            "endpoint_is_physical": True,
        },
        "auth": {"mode": "trusted_host"},
    }
    refused = call("mfg_printer_adapter_open", params)
    assert refused["ok"] is False
    assert "requires address_class" in refused["message"]
    params["endpoint"]["address_class"] = "local"
    assert call("mfg_printer_adapter_open", params)["ok"] is True
    try:
        from services.manufacturing.printers import worker_rpc
        adapter = worker_rpc._ADAPTERS[params["adapter_id"]]
        assert adapter.config.address_class == "local"
        assert adapter.http.address_class == "local"
    finally:
        call("mfg_printer_adapter_close", {"adapter_id": params["adapter_id"]})


@pytest.fixture
def native_printer_lane_binary() -> str:
    configured = os.environ.get("CHUDWARE_MFG_LANE_TEST_BINARY")
    if not configured:
        pytest.skip("set CHUDWARE_MFG_LANE_TEST_BINARY to the freshly built test_mfg_lane")
    binary = Path(configured).resolve()
    assert binary.is_file(), f"manufacturing integration binary is missing: {binary}"
    return str(binary)


def consume_native_printer_lane(binary: str, endpoint: dict, *, on_lane=None, on_worker=None):
    """Run the real native lane against real worker handlers, with no canned replies."""
    worker_calls, lane_results, failures = [], [], []
    process = subprocess.Popen(
        [binary, "--printer-worker-fixture"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None

    def consume() -> None:
        try:
            process.stdin.write(json.dumps(endpoint) + "\n")
            process.stdin.flush()
            for line in process.stdout:
                request = json.loads(line)
                if "worker_method" in request:
                    method, params = request["worker_method"], request["params"]
                    result = call(method, params)
                    worker_calls.append({"method": method, "params": params, "result": result})
                    if on_worker:
                        on_worker(method, params, result)
                    process.stdin.write(json.dumps(result) + "\n")
                    process.stdin.flush()
                else:
                    assert "lane_method" in request, request
                    lane_results.append(request)
                    if on_lane:
                        on_lane(request)
        except BaseException as error:
            failures.append(error)
            if process.poll() is None:
                process.kill()

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    try:
        thread.join(15)
        assert not thread.is_alive(), "disposable native printer fixture exceeded its operation deadline"
        returncode = process.wait(timeout=3)
        if failures:
            raise failures[0]
        assert returncode == 0, process.stderr.read()
        return worker_calls, lane_results
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        thread.join(3)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        for request in worker_calls:
            if request["method"] == "mfg_printer_adapter_open":
                call("mfg_printer_adapter_close", {"adapter_id": request["params"]["adapter_id"]})


@pytest.mark.parametrize(
    ("host", "address_class", "rebind"),
    [("127.0.0.1", None, False), ("printer.fixture.invalid", "loopback", True)],
)
def test_native_lane_emitted_printer_params_reach_real_adapter(
    native_printer_lane_binary, monkeypatch, host, address_class, rebind,
):
    from services.manufacturing.printers import worker_rpc

    with VirtualPrinter("moonraker") as server:
        port = urlsplit(server.base_url).port
        endpoint = {"host": host, "port": port}
        if address_class is not None:
            endpoint["address_class"] = address_class
        state = {"rebound": False}
        connections, resolutions, commissioned_classes = [], [], []
        real_resolve, real_socket = socket.getaddrinfo, socket.socket

        def resolve(name, requested_port, *args, **kwargs):
            assert name in {"127.0.0.1", "printer.fixture.invalid"}
            assert requested_port == port
            address = "203.0.113.10" if state["rebound"] else "127.0.0.1"
            resolutions.append(address)
            return real_resolve(address, requested_port, *args, **kwargs)

        class LoopbackOnlySocket(real_socket):
            def connect(self, address):
                # Even a broken admission guard may not contact a live destination.
                assert address[:2] == ("127.0.0.1", port), address
                connections.append(address)
                return super().connect(address)

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(socket, "socket", LoopbackOnlySocket)

        def observe_worker(method, params, result):
            if method == "mfg_printer_adapter_open" and result["ok"]:
                adapter = worker_rpc._ADAPTERS[params["adapter_id"]]
                commissioned_classes.append((adapter.config.address_class, adapter.http.address_class))

        def observe_lane(result):
            if rebind and result["lane_method"] == "mfg_telemetry_snapshot" and not state["rebound"]:
                state["requests_before_rebind"] = len(server.requests)
                state["connections_before_rebind"] = len(connections)
                state["rebound"] = True

        workers, lane_results = consume_native_printer_lane(
            native_printer_lane_binary, endpoint, on_lane=observe_lane, on_worker=observe_worker,
        )
        emitted = next(row for row in workers if row["method"] == "mfg_printer_adapter_open")
        assert emitted["result"]["ok"] is True
        assert emitted["params"]["endpoint"]["base_url"] == f"http://{host}:{port}"
        if address_class is None:
            assert "address_class" not in emitted["params"]["endpoint"]
        else:
            assert emitted["params"]["endpoint"]["address_class"] == address_class
        assert commissioned_classes == [(address_class, "loopback")]
        assert all(not row["is_error"] for row in lane_results), lane_results
        connected = next(row for row in lane_results if row["lane_method"] == "mfg_device_connect")
        assert connected["body"]["connected"] is True
        assert connected["body"]["capability"]["max_advertised"] == "transport"
        snapshots = [
            row["body"]["snapshot"]["v"]["rows"]
            for row in lane_results if row["lane_method"] == "mfg_telemetry_snapshot"
        ]
        assert len(snapshots) == 2
        assert all(row["state"] == "ok" and row["v"] is not None for row in snapshots[0])
        assert {"path": "/printer/info", "method": "GET"} in server.requests
        assert {"path": _QUERY_PATH, "method": "GET"} in server.requests
        if rebind:
            assert all(row["state"] == "unknown" and "address_refused" in row["why"]
                       and row["last_ok"] is not None for row in snapshots[1])
            assert resolutions[-1] == "203.0.113.10"
            assert len(server.requests) == state["requests_before_rebind"]
            assert len(connections) == state["connections_before_rebind"]
        else:
            assert all(row["state"] == "ok" for row in snapshots[1])


def test_native_lane_dns_without_commissioned_class_refuses_before_connect(
    native_printer_lane_binary, monkeypatch,
):
    with VirtualPrinter("moonraker") as server:
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda *args, **kwargs: pytest.fail("uncommissioned DNS reached resolution"))
        workers, lane_results = consume_native_printer_lane(
            native_printer_lane_binary,
            {"host": "printer.fixture.invalid", "port": urlsplit(server.base_url).port},
        )
        emitted = next(row for row in workers if row["method"] == "mfg_printer_adapter_open")
        assert "address_class" not in emitted["params"]["endpoint"]
        assert emitted["result"]["ok"] is False
        assert "requires address_class" in emitted["result"]["message"]
        refused = next(row for row in lane_results if row["lane_method"] == "mfg_device_connect")
        assert refused["is_error"] is True
        assert refused["body"]["code"] == "mfg_printer_worker_refused"
        assert all(row["method"] != "mfg_printer_adapter_probe" for row in workers)
        assert server.requests == []


def assert_unknown_snapshot(result: dict, expected_fields: set[str], reason_code: str) -> None:
    assert result["ok"] is True
    rows = result["rows"]
    assert {row["field"] for row in rows} == expected_fields
    for row in rows:
        assert row["state"] == "unknown"
        assert row["v"] is None
        assert reason_code in row["why"]
        assert row["last_ok"] is not None
    assert sum("raw" in row for row in rows) == 1


def test_worker_bridge_registration_drives_adapter_without_exposing_secrets() -> None:
    secret = "worker-secret-c51a"
    adapter_id = "test-worker-moonraker"
    with VirtualPrinter("moonraker", api_key=secret) as server:
        opened = open_adapter(server, adapter_id, api_key=secret)
        assert opened == {"ok": True, "adapter_id": adapter_id, "kind": "moonraker"}
        try:
            report = call("mfg_printer_adapter_probe", {"adapter_id": adapter_id})
            assert report["ok"] is True
            uploaded = call(
                "mfg_printer_adapter_upload",
                {
                    "adapter_id": adapter_id,
                    "artifact_b64": base64.b64encode(b"G1 X3\n").decode("ascii"),
                },
            )
            remote = uploaded["remote_artifact"]
            refused = call(
                "mfg_printer_adapter_start",
                {"adapter_id": adapter_id, "remote_artifact": remote},
            )
            assert refused["ok"] is False
            assert refused["error"] == "AuthorizationRequired"
            started = call(
                "mfg_printer_adapter_start",
                {
                    "adapter_id": adapter_id,
                    "remote_artifact": remote,
                    "authorization_token": "opaque-token",
                },
            )
            assert started["ok"] is True
            snapshot = call("mfg_printer_adapter_snapshot", {"adapter_id": adapter_id})
            assert snapshot["ok"] is True
            assert secret not in json.dumps({"opened": opened, "report": report, "snapshot": snapshot})
        finally:
            assert call("mfg_printer_adapter_close", {"adapter_id": adapter_id})["closed"] is True


def test_worker_methods_stay_behind_mfg_lane_and_open_pins_expected_endpoint() -> None:
    assert WORKER_METHODS
    assert all(method.startswith("mfg_") for method in WORKER_METHODS)
    with VirtualPrinter("moonraker") as server:
        refused = open_adapter(
            server,
            "endpoint-mismatch",
            expected_base_url="http://example.invalid:80",
        )
        assert refused["ok"] is False
        assert "expected endpoint" in refused["message"]
        opened = open_adapter(server, "unprobed")
        assert opened["ok"] is True
        try:
            snapshot = call("mfg_printer_adapter_snapshot", {"adapter_id": "unprobed"})
            assert snapshot["ok"] is False
            assert "probe it first" in snapshot["message"]
        finally:
            call("mfg_printer_adapter_close", {"adapter_id": "unprobed"})


@pytest.mark.parametrize(
    ("control", "reason_code"),
    [
        ("html_error_paths", "http_500"),
        ("malformed_paths", "malformed_body"),
        ("timeout_paths", "timeout"),
        ("oversized_paths", "response_too_large"),
    ],
)
def test_worker_snapshot_outages_are_complete_unknown_rows_and_recover(control: str, reason_code: str) -> None:
    adapter_id = f"outage-{reason_code}"
    with VirtualPrinter("moonraker") as server:
        opened = open_adapter(server, adapter_id, timeout_s=0.05)
        assert opened["ok"] is True
        try:
            assert call("mfg_printer_adapter_probe", {"adapter_id": adapter_id})["ok"] is True
            baseline = call("mfg_printer_adapter_snapshot", {"adapter_id": adapter_id})
            expected_fields = {row["field"] for row in baseline["rows"]}
            assert all(row["state"] == "ok" for row in baseline["rows"])

            failure_paths = getattr(server, control)
            failure_paths.add(_QUERY_PATH)
            failed = call("mfg_printer_adapter_snapshot", {"adapter_id": adapter_id})
            assert_unknown_snapshot(failed, expected_fields, reason_code)

            failure_paths.clear()
            recovered = call("mfg_printer_adapter_snapshot", {"adapter_id": adapter_id})
            assert recovered["ok"] is True
            assert {row["field"] for row in recovered["rows"]} == expected_fields
            assert all(row["state"] == "ok" and row["v"] is not None for row in recovered["rows"])
        finally:
            call("mfg_printer_adapter_close", {"adapter_id": adapter_id})


def test_worker_snapshot_connection_refused_is_unknown_not_rpc_failure() -> None:
    adapter_id = "outage-connect-refused"
    server = VirtualPrinter("moonraker")
    server.__enter__()
    assert open_adapter(server, adapter_id)["ok"] is True
    assert call("mfg_printer_adapter_probe", {"adapter_id": adapter_id})["ok"] is True
    baseline = call("mfg_printer_adapter_snapshot", {"adapter_id": adapter_id})
    expected_fields = {row["field"] for row in baseline["rows"]}
    server.__exit__(None, None, None)
    try:
        failed = call("mfg_printer_adapter_snapshot", {"adapter_id": adapter_id})
        assert_unknown_snapshot(failed, expected_fields, "connect_refused")
    finally:
        call("mfg_printer_adapter_close", {"adapter_id": adapter_id})


def test_snapshot_raw_is_attached_once_and_capped_before_journaling() -> None:
    adapter_id = "raw-cap"
    with VirtualPrinter("moonraker") as server:
        server.raw_blob_bytes = 100_000
        assert open_adapter(server, adapter_id)["ok"] is True
        try:
            assert call("mfg_printer_adapter_probe", {"adapter_id": adapter_id})["ok"] is True
            snapshot = call("mfg_printer_adapter_snapshot", {"adapter_id": adapter_id})
            rows_with_raw = [row for row in snapshot["rows"] if "raw" in row]
            assert len(rows_with_raw) == 1
            raw = rows_with_raw[0]["raw"]
            assert raw["truncated"] is True
            assert raw["original_bytes"] > 64 * 1024
            assert len(json.dumps(raw).encode("utf-8")) < 64 * 1024
        finally:
            call("mfg_printer_adapter_close", {"adapter_id": adapter_id})
