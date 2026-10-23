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
import math
import mimetypes
import os
import shutil
import sys
import tempfile
import re
import threading
import subprocess
import sqlite3
import time
import uuid
from urllib.parse import urlsplit
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Reversible, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.agent.chudcode_questions import TurnQuestions
from services.agent.online_turn import format_duration
from services.agent.progress_watchdog import ProgressWatchdog, TurnBudgetReminder
from services.agent import request_context as tenancy

_DEFAULT_MAX_SESSIONS = 8
_MAX_MAX_SESSIONS = 32
_MAX_OPEN_URL_CHARS = 16_384
_MAX_UI_TEXT_CHARS = 4_096
_MAX_DOCTOR_OUTPUT_CHARS = 2_097_152
_MAX_DOCTOR_FINDINGS = 128
_MAX_DOCTOR_MESSAGE_CHARS = 4_096

# Rewinding an edited user message: bounds for the GUI's typed text / repeat index, and
# how long to wait for a brief state/stats call holding the session's turn lock.
_MAX_REWIND_TEXT_CHARS = 1_000_000
_MAX_REWIND_OCCURRENCE = 100_000
_REWIND_LOCK_WAIT_SECONDS = 10.0
# worker_rpc._bounded_history_seed + online_turn.compose_request_text: a fresh
# conversation's first request leads with the migrated GUI history (JSON rows, so these
# delimiter lines cannot occur inside it); the typed request follows.
_MIGRATION_PREFIX = "<<<CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>\n"
_MIGRATION_REQUEST_MARKER = (
    "\n<<<END_CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>\n\nCURRENT USER REQUEST\n"
)
# The only blocks appended after the typed text (each after a blank line): composer.cpp
# consumeAttachments, plan_board planBoardTakeEditsForTurn, context_ref
# contextRefsPromptText (steers), worker_rpc _resumed_documents_block /
# _editor_context_block / _picked_context_block, and _prompt_payload's file list.
_APPENDED_BLOCK_HEADERS = (
    "[Attached image: ",
    "[Attached file: ",
    "[Attached file (",
    "[Attached document: ",
    "The user edited the plan since the last run.\n",
    "Context references (picked by the user in the model):",
    "RESUMED CONVERSATION\n",
    "ACTIVE DOCUMENT (what the user is looking at)\n",
    "PICKED CONTEXT \u2014 the user attached ",
    "Attached workspace files:\n",
)

# A turn whose caller sent no positive `timeout` has NO wall-clock limit: it ends at its
# terminal agent_end, on Stop/close (abort), or when the Chudcode process dies. The old
# 2-hour default stopped healthy GUI turns mid-build. chudcode_rpc's
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
# The shared RPC service keeps its model registry in memory. A catalog RPC alone never
# re-discovers models; refresh Chudcode's own cache periodically, off the worker call lane.
_CATALOG_REFRESH_SECONDS = 2 * 60 * 60.0
_CATALOG_RETRY_SECONDS = 5 * 60.0
_CATALOG_COMMAND_TIMEOUT_SECONDS = 90
# One CAD helper model step (regen diagnosis, ECAD topology planning): a tool-free,
# history-free completion. It is bounded on its own; it never bounds a conversation.
_HELPER_TIMEOUT_SECONDS = 180.0
# Cancel/deadline re-check interval while a helper completion runs.
_HELPER_WAIT_SLICE_SECONDS = 0.5

#: Chudcode builtins a CAD conversation uses. CAD's own tools arrive as host tools
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


#: Chudcode shows this for `read`/`write xd://<name>` when <name> is in the CAD catalog
#: but not mounted yet; `{name}` is the tool name.
_LOADABLE_TOOL_HINT = 'Call enable_tools(tools=["{name}"]) first. Then read or write it.'


