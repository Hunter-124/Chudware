#!/usr/bin/env python3
"""Idempotently describe the production Chudware Cloudflare topology.

The tunnel serves only the authenticated portal and staging gateway. The public
marketing apex/www live on Cloudflare Pages. One Access application protects
``app.chudware.com/admin*`` and therefore emits the gateway's single accepted
AUD; this script never creates a second Access application.

IMPORTANT: deploy and verify gateway session-cookie auth before running this
script to narrow Access. See deploy/RUNBOOK.md for the required ordering.

Secrets: reads $CF_API_TOKEN or the gitignored Cloudflare-info.txt. Gateway
settings are written to ~/chudware-web-data/gateway.env; the tunnel credential
is written only to owner-readable cloudflared.env.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
ACCOUNT_ID = "9a800a5f674233fe127cdb44b395ebfa"
TUNNEL_NAME = "chudware-web"
APP_NAME = "Chudware Web"
PORTAL_HOST = "app.chudware.com"
STAGING_HOST = "staging.chudware.com"
ACCESS_DOMAIN = f"{PORTAL_HOST}/admin*"
PRODUCTION_SERVICE = "http://127.0.0.1:8300"
STAGING_SERVICE = "http://127.0.0.1:8310"
PAGES_PROJECT = "chudware-site"
PAGES_HOSTS = ("chudware.com", "www.chudware.com")
ENV_FILE = Path.home() / "chudware-web-data" / "gateway.env"
TUNNEL_ENV_FILE = Path.home() / "chudware-web-data" / "cloudflared.env"


def token() -> str:
    if os.environ.get("CF_API_TOKEN"):
        return os.environ["CF_API_TOKEN"]
    info = Path(__file__).resolve().parents[1] / "Cloudflare-info.txt"
    if info.exists():
        if info.stat().st_mode & 0o077:
            sys.exit(f"{info} is group/world-readable — run: chmod 600 {info}")
        match = re.search(r"^(cfat_\S+)$", info.read_text(), re.M)
        if match:
            return match.group(1)
    sys.exit("no CF_API_TOKEN and no token in Webservice/Cloudflare-info.txt")


def api(method: str, path: str, body: dict | None = None) -> dict | list:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as response:
            out = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        sys.exit(f"cloudflare API error on {method} {path}: HTTP {exc.code} {detail}")
    if not out.get("success"):
        sys.exit(f"cloudflare API error on {method} {path}: {out.get('errors')}")
    return out["result"]


def find(items: list[dict], key: str, value: str) -> dict | None:
    return next((item for item in items if item.get(key) == value), None)


ACCESS_DOMAINS = ["ua.edu", "crimson.ua.edu"]


def _include_list(emails: list[str]) -> list[dict]:
    result: list[dict] = [{"email": {"email": email}} for email in emails]
    result += [{"email_domain": {"domain": domain}} for domain in ACCESS_DOMAINS]
    return result


def _atomic_write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def upsert_env(updates: dict[str, str]) -> None:
    lines: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key != "TUNNEL_TOKEN":
                    lines[key] = value
    lines.update(updates)
    _atomic_write_env(ENV_FILE, lines)


def write_tunnel_env(tunnel_token: str) -> None:
    _atomic_write_env(TUNNEL_ENV_FILE, {"TUNNEL_TOKEN": tunnel_token})


def _zone(zones: list[dict], hostname: str) -> dict:
    zone_name = ".".join(hostname.split(".")[-2:])
    zone = find(zones, "name", zone_name)
    if zone is None:
        sys.exit(f"zone {zone_name} not on this account")
    return zone


def _upsert_cname(zones: list[dict], hostname: str, target: str) -> None:
    zone = _zone(zones, hostname)
    records = api("GET", f"/zones/{zone['id']}/dns_records?name={hostname}")
    assert isinstance(records, list)
    body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True}
    if records:
        api("PUT", f"/zones/{zone['id']}/dns_records/{records[0]['id']}", body)
    else:
        api("POST", f"/zones/{zone['id']}/dns_records", body)
    print(f"dns: {hostname} CNAME {target} (proxied)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--emails", required=True,
        help="comma-separated operator allowlist for app.chudware.com/admin*",
    )
    parser.add_argument("--pages-project", default=PAGES_PROJECT)
    args = parser.parse_args()
    emails = [email.strip().lower() for email in args.emails.split(",") if email.strip()]
    if not emails:
        parser.error("--emails parsed to an empty allowlist")

    tunnels = api("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel?is_deleted=false")
    assert isinstance(tunnels, list)
    tunnel = find(tunnels, "name", TUNNEL_NAME) or api(
        "POST", f"/accounts/{ACCOUNT_ID}/cfd_tunnel",
        {"name": TUNNEL_NAME, "config_src": "cloudflare"},
    )
    assert isinstance(tunnel, dict)
    tunnel_id = tunnel["id"]
    print(f"tunnel {TUNNEL_NAME}: {tunnel_id}")

    ingress = [
        {"hostname": PORTAL_HOST, "service": PRODUCTION_SERVICE},
        {"hostname": STAGING_HOST, "service": STAGING_SERVICE},
        {"service": "http_status:404"},
    ]
    api("PUT", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/configurations", {"config": {"ingress": ingress}})
    print(f"ingress: {PORTAL_HOST} -> {PRODUCTION_SERVICE}")
    print(f"ingress: {STAGING_HOST} -> {STAGING_SERVICE}")
    print("ingress: catch-all -> 404")

    zones = api("GET", "/zones")
    assert isinstance(zones, list)
    tunnel_target = f"{tunnel_id}.cfargotunnel.com"
    _upsert_cname(zones, PORTAL_HOST, tunnel_target)
    _upsert_cname(zones, STAGING_HOST, tunnel_target)
    pages_target = f"{args.pages_project}.pages.dev"
    for hostname in PAGES_HOSTS:
        _upsert_cname(zones, hostname, pages_target)

    apps = api("GET", f"/accounts/{ACCOUNT_ID}/access/apps")
    assert isinstance(apps, list)
    candidates = [
        item for item in apps
        if item.get("name") == APP_NAME
        or item.get("domain") in {"chudware.com", PORTAL_HOST, ACCESS_DOMAIN}
    ]
    if len(candidates) > 1:
        sys.exit(
            "multiple Chudware Access applications exist; consolidate them in Zero Trust "
            "before provisioning so the gateway continues to validate exactly one AUD"
        )
    app_body = {
        "name": APP_NAME,
        "type": "self_hosted",
        "domain": ACCESS_DOMAIN,
        "self_hosted_domains": [ACCESS_DOMAIN],
        "session_duration": "24h",
        "skip_interstitial": True,
        "auto_redirect_to_identity": False,
    }
    if candidates:
        app = api("PUT", f"/accounts/{ACCOUNT_ID}/access/apps/{candidates[0]['id']}", app_body)
    else:
        app = api("POST", f"/accounts/{ACCOUNT_ID}/access/apps", app_body)
    assert isinstance(app, dict)
    print(f"access app: {ACCESS_DOMAIN} id={app['id']} aud={app['aud']}")

    policies = api("GET", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies")
    assert isinstance(policies, list)
    allow_policies = [policy for policy in policies if policy.get("decision") == "allow"]
    policy = find(policies, "name", "chudware-admins") or (
        allow_policies[0] if len(allow_policies) == 1 else None
    )
    policy_body = {
        "name": policy["name"] if policy else "chudware-admins",
        "decision": "allow",
        "precedence": policy["precedence"] if policy else 1,
        "include": _include_list(emails),
    }
    if policy:
        api("PUT", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies/{policy['id']}", policy_body)
    else:
        api("POST", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies", policy_body)
    print(f"policy {policy_body['name']!r}: {len(emails)} operator email(s)")

    organizations = api("GET", f"/accounts/{ACCOUNT_ID}/access/organizations")
    assert isinstance(organizations, dict)
    tunnel_token = api("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/token")
    assert isinstance(tunnel_token, str)
    write_tunnel_env(tunnel_token)
    upsert_env({
        "CHUDWARE_WEB_CF_TEAM_DOMAIN": organizations["auth_domain"],
        "CHUDWARE_WEB_CF_AUD": app["aud"],
        "CHUDWARE_WEB_PUBLIC_BASE_URL": f"https://{PORTAL_HOST}",
    })
    print(f"gateway env written: {ENV_FILE} (team={organizations['auth_domain']})")
    print(f"tunnel env written: {TUNNEL_ENV_FILE}")
    print("Access is narrowed only after session-cookie auth verification; see RUNBOOK.md.")


if __name__ == "__main__":
    main()
