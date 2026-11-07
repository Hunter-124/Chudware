"""Persistent OMP RPC sessions for the long-lived agent worker.

The dependency import and executable lookup deliberately happen on first use.  Importing
``services.agent`` must remain possible in offline and partially provisioned runtimes.
"""

from __future__ import annotations

import dataclasses
import json
import hashlib
import logging
import math
import os
import shutil
import sys
import tempfile
import re
import threading
import subprocess
import signal
import sqlite3
import stat
import time
import uuid
from urllib.parse import urlsplit
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Reversible, Sequence
from dataclasses import dataclass, field
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from services.agent import demo_lane
from services.agent.omp_bridge import BridgeError
from services.agent.omp_questions import TurnQuestions
from services.agent.online_turn import format_duration
from services.agent.progress_watchdog import ProgressWatchdog, TurnBudgetReminder
from services.agent import request_context as tenancy

_DEFAULT_MAX_SESSIONS = 8
_MAX_MAX_SESSIONS = 32
_MAX_OPEN_URL_CHARS = 16_384
_MAX_UI_TEXT_CHARS = 4_096

# A turn whose caller sent no positive `timeout` has NO wall-clock limit: it ends at its
# terminal agent_end, on Stop/close (abort), or when the OMP process dies. The old
# 2-hour default stopped healthy GUI turns mid-build. omp_rpc's
# `wait_for_idle(timeout=None)` means its own 60 s short-wait default, never "forever", so
# an unbudgeted turn waits in finite slices and re-checks cancel/close between them.
_UNBOUNDED_WAIT_SLICE_SECONDS = 30.0
# After an explicit budget expires the host aborts the run and waits this long for its
# terminal agent_end. It must stay under hostd's bridge slack (budget + 60 s) so the GUI
# still receives this result instead of its own transport timeout. An unbudgeted turn
# whose Stop was delivered gets the same grace to settle before it reports cancelled.
_BUDGET_ABORT_GRACE_SECONDS = 30.0
# Clock for that post-cancel grace (tests substitute a controlled clock).
_monotonic = time.monotonic
# OMP's `/compact <mode>` subcommands (session/compact-modes.ts COMPACT_MODES).
_COMPACT_MODES = frozenset({"soft", "remote", "snapcompact"})
# One CAD helper model step (regen diagnosis, ECAD topology planning): a tool-free,
# history-free completion. It is bounded on its own; it never bounds a conversation.
_HELPER_TIMEOUT_SECONDS = 180.0
# Cancel/deadline re-check interval while a helper completion or a turn's settlement
# probe runs.
_HELPER_WAIT_SLICE_SECONDS = 0.5
_CONTROL_CALL_SECONDS = 1.5
_MAINTENANCE_SECONDS = 600.0

class _BoundedCallTimeout(TimeoutError):
    pass


def _start_call(
    call: Callable[[], Any], *, hold: threading.Event | None = None
) -> tuple[threading.Event, dict[str, Any]]:
    """Run a transport call on its own thread; ``done`` is set once ``result`` holds
    its ``value`` or ``error``. With ``hold``, a call that succeeded keeps its thread
    parked until ``hold`` is set."""
    done = threading.Event()
    result: dict[str, Any] = {}
    def invoke() -> None:
        try:
            result["value"] = call()
        except BaseException as exc:
            result["error"] = exc
        finally:
            done.set()
        if hold is not None and "error" not in result:
            hold.wait()
    threading.Thread(target=_context_callback(invoke), name="omp-bounded-call", daemon=True).start()
    return done, result


def _bounded_call(
    call: Callable[[], Any], timeout: float, *, hold: threading.Event | None = None
) -> Any:
    """Bound a transport call including writer-lock acquisition and pipe writes."""
    done, result = _start_call(call, hold=hold)
    if not done.wait(timeout):
        raise _BoundedCallTimeout("OMP control request timed out")
    if "error" in result:
        raise result["error"]
    return result.get("value")

#: OMP builtins a CAD conversation uses. CAD's own tools arrive as host tools
#: (set_custom_tools), which this list does not restrict. Memory tools are added per the
#: session's memory backend (``_CAD_MEMORY_TOOLS``).
CAD_SESSION_TOOLS = (
    "read", "bash", "edit", "write", "glob", "grep", "eval", "task", "wait", "todo",
    "web_search",
)

_LOG = logging.getLogger(__name__)


class HelperCompletionError(RuntimeError):
    """A tool-free helper completion produced no usable text (cancelled, over budget,
    model rejected, provider failure or runtime error). The message is the runtime's own
    reason; callers report it rather than fabricating a model answer."""

class SessionAssetError(RuntimeError):
    """An exact session image is unavailable; the journal remains resumable after repair."""

    def __init__(self, detail: str):
        super().__init__(f"session_asset_error: {detail} (recoverable; session history was not changed)")


def publish_host_tools(client: Any, tools: Sequence[Any], advertised: Iterable[str]) -> None:
    """Publish the authorized CAD registry to the session-bound extension."""
    bridge = getattr(client, "_omp_bridge", None)
    if bridge is None:
        raise RuntimeError("OMP CAD extension bridge is unavailable")
    client._omp_bindings.clear()
    client._omp_bindings.update({binding.name: binding for binding in tools})
    bridge.publish_tools([binding.as_dict() for binding in tools])
    bridge.wait_tools(timeout=20.0)


def _keyless_provider_opt_ins() -> list[str]:
    if demo_lane.enabled():
        # The lane authenticates on its own side of the socket; there is no key to look for.
        return [demo_lane.PROVIDER_ID]
    from services.agent.config import load_config
    config = load_config()
    agent = config.get("agent") if isinstance(config, Mapping) else None
    values = agent.get("keyless_providers") if isinstance(agent, Mapping) else None
    return [value for value in values if isinstance(value, str) and value] if isinstance(values, list) else []


def _require_runtime_model(client: Any, state: Mapping[str, Any], *, timeout: float,
                           keyless_providers: Sequence[str] | None = None) -> None:
    """Admit the actual selected model, not an inferred provider or a catalog default."""
    model = state.get("model")
    provider = _nonempty_string(model.get("provider")) if isinstance(model, Mapping) else None
    model_id = _nonempty_string(model.get("id")) if isinstance(model, Mapping) else None
    if not provider or not model_id:
        raise RuntimeError("No active OMP model is selected; choose a provider and model in Settings.")
    bridge = getattr(client, "_omp_bridge", None)
    if bridge is None:
        raise RuntimeError("Cannot verify the active OMP provider authentication.")
    auth = bridge.call("auth_state", {"provider": provider}, timeout=timeout)
    if not isinstance(auth, Mapping) or auth.get("provider") != provider or type(auth.get("configured")) is not bool:
        raise RuntimeError("Cannot verify the active OMP provider authentication.")
    if auth["configured"]:
        return
    opted_in = _keyless_provider_opt_ins() if keyless_providers is None else keyless_providers
    if provider not in opted_in:
        raise RuntimeError(
            f"Provider {provider} has no configured authentication. Select an authenticated model "
            "or explicitly enable this keyless provider in Settings."
        )


_BYOK_PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "xai": "XAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
}


def byok_provider_env(provider: str) -> str | None:
    return _BYOK_PROVIDER_ENV.get(provider.strip().lower())


def _credential_scope(params: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """Return the BYOK tenant, provider, key, and OMP's credential env name."""
    if tenancy.in_cell():
        # Provider key bytes never enter a cell. The one exception is a public-demo
        # container's free-model lane: a placeholder key, one fixed loopback endpoint, and
        # nothing the caller chooses, so a caller-supplied key is still refused.
        if not demo_lane.enabled() or any(key in params for key in ("api_key", "byok_key")):
            raise ValueError("credential_isolation_unsupported")
        owner = tenancy.owner_key()
        if owner is None:
            raise ValueError("cell_refused")
        return owner[1], demo_lane.PROVIDER_ID, demo_lane.PLACEHOLDER_KEY, ""
    key = _nonempty_string(params.get("byok_key"))
    if key is None:
        if params.get("byok_key") is not None:
            raise ValueError("byok_unsupported_online")
        return None
    tenant = _nonempty_string(params.get("byok_tenant")) or "desktop"
    provider = (_nonempty_string(params.get("byok_provider")) or "").lower()
    env_name = byok_provider_env(provider)
    if env_name is None:
        raise ValueError("byok_unsupported_online")
    return tenant, provider, key, env_name


def _credential_identity(params: Mapping[str, Any]) -> str | None:
    scope = _credential_scope(params)
    if scope is None:
        return None
    tenant, provider, secret, _ = scope
    return hashlib.sha256(
        json.dumps(["byok", tenant, provider, secret], separators=(",", ":")).encode()
    ).hexdigest()


def _credential_client_scope(
    params: Mapping[str, Any], session_dir: str | None, resume_file: str | None
) -> tuple[tempfile.TemporaryDirectory[str], str, dict[str, str]]:
    from services import paths

    scope = _credential_scope(params)
    if scope is None:
        raise ValueError("byok_unsupported_online")
    _, _, secret, env_name = scope
    identity = _credential_identity(params)
    if tenancy.in_cell():
        return _demo_client_scope(identity, resume_file)
    root = Path(session_dir) if session_dir else paths.ROOT / "Sessions"
    if resume_file and session_dir is None:
        root = Path(resume_file).expanduser().parent.parent
    scoped = root.expanduser() / f"byok-{identity}"
    if scoped.is_symlink():
        raise RuntimeError("credential session directory cannot be a symlink")
    if resume_file and Path(resume_file).expanduser().resolve().parent != scoped.resolve():
        raise RuntimeError("session file belongs to another credential scope")
    scoped.mkdir(mode=0o700, parents=True, exist_ok=True)
    scoped.chmod(0o700)
    tempdir = tempfile.TemporaryDirectory(prefix="chudware-byok-")
    home = Path(tempdir.name)
    agent, xdg = home / "agent", home / "xdg"
    agent.mkdir(mode=0o700)
    xdg.mkdir(mode=0o700)
    # RpcClient merges env into os.environ: blank inherited credentials/config
    # before restoring process essentials and the one selected provider secret.
    clean = {name: "" for name in os.environ}
    for name in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "PI_PACKAGE_DIR"):
        if name in os.environ:
            clean[name] = os.environ[name]
    clean.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "PI_CODING_AGENT_DIR": str(agent),
        env_name: secret,
    })
    return tempdir, str(scoped), clean


def _bridge_parent() -> str | None:
    """Where an OMP extension bridge socket may live.

    A cell worker's OMP child runs in a nested sandbox with its own private /tmp; only the
    actor directory is shared, so the bridge must be created beneath the actor's runtime
    root (short enough for a socket path). Desktop keeps the system temporary directory.
    """
    if not tenancy.in_cell():
        return None
    root = Path(os.environ.get("XDG_RUNTIME_DIR") or "/actor/run")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(root)


def _demo_client_scope(
    identity: str | None, resume_file: str | None
) -> tuple[tempfile.TemporaryDirectory[str], str, dict[str, str]]:
    """A demo cell's OMP state: private under /actor, with OpenRouter pointed at the lane.

    OMP runs in a nested sandbox that shares only /actor with this worker, so every path it
    needs is created beneath the actor's own state root. The environment is not rebuilt: a
    cell worker's environment already carries no ambient credentials, and the launcher
    copies only an allowlist of it across the sandbox boundary.
    """
    state = tenancy.actor_state_root()
    root = state / "Sessions"
    scoped = root / f"demo-{identity}"
    if root.is_symlink() or scoped.is_symlink():
        raise RuntimeError("credential session directory cannot be a symlink")
    if resume_file and Path(resume_file).expanduser().resolve().parent != scoped.resolve():
        raise RuntimeError("session file belongs to another credential scope")
    scoped.mkdir(mode=0o700, parents=True, exist_ok=True)
    scoped.chmod(0o700)
    tempdir = tempfile.TemporaryDirectory(prefix="chudware-demo-", dir=state)
    try:
        agent = Path(tempdir.name) / "agent"
        agent.mkdir(mode=0o700)
        try:
            rows = demo_lane.models()
        except demo_lane.LaneError as exc:
            # The override still reaches the lane; OMP falls back to its bundled catalog.
            _LOG.warning("demo model list unavailable (%s)", exc)
            rows = None
        config = agent / "models.yml"
        config.write_text(json.dumps(demo_lane.models_config(rows)), encoding="utf-8")  # JSON is YAML
        config.chmod(0o600)
    except BaseException:
        tempdir.cleanup()
        raise
    return tempdir, str(scoped), {"PI_CODING_AGENT_DIR": str(agent)}







_AUTH_SHAPES = {
    "setup_providers": (frozenset(), frozenset()),
    "auth_state": (frozenset({"provider"}), frozenset()),
    "auth_states": (frozenset({"providers"}), frozenset()),
    "set_api_key": (frozenset({"provider", "api_key"}), frozenset()),
    "remove_api_key": (frozenset({"provider"}), frozenset()),
}
_MEMORY_SHAPES = {
    "memory_status": (frozenset({"root"}), frozenset()),
    "memory_search": (frozenset({"root", "query"}), frozenset({"limit"})),
    "memory_save": (frozenset({"root", "content"}), frozenset({"kind", "context", "source", "importance"})),
}
_TRANSIENT_SHAPES = {
    "get_login_providers": (frozenset(), frozenset()),
    "get_available_models": (frozenset(), frozenset()),
    "login": (frozenset({"providerId"}), frozenset()),
    "get_logout_accounts": (frozenset({"providerId"}), frozenset()),
    "logout": (frozenset({"providerId", "credentialId"}), frozenset()),
}
_SESSION_SHAPES = {
    "get_session_stats": (frozenset(), frozenset()),
    "set_auto_compaction": (frozenset({"enabled"}), frozenset()),
    "set_auto_retry": (frozenset({"enabled"}), frozenset()),
    "get_state": (frozenset(), frozenset()),
    "tool_state": (frozenset(), frozenset()),
    "get_messages": (frozenset(), frozenset()),
    "handoff": (frozenset(), frozenset({"customInstructions"})),
    "export_html": (frozenset(), frozenset({"outputPath"})),
    "branch": (frozenset({"entryId"}), frozenset()),
}

_MAX_MODEL_HINTS = 6




def _model_catalog(client: Any) -> list[Any]:
    # Discovery can report unknown limits/prices as null. Read the catalog at
    # the wire boundary so one such row cannot fail the SDK's eager ModelInfo
    # decoding and hide every provider; worker_rpc sanitizes public metadata.
    response = client.request_raw("get_available_models")
    if not isinstance(response, Mapping) or not isinstance(response.get("models"), list):
        raise TypeError("Agent service returned an invalid models list")
    return response["models"]


def _catalog_effort(client: Any, provider: str | None, model: str | None,
                    requested: str) -> str:
    """Keep the selected effort exact; OMP reports unsupported selections."""
    return requested


def _describe_model_rejection(client: Any, provider: str, model: str, exc: Exception) -> str:
    """Turn a bare ``set_model`` refusal into the user's next move.

    OMP's catalog lists only providers that are signed in, so "Model not found" means
    one of two very different things: the provider has no credential, or the id is not in
    that provider's catalog. Live 2026-09-03: a persisted anthropic/claude-sonnet-5 pick
    with Anthropic signed out surfaced as ``set_model: Model not found`` and nothing told
    the user to sign in or pick another model.
    """
    try:
        models = _model_catalog(client)
    except Exception:  # noqa: BLE001 - the catalog probe is best-effort
        return f"set_model {provider}/{model}: {exc}"

    def field_of(item: Any, name: str) -> str:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        return value if isinstance(value, str) else ""

    signed_in = sorted({field_of(item, "provider") for item in models} - {""})
    same_provider = [field_of(item, "id") for item in models if field_of(item, "provider") == provider]
    if not same_provider:
        others = (
            f" Signed-in providers: {', '.join(signed_in)}."
            if signed_in
            else " No provider is signed in."
        )
        return (
            f"{provider} is not signed in to the agent service, so {provider}/{model} cannot run. "
            f"Sign in under Settings -> Providers, or pick a model from a signed-in "
            f"provider in the composer's model chip.{others}"
        )
    hints = ", ".join(sorted(m for m in same_provider if m)[:_MAX_MODEL_HINTS])
    return (
        f"{model} is not in the agent service's {provider} catalog. Pick a listed {provider} model "
        f"(e.g. {hints})."
    )


@dataclass
class _Session:
    session_id: str
    client: Any
    resume_file: str | None
    workspace_dir: str | None
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    policy_lock: threading.Lock = field(default_factory=threading.Lock)
    credential_identity: str | None = None
    active_turns: int = 0
    closed: bool = False
    dispose_started: bool = False
    client_stopped: bool = False
    host_tool_names: set[str] = field(default_factory=set)
    compaction_settings: dict[str, Any] = field(default_factory=dict)
    auto_compaction: bool | None = None
    auto_retry: bool | None = None
    launch_params: dict[str, Any] = field(default_factory=dict, repr=False)
    owner: tuple[str, str] | None = field(default_factory=tenancy.owner_key)
    registry_key: str = ""
    sandbox_class: str = "rw"


@dataclass
class _CreationGuard:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0

@dataclass
class _ActivityProbe:
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, int] | None = None
    complete: bool = False
    request_id: str | None = None

@dataclass
class _DictationActivity:
    """Track the RPC's event-only dictation lifecycle from process startup."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    captures: set[str] = field(default_factory=set)
    attested: bool = True

    def observe(self, event: Any) -> None:
        malformed = getattr(event, "type", None) == "unknown"
        if malformed:
            event = getattr(event, "payload", None)
        if isinstance(event, Mapping):
            kind = event.get("type")
            capture_id = event.get("captureId")
            state = event.get("state")
        else:
            kind = getattr(event, "type", None)
            capture_id = getattr(event, "capture_id", None)
            state = getattr(event, "state", None)
        if kind not in {"dictation_state", "dictation_transcript", "dictation_end"}:
            return
        with self.lock:
            if malformed:
                self.attested = False
            if (not isinstance(capture_id, str) or not capture_id.strip()
                    or len(capture_id) > 128):
                self.attested = False
                return
            if kind == "dictation_end":
                if not malformed:
                    self.captures.discard(capture_id)
            else:
                self.captures.add(capture_id)
                if kind == "dictation_state" and state not in {
                    "starting", "recording", "transcribing"
                }:
                    self.attested = False




@dataclass
class _Steer:
    """One steer handed to OMP: its text, images and the GUI's correlation id."""

    text: str
    images: list[dict[str, Any]]
    steer_id: str = ""
    from_user: bool = True
    owner: tuple[str, str] | None = field(default_factory=tenancy.owner_key)

    @property
    def key(self) -> tuple[str, str]:
        """Deduplicate the whole payload, never text shared by distinct images."""
        return self.text, json.dumps(self.images, sort_keys=True)


@dataclass
class _Run:
    run_id: str
    session_id: str
    client: Any | None = None
    started: bool = False
    cancel_requested: bool = False
    daemon_connections: Any | None = None
    cancel_reason: str = ""
    control_delegate: dict[str, str] | None = None
    cancel_sent: bool = False
    terminal_cancelled: bool | None = None
    submitting: bool = False
    steers: set[tuple[str, str]] = field(default_factory=set)
    control_lock: threading.Lock = field(default_factory=threading.Lock)
    # Steers queued before prompt() returned; images are OMP ImageContent.
    pending_steers: list[_Steer] = field(default_factory=list)
    # User steers OMP holds in its queue until the next tool boundary. A user
    # message frame carrying the text means the model has it; until then a pure wait
    # (wait_job) is woken and Stop withdraws them instead of letting OMP's
    # post-abort drain start a turn nobody observes.
    undelivered: list[_Steer] = field(default_factory=list)
    # Pending ask_user batches of an interactive (GUI) turn; None for headless turns.
    questions: TurnQuestions | None = None
    owner: tuple[str, str] | None = field(default_factory=tenancy.owner_key)
    credential_identity: str | None = None
    registry_key: str = ""
    session_key: str = ""
    resume_file: str | None = None
    # False for a maintenance run (manual compaction): a steer would start a turn.
    steerable: bool = True

_EVENT_QUEUE_LIMIT = 256


