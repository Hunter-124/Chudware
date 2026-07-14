"""Tests for lightweight PLM branching (wave-2).

Covers:
  * create / list / switch clean round-trip
  * feature-list 3-way merge (clean + conflicting feature edit)
  * ECAD net / component id-based merge
  * binary whole-file conflict
  * branch-scoped AI-VC timeline
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from services.plm import ai_vc, branches, snapshot
from services.plm.vault import Vault, VaultError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _container(doc: dict, *, extras: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
        for name, data in (extras or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _part(features=None, *, name: str = "Bracket", wall: float = 3.0) -> dict:
    if features is None:
        features = [
            {"id": "f1", "op": "box", "params": {"wall": wall}},
            {"id": "f2", "op": "fillet", "params": {"r": 1.0}},
        ]
    return {
        "schema": "aicad.part/1",
        "id": "uuid-part-1",
        "name": name,
        "features": features,
    }


def _ecad(nets=None, footprints=None) -> dict:
    return {
        "schema": "aicad.pcb/1",
        "id": "uuid-pcb-1",
        "name": "Controller",
        "nets": nets if nets is not None else [
            {"id": "n1", "name": "GND"},
            {"id": "n2", "name": "VCC"},
        ],
        "footprints": footprints if footprints is not None else [
            {"id": "u1", "ref": "U1"},
            {"id": "r1", "ref": "R1"},
        ],
    }


def _write(project_dir, name, doc, extras=None):
    (project_dir / name).write_bytes(_container(doc, extras=extras))


def _doc_from_container(raw: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return json.loads(zf.read("document.json"))


def _open_seeded(tmp_path, doc, filename="Bracket.step+"):
    _write(tmp_path, filename, doc)
    v = Vault.open(tmp_path)
    # ensure a real history entry beyond seed
    v.checkpoint("seed")
    return v


# ---------------------------------------------------------------------------
# create / list / switch
# ---------------------------------------------------------------------------

def test_create_list_switch_clean(tmp_path):
    v = _open_seeded(tmp_path, _part())
    try:
        listed = branches.list_branches(v)
        assert listed["ok"]
        assert listed["active"] == "main"
        names = {b["name"] for b in listed["branches"]}
        assert "main" in names

        created = branches.create_branch(v, "feature/fillet")
        assert created["ok"]
        assert created["name"] == "feature/fillet"
        assert created["head"]

        listed2 = branches.list_branches(v)
        names2 = {b["name"] for b in listed2["branches"]}
        assert "feature/fillet" in names2
        assert listed2["active"] == "main"

        sw = branches.switch_branch(v, "feature/fillet")
        assert sw["ok"]
        assert sw["active"] == "feature/fillet"
        assert branches.active_branch(v) == "feature/fillet"

        # switch back
        sw2 = branches.switch_branch(v, "main")
        assert sw2["ok"]
        assert branches.active_branch(v) == "main"
    finally:
        v.close()


def test_create_duplicate_rejected(tmp_path):
    v = _open_seeded(tmp_path, _part())
    try:
        branches.create_branch(v, "alt")
        with pytest.raises(VaultError) as ei:
            branches.create_branch(v, "alt")
        assert ei.value.reason == "already_exists"
    finally:
        v.close()


def test_switch_unknown_rejected(tmp_path):
    v = _open_seeded(tmp_path, _part())
    try:
        with pytest.raises(VaultError) as ei:
            branches.switch_branch(v, "does-not-exist")
        assert ei.value.reason == "not_found"
    finally:
        v.close()


def test_branch_isolates_saves(tmp_path):
    """Edits on a feature branch do not move main's tip."""
    v = _open_seeded(tmp_path, _part(wall=3.0))
    try:
        main_tip = v._bind.head_commit()
        branches.create_branch(v, "work")
        branches.switch_branch(v, "work")

        # edit working file + checkpoint on work
        _write(tmp_path, "Bracket.step+", _part(wall=9.0))
        v.checkpoint("thicker wall")
        work_tip = v._bind.head_commit()
        assert work_tip != main_tip

        # main tip still the old one
        listed = branches.list_branches(v)
        by_name = {b["name"]: b for b in listed["branches"]}
        assert by_name["main"]["head"] == main_tip
        assert by_name["work"]["head"] == work_tip

        # switch to main → working file reverts to original wall
        branches.switch_branch(v, "main")
        doc = _doc_from_container((tmp_path / "Bracket.step+").read_bytes())
        assert doc["features"][0]["params"]["wall"] == 3.0
    finally:
        v.close()


# ---------------------------------------------------------------------------
# feature-list merge
# ---------------------------------------------------------------------------

