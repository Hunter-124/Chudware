"""Disposable backup/recovery behavior: real age, inert data and fake PG clients."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_state", DEPLOY / "backup_state.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
DUMP_MARKER = b"PGDMP\x00fixture-encrypted-only-postgresql-dump\xff"
PART_BYTES = b"PK\x03\x04opaque-stepplus\x00cache.ckpt\x00BREP-cache\xffactor-solve-state"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    for key in backup.CREDENTIAL_ENV:
        monkeypatch.delenv(key, raising=False)
    values = {label: tmp_path / label for label in ("data", "workspace", "bucket", "cells")}
    for label, root in values.items():
        root.mkdir()
        (root / (label + "-marker")).write_bytes((label + "-durable").encode())
        credential = root / "nested" / "secrets"
        credential.mkdir(parents=True)
        (credential / "rclone.conf").write_text("fixture-credential-must-not-archive")
        (root / "nested" / "provider.env.bak-old").write_text("fixture-env-must-not-archive")
    (values["workspace"] / "cached.step+").write_bytes(PART_BYTES)
    actor = values["cells"] / "cell-one" / "state" / "actors" / "actor-one"
    for name in ("home", "data", "config", "cache", "run"):
        (actor / name).mkdir(parents=True, exist_ok=True)
        (actor / name / "marker").write_text("actor-" + name)
    private = actor / "data" / ("b" * 32)
    private.mkdir()
    (private / "config.json").write_text('{"subscription_auth":{"fixture":"not-a-real-token"}}')
    (private / "config.json.tmp.123").write_text("fixture-credential-temporary-copy")
    (private / "sessions.sqlite").write_bytes(b"opaque-private-actor-session-state")
    sdk = private / "chudware-byok-fixture" / "agent"
    sdk.mkdir(parents=True)
    (sdk / "fixture-unqualified-auth-store").write_bytes(b"fixture-sdk-auth-material")
    history = private / "Sessions" / "byok-fixture"
    history.mkdir(parents=True)
    (history / "history.jsonl").write_bytes(b'{"fixture":"opaque-session-history"}\n')
    (actor.parent.parent / "cache-provenance.key").write_bytes(b"fixture-provenance-key-not-real")
    artifact = private / "models" / "fixture-model"
    artifact.mkdir(parents=True)
    (artifact / "config.json").write_bytes(b'{"fixture":"durable-model-configuration"}')
    desktop = actor / "home" / ".local/share/Chudware"
    desktop.mkdir(parents=True)
    (desktop / "config.json").write_text('{"api_key":"fixture-only"}')
    (desktop / "fixture-model").mkdir()
    (desktop / "fixture-model/config.json").write_bytes(b'{"fixture":"desktop-model-config"}')
    profile = actor / "data" / "browser_profiles"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"fixture-browser-credential")
    university = values["data"] / "university"
    (university / "secrets").mkdir(parents=True)
    (university / "secrets" / "rclone.conf").write_text("fixture-institutional-secret")
    (university / "cache").mkdir()
    (university / "cache" / "marker").write_text("institutional-cache")
    return values


@pytest.fixture
def age_keys(tmp_path):
    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("real age and age-keygen are required for encrypted recovery smoke")
    identity = tmp_path / "fixture-identity"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.check_output(["age-keygen", "-y", str(identity)], text=True).strip()
    return identity, recipient


@pytest.fixture
def command_env(tmp_path, roots, age_keys):
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "age").symlink_to(shutil.which("age"))
    pg_dump = binary / "pg_dump"
    pg_dump.write_text(
        f"#!{sys.executable}\nimport os, stat, sys\nfrom pathlib import Path\n"
        "assert '--format=custom' in sys.argv\n"
        "assert not any(arg.startswith('--file') for arg in sys.argv)\n"
        "assert 'fixture-password' not in ' '.join(sys.argv)\n"
        "pgpass=Path(os.environ['PGPASSFILE'])\n"
        "assert stat.S_IMODE(pgpass.stat().st_mode)==0o600\n"
        "assert stat.S_IMODE(pgpass.parent.stat().st_mode)==0o700\n"
        "assert 'fixture-password' in pgpass.read_text()\n"
        f"sys.stdout.buffer.write({DUMP_MARKER!r})\n"
    )
    pg_dump.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return {
        "PATH": f"{binary}:{os.defpath}", "HOME": str(home),
        "CHUDWARE_DEPLOY_ENV": "prod", "CHUDWARE_GATEWAY_PYTHON": sys.executable,
        "CHUDWARE_DB_URL": "postgresql://chudware_prod:fixture-password@127.0.0.1:5432/chudware_prod",
        "CHUDWARE_WEB_DATA_ROOT": str(roots["data"]),
        "CHUDWARE_WORKSPACE_ROOT": str(roots["workspace"]),
        "CHUDWARE_WEB_BUCKET_ROOT": str(roots["bucket"]),
        "CHUDWARE_CELLS_ROOT": str(roots["cells"]),
        "CHUDWARE_BACKUP_AGE_RECIPIENT": age_keys[1],
        "CHUDWARE_WEB_BACKUP_DIR": str(tmp_path / "encrypted-backups"),
    }


def decrypt(path, identity):
    return subprocess.check_output(["age", "--decrypt", "--identity", str(identity), str(path)])


def test_nightly_streams_encrypted_roots_and_isolated_restore(tmp_path, roots, age_keys, command_env):
    credential = roots["data"] / "nested" / "custom-private-authority"
    credential.mkdir()
    (credential / "fixture-token").write_text("fixture-configured-credential")
    command_env["CREDENTIALS_DIRECTORY"] = str(credential)
    result = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=command_env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    destination = Path(command_env["CHUDWARE_WEB_BACKUP_DIR"])
    dumps = list(destination.glob("nightly-database-prod-*.dump.age"))
    archives = list(destination.glob("nightly-state-prod-*.tar.age"))
    assert len(dumps) == len(archives) == 1
    assert decrypt(dumps[0], age_keys[0]) == DUMP_MARKER
    for artifact in destination.iterdir():
        assert artifact.name.endswith(".age")
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        assert DUMP_MARKER not in artifact.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert "LOCAL COPY ONLY" in result.stdout
    target = tmp_path / "isolated-recovery"
    recovery = subprocess.run([
        sys.executable, str(DEPLOY / "backup_state.py"), "restore-state",
        "--environment", "prod", "--artifact", str(archives[0]),
        "--identity", str(age_keys[0]), "--target", str(target),
    ], env=command_env, capture_output=True, text=True, timeout=30)
    assert recovery.returncode == 0, recovery.stdout + recovery.stderr
    manifest = json.loads((target / "backup-manifest.json").read_text())
    assert manifest["environment"] == "prod"
    assert {entry["label"]: entry["source"] for entry in manifest["roots"]} == {label: str(root) for label, root in roots.items()}
    assert all(entry["mount_id"] and entry["mount_path"] for entry in manifest["roots"])
    for label in roots:
        recovered = target / "roots" / label
        assert (recovered / (label + "-marker")).read_text() == label + "-durable"
        assert not (recovered / "nested" / "secrets").exists()
        assert not (recovered / "nested" / "provider.env.bak-old").exists()
    assert (target / "roots/workspace/cached.step+").read_bytes() == PART_BYTES
    assert (target / "roots/data/university/cache/marker").read_text() == "institutional-cache"
    assert not (target / "roots/data/university/secrets").exists()
    assert not (target / "roots/data/nested/custom-private-authority").exists()
    assert str(credential) in manifest["credential_inventory"]["configured_paths"]
    actor = target / "roots/cells/cell-one/state/actors/actor-one"
    for name in ("home", "data", "config", "cache"):
        assert (actor / name / "marker").read_text() == "actor-" + name
    assert not (actor / "data" / ("b" * 32) / "config.json").exists()
    assert not (actor / "data" / ("b" * 32) / "config.json.tmp.123").exists()
    assert (actor / "data" / ("b" * 32) / "sessions.sqlite").read_bytes() == b"opaque-private-actor-session-state"
    assert not (actor / "home/.local/share/Chudware/config.json").exists()
    assert not (actor / "data/browser_profiles").exists()
    assert not (actor / "data" / ("b" * 32) / "chudware-byok-fixture").exists()
    assert (actor / "data" / ("b" * 32) / "Sessions/byok-fixture/history.jsonl").read_bytes() == b'{"fixture":"opaque-session-history"}\n'
    assert (actor / "data" / ("b" * 32) / "models/fixture-model/config.json").read_bytes() == b'{"fixture":"durable-model-configuration"}'
    assert (actor / "home/.local/share/Chudware/fixture-model/config.json").read_bytes() == b'{"fixture":"desktop-model-config"}'
    assert not (target / "roots/cells/cell-one/state/cache-provenance.key").exists()
    assert not (actor / "run").exists()
    assert not list(tmp_path.glob(".chudware-restore-*"))
    assert not list(destination.glob(".pgpass-*"))


def test_predeploy_function_encrypts_and_bounds_only_its_family(tmp_path, roots, age_keys, command_env):
    source = (DEPLOY / "update.sh").read_text()
    start = source.index("backup_database() {")
    end = source.index("\n}", start) + 2
    program = source[start:end] + "\nbackup_database\n"
    env = {**command_env, "REPO": str(DEPLOY.parents[1]), "DB_URL": command_env["CHUDWARE_DB_URL"],
           "DATA_ROOT": str(roots["data"]), "ENVIRONMENT": "prod", "EXPECTED_DB": "chudware_prod",
           "CHUDWARE_WEB_BACKUP_KEEP": "1"}
    destination = roots["data"] / "backups"
    destination.mkdir(mode=0o700)
    untouched = destination / "pre-deploy-staging-20000101T000000Z-00000000000000000000000000000000.dump.age"
    untouched.write_bytes(b"foreign-environment")
    for _ in range(2):
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", program], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        artifacts = list(destination.glob("pre-deploy-prod-*.dump.age"))
        assert len(artifacts) == 1
        assert decrypt(artifacts[0], age_keys[0]) == DUMP_MARKER
        assert DUMP_MARKER not in artifacts[0].read_bytes()
    assert untouched.read_bytes() == b"foreign-environment"
    assert not list(destination.glob("*.dump"))
    assert not list(destination.glob("*.partial"))
    assert not list(destination.glob(".pgpass-*"))


def test_encrypted_database_restore_uses_isolated_transactional_consumer(tmp_path, roots, age_keys, command_env):
    destination = tmp_path / "encrypted-db"
    result = subprocess.run([
        sys.executable, str(DEPLOY / "backup_state.py"), "database", "--environment", "prod",
        "--data-root", str(roots["data"]), "--destination", str(destination),
    ], env=command_env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    artifact = Path(result.stdout.strip())
    receipt = tmp_path / "receipt.json"
    consumer = Path(command_env["PATH"].split(":")[0]) / "pg_restore"
    consumer.write_text(
        f"#!{sys.executable}\nimport hashlib,json,os,sys\nfrom pathlib import Path\n"
        "assert '--single-transaction' in sys.argv and '--exit-on-error' in sys.argv\n"
        "assert '--clean' not in sys.argv and '--create' not in sys.argv\n"
        "assert 'fixture-recovery-password' not in ' '.join(sys.argv)\n"
        "raw=sys.stdin.buffer.read()\n"
        f"assert raw=={DUMP_MARKER!r}\n"
        f"Path({str(receipt)!r}).write_text(json.dumps({{'sha256':hashlib.sha256(raw).hexdigest()}}))\n"
    )
    consumer.chmod(0o700)
    env = {**command_env, "CHUDWARE_RESTORE_DB_URL": "postgresql://recovery_operator:fixture-recovery-password@127.0.0.1:5432/chudware_recovery_drill"}
    restored = subprocess.run([
        sys.executable, str(DEPLOY / "backup_state.py"), "restore-database",
        "--artifact", str(artifact), "--identity", str(age_keys[0]),
        "--target-db", "chudware_recovery_drill", "--temporary-root", str(tmp_path / "private"),
    ], env=env, capture_output=True, text=True, timeout=30)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert json.loads(receipt.read_text())["sha256"] == hashlib.sha256(DUMP_MARKER).hexdigest()
    assert not list((tmp_path / "private").iterdir())
    env["CHUDWARE_RESTORE_DB_URL"] = command_env["CHUDWARE_DB_URL"]
    refused = subprocess.run([
        sys.executable, str(DEPLOY / "backup_state.py"), "restore-database",
        "--artifact", str(artifact), "--identity", str(age_keys[0]),
        "--target-db", "chudware_prod", "--temporary-root", str(tmp_path / "private"),
    ], env=env, capture_output=True, text=True, timeout=10)
    assert refused.returncode != 0


@pytest.mark.parametrize("failure", ["missing", "symlink-root", "symlink-ancestor", "symlink-file", "hardlink", "unreadable", "fifo", "broad"])
def test_archive_rejects_incomplete_or_unsafe_sources(tmp_path, roots, failure):
    if failure == "missing":
        roots["bucket"] = tmp_path / "absent"
    elif failure == "symlink-root":
        alias = tmp_path / "alias"
        alias.symlink_to(roots["bucket"], target_is_directory=True)
        roots["bucket"] = alias
    elif failure == "symlink-ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        roots["bucket"] = alias / "bucket"
    elif failure == "symlink-file":
        (roots["data"] / "linked-customer-file").symlink_to(roots["workspace"] / "cached.step+")
    elif failure == "hardlink":
        os.link(roots["data"] / "nested/secrets/rclone.conf", roots["data"] / "credential-alias")
    elif failure == "unreadable":
        (roots["bucket"] / "bucket-marker").chmod(0)
    elif failure == "fifo":
        os.mkfifo(roots["data"] / "unexpected-pipe")
    else:
        roots["bucket"] = Path("/")
    with pytest.raises((backup.BackupError, OSError)):
        backup.archive_state(io.BytesIO(), roots, "prod")


def test_unconfigured_mount_fails_and_explicit_mount_is_enumerated(tmp_path, roots, monkeypatch):
    mount = roots["data"] / "university" / "workspace"
    mount.mkdir()
    (mount / "marker").write_text("institutional-workspace")
    monkeypatch.setattr(backup, "_mounts", lambda: {Path("/"): "1", mount: "42"})
    with pytest.raises(backup.BackupError, match="unconfigured nested mount"):
        backup.archive_state(io.BytesIO(), roots, "prod")
    roots["university"] = mount
    output = io.BytesIO()
    backup.archive_state(output, roots, "prod")
    with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
        assert archive.extractfile("roots/university/marker").read() == b"institutional-workspace"
        assert "roots/data/university/workspace/marker" not in archive.getnames()
        manifest = json.load(archive.extractfile("backup-manifest.json"))
        boundary = next(entry for entry in manifest["roots"] if entry["label"] == "university")
        assert boundary["mount_id"] == "42"


def unsafe_archive(roots, member_name, member_type=tarfile.REGTYPE):
    raw = io.BytesIO()
    manifest = json.dumps(backup.validate_roots(roots, "prod")).encode()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        header = tarfile.TarInfo("backup-manifest.json")
        header.size = len(manifest)
        archive.addfile(header, io.BytesIO(manifest))
        for name in ("roots", *("roots/" + label for label in roots)):
            item = tarfile.TarInfo(name)
            item.type = tarfile.DIRTYPE
            archive.addfile(item)
        item = tarfile.TarInfo(member_name)
        item.type = member_type
        if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
            item.linkname = "/outside-fixture"
        if member_type == tarfile.REGTYPE:
            item.size = 4
        archive.addfile(item, io.BytesIO(b"evil") if item.isreg() else None)
    return raw.getvalue()


@pytest.mark.parametrize("name,kind", [
    ("../outside", tarfile.REGTYPE), ("/outside", tarfile.REGTYPE),
    ("roots/data/../../outside", tarfile.REGTYPE), ("roots/data/linked", tarfile.SYMTYPE),
    ("roots/data/hardlinked", tarfile.LNKTYPE), ("roots/data/pipe", tarfile.FIFOTYPE),
    ("roots/data/device", tarfile.CHRTYPE), ("roots/data/secrets", tarfile.DIRTYPE),
    ("roots/data/rclone.conf", tarfile.REGTYPE), ("roots/data", tarfile.DIRTYPE),
])
def test_restore_rejects_traversal_links_types_credentials_and_duplicates(tmp_path, roots, name, kind):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    with pytest.raises(backup.BackupError):
        backup.unpack_state(io.BytesIO(unsafe_archive(roots, name, kind)), stage, "prod")
    assert not (tmp_path / "outside").exists()


def test_restore_preserves_existing_target_and_cleans_failed_staging(tmp_path, roots, age_keys):
    artifact = tmp_path / "state.age"
    with backup.encrypted_output(artifact, age_keys[1]) as output:
        backup.archive_state(output, roots, "prod")
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (target / "existing").write_bytes(b"preserve-me")
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore_state(artifact, age_keys[0], target, "prod")
    assert (target / "existing").read_bytes() == b"preserve-me"
    bad = tmp_path / "bad.age"
    with backup.encrypted_output(bad, age_keys[1]) as output:
        output.write(unsafe_archive(roots, "../outside"))
    with pytest.raises(backup.BackupError):
        backup.restore_state(bad, age_keys[0], target, "prod", replace=True)
    assert (target / "existing").read_bytes() == b"preserve-me"
    assert not list(tmp_path.glob(".chudware-restore-*"))
    backup.restore_state(artifact, age_keys[0], target, "prod", replace=True)
    assert not (target / "existing").exists()
    assert (target / "roots/workspace/cached.step+").read_bytes() == PART_BYTES
    with pytest.raises(backup.BackupError, match="overlaps"):
        backup.restore_state(artifact, age_keys[0], roots["data"], "prod", replace=True)
    assert (roots["data"] / "data-marker").read_bytes() == b"data-durable"
    assert not list(tmp_path.glob(".chudware-restore-*"))


def test_authentication_failure_never_places_or_replaces_state(tmp_path, roots, age_keys):
    artifact = tmp_path / "corrupted.age"
    with backup.encrypted_output(artifact, age_keys[1]) as output:
        backup.archive_state(output, roots, "prod")
    ciphertext = bytearray(artifact.read_bytes())
    ciphertext[-1] ^= 1
    artifact.write_bytes(ciphertext)
    target = tmp_path / "must-remain-absent"
    with pytest.raises((backup.BackupError, tarfile.TarError, OSError)):
        backup.restore_state(artifact, age_keys[0], target, "prod")
    assert not target.exists()
    assert not list(tmp_path.glob(".chudware-restore-*"))


def test_restore_requires_every_manifest_root(tmp_path, roots):
    output = io.BytesIO()
    manifest = json.dumps(backup.validate_roots(roots, "prod")).encode()
    with tarfile.open(fileobj=output, mode="w") as archive:
        item = tarfile.TarInfo("backup-manifest.json")
        item.size = len(manifest)
        archive.addfile(item, io.BytesIO(manifest))
        for name in ("roots", "roots/data", "roots/workspace", "roots/bucket"):
            item = tarfile.TarInfo(name)
            item.type = tarfile.DIRTYPE
            archive.addfile(item)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    with pytest.raises(backup.BackupError, match="storage root missing"):
        backup.unpack_state(io.BytesIO(output.getvalue()), stage, "prod")


def test_encryption_refuses_preexisting_partial_without_deleting_it(tmp_path):
    final = tmp_path / "artifact.age"
    partial = tmp_path / "artifact.age.partial"
    partial.write_bytes(b"another-run-owned-partial")
    with pytest.raises(FileExistsError):
        with backup.encrypted_output(final, "inert-not-used-recipient"):
            pytest.fail("existing partial must fail before starting age")
    assert partial.read_bytes() == b"another-run-owned-partial"
    assert not final.exists()


def test_private_destination_rejects_linked_parent_before_creating_files(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        backup.private_destination(linked / "must-not-create")
    assert not (actual / "must-not-create").exists()


def test_retention_collects_only_expired_owned_environment_families(tmp_path):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    def create(age, environment="prod", family="nightly-state", suffix=None):
        stamp = (now - timedelta(days=age)).strftime("%Y%m%dT%H%M%SZ")
        kind = "tar" if family == "nightly-state" else "dump"
        path = destination / f"{family}-{environment}-{stamp}-{uuid.uuid4().hex}.{kind}.age"
        if suffix:
            path = path.with_name(path.name + suffix)
        path.write_bytes(b"inert-ciphertext-fixture")
        return path
    old = create(31)
    excess = create(2)
    fresh = [create(1), create(0)]
    foreign = create(99, environment="staging")
    other_family = create(99, family="pre-deploy")
    partial = create(99, suffix=".partial")
    unrelated = destination / "unrelated.age"
    unrelated.write_bytes(b"leave-me")
    legacy = destination / "pre-deploy-prod-20000101T000000Z.dump"
    legacy.write_bytes(b"legacy-operator-disposition-only")
    linked = create(99)
    linked.unlink()
    linked.symlink_to(unrelated)
    hardlinked = create(98)
    alias = destination / "foreign-hardlink"
    os.link(hardlinked, alias)
    removed = backup.prune(destination, "prod", keep=2, days=30, families={"nightly-state"})
    assert set(removed) == {old, excess}
    assert all(path.exists() for path in [*fresh, foreign, other_family, partial, unrelated, legacy, hardlinked, alias])
    assert linked.is_symlink()


def test_missing_root_and_environment_authority_fail_without_backup_writes(tmp_path, command_env):
    env = {**command_env, "CHUDWARE_WEB_BUCKET_ROOT": str(tmp_path / "missing")}
    failed = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0
    assert not Path(env["CHUDWARE_WEB_BACKUP_DIR"]).exists()
    env = {**command_env, "CHUDWARE_DEPLOY_ENV": "staging"}
    failed = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0
    assert not Path(env["CHUDWARE_WEB_BACKUP_DIR"]).exists()


@pytest.mark.parametrize("use_default", [False, True])
def test_configured_and_default_root_links_fail_before_backup_writes(tmp_path, command_env, roots, use_default):
    env = dict(command_env)
    if use_default:
        linked = Path(env["HOME"]) / "chudware-web-data"
        linked.symlink_to(roots["data"], target_is_directory=True)
        env.pop("CHUDWARE_WEB_DATA_ROOT")
    else:
        linked = tmp_path / "linked-bucket"
        linked.symlink_to(roots["bucket"], target_is_directory=True)
        env["CHUDWARE_WEB_BUCKET_ROOT"] = str(linked)
    failed = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0
    assert not Path(env["CHUDWARE_WEB_BACKUP_DIR"]).exists()


def test_failed_dump_leaves_no_plaintext_encrypted_partial_or_pgpass(tmp_path, command_env):
    dump = tmp_path / "bin" / "pg_dump"
    dump.write_text("#!" + sys.executable + "\nimport sys\n"
                    "sys.stdout.buffer.write(" + repr(DUMP_MARKER) + ")\n"
                    "sys.stdout.buffer.flush()\nraise SystemExit(1)\n")
    dump.chmod(0o700)
    failed = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=command_env,
                            capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0
    destination = Path(command_env["CHUDWARE_WEB_BACKUP_DIR"])
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_failed_age_leaves_no_dump_or_key_material(tmp_path, command_env):
    env = {**command_env, "CHUDWARE_BACKUP_AGE_RECIPIENT": "fixture-invalid-recipient"}
    failed = subprocess.run(["bash", str(DEPLOY / "backup.sh")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0
    destination = Path(env["CHUDWARE_WEB_BACKUP_DIR"])
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_configured_roots_need_no_authentication_or_provider_keys(tmp_path, roots):
    env = {
        "CHUDWARE_DEPLOY_ENV": "prod", "HOME": str(tmp_path),
        "CHUDWARE_WEB_DATA_ROOT": str(roots["data"]),
        "CHUDWARE_WORKSPACE_ROOT": str(roots["workspace"]),
        "CHUDWARE_WEB_BUCKET_ROOT": str(roots["bucket"]),
        "CHUDWARE_CELLS_ROOT": str(roots["cells"]),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import json,sys;sys.path.insert(0,sys.argv[1]);"
         "import backup_state;"
         "print(json.dumps({k:str(v) for k,v in backup_state.configured_roots('prod').items()}));"
         "assert not any(k in sys.modules for k in ('config','gateway.config','authn','gateway.authn'))",
         str(DEPLOY)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {label: str(root) for label, root in roots.items()}


def test_webd_workspace_fallback_preserves_raw_link_refusal_without_keys(tmp_path, roots):
    linked = tmp_path / "linked-workspace"
    linked.symlink_to(roots["workspace"], target_is_directory=True)
    webd = tmp_path / "webd.env"
    webd.write_text("CHUDWARE_WORKSPACE_ROOT=" + str(linked) + "\n")
    webd.chmod(0o600)
    env = {
        "CHUDWARE_DEPLOY_ENV": "prod", "HOME": str(tmp_path),
        "CHUDWARE_WEB_DATA_ROOT": str(roots["data"]),
        "CHUDWARE_WEB_BUCKET_ROOT": str(roots["bucket"]),
        "CHUDWARE_CELLS_ROOT": str(roots["cells"]), "CHUDWARE_WEBD_ENV": str(webd),
    }
    command = [sys.executable, "-I", "-c",
               "import json,sys;sys.path.insert(0,sys.argv[1]);import backup_state;"
               "print(json.dumps({k:str(v) for k,v in backup_state.configured_roots('prod').items()}))",
               str(DEPLOY)]
    failed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0 and failed.stdout == ""
    webd.write_text("CHUDWARE_WORKSPACE_ROOT=" + str(roots["workspace"]) + "\n")
    accepted = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["workspace"] == str(roots["workspace"])
