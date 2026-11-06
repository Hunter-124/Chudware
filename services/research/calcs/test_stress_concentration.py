"""Roark Table 6-1 and steel notch-sensitivity regression cases."""
import math

import pytest

from . import list_calcs, run_calc


def calc(**inputs):
    return run_calc('stress_concentration', inputs)


def test_registry_and_published_roark_hole_example():
    # Roark 8e Example 6.2, 20 mm hole in 100 mm plate: Kt = 2.51.
    assert 'stress_concentration' in list_calcs()
    result = calc(geometry='central_hole', load='tension', D_mm=100, d_mm=20)
    assert result['ok'] and result['schema'] == 'aicad.calc/1'
    assert result['answer']['kt'] == pytest.approx(2.51, abs=.005)
    assert 'net plate section' in result['answer']['nominal_stress_basis']
    assert 'II-2a' in result['source']['citation']
    assert 'kf' not in result['answer']


@pytest.mark.parametrize('load,expected,case', [
    ('bending', 1.78, 'I-7b'), ('torsion', 1.41, 'I-7c'),
])
def test_published_roark_groove_example(load, expected, case):
    # Roark Example 6.1: D=70, d=49, h=10.5, r=7 mm.
    result = calc(geometry='shaft_u_groove', load=load, D_mm=70, d_mm=49, r_mm=7)
    assert result['ok']
    assert result['answer']['kt'] == pytest.approx(expected, abs=.015)
    assert case in result['source']['citation']
    assert 'reduced round section' in result['answer']['nominal_stress_basis']


@pytest.mark.parametrize('geometry,load,coeffs', [
    ('shaft_shoulder', 'tension', (1.984, -2.063, 1.931, -.855)),
    ('shaft_shoulder', 'bending', (2.022, -2.468, 2.091, -.648)),
    ('shaft_shoulder', 'torsion', (1.613, -1.853, 2.052, -.804)),
    ('flat_shoulder', 'tension', (1.970, -.384, -1.018, .430)),
    ('flat_shoulder', 'bending', (1.986, -1.934, .911, -.356)),
    ('shaft_u_groove', 'tension', (3.004, -5.963, 6.825, -2.893)),
])
def test_fillet_and_groove_polynomials(geometry, load, coeffs):
    # h/r = 1, 2h/D = 0.2, Table 6-1 cases III-1, III-2, I-7a.
    inputs = dict(geometry=geometry, load=load, D_mm=50, d_mm=40, r_mm=5)
    if geometry == 'flat_shoulder':
        inputs['L_mm'] = 400
    result = calc(**inputs)
    assert result['ok'], result
    expected = sum(c * .2**i for i, c in enumerate(coeffs))
    assert result['answer']['kt'] == pytest.approx(expected, abs=1e-10)


def test_piecewise_fits_use_upper_branch_at_two():
    # Roark III-2a, h/r=2 and 2h/D=.2.
    result = calc(geometry='shaft_shoulder', load='tension',
                  D_mm=50, d_mm=40, r_mm=2.5)
    assert result['ok'], result
    s = math.sqrt(2)
    coeffs = (1.200+.860*s-.022*2, -1.805-.346*s-.038*2,
              2.198-.486*s+.165*2, -.593-.028*s-.106*2)
    assert result['answer']['kt'] == pytest.approx(
        sum(c*.2**i for i, c in enumerate(coeffs)))


def test_transverse_hole_loading_and_nominal_basis():
    # Roark II-15, solid round bar for tension and bending: d/D=0, 2r/D=.2.
    for load, expected in [('tension', 3+.427*.2+11.357*.2**2),
                           ('bending', 3-6.25*.2+41*.2**2-45*.2**3)]:
        result = calc(geometry='transverse_hole', load=load, D_mm=50, d_mm=0, r_mm=5)
        assert result['ok'], result
        assert result['answer']['kt'] == pytest.approx(expected)
        assert 'gross' in result['answer']['nominal_stress_basis']
    # Torsion: hollow tube, II-15c: Kt for shear is half of the table's
    # maximum principal stress factor, not the principal stress factor itself.
    result = calc(geometry='transverse_hole', load='torsion', D_mm=100, d_mm=50, r_mm=5)
    y, x = .5, .1
    principal = (4+(-6.055+3.184*y-3.461*y*y)*x
                 +(32.764-30.121*y+39.887*y*y)*x*x
                 +(-38.330+51.542*math.sqrt(y)-27.483*y)*x**3)
    assert result['ok']
    assert result['answer']['kt'] == pytest.approx(principal/2)
    assert 'maximum shear' in result['answer']['nominal_stress_basis']


