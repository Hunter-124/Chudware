"""PDN v1 tests: analytic validation + fixture behaviour + sidecar shape.

Analytic cases (DoD: within 5% of closed form):
  * uniform strip  — R = R_sheet * L/W
  * annular washer — radial spreading R = R_sheet * ln(r_out/r_in) / (2*pi)
  * via barrel     — R = rho * L / (pi * (r_plated^2 - r_drill^2)), in-circuit
Fixtures:
  * seeded undersized 3 A trace -> flags voltage drop AND current-density hotspots
  * clean generously-sized board -> no violations
  * pour-based board solves well inside the 10 s budget
"""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

from services.ecad import pdn, schema


RHO = pdn.RHO_CU_OHM_M
T_CU = 0.035  # default stackup foil, mm
RS = RHO / (T_CU * 1e-3)  # ohm/sq


# ── fixture helpers ──────────────────────────────────────────────────────────────

def _fp(ref: str, x: float, y: float, net: str, pad_w: float = 1.0, pad_h: float = 1.0,
        layer: str = "F.Cu", eng: dict | None = None, libid: str = "") -> dict:
    fp = {
        "id": f"fp_{ref}", "ref": ref, "fpid": "Test:PAD1", "pos": [x, y],
        "angle": 0.0, "side": "top",
        "pads": [{"id": "p1", "number": "1", "pin": "1", "pos": [0.0, 0.0], "angle": 0.0,
                  "shape": "rect", "size": [pad_w, pad_h], "type": "smd",
                  "layers": [layer], "net": net}],
    }
    if eng:
        fp["eng_overrides"] = eng
    if libid:
        fp["libid"] = libid
    return fp


def _track(tid: str, p1, p2, width: float, net: str, layer: str = "F.Cu") -> dict:
    return {"id": tid, "kind": "segment", "p1": list(p1), "p2": list(p2),
            "width": width, "net": net, "layer": layer}


def _board() -> dict:
    b = schema.new_board("pdn_test")
    b["nets"].append({"name": "+5V", "class": "default"})
    return b


class _StubLib:
    """Library stub: makes ref-prefixed 'REG' components regulator-like (power_out pin 1)."""

    def resolve(self, libid):
        if libid == "Test:REG":
            return {"libid": libid, "pins": [{"number": "1", "etype": "power_out"}]}
        return None

    def resolve_footprint(self, fpid):
        return None


# ── analytic: uniform strip ──────────────────────────────────────────────────────

def test_strip_matches_closed_form():
    """50 mm x 2 mm strip carrying 1 A: drop within 5% of R_sheet * L/W * I."""
    b = _board()
    L_mm, W_mm, I = 50.0, 2.0, 1.0
    # full-width contact pads at both ends, track body between them
    b["tracks"].append(_track("t1", (0, 0), (L_mm, 0), W_mm, "+5V"))
    b["footprints"].append(_fp("J1", 0.0, 0.0, "+5V", pad_w=0.4, pad_h=W_mm))
    b["footprints"].append(_fp("U1", L_mm, 0.0, "+5V", pad_w=0.4, pad_h=W_mm))
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"],
                      sink_overrides={"U1": I}, resolution_mm=0.2)
    assert r["ok"], r
    drop = next(d for d in r["drops"] if d["ref"] == "U1")["drop_v"]
    expected = RS * (L_mm / W_mm) * I
    assert drop == pytest.approx(expected, rel=0.05), (drop, expected)
    # energy conservation: total I2R == I * drop (single 1-sink chain) within 5%
    assert r["i2r_map"]["total_w"] == pytest.approx(I * drop, rel=0.05)


def test_strip_single_cell_chain_is_exact():
    """A one-cell-wide strip is a 1D chain — the discretisation is exact (<1%)."""
    b = _board()
    b["tracks"].append(_track("t1", (0, 0.25), (50, 0.25), 0.5, "+5V"))
    b["footprints"].append(_fp("J1", 0.0, 0.0, "+5V", pad_w=0.5, pad_h=0.5))
    b["footprints"].append(_fp("U1", 50.0, 0.25, "+5V", pad_w=0.5, pad_h=0.5))
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"],
                      sink_overrides={"U1": 2.0}, resolution_mm=0.5)
    assert r["ok"], r
    drop = next(d for d in r["drops"] if d["ref"] == "U1")["drop_v"]
    expected = RS * (50.0 / 0.5) * 2.0
    assert drop == pytest.approx(expected, rel=0.02), (drop, expected)


