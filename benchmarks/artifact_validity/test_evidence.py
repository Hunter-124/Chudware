"""Evidence is built from ONE declared artifact, reopened in the in-process engine."""
from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.artifact_validity.evidence import build_evidence, gate
from benchmarks.artifact_validity.tasks.schema import Status

_BOX = [{"op": "add_feature", "feature": {"type": "box", "name": "Slab",
                                          "params": {"length": 60, "width": 40,
                                                     "height": 20}}}]


def test_saved_part_reopens_with_bodies_features_and_studies(build_part):
    path = build_part(_BOX)
    ev = build_evidence(path)
    assert ev.exists and ev.open_error is None and ev.engine_error is None, ev.summary()
    assert ev.doc_id and len(ev.sha256 or "") == 64 and ev.size_bytes
    assert len(ev.bodies) == 1, ev.bodies
    body = ev.bodies[0]
    assert body["volume_mm3"] == pytest.approx(48000.0, rel=1e-6)
    assert body["bbox"]["size"] == pytest.approx([60, 40, 20], abs=1e-3)
    assert body["valid"] is True and body["closed"] is True
    assert [(f["type"], f["name"], f["failed"]) for f in ev.features] == [
        ("box", "Slab", False)]
    assert ev.features[0]["params"]["length"] == 60
    assert ev.studies == [] and ev.study("static") is None
    assert ev.rpc("doc_bbox")["size"] == pytest.approx([60, 40, 20], abs=1e-6)
    assert gate(ev, "x") is None
    assert ev.summary()["bodies"][0]["volume_mm3"] == pytest.approx(48000.0)


def test_missing_file_is_a_fail_not_a_crash(tmp_path: Path):
    ev = build_evidence(tmp_path / "deliverable.step+")
    assert not ev.exists and ev.doc_id is None
    assert gate(ev, "c").status is Status.FAIL
    with pytest.raises(RuntimeError):
        ev.rpc("doc_tree", {})


def test_empty_and_garbage_files_fail_to_open(tmp_path: Path):
    empty = tmp_path / "deliverable.step+"
    empty.write_bytes(b"")
    ev = build_evidence(empty)
    assert ev.exists and ev.open_error == "file is empty"
    assert gate(ev, "c").status is Status.FAIL

    junk = tmp_path / "junk.step+"
    junk.write_text("this is prose, not a part")
    ev = build_evidence(junk)
    assert ev.exists and ev.doc_id is None and ev.open_error
    assert gate(ev, "c").status is Status.FAIL


def test_bodyless_document_has_empty_bodies_without_probe_errors(cad_tools, tmp_path,
                                                                 monkeypatch):
    import json

    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    doc = json.loads(cad_tools["new_part"](name="bodyless", fresh=True))["doc_id"]
    path = tmp_path / "deliverable.step+"
    assert json.loads(cad_tools["save_part"](path=str(path), doc_id=doc,
                                             allow_empty=True))["ok"]
    ev = build_evidence(path)
    assert ev.opened and ev.bodies == [] and ev.probe_errors == {}, ev.summary()


def test_engine_fault_gates_to_error(tmp_path: Path):
    from benchmarks.artifact_validity.evidence import Evidence

    ev = Evidence(final_path=tmp_path / "x.step+", exists=True, engine_error="boom")
    assert gate(ev, "c").status is Status.ERROR
