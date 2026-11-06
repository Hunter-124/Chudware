"""Fastener discovery and geometry screens, including isolated engine-backed assembly."""
from __future__ import annotations

import json
import uuid

import pytest

from services.mcp import assembly_fastener_checks as fasteners
from services.mcp import core_bridge as cb


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def register(fn):
            self.functions[fn.__name__] = fn
            return fn
        return register


def _registered():
    tools = Tools()
    fasteners.register_fastener_tools(tools)
    return tools.functions


def _fixture_faces():
    shank = {'concavity': 'boss', 'radius': 3., 'axis': [0, 0, 1],
             'axis_point': [0, 0, 6.5], 'sid': 'shaft'}
    flats = [{'normal': [1, 0, 0], 'centroid': [5, 0, 2]},
             {'normal': [-1, 0, 0], 'centroid': [-5, 0, 2]}]
    bolt = {'component': 'bolt', 'bbox': {'min': [-6, -6, 0], 'max': [6, 6, 9]}}
    bore = {'concavity': 'bore', 'radius': 3.5, 'axis': [0, 0, 1],
            'axis_point': [0, 0, 15], 'sid': 'hole'}
    plate = {'component': 'plate', 'bbox': {'min': [-15, -15, 10], 'max': [15, 15, 20]}}
    return {'bolt': [(bolt, [shank], flats)], 'plate': [(plate, [bore], [])]}


_META = {'family': 'hex_bolt', 'size': 'M6', 'specs': {'hex_af': 10., 'length': 5.}}


def test_short_screw_and_unknown_thread_evidence():
    row = fasteners._fastener_row('bolt', _META, _fixture_faces(), None, None)
    assert row['status'] == 'fail'
    reach = next(c for c in row['checks'] if c['check'] == 'axial_reach')
    assert reach['status'] == 'fail' and reach['bore_axial_point_mm'] == 15
    assert next(c for c in row['checks'] if c['check'] == 'thread_engagement')['status'] == 'not_evaluated'
    assert next(c for c in row['checks'] if c['check'] == 'through_or_blind')['status'] == 'not_evaluated'


def test_stale_nominal_is_not_geometry(monkeypatch):
    faces = _fixture_faces()
    faces['bolt'][0][1][0]['radius'] = 4
    row = fasteners._fastener_row('bolt', _META, faces, None, None)
    assert row['status'] == 'not_evaluated'
    assert 'stale' in row['checks'][0]['reason']


def test_socket_uses_both_standard_diameters_and_incomplete_is_not_pass(monkeypatch):
    captured = []
    def call(method, params):
        assert method == 'eval_envelope_clearance'
        captured.append(params)
        return {'complete': False, 'results': [{'hits': [], 'failed': []},
                                               {'hits': [], 'failed': [{'reason': 'failed'}]}]}
    monkeypatch.setattr(fasteners.cb, 'call', call)
    row = fasteners._access_row('doc', 'bolt', _META, _fixture_faces())
    assert row['status'] == 'not_evaluated'
    sections = captured[0]['solids']
    assert sections[0]['cylinder']['radius'] == 15.5 / 2
    assert sections[1]['cylinder']['radius'] == 24 / 2
    assert 'ISO 2725-1:2017 Table 3' in row['source']


@pytest.mark.parametrize('joint,expected', [('fixed', 1.0), ('floating', .5)])
def test_diametral_position_allowance_requires_mmc(joint, expected):
    pos = {'bolt': {'joint': joint, 'hole_position_mm': .4,
                    'hole_mmc_mm': 7, 'fastener_mmc_mm': 6}}
    row = fasteners._fastener_row('bolt', _META, _fixture_faces(), pos, None)
    check = next(c for c in row['checks'] if c['check'] == 'position_tolerance')
    assert check['status'] == 'pass'
    assert check['maximum_diametral_zone_mm'] == expected
    del pos['bolt']['hole_mmc_mm']
    with pytest.raises(ValueError):
        fasteners._fastener_row('bolt', _META, _fixture_faces(), pos, None)


def test_model_hit_does_not_prove_actual_socket_blocked(monkeypatch):
    monkeypatch.setattr(fasteners.cb, 'call', lambda method, params: {
        'complete': True, 'results': [
            {'hits': [{'component': 'obstacle'}], 'failed': []},
            {'hits': [], 'failed': []}]})
    row = fasteners._access_row('doc', 'bolt', _META, _fixture_faces())
    assert row['status'] == 'not_evaluated'
    assert row['modeled_envelope_status'] == 'fail'

