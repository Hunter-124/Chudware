"""Consumer-facing failure and baseline gates; live failure cases use the real engine."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import perf_regression as perf


def test_import_does_not_change_engine_discovery():
    code = """
import os
keys = ('CHUDWARE_ENGINE_DAEMON', 'CHUDWARE_HOSTD_SOCKET', 'CHUDWARE_HOSTD_PIPE')
before = {key: os.environ.get(key) for key in keys}
from tools import perf_regression, profile_modeling
assert {key: os.environ.get(key) for key in keys} == before
"""
    subprocess.run([sys.executable, "-c", code], cwd=perf._HERE.parent, check=True)


def test_failed_command_restores_caller_engine_discovery(monkeypatch, tmp_path):
    values = {"CHUDWARE_ENGINE_DAEMON": "1",
              "CHUDWARE_HOSTD_SOCKET": str(tmp_path / "caller.sock"),
              "CHUDWARE_HOSTD_PIPE": r"\\.\pipe\caller"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert perf.main(["perf_regression.py", "--iters", "0"]) == 3
    assert {key: os.environ.get(key) for key in values} == values


def _record(name="box", samples=(10.0, 11.0, 12.0)):
    metric = perf._stats([{"wall_ms": value, "cpu_ms": value} for value in samples])
    metric.update(perf._workload_contract(name))
    return {"schema_version": perf.SCHEMA_VERSION, "status": "ok", "failures": [],
            "environment": {"machine": "test-machine", "thread_env": {}},
            "engine": {"abi": "1", "sha256": "before"}, "metrics": {name: metric}}


def test_compatible_baseline_compares_changed_engine_and_detects_regression():
    baseline = _record()
    current = _record(samples=(20.0, 21.0, 22.0))
    current["engine"]["sha256"] = "after"
    regressions, rows = perf.compare(current, baseline, 0.25)
    assert len(regressions) == 1
    assert rows[0][-1] == "REGRESSION"
    assert perf.compare(baseline, baseline, 0.25)[0] == []


@pytest.mark.parametrize("damage", ["empty", "legacy", "missing_metric", "failed", "samples",
                                    "nonfinite", "timing", "signature", "environment", "abi"])
def test_incompatible_baselines_cannot_pass(damage):
    current = _record()
    baseline = copy.deepcopy(current)
    if damage == "empty":
        baseline["metrics"] = {}
    elif damage == "legacy":
        baseline.pop("schema_version")
    elif damage == "missing_metric":
        baseline["metrics"]["other"] = baseline["metrics"].pop("box")
    elif damage == "failed":
        baseline["metrics"]["box"]["status"] = "failed"
    elif damage == "samples":
        baseline["metrics"]["box"]["samples"] = []
    elif damage == "nonfinite":
        baseline["metrics"]["box"]["median_ms"] = float("nan")
    elif damage == "timing":
        baseline["metrics"]["box"]["timing"] = "doc_new+doc_apply_ops+doc_close"
    elif damage == "signature":
        baseline["metrics"]["box"]["signature"] = "different-workload"
    elif damage == "environment":
        baseline["environment"]["thread_env"]["OMP_NUM_THREADS"] = "8"
    elif damage == "abi":
        baseline["engine"]["abi"] = "different-ABI"
    with pytest.raises(perf.BaselineError):
        perf.compare(current, baseline, 0.25)


def test_open_cold_is_not_a_warm_open_baseline():
    current = _record("open_warm")
    baseline = copy.deepcopy(current)
    baseline["metrics"]["open_cold"] = baseline["metrics"].pop("open_warm")
    with pytest.raises(perf.BaselineError):
        perf.compare(current, baseline, 0.25)


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), -0.1])
def test_invalid_threshold_cannot_pass(threshold):
    with pytest.raises(perf.BaselineError):
        perf.compare(_record(), _record(), threshold)


def test_report_cannot_overwrite_baseline_on_argument_failure(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text("preserve this baseline", encoding="utf-8")
    code = perf.main(["perf_regression.py", "--baseline", str(baseline), "--json", str(baseline)])
    assert code == 3
    assert baseline.read_text(encoding="utf-8") == "preserve this baseline"


@pytest.mark.parametrize("arguments", [["--iters", "0"], ["--threshold", "nan"],
                                       ["--threshold", "inf"], ["--threshold", "-1"],
                                       ["--workload", "open_cold"]])
def test_invalid_measurement_request_returns_three_and_records_failure(tmp_path, arguments):
    report = tmp_path / "failed.json"
    assert perf.main(["perf_regression.py", "--json", str(report), *arguments]) == 3
    record = json.loads(report.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["failures"]


@pytest.mark.parametrize("abi", ["missing_export", None, b""])
def test_missing_native_abi_metadata_is_rejected(monkeypatch, abi):
    dll = SimpleNamespace(_name=perf.__file__)
    if abi != "missing_export":
        def abi_fn():
            return abi
        dll.chudware_abi_version = abi_fn
    monkeypatch.setattr(perf.cb, "_load", lambda: dll)
    with pytest.raises(perf.MeasurementError):
        perf._engine_metadata()


@pytest.fixture
def engine():
    with perf._private_engine():
        try:
            perf.cb._load()
        except Exception as exc:
            pytest.skip(f"in-process engine unavailable: {exc}")
        yield perf.cb


def test_metadata_ping_failure_is_not_silenced(engine, monkeypatch):
    call = engine.call

    def fail_ping(method, params):
        return {"ok": False, "error": "metadata unavailable"} if method == "ping" else call(method, params)

    monkeypatch.setattr(engine, "call", fail_ping)
    with pytest.raises(perf.MeasurementError):
        perf._engine_metadata()


def _invalid_fillet_workload():
    # A real edge plus an impossible circular edge on a box: the complete
    # selection must fail, rather than silently blending the valid subset.
    fillet = perf._fillet(2)
    fillet["feature"]["refs"]["edges"] = [
        {"of": "edge", "parallel": "z"},
        {"of": "edge", "surf": "circle"},
    ]
    return [perf._box("Block", 80, 60, 40), fillet]


@pytest.mark.parametrize("warmup,phase", [(0, "sample"), (1, "warmup")])
def test_invalid_fillet_never_produces_a_timing_and_closes_its_document(engine, warmup, phase):
    before = engine.call("doc_list", {})["docs"]
    ops = _invalid_fillet_workload()
    with pytest.raises(perf.MeasurementError) as caught:
        perf._time_build(ops, iters=1, warmup=warmup)
    assert caught.value.detail["phase"] == phase
    assert caught.value.detail["completed_samples"] == []
    assert engine.call("doc_list", {})["docs"] == before


def test_failed_sample_after_successful_warmup_has_no_measurement(engine):
    before = engine.call("doc_list", {})["docs"]
    workloads = iter(([perf._box("Block", 80, 60, 40)], _invalid_fillet_workload()))
    with pytest.raises(perf.MeasurementError) as caught:
        perf._time_iterations(lambda: perf._build_once(next(workloads)), iters=1, warmup=1)
    assert caught.value.detail["phase"] == "sample"
    assert caught.value.detail["completed_samples"] == []
    assert len(caught.value.detail["completed_warmups"]) == 1
    assert engine.call("doc_list", {})["docs"] == before


def test_open_workload_is_a_real_warm_cache_measurement(engine):
    before = engine.call("doc_list", {})["docs"]
    metric = perf._time_open(iters=1, warmup=1)
    assert metric["status"] == "ok"
    assert metric["n"] == 1
    for sample in metric["warmups"] + metric["samples"]:
        assert sample["cache"]["cache_hit"] is True
        assert sample["cache"]["warm"] is True
        assert sample["cache"]["features_executed"] == 0
    assert engine.call("doc_list", {})["docs"] == before


def test_lost_persisted_cache_cannot_be_a_warm_benchmark(engine, monkeypatch):
    before = engine.call("doc_list", {})["docs"]
    original = perf._rpc
    observed = []

    def lose_cache(method, params):
        response = original(method, params)
        if method == "doc_save":
            path = Path(params["path"])
            with zipfile.ZipFile(path) as source:
                members = [(info, source.read(info)) for info in source.infolist()
                           if not info.filename.startswith("cache.")]
            replacement = path.with_suffix(".cold")
            with zipfile.ZipFile(replacement, "w") as target:
                for info, data in members:
                    target.writestr(info, data)
            os.replace(replacement, path)
        elif method == "doc_open":
            observed.append(response)
        return response

    monkeypatch.setattr(perf, "_rpc", lose_cache)
    with pytest.raises(perf.MeasurementError) as caught:
        perf._time_open(iters=1, warmup=1)
    assert observed and observed[0]["cache_hit"] is False
    assert caught.value.detail["phase"] == "warmup"
    assert caught.value.detail["completed_samples"] == []
    assert caught.value.detail["completed_warmups"] == []
    assert engine.call("doc_list", {})["docs"] == before


@pytest.mark.parametrize("existing_baseline", [False, True])
def test_failed_measurements_are_reported_and_cannot_update_baseline(
        engine, monkeypatch, tmp_path, existing_baseline):
    monkeypatch.setitem(perf.WORKLOADS, "fillet", _invalid_fillet_workload())
    baseline, report = tmp_path / "baseline.json", tmp_path / "failed.json"
    previous = b'{"sentinel":"do not overwrite"}\n'
    if existing_baseline:
        baseline.write_bytes(previous)
    before = engine.call("doc_list", {})["docs"]
    code = perf.main(["perf_regression.py", "--iters", "1", "--workload", "fillet",
                      "--update", "--baseline", str(baseline), "--json", str(report)])
    assert code == 3
    if existing_baseline:
        assert baseline.read_bytes() == previous
    else:
        assert not baseline.exists()
    record = json.loads(report.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["engine"]["abi"] == engine._load().chudware_abi_version().decode("utf-8")
    assert record["metrics"]["fillet"]["status"] == "failed"
    assert "median_ms" not in record["metrics"]["fillet"]
    assert record["metrics"]["fillet"]["detail"]["phase"] == "warmup"
    assert record["metrics"]["fillet"]["detail"]["completed_samples"] == []
    assert record["diagnostics"]["per_feature_ms_fillet"]["status"] == "failed"
    contention = record["diagnostics"]["contention_burst"]
    assert contention.get("available") is False or contention["status"] == "failed"
    assert record["failures"]
    assert record["comparison"]["status"] == "not_run"
    assert engine.call("doc_list", {})["docs"] == before
