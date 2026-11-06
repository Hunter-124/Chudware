"""Elastic stress concentration and steel fatigue notch factors.

Roark's Formulas for Stress and Strain, 8th ed., Table 6-1: I-7,
II-2 and II-15, III-1 and III-2. The coefficients are Pilkey/Peterson
polynomial chart fits, not finite-element results. The reference dimensions,
loading and nominal stresses are essential; the factor is not transferable
between net and gross section stresses. At h/r=2 the upper fit is used.

Steel fatigue: Shigley's Mechanical Engineering Design, 10th ed., section 6-8,
Neuber's notch sensitivity fits for axial/bending and torsion, with S_ut in ksi
and sqrt(a) in sqrt(in). Limit the steel fit to 50–250 ksi; see also
https://www.quadco.engineering/en/know-how/stress-concentration-factor-calculator.htm.
Kf = 1 + q(Kt-1) is the Peterson notch-sensitivity relation. This is an
estimate of fatigue strength reduction, not a local elastic-plastic stress.
"""
from __future__ import annotations

import math

from .registry import calc_envelope, calc_fail, register

_ROARK = ("Roark's Formulas for Stress and Strain, 8th ed., Table 6-1, "
          "https://s1b0d3d77136c1679.jimcontent.com/download/version/1531108625/module/14143373530/name/Roark-Ch06.pdf")
_FATIGUE = ("Shigley's Mechanical Engineering Design, 10th ed., §6-8, "
            "Neuber steel notch sensitivity fits and Peterson Kf relation; "
            "https://rbb.union.edu/courses/mer419/content/data/Lectures/MER419%20L03%20Fatigue%20Review%20III.pdf")
_KSI_MPA = 6.894757293168

# Each row is (constant, sqrt(h/r), h/r) for C1 through C4.
_FITS = {
    ('shaft_shoulder', 'tension'): (
        ((.926, 1.157, -.099), (.012, -3.036, .961), (-.302, 3.977, -1.744), (.365, -2.098, .878)),
        ((1.200, .860, -.022), (-1.805, -.346, -.038), (2.198, -.486, .165), (-.593, -.028, -.106))),
    ('shaft_shoulder', 'bending'): (
        ((.947, 1.206, -.131), (.022, -3.405, .915), (.869, 1.777, -.555), (-.810, .422, -.260)),
        ((1.232, .832, -.008), (-3.813, .968, -.260), (7.423, -4.868, .869), (-3.839, 3.070, -.600))),
    ('shaft_shoulder', 'torsion'): (
        ((.905, .783, -.075), (-.437, -1.969, .553), (1.557, 1.073, -.578), (-1.061, .171, .086)),),
    ('flat_shoulder', 'tension'): (
        ((1.006, 1.008, -.044), (-.115, -.584, .315), (.245, -1.006, -.257), (-.135, .582, -.017)),
        ((1.020, 1.009, -.048), (-.065, -.165, -.007), (-3.459, 1.266, -.016), (3.505, -2.109, .069))),
    ('flat_shoulder', 'bending'): (
        ((1.006, .967, .013), (-.270, -2.372, .708), (.662, 1.157, -.908), (-.405, .249, -.200)),
        ((1.058, 1.002, -.038), (-3.652, 1.639, -.436), (6.170, -5.687, 1.175), (-2.558, 3.046, -.701))),
    ('shaft_u_groove', 'tension'): (
        ((.890, 2.208, -.094), (-.923, -6.678, 1.638), (2.893, 6.448, -2.516), (-1.912, -1.944, .963)),
        ((1.037, 1.967, .002), (-2.679, -2.980, -.053), (3.090, 2.124, .165), (-.424, -1.153, -.106))),
    ('shaft_u_groove', 'bending'): (
        ((.594, 2.958, -.520), (.422, -10.545, 2.692), (.501, 14.375, -4.486), (-.613, -6.573, 2.177)),
        ((.965, 1.926, 0), (-2.773, -4.414, -.017), (4.785, 4.681, .096), (-1.995, -2.241, -.074))),
    ('shaft_u_groove', 'torsion'): (
        ((.966, 1.056, -.022), (-.192, -4.037, .674), (.808, 5.321, -1.231), (-.567, -2.364, .566)),
        ((1.089, .924, .018), (-1.504, -2.141, -.047), (2.486, 2.289, .091), (-1.056, -1.104, -.059))),
}


def _number(value, name, *, zero=False):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite number')
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{name} must be a finite number') from None
    if not math.isfinite(n) or (n < 0 if zero else n <= 0):
        raise ValueError(f'{name} must be finite and {">= 0" if zero else "> 0"}')
    return n


