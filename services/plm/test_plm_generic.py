"""Tests for the 2026-07 PLM taxonomy + generic-item extension.

Covers:
  * doc_type detection (files + folders), taxonomy → frozen kind mapping
  * generic file/tree items: add, snapshot, dirty detection, byte-exact restore
  * THE CARRY-FORWARD LAW: a generic item survives commits that touch only a
    different (container) item — the whole-tree-commit drop-bug regression guard
  * generic release (rev/lifecycle stamped, no geometry.step frozen)
  * untrack keeps history; link_repo reference; generic compare (text diff)
  * worker RPC surface (plm_item_list/add/untrack/link_repo) with path handling
  * ease-of-use: no git vocabulary in any generic summary

Runs under the gate venv (dulwich installed). No engine C++ dependency.
"""
from __future__ import annotations

import io
import json
import zipfile

from services.plm import doctypes, manifest, snapshot
from services.plm.vault import Vault, VaultError
from services.plm import worker_rpc

_GIT_VOCAB = {
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
}


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": "uuid-part-1", "name": "Bracket",
            "features": [{"id": "f1", "params": {"wall": wall}}]}


def _no_git_vocab(text: str) -> None:
    words = set(text.lower().replace(".", " ").replace(",", " ").split())
    leaked = words & _GIT_VOCAB
    assert not leaked, f"git vocabulary leaked: {leaked} in {text!r}"


# ---------------------------------------------------------------------------
# doc_type detection + taxonomy
# ---------------------------------------------------------------------------

def test_detect_doc_type_files(tmp_path):
    (tmp_path / "spec.md").write_text("# Requirements\n")
    (tmp_path / "datasheet.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "bom.xlsx").write_bytes(b"PK\x03\x04")
    (tmp_path / "render.png").write_bytes(b"\x89PNG")
    (tmp_path / "notes.rando").write_text("x")
    assert doctypes.detect_doc_type(str(tmp_path / "spec.md")) == "spec"
    assert doctypes.detect_doc_type(str(tmp_path / "datasheet.pdf")) == "report"
    assert doctypes.detect_doc_type(str(tmp_path / "bom.xlsx")) == "spreadsheet"
    assert doctypes.detect_doc_type(str(tmp_path / "render.png")) == "image"
    assert doctypes.detect_doc_type(str(tmp_path / "notes.rando")) == "document"


def test_detect_doc_type_folders(tmp_path):
    code = tmp_path / "firmware"
    (code / "src").mkdir(parents=True)
    (code / "CMakeLists.txt").write_text("project(fw)")
    assert doctypes.detect_doc_type(str(code)) == "software"
    plain = tmp_path / "misc"
    plain.mkdir()
    (plain / "a.txt").write_text("x")
    assert doctypes.detect_doc_type(str(plain)) == "document"


def test_taxonomy_maps_to_frozen_kinds():
    for dt, desc in doctypes.DOC_TYPES.items():
        assert desc["kind"] in manifest.ITEM_KINDS, dt
    # the frozen enum is untouched
    assert manifest.ITEM_KINDS == {
        "part", "assembly", "drawing", "ecad", "cam", "requirements", "deliverable"}


# ---------------------------------------------------------------------------
# generic file items
# ---------------------------------------------------------------------------

def test_add_file_item_and_dirty(tmp_path):
    (tmp_path / "spec.md").write_text("v1\n")
    v = Vault.open(tmp_path)
    item = v.add_item("spec.md")
    assert item["doc_type"] == "spec"
    assert item["kind"] == "requirements"
    assert item["part_number"]  # auto-assigned
    # not dirty right after add (it was committed)
    assert item["item_id"] not in v._dirty_items(v.manifest())
    # edit → dirty
    (tmp_path / "spec.md").write_text("v2 changed\n")
    assert item["item_id"] in v._dirty_items(v.manifest())
    v.close()


def test_add_item_already_tracked_is_idempotent(tmp_path):
    (tmp_path / "spec.md").write_text("v1\n")
    v = Vault.open(tmp_path)
    first = v.add_item("spec.md")
    second = v.add_item("spec.md")

    assert second["item_id"] == first["item_id"]
    assert second["already_tracked"] is True
    assert len(v.manifest()["items"]) == 1
    v.close()


def test_file_item_byte_exact_restore(tmp_path):
    payload = b"\x00\x01binary-\xff-content"
    (tmp_path / "data.bin").write_bytes(payload)
    v = Vault.open(tmp_path)
    item = v.add_item("data.bin", doc_type="dataset")
    first = v.history(item["item_id"])[0]["commit"]
    # change the file, checkpoint
    (tmp_path / "data.bin").write_bytes(b"different")
    v.checkpoint("changed data")
    # restore the first version → byte-exact
    v.restore(first, item["item_id"])
    assert (tmp_path / "data.bin").read_bytes() == payload
    v.close()


def test_carry_forward_law_generic_survives_other_item_commit(tmp_path):
    """A generic item must not be dropped by a whole-tree commit that only
    touches a different (container) item. Regression guard for the commit-tree
    drop-bug the advisor flagged."""
    (tmp_path / "spec.md").write_text("the spec\n")
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(3.0)))
    v = Vault.open(tmp_path)
    spec = v.add_item("spec.md")
    point = v.history(spec["item_id"])[0]["commit"]
    # now edit ONLY the container part and checkpoint (spec untouched on disk)
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(5.0)))
    v.checkpoint("wall 3 to 5")
    # the spec must still be present at HEAD (carried forward), byte-exact
    head = v._bind.head_commit()
    tree = v._bind.read_tree(head)
    spec_blob = tree.get(snapshot.item_prefix(spec["item_id"]) + "file/spec.md")
    assert spec_blob == b"the spec\n"
    # and restoring the earlier point still recovers the spec content
    v.restore(point, spec["item_id"])
    assert (tmp_path / "spec.md").read_bytes() == b"the spec\n"
    v.close()