def test_hex_key_uses_geometry_checked_recess(monkeypatch):
    faces = _fixture_faces()
    faces['bolt'][0][1][0]['axis_point'] = [0, 0, 2.5]
    faces['bolt'][0][2][:] = [
        {'normal': [1, 0, 0], 'centroid': [-2.5, 0, 8]},
        {'normal': [-1, 0, 0], 'centroid': [2.5, 0, 8]}]
    meta = {'family': 'shcs', 'size': 'M6', 'specs': {'socket_key_width': 5}}
    calls = []
    def call(method, params):
        calls.append(params)
        return {'complete': True, 'results': [{'hits': [], 'failed': []}]}
    monkeypatch.setattr(fasteners.cb, 'call', call)
    row = fasteners._access_row('doc', 'bolt', meta, faces)
    assert row['tool'] == 'hex key' and row['status'] == 'not_evaluated'
    assert calls[0]['solids'][0]['cylinder']['radius'] == pytest.approx(5 / 3**.5)


def test_exact_finite_cylinders_detect_blind_bottom_and_stack():
    faces = _fixture_faces()
    shank = faces['bolt'][0][1][0]
    shank['cylinder'] = {'complete_cylinder': True, 'axial_start': [0, 0, 4],
                         'axial_end': [0, 0, 9]}
    faces['bolt'][0][2].append({'normal': [0, 0, -1], 'centroid': [0, 0, 0]})
    plate, rounds, _ = faces['plate'][0]
    plate['bbox'] = {'min': [-15, -15, 5], 'max': [15, 15, 8]}
    rounds[0]['axis_point'] = [0, 0, 6.5]
    rounds[0]['cylinder'] = {'complete_cylinder': True, 'axial_start': [0, 0, 5],
                             'axial_end': [0, 0, 8], 'ends': [
                                 {'classification': 'unknown'},
                                 {'classification': 'closed_bottom'}]}
    row = fasteners._fastener_row('bolt', _META, faces, None, None)
    blind = next(c for c in row['checks'] if c['check'] == 'through_or_blind')
    bottom = next(c for c in row['checks'] if c['check'] == 'blind_bottom_clearance')
    grip = next(c for c in row['checks'] if c['check'] == 'grip')
    assert blind['classification'] == 'blind'
    assert bottom['status'] == 'fail' and bottom['modeled_tip_to_bottom_mm'] == -1
    assert grip['candidate_span_mm'] == 3 and grip['status'] == 'not_evaluated'


def test_through_geometry_and_iso_series_do_not_invent_clearance_role():
    faces = _fixture_faces()
    bore = faces['plate'][0][1][0]
    bore['cylinder'] = {'complete_cylinder': True, 'axial_start': [0, 0, 10],
                        'axial_end': [0, 0, 20], 'ends': [
                            {'classification': 'open_to_exterior'},
                            {'classification': 'open_to_exterior'}]}
    row = fasteners._fastener_row('bolt', _META, faces, None, None)
    through = next(c for c in row['checks'] if c['check'] == 'through_or_blind')
    iso = next(c for c in row['checks'] if c['check'] == 'iso273_clearance')
    assert through['classification'] == 'through' and through['status'] == 'pass'
    assert iso['modeled_meets_iso273'] is True and iso['status'] == 'not_evaluated'


def test_rotated_shank_uses_world_endpoints():
    faces = _fixture_faces()
    direction = [2**-.5, 0, 2**-.5]
    shank = faces['bolt'][0][1][0]
    shank['axis'] = direction
    shank['axis_point'] = [6.5 * x for x in direction]
    shank['cylinder'] = {'complete_cylinder': True,
                         'axial_start': [4 * x for x in direction],
                         'axial_end': [9 * x for x in direction]}
    faces['bolt'][0][2].append({'normal': [-x for x in direction], 'centroid': [0, 0, 0]})
    _, shaft, error = fasteners._shaft('bolt', _META, faces)
    assert error is None
    assert shaft['shank_segment'] == pytest.approx([4, 9])
    assert shaft['head_axis'] == pytest.approx([-x for x in direction])


def test_counterbore_requires_transition_at_shared_end():
    shaft = {'axis': [0, 0, 1]}
    def bore(sid, diameter, segment, ends):
        return {'component': 'plate', 'sid': sid, 'diameter_mm': diameter,
                'axis_point': [0, 0, 0], 'axial_segment_mm': segment,
                'ends': [{'classification': c} for c in ends]}
    minor = bore('minor', 7, [0, 5], ['unknown', 'coaxial_transition'])
    major = bore('major', 12, [5, 8], ['coaxial_transition', 'unknown'])
    result = fasteners._counterbore_geometry(shaft, [minor, major])
    assert result['candidates'][0]['major_depth_mm'] == 3
    major['axial_segment_mm'] = [6, 8]
    assert not fasteners._counterbore_geometry(shaft, [minor, major])['candidates']


