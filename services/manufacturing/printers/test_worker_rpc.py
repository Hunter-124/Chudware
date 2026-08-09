from __future__ import annotations

import base64
import json

from fixtures.mfg.printers import VirtualPrinter
from services.manufacturing.printers.worker_rpc import WORKER_METHODS


def call(method: str, params: dict) -> dict:
    return WORKER_METHODS[method](params, lambda event: None)


def test_worker_bridge_registration_drives_adapter_without_exposing_secrets() -> None:
    secret = "worker-secret-c51a"
    adapter_id = "test-worker-moonraker"
    with VirtualPrinter("moonraker", api_key=secret) as server:
        opened = call(
            "mfg_printer_adapter_open",
            {
                "adapter_id": adapter_id,
                "kind": "moonraker",
                "endpoint": {
                    "base_url": server.base_url,
                    "endpoint_is_physical": False,
                    "origin": "virtual",
                    "timeout_s": 0.5,
                },
                "auth": {"mode": "api_key", "api_key": secret},
            },
        )
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
