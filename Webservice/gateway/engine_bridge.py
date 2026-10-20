"""Strict browser RPC admission to OS-isolated, workspace-owned tenant cells."""
from __future__ import annotations

import asyncio
import hashlib
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
    from . import accounts, authn, cells, orgs, provider_broker
    from .auth import AuthError, revalidate_websocket_user
    from .config import settings
    from .limits import enforce_disk_quota, storage_write_lock
    from .plans import limits_for
except ImportError:
    import accounts, authn, cells, orgs, provider_broker
    from auth import AuthError, revalidate_websocket_user
    from config import settings
    from limits import enforce_disk_quota, storage_write_lock
    from plans import limits_for

log = logging.getLogger("chudware-web.engine")
router = APIRouter()
WEB_RPC_VERSION = 2
_MAX_PENDING_REQUESTS = 32
_INTERNAL_HELLO = -(2**31)
_bridge_lock = threading.Lock()
_active_bridges = 0
_bridge_owners: dict[str, int] = {}
_MESH_GRANT_TTL = 24 * 60 * 60
_MESH_GRANT_LIMIT = 8192
_mesh_grants_lock = threading.Lock()
_mesh_grants: OrderedDict[tuple[str, str, str], tuple[float, cells.CellPrincipal, Path | None, str]] = OrderedDict()
_DOC_EVENTS = frozenset({"doc.opened", "doc.changed", "doc.closed", "doc.lease_change",
                         "doc.studies_changed", "doc.history_cleared"})
_ACTOR_EVENTS = frozenset({"agent.event", "agent.message", "agent.tool_call", "agent.context",
                          "agent.question", "agent.answer", "agent.steer", "agent.provider_usage",
                          "agent.session", "agent.spawn", "agent.model_assignment",
                          "agent.context_injected", "agent.progress_nudge", "sim.progress",
                          "sim.completed", "job.progress", "job.completed", "dag.event"})


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
            if request and key in {"principal", "ctx", "_chudware_actor", "api_key", "_subscription",
                                   "_pool", "provider_grant", "web_provider_grant", "web_credential",
                                   "byok_key", "_byok_key_sha256", "_provider_catalog"}:
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
    if not isinstance(doc_id, str) or not 0 < len(doc_id) <= 180:
        return
    root, relative = _mesh_location(principal, path)
    key = (principal.account_uid, principal.workspace_key, doc_id)
    now = time.monotonic()
    with _mesh_grants_lock:
        for old, row in list(_mesh_grants.items()):
            if row[0] <= now:
                _mesh_grants.pop(old)
        existing = _mesh_grants.pop(key, None)
        if root is None and existing:
            root, relative = existing[2:]
        _mesh_grants[key] = (now + _MESH_GRANT_TTL, principal, root, relative)
        while len(_mesh_grants) > _MESH_GRANT_LIMIT:
            _mesh_grants.popitem(last=False)


def invalidate_mesh_grants(keys: set) -> None:
    with _mesh_grants_lock:
        for key in list(_mesh_grants):
            if (key[1], key[0]) in keys:
                del _mesh_grants[key]


def _mesh_grant(uid, workspace_key, doc_id):
    with _mesh_grants_lock:
        row = _mesh_grants.get((uid, workspace_key, doc_id))
    if not row or row[0] <= time.monotonic():
        return None
    principal = row[1]
    try:
        with cells.tx() as cur:
            cur.execute("SELECT email,status FROM accounts WHERE uid=%s", (uid,))
            account = cur.fetchone()
        if not account or account["status"] != "active":
            return None
        current = cells.resolve_workspace(account["email"], principal.team_id)
        if current.workspace_key != workspace_key or current.epoch != principal.epoch:
            return None
    except HTTPException:
        return None
    return row


def mesh_document_authorized(account_uid, workspace_key, doc_id) -> bool:
    return _mesh_grant(account_uid, workspace_key, doc_id) is not None


