"""Provider-neutral message types + base class.

Messages are dicts: {"role": "user"|"assistant", "content": [parts]}.
A part is either {"type": "text", "text": str} or {"type": "image", "path": str}.
Each adapter converts these to its own API's wire format (Anthropic image blocks,
OpenAI image_url data URIs, etc.), so the loop stays provider-agnostic.

Provider delta sink seam (mirrors core/agent/run.cpp's set_provider_delta_emitter
/ ProviderDeltaGuard): a thread-local sink a streaming-capable provider feeds raw
text/thinking fragments into while a completion streams. Display-only — the
provider's final result still carries the full text. Unset means no streaming,
zero overhead.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

# Sink protocol: callable(delta: str, channel: str) -> None (delta first, like the
# C++ emitter). MAY expose discard() -> None; providers call it before a resample
# so buffered fragments from the discarded attempt are dropped.
DeltaSink = Callable[[str, str], None]

_tls = threading.local()


class ProviderUnreachable(RuntimeError):
    """The provider resolved but could not be reached (connection refused, timeout).

    Distinct from a bad request, bad credentials, or a model error: nothing about the
    CALL was wrong, the server simply is not answering — overwhelmingly a local
    LM Studio/Ollama endpoint that is configured but not running.

    Subclasses RuntimeError so every existing ``except RuntimeError`` handler keeps
    working unchanged. The point is to let a caller that still has a useful non-LLM
    answer (regen diagnosis detects failures without a model) degrade to it instead of
    propagating a crash into an agent turn.
    """


def set_provider_delta_sink(sink: Any) -> Any:
    """Install (or clear, with None) this thread's provider delta sink.

    Returns the previously installed sink so callers can restore it (nested
    turns on one dispatch thread must not orphan the parent's stream).
    """
    prev = getattr(_tls, "delta_sink", None)
    _tls.delta_sink = sink
    return prev


def provider_delta_sink() -> Any:
    """This thread's installed provider delta sink, or None."""
    return getattr(_tls, "delta_sink", None)


class ProviderDeltaGuard:
    """Install-and-restore guard mirroring C++'s ProviderDeltaGuard.

    On __enter__ installs `sink`; on __exit__ calls ``sink.flush_all()`` when
    available, then restores the previous sink. Context-manager usage only.
    """

    def __init__(self, sink: Any) -> None:
        self._sink = sink
        self._prev: Any = None

    def __enter__(self) -> Any:
        self._prev = set_provider_delta_sink(self._sink)
        return self._sink

    def __exit__(self, *exc: Any) -> bool:
        flush = getattr(self._sink, "flush_all", None)
        if callable(flush):
            try:
                flush()
            except Exception:  # noqa: BLE001 — flushing never breaks the turn
                pass
        set_provider_delta_sink(self._prev)
        return False


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(path: str, mime: str = "image/png") -> dict:
    # `mime` lets ingested previews (e.g. downscaled JPEG photos, services.vision) ride the same slot
    # without being mislabeled png; adapters honor it and default to png for back-compat.
    return {"type": "image", "path": str(path), "mime": mime}


def user(parts: list) -> dict:
    return {"role": "user", "content": parts}


def assistant(parts: list) -> dict:
    return {"role": "assistant", "content": parts}


class Provider:
    """A vision+text chat completion backend."""

    name = "base"
    supports_vision = True  # if False, the loop sends stats-only feedback (no screenshot)

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        """Return the assistant's text reply for (system, messages)."""
        raise NotImplementedError

    def complete_ex(self, system: str, messages: list[dict],
                    max_tokens: int = 8000) -> tuple[str, str]:
        """Return (reply_text, thinking_text).

        ``thinking_text`` is the model's extended-reasoning trace when the backend exposes
        one (e.g. Anthropic adaptive thinking), used by the agent loop to surface the
        model's rationale in the UI. Defaults to no separate thinking channel."""
        return self.complete(system, messages, max_tokens=max_tokens), ""
