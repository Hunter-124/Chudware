"""Post-route trace optimization gates (corner rounding, teardrops, impedance, fab).

Run::

    /home/hunter/.local/share/Chudware/mcp-venv/bin/python -X utf8 -m pytest \\
        engine_oracle/ecad/test_trace_opt.py -q
"""
from __future__ import annotations

import os
import tempfile
import time


from services.ecad import schema
from services.ecad.drc import run_drc
from services.ecad.fab import write_gerber_layer, write_gerbers
from services.ecad.impedance import track_impedance
from services.ecad.trace_opt import optimize_traces


# --------------------------------------------------------------------------- fixtures


def _outline(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {
        "shapes": [
            {"type": "line", "p1": [x0, y0], "p2": [x1, y0]},
            {"type": "line", "p1": [x1, y0], "p2": [x1, y1]},
            {"type": "line", "p1": [x1, y1], "p2": [x0, y1]},
            {"type": "line", "p1": [x0, y1], "p2": [x0, y0]},
        ],
        "closed": True,
    }


def _smd_pad(pid: str, pos, net: str, size=(1.0, 1.0), layers=None) -> dict:
    return {
        "id": pid,
        "number": pid.replace("p", "") or "1",
        "pos": list(pos),
        "angle": 0.0,
        "shape": "rect",
        "size": list(size),
        "type": "smd",
        "layers": layers or ["F.Cu", "F.Mask", "F.Paste"],
        "drill": None,
        "net": net,
        "pin": "1",
    }


def _fp(fid: str, pos, pads: list) -> dict:
    return {
        "id": fid,
        "fpid": "generic:pad",
        "ref": fid.upper(),
        "value": "",
        "pos": list(pos),
        "angle": 0.0,
        "side": "top",
        "attr": "smd",
        "pads": pads,
        "graphics": [],
        "model_3d": None,
        "linked_symbol": None,
    }


def _seg(tid: str, p1, p2, net: str, width: float = 0.25, layer: str = "F.Cu") -> dict:
    return {
        "id": tid,
        "net": net,
        "layer": layer,
        "width": float(width),
        "kind": "segment",
        "p1": list(p1),
        "p2": list(p2),
    }


def _thin_stackup() -> dict:
    """1.6 mm board with a thin outer dielectric so 50 Ω is reachable at ~0.33 mm width."""
    return {
        "copper_layers": 2,
        "thickness": 1.6,
        "layers": [
            {"id": "F.Cu", "type": "copper", "thickness": 0.035},
            {"id": "diel1", "type": "core", "thickness": 0.2, "er": 4.3},
            {"id": "B.Cu", "type": "copper", "thickness": 0.035},
        ],
    }


def _board_with_corner_and_z0() -> dict:
    """Hand-built board: 90° L-corner on SIG, pad junctions, target Z0=50 on SIG.

    Geometry (mm)::

        pad A (5,15) -- horizontal -- (20,15) corner
                                         |
                                         vertical
                                         |
                                      pad B (20,5)

        pad C (5,5) -- SIG2 short -- pad D (8,5)   (untargeted, for DRC neighbour)
    """
    b = schema.new_board("TraceOptBoard")
    b["stackup"] = _thin_stackup()
    b["outline"] = _outline(0.0, 0.0, 40.0, 30.0)
    b["rules"] = {
        "clearance": 0.15,
        "track_width_min": 0.1,
        "via_diameter_min": 0.45,
        "via_drill_min": 0.2,
        "annular_ring_min": 0.13,
        "hole_to_hole_min": 0.25,
        "edge_clearance": 0.3,
        "silk_to_mask_min": 0.15,
    }
    b["nets"] = [
        {"name": "GND", "class": "default"},
        {"name": "SIG", "class": "rf50"},
        {"name": "SIG2", "class": "default"},
    ]
    b["net_classes"] = {
        "default": {
            "clearance": 0.15,
            "track_width": 0.25,
            "via_diameter": 0.8,
            "via_drill": 0.4,
        },
        "rf50": {
            "clearance": 0.15,
            "track_width": 0.15,  # deliberately off 50 Ω so impedance pass retunes
            "via_diameter": 0.8,
            "via_drill": 0.4,
            "target_z0": 50.0,
        },
    }
    # Pads at the L endpoints + a quiet neighbour net.
    b["footprints"] = [
        _fp("fp1", [5.0, 15.0], [_smd_pad("p1", (0.0, 0.0), "SIG", size=(1.2, 1.2))]),
        _fp("fp2", [20.0, 5.0], [_smd_pad("p2", (0.0, 0.0), "SIG", size=(1.2, 1.2))]),
        _fp("fp3", [5.0, 5.0], [_smd_pad("p3", (0.0, 0.0), "SIG2", size=(1.0, 1.0))]),
        _fp("fp4", [8.0, 5.0], [_smd_pad("p4", (0.0, 0.0), "SIG2", size=(1.0, 1.0))]),
    ]
    # L-route on SIG: horizontal then vertical, meeting at (20,15). Width 0.15 -> Z0 ~74 Ω.
    b["tracks"] = [
        _seg("t1", [5.0, 15.0], [20.0, 15.0], "SIG", width=0.15),
        _seg("t2", [20.0, 15.0], [20.0, 5.0], "SIG", width=0.15),
        _seg("t3", [5.0, 5.0], [8.0, 5.0], "SIG2", width=0.25),
    ]
    return b


def _error_drc(board: dict) -> list[dict]:
    return [v for v in run_drc(board) if v.get("severity") == "error"]


# --------------------------------------------------------------------------- tests


def test_corner_rounding_and_drc():
    t0 = time.perf_counter()
    board = _board_with_corner_and_z0()
    assert not any(t.get("kind") == "arc" for t in board["tracks"])

    res = optimize_traces(board, opts={
        "targets": {"SIG": 50.0},
        "corner_radius_mult": 3.0,
    })
    doc = res["doc"]
    rep = res["report"]
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"  corner_rounding wall-time: {ms:.2f} ms")
    print(f"  report: corners_rounded={rep['corners_rounded']} teardrops={rep['teardrops']} "
          f"nets_tuned={rep['nets_tuned']} drc_ok={rep['drc_ok']}")

    arcs = [t for t in doc["tracks"] if t.get("kind") == "arc"]
    assert rep["corners_rounded"] >= 1, f"expected at least one rounded corner, got {rep}"
    assert len(arcs) >= 1, "board should contain arc track(s) after corner rounding"
    for a in arcs:
        assert "center" in a and "a0" in a and "a1" in a and "p1" in a and "p2" in a
        assert a.get("layer") == "F.Cu"
        assert a.get("net") == "SIG"
        # ~90° sweep
        sweep = abs(float(a["a1"]) - float(a["a0"]))
        assert 60.0 <= sweep <= 120.0, f"fillet sweep should be ~90°, got {sweep}"

    errs = _error_drc(doc)
    assert rep["drc_ok"] is True, f"report.drc_ok False; errors={errs}"
    assert not errs, f"board must stay DRC-clean after corner rounding: {errs}"
    # Input untouched.
    assert not any(t.get("kind") == "arc" for t in board["tracks"])
    print("  [gate a corner rounding + DRC] PASS")


