"""Provider-neutral message types + base class.

Messages are dicts: {"role": "user"|"assistant", "content": [parts]}.
A part is either {"type": "text", "text": str} or {"type": "image", "path": str}.
Each adapter converts these to its own API's wire format (Anthropic image blocks,
OpenAI image_url data URIs, etc.), so the loop stays provider-agnostic.
"""
from __future__ import annotations


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