def mesh_document_open(account_uid, workspace_key, doc_id, *, max_bytes=None):
    row = _mesh_grant(account_uid, workspace_key, doc_id)
    if not row or row[2] is None:
        return None
    try:
        return cells.open_beneath(row[2], row[3], max_bytes=max_bytes or settings.max_upload_bytes)
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


def _reserve_bridge(uid):
    global _active_bridges
    with _bridge_lock:
        count = _bridge_owners.get(uid, 0)
        if count >= settings.max_engine_bridges_per_user:
            return False, count
        _bridge_owners[uid] = count + 1
        _active_bridges += 1
        return True, count + 1


def _release_bridge(uid):
    global _active_bridges
    with _bridge_lock:
        count = _bridge_owners[uid] - 1
        if count:
            _bridge_owners[uid] = count
        else:
            del _bridge_owners[uid]
        _active_bridges -= 1
        return count, _active_bridges


def _error(request_id, code, message):
    return json.dumps({"id": request_id, "error": {"code": code, "message": message}}, separators=(",", ":"))


async def _principal_control(principal, epoch, current=None):
    updated = cells.CellPrincipal(principal.account_uid, principal.workspace_key, principal.actor_key,
                                  current.right if current else principal.right, epoch, principal.cell_id,
                                  principal.root, principal.team_id, principal.plan)
    writer = None
    try:
        reader, writer = await cells.connect_cell(updated, activate=False)
        writer.write(json.dumps({"id": _INTERNAL_HELLO, "method": "hello", "params": {
            "client": "control", "proto": 1, "principal": updated.hello()}}).encode() + b"\n")
        await writer.drain()
        async with asyncio.timeout(10):
            raw = await reader.readline()
            hello = parse_cell_frame(raw)
            if (hello is None or hello.get("id") != _INTERNAL_HELLO or "error" in hello
                    or not isinstance(hello.get("result"), dict)):
                return
        method = "principal_update" if current else "principal_revoke"
        params = {"actor_key": updated.actor_key, "epoch": epoch}
        if current:
            params["right"] = current.right
        writer.write(json.dumps({"id": 1, "method": method, "params": params}).encode() + b"\n")
        await writer.drain()
        async with asyncio.timeout(10):
            await reader.readline()
    except (OSError, HTTPException, ValueError, TimeoutError):
        log.warning("cell principal control unavailable for %s", principal.cell_id)
    finally:
        if writer:
            writer.close()


