"""Nothing the agent built is left as a "part potentially lost".

hostd retires an abandoned document to its crash-recovery ORPHAN tier when
``path.empty() || dirty || isAutosavePath(path)`` (core/hostd/server.cpp::gcCloseDoc), and
every orphan is offered back through the GUI's "Recover unsaved parts?" prompt. Nothing
closes the documents an agent run builds — ``services/agent/loop.py::_terminal_autosave_close_docs``
was removed and the C++ mission path only
saves the one doc a DAG node is on — so every agent doc without a real backing file
eventually became a recovery row. Measured on the reporting user's machine 2026-08-05:
eight retired in the same second at the 30-minute agent GC grace, then all eight opened as
tabs when he accepted the prompt.

``save_touched_docs`` closes that gap by SAVING, never closing: a close would fan out
doc.closed and destroy the GUI mirror tab the user browses after a run, which is the very
reason agent-origin docs get the long GC floor.

The sweep persists only this turn's touched documents and never silently saves
outstanding human edits, irrespective of which client opened the document.

Run:  python3 -m pytest services/mcp/test_terminal_save_sweep.py -q -p no:randomly
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_common as C  # noqa: E402


class _FakeEngine:
    """doc_list from a fixed roster; doc_save records the call and writes the file.

    Thread-affine on purpose. This stands in for the engine only for the code under
    test, which runs synchronously on the installing thread; a call arriving on any
    other thread is somebody else's in-flight work — a deferred handoff publish armed
    by an earlier test is the one measured in practice — and is passed through to the
    real bridge instead of being recorded as a save the sweep made.
    """

    def __init__(self, docs, passthrough=None, studies=None):
        self.docs = [dict(d) for d in docs]
        self.studies = dict(studies or {})
        self.saves: list = []
        self._owner = threading.current_thread()
        self._passthrough = passthrough

    def call(self, method, params=None):
        if threading.current_thread() is not self._owner:
            return self._passthrough(method, params) if self._passthrough else {}
        p = params or {}
        if method == "doc_list":
            return {"docs": [dict(e) for e in self.docs]}
        if method == "doc_save":
            self.saves.append((p.get("doc_id"), p.get("path")))
            dest = Path(str(p.get("path")))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({
                "doc_id": p.get("doc_id"),
                "studies": self.studies.get(p.get("doc_id"), []),
            }))
            for row in self.docs:
                if row.get("doc_id") == p.get("doc_id"):
                    row.update(path=str(dest), dirty=False)
            return {"ok": True, "path": str(dest)}
        if method == "sim_study_list":
            return {"studies": [dict(s) for s in self.studies.get(p.get("doc_id"), [])]}
        if method == "sim_study_add":
            studies = self.studies.setdefault(p["doc_id"], [])
            record = dict(p["study"])
            record.setdefault("id", f"study-{len(studies) + 1}")
            studies.append(record)
            for row in self.docs:
                if row.get("doc_id") == p["doc_id"]:
                    row.update(revision=row.get("revision", 0) + 1, dirty=True,
                               reproducible_from=[])
            return {"study_id": record["id"]}
        return {}


@pytest.fixture()
def sweep(monkeypatch, tmp_path):
    """Install a fake engine + project save dir, and reset the touched-doc registry."""
    save_dir = tmp_path / "Projects" / "Testing"
    # Let any trailing publish an earlier test armed finish against the REAL bridge
    # before the fake goes in (cad_common.quiesce_publishes).
    C.quiesce_publishes(10.0)
    real_call = C.cb.call

    def _install(docs, *, touched=(), project_dir=save_dir, studies=None):
        fake = _FakeEngine(docs, passthrough=real_call, studies=studies)
        monkeypatch.setattr(C.cb, "call", fake.call)
        if project_dir is None:
            monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
        else:
            monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(project_dir))
        monkeypatch.setattr(C, "_TOUCHED_DOCS", {})
        for did in touched:
            C.note_touched_doc(did, row=next((row for row in fake.docs
                                              if row.get("doc_id") == did), None))
        return fake

    return _install


def test_terminal_rollback_warnings_use_current_touched_tree(sweep, monkeypatch):
    sweep([], touched=["partial", "complete"])
    trees = {
        "partial": {"rollback_after": "f1", "features": [
            {"id": "f1"}, {"id": "f7"}, {"id": "f8"}]},
        "complete": {"rollback_after": "f3", "features": [{"id": "f3"}]},
    }
    monkeypatch.setattr(C.cb, "call", lambda method, args: {"tree": trees[args["doc_id"]]})
    warnings = C.touched_rollback_warnings()
    assert len(warnings) == 1
    assert warnings[0]["doc_id"] == "partial"
    assert warnings[0]["after"] == "f1"
    assert warnings[0]["hidden_features"] == ["f7", "f8"]
    assert all(fid in warnings[0]["warning"] for fid in ("f1", "f7", "f8"))
    trees["partial"]["rollback_after"] = "start"
    warnings = C.touched_rollback_warnings()
    assert warnings[0]["after"] == "start"
    assert warnings[0]["hidden_features"] == ["f1", "f7", "f8"]



def test_an_unsaved_agent_part_is_written_to_the_project_folder(sweep, tmp_path):
    """The reported case: the doc has geometry and no file, so hostd would retire it."""
    fake = sweep(
        [
            {
                "doc_id": "doc-cpp00000002",
                "name": "ruger_lcp_iwb_holster",
                "kind": "part",
                "feature_count": 6,
                "revision": 3,
                "dirty": True,
                "path": "",
                "origin_kind": "mcp",
            }
        ],
        touched=["doc-cpp00000002"],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["doc-cpp00000002"]
    assert len(out["saved"]) == 1
    written = Path(out["saved"][0]["path"])
    assert written.name == "ruger_lcp_iwb_holster.step+"
    assert written.parent == tmp_path / "Projects" / "Testing"
    assert written.exists()


def test_a_doc_already_in_a_real_clean_file_is_left_alone(sweep, tmp_path):
    """Idempotence: the sweep runs on every turn AND in the crash path, so a document
    that hostd would not retire must not be rewritten."""
    existing = tmp_path / "Projects" / "Testing" / "bracket.step+"
    existing.parent.mkdir(parents=True)
    existing.write_text("saved")
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "bracket",
                "kind": "part",
                "feature_count": 4,
                "dirty": False,
                "path": str(existing),
                "origin_kind": "mcp",
            }
        ],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []
    assert out["skipped"] == [
        {"doc_id": "d1", "name": "bracket", "reason": "already_saved"}
    ]


def test_the_users_own_document_is_never_touched(sweep):
    """A dirty unsaved GUI document is exactly the work the recovery prompt exists to
    protect. Saving it over a guessed filename would be data loss dressed as tidying."""
    fake = sweep(
        [
            {
                "doc_id": "d-gui",
                "name": "Part1",
                "kind": "part",
                "feature_count": 9,
                "dirty": True,
                "path": "",
                "origin_kind": "gui",
            }
        ],
        touched=[],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []


def test_a_touched_doc_with_human_edits_is_left_for_the_human(sweep):
    """An agent must not silently commit a human's unsaved changes."""
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "holster",
                "kind": "part",
                "feature_count": 5,
                "dirty": True,
                "human_dirty": True,
                "path": "",
                "origin_kind": "gui",
            }
        ],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []
    assert out["skipped_human_edits"] == [{"doc_id": "d1", "name": "holster"}]
    assert out["persistence_failure"]["reason"] == "skipped_human_edits"
    assert "d1" in C._TOUCHED_DOCS[C._touch_scope()]
    fake.docs[0]["human_dirty"] = False
    retry = C.save_touched_docs()
    assert [row["doc_id"] for row in retry["saved"]] == ["d1"]
    assert "d1" not in C._TOUCHED_DOCS.get(C._touch_scope(), {})


