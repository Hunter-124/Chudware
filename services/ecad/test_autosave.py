"""ECAD crash-recovery autosave tests (pytest-collectable, self-contained).

Covers services.ecad.autosave and its native-session facade hooks. Snapshots provide
best-effort recovery after a server restart. These tests exercise:
  * autosave is DISABLED by default (unit tests / the engine never touch disk uninvited);
  * once enabled, every mutating session op persists a session-namespaced snapshot;
  * a simulated server restart (new session id + cleared registry) RECOVERS the prior docs;
  * scope="recent" recovers only the most-recent prior session, "all" recovers everything;
  * closing a session retains its snapshot unless a replacement is verified.

Each test points CHUDWARE_ECAD_AUTOSAVE_DIR at a fresh temp dir so nothing touches %APPDATA%.
Run:  python -m pytest services/ecad/test_autosave.py -q
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import tempfile
import pytest

from services.ecad import autosave, session
from services.ecad.library.starter import SYMBOLS


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)


# --------------------------------------------------------------------------- fixtures / helpers


def _fresh_dir() -> str:
    """A clean autosave dir for one test + reset all autosave/session global state."""
    d = tempfile.mkdtemp(prefix="ecad-autosave-test-")
    os.environ["CHUDWARE_ECAD_AUTOSAVE_DIR"] = d
    autosave.disable()
    autosave._STATE["session_id"] = None
    session._REGISTRY.clear()
    return d


def _files(d: str) -> list:
    return sorted(p.name for p in pathlib.Path(d).glob("*.json"))


def _seed_divider(name: str = "Divider") -> session.EcadSession:
    """A 1-resistor schematic session built through the public session/op layer."""
    s = session.new_session("sch", name)
    for libid in ("Simulation_SPICE:VDC", "Device:R"):
        s.doc["lib_cache"][libid] = copy.deepcopy(SYMBOLS[libid])
    session.apply_ops(s, s.revision, [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [0.0, 0.0],
         "ref": "R1", "value": "10k"}])
    return s


# --------------------------------------------------------------------------- tests


def test_disabled_by_default() -> None:
    d = _fresh_dir()
    assert not autosave.is_enabled()
    s = session.new_session("sch", "Quiet")
    revision = s.revision
    session.apply_ops(s, revision, [])
    assert s.revision == revision
    session.rebuild(s)
    assert s.revision == revision
    assert _files(d) == [], "autosave wrote to disk while DISABLED"


def test_autosave_after_each_mutation() -> None:
    d = _fresh_dir()
    autosave.enable("SESS-A")
    s = _seed_divider()
    f = autosave._STATE["snapshots"][s.doc_id]
    assert f.exists(), "no KiCad autosave file after a mutating op"
    payload = json.loads(pathlib.Path(str(f) + ".recovery.json").read_text(encoding="utf-8"))
    assert payload["doc_id"] == s.doc_id
    assert payload["kind"] == "sch"
    assert payload["session"] == "SESS-A"
    assert payload["revision"] == s.revision
    assert "(kicad_sch" in f.read_text(encoding="utf-8")
    assert "R1" in f.read_text(encoding="utf-8")


def test_recover_round_trip() -> None:
    d = _fresh_dir()
    autosave.enable("SESS-A")
    s = _seed_divider()
    original_id = s.doc_id

    # simulate a server RESTART: fresh process session id + an empty in-memory registry.
    autosave.enable("SESS-B")
    session._REGISTRY.clear()
    recovered = autosave.recover_ecad_autosaved(scope="recent")

    assert len(recovered) == 1, recovered
    rec = recovered[0]
    assert rec["kind"] == "sch"
    assert rec["original_doc_id"] == original_id
    live = session.get_session(rec["doc_id"])  # back in the live registry under a fresh id
    assert any(sy.get("ref") == "R1" for sy in live.doc["symbols"])


def test_recover_scope_recent_vs_all() -> None:
    d = _fresh_dir()
    # prior session 1
    autosave.enable("SESS-1")
    _seed_divider("Old")
    # prior session 2 (more recent)
    autosave.enable("SESS-2")
    _seed_divider("New")

    # restart
    autosave.enable("SESS-3")
    session._REGISTRY.clear()
    recent = autosave.recover_ecad_autosaved(scope="recent")
    assert {r["name"] for r in recent} == {"New"}, recent

    session._REGISTRY.clear()
    every = autosave.recover_ecad_autosaved(scope="all")
    assert {r["name"] for r in every} == {"Old", "New"}, every


def test_close_preserves_autosave() -> None:
    d = _fresh_dir()
    autosave.enable("SESS-A")
    s = _seed_divider()
    f = autosave._STATE["snapshots"][s.doc_id]
    assert f.exists()
    assert session.close_session(s.doc_id) is True
    assert f.exists(), "closing must not discard the only recovery copy"


def test_board_round_trips_too() -> None:
    d = _fresh_dir()
    autosave.enable("SESS-A")
    s = session.new_session("pcb", "Board1")
    bid = s.doc_id
    autosave.enable("SESS-B")
    session._REGISTRY.clear()
    recovered = autosave.recover_ecad_autosaved(scope="recent")
    assert any(r["kind"] == "pcb" and r["original_doc_id"] == bid for r in recovered), recovered


def test_failed_migration_keeps_source(monkeypatch) -> None:
    d = _fresh_dir()
    autosave.enable("prior")
    _seed_divider()
    source = next(pathlib.Path(d).glob("*.json"))
    autosave.enable("new")
    session._REGISTRY.clear()
    monkeypatch.setattr(autosave, "autosave_session", lambda _session: False)
    assert autosave.recover_ecad_autosaved()
    assert source.exists()


def test_same_doc_id_is_namespaced_by_session() -> None:
    _fresh_dir()
    autosave.enable("first")
    doc = _seed_divider()
    first = autosave._STATE["snapshots"][doc.doc_id]
    autosave.enable("second")
    assert autosave.autosave_session(doc)
    second = autosave._STATE["snapshots"][doc.doc_id]
    assert first != second and first.exists() and second.exists()


def test_kicad_recovery_preserves_original_binding_and_file() -> None:
    from services.mcp.ecad_common import _rpc

    directory = pathlib.Path(_fresh_dir())
    original = directory / "original.kicad_sch"
    snapshot = directory / "snapshot.kicad_sch"
    first = session.new_session("sch", "Recovery", str(original))
    session.apply_ops(first, first.revision, [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [0, 0],
         "ref": "R1", "value": "10k"}])
    before = original.read_bytes()
    saved = _rpc("ecad_save", {"doc_id": first.doc_id, "path": str(snapshot),
                                "recovery": {"path": str(original), "pid": 1,
                                             "recovery_dir": str(directory)}})
    assert saved["saved_revision"] == first.revision
    metadata = json.loads(pathlib.Path(str(snapshot) + ".recovery.json").read_text())
    assert metadata["human_dirty"] and metadata["path"] == str(original)
    assert "R1" in snapshot.read_text()
    assert original.read_bytes() == before
    recovered = _rpc("ecad_open", {"path": str(snapshot)})
    assert recovered["path"] == str(original)
    assert any(symbol.get("ref") == "R1" for symbol in recovered["tree"]["symbols"])
