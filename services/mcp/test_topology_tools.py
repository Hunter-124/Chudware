"""Consumer behaviour of the topology model tools (services/mcp/topology_tools.py).

The engine is replaced by an in-memory DOCUMENT (studies, face points, STEP export, the
native capability matrix); everything this module owns runs for real: study
normalization and preservation, capability gating, project-jailed run directories, the
real core_rpc job-stage validation of the payload, the exactly-once durable collect over
an isolated job registry, artifact hashing, DFAM on a real STL and the RunRecord append.
No native solver is exercised here; its smoke belongs to the integration run.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.mcp import cad_common
from services.mcp import topology_tools as tt
from services.sim import jobs as J
from services.sim import topology as topo
from services.sim.topology_worker_rpc import WORKER_METHODS

LEFT = {"points": [[0.0, y, z] for y in (0.0, 10.0) for z in (0.0, 10.0)],
        "tol_mm": 0.5, "face_normal": [-1.0, 0.0, 0.0], "perp_tol_mm": 1.5}
RIGHT = {"points": [[40.0, y, z] for y in (0.0, 10.0) for z in (0.0, 10.0)],
         "tol_mm": 0.7, "face_normal": [1.0, 0.0, 0.0], "perp_tol_mm": 1.5}
BOTTOM = {"points": [[x, y, 0.0] for x in (0.0, 40.0) for y in (0.0, 10.0)],
          "tol_mm": 0.5, "face_normal": [0.0, 0.0, -1.0], "perp_tol_mm": 1.5}

CAPS = {
    "methods": [{"id": "simp"}, {"id": "beso"}],
    "physics": [{"id": "structural"}, {"id": "thermomechanical"}, {"id": "flow"}],
    "combinations": [
        {"method": "simp", "physics": "structural", "supported": True,
         "formulation": "SIMP p-penalized density", "volume_phase": "solid",
         "objectives": ["compliance", "mass"],
         "limits": ["stress_limit_mpa", "max_displacement_mm"],
         "parameters": ["volume_fraction", "max_iterations", "filter_radius_mm", "penalty",
                        "overhang_angle_deg", "build_direction", "min_member_mm"],
         "defaults": {"penalty": 3.0}},
        {"method": "simp", "physics": "thermomechanical", "supported": True,
         "objectives": ["compliance"], "limits": [], "parameters": ["volume_fraction"]},
        {"method": "beso", "physics": "flow", "supported": False,
         "reason": "BESO has no fluid formulation in this build"},
    ],
}


def _gui_pick(topo_id: int) -> dict:
    return {"kind": "face", "body": 0, "topo": topo_id,
            "_pick": {"body": 0, "topo": topo_id}}


def _gui_study() -> dict:
    """A study as the Simulation tab persists it (selectors carry viewport identity)."""
    return {
        "id": "study-00000001", "type": "topology", "name": "Bracket topology",
        "material": {"E_mpa": 68900.0, "nu": 0.33, "density_g_cm3": 2.7, "name": "Al 6061"},
        "mesh_order": 1, "mesh_target_mm": 3.0,
        "topology": {"physics": "structural", "method": "simp", "objective": "compliance",
                     "volume_fraction": 0.4, "filter_radius_mm": 4.0, "max_iterations": 60},
        "fixtures": [{"faces": [_gui_pick(1)], "label": "wall"},
                     {"faces": ["bottom"], "dofs": [2]}],
        "loads": [{"faces": [_gui_pick(2)], "type": "force", "vec": [0.0, 0.0, -500.0],
                   "label": "tip"}],
        "preserve": [{"faces": ["+x"], "thickness_mm": 2.0}],
        "thermal": {"fixtures": [{"faces": ["-x"], "temperature_c": 20.0}]},
        "agent_notes": "keep the boss round",
        "runs": [{"run_id": "older", "settings": {}, "result": {"ok": True}}],
    }


class FakeDocument:
    """The engine's view of ONE open part: studies, faces, export, capability RPC."""

    def __init__(self):
        self.studies: list = []
        self.edits = 0
        self.caps = copy.deepcopy(CAPS)
        self.at_start: list = []
        self.calls: list = []
        self.queries = {"-x": [LEFT], "+x": [RIGHT], "bottom": [BOTTOM],
                        "ends": [LEFT, RIGHT]}

    def query(self, _doc_id, query):
        if isinstance(query, str) and query in self.queries:
            return copy.deepcopy(self.queries[query])
        raise ValueError(f"No face matched query {query!r} (try query_geometry to explore).")

    def call(self, method, params=None):
        params = params or {}
        self.calls.append(method)
        if method == "sim_study_list":
            return {"studies": copy.deepcopy(self.studies), "revision": self.edits}
        if method == "sim_study_add":
            sid = f"study-{len(self.studies) + 100:08x}"
            study = copy.deepcopy(params["study"])
            study["id"] = sid
            self.studies.append(study)
            return {"study_id": sid, "studies": copy.deepcopy(self.studies)}
        if method == "sim_study_edit":
            for i, study in enumerate(self.studies):
                if study.get("id") == params["study_id"]:
                    self.studies[i] = {**copy.deepcopy(params["study"]),
                                       "id": params["study_id"]}
                    self.edits += 1
                    return {"studies": copy.deepcopy(self.studies)}
            raise RuntimeError(f"invalid_op: no study {params['study_id']}")
        if method == "doc_export_step":
            Path(params["path"]).write_text("ISO-10303-21;\nEND-ISO-10303-21;\n",
                                            encoding="utf-8")
            return {"ok": True, "path": params["path"]}
        if method == "doc_face_points":
            return copy.deepcopy({1: LEFT, 2: RIGHT}[params["selector"]["topo"]])
        if method == "topology_capabilities":
            if self.caps is None:
                raise RuntimeError("unknown method: topology_capabilities")
            return copy.deepcopy(self.caps)
        raise AssertionError(f"unexpected engine call {method}")

    def stored(self, sid):
        return next(s for s in self.studies if s["id"] == sid)


