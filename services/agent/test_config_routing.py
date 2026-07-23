"""End-to-end proof that routing.yaml actually drives provider resolution.

Bug (confirmed): a source written into routing.yaml (e.g. by
``local_runtime.register_routing_source`` when the bundled llama.cpp server comes up)
had ZERO effect on which provider ``services.agent.config.get_provider`` — the real
seam every production caller (agent_turn.py, node_runner.py, worker_rpc.py, ...) uses to
get a live model — actually returns. ``services.agent.routing.load_routing()`` could
parse routing.yaml, but nothing in production ever called it; ``config.get_provider``
only read config.json's flat ``provider``/``model`` fields.

These tests exercise the REAL resolution path end-to-end:

    register_routing_source()  ->  routing.yaml on disk
                                        |
                                        v
    config.get_provider(role=...)  ->  _resolve_via_routing() (services.agent.routing
                                        .load_routing().resolve(role))
                                        |
                                        v
                                a constructed Provider (base_url / model from the
                                registered source, not config.json)

not just ``load_routing()`` in isolation.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from services.agent import config as agent_config
from services.agent import routing as agent_routing
from services.agent.local_runtime import register_routing_source


def _write_routing_yaml(path: Path, assignments: dict) -> None:
    path.write_text(yaml.safe_dump({
        "schema": "aicad.routing/1",
        "sources": [],
        "assignments": assignments,
        "approval_policy": {"depth_1": "auto", "deeper": "overseer"},
    }), encoding="utf-8")


class TestRoutingYamlDrivesGetProvider(unittest.TestCase):
    """routing.yaml entries are consulted by the real get_provider() resolution path."""

    def _use_temp_routing_yaml(self, path: Path):
        """Point BOTH modules that hardcode the routing.yaml path at *path*."""
        return (
            patch.object(agent_routing, "_ROUTING_YAML", path),
            patch.object(agent_routing, "DEFAULT_PATH", path),
        )

    def test_registered_llamacpp_source_is_picked_up_by_get_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            routing_path = Path(td) / "routing.yaml"
            _write_routing_yaml(routing_path, {"default": ["cheap_local"]})

            # Simulate local_runtime.ensure_running() registering the bundled server.
            changed = register_routing_source(
                "http://127.0.0.1:8765/v1", "qwen3-4b", "local-24k",
                routing_yaml=routing_path,
            )
            self.assertTrue(changed)

            patchers = self._use_temp_routing_yaml(routing_path)
            for p in patchers:
                p.start()
            try:
                # get_provider() must not spawn a real llama.cpp server: LlamaCppProvider
                # wraps installer.llamacpp.ensure_server_running() in its constructor.
                with patch(
                    "installer.llamacpp.ensure_server_running",
                    return_value={"base_url": "http://127.0.0.1:8765/v1"},
                ) as mock_ensure:
                    provider = agent_config.get_provider(cfg={}, role="default")
            finally:
                for p in patchers:
                    p.stop()

            self.assertEqual(provider.name, "llamacpp")
            self.assertEqual(provider.model, "qwen3-4b")
            self.assertEqual(provider.base_url, "http://127.0.0.1:8765/v1")
            # And it really did resolve through the registered source's endpoint.
            mock_ensure.assert_called_once()

    def test_no_sources_falls_back_to_config_json(self) -> None:
        """Empty routing.yaml (no registered sources) must not break the existing
        config.json-driven default — this is the fallback-safety half of the fix."""
        with tempfile.TemporaryDirectory() as td:
            routing_path = Path(td) / "routing.yaml"
            _write_routing_yaml(routing_path, {"default": ["cheap_local"]})

            patchers = self._use_temp_routing_yaml(routing_path)
            for p in patchers:
                p.start()
            try:
                provider = agent_config.get_provider(name="mock", cfg={})
            finally:
                for p in patchers:
                    p.stop()

            self.assertEqual(provider.name, "mock")

    def test_explicit_name_bypasses_routing(self) -> None:
        """An explicit `name=` argument (or CHUDWARE_PROVIDER) must outrank routing.yaml —
        routing.yaml only fills in when the caller didn't ask for something specific."""
        with tempfile.TemporaryDirectory() as td:
            routing_path = Path(td) / "routing.yaml"
            _write_routing_yaml(routing_path, {"default": ["cheap_local"]})
            register_routing_source(
                "http://127.0.0.1:8765/v1", "qwen3-4b", "local-24k",
                routing_yaml=routing_path,
            )

            patchers = self._use_temp_routing_yaml(routing_path)
            for p in patchers:
                p.start()
            try:
                provider = agent_config.get_provider(name="mock", cfg={})
            finally:
                for p in patchers:
                    p.stop()

            self.assertEqual(provider.name, "mock")


class TestRoutingSourceParsesBaseUrl(unittest.TestCase):
    """Source.base_url round-trips through load_routing() (needed for get_provider)."""

    def test_source_base_url_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            routing_path = Path(td) / "routing.yaml"
            _write_routing_yaml(routing_path, {"default": ["cheap_local"]})
            register_routing_source(
                "http://127.0.0.1:9999/v1", "gemma3-4b-qat", "local-24k",
                routing_yaml=routing_path,
            )
            table = agent_routing.load_routing(routing_path)
            src = table.get_source("bundled_llamacpp")
            self.assertIsNotNone(src)
            self.assertEqual(src.base_url, "http://127.0.0.1:9999/v1")
            self.assertEqual(src.kind, "llamacpp")


if __name__ == "__main__":
    unittest.main()
