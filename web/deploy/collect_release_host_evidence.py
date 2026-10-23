#!/usr/bin/env python3
"""Read-only, fail-closed P3 operator evidence collection.

Only explicit sanitized packets are imported. Packet signatures authenticate the
pinned signer and exact claims, not the truth of a claim or an arbitrary local
hash. The collector never signs, reads configuration environments/credentials,
queries customer databases, or reads journals/process environments. Local probes
are opt-in and never elevate privileges. Output goes only to stdout.

Packet schema: {schema, payload, authentication?}; payload contains host_id,
created_at, expires_at, origin {kind,id}, host_identity_sha256 (SHA256 of exact
/etc/machine-id bytes), and gates. Each gate contains exactly
policy, facts, references. References contain id, sha256, origin {kind,id}, and
observed_at. Ed25519 signs canonical JSON payload (sorted keys, compact separators,
ASCII escapes, no NaN), prefixed by SIGNING_DOMAIN. --trusted-key pins key_id=hex
public key; a key embedded in a packet is never trusted. The validators below
are the machine-readable field contract; unknown fields are refused, not echoed.

No probes run without --observe-local. --fixture-root maps only unit-file paths
into a disposable root and disables subprocesses; fixture reports never satisfy
release requirements. Sanitized signed imports can supply observations that the
current account cannot read. A contradictory or unsafe local observation always
refuses its gate, rather than falling back to the imported claim.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pwd
import grp
import re
import selectors
import signal
import stat
import subprocess
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

SCHEMA = "chudware-release-host-evidence-v1"
PACKET_SCHEMA = "chudware-release-host-packet-v1"
SIGNING_DOMAIN = b"chudware-release-host-packet-v1\x00"
GATES = ("installed_units", "service_authority", "network_policy", "runtime_settings",
         "historical_credentials", "backup_coverage", "restore_objectives", "concurrent_timers")
ENVIRONMENTS = {"prod", "staging"}
ROLES = {"gateway", "hostd", "cell", "cell_socket", "egress", "egress_socket"}
ORIGINS = {"independent-operator", "provider", "control-plane", "incident-review"}
PRIVILEGED_GROUPS = {"root", "wheel", "sudo", "admin", "docker", "podman", "lxd", "lxc",
                     "libvirt", "disk", "shadow", "systemd-journal"}
LIMIT = 1024 * 1024
COMMAND_LIMIT = 128 * 1024
MAX_AGE = 7 * 24 * 60 * 60
SHOW = ("LoadState", "ActiveState", "UnitFileState", "User", "Group", "SupplementaryGroups",
        "SocketUser", "SocketGroup", "FragmentPath", "DropInPaths", "ControlGroup", "Delegate", "NoNewPrivileges",
        "MemoryMax", "TasksMax", "NextElapseUSecMonotonic", "NextElapseUSecRealtime", "Persistent")
SSH_FIELDS = {"passwordauthentication", "permitrootlogin", "pubkeyauthentication",
              "permituserenvironment", "allowtcpforwarding"}


class Refusal(ValueError):
    """Contains only a fixed diagnostic code, never input bytes."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def exact(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "invalid_or_unknown_fields")
    return value


def sequence(value, maximum=128):
    require(isinstance(value, list) and len(value) <= maximum, "invalid_list")
    return value


def ident(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_@.:-]{0,127}", value), "invalid_identity")
    return value


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_sha256")
    return value


def number(value, minimum=0, maximum=2**63 - 1):
    require(type(value) is int and minimum <= value <= maximum, "invalid_integer")
    return value


def boolean(value):
    require(type(value) is bool, "invalid_boolean")
    return value


def stamp(value):
    require(isinstance(value, str) and len(value) <= 40, "invalid_date")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("invalid_date") from None
    require(result.tzinfo is not None and result.utcoffset() == timedelta(0), "utc_date_required")
    return result


def recent(value, now, age=MAX_AGE):
    date = stamp(value)
    require(now - timedelta(seconds=age) <= date <= now, "stale_or_future_evidence")
    return date


