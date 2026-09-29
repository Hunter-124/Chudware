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


def test_moonshot_kimi_k3_honours_caller_effort():
    # kimi-k3's real wire ladder is low|high|max. There is no medium rung, so a medium
    # inherited from a shared low|medium|high role config maps to high (the nearest real
    # rung above low) rather than being dropped. Nothing is clamped away.
    k3 = "https://api.moonshot.ai/v1"
    assert w("moonshot", "kimi-k3", k3, "low") == {"reasoning_effort": "low"}
    assert w("moonshot", "kimi-k3", k3, "minimal") == {"reasoning_effort": "low"}
    assert w("moonshot", "kimi-k3", k3, "medium") == {"reasoning_effort": "high"}
    assert w("moonshot", "kimi-k3", k3, "med") == {"reasoning_effort": "high"}
    assert w("moonshot", "kimi-k3", k3, "high") == {"reasoning_effort": "high"}
    assert w("moonshot", "kimi-k3", k3, "max") == {"reasoning_effort": "max"}
    assert w("moonshot", "kimi-k3", k3, "off") == {}


def test_moonshot_kimi_k3_top_rung_aliases_reach_max():
    # Every "give me the most" spelling lands on this model's genuine top rung.
    k3 = "https://api.moonshot.ai/v1"
    for raw in ("max", "MAX", " Max ", "maximum", "xhigh", "x-high", "extra_high",
                "ultra", "highest"):
        assert w("moonshot", "kimi-k3", k3, raw) == {"reasoning_effort": "max"}


def test_moonshot_kimi_k2x_thinks_by_default_no_effort():
    # K2.6/K2.7 reason natively and expose no dial — sending reasoning_effort is wrong.
    k3 = "https://api.moonshot.ai/v1"
    for mid in ("kimi-k2.6", "kimi-k2.7", "kimi-k2.7-code", "kimi-for-coding"):
        assert w("moonshot", mid, k3, "high") == {}
        assert w("moonshot", mid, k3, "low") == {}


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
    assert w("moonshot", "kimi-k3", "", "high") == {"reasoning_effort": "high"}
    assert w("moonshot", "kimi-k3", "", "medium") == {"reasoning_effort": "high"}
    assert w("moonshot", "kimi-k3", "", "max") == {"reasoning_effort": "max"}
    assert w("moonshot", "kimi-k2.7", "", "high") == {}
    assert w("xai", "grok-4.5", "", "low") == {"reasoning_effort": "low"}
    assert w("openai", "gpt-5.6", "", "high") == {"reasoning_effort": "high"}


def test_google_level_models_do_not_receive_legacy_budget(monkeypatch):
    import json
    from services.agent.providers import gemini_provider

    bodies = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'

    def fake_urlopen(request, timeout):
        bodies.append(json.loads(request.data))
        return Reply()

    monkeypatch.setattr(gemini_provider.urllib.request, "urlopen", fake_urlopen)
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert gemini_provider.GeminiProvider(api_key="k", model="gemini-3.5-flash",
                                           reasoning_effort="medium").complete("sys", messages) == "ok"
    assert bodies[-1]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
    assert gemini_provider.GeminiProvider(api_key="k", model="gemini-2.5-flash",
                                           reasoning_effort="medium").complete("sys", messages) == "ok"
    assert bodies[-1]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 8192}
    assert gemini_provider.GeminiProvider(api_key="k", model="gemini-2.5-pro",
                                           reasoning_effort="off").complete("sys", messages) == "ok"
    assert bodies[-1]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 128}
