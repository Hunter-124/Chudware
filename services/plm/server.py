"""Authoritative WebSocket relay for ``aicad.plm-live/1`` sessions."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import inspect
import json
import os
import re
import signal
import stat
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from services.plm import manifest as manifest_mod
from services.plm.vault import Vault, VaultError

SCHEMA = "aicad.plm-live/1"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_OPS_BYTES = 1024 * 1024
MAX_OPS = 1000
MAX_SEQ = (1 << 64) - 1
_RETAINED_COMMITS = 256
_RETAINED_BYTES = 64 * 1024 * 1024
_PRESENCE_INTERVAL = 1.0 / 20.0
_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ProtocolFault(Exception):
    """A plain-language protocol refusal safe to return to a peer."""

    def __init__(self, reason: str, hint: str, *, fatal: bool = True) -> None:
        super().__init__(hint)
        self.reason = reason
        self.hint = hint
        self.fatal = fatal


class EngineRejected(Exception):
    """A typed, sanitized semantic rejection from a session engine."""

    def __init__(self, reason: str, hint: str) -> None:
        super().__init__(hint)
        self.reason = reason if _REASON_RE.fullmatch(reason) else "semantic_rejection"
        self.hint = hint


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(document),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_state(document: Mapping[str, Any], revision: Any) -> dict[str, Any]:
    raw = _canonical_bytes(document)
    try:
        rev = int(revision)
    except (TypeError, ValueError) as exc:
        raise EngineRejected("engine_unavailable", "The document engine returned an invalid revision.") from exc
    if rev < 0:
        raise EngineRejected("engine_unavailable", "The document engine returned an invalid revision.")
    return {
        "document": json.loads(raw),
        "engine_revision": rev,
        "state_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ProtocolFault("invalid_json", "The message contains a value that is not valid JSON.", fatal=False) from exc


def _message_text(message: Mapping[str, Any]) -> str:
    raw = json.dumps(
        message,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolFault("message_too_large", "The server response exceeds the protocol message limit.")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ProtocolFault("invalid_json", "The message contains a duplicate object field.")
        out[key] = value
    return out


def _reject_constant(_value: str) -> None:
    raise ProtocolFault("invalid_json", "The message contains a non-finite number.")


def _decode_message(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ProtocolFault("invalid_message", "Binary WebSocket messages are not supported.")
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolFault("message_too_large", "The message exceeds the 4 MiB limit.")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except ProtocolFault:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProtocolFault("invalid_json", "Each WebSocket message must be one UTF-8 JSON object.") from exc
    if not isinstance(value, dict):
        raise ProtocolFault("invalid_message", "Each WebSocket message must be one JSON object.")
    if value.get("schema") != SCHEMA:
        raise ProtocolFault("invalid_schema", f"Messages must use schema {SCHEMA}.")
    if not isinstance(value.get("type"), str):
        raise ProtocolFault("invalid_type", "The message type must be a string.")
    return value


def _expect_keys(message: Mapping[str, Any], required: set[str], optional: set[str] = set()) -> None:
    missing = required - message.keys()
    if missing:
        raise ProtocolFault("invalid_message", "The message is missing required fields.")
    if message.keys() - required - optional:
        raise ProtocolFault("invalid_message", "The message contains unsupported fields.")


def _bounded_text(value: Any, label: str, *, limit: int = 128, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not _ID_RE.fullmatch(value):
        raise ProtocolFault("invalid_identity", f"{label} must be a non-empty plain string.")
    return value


def _uint64(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SEQ:
        raise ProtocolFault("invalid_sequence", f"{label} must be an unsigned 64-bit integer.")
    return value


def _identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolFault("invalid_identity", "Participant identity must be an object.")
    if value.keys() - {"id", "display", "account_id"} or not {"id", "display"} <= value.keys():
        raise ProtocolFault("invalid_identity", "Participant identity has invalid fields.")
    participant_id = _bounded_text(value.get("id"), "Participant id")
    display = _bounded_text(value.get("display"), "Participant display", limit=256)
    account_id = value.get("account_id")
    if account_id is not None:
        account_id = _bounded_text(account_id, "Participant account id", limit=256, nullable=True)
    return {"id": participant_id, "display": display, "account_id": account_id}




class ProductionEngine:
    """Thin async adapter over the existing core bridge document RPCs."""

    def __init__(self, item_path: Path) -> None:
        self.item_path = item_path
        self.doc_id: str | None = None
        self.revision = 0

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from services.mcp import core_bridge
        return await asyncio.to_thread(core_bridge.call, method, params)

    async def open(self) -> dict[str, Any]:
        try:
            opened = await self._call("doc_open", {"path": str(self.item_path)})
            doc_id = opened.get("doc_id")
            if not isinstance(doc_id, str) or not doc_id:
                raise EngineRejected("engine_unavailable", "The document engine could not open this item.")
            self.doc_id = doc_id
            return await self.snapshot()
        except EngineRejected:
            raise
        except Exception as exc:
            raise EngineRejected("engine_unavailable", "The document engine could not open this item.") from exc

    async def snapshot(self) -> dict[str, Any]:
        if self.doc_id is None:
            raise EngineRejected("engine_unavailable", "The document engine is not open.")
        try:
            result = await self._call("doc_tree", {"doc_id": self.doc_id})
            document = result.get("tree")
            if not isinstance(document, dict):
                raise EngineRejected("engine_unavailable", "The document engine returned no canonical document.")
            state = _canonical_state(document, result.get("revision", self.revision))
            self.revision = state["engine_revision"]
            return state
        except EngineRejected:
            raise
        except Exception as exc:
            raise EngineRejected("engine_unavailable", "The document engine could not read this item.") from exc

    async def apply_ops(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        if self.doc_id is None:
            raise EngineRejected("engine_unavailable", "The document engine is not open.")
        try:
            await self._call("doc_apply_ops", {
                "doc_id": self.doc_id,
                "base_revision": self.revision,
                "with_tess": False,
                "ops": ops,
            })
            state = await self.snapshot()
            await self.save()
            return state
        except EngineRejected:
            raise
        except Exception as exc:
            from services.mcp.core_bridge import CoreError
            if isinstance(exc, CoreError):
                code = str(exc.code or "")
                reason = code if _REASON_RE.fullmatch(code) else "semantic_rejection"
                hints = {
                    "stale_reference": "A referenced feature no longer exists; refresh and choose it again.",
                    "invalid_op": "The operation batch is not valid for the current document.",
                    "invalid_ops": "The operation batch is not valid for the current document.",
                    "validation_failed": "The operation batch is not valid for the current document.",
                    "revision_conflict": "The document changed before the batch could be applied; catch up and retry.",
                }
                raise EngineRejected(reason, hints.get(reason, "The document engine rejected this operation batch.")) from exc
            raise EngineRejected("engine_unavailable", "The document engine could not apply this operation batch.") from exc

    async def save(self) -> None:
        if self.doc_id is None:
            return
        try:
            await self._call("doc_save", {"doc_id": self.doc_id, "path": str(self.item_path)})
        except Exception as exc:
            raise EngineRejected("save_failed", "The authoritative item could not be saved.") from exc

    async def close(self) -> None:
        if self.doc_id is None:
            return
        doc_id, self.doc_id = self.doc_id, None
        with contextlib.suppress(Exception):
            await self._call("doc_close", {"doc_id": doc_id})


@dataclass
class _Session:
    key: tuple[str, str]
    session_id: str
    item_path: Path
    checkpoint_base: str
    engine: Any
    state: dict[str, Any]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seq: int = 0
    commits: deque[dict[str, Any]] = field(default_factory=deque)
    retained_bytes: int = 0
    dedupe: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    sockets: dict[str, Any] = field(default_factory=dict)
    identities: dict[str, dict[str, Any]] = field(default_factory=dict)
    committers: dict[str, dict[str, Any]] = field(default_factory=dict)
    presence: dict[str, dict[str, Any]] = field(default_factory=dict)
    presence_last_sent: dict[str, float] = field(default_factory=dict)
    presence_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    joined_once: bool = False
    committed_batches: int = 0
    session_note: str = ""
    ending: bool = False
    finalized: bool = False

    @property
    def initial_hash(self) -> str:
        if self.commits and self.commits[0]["seq"] == 1:
            return self.commits[0].get("prior_state_hash", "")
        return self.state["state_hash"] if self.seq == 0 else ""


class LivePlmServer:
    """One-project, deny-by-default authoritative live collaboration relay."""

    def __init__(
        self,
        project_dir: str | os.PathLike[str],
        token: str | bytes | None,
        engine_factory: Callable[[Path], Any] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        if not self.project_dir.is_dir():
            raise ValueError("project must be an existing folder")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty address")
        if token is None:
            token_bytes = b""
        elif isinstance(token, str):
            token_bytes = token.encode("utf-8")
        elif isinstance(token, bytes):
            token_bytes = bytes(token)
        else:
            raise TypeError("token must be text, bytes, or None")
        if len(token_bytes) > 4096:
            raise ValueError("token is too large")
        if not token_bytes or not token_bytes.strip():
            raise ValueError("a nonempty bearer token is required for every listener")
        self._token = token_bytes
        # Native desktop clients send no Origin; browser origins must be
        # explicitly configured, never inferred from loopback or wildcarded.
        for origin in allowed_origins:
            if not isinstance(origin, str):
                raise ValueError("allowed origins must be explicit HTTP(S) origins")
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or "*" in origin):
                raise ValueError("allowed origins must be explicit HTTP(S) origins")
            _ = parsed.port
        self._origins = (None, *allowed_origins)
        self._engine_factory = engine_factory or ProductionEngine
        self.host = host
        self.port = port
        self.url = ""
        self._websocket_server: Any = None
        self._vault: Vault | None = None
        self._vault_lock = asyncio.Lock()
        self._sessions_lock = asyncio.Lock()
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._stopping = False

    async def start(self) -> None:
        if self._websocket_server is not None:
            return
        if self._stopping:
            raise RuntimeError("server has stopped")
        from websockets.asyncio.server import serve

        self._vault = await asyncio.to_thread(Vault.open, self.project_dir)
        try:
            self._websocket_server = await serve(
                self._handle_socket,
                self.host,
                self.port,
                compression=None,
                origins=self._origins,
                max_size=MAX_MESSAGE_BYTES,
                max_queue=16,
                ping_interval=20,
                ping_timeout=60,
            )
        except Exception:
            self._vault.close()
            self._vault = None
            raise
        sockets = self._websocket_server.sockets or []
        if not sockets:
            await self.stop()
            raise RuntimeError("listener did not expose a socket")
        bound_port = int(sockets[0].getsockname()[1])
        url_host = self.host
        if ":" in url_host and not url_host.startswith("["):
            url_host = f"[{url_host}]"
        self.url = f"ws://{url_host}:{bound_port}"

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        server, self._websocket_server = self._websocket_server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        async with self._sessions_lock:
            remaining = list(self._sessions.values())
        for session in remaining:
            await self._finalize_if_empty(session, force=True)
        if self._vault is not None:
            await asyncio.to_thread(self._vault.close)
            self._vault = None
        self.url = ""

    async def _send(self, websocket: Any, message: Mapping[str, Any]) -> None:
        await websocket.send(_message_text(message))

    async def _send_error(self, websocket: Any, fault: ProtocolFault) -> None:
        with contextlib.suppress(Exception):
            await self._send(websocket, {
                "schema": SCHEMA,
                "type": "error",
                "reason": fault.reason,
                "hint": fault.hint,
            })
        if fault.fatal:
            with contextlib.suppress(Exception):
                await websocket.close(4400, fault.reason[:80])

    def _authorized(self, supplied: Any) -> bool:
        raw = supplied.encode("utf-8") if isinstance(supplied, str) else b""
        return hmac.compare_digest(self._token, raw)

    async def _handle_socket(self, websocket: Any) -> None:
        session: _Session | None = None
        participant_id: str | None = None
        try:
            raw = await websocket.recv()
            message = _decode_message(raw)
            if message.get("type") != "join":
                raise ProtocolFault("join_required", "The first message on a connection must be a join.")
            supplied_token = message.pop("token", None)
            if not self._authorized(supplied_token):
                await websocket.close(4401, "unauthorized")
                return
            session, participant, replaced = await self._join(websocket, message)
            participant_id = participant["id"]
            if replaced is not None and replaced is not websocket:
                with contextlib.suppress(Exception):
                    await replaced.close(4001, "reconnected")

            async for raw in websocket:
                try:
                    message = _decode_message(raw)
                    kind = message.get("type")
                    if kind == "propose":
                        await self._propose(session, participant, websocket, message)
                    elif kind == "catchup":
                        await self._catchup(session, participant_id, websocket, message)
                    elif kind == "presence":
                        await self._presence(session, participant, websocket, message)
                    elif kind == "leave":
                        self._validate_leave(session, message)
                        note = message.get("session_note")
                        if isinstance(note, str) and note.strip():
                            async with session.lock:
                                session.session_note = note.strip()
                        await websocket.close(1000, "leave")
                        break
                    else:
                        raise ProtocolFault("invalid_type", "This message type is not supported.")
                except ProtocolFault as fault:
                    await self._send_error(websocket, fault)
                    if fault.fatal:
                        break
        except ProtocolFault as fault:
            await self._send_error(websocket, fault)
        except Exception as exc:
            from websockets.exceptions import ConnectionClosed
            if not isinstance(exc, ConnectionClosed):
                await self._send_error(websocket, ProtocolFault(
                    "server_unavailable", "The live session could not process this connection."
                ))
        finally:
            if session is not None and participant_id is not None:
                await self._disconnect(session, participant_id, websocket)

    async def _resolve_item(self, project_id: str, item_id: str) -> tuple[Path, str]:
        if self._vault is None:
            raise ProtocolFault("server_unavailable", "The project vault is not available.")
        async with self._vault_lock:
            manifest = await asyncio.to_thread(self._vault.manifest)
            checkpoint = self._vault._bind.head_commit() or ""  # vault commit is the protocol base
        if manifest.get("project", {}).get("id") != project_id:
            raise ProtocolFault("unknown_project", "The requested project is not hosted by this server.")
        item = manifest_mod.find_item(manifest, item_id)
        if item is None:
            raise ProtocolFault("unknown_item", "The requested item is not registered in this project.")
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ProtocolFault("invalid_item_path", "The registered item path is invalid.")
        try:
            target = (self.project_dir / relative).resolve(strict=True)
            target.relative_to(self.project_dir)
        except (OSError, ValueError):
            raise ProtocolFault("invalid_item_path", "The registered item is outside this project or unavailable.") from None
        if not target.is_file() or not target.name.endswith((".step+", ".ecad+")):
            raise ProtocolFault("unsupported_item", "Live editing requires a registered design container.")
        return target, checkpoint

    async def _new_engine(self, item_path: Path) -> tuple[Any, dict[str, Any]]:
        try:
            engine = self._engine_factory(item_path)
            if inspect.isawaitable(engine):
                engine = await engine
            opened = engine.open()
            state = await opened if inspect.isawaitable(opened) else opened
            if not isinstance(state, dict) or not isinstance(state.get("document"), dict):
                snap = engine.snapshot()
                state = await snap if inspect.isawaitable(snap) else snap
            if not isinstance(state, dict) or not isinstance(state.get("document"), dict):
                raise EngineRejected("engine_unavailable", "The document engine returned no canonical document.")
            normalized = _canonical_state(state["document"], state.get("engine_revision", state.get("revision", 0)))
            return engine, normalized
        except EngineRejected:
            raise
        except Exception as exc:
            raise EngineRejected("engine_unavailable", "The document engine could not open this item.") from exc

    async def _get_session(self, project_id: str, item_id: str, item_path: Path, checkpoint: str) -> _Session:
        key = (project_id, item_id)
        while True:
            async with self._sessions_lock:
                existing = self._sessions.get(key)
                if existing is None:
                    try:
                        engine, state = await self._new_engine(item_path)
                    except EngineRejected as exc:
                        raise ProtocolFault(exc.reason, exc.hint) from exc
                    session = _Session(
                        key=key,
                        session_id="s" + uuid.uuid4().hex,
                        item_path=item_path,
                        checkpoint_base=checkpoint,
                        engine=engine,
                        state=state,
                    )
                    self._sessions[key] = session
                    return session
            async with existing.lock:
                ending = existing.ending or existing.finalized
            if not ending:
                return existing
            await asyncio.sleep(0)

    async def _join(self, websocket: Any, message: dict[str, Any]) -> tuple[_Session, dict[str, Any], Any]:
        _expect_keys(
            message,
            {"schema", "type", "project_id", "item_id", "participant"},
            {"last_seq", "session_note"},
        )
        project_id = _bounded_text(message.get("project_id"), "Project id")
        item_id = _bounded_text(message.get("item_id"), "Item id")
        participant = _identity(message.get("participant"))
        last_seq = message.get("last_seq")
        if last_seq is not None:
            last_seq = _uint64(last_seq, "last_seq")
        note = message.get("session_note")
        if note is not None and (not isinstance(note, str) or len(note) > 1024):
            raise ProtocolFault("invalid_message", "A session note must be plain text no longer than 1024 characters.")
        item_path, checkpoint = await self._resolve_item(project_id, item_id)
        session = await self._get_session(project_id, item_id, item_path, checkpoint)
        replaced = None
        async with session.lock:
            if session.ending or session.finalized:
                raise ProtocolFault("session_ending", "The prior live session is being saved; reconnect shortly.")
            if last_seq is not None and last_seq > session.seq:
                raise ProtocolFault("invalid_sequence", "last_seq is ahead of the authoritative session.")
            first_join = not session.joined_once
            current_identities = dict(session.identities)
            current_identities[participant["id"]] = participant
            joined = {
                "schema": SCHEMA,
                "type": "joined",
                "session_id": session.session_id,
                "project_id": project_id,
                "item_id": item_id,
                "head_seq": session.seq,
                "engine_revision": session.state["engine_revision"],
                "participants": list(current_identities.values()),
                "checkpoint_base": session.checkpoint_base,
            }
            retained_from = session.commits[0]["seq"] if session.commits else session.seq + 1
            needs_snapshot = first_join or last_seq is None or last_seq < retained_from - 1
            if needs_snapshot:
                joined["snapshot"] = {
                    "encoding": "canonical-json",
                    "document": session.state["document"],
                }
            await self._send(websocket, joined)
            if not needs_snapshot and last_seq is not None:
                for commit in session.commits:
                    if commit["seq"] > last_seq:
                        await self._send(
                            websocket,
                            {k: v for k, v in commit.items() if k != "prior_state_hash"},
                        )
            replaced = session.sockets.get(participant["id"])
            session.sockets[participant["id"]] = websocket
            session.identities[participant["id"]] = participant
            session.joined_once = True
            if isinstance(note, str) and note.strip():
                session.session_note = note.strip()
        return session, participant, replaced

    async def _broadcast_locked(self, session: _Session, message: Mapping[str, Any]) -> None:
        text = _message_text(message)
        for websocket in list(session.sockets.values()):
            with contextlib.suppress(Exception):
                await websocket.send(text)

    def _validate_session_message(
        self,
        session: _Session,
        message: Mapping[str, Any],
        required: set[str],
        optional: set[str] = set(),
    ) -> None:
        _expect_keys(message, {"schema", "type", "session_id"} | required, optional)
        if message.get("session_id") != session.session_id:
            raise ProtocolFault("unknown_session", "The session id does not match this connection.")

    async def _propose(
        self,
        session: _Session,
        participant: dict[str, Any],
        websocket: Any,
        message: dict[str, Any],
    ) -> None:
        self._validate_session_message(session, message, {"client_op_id", "base_seq", "ops"})
        client_op_id = _bounded_text(message.get("client_op_id"), "client_op_id")
        _uint64(message.get("base_seq"), "base_seq")
        ops = message.get("ops")
        if not isinstance(ops, list) or not ops or any(not isinstance(op, dict) for op in ops):
            await self._input_rejection(
                session, participant["id"], websocket, client_op_id,
                "invalid_ops", "ops must be a non-empty array of operation objects.")
            return
        if len(ops) > MAX_OPS:
            await self._input_rejection(
                session, participant["id"], websocket, client_op_id,
                "batch_too_large", "An operation batch can contain at most 1,000 operations.")
            return
        try:
            ops_size = _json_size(ops)
        except ProtocolFault as fault:
            await self._input_rejection(
                session, participant["id"], websocket, client_op_id, fault.reason, fault.hint)
            return
        if ops_size > MAX_OPS_BYTES:
            await self._input_rejection(
                session, participant["id"], websocket, client_op_id,
                "batch_too_large", "The operation batch exceeds the 1 MiB limit.")
            return
        key = (participant["id"], client_op_id)
        async with session.lock:
            if session.sockets.get(participant["id"]) is not websocket:
                raise ProtocolFault("connection_replaced", "This connection was replaced by a reconnect.")
            prior = session.dedupe.get(key)
            if prior is not None:
                await self._send(websocket, prior)
                return
            if session.seq == MAX_SEQ:
                rejected = self._rejection(session, client_op_id, "sequence_exhausted", "This session reached its sequence limit.")
                session.dedupe[key] = rejected
                await self._send(websocket, rejected)
                return
            prior_hash = session.state["state_hash"]
            try:
                result = session.engine.apply_ops(ops)
                state = await result if inspect.isawaitable(result) else result
                if not isinstance(state, dict) or not isinstance(state.get("document"), dict):
                    raise EngineRejected("engine_unavailable", "The document engine returned no canonical document.")
                state = _canonical_state(
                    state["document"],
                    state.get("engine_revision", state.get("revision", session.state["engine_revision"] + 1)),
                )
            except EngineRejected as exc:
                rejected = self._rejection(session, client_op_id, exc.reason, exc.hint)
                session.dedupe[key] = rejected
                await self._send(websocket, rejected)
                return
            except Exception:
                rejected = self._rejection(
                    session,
                    client_op_id,
                    "engine_unavailable",
                    "The document engine could not apply this operation batch.",
                )
                session.dedupe[key] = rejected
                await self._send(websocket, rejected)
                return
            seq = session.seq + 1
            commit = {
                "schema": SCHEMA,
                "type": "commit",
                "session_id": session.session_id,
                "seq": seq,
                "client_op_id": client_op_id,
                "author": participant,
                "engine_revision": state["engine_revision"],
                "ops": ops,
                "snapshot": {"encoding": "canonical-json", "document": state["document"]},
                "state_hash": state["state_hash"],
            }
            encoded_size = len(_message_text(commit).encode("utf-8"))
            retained = dict(commit)
            retained["prior_state_hash"] = prior_hash
            session.seq = seq
            session.committed_batches += 1
            session.state = state
            session.dedupe[key] = commit
            session.committers[participant["id"]] = participant
            session.commits.append(retained)
            session.retained_bytes += encoded_size
            while len(session.commits) > _RETAINED_COMMITS or session.retained_bytes > _RETAINED_BYTES:
                old = session.commits.popleft()
                session.retained_bytes -= len(_message_text({k: v for k, v in old.items() if k != "prior_state_hash"}).encode("utf-8"))
            await self._broadcast_locked(session, commit)

    async def _input_rejection(
        self,
        session: _Session,
        participant_id: str,
        websocket: Any,
        client_op_id: str,
        reason: str,
        hint: str,
    ) -> None:
        key = (participant_id, client_op_id)
        async with session.lock:
            prior = session.dedupe.get(key)
            if prior is None:
                prior = self._rejection(session, client_op_id, reason, hint)
                session.dedupe[key] = prior
            await self._send(websocket, prior)

    @staticmethod
    def _rejection(session: _Session, client_op_id: str, reason: str, hint: str) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "type": "rejected",
            "session_id": session.session_id,
            "client_op_id": client_op_id,
            "head_seq": session.seq,
            "reason": reason if _REASON_RE.fullmatch(reason) else "invalid_ops",
            "hint": hint,
        }

    async def _catchup(
        self,
        session: _Session,
        participant_id: str,
        websocket: Any,
        message: dict[str, Any],
    ) -> None:
        self._validate_session_message(session, message, {"from_seq"}, {"state_hash"})
        from_seq = _uint64(message.get("from_seq"), "from_seq")
        supplied_hash = message.get("state_hash")
        if supplied_hash is not None and (not isinstance(supplied_hash, str) or len(supplied_hash) > 80):
            raise ProtocolFault("invalid_message", "state_hash must be a short string.")
        async with session.lock:
            if session.sockets.get(participant_id) is not websocket:
                raise ProtocolFault("connection_replaced", "This connection was replaced by a reconnect.")
            retained_from = session.commits[0]["seq"] if session.commits else session.seq + 1
            expected_hash = None
            if from_seq == session.seq:
                expected_hash = session.state["state_hash"]
            elif from_seq == 0 and session.commits and session.commits[0]["seq"] == 1:
                expected_hash = session.commits[0].get("prior_state_hash")
            else:
                for commit in session.commits:
                    if commit["seq"] == from_seq:
                        expected_hash = commit["state_hash"]
                        break
            unavailable = from_seq > session.seq or from_seq < retained_from - 1
            disagrees = supplied_hash is not None and expected_hash != supplied_hash
            if unavailable or disagrees:
                await self._send(websocket, self._snapshot_message(session))
                return
            for commit in session.commits:
                if commit["seq"] > from_seq:
                    await self._send(websocket, {k: v for k, v in commit.items() if k != "prior_state_hash"})

    @staticmethod
    def _snapshot_message(session: _Session) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "type": "snapshot",
            "session_id": session.session_id,
            "head_seq": session.seq,
            "engine_revision": session.state["engine_revision"],
            "snapshot": {"encoding": "canonical-json", "document": session.state["document"]},
            "state_hash": session.state["state_hash"],
        }

    async def _presence(
        self,
        session: _Session,
        participant: dict[str, Any],
        websocket: Any,
        message: dict[str, Any],
    ) -> None:
        required = {"schema", "type", "session_id"}
        if not required <= message.keys() or message.get("session_id") != session.session_id:
            raise ProtocolFault("unknown_session", "The session id does not match this connection.")
        if any(key in message for key in ("participant", "participant_id", "token")):
            raise ProtocolFault("invalid_presence", "Presence cannot replace authenticated identity.", fatal=False)
        payload = {
            "schema": SCHEMA,
            "type": "presence",
            "session_id": session.session_id,
            "participant": participant,
        }
        for key, value in message.items():
            if key in required:
                continue
            if _json_size(value) > 64 * 1024:
                raise ProtocolFault("invalid_presence", "Presence fields must be small JSON values.", fatal=False)
            payload[key] = value
        if _json_size(payload) > 64 * 1024:
            raise ProtocolFault("invalid_presence", "The presence update is too large.", fatal=False)
        participant_id = participant["id"]
        loop = asyncio.get_running_loop()
        async with session.lock:
            if session.sockets.get(participant_id) is not websocket:
                raise ProtocolFault("connection_replaced", "This connection was replaced by a reconnect.")
            session.presence[participant_id] = payload
            elapsed = loop.time() - session.presence_last_sent.get(participant_id, -1e9)
            if elapsed >= _PRESENCE_INTERVAL:
                session.presence_last_sent[participant_id] = loop.time()
                await self._broadcast_locked(session, payload)
                return
            task = session.presence_tasks.get(participant_id)
            if task is None or task.done():
                session.presence_tasks[participant_id] = asyncio.create_task(
                    self._flush_presence(session, participant_id, _PRESENCE_INTERVAL - elapsed)
                )

    async def _flush_presence(self, session: _Session, participant_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            async with session.lock:
                payload = session.presence.get(participant_id)
                if payload is None or participant_id not in session.sockets:
                    return
                session.presence_last_sent[participant_id] = asyncio.get_running_loop().time()
                await self._broadcast_locked(session, payload)
        except asyncio.CancelledError:
            pass

    def _validate_leave(self, session: _Session, message: dict[str, Any]) -> None:
        self._validate_session_message(session, message, set(), {"session_note"})
        note = message.get("session_note")
        if note is not None and (not isinstance(note, str) or len(note) > 1024):
            raise ProtocolFault("invalid_message", "A session note must be plain text no longer than 1024 characters.")

    async def _disconnect(self, session: _Session, participant_id: str, websocket: Any) -> None:
        should_finalize = False
        async with session.lock:
            if session.sockets.get(participant_id) is not websocket:
                return
            session.sockets.pop(participant_id, None)
            session.identities.pop(participant_id, None)
            session.presence.pop(participant_id, None)
            session.presence_last_sent.pop(participant_id, None)
            task = session.presence_tasks.pop(participant_id, None)
            if task is not None:
                task.cancel()
            if session.sockets:
                await self._broadcast_locked(session, {
                    "schema": SCHEMA,
                    "type": "presence_left",
                    "session_id": session.session_id,
                    "participant_id": participant_id,
                })
            elif not session.ending and not session.finalized:
                session.ending = True
                should_finalize = True
        if should_finalize:
            await self._finalize(session)

    async def _finalize_if_empty(self, session: _Session, *, force: bool = False) -> None:
        sockets: list[Any] = []
        async with session.lock:
            if session.finalized:
                return
            if session.sockets and not force:
                return
            if session.sockets:
                sockets = list(session.sockets.values())
                session.sockets.clear()
                session.identities.clear()
            if session.ending:
                return
            session.ending = True
        for websocket in sockets:
            with contextlib.suppress(Exception):
                await websocket.close(1001, "server stopping")
        await self._finalize(session)

    async def _finalize(self, session: _Session) -> None:
        try:
            saver = session.engine.save()
            if inspect.isawaitable(saver):
                await saver
            if session.committed_batches and self._vault is not None:
                participants = list(session.committers.values())
                message = session.session_note or f"Live session: {session.committed_batches} committed batch(es)"
                primary = participants[0]
                author = {
                    "kind": "user",
                    "id": primary["id"],
                    "display": primary["display"],
                    "account_id": primary.get("account_id"),
                }
                async with self._vault_lock:
                    await asyncio.to_thread(
                        self._vault.checkpoint,
                        message,
                        author=author,
                        item_id=session.key[1],
                        participants=participants,
                    )
        except (EngineRejected, VaultError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                closer = session.engine.close()
                if inspect.isawaitable(closer):
                    await closer
            async with session.lock:
                session.finalized = True
                session.ending = False
            async with self._sessions_lock:
                if self._sessions.get(session.key) is session:
                    self._sessions.pop(session.key, None)


def _parse_listen(value: str) -> tuple[str, int]:
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or end + 1 >= len(value) or value[end + 1] != ":":
            raise ValueError("listen must be HOST:PORT")
        host, port_text = value[1:end], value[end + 2:]
    else:
        if ":" not in value:
            raise ValueError("listen must be HOST:PORT")
        host, port_text = value.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("listen port must be an integer") from exc
    if not host or not 0 <= port <= 65535:
        raise ValueError("listen must contain a host and port between 0 and 65535")
    return host, port


def _read_token_file(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError("token file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        raise ValueError("token file must be a small regular file")
    if info.st_mode & 0o177:
        raise ValueError("token file permissions must be 0600 or stricter")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("token file could not be read as UTF-8") from exc
    if not token:
        raise ValueError("token file is empty")
    return token


async def _run_cli(args: argparse.Namespace) -> None:
    host, port = _parse_listen(args.listen)
    token = _read_token_file(Path(args.token_file)) if args.token_file else os.environ.get("CHUDWARE_PLM_LIVE_TOKEN")
    server = LivePlmServer(args.project, token, host=host, port=port,
                           allowed_origins=tuple(args.origin))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)
        except NotImplementedError:
            pass
    try:
        await server.start()
        print(f"PLM live relay listening at {server.url}", flush=True)
        await stop_event.wait()
    finally:
        await server.stop()
        for sig in installed:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the aicad.plm-live/1 authoritative relay")
    parser.add_argument("--project", required=True, help="Vault project folder")
    parser.add_argument("--listen", default="127.0.0.1:8765", help="HOST:PORT (default: 127.0.0.1:8765)")
    parser.add_argument("--token-file", help="0600 UTF-8 bearer-token file")
    parser.add_argument("--origin", action="append", default=[],
                        help="Exact allowed browser Origin; repeat for multiple origins (native clients need none)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run_cli(args))
    except (ValueError, VaultError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