def test_complete_annular_seat_requires_finite_support():
    faces = _fixture_faces()
    shank = faces['bolt'][0][1][0]
    shank['cylinder'] = {'complete_cylinder': True, 'axial_start': [0, 0, 4],
                         'axial_end': [0, 0, 9]}
    head = {'center': [0, 0, 4], 'normal': [0, 0, 1], 'inner_radius': 3,
            'outer_radius': 5, 'adjacency_complete': True}
    faces['bolt'][0][2].extend([{'normal': [0, 0, -1], 'centroid': [0, 0, 0]},
                                {'sid': 'head', 'normal': [0, 0, 1], 'centroid': [0, 0, 4],
                                 'bearing_annulus': head}])
    support = dict(head, normal=[0, 0, -1], inner_radius=2.5, outer_radius=6)
    faces['plate'][0][2].append({'sid': 'seat', 'bearing_annulus': support})
    nominal, shaft, error = fasteners._shaft('bolt', _META, faces)
    assert error is None and nominal == 6
    assert fasteners._bearing_seat('bolt', shaft, faces)['status'] == 'pass'
    support['outer_radius'] = 4
    assert fasteners._bearing_seat('bolt', shaft, faces)['status'] == 'not_evaluated'


def test_missing_evidence_never_passes(monkeypatch):
    tools = _registered()
    monkeypatch.setattr(fasteners, '_doc', lambda doc: doc)
    monkeypatch.setattr(fasteners, '_world', lambda doc, components: ([], [], [], {}))
    monkeypatch.setattr(fasteners, '_faces', lambda doc, bodies: ({}, []))
    for name in ('fastener_check', 'tool_access_check'):
        result = json.loads(tools[name](doc_id='doc'))
        assert result['status'] == 'not_evaluated'


def test_discovers_nuts_and_washers_without_guessing_their_role(monkeypatch):
    components = [{'id': 'nut'}, {'id': 'washer'}, {'id': 'plate'}]
    monkeypatch.setattr(fasteners.cb, 'call', lambda method, params: (
        {'tree': {'components': components}} if method == 'doc_tree' else {'bodies': []}))
    metadata = {'nut': {'family': 'hex_nut', 'size': 'M6'},
                'washer': {'family': 'washer_flat', 'size': 'M6'}, 'plate': {}}
    monkeypatch.setattr(fasteners, '_source', lambda doc, comp: metadata[comp['id']])
    _, candidates, unknown, _ = fasteners._world('assembly', None)
    assert [cid for cid, _ in candidates] == ['nut', 'washer'] and not unknown
    for cid, meta in candidates:
        assert fasteners._fastener_row(cid, meta, {}, None, None)['status'] == 'not_evaluated'


def test_thread_calculator_receives_real_material_and_never_assumes_thread(monkeypatch):
    req = {'bolt': {'receiving_component': 'plate', 'material_id': 'alu_6061',
                    'grade': '8.8', 'pitch_mm': 1.0}}
    row = fasteners._fastener_row('bolt', _META, _fixture_faces(), None, req)
    engagement = next(c for c in row['checks'] if c['check'] == 'thread_engagement')
    assert engagement['calculation']['ok']
    assert engagement['calculation']['inputs']['internal_material_id'] == 'alu_6061'
    assert engagement['status'] == 'fail' and engagement['maximum_possible_overlap_mm'] == 0


