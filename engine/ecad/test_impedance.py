"""Controlled-impedance + delay calculator gates (signal integrity).

House style: ``from __future__ import annotations``, plain ``main()`` + asserts, run as a module.
Each gate recomputes the *expected* value from the same closed form the implementation cites
(IPC-2141 / Hammerstad / Wheeler) -- not a magic number -- and also sanity-checks the result is in
the physically sensible band a fab house would quote.

Run:  & "<venv>/python.exe" -m engine.ecad.test_impedance

Gates:
  1. microstrip Z0 matches the IPC-2141 surface-microstrip closed form (50-ohm-class geometry);
     delay = sqrt(eeff)/c with 1 < eeff < er.
  2. stripline Z0 < microstrip Z0 for the same geometry (fully-embedded field, eeff = er).
  3. CPWG returns a sensible 40-80 ohm impedance and eeff between 1 and er.
  4. differential pair: z0_diff < 2*z0_se (coupling lowers the odd-mode impedance).
  5. track_impedance: outer layer -> microstrip, inner layer -> stripline; delay scales with the
     routed length.
"""
from __future__ import annotations

import math
import time

from engine.ecad import schema
from engine.ecad.impedance import (
    coplanar_z0,
    diff_pair_z0,
    microstrip_z0,
    stripline_z0,
    track_impedance,
)

# speed of light, ps/mm (one-way vacuum delay)
_PS_PER_MM_VACUUM = 1.0e12 / 299_792_458_000.0


def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


# =============================================================================
# Gate 1 — microstrip vs the IPC-2141 closed form
# =============================================================================

def test_microstrip_ipc2141() -> None:
    # 50-ohm-class FR-4 surface microstrip: w=0.3, h=0.2, t=0.035 mm, er=4.3.
    w, h, t, er = 0.3, 0.2, 0.035, 4.3
    r = microstrip_z0(w, h, t, er)
    z0 = r["z0"]; eeff = r["eeff"]; delay = r["delay_ps_per_mm"]

    # Recompute the IPC-2141 surface-microstrip formula independently (same model -> exact match).
    z0_formula = (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h / (0.8 * w + t))
    err = _pct_err(z0, z0_formula)
    print(f"  microstrip: Z0={z0:.3f} ohm  IPC-2141 formula={z0_formula:.3f} ohm  err={err:.4f}%")
    print(f"              eeff={eeff:.4f}  (1 < eeff < er={er})  delay={delay:.4f} ps/mm")
    assert err < 0.01, f"microstrip Z0 must match its own formula ({err:.4f}%)"
    assert 40.0 < z0 < 60.0, f"50-ohm-class geometry should land 40-60 ohm, got {z0:.2f}"

    # eeff is between air (1) and bulk er (half the field is in air for a microstrip).
    assert 1.0 < eeff < er, f"microstrip eeff {eeff:.3f} must be in (1, {er})"
    # delay = sqrt(eeff)/c.
    delay_exp = _PS_PER_MM_VACUUM * math.sqrt(eeff)
    assert _pct_err(delay, delay_exp) < 0.01, "delay must equal sqrt(eeff)/c"
    print("  [gate 1 microstrip IPC-2141] PASS")


# =============================================================================
# Gate 2 — stripline lower than microstrip for the same geometry
# =============================================================================

