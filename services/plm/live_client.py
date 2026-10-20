"""Authoritative PLM live-session client and synchronous worker manager.

The wire side implements the frozen ``aicad.plm-live/1`` protocol.  The manager
keeps WebSocket I/O on one asyncio thread while the Python worker remains a
synchronous RPC process.  Bearer tokens are process inputs only; profiles and
vault history never receive them.
"""
from __future__ import annotations

import asyncio
import atexit
import hashlib
import inspect
import json
import os
import tempfile
import threading
import uuid
from contextvars import copy_context
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from services.plm import manifest

SCHEMA = "aicad.plm-live/1"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_OPS_BYTES = 1024 * 1024
MAX_OPS = 1000
MAX_PARTICIPANTS = 256
MAX_PRESENCE = 256
MAX_EVENTS = 1024
MAX_PENDING = 1024
_MAX_TEXT = 512
_UINT64_MAX = (1 << 64) - 1

ApplyOps = Callable[[list[dict[str, Any]]], Any]
InstallSnapshot = Callable[[dict[str, Any], str], Any]


class LiveClientError(RuntimeError):
    """Typed, user-safe live-session failure."""

    def __init__(self, reason: str, hint: str) -> None:
        super().__init__(hint)
        self.reason = reason
        self.hint = hint

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "reason": self.reason, "hint": self.hint}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def state_hash(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(document)).hexdigest()


def _bounded_json_size(value: Any, limit: int) -> int:
    """Measure JSON incrementally, stopping before a large value is copied."""
    total = 0
    encoder = json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    try:
        for piece in encoder.iterencode(value):
            total += len(piece.encode("utf-8"))
            if total > limit:
                break
    except (TypeError, ValueError) as exc:
        raise LiveClientError("invalid_message", "The live edit contains invalid JSON data.") from exc
    return total


def _text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise LiveClientError("invalid_message", f"The live server sent an invalid {field}.")
    return value


