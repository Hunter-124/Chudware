"""Anthropic adapter — default provider (vision + codegen).

Uses the official ``anthropic`` SDK. Thinking / effort wire shape is **model-
aware** (mirrors omp ``model-thinking`` + ``anthropic.ts`` ``buildParams``):

* Adaptive (Opus/Sonnet/Haiku ≥4.6, Sonnet/Opus 5, Fable/Mythos):
  ``thinking: {type: adaptive}`` + ``output_config.effort``.
* Budget-effort (Opus 4.5 only):
  ``thinking: {type: enabled, budget_tokens}`` + ``output_config.effort``.
* Budget (Sonnet/Haiku 4.5 and older Claude 3/4):
  ``thinking: {type: enabled, budget_tokens}`` only —
  ``output_config.effort`` is rejected with HTTP 400
  ``This model does not support the effort parameter.``

Always sending adaptive + effort (the previous default) 400s non-adaptive
models on every call, which the design machine then mislabels as
"budget exhausted".
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from .base import Provider

DEFAULT_MODEL = "claude-opus-4-8"

# Effort ladders accepted on the wire / by our config UI.
_EFFORTS = frozenset({"low", "medium", "high", "max", "xhigh"})

# Map user effort → thinking.budget_tokens for budget-mode models.
_BUDGET_TOKENS = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 24576,
}


def _normalize_effort(raw: str | None) -> str:
    effort = (raw or "high").strip().lower()
    if effort in ("med",):
        effort = "medium"
    if effort in ("auto", "none", "off", ""):
        effort = "high"
    if effort not in _EFFORTS:
        effort = "high"
    # API accepts xhigh on newer adaptive models; budget maps treat it as max-ish.
    return effort


def _parse_claude_model(model: str) -> tuple[str, tuple[int, ...]]:
    """Return (kind, version_tuple) for a Claude model id.

    kind: opus|sonnet|haiku|fable|mythos|other
    version: e.g. (4, 5), (4, 8), (5,) — empty when unknown.

    Handles both modern (``claude-sonnet-4-5-20250929``, ``claude-sonnet-5``)
    and legacy (``claude-3-5-sonnet-20241022``) id shapes.
    """
    m = (model or "").lower().strip()
    # Strip provider / date / bedrock prefixes and trailing aliases.
    m = m.split("/")[-1]
    m = re.sub(r"^(anthropic\.|au\.anthropic\.|eu\.anthropic\.)", "", m)
    m = re.sub(r"-v\d+:\d+$", "", m)
    # Drop trailing YYYYMMDD date stamps so they never become "major".
    m = re.sub(r"-\d{8}$", "", m)

    kind = "other"
    for k in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if k in m:
            kind = k
            break

    ver: tuple[int, ...] = ()
    if kind != "other":
        # Legacy: claude-3-5-sonnet / claude-3-opus
        legacy = re.search(r"claude[-_](\d+)(?:[-_.](\d+))?[-_]" + kind, m)
        if legacy:
            major = int(legacy.group(1))
            if legacy.group(2):
                ver = (major, int(legacy.group(2)))
            else:
                ver = (major,)
        else:
            # Modern: claude-sonnet-4-5 / claude-sonnet-5 / claude-opus-4-8
            modern = re.search(kind + r"[-_](\d+)(?:[-_.](\d+))?", m)
            if modern:
                major = int(modern.group(1))
                # Guard: an 8-digit "minor" is a date that escaped the strip.
                if modern.group(2) and len(modern.group(2)) < 6:
                    ver = (major, int(modern.group(2)))
                else:
                    ver = (major,)
    if not ver:
        mk2 = re.search(r"claude[-_]?(\d+)(?:[-_.](\d+))?", m)
        if mk2:
            major = int(mk2.group(1))
            if mk2.group(2) and len(mk2.group(2)) < 6:
                ver = (major, int(mk2.group(2)))
            else:
                ver = (major,)

    return kind, ver


def thinking_mode_for_model(model: str) -> str:
    """Classify model → ``adaptive`` | ``budget_effort`` | ``budget`` | ``none``.

    Mirrors omp ``inferThinkingControlMode`` for anthropic-messages.
    """
    kind, ver = _parse_claude_model(model)
    if kind in ("fable", "mythos"):
        return "adaptive"
    if not ver:
        # Unknown Claude-ish id: prefer adaptive only for clearly modern names.
        mid = (model or "").lower()
        if any(s in mid for s in ("opus-4-6", "opus-4-7", "opus-4-8", "sonnet-4-6",
                                  "sonnet-4-7", "sonnet-5", "opus-5", "haiku-4-6")):
            return "adaptive"
        if "claude" in mid:
            return "budget"
        return "none"

    major = ver[0]
    minor = ver[1] if len(ver) > 1 else 0

    # Gen 5 (sonnet-5 / opus-5): adaptive.
    if major >= 5:
        return "adaptive"
    # 4.6+: adaptive for all kinds.
    if major == 4 and minor >= 6:
        return "adaptive"
    # Opus 4.5: budget_tokens + output_config.effort.
    if kind == "opus" and major == 4 and minor >= 5:
        return "budget_effort"
    # Sonnet/Haiku 4.5 and older: budget_tokens only (effort 400s).
    if major >= 3:
        return "budget"
    return "none"


def _budget_tokens_for(effort: str, max_tokens: int) -> int:
    """Pick a legal ``budget_tokens`` (< max_tokens, ≥1024 when possible)."""
    want = _BUDGET_TOKENS.get(effort, 4096)
    # Anthropic requires budget_tokens < max_tokens and typically ≥1024.
    cap = max(1, int(max_tokens) - 1)
    budget = min(want, cap)
    if budget < 1024 and cap >= 1024:
        budget = 1024
    return max(1, budget)


def build_thinking_kwargs(
    model: str,
    *,
    reasoning_effort: str | None = None,
    max_tokens: int = 8000,
) -> dict[str, Any]:
    """Return extra ``messages.create`` kwargs for thinking + effort.

    Pure / offline-testable. Empty dict when the model has no thinking surface.
    """
    effort = _normalize_effort(reasoning_effort)
    mode = thinking_mode_for_model(model)
    if mode == "none":
        return {}

    if mode == "adaptive":
        # Adaptive models reject temperature/top_p/budget_tokens; effort rides
        # on output_config. Map our "xhigh" through as-is (API accepts it).
        wire_effort = effort
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": wire_effort},
        }

    budget = _budget_tokens_for(effort, max_tokens)
    # If the caller's max_tokens is too tight for a real budget, lift it so the
    # request is legal (budget_tokens must be < max_tokens).
    effective_max = max_tokens
    if budget >= effective_max:
        effective_max = budget + 1024

    thinking: dict[str, Any] = {"type": "enabled", "budget_tokens": budget}
    out: dict[str, Any] = {"thinking": thinking}
    if effective_max != max_tokens:
        out["max_tokens"] = effective_max
    if mode == "budget_effort":
        # Opus 4.5: effort alongside budget_tokens.
        out["output_config"] = {"effort": effort if effort != "xhigh" else "max"}
    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str | None = None,
    ):
        import anthropic

        # Empty/None api_key → SDK resolves ANTHROPIC_API_KEY from the environment.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        # low|medium|high|max|xhigh|auto/None — model-aware wire mapping (see above).
        self.reasoning_effort = reasoning_effort

    def _block(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"type": "text", "text": part["text"]}
        data = base64.standard_b64encode(Path(part["path"]).read_bytes()).decode("utf-8")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.get("mime", "image/png"),
                "data": data,
            },
        }

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]

    def complete_ex(self, system: str, messages: list[dict],
                    max_tokens: int = 8000) -> tuple[str, str]:
        api_messages = [
            {"role": m["role"], "content": [self._block(p) for p in m["content"]]}
            for m in messages
        ]
        extra = build_thinking_kwargs(
            self.model,
            reasoning_effort=self.reasoning_effort,
            max_tokens=max_tokens,
        )
        # build_thinking_kwargs may raise max_tokens when budget mode needs headroom.
        call_max = int(extra.pop("max_tokens", max_tokens))
        # omp: output_config.effort requires the effort-2025-11-24 beta header;
        # without it some endpoints 400 the field even on adaptive-capable models.
        create_kwargs: dict[str, Any] = dict(extra)
        if isinstance(create_kwargs.get("output_config"), dict) and create_kwargs[
            "output_config"
        ].get("effort"):
            create_kwargs["extra_headers"] = {
                "anthropic-beta": "effort-2025-11-24",
            }
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=call_max,
            system=system,
            messages=api_messages,
            **create_kwargs,
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        # Adaptive thinking returns `thinking` blocks (and possibly redacted ones); collect
        # the visible reasoning so the loop can show "what the model was thinking".
        thinking = "".join(
            getattr(b, "thinking", "") for b in resp.content if b.type == "thinking"
        )
        try:
            from . import token_usage as _tu  # noqa: PLC0415
            u = _tu.extract_anthropic_usage(resp)
            _tu.record_usage(
                provider=self.name,
                model=str(self.model or ""),
                prompt_tokens=u.get("prompt_tokens"),
                completion_tokens=u.get("completion_tokens"),
                total_tokens=u.get("total_tokens"),
                source="api",
            )
        except Exception:  # noqa: BLE001
            pass
        return text, thinking
