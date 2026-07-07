"""Tests for the RE session manager (``aicad.reveng_session/1`` CRUD + dir layout)."""
from __future__ import annotations

import json

import pytest

from engine_oracle.vision import reveng_session as rs


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "reveng"


# -- ids & root -------------------------------------------------------------------------
def test_session_id_shape():
    sid = rs.new_session_id()
    assert rs.is_valid_session_id(sid)
    assert sid.startswith("re_") and len(sid) == 11
    assert not rs.is_valid_session_id("re_XYZ")
    assert not rs.is_valid_session_id("bad")


def test_default_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AICAD_REVENG_DIR", str(tmp_path / "custom"))
    assert rs.default_reveng_root() == tmp_path / "custom"


def test_default_root_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("AICAD_REVENG_DIR", raising=False)
    monkeypatch.setattr(rs.os, "name", "posix")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert rs.default_reveng_root() == tmp_path / "xdg" / "AI-CAD" / "reveng"


# -- create -----------------------------------------------------------------------------
def test_create_builds_dir_tree_and_manifest(root):
    sess = rs.create("M8 bracket", part_ref="catalog:grabcad:bracket-123", root=root)

    assert sess.manifest_path.exists()
    for sub in rs.SESSION_SUBDIRS:
        assert sess.subdir(sub).is_dir()

    m = json.loads(sess.manifest_path.read_text())
    assert m["schema"] == "aicad.reveng_session/1"
    assert m["session_id"] == sess.session_id
    assert m["name"] == "M8 bracket"
    assert m["status"] == "capturing"
    assert m["part_ref"] == "catalog:grabcad:bracket-123"
    assert set(m["stages"]) == set(rs.STAGE_KEYS)
    assert m["stages"]["capture"] == "pending"
    assert m["scale"]["confirmed"] is False
    assert m["honesty"]["pipeline"] == "scan_mesh_ai_parametric"
    assert m["honesty"]["approximate"] is False
    assert m["provenance"] == []


def test_create_requires_name(root):
    with pytest.raises(rs.RevEngSessionError):
        rs.create("   ", root=root)


def test_create_rejects_duplicate(root):
    sess = rs.create("dupe", root=root, session_id="re_00000001")
    with pytest.raises(rs.RevEngSessionError):
        rs.create("dupe2", root=root, session_id="re_00000001")


def test_create_rejects_bad_id(root):
    with pytest.raises(rs.RevEngSessionError):
        rs.create("x", root=root, session_id="nope")


# -- load / exists / list ---------------------------------------------------------------
def test_load_roundtrip(root):
    sess = rs.create("part", root=root)
    reloaded = rs.load(sess.session_id, root=root)
    assert reloaded.session_id == sess.session_id
    assert reloaded.manifest["name"] == "part"


def test_load_missing_raises(root):
    with pytest.raises(rs.RevEngSessionError):
        rs.load("re_deadbeef", root=root)


def test_load_bad_schema(root):
    sess = rs.create("part", root=root)
    m = json.loads(sess.manifest_path.read_text())
    m["schema"] = "aicad.reveng_session/999"
    sess.manifest_path.write_text(json.dumps(m))
    with pytest.raises(rs.RevEngSessionError):
        rs.load(sess.session_id, root=root)


def test_exists(root):
    assert not rs.exists("re_00000002", root=root)
    rs.create("p", root=root, session_id="re_00000002")
    assert rs.exists("re_00000002", root=root)
    assert not rs.exists("garbage", root=root)


def test_load_or_create(root):
    a = rs.load_or_create("re_00000003", name="fresh", root=root)
    assert a.manifest["name"] == "fresh"
    b = rs.load_or_create("re_00000003", name="ignored", root=root)
    assert b.manifest["name"] == "fresh"  # loaded, not recreated


def test_list_sessions_sorted(root):
    import time

    rs.create("first", root=root, session_id="re_00000010")
    time.sleep(0.01)
    rs.create("second", root=root, session_id="re_00000011")
    listing = rs.list_sessions(root=root)
    assert [s["session_id"] for s in listing][0] == "re_00000011"
    assert len(listing) == 2
    # non-session junk is ignored
    (root / "not_a_session").mkdir()
    assert len(rs.list_sessions(root=root)) == 2


def test_list_empty_root(tmp_path):
    assert rs.list_sessions(root=tmp_path / "nope") == []


# -- mutators ---------------------------------------------------------------------------
def test_set_status(root):
    sess = rs.create("p", root=root)
    sess.set_status("reconstructing")
    assert rs.load(sess.session_id, root=root).manifest["status"] == "reconstructing"
    with pytest.raises(rs.RevEngSessionError):
        sess.set_status("bogus")


def test_set_stage(root):
    sess = rs.create("p", root=root)
    sess.set_stage("reconstruct", "running")
    assert rs.load(sess.session_id, root=root).manifest["stages"]["reconstruct"] == "running"
    with pytest.raises(rs.RevEngSessionError):
        sess.set_stage("reconstruct", "weird")
    with pytest.raises(rs.RevEngSessionError):
        sess.set_stage("no_stage", "done")


def test_update_scale(root):
    sess = rs.create("p", root=root)
    sess.update_scale(method="aruco_mat", mm_per_unit=1.0, residual_pct=0.4, confirmed=True)
    m = rs.load(sess.session_id, root=root).manifest["scale"]
    assert m["method"] == "aruco_mat"
    assert m["residual_pct"] == 0.4
    assert m["confirmed"] is True


def test_update_artifacts(root):
    sess = rs.create("p", root=root)
    sess.update_artifacts(images=12, mesh_path="mesh/cleaned.ply")
    m = rs.load(sess.session_id, root=root).manifest["artifacts"]
    assert m["images"] == 12
    assert m["mesh_path"] == "mesh/cleaned.ply"
    with pytest.raises(rs.RevEngSessionError):
        sess.update_artifacts(unknown_key=1)


def test_single_photo_tier_forces_approximate(root):
    sess = rs.create("p", root=root)
    sess.set_single_photo_tier("triposr")
    h = rs.load(sess.session_id, root=root).manifest["honesty"]
    assert h["single_photo_tier"] == "triposr"
    assert h["approximate"] is True

    sess.set_single_photo_tier(None)
    h = rs.load(sess.session_id, root=root).manifest["honesty"]
    assert h["single_photo_tier"] is None
    assert h["approximate"] is False


def test_add_provenance(root):
    sess = rs.create("p", root=root)
    sess.add_provenance({"method": "photo_fiducial", "tier": "medium"})
    prov = rs.load(sess.session_id, root=root).manifest["provenance"]
    assert prov == [{"method": "photo_fiducial", "tier": "medium"}]


def test_updated_at_bumps(root):
    sess = rs.create("p", root=root)
    first = sess.manifest["updated_at"]
    import time

    time.sleep(1.1)
    sess.set_status("modeling")
    assert sess.manifest["updated_at"] >= first


def test_subdir_rejects_unknown(root):
    sess = rs.create("p", root=root)
    with pytest.raises(rs.RevEngSessionError):
        sess.subdir("nope")


# -- delete -----------------------------------------------------------------------------
def test_delete(root):
    sess = rs.create("p", root=root, session_id="re_000000ff")
    assert rs.delete("re_000000ff", root=root) is True
    assert not rs.exists("re_000000ff", root=root)
    assert rs.delete("re_000000ff", root=root) is False
