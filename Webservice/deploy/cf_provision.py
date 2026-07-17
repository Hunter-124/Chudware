#!/usr/bin/env python3
"""Idempotent Cloudflare provisioning for billet-web.

Creates/updates: the `billet-web` remotely-managed tunnel + ingress to the local
gateway, the DNS CNAME, the Zero Trust Access application + allow policy, and
writes TUNNEL token / team domain / AUD into the gateway env file.

Secrets: reads the API token from $CF_API_TOKEN or Webservice/Cloudflare-info.txt
(gitignored). Writes secrets ONLY to ~/billet-web-data/gateway.env (0600).

Usage: python3 cf_provision.py [--hostname cad.scootware.us] [--emails a@x,b@y]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
ACCOUNT_ID = "9a800a5f674233fe127cdb44b395ebfa"
TUNNEL_NAME = "billet-web"
APP_NAME = "Billet Web"
GATEWAY_SERVICE = "http://127.0.0.1:8300"
ENV_FILE = Path.home() / "billet-web-data" / "gateway.env"


def token() -> str:
    if os.environ.get("CF_API_TOKEN"):
        return os.environ["CF_API_TOKEN"]
    info = Path(__file__).resolve().parents[1] / "Cloudflare-info.txt"
    if info.exists():
        m = re.search(r"^(cfat_\S+)$", info.read_text(), re.M)
        if m:
            return m.group(1)
    sys.exit("no CF_API_TOKEN and no token in Webservice/Cloudflare-info.txt")


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        out = json.load(resp)
    if not out.get("success"):
        sys.exit(f"cloudflare API error on {method} {path}: {out.get('errors')}")
    return out["result"]


def find(items: list[dict], key: str, value: str) -> dict | None:
    return next((i for i in items if i.get(key) == value), None)


# Whole-domain auto-approval: anyone with an email at these domains is allowed through
# CF Access (as a standard user; the gateway assigns role="user", never admin). Keep in
# sync with AUTO_APPROVE_DOMAINS in Webservice/gateway/auth.py. CF email_domain is an
# EXACT match (no subdomain wildcard), so each domain must be listed explicitly.
ACCESS_DOMAINS = ["ua.edu", "crimson.ua.edu"]

# Additional hostnames served by the SAME tunnel and gated by the SAME Access app.
# One app => one AUD => the gateway's single BILLET_WEB_CF_AUD keeps validating; a
# separate per-domain app would mint a different AUD and break JWT validation. So new
# public domains go HERE (added to the app's self_hosted_domains), never as a new app.
EXTRA_HOSTS = ["chudware.com", "www.chudware.com"]


def _include_list(emails: list[str]) -> list[dict]:
    """Access policy include = explicit allowlisted emails + whole-domain rules."""
    inc: list[dict] = [{"email": {"email": e}} for e in emails]
    inc += [{"email_domain": {"domain": d}} for d in ACCESS_DOMAINS]
    return inc


def upsert_env(updates: dict[str, str]) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                lines[k] = v
    lines.update(updates)
    ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in lines.items()) + "\n")
    ENV_FILE.chmod(0o600)


def _users_yaml_emails() -> list[str]:
    """Read the gateway allowlist (single source of truth for people).

    Prefers the runtime file ($BILLET_WEB_USERS_FILE, or the data-root copy the
    /admin page edits) over the repo template.
    """
    candidates = [
        os.environ.get("BILLET_WEB_USERS_FILE"),
        str(Path.home() / "billet-web-data" / "users.yaml"),
        str(Path(__file__).resolve().parents[1] / "gateway" / "users.yaml"),
    ]
    users_file = next(Path(c) for c in candidates if c and Path(c).is_file())
    emails: list[str] = []
    for line in users_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("- email:") or line.startswith("email:"):
            emails.append(line.split(":", 1)[1].strip())
    return [e for e in emails if "@" in e and not e.endswith("example.com")]


def sync_users(host: str) -> None:
    """Push users.yaml emails into the Access allow policy (operator one-liner).

    Advisor T-WEB2 Q1: invite keys rejected; the dual-edit friction is solved
    here instead — edit users.yaml, run `cf_provision.py --sync-users`.
    """
    emails = _users_yaml_emails()
    if not emails:
        sys.exit("users.yaml has no real emails (example.com entries are skipped)")
    apps = api("GET", f"/accounts/{ACCOUNT_ID}/access/apps")
    app = find(apps, "domain", host)
    if app is None:
        sys.exit(f"no Access app for {host} — run full provisioning first")
    pols = api("GET", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies")
    pol_body = {
        "name": "billet-users", "decision": "allow", "precedence": 1,
        "include": _include_list(emails),
    }
    pol = find(pols, "name", "billet-users")
    if pol:
        api("PUT", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies/{pol['id']}", pol_body)
    else:
        api("POST", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies", pol_body)
    print(f"Access policy billet-users synced from users.yaml: {len(emails)} email(s)")
    for e in emails:
        print(f"  - {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hostname", default="cad.scootware.us")
    ap.add_argument("--emails", default="haedmonds69@gmail.com",
                    help="comma-separated allowlist for the Access policy")
    ap.add_argument("--sync-users", action="store_true",
                    help="only sync gateway/users.yaml emails into the Access policy")
    args = ap.parse_args()
    host = args.hostname
    zone_name = ".".join(host.split(".")[-2:])
    emails = [e.strip() for e in args.emails.split(",") if e.strip()]

    if args.sync_users:
        sync_users(host)
        return
    # 1. tunnel
    tunnels = api("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel?is_deleted=false")
    tun = find(tunnels, "name", TUNNEL_NAME) or api(
        "POST", f"/accounts/{ACCOUNT_ID}/cfd_tunnel",
        {"name": TUNNEL_NAME, "config_src": "cloudflare"})
    tid = tun["id"]
    print(f"tunnel {TUNNEL_NAME}: {tid}")

    # 2. ingress config
    api("PUT", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tid}/configurations", {
        "config": {"ingress":
            [{"hostname": h, "service": GATEWAY_SERVICE} for h in [host] + EXTRA_HOSTS]
            + [{"service": "http_status:404"}]}})
    print(f"ingress: {[host] + EXTRA_HOSTS} -> {GATEWAY_SERVICE}")

    # 3. DNS CNAME (proxied)
    zone = find(api("GET", "/zones"), "name", zone_name)
    if zone is None:
        sys.exit(f"zone {zone_name} not on this account")
    recs = api("GET", f"/zones/{zone['id']}/dns_records?name={host}")
    rec_body = {"type": "CNAME", "name": host,
                "content": f"{tid}.cfargotunnel.com", "proxied": True}
    if recs:
        api("PUT", f"/zones/{zone['id']}/dns_records/{recs[0]['id']}", rec_body)
    else:
        api("POST", f"/zones/{zone['id']}/dns_records", rec_body)
    print(f"dns: {host} CNAME {tid}.cfargotunnel.com (proxied)")
    # Extra hosts live in their own zones — resolve each and upsert a proxied CNAME.
    for eh in EXTRA_HOSTS:
        ez = find(api("GET", "/zones"), "name", ".".join(eh.split(".")[-2:]))
        if ez is None:
            print(f"  warn: zone for {eh} not on account — skipping DNS")
            continue
        erecs = api("GET", f"/zones/{ez['id']}/dns_records?name={eh}")
        ebody = {"type": "CNAME", "name": eh,
                 "content": f"{tid}.cfargotunnel.com", "proxied": True}
        if erecs:
            api("PUT", f"/zones/{ez['id']}/dns_records/{erecs[0]['id']}", ebody)
        else:
            api("POST", f"/zones/{ez['id']}/dns_records", ebody)
        print(f"dns: {eh} CNAME {tid}.cfargotunnel.com (proxied)")

    # 4. Access application
    apps = api("GET", f"/accounts/{ACCOUNT_ID}/access/apps")
    app_body = {
        "name": APP_NAME, "type": "self_hosted", "domain": host,
        # All domains on ONE app => ONE shared AUD (see EXTRA_HOSTS note above).
        "self_hosted_domains": [host] + EXTRA_HOSTS,
        "session_duration": "24h", "skip_interstitial": True,
        "auto_redirect_to_identity": False,
    }
    app = find(apps, "domain", host)
    if app:
        app = api("PUT", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}", app_body)
    else:
        app = api("POST", f"/accounts/{ACCOUNT_ID}/access/apps", app_body)
    print(f"access app: {app['id']} aud={app['aud']}")

    # 5. allow policy (exact-email allowlist; OTP + any configured IdP)
    pols = api("GET", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies")
    pol_body = {
        "name": "billet-users", "decision": "allow", "precedence": 1,
        "include": _include_list(emails),
    }
    pol = find(pols, "name", "billet-users")
    if pol:
        api("PUT", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies/{pol['id']}", pol_body)
    else:
        api("POST", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies", pol_body)
    print(f"policy billet-users: {len(emails)} email(s)")

    # 6. org team domain + tunnel token -> env file
    org = api("GET", f"/accounts/{ACCOUNT_ID}/access/organizations")
    tunnel_token = api("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tid}/token")
    upsert_env({
        "TUNNEL_TOKEN": tunnel_token,
        "BILLET_WEB_CF_TEAM_DOMAIN": org["auth_domain"],
        "BILLET_WEB_CF_AUD": app["aud"],
        "BILLET_WEB_PUBLIC_URL": f"https://{host}",
    })
    print(f"env written: {ENV_FILE} (team={org['auth_domain']})")
    print("NOTE: Google IdP is a manual dashboard step (see RUNBOOK.md); "
          "One-Time PIN already active — listed emails can log in today.")


if __name__ == "__main__":
    main()
