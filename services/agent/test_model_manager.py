"""Tests for services/agent/model_manager.py — model registry + provisioning.

Strategy (offline-ML-track discipline — no all-skip):
  * All download logic is tested via a tiny dummy-GGUF fixture served by an in-process
    HTTP server.  No real HuggingFace traffic in CI.
  * Registry correctness (ids, licenses, ctx, quants) tested statically.
  * Checksum verify, resume (206 path), and already-complete (416 path) each have
    dedicated test cases.
  * ``get_model_path`` / ``list_models`` exercised against a temp directory.

Live provisioning: marked skip unless CHUDWARE_PROVISION_LIVE is set, to avoid
downloading multi-GB GGUFs in CI.
"""
from __future__ import annotations

import hashlib
import io
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest


# --------------------------------------------------------------------------- dummy GGUF fixture

# A minimal valid-looking binary blob we use as a "GGUF" for testing.
_DUMMY_CONTENT = b"GGUF" + b"\x00" * 256  # 260 bytes, not a real model


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- mock HTTP server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _GGUFServer:
    """Minimal HTTP server that serves a fixed byte blob as a GGUF download.

    Supports:
      * Normal 200 responses.
      * 206 Partial Content (resume) when Range header is present and satisfiable.
      * 416 Range Not Satisfiable when the Range start equals the file length.
    """

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._port = _free_port()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/model.gguf"

    def __enter__(self) -> "_GGUFServer":
        content = self._content

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ANN001
                pass

            def do_GET(self):  # noqa: N802
                rng = self.headers.get("Range", "")
                if rng.startswith("bytes="):
                    start = int(rng.split("=")[1].rstrip("-"))
                    if start >= len(content):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(content)}")
                        self.end_headers()
                        return
                    body = content[start:]
                    self.send_response(206)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{len(content)-1}/{len(content)}",
                    )
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

        self._server = HTTPServer(("127.0.0.1", self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        if self._server:
            self._server.shutdown()


# --------------------------------------------------------------------------- registry tests


class TestRegistry(unittest.TestCase):
    """Static checks on _REGISTRY — catches typos in model ids/licenses."""

    def test_both_models_present(self) -> None:
        from services.agent.model_manager import _REGISTRY

        assert "qwen3-4b" in _REGISTRY
        assert "gemma3-4b-qat" in _REGISTRY

    def test_qwen3_license_is_apache(self) -> None:
        from services.agent.model_manager import _REGISTRY

        assert _REGISTRY["qwen3-4b"]["license"] == "Apache-2.0"

    def test_gemma3_license_is_tou(self) -> None:
        from services.agent.model_manager import _REGISTRY

        # Must NOT be Apache-2.0 — Gemma has use-restricted ToU.
        lic = _REGISTRY["gemma3-4b-qat"]["license"]
        assert lic == "Gemma-ToU"
        assert "Apache" not in lic

    def test_qwen3_ctx(self) -> None:
        from services.agent.model_manager import _REGISTRY

        assert _REGISTRY["qwen3-4b"]["ctx"] == 32768

    def test_gemma3_ctx(self) -> None:
        from services.agent.model_manager import _REGISTRY

        # Gemma 3 4B has a 128k context window.
        assert _REGISTRY["gemma3-4b-qat"]["ctx"] == 131072

    def test_quant_fields(self) -> None:
        from services.agent.model_manager import _REGISTRY

        assert _REGISTRY["qwen3-4b"]["quant"] == "Q4_K_M"
        assert _REGISTRY["gemma3-4b-qat"]["quant"] == "Q4_K_M"

    def test_fallback_quants_present(self) -> None:
        from services.agent.model_manager import _REGISTRY

        assert "fallback_file" in _REGISTRY["qwen3-4b"]
        assert "fallback_file" in _REGISTRY["gemma3-4b-qat"]


class TestW29RoleMapping(unittest.TestCase):
    """The local-model benchmark's role-model mapping shipped as registry data.

    Numbers/verdicts copied from the benchmark, not re-derived:
    qwen3-4b GBNF broken -> prompted_json fan-out; gemma3-4b-qat GBNF 100% ->
    gbnf_worker; gemma4-e4b registered as a comparison-only GBNF backup (not a
    shipped role_shape default, see
    test_gemma4_e4b_registered_but_not_a_shipped_default); DiffusionGemma NO-GO.
    """

    def test_qwen3_is_prompted_json_fanout(self) -> None:
        from services.agent.model_manager import _REGISTRY

        assert _REGISTRY["qwen3-4b"]["structured_output"] == "prompted_json"
        assert _REGISTRY["qwen3-4b"]["role_shape"] == "fanout"

    def test_gemma3_is_gbnf_worker(self) -> None:
        from services.agent.model_manager import _REGISTRY

        assert _REGISTRY["gemma3-4b-qat"]["structured_output"] == "gbnf"
        assert _REGISTRY["gemma3-4b-qat"]["role_shape"] == "gbnf_worker"

    def test_gemma4_e4b_registered_but_not_a_shipped_default(self) -> None:
        # 2026-07-07 tokenmaxxing: gemma4-e4b IS now registered (so the gym's
        # local-24k-gemma4-e4b tier can address/provision it as a comparison-only
        # GBNF backup worker), but it must not usurp gemma3-4b-qat as the shipped
        # gbnf_worker default (slower/larger, no measured upside — REPORT.md).
        # DiffusionGemma remains a hard NO-GO (16 GB min quant on a 12 GB card).
        from services.agent.model_manager import _REGISTRY, model_for_role_shape

        assert "gemma4-e4b" in _REGISTRY
        assert _REGISTRY["gemma4-e4b"]["role_shape"] != "gbnf_worker"
        assert model_for_role_shape("gbnf_worker") == "gemma3-4b-qat"
        assert not any("diffusion" in k.lower() for k in _REGISTRY)

    def test_structured_output_for(self) -> None:
        from services.agent.model_manager import structured_output_for

        assert structured_output_for("qwen3-4b") == "prompted_json"
        assert structured_output_for("gemma3-4b-qat") == "gbnf"
        # Unknown model -> the caller's tier default.
        assert structured_output_for("some-user-model") == "gbnf"
        assert structured_output_for("some-user-model", default="json_mode") == "json_mode"

    def test_model_for_role_shape(self) -> None:
        from services.agent.model_manager import model_for_role_shape

        assert model_for_role_shape("gbnf_worker") == "gemma3-4b-qat"
        assert model_for_role_shape("fanout") == "qwen3-4b"
        assert model_for_role_shape("nonsense") is None

    def test_list_models_exposes_capabilities(self) -> None:
        from services.agent.model_manager import list_models

        by_id = {m["id"]: m for m in list_models()}
        assert by_id["qwen3-4b"]["structured_output"] == "prompted_json"
        assert by_id["gemma3-4b-qat"]["structured_output"] == "gbnf"
        assert by_id["qwen3-4b"]["parallel_slots"] == 8

    def test_tiers_yaml_local_models_block_agrees(self) -> None:
        # tiers.yaml shipped defaults and the registry must never drift apart.
        from services.agent.model_manager import model_for_role_shape
        from services.agent.tiers import load_local_models

        block = load_local_models()
        assert block["gbnf_worker"] == model_for_role_shape("gbnf_worker")
        assert block["fanout"] == model_for_role_shape("fanout")
        assert block["parallel_slots"] == 8


# --------------------------------------------------------------------------- list_models


class TestListModels(unittest.TestCase):
    def test_returns_three_entries(self) -> None:
        # 2026-07-07: gemma4-e4b joined the registry (tokenmaxxing comparison tier).
        from services.agent import model_manager

        with tempfile.TemporaryDirectory() as td:
            with patch.object(model_manager, "_model_dir", return_value=Path(td)):
                models = model_manager.list_models()

        assert len(models) == 3
        ids = {m["id"] for m in models}
        assert ids == {"qwen3-4b", "gemma3-4b-qat", "gemma4-e4b"}

    def test_present_false_when_not_downloaded(self) -> None:
        from services.agent import model_manager

        with tempfile.TemporaryDirectory() as td:
            with patch.object(model_manager, "_model_dir", return_value=Path(td)):
                models = model_manager.list_models()

        for m in models:
            assert m["present"] is False

    def test_present_true_when_file_exists(self) -> None:
        from services.agent import model_manager
        from services.agent.model_manager import _REGISTRY

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Create a fake GGUF file for qwen3-4b
            fake = td_path / _REGISTRY["qwen3-4b"]["file"]
            fake.write_bytes(_DUMMY_CONTENT)

            with patch.object(model_manager, "_model_dir", return_value=td_path):
                models = model_manager.list_models()

        model_map = {m["id"]: m for m in models}
        assert model_map["qwen3-4b"]["present"] is True
        assert model_map["gemma3-4b-qat"]["present"] is False


# --------------------------------------------------------------------------- get_model_path


class TestGetModelPath(unittest.TestCase):
    def test_returns_none_when_absent(self) -> None:
        from services.agent import model_manager

        with tempfile.TemporaryDirectory() as td:
            with patch.object(model_manager, "_model_dir", return_value=Path(td)):
                result = model_manager.get_model_path("qwen3-4b")

        assert result is None

    def test_returns_path_when_present(self) -> None:
        from services.agent import model_manager
        from services.agent.model_manager import _REGISTRY

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            fake = td_path / _REGISTRY["qwen3-4b"]["file"]
            fake.write_bytes(_DUMMY_CONTENT)

            with patch.object(model_manager, "_model_dir", return_value=td_path):
                result = model_manager.get_model_path("qwen3-4b")

        assert result == fake

    def test_returns_none_for_unknown_id(self) -> None:
        from services.agent.model_manager import get_model_path

        result = get_model_path("nonexistent-model")
        assert result is None


# --------------------------------------------------------------------------- provision (download)


class TestProvision(unittest.TestCase):
    """Tests for provision() using a tiny dummy-GGUF served by _GGUFServer."""

    def test_download_200_ok(self) -> None:
        """Full 200 OK download lands correctly."""
        from services.agent import model_manager
        from services.agent.model_manager import _REGISTRY

        entry = _REGISTRY["qwen3-4b"]

        with _GGUFServer(_DUMMY_CONTENT) as srv, tempfile.TemporaryDirectory() as td:
            td_path = Path(td)

            def fake_hf_url(repo, filename):
                return srv.url

            with (
                patch.object(model_manager, "_model_dir", return_value=td_path),
                patch.object(model_manager, "_hf_url", side_effect=fake_hf_url),
            ):
                result = model_manager.provision("qwen3-4b")

            assert result["status"] == "ok"
            assert result["model_id"] == "qwen3-4b"
            dest = Path(result["path"])
            assert dest.exists()
            assert dest.read_bytes() == _DUMMY_CONTENT

    def test_already_present_returns_status(self) -> None:
        """If file exists and force=False, returns already_present without downloading."""
        from services.agent import model_manager
        from services.agent.model_manager import _REGISTRY

        entry = _REGISTRY["qwen3-4b"]

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            fake = td_path / entry["file"]
            fake.write_bytes(_DUMMY_CONTENT)

            with patch.object(model_manager, "_model_dir", return_value=td_path):
                result = model_manager.provision("qwen3-4b")

        assert result["status"] == "already_present"

    def test_force_redownloads(self) -> None:
        """force=True triggers a fresh download even when file exists."""
        from services.agent import model_manager
        from services.agent.model_manager import _REGISTRY

        entry = _REGISTRY["qwen3-4b"]
        new_content = b"GGUF_NEW" + b"\x01" * 256

        with _GGUFServer(new_content) as srv, tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            fake = td_path / entry["file"]
            fake.write_bytes(_DUMMY_CONTENT)  # old content

            def fake_hf_url(repo, filename):
                return srv.url

            with (
                patch.object(model_manager, "_model_dir", return_value=td_path),
                patch.object(model_manager, "_hf_url", side_effect=fake_hf_url),
            ):
                result = model_manager.provision("qwen3-4b", force=True)

            assert result["status"] == "ok"
            assert Path(result["path"]).read_bytes() == new_content

    def test_resume_206_path(self) -> None:
        """When a .part file exists, the 206 resume path appends correctly."""
        from services.agent import model_manager
        from services.agent.model_manager import _REGISTRY

        entry = _REGISTRY["qwen3-4b"]
        # Split content: first half already in .part, second half from server 206
        half = len(_DUMMY_CONTENT) // 2
        first_half = _DUMMY_CONTENT[:half]
        # Server will serve 206 from byte `half` onwards
        full_content = _DUMMY_CONTENT  # server has the full content

        with _GGUFServer(full_content) as srv, tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Create pre-existing .part with the first half
            part_path = td_path / (entry["file"] + ".part")
            part_path.write_bytes(first_half)

            def fake_hf_url(repo, filename):
                return srv.url

            with (
                patch.object(model_manager, "_model_dir", return_value=td_path),
                patch.object(model_manager, "_hf_url", side_effect=fake_hf_url),
            ):
                result = model_manager.provision("qwen3-4b")

            assert result["status"] == "ok"
            # The final file must equal the full content
            assert Path(result["path"]).read_bytes() == full_content

    def test_416_already_complete(self) -> None:
        """A 416 response from the server means the .part is complete; rename it."""
        from services.agent import model_manager
        from services.agent.model_manager import _REGISTRY

        entry = _REGISTRY["qwen3-4b"]
        # Server has the same content as .part — 416 will be returned
        content = _DUMMY_CONTENT

        with _GGUFServer(content) as srv, tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # .part has the full content already
            part_path = td_path / (entry["file"] + ".part")
            part_path.write_bytes(content)

            def fake_hf_url(repo, filename):
                return srv.url

            with (
                patch.object(model_manager, "_model_dir", return_value=td_path),
                patch.object(model_manager, "_hf_url", side_effect=fake_hf_url),
            ):
                result = model_manager.provision("qwen3-4b")

            assert result["status"] == "ok"
            assert Path(result["path"]).read_bytes() == content

    def test_checksum_mismatch_raises_and_deletes_file(self) -> None:
        """When sha256 is set and doesn't match, provision raises and cleans up."""
        from services.agent import model_manager
        from services.agent.model_manager import ModelProvisionError, _REGISTRY

        with _GGUFServer(_DUMMY_CONTENT) as srv, tempfile.TemporaryDirectory() as td:
            td_path = Path(td)

            def fake_hf_url(repo, filename):
                return srv.url

            # Patch registry entry to have a wrong sha256
            patched_entry = dict(_REGISTRY["qwen3-4b"])
            patched_entry["sha256"] = "deadbeef" * 8  # 64 hex chars, wrong

            with (
                patch.object(model_manager, "_model_dir", return_value=td_path),
                patch.object(model_manager, "_hf_url", side_effect=fake_hf_url),
                patch.dict(model_manager._REGISTRY, {"qwen3-4b": patched_entry}),
            ):
                with pytest.raises(ModelProvisionError, match="sha256 mismatch"):
                    model_manager.provision("qwen3-4b")

            # Bad file must have been cleaned up
            dest = td_path / _REGISTRY["qwen3-4b"]["file"]
            assert not dest.exists()

    def test_checksum_skipped_when_sha256_is_none(self) -> None:
        """Registry entries with sha256=None skip verification — default for both picks."""
        from services.agent import model_manager

        with _GGUFServer(_DUMMY_CONTENT) as srv, tempfile.TemporaryDirectory() as td:
            td_path = Path(td)

            def fake_hf_url(repo, filename):
                return srv.url

            with (
                patch.object(model_manager, "_model_dir", return_value=td_path),
                patch.object(model_manager, "_hf_url", side_effect=fake_hf_url),
            ):
                result = model_manager.provision("qwen3-4b")

        assert result["status"] == "ok"

    def test_unknown_model_id_raises_key_error(self) -> None:
        from services.agent.model_manager import provision

        with pytest.raises(KeyError, match="Unknown model id"):
            provision("no-such-model")


# --------------------------------------------------------------------------- live provision


@pytest.mark.skipif(
    not os.environ.get("CHUDWARE_PROVISION_LIVE"),
    reason="Live provisioning: set CHUDWARE_PROVISION_LIVE=1 to run (downloads GBs)",
)
class TestLiveProvision(unittest.TestCase):
    """Actually downloads a real GGUF from HuggingFace — skip unless explicitly enabled.

    Recipe:
        export CHUDWARE_PROVISION_LIVE=1
        pytest services/agent/test_model_manager.py::TestLiveProvision -v -s
    """

    def test_provision_qwen3_4b(self) -> None:
        from services.agent.model_manager import get_model_path, provision

        result = provision("qwen3-4b")
        assert result["status"] in ("ok", "already_present")
        path = get_model_path("qwen3-4b")
        assert path is not None
        assert path.exists()
        assert path.stat().st_size > 1_000_000  # at least 1 MB
        print(f"\nLIVE: provisioned qwen3-4b at {path}")  # noqa: T201