class _Capture:
    def __init__(self):
        self.tools = {}

    def tool(self, *_args, **_kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def h(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    spool = tmp_path / "spool"
    for var in topo.PROJECT_ROOT_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(project))
    monkeypatch.setenv("CHUDWARE_SIM_JOBS_DIR", str(spool))
    J._LOCAL_PROCS.clear()
    doc = FakeDocument()
    monkeypatch.setattr(tt, "cb", doc)
    monkeypatch.setattr(tt, "_doc", lambda d: d or "doc-1")
    monkeypatch.setattr(tt, "_require_sim_geometry", lambda d: [{"bbox": {}}])
    monkeypatch.setattr(tt, "_sim_face_points", doc.query)
    # The only engine touch of job validation: "is the solver compiled in".
    monkeypatch.setattr(J, "_probe_backend", lambda method: None)
    starts: list = []

    def start(kind, stage, payload, meta=None):
        J._validate_start(kind, stage, payload, meta)  # the real stage validation
        doc.at_start.append(copy.deepcopy(doc.studies))
        starts.append({"kind": kind, "stage": stage, "payload": payload, "meta": meta})
        return {"job_id": f"sim-{len(starts):012x}", "state": "running", "pid": None,
                "kind": kind, "stage": stage, "started_at": time.time()}

    monkeypatch.setattr(J, "start", start)
    capture = _Capture()
    tt.register_topology_tools(capture)
    run = capture.tools["run_topology_optimization"]
    return SimpleNamespace(doc=doc, starts=starts, project=project, spool=spool,
                           run=lambda **kw: json.loads(run(**kw)),
                           caps=lambda: json.loads(capture.tools["topology_capabilities"]()))


def _cube_stl(path: Path, s: float = 20.0) -> None:
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    lines = ["solid cube"]
    for a, b, c in tris:
        lines += ["facet normal 0 0 0", "outer loop",
                  *(f"vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)),
                  "endloop", "endfacet"]
    lines.append("endsolid cube")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _native_result(run_dir: Path, **over) -> dict:
    stl = run_dir / "surface.stl"
    _cube_stl(stl)
    result = {
        "ok": True, "converged": True, "feasible": True, "termination": "converged",
        "method": "simp", "physics": "structural", "formulation": "SIMP p-penalized density",
        "summary": {"objective": "compliance", "objective_baseline": 10.0,
                    "objective_final": 6.0, "volume_fraction_final": 0.4, "iterations": 42,
                    "max_constraint_violation": 0.0, "convergence_criterion": "change<0.01"},
        "history": {"iteration": [1, 2, 3], "objective": [10.0, 7.0, 6.0]},
        "constraints": [{"name": "solid_volume_fraction", "value": 0.4, "limit": 0.4,
                         "violation": 0.0, "satisfied": True}],
        "manufacturing": {"checks": [], "geometry_valid": True,
                          "dfam": {"checked": False, "passed": False}, "print_ready": True},
        "provenance": {"engine_build_id": "test-build"},
        "mesh_quality": {"elements": 1000},
        "artifacts": {"surface": {"path": str(stl), "format": "stl",
                                  "sha256": topo.sha256_file(stl)}},
        "density": [0.25] * 1000,
        "fields": {"von_mises_mpa": [1.0] * 10},
    }
    result.update(over)
    return result


def _finish(meta: dict, raw: dict, job_id: str) -> None:
    """A finished child: spool result.json + a registry row whose process is gone."""
    d = J.job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps({"result": raw, "done_at": time.time()}),
                                   encoding="utf-8")
    now = time.time()
    with J._db() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,pgid,proc_key,created_at,"
            "started_at,cancel_requested,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "topology", "core_rpc", J.RUNNING, 999999, 999999, "gone", now, now,
             0, json.dumps(meta)))


# ---------------------------------------------------------------- submit


def test_gui_study_is_persisted_before_start_and_nothing_user_authored_is_lost(h):
    h.doc.studies.append(_gui_study())
    out = h.run(study_id="study-00000001")
    assert out["ok"] is True, out
    assert len(h.starts) == 1
    started = h.starts[0]

    at_start = next(s for s in h.doc.at_start[0] if s["id"] == "study-00000001")
    assert at_start["type"] == "topology"
    assert at_start["last_job"]["state"] == "submitting"
    assert at_start["last_job"]["run_id"] == out["run_id"]

    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["state"] == "running"
    assert stored["last_job"]["job_id"] == out["job_id"]
    assert stored["fixtures"][0]["faces"][0]["_pick"] == {"body": 0, "topo": 1}
    assert stored["fixtures"][1]["dofs"] == [2]
    assert stored["agent_notes"] == "keep the boss round"
    assert stored["runs"] == [{"run_id": "older", "settings": {}, "result": {"ok": True}}]
    assert stored["thermal"] == {"fixtures": [{"faces": ["-x"], "temperature_c": 20.0}]}
    assert stored["loads"][0]["vec"] == [0.0, 0.0, -500.0]

    env = started["payload"]["params"]["study"]
    assert env["fixtures"][0]["faces"]["points"] == LEFT["points"]
    assert env["fixtures"][0]["faces"]["face_normal"] == [-1.0, 0.0, 0.0]
    assert env["fixtures"][0]["label"] == "wall"
    assert env["fixtures"][1]["dofs"] == [2]
    assert env["loads"][0]["vector"] == [0.0, 0.0, -500.0] and "vec" not in env["loads"][0]
    assert env["preserve"][0]["thickness_mm"] == 2.0
    assert "thermal" not in env, "structural physics must not send a thermal section"
    assert set(env["material"]) <= set(topo.MATERIAL_KEYS) | {"name"}
    assert env["mesh_order"] == 1 and env["target_size_mm"] == 3.0

    run_dir = Path(out["artifact_dir"])
    assert topo.contained(run_dir, h.project) and not topo.contained(run_dir, h.spool)
    assert env["topology"]["result_dir"] == str(run_dir)
    assert Path(env["step_path"]) == run_dir / "source.step"
    assert (run_dir / "inputs.json").is_file() and (run_dir / "setup.json").is_file()
    assert started["meta"]["inputs"]["sha256"] == topo.sha256_file(run_dir / "inputs.json")
    assert str(run_dir / "source.step") in out["paths"]


def test_overlay_merges_topology_keys_and_new_study_reuses_its_name(h):
    h.doc.studies.append(_gui_study())
    out = h.run(study_id="study-00000001", study={"topology": {"volume_fraction": 0.3}})
    assert out["ok"] is True, out
    stored = h.doc.stored("study-00000001")
    assert stored["topology"]["volume_fraction"] == 0.3
    assert stored["topology"]["filter_radius_mm"] == 4.0
    assert stored["agent_notes"] == "keep the boss round"

    fresh = {k: v for k, v in _gui_study().items() if k not in ("id", "runs")}
    first = h.run(study=fresh)
    second = h.run(study=fresh)
    assert first["study_id"] == second["study_id"] == "study-00000001"
    assert len([s for s in h.doc.studies if s["type"] == "topology"]) == 1


def test_unsupported_combination_and_unoffered_key_refuse_before_any_write(h):
    study = {k: v for k, v in _gui_study().items() if k not in ("id",)}
    study["topology"] = {"physics": "flow", "method": "beso", "volume_fraction": 0.5}
    out = h.run(study=study)
    assert out["ok"] is False and out["code"] == "unsupported_combination"

    study["topology"] = {"physics": "structural", "method": "simp", "volume_fraction": 0.5,
                         "max_pressure_drop_pa": 50.0}
    out = h.run(study=study)
    assert out["code"] == "unsupported_combination" and "max_pressure_drop_pa" in out["error"]

    study["topology"] = {"physics": "structural", "method": "simp", "objective": "dissipation",
                         "volume_fraction": 0.5}
    assert h.run(study=study)["field"] == "topology.objective"
    assert h.starts == [] and h.doc.studies == []
    assert not (h.project / "simulation").exists()