def origin(value):
    exact(value, {"kind", "id"})
    require(value["kind"] in ORIGINS, "invalid_origin")
    ident(value["id"])


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def bounded_tree(value):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(depth <= 20 and count <= 20000, "packet_complexity_limit")
        if isinstance(item, dict):
            require(all(isinstance(key, str) and len(key) <= 128 for key in item), "invalid_json_key")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            require(len(item) <= 512, "packet_list_limit")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            require(len(item) <= 1024 and all(32 <= ord(char) < 127 for char in item), "unsafe_packet_text")
        else:
            require(item is None or type(item) in {bool, int}, "invalid_json_value")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def open_regular(path, maximum=LIMIT):
    """Pin each component; reject symlinks, devices, FIFOs and concurrent changes."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "absolute_path_required")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, "unsafe_or_oversized_file")
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, maximum + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                require(total <= maximum, "file_size_limit")
                chunks.append(chunk)
            after = os.fstat(fd)
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "file_changed_during_read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def read_packet(path):
    path = Path(path).absolute()
    require(path.suffix == ".json" and not any(part.lower() in
            {"secrets", "credentials", "credstore", "credstore.encrypted", ".ssh", ".aws", ".azure", ".gnupg",
             ".pki", ".docker", ".kube", "gcloud", "browser_profiles", ".mozilla", "chromium", "firefox",
             "auth.json", "tokens.json", "credentials.json", ".credentials.json", "config.json",
             "application_default_credentials.json"}
            or part.lower().startswith(".env") or ".env" in part.lower() for part in path.parts),
            "sanitized_json_packet_required")
    try:
        value = json.loads(open_regular(path), object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(Refusal("invalid_json_constant")))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise Refusal("invalid_packet_json") from None
    bounded_tree(value)
    require(isinstance(value, dict) and set(value) in
            ({"schema", "payload"}, {"schema", "payload", "authentication"}), "invalid_packet_envelope")
    require(value["schema"] == PACKET_SCHEMA, "unsupported_packet_schema")
    return value


def authenticate(packet, keys):
    auth = packet.get("authentication")
    if auth is None:
        return {"status": "unsigned_reported", "reason": "signature_missing"}
    exact(auth, {"algorithm", "key_id", "signature"})
    require(auth["algorithm"] == "ed25519", "unsupported_signature_algorithm")
    key_id = ident(auth["key_id"])
    require(isinstance(auth["signature"], str) and re.fullmatch(r"[0-9a-f]{128}", auth["signature"]),
            "invalid_signature_encoding")
    if key_id not in keys:
        return {"status": "unavailable", "reason": "signer_not_pinned", "key_id": key_id}
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return {"status": "unavailable", "reason": "ed25519_verifier_unavailable", "key_id": key_id}
    try:
        Ed25519PublicKey.from_public_bytes(keys[key_id]).verify(
            bytes.fromhex(auth["signature"]), SIGNING_DOMAIN + canonical(packet["payload"]))
    except Exception:
        raise Refusal("signature_verification_failed") from None
    return {"status": "authenticated_reported", "key_id": key_id,
            "public_key_sha256": hashlib.sha256(keys[key_id]).hexdigest()}


def references(value, now):
    result = {}
    for entry in sequence(value):
        exact(entry, {"id", "sha256", "origin", "observed_at"})
        key = ident(entry["id"])
        require(key not in result, "duplicate_reference")
        digest(entry["sha256"])
        origin(entry["origin"])
        recent(entry["observed_at"], now)
        result[key] = entry
    require(result, "dated_hashed_references_required")
    return result


def reference(value, refs, kind=None):
    require(ident(value) in refs, "missing_evidence_reference")
    if kind:
        require(refs[value]["origin"]["kind"] == kind, "reference_origin_mismatch")


def unit_path(value, unit):
    require(isinstance(value, str) and len(value) <= 512, "invalid_unit_path")
    path = PurePosixPath(value)
    require(path.is_absolute() and not any(part in {".", ".."} for part in value.split("/")),
            "unsafe_unit_path")
    names = {unit, re.sub(r"@.*\.(service|socket|timer)$", r"@.\1", unit)}
    fragment = path.name in names
    dropin = path.parent.name in {name + ".d" for name in names} and re.fullmatch(r"[A-Za-z0-9_.-]+\.conf", path.name)
    root = str(path.parent if fragment else path.parent.parent)
    allowed = re.fullmatch(r"/(?:etc|usr/lib|usr/local/lib|lib)/systemd/(?:system|user)", root)
    allowed = allowed or re.fullmatch(r"/(?:home/[A-Za-z0-9_-]+|var/lib/chudware-staging|root)/"
                                     r"\.(?:config|local/share)/systemd/user", root)
    require(allowed and (fragment or dropin), "non_unit_file_refused")
    return value


def unit_identity(value):
    ident(value)
    require(re.fullmatch(r"[A-Za-z0-9_@.:-]+\.(?:service|socket|timer)", value), "invalid_unit_name")


def unit_policy(entry):
    exact(entry, {"id", "environment", "role", "manager", "user", "unit", "fragment", "sha256", "dropins"})
    ident(entry["id"])
    require(entry["environment"] in ENVIRONMENTS and entry["manager"] in {"system", "user"}, "invalid_unit_scope")
    ident(entry["role"])
    ident(entry["user"])
    unit_identity(entry["unit"])
    unit_path(entry["fragment"], entry["unit"])
    digest(entry["sha256"])
    seen = set()
    for dropin in sequence(entry["dropins"]):
        exact(dropin, {"path", "sha256"})
        unit_path(dropin["path"], entry["unit"])
        require(dropin["path"] not in seen and dropin["path"] != entry["fragment"], "duplicate_dropin")
        seen.add(dropin["path"])
        digest(dropin["sha256"])
    require(entry["dropins"] == sorted(entry["dropins"], key=lambda item: item["path"]), "dropins_not_canonical")


def installed_units(policy, facts, refs, now):
    exact(policy, {"inventory_complete", "units"})
    require(boolean(policy["inventory_complete"]), "unit_inventory_incomplete")
    units = sequence(policy["units"])
    require(units, "units_missing")
    exact(facts, {"units"})
    require(isinstance(facts["units"], dict), "invalid_units")
    seen, coverage = set(), set()
    for entry in units:
        unit_policy(entry)
        key = entry["id"]
        require(key not in seen, "duplicate_unit")
        seen.add(key)
        coverage.add((entry["environment"], entry["role"]))
        actual = facts["units"].get(key)
        exact(actual, {"fragment", "sha256", "dropins", "user", "group", "supplementary_groups",
                       "load_state", "no_new_privileges", "memory_max", "tasks_max", "control_group", "delegate"})
        require(actual["fragment"] == entry["fragment"] and actual["sha256"] == entry["sha256"], "unit_hash_mismatch")
        require(actual["dropins"] == entry["dropins"], "dropin_set_or_hash_mismatch")
        require(ident(actual["user"]) == entry["user"] and actual["user"] != "root", "unit_identity_mismatch")
        ident(actual["group"])
        groups = sequence(actual["supplementary_groups"])
        require(all(ident(group) not in PRIVILEGED_GROUPS and not group.isdigit() for group in groups) and
                actual["group"] not in PRIVILEGED_GROUPS and not actual["group"].isdigit(), "privileged_unit_group")
        require(actual["load_state"] == "loaded", "unit_not_loaded")
        service = entry["unit"].endswith(".service")
        nnp = boolean(actual["no_new_privileges"])
        require(not service or nnp, "privilege_inheritance")
        number(actual["memory_max"], 1 if service else 0)
        number(actual["tasks_max"], 1 if service else 0)
        group_path = actual["control_group"]
        require(isinstance(group_path, str) and
                ((not service and not group_path) or (re.fullmatch(r"/[A-Za-z0-9_@./:\\-]{1,256}", group_path) and
                 ".." not in PurePosixPath(group_path).parts)), "missing_cgroup")
        boolean(actual["delegate"])
    require(set(facts["units"]) == seen, "unit_inventory_mismatch")
    require({(env, role) for env in ENVIRONMENTS for role in ROLES} <= coverage, "environment_unit_coverage_missing")
    require({entry["user"] for entry in units if entry["environment"] == "prod"}.isdisjoint(
            {entry["user"] for entry in units if entry["environment"] == "staging"}), "shared_environment_identity")


def service_authority(policy, facts, refs, now):
    exact(policy, {"inventory_complete", "identities"})
    require(boolean(policy["inventory_complete"]), "authority_inventory_incomplete")
    exact(facts, {"identities"})
    require(isinstance(facts["identities"], dict), "invalid_authorities")
    expected, uids = set(), {}
    for item in sequence(policy["identities"]):
        exact(item, {"user", "environment", "uid", "gid", "groups"})
        user = ident(item["user"])
        require(user not in expected and item["environment"] in ENVIRONMENTS, "invalid_authority_scope")
        expected.add(user)
        number(item["uid"], 1)
        number(item["gid"], 1)
        groups = sequence(item["groups"])
        require(groups and groups == sorted(set(groups)) and
                all(ident(group) not in PRIVILEGED_GROUPS and not group.isdigit() for group in groups),
                "privileged_or_noncanonical_identity_group")
        actual = facts["identities"].get(user)
        exact(actual, {"uid", "gid", "groups", "sudo_allowed", "user_manager_uid", "user_manager_groups",
                       "user_manager_sudo_allowed", "linger", "controllers", "memory_max", "tasks_max", "review_reference"})
        number(actual["uid"], 1)
        number(actual["gid"], 1)
        number(actual["user_manager_uid"], 1)
        require(actual["uid"] == item["uid"] and actual["gid"] == item["gid"] and actual["groups"] == groups, "identity_or_groups_mismatch")
        require(actual["sudo_allowed"] is False and actual["user_manager_sudo_allowed"] is False, "sudo_authority_present")
        require(actual["user_manager_uid"] == item["uid"] and actual["user_manager_groups"] == groups, "user_manager_privilege_inheritance")
        require(boolean(actual["linger"]), "user_manager_not_persistent")
        require(set(sequence(actual["controllers"])) >= {"cpu", "memory", "pids"} and
                set(actual["controllers"]) <= {"cpu", "cpuset", "memory", "pids", "io"}, "required_cgroup_controller_missing")
        number(actual["memory_max"], 1)
        number(actual["tasks_max"], 1)
        reference(actual["review_reference"], refs)
        uids.setdefault(item["environment"], set()).add(item["uid"])
    require(set(facts["identities"]) == expected and set(uids) == ENVIRONMENTS, "authority_inventory_mismatch")
    require(uids["prod"].isdisjoint(uids["staging"]), "shared_environment_uid")


def endpoint(entry):
    exact(entry, {"protocol", "address", "port"})
    require(entry["protocol"] in {"tcp", "udp"}, "invalid_listener_protocol")
    try:
        ipaddress.ip_address(entry["address"])
    except (ValueError, TypeError):
        raise Refusal("invalid_listener_address") from None
    number(entry["port"], 1, 65535)


def network_policy(policy, facts, refs, now):
    exact(policy, {"public_ports", "firewall_zones", "ssh_contexts"})
    ports = sequence(policy["public_ports"])
    require(len(set(ports)) == len(ports), "duplicate_public_port")
    for port in ports:
        number(port, 1, 65535)
    require(not set(ports) & {5432, 8300, 8310}, "backend_publication_forbidden")
    exact(facts, {"listeners", "firewall_zones", "ssh_contexts", "external_scan_ports", "external_scan_reference", "firewall_review_reference"})
    seen_gateway = set()
    for entry in sequence(facts["listeners"], 512):
        endpoint(entry)
        address = ipaddress.ip_address(entry["address"])
        if not address.is_loopback:
            require(entry["port"] in ports, "unexpected_public_listener")
        if entry["protocol"] == "tcp" and address.is_loopback and entry["port"] in {8300, 8310}:
            seen_gateway.add(entry["port"])
    require(seen_gateway == {8300, 8310}, "gateway_loopback_listener_missing")
    zones = sequence(policy["firewall_zones"])
    require(zones and facts["firewall_zones"] == zones, "firewall_zone_mismatch")
    for zone in zones:
        exact(zone, {"name", "target", "services", "ports"})
        ident(zone["name"])
        require(zone["target"] in {"default", "DROP", "REJECT"}, "permissive_firewall_target")
        for service in sequence(zone["services"]):
            ident(service)
        for port in sequence(zone["ports"]):
            require(isinstance(port, str) and re.fullmatch(r"[0-9]{1,5}/(?:tcp|udp)", port), "firewall_range_refused")
            require(int(port.split("/")[0]) in ports, "unexpected_firewall_port")
    contexts = sequence(policy["ssh_contexts"])
    require(contexts and facts["ssh_contexts"] == contexts, "ssh_context_mismatch")
    for context in contexts:
        exact(context, {"user", "host", "address", "settings"})
        ident(context["user"])
        ident(context["host"])
        try:
            ipaddress.ip_address(context["address"])
        except (ValueError, TypeError):
            raise Refusal("invalid_ssh_context") from None
        settings = exact(context["settings"], SSH_FIELDS)
        require(settings == {"passwordauthentication": "no", "permitrootlogin": "no",
                             "pubkeyauthentication": "yes", "permituserenvironment": "no",
                             "allowtcpforwarding": "no"}, "unsafe_ssh_policy")
    require(facts["external_scan_ports"] == ports, "external_exposure_mismatch")
    reference(facts["external_scan_reference"], refs, "independent-operator")
    reference(facts["firewall_review_reference"], refs, "independent-operator")


def runtime_settings(policy, facts, refs, now):
    exact(policy, {"staging_publication_allowed"})
    boolean(policy["staging_publication_allowed"])
    exact(facts, {"environments", "config_inventory_complete", "production_staging_separate",
                  "staging_published", "staging_approval_reference", "edge_reference", "settings_reference"})
    require(facts["config_inventory_complete"] is True and facts["production_staging_separate"] is True,
            "configuration_inventory_or_separation_missing")
    exact(facts["environments"], ENVIRONMENTS)
    for env in ENVIRONMENTS:
        entry = exact(facts["environments"][env], {"terminal_enabled", "provider_sharing", "proxy_trust", "edge_origin", "edge_access_enforced"})
        require(entry["terminal_enabled"] is False and entry["provider_sharing"] is False, "terminal_or_provider_sharing_enabled")
        require(entry["edge_access_enforced"] is True, "edge_control_unproven")
        require(entry["edge_origin"] == ("127.0.0.1:8300" if env == "prod" else "127.0.0.1:8310"), "edge_origin_mismatch")
        trust = sequence(entry["proxy_trust"])
        require(trust, "proxy_trust_missing")
        for raw in trust:
            try:
                network = ipaddress.ip_network(raw, strict=True)
            except (TypeError, ValueError):
                raise Refusal("invalid_proxy_trust") from None
            require(network.is_loopback, "proxy_trust_unrestricted")
    boolean(facts["staging_published"])
    if facts["staging_published"]:
        require(policy["staging_publication_allowed"], "staging_published_without_approval")
        reference(facts["staging_approval_reference"], refs, "independent-operator")
    else:
        require(facts["staging_approval_reference"] is None, "unexpected_staging_approval")
    reference(facts["edge_reference"], refs, "control-plane")
    reference(facts["settings_reference"], refs, "independent-operator")


def historical_credentials(policy, facts, refs, now):
    exact(policy, {"consumer_inventory_complete", "credentials", "exposure_windows"})
    require(policy["consumer_inventory_complete"] is True, "consumer_inventory_incomplete")
    exact(facts, {"credentials", "log_reviews", "sessions_revoked", "identity_recovery_reviewed"})
    require(facts["sessions_revoked"] is True and facts["identity_recovery_reviewed"] is True, "historical_identity_recovery_incomplete")
    require(isinstance(facts["credentials"], dict) and isinstance(facts["log_reviews"], dict), "invalid_incident_inventory")
    expected, kinds = set(), set()
    for item in sequence(policy["credentials"]):
        exact(item, {"id", "kind", "consumers", "exposed_at", "history_reference"})
        key = ident(item["id"])
        require(key not in expected, "duplicate_credential_identity")
        expected.add(key)
        require(item["kind"] in {"cloudflare_api", "r2_s3", "tunnel", "session_signing", "oauth", "provider", "entitlement"}, "invalid_credential_kind")
        kinds.add(item["kind"])
        exposed = stamp(item["exposed_at"])
        require(exposed <= now, "future_exposure")
        reference(item["history_reference"], refs, "incident-review")
        consumers = sequence(item["consumers"])
        require(consumers and len(set(consumers)) == len(consumers), "credential_consumers_missing")
        for consumer in consumers:
            ident(consumer)
        actual = exact(facts["credentials"].get(key), {"old_identity_sha256", "new_identity_sha256", "revoked_at", "rotated_at", "consumers", "revocation_reference", "rotation_reference"})
        old, new = digest(actual["old_identity_sha256"]), digest(actual["new_identity_sha256"])
        require(old != new, "credential_not_rotated")
        require(exposed <= stamp(actual["revoked_at"]) <= now and exposed <= stamp(actual["rotated_at"]) <= now, "credential_revocation_or_rotation_missing")
        kind = "provider" if item["kind"] in {"cloudflare_api", "r2_s3", "tunnel", "provider", "oauth"} else "independent-operator"
        reference(actual["revocation_reference"], refs, kind)
        reference(actual["rotation_reference"], refs, kind)
        observed = set()
        for consumer in sequence(actual["consumers"]):
            exact(consumer, {"id", "identity_sha256", "updated_at", "reference"})
            ident(consumer["id"])
            require(consumer["id"] not in observed, "duplicate_consumer")
            observed.add(consumer["id"])
            require(digest(consumer["identity_sha256"]) == new and stamp(actual["rotated_at"]) <= stamp(consumer["updated_at"]) <= now, "consumer_not_updated")
            reference(consumer["reference"], refs)
        require(observed == set(consumers), "consumer_inventory_mismatch")
    require(set(facts["credentials"]) == expected and
            {"cloudflare_api", "r2_s3", "tunnel"} <= kinds, "historical_credential_inventory_missing")
    paths = {"engine_rpc", "oauth_preregistration", "account_status_override"}
    exact(policy["exposure_windows"], paths)
    exact(facts["log_reviews"], paths)
    for path in paths:
        window = exact(policy["exposure_windows"][path], {"start", "end", "reference"})
        start, end = stamp(window["start"]), stamp(window["end"])
        require(start <= end <= now, "invalid_exposure_window")
        reference(window["reference"], refs, "incident-review")
        review = exact(facts["log_reviews"][path], {"start", "end", "reviewed_at", "disposition", "reference"})
        require(stamp(review["start"]) <= start and end <= stamp(review["end"]) <= now and
                end <= stamp(review["reviewed_at"]) <= now, "exposure_window_not_reviewed")
        require(review["disposition"] in {"no_findings", "incident_response_complete"}, "incident_disposition_open")
        reference(review["reference"], refs, "incident-review")


def backup_policy(policy):
    exact(policy, {"inventory_complete", "environments"})
    require(policy["inventory_complete"] is True, "configured_root_inventory_incomplete")
    exact(policy["environments"], ENVIRONMENTS)
    for env in ENVIRONMENTS:
        entry = exact(policy["environments"][env], {"database_id", "roots", "institutional_required", "rpo_seconds", "rto_seconds"})
        ident(entry["database_id"])
        boolean(entry["institutional_required"])
        number(entry["rpo_seconds"], 1, MAX_AGE)
        number(entry["rto_seconds"], 1, MAX_AGE)
        require(isinstance(entry["roots"], dict), "invalid_backup_roots")
        expected = {"data", "workspace", "bucket", "cells"} | ({"university"} if entry["institutional_required"] else set())
        require(set(entry["roots"]) == expected, "configured_backup_root_missing")
        for value in entry["roots"].values():
            digest(value)
    require(policy["environments"]["prod"]["database_id"] != policy["environments"]["staging"]["database_id"] and
            set(policy["environments"]["prod"]["roots"].values()).isdisjoint(
                policy["environments"]["staging"]["roots"].values()), "shared_backup_authority")


def backup_coverage(policy, facts, refs, now):
    backup_policy(policy)
    exact(facts, ENVIRONMENTS)
    for env in ENVIRONMENTS:
        expected = policy["environments"][env]
        entry = exact(facts[env], {"database_id", "roots", "mount_inventory_complete", "credentials_separate_protected",
                                  "manifest_sha256", "manifest_reference", "artifacts", "credential_protection_reference"})
        require(entry["database_id"] == expected["database_id"] and entry["roots"] == expected["roots"], "backup_root_or_database_missing")
        require(entry["mount_inventory_complete"] is True and entry["credentials_separate_protected"] is True, "backup_mount_or_credential_protection_missing")
        digest(entry["manifest_sha256"])
        reference(entry["manifest_reference"], refs)
        require(refs[entry["manifest_reference"]]["sha256"] == entry["manifest_sha256"], "manifest_reference_hash_mismatch")
        reference(entry["credential_protection_reference"], refs, "independent-operator")
        exact(entry["artifacts"], {"database", "state"})
        for artifact in entry["artifacts"].values():
            exact(artifact, {"ciphertext_sha256", "remote_sha256", "encryption", "off_host", "destination_id", "completed_at", "reference"})
            require(digest(artifact["ciphertext_sha256"]) == digest(artifact["remote_sha256"]), "off_host_hash_mismatch")
            require(artifact["encryption"] == "age" and artifact["off_host"] is True, "off_host_encrypted_backup_missing")
            ident(artifact["destination_id"])
            recent(artifact["completed_at"], now, expected["rpo_seconds"])
            reference(artifact["reference"], refs, "provider")
            require(refs[artifact["reference"]]["sha256"] == artifact["remote_sha256"], "off_host_reference_hash_mismatch")


def restore_objectives(policy, facts, refs, now):
    backup_policy(policy)
    exact(facts, ENVIRONMENTS)
    for env in ENVIRONMENTS:
        expected = policy["environments"][env]
        entry = exact(facts[env], {"artifacts", "restored_roots", "target_root_sha256", "target_database_id", "isolated",
                                  "schema_compatible", "source_rollback", "rollback_drill_complete", "started_at", "completed_at",
                                  "recovery_point_at", "duration_seconds", "reference"})
        exact(entry["artifacts"], {"database", "state"})
        for value in entry["artifacts"].values():
            digest(value)
        require(entry["restored_roots"] == expected["roots"], "restore_root_missing")
        source_roots = {value for item in policy["environments"].values() for value in item["roots"].values()}
        require(digest(entry["target_root_sha256"]) not in source_roots and
                ident(entry["target_database_id"]) not in {item["database_id"] for item in policy["environments"].values()}, "restore_target_not_isolated")
        require(entry["isolated"] is True and entry["schema_compatible"] is True and entry["rollback_drill_complete"] is True,
                "restore_or_schema_or_rollback_incomplete")
        require(entry["source_rollback"] in {"compatible", "database_restore_required"}, "rollback_limitations_missing")
        started, completed = recent(entry["started_at"], now), recent(entry["completed_at"], now)
        point = stamp(entry["recovery_point_at"])
        require(point <= started <= completed, "invalid_restore_timeline")
        duration = number(entry["duration_seconds"])
        require(duration == int((completed - started).total_seconds()) and duration <= expected["rto_seconds"] and
                (started - point).total_seconds() <= expected["rpo_seconds"], "restore_objective_missed")
        reference(entry["reference"], refs, "independent-operator")


def concurrent_timers(policy, facts, refs, now):
    exact(policy, {"timers", "security_updates_applied", "security_updates_reference"})
    require(policy["security_updates_applied"] is True, "security_updates_not_automatic")
    reference(policy["security_updates_reference"], refs, "independent-operator")
    require(isinstance(facts, dict), "invalid_timer_facts")
    expected, coverage = set(), set()
    for entry in sequence(policy["timers"]):
        exact(entry, {"id", "environment", "role", "manager", "user", "unit", "max_last_run_age_seconds"})
        key = ident(entry["id"])
        require(key not in expected and entry["environment"] in ENVIRONMENTS and
                entry["role"] in {"release_retention", "security_retention", "backup", "security_update"} and
                entry["manager"] in {"user", "system"}, "invalid_timer_scope")
        expected.add(key)
        coverage.add((entry["environment"], entry["role"]))
        ident(entry["user"])
        require(entry["role"] == "security_update" or entry["manager"] == "user",
                "environment_timer_wrong_user_manager")
        unit_identity(entry["unit"])
        require(entry["unit"].endswith(".timer"), "timer_unit_required")
        age = number(entry["max_last_run_age_seconds"], 1, MAX_AGE)
        actual = exact(facts.get(key), {"enabled", "active", "persistent", "next_elapse_set", "last_success_at", "reference"})
        require(all(actual[key] is True for key in ("enabled", "active", "persistent", "next_elapse_set")), "timer_not_concurrently_active")
        recent(actual["last_success_at"], now, age)
        reference(actual["reference"], refs)
    require(set(facts) == expected and coverage == {(env, role) for env in ENVIRONMENTS
            for role in {"release_retention", "security_retention", "backup", "security_update"}}, "environment_timer_coverage_missing")


VALIDATORS = {name: globals()[name] for name in GATES}


def validate_command(argv):
    """Reject even an internal caller's attempt to broaden the probe surface."""
    require(isinstance(argv, list) and all(isinstance(arg, str) and len(arg) <= 1024 for arg in argv),
            "invalid_command")
    command = argv[0] if argv else ""
    if command == "/usr/bin/systemctl":
        prefix = [command, "--no-pager", "--no-ask-password"]
        rest = argv[len(prefix):]
        require(argv[:len(prefix)] == prefix, "command_not_allowlisted")
        if rest and rest[0] == "--user":
            rest = rest[1:]
        require(len(rest) == 3 and rest[0] == "show" and rest[2] == "--property=" + ",".join(SHOW),
                "command_not_allowlisted")
        unit_identity(rest[1])
    elif command == "/usr/bin/loginctl":
        require(len(argv) == 6 and argv[1:4] == ["--no-pager", "--no-ask-password", "show-user"] and
                argv[5] == "--property=UID,GID,Linger", "command_not_allowlisted")
        ident(argv[4])
    elif command == "/usr/bin/id":
        require(len(argv) == 3 and argv[1] in {"-u", "-g", "-Gn"}, "command_not_allowlisted")
        ident(argv[2])
    elif command in {"/usr/bin/ss", "/usr/sbin/ss"}:
        require(argv[1:] == ["-H", "-lntu"], "command_not_allowlisted")
    elif command == "/usr/bin/firewall-cmd":
        if argv[1:] != ["--get-active-zones"]:
            require(len(argv) == 3 and argv[1].startswith("--zone=") and
                    argv[2] in {"--get-target", "--list-services", "--list-ports"}, "command_not_allowlisted")
            ident(argv[1][7:])
    elif command == "/usr/sbin/sshd":
        require(len(argv) == 4 and argv[1:3] == ["-T", "-C"], "command_not_allowlisted")
        match = re.fullmatch(r"user=([^,]+),host=([^,]+),addr=([^,]+)", argv[3])
        require(match is not None, "invalid_ssh_context")
        ident(match[1])
        ident(match[2])
        try:
            ipaddress.ip_address(match[3])
        except ValueError:
            raise Refusal("invalid_ssh_context") from None
    else:
        raise Refusal("command_not_allowlisted")


