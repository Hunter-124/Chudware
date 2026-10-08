"""Pure and mocked fitting calibration contracts; no engine or CFD is required."""
from __future__ import annotations

import copy
import threading

import pytest

from services.piping import fitting_cfd as fc
from services.piping import worker_rpc as worker


def test_two_k_recovers_synthetic_coefficients():
    diameter = 52.5
    points = [{"re": re, "K": 800 / re + 0.12 * (1 + 25.4 / diameter)}
              for re in (1e3, 1e4, 1e5)]
    fit = fc.fit_two_k(points, diameter)
    assert fit["K1"] == pytest.approx(800)
    assert fit["Kinf"] == pytest.approx(0.12)


def test_two_k_constrains_negative_coefficient():
    fit = fc.fit_two_k([{"re": 1e4, "K": 0.2}, {"re": 1e5, "K": 0.4}], 50)
    assert fit["K1"] == 0
    assert fit["Kinf"] == pytest.approx(0.3 / (1 + 25.4 / 50))


@pytest.mark.parametrize("kind", ["tee", "valve", "orifice", "custom", "reducer"])
def test_unsupported_fitting_refuses_before_native_call(kind):
    def forbidden(*args):
        raise AssertionError("Unsupported geometry must not call the engine")
    result = fc.compute({"type": kind}, rpc=forbidden)
    assert not result["ok"]
    assert result["reason"] == "unsupported_fitting_geometry"
    assert kind in result["hint"] and "geometry" in result["hint"]


def test_k_model_table_shape_and_sorted_points():
    provenance = {"engine": "cpp-lbm-internal", "cells_across": 12, "steps": [20, 30],
                  "converged": True, "quality": "quick", "started_at": "start", "completed_at": "end"}
    model = fc.make_k_model([{"re": 1e5, "K": 0.2}, {"re": 1e4, "K": 0.3}], 50, provenance)
    assert model["kind"] == "table" and model["source"] == "cfd"
    assert model["points"] == [{"re": 1e4, "K": 0.3}, {"re": 1e5, "K": 0.2}]
    assert set(model["fit"]) == {"K1", "Kinf"}
    assert model["provenance"] == provenance


def test_apply_issues_native_update_fitting():
    calls = []
    model = {"kind": "constant", "K": 0.2}
    doc = {"schema": "aicad.piping/1", "fittings": [{"id": "f1"}]}
    def rpc(method, params):
        calls.append((method, params))
        return {"ok": True, "piping": doc}
    assert fc.apply_model(doc, "f1", model, rpc)["ok"]
    assert calls == [("piping_edit", {"piping": doc, "ops": [
        {"op": "update_fitting", "id": "f1", "params": {"k_model": model}}]})]


def test_custom_pipeline_subtracts_darcy_and_uses_durable_jobs(monkeypatch, tmp_path):
    stl = tmp_path / "fluid.stl"
    stl.write_text("solid fluid\nendsolid fluid\n")
    geometry = {"stl_path": str(stl), "inner_dia_mm": 50,
                "inlet": {"point": [0, 0, 0], "normal": [1, 0, 0]},
                "outlet": {"point": [500, 0, 0], "normal": [-1, 0, 0]}}
    payloads = []
    def start(kind, stage, payload):
        payloads.append(copy.deepcopy(payload))
        return {"job_id": str(len(payloads))}
    def raw(job):
        velocity = payloads[int(job) - 1]["params"]["mean_velocity_ms"]
        dynamic = 0.5 * 1000 * velocity ** 2
        return {"ok": True, "converged": True, "steps": 500, "dp_pa": 10 + 0.3 * dynamic,
                "darcy_estimate_pa": 10, "provenance": {"collision": "regularized"}}
    monkeypatch.setattr(fc.jobs, "start", start)
    monkeypatch.setattr(fc.jobs, "status", lambda job: {"state": "done"})
    monkeypatch.setattr(fc.jobs, "raw_result", raw)
    monkeypatch.setattr(fc.jobs, "refusal_result", lambda job: None)
    def rpc(method, params):
        assert method == "piping_fitting_k"
        assert "k_model" not in params["fitting"]["params"]
        return {"ok": True, "K": 0.25, "method": "test correlation"}
    result = fc.compute({"type": "custom", "params": {"k_model": {"kind": "constant", "K": 7}}},
                        geometry=geometry, fluid={"rho": 1000, "mu": 0.001}, rpc=rpc)
    assert result["ok"]
    assert [p["K"] for p in result["points"]] == pytest.approx([0.3, 0.3])
    assert result["deviation_pct"] == pytest.approx(20)
    assert len(payloads) == 2
    assert all(p["method"] == "sim_run_pipe_flow" and p["params"]["cells_across"] == 12 for p in payloads)
    assert result["k_model"]["provenance"]["converged"] is True


def test_worker_status_cancel_never_wait_for_solver(monkeypatch):
    entered, released = threading.Event(), threading.Event()
    def compute(**kwargs):
        entered.set()
        kwargs["cancel_event"].wait(2)
        released.set()
        return {"ok": False, "reason": "cancelled", "hint": "cancelled"}
    monkeypatch.setattr(fc, "compute", compute)
    started = worker.start({"fitting": {"type": "elbow", "nps": 2}})
    assert entered.wait(1)
    job = started["job_id"]
    assert worker.status({"job_id": job})["state"] == "running"
    assert worker.cancel({"job_id": job})["ok"]
    assert released.wait(1)


def test_worker_apply_persists_latest_network(monkeypatch):
    from services.mcp import piping_tools as pt
    model = {"kind": "table", "source": "cfd", "points": [{"re": 1e4, "K": 0.2}]}
    latest = {"schema": "aicad.piping/1", "fittings": [{"id": "f1"}], "newer": True}
    calls = []
    monkeypatch.setattr(fc, "compute", lambda **kwargs: {"ok": True, "k_model": model})
    monkeypatch.setattr(pt, "_find_study", lambda *args: {"params": {"piping": latest}})
    monkeypatch.setattr(pt, "_rpc", lambda method, params: calls.append((method, params)) or {
        "ok": True, "piping": latest, "changed": ["f1"]})
    monkeypatch.setattr(pt, "_persist_network", lambda *args: {"saved_to_study": True})
    event = threading.Event()
    with worker._LOCK:
        worker._JOBS["test-apply"] = {"ok": True, "state": "running", "cancel_event": event}
    worker._run("test-apply", {"fitting": {"type": "elbow"}, "apply_context": {
        "piping": {"old": True}, "fitting_id": "f1", "network": "plant", "doc_id": "d1"}}, event)
    result = worker.status({"job_id": "test-apply"})["result"]
    assert result["applied"] and result["saved_to_study"]
    assert calls[0][0] == "piping_edit" and calls[0][1]["piping"] == latest
    assert calls[0][1]["ops"] == [{"op": "update_fitting", "id": "f1", "params": {"k_model": model}}]