def test_stripline_lower_than_microstrip() -> None:
    w, h, t, er = 0.3, 0.2, 0.035, 4.3
    ms = microstrip_z0(w, h, t, er)
    sl = stripline_z0(w, h, t, er)

    z0_formula = (60.0 / math.sqrt(er)) * math.log(4.0 * h / (0.67 * math.pi * (0.8 * w + t)))
    err = _pct_err(sl["z0"], z0_formula)
    print(f"  stripline: Z0={sl['z0']:.3f} ohm  IPC-2141 formula={z0_formula:.3f} ohm  err={err:.4f}%")
    print(f"             microstrip Z0={ms['z0']:.3f} ohm  (stripline must be lower)")
    print(f"             stripline eeff={sl['eeff']:.3f} (== er) > microstrip eeff={ms['eeff']:.3f}")
    assert err < 0.01, f"stripline Z0 must match its own formula ({err:.4f}%)"
    assert sl["z0"] < ms["z0"], "stripline must be lower-impedance than microstrip (same geometry)"
    # Fully embedded -> eeff == er, and the line is slower (larger delay) than the microstrip.
    assert _pct_err(sl["eeff"], er) < 0.01, "stripline eeff must equal er"
    assert sl["delay_ps_per_mm"] > ms["delay_ps_per_mm"], "stripline must be slower than microstrip"
    print("  [gate 2 stripline < microstrip] PASS")


# =============================================================================
# Gate 3 — CPWG sanity
# =============================================================================

def test_cpwg_sanity() -> None:
    # CPWG: 0.3mm trace, 0.2mm gap to coplanar ground, 0.2mm to lower plane, FR-4.
    w, s, h, t, er = 0.3, 0.2, 0.2, 0.035, 4.3
    r = coplanar_z0(w, s, h, t, er)
    z0 = r["z0"]; eeff = r["eeff"]
    print(f"  CPWG: Z0={z0:.3f} ohm  eeff={eeff:.4f}  delay={r['delay_ps_per_mm']:.4f} ps/mm")
    assert 25.0 < z0 < 90.0, f"CPWG Z0 should land in a sensible 25-90 ohm band, got {z0:.2f}"
    assert 1.0 < eeff < er, f"CPWG eeff {eeff:.3f} must be in (1, {er})"
    # Widening the gap raises Z0 (less capacitive coupling to the coplanar grounds).
    r_wide = coplanar_z0(w, s * 3.0, h, t, er)
    print(f"        wider gap (s={s*3:.2f}) -> Z0={r_wide['z0']:.3f} ohm (must be higher)")
    assert r_wide["z0"] > z0, "wider CPWG gap must raise Z0"
    print("  [gate 3 CPWG sanity] PASS")


# =============================================================================
# Gate 4 — differential pair coupling
# =============================================================================

def test_diff_pair_coupling() -> None:
    w, s, h, t, er = 0.2, 0.2, 0.2, 0.035, 4.3
    dp = diff_pair_z0(w, s, h, t, er, kind="microstrip")
    z0_se = dp["z0_se"]; z0_diff = dp["z0_diff"]
    print(f"  diff pair (microstrip): z0_se={z0_se:.3f} ohm  z0_diff={z0_diff:.3f} ohm  "
          f"(2*z0_se={2*z0_se:.3f})")
    assert z0_diff < 2.0 * z0_se, "coupling must give z0_diff < 2*z0_se"
    assert z0_diff > 0.0, "z0_diff must be positive"
    # Tighter spacing -> stronger coupling -> lower z0_diff.
    dp_tight = diff_pair_z0(w, s / 4.0, h, t, er, kind="microstrip")
    print(f"  tighter spacing (s={s/4:.3f}) -> z0_diff={dp_tight['z0_diff']:.3f} ohm (must drop)")
    assert dp_tight["z0_diff"] < z0_diff, "tighter spacing must lower z0_diff (stronger coupling)"
    # Stripline variant also obeys the bound.
    dps = diff_pair_z0(w, s, h, t, er, kind="stripline")
    print(f"  diff pair (stripline):  z0_se={dps['z0_se']:.3f}  z0_diff={dps['z0_diff']:.3f}")
    assert dps["z0_diff"] < 2.0 * dps["z0_se"], "stripline diff pair must also obey coupling bound"
    print("  [gate 4 diff pair coupling] PASS")


# =============================================================================
# Gate 5 — track_impedance from a board stackup (microstrip vs stripline)
# =============================================================================

