"""Anthropic adapter — default provider (vision + codegen).

Uses the official `anthropic` SDK with Claude Opus 4.8: adaptive thinking + high
effort for the reasoning loop. (On Opus 4.8 `temperature`/`top_p`/`budget_tokens` are
removed and would 400, so they're intentionally absent.)
"""
from __future__ import annotations

import base64
from pathlib import Path

from .base import Provider

DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        import anthropic

        # Empty/None api_key → SDK resolves ANTHROPIC_API_KEY from the environment.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model

    def _block(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"type": "text", "text": part["text"]}
        data = base64.standard_b64encode(Path(part["path"]).read_bytes()).decode("utf-8")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": part.get("mime", "image/png"), "data": data},
        }

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]

    def complete_ex(self, system: str, messages: list[dict],
                    max_tokens: int = 8000) -> tuple[str, str]:
        api_messages = [
            {"role": m["role"], "content": [self._block(p) for p in m["content"]]}
            for m in messages
        ]
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=api_messages,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
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