@pytest.mark.parametrize('load,root_a', [('bending', .0623), ('torsion', .0473)])
def test_steel_neuber_kf(load, root_a):
    result = calc(geometry='shaft_shoulder', load=load, D_mm=50, d_mm=40, r_mm=5,
                  material='steel', ultimate_strength_mpa=100*6.894757293168)
    assert result['ok'], result
    answer = result['answer']
    q = 1/(1+root_a/math.sqrt(5/25.4))
    assert answer['q'] == pytest.approx(q)
    assert answer['kf'] == pytest.approx(1+q*(answer['kt']-1))
    assert 1 < answer['kf'] < answer['kt']
    assert 'Shigley' in result['source']['citation']


def test_central_hole_fatigue_uses_half_diameter():
    result = calc(geometry='central_hole', load='tension', D_mm=100, d_mm=20,
                  material='steel', ultimate_strength_mpa=689.4757293168)
    assert result['ok']
    assert result['answer']['notch_radius_mm'] == 10


@pytest.mark.parametrize('inputs', [
    dict(geometry='shaft_shoulder', load='torsion', D_mm=50, d_mm=40, r_mm=21),
    dict(geometry='shaft_shoulder', load='torsion', D_mm=50, d_mm=40, r_mm=1),
    dict(geometry='shaft_u_groove', load='bending', D_mm=50, d_mm=40, r_mm=21),
    dict(geometry='shaft_shoulder', load='tension', D_mm=40, d_mm=50, r_mm=1),
    dict(geometry='shaft_shoulder', load='tension', D_mm=50, d_mm=40, r_mm=0),
    dict(geometry='shaft_shoulder', load='tension', D_mm=float('nan'), d_mm=40, r_mm=1),
    dict(geometry='flat_shoulder', load='torsion', D_mm=50, d_mm=40, r_mm=5, L_mm=400),
    dict(geometry='flat_shoulder', load='tension', D_mm=50, d_mm=40, r_mm=5, L_mm=50),
    dict(geometry='central_hole', load='torsion', D_mm=50, d_mm=10),
    dict(geometry='central_hole', load='tension', D_mm=50, d_mm=50),
    dict(geometry='central_hole', load='tension', D_mm=50, d_mm=10, r_mm=1),
    dict(geometry='transverse_hole', load='tension', D_mm=50, d_mm=0, r_mm=12),
    dict(geometry='transverse_hole', load='torsion', D_mm=50, d_mm=0, r_mm=2),
    dict(geometry='transverse_hole', load='bending', D_mm=50, d_mm=46, r_mm=2),
    dict(geometry='central_hole', load='tension', D_mm=100, d_mm=20, material='aluminum', ultimate_strength_mpa=700),
    dict(geometry='central_hole', load='tension', D_mm=100, d_mm=20, material='steel', ultimate_strength_mpa=200),
    dict(geometry='central_hole', load='tension', D_mm=100, d_mm=20, material='steel', ultimate_strength_mpa=1800),
    dict(geometry='central_hole', load='tension', D_mm=100, d_mm=20, material='steel'),
    dict(geometry='transverse_hole', load='tension', D_mm=50, d_mm=0, r_mm=5, material='steel', ultimate_strength_mpa=700),
])
def test_refuses_invalid_inputs_and_extrapolation(inputs):
    result = calc(**inputs)
    assert result['ok'] is False, result
    assert result['reason'] == 'bad_query'
    assert result['hint']


def test_prefill_provenance_preserved():
    result = run_calc('stress_concentration', {'geometry': 'central_hole', 'load': 'tension', 'D_mm': 100, 'd_mm': 20}, prefilled={'D_mm': 'geometry'})
    assert result['inputs']['prefilled'] == {'D_mm': 'geometry'}
