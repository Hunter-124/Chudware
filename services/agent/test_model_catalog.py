"""Tests for services.agent.model_catalog — discovery, models.dev merge, overrides.

All network is mocked. models.dev is never fetched at import.
"""
from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.agent import model_catalog as mc


class _FakeResp:
    def __init__(self, body: bytes | str, status: int = 200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener_for(payloads: dict[str, object]):
    """urlopen mock: map URL substring -> JSON-serializable payload or Exception."""

    def open_fn(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for key, val in payloads.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return _FakeResp(json.dumps(val))
        raise RuntimeError(f"unexpected URL in test opener: {url}")

    return open_fn


SAMPLE_MODELS_DEV = {
    "xai": {
        "id": "xai",
        "models": {
            "grok-4.5": {
                "id": "grok-4.5",
                "name": "Grok 4.5",
                "attachment": True,
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "medium", "high"]}
                ],
                "tool_call": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 500000, "output": 64000},
            },
            "grok-build-0.1": {
                "id": "grok-build-0.1",
                "name": "Grok Build",
                "attachment": False,
                "reasoning": False,
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 128000, "output": 8000},
            },
        },
    },
    "google": {
        "id": "google",
        "models": {
            "gemini-2.5-pro": {
                "id": "gemini-2.5-pro",
                "name": "Gemini 2.5 Pro",
                "attachment": True,
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "medium", "high"]}
                ],
                "tool_call": True,
                "modalities": {
                    "input": ["text", "image"],
                    "output": ["text"],
                },
                "limit": {"context": 1048576, "output": 65536},
            }
        },
    },
    "anthropic": {
        "id": "anthropic",
        "models": {
            "claude-opus-4-5": {
                "id": "claude-opus-4-5",
                "name": "Claude Opus 4.5",
                "attachment": True,
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "medium", "high"]}
                ],
                "tool_call": True,
                "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                "limit": {"context": 200000, "output": 64000},
            }
        },
    },
    "openai": {
        "id": "openai",
        "models": {
            "gpt-4o": {
                "id": "gpt-4o",
                "name": "GPT-4o",
                "attachment": True,
                "reasoning": False,
                "tool_call": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 128000, "output": 16384},
            },
            "o3": {
                "id": "o3",
                "name": "o3",
                "attachment": True,
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "medium", "high"]}
                ],
                "tool_call": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 200000, "output": 100000},
            },
        },
    },
}


class TestNoImportFetch(unittest.TestCase):
    def test_import_does_not_touch_network(self):
        # The module is already imported; re-executing ensure is the risk surface.
        # load_models_dev with allow_fetch=False and missing cache returns {}.
        with patch.object(mc, "models_dev_cache_path", return_value=Path("/no/such/cache.json")):
            self.assertEqual(mc.load_models_dev(allow_fetch=False), {})


