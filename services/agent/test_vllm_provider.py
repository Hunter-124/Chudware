"""Tests for the vLLM adapter provider + config wiring.

Run: python -m pytest services/agent/test_vllm_provider.py -q

Following the repo convention (test_providers_conformance.py): SDK-backed construction is
`importorskip("openai")`'d, while offline-safety classification (which reads only
`.name`/`.base_url`) is exercised with a lightweight stub so it runs without the SDK.
`openai` is already declared in requirements.txt (line 27); it is simply absent from some
venvs, exactly like the openai/anthropic/openrouter adapters.
"""
from __future__ import annotations

import pytest

from services.agent import config as cfg


# --------------------------------------------------------------------------- offline classification
# No SDK needed: provider_offline_safe only inspects `.name`, `.base_url`, `.offline_safe`.

class _StubVLLM:
    name = "vllm"

    def __init__(self, base_url):
        self.base_url = base_url
        self.offline_safe = False


def test_loopback_vllm_is_offline_safe():
    assert cfg.provider_offline_safe(_StubVLLM("http://localhost:8000/v1")) is True
    assert cfg.provider_offline_safe(_StubVLLM("http://127.0.0.1:8000/v1")) is True


def test_remote_vllm_is_not_offline_safe():
    assert cfg.provider_offline_safe(_StubVLLM("http://gpu-box.internal:8000/v1")) is False


def test_vllm_default_endpoint_is_loopback_safe():
    # A vllm provider with no explicit base_url defaults to the loopback vLLM endpoint.
    assert cfg.provider_offline_safe(_StubVLLM(None)) is True


def test_offline_mode_refuses_remote_vllm():
    conf = {"provider": "vllm", "offline_mode": True,
            "vllm": {"base_url": "http://gpu-box.internal:8000/v1"}}
    with pytest.raises(cfg.OfflineModeError):
        cfg.get_provider(cfg=conf)


# --------------------------------------------------------------------------- SDK-backed construction

def test_construction_defaults():
    pytest.importorskip("openai")
    from services.agent.providers.vllm_provider import VLLMProvider
    p = VLLMProvider()
    assert p.name == "vllm"
    assert p.adapter is None
    assert p.base_model == "qwen3-4b"
    assert p.model == "qwen3-4b"          # base model when no adapter
    assert "8000" in (p.base_url or "")


def test_adapter_becomes_served_model_name():
    pytest.importorskip("openai")
    from services.agent.providers.vllm_provider import VLLMProvider
    # vLLM routes a LoRA by the request `model` field == the served adapter name.
    p = VLLMProvider(base_model="qwen3-4b", adapter="IP-13/v3")
    assert p.adapter == "IP-13/v3"
    assert p.model == "IP-13/v3"          # this is what gets sent to vLLM
    assert p.base_model == "qwen3-4b"


def test_telemetry_model_carries_adapter():
    pytest.importorskip("openai")
    from services.agent.providers.vllm_provider import VLLMProvider
    p = VLLMProvider(base_model="qwen3-4b", adapter="IP-13/v3")
    assert p.telemetry_model() == {
        "provider": "vllm", "model": "qwen3-4b", "adapter": "IP-13/v3"}


def test_get_provider_builds_vllm_with_adapter():
    pytest.importorskip("openai")
    from services.agent.providers.vllm_provider import VLLMProvider
    conf = {"provider": "vllm",
            "vllm": {"model": "qwen3-4b", "adapter": "IP-13/v3",
                     "base_url": "http://localhost:8000/v1"}}
    p = cfg.get_provider(cfg=conf)
    assert isinstance(p, VLLMProvider)
    assert p.adapter == "IP-13/v3"
    assert p.model == "IP-13/v3"


def test_offline_mode_allows_loopback_vllm():
    pytest.importorskip("openai")
    from services.agent.providers.vllm_provider import VLLMProvider
    conf = {"provider": "vllm", "offline_mode": True,
            "vllm": {"base_url": "http://localhost:8000/v1", "adapter": "IP-13/v3"}}
    p = cfg.get_provider(cfg=conf)        # no raise
    assert isinstance(p, VLLMProvider)
    assert cfg.provider_offline_safe(p) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