def test_an_empty_touched_doc_is_saved(sweep):
    """A created document is authored work even before geometry is added."""
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "holster",
                "kind": "part",
                "feature_count": 0,
                "empty": True,
                "dirty": False,
                "path": "",
                "origin_kind": "mcp",
            }
        ],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d1"]
    assert out["saved"][0]["doc_id"] == "d1"


def test_a_study_only_doc_is_saved(sweep, tmp_path):
    """A hydraulic circuit / piping network / vessel / duct sizing lives in a document
    with no geometry. The engine flags it `empty`, but it carries the whole design: the
    2026-10-08 clamp-HPU run lost its simulated circuit when the sweep skipped it."""
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "clamp_hpu",
                "kind": "part",
                "feature_count": 0,
                "empty": True,
                "dirty": False,
                "path": "",
                "origin_kind": "mcp",
            }
        ],
        touched=["d1"],
        studies={"d1": [{"id": "study-1", "type": "hydraulic_circuit", "name": "clamp"}]},
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d1"]
    assert Path(out["saved"][0]["path"]).name == "clamp_hpu.step+"


def test_a_crash_autosave_path_does_not_count_as_saved(sweep, tmp_path):
    """isAutosavePath() makes hostd retire it anyway: the appdata snapshot is crash
    recovery, not a file the user can find."""
    snap = C._autosave_dir() / "sess-holster.step+"
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "holster",
                "kind": "part",
                "feature_count": 5,
                "dirty": False,
                "path": str(snap),
                "origin_kind": "mcp",
            }
        ],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d1"]
    assert Path(out["saved"][0]["path"]).parent == tmp_path / "Projects" / "Testing"


def test_an_assembly_is_swept_on_its_component_count(sweep):
    """An assembly's content is components, not features — reading only feature_count
    would silently skip every assembly the agent built."""
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "motor_mount",
                "kind": "assembly",
                "feature_count": 0,
                "component_count": 4,
                "dirty": True,
                "path": "",
                "origin_kind": "mcp",
            }
        ],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d1"]
    assert out["saved"][0]["name"] == "motor_mount"


