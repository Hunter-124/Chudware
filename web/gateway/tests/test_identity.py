"""Single-operator identity, Host allowlist, same-origin guard and response headers."""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from gateway import identity
from gateway.config import Settings


def test_healthz_is_bare_liveness(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_security_headers_on_gateway_responses(client):
    headers = client.get("/healthz").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["referrer-policy"] == "same-origin"
    assert "object-src 'none'" in headers["content-security-policy"]


def test_api_docs_are_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_unknown_host_header_is_refused(client):
    """A DNS-rebinding page reaches the loopback listener under its own name."""
    r = client.get("/healthz", headers={"Host": "attacker.example"})
    assert r.status_code == 400
    assert client.get("/healthz", headers={"Host": "127.0.0.1:8300"}).status_code == 200


def test_cross_origin_write_is_refused_before_the_handler(client, workspace):
    r = client.post("/workspace/mkdir", json={"path": "planted"},
                    headers={"Origin": "https://attacker.example"})
    assert r.status_code == 403
    assert not (workspace / "planted").exists()


def test_cross_origin_multipart_upload_is_refused(client, workspace):
    r = client.post("/workspace/upload", files={"file": ("x.step", b"data")},
                    data={"dir": ""}, headers={"Origin": "http://localhost:9999"})
    assert r.status_code == 403
    assert not (workspace / "x.step").exists()


@pytest.mark.parametrize("origin", ["null", "file://", "http://testserver/path"])
def test_opaque_or_malformed_origin_is_refused(client, origin):
    r = client.post("/workspace/mkdir", json={"path": "x"}, headers={"Origin": origin})
    assert r.status_code == 403


def test_same_origin_and_originless_writes_pass(client, workspace):
    same = client.post("/workspace/mkdir", json={"path": "a"},
                       headers={"Origin": "http://testserver"})
    assert same.status_code == 200
    plain = client.post("/workspace/mkdir", json={"path": "b"})
    assert plain.status_code == 200
    assert (workspace / "a").is_dir() and (workspace / "b").is_dir()


def test_cross_origin_reads_are_not_blocked(client, workspace):
    # Reads cannot be observed cross-origin without CORS, which is never sent.
    r = client.get("/workspace/list", headers={"Origin": "https://attacker.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_cross_origin_engine_websocket_is_refused(client):
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/engine", headers={"Origin": "https://attacker.example"}):
            pass
    assert refused.value.code == 4403


def test_same_origin_helper():
    assert identity.same_origin("http://127.0.0.1:8300", "127.0.0.1:8300")
    assert identity.same_origin("https://cad.example.org", "CAD.example.org")
    assert not identity.same_origin("https://cad.example.org", "cad.example.org:8443")
    assert not identity.same_origin("chrome-extension://abc", "abc")


def test_operator_names_the_workspace(monkeypatch):
    monkeypatch.setattr(identity.settings, "operator", "alice")
    assert identity.current_operator().workspace_key == "user:alice"


@pytest.mark.parametrize("name", ["", "a b", "../x", "x" * 65, "al.ice"])
def test_invalid_operator_name_fails_at_startup(monkeypatch, name):
    monkeypatch.setenv("CHUDWARE_WEB_OPERATOR", name)
    if name == "":
        # Empty means "use the default".
        assert Settings().operator == "operator"
        return
    with pytest.raises(ValueError, match="CHUDWARE_WEB_OPERATOR"):
        Settings()


def test_settings_defaults_are_local(monkeypatch, tmp_path):
    monkeypatch.delenv("CHUDWARE_WEB_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("CHUDWARE_WEB_HOME", str(tmp_path))
    settings = Settings()
    assert settings.allowed_hosts == ["localhost", "127.0.0.1"]
    assert settings.workspace_root == tmp_path.resolve() / "workspace"
    assert settings.cells_root == tmp_path.resolve() / "cells"
    assert settings.app_dir.parts[-3:] == ("host", "build-wasm", "app")
