"""Tests for the PLM vault engine (contract aicad.plm/1).

Covers:
  * zero-config init (open == create, silent, no visible files beyond .aicadvault/)
  * auto-history on save; checkpoint(message); release() per contract
  * release freezes geometry.step; restore rebuilds working container + APPENDS
  * round-trip: save → checkpoint → edit → restore → doc_diff == empty
    (part, assembly, drawing, and .ecad+ containers)
  * plm_* MCP tools registered + exercised end-to-end
  * vault stays lightweight (canonical-JSON delta is O(KB), not O(geometry))
  * corrupted-vault graceful degradation (app still opens docs)
  * §7.1 in-process PLMVaultClient seam (the plm_bridge substrate)
  * ease-of-use: no git vocabulary in any user-facing string

Runs under the gate venv (dulwich installed). No engine C++ dependency.
"""
from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from services.plm import gitbind, manifest, snapshot
from services.plm import vault as vault_mod
from services.plm.client import PLMVaultClient
from services.plm.vault import Vault, VaultError

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_GIT_VOCAB = {
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
}


def _container(doc: dict, *, extras: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
        for name, data in (extras or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _part(wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": "uuid-part-1", "name": "Bracket",
            "features": [{"id": "f1", "params": {"wall": wall}}]}


def _assembly() -> dict:
    return {"schema": "aicad.asm/1", "id": "uuid-asm-1", "name": "Gearbox",
            "components": [{"id": "c1", "ref": "Bracket"}],
            "mates": [{"id": "m1", "kind": "coincident"}]}


def _drawing() -> dict:
    return {"schema": "aicad.drawing/1", "id": "uuid-dwg-1", "name": "Sheet1",
            "views": [{"id": "v1", "kind": "front"}]}


def _ecad() -> dict:
    return {"schema": "aicad.pcb/1", "id": "uuid-pcb-1", "name": "Controller",
            "footprints": [{"id": "u1", "ref": "U1"}]}


def _write(project_dir, name, doc, extras=None):
    (project_dir / name).write_bytes(_container(doc, extras=extras))


def _doc_from_container(raw: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return json.loads(zf.read("document.json"))


def _assert_no_git_vocab(text: str) -> None:
    words = set(text.lower().replace(".", " ").replace(",", " ").split())
    leaked = words & _GIT_VOCAB
    assert not leaked, f"git vocabulary leaked into user-facing text: {leaked} in {text!r}"


# ---------------------------------------------------------------------------
# zero-config init
# ---------------------------------------------------------------------------

def test_zero_config_open_creates_vault(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    assert not Vault.exists(tmp_path)
    v = Vault.open(tmp_path)
    try:
        assert Vault.exists(tmp_path)
        # only .aicadvault appears beyond the working files (contract §8.2)
        names = {p.name for p in tmp_path.iterdir()}
        assert names == {"Bracket.step+", ".aicadvault"}
        st = v.status()
        assert st["head"] is not None
        assert len(st["items"]) == 1
        assert st["items"][0]["lifecycle"] == manifest.LIFECYCLE_IN_WORK
        assert st["items"][0]["part_number"].endswith("-0001")
    finally:
        v.close()


def test_open_is_idempotent(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v1 = Vault.open(tmp_path)
    pid1 = v1.status()["project_id"]
    v1.close()
    v2 = Vault.open(tmp_path)  # re-open, must not re-init
    assert v2.status()["project_id"] == pid1
    v2.close()


def test_open_missing_folder_is_reason_envelope():
    with pytest.raises(VaultError) as e:
        Vault.open("/nonexistent/path/xyz")
    assert e.value.reason == "invalid_project"


# ---------------------------------------------------------------------------
# auto-history + checkpoint + save modes
# ---------------------------------------------------------------------------

def test_save_cuts_auto_entry(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        res = v.save("auto")
        assert res["kind"] == "auto"
        assert res["entry"]["kind"] == "auto"
    finally:
        v.close()


def test_checkpoint_carries_message(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        res = v.checkpoint("Increased wall 3 to 5 mm")
        assert res["entry"]["kind"] == "checkpoint"
        assert res["entry"]["message"] == "Increased wall 3 to 5 mm"
    finally:
        v.close()


def test_save_rejects_release_kind(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        with pytest.raises(VaultError) as e:
            v.save("release")
        assert e.value.reason == "invalid_mode"
    finally:
        v.close()


# ---------------------------------------------------------------------------
# round-trip per doc kind: save → checkpoint → edit → restore → doc_diff empty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,base_doc,edit", [
    ("Bracket.step+", _part(3.0), _part(5.0)),
    ("Gearbox.step+", _assembly(), {**_assembly(), "mates": [{"id": "m1", "kind": "concentric"}]}),
    ("Sheet1.step+", _drawing(), {**_drawing(), "views": [{"id": "v1", "kind": "top"}]}),
    ("Controller.ecad+", _ecad(), {**_ecad(), "footprints": [{"id": "u1", "ref": "U2"}]}),
])
def test_roundtrip_restore_matches_original(tmp_path, name, base_doc, edit):
    from services.mcp.doc_diff_tools import doc_diff

    _write(tmp_path, name, base_doc)
    v = Vault.open(tmp_path)
    try:
        cp = v.checkpoint("original state")
        orig_commit = cp["commit"]

        # edit the working container and save
        _write(tmp_path, name, edit)
        v.save("auto")

        # restore to the original checkpoint
        v.restore(orig_commit)

        # (1) canonical document.json byte-equality — the real proof restore is
        # faithful for ALL kinds (doc_diff only models part/asm, so on drawing/pcb
        # a semantic diff is vacuously empty; byte-equality is the honest check).
        restored_raw = (tmp_path / name).read_bytes()
        restored_canonical = snapshot.explode_container(restored_raw).document_json
        head = v._bind.head_commit()
        # the item's stored blob at the restore entry must equal the working form
        iid = v.status()["items"][0]["item_id"]
        stored = v._bind.read_blob_at(head, snapshot.item_prefix(iid) + "document.json")
        assert restored_canonical == stored, f"{name}: restored blob != working form"

        # (2) semantic doc_diff empty (meaningful for part/asm; harmless elsewhere)
        restored = _doc_from_container(restored_raw)
        diff = doc_diff(base_doc, restored)
        for key in ("features_added", "features_removed", "features_changed",
                    "params_changed", "mate_deltas", "suppression_flips"):
            assert not diff.get(key), f"{name}: non-empty {key}: {diff.get(key)}"
    finally:
        v.close()


def test_restore_appends_never_rewrites(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        cp = v.checkpoint("v1")
        before = v.history()
        n_before = len(before)
        _write(tmp_path, "Bracket.step+", _part(9.0))
        v.save("auto")
        v.restore(cp["commit"])
        after = v.history()
        # restore ADDS entries; the original entries are all still present (same commits)
        assert len(after) > n_before
        before_commits = {e["commit"] for e in before}
        after_commits = {e["commit"] for e in after}
        assert before_commits <= after_commits  # nothing rewritten/removed
        assert after[0]["kind"] == "restore"  # newest entry is the restore
    finally:
        v.close()


# ---------------------------------------------------------------------------
# release: rev stamp + lifecycle + geometry freeze
# ---------------------------------------------------------------------------

def test_release_stamps_rev_and_lifecycle(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        rel = v.release(iid, "First production release")
        assert rel["rev"] == "A"
        item = v.status()["items"][0]
        assert item["rev"] == "A"
        assert item["lifecycle"] == manifest.LIFECYCLE_RELEASED
    finally:
        v.close()


def test_release_requires_note(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        # In assisted/managed tiers an omitted note is drafted
        # deterministically; the "a release needs a note" rule is enforced
        # whenever no draft is available — pin the manual tier to prove it.
        v.set_ai_mode("manual")
        iid = v.status()["items"][0]["item_id"]
        with pytest.raises(VaultError) as e:
            v.release(iid, "")
        assert e.value.reason == "preflight_failed"
    finally:
        v.close()


def test_release_freezes_geometry_step(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        geom = b"ISO-10303-21;\nfake STEP body\nENDSEC;"
        rel = v.release(iid, "rev A", geometry_step=geom)
        tree = v._bind.read_tree(rel["commit"])
        assert snapshot.item_prefix(iid) + "geometry.step" in tree
        assert tree[snapshot.item_prefix(iid) + "geometry.step"] == geom
    finally:
        v.close()


def test_release_pin_is_persisted_not_null(tmp_path):
    # regression: released_revs[].commit must be non-null in the STORED manifest
    # so §4/§5 readers can locate the frozen geometry.step later (frozen store).
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    iid = v.status()["items"][0]["item_id"]
    rel = v.release(iid, "rev A")
    v.close()
    # reopen: read the persisted manifest, not the in-memory one
    v2 = Vault.open(tmp_path)
    try:
        item = manifest.find_item(v2.manifest(), iid)
        pin = item["released_revs"][-1]
        assert pin["rev"] == "A"
        assert pin["commit"] is not None, "release pin persisted as null"
        # the pinned commit must actually carry the frozen geometry-freeze entry
        tree = v2._bind.read_tree(pin["commit"])
        assert snapshot.item_prefix(iid) + "meta.json" in tree
    finally:
        v2.close()


@pytest.mark.parametrize("later_commit", [False, True])
def test_release_interrupted_before_pin_recovers_from_release_entry(
        tmp_path, monkeypatch, later_commit):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        commit_tree = v._bind.commit_tree
        calls = 0

        def fail_second_commit(files, meta):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("interrupted before pin commit")
            return commit_tree(files, meta)

        with monkeypatch.context() as patch:
            patch.setattr(v._bind, "commit_tree", fail_second_commit)
            with pytest.raises(OSError, match="interrupted before pin"):
                v.release(iid, "rev A", bom_pins=[])

        release_head = v._bind.head_commit()
        stored = manifest.loads(v._bind.read_blob_at(release_head, "manifest.json"))
        assert manifest.find_item(stored, iid)["released_revs"][-1]["commit"] is None
        if later_commit:
            v._bind.commit_tree(
                v._bind.read_tree(release_head),
                gitbind.EntryMeta(
                    kind="checkpoint", message="later entry",
                    author_name="Test", author_email="test@example.org",
                ),
            )
        assert manifest.find_item(v.manifest(), iid)["released_revs"][-1]["commit"] == release_head
        assert v.manifest()["bom_freezes"][-1]["commit"] == release_head
        v.start_new_rev(iid)
        item = manifest.find_item(v.manifest(), iid)
        assert item["released_revs"][-1]["commit"] == release_head
        v.discard_working_rev(iid)
        assert manifest.find_item(v.manifest(), iid)["lifecycle"] == manifest.LIFECYCLE_RELEASED
    finally:
        v.close()


def test_local_config_replace_preserves_prior_file_until_new_file_is_ready(tmp_path, monkeypatch):
    v = Vault.open(tmp_path)
    try:
        v.set_local_config({"link_checkouts": {"old": "old-location"}})
        cfg_path = v._bind.vault_dir / "config.json"
        replace = os.replace
        observed = []

        def observe_replace(src, dest):
            observed.append((
                json.loads(cfg_path.read_text()),
                json.loads(src.read_text()),
                dest,
            ))
            replace(src, dest)

        monkeypatch.setattr(vault_mod.os, "replace", observe_replace)
        v.set_local_config({"link_checkouts": {"new": "new-location"}})
        assert len(observed) == 1
        old, new, destination = observed[0]
        assert old["link_checkouts"] == {"old": "old-location"}
        assert new["link_checkouts"] == {
            "old": "old-location", "new": "new-location",
        }
        assert destination == cfg_path
        assert json.loads(cfg_path.read_text()) == new
    finally:
        v.close()


def test_non_release_commits_have_no_geometry(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        cp = v.checkpoint("no geometry here")
        tree = v._bind.read_tree(cp["commit"])
        assert snapshot.item_prefix(iid) + "geometry.step" not in tree
    finally:
        v.close()


def test_second_release_supersedes_prior(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        v.release(iid, "rev A")
        # Lifecycle rule: a Released item is never edited in place —
        # "start a new revision" first, then the edit + next release.
        v.start_new_rev(iid)
        _write(tmp_path, "Bracket.step+", _part(5.0))
        v.save("auto")
        rel_b = v.release(iid, "rev B")
        assert rel_b["rev"] == "B"
        item = manifest.find_item(v.manifest(), iid)
        revs = {rr["rev"]: rr["lifecycle"] for rr in item["released_revs"]}
        assert revs["A"] == manifest.LIFECYCLE_SUPERSEDED
        assert revs["B"] == manifest.LIFECYCLE_RELEASED
    finally:
        v.close()


# ---------------------------------------------------------------------------
# lightweight: canonical-JSON delta is O(KB), not O(geometry)
# ---------------------------------------------------------------------------

def test_checkpoint_is_lightweight(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        v.checkpoint("v1")
        size_before = _vault_object_bytes(tmp_path)
        # a one-param edit + checkpoint
        _write(tmp_path, "Bracket.step+", _part(5.0))
        v.checkpoint("v2")
        size_after = _vault_object_bytes(tmp_path)
        delta = size_after - size_before
        # a single-param change must cost well under 64 KB of object store growth
        assert 0 < delta < 64 * 1024, f"checkpoint delta too large: {delta} bytes"
    finally:
        v.close()


def _vault_object_bytes(project_dir) -> int:
    root = project_dir / ".aicadvault" / "objects"
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            total += os.path.getsize(os.path.join(dirpath, f))
    return total


# ---------------------------------------------------------------------------
# corrupted-vault graceful degradation
# ---------------------------------------------------------------------------

def test_unreadable_container_is_skipped_not_fatal(tmp_path):
    _write(tmp_path, "Good.step+", _part())
    (tmp_path / "Broken.step+").write_bytes(b"not a zip at all")
    v = Vault.open(tmp_path)
    try:
        # the good doc is registered; the broken one is skipped, app still opens
        items = {it["path"] for it in v.status()["items"]}
        assert "Good.step+" in items
        assert "Broken.step+" not in items
    finally:
        v.close()


def test_tracked_corrupt_container_aborts_save_without_moving_head(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        head_before = v._bind.head_commit()
        item_id = v.status()["items"][0]["item_id"]
        document_before = v._bind.read_blob_at(
            head_before, snapshot.item_prefix(item_id) + "document.json")

        (tmp_path / "Bracket.step+").write_bytes(b"not a zip anymore")

        assert item_id in v.status()["dirty"]
        with pytest.raises(VaultError) as exc:
            v.save("auto")
        assert exc.value.reason == "corrupt_item"
        assert v._bind.head_commit() == head_before
        assert v._bind.read_blob_at(
            head_before, snapshot.item_prefix(item_id) + "document.json"
        ) == document_before
    finally:
        v.close()


def test_unreadable_tracked_tree_member_aborts_without_deleting_blob(
        tmp_path, monkeypatch):
    from services.plm import generic_snapshot

    _write(tmp_path, "Bracket.step+", _part())
    source = tmp_path / "source"
    source.mkdir()
    readable = source / "readable.py"
    unreadable = source / "unreadable.py"
    readable.write_text("print('kept')\n", encoding="utf-8")
    unreadable.write_text("print('must not vanish')\n", encoding="utf-8")

    v = Vault.open(tmp_path)
    try:
        item = v.add_item("source", doc_type="software")
        head_before = v._bind.head_commit()
        stored_path = snapshot.item_prefix(item["item_id"]) + "tree/unreadable.py"
        stored_before = v._bind.read_blob_at(head_before, stored_path)
        assert stored_before == b"print('must not vanish')\n"

        original_read_bytes = generic_snapshot.Path.read_bytes

        def fail_one_member(path):
            if path == unreadable:
                raise OSError("synthetic unreadable member")
            return original_read_bytes(path)

        monkeypatch.setattr(generic_snapshot.Path, "read_bytes", fail_one_member)
        with pytest.raises(VaultError) as exc:
            v.save("auto")
        assert exc.value.reason == "snapshot_failed"
        assert v._bind.head_commit() == head_before
        assert v._bind.read_blob_at(head_before, stored_path) == stored_before
    finally:
        v.close()


def test_extra_container_entries_survive_roundtrip(tmp_path):
    # netlist.json (a derived .ecad+ entry) must not be dropped on restore
    _write(tmp_path, "Controller.ecad+", _ecad(), extras={"netlist.json": b'{"nets":[1,2,3]}'})
    v = Vault.open(tmp_path)
    try:
        cp = v.checkpoint("with netlist")
        _write(tmp_path, "Controller.ecad+", _ecad())  # rewrite WITHOUT netlist
        v.save("auto")
        v.restore(cp["commit"])
        with zipfile.ZipFile(io.BytesIO((tmp_path / "Controller.ecad+").read_bytes())) as zf:
            assert "netlist.json" in zf.namelist()
            assert json.loads(zf.read("netlist.json")) == {"nets": [1, 2, 3]}
    finally:
        v.close()


# ---------------------------------------------------------------------------
# §7.1 in-process PLMVaultClient seam (the plm_bridge substrate)
# ---------------------------------------------------------------------------

def test_client_is_available():
    c = PLMVaultClient()
    assert c.is_available() is True


def test_client_project_id_for_run_best_effort():
    c = PLMVaultClient()
    assert c.project_id_for_run("run_unknown") is None  # best-effort → None is valid
    c.register_run("run_1", "p_abc")
    assert c.project_id_for_run("run_1") == "p_abc"


def test_client_checkpoint_writes_ai_attribution(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    pid = v.status()["project_id"]
    # Assist-tier floor: AI-authored entries require the managed tier
    v.set_ai_mode("managed")
    v.close()

    c = PLMVaultClient()
    c.register_project(pid, str(tmp_path))
    res = c.checkpoint(project_id=pid, message="AI adjusted wall thickness",
                       author_kind="ai", run_id="run_42", artifact_id="bg_node_7")
    assert res["ok"] is True
    entry = res["entry"]
    assert entry["kind"] == "checkpoint"
    assert entry["author"]["kind"] == "ai"
    assert entry["ai"] is not None
    assert entry["ai"]["run_id"] == "run_42"
    assert entry["ai"]["artifact_id"] == "bg_node_7"
    assert entry["ai"]["reversible"] is True


def test_client_invalid_project_is_reason_envelope():
    c = PLMVaultClient()
    res = c.checkpoint(project_id="p_nope", message="x")
    assert res["ok"] is False
    assert res["reason"] == "invalid_project"


def test_client_satisfies_bridge_protocol():
    # the client must duck-type the VaultClient Protocol (contract §7.1)
    from services.agent.plm_bridge import VaultClient
    assert isinstance(PLMVaultClient(), VaultClient)


def test_plm_bridge_uses_real_client_when_flag_on(tmp_path, monkeypatch):
    # CHUDWARE_PLM_BRIDGE=1 → the bridge imports our PLMVaultClient, not the mock
    monkeypatch.setenv("CHUDWARE_PLM_BRIDGE", "1")
    from services.agent import plm_bridge
    vault = plm_bridge._default_vault()
    assert type(vault).__name__ == "PLMVaultClient"


# ---------------------------------------------------------------------------
# ease-of-use: no git vocabulary in user-facing strings
# ---------------------------------------------------------------------------

def test_restore_message_has_no_git_vocab(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        cp = v.checkpoint("v1")
        r = v.restore(cp["commit"])
        _assert_no_git_vocab(r["entry"]["message"])
    finally:
        v.close()


def test_error_hints_have_no_git_vocab():
    # sample the reason hints surfaced by the vault
    for reason, hint in [
        ("invalid_project", "not a folder: /x"),
        ("not_found", "that point in history could not be found"),
    ]:
        e = VaultError(reason, hint)
        _assert_no_git_vocab(e.hint)


# ---------------------------------------------------------------------------
# manifest / rev-letter units
# ---------------------------------------------------------------------------

def test_rev_letter_progression():
    assert manifest.next_rev_letter(None) == "A"
    assert manifest.next_rev_letter("A") == "B"
    assert manifest.next_rev_letter("Z") == "AA"


def test_manifest_rejects_future_major():
    bad = json.dumps({"schema": "aicad.plm/2", "items": []}).encode()
    with pytest.raises(ValueError):
        manifest.loads(bad)


# ---------------------------------------------------------------------------
# 2A/2B — derived caches excluded from history; recursive nested tracking
# ---------------------------------------------------------------------------


def test_derived_caches_excluded_from_history(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(),
           extras={"cache.brep": b"SOLVED-BREP",
                   "cache.aicm": b"SOLVED-AICM",
                   "cache.json": b'{"hdr":1}',
                   "netlist.json": b'{"nets":[]}'})
    v = Vault.open(tmp_path)
    try:
        r = v.save()
        tree = v._bind.read_tree(r["commit"])
        assert not any("/extra/cache." in p or p.endswith("/cache.brep")
                       for p in tree), list(tree)
        # a non-derived extra is still preserved
        assert any(p.endswith("/extra/netlist.json") for p in tree), list(tree)
        # restore yields a cache-less container; document.json survives intact
        item_id = v.manifest()["items"][0]["item_id"]
        v.restore(r["commit"], item_id)
        raw = (tmp_path / "Bracket.step+").read_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
        assert not any(n.startswith("cache.") for n in names), names
        assert "netlist.json" in names
        assert _doc_from_container(raw)["id"] == "uuid-part-1"
    finally:
        v.close()


def test_nested_container_tracked_and_restored(tmp_path):
    sub = tmp_path / "sub" / "deep"
    sub.mkdir(parents=True)
    (sub / "Part.step+").write_bytes(_container(_part()))
    v = Vault.open(tmp_path)
    try:
        items = v.manifest()["items"]
        assert [it["path"] for it in items] == ["sub/deep/Part.step+"]
        r = v.save()
        # delete the working file, then restore it to the SAME nested path
        (sub / "Part.step+").unlink()
        v.restore(r["commit"], items[0]["item_id"])
        assert (sub / "Part.step+").is_file()
        assert _doc_from_container((sub / "Part.step+").read_bytes())["id"] \
            == "uuid-part-1"
    finally:
        v.close()


# ---------------------------------------------------------------------------
# per-save scan cost: pruned container walk, explode reuse, bounded history
# ---------------------------------------------------------------------------


def test_container_scan_does_not_walk_the_vault(tmp_path, monkeypatch):
    """The per-save rescan still happens, but never descends into dot-dirs."""
    _write(tmp_path, "Bracket.step+", _part())
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub", "Nested.step+", _assembly())
    # decoys the scan must ignore: a container inside a dot-dir and a dot-named one
    (tmp_path / ".derived").mkdir()
    _write(tmp_path / ".derived", "Ignored.step+", _part())
    _write(tmp_path, ".Hidden.step+", _part())

    v = Vault.open(tmp_path)
    try:
        walked: list[str] = []
        real_walk = os.walk

        def counting_walk(top, *a, **kw):
            for row in real_walk(top, *a, **kw):
                walked.append(os.path.relpath(row[0], tmp_path))
                yield row

        monkeypatch.setattr(vault_mod.os, "walk", counting_walk)
        found = [p.relative_to(tmp_path).as_posix() for p in v._project_containers()]
        assert found == ["Bracket.step+", "sub/Nested.step+"]
        assert walked, "the rescan must still run on every call"
        assert sorted(walked) == [".", "sub"], walked
    finally:
        v.close()


def test_unchanged_container_is_not_re_exploded(tmp_path, monkeypatch):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    _write(tmp_path, "Gearbox.step+", _assembly())
    vault_mod._explode_cache.clear()

    v = Vault.open(tmp_path)  # seeding explodes both containers
    try:
        exploded: list[bytes] = []
        real_explode = snapshot.explode_container

        def counting_explode(raw):
            exploded.append(raw)
            return real_explode(raw)

        monkeypatch.setattr(vault_mod.snapshot, "explode_container", counting_explode)

        # nothing changed on disk -> no container is read or exploded again
        r_clean = v.save()
        assert exploded == []
        seed_tree = v._bind.read_tree(v._bind.log()[1])
        clean_tree = v._bind.read_tree(r_clean["commit"])
        content = {p: b for p, b in seed_tree.items()
                   if not p.endswith(("meta.json", "summary.txt", "manifest.json"))}
        assert {p: b for p, b in clean_tree.items()
                if not p.endswith(("meta.json", "summary.txt", "manifest.json"))} == content

        # touch exactly one container -> exactly one explode
        _write(tmp_path, "Bracket.step+", _part(5.0))
        r_edit = v.save()
        assert len(exploded) == 1, exploded

        # and the committed blobs are byte-identical to a fresh explode of disk
        tree = v._bind.read_tree(r_edit["commit"])
        for it in v.manifest()["items"]:
            prefix = snapshot.item_prefix(it["item_id"])
            fresh = real_explode((tmp_path / it["path"]).read_bytes())
            assert tree[prefix + "document.json"] == fresh.document_json
            assert tree.get(prefix + "thumbnail.png") == fresh.thumbnail_png
        assert _doc_from_container(
            (tmp_path / "Bracket.step+").read_bytes())["features"][0]["params"]["wall"] == 5.0
        assert json.loads(tree[snapshot.item_prefix(
            v.manifest()["items"][0]["item_id"]) + "document.json"]
        )["features"][0]["params"]["wall"] == 5.0
    finally:
        v.close()


def test_dirty_scan_checks_preserved_members_with_unchanged_canonical_recipe(tmp_path):
    path = tmp_path / "Bracket.step+"
    canonical = snapshot.explode_container(_container(_part())).document_json
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("document.json", canonical)
        zf.writestr("source.dat", b"unique_extra")
    v = Vault.open(tmp_path)
    try:
        iid = v.manifest()["items"][0]["item_id"]
        assert v.status()["dirty"] == []
        # Keep the recipe exact but break a preserved member's CRC.
        path.write_bytes(path.read_bytes().replace(b"unique_extra", b"broken_extra"))
        assert v.status()["dirty"] == [iid]
        with pytest.raises(VaultError) as exc:
            v.save()
        assert exc.value.reason == "corrupt_item"
    finally:
        v.close()


def test_history_limit_stops_decoding_the_chain(tmp_path, monkeypatch):
    _write(tmp_path, "Bracket.step+", _part(1.0))
    v = Vault.open(tmp_path)
    try:
        for wall in (2.0, 3.0, 4.0, 5.0, 6.0):
            _write(tmp_path, "Bracket.step+", _part(wall))
            v.save()
        total = len(v._bind.log())
        assert total >= 6

        full = v.history()
        decoded: list[str] = []
        real_meta = gitbind._meta_from_commit

        def counting_meta(commit_hex, c):
            decoded.append(commit_hex)
            return real_meta(commit_hex, c)

        monkeypatch.setattr(gitbind, "_meta_from_commit", counting_meta)

        limited = v.history(limit=2)
        # identical answer, and the older tail of the chain was never decoded
        assert limited == full[:2]
        assert len(decoded) == 2, decoded

        # scope='all' is unaffected in content and ordering
        decoded.clear()
        assert [e["commit"] for e in v.history(scope="all")] == v._bind.log()

        # the per-item walk is bounded the same way
        item_id = v.manifest()["items"][0]["item_id"]
        item_full = v.history(item_id)
        decoded.clear()
        assert v.history(item_id, limit=1) == item_full[:1]
        assert len(decoded) <= 2, decoded
    finally:
        v.close()