# ── analytic: annular (radial spreading) ─────────────────────────────────────────

def test_annulus_matches_closed_form():
    """Washer r_in=2, r_out=8 mm, Dirichlet rims: R = RS*ln(r_out/r_in)/(2*pi) ±5%.

    Exercises the 2D mesh machinery directly (grid + network + Dirichlet solve).
    """
    r_in, r_out, res = 2.0, 8.0, 0.1
    b = _board()
    items = {"cu_ids": ["F.Cu"], "tracks": [], "vias": [], "zones": [], "pads": []}
    grid = pdn._Grid(-r_out - 1, -r_out - 1, int(2 * (r_out + 1) / res),
                     int(2 * (r_out + 1) / res), res, ["F.Cu"])
    grid.stamp_disk("F.Cu", 0.0, 0.0, r_out)
    netw = pdn._Network(b, items, grid)
    X, Y = np.meshgrid(grid.xs, grid.ys)
    R2 = X * X + Y * Y
    idx = netw.node_of["F.Cu"]
    inner = idx[(R2 <= r_in * r_in) & (idx >= 0)]
    outer = idx[(R2 >= (r_out - 2 * res) ** 2) & (idx >= 0)]
    L = netw.laplacian().tolil()
    # fix inner at 1 V, outer at 0 V; measure total current through the sheet
    fixed = np.concatenate([inner, outer])
    vfix = np.concatenate([np.ones(inner.size), np.zeros(outer.size)])
    n = netw.n
    mask = np.zeros(n, dtype=bool)
    mask[fixed] = True
    free = np.nonzero(~mask)[0]
    import scipy.sparse as sp
    import scipy.sparse.linalg as spla
    Lc = netw.laplacian()
    Vf = np.zeros(n)
    Vf[fixed] = vfix
    rhs = -(Lc[free][:, fixed] @ Vf[fixed])
    V = Vf.copy()
    V[free] = spla.spsolve(Lc[free][:, free].tocsc(), rhs)
    # total current out of the inner rim
    dv = V[netw.er] - V[netw.ec]
    ie = netw.eg * dv
    cur = np.zeros(n)
    np.add.at(cur, netw.er, -ie)
    np.add.at(cur, netw.ec, ie)
    I_total = float(-cur[inner].sum())
    R_meas = 1.0 / I_total
    # effective radii: Dirichlet regions are r<=r_in and r>=r_out-2res
    expected = RS * math.log((r_out - 2 * res) / r_in) / (2 * math.pi)
    assert R_meas == pytest.approx(expected, rel=0.05), (R_meas, expected)


# ── analytic: via barrel ─────────────────────────────────────────────────────────

def test_via_resistance_closed_form():
    drill, span, plating = 0.3, 1.53, 0.025
    r_i = drill / 2 * 1e-3
    r_o = r_i + plating * 1e-3
    expected = RHO * span * 1e-3 / (math.pi * (r_o ** 2 - r_i ** 2))
    assert pdn.via_resistance(drill, span, plating) == pytest.approx(expected, rel=1e-9)


