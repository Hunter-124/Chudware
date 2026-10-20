"""Bounded, device-signed desktop security event ingestion."""
from __future__ import annotations

import gzip
import io
import json
import re
import time
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

try:
    from .db import tx
    from .entitlement import _enforcement_state, _sign_object, _verify_device_object
    from .limits import RateLimiter, request_network
    from .metrics import registry
except ImportError:
    from db import tx  # type: ignore
    from entitlement import _enforcement_state, _sign_object, _verify_device_object  # type: ignore
    from limits import RateLimiter, request_network  # type: ignore
    from metrics import registry  # type: ignore

router = APIRouter(tags=["security"])
MAX_COMPRESSED_BYTES = 64 * 1024
MAX_EXPANDED_BYTES = 512 * 1024
MAX_EVENTS = 100
_BATCH_FIELDS = frozenset({"batch_id", "subject", "sequence", "policy_id", "app_version", "platform", "events", "signature"})
_EVENT_FIELDS = frozenset({"reason", "occurred_at"})
_REASON = {
    "manifest_signature_invalid": ("manifest", 100),
    "protected_file_hash_mismatch": ("file_hash", 80),
    "protected_module_unlisted": ("unlisted", 50),
    "debugger_attached": ("debugger", 35),
    "trace_environment_present": ("trace", 20),
    "entitlement_signature_invalid": ("entitlement", 0),
    "heartbeat": ("heartbeat", 0),
}
_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_by_subject = RateLimiter(60, 60.0)
_by_ip = RateLimiter(120, 60.0)


