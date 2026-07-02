"""Provider conformance to contracts/providers.md (neutral schema + offline-safety).

No network: SDK-backed adapters (openai/anthropic/openrouter) are importorskip'd; the
dependency-free gemini adapter and the neutral helpers/classification always run.
Lives under agent/ (gate runs `pytest engine/doc agent`).
"""
from __future__ import annotations

import types

import pytest

from agent import config
from agent.providers import base


def _png(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)  # header + filler; adapters just b64 it
    return str(p)


# ------------------------------------------------------------------ neutral schema (§1)
def test_neutral_part_and_message_helpers():
    assert base.text_part("hi") == {"type": "text", "text": "hi"}
    assert base.image_part("/x/y.png") == {"type": "image", "path": "/x/y.png"}
    msg = base.user([base.text_part("q")])
    assert msg == {"role": "user", "content": [{"type": "text", "text": "q"}]}
    assert base.assistant([])["role"] == "assistant"


def test_base_complete_ex_defaults_to_empty_thinking():
    class P(base.Provider):
        def complete(self, system, messages, max_tokens=8000):
            return "answer"

    text, thinking = P().complete_ex("sys", [])
    assert (text, thinking) == ("answer", "")


# ------------------------------------------------------------------ offline-safety (§5)
def _stub(name, **kw):
    return types.SimpleNamespace(name=name, **kw)


def test_offline_safe_classification():
    assert config.provider_offline_safe(_stub("mock")) is True
    assert config.provider_offline_safe(_stub("anthropic")) is False
    assert config.provider_offline_safe(_stub("gemini")) is False
    # openrouter is cloud -> never offline-safe (contract §5)
    assert config.provider_offline_safe(_stub("openrouter")) is False
    assert config.provider_offline_safe(_stub("openai", base_url="http://127.0.0.1:1234/v1")) is True
    assert config.provider_offline_safe(_stub("openai", base_url=None)) is False
    assert config.provider_offline_safe(_stub("llamacpp", base_url="http://localhost:1234/v1")) is True


def test_assert_offline_safe_refuses_openrouter_when_offline():
    with pytest.raises(config.OfflineModeError):
        config.assert_offline_safe(_stub("openrouter"), {"offline_mode": True})
    # no-op when offline mode is off
    config.assert_offline_safe(_stub("openrouter"), {"offline_mode": False})


# ------------------------------------------------------------------ gemini wire shape (§1)
def test_gemini_parts_text_and_image(tmp_path):
    from agent.providers.gemini_provider import GeminiProvider

    g = GeminiProvider(api_key="k")
    assert g.name == "gemini"
    assert g._part(base.text_part("hello")) == {"text": "hello"}
    img = g._part(base.image_part(_png(tmp_path)))
    assert img["inline_data"]["mime_type"] == "image/png"
    assert isinstance(img["inline_data"]["data"], str) and img["inline_data"]["data"]


# ------------------------------------------------------------------ openai-family (§1, needs SDK)
def test_openai_wire_shape(tmp_path):
    pytest.importorskip("openai")
    from agent.providers.openai_provider import OpenAIProvider

    o = OpenAIProvider(api_key="k", base_url="http://127.0.0.1:1234/v1")
    assert o._content(base.text_part("hi")) == {"type": "text", "text": "hi"}
    img = o._content(base.image_part(_png(tmp_path)))
    assert img["type"] == "image_url" and img["image_url"]["url"].startswith("data:image/png;base64,")
    # text-only message collapses to a plain string (broad compatibility)
    m = o._message("user", [base.text_part("a"), base.text_part("b")])
    assert m == {"role": "user", "content": "a\n\nb"}


def test_openrouter_pins_openrouter_base_url():
    pytest.importorskip("openai")
    from agent.providers.openrouter_provider import OpenRouterProvider, BASE_URL

    r = OpenRouterProvider(api_key="sk-or-x")
    assert r.name == "openrouter"
    assert r.base_url == BASE_URL
    # inherits the OpenAI-compatible wire form
    assert r._content(base.text_part("hi")) == {"type": "text", "text": "hi"}