def _uint(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _UINT64_MAX:
        raise LiveClientError("invalid_message", f"The live server sent an invalid {field}.")
    return value


def _participant(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LiveClientError("invalid_participant", "A live participant identity is required.")
    participant = {
        "id": _text(value.get("id"), "participant id"),
        "display": _text(value.get("display"), "participant display name"),
        "account_id": _text(value.get("account_id"), "participant account", nullable=True),
    }
    return participant


def _snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("encoding") != "canonical-json":
        raise LiveClientError("invalid_snapshot", "The live server sent an unsupported snapshot.")
    document = value.get("document")
    if not isinstance(document, dict):
        raise LiveClientError("invalid_snapshot", "The live server sent an invalid document snapshot.")
    return value


def _hash(value: Any, document: dict[str, Any]) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise LiveClientError("invalid_hash", "The live server sent an invalid state fingerprint.")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise LiveClientError("invalid_hash", "The live server sent an invalid state fingerprint.") from exc
    if state_hash(document) != value:
        raise LiveClientError("hash_mismatch", "The live snapshot did not match its state fingerprint.")
    return value


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class LiveClient:
    """One asynchronous authoritative item session.

    ``join`` establishes the session and starts reconnect handling. ``propose``
    resolves only after the server broadcasts the resulting commit or rejection.
    ``poll`` drains complete commit/snapshot events; partial or out-of-order state
    is never exposed.
    """

    def __init__(
        self,
        url: str,
        project_id: str,
        item_id: str,
        participant: dict[str, Any],
        token: str | None = None,
        *,
        last_seq: int = 0,
        apply_ops: ApplyOps | None = None,
        install_snapshot: InstallSnapshot | None = None,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.url = str(url)
        self.project_id = str(_text(project_id, "project id"))
        self.item_id = str(_text(item_id, "item id"))
        self.participant = _participant(participant)
        selected_token = token if token is not None else os.environ.get("CHUDWARE_PLM_LIVE_TOKEN", "")
        if not isinstance(selected_token, str):
            raise LiveClientError("unauthorized", "The live-session token is invalid.")
        self._token = selected_token
        self._last_seq = _uint(last_seq, "last sequence")
        self._last_hash: str | None = None
        self._session_id: str | None = None
        self._head_seq = self._last_seq
        self._engine_revision = 0
        self._apply_ops = apply_ops
        self._install_snapshot = install_snapshot
        self._connect_factory = connect_factory or websocket_connect
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._closing = False
        self._ready = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._events: deque[dict[str, Any]] = deque()
        self._presence: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._seq_hashes: OrderedDict[int, str] = OrderedDict()
        self._pending: OrderedDict[str, tuple[dict[str, Any], asyncio.Future]] = OrderedDict()
        self._paused = False
        self._catchup_target = self._last_seq
        self._joined_message: dict[str, Any] | None = None
        self._fatal: LiveClientError | None = None

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def connected(self) -> bool:
        return self._ready.is_set() and self._ws is not None and not self._closing

    async def join(self) -> dict[str, Any]:
        if self._closing:
            raise LiveClientError("not_joined", "This live session has already left.")
        if self._reader is not None:
            joined = dict(self._joined_message or {})
            return {"ok": True, **joined}
        await self._connect_once()
        self._reader = asyncio.create_task(self._reader_loop(), name="plm-live-reader")
        return {"ok": True, **dict(self._joined_message or {})}

    async def _connect_once(self) -> None:
        async with self._connect_lock:
            if self._closing:
                return
            self._ready.clear()
            try:
                candidate = self._connect_factory(
                    self.url,
                    max_size=MAX_MESSAGE_BYTES,
                    compression=None,
                    ping_interval=20,
                    ping_timeout=60,
                    open_timeout=15,
                )
                self._ws = await candidate if inspect.isawaitable(candidate) else candidate
                request = {
                    "schema": SCHEMA,
                    "type": "join",
                    "project_id": self.project_id,
                    "item_id": self.item_id,
                    "participant": self.participant,
                    "last_seq": self._last_seq,
                    "token": self._token,
                }
                await self._send(request)
                raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
                message = self._decode(raw)
                if message.get("type") != "joined":
                    raise LiveClientError("join_failed", "The live server did not accept the session join.")
                await self._handle_joined(message)
                self._ready.set()
                for proposal, _future in list(self._pending.values()):
                    proposal["session_id"] = self._session_id
                    await self._send(proposal)
            except LiveClientError:
                await self._drop_socket()
                raise
            except Exception as exc:
                await self._drop_socket()
                raise LiveClientError(
                    "remote_unreachable", "The live server could not be reached."
                ) from exc

    async def _handle_joined(self, message: dict[str, Any]) -> None:
        self._common(message)
        session_id = str(_text(message.get("session_id"), "session id"))
        if message.get("project_id") != self.project_id or message.get("item_id") != self.item_id:
            raise LiveClientError("wrong_session", "The live server joined a different project item.")
        head_seq = _uint(message.get("head_seq"), "head sequence")
        engine_revision = _uint(message.get("engine_revision"), "engine revision")
        participants = message.get("participants")
        if not isinstance(participants, list) or len(participants) > MAX_PARTICIPANTS:
            raise LiveClientError("invalid_participant", "The live server sent an invalid participant list.")
        for value in participants:
            _participant(value)
        if message.get("checkpoint_base") is not None:
            _text(message.get("checkpoint_base"), "checkpoint base", nullable=True)
        self._session_id = session_id
        self._head_seq = head_seq
        self._engine_revision = engine_revision
        snapshot_value = message.get("snapshot")
        if snapshot_value is not None:
            snap = _snapshot(snapshot_value)
            computed = state_hash(snap["document"])
            advertised = message.get("state_hash")
            if advertised is not None:
                computed = _hash(advertised, snap["document"])
            if head_seq < self._last_seq:
                raise LiveClientError(
                    "sequence_regressed", "The live server sequence moved backwards."
                )
            already_installed = head_seq == self._last_seq and computed == self._last_hash
            if not already_installed:
                install = await self._install(snap["document"], computed)
                event = {
                    "schema": SCHEMA,
                    "type": "snapshot",
                    "session_id": session_id,
                    "head_seq": head_seq,
                    "engine_revision": engine_revision,
                    "snapshot": snap,
                    "state_hash": computed,
                }
                if isinstance(install, dict):
                    event.update(install)
                self._queue_event(event)
            self._last_seq = head_seq
            self._last_hash = computed
            self._remember_hash(head_seq, computed)
            self._paused = False
        elif head_seq < self._last_seq:
            raise LiveClientError("sequence_regressed", "The live server sequence moved backwards.")
        self._joined_message = message

    async def _reader_loop(self) -> None:
        backoff = 0.1
        while not self._closing:
            if self._ws is None:
                try:
                    await self._connect_once()
                    backoff = 0.1
                except LiveClientError:
                    if self._closing:
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 2.0)
                    continue
            try:
                raw = await self._ws.recv()
                await self._handle(self._decode(raw))
            except asyncio.CancelledError:
                return
            except (ConnectionClosed, OSError):
                self._ready.clear()
                await self._drop_socket()
            except LiveClientError as exc:
                self._fatal = exc
                self._ready.clear()
                await self._drop_socket()
                self._fail_pending(exc)
                return
            except Exception:
                self._ready.clear()
                await self._drop_socket()

    def _decode(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise LiveClientError("invalid_message", "The live server sent a non-text message.")
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise LiveClientError("message_too_large", "The live server message was too large.")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LiveClientError("invalid_message", "The live server sent malformed JSON.") from exc
        if not isinstance(message, dict):
            raise LiveClientError("invalid_message", "The live server message was not an object.")
        return message

    def _common(self, message: dict[str, Any]) -> None:
        if message.get("schema") != SCHEMA:
            raise LiveClientError("invalid_schema", "The live server uses an unsupported protocol.")
        _text(message.get("type"), "message type")

    def _session(self, message: dict[str, Any]) -> None:
        self._common(message)
        if message.get("session_id") != self._session_id:
            raise LiveClientError("wrong_session", "The live server sent data for another session.")

    async def _handle(self, message: dict[str, Any]) -> None:
        self._common(message)
        kind = message.get("type")
        if kind == "commit":
            await self._handle_commit(message)
        elif kind == "snapshot":
            await self._handle_snapshot(message)
        elif kind == "rejected":
            self._handle_rejected(message)
        elif kind == "presence":
            self._handle_presence(message)
        elif kind == "presence_left":
            self._handle_presence_left(message)
        else:
            raise LiveClientError("invalid_message", "The live server sent an unknown message type.")

    async def _handle_commit(self, message: dict[str, Any]) -> None:
        self._session(message)
        seq = _uint(message.get("seq"), "commit sequence")
        _text(message.get("client_op_id"), "client operation id")
        author = _participant(message.get("author"))
        engine_revision = _uint(message.get("engine_revision"), "engine revision")
        ops = message.get("ops")
        if not isinstance(ops, list) or len(ops) > MAX_OPS:
            raise LiveClientError("invalid_ops", "The live server sent an invalid operation batch.")
        if _bounded_json_size(ops, MAX_OPS_BYTES) > MAX_OPS_BYTES:
            raise LiveClientError("ops_too_large", "The live server operation batch was too large.")
        if any(not isinstance(op, dict) for op in ops):
            raise LiveClientError("invalid_ops", "The live server sent an invalid operation batch.")
        snap = _snapshot(message.get("snapshot"))
        fingerprint = _hash(message.get("state_hash"), snap["document"])

        if seq <= self._last_seq:
            prior = self._seq_hashes.get(seq)
            if prior is None or prior != fingerprint:
                await self._request_catchup(max(self._last_seq, seq))
            return
        if seq != self._last_seq + 1:
            await self._request_catchup(seq)
            return

        try:
            ops_response = await _maybe_await(self._apply_ops(ops)) if self._apply_ops else None
        except LiveClientError:
            raise
        except Exception as exc:
            raise LiveClientError(
                "engine_apply_failed",
                "A committed live edit could not be applied to the local document.",
            ) from exc
        event = dict(message)
        if ops_response is not None:
            event["ops_response"] = ops_response
        self._last_seq = seq
        self._head_seq = max(self._head_seq, seq)
        self._engine_revision = engine_revision
        self._last_hash = fingerprint
        self._remember_hash(seq, fingerprint)
        self._queue_event(event)
        if self._paused and seq >= self._catchup_target:
            self._paused = False
        client_op_id = str(message["client_op_id"])
        if author["id"] == self.participant["id"]:
            pending = self._pending.pop(client_op_id, None)
            if pending is not None and not pending[1].done():
                pending[1].set_result({
                    "ok": True,
                    "client_op_id": client_op_id,
                    "seq": seq,
                    "state_hash": fingerprint,
                    "ops_response": ops_response,
                })

    async def _handle_snapshot(self, message: dict[str, Any]) -> None:
        self._session(message)
        head_seq = _uint(message.get("head_seq"), "head sequence")
        engine_revision = _uint(message.get("engine_revision"), "engine revision")
        snap = _snapshot(message.get("snapshot"))
        fingerprint = _hash(message.get("state_hash"), snap["document"])
        if head_seq < self._last_seq:
            raise LiveClientError(
                "sequence_regressed", "The live server sequence moved backwards."
            )
        if head_seq == self._last_seq and fingerprint == self._last_hash:
            self._paused = False
            return
        install = await self._install(snap["document"], fingerprint)
        self._last_seq = head_seq
        self._head_seq = head_seq
        self._engine_revision = engine_revision
        self._last_hash = fingerprint
        self._seq_hashes.clear()
        self._remember_hash(head_seq, fingerprint)
        self._paused = False
        event = dict(message)
        if isinstance(install, dict):
            event.update(install)
        self._queue_event(event)

    def _handle_rejected(self, message: dict[str, Any]) -> None:
        self._session(message)
        client_op_id = str(_text(message.get("client_op_id"), "client operation id"))
        head_seq = _uint(message.get("head_seq"), "head sequence")
        reason = str(_text(message.get("reason"), "rejection reason"))
        hint = str(_text(message.get("hint"), "rejection hint"))
        pending = self._pending.pop(client_op_id, None)
        if pending is not None and not pending[1].done():
            pending[1].set_result({
                "ok": False,
                "reason": reason,
                "hint": hint,
                "client_op_id": client_op_id,
                "head_seq": head_seq,
            })

    def _handle_presence(self, message: dict[str, Any]) -> None:
        self._session(message)
        if _bounded_json_size(message, 64 * 1024) > 64 * 1024:
            raise LiveClientError(
                "presence_too_large", "The live server sent oversized presence data."
            )
        participant = _participant(message.get("participant"))
        for field in ("cursor", "selection", "viewport"):
            if field in message and not isinstance(message[field], dict):
                raise LiveClientError("invalid_presence", "The live server sent invalid presence data.")
        participant_id = str(participant["id"])
        self._presence.pop(participant_id, None)
        self._presence[participant_id] = message
        while len(self._presence) > MAX_PRESENCE:
            self._presence.popitem(last=False)

    def _handle_presence_left(self, message: dict[str, Any]) -> None:
        self._session(message)
        participant_id = str(_text(message.get("participant_id"), "participant id"))
        self._presence.pop(participant_id, None)

    async def _request_catchup(self, target: int) -> None:
        self._paused = True
        self._catchup_target = max(self._catchup_target, target)
        request = {
            "schema": SCHEMA,
            "type": "catchup",
            "session_id": self._session_id,
            "from_seq": self._last_seq,
        }
        if self._last_hash is not None:
            request["state_hash"] = self._last_hash
        await self._send(request)

    async def _install(self, document: dict[str, Any], fingerprint: str) -> Any:
        if self._install_snapshot is None:
            return None
        try:
            return await _maybe_await(self._install_snapshot(document, fingerprint))
        except LiveClientError:
            raise
        except Exception as exc:
            raise LiveClientError(
                "snapshot_install_failed", "The authoritative live snapshot could not be opened."
            ) from exc

    def _remember_hash(self, seq: int, fingerprint: str) -> None:
        self._seq_hashes[seq] = fingerprint
        self._seq_hashes.move_to_end(seq)
        while len(self._seq_hashes) > 4096:
            self._seq_hashes.popitem(last=False)

    def _queue_event(self, event: dict[str, Any]) -> None:
        if len(self._events) >= MAX_EVENTS:
            raise LiveClientError(
                "poll_required", "Live edits are arriving faster than this client can consume them."
            )
        self._events.append(event)

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            raise LiveClientError("not_connected", "The live session is reconnecting.")
        raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise LiveClientError("message_too_large", "The live-session message is too large.")
        async with self._send_lock:
            await self._ws.send(raw)

    async def propose(
        self,
        ops: list[dict[str, Any]],
        client_op_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(ops, list) or len(ops) > MAX_OPS or any(not isinstance(op, dict) for op in ops):
            raise LiveClientError("invalid_ops", "A live edit must be a list of at most 1,000 operations.")
        if _bounded_json_size(ops, MAX_OPS_BYTES) > MAX_OPS_BYTES:
            raise LiveClientError("ops_too_large", "A live edit may not exceed 1 MiB.")
        op_id = client_op_id or uuid.uuid4().hex
        _text(op_id, "client operation id")
        message = {
            "schema": SCHEMA,
            "type": "propose",
            "session_id": self._session_id,
            "client_op_id": op_id,
            "base_seq": self._last_seq,
            "ops": ops,
        }
        existing = self._pending.get(op_id)
        if existing is not None:
            if _canonical_bytes(existing[0].get("ops")) != _canonical_bytes(ops):
                raise LiveClientError(
                    "duplicate_operation", "That client operation id already names a different edit."
                )
            return await asyncio.shield(existing[1])
        if len(self._pending) >= MAX_PENDING:
            raise LiveClientError("too_many_pending", "Too many live edits are awaiting confirmation.")
        if self._fatal is not None:
            raise self._fatal
        await self._ready.wait()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[op_id] = (message, future)
        try:
            await self._send(message)
        except (ConnectionClosed, OSError):
            # Keep it pending: reconnect resends the same participant/op id.
            self._ready.clear()
            await self._drop_socket()
            await self._ready.wait()
        return await asyncio.shield(future)

    async def poll(self, limit: int = 100) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 256:
            raise LiveClientError("invalid_limit", "Live poll limit must be from 1 to 256.")
        events = []
        while self._events and len(events) < limit:
            events.append(self._events.popleft())
        return {
            "ok": True,
            "events": events,
            "last_seq": self._last_seq,
            "head_seq": self._head_seq,
            "connected": self.connected,
        }

    async def presence(
        self,
        *,
        cursor: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        viewport: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "schema": SCHEMA,
            "type": "presence",
            "session_id": self._session_id,
        }
        for name, value in (("cursor", cursor), ("selection", selection), ("viewport", viewport)):
            if value is not None:
                if not isinstance(value, dict):
                    raise LiveClientError("invalid_presence", "Presence fields must be objects.")
                message[name] = value
        if _bounded_json_size(message, 64 * 1024) > 64 * 1024:
            raise LiveClientError("presence_too_large", "Presence data may not exceed 64 KiB.")
        await self._ready.wait()
        await self._send(message)
        return {"ok": True, "participants": list(self._presence.values())}

    def presence_state(self) -> list[dict[str, Any]]:
        return list(self._presence.values())

    async def leave(self) -> dict[str, Any]:
        if self._closing:
            return {"ok": True, "left": True}
        self._closing = True
        self._ready.clear()
        if self._ws is not None and self._session_id is not None:
            try:
                await self._send({"schema": SCHEMA, "type": "leave", "session_id": self._session_id})
            except Exception:
                pass
        await self._drop_socket()
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        self._fail_pending(LiveClientError("left", "The live session has ended."))
        return {"ok": True, "left": True, "last_seq": self._last_seq}

    async def _drop_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def _fail_pending(self, error: LiveClientError) -> None:
        for _message, future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


class LiveClientManager:
    """Process singleton owning the asyncio loop used by synchronous worker RPCs."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., LiveClient] = LiveClient,
        core_call: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._core_call_override = core_call
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str, str], LiveClient] = {}
        self._local_docs: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._submissions = 0
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._started.clear()
            self._stopping = False
            context = copy_context()
            self._thread = threading.Thread(target=context.run, args=(self._run_loop,),
                                            name="plm-live-loop", daemon=True)
            self._thread.start()
        self._started.wait(timeout=5)
        if self._loop is None:
            raise LiveClientError("internal", "The live-session worker could not start.")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._started.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _submit(self, awaitable: Awaitable, timeout: float = 60.0) -> Any:
        self._start()
        assert self._loop is not None
        with self._lock:
            self._submissions += 1

        async def invoke():
            try:
                return await awaitable
            finally:
                with self._lock:
                    self._submissions -= 1

        invocation = invoke()
        try:
            future = asyncio.run_coroutine_threadsafe(invocation, self._loop)
        except BaseException:
            invocation.close()
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            with self._lock:
                self._submissions -= 1
            raise
        try:
            return future.result(timeout=timeout)
        except LiveClientError:
            raise
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(cause, LiveClientError):
                raise cause
            raise LiveClientError("internal", "The live-session request could not be completed.") from exc

    @staticmethod
    def _key(path: str, remote_id: str, item_id: str) -> tuple[str, str, str]:
        return (str(Path(path).expanduser().resolve()), str(remote_id or "origin"), str(item_id))

    @staticmethod
    def _profile(path: str, remote_id: str, item_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
        from services.plm.vault import Vault

        v = Vault.open(path)
        try:
            project_manifest = v.manifest()
        finally:
            v.close()
        profile = next(
            (
                entry
                for entry in project_manifest.get("remotes", [])
                if entry.get("name") == remote_id or entry.get("id") == remote_id
            ),
            None,
        )
        if profile is None:
            raise LiveClientError("not_found", f"No remote named {remote_id!r} is configured.")
        if manifest.remote_mode(profile) != "live":
            raise LiveClientError("invalid_mode", "This remote uses snapshot sync, not live editing.")
        item = manifest.find_item(project_manifest, item_id)
        if item is None:
            raise LiveClientError("invalid_item", "The requested live item is not in this project.")
        from services.plm.remote import approved_remote_url
        from services.plm.vault import VaultError
        try:
            url = approved_remote_url(path, profile)
        except VaultError as exc:
            raise LiveClientError(exc.reason, exc.hint) from exc
        if not isinstance(url, str) or not url:
            raise LiveClientError("remote_unreachable", "This live remote has no WebSocket location.")
        return project_manifest, profile, url

    @staticmethod
    def _identity(profile: dict[str, Any], participant: dict[str, Any] | None) -> dict[str, Any]:
        if participant:
            return _participant(participant)
        identity = profile.get("identity") or {}
        return _participant({
            "id": identity.get("user_id"),
            "display": identity.get("display"),
            "account_id": identity.get("account_id"),
        })

    def _core_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._core_call_override is not None:
            return self._core_call_override(method, params)
        from services.mcp import core_bridge

        return core_bridge.call(method, params)

    async def _materialize_snapshot(
        self,
        document: dict[str, Any],
        suffix: str,
    ) -> tuple[str, dict[str, Any]]:
        from services.plm import snapshot

        payload = snapshot.reassemble_container(
            snapshot.ExplodedItem(document_json=_canonical_bytes(document))
        )
        fd, temp_path = tempfile.mkstemp(prefix="chudware-live-", suffix=suffix)
        os.close(fd)
        try:
            Path(temp_path).write_bytes(payload)
            opened = await asyncio.to_thread(self._core_call, "doc_open", {"path": temp_path})
        finally:
            try:
                Path(temp_path).unlink()
            except OSError:
                pass
        doc_id = opened.get("doc_id") if isinstance(opened, dict) else None
        if not isinstance(doc_id, str) or not doc_id:
            raise LiveClientError("snapshot_install_failed",
                                  "The authoritative live snapshot could not be opened.")
        return doc_id, opened

    async def _join_async(
        self,
        path: str,
        remote_id: str,
        item_id: str,
        participant: dict[str, Any] | None,
        token: str | None,
        local_doc_id: str | None,
    ) -> dict[str, Any]:
        project_manifest, profile, url = await asyncio.to_thread(
            self._profile, path, remote_id, item_id
        )
        key = self._key(path, remote_id, item_id)
        existing = self._clients.get(key)
        if existing is not None:
            if existing.participant["id"] != self._identity(profile, participant)["id"]:
                raise LiveClientError("already_joined", "This item is already joined by another local participant.")
            return {
                "ok": True,
                "session_id": existing.session_id,
                "last_seq": existing.last_seq,
                "local_doc_id": self._local_docs[key].get("doc_id"),
                "reused": True,
            }
        identity = self._identity(profile, participant)
        item = manifest.find_item(project_manifest, item_id)
        assert item is not None
        suffix = Path(str(item.get("path") or "")).suffix.lower()
        if suffix not in (".step+", ".ecad+"):
            suffix = ".step+"
        local: dict[str, Any] = {
            "doc_id": str(local_doc_id).strip() if local_doc_id else None,
            "state_hash": None,
            "open_response": None,
        }
        if local["doc_id"]:
            attached = await asyncio.to_thread(
                self._core_call,
                "attach_doc",
                {"doc_id": local["doc_id"], "mode": "rw"},
            )
            if isinstance(attached, dict) and attached.get("_is_error"):
                raise LiveClientError(
                    "attach_failed",
                    "The local document could not join the live editing session.",
                )

        async def install(document: dict[str, Any], fingerprint: str) -> dict[str, Any]:
            current = local.get("doc_id")
            matches = False
            if current:
                snap = await asyncio.to_thread(
                    self._core_call, "doc_snapshot", {"doc_id": current}
                )
                if isinstance(snap, dict):
                    candidate = snap.get("state_hash")
                    if isinstance(candidate, str):
                        matches = candidate == fingerprint
                    else:
                        candidate_doc = (
                            snap.get("document")
                            or snap.get("canonical_document")
                            or snap.get("tree")
                        )
                        if isinstance(candidate_doc, dict):
                            matches = state_hash(candidate_doc) == fingerprint
            if not matches:
                current, opened = await self._materialize_snapshot(document, suffix)
                local["open_response"] = opened
            local["doc_id"] = current
            local["state_hash"] = fingerprint
            return {"local_doc_id": current, "open_response": local.get("open_response")}

        async def apply(ops: list[dict[str, Any]]) -> dict[str, Any]:
            doc_id = local.get("doc_id")
            if not doc_id:
                raise LiveClientError("not_joined", "The live item has no local document.")
            response = await asyncio.to_thread(
                self._core_call, "doc_apply_ops", {"doc_id": doc_id, "ops": ops}
            )
            if not isinstance(response, dict) or response.get("_is_error"):
                raise LiveClientError(
                    "engine_apply_failed",
                    "A committed live edit could not be applied to the local document.",
                )
            return response

        client = self._client_factory(
            url,
            project_manifest["project"]["id"],
            item_id,
            identity,
            token,
            apply_ops=apply,
            install_snapshot=install,
        )
        self._clients[key] = client
        self._local_docs[key] = local
        try:
            joined = await client.join()
        except Exception:
            self._clients.pop(key, None)
            self._local_docs.pop(key, None)
            raise
        return {
            **joined,
            "remote_id": remote_id,
            "item_id": item_id,
            "participant": identity,
            "local_doc_id": local.get("doc_id"),
            "open_response": local.get("open_response"),
        }

    def join(
        self,
        path: str,
        remote_id: str,
        item_id: str,
        participant: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        local_doc_id: str | None = None,
    ) -> dict[str, Any]:
        return self._submit(
            self._join_async(path, remote_id, item_id, participant, token, local_doc_id)
        )

    def _client(self, path: str, remote_id: str, item_id: str) -> LiveClient:
        client = self._clients.get(self._key(path, remote_id, item_id))
        if client is None:
            raise LiveClientError("not_joined", "Join this item with plm_live_join first.")
        return client

    def propose(
        self,
        path: str,
        remote_id: str,
        item_id: str,
        participant: dict[str, Any] | None,
        ops: list[dict[str, Any]],
        client_op_id: str | None = None,
    ) -> dict[str, Any]:
        client = self._client(path, remote_id, item_id)
        if participant and _participant(participant)["id"] != client.participant["id"]:
            raise LiveClientError("wrong_participant", "This live session belongs to another participant.")
        return self._submit(client.propose(ops, client_op_id), timeout=120)

    def poll(
        self, path: str, remote_id: str, item_id: str, participant: dict[str, Any] | None, limit: int = 100
    ) -> dict[str, Any]:
        client = self._client(path, remote_id, item_id)
        if participant and _participant(participant)["id"] != client.participant["id"]:
            raise LiveClientError("wrong_participant", "This live session belongs to another participant.")
        return self._submit(client.poll(limit))

    def presence(
        self,
        path: str,
        remote_id: str,
        item_id: str,
        participant: dict[str, Any] | None,
        *,
        cursor: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        viewport: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._client(path, remote_id, item_id)
        if participant and _participant(participant)["id"] != client.participant["id"]:
            raise LiveClientError("wrong_participant", "This live session belongs to another participant.")
        return self._submit(client.presence(cursor=cursor, selection=selection, viewport=viewport))

    def leave(
        self, path: str, remote_id: str, item_id: str, participant: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        key = self._key(path, remote_id, item_id)
        client = self._clients.get(key)
        if client is None:
            return {"ok": True, "left": True}
        if participant and _participant(participant)["id"] != client.participant["id"]:
            raise LiveClientError("wrong_participant", "This live session belongs to another participant.")
        try:
            return self._submit(client.leave())
        finally:
            self._clients.pop(key, None)
            self._local_docs.pop(key, None)

    async def _leave_all(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        self._local_docs.clear()
        if clients:
            await asyncio.gather(*(client.leave() for client in clients), return_exceptions=True)

    def stop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        with self._lock:
            self._stopping = True
        if thread.is_alive():
            future = asyncio.run_coroutine_threadsafe(self._leave_all(), loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
        if thread.is_alive():
            return  # Keep ownership while cancelled writers still actually live.
        self._loop = None
        self._thread = None
        self._started.clear()


_MANAGER: LiveClientManager | None = None
_MANAGER_LOCK = threading.Lock()


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    with registry_lock(_MANAGER_LOCK):
        manager = _MANAGER
    # Registered receivers can write without another worker RPC. Count queued
    # submissions until the coroutine really ends, even if its caller timed out.
    if manager is None:
        return {"by_kind": {"plm_live": 0}}
    with registry_lock(manager._lock):
        active = (len(manager._clients) + manager._submissions
                  + int(manager._stopping and manager.running))
    return {"by_kind": {"plm_live": active}}


def get_live_manager() -> LiveClientManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = LiveClientManager()
        return _MANAGER


def stop_live_manager() -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        manager = _MANAGER
    if manager is not None:
        manager.stop()
        if not manager.running:
            with _MANAGER_LOCK:
                if _MANAGER is manager:
                    _MANAGER = None


atexit.register(stop_live_manager)