def _decode(body: bytes, encoding: str | None) -> dict[str, Any]:
    if len(body) > MAX_COMPRESSED_BYTES:
        raise HTTPException(413, "security event batch too large")
    if (encoding or "").lower().strip() != "gzip":
        raise HTTPException(415, "security event batch must be gzip encoded")
    try:
        # GzipFile.read bounds expansion across concatenated gzip members too.
        # Never materialize the complete expanded input before applying the cap.
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            raw = compressed.read(MAX_EXPANDED_BYTES + 1)
    except (gzip.BadGzipFile, EOFError, OSError, zlib.error) as exc:
        raise HTTPException(400, "invalid gzip body") from exc
    if len(raw) > MAX_EXPANDED_BYTES:
        raise HTTPException(413, "security event batch expands too large")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(400, "invalid security event JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "invalid security event batch")
    return value


def _validate(batch: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID, list[dict[str, str]]]:
    if set(batch) != _BATCH_FIELDS:
        raise HTTPException(400, "invalid security event batch schema")
    try:
        batch_id, subject = uuid.UUID(batch["batch_id"]), uuid.UUID(batch["subject"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(400, "invalid security event identity") from exc
    if str(batch_id) != batch["batch_id"] or str(subject) != batch["subject"]:
        raise HTTPException(400, "non-canonical security event identity")
    if type(batch.get("sequence")) is not int or batch["sequence"] <= 0:
        raise HTTPException(400, "invalid security event sequence")
    if not all(isinstance(batch.get(k), str) and _TEXT_RE.fullmatch(batch[k]) for k in ("policy_id", "app_version", "platform")):
        raise HTTPException(400, "invalid security event metadata")
    events = batch.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise HTTPException(400, "invalid security event count")
    clean: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict) or set(event) != _EVENT_FIELDS or event.get("reason") not in _REASON:
            raise HTTPException(400, "invalid security event")
        occurred = event.get("occurred_at")
        try:
            stamp = datetime.fromisoformat(occurred.replace("Z", "+00:00")) if isinstance(occurred, str) else None
        except ValueError as exc:
            raise HTTPException(400, "invalid security event timestamp") from exc
        if stamp is None or stamp.tzinfo is None:
            raise HTTPException(400, "invalid security event timestamp")
        clean.append({"reason": event["reason"], "occurred_at": stamp.astimezone(timezone.utc).isoformat()})
    return batch_id, subject, clean


def _response(status: str, batch_id: str, subject: str, sequence: int, policy_id: str,
              enforcement_state: dict[str, Any]) -> dict[str, Any]:
    return _sign_object({
        "status": status, "batch_id": batch_id, "subject": subject, "sequence": sequence,
        "policy_id": policy_id, "enforcement_state": enforcement_state,
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    })


@router.post("/api/security/events")
async def ingest_security_events(request: Request) -> JSONResponse:
    # Charge rejected bodies as well, before buffering or parsing attacker input.
    _by_ip.enforce(request_network(request), "security event batches")
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_COMPRESSED_BYTES:
                raise HTTPException(413, "security event batch too large")
        except ValueError as exc:
            raise HTTPException(400, "invalid content length") from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_COMPRESSED_BYTES - len(body):
            raise HTTPException(413, "security event batch too large")
        body.extend(chunk)
    batch = _decode(bytes(body), request.headers.get("content-encoding"))
    batch_id, subject_uuid, clean_events = _validate(batch)
    subject = str(subject_uuid)
    _by_subject.enforce(subject, "security event batches")
    with tx() as cur:
        cur.execute("SELECT email, device_public_key, last_event_sequence, policy_id, revoked_at FROM desktop_entitlements WHERE subject = %s FOR UPDATE", (subject_uuid,))
        device = cur.fetchone()
        if device is None or device["device_public_key"] is None:
            raise HTTPException(401, "unknown security event subject")
        _verify_device_object(batch, bytes(device["device_public_key"]))
        cur.execute("SELECT sequence, policy_id, subject::text AS subject FROM security_event_batches WHERE batch_id = %s", (batch_id,))
        duplicate = cur.fetchone()
        if duplicate is not None:
            if duplicate["subject"] != subject or int(duplicate["sequence"]) != batch["sequence"] or str(duplicate["policy_id"]) != batch["policy_id"]:
                raise HTTPException(409, "security event batch identity conflict")
            registry.security_batch("duplicate")
            enforcement = _enforcement_state(subject, "active", int(time.time()) + 900)
            return JSONResponse(_response("acknowledged", str(batch_id), subject, int(duplicate["sequence"]), str(duplicate["policy_id"]), enforcement))
        cur.execute("SELECT telemetry_mode FROM security_policies WHERE id = %s", (batch["policy_id"],))
        policy = cur.fetchone()
        if policy is None or batch["policy_id"] != device["policy_id"]:
            raise HTTPException(409, "security policy mismatch")
        current = int(device["last_event_sequence"])
        if batch["sequence"] <= current:
            registry.security_batch("reauthorize")
            enforcement = _enforcement_state(subject, "reauthorize", int(time.time()) + 900)
            return JSONResponse(_response("reauthorize", str(batch_id), subject, current, str(device["policy_id"]), enforcement))
        families: dict[str, tuple[int, str, uuid.UUID]] = {}
        cur.execute("SELECT batch_id, events FROM security_event_batches WHERE subject = %s AND received_at >= now() - interval '24 hours'", (subject_uuid,))
        for row in cur.fetchall():
            for event in row["events"]:
                family, weight = _REASON[event["reason"]]
                if family not in families or weight > families[family][0]:
                    families[family] = (weight, event["reason"], row["batch_id"])
        for event in clean_events:
            family, weight = _REASON[event["reason"]]
            if family not in families or weight > families[family][0]:
                families[family] = (weight, event["reason"], batch_id)
        nonzero = {family: value for family, value in families.items() if value[0] > 0}
        score = sum(value[0] for value in nonzero.values())
        contained = len(nonzero) >= 2 and score >= 100
        stored_events = clean_events
        app_version = batch["app_version"]
        platform = batch["platform"]
        stored_score = score
        if policy["telemetry_mode"] == "minimized":
            verdict = "contained" if contained else "acknowledged"
            reasons = sorted(value[1] for value in nonzero.values())
            event_times = sorted(event["occurred_at"] for event in clean_events)
            stored_events = [{"reason": reason, "occurred_at": event_times[-1] if event_times else datetime.now(timezone.utc).isoformat()}
                             for reason in reasons]
            app_version = verdict
            platform = ",".join(reasons)
            stored_score = 0
        cur.execute("INSERT INTO security_event_batches (batch_id,subject,email,sequence,policy_id,app_version,platform,events,risk_score) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)", (batch_id, subject_uuid, device["email"], batch["sequence"], batch["policy_id"], app_version, platform, json.dumps(stored_events), stored_score))
        action_id: uuid.UUID | None = None
        reasons = sorted(value[1] for value in nonzero.values())
        if contained and device["revoked_at"] is None:
            sources = sorted({value[2] for value in nonzero.values()}, key=str)
            action_id = uuid.uuid4()
            cur.execute("INSERT INTO security_actions (id,email,subject,action,reason_codes,risk_score,source_batches) VALUES (%s,%s,%s,'revoke_device',%s,%s,%s)", (action_id, device["email"], subject_uuid, reasons, score, sources))
            registry.security_action("revoked")
        cur.execute("UPDATE desktop_entitlements SET last_event_sequence=%s,last_security_checkin_at=now(),revoked_at=CASE WHEN %s THEN COALESCE(revoked_at,now()) ELSE revoked_at END WHERE subject=%s", (batch["sequence"], contained, subject_uuid))
    registry.security_batch("accepted")
    state = "contained" if contained else "active"
    enforcement = _enforcement_state(subject, state, int(time.time()) + 900,
                                     reason_codes=reasons if contained else [],
                                     action_id=str(action_id) if action_id is not None else None)
    return JSONResponse(_response(state if contained else "acknowledged", str(batch_id), subject, int(batch["sequence"]), batch["policy_id"], enforcement))
