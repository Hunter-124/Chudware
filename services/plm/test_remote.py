"""Tests for folder remotes, sync, locks, and author injection.

Covers (acceptance):
  * seed + clone round-trip
  * two clones alternating saves + sync (linear history)
  * diverged → copy-as-variant (§6.4)
  * lock CAS contention
  * author env injection appears in history entries

No engine C++ dependency; dulwich only.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from services.plm import remote
from services.plm.vault import Vault, _user_author


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(name: str = "Bracket", wall: float = 3.0, uid: str = "uuid-part-1") -> dict:
    return {
        "schema": "aicad.part/1",
        "id": uid,
        "name": name,
        "features": [{"id": "f1", "params": {"wall": wall}}],
    }


def _write_part(project: Path, filename: str, wall: float = 3.0, uid: str = "uuid-part-1") -> None:
    name = filename.replace(".step+", "")
    (project / filename).write_bytes(_container(_part(name=name, wall=wall, uid=uid)))


def _read_wall(project: Path, filename: str = "Bracket.step+") -> float:
    raw = (project / filename).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        doc = json.loads(zf.read("document.json"))
    return float(doc["features"][0]["params"]["wall"])


# ---------------------------------------------------------------------------
# seed + clone
# ---------------------------------------------------------------------------

def test_seed_and_clone_roundtrip(tmp_path):
    src = tmp_path / "alice"
    src.mkdir()
    _write_part(src, "Bracket.step+", wall=3.0)
    v = Vault.open(src)
    try:
        v.save()
        head = v.status()["head"]
        assert head
    finally:
        v.close()

    bare = tmp_path / "central.git"
    out = remote.seed_remote(src, bare)
    assert out["ok"] is True, out
    assert bare.is_dir()
    assert (bare / "objects").is_dir() or (bare / "HEAD").is_file()

    bob = tmp_path / "bob"
    out = remote.clone_from_remote(bare, bob)
    assert out["ok"] is True, out
    assert (bob / "Bracket.step+").is_file()
    assert _read_wall(bob) == 3.0
    assert Vault.exists(bob)

    vb = Vault.open(bob)
    try:
        st = vb.status()
        assert st["head"]
        assert any(it["path"] == "Bracket.step+" for it in st["items"])
        remotes = st["remotes"]
        assert any(r.get("name") == "origin" for r in remotes)
    finally:
        vb.close()


# ---------------------------------------------------------------------------
# linear alternating sync
# ---------------------------------------------------------------------------

def test_two_clones_linear_alternating_sync(tmp_path):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+", wall=3.0)
    v = Vault.open(src)
    v.save()
    v.close()

    bare = tmp_path / "team.git"
    assert remote.seed_remote(src, bare)["ok"]

    a = tmp_path / "alice"
    b = tmp_path / "bob"
    assert remote.clone_from_remote(bare, a)["ok"]
    assert remote.clone_from_remote(bare, b)["ok"]

    # Alice edits + saves + syncs
    _write_part(a, "Bracket.step+", wall=5.0)
    va = Vault.open(a)
    va.save(message="alice wall 5")
    va.close()
    out = remote.sync(a, remote="origin")
    assert out["ok"] is True, out
    assert out["pushed"] is True

    # Bob syncs (pull), edits further, syncs
    out = remote.sync(b, remote="origin")
    assert out["ok"] is True, out
    assert out["pulled"] is True
    assert _read_wall(b) == 5.0

    _write_part(b, "Bracket.step+", wall=7.0)
    vb = Vault.open(b)
    vb.save(message="bob wall 7")
    vb.close()
    out = remote.sync(b, remote="origin")
    assert out["ok"] is True, out
    assert out["pushed"] is True

    # Alice pulls bob's tip
    out = remote.sync(a, remote="origin")
    assert out["ok"] is True, out
    assert out["pulled"] is True
    assert _read_wall(a) == 7.0

    # histories are linear and share the same HEAD
    va = Vault.open(a)
    vb = Vault.open(b)
    try:
        assert va.status()["head"] == vb.status()["head"]
        ha = [e["commit"] for e in va.history()]
        hb = [e["commit"] for e in vb.history()]
        assert ha == hb
        # parent chain is single-parent (linear)
        from services.plm import gitbind
        bind = gitbind.open_binding(a)
        try:
            for cid in ha:
                parents = bind.commit_meta(cid)["parents"]
                assert len(parents) <= 1
        finally:
            bind.close()
    finally:
        va.close()
        vb.close()


@pytest.mark.parametrize("divergent", [False, True])
def test_sync_refuses_unsaved_work_before_pull(tmp_path, divergent):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+", wall=3.0)
    v = Vault.open(src)
    v.save()
    v.close()
    bare = tmp_path / "team.git"
    assert remote.seed_remote(src, bare)["ok"]
    alice, bob = tmp_path / "alice", tmp_path / "bob"
    assert remote.clone_from_remote(bare, alice)["ok"]
    assert remote.clone_from_remote(bare, bob)["ok"]

    _write_part(alice, "Bracket.step+", wall=5.0)
    va = Vault.open(alice)
    va.checkpoint("alice change")
    va.close()
    assert remote.sync(alice)["ok"]

    if divergent:
        _write_part(bob, "Bracket.step+", wall=7.0)
        vb = Vault.open(bob)
        vb.checkpoint("bob saved change")
        vb.close()
    _write_part(bob, "Bracket.step+", wall=9.0)
    before = (bob / "Bracket.step+").read_bytes()
    vb = Vault.open(bob)
    head = vb._bind.head_commit()
    vb.close()

    result = remote.sync(bob)
    assert result["ok"] is False
    assert result["reason"] == "unsaved_changes"
    assert (bob / "Bracket.step+").read_bytes() == before
    vb = Vault.open(bob)
    try:
        assert vb._bind.head_commit() == head
        assert vb._dirty_items(vb.manifest())
    finally:
        vb.close()


def test_sync_rejects_peer_manifest_with_escape_before_overwrite(tmp_path):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+")
    v = Vault.open(src)
    v.save()
    v.close()
    bare = tmp_path / "team.git"
    assert remote.seed_remote(src, bare)["ok"]
    alice, bob = tmp_path / "alice", tmp_path / "bob"
    assert remote.clone_from_remote(bare, alice)["ok"]
    assert remote.clone_from_remote(bare, bob)["ok"]
    outside = tmp_path / "secret.txt"
    outside.write_text("untouched")

    va = Vault.open(alice)
    try:
        m = va.manifest()
        m["items"][0]["path"] = "../secret.txt"
        files = va._bind.read_tree(va._bind.head_commit())
        files["manifest.json"] = remote.manifest.dumps(m)
        va._bind.commit_tree(files, remote.gitbind.EntryMeta(
            kind=remote.hist.KIND_CHECKPOINT, message="crafted peer history",
            author_name="Test", author_email="test@example.org",
        ))
    finally:
        va.close()
    assert remote.sync(alice)["ok"]
    vb = Vault.open(bob)
    before = vb._bind.head_commit()
    vb.close()
    result = remote.sync(bob)
    assert result["ok"] is False
    assert result["reason"] == "invalid_mode"
    assert outside.read_text() == "untouched"
    vb = Vault.open(bob)
    try:
        assert vb._bind.head_commit() == before
    finally:
        vb.close()


# ---------------------------------------------------------------------------
# diverge → copy-as-variant
# ---------------------------------------------------------------------------

def test_diverged_copy_as_variant(tmp_path):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+", wall=3.0)
    v = Vault.open(src)
    v.save()
    item_id = v.status()["items"][0]["item_id"]
    v.close()

    bare = tmp_path / "team.git"
    assert remote.seed_remote(src, bare)["ok"]

    a = tmp_path / "alice"
    b = tmp_path / "bob"
    assert remote.clone_from_remote(bare, a)["ok"]
    assert remote.clone_from_remote(bare, b)["ok"]

    # both edit offline from the same base
    _write_part(a, "Bracket.step+", wall=11.0)
    va = Vault.open(a)
    va.save(message="alice 11")
    va.close()

    _write_part(b, "Bracket.step+", wall=22.0)
    vb = Vault.open(b)
    vb.save(message="bob 22")
    queued_head = vb.status()["head"]
    queued_history = [entry["commit"] for entry in vb.history()]
    vb.close()

    # Local history is the durable offline queue: an unreachable share must not
    # consume or rewrite Bob's saved work.
    offline = tmp_path / "team-offline.git"
    bare.rename(offline)
    failed = remote.sync(b, remote="origin")
    assert failed["ok"] is False
    bare = offline.rename(bare)
    vb = Vault.open(b)
    try:
        assert vb.status()["head"] == queued_head
        assert [entry["commit"] for entry in vb.history()] == queued_history
    finally:
        vb.close()

    # Alice publishes first
    out = remote.sync(a, remote="origin")
    assert out["ok"] is True, out
    assert out["pushed"] is True

    # Bob syncs: diverged → remote (alice) wins Bracket; bob's copy survives
    os.environ["CHUDWARE_USER_ID"] = "bob"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Bob"
    try:
        out = remote.sync(b, remote="origin")
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert out["ok"] is True, out
    assert out.get("conflicts_copied"), out
    conflicts = out["conflicts_copied"]
    assert any(c["original_item_id"] == item_id for c in conflicts)

    # working tree: canonical Bracket is alice's (11); bob's edits under new name
    assert _read_wall(b, "Bracket.step+") == 11.0
    variants = list(b.glob("Bracket (*'s edits).step+"))
    assert variants, "expected a copy-as-variant working file"
    assert _read_wall(b, variants[0].name) == 22.0

    vb = Vault.open(b)
    try:
        items = vb.status()["items"]
        assert len(items) >= 2
        paths = {it["path"] for it in items}
        assert "Bracket.step+" in paths
        assert any("edits" in p for p in paths)
        # both item_ids present and distinct
        ids = {it["item_id"] for it in items}
        assert item_id in ids
        assert len(ids) >= 2
    finally:
        vb.close()


# ---------------------------------------------------------------------------
# lock CAS
# ---------------------------------------------------------------------------

def test_lock_cas_contention(tmp_path):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+")
    v = Vault.open(src)
    v.save()
    item_id = v.status()["items"][0]["item_id"]
    v.close()

    bare = tmp_path / "team.git"
    assert remote.seed_remote(src, bare)["ok"]

    a = tmp_path / "alice"
    b = tmp_path / "bob"
    assert remote.clone_from_remote(bare, a)["ok"]
    assert remote.clone_from_remote(bare, b)["ok"]

    os.environ["CHUDWARE_USER_ID"] = "alice"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Alice"
    try:
        out = remote.plm_lock(a, item_id, remote="origin")
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert out["ok"] is True, out
    assert out["lock"]["holder"] == "alice"
    assert out["lock"]["item_id"] == item_id

    os.environ["CHUDWARE_USER_ID"] = "bob"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Bob"
    try:
        out = remote.plm_lock(b, item_id, remote="origin")
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert out["ok"] is False
    assert out["reason"] == "locked"
    assert "Alice" in out.get("hint", "") or "alice" in out.get("hint", "").lower()

    # alice unlocks; bob can take it
    os.environ["CHUDWARE_USER_ID"] = "alice"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Alice"
    try:
        out = remote.plm_unlock(a, item_id, remote="origin")
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert out["ok"] is True, out

    os.environ["CHUDWARE_USER_ID"] = "bob"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Bob"
    try:
        out = remote.plm_lock(b, item_id, remote="origin")
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert out["ok"] is True, out
    assert out["lock"]["holder"] == "bob"


# ---------------------------------------------------------------------------
# author env injection
# ---------------------------------------------------------------------------

def test_author_env_injection_in_history(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    _write_part(project, "Bracket.step+")

    monkeypatch.setenv("CHUDWARE_USER_ID", "sarah@example.com")
    monkeypatch.setenv("CHUDWARE_USER_DISPLAY", "Sarah")
    monkeypatch.delenv("CHUDWARE_ACCOUNT_ID", raising=False)
    assert _user_author() == {
        "kind": "user",
        "id": "sarah@example.com",
        "display": "Sarah",
        "account_id": None,
    }
    v = Vault.open(project)
    try:
        # re-save so a user-authored entry lands (seed uses System)
        _write_part(project, "Bracket.step+", wall=4.0)
        v.save(message="sarah edit")
        entries = v.history()
        authored = [
            e for e in entries if e["author"].get("id") == "sarah@example.com"
        ]
        assert authored, entries
        assert authored[0]["author"]["display"] == "Sarah"
        assert authored[0]["author"]["kind"] == "user"
        assert authored[0]["author"]["account_id"] is None
        # document.json must not carry author (byte-stability)
        head = v.status()["head"]
        tree = v._bind.read_tree(head)
        for path, data in tree.items():
            if path.endswith("/document.json"):
                doc = json.loads(data.decode("utf-8"))
                assert "author" not in doc
    finally:
        v.close()


def test_reserved_aicad_cloud_remote(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_part(project, "Bracket.step+")
    v = Vault.open(project)
    v.save()
    v.close()

    out = remote.add_remote(project, "Cloud", kind="chudware-cloud")
    assert out["ok"] is True, out
    assert out["remote"]["kind"] == "chudware-cloud"
    assert out["remote"].get("reserved") is True

    out = remote.sync(project, remote="Cloud")
    assert out["ok"] is False
    assert out["reason"] == "reserved_remote"


def test_remote_identity_capabilities_and_account_reservation(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    _write_part(project, "Bracket.step+")
    v = Vault.open(project)
    v.save()
    v.close()
    share = tmp_path / "share.git"
    assert remote.seed_remote(project, share)["ok"]

    explicit = remote.add_remote(
        project,
        "origin",
        str(share),
        kind="folder",
        display_name="Sarah",
        account_id="acct-42",
    )
    assert explicit["ok"], explicit
    profile = explicit["remote"]
    assert profile["identity"] == {
        "user_id": _user_author()["id"],
        "display": "Sarah",
        "account_id": "acct-42",
    }
    assert profile["capabilities"] == ["snapshot_sync", "advisory_locks"]

    monkeypatch.delenv("CHUDWARE_USER_ID", raising=False)
    monkeypatch.delenv("CHUDWARE_USER_DISPLAY", raising=False)
    monkeypatch.delenv("CHUDWARE_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(remote.getpass, "getuser", lambda: "os-user")
    fallback = remote.add_remote(project, "backup", str(share), kind="folder")
    assert fallback["remote"]["identity"] == {
        "user_id": "os-user",
        "display": "os-user",
        "account_id": None,
    }


def test_status_lists_live_locks_and_offline_remote_is_best_effort(
    tmp_path, monkeypatch
):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+")
    v = Vault.open(src)
    v.save()
    item_id = v.status()["items"][0]["item_id"]
    v.close()
    share = tmp_path / "team.git"
    assert remote.seed_remote(src, share)["ok"]

    monkeypatch.setenv("CHUDWARE_USER_ID", "alice")
    monkeypatch.setenv("CHUDWARE_USER_DISPLAY", "Alice")
    monkeypatch.setenv("CHUDWARE_ACCOUNT_ID", "acct-alice")
    acquired = remote.plm_lock(src, item_id)
    assert acquired["ok"], acquired

    v = Vault.open(src)
    try:
        locks = v.status()["locks"]
        assert locks == [{
            "item_id": item_id,
            "user_id": "alice",
            "display": "Alice",
            "account_id": "acct-alice",
            "acquired_at": acquired["lock"]["acquired_at"],
            "expires_at": acquired["lock"]["expires_at"],
        }]
    finally:
        v.close()

    offline = tmp_path / "team-offline.git"
    share.rename(offline)
    v = Vault.open(src)
    try:
        status = v.status()
        assert status["project_id"]
        assert status["locks"] == []
    finally:
        v.close()


def test_stale_lock_expires_and_can_be_reacquired(tmp_path, monkeypatch):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+")
    v = Vault.open(src)
    v.save()
    item_id = v.status()["items"][0]["item_id"]
    v.close()
    share = tmp_path / "team.git"
    assert remote.seed_remote(src, share)["ok"]

    repo = remote.Repo(str(share))
    try:
        stale = {
            "item_id": item_id,
            "holder": "alice",
            "display": "Alice",
            "acquired_at": "2000-01-01T00:00:00Z",
            "expires_at": "2000-01-01T01:00:00Z",
        }
        sha = remote._write_lock_blob(
            repo, {"schema": remote._LOCK_SCHEMA, "locks": [stale]}
        )
        repo.refs[remote._LOCK_REF] = sha
    finally:
        repo.close()

    monkeypatch.setenv("CHUDWARE_USER_ID", "bob")
    monkeypatch.setenv("CHUDWARE_USER_DISPLAY", "Bob")
    out = remote.plm_lock(src, item_id)
    assert out["ok"], out
    assert out["lock"]["user_id"] == "bob"
    assert "overrode" not in out


def test_force_lock_override_returns_prior_holder(tmp_path, monkeypatch):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+")
    v = Vault.open(src)
    v.save()
    item_id = v.status()["items"][0]["item_id"]
    v.close()
    share = tmp_path / "team.git"
    assert remote.seed_remote(src, share)["ok"]

    monkeypatch.setenv("CHUDWARE_USER_ID", "alice")
    monkeypatch.setenv("CHUDWARE_USER_DISPLAY", "Alice")
    assert remote.plm_lock(src, item_id)["ok"]

    monkeypatch.setenv("CHUDWARE_USER_ID", "bob")
    monkeypatch.setenv("CHUDWARE_USER_DISPLAY", "Bob")
    refused = remote.plm_lock(src, item_id)
    assert refused["ok"] is False
    assert refused["reason"] == "locked"

    forced = remote.plm_lock(src, item_id, force=True)
    assert forced["ok"], forced
    assert forced["lock"]["user_id"] == "bob"
    assert forced["overrode"]["user_id"] == "alice"
    assert forced["overrode"]["display"] == "Alice"
    assert "account_id" in forced["overrode"]


def test_lock_cas_retry_recovers_from_deterministic_race(
    tmp_path, monkeypatch
):
    src = tmp_path / "seed"
    src.mkdir()
    _write_part(src, "Bracket.step+")
    v = Vault.open(src)
    v.save()
    item_id = v.status()["items"][0]["item_id"]
    v.close()
    share = tmp_path / "team.git"
    assert remote.seed_remote(src, share)["ok"]

    real_cas_push = remote._cas_push
    calls = {"count": 0}

    def race_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return False
        return real_cas_push(*args, **kwargs)

    monkeypatch.setattr(remote, "_cas_push", race_once)
    out = remote.plm_lock(src, item_id)
    assert out["ok"], out
    assert calls["count"] == 2
