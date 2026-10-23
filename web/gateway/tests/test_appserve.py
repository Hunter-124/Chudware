"""WASM /app static serving: COOP/COEP, CSP, cache policy and precompressed variants."""

from __future__ import annotations

import gzip
import os
import types

from gateway import appserve


def _request(accept):
    return types.SimpleNamespace(headers={"accept-encoding": accept})


class TestAppServe:
    def test_app_index_coop_coep_and_cache(self, client, app_tree):
        r = client.get("/app/")
        assert r.status_code == 200
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
        assert r.headers["cross-origin-embedder-policy"] == "require-corp"
        assert r.headers.get("cache-control") == "no-cache"
        assert "text/html" in (r.headers.get("content-type") or "")
        assert "chudware" in r.text
        assert r.headers["vary"].lower() == "accept-encoding"

    def test_root_redirects_to_app(self, client, app_tree):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/app/"

    def test_app_csp_permits_wasm_and_blob_workers(self, client, app_tree):
        """A bare `script-src 'self'` makes Chrome refuse WebAssembly.instantiate,
        and Chudware.js spawns a blob: worker. Both must be admitted for /app,
        without falling back to a blanket 'unsafe-eval'."""
        for path in ("/app/", "/app/Chudware.js"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            csp = r.headers.get("content-security-policy", "")
            assert "'wasm-unsafe-eval'" in csp, f"{path} would break wasm: {csp!r}"
            assert "worker-src 'self' blob:" in csp, f"{path} blocks blob worker: {csp!r}"
            assert "object-src 'none'" in csp
            assert "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", "")

    def test_inline_bootstrap_scripts_are_hashed_into_csp(self, client, app_tree):
        (app_tree / "index.html").write_text(
            "<!doctype html><script>window.boot = 1;</script><script src=\"Chudware.js\"></script>\n",
            encoding="utf-8",
        )
        csp = client.get("/app/").headers["content-security-policy"]
        assert "'sha256-" in csp
        assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]

    def test_app_bare_path_redirects_to_slash_and_relative_bundle_resolves(self, client, app_tree):
        redirect = client.get("/app", follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/app/"
        assert redirect.headers["cross-origin-opener-policy"] == "same-origin"
        assert redirect.headers["cross-origin-embedder-policy"] == "require-corp"
        assert "content-security-policy" in redirect.headers

        index = client.get("/app")
        assert index.status_code == 200
        assert index.url.path == "/app/"
        assert 'src="Chudware.js"' in index.text

        asset = client.get("/app/Chudware.js")
        assert asset.status_code == 200
        assert asset.headers["cross-origin-opener-policy"] == "same-origin"
        assert asset.text == "/* wasm glue */\n"

    def test_app_wasm_and_data_mime(self, client, app_tree):
        rw = client.get("/app/chudware.wasm")
        assert rw.status_code == 200
        assert rw.headers["content-type"].startswith("application/wasm")
        assert rw.headers["cross-origin-embedder-policy"] == "require-corp"
        # Unhashed bundle files must be no-cache so js/wasm never skew across restages.
        assert rw.headers.get("cache-control") == "no-cache"

        rd = client.get("/app/chudware.data")
        assert rd.status_code == 200
        assert "octet-stream" in (rd.headers.get("content-type") or "")
        assert rd.content == b"PACK"

    def test_app_traversal_and_missing_rejected(self, client, app_tree):
        # Starlette may normalize before route match (404); the clamp answers 400.
        assert client.get("/app/../config.py").status_code in (400, 404)
        assert client.get("/app/missing.js").status_code == 404

    def test_missing_bundle_is_404_not_500(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(appserve.settings, "app_dir", tmp_path / "never-built")
        assert client.get("/app/").status_code == 404


class TestContentEncoding:
    """Precompressed .br/.gz variant negotiation (appserve._negotiate + HTTP)."""

    def test_prefers_brotli_over_gzip(self, app_tree):
        target = app_tree / "chudware.wasm"
        (app_tree / "chudware.wasm.br").write_bytes(b"BR")
        (app_tree / "chudware.wasm.gz").write_bytes(b"GZ")
        served, enc = appserve._negotiate(target, _request("gzip, br"))
        assert enc == "br"
        assert served.name == "chudware.wasm.br"

    def test_gzip_when_brotli_not_accepted(self, app_tree):
        target = app_tree / "chudware.wasm"
        (app_tree / "chudware.wasm.gz").write_bytes(b"GZ")
        served, enc = appserve._negotiate(target, _request("gzip, deflate"))
        assert enc == "gzip"
        assert served.name == "chudware.wasm.gz"

    def test_raw_when_no_variant_or_encoding_not_accepted(self, app_tree):
        target = app_tree / "chudware.wasm"
        assert appserve._negotiate(target, _request("br, gzip")) == (target, None)
        (app_tree / "chudware.wasm.br").write_bytes(b"BR")
        assert appserve._negotiate(target, _request("identity")) == (target, None)

    def test_stale_variant_ignored(self, app_tree):
        # A .br older than its original (restaged wasm, missed precompress) MUST fall
        # back to raw — never serve outdated compressed bytes (the js/wasm-skew hazard).
        target = app_tree / "chudware.wasm"
        variant = app_tree / "chudware.wasm.br"
        variant.write_bytes(b"BR")
        old = target.stat().st_mtime - 100
        os.utime(variant, (old, old))
        assert appserve._negotiate(target, _request("br")) == (target, None)

    def test_http_serves_gzip_with_headers(self, client, app_tree):
        raw = (app_tree / "index.html").read_bytes()
        (app_tree / "index.html.gz").write_bytes(gzip.compress(raw, 9))
        r = client.get("/app/", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip"
        assert "accept-encoding" in r.headers.get("vary", "").lower()
        assert r.headers.get("cache-control") == "no-cache"  # policy keys on original suffix
        assert "chudware" in r.text  # decoded transparently
        identity = client.get("/app/", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in identity.headers
        assert identity.content == raw