def test_via_in_circuit():
    """F.Cu chain -> via -> B.Cu chain: total R = 2 chains + barrel, within 5%."""
    b = _board()
    b["tracks"].append(_track("t1", (0, 0.25), (20, 0.25), 0.5, "+5V", "F.Cu"))
    b["tracks"].append(_track("t2", (20, 0.25), (40, 0.25), 0.5, "+5V", "B.Cu"))
    b["vias"].append({"id": "v1", "at": [20.0, 0.25], "diameter": 0.6, "drill": 0.3,
                      "type": "through", "layers": ["F.Cu", "B.Cu"], "net": "+5V"})
    b["footprints"].append(_fp("J1", 0.0, 0.25, "+5V", pad_w=0.5, pad_h=0.5, layer="F.Cu"))
    b["footprints"].append(_fp("U1", 40.0, 0.25, "+5V", pad_w=0.5, pad_h=0.5, layer="B.Cu"))
    I = 2.0
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"],
                      sink_overrides={"U1": I}, resolution_mm=0.5)
    assert r["ok"], r
    drop = next(d for d in r["drops"] if d["ref"] == "U1")["drop_v"]
    span = pdn._dielectric_span_mm(b, "F.Cu", "B.Cu")
    expected = (RS * (40.0 / 0.5) + pdn.via_resistance(0.3, span)) * I
    assert drop == pytest.approx(expected, rel=0.05), (drop, expected)
    # the via shows up in the I2R map with its own dissipation
    vias = r["i2r_map"]["vias"]
    assert len(vias) == 1 and vias[0]["p_w"] == pytest.approx(
        I * I * pdn.via_resistance(0.3, span), rel=0.05)


# ── fixtures: undersized 3 A trace vs clean board ────────────────────────────────

def _rail_eng(i_a: float, v_min: float = 4.75) -> dict:
    return {"supply": {"rails": [{"name": "+5V", "voltage_v_min": v_min,
                                  "voltage_v_nom": 5.0, "voltage_v_max": 5.25,
                                  "current_a_active": i_a, "current_a_sleep": None}],
                       "voltage_domains": ["+5V"], "sequencing": []},
            "sources": ["test fixture"]}


def test_undersized_3a_trace_flags_drop_and_density():
    """0.3 mm trace @ 3 A: expect ~148 mV drop (v_min 4.9 violated) + J >> 30 A/mm²."""
    b = _board()
    b["tracks"].append(_track("t1", (0, 0), (30, 0), 0.3, "+5V"))
    b["footprints"].append(_fp("J1", 0.0, 0.0, "+5V", pad_w=0.3, pad_h=0.3))
    b["footprints"].append(_fp("U1", 30.0, 0.0, "+5V", pad_w=0.3, pad_h=0.3,
                               eng=_rail_eng(3.0, v_min=4.9)))
    r = pdn.solve_pdn(b, "+5V", source_refs=["J1"])  # vsrc from rail metadata (5.0)
    assert r["ok"], r
    assert r["vsrc"] == 5.0  # inferred from the rail metadata
    u1 = next(d for d in r["drops"] if d["ref"] == "U1")
    assert u1["i_a"] == 3.0                      # sink current from engineering metadata
    assert u1["within_tolerance"] is False       # delivered < voltage_v_min
    assert u1["drop_v"] > 0.1
    # current density on the 0.3 mm trace: 3 / (0.3*0.035) ~ 286 A/mm²
    assert r["max_j_a_mm2"] > 100
    assert r["hotspots"], "expected current-density hotspots"
    assert any("delivered" in v for v in r["violations"])
    assert any("current density" in v for v in r["violations"])


def test_clean_board_passes():
    b = _board()
    b["tracks"].append(_track("t1", (0, 0), (30, 0), 3.0, "+5V"))
    b["footprints"].append(_fp("J1", 0.0, 0.0, "+5V", pad_w=1.0, pad_h=3.0))
    b["footprints"].append(_fp("U1", 30.0, 0.0, "+5V", pad_w=1.0, pad_h=3.0,
                               eng=_rail_eng(0.1)))
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"])
    assert r["ok"], r
    u1 = next(d for d in r["drops"] if d["ref"] == "U1")
    assert u1["within_tolerance"] is True
    assert r["violations"] == []
    assert r["hotspots"] == []


def test_unknown_sink_reported_not_zeroed():
    """A component with no rail metadata lands in unknown_sinks (honesty rule)."""
    b = _board()
    b["tracks"].append(_track("t1", (0, 0), (10, 0), 1.0, "+5V"))
    b["footprints"].append(_fp("J1", 0.0, 0.0, "+5V"))
    b["footprints"].append(_fp("U9", 10.0, 0.0, "+5V"))  # no eng metadata
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"])
    assert r["ok"]
    assert "U9" in r["unknown_sinks"]
    u9 = next(d for d in r["drops"] if d["ref"] == "U9")
    assert u9["i_a"] is None


