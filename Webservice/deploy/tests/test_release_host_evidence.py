"""Offline P3 fixtures: exact claims, actual disposable files, real signatures.

No production host probes, provider calls, credentials or customer data. A valid
signed fixture demonstrates packet validation, not an operational attestation.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_host_evidence", DEPLOY / "collect_release_host_evidence.py")
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)
NOW = datetime.now(timezone.utc).replace(microsecond=0)
UNIT_BYTES = b"[Service]\nEnvironment=FIXTURE_SECRET_MUST_NOT_APPEAR\n"
UNIT_HASH = hashlib.sha256(UNIT_BYTES).hexdigest()
DROPIN_BYTES = b"[Service]\nEnvironment=FIXTURE_DROPIN_SECRET\n"
DROPIN_HASH = hashlib.sha256(DROPIN_BYTES).hexdigest()


def hash_id(value):
    return hashlib.sha256(value.encode()).hexdigest()


def date(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def ref(key, kind="independent-operator", value=None):
    return {"id": key, "sha256": value or hash_id(key), "origin": {"kind": kind, "id": "fixture-origin"}, "observed_at": date(-10)}


def record(policy, facts, refs=None):
    return {"policy": policy, "facts": facts, "references": refs or [ref("operator")]}


def packet_fixture():
    units, unit_facts = [], {}
    authority, identities = [], {}
    for index, env in enumerate(("prod", "staging")):
        user = "fixture-" + env
        uid = 2000 + index
        authority.append({"user": user, "environment": env, "uid": uid, "gid": uid, "groups": [user]})
        identities[user] = {"uid": uid, "gid": uid, "groups": [user], "sudo_allowed": False,
                            "user_manager_uid": uid, "user_manager_groups": [user], "user_manager_sudo_allowed": False,
                            "linger": True, "controllers": ["cpu", "memory", "pids"], "memory_max": 2**30,
                            "tasks_max": 256, "review_reference": "operator"}
        for role in sorted(host.ROLES):
            name = f"fixture-{env}-{role}." + ("socket" if role.endswith("socket") else "service")
            path = "/etc/systemd/system/" + name
            dropins = [{"path": path + ".d/10-security.conf", "sha256": DROPIN_HASH}]
            key = env + "-" + role
            units.append({"id": key, "environment": env, "role": role, "manager": "system", "user": user,
                          "unit": name, "fragment": path, "sha256": UNIT_HASH, "dropins": dropins})
            unit_facts[key] = {"fragment": path, "sha256": UNIT_HASH, "dropins": copy.deepcopy(dropins), "user": user,
                               "group": user, "supplementary_groups": [], "load_state": "loaded", "no_new_privileges": True,
                               "memory_max": 2**30, "tasks_max": 256, "control_group": "/fixture/" + key, "delegate": True}
    units_record = record({"inventory_complete": True, "units": units}, {"units": unit_facts})
    authority_record = record({"inventory_complete": True, "identities": authority}, {"identities": identities})
    contexts = [{"user": "operator", "host": "fixture-host", "address": "192.0.2.2",
                 "settings": {"passwordauthentication": "no", "permitrootlogin": "no", "pubkeyauthentication": "yes",
                              "permituserenvironment": "no", "allowtcpforwarding": "no"}}]
    zones = [{"name": "public", "target": "default", "services": ["ssh"], "ports": []}]
    listeners = [{"protocol": "tcp", "address": "127.0.0.1", "port": 8300},
                 {"protocol": "tcp", "address": "127.0.0.1", "port": 8310},
                 {"protocol": "tcp", "address": "0.0.0.0", "port": 22}]
    listeners.sort(key=lambda item: (item["protocol"], item["address"], item["port"]))
    network_record = record({"public_ports": [22], "firewall_zones": zones, "ssh_contexts": contexts},
                            {"listeners": listeners, "firewall_zones": copy.deepcopy(zones), "ssh_contexts": copy.deepcopy(contexts),
                             "external_scan_ports": [22], "external_scan_reference": "operator", "firewall_review_reference": "operator"})
    runtime_record = record({"staging_publication_allowed": False},
                            {"environments": {env: {"terminal_enabled": False, "provider_sharing": False,
                             "proxy_trust": ["127.0.0.1/32"], "edge_origin": "127.0.0.1:" + str(8300 if env == "prod" else 8310),
                             "edge_access_enforced": True} for env in ("prod", "staging")},
                             "config_inventory_complete": True, "production_staging_separate": True, "staging_published": False,
                             "staging_approval_reference": None, "edge_reference": "edge", "settings_reference": "operator"},
                            [ref("operator"), ref("edge", "control-plane")])
    credentials, credential_facts = [], {}
    for kind in ("cloudflare_api", "r2_s3", "tunnel", "session_signing", "oauth", "provider", "entitlement"):
        consumer = "consumer-" + kind
        credentials.append({"id": kind, "kind": kind, "consumers": [consumer], "exposed_at": date(-5000), "history_reference": "history"})
        provider_ref = "provider" if kind in {"cloudflare_api", "r2_s3", "tunnel", "oauth", "provider"} else "operator"
        credential_facts[kind] = {"old_identity_sha256": hash_id(kind + "-old"), "new_identity_sha256": hash_id(kind + "-new"),
                                  "revoked_at": date(-3000), "rotated_at": date(-3000), "revocation_reference": provider_ref,
                                  "rotation_reference": provider_ref, "consumers": [{"id": consumer, "identity_sha256": hash_id(kind + "-new"),
                                  "updated_at": date(-2900), "reference": "operator"}]}
    windows = {path: {"start": date(-5000), "end": date(-2000), "reference": "history"}
               for path in ("engine_rpc", "oauth_preregistration", "account_status_override")}
    reviews = {path: {"start": date(-5000), "end": date(-2000), "reviewed_at": date(-1000),
                     "disposition": "no_findings", "reference": "history"} for path in windows}
    credential_record = record({"consumer_inventory_complete": True, "credentials": credentials, "exposure_windows": windows},
                               {"credentials": credential_facts, "log_reviews": reviews, "sessions_revoked": True, "identity_recovery_reviewed": True},
                               [ref("operator"), ref("provider", "provider"), ref("history", "incident-review")])
    backup_policy = {"inventory_complete": True, "environments": {}}
    backups, restores, backup_refs = {}, {}, [ref("operator")]
    for env in ("prod", "staging"):
        roots = {label: hash_id(env + "-" + label) for label in ("data", "workspace", "bucket", "cells", "university")}
        backup_policy["environments"][env] = {"database_id": "fixture-" + env, "roots": roots,
                                               "institutional_required": True, "rpo_seconds": 3600, "rto_seconds": 600}
        manifest = hash_id(env + "-manifest")
        manifest_ref = env + "-manifest"
        backup_refs.append(ref(manifest_ref, value=manifest))
        artifacts = {}
        for family in ("database", "state"):
            key = env + "-" + family
            artifact_hash = hash_id(key + "-encrypted")
            artifacts[family] = {"ciphertext_sha256": artifact_hash, "remote_sha256": artifact_hash,
                                 "encryption": "age", "off_host": True, "destination_id": "fixture-off-host-" + env,
                                 "completed_at": date(-1000), "reference": key}
            backup_refs.append(ref(key, "provider", artifact_hash))
        backups[env] = {"database_id": "fixture-" + env, "roots": copy.deepcopy(roots), "mount_inventory_complete": True,
                        "credentials_separate_protected": True, "manifest_sha256": manifest, "manifest_reference": manifest_ref,
                        "artifacts": artifacts, "credential_protection_reference": "operator"}
        restores[env] = {"artifacts": {family: artifact["ciphertext_sha256"] for family, artifact in artifacts.items()},
                         "restored_roots": copy.deepcopy(roots), "target_root_sha256": hash_id(env + "-isolated"),
                         "target_database_id": "fixture-restore-" + env, "isolated": True, "schema_compatible": True,
                         "source_rollback": "database_restore_required", "rollback_drill_complete": True,
                         "started_at": date(-600), "completed_at": date(-300), "recovery_point_at": date(-1000),
                         "duration_seconds": 300, "reference": "operator"}
    timer_policy, timer_facts = [], {}
    for env in ("prod", "staging"):
        for role in ("release_retention", "security_retention", "backup", "security_update"):
            key = env + "-" + role
            timer_policy.append({"id": key, "environment": env, "role": role, "manager": "system" if role == "security_update" else "user", "user": "fixture-" + env,
                                 "unit": "fixture-" + key + ".timer", "max_last_run_age_seconds": 86400})
            timer_facts[key] = {"enabled": True, "active": True, "persistent": True, "next_elapse_set": True,
                                "last_success_at": date(-1000), "reference": "operator"}
            if role != "security_update":
                unit_name = timer_policy[-1]["unit"]
                unit_path = "/etc/systemd/user/" + unit_name
                unit_key = key + "-timer"
                drops = [{"path": unit_path + ".d/10-security.conf", "sha256": DROPIN_HASH}]
                units.append({"id": unit_key, "environment": env, "role": role + "_timer", "manager": "user",
                              "user": "fixture-" + env, "unit": unit_name, "fragment": unit_path,
                              "sha256": UNIT_HASH, "dropins": drops})
                unit_facts[unit_key] = {"fragment": unit_path, "sha256": UNIT_HASH, "dropins": copy.deepcopy(drops),
                                       "user": "fixture-" + env, "group": "fixture-" + env, "supplementary_groups": [],
                                       "load_state": "loaded", "no_new_privileges": True, "memory_max": 2**30,
                                       "tasks_max": 256, "control_group": "/fixture/" + unit_key, "delegate": True}
    timers_record = record({"timers": timer_policy, "security_updates_applied": True, "security_updates_reference": "operator"}, timer_facts)
    return {"schema": host.PACKET_SCHEMA, "payload": {"host_id": "fixture-host", "host_identity_sha256": hash_id("fixture-machine-id"),
            "created_at": date(-20), "expires_at": date(86400), "origin": {"kind": "independent-operator", "id": "fixture-operator"},
            "gates": {"installed_units": units_record, "service_authority": authority_record, "network_policy": network_record,
                      "runtime_settings": runtime_record, "historical_credentials": credential_record,
                      "backup_coverage": record(backup_policy, backups, backup_refs),
                      "restore_objectives": record(copy.deepcopy(backup_policy), restores), "concurrent_timers": timers_record}}}


@pytest.fixture
def packet():
    return packet_fixture()


@pytest.fixture
def signing():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    private = crypto.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(packet):
        value = copy.deepcopy(packet)
        signature = private.sign(host.SIGNING_DOMAIN + host.canonical(value["payload"]))
        value["authentication"] = {"algorithm": "ed25519", "key_id": "fixture-key", "signature": signature.hex()}
        return value

    return sign, {"fixture-key": public}


class FixtureRunner:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def run(self, argv):
        self.calls.append(tuple(argv))
        return self.responses.get(tuple(argv))


def materialize(root, packet):
    for unit in packet["payload"]["gates"]["installed_units"]["policy"]["units"]:
        path = root / unit["fragment"].lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(UNIT_BYTES)
        for dropin in unit["dropins"]:
            path = root / dropin["path"].lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(DROPIN_BYTES)


def unit_responses(packet):
    responses = {}
    for unit in packet["payload"]["gates"]["installed_units"]["policy"]["units"]:
        argv = ("/usr/bin/systemctl", "--no-pager", "--no-ask-password", "show", unit["unit"], "--property=" + ",".join(host.SHOW))
        facts = packet["payload"]["gates"]["installed_units"]["facts"]["units"][unit["id"]]
        values = {"LoadState": "loaded", "User": unit["user"], "Group": unit["user"], "SupplementaryGroups": "",
                  "FragmentPath": unit["fragment"], "DropInPaths": " ".join(item["path"] for item in unit["dropins"]),
                  "NoNewPrivileges": "yes", "MemoryMax": str(facts["memory_max"]), "TasksMax": str(facts["tasks_max"]),
                  "ControlGroup": facts["control_group"], "Delegate": "yes"}
        responses[argv] = "\n".join(key + "=" + value for key, value in values.items()) + "\n"
    return responses


def test_complete_pinned_packet_validates_each_gate(packet, signing):
    sign, keys = signing
    report = host.collect(sign(packet), keys, now=NOW)
    assert report["requirements_met"] is True
    assert report["authentication"]["status"] == "authenticated_reported"
    assert all(gate["status"] == "observed" for gate in report["gates"].values())
    assert report["packet_payload_sha256"] == host.sha(packet["payload"])


def test_hashes_and_unsigned_source_templates_are_not_attestation(packet):
    report = host.collect(packet, now=NOW)
    assert report["requirements_met"] is False
    assert report["authentication"]["status"] == "unsigned_reported"
    assert all(not gate["requirements_met"] for gate in report["gates"].values())


def test_signature_tampering_and_unpinned_key_fail_closed(packet, signing):
    sign, keys = signing
    value = sign(packet)
    value["payload"]["host_id"] = "another-host"
    report = host.collect(value, keys, now=NOW)
    assert all(gate["status"] == "refused" for gate in report["gates"].values())
    report = host.collect(sign(packet), {}, now=NOW)
    assert not report["requirements_met"]
    assert report["authentication"]["reason"] == "signer_not_pinned"


@pytest.mark.parametrize("gate,mutate", [
    ("installed_units", lambda item: item["facts"]["units"]["prod-gateway"].update(sha256=hash_id("wrong-unit"))),
    ("installed_units", lambda item: item["facts"]["units"]["prod-gateway"]["dropins"][0].update(sha256=hash_id("wrong-dropin"))),
    ("installed_units", lambda item: item["facts"]["units"]["prod-gateway"].update(no_new_privileges=False)),
    ("installed_units", lambda item: item["facts"]["units"]["prod-gateway"].update(supplementary_groups=["wheel"])),
    ("service_authority", lambda item: item["facts"]["identities"]["fixture-prod"].update(sudo_allowed=True)),
    ("service_authority", lambda item: item["facts"]["identities"]["fixture-prod"].update(user_manager_uid=0)),
    ("service_authority", lambda item: item["facts"]["identities"]["fixture-prod"].update(user_manager_groups=["wheel"])),
    ("service_authority", lambda item: item["facts"]["identities"]["fixture-prod"].update(controllers=["cpu", "pids"])),
    ("network_policy", lambda item: item["facts"]["listeners"].append({"protocol": "tcp", "address": "0.0.0.0", "port": 8300})),
    ("network_policy", lambda item: item["facts"].update(external_scan_ports=[22, 5432])),
    ("network_policy", lambda item: item["facts"]["ssh_contexts"][0]["settings"].update(passwordauthentication="yes")),
    ("runtime_settings", lambda item: item["facts"]["environments"]["prod"].update(terminal_enabled=True)),
    ("runtime_settings", lambda item: item["facts"]["environments"]["staging"].update(provider_sharing=True)),
    ("runtime_settings", lambda item: item["facts"]["environments"]["prod"].update(proxy_trust=["0.0.0.0/0"])),
    ("runtime_settings", lambda item: item["facts"].update(staging_published=True)),
    ("backup_coverage", lambda item: item["facts"]["prod"]["roots"].pop("workspace")),
    ("backup_coverage", lambda item: item["policy"]["environments"]["prod"]["roots"].pop("university")),
    ("backup_coverage", lambda item: item["facts"]["staging"]["artifacts"]["state"].update(off_host=False)),
    ("backup_coverage", lambda item: item["facts"]["prod"]["artifacts"]["database"].update(encryption="plaintext")),
    ("backup_coverage", lambda item: item["facts"]["prod"].update(credentials_separate_protected=False)),
    ("backup_coverage", lambda item: item["policy"]["environments"]["prod"].pop("rpo_seconds")),
    ("restore_objectives", lambda item: item["policy"]["environments"]["prod"].pop("rto_seconds")),
    ("restore_objectives", lambda item: item["facts"]["prod"].update(isolated=False)),
    ("restore_objectives", lambda item: item["facts"]["prod"].update(schema_compatible=False)),
    ("restore_objectives", lambda item: item["facts"]["prod"].update(recovery_point_at=date(-10000))),
    ("restore_objectives", lambda item: item["facts"]["prod"].update(started_at=date(-1500), duration_seconds=1200)),
    ("restore_objectives", lambda item: item["facts"]["prod"].update(rollback_drill_complete=False)),
    ("concurrent_timers", lambda item: item["facts"]["staging-backup"].update(active=False)),
    ("concurrent_timers", lambda item: item["facts"]["prod-security_update"].update(enabled=False)),
    ("concurrent_timers", lambda item: item["facts"]["prod-release_retention"].update(last_success_at=date(-100000))),
    ("historical_credentials", lambda item: item["facts"]["credentials"]["cloudflare_api"].update(new_identity_sha256=hash_id("cloudflare_api-old"))),
    ("historical_credentials", lambda item: item["facts"]["credentials"]["r2_s3"].update(revoked_at=None)),
    ("historical_credentials", lambda item: item["facts"]["credentials"]["tunnel"]["consumers"].clear()),
    ("historical_credentials", lambda item: item["facts"]["log_reviews"]["engine_rpc"].update(start=date(-4000))),
    ("historical_credentials", lambda item: item["facts"]["log_reviews"]["oauth_preregistration"].update(disposition="unreviewed")),
    ("historical_credentials", lambda item: item["facts"].update(sessions_revoked=False)),
])
def test_signed_bad_operational_claims_cannot_pass(packet, signing, gate, mutate):
    sign, keys = signing
    mutate(packet["payload"]["gates"][gate])
    report = host.collect(sign(packet), keys, now=NOW)
    assert report["gates"][gate]["status"] == "refused"
    assert not report["requirements_met"]


def test_restore_artifacts_must_bind_to_off_host_backups(packet, signing):
    sign, keys = signing
    packet["payload"]["gates"]["restore_objectives"]["facts"]["prod"]["artifacts"]["state"] = hash_id("different-backup")
    report = host.collect(sign(packet), keys, now=NOW)
    assert report["gates"]["restore_objectives"]["reasons"] == ["restore_not_bound_to_off_host_backup"]
    assert not report["requirements_met"]


def test_actual_mismatching_dropin_refuses_authenticated_claim(packet, signing, tmp_path):
    sign, keys = signing
    materialize(tmp_path, packet)
    runner = FixtureRunner(unit_responses(packet))
    dropin = packet["payload"]["gates"]["installed_units"]["policy"]["units"][0]["dropins"][0]
    (tmp_path / dropin["path"].lstrip("/")).write_bytes(b"[Service]\nUser=root\n")
    report = host.collect(sign(packet), keys, host.LocalCollector(runner, tmp_path), NOW)
    assert report["gates"]["installed_units"]["status"] == "refused"
    assert "FIXTURE_SECRET" not in json.dumps(report)
    assert "User=root" not in json.dumps(report)


def test_actual_unexpected_dropin_cannot_be_hidden(packet, signing, tmp_path):
    sign, keys = signing
    materialize(tmp_path, packet)
    responses = unit_responses(packet)
    first = next(iter(responses))
    unit = packet["payload"]["gates"]["installed_units"]["policy"]["units"][0]
    extra = unit["fragment"] + ".d/99-privileged.conf"
    path = tmp_path / extra.lstrip("/")
    path.write_bytes(b"[Service]\nNoNewPrivileges=no\n")
    responses[first] = responses[first].replace("DropInPaths=", "DropInPaths=" + extra + " ")
    report = host.collect(sign(packet), keys, host.LocalCollector(FixtureRunner(responses), tmp_path), NOW)
    assert report["gates"]["installed_units"]["status"] == "refused"


def test_actual_system_service_with_implicit_root_cannot_inherit_claimed_user(packet, signing, tmp_path):
    sign, keys = signing
    materialize(tmp_path, packet)
    responses = unit_responses(packet)
    first = next(iter(responses))
    responses[first] = responses[first].replace("User=fixture-prod", "User=")
    report = host.collect(sign(packet), keys, host.LocalCollector(FixtureRunner(responses), tmp_path), NOW)
    assert report["gates"]["installed_units"]["status"] == "refused"


def test_actual_privileged_membership_refuses_clean_signed_claim(packet, signing, tmp_path):
    sign, keys = signing
    materialize(tmp_path, packet)
    responses = {("/usr/bin/id", "-u", "fixture-prod"): "2000\n",
                 ("/usr/bin/id", "-g", "fixture-prod"): "2000\n",
                 ("/usr/bin/id", "-Gn", "fixture-prod"): "fixture-prod wheel\n"}
    runner = FixtureRunner(responses)
    report = host.collect(sign(packet), keys, host.LocalCollector(runner, tmp_path), NOW)
    assert report["gates"]["service_authority"]["status"] == "refused"
    assert all(argv[0] not in {"sudo", "/usr/bin/sudo"} for argv in runner.calls)


def test_capability_absence_is_not_an_observed_local_pass(packet, tmp_path):
    materialize(tmp_path, packet)
    report = host.collect(packet, local=host.LocalCollector(FixtureRunner(), tmp_path), now=NOW)
    assert not report["requirements_met"]
    assert report["gates"]["service_authority"]["source"] == "unsigned_reported"
    assert report["scope"] == "fixture"


def test_unit_paths_cannot_redirect_to_secret_environment(packet, tmp_path):
    materialize(tmp_path, packet)
    unit = packet["payload"]["gates"]["installed_units"]["policy"]["units"][0]
    unit["fragment"] = "/etc/chudware/gateway.env"
    report = host.collect(packet, local=host.LocalCollector(fixture_root=tmp_path), now=NOW)
    assert report["gates"]["installed_units"]["reasons"] == ["non_unit_file_refused"]


def test_symlinked_unit_is_not_opened(packet, tmp_path):
    materialize(tmp_path, packet)
    unit = packet["payload"]["gates"]["installed_units"]["policy"]["units"][0]
    path = tmp_path / unit["fragment"].lstrip("/")
    path.unlink()
    path.symlink_to("/proc/self/environ")
    report = host.collect(packet, local=host.LocalCollector(fixture_root=tmp_path), now=NOW)
    assert report["gates"]["installed_units"]["status"] == "refused"


def test_unknown_secret_fields_never_reach_output(packet):
    packet["payload"]["gates"]["runtime_settings"]["facts"]["secret"] = "FIXTURE_SECRET_FIELD"
    report = host.collect(packet, now=NOW)
    assert report["gates"]["runtime_settings"]["status"] == "refused"
    assert "FIXTURE_SECRET_FIELD" not in json.dumps(report)


def test_packet_rejects_duplicate_keys_and_unsafe_sources(tmp_path):
    path = tmp_path / "packet.json"
    path.write_text('{"schema":"one","schema":"two","payload":{}}')
    with pytest.raises(host.Refusal, match="duplicate_json_key"):
        host.read_packet(path)
    with pytest.raises(host.Refusal, match="sanitized_json_packet_required"):
        host.read_packet(tmp_path / "gateway.env")
    path.write_bytes(b"x" * (host.LIMIT + 1))
    with pytest.raises(host.Refusal, match="unsafe_or_oversized_file"):
        host.read_packet(path)


def test_stale_packet_and_reference_do_not_pass(packet):
    packet["payload"]["expires_at"] = date(-1)
    report = host.collect(packet, now=NOW)
    assert not report["requirements_met"]
    assert all(record["status"] == "refused" for record in report["gates"].values())
    packet = packet_fixture()
    packet["payload"]["gates"]["runtime_settings"]["references"][0]["observed_at"] = date(-host.MAX_AGE - 1)
    report = host.collect(packet, now=NOW)
    assert report["gates"]["runtime_settings"]["status"] == "refused"


def tree_snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
            for path in root.rglob("*")}


def test_cli_disposable_root_is_read_only_and_nonzero(packet, tmp_path):
    materialize(tmp_path, packet)
    sanitized = tmp_path / "sanitized-packet.json"
    sanitized.write_text(json.dumps(packet))
    before = tree_snapshot(tmp_path)
    result = subprocess.run([sys.executable, "-B", str(DEPLOY / "collect_release_host_evidence.py"),
                             "--packet", str(sanitized), "--fixture-root", str(tmp_path)],
                            capture_output=True, text=True, timeout=10,
                            env={"PATH": os.defpath, "LANG": "C"})
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert not report["requirements_met"] and report["scope"] == "fixture"
    assert tree_snapshot(tmp_path) == before
    assert not result.stderr
    assert "FIXTURE_SECRET" not in result.stdout


def test_cli_missing_evidence_has_all_p3_gates_and_no_mutation(tmp_path):
    before = tree_snapshot(tmp_path)
    result = subprocess.run([sys.executable, "-B", str(DEPLOY / "collect_release_host_evidence.py"),
                             "--fixture-root", str(tmp_path)], capture_output=True, text=True, timeout=10,
                            env={"PATH": os.defpath, "LANG": "C"})
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert set(report["gates"]) == set(host.GATES)
    assert all(record["status"] == "unavailable" for record in report["gates"].values())
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("argv", [
    ["/usr/bin/systemctl", "restart", "sshd.service"],
    ["/usr/bin/systemctl", "--no-pager", "--no-ask-password", "show", "-Hremote.service", "--property=" + ",".join(host.SHOW)],
    ["/usr/bin/systemctl", "--no-pager", "--no-ask-password", "show", "sshd.service", "--property=Environment"],
    ["/usr/bin/firewall-cmd", "--reload"],
    ["/usr/bin/id", "-Gn", "--help"],
    ["/usr/sbin/sshd", "-T", "-f", "/tmp/secret.env"],
    ["/usr/bin/sudo", "-n", "true"],
])
def test_runner_rejects_mutating_or_broadened_commands_without_spawning(argv):
    with pytest.raises(host.Refusal):
        host.validate_command(argv)


def test_local_user_manager_root_inheritance_refuses_clean_claim(packet, signing, tmp_path):
    sign, keys = signing
    materialize(tmp_path, packet)
    responses = {("/usr/bin/id", "-u", "fixture-prod"): "2000\n",
                 ("/usr/bin/id", "-g", "fixture-prod"): "2000\n",
                 ("/usr/bin/id", "-Gn", "fixture-prod"): "fixture-prod\n",
                 ("/usr/bin/id", "-Gn", "0"): "root wheel\n"}
    manager_argv = ("/usr/bin/systemctl", "--no-pager", "--no-ask-password", "show",
                    "user@2000.service", "--property=" + ",".join(host.SHOW))
    responses[manager_argv] = "User=0\nGroup=root\nSupplementaryGroups=wheel\nControlGroup=\n"
    report = host.collect(sign(packet), keys, host.LocalCollector(FixtureRunner(responses), tmp_path), NOW)
    assert report["gates"]["service_authority"]["status"] == "refused"
    assert not report["requirements_met"]


def test_local_network_probe_exposes_contradiction_without_raw_sshd_text(packet, signing, tmp_path):
    sign, keys = signing
    materialize(tmp_path, packet)
    responses = {("/usr/bin/ss", "-H", "-lntu"):
                 "tcp LISTEN 0 128 0.0.0.0:8300 0.0.0.0:*\n"
                 "tcp LISTEN 0 128 127.0.0.1:8310 0.0.0.0:*\n",
                 ("/usr/sbin/sshd", "-T", "-C", "user=operator,host=fixture-host,addr=192.0.2.2"):
                 "passwordauthentication no\npermitrootlogin no\npubkeyauthentication yes\n"
                 "permituserenvironment no\nallowtcpforwarding no\nbanner FIXTURE_UNRESTRICTED_SSHD_TEXT\n"}
    report = host.collect(sign(packet), keys, host.LocalCollector(FixtureRunner(responses), tmp_path), NOW)
    assert report["gates"]["network_policy"]["status"] == "refused"
    assert "FIXTURE_UNRESTRICTED_SSHD_TEXT" not in json.dumps(report)


def test_restore_cannot_target_other_environment_customer_root(packet, signing):
    sign, keys = signing
    gate = packet["payload"]["gates"]["restore_objectives"]
    gate["facts"]["prod"]["target_root_sha256"] = gate["policy"]["environments"]["staging"]["roots"]["workspace"]
    report = host.collect(sign(packet), keys, now=NOW)
    assert report["gates"]["restore_objectives"]["status"] == "refused"


def test_gate_cannot_substitute_unreviewed_service_identity(packet, signing):
    sign, keys = signing
    gate = packet["payload"]["gates"]["installed_units"]
    gate["policy"]["units"][0]["user"] = "unreviewed-user"
    gate["facts"]["units"][gate["policy"]["units"][0]["id"]]["user"] = "unreviewed-user"
    report = host.collect(sign(packet), keys, now=NOW)
    assert report["gates"]["installed_units"]["reasons"] == ["installed_identity_not_in_authority_inventory"]


def test_missing_local_capabilities_are_explicitly_unavailable(packet):
    report = host.collect(packet, local=host.LocalCollector(), now=NOW)
    assert report["gates"]["service_authority"]["local_observation_status"] == "unavailable"
    assert report["gates"]["service_authority"]["local_fields_observed"] is False
    assert not report["requirements_met"]


def test_active_timers_without_installed_hash_parity_cannot_pass(packet, signing):
    sign, keys = signing
    gate = packet["payload"]["gates"]["installed_units"]
    gate["policy"]["units"] = [entry for entry in gate["policy"]["units"] if not entry["unit"].endswith(".timer")]
    gate["facts"]["units"] = {key: value for key, value in gate["facts"]["units"].items() if not key.endswith("-timer")}
    report = host.collect(sign(packet), keys, now=NOW)
    assert report["gates"]["concurrent_timers"]["reasons"] == ["timer_installed_hash_parity_missing"]