class TestModelsDevCache(unittest.TestCase):
    def test_ensure_writes_cache(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "models_dev_cache.json"
            opener = _opener_for({"models.dev": SAMPLE_MODELS_DEV})
            with patch.object(mc, "models_dev_cache_path", return_value=cache):
                path = mc.ensure_models_dev_cache(force=True, opener=opener)
                self.assertTrue(path.exists())
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("xai", data)
                # Second call with fresh cache does not re-fetch.
                dead = _opener_for({"models.dev": RuntimeError("should not fetch")})
                path2 = mc.ensure_models_dev_cache(force=False, opener=dead)
                self.assertEqual(path2, path)


class TestLiveDiscovery(unittest.TestCase):
    def test_openai_compatible_ids(self):
        payload = {
            "data": [
                {"id": "gpt-4o"},
                {"id": "o3", "name": "OpenAI o3"},
            ]
        }
        opener = _opener_for({"/models": payload})
        rows = mc.discover_openai_compatible(
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            opener=opener,
        )
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["gpt-4o", "o3"])
        self.assertEqual(rows[1]["label"], "OpenAI o3")

    def test_openrouter_context_and_vision(self):
        payload = {
            "data": [
                {
                    "id": "qwen/qwen3-vl-32b-instruct",
                    "context_length": 131072,
                    "architecture": {
                        "modality": "text+image->text",
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    },
                    "top_provider": {"max_completion_tokens": 8192},
                }
            ]
        }
        opener = _opener_for({"/models": payload})
        rows = mc.discover_openai_compatible(
            base_url="https://openrouter.ai/api/v1",
            api_key="or-test",
            provider="openrouter",
            opener=opener,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context"], 131072)
        self.assertTrue(rows[0]["vision"])
        self.assertEqual(rows[0]["max_output"], 8192)

    def test_gemini_keeps_token_limits(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-2.5-pro",
                    "displayName": "Gemini 2.5 Pro",
                    "supportedGenerationMethods": ["generateContent"],
                    "inputTokenLimit": 1048576,
                    "outputTokenLimit": 65536,
                },
                {
                    "name": "models/embedding-001",
                    "supportedGenerationMethods": ["embedContent"],
                    "inputTokenLimit": 2048,
                },
            ]
        }
        opener = _opener_for({"generativelanguage": payload})
        rows = mc.discover_gemini(api_key="g-test", opener=opener)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "gemini-2.5-pro")
        self.assertEqual(rows[0]["context"], 1048576)
        self.assertEqual(rows[0]["max_output"], 65536)
        self.assertTrue(rows[0]["vision"])
        self.assertIn("high", rows[0]["reasoning_efforts"])

    def test_xai_via_discover_models(self):
        payload = {"data": [{"id": "grok-4.5"}, {"id": "grok-4-1"}]}
        opener = _opener_for({"api.x.ai": payload})
        rows = mc.discover_models("xai", api_key="xai-key", opener=opener)
        self.assertEqual([r["id"] for r in rows], ["grok-4.5", "grok-4-1"])

    def test_anthropic_static_seed(self):
        rows = mc.discover_models("anthropic")
        self.assertTrue(any(r["id"] == "claude-opus-4-8" for r in rows))


class TestMergeCatalog(unittest.TestCase):
    def test_live_plus_models_dev(self):
        live = [
            mc.ModelRow.make(id="grok-4.5", auto_source="live"),  # id only
        ]
        merged = mc.merge_catalog("xai", live, models_dev=SAMPLE_MODELS_DEV)
        by_id = {r["id"]: r for r in merged}
        self.assertIn("grok-4.5", by_id)
        row = by_id["grok-4.5"]
        # models.dev fills context for bare live id
        self.assertEqual(row["context"], 500000)
        self.assertTrue(row["vision"])
        self.assertIn("medium", row["reasoning_efforts"])
        self.assertIn("live", row["auto_source"])
        # models.dev-only id kept for cloud
        self.assertIn("grok-build-0.1", by_id)
        self.assertEqual(by_id["grok-build-0.1"]["auto_source"], "models.dev")

    def test_live_limits_win_over_models_dev(self):
        live = [
            mc.ModelRow.make(
                id="gemini-2.5-pro",
                context=999999,
                max_output=1111,
                vision=True,
                tools=True,
                reasoning_efforts=["off", "low", "medium", "high", "max"],
                auto_source="live",
            )
        ]
        merged = mc.merge_catalog("gemini", live, models_dev=SAMPLE_MODELS_DEV)
        row = next(r for r in merged if r["id"] == "gemini-2.5-pro")
        self.assertEqual(row["context"], 999999)
        self.assertEqual(row["max_output"], 1111)

    def test_local_never_pulls_models_dev_ids(self):
        live = [mc.ModelRow.make(id="my-local-gguf", auto_source="live")]
        merged = mc.merge_catalog("llamacpp", live, models_dev=SAMPLE_MODELS_DEV)
        ids = [r["id"] for r in merged]
        self.assertEqual(ids, ["my-local-gguf"])
        # No openai/xai models leaked in.
        self.assertNotIn("gpt-4o", ids)


