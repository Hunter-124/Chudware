"""Private, runtime-scoped bridge for the trusted OMP CAD extension.

The OMP client remains responsible for its own RPC protocol and process.  This
channel only carries extension SDK controls and calls back into CAD's registry.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from services.mcp.core_bridge import CoreError

from services.agent.omp_control import ControlServer

_CONTROL_LIMIT = 64 * 1024
_FRAME_LIMIT = 16 * 1024 * 1024
_MAX_PENDING = 128
_MAX_TOOLS = 64
_MAX_PROGRESS = 32
_OPERATIONS = frozenset({"auth_state", "auth_states", "set_api_key", "remove_api_key", "setup_providers", "memory_status", "memory_search", "memory_save", "settings_get", "settings_set", "config_get", "config_list", "config_set", "config_parse", "compact", "model_metadata", "tool_state", "handoff", "cad_tool_loads_get", "cad_tool_loads_put", "cad_tool_loads_legacy", "cad_image_blobs_status", "cad_image_blob_import", "cad_image_context_refs"})
_ACTIONS = frozenset({"bootstrap", "tool_ack", "poll", "is_pending", "model_policy", "result", "tool_start", "tool_poll", "tool_cancel"})


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str = "extension bridge request failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Control:
    operation: str
    arguments: dict
    ready: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str | None = None


class _ToolContext:
    def __init__(self, data: dict) -> None:
        self.tool_call_id = data.get("tool_call_id")
        self.agent_id = data.get("agent_id")
        self.parent_agent_id = data.get("parent_agent_id")
        self.session_id = data.get("session_id")
        self.cancel_event = threading.Event()
        self._connections: set[Any] = set()
        self._updates: deque = deque(maxlen=_MAX_PROGRESS)
        self._lock = threading.Lock()
        self.done = False
        self.result: Any = None
        self.error: str | None = None
        self.error_detail: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def attach_connection(self, connection: Any) -> None:
        with self._lock:
            if self.cancelled:
                connection.abort()
            else:
                self._connections.add(connection)

    def detach_connection(self, connection: Any) -> None:
        with self._lock:
            self._connections.discard(connection)

    def cancel(self) -> None:
        with self._lock:
            self.cancel_event.set()
            for connection in self._connections:
                connection.abort()

    def send_update(self, payload: Any) -> None:
        if _encoded_size(payload) > _CONTROL_LIMIT:
            return
        with self._lock:
            self._updates.append(payload)


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class ExtensionBridge:
    def __init__(self, runtime_dir: str, tool_dispatch: Callable[[str, dict, Any], Any]) -> None:
        self._dispatch_tool = tool_dispatch
        self._server = ControlServer(self._handle, runtime_dir, request_limit=_FRAME_LIMIT,
                                     response_limit=_FRAME_LIMIT, max_connections=32,
                                     accepted_actions=_ACTIONS)
        self._lock = threading.Lock()
        self._commands: dict[str, _Control] = {}
        self._queue: deque[str] = deque()
        self._tools: dict[str, _ToolContext] = {}
        self._definitions: list[dict] = []
        # Presentation belongs to the runtime, not the latest activation
        # descriptor: child factories may bind after a category was enabled.
        self._tool_presentations: dict[str, str] = {}
        # A description is provider-visible prefix. Text derived from mutable state
        # (Settings tool notes/timeouts, the skill catalog) is read once per runtime
        # so a later turn cannot rewrite the tool array and miss the prompt cache;
        # a relaunch composes it afresh. Schemas and enforcement stay live.
        self._tool_descriptions: dict[str, str] = {}
        self._tools_published = False
        self._keyless_providers: frozenset[str] = frozenset()
        self._generation = 0
        self._ack_generation = -1
        self._tools_changed = threading.Condition(self._lock)
        self._stopped = False
        self._tool_error_redactions: tuple[str, ...] = ()

    def start(self) -> "ExtensionBridge":
        self._server.start()
        info = self._server.info()
        self._tool_error_redactions = (info["token"], info["endpoint"])
        return self

    def info(self) -> dict:
        return self._server.info()

    def publish_keyless_providers(self, providers: list[str]) -> None:
        if (not isinstance(providers, list) or len(providers) > 1024 or
                any(not isinstance(item, str) or not item or len(item) > 256 for item in providers) or
                _encoded_size(providers) > _CONTROL_LIMIT):
            raise ValueError("invalid keyless provider policy")
        with self._lock:
            if self._stopped:
                raise BridgeError("stopped")
            self._keyless_providers = frozenset(providers)

    def publish_tools(self, definitions: list[dict]) -> None:
        if not isinstance(definitions, list) or _encoded_size(definitions) > _FRAME_LIMIT // 2:
            raise ValueError("invalid or oversized CAD tool registry")
        names: set[str] = set()
        clean = []
        for item in definitions:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("input_schema"), dict):
                raise ValueError("invalid CAD tool descriptor")
            name = item["name"]
            if not name or name in names:
                raise ValueError("duplicate CAD tool descriptor")
            if item.get("load_mode", "discoverable") not in {"essential", "discoverable"}:
                raise ValueError("invalid CAD tool load mode")
            if type(item.get("active", True)) is not bool:
                raise ValueError("invalid CAD tool activation")
            names.add(name)
            clean.append({"name": name, "description": str(item.get("description", "")),
                          "input_schema": item["input_schema"],
                          "load_mode": item.get("load_mode", "discoverable"),
                          "active": item.get("active", True)})
        with self._tools_changed:
            for item in clean:
                name = item["name"]
                if name not in self._tool_presentations:
                    self._tool_presentations[name] = (
                        "essential" if not self._tools_published and item["active"]
                        and item["load_mode"] == "essential" else "discoverable"
                    )
                item["presentation"] = self._tool_presentations[name]
                item["description"] = self._tool_descriptions.setdefault(name, item["description"])
            self._tools_published = True
            if self._definitions != clean:
                self._definitions = clean
                self._generation += 1
                self._tools_changed.notify_all()

    def wait_tools(self, *, timeout: float) -> None:
        if not 0 < timeout < float("inf"):
            raise ValueError("a finite positive timeout is required")
        deadline = time.monotonic() + timeout
        with self._tools_changed:
            target = self._generation
            while self._ack_generation < target and not self._stopped:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("timeout", "extension tool registry did not refresh")
                self._tools_changed.wait(remaining)
            if self._stopped:
                raise BridgeError("stopped")

    def call(self, operation: str, arguments: dict, *, timeout: float, cancel_event: threading.Event | None = None) -> Any:
        if operation not in _OPERATIONS:
            raise BridgeError("unsupported", "unsupported extension operation")
        if not isinstance(arguments, dict) or _encoded_size(arguments) > _CONTROL_LIMIT:
            raise BridgeError("invalid_request", "invalid extension arguments")
        if timeout is None or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
            raise ValueError("a finite positive timeout is required")
        request_id = secrets.token_hex(16)
        pending = _Control(operation, arguments)
        with self._lock:
            if self._stopped:
                raise BridgeError("stopped")
            if len(self._commands) >= _MAX_PENDING:
                raise BridgeError("busy", "extension control queue is full")
            self._commands[request_id] = pending
            self._queue.append(request_id)
        deadline = time.monotonic() + timeout
        try:
            while not pending.ready.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
                if cancel_event is not None and cancel_event.is_set():
                    raise BridgeError("cancelled", "extension operation cancelled")
                if time.monotonic() >= deadline:
                    raise BridgeError("timeout", "extension operation timed out")
            if pending.error:
                raise BridgeError(pending.error)
            return pending.result
        finally:
            with self._lock:
                self._commands.pop(request_id, None)
                try:
                    self._queue.remove(request_id)
                except ValueError:
                    pass

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._tools_changed.notify_all()
            for pending in self._commands.values():
                pending.error = "stopped"
                pending.ready.set()
            for ctx in self._tools.values():
                ctx.cancel()
            self._queue.clear()
        self._server.stop()

    def _handle(self, action: str, data: dict) -> dict:
        if action == "model_policy":
            provider = data.get("provider")
            if not isinstance(provider, str) or not provider or len(provider) > 256:
                return {"error": "invalid_request"}
            with self._lock:
                return {"allowed": not self._stopped and provider in self._keyless_providers}
        if action == "bootstrap":
            with self._lock:
                return {"tools": self._definitions, "generation": self._generation}
        if action == "tool_ack":
            generation = data.get("generation")
            with self._tools_changed:
                if type(generation) is int and generation == self._generation:
                    self._ack_generation = generation
                    self._tools_changed.notify_all()
                    return {"accepted": True}
            return {"accepted": False}
        if action == "poll":
            with self._lock:
                while self._queue:
                    request_id = self._queue.popleft()
                    pending = self._commands.get(request_id)
                    if pending:
                        return {"command": {"id": request_id, "operation": pending.operation, "arguments": pending.arguments}}
            return {"command": None}
        if action == "is_pending":
            request_id = data.get("id")
            with self._lock:
                pending = self._commands.get(request_id) if isinstance(request_id, str) else None
                return {"pending": bool(pending and not pending.ready.is_set() and not self._stopped)}
        if action == "result":
            request_id = data.get("id")
            with self._lock:
                pending = self._commands.get(request_id)
                if pending and not pending.ready.is_set():
                    result = data.get("result")
                    error = data.get("error")
                    limit = (_FRAME_LIMIT // 2 if pending.operation == "cad_image_context_refs"
                             else 512 * 1024 if pending.operation in {"tool_state", "handoff", "config_list"}
                             else _CONTROL_LIMIT)
                    if _encoded_size(result) > limit:
                        pending.error = "failed"
                    else:
                        pending.result = result
                        pending.error = error if isinstance(error, str) and error in {"unsupported", "invalid_request", "failed", "cancelled", "busy", "timeout"} else ("failed" if error else None)
                    pending.ready.set()
            return {"accepted": bool(pending)}
        if action == "tool_start":
            name, args, identity = data.get("name"), data.get("arguments"), data.get("context")
            if not isinstance(name, str) or not isinstance(args, dict) or not isinstance(identity, dict):
                return {"error": "invalid_request"}
            if not all(isinstance(identity.get(k), str) and identity[k] for k in ("agent_id", "session_id", "tool_call_id")):
                return {"error": "invalid_request"}
            ctx = _ToolContext(identity)
            request_id = secrets.token_hex(16)
            with self._lock:
                if self._stopped or len(self._tools) >= _MAX_TOOLS:
                    return {"error": "busy"}
                if not any(t["name"] == name for t in self._definitions):
                    return {"error": "unsupported"}
                self._tools[request_id] = ctx
            def execute() -> None:
                try:
                    result = self._dispatch_tool(name, args, ctx)
                    if _encoded_size(result) > _FRAME_LIMIT // 2:
                        raise ValueError("tool result exceeds bridge limit")
                    with ctx._lock:
                        ctx.result = result
                except Exception as exc:
                    detail = None
                    # Tool-authored refusals and engine diagnostics explain how to
                    # fix the call. Unexpected exceptions stay private.
                    if isinstance(exc, (ValueError, CoreError)):
                        from services.agent.agent_turn import format_error_detail
                        message = str(exc)
                        for private in self._tool_error_redactions:
                            if private:
                                message = message.replace(private, "[redacted]")
                        detail = format_error_detail(message, limit=2000)
                    with ctx._lock:
                        ctx.error = "failed"
                        ctx.error_detail = detail
                finally:
                    with ctx._lock:
                        ctx.done = True
                    if ctx.cancelled:
                        def discard() -> None:
                            with self._lock:
                                self._tools.pop(request_id, None)
                        timer = threading.Timer(30, discard)
                        timer.daemon = True
                        timer.start()
            threading.Thread(target=execute, name="omp-cad-tool", daemon=True).start()
            return {"id": request_id}
        if action in {"tool_poll", "tool_cancel"}:
            request_id = data.get("id")
            with self._lock:
                ctx = self._tools.get(request_id)
            if ctx is None:
                return {"error": "not_found"}
            if action == "tool_cancel":
                ctx.cancel()
                with ctx._lock:
                    already_done = ctx.done
                if already_done:
                    with self._lock:
                        self._tools.pop(request_id, None)
                return {"cancelled": True}
            with ctx._lock:
                updates = list(ctx._updates)
                ctx._updates.clear()
                done, result, error = ctx.done, ctx.result, ctx.error
                error_detail = ctx.error_detail
            with self._lock:
                if done:
                    self._tools.pop(request_id, None)
                generation = self._generation
            return {"done": done, "updates": updates, "result": result, "error": error,
                    "error_detail": error_detail, "generation": generation}
        return {"error": "unsupported"}