class CommandRunner:
    """Fixed read-only argv, bounded stdout/stderr, clean child environment."""
    def __init__(self, timeout=4):
        require(type(timeout) in {int, float} and 0 < timeout <= 10, "invalid_command_timeout")
        self.timeout = timeout
        self.deadline = time.monotonic() + 120
        self.calls = 0

    def run(self, argv):
        validate_command(argv)
        self.calls += 1
        if self.calls > 256 or time.monotonic() >= self.deadline:
            return None
        account = pwd.getpwuid(os.getuid())
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C",
                       "HOME": account.pw_dir, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                       "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=environment, start_new_session=True)
        except OSError:
            return None
        selector = selectors.DefaultSelector()
        output = bytearray()
        total = 0
        deadline = min(self.deadline, time.monotonic() + self.timeout)
        try:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                for key, _ in selector.select(min(left, 0.1)):
                    block = os.read(key.fd, 16384)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > COMMAND_LIMIT:
                        return None
                    if key.fileobj is process.stdout:
                        output.extend(block)
            left = deadline - time.monotonic()
            if left <= 0 or process.wait(timeout=left) != 0:
                return None
            try:
                return output.decode("utf-8", errors="strict")
            except UnicodeError:
                return None
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            process.stdout.close()
            process.stderr.close()