def _event_field(value: Any, camel: str, snake: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(camel, value.get(snake)) if snake else value.get(camel)
    return getattr(value, snake or camel, None)


def _event_delta(event: Any) -> tuple[tuple[Any, ...], str] | None:
    """Identify only unambiguous, adjacent text/thinking updates of one block."""
    owner = ""
    if _event_field(event, "type") == "subagent_event":
        if isinstance(event, Mapping) and set(event) != {"type", "payload"}:
            return None
        payload = _event_field(event, "payload")
        if isinstance(payload, Mapping) and set(payload) != {"id", "event"}:
            return None
        owner = _event_field(payload, "id")
        if not isinstance(owner, str) or not owner:
            return None
        event = _event_field(payload, "event")
    if _event_field(event, "type") != "message_update":
        return None
    if isinstance(event, Mapping) and set(event) - {
        "type", "message", "messageId", "message_id",
        "assistantMessageEvent", "assistant_message_event",
    }:
        return None
    message_id = _event_field(event, "messageId", "message_id")
    child = _event_field(event, "assistantMessageEvent", "assistant_message_event")
    if not isinstance(message_id, str) or not message_id or not isinstance(child, Mapping):
        return None
    if set(child) - {"type", "contentIndex", "content_index", "delta", "partial"}:
        return None
    kind = child.get("type")
    index = child.get("contentIndex", child.get("content_index"))
    delta = child.get("delta")
    if (kind not in {"text_delta", "thinking_delta"} or type(index) is not int
            or not isinstance(delta, str)):
        return None
    return (owner, message_id, kind, index), delta


@dataclass
class _MergedEventDelta:
    event: Any
    key: tuple[Any, ...]
    parts: list[str]


@dataclass
class _QueuedEvent:
    event: Any
    received_at: float


@dataclass
class _SteerDelivery:
    event: Any
    steers: list[_Steer]


def _merge_event_deltas(first: Any, second: Any) -> _QueuedEvent | None:
    if not isinstance(first, _QueuedEvent) or not isinstance(second, _QueuedEvent):
        return None
    left_event, right_event = first.event, second.event
    left = ((left_event.key, left_event.parts) if isinstance(left_event, _MergedEventDelta)
            else _event_delta(left_event))
    right = ((right_event.key, right_event.parts) if isinstance(right_event, _MergedEventDelta)
             else _event_delta(right_event))
    if left is None or right is None or left[0] != right[0]:
        return None
    parts = left[1] if isinstance(left_event, _MergedEventDelta) else [left[1]]
    parts.extend(right[1] if isinstance(right_event, _MergedEventDelta) else [right[1]])
    merged = _MergedEventDelta(
        right_event.event if isinstance(right_event, _MergedEventDelta) else right_event,
        left[0], parts,
    )
    return _QueuedEvent(merged, second.received_at)


class _EventForwarder:
    """Bounded ordered handoff from the RPC reader to the GUI callback."""

    def __init__(self, callback: Callable[[Any, float], None], errors: list[str],
                 *, clock: Callable[[], float] = time.monotonic):
        self._callback = callback
        self._errors = errors
        self._clock = clock
        self._condition = threading.Condition()
        self._pending: deque[Any] = deque()
        self._putters = 0
        self._closing = False
        self.thread = threading.Thread(
            target=_context_callback(self._consume), name="omp-event-forwarder", daemon=True
        )
        self.thread.start()

    def put(self, event: Any) -> bool:
        if not isinstance(event, threading.Event):
            event = _QueuedEvent(event, self._clock())
        with self._condition:
            if self._closing:
                return False
            self._putters += 1
            try:
                while len(self._pending) >= _EVENT_QUEUE_LIMIT:
                    merged = _merge_event_deltas(self._pending[-1], event)
                    if merged is not None:
                        self._pending[-1] = merged
                        return True
                    # A previous pair can make room even for a non-delta frame.
                    for index in range(len(self._pending) - 1):
                        merged = _merge_event_deltas(
                            self._pending[index], self._pending[index + 1]
                        )
                        if merged is not None:
                            self._pending[index] = merged
                            del self._pending[index + 1]
                            break
                    else:
                        # No bounded, lossless queue can accept an infinite stream
                        # of distinct events while its consumer is stopped.
                        self._condition.wait()
                self._pending.append(event)
                self._condition.notify_all()
                return True
            finally:
                self._putters -= 1
                self._condition.notify_all()

    def flush(self) -> None:
        barrier = threading.Event()
        if self.put(barrier):
            barrier.wait()

    def close(self) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self.thread.join()

    def _consume(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    if self._closing and not self._putters:
                        return
                    self._condition.wait()
                event = self._pending.popleft()
                self._condition.notify_all()
            if isinstance(event, threading.Event):
                event.set()
                continue
            received_at = event.received_at
            event = event.event
            try:
                if isinstance(event, _MergedEventDelta):
                    frame = _json_value(event.event)
                    update = frame["payload"]["event"] if event.key[0] else frame
                    child = update.get("assistantMessageEvent", update.get("assistant_message_event"))
                    child["delta"] = "".join(event.parts)
                    event = frame
                self._callback(event, received_at)
            except Exception as exc:  # noqa: BLE001 - keep draining after a bad event
                self._errors.append(str(exc))


@contextmanager
def _turn_claim(session: _Session, run: _Run):
    while not session.turn_lock.acquire(timeout=0.25):
        if run.cancel_requested:
            raise RuntimeError("OMP turn cancelled while waiting for the session")
        if session.closed:
            raise RuntimeError("OMP session is closed")
    try:
        yield
    finally:
        session.turn_lock.release()


class OmpSessionHost:
    """Own one persistent ``RpcClient`` process per Chudware conversation.

    ``client_factory`` is intentionally injectable.  Production leaves it unset and gets
    the pinned ``omp_rpc.RpcClient`` lazily; focused tests can supply a process-free
    fake accepting the same keyword arguments.
    """

    def __init__(self, client_factory=None, binary=None, *, clock=None):
        self._clock = clock or time.monotonic
        self._realtime_watchdog = clock is None
        # Watchdog / turn-budget timers; a test may substitute a recording factory.
        self._timer_factory: Callable[..., threading.Timer] = threading.Timer
        self._client_factory = client_factory
        self._binary_hint = os.fspath(binary) if binary is not None else None
        self._max_sessions = _session_limit_from_env()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._creating: dict[str, _CreationGuard] = {}
        self._reserved_creations: dict[tuple[str, str] | None, int] = {}
        self._runs: dict[str, _Run] = {}
        self._completed_runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # User steers Stop withdrew before OMP delivered them, keyed by Chudware
        # session id: the transcript already shows them, so they ride with that
        # conversation's next prompt.
        self._carried_steers: dict[str, list[_Steer]] = {}
        self._prestart_cancels: dict[str, tuple[dict[str, str] | None, str | None, bool]] = {}
        self._lock = threading.RLock()
        # One service process owns catalog and authentication SDK operations.
        self._service_client: Any = None
        self._service_memory_backend: str | None = None
        self._service_lock = threading.Lock()
        # Live tool-free helper processes (``complete_text``): disposable per call,
        # tracked only so shutdown() stops them with everything else this worker owns.
        self._helper_clients: set[Any] = set()
        self._activity_probes: dict[Any, _ActivityProbe] = {}
        self._shutdown = False

    def run_turn(self, params, emit) -> dict:
        """Schedule a prompt and block until a terminal ``agent_end`` is observed."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        if not isinstance(params, Mapping):
            return {"ok": False, "reason": "omp turn params must be an object"}
        if not callable(emit):
            return {"ok": False, "reason": "omp turn emit callback is required"}
        emit = _context_callback(emit)

        prompt = params.get("request", params.get("prompt", params.get("message")))
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "reason": "omp turn prompt is empty"}
        run = self._register_run(params)
        if isinstance(run, dict):
            return run
        session_id, run_id = run.session_id, run.run_id
        session_key = run.session_key
        resume_file = run.resume_file

        session: _Session | None = None
        unsubscribe: Callable[[], None] | None = None
        observed_contexts: list[dict[str, Any]] = []
        tool_recoveries: dict[int, Any] = {}
        daemon_connections = None
        emit_errors: list[str] = []
        provider_frames: deque[dict[str, Any]] = deque(maxlen=256)
        quota_failure: list[str] = []  # the lane's own words for a spent demo allowance
        terminal: dict[str, Any] | None = None
        prompt_outcome: dict[str, Any] = {}
        chosen_mode = params.get("mode")
        preferences = params.get("turn_preferences")
        readonly_turn = (
            bool(params.get("plan_only"))
            or chosen_mode in ("ask", "plan")
            or getattr(preferences, "mode", None) in ("ask", "plan")
        )
        progress = ProgressWatchdog(self._clock, enabled=not readonly_turn)
        progress_lock = threading.RLock()
        progress_timer: threading.Timer | None = None
        scheduled_deadline: float | None = None
        progress_closed = False
        progress_threads: list[threading.Thread] = []
        # Set once the prompt is sent, only for a turn with an explicit budget.
        budget_reminder: TurnBudgetReminder | None = None
        budget_timer: threading.Timer | None = None

        def schedule_progress_timer() -> None:
            nonlocal progress_timer, scheduled_deadline
            deadline = progress.deadline() if not progress_closed else None
            if deadline == scheduled_deadline:
                return
            if progress_timer is not None:
                progress_timer.cancel()
                progress_timer = None
            scheduled_deadline = deadline
            if self._realtime_watchdog and deadline is not None:
                progress_timer = self._timer_factory(
                    max(0.0, deadline - self._clock()), _context_callback(check_progress_timer)
                )
                progress_timer.daemon = True
                progress_timer.start()

        def send_progress_nudge(text: str, feed: dict[str, Any]) -> None:
            if progress_closed or run.cancel_requested:
                # The turn already ended or was stopped: a steer now lands after it.
                return
            try:
                outcome = self.steer(run_id, text, from_user=False)
            except Exception as exc:  # noqa: BLE001 - advisory steering never aborts a turn
                _LOG.warning("Progress steering failed (%s)", exc)
                return
            if outcome.get("steered") and not outcome.get("duplicate"):
                try:
                    emit({
                        "type": "agent.progress_nudge",
                        "message": text,
                        **feed,
                        "run_id": run_id,
                        "session_id": session_id,
                    })
                except Exception as exc:  # noqa: BLE001 - same boundary as RPC events
                    emit_errors.append(str(exc))

        def dispatch_progress_nudge(text: str, feed: dict[str, Any] | None = None) -> None:
            if feed is None:
                feed = {"text": (
                    "Progress nudge: "
                    + text.split(" without building anything.", 1)[0]
                    + ". Commit sourced dimensions; start modeling and save one part at a time."
                )}
            # RpcClient notifications can run on the reader thread. Steering there
            # would wait for a response only that same reader can receive.
            if self._realtime_watchdog:
                thread = threading.Thread(
                    target=_context_callback(send_progress_nudge), args=(text, feed), daemon=True
                )
                progress_threads.append(thread)
                thread.start()
            else:
                send_progress_nudge(text, feed)

        def check_progress_timer() -> None:
            nonlocal progress_timer, scheduled_deadline
            with progress_lock:
                progress_timer = None
                scheduled_deadline = None
                if progress_closed:
                    return
                if run.questions is not None and run.questions.has_pending():
                    # The agent is waiting on the user's answers, not researching.
                    progress.user_input()
                    schedule_progress_timer()
                    return
                text = progress.due()
                schedule_progress_timer()
            if text is not None:
                dispatch_progress_nudge(text)

        def take_budget_reminder() -> tuple[str, dict[str, Any]] | None:
            """The one pre-expiry reminder once due; caller holds ``progress_lock``."""
            if progress_closed or budget_reminder is None:
                return None
            text = budget_reminder.due()
            if text is None:
                return None
            left = budget_reminder.remaining()
            action = ("give the final answer now, then check it" if readonly_turn
                      else "save requested deliverables now, then verify")
            return text, {
                "text": f"Turn budget: {format_duration(left)} left; {action}.",
                "reason": "turn_budget",
                "budget_s": budget_reminder.budget_s,
                "remaining_s": round(left, 1),
            }

        def check_budget_timer() -> None:
            with progress_lock:
                reminder = take_budget_reminder()
            if reminder is not None:
                dispatch_progress_nudge(*reminder)

        try:
            if run.cancel_requested:
                emit({"type": "run_end", "status": "cancelled"})
                return {"ok": True, "run_id": run_id, "session_id": session_id,
                        "cancelled": True, "terminal": None, **_stop_audit(run)}
            session = self._get_or_create_session(session_id, params, resume_file)
            with _turn_claim(session, run), ExitStack() as turn_resources:
                self._refresh_compaction(session, params)
                with self._lock:
                    if session.closed:
                        raise RuntimeError("omp RPC session was closed")
                    run.client = session.client
                    self._sessions.move_to_end(session_key)

                # Emitted right after agent_start (the GUI resets its meter on run_started):
                # the resume key first, then get_state's contextUsage — the resumed session's
                # real fill.
                opening: list[dict[str, Any]] = []
                # Runtime call ids are scoped to their root/child agent. Keep
                # backgrounded evals open until their async completion update.
                open_calls: dict[tuple[str, str], str] = {}
                nested_parents: dict[tuple[str, str], tuple[str, str | None]] = {}
                child_agents: set[str] = set()
                # The prompt text actually sent: its own user frame is not a steer delivery.
                sent_prompt: list[str] = []
                # The observer's state lock is never held while calling the GUI.
                emit_lock = threading.Lock()
                output_lock = threading.Lock()
                prompt_id: list[str] = []
                prompt_ready = threading.Event()
                rollback_warnings: list[dict[str, Any]] = []
                rollback_notice = ""
                rollback_checked = False

                def emit_rollback_notice() -> None:
                    nonlocal rollback_checked, rollback_notice
                    if rollback_checked:
                        return
                    rollback_checked = True
                    try:
                        from services.mcp.cad_common import touched_rollback_warnings
                        rollback_warnings.extend(touched_rollback_warnings(run_id))
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning("Could not inspect terminal rollback state: %s", exc)
                        return
                    if rollback_warnings:
                        rollback_notice = "\n\n**Chudware safety notice:** " + "\n".join(
                            str(w["warning"]) for w in rollback_warnings)
                        emit({"type": "assistant_delta", "channel": "output",
                              "text": rollback_notice})

                def deliver(event: Any, received_at: float) -> None:
                    nonlocal terminal, progress_closed, budget_timer
                    delivered = event.steers if isinstance(event, _SteerDelivery) else []
                    if isinstance(event, _SteerDelivery):
                        event = event.event
                    # Every queued notification is distinct, even if an SDK frame's
                    # object identity is recycled.
                    frame = _json_value(event)
                    if not isinstance(frame, dict):
                        frame = {"type": "unknown", "value": frame}
                    kind = frame.get("type")
                    if kind in {"prompt_result", "session_settled"}:
                        # A notification can precede the prompt's control response.
                        # Wait here, not on the RPC reader, and retain stream order.
                        prompt_ready.wait()
                        if not prompt_id:
                            return
                        if kind == "prompt_result":
                            if frame.get("id") != prompt_id[0]:
                                return
                            prompt_outcome.update(frame)
                        elif not prompt_outcome:
                            return
                    if (kind == "session_settled" or
                            (kind == "prompt_result" and frame.get("sessionSettled") is True)):
                        frame.pop("control_delegate", None)
                        for key in ("reason", "stopReason", "stop_reason"):
                            if frame.get(key) == "manager_stop":
                                frame.pop(key)
                        frame.update(_stop_audit(run))
                        if run.cancel_reason == "manager_stop":
                            frame["stopReason"] = "manager_stop"
                        terminal = frame
                        with progress_lock:
                            progress_closed = True
                            schedule_progress_timer()
                            if budget_timer is not None:
                                budget_timer.cancel()
                                budget_timer = None
                    if kind in _RETRY_FAILED_FRAMES or kind in _ASSISTANT_OUTPUT_FRAMES:
                        provider_frames.append(frame)
                    quota = demo_lane.frame_quota_message(frame)
                    if quota is not None:
                        # A spent demo allowance is not transient: end OMP's retry loop now and
                        # report the lane's message as the turn's result.
                        quota_failure[:] = [quota]
                        _abort_retry(session.client)
                    if not progress_closed:
                        observed = frame
                        if kind in {"host_tool_start", "host_tool_end"} and frame.get("subagentId"):
                            # A child's research is not the root's stalled research;
                            # use the watchdog's existing child-mutation path.
                            observed = {
                                "type": "subagent_event",
                                "payload": {"id": frame["subagentId"], "event": {
                                    **frame, "type": (
                                        "tool_execution_start" if kind == "host_tool_start"
                                        else "tool_execution_end"
                                    ),
                                }},
                            }
                        with progress_lock:
                            # The watchdog observes when the reader saw each frame,
                            # not when a slow GUI consumer happened to release it.
                            clock = progress.clock
                            progress.clock = lambda: received_at
                            reminder_clock = budget_reminder.clock if budget_reminder is not None else None
                            if budget_reminder is not None:
                                budget_reminder.clock = progress.clock
                            try:
                                nudge = progress.observe(observed, session.host_tool_names)
                                schedule_progress_timer()
                                reminder = take_budget_reminder()
                            finally:
                                progress.clock = clock
                                if budget_reminder is not None and reminder_clock is not None:
                                    budget_reminder.clock = reminder_clock
                        if nudge is not None:
                            dispatch_progress_nudge(nudge)
                        if reminder is not None:
                            dispatch_progress_nudge(*reminder)
                    try:
                        with output_lock:
                            if (kind == "session_settled" or
                                    (kind == "prompt_result" and frame.get("sessionSettled") is True)):
                                emit_rollback_notice()
                            emit(frame)
                            for steer in delivered:
                                emit(_steer_status_frame(run, steer, "delivered"))
                            if kind == "agent_start":
                                while opening:
                                    emit(opening.pop(0))
                    except Exception as exc:  # noqa: BLE001 - emit is an injected boundary
                        emit_errors.append(str(exc))

                forwarder = _EventForwarder(deliver, emit_errors, clock=self._clock)

                def stop_forwarding() -> None:
                    nonlocal unsubscribe
                    if unsubscribe is not None:
                        try:
                            unsubscribe()
                        except Exception as exc:  # noqa: BLE001 - teardown must continue
                            _LOG.warning(
                                "OMP event unsubscription failed (%s)",
                                type(exc).__name__,
                            )
                        unsubscribe = None
                    prompt_ready.set()
                    forwarder.close()

                turn_resources.callback(stop_forwarding)

                def forward(event: Any) -> None:
                    # SDK notifications are fresh frozen dataclasses. Mutable mapping
                    # clients may reuse the same object immediately after this call.
                    if isinstance(event, Mapping):
                        event = _json_value(event)
                    tracked = event
                    if (_event_field(tracked, "type") == "unknown"
                            and isinstance(_event_field(tracked, "payload"), Mapping)):
                        tracked = _event_field(tracked, "payload")
                    kind = _event_field(tracked, "type")
                    delivered = (
                        self._take_delivered_steers(
                            run, {"message": _event_field(tracked, "message")}, sent_prompt
                        ) if kind == "message_start" else []
                    )
                    execution = tracked
                    owner = ""
                    if kind == "subagent_event":
                        payload = _event_field(tracked, "payload")
                        owner = _nonempty_string(_event_field(payload, "id")) or ""
                        execution = _event_field(payload, "event") if owner else None
                    if (_event_field(execution, "type") == "unknown"
                            and isinstance(_event_field(execution, "payload"), Mapping)):
                        execution = _event_field(execution, "payload")
                    execution_kind = _event_field(execution, "type")
                    body = execution
                    if execution_kind == "custom":
                        execution_kind = _event_field(execution, "customType", "custom_type")
                        body = _event_field(execution, "data")
                    with emit_lock:
                        if kind == "subagent_event" and owner:
                            child_agents.add(owner)
                        # The observer needs call ownership as soon as the RPC reader
                        # sees it, independently of a slow GUI callback.
                        if execution_kind in {
                            "tool_execution_start", "tool_execution_end", "tool_execution_update",
                        }:
                            call_id = _nonempty_string(_event_field(body, "toolCallId", "tool_call_id"))
                            tool = _nonempty_string(_event_field(body, "toolName", "tool_name")) or ""
                            result = (_event_field(body, "result")
                                      if execution_kind == "tool_execution_end"
                                      else _event_field(body, "partialResult", "partial_result"))
                            details = _event_field(result, "details")
                            async_info = _event_field(details, "async")
                            async_state = _event_field(async_info, "state")
                            if call_id and execution_kind == "tool_execution_start":
                                open_calls[(owner, call_id)] = tool
                            elif call_id and execution_kind == "tool_execution_end":
                                if tool != "eval" or async_state != "running":
                                    open_calls.pop((owner, call_id), None)
                            elif call_id and execution_kind == "tool_execution_update" and async_state in {
                                "completed", "failed", "cancelled"
                            }:
                                if open_calls.get((owner, call_id)) == "eval":
                                    open_calls.pop((owner, call_id), None)
                        forwarder.put(_SteerDelivery(event, delivered) if delivered else event)

                def observe_host_call(frame: dict[str, Any]) -> None:
                    """Forward bridged CAD calls under their own agent's eval.

                    OMP supplies a fresh nested id, not the parent eval id. When
                    several evals for an agent are active, never guess which one
                    issued the call. Preserve that decision through the end frame.
                    """
                    call_id = frame.get("toolCallId")
                    agent_id = _nonempty_string(frame.pop("agent_id", None))
                    parent_agent_id = _nonempty_string(frame.pop("parent_agent_id", None))
                    if not isinstance(call_id, str) or not call_id:
                        return
                    key = (agent_id or "", call_id)
                    with emit_lock:
                        if frame.get("type") == "host_tool_start":
                            owner = (
                                agent_id if agent_id and (
                                    parent_agent_id or agent_id in child_agents
                                ) else ""
                            )
                            if (owner, call_id) in open_calls:
                                return  # OMP already owns the visible direct-call row.
                            candidates = [
                                outer for (scope, outer), tool in open_calls.items()
                                if scope == owner and tool == "eval"
                            ]
                            parent = candidates[0] if len(candidates) == 1 else None
                            nested_parents[key] = (owner, parent)
                        else:
                            if key not in nested_parents:
                                return
                            owner, parent = nested_parents.pop(key)
                    if owner:
                        frame["subagentId"] = owner
                    if parent:
                        frame["parentToolCallId"] = parent
                    else:
                        frame["parent_unknown"] = True
                    forward(frame)

                unsubscribe = _register_listener(session.client, _context_callback(forward))
                _collect_host_tool_recoveries(params.get("host_tools"), tool_recoveries)
                observed_contexts = _bind_host_call_observer(
                    params.get("host_tools"), observe_host_call
                )
                from services.mcp.daemon_bridge import TurnConnections

                doc_scope = params.get("_hostd_doc_scope", "")
                daemon_connections = TurnConnections(
                    agent_run_scope=doc_scope if isinstance(doc_scope, str) else ""
                )
                with self._lock:
                    run.daemon_connections = daemon_connections
                    if run.cancel_requested:
                        daemon_connections.abort_active()
                from services.agent.providers.omp_provider import OmpProvider

                helper_params = _client_launch_params(params)
                helper_settings = dict(params.get("settings") or {})
                for key in ("provider", "model", "thinking", "reasoning_effort",
                            "auto_compaction", "auto_retry"):
                    if key in params:
                        helper_settings[key] = params[key]
                helper_params["provider_session_id"] = session_id

                def helper_runtime(role, *, cancel=None, timeout=None):
                    # Preserve this caller's model/actor/OAuth scope, including nested
                    # calls. Role defaults never override an explicit turn selection.
                    def stopped():
                        return run.cancel_requested or bool(callable(cancel) and cancel())

                    inherited_settings = dict(helper_settings)
                    for key in ("auto_compaction", "auto_retry"):
                        value = getattr(session, key)
                        if value is not None:
                            inherited_settings.setdefault(key, value)
                    return OmpProvider(
                        host=self,
                        provider=helper_settings.get("provider"),
                        model=helper_settings.get("model"),
                        compaction_settings=dict(session.compaction_settings),
                        runtime_params={**helper_params, "settings": inherited_settings},
                        cancel=stopped,
                        timeout=timeout,
                    )

                for context in observed_contexts:
                    context["daemon_connections"] = daemon_connections
                    context["helper_runtime"] = helper_runtime
                    context["user_steer_pending"] = lambda: bool(run.undelivered)
                if params.get("interactive_questions"):

                    def emit_question_frame(frame: dict[str, Any]) -> None:
                        with output_lock:
                            emit(frame)

                    questions = TurnQuestions(
                        emit_question_frame,
                        run_id=run_id,
                        session_id=session_id,
                        is_cancelled=lambda: run.cancel_requested,
                    )

                    def ask_human(batch, *, agent_id=None, cancel=None):
                        try:
                            return questions.ask(batch, agent_id=agent_id, cancel=cancel)
                        finally:
                            with progress_lock:
                                progress.user_input()
                                schedule_progress_timer()

                    with self._lock:
                        run.questions = questions
                    for context in observed_contexts:
                        context["ask_human"] = ask_human
                try:
                    _bounded_call(lambda: self._configure_turn(session, params), 30.0)
                except _BoundedCallTimeout:
                    session.closed = True
                    session.client._omp_force_stop = True
                    raise
                keyless_providers = _keyless_provider_opt_ins()
                session.client._omp_bridge.publish_keyless_providers(keyless_providers)
                opening_state = _safe_client_state(session.client)
                _require_runtime_model(session.client, opening_state, timeout=_CONTROL_CALL_SECONDS,
                                       keyless_providers=keyless_providers)
                bound_file = opening_state.get("sessionFile") or opening_state.get(
                    "session_file"
                )
                if isinstance(bound_file, str) and bound_file:
                    if tenancy.in_cell():
                        _session_link(session_id, session.credential_identity, bound_file)
                    opening.append(
                        {
                            "type": "session_bound",
                            "sessionId": opening_state.get("sessionId")
                            or opening_state.get("session_id"),
                            "sessionFile": bound_file,
                        }
                    )
                opening_frame = _context_frame(opening_state)
                if opening_frame is not None:
                    opening.append(opening_frame)
                asset_notice = _session_image_notice(session.client)
                if asset_notice is not None:
                    opening.append(asset_notice)
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
                            **_stop_audit(run),
                        }
                        return result
                    # Messages a Stop withdrew before delivery are already in the user's
                    # transcript, ahead of this one: they open this prompt, in order.
                    carried = self._carried_steers.pop(session_key, [])
                    if carried:
                        message = "\n\n".join([*(s.text for s in carried), message])
                        images = [*(i for s in carried for i in s.images), *(images or [])]
                        opening.extend(
                            _steer_status_frame(run, s, "delivered")
                            for s in carried
                            if s.steer_id
                        )
                    # Mark submission before releasing the lock so Stop can intervene.
                    sent_prompt.append(message)
                    run.started = True
                    run.submitting = True
                try:
                    accepted_id = _bounded_call(
                        lambda: session.client.prompt(message, images=images or None),
                        _CONTROL_CALL_SECONDS)
                    prompt_id.append(accepted_id)
                    prompt_ready.set()
                    with self._lock:
                        run.submitting = False
                        if run.cancel_requested:
                            session.closed = True
                            session.client._omp_force_stop = True
                            raise RuntimeError("OMP prompt cancelled during submission")
                except Exception:
                    with self._lock:
                        session.closed = True
                        session.client._omp_force_stop = True
                        if carried:
                            self._carried_steers[session_key] = [
                                *carried, *self._carried_steers.get(session_key, [])
                            ]
                    raise
                with self._lock:
                    pending_steers = tuple(run.pending_steers)

                for steer in pending_steers:
                    with run.control_lock:
                        # Tracked before the send: its user frame may be forwarded first.
                        with self._lock:
                            if steer.from_user:
                                run.undelivered.append(steer)
                        try:
                            _bounded_call(lambda: session.client.steer(steer.text, images=steer.images or None),
                                          _CONTROL_CALL_SECONDS)
                        except Exception:
                            with self._lock:
                                if steer in run.undelivered:
                                    run.undelivered.remove(steer)
                            raise
                        with self._lock:
                            run.steers.add(steer.key)
                            run.pending_steers.remove(steer)
                # The caller owns the budget (`timeout`, seconds): run_mission.py sends
                # --timeout-s, an explicit GUI/RPC caller may send one. Absent, zero or
                # non-finite means no wall-clock stop. When an explicit budget expires
                # the run is aborted, because a OMP run nobody waits for keeps
                # editing documents unseen (live 2026-09-25: two more unseen hours).
                budget_s = _turn_budget_seconds(params.get("timeout"))
                if budget_s is not None:
                    # One pre-expiry steer (TurnBudgetReminder); it is cancelled by the
                    # terminal or the end of this call, whichever comes first.
                    with progress_lock:
                        if not progress_closed:
                            budget_reminder = TurnBudgetReminder(
                                budget_s, self._clock, readonly=readonly_turn
                            )
                            deadline = budget_reminder.deadline()
                            if self._realtime_watchdog and deadline is not None:
                                budget_timer = self._timer_factory(
                                    max(0.0, deadline - self._clock()), _context_callback(check_budget_timer)
                                )
                                budget_timer.daemon = True
                                budget_timer.start()
                outcome = self._await_turn_idle(run, session, budget_s)
                if outcome == "budget" and budget_s is not None:
                    def budget_terminal() -> dict[str, Any] | None:
                        forwarder.flush()
                        return terminal

                    result = self._stop_budget_exhausted_run(
                        run, session, budget_s, budget_terminal
                    )
                    stop_forwarding()
                    emit({"type": "run_end", "status": "failed", "reason": "turn_budget"})
                    return result
                if outcome == "cancel_unsettled":
                    stop_forwarding()
                    emit({"type": "run_end", "status": "cancelled"})
                    return self._cancelled_without_terminal(run, session)
                state = _safe_client_state(session.client)
                # Unsubscribe and drain while still holding the session turn lock.
                # No terminal result or synthetic frame can overtake queued output.
                stop_forwarding()
                for thread in progress_threads:
                    thread.join(timeout=1)
                if terminal is None:
                    emit_rollback_notice()
                    terminal = {"type": "session_settled"}
                    emit(terminal)

                if tenancy.in_cell():
                    bound = state.get("sessionFile") or state.get("session_file")
                    if bound:
                        _session_link(session_id, session.credential_identity, bound)
                # OMP's own figure (anchored provider usage + unbilled tail), the
                # same number its footer shows; the per-message usage was provisional.
                closing_frame = _context_frame(state)
                if closing_frame is not None:
                    try:
                        emit(closing_frame)
                    except Exception as exc:  # noqa: BLE001 - emit is an injected boundary
                        emit_errors.append(str(exc))
                provider_failure = _unrecovered_provider_failure(provider_frames)
                if provider_failure is not None and quota_failure:
                    provider_failure = quota_failure[0]
                prompt_failure = None
                if prompt_outcome.get("status") == "error":
                    error = prompt_outcome.get("error")
                    prompt_failure = str(error.get("message") or "OMP prompt failed") if isinstance(error, Mapping) else str(error or "OMP prompt failed")
                result = {
                    **_stop_audit(run),
                    "ok": not emit_errors and provider_failure is None and prompt_failure is None,
                    "run_id": run_id,
                    "session_id": session_id,
                    "cancelled": run.cancel_requested,
                    "terminal": terminal,
                    "result": _safe_last_assistant_text(session.client),
                }
                if rollback_warnings:
                    result["rollback_warnings"] = rollback_warnings
                    result["result"] = (result["result"] or "") + rollback_notice
                if asset_notice is not None:
                    result["session_assets"] = session.client._omp_image_recovery
                if state:
                    result["omp_session_id"] = state.get("sessionId") or state.get(
                        "session_id"
                    )
                    result["omp_session_file"] = state.get(
                        "sessionFile"
                    ) or state.get("session_file")
                if provider_failure is not None:
                    # OMP still emits a terminal agent_end when its retry budget is
                    # exhausted (429 with a retry-after past retry.maxDelayMs, auth revoked,
                    # …); the process is idle and healthy but the turn delivered nothing.
                    # A mission driver that trusts `terminal` alone reported OK/completed
                    # on a 53-second turn with 77 bytes of text. The failure is the result.
                    result["stop_reason"] = "provider_error"
                    result["reason"] = provider_failure
                elif prompt_failure is not None:
                    result["stop_reason"] = "runtime_error"
                    result["reason"] = prompt_failure
                if emit_errors:
                    result["stop_reason"] = "runtime_error"
                    result["reason"] = (
                        f"omp event forwarding failed: {emit_errors[0]}"
                    )
                return result
        except KeyError:
            return _not_found()
        except Exception as exc:  # noqa: BLE001 - worker RPC boundary returns failures
            emit({"type": "run_end", "status": "cancelled" if run.cancel_requested else "failed",
                  "reason": str(exc)})
            return {
                "ok": False,
                "run_id": run_id,
                "session_id": session_id,
                "reason": str(exc),
                "stop_reason": "runtime_error",
                **({"code": "session_asset_error", "recoverable": True}
                   if isinstance(exc, SessionAssetError) else {}),
            }
        finally:
            with progress_lock:
                progress_closed = True
                if progress_timer is not None:
                    progress_timer.cancel()
                if budget_timer is not None:
                    budget_timer.cancel()
            for thread in progress_threads:
                if thread.is_alive():
                    thread.join(timeout=1)
            if run.questions is not None:
                run.questions.close()
            # Metadata-backed immutable contexts cannot carry the observer. Their
            # recovery ownership is independent of that optional callback wiring.
            # Rescan for recoveries lazily created by direct bindings during the run.
            _collect_host_tool_recoveries(
                params.get("host_tools"), tool_recoveries, contexts=observed_contexts,
            )
            for recovery in tool_recoveries.values():
                recovery.close()  # Forget results, never cancel/join native work.
            for context in observed_contexts:
                context.pop("tool_call_recovery", None)
                context.pop("host_call_observer", None)
                context.pop("ask_human", None)
                context.pop("helper_runtime", None)
                context.pop("user_steer_pending", None)
                context.pop("daemon_connections", None)
            if daemon_connections is not None:
                daemon_connections.close()
            # Touched documents remain until worker_rpc completes its save sweep.
            # This only discards agent-scoped buffers; it never closes live internal docs.
            try:
                from services.mcp.cad_common import release_agent_run_state
                release_agent_run_state(run_id)
            except Exception:  # noqa: BLE001 — cleanup cannot replace the turn result
                pass
            self._retire_run(run, session)

    def _register_run(self, params: Mapping[str, Any], *, steerable: bool = True) -> _Run | dict:
        """Register one controllable run, or return the refusal that prevents it.

        The run is cancellable from ``cancel(run_id)`` from here on, before its session
        exists; ``_retire_run`` must follow on every path that got a ``_Run``.
        """
        session_id = _nonempty_string(params.get("session_id")) or "default"
        run_id = _nonempty_string(params.get("run_id")) or uuid.uuid4().hex
        resume_file = _nonempty_string(params.get("omp_session_file"))
        session_key, run_key = tenancy.scoped_key(session_id), tenancy.scoped_key(run_id)
        if tenancy.in_cell() and resume_file is not None:
            return {"ok": False, "reason": "cell_refused", "code": "cell_refused"}
        try:
            credential_identity = _credential_identity(params)
            if tenancy.in_cell():
                resume_file = _session_link(session_id, credential_identity)
        except KeyError:
            return _not_found()
        except ValueError as exc:
            return {"ok": False, "reason": str(exc), "code": str(exc)}

        with self._lock:
            if self._shutdown:
                return {"ok": False, "reason": "omp session host is shut down"}
            if run_key in self._runs:
                return {
                    "ok": False,
                    "run_id": run_id,
                    "reason": "omp run_id is already active",
                }
            self._completed_runs.pop(run_key, None)
            early = self._prestart_cancels.pop(run_key, None)
            cancelled = early is not None and (not early[2] or early[1] == credential_identity)
            delegate = early[0] if cancelled else None
            run = _Run(
                run_id=run_id, session_id=session_id,
                credential_identity=credential_identity,
                registry_key=run_key, session_key=session_key,
                resume_file=resume_file, steerable=steerable,
                cancel_requested=cancelled,
                control_delegate=delegate,
                cancel_reason="manager_stop" if delegate else "",
            )
            self._runs[run_key] = run
        return run

    def _retire_run(self, run: _Run, session: _Session | None) -> None:
        """Release the run's session claim and record it as completed."""
        dispose = False
        with self._lock:
            if session is not None:
                dispose = self._release_session_locked(session)
            self._runs.pop(run.registry_key, None)
            completed = {
                "run_id": run.run_id,
                "session_id": run.session_id,
                "cancelled": (run.cancel_requested if run.terminal_cancelled is None
                              else run.terminal_cancelled),
                "started": run.started,
                "_owner": run.owner,
                "_credential_identity": run.credential_identity,
            }
            completed.update(_stop_audit(run))
            self._completed_runs[run.registry_key] = completed
            owned_completed = [key for key, value in self._completed_runs.items()
                               if value["_owner"] == run.owner]
            for key in owned_completed[:-self._max_sessions * 8]:
                self._completed_runs.pop(key)
        if dispose:
            self._finish_session_disposal(session)

    def run_compaction(self, params, emit) -> dict:
        """Run bounded maintenance without replacing or reconstructing its journal."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        if not isinstance(params, Mapping) or not callable(emit):
            return {"ok": False, "reason": "invalid compaction request"}
        compact = params.get("compact")
        if not isinstance(compact, Mapping):
            return {"ok": False, "reason": "compact must be an object"}
        mode, instructions = compact.get("mode"), compact.get("instructions")
        if mode is not None and mode not in _COMPACT_MODES:
            return {"ok": False, "reason": "unsupported compaction mode"}
        if instructions is not None and not isinstance(instructions, str):
            return {"ok": False, "reason": "compact instructions must be a string"}
        if mode == "remote" or (mode == "snapcompact" and instructions):
            return {"ok": False, "stop_reason": "compaction_refused",
                    "reason": "OMP does not support this exact manual compaction"}
        run = self._register_run(params, steerable=False)
        if isinstance(run, dict):
            return run
        refusal = {"ok": False, "outcome": "failed", "run_id": run.run_id, "session_id": run.session_id}
        emit = _context_callback(emit)
        session = None
        unsubscribe = None
        ended: dict[str, Any] = {}
        end_received = threading.Event()
        done = threading.Event()
        outcome: dict[str, Any] = {}
        retire_unsafe = False
        active = threading.Event()
        active.set()
        try:
            if run.cancel_requested:
                return {**refusal, "ok": True, "outcome": "cancelled",
                        "cancelled": True, **_stop_audit(run)}
            session = self._get_or_create_session(run.session_id, params, run.resume_file)
            if not session.turn_lock.acquire(timeout=_CONTROL_CALL_SECONDS):
                return {**refusal, "reason": "session_busy"}
            try:
                self._refresh_compaction(session, params)
                asset_notice = _session_image_notice(session.client)
                if asset_notice is not None:
                    emit(asset_notice)
                with self._lock:
                    if session.closed:
                        raise RuntimeError("OMP RPC session was closed")
                    run.client = session.client
                    self._sessions.move_to_end(run.session_key)
                try:
                    _bounded_call(lambda: self._configure_turn(session, params), 30.0)
                except _BoundedCallTimeout:
                    session.closed = True
                    session.client._omp_force_stop = True
                    raise
                state = _safe_client_state(session.client)
                _require_runtime_model(session.client, state, timeout=_CONTROL_CALL_SECONDS)
                identity = _session_identity_from_state(state)
                if run.resume_file and "omp_session_file" not in identity:
                    identity["omp_session_file"] = run.resume_file
                if run.cancel_requested:
                    return {**refusal, "ok": True, "outcome": "cancelled", "cancelled": True, **_stop_audit(run)}
                def forward(event: Any) -> None:
                    frame = _json_value(event)
                    if not active.is_set() or not isinstance(frame, dict):
                        return
                    if frame.get("type") == "manual_compaction_end":
                        ended.update(frame)
                        end_received.set()
                        return
                    emit(frame)
                unsubscribe = _register_listener(session.client, _context_callback(forward))
                with self._lock:
                    run.started = True
                def execute() -> None:
                    try:
                        arguments = {}
                        if mode is not None:
                            arguments["mode"] = mode
                        if instructions:
                            arguments["instructions"] = instructions
                        outcome["value"] = session.client._omp_bridge.call(
                            "compact", arguments, timeout=_MAINTENANCE_SECONDS)
                    except Exception as exc:
                        outcome["error"] = exc
                    finally:
                        done.set()
                threading.Thread(target=execute, name="omp-compaction", daemon=True).start()
                deadline = _monotonic() + _MAINTENANCE_SECONDS
                cancel_deadline = None
                watchdog_expired = False
                runtime_lost = False
                while not done.wait(0.1):
                    now = _monotonic()
                    if run.cancel_requested and cancel_deadline is None:
                        cancel_deadline = now + _BUDGET_ABORT_GRACE_SECONDS
                    if (cancel_deadline is not None and now >= cancel_deadline) or now >= deadline:
                        if not done.is_set():
                            watchdog_expired = True
                            break
                    if getattr(session.client, "_closed_error", None) is not None:
                        runtime_lost = True
                        break
                error = outcome.get("error") if done.is_set() else None
                unknown = (watchdog_expired or runtime_lost or isinstance(error, TimeoutError) or
                           (isinstance(error, BridgeError) and
                            error.code in {"timeout", "cancelled", "stopped"}))
                if unknown:
                    retire_unsafe = True
                    # The bridge discards an expired request, not the extension's
                    # still-running compact(). Prevent any reuse before aborting.
                    with self._lock:
                        session.closed = True
                        session.client._omp_force_stop = True
                    if not end_received.is_set() and not run.cancel_sent:
                        try:
                            _bounded_call(session.client.abort_and_restore_queue,
                                          _CONTROL_CALL_SECONDS)
                        except Exception:  # teardown below still retires this runtime
                            pass
                    if not end_received.is_set():
                        end_received.wait(_BUDGET_ABORT_GRACE_SECONDS)
                elif not end_received.is_set():
                    # Lifecycle frames and bridge replies use separate transports.
                    end_received.wait(_CONTROL_CALL_SECONDS)
                active.clear()
                value = outcome.get("value") if done.is_set() else None
                if (not isinstance(value, Mapping) or value.get("completed") is not True or
                        not isinstance(value.get("result"), Mapping)):
                    if isinstance(ended.get("result"), Mapping) and not ended.get("aborted"):
                        with self._lock:
                            run.terminal_cancelled = False
                        value = _committed_compaction_from_journal(session.client, ended["result"])
                        if value is None:
                            emit({"type": "context_update", "contextTokens": None})
                            emit({"type": "manual_compaction_end",
                                  "errorMessage": "Committed compaction method could not be verified"})
                            return {**refusal, "reason": "Committed compaction method could not be verified",
                                    "stop_reason": "compaction_failed",
                                    "omp_runtime_live": not session.closed, **identity}
                    else:
                        if unknown:
                            emit({"type": "context_update", "contextTokens": None})
                        if run.cancel_requested:
                            emit({"type": "manual_compaction_end", "aborted": True})
                            return {**refusal, "ok": not unknown, "outcome": "cancelled",
                                    "cancelled": True, "stop_reason": "cancelled",
                                    "reason": "Compaction did not settle" if unknown else "",
                                    "omp_runtime_live": not session.closed, **identity,
                                    **_stop_audit(run)}
                        if ended.get("aborted") or ended.get("skipped") or ended.get("errorMessage"):
                            emit(ended)
                            result_outcome = ("cancelled" if ended.get("aborted") else
                                              "skipped" if ended.get("skipped") else "failed")
                            return {**refusal, "outcome": result_outcome,
                                    "stop_reason": result_outcome,
                                    "reason": ended.get("errorMessage") or "Compaction did not commit",
                                    "omp_runtime_live": not session.closed, **identity}
                        if unknown:
                            raise RuntimeError("Compaction timed out; its outcome is unknown")
                        if error is not None:
                            raise error
                        raise RuntimeError("OMP compaction returned no committed result")
                with self._lock:
                    run.terminal_cancelled = False
                result = value["result"]
                compaction = dict(result)
                method = value.get("method")
                if not isinstance(method, str) or not method:
                    journal_value = _committed_compaction_from_journal(session.client, result)
                    if journal_value is not None:
                        method = journal_value["method"]
                if isinstance(method, str) and method:
                    compaction["method"] = method
                for key, prefix in (("contextBefore", "tokensBefore"), ("contextAfter", "tokensAfter")):
                    usage = value.get(key)
                    if not isinstance(usage, Mapping):
                        continue
                    tokens = usage.get("tokens")
                    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
                        compaction[prefix] = tokens
                        compaction[prefix + "Estimated"] = usage.get("estimated") is not False
                after = compaction.get("tokensAfter")
                if isinstance(after, int) and not isinstance(after, bool) and after >= 0:
                    emit({"type": "context_update", "contextTokens": after,
                          "source": "estimate" if compaction.get("tokensAfterEstimated") else "api"})
                else:
                    # The visible transcript is not the post-compaction context.
                    emit({"type": "context_update", "contextTokens": None})
                if not isinstance(method, str) or not method:
                    emit({"type": "manual_compaction_end",
                          "errorMessage": "Committed compaction method could not be verified"})
                    return {**refusal, "reason": "Committed compaction method could not be verified",
                            "stop_reason": "compaction_failed",
                            "omp_runtime_live": not session.closed, **identity}
                if mode is not None and method != mode:
                    emit({"type": "manual_compaction_end",
                          "errorMessage": "OMP did not commit the requested compaction method"})
                    return {**refusal, "reason": "OMP did not commit the requested compaction method",
                            "stop_reason": "compaction_failed",
                            "omp_runtime_live": not session.closed, **identity}
                emit({"type": "manual_compaction_end", "aborted": False, "result": compaction})
                return {**refusal, "ok": True, "outcome": "committed", "cancelled": False,
                        "compaction": compaction, "omp_runtime_live": not session.closed, **identity}
            finally:
                try:
                    if retire_unsafe:
                        # Keep the closed session registered and locked until
                        # its old process can no longer mutate the journal.
                        try:
                            _stop_client(session.client)
                        except Exception:
                            with self._lock:
                                session.dispose_started = True
                            raise
                        session.client_stopped = True
                finally:
                    session.turn_lock.release()
        except Exception as exc:
            if session is not None and getattr(session.client, "_closed_error", None) is not None:
                session.closed = True
            emit({"type": "manual_compaction_end", "aborted": run.cancel_requested,
                  "errorMessage": str(exc) or type(exc).__name__})
            return {**refusal, "outcome": "cancelled" if run.cancel_requested else "failed",
                    "cancelled": run.cancel_requested,
                    "stop_reason": "cancelled" if run.cancel_requested else "compaction_failed",
                    "reason": str(exc) or type(exc).__name__,
                    **({"code": "session_asset_error", "recoverable": True}
                       if isinstance(exc, SessionAssetError) else {}),
                    **({"omp_runtime_live": False} if session is not None and session.closed else {})}
        finally:
            active.clear()
            if unsubscribe is not None:
                unsubscribe()
            self._retire_run(run, session)

    def _await_turn_idle(self, run: _Run, session: _Session, budget_s: float | None) -> str:
        """Wait for actual session settlement, not the earlier prompt yield.

        One settlement probe (``wait_for_settled``, which opens with a ``get_state``
        request) is in flight at a time. A probe that outlives its slice is a busy
        runtime, not a dead one: its request queues behind OMP's serial commands and
        the event stream ahead of it. Closing the session for that (the old 1.5 s
        slack) killed healthy turns mid-stream with "OMP control request timed out",
        so the loop keeps honoring Stop, the budget and closure while the probe
        finishes, re-checking them every ``_HELPER_WAIT_SLICE_SECONDS``: waiting out
        the probe's whole slice left a stopped turn holding the GUI's composer for
        up to that slice. Only the probe's own failure (process exit, protocol
        error) ends the wait early.
        """
        deadline = _monotonic() + budget_s if budget_s is not None else None
        cancel_deadline = None
        probe: tuple[threading.Event, dict[str, Any]] | None = None
        while True:
            now = _monotonic()
            if run.cancel_requested:
                if cancel_deadline is None:
                    cancel_deadline = now + _BUDGET_ABORT_GRACE_SECONDS
                if now >= cancel_deadline:
                    return "cancel_unsettled"
            elif deadline is not None and now >= deadline:
                return "budget"
            if session.closed:
                raise RuntimeError("OMP RPC session was closed")
            wait_s = _UNBOUNDED_WAIT_SLICE_SECONDS
            if deadline is not None and not run.cancel_requested:
                wait_s = min(wait_s, max(0.01, deadline - now))
            if cancel_deadline is not None:
                wait_s = min(wait_s, max(0.01, cancel_deadline - now))
            if probe is None:
                probe = _start_call(
                    lambda timeout=wait_s: session.client.wait_for_settled(timeout=timeout))
            done, outcome = probe
            if not done.wait(_HELPER_WAIT_SLICE_SECONDS):
                continue
            probe = None
            if "error" not in outcome:
                return "idle"
            if not _is_wait_timeout(outcome["error"]):
                raise outcome["error"]

    def _cancelled_without_terminal(self, run: _Run, session: _Session) -> dict:
        """Report a Stop whose abort OMP never confirmed with a terminal frame."""
        _LOG.warning(
            "OMP run %s did not settle within %.0fs of its abort",
            run.run_id,
            _BUDGET_ABORT_GRACE_SECONDS,
        )
        # An unacknowledged abort is not evidence the agent stopped. Dispose this
        # connection on release so no work survives after its CAD context is gone.
        with self._lock:
            session.closed = True
        session.client._omp_force_stop = True
        result: dict[str, Any] = {
            "ok": True,
            "run_id": run.run_id,
            "session_id": run.session_id,
            "cancelled": True,
            "terminal": None,
            "result": _safe_last_assistant_text(session.client),
        }
        result.update(_stop_audit(run))
        state = _safe_client_state(session.client)
        if state:
            result["omp_session_id"] = state.get("sessionId") or state.get("session_id")
            result["omp_session_file"] = state.get("sessionFile") or state.get(
                "session_file"
            )
        return result

    def _stop_budget_exhausted_run(
        self,
        run: _Run,
        session: _Session,
        budget_s: float,
        terminal_seen: Callable[[], dict[str, Any] | None],
    ) -> dict:
        """Abort a run that outlived its budget and report it as ``turn_budget``."""
        self._cancel_run(run)
        try:
            _bounded_call(lambda: session.client.wait_for_settled(timeout=_BUDGET_ABORT_GRACE_SECONDS),
                          _BUDGET_ABORT_GRACE_SECONDS + _CONTROL_CALL_SECONDS)
        except Exception as exc:
            _LOG.warning("OMP run did not settle after budget abort (%s)", type(exc).__name__)
            session.closed = True
            session.client._omp_force_stop = True
        minutes = max(1, round(budget_s / 60.0))
        hours, mins = divmod(minutes, 60)
        budget_text = (
            f"{hours} h {mins} min" if hours and mins else f"{hours} h" if hours else f"{mins} min"
        )
        result: dict[str, Any] = {
            "ok": False,
            "run_id": run.run_id,
            "session_id": run.session_id,
            "cancelled": False,
            "stop_reason": "turn_budget",
            "reason": (
                f"This turn used its whole {budget_text} time budget while the agent "
                "was still working, so it was stopped. Everything it already saved is "
                'kept; send "continue" to pick up where it left off.'
            ),
            "terminal": terminal_seen(),
            "result": _safe_last_assistant_text(session.client),
        }
        state = _safe_client_state(session.client)
        if state:
            result["omp_session_id"] = state.get("sessionId") or state.get("session_id")
            result["omp_session_file"] = state.get("sessionFile") or state.get(
                "session_file"
            )
        return result

    def cancel(self, run_id, *, credential_params=None, allow_prestart=False) -> dict:
        try:
            delegate = tenancy.control_delegate("cancel")
        except ValueError:
            return _forbidden_right()
        if delegate is not None:
            if _nonempty_string(run_id) is None:
                return _forbidden_right()
            credential_params = None
        run_id = _nonempty_string(run_id)
        with self._lock:
            if run_id is None:
                if not tenancy.in_cell():
                    return {"ok": False, "reason": "run_id is required"}
                runs = [run for run in self._runs.values()
                        if _control_allowed(run, credential_params)]
            else:
                key = tenancy.scoped_key(run_id)
                run = self._runs.get(key)
                completed = self._completed_runs.get(key)
                if run is None:
                    if completed is None and (tenancy.in_cell() or allow_prestart):
                        if tenancy.in_cell():
                            try:
                                tenancy.require_handle(run_id, "run")
                            except KeyError:
                                return _not_found()
                        restricted = credential_params is not None and "byok_key" in credential_params
                        try:
                            identity = _credential_identity(credential_params) if restricted else None
                        except ValueError:
                            return _not_found()
                        self._prestart_cancels.setdefault(key, (delegate, identity, restricted))
                        return {
                            "ok": True, "run_id": run_id, "cancelled": True, "active": False,
                            **({"reason": "manager_stop", "control_delegate": delegate} if delegate else {}),
                        }
                    if not _control_allowed(completed, credential_params):
                        return _not_found() if tenancy.in_cell() or completed else {
                            "ok": True, "run_id": run_id, "cancelled": False, "active": False,
                        }
                    return {"ok": True, "run_id": run_id, "active": False,
                            "cancelled": bool(completed.get("cancelled"))}
                if not _control_allowed(run, credential_params):
                    return _not_found()
                runs = [run]
        results = [self._cancel_run(run) for run in runs]
        if delegate is not None:
            for result in results:
                result.pop("carried_steer_ids", None)
                result.setdefault("reason", "manager_stop")
                result["control_delegate"] = delegate
                result["stop_reason"] = "manager_stop"
        if run_id is not None:
            return results[0]
        return {"ok": all(result["ok"] for result in results), "cancelled_runs": results}

    def _cancel_run(self, run: _Run) -> dict:
        with self._lock:
            delegate = tenancy.control_delegate("cancel")
            if delegate is not None and not run.cancel_requested:
                run.control_delegate = dict(delegate)
                run.cancel_reason = "manager_stop"
            run.cancel_requested = True
            client = run.client
            should_abort = (client is not None and run.started and not run.cancel_sent
                            and not run.submitting)
            if run.submitting and client is not None:
                session = self._sessions.get(run.session_key)
                if session is not None:
                    session.closed = True
                client._omp_force_stop = True
            if should_abort:
                run.cancel_sent = True
            questions = run.questions
            connections = run.daemon_connections
        if connections is not None:
            connections.abort_active()
        if questions is not None:
            questions.close()
        carried: list[_Steer] = []
        if not should_abort:
            with self._lock:
                carried = list(run.pending_steers)
                run.pending_steers.clear()
                if carried:
                    self._carried_steers.setdefault(run.session_key, []).extend(carried)
        if should_abort:
            try:
                restored = _json_value(_bounded_call(client.abort_and_restore_queue,
                                                      _CONTROL_CALL_SECONDS))
                if not isinstance(restored, Mapping) or restored.get("imagesDropped") or restored.get("truncated"):
                    raise RuntimeError("OMP could not restore the complete queued input")
                items = list(restored.get("steering") or ()) + list(restored.get("followUp") or ())
                with self._lock:
                    pending = list(run.undelivered)
                    for item in items:
                        if not isinstance(item, Mapping):
                            continue
                        text = item.get("text") or ""
                        images = list(item.get("images") or ())
                        matched = next((s for s in pending if s.text == text and s.images == images), None)
                        if matched is not None:
                            pending.remove(matched)
                            run.undelivered.remove(matched)
                            carried.append(matched)
                        else:
                            carried.append(_Steer(text, images))
                    carried.extend(run.pending_steers)
                    run.pending_steers.clear()
                    if carried:
                        self._carried_steers.setdefault(run.session_key, []).extend(carried)
            except Exception as exc:
                with self._lock:
                    session = self._sessions.get(run.session_key)
                    if session is not None:
                        session.closed = True
                        client._omp_force_stop = True
                    carried.extend(run.undelivered)
                    carried.extend(run.pending_steers)
                    run.undelivered.clear()
                    run.pending_steers.clear()
                    if carried:
                        self._carried_steers.setdefault(run.session_key, []).extend(carried)
                return {"ok": False, "run_id": run.run_id, "cancelled": True,
                        "active": True, "reason": str(exc),
                        "carried_steer_ids": [s.steer_id for s in carried if s.steer_id]}
        return {"ok": True, "run_id": run.run_id, "cancelled": True, "active": True,
                "carried_steer_ids": [s.steer_id for s in carried if s.steer_id]}


    def _take_delivered_steers(
        self, run: _Run, frame: Mapping[str, Any], sent_prompt: Sequence[str]
    ) -> list[_Steer]:
        """Undelivered user steers a ``message_start`` user frame just delivered.

        Correlate exact sent text and the number of attached images only: an
        unrelated or transformed user frame is not evidence that any tracked steer
        was consumed. Image bytes cannot be compared: OMP re-encodes every
        image before it enters the model context (a pasted PNG streams back as
        WebP), keeping one image per input. Same-text steers leave its FIFO
        steering queue in send order, so the oldest match is the one delivered.
        """
        text = _user_frame_text(frame)
        if text is None or text in sent_prompt:
            return []
        with self._lock:
            if not run.undelivered:
                return []
            message = frame.get("message", {})
            content = message.get("content")
            image_count = sum(
                1 for part in content
                if isinstance(part, Mapping) and part.get("type") == "image"
            ) if isinstance(content, list) else 0
            taken = [
                s for s in run.undelivered
                if s.text == text and len(s.images) == image_count
            ][:1]
            for steer in taken:
                run.undelivered.remove(steer)
        return [s for s in taken if s.steer_id]

    def answer(self, run_id, batch_id, answers=None, declined=False, *, credential_params=None) -> dict:
        """Resolve a pending ask_user batch from the GUI's question card."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        run_key = _nonempty_string(run_id)
        if run_key is None:
            return {"ok": False, "reason": "run_id is required"}
        with self._lock:
            run = self._runs.get(tenancy.scoped_key(run_key))
            if not _control_allowed(run, credential_params) and (run is not None or tenancy.in_cell()):
                return _not_found()
            questions = run.questions if run is not None else None
        if questions is None:
            return {
                "ok": False,
                "run_id": run_key,
                "answered": False,
                "active": run is not None,
                "reason": "question is no longer pending",
            }
        result = questions.answer(batch_id, answers, declined=bool(declined))
        result["run_id"] = run_key
        return result

    def steer(
        self,
        run_id,
        text,
        attachments=None,
        *,
        from_user: bool = True,
        steer_id: str | None = None,
        credential_params=None,
    ) -> dict:
        """Queue ``text`` into a live run; the response echoes the GUI's ``steer_id``.

        A user steer stays tracked until its user frame streams (an ``agent_steer``
        ``delivered`` frame names it), wakes pure waits meanwhile, and a Stop before
        delivery carries it to the conversation's next prompt.
        """
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        steer_key = _nonempty_string(steer_id) or ""
        run_key = _nonempty_string(run_id)
        message = _nonempty_string(text)
        if run_key is None or message is None:
            return {
                "ok": False,
                "reason": "run_id and non-empty steering text are required",
            }
        with self._lock:
            run = self._runs.get(tenancy.scoped_key(run_key))
            if not _control_allowed(run, credential_params) and (run is not None or tenancy.in_cell()):
                return _not_found()
        try:
            message, images = _prompt_payload(message, attachments, None)
        except (OSError, ValueError) as exc:
            return {
                "ok": False,
                "run_id": run_key,
                "steered": False,
                "reason": f"steer attachment unreadable: {exc}",
            }
        steer = _Steer(message, list(images or []), steer_key, from_user)
        with self._lock:
            run = self._runs.get(tenancy.scoped_key(run_key))
            if run is None:
                return {
                    "ok": True,
                    "run_id": run_key,
                    "steer_id": steer_key,
                    "steered": False,
                    "active": False,
                }
            if not run.steerable:
                # OMP would queue it and start a turn once the compaction ends.
                return {
                    "ok": False,
                    "run_id": run_key,
                    "steer_id": steer_key,
                    "steered": False,
                    "active": True,
                    "reason": "compaction_in_progress",
                }
        # A user message sent while the agent waits on its question form IS the answer:
        # steering it instead would queue behind the blocked ask_user call forever.
        questions = run.questions if from_user else None
        if questions is not None and questions.has_pending():
            # A text-only reply is consumed by ask_user itself; its answer frame
            # acknowledges the bubble even if the control response is lost. Images
            # still need their own streamed user frame before claiming delivery.
            answered = questions.reply(message, steer_id=steer_key if not images else "")
            if answered is not None and not images:
                with self._lock:
                    run.steers.add(steer.key)
                return {
                    "ok": True,
                    "run_id": run_key,
                    "steer_id": steer_key,
                    "steered": True,
                    "active": True,
                    "answered_question": answered,
                }
        with run.control_lock:
            with self._lock:
                if steer.key in run.steers or any(
                    queued.key == steer.key for queued in run.pending_steers
                ):
                    return {
                        "ok": True,
                        "run_id": run_key,
                        "steer_id": steer_key,
                        "steered": True,
                        "active": True,
                        "duplicate": True,
                    }
                client = run.client
                send_now = client is not None and run.started and not run.cancel_requested
                if not send_now:
                    run.pending_steers.append(steer)
                elif from_user:
                    # Tracked before the send: its user frame may be forwarded first.
                    run.undelivered.append(steer)
            if send_now:
                try:
                    _bounded_call(lambda: client.steer(message, images=images or None),
                                  _CONTROL_CALL_SECONDS)
                except Exception as exc:  # noqa: BLE001 - RPC transport failures are retryable
                    # The runtime may have accepted the message before its control
                    # response failed. Keep correlation for a later user frame;
                    # absent that evidence the GUI still reports this call failed.
                    return {
                        "ok": False,
                        "run_id": run_key,
                        "steer_id": steer_key,
                        "steered": False,
                        "active": True,
                        "reason": str(exc),
                    }
                with self._lock:
                    run.steers.add(steer.key)
        return {
            "ok": True,
            "run_id": run_key,
            "steer_id": steer_key,
            "steered": True,
            "active": True,
            "queued": not send_now,
        }

    def status(self, run_id=None, session_id=None, *, credential_params=None) -> dict:
        """Return only the authenticated actor's control-plane snapshot."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        run_id = _nonempty_string(run_id)
        session_id = _nonempty_string(session_id)
        with self._lock:
            if run_id is not None:
                key = tenancy.scoped_key(run_id)
                active = self._runs.get(key)
                completed = self._completed_runs.get(key)
                record = active if active is not None else completed
                if not _control_allowed(record, credential_params):
                    if tenancy.in_cell() or record is not None:
                        return _not_found()
                    return {"ok": True, "run_id": run_id, "active": False,
                            "cancelled": False, "started": False}
                return {
                    "ok": True, "run_id": run_id, "active": active is not None,
                    "cancelled": active.cancel_requested if active else bool(completed.get("cancelled")),
                    "started": active.started if active else bool(completed.get("started")),
                    "session_id": active.session_id if active else completed["session_id"],
                }
            if session_id is not None:
                session = self._sessions.get(tenancy.scoped_key(session_id))
                if not _control_allowed(session, credential_params):
                    if tenancy.in_cell() or session is not None:
                        return _not_found()
                runs = sorted(run.run_id for run in self._runs.values()
                              if run.session_id == session_id and _control_allowed(run, credential_params))
                return {"ok": True, "session_id": session_id,
                        "live_session": session is not None and not session.closed,
                        "active_runs": runs}
            return {
                "ok": True,
                "sessions": sum(not s.closed and _control_allowed(s, credential_params)
                                for s in self._sessions.values()),
                "active_runs": sum(_control_allowed(r, credential_params) for r in self._runs.values()),
                "max_sessions": self._max_sessions, "shutdown": self._shutdown,
            }

    def worker_activity(self) -> dict:
        """Inspect existing ownership, including settle and event-only dictation."""
        from services.worker_activity import registry_lock

        owner = tenancy.owner_key()
        with registry_lock(self._lock):
            sessions = [s for s in self._sessions.values() if s.owner == owner]
            runs = sum(r.owner == owner for r in self._runs.values())
            clients = {s.client for s in sessions}
            helpers = [c for c in self._helper_clients
                       if getattr(c, "_chudware_activity_owner", None) == owner]
            clients.update(helpers)
            service = self._service_client
            if service is not None and getattr(service, "_chudware_activity_owner", None) == owner:
                clients.add(service)
            counts = {"agent_runs": runs,
                      "sdk_session_calls": sum(s.active_turns for s in sessions),
                      "sdk_reservations": self._reserved_creations.get(owner, 0),
                      "sdk_helpers": len(helpers),
                      "sdk_service_calls": int(self._service_lock.locked())}
            for client, probe in list(self._activity_probes.items()):
                if probe.done.is_set():
                    self._activity_probes.pop(client)
            probes = {}
            for client in clients:
                process = getattr(client, "_process", None)
                if process is not None and process.poll() is not None:
                    continue
                probe = self._activity_probes.get(client)
                if probe is None:
                    probe = _ActivityProbe()
                    self._activity_probes[client] = probe
                    thread = threading.Thread(
                        target=_context_callback(_probe_sdk_activity),
                        args=(client, probe), name="chudware-sdk-census", daemon=True)
                    try:
                        thread.start()
                    except BaseException:
                        self._activity_probes.pop(client, None)
                        raise
                probes[client] = probe
        processes = []
        idle_clients = []
        complete = True
        # SDK requests retain their own response IDs and ordinary timeout. Only
        # waiting for this census is bounded: do not change client timeouts, abort,
        # close or consume another request's response when it takes longer.
        deadline = time.monotonic() + 0.65
        for client, probe in probes.items():
            probe.done.wait(max(0.0, deadline - time.monotonic()))
            if not probe.done.is_set():
                complete = False
            evidence = probe.result.copy() if probe.result is not None else {}
            if not probe.complete:
                complete = False
            # Refresh local sources even while a single-flight state request is
            # pending: notifications and SDK calls may have arrived since it began.
            live_evidence, local_complete = _live_sdk_activity(client, probe)
            if not local_complete:
                complete = False
            for kind, count in live_evidence.items():
                evidence[kind] = max(evidence.get(kind, 0), count)
            for kind, count in evidence.items():
                counts[kind] = counts.get(kind, 0) + count
            process = getattr(client, "_process", None)
            if any(evidence.values()):
                if process is not None and process.poll() is None:
                    processes.append(process)
            elif process is not None and probe.done.is_set() and probe.complete and local_complete:
                idle_clients.append(client)
        return {"by_kind": counts, "processes": processes,
                "idle_clients": idle_clients, "complete": complete}

    def close_session(self, session_id, *, credential_params=None) -> dict[str, Any]:
        """Dispose a live process without deleting or rewriting its session file."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        key = _nonempty_string(session_id)
        if key is None:
            return {"ok": True}
        with self._lock:
            session = self._sessions.get(tenancy.scoped_key(key))
            if not _control_allowed(session, credential_params) and (session is not None or tenancy.in_cell()):
                return _not_found()
            if session is None or session.closed:
                return {"ok": True}
            session.closed = True
            runs = [run for run in self._runs.values()
                    if run.session_key == (session.registry_key or key)]
            for run in runs:
                run.cancel_requested = True
            dispose = self._dispose_if_idle_locked(session)
        for run in runs:
            result = self._cancel_run(run)
            if not result["ok"]:
                _LOG.warning("OMP abort during session disposal failed")
        if dispose:
            self._finish_session_disposal(session)
        return {"ok": True}

    def release_session(self, session_id, *, credential_params=None) -> dict[str, Any]:
        """Stop an idle runtime before another worker resumes its persisted history."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        key = _nonempty_string(session_id)
        with self._lock:
            session = self._sessions.get(tenancy.scoped_key(key)) if key is not None else None
            if not _control_allowed(session, credential_params) and (session is not None or tenancy.in_cell()):
                return _not_found()
            if session is None:
                return {"ok": True, "released": False}
            if session.closed:
                return {"ok": False, "reason": "session_busy"}
            if session.active_turns > 0 or any(
                run.session_key == (session.registry_key or key) for run in self._runs.values()
            ):
                return {"ok": False, "reason": "session_busy"}
            session.closed = True
            session.dispose_started = True
        # A timed-out control request can leave its stop running in a handler thread.
        # Keep the closing session visible to release and creation until the journal
        # is flushed; a failed stop retains the tombstone rather than granting reuse.
        self._finish_session_disposal(session)
        return {"ok": True, "released": True}

    def export_session(self, session_id, output_path=None) -> dict[str, Any]:
        """Export one live transcript through OMP and atomically publish it."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        session = self._claim_live_session(session_id)
        if session is None:
            return _missing_session()
        temporary: Path | None = None
        try:
            with session.turn_lock:
                if session.closed:
                    return _missing_session()
                try:
                    target = _project_export_path(
                        session.workspace_dir, session.session_id, output_path
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                except (OSError, TypeError, ValueError) as exc:
                    return {"ok": False, "reason": str(exc)}
                temporary = target.parent / (
                    f".{target.name}.{uuid.uuid4().hex}.omp-export.tmp"
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
                            "Agent service export_html did not create the requested file"
                        )
                    if (
                        exported.is_symlink()
                        or exported.absolute() != temporary.absolute()
                        or exported.resolve().parent != target.parent.resolve()
                    ):
                        raise RuntimeError(
                            "Agent service export_html returned an unexpected output path"
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
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        key = _nonempty_string(session_id)
        branch_entry = _nonempty_string(entry_id)
        if key is None:
            return _missing_session()
        if branch_entry is None:
            return {"ok": False, "reason": "entry_id is required"}
        session = self._claim_live_session(key)
        if session is None:
            return _missing_session()
        dispose = False
        try:
            with session.turn_lock:
                if session.closed:
                    return _missing_session()
                raw = _invoke_session_canonical(
                    session.client, "branch", {"entryId": branch_entry}
                )
                dispose = True
                normalized = _json_value(raw)
                if not isinstance(normalized, Mapping):
                    raise TypeError("Agent session branch returned a non-object response")
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
                        "Agent session branch returned no child session metadata"
                    )
                if tenancy.in_cell():
                    _session_link(key, session.credential_identity, child_file)
                return {
                    "ok": True,
                    "omp_session_id": child_id,
                    "omp_session_file": child_file,
                }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {"ok": False, "reason": str(exc)}
        finally:
            self._release_live_session(session)
            if dispose:
                self.close_session(key)

    def shutdown(self):
        if _delegate_refusal() is not None:
            raise ValueError("forbidden_right")
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            sessions = tuple(self._sessions.values())
            service_client = self._service_client
            self._sessions.clear()
            self._service_client = None
            self._service_memory_backend = None
            helpers = tuple(self._helper_clients)
            self._helper_clients.clear()
            for session in sessions:
                session.closed = True
                session.dispose_started = True
            for run in self._runs.values():
                run.cancel_requested = True
        for session in sessions:
            _stop_client(session.client)
        if service_client is not None:
            _stop_client(service_client)
        for client in helpers:
            _stop_client(client)

    def transient_call(self, command, params=None, emit=None) -> dict[str, Any]:
        """Run a bounded service-level command without creating a saved session."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        command_name = _nonempty_string(command)
        if command_name not in _TRANSIENT_SHAPES:
            raise ValueError(
                f"unsupported agent-service transient command: {command_name!r}"
            )
        payload = _validated_payload(
            command_name,
            params if params is not None else {},
            _TRANSIENT_SHAPES[command_name],
        )
        _validate_service_payload(command_name, payload)
        if emit is not None and not callable(emit):
            raise TypeError("Agent-service transient emit callback must be callable")
        return self._transient_call(command_name, payload, emit=emit)

    def auth_call(self, command, params):
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        command_name = _nonempty_string(command)
        if command_name not in _AUTH_SHAPES:
            raise ValueError(f"unsupported agent-service auth command: {command_name!r}")
        payload = _validated_payload(command_name, params, _AUTH_SHAPES[command_name])
        if command_name == "auth_states":
            providers = payload.get("providers")
            if (not isinstance(providers, list) or len(providers) > 32 or
                    any(not isinstance(p, str) or not p or len(p) > 128 for p in providers)):
                raise ValueError("invalid auth provider batch")
        with self._service_lock:
            client = self._service_client_locked()
            return _json_value(client._omp_bridge.call(command_name, payload, timeout=30.0))

    def memory_call(self, command, params):
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        command_name = _nonempty_string(command)
        if command_name not in _MEMORY_SHAPES:
            raise ValueError(f"unsupported agent-service memory command: {command_name!r}")
        payload = _validated_payload(command_name, params, _MEMORY_SHAPES[command_name])
        root = _nonempty_string(payload.pop("root", None))
        if root is None or not Path(root).is_dir():
            raise ValueError("memory root must be an existing directory")
        if "kind" in payload:
            raise ValueError("memory kind is not supported by OMP")
        client = self._make_client({"workspace_dir": str(Path(root).resolve())}, no_session=True)
        try:
            _start_client(client)
            return _json_value(client._omp_bridge.call(command_name, payload, timeout=30.0))
        finally:
            _stop_client(client)

    def complete_text(
        self,
        system: str,
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        timeout: float = _HELPER_TIMEOUT_SECONDS,
        cancel: Callable[[], bool] | None = None,
        compaction_settings: Mapping[str, Any] | None = None,
        runtime_params: Mapping[str, Any] | None = None,
    ) -> str:
        """Run one private, tool-free, unsaved helper through compiled RPC v2.

        Auth, provider requests and retry remain runtime-owned. The helper has a private
        cwd, no tools/skills/rules, memory off and the selected compaction policy.
        Explicit model rejection never substitutes a model. The positive helper budget
        covers startup, selection and prompt; cancellation/shutdown disposes its process.
        RPC startup/stop have their own bounded transport grace. No conversation is
        created or capped; RPC v2 exposes no per-request output-token limit.
        """
        if _delegate_refusal() is not None:
            raise HelperCompletionError("forbidden_right")
        message = _nonempty_string(prompt)
        if message is None:
            raise ValueError("helper completion prompt is empty")
        budget = _turn_budget_seconds(timeout)
        if budget is None:
            raise ValueError("helper completion requires a positive finite timeout")
        provider_id = _nonempty_string(provider)
        model_id = _nonempty_string(model)
        if model_id and not provider_id and "/" in model_id:
            provider_id, model_id = model_id.split("/", 1)
        if bool(provider_id) != bool(model_id):
            raise HelperCompletionError("helper completion model selection needs both provider and model")
        if cancel is not None and cancel():
            raise HelperCompletionError("helper completion cancelled")
        with self._lock:
            if self._shutdown:
                raise HelperCompletionError("agent service host is shut down")

        client = None
        registered = False
        workdir = None
        unsubscribe = None
        credential_dir = None
        helper_bridge = None
        helper_bridge_dir = None
        worker = None
        done = threading.Event()
        outcome: dict[str, Any] = {}
        deadline = _monotonic() + budget
        try:
            factory = self._rpc_factory()
            binary = self._resolve_binary(required=self._client_factory is None)
            scoped_params = runtime_params or {}
            if compaction_settings is None:
                inherited_compaction = _turn_compaction_settings(scoped_params)
                if inherited_compaction:
                    compaction_settings = inherited_compaction
                else:
                    from services.agent.config import online_compaction_settings
                    _, compaction_settings = online_compaction_settings()
            workdir = tempfile.TemporaryDirectory(
                prefix="chudware-helper-",
                dir=tenancy.actor_state_root() if tenancy.in_cell() else None,
            )
            kwargs: dict[str, Any] = {
                "tools": [],
                "no_session": True,
                "no_skills": True,
                "no_rules": True,
                "rpc_defaults": True,
                "startup_timeout": min(30.0, budget),
                "request_timeout": min(30.0, budget),
                "cwd": workdir.name,
            }
            # A helper's prefix (its own system prompt, no tools) differs from the
            # conversation's. Sharing the conversation's provider session would
            # pollute its prompt-cache routing, so derive a key per (conversation, system).
            provider_session_id = _nonempty_string(scoped_params.get("provider_session_id"))
            if provider_session_id is not None:
                helper_digest = hashlib.sha256((_nonempty_string(system) or "").encode("utf-8")).hexdigest()[:12]
                kwargs["provider_session_id"] = f"{provider_session_id}-helper-{helper_digest}"
            if _credential_scope(scoped_params) is not None:
                credential_dir, _, clean_env = _credential_client_scope(
                    scoped_params, workdir.name, None)
                kwargs["env"] = clean_env
            system_text = _nonempty_string(system)
            if system_text is not None:
                kwargs["append_system_prompt"] = system_text
            if binary is not None:
                kwargs["executable"] = binary
                if not _binary_supports_flag(binary, "--config"):
                    raise HelperCompletionError("helper runtime requires --config support")
                overlay = _cad_session_overlay("off", compaction_settings)
                if overlay is None:
                    raise HelperCompletionError("helper runtime settings overlay unavailable")
                extra = ("--no-lsp", "--config", overlay)
                if _binary_supports_flag(binary, "--no-mcp"):
                    extra += ("--no-mcp",)
                kwargs["extra_args"] = extra
            from services.agent.omp_bridge import ExtensionBridge
            helper_bridge_dir = tempfile.TemporaryDirectory(prefix="omp-helper-bridge-", dir=_bridge_parent())
            def reject_tool(_name, _args, _context):
                raise RuntimeError("Private helper tools are disabled")
            helper_bridge = ExtensionBridge(helper_bridge_dir.name, reject_tool).start()
            helper_bridge.publish_tools([])
            helper_bridge.publish_keyless_providers(_keyless_provider_opt_ins())
            bridge_info = helper_bridge.info()
            extension = Path(sys.executable).parent / "omp_extension.ts"
            if not extension.is_file():
                extension = Path(__file__).resolve().with_name("omp_extension.ts")
            kwargs["extra_args"] = (*kwargs.get("extra_args", ()), "--no-extensions",
                                    "--trusted-extension", str(extension))
            env = dict(kwargs.get("env", {}))
            env.update({"OMP_CAD_BRIDGE_TRANSPORT": bridge_info["transport"],
                        "OMP_CAD_BRIDGE_ENDPOINT": bridge_info["endpoint"],
                        "OMP_CAD_BRIDGE_TOKEN": bridge_info["token"]})
            kwargs["env"] = env
            _sandbox_client_kwargs(kwargs, {**scoped_params, "mode": "ask"})
            client = factory(**kwargs)
            client._omp_bridge = helper_bridge
            client._omp_bridge_dir = helper_bridge_dir
            client._chudware_tempdir = credential_dir or workdir
            with self._lock:
                if self._shutdown:
                    raise HelperCompletionError("agent service host is shut down")
                self._helper_clients.add(client)
                registered = True
            _start_client(client, timeout=max(0.01, min(35.0, deadline - _monotonic())))
            with self._lock:
                if self._shutdown:
                    raise HelperCompletionError("agent service host is shut down")

            frames: deque[dict[str, Any]] = deque(maxlen=256)
            quota_failure: list[str] = []

            def record(event: Any) -> None:
                frame = _json_value(event)
                if not isinstance(frame, dict):
                    return
                quota = demo_lane.frame_quota_message(frame)
                if quota is not None:
                    quota_failure[:] = [quota]
                    _abort_retry(client)
                if (frame.get("type") in _RETRY_FAILED_FRAMES
                        or frame.get("type") in _ASSISTANT_OUTPUT_FRAMES):
                    frames.append(frame)

            unsubscribe = _register_listener(client, _context_callback(record))

            def complete() -> None:
                try:
                    if provider_id and model_id:
                        try:
                            client.set_model(provider_id, model_id)
                        except Exception as exc:
                            raise HelperCompletionError(
                                _describe_model_rejection(client, provider_id, model_id, exc)
                            ) from exc
                    settings = scoped_params.get("settings") or {}
                    thinking = settings.get("reasoning_effort") or settings.get("thinking")
                    if thinking and thinking != "auto":
                        thinking = _catalog_effort(client, provider_id, model_id, thinking)
                        if thinking:
                            client.set_thinking_level(thinking)
                    for key in ("auto_compaction", "auto_retry"):
                        if isinstance(settings.get(key), bool):
                            getattr(client, "set_" + key)(settings[key])
                    remaining = deadline - _monotonic()
                    if remaining <= 0 or (cancel is not None and cancel()):
                        return
                    _require_runtime_model(client, _safe_client_state(client),
                                           timeout=max(0.01, min(_CONTROL_CALL_SECONDS, remaining)))
                    remaining = deadline - _monotonic()
                    if remaining <= 0 or (cancel is not None and cancel()):
                        return
                    turn = client.prompt_and_wait(message, timeout=remaining)
                    failure = _unrecovered_provider_failure(frames)
                    if failure is not None:
                        raise HelperCompletionError(quota_failure[0] if quota_failure else failure)
                    text = turn.assistant_text
                    if not isinstance(text, str) or not text.strip():
                        raise HelperCompletionError("helper completion returned no assistant text")
                    outcome["text"] = text
                except Exception as exc:  # one failure class at the public boundary
                    outcome["error"] = exc
                finally:
                    done.set()

            worker = threading.Thread(target=_context_callback(complete), name="chudware-helper", daemon=True)
            worker.start()
            while True:
                with self._lock:
                    if self._shutdown:
                        raise HelperCompletionError("agent service host is shut down")
                if cancel is not None and cancel():
                    raise HelperCompletionError("helper completion cancelled")
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    raise HelperCompletionError(
                        f"helper completion exceeded its {budget:g} s budget"
                    )
                if done.wait(min(_HELPER_WAIT_SLICE_SECONDS, remaining)):
                    if "error" in outcome:
                        raise outcome["error"]
                    if "text" in outcome:
                        return outcome["text"]
        except HelperCompletionError:
            raise
        except Exception as exc:  # setup, transport and protocol failures are honest result data
            raise HelperCompletionError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception as exc:
                    _LOG.warning("Helper unsubscription failed (%s)", type(exc).__name__)
            if client is not None:
                with self._lock:
                    owned = client in self._helper_clients
                    self._helper_clients.discard(client)
                if owned or not registered:
                    _stop_client(client)
                if worker is not None:
                    worker.join(timeout=5.0)
            elif helper_bridge_dir is not None:
                if helper_bridge is not None:
                    helper_bridge.stop()
                helper_bridge_dir.cleanup()
            if credential_dir is not None:
                credential_dir.cleanup()
            if workdir is not None:
                workdir.cleanup()

    def session_call(self, session_id, command, params=None) -> dict[str, Any]:
        """Call one named live session; these calls never create or resume sessions."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        command_name = _nonempty_string(command)
        if command_name not in _SESSION_SHAPES:
            raise ValueError(f"unsupported agent-session command: {command_name!r}")
        payload = _validated_payload(
            command_name,
            params if params is not None else {},
            _SESSION_SHAPES[command_name],
        )
        if command_name in {"set_auto_compaction", "set_auto_retry"} and not isinstance(
            payload.get("enabled"), bool
        ):
            raise TypeError(f"{command_name} enabled must be a boolean")
        if command_name == "handoff" and "customInstructions" in payload and not isinstance(
            payload["customInstructions"], str
        ):
            raise TypeError("handoff customInstructions must be a string")
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
            return _missing_session()
        try:
            if command_name in {"tool_state", "get_state", "get_session_stats"}:
                if command_name == "tool_state":
                    result = session.client._omp_bridge.call("tool_state", {}, timeout=_CONTROL_CALL_SECONDS)
                else:
                    result = _bounded_call(
                        lambda: _invoke_session_canonical(session.client, command_name, payload),
                        _CONTROL_CALL_SECONDS)
                normalized = _json_value(result)
                if not isinstance(normalized, dict):
                    raise TypeError("OMP session state is not an object")
                normalized.setdefault("ok", True)
                if command_name == "get_state":
                    normalized["_hostToolNames"] = sorted(_session_host_tool_names(session))
                return normalized
            if not session.turn_lock.acquire(timeout=_CONTROL_CALL_SECONDS):
                return {"ok": False, "reason": "session_busy"}
            try:
                if session.closed:
                    return _missing_session()
                if command_name == "handoff":
                    _require_runtime_model(session.client, _safe_client_state(session.client),
                                           timeout=_CONTROL_CALL_SECONDS)
                    try:
                        result = session.client._omp_bridge.call("handoff", payload, timeout=90.0)
                    except Exception as exc:
                        if getattr(exc, "code", None) not in {"busy", "unsupported", "invalid_request"}:
                            session.closed = True
                            session.client._omp_force_stop = True
                        raise
                    if not isinstance(result, Mapping) or not isinstance(result.get("document"), str):
                        raise RuntimeError("OMP handoff returned no document")
                    return {"ok": True, **result}
                try:
                    result = _bounded_call(
                        lambda: _invoke_session_canonical(session.client, command_name, payload),
                        _CONTROL_CALL_SECONDS)
                except TimeoutError:
                    session.closed = True
                    session.client._omp_force_stop = True
                    raise
                normalized = _json_value(result)
                if normalized is None:
                    normalized = {"ok": True}
                if not isinstance(normalized, dict):
                    raise TypeError(
                        f"Agent service {command_name} returned a non-object response"
                    )
                normalized.setdefault("ok", True)
                if command_name in {"set_auto_compaction", "set_auto_retry"}:
                    normalized.setdefault("enabled", payload["enabled"])
                    if command_name == "set_auto_compaction":
                        session.auto_compaction = payload["enabled"]
                    else:
                        session.auto_retry = payload["enabled"]
                if command_name == "get_state":
                    normalized["_hostToolNames"] = sorted(
                        _session_host_tool_names(session)
                    )
                return normalized
            finally:
                session.turn_lock.release()
        finally:
            self._release_live_session(session)

    def session_launch_params(self, session_id) -> dict[str, Any]:
        """Private child-call inheritance; never expose credential bytes over RPC."""
        if _delegate_refusal() is not None:
            raise ValueError("forbidden_right")
        key = _nonempty_string(session_id)
        with self._lock:
            session = self._sessions.get(tenancy.scoped_key(key)) if key else None
            if not _control_allowed(session) or session.closed:
                raise KeyError("not_found")
            return dict(session.launch_params)

    def _claim_live_session(self, session_id) -> _Session | None:
        key = _nonempty_string(session_id)
        if key is None:
            return None
        with self._lock:
            session = self._sessions.get(tenancy.scoped_key(key))
            if session is None or session.closed or not _control_allowed(session):
                return None
            session.active_turns += 1
            self._sessions.move_to_end(tenancy.scoped_key(key))
            return session

    def _release_session_locked(self, session: _Session) -> bool:
        session.active_turns -= 1
        return self._dispose_if_idle_locked(session)

    def _dispose_if_idle_locked(self, session: _Session) -> bool:
        if session.closed and session.active_turns == 0 and not session.dispose_started:
            session.dispose_started = True
            # Leave the closing session registered until its process is stopped.
            return True
        return False

    def _finish_session_disposal(self, session: _Session) -> None:
        if not session.client_stopped:
            _stop_client(session.client)
        with self._lock:
            session.client_stopped = True
            key = session.registry_key or session.session_id
            if self._sessions.get(key) is session:
                self._sessions.pop(key)

    def _release_live_session(self, session: _Session) -> None:
        with self._lock:
            dispose = self._release_session_locked(session)
        if dispose:
            self._finish_session_disposal(session)

    def _get_or_create_session(
        self, session_id: str, params: Mapping[str, Any], resume_file: str | None
    ) -> _Session:
        workspace = _nonempty_string(params.get("workspace_dir"))
        workspace_dir = (
            str(Path(workspace).expanduser().resolve())
            if workspace is not None
            else None
        )
        credential_identity = _credential_identity(params)
        session_key = tenancy.scoped_key(session_id)
        owner = tenancy.owner_key()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("omp session host is shut down")
            existing = self._claim_existing_session_locked(
                session_id, resume_file, workspace_dir, credential_identity
            )
            if existing is not None:
                return existing
            guard = self._creating.setdefault(session_key, _CreationGuard())
            guard.users += 1
        try:
            with guard.lock:
                reserved = False
                try:
                    with self._lock:
                        if self._shutdown:
                            raise RuntimeError("omp session host is shut down")
                        existing = self._claim_existing_session_locked(
                            session_id, resume_file, workspace_dir, credential_identity
                        )
                        if existing is not None:
                            return existing
                        victim = self._evict_for_new_session()
                        self._reserved_creations[owner] = self._reserved_creations.get(owner, 0) + 1
                        reserved = True
                    if victim is not None:
                        self._finish_session_disposal(victim)
                    client = self._make_client(
                        params, no_session=False, resume_file=resume_file
                    )
                    try:
                        _start_client(client)
                        if resume_file:
                            resume_path = Path(resume_file).expanduser()
                            if not resume_path.is_file():
                                raise RuntimeError(
                                    f"persisted agent session file does not exist: {resume_path}"
                                )
                            client._omp_image_recovery = recover_session_image_assets(client, resume_path)
                            try:
                                _bounded_call(lambda: _resume_session(client, str(resume_path)), 30.0)
                            except _BoundedCallTimeout:
                                client._omp_force_stop = True
                                raise
                        with self._lock:
                            if self._shutdown:
                                raise RuntimeError("omp session host is shut down")
                            session = _Session(
                                session_id,
                                client,
                                resume_file,
                                workspace_dir,
                                credential_identity=credential_identity,
                                active_turns=1,
                                compaction_settings={},
                                launch_params=_client_launch_params(params),
                                registry_key=session_key,
                                sandbox_class=_sandbox_class(params),
                            )
                            self._sessions[session_key] = session
                            self._reserved_creations[owner] -= 1
                            reserved = False
                        return session
                    except Exception:  # noqa: BLE001 - even RPC-specific errors need teardown
                        _stop_client(client)
                        raise
                finally:
                    if reserved:
                        with self._lock:
                            self._reserved_creations[owner] -= 1
        finally:
            with self._lock:
                guard.users -= 1
                if not guard.users:
                    self._creating.pop(session_key, None)

    def _claim_existing_session_locked(
        self, session_id: str, resume_file: str | None, workspace_dir: str | None,
        credential_identity: str | None,
    ) -> _Session | None:
        session_key = tenancy.scoped_key(session_id)
        existing = self._sessions.get(session_key)
        if existing is None:
            return None
        if existing.closed:
            raise RuntimeError("agent RPC session is closing")
        if not tenancy.owns(existing.owner) or existing.credential_identity != credential_identity:
            raise KeyError("not_found")
        if (
            resume_file
            and existing.resume_file
            and resume_file != existing.resume_file
        ):
            raise RuntimeError(
                "cannot change omp_session_file on a live Chudware session"
            )
        if existing.workspace_dir is None and workspace_dir is not None:
            existing.workspace_dir = workspace_dir
        self._sessions.move_to_end(session_key)
        existing.active_turns += 1
        return existing

    def _evict_for_new_session(self) -> _Session | None:
        owned = sum(tenancy.owns(session.owner) and not session.closed
                    for session in self._sessions.values())
        if owned + self._reserved_creations.get(tenancy.owner_key(), 0) < self._max_sessions:
            return None
        victim_key = next(
            (
                key
                for key, value in self._sessions.items()
                if tenancy.owns(value.owner) and value.active_turns == 0 and not value.closed
            ),
            None,
        )
        if victim_key is None:
            raise RuntimeError("all bounded agent sessions are currently active")
        victim = self._sessions[victim_key]
        victim.closed = True
        victim.dispose_started = True
        return victim

    def _rpc_factory(self) -> Any:
        """The injected client factory, else the pinned ``omp_rpc.RpcClient``."""
        if self._client_factory is not None:
            return self._client_factory
        try:
            from omp_rpc import RpcClient  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Agent service RPC support is unavailable; install the managed "
                "omp-rpc runtime dependency"
            ) from exc
        return RpcClient

    def _make_client(
        self,
        params: Mapping[str, Any],
        *,
        no_session: bool,
        resume_file: str | None = None,
    ) -> Any:
        factory = self._rpc_factory()

        binary = self._resolve_binary(required=self._client_factory is None)
        session_dir = _nonempty_string(params.get("omp_session_dir"))
        tempdir = None
        clean_env = None
        if _credential_scope(params) is not None:
            tempdir, session_dir, clean_env = _credential_client_scope(
                params, session_dir, resume_file
            )
        elif session_dir is None and resume_file:
            session_dir = str(Path(resume_file).expanduser().parent)
        kwargs: dict[str, Any] = {
            "no_session": no_session,
            "rpc_defaults": True,
            # Conversation turns observe frames through listeners (run_turn's `forward`)
            # and wait with wait_for_settled; nothing replays this client's own history
            # (only prompt_and_wait/collect_events do, on helper clients). Every retained
            # message_update holds the whole streamed message twice: one 155k-character
            # thinking block streamed in 100-character deltas pinned ~360 MB, and the old
            # 1,000,000-frame ring kept that for the session's life.
            "max_event_history": 1,
        }
        if no_session:
            from services.agent.config import memory_backend
            memory = memory_backend() if params.get("workspace_dir") else "off"
            overlay = _cad_session_overlay(memory)
            if overlay:
                kwargs["extra_args"] = ("--config", overlay)
        if clean_env is not None:
            kwargs["env"] = clean_env
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
        if not no_session:
            # Maintenance is bounded separately; control writes have shorter host bounds.
            kwargs["request_timeout"] = _MAINTENANCE_SECONDS
            # A CAD conversation re-reads OMP's prompt on every request. Without
            # these, a session carried the user's own OMP skills catalog (18 KB of
            # repo-dev skills; CAD skills come from cad_skill), LSP docs, and a dozen
            # code devices (security_scan, debug, firmware, cpp_*, ast_*, github ...):
            # 106 KB of prompt + tool schema, 72 KB after (measured with get_state).
            kwargs["no_skills"] = True
            # The user's own OMP MCP servers are not CAD tools.
            extra: tuple[str, ...] = ("--no-lsp",)
            if binary is not None:
                template = _cad_system_prompt_template(binary)
                if template is not None:
                    extra += ("--system-prompt-template", str(template))
            if binary is not None and _binary_supports_flag(binary, "--no-mcp"):
                extra += ("--no-mcp",)
            memory = _session_memory_backend(params, workspace)
            overlay = (
                _cad_session_overlay(memory)
                if binary is not None and _binary_supports_flag(binary, "--config")
                else None
            )
            if tenancy.in_cell() and overlay is None:
                raise RuntimeError("cell_refused")
            if overlay is None and _turn_compaction_settings(params) and self._client_factory is None:
                raise RuntimeError("Cannot apply the selected compaction settings: runtime --config support is required")
            tools = list(CAD_SESSION_TOOLS)
            if overlay is not None:
                extra += ("--config", overlay)
                tools += _CAD_MEMORY_TOOLS[memory]
            kwargs["tools"] = tools
            kwargs["extra_args"] = extra
        from services.agent.omp_bridge import ExtensionBridge

        bridge_dir = tempfile.TemporaryDirectory(prefix="omp-cad-", dir=_bridge_parent())
        asset_staging = (Path(bridge_dir.name) / "images"
                         if tempdir is None and not tenancy.in_cell() and not no_session else None)
        if asset_staging is not None:
            asset_staging.mkdir(mode=0o700)
        bindings: dict[str, Any] = {}
        def dispatch(name: str, args: dict, context: Any) -> Any:
            binding = bindings.get(name)
            if binding is None:
                raise ValueError("CAD tool is not authorized in this turn")
            execute = binding.to_host_tool(lambda **kw: kw["execute"])
            return execute(args, context)
        bridge = ExtensionBridge(bridge_dir.name, dispatch).start()
        # OMP rebuilds and freezes the base prompt while switching to a saved
        # transcript. Start with the authorized bootstrap descriptors, not an
        # empty or already-expanded catalog; persisted loads are applied after
        # the switch and before the next model request.
        initial = params.get("host_tools") or ()
        bindings.update({binding.name: binding for binding in initial})
        bridge.publish_tools([binding.as_dict() for binding in initial])
        bridge.publish_keyless_providers(_keyless_provider_opt_ins())
        info = bridge.info()
        extension = Path(sys.executable).parent / "omp_extension.ts"
        if not extension.is_file():
            extension = Path(__file__).resolve().with_name("omp_extension.ts")
        kwargs["extra_args"] = (*kwargs.get("extra_args", ()), "--no-extensions",
                                "--trusted-extension", str(extension))
        asset_journal = Path(resume_file).expanduser().resolve() if resume_file else None
        env = dict(kwargs.get("env", {}))
        env.update({"OMP_CAD_BRIDGE_TRANSPORT": info["transport"],
                    "OMP_CAD_BRIDGE_ENDPOINT": info["endpoint"],
                    "OMP_CAD_BRIDGE_TOKEN": info["token"],
                    "OMP_CAD_ASSET_STAGING_DIR": str(asset_staging) if asset_staging else "",
                    "OMP_CAD_ASSET_JOURNAL": str(asset_journal) if asset_journal else ""})
        kwargs["env"] = env
        try:
            _sandbox_client_kwargs(kwargs, params)
            client = factory(**kwargs)
            client._omp_bridge = bridge
            client._omp_bridge_dir = bridge_dir
            client._omp_bindings = bindings
            client._omp_asset_staging_dir = asset_staging
            client._omp_asset_journal = asset_journal
            client._omp_asset_recovery_available = True
            client._chudware_activity_owner = tenancy.owner_key()
            client._chudware_activity_sandboxed = tenancy.in_cell()
            client._chudware_activity_sdk_argv = (
                tuple(kwargs["command"][4:]) if tenancy.in_cell()
                else tuple(getattr(client, "command", ())))
            if tempdir is not None:
                client._chudware_tempdir = tempdir
            return client
        except Exception:
            bridge.stop()
            bridge_dir.cleanup()
            if tempdir is not None:
                tempdir.cleanup()
            raise

    def _resolve_binary(self, *, required: bool) -> str | None:
        if self._binary_hint:
            resolved, error = _binary_candidate("explicit binary", self._binary_hint)
            if error:
                raise RuntimeError(error)
            return resolved

        configured = _nonempty_string(os.environ.get("CHUDWARE_OMP_BIN"))
        if configured:
            resolved, error = _binary_candidate("CHUDWARE_OMP_BIN", configured)
            if error:
                raise RuntimeError(error)
            return resolved

        executable_name = "omp.exe" if os.name == "nt" else "omp"
        adjacent = Path(sys.executable).parent / executable_name
        resolved, _error = _binary_candidate("signed payload", str(adjacent))
        if resolved:
            return resolved

        if _developer_mode():
            found = shutil.which(executable_name)
            if found:
                resolved, _error = _binary_candidate("developer PATH", found)
                if resolved:
                    return resolved

        if not required:
            return None
        raise RuntimeError(
            "Agent service executable not found; set CHUDWARE_OMP_BIN, install the "
            "signed-payload-adjacent omp binary, or put omp on PATH in developer mode"
        )

    def _apply_session_compaction(self, session: _Session, settings: Mapping[str, Any],
                                  *, timeout: float = 30.0) -> Any:
        if not session.policy_lock.acquire(timeout=min(0.1, timeout)):
            raise TimeoutError("OMP compaction policy is busy")
        try:
            if session.closed:
                raise RuntimeError("OMP session is closed")
            if settings == session.compaction_settings:
                return None
            result = session.client._omp_bridge.call(
                "settings_set", {"compaction": dict(settings)}, timeout=timeout)
            session.compaction_settings = dict(settings)
            return result
        finally:
            session.policy_lock.release()

    def apply_compaction_policy(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Apply persisted policy to each live session owned by this caller."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        if not isinstance(settings, Mapping):
            raise TypeError("compaction policy must be an object")
        with self._lock:
            sessions = [session for session in self._sessions.values()
                        if not session.closed and _control_allowed(session)]
            for session in sessions:
                session.active_turns += 1
        results: list[dict[str, Any]] = []
        results_lock = threading.Lock()
        def apply(session: _Session) -> None:
            try:
                effective = self._apply_session_compaction(session, settings, timeout=1.0)
                if effective is None:
                    effective = session.client._omp_bridge.call("settings_get", {}, timeout=1.0)
                result = {"session_id": session.session_id, "ok": True, "effective": effective}
            except Exception as exc:
                result = {"session_id": session.session_id, "ok": False, "reason": str(exc)}
            finally:
                self._release_live_session(session)
            with results_lock:
                results.append(result)
        threads = [threading.Thread(target=_context_callback(lambda s=s: apply(s)), daemon=True)
                   for s in sessions]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 1.5
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with results_lock:
            finished = list(results)
        completed_ids = {item["session_id"] for item in finished}
        pending = [session.session_id for session in sessions if session.session_id not in completed_ids]
        finished.extend({"session_id": session_id, "ok": False, "pending": True,
                         "reason": "policy update timed out"} for session_id in pending)
        if pending:
            return {"ok": False, "sessions": finished, "pending": pending,
                    "reason": "policy update timed out"}
        return {"ok": all(item["ok"] for item in finished), "sessions": finished, "pending": []}

    def _refresh_compaction(self, session: _Session, params: Mapping[str, Any]) -> None:
        """Apply policy in the live owning runtime, never by replacing its journal."""
        if _sandbox_class(params) != session.sandbox_class:
            raise RuntimeError("Cannot change a live session's sandbox class")
        self._apply_session_compaction(session, _turn_compaction_settings(params))
        session.launch_params = _client_launch_params(params)

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
            try:
                client.set_model(provider, model)
            except Exception as exc:  # noqa: BLE001 - RpcCommandError or a test double's error
                raise RuntimeError(
                    _describe_model_rejection(client, provider, model, exc)
                ) from exc
        # The composer is a per-turn user choice; stale role/Auto `thinking` must
        # never override it. Only clamp against a matching live catalog ladder.
        thinking = (_nonempty_string(params.get("reasoning_effort")) or
                    _nonempty_string(settings.get("reasoning_effort")) or
                    _nonempty_string(settings.get("thinking")))
        if thinking and thinking != "auto":
            thinking = _catalog_effort(client, provider, model, thinking)
            if thinking:
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
            session.auto_compaction = settings["auto_compaction"]
        if isinstance(settings.get("auto_retry"), bool):
            client.set_auto_retry(settings["auto_retry"])
            session.auto_retry = settings["auto_retry"]
        subscription = _nonempty_string(settings.get("subagent_subscription"))
        if subscription:
            client.request_raw("set_subagent_subscription", level=subscription)

        registrar = params.get("register_callbacks")
        if callable(registrar):
            registrar(client)

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
            publish_host_tools(client, host_tools, session.host_tool_names)
        host_uris = params.get("host_uris")
        if isinstance(host_uris, Sequence) and not isinstance(host_uris, (str, bytes)):
            client.set_host_uris(host_uris)
        session.launch_params = _client_launch_params(params)

    def _service_client_locked(self) -> Any:
        """Return the shared no-session process for the current GUI memory backend.

        Caller holds ``_service_lock``. Recycle on a settings change so explicit-root
        memory commands and conversations use the same selected backend; auth/catalog
        calls continue to share this single service process.
        """
        if tenancy.in_cell():
            raise ValueError("credential_isolation_unsupported")
        from services.agent.config import memory_backend

        backend = memory_backend()
        client = self._service_client
        if (client is not None and getattr(client, "_closed_error", None) is None
                and self._service_memory_backend == backend):
            return client
        if client is not None:
            _stop_client(client)
            with self._lock:
                if self._service_client is client:
                    self._service_client = None
                    self._service_memory_backend = None
        client = self._make_client({}, no_session=True)
        try:
            _start_client(client)
        except Exception:
            _stop_client(client)
            raise
        with self._lock:
            stopping = self._shutdown
            if not stopping:
                self._service_client = client
                self._service_memory_backend = backend
        if stopping:
            _stop_client(client)
            raise RuntimeError("omp session host is shut down")
        return client


    def warm_service(self) -> None:
        """Spawn the shared service process ahead of the first transient call.

        Best-effort and idempotent; the worker starts it on a background thread so
        the GUI's first provider/catalog query does not pay the ~1.1 s spawn.
        """
        try:
            with self._service_lock:
                self._service_client_locked()
        except Exception as exc:  # noqa: BLE001 - warm-up never fails the worker
            _LOG.debug("OMP service warm-up skipped (%s)", type(exc).__name__)

    def _transient_call(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("omp session host is shut down")
        with self._service_lock:
            client = self._service_client_locked()
            unsubscribe: Callable[[], None] | None = None
            try:
                if command == "login":
                    unsubscribe = _register_login_ui(client, emit)
                result = _invoke_canonical(client, command, payload)
            except Exception:
                # The process died under this call (or shutdown stopped it): drop it so
                # the next transient call spawns a fresh one instead of failing forever.
                if getattr(client, "_closed_error", None) is not None:
                    with self._lock:
                        if self._service_client is client:
                            self._service_client = None
                    _stop_client(client)
                raise
            finally:
                if unsubscribe is not None:
                    try:
                        unsubscribe()
                    except Exception as exc:  # noqa: BLE001 - teardown must continue
                        _LOG.debug(
                            "OMP login UI teardown failed (%s)", type(exc).__name__
                        )
            normalized = _json_value(result)
            if not isinstance(normalized, dict):
                raise TypeError(f"Agent service {command} returned a non-object response")
            return normalized


def _project_export_path(
    workspace_dir: str | None, session_id: str, output_path: Any
) -> Path:
    if workspace_dir is None:
        raise ValueError("project_root_unavailable")
    root = Path(workspace_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("project_root_unavailable")
    if output_path is None:
        basename = (uuid.uuid4().hex if tenancy.in_cell()
                    else re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip(".-"))
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


_IMAGE_BLOB_PREFIX = "blob:sha256:"
_IMAGE_BLOB_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_SESSION_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_SESSION_RECORD_BYTES = 128 * 1024 * 1024


def _session_image_hashes(session_file: Path) -> tuple[list[str], int, set[str]]:
    """Audit image positions the loader hydrates, without interpreting history."""
    hashes: dict[str, None] = {}
    eager: set[str] = set()
    malformed = 0

    def add(value: Any, *, hydrate: bool = True, report_invalid: bool = False) -> bool:
        nonlocal malformed
        if not isinstance(value, str) or not value.startswith(_IMAGE_BLOB_PREFIX):
            return False
        digest = value[len(_IMAGE_BLOB_PREFIX):]
        if not _IMAGE_BLOB_HASH.fullmatch(digest):
            if report_invalid:
                malformed += 1
            return False
        hashes[digest] = None
        if hydrate:
            eager.add(digest)
        return True

    try:
        with session_file.open("rb") as stream:
            while line := stream.readline(_MAX_SESSION_RECORD_BYTES + 1):
                if len(line) > _MAX_SESSION_RECORD_BYTES:
                    raise SessionAssetError("journal record exceeds the image inspection limit")
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # OMP's JSONL loader also skips malformed records.
                    continue
                if not isinstance(entry, dict) or entry.get("type") == "session":
                    continue
                stack: list[tuple[Any, str | None]] = [(entry, None)]
                while stack:
                    value, key = stack.pop()
                    if isinstance(value, list):
                        stack.extend((item, key) for item in reversed(value)
                                     if isinstance(item, (dict, list)))
                        continue
                    kind = value.get("type")
                    mime = value.get("mimeType")
                    payload = kind == "image" or (
                        isinstance(mime, str) and mime.lower().startswith("image/"))
                    if payload and (key in {"images", "frames"} or
                                    key == "content" and kind == "image"):
                        if add(value.get("data"), hydrate=key != "frames",
                               report_invalid=True) and key != "frames":
                            continue
                    if kind == "image_generation_call":
                        add(value.get("result"), report_invalid=True)
                    elif kind == "computer_call_output" and isinstance(value.get("output"), dict):
                        add(value["output"].get("image_url"), report_invalid=True)
                    # OMP eagerly visits these even in custom/off-branch
                    # metadata. Missing is optional unless context selects it;
                    # an unsafe existing file must never reach that loader.
                    add(value.get("image_url"), report_invalid=key == "screenshot" or
                        kind in {"input_image", "output_image", "image_url", "computer_screenshot"})
                    stack.extend((child, child_key)
                                 for child_key, child in reversed(list(value.items()))
                                 if isinstance(child, (dict, list)))
    except OSError as exc:
        raise SessionAssetError("cannot inspect the owning journal") from exc
    return list(hashes), malformed, eager


@contextmanager
def _open_legacy_image(digest: str):
    """Open exactly one legacy file while pinning each non-symlink parent."""
    with ExitStack() as stack:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class FileInformation(ctypes.Structure):
                _fields_ = [
                    ("attributes", wintypes.DWORD),
                    ("creation", wintypes.FILETIME), ("access", wintypes.FILETIME),
                    ("write", wintypes.FILETIME), ("volume", wintypes.DWORD),
                    ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                    ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
                    ("index_low", wintypes.DWORD),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            information = kernel.GetFileInformationByHandle
            information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
            information.restype = wintypes.BOOL
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL

            def open_component(path: Path, directory: bool):
                # OPEN_REPARSE_POINT examines the component itself; no delete
                # sharing pins each parent against replacement while reading.
                handle = create(str(path), 0x80 if directory else 0x80000000, 3, None, 3,
                                0x00200000 | (0x02000000 if directory else 0), None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                details = FileInformation()
                if not information(handle, ctypes.byref(details)):
                    error = ctypes.get_last_error()
                    close(handle)
                    raise ctypes.WinError(error)
                is_directory = bool(details.attributes & 0x10)
                if details.attributes & 0x400 or is_directory != directory:
                    close(handle)
                    raise OSError("legacy asset path is a reparse point or wrong file type")
                return handle

            path = Path.home().resolve()
            for component in (None, ".chudcode", "agent", "blobs"):
                if component is not None:
                    path /= component
                handle = open_component(path, True)
                stack.callback(close, handle)
            handle = open_component(path / digest, False)
            try:
                source_fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                close(handle)
                raise
        else:
            if (os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY")
                    or not hasattr(os, "O_NOFOLLOW")):
                raise OSError("secure legacy access is unavailable")
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            directory = os.open(Path.home().resolve(), directory_flags)
            stack.callback(os.close, directory)
            for component in (".chudcode", "agent", "blobs"):
                directory = os.open(component, directory_flags, dir_fd=directory)
                stack.callback(os.close, directory)
            source_fd = os.open(digest, file_flags, dir_fd=directory)
        source = stack.enter_context(os.fdopen(source_fd, "rb"))
        yield source


def _stage_legacy_session_image(digest: str, staging_dir: Path) -> Path:
    """Stage one verified blob, without following any legacy path symlink."""
    if not isinstance(digest, str) or not _IMAGE_BLOB_HASH.fullmatch(digest):
        raise SessionAssetError("invalid image reference")
    target = staging_dir / digest
    try:
        with ExitStack() as stack:
            source = stack.enter_context(_open_legacy_image(digest))
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SESSION_IMAGE_BYTES:
                raise SessionAssetError(f"{_IMAGE_BLOB_PREFIX}{digest}: unsafe or oversized legacy asset")
            target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
            destination = stack.enter_context(os.fdopen(target_fd, "wb"))
            hasher = hashlib.sha256()
            total = 0
            while True:
                block = source.read(min(1024 * 1024, _MAX_SESSION_IMAGE_BYTES + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > _MAX_SESSION_IMAGE_BYTES:
                    raise SessionAssetError(f"{_IMAGE_BLOB_PREFIX}{digest}: legacy asset exceeds size limit")
                hasher.update(block)
                destination.write(block)
            if total != info.st_size or hasher.hexdigest() != digest:
                raise SessionAssetError(f"{_IMAGE_BLOB_PREFIX}{digest}: corrupt legacy asset")
        return target
    except FileNotFoundError as exc:
        raise SessionAssetError(f"{_IMAGE_BLOB_PREFIX}{digest}: missing legacy asset") from exc
    except OSError as exc:
        raise SessionAssetError(f"{_IMAGE_BLOB_PREFIX}{digest}: unsafe or unreadable legacy asset") from exc


def recover_session_image_assets(client: Any, session_file: str | Path) -> dict[str, Any]:
    """Import exact references and ask OMP which ones the resumed context uses.

    The journal is read, never opened as a session or modified. Desktop legacy
    candidates are opportunistic; scoped runtimes never consult that store.
    """
    if not getattr(client, "_omp_asset_recovery_available", False):
        raise SessionAssetError("image recovery requires a fresh, unopened runtime")
    if tenancy.in_cell() and not getattr(client, "_chudware_activity_sandboxed", False):
        raise SessionAssetError("image recovery refused across tenant scope")
    path = Path(session_file).expanduser().resolve()
    bound = getattr(client, "_omp_asset_journal", None)
    if bound is None or path != Path(bound).resolve():
        raise SessionAssetError("image recovery requires the runtime's owning journal")
    hashes, malformed, eager = _session_image_hashes(path)
    unavailable: dict[str, str] = {}
    unsafe_current: set[str] = set()
    imported = 0
    bridge = getattr(client, "_omp_bridge", None)
    staging = getattr(client, "_omp_asset_staging_dir", None)
    if (hashes or malformed) and bridge is None:
        raise SessionAssetError("the session image store is unavailable")

    def recover_batch(candidates: list[str]) -> None:
        nonlocal imported
        missing: list[str] = []
        for offset in range(0, len(candidates), 128):
            batch = candidates[offset:offset + 128]
            try:
                response = bridge.call("cad_image_blobs_status", {"hashes": batch}, timeout=60.0)
            except BridgeError as exc:
                raise SessionAssetError("the session image store could not be queried") from exc
            rows = response.get("states") if isinstance(response, Mapping) else None
            by_hash = ({row.get("hash"): row.get("state") for row in rows if isinstance(row, Mapping)}
                       if isinstance(rows, list) and len(rows) == len(batch) else {})
            if set(by_hash) != set(batch):
                raise SessionAssetError("the session image store returned an invalid status")
            for digest in batch:
                state = by_hash[digest]
                if state == "missing":
                    missing.append(digest)
                elif state == "invalid":
                    unsafe_current.add(digest)
                    unavailable[digest] = "corrupt or unsafe current asset"
                elif state != "present":
                    raise SessionAssetError("the session image store returned an invalid status")
        for digest in missing:
            if staging is None or tenancy.in_cell():
                unavailable[digest] = "legacy recovery refused for this credential or tenant scope"
                continue
            target = Path(staging) / digest
            try:
                _stage_legacy_session_image(digest, Path(staging))
                response = bridge.call("cad_image_blob_import", {"hash": digest}, timeout=60.0)
                if not isinstance(response, Mapping) or response.get("hash") != digest:
                    raise SessionAssetError("invalid image import response")
                state = response.get("state")
                if state == "imported" and response.get("ref") == _IMAGE_BLOB_PREFIX + digest:
                    imported += 1
                elif state != "present":
                    raise SessionAssetError("image import failed or asset is corrupt")
            except (BridgeError, SessionAssetError) as exc:
                unavailable[digest] = str(exc)
                # A failed import may still have changed the canonical path.
                # Do not pass a raced unsafe file to OMP's eager loader.
                try:
                    checked = bridge.call("cad_image_blobs_status",
                                          {"hashes": [digest]}, timeout=60.0)
                except BridgeError as status_error:
                    raise SessionAssetError("cannot verify an image after failed recovery") from status_error
                rows = checked.get("states") if isinstance(checked, Mapping) else None
                if (not isinstance(rows, list) or len(rows) != 1 or
                        not isinstance(rows[0], Mapping) or rows[0].get("hash") != digest or
                        rows[0].get("state") not in {"missing", "present", "invalid"}):
                    raise SessionAssetError("the session image store returned an invalid status")
                if rows[0]["state"] == "invalid":
                    unsafe_current.add(digest)
            finally:
                target.unlink(missing_ok=True)

    recover_batch(hashes)
    required: set[str] = set()
    frames_selected: set[str] = set()
    frames_seen: set[str] = set()
    frames_missing: set[str] = set()
    malformed_frame = False
    if hashes or malformed:
        # OMP's public loader and standalone builder select the actual branch,
        # compaction, reset and native replay without opening a session or
        # hydrating any journal blob. The control returns only image hashes.
        for _ in range(3):
            try:
                projection = bridge.call("cad_image_context_refs", {}, timeout=120.0)
            except BridgeError as exc:
                raise SessionAssetError("the session image context could not be inspected") from exc
            if not isinstance(projection, Mapping):
                raise SessionAssetError("the session image context returned an invalid projection")
            def projected_set(key: str) -> set[str]:
                values = projection.get(key)
                if (not isinstance(values, list) or len(values) > 32768 or
                        any(not isinstance(value, str) or not _IMAGE_BLOB_HASH.fullmatch(value) for value in values)):
                    raise SessionAssetError("the session image context returned an invalid projection")
                return set(values)
            required = projected_set("required")
            frames_seen = projected_set("framesSeen")
            frames_selected = projected_set("framesSelected")
            frames_missing = projected_set("framesUnavailable")
            if (not frames_missing <= frames_seen or not frames_selected <= frames_seen or
                    type(projection.get("malformedRequired")) is not bool or
                    type(projection.get("malformedFrame")) is not bool):
                raise SessionAssetError("the session image context returned an invalid projection")
            if projection["malformedRequired"]:
                raise SessionAssetError("invalid image reference")
            malformed_frame = projection["malformedFrame"]
            extra = sorted((required | frames_seen) - set(hashes))
            if not extra:
                break
            hashes.extend(extra)
            recover_batch(extra)
        else:
            raise SessionAssetError("the session image context changed while recovering assets")
    for digest in frames_missing:
        unavailable.setdefault(digest, "archived frame is missing or unsafe")
    for digest in hashes:
        if (digest in unavailable and
                (digest in required or digest in unsafe_current and
                 (digest in eager or digest in frames_selected or digest in frames_missing))):
            raise SessionAssetError(f"{_IMAGE_BLOB_PREFIX}{digest}: {unavailable[digest]}")

    frame_unavailable = [{"reference": _IMAGE_BLOB_PREFIX + digest, "reason": unavailable[digest]}
                         for digest in hashes if digest in frames_seen and digest in unavailable]
    historical_unavailable = [{"reference": _IMAGE_BLOB_PREFIX + digest, "reason": unavailable[digest]}
                              for digest in hashes if digest not in frames_seen and digest in unavailable]
    frame_malformed = int(malformed_frame)
    historical_malformed = max(0, malformed - frame_malformed)
    if historical_unavailable or historical_malformed or frame_unavailable or frame_malformed:
        _LOG.warning("Session images unavailable: historical=%s malformed=%d frames=%s malformed_frames=%d",
                     historical_unavailable, historical_malformed, frame_unavailable, frame_malformed)
    return {"referenced": len(hashes), "present": len(hashes) - imported - len(unavailable),
            "imported": imported, "historical_unavailable": historical_unavailable,
            "historical_malformed": historical_malformed, "frame_unavailable": frame_unavailable,
            "frame_malformed": frame_malformed}


def _session_image_notice(client: Any) -> dict[str, str] | None:
    result = getattr(client, "_omp_image_recovery", None)
    if not isinstance(result, Mapping):
        return None
    historical = result.get("historical_unavailable")
    frames = result.get("frame_unavailable")
    old_count = (len(historical) if isinstance(historical, list) else 0) + result.get("historical_malformed", 0)
    frame_count = (len(frames) if isinstance(frames, list) else 0) + result.get("frame_malformed", 0)
    if not old_count and not frame_count:
        return None
    parts = []
    if old_count:
        parts.append(f"{old_count} older conversation image reference(s) could not be restored.")
    if frame_count:
        parts.append(f"{frame_count} archived context image frame(s) are unavailable or could not be verified.")
    return {"type": "notice", "text": " ".join(parts)}


def _resume_session(client: Any, session_file: str) -> None:
    """Switch ``client`` to a persisted session, or raise when OMP refuses.

    OMP cancels a resume whose recorded working directory differs from the
    process's and stays on a fresh, empty session. Continuing on that session would
    run the conversation with no history, so the refusal is an error.
    """
    client._omp_asset_recovery_available = False
    outcome = _json_value(client.switch_session(session_file))
    if isinstance(outcome, Mapping) and outcome.get("cancelled") is True:
        raise RuntimeError(
            f"session_file_mismatch: the agent service would not resume {session_file}; "
            "it was recorded in a different project folder than this conversation's"
        )


def _session_identity(client: Any) -> dict[str, Any]:
    """The runtime session id and file a result reports, when the runtime knows them."""
    return _session_identity_from_state(_safe_client_state(client))


def _session_identity_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    session_id = state.get("sessionId") or state.get("session_id")
    session_file = state.get("sessionFile") or state.get("session_file")
    if session_id:
        identity["omp_session_id"] = session_id
    if session_file:
        identity["omp_session_file"] = session_file
    return identity


def _active_branch(entries: Any, leaf_id: Any) -> list[Mapping[str, Any]]:
    """Root-to-leaf path of a runtime ``get_entries`` listing; abandoned siblings excluded."""
    if not isinstance(entries, list):
        raise TypeError("Agent session get_entries returned no entry list")
    by_id = {
        entry["id"]: entry
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    }
    path: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    current = leaf_id if isinstance(leaf_id, str) else None
    while current is not None and current not in seen and current in by_id:
        seen.add(current)
        entry = by_id[current]
        path.append(entry)
        parent = entry.get("parentId")
        current = parent if isinstance(parent, str) else None
    path.reverse()
    return path


def _committed_compaction_from_journal(client: Any, result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recover the exact method when a committed end precedes a lost bridge receipt."""
    if not isinstance(result.get("summary"), str) or not isinstance(result.get("firstKeptEntryId"), str):
        return None
    try:
        listing = _json_value(_bounded_call(lambda: client.request_raw("get_entries"),
                                            _CONTROL_CALL_SECONDS))
        branch = _active_branch(listing.get("entries"), listing.get("leafId"))
        entry = next((item for item in reversed(branch) if item.get("type") == "compaction"), None)
        if (not entry or entry.get("summary") != result["summary"] or
                entry.get("firstKeptEntryId") != result["firstKeptEntryId"] or
                not isinstance(entry.get("method"), str) or not entry["method"]):
            return None
        return {"completed": True, "result": dict(result), "method": entry["method"]}
    except (AttributeError, TypeError, RuntimeError, TimeoutError, ValueError):
        return None


def _invoke_canonical(client: Any, command: str, payload: Mapping[str, Any]) -> Any:
    """Use secret-safe typed wrappers when available; fakes may expose raw RPC only."""
    if command == "get_available_models":
        return {"models": _model_catalog(client)}
    method = getattr(client, command, None)
    if callable(method):
        if command == "get_login_providers":
            providers = method()
            return (
                providers
                if isinstance(providers, Mapping)
                else {"providers": providers}
            )
        if command == "login":
            return method(payload["providerId"])
        if command == "get_logout_accounts":
            return {"ok": True, "accounts": method(payload["providerId"])}
        if command == "logout":
            return method(payload["providerId"], payload["credentialId"])
    return client.request_raw(command, **payload)


def _invoke_session_canonical(
    client: Any, command: str, payload: Mapping[str, Any]
) -> Any:
    method = getattr(client, command, None)
    if callable(method):
        if command in {"get_session_stats", "get_state"}:
            try:
                return method()
            except ValueError:
                # The SDK may reject OMP's valid null context occupancy.
                raw = getattr(client, "request_raw", None)
                if not callable(raw):
                    raise
                return raw(command)
        if command == "get_messages":
            return {"messages": method()}
        if command in {"set_auto_compaction", "set_auto_retry"}:
            method(payload["enabled"])
            return {"ok": True, "enabled": payload["enabled"]}
        if command == "export_html":
            return {"path": str(method(payload.get("outputPath")))}
        if command == "branch":
            return method(payload["entryId"])
    return client.request_raw(command, **payload)


_EXECUTION_PHASES = {"tool_execution_start": "start", "tool_execution_end": "end"}


def _tool_execution_phase(frame: Mapping[str, Any]) -> tuple[str, str, str]:
    """(call id, "start"|"end"|"", tool name) of a OMP top-level tool frame."""
    kind = frame.get("type")
    body: Any = frame
    if kind == "custom":
        kind = frame.get("customType")
        body = frame.get("data")
    phase = _EXECUTION_PHASES.get(kind) if isinstance(kind, str) else None
    if phase is None or not isinstance(body, Mapping):
        return "", "", ""
    call_id = _nonempty_string(body.get("toolCallId"))
    if call_id is None:
        return "", "", ""
    return call_id, phase, _nonempty_string(body.get("toolName")) or ""


def _collect_host_tool_recoveries(
    host_tools: Any, recoveries: dict[int, Any], *,
    contexts: Sequence[dict[str, Any]] = (),
) -> None:
    """Retain each run-owned recovery, including metadata-only bindings."""
    sources = list(contexts)
    if isinstance(host_tools, Sequence) and not isinstance(host_tools, (str, bytes)):
        for binding in host_tools:
            sources.extend((getattr(binding, "context", None),
                            getattr(binding, "metadata", None)))
    if not sources:
        return
    from services.agent.agent_turn import _ToolCallRecovery
    from services.agent.omp_tools import _ctx

    for source in sources:
        recovery = _ctx(source, "tool_call_recovery", None)
        if isinstance(recovery, _ToolCallRecovery):
            recoveries[id(recovery)] = recovery


def _bind_host_call_observer(
    host_tools: Any, observer: Callable[[dict[str, Any]], None]
) -> list[dict[str, Any]]:
    """Hand the turn's nested-call observer to its host tools' shared run context.

    Bindings rebuilt mid-turn by ``enable_tools`` share the same context dict, so
    they report too. Returns the contexts to unbind when the turn ends.
    """
    if not isinstance(host_tools, Sequence) or isinstance(host_tools, (str, bytes)):
        return []
    contexts: dict[int, dict[str, Any]] = {}
    for binding in host_tools:
        context = getattr(binding, "context", None)
        if isinstance(context, dict):
            contexts[id(context)] = context
    for context in contexts.values():
        context["host_call_observer"] = observer
    return list(contexts.values())


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
                        "OMP login UI forwarding failed (%s)", type(exc).__name__
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
    raw = os.environ.get("CHUDWARE_OMP_MAX_SESSIONS", "")
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


def _binary_identity(path: str) -> tuple | None:
    """Stat identity of a resolved binary; an in-place upgrade changes it."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


_PROBE_OUTPUT: dict[tuple[str, tuple | None, tuple[str, ...]], str] = {}
_FLAG_SUPPORT_LOCK = threading.Lock()
_FLAG_PROBE_TIMEOUT_SECONDS = 10.0
_TEMPLATE_WARNED: set[tuple[str, tuple | None, str]] = set()


def _cad_system_prompt_template(binary: str) -> Path | None:
    """Select the shipped template once per CAD process, if the runtime supports it."""
    template = Path(sys.executable).parent / "omp_cad_system.md"
    if not template.is_file():
        template = Path(__file__).resolve().parent / "prompts" / "omp_cad_system.md"
    reason = ""
    if not _binary_supports_flag(binary, "--system-prompt-template"):
        reason = "runtime does not support --system-prompt-template"
    elif not template.is_file():
        reason = "CAD system prompt template is unavailable"
    if reason:
        key = (binary, _binary_identity(binary), reason)
        with _FLAG_SUPPORT_LOCK:
            first = key not in _TEMPLATE_WARNED
            _TEMPLATE_WARNED.add(key)
        if first:
            _LOG.warning("Using OMP's default system prompt: %s", reason)
        return None
    return template


def _binary_probe(path: str, args: tuple[str, ...]) -> str:
    """``<path> <args>`` stdout+stderr, run once per binary identity; "" when it fails."""
    key = (path, _binary_identity(path), args)
    if key[1] is None:  # nothing on disk to ask
        return ""
    with _FLAG_SUPPORT_LOCK:
        text = _PROBE_OUTPUT.get(key)
    if text is None:
        try:
            proc = subprocess.run([path, *args], capture_output=True, text=True,
                                  timeout=_FLAG_PROBE_TIMEOUT_SECONDS, check=False)
            text = f"{proc.stdout}\n{proc.stderr}"
        except (OSError, subprocess.SubprocessError):
            text = ""
        with _FLAG_SUPPORT_LOCK:
            _PROBE_OUTPUT[key] = text
    return text


def _binary_supports_flag(path: str, flag: str) -> bool:
    """Whether ``<path> --help`` lists ``flag``.

    A binary that cannot be probed (timeout, crash, missing) does not get the flag:
    passing an unknown flag would stop the session from starting at all.
    """
    text = _binary_probe(path, ("--help",))
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None




#: OMP settings every CAD conversation process runs with (``--config`` overlay).
#: A user-level context file is the user's own coding-agent brief, not CAD policy:
#: ~/.claude/CLAUDE.md ("git pull --rebase before starting; commit + push as you go")
#: reached every CAD turn, and the GPT-6 sessions of 2026-09-28 pulled, committed and
#: pushed the firmware repo inside the user's CAD project. Project-level context files
#: in the workspace still load: those are the user's instructions for this project.
_CAD_SESSION_SETTINGS = (
    "# Written by Chudware for its CAD conversation processes; regenerated on use.\n"
    "disabledExtensions:\n"
    "  - context-file:user:AGENTS.md\n"
    "  - context-file:user:CLAUDE.md\n"
    "  - context-file:user:GEMINI.md\n"
    "  - context-file:user:copilot-instructions.md\n"
    "browser.enabled: true\n"
    "computer.enabled: true\n"
    "async.enabled: true\n"
    "task.maxRuntimeMs: 0\n"
)
#: Per-backend memory settings. OMP keys every backend's store by the process cwd,
#: which is the CAD project folder, so memory never crosses projects; Mnemopi's scoping
#: is pinned so a user-level `global` setting cannot pool all projects into one bank.
_CAD_MEMORY_SETTINGS = {
    "off": "memory:\n  backend: \"off\"\n",
    "mnemopi": "memory:\n  backend: mnemopi\nmnemopi:\n  scoping: per-project\n",
    "local": "memory:\n  backend: local\n",
    "hindsight": "memory:\n  backend: hindsight\n",
    "sharpshooter": "memory:\n  backend: sharpshooter\n",
}
# Only list tools registered by the configured memory backend.
_CAD_MEMORY_TOOLS: dict[str, tuple[str, ...]] = {
    "off": (),
    "mnemopi": ("retain", "recall", "reflect"),
    "local": (),
    "hindsight": (),
    "sharpshooter": (),
}


def _session_memory_backend(params: Mapping[str, Any], workspace: str | None) -> str:
    """The memory backend for a CAD conversation process (``params["memory_backend"]``).

    Without a workspace the cwd is the services worker's own directory (the Chudware
    install or source tree), a store no CAD project owns: such a session gets none.
    """
    if workspace is None:
        return "off"
    backend = params.get("memory_backend")
    if backend in _CAD_MEMORY_SETTINGS:
        return backend
    from services.agent.config import DEFAULT_MEMORY_BACKEND

    return DEFAULT_MEMORY_BACKEND


def _client_launch_params(params: Mapping[str, Any]) -> dict[str, Any]:
    # Retain process identity, not a turn's prompt, callbacks, or CAD tool bindings.
    launch = {key: params[key] for key in (
        "workspace_dir", "omp_session_dir", "system_prompt", "memory_backend",
        "byok_key", "byok_tenant", "byok_provider",
        "mode", "plan_only", "turn_preferences",
    ) if key in params}
    settings = params.get("settings")
    if isinstance(settings, Mapping):
        launch["settings"] = {key: settings[key] for key in (
            "provider", "model", "compaction_strategy", "compaction_settings",
        ) if key in settings}
    return launch


def _turn_compaction_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    settings = params.get("settings")
    value = settings.get("compaction_settings") if isinstance(settings, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _cad_session_overlay(memory: str, compaction: Mapping[str, Any] | None = None) -> str | None:
    """Immutable policy overlay: one session must never rewrite another's settings."""
    from services import paths

    content = _CAD_SESSION_SETTINGS + _CAD_MEMORY_SETTINGS[memory]
    if tenancy.in_cell():
        content = content.replace("browser.enabled: true", "browser.enabled: false")
        content = content.replace("computer.enabled: true", "computer.enabled: false")
    if compaction:
        content += "compaction: " + json.dumps(dict(compaction), sort_keys=True) + "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    root = tenancy.actor_state_root() if tenancy.in_cell() else Path(paths.ROOT)
    target = root / "omp" / f"cad-session-{memory}-{digest}.yml"
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".cad-session-", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, target)
    except OSError as exc:
        if compaction or tenancy.in_cell():
            raise RuntimeError("Cannot write the selected compaction settings") from exc
        _LOG.warning("CAD session settings overlay unavailable: %s", exc)
        return None
    return str(target)


def _binary_candidate(source: str, candidate: str) -> tuple[str | None, str | None]:
    path = Path(candidate).expanduser()
    if not path.is_file() or (os.name != "nt" and not os.access(path, os.X_OK)):
        return None, f"{source} is not an executable file: {path}"
    if _looks_like_source_script(path):
        return (
            None,
            f"{source} points to a source/Bun script, not a compiled omp binary: {path}",
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
        raise TypeError("omp RpcClient does not expose an event listener API")
    unsubscribe = register(listener)
    return unsubscribe if callable(unsubscribe) else (lambda: None)


def _stop_client(client: Any) -> None:
    try:
        _teardown_client(client)
    finally:
        hold = getattr(client, "_omp_launch_hold", None)
        if hold is not None:
            hold.set()


def _teardown_client(client: Any) -> None:
    def kill_owned_process() -> None:
        process = getattr(client, "_process", None)
        if process is None or process.poll() is not None:
            return
        try:
            pgid = getattr(client, "_pgid", None)
            if os.name != "nt" and pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
    if getattr(client, "_omp_force_stop", False):
        kill_owned_process()
    bridge = getattr(client, "_omp_bridge", None)
    if bridge is not None:
        bridge.stop()
        client._omp_bridge = None
    bridge_dir = getattr(client, "_omp_bridge_dir", None)
    if bridge_dir is not None:
        bridge_dir.cleanup()
        client._omp_bridge_dir = None
    stop = getattr(client, "stop", None)
    if callable(stop):
        try:
            stopped = _bounded_call(stop, 3.0)
            if stopped is False:
                raise RuntimeError("OMP process teardown left a surviving process")
        except _BoundedCallTimeout:
            kill_owned_process()
            raise RuntimeError("OMP process teardown did not settle") from None
        except Exception as exc:
            _LOG.warning("OMP RPC client teardown failed (%s)", type(exc).__name__)
            raise
    tempdir = getattr(client, "_chudware_tempdir", None)
    if tempdir is not None:
        tempdir.cleanup()
        client._chudware_tempdir = None


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _install_sdk_activity_listener(client: Any) -> None:
    if isinstance(getattr(client, "_chudware_activity_dictation", None), _DictationActivity):
        return
    register = getattr(client, "on_notification", None)
    if not callable(register):
        return
    tracker = _DictationActivity()
    try:
        register(tracker.observe)
        client._chudware_activity_dictation = tracker
    except Exception:
        # Missing observation must not change normal session startup. The census
        # will report unknown instead of assuming dictation is idle.
        _LOG.debug("OMP dictation activity listener unavailable")


def _start_client(client: Any, *, timeout: float = 35.0) -> None:
    # Dictation has start/transcript/end notifications, but no status RPC.
    # Subscribe before launch so even an early capture cannot go unobserved.
    _install_sdk_activity_listener(client)
    if type(client).__module__.startswith("omp_rpc."):
        from services.worker_activity import note_sdk_launch

        note_sdk_launch(sandboxed=bool(getattr(client, "_chudware_activity_sandboxed", False)))
    abandoned = threading.Event()
    # bubblewrap's --die-with-parent is prctl(PR_SET_PDEATHSIG), which the kernel delivers when
    # the *thread* that forked the child exits, not when its process does. client.start() forks
    # the (sandboxed) RPC process, so the thread running it must outlive that process or the
    # kernel kills the sandbox as soon as start() returns. `_stop_client` releases the thread.
    client._omp_launch_hold = hold = threading.Event()
    def start() -> None:
        try:
            client.start()
        finally:
            if abandoned.is_set():
                client._omp_force_stop = True
                _stop_client(client)
    try:
        _bounded_call(start, timeout, hold=hold)
    except _BoundedCallTimeout:
        abandoned.set()
        client._omp_force_stop = True
        raise
    process = getattr(client, "_process", None)
    if process is not None:
        from services.worker_activity import capture_sdk_identity, process_identity

        try:
            client._chudware_activity_identity = process_identity(process)
            client._chudware_activity_sdk_identity = capture_sdk_identity(client)
        except (OSError, ValueError, RuntimeError):
            # Missing process-census capability must not prevent ordinary jobs
            # from starting; their later maintenance census remains unknown.
            client._chudware_activity_identity = None
            client._chudware_activity_sdk_identity = None
    protocol_version = getattr(
        client, "protocol_version", getattr(client, "_protocol_version", None)
    )
    if protocol_version is not None and protocol_version != 2:
        raise RuntimeError(
            f"Agent service RPC protocol v2 is required (runtime negotiated v{protocol_version})"
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


_PROMPT_MAX_IMAGES = 8
#: Delivered image bytes per prompt. Path images are normalized to the model limits
#: (tool_images.MAX_BYTES each), so a full set of them always fits; inline data
#: (already encoded by the caller) is charged against the same total.
_PROMPT_MAX_IMAGE_BYTES = _PROMPT_MAX_IMAGES * 3 * 1024 * 1024
_PROMPT_MAX_ATTACHMENTS = 32


def _prompt_payload(
    prompt: str, attachments_value: Any, images_value: Any
) -> tuple[str, list[dict[str, Any]]]:
    from services.agent.tool_images import ImageUnavailable, load_model_image

    images: list[dict[str, Any]] = []
    file_refs: list[str] = []
    remaining = _PROMPT_MAX_IMAGE_BYTES

    def add_inline(image: Mapping[str, Any]) -> None:
        nonlocal remaining
        data = image.get("data")
        if len(images) >= _PROMPT_MAX_IMAGES:
            raise ValueError("attachment_image_count_exceeded")
        if not isinstance(data, str) or len(data) > 4 * ((remaining + 2) // 3):
            raise ValueError("attachment_image_bytes_exceeded")
        # Bound before any encoding/copying; the runtime owns image decoding.
        source_bytes = (len(data) // 4) * 3 + (3 if len(data) % 4 else 0)
        if len(data) % 4 == 0:
            source_bytes -= 2 if data.endswith("==") else int(data.endswith("="))
        if source_bytes > remaining:
            raise ValueError("attachment_image_bytes_exceeded")
        if not data or not data.isascii():
            raise ValueError("attachment_image_data_invalid")
        remaining -= source_bytes
        images.append({
            "type": "image",
            "mimeType": image.get("mimeType") or image.get("mime") or "application/octet-stream",
            "data": data,
        })

    if isinstance(images_value, Sequence) and not isinstance(images_value, (str, bytes)):
        if len(images_value) > _PROMPT_MAX_IMAGES:
            raise ValueError("attachment_image_count_exceeded")
        for image in images_value:
            if isinstance(image, Mapping):
                add_inline(image)
    attachments = (
        attachments_value
        if isinstance(attachments_value, Sequence)
        and not isinstance(attachments_value, (str, bytes))
        else ()
    )
    if len(attachments) > _PROMPT_MAX_ATTACHMENTS:
        raise ValueError("attachment_count_exceeded")
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        if "data" in attachment:
            add_inline(attachment)
            continue
        path_value = _nonempty_string(attachment.get("path"))
        kind = _nonempty_string(attachment.get("kind"))
        mime = _nonempty_string(attachment.get("mime")) or _nonempty_string(
            attachment.get("mimeType")
        )
        if path_value and kind == "video":
            # Video bytes are not a provider image part. Probe only container headers,
            # with a short timeout; a missing executable or unsupported codec still
            # leaves the workspace path usable by the reconstruction tools.
            from services.vision.frames import probe_video
            metadata = probe_video(str(tenancy.cell_path(path_value)), timeout=2.0)
            details = {"kind": "video", "path": path_value}
            for key in ("duration_s", "width", "height", "orientation_degrees_clockwise"):
                if key in metadata:
                    details[key] = metadata[key]
            file_refs.append(
                "- Video (path only; video frames were not sent to the model): "
                + json.dumps(details, ensure_ascii=False)
                + ". For a reconstruction session use reveng_add_video(session_id, path) "
                  "for frame extraction, reveng_start_splat for Gaussian reconstruction, "
                  "and reveng_reconstruct for the measured CAD workflow."
            )
        elif path_value and (kind == "image" or (mime and mime.startswith("image/"))):
            if len(images) >= _PROMPT_MAX_IMAGES:
                raise ValueError("attachment_image_count_exceeded")
            # Each source is bounded on its own (tool_images.MAX_SOURCE_BYTES) and sent
            # at the model's image limits: a phone or 4K screenshot is several MB of
            # pixels no model sees, and must not exhaust the budget for the rest.
            try:
                model_image = load_model_image(str(Path(path_value).expanduser()))
            except ImageUnavailable as exc:
                raise ValueError(f"attachment_image_unavailable: {exc}") from exc
            if len(model_image.data) > remaining:
                raise ValueError("attachment_image_bytes_exceeded")
            remaining -= len(model_image.data)
            images.append(model_image.block())
        elif path_value:
            name = _nonempty_string(attachment.get("name")) or Path(path_value).name
            file_refs.append(f"- {name}: {path_value}")
    if file_refs:
        prompt = prompt.rstrip() + "\n\nAttached workspace files:\n" + "\n".join(file_refs)
    return prompt, images


# OMP's RPC wire names are lowerCamelCase of the client's snake_case dataclass fields,
# EXCEPT where the name carries an internal acronym: the protocol spells `has_oauth` as
# `hasOAuth`, which naive per-piece capitalization renders `hasOauth`. That one character cost
# every OAuth-connected provider its `oauth_connected` state (the GUI read `hasOAuth`, got
# nothing, and reported anthropic/kimi-code/openai-codex as signed out). Overrides are keyed by
# field name, and test_omp_host asserts every protocol field resolves to a real wire key.
_WIRE_NAME_OVERRIDES = {"has_oauth": "hasOAuth"}


def _camel(name: str) -> str:
    override = _WIRE_NAME_OVERRIDES.get(name)
    if override is not None:
        return override
    head, *tail = name.split("_")
    return head + "".join(piece[:1].upper() + piece[1:] for piece in tail)


def _steer_status_frame(run: _Run, steer: _Steer, status: str) -> dict[str, Any]:
    """``agent_steer`` frame: ``delivered`` (the model has it) or ``carried``
    (withdrawn before delivery; it opens the conversation's next prompt)."""
    return {
        "type": "agent_steer",
        "run_id": run.run_id,
        "session_id": run.session_id,
        "steer_id": steer.steer_id,
        "status": status,
    }


def _user_frame_text(frame: Mapping[str, Any]) -> str | None:
    """Text of a streamed user message frame; None for any other role."""
    message = frame.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    )


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
        state = _json_value(_bounded_call(
            lambda: _invoke_session_canonical(client, "get_state", {}), _CONTROL_CALL_SECONDS))
        return state if isinstance(state, dict) else {}
    except (RuntimeError, TimeoutError, ValueError) as exc:
        _LOG.debug(
            "OMP get_state failed (%s)",
            type(exc).__name__,
        )
        return {}


def _context_frame(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Turn ``get_state().contextUsage`` into a ``context_update`` RPC-shaped frame."""
    usage = state.get("contextUsage")
    if not isinstance(usage, Mapping):
        return None
    frame: dict[str, Any] = {"type": "context_update"}
    tokens = usage.get("tokens")
    if "tokens" in usage and tokens is None:
        frame["contextTokens"] = None
    elif isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
        frame["contextTokens"] = int(tokens)
    window = usage.get("contextWindow")
    if isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0:
        frame["contextWindow"] = int(window)
        frame["windowSource"] = "catalog"
    return frame if len(frame) > 1 else None


_RETRY_FAILED_FRAMES = frozenset({"auto_retry_end", "retry_end", "retry_failed"})
_MESSAGE_FRAMES = frozenset({"message_end", "message_complete"})
_ASSISTANT_OUTPUT_FRAMES = _MESSAGE_FRAMES | {"tool_execution_end"}


def _abort_retry(client: Any) -> None:
    """Cancel OMP's automatic retry without blocking its frame reader (replies arrive on it)."""
    abort = getattr(client, "abort_retry", None)
    if not callable(abort):
        return

    def run() -> None:
        try:
            abort()
        except Exception as exc:  # noqa: BLE001 - the turn still ends with OMP's own error
            _LOG.debug("OMP abort_retry failed (%s)", type(exc).__name__)

    threading.Thread(target=run, name="chudware-abort-retry", daemon=True).start()



def _unrecovered_provider_failure(frames: Reversible[dict[str, Any]]) -> str | None:
    """Return the provider error when a provider failure was the turn's last real event.

    Two shapes end a turn on a provider failure: a failed retry (429 past the retry
    budget, …) and a non-retryable rejection, which OMP records as an assistant
    message with ``stopReason: "error"`` (a 400 such as a model the credential's client
    version may not use). Either one followed by assistant output (a later message or
    tool result) was recovered — a model fallback or a manual continue — and is not a
    turn failure. Only lifecycle frames after it mean the turn produced no work past
    that point.
    """
    for frame in reversed(frames):
        kind = frame.get("type")
        if kind in _MESSAGE_FRAMES:
            message = frame.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                continue  # user/custom/tool-result records are not assistant output
            if message.get("stopReason") != "error":
                return None
            quota = demo_lane.quota_message(message.get("errorMessage"))
            if quota is not None:
                return quota
            detail = _provider_error_detail(message.get("errorMessage"))
            return f"provider error: {detail or 'no error detail'}"[:1200]
        if kind in _ASSISTANT_OUTPUT_FRAMES:
            return None
        if kind not in _RETRY_FAILED_FRAMES:
            continue
        if frame.get("success") is True:
            return None
        message = ""
        for key in ("finalError", "final_error", "error", "errorMessage", "message", "reason"):
            value = frame.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("error")
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break
        quota = demo_lane.quota_message(message)
        if quota is not None:
            return quota
        return f"provider retry exhausted: {message or 'no error detail'}"[:1200]
    return None


def _provider_error_detail(raw: Any) -> str:
    """Reduce OMP's ``"<status> <json body>\\n<diagnostic lines>"`` to one sentence."""
    if not isinstance(raw, str):
        return ""
    head = raw.strip().split("\n", 1)[0]
    status, _, body = head.partition(" ")
    try:
        parsed = json.loads(body)
    except ValueError:
        return head
    error = parsed.get("error") if isinstance(parsed, Mapping) else None
    text = error.get("message") if isinstance(error, Mapping) else None
    if isinstance(text, str) and text.strip():
        return f"{status} {text.strip()}" if status.isdigit() else text.strip()
    return head


def _turn_budget_seconds(timeout: Any) -> float | None:
    """The caller's explicit turn budget, or ``None`` for a turn with no wall-clock stop."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float, str)):
        return None
    try:
        budget = float(timeout)
    except ValueError:
        return None
    return budget if math.isfinite(budget) and budget > 0 else None


def _is_wait_timeout(exc: BaseException) -> bool:
    """True when ``wait_for_idle`` gave up on its deadline (not a process/protocol failure)."""
    if isinstance(exc, TimeoutError):
        return True
    try:
        from omp_rpc import RpcTimeoutError  # type: ignore
    except ImportError:
        return False
    return isinstance(exc, RpcTimeoutError)


def _safe_last_assistant_text(client: Any) -> str | None:
    getter = getattr(client, "get_last_assistant_text", None)
    if not callable(getter):
        return None
    try:
        value = _bounded_call(getter, _CONTROL_CALL_SECONDS)
        return value if isinstance(value, str) else None
    except (RuntimeError, TimeoutError) as exc:
        _LOG.debug(
            "OMP get_last_assistant_text failed (%s)",
            type(exc).__name__,
        )
        return None


_DEFAULT_OMP_HOST: OmpSessionHost | None = None
_DEFAULT_OMP_HOST_LOCK = threading.Lock()

def worker_activity() -> dict:
    host = _DEFAULT_OMP_HOST
    return host.worker_activity() if host is not None else {"by_kind": {}}


def _live_sdk_activity(client: Any, probe: _ActivityProbe) -> tuple[dict[str, int], bool]:
    """Collect nonblocking local evidence, excluding this census's own request."""
    from services.worker_activity import registry_lock

    evidence: dict[str, int] = {}
    complete = True
    tracker = getattr(client, "_chudware_activity_dictation", None)
    if isinstance(tracker, _DictationActivity):
        try:
            with registry_lock(tracker.lock):
                evidence["sdk_stt"] = len(tracker.captures)
                complete = tracker.attested
        except Exception:
            complete = False
    else:
        complete = False

    pending = 0
    try:
        with registry_lock(client._state_lock):
            pending = len(client._pending)
            if probe.request_id in client._pending:
                pending -= 1
    except Exception:
        complete = False
    try:
        with registry_lock(client._event_condition):
            pending += len(client._pending_prompt_ids)
    except Exception:
        complete = False
    try:
        pending += len(client._pending_host_tool_calls) + len(client._pending_host_uri_requests)
    except Exception:
        complete = False
    evidence["sdk_pending_calls"] = pending
    return evidence, complete


def _probe_sdk_activity(client: Any, probe: _ActivityProbe) -> None:
    # Publish each source as soon as it is known: a later blocked or malformed
    # source must not discard already observed work.
    probe.result = {}

    def publish(kind: str, count: int) -> None:
        probe.result = {**(probe.result or {}), kind: count}

    def local_state() -> bool:
        tracker = getattr(client, "_chudware_activity_dictation", None)
        attested = isinstance(tracker, _DictationActivity)
        if attested:
            with tracker.lock:
                publish("sdk_stt", len(tracker.captures))
                attested = tracker.attested
        with client._state_lock:
            pending = len(client._pending)
        publish("sdk_pending_calls", max(pending, (probe.result or {}).get("sdk_pending_calls", 0)))
        with client._event_condition:
            pending += len(client._pending_prompt_ids)
        publish("sdk_pending_calls", max(pending, (probe.result or {}).get("sdk_pending_calls", 0)))
        pending += len(client._pending_host_tool_calls) + len(client._pending_host_uri_requests)
        publish("sdk_pending_calls", pending)
        return attested

    try:
        try:
            local_complete = local_state()
        except Exception:
            local_complete = False
        # Inspect raw fields: the SDK typed parser supplies defaults for missing
        # fields, which cannot establish a truthful idle attestation.
        # The SDK's _request delegates to these same methods. Keep its normal
        # timeout while identifying this request for concurrent local censuses.
        probe.request_id = client._next_request_id()
        state = client._request_with_id("get_state", probe.request_id, {})
        raw_complete = False
        if isinstance(state, dict):
            flags = ("isStreaming", "isCompacting", "hasPendingAsyncWork", "isSettled")
            runtime = any(state.get(key) is True for key in flags[:3])
            runtime = runtime or state.get("isSettled") is False
            publish("sdk_runtime", int(runtime))
            queued = state.get("queuedMessageCount")
            if type(queued) is int and queued >= 0:
                publish("sdk_queued_messages", queued)
            raw_complete = (all(type(state.get(key)) is bool for key in flags)
                            and type(queued) is int and queued >= 0)
        try:
            local_complete = local_state() and local_complete
        except Exception:
            local_complete = False
        probe.complete = raw_complete and local_complete
    except Exception:
        # Missing capability, death, malformed state and transport failures are
        # unknown; never infer idle from an empty persistent session registry.
        pass
    finally:
        probe.done.set()



def get_default_omp_host() -> OmpSessionHost:
    """Return the worker process's single lazily-created OMP session pool."""
    global _DEFAULT_OMP_HOST
    host = _DEFAULT_OMP_HOST
    if host is not None:
        return host
    with _DEFAULT_OMP_HOST_LOCK:
        if _DEFAULT_OMP_HOST is None:
            _DEFAULT_OMP_HOST = OmpSessionHost()
        return _DEFAULT_OMP_HOST


def _set_default_omp_host_for_tests(
    host: OmpSessionHost | None,
) -> OmpSessionHost | None:
    """Inject or clear the default host only from process-isolated tests."""
    global _DEFAULT_OMP_HOST
    with _DEFAULT_OMP_HOST_LOCK:
        previous = _DEFAULT_OMP_HOST
        _DEFAULT_OMP_HOST = host
        return previous

def _sandbox_class(params: Mapping[str, Any]) -> str:
    preferences = params.get("turn_preferences")
    readonly = (params.get("plan_only") or params.get("mode") in ("ask", "plan")
                or getattr(preferences, "mode", None) in ("ask", "plan"))
    return "ro" if tenancy.in_cell() and readonly else "rw"


def _context_callback(callback):
    """RPC readers and timers never inherit contextvars from the request thread."""
    ctx = tenancy.current_context()
    project = tenancy.root_value("CHUDWARE_PROJECT_DIR")
    save = tenancy.root_value("CHUDWARE_PROJECT_SAVE_DIR")

    def bound(*args, **kwargs):
        with tenancy.bind_context(ctx), tenancy.bind_roots(project, save):
            return callback(*args, **kwargs)

    return bound


def _not_found() -> dict[str, Any]:
    return {"ok": False, "reason": "not_found", "code": "not_found"}


def _control_allowed(record, credential_params=None) -> bool:
    if record is None:
        return False
    owner = record.get("_owner") if isinstance(record, Mapping) else record.owner
    identity = (record.get("_credential_identity") if isinstance(record, Mapping)
                else record.credential_identity)
    if not tenancy.owns(owner):
        return False
    if credential_params is not None and "byok_key" in credential_params:
        try:
            return identity == _credential_identity(credential_params)
        except ValueError:
            return False
    # Desktop has one local principal; cell controls use the trusted actor lane.
    return True


def _sandbox_client_kwargs(kwargs: dict[str, Any], params: Mapping[str, Any]) -> None:
    if not tenancy.in_cell():
        return
    if not kwargs.get("executable"):
        raise ValueError("cell_refused")
    # Public SDK seam: construct an unstarted client solely to obtain the exact CLI.
    # Passing command back through the constructor leaves all flags SDK-owned.
    from omp_rpc import RpcClient

    argv = list(RpcClient(**kwargs).command)
    kwargs["command"] = tenancy.sandbox_argv(argv, _sandbox_class(params))


def _session_link(session_id: str, identity: str | None, path: str | None = None) -> str | None:
    """Persist opaque conversation → history bindings, never handles in filenames."""
    root = tenancy.actor_state_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = root / "omp-session-links.sqlite"
    if database.is_symlink():
        raise ValueError("cell_refused")
    with sqlite3.connect(database) as db:
        database.chmod(0o600)
        db.execute("CREATE TABLE IF NOT EXISTS links "
                   "(handle TEXT PRIMARY KEY, credential TEXT NOT NULL, file TEXT NOT NULL)")
        key = tenancy.scoped_key(session_id)
        row = db.execute("SELECT credential, file FROM links WHERE handle=?", (key,)).fetchone()
        if row is None:
            legacy = root / "chudcode-session-links.sqlite"
            if legacy.is_file() and not legacy.is_symlink():
                with sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True) as old:
                    row = old.execute("SELECT credential, file FROM links WHERE handle=?", (key,)).fetchone()
                if row is not None:
                    if row[0] != identity:
                        raise KeyError("not_found")
                    if not Path(row[1]).resolve().is_relative_to(root.resolve()):
                        raise ValueError("cell_refused")
                    db.execute("INSERT INTO links(handle,credential,file) VALUES(?,?,?)",
                               (key, row[0], row[1]))
        if row is not None and row[0] != identity:
            raise KeyError("not_found")
        if path is not None:
            target = Path(path).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError("cell_refused")
            db.execute("INSERT INTO links(handle,credential,file) VALUES(?,?,?) "
                       "ON CONFLICT(handle) DO UPDATE SET file=excluded.file",
                       (key, identity, str(target)))
            return str(target)
        return row[1] if row is not None else None


def _missing_session() -> dict[str, Any]:
    return _not_found() if tenancy.in_cell() else {"ok": False, "reason": "session_not_live"}


def _forbidden_right() -> dict[str, Any]:
    return {"ok": False, "reason": "forbidden_right", "code": "forbidden_right"}


def _delegate_refusal() -> dict[str, Any] | None:
    return _forbidden_right() if "control_delegate" in tenancy.current_context() else None



def _stop_audit(run: _Run) -> dict[str, Any]:
    if run.cancel_reason != "manager_stop" or run.control_delegate is None:
        return {}
    return {"reason": "manager_stop", "stop_reason": "manager_stop",
            "control_delegate": dict(run.control_delegate)}
