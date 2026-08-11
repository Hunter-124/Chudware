#!/usr/bin/env python3
"""Idempotent Cloudflare provisioning for chudware-web.

Creates/updates: the `chudware-web` remotely-managed tunnel + ingress to the local
gateway, the DNS CNAME, the Zero Trust Access application + allow policy, and
writes TUNNEL token / team domain / AUD into the gateway env file.

Secrets: reads the API token from $CF_API_TOKEN or Webservice/Cloudflare-info.txt
(gitignored). Writes secrets ONLY to ~/chudware-web-data/gateway.env (0600).

Usage: python3 cf_provision.py [--hostname chudware.com] [--emails a@x,b@y]
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
GATEWAY_SERVICE = "http://127.0.0.1:8300"
ENV_FILE = Path.home() / "chudware-web-data" / "gateway.env"


def token() -> str:
    if os.environ.get("CF_API_TOKEN"):
        return os.environ["CF_API_TOKEN"]
    info = Path(__file__).resolve().parents[1] / "Cloudflare-info.txt"
    if info.exists():
        # The file holds a LIVE API token; a group/world-readable copy means
        # any local account can mint tunnels/DNS. Refuse rather than read it.
        if info.stat().st_mode & 0o077:
            sys.exit(f"{info} is group/world-readable — run: chmod 600 {info}")
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
    try:
        with urllib.request.urlopen(req) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"cloudflare API error on {method} {path}: HTTP {e.code} {detail}")
    if not out.get("success"):
        sys.exit(f"cloudflare API error on {method} {path}: {out.get('errors')}")
    return out["result"]


def find(items: list[dict], key: str, value: str) -> dict | None:
    return next((i for i in items if i.get(key) == value), None)


# Cloudflare Access is the operator door: it gates /admin only. Anyone with an email at
# one of these domains may pass Access; the gateway itself decides who is a real user
# and assigns roles from its own account store, never from this list. CF email_domain is
# an EXACT match (no subdomain wildcard), so each domain must be listed explicitly.
ACCESS_DOMAINS = ["ua.edu", "crimson.ua.edu"]

# Additional hostnames served by the SAME tunnel and gated by the SAME Access app.
# One app => one AUD => the gateway's single CHUDWARE_WEB_CF_AUD keeps validating; a
# separate per-domain app would mint a different AUD and break JWT validation. So new
# public domains go HERE (added to the app's self_hosted_domains), never as a new app.
EXTRA_HOSTS = ["www.chudware.com"]


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hostname", default="chudware.com")
    ap.add_argument("--emails", default=None,
                    help="comma-separated allowlist for the Access policy (required; no default)")
    args = ap.parse_args()
    host = args.hostname
    zone_name = ".".join(host.split(".")[-2:])
    if not args.emails:
        # No personal default: provisioning with an omitted allowlist would
        # silently lock everyone out of the Access policy.
        ap.error("--emails is required (comma-separated Access allowlist)")
    emails = [e.strip() for e in args.emails.split(",") if e.strip()]
    if not emails:
        ap.error("--emails parsed to an empty allowlist")

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
    allow_pols = [p for p in pols if p.get("decision") == "allow"]
    pol = find(pols, "name", "chudware-users") or (allow_pols[0] if len(allow_pols) == 1 else None)
    name = pol["name"] if pol else "chudware-users"
    pol_body = {
        "name": name, "decision": "allow", "precedence": pol["precedence"] if pol else 1,
        "include": _include_list(emails),
    }
    if pol:
        api("PUT", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies/{pol['id']}", pol_body)
    else:
        api("POST", f"/accounts/{ACCOUNT_ID}/access/apps/{app['id']}/policies", pol_body)
    print(f"policy {name!r}: {len(emails)} email(s)")

    # 6. org team domain + tunnel token -> env file
    org = api("GET", f"/accounts/{ACCOUNT_ID}/access/organizations")
    tunnel_token = api("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tid}/token")
    upsert_env({
        "TUNNEL_TOKEN": tunnel_token,
        "CHUDWARE_WEB_CF_TEAM_DOMAIN": org["auth_domain"],
        "CHUDWARE_WEB_CF_AUD": app["aud"],
        "CHUDWARE_WEB_PUBLIC_URL": f"https://{host}",
    })
    print(f"env written: {ENV_FILE} (team={org['auth_domain']})")
    print("NOTE: Google IdP is a manual dashboard step (see RUNBOOK.md); "
          "One-Time PIN already active — listed emails can log in today.")


if __name__ == "__main__":
    main()
