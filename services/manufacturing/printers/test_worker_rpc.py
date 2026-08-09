from __future__ import annotations

import base64
import json

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
            },
            "auth": {"mode": "api_key", "api_key": api_key} if api_key else {"mode": "trusted_host"},
        },
    )


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
