"""OpenAI / OpenAI-compatible adapter (vision via image_url data URIs).

Works with OpenAI and any OpenAI-compatible endpoint (local models, etc.) by passing
`base_url`. Kept minimal; Anthropic is the default.
"""
from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlparse

from .base import Provider

DEFAULT_MODEL = "gpt-4o"


def _normalize_base_url(base_url: str | None) -> str | None:
    """OpenAI-compatible servers expect a versioned API root (…/v1). If the user gives just
    a host:port with no path (a very common mistake with LM Studio / llama.cpp / Ollama,
    which makes requests hit POST /chat/completions instead of /v1/chat/completions),
    default the path to /v1."""
    if not base_url:
        return base_url
    u = base_url.rstrip("/")
    parsed = urlparse(u)
    if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
        u += "/v1"
    return u


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 base_url: str | None = None, supports_vision: bool = True,
                 timeout: float = 120.0):
        from openai import OpenAI

        # Local OpenAI-compatible servers (LM Studio, etc.) often need any non-empty key.
        self.base_url = _normalize_base_url(base_url)
        # Bound every request: the OpenAI SDK default is 600 s, so a local server that is
        # slow/loading a model would otherwise hang the caller (e.g. ecad_design) for ten
        # minutes. A finite timeout makes a stuck endpoint fail fast so callers can degrade.
        self.timeout = float(timeout) if timeout and timeout > 0 else 120.0
        self.client = OpenAI(api_key=api_key or "not-needed", base_url=self.base_url,
                             timeout=self.timeout)
        self.model = model
        self.supports_vision = supports_vision

    def _content(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"type": "text", "text": part["text"]}
        data = base64.standard_b64encode(Path(part["path"]).read_bytes()).decode("utf-8")
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}

    def _message(self, role: str, parts: list[dict]) -> dict:
        # Use plain-string content for text-only messages — the most broadly compatible
        # form. Some OpenAI-compatible servers (LM Studio, llama.cpp, etc.) reject the
        # array-of-parts form for text and return an empty/None `choices`.
        if all(p["type"] == "text" for p in parts):
            return {"role": role, "content": "\n\n".join(p["text"] for p in parts)}
        return {"role": role, "content": [self._content(p) for p in parts]}

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        from openai import APIConnectionError, APITimeoutError

        msgs = [{"role": "system", "content": system}]
        for m in messages:
            msgs.append(self._message(m["role"], m["content"]))
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=msgs, max_tokens=max_tokens
            )
        except APITimeoutError as e:
            url = self.base_url or "https://api.openai.com/v1"
            raise RuntimeError(
                f"The model server at {url} did not respond within {self.timeout:.0f}s "
                f"(model={self.model!r} may still be loading, or the request is too large). "
                f"Try again once it is warm, lower the load, or switch provider. ({e})"
            ) from e
        except APIConnectionError as e:
            # The single most common local-model failure: the server isn't there. Name the
            # endpoint so the user can tell "server down" apart from auth/model problems.
            url = self.base_url or "https://api.openai.com/v1"
            raise RuntimeError(
                f"Could not reach the model server at {url} — connection refused/failed. "
                f"If you use LM Studio (or another local server), make sure it is running "
                f"and its local server is started, then try again. ({e})"
            ) from e
        # OpenAI-compatible endpoints sometimes return HTTP 200 with no choices (model not
        # loaded, an error payload, or a context-length overflow). Surface that clearly
        # instead of crashing with "'NoneType' object is not subscriptable".
        choices = getattr(resp, "choices", None)
        if not choices:
            detail = getattr(resp, "error", None)
            if detail is None:
                detail = getattr(resp, "model_extra", None) or repr(resp)[:400]
            raise RuntimeError(
                f"The model endpoint returned no completion (model={self.model!r}). "
                f"Check that the model is loaded and the Base URL is correct in Settings, "
                f"or switch provider to Anthropic. Endpoint detail: {detail}"
            )
        msg = choices[0].message
        # Reasoning models served over the OpenAI-compatible API (LM Studio, llama.cpp,
        # vLLM, DeepSeek-R1, Qwen-think, …) put their chain-of-thought in `reasoning_content`
        # (some use `reasoning`). When the model returned ONLY reasoning with an empty
        # `content` (spent its output budget thinking, or never split the two), fall back to
        # it so the caller can still pull any ops JSON the model wrote inline.
        content = getattr(msg, "content", None) or ""
        if not content:
            extra = getattr(msg, "model_extra", None) or {}
            content = (getattr(msg, "reasoning_content", None)
                       or getattr(msg, "reasoning", None)
                       or extra.get("reasoning_content")
                       or extra.get("reasoning")
                       or "")
        return content
