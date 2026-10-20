#!/usr/bin/env python3
"""Idempotently describe the production Chudware Cloudflare topology.

The tunnel serves the portal; staging stays unpublished unless explicitly selected.
The public marketing apex/www live on Cloudflare Pages. This script provisions NO Cloudflare Access
application: the door to ``app.chudware.com/admin`` is the gateway's own
session cookie plus ``accounts.role == 'admin'``, granted by
``python -m authn grant-admin``. Fronting ``/admin*`` with Access meant the
operator met Cloudflare's login page instead of the gateway's, and every
browser that had completed Access for the zone carried a CF_Authorization
cookie that the middleware once preferred over the live session.

Access remains supported by the gateway as a fallback credential for a caller
with no session, but it is configured by hand in Zero Trust if wanted at all;
nothing here writes CHUDWARE_WEB_CF_TEAM_DOMAIN or CHUDWARE_WEB_CF_AUD.

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
PORTAL_HOST = "app.chudware.com"
STAGING_HOST = "staging.chudware.com"
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

def configure_ingress(tunnel_id: str, publish_staging: bool) -> None:
    path = f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/configurations"
    current = api("GET", path)
    if not isinstance(current, dict) or not isinstance(current.get("config", {}), dict):
        sys.exit("invalid existing tunnel configuration; refusing to replace it")
    config = dict(current.get("config", {}))
    previous = config.get("ingress", [])
    if not isinstance(previous, list) or any(not isinstance(row, dict) for row in previous):
        sys.exit("invalid existing tunnel ingress; refusing to replace it")
    managed = {PORTAL_HOST, STAGING_HOST}
    foreign = [
        row for row in previous
        if row.get("hostname") not in managed and (row.get("hostname") or row.get("path"))
    ]
    fallbacks = [row for row in previous if not row.get("hostname") and not row.get("path")]
    if len(fallbacks) > 1:
        sys.exit("ambiguous tunnel fallback; refusing to replace it")
    ingress = [{"hostname": PORTAL_HOST, "service": PRODUCTION_SERVICE}]
    if publish_staging:
        ingress.append({"hostname": STAGING_HOST, "service": STAGING_SERVICE})
    config["ingress"] = ingress + foreign + (fallbacks or [{"service": "http_status:404"}])
    api("PUT", path, {"config": config})


def remove_staging_dns(zones: list[dict], target: str) -> None:
    zone = _zone(zones, STAGING_HOST)
    records = api("GET", f"/zones/{zone['id']}/dns_records?name={STAGING_HOST}")
    if not isinstance(records, list):
        sys.exit("invalid staging DNS response")
    for record in records:
        if record.get("type") == "CNAME" and record.get("content") == target:
            api("DELETE", f"/zones/{zone['id']}/dns_records/{record['id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages-project", default=PAGES_PROJECT)
    parser.add_argument(
        "--publish-staging", action="store_true",
        help="explicitly publish the isolated staging gateway; private by default",
    )
    args = parser.parse_args()

    tunnels = api("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel?is_deleted=false")
    assert isinstance(tunnels, list)
    tunnel = find(tunnels, "name", TUNNEL_NAME) or api(
        "POST", f"/accounts/{ACCOUNT_ID}/cfd_tunnel",
        {"name": TUNNEL_NAME, "config_src": "cloudflare"},
    )
    assert isinstance(tunnel, dict)
    tunnel_id = tunnel["id"]
    print(f"tunnel {TUNNEL_NAME}: {tunnel_id}")

    configure_ingress(tunnel_id, args.publish_staging)
    print(f"ingress: {PORTAL_HOST} -> {PRODUCTION_SERVICE}; unrelated routes preserved")
    print(f"staging: {'published explicitly' if args.publish_staging else 'unpublished'}")

    zones = api("GET", "/zones")
    assert isinstance(zones, list)
    tunnel_target = f"{tunnel_id}.cfargotunnel.com"
    _upsert_cname(zones, PORTAL_HOST, tunnel_target)
    if args.publish_staging:
        _upsert_cname(zones, STAGING_HOST, tunnel_target)
    else:
        remove_staging_dns(zones, tunnel_target)
    pages_target = f"{args.pages_project}.pages.dev"
    for hostname in PAGES_HOSTS:
        _upsert_cname(zones, hostname, pages_target)

    tunnel_token = api("GET", f"/accounts/{ACCOUNT_ID}/cfd_tunnel/{tunnel_id}/token")
    assert isinstance(tunnel_token, str)
    write_tunnel_env(tunnel_token)
    # Only the public base URL. CHUDWARE_WEB_CF_TEAM_DOMAIN / CHUDWARE_WEB_CF_AUD
    # are deliberately absent: no Access application is provisioned, so there is
    # no AUD to emit. upsert_env merges, so a gateway.env that already carries
    # those two keys from a hand-configured Access fallback keeps them.
    upsert_env({"CHUDWARE_WEB_PUBLIC_BASE_URL": f"https://{PORTAL_HOST}"})
    print(f"gateway env written: {ENV_FILE}")
    print(f"tunnel env written: {TUNNEL_ENV_FILE}")
    print(f"admin access: python -m authn grant-admin --email <addr>, then sign in at https://{PORTAL_HOST}/signin")


if __name__ == "__main__":
    main()
