"""Inert rollback trees: no native execution, services, customer state or authority writes."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("deployment_release", DEPLOY / "deployment_release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def retained_tree(root):
    root.mkdir()
    for _, relative in release._FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("inert " + relative).encode())
    (root / "app/index.html").write_bytes(b"inert html closure")
    prompts = root / "agent/prompts"
    prompts.mkdir(parents=True)
    (prompts / "engineering.md").write_bytes(b"inert engineering policy")
    advisor = root / "bin/polymesh-advisor"
    advisor.mkdir()
    (advisor / "model.onnx").write_bytes(b"inert advisor")
    cell = root / "cell"
    (cell / "units").mkdir(parents=True)
    (cell / "units/chudware-cell@.service").write_bytes(b"inert cell unit")
    (cell / "bin").mkdir()
    (cell / "bin/chudware-cell-launch").write_bytes(b"inert launcher")
    manifest = {"schema": "7", "release_id": "fixture-release"}
    manifest.update({field: release.file_digest(root / relative) for field, relative in release._FILES.items()})
    manifest["prompts_sha256"] = release._group_digest(prompts, [prompts / "engineering.md"])
    manifest["advisor_runtime_sha256"] = release._group_digest(root / "bin", [advisor / "model.onnx"])
    manifest["cell_runtime_sha256"] = release._group_digest(
        cell, [cell / "bin/chudware-cell-launch", cell / "units/chudware-cell@.service"])
    manifest["joint_sha256"] = hashlib.sha256("".join(manifest[field] + "\n" for field in release._JOINT_FIELDS).encode()).hexdigest()
    manifest["release_tree_sha256"] = release.tree_digest(root)
    (root / "deploy-manifest").write_text("".join(f"{key}={value}\n" for key, value in manifest.items()))
    return manifest


@pytest.mark.parametrize("member", [
    "bin/chudware-hostd", "bin/libchudware.so", "bin/polymesh-advisor/model.onnx",
    "app/Chudware.wasm", "app/Chudware.js", "app/index.html",
    "agent/prompts/engineering.md", "chudware-hostd.service",
    "chudware-gateway.service", "chudware-gateway-staging.service",
    "cell/bin/chudware-cell-launch", "cell/units/chudware-cell@.service",
])
def test_mutated_runtime_input_is_rejected_before_any_execution(tmp_path, member):
    root = tmp_path / "release"
    retained_tree(root)
    release.validate(root)
    (root / member).write_bytes(b"substituted input")
    with pytest.raises(release.ReleaseError):
        release.validate(root)


def test_unknown_added_file_and_duplicate_manifest_are_not_authorized(tmp_path):
    root = tmp_path / "release"
    retained_tree(root)
    (root / "bin/new-loader.so").write_bytes(b"inert injected loader")
    with pytest.raises(release.ReleaseError):
        release.validate(root)
    (root / "bin/new-loader.so").unlink()
    with (root / "deploy-manifest").open("a") as stream:
        stream.write("schema=6\n")
    with pytest.raises(release.ReleaseError):
        release.validate(root)


def test_linked_payload_never_reads_or_modifies_external_target(tmp_path):
    root = tmp_path / "release"
    retained_tree(root)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unaltered external fixture")
    member = root / "bin/libchudware.so"
    member.unlink()
    member.symlink_to(sentinel)
    with pytest.raises(release.ReleaseError):
        release.validate(root)
    assert sentinel.read_bytes() == b"unaltered external fixture"


def test_service_owned_authorization_directory_is_not_a_trust_root(tmp_path):
    registry = tmp_path / "registry"
    (registry / "prod").mkdir(parents=True)
    joint, tree = "a" * 64, "b" * 64
    (registry / "prod" / joint).write_text(tree + "\n")
    # World/group writable /tmp or non-root home ancestors are disallowed even
    # when the record repeats the requested digest. No bootstrap-by-self-hash.
    with pytest.raises(release.ReleaseError):
        release.authorize(joint, tree, "prod", trust_root=registry)


def test_real_rollback_rejects_unauthorized_tree_before_native_or_service_execution(tmp_path):
    home = tmp_path / "home"
    root = home / "chudware-web"
    retained = root / "releases"
    retained.mkdir(parents=True)
    candidate = retained / "fixture-release"
    manifest = retained_tree(candidate)
    marker = tmp_path / "executed"
    for member in ("bin/chudware-hostd", "bin/chud"):
        path = candidate / member
        path.write_text(f"#!/bin/sh\nprintf touched > {marker}\n")
        path.chmod(0o700)
    # Even a self-consistent rewritten manifest supplies no independent authority.
    for field, relative in release._FILES.items():
        manifest[field] = release.file_digest(candidate / relative)
    manifest["joint_sha256"] = hashlib.sha256("".join(manifest[field] + "\n" for field in release._JOINT_FIELDS).encode()).hexdigest()
    manifest["release_tree_sha256"] = release.tree_digest(candidate)
    (candidate / "deploy-manifest").write_text("".join(f"{key}={value}\n" for key, value in manifest.items()))
    result = subprocess.run(["bash", str(DEPLOY / "rollback.sh"), "--to", candidate.name],
                            env={"HOME": str(home), "PATH": os.defpath, "CHUDWARE_DEPLOY_ENV": "prod"},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "independent operator authorization failed" in result.stderr
    assert not marker.exists()
    assert not (root / "current").exists()
    assert not (home / ".config/systemd/user/chudware-hostd.service").exists()
    assert [path.name for path in retained.iterdir()] == [candidate.name]


@pytest.fixture
def fixture_authority(tmp_path, monkeypatch):
    """Real records/payloads, emulating root metadata without privileged writes."""
    registry = tmp_path / "root-authority"
    (registry / "prod/first-cutover").mkdir(parents=True)
    original_lstat = Path.lstat

    def root_metadata(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == registry or registry in path.parents or path in registry.parents:
            fields = list(info)
            fields[4] = 0
            # Ancestor metadata is fixture-only; record modes/types stay real.
            if path in registry.parents:
                fields[0] &= ~0o022
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", root_metadata)
    original_fstat = os.fstat

    def descriptor_metadata(fd):
        info = original_fstat(fd)
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", descriptor_metadata)
    return registry


def test_real_manifest_authorized_prior_payload_needs_no_first_cutover_ack(tmp_path, fixture_authority):
    home = tmp_path / "web"
    retained = home / "releases"
    retained.mkdir(parents=True)
    manifest = retained_tree(retained / "prior")
    (home / "current").symlink_to("releases/prior")
    record = fixture_authority / "prod" / manifest["joint_sha256"]
    record.write_text(manifest["release_tree_sha256"] + "\n")
    record.chmod(0o600)
    assert release.promotion_preflight(home, "prod", "c" * 40, trust_root=fixture_authority) == "authorized_retained_payload"
    (retained / "prior/cell/bin/chudware-cell-launch").write_bytes(b"substituted")
    with pytest.raises(release.ReleaseError, match="explicit first-cutover"):
        release.promotion_preflight(home, "prod", "c" * 40, trust_root=fixture_authority)


def test_authorized_bytes_do_not_authorize_a_linked_release_store(tmp_path, fixture_authority):
    home = tmp_path / "web"
    home.mkdir()
    external = tmp_path / "external-store"
    external.mkdir()
    manifest = retained_tree(external / "prior")
    (home / "releases").symlink_to(external, target_is_directory=True)
    (home / "current").symlink_to("releases/prior")
    record = fixture_authority / "prod" / manifest["joint_sha256"]
    record.write_text(manifest["release_tree_sha256"] + "\n")
    record.chmod(0o600)
    with pytest.raises(release.ReleaseError, match="explicit first-cutover"):
        release.promotion_preflight(home, "prod", "c" * 40, trust_root=fixture_authority)


@pytest.mark.parametrize("invalid", ["missing", "wrong_commit", "incomplete", "writable", "symlink"])
def test_first_cutover_requires_exact_protected_independent_recovery_ack(
        tmp_path, fixture_authority, invalid):
    commit = "c" * 40
    ack = fixture_authority / "prod/first-cutover" / commit
    expected = release._FIRST_CUTOVER_RECOVERY.format(commit=commit)
    if invalid != "missing":
        ack.write_text(expected)
        ack.chmod(0o600)
    if invalid == "wrong_commit":
        ack.write_text(expected.replace(commit, "d" * 40))
    elif invalid == "incomplete":
        ack.write_text(expected.replace("isolated_restore_and_schema_compatibility_drill=yes\n", ""))
    elif invalid == "writable":
        ack.chmod(0o666)
    elif invalid == "symlink":
        external = tmp_path / "copied-ack"
        external.write_text(expected)
        ack.unlink()
        ack.symlink_to(external)
    with pytest.raises((release.ReleaseError, OSError)):
        release.promotion_preflight(tmp_path / "absent", "prod", commit,
                                    first_cutover_ack=ack, trust_root=fixture_authority)
    assert not (tmp_path / "absent").exists()


def test_first_cutover_ack_is_not_ambient_and_does_not_claim_full_rollback(tmp_path, fixture_authority, monkeypatch):
    commit = "c" * 40
    ack = fixture_authority / "prod/first-cutover" / commit
    ack.write_text(release._FIRST_CUTOVER_RECOVERY.format(commit=commit))
    ack.chmod(0o600)
    monkeypatch.setenv("CHUDWARE_FIRST_CUTOVER_ACK", str(ack))
    with pytest.raises(release.ReleaseError, match="explicit first-cutover"):
        release.promotion_preflight(tmp_path / "absent", "prod", commit, trust_root=fixture_authority)
    assert release.promotion_preflight(
        tmp_path / "absent", "prod", commit, first_cutover_ack=ack,
        trust_root=fixture_authority) == "first_cutover_manual_recovery_only"


def disposable_update_checkout(tmp_path):
    repo = tmp_path / "checkout"
    deploy = repo / "Webservice/deploy"
    deploy.mkdir(parents=True)
    for name in ("update.sh", "deployment_release.py", "gateway_preflight.py", "deployment_drain.sh"):
        shutil.copy2(DEPLOY / name, deploy / name)
    gateway = repo / "Webservice/gateway"
    gateway.mkdir()
    for name in ("deployment_config.py", "storage_config.py"):
        shutil.copy2(DEPLOY.parent / "gateway" / name, gateway / name)
    # The disposable build is observable and never invokes any product build.
    (repo / "build.sh").write_text("#!/bin/sh\nprintf mutated > build-was-run\nexit 77\n")
    (repo / "build.sh").chmod(0o700)
    for args in (["init", "-q"], ["add", "."],
                 ["-c", "user.name=Disposable Fixture", "-c", "user.email=fixture@invalid",
                  "commit", "-qm", "disposable inputs"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=10)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.defpath, "CHUDWARE_REPO_DIR": str(repo),
           "CHUDWARE_DEPLOY_LOCK": str(home / "deploy.lock")}
    return repo, home, env


@pytest.mark.parametrize("missing", [
    "runtime", "key", "rollback_authority", "target_commit", "dirty_checkout", "first_cutover_ack",
])
def test_disposable_update_refuses_before_mutation_without_rollback(tmp_path, missing):
    repo, home, env = disposable_update_checkout(tmp_path)
    before = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=10)
    flags = ["--gateway-only"] if missing in ("runtime", "key") else ["--host-only"]
    if missing == "runtime":
        env["CHUDWARE_GATEWAY_PYTHON"] = str(home / "absent-python")
    elif missing == "key":
        env["CHUDWARE_GATEWAY_PYTHON"] = sys.executable
        data = home / "chudware-web-data"
        data.mkdir()
        gateway_env = data / "gateway.env"
        gateway_env.write_text(
            "CHUDWARE_DEPLOY_ENV=prod\nCHUDWARE_WEB_DEV=0\n"
            "CHUDWARE_DB_URL=postgresql:///chudware_prod\n"
            "CHUDWARE_MAIL_BACKEND=smtp\nCHUDWARE_SMTP_HOST=127.0.0.1\n"
            "CHUDWARE_SMTP_USER=fixture\nCHUDWARE_SMTP_PASS=fixture-only\n"
            + "".join(f"{key}={home / name}\n" for key, name in (
                ("CHUDWARE_WEB_DATA_ROOT", "data"), ("CHUDWARE_WORKSPACE_ROOT", "workspaces"),
                ("CHUDWARE_WEB_BUCKET_ROOT", "bucket"), ("CHUDWARE_CELLS_ROOT", "cells"))))
        gateway_env.chmod(0o600)
    elif missing == "target_commit":
        env["CHUDWARE_DEPLOY_TARGET_SHA"] = "0" * 40
    elif missing == "dirty_checkout":
        (repo / "unqualified-change").write_text("unreviewed checkout change\n")
    elif missing == "first_cutover_ack":
        # A complete service-owned assertion still cannot confer independent authority.
        ack = home / "first-cutover-ack"
        ack.write_text(release._FIRST_CUTOVER_RECOVERY.format(commit=before.decode().strip()))
        ack.chmod(0o600)
        flags.extend(["--first-cutover-ack", str(ack)])
    result = subprocess.run(
        ["bash", str(repo / "Webservice/deploy/update.sh"), "prod", "--continuous", "--no-pull", *flags],
        env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 4, result.stdout + result.stderr
    reason = {
        "target_commit": "candidate no longer matches the independently gated commit",
        "dirty_checkout": "working tree is dirty",
        "rollback_authority": "rollback authority is unavailable for this exact candidate",
        "first_cutover_ack": "rollback authority is unavailable for this exact candidate",
    }.get(missing)
    if reason:
        assert reason in result.stdout + result.stderr
    assert not (repo / "build-was-run").exists()
    assert not (home / ".config/systemd").exists()
    assert not (home / "chudware-web/current").exists()
    assert not (home / "chudware-web-data/logs/deploy-recovery-required").exists()
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=10) == before
    assert "rollback.sh" not in result.stdout + result.stderr


@pytest.mark.parametrize("mutated,expected", [(0, 4), (1, 1)])
def test_real_update_exit_trap_classifies_later_failure(tmp_path, mutated, expected):
    repo, home, env = disposable_update_checkout(tmp_path)
    # Exercise the whole script and its actual EXIT trap, not extracted source.
    env.update(CHUDWARE_GATEWAY_PYTHON=str(home / "missing-python"),
               CHUDWARE_DEPLOY_MUTATED=str(mutated))
    result = subprocess.run(
        ["bash", str(repo / "Webservice/deploy/update.sh"), "prod", "--continuous", "--no-pull", "--gateway-only"],
        env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == expected
    import json
    assert json.loads((home / "chudware-web-data/logs/deploy-status.json").read_text())["last"]["rc"] == expected


@pytest.mark.parametrize("known_mutation", [True, False])
def test_automatic_rollback_names_manual_gateway_schema_recovery_without_touching_payload(tmp_path, known_mutation):
    home = tmp_path / "home"
    data = home / "chudware-web-data"
    (data / "logs").mkdir(parents=True)
    if known_mutation:
        (data / "logs/deploy-recovery-required").write_text("manual_gateway_checkout_and_database_schema_recovery_required\n")
    customer = data / "customer-data"
    customer.write_bytes(b"new customer writes must survive recovery")
    web = home / "chudware-web"
    (web / "releases/old").mkdir(parents=True)
    (web / "current").symlink_to("releases/old")
    result = subprocess.run(["bash", str(DEPLOY / "rollback.sh"), "--automatic"],
                            env={"HOME": str(home), "PATH": os.defpath},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 5
    assert "OPERATOR RECOVERY REQUIRED" in result.stderr
    if not known_mutation:
        assert "UNKNOWN" in result.stderr
    assert customer.read_bytes() == b"new customer writes must survive recovery"
    assert (web / "current").readlink() == Path("releases/old")
    assert not (web / ".cell-payload-installed-joint").exists()
    assert not (home / ".config/systemd").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="disposable root-authority namespace requires Linux")
def test_complete_disposable_update_classifies_failure_after_mutation(tmp_path):
    """A private user/mount namespace supplies fixture root authority, never host trust."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bubblewrap is required for the disposable release-authority smoke")
    repo, home, env = disposable_update_checkout(tmp_path)
    web = home / "chudware-web"
    retained = web / "releases"
    retained.mkdir(parents=True)
    manifest = retained_tree(retained / "authorized-prior")
    (web / "current").symlink_to("releases/authorized-prior")
    authority = tmp_path / "authority"
    (authority / "prod").mkdir(parents=True)
    (authority / "prod" / manifest["joint_sha256"]).write_text(manifest["release_tree_sha256"] + "\n")
    # A fresh tmpfs root makes every authorization ancestor genuinely uid 0
    # inside this namespace. Host /var/lib, services and credentials are absent.
    command = [
        bwrap, "--unshare-all", "--uid", "0", "--gid", "0", "--die-with-parent",
        "--tmpfs", "/", "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
        "--dir", "/etc", "--ro-bind", "/etc/passwd", "/etc/passwd",
        "--ro-bind", "/etc/group", "/etc/group",
        "--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
        "--dir", "/var", "--dir", "/var/lib",
        "--ro-bind", str(authority), "/var/lib/chudware-deployment-trust",
        "--bind", str(tmp_path), str(tmp_path), "--chdir", str(repo),
        "bash", str(repo / "Webservice/deploy/update.sh"), "prod", "--continuous",
        "--no-pull", "--host-only",
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (repo / "build-was-run").read_text() == "mutated"
    assert (web / "current").readlink() == Path("releases/authorized-prior")
    assert not (home / ".config/systemd").exists()
