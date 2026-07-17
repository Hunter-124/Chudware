"""Per-provider/model reasoning-effort wire mapping (openai-compatible family).

Mirrors the C++ openai_reasoning_patch test (core/tests/test_agent.cpp). The GUI sends one
effort level (off/low/medium/high); each provider/model accepts a DIFFERENT set, so the
resolver gates + remaps and endpoint identity wins over model-name prefixes.
"""
from services.agent.providers.openai_provider import wire_reasoning_effort as w


def test_off_and_empty_omit_everywhere():
    assert w("openai", "gpt-5.6", "https://api.openai.com/v1", "off") == {}
    assert w("openai", "gpt-5.6", "https://api.openai.com/v1", "") == {}
    assert w("xai", "grok-4.5", "https://api.x.ai/v1", "none") == {}


def test_openai_reasoning_models_pass_through_nonreasoning_omit():
    assert w("openai", "gpt-5.6", "https://api.openai.com/v1", "high") == {"reasoning_effort": "high"}
    assert w("openai", "o3", "https://api.openai.com/v1", "medium") == {"reasoning_effort": "medium"}
    assert w("openai", "gpt-4.1", "https://api.openai.com/v1", "high") == {}


def test_xai_grok_passthrough_original_grok4_omit():
    assert w("xai", "grok-4.5", "https://api.x.ai/v1", "low") == {"reasoning_effort": "low"}
    assert w("xai", "grok-4.5", "https://api.x.ai/v1", "medium") == {"reasoning_effort": "medium"}
    assert w("xai", "grok-4", "https://api.x.ai/v1", "high") == {}


def test_moonshot_kimi_k3_only_max_k2_omit():
    assert w("moonshot", "kimi-k3", "https://api.moonshot.ai/v1", "low") == {"reasoning_effort": "max"}
    assert w("moonshot", "kimi-k3", "https://api.moonshot.ai/v1", "high") == {"reasoning_effort": "max"}
    assert w("moonshot", "kimi-k2.6", "https://api.moonshot.ai/v1", "high") == {}


def test_deepseek_reasoner_high_only_chat_omit():
    assert w("openai", "deepseek-reasoner", "https://api.deepseek.com/v1", "low") == {
        "reasoning_effort": "high"
    }
    assert w("openai", "deepseek-chat", "https://api.deepseek.com/v1", "high") == {}


def test_qwen_dashscope_enable_thinking():
    assert w(
        "openai", "qwen3-max",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "high",
    ) == {"extra_body": {"enable_thinking": True}}


def test_openrouter_uses_reasoning_object_and_beats_model_prefix():
    # Vendor-prefixed slugs must route by ENDPOINT, not the deepseek/qwen prefix.
    assert w("openai", "qwen/qwen3-max", "https://openrouter.ai/api/v1", "medium") == {
        "extra_body": {"reasoning": {"effort": "medium"}}
    }
    assert w("openai", "deepseek/deepseek-chat", "https://openrouter.ai/api/v1", "high") == {
        "extra_body": {"reasoning": {"effort": "high"}}
    }


def test_local_endpoint_omits_even_reasoning_model():
    assert w("openai", "gpt-5", "http://localhost:1234/v1", "high") == {}
    assert w("openai", "gpt-oss-120b", "http://127.0.0.1:1234/v1", "high") == {}


def test_model_name_fallback_when_base_url_unknown():
    # Bare provider name / empty base_url: fall back to model-name heuristics.
    assert w("moonshot", "kimi-k3", "", "high") == {"reasoning_effort": "max"}
    assert w("xai", "grok-4.5", "", "low") == {"reasoning_effort": "low"}
    assert w("openai", "gpt-5.6", "", "high") == {"reasoning_effort": "high"}