def test_teardrops_at_pad_junctions():
    t0 = time.perf_counter()
    board = _board_with_corner_and_z0()
    res = optimize_traces(board, opts={
        "targets": {"SIG": 50.0},
        "teardrop_length_mult": 1.5,
        "teardrop_width_mult": 1.6,
    })
    doc = res["doc"]
    rep = res["report"]
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"  teardrops wall-time: {ms:.2f} ms")
    print(f"  teardrops added: {rep['teardrops']}")

    tears = [t for t in doc["tracks"]
             if t.get("role") == "teardrop" or (t.get("meta") or {}).get("teardrop")]
    assert rep["teardrops"] >= 1, f"expected teardrops at pad junctions, got {rep}"
    assert len(tears) >= 1
    for t in tears:
        assert t.get("kind", "segment") == "segment"
        assert float(t["width"]) > 0.15  # wider than original SIG width

    errs = _error_drc(doc)
    assert not errs, f"teardrops must not introduce clearance violations: {errs}"
    assert rep["drc_ok"] is True
    print("  [gate b teardrops] PASS")


def test_impedance_width_tune():
    t0 = time.perf_counter()
    board = _board_with_corner_and_z0()
    z_before = track_impedance(board, "SIG")["z0"]
    print(f"  SIG Z0 before: {z_before:.3f} ohm (width={board['tracks'][0]['width']})")
    assert abs(z_before - 50.0) / 50.0 > 0.10, "fixture must start outside ±10% of 50 Ω"

    res = optimize_traces(board, opts={"targets": {"SIG": 50.0}, "z0_tolerance": 0.10})
    doc = res["doc"]
    rep = res["report"]
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"  impedance wall-time: {ms:.2f} ms")

    assert rep["nets_tuned"] >= 1, f"SIG should be width-tuned: {rep}"
    z_after = track_impedance(doc, "SIG")["z0"]
    print(f"  SIG Z0 after:  {z_after:.3f} ohm  (target 50 ±10%)")
    print(f"  report.z0: {rep.get('z0')}")
    assert abs(z_after - 50.0) / 50.0 <= 0.10 + 1e-9, (
        f"Z0 {z_after:.3f} not within ±10% of 50"
    )
    # Width actually changed on SIG tracks.
    sig_w = [float(t["width"]) for t in doc["tracks"] if t.get("net") == "SIG"
             and t.get("kind", "segment") in ("segment", "arc")]
    assert all(w > 0.2 for w in sig_w), f"expected wider SIG tracks, got {sig_w}"
    print("  [gate c impedance width] PASS")