def test_a_touched_import_is_persisted_even_when_source_is_reproducible(sweep, tmp_path):
    """A source file does not prove that later metadata or other edits are persisted."""
    source = tmp_path / "depricated" / "_frame624_2026-09-07.step"
    source.parent.mkdir(parents=True)
    source.write_text("ISO-10303-21;\n")
    row = {"kind": "part", "dirty": True, "path": "", "origin_kind": "mcp"}
    fake = sweep(
        [
            {**row, "doc_id": "d-probe", "name": "_frame624_2026-09-07",
             "feature_count": 1, "revision": 1, "reproducible_from": [str(source)]},
            # Engine empties reproducible_from once anything is authored after the import.
            {**row, "doc_id": "d-placed", "name": "frame_placed",
             "feature_count": 2, "revision": 2, "reproducible_from": []},
            {**row, "doc_id": "d-orphaned", "name": "frame_orphaned",
             "feature_count": 1, "revision": 1,
             "reproducible_from": [str(tmp_path / "gone" / "frame.step")]},
        ],
        touched=["d-probe", "d-placed", "d-orphaned"],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d-probe", "d-placed", "d-orphaned"]
    assert out["skipped"] == []
    assert out["persistence_failure"] is None


@pytest.mark.parametrize("gui_open,design_edited", [
    (False, False), (True, False), (True, True),
])
def test_detached_simulation_preserves_its_source_and_delivers_its_study(
        sweep, tmp_path, gui_open, design_edited):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "arm.step+"
    source.write_bytes(b"user's saved assembly")
    occupied = project / "arm_2.step+"
    occupied.write_bytes(b"another existing deliverable")
    docs = [{
        "doc_id": "d-scratch", "name": "arm", "kind": "assembly",
        "component_count": 3, "revision": int(design_edited), "dirty": design_edited,
        "path": None, "native_source_path": str(source),
        "native_source_edited": design_edited,
        "reproducible_from": [] if design_edited else [str(source)],
        "origin_kind": "agent",
    }]
    if gui_open:
        docs.append({
            "doc_id": "d-gui", "name": "user arm", "kind": "assembly",
            "component_count": 3, "dirty": True, "path": str(source),
            "origin_kind": "gui",
        })
    fake = sweep(docs, touched=["d-scratch"], project_dir=project)
    record = {"type": "motion", "name": "Motion: m75",
              "motion": {"drivers": [{"mate_id": "m75", "start": 0, "stop": 80}],
                         "min_clearance": 0.5}}
    study_id = C._persist_study("d-scratch", record)
    assert study_id

    result = C.save_touched_docs()

    destination = project / "arm_3.step+"
    assert fake.saves == [("d-scratch", str(destination))]
    assert result["saved"][0]["path"] == str(destination)
    assert result["errors"] == []
    assert source.read_bytes() == b"user's saved assembly"
    assert occupied.read_bytes() == b"another existing deliverable"
    assert json.loads(destination.read_text())["studies"] == [
        {**record, "id": study_id}]
    assert C.save_touched_docs()["saved"] == []
    assert fake.saves == [("d-scratch", str(destination))]


def test_detached_recovery_snapshot_still_gets_a_real_deliverable(
        sweep, tmp_path, monkeypatch):
    recovery = tmp_path / "hostd_autosave"
    recovery.mkdir()
    source = recovery / "base.step+"
    source.write_text("recovery snapshot")
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(recovery))
    fake = sweep([{
        "doc_id": "d-recovered", "name": "base", "kind": "part",
        "feature_count": 2, "revision": 0, "dirty": False, "path": None,
        "native_source_path": str(source), "reproducible_from": [str(source)],
        "origin_kind": "agent",
    }], touched=["d-recovered"])
    out = C.save_touched_docs()
    assert out["saved"][0]["doc_id"] == "d-recovered"
    assert fake.saves == [("d-recovered", str(
        tmp_path / "Projects" / "Testing" / "base.step+"))]
    assert source.read_text() == "recovery snapshot"



def test_an_untouched_agent_origin_doc_is_not_swept(sweep):
    """A different turn's work must never be assigned this turn's workspace."""
    fake = sweep(
        [
            {
                "doc_id": "d-earlier",
                "name": "holster",
                "kind": "part",
                "feature_count": 6,
                "dirty": True,
                "path": "",
                "origin_kind": "agent",
            }
        ],
        touched=[],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []


def test_sweep_skips_untouched_docs_held_by_another_writer(sweep, monkeypatch, tmp_path):
    sweep(
        [
            {
                "doc_id": did,
                "name": did,
                "kind": "part",
                "feature_count": 3,
                "dirty": True,
                "path": "",
                "origin_kind": "agent",
                **({"other_writer": True} if other_writer else {}),
            }
            for did, other_writer in (
                ("d-foreign", True),
                ("d-unclaimed", False),
                ("d-touched", True),
            )
        ],
        touched=["d-touched"],
    )
    saved = []

    def project_save(doc_id, name):
        saved.append(doc_id)
        return str(tmp_path / f"{name}.step+")

    monkeypatch.setattr(C, "_project_save", project_save)
    out = C.save_touched_docs()

    assert saved == ["d-touched"]
    assert [row["doc_id"] for row in out["saved"]] == saved
    assert out["errors"] == []
    assert out["persistence_failure"] is None


def test_a_touched_dirty_doc_without_a_project_folder_is_a_persistence_failure(sweep):
    """A mutation with no durable destination is not a successful no-op. The geometry
    may still be live in hostd, but the caller must downgrade delivery until it is saved."""
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "holster",
                "kind": "part",
                "feature_count": 6,
                "dirty": True,
                "path": "",
                "origin_kind": "mcp",
            }
        ],
        touched=["d1"],
        project_dir=None,
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["skipped"] == []
    assert out["persistence_failure"] == {
        "reason": "no_project_save_dir",
        "docs": [{"doc_id": "d1", "name": "holster"}],
    }


