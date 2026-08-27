from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "cf_provision.py"
SPEC = importlib.util.spec_from_file_location("cf_provision", SCRIPT)
assert SPEC and SPEC.loader
cf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cf)


def test_provision_sets_portal_staging_pages_and_no_access_application(monkeypatch):
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
            # Present on the account and deliberately ignored: consolidating or
            # deleting the leftover /admin* application is a Zero Trust dashboard
            # decision, not something a DNS/tunnel run gets to make.
            return [{"id": "access-1", "name": "Chudware Web", "domain": "chudware.com"}]
        if path.endswith("/cfd_tunnel/tunnel-1/token"):
            return "tunnel-token"
        return {}

    monkeypatch.setattr(cf, "api", fake_api)
    monkeypatch.setattr(cf, "upsert_env", lambda updates: env.update(updates))
    monkeypatch.setattr(cf, "write_tunnel_env", lambda token: tunnel_env.update(TUNNEL_TOKEN=token))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
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

    # /admin is gated by the gateway's session cookie plus accounts.role now, so
    # this script must not read, rewrite or create an Access application — and
    # must emit no AUD, since fronting /admin* with Access is what made the
    # operator meet Cloudflare's login page instead of the gateway's.
    assert not any("/access/" in path for _, path, _ in calls)
    assert "CHUDWARE_WEB_CF_AUD" not in env
    assert "CHUDWARE_WEB_CF_TEAM_DOMAIN" not in env
    assert env["CHUDWARE_WEB_PUBLIC_BASE_URL"] == "https://app.chudware.com"
    assert "TUNNEL_TOKEN" not in env
    assert tunnel_env == {"TUNNEL_TOKEN": "tunnel-token"}


def test_module_carries_no_admin_access_application_logic():
    """The deletion, pinned by symbol rather than by grepping the source, which
    still discusses the removed application in prose. Re-adding an Access
    application over /admin* would put Cloudflare's login page back in front of
    the operator's own account."""
    for gone in ("APP_NAME", "ACCESS_DOMAIN", "ACCESS_DOMAINS", "_include_list"):
        assert not hasattr(cf, gone), f"{gone} came back: /admin* Access app re-added"


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

    prod = (deploy / "chudware-gateway.service").read_text()
    assert "%h/chudware-releases" in prod, (
        "release upload/publish runs inside the hardened gateway unit; without this "
        "ReadWritePaths entry ProtectHome=read-only turns a valid incoming/ write into EROFS"
    )


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
    # The launch profile applies every "safe" item unattended, and sshd is the one item that can
    # lock the operator out of the box, so it must never appear there. Pinning the exact SAFE_ITEMS
    # literal instead meant each newly added safe item (`entitlement`, most recently) failed this
    # test while the invariant it exists to protect was never in doubt.
    declared = re.search(r'^SAFE_ITEMS="([^"]*)"', harden, re.M)
    assert declared, "harden_host.sh no longer declares SAFE_ITEMS"
    safe_items = set(declared.group(1).split())
    assert "sshd" not in safe_items
    assert {"sudo", "firewall", "tunnel", "updates", "keys", "backups", "gateway"} <= safe_items
