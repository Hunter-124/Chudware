"""Invalid exports and real cache misses cannot become successful throughput."""
from __future__ import annotations

import json
import math
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


def test_interactive_workload_rounds_physical_edges_without_seams(engine):
    ops = profile_modeling._heavy_build()
    before_docs = engine.call("doc_list", {})["docs"]
    with perf_regression._document(perf_regression._rpc(
            "doc_new", {"kind": "part", "name": "profile_geometry"})) as doc_id:
        base = perf_regression._rpc("doc_apply_ops", {"doc_id": doc_id, "ops": ops[:-1]})
        perf_regression._validate_build(base, ops[:-1])

        def query(selector):
            return perf_regression._rpc(
                "doc_query", {"doc_id": doc_id, "query": selector})["matches"]

        def key(edge):
            return edge["body"], edge["topo"]

        edges = query({"of": "edge"})
        outside = [e for e in edges if e["surf"] == "line"
                   and (abs(abs(e["centroid"][0]) - 80) < 1e-4
                        or abs(abs(e["centroid"][1]) - 80) < 1e-4)]
        rims = [e for e in edges if e["surf"] == "circle"]
        seams = [e for e in edges if e["surf"] == "line" and e not in outside]
        assert len(outside) == 12
        assert len(rims) == 2 * 24
        assert all(e["radius"] == pytest.approx(3) for e in rims)
        assert all(math.hypot(*e["centroid"][:2]) == pytest.approx(60, abs=1e-4)
                   for e in rims)
        assert len(seams) == 24
        assert all(abs(e["axis"][2]) == pytest.approx(1) for e in seams)

        selected = {key(e) for selector in ops[-1]["feature"]["refs"]["edges"]
                    for e in query(selector)}
        assert selected == {key(e) for e in outside + rims}
        assert selected.isdisjoint({key(e) for e in seams})

        built = perf_regression._rpc("doc_apply_ops", {"doc_id": doc_id, "ops": ops[-1:]})
        perf_regression._validate_build(built, ops)
        fillet = built["rebuild"]["feature_status"][-1]["data"]["fillet"]
        assert fillet["selected_edges"] == fillet["covered_edges"] == len(selected)
        assert fillet["requested"]["radius"] == pytest.approx(2)
        assert fillet["effective"]["radius"] == pytest.approx(2)
        assert 0 < built["volume"] < base["volume"]
    assert engine.call("doc_list", {})["docs"] == before_docs


def test_failed_export_invalidates_completed_workload(engine, tmp_path):
    before_docs = engine.call("doc_list", {})["docs"]
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
    assert report["throughput"]["completed_iterations"] > 0
    assert report["ok"] is False
    assert report["throughput"]["valid"] is False
    assert report["throughput"]["iterations_per_second"] is None
    assert engine.call("doc_list", {})["docs"] == before_docs


def test_lost_persisted_cache_is_not_a_warm_open_measurement(engine, monkeypatch, tmp_path):
    before_docs = engine.call("doc_list", {})["docs"]
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
    assert observed
    assert observed[0]["cache_hit"] is False
    assert report["ok"] is False
    assert report["throughput"]["completed_iterations"] == 0
    assert report["throughput"]["valid"] is False
    assert report["throughput"]["iterations_per_second"] is None
    assert engine.call("doc_list", {})["docs"] == before_docs
