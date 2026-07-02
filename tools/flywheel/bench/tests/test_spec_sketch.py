"""spec-sketch bench: spec loading, deterministic template filler, scoring (fixture mode)."""
import copy
import os

from tools.flywheel.bench.benches import spec_sketch as SS
from tools.flywheel.bench.dataset import DatasetSpec

SPECS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                         "training", "benches", "spec_sketch", "specs")
MANIFEST = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                        "training", "benches", "spec_sketch", "spec.json")


def test_specs_load_and_span_families():
    specs = SS.load_specs(SPECS_DIR)
    assert 8 <= len(specs) <= 12
    families = {s["family"] for s in specs}
    assert families == set(SS.FAMILY_BUILDERS)
    assert len(families) >= 4
    ids = [s["id"] for s in specs]
    assert len(ids) == len(set(ids))  # unique example ids


def test_dataset_hash_is_stable_and_frozen():
    ds = DatasetSpec.load(MANIFEST, SPECS_DIR)
    assert ds.verify()
    # rebuilding the hash independently must match the frozen manifest
    ds2 = DatasetSpec.freeze("spec_sketch_specs", SPECS_DIR, split="all",
                             n_examples=ds.n_examples, structures=ds.structures)
    assert ds2.hash == ds.hash


def test_deterministic_filler_is_in_tol_and_full_for_every_family():
    specs = SS.load_specs(SPECS_DIR)
    seen_families = set()
    for spec in specs:
        sketch = SS.fill_template(spec)
        assert SS.solver_status_proxy(sketch) == "full"
        realized = SS.measure_dims(sketch, spec["family"])
        in_tol, diffs = SS._dims_in_tol(spec["dims"], realized, spec["family"], spec["tol"])
        assert in_tol, (spec["id"], diffs)
        ex = SS.score_example(spec)
        assert ex.score == 1.0
        assert ex.passed is True
        assert ex.structure == spec["family"]
        assert ex.detail["mode"] == "fixture"
        seen_families.add(spec["family"])
    assert seen_families == set(SS.FAMILY_BUILDERS)


def test_realized_dims_are_computed_from_geometry_not_echoed():
    spec = {"id": "t1", "family": "rect_plate", "dims": {"width": 80, "height": 50}, "tol": 0.01}
    sketch = SS.fill_template(spec)
    realized = SS.measure_dims(sketch, "rect_plate")
    # perturb the produced entity coordinates directly (not the spec) -> realized must track it
    mutated = copy.deepcopy(sketch)
    mutated["params"]["entities"][1]["p2"][0] = 85.0  # e2.p2 defines the width corner
    mutated["params"]["entities"][2]["p1"][0] = 85.0
    realized2 = SS.measure_dims(mutated, "rect_plate")
    assert realized2["width"] == 85.0
    assert realized["width"] == 80.0


def test_out_of_tolerance_candidate_scores_below_one_and_fails():
    spec = {"id": "t2", "family": "rect_plate", "dims": {"width": 80, "height": 50}, "tol": 0.01}
    sketch = SS.fill_template(spec)
    # corrupt the built sketch's geometry so width misses tolerance, but keep it fully
    # constrained structurally (proxy still says "full") -> isolates the in-tol failure.
    bad = copy.deepcopy(sketch)
    bad["params"]["entities"][0]["p2"][0] = 81.0   # e1.p2
    bad["params"]["entities"][1]["p1"][0] = 81.0   # e2.p1
    assert SS.solver_status_proxy(bad) == "full"
    ex = SS.score_sketch(spec, bad)
    assert ex.detail["in_tol"] is False
    assert ex.score < 1.0
    assert ex.passed is False


def test_under_constrained_candidate_fails_even_if_in_tol():
    spec = {"id": "t3", "family": "circle_hole", "dims": {"diameter": 5}, "tol": 0.01}
    sketch = SS.fill_template(spec)
    under = copy.deepcopy(sketch)
    under["params"]["constraints"] = [c for c in under["params"]["constraints"]
                                       if c["type"] != "fix"]  # drop the fix -> under-defined
    assert SS.solver_status_proxy(under) == "under"
    ex = SS.score_sketch(spec, under)
    assert ex.detail["in_tol"] is True   # dims still measure correctly...
    assert ex.passed is False            # ...but not "full", so it must not pass
    assert ex.score < 1.0


def test_run_produces_two_baselines_and_pending_residual():
    ds = DatasetSpec.load(MANIFEST, SPECS_DIR)
    res = SS.run(SPECS_DIR, ds.hash)
    assert res.n_examples == ds.n_examples
    agg = res.aggregate
    assert agg["in_tol_rate"] == 1.0
    assert agg["full_solve_rate"] == 1.0
    assert agg["success_bar"]["full_and_in_tol"] is True
    kinds = {b.kind: b.status for b in res.baselines}
    assert kinds == {"deterministic": "measured", "frontier": "pending"}
    det_row = next(b for b in res.baselines if b.kind == "deterministic")
    assert det_row.mean_score == 1.0
    assert det_row.pass_rate == 1.0
    assert res.residual["status"] == "pending"


def test_run_respects_ids_filter_for_smoke():
    ds = DatasetSpec.load(MANIFEST, SPECS_DIR)
    specs = SS.load_specs(SPECS_DIR)
    seen, ids = set(), []
    for s in specs:
        if s["family"] not in seen:
            ids.append(s["id"])
            seen.add(s["family"])
    res = SS.run(SPECS_DIR, ds.hash, split="smoke", ids=ids)
    assert res.n_examples == len(ids)
    assert {e.structure for e in res.examples} == set(SS.FAMILY_BUILDERS)


def test_result_json_roundtrips():
    ds = DatasetSpec.load(MANIFEST, SPECS_DIR)
    res = SS.run(SPECS_DIR, ds.hash)
    from tools.flywheel.bench.schema import BenchResult
    back = BenchResult.from_json(res.to_json())
    assert back.bench_id == "spec_sketch"
    assert len(back.examples) == res.n_examples
    assert back.examples[0].structure  # held-out structure recorded per example


def test_driver_is_registered_in_runner():
    from tools.flywheel.bench.runner import REGISTRY
    assert "spec_sketch" in REGISTRY
    assert REGISTRY["spec_sketch"] is SS.DRIVER