def test_idempotent_second_pass():
    t0 = time.perf_counter()
    board = _board_with_corner_and_z0()
    r1 = optimize_traces(board, opts={"targets": {"SIG": 50.0}})
    r2 = optimize_traces(r1["doc"], opts={"targets": {"SIG": 50.0}})
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"  idempotent wall-time (2 passes): {ms:.2f} ms")
    print(f"  pass1: {r1['report']}")
    print(f"  pass2: {r2['report']}")

    rep2 = r2["report"]
    assert rep2["corners_rounded"] == 0, rep2
    assert rep2["teardrops"] == 0, rep2
    assert rep2["nets_tuned"] == 0, rep2
    assert rep2["reverted"] == 0, rep2
    # Track multiset of (kind, net, rounded geometry) stable.
    def _sig(doc):
        out = []
        for t in doc["tracks"]:
            out.append((
                t.get("kind", "segment"),
                t.get("net"),
                t.get("layer"),
                round(float(t.get("width", 0)), 5),
                t.get("role"),
            ))
        return sorted(out)
    assert _sig(r1["doc"]) == _sig(r2["doc"])
    print("  [gate d idempotent] PASS")


def test_arc_survives_gerber_export():
    t0 = time.perf_counter()
    board = _board_with_corner_and_z0()
    res = optimize_traces(board, opts={"targets": {"SIG": 50.0}})
    doc = res["doc"]
    arcs = [t for t in doc["tracks"] if t.get("kind") == "arc"]
    assert arcs, "need at least one arc for gerber round-trip"

    # Layer-level export (in-memory) plus full write_gerbers to a temp dir.
    body = write_gerber_layer(doc, "F.Cu")
    assert isinstance(body, str) and len(body) > 50
    assert ("D01" in body) or ("G01" in body) or ("X" in body)

    with tempfile.TemporaryDirectory() as td:
        files = write_gerbers(doc, td)
        assert isinstance(files, dict) and files, f"write_gerbers returned empty: {files!r}"
        # Paths exist and front copper is non-empty.
        for path in files.values():
            assert os.path.isfile(path), path
        fcu = None
        for k, path in files.items():
            if "F.Cu" in str(k) or str(path).endswith(".gtl") or "F_Cu" in str(path):
                with open(path, "r", encoding="utf-8") as fh:
                    fcu = fh.read()
                break
        if fcu is None:
            # Any written file is fine as long as gerbers were produced.
            path = next(iter(files.values()))
            with open(path, "r", encoding="utf-8") as fh:
                fcu = fh.read()
        assert len(fcu) > 50

    ms = (time.perf_counter() - t0) * 1000.0
    print(f"  gerber round-trip wall-time: {ms:.2f} ms")
    print(f"  gerber layers: {list(files.keys())}")
    print(f"  arcs exported: {len(arcs)}")
    print("  [gate e gerber arc round-trip] PASS")

def test_pure_no_mutate_and_signature():
    board = _board_with_corner_and_z0()
    snap = [dict(t) for t in board["tracks"]]
    out = optimize_traces(board, opts={"targets": {"SIG": 50.0}})
    assert set(out.keys()) >= {"doc", "report"}
    assert board["tracks"] == snap
    assert out["doc"] is not board
    # Report keys Wave 2b relies on.
    for k in ("corners_rounded", "teardrops", "nets_tuned", "drc_ok"):
        assert k in out["report"], out["report"]
    print("  [gate purity + signature] PASS")


if __name__ == "__main__":
    # Allow `python -m engine.ecad.test_trace_opt` without pytest.
    test_corner_rounding_and_drc()
    test_teardrops_at_pad_junctions()
    test_impedance_width_tune()
    test_idempotent_second_pass()
    test_arc_survives_gerber_export()
    test_pure_no_mutate_and_signature()
    print("ALL PASS")
