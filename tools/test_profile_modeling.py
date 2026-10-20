"""Invalid exports and real cache misses cannot become successful throughput."""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from tools import perf_regression, profile_modeling


@pytest.fixture
def engine():
    with perf_regression._private_engine():
        try:
            profile_modeling.cb._load()
        except Exception as exc:
            pytest.skip(f"in-process engine unavailable: {exc}")
        yield profile_modeling.cb


def test_failed_export_invalidates_completed_workload(engine, tmp_path):
    prefix = tmp_path / "profile"
    prefix.with_suffix(".folded").mkdir()
    code = profile_modeling.main([
        "profile_modeling.py", "--workload", "open", "--seconds", "0.02",
        "--out", str(prefix),
    ])
    report = json.loads(prefix.with_suffix(".report.json").read_text())
    assert code == 1
    assert report["warmup"]["cache_hit"] is True
    assert report["last_measured"]["cache"]["features_executed"] == 0
    assert report["ok"] is False
    assert report["throughput"]["valid"] is False
    assert report["throughput"]["iterations_per_second"] is None


def test_lost_persisted_cache_is_not_a_warm_open_measurement(engine, monkeypatch, tmp_path):
    original = profile_modeling._checked
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

    monkeypatch.setattr(profile_modeling, "_checked", lose_cache)
    prefix = tmp_path / "profile"
    code = profile_modeling.main([
        "profile_modeling.py", "--workload", "open", "--seconds", "0.02",
        "--out", str(prefix),
    ])
    report = json.loads(prefix.with_suffix(".report.json").read_text())
    assert code == 1
    assert observed[0]["cache_hit"] is False
    assert report["ok"] is False
    assert report["throughput"]["completed_iterations"] == 0
    assert report["throughput"]["valid"] is False
    assert report["throughput"]["iterations_per_second"] is None
