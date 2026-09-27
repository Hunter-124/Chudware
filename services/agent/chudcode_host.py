"""Persistent Chudcode RPC sessions for the long-lived agent worker.

The dependency import and executable lookup deliberately happen on first use.  Importing
``services.agent`` must remain possible in offline and partially provisioned runtimes.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import hashlib
import logging
import mimetypes
import os
import shutil
import sys
import tempfile
import re
import threading
import time
import uuid
from urllib.parse import urlsplit
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Reversible, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.agent.progress_watchdog import ProgressWatchdog

_DEFAULT_MAX_SESSIONS = 8
_MAX_MAX_SESSIONS = 32
_MAX_OPEN_URL_CHARS = 16_384
_MAX_UI_TEXT_CHARS = 4_096
_MAX_DOCTOR_OUTPUT_CHARS = 2_097_152
_MAX_DOCTOR_FINDINGS = 128
_MAX_DOCTOR_MESSAGE_CHARS = 4_096

# Default turn budget when the caller sends no `timeout` (the GUI and run_mission.py both
# send one). When it expires the run is ABORTED, not abandoned: see
# ChudcodeSessionHost._stop_budget_exhausted_run.
_TURN_ABSOLUTE_CAP_SECONDS = 2 * 60 * 60.0
# After the budget expires the host aborts the run and waits this long for its terminal
# agent_end. It must stay under hostd's bridge slack (budget + 60 s) so the GUI still
# receives this result instead of its own transport timeout.
_BUDGET_ABORT_GRACE_SECONDS = 30.0

_LOG = logging.getLogger(__name__)
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

def _credential_scope(params: Mapping[str, Any]) -> tuple[str, str, str, str, str] | None:
    """Return source, tenant, provider, secret, and Chudcode's credential env name."""
    source = _nonempty_string(params.get("web_key_source"))
    if source is not None:
        if source not in {"byok", "subscription", "pool"}:
            raise ValueError("web_credential_source_invalid")
        tenant = _nonempty_string(params.get("web_tenant"))
        provider = _nonempty_string(params.get("web_provider"))
        secret = _nonempty_string(params.get("web_credential"))
        if not tenant or not provider or not secret:
            raise ValueError("web_credential_missing")
        provider = provider.lower()
        if source == "subscription":
            env_name = {"anthropic-sub": "ANTHROPIC_OAUTH_TOKEN"}.get(provider)
            if env_name is None:
                raise ValueError("subscription_isolation_unsupported")
        else:
            env_name = byok_provider_env(provider)
            if env_name is None:
                raise ValueError(f"{source}_unsupported_online")
        return source, tenant, provider, secret, env_name
    if params.get("web_actor"):
        raise ValueError("web_credential_source_invalid")
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
    return "byok", tenant, provider, key, env_name


def _credential_identity(params: Mapping[str, Any]) -> str | None:
    scope = _credential_scope(params)
    if scope is None:
        return None
    source, tenant, provider, secret, _ = scope
    return hashlib.sha256(
        json.dumps([source, tenant, provider, secret], separators=(",", ":")).encode()
    ).hexdigest()


def _credential_client_scope(
    params: Mapping[str, Any], session_dir: str | None, resume_file: str | None
) -> tuple[tempfile.TemporaryDirectory[str], str, dict[str, str]]:
    from services import paths

    scope = _credential_scope(params)
    if scope is None:
        raise ValueError("web_credential_missing")
    source, _, _, secret, env_name = scope
    identity = _credential_identity(params)
    root = Path(session_dir) if session_dir else paths.ROOT / "Sessions"
    if resume_file and session_dir is None:
        root = Path(resume_file).expanduser().parent.parent
    scoped = root.expanduser() / f"{source}-{identity}"
    if scoped.is_symlink():
        raise RuntimeError("credential session directory cannot be a symlink")
    if resume_file and Path(resume_file).expanduser().resolve().parent != scoped.resolve():
        raise RuntimeError("session file belongs to another credential scope")
    scoped.mkdir(mode=0o700, parents=True, exist_ok=True)
    scoped.chmod(0o700)
    tempdir = tempfile.TemporaryDirectory(prefix=f"chudware-{source}-")
    home = Path(tempdir.name)
    agent, xdg = home / "agent", home / "xdg"
    agent.mkdir(mode=0o700)
    xdg.mkdir(mode=0o700)
    # RpcClient merges env into os.environ: blank inherited credentials/config
    # before restoring process essentials and the one selected provider secret.
    clean = {name: "" for name in os.environ}
    for name in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "CHUDCODE_PACKAGE_DIR"):
        if name in os.environ:
            clean[name] = os.environ[name]
    clean.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "CHUDCODE_CODING_AGENT_DIR": str(agent),
        env_name: secret,
    })
    return tempdir, str(scoped), clean







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

