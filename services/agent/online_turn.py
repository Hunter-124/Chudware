"""Pure turn-assembly seams for the online agent lane."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from services.agent.request_context import cell_path, in_cell


# Host-call threads bind a caller-owned provider factory, never credential bytes in
# tool arguments. A bounded planner retains its provider.
_HELPER_RUNTIME: ContextVar[Any] = ContextVar("chudware_helper_runtime", default=None)
_TOOL_SESSION_ID: ContextVar[str | None] = ContextVar("chudware_tool_session_id", default=None)


def current_helper_runtime() -> Any:
    return _HELPER_RUNTIME.get()


def current_tool_session_id() -> str | None:
    """Owning live conversation for this host call, never a model tool argument."""
    return _TOOL_SESSION_ID.get()


@contextmanager
def bind_helper_runtime(runtime: Any, *, session_id: str | None = None) -> Iterator[None]:
    token = _HELPER_RUNTIME.set(runtime)
    session_token = _TOOL_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _HELPER_RUNTIME.reset(token)
        _TOOL_SESSION_ID.reset(session_token)


def resolve_workspace(raw_params: Mapping[str, Any]) -> tuple[str, str, str | None]:
    """Return (project root, requested save directory, refusal reason)."""
    if "workspace_dir" not in raw_params:
        return "", "", None
    raw_workspace = raw_params.get("workspace_dir")
    if not isinstance(raw_workspace, str) or not raw_workspace.strip():
        return "", "", "invalid_workspace: workspace_dir must be a non-empty directory"
    try:
        candidate = cell_path(raw_workspace) if in_cell() else Path(raw_workspace).expanduser().resolve()
        if not candidate.is_dir():
            return "", "", f"invalid_workspace: not an existing directory: {candidate}"
        # The selected directory is authoritative, including inside another
        # project. Ancestor discovery would silently widen tool/save scope.
        return str(candidate), str(candidate), None
    except Exception as exc:  # noqa: BLE001 - invalid workspace is an RPC refusal
        return "", "", f"invalid_workspace: {type(exc).__name__}: {exc}"


def compose_request_text(request: str, migration: str, editor: str = "") -> str:
    """One formatter for the migration preflight estimate and actual RPC request."""
    prompt = f"{migration}\n\nCURRENT USER REQUEST\n{request}" if migration else request
    return f"{prompt}\n\n{editor}" if editor else prompt


def format_duration(seconds: float) -> str:
    """Model-facing duration: whole seconds under two minutes, else minutes."""
    if seconds < 120:
        return f"{max(0, round(seconds))} s"
    return f"{round(seconds / 60, 1):g} min"


def turn_budget_notice(budget_s: float | None, *, readonly: bool) -> str:
    """The turn-start line naming an explicit wall-clock budget; empty without one.

    Only a caller-sent positive ``timeout`` is a budget (run_mission.py --timeout-s,
    missions, benchmark trials). GUI turns send none, so they never get this line.
    """
    if budget_s is None or not budget_s > 0:
        return ""
    budget = format_duration(budget_s)
    if readonly:
        return (f"This turn has a hard {budget} budget; reach your answer early, "
                "then use the remaining time to check it.")
    return (f"This turn has a hard {budget} budget; save deliverables as soon as they "
            "are correct, then verify.")


def resolve_model_and_effort(
    request: str,
    raw_params: Mapping[str, Any],
    cfg: Mapping[str, Any],
    byok_key: Any,
    transient_call: Any,
    auth_providers: Any,
) -> tuple[Any, str, str, Any, list[dict[str, Any]] | None]:
    """Resolve the turn preferences, available models and explicit override."""
    from services.agent.chudcode_host import byok_provider_env
    from services.agent.preferences import resolve_turn_preferences
    from services.agent.role_models import _auto_local_provider, resolve_role_model

    def param_str(key: str) -> str:
        value = raw_params.get(key)
        return value.strip() if isinstance(value, str) else ""

    requested_mode = param_str("mode")
    styles = {"agent": "do_it", "plan": "check_with_me", "ask": "answer_only"}
    turn_cfg = cfg
    if requested_mode in styles:
        turn_cfg = {
            **cfg,
            "copilot": {**(cfg.get("copilot") or {}), "work_style": styles[requested_mode]},
        }
    preferences = resolve_turn_preferences(
        request, cfg=turn_cfg,
        feature_count=max(0, int(raw_params.get("feature_count") or 0)),
        doc_count=max(1, int(raw_params.get("doc_count") or 1)),
        context_tokens=max(0, int(raw_params.get("context_tokens") or 0)),
        context_window=max(0, int(raw_params.get("context_window") or cfg.get("context_window") or 0)),
    )
    provider, model = param_str("provider"), param_str("model")
    assignment = None
    candidates = None
    if not provider and not model:
        catalog = transient_call("get_available_models", {})
        auth = transient_call("get_auth_state", {})
        if not catalog.get("ok") or not auth.get("ok"):
            raise RuntimeError("Cannot inspect available models. Repair the runtime or sign in in Accounts.")
        authenticated = {row["id"] for row in auth_providers(auth) if row["authenticated"]}
        candidates = [
            {"provider": row["provider"], "model": row["id"],
             "base_url": row.get("baseUrl") or row.get("base_url") or ""}
            for row in catalog.get("models", [])
            if isinstance(row, Mapping) and row.get("provider") and row.get("id")
            and (row["provider"] in authenticated
                 or _auto_local_provider(row["provider"], cfg, row.get("baseUrl") or row.get("base_url")))
        ]
        assignment = resolve_role_model(
            "chat", cfg=cfg, request=request, turn_preferences=preferences, enabled=candidates
        )
        provider, model = assignment.provider, assignment.model
    else:
        provider = provider or str(cfg.get("provider") or "")
        model = model or str(cfg.get("model") or "")
    if preferences.local_only and not _auto_local_provider(provider, cfg):
        raise ValueError("This computer only requires a loopback model. Configure a local model in Accounts.")
    if byok_key is not None and byok_provider_env(provider) is None:
        raise ValueError("byok_unsupported_online")
    return preferences, provider, model, assignment, candidates


def resolve_settings(
    raw_params: Mapping[str, Any],
    preferences: Any,
    provider_name: str,
    model_name: str,
    cfg: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the Chudcode session settings for one online turn.

    Compaction is the persisted user choice (``agent.compaction_strategy``), carried as
    ``compaction_strategy`` plus ``compaction_settings`` (Chudcode ``compaction.*``
    sub-keys) for the conversation process configuration. A request cannot replace
    it: the process configuration is shared by every turn of the session.
    ``auto_compaction`` stays a session switch — sent only when the request names it,
    or for ``auto`` when the turn policy asks for compaction — so a turn never resets
    the user's per-session Auto-compaction choice. Raises :class:`ValueError` for an
    unrecognized strategy.
    """
    from services.agent.config import online_compaction_settings

    value = raw_params.get("settings")
    settings = dict(value) if isinstance(value, Mapping) else {}
    for key in ("compaction_strategy", "compaction_settings"):
        settings.pop(key, None)
    strategy, compaction = online_compaction_settings(dict(cfg) if cfg is not None else None)
    settings["compaction_strategy"] = strategy
    settings["compaction_settings"] = compaction
    if not any(key in raw_params or key in settings for key in ("thinking", "reasoning_effort")):
        settings["thinking"] = preferences.thinking
    if preferences.schedule_compaction and strategy == "auto":
        settings.setdefault("auto_compaction", True)
    if provider_name:
        settings["provider"] = provider_name
    if model_name:
        settings["model"] = model_name
    for key in (
        "thinking", "reasoning_effort", "steering_mode", "follow_up_mode",
        "interrupt_mode", "auto_compaction", "auto_retry",
        "subagent_subscription", "queue",
    ):
        if key in raw_params:
            settings[key] = raw_params[key]
    # Task sub-agents' CAD calls are only visible to the feed/trace and the
    # progress watchdog through their forwarded `subagent_event` frames.
    settings.setdefault("subagent_subscription", "events")
    return settings
