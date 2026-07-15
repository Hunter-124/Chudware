"""Lane S — mission-terminal autosave + close of AI-minted geometry docs.

Axes:
  SV1 — geometry docs saved to run artifacts + workspace (nonzero files + manifest)
  SV2 — docs closed/detached after save; GUI-origin (origin_kind=gui) exempt
  SV3 — save-then-close ordering; mega suite gate for every future run
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


class _MemLedger:
    def __init__(self, path: Path | None = None):
        self.events: list[dict] = []
        self.path = path

    def append(self, actor, kind, payload):
        ev = {"actor": actor, "kind": kind, "payload": payload}
        self.events.append(ev)
        return ev

    def log_stage(self, *a, **k):
        return None

    def features(self):
        return []

    def geometry_specs(self):
        return []


def _ledger_with_mech_part(doc_id: str = "doc-cpp00000042", name: str = "rocket_plane_airframe"):
    led = _MemLedger()
    led.append("feature_modeler", "note", {
        "event": "tool_call",
        "tool": "new_part",
        "args": {"name": name},
        "ok": True,
        "result": {"ok": True, "doc_id": doc_id, "name": name},
    })
    led.append("feature_modeler", "note", {
        "event": "tool_call",
        "tool": "add_features",
        "args": {"doc_id": doc_id, "ops": [{"op": "add_feature"}]},
        "ok": True,
        "result": {"ok": True, "doc_id": doc_id, "body_count": 1},
    })
    return led


class _FakeEngine:
    """In-memory engine: doc_list / eval_mass / doc_tree / doc_save / doc_close."""

    def __init__(self, docs: list[dict] | None = None):
        self.docs = {d["doc_id"]: dict(d) for d in (docs or [])}
        self.calls: list[tuple[str, dict]] = []
        self.closed: list[str] = []
        self.saved: list[tuple[str, str]] = []  # (doc_id, path)

    def __call__(self, method: str, params: dict | None = None):
        params = params or {}
        self.calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": list(self.docs.values())}
        if method == "eval_mass":
            did = params.get("doc_id")
            d = self.docs.get(did) or {}
            bc = int(d.get("body_count") or 0)
            if bc <= 0:
                raise RuntimeError("no bodies")
            return {"body_count": bc, "volume_mm3": 1000.0 * bc}
        if method == "doc_tree":
            did = params.get("doc_id")
            d = self.docs.get(did) or {}
            return {
                "doc_id": did,
                "name": d.get("name"),
                "schema": d.get("schema") or "aicad.part/1",
                "body_count": d.get("body_count") or 0,
                "features": d.get("features") or ([{"id": "f1"}] if d.get("body_count") else []),
            }
        if method == "doc_save":
            did = params.get("doc_id")
            path = params.get("path")
            assert did in self.docs or True  # allow save of ledger-only ids
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Nonzero native payload
            p.write_bytes(b"ISO-10303-21;/* step+ mock */" + did.encode())
            self.saved.append((did, str(p)))
            return {"ok": True, "path": str(p)}
        if method == "doc_close":
            did = params.get("doc_id")
            self.closed.append(did)
            self.docs.pop(did, None)
            return {"ok": True, "closed": True, "doc_id": did}
        raise AssertionError(f"unexpected method {method}")


def test_workspace_dir_default(tmp_path, monkeypatch):
    from services.agent import config as agent_config

    # Empty config + no real Documents override → falls through to Billet or AI-CAD.
    monkeypatch.setattr(agent_config, "load_config", lambda: {})
    # Force HOME to tmp so we don't touch the real Documents tree.
    monkeypatch.setenv("HOME", str(tmp_path))
    # No Documents/AI-CAD yet → Documents/Billet
    d = agent_config.workspace_dir({})
    assert d == tmp_path / "Documents" / "Billet"
    assert d.is_dir()

    # Legacy AI-CAD wins when present.
    legacy = tmp_path / "Documents" / "AI-CAD"
    legacy.mkdir(parents=True, exist_ok=True)
    d2 = agent_config.workspace_dir({})
    assert d2 == legacy

    # Explicit config wins.
    custom = tmp_path / "ws_custom"
    d3 = agent_config.workspace_dir({"workspace_dir": str(custom)})
    assert d3 == custom
    assert custom.is_dir()


def test_save_then_close_ordering(tmp_path, monkeypatch):
    """SV1+SV2+SV3: geometry doc is saved (artifact+workspace) then closed."""
    from services.agent import loop

    run_id = "test-autosave-s1"
    art = tmp_path / "reports" / "runs" / run_id
    art.mkdir(parents=True)
    ws = tmp_path / "workspace"
    ws.mkdir()

    doc_id = "doc-cpp00000042"
    eng = _FakeEngine(docs=[{
        "doc_id": doc_id,
        "name": "rocket_plane_airframe",
        "origin_kind": "mcp",
        "body_count": 1,
        "revision": 3,
    }])
    led = _ledger_with_mech_part(doc_id)

    report = loop._terminal_autosave_close_docs(
        run_id=run_id,
        ledger=led,
        status="done",
        stop_reason="deliver_partial",
        call_fn=eng,
        workspace=ws,
        artifact_dir=art,
    )

    assert report["saved"], report
    assert report["closed"], report
    assert report["closed"][0]["doc_id"] == doc_id
    assert report["closed"][0]["closed"] is True

    # Ordering: every doc_save for this doc precedes its doc_close.
    methods = [m for m, p in eng.calls if p.get("doc_id") == doc_id or m == "doc_list"]
    save_idxs = [i for i, (m, p) in enumerate(eng.calls)
                 if m == "doc_save" and p.get("doc_id") == doc_id]
    close_idxs = [i for i, (m, p) in enumerate(eng.calls)
                  if m == "doc_close" and p.get("doc_id") == doc_id]
    assert save_idxs, eng.calls
    assert close_idxs, eng.calls
    assert max(save_idxs) < min(close_idxs), (save_idxs, close_idxs, eng.calls)

    # SV1: files exist with nonzero size under parts/ and workspace.
    parts = list((art / "parts").glob("*.step+"))
    assert parts, list((art / "parts").iterdir()) if (art / "parts").exists() else "no parts dir"
    assert all(p.stat().st_size > 0 for p in parts)
    ws_parts = list(ws.glob("*.step+"))
    assert ws_parts
    assert all(p.stat().st_size > 0 for p in ws_parts)

    # Manifest lists them.
    man_path = Path(report["manifest_path"])
    assert man_path.is_file()
    man = json.loads(man_path.read_text(encoding="utf-8"))
    assert man["schema"] == "aicad.saved_parts/1"
    assert man["saved"]
    assert man["saved"][0]["doc_id"] == doc_id
    assert man["closed"]
    # doc_list empty of mission doc after close
    assert doc_id not in eng.docs
    assert doc_id in eng.closed


def test_gui_origin_exempt_from_close(tmp_path):
    """SV2: origin_kind=gui docs are never saved/closed by terminal teardown."""
    from services.agent import loop

    run_id = "test-autosave-gui"
    art = tmp_path / "run"
    art.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    gui_id = "doc-cpp00000099"
    agent_id = "doc-cpp00000042"
    eng = _FakeEngine(docs=[
        {
            "doc_id": gui_id,
            "name": "user_part",
            "origin_kind": "gui",
            "body_count": 2,
        },
        {
            "doc_id": agent_id,
            "name": "rocket_plane_airframe",
            "origin_kind": "mcp",
            "body_count": 1,
        },
    ])
    # Ledger only tracks the agent part; gui appears only via doc_list.
    led = _ledger_with_mech_part(agent_id)

    report = loop._terminal_autosave_close_docs(
        run_id=run_id,
        ledger=led,
        status="done",
        stop_reason="mission_complete",
        call_fn=eng,
        workspace=ws,
        artifact_dir=art,
    )

    skipped_ids = {s["doc_id"] for s in report["skipped"]}
    assert gui_id in skipped_ids or gui_id not in {s["doc_id"] for s in report["saved"]}
    # Explicit: gui never closed
    assert gui_id not in eng.closed
    assert gui_id in eng.docs  # still live
    # Agent doc was closed
    assert agent_id in eng.closed
    assert agent_id not in eng.docs
    # No save calls for gui
    gui_saves = [p for did, p in eng.saved if did == gui_id]
    assert not gui_saves, gui_saves


def test_no_geometry_skipped(tmp_path):
    """Empty docs (no body, no mutator) are skipped — no save/close."""
    from services.agent import loop

    run_id = "test-autosave-empty"
    art = tmp_path / "run"
    art.mkdir()
    empty_id = "doc-cpp00000001"
    eng = _FakeEngine(docs=[{
        "doc_id": empty_id,
        "name": "hollow",
        "origin_kind": "mcp",
        "body_count": 0,
        "features": [],
    }])
    led = _MemLedger()
    led.append("feature_modeler", "note", {
        "event": "tool_call",
        "tool": "new_part",
        "args": {"name": "hollow"},
        "ok": True,
        "result": {"ok": True, "doc_id": empty_id, "name": "hollow"},
    })
    # No add_features → no mutator, no geometry.

    report = loop._terminal_autosave_close_docs(
        run_id=run_id,
        ledger=led,
        status="done",
        stop_reason="deliver_partial",
        call_fn=eng,
        workspace=tmp_path / "ws",
        artifact_dir=art,
    )
    assert not report["saved"], report
    assert not report["closed"], report
    assert empty_id not in eng.closed
    assert any(s.get("reason") == "no_geometry" for s in report["skipped"])


def test_board_counts_as_geometry(tmp_path):
    """ECAD board (schema aicad.pcb) is saved even without solid body_count."""
    from services.agent import loop

    run_id = "test-autosave-board"
    art = tmp_path / "run"
    art.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    board_id = "doc-board-abc"
    eng = _FakeEngine(docs=[{
        "doc_id": board_id,
        "name": "fc_board",
        "origin_kind": "mcp",
        "body_count": 0,
        "schema": "aicad.pcb/1",
    }])
    # Override eval_mass to fail (no solids) — board path uses doc_tree schema.
    real = eng.__call__

    def _call(method, params=None):
        if method == "eval_mass":
            raise RuntimeError("no solids on board")
        if method == "doc_tree":
            return {
                "doc_id": board_id,
                "schema": "aicad.pcb/1",
                "name": "fc_board",
                "body_count": 0,
            }
        return real(method, params)

    led = _MemLedger()
    led.append("ecad_engineer", "note", {
        "event": "tool_call",
        "tool": "board_from_schematic",
        "args": {},
        "ok": True,
        "result": {"ok": True, "board_doc_id": board_id, "doc_id": board_id},
    })

    report = loop._terminal_autosave_close_docs(
        run_id=run_id,
        ledger=led,
        status="done",
        stop_reason="mission_complete",
        call_fn=_call,
        workspace=ws,
        artifact_dir=art,
    )
    assert report["saved"], report
    assert report["closed"]
    assert board_id in eng.closed


def test_drive_phases_invokes_terminal_autosave(monkeypatch, tmp_path):
    """Mission terminal path in _drive_phases_from_dag calls autosave teardown."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.agent import loop
    from services.agent.pipeline.phases import Phase, PhaseMachine
    from services.dag.mission_template import build_mission_graph
    from services.agent.mission_criteria import apply_rocket_plane_checks

    called: list[dict] = []

    def _fake_autosave(**kwargs):
        called.append(kwargs)
        return {"ok": True, "saved": [], "closed": [], "skipped": []}

    monkeypatch.setattr(loop, "_terminal_autosave_close_docs", _fake_autosave)
    # Silence grading backends.
    monkeypatch.setattr(
        "services.agent.reviewer_exec.run_verify_battery",
        lambda *a, **k: {
            "verdict": "NOT-CHECKED", "ok": False, "checks": [], "detail": "no doc",
        },
    )
    monkeypatch.setattr("services.agent.reviewer_exec._resolve_fn", lambda tool: None)

    base = {
        "request_verbatim": "flight computer",
        "ambiguities": [],
        "deliverables": [
            {"id": "DL1", "name": "Airframe", "kind": "part",
             "description": "a", "acceptance_criteria": [],
             "depends_on": [], "target_refs": {}, "status": "pending"},
            {"id": "DL5", "name": "Report", "kind": "report",
             "description": "e", "acceptance_criteria": [],
             "depends_on": [], "target_refs": {}, "status": "pending"},
        ],
        "assumptions": [],
    }
    manifest = apply_rocket_plane_checks(base)
    graph = build_mission_graph(manifest, graph_id="m_s_autosave", org_layer=False)
    node_states = {}
    for n in graph["nodes"]:
        nid = n["id"]
        if nid.startswith("n_deliver_"):
            node_states[nid] = {"status": "done", "output": {"status": "done"}}
        elif nid.startswith("n_build_") or nid.startswith("n_verify_"):
            node_states[nid] = {"status": "failed", "output": {"status": "failed"}}
        else:
            node_states[nid] = {"status": "done", "output": {"status": "done"}}
    dag_result = {"status": "paused", "nodes": node_states}
    led = _MemLedger(path=tmp_path / "ledger.json")

    machine = PhaseMachine()
    completed: list = []
    loop._drive_phases_from_dag(
        machine, manifest, graph, dag_result, completed, lambda *a, **k: None, Phase,
        ledger=led, run_id="mega-test-autosave",
    )
    assert called, "terminal autosave was not invoked"
    assert called[0].get("run_id") == "mega-test-autosave"
    assert called[0].get("status") in ("done", "blocked", "needs_correction", "failed")


def test_manifest_written_under_run_dir(tmp_path):
    """SV1: saved_parts_manifest.json lands in the run artifact dir."""
    from services.agent import loop

    run_id = "test-manifest"
    art = tmp_path / "reports" / "runs" / run_id
    art.mkdir(parents=True)
    doc_id = "doc-cpp00000007"
    eng = _FakeEngine(docs=[{
        "doc_id": doc_id,
        "name": "wing",
        "origin_kind": "agent",
        "body_count": 1,
    }])
    led = _ledger_with_mech_part(doc_id, name="wing")
    report = loop._terminal_autosave_close_docs(
        run_id=run_id,
        ledger=led,
        status="done",
        stop_reason="mission_complete",
        call_fn=eng,
        workspace=tmp_path / "ws",
        artifact_dir=art,
    )
    man = Path(report["manifest_path"])
    assert man.is_file()
    assert man.parent == art
    data = json.loads(man.read_text(encoding="utf-8"))
    assert data["run_id"] == run_id
    assert data["parts_dir"] == str(art / "parts")
    assert any(s["doc_id"] == doc_id for s in data["saved"])