def test_disconnected_sink_flagged():
    b = _board()
    b["tracks"].append(_track("t1", (0, 0), (10, 0), 1.0, "+5V"))
    b["tracks"].append(_track("t2", (30, 30), (40, 30), 1.0, "+5V"))  # island
    b["footprints"].append(_fp("J1", 0.0, 0.0, "+5V"))
    b["footprints"].append(_fp("U1", 40.0, 30.0, "+5V", eng=_rail_eng(0.5)))
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"])
    assert r["ok"]
    u1 = next(d for d in r["drops"] if d["ref"] == "U1")
    assert u1["connected"] is False
    assert any("not connected" in v for v in r["violations"])


def test_auto_source_detection_via_power_out_pin():
    b = _board()
    b["tracks"].append(_track("t1", (0, 0), (10, 0), 1.0, "+5V"))
    b["footprints"].append(_fp("VR1", 0.0, 0.0, "+5V", libid="Test:REG"))
    b["footprints"].append(_fp("U1", 10.0, 0.0, "+5V", eng=_rail_eng(0.2)))
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, library=_StubLib())
    assert r["ok"], r
    assert r["sources"] == ["VR1"]


def test_no_source_is_actionable_error():
    b = _board()
    b["tracks"].append(_track("t1", (0, 0), (10, 0), 1.0, "+5V"))
    b["footprints"].append(_fp("U1", 10.0, 0.0, "+5V"))
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, library=_StubLib())
    assert not r["ok"] and "source" in r["error"]


# ── sidecar extension shape (the thermal-handoff seam) ─────────────────────────

def test_i2r_sidecar_extension_shape():
    b = _board()
    b["tracks"].append(_track("t1", (0, 0), (30, 0), 0.5, "+5V"))
    b["footprints"].append(_fp("J1", 0.0, 0.0, "+5V"))
    b["footprints"].append(_fp("U1", 30.0, 0.0, "+5V", eng=_rail_eng(1.0)))
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"])
    assert r["ok"]
    sidecar = {"schema": "aicad.boardphys/1", "board": {"name": "x"}, "components": []}
    out = pdn.attach_pdn(sidecar, [r])
    blk = out["pdn"]
    assert blk["schema"] == "aicad.pdn/1"
    net = blk["nets"][0]
    assert net["net"] == "+5V" and net["vsrc"] == 5.0
    m = net["i2r_map"]
    assert m["columns"] == ["layer", "x_mm", "y_mm", "p_w"]
    assert m["cell_size_mm"] > 0
    assert m["copper_thickness_mm"]["F.Cu"] == pytest.approx(T_CU)
    assert m["cells"], "expected per-cell dissipation rows"
    row = m["cells"][0]
    assert row[0] in ("F.Cu", "B.Cu") and isinstance(row[3], float)
    # map totals reconcile: cells + vias + omitted == total (within rounding)
    cell_sum = sum(c[3] for c in m["cells"])
    via_sum = sum(v["p_w"] for v in m["vias"])
    assert cell_sum + via_sum + m["omitted_w"] == pytest.approx(m["total_w"], abs=1e-6)


# ── runtime budget ───────────────────────────────────────────────────────────────

def test_pour_board_solves_inside_budget():
    """Demo-class board (50x50 mm pour + tracks) well inside the 10 s DoD budget."""
    b = _board()
    poly = [[0, 0], [50, 0], [50, 50], [0, 50]]
    b["zones"].append({"id": "z1", "layer": "F.Cu", "net": "+5V",
                       "polygon": poly, "filled_polys": [poly], "fill": "solid"})
    for k in range(6):
        b["footprints"].append(_fp(f"U{k+1}", 5.0 + 7 * k, 25.0, "+5V",
                                   eng=_rail_eng(0.25)))
    b["footprints"].append(_fp("J1", 2.0, 2.0, "+5V"))
    t0 = time.time()
    r = pdn.solve_pdn(b, "+5V", vsrc=5.0, source_refs=["J1"], resolution_mm=0.25)
    dt = time.time() - t0
    assert r["ok"], r
    assert dt < 10.0, f"solve took {dt:.1f}s"
    assert all(d["within_tolerance"] for d in r["drops"] if d["v_min"] is not None)
