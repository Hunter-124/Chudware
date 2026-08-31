"""Persistent Chudcode RPC sessions for the long-lived agent worker.

The dependency import and executable lookup deliberately happen on first use.  Importing
``services.agent`` must remain possible in offline and partially provisioned runtimes.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import mimetypes
import os
import shutil
import sys
import re
import threading
import uuid
from urllib.parse import urlsplit
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_MAX_SESSIONS = 8
_MAX_MAX_SESSIONS = 32
_MAX_OPEN_URL_CHARS = 16_384
_MAX_UI_TEXT_CHARS = 4_096
_MAX_DOCTOR_OUTPUT_CHARS = 2_097_152
_MAX_DOCTOR_FINDINGS = 128
_MAX_DOCTOR_MESSAGE_CHARS = 4_096

_LOG = logging.getLogger(__name__)

_AUTH_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "get_auth_state": (frozenset(), frozenset({"providerId"})),
    "validate_api_key": (frozenset({"providerId", "apiKey"}), frozenset()),
    "set_api_key": (frozenset({"providerId", "apiKey"}), frozenset()),
    "remove_api_key": (frozenset({"providerId"}), frozenset({"accountId"})),
    "logout_provider": (frozenset({"providerId"}), frozenset({"accountId"})),
}
_MEMORY_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "memory_remember": (
        frozenset({"root", "content"}),
        frozenset({"kind", "source", "importance"}),
    ),
    "memory_recall": (
        frozenset({"root", "query"}),
        frozenset({"limit", "tokenBudget", "kind"}),
    ),
    "memory_stats": (frozenset({"root"}), frozenset()),
}
_TRANSIENT_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    **_AUTH_SHAPES,
    **_MEMORY_SHAPES,
    "get_login_providers": (frozenset(), frozenset()),
    "get_available_models": (frozenset(), frozenset()),
    "login": (frozenset({"providerId"}), frozenset()),
}
_SESSION_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "get_session_stats": (frozenset(), frozenset()),
    "compact": (frozenset(), frozenset({"customInstructions"})),
    "handoff": (frozenset(), frozenset({"customInstructions"})),
    "set_auto_compaction": (frozenset({"enabled"}), frozenset()),
    "set_auto_retry": (frozenset({"enabled"}), frozenset()),
    "get_state": (frozenset(), frozenset()),
    "get_messages": (frozenset(), frozenset()),
    "export_html": (frozenset(), frozenset({"outputPath"})),
    "branch": (frozenset({"entryId"}), frozenset()),
    "stt_status": (frozenset(), frozenset()),
    "stt_start": (frozenset(), frozenset()),
    "stt_stop": (frozenset(), frozenset()),
    "stt_cancel": (frozenset(), frozenset()),
}


@dataclass
class _Session:
    session_id: str
    client: Any
    resume_file: str | None
    workspace_dir: str | None
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    active_turns: int = 0
    closed: bool = False
    host_tool_names: set[str] = field(default_factory=set)


@dataclass
class _Run:
    run_id: str
    session_id: str
    client: Any | None = None
    started: bool = False
    cancel_requested: bool = False
    cancel_sent: bool = False
    steers: set[str] = field(default_factory=set)
    pending_steers: list[str] = field(default_factory=list)


class ChudcodeSessionHost:
    """Own one persistent ``RpcClient`` process per Chudware conversation.

    ``client_factory`` is intentionally injectable.  Production leaves it unset and gets
    the pinned ``chudcode_rpc.RpcClient`` lazily; focused tests can supply a process-free
    fake accepting the same keyword arguments.
    """

    def __init__(self, client_factory=None, binary=None):
        self._client_factory = client_factory
        self._binary_hint = os.fspath(binary) if binary is not None else None
        self._max_sessions = _session_limit_from_env()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._runs: dict[str, _Run] = {}
        self._completed_runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._transient_clients: dict[int, Any] = {}
        self._shutdown = False

    def run_turn(self, params, emit) -> dict:
        """Schedule a prompt and block until a terminal ``agent_end`` is observed."""
        if not isinstance(params, Mapping):
            return {"ok": False, "reason": "chudcode turn params must be an object"}
        if not callable(emit):
            return {"ok": False, "reason": "chudcode turn emit callback is required"}

        prompt = params.get("request", params.get("prompt", params.get("message")))
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "reason": "chudcode turn prompt is empty"}
        session_id = _nonempty_string(params.get("session_id")) or "default"
        run_id = _nonempty_string(params.get("run_id")) or uuid.uuid4().hex
        resume_file = _nonempty_string(params.get("chudcode_session_file"))

        with self._lock:
            if self._shutdown:
                return {"ok": False, "reason": "chudcode session host is shut down"}
            if run_id in self._runs:
                return {
                    "ok": False,
                    "run_id": run_id,
                    "reason": "chudcode run_id is already active",
                }
            self._completed_runs.pop(run_id, None)
            run = _Run(run_id=run_id, session_id=session_id)
            self._runs[run_id] = run

        session: _Session | None = None
        unsubscribe: Callable[[], None] | None = None
        forwarded_ids: set[int] = set()
        emit_errors: list[str] = []
        frames: list[dict[str, Any]] = []
        try:
            session = self._get_or_create_session(session_id, params, resume_file)
            with session.turn_lock:
                with self._lock:
                    if session.closed:
                        raise RuntimeError("chudcode RPC session was closed")
                    run.client = session.client
                    self._sessions.move_to_end(session_id)

                def forward(event: Any) -> None:
                    # A few test doubles dispatch the same object to both generic listener
                    # families.  The real client uses on_notification, but identity dedupe
                    # keeps injected adapters honest without suppressing distinct frames.
                    marker = id(event)
                    if marker in forwarded_ids:
                        return
                    forwarded_ids.add(marker)
                    frame = _json_value(event)
                    if not isinstance(frame, dict):
                        frame = {"type": "unknown", "value": frame}
                    frames.append(frame)
                    try:
                        emit(frame)
                    except Exception as exc:  # noqa: BLE001 - emit is an injected boundary
                        emit_errors.append(str(exc))

                unsubscribe = _register_listener(session.client, forward)
                self._configure_turn(session, params)
                message, images = _prompt_payload(
                    prompt, params.get("attachments"), params.get("images")
                )

                with self._lock:
                    if run.cancel_requested:
                        result = {
                            "ok": True,
                            "run_id": run_id,
                            "session_id": session_id,
                            "cancelled": True,
                            "terminal": None,
                        }
                        return result
                    session.client.prompt(message, images=images or None)
                    run.started = True
                    pending_steers = tuple(run.pending_steers)
                    run.pending_steers.clear()

                for text in pending_steers:
                    session.client.steer(text)
                timeout = params.get("timeout")
                session.client.wait_for_idle(
                    timeout=float(timeout) if timeout is not None else None
                )

                terminal = next(
                    (
                        frame
                        for frame in reversed(frames)
                        if frame.get("type") == "agent_end"
                        and frame.get("isTerminal", frame.get("is_terminal"))
                        is not False
                    ),
                    None,
                )
                if terminal is None:
                    raise RuntimeError(
                        "chudcode RPC turn ended without a terminal agent_end"
                    )

                state = _safe_client_state(session.client)
                result = {
                    "ok": not emit_errors,
                    "run_id": run_id,
                    "session_id": session_id,
                    "cancelled": run.cancel_requested,
                    "terminal": terminal,
                    "result": _safe_last_assistant_text(session.client),
                }
                if state:
                    result["chudcode_session_id"] = state.get("sessionId") or state.get(
                        "session_id"
                    )
                    result["chudcode_session_file"] = state.get(
                        "sessionFile"
                    ) or state.get("session_file")
                if emit_errors:
                    result["reason"] = (
                        f"chudcode event forwarding failed: {emit_errors[0]}"
                    )
                return result
        except Exception as exc:  # noqa: BLE001 - worker RPC boundary returns failures
            return {
                "ok": False,
                "run_id": run_id,
                "session_id": session_id,
                "reason": str(exc),
            }
        finally:
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception as exc:  # noqa: BLE001 - teardown must continue
                    _LOG.warning(
                        "Chudcode event unsubscription failed (%s)",
                        type(exc).__name__,
                    )
            with self._lock:
                if session is not None and session.active_turns:
                    session.active_turns -= 1
                self._runs.pop(run_id, None)
                completed = {
                    "run_id": run_id,
                    "session_id": session_id,
                    "cancelled": run.cancel_requested,
                    "started": run.started,
                }
                self._completed_runs[run_id] = completed
                while len(self._completed_runs) > self._max_sessions * 8:
                    self._completed_runs.popitem(last=False)

    def cancel(self, run_id) -> dict:
        run_key = _nonempty_string(run_id)
        if run_key is None:
            return {"ok": False, "reason": "run_id is required"}
        with self._lock:
            run = self._runs.get(run_key)
            if run is None:
                completed = self._completed_runs.get(run_key)
                return {
                    "ok": True,
                    "run_id": run_key,
                    "cancelled": bool(completed and completed.get("cancelled")),
                    "active": False,
                }
            if run.cancel_requested:
                return {
                    "ok": True,
                    "run_id": run_key,
                    "cancelled": True,
                    "active": True,
                }
            run.cancel_requested = True
            client = run.client
            should_abort = client is not None and run.started and not run.cancel_sent
            if should_abort:
                run.cancel_sent = True
        if should_abort:
            try:
                client.abort()
            except RuntimeError as exc:
                return {
                    "ok": False,
                    "run_id": run_key,
                    "cancelled": True,
                    "active": True,
                    "reason": str(exc),
                }
        return {"ok": True, "run_id": run_key, "cancelled": True, "active": True}

    def steer(self, run_id, text) -> dict:
        run_key = _nonempty_string(run_id)
        message = _nonempty_string(text)
        if run_key is None or message is None:
            return {
                "ok": False,
                "reason": "run_id and non-empty steering text are required",
            }
        with self._lock:
            run = self._runs.get(run_key)
            if run is None:
                return {
                    "ok": True,
                    "run_id": run_key,
                    "steered": False,
                    "active": False,
                }
            if message in run.steers:
                return {
                    "ok": True,
                    "run_id": run_key,
                    "steered": True,
                    "active": True,
                    "duplicate": True,
                }
            run.steers.add(message)
            client = run.client
            send_now = client is not None and run.started and not run.cancel_requested
            if not send_now:
                run.pending_steers.append(message)
        if send_now:
            try:
                client.steer(message)
            except RuntimeError as exc:
                return {
                    "ok": False,
                    "run_id": run_key,
                    "steered": False,
                    "active": True,
                    "reason": str(exc),
                }
        return {
            "ok": True,
            "run_id": run_key,
            "steered": True,
            "active": True,
            "queued": not send_now,
        }

    def status(self, run_id=None, session_id=None) -> dict:
        """Return a sanitized, mutation-free control-plane snapshot."""
        run_key = _nonempty_string(run_id)
        session_key = _nonempty_string(session_id)
        with self._lock:
            if run_key is not None:
                active = self._runs.get(run_key)
                if active is not None:
                    return {
                        "ok": True,
                        "run_id": run_key,
                        "active": True,
                        "cancelled": active.cancel_requested,
                        "started": active.started,
                        "session_id": active.session_id,
                    }
                completed = self._completed_runs.get(run_key)
                if completed is not None:
                    return {
                        "ok": True,
                        "run_id": run_key,
                        "active": False,
                        "cancelled": bool(completed.get("cancelled")),
                        "started": bool(completed.get("started")),
                        "session_id": completed["session_id"],
                    }
                return {
                    "ok": True,
                    "run_id": run_key,
                    "active": False,
                    "cancelled": False,
                    "started": False,
                }
            if session_key is not None:
                session = self._sessions.get(session_key)
                active_runs = sorted(
                    run.run_id
                    for run in self._runs.values()
                    if run.session_id == session_key
                )
                return {
                    "ok": True,
                    "session_id": session_key,
                    "live_session": session is not None and not session.closed,
                    "active_runs": active_runs,
                }
            return {
                "ok": True,
                "sessions": len(self._sessions),
                "active_runs": len(self._runs),
                "max_sessions": self._max_sessions,
                "shutdown": self._shutdown,
            }

    def close_session(self, session_id) -> dict[str, Any]:
        """Dispose a live process without deleting or rewriting its session file."""
        key = _nonempty_string(session_id)
        if key is None:
            return {"ok": True}
        with self._lock:
            session = self._sessions.pop(key, None)
            if session is None or session.closed:
                return {"ok": True}
            session.closed = True
            runs = [run for run in self._runs.values() if run.session_id == key]
            for run in runs:
                run.cancel_requested = True
        for run in runs:
            if run.client is not None and run.started and not run.cancel_sent:
                run.cancel_sent = True
                try:
                    run.client.abort()
                except Exception as exc:  # noqa: BLE001 - disposal must continue
                    _LOG.warning(
                        "Chudcode abort during session disposal failed (%s)",
                        type(exc).__name__,
                    )
        _stop_client(session.client)
        return {"ok": True}

    def export_session(self, session_id, output_path=None) -> dict[str, Any]:
        """Export one live transcript through Chudcode and atomically publish it."""
        session = self._claim_live_session(session_id)
        if session is None:
            return {"ok": False, "reason": "session_not_live"}
        temporary: Path | None = None
        try:
            with session.turn_lock:
                if session.closed:
                    return {"ok": False, "reason": "session_not_live"}
                try:
                    target = _project_export_path(
                        session.workspace_dir, session.session_id, output_path
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                except (OSError, TypeError, ValueError) as exc:
                    return {"ok": False, "reason": str(exc)}
                temporary = target.parent / (
                    f".{target.name}.{uuid.uuid4().hex}.chudcode-export.tmp"
                )
                try:
                    raw = _invoke_session_canonical(
                        session.client,
                        "export_html",
                        {"outputPath": str(temporary)},
                    )
                    normalized = _json_value(raw)
                    exported_value = (
                        normalized.get("path")
                        if isinstance(normalized, Mapping)
                        else None
                    )
                    exported = (
                        Path(exported_value).expanduser()
                        if isinstance(exported_value, str)
                        else None
                    )
                    if exported is None or not exported.is_file():
                        raise RuntimeError(
                            "Chudcode export_html did not create the requested file"
                        )
                    if (
                        exported.is_symlink()
                        or exported.absolute() != temporary.absolute()
                        or exported.resolve().parent != target.parent.resolve()
                    ):
                        raise RuntimeError(
                            "Chudcode export_html returned an unexpected output path"
                        )
                    os.replace(exported, target)
                    temporary = None
                    return {"ok": True, "path": str(target)}
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    return {"ok": False, "reason": str(exc)}
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            self._release_live_session(session)

    def branch_session(self, session_id, entry_id) -> dict[str, Any]:
        """Create a persisted child, capture its identity, then retire the mutated process."""
        key = _nonempty_string(session_id)
        branch_entry = _nonempty_string(entry_id)
        if key is None:
            return {"ok": False, "reason": "session_not_live"}
        if branch_entry is None:
            return {"ok": False, "reason": "entry_id is required"}
        session = self._claim_live_session(key)
        if session is None:
            return {"ok": False, "reason": "session_not_live"}
        dispose = False
        try:
            with session.turn_lock:
                if session.closed:
                    return {"ok": False, "reason": "session_not_live"}
                raw = _invoke_session_canonical(
                    session.client, "branch", {"entryId": branch_entry}
                )
                dispose = True
                normalized = _json_value(raw)
                if not isinstance(normalized, Mapping):
                    raise TypeError("Chudcode branch returned a non-object response")
                if normalized.get("cancelled"):
                    dispose = False
                    return {"ok": False, "reason": "branch_cancelled"}
                state = _safe_client_state(session.client)
                child_id = _nonempty_string(
                    state.get("sessionId") or state.get("session_id")
                )
                child_file = _nonempty_string(
                    state.get("sessionFile") or state.get("session_file")
                )
                if child_id is None or child_file is None:
                    raise RuntimeError(
                        "Chudcode branch returned no child session metadata"
                    )
                return {
                    "ok": True,
                    "chudcode_session_id": child_id,
                    "chudcode_session_file": child_file,
                }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {"ok": False, "reason": str(exc)}
        finally:
            self._release_live_session(session)
            if dispose:
                self.close_session(key)

    def repair_session(self, session_id, repair=False) -> dict[str, Any]:
        """Run Chudcode's local-only session doctor in an existing RPC process."""
        if not isinstance(repair, bool):
            return {"ok": False, "reason": "repair must be a boolean"}
        session = self._claim_live_session(session_id)
        if session is None:
            return {"ok": False, "reason": "session_not_live"}
        try:
            with session.turn_lock:
                if session.closed:
                    return {"ok": False, "reason": "session_not_live"}
                try:
                    summary = _run_session_doctor(session.client, repair)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    return {"ok": False, "reason": _sanitize_doctor_text(str(exc))}
                state = _safe_client_state(session.client)
                child_id = _nonempty_string(
                    state.get("sessionId") or state.get("session_id")
                )
                child_file = _nonempty_string(
                    state.get("sessionFile") or state.get("session_file")
                )
                if child_id is not None:
                    summary["chudcode_session_id"] = child_id[:256]
                if child_file is not None:
                    summary["chudcode_session_file"] = child_file[:4_096]
                return summary
        finally:
            self._release_live_session(session)

    def shutdown(self):
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            sessions = tuple(self._sessions.values())
            transient_clients = tuple(self._transient_clients.values())
            self._sessions.clear()
            self._transient_clients.clear()
            for session in sessions:
                session.closed = True
            for run in self._runs.values():
                run.cancel_requested = True
        for session in sessions:
            _stop_client(session.client)
        for client in transient_clients:
            _stop_client(client)

    def transient_call(self, command, params=None, emit=None) -> dict[str, Any]:
        """Run a bounded service-level command without creating a saved session."""
        command_name = _nonempty_string(command)
        if command_name not in _TRANSIENT_SHAPES:
            raise ValueError(
                f"unsupported Chudcode transient command: {command_name!r}"
            )
        payload = _validated_payload(
            command_name,
            params if params is not None else {},
            _TRANSIENT_SHAPES[command_name],
        )
        _validate_service_payload(command_name, payload)
        if emit is not None and not callable(emit):
            raise TypeError("Chudcode transient emit callback must be callable")
        return self._transient_call(command_name, payload, emit=emit)

    def auth_call(self, command, params):
        command_name = _nonempty_string(command)
        if command_name not in _AUTH_SHAPES:
            raise ValueError(f"unsupported Chudcode auth command: {command_name!r}")
        return self.transient_call(command_name, params)

    def memory_call(self, command, params):
        command_name = _nonempty_string(command)
        if command_name not in _MEMORY_SHAPES:
            raise ValueError(f"unsupported Chudcode memory command: {command_name!r}")
        return self.transient_call(command_name, params)

    def session_call(self, session_id, command, params=None) -> dict[str, Any]:
        """Call one named live session; these calls never create or resume sessions."""
        command_name = _nonempty_string(command)
        if command_name not in _SESSION_SHAPES:
            raise ValueError(f"unsupported Chudcode session command: {command_name!r}")
        payload = _validated_payload(
            command_name,
            params if params is not None else {},
            _SESSION_SHAPES[command_name],
        )
        if command_name in {"set_auto_compaction", "set_auto_retry"} and not isinstance(
            payload.get("enabled"), bool
        ):
            raise TypeError(f"{command_name} enabled must be a boolean")
        if (
            command_name in {"compact", "handoff"}
            and "customInstructions" in payload
            and not isinstance(payload["customInstructions"], str)
        ):
            raise TypeError(f"{command_name} customInstructions must be a string")
        if command_name == "export_html" and (
            "outputPath" in payload and _nonempty_string(payload["outputPath"]) is None
        ):
            raise TypeError("export_html outputPath must be a non-empty string")
        if (
            command_name == "branch"
            and _nonempty_string(payload.get("entryId")) is None
        ):
            raise TypeError("branch entryId must be a non-empty string")
        if command_name == "export_html":
            return self.export_session(session_id, payload.get("outputPath"))
        if command_name == "branch":
            return self.branch_session(session_id, payload["entryId"])

        session = self._claim_live_session(session_id)
        if session is None:
            return {"ok": False, "reason": "session_not_live"}
        try:
            with session.turn_lock:
                if session.closed:
                    return {"ok": False, "reason": "session_not_live"}
                result = _invoke_session_canonical(
                    session.client, command_name, payload
                )
                normalized = _json_value(result)
                if normalized is None:
                    normalized = {"ok": True}
                if not isinstance(normalized, dict):
                    raise TypeError(
                        f"Chudcode {command_name} returned a non-object response"
                    )
                normalized.setdefault("ok", True)
                if command_name in {"set_auto_compaction", "set_auto_retry"}:
                    normalized.setdefault("enabled", payload["enabled"])
                if command_name == "get_state":
                    normalized["_hostToolNames"] = sorted(
                        _session_host_tool_names(session)
                    )
                return normalized
        finally:
            self._release_live_session(session)

    def _claim_live_session(self, session_id) -> _Session | None:
        key = _nonempty_string(session_id)
        if key is None:
            return None
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session.closed:
                return None
            session.active_turns += 1
            self._sessions.move_to_end(key)
            return session

    def _release_live_session(self, session: _Session) -> None:
        with self._lock:
            if session.active_turns:
                session.active_turns -= 1

    def _get_or_create_session(
        self, session_id: str, params: Mapping[str, Any], resume_file: str | None
    ) -> _Session:
        workspace = _nonempty_string(params.get("workspace_dir"))
        workspace_dir = (
            str(Path(workspace).expanduser().resolve())
            if workspace is not None
            else None
        )
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                if (
                    resume_file
                    and existing.resume_file
                    and resume_file != existing.resume_file
                ):
                    raise RuntimeError(
                        "cannot change chudcode_session_file on a live Chudware session"
                    )
                if existing.workspace_dir is None and workspace_dir is not None:
                    existing.workspace_dir = workspace_dir
                self._sessions.move_to_end(session_id)
                existing.active_turns += 1
                return existing

            self._evict_for_new_session()
            client = self._make_client(
                params, no_session=False, resume_file=resume_file
            )
            try:
                _start_client(client)
                if resume_file:
                    resume_path = Path(resume_file).expanduser()
                    if not resume_path.is_file():
                        raise RuntimeError(
                            f"persisted Chudcode session file does not exist: {resume_path}"
                        )
                    client.switch_session(str(resume_path))
            except (OSError, RuntimeError, ValueError):
                _stop_client(client)
                raise
            session = _Session(
                session_id,
                client,
                resume_file,
                workspace_dir,
                active_turns=1,
            )
            self._sessions[session_id] = session
            return session

    def _evict_for_new_session(self) -> None:
        while len(self._sessions) >= self._max_sessions:
            victim_key = next(
                (
                    key
                    for key, value in self._sessions.items()
                    if value.active_turns == 0 and not value.closed
                ),
                None,
            )
            if victim_key is None:
                raise RuntimeError(
                    "all bounded Chudcode RPC sessions are currently active"
                )
            victim = self._sessions.pop(victim_key)
            victim.closed = True
            _stop_client(victim.client)

    def _make_client(
        self,
        params: Mapping[str, Any],
        *,
        no_session: bool,
        resume_file: str | None = None,
    ) -> Any:
        factory = self._client_factory
        if factory is None:
            try:
                from chudcode_rpc import RpcClient  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "Chudcode RPC support is unavailable; install the managed "
                    "chudcode-rpc==0.2.0 runtime dependency"
                ) from exc
            factory = RpcClient

        binary = self._resolve_binary(required=self._client_factory is None)
        session_dir = _nonempty_string(params.get("chudcode_session_dir"))
        if session_dir is None and resume_file:
            session_dir = str(Path(resume_file).expanduser().parent)
        kwargs: dict[str, Any] = {
            "no_session": no_session,
            "rpc_defaults": True,
        }
        if binary is not None:
            kwargs["executable"] = binary
        if session_dir is not None:
            kwargs["session_dir"] = session_dir
        workspace = _nonempty_string(params.get("workspace_dir"))
        if workspace is not None:
            kwargs["cwd"] = workspace
        system_prompt = _nonempty_string(params.get("system_prompt"))
        if system_prompt is not None:
            kwargs["append_system_prompt"] = system_prompt
        return factory(**kwargs)

    def _resolve_binary(self, *, required: bool) -> str | None:
        if self._binary_hint:
            resolved, error = _binary_candidate("explicit binary", self._binary_hint)
            if error:
                raise RuntimeError(error)
            return resolved

        configured = _nonempty_string(os.environ.get("CHUDWARE_CHUDCODE_BIN"))
        if configured:
            resolved, error = _binary_candidate("CHUDWARE_CHUDCODE_BIN", configured)
            if error:
                raise RuntimeError(error)
            return resolved

        executable_name = "chud.exe" if os.name == "nt" else "chud"
        adjacent = Path(sys.executable).resolve().parent / executable_name
        resolved, _error = _binary_candidate("signed payload", str(adjacent))
        if resolved:
            return resolved

        if _developer_mode():
            found = shutil.which(executable_name) or shutil.which("chud")
            if found:
                resolved, _error = _binary_candidate("developer PATH", found)
                if resolved:
                    return resolved

        if not required:
            return None
        raise RuntimeError(
            "Chudcode executable not found; set CHUDWARE_CHUDCODE_BIN, install the "
            "signed-payload-adjacent chud binary, or put chud on PATH in developer mode"
        )

    def _configure_turn(self, session: _Session, params: Mapping[str, Any]) -> None:
        client = session.client
        settings_value = params.get("settings")
        settings = dict(settings_value) if isinstance(settings_value, Mapping) else {}
        for key in (
            "provider",
            "model",
            "thinking",
            "reasoning_effort",
            "steering_mode",
            "follow_up_mode",
            "interrupt_mode",
            "auto_compaction",
            "auto_retry",
            "subagent_subscription",
        ):
            if key in params:
                settings[key] = params[key]
        queue_value = settings.get("queue")
        if isinstance(queue_value, Mapping):
            settings.setdefault("steering_mode", queue_value.get("steering"))
            settings.setdefault("follow_up_mode", queue_value.get("follow_up"))
            settings.setdefault("interrupt_mode", queue_value.get("interrupt"))

        provider = _nonempty_string(settings.get("provider"))
        model = _nonempty_string(settings.get("model"))
        if model and not provider and "/" in model:
            provider, model = model.split("/", 1)
        if provider and model:
            client.set_model(provider, model)
        thinking = _nonempty_string(settings.get("thinking")) or _nonempty_string(
            settings.get("reasoning_effort")
        )
        if thinking and thinking != "auto":
            client.set_thinking_level(thinking)
        steering = _nonempty_string(settings.get("steering_mode"))
        if steering:
            client.set_steering_mode(steering)
        follow_up = _nonempty_string(settings.get("follow_up_mode"))
        if follow_up:
            client.set_follow_up_mode(follow_up)
        interrupt = _nonempty_string(settings.get("interrupt_mode"))
        if interrupt:
            client.set_interrupt_mode(interrupt)
        if isinstance(settings.get("auto_compaction"), bool):
            client.set_auto_compaction(settings["auto_compaction"])
        if isinstance(settings.get("auto_retry"), bool):
            client.set_auto_retry(settings["auto_retry"])
        subscription = _nonempty_string(settings.get("subagent_subscription"))
        if subscription:
            client.request_raw("set_subagent_subscription", level=subscription)

        host_tools = params.get("host_tools")
        if isinstance(host_tools, Sequence) and not isinstance(
            host_tools, (str, bytes)
        ):
            session.host_tool_names = {
                name
                for name in (
                    _nonempty_string(getattr(binding, "name", None))
                    for binding in host_tools
                )
                if name is not None
            }
            converted_tools = []
            for binding in host_tools:
                converter = getattr(binding, "to_host_tool", None)
                if callable(converter):
                    try:
                        from chudcode_rpc import host_tool  # type: ignore
                    except ImportError as exc:
                        raise RuntimeError(
                            "chudcode-rpc==0.2.0 is required to register CAD host tools"
                        ) from exc
                    binding = converter(host_tool)
                converted_tools.append(binding)
            client.set_custom_tools(converted_tools)
        host_uris = params.get("host_uris")
        if isinstance(host_uris, Sequence) and not isinstance(host_uris, (str, bytes)):
            client.set_host_uris(host_uris)
        registrar = params.get("register_callbacks")
        if callable(registrar):
            registrar(client)

    def _transient_call(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("chudcode session host is shut down")
        client = self._make_client({}, no_session=True)
        marker = id(client)
        with self._lock:
            if self._shutdown:
                _stop_client(client)
                raise RuntimeError("chudcode session host is shut down")
            self._transient_clients[marker] = client
        unsubscribe: Callable[[], None] | None = None
        try:
            _start_client(client)
            if command == "login":
                unsubscribe = _register_login_ui(client, emit)
            result = _invoke_canonical(client, command, payload)
            normalized = _json_value(result)
            if not isinstance(normalized, dict):
                raise TypeError(f"Chudcode {command} returned a non-object response")
            return normalized
        finally:
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception as exc:  # noqa: BLE001 - teardown must continue
                    _LOG.debug(
                        "Chudcode login UI teardown failed (%s)", type(exc).__name__
                    )
            with self._lock:
                owned = self._transient_clients.pop(marker, None) is not None
            if owned:
                _stop_client(client)


def _project_export_path(
    workspace_dir: str | None, session_id: str, output_path: Any
) -> Path:
    if workspace_dir is None:
        raise ValueError("project_root_unavailable")
    root = Path(workspace_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("project_root_unavailable")
    if output_path is None:
        basename = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip(".-")
        requested = root / "chat-exports" / f"{basename or 'session'}.html"
    else:
        if not isinstance(output_path, (str, os.PathLike)):
            raise TypeError("output_path must be a path string")
        requested = Path(output_path).expanduser()
        if not requested.is_absolute():
            requested = root / requested
    target = requested.resolve()
    if target == root:
        raise ValueError("export path must name a file inside the project")
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("export path must stay inside the project") from exc
    return target


_DOCTOR_STATUSES = frozenset({"healthy", "issues", "repaired", "repaired_with_issues"})
_DOCTOR_COUNT_KEYS = {
    "malformedRecords": "malformed_records",
    "malformedEntries": "malformed_entries",
    "duplicateEntryIds": "duplicate_entry_ids",
    "orphanedEntries": "orphaned_entries",
    "malformedToolCalls": "malformed_tool_calls",
    "orphanedToolCalls": "orphaned_tool_calls",
    "orphanedToolResults": "orphaned_tool_results",
    "malformedImages": "malformed_images",
    "malformedBlobRefs": "malformed_blob_refs",
    "missingBlobs": "missing_blobs",
    "invalidCompactions": "invalid_compactions",
    "truncatedCompactionImages": "truncated_compaction_images",
}
_DOCTOR_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[\s_-]*key|access[\s_-]*token|refresh[\s_-]*token|"
    r"control[\s_-]*token|password|secret)\b(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_DOCTOR_TOKEN_LITERAL = re.compile(
    r"(?i)\b(?:sk|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{8,}\b"
)


def _sanitize_doctor_text(value: Any, limit: int = _MAX_DOCTOR_MESSAGE_CHARS) -> str:
    text = value if isinstance(value, str) else str(value)
    text = _DOCTOR_SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", text)
    text = _DOCTOR_TOKEN_LITERAL.sub("<redacted>", text)
    return text[:limit]


def _bounded_doctor_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        count = int(value)
    except (OverflowError, ValueError):
        return 0
    return min(1_000_000, max(0, count))


def _sanitize_doctor_summary(raw: Mapping[str, Any], *, repair: bool) -> dict[str, Any]:
    status = _nonempty_string(raw.get("status"))
    if status not in _DOCTOR_STATUSES:
        raise ValueError("Chudcode session doctor returned an invalid status")
    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("Chudcode session doctor returned invalid findings")
    findings: list[dict[str, Any]] = []
    for value in raw_findings[:_MAX_DOCTOR_FINDINGS]:
        if not isinstance(value, Mapping):
            continue
        code = _bounded_string(value.get("code"), 64)
        message = _bounded_string(value.get("message"), _MAX_DOCTOR_MESSAGE_CHARS)
        if code is None or message is None:
            continue
        finding: dict[str, Any] = {
            "code": re.sub(r"[^a-z0-9_]+", "_", code.lower())[:64],
            "message": _sanitize_doctor_text(message),
            "repairable": bool(value.get("repairable")),
        }
        entry_id = _bounded_string(value.get("entryId"), 256)
        if entry_id is not None:
            finding["entry_id"] = _sanitize_doctor_text(entry_id, 256)
        finding_path = _bounded_string(value.get("path"), 1_024)
        if finding_path is not None:
            finding["path"] = _sanitize_doctor_text(finding_path, 1_024)
        findings.append(finding)

    raw_counts = raw.get("counts")
    counts_source = raw_counts if isinstance(raw_counts, Mapping) else {}
    counts = {
        output_key: _bounded_doctor_count(counts_source.get(source_key))
        for source_key, output_key in _DOCTOR_COUNT_KEYS.items()
    }
    finding_count = _bounded_doctor_count(raw.get("findingCount"))
    finding_count = max(finding_count, len(raw_findings))
    result: dict[str, Any] = {
        "ok": True,
        "mode": "repair" if repair else "dry-run",
        "status": status,
        "changed": bool(raw.get("changed")),
        "finding_count": min(1_000_000, finding_count),
        "repairable_finding_count": _bounded_doctor_count(
            raw.get("repairableFindingCount")
        ),
        "remaining_finding_count": _bounded_doctor_count(
            raw.get("remainingFindingCount")
        ),
        "counts": counts,
        "findings": findings,
        "backup_path": _bounded_string(raw.get("backupPath"), 4_096),
    }
    session_file = _bounded_string(raw.get("sessionFile"), 4_096)
    if session_file is not None:
        result["chudcode_session_file"] = session_file
    return result


def _run_session_doctor(client: Any, repair: bool) -> dict[str, Any]:
    outputs: list[str] = []
    prompt_results: list[dict[str, Any]] = []
    completed = threading.Event()
    retained_chars = 0

    def capture(event: Any) -> None:
        nonlocal retained_chars
        frame = _json_value(event)
        if not isinstance(frame, Mapping):
            return
        frame_type = frame.get("type")
        if frame_type == "command_output":
            text = frame.get("text")
            if isinstance(text, str) and retained_chars < _MAX_DOCTOR_OUTPUT_CHARS:
                retained = text[: _MAX_DOCTOR_OUTPUT_CHARS - retained_chars]
                outputs.append(retained)
                retained_chars += len(retained)
        elif frame_type == "prompt_result":
            prompt_results.append(dict(frame))
            completed.set()

    unsubscribe = _register_listener(client, capture)
    try:
        command = "/session doctor --repair" if repair else "/session doctor"
        response = client.request_raw("prompt", message=command)
        normalized_response = _json_value(response)
        response_invoked = (
            normalized_response.get("agentInvoked")
            if isinstance(normalized_response, Mapping)
            else None
        )
        if response_invoked is True:
            raise RuntimeError("Chudcode session doctor unexpectedly invoked a model")
        if response_invoked is not False and not prompt_results:
            completed.wait(5.0)
        if prompt_results and prompt_results[-1].get("agentInvoked") is not False:
            raise RuntimeError("Chudcode session doctor unexpectedly invoked a model")
        if response_invoked is not False and not prompt_results:
            raise RuntimeError(
                "Chudcode session doctor did not report local completion"
            )
    finally:
        try:
            unsubscribe()
        except Exception as exc:  # noqa: BLE001 - doctor completion already decided
            _LOG.debug(
                "Chudcode doctor listener teardown failed (%s)", type(exc).__name__
            )

    for text in reversed(outputs):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            return _sanitize_doctor_summary(value, repair=repair)
    raise RuntimeError("Chudcode session doctor returned no valid summary")


def _invoke_canonical(client: Any, command: str, payload: Mapping[str, Any]) -> Any:
    """Use secret-safe typed wrappers when available; fakes may expose raw RPC only."""
    method = getattr(client, command, None)
    if callable(method):
        if command == "get_login_providers":
            providers = method()
            return (
                providers
                if isinstance(providers, Mapping)
                else {"providers": providers}
            )
        if command == "get_available_models":
            return {"models": method()}
        if command == "login":
            return method(payload["providerId"])
        if command == "get_auth_state":
            return method(payload.get("providerId"))
        if command in {"validate_api_key", "set_api_key"}:
            return method(payload["providerId"], payload["apiKey"])
        if command in {"remove_api_key", "logout_provider"}:
            return method(payload["providerId"], payload.get("accountId"))
        if command == "memory_remember":
            return method(
                payload["root"],
                payload["content"],
                kind=payload.get("kind"),
                source=payload.get("source"),
                importance=payload.get("importance"),
            )
        if command == "memory_recall":
            return method(
                payload["root"],
                payload["query"],
                limit=payload.get("limit"),
                token_budget=payload.get("tokenBudget"),
                kind=payload.get("kind"),
            )
        if command == "memory_stats":
            return method(payload["root"])
    return client.request_raw(command, **payload)


def _invoke_session_canonical(
    client: Any, command: str, payload: Mapping[str, Any]
) -> Any:
    method = getattr(client, command, None)
    if callable(method):
        if command in {
            "get_session_stats",
            "get_state",
            "get_messages",
            "stt_status",
            "stt_start",
            "stt_stop",
            "stt_cancel",
        }:
            result = method()
            return {"messages": result} if command == "get_messages" else result
        if command in {"compact", "handoff"}:
            return method(payload.get("customInstructions"))
        if command in {"set_auto_compaction", "set_auto_retry"}:
            method(payload["enabled"])
            return {"ok": True, "enabled": payload["enabled"]}
        if command == "export_html":
            return {"path": str(method(payload.get("outputPath")))}
        if command == "branch":
            return method(payload["entryId"])
    return client.request_raw(command, **payload)


def _session_host_tool_names(session: _Session) -> set[str]:
    names = set(session.host_tool_names)
    for tool in getattr(session.client, "_custom_tools", ()):
        name = _nonempty_string(getattr(tool, "name", None))
        if name is not None:
            names.add(name)
    return names


def _register_login_ui(
    client: Any, emit: Callable[[dict[str, Any]], None] | None
) -> Callable[[], None]:
    register = getattr(client, "on_ui_request", None)
    if not callable(register):
        return lambda: None

    def handle(request: Any) -> None:
        frame = _json_value(request)
        if not isinstance(frame, Mapping):
            return
        method = _nonempty_string(frame.get("method"))
        if method == "open_url":
            event = _safe_open_url_event(frame)
            if event is not None and emit is not None:
                try:
                    emit(event)
                except Exception as exc:  # noqa: BLE001 - UI forwarding is advisory
                    _LOG.debug(
                        "Chudcode login UI forwarding failed (%s)", type(exc).__name__
                    )
            return
        request_id = _nonempty_string(frame.get("id"))
        if method in {"select", "confirm", "input", "editor"} and request_id:
            cancel = getattr(client, "cancel_ui_request", None)
            if callable(cancel):
                cancel(request_id)

    unsubscribe = register(handle)
    return unsubscribe if callable(unsubscribe) else (lambda: None)


def _safe_open_url_event(frame: Mapping[str, Any]) -> dict[str, Any] | None:
    url = _safe_web_url(frame.get("url"))
    if url is None:
        return None
    event: dict[str, Any] = {
        "type": "extension_ui_request",
        "method": "open_url",
        "kind": "open_url",
        "url": url,
    }
    launch_url = _safe_web_url(frame.get("launchUrl") or frame.get("launch_url"))
    if launch_url is not None:
        event["launchUrl"] = launch_url
    request_id = _bounded_string(frame.get("id"), 256)
    if request_id is not None:
        event["id"] = request_id
    instructions = _bounded_string(frame.get("instructions"), _MAX_UI_TEXT_CHARS)
    if instructions is not None:
        event["instructions"] = instructions
        event["message"] = instructions
    return event


def _safe_web_url(value: Any) -> str | None:
    text = _bounded_string(value, _MAX_OPEN_URL_CHARS)
    if text is None:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return text


def _bounded_string(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _session_limit_from_env() -> int:
    raw = os.environ.get("CHUDWARE_CHUDCODE_MAX_SESSIONS", "")
    try:
        value = int(raw) if raw else _DEFAULT_MAX_SESSIONS
    except ValueError:
        value = _DEFAULT_MAX_SESSIONS
    return max(1, min(value, _MAX_MAX_SESSIONS))


def _developer_mode() -> bool:
    explicit = os.environ.get("CHUDWARE_DEVELOPER_BUILD", "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    return (Path(__file__).resolve().parents[2] / ".git").exists()


def _binary_candidate(source: str, candidate: str) -> tuple[str | None, str | None]:
    path = Path(candidate).expanduser()
    if not path.is_file() or (os.name != "nt" and not os.access(path, os.X_OK)):
        return None, f"{source} is not an executable file: {path}"
    if _looks_like_source_script(path):
        return (
            None,
            f"{source} points to a source/Bun script, not a compiled chud binary: {path}",
        )
    return str(path.resolve()), None


def _looks_like_source_script(path: Path) -> bool:
    if path.suffix.lower() in {".js", ".mjs", ".cjs", ".ts", ".tsx"}:
        return True
    try:
        prefix = path.read_bytes()[:256].lower()
    except OSError:
        return True
    return prefix.startswith(b"#!") and b"bun" in prefix.splitlines()[0]


def _register_listener(
    client: Any, listener: Callable[[Any], None]
) -> Callable[[], None]:
    register = getattr(client, "on_notification", None)
    if not callable(register):
        register = getattr(client, "on_event", None)
    if not callable(register):
        raise TypeError("chudcode RpcClient does not expose an event listener API")
    unsubscribe = register(listener)
    return unsubscribe if callable(unsubscribe) else (lambda: None)


def _stop_client(client: Any) -> None:
    stop = getattr(client, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception as exc:  # noqa: BLE001 - teardown must remain idempotent
            _LOG.warning(
                "Chudcode RPC client teardown failed (%s)",
                type(exc).__name__,
            )


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _start_client(client: Any) -> None:
    client.start()
    protocol_version = getattr(
        client, "protocol_version", getattr(client, "_protocol_version", None)
    )
    if protocol_version is not None and protocol_version != 2:
        raise RuntimeError(
            f"Chudcode RPC protocol v2 is required (runtime negotiated v{protocol_version})"
        )


def _validated_payload(
    command: str,
    params: Any,
    shape: tuple[frozenset[str], frozenset[str]],
) -> dict[str, Any]:
    if not isinstance(params, Mapping):
        raise TypeError(f"{command} params must be an object")
    required, optional = shape
    keys = set(params)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ValueError(
            f"{command} missing required params: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ValueError(
            f"{command} has unknown params: {', '.join(sorted(map(str, unknown)))}"
        )
    return {str(key): value for key, value in params.items() if value is not None}


def _validate_service_payload(command: str, payload: Mapping[str, Any]) -> None:
    if "providerId" in payload and _nonempty_string(payload["providerId"]) is None:
        raise TypeError(f"{command} providerId must be a non-empty string")
    if "apiKey" in payload and _nonempty_string(payload["apiKey"]) is None:
        raise TypeError(f"{command} apiKey must be a non-empty string")
    if "accountId" in payload and _nonempty_string(payload["accountId"]) is None:
        raise TypeError(f"{command} accountId must be a non-empty string")


def _prompt_payload(
    prompt: str, attachments_value: Any, images_value: Any
) -> tuple[str, list[dict[str, Any]]]:
    images: list[dict[str, Any]] = []
    file_refs: list[str] = []
    if isinstance(images_value, Sequence) and not isinstance(
        images_value, (str, bytes)
    ):
        for image in images_value:
            if isinstance(image, Mapping):
                images.append(dict(image))
    attachments = (
        attachments_value
        if isinstance(attachments_value, Sequence)
        and not isinstance(attachments_value, (str, bytes))
        else ()
    )
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        if "data" in attachment:
            images.append(
                {
                    "type": "image",
                    "mimeType": attachment.get("mimeType")
                    or attachment.get("mime")
                    or "application/octet-stream",
                    "data": attachment["data"],
                }
            )
            continue
        path_value = _nonempty_string(attachment.get("path"))
        kind = _nonempty_string(attachment.get("kind"))
        mime = _nonempty_string(attachment.get("mime")) or _nonempty_string(
            attachment.get("mimeType")
        )
        if path_value and (kind == "image" or (mime and mime.startswith("image/"))):
            path = Path(path_value).expanduser()
            images.append(
                {
                    "type": "image",
                    "mimeType": mime
                    or mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            )
        elif path_value:
            name = _nonempty_string(attachment.get("name")) or Path(path_value).name
            file_refs.append(f"- {name}: {path_value}")
    if file_refs:
        prompt = (
            prompt.rstrip() + "\n\nAttached workspace files:\n" + "\n".join(file_refs)
        )
    return prompt, images


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(piece[:1].upper() + piece[1:] for piece in tail)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if getattr(value, "type", None) == "unknown" and isinstance(
            getattr(value, "payload", None), Mapping
        ):
            return _json_value(value.payload)
        return {
            _camel(field_info.name): _json_value(getattr(value, field_info.name))
            for field_info in dataclasses.fields(value)
        }
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return str(value)


def _safe_client_state(client: Any) -> dict[str, Any]:
    getter = getattr(client, "get_state", None)
    if not callable(getter):
        return {}
    try:
        state = _json_value(getter())
        return state if isinstance(state, dict) else {}
    except RuntimeError as exc:
        _LOG.debug(
            "Chudcode get_state failed (%s)",
            type(exc).__name__,
        )
        return {}


def _safe_last_assistant_text(client: Any) -> str | None:
    getter = getattr(client, "get_last_assistant_text", None)
    if not callable(getter):
        return None
    try:
        value = getter()
        return value if isinstance(value, str) else None
    except RuntimeError as exc:
        _LOG.debug(
            "Chudcode get_last_assistant_text failed (%s)",
            type(exc).__name__,
        )
        return None


_DEFAULT_CHUDCODE_HOST: ChudcodeSessionHost | None = None
_DEFAULT_CHUDCODE_HOST_LOCK = threading.Lock()


def get_default_chudcode_host() -> ChudcodeSessionHost:
    """Return the worker process's single lazily-created Chudcode session pool."""
    global _DEFAULT_CHUDCODE_HOST
    host = _DEFAULT_CHUDCODE_HOST
    if host is not None:
        return host
    with _DEFAULT_CHUDCODE_HOST_LOCK:
        if _DEFAULT_CHUDCODE_HOST is None:
            _DEFAULT_CHUDCODE_HOST = ChudcodeSessionHost()
        return _DEFAULT_CHUDCODE_HOST


def _set_default_chudcode_host_for_tests(
    host: ChudcodeSessionHost | None,
) -> ChudcodeSessionHost | None:
    """Inject or clear the default host only from process-isolated tests."""
    global _DEFAULT_CHUDCODE_HOST
    with _DEFAULT_CHUDCODE_HOST_LOCK:
        previous = _DEFAULT_CHUDCODE_HOST
        _DEFAULT_CHUDCODE_HOST = host
        return previous
