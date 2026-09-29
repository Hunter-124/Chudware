"""Dirty-scan stamp cache for plm overview/status (warm vault speed).

Covers:
  * second overview reuses the process-wide stamp cache (no re-explode)
  * external content edit (new mtime) still surfaces as dirty
  * vault mutation RPCs invalidate the cache so HEAD baselines stay correct
"""
from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from services.plm import vault as vault_mod
from services.plm import worker_rpc
from services.plm.vault import Vault


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(wall: float = 3.0) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "uuid-part-1",
        "name": "Bracket",
        "features": [{"id": "f1", "params": {"wall": wall}}],
    }


def _write(project_dir, name: str, doc: dict) -> None:
    (project_dir / name).write_bytes(_container(doc))


def test_dirty_scan_cache_warm_hit_and_mtime_miss(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    vault_mod._invalidate_dirty_cache(tmp_path)

    v1 = Vault.open(tmp_path)
    try:
        # seed leaves working == HEAD → clean
        dirty0 = v1._dirty_items(v1.manifest())
        assert dirty0 == []
        iid = v1.manifest()["items"][0]["item_id"]
    finally:
        v1.close()

    # Fresh Vault (mirrors worker_rpc _with_vault) must reuse process cache.
    v2 = Vault.open(tmp_path)
    try:
        dirty1 = v2._dirty_items(v2.manifest())
        assert dirty1 == []

        # External edit changes mtime/size → must recompute and report dirty.
        _write(tmp_path, "Bracket.step+", _part(9.0))
        dirty2 = v2._dirty_items(v2.manifest())
        assert dirty2 == [iid]
    finally:
        v2.close()


def test_dirty_scan_cache_invalidated_on_save(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    vault_mod._invalidate_dirty_cache(tmp_path)

    v = Vault.open(tmp_path)
    try:
        # Make working dirty, warm the cache with that verdict.
        _write(tmp_path, "Bracket.step+", _part(5.0))
        dirty = v._dirty_items(v.manifest())
        assert dirty

        # save() must drop the process cache so the new HEAD is the baseline.
        v.save()

        clean = v._dirty_items(v.manifest())
        assert clean == []
    finally:
        v.close()


def test_dirty_scan_cache_invalidated_on_add_untrack(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    (tmp_path / "spec.md").write_text("v1\n")
    vault_mod._invalidate_dirty_cache(tmp_path)

    v = Vault.open(tmp_path)
    try:
        v._dirty_items(v.manifest())  # warm

        item = v.add_item("spec.md")

        v._dirty_items(v.manifest())  # warm again
        v.untrack_item(item["item_id"])
        assert v.status()["dirty"] == []
        assert all(it["item_id"] != item["item_id"] for it in v.status()["items"])
    finally:
        v.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows creation-time ctime uses bounded rechecks")
@pytest.mark.parametrize("kind", ["container", "file", "tree"])
def test_same_size_mtime_preserving_rewrite_is_immediately_dirty(tmp_path, kind):
    """Editors/copy tools preserving mtime must not hide edits until a TTL."""
    if kind == "container":
        path = tmp_path / "Bracket.step+"
        # Stored ZIP keeps equal-size document rewrites equal-size on disk.
        def payload(wall):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr("document.json", json.dumps(_part(wall)))
            return buf.getvalue()
        before, after = payload(3.0), payload(9.0)
    else:
        path = tmp_path / "spec.md"
        if kind == "tree":
            path = tmp_path / "src" / "model.txt"
            path.parent.mkdir()
        before, after = b"old\n", b"new\n"
    path.write_bytes(before)
    v = Vault.open(tmp_path)
    try:
        if kind != "container":
            v.add_item("src" if kind == "tree" else "spec.md",
                       doc_type="software" if kind == "tree" else "spec")
        iid = v.manifest()["items"][0]["item_id"]
        assert v.status()["dirty"] == []
        stamp = path.stat()
        path.write_bytes(after)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert path.stat().st_size == stamp.st_size
        assert v.status()["dirty"] == [iid]
        v.save()
        assert v.status()["dirty"] == []
    finally:
        v.close()


def test_windows_same_stat_rewrites_are_rechecked_without_reusing_explode(tmp_path, monkeypatch):
    """Windows creation-time ctime cannot validate mtime-preserving rewrites."""
    clock = [100.0]
    monkeypatch.setattr(vault_mod, "_STAT_RECHECK_S", 30.0)
    monkeypatch.setattr(vault_mod.time, "monotonic", lambda: clock[0])
    _write(tmp_path, "Bracket.step+", _part(3.0))
    path = tmp_path / "Bracket.step+"
    original = path.read_bytes()
    v = Vault.open(tmp_path)
    try:
        item = v.manifest()["items"][0]
        frozen_stamp = v._working_stamp(item)
        monkeypatch.setattr(v, "_working_stamp", lambda _item: frozen_stamp)
        assert v.status()["dirty"] == []
        _write(tmp_path, "Bracket.step+", _part(9.0))
        clock[0] = 129.0
        assert v.status()["dirty"] == []  # indistinguishable Windows stat within bound
        clock[0] = 131.0
        assert v.status()["dirty"] == [item["item_id"]]
        # The recheck resets its own age, not the age of the whole scan.
        path.write_bytes(original)
        clock[0] = 150.0
        assert v.status()["dirty"] == [item["item_id"]]
        clock[0] = 162.0
        assert v.status()["dirty"] == []
    finally:
        v.close()


@pytest.mark.parametrize("before,after", [(True, 1), (1, 1.0), (-0.0, 0.0)])
def test_dirty_comparison_preserves_json_scalar_distinctions(tmp_path, before, after):
    """Python-equal scalars must not hide a changed canonical recipe."""
    _write(tmp_path, "Bracket.step+", _part(before))
    v = Vault.open(tmp_path)
    try:
        iid = v.manifest()["items"][0]["item_id"]
        assert v.status()["dirty"] == []
        _write(tmp_path, "Bracket.step+", _part(after))
        assert v.status()["dirty"] == [iid]
        v.save()
        assert v.status()["dirty"] == []
    finally:
        v.close()


def test_reformatted_recipe_reuses_canonical_content_without_losing_extras(tmp_path):
    doc = _part()
    doc["notes"] = 'Bräckét \U0001d11e: {}, [], "quoted", \\path\n'
    _write(tmp_path, "Bracket.step+", doc)
    v = Vault.open(tmp_path)
    try:
        iid = v.manifest()["items"][0]["item_id"]
        prefix = f"items/{iid}/"
        original = v._bind.read_blob_at(v._bind.head_commit(), prefix + "document.json")
        # Different key order, whitespace and Unicode spelling; identical recipe.
        with zipfile.ZipFile(tmp_path / "Bracket.step+", "w") as zf:
            zf.writestr("document.json", json.dumps(
                dict(reversed(list(doc.items()))), ensure_ascii=False, separators=(",", ":")))
            zf.writestr("source.dat", b"preserved source attachment")
        assert v.status()["dirty"] == []
        saved = v.save()["commit"]
        assert v._bind.read_blob_at(saved, prefix + "document.json") == original
        assert v._bind.read_blob_at(
            saved, prefix + "extra/source.dat") == b"preserved source attachment"
    finally:
        v.close()


def test_item_list_detects_preserved_member_change_without_changing_legacy_dirty(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        project = {"path": str(tmp_path)}
        path = tmp_path / "Bracket.step+"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
            zf.writestr("document.json", json.dumps(_part()))
            zf.writestr("attachment.txt", b"new drawing note")
        # The overview's existing recipe-only dirty semantics remain unchanged.
        assert v.status()["dirty"] == []
        result = worker_rpc._plm_item_list(project, None)
        assert result["ok"], result
        assert result["items"][0]["change"] == "M"
        assert result["items"][0]["dirty"] is False
        # A second list uses the same HEAD-bound stamp cache.
        assert worker_rpc._plm_item_list(project, None)["items"][0]["change"] == "M"
        v.checkpoint("Add drawing note")
        assert worker_rpc._plm_item_list(project, None)["items"][0]["change"] == ""
    finally:
        v.close()


def test_item_list_changes_and_discovery_do_not_save_or_register(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    project = {"path": str(tmp_path)}
    vault_mod._invalidate_dirty_cache(tmp_path)
    v = Vault.open(tmp_path)
    try:
        iid = v.manifest()["items"][0]["item_id"]
        original_head = v._bind.head_commit()

        def listing():
            result = worker_rpc._plm_item_list(project, None)
            assert result["ok"], result
            assert v._bind.head_commit() == original_head
            return result

        initial = listing()
        assert [(row["item_id"], row["change"], row["dirty"])
                for row in initial["items"]] == [(iid, "", False)]
        assert initial["untracked"] == []

        _write(tmp_path, "Bracket.step+", _part(9.0))
        (tmp_path / "nested").mkdir()
        _write(tmp_path / "nested", "Board.ecad+",
               {"schema": "aicad.pcb/1", "id": "new-board", "name": "Board"})
        (tmp_path / "broken.step+").write_bytes(b"not a container")
        changed = listing()
        assert [(row["item_id"], row["change"], row["dirty"])
                for row in changed["items"]] == [(iid, "M", True)]
        assert changed["untracked"] == [{"path": "nested/Board.ecad+", "change": "U"}]
        assert len(v.manifest()["items"]) == 1
        assert worker_rpc._plm_item_list({**project, "kind": "ecad"}, None)["untracked"] == (
            changed["untracked"])
        assert worker_rpc._plm_item_list({**project, "kind": "part"}, None)["untracked"] == []

        saved = v.checkpoint("Update bracket and board")["commit"]
        checked = worker_rpc._plm_item_list(project, None)
        assert {row["path"]: (row["change"], row["dirty"]) for row in checked["items"]} == {
            "Bracket.step+": ("", False), "nested/Board.ecad+": ("", False)}
        assert checked["untracked"] == []

        (tmp_path / "Bracket.step+").unlink()
        deleted = worker_rpc._plm_item_list(project, None)
        assert next(row for row in deleted["items"] if row["item_id"] == iid)["change"] == "D"
        assert next(row for row in deleted["items"] if row["item_id"] == iid)["dirty"] is False
        assert v._bind.head_commit() == saved

        v.checkpoint("Remove bracket")
        after_delete = worker_rpc._plm_item_list(project, None)
        assert next(row for row in after_delete["items"] if row["item_id"] == iid)["change"] == ""
        assert not (tmp_path / "Bracket.step+").exists()
        _write(tmp_path, "Bracket.step+", _part())
        added = worker_rpc._plm_item_list(project, None)
        assert next(row for row in added["items"] if row["item_id"] == iid)["change"] == "A"
        v.restore(saved)
        restored = worker_rpc._plm_item_list(project, None)
        assert next(row for row in restored["items"] if row["item_id"] == iid)["change"] == ""
    finally:
        v.close()


def test_item_list_unreadable_working_path_is_not_deleted(tmp_path, monkeypatch):
    _write(tmp_path, "Bracket.step+", _part())
    project = {"path": str(tmp_path)}
    v = Vault.open(tmp_path)
    try:
        path = tmp_path / "Bracket.step+"
        original_stat = type(path).stat

        def refuse_stat(self, *args, **kwargs):
            if self == path:
                raise PermissionError("file temporarily inaccessible")
            return original_stat(self, *args, **kwargs)

        monkeypatch.setattr(type(path), "stat", refuse_stat)
        result = worker_rpc._plm_item_list(project, None)
        assert result["ok"], result
        row = result["items"][0]
        assert row["change"] != "D"
        assert row["dirty"] is True
    finally:
        v.close()


def test_generic_file_deletion_remains_visible_when_checkpoint_carries_content(tmp_path):
    (tmp_path / "spec.md").write_text("revision one\n")
    v = Vault.open(tmp_path)
    try:
        item = v.add_item("spec.md")
        (tmp_path / "spec.md").unlink()
        project = {"path": str(tmp_path)}
        before = worker_rpc._plm_item_list(project, None)["items"]
        assert next(row for row in before if row["item_id"] == item["item_id"])["change"] == "D"
        v.checkpoint("Other project changes")
        after = worker_rpc._plm_item_list(project, None)["items"]
        assert next(row for row in after if row["item_id"] == item["item_id"])["change"] == "D"
    finally:
        v.close()
