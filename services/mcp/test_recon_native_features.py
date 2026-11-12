"""Recon feature recovery over exact native SolidWorks imports (no SolidWorks needed).

Two layers:
  - pure recognizer helpers (no engine): seam-split cylinder merging, the exact ring fit for
    an off-origin circular pattern, the blend-setback undo on a cap outline, shell detection;
  - end-to-end over corpora/sw_fixtures: import_file -> exact_native_brep, then
    reverse_engineer must rebuild every fixture to a full boolean match with the editable
    feature that SolidWorks used (hole + pattern, revolve, shell, fillet, chamfer).

The end-to-end layer skips when the core library or the SLDPRT converter's JS runtime is
not available on the host.

Run:  scripts/testbox.py --suite formats
"""
from __future__ import annotations

import math
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp.test_recon_tools import _unshadow_mcp_sdk  # noqa: E402

_unshadow_mcp_sdk()

from services.mcp import core_bridge as cb  # noqa: E402
from services.mcp import recon_tools as rt  # noqa: E402

_FIXTURES = os.environ.get("CHUDWARE_SW_FIXTURES",
                           os.path.join(_REPO_ROOT, "corpora", "sw_fixtures"))


# ---------------------------------------------------------------- pure helpers

def _half(sid, axis_pt, r, side, h, concavity="bore"):
    """A seam-split half cylinder card along +Y, as the native import emits it."""
    d = 2.0 * r / math.pi
    c = [axis_pt[0] + side * d, axis_pt[1], axis_pt[2]]
    return {"sid": sid, "surf": "cylinder", "concavity": concavity, "radius": r,
            "axis": [0.0, 1.0, 0.0], "axis_point": list(axis_pt), "centroid": c,
            "normal": [float(side), 0.0, 0.0], "area_or_len": math.pi * r * h}


def test_sweep_inverts_the_centroid_offset():
    assert rt._sweep_from_offset(2.0 * 4.0 / math.pi, 4.0) == pytest.approx(math.pi, abs=1e-9)
    assert rt._sweep_from_offset(0.0, 4.0) == pytest.approx(2.0 * math.pi, abs=1e-9)


def test_split_halves_merge_into_one_full_cylinder():
    faces = [_half("A", (30.0, 10.0, 0.0), 4.0, +1, 20.0),
             {"sid": "P", "surf": "plane", "centroid": [0, 0, 0], "normal": [0, -1, 0]},
             _half("B", (30.0, 10.0, 0.0), 4.0, -1, 20.0),
             _half("C", (-30.0, 10.0, 0.0), 4.0, +1, 20.0)]
    out = rt._merge_split_cylinders(faces)
    assert [f["sid"] for f in out] == ["A", "P", "C"]
    full = out[0]
    assert full["member_sids"] == ["A", "B"]
    assert full["sweep_deg"] == pytest.approx(360.0, abs=1e-3)
    assert full["centroid"] == pytest.approx([30.0, 10.0, 0.0], abs=1e-6)
    assert full["area_or_len"] == pytest.approx(2.0 * math.pi * 4.0 * 20.0)
    # a lone half stays a half: never promoted to a hole
    assert out[2]["sweep_deg"] == pytest.approx(180.0, abs=1e-3)
    assert not rt._is_full_cylinder(out[2])


def test_ring_fit_recovers_an_off_origin_pattern_axis():
    c0, r = (-0.4, 5.0, 0.0), 25.4
    centers = [(c0[0] + r * math.cos(math.radians(60 * k)), 5.0,
                c0[2] + r * math.sin(math.radians(60 * k))) for k in range(6)]
    ring = rt._ring_fit(centers, (0.0, 1.0, 0.0))
    assert ring is not None
    assert ring["radius"] == pytest.approx(r, abs=1e-6)
    assert ring["point"][0] == pytest.approx(-0.4, abs=1e-6)
    assert ring["point"][2] == pytest.approx(0.0, abs=1e-6)
    # unequal spacing is not a pattern
    centers[1] = (c0[0] + r * math.cos(math.radians(70)), 5.0,
                  c0[2] + r * math.sin(math.radians(70)))
    assert rt._ring_fit(centers, (0.0, 1.0, 0.0)) is None