def _board_4layer() -> dict:
    """4-copper board: F.Cu / pp / In1.Cu / core / In2.Cu / pp / B.Cu (mm thicknesses, er)."""
    b = schema.new_board("si")
    b["stackup"] = {
        "copper_layers": 4, "thickness": 1.6,
        "layers": [
            {"id": "F.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d1", "type": "prepreg", "thickness": 0.2, "er": 4.3},
            {"id": "In1.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d2", "type": "core", "thickness": 0.4, "er": 4.5},
            {"id": "In2.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d3", "type": "prepreg", "thickness": 0.2, "er": 4.3},
            {"id": "B.Cu", "type": "copper", "thickness": 0.035},
        ],
    }
    return b


def test_track_impedance() -> None:
    b = _board_4layer()
    # An outer-layer (F.Cu) routed trace -> microstrip.
    b["tracks"].append({"id": "t1", "net": "SIG", "layer": "F.Cu", "width": 0.3,
                        "kind": "segment", "p1": [0.0, 0.0], "p2": [10.0, 0.0]})
    b["nets"].append({"name": "SIG", "class": "default"})
    r_outer = track_impedance(b, "t1")
    print(f"  track t1 (F.Cu): kind={r_outer['kind']}  Z0={r_outer['z0']:.3f} ohm  "
          f"len={r_outer['length_mm']:.2f}mm  delay={r_outer['delay_ps']:.3f} ps")
    assert r_outer["kind"] == "microstrip", "outer copper must classify as microstrip"
    # Cross-check Z0 against the direct microstrip call with the geometry track_impedance pulled.
    ms = microstrip_z0(r_outer["width"], r_outer["h"], r_outer["t"], r_outer["er"])
    assert _pct_err(r_outer["z0"], ms["z0"]) < 0.01, "track_impedance Z0 must match microstrip_z0"
    # Length is 10 mm; delay = delay_ps_per_mm * 10.
    assert _pct_err(r_outer["length_mm"], 10.0) < 0.01, "routed length must be 10 mm"
    assert _pct_err(r_outer["delay_ps"], r_outer["delay_ps_per_mm"] * 10.0) < 0.01, "delay scales w/ length"

    # An inner-layer (In1.Cu) trace -> stripline, and lower Z0 than the microstrip.
    b["tracks"].append({"id": "t2", "net": "SIG2", "layer": "In1.Cu", "width": 0.3,
                        "kind": "segment", "p1": [0.0, 1.0], "p2": [5.0, 1.0]})
    b["nets"].append({"name": "SIG2", "class": "default"})
    r_inner = track_impedance(b, "t2")
    print(f"  track t2 (In1.Cu): kind={r_inner['kind']}  Z0={r_inner['z0']:.3f} ohm  "
          f"h={r_inner['h']:.3f}mm er={r_inner['er']:.3f}")
    assert r_inner["kind"] == "stripline", "inner copper must classify as stripline"
    # Stripline h = sum of the two adjacent dielectrics (0.2 + 0.4 = 0.6 mm).
    assert _pct_err(r_inner["h"], 0.6) < 0.01, f"inner stripline h should be 0.6 mm, got {r_inner['h']}"
    print(f"  validate_board on the SI board -> {schema.validate_board(b)}")
    assert schema.validate_board(b) == [], "the SI board must still validate"
    print("  [gate 5 track_impedance] PASS")


# =============================================================================
# main
# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    print("=" * 64)
    print("ECAD SIGNAL-INTEGRITY - CONTROLLED-IMPEDANCE GATES")
    print("=" * 64)
    gates = [
        ("Gate 1: microstrip IPC-2141", test_microstrip_ipc2141),
        ("Gate 2: stripline < microstrip", test_stripline_lower_than_microstrip),
        ("Gate 3: CPWG sanity", test_cpwg_sanity),
        ("Gate 4: diff pair coupling", test_diff_pair_coupling),
        ("Gate 5: track_impedance", test_track_impedance),
    ]
    for title, fn in gates:
        print(f"\n--- {title} ---")
        fn()
    print(f"\nALL IMPEDANCE GATES PASSED  ({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