def test_failures_name_the_missing_selection_material_or_geometry(h, monkeypatch):
    base = {k: v for k, v in _gui_study().items() if k not in ("id",)}
    no_fix = {**base, "fixtures": []}
    out = h.run(study=no_fix)
    assert (out["code"], out["field"]) == ("missing_selection", "fixtures")

    unloaded = {**base, "loads": [{"faces": ["+x"], "type": "force", "vec": [0, 0, 0]}]}
    assert h.run(study=unloaded)["field"] == "loads"

    bad_pick = {**base, "loads": [{"faces": ["the shiny face"], "type": "pressure",
                                   "magnitude": 1.0}]}
    out = h.run(study=bad_pick)
    assert out["code"] == "missing_selection" and "the shiny face" in out["error"]

    thermo = {**base, "material": {"E_mpa": 68900.0, "nu": 0.33},
              "topology": {"physics": "thermomechanical", "method": "simp",
                           "volume_fraction": 0.4}}
    out = h.run(study=thermo)
    assert out["code"] == "missing_material"
    assert "k_w_mk" in out["error"] and "alpha_per_k" in out["error"]

    per_axis_typo = {**base, "fixtures": [{"faces": ["bottom"], "dofz": [2]}]}
    out = h.run(study=per_axis_typo)
    assert out["code"] == "invalid_input" and "dofz" in out["error"]

    monkeypatch.setattr(tt, "_require_sim_geometry",
                        lambda d: (_ for _ in ()).throw(ValueError("no solid body")))
    assert h.run(study=base)["code"] == "missing_geometry"
    assert h.starts == []


