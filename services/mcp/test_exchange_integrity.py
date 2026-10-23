"""Geometry-complete nested exports, stable ownership, and independent native imports."""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from services.mcp.test_gsl_tools import _call, cb, pytestmark


@pytest.fixture(scope="module")
def exchange_daemon():
    """Own the document pool swept by these tools, without disturbing other suites."""
    from services.mcp import cad_common as C
    from services.mcp import daemon_bridge, private_hostd

    with tempfile.TemporaryDirectory(prefix="cw-exchange-", dir="/tmp") as directory:
        scratch = Path(directory)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
            patch.setenv("CHUDWARE_CHUDCODE_BIN", str(scratch / "no-model-runtime"))
            spawned = private_hostd.spawn_private_daemon(scratch)
            assert spawned is not None, private_hostd.last_failure
            process, socket_path, key = spawned
            patch.setenv("CHUDWARE_HOSTD_SOCKET", socket_path)
            patch.setenv("CHUDWARE_INSTANCE_KEY", key)
            connection = daemon_bridge.DaemonClient(writer_group=key)
            # These are real RPCs through one owned, thread-safe connection. A new
            # socket environment alone would leave existing thread clients attached
            # to the package daemon and its unrelated documents.
            patch.setattr(daemon_bridge, "client", lambda: connection)
            patch.setattr(C, "_TOUCHED_DOCS", [])
            patch.setattr(C, "_PROCESS_DOCS", set())
            patch.setattr(C, "_DOC_ALIASES", {})
            patch.setattr(C, "_DISCARDED_DOCS", {})
            patch.setattr(C, "_HARNESS_SAVED_PATHS", {})
            patch.setattr(cb, "_FOREIGN_EDITS_PENDING", {})
            try:
                connection.connect(spawn=False)
                with C.doc_scope():
                    C._CUR["doc_id"] = None
                    yield connection
            finally:
                C.quiesce_publishes(10.0)
                connection.close()
                private_hostd._stop(process, scratch)



@pytest.fixture
def docs(exchange_daemon):
    ids = []
    yield ids
    for did in reversed(ids):
        cb.call("doc_close", {"doc_id": did})


def new_doc(docs, kind="part"):
    did = cb.call("doc_new", {"kind": kind, "name": "Exchange integrity"})["doc_id"]
    docs.append(did)
    return did


def box(did):
    return cb.call("doc_apply_ops", {"doc_id": did, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(10), "width": cb.dim(10),
                                   "height": cb.dim(10)})]})


