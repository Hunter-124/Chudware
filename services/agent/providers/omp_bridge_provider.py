"""OMP bun-sidecar provider — the cloud path for the Python agent stack.

The sidecar (``vendor/omp/bridge/src/bridge.ts``, run by ``bun``) owns CAD's shared
credential store: keys typed into Settings *and* OAuth subscription logins (Claude
Pro/Max, Kimi For Coding, …) live in its SQLite ``AuthStorage``, and ``handleComplete``
falls back to that store whenever a request carries no ``apiKey``. Routing the Python
providers through it is what lets an OAuth-only sign-in actually complete a turn: the
raw vendor SDKs only ever see an ``api_key``, and several of them aren't installed on
every host to begin with.

Transport is the existing :mod:`services.agent.omp_bridge` client — the same
process-wide ``bun run bridge.ts`` child that ``compaction`` and ``project_memory``
already talk to, with its own respawn-once-on-death, id correlation and atexit reaping.
``complete`` is just one more method on it::

    -> {"id":7,"method":"complete","params":{provider,model,system,messages,maxTokens,…}}
    <- {"id":7,"ok":true,"result":{"text":…,"thinking":…,"usage":…,"stopReason":…}}

Errors surface as :class:`~services.agent.omp_bridge.BridgeUnavailable` (a
``RuntimeError``) carrying the provider's HTTP ``status`` where the bridge reported one,
so ``agent_turn``'s taxonomy buckets a 401 as ``provider_auth`` rather than a generic
retryable failure — same treatment the SDK providers get from their own exceptions.

Module scope is stdlib-only on purpose (the bridge client is imported lazily, like every
other consumer of it): ``services/worker_main.py`` silently drops a whole RPC table when
a module-level import raises.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .base import Provider

# handleComplete() awaits the FULL completion before answering, so this has to tolerate a
# slow reasoning-model turn, not just RPC latency — same 300 s the C++ caller allows
# (core/agent/providers.cpp, OMP bridge branch). CHUDWARE_OMP_BRIDGE_TIMEOUT overrides.
DEFAULT_TIMEOUT_S = 300.0

# CAD provider id -> the id OMP's catalog knows. anthropic-sub is CAD's OAuth-subscription
# id; OMP has a single top-level "anthropic" key and auto-detects OAuth vs API key from the
# token shape, so both CAD ids must speak as "anthropic". Mirrors the wire_provider mapping
# in core/agent/providers.cpp's bridge branch.
_WIRE_PROVIDER = {"anthropic-sub": "anthropic"}


def _bridge():
    from services.agent import omp_bridge  # noqa: PLC0415 — stdlib-only module scope

    return omp_bridge


def _wire_part(part: Any) -> dict:
    """base.py content part -> bridge.ts readPart() shape."""
    if not isinstance(part, dict):
        return {"type": "text", "text": str(part)}
    if part.get("type") == "image":
        out = {"type": "image", "path": str(part.get("path") or "")}
        mime = part.get("mime")
        if mime:
            out["mime"] = str(mime)
        return out
    return {"type": "text", "text": str(part.get("text") or "")}


def _wire_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for message in messages or []:
        role = "assistant" if str(message.get("role") or "") == "assistant" else "user"
        content = message.get("content")
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        else:
            parts = [_wire_part(p) for p in (content or [])]
        out.append({"role": role, "content": parts})
    return out


def _usage_int(usage: dict, key: str) -> int | None:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _default_timeout() -> float:
    raw = os.environ.get("CHUDWARE_OMP_BRIDGE_TIMEOUT") or ""
    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return seconds if seconds > 0 else DEFAULT_TIMEOUT_S


# Kimi K3 (kimi-code subscription) is tuned for NATIVE tool calling: with no `tools`
# array on the wire it drifts out of the harness's text dialect — it answers with a prose
# announcement ("I'll build this inline — let me pull the modeling pack first.") and ends
# the turn (stop_reason "stop") WITHOUT emitting the fenced ```json tool block, so zero
# tools run and the harness mis-reads the turn as a finish. Measured 2026-07-27: ~25-50%
# of k3-256k replies at any reasoning effort. This suffix re-anchors the dialect.
_KIMI_CODE_DIALECT_NOTE = (
    "\n\nCRITICAL — this environment has NO native tool-calling channel. The ONLY way to "
    "act is to end every reply with exactly one fenced json block:\n"
    '```json\n{"tool": "<tool_name>", "args": {<kwargs>}}\n```\n'
    "(or the fenced finish envelope when the goal is met). NEVER stop at a prose "
    "announcement of what you are about to do — the announcement alone executes nothing. "
    "Emit the fenced block in the SAME reply."
)



_THINK_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _directive_from_thinking(thinking: str) -> str:
    """A fenced tool/finish block the model left in its REASONING channel.

    k3 often emits the JSON directive while thinking and returns empty text.
    Salvaging it turns a wasted, fully-billed resample into a free hit; the
    block is only accepted when it parses AND looks like a real directive, so
    a stray brace in prose never becomes a phantom tool call.
    """
    for match in _THINK_FENCE_RE.finditer(thinking or ""):
        blob = match.group(1)
        try:
            obj = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and (
            obj.get("tool") or obj.get("done") or obj.get("status")
        ):
            return f"```json\n{blob}\n```"
    return ""


#: Per-(provider, model) sidecar catalog caps, cached for the life of the worker.
#: The worker is a fresh fork per turn, so this is at most ONE extra `models_list`
#: call per turn against an already-running sidecar (~0.4 s cold, served from its
#: bundled catalog — no network).
_CATALOG_CAPS: dict[tuple[str, str], dict[str, Any] | None] = {}


def catalog_caps(provider: str, model: str) -> dict[str, Any] | None:
    """Authoritative ``{context, max_output, vision, tools}`` from the OMP catalog.

    ``services.agent.model_catalog`` only knows providers it has hardcoded rows or
    id-substring heuristics for, and it defaults everything else to the LOCAL
    8192/no-vision row. For kimi-code/k3 that produced context=8192 (real:
    1,048,576), max_output=0 (real: 131,072) and vision=False (k3 takes images) —
    so the desktop context meter was measuring a 1M window against an 8192 ruler.
    The sidecar already serves the curated row the GUI itself trusts; ask it
    instead of adding another guess to the heuristic table.

    Returns None when the sidecar has no row (or is unreachable) so the caller
    keeps whatever it already resolved — never a fabricated capability.
    """
    key = (str(provider or ""), str(model or ""))
    if key in _CATALOG_CAPS:
        return _CATALOG_CAPS[key]
    caps: dict[str, Any] | None = None
    try:
        result = _bridge().call("models_list", {"provider": key[0]})
        for row in (result or {}).get("models") or []:
            if not isinstance(row, dict) or str(row.get("id") or "") != key[1]:
                continue
            ctx = int(row.get("contextWindow") or 0)
            if ctx > 0:
                caps = {
                    "context": ctx,
                    "max_output": int(row.get("maxTokens") or 0),
                    "vision": bool(row.get("vision")),
                    "tools": bool(row.get("supportsTools", True)),
                }
            break
    except Exception:  # noqa: BLE001 — capability lookup never breaks a turn
        caps = None
    _CATALOG_CAPS[key] = caps
    return caps


class OmpBridgeProvider(Provider):
    """A cloud model served through the OMP bun sidecar."""

    name = "omp-bridge"
    supports_vision = True

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        reasoning_effort: str | None = None,
        supports_vision: bool = True,
        timeout: float | None = None,
    ):
        # Report under the CAD provider id (anthropic, zenmux, …) rather than a generic
        # "omp-bridge": config.provider_offline_safe, usage accounting and the role-model
        # tables all key off provider.name.
        self.name = provider
        self.wire_provider = _WIRE_PROVIDER.get(provider, provider)
        self.model = model
        self.api_key = api_key or ""
        self.base_url = base_url or None
        self.reasoning_effort = reasoning_effort or ""
        self.supports_vision = bool(supports_vision)
        self.timeout = float(timeout) if timeout and timeout > 0 else _default_timeout()

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]

    def complete_ex(self, system: str, messages: list[dict],
                    max_tokens: int = 8000) -> tuple[str, str]:
        bridge = _bridge()
        from . import base as _base  # noqa: PLC0415 — stdlib-only module scope
        # Live-stream seam: when the calling thread installed a provider delta
        # sink (agent_turn does for GUI turns), ask the bridge to stream and
        # forward each fragment to the sink. Deltas are DISPLAY-ONLY — the
        # final result normally carries the full text and is what we return.
        #
        # Keep a private per-attempt copy too. The pi-ai stream can emit real
        # fragments and then resolve to an AssistantMessage with empty content;
        # the transport can also die before its final response line. In either
        # case the display sink is not a parse buffer, so without this copy the
        # already-paid reply is irretrievably lost.
        sink = _base.provider_delta_sink()
        streamed_text: list[str] = []
        streamed_thinking: list[str] = []
        on_event = None
        if sink is not None:
            def on_event(kind: str, data: dict) -> None:  # noqa: F811
                if kind != "delta" or not isinstance(data, dict):
                    return
                frag_text = data.get("text")
                frag_think = data.get("thinking")
                if frag_text:
                    chunk = str(frag_text)
                    streamed_text.append(chunk)
                    sink(chunk, "text")
                if frag_think:
                    chunk = str(frag_think)
                    streamed_thinking.append(chunk)
                    sink(chunk, "thinking")

        # K3 dialect re-anchor: see _KIMI_CODE_DIALECT_NOTE. Applies to the system prompt
        # only; user/assistant history stays verbatim.
        system_text = system or ""
        if self.wire_provider == "kimi-code":
            system_text += _KIMI_CODE_DIALECT_NOTE
        params: dict[str, Any] = {
            "provider": self.wire_provider,
            "model": self.model,
            "system": system_text,
            "messages": _wire_messages(messages),
            "maxTokens": int(max_tokens),
        }
        # An absent apiKey is meaningful: handleComplete then resolves the credential from the
        # sidecar's own AuthStorage, which is the only place an OAuth subscription lives.
        if self.api_key:
            params["apiKey"] = self.api_key
        if self.base_url:
            params["baseUrl"] = self.base_url
        if self.reasoning_effort:
            params["reasoningEffort"] = self.reasoning_effort
        if sink is not None:
            params["stream"] = True

        # OMP owns provider retry policy. Its stream adapters already discard and
        # retry genuinely empty completions with bounded backoff; resampling again
        # here multiplied one empty answer into as many as six paid wire requests
        # before DAG retry policy even ran. This layer performs one bridge call,
        # salvages any streamed result, and reports the sidecar's final outcome.
        result: dict[str, Any] = {}

        def _salvage_stream() -> tuple[str, str]:
            text = "".join(streamed_text)
            thinking = "".join(streamed_thinking)
            # Visible text is the intended answer. If it never reached the text
            # channel, a complete fenced directive in reasoning is still safe
            # to parse; arbitrary chain-of-thought is not.
            if text.strip():
                return text, thinking
            directive = _directive_from_thinking(thinking)
            return (directive, thinking) if directive else ("", thinking)

        try:
            if sink is not None:
                result = bridge.call(
                    "complete", params, timeout=self.timeout, on_event=on_event)
            else:
                # No sink: omit both `stream` and on_event so non-GUI lanes
                # remain byte-identical.
                result = bridge.call("complete", params, timeout=self.timeout)
        except bridge.BridgeUnavailable as call_exc:
            salvaged_text, salvaged_thinking = _salvage_stream()
            if salvaged_text:
                # No terminal line means the stream may be partial. Preserve it
                # for parsing but mark it truncated so bare prose cannot
                # masquerade as a completed turn; a complete fenced directive
                # still parses and executes without another provider call.
                result = {
                    "text": salvaged_text,
                    "thinking": salvaged_thinking,
                    "stopReason": "length",
                }
            else:
                raise bridge.BridgeUnavailable(
                    f"omp-bridge {self.name}: stream ended before the final response "
                    f"line ({type(call_exc).__name__}: {call_exc}); no usable text "
                    "or fenced directive arrived. OMP provider retries are already "
                    "exhausted.",
                    status=getattr(call_exc, "status", None),
                ) from call_exc

        stop_reason = str(result.get("stopReason") or "")
        if stop_reason == "error":
            # Failed paid completions still count. Recording before raising keeps
            # delegation and pooled-credit accounting honest.
            self._record_usage(result.get("usage"))
            text = str(result.get("errorMessage") or "provider error")
            status = result.get("errorStatus")
            raise bridge.BridgeUnavailable(
                f"omp-bridge {self.name}: {text}",
                status=status if isinstance(status, int) and not isinstance(status, bool) else None)

        if not str(result.get("text") or ""):
            # Prefer a complete directive from either the authoritative final
            # thinking or the fragments. This is the zero-cost K3 recovery.
            final_thinking = str(result.get("thinking") or "")
            salvaged = _directive_from_thinking(final_thinking)
            if not salvaged:
                salvaged = _directive_from_thinking("".join(streamed_thinking))
            if salvaged:
                result["text"] = salvaged
            else:
                streamed_reply, streamed_reasoning = _salvage_stream()
                if streamed_reply:
                    # Do NOT send it to the sink again. The fragments were already
                    # displayed; this copy exists only for the parser/final result.
                    result["text"] = streamed_reply
                    if not final_thinking and streamed_reasoning:
                        result["thinking"] = streamed_reasoning
                elif (
                    self.wire_provider != "kimi-code"
                    and (final_thinking or streamed_reasoning)
                ):
                    # Reasoning with no text is accepted outside the K3 text dialect.
                    if not final_thinking:
                        result["thinking"] = streamed_reasoning

        reply = str(result.get("text") or "")
        thinking = str(result.get("thinking") or "")
        if not reply and not thinking:
            # The sidecar may have spent tokens while exhausting its own bounded
            # empty-completion retry. Account before surfacing the failure.
            self._record_usage(result.get("usage"))
            if stop_reason == "length":
                effort = (
                    f' at reasoningEffort="{self.reasoning_effort}"'
                    if self.reasoning_effort else ""
                )
                raise bridge.BridgeUnavailable(
                    f"omp-bridge {self.name}: {self.model} spent the whole max_tokens "
                    f"budget ({max_tokens}) before writing anything{effort} — the reply was "
                    "TRUNCATED, not missing (stopReason=length). Raise max_tokens "
                    "or lower the reasoning effort for this model.")
            raise bridge.BridgeUnavailable(
                f"omp-bridge {self.name}: final response contained no text or thinking; "
                "OMP's bounded empty-completion retries produced no usable streamed "
                "content. Retry the turn or lower reasoning effort.")
        self._record_usage(result.get("usage"))
        # A salvaged stream with no terminal line is marked "length" above so
        # agent_turn's existing compact re-emit guard handles incomplete prose.
        _base.set_last_stop_reason(result.get("stopReason"))
        return reply, thinking

    def _record_usage(self, usage: Any) -> None:
        """Stash the sidecar's token counts (pi-catalog Usage) into the turn accumulator.

        The sidecar's Usage shape is ``{input, output, cacheRead, cacheWrite,
        totalTokens, cost}``. ``input`` is the NON-CACHED input only — pi-ai
        normalized it that way deliberately so ``input + cacheRead + cacheWrite``
        is the total context size (see vendor/omp/packages/ai CHANGELOG 0.12.10,
        and pi-ai's own error/flags.ts which computes occupancy exactly that way).

        So ``input`` alone is the right number for COST and the wrong one for the
        context meter: once the prompt cache engages it collapses to the per-call
        delta. Live 2026-07-29 (case bench, k3): 8121 -> 719 -> 197 -> 1229 across
        one turn, so the desktop meter showed "1k/1.0M" for a context holding
        >10k. Report both, and let each consumer take the one it means.
        """
        if not isinstance(usage, dict):
            return
        try:
            from . import token_usage as _tu  # noqa: PLC0415

            # `_usage_int` returns None for an ABSENT key, so the occupancy sum has to
            # coerce: `int + None` raises, the bare `except` below would swallow it, and
            # the whole call's usage would silently go unrecorded for every provider
            # whose Usage omits the cache fields. prompt/completion/total keep None
            # (record_usage maps it to 0) so "not reported" stays distinguishable there.
            def _z(key: str) -> int:
                return _usage_int(usage, key) or 0

            inp = _usage_int(usage, "input")
            _tu.record_usage(
                provider=self.name,
                model=str(self.model or ""),
                prompt_tokens=inp,
                completion_tokens=_usage_int(usage, "output"),
                total_tokens=_usage_int(usage, "totalTokens"),
                context_tokens=(inp or 0) + _z("cacheRead") + _z("cacheWrite"),
                raw=usage,
                source="api",
            )
        except Exception:  # noqa: BLE001 — usage accounting never breaks a completion
            pass
