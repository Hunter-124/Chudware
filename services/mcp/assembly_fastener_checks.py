"""Geometry-based, read-only fastener and assembly-tool screening.

Standard-part metadata identifies candidates, not the current shape. Geometric
results are nominal-model screens, not certification of purchased hardware.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from services.mcp import core_bridge as cb
from services.mcp.cad_common import _doc, _dumps, _err
from services.mcp.cad_asm_tools import component_source_file, open_component_part
from services.mcp.interface_check_tools import (
    _axis_angle_deg, _perp_distance_between_axes, _unit, _dot,
)
from services.research.fits import iso273_clearance_hole
from services.research.calcs.thread_engagement import thread_engagement

_MAX = 100
_SCREWS = {'shcs', 'bhcs', 'fhcs', 'hex_bolt'}
_FASTENERS = _SCREWS | {'hex_nut', 'washer_flat', 'washer_lock', 'threaded_rod',
                        'dowel_pin', 'heat_set_insert', 'standoff_round', 'standoff_hex'}
# ISO 2725-1:2017 Table 3, square drive 12.5 mm. d1 and d2 are
# maximum operating- and drive-end diameters, respectively; normal length is
# a minimum. A real socket's profile and length must be obtained from its maker.
_SOCKET = {8: (14, 24, 40), 10: (15.5, 24, 40), 13: (19.2, 24, 40),
           17: (24.2, 25.5, 40), 19: (26.7, 26.7, 42)}
_SOCKET_SOURCE = ('ISO 2725-1:2017 Table 3, 12.5 mm square drive, d1 max, d2 max '
                  'and normal length min; https://cdn.standards.iteh.ai/samples/69305/'
                  'bbec890142bc41238d9571bfd02fb1a7/ISO-2725-1-2017.pdf')


def _status(rows):
    statuses = [r.get('status') for r in rows]
    return ('fail' if 'fail' in statuses else 'not_evaluated'
            if not statuses or 'not_evaluated' in statuses else 'pass')


def _unknown(check, reason, **extra):
    return {'check': check, 'status': 'not_evaluated', 'reason': reason, **extra}


def _source(doc, comp):
    path = component_source_file(comp, doc)
    with open_component_part(path) as part:
        standard = cb.call('doc_get_meta', {'doc_id': part, 'key': 'standard_part'}).get('value')
        catalog = cb.call('doc_get_meta', {'doc_id': part, 'key': 'parts_catalog'}).get('value')
    if isinstance(standard, dict):
        return standard
    for _ in range(8):
        if not isinstance(catalog, dict):
            break
        if isinstance(catalog.get('generated'), dict):
            return catalog['generated']
        catalog = catalog.get('source_catalog')
    return {}


def _positive(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _vector(x):
    return (isinstance(x, (list, tuple)) and len(x) == 3 and
            all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in x))


def _bounds(body, axis):
    box = body.get('bbox') or {}
    lo, hi = box.get('min'), box.get('max')
    if not _vector(lo) or not _vector(hi):
        return None
    return (sum(axis[i] * (lo[i] if axis[i] >= 0 else hi[i]) for i in range(3)),
            sum(axis[i] * (hi[i] if axis[i] >= 0 else lo[i]) for i in range(3)))


def _world(doc, components):
    tree_result = cb.call('doc_tree', {'doc_id': doc})
    comps = {c['id']: c for c in (tree_result.get('tree') or {}).get('components', [])
             if isinstance(c, dict) and isinstance(c.get('id'), str)}
    if components is not None:
        if not isinstance(components, list) or len(components) > _MAX or any(not isinstance(c, str) for c in components):
            raise ValueError('components must be a list of at most 100 component IDs')
        missing = set(components) - comps.keys()
        if missing:
            raise ValueError(f'Unknown components: {sorted(missing)}')
    world = cb.call('asm_world_bodies', {'doc_id': doc, 'explode': False})
    if world.get('ok') is False:
        raise ValueError('World geometry unavailable: ' + str(world.get('error', world)))
    bodies = world.get('bodies') or []
    selected = set(components) if components is not None else set(comps)
    candidates, unknown = [], []
    for cid, comp in comps.items():
        if cid not in selected:
            continue
        try:
            meta = _source(doc, comp)
        except Exception as exc:
            unknown.append({'component': cid, 'status': 'not_evaluated',
                            'reason': f'Source metadata unavailable: {exc}'})
            continue
        if meta.get('family') in _FASTENERS:
            candidates.append((cid, meta))
        elif components is not None:
            unknown.append({'component': cid, 'status': 'not_evaluated',
                            'reason': 'Source does not identify a standard fastener.'})
    return bodies, candidates, unknown, world


def _faces(doc, bodies):
    result, errors = {}, []
    for index, body in enumerate(bodies, 1):
        cid = body.get('component')
        try:
            replies = [cb.call('doc_query', {'doc_id': doc, 'query': {
                'of': 'face', 'surf': surf, 'body_id': index, 'fastener_geometry': True}}) for surf in ('round', 'flat')]
            if any(not isinstance(r, dict) or r.get('ok') is False or not isinstance(r.get('matches'), list)
                   for r in replies):
                raise ValueError('Face query failed or omitted matches')
            round_faces = [f for f in replies[0]['matches'] if f.get('surf') == 'cylinder']
            planes = [f for f in replies[1]['matches'] if f.get('surf') in ('plane', 'flat')]
            result.setdefault(cid, []).append((body, round_faces, planes))
        except Exception as exc:
            errors.append({'component': cid, 'reason': str(exc)})
    return result, errors


def _cylinder_segment(face, axis):
    cylinder = face.get('cylinder')
    if not isinstance(cylinder, dict) or cylinder.get('complete_cylinder') is not True:
        return None
    start, end = cylinder.get('axial_start'), cylinder.get('axial_end')
    if not _vector(start) or not _vector(end):
        return None
    a, b = sorted((_dot(start, axis), _dot(end, axis)))
    return (a, b) if b - a > 1e-6 else None


def _shaft(cid, meta, faces):
    size = re.fullmatch(r'M\s*(\d+(?:\.\d+)?)', str(meta.get('size', '')).strip(), re.I)
    nominal = float(size.group(1)) if size else None
    candidates = []
    for body, rounds, planes in faces.get(cid, []):
        for f in rounds:
            if (f.get('concavity') not in ('boss', 'convex') or not _positive(f.get('radius'))
                    or not _vector(f.get('axis')) or not _vector(f.get('axis_point'))):
                continue
            dia = 2 * f['radius']
            if nominal and abs(dia - nominal) <= max(.05, nominal * .01):
                axis = _unit(f['axis'])
                bounds = _bounds(body, axis)
                if bounds:
                    candidates.append((body, f, planes, axis, bounds))
    if len(candidates) != 1:
        return nominal, None, 'Expected one geometry-verified nominal-diameter cylindrical shank; metadata may be stale.'
    body, face, planes, axis, bounds = candidates[0]
    segment = _cylinder_segment(face, axis)
    center = _dot(face['axis_point'], axis)
    if segment:
        # A head's outer flat lies beyond one of the cylindrical shank endpoints.
        head_planes = [p for p in planes if _vector(p.get('normal')) and _vector(p.get('centroid'))
                       and abs(_dot(p['normal'], axis)) > .999]
        before = [_dot(p['centroid'], axis) for p in head_planes
                  if _dot(p['centroid'], axis) < segment[0] - 1e-4]
        after = [_dot(p['centroid'], axis) for p in head_planes
                 if _dot(p['centroid'], axis) > segment[1] + 1e-4]
        if bool(before) == bool(after):
            return nominal, None, 'Head side is ambiguous in the current placed geometry.'
        sign = -1 if before else 1
        head_t = min(before) if before else max(after)
    else:
        # Legacy query without exact cylindrical extents: only use aligned AABB as
        # a conservative whole-component upper bound, never as threaded length.
        aligned = max(abs(x) for x in axis) > 1 - 1e-8
        left, right = center - bounds[0], bounds[1] - center
        if not aligned or abs(left - right) < 1e-4:
            return nominal, None, 'Cannot establish head direction and axial extent from placed geometry.'
        sign = -1 if left > right else 1
        head_t = bounds[0] if sign < 0 else bounds[1]
    head_point = [face['axis_point'][i] + axis[i] * (head_t - center) for i in range(3)]
    return nominal, {'body': body, 'face': face, 'planes': planes, 'axis': axis,
                     'bounds': bounds, 'shank_segment': segment, 'head_axis': [sign * a for a in axis],
                     'head_point': head_point}, None


def _bores(cid, shaft, faces):
    found = []
    f, axis = shaft['face'], shaft['axis']
    for other, groups in faces.items():
        if other == cid:
            continue
        for body, rounds, _ in groups:
            bounds = _bounds(body, axis)
            for bore in rounds:
                if (bore.get('concavity') not in ('bore', 'concave') or
                    not _positive(bore.get('radius')) or not _vector(bore.get('axis')) or
                    not _vector(bore.get('axis_point'))):
                    continue
                angle = _axis_angle_deg(axis, bore['axis'])
                offset = _perp_distance_between_axes(f['axis_point'], axis, bore['axis_point'], bore['axis'])
                # Near-coaxial candidates only: assembly topology cannot establish intent.
                if angle <= .5 and offset <= max(1., bore['radius']):
                    segment = _cylinder_segment(bore, axis)
                    cylinder = bore.get('cylinder') if isinstance(bore.get('cylinder'), dict) else {}
                    ends = cylinder.get('ends')
                    start, end = cylinder.get('axial_start'), cylinder.get('axial_end')
                    if isinstance(ends, list) and len(ends) == 2 and _vector(start) and _vector(end):
                        if _dot(start, axis) > _dot(end, axis):
                            ends = list(reversed(ends))
                    found.append({'component': other, 'sid': bore.get('sid'),
                                  'diameter_mm': 2 * bore['radius'], 'axis_offset_mm': offset,
                                  'axis_angle_deg': angle, 'axis_point': bore['axis_point'],
                                  'axial_point_mm': _dot(bore['axis_point'], axis),
                                  'axial_segment_mm': segment,
                                  'ends': ends, 'body_axial_bounds_mm': bounds})
    return found[:_MAX], len(found) > _MAX


def _stack_geometry(shaft, bores):
    """Report finite coaxial bore segments without assigning unproved joint roles."""
    candidates = [b for b in bores if b['axial_segment_mm'] and b['axis_angle_deg'] < .01]
    intervals = sorted((b['axial_segment_mm'][0], b['axial_segment_mm'][1]) for b in candidates)
    if not intervals:
        return _unknown('grip', 'No complete finite coaxial bore intervals were established.')
    merged = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1] + 1e-4:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    span = merged[-1][1] - merged[0][0]
    segment = shaft['shank_segment']
    length = segment[1] - segment[0] if segment else None
    return _unknown('grip', 'Finite bore span is not necessarily the intended clamped grip.',
                    candidate_intervals_mm=merged, candidate_span_mm=span,
                    geometry_shank_length_mm=length,
                    candidate_span_minus_shank_mm=span - length if length is not None else None,
                    gap_count=max(0, len(merged) - 1))


def _counterbore_geometry(shaft, bores):
    rows = []
    for i, a in enumerate(bores):
        for b in bores[i + 1:]:
            if a['component'] != b['component'] or not a['axial_segment_mm'] or not b['axial_segment_mm']:
                continue
            if abs(a['diameter_mm'] - b['diameter_mm']) < .01:
                continue
            if _perp_distance_between_axes(a['axis_point'], shaft['axis'], b['axis_point'], shaft['axis']) > .01:
                continue
            ends_a, ends_b = a.get('ends'), b.get('ends')
            if not isinstance(ends_a, list) or len(ends_a) != 2 or not isinstance(ends_b, list) or len(ends_b) != 2:
                continue
            adjacent_transition = any(
                abs(a['axial_segment_mm'][ia] - b['axial_segment_mm'][ib]) < .01 and
                any(isinstance(end, dict) and end.get('classification') == 'coaxial_transition'
                    for end in (ends_a[ia], ends_b[ib]))
                for ia in range(2) for ib in range(2))
            if adjacent_transition:
                minor, major = sorted((a, b), key=lambda item: item['diameter_mm'])
                rows.append({'component': a['component'], 'minor_sid': minor['sid'], 'major_sid': major['sid'],
                             'minor_diameter_mm': minor['diameter_mm'], 'major_diameter_mm': major['diameter_mm'],
                             'major_depth_mm': major['axial_segment_mm'][1] - major['axial_segment_mm'][0]})
    return _unknown('counterbore', 'Connected stepped bores are geometric candidates; head seating and tool clearance require separate evidence.',
                    candidates=rows[:_MAX], candidates_omitted=max(0, len(rows) - _MAX))


def _bearing_seat(cid, shaft, faces):
    segment = shaft['shank_segment']
    if not segment:
        return _unknown('head_washer_seat', 'Exact head/shank transition unavailable.')
    head_sign = _dot(shaft['head_axis'], shaft['axis'])
    seat_t = segment[0 if head_sign < 0 else 1]
    def annulus(face):
        a = face.get('bearing_annulus')
        if (not isinstance(a, dict) or a.get('adjacency_complete') is not True or
                not _vector(a.get('center')) or not _vector(a.get('normal')) or
                not _positive(a.get('inner_radius')) or not _positive(a.get('outer_radius'))):
            return None
        return a
    head = [(p, annulus(p)) for p in shaft['planes']]
    head = [(p, a) for p, a in head if a and abs(_dot(a['center'], shaft['axis']) - seat_t) < 1e-3
            and abs(_dot(_unit(a['normal']), shaft['axis'])) > .999
            and _perp_distance_between_axes(a['center'], shaft['axis'], shaft['face']['axis_point'], shaft['axis']) < 1e-3]
    for hp, h in head:
        for other, groups in faces.items():
            if other == cid:
                continue
            for _, _, planes in groups:
                for plane in planes:
                    a = annulus(plane)
                    if not a or _dot(_unit(h['normal']), _unit(a['normal'])) > -.999:
                        continue
                    delta = [a['center'][i] - h['center'][i] for i in range(3)]
                    gap = abs(_dot(delta, shaft['axis']))
                    offset = _perp_distance_between_axes(h['center'], shaft['axis'], a['center'], shaft['axis'])
                    if (gap < 1e-3 and offset < 1e-3 and a['inner_radius'] <= h['inner_radius'] + 1e-3
                            and a['outer_radius'] >= h['outer_radius'] - 1e-3):
                        return {'check': 'head_washer_seat', 'status': 'pass', 'component': other,
                                'head_sid': hp.get('sid'), 'seat_sid': plane.get('sid'),
                                'gap_mm': gap, 'axis_offset_mm': offset,
                                'basis': 'Coincident, oppositely oriented finite annuli; entire head annulus supported.'}
    return _unknown('head_washer_seat', 'No fully supporting, coincident finite annular bearing face was established.')


def _fastener_row(cid, meta, faces, position, thread_requirements):
    if meta.get('family') not in _SCREWS:
        return {'component': cid, 'family': meta.get('family'), 'size': meta.get('size'),
                'status': 'not_evaluated', 'checks': [_unknown(
                    'geometry', 'Headed-screw joint checks do not establish this fastener family’s role.')]}
    nominal, shaft, reason = _shaft(cid, meta, faces)
    row = {'component': cid, 'family': meta.get('family'), 'size': meta.get('size'),
           'nominal_mm': nominal, 'checks': []}
    checks = row['checks']
    if not shaft:
        checks.append(_unknown('geometry', reason))
        row['status'] = 'not_evaluated'
        return row
    bores, truncated = _bores(cid, shaft, faces)
    row['bore_candidates'] = bores
    row['bore_candidates_truncated'] = truncated
    row['geometry'] = {'axis': shaft['axis'], 'shank_diameter_mm': 2 * shaft['face']['radius'],
                       'component_axial_bounds_mm': shaft['bounds'], 'head_point': shaft['head_point'],
                       'shank_axial_segment_mm': shaft['shank_segment']}
    if not bores:
        checks.append(_unknown('hole_stack', 'No near-coaxial bore detected; intended joint is unknown.'))
    for bore in bores:
        spec = iso273_clearance_hole(nominal, 'normal') if nominal else {}
        clearance = bore['diameter_mm'] - 2 * shaft['face']['radius'] - 2 * bore['axis_offset_mm']
        checks.append(_unknown('radial_clearance',
                               'Bore role and overlapping trimmed cylindrical extents are not established.',
                               component=bore['component'], sid=bore['sid'], modeled_margin_mm=clearance))
        # A small hole can be tapped; do not classify it as clearance without intent.
        checks.append(_unknown('iso273_clearance', 'Bore role (clearance or tapped) is not established.',
                               component=bore['component'], sid=bore['sid'],
                               modeled_diameter_mm=bore['diameter_mm'], iso273_normal=spec,
                               modeled_meets_iso273=(bore['diameter_mm'] >= spec['diameter_mm']
                                                     if spec.get('ok') else None)))
        segment = bore['axial_segment_mm']
        ends = bore.get('ends')
        if segment and isinstance(ends, list) and len(ends) == 2 and all(isinstance(e, dict) for e in ends):
            classes = [e.get('classification') for e in ends]
            if 'closed_bottom' in classes:
                checks.append({'check': 'through_or_blind', 'status': 'pass', 'component': bore['component'],
                               'sid': bore['sid'], 'classification': 'blind', 'end_classification': classes,
                               'note': 'Classification is geometric, not a thread or strength verdict.'})
            elif classes == ['open_to_exterior', 'open_to_exterior']:
                checks.append({'check': 'through_or_blind', 'status': 'pass', 'component': bore['component'],
                               'sid': bore['sid'], 'classification': 'through', 'end_classification': classes})
            else:
                checks.append(_unknown('through_or_blind', 'One or both bore ends are not proven open or closed.',
                                       component=bore['component'], sid=bore['sid'], end_classification=classes))
            if shaft['shank_segment']:
                head_sign = _dot(shaft['head_axis'], shaft['axis'])
                tip = shaft['shank_segment'][1 if head_sign < 0 else 0]
                far = segment[1 if head_sign < 0 else 0]
                protrusion = (tip - far) * -head_sign
                checks.append(_unknown('protrusion', 'Cylindrical shank end is measured; complete tip, required protrusion and threaded length are unspecified.',
                                       component=bore['component'], sid=bore['sid'], modeled_shank_end_beyond_end_mm=protrusion))
                far_index = 1 if head_sign < 0 else 0
                near_index = 1 - far_index
                if (classes[far_index] == 'closed_bottom' and bore['axis_angle_deg'] < .01
                        and clearance >= -1e-4 and (tip - segment[near_index]) * -head_sign >= 0):
                    past_bottom = (tip - segment[far_index]) * -head_sign
                    whole_tip = shaft['bounds'][1 if head_sign < 0 else 0]
                    whole_past = (whole_tip - segment[far_index]) * -head_sign
                    verdict = 'fail' if past_bottom > 1e-4 else 'pass' if whole_past < -1e-4 else 'not_evaluated'
                    checks.append({'check': 'blind_bottom_clearance', 'component': bore['component'],
                                   'sid': bore['sid'], 'status': verdict,
                                   'modeled_tip_to_bottom_mm': -past_bottom,
                                   'basis': 'Cylindrical shank endpoint and conservative whole-component bound; excludes drill-point/thread allowances.'})
        lo, hi = shaft['bounds']
        t = bore['axial_point_mm']
        bounds = bore['body_axial_bounds_mm']
        if bounds and (bounds[0] > hi + 1e-4 or bounds[1] < lo - 1e-4):
            checks.append({'check': 'axial_reach', 'status': 'fail', 'component': bore['component'],
                           'sid': bore['sid'], 'reason': 'Candidate receiver body and fastener bounding extents do not overlap; candidate joint cannot engage.',
                           'bore_axial_point_mm': t, 'fastener_axial_bounds_mm': [lo, hi],
                           'receiver_axial_bounds_mm': bounds})
        elif t < lo - 1e-4 or t > hi + 1e-4:
            checks.append(_unknown('axial_reach', 'The bore center lies beyond the fastener but its trimmed endpoints and required engagement are unknown.',
                                   component=bore['component'], sid=bore['sid'], bore_axial_point_mm=t))
    for name, why in (
        ('through_or_blind', 'An end opening or complete connected bore is not always established.'),
        ('protrusion', 'Required thread protrusion is not established.'),
    ):
        checks.append(_unknown(name, why))
    checks.append(_stack_geometry(shaft, bores))
    checks.append(_counterbore_geometry(shaft, bores))
    checks.append(_bearing_seat(cid, shaft, faces))
    requirement = thread_requirements.get(cid) if isinstance(thread_requirements, dict) else None
    engagement = _unknown('thread_engagement', 'Receiving material, grade, pitch and thread extent are not established.')
    if requirement is not None:
        if (not isinstance(requirement, dict) or not isinstance(requirement.get('receiving_component'), str)
                or not isinstance(requirement.get('material_id'), str) or not isinstance(requirement.get('grade'), str)
                or not _positive(requirement.get('pitch_mm'))):
            raise ValueError('thread_requirements entries need receiving_component, material_id, grade and pitch_mm')
        receiver = requirement['receiving_component']
        receiver_bores = [b for b in bores if b['component'] == receiver and b['body_axial_bounds_mm']]
        calculation = thread_engagement(nominal, requirement['grade'], requirement['material_id'],
                                        requirement['pitch_mm'])
        engagement['calculation'] = calculation
        if receiver_bores and calculation.get('ok'):
            lo, hi = shaft['bounds']
            upper = max(max(0., min(hi, b['body_axial_bounds_mm'][1]) -
                            max(lo, b['body_axial_bounds_mm'][0])) for b in receiver_bores)
            minimum = calculation['answer']['recommended_engagement_mm']
            engagement.update(component=receiver, maximum_possible_overlap_mm=upper,
                              recommended_engagement_mm=minimum)
            if upper < minimum:
                engagement.update(status='fail', reason='Even the bounding extents overlap less than recommended thread engagement.')
            else:
                engagement['reason'] = 'Bounding overlap does not establish actual threaded length or engagement.'
        else:
            engagement['reason'] = 'Receiving bore geometry or material/grade calculation unavailable.'
    checks.append(engagement)
    p = position.get(cid) if isinstance(position, dict) else None
    if p is not None and bores:
        if (not isinstance(p, dict) or p.get('joint') not in ('fixed', 'floating')
                or not _positive(p.get('hole_position_mm')) or not _positive(p.get('hole_mmc_mm'))
                or not _positive(p.get('fastener_mmc_mm'))):
            raise ValueError('position_tolerances requires joint, hole_position_mm, hole_mmc_mm and fastener_mmc_mm')
        # ASME position zones are diametral; floating assumes equal zones for two members.
        h = p['hole_mmc_mm']
        allowance = (h - p['fastener_mmc_mm']) / (2 if p['joint'] == 'floating' else 1)
        checks.append({'check': 'position_tolerance', 'status': 'pass' if p['hole_position_mm'] <= allowance else 'fail',
                       'joint': p['joint'], 'specified_diametral_zone_mm': p['hole_position_mm'],
                       'maximum_diametral_zone_mm': allowance,
                       'basis': 'ASME Y14.5 fixed H−F; equal floating zones (H−F)/2 at supplied MMC; no bonus or datum shift'})
    else:
        checks.append(_unknown('position_tolerance', 'Joint type and specified diametral position zones are not supplied.'))
    row['status'] = _status(checks)
    return row


def _access_row(doc, cid, meta, faces):
    if meta.get('family') not in _SCREWS:
        return {'component': cid, 'family': meta.get('family'), 'size': meta.get('size'),
                'status': 'not_evaluated', 'reason': 'Tool geometry for this fastener family is not established.'}
    nominal, shaft, reason = _shaft(cid, meta, faces)
    row = {'component': cid, 'family': meta.get('family'), 'size': meta.get('size')}
    if not shaft:
        return {**row, 'status': 'not_evaluated', 'reason': reason}
    specs = meta.get('specs') if isinstance(meta.get('specs'), dict) else {}
    family = meta.get('family')
    if family not in ('hex_bolt', 'shcs'):
        return {**row, 'status': 'not_evaluated',
                'reason': 'Screwdriver or key recess is not represented by verified geometry.'}
    af = specs.get('hex_af' if family == 'hex_bolt' else 'socket_key_width')
    # Confirm opposite planar faces at the nominal head width before trusting source metadata.
    planes = [p for p in shaft['planes'] if _vector(p.get('normal')) and _vector(p.get('centroid'))
              and _dot([p['centroid'][i] - shaft['face']['axis_point'][i] for i in range(3)],
                       shaft['head_axis']) > 0]
    paired = any(abs(_dot(a['normal'], b['normal']) + 1) < .01 and
                 abs(abs(_dot([a['centroid'][i] - b['centroid'][i] for i in range(3)], a['normal'])) - af) < .05
                 and abs(_dot(a['normal'], shaft['axis'])) < .01
                 for i, a in enumerate(planes) for b in planes[i + 1:]) if _positive(af) else False
    if not paired:
        return {**row, 'status': 'not_evaluated', 'reason': 'Head or recess flats do not match the nominal metadata.'}
    origin, axis = shaft['head_point'], shaft['head_axis']
    if family == 'hex_bolt':
        if shaft['shank_segment']:
            sign = _dot(axis, shaft['axis'])
            seat_t = shaft['shank_segment'][0 if sign < 0 else 1]
            delta = seat_t - _dot(origin, shaft['axis'])
            origin = [origin[i] + shaft['axis'][i] * delta for i in range(3)]
        data = _SOCKET.get(round(af)) if abs(af - round(af)) < .05 else None
        if not data:
            return {**row, 'status': 'not_evaluated', 'reason': 'No ISO 2725-1 Table 3 socket row for this head.'}
        d1, d2, length = data
        # The table does not specify where its maximum diameters transition.
        split = length / 2
        start2 = [origin[i] + axis[i] * split for i in range(3)]
        solids = [{'id': 'operating_end', 'cylinder': {'origin': origin, 'axis': axis, 'radius': d1 / 2, 'length': split}},
                  {'id': 'drive_end', 'cylinder': {'origin': start2, 'axis': axis, 'radius': d2 / 2, 'length': length - split}}]
        tool, source = '12.5 mm drive socket', _SOCKET_SOURCE
    else:
        # A circle through a regular hex key's corners bounds its straight shaft.
        # Length is a screening extent, not a claim about a purchased key or its handle.
        solids = [{'id': 'hex_key_shaft', 'cylinder': {'origin': origin, 'axis': axis,
                   'radius': af / math.sqrt(3), 'length': 30.0}}]
        tool, source = 'hex key', 'Circumscribed circle of the geometry-verified hexagonal drive'
    result = cb.call('eval_envelope_clearance', {'doc_id': doc, 'ignore': [cid], 'solids': solids})
    results = result.get('results') or []
    hits = [hit for item in results for hit in (item.get('hits') or [])]
    complete = result.get('complete') is True and len(results) == len(solids) and all(
        isinstance(item.get('hits'), list) and not item.get('failed') for item in results)
    bounded_results = []
    for item in results[:len(solids)]:
        bounded = dict(item)
        for key in ('hits', 'failed'):
            entries = item.get(key)
            if isinstance(entries, list):
                bounded[key] = entries[:_MAX]
                if len(entries) > _MAX:
                    bounded[key + '_omitted'] = len(entries) - _MAX
        bounded_results.append(bounded)
    return {**row, 'status': 'not_evaluated',
            'modeled_envelope_status': 'fail' if hits else 'pass' if complete else 'not_evaluated',
            'tool': tool, 'across_flats_mm': af, 'sections': solids, 'results': bounded_results,
            'source_failures': (result.get('source_failures') or [])[:_MAX], 'complete': complete,
            'source': source,
            'reason': ('Illustrative envelope intersects geometry, indicating a potential obstruction; the actual tool is not specified.'
                       if hits else 'No hit in illustrative finite envelope; actual tool and approach are unverified.')}


def register_fastener_tools(mcp):
    @mcp.tool()
    def fastener_check(doc_id: Optional[str] = None, components: Optional[list] = None,
                       position_tolerances: Optional[dict] = None,
                       thread_requirements: Optional[dict] = None) -> str:
        """Discover placed standard screws and screen their current coaxial geometry.
        position_tolerances maps component IDs to {joint: fixed|floating,
        hole_position_mm, hole_mmc_mm, fastener_mmc_mm}; values are diametral.
        Missing evidence is not_evaluated.
        thread_requirements maps screw IDs to {receiving_component, material_id,
        grade, pitch_mm}; material_id is a real services/sim/materials.json key.
        Nominal metadata is not proof of a thread, material, fit or manufacturing tolerance.
        """
        try:
            doc = _doc(doc_id)
            if position_tolerances is not None and not isinstance(position_tolerances, dict):
                raise ValueError('position_tolerances must be an object keyed by component ID')
            if thread_requirements is not None and not isinstance(thread_requirements, dict):
                raise ValueError('thread_requirements must be an object keyed by component ID')
            bodies, candidates, unknown, world = _world(doc, components)
            faces, errors = _faces(doc, bodies)
            rows = [_fastener_row(cid, meta, faces, position_tolerances, thread_requirements)
                    for cid, meta in candidates[:_MAX]] + unknown[:_MAX]
            if errors or world.get('complete') is False or len(candidates) > _MAX or len(unknown) > _MAX:
                rows.append(_unknown('geometry_completeness', 'Some source geometry or candidates were unavailable or truncated.'))
            return _dumps({'ok': True, 'doc_id': doc, 'status': _status(rows), 'fasteners': rows,
                           'geometry_errors': errors[:_MAX], 'source_failures': world.get('source_failures', [])[:_MAX],
                           'assumptions': ['Bore association is geometric proximity, not proof of joint intent.',
                                           'Geometry and ISO table screens do not establish actual thread or manufacturing tolerances.']})
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def tool_access_check(doc_id: Optional[str] = None, components: Optional[list] = None) -> str:
        """Discover standard screws and screen finite tool envelopes in placed assembly geometry.

        ISO socket limits describe a conservative illustrative shape, not a specific tool;
        clear envelopes, unverified drives and incomplete geometry remain not_evaluated.
        """
        try:
            doc = _doc(doc_id)
            bodies, candidates, unknown, world = _world(doc, components)
            faces, errors = _faces(doc, bodies)
            rows = [_access_row(doc, cid, meta, faces) for cid, meta in candidates[:_MAX]] + unknown[:_MAX]
            if errors or world.get('complete') is False or len(candidates) > _MAX or len(unknown) > _MAX:
                rows.append(_unknown('geometry_completeness', 'Some source geometry or candidates were unavailable or truncated.'))
            return _dumps({'ok': True, 'doc_id': doc, 'status': _status(rows), 'fasteners': rows,
                           'geometry_errors': errors[:_MAX], 'source_failures': world.get('source_failures', [])[:_MAX]})
        except Exception as exc:
            return _err(exc)
