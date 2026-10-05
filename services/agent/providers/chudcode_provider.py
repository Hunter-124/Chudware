"""Provider-shaped adapter for CAD helper model steps (regen diagnosis, ECAD planning).

Online, a CAD tool's model step is one tool-free completion through the compiled Chudcode
runtime (``ChudcodeSessionHost.complete_text``): Chudcode owns auth, provider retry and the
request, and this module builds no SDK client and sends nothing itself. An explicit
offline/local-only configuration instead binds the configured provider, which must be
offline-safe. Neither path falls back to the other.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from services.agent.providers.base import Provider


class ChudcodeProvider(Provider):
    """``complete(system, messages)`` as one tool-free Chudcode runtime completion.

    ``provider``/``model`` are an explicit selection applied by the runtime (a rejection
    fails the call); both empty means the runtime's own selected model. ``cancel`` is a
    zero-argument callable; when it returns true the helper process is stopped. Failures
    raise ``chudcode_host.HelperCompletionError`` with the runtime's reason.
    """

    name = "chudcode"
    supports_vision = False

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        cancel: Callable[[], bool] | None = None,
        timeout: float | None = None,
        compaction_settings: dict | None = None,
        runtime_params: dict | None = None,
        host: Any = None,
    ) -> None:
        self._provider = provider or None
        self._model = model or None
        self._cancel = cancel
        self._timeout = timeout
        self._compaction_settings = compaction_settings
        self._runtime_params = runtime_params
        self._host = host

    @property
    def model(self) -> str:
        if self._provider and self._model:
            return f"{self._provider}/{self._model}"
        return self._model or "runtime-default"

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        """``max_tokens`` is not forwarded: RPC v2 has no per-request output cap."""
        from services.agent.chudcode_host import get_default_chudcode_host

        host = self._host if self._host is not None else get_default_chudcode_host()
        kwargs: dict[str, Any] = {
            "provider": self._provider,
            "model": self._model,
            "cancel": self._cancel,
            "compaction_settings": self._compaction_settings,
            "runtime_params": self._runtime_params,
        }
        if self._timeout is not None:
            kwargs["timeout"] = self._timeout
        return host.complete_text(system, _prompt_text(messages), **kwargs)


def _prompt_text(messages: Sequence[dict]) -> str:
    """Join user text parts; a helper completion carries no history, images or tools."""
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            raise ValueError("helper completions take user messages only")
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content or ():
            if not isinstance(part, dict) or part.get("type") != "text":
                raise ValueError("helper completions take text parts only")
            texts.append(str(part.get("text") or ""))
    return "\n\n".join(text for text in texts if text)


def helper_provider(
    role: str,
    *,
    cancel: Callable[[], bool] | None = None,
    timeout: float | None = None,
) -> Provider:
    """The provider for one CAD helper model step performed on behalf of mission ``role``.

    ``offline_mode``/``compliance_mode: local_only`` bind the configured provider and
    refuse anything that is not offline-safe. Otherwise the step runs through the Chudcode
    runtime with the role's governed provider/model (``ai_roles``, then config.json
    ``provider``/``model``). With neither set the runtime selects its own model; an
    incomplete selection fails rather than silently dropping the user's choice.
    Selected compaction settings also cross the host boundary unchanged.
    During an online host-tool call the owning turn's hidden runtime factory takes
    precedence: its exact model, credential scope, selected compaction and Stop remain
    authoritative. Credentials never travel as tool arguments or provider output.
    """
    from services.agent import config as agent_config
    from services.agent.role_models import (
        harness_role_for_mission_role,
        resolve_role_model,
    )
    from services.agent.online_turn import current_helper_runtime

    runtime = current_helper_runtime()
    if runtime is not None:
        if not callable(runtime):
            raise RuntimeError("helper runtime context is invalid")
        return runtime(role, cancel=cancel, timeout=timeout)

    cfg = dict(agent_config.load_config() or {})
    if agent_config.local_only_enabled(cfg):
        provider = agent_config.get_provider(cfg=cfg, role=role)
        if not agent_config.provider_offline_safe(provider):
            raise agent_config.OfflineModeError(
                "offline helper model step requires a loopback or deterministic provider"
            )
        return provider

    assignment = resolve_role_model(harness_role_for_mission_role(role), cfg=cfg)
    provider_id = str(getattr(assignment, "provider", "") or "").strip()
    model_id = str(getattr(assignment, "model", "") or "").strip()
    if not provider_id:
        provider_id = str(cfg.get("provider") or "").strip()
    if not model_id:
        model_id = str(cfg.get("model") or "").strip()
    _, compaction_settings = agent_config.online_compaction_settings(cfg)
    return ChudcodeProvider(
        provider=provider_id or None,
        model=model_id or None,
        cancel=cancel,
        timeout=timeout,
        compaction_settings=compaction_settings,
    )


__all__ = ["ChudcodeProvider", "helper_provider"]