def publish_host_tools(client: Any, tools: Sequence[Any], advertised: Iterable[str]) -> None:
    """Register *tools* and name every catalog tool that ``enable_tools`` can still load.

    With the list, Chudcode answers ``xd://<unloaded tool>`` with the enable_tools step
    instead of a bare "No such tool".
    """
    from chudcode_rpc import LoadableHostTools  # type: ignore  # noqa: PLC0415

    from services.agent.tool_categories import loadable_tool_names  # noqa: PLC0415

    names = loadable_tool_names(advertised)
    if not names:
        client.set_custom_tools(tools)
        return
    client.set_custom_tools(
        tools,
        loadable_tools=LoadableHostTools(names=tuple(names), hint=_LOADABLE_TOOL_HINT),
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
    """Return the BYOK tenant, provider, key, and Chudcode's credential env name."""
    if tenancy.in_cell():
        # Provider key bytes never enter a cell, so a cell has no credential route.
        raise ValueError("credential_isolation_unsupported")
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


_EFFORT_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def _catalog_effort(client: Any, provider: str | None, model: str | None,
                    requested: str) -> str | None:
    """Clamp an inherited or explicit effort to the selected catalog model's ladder.

    No matching row means the catalog is unavailable: leave the user's value
    untouched. A matching empty ladder means no thinking dial, so send nothing.
    """
    if not provider or not model:
        return requested
    try:
        rows = client.get_available_models()
    except Exception:  # noqa: BLE001 - optional catalog read, not a turn failure
        return requested
    for row in rows:
        value = row if isinstance(row, Mapping) else {
            key: getattr(row, key, None) for key in
            ("provider", "id", "thinking", "reasoningEfforts")
        }
        if value.get("provider") != provider or value.get("id") != model:
            continue
        thinking = value.get("thinking")
        efforts = (thinking.get("efforts") if isinstance(thinking, Mapping)
                   else thinking if isinstance(thinking, list) else
                   value.get("reasoningEfforts"))
        if not isinstance(efforts, list):
            return requested
        ladder = [v for v in efforts if isinstance(v, str)]
        if not ladder:
            return None
        if requested in ladder:
            return requested
        index = _EFFORT_ORDER.index(requested) if requested in _EFFORT_ORDER else 4
        return min(ladder, key=lambda v: (abs(
            (_EFFORT_ORDER.index(v) if v in _EFFORT_ORDER else 4) - index),
            -(_EFFORT_ORDER.index(v) if v in _EFFORT_ORDER else 4)))
    return requested


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
    credential_identity: str | None = None
    active_turns: int = 0
    closed: bool = False
    dispose_started: bool = False
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



@dataclass
class _Steer:
    """One steer handed to Chudcode: its text, images and the GUI's correlation id."""

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
    cancel_reason: str = ""
    control_delegate: dict[str, str] | None = None
    cancel_sent: bool = False
    steers: set[tuple[str, str]] = field(default_factory=set)
    control_lock: threading.Lock = field(default_factory=threading.Lock)
    # Steers queued before prompt() returned; images are Chudcode ImageContent.
    pending_steers: list[_Steer] = field(default_factory=list)
    # User steers Chudcode holds in its queue until the next tool boundary. A user
    # message frame carrying the text means the model has it; until then a pure wait
    # (wait_job) is woken and Stop withdraws them instead of letting Chudcode's
    # post-abort drain start a turn nobody observes.
    undelivered: list[_Steer] = field(default_factory=list)
    # Pending ask_user batches of an interactive (GUI) turn; None for headless turns.
    questions: TurnQuestions | None = None
    owner: tuple[str, str] | None = field(default_factory=tenancy.owner_key)
    credential_identity: str | None = None
    registry_key: str = ""
    session_key: str = ""


class ChudcodeSessionHost:
    """Own one persistent ``RpcClient`` process per Chudware conversation.

    ``client_factory`` is intentionally injectable.  Production leaves it unset and gets
    the pinned ``chudcode_rpc.RpcClient`` lazily; focused tests can supply a process-free
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
        # User steers Stop withdrew before Chudcode delivered them, keyed by Chudware
        # session id: the transcript already shows them, so they ride with that
        # conversation's next prompt.
        self._carried_steers: dict[str, list[_Steer]] = {}
        self._prestart_cancels: dict[str, dict[str, str] | None] = {}
        self._lock = threading.RLock()
        # ONE long-lived no-session process serves every transient (auth / catalog /
        # memory / login) command. Spawning `chud --mode rpc` costs ~1.1 s before the
        # first request can be answered, and the GUI issues a dozen of these at startup
        # (provider list = 3 commands, model catalog, auth state, STT status, usage),
        # so spawn-per-call was what made "loading providers" take 30 s+. Calls are
        # serialized on the process; a dead process is replaced on the next call.
        self._service_client: Any = None
        self._service_memory_backend: str | None = None
        self._service_lock = threading.Lock()
        # Live tool-free helper processes (``complete_text``): disposable per call,
        # tracked only so shutdown() stops them with everything else this worker owns.
        self._helper_clients: set[Any] = set()
        self._activity_probes: dict[Any, _ActivityProbe] = {}
        self._shutdown = False
        self._catalog_refresh_lock = threading.Lock()
        self._catalog_refreshing = False
        self._catalog_last_attempt = float("-inf")
        self._catalog_last_success = float("-inf")
        # Identity of the binary each refresh ran against: an upgraded `chud` (e.g. a new
        # Codex client_version that unlocks newer models) is re-discovered at once rather
        # than after the TTL, and the service still running the old binary is replaced.
        self._catalog_attempt_binary: tuple | None = None
        self._catalog_success_binary: tuple | None = None
        self._catalog_refresh_thread: threading.Thread | None = None
        self._catalog_process: subprocess.Popen | None = None

    def run_turn(self, params, emit) -> dict:
        """Schedule a prompt and block until a terminal ``agent_end`` is observed."""
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        if not isinstance(params, Mapping):
            return {"ok": False, "reason": "chudcode turn params must be an object"}
        if not callable(emit):
            return {"ok": False, "reason": "chudcode turn emit callback is required"}
        emit = _context_callback(emit)

        prompt = params.get("request", params.get("prompt", params.get("message")))
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "reason": "chudcode turn prompt is empty"}
        session_id = _nonempty_string(params.get("session_id")) or "default"
        run_id = _nonempty_string(params.get("run_id")) or uuid.uuid4().hex
        resume_file = _nonempty_string(params.get("chudcode_session_file"))
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
                return {"ok": False, "reason": "chudcode session host is shut down"}
            if run_key in self._runs:
                return {
                    "ok": False,
                    "run_id": run_id,
                    "reason": "chudcode run_id is already active",
                }
            self._completed_runs.pop(run_key, None)
            run = _Run(
                run_id=run_id, session_id=session_id,
                credential_identity=credential_identity,
                registry_key=run_key, session_key=session_key,
                cancel_requested=run_key in self._prestart_cancels,
                control_delegate=self._prestart_cancels.get(run_key),
                cancel_reason="manager_stop" if self._prestart_cancels.get(run_key) else "",
            )
            self._runs[run_key] = run
            self._prestart_cancels.pop(run_key, None)

        session: _Session | None = None
        unsubscribe: Callable[[], None] | None = None
        observed_contexts: list[dict[str, Any]] = []
        tool_recoveries: dict[int, Any] = {}
        daemon_connections = None
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
            session = self._get_or_create_session(session_id, params, resume_file)
            with session.turn_lock:
                self._refresh_compaction(session, params)
                with self._lock:
                    if session.closed:
                        raise RuntimeError("chudcode RPC session was closed")
                    run.client = session.client
                    self._sessions.move_to_end(session_key)

                # Emitted right after agent_start (the GUI resets its meter on run_started):
                # the resume key first, then get_state's contextUsage — the resumed session's
                # real fill.
                opening: list[dict[str, Any]] = []
                # Top-level Chudcode tool calls in flight (id -> name). A host call made
                # under one of these ids IS that visible call (`write xd://add_features`);
                # any other id was made from inside a tool, e.g. `tool.write(...)` in eval.
                open_calls: dict[str, str] = {}
                # The prompt text actually sent: its own user frame is not a steer delivery.
                sent_prompt: list[str] = []
                # Host-tool threads report nested calls concurrently with the reader thread.
                emit_lock = threading.Lock()

                def forward(event: Any) -> None:
                    nonlocal terminal, progress_closed, budget_timer
                    # Keep distinct notifications when the RPC client reuses a frame's
                    # object identity.
                    frame = _json_value(event)
                    if not isinstance(frame, dict):
                        frame = {"type": "unknown", "value": frame}
                    kind = frame.get("type")
                    call_id, phase, tool = _tool_execution_phase(frame)
                    if phase == "start":
                        with emit_lock:
                            open_calls[call_id] = tool
                    elif phase == "end":
                        with emit_lock:
                            open_calls.pop(call_id, None)
                    if kind == "agent_end" and frame.get(
                        "isTerminal", frame.get("is_terminal")
                    ) is not False:
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
                    if not progress_closed:
                        with progress_lock:
                            nudge = progress.observe(frame, session.host_tool_names)
                            schedule_progress_timer()
                            reminder = take_budget_reminder()
                        if nudge is not None:
                            dispatch_progress_nudge(nudge)
                        if reminder is not None:
                            dispatch_progress_nudge(*reminder)
                    delivered = (
                        self._take_delivered_steers(run, frame, sent_prompt)
                        if kind == "message_start"
                        else []
                    )
                    try:
                        with emit_lock:
                            emit(frame)
                            for steer in delivered:
                                emit(_steer_status_frame(run, steer, "delivered"))
                            if kind == "agent_start":
                                while opening:
                                    emit(opening.pop(0))
                    except Exception as exc:  # noqa: BLE001 - emit is an injected boundary
                        emit_errors.append(str(exc))

                def observe_host_call(frame: dict[str, Any]) -> None:
                    """A CAD host call made inside another tool gets its own feed row.

                    c230 ran every add_features through eval's ``tool.write``: 49 "eval"
                    rows and no add_features row, so the user could not see one feature
                    the agent built, and the progress watchdog never counted them.
                    """
                    call_id = frame.get("toolCallId")
                    with emit_lock:
                        if not call_id or call_id in open_calls:
                            return
                        outer = list(open_calls)
                    if len(outer) == 1:
                        frame["parentToolCallId"] = outer[0]
                    forward(frame)

                unsubscribe = _register_listener(session.client, _context_callback(forward))
                _collect_host_tool_recoveries(params.get("host_tools"), tool_recoveries)
                observed_contexts = _bind_host_call_observer(
                    params.get("host_tools"), observe_host_call
                )
                from services.mcp.daemon_bridge import TurnConnections

                daemon_connections = TurnConnections()
                from services.agent.providers.chudcode_provider import ChudcodeProvider

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
                    return ChudcodeProvider(
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
                        with emit_lock:
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
                self._configure_turn(session, params)
                opening_state = _safe_client_state(session.client)
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
                    # Set before prompt(): the reader thread may forward its user frame first.
                    sent_prompt.append(message)
                    try:
                        session.client.prompt(message, images=images or None)
                    except Exception:
                        if carried:
                            self._carried_steers[session_key] = [
                                *carried, *self._carried_steers.get(session_key, [])
                            ]
                        raise
                    run.started = True
                    pending_steers = tuple(run.pending_steers)

                for steer in pending_steers:
                    with run.control_lock:
                        # Tracked before the send: its user frame may be forwarded first.
                        with self._lock:
                            if steer.from_user:
                                run.undelivered.append(steer)
                        try:
                            session.client.steer(steer.text, images=steer.images or None)
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
                # the run is aborted, because a Chudcode run nobody waits for keeps
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
                    return self._stop_budget_exhausted_run(
                        run, session, budget_s, lambda: terminal
                    )
                if outcome == "cancel_unsettled":
                    return self._cancelled_without_terminal(run, session)
                for thread in progress_threads:
                    thread.join(timeout=1)

                if terminal is None:
                    raise RuntimeError(
                        "chudcode RPC turn ended without a terminal agent_end"
                    )
                # A steer that reached Chudcode after the loop's last boundary poll is
                # still queued, and Chudcode's settle drain would answer it in a turn no
                # one observes. Withdraw it; it opens this conversation's next prompt.
                if not run.cancel_requested:
                    for steer in self._withdraw_undelivered(run, session.client):
                        if steer.steer_id:
                            try:
                                with emit_lock:
                                    emit(_steer_status_frame(run, steer, "carried"))
                            except Exception as exc:  # noqa: BLE001 - injected boundary
                                emit_errors.append(str(exc))

                state = _safe_client_state(session.client)
                if tenancy.in_cell():
                    bound = state.get("sessionFile") or state.get("session_file")
                    if bound:
                        _session_link(session_id, session.credential_identity, bound)
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
                    **_stop_audit(run),
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
        except KeyError:
            return _not_found()
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
                if budget_timer is not None:
                    budget_timer.cancel()
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
            dispose = False
            with self._lock:
                if session is not None:
                    dispose = self._release_session_locked(session)
                self._runs.pop(run_key, None)
                completed = {
                    "run_id": run_id,
                    "session_id": session_id,
                    "cancelled": run.cancel_requested,
                    "started": run.started,
                    "_owner": run.owner,
                    "_credential_identity": run.credential_identity,
                }
                completed.update(_stop_audit(run))
                self._completed_runs[run_key] = completed
                owned_completed = [key for key, value in self._completed_runs.items()
                                   if value["_owner"] == run.owner]
                for key in owned_completed[:-self._max_sessions * 8]:
                    self._completed_runs.pop(key)
            if dispose:
                _stop_client(session.client)

    def _await_turn_idle(self, run: _Run, session: _Session, budget_s: float | None) -> str:
        """Wait for the prompt to settle: ``"idle"``, ``"budget"`` or ``"cancel_unsettled"``.

        An explicit budget is one bounded wait. Without one there is no deadline, only
        finite slices: Stop (abort) or close makes Chudcode answer ``prompt_result``,
        process death raises from ``wait_for_idle``, and a delivered Stop that still has
        not settled after ``_BUDGET_ABORT_GRACE_SECONDS`` is reported as cancelled
        instead of holding the worker lane forever.
        """
        if budget_s is not None:
            try:
                session.client.wait_for_idle(timeout=budget_s)
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _is_wait_timeout(exc):
                    raise
                return "budget"
            return "idle"
        cancel_deadline: float | None = None
        while True:
            wait_s = _UNBOUNDED_WAIT_SLICE_SECONDS
            if cancel_deadline is not None:
                wait_s = max(0.0, min(wait_s, cancel_deadline - _monotonic()))
            try:
                session.client.wait_for_idle(timeout=wait_s)
                return "idle"
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _is_wait_timeout(exc):
                    raise
            with self._lock:
                closed = session.closed
                cancel_sent = run.cancel_sent
            if closed:
                raise RuntimeError("chudcode RPC session was closed")
            if cancel_sent:
                now = _monotonic()
                if cancel_deadline is None:
                    cancel_deadline = now + _BUDGET_ABORT_GRACE_SECONDS
                elif now >= cancel_deadline:
                    return "cancel_unsettled"

    def _cancelled_without_terminal(self, run: _Run, session: _Session) -> dict:
        """Report a Stop whose abort Chudcode never confirmed with a terminal frame."""
        _LOG.warning(
            "Chudcode run %s did not settle within %.0fs of its abort",
            run.run_id,
            _BUDGET_ABORT_GRACE_SECONDS,
        )
        # An unacknowledged abort is not evidence the agent stopped. Dispose this
        # connection on release so no work survives after its CAD context is gone.
        with self._lock:
            session.closed = True
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
            result["chudcode_session_id"] = state.get("sessionId") or state.get("session_id")
            result["chudcode_session_file"] = state.get("sessionFile") or state.get(
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
        if terminal_seen() is None:
            with self._lock:
                session.closed = True
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

    def cancel(self, run_id, *, credential_params=None) -> dict:
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
                    if completed is None and tenancy.in_cell():
                        try:
                            tenancy.require_handle(run_id, "run")
                        except KeyError:
                            return _not_found()
                        self._prestart_cancels.setdefault(key, delegate)
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
        run_key = run.run_id
        with run.control_lock:
            with self._lock:
                delegate = tenancy.control_delegate("cancel")
                if delegate is not None and not run.cancel_requested:
                    run.control_delegate = dict(delegate)
                    run.cancel_reason = "manager_stop"
                run.cancel_requested = True
                client = run.client
                should_abort = client is not None and run.started and not run.cancel_sent
                questions = run.questions
            if questions is not None:
                questions.close()
            carried: list[_Steer] = []
            if should_abort:
                # Chudcode's abort keeps queued user steers and its settle drain then
                # answers them in a fresh turn nobody observes (c252: a steer queued
                # behind wait_job started a model call 0.2 s after Stop). Withdraw first.
                carried = self._withdraw_undelivered(run, client)
                try:
                    client.abort()
                except Exception as exc:  # noqa: BLE001 - RPC transport failures are retryable
                    return {
                        "ok": False,
                        "run_id": run_key,
                        "cancelled": True,
                        "active": True,
                        "reason": str(exc),
                        "carried_steer_ids": [s.steer_id for s in carried if s.steer_id],
                    }
                with self._lock:
                    run.cancel_sent = True
        return {
            "ok": True,
            "run_id": run_key,
            "cancelled": True,
            "active": True,
            "carried_steer_ids": [s.steer_id for s in carried if s.steer_id],
        }

    def _withdraw_undelivered(self, run: _Run, client: Any) -> list[_Steer]:
        """Pull the run's undelivered user steers out of Chudcode's queue.

        Withdrawn steers are carried to the session's next prompt and returned.
        ``removed: false`` does not prove consumption (the original send may have
        failed); only a streamed user frame acknowledges delivery.
        """
        with self._lock:
            pending = list(run.undelivered)
            run.undelivered.clear()
        carried: list[_Steer] = []
        for steer in pending:
            try:
                outcome = client.remove_queued_message(steer.text, "steering")
            except Exception as exc:  # noqa: BLE001 - a failed withdrawal leaves it queued
                _LOG.warning("Withdrawing a queued steer failed (%s)", type(exc).__name__)
                continue
            if getattr(outcome, "removed", False) is True:
                carried.append(steer)
        if carried:
            with self._lock:
                self._carried_steers.setdefault(run.session_key, []).extend(carried)
        return carried

    def _take_delivered_steers(
        self, run: _Run, frame: Mapping[str, Any], sent_prompt: Sequence[str]
    ) -> list[_Steer]:
        """Undelivered user steers a ``message_start`` user frame just delivered.

        Correlate exact sent text only: an unrelated or transformed user frame is
        not evidence that any tracked steer was consumed.
        """
        text = _user_frame_text(frame)
        if text is None or text in sent_prompt:
            return []
        with self._lock:
            if not run.undelivered:
                return []
            message = frame.get("message", {})
            content = message.get("content")
            images = [
                part for part in content
                if isinstance(part, Mapping) and part.get("type") == "image"
            ] if isinstance(content, list) else []
            taken = [
                s for s in run.undelivered if s.text == text and s.images == images
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
                    client.steer(message, images=images or None)
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
                "sessions": sum(_control_allowed(s, credential_params) for s in self._sessions.values()),
                "active_runs": sum(_control_allowed(r, credential_params) for r in self._runs.values()),
                "max_sessions": self._max_sessions, "shutdown": self._shutdown,
            }

    def worker_activity(self) -> dict:
        """Inspect existing ownership; idle SDKs require live settle/STT evidence."""
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
        with registry_lock(self._catalog_refresh_lock):
            counts["sdk_catalog_refresh"] = int(self._catalog_refreshing)
            catalog = self._catalog_process
        processes = [catalog] if catalog is not None and catalog.poll() is None else []
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
            evidence = probe.result.copy() if probe.result is not None else None
            if evidence is None:
                complete = False
                continue
            if not probe.complete:
                complete = False
            for kind, count in evidence.items():
                counts[kind] = counts.get(kind, 0) + count
            process = getattr(client, "_process", None)
            if any(evidence.values()):
                if process is not None and process.poll() is None:
                    processes.append(process)
            elif process is not None and probe.complete:
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
                _LOG.warning("Chudcode abort during session disposal failed")
        if dispose:
            _stop_client(session.client)
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
            if session is None or session.closed:
                return {"ok": True, "released": False}
            if session.active_turns > 0 or any(
                run.session_key == (session.registry_key or key) for run in self._runs.values()
            ):
                return {"ok": False, "reason": "session_busy"}
            session.closed = True
            session.dispose_started = True
            self._sessions.pop(session.registry_key or key)
        # Shutdown writes session_exit; the next owner must resume only after it lands.
        _stop_client(session.client)
        return {"ok": True, "released": True}

    def export_session(self, session_id, output_path=None) -> dict[str, Any]:
        """Export one live transcript through Chudcode and atomically publish it."""
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
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        if not isinstance(repair, bool):
            return {"ok": False, "reason": "repair must be a boolean"}
        session = self._claim_live_session(session_id)
        if session is None:
            return _missing_session()
        try:
            with session.turn_lock:
                if session.closed:
                    return _missing_session()
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

    def rewind_session(
        self, session_id, user_text, occurrence, session_file=None, workspace_dir=None
    ) -> dict[str, Any]:
        """Fork the conversation just BEFORE one of its user messages (an edited prompt).

        The target is the ``occurrence``-th (zero-based) user message on the runtime's
        active branch whose request is ``user_text``, resolved against Chudcode's own
        persisted entry ids (``get_entries``). Chudcode ``branch`` writes a new session
        file holding the path up to that message's parent and leaves the parent file
        untouched; the mutated process is retired so the next turn resumes the child.
        A session that is not live resumes only the explicit ``session_file`` (no provider
        call); that process is always retired afterwards because it was spawned without
        the turn's prompt, tools, and credentials.
        """
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        key = _nonempty_string(session_id)
        typed = user_text.strip() if isinstance(user_text, str) else ""
        if key is None:
            return {"ok": False, "reason": "invalid_params: session_id is required"}
        if not typed or len(typed) > _MAX_REWIND_TEXT_CHARS:
            return {"ok": False, "reason": "invalid_params: user_text must be non-empty text"}
        if (
            isinstance(occurrence, bool)
            or not isinstance(occurrence, int)
            or not 0 <= occurrence <= _MAX_REWIND_OCCURRENCE
        ):
            return {
                "ok": False,
                "reason": "invalid_params: occurrence must be a non-negative integer",
            }
        resume_file = _nonempty_string(session_file)
        with self._lock:
            if self._shutdown:
                return {"ok": False, "reason": "chudcode session host is shut down"}
            if any(run.session_key == tenancy.scoped_key(key) for run in self._runs.values()):
                return {"ok": False, "reason": "session_busy"}
        session = self._claim_live_session(key)
        spawned = False
        if session is None:
            if resume_file is None or tenancy.in_cell():
                return _missing_session()
            try:
                session = self._get_or_create_session(
                    key, {"workspace_dir": workspace_dir}, resume_file
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                return {"ok": False, "reason": str(exc)}
            spawned = True
        retire = spawned
        try:
            if not session.turn_lock.acquire(timeout=_REWIND_LOCK_WAIT_SECONDS):
                return {"ok": False, "reason": "session_busy"}
            try:
                with self._lock:
                    if session.closed:
                        return _missing_session()
                    if any(run.session_key == tenancy.scoped_key(key) for run in self._runs.values()):
                        return {"ok": False, "reason": "session_busy"}
                client = session.client
                state = _json_value(client.get_state())
                if not isinstance(state, Mapping):
                    raise TypeError("Agent session get_state returned a non-object response")
                if any(
                    state.get(flag) for flag in ("isStreaming", "isCompacting")
                ):
                    return {"ok": False, "reason": "session_busy"}
                parent_file = _nonempty_string(
                    state.get("sessionFile") or state.get("session_file")
                )
                if parent_file is None:
                    raise RuntimeError("Agent session has no persisted history to rewind")
                if resume_file is not None and not _same_path(resume_file, parent_file):
                    return {"ok": False, "reason": "session_file_mismatch"}
                listing = _json_value(client.request_raw("get_entries"))
                if not isinstance(listing, Mapping):
                    raise TypeError("Agent session get_entries returned a non-object response")
                target = _rewind_target(
                    _active_branch(listing.get("entries"), listing.get("leafId")),
                    typed,
                    occurrence,
                )
                if target is None:
                    return {"ok": False, "reason": "target_not_found"}
                # From here the runtime may have switched files even if the call fails.
                retire = True
                raw = _json_value(
                    _invoke_session_canonical(client, "branch", {"entryId": target})
                )
                if not isinstance(raw, Mapping):
                    raise TypeError("Agent session branch returned a non-object response")
                if raw.get("cancelled"):
                    retire = spawned
                    return {"ok": False, "reason": "branch_cancelled"}
                child = _json_value(client.get_state())
                child = child if isinstance(child, Mapping) else {}
                child_id = _nonempty_string(
                    child.get("sessionId") or child.get("session_id")
                )
                child_file = _nonempty_string(
                    child.get("sessionFile") or child.get("session_file")
                )
                if (
                    child_id is None
                    or child_file is None
                    or _same_path(child_file, parent_file)
                    or not Path(child_file).expanduser().is_file()
                ):
                    raise RuntimeError(
                        "Agent session branch returned no new persisted session"
                    )
                if not Path(parent_file).expanduser().is_file():
                    raise RuntimeError("Agent session branch lost the parent session file")
                if tenancy.in_cell():
                    _session_link(key, session.credential_identity, child_file)
                return {
                    "ok": True,
                    "chudcode_session_id": child_id,
                    "chudcode_session_file": child_file,
                }
            finally:
                # Close THIS process before any turn queued on its lock can run in it;
                # _claim_existing_session_locked refuses a closed session from here on.
                if retire:
                    with self._lock:
                        session.closed = True
                session.turn_lock.release()
        except Exception as exc:  # noqa: BLE001 - RPC errors are runtime-specific classes
            return {"ok": False, "reason": str(exc) or type(exc).__name__}
        finally:
            if retire:
                with self._lock:
                    session.closed = True
            # The last holder disposes it, and only while it is still the registered one.
            self._release_live_session(session)

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
        with self._catalog_refresh_lock:
            refresh = self._catalog_process
        if refresh is not None:
            try:
                refresh.terminate()
            except OSError:
                pass
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
        return self.transient_call(command_name, params)

    def memory_call(self, command, params):
        if _delegate_refusal() is not None:
            return _delegate_refusal()
        command_name = _nonempty_string(command)
        if command_name not in _MEMORY_SHAPES:
            raise ValueError(f"unsupported agent-service memory command: {command_name!r}")
        return self.transient_call(command_name, params)

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
        if provider_id:
            provider_id = _CHUDCODE_PROVIDER_ALIASES.get(provider_id, provider_id)
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
            provider_session_id = _nonempty_string(scoped_params.get("provider_session_id"))
            if provider_session_id is not None:
                kwargs["provider_session_id"] = provider_session_id
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
            _sandbox_client_kwargs(kwargs, {**scoped_params, "mode": "ask"})
            client = factory(**kwargs)
            client._chudware_tempdir = credential_dir or workdir
            # Serialize startup against shutdown: no client may spawn after shutdown
            # has already disposed it. RpcClient.start has a bounded handshake timeout.
            with self._lock:
                if self._shutdown:
                    raise HelperCompletionError("agent service host is shut down")
                self._helper_clients.add(client)
                registered = True
                _start_client(client)

            frames: deque[dict[str, Any]] = deque(maxlen=256)

            def record(event: Any) -> None:
                frame = _json_value(event)
                if isinstance(frame, dict) and (
                    frame.get("type") in _RETRY_FAILED_FRAMES
                    or frame.get("type") in _ASSISTANT_OUTPUT_FRAMES
                ):
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
                    turn = client.prompt_and_wait(message, timeout=remaining)
                    failure = _unrecovered_provider_failure(frames)
                    if failure is not None:
                        raise HelperCompletionError(failure)
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
                    # Shutdown already owns clients removed from the registry.
                    if owned or not registered:
                        _stop_client(client)
                if worker is not None:
                    worker.join(timeout=5.0)
            if credential_dir is not None:
                credential_dir.cleanup()
            if workdir is not None:
                workdir.cleanup()

    def session_call(self, session_id, command, params=None, *, compaction_settings=None) -> dict[str, Any]:
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
            return _missing_session()
        try:
            if not session.turn_lock.acquire(blocking=command_name != "compact"):
                return {"ok": False, "reason": "session_busy"}
            try:
                if session.closed:
                    return _missing_session()
                if command_name == "compact" and compaction_settings is not None:
                    self._refresh_compaction(session, {
                        **session.launch_params,
                        "settings": {"compaction_settings": compaction_settings},
                    })
                result = _invoke_session_canonical(
                    session.client, command_name, payload
                )
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
                if command_name == "compact":
                    listing = _json_value(session.client.request_raw("get_entries"))
                    if isinstance(listing, Mapping) and isinstance(listing.get("entries"), list):
                        branch = _active_branch(listing["entries"], listing.get("leafId"))
                        entry = next((row for row in reversed(branch) if row.get("type") == "compaction"), None)
                        if entry is not None and entry.get("method"):
                            normalized["method"] = entry["method"]
                if command_name == "get_state":
                    normalized["_hostToolNames"] = sorted(
                        _session_host_tool_names(session)
                    )
                return normalized
            finally:
                session.turn_lock.release()
        except Exception as exc:
            if command_name == "compact" and _is_wait_timeout(exc):
                with self._lock:
                    session.closed = True
            raise
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
            if self._sessions.get(session.registry_key or session.session_id) is session:
                self._sessions.pop(session.registry_key or session.session_id)
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
        session_key = tenancy.scoped_key(session_id)
        owner = tenancy.owner_key()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("chudcode session host is shut down")
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
                            raise RuntimeError("chudcode session host is shut down")
                        existing = self._claim_existing_session_locked(
                            session_id, resume_file, workspace_dir, credential_identity
                        )
                        if existing is not None:
                            return existing
                        victim = self._evict_for_new_session()
                        self._reserved_creations[owner] = self._reserved_creations.get(owner, 0) + 1
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
                                    f"persisted agent session file does not exist: {resume_path}"
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
                                compaction_settings=_turn_compaction_settings(params),
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
                "cannot change chudcode_session_file on a live Chudware session"
            )
        if existing.workspace_dir is None and workspace_dir is not None:
            existing.workspace_dir = workspace_dir
        self._sessions.move_to_end(session_key)
        existing.active_turns += 1
        return existing

    def _evict_for_new_session(self) -> _Session | None:
        owned = sum(tenancy.owns(session.owner) for session in self._sessions.values())
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
        victim = self._sessions.pop(victim_key)
        victim.closed = True
        victim.dispose_started = True
        return victim

    def _rpc_factory(self) -> Any:
        """The injected client factory, else the pinned ``chudcode_rpc.RpcClient``."""
        if self._client_factory is not None:
            return self._client_factory
        try:
            from chudcode_rpc import RpcClient  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Agent service RPC support is unavailable; install the managed "
                "chudcode-rpc==0.3.1 runtime dependency"
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
        if no_session:
            from services.agent.config import memory_backend

            memory = memory_backend()
            if binary is not None and not _binary_has_memory_backend(binary, memory):
                memory = "mnemo"
            if binary is not None and _binary_supports_flag(binary, "--config"):
                overlay = _cad_session_overlay(memory)
                if overlay is not None:
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
            # Synchronous maintenance (notably a GPT-written summary) can exceed
            # RpcClient's 30 s default. Leave 30 s before the worker bridge's bound.
            kwargs["request_timeout"] = 90.0
            # A CAD conversation re-reads Chudcode's prompt on every request. Without
            # these, a session carried the user's own Chudcode skills catalog (18 KB of
            # repo-dev skills; CAD skills come from cad_skill), LSP docs, and a dozen
            # code devices (security_scan, debug, firmware, cpp_*, ast_*, github ...):
            # 106 KB of prompt + tool schema, 72 KB after (measured with get_state).
            kwargs["no_skills"] = True
            # The user's Chudcode MCP servers (stripe, graphify ...) are not CAD tools.
            extra: tuple[str, ...] = ("--no-lsp",)
            if binary is not None and _binary_supports_flag(binary, "--no-mcp"):
                extra += ("--no-mcp",)
            memory = _session_memory_backend(params, workspace)
            if binary is not None and not _binary_has_memory_backend(binary, memory):
                # Older binaries lacking sharpshooter still have project-scoped Mnemopi.
                _LOG.warning("chud at %s lacks memory backend %r; using mnemo", binary, memory)
                memory = "mnemo"
            overlay = (
                _cad_session_overlay(memory, _turn_compaction_settings(params))
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
                # Only a backend this overlay pins is known to register its tools; chud
                # refuses to start when --tools names one it lacks.
                tools += _CAD_MEMORY_TOOLS[memory]
            kwargs["tools"] = tools
            kwargs["extra_args"] = extra
        try:
            _sandbox_client_kwargs(kwargs, params)
            client = factory(**kwargs)
            client._chudware_activity_owner = tenancy.owner_key()
            client._chudware_activity_sandboxed = tenancy.in_cell()
            client._chudware_activity_sdk_argv = (
                tuple(kwargs["command"][4:]) if tenancy.in_cell()
                else tuple(getattr(client, "command", ())))
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
            "Agent service executable not found; set CHUDWARE_CHUDCODE_BIN, install the "
            "signed-payload-adjacent chud binary, or put chud on PATH in developer mode"
        )

    def _refresh_compaction(self, session: _Session, params: Mapping[str, Any]) -> None:
        """Apply changed policy at an idle turn boundary, preserving durable history.

        RPC v2 cannot acknowledge a live method-order change. Immutable overlays and
        a new connection avoid racing its config watcher or changing another run.
        """
        settings = _turn_compaction_settings(params)
        sandbox_class = _sandbox_class(params)
        if (settings == session.compaction_settings
                and sandbox_class == session.sandbox_class):
            return
        state = _safe_client_state(session.client)
        if state.get("isStreaming") or state.get("hasPendingAsyncWork"):
            raise RuntimeError("Cannot change compaction while the session has active work")
        resume_file = state.get("sessionFile") or state.get("session_file")
        if not isinstance(resume_file, str) or not Path(resume_file).is_file():
            raise RuntimeError("Cannot change compaction without the persisted session history")
        replacement = self._make_client(params, no_session=False, resume_file=resume_file)
        try:
            _start_client(replacement)
        except Exception:
            _stop_client(replacement)
            raise
        with self._lock:
            if session.closed or self._shutdown:
                _stop_client(replacement)
                raise RuntimeError("chudcode RPC session was closed")
            old_client = session.client
            session.client = replacement
        _stop_client(old_client)
        try:
            if resume_file is not None:
                replacement.switch_session(resume_file)
            if settings.get("enabled", True) and session.auto_compaction is not None:
                replacement.set_auto_compaction(session.auto_compaction)
            if session.auto_retry is not None:
                replacement.set_auto_retry(session.auto_retry)
            session.compaction_settings = settings
            session.sandbox_class = sandbox_class
            session.launch_params = _client_launch_params(params)
        except Exception:
            with self._lock:
                session.closed = True
            raise

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
                            "chudcode-rpc==0.3.1 is required to register CAD host tools"
                        ) from exc
                    binding = converter(host_tool)
                converted_tools.append(binding)
            publish_host_tools(client, converted_tools, session.host_tool_names)
        host_uris = params.get("host_uris")
        if isinstance(host_uris, Sequence) and not isinstance(host_uris, (str, bytes)):
            client.set_host_uris(host_uris)
        registrar = params.get("register_callbacks")
        if callable(registrar):
            registrar(client)
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
        _start_client(client)
        with self._lock:
            if self._shutdown:
                _stop_client(client)
                raise RuntimeError("chudcode session host is shut down")
            self._service_client = client
            self._service_memory_backend = backend
        return client

    def refresh_catalog_if_stale(self) -> bool:
        """Refresh Chudcode discovery without holding the agent worker's RPC lane.

        Only the no-session service is recycled; conversation processes and active
        turns are never stopped. A fresh catalog reaches the GUI on its next poll.
        """
        if tenancy.in_cell():
            return False
        with self._catalog_refresh_lock:
            if self._shutdown:
                return False
            if self._catalog_refreshing:
                return True
            # Tests with an injected client factory have no real binary to refresh.
            if self._client_factory is not None and self._binary_hint is None:
                return False
            binary = self._resolve_binary(required=True)
            identity = _binary_identity(binary)
            now = self._clock()
            if ((identity == self._catalog_success_binary and
                 now - self._catalog_last_success < _CATALOG_REFRESH_SECONDS) or
                    (identity == self._catalog_attempt_binary and
                     now - self._catalog_last_attempt < _CATALOG_RETRY_SECONDS)):
                return False
            self._catalog_last_attempt = now
            self._catalog_attempt_binary = identity
            self._catalog_refreshing = True
            thread = threading.Thread(target=self._refresh_catalog, args=(binary, identity),
                                      name="chudcode-model-refresh", daemon=True)
            self._catalog_refresh_thread = thread
            thread.start()
            return True

    def _refresh_catalog(self, binary: str, identity: tuple | None = None) -> None:
        try:
            process = subprocess.Popen([binary, "models", "refresh", "--json"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self._catalog_refresh_lock:
                self._catalog_process = process
                stopping = self._shutdown
            if stopping:
                process.terminate()
            try:
                exit_code = process.wait(timeout=_CATALOG_COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            if exit_code:
                raise subprocess.CalledProcessError(exit_code, process.args)
            # The CLI updated Chudcode's cache, but the service still holds its old
            # in-memory registry. Replace only that service under its normal call lock.
            with self._service_lock:
                with self._lock:
                    client = self._service_client if not self._shutdown else None
                    if client is not None:
                        self._service_client = None
                if client is not None:
                    _stop_client(client)
            with self._catalog_refresh_lock:
                if not self._shutdown:
                    self._catalog_last_success = self._clock()
                    self._catalog_success_binary = identity
        except (OSError, subprocess.SubprocessError) as exc:
            if not self._shutdown:
                _LOG.warning("Chudcode model refresh failed (%s)", type(exc).__name__)
        finally:
            with self._catalog_refresh_lock:
                self._catalog_process = None
                self._catalog_refreshing = False

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


def _same_path(left: str, right: str) -> bool:
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


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


def _user_entry_text(entry: Mapping[str, Any]) -> str | None:
    """Chudcode's own user-message text extraction (string or joined text parts)."""
    message = entry.get("message")
    if entry.get("type") != "message" or not isinstance(message, Mapping):
        return None
    if message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, Mapping)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return None