@router.websocket("/engine")
async def engine_ws(ws: WebSocket):
    user = ws.state.user
    try:
        team = orgs.selected_workspace_team(ws, user.email)
        principal = cells.resolve_workspace(user.email, team)
        account = accounts.get_account(user.email)
        university_access = bool(team and team == settings.university_team_id and orgs.university_eligible(user.email))
        if not limits_for(account.plan).cloud and not university_access:
            raise HTTPException(403, "plan_no_cloud")
        if account.plan == "trial" and authn.trial_expired(user.email) and not university_access:
            raise HTTPException(403, "plan_no_cloud")
    except (HTTPException, ValueError):
        await ws.close(code=4403, reason="workspace unavailable")
        return
    reserved, _ = _reserve_bridge(principal.account_uid)
    if not reserved:
        await ws.close(code=4429, reason="too many engine connections")
        return
    writer = None
    tasks = []
    revoke_tasks = set()
    token = None
    pending = {}
    docs = set()
    nonce = principal.hello()
    async def revoked(epoch):
        await ws.close(code=4403, reason="revoked")
        if writer:
            writer.close()
    def revoke_callback(epoch):
        task = asyncio.create_task(revoked(epoch))
        revoke_tasks.add(task)
        task.add_done_callback(revoke_tasks.discard)
    async def authorized():
        try:
            revalidate_websocket_user(ws.scope, require_cloud=False)
            current = cells.resolve_workspace(user.email, team)
            if current.epoch != principal.epoch or current.right != principal.right:
                raise HTTPException(403, "revoked")
            return True
        except (AuthError, HTTPException):
            await ws.close(code=4403, reason="revoked")
            return False
    try:
        await ws.accept()
        token = cells.register(principal, revoke_callback)
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
                if not await authorized():
                    return
                try:
                    request = parse_browser_request(frame, right=principal.right)
                    policy = cells.method_policy()[request["method"]]
                    if principal.team_id and policy["right"] != "read":
                        orgs.resolve_team_bucket(user.email, principal.team_id, write=True)
                    if policy["right"] != "read":
                        # Interim native/worker accounting: every write/run admission
                        # measures actual cell workspace usage, rather than cached upload usage.
                        with storage_write_lock(principal.root):
                            enforce_disk_quota(user.email, principal.plan, principal.root)
                    if request["method"] == "hello":
                        request["params"] = {"client": "gui", "proto": 1, "principal": nonce}
                    if request["method"] in {"agent_admin_turn", "agent_cancel", "agent_steer",
                                              "agent_answer", "chudcode_close_session"}:
                        try:
                            from .credits import actor_for
                        except ImportError:
                            from credits import actor_for
                        params = request["params"]
                        actor = actor_for(user.email)
                        actor["key_source"] = "byok"
                        params["_chudware_actor"] = actor
                        if request["method"] == "agent_admin_turn":
                            try:
                                params["provider_grant"] = provider_broker.mint_grant(
                                    user.email, principal, params.get("provider", ""))
                            except provider_broker.BrokerRefusal:
                                raise InvalidRequest("credential_isolation_unsupported",
                                                     "BYOK provider key required", request["id"]) from None
                    if request["method"] == "chudcode_models_list":
                        catalog = {}
                        for provider in sorted(provider_broker.SUPPORTED_PROVIDERS):
                            try:
                                catalog[provider] = provider_broker.mint_grant(user.email, principal, provider)
                            except provider_broker.BrokerRefusal:
                                continue  # This actor has no currently usable broker source.
                        request["params"]["_provider_catalog"] = catalog
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
                    await ws.send_text(_error(request.get("id") if "request" in locals() else None,
                                              "forbidden_right", str(exc.detail)))

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
                if not await authorized():
                    return
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
                    policy = cells.method_policy().get(request["method"])
                    if policy and policy["right"] != "read":
                        try:
                            with storage_write_lock(principal.root):
                                enforce_disk_quota(user.email, principal.plan, principal.root)
                        except HTTPException:
                            await ws.send_text(_error(reply["id"], "quota_exceeded", "cell workspace storage quota exceeded"))
                            continue
                _publish_mesh(principal, reply, docs, inherited_doc)
                await ws.send_text(json.dumps(reply, separators=(",", ":"), allow_nan=False))

        async def epoch_watch():
            # Observers stay revoked even across another gateway process; this is
            # not an agent run cap, only live authorization revalidation.
            while True:
                await asyncio.sleep(1)
                if not await authorized():
                    return
        tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound()), asyncio.create_task(epoch_watch())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error and not isinstance(error, (WebSocketDisconnect, ConnectionError, RuntimeError)):
                log.warning("tenant bridge ended for %s", principal.cell_id)
    finally:
        # Reclaim admission state synchronously even if ASGI cancels shutdown.
        if token:
            cells.unregister(principal, token)
        if writer:
            writer.close()
        _release_bridge(principal.account_uid)
        for task in [*tasks, *revoke_tasks]:
            task.cancel()
        # AnyIO's disconnect scope otherwise repeatedly cancels cleanup awaits,
        # leaving child tasks alive and exposing cancellation to WS clients.
        with anyio.CancelScope(shield=True):
            if tasks or revoke_tasks:
                await asyncio.gather(*tasks, *revoke_tasks, return_exceptions=True)
            if writer:
                await writer.wait_closed()
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
