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

These pin the scope rules that make it safe — the user's own documents are never touched,
and no file is written for a document that holds nothing.

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
        monkeypatch.setattr(C, "_TOUCHED_DOCS", list(touched))
        if project_dir is None:
            monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
        else:
            monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(project_dir))
        return fake

    return _install


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


def test_a_touched_doc_that_the_gui_adopted_is_still_left_alone(sweep):
    """Adoption transfers ownership: once origin is gui the user's save decisions win,
    even for a document this worker created earlier in the run."""
    fake = sweep(
        [
            {
                "doc_id": "d1",
                "name": "holster",
                "kind": "part",
                "feature_count": 5,
                "dirty": True,
                "path": "",
                "origin_kind": "gui",
            }
        ],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []


def test_an_empty_doc_never_becomes_a_file(sweep):
    """hostd purges empty docs instead of retiring them, so there is nothing to rescue —
    and a stub .step+ in the user's project folder is exactly the clutter being fixed."""
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

    assert fake.saves == []
    assert out["saved"] == []


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


def test_a_pristine_import_of_an_existing_file_is_not_written_to_the_project(
    sweep, tmp_path
):
    """An agent's body-roster probe (`import_part(..., keep_bodies=[...])`) was written
    into the project root as `_frame624_2026-09-07.step+` beside the STEP it came from
    (2026-09-24). A doc that is only an unmodified import of a file still on disk is
    reproducible scratch; hostd closes it at GC without a recovery row for the same
    reason. Authored content after the import, or a vanished source, is real work."""
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

    assert [s[0] for s in fake.saves] == ["d-placed", "d-orphaned"]
    assert out["skipped"] == [
        {"doc_id": "d-probe", "name": "_frame624_2026-09-07",
         "reason": "reproducible_import", "source_paths": [str(source)]}
    ]
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
    }])
    out = C.save_touched_docs()
    assert out["saved"][0]["doc_id"] == "d-recovered"
    assert fake.saves == [("d-recovered", str(
        tmp_path / "Projects" / "Testing" / "base.step+"))]
    assert source.read_text() == "recovery snapshot"



def test_an_agent_origin_doc_this_process_never_touched_is_still_swept(sweep):
    """A doc left by an earlier worker is absent from this process's registry.
    Agent attribution still covers it when no other writer holds it, so abandoned
    unsaved docs do not get stranded."""
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

    assert [s[0] for s in fake.saves] == ["d-earlier"]


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

    assert saved == ["d-unclaimed", "d-touched"]
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


def test_a_doc_list_failure_is_reported_not_raised(sweep):
    """The sweep runs in a `finally` on the crash path. Raising there would replace the
    real failure with this one."""
    sweep([], touched=["d1"])

    def _boom(method, params=None):
        raise RuntimeError("daemon gone")

    C.cb.call = _boom  # monkeypatched attribute; the fixture restores it
    out = C.save_touched_docs()

    assert out["saved"] == []
    assert out["errors"] and "daemon gone" in out["errors"][0]


def test_the_box_mirror_is_started_never_awaited(sweep, monkeypatch):
    """A cloud upload must never sit between the model's last word and the turn's
    return. It used to: the sweep drained the Box spool with `block=True,
    budget_s=90`, so with a backlogged spool every chat message stayed at
    "Thinking…" for 90 s after the agent had finished (measured 5.5 s turn +
    90.2 s drain). The persistent services worker keeps a daemon drain alive, and
    worker_main drains what is left at shutdown."""
    sweep([], touched=[])
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
    monkeypatch.setattr(C, "_TOUCHED_DOCS", [])
    C.note_touched_doc("d2")
    C.note_touched_doc("d1")
    C.note_touched_doc("d2")
    C.note_touched_doc(None)
    C.note_touched_doc("")

    assert C._TOUCHED_DOCS == ["d2", "d1"]


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
        "chudcode_agent_turn",
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
        lambda: sweeps.append("save") or persistence,
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
        if method == "doc_close":
            return {"detached": True, "note": "an agent run still owns this doc"}
        return engine_call(method, params)

    monkeypatch.setattr(C.cb, "call", call)
    monkeypatch.setattr(C, "_DISCARDED_DOCS", {})
    from services.agent.agent_turn import _bind_tool

    closed = _bind_tool("close_part")(doc_id="doc-stale", discard=True)
    assert '"detached": true' in closed

    out = C.save_touched_docs()

    assert fake.saves == []
    assert target.read_text() == "rebuilt"
    assert {"doc_id": "doc-stale", "name": "Wheel End", "reason": "discarded"} in out["skipped"]