def insert(did, path, name, x=0):
    return cb.call("doc_apply_ops", {"doc_id": did, "ops": [{
        "op": "asm_insert_component", "name": name, "source": {"path": str(path)},
        "fixed": True, "transform": [1, 0, 0, x, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}]})


def test_nested_reference_export_is_complete_or_refuses(tmp_path, docs):
    leaf = new_doc(docs)
    box(leaf)
    leaf_path = tmp_path / "leaf.step+"
    cb.call("doc_save", {"doc_id": leaf, "path": str(leaf_path)})
    child = new_doc(docs, "assembly")
    child_path = tmp_path / "wheels.step+"
    cb.call("doc_save", {"doc_id": child, "path": str(child_path)})
    insert(child, leaf_path.name, "wheel-left")
    insert(child, leaf_path.name, "wheel-right", 30)
    cb.call("doc_save", {"doc_id": child, "path": str(child_path)})
    opened, _ = _call("import_part", {"path": str(child_path)})
    assert opened["ok"], opened
    docs.append(opened["doc_id"])
    detached = cb.call("asm_world_bodies", {"doc_id": opened["doc_id"]})["bodies"]
    assert len(detached) == 2
    parent = new_doc(docs, "assembly")
    insert(parent, child_path, "wheel-module", 100)
    insert(parent, leaf_path, "frame")

    for fmt in ("step", "step+"):
        destination = tmp_path / ("rover." + fmt)
        out, _ = _call("export_part", {"doc_id": parent, "fmt": fmt,
                                        "path": str(destination)})
        assert out["ok"], out
        assert [(c["name"], c["solids"]) for c in out["components"]] == [
            ("wheel-module", 2), ("frame", 1)]
        assert out["components"][0]["source"]["path"] == str(child_path)
        if fmt == "step":
            assert out["validated"] is True
            assert out["solid_count"] == out["expected_solids"] == 3
        else:
            with zipfile.ZipFile(destination) as archive:
                step = tmp_path / "native-geometry.step"
                step.write_bytes(archive.read("geometry.step"))
            destination = step
        recovered = cb.call("import_file", {"path": str(destination), "format": "step"})["doc_id"]
        docs.append(recovered)
        bodies = cb.call("asm_world_bodies", {"doc_id": recovered})["bodies"]
        assert len(bodies) == 3
        mass = cb.call("eval_mass", {"doc_id": recovered})
        assert mass["volume_mm3"] == pytest.approx(3000)

    # A descendant disappears WITHOUT editing the parent. No old cache may certify it.
    leaf_path.unlink()
    for fmt in ("step", "step+"):
        refused, _ = _call("export_part", {"doc_id": parent, "fmt": fmt,
                                             "path": str(tmp_path / ("incomplete." + fmt))})
        assert refused.get("ok") is False, refused
        assert "wheel-module" in json.dumps(refused)
        assert str(child_path) in json.dumps(refused)
        assert not refused.get("validated")
    target = new_doc(docs)
    open_ids = {d["doc_id"] for d in cb.call("doc_list", {})["docs"]}
    refused, _ = _call("import_part", {"path": str(child_path), "into": target})
    assert refused["ok"] is False
    assert {d["doc_id"] for d in cb.call("doc_list", {})["docs"]} == open_ids


def test_owner_map_complete_and_identical_across_five_reads(docs):
    did = new_doc(docs)
    box(did)
    ops = [cb.feat("transform", f"Instance {i}",
                   {"translate": [20 * (i + 1), 0, 0], "copy": True, "body": "f1"})
           for i in range(16)]
    cb.call("doc_apply_ops", {"doc_id": did, "ops": ops})
    snapshots = [_call("get_part", {"doc_id": did})[0]["owner_map"] for _ in range(5)]
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    owner = snapshots[0]
    assert set(owner) == {f"f{i}" for i in range(1, 18)}
    assert {body for entry in owner.values() for body in entry["bodies"]} == set(range(1, 18))
    for entry in owner.values():
        assert len(entry["faces"]) == 6
        assert len(entry["edges"]) == 12


def test_native_imports_are_independent_and_resume_is_explicit(tmp_path, docs):
    source = new_doc(docs)
    box(source)
    path = tmp_path / "source.step+"
    cb.call("doc_save", {"doc_id": source, "path": str(path)})
    imports = []
    for _ in range(2):
        result, _ = _call("import_part", {"path": str(path)})
        assert result["ok"], result
        imports.append(result["doc_id"])
        docs.append(result["doc_id"])
    assert len({source, *imports}) == 3
    before = {did: cb.call("eval_mass", {"doc_id": did})["com"] for did in [source, *imports]}
    moved, _ = _call("add_features", {"doc_id": imports[1], "ops": [
        {"type": "transform", "body": "f1", "translate": [40, 0, 0]}]})
    assert moved["ok"], moved
    for did in (source, imports[0]):
        assert cb.call("eval_mass", {"doc_id": did})["com"] == before[did]
    after = cb.call("eval_mass", {"doc_id": imports[1]})["com"]
    assert after[0] == pytest.approx(before[imports[1]][0] + 40)
    resumed, _ = _call("import_part", {"path": str(path), "resume": True})
    assert resumed["doc_id"] == source
    assert resumed["reused"] is True


def test_eleven_native_imports_keep_ownership_and_move_in_target(tmp_path, docs):
    source = new_doc(docs)
    box(source)
    source_com = cb.call("eval_mass", {"doc_id": source})["com"]
    path = tmp_path / "piece.step+"
    cb.call("doc_save", {"doc_id": source, "path": str(path)})
    target = new_doc(docs)
    for _ in range(11):
        imported, _ = _call("import_part", {"path": str(path), "into": target})
        assert imported["ok"], imported
        assert imported["doc_id"] == target
    snapshots = [_call("get_part", {"doc_id": target})[0] for _ in range(5)]
    features = snapshots[0]["tree"]["features"]
    import_ids = [f["id"] for f in features if f["type"] == "import"]
    assert len(import_ids) == 11
    owner = snapshots[0]["owner_map"]
    assert set(owner) == set(import_ids)
    assert all(s["owner_map"] == owner for s in snapshots)
    assert {b for entry in owner.values() for b in entry["bodies"]} == set(range(1, 12))
    moved, _ = _call("add_features", {"doc_id": target, "ops": [
        {"type": "transform", "body": fid, "translate": [20 * (i + 1), 0, 0]}
        for i, fid in enumerate(import_ids)]})
    assert moved["ok"], moved
    bodies = cb.call("asm_world_bodies", {"doc_id": target})["bodies"]
    assert len(bodies) == 11
    for i, body in enumerate(bodies):
        bounds = body["bbox"]
        com = [(lo + hi) / 2 for lo, hi in zip(bounds["min"], bounds["max"])]
        assert com == pytest.approx([source_com[0] + 20 * (i + 1), *source_com[1:]], abs=1e-6)
    assert cb.call("eval_mass", {"doc_id": source})["com"] == source_com


def test_cyclic_native_references_refuse_without_waiting_on_the_cache(tmp_path, docs):
    first = new_doc(docs, "assembly")
    second = new_doc(docs, "assembly")
    a, b = tmp_path / "a.step+", tmp_path / "b.step+"
    cb.call("doc_save", {"doc_id": first, "path": str(a)})
    cb.call("doc_save", {"doc_id": second, "path": str(b)})
    insert(first, b, "second")
    cb.call("doc_save", {"doc_id": first, "path": str(a)})
    insert(second, a, "first")
    cb.call("doc_save", {"doc_id": second, "path": str(b)})
    refused, _ = _call("export_part", {"doc_id": first, "fmt": "step",
                                        "path": str(tmp_path / "cycle.step")})
    assert refused["ok"] is False
    assert "cyclic component reference" in json.dumps(refused)
    assert not (tmp_path / "cycle.step").exists()


def test_detached_native_import_is_reproducible_and_saves_edits_in_place(
        tmp_path, docs, monkeypatch):
    from services.mcp import cad_common as C

    # Exercise the real daemon's no-handoff behavior in this module's owned pool.
    assert cb.active_daemon_mode()
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    source = new_doc(docs)
    box(source)
    destination = tmp_path / "parts" / "base.step+"
    destination.parent.mkdir()
    cb.call("doc_set_name", {"doc_id": source, "name": "base_revD"})
    cb.call("doc_save", {"doc_id": source, "path": str(destination)})
    cb.call("doc_close", {"doc_id": source})
    docs.remove(source)

    imported, _ = _call("import_part", {"path": str(destination)})
    did = imported["doc_id"]
    docs.append(did)
    monkeypatch.setattr(C, "_TOUCHED_DOCS", [did])
    before = destination.read_bytes()
    cb.call("eval_mass", {"doc_id": did})
    _call("verify_battery", {"doc_id": did})
    row = next(e for e in cb.call("doc_list", {})["docs"] if e["doc_id"] == did)
    assert row["path"] is None
    assert row["native_source_path"] == str(destination)
    assert row["native_source_edited"] is False
    assert row["reproducible_from"] == [str(destination)]
    swept = C.save_touched_docs()
    assert any(e["doc_id"] == did and e["reason"] == "reproducible_import"
               for e in swept["skipped"])
    assert destination.read_bytes() == before
    assert not list(tmp_path.glob("*.step+"))
    # Unedited provenance is reproducible scratch, not source-overwrite authority.
    saved, _ = _call("save_part", {"doc_id": did})
    assert saved["ok"], saved
    detached_path = tmp_path / "base_revD.step+"
    assert saved["path"] == str(detached_path)
    assert detached_path.is_file()
    assert destination.read_bytes() == before

    # Non-study authored changes retain automatic source-save behavior.
    again, _ = _call("import_part", {"path": str(destination)})
    edited = again["doc_id"]
    docs.append(edited)
    original_com = cb.call("eval_mass", {"doc_id": edited})["com"]
    moved, _ = _call("add_features", {"doc_id": edited, "ops": [
        {"type": "transform", "body": "f1", "translate": [40, 0, 0]}]})
    assert moved["ok"], moved
    assert moved["saved_path"] == str(destination)
    assert list(tmp_path.glob("*.step+")) == [detached_path]
    reopened = cb.call("doc_open", {"path": str(destination)})["doc_id"]
    docs.append(reopened)
    assert cb.call("eval_mass", {"doc_id": reopened})["com"] == pytest.approx(
        [original_com[0] + 40, *original_com[1:]])


def test_face_sampling_re_resolves_after_a_geometry_edit(docs):
    did = new_doc(docs)
    box(did)
    selector = cb.call("doc_query", {"doc_id": did, "query": {
        "of": "face", "side": "top", "pick": "largest"}})["matches"][0]
    first = cb.call("doc_face_points", {"doc_id": did, "selector": selector})
    repeated = cb.call("doc_face_points", {"doc_id": did, "selector": selector})
    assert repeated == first
    cb.call("doc_apply_ops", {"doc_id": did, "ops": [
        cb.feat("transform", "Move", {"body": "f1", "translate": [40, 0, 0]})]})
    new_selector = cb.call("doc_query", {"doc_id": did, "query": {
        "of": "face", "side": "top", "pick": "largest"}})["matches"][0]
    moved = cb.call("doc_face_points", {"doc_id": did, "selector": new_selector})
    assert sorted(p[0] for p in moved["points"]) == pytest.approx(
        sorted(p[0] + 40 for p in first["points"]))
