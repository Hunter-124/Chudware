"""Capability-probed PrusaLink adapter for the documented local draft API."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any
from urllib.parse import quote

from .base import PrinterAdapter, _MISSING, nested, number_or_missing, percent_fraction, string_or_missing
from .http_client import HttpClient, HttpResponse
from .model import (
    Artifact,
    AuthConfig,
    EndpointConfig,
    ProtocolError,
    capability_report,
    operation_truth,
)

_DEVICE_STATE = {
    "IDLE": "idle",
    "READY": "idle",
    "BUSY": "running",
    "PRINTING": "running",
    "PAUSED": "hold",
    "FINISHED": "idle",
    "STOPPED": "idle",
    "ERROR": "fault",
    "ATTENTION": "hold",
}
_JOB_STATE = {
    "IDLE": "draft",
    "READY": "draft",
    "BUSY": "queued",
    "PRINTING": "running",
    "PAUSED": "held",
    "FINISHED": "completed",
    "STOPPED": "aborted",
    "ERROR": "faulted",
    "ATTENTION": "held",
}
_OPTIONAL_STATUSES = (200, 204, 401, 403, 404, 405, 501)


class PrusaLinkAdapter(PrinterAdapter):
    adapter_kind = "prusalink"

    def __init__(self, config: EndpointConfig) -> None:
        super().__init__(config, api_key_header="X-Api-Key")
        self._status_supported = False
        self._storage_path: str | None = None
        self._job_id: int | None = None

    def probe(self) -> dict[str, Any]:
        auth_observation = self._probe_auth()
        version = self.http.request_json("GET", "/api/version")
        if not isinstance(version, Mapping):
            raise ProtocolError("PrusaLink /api/version must return a JSON object")

        status_response, status = self._optional_json("/api/v1/status")
        self._status_supported = status_response.status == 200 and isinstance(status, Mapping)
        job_response, job = self._optional_json("/api/v1/job", allow_empty=True)
        if isinstance(job, Mapping) and isinstance(job.get("id"), int):
            self._job_id = job["id"]
        storage_response, storage = self._optional_json("/api/v1/storage")
        self._storage_path = self._select_storage(storage)

        declared = version.get("capabilities") if isinstance(version.get("capabilities"), Mapping) else {}
        upload_by_put = declared.get("upload-by-put") is True and self._storage_path is not None
        sentinel = self._file_route("__chudware_capability_probe__.gcode") if self._storage_path else None
        file_methods = self._allowed_methods(sentinel) if sentinel else frozenset()
        upload_supported = upload_by_put and (not file_methods or "PUT" in file_methods)
        start_supported = upload_by_put and "POST" in file_methods

        pause_methods = self._allowed_methods(f"/api/v1/job/{self._job_id}/pause") if self._job_id is not None else frozenset()
        resume_methods = self._allowed_methods(f"/api/v1/job/{self._job_id}/resume") if self._job_id is not None else frozenset()
        cancel_methods = self._allowed_methods(f"/api/v1/job/{self._job_id}") if self._job_id is not None else frozenset()

        monitor_why = "verified by /api/v1/status" if self._status_supported else self._endpoint_why("/api/v1/status", status_response)
        no_job_why = (
            "no active job id was available to capability-probe the firmware-specific action route"
            if self._job_id is None
            else "firmware route did not advertise the documented method"
        )
        self._operations = {
            "snapshot": operation_truth("verified") if self._status_supported else operation_truth("unsupported", monitor_why),
            "event_stream": (
                operation_truth("verified", "PrusaLink has no documented push API; polling is authoritative")
                if self._status_supported
                else operation_truth("unsupported", monitor_why)
            ),
            "upload": (
                operation_truth("verified")
                if upload_supported
                else operation_truth("unsupported", "firmware did not expose upload-by-put with writable storage")
            ),
            "start": (
                operation_truth("execute_assumed")
                if start_supported
                else operation_truth("unsupported", "firmware file route did not advertise POST start")
            ),
            "pause": operation_truth("execute_assumed") if "PUT" in pause_methods else operation_truth("unsupported", no_job_why),
            "resume": operation_truth("execute_assumed") if "PUT" in resume_methods else operation_truth("unsupported", no_job_why),
            "cancel": operation_truth("execute_assumed") if "DELETE" in cancel_methods else operation_truth("unsupported", no_job_why),
        }
        identity = json.dumps(self.redactor.value(version), sort_keys=True, separators=(",", ":"))
        subject = "printer:prusalink:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return capability_report(
            subject_id=subject,
            adapter_kind=self.adapter_kind,
            monitor_verified=self._status_supported,
            transport_verified=True,
            operations=self._operations,
            raw={
                "version": version,
                "auth_probe": auth_observation,
                "routes": {
                    "status": status_response.status,
                    "job": job_response.status,
                    "storage": storage_response.status,
                    "file_methods": sorted(file_methods),
                    "pause_methods": sorted(pause_methods),
                    "resume_methods": sorted(resume_methods),
                    "cancel_methods": sorted(cancel_methods),
                },
            },
            redactor=self.redactor,
        )

    def snapshot(self) -> list[dict[str, Any]]:
        self._require_operation("snapshot")
        try:
            status = self.http.request_json("GET", "/api/v1/status")
            if not isinstance(status, Mapping) or not isinstance(status.get("printer"), Mapping):
                raise ProtocolError(
                    "PrusaLink status response lacks required printer object",
                    reason_code="malformed_body",
                )
            printer = status["printer"]
            job = status.get("job") if isinstance(status.get("job"), Mapping) else {}
            raw = status
            vendor_state = printer.get("state")
            folded = vendor_state.upper() if isinstance(vendor_state, str) else ""
            device = _DEVICE_STATE.get(folded)
            run = _JOB_STATE.get(folded)
            return self._finish_snapshot([
                self._row("device_state", device if device is not None else _MISSING, raw=raw, why=f"unmapped PrusaLink state {vendor_state!r}"),
                self._row("job_state", run if run is not None else _MISSING, raw=raw, why=f"unmapped PrusaLink job state {vendor_state!r}"),
                self._row("job_name", string_or_missing(nested(job, "file", "display_name")), raw=raw),
                self._row("progress", percent_fraction(job.get("progress")), raw=raw),
                self._row("nozzle_temperature_c", number_or_missing(printer.get("temp_nozzle")), raw=raw),
                self._row("nozzle_target_c", number_or_missing(printer.get("target_nozzle")), raw=raw),
                self._row("bed_temperature_c", number_or_missing(printer.get("temp_bed")), raw=raw),
                self._row("bed_target_c", number_or_missing(printer.get("target_bed")), raw=raw),
            ], raw)
        except ProtocolError as error:
            return self._unknown_snapshot(error)

    def upload(self, artifact: Artifact) -> dict[str, Any]:
        self._require_operation("upload")
        path = self._file_route(artifact.remote_name)
        response = self.http.request(
            "PUT",
            path,
            body=artifact.data,
            headers={
                "Content-Type": "application/octet-stream",
                "Print-After-Upload": "?0",
                "Overwrite": "?0",
            },
            expected=(200, 201, 204),
        )
        return {
            "adapter_kind": self.adapter_kind,
            "remote_path": path,
            "sha256": artifact.sha256,
            "size": len(artifact.data),
            "raw": {"status": response.status},
        }

    def start(self, remote_artifact: Mapping[str, Any], authorization_token: str | None = None) -> dict[str, Any]:
        self._require_authorization(authorization_token)
        self._require_operation("start")
        path = remote_artifact.get("remote_path")
        if not isinstance(path, str) or not path.startswith("/api/v1/files/"):
            raise ProtocolError("PrusaLink start requires a staged remote_path")
        response = self.http.request("POST", path, body=b"", expected=(200, 204))
        return self._command_result("start", {"status": response.status})

    def pause(self) -> dict[str, Any]:
        self._require_operation("pause")
        response = self.http.request("PUT", f"/api/v1/job/{self._required_job_id()}/pause", body=b"", expected=(200, 204))
        return self._command_result("pause", {"status": response.status})

    def resume(self) -> dict[str, Any]:
        self._require_operation("resume")
        response = self.http.request("PUT", f"/api/v1/job/{self._required_job_id()}/resume", body=b"", expected=(200, 204))
        return self._command_result("resume", {"status": response.status})

    def cancel(self) -> dict[str, Any]:
        self._require_operation("cancel")
        response = self.http.request("DELETE", f"/api/v1/job/{self._required_job_id()}", expected=(200, 204))
        return self._command_result("cancel", {"status": response.status})

    def _probe_auth(self) -> dict[str, Any]:
        unauthenticated = HttpClient(
            self.config.base_url,
            AuthConfig("trusted_host"),
            self.config.timeout_s,
            "X-Api-Key",
            address_class=self.config.address_class,
        )
        response = unauthenticated.request("GET", "/api/version", expected=(200, 401, 403))
        challenge = response.headers.get("WWW-Authenticate", "")
        schemes: list[str] = []
        if response.status == 200:
            schemes.append("trusted_host")
        if challenge:
            schemes.append(challenge.split(None, 1)[0].lower())
        if self.config.auth.mode == "api_key":
            schemes.append("api_key_selected_and_verified_separately")
        return {"configured_mode": self.config.auth.mode, "unauthenticated_status": response.status, "advertised_schemes": sorted(set(schemes))}

    def _optional_json(self, path: str, *, allow_empty: bool = False) -> tuple[HttpResponse, Any | None]:
        response = self.http.request("GET", path, expected=_OPTIONAL_STATUSES)
        if response.status in {401, 403}:
            raise ProtocolError(f"PrusaLink authentication refused access to {path}")
        if response.status == 200:
            return response, self.http.decode_json(response, method="GET")
        if response.status == 204 and allow_empty:
            return response, None
        return response, None

    def _allowed_methods(self, path: str) -> frozenset[str]:
        response = self.http.request("OPTIONS", path, expected=_OPTIONAL_STATUSES)
        if response.status in {401, 403}:
            raise ProtocolError(f"PrusaLink authentication refused capability probe for {path}")
        if response.status not in {200, 204}:
            return frozenset()
        return frozenset(item.strip().upper() for item in response.headers.get("Allow", "").split(",") if item.strip())

    def _select_storage(self, payload: Any) -> str | None:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("storage_list"), list):
            return None
        for item in payload["storage_list"]:
            if not isinstance(item, Mapping):
                continue
            if item.get("available") is True and item.get("read_only") is not True and isinstance(item.get("path"), str):
                return item["path"]
        return None

    def _file_route(self, name: str) -> str:
        if not self._storage_path:
            raise ProtocolError("PrusaLink writable storage was not capability-probed")
        storage = quote(self._storage_path.strip("/"), safe="")
        return f"/api/v1/files/{storage}/{quote(name, safe='')}"

    def _required_job_id(self) -> int:
        if self._job_id is None:
            raise ProtocolError("PrusaLink command requires a capability-probed active job id")
        return self._job_id

    @staticmethod
    def _endpoint_why(path: str, response: HttpResponse) -> str:
        return f"firmware did not expose {path} as JSON (HTTP {response.status})"
