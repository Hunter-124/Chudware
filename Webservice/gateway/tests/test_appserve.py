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

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod  # noqa: E402
from gateway.config import settings  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


@pytest.fixture()
def dev_account(pg, monkeypatch):
    """DEV_HEADERS' identity, active in the account store.

    AuthMiddleware authorizes from the store, so an unseeded dev identity 403s
    before any route below is reached. Requested only by tests whose subject is
    the served response — the two anonymous tests must stay unseeded, or a
    regression that stopped requiring auth at all would still look green.
    """
    monkeypatch.setattr(settings, "dev_mode", True)
    accounts.ensure_account("demo@example.com", display="Demo User")
    accounts.set_status("demo@example.com", "active", decided_by="test")


@pytest.fixture()
def app_tree(tmp_path, monkeypatch):
    """Point settings.app_dir at a tiny fake WASM client tree."""
    root = tmp_path / "app"
    root.mkdir()
    (root / "index.html").write_text(
        '<!doctype html><title>chudware</title><script src="Chudware.js"></script>\n',
        encoding="utf-8",
    )
    (root / "Chudware.js").write_text("/* wasm glue */\n", encoding="utf-8")
    (root / "chudware.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (root / "chudware.data").write_bytes(b"PACK")
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

    def test_app_index_coop_coep_and_cache(self, client, app_tree, dev_account):
        r = client.get("/app/", headers=DEV_HEADERS)
        assert r.status_code == 200
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
        assert r.headers["cross-origin-embedder-policy"] == "require-corp"
        assert r.headers.get("cache-control") == "no-cache"
        assert "text/html" in (r.headers.get("content-type") or "")
        assert "chudware" in r.text
        assert r.headers["vary"].lower() == "accept-encoding"

    def test_desktop_plan_gets_upgrade_page_while_studio_opens_app(
        self, client, app_tree, dev_account
    ):
        accounts.set_plan("demo@example.com", "desktop")
        denied = client.get("/app/", headers=DEV_HEADERS)
        assert denied.status_code == 403
        assert "does not include the cloud workspace" in denied.text
        assert denied.headers["cache-control"] == "no-store"

        accounts.set_plan("demo@example.com", "studio")
        allowed = client.get("/app/", headers=DEV_HEADERS)
        assert allowed.status_code == 200
        assert "chudware" in allowed.text

    def test_app_csp_permits_wasm_and_blob_workers(self, client, app_tree, dev_account):
        """The site-wide CSP in app.py is fatal for the Emscripten client.

        `script-src 'self'` alone makes Chrome refuse WebAssembly.instantiate,
        and Chudware.js spawns a worker from a createObjectURL blob which
        worker-src (falling back to default-src) would block. Both broke the
        whole /app client in production on 2026-07-28 with no server error —
        every asset returned 200 and the page simply never started.
        """
        for path in ("/app/", "/app/Chudware.js"):
            r = client.get(path, headers=DEV_HEADERS)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            csp = r.headers.get("content-security-policy", "")
            assert "'wasm-unsafe-eval'" in csp, f"{path} would break wasm: {csp!r}"
            assert "worker-src 'self' blob:" in csp, f"{path} blocks blob worker: {csp!r}"
            # Still tight where it matters — wasm-unsafe-eval must not have been
            # waved through as a blanket 'unsafe-eval'.
            assert "object-src 'none'" in csp
            assert "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", "")

    def test_app_bare_path_redirects_to_slash_and_relative_bundle_resolves(
        self, client, app_tree, dev_account
    ):
        redirect = client.get("/app", headers=DEV_HEADERS, follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/app/"
        assert redirect.headers["cross-origin-opener-policy"] == "same-origin"
        assert redirect.headers["cross-origin-embedder-policy"] == "require-corp"
        assert "content-security-policy" in redirect.headers

        index = client.get("/app", headers=DEV_HEADERS)
        assert index.status_code == 200
        assert index.url.path == "/app/"
        assert index.history[0].headers["location"] == "/app/"
        assert 'src="Chudware.js"' in index.text

        asset = client.get("/app/Chudware.js", headers=DEV_HEADERS)
        assert asset.status_code == 200
        assert asset.headers["cross-origin-opener-policy"] == "same-origin"
        assert asset.text == "/* wasm glue */\n"

    def test_app_wasm_and_data_mime(self, client, app_tree, dev_account):
        rw = client.get("/app/chudware.wasm", headers=DEV_HEADERS)
        assert rw.status_code == 200
        assert rw.headers["content-type"].startswith("application/wasm")
        assert rw.headers["cross-origin-opener-policy"] == "same-origin"
        assert rw.headers["cross-origin-embedder-policy"] == "require-corp"
        # Unhashed bundle files must be no-cache so js/wasm never skew across deploys.
        assert rw.headers.get("cache-control") == "no-cache"
        assert rw.headers["vary"].lower() == "accept-encoding"

        rd = client.get("/app/chudware.data", headers=DEV_HEADERS)
        assert rd.status_code == 200
        assert "octet-stream" in (rd.headers.get("content-type") or "")
        assert rd.headers["cross-origin-embedder-policy"] == "require-corp"
        assert rd.content == b"PACK"

    def test_app_traversal_rejected(self, client, app_tree, dev_account):
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

    def test_perf_appends_line(self, client, traces_dir, dev_account):
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

    def test_perf_oversized_rejected(self, client, traces_dir, dev_account):
        # >4 KiB payload — Content-Length / body both over the hard cap.
        fat = {"boot_ms": 1, "fps": 1, "rpc_ms": 1, "ua": "x" * 5000}
        r = client.post("/perf/client", headers=DEV_HEADERS, json=fat)
        assert r.status_code == 413
        log_path = traces_dir / "client.jsonl"
        assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""

class TestContentEncoding:
    """Precompressed .br/.gz variant negotiation (appserve._negotiate + HTTP)."""

    def _req(self, accept):
        import types

        return types.SimpleNamespace(headers={"accept-encoding": accept})

    def test_prefers_brotli_over_gzip(self, app_tree):
        from gateway import appserve

        target = app_tree / "chudware.wasm"
        (app_tree / "chudware.wasm.br").write_bytes(b"BR")
        (app_tree / "chudware.wasm.gz").write_bytes(b"GZ")
        served, enc = appserve._negotiate(target, self._req("gzip, br"))
        assert enc == "br"
        assert served.name == "chudware.wasm.br"

    def test_gzip_when_brotli_not_accepted(self, app_tree):
        from gateway import appserve

        target = app_tree / "chudware.wasm"
        (app_tree / "chudware.wasm.gz").write_bytes(b"GZ")
        served, enc = appserve._negotiate(target, self._req("gzip, deflate"))
        assert enc == "gzip"
        assert served.name == "chudware.wasm.gz"

    def test_raw_when_no_variant(self, app_tree):
        from gateway import appserve

        target = app_tree / "chudware.wasm"
        served, enc = appserve._negotiate(target, self._req("br, gzip"))
        assert enc is None
        assert served == target

    def test_raw_when_encoding_not_accepted(self, app_tree):
        from gateway import appserve

        target = app_tree / "chudware.wasm"
        (app_tree / "chudware.wasm.br").write_bytes(b"BR")
        served, enc = appserve._negotiate(target, self._req("identity"))
        assert enc is None
        assert served == target

    def test_stale_variant_ignored(self, app_tree):
        # A .br older than the original (restaged wasm, missed precompress) MUST fall
        # back to raw — never serve outdated compressed bytes (the js/wasm-skew hazard).
        import os

        from gateway import appserve

        target = app_tree / "chudware.wasm"
        variant = app_tree / "chudware.wasm.br"
        variant.write_bytes(b"BR")
        old = target.stat().st_mtime - 100
        os.utime(variant, (old, old))
        served, enc = appserve._negotiate(target, self._req("br"))
        assert enc is None
        assert served == target

    def test_http_serves_gzip_with_headers(self, client, app_tree, dev_account):
        # End-to-end: stdlib-gzip variant, fresh mtime → server sets Content-Encoding
        # + Vary and httpx transparently decodes back to the original bytes.
        import gzip as _gz

        raw = (app_tree / "index.html").read_bytes()
        (app_tree / "index.html.gz").write_bytes(_gz.compress(raw, 9))
        r = client.get("/app/", headers={**DEV_HEADERS, "Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip"
        assert "accept-encoding" in (r.headers.get("vary", "").lower())
        assert r.headers.get("cache-control") == "no-cache"  # policy keys on original suffix
        identity = client.get(
            "/app/", headers={**DEV_HEADERS, "Accept-Encoding": "identity"}
        )
        assert identity.status_code == 200
        assert "content-encoding" not in identity.headers
        assert identity.headers["vary"].lower() == "accept-encoding"
        assert identity.content == raw
        assert "chudware" in r.text  # decoded transparently