def test_merge_clean_feature_add(tmp_path):
    """Src adds a feature; dst unchanged → clean merge includes the add."""
    v = _open_seeded(tmp_path, _part())
    try:
        branches.create_branch(v, "add-hole")
        branches.switch_branch(v, "add-hole")
        feats = [
            {"id": "f1", "op": "box", "params": {"wall": 3.0}},
            {"id": "f2", "op": "fillet", "params": {"r": 1.0}},
            {"id": "f3", "op": "hole", "params": {"d": 4.0}},
        ]
        _write(tmp_path, "Bracket.step+", _part(features=feats))
        v.checkpoint("add hole")

        branches.switch_branch(v, "main")
        result = branches.merge_branches(v, "add-hole", "main")
        assert result["ok"]
        assert result["conflict_count"] == 0
        assert result["conflicts"] == []

        # materialize main and inspect
        branches.switch_branch(v, "main")
        doc = _doc_from_container((tmp_path / "Bracket.step+").read_bytes())
        ids = [f["id"] for f in doc["features"]]
        assert ids == ["f1", "f2", "f3"]
    finally:
        v.close()


def test_merge_conflicting_feature_edit(tmp_path):
    """Same feature id modified both sides → structured conflict."""
    v = _open_seeded(tmp_path, _part(wall=3.0))
    try:
        branches.create_branch(v, "side-a")
        branches.create_branch(v, "side-b")

        # side-a: thicken f1
        branches.switch_branch(v, "side-a")
        feats_a = [
            {"id": "f1", "op": "box", "params": {"wall": 5.0}},
            {"id": "f2", "op": "fillet", "params": {"r": 1.0}},
        ]
        _write(tmp_path, "Bracket.step+", _part(features=feats_a))
        v.checkpoint("side-a wall=5")

        # side-b: different wall on f1
        branches.switch_branch(v, "side-b")
        feats_b = [
            {"id": "f1", "op": "box", "params": {"wall": 8.0}},
            {"id": "f2", "op": "fillet", "params": {"r": 1.0}},
        ]
        _write(tmp_path, "Bracket.step+", _part(features=feats_b))
        v.checkpoint("side-b wall=8")

        # merge side-a into side-b
        result = branches.merge_branches(v, "side-a", "side-b")
        assert result["ok"]
        assert result["conflict_count"] >= 1
        kinds = {c.get("kind") for c in result["conflicts"]}
        ids = {c.get("id") for c in result["conflicts"]}
        assert "f1" in ids
        assert any(k in ("feature", "features") or k == "feature" for k in kinds) or "f1" in ids
        # structured, not silent
        for c in result["conflicts"]:
            assert "reason" in c
            assert c["reason"] in (
                "both_modified", "deleted_src_modified_dst", "deleted_dst_modified_src",
            )
            assert "src" in c or "src_hash" in c
            assert "dst" in c or "dst_hash" in c
    finally:
        v.close()


def test_merge_id_list_unit():
    base = [{"id": "a", "v": 1}, {"id": "b", "v": 1}]
    src = [{"id": "a", "v": 2}, {"id": "b", "v": 1}, {"id": "c", "v": 1}]
    dst = [{"id": "a", "v": 1}, {"id": "b", "v": 3}]
    merged, conflicts = branches.merge_id_list(base, src, dst, path="features", kind="feature")
    by_id = {_["id"]: _ for _ in merged}
    assert by_id["a"]["v"] == 2  # only src modified
    assert by_id["b"]["v"] == 3  # only dst modified
    assert by_id["c"]["v"] == 1  # src-only add
    assert conflicts == []

    # both modify a
    src2 = [{"id": "a", "v": 9}]
    dst2 = [{"id": "a", "v": 7}]
    base2 = [{"id": "a", "v": 1}]
    _, c2 = branches.merge_id_list(base2, src2, dst2, path="features", kind="feature")
    assert len(c2) == 1
    assert c2[0]["id"] == "a"
    assert c2[0]["reason"] == "both_modified"


# ---------------------------------------------------------------------------
# ECAD net merge
# ---------------------------------------------------------------------------

def test_ecad_net_merge(tmp_path):
    v = _open_seeded(tmp_path, _ecad(), filename="Board.ecad+")
    try:
        branches.create_branch(v, "nets")
        branches.switch_branch(v, "nets")
        nets = [
            {"id": "n1", "name": "GND"},
            {"id": "n2", "name": "VCC"},
            {"id": "n3", "name": "SPI_CLK"},
        ]
        _write(tmp_path, "Board.ecad+", _ecad(nets=nets))
        v.checkpoint("add SPI_CLK")

        branches.switch_branch(v, "main")
        # main renames a footprint only
        fps = [
            {"id": "u1", "ref": "U1"},
            {"id": "r1", "ref": "R1A"},
        ]
        _write(tmp_path, "Board.ecad+", _ecad(footprints=fps))
        v.checkpoint("rename R1")

        result = branches.merge_branches(v, "nets", "main")
        assert result["ok"]
        assert result["conflict_count"] == 0

        branches.switch_branch(v, "main")
        doc = _doc_from_container((tmp_path / "Board.ecad+").read_bytes())
        net_ids = [n["id"] for n in doc["nets"]]
        assert "n3" in net_ids
        fp_by = {f["id"]: f for f in doc["footprints"]}
        assert fp_by["r1"]["ref"] == "R1A"
    finally:
        v.close()


