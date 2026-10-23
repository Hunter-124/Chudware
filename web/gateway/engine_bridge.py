"""WS /engine: strict browser RPC admission to the operator's engine cell.

Each browser frame is parsed as one strict JSON-RPC object, admitted only if
contracts/cell_methods.json allows the method for the principal's right, and
forwarded to the cell's hostd as one newline-delimited line. Replies and events
from the cell are re-validated before they reach the browser; document events
are forwarded only for documents this connection opened.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

try:
    from . import cells
    from .config import settings
except ImportError:  # uvicorn app:app from the package directory
    import cells
    from config import settings

log = logging.getLogger("chudware-web.engine")
router = APIRouter()
WEB_RPC_VERSION = 2
_MAX_PENDING_REQUESTS = 32
_INTERNAL_HELLO = -(2**31)
_bridge_lock = threading.Lock()
_active_bridges = 0
_MESH_GRANT_TTL = 24 * 60 * 60
_MESH_GRANT_LIMIT = 8192
_mesh_grants_lock = threading.Lock()
_mesh_grants: OrderedDict[tuple[str, str], tuple[float, Path | None, str]] = OrderedDict()
_DOC_EVENTS = frozenset({"doc.opened", "doc.changed", "doc.closed", "doc.lease_change",
                         "doc.studies_changed", "doc.history_cleared"})
_ACTOR_EVENTS = frozenset({"agent.event", "agent.message", "agent.tool_call", "agent.context",
                          "agent.question", "agent.answer", "agent.steer", "agent.provider_usage",
                          "agent.session", "agent.spawn", "agent.model_assignment",
                          "agent.context_injected", "agent.progress_nudge", "sim.progress",
                          "sim.completed", "job.progress", "job.completed", "dag.event"})
# Identity and credential fields are attached by trusted code only; a browser
# frame carrying any of them is refused rather than stripped.
_RESERVED_FIELDS = frozenset({"principal", "ctx", "_chudware_actor", "api_key", "_subscription",
                              "_pool", "provider_grant", "web_provider_grant", "web_credential",
                              "byok_key", "_byok_key_sha256", "_provider_catalog"})


class InvalidRequest(ValueError):
    def __init__(self, code: int | str, message: str, request_id: int | None = None):
        super().__init__(message)
        self.code, self.request_id = code, request_id


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _check_value(value, depth=0, *, request=False):
    if depth > 48:
        raise ValueError("JSON nesting too deep")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, str):
        if any((ord(c) < 32 and c not in "\n\r\t") or 127 <= ord(c) <= 159
               or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("invalid string")
    elif isinstance(value, dict):
        if len(value) > 4096:
            raise ValueError("object too large")
        for key, item in value.items():
            _check_value(key, depth + 1, request=request)
            if request and key in _RESERVED_FIELDS:
                raise ValueError("reserved identity/credential field")
            _check_value(item, depth + 1, request=request)
    elif isinstance(value, list):
        if len(value) > 100000:
            raise ValueError("array too large")
        for item in value:
            _check_value(item, depth + 1, request=request)


def parse_browser_request(frame: str, *, right: str = "viewer") -> dict[str, Any]:
    try:
        if len(frame.encode("utf-8")) > settings.ws_max_message_bytes:
            raise ValueError("frame too large")
        message = json.loads(frame, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        _check_value(message, request=True)
    except (ValueError, RecursionError, UnicodeError):
        raise InvalidRequest(-32700, "invalid JSON request") from None
    if not isinstance(message, dict):
        raise InvalidRequest(-32600, "request must be an object")
    request_id = message.get("id")
    if type(request_id) is not int or not -(2**31) < request_id < 2**31:
        raise InvalidRequest(-32600, "id must be a signed 32-bit integer")
    if message.keys() - {"id", "method", "params", "jsonrpc"} or message.get("jsonrpc", "2.0") != "2.0":
        raise InvalidRequest(-32600, "unsupported envelope fields", request_id)
    method, params = message.get("method"), message.get("params", {})
    if not isinstance(method, str) or not method or len(method) > 128:
        raise InvalidRequest(-32600, "invalid method", request_id)
    if not isinstance(params, dict):
        raise InvalidRequest(-32602, "params must be an object", request_id)
    policy = cells.method_policy().get(method)
    if not policy or policy["cell"] != "allow":
        raise InvalidRequest("cell_refused", "method unavailable in tenant cell", request_id)
    if policy["right"] not in cells.RIGHTS.get(right, set()):
        raise InvalidRequest("forbidden_right", "workspace right does not admit method", request_id)
    if method == "ping" and params:
        raise InvalidRequest(-32602, "ping takes no parameters", request_id)
    if method == "hello" and (params.keys() - {"client", "pid", "proto", "user"}
                              or params.get("proto", 1) != 1):
        raise InvalidRequest(-32602, "invalid hello parameters", request_id)
    return {"id": request_id, "method": method, "params": params}


def parse_cell_frame(raw: bytes) -> dict | None:
    if not raw or len(raw) > settings.ws_max_message_bytes or not raw.endswith(b"\n"):
        return None
    try:
        frame = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        _check_value(frame)
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None
    if not isinstance(frame, dict):
        return None
    if "event" in frame:
        if (frame.keys() - {"event", "data"} or not isinstance(frame.get("event"), str)
                or frame["event"] not in _DOC_EVENTS | _ACTOR_EVENTS):
            return None
        data = frame.get("data")
        if not isinstance(data, dict):
            return None
        if frame["event"] in _DOC_EVENTS and (not isinstance(data.get("doc_id"), str)
                                               or not 0 < len(data["doc_id"]) <= 180):
            return None
        # hostd wraps the mapped CAD feed, whose discriminator is kind, not
        # the upstream SDK's raw type (contracts/agent_events.md).
        if frame["event"] == "agent.event" and (not isinstance(data.get("kind"), str)
                                                 or not 0 < len(data["kind"]) <= 128):
            return None
        return frame
    if (frame.keys() - {"id", "result", "error", "jsonrpc"} or type(frame.get("id")) is not int
            or frame.get("jsonrpc", "2.0") != "2.0"):
        return None
    if ("result" in frame) == ("error" in frame):
        return None
    if "error" in frame and (not isinstance(frame["error"], dict)
                             or not isinstance(frame["error"].get("message"), str)
                             or type(frame["error"].get("code")) not in (int, str)):
        return None
    return frame


def _mesh_location(principal, path):
    if not isinstance(path, str):
        return None, ""
    if re.fullmatch(r"/state/mesh/[0-9a-f]{32}\.aicm", path):
        return settings.cells_root / principal.cell_id / "state" / "mesh", Path(path).name
    relative = path[len("/work/"):] if path.startswith("/work/") else path
    if relative.startswith("/") or "\\" in relative or any(p in {"", ".", ".."} for p in relative.split("/")):
        return None, ""
    return principal.root, relative


def grant_mesh_document(principal: cells.CellPrincipal, doc_id: str, path: str | None = None) -> None:
    """Record that this workspace's cell published ``doc_id``'s mesh at ``path``."""
    if not isinstance(doc_id, str) or not 0 < len(doc_id) <= 180:
        return
    root, relative = _mesh_location(principal, path)
    key = (principal.workspace_key, doc_id)
    now = time.monotonic()
    with _mesh_grants_lock:
        for old, row in list(_mesh_grants.items()):
            if row[0] <= now:
                _mesh_grants.pop(old)
        existing = _mesh_grants.pop(key, None)
        if root is None and existing:
            root, relative = existing[1:]
        _mesh_grants[key] = (now + _MESH_GRANT_TTL, root, relative)
        while len(_mesh_grants) > _MESH_GRANT_LIMIT:
            _mesh_grants.popitem(last=False)


def mesh_document_open(workspace_key, doc_id, *, max_bytes=None):
    """Open a granted mesh through a pinned descriptor, or None."""
    with _mesh_grants_lock:
        row = _mesh_grants.get((workspace_key, doc_id))
    if not row or row[0] <= time.monotonic() or row[1] is None:
        return None
    try:
        return cells.open_beneath(row[1], row[2], max_bytes=max_bytes or settings.max_upload_bytes)
    except HTTPException:
        return None


def _publish_mesh(principal, value, docs, inherited_doc=""):
    if isinstance(value, dict):
        doc = value.get("doc_id", inherited_doc)
        if isinstance(doc, str) and doc in docs:
            path = value.get("aicm_path")
            if isinstance(path, str) and path:
                root, relative = _mesh_location(principal, path)
                if root:
                    grant_mesh_document(principal, doc, path)
                    value["aicm_path"] = "aicm-cell:" + json.dumps(
                        {"workspace_key": principal.workspace_key, "doc_id": doc, "path": relative},
                        separators=(",", ":"))
                else:
                    value["aicm_path"] = ""
        for item in value.values():
            _publish_mesh(principal, item, docs, doc if isinstance(doc, str) else inherited_doc)
    elif isinstance(value, list):
        for item in value:
            _publish_mesh(principal, item, docs, inherited_doc)


def active_bridge_count():
    with _bridge_lock:
        return _active_bridges


def _reserve_bridge():
    global _active_bridges
    with _bridge_lock:
        if _active_bridges >= settings.max_engine_bridges:
            return False
        _active_bridges += 1
        return True


def _release_bridge():
    global _active_bridges
    with _bridge_lock:
        _active_bridges -= 1


def _error(request_id, code, message):
    return json.dumps({"id": request_id, "error": {"code": code, "message": message}}, separators=(",", ":"))


@router.websocket("/engine")
async def engine_ws(ws: WebSocket):
    try:
        principal = cells.resolve_workspace()
    except HTTPException:
        await ws.close(code=4403, reason="workspace unavailable")
        return
    if not _reserve_bridge():
        await ws.close(code=4429, reason="too many engine connections")
        return
    writer = None
    tasks = []
    pending = {}
    docs = set()
    nonce = principal.hello()
    try:
        await ws.accept()
        try:
            reader, writer = await cells.connect_cell(principal)
            writer.write(json.dumps({"id": _INTERNAL_HELLO, "method": "hello", "params": {
                "client": "gui", "proto": 1, "principal": nonce}}).encode() + b"\n")
            await writer.drain()
            async with asyncio.timeout(15):
                hello = parse_cell_frame(await reader.readline())
            if not hello or hello.get("id") != _INTERNAL_HELLO or "error" in hello:
                raise HTTPException(503, "cell handshake refused")
        except (HTTPException, OSError, ValueError, TimeoutError):
            await ws.close(code=1013, reason="cell unavailable")
            return

        async def inbound():
            while True:
                frame = await ws.receive_text()
                try:
                    frame_size = len(frame.encode("utf-8"))
                except UnicodeError:
                    await ws.close(code=1007, reason="invalid UTF-8")
                    return
                if frame_size > settings.ws_max_message_bytes:
                    await ws.close(code=1009, reason="frame too large")
                    return
                try:
                    request = parse_browser_request(frame, right=principal.right)
                    if request["method"] == "hello":
                        request["params"] = {"client": "gui", "proto": 1, "principal": nonce}
                    ident = request["id"]
                    if ident in pending or len(pending) >= _MAX_PENDING_REQUESTS:
                        await ws.close(code=4429, reason="too many or duplicate pending requests")
                        return
                    pending[ident] = request
                    writer.write(json.dumps(request, separators=(",", ":"), allow_nan=False).encode() + b"\n")
                    await writer.drain()
                except InvalidRequest as exc:
                    await ws.send_text(_error(exc.request_id, exc.code, str(exc)))
                except HTTPException as exc:
                    await ws.send_text(_error(None, "cell_refused", str(exc.detail)))

        async def outbound():
            while True:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await ws.close(code=1009, reason="invalid cell reply")
                    return
                if not raw:
                    return
                reply = parse_cell_frame(raw)
                if not reply:
                    continue
                inherited_doc = ""
                if "event" in reply:
                    data = reply["data"]
                    if reply["event"] in _DOC_EVENTS and data["doc_id"] not in docs:
                        continue
                    if reply["event"] in _ACTOR_EVENTS and data.get("actor_key", principal.actor_key) != principal.actor_key:
                        continue
                else:
                    request = pending.pop(reply["id"], None)
                    if not request:
                        continue
                    inherited_doc = request["params"].get("doc_id", "")
                    result = reply.get("result")
                    if isinstance(result, dict) and result.get("ok", True) is not False:
                        doc = result.get("doc_id") or request["params"].get("doc_id")
                        if isinstance(doc, str) and request["method"] in {"doc_open", "doc_new", "import_file", "doc_attach", "attach_doc"}:
                            docs.add(doc)
                            grant_mesh_document(principal, doc)
                        if request["method"] == "hello":
                            result = {key: result[key] for key in ("proto", "abi", "git_rev", "engine_build_id", "state")
                                      if key in result and isinstance(result[key], (str, int)) and not isinstance(result[key], bool)}
                            result["web_rpc_version"] = WEB_RPC_VERSION
                            result["workspace_key"] = principal.workspace_key
                            result["web_rpc_methods"] = sorted(name for name, rule in cells.method_policy().items()
                                                                 if rule["cell"] == "allow" and rule["right"] in cells.RIGHTS[principal.right])
                            reply["result"] = result
                _publish_mesh(principal, reply, docs, inherited_doc)
                await ws.send_text(json.dumps(reply, separators=(",", ":"), allow_nan=False))

        tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error and not isinstance(error, (WebSocketDisconnect, ConnectionError, RuntimeError)):
                log.warning("engine bridge ended for cell %s", principal.cell_id)
    finally:
        # Reclaim admission state synchronously even if ASGI cancels shutdown.
        if writer:
            writer.close()
        _release_bridge()
        for task in tasks:
            task.cancel()
        # AnyIO's disconnect scope otherwise repeatedly cancels cleanup awaits,
        # leaving child tasks alive and exposing cancellation to WS clients.
        with anyio.CancelScope(shield=True):
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if writer:
                await writer.wait_closed()
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
