"""Tests for ssh/https remote transports (contract §6).

Folder remotes already have full coverage in ``test_remote.py`` (seed/clone,
linear sync, diverge → copy-as-variant, lock CAS, author injection). This file
proves the *same* fetch / CAS-push / lock code paths now also run for remote
profiles labeled ``ssh`` and ``https`` (contract §6.1's non-folder kinds),
without any real network or ssh daemon: the manifest's ``kind`` field is a
UI label only — dulwich picks the wire protocol purely from the URL string
(``dulwich.client.get_transport_and_path``) — so pointing an ssh/https-labeled
remote at a local bare repo (a plain filesystem path, or a ``file://`` URL)
genuinely exercises the non-folder branch opened up in ``remote.sync()`` /
``remote._lock_mutate()`` while staying entirely local, via dulwich's
``LocalGitClient``.

No engine C++ dependency; dulwich only.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

from services.plm import remote
from services.plm.vault import Vault, VaultError


# ---------------------------------------------------------------------------
# helpers (mirror services/plm/test_remote.py so fixtures stay identical)
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


def _relabel_origin(project_dir: Path, url: str, kind: str) -> None:
    """Re-tag the existing "origin" remote profile as *kind* (ssh/https).

    ``seed_remote``/``clone_from_remote`` always register folder remotes;
    this exercises ``add_remote``'s update-in-place path to swap the kind
    label onto the *same* local URL, so ``sync``/``plm_lock`` read the live
    manifest kind rather than any transport choice cached at clone time.
    """
    out = remote.add_remote(project_dir, "origin", url, kind=kind)
    assert out["ok"], out
    assert out["remote"]["kind"] == kind


# ---------------------------------------------------------------------------
# FF sync through an "ssh"-labeled remote (plain local path URL)
# ---------------------------------------------------------------------------

def test_ssh_labeled_remote_linear_sync(tmp_path):
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

    # relabel both clones' "origin" as ssh, same local path URL — no network,
    # but now routed through the non-folder branch of sync().
    _relabel_origin(a, str(bare), "ssh")
    _relabel_origin(b, str(bare), "ssh")

    # Alice edits + saves + syncs (push through the ssh-labeled remote)
    _write_part(a, "Bracket.step+", wall=5.0)
    va = Vault.open(a)
    va.save(message="alice wall 5")
    va.close()
    out = remote.sync(a, remote="origin")
    assert out["ok"] is True, out
    assert out["pushed"] is True

    # Bob syncs (pull) through the same ssh-labeled remote
    out = remote.sync(b, remote="origin")
    assert out["ok"] is True, out
    assert out["pulled"] is True
    assert _read_wall(b) == 5.0

    # Bob edits further, syncs (push)
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

    va = Vault.open(a)
    vb = Vault.open(b)
    try:
        assert va.status()["head"] == vb.status()["head"]
    finally:
        va.close()
        vb.close()


# ---------------------------------------------------------------------------
# CAS-push conflict → copy-as-new-item through an "https"-labeled remote
# (a file:// URL, exercising dulwich's scheme-dispatch parse path too)
# ---------------------------------------------------------------------------

def test_https_labeled_remote_diverge_copy_as_new_item(tmp_path):
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

    file_url = bare.resolve().as_uri()  # file:///... — real scheme dispatch
    _relabel_origin(a, file_url, "https")
    _relabel_origin(b, file_url, "https")

    # both edit offline from the same base
    _write_part(a, "Bracket.step+", wall=11.0)
    va = Vault.open(a)
    va.save(message="alice 11")
    va.close()

    _write_part(b, "Bracket.step+", wall=22.0)
    vb = Vault.open(b)
    vb.save(message="bob 22")
    vb.close()

    # Alice publishes first — CAS-push against the https-labeled remote
    out = remote.sync(a, remote="origin")
    assert out["ok"] is True, out
    assert out["pushed"] is True

    # Bob syncs: diverged → CAS-push forces a fetch + replay; remote (alice)
    # wins the collision on Bracket, bob's edit survives as a copy-as-new-item
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

    assert _read_wall(b, "Bracket.step+") == 11.0
    variants = list(b.glob("Bracket (*'s edits).step+"))
    assert variants, "expected a copy-as-variant working file"
    assert _read_wall(b, variants[0].name) == 22.0

    vb = Vault.open(b)
    try:
        items = vb.status()["items"]
        ids = {it["item_id"] for it in items}
        assert item_id in ids
        assert len(ids) >= 2
    finally:
        vb.close()


# ---------------------------------------------------------------------------
# advisory lock CAS through an "ssh"-labeled remote
# ---------------------------------------------------------------------------

def test_lock_cas_via_ssh_remote(tmp_path):
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
    _relabel_origin(a, str(bare), "ssh")
    _relabel_origin(b, str(bare), "ssh")

    os.environ["CHUDWARE_USER_ID"] = "alice"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Alice"
    try:
        out = remote.plm_lock(a, item_id, remote="origin")
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert out["ok"] is True, out
    assert out["lock"]["holder"] == "alice"

    # bob contends for the same lock over the same ssh-labeled remote
    os.environ["CHUDWARE_USER_ID"] = "bob"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Bob"
    try:
        out = remote.plm_lock(b, item_id, remote="origin")
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert out["ok"] is False
    assert out["reason"] == "locked"

    # alice releases; bob can now take it (still CAS'd over the ssh remote)
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
# chudware-cloud stays reserved regardless of the folder-only gate removal
# ---------------------------------------------------------------------------

def test_reserved_remote_still_rejected_for_all_ops(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_part(project, "Bracket.step+")
    v = Vault.open(project)
    v.save()
    item_id = v.status()["items"][0]["item_id"]
    v.close()

    assert remote.add_remote(project, "Cloud", kind="chudware-cloud")["ok"]

    out = remote.sync(project, remote="Cloud")
    assert out == {"ok": False, "reason": "reserved_remote", "hint": "Chudware Cloud is not available yet"}

    out = remote.plm_lock(project, item_id, remote="Cloud")
    assert out["ok"] is False
    assert out["reason"] == "reserved_remote"


# ---------------------------------------------------------------------------
# unreachable ssh/https remotes fail typed, never a traceback
# ---------------------------------------------------------------------------

def test_ssh_labeled_remote_unreachable_end_to_end(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_part(project, "Bracket.step+")
    v = Vault.open(project)
    v.save()
    v.close()

    missing = tmp_path / "does" / "not" / "exist.git"
    out = remote.add_remote(project, "origin", str(missing), kind="ssh")
    assert out["ok"], out

    out = remote.sync(project, remote="origin")
    assert out["ok"] is False
    assert out["reason"] == "remote_unreachable"
    assert out.get("hint")
    lowered = out["hint"].lower()
    assert "fatal" not in lowered
    assert "repository" not in lowered
    assert "does/not/exist.git" not in lowered


# ---------------------------------------------------------------------------
# transport-error classification (contract: typed remote_auth vs
# remote_unreachable, never a bare traceback)
# ---------------------------------------------------------------------------

def test_classify_transport_error_https_401_403_is_remote_auth():
    from dulwich.client import HTTPProxyUnauthorized, HTTPUnauthorized

    e1 = HTTPUnauthorized("Basic realm=\"git\"", "https://example.invalid/repo.git")
    err = remote._classify_transport_error(e1, "https://example.invalid/repo.git")
    assert isinstance(err, VaultError)
    assert err.reason == "remote_auth"

    e2 = HTTPProxyUnauthorized(None, "https://example.invalid/repo.git")
    err = remote._classify_transport_error(e2, "https://example.invalid/repo.git")
    assert err.reason == "remote_auth"


def test_classify_transport_error_ssh_permission_denied_is_remote_auth():
    # dulwich's SubprocessSSHVendor surfaces a rejected key as a
    # GitProtocolError/HangupException whose message embeds ssh's own stderr.
    e = Exception(
        "git@example.invalid: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    )
    err = remote._classify_transport_error(e, "ssh://git@example.invalid/repo.git")
    assert err.reason == "remote_auth"
    assert err.hint == (
        "Authentication for the shared project failed. Sign in and try again."
    )
    lowered = err.hint.lower()
    assert "fatal" not in lowered
    assert "repository" not in lowered
    assert "permission denied" not in lowered
    assert str(e) not in err.hint


def test_classify_transport_error_generic_failure_is_remote_unreachable():
    e = ConnectionRefusedError("Connection refused")
    err = remote._classify_transport_error(e, "ssh://example.invalid/repo.git")
    assert err.reason == "remote_unreachable"
    assert err.hint == (
        "The shared project could not be reached. "
        "Check its location and connection, then try again."
    )
    lowered = err.hint.lower()
    assert "fatal" not in lowered
    assert "repository" not in lowered
    assert "connection refused" not in lowered
    assert str(e) not in err.hint