@pytest.mark.skipif(not cb.available(), reason='CAD engine unavailable')
def test_short_bolt_and_blocked_socket_in_real_assembly(tmp_path, monkeypatch):
    from services.mcp.test_cad_tools import _call
    monkeypatch.setenv('CHUDWARE_PROJECT_DIR', str(tmp_path))
    docs = []
    try:
        bolt = cb.call('parts_make', {'family': 'hex_bolt', 'size': 'M6', 'length_mm': 5})['doc_id']
        docs.append(bolt)
        bolt_path = str(tmp_path / 'bolt.step+')
        cb.call('doc_save', {'doc_id': bolt, 'path': bolt_path})
        nut = cb.call('parts_make', {'family': 'hex_nut', 'size': 'M6'})['doc_id']
        docs.append(nut)
        nut_path = str(tmp_path / 'nut.step+')
        cb.call('doc_save', {'doc_id': nut, 'path': nut_path})
        plate = cb.call('doc_new', {'name': 'fastener_plate_' + uuid.uuid4().hex,
                                    'kind': 'part', 'resume_if_open': False})['doc_id']
        docs.append(plate)
        result = cb.call('doc_apply_ops', {'doc_id': plate, 'ops': [
            cb.feat('box', 'Plate', {'length': cb.dim(30), 'width': cb.dim(30), 'height': cb.dim(10)}),
            {'op': 'add_feature', 'feature': {'type': 'hole', 'name': 'Bore',
             'params': {'face': {'of': 'face', 'side': 'top'}, 'style': 'simple',
                        'diameter': 7, 'end': 'through_all', 'positions': [[0, 0]]}}},
        ]})
        assert all(s['ok'] for s in result['rebuild']['feature_status']), result
        plate_path = str(tmp_path / 'plate.step+')
        cb.call('doc_save', {'doc_id': plate, 'path': plate_path})
        blocker = cb.call('doc_new', {'name': 'socket_blocker_' + uuid.uuid4().hex,
                                      'kind': 'part', 'resume_if_open': False})['doc_id']
        docs.append(blocker)
        result = cb.call('doc_apply_ops', {'doc_id': blocker, 'ops': [
            cb.feat('box', 'Blocker', {'length': cb.dim(2), 'width': cb.dim(2), 'height': cb.dim(4)})]})
        assert all(s['ok'] for s in result['rebuild']['feature_status']), result
        blocker_path = str(tmp_path / 'blocker.step+')
        cb.call('doc_save', {'doc_id': blocker, 'path': blocker_path})
        assembly = cb.call('doc_new', {'name': 'fastener_assembly_' + uuid.uuid4().hex,
                                       'kind': 'assembly', 'resume_if_open': False})['doc_id']
        docs.append(assembly)
        for path, at in ((bolt_path, [0, 0, 0]), (plate_path, [0, 0, 15]),
                         (blocker_path, [7, 0, -10])):
            placed, _ = _call('asm_add_component', {'doc_id': assembly, 'path': path, 'at': at})
            assert placed['ok'], placed
        result, _ = _call('fastener_check', {'doc_id': assembly})
        assert result['ok'] and result['status'] == 'fail', result
        bolt_row = next(r for r in result['fasteners'] if r.get('family') == 'hex_bolt')
        assert any(c['check'] == 'axial_reach' and c['status'] == 'fail' for c in bolt_row['checks']), bolt_row
        access, _ = _call('tool_access_check', {'doc_id': assembly})
        assert access['ok'] and access['status'] == 'not_evaluated', access
        bolt_access = next(r for r in access['fasteners'] if r.get('family') == 'hex_bolt')
        assert bolt_access['modeled_envelope_status'] == 'fail', bolt_access
        assert any(r['hits'] for r in bolt_access['results']), bolt_access
        rotated = cb.call('doc_new', {'name': 'rotated_fastener_' + uuid.uuid4().hex,
                                      'kind': 'assembly', 'resume_if_open': False})['doc_id']
        docs.append(rotated)
        placed, _ = _call('asm_add_component', {'doc_id': rotated, 'path': bolt_path,
                                                  'rotate': [0, 1, 0], 'angle': 45})
        assert placed['ok'], placed
        rotated_result, _ = _call('fastener_check', {'doc_id': rotated})
        rotated_bolt = next(r for r in rotated_result['fasteners'] if r.get('family') == 'hex_bolt')
        assert rotated_bolt['geometry']['shank_axial_segment_mm'] is not None, rotated_bolt
        assert abs(rotated_bolt['geometry']['axis'][0]) == pytest.approx(2**-.5, abs=1e-5)
        nut_assembly = cb.call('doc_new', {'name': 'short_nut_' + uuid.uuid4().hex,
                                           'kind': 'assembly', 'resume_if_open': False})['doc_id']
        docs.append(nut_assembly)
        for path, at in ((bolt_path, [0, 0, 0]), (nut_path, [0, 0, 15])):
            placed, _ = _call('asm_add_component', {'doc_id': nut_assembly, 'path': path, 'at': at})
            assert placed['ok'], placed
        short, _ = _call('fastener_check', {'doc_id': nut_assembly})
        assert short['ok'] and short['status'] == 'fail', short
        short_bolt = next(r for r in short['fasteners'] if r.get('family') == 'hex_bolt')
        nut_row = next(r for r in short['fasteners'] if r.get('family') == 'hex_nut')
        assert any(c['check'] == 'axial_reach' and c['status'] == 'fail'
                   and c.get('component') == nut_row['component'] for c in short_bolt['checks']), short_bolt
    finally:
        for doc in reversed(docs):
            cb.call('doc_close', {'doc_id': doc})
