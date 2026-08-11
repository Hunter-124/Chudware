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