def test_result_dir_cannot_escape_the_project(h, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (h.project / "link").symlink_to(outside, target_is_directory=True)
    base = {k: v for k, v in _gui_study().items() if k not in ("id",)}
    for bad in ("../outside", str(outside), "link/runs", str(h.spool)):
        study = copy.deepcopy(base)
        study["topology"]["result_dir"] = bad
        out = h.run(study=study)
        assert out["ok"] is False and out["code"] == "path_outside_project", (bad, out)
    assert list(outside.iterdir()) == []
    assert h.starts == []

    study = copy.deepcopy(base)
    study["topology"]["result_dir"] = "results/topo"
    out = h.run(study=study)
    assert out["ok"] is True, out
    assert Path(out["artifact_dir"]).parent == (h.project / "results" / "topo").resolve()
    assert h.starts[0]["payload"]["params"]["study"]["topology"]["result_dir"] == \
        out["artifact_dir"]


def test_unavailable_native_capabilities_are_reported_not_substituted(h):
    h.doc.caps = None
    caps = h.caps()
    assert caps["ok"] is False and caps["code"] == "capabilities_unavailable"
    assert h.run(study={k: v for k, v in _gui_study().items() if k != "id"})["code"] == \
        "capabilities_unavailable"
    h.doc.caps = copy.deepcopy(CAPS)
    caps = h.caps()
    assert caps["capabilities"] == CAPS
    assert caps["supported_pairs"] == ["simpxstructural", "simpxthermomechanical"]


def test_job_stage_refuses_non_topology_shapes(h, tmp_path):
    step = tmp_path / "d.step"
    step.write_text("ISO-10303-21;", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    fixture = {"faces": copy.deepcopy(LEFT)}
    load = {"faces": copy.deepcopy(RIGHT), "type": "force", "vector": [0, 0, -1]}
    good = {"step_path": str(step), "mesh_order": 1,
            "material": {"E_mpa": 1000.0, "nu": 0.3},
            "topology": {"physics": "structural", "method": "simp",
                         "volume_fraction": 0.5, "result_dir": str(run_dir)},
            "fixtures": [fixture], "loads": [load]}

    def payload(study, **extra):
        return {"method": "sim_run_topology", "params": {"study": study}, **extra}

    J._validate_start("topology", "core_rpc", payload(good), {})
    cases = [
        ({**good, "topology": {**good["topology"], "result_dir": "relative"}}, "result_dir"),
        ({**good, "thermal": {"fixtures": []}}, "thermal"),
        ({**good, "material": {"E_mpa": 1000.0, "nu": 0.3, "yield_mpa": 1.0,
                               "plasticity": {}}}, "non-physical"),
        ({**good, "mesh_order": 2}, "tet4"),
        ({k: v for k, v in good.items() if k != "topology"}, "topology"),
        ({**good, "fixtures": [{"faces": {"selector": "left"}}]}, "points"),
        ({**good, "loads": [{**load, "vector": [0, 0, 0]}]}, "non-zero"),
    ]
    for study, needle in cases:
        with pytest.raises(ValueError, match=needle):
            J._validate_start("topology", "core_rpc", payload(study), {})
    with pytest.raises(ValueError, match="open_step"):
        J._validate_start("topology", "core_rpc", payload(good, open_step=str(step)), {})


# ---------------------------------------------------------------- collect


def _submitted(h) -> tuple[dict, dict]:
    h.doc.studies.append(_gui_study())
    out = h.run(study_id="study-00000001")
    assert out["ok"] is True, out
    return out, h.starts[-1]["meta"]


def test_collect_runs_once_and_appends_one_compact_run(h):
    out, meta = _submitted(h)
    run_dir = Path(out["artifact_dir"])
    _finish(meta, _native_result(run_dir), out["job_id"])
    edits_before = h.doc.edits

    first = cad_common.collect_durable(out["job_id"])
    second = cad_common.collect_durable(out["job_id"])
    assert json.loads(json.dumps(first)) == second
    assert h.doc.edits == edits_before + 1, "collect must persist the run exactly once"
    assert first["ok"] is True and first["saved_to_study"] is True
    assert first["converged"] is True

    stored = h.doc.stored("study-00000001")
    assert [r["run_id"] for r in stored["runs"]] == ["older", out["run_id"]]
    record = stored["runs"][-1]
    assert record["settings"]["volume_fraction"] == 0.4
    assert "density" not in record["result"] and "fields" not in record["result"]
    assert stored["last_job"]["state"] == "collected"
    assert stored["agent_notes"] == "keep the boss round"

    arts = record["result"]["artifacts"]
    assert topo.sha256_file(arts["density"]["path"]) == arts["density"]["sha256"]
    assert json.loads(Path(arts["density"]["path"]).read_text())[:2] == [0.25, 0.25]
    assert arts["surface"]["sha256"] == topo.sha256_file(run_dir / "surface.stl")
    assert (run_dir / "result.json").is_file() and (run_dir / "provenance.json").is_file()
    prov = json.loads((run_dir / "provenance.json").read_text())
    assert prov["source_step"]["sha256"] == topo.sha256_file(run_dir / "source.step")
    assert str(run_dir / "result.json") in first["paths"]

    mfg = record["result"]["manufacturing"]
    assert mfg["dfam"]["checked"] is True
    assert mfg["print_ready"] is False, "collection never slices, so it never claims ready"

    # The spool is pruned; the run's evidence is not.
    import shutil
    shutil.rmtree(J.job_dir(out["job_id"]))
    assert (run_dir / "result.json").is_file()


def test_missing_stale_and_foreign_artifacts_are_flagged_not_hashed(h, tmp_path):
    out, meta = _submitted(h)
    run_dir = Path(out["artifact_dir"])
    foreign = tmp_path / "elsewhere.stl"
    _cube_stl(foreign)
    raw = _native_result(run_dir)
    raw["artifacts"]["surface"]["sha256"] = "0" * 64
    raw["artifacts"]["gone"] = {"path": str(run_dir / "never_written.3mf"), "format": "3mf"}
    raw["artifacts"]["foreign"] = {"path": str(foreign), "format": "stl"}
    _finish(meta, raw, out["job_id"])
    collected = cad_common.collect_durable(out["job_id"])
    arts = collected["result"]["artifacts"]
    assert arts["surface"]["stale"] is True
    assert arts["gone"]["missing"] is True
    assert arts["foreign"]["missing"] is True and arts["foreign"]["outside_run_dir"] is True
    assert "sha256" not in arts["foreign"]
    assert str(foreign) not in collected["paths"]
    # A stale surface is never DFAM-checked as if it were the solver's geometry.
    assert collected["result"]["manufacturing"]["dfam"]["checked"] is False
    assert collected["print_ready"] is False


def test_print_ready_is_never_claimed_without_validated_geometry(h):
    out, meta = _submitted(h)
    raw = _native_result(Path(out["artifact_dir"]),
                         manufacturing={"geometry_valid": False, "print_ready": True,
                                        "checks": []})
    _finish(meta, raw, out["job_id"])
    collected = cad_common.collect_durable(out["job_id"])
    mfg = collected["result"]["manufacturing"]
    assert mfg["print_ready"] is False and mfg["dfam"]["checked"] is False


def test_passing_dfam_on_valid_geometry_is_still_not_print_ready_without_slicing(
        h, monkeypatch):
    """Only slice_part's passing G-code check makes a part print-ready; collection
    never slices, so a finished run with validated geometry and an all-pass DFAM keeps
    that DFAM evidence and still reports print_ready false."""
    import services.dfam as dfam_pkg

    real = dfam_pkg.analyze_printability

    def raw_mesh_holes_measured(mesh, params, build_dir=(0, 0, 1), **kw):
        # A raw STL can never evaluate hole sizing; every other row is the real analysis.
        report = real(mesh, params, build_dir, **kw)
        for row in report["checks"]:
            if row["check"] == "small_holes":
                row["status"] = "pass"
        assert {r["status"] for r in report["checks"]} <= {"pass", "not_applicable"}
        report["status"] = "pass"
        return report

    monkeypatch.setattr(dfam_pkg, "analyze_printability", raw_mesh_holes_measured)
    out, meta = _submitted(h)
    _finish(meta, _native_result(Path(out["artifact_dir"])), out["job_id"])
    collected = cad_common.collect_durable(out["job_id"])
    assert collected["ok"] is True and collected["converged"] is True

    stored = h.doc.stored("study-00000001")["runs"][-1]["result"]["manufacturing"]
    for mfg in (collected["result"]["manufacturing"], stored):
        assert mfg["geometry_valid"] is True
        assert mfg["dfam"]["checked"] is True and mfg["dfam"]["passed"] is True
        assert mfg["print_ready"] is False
        assert "slice_part" in mfg["print_ready_reason"]
    assert collected["print_ready"] is False


def test_infeasible_run_is_a_completed_computation_not_a_convergence(h):
    out, meta = _submitted(h)
    raw = _native_result(Path(out["artifact_dir"]), converged=False, feasible=False,
                         termination="infeasible")
    _finish(meta, raw, out["job_id"])
    collected = cad_common.collect_durable(out["job_id"])
    assert collected["ok"] is True
    assert collected["converged"] is False and collected["feasible"] is False
    record = h.doc.stored("study-00000001")["runs"][-1]
    assert record["result"]["termination"] == "infeasible"


def test_late_collect_of_an_older_run_does_not_replace_the_latest(h):
    first, meta_a = _submitted(h)
    second = h.run(study_id="study-00000001")
    assert second["ok"] is True
    _finish(meta_a, _native_result(Path(first["artifact_dir"])), first["job_id"])
    collected = cad_common.collect_durable(first["job_id"])
    assert collected["superseded_by_run"] == second["run_id"]
    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["run_id"] == second["run_id"]
    assert stored["last_job"]["state"] == "running"
    assert first["run_id"] in [r["run_id"] for r in stored["runs"]]


def test_deleted_study_is_not_recreated_and_duplicate_records_are_not_appended(h):
    out, meta = _submitted(h)
    h.doc.studies.clear()
    _finish(meta, _native_result(Path(out["artifact_dir"])), out["job_id"])
    collected = cad_common.collect_durable(out["job_id"])
    assert collected["saved_to_study"] is False
    assert h.doc.studies == []
    assert (Path(out["artifact_dir"]) / "result.json").is_file()

    record = {"run_id": "r1", "job_id": "sim-x", "result": {"ok": True}}
    once, info1 = topo.apply_run_record({"name": "s", "runs": []}, record)
    twice, info2 = topo.apply_run_record(once, record)
    assert len(twice["runs"]) == 1 and info2["duplicate"] is True and not info1["duplicate"]


# ---------------------------------------------------------------- terminal lifecycle
# A failed or cancelled job reaches the SAME study exactly once, through the same
# get_job/wait_job snapshot and the synchronous handoff, with nothing invented.


def _died(meta: dict, job_id: str, *, cancel_requested: bool = False) -> None:
    """A worker that died (killed or crashed) before writing any result.json."""
    J.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    now = time.time()
    with J._db() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,pid,pgid,proc_key,created_at,"
            "started_at,cancel_requested,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "topology", "core_rpc", J.RUNNING, 999999, 999999, "gone", now, now,
             int(cancel_requested), json.dumps(meta)))


