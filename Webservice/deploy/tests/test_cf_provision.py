from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cf_provision.py"
SPEC = importlib.util.spec_from_file_location("cf_provision", SCRIPT)
assert SPEC and SPEC.loader
cf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cf)


@pytest.mark.parametrize("publish_staging", [False, True])
def test_provision_preserves_foreign_routes_and_publishes_staging_only_explicitly(monkeypatch, publish_staging):
    calls = []
    env = {}
    tunnel_env = {}
    foreign = {"hostname": "foreign.example.invalid", "service": "http://127.0.0.1:9001"}

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/cfd_tunnel?is_deleted=false"):
            return [{"id": "tunnel-1", "name": cf.TUNNEL_NAME}]
        if path.endswith("/configurations") and method == "GET":
            return {"config": {"ingress": [
                foreign,
                {"hostname": cf.STAGING_HOST, "service": cf.STAGING_SERVICE},
                {"service": "http_status:404"},
            ], "warp-routing": {"enabled": True}}}
        if path == "/zones":
            return [{"id": "zone-1", "name": "chudware.com"}]
        if "/dns_records?name=" in path:
            if path.endswith(cf.STAGING_HOST):
                return [
                    {"id": "owned-stage", "type": "CNAME", "content": "tunnel-1.cfargotunnel.com"},
                    {"id": "foreign-stage", "type": "TXT", "content": "keep"},
                ]
            return []
        if path.endswith("/cfd_tunnel/tunnel-1/token"):
            return "fixture-tunnel-token"
        return {}

    monkeypatch.setattr(cf, "api", fake_api)
    monkeypatch.setattr(cf, "upsert_env", lambda updates: env.update(updates))
    monkeypatch.setattr(cf, "write_tunnel_env", lambda token: tunnel_env.update(TUNNEL_TOKEN=token))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *(["--publish-staging"] if publish_staging else [])])
    cf.main()
    config = next(body["config"] for method, path, body in calls if method == "PUT" and path.endswith("/configurations"))
    assert foreign in config["ingress"]
    assert config["warp-routing"] == {"enabled": True}
    assert any(row.get("hostname") == cf.STAGING_HOST for row in config["ingress"]) is publish_staging
    assert config["ingress"][-1] == {"service": "http_status:404"}
    deleted = [path for method, path, _ in calls if method == "DELETE"]
    assert deleted == ([] if publish_staging else ["/zones/zone-1/dns_records/owned-stage"])
    assert not any("/access/" in path for _, path, _ in calls)
    assert "TUNNEL_TOKEN" not in env
    assert tunnel_env == {"TUNNEL_TOKEN": "fixture-tunnel-token"}




def test_env_writes_are_private_atomic_and_remove_legacy_tunnel_token(monkeypatch, tmp_path):
    gateway = tmp_path / "gateway.env"
    tunnel = tmp_path / "cloudflared.env"
    gateway.write_text("KEEP=value\nTUNNEL_TOKEN=legacy\n")
    monkeypatch.setattr(cf, "ENV_FILE", gateway)
    monkeypatch.setattr(cf, "TUNNEL_ENV_FILE", tunnel)

    cf.upsert_env({"NEW": "setting"})
    cf.write_tunnel_env("fresh-token")

    assert gateway.read_text() == "KEEP=value\nNEW=setting\n"
    assert tunnel.read_text() == "TUNNEL_TOKEN=fresh-token\n"
    assert gateway.stat().st_mode & 0o777 == 0o600
    assert tunnel.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


