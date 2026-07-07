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

    def test_reads_nested_default_generation_settings_n_ctx(self) -> None:
        # b9860 (pinned build) nests the served per-slot context here —
        # verified against the live CUDA server in the T-W31 smoke.
        from services.agent.local_runtime import get_served_n_ctx

        props_response = {"default_generation_settings": {"n_ctx": 4096},
                          "total_slots": 8}
        with _MockHTTPServer({"/props": (200, props_response)}) as srv:
            result = get_served_n_ctx(srv.base_url)
        assert result == 4096

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
                info = ensure_running({}, auto_port=True, register_routing=False)

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
                info = ensure_running({}, auto_port=False, register_routing=False)

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
                ensure_running({}, auto_port=False, register_routing=False)

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
                info = ensure_running({}, auto_port=False, register_routing=False)

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
            # "local-24k" is a services.agent.tiers context-length bucket, not a
            # routing.py capability tier (routing.KNOWN_TIERS) — it is translated to
            # its routing-vocabulary equivalent before writing so load_routing() can
            # actually parse the source back (see _to_routing_tier).
            assert src["models"][0]["tier"] == "cheap_local"
            from services.agent.routing import KNOWN_TIERS
            assert src["models"][0]["tier"] in KNOWN_TIERS
            # And the written file must round-trip through the real parser.
            from services.agent.routing import load_routing
            load_routing(routing)

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


class TestCudaVariantSelection(unittest.TestCase):
    """T-W31: CUDA build selection + pin reconcile (detection mocked — gate-safe)."""

    @staticmethod
    def _make_layout(root: Path, *, cuda: bool = True, cpu: bool = True,
                     flat: bool = False) -> None:
        if cuda:
            d = root / "build-cuda" / "bin"
            d.mkdir(parents=True)
            (d / "llama-server").write_text("#!/bin/sh\n")
        if cpu:
            d = root / "build" / "bin"
            d.mkdir(parents=True)
            (d / "llama-server").write_text("#!/bin/sh\n")
        if flat:
            (root / "llama-server").write_text("#!/bin/sh\n")

    def test_pin_reconciled_to_benchmarked_build(self) -> None:
        # T-W29 provenance: installed + benchmarked runtime is b9860 (fdb1db8),
        # NOT the previously recorded b9871.
        from services.agent import local_runtime

        assert local_runtime.LLAMA_CPP_PINNED_BUILD == "b9860"
        assert local_runtime.LLAMA_CPP_PINNED_COMMIT == "fdb1db8"

    def test_find_server_prefers_cuda_when_gpu_present(self) -> None:
        from installer import llamacpp

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=True, cpu=True)
            p = llamacpp.find_server(root, prefer_cuda=True)
            assert p is not None and "build-cuda" in str(p)
            assert llamacpp.server_variant(p) == "cuda"

    def test_find_server_cpu_fallback_without_gpu(self) -> None:
        from installer import llamacpp

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=True, cpu=True)
            p = llamacpp.find_server(root, prefer_cuda=False)
            assert p is not None and "build-cuda" not in str(p)
            assert llamacpp.server_variant(p) == "cpu"

    def test_find_server_cuda_preference_falls_back_to_cpu_build(self) -> None:
        from installer import llamacpp

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=False, cpu=True)
            p = llamacpp.find_server(root, prefer_cuda=True)
            assert p is not None
            assert llamacpp.server_variant(p) == "cpu"

    def test_find_server_flat_legacy_layout(self) -> None:
        from installer import llamacpp

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=False, cpu=False, flat=True)
            p = llamacpp.find_server(root, prefer_cuda=True)
            assert p is not None
            assert llamacpp.server_variant(p) == "unknown"

    def test_cuda_available_env_override(self) -> None:
        from installer import llamacpp

        with patch.dict(os.environ, {"AICAD_LLAMA_VARIANT": "cuda"}):
            assert llamacpp.cuda_available() is True
        with patch.dict(os.environ, {"AICAD_LLAMA_VARIANT": "cpu"}):
            assert llamacpp.cuda_available() is False

    def test_cuda_available_no_tool_is_false(self) -> None:
        from installer import llamacpp

        with patch.dict(os.environ, {"AICAD_LLAMA_VARIANT": ""}), \
             patch("installer.llamacpp.subprocess.run",
                   side_effect=FileNotFoundError("nvidia-smi")):
            assert llamacpp.cuda_available() is False

    def _run_ensure(self, root: Path, cfg: dict) -> tuple[dict, list[str]]:
        """Drive ensure_server_running with spawn/health/paths mocked (gate-safe)."""
        from installer import llamacpp

        captured: list[str] = []

        def fake_popen(args, **kwargs):
            captured.extend(str(a) for a in args)
            return MagicMock()

        gguf = root / "model.gguf"
        gguf.write_bytes(b"GGUF")
        fake_paths = MagicMock()
        fake_paths.ROOT = root
        fake_paths.MODELS = root
        with patch("installer.llamacpp._http_models_ready",
                   side_effect=[False, True]), \
             patch("installer.llamacpp.subprocess.Popen", side_effect=fake_popen), \
             patch("installer.llamacpp.paths", fake_paths), \
             patch("installer.llamacpp.find_default_model", return_value=gguf):
            res = llamacpp.ensure_server_running(cfg)
        return res, captured

    def test_ensure_running_cuda_defaults_ngl_and_parallel(self) -> None:
        # Shipped CUDA defaults = the integrator-verified smoke config
        # (-ngl 99 -np 8 -c 32768). Result reports variant + offload.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=True, cpu=False)
            server = root / "build-cuda" / "bin" / "llama-server"
            res, args = self._run_ensure(
                root, {"server_path": str(server), "port": _free_port()})
            assert res["variant"] == "cuda"
            assert res["n_gpu_layers"] == 99
            assert res["parallel"] == 8
            assert res["ctx_size"] == 32768
            assert args[args.index("--n-gpu-layers") + 1] == "99"
            assert "--parallel" in args

    def test_ensure_running_cuda_user_cfg_wins(self) -> None:
        # T-W28 discipline: explicit user cfg beats shipped CUDA defaults.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=True, cpu=False)
            server = root / "build-cuda" / "bin" / "llama-server"
            res, args = self._run_ensure(
                root, {"server_path": str(server), "port": _free_port(),
                       "gpu_layers": 10, "ctx_size": 8192})
            assert res["n_gpu_layers"] == 10
            assert res["parallel"] is None  # ctx pinned -> no parallel default
            assert args[args.index("--n-gpu-layers") + 1] == "10"
            assert "--parallel" not in args

    def test_ensure_running_cpu_build_gets_no_gpu_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=False, cpu=True)
            server = root / "build" / "bin" / "llama-server"
            res, args = self._run_ensure(
                root, {"server_path": str(server), "port": _free_port()})
            assert res["variant"] == "cpu"
            assert res["n_gpu_layers"] is None
            assert "--n-gpu-layers" not in args
            assert "--parallel" not in args

    def test_launch_log_records_variant_and_offload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_layout(root, cuda=True, cpu=False)
            server = root / "build-cuda" / "bin" / "llama-server"
            self._run_ensure(root, {"server_path": str(server),
                                    "port": _free_port()})
            log = (root / "logs" / "llamacpp-server.log").read_text()
            assert "variant=cuda" in log
            assert "n_gpu_layers=99" in log

    def test_register_routing_source_accepts_model_list(self) -> None:
        # T-W31: the provisioner registers BOTH bundled models (W29 role split).
        from services.agent.local_runtime import register_routing_source

        with tempfile.TemporaryDirectory() as td:
            routing = Path(td) / "routing.yaml"
            routing.write_text(yaml.safe_dump({
                "schema": "aicad.routing/1",
                "sources": [],
                "assignments": {"default": ["cheap_local"]},
            }), encoding="utf-8")
            changed = register_routing_source(
                "http://127.0.0.1:8765/v1",
                ["qwen3-4b", "gemma3-4b-qat"],
                "cheap_local",
                routing_yaml=routing,
            )
            assert changed is True
            data = yaml.safe_load(routing.read_text())
            ids = [m["id"] for m in data["sources"][0]["models"]]
            assert ids == ["qwen3-4b", "gemma3-4b-qat"]


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