def _poll_concurrently(job_id: str, threads: int = 6, after: int = 3) -> list:
    snaps: list = []
    lock = threading.Lock()

    def poll():
        snap = cad_common.durable_job_snapshot(job_id)
        with lock:
            snaps.append(snap)

    workers = [threading.Thread(target=poll) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
    snaps.extend(cad_common.durable_job_snapshot(job_id) for _ in range(after))
    return snaps


def _native_child(run_dir: Path) -> Path:
    child = run_dir / "native-20261002T120000Z-structural-simp-0a1b2c3d"
    child.mkdir()
    return child


@pytest.mark.parametrize("state", [J.FAILED, J.CANCELLED])
def test_failed_or_cancelled_job_reaches_the_same_study_once(h, state):
    out, meta = _submitted(h)
    _died(meta, out["job_id"], cancel_requested=state == J.CANCELLED)
    edits_before = h.doc.edits

    snaps = _poll_concurrently(out["job_id"])
    assert all(s["status"] == "error" for s in snaps), snaps
    assert {s["code"] for s in snaps} == {f"solve_{state}"}
    details = [json.loads(json.dumps(s["detail"])) for s in snaps]
    assert all(d == details[0] for d in details)
    detail = details[0]
    assert detail["ok"] is False and detail["state"] == state
    assert detail["saved_to_study"] is True
    assert detail["converged"] is False and detail["feasible"] is None
    assert detail["print_ready"] is False
    assert h.doc.edits == edits_before + 1, "a terminal job persists exactly one update"

    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["state"] == state
    assert stored["last_job"]["run_id"] == out["run_id"]
    assert stored["last_job"]["job_id"] == out["job_id"]
    assert stored["agent_notes"] == "keep the boss round"
    assert [r["run_id"] for r in stored["runs"]] == ["older", out["run_id"]]
    result = stored["runs"][-1]["result"]
    assert result["ok"] is False and result["converged"] is False
    assert result["termination"] == ("cancelled" if state == J.CANCELLED else "failed")
    assert "feasible" not in result
    for invented in ("summary", "history", "density", "fields", "constraints", "mesh_quality"):
        assert invented not in result, invented
    assert result["manufacturing"]["print_ready"] is False
    assert result["manufacturing"]["dfam"]["checked"] is False
    for entry in result["artifacts"].values():
        assert entry.get("missing") or topo.sha256_file(entry["path"]) == entry["sha256"]
    assert (Path(out["artifact_dir"]) / "source.step").read_text().startswith("ISO-10303-21")


def test_native_failure_reply_is_failed_with_its_evidence_and_nothing_invented(h):
    out, meta = _submitted(h)
    run_dir = Path(out["artifact_dir"])
    child = _native_child(run_dir)
    (child / "run.json").write_text('{"schema": "chudware.topology.native_run/1"}')
    (child / "checkpoint.json").write_text('{"schema": "x"}')
    raw = {"ok": False, "converged": False, "feasible": True, "termination": "failed",
           "termination_detail": "physics evaluation failed at iteration 3",
           "error": "singular stiffness", "error_code": "physics_failed",
           "method": "simp", "physics": "structural", "result_dir": str(child),
           "summary": {"iterations": 3}, "history": {"iteration": [1, 2, 3]},
           "density": [0.5] * 8,
           "manufacturing": {"geometry_valid": True, "print_ready": True},
           "artifacts": {
               "run": {"path": str(child / "run.json"), "format": "json",
                       "sha256": topo.sha256_file(child / "run.json")},
               "checkpoint": {"path": str(child / "checkpoint.json"), "format": "json",
                              "sha256": topo.sha256_file(child / "checkpoint.json")},
               "density_vtu": {"path": str(child / "density.vtu"), "format": "vtu"}}}
    _finish(meta, raw, out["job_id"])
    assert J.status(out["job_id"])["state"] == J.FAILED

    snap = cad_common.durable_job_snapshot(out["job_id"])
    assert snap["status"] == "error" and snap["code"] == "solve_failed"
    detail = snap["detail"]
    assert detail["ok"] is False and detail["converged"] is False
    assert detail["feasible"] is None and detail["print_ready"] is False
    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["state"] == J.FAILED
    result = stored["runs"][-1]["result"]
    assert result["error_code"] == "physics_failed" and result["termination"] == "failed"
    assert result["summary"] == {"iterations": 3}
    arts = result["artifacts"]
    assert arts["checkpoint"]["sha256"] == topo.sha256_file(child / "checkpoint.json")
    assert arts["density_vtu"]["missing"] is True
    assert json.loads(Path(arts["density"]["path"]).read_text()) == [0.5] * 8
    assert result["manufacturing"]["print_ready"] is False
    assert result["manufacturing"]["dfam"]["checked"] is False


def test_killed_run_recovers_only_checkpoint_files_the_native_run_wrote(h):
    out, meta = _submitted(h)
    run_dir = Path(out["artifact_dir"])
    child = _native_child(run_dir)
    (child / "run.json").write_text(json.dumps(
        {"schema": "chudware.topology.native_run/1", "method": "simp",
         "physics": "structural"}))
    (child / "history.jsonl").write_text('{"iteration": 1}\n{"iteration": 2}\n{"iterat')
    (child / "checkpoint.json").write_text(json.dumps(
        {"schema": "chudware.topology.native_checkpoint/1", "method": "simp",
         "physics": "structural", "iteration": 2, "density": [0.4] * 8,
         "state": {"schema": "chudware.topology.optimizer_state/1"}}))
    (child / "checkpoint.json.tmp").write_text("{")
    _died(meta, out["job_id"], cancel_requested=True)

    snap = cad_common.durable_job_snapshot(out["job_id"])
    assert snap["code"] == "solve_cancelled"
    result = h.doc.stored("study-00000001")["runs"][-1]["result"]
    assert result["ok"] is False and result["termination"] == "cancelled"
    arts = result["artifacts"]
    for key, name in (("run", "run.json"), ("checkpoint", "checkpoint.json"),
                      ("history_log", "history.jsonl")):
        assert arts[key]["sha256"] == topo.sha256_file(child / name), key
    assert not any(e["path"].endswith(".tmp") for e in arts.values())
    recovered = result["recovered"]
    assert recovered["checkpoint_iteration"] == 2
    assert recovered["iterations_recorded"] == 2
    for invented in ("summary", "history", "density", "surface"):
        assert invented not in result and invented not in arts, invented


def test_late_terminal_job_does_not_replace_the_newer_current_job(h):
    first, meta_a = _submitted(h)
    second = h.run(study_id="study-00000001")
    _died(meta_a, first["job_id"])
    snap = cad_common.durable_job_snapshot(first["job_id"])
    assert snap["detail"]["superseded_by_run"] == second["run_id"]
    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["run_id"] == second["run_id"]
    assert stored["last_job"]["state"] == "running"
    retained = next(r for r in stored["runs"] if r["run_id"] == first["run_id"])
    assert retained["result"]["ok"] is False


@pytest.mark.parametrize("state", [J.DONE, J.FAILED])
def test_study_save_failure_is_explicit_and_retried_without_duplicates(h, monkeypatch, state):
    out, meta = _submitted(h)
    run_dir = Path(out["artifact_dir"])
    if state == J.DONE:
        _finish(meta, _native_result(run_dir), out["job_id"])
    else:
        _died(meta, out["job_id"])
    real_call = h.doc.call
    outage = {"left": 1}

    def flaky(method, params=None):
        if method == "sim_study_edit" and outage["left"]:
            outage["left"] -= 1
            raise RuntimeError("engine connection lost")
        return real_call(method, params)

    monkeypatch.setattr(tt.cb, "call", flaky)
    first = cad_common.collect_durable(out["job_id"])
    assert first["saved_to_study"] is False and first["retryable"] is True
    assert J.collected(out["job_id"]) is None
    assert not (J.job_dir(out["job_id"]) / "collect.claim").exists()
    assert h.doc.stored("study-00000001")["last_job"]["state"] == "running"

    second = cad_common.collect_durable(out["job_id"])
    assert second["saved_to_study"] is True and "retryable" not in second
    assert cad_common.collect_durable(out["job_id"]) == json.loads(json.dumps(second))
    stored = h.doc.stored("study-00000001")
    assert [r["run_id"] for r in stored["runs"]] == ["older", out["run_id"]]
    assert stored["last_job"]["state"] == ("collected" if state == J.DONE else state)
    names = sorted(p.name for p in run_dir.iterdir())
    assert not [n for n in names if ".1." in n], names


def test_deleted_study_is_not_resurrected_by_a_failed_job(h):
    out, meta = _submitted(h)
    h.doc.studies.clear()
    _died(meta, out["job_id"])
    snap = cad_common.durable_job_snapshot(out["job_id"])
    assert snap["detail"]["saved_to_study"] is False
    assert "retryable" not in snap["detail"]
    assert h.doc.studies == []
    assert J.collected(out["job_id"]) == json.loads(json.dumps(snap["detail"]))


def test_synchronous_failure_is_persisted_once_and_replayed(h, monkeypatch):
    h.doc.studies.append(_gui_study())
    fake_start = J.start

    def start_then_die(kind, stage, payload, meta=None):
        job = fake_start(kind, stage, payload, meta)
        _died(meta, job["job_id"])
        return job

    monkeypatch.setattr(J, "start", start_then_die)
    out = h.run(study_id="study-00000001", background=False)
    assert out["ok"] is False and out["state"] == J.FAILED
    assert out["saved_to_study"] is True
    stored = h.doc.stored("study-00000001")
    assert stored["last_job"]["state"] == J.FAILED
    assert stored["last_job"]["job_id"] == out["job_id"]
    edits = h.doc.edits
    snap = cad_common.durable_job_snapshot(out["job_id"])
    assert snap["detail"]["run_id"] == out["run_id"]
    assert h.doc.edits == edits
    assert [r["run_id"] for r in h.doc.stored("study-00000001")["runs"]] == \
        ["older", out["run_id"]]


def test_kinds_without_a_terminal_hook_keep_their_failure_snapshot(h, monkeypatch):
    calls: list = []
    monkeypatch.setitem(cad_common._DURABLE_COLLECTORS, "probe_kind",
                        lambda st, raw: calls.append(raw) or {"ok": True})
    job_id = "sim-0000000probe"
    J.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    now = time.time()
    with J._db() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id,kind,stage,state,created_at,started_at,error,meta)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (job_id, "probe_kind", "core_rpc", J.FAILED, now, now, "boom", "{}"))
    snap = cad_common.durable_job_snapshot(job_id)
    assert snap["status"] == "error" and snap["error"] == "boom"
    assert "detail" not in snap and "code" not in snap
    assert calls == [] and J.collected(job_id) is None
    assert not (J.job_dir(job_id) / "collect.claim").exists()


