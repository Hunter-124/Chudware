"""Google Gemini adapter (generativelanguage REST: models/{model}:generateContent).

Dependency-free (urllib) so it works in the oracle without pulling google-generativeai.
Mirrors the C++ port in core/agent/providers.cpp byte-for-behavior: system prompt goes in
`system_instruction`, parts map to {text}/{inline_data}, reasoning effort maps to
`generationConfig.thinkingConfig.thinkingBudget`, and the reply is the concatenation of the
non-thought text parts of candidates[0].
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .base import Provider

DEFAULT_MODEL = "gemini-2.5-flash"
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _thinking_budget(effort: str | None) -> int | None:
    """Map an effort string to a thinkingBudget, or None to let the model decide."""
    e = (effort or "").strip().lower()
    return {"low": 1024, "medium": 8192, "med": 8192, "high": 24576,
            "max": 24576, "off": 0, "none": 0}.get(e)


class GeminiProvider(Provider):
    name = "gemini"
    supports_vision = True

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 reasoning_effort: str | None = None):
        # Empty/None api_key → fall back to the env, matching the C++ resolver.
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY") or ""
        self.model = model
        self.reasoning_effort = reasoning_effort

    def _part(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"text": part["text"]}
        data = base64.standard_b64encode(Path(part["path"]).read_bytes()).decode("utf-8")
        return {"inline_data": {"mime_type": "image/png", "data": data}}

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]

    def complete_ex(self, system: str, messages: list[dict],
                    max_tokens: int = 8000) -> tuple[str, str]:
        contents = []
        for m in messages:
            role = m.get("role", "user")
            role = "model" if role in ("assistant", "model") else "user"
            contents.append({"role": role, "parts": [self._part(p) for p in m["content"]]})
        gen: dict = {"maxOutputTokens": max_tokens}
        budget = _thinking_budget(self.reasoning_effort)
        if budget is not None:
            gen["thinkingConfig"] = {"thinkingBudget": budget}
        body: dict = {"contents": contents, "generationConfig": gen}
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}

        req = urllib.request.Request(
            _ENDPOINT.format(model=self.model),
            data=json.dumps(body).encode("utf-8"),
            headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # surface the body for diagnostics
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"gemini HTTP {e.code}: {detail}") from e

        cands = payload.get("candidates") or []
        if not cands:
            fb = payload.get("promptFeedback")
            raise RuntimeError(
                "gemini endpoint returned no completion"
                + (f": {json.dumps(fb)}" if fb else ""))
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        thinking = "".join(p["text"] for p in parts if "text" in p and p.get("thought"))
        return text, thinking
