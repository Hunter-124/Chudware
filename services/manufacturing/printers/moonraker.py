"""Moonraker REST and JSON-RPC websocket printer adapter."""

from __future__ import annotations

from collections.abc import Generator, Mapping
import hashlib
import json
import threading
from typing import Any
from urllib.parse import quote

from .base import PrinterAdapter, _MISSING, nested, number_or_missing, string_or_missing
from .model import Artifact, EndpointConfig, ProtocolError, capability_report, operation_truth
from .websocket import JsonWebSocket

_OBJECTS = "print_stats&virtual_sdcard&extruder&heater_bed&toolhead&fan"
_DEVICE_STATE = {
    "ready": "idle",
    "startup": "connecting",
    "initializing": "identifying",
    "shutdown": "fault",
    "error": "fault",
    "disconnected": "disconnected",
}
_JOB_DEVICE_STATE = {
    "standby": "idle",
    "printing": "running",
    "paused": "hold",
    "complete": "idle",
    "cancelled": "idle",
    "error": "fault",
}
_JOB_STATE = {
    "standby": "draft",
    "printing": "running",
    "paused": "held",
    "complete": "completed",
    "cancelled": "aborted",
    "error": "faulted",
}


class MoonrakerAdapter(PrinterAdapter):
    adapter_kind = "moonraker"

    def __init__(self, config: EndpointConfig) -> None:
        super().__init__(config, api_key_header="X-Api-Key")
        self._info: dict[str, Any] | None = None

    def probe(self) -> dict[str, Any]:
        info_envelope = self.http.request_json("GET", "/printer/info")
        if not isinstance(info_envelope, Mapping) or not isinstance(info_envelope.get("result"), Mapping):
            raise ProtocolError("Moonraker /printer/info response lacks result object")
        query_envelope = self.http.request_json("GET", f"/printer/objects/query?{_OBJECTS}")
        status = nested(query_envelope, "result", "status")
        if not isinstance(status, Mapping):
            raise ProtocolError("Moonraker object query response lacks result.status")
        self._info = dict(info_envelope["result"])
        self._operations = {
            "snapshot": operation_truth("verified"),
            "event_stream": operation_truth("verified"),
            "upload": operation_truth("verified"),
            "start": operation_truth("execute_assumed"),
            "pause": operation_truth("execute_assumed"),
            "resume": operation_truth("execute_assumed"),
            "cancel": operation_truth("execute_assumed"),
        }
        identity = json.dumps(self.redactor.value(self._info), sort_keys=True, separators=(",", ":"))
        subject = "printer:moonraker:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return capability_report(
            subject_id=subject,
            adapter_kind=self.adapter_kind,
            monitor_verified=True,
            transport_verified=True,
            operations=self._operations,
            raw={"printer_info": self._info, "configured_objects": sorted(status)},
            redactor=self.redactor,
        )

    def snapshot(self) -> list[dict[str, Any]]:
        try:
            if self._info is None:
                info_envelope = self.http.request_json("GET", "/printer/info")
                info = nested(info_envelope, "result")
                if not isinstance(info, Mapping):
                    raise ProtocolError(
                        "Moonraker /printer/info response lacks result object",
                        reason_code="malformed_body",
                    )
                self._info = dict(info)
            envelope = self.http.request_json("GET", f"/printer/objects/query?{_OBJECTS}")
            status = nested(envelope, "result", "status")
            if not isinstance(status, Mapping):
                raise ProtocolError(
                    "Moonraker object query response lacks result.status",
                    reason_code="malformed_body",
                )
            return self._rows(status, self._info)
        except ProtocolError as error:
            return self._unknown_snapshot(error)

    def upload(self, artifact: Artifact) -> dict[str, Any]:
        self._require_operation("upload")
        response = self.http.multipart(
            "/server/files/upload",
            fields={"root": "gcodes", "path": "", "checksum": artifact.sha256 or ""},
            file_field="file",
            filename=artifact.remote_name,
            data=artifact.data,
            expected=(200, 201),
        )
        payload = self.http.decode_json(response, method="POST")
        result = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(result, Mapping):
            raise ProtocolError("Moonraker upload response lacks result object")
        remote_path = result.get("path") or result.get("item", {}).get("path") if isinstance(result.get("item"), Mapping) else result.get("path")
        if not isinstance(remote_path, str) or not remote_path:
            remote_path = artifact.remote_name
        return {
            "adapter_kind": self.adapter_kind,
            "remote_path": remote_path,
            "sha256": artifact.sha256,
            "size": len(artifact.data),
            "raw": self.redactor.value(payload),
        }

    def start(self, remote_artifact: Mapping[str, Any], authorization_token: str | None = None) -> dict[str, Any]:
        self._require_authorization(authorization_token)
        self._require_operation("start")
        remote_path = remote_artifact.get("remote_path")
        if not isinstance(remote_path, str) or not remote_path:
            raise ProtocolError("Moonraker start requires a staged remote_path")
        raw = self.http.request_json("POST", "/printer/print/start?filename=" + quote(remote_path, safe="/"))
        return self._command_result("start", raw)

    def pause(self) -> dict[str, Any]:
        self._require_operation("pause")
        return self._command_result("pause", self.http.request_json("POST", "/printer/print/pause"))

    def resume(self) -> dict[str, Any]:
        self._require_operation("resume")
        return self._command_result("resume", self.http.request_json("POST", "/printer/print/resume"))

    def cancel(self) -> dict[str, Any]:
        self._require_operation("cancel")
        return self._command_result("cancel", self.http.request_json("POST", "/printer/print/cancel"))

    def events(
        self,
        *,
        stop_event: threading.Event | None = None,
        poll_interval_s: float = 1.0,
    ) -> Generator[dict[str, Any], None, None]:
        stopper = stop_event or threading.Event()
        request = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {"objects": {name: None for name in _OBJECTS.split("&")}},
            "id": 1,
        }
        try:
            with JsonWebSocket(
                self.config.base_url,
                "/websocket",
                self.http.auth_headers(),
                self.config.timeout_s,
                self.redactor,
                address_class=self.config.address_class,
            ) as websocket:
                websocket.send_json(request)
                while not stopper.is_set():
                    message = websocket.recv_json()
                    status = self._subscription_status(message)
                    if status is None:
                        continue
                    yield {
                        "event": "snapshot",
                        "transport": "websocket",
                        "rows": self._rows(status, self._info or {}),
                    }
                return
        except (ProtocolError, TimeoutError, OSError):
            # Subscription is an optimization. REST reconciliation remains authoritative.
            pass
        yield from super().events(stop_event=stopper, poll_interval_s=poll_interval_s)

    @staticmethod
    def _subscription_status(message: Any) -> Mapping[str, Any] | None:
        if not isinstance(message, Mapping):
            return None
        initial = nested(message, "result", "status")
        if isinstance(initial, Mapping):
            return initial
        params = message.get("params")
        if message.get("method") == "notify_status_update" and isinstance(params, list) and params and isinstance(params[0], Mapping):
            return params[0]
        return None

    def _rows(self, status: Mapping[str, Any], info: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw = {"printer_info": info, "objects": status}
        print_stats = status.get("print_stats") if isinstance(status.get("print_stats"), Mapping) else {}
        virtual_sd = status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), Mapping) else {}
        extruder = status.get("extruder") if isinstance(status.get("extruder"), Mapping) else {}
        bed = status.get("heater_bed") if isinstance(status.get("heater_bed"), Mapping) else {}
        vendor_job = print_stats.get("state")
        vendor_device = info.get("state")
        device = _JOB_DEVICE_STATE.get(str(vendor_job).lower()) if isinstance(vendor_job, str) else None
        device = device or (_DEVICE_STATE.get(str(vendor_device).lower()) if isinstance(vendor_device, str) else None)
        job = _JOB_STATE.get(str(vendor_job).lower()) if isinstance(vendor_job, str) else None
        return self._finish_snapshot([
            self._row("device_state", device if device is not None else _MISSING, raw=raw, why=f"unmapped Moonraker state {vendor_job or vendor_device!r}"),
            self._row("job_state", job if job is not None else _MISSING, raw=raw, why=f"unmapped Moonraker job state {vendor_job!r}"),
            self._row("job_name", string_or_missing(print_stats.get("filename")), raw=raw),
            self._row("progress", number_or_missing(virtual_sd.get("progress")), raw=raw),
            self._row("nozzle_temperature_c", number_or_missing(extruder.get("temperature")), raw=raw),
            self._row("nozzle_target_c", number_or_missing(extruder.get("target")), raw=raw),
            self._row("bed_temperature_c", number_or_missing(bed.get("temperature")), raw=raw),
            self._row("bed_target_c", number_or_missing(bed.get("target")), raw=raw),
        ], raw)