def test_blend_setback_restores_the_sharp_cap_outline():
    # a 40x30 box with every edge blended 3 mm shows a 34x24 cap
    rect = [(-17, -12), (17, -12), (17, 12), (-17, 12)]
    prof = [{"id": f"e{i}", "type": "line", "p1": list(rect[i]), "p2": list(rect[(i + 1) % 4])}
            for i in range(4)]
    walls = [((1.0, 0.0), 20.0), ((-1.0, 0.0), 20.0), ((0.0, 1.0), 15.0), ((0.0, -1.0), 15.0)]
    out = rt._unset_cap_profile(prof, walls, [3.0])
    corners = sorted(tuple(round(v, 9) for v in e["p1"]) for e in out)
    assert corners == [(-20, -15), (-20, 15), (20, -15), (20, 15)]
    # a setback that matches no detected blend leaves the outline alone
    assert rt._unset_cap_profile(prof, walls, [2.0]) == prof


def _pl(sid, c, n):
    return {"sid": sid, "surf": "plane", "centroid": list(c), "normal": list(n)}


def test_shell_detection_finds_wall_and_open_face():
    bbox = {"min": [-20, 0, -15], "max": [20, 20, 15]}
    outer = [_pl("bot", (0, 10, -15), (0, 0, -1)), _pl("top", (0, 10, 15), (0, 0, 1)),
             _pl("l", (-20, 10, 0), (-1, 0, 0)), _pl("r", (20, 10, 0), (1, 0, 0)),
             _pl("front", (0, 0, 0), (0, -1, 0)), _pl("back", (0, 20, 0), (0, 1, 0))]
    inner = [_pl("il", (-18, 11, 0), (1, 0, 0)), _pl("ir", (18, 11, 0), (-1, 0, 0)),
             _pl("ib", (0, 11, -13), (0, 0, 1)), _pl("it", (0, 11, 13), (0, 0, -1)),
             _pl("floor", (0, 2, 0), (0, 1, 0))]
    sh = rt._detect_shell(outer + inner, bbox)
    assert sh is not None
    assert sh["thickness"] == pytest.approx(2.0)
    assert [o["sid"] for o in sh["open"]] == ["back"]
    assert sorted(sh["inner_sids"]) == sorted(f["sid"] for f in inner)
    # two OPPOSED openings are a tube profile (a plain extrude), not a shell
    assert rt._detect_shell(outer + inner[:4], bbox) is None


# ---------------------------------------------------------------- end-to-end fixtures

# fixture -> feature types the rebuild must contain (besides the base)
_EXPECTED = {
    "linpat_holes": {"extrude", "hole", "linear_pattern"},
    "cirpat_holes": {"extrude", "hole", "circular_pattern"},
    "mirror_holes": {"extrude", "hole"},
    "revolve_ring": {"revolve"},
    "shell_box": {"extrude", "shell"},
    "fillet_box": {"extrude", "fillet"},
    "chamfer_box": {"extrude", "chamfer"},
    "extrude_front_40x30x20": {"extrude"},
}


def _import_native(path: str) -> str:
    try:
        r = cb.call("import_file", {"path": path})
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if any(k in msg for k in ("CHUDWARE_JS_RUNTIME", "CHUDWARE_SLDPRT_ROOT", "converter",
                                  "node")):
            pytest.skip(f"native SLDPRT converter unavailable: {msg[:160]}")
        raise
    if r.get("import_fidelity") != "exact_native_brep":
        pytest.skip(f"native exact import unavailable (got {r.get('import_fidelity')!r})")
    return r["doc_id"]


@pytest.mark.skipif(not cb.available(), reason="libchudware not staged")
@pytest.mark.parametrize("stem", sorted(_EXPECTED))
def test_native_fixture_rebuilds_to_full_match(stem):
    path = os.path.join(_FIXTURES, f"{stem}.SLDPRT")
    if not os.path.isfile(path):
        pytest.skip(f"{path} missing")
    from services.mcp.reveng_common import _call_reverse_engineer

    doc = _import_native(path)
    res = _call_reverse_engineer(doc)
    assert res.get("ok"), res
    assert res["match_pct"] >= 99.9, (stem, res["match_pct"], res.get("rejected"),
                                      res.get("candidates"))
    types = {f["type"] for f in res["features"]}
    assert _EXPECTED[stem] <= types, (stem, types)
    assert not res["rejected"], res["rejected"]
    assert (res.get("recognition") or {}).get("unclaimed_count") == 0, res["recognition"]