# --------------------------------------------------------------------------- T-W29
# Backend-selection seam + extras-guarded transformers backend.
# These run in the GATE venv (no torch) and must stay green there: the point is
# that the optional backend degrades HONESTLY, not that torch is installed.


class TestBackendSeam(unittest.TestCase):
    def test_select_backend_default_llamacpp(self) -> None:
        from services.agent.local_runtime import select_backend
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AICAD_LOCAL_BACKEND", None)
            assert select_backend() == "llamacpp"

    def test_select_backend_env_override(self) -> None:
        from services.agent.local_runtime import select_backend
        with patch.dict(os.environ, {"AICAD_LOCAL_BACKEND": "transformers"}):
            assert select_backend() == "transformers"

    def test_select_backend_cfg(self) -> None:
        from services.agent.local_runtime import select_backend
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AICAD_LOCAL_BACKEND", None)
            assert select_backend({"local_backend": "transformers"}) == "transformers"

    def test_select_backend_rejects_unknown(self) -> None:
        from services.agent.local_runtime import LocalRuntimeError, select_backend
        with patch.dict(os.environ, {"AICAD_LOCAL_BACKEND": "bogus"}):
            with pytest.raises(LocalRuntimeError, match="unknown local backend"):
                select_backend()

    def test_transformers_available_never_raises(self) -> None:
        # Must be callable and return a bool even with no torch (gate venv).
        from services.agent.local_runtime import transformers_available
        assert isinstance(transformers_available(), bool)

    def test_transformers_backend_honest_skip_without_extras(self) -> None:
        # In the gate venv torch is absent → constructing must raise
        # BackendUnavailable, NOT crash at import time.
        from services.agent.local_runtime import (
            BackendUnavailable, TransformersBackend, transformers_available,
        )
        if transformers_available():
            pytest.skip("torch/transformers present — honest-skip path not exercised")
        with pytest.raises(BackendUnavailable, match="separate venv|SEPARATE venv"):
            TransformersBackend("any/model")
