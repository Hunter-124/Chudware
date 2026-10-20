"""Integration audit regressions: only disposable files/repos, no provider calls."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import time
import zipfile

import pytest
from dulwich.repo import Repo

from services.plm import box_mirror, generic_snapshot, gitbind, gitscm, manifest, remote, snapshot, worker_rpc
from services.plm.git_security import git_argv, git_env
from services.plm.vault import Vault, VaultError


def _container() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("document.json", json.dumps({"schema": "aicad.part/1", "id": "test-part",
                                                    "name": "Bracket", "features": []}))
    return stream.getvalue()


def _reachable_history_bytes(project) -> bytes:
    """Read every reachable real Git object, not just the current manifest."""
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    location = ["--git-dir", str(project / ".aicadvault")]
    objects = subprocess.run(
        git_argv([*location, "rev-list", "--objects", "--all"]),
        env=git_env(), check=True, capture_output=True,
    ).stdout
    ids = b"".join(line.split(b" ", 1)[0] + b"\n" for line in objects.splitlines())
    return subprocess.run(
        git_argv([*location, "cat-file", "--batch"]),
        input=ids, env=git_env(), check=True, capture_output=True,
    ).stdout


def _poison_remote_head(project, profiles):
    """Construct an actual legacy commit, deliberately bypassing today's writer."""
    v = Vault.open(project)
    try:
        m = v.manifest()
        m["remotes"] = profiles
        files = v._bind.read_tree(v._bind.head_commit())
        files["manifest.json"] = json.dumps(m).encode("utf-8")
        poisoned = v._bind.commit_tree(
            files, gitbind.EntryMeta(kind="auto", message="legacy fixture",
                                     author_name="Test", author_email="test@example.invalid"),
        )
        return poisoned, files["manifest.json"]
    finally:
        v.close()


@pytest.fixture
def git_repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    project = tmp_path / "repo"
    project.mkdir()
    for args in (["init"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"]):
        subprocess.run(git_argv(args), cwd=project, env=git_env(), check=True, capture_output=True)
    (project / "file.txt").write_text("first\n")
    assert gitscm.stage(str(project))["ok"]
    assert gitscm.commit(str(project), "first")["ok"]
    return project


def test_git_revision_options_never_write_output(git_repo, tmp_path):
    marker = tmp_path / "outside-marker"
    for call in (gitscm.log, gitscm.tree):
        result = call(str(git_repo), ref=f"--output={marker}")
        assert not result["ok"] and result["reason"] == "invalid_ref"
    assert not gitscm.file_at(str(git_repo), "file.txt", ref=f"--output={marker}")["ok"]
    assert not marker.exists()
    assert gitscm.log(str(git_repo), ref="HEAD")["commits"][0]["subject"] == "first"
    assert gitscm.file_at(str(git_repo), "file.txt", ref="HEAD")["content"] == "first\n"


def test_git_remote_and_branch_options_are_refused(git_repo, tmp_path):
    marker = tmp_path / "upload-pack-marker"
    helper = tmp_path / "upload-pack"
    helper.write_text(f"#!/bin/sh\nprintf injected > '{marker}'\nexit 1\n")
    helper.chmod(0o700)
    bare = tmp_path / "bare.git"
    bare.mkdir()
    Repo.init_bare(str(bare)).close()
    subprocess.run(git_argv(["remote", "add", "origin", str(bare)]), cwd=git_repo,
                   env=git_env(), check=True, capture_output=True)
    result = gitscm.fetch(str(git_repo), remote=f"--upload-pack={helper}")
    assert not result["ok"] and result["reason"] == "invalid_remote"
    for call in (gitscm.create_branch, gitscm.checkout_branch):
        assert not call(str(git_repo), "--orphan")["ok"]
    assert not marker.exists()
    assert gitscm.fetch(str(git_repo), "origin")["ok"]


def test_git_hooks_and_ambient_execution_overrides_are_ignored(git_repo, tmp_path, monkeypatch):
    marker = tmp_path / "hook-marker"
    hook = git_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf injected > '{marker}'\nexit 1\n")
    hook.chmod(0o700)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook.parent))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repo"))
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-never-forwarded")
    assert "OPENAI_API_KEY" not in git_env()
    (git_repo / "file.txt").write_text("second\n")
    assert gitscm.stage(str(git_repo))["ok"]
    assert gitscm.commit(str(git_repo), "second")["ok"]
    assert not marker.exists()


