"""ECAD crash-recovery autosave tests (pytest-collectable, self-contained).

Covers services.ecad.autosave + its session-layer hooks: a heavy MCP tool that overruns the client's
transport timeout gets the server killed, and without this every open schematic/board is lost. These
tests prove that:
  * autosave is DISABLED by default (unit tests / the engine never touch disk uninvited);
  * once enabled, every mutating session op persists the doc to <doc_id>.json;
  * a simulated server restart (new session id + cleared registry) RECOVERS the prior docs;
  * scope="recent" recovers only the most-recent prior session, "all" recovers everything;
  * a deliberately closed session's autosave is removed (no resurrection).

Each test points CHUDWARE_ECAD_AUTOSAVE_DIR at a fresh temp dir so nothing touches %APPDATA%.
Run:  python -m pytest engine/ecad/test_autosave.py -q
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import tempfile

from services.ecad import autosave, schema, session
from services.ecad.library.starter import SYMBOLS


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
    for libid in ("Simulation_SPICE:V_source", "Device:R"):
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
    session.apply_ops(s, s.revision, [])  # a no-op mutation still bumps revision
    assert _files(d) == [], "autosave wrote to disk while DISABLED"


def test_autosave_after_each_mutation() -> None:
    d = _fresh_dir()
    autosave.enable("SESS-A")
    s = _seed_divider()
    f = pathlib.Path(d) / f"{s.doc_id}.json"
    assert f.exists(), "no autosave file after a mutating op"
    payload = json.loads(f.read_text(encoding="utf-8"))
    assert payload["doc_id"] == s.doc_id
    assert payload["kind"] == "sch"
    assert payload["session"] == "SESS-A"
    assert payload["revision"] == s.revision
    assert any(sy.get("ref") == "R1" for sy in payload["doc"]["symbols"])
    # the persisted doc is itself a valid ECAD document
    assert schema.validate(payload["doc"]) == []


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


def test_close_removes_autosave() -> None:
    d = _fresh_dir()
    autosave.enable("SESS-A")
    s = _seed_divider()
    f = pathlib.Path(d) / f"{s.doc_id}.json"
    assert f.exists()
    assert session.close_session(s.doc_id) is True
    assert not f.exists(), "closing a session must remove its autosave (no resurrection)"


def test_board_round_trips_too() -> None:
    d = _fresh_dir()
    autosave.enable("SESS-A")
    s = session.new_session("pcb", "Board1")
    bid = s.doc_id
    autosave.enable("SESS-B")
    session._REGISTRY.clear()
    recovered = autosave.recover_ecad_autosaved(scope="recent")
    assert any(r["kind"] == "pcb" and r["original_doc_id"] == bid for r in recovered), recovered