def test_tree_item_snapshot_and_exclusions(tmp_path):
    code = tmp_path / "fw"
    (code / "src").mkdir(parents=True)
    (code / "src" / "main.c").write_text("int main(){}")
    (code / "CMakeLists.txt").write_text("project(fw)")
    (code / "node_modules").mkdir()
    (code / "node_modules" / "junk.js").write_text("nope")
    (code / "build").mkdir()
    (code / "build" / "out.o").write_text("obj")
    v = Vault.open(tmp_path)
    item = v.add_item("fw", doc_type="software")
    head = v._bind.head_commit()
    tree = v._bind.read_tree(head)
    p = snapshot.item_prefix(item["item_id"]) + "tree/"
    assert p + "src/main.c" in tree
    assert p + "CMakeLists.txt" in tree
    assert not any("node_modules" in k for k in tree)
    assert not any(k.startswith(p + "build/") for k in tree)
    # index present
    assert snapshot.item_prefix(item["item_id"]) + "tree.index.json" in tree
    v.close()


# ---------------------------------------------------------------------------
# generic release / untrack / link / compare
# ---------------------------------------------------------------------------

def test_generic_release_stamps_rev_no_geometry(tmp_path):
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 body")
    v = Vault.open(tmp_path)
    item = v.add_item("report.pdf")
    res = v.release(item["item_id"], "First issue of the report")
    assert res["rev"] == "A"
    head = v._bind.head_commit()
    tree = v._bind.read_tree(head)
    # NO geometry.step for a generic item
    assert not any(k.endswith("geometry.step") for k in tree)
    m = v.manifest()
    it = manifest.find_item(m, item["item_id"])
    assert it["lifecycle"] == "Released"
    _no_git_vocab(res["entry"]["summary"])
    v.close()


