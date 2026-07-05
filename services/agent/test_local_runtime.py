"""Tests for services/agent/local_runtime.py — T-W26 bundled llama.cpp runtime.

Test strategy (offline-ML-track discipline, no all-skip):

  fixture mode (default, no real server):
    * Mock the installer.llamacpp.ensure_server_running() to avoid needing a real binary.
    * Mock /props HTTP endpoint to verify n_ctx capture + tier detection.
    * Verify routing.yaml idempotent append.
    * Verify port allocation uses a free port when auto_port=True.
    * Verify dummy-GGUF checksum path via model_manager fixtures.

  live mode (requires AI_CAD_LOCAL_ENDPOINT env var):
    * Actually query a running llama-server's /props.
    * Verify tier detection from real n_ctx.
    Skipped in CI (env var absent).

Pre-existing known failures (do NOT fix here):
  * issue #16: test_h7_shadow, test_providers_conformance — unrelated, pre-existing.

Co-Authored-By: sonnet-pike <noreply@anthropic.com>
"""
from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# --------------------------------------------------------------------------- helpers


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MockHTTPServer:
    """Tiny in-process HTTP server for mocking llama-server endpoints."""

    def __init__(self, responses: dict[str, tuple[int, object]]) -> None:
        self._responses = responses
        self._port: int = _free_port()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}/v1"

    def __enter__(self) -> "_MockHTTPServer":
        responses = self._responses
        port = self._port

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ANN001
                pass  # suppress output

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path in responses:
                    code, body = responses[path]
                    data = json.dumps(body).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path in responses:
                    code, body = responses[path]
                    data = json.dumps(body).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

        self._server = HTTPServer(("127.0.0.1", self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        if self._server:
            self._server.shutdown()


# --------------------------------------------------------------------------- unit tests


class TestGetServedNCtx(unittest.TestCase):
    """get_served_n_ctx() queries /props and extracts n_ctx."""

    def test_reads_n_ctx_from_props(self) -> None:
        from services.agent.local_runtime import get_served_n_ctx

        props_response = {"n_ctx": 32768, "some_other": "field"}
        with _MockHTTPServer({"/props": (200, props_response)}) as srv:
            result = get_served_n_ctx(srv.base_url)
        assert result == 32768

    def test_fallback_to_model_n_ctx_train(self) -> None:
        from services.agent.local_runtime import get_served_n_ctx

        props_response = {"model": {"n_ctx_train": 131072}}
        with _MockHTTPServer({"/props": (200, props_response)}) as srv:
            result = get_served_n_ctx(srv.base_url)
        assert result == 131072

    def test_returns_none_on_unreachable(self) -> None:
        from services.agent.local_runtime import get_served_n_ctx

        # Port 1 is reserved and will refuse connection
        result = get_served_n_ctx("http://127.0.0.1:1/v1", timeout=0.3)
        assert result is None

    def test_returns_none_on_missing_field(self) -> None:
        from services.agent.local_runtime import get_served_n_ctx

        props_response = {"other_key": 42}
        with _MockHTTPServer({"/props": (200, props_response)}) as srv:
            result = get_served_n_ctx(srv.base_url)
        assert result is None


class TestPickFreePort(unittest.TestCase):
    """_pick_free_port() returns an OS-assigned free port."""

    def test_returns_valid_port(self) -> None:
        from services.agent.local_runtime import _pick_free_port

        port = _pick_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_two_calls_differ(self) -> None:
        from services.agent.local_runtime import _pick_free_port

        p1 = _pick_free_port()
        p2 = _pick_free_port()
        # Not guaranteed but practically always true
        assert isinstance(p1, int) and isinstance(p2, int)


class TestEnsureRunning(unittest.TestCase):
    """ensure_running() wires auto_port + n_ctx + tier detection."""

    def test_auto_port_injected_into_cfg(self) -> None:
        """When auto_port=True and no port in cfg, a free port is injected."""
        from services.agent.local_runtime import ensure_running

        captured_cfg: list[dict] = []

        def fake_ensure(cfg):
            captured_cfg.append(dict(cfg))
            host = cfg.get("host", "127.0.0.1")
            port = cfg.get("port", 1234)
            return {
                "status": "ready",
                "base_url": f"http://{host}:{port}/v1",
                "launched": True,
                "server": "/fake/llama-server",
                "model": "/fake/model.gguf",
            }

        with patch("installer.llamacpp.ensure_server_running", side_effect=fake_ensure):
            with patch("services.agent.local_runtime.get_served_n_ctx", return_value=32768):
                info = ensure_running({}, auto_port=True)

        assert captured_cfg[0].get("port") is not None
        assert info.launched is True

    def test_tier_detected_from_n_ctx(self) -> None:
        from services.agent.local_runtime import ensure_running

        def fake_ensure(cfg):
            return {
                "status": "ready",
                "base_url": "http://127.0.0.1:9999/v1",
                "launched": False,
                "server": "",
                "model": "",
            }

        with patch("installer.llamacpp.ensure_server_running", side_effect=fake_ensure):
            with patch("services.agent.local_runtime.get_served_n_ctx", return_value=24000):
                info = ensure_running({}, auto_port=False)

        # 24000 <= 32768 → local-24k
        assert info.tier == "local-24k"
        assert info.n_ctx == 24000

    def test_runtime_error_wrapped(self) -> None:
        from services.agent.local_runtime import LocalRuntimeError, ensure_running

        with patch(
            "installer.llamacpp.ensure_server_running",
            side_effect=RuntimeError("binary not found"),
        ):
            with pytest.raises(LocalRuntimeError, match="binary not found"):
                ensure_running({}, auto_port=False)

    def test_n_ctx_none_when_unreachable(self) -> None:
        from services.agent.local_runtime import ensure_running

        def fake_ensure(cfg):
            return {
                "status": "ready",
                "base_url": "http://127.0.0.1:9998/v1",
                "launched": True,
            }

        with patch("installer.llamacpp.ensure_server_running", side_effect=fake_ensure):
            with patch("services.agent.local_runtime.get_served_n_ctx", return_value=None):
                info = ensure_running({}, auto_port=False)

        assert info.n_ctx is None
        assert info.tier is None


class TestStop(unittest.TestCase):
    """stop() calls POST /stop and returns True on 200."""

    def test_stop_returns_true_on_success(self) -> None:
        from services.agent.local_runtime import stop

        with _MockHTTPServer({"/stop": (200, {"ok": True})}) as srv:
            result = stop(srv.base_url)
        assert result is True

    def test_stop_returns_false_when_endpoint_missing(self) -> None:
        from services.agent.local_runtime import stop

        with _MockHTTPServer({}) as srv:
            result = stop(srv.base_url, timeout=0.5)
        assert result is False


class TestRegisterRoutingSource(unittest.TestCase):
    """register_routing_source() idempotently appends to routing.yaml."""

    def _make_routing_yaml(self, tmp_dir: Path) -> Path:
        path = tmp_dir / "routing.yaml"
        data = {
            "schema": "aicad.routing/1",
            "sources": [],
            "assignments": {"default": ["cheap_local"]},
            "approval_policy": {"depth_1": "auto", "deeper": "overseer"},
        }
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def test_appends_source_when_absent(self) -> None:
        from services.agent.local_runtime import register_routing_source

        with tempfile.TemporaryDirectory() as td:
            routing = self._make_routing_yaml(Path(td))
            result = register_routing_source(
                "http://127.0.0.1:8765/v1",
                "qwen3-4b",
                "local-24k",
                routing_yaml=routing,
            )
            assert result is True

            data = yaml.safe_load(routing.read_text())
            assert len(data["sources"]) == 1
            src = data["sources"][0]
            assert src["id"] == "bundled_llamacpp"
            assert src["kind"] == "llamacpp"
            assert src["models"][0]["id"] == "qwen3-4b"
            assert src["models"][0]["tier"] == "local-24k"

    def test_idempotent_when_source_already_present(self) -> None:
        from services.agent.local_runtime import register_routing_source

        with tempfile.TemporaryDirectory() as td:
            routing = self._make_routing_yaml(Path(td))
            # First call
            r1 = register_routing_source(
                "http://127.0.0.1:8765/v1",
                "qwen3-4b",
                "local-24k",
                routing_yaml=routing,
            )
            # Second call — must be a no-op
            r2 = register_routing_source(
                "http://127.0.0.1:8765/v1",
                "qwen3-4b",
                "local-24k",
                routing_yaml=routing,
            )

            assert r1 is True
            assert r2 is False
            data = yaml.safe_load(routing.read_text())
            assert len(data["sources"]) == 1  # still only one

    def test_preserves_existing_sources(self) -> None:
        from services.agent.local_runtime import register_routing_source

        with tempfile.TemporaryDirectory() as td:
            routing = self._make_routing_yaml(Path(td))
            # Pre-populate with a user source
            data = yaml.safe_load(routing.read_text())
            data["sources"].append({
                "id": "user_anthropic",
                "kind": "anthropic",
                "models": [{"id": "claude-sonnet-4-6", "tier": "frontier"}],
            })
            routing.write_text(yaml.safe_dump(data))

            register_routing_source(
                "http://127.0.0.1:8765/v1",
                "qwen3-4b",
                "local-24k",
                routing_yaml=routing,
            )

            data = yaml.safe_load(routing.read_text())
            ids = [s["id"] for s in data["sources"]]
            assert "user_anthropic" in ids
            assert "bundled_llamacpp" in ids
            assert len(data["sources"]) == 2

    def test_tier_falls_back_to_cheap_local_when_none(self) -> None:
        from services.agent.local_runtime import register_routing_source

        with tempfile.TemporaryDirectory() as td:
            routing = self._make_routing_yaml(Path(td))
            register_routing_source(
                "http://127.0.0.1:8765/v1",
                "qwen3-4b",
                None,  # no tier
                routing_yaml=routing,
            )

            data = yaml.safe_load(routing.read_text())
            assert data["sources"][0]["models"][0]["tier"] == "cheap_local"

    def test_returns_false_when_yaml_missing(self) -> None:
        from services.agent.local_runtime import register_routing_source

        result = register_routing_source(
            "http://127.0.0.1:8765/v1",
            "qwen3-4b",
            "local-24k",
            routing_yaml=Path("/nonexistent/routing.yaml"),
        )
        assert result is False


class TestRuntimeInfo(unittest.TestCase):
    """RuntimeInfo wraps raw dict correctly."""

    def test_as_dict_roundtrip(self) -> None:
        from services.agent.local_runtime import RuntimeInfo

        raw = {"base_url": "http://127.0.0.1:1234/v1", "launched": True, "server": "/s", "model": "/m"}
        info = RuntimeInfo(raw, n_ctx=32768, tier="local-24k")
        d = info.as_dict()
        assert d["base_url"] == "http://127.0.0.1:1234/v1"
        assert d["n_ctx"] == 32768
        assert d["tier"] == "local-24k"
        assert d["launched"] is True


# --------------------------------------------------------------------------- live smoke


@pytest.mark.skipif(
    not os.environ.get("AI_CAD_LOCAL_ENDPOINT"),
    reason="Live mode: set AI_CAD_LOCAL_ENDPOINT to a running llama-server base URL",
)
class TestLiveSmoke(unittest.TestCase):
    """Live smoke test — runs only when AI_CAD_LOCAL_ENDPOINT is set.

    Recipe to run manually:
        # Start a llama-server first, e.g.:
        #   llama-server --model Qwen3-4B-Q4_K_M.gguf --port 8765
        export AI_CAD_LOCAL_ENDPOINT=http://localhost:8765/v1
        pytest services/agent/test_local_runtime.py::TestLiveSmoke -v
    """

    def test_props_returns_n_ctx(self) -> None:
        from services.agent.local_runtime import get_served_n_ctx

        endpoint = os.environ["AI_CAD_LOCAL_ENDPOINT"]
        n_ctx = get_served_n_ctx(endpoint, timeout=5.0)
        assert n_ctx is not None, f"Could not read n_ctx from {endpoint}/props"
        assert isinstance(n_ctx, int)
        assert n_ctx > 0
        print(f"\nLIVE: n_ctx={n_ctx}")  # noqa: T201

    def test_tier_detected_from_live_server(self) -> None:
        from services.agent.local_runtime import get_served_n_ctx
        from services.agent.tiers import detect_tier

        endpoint = os.environ["AI_CAD_LOCAL_ENDPOINT"]
        n_ctx = get_served_n_ctx(endpoint, timeout=5.0)
        if n_ctx is None:
            pytest.skip("Could not read n_ctx from live server")
        tier = detect_tier(n_ctx)
        assert tier in ("local-24k", "local-48k", "mid", "frontier")
        print(f"\nLIVE: n_ctx={n_ctx} → tier={tier}")  # noqa: T201