# Chudware wire provider names -> Chudcode provider ids. The GUI (composer providerWireName)
# and the native catalog spell a few providers differently from Chudcode's auth store, and
# `set_model` on the wrong id is a guaranteed "Model not found" even when the user is signed
# in. Chudcode-native ids pass through unchanged.
_CHUDCODE_PROVIDER_ALIASES: dict[str, str] = {
    "gemini": "google",
    "google-gemini": "google",
    "llamacpp": "llama.cpp",
    "llama-cpp": "llama.cpp",
    "lmstudio": "lm-studio",
    "anthropic-sub": "anthropic",
    "xai-sub": "xai",
    "kimi-sub": "kimi-code",
    "cursor-sub": "cursor",
    "codex": "openai-codex",
    "codex-sub": "openai-codex",
}
_MAX_MODEL_HINTS = 6


def _describe_model_rejection(client: Any, provider: str, model: str, exc: Exception) -> str:
    """Turn a bare ``set_model`` refusal into the user's next move.

    Chudcode's catalog lists only providers that are signed in, so "Model not found" means
    one of two very different things: the provider has no credential, or the id is not in
    that provider's catalog. Live 2026-09-03: a persisted anthropic/claude-sonnet-5 pick
    with Anthropic signed out surfaced as ``set_model: Model not found`` and nothing told
    the user to sign in or pick another model.
    """
    try:
        models = list(client.get_available_models())
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
            f"{provider} is not signed in to Chudcode, so {provider}/{model} cannot run. "
            f"Sign in under Settings -> Providers, or pick a model from a signed-in "
            f"provider in the composer's model chip.{others}"
        )
    hints = ", ".join(sorted(m for m in same_provider if m)[:_MAX_MODEL_HINTS])
    return (
        f"{model} is not in Chudcode's {provider} catalog. Pick a listed {provider} model "
        f"(e.g. {hints})."
    )


@dataclass
class _Session:
    session_id: str
    client: Any
    resume_file: str | None
    workspace_dir: str | None
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    credential_identity: str | None = None
    active_turns: int = 0
    closed: bool = False
    dispose_started: bool = False
    host_tool_names: set[str] = field(default_factory=set)


@dataclass
class _CreationGuard:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


@dataclass
class _Run:
    run_id: str
    session_id: str
    client: Any | None = None
    started: bool = False
    cancel_requested: bool = False
    cancel_sent: bool = False
    steers: set[str] = field(default_factory=set)
    control_lock: threading.Lock = field(default_factory=threading.Lock)
    # (message, images) queued before prompt() returned; images are Chudcode ImageContent.
    pending_steers: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)