def test_faceset_merge_keeps_plane_hints_only_for_a_shared_normal():
    same = topo.merge_facesets([copy.deepcopy(LEFT), copy.deepcopy(LEFT)], "two")
    assert same["face_normal"] == [-1.0, 0.0, 0.0] and same["perp_tol_mm"] == 1.5
    mixed = topo.merge_facesets([copy.deepcopy(LEFT), copy.deepcopy(RIGHT)], "ends")
    assert "face_normal" not in mixed and "perp_tol_mm" not in mixed
    assert mixed["tol_mm"] == 0.7 and len(mixed["points"]) == 8
    assert mixed["selector"] == "ends"


def test_run_dirs_are_unique_and_never_reused(h):
    a = topo.allocate_run_dir("Same", "run-a", spool=h.spool)
    b = topo.allocate_run_dir("Same", "run-b", spool=h.spool)
    assert a != b and a.parent == b.parent
    with pytest.raises(FileExistsError):
        topo.allocate_run_dir("Same", "run-a", spool=h.spool)
    first = topo.write_json_new(a, "result.json", {"v": 1})
    second = topo.write_json_new(a, "result.json", {"v": 2})
    assert first["path"] != second["path"]
    assert json.loads(Path(first["path"]).read_text()) == {"v": 1}
    assert os.path.basename(second["path"]) == "result.1.json"


# ---------------------------------------------------------------- printing


def test_print_presets_are_shared_and_discoverable(h):
    processes = topo.print_processes()
    assert topo.print_processes() is processes
    assert [p["id"] for p in processes] == ["fdm_0.4", "fdm_0.6", "resin", "powder"]
    assert topo.print_process("powder")["topology"]["overhang_angle_deg"] is None
    caps = h.caps()
    assert caps["print_processes"] == [
        {k: p[k] for k in ("id", "label", "process", "description", "topology")}
        for p in processes]
    assert all("build_plate" in sections for sections in caps["boundaries"].values())


def test_print_preset_fills_persisted_offered_keys_and_explicit_values_win(h):
    h.doc.studies.append(_gui_study())
    out = h.run(study_id="study-00000001", study={
        "print_process": "fdm_0.4", "build_plate": {"faces": ["bottom"], "label": "print bed"},
        "topology": {"min_member_mm": 2.4}})
    assert out["ok"] is True, out
    saved = h.doc.stored(out["study_id"])
    assert saved["print_process"] == "fdm_0.4"
    assert saved["topology"]["min_member_mm"] == 2.4
    assert saved["topology"]["overhang_angle_deg"] == 45
    assert "overhang_penalty" not in saved["topology"], "not offered by this pair"
    assert "no_enclosed_voids" not in saved["topology"], "not offered by this pair"
    assert any("overhang_angle_deg=45" in note for note in out["notes"])
    assert not any("min_member_mm=" in note for note in out["notes"])
    envelope = h.starts[-1]["payload"]["params"]["study"]
    assert envelope["build_plate"]["label"] == "print bed"
    assert envelope["build_plate"]["faces"]["points"] == BOTTOM["points"]
    assert envelope["build_plate"]["faces"]["face_normal"] == [0.0, 0.0, -1.0]
    assert saved["build_plate"] == {"faces": ["bottom"], "label": "print bed"}


