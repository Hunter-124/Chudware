"""Tests for the per-item timeline contract behind the PLM Compare page.

``Vault.history(item_id=...)`` must answer "the versions of THIS file": a commit
belongs in an item's timeline iff the item EXISTS at that commit AND its content
differs from the content at that commit's parent (the next-older commit in the
scope-filtered chain). The oldest such entry is the item's creation.

That guarantee is what makes every adjacent pair ``(entries[i+1], entries[i])`` a
valid compare pair; without it the Compare page credited changes to unrelated
later commits and offered pre-creation entries that fail to diff.

Runs under the gate venv (dulwich installed). No engine C++ dependency.
"""
from __future__ import annotations

import io
import json
import zipfile

from services.plm import history as hist
from services.plm import snapshot
from services.plm.vault import Vault

# ---------------------------------------------------------------------------
# fixtures / helpers (same conventions as test_plm_vault.py)
# ---------------------------------------------------------------------------


def _container(doc: dict, *, extras: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
        for name, data in (extras or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _part(name: str = "Bracket", wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": f"uuid-{name.lower()}",
            "name": name,
            "features": [{"id": "f1", "params": {"wall": wall}}]}


def _write(project_dir, name, doc, extras=None):
    (project_dir / name).write_bytes(_container(doc, extras=extras))


def _item_id(v: Vault, path: str) -> str:
    for it in v.manifest()["items"]:
        if it["path"] == path:
            return it["item_id"]
    raise AssertionError(f"no item registered at {path}")


def _commits(entries) -> list[str]:
    return [e["commit"] for e in entries]


def _scope_of(v: Vault, commit: str) -> str:
    return hist.scope_from_commit_message(v._bind.commit_meta(commit)["message"])


def _repro_project(tmp_path):
    """The 4-commit reproduction: add A, edit A, add B, edit B.

    Returns ``(vault, {label: commit})``.
    """
    v = Vault.open(tmp_path)  # empty project — seed entry only
    marks: dict[str, str] = {}
    _write(tmp_path, "A.step+", _part("Alpha", 3.0))
    marks["add A"] = v.checkpoint("add A")["commit"]
    _write(tmp_path, "A.step+", _part("Alpha", 4.0))
    marks["edit A"] = v.checkpoint("edit A")["commit"]
    _write(tmp_path, "B.step+", _part("Beta", 5.0))
    marks["add B"] = v.checkpoint("add B")["commit"]
    _write(tmp_path, "B.step+", _part("Beta", 6.0))
    marks["edit B"] = v.checkpoint("edit B")["commit"]
    return v, marks


# ---------------------------------------------------------------------------
# 1-2: attribution + no pre-creation entries (the reported bug)
# ---------------------------------------------------------------------------


def test_timeline_credits_the_commit_that_changed_the_item(tmp_path):
    v, marks = _repro_project(tmp_path)
    try:
        a = _item_id(v, "A.step+")
        got = _commits(v.history(item_id=a))
        assert got == [marks["edit A"], marks["add A"]], (
            "A's timeline must be exactly the commits that changed A, newest "
            f"first; got {got} vs marks {marks}")
        assert marks["edit B"] not in got, \
            "a commit that never touched A must not appear in A's timeline"
    finally:
        v.close()


def test_timeline_has_no_pre_creation_entries(tmp_path):
    v, marks = _repro_project(tmp_path)
    try:
        b = _item_id(v, "B.step+")
        got = _commits(v.history(item_id=b))
        assert got == [marks["edit B"], marks["add B"]], (
            f"B's timeline must start at B's creation; got {got} vs {marks}")
        assert marks["add A"] not in got and marks["edit A"] not in got, \
            "commits from before B existed must not appear in B's timeline"
    finally:
        v.close()


# ---------------------------------------------------------------------------
# 3: creation is the oldest entry, with a real summary
# ---------------------------------------------------------------------------


def test_oldest_entry_is_the_items_creation(tmp_path):
    v, marks = _repro_project(tmp_path)
    try:
        for path, created in (("A.step+", "add A"), ("B.step+", "add B")):
            entries = v.history(item_id=_item_id(v, path))
            assert entries[-1]["commit"] == marks[created], path
            assert entries[-1]["summary"].strip(), \
                f"creation entry for {path} must carry a real first-version summary"
    finally:
        v.close()


# ---------------------------------------------------------------------------
# 4: every adjacent pair is a valid compare pair
# ---------------------------------------------------------------------------


def test_adjacent_entries_are_always_comparable(tmp_path):
    v, _marks = _repro_project(tmp_path)
    try:
        for path in ("A.step+", "B.step+"):
            iid = _item_id(v, path)
            doc = snapshot.item_prefix(iid) + "document.json"
            entries = v.history(item_id=iid)
            assert len(entries) >= 2, path
            for i in range(len(entries) - 1):
                older = v._bind.read_tree(entries[i + 1]["commit"])
                newer = v._bind.read_tree(entries[i]["commit"])
                assert doc in older and doc in newer, \
                    f"{path} must exist at both ends of every compare pair"
                assert older[doc] != newer[doc], \
                    f"adjacent entries for {path} must actually differ"
    finally:
        v.close()


# ---------------------------------------------------------------------------
# 5: a commit touching one item stays out of the others' timelines
# ---------------------------------------------------------------------------


def test_single_item_commit_absent_from_other_timelines(tmp_path):
    v = Vault.open(tmp_path)
    try:
        marks: dict[str, str] = {}
        for name in ("A", "B", "C"):
            _write(tmp_path, f"{name}.step+", _part(name, 3.0))
            marks[f"add {name}"] = v.checkpoint(f"add {name}")["commit"]
        for name in ("A", "B", "C"):
            _write(tmp_path, f"{name}.step+", _part(name, 9.0))
            marks[f"edit {name}"] = v.checkpoint(f"edit {name}")["commit"]
        for name in ("A", "B", "C"):
            got = _commits(v.history(item_id=_item_id(v, f"{name}.step+")))
            assert got == [marks[f"edit {name}"], marks[f"add {name}"]], name
            for other in set("ABC") - {name}:
                assert marks[f"edit {other}"] not in got, (name, other)
    finally:
        v.close()


# ---------------------------------------------------------------------------
# 6: generic (non-container) items — the absence-vs-empty signature case
# ---------------------------------------------------------------------------


def test_generic_item_timeline(tmp_path):
    _write(tmp_path, "A.step+", _part("Alpha", 3.0))
    v = Vault.open(tmp_path)
    try:
        marks: dict[str, str] = {}
        _write(tmp_path, "A.step+", _part("Alpha", 4.0))
        marks["edit A"] = v.checkpoint("edit A first")["commit"]
        (tmp_path / "spec.md").write_text("# Requirements\nv1\n")
        v.add_item("spec.md")  # tracking already snapshots the first version
        marks["add spec"] = v._bind.head_commit()
        marks["no-op after add"] = v.checkpoint("checkpoint after add")["commit"]
        (tmp_path / "spec.md").write_text("# Requirements\nv2 revised\n")
        marks["edit spec"] = v.checkpoint("edit spec")["commit"]
        _write(tmp_path, "A.step+", _part("Alpha", 7.0))
        marks["edit A again"] = v.checkpoint("edit A again")["commit"]

        got = _commits(v.history(item_id=_item_id(v, "spec.md")))
        assert got == [marks["edit spec"], marks["add spec"]], \
            f"generic item timeline must be creation + 1 change; got {got} vs {marks}"
        assert marks["edit A"] not in got and marks["edit A again"] not in got, \
            "commits that only touched a different item must not appear"
        assert marks["no-op after add"] not in got, \
            "a commit that did not change the spec must not appear in its timeline"
    finally:
        v.close()


# ---------------------------------------------------------------------------
# 7-8: project-wide branch untouched; limit still caps
# ---------------------------------------------------------------------------


def test_project_wide_history_is_one_entry_per_scoped_commit(tmp_path):
    v, marks = _repro_project(tmp_path)
    try:
        entries = v.history()
        scoped = [c for c in v._bind.log() if _scope_of(v, c) == hist.SCOPE_DESIGN]
        assert _commits(entries) == scoped
        assert _commits(entries)[:4] == [marks["edit B"], marks["add B"],
                                         marks["edit A"], marks["add A"]]
    finally:
        v.close()


def test_limit_caps_returned_entries(tmp_path):
    v, marks = _repro_project(tmp_path)
    try:
        a = _item_id(v, "A.step+")
        assert len(v.history(item_id=a)) == 2
        assert _commits(v.history(item_id=a, limit=1)) == [marks["edit A"]]
        assert len(v.history(limit=2)) == 2
        assert len(v.history(limit=1)) == 1
    finally:
        v.close()