class ChudcodeSessionHost:
    """Own one persistent ``RpcClient`` process per Chudware conversation.

    ``client_factory`` is intentionally injectable.  Production leaves it unset and gets
    the pinned ``chudcode_rpc.RpcClient`` lazily; focused tests can supply a process-free
    fake accepting the same keyword arguments.
    """

    def __init__(self, client_factory=None, binary=None, *, clock=None):
        self._clock = clock or time.monotonic
        self._realtime_watchdog = clock is None
        self._client_factory = client_factory
        self._binary_hint = os.fspath(binary) if binary is not None else None
        self._max_sessions = _session_limit_from_env()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._creating: dict[str, _CreationGuard] = {}
        self._reserved_creations = 0
        self._runs: dict[str, _Run] = {}
        self._completed_runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        # ONE long-lived no-session process serves every transient (auth / catalog /
        # memory / login) command. Spawning `chud --mode rpc` costs ~1.1 s before the
        # first request can be answered, and the GUI issues a dozen of these at startup
        # (provider list = 3 commands, model catalog, auth state, STT status, usage),
        # so spawn-per-call was what made "loading providers" take 30 s+. Calls are
        # serialized on the process; a dead process is replaced on the next call.
        self._service_client: Any = None
        self._service_lock = threading.Lock()
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
        emit_errors: list[str] = []
        provider_frames: deque[dict[str, Any]] = deque(maxlen=256)
        terminal: dict[str, Any] | None = None
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
                progress_timer = threading.Timer(
                    max(0.0, deadline - self._clock()), check_progress_timer
                )
                progress_timer.daemon = True
                progress_timer.start()

        def send_progress_nudge(text: str) -> None:
            try:
                outcome = self.steer(run_id, text)
            except Exception as exc:  # noqa: BLE001 - advisory steering never aborts a turn
                _LOG.warning("Progress steering failed (%s)", exc)
                return
            if outcome.get("steered") and not outcome.get("duplicate"):
                try:
                    emit({
                        "type": "agent.progress_nudge",
                        "message": text,
                        "text": (
                            "Progress nudge: "
                            + text.split(" without building anything.", 1)[0]
                            + ". Commit sourced dimensions; start modeling and save one part at a time."
                        ),
                        "run_id": run_id,
                        "session_id": session_id,
                    })
                except Exception as exc:  # noqa: BLE001 - same boundary as RPC events
                    emit_errors.append(str(exc))

        def dispatch_progress_nudge(text: str) -> None:
            # RpcClient notifications can run on the reader thread. Steering there
            # would wait for a response only that same reader can receive.
            if self._realtime_watchdog:
                thread = threading.Thread(
                    target=send_progress_nudge, args=(text,), daemon=True
                )
                progress_threads.append(thread)
                thread.start()
            else:
                send_progress_nudge(text)

        def check_progress_timer() -> None:
            nonlocal progress_timer, scheduled_deadline
            with progress_lock:
                progress_timer = None
                scheduled_deadline = None
                if progress_closed:
                    return
                text = progress.due()
                schedule_progress_timer()
            if text is not None:
                dispatch_progress_nudge(text)
        try:
            session = self._get_or_create_session(session_id, params, resume_file)
            with session.turn_lock:
                with self._lock:
                    if session.closed:
                        raise RuntimeError("chudcode RPC session was closed")
                    run.client = session.client
                    self._sessions.move_to_end(session_id)

                # Emitted right after agent_start (the GUI resets its meter on run_started):
                # the resume key first, then get_state's contextUsage — the resumed session's
                # real fill.
                opening: list[dict[str, Any]] = []

                def forward(event: Any) -> None:
                    nonlocal terminal, progress_closed
                    # Keep distinct notifications when the RPC client reuses a frame's
                    # object identity.
                    frame = _json_value(event)
                    if not isinstance(frame, dict):
                        frame = {"type": "unknown", "value": frame}
                    kind = frame.get("type")
                    if kind == "agent_end" and frame.get(
                        "isTerminal", frame.get("is_terminal")
                    ) is not False:
                        terminal = frame
                        with progress_lock:
                            progress_closed = True
                            schedule_progress_timer()
                    if kind in _RETRY_FAILED_FRAMES or kind in _ASSISTANT_OUTPUT_FRAMES:
                        provider_frames.append(frame)
                    if not progress_closed:
                        with progress_lock:
                            nudge = progress.observe(frame, session.host_tool_names)
                            schedule_progress_timer()
                        if nudge is not None:
                            dispatch_progress_nudge(nudge)
                    try:
                        emit(frame)
                        if kind == "agent_start":
                            while opening:
                                emit(opening.pop(0))
                    except Exception as exc:  # noqa: BLE001 - emit is an injected boundary
                        emit_errors.append(str(exc))
                unsubscribe = _register_listener(session.client, forward)
                self._configure_turn(session, params)
                opening_state = _safe_client_state(session.client)
                bound_file = opening_state.get("sessionFile") or opening_state.get(
                    "session_file"
                )
                if isinstance(bound_file, str) and bound_file:
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

                for text, steer_images in pending_steers:
                    with run.control_lock:
                        session.client.steer(text, images=steer_images or None)
                        with self._lock:
                            run.steers.add(text)
                            run.pending_steers.remove((text, steer_images))
                # `wait_for_idle(timeout=None)` inherits the RPC client's own 60s default,
                # which is a short-wait default and not a turn budget: nothing in CAD ever
                # set `timeout`, so EVERY online turn was killed at 60 seconds with
                # `provider_error - Timed out waiting for agent_end` and an empty stderr,
                # mid-build, while the agent was still healthy and working. The caller owns
                # the budget (GUI 7200 s, run_mission.py --timeout-s); the default matches
                # the old absolute cap. When it expires the run is aborted, because a
                # Chudcode run nobody waits for keeps editing documents unseen.
                timeout = params.get("timeout")
                budget_s = (
                    float(timeout) if timeout is not None else _TURN_ABSOLUTE_CAP_SECONDS
                )
                try:
                    session.client.wait_for_idle(timeout=budget_s)
                except Exception as exc:  # noqa: BLE001 - classified below
                    if not _is_wait_timeout(exc):
                        raise
                    # The caller stopped waiting, but Chudcode would keep running this
                    # run: every later CAD tool call mutates documents with the turn's
                    # context torn down and no event reaching any UI. Live 2026-09-25: a
                    # GUI turn hit its 2 h budget and the agent kept editing, saving and
                    # checkpointing the user's assembly for two more unseen hours.
                    return self._stop_budget_exhausted_run(
                        run, session, budget_s, lambda: terminal
                    )
                for thread in progress_threads:
                    thread.join(timeout=1)

                if terminal is None:
                    raise RuntimeError(
                        "chudcode RPC turn ended without a terminal agent_end"
                    )

                state = _safe_client_state(session.client)
                # Chudcode's own figure (anchored provider usage + unbilled tail), the
                # same number its footer shows; the per-message usage was provisional.
                closing_frame = _context_frame(state)
                if closing_frame is not None:
                    try:
                        emit(closing_frame)
                    except Exception as exc:  # noqa: BLE001 - emit is an injected boundary
                        emit_errors.append(str(exc))
                provider_failure = _unrecovered_provider_failure(provider_frames)
                result = {
                    "ok": not emit_errors and provider_failure is None,
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
                if provider_failure is not None:
                    # Chudcode still emits a terminal agent_end when its retry budget is
                    # exhausted (429 with a retry-after past retry.maxDelayMs, auth revoked,
                    # …); the process is idle and healthy but the turn delivered nothing.
                    # A mission driver that trusts `terminal` alone reported OK/completed
                    # on a 53-second turn with 77 bytes of text. The failure is the result.
                    result["stop_reason"] = "provider_error"
                    result["reason"] = provider_failure
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
            with progress_lock:
                progress_closed = True
                if progress_timer is not None:
                    progress_timer.cancel()
            for thread in progress_threads:
                if thread.is_alive():
                    thread.join(timeout=1)
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception as exc:  # noqa: BLE001 - teardown must continue
                    _LOG.warning(
                        "Chudcode event unsubscription failed (%s)",
                        type(exc).__name__,
                    )
            dispose = False
            with self._lock:
                if session is not None:
                    dispose = self._release_session_locked(session)
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
            if dispose:
                _stop_client(session.client)

    def _stop_budget_exhausted_run(
        self,
        run: _Run,
        session: _Session,
        budget_s: float,
        terminal_seen: Callable[[], dict[str, Any] | None],
    ) -> dict:
        """Abort a run that outlived its budget and report it as ``turn_budget``."""
        with run.control_lock:
            with self._lock:
                should_abort = not run.cancel_sent
            if should_abort:
                try:
                    session.client.abort()
                except Exception as exc:  # noqa: BLE001 - still report the budget stop
                    _LOG.warning("Chudcode abort after turn budget failed (%s)", exc)
                else:
                    with self._lock:
                        run.cancel_sent = True
        try:
            session.client.wait_for_idle(timeout=_BUDGET_ABORT_GRACE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - the abort is already on its way
            _LOG.warning("Chudcode run did not settle after budget abort (%s)", exc)
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
            result["chudcode_session_id"] = state.get("sessionId") or state.get("session_id")
            result["chudcode_session_file"] = state.get("sessionFile") or state.get(
                "session_file"
            )
        return result

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
        return self._cancel_run(run)

    def _cancel_run(self, run: _Run) -> dict:
        run_key = run.run_id
        with run.control_lock:
            with self._lock:
                run.cancel_requested = True
                client = run.client
                should_abort = client is not None and run.started and not run.cancel_sent
            if should_abort:
                try:
                    client.abort()
                except Exception as exc:  # noqa: BLE001 - RPC transport failures are retryable
                    return {
                        "ok": False,
                        "run_id": run_key,
                        "cancelled": True,
                        "active": True,
                        "reason": str(exc),
                    }
                with self._lock:
                    run.cancel_sent = True
        return {"ok": True, "run_id": run_key, "cancelled": True, "active": True}

    def steer(self, run_id, text, attachments=None) -> dict:
        run_key = _nonempty_string(run_id)
        message = _nonempty_string(text)
        if run_key is None or message is None:
            return {
                "ok": False,
                "reason": "run_id and non-empty steering text are required",
            }
        try:
            message, images = _prompt_payload(message, attachments, None)
        except OSError as exc:
            return {
                "ok": False,
                "run_id": run_key,
                "steered": False,
                "reason": f"steer attachment unreadable: {exc}",
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
        with run.control_lock:
            with self._lock:
                if message in run.steers or any(
                    queued == message for queued, _ in run.pending_steers
                ):
                    return {
                        "ok": True,
                        "run_id": run_key,
                        "steered": True,
                        "active": True,
                        "duplicate": True,
                    }
                client = run.client
                send_now = client is not None and run.started and not run.cancel_requested
                if not send_now:
                    run.pending_steers.append((message, images))
            if send_now:
                try:
                    client.steer(message, images=images or None)
                except Exception as exc:  # noqa: BLE001 - RPC transport failures are retryable
                    return {
                        "ok": False,
                        "run_id": run_key,
                        "steered": False,
                        "active": True,
                        "reason": str(exc),
                    }
                with self._lock:
                    run.steers.add(message)
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
            session = self._sessions.get(key)
            if session is None or session.closed:
                return {"ok": True}
            session.closed = True
            runs = [run for run in self._runs.values() if run.session_id == key]
            for run in runs:
                run.cancel_requested = True
            dispose = self._dispose_if_idle_locked(session)
        for run in runs:
            result = self._cancel_run(run)
            if not result["ok"]:
                _LOG.warning("Chudcode abort during session disposal failed")
        if dispose:
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
            service_client = self._service_client
            self._sessions.clear()
            self._service_client = None
            for session in sessions:
                session.closed = True
                session.dispose_started = True
            for run in self._runs.values():
                run.cancel_requested = True
        for session in sessions:
            _stop_client(session.client)
        if service_client is not None:
            _stop_client(service_client)

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

    def _release_session_locked(self, session: _Session) -> bool:
        session.active_turns -= 1
        return self._dispose_if_idle_locked(session)

    def _dispose_if_idle_locked(self, session: _Session) -> bool:
        if session.closed and session.active_turns == 0 and not session.dispose_started:
            session.dispose_started = True
            if self._sessions.get(session.session_id) is session:
                self._sessions.pop(session.session_id)
            return True
        return False

    def _release_live_session(self, session: _Session) -> None:
        with self._lock:
            dispose = self._release_session_locked(session)
        if dispose:
            _stop_client(session.client)

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
        with self._lock:
            if self._shutdown:
                raise RuntimeError("chudcode session host is shut down")
            existing = self._claim_existing_session_locked(
                session_id, resume_file, workspace_dir, credential_identity
            )
            if existing is not None:
                return existing
            guard = self._creating.setdefault(session_id, _CreationGuard())
            guard.users += 1
        try:
            with guard.lock:
                reserved = False
                try:
                    with self._lock:
                        if self._shutdown:
                            raise RuntimeError("chudcode session host is shut down")
                        existing = self._claim_existing_session_locked(
                            session_id, resume_file, workspace_dir, credential_identity
                        )
                        if existing is not None:
                            return existing
                        victim = self._evict_for_new_session()
                        self._reserved_creations += 1
                        reserved = True
                    if victim is not None:
                        _stop_client(victim.client)
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
                        with self._lock:
                            if self._shutdown:
                                raise RuntimeError("chudcode session host is shut down")
                            session = _Session(
                                session_id,
                                client,
                                resume_file,
                                workspace_dir,
                                credential_identity=credential_identity,
                                active_turns=1,
                            )
                            self._sessions[session_id] = session
                            self._reserved_creations -= 1
                            reserved = False
                        return session
                    except Exception:  # noqa: BLE001 - even RPC-specific errors need teardown
                        _stop_client(client)
                        raise
                finally:
                    if reserved:
                        with self._lock:
                            self._reserved_creations -= 1
        finally:
            with self._lock:
                guard.users -= 1
                if not guard.users:
                    self._creating.pop(session_id, None)

    def _claim_existing_session_locked(
        self, session_id: str, resume_file: str | None, workspace_dir: str | None,
        credential_identity: str | None,
    ) -> _Session | None:
        existing = self._sessions.get(session_id)
        if existing is None:
            return None
        if existing.closed:
            raise RuntimeError("chudcode RPC session is closing")
        if existing.credential_identity != credential_identity:
            raise RuntimeError("cannot change Chudcode credential identity on a live session")
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

    def _evict_for_new_session(self) -> _Session | None:
        if len(self._sessions) + self._reserved_creations < self._max_sessions:
            return None
        victim_key = next(
            (
                key
                for key, value in self._sessions.items()
                if value.active_turns == 0 and not value.closed
            ),
            None,
        )
        if victim_key is None:
            raise RuntimeError("all bounded Chudcode RPC sessions are currently active")
        victim = self._sessions.pop(victim_key)
        victim.closed = True
        victim.dispose_started = True
        return victim

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
            # RpcClient's default 10_000-frame history raises during long missions;
            # this transport limit must outlast a turn even though run_turn only
            # retains terminal and provider-recovery frames.
            "max_event_history": _event_history_limit_from_env(),
        }
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
        try:
            client = factory(**kwargs)
            if tempdir is not None:
                client._chudware_tempdir = tempdir
            return client
        except Exception:
            if tempdir is not None:
                tempdir.cleanup()
            raise

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
        if provider:
            provider = _CHUDCODE_PROVIDER_ALIASES.get(provider, provider)
        if provider and model:
            try:
                client.set_model(provider, model)
            except Exception as exc:  # noqa: BLE001 - RpcCommandError or a test double's error
                raise RuntimeError(
                    _describe_model_rejection(client, provider, model, exc)
                ) from exc
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

    def _service_client_locked(self) -> Any:
        """Return the live shared service process, spawning it when absent or dead.

        Caller holds ``_service_lock``.
        """
        client = self._service_client
        if client is not None and getattr(client, "_closed_error", None) is None:
            return client
        if client is not None:
            _stop_client(client)
            with self._lock:
                if self._service_client is client:
                    self._service_client = None
        client = self._make_client({}, no_session=True)
        _start_client(client)
        with self._lock:
            if self._shutdown:
                _stop_client(client)
                raise RuntimeError("chudcode session host is shut down")
            self._service_client = client
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
            _LOG.debug("Chudcode service warm-up skipped (%s)", type(exc).__name__)

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
                            "Chudcode login UI teardown failed (%s)", type(exc).__name__
                        )
            normalized = _json_value(result)
            if not isinstance(normalized, dict):
                raise TypeError(f"Chudcode {command} returned a non-object response")
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


_DEFAULT_EVENT_HISTORY = 1_000_000


def _event_history_limit_from_env() -> int:
    raw = os.environ.get("CHUDWARE_CHUDCODE_EVENT_HISTORY", "")
    try:
        value = int(raw) if raw else _DEFAULT_EVENT_HISTORY
    except ValueError:
        value = _DEFAULT_EVENT_HISTORY
    return max(10_000, value)


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
    tempdir = getattr(client, "_chudware_tempdir", None)
    if tempdir is not None:
        tempdir.cleanup()
        client._chudware_tempdir = None


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


# Chudcode's RPC wire names are lowerCamelCase of the client's snake_case dataclass fields,
# EXCEPT where the name carries an internal acronym: the protocol spells `has_oauth` as
# `hasOAuth`, which naive per-piece capitalization renders `hasOauth`. That one character cost
# every OAuth-connected provider its `oauth_connected` state (the GUI read `hasOAuth`, got
# nothing, and reported anthropic/kimi-code/openai-codex as signed out). Overrides are keyed by
# field name, and test_chudcode_host asserts every protocol field resolves to a real wire key.
_WIRE_NAME_OVERRIDES = {"has_oauth": "hasOAuth"}


def _camel(name: str) -> str:
    override = _WIRE_NAME_OVERRIDES.get(name)
    if override is not None:
        return override
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


def _context_frame(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Turn ``get_state().contextUsage`` into a ``context_update`` RPC-shaped frame."""
    usage = state.get("contextUsage")
    if not isinstance(usage, Mapping):
        return None
    frame: dict[str, Any] = {"type": "context_update"}
    tokens = usage.get("tokens")
    # null right after compaction, until the next response anchors it.
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
        frame["contextTokens"] = int(tokens)
    window = usage.get("contextWindow")
    if isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0:
        frame["contextWindow"] = int(window)
        frame["windowSource"] = "catalog"
    return frame if len(frame) > 1 else None


_RETRY_FAILED_FRAMES = frozenset({"auto_retry_end", "retry_end", "retry_failed"})
_MESSAGE_FRAMES = frozenset({"message_end", "message_complete"})
_ASSISTANT_OUTPUT_FRAMES = _MESSAGE_FRAMES | {"tool_execution_end"}


def _unrecovered_provider_failure(frames: Reversible[dict[str, Any]]) -> str | None:
    """Return the provider error when a provider failure was the turn's last real event.

    Two shapes end a turn on a provider failure: a failed retry (429 past the retry
    budget, …) and a non-retryable rejection, which Chudcode records as an assistant
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
        return f"provider retry exhausted: {message or 'no error detail'}"[:1200]
    return None


def _provider_error_detail(raw: Any) -> str:
    """Reduce Chudcode's ``"<status> <json body>\\n<diagnostic lines>"`` to one sentence."""
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


def _is_wait_timeout(exc: BaseException) -> bool:
    """True when ``wait_for_idle`` gave up on its deadline (not a process/protocol failure)."""
    if isinstance(exc, TimeoutError):
        return True
    try:
        from chudcode_rpc import RpcTimeoutError  # type: ignore
    except ImportError:
        return False
    return isinstance(exc, RpcTimeoutError)


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