def test_touched_bound_file_saves_without_project_directory(sweep, tmp_path):
    destination = tmp_path / "opened.step+"
    destination.write_text("old")
    fake = sweep([{"doc_id": "d1", "name": "opened", "dirty": True,
                   "path": str(destination), "origin_kind": "gui"}],
                 touched=["d1"], project_dir=None)
    out = C.save_touched_docs()
    assert fake.saves == [("d1", str(destination))]
    assert out["saved"][0]["path"] == str(destination)
    assert out["persistence_failure"] is None


@pytest.mark.parametrize("reopened", [False, True])
def test_missing_saved_document_uses_revision_receipt(sweep, monkeypatch, tmp_path, reopened):
    path = tmp_path / "bracket.step+"
    path.write_text("old")
    row = {"doc_id": "old", "name": "bracket", "path": str(path), "dirty": True,
           "revision": 5, "metadata_revision": 2}
    fake = sweep([row], touched=["old"])
    path.write_text("saved revision five")
    stamp = C._file_stamp(str(path))
    fake.docs[0].update(dirty=False, saved_revision=5, saved_metadata_revision=2,
                        file_stamp=stamp)
    monkeypatch.setattr(C.cb, "_document_row", lambda _doc: dict(fake.docs[0]))
    C.note_document_saved("old", {"path": str(path), "saved_revision": 5,
                                  "metadata_revision": 2})
    fake.docs = ([{"doc_id": "new", "name": "bracket", "path": str(path),
                   "file_stamp": stamp}] if reopened else [])

    out = C.save_touched_docs()

    assert out["errors"] == [] and out["warnings"] == []
    assert out["skipped"][0]["reason"] == ("reopened" if reopened else "already_saved")
    assert out["skipped"][0]["path"] == str(path)
    if reopened:
        assert out["skipped"][0]["open_doc_id"] == "new"
    assert "old" not in C._TOUCHED_DOCS.get(C._touch_scope(), {})


def test_missing_already_saved_revision_is_not_reported_lost(sweep, tmp_path):
    path = tmp_path / "already-saved.step+"
    path.write_text("saved")
    row = {"doc_id": "old", "name": "bracket", "path": str(path), "dirty": False,
           "revision": 4, "metadata_revision": 2, "saved_revision": 4,
           "saved_metadata_revision": 2, "file_stamp": C._file_stamp(str(path))}
    fake = sweep([row], touched=["old"])
    fake.docs = []
    out = C.save_touched_docs()
    assert out["skipped"][0]["reason"] == "already_saved"
    assert out["errors"] == [] and out["warnings"] == []


@pytest.mark.parametrize(("same_session", "revision", "metadata_revision", "preserved"), [
    (True, 5, 2, True),
    (False, 5, 2, False),
    (True, 4, 2, False),
    (True, 5, 1, False),
])
def test_missing_document_requires_current_recovery_snapshot(
        sweep, monkeypatch, same_session, revision, metadata_revision, preserved):
    row = {"doc_id": "old", "name": "bracket", "dirty": True,
           "revision": 5, "metadata_revision": 2}
    fake = sweep([row], touched=["old"])
    fake.docs = []
    entry = {"id": "snapshot-1", "doc_id": "old", "same_session": same_session,
             "revision": revision, "metadata_revision": metadata_revision}
    engine_call = fake.call
    monkeypatch.setattr(C.cb, "active_daemon_mode", lambda: True)
    monkeypatch.setattr(C.cb, "call", lambda method, params=None:
                        {"entries": [entry]} if method == "recovery_list"
                        else engine_call(method, params))

    out = C.save_touched_docs()

    assert out["errors"] == []
    if preserved:
        assert out["skipped"][0]["reason"] == "preserved_in_recovery"
        assert out["skipped"][0]["recovery_id"] == "snapshot-1"
        assert out["warnings"] == []
    else:
        assert out["skipped"] == []
        assert out["warnings"][0]["reason"] == "missing_unverified"
        assert "old" in C._TOUCHED_DOCS[C._touch_scope()]


def test_reopened_path_without_a_saved_revision_is_not_proof(sweep, monkeypatch, tmp_path):
    path = tmp_path / "bracket.step+"
    path.write_text("old version")
    row = {"doc_id": "old", "path": str(path), "dirty": True,
           "revision": 5, "metadata_revision": 2}
    fake = sweep([row], touched=["old"])
    fake.docs = [{"doc_id": "new", "path": str(path), "file_stamp": C._file_stamp(str(path))}]
    monkeypatch.setattr(C.cb, "active_daemon_mode", lambda: False)
    out = C.save_touched_docs()
    assert out["skipped"] == [] and out["errors"] == []
    assert out["warnings"][0]["reason"] == "missing_unverified"
    assert out["warnings"][0]["path"] == str(path)


