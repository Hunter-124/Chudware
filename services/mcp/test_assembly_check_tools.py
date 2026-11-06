"""Assembly checks on live BOM, mate references and placed disassembly geometry."""
import json
import uuid

import pytest

from services.mcp import assembly_check_tools as checks
from services.mcp import core_bridge as cb


class _Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def register(fn):
            self.functions[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def tools():
    registry = _Tools()
    checks.register_assembly_check_tools(registry)
    return {name: lambda _fn=fn, **kwargs: json.loads(_fn(**kwargs))
            for name, fn in registry.functions.items()}


def test_dof_pagination_is_forwarded(tools, monkeypatch):
    monkeypatch.setattr(checks, '_doc', lambda d: d)
    calls = []
    monkeypatch.setattr(checks.cb, 'call', lambda method, params: calls.append((method, params)) or
                        {'ok': True, 'components': [], 'next_cursor': 12})
    result = tools['assembly_dof'](doc_id='d', cursor=7, limit=5)
    assert result['next_cursor'] == 12
    assert calls == [('eval_assembly_dof', {'doc_id': 'd', 'cursor': 7, 'limit': 5})]


def test_disassembly_does_not_pass_incomplete_clearance(tools, monkeypatch):
    monkeypatch.setattr(checks, '_doc', lambda d: d)
    def call(method, params):
        if method == 'asm_world_bodies':
            return {'bodies': [{'component': 'k1', 'bbox': {'min': [0, 0, 0], 'max': [1, 1, 1]}}]}
        return {'ok': False, 'complete': False, 'results': [{'component': 'k1', 'clear': None,
                'blockers': [], 'failed': [{'reason': 'boolean failed'}]}]}
    monkeypatch.setattr(checks.cb, 'call', call)
    result = tools['disassembly_check'](doc_id='d')
    assert result['status'] == 'not_evaluated'
    assert result['blocked_remaining'] == ['k1']


def test_disassembly_graph_removes_blocker_before_target(tools, monkeypatch):
    monkeypatch.setattr(checks, '_doc', lambda d: d)
    def call(method, params):
        if method == 'asm_world_bodies':
            return {'bodies': [{'component': c, 'bbox': {'min': [0, 0, 0], 'max': [2, 2, 2]}}
                               for c in ('target', 'blocker')]}
        assert params['stop_at_first_hit'] is False
        return {'ok': True, 'complete': True, 'results': [
            {'component': q['component'], 'direction': v, 'clear': q['component'] == 'blocker',
             'blockers': [] if q['component'] == 'blocker' else [{'component': 'blocker'}], 'failed': []}
            for q in params['queries'] for v in q['directions']]}
    monkeypatch.setattr(checks.cb, 'call', call)
    result = tools['disassembly_check'](doc_id='d')
    assert result['status'] == 'pass'
    assert [r['component'] for r in result['removal_order']] == ['blocker', 'target']
    assert all(len(r['directions']) == 6 for r in result['components'])


def test_live_bom_checks_pattern_quantity_and_missing_identity(tools, monkeypatch):
    monkeypatch.setattr(checks, '_doc', lambda d: d)
    monkeypatch.setattr(checks, '_components', lambda d: {'k1': {'id': 'k1', 'source': {'path': 'part.step+'}}})
    monkeypatch.setattr(checks, '_source_meta', lambda d, c: {})
    monkeypatch.setattr(checks, '_hardware_coverage', lambda *args: {'status': 'pass', 'patterns': []})
    def call(method, params):
        if method == 'asm_bom_components':
            return {'ok': True, 'components': [{'component': 'k1', 'quantity': 2, 'source': 'part.step+'}], 'truncated': False}
        return {'bodies': [{'component': 'k1'}, {'component': 'k1#1'}]}
    monkeypatch.setattr(checks.cb, 'call', call)
    result = tools['bom_consistency_check'](doc_id='d')
    assert result['status'] == 'not_evaluated'
    assert result['checks'][0]['placed_quantity'] == 2
    monkeypatch.setattr(checks, '_source_meta', lambda d, c: {'generated': {'family': 'bracket'}})
    assert tools['bom_consistency_check'](doc_id='d')['status'] == 'pass'


def test_bore_pattern_reports_missing_recognized_hardware(monkeypatch):
    from services.mcp import assembly_fastener_checks as fast
    def bore(x, sid):
        return {'sid': sid, 'surf': 'cylinder', 'concavity': 'bore', 'radius': 2.75,
                'axis': [0, 0, 1], 'axis_point': [x, 0, 0],
                'cylinder': {'complete_cylinder': True, 'axial_start': [x, 0, 0],
                             'axial_end': [x, 0, 8]}}
    faces = {'plate': [({}, [bore(0, 'one'), bore(10, 'two')], [])], 'bolt': [({}, [], [])]}
    monkeypatch.setattr(fast, '_faces', lambda doc, bodies: (faces, []))
    monkeypatch.setattr(fast, '_shaft', lambda cid, meta, fs: (5, {
        'axis': [0, 0, 1], 'face': {'axis_point': [0, 0, 0]}, 'shank_segment': [0, 6]}, None))
    result = checks._hardware_coverage('d', [{}, {}], {'plate': {}, 'bolt': {}},
                                       {'plate': ({'part_id': 'plate'}, {}),
                                        'bolt': ({}, {'family': 'shcs', 'size': 'M5'})})
    assert result['status'] == 'not_evaluated'
    pattern = result['patterns'][0]
    assert pattern['hole_count'] == 2 and pattern['associated_fastener_count'] == 1
    assert pattern['without_detected_fastener'] == 1
    assert pattern['sites'][1]['observation'] == 'no_detected_fastener'


def test_counterbore_tiers_merge_only_with_shared_transition(monkeypatch):
    from services.mcp import assembly_fastener_checks as fast
    transition = {'classification': 'coaxial_transition', 'adjacency_complete': True,
                  'adjacent_faces': [{'topo': 17}]}
    unknown = {'classification': 'unknown', 'adjacency_complete': False, 'adjacent_faces': []}
    def tier(radius, lo, hi, ends):
        return {'surf': 'cylinder', 'concavity': 'bore', 'radius': radius,
                'axis': [0, 0, 1], 'axis_point': [0, 0, 0], 'sid': str(radius),
                'cylinder': {'complete_cylinder': True, 'axial_start': [0, 0, lo],
                             'axial_end': [0, 0, hi], 'ends': ends}}
    bore = tier(2.75, 0, 5, [unknown, transition])
    counterbore = tier(4, 5, 8, [transition, unknown])
    monkeypatch.setattr(fast, '_faces', lambda doc, bodies: ({'plate': [({}, [bore, counterbore], [])]}, []))
    merged = checks._hardware_coverage('d', [{}], {'plate': {}}, {'plate': ({}, {})})
    assert len(merged['patterns']) == 1 and merged['patterns'][0]['hole_count'] == 1
    counterbore['cylinder']['ends'][0] = unknown
    distinct = checks._hardware_coverage('d', [{}], {'plate': {}}, {'plate': ({}, {})})
    assert sum(p['hole_count'] for p in distinct['patterns']) == 2


def test_bom_duplicate_and_unsourced_rows_are_distinguished(tools, monkeypatch):
    monkeypatch.setattr(checks, '_doc', lambda d: d)
    comps = {cid: {'id': cid, 'source': {'path': 'bolt.step+'}} for cid in ('k1', 'k2')}
    monkeypatch.setattr(checks, '_components', lambda d: comps)
    monkeypatch.setattr(checks, '_source_meta', lambda d, c: {'part_record': {
        'part_id': 'fastener:example:M5', 'sourcing': []}, 'generated': {'family': 'shcs'}})
    monkeypatch.setattr(checks, 'component_source_file', lambda comp, doc: '/tmp/bolt.step+')
    monkeypatch.setattr(checks, '_hardware_coverage', lambda *args: {'status': 'pass', 'patterns': []})
    rows = [{'component': c, 'quantity': 1, 'source': 'bolt.step+'} for c in comps]
    def call(method, params):
        return {'ok': True, 'components': rows, 'truncated': False} if method == 'asm_bom_components' else {
            'bodies': [{'component': c} for c in comps]}
    monkeypatch.setattr(checks.cb, 'call', call)
    result = tools['bom_consistency_check'](doc_id='d')
    assert result['status'] == 'not_evaluated'
    assert len(result['unsourced_components']) == 2
    assert result['repeated_source_lines'][0]['components'] == ['k1', 'k2']
    rows.append(dict(rows[0]))
    duplicate = tools['bom_consistency_check'](doc_id='d')
    assert duplicate['status'] == 'fail'
    assert duplicate['checks'][0]['bom_rows'] == 2


def test_mate_fit_automatically_measures_concentric_refs(tools, monkeypatch):
    from services.mcp import cad_render_tools
    monkeypatch.setattr(checks, '_doc', lambda d: d)
    def call(method, params):
        if method == 'doc_tree':
            return {'tree': {'components': [{'id': 'a'}, {'id': 'b'}], 'mates': [
                {'id': 'm1', 'type': 'concentric', 'refs': [{'component': 'a'}, {'component': 'b'}]}]}}
        return {'bodies': [{'component': 'a'}, {'component': 'b'}]}
    monkeypatch.setattr(checks.cb, 'call', call)
    def faces(cb_arg, doc, body):
        return [{'sid': str(body), 'surf': 'cylinder', 'axis': [0, 0, 1], 'axis_point': [0, 0, 0],
                 'radius': 2.55 if body == 1 else 2.5, 'concavity': 'bore' if body == 1 else 'boss'}]
    monkeypatch.setattr(checks, '_round_faces_for_body', faces)
    monkeypatch.setattr(cad_render_tools, '_mate_face_matches', lambda ref, comps, topo: topo['face'])
    measured = tools['mate_fit_check'](doc_id='d')
    assert measured['status'] == 'not_evaluated'
    assert measured['checks'][0]['diametral_clearance_mm'] == pytest.approx(0.1)
    assert measured['checks'][0]['fit_character'] == 'clearance'
    assert measured['checks'][0]['nearest_iso_fit']['fit']
    assert measured['checks'][0]['classification_nominal_basis'] == 'inferred'
    explicit_nominal = tools['mate_fit_check'](doc_id='d', nominal_mm=5)
    assert explicit_nominal['checks'][0]['classification_nominal_mm'] == 5
    assert explicit_nominal['checks'][0]['classification_nominal_basis'] == 'caller'
    assert tools['mate_fit_check'](doc_id='d', nominal_mm=5, fit='H7/g6')['status'] == 'fail'


def test_source_document_private_lease_closes_on_error(monkeypatch):
    calls = []
    monkeypatch.setattr(checks, 'component_source_file', lambda comp, doc: '/tmp/part.step+')
    def call(method, params):
        calls.append((method, params))
        if method == 'doc_list':
            return {'docs': []}
        if method == 'doc_open':
            return {'doc_id': 'private'}
        if method == 'doc_get_meta':
            raise RuntimeError('read failed')
        return {}
    monkeypatch.setattr(checks.cb, 'call', call)
    with pytest.raises(RuntimeError):
        checks._source_meta('assembly', {'id': 'k1'})
    assert ('doc_open', {'path': '/tmp/part.step+', 'internal': True, 'detach_path': True}) in calls
    assert ('doc_close', {'doc_id': 'private'}) in calls


def _box(path, dims, docs):
    part = cb.call('doc_new', {'name': 'cage_' + uuid.uuid4().hex[:8], 'kind': 'part', 'resume_if_open': False})['doc_id']
    docs.append(part)
    result = cb.call('doc_apply_ops', {'doc_id': part, 'ops': [cb.feat('box', 'Box', {
        'length': cb.dim(dims[0]), 'width': cb.dim(dims[1]), 'height': cb.dim(dims[2])})]})
    assert all(s['ok'] for s in result['rebuild']['feature_status'])
    cb.call('doc_save', {'doc_id': part, 'path': str(path)})


def _pose(x, y, z):
    return [1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1]


@pytest.mark.skipif(not cb.available(), reason='native CAD engine unavailable')
def test_engine_closed_cage_blocks_all_six_axes(tmp_path, tools):
    docs = []
    try:
        paths = [tmp_path / f'{i}.step+' for i in range(4)]
        for path, dims in zip(paths, [(4, 4, 4), (2, 20, 20), (20, 2, 20), (20, 20, 2)]):
            _box(path, dims, docs)
        assembly = cb.call('doc_new', {'name': 'closed_cage_' + uuid.uuid4().hex[:8],
                                      'kind': 'assembly', 'resume_if_open': False})['doc_id']
        docs.append(assembly)
        ops = [{'op': 'asm_insert_component', 'source': {'path': str(paths[0])}}]
        for path, positions in zip(paths[1:], [[(-7, 0, 0), (7, 0, 0)],
                                                [(0, -7, 0), (0, 7, 0)],
                                                [(0, 0, -7), (0, 0, 7)]]):
            ops.extend({'op': 'asm_insert_component', 'source': {'path': str(path)}, 'transform': _pose(*pos)}
                       for pos in positions)
        inserted = cb.call('doc_apply_ops', {'doc_id': assembly, 'ops': ops})
        target = inserted['id_map']['$0']
        result = tools['disassembly_check'](doc_id=assembly, components=[target])
        assert result['status'] == 'fail', result
        assert result['blocked_remaining'] == [target]
        assert len(result['components'][0]['directions']) == 6
        assert all(r['clear'] is False and r['blockers'] for r in result['components'][0]['directions'])
    finally:
        for doc in reversed(docs):
            cb.call('doc_close', {'doc_id': doc})


@pytest.mark.skipif(not cb.available(), reason='native CAD engine unavailable')
def test_engine_bom_pagination_and_empty_assembly(tmp_path, tools):
    docs = []
    try:
        path = tmp_path / 'box.step+'
        _box(path, (4, 4, 4), docs)
        assembly = cb.call('doc_new', {'name': 'bom_check_' + uuid.uuid4().hex[:8],
                                      'kind': 'assembly', 'resume_if_open': False})['doc_id']
        docs.append(assembly)
        assert tools['bom_consistency_check'](doc_id=assembly)['status'] == 'not_evaluated'
        cb.call('doc_apply_ops', {'doc_id': assembly, 'ops': [
            {'op': 'asm_insert_component', 'source': {'path': str(path)}, 'transform': _pose(i*10, 0, 0)}
            for i in range(3)]})
        first = cb.call('asm_bom_components', {'doc_id': assembly, 'cursor': 0, 'limit': 1})
        second = cb.call('asm_bom_components', {'doc_id': assembly, 'cursor': first['next_cursor'], 'limit': 1})
        assert first['ok'] and first['truncated'] and len(first['components']) == 1
        assert second['ok'] and len(second['components']) == 1
        assert first['components'][0]['component'] != second['components'][0]['component']
    finally:
        for doc in reversed(docs):
            cb.call('doc_close', {'doc_id': doc})


@pytest.mark.skipif(not cb.available(), reason='native CAD engine unavailable')
def test_engine_concentric_mate_fit_uses_world_faces(tmp_path, tools):
    from services.mcp.test_cad_tools import _call
    docs = []
    try:
        plate = cb.call('doc_new', {'name': 'fit_plate_' + uuid.uuid4().hex[:8], 'kind': 'part',
                                    'resume_if_open': False})['doc_id']
        docs.append(plate)
        cb.call('doc_apply_ops', {'doc_id': plate, 'ops': [
            cb.feat('box', 'Plate', {'length': cb.dim(20), 'width': cb.dim(20), 'height': cb.dim(4)}),
            cb.feat('hole', 'Bore', {'style': 'simple', 'end': 'through_all', 'diameter': cb.dim(5.2)},
                    {'face': {'of': 'face', 'side': 'top'}})]})
        plate_path = tmp_path / 'plate.step+'
        cb.call('doc_save', {'doc_id': plate, 'path': str(plate_path)})
        pin = cb.call('doc_new', {'name': 'fit_pin_' + uuid.uuid4().hex[:8], 'kind': 'part',
                                  'resume_if_open': False})['doc_id']
        docs.append(pin)
        cb.call('doc_apply_ops', {'doc_id': pin, 'ops': [
            cb.feat('cylinder', 'Pin', {'radius': cb.dim(2.5), 'height': cb.dim(8)})]})
        pin_path = tmp_path / 'pin.step+'
        cb.call('doc_save', {'doc_id': pin, 'path': str(pin_path)})
        assembly = cb.call('doc_new', {'name': 'fit_asm_' + uuid.uuid4().hex[:8], 'kind': 'assembly',
                                       'resume_if_open': False})['doc_id']
        docs.append(assembly)
        inserted = cb.call('doc_apply_ops', {'doc_id': assembly, 'ops': [
            {'op': 'asm_insert_component', 'source': {'path': str(plate_path)}, 'fixed': True},
            {'op': 'asm_insert_component', 'source': {'path': str(pin_path)}}]})
        mate, _ = _call('asm_mate', {'doc_id': assembly, 'mate_type': 'concentric',
            'a': {'component': inserted['id_map']['$0'], 'query': {'of': 'face', 'surf': 'round'}},
            'b': {'component': inserted['id_map']['$1'], 'query': {'of': 'face', 'surf': 'round'}}})
        assert mate['ok'], mate
        result = tools['mate_fit_check'](doc_id=assembly)
        assert len(result['checks']) == 1, result
        assert result['checks'][0]['diametral_clearance_mm'] == pytest.approx(0.2, abs=1e-4)
        assert result['status'] == 'not_evaluated'
    finally:
        for doc in reversed(docs):
            cb.call('doc_close', {'doc_id': doc})
