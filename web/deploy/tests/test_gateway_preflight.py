"""Actual offline operator CLI behavior with disposable protected configuration."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
HELPER = DEPLOY / "gateway_preflight.py"
SPEC = importlib.util.spec_from_file_location("gateway_preflight", HELPER)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def configuration(tmp_path, **overrides):
    values = {
        "CHUDWARE_DEPLOY_ENV": "prod", "CHUDWARE_WEB_DEV": "0",
        "CHUDWARE_DB_URL": "postgresql://fixture:fixture-db-password@127.0.0.1/fixture",
        "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"m" * 32).decode(),
        "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"p" * 32).decode(),
        "CHUDWARE_MAIL_BACKEND": "smtp", "CHUDWARE_SMTP_HOST": "127.0.0.1",
        "CHUDWARE_SMTP_PORT": "587", "CHUDWARE_SMTP_TLS": "starttls",
        "CHUDWARE_SMTP_USER": "fixture", "CHUDWARE_SMTP_PASS": "fixture-smtp-password",
    }
    for label, name in (("data", "CHUDWARE_WEB_DATA_ROOT"), ("workspace", "CHUDWARE_WORKSPACE_ROOT"),
                        ("bucket", "CHUDWARE_WEB_BUCKET_ROOT"), ("cells", "CHUDWARE_CELLS_ROOT")):
        root = tmp_path / label
        root.mkdir(exist_ok=True)
        values[name] = str(root)
    values.update(overrides)
    return values


def environment_file(tmp_path, values):
    path = tmp_path / "gateway.env"
    path.write_text("".join(name + "=" + value + "\n" for name, value in values.items()))
    path.chmod(0o600)
    return path


def invoke(path, command="validate", *extra, environment="prod", ambient=None):
    return subprocess.run(
        [sys.executable, "-I", str(HELPER), command, "--env-file", str(path),
         "--environment", environment, *extra],
        env={"PATH": os.defpath, **(ambient or {})}, capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("name", [preflight.MAIL_KEY, preflight.PROVIDER_KEY])
@pytest.mark.parametrize("invalid", ["", "a" * 64, "%%%", base64.urlsafe_b64encode(b"short").decode()])
def test_missing_or_wrong_format_key_refuses_without_importing_auth_or_accepting_ambient(tmp_path, name, invalid):
    values = configuration(tmp_path, **{name: invalid})
    result = invoke(environment_file(tmp_path, values), ambient={name: base64.urlsafe_b64encode(b"a" * 32).decode()})
    assert result.returncode == 4
    report = json.loads(result.stdout)
    assert report["configuration"]["status"] == "FAIL"
    assert name + "_invalid_or_missing" in report["configuration"]["refusals"]
    assert report["deliverability"]["status"] == "UNKNOWN"
    assert values["CHUDWARE_DB_URL"] not in result.stdout + result.stderr
    assert values["CHUDWARE_SMTP_PASS"] not in result.stdout + result.stderr


@pytest.mark.parametrize("backend", ["smtp", "resend"])
def test_valid_configuration_pass_is_redacted_and_never_delivery_attestation(tmp_path, backend):
    values = configuration(tmp_path, CHUDWARE_MAIL_BACKEND=backend, CHUDWARE_RESEND_API_KEY="fixture-resend-credential")
    result = invoke(environment_file(tmp_path, values))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["configuration"] == {"status": "PASS", "refusals": []}
    assert report["deliverability"]["status"] == "UNKNOWN"
    assert set(report["deliverability"]["external_prerequisites"]) == {"DNS", "SPF", "DKIM", "provider acceptance", "recipient receipt"}
    for name in (preflight.MAIL_KEY, preflight.PROVIDER_KEY, "CHUDWARE_DB_URL", "CHUDWARE_SMTP_PASS", "CHUDWARE_RESEND_API_KEY"):
        assert values[name] not in result.stdout + result.stderr


@pytest.mark.parametrize("overrides,refusal", [
    ({"CHUDWARE_MAIL_BACKEND": "console"}, "production_console_mail_refused"),
    ({"CHUDWARE_SMTP_PASS": ""}, "smtp_credentials_missing_or_partial"),
    ({"CHUDWARE_SMTP_TLS": "plaintext"}, "smtp_tls_invalid"),
    ({"CHUDWARE_SMTP_PORT": "65536"}, "smtp_port_invalid"),
    ({"CHUDWARE_WEB_PUBLIC_BASE_URL": "http://fixture.test"}, "public_origin_invalid"),
    ({"CHUDWARE_ENTITLEMENT_KEY_PATH": "/fixture/plaintext.key"}, "plaintext_entitlement_key_path_refused"),
])
def test_production_transport_or_plaintext_authority_refuses(tmp_path, overrides, refusal):
    result = invoke(environment_file(tmp_path, configuration(tmp_path, **overrides)))
    assert result.returncode == 4
    assert refusal in json.loads(result.stdout)["configuration"]["refusals"]


def test_independent_keys_required_and_staging_console_is_not_production_delivery(tmp_path):
    values = configuration(tmp_path)
    values[preflight.PROVIDER_KEY] = values[preflight.MAIL_KEY]
    result = invoke(environment_file(tmp_path, values))
    assert result.returncode == 4
    assert "encryption_keys_must_be_independent" in json.loads(result.stdout)["configuration"]["refusals"]
    stage = tmp_path / "stage"
    stage.mkdir()
    values = configuration(stage, CHUDWARE_DEPLOY_ENV="staging", CHUDWARE_MAIL_BACKEND="console")
    for name, suffix in (("CHUDWARE_WORKSPACE_ROOT", "workspace"), ("CHUDWARE_WEB_BUCKET_ROOT", "bucket"), ("CHUDWARE_CELLS_ROOT", "cells")):
        values[name] = str(Path(values["CHUDWARE_WEB_DATA_ROOT"]) / suffix)
    path = environment_file(stage, values)
    result = invoke(path, environment="staging")
    assert result.returncode == 0
    assert json.loads(result.stdout)["deliverability"]["status"] == "UNKNOWN"
    assert invoke(path, environment="prod").returncode == 4


@pytest.mark.parametrize("environment", ["prod", "staging"])
def test_deployed_dev_mode_always_refuses(tmp_path, environment):
    values = configuration(tmp_path, CHUDWARE_DEPLOY_ENV=environment, CHUDWARE_WEB_DEV="yes")
    result = invoke(environment_file(tmp_path, values), environment=environment)
    assert result.returncode == 4
    assert "deployed_dev_auth_refused" in json.loads(result.stdout)["configuration"]["refusals"]


def test_staging_live_stripe_key_refuses_even_with_console_transport(tmp_path):
    values = configuration(tmp_path, CHUDWARE_DEPLOY_ENV="staging",
                           CHUDWARE_MAIL_BACKEND="console", CHUDWARE_WEB_STRIPE_SECRET_KEY="sk_live_fixture-not-real")
    result = invoke(environment_file(tmp_path, values), environment="staging")
    assert result.returncode == 4
    assert "staging_live_stripe_refused" in json.loads(result.stdout)["configuration"]["refusals"]


@pytest.mark.parametrize("unsafe", ["permissions", "file_link", "ancestor_link", "hardlink", "oversized", "fifo"])
def test_unprotected_environment_refuses_boundedly_without_leaking_values(tmp_path, unsafe):
    values = configuration(tmp_path)
    path = environment_file(tmp_path, values)
    if unsafe == "permissions":
        path.chmod(0o640)
    elif unsafe == "file_link":
        alias = tmp_path / "alias.env"
        alias.symlink_to(path)
        path = alias
    elif unsafe == "ancestor_link":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        path = alias / path.name
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "hardlink.env")
    elif unsafe == "oversized":
        path.write_bytes(b"x" * 65537)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    result = invoke(path)
    assert result.returncode == 4
    assert json.loads(result.stdout)["configuration"]["status"] == "FAIL"
    assert values[preflight.MAIL_KEY] not in result.stdout + result.stderr


def test_ops_session_missing_key_refuses_before_database_import_and_has_no_ambient_repair(tmp_path):
    values = configuration(tmp_path, **{preflight.MAIL_KEY: ""})
    result = invoke(environment_file(tmp_path, values), "ops-session", "--email", "fixture@example.test",
                    ambient={preflight.MAIL_KEY: base64.urlsafe_b64encode(b"a" * 32).decode(), "CHUDWARE_DB_URL": "invalid-ambient-credential"})
    assert result.returncode == 4
    assert result.stdout == ""
    assert result.stderr == "gateway preflight refused protected configuration or operation\n"


def test_key_provisioning_requires_apply_preserves_existing_keys_and_never_makes_backup(tmp_path):
    values = configuration(tmp_path, **{preflight.MAIL_KEY: "", preflight.PROVIDER_KEY: ""})
    path = environment_file(tmp_path, values)
    original = path.read_bytes()
    result = invoke(path, "provision-keys")
    assert result.returncode == 4 and path.read_bytes() == original
    result = invoke(path, "provision-keys", "--apply")
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    provisioned = preflight.load_deploy_environment(path, "prod")
    keys = [preflight.decode_encryption_key(provisioned[name], name) for name in (preflight.MAIL_KEY, preflight.PROVIDER_KEY)]
    assert all(len(key) == 32 for key in keys) and keys[0] != keys[1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.read_bytes()
    result = invoke(path, "provision-keys", "--apply")
    assert result.returncode == 0 and path.read_bytes() == first
    assert set(item.name for item in tmp_path.iterdir()) == {"data", "workspace", "bucket", "cells", "gateway.env"}


def test_invalid_existing_key_is_never_silently_rotated(tmp_path):
    path = environment_file(tmp_path, configuration(tmp_path, **{preflight.MAIL_KEY: "invalid-key", preflight.PROVIDER_KEY: ""}))
    original = path.read_bytes()
    result = invoke(path, "provision-keys", "--apply")
    assert result.returncode == 4 and result.stdout == ""
    assert path.read_bytes() == original


def test_provisioning_never_places_encryption_keys_in_backup_environment(tmp_path):
    values = configuration(tmp_path, **{preflight.MAIL_KEY: "", preflight.PROVIDER_KEY: ""})
    path = environment_file(tmp_path, values)
    backup = path.with_name("backup.env")
    path.rename(backup)
    original = backup.read_bytes()
    result = invoke(backup, "provision-keys", "--apply")
    assert result.returncode == 4 and result.stdout == ""
    assert backup.read_bytes() == original


def test_setup_key_operation_only_provisions_existing_protected_file(tmp_path):
    values = configuration(tmp_path, **{preflight.MAIL_KEY: "", preflight.PROVIDER_KEY: ""})
    data = Path(values["CHUDWARE_WEB_DATA_ROOT"])
    path = environment_file(data, values)
    result = subprocess.run(
        ["bash", str(DEPLOY / "setup.sh"), "prod", "--provision-gateway-keys"],
        env={"PATH": os.defpath, "HOME": str(tmp_path),
             "CHUDWARE_WEB_DATA_ROOT": str(data), "CHUDWARE_GATEWAY_PYTHON": sys.executable},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    provisioned = preflight.load_deploy_environment(path, "prod")
    assert provisioned[preflight.MAIL_KEY] != provisioned[preflight.PROVIDER_KEY]
    assert set(item.name for item in tmp_path.iterdir()) == {"data", "workspace", "bucket", "cells"}
    assert set(item.name for item in data.iterdir()) == {"gateway.env"}
