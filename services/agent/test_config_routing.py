"""End-to-end proof that routing.yaml drives provider resolution.

These tests exercise a configured source through the production
config.get_provider(role=...) path, not just load_routing() in isolation.
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from services.agent import config as agent_config
from services.agent import routing as agent_routing

def _write_routing_yaml(path: Path, assignments: dict, sources: list[dict] | None = None) -> None:
    path.write_text(yaml.safe_dump({
        "schema": "aicad.routing/1",
        "sources": sources or [],
        "assignments": assignments,
    }), encoding="utf-8")


def _llamacpp_source(base_url: str = "http://127.0.0.1:8765/v1",
                     model: str = "qwen3-4b") -> dict:
    return {
        "id": "bundled_llamacpp",
        "kind": "llamacpp",
        "models": [{"id": model, "tier": "cheap_local"}],
        "base_url": base_url,
    }


class TestRoutingYamlDrivesGetProvider(unittest.TestCase):
    """routing.yaml entries are consulted by the real get_provider() resolution path."""

    def _use_temp_routing_yaml(self, path: Path):
        """Point BOTH modules that hardcode the routing.yaml path at *path*."""
        return (
            patch.object(agent_routing, "_ROUTING_YAML", path),
            patch.object(agent_routing, "DEFAULT_PATH", path),
        )

    def test_configured_llamacpp_source_is_picked_up_by_get_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            routing_path = Path(td) / "routing.yaml"
            _write_routing_yaml(routing_path, {"default": ["cheap_local"]},
                                [_llamacpp_source()])

            patchers = self._use_temp_routing_yaml(routing_path)
            for p in patchers:
                p.start()
            try:
                # get_provider() must not spawn a real llama.cpp server: LlamaCppProvider
                # wraps services.installer.llamacpp.ensure_server_running() in its constructor.
                with patch(
                    "services.installer.llamacpp.ensure_server_running",
                    return_value={"base_url": "http://127.0.0.1:8765/v1"},
                ) as mock_ensure:
                    provider = agent_config.get_provider(cfg={}, role="default")
            finally:
                for p in patchers:
                    p.stop()

            self.assertEqual(provider.name, "llamacpp")
            self.assertEqual(provider.model, "qwen3-4b")
            self.assertEqual(provider.base_url, "http://127.0.0.1:8765/v1")
            # And it really did resolve through the configured source's endpoint.
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
            _write_routing_yaml(routing_path, {"default": ["cheap_local"]},
                                [_llamacpp_source()])

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
            _write_routing_yaml(routing_path, {"default": ["cheap_local"]},
                                [_llamacpp_source("http://127.0.0.1:9999/v1", "gemma3-4b-qat")])
            table = agent_routing.load_routing(routing_path)
            src = table.get_source("bundled_llamacpp")
            self.assertIsNotNone(src)
            self.assertEqual(src.base_url, "http://127.0.0.1:9999/v1")
            self.assertEqual(src.kind, "llamacpp")


@unittest.skipIf(os.name == "nt", "POSIX permission contract")
class TestConfigPermissions(unittest.TestCase):
    def test_save_config_uses_owner_only_directory_and_file_modes(self) -> None:
        from services import paths as service_paths

        with tempfile.TemporaryDirectory() as td:
            config_dir = Path(td) / "nested"
            with patch.object(service_paths, "CONFIG", config_dir / "config.json"):
                agent_config.save_config({"keys": {"provider": "secret"}})
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((config_dir / "config.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((config_dir / "config.json.lock").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
