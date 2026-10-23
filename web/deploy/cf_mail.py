#!/usr/bin/env python3
"""Idempotently manage chudware.com mail: Cloudflare Email Routing + mail DNS.

Inbound mail is free Cloudflare Email Routing: Cloudflare owns the MX records
(route1/2/3.mx.cloudflare.net) and the cf2024-1._domainkey DKIM TXT record --
those rows are ``read_only`` and must never be written from here. A routing
rule forwards one address (hunter@chudware.com) to a personal mailbox; there is
deliberately NO catch-all rule, so unknown local parts are dropped instead of
flooding the destination.

Outbound mail leaves through smtp.gmail.com, so the apex SPF record has to
include ``_spf.google.com`` alongside Cloudflare's own include, and a permissive
``p=none`` DMARC record collects reports without quarantining anything yet.

A destination address is only usable after the human owner of that mailbox
clicks the verification link Cloudflare emails on creation; ``status`` prints
the verified flag so the wait is visible.

Secrets: same as cf_provision.py -- $CF_API_TOKEN, else the gitignored
Webservice/Cloudflare-info.txt. Needs a token with DNS Write, Email Routing
Rules Write and Email Routing Addresses Write.

    python3 cf_mail.py status
    python3 cf_mail.py dns
    python3 cf_mail.py ensure --forward hunter@chudware.com --to you@gmail.com
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from cf_provision import ACCOUNT_ID, API, find, token

ZONE_NAME = "chudware.com"
FORWARD_FROM = f"hunter@{ZONE_NAME}"
SPF_RECORD = "v=spf1 include:_spf.mx.cloudflare.net include:_spf.google.com ~all"
DMARC_NAME = f"_dmarc.{ZONE_NAME}"
DMARC_RECORD = f"v=DMARC1; p=none; rua=mailto:hunter@{ZONE_NAME}"


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """Call the API and hand back (http_status, payload) without exiting.

    ``status`` probes endpoints whose permission the token may lack, and a 403
    there is information to print, not a fatal error.
    """
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"success": False, "errors": [{"message": raw}]}


def api(method: str, path: str, body: dict | None = None):
    status, payload = request(method, path, body)
    if not payload.get("success"):
        hint = " (token is missing a permission for this endpoint)" if status == 403 else ""
        sys.exit(f"cloudflare API error on {method} {path}: HTTP {status} {payload.get('errors')}{hint}")
    return payload["result"]


def zone_id() -> str:
    zones = api("GET", f"/zones?name={ZONE_NAME}")
    zone = find(zones, "name", ZONE_NAME)
    if zone is None:
        sys.exit(f"zone {ZONE_NAME} not on account {ACCOUNT_ID}")
    return zone["id"]


def unquote_txt(content: str) -> str:
    """Normalise a TXT value for comparison.

    Cloudflare returns TXT content quoted, and splits anything over 255 octets
    into several adjacent quoted strings, so the wire form of the DKIM record is
    ``"chunk one" "chunk two"``. Rejoining is what makes the compare, and hence
    the NO-OP, reliable.
    """
    parts = [chunk for chunk in content.split('"') if chunk not in ("", " ")]
    return "".join(parts) if parts else content


def txt_records(zone: str) -> list[dict]:
    return api("GET", f"/zones/{zone}/dns_records?type=TXT&per_page=100")


def upsert_txt(zone: str, name: str, content: str) -> None:
    existing = [row for row in txt_records(zone) if row["name"] == name]
    body = {"type": "TXT", "name": name, "content": content, "ttl": 1}
    for row in existing:
        if unquote_txt(row["content"]) == content:
            print(f"NO-OP   TXT {name} {content}")
            return
    if existing:
        if existing[0].get("meta", {}).get("read_only"):
            sys.exit(f"TXT {name} is a Cloudflare-managed read-only record; refusing to write")
        api("PUT", f"/zones/{zone}/dns_records/{existing[0]['id']}", body)
        print(f"UPDATE  TXT {name} {content}")
        for stale in existing[1:]:
            print(f"        WARNING extra TXT {name} left in place: {unquote_txt(stale['content'])}")
    else:
        api("POST", f"/zones/{zone}/dns_records", body)
        print(f"CREATE  TXT {name} {content}")


def describe_rule(rule: dict) -> str:
    matchers = ",".join(
        matcher.get("value", matcher["type"]) if matcher["type"] != "all" else "<catch-all>"
        for matcher in rule["matchers"]
    )
    actions = ",".join(
        f"{action['type']}({';'.join(action.get('value', []))})".replace("()", "")
        for action in rule["actions"]
    )
    state = "enabled" if rule["enabled"] else "disabled"
    return f"{matchers} -> {actions} [{state}] tag={rule['tag']}"


def ensure_routing_enabled(zone: str) -> dict:
    settings = api("GET", f"/zones/{zone}/email/routing")
    if settings.get("enabled"):
        print(f"NO-OP   email routing enabled (status {settings.get('status')})")
        return settings
    settings = api("POST", f"/zones/{zone}/email/routing/enable", {})
    print(f"UPDATE  email routing enabled (status {settings.get('status')})")
    return settings


def cmd_status(_: argparse.Namespace) -> None:
    zone = zone_id()
    print(f"zone {ZONE_NAME}: {zone}")

    status, payload = request("GET", f"/zones/{zone}/email/routing")
    if payload.get("success"):
        settings = payload["result"]
        print(f"routing: enabled={settings.get('enabled')} status={settings.get('status')} "
              f"synced={settings.get('synced')} subaddressing={settings.get('support_subaddress')}")
    else:
        print(f"routing: HTTP {status} {payload.get('errors')} (needs Zone > Email Routing Rules > Read)")

    status, payload = request("GET", f"/accounts/{ACCOUNT_ID}/email/routing/addresses?per_page=100")
    if payload.get("success"):
        print("destinations:" if payload["result"] else "destinations: none")
        for row in payload["result"]:
            flag = f"verified {row['verified']}" if row.get("verified") else "UNVERIFIED (owner must click the emailed link)"
            print(f"  {row['email']}: {flag}")
    else:
        print(f"destinations: HTTP {status} {payload.get('errors')} (needs Account > Email Routing Addresses > Read)")

    status, payload = request("GET", f"/zones/{zone}/email/routing/rules?per_page=100")
    if payload.get("success"):
        print("rules:" if payload["result"] else "rules: none")
        for rule in payload["result"]:
            print(f"  {describe_rule(rule)}")
    else:
        print(f"rules: HTTP {status} {payload.get('errors')} (needs Zone > Email Routing Rules > Read)")

    rows = txt_records(zone)
    print("mail TXT records:")
    for row in sorted(rows, key=lambda r: r["name"]):
        value = unquote_txt(row["content"])
        if not (value.startswith(("v=spf1", "v=DMARC1", "v=DKIM1")) or "_domainkey" in row["name"]):
            continue
        managed = " [cloudflare-managed, read-only]" if row.get("meta", {}).get("read_only") else ""
        print(f"  {row['name']}: {value[:110]}{'...' if len(value) > 110 else ''}{managed}")
    if not any(row["name"] == DMARC_NAME for row in rows):
        print(f"  {DMARC_NAME}: ABSENT (run: cf_mail.py dns)")


def cmd_dns(_: argparse.Namespace) -> None:
    zone = zone_id()
    upsert_txt(zone, ZONE_NAME, SPF_RECORD)
    upsert_txt(zone, DMARC_NAME, DMARC_RECORD)


def cmd_ensure(args: argparse.Namespace) -> None:
    zone = zone_id()
    ensure_routing_enabled(zone)

    addresses = api("GET", f"/accounts/{ACCOUNT_ID}/email/routing/addresses?per_page=100")
    destination = find(addresses, "email", args.to)
    if destination is None:
        destination = api("POST", f"/accounts/{ACCOUNT_ID}/email/routing/addresses", {"email": args.to})
        print(f"CREATE  destination {args.to} — Cloudflare has emailed a verification link there")
    elif destination.get("verified"):
        print(f"NO-OP   destination {args.to} (verified {destination['verified']})")
    else:
        print(f"NO-OP   destination {args.to} (still UNVERIFIED — the owner must click the emailed link)")

    body = {
        "name": f"{args.forward} -> {args.to}",
        "enabled": True,
        "matchers": [{"type": "literal", "field": "to", "value": args.forward}],
        "actions": [{"type": "forward", "value": [args.to]}],
    }
    rules = api("GET", f"/zones/{zone}/email/routing/rules?per_page=100")
    current = next(
        (
            rule
            for rule in rules
            if any(m.get("value") == args.forward for m in rule["matchers"])
        ),
        None,
    )
    if current is None:
        api("POST", f"/zones/{zone}/email/routing/rules", body)
        print(f"CREATE  rule {args.forward} -> forward({args.to})")
    elif (
        current["matchers"] == body["matchers"]
        and current["actions"] == body["actions"]
        and current["enabled"]
    ):
        print(f"NO-OP   rule {args.forward} -> forward({args.to})")
    else:
        api("PUT", f"/zones/{zone}/email/routing/rules/{current['tag']}", body)
        print(f"UPDATE  rule {args.forward} -> forward({args.to})")

    # No catch-all rule: the zone's catch-all stays whatever it is (drop), so
    # only the addresses named above ever reach a mailbox.
    print(f"catch-all: untouched by design — only {args.forward} is routed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print routing state, destinations, rules and mail TXT records").set_defaults(func=cmd_status)
    sub.add_parser("dns", help="upsert the SPF and DMARC TXT records").set_defaults(func=cmd_dns)
    ensure = sub.add_parser("ensure", help="enable routing and forward one address to a destination mailbox")
    ensure.add_argument("--forward", default=FORWARD_FROM, help=f"address at {ZONE_NAME} to route (default {FORWARD_FROM})")
    ensure.add_argument("--to", required=True, help="destination mailbox, e.g. you@gmail.com")
    ensure.set_defaults(func=cmd_ensure)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
