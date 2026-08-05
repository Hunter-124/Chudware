"""Nothing the agent built is left as a "part potentially lost".

hostd retires an abandoned document to its crash-recovery ORPHAN tier when
``path.empty() || dirty || isAutosavePath(path)`` (core/hostd/server.cpp::gcCloseDoc), and
every orphan is offered back through the GUI's "Recover unsaved parts?" prompt. Nothing
closes the documents an agent run builds — T-RF5 deleted
``services/agent/loop.py::_terminal_autosave_close_docs`` and the C++ mission path only
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

import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_common as C  # noqa: E402


class _FakeEngine:
    """doc_list from a fixed roster; doc_save records the call and writes the file."""

    def __init__(self, docs):
        self.docs = [dict(d) for d in docs]
        self.saves: list = []

    def call(self, method, params=None):
        p = params or {}
        if method == "doc_list":
            return {"docs": [dict(e) for e in self.docs]}
        if method == "doc_save":
            self.saves.append((p.get("doc_id"), p.get("path")))
            dest = Path(str(p.get("path")))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("ISO-10303-21;\n")  # a real file, so the size check passes
            return {"ok": True, "path": str(dest)}
        return {}


@pytest.fixture()
def sweep(monkeypatch, tmp_path):
    """Install a fake engine + project save dir, and reset the touched-doc registry."""
    save_dir = tmp_path / "Projects" / "Testing"

    def _install(docs, *, touched=(), project_dir=save_dir):
        fake = _FakeEngine(docs)
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
        [{"doc_id": "doc-cpp00000002", "name": "ruger_lcp_iwb_holster", "kind": "part",
          "feature_count": 6, "revision": 3, "dirty": True, "path": "",
          "origin_kind": "mcp"}],
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
        [{"doc_id": "d1", "name": "bracket", "kind": "part", "feature_count": 4,
          "dirty": False, "path": str(existing), "origin_kind": "mcp"}],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []
    assert out["skipped"] == [{"doc_id": "d1", "name": "bracket",
                               "reason": "already_saved"}]


def test_the_users_own_document_is_never_touched(sweep):
    """A dirty unsaved GUI document is exactly the work the recovery prompt exists to
    protect. Saving it over a guessed filename would be data loss dressed as tidying."""
    fake = sweep(
        [{"doc_id": "d-gui", "name": "Part1", "kind": "part", "feature_count": 9,
          "dirty": True, "path": "", "origin_kind": "gui"}],
        touched=[],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []


def test_a_touched_doc_that_the_gui_adopted_is_still_left_alone(sweep):
    """Adoption transfers ownership: once origin is gui the user's save decisions win,
    even for a document this worker created earlier in the run."""
    fake = sweep(
        [{"doc_id": "d1", "name": "holster", "kind": "part", "feature_count": 5,
          "dirty": True, "path": "", "origin_kind": "gui"}],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []


def test_an_empty_doc_never_becomes_a_file(sweep):
    """hostd purges empty docs instead of retiring them, so there is nothing to rescue —
    and a stub .step+ in the user's project folder is exactly the clutter being fixed."""
    fake = sweep(
        [{"doc_id": "d1", "name": "holster", "kind": "part", "feature_count": 0,
          "empty": True, "dirty": False, "path": "", "origin_kind": "mcp"}],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["saved"] == []


def test_a_crash_autosave_path_does_not_count_as_saved(sweep, tmp_path):
    """isAutosavePath() makes hostd retire it anyway: the appdata snapshot is crash
    recovery, not a file the user can find."""
    snap = C._autosave_dir() / "sess-holster.step+"
    fake = sweep(
        [{"doc_id": "d1", "name": "holster", "kind": "part", "feature_count": 5,
          "dirty": False, "path": str(snap), "origin_kind": "mcp"}],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d1"]
    assert Path(out["saved"][0]["path"]).parent == tmp_path / "Projects" / "Testing"


def test_an_assembly_is_swept_on_its_component_count(sweep):
    """An assembly's content is components, not features — reading only feature_count
    would silently skip every assembly the agent built."""
    fake = sweep(
        [{"doc_id": "d1", "name": "motor_mount", "kind": "assembly",
          "feature_count": 0, "component_count": 4, "dirty": True, "path": "",
          "origin_kind": "mcp"}],
        touched=["d1"],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d1"]
    assert out["saved"][0]["name"] == "motor_mount"


def test_an_agent_origin_doc_this_process_never_touched_is_still_swept(sweep):
    """The worker is forked per turn, so a doc built by an earlier turn's worker is not
    in this process's registry. origin_kind is the engine's own attribution and covers
    it — without this the very docs that flooded the prompt would be missed."""
    fake = sweep(
        [{"doc_id": "d-earlier", "name": "holster", "kind": "part", "feature_count": 6,
          "dirty": True, "path": "", "origin_kind": "agent"}],
        touched=[],
    )
    out = C.save_touched_docs()

    assert [s[0] for s in fake.saves] == ["d-earlier"]


def test_no_project_folder_means_no_op(sweep):
    """With no project dir there is nowhere a human would find the file, so the recovery
    tier stays the honest safety net rather than writing into a temp dir nobody opens."""
    fake = sweep(
        [{"doc_id": "d1", "name": "holster", "kind": "part", "feature_count": 6,
          "dirty": True, "path": "", "origin_kind": "mcp"}],
        touched=["d1"],
        project_dir=None,
    )
    out = C.save_touched_docs()

    assert fake.saves == []
    assert out["skipped"] == [{"reason": "no_project_save_dir"}]


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


def test_note_touched_doc_keeps_build_order_and_dedupes(monkeypatch):
    monkeypatch.setattr(C, "_TOUCHED_DOCS", [])
    C.note_touched_doc("d2")
    C.note_touched_doc("d1")
    C.note_touched_doc("d2")
    C.note_touched_doc(None)
    C.note_touched_doc("")

    assert C._TOUCHED_DOCS == ["d2", "d1"]