def test_reopened_replaced_file_does_not_certify_the_old_revision(sweep, monkeypatch, tmp_path):
    path = tmp_path / "bracket.step+"
    path.write_text("saved old revision")
    row = {"doc_id": "old", "path": str(path), "dirty": False,
           "revision": 5, "metadata_revision": 2, "saved_revision": 5,
           "saved_metadata_revision": 2, "file_stamp": C._file_stamp(str(path))}
    fake = sweep([row], touched=["old"])
    path.write_text("unrelated replacement contents")
    fake.docs = [{"doc_id": "new", "path": str(path), "file_stamp": C._file_stamp(str(path))}]
    monkeypatch.setattr(C.cb, "active_daemon_mode", lambda: False)
    out = C.save_touched_docs()
    assert out["skipped"] == [] and out["errors"] == []
    assert out["warnings"][0]["reason"] == "missing_unverified"


def test_later_mutation_invalidates_saved_receipt(sweep, monkeypatch, tmp_path):
    path = tmp_path / "bracket.step+"
    path.write_text("saved")
    row = {"doc_id": "old", "path": str(path), "dirty": False, "revision": 5,
           "metadata_revision": 2, "saved_revision": 5, "saved_metadata_revision": 2,
           "file_stamp": C._file_stamp(str(path))}
    fake = sweep([row], touched=["old"])
    C.note_touched_doc("old", row={**row, "dirty": True, "revision": 6})
    fake.docs = []
    monkeypatch.setattr(C.cb, "active_daemon_mode", lambda: False)
    out = C.save_touched_docs()
    assert out["skipped"] == [] and out["errors"] == []
    assert out["warnings"][0]["reason"] == "missing_unverified"


def test_out_of_order_touch_reports_keep_newest_revisions(sweep, monkeypatch, tmp_path):
    path = tmp_path / "bracket.step+"
    path.write_text("revision five")
    row = {"doc_id": "old", "path": str(path), "dirty": False, "revision": 5,
           "metadata_revision": 2, "saved_revision": 5, "saved_metadata_revision": 2,
           "file_stamp": C._file_stamp(str(path))}
    fake = sweep([row], touched=[])
    C.note_touched_doc("old", row={**row, "dirty": True, "revision": 6,
                                  "metadata_revision": 3})
    C.note_touched_doc("old", row=row)  # a delayed result from an older concurrent call
    fake.docs = []
    monkeypatch.setattr(C.cb, "active_daemon_mode", lambda: False)
    out = C.save_touched_docs()
    assert out["skipped"] == [] and out["errors"] == []
    assert out["warnings"][0]["reason"] == "missing_unverified"


def test_sweep_does_not_retire_a_newer_mutation(sweep, monkeypatch, tmp_path):
    row = {"doc_id": "old", "name": "bracket", "dirty": True,
           "revision": 5, "metadata_revision": 2}
    sweep([row], touched=["old"])
    def save(_doc, _name):
        C.note_touched_doc("old", row={**row, "revision": 6})
        return str(tmp_path / "bracket.step+")
    monkeypatch.setattr(C, "_project_save", save)
    C.save_touched_docs()
    assert C._TOUCHED_DOCS[C._touch_scope()]["old"]["revision"] == 6


def test_document_retired_during_sweep_uses_its_recovery_snapshot(sweep, monkeypatch):
    row = {"doc_id": "old", "name": "bracket", "dirty": True,
           "revision": 5, "metadata_revision": 2}
    fake = sweep([row], touched=["old"])
    engine_call = fake.call
    entry = {"id": "snapshot", "doc_id": "old", "same_session": True,
             "revision": 5, "metadata_revision": 2}
    monkeypatch.setattr(C.cb, "active_daemon_mode", lambda: True)
    monkeypatch.setattr(C.cb, "call", lambda method, params=None:
                        {"entries": [entry]} if method == "recovery_list"
                        else engine_call(method, params))
    def disappear(_doc, _name):
        fake.docs = []
        raise C.CoreError("document is gone", code="document_not_found")
    monkeypatch.setattr(C, "_project_save", disappear)
    out = C.save_touched_docs()
    assert out["errors"] == [] and out["warnings"] == []
    assert out["skipped"][0]["reason"] == "preserved_in_recovery"


def test_a_doc_list_failure_is_reported_not_raised(sweep):
    """The sweep runs in a `finally` on the crash path. Raising there would replace the
    real failure with this one."""
    sweep([], touched=["d1"])

    def _boom(method, params=None):
        raise RuntimeError("daemon gone")

    C.cb.call = _boom  # monkeypatched attribute; the fixture restores it
    out = C.save_touched_docs()

    assert out["saved"] == []
    assert out["errors"] == []
    assert out["warnings"][0]["reason"] == "doc_list_unavailable"
    assert "daemon gone" in out["warnings"][0]["error"]


