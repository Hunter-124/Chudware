from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "cf_provision.py"
SPEC = importlib.util.spec_from_file_location("cf_provision", SCRIPT)
assert SPEC and SPEC.loader
cf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cf)


def test_provision_sets_portal_staging_pages_and_one_admin_access_app(monkeypatch):
    calls = []
    env = {}
    tunnel_env = {}

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/cfd_tunnel?is_deleted=false"):
            return [{"id": "tunnel-1", "name": cf.TUNNEL_NAME}]
        if path == "/zones":
            return [{"id": "zone-1", "name": "chudware.com"}]
        if "/dns_records?name=" in path:
            return []
        if path.endswith("/access/apps") and method == "GET":
            return [{"id": "access-1", "name": cf.APP_NAME, "domain": "chudware.com"}]
        if path.endswith("/access/apps/access-1") and method == "PUT":
            return {"id": "access-1", "aud": "one-aud", **body}
        if path.endswith("/access/apps/access-1/policies") and method == "GET":
            return []
        if path.endswith("/access/organizations"):
            return {"auth_domain": "https://team.cloudflareaccess.com"}
        if path.endswith("/cfd_tunnel/tunnel-1/token"):
            return "tunnel-token"
        return {}

    monkeypatch.setattr(cf, "api", fake_api)
    monkeypatch.setattr(cf, "upsert_env", lambda updates: env.update(updates))
    monkeypatch.setattr(cf, "write_tunnel_env", lambda token: tunnel_env.update(TUNNEL_TOKEN=token))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--emails", "operator@example.com"])
    cf.main()

    config = next(body for method, path, body in calls if path.endswith("/configurations"))
    assert config["config"]["ingress"] == [
        {"hostname": "app.chudware.com", "service": "http://127.0.0.1:8300"},
        {"hostname": "staging.chudware.com", "service": "http://127.0.0.1:8310"},
        {"service": "http_status:404"},
    ]

    cname_bodies = [body for method, path, body in calls if method == "POST" and "/dns_records" in path]
    assert {(body["name"], body["content"]) for body in cname_bodies} == {
        ("app.chudware.com", "tunnel-1.cfargotunnel.com"),
        ("staging.chudware.com", "tunnel-1.cfargotunnel.com"),
        ("chudware.com", "chudware-site.pages.dev"),
        ("www.chudware.com", "chudware-site.pages.dev"),
    }

    app_updates = [body for method, path, body in calls if path.endswith("/access/apps/access-1")]
    assert len(app_updates) == 1
    assert app_updates[0]["domain"] == "app.chudware.com/admin*"
    assert app_updates[0]["self_hosted_domains"] == ["app.chudware.com/admin*"]
    assert not any(method == "POST" and path.endswith("/access/apps") for method, path, _ in calls)
    assert env["CHUDWARE_WEB_CF_AUD"] == "one-aud"
    assert env["CHUDWARE_WEB_PUBLIC_BASE_URL"] == "https://app.chudware.com"
    assert "TUNNEL_TOKEN" not in env
    assert tunnel_env == {"TUNNEL_TOKEN": "tunnel-token"}


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


def test_units_fail_closed_on_dev_mode_and_do_not_receive_tunnel_token():
    deploy = SCRIPT.parent
    for name in ("chudware-gateway.service", "chudware-gateway-staging.service"):
        unit = (deploy / name).read_text()
        assert "ExecStartPre=" in unit and "CHUDWARE_WEB_DEV must be disabled" in unit
        assert "TUNNEL_TOKEN" not in unit
        assert "cloudflared.env" not in unit
        assert "NoNewPrivileges=yes" in unit
        assert "ProtectSystem=strict" in unit
        assert "PrivateDevices=yes" in unit


def test_deploy_scripts_never_put_password_dsn_in_postgres_client_argv():
    deploy = SCRIPT.parent
    update = (deploy / "update.sh").read_text()
    install = (deploy / "install_webd.sh").read_text()
    backup = (deploy / "backup.sh").read_text()
    assert 'pg_dump --format=custom --file="$tmp" "$DB_URL"' not in update
    assert 'psql "$DB_URL"' not in update
    assert 'psql "$HEALTH_DB_URL"' not in install
    for script in (update, install, backup):
        assert 'PGPASSFILE="$pgpass"' in script
        assert "--no-password" in script


def test_setup_and_launch_profile_default_to_least_privilege():
    deploy = SCRIPT.parent
    setup = (deploy / "setup.sh").read_text()
    harden = (deploy / "harden_host.sh").read_text()
    assert "TUNNEL_TOKEN=" not in setup.split('cat >"${ENV_FILE}"', 1)[1].split("EOF", 1)[0]
    assert 'cloudflared.env' in setup
    assert 'SAFE_ITEMS=" sudo firewall tunnel updates keys backups gateway "' in harden
