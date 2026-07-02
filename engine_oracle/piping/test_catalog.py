"""Catalog tests: fluids-backed nearest_pipe + CSV fitting lookup. House style: main() + asserts.

The nearest_pipe tests need the optional ``fluids`` dependency; they SKIP cleanly when it is
not installed (contract-shaped degradation is asserted instead). The CSV tests are pure-Python.

Run (from repo root):  python -m engine.piping.test_catalog
"""

from __future__ import annotations

try:
    import pytest
except Exception:  # noqa: BLE001
    pytest = None

from engine.piping import catalog

HAVE_FLUIDS = catalog.have_fluids()
_needs_fluids = (pytest.mark.skipif(not HAVE_FLUIDS, reason="fluids not installed")
                 if pytest else (lambda f: f))


@_needs_fluids
def test_nearest_pipe_nps():
    spec = catalog.nearest_pipe(nps=2, schedule="40")
    assert not isinstance(spec, dict)
    # NPS-2 sch40: OD 60.3 mm, wall ~3.91 mm (ASME B36.10) — mm, not metres.
    assert abs(spec.outer_dia - 60.3) < 0.2, spec
    assert 3.5 < spec.wall < 4.3, spec
    assert spec.inner_dia < spec.outer_dia


@_needs_fluids
def test_nearest_pipe_by_od_and_alias():
    spec = catalog.nearest_pipe(outer_dia=114.3, schedule="STD")  # ~ NPS 4
    assert not isinstance(spec, dict)
    assert abs(spec.nps - 4.0) < 0.01, spec


def test_nearest_pipe_soft_fail():
    # Unknown size -> contract-shaped, never raises.
    bad = catalog.nearest_pipe(nps=999)
    if HAVE_FLUIDS:
        assert isinstance(bad, dict) and bad["ok"] is False and "hint" in bad
    else:
        assert isinstance(bad, dict) and "fluids" in bad["reason"]


def test_fitting_dims_hits():
    for ftype in ("elbow", "tee"):
        d = catalog.fitting_dims(ftype, 2)
        assert d.get("ok") is not False, d
        assert d["center_to_end"] and d["center_to_end"] > 0
    fl = catalog.fitting_dims("flange", 2, flange_class=150)
    assert fl.get("ok") is not False
    assert fl["od"] > 0 and fl["bolt_holes"] >= 4
    rd = catalog.fitting_dims("reducer", 2, nps2=1.5)
    assert rd.get("ok") is not False and rd["length"] > 0


def test_fitting_dims_miss_hint():
    d = catalog.fitting_dims("elbow", 5)  # NPS 5 not catalogued
    assert d["ok"] is False and "nearest" in d.get("hint", "")


def test_available_sizes_nonempty():
    for ftype in ("elbow", "tee", "flange", "reducer"):
        assert catalog.available_sizes(ftype), ftype


def main():
    if HAVE_FLUIDS:
        test_nearest_pipe_nps()
        test_nearest_pipe_by_od_and_alias()
    else:
        print("test_catalog: SKIP fluids-dependent cases (fluids not installed)")
    test_nearest_pipe_soft_fail()
    test_fitting_dims_hits()
    test_fitting_dims_miss_hint()
    test_available_sizes_nonempty()
    print("test_catalog OK")


if __name__ == "__main__":
    main()