def test_ecad_net_conflict(tmp_path):
    v = _open_seeded(tmp_path, _ecad(), filename="Board.ecad+")
    try:
        branches.create_branch(v, "a")
        branches.create_branch(v, "b")

        branches.switch_branch(v, "a")
        _write(tmp_path, "Board.ecad+", _ecad(nets=[
            {"id": "n1", "name": "GND_A"},
            {"id": "n2", "name": "VCC"},
        ]))
        v.checkpoint("a renames GND")

        branches.switch_branch(v, "b")
        _write(tmp_path, "Board.ecad+", _ecad(nets=[
            {"id": "n1", "name": "GND_B"},
            {"id": "n2", "name": "VCC"},
        ]))
        v.checkpoint("b renames GND")

        result = branches.merge_branches(v, "a", "b")
        assert result["ok"]
        assert result["conflict_count"] >= 1
        assert any(c.get("id") == "n1" for c in result["conflicts"])
    finally:
        v.close()


# ---------------------------------------------------------------------------
# binary whole-file
# ---------------------------------------------------------------------------

def test_binary_both_modified_conflict():
    base = b"AAA"
    src = b"BBB"
    dst = b"CCC"
    blob, cfl = branches.merge_blob(base, src, dst, path="items/x/thumbnail.png")
    assert blob == dst  # dst-favored
    assert len(cfl) == 1
    assert cfl[0]["kind"] == "binary"
    assert cfl[0]["reason"] == "both_modified"


def test_binary_one_side_modified():
    base = b"AAA"
    src = b"BBB"
    dst = b"AAA"
    blob, cfl = branches.merge_blob(base, src, dst, path="thumb.png")
    assert blob == src
    assert cfl == []


# ---------------------------------------------------------------------------
# branch-scoped AI-VC
# ---------------------------------------------------------------------------

def test_ai_vc_timeline_is_branch_scoped(tmp_path):
    v = _open_seeded(tmp_path, _part())
    try:
        branches.create_branch(v, "ai-work")
        branches.switch_branch(v, "ai-work")

        root_work = branches.bind_ai_vc_to_branch(v, "ai-work")
        store_work = ai_vc.reset_store_for_tests(root_work)
        ai_vc.stamp_authored("doc-1", "agent")
        ai_vc.set_session_context(session_id="s1", turn_id=1)
        r1 = store_work.checkpoint(
            "doc-1",
            _part(wall=4.0),
            turn_id=1,
            op_batch="edit",
            label="thicken",
            kind="tool",
        )
        assert r1["ok"]

        # switch to main — different AI-VC root, empty timeline
        branches.switch_branch(v, "main")
        root_main = branches.bind_ai_vc_to_branch(v, "main")
        assert root_main != root_work
        store_main = ai_vc.get_store(root_main)
        tl_main = store_main.timeline("doc-1")
        assert tl_main["ok"]
        assert tl_main["checkpoints"] == []

        # back to ai-work — checkpoint still there
        branches.switch_branch(v, "ai-work")
        root_work2 = branches.bind_ai_vc_to_branch(v, "ai-work")
        store_work2 = ai_vc.get_store(root_work2)
        tl_work = store_work2.timeline("doc-1")
        assert tl_work["ok"]
        assert len(tl_work["checkpoints"]) == 1
        assert tl_work["checkpoints"][0]["id"] == r1["id"]
    finally:
        v.close()


def test_rpc_registration():
    from engine_oracle.parts_hub.plm_branch_rpc import PLM_BRANCH_METHODS
    from engine_oracle.parts_hub.worker_rpc import WORKER_METHODS

    for name in (
        "plm_branch_create",
        "plm_branch_list",
        "plm_branch_switch",
        "plm_branch_merge",
    ):
        assert name in PLM_BRANCH_METHODS
        assert name in WORKER_METHODS
        assert callable(WORKER_METHODS[name])


def test_rpc_create_list_merge_end_to_end(tmp_path):
    from engine_oracle.parts_hub.plm_branch_rpc import PLM_BRANCH_METHODS

    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    v.checkpoint("seed")
    v.close()

    path = str(tmp_path)
    emit = lambda *a, **k: None  # noqa: E731

    r = PLM_BRANCH_METHODS["plm_branch_create"](
        {"path": path, "name": "rpc-feat"}, emit)
    assert r["ok"], r

    listed = PLM_BRANCH_METHODS["plm_branch_list"]({"path": path}, emit)
    assert listed["ok"]
    assert any(b["name"] == "rpc-feat" for b in listed["branches"])

    # edit on rpc-feat
    sw = PLM_BRANCH_METHODS["plm_branch_switch"](
        {"path": path, "name": "rpc-feat"}, emit)
    assert sw["ok"]
    _write(tmp_path, "Bracket.step+", _part(wall=11.0))
    v = Vault.open(tmp_path)
    v.checkpoint("rpc edit")
    v.close()

    # merge back to main
    sw2 = PLM_BRANCH_METHODS["plm_branch_switch"](
        {"path": path, "name": "main"}, emit)
    assert sw2["ok"]
    merged = PLM_BRANCH_METHODS["plm_branch_merge"](
        {"path": path, "src": "rpc-feat", "dst": "main"}, emit)
    assert merged["ok"], merged
    assert merged["conflict_count"] == 0
