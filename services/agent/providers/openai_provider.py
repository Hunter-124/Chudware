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


def wire_reasoning_effort(name: str, model: str, base_url: str | None,
                          level: str | None) -> dict:
    """Per-provider/model reasoning-effort -> OpenAI-compatible ``create()`` kwargs.

    The GUI sends ONE level (off/low/medium/high); each provider/model accepts a DIFFERENT
    set and an unsupported value 400s, so we gate + remap (verified vs live docs 2026-07):
      OpenAI gpt-5*/o1/o3/o4       reasoning_effort: low|medium|high   (gpt-4*: omit)
      xAI grok-4.5/4.3/-fast/3-mini reasoning_effort: low|medium|high  (original grok-4: omit)
      Moonshot kimi-k3            reasoning_effort: "max" only         (kimi-k2.x: omit)
      DeepSeek reasoner/v4        reasoning_effort: high|max -> "high" (deepseek-chat: omit)
      Alibaba Qwen (DashScope)    extra_body={"enable_thinking": True}  (bool; no level)
      OpenRouter                  extra_body={"reasoning":{"effort":...}}
      local / unknown             omit
    Returns kwargs to splat into create(): {} | {"reasoning_effort": v} | {"extra_body": {...}}.
    """
    lvl = (level or "").strip().lower()
    if lvl in ("", "off", "none"):
        return {}
    m = (model or "").lower()
    b = (base_url or "").lower()
    p = (name or "").lower()

    def lmh() -> str:
        if lvl in ("low", "medium", "high"):
            return lvl
        if lvl == "minimal":
            return "low"
        return "high"  # xhigh / max / any other on-value

    if any(h in b for h in ("localhost", "127.0.0.1", "::1", "0.0.0.0")):
        return {}
    # Endpoint-identified services win over model-name heuristics so a vendor-prefixed
    # OpenRouter slug (deepseek/*, qwen/*, moonshotai/*) routes by its ENDPOINT, not the prefix.
    if "openrouter" in b:
        return {"extra_body": {"reasoning": {"effort": lmh()}}}
    if p == "moonshot" or "moonshot.ai" in b:
        return {"reasoning_effort": "max"} if "kimi-k3" in m else {}
    if p in ("xai", "xai-sub") or "x.ai" in b:
        return {} if m in ("grok-4", "grok-4-0709") else {"reasoning_effort": lmh()}
    if "deepseek.com" in b:
        if "reasoner" in m or "v4-pro" in m or "v4-flash" in m or "deepseek-v4" in m:
            return {"reasoning_effort": "high"}
        return {}
    if "aliyuncs" in b or "dashscope" in b:  # Qwen (DashScope): hybrid thinking bool, no level
        return {"extra_body": {"enable_thinking": True}}
    if "openai.com" in b:
        return {"reasoning_effort": lmh()} if m.startswith(("gpt-5", "o1", "o3", "o4")) else {}
    # No endpoint match: fall back to model-name heuristics (bare provider name / empty base).
    if m.startswith("kimi"):
        return {"reasoning_effort": "max"} if "kimi-k3" in m else {}
    if m.startswith("grok"):
        return {} if m in ("grok-4", "grok-4-0709") else {"reasoning_effort": lmh()}
    if m.startswith("deepseek"):
        if "reasoner" in m or "v4-pro" in m or "v4-flash" in m:
            return {"reasoning_effort": "high"}
        return {}
    if m.startswith("qwen"):
        return {"extra_body": {"enable_thinking": True}}
    if p == "openai" and not b and m.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"reasoning_effort": lmh()}
    return {}


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 base_url: str | None = None, supports_vision: bool = True,
                 timeout: float = 120.0, reasoning_effort: str | None = None):
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
        self.reasoning_effort = reasoning_effort

    def _content(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"type": "text", "text": part["text"]}
        data = base64.standard_b64encode(Path(part["path"]).read_bytes()).decode("utf-8")
        mime = part.get("mime", "image/png")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}

    def _message(self, role: str, parts) -> dict:
        # Accept plain-string content (mega-8 leaf oneshot / overseer audit) as
        # well as the agent_turn list-of-parts form. XAISubProvider already did
        # this; OpenAI/ollama/lmstudio crashed with
        # ``string indices must be integers`` on cheap-leaf one-shots.
        if isinstance(parts, str):
            return {"role": role, "content": parts}
        # Use plain-string content for text-only messages — the most broadly
        # compatible form. Some OpenAI-compatible servers (LM Studio, llama.cpp,
        # etc.) reject the array-of-parts form for text and return empty choices.
        if all(p["type"] == "text" for p in parts):
            return {"role": role, "content": "\n\n".join(p["text"] for p in parts)}
        return {"role": role, "content": [self._content(p) for p in parts]}

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        from openai import APIConnectionError, APITimeoutError

        msgs = [{"role": "system", "content": system}]
        for m in messages:
            msgs.append(self._message(m["role"], m["content"]))
        try:
            extra = wire_reasoning_effort(self.name, self.model, self.base_url,
                                          getattr(self, "reasoning_effort", None))
            resp = self.client.chat.completions.create(
                model=self.model, messages=msgs, max_tokens=max_tokens, **extra
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
        # mega-6: stash provider usage without changing complete return signature.
        try:
            from . import token_usage as _tu  # noqa: PLC0415
            u = _tu.extract_openai_usage(resp)
            _tu.record_usage(
                provider=self.name,
                model=str(self.model or ""),
                prompt_tokens=u.get("prompt_tokens"),
                completion_tokens=u.get("completion_tokens"),
                total_tokens=u.get("total_tokens"),
                source="api",
            )
        except Exception:  # noqa: BLE001 — usage never blocks completion
            pass
        return content