@pytest.mark.parametrize("location", ["--upload-pack=helper", "ext::helper", "git://example.invalid/repo",
                                       "http://example.invalid/repo", "ftp://example.invalid/repo",
                                       "https://user:dummy-secret@example.invalid/repo",
                                       "ssh://user:dummy-secret@example.invalid/repo",
                                       "https://example.invalid/repo?token=dummy-secret"])
def test_remote_transport_and_url_secrets_are_rejected_without_history(tmp_path, location):
    result = remote.add_remote(tmp_path, "origin", url=location, kind="https")
    assert not result["ok"] and result["reason"] == "invalid_mode"
    assert not (tmp_path / ".aicadvault").exists()


def test_manifest_serialization_forbids_url_credentials():
    value = manifest.new_manifest("Test", "now")
    value["remotes"] = [{"name": "origin", "kind": "https",
                          "url": "https://dummy-user:dummy-secret@example.invalid/repo"}]
    with pytest.raises(ValueError, match="credentials"):
        manifest.dumps(value)
    with pytest.raises(ValueError, match="credentials"):
        manifest.loads(json.dumps(value).encode())


def test_fetched_remote_destination_requires_explicit_local_approval(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "Bracket.step+").write_bytes(_container())
    v = Vault.open(source)
    v.save()
    v.close()
    legitimate = tmp_path / "legitimate.git"
    attacker = tmp_path / "attacker.git"
    attacker.mkdir()
    Repo.init_bare(str(attacker)).close()
    assert remote.seed_remote(source, legitimate)["ok"]
    victim = tmp_path / "victim"
    assert remote.clone_from_remote(legitimate, victim)["ok"]
    assert remote.sync(victim)["ok"]
    assert remote.sync(source)["ok"]
    v = Vault.open(source)
    changed = v.manifest()
    changed["remotes"][0]["url"] = str(attacker)
    v._commit_manifest_change(changed)
    v.close()
    # An untrusted peer publishes a malicious manifest to the legitimate repo.
    assert remote._sync_once(source, str(legitimate), "origin")["ok"]
    assert remote.sync(victim)["ok"]
    v = Vault.open(victim)
    try:
        assert v.manifest()["remotes"][0]["url"] == str(attacker)
        v.checkpoint(message="victim-only private work")
    finally:
        v.close()
    for operation in (remote.sync, remote.list_locks):
        result = operation(victim)
        assert not result["ok"] and result["reason"] == "remote_destination_unapproved"
    repo = Repo(str(attacker))
    try:
        assert remote._branch_tip(repo) is None
    finally:
        repo.close()
    assert remote.add_remote(victim, "origin", str(attacker), kind="folder")["ok"]
    assert remote.sync(victim)["pushed"]


def test_versioned_destination_without_local_approval_is_not_contacted(tmp_path, monkeypatch):
    v = Vault.open(tmp_path)
    try:
        value = v.manifest()
        value["remotes"] = [{"name": "origin", "id": "rpeer", "kind": "folder",
                             "url": str(tmp_path / "peer.git")}]
        v._commit_manifest_change(value)
    finally:
        v.close()
    contacted = []
    monkeypatch.setattr(remote, "_fetch_folder", lambda *args: contacted.append(args))
    result = remote.sync(tmp_path)
    assert not result["ok"] and result["reason"] == "remote_destination_unapproved"
    assert contacted == []