def properties(text):
    result = {}
    for line in text.splitlines():
        require("=" in line, "invalid_systemctl_response")
        key, value = line.split("=", 1)
        require(key in SHOW and key not in result, "unexpected_systemctl_property")
        result[key] = value
    return result


def normalized_user(value):
    if value.isdigit():
        try:
            value = pwd.getpwuid(int(value)).pw_name
        except KeyError:
            raise Refusal("unknown_unit_uid") from None
    return ident(value)


def normalized_group(value):
    if value.isdigit():
        try:
            value = grp.getgrgid(int(value)).gr_name
        except KeyError:
            raise Refusal("unknown_unit_gid") from None
    return ident(value)


class LocalCollector:
    def __init__(self, runner=None, fixture_root=None):
        self.runner = runner
        self.fixture_root = Path(fixture_root).absolute() if fixture_root else None

    def show(self, entry):
        if self.runner is None:
            return None
        argv = ["/usr/bin/systemctl", "--no-pager", "--no-ask-password"]
        if entry["manager"] == "user":
            # Never switch identity, start a user manager, or ask polkit/sudo.
            if entry["user"] != pwd.getpwuid(os.getuid()).pw_name:
                return None
            argv.append("--user")
        argv.extend(["show", entry["unit"], "--property=" + ",".join(SHOW)])
        text = self.runner.run(argv)
        return properties(text) if text is not None else None

    def unit_hash(self, path, unit):
        unit_path(path, unit)
        mapped = self.fixture_root / path.lstrip("/") if self.fixture_root else Path(path)
        return hashlib.sha256(open_regular(mapped)).hexdigest()

    def units(self, policy):
        result = {"units": {}}
        if self.runner is None and self.fixture_root is None:
            return result
        for entry in policy["units"]:
            unit_policy(entry)
            props = self.show(entry)
            partial = {}
            if props is not None:
                required = {"FragmentPath", "DropInPaths", "LoadState"}
                if entry["unit"].endswith(".service"):
                    required |= {"User", "Group", "SupplementaryGroups", "NoNewPrivileges",
                                 "MemoryMax", "TasksMax", "ControlGroup", "Delegate"}
                require(required <= set(props), "incomplete_systemctl_response")
                require(props["LoadState"] == "loaded", "installed_unit_missing")
                path = unit_path(props["FragmentPath"], entry["unit"])
                dropins = []
                for raw in props["DropInPaths"].split():
                    unit_path(raw, entry["unit"])
                    dropins.append({"path": raw, "sha256": self.unit_hash(raw, entry["unit"])})
                groups = sorted(normalized_group(group) for group in props.get("SupplementaryGroups", "").split())
                fallback = entry["user"] if entry["manager"] == "user" else "root"
                actual_user = normalized_user(props.get("SocketUser") or props.get("User") or fallback)
                actual_group = normalized_group(props.get("SocketGroup") or props.get("Group") or actual_user)
                partial.update({"fragment": path, "sha256": self.unit_hash(path, entry["unit"]),
                                "dropins": sorted(dropins, key=lambda item: item["path"]),
                                "user": actual_user, "group": actual_group,
                                "supplementary_groups": groups,
                                "load_state": props["LoadState"], "no_new_privileges": props.get("NoNewPrivileges") == "yes",
                                "memory_max": int(props["MemoryMax"]) if props.get("MemoryMax", "").isdigit() else 0,
                                "tasks_max": int(props["TasksMax"]) if props.get("TasksMax", "").isdigit() else 0,
                                "control_group": props.get("ControlGroup", ""), "delegate": props.get("Delegate") == "yes"})
            elif self.fixture_root is not None:
                partial = {"fragment": entry["fragment"], "sha256": self.unit_hash(entry["fragment"], entry["unit"]),
                           "dropins": [{"path": item["path"], "sha256": self.unit_hash(item["path"], entry["unit"])}
                                       for item in entry["dropins"]]}
            if partial:
                result["units"][entry["id"]] = partial
        return result

    def authority(self, policy):
        result = {"identities": {}}
        if self.runner is None:
            return result
        for entry in policy["identities"]:
            ident(entry["user"])
            values = [self.runner.run(["/usr/bin/id", flag, entry["user"]]) for flag in ("-u", "-g", "-Gn")]
            if any(value is None for value in values):
                continue
            require(values[0].strip().isdigit() and values[1].strip().isdigit(), "invalid_id_response")
            groups = sorted(values[2].split())
            for group in groups:
                ident(group)
            observed = {"uid": int(values[0]), "gid": int(values[1]), "groups": groups}
            text = self.runner.run(["/usr/bin/loginctl", "--no-pager", "--no-ask-password", "show-user",
                                    entry["user"], "--property=UID,GID,Linger"])
            if text is not None:
                fields = {}
                for line in text.splitlines():
                    key, separator, value = line.partition("=")
                    require(separator and key in {"UID", "GID", "Linger"} and key not in fields,
                            "invalid_user_manager_response")
                    fields[key] = value
                require(set(fields) == {"UID", "GID", "Linger"} and fields["UID"].isdigit() and
                        fields["GID"].isdigit(), "incomplete_user_manager_response")
                observed["linger"] = fields["Linger"] == "yes"
                require(int(fields["UID"]) == observed["uid"] and int(fields["GID"]) == observed["gid"],
                        "user_manager_identity_mismatch")
            manager = self.show({"manager": "system", "user": entry["user"],
                                 "unit": f"user@{observed['uid']}.service"})
            if manager is not None:
                require({"User", "Group", "SupplementaryGroups", "ControlGroup"} <= set(manager),
                        "incomplete_user_manager_response")
                raw_user = manager["User"]
                ident(raw_user or "root")
                if raw_user.isdigit():
                    observed["user_manager_uid"] = int(raw_user)
                elif raw_user == entry["user"]:
                    observed["user_manager_uid"] = observed["uid"]
                else:
                    observed["user_manager_uid"] = 0
                inherited = self.runner.run(["/usr/bin/id", "-Gn", raw_user or "root"])
                if inherited is not None:
                    manager_groups = set(inherited.split()) | set(manager["SupplementaryGroups"].split())
                    if manager["Group"] and not manager["Group"].isdigit():
                        manager_groups.add(manager["Group"])
                    observed["user_manager_groups"] = sorted(ident(group) for group in manager_groups)
                group_path = manager["ControlGroup"]
                if group_path:
                    require(re.fullmatch(r"/[A-Za-z0-9_@./:\\-]{1,256}", group_path) and
                            ".." not in PurePosixPath(group_path).parts, "unsafe_cgroup_path")
                    path = Path("/sys/fs/cgroup") / group_path.lstrip("/") / "cgroup.controllers"
                    mapped = self.fixture_root / str(path).lstrip("/") if self.fixture_root else path
                    try:
                        controllers = open_regular(mapped, 4096).decode("ascii").split()
                    except OSError:
                        pass  # Capability absence does not imply controller availability.
                    else:
                        require(set(controllers) <= {"cpu", "cpuset", "memory", "pids", "io"},
                                "unexpected_cgroup_controller")
                        observed["controllers"] = sorted(controllers)
            result["identities"][entry["user"]] = observed
        return result

    def timers(self, policy):
        result = {}
        for entry in policy["timers"]:
            props = self.show(entry)
            if props is None:
                continue
            require({"ActiveState", "UnitFileState", "Persistent", "NextElapseUSecMonotonic", "NextElapseUSecRealtime"} <= set(props), "incomplete_timer_response")
            next_values = (props["NextElapseUSecMonotonic"], props["NextElapseUSecRealtime"])
            result[entry["id"]] = {"enabled": props["UnitFileState"] == "enabled", "active": props["ActiveState"] == "active",
                                  "persistent": props["Persistent"] == "yes", "next_elapse_set": any(value not in {"", "0", "n/a", "infinity"} for value in next_values)}
        return result

    def network(self, policy):
        result = {}
        if self.runner is None:
            return result
        text = self.runner.run(["/usr/bin/ss", "-H", "-lntu"])
        if text is None:
            text = self.runner.run(["/usr/sbin/ss", "-H", "-lntu"])
        if text is not None:
            listeners = []
            for line in text.splitlines():
                fields = line.split()
                require(len(fields) == 6 and fields[0] in {"tcp", "udp"}, "invalid_listener_response")
                raw = fields[4]
                address, port = raw.rsplit(":", 1)
                address = address.strip("[]").split("%", 1)[0]
                if address == "*":
                    address = "0.0.0.0"
                require(port.isdigit(), "invalid_listener_response")
                entry = {"protocol": fields[0], "address": address, "port": int(port)}
                endpoint(entry)
                listeners.append(entry)
            result["listeners"] = sorted(listeners, key=lambda item: (item["protocol"], item["address"], item["port"]))
        active = self.runner.run(["/usr/bin/firewall-cmd", "--get-active-zones"])
        if active is not None:
            names = sorted(line.strip() for line in active.splitlines() if line and not line[0].isspace())
            zones = []
            for name in names:
                ident(name)
                values = [self.runner.run(["/usr/bin/firewall-cmd", "--zone=" + name, flag])
                          for flag in ("--get-target", "--list-services", "--list-ports")]
                if any(value is None for value in values):
                    break
                zones.append({"name": name, "target": values[0].strip(), "services": sorted(values[1].split()), "ports": sorted(values[2].split())})
            else:
                result["firewall_zones"] = zones
        contexts = []
        for context in policy["ssh_contexts"]:
            ident(context["user"])
            ident(context["host"])
            try:
                ipaddress.ip_address(context["address"])
            except (ValueError, TypeError):
                raise Refusal("invalid_ssh_context") from None
            text = self.runner.run(["/usr/sbin/sshd", "-T", "-C", "user=" + context["user"] +
                                   ",host=" + context["host"] + ",addr=" + context["address"]])
            if text is None:
                break
            settings = {}
            for line in text.splitlines():
                key, _, value = line.partition(" ")
                if key in SSH_FIELDS:
                    require(key not in settings, "duplicate_ssh_property")
                    settings[key] = value.strip()
            contexts.append({"user": context["user"], "host": context["host"], "address": context["address"], "settings": settings})
        else:
            result["ssh_contexts"] = contexts
        return result

    def collect(self, gate, policy):
        if gate == "installed_units":
            return self.units(policy)
        if gate == "service_authority":
            return self.authority(policy)
        if gate == "concurrent_timers":
            return self.timers(policy)
        if gate == "network_policy":
            return self.network(policy)
        return {}