def test_the_box_mirror_is_started_never_awaited(sweep, monkeypatch, tmp_path):
    """A cloud upload must never sit between the model's last word and the turn's
    return. It used to: the sweep drained the Box spool with `block=True,
    budget_s=90`, so with a backlogged spool every chat message stayed at
    "Thinking…" for 90 s after the agent had finished (measured 5.5 s turn +
    90.2 s drain). The persistent services worker keeps a daemon drain alive, and
    worker_main drains what is left at shutdown."""
    existing = tmp_path / "existing.step+"
    existing.write_text("saved")
    sweep([{"doc_id": "d1", "name": "part", "dirty": False,
            "path": str(existing)}], touched=["d1"])
    from services.plm import box_mirror

    calls: list = []
    monkeypatch.setattr(box_mirror, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(box_mirror, "status", lambda *a, **k: {"pending": 7})
    monkeypatch.setattr(
        box_mirror,
        "drain",
        lambda **kw: calls.append(kw) or {"ok": True, "started": True},
    )

    out = C.save_touched_docs()

    assert calls == [{"block": False}]
    assert out["box"] == {"ok": True, "started": True}


def test_note_touched_doc_keeps_build_order_and_dedupes(monkeypatch):
    monkeypatch.setattr(C, "_TOUCHED_DOCS", {})
    C.note_touched_doc("d2")
    C.note_touched_doc("d1")
    C.note_touched_doc("d2")
    C.note_touched_doc(None)
    C.note_touched_doc("")

    assert list(C._TOUCHED_DOCS[C._touch_scope()]) == ["d2", "d1"]


def test_agent_admin_turn_rejects_explicit_invalid_workspace_before_provider(
    monkeypatch,
    tmp_path,
):
    """Bad durable-delivery plumbing is rejected before a completion can spend tokens."""
    from services.agent import config as agent_config
    from services.agent import worker_rpc

    provider_calls = []
    monkeypatch.setattr(
        agent_config,
        "get_provider",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    out = worker_rpc._agent_admin_turn(
        {
            "request": "build a bracket",
            "workspace_dir": str(tmp_path / "does-not-exist"),
        },
        lambda data: None,
    )

    assert out["ok"] is False
    assert out["reason"].startswith("invalid_workspace:")
    assert provider_calls == []


def test_crash_save_runs_and_surfaces_all_finalization_faults(monkeypatch):
    """A usage-sink reset fault must not jump over the crash-path save. The original
    turn crash, reset fault, and persistence failure all remain visible to the caller."""
    from services.agent import worker_rpc
    from services.agent import config as agent_config
    from services.agent.providers import token_usage

    monkeypatch.setattr(agent_config, "load_config", lambda: {})

    monkeypatch.setattr(
        worker_rpc,
        "omp_agent_turn",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("turn crashed")),
    )

    monkeypatch.setattr(
        token_usage,
        "reset_usage_sink",
        lambda token: (_ for _ in ()).throw(RuntimeError("reset crashed")),
    )
    sweeps = []
    persistence = {
        "saved": [],
        "skipped": [],
        "errors": ["d1: doc_save wrote nothing"],
        "persistence_failure": None,
    }
    monkeypatch.setattr(
        C,
        "save_touched_docs",
        lambda **kwargs: sweeps.append("save") or persistence,
    )

    # Select a model explicitly: Auto's live catalog/auth discovery is unrelated
    # to finalization and must not prevent this test from reaching the turn crash.
    out = worker_rpc._agent_admin_turn(
        {"request": "build it", "provider": "anthropic", "model": "claude-sonnet-4-6",
         "mode": "agent"},
        lambda data: None,
    )

    assert sweeps == ["save"]
    assert out["ok"] is False
    assert out["persistence"] == persistence
    assert "turn crashed" in out["reason"]
    assert "reset crashed" in out["reason"]
    assert "doc_save wrote nothing" in out["reason"]


def test_a_discarded_doc_the_daemon_kept_alive_is_not_swept_over_its_file(
        sweep, monkeypatch, tmp_path):
    """Live 2026-09-24: the agent rebuilt Wheel End in a fresh doc, saved it over the file,
    then close_part(discard=True) on the stale doc — hostd only DETACHED it (the run still
    held it) and the terminal sweep wrote the stale state back over the rebuilt file."""
    target = tmp_path / "CAD" / "Wheels" / "Wheel End.step+"
    target.parent.mkdir(parents=True)
    target.write_text("rebuilt")
    fake = sweep(
        [
            {"doc_id": "doc-stale", "name": "Wheel End", "kind": "assembly",
             "component_count": 21, "revision": 9, "dirty": True,
             "path": str(target), "origin_kind": "mcp"},
            {"doc_id": "doc-rebuilt", "name": "Wheel End", "kind": "assembly",
             "component_count": 25, "revision": 4, "dirty": False,
             "path": str(target), "origin_kind": "mcp"},
        ],
        touched=["doc-stale", "doc-rebuilt"],
    )
    engine_call = fake.call

    def call(method, params=None):
        if method in ("doc_close", "doc_discard"):
            return {"detached": True, "note": "an agent run still owns this doc"}
        return engine_call(method, params)

    monkeypatch.setattr(C.cb, "call", call)
    from services.agent.agent_turn import _bind_tool

    closed = _bind_tool("close_part")(doc_id="doc-stale", discard=True)
    assert '"detached": true' in closed

    out = C.save_touched_docs()

    assert fake.saves == []
    assert target.read_text() == "rebuilt"
    assert not any(row["doc_id"] == "doc-stale" for row in out["saved"])


def test_sweeps_are_isolated_by_turn_and_workspace(sweep, tmp_path):
    from services.agent.request_context import bind_roots

    project_a, project_b = tmp_path / "a", tmp_path / "b"
    docs = [{"doc_id": did, "name": did, "dirty": True, "path": "",
             "feature_count": 1, "origin_kind": "gui"} for did in ("a", "b", "c")]
    fake = sweep(docs, touched=[], project_dir=project_a)
    with bind_roots(str(project_a)), C.agent_doc_context("agent", scope="turn-a"):
        C.note_touched_doc("a")
    with bind_roots(str(project_a)), C.agent_doc_context("agent", scope="turn-b"):
        C.note_touched_doc("b")
    with bind_roots(str(project_b)), C.agent_doc_context("agent", scope="turn-a"):
        C.note_touched_doc("c")
    with bind_roots(str(project_a)):
        out = C.save_touched_docs(run_id="turn-a")
    assert [row["doc_id"] for row in out["saved"]] == ["a"]
    assert fake.saves == [("a", str(project_a / "a.step+"))]
    with bind_roots(str(project_b)):
        out = C.save_touched_docs(run_id="turn-a")
    assert [row["doc_id"] for row in out["saved"]] == ["c"]
    with bind_roots(str(project_a)):
        out = C.save_touched_docs(run_id="turn-b")
    assert [row["doc_id"] for row in out["saved"]] == ["b"]


def test_same_save_root_does_not_merge_distinct_workspaces(monkeypatch, tmp_path):
    from services.agent.request_context import bind_roots

    monkeypatch.setattr(C, "_TOUCHED_DOCS", {})
    shared = str(tmp_path / "shared")
    with bind_roots(str(tmp_path / "workspace-a"), shared), C.agent_doc_context("a", scope="run"):
        C.note_touched_doc("a")
        scope_a = C._touch_scope()
    with bind_roots(str(tmp_path / "workspace-b"), shared), C.agent_doc_context("b", scope="run"):
        C.note_touched_doc("b")
        scope_b = C._touch_scope()
    assert scope_a != scope_b
    assert list(C._TOUCHED_DOCS[scope_a]) == ["a"]
    assert list(C._TOUCHED_DOCS[scope_b]) == ["b"]


def test_bridge_records_all_revision_and_metadata_advances_once(monkeypatch, tmp_path):
    from services.agent.request_context import bind_roots
    from services.mcp import core_bridge

    monkeypatch.setattr(C, "_TOUCHED_DOCS", {})
    monkeypatch.setattr(core_bridge, "active_daemon_mode", lambda: False)
    row = {"doc_id": "d1", "revision": 0, "metadata_revision": 0}

    def dispatch(method, params=None):
        if method == "doc_list":
            return {"docs": [dict(row)]}
        if method == "doc_set_meta":
            row["metadata_revision"] += 1
            return {"ok": True}
        if method in {"doc_apply_ops", "config_add", "sim_study_add"}:
            row["revision"] += 1
            return {"ok": True, "revision": row["revision"]}
        return {"ok": False}

    monkeypatch.setattr(core_bridge, "_dispatch_call", dispatch)
    with bind_roots(str(tmp_path)), C.agent_doc_context("agent", scope="run-1"):
        for method in ("doc_apply_ops", "config_add", "sim_study_add", "doc_set_meta"):
            core_bridge.call(method, {"doc_id": "d1"})
        before = C._TOUCHED_DOCS[C._touch_scope()]["d1"]["sequence"]
        core_bridge.call("doc_apply_ops", {"doc_id": "d1"})
        assert C._TOUCHED_DOCS[C._touch_scope()]["d1"]["sequence"] > before
        before = C._TOUCHED_DOCS[C._touch_scope()]["d1"]
        core_bridge.call("unknown_failure", {"doc_id": "d1"})
        assert C._TOUCHED_DOCS[C._touch_scope()]["d1"] == before


@pytest.mark.parametrize("preserve_path", [False, True])
def test_bridge_records_only_real_saves_for_touched_revisions(
        monkeypatch, tmp_path, preserve_path):
    from services.agent.request_context import bind_roots
    from services.mcp import core_bridge

    monkeypatch.setattr(C, "_TOUCHED_DOCS", {})
    monkeypatch.setattr(core_bridge, "active_daemon_mode", lambda: False)
    path = tmp_path / "bracket.step+"
    path.write_text("old")
    row = {"doc_id": "d1", "name": "bracket", "path": str(path), "dirty": False,
           "revision": 0, "metadata_revision": 0}
    docs = [row]

    def dispatch(method, params=None):
        if method == "doc_list":
            return {"docs": [dict(doc) for doc in docs]}
        if method == "doc_apply_ops":
            row.update(revision=1, metadata_revision=1, dirty=True)
            return {"ok": True, "revision": 1}
        if method == "doc_save":
            path.write_text("snapshot" if preserve_path else "saved")
            if not preserve_path:
                row.update(dirty=False, saved_revision=1, saved_metadata_revision=1,
                           file_stamp=C._file_stamp(str(path)))
            return {"ok": True, "path": str(path), "saved_revision": 1,
                    "metadata_revision": 1}
        return {"ok": False}

    monkeypatch.setattr(core_bridge, "_dispatch_call", dispatch)
    with bind_roots(str(tmp_path)), C.agent_doc_context("agent", scope="run-1"):
        core_bridge.call("doc_apply_ops", {"doc_id": "d1"})
        core_bridge.call("doc_save", {"doc_id": "d1", "path": str(path),
                                      "preserve_path": preserve_path})
        docs.clear()
        out = C.save_touched_docs(run_id="run-1")
    if preserve_path:
        assert out["skipped"] == []
        assert out["warnings"][0]["reason"] == "missing_unverified"
    else:
        assert out["warnings"] == []
        assert out["skipped"][0]["reason"] == "already_saved"


def test_read_lease_upgrades_for_later_mutation(monkeypatch):
    from services.mcp import core_bridge, daemon_bridge

    class Connection:
        pass

    connection = Connection()
    calls = []
    monkeypatch.setattr(C, "_TOUCHED_DOCS", {})
    monkeypatch.setattr(core_bridge, "active_daemon_mode", lambda: True)
    monkeypatch.setattr(daemon_bridge, "client", lambda: connection)

    def dispatch(method, params=None):
        calls.append((method, dict(params or {})))
        if method == "doc_list":
            return {"docs": [{"doc_id": "d1", "revision": 0, "metadata_revision": 0}]}
        if method == "attach_doc":
            return {"ok": True, "mode": params["mode"]}
        return {"ok": True}

    monkeypatch.setattr(core_bridge, "_dispatch_call", dispatch)
    with C.agent_doc_context("agent", scope="run"):
        core_bridge.lease_document("d1")
        core_bridge.lease_document("d1")
        core_bridge.call("doc_apply_ops", {"doc_id": "d1"})
    assert [(m, p["mode"]) for m, p in calls if m == "attach_doc"] == [
        ("attach_doc", "ro"), ("attach_doc", "rw")]


@pytest.mark.parametrize("tracked", [True, False])
def test_read_lease_upgrades_before_guardrail_checkpoint_and_mutation(monkeypatch, tracked):
    from services.agent import guardrails
    from services.mcp import core_bridge, daemon_bridge

    class Connection:
        pass

    class Ledger:
        def __init__(self):
            self.events = []

        def append(self, actor, kind, payload):
            self.events.append((kind, payload))

    connection = Connection()
    ledger = Ledger()
    calls = []
    mode = None if tracked else "ro"
    checkpointed = False
    monkeypatch.setattr(C, "_TOUCHED_DOCS", {})
    monkeypatch.setattr(core_bridge, "active_daemon_mode", lambda: True)
    monkeypatch.setattr(daemon_bridge, "client", lambda: connection)

    def dispatch(method, params=None):
        nonlocal mode, checkpointed
        calls.append(method)
        if method == "attach_doc":
            mode = params["mode"]
            return {"ok": True, "mode": mode}
        if method == "doc_list":
            return {"docs": [{"doc_id": "d1", "revision": 0}]}
        if method == "doc_checkpoint":
            assert mode == "rw"
            checkpointed = True
            return {"checkpoint": "before"}
        if method == "doc_apply_ops":
            assert mode == "rw" and checkpointed
            return {"ok": True}
        raise AssertionError(method)

    monkeypatch.setattr(core_bridge, "_dispatch_call", dispatch)
    args = {"doc_id": "d1", "ops": [{"feature": {"type": "shell"}}]}
    with C.agent_doc_context("agent", scope="run"):
        if tracked:
            core_bridge.lease_document("d1")
        verdict = guardrails.pre_dispatch_checkpoint(
            "add_features", args, ledger=ledger,
            cfg={"hard_guardrails": {"auto_checkpoint": True}},
        )
        assert verdict is None
        assert core_bridge.call("doc_apply_ops", args)["ok"]
    assert calls.index("doc_checkpoint") < calls.index("doc_apply_ops")
    assert calls.count("attach_doc") == (2 if tracked else 1)
    assert any(kind == "auto_checkpoint" and payload["checkpoint"] == "before"
               for kind, payload in ledger.events)
    assert not any(kind == "hard_guardrail" and payload["outcome"] == "blocked"
                   for kind, payload in ledger.events)


def test_workspace_and_part_reads_lease_returned_documents(monkeypatch):
    from services.agent.agent_turn import _bind_tool
    from services.mcp import workspace_tools

    rows = [{"doc_id": "d1", "name": "part", "origin_kind": "gui"}]
    leased = []

    def engine(method, params=None):
        if method == "doc_list":
            return {"docs": rows}
        if method == "doc_tree":
            return {"doc_id": "d1", "revision": 0, "tree": {"features": []}}
        return {}

    monkeypatch.setattr(C.cb, "call", engine)
    monkeypatch.setattr(C.cb, "lease_document", leased.append)
    with C.agent_doc_context("agent", scope="turn"):
        assert json.loads(_bind_tool("get_part")(doc_id="d1"))["ok"]
        assert json.loads(_bind_tool("list_parts")())["docs"] == rows
        assert workspace_tools._turn_snapshot("now")["ok"]
    assert leased == ["d1", "d1", "d1"]


def test_workspace_snapshot_does_not_return_unleased_handles(monkeypatch):
    from services.mcp import workspace_tools

    monkeypatch.setattr(C.cb, "call", lambda *args: {"docs": [{"doc_id": "gone"}]})
    monkeypatch.setattr(C.cb, "lease_documents",
                        lambda rows: (_ for _ in ()).throw(RuntimeError("document closed")))
    result = workspace_tools._turn_snapshot("now")
    assert result["ok"] is False
    assert result["reason"] == "document_lease_failed"
    assert "docs" not in result