def test_zip_attachment_ratio_is_rejected_before_expansion(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("document.json", "{}")
        archive.writestr("attachment.bin", b"\0" * (1024 * 1024))
    path = tmp_path / "compressed.step+"
    path.write_bytes(data.getvalue())
    with pytest.raises(ValueError, match="compression ratio"):
        snapshot.explode_container(path)


@pytest.mark.parametrize("rel", [".aicadvault/config", ".git/config", "nested/.git/config",
                                  ".GIT/config", ".gitmodules", ".hg/hgrc", "nested\\.git\\config",
                                  ".git./config", ".git /config", ".git::$INDEX_ALLOCATION/x",
                                  ".aicadvault:s/config", "nested/.GIT. /config",
                                  "nested\\.aicadvault.:stream\\config",
                                  ".gitmodules:stream", ".gitattributes. "])
def test_reserved_control_paths_cannot_be_registered_or_materialized(tmp_path, rel):
    with pytest.raises(VaultError, match="reserved"):
        generic_snapshot.contained_path(tmp_path, rel)
    result = worker_rpc._plm_item_add({"path": str(tmp_path), "file_path": rel}, None)
    assert not result["ok"] and result["reason"] == "invalid_mode"
    assert "reserved" in result["hint"]


@pytest.mark.parametrize("rel", [".aicadvault/config", ".git./config", ".git /config",
                                  ".git::$INDEX_ALLOCATION/config", ".aicadvault:stream/config"])
def test_incoming_manifest_preflight_preserves_every_control_file(tmp_path, rel):
    (tmp_path / "Bracket.step+").write_bytes(_container())
    v = Vault.open(tmp_path)
    try:
        m = v.manifest()
        before = (tmp_path / ".aicadvault" / "config").read_bytes()
        head = v._bind.head_commit()
        repo = v._bind._repo
        files = v._bind.read_tree(head)
        # First valid item must not be written before the later hostile item.
        m["items"].append({**m["items"][0], "path": rel})
        files["manifest.json"] = manifest.dumps(m)
        commit = remote._commit_files(repo, files, parent=head.encode(), message="malicious paths",
                                      author={"id": "test", "display": "Test"}, kind="auto")
        original = (tmp_path / "Bracket.step+").read_bytes()
        with pytest.raises(VaultError, match="reserved"):
            remote._materialize_working_tree(tmp_path, repo, commit)
        assert (tmp_path / ".aicadvault" / "config").read_bytes() == before
        assert (tmp_path / "Bracket.step+").read_bytes() == original
    finally:
        v.close()


def _mirror_stubs(monkeypatch):
    state = {"folders": {}, "files": {}, "parent_folder_id": "root"}
    uploaded = []
    monkeypatch.setattr(box_mirror, "_access_token", lambda box: "dummy-token")
    monkeypatch.setattr(box_mirror, "_load_state", lambda project: state)
    monkeypatch.setattr(box_mirror, "_save_state", lambda *args: None)
    monkeypatch.setattr(box_mirror, "_ensure_folder", lambda *args: "folder")
    monkeypatch.setattr(box_mirror, "_upload",
                        lambda token, fid, parent, name, payload: (uploaded.append((name, payload)) or "file", False))
    return state, uploaded


def _symlink(source, target):
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
    except OSError:
        pytest.skip("symlink creation unavailable")


def test_box_mirror_never_uploads_file_or_directory_symlink_targets(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside-private-marker")
    _symlink(outside, project / "manual.pdf")
    _symlink(tmp_path, project / "linked-directory")
    (project / "normal.txt").write_bytes(b"authored")
    state, uploaded = _mirror_stubs(monkeypatch)
    result = box_mirror._mirror_project(str(project), {"folder_id": "root", "max_file_mb": 1}, time.time() + 30)
    assert result["uploaded"] == 1
    assert uploaded == [("normal.txt", b"authored")]
    assert state["files"]["normal.txt"]["sha1"] == hashlib.sha1(b"authored").hexdigest()


def test_box_mirror_refuses_symlink_swap_after_discovery(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    file = project / "manual.pdf"
    file.write_bytes(b"authored")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside-private-marker")
    _, uploaded = _mirror_stubs(monkeypatch)
    def raced_scan(root):
        file.unlink()
        _symlink(outside, file)
        yield "manual.pdf", file, 8
    monkeypatch.setattr(box_mirror, "_iter_project_files", raced_scan)
    box_mirror._mirror_project(str(project), {"folder_id": "root", "max_file_mb": 1}, time.time() + 30)
    assert uploaded == []


def test_box_hash_and_upload_use_same_verified_bytes(tmp_path, monkeypatch):
    project = tmp_path / "project"
    nested = project / "docs"
    nested.mkdir(parents=True)
    file = nested / "manual.pdf"
    file.write_bytes(b"authored")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside-private-marker")
    state, uploaded = _mirror_stubs(monkeypatch)
    def raced_folder(token, state, parent, key, name):
        if key == "docs":
            file.unlink()
            _symlink(outside, file)
        return "folder"
    monkeypatch.setattr(box_mirror, "_ensure_folder", raced_folder)
    box_mirror._mirror_project(str(project), {"folder_id": "root", "max_file_mb": 1}, time.time() + 30)
    assert uploaded == [("manual.pdf", b"authored")]
    assert state["files"]["docs/manual.pdf"]["sha1"] == hashlib.sha1(b"authored").hexdigest()


def test_git_configured_helper_transport_is_refused(git_repo, tmp_path):
    marker = tmp_path / "helper-marker"
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\nprintf injected > '{marker}'\nexit 1\n")
    helper.chmod(0o700)
    subprocess.run(git_argv(["config", "remote.origin.url", f"ext::{helper}"]), cwd=git_repo,
                   env=git_env(), check=True, capture_output=True)
    for operation in (gitscm.fetch, gitscm.pull, gitscm.push):
        result = operation(str(git_repo))
        assert not result["ok"] and result["reason"] == "invalid_remote"
    assert not marker.exists()


def test_incoming_container_cannot_replace_a_directory(tmp_path):
    original = _container()
    (tmp_path / "Bracket.step+").write_bytes(original)
    (tmp_path / "existing-directory").mkdir()
    v = Vault.open(tmp_path)
    try:
        value = v.manifest()
        head = v._bind.head_commit()
        files = v._bind.read_tree(head)
        value["items"][0]["path"] = "existing-directory"
        files["manifest.json"] = manifest.dumps(value)
        commit = remote._commit_files(v._bind._repo, files, parent=head.encode(),
                                      message="incompatible destination", author={"id": "test"}, kind="auto")
        with pytest.raises(VaultError, match="incompatible type"):
            remote._materialize_working_tree(tmp_path, v._bind._repo, commit)
        assert (tmp_path / "existing-directory").is_dir()
        assert (tmp_path / "Bracket.step+").read_bytes() == original
    finally:
        v.close()


@pytest.mark.parametrize("url", [
    "https://sentinel-link-user@example.invalid/repo",
    "https://user:sentinel-link-secret@example.invalid/repo",
    "ssh://user:sentinel-link-secret@example.invalid/repo",
    "https://user:sentinel-link-secret@example.invalid\uff0fpath",
    "https://example.invalid/repo?token=sentinel-link-secret",
    "https://example.invalid/repo?ACCESS_TOKEN=sentinel-link-secret",
    "https://example.invalid/repo?%70assword=sentinel-link-secret",
    "https://example.invalid/repo?Authorization=sentinel-link-secret",
    "https://example.invalid/repo?bearer=sentinel-link-secret",
])
def test_link_credentials_refuse_worker_request_before_any_history_write(tmp_path, url, caplog):
    v = Vault.open(tmp_path)
    try:
        before = v._bind.head_commit()
    finally:
        v.close()
    history_before = _reachable_history_bytes(tmp_path)
    result = worker_rpc._plm_link_repo(
        {"path": str(tmp_path), "url": url, "name": "Code", "checkout_path": "unused"}, None,
    )
    assert not result["ok"] and result["reason"] == "invalid_mode"
    assert "sentinel-link" not in json.dumps(result) + caplog.text
    v = Vault.open(tmp_path)
    try:
        assert v._bind.head_commit() == before
        assert v.manifest()["items"] == []
        assert "link_checkouts" not in v.get_local_config()
    finally:
        v.close()
    assert _reachable_history_bytes(tmp_path) == history_before
    assert b"sentinel-link" not in history_before


@pytest.mark.parametrize("field", ["token", "access_token", "bearer", "authorization", "password"])
def test_live_query_credentials_refuse_worker_request_before_vault_creation(tmp_path, field, caplog):
    result = worker_rpc._plm_add_remote(
        {"path": str(tmp_path), "name": "live", "kind": "plm-server",
         "url": f"wss://example.invalid/live?{field}=sentinel-live-secret"}, None,
    )
    assert not result["ok"] and result["reason"] == "invalid_mode"
    assert not (tmp_path / ".aicadvault").exists()
    assert "sentinel-live-secret" not in json.dumps(result) + caplog.text


@pytest.mark.parametrize("profile", [
    {"name": "origin", "id": "rlegacy", "kind": "https",
     "url": "https://user:sentinel-legacy-secret@example.invalid/repo"},
    {"name": "origin", "id": "rlegacy", "kind": "https",
     "url": "https://example.invalid/repo?password=sentinel-legacy-secret"},
    {"name": "origin", "id": "rlegacy", "kind": "https",
     "url": "https://example.invalid/repo", "authorization": "sentinel-legacy-secret"},
    {"name": "origin", "id": "rlegacy", "kind": "https",
     "url": "https://user:sentinel-legacy-secret@example.invalid\uff0fpath"},
])
def test_legacy_remote_reads_never_write_and_explicit_replacement_repairs(
    tmp_path, monkeypatch, caplog, profile,
):
    (tmp_path / "Bracket.step+").write_bytes(_container())
    poisoned, raw = _poison_remote_head(tmp_path, [profile])
    history_before = _reachable_history_bytes(tmp_path)
    assert b"sentinel-legacy-secret" in history_before
    v = Vault.open(tmp_path)
    try:
        v.set_local_config({"approved_remotes": {
            "origin": {"kind": profile["kind"], "url": profile["url"]},
        }})
        config_before = (tmp_path / ".aicadvault" / "config.json").read_bytes()
    finally:
        v.close()
    contacted = []
    real_fetch, real_ls = remote._fetch_folder, remote._ls_remote
    def forbidden_network(*args, **kwargs):
        contacted.append(True)
        raise AssertionError("blocked legacy destination contacted")
    monkeypatch.setattr(remote, "_fetch_folder", forbidden_network)
    monkeypatch.setattr(remote, "_ls_remote", forbidden_network)
    responses = [
        worker_rpc._plm_open({"path": str(tmp_path)}, None),
        worker_rpc._plm_status({"path": str(tmp_path)}, None),
        worker_rpc._plm_settings_get({"path": str(tmp_path)}, None),
    ]
    assert all(response["ok"] for response in responses)
    diagnostic = responses[0]["manifest"]["remotes"][0]
    assert diagnostic["url"] is None and diagnostic["credential_repair_required"]
    for operation in (remote.sync, remote.list_locks):
        result = operation(tmp_path)
        assert not result["ok"] and result["reason"] == "remote_destination_unapproved"
        responses.append(result)
    v = Vault.open(tmp_path)
    try:
        with pytest.raises(VaultError, match="explicit replacement"):
            v.checkpoint(message="not a remote repair")
        with pytest.raises(VaultError, match="explicit replacement"):
            v._commit_sibling("planning/test.json", b"{}", scope="planning")
        assert v._bind.head_commit() == poisoned
        assert v._bind.read_blob_at(poisoned, "manifest.json") == raw
        assert (tmp_path / ".aicadvault" / "config.json").read_bytes() == config_before
    finally:
        v.close()
    assert _reachable_history_bytes(tmp_path) == history_before
    assert contacted == []
    assert "sentinel-legacy-secret" not in json.dumps(responses) + caplog.text

    replacement = tmp_path / "approved.git"
    replacement.mkdir()
    Repo.init_bare(str(replacement)).close()
    repaired = worker_rpc._plm_add_remote(
        {"path": str(tmp_path), "name": "origin", "kind": "folder", "url": str(replacement)}, None,
    )
    assert repaired["ok"] and repaired["remote"]["id"] == "rlegacy"
    v = Vault.open(tmp_path)
    try:
        head = v._bind.head_commit()
        assert head != poisoned
        assert v._bind.commit_meta(head)["parents"] == [poisoned]
        stored = v._bind.read_blob_at(head, "manifest.json")
        assert b"sentinel-legacy-secret" not in stored
        assert b"credential_repair_required" not in stored
        entry = manifest.loads(stored)["remotes"][0]
        assert remote.approved_remote_url(tmp_path, entry) == str(replacement)
        assert v._bind.read_blob_at(poisoned, "manifest.json") == raw
        assert v.get_local_config()["approved_remotes"]["origin"] == {
            "kind": "folder", "url": str(replacement),
        }
        v.checkpoint(message="ordinary snapshots work after explicit repair")
    finally:
        v.close()
    monkeypatch.setattr(remote, "_fetch_folder", real_fetch)
    monkeypatch.setattr(remote, "_ls_remote", real_ls)
    assert remote.sync(tmp_path)["ok"]
    assert "sentinel-legacy-secret" not in json.dumps(repaired) + caplog.text


def test_multiple_poisoned_remotes_require_each_explicit_replacement(tmp_path, monkeypatch, caplog):
    profiles = [
        {"name": "origin", "id": "rone", "kind": "https",
         "url": "https://user:sentinel-one@example.invalid/repo"},
        {"name": "live", "id": "rtwo", "kind": "plm-server",
         "url": "wss://example.invalid/live?password=sentinel-two",
         "capabilities": ["snapshot_sync", "live_coedit", "presence"]},
    ]
    poisoned, raw = _poison_remote_head(tmp_path, profiles)
    v = Vault.open(tmp_path)
    try:
        v.set_local_config({"approved_remotes": {
            "live": {"kind": "plm-server", "url": "wss://example.invalid/live"},
        }})
        with pytest.raises(ValueError, match="internal remote diagnostics"):
            manifest.dumps(v.manifest())
        with pytest.raises(ValueError, match="internal remote diagnostics"):
            manifest.loads(json.dumps(v.manifest()).encode())
    finally:
        v.close()
    approved = tmp_path / "approved.git"
    approved.mkdir()
    Repo.init_bare(str(approved)).close()
    first = remote.add_remote(tmp_path, "origin", approved, kind="folder")
    assert first["ok"]
    v = Vault.open(tmp_path)
    try:
        head = v._bind.head_commit()
        stored = v._bind.read_blob_at(head, "manifest.json")
        assert b"sentinel-" not in stored and b"credential_repair_required" not in stored
        assert manifest.loads(stored)["remotes"][1]["url"] is None
        blocked = v.manifest()["remotes"][1]
        assert blocked["url"] is None and blocked["credential_repair_required"]
        with pytest.raises(VaultError, match="explicit replacement"):
            v.set_ai_mode("manual")
        with pytest.raises(VaultError) as refusal:
            remote.approved_remote_url(tmp_path, blocked)
        assert refusal.value.reason == "remote_destination_unapproved"
        assert v._bind.head_commit() == head
        assert v._bind.read_blob_at(poisoned, "manifest.json") == raw
    finally:
        v.close()
    contacted = []
    monkeypatch.setattr(remote, "_fetch_folder", lambda *args: contacted.append(args))
    result = remote.sync(tmp_path, "live")
    assert not result["ok"] and contacted == []
    second = remote.add_remote(tmp_path, "live", kind="plm-server", url="wss://approved.invalid/live")
    assert second["ok"]
    v = Vault.open(tmp_path)
    try:
        m = v.manifest()
        assert not any(r.get("credential_repair_required") for r in m["remotes"])
        assert m["remotes"][1]["id"] == "rtwo"
        assert remote.approved_remote_url(tmp_path, m["remotes"][1]) == "wss://approved.invalid/live"
        v.checkpoint(message="both remotes explicitly repaired")
        assert v._bind.read_blob_at(poisoned, "manifest.json") == raw
    finally:
        v.close()
    assert "sentinel-" not in json.dumps([first, result, second]) + caplog.text


def test_benign_posix_colon_file_and_normal_link_snapshot_round_trip(tmp_path):
    import os
    if os.name == "nt":
        pytest.skip("colon filenames are POSIX-only")
    original = b"ordinary authored bytes"
    (tmp_path / "manual:notes.pdf").write_bytes(original)
    v = Vault.open(tmp_path)
    try:
        tracked = v.add_item("manual:notes.pdf", doc_type="report")
        linked = v.link_repo("https://example.invalid/repo?view=source", "Code")
        saved = v.checkpoint(message="valid ordinary snapshot")
        (tmp_path / "manual:notes.pdf").write_bytes(b"changed")
        v.restore(saved["commit"], tracked["item_id"])
        assert (tmp_path / "manual:notes.pdf").read_bytes() == original
        files = v._bind.read_tree(v._bind.head_commit())
        link = json.loads(files[generic_snapshot.link_path(linked["item_id"])])
        assert link["url"] == "https://example.invalid/repo?view=source"
    finally:
        v.close()


@pytest.mark.parametrize("kind,url", [
    ("https", "https://user:sentinel-malformed-secret@example.invalid\uff0fpath"),
    ("plm-server", "wss://user:sentinel-malformed-secret@example.invalid\uff0fpath"),
])
def test_malformed_credential_authority_never_appears_in_remote_errors(tmp_path, kind, url, caplog):
    result = worker_rpc._plm_add_remote(
        {"path": str(tmp_path), "name": "origin", "kind": kind, "url": url}, None,
    )
    assert not result["ok"] and result["reason"] == "invalid_mode"
    assert "sentinel-malformed-secret" not in json.dumps(result) + caplog.text
    assert not (tmp_path / ".aicadvault").exists()


def test_peer_diagnostic_marker_is_rejected_before_working_file_write(tmp_path):
    original = _container()
    (tmp_path / "Bracket.step+").write_bytes(original)
    _poison_remote_head(tmp_path, [
        {"name": "origin", "kind": "https",
         "url": "https://user:sentinel-peer-secret@example.invalid/repo"},
    ])
    v = Vault.open(tmp_path)
    try:
        head = v._bind.head_commit()
        files = v._bind.read_tree(head)
        files["manifest.json"] = json.dumps(v.manifest()).encode("utf-8")
        incoming = remote._commit_files(
            v._bind._repo, files, parent=head.encode(), message="peer diagnostics fixture",
            author={"id": "test", "display": "Test"}, kind="auto",
        )
        (tmp_path / "Bracket.step+").write_bytes(b"unsaved authored content")
        with pytest.raises(ValueError, match="internal remote diagnostics"):
            remote._materialize_working_tree(tmp_path, v._bind._repo, incoming)
        assert (tmp_path / "Bracket.step+").read_bytes() == b"unsaved authored content"
        assert v._bind.head_commit() == head
    finally:
        v.close()