def corroborate(actual, reported):
    """Unavailable fields cannot erase an observed contradiction."""
    require(isinstance(actual, dict) and isinstance(reported, dict), "invalid_observation")
    for key, value in actual.items():
        require(key in reported, "local_observation_not_in_packet")
        if isinstance(value, dict):
            corroborate(value, reported[key])
        else:
            require(value == reported[key], "local_observation_conflicts_with_packet")


def has_observation(value):
    return any(has_observation(child) if isinstance(child, dict) else True for child in value.values())


def collect(packet=None, trusted_keys=None, local=None, now=None):
    now = now or datetime.now(timezone.utc)
    result = {"schema": SCHEMA, "collected_at": now.isoformat(), "scope": "fixture" if local and local.fixture_root else "host",
              "authentication": {"status": "unavailable", "reason": "packet_missing"},
              "requirements_met": False, "gates": {}}
    if packet is None:
        for gate in GATES:
            result["gates"][gate] = {"status": "unavailable", "requirements_met": False, "reasons": ["explicit_sanitized_packet_required"]}
        return result
    try:
        bounded_tree(packet)
        require(isinstance(packet, dict) and set(packet) in
                ({"schema", "payload"}, {"schema", "payload", "authentication"}), "invalid_packet_envelope")
        require(packet.get("schema") == PACKET_SCHEMA, "unsupported_packet_schema")
        payload = exact(packet["payload"], {"host_id", "host_identity_sha256", "created_at", "expires_at", "origin", "gates"})
        ident(payload["host_id"])
        digest(payload["host_identity_sha256"])
        if local and local.runner is not None and local.fixture_root is None:
            require(hashlib.sha256(open_regular(Path("/etc/machine-id"), 4096)).hexdigest() ==
                    payload["host_identity_sha256"], "host_identity_mismatch")
        origin(payload["origin"])
        created = recent(payload["created_at"], now)
        expires = stamp(payload["expires_at"])
        require(created <= now < expires <= created + timedelta(seconds=MAX_AGE), "packet_expired_or_excessive_lifetime")
        require(isinstance(payload["gates"], dict) and set(payload["gates"]) <= set(GATES), "unknown_gate")
        auth = authenticate(packet, trusted_keys or {})
        result["authentication"] = auth
        result["host_id"] = payload["host_id"]
        result["host_identity_sha256"] = payload["host_identity_sha256"]
        result["packet_payload_sha256"] = sha(payload)
        result["origin"] = payload["origin"]
    except (Refusal, OSError, KeyError, TypeError, ValueError) as error:
        code = str(error) if isinstance(error, Refusal) else "invalid_packet"
        for gate in GATES:
            result["gates"][gate] = {"status": "refused", "requirements_met": False, "reasons": [code]}
        return result
    authenticated = auth["status"] == "authenticated_reported"
    for gate in GATES:
        record = {"status": "unavailable", "requirements_met": False, "reasons": []}
        result["gates"][gate] = record
        entry = payload["gates"].get(gate)
        if entry is None:
            record["reasons"] = ["gate_evidence_missing"]
            continue
        try:
            exact(entry, {"policy", "facts", "references"})
            refs = references(entry["references"], now)
            VALIDATORS[gate](entry["policy"], entry["facts"], refs, now)
            actual = local.collect(gate, entry["policy"]) if local else {}
            corroborate(actual, entry["facts"])
            record.update({"status": "observed", "source": auth["status"], "policy_sha256": sha(entry["policy"]),
                           "facts_sha256": sha(entry["facts"]), "references": list(refs.values()),
                           "local_observation_sha256": sha(actual), "local_fields_observed": has_observation(actual),
                           "local_observation_status": "observed" if has_observation(actual) else "unavailable",
                           "requirements_met": authenticated and result["scope"] != "fixture"})
            # Only validated allowlisted facts are emitted; invalid input stays out.
            record["facts"] = entry["facts"]
            if not authenticated:
                record["reasons"].append("authenticated_operator_evidence_required")
            if result["scope"] == "fixture":
                record["reasons"].append("fixture_is_not_host_attestation")
        except (Refusal, OSError, KeyError, TypeError, ValueError) as error:
            record["status"] = "unavailable" if isinstance(error, (PermissionError, FileNotFoundError)) else "refused"
            record["reasons"] = [str(error) if isinstance(error, Refusal) else "local_capability_or_evidence_unavailable"]
    # Recovery must bind to the actual retained off-host artifacts and scope.
    backup, restore = payload["gates"].get("backup_coverage"), payload["gates"].get("restore_objectives")
    if backup and restore and all(result["gates"][key]["status"] == "observed" for key in ("backup_coverage", "restore_objectives")):
        if backup["policy"] != restore["policy"] or any(
                restore["facts"][env]["artifacts"][family] != backup["facts"][env]["artifacts"][family]["ciphertext_sha256"]
                for env in ENVIRONMENTS for family in ("database", "state")):
            result["gates"]["restore_objectives"].update(status="refused", requirements_met=False,
                                                       reasons=["restore_not_bound_to_off_host_backup"])
    # Unit/timer claims must refer to the very identities reviewed for each environment.
    if all(result["gates"][gate]["status"] == "observed" for gate in ("installed_units", "service_authority", "concurrent_timers")):
        allowed = {env: {entry["user"] for entry in payload["gates"]["service_authority"]["policy"]["identities"]
                         if entry["environment"] == env} for env in ENVIRONMENTS}
        for gate, field in (("installed_units", "units"), ("concurrent_timers", "timers")):
            entries = payload["gates"][gate]["policy"][field]
            if any(entry["user"] not in allowed[entry["environment"]] for entry in entries
                   if entry["role"] != "security_update"):
                result["gates"][gate].update(status="refused", requirements_met=False,
                                            reasons=["installed_identity_not_in_authority_inventory"])
        installed = {(entry["environment"], entry["manager"], entry["user"], entry["unit"])
                     for entry in payload["gates"]["installed_units"]["policy"]["units"]}
        if any((entry["environment"], entry["manager"], entry["user"], entry["unit"]) not in installed
               for entry in payload["gates"]["concurrent_timers"]["policy"]["timers"]
               if entry["role"] != "security_update"):
            result["gates"]["concurrent_timers"].update(status="refused", requirements_met=False,
                                                      reasons=["timer_installed_hash_parity_missing"])
    result["requirements_met"] = all(record["requirements_met"] for record in result["gates"].values())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packet", type=Path, help="explicit sanitized .json packet; never a gateway config")
    parser.add_argument("--trusted-key", action="append", default=[], metavar="KEY_ID=PUBLIC_KEY_HEX")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--observe-local", action="store_true", help="opt into fixed read-only host probes")
    mode.add_argument("--fixture-root", type=Path, help="unit-only disposable root; no subprocesses; never release-ready")
    args = parser.parse_args(argv)
    try:
        keys = {}
        for raw in args.trusted_key:
            name, separator, public = raw.partition("=")
            ident(name)
            require(separator and re.fullmatch(r"[0-9a-f]{64}", public) and name not in keys, "invalid_trusted_key")
            keys[name] = bytes.fromhex(public)
        packet = read_packet(args.packet) if args.packet else None
        local = LocalCollector(CommandRunner() if args.observe_local else None, args.fixture_root)
        report = collect(packet, keys, local)
    except (Refusal, OSError, ValueError) as error:
        report = {"schema": SCHEMA, "requirements_met": False,
                  "gates": {gate: {"status": "refused", "requirements_met": False,
                                    "reasons": [str(error) if isinstance(error, Refusal) else "packet_unavailable"]} for gate in GATES}}
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if report["requirements_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