def test_untrack_keeps_history(tmp_path):
    (tmp_path / "spec.md").write_text("keep me\n")
    v = Vault.open(tmp_path)
    item = v.add_item("spec.md")
    point = v.history(item["item_id"])[0]["commit"]
    v.untrack_item(item["item_id"])
    assert manifest.find_item(v.manifest(), item["item_id"]) is None
    # the content still lives at the earlier point
    tree = v._bind.read_tree(point)
    assert tree.get(snapshot.item_prefix(item["item_id"]) + "file/spec.md") == b"keep me\n"
    v.close()


def test_link_repo_reference_only(tmp_path):
    v = Vault.open(tmp_path)
    item = v.link_repo("git@example.com:fw.git", "Firmware")
    assert item["doc_type"] == "linked_repo"
    head = v._bind.head_commit()
    tree = v._bind.read_tree(head)
    link_raw = tree.get(snapshot.item_prefix(item["item_id"]) + "link.json")
    assert link_raw is not None
    link = json.loads(link_raw)
    assert link["url"] == "git@example.com:fw.git"
    assert link["schema"] == "aicad.plm.link/1"
    v.close()


def test_generic_compare_text_diff(tmp_path):
    (tmp_path / "spec.md").write_text("line one\nline two\n")
    v = Vault.open(tmp_path)
    item = v.add_item("spec.md")
    a = v.history(item["item_id"])[0]["commit"]
    (tmp_path / "spec.md").write_text("line one\nline two changed\nline three\n")
    v.checkpoint("edited spec")
    b = v.history(item["item_id"])[0]["commit"]
    from services.plm import compare as compare_mod
    assert compare_mod.is_generic_item(v, item["item_id"])
    out = compare_mod.generic_compare(v, a, b, item["item_id"])
    assert out["mode"] == "generic"
    assert "line three" in out["text_diff"]
    _no_git_vocab(out["summary"])
    v.close()


# ---------------------------------------------------------------------------
# worker RPC surface
# ---------------------------------------------------------------------------

def test_worker_item_list_and_add(tmp_path):
    (tmp_path / "spec.md").write_text("hello\n")
    # add via worker
    r = worker_rpc._plm_item_add({"path": str(tmp_path), "file_path": "spec.md"}, None)
    assert r["ok"], r
    assert r["item"]["doc_type"] == "spec"
    # list via worker
    r2 = worker_rpc._plm_item_list({"path": str(tmp_path)}, None)
    assert r2["ok"]
    assert len(r2["items"]) == 1
    assert r2["items"][0]["dirty"] is False
    # missing path → contract error, not a traceback
    r3 = worker_rpc._plm_item_list({}, None)
    assert r3["ok"] is False
    assert r3["reason"] == "invalid_project"


def test_worker_untrack_and_link(tmp_path):
    (tmp_path / "a.csv").write_text("x,y\n1,2\n")
    add = worker_rpc._plm_item_add({"path": str(tmp_path), "file_path": "a.csv"}, None)
    iid = add["item"]["item_id"]
    un = worker_rpc._plm_item_untrack({"path": str(tmp_path), "item_id": iid}, None)
    assert un["ok"]
    link = worker_rpc._plm_link_repo(
        {"path": str(tmp_path), "url": "https://example.com/x.git", "name": "X"}, None)
    assert link["ok"]
    assert link["item"]["doc_type"] == "linked_repo"


def test_add_container_via_item_add_rejected_before_other_checks(tmp_path):
    target = tmp_path / "Bracket.step+"
    target.write_bytes(_container(_part()))

    try:
        Vault.open(target)
    except VaultError as e:
        assert e.reason == "invalid_mode"
        assert "plm_checkpoint" in e.hint
    else:
        raise AssertionError("expected a container project path to be rejected")

    v = Vault.open(tmp_path)
    for supplied_type in (None, "part", "report"):
        try:
            v.add_item("Bracket.step+", doc_type=supplied_type)
        except VaultError as e:
            assert e.reason == "invalid_mode"
            assert "plm_checkpoint" in e.hint
            assert "do not register them by hand" in e.hint
        else:
            raise AssertionError(
                f"expected a container add with doc_type={supplied_type!r} to be rejected"
            )
    v.close()