class TestOverridePrecedence(unittest.TestCase):
    def test_user_context_and_vision_win(self):
        cfg = {
            "context_window": 4242,
            "max_output_tokens": 777,
            "openai": {"vision": False},
        }
        caps = mc.resolve_capabilities(
            "openai",
            "gpt-4o",
            cfg,
            models_dev=SAMPLE_MODELS_DEV,
        )
        self.assertEqual(caps["context"], 4242)
        self.assertEqual(caps["max_output"], 777)
        self.assertFalse(caps["vision"])
        self.assertEqual(caps["auto_source"], "user")

    def test_zero_context_uses_catalog(self):
        cfg = {"context_window": 0}
        caps = mc.resolve_capabilities(
            "xai",
            "grok-4.5",
            cfg,
            models_dev=SAMPLE_MODELS_DEV,
        )
        self.assertEqual(caps["context"], 500000)
        self.assertTrue(caps["vision"])
        self.assertNotEqual(caps["auto_source"], "user")

    def test_user_reasoning_efforts_list(self):
        cfg = {"reasoning_efforts": ["low", "high"]}
        caps = mc.resolve_capabilities(
            "anthropic",
            "claude-opus-4-5",
            cfg,
            models_dev=SAMPLE_MODELS_DEV,
        )
        self.assertEqual(caps["reasoning_efforts"], ["low", "high"])
        self.assertEqual(caps["auto_source"], "user")


class TestHeuristics(unittest.TestCase):
    def test_grok_45_context(self):
        row = mc.heuristic_row("xai", "grok-4.5")
        self.assertEqual(row["context"], 500000)
        self.assertTrue(row["vision"])

    def test_gemini_efforts(self):
        # Gemini exposes the full dial including "max" — the harness advertises exactly
        # what the provider accepts and does not clamp the ladder.
        efforts = mc.reasoning_efforts_for("gemini", "gemini-2.5-flash")
        self.assertEqual(efforts, ["off", "low", "medium", "high", "max"])
        self.assertEqual(mc.reasoning_efforts_for("gemini", "gemini-2.0-flash"), [])