def test_print_preset_writes_all_and_only_offered_parameters(h):
    h.doc.caps["combinations"][0]["parameters"] += ["overhang_penalty", "no_enclosed_voids"]
    base = _gui_study()
    base.pop("id")
    base.update(print_process="fdm_0.6", build_plate={"faces": ["bottom"]})
    out = h.run(study=base)
    assert out["ok"] is True, out
    topology = h.doc.stored(out["study_id"])["topology"]
    for key, value in topo.print_process("fdm_0.6")["topology"].items():
        assert topology[key] == value
    assert any("no_enclosed_voids=false" in note for note in out["notes"])


@pytest.mark.parametrize("explicit", [False, True])
def test_powder_null_preset_keys_do_not_insert_or_erase_settings(h, explicit):
    h.doc.caps["combinations"][0]["parameters"] += ["overhang_penalty", "no_enclosed_voids"]
    base = _gui_study()
    base.pop("id")
    base["print_process"] = "powder"
    if explicit:
        base["topology"].update(overhang_angle_deg=60, overhang_penalty=0.25,
                                build_direction=[0, 0, 1])
    out = h.run(study=base)
    assert out["ok"] is True, out
    settings = h.doc.stored(out["study_id"])["topology"]
    if explicit:
        assert settings["overhang_angle_deg"] == 60
        assert settings["overhang_penalty"] == 0.25
    else:
        assert "overhang_angle_deg" not in settings
        assert "overhang_penalty" not in settings
    assert settings["no_enclosed_voids"] is True


def test_unknown_print_preset_refuses_before_save_or_export(h):
    base = {k: v for k, v in _gui_study().items() if k != "id"}
    out = h.run(study={**base, "print_process": "unknown"})
    assert out["ok"] is False and out["code"] == "unknown_print_process", out
    assert out["field"] == "study.print_process"
    assert all(p["id"] in out["error"] for p in topo.print_processes())
    assert not {"sim_study_add", "sim_study_edit", "doc_export_step"} & set(h.doc.calls)
    assert not h.starts and not (h.project / "simulation").exists()


@pytest.mark.parametrize(("changes", "code", "field"), [
    ({"volume_fraction": None}, "missing_volume_fraction", "volume_fraction"),
    ({"volume_fraction": 0}, "out_of_range", "volume_fraction"),
    ({"volume_fraction": -0.1}, "out_of_range", "volume_fraction"),
    ({"volume_fraction": 1.1}, "out_of_range", "volume_fraction"),
    ({"overhang_angle_deg": 45}, "build_direction_required", "build_direction"),
    ({"overhang_penalty": 1}, "build_direction_required", "build_direction"),
    ({"objective": "mass"}, "mass_needs_limit", "objective"),
])
@pytest.mark.parametrize("stored", [False, True])
def test_topology_intent_refuses_before_any_study_write_or_export(
        h, changes, code, field, stored):
    base = _gui_study()
    base["topology"].update(changes)
    if changes.get("volume_fraction", False) is None:
        base["topology"].pop("volume_fraction")
    h.doc.caps["combinations"][0]["parameters"].append("overhang_penalty")
    if stored:
        h.doc.studies.append(base)
        out = h.run(study_id=base["id"])
    else:
        base.pop("id")
        out = h.run(study=base)
    assert out["ok"] is False and out["code"] == code, out
    assert out["field"] == f"study.topology.{field}"
    if code == "build_direction_required":
        assert out["error"] == (
            "Overhang control needs a build direction or a bed face (build_plate).")
    assert not {"sim_study_add", "sim_study_edit", "doc_export_step"} & set(h.doc.calls)
    assert not h.starts and not (h.project / "simulation").exists()


def test_overhang_penalty_uses_native_45_degree_default_with_a_bed_face(h):
    h.doc.caps["combinations"][0]["parameters"].append("overhang_penalty")
    base = _gui_study()
    base.pop("id")
    base["build_plate"] = {"faces": ["bottom"]}
    base["topology"]["overhang_penalty"] = 1.0
    out = h.run(study=base)
    assert out["ok"] is True, out
    settings = h.starts[-1]["meta"]["settings"]
    assert "overhang_angle_deg" not in settings
    raw = _native_result(Path(out["artifact_dir"]))
    raw["manufacturing"]["enforcement"] = {
        "overhang": {"build_direction": [0, 0, 1], "build_direction_source": "build_plate"}}
    _finish(h.starts[-1]["meta"], raw, out["job_id"])
    collected = cad_common.collect_durable(out["job_id"])
    assert collected["result"]["manufacturing"]["dfam"]["overhang_max_deg"] == 45


@pytest.mark.parametrize("process_id", ["fdm_0.6", "resin"])
def test_dfam_prefers_print_surface_and_uses_process_dimensions(tmp_path, monkeypatch, process_id):
    import services.dfam as dfam_pkg

    raw = _native_result(tmp_path)
    print_path = tmp_path / "surface_print.stl"
    _cube_stl(print_path)
    raw["artifacts"]["surface_stl"] = raw["artifacts"].pop("surface")
    raw["artifacts"]["surface_print_stl"] = {
        **topo.file_record(print_path), "format": "stl"}
    raw["manufacturing"]["enforcement"] = {
        "overhang": {"build_direction": [1, 0, 0], "build_direction_source": "build_plate"}}
    calls = []
    analyze = dfam_pkg.analyze_printability

    def capture(mesh, params, build_dir):
        calls.append((params, build_dir))
        return analyze(mesh, params, build_dir)

    monkeypatch.setattr(dfam_pkg, "analyze_printability", capture)
    process = topo.print_process(process_id)
    manufacturing = topo.dfam_report(
        raw, raw["artifacts"], {"build_direction": [0, 1, 0]}, process)
    report = manufacturing["dfam"]
    assert report["checked"] is True, report
    assert report["artifact"] == "surface_print_stl"
    assert report["process"] == process["process"]
    assert calls[0][0].process == process["process"]
    assert calls[0][0].layer_height_mm == process["layer_height_mm"]
    if "nozzle_mm" in process:
        assert calls[0][0].nozzle_mm == process["nozzle_mm"]
        assert calls[0][0].line_width_mm == process["line_width_mm"]
        assert report["nozzle_mm"] == process["nozzle_mm"]
    assert report["build_dir"] == [1, 0, 0]
    assert "build_dir_assumed" not in report
    assert f"{process['process'].upper()} rules" in report["summary"]
    assert manufacturing["print_ready"] is False


