"""Tests for T-R7 folder remotes, sync, locks, and author injection.

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
    vb.close()

    # Alice publishes first
    out = remote.sync(a, remote="origin")
    assert out["ok"] is True, out
    assert out["pushed"] is True

    # Bob syncs: diverged → remote (alice) wins Bracket; bob's copy survives
    os.environ["AICAD_USER_ID"] = "bob"
    os.environ["AICAD_USER_DISPLAY"] = "Bob"
    try:
        out = remote.sync(b, remote="origin")
    finally:
        os.environ.pop("AICAD_USER_ID", None)
        os.environ.pop("AICAD_USER_DISPLAY", None)
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

    os.environ["AICAD_USER_ID"] = "alice"
    os.environ["AICAD_USER_DISPLAY"] = "Alice"
    try:
        out = remote.plm_lock(a, item_id, remote="origin")
    finally:
        os.environ.pop("AICAD_USER_ID", None)
        os.environ.pop("AICAD_USER_DISPLAY", None)
    assert out["ok"] is True, out
    assert out["lock"]["holder"] == "alice"
    assert out["lock"]["item_id"] == item_id

    os.environ["AICAD_USER_ID"] = "bob"
    os.environ["AICAD_USER_DISPLAY"] = "Bob"
    try:
        out = remote.plm_lock(b, item_id, remote="origin")
    finally:
        os.environ.pop("AICAD_USER_ID", None)
        os.environ.pop("AICAD_USER_DISPLAY", None)
    assert out["ok"] is False
    assert out["reason"] == "locked"
    assert "Alice" in out.get("hint", "") or "alice" in out.get("hint", "").lower()

    # alice unlocks; bob can take it
    os.environ["AICAD_USER_ID"] = "alice"
    os.environ["AICAD_USER_DISPLAY"] = "Alice"
    try:
        out = remote.plm_unlock(a, item_id, remote="origin")
    finally:
        os.environ.pop("AICAD_USER_ID", None)
        os.environ.pop("AICAD_USER_DISPLAY", None)
    assert out["ok"] is True, out

    os.environ["AICAD_USER_ID"] = "bob"
    os.environ["AICAD_USER_DISPLAY"] = "Bob"
    try:
        out = remote.plm_lock(b, item_id, remote="origin")
    finally:
        os.environ.pop("AICAD_USER_ID", None)
        os.environ.pop("AICAD_USER_DISPLAY", None)
    assert out["ok"] is True, out
    assert out["lock"]["holder"] == "bob"


# ---------------------------------------------------------------------------
# author env injection
# ---------------------------------------------------------------------------

def test_author_env_injection_in_history(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_part(project, "Bracket.step+")

    os.environ["AICAD_USER_ID"] = "sarah@ua.edu"
    os.environ["AICAD_USER_DISPLAY"] = "Sarah"
    try:
        assert _user_author() == {
            "kind": "user",
            "id": "sarah@ua.edu",
            "display": "Sarah",
        }
        v = Vault.open(project)
        try:
            # re-save so a user-authored entry lands (seed uses System)
            _write_part(project, "Bracket.step+", wall=4.0)
            res = v.save(message="sarah edit")
            entries = v.history()
            # newest first
            authored = [e for e in entries if e["author"].get("id") == "sarah@ua.edu"]
            assert authored, entries
            assert authored[0]["author"]["display"] == "Sarah"
            assert authored[0]["author"]["kind"] == "user"
            # document.json must not carry author (byte-stability)
            head = v.status()["head"]
            tree = v._bind.read_tree(head)
            for path, data in tree.items():
                if path.endswith("/document.json"):
                    doc = json.loads(data.decode("utf-8"))
                    assert "author" not in doc
        finally:
            v.close()
    finally:
        os.environ.pop("AICAD_USER_ID", None)
        os.environ.pop("AICAD_USER_DISPLAY", None)


def test_reserved_aicad_cloud_remote(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_part(project, "Bracket.step+")
    v = Vault.open(project)
    v.save()
    v.close()

    out = remote.add_remote(project, "Cloud", kind="aicad-cloud")
    assert out["ok"] is True, out
    assert out["remote"]["kind"] == "aicad-cloud"
    assert out["remote"].get("reserved") is True

    out = remote.sync(project, remote="Cloud")
    assert out["ok"] is False
    assert out["reason"] == "reserved_remote"