def _compute(geometry, load, D_mm, d_mm, r_mm, L_mm):
    D = _number(D_mm, 'D_mm')
    d = _number(d_mm, 'd_mm', zero=geometry == 'transverse_hole')
    if d >= D:
        raise ValueError('d_mm must be smaller than D_mm')
    if geometry == 'central_hole':
        if load != 'tension':
            raise ValueError('central_hole supports tension only')
        x = d / D
        return 3 - 3.140*x + 3.667*x*x - 1.527*x**3, 'II-2a', 'P/[t(D-d)], net plate section', d/2, '0 < d/D < 1'
    r = _number(r_mm, 'r_mm')
    if geometry == 'transverse_hole':
        x, y = 2*r/D, d/D
        if y > .9:
            raise ValueError('transverse hole requires d/D <= 0.9')
        if load == 'tension':
            if x > .45:
                raise ValueError('transverse hole tension requires 2r/D <= 0.45')
            kt = 3 + (.427-6.770*y+22.698*y*y-16.670*y**3)*x + (11.357+15.665*y-60.929*y*y+41.501*y**3)*x*x
            basis, case, validity = '4P/[π(D²-d²)], gross bar/tube section', 'II-15a', 'd/D <= 0.9, 2r/D <= 0.45'
        elif load == 'bending':
            if x > .4:
                raise ValueError('transverse hole bending requires 2r/D <= 0.4')
            kt = 3 + (-6.250-.585*y+3.115*y*y)*x + (41-1.071*y-6.746*y*y)*x*x + (-45+1.389*y+13.889*y*y)*x**3
            basis, case, validity = '32MD/[π(D⁴-d⁴)], gross bar/tube section', 'II-15b', 'd/D <= 0.9, 2r/D <= 0.4'
        elif load == 'torsion':
            # Roark explicitly specifies 2r/d, not 2r/D; it excludes solid bars.
            if d == 0 or y > .8 or 2*r/d > .4:
                raise ValueError('transverse hole torsion requires 0 < d/D <= 0.8 and 2r/d <= 0.4')
            kt = (4 + (-6.055+3.184*y-3.461*y*y)*x + (32.764-30.121*y+39.887*y*y)*x*x + (-38.330+51.542*math.sqrt(y)-27.483*y)*x**3) / 2
            basis, case, validity = '16TD/[π(D⁴-d⁴)], gross tube section; maximum shear factor is half the tabulated principal-stress factor', 'II-15c', '0 < d/D <= 0.8, 2r/d <= 0.4'
        else:
            raise ValueError('unsupported transverse hole load')
        # The hole/bar intersection is not a rounded notch of radius r.
        return kt, case, basis, None, validity
    key = (geometry, load)
    if key not in _FITS:
        raise ValueError('unsupported geometry/load combination')
    h = (D-d)/2
    ratio = h/r
    lower = .25 if load == 'torsion' or (geometry == 'shaft_u_groove' and load == 'bending') else .1
    upper = 4 if geometry == 'shaft_shoulder' and load == 'torsion' else (50 if geometry == 'shaft_u_groove' else 20)
    if not lower <= ratio <= upper:
        raise ValueError(f'{geometry} {load} requires {lower} <= h/r <= {upper}, h=(D-d)/2')
    if geometry == 'flat_shoulder':
        L = _number(L_mm, 'L_mm')
        threshold = (-1.89*(r/d-.15)+5.5 if load == 'tension' else -2.05*(r/d-.025)+2.0)
        if L/D <= threshold:
            raise ValueError(f'flat shoulder {load} requires L/D > {threshold:g} for this r/d')
    sets = _FITS[key]
    coeffs = sets[0 if len(sets) == 1 or ratio < 2 else 1]
    c = [a+b*math.sqrt(ratio)+v*ratio for a, b, v in coeffs]
    x = 2*h/D
    kt = c[0]+x*(c[1]+x*(c[2]+x*c[3]))
    case = {'shaft_shoulder': 'III-2', 'flat_shoulder': 'III-1', 'shaft_u_groove': 'I-7'}[geometry] + {'tension': 'a', 'bending': 'b', 'torsion': 'c'}[load]
    if geometry == 'flat_shoulder':
        basis = 'P/(td), net narrow section' if load == 'tension' else '6M/(td²), narrow section, in-plane bending'
    else:
        basis = {'tension': '4P/(πd²)', 'bending': '32M/(πd³)', 'torsion': '16T/(πd³)'}[load] + ', reduced round section'
    validity = f'{lower} <= h/r <= {upper}, h=(D-d)/2'
    if geometry == 'flat_shoulder':
        validity += f'; L/D > {threshold:g} at this r/d'
    return kt, case, basis, r, validity


