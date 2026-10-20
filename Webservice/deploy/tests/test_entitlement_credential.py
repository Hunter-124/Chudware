import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


DEPLOY = Path(__file__).resolve().parents[1]
GATEWAY = DEPLOY.parent / "gateway"


@pytest.mark.parametrize(
    "name,encrypted",
    [
        ("chudware-gateway.service", "chudware-entitlement.cred"),
        ("chudware-gateway-staging.service", "chudware-entitlement-staging.cred"),
    ],
)
def test_gateway_units_use_private_systemd_credential(name: str, encrypted: str) -> None:
    unit = (DEPLOY / name).read_text(encoding="utf-8")
    assert f"LoadCredentialEncrypted=entitlement.key:%h/.config/credstore.encrypted/{encrypted}" in unit
    assert "Environment=CHUDWARE_ENTITLEMENT_KEY_PATH=%d/entitlement.key" in unit
    assert "Environment=CHUDWARE_ENTITLEMENT_KEY=" not in unit


def test_gateway_configuration_contains_no_raw_entitlement_secret_path() -> None:
    config = (GATEWAY / "config.example.env").read_text(encoding="utf-8")
    setup = (DEPLOY / "setup.sh").read_text(encoding="utf-8")
    assert "CHUDWARE_ENTITLEMENT_KEY=" not in config
    env_skeleton = setup.split('cat >"${ENV_FILE}"', 1)[1].split("EOF", 1)[0]
    assert "CHUDWARE_ENTITLEMENT_KEY" not in env_skeleton


@pytest.fixture
def provisioner(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    record = tmp_path / "record.json"
    recorder = binaries / "systemd-creds"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, os, pathlib, sys\n"
        "raw = sys.stdin.buffer.read()\n"
        "roots = [pathlib.Path(os.environ['HOME']), pathlib.Path(os.environ['TMPDIR'])]\n"
        "files = [p for root in roots for p in root.rglob('*') if p.is_file()]\n"
        "payload = {'argv': sys.argv[1:], 'size': len(raw), 'digest': hashlib.sha256(raw).hexdigest(),\n"
        " 'files': [str(p) for p in files],\n"
        " 'plaintext_files': [str(p) for p in files if raw and raw in p.read_bytes()],\n"
        " 'self_cmdline': pathlib.Path('/proc/self/cmdline').read_bytes().hex(),\n"
        " 'parent_cmdline': pathlib.Path(f'/proc/{os.getppid()}/cmdline').read_bytes().hex()}\n"
        "pathlib.Path(os.environ['RECORD']).write_text(json.dumps(payload))\n"
        "pathlib.Path(sys.argv[-1]).write_bytes(b'encrypted-fixture-only')\n"
        "sys.exit(int(os.environ.get('ENCRYPT_EXIT', '0')))\n",
        encoding="utf-8",
    )
    recorder.chmod(0o700)
    environment = {
        "HOME": str(home), "TMPDIR": str(temporary), "RECORD": str(record),
        "PATH": f"{binaries}:{os.defpath}",
    }
    output = home / ".config/credstore.encrypted/chudware-entitlement.cred"

    def run(key, *args, **env):
        return subprocess.run(
            ["bash", str(DEPLOY / "provision_entitlement_credential.sh"), "prod", *args],
            input=key, env={**environment, **env}, capture_output=True, check=False,
        )

    return run, output, record, temporary


def test_provisioner_streams_binary_key_without_plaintext_file_or_argv(provisioner):
    run, output, record, temporary = provisioner
    key = bytes(range(32))  # fake bytes include NUL/newline: no shell-variable transport
    result = run(key)
    assert result.returncode == 0, result.stderr
    captured = json.loads(record.read_text())
    assert captured["size"] == 32
    assert captured["digest"] == hashlib.sha256(key).hexdigest()
    assert captured["argv"][:2] == ["encrypt", "--name=entitlement.key"]
    assert captured["argv"][2] in {"--with-key=tpm2", "--with-key=host+tpm2-absent"}
    assert captured["argv"][-2] == "-"
    assert captured["plaintext_files"] == []
    assert key not in bytes.fromhex(captured["self_cmdline"])
    assert key not in bytes.fromhex(captured["parent_cmdline"])
    assert key not in result.stdout + result.stderr
    assert output.read_bytes() == b"encrypted-fixture-only"
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert list(output.parent.iterdir()) == [output]
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize("size", [0, 31, 33, 65536])
def test_provisioner_rejects_malformed_input_before_encrypt_or_placement(provisioner, size):
    run, output, record, temporary = provisioner
    result = run(b"x" * size)
    assert result.returncode != 0
    assert b"exactly 32 raw bytes" in result.stderr
    assert not record.exists()
    assert not output.parent.exists()
    assert list(temporary.iterdir()) == []


def test_provisioner_failed_rotation_preserves_encrypted_credential(provisioner):
    run, output, record, temporary = provisioner
    output.parent.mkdir(parents=True, mode=0o700)
    output.write_bytes(b"previous-encrypted-fixture")
    output.chmod(0o600)
    result = run(b"x" * 32, "--replace", ENCRYPT_EXIT="7")
    assert result.returncode != 0
    assert record.exists()
    assert output.read_bytes() == b"previous-encrypted-fixture"
    assert list(output.parent.iterdir()) == [output]
    assert list(temporary.iterdir()) == []


def test_provisioner_intentional_rotation_atomically_replaces_encrypted_credential(provisioner):
    run, output, record, _ = provisioner
    output.parent.mkdir(parents=True, mode=0o700)
    output.write_bytes(b"previous-encrypted-fixture")
    output.chmod(0o600)
    result = run(bytes(range(32)), "--replace")
    assert result.returncode == 0, result.stderr
    assert record.exists()
    assert output.read_bytes() == b"encrypted-fixture-only"
    assert output.stat().st_mode & 0o777 == 0o600
    assert list(output.parent.iterdir()) == [output]


def test_provisioner_refuses_unrequested_rotation(provisioner):
    run, output, record, _ = provisioner
    output.parent.mkdir(parents=True, mode=0o700)
    output.write_bytes(b"previous-encrypted-fixture")
    output.chmod(0o600)
    assert run(b"x" * 32).returncode != 0
    assert not record.exists()
    assert output.read_bytes() == b"previous-encrypted-fixture"


@pytest.mark.parametrize("unsafe", ["directory-mode", "credential-mode", "credential-link"])
def test_provisioner_refuses_unsafe_encrypted_output(provisioner, unsafe):
    run, output, record, _ = provisioner
    output.parent.mkdir(parents=True, mode=0o700)
    if unsafe == "directory-mode":
        output.parent.chmod(0o755)
    elif unsafe == "credential-mode":
        output.write_bytes(b"previous-encrypted-fixture")
        output.chmod(0o644)
    else:
        target = output.parent / "not-a-credential"
        target.write_bytes(b"unrelated-fixture")
        output.symlink_to(target)
    result = run(b"x" * 32, "--replace")
    assert result.returncode != 0
    assert not record.exists()