def _is_typed_request(stored: str, typed: str) -> bool:
    """Whether a persisted request is the user's typed text plus host-added blocks.

    Only a genuine migration prefix is stripped, and only a recognized appended block
    may follow the typed text: a distinct prompt that merely starts with the same words
    ("hello" vs "hello\\n\\nchange the other part") is a different message.
    """
    if stored.startswith(_MIGRATION_PREFIX):
        _, marker, request = stored.partition(_MIGRATION_REQUEST_MARKER)
        if marker:
            stored = request
    stored = stored.strip()
    if not stored.startswith(typed):
        return False
    rest = stored[len(typed):]
    if not rest:
        return True
    block = rest.lstrip()
    return "\n\n" in rest[: len(rest) - len(block)] and block.startswith(
        _APPENDED_BLOCK_HEADERS
    )


def _rewind_target(
    path: Sequence[Mapping[str, Any]], typed: str, occurrence: int
) -> str | None:
    """Persisted entry id of the ``occurrence``-th user message carrying ``typed``."""
    seen = 0
    for entry in path:
        text = _user_entry_text(entry)
        if text is None or not _is_typed_request(text, typed):
            continue
        if seen == occurrence:
            return entry["id"]
        seen += 1
    return None


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
        raise ValueError("Agent session doctor returned an invalid status")
    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("Agent session doctor returned invalid findings")
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
            raise RuntimeError("Agent session doctor unexpectedly invoked a model")
        if response_invoked is not False and not prompt_results:
            completed.wait(5.0)
        if prompt_results and prompt_results[-1].get("agentInvoked") is not False:
            raise RuntimeError("Agent session doctor unexpectedly invoked a model")
        if response_invoked is not False and not prompt_results:
            raise RuntimeError(
                "Agent session doctor did not report local completion"
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
    raise RuntimeError("Agent session doctor returned no valid summary")


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


_EXECUTION_PHASES = {"tool_execution_start": "start", "tool_execution_end": "end"}


def _tool_execution_phase(frame: Mapping[str, Any]) -> tuple[str, str, str]:
    """(call id, "start"|"end"|"", tool name) of a Chudcode top-level tool frame."""
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
    from services.agent.chudcode_tools import _ctx

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


def _binary_has_memory_backend(path: str, backend: str) -> bool:
    """Whether ``chud`` knows ``backend``: it exits at startup on an unknown one.

    Only "off" predates the setting listing. CAD's "mnemo" is named "mnemopi"
    by Chudcode; probe its settings just like sharpshooter's.
    """
    if backend == "off":
        return True
    runtime_backend = "mnemopi" if backend == "mnemo" else backend
    return f'"{runtime_backend}.' in _binary_probe(path, ("config", "list", "--json"))


#: Chudcode settings every CAD conversation process runs with (``--config`` overlay).
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
)
#: Per-backend memory settings. Chudcode keys every backend's store by the process cwd,
#: which is the CAD project folder, so memory never crosses projects; Mnemopi's scoping
#: is pinned so a user-level `global` setting cannot pool all projects into one bank.
_CAD_MEMORY_SETTINGS = {
    "off": "memory:\n  backend: \"off\"\n",
    "mnemo": "memory:\n  backend: mnemopi\nmnemopi:\n  scoping: per-project\n",
    "sharpshooter": "memory:\n  backend: sharpshooter\n",
}
#: The Chudcode tools each backend provides. chud refuses to start when --tools names a
#: tool the configured backend does not register.
_CAD_MEMORY_TOOLS: dict[str, tuple[str, ...]] = {
    "off": (),
    "mnemo": ("retain", "recall", "reflect"),
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
        "workspace_dir", "chudcode_session_dir", "system_prompt", "memory_backend",
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
    target = root / "chudcode" / f"cad-session-{memory}-{digest}.yml"
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
    if type(client).__module__.startswith("chudcode_rpc."):
        from services.worker_activity import note_sdk_launch

        note_sdk_launch(sandboxed=bool(getattr(client, "_chudware_activity_sandboxed", False)))
    client.start()
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
_PROMPT_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_PROMPT_MAX_ATTACHMENTS = 32


def _prompt_payload(
    prompt: str, attachments_value: Any, images_value: Any
) -> tuple[str, list[dict[str, Any]]]:
    from services.agent.tool_images import ImageUnavailable, read_image_bytes

    images: list[dict[str, Any]] = []
    file_refs: list[str] = []
    remaining = _PROMPT_MAX_SOURCE_BYTES

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
        if path_value and (kind == "image" or (mime and mime.startswith("image/"))):
            if len(images) >= _PROMPT_MAX_IMAGES:
                raise ValueError("attachment_image_count_exceeded")
            path = Path(path_value).expanduser()
            try:
                raw = read_image_bytes(str(path), max_bytes=remaining)
            except ImageUnavailable as exc:
                raise ValueError(f"attachment_image_unavailable: {exc}") from exc
            if not raw:
                raise ValueError("attachment_image_data_invalid")
            remaining -= len(raw)
            images.append({
                "type": "image",
                "mimeType": mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "data": base64.b64encode(raw).decode("ascii"),
            })
        elif path_value:
            name = _nonempty_string(attachment.get("name")) or Path(path_value).name
            file_refs.append(f"- {name}: {path_value}")
    if file_refs:
        prompt = prompt.rstrip() + "\n\nAttached workspace files:\n" + "\n".join(file_refs)
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

def worker_activity() -> dict:
    host = _DEFAULT_CHUDCODE_HOST
    return host.worker_activity() if host is not None else {"by_kind": {}}


def _probe_sdk_activity(client: Any, probe: _ActivityProbe) -> None:
    try:
        # Inspect raw fields: the pinned SDK's typed parser supplies false defaults
        # for absent fields, which cannot establish a truthful idle attestation.
        state = client._request("get_state")
        flags = ("isStreaming", "isCompacting", "hasPendingAsyncWork", "isSettled")
        if (not isinstance(state, dict) or any(type(state.get(k)) is not bool for k in flags)
                or type(state.get("queuedMessageCount")) is not int
                or state["queuedMessageCount"] < 0):
            return
        with client._state_lock:
            pending = len(client._pending)
        with client._event_condition:
            prompts = len(client._pending_prompt_ids)
        host_calls = len(client._pending_host_tool_calls) + len(client._pending_host_uri_requests)
        probe.result = {
            "sdk_runtime": int(state["isStreaming"] or state["isCompacting"]
                               or state["hasPendingAsyncWork"] or not state["isSettled"]),
            "sdk_queued_messages": state["queuedMessageCount"],
            "sdk_pending_calls": pending + prompts + host_calls,
        }
        stt = client._request("stt_status")
        if not isinstance(stt, dict) or stt.get("state") not in {"idle", "recording", "transcribing"}:
            return
        probe.result["sdk_stt"] = int(stt["state"] != "idle")
        probe.complete = True
    except Exception:
        # Missing capability, death, malformed state and a transport failure are
        # unknown; never infer idle from an empty persistent session registry.
        pass
    finally:
        probe.done.set()



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
    from chudcode_rpc import RpcClient

    argv = list(RpcClient(**kwargs).command)
    kwargs["command"] = tenancy.sandbox_argv(argv, _sandbox_class(params))


def _session_link(session_id: str, identity: str | None, path: str | None = None) -> str | None:
    """Persist opaque conversation → history bindings, never handles in filenames."""
    root = tenancy.actor_state_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = root / "chudcode-session-links.sqlite"
    if database.is_symlink():
        raise ValueError("cell_refused")
    with sqlite3.connect(database) as db:
        database.chmod(0o600)
        db.execute("CREATE TABLE IF NOT EXISTS links "
                   "(handle TEXT PRIMARY KEY, credential TEXT NOT NULL, file TEXT NOT NULL)")
        key = tenancy.scoped_key(session_id)
        row = db.execute("SELECT credential, file FROM links WHERE handle=?", (key,)).fetchone()
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
