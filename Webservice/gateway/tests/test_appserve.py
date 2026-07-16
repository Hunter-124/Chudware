"""WASM /app static serve + /perf/client beacon tests (dev-auth hermetic)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

os.environ["BILLET_WEB_DEV"] = "1"
os.environ.setdefault("BILLET_WEB_DATA_ROOT", str(Path.home() / "billet-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import app as app_mod  # noqa: E402
from gateway.config import settings  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


@pytest.fixture()
def app_tree(tmp_path, monkeypatch):
    """Point settings.app_dir at a tiny fake WASM client tree."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>billet</title>\n", encoding="utf-8")
    (root / "billet.js").write_text("/* wasm glue */\n", encoding="utf-8")
    (root / "billet.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (root / "billet.data").write_bytes(b"PACK")
    monkeypatch.setattr(settings, "app_dir", root)
    return root


@pytest.fixture()
def traces_dir(tmp_path, monkeypatch):
    d = tmp_path / "traces"
    d.mkdir()
    monkeypatch.setattr(settings, "perf_traces_dir", d)
    return d


class TestAppServe:
    def test_app_requires_auth(self, client, app_tree):
        r = client.get("/app/")
        assert r.status_code in (401, 403)

    def test_app_index_coop_coep_and_cache(self, client, app_tree):
        r = client.get("/app/", headers=DEV_HEADERS)
        assert r.status_code == 200
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
        assert r.headers["cross-origin-embedder-policy"] == "require-corp"
        assert r.headers.get("cache-control") == "no-cache"
        assert "text/html" in (r.headers.get("content-type") or "")
        assert "billet" in r.text

    def test_app_bare_path_same_as_slash(self, client, app_tree):
        r = client.get("/app", headers=DEV_HEADERS)
        assert r.status_code == 200
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
        assert r.headers["cross-origin-embedder-policy"] == "require-corp"

    def test_app_wasm_and_data_mime(self, client, app_tree):
        rw = client.get("/app/billet.wasm", headers=DEV_HEADERS)
        assert rw.status_code == 200
        assert rw.headers["content-type"].startswith("application/wasm")
        assert rw.headers["cross-origin-opener-policy"] == "same-origin"
        assert rw.headers["cross-origin-embedder-policy"] == "require-corp"
        # Unhashed bundle files must be no-cache so js/wasm never skew across deploys.
        assert rw.headers.get("cache-control") == "no-cache"

        rd = client.get("/app/billet.data", headers=DEV_HEADERS)
        assert rd.status_code == 200
        assert "octet-stream" in (rd.headers.get("content-type") or "")
        assert rd.headers["cross-origin-embedder-policy"] == "require-corp"
        assert rd.content == b"PACK"

    def test_app_traversal_rejected(self, client, app_tree):
        r = client.get("/app/../config.py", headers=DEV_HEADERS)
        # Starlette may normalize before route match → 404; our clamp → 400.
        assert r.status_code in (400, 404)


class TestPerfClient:
    def test_perf_requires_auth(self, client, traces_dir):
        r = client.post(
            "/perf/client",
            json={"boot_ms": 1, "fps": 60, "rpc_ms": 2, "ua": "t"},
        )
        assert r.status_code in (401, 403)

    def test_perf_appends_line(self, client, traces_dir):
        body = {"boot_ms": 1234.5, "fps": 58.0, "rpc_ms": 12.0, "ua": "TestUA/1.0"}
        r = client.post("/perf/client", headers=DEV_HEADERS, json=body)
        assert r.status_code == 200
        assert r.json().get("ok") is True

        log_path = traces_dir / "client.jsonl"
        assert log_path.is_file()
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["email"] == "demo@example.com"
        assert rec["boot_ms"] == 1234.5
        assert rec["fps"] == 58.0
        assert rec["rpc_ms"] == 12.0
        assert rec["ua"] == "TestUA/1.0"
        assert isinstance(rec["ts"], (int, float))

    def test_perf_oversized_rejected(self, client, traces_dir):
        # >4 KiB payload — Content-Length / body both over the hard cap.
        fat = {"boot_ms": 1, "fps": 1, "rpc_ms": 1, "ua": "x" * 5000}
        r = client.post("/perf/client", headers=DEV_HEADERS, json=fat)
        assert r.status_code == 413
        log_path = traces_dir / "client.jsonl"
        assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""