@pytest.mark.parametrize(("settings", "source", "direction"), [
    ({}, "assumed", [0, 0, 1]),
    ({"build_direction": [0, 1, 0]}, "build_direction", [0, 1, 0]),
])
def test_dfam_reports_direction_fallback_truthfully(tmp_path, settings, source, direction):
    result = _native_result(tmp_path)
    manufacturing = topo.dfam_report(result, result["artifacts"], settings)
    dfam = manufacturing["dfam"]
    assert dfam["checked"] is True, dfam
    assert dfam["build_dir"] == direction
    assert dfam.get("build_dir_assumed", False) is (source == "assumed")


@pytest.mark.parametrize("mismatch", ["hash", "missing", "outside", "no_hash"])
def test_topology_dfam_worker_refuses_unverified_preferred_surface(tmp_path, mismatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = _native_result(run_dir)
    result["topology"] = {"physics": "structural", "method": "simp", "volume_fraction": 0.4}
    print_path = run_dir / "surface_print.stl"
    _cube_stl(print_path)
    entry = {**topo.file_record(print_path), "format": "stl"}
    if mismatch == "hash":
        print_path.write_text("modified", encoding="utf-8")
    elif mismatch == "missing":
        print_path.unlink()
    elif mismatch == "outside":
        outside = tmp_path / "outside.stl"
        _cube_stl(outside)
        entry = {**topo.file_record(outside), "format": "stl"}
    else:
        entry.pop("sha256")
    result["artifacts"]["surface_print_stl"] = entry
    (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    out = WORKER_METHODS["topology_dfam"]({"run_dir": str(run_dir)}, lambda *_: None)
    assert out["ok"] is False and out["code"] == "artifact_mismatch", out
    assert "surface_print_stl" in out["error"]


def test_topology_dfam_worker_checks_fixture_run_and_matches_shared_report(tmp_path):
    result = _native_result(tmp_path)
    result["topology"] = {"physics": "structural", "method": "simp", "volume_fraction": 0.4,
                          "min_member_mm": 1.8, "build_direction": [0, 0, 1]}
    result["artifacts"]["surface_stl"] = result["artifacts"].pop("surface")
    (tmp_path / "result.json").write_text(json.dumps(result), encoding="utf-8")
    out = WORKER_METHODS["topology_dfam"](
        {"run_dir": str(tmp_path), "print_process": "fdm_0.6"}, lambda *_: None)
    expected = topo.dfam_report(result, result["artifacts"], result["topology"],
                                topo.print_process("fdm_0.6"))
    assert out == {"ok": True, "dfam": expected["dfam"], "print_ready": False,
                   "print_ready_reason": expected["print_ready_reason"]}
    assert out["dfam"]["checked"] is True
    assert out["dfam"]["nozzle_mm"] == 0.6


@pytest.mark.parametrize(("material", "slicer_material"), [
    ("pla", "PLA"), ({"key": "petg"}, "PETG"), ("abs", "ABS"), ("asa", "ASA"),
    ("tpu", "TPU"), ("6061-T6", None), ({"name": "PLA plastic"}, None),
])
def test_print_handoff_prefers_print_surface_and_only_maps_library_materials(
        h, material, slicer_material):
    h.doc.studies.append(_gui_study())
    out = h.run(study_id="study-00000001", study={
        "print_process": "fdm_0.4", "build_plate": {"faces": ["bottom"]}})
    assert out["ok"] is True, out
    meta = h.starts[-1]["meta"]
    meta["material_key"] = material if isinstance(material, str) else material.get("key")
    run_dir = Path(out["artifact_dir"])
    result = _native_result(run_dir)
    print_path = run_dir / "surface_print.stl"
    _cube_stl(print_path)
    result["artifacts"]["surface_stl"] = result["artifacts"].pop("surface")
    result["artifacts"]["surface_print_stl"] = {
        **topo.file_record(print_path), "format": "stl"}
    result["manufacturing"]["enforcement"] = {
        "overhang": {"build_direction": [0, 0, 1], "build_direction_source": "build_plate"}}
    _finish(meta, result, out["job_id"])
    collected = cad_common.collect_durable(out["job_id"])
    expected = {
        "surface_path": str(print_path), "surface_artifact": "surface_print_stl",
        "build_dir": [0, 0, 1], "build_dir_source": "build_plate", "print_process": "fdm_0.4",
        "slice_part": {"path": str(print_path), "build_dir": [0, 0, 1]},
        "import_part": {"path": str(print_path), "as_solid": True}}
    if slicer_material:
        expected["material"] = slicer_material
        expected["slice_part"]["material"] = slicer_material
    assert collected["print_handoff"] == expected
    assert collected["result"]["print_handoff"] == expected
    assert "printer" not in collected["print_handoff"]["slice_part"]
    assert collected["result"]["manufacturing"]["dfam"]["process"] == "fdm"
    assert collected["result"]["topology"]["min_member_mm"] == 1.2


def test_resolved_bed_faceset_is_validated_before_job_submission(h, tmp_path):
    step = tmp_path / "source.step"
    step.write_text("ISO-10303-21;", encoding="utf-8")
    study = topo.normalize_study(_gui_study(), [])
    study["build_plate"] = {"faces": ["bottom"]}
    envelope = topo.build_envelope(
        study, settings=study["topology"], material=study["material"],
        resolve=lambda selector: h.doc.query("doc-1", selector) if isinstance(
            selector, str) else [LEFT if selector["topo"] == 1 else RIGHT],
        step_path=str(step), result_dir=str(tmp_path), notes=[])
    topo.validate_run_envelope(envelope)
    envelope["build_plate"]["faces"] = {"selector": "bottom"}
    with pytest.raises(ValueError, match="build_plate.*points"):
        topo.validate_run_envelope(envelope)


@pytest.mark.parametrize(("params", "code"), [
    ({}, "invalid_input"), ({"run_dir": "relative"}, "invalid_input"),
    (None, "invalid_input"),
])
def test_topology_dfam_worker_refuses_invalid_run_parameters(params, code):
    out = WORKER_METHODS["topology_dfam"](params, lambda *_: None)
    assert out["ok"] is False and out["code"] == code


def test_topology_dfam_worker_refuses_non_topology_results_and_unknown_presets(tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"ok":true}', encoding="utf-8")
    params = {"run_dir": str(tmp_path)}
    out = WORKER_METHODS["topology_dfam"](params, lambda *_: None)
    assert out["code"] == "invalid_result"
    result = _native_result(tmp_path)
    result["topology"] = {}
    path.write_text(json.dumps(result), encoding="utf-8")
    out = WORKER_METHODS["topology_dfam"](
        {**params, "print_process": "unknown"}, lambda *_: None)
    assert out["code"] == "unknown_print_process"