class TestReasoningEffortLadders(unittest.TestCase):
    """Family effort ladders — Kimi coverage plus a regression table for everything else."""

    # (provider, model_id) -> ladder. Frozen baseline: these must not drift.
    BASELINE = {
        ("anthropic", "claude-opus-4-5"): ["low", "medium", "high"],
        ("anthropic", "claude-sonnet-5"): ["low", "medium", "high"],
        ("openai", "gpt-5"): ["low", "medium", "high"],
        ("openai", "gpt-5.2"): ["low", "medium", "high"],
        ("openai", "o3"): ["low", "medium", "high"],
        ("gemini", "gemini-2.5-pro"): ["off", "low", "medium", "high", "max"],
        ("xai", "grok-4.5"): ["off", "low", "medium", "high"],
        ("xai", "grok-4"): ["off", "low", "medium", "high"],
        ("xai", "grok-code-fast-1"): ["off", "low", "medium", "high"],
        ("moonshot", "kimi-k2-thinking"): ["off", "low", "medium", "high"],
        ("cursor", "gpt-5"): ["low", "medium", "high"],
        ("cursor", "claude-sonnet-5"): ["low", "medium", "high"],
        ("zenmux", "anthropic/claude-opus-4.5"): ["low", "medium", "high"],
        ("openrouter", "anthropic/claude-opus-4.5"): ["low", "medium", "high"],
        ("cloud", "deepseek-reasoner"): ["off", "low", "medium", "high"],
        ("cloud", "qwen3-max"): ["off", "low", "medium", "high"],
        ("openrouter", "deepseek/deepseek-r1"): ["off", "low", "medium", "high"],
        ("local", "qwen3-32b"): ["off", "low", "medium", "high"],
        ("local", "deepseek-r1-distill"): ["off", "low", "medium", "high"],
        ("llamacpp", "qwen3-30b-a3b"): ["off", "low", "medium", "high"],
        # Correct to have NO dial — including the families deliberately left untouched
        # (router-prefixed gpt-5, gpt-oss, GLM): they stay dial-less by decision.
        ("openai", "gpt-4.1"): [],
        ("cloud", "deepseek-chat"): [],
        ("local", "llama-3.3-70b"): [],
        ("cursor", "composer-1"): [],
        ("zenmux", "openai/gpt-5"): [],
        ("cloud", "openai/gpt-5"): [],
        ("openrouter", "z-ai/glm-4.6"): [],
        ("llamacpp", "gpt-oss-20b"): [],
    }

    def test_baseline_ladders_unchanged(self):
        for (provider, model), expected in self.BASELINE.items():
            with self.subTest(provider=provider, model=model):
                self.assertEqual(mc.reasoning_efforts_for(provider, model), expected)

    def test_kimi_k3_low_high_max(self):
        # K3's wire ladder is exactly low|high|max — no medium rung, and "max" is real,
        # so the GUI offers all three rather than a clamped subset or a 4th that 400s.
        for provider in ("moonshot", "kimi-code", "kimi-sub"):
            for model in ("kimi-k3", "kimi-k3-256k", "kimi-k3-1m"):
                with self.subTest(provider=provider, model=model):
                    self.assertEqual(mc.reasoning_efforts_for(provider, model),
                                     ["low", "high", "max"])

    def test_kimi_k3_router_qualified_id_resolves(self):
        # zenmux / openrouter ship the same model under a vendor-prefixed id.
        self.assertEqual(mc.reasoning_efforts_for("zenmux", "moonshotai/kimi-k3"),
                         ["low", "high", "max"])
        self.assertEqual(mc.reasoning_efforts_for("openrouter", "moonshotai/kimi-k3-free"),
                         ["low", "high", "max"])

    def test_kimi_k2x_has_no_dial(self):
        # K2.6 / K2.7 think by default and expose no effort options.
        for model in ("kimi-k2.6", "kimi-k2.7", "kimi-k2.7-code", "kimi-for-coding",
                      "moonshotai/kimi-k2.7-code"):
            with self.subTest(model=model):
                self.assertEqual(mc.reasoning_efforts_for("moonshot", model), [])

    def test_every_advertised_rung_is_a_real_wire_value(self):
        """A ladder may only offer rungs the provider actually accepts.

        The harness does not clamp effort — but it must not invent a rung either. An
        advertised level that 400s is worse than not offering it, so each family's
        ladder is checked against the vocabulary its wire mapper recognises.
        """
        known = {"off", "low", "medium", "high", "max"}
        probes = list(self.BASELINE) + [
            ("moonshot", "kimi-k3"), ("kimi-code", "kimi-k3"), ("moonshot", "kimi-k2.7"),
            ("gemini", "gemini-2.5-flash"), ("openai", "o4-mini"),
        ]
        for provider, model in probes:
            for reasoning in (None, True, False):
                ladder = mc.reasoning_efforts_for(provider, model, reasoning=reasoning)
                self.assertFalse(set(ladder) - known,
                                 f"{provider}/{model} advertises an unknown rung: {ladder}")
                self.assertEqual(len(ladder), len(set(ladder)),
                                 f"{provider}/{model} has a duplicate rung: {ladder}")


class TestConfigConsumesCatalog(unittest.TestCase):
    def test_resolve_provider_stamps_catalog_meta(self):
        from services.agent import config as agent_config

        # No network: openai with explicit vision override + context.
        cfg = {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "sk-test",
            "context_window": 64000,
            "openai": {"vision": False, "base_url": "http://127.0.0.1:9/v1"},
        }
        # OpenAIProvider constructor imports openai SDK — may be present; if not, skip.
        try:
            prov = agent_config._resolve_provider("openai", cfg)
        except Exception as exc:  # noqa: BLE001
            if "openai" in str(exc).lower() or "OpenAI" in type(exc).__name__:
                self.skipTest(f"openai SDK unavailable: {exc}")
            raise
        self.assertFalse(prov.supports_vision)
        self.assertEqual(getattr(prov, "context_window", None), 64000)
        self.assertEqual(getattr(prov, "catalog_auto_source", None), "user")


if __name__ == "__main__":
    unittest.main()