def stress_concentration(geometry: str, load: str, D_mm: float, d_mm: float,
                         r_mm: float | None = None, L_mm: float | None = None,
                         material: str | None = None, ultimate_strength_mpa: float | None = None,
                         prefilled: dict | None = None) -> dict:
    """Return Kt and optionally an estimated steel Kf in an aicad.calc/1 envelope."""
    try:
        if geometry not in {'shaft_shoulder', 'flat_shoulder', 'shaft_u_groove', 'central_hole', 'transverse_hole'}:
            raise ValueError('unknown geometry')
        if load not in {'tension', 'bending', 'torsion'}:
            raise ValueError('load must be tension, bending, or torsion')
        if geometry == 'central_hole' and r_mm is not None:
            raise ValueError('central hole radius is d_mm/2; do not supply r_mm')
        if geometry != 'flat_shoulder' and L_mm is not None:
            raise ValueError('L_mm applies only to flat_shoulder')
        kt, case, basis, notch_radius, validity = _compute(geometry, load, D_mm, d_mm, r_mm, L_mm)
        if not math.isfinite(kt) or kt < 1:
            raise ValueError('fit gives an unphysical Kt below 1 for this geometry; use another analysis')
        answer = {'kt': kt, 'nominal_stress_basis': basis, 'validity': validity}
        citation = f'{_ROARK}, case {case}'
        notes = ['Linear elastic, isolated stress raiser; chart-fit approximation, not a strength or fatigue-life prediction.']
        if material is not None or ultimate_strength_mpa is not None:
            if material != 'steel' or ultimate_strength_mpa is None:
                raise ValueError('fatigue estimate requires material=steel and ultimate_strength_mpa')
            if notch_radius is None:
                raise ValueError('transverse hole has a sharp surface intersection; hole radius is not a notch-root radius for Kf')
            sut = _number(ultimate_strength_mpa, 'ultimate_strength_mpa') / _KSI_MPA
            if not 50 <= sut <= 250:
                raise ValueError('steel fatigue fit requires 50 <= ultimate strength [ksi] <= 250')
            if load == 'torsion':
                a = .190 - .00251*sut + .0000135*sut**2 - .0000000267*sut**3
            else:
                a = .246 - .00308*sut + .0000151*sut**2 - .0000000267*sut**3
            if a <= 0:
                raise ValueError('steel notch-sensitivity fit is nonpositive at this strength; use material-specific data')
            q = 1/(1+a/math.sqrt(notch_radius/25.4))
            answer.update(q=q, kf=1+q*(kt-1), notch_radius_mm=notch_radius)
            citation += '; ' + _FATIGUE
            notes.append('Kf is an approximate fatigue strength reduction for steel, 50–250 ksi ultimate strength; not an elastic stress multiplier for static yielding.')
        inputs = {'geometry': geometry, 'load': load, 'D_mm': D_mm, 'd_mm': d_mm}
        inputs.update({k: v for k, v in {'r_mm': r_mm, 'L_mm': L_mm, 'material': material, 'ultimate_strength_mpa': ultimate_strength_mpa}.items() if v is not None})
        return calc_envelope('stress_concentration', answer, 'dimensionless', 'Kt=peak/nominal; Kf=1+q(Kt-1), q=1/(1+sqrt(a/r))', inputs, 'closed_form', citation, notes=notes, prefilled=prefilled)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
        return calc_fail('bad_query', str(exc))


register('stress_concentration', stress_concentration, input_schema={
    'geometry': {'type': 'string', 'unit': None, 'family': 'string', 'description': 'shaft_shoulder | flat_shoulder | shaft_u_groove | central_hole | transverse_hole'},
    'load': {'type': 'string', 'unit': None, 'family': 'string', 'description': 'tension | bending | torsion (where supported)'},
    'D_mm': {'type': 'float', 'unit': 'mm', 'family': 'length', 'description': 'Major width/diameter; plate width for central hole; outer tube diameter for transverse hole'},
    'd_mm': {'type': 'float', 'unit': 'mm', 'family': 'length', 'description': 'Minor width/diameter; hole diameter for central hole; inner tube diameter (0 for solid) for transverse hole'},
    'r_mm': {'type': 'float', 'unit': 'mm', 'family': 'length', 'description': 'Fillet/groove root radius or transverse-hole radius; omit for central hole'},
    'L_mm': {'type': 'float', 'unit': 'mm', 'family': 'length', 'description': 'Flat shoulder length between transitions, required for flat_shoulder'},
    'material': {'type': 'string', 'unit': None, 'family': 'string', 'description': 'Optional steel for fatigue notch sensitivity'},
    'ultimate_strength_mpa': {'type': 'float', 'unit': 'MPa', 'family': 'material_ref', 'description': 'Steel ultimate tensile strength; required with material, 50–250 ksi'},
}, prefill_hints={})
