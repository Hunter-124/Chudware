"""OctoPrint 1.11 REST adapter with authoritative job/state polling."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

from .base import PrinterAdapter, _MISSING, nested, number_or_missing, percent_fraction, string_or_missing
from .model import Artifact, EndpointConfig, ProtocolError, capability_report, operation_truth

_SAFE_REMOTE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


def _validated_remote_path(value: Any) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or not _SAFE_REMOTE_PATH.fullmatch(value):
        raise ProtocolError("OctoPrint returned an unsafe remote path")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ProtocolError("OctoPrint returned an unsafe remote path")
    return value

_DEVICE_STATE = {
    "operational": "idle",
    "ready": "idle",
    "opening serial connection": "connecting",
    "detecting baudrate": "identifying",
    "connecting": "connecting",
    "starting": "running",
    "printing": "running",
    "transferring file to sd": "running",
    "paused": "hold",
    "pausing": "hold",
    "resuming": "running",
    "cancelling": "hold",
    "finishing": "running",
    "finished": "idle",
    "cancelled": "idle",
    "closed": "disconnected",
    "offline": "disconnected",
    "offline after error": "fault",
    "error": "fault",
    "unknown": None,
}
_JOB_STATE = {
    "operational": "draft",
    "ready": "draft",
    "opening serial connection": "draft",
    "detecting baudrate": "draft",
    "connecting": "draft",
    "starting": "arming",
    "printing": "running",
    "transferring file to sd": "queued",
    "paused": "held",
    "pausing": "holding",
    "resuming": "resuming",
    "cancelling": "aborting",
    "finishing": "completing",
    "finished": "completed",
    "cancelled": "aborted",
    "closed": "abandoned",
    "offline": "abandoned",
    "offline after error": "faulted",
    "error": "faulted",
}


class OctoPrintAdapter(PrinterAdapter):
    adapter_kind = "octoprint"

    def __init__(self, config: EndpointConfig) -> None:
        super().__init__(config, api_key_header="X-Api-Key")
        self._version: dict[str, Any] | None = None

    def probe(self) -> dict[str, Any]:
        version = self.http.request_json("GET", "/api/version")
        printer = self.http.request_json("GET", "/api/printer")
        job = self.http.request_json("GET", "/api/job")
        if not isinstance(version, Mapping) or not isinstance(printer, Mapping) or not isinstance(job, Mapping):
            raise ProtocolError("OctoPrint probe endpoints must return JSON objects")
        self._version = dict(version)
        self._operations = {
            "snapshot": operation_truth("verified"),
            "event_stream": operation_truth("verified", "REST polling is authoritative; SockJS is intentionally not required"),
            "upload": operation_truth("verified"),
            "start": operation_truth("execute_assumed"),
            "pause": operation_truth("execute_assumed"),
            "resume": operation_truth("execute_assumed"),
            "cancel": operation_truth("execute_assumed"),
        }
        identity = json.dumps(self.redactor.value(version), sort_keys=True, separators=(",", ":"))
        subject = "printer:octoprint:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return capability_report(
            subject_id=subject,
            adapter_kind=self.adapter_kind,
            monitor_verified=True,
            transport_verified=True,
            operations=self._operations,
            raw={"version": version},
            redactor=self.redactor,
        )

    def snapshot(self) -> list[dict[str, Any]]:
        try:
            printer = self.http.request_json("GET", "/api/printer")
            job = self.http.request_json("GET", "/api/job")
            if not isinstance(printer, Mapping) or not isinstance(job, Mapping):
                raise ProtocolError(
                    "OctoPrint snapshot endpoints must return JSON objects",
                    reason_code="malformed_body",
                )
            raw = {"printer": printer, "job": job}
            vendor_state = nested(printer, "state", "text")
            if not isinstance(vendor_state, str):
                vendor_state = job.get("state")
            folded = vendor_state.strip().lower() if isinstance(vendor_state, str) else ""
            device = _DEVICE_STATE.get(folded)
            run = _JOB_STATE.get(folded)
            tool = nested(printer, "temperature", "tool0")
            if not isinstance(tool, Mapping):
                tool = {}
            bed = nested(printer, "temperature", "bed")
            if not isinstance(bed, Mapping):
                bed = {}
            return self._finish_snapshot([
                self._row("device_state", device if device is not None else _MISSING, raw=raw, why=f"unmapped OctoPrint state {vendor_state!r}"),
                self._row("job_state", run if run is not None else _MISSING, raw=raw, why=f"unmapped OctoPrint job state {vendor_state!r}"),
                self._row("job_name", string_or_missing(nested(job, "job", "file", "name")), raw=raw),
                self._row("progress", percent_fraction(nested(job, "progress", "completion")), raw=raw),
                self._row("nozzle_temperature_c", number_or_missing(tool.get("actual")), raw=raw),
                self._row("nozzle_target_c", number_or_missing(tool.get("target")), raw=raw),
                self._row("bed_temperature_c", number_or_missing(bed.get("actual")), raw=raw),
                self._row("bed_target_c", number_or_missing(bed.get("target")), raw=raw),
            ], raw)
        except ProtocolError as error:
            return self._unknown_snapshot(error)

    def upload(self, artifact: Artifact) -> dict[str, Any]:
        self._require_operation("upload")
        response = self.http.multipart(
            "/api/files/local",
            fields={"select": "false", "print": "false"},
            file_field="file",
            filename=artifact.remote_name,
            data=artifact.data,
            expected=(200, 201),
        )
        payload = self.http.decode_json(response, method="POST")
        local = nested(payload, "files", "local")
        if not isinstance(local, Mapping):
            raise ProtocolError("OctoPrint upload response lacks files.local")
        remote_path = _validated_remote_path(local.get("path") or local.get("name"))
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
        remote_path = _validated_remote_path(remote_artifact.get("remote_path"))
        selected = self.http.request_json(
            "POST",
            "/api/files/local/" + quote(remote_path, safe="/"),
            payload={"command": "select", "print": False},
            expected=(200, 204),
            allow_empty=True,
        )
        started = self.http.request_json(
            "POST", "/api/job", payload={"command": "start"}, expected=(200, 204), allow_empty=True
        )
        return self._command_result("start", {"select": selected, "start": started})

    def pause(self) -> dict[str, Any]:
        self._require_operation("pause")
        raw = self.http.request_json(
            "POST",
            "/api/job",
            payload={"command": "pause", "action": "pause"},
            expected=(200, 204),
            allow_empty=True,
        )
        return self._command_result("pause", raw)

    def resume(self) -> dict[str, Any]:
        self._require_operation("resume")
        raw = self.http.request_json(
            "POST",
            "/api/job",
            payload={"command": "pause", "action": "resume"},
            expected=(200, 204),
            allow_empty=True,
        )
        return self._command_result("resume", raw)

    def cancel(self) -> dict[str, Any]:
        self._require_operation("cancel")
        raw = self.http.request_json(
            "POST", "/api/job", payload={"command": "cancel"}, expected=(200, 204), allow_empty=True
        )
        return self._command_result("cancel", raw)
