"""Piping -> 3D bridge tests (contract §7). House style: main() + asserts.

Needs the build123d/OCP kernel AND fluids; SKIPS cleanly when either is absent (an
environment SKIP, not a failure — same policy as scripts/gate.sh). A full run does a real
OCCT sweep + STEP export/import and takes a few seconds.

Covers:
  1. A routed run sweeps to a non-empty, positive-volume solid along its 3-D centreline.
  2. All four fitting types build; the assembly exports STEP and re-imports non-empty.
  3. piping_to_part_doc emits a valid aicad.part/1 import envelope pointing at the STEP.
  4. An all-unresolvable doc raises PipingGeometryError (never a bare traceback).

Run (from repo root):  python -m engine.piping.test_threed
"""

from __future__ import annotations

import os
import tempfile

try:
    import pytest
except Exception:  # noqa: BLE001
    pytest = None

from services.piping import catalog, schema, threed


def _have_kernel() -> bool:
    if not catalog.have_fluids():
        return False
    try:
        import build123d  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


HAVE_KERNEL = _have_kernel()
_needs_kernel = (pytest.mark.skipif(not HAVE_KERNEL, reason="build123d/fluids not installed")
                 if pytest else (lambda f: f))


def _sample_doc() -> dict:
    doc = schema.new_piping("Skid line 100")
    doc["runs"].append(schema.new_pipe_run(
        "r1", path=[[0, 0, 0], [1000, 0, 0], [1000, 800, 0], [1000, 800, 600]],
        nps=2, schedule="40"))
    doc["fittings"] += [
        schema.new_fitting("ft1", "elbow", nps=2, origin=(1000, 0, 0),
                           params={"angle": 90, "radius": "LR"}),
        schema.new_fitting("ft2", "tee", nps=2, origin=(0, 0, 0),
                           params={"branch_nps": 2}),
        schema.new_fitting("ft3", "reducer", nps=2, nps2=1.5, origin=(1000, 800, 600)),
        schema.new_fitting("ft4", "flange", nps=2, origin=(-100, 0, 0),
                           params={"flange_class": 150, "flange_face": "RF"}),
    ]
    assert schema.validate_piping(doc) == [], schema.validate_piping(doc)
    return doc


@_needs_kernel
def test_run_sweeps_to_solid():
    doc = schema.new_piping()
    doc["runs"].append(schema.new_pipe_run(
        "r1", path=[[0, 0, 0], [200, 0, 0], [200, 150, 0]], nps=2, schedule="40"))
    with tempfile.TemporaryDirectory() as d:
        res = threed.piping_to_step(doc, os.path.join(d, "run.step"))
    s = res["summary"]
    assert s["n_runs"] == 1 and s["n_skipped"] == 0, s
    assert s["volume"] > 0 and s["total_length"] > 300, s


@_needs_kernel
def test_full_assembly_step_roundtrips():
    doc = _sample_doc()
    with tempfile.TemporaryDirectory() as d:
        step = os.path.join(d, "piping.step")
        res = threed.piping_to_step(doc, step)
        s = res["summary"]
        assert s["n_runs"] == 1 and s["n_fittings"] == 4 and s["n_skipped"] == 0, s
        assert os.path.getsize(step) > 0
        # re-import the STEP -> a non-empty shape (proves valid B-rep landed on disk).
        from build123d import import_step
        shape = import_step(step)
        assert shape is not None and shape.volume > 0

        pd = threed.piping_to_part_doc(doc, step)
        assert pd["schema"] == "aicad.part/1"
        feat = pd["features"][0]
        assert feat["type"] == "import" and feat["params"]["format"] == "step"
        assert os.path.isfile(feat["params"]["source_path"])


@_needs_kernel
def test_unresolvable_doc_raises_clean():
    doc = schema.new_piping()
    doc["runs"].append(schema.new_pipe_run(
        "r1", path=[[0, 0, 0], [10, 0, 0]], nps=999, schedule="40"))  # no such size
    raised = False
    try:
        with tempfile.TemporaryDirectory() as d:
            threed.piping_to_step(doc, os.path.join(d, "x.step"))
    except threed.PipingGeometryError:
        raised = True
    assert raised, "expected PipingGeometryError for all-unresolvable doc"


def main():
    if not HAVE_KERNEL:
        print("test_threed: SKIP (build123d/fluids kernel not installed)")
        return
    test_run_sweeps_to_solid()
    test_full_assembly_step_roundtrips()
    test_unresolvable_doc_raises_clean()
    print("test_threed OK")


if __name__ == "__main__":
    main()
