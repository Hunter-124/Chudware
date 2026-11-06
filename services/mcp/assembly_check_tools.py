"""Read-only assembly checks using placed geometry and source-document metadata."""
from __future__ import annotations

import math
import os
from collections import Counter
from typing import Optional

from services.mcp import core_bridge as cb
from services.mcp.cad_common import _doc, _dumps, _err
from services.mcp.cad_asm_tools import component_source_file, open_component_part
from services.mcp.interface_check_tools import (
    _round_faces_for_body, _cylinders_from_faces, _axis_angle_deg,
    _perp_distance_between_axes,
)
from services.research.fits import fit_calc

_MAX_ROWS = 100


def _number(value, name, *, zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (not zero and value == 0)):
        raise ValueError(f"{name} must be finite and {'non-negative' if zero else 'positive'}")
    return float(value)


def _components(doc):
    tree = cb.call('doc_tree', {'doc_id': doc}).get('tree') or {}
    return {c['id']: c for c in tree.get('components', []) if isinstance(c, dict) and c.get('id')}


def _component(comps, component):
    if component not in comps:
        raise ValueError(f"Unknown component {component!r}; available: {list(comps)[:_MAX_ROWS]}")
    return comps[component]


def _source_meta(doc, comp):
    path = component_source_file(comp, doc)
    with open_component_part(path) as part:
        catalog = cb.call('doc_get_meta', {'doc_id': part, 'key': 'parts_catalog'}).get('value')
        standard = cb.call('doc_get_meta', {'doc_id': part, 'key': 'standard_part'}).get('value')
    value = dict(catalog) if isinstance(catalog, dict) else {}
    if not isinstance(value.get('generated'), dict) and isinstance(standard, dict):
        value['generated'] = standard
    return value


def _metadata(meta):
    # Catalog placement wraps the original generated metadata in source_catalog.
    records = []
    for _ in range(8):
        if not isinstance(meta, dict):
            break
        records.append(meta)
        meta = meta.get('source_catalog')
    record = next((m['part_record'] for m in records if isinstance(m.get('part_record'), dict)), {})
    generated = next((m['generated'] for m in records if isinstance(m.get('generated'), dict)), {})
    return record, generated

def _fastener_meta(record, generated):
    if generated:
        return generated
    ref = record.get('geometry_ref') if isinstance(record, dict) else None
    if isinstance(ref, dict) and ref.get('kind') == 'core_family':
        return {'family': ref.get('family'), 'size': ref.get('size')}
    return {}



def _face(doc, component, sid, kind):
    bodies = cb.call('asm_world_bodies', {'doc_id': doc, 'explode': False}).get('bodies', [])
    indices = [i + 1 for i, body in enumerate(bodies) if body.get('component') == component]
    if not indices:
        raise ValueError(f'No placed body for component {component!r}')
    matches = []
    for index in indices:
        faces = [f for f in _round_faces_for_body(cb, doc, index)
                 if f.get('sid') == sid and f.get('concavity') in
                 (('bore', 'concave') if kind == 'bore' else ('boss', 'convex'))]
        bores, bosses = _cylinders_from_faces(faces)
        matches.extend(bores if kind == 'bore' else bosses)
    if len(matches) != 1:
        raise ValueError(f'{sid!r} must identify exactly one {kind} cylinder on {component!r}')
    return matches[0]


def _status(checks):
    statuses = [c['status'] for c in checks]
    if 'fail' in statuses:
        return 'fail'
    if not statuses or 'not_evaluated' in statuses:
        return 'not_evaluated'
    return 'pass'


def _hardware_coverage(doc, bodies, comps, metadata):
    """Associate verified finite screw shanks with finite bore sites, without inferring intent."""
    from services.mcp import assembly_fastener_checks as fast
    if len(bodies) > _MAX_ROWS:
        return {'status': 'not_evaluated', 'reason': 'More than 100 placed bodies', 'patterns': []}
    faces, errors = fast._faces(doc, bodies)
    def seed(cid):
        if cid in comps:
            return cid
        candidates = [key for key in comps if isinstance(cid, str) and cid.startswith(key + '#')]
        return max(candidates, key=len) if candidates else None
    screws, unknown_fasteners = [], []
    for cid in faces:
        generated = _fastener_meta(*metadata.get(seed(cid), ({}, {})))
        if generated.get('family') not in fast._SCREWS:
            continue
        nominal, shaft, reason = fast._shaft(cid, generated, faces)
        if not shaft or not shaft.get('shank_segment'):
            unknown_fasteners.append({'component': cid, 'reason': reason or 'Finite shank extent unavailable.'})
        else:
            screws.append({'component': cid, 'nominal_mm': nominal, 'shaft': shaft})
    sites = []
    incomplete = bool(errors or unknown_fasteners)
    for cid, groups in faces.items():
        generated = _fastener_meta(*metadata.get(seed(cid), ({}, {})))
        if generated.get('family') in fast._FASTENERS:
            continue
        for body_index, (_, rounds, _) in enumerate(groups):
            for face in rounds:
                if face.get('concavity') not in ('bore', 'concave'):
                    continue
                if not fast._positive(face.get('radius')) or not fast._vector(face.get('axis')) or not fast._vector(face.get('axis_point')):
                    incomplete = True
                    continue
                axis = fast._unit(face['axis'])
                segment = fast._cylinder_segment(face, axis)
                if not segment:
                    incomplete = True
                site = {'component': cid, 'body': body_index, 'sid': face.get('sid'),
                        'diameter_mm': 2*face['radius'], 'axis': list(axis),
                        'axis_point': face['axis_point'], 'segment': segment, 'faces': [face]}
                # Merge coincident seam fragments only; distinct stepped tiers remain
                # separate candidates unless their topology proves a transition.
                if segment and any(s['component'] == cid and s['body'] == body_index
                        and s['segment'] and abs(s['diameter_mm'] - site['diameter_mm']) < 1e-4
                        and _axis_angle_deg(s['axis'], axis) < .01
                        and _perp_distance_between_axes(s['axis_point'], s['axis'], site['axis_point'], axis) < .01
                        and max(abs(a-b) for a, b in zip(s['segment'], fast._cylinder_segment(face, s['axis']))) < 1e-4 for s in sites):
                    continue
                sites.append(site)
                if len(sites) > _MAX_ROWS:
                    return {'status': 'not_evaluated', 'reason': 'More than 100 bore faces', 'patterns': []}
    def transition(a, b):
        if a['component'] != b['component'] or a['body'] != b['body'] or not a['segment'] or not b['segment']:
            return False
        if _axis_angle_deg(a['axis'], b['axis']) >= .01 or _perp_distance_between_axes(
                a['axis_point'], a['axis'], b['axis_point'], b['axis']) >= .01:
            return False
        for left in a['faces']:
            for right in b['faces']:
                lc, rc = left.get('cylinder') or {}, right.get('cylinder') or {}
                le, re = lc.get('ends') or [], rc.get('ends') or []
                lp = [lc.get('axial_start'), lc.get('axial_end')]
                rp = [rc.get('axial_start'), rc.get('axial_end')]
                if len(le) != 2 or len(re) != 2:
                    continue
                for i in range(2):
                    for j in range(2):
                        l, r = le[i], re[j]
                        if not isinstance(l, dict) or not isinstance(r, dict) or not fast._vector(lp[i]) or not fast._vector(rp[j]):
                            continue
                        if (l.get('classification') != 'coaxial_transition' or r.get('classification') != 'coaxial_transition'
                                or not l.get('adjacency_complete') or not r.get('adjacency_complete')
                                or math.dist(lp[i], rp[j]) > .01):
                            continue
                        la = {x.get('topo') for x in l.get('adjacent_faces', []) if isinstance(x, dict) and x.get('topo') is not None}
                        ra = {x.get('topo') for x in r.get('adjacent_faces', []) if isinstance(x, dict) and x.get('topo') is not None}
                        if la & ra:
                            return True
        return False
    # Adjacent tiers must share the proven annular transition, not just an axis.
    for i in range(len(sites)):
        if sites[i] is None:
            continue
        for j in range(i + 1, len(sites)):
            if sites[j] is not None and transition(sites[i], sites[j]):
                sites[i]['faces'].extend(sites[j]['faces'])
                sites[i]['diameter_mm'] = min(sites[i]['diameter_mm'], sites[j]['diameter_mm'])
                sites[j] = None
    sites = [site for site in sites if site is not None]
    patterns = []
    for site in sites:
        matched, uncertain = [], not bool(site['segment'])
        for item in screws:
            shaft = item['shaft']
            if _axis_angle_deg(site['axis'], shaft['axis']) > .5:
                continue
            if _perp_distance_between_axes(site['axis_point'], site['axis'],
                                           shaft['face']['axis_point'], shaft['axis']) > .1:
                continue
            if site['diameter_mm'] + .05 < item['nominal_mm']:
                continue
            bore_segments = [fast._cylinder_segment(face, shaft['axis']) for face in site['faces']]
            shank_segment = shaft['shank_segment']
            if any(not segment for segment in bore_segments):
                uncertain = True
            elif any(min(segment[1], shank_segment[1]) - max(segment[0], shank_segment[0]) > 1e-4
                     for segment in bore_segments):
                matched.append(item['component'])
        if len(matched) > 1:
            uncertain = True
        rec = {'sid': site['sid'], 'axis_point': site['axis_point'], 'fasteners': matched,
               'status': 'not_evaluated' if uncertain or not matched else 'pass',
               'observation': 'associated' if len(matched) == 1 and not uncertain else
                              'ambiguous' if matched or uncertain else 'no_detected_fastener'}
        group = next((p for p in patterns if p['component'] == site['component']
                      and abs(p['diameter_mm'] - site['diameter_mm']) < .05
                      and _axis_angle_deg(p['axis'], site['axis']) < .5), None)
        if group is None:
            group = {'component': site['component'], 'diameter_mm': site['diameter_mm'],
                     'axis': site['axis'], 'sites': []}
            patterns.append(group)
        group['sites'].append(rec)
    for group in patterns:
        group['hole_count'] = len(group['sites'])
        group['associated_fastener_count'] = len({f for s in group['sites'] for f in s['fasteners']})
        group['without_detected_fastener'] = sum(s['observation'] == 'no_detected_fastener' for s in group['sites'])
        group['status'] = 'pass' if all(s['status'] == 'pass' for s in group['sites']) and not incomplete else 'not_evaluated'
    return {'status': 'not_evaluated' if incomplete or not patterns or any(p['status'] != 'pass' for p in patterns) else 'pass',
            'patterns': patterns, 'recognized_screw_count': len(screws),
            'unknown_fasteners': unknown_fasteners[:32], 'unknown_fastener_count': len(unknown_fasteners),
            'geometry_errors': errors[:32], 'geometry_error_count': len(errors),
            'assumptions': ['Bore groups are per component and diameter; stepped tiers are not merged without topological proof.',
                            'A bore without a recognized coaxial finite screw is not proof that hardware is intended or absent.',
                            'Non-standard, unrecognized and unthreaded hardware may be present.']}


def register_assembly_check_tools(mcp):
    from services.mcp.assembly_fastener_checks import register_fastener_tools
    register_fastener_tools(mcp)

    @mcp.tool()
    def assembly_dof(doc_id: Optional[str] = None, cursor: int = 0, limit: int = 100) -> str:
        """Report instantaneous solved-pose mate Jacobian freedoms, including disconnected groups.
        Per-component projected freedoms are not additive; this is not a finite motion proof."""
        try:
            d = _doc(doc_id)
            result = cb.call('eval_assembly_dof', {'doc_id': d, 'cursor': cursor, 'limit': limit})
            return _dumps(result)
        except Exception as exc:
            return _err(exc)


    @mcp.tool()
    def disassembly_check(doc_id: Optional[str] = None, components: Optional[list] = None,
                          distance_mm: Optional[float] = None) -> str:
        """Test straight removal along +/- world axes and resolved mate axes, then find a
        removal order from the resulting blocker graph. Tests at most 16 components and
        16 directions per component. This is not an exhaustive path or rotation search."""
        try:
            d = _doc(doc_id)
            world = cb.call('asm_world_bodies', {'doc_id': d, 'explode': False,
                                                   'include_mate_axes': True})
            bodies = world.get('bodies') or []
            by_id = {b.get('component'): b for b in bodies if b.get('component')}
            ids = list(by_id)
            if components is not None:
                if not isinstance(components, list) or not components or len(set(components)) != len(components):
                    raise ValueError('components must be a nonempty list of distinct IDs')
                unknown = [c for c in components if c not in by_id]
                if unknown:
                    raise ValueError(f'Unknown placed components: {unknown}')
                ids = components
            if len(ids) > 16:
                raise ValueError('Select at most 16 components per disassembly check')
            if not ids:
                return _dumps({'ok': True, 'status': 'not_evaluated', 'reason': 'No placed components', 'components': []})
            boxes = [b.get('bbox') for b in bodies]
            if not boxes or any(not isinstance(b, dict) or not isinstance(b.get('min'), list)
                                or not isinstance(b.get('max'), list) or len(b['min']) != 3
                                or len(b['max']) != 3 for b in boxes):
                return _dumps({'ok': True, 'status': 'not_evaluated', 'reason': 'World bounds unavailable'})
            lo = [min(b['min'][i] for b in boxes) for i in range(3)]
            hi = [max(b['max'][i] for b in boxes) for i in range(3)]
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in lo + hi):
                return _dumps({'ok': True, 'status': 'not_evaluated', 'reason': 'World bounds are non-finite'})
            travel = _number(distance_mm, 'distance_mm') if distance_mm is not None else 2 * math.dist(lo, hi) + 1
            axes = world.get('mate_axes') or {}
            queries = []
            truncated = []
            for cid in ids:
                directions = [[1., 0., 0.], [-1., 0., 0.], [0., 1., 0.],
                              [0., -1., 0.], [0., 0., 1.], [0., 0., -1.]]
                for entries in axes.values():
                    for axis in entries if isinstance(entries, list) else []:
                        v = axis.get('direction') if isinstance(axis, dict) and axis.get('component') == cid else None
                        if not isinstance(v, list) or len(v) != 3 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v):
                            continue
                        mag = math.sqrt(sum(x*x for x in v))
                        if mag < 1e-12:
                            continue
                        for sign in (1, -1):
                            candidate = [sign*x/mag for x in v]
                            if not any(sum(a*b for a, b in zip(candidate, existing)) > 1 - 1e-9 for existing in directions):
                                if len(directions) == 16:
                                    if cid not in truncated:
                                        truncated.append(cid)
                                else:
                                    directions.append(candidate)
                queries.append({'component': cid, 'directions': directions, 'distance_mm': travel})
            report = cb.call('eval_removal_clearance', {'doc_id': d, 'queries': queries,
                                                        'stop_at_first_hit': False})
            if report.get('ok') is False and 'results' not in report:
                return _dumps(report)
            grouped = {cid: [] for cid in ids}
            for r in report.get('results') or []:
                cid = r.get('component')
                if cid in grouped:
                    grouped[cid].append({'direction': r.get('direction'), 'clear': r.get('clear'),
                                         'blockers': [b.get('component') for b in r.get('blockers', [])],
                                         'failed': r.get('failed', [])})
            removed, order = set(), []
            remaining = set(ids)
            while remaining:
                choice = next(((cid, row) for cid in ids if cid in remaining for row in grouped[cid]
                               if ((row.get('clear') is True and not row['blockers'])
                                   or (row.get('clear') is False and row['blockers']))
                               and not row['failed'] and not report.get('source_failures')
                               and set(row['blockers']) <= removed), None)
                if not choice:
                    break
                cid, row = choice
                removed.add(cid)
                remaining.remove(cid)
                order.append({'component': cid, 'direction': row['direction']})
            incomplete = (not report.get('complete') or bool(truncated) or bool(report.get('source_failures'))
                          or any(len(grouped[q['component']]) != len(q['directions']) for q in queries)
                          or any(row.get('clear') not in (True, False) or row['failed']
                                 or (row['clear'] is False and not row['blockers'])
                                 or (row['clear'] is True and row['blockers'])
                                 for rows in grouped.values() for row in rows))
            status = 'pass' if not remaining and not report.get('source_failures') else 'not_evaluated' if incomplete else 'fail'
            public_rows = {}
            for cid in ids:
                public_rows[cid] = []
                for row in grouped[cid]:
                    public_rows[cid].append({**row, 'blockers': row['blockers'][:32],
                                             'blocker_count': len(row['blockers']),
                                             'failed': row['failed'][:32],
                                             'failure_count': len(row['failed'])})
            return _dumps({'ok': True, 'status': status, 'distance_mm': travel, 'removal_order': order,
                           'blocked_remaining': [cid for cid in ids if cid in remaining],
                           'components': [{'component': cid, 'directions': public_rows[cid]} for cid in ids],
                           'directions_truncated': truncated,
                           'source_failures': report.get('source_failures', [])[:32],
                           'source_failure_count': len(report.get('source_failures', [])),
                           'assumptions': ['Mate constraints are assumed released; unselected components remain.',
                                           'Only straight candidate sweeps were checked; failure is not proof of physical trapping.',
                                           'Initial overlap is an obstruction; contact-only interference is not detected.']})
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def mate_fit_check(doc_id: Optional[str] = None, mate_ids: Optional[list] = None,
                       nominal_mm: Optional[float] = None, fit: Optional[str] = None) -> str:
        """Measure bore/shaft cylinders referenced by concentric mates in world geometry.
        Report measured clearance character and the nearest tabulated ISO 286 candidate.
        Optionally compare nominal geometry with an explicitly requested fit; this is not
        proof of manufacturing tolerances or of the originally specified designation."""
        try:
            from services.mcp.cad_render_tools import _mate_face_matches
            from services.mcp.research_tools import fit_from_kwargs
            d = _doc(doc_id)
            tree = cb.call('doc_tree', {'doc_id': d}).get('tree') or {}
            comps = {c['id']: c for c in tree.get('components', []) if isinstance(c, dict) and c.get('id')}
            mates = [m for m in tree.get('mates', []) if isinstance(m, dict)
                     and m.get('type') == 'concentric' and not m.get('suppressed')]
            if mate_ids is not None:
                if not isinstance(mate_ids, list) or not mate_ids or any(not isinstance(x, str) for x in mate_ids):
                    raise ValueError('mate_ids must be a nonempty list of IDs')
                missing = set(mate_ids) - {m.get('id') for m in mates}
                if missing:
                    raise ValueError(f'Unknown concentric mate IDs: {sorted(missing)}')
                mates = [m for m in mates if m.get('id') in mate_ids]
            if len(mates) > _MAX_ROWS:
                raise ValueError('Select at most 100 concentric mates')
            if fit is not None and nominal_mm is None:
                raise ValueError('An explicit fit requires nominal_mm')
            if nominal_mm is not None:
                nominal_mm = _number(nominal_mm, 'nominal_mm')
            iso = fit_calc(nominal_mm, fit) if fit is not None else None
            world = cb.call('asm_world_bodies', {'doc_id': d, 'explode': False})
            bodies = world.get('bodies') or []
            face_cache = {}
            def resolve(ref):
                cid = ref.get('component')
                faces = []
                for idx, body in enumerate(bodies, 1):
                    if body.get('component') == cid:
                        if idx not in face_cache:
                            face_cache[idx] = _round_faces_for_body(cb, d, idx)
                        faces.extend(face_cache[idx])
                matches = _mate_face_matches(ref, comps, {'face': faces})
                if len(matches) != 1 or matches[0].get('concavity') not in ('bore', 'boss', 'concave', 'convex'):
                    return None
                bores, bosses = _cylinders_from_faces(matches)
                return (bores or bosses or [None])[0]
            checks = []
            for mate in mates:
                row = {'mate': mate.get('id'), 'status': 'not_evaluated'}
                refs = mate.get('refs') or []
                try:
                    found = [resolve(ref) for ref in refs] if len(refs) == 2 else []
                    if len(found) != 2 or any(f is None for f in found) or {f['kind'] for f in found} != {'bore', 'boss'}:
                        row['reason'] = 'Mate references do not resolve unambiguously to a bore and shaft cylinder.'
                    else:
                        bore = next(f for f in found if f['kind'] == 'bore')
                        shaft = next(f for f in found if f['kind'] == 'boss')
                        bd, sd = 2*bore['radius'], 2*shaft['radius']
                        row.update(bore_diameter_mm=bd, shaft_diameter_mm=sd,
                                   diametral_clearance_mm=bd-sd,
                                   axis_offset_mm=_perp_distance_between_axes(bore['centroid'], bore['axis'], shaft['centroid'], shaft['axis']),
                                   axis_angle_deg=_axis_angle_deg(bore['axis'], shaft['axis']))
                        inputs = {'hole_diameter': bd, 'shaft_diameter': sd}
                        if nominal_mm is not None:
                            inputs['nominal_mm'] = nominal_mm
                        measured = fit_from_kwargs(inputs, prefilled={'hole_diameter': 'world bore geometry',
                                                                      'shaft_diameter': 'world shaft geometry'})
                        measured_answer = measured.get('answer') or {}
                        candidates = measured_answer.get('closest_iso_fits') or []
                        row['fit_character'] = measured_answer.get('fit_character')
                        row['nearest_iso_fit'] = candidates[0] if candidates else None
                        row['classification_nominal_mm'] = measured_answer.get('nominal_mm')
                        row['classification_nominal_basis'] = 'caller' if nominal_mm is not None else 'inferred'
                        if not measured.get('ok'):
                            row['classification_reason'] = measured.get('hint') or measured.get('reason') or 'Measured fit calculation unavailable.'
                        answer = (iso or {}).get('answer') or {}
                        hole, shaft_band = answer.get('hole_absolute'), answer.get('shaft_absolute')
                        if iso and iso.get('ok') and hole and shaft_band:
                            row['status'] = 'pass' if hole[0]-1e-6 <= bd <= hole[1]+1e-6 and shaft_band[0]-1e-6 <= sd <= shaft_band[1]+1e-6 else 'fail'
                        else:
                            row['reason'] = 'No applicable combined ISO 286 fit bands supplied.'
                except (KeyError, TypeError, ValueError) as exc:
                    row['reason'] = f'Cannot resolve mate geometry: {exc}'
                checks.append(row)
            return _dumps({'ok': True, 'status': _status(checks), 'checks': checks, 'iso_fit': iso,
                           'assumptions': ['Model diameters are not manufacturing tolerance measurements.',
                                           'Nearest ISO candidate is ranked by modeled clearance, not established design intent or individual tolerance conformity.',
                                           'The basic size is inferred unless nominal_mm is supplied.',
                                           'Concentric mate selections express intent; axial overlap and fit strength are not assessed.']})
        except Exception as exc:
            return _err(exc)

    @mcp.tool()
    def bom_consistency_check(doc_id: Optional[str] = None) -> str:
        """Compare live native BOM rows, placed component/pattern quantities and source
        identity; screen geometric bore patterns against finite screw shanks. Unrecognized
        hardware and unknown hole intent remain not_evaluated, not invented omissions."""
        try:
            d = _doc(doc_id)
            comps = {k: v for k, v in _components(d).items() if not v.get('suppressed')}
            if len(comps) > _MAX_ROWS:
                raise ValueError('Assembly has more than 100 components; narrow the assembly first')
            native = cb.call('asm_bom_components', {'doc_id': d, 'cursor': 0, 'limit': _MAX_ROWS})
            if native.get('ok') is False:
                return _dumps(native)
            rows = native.get('components') or []
            world = cb.call('asm_world_bodies', {'doc_id': d, 'explode': False})
            ids = {b.get('component') for b in world.get('bodies', []) if b.get('component')}
            counts = Counter()
            for cid in ids:
                if cid in comps:
                    counts[cid] += 1
                else:
                    seeds = [seed for seed in comps if cid.startswith(seed + '#')]
                    if seeds:
                        counts[max(seeds, key=len)] += 1
            grouped = {}
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get('component'), str):
                    grouped.setdefault(row['component'], []).append(row)
            checks, metadata, source_errors, identity_groups, unsourced = [], {}, {}, {}, []
            for cid in sorted(set(comps) | set(grouped)):
                group = grouped.get(cid, [])
                comp = comps.get(cid)
                check = {'component': cid, 'bom_rows': len(group), 'placed_quantity': counts[cid],
                         'status': 'not_evaluated'}
                if not comp:
                    check.update(status='fail', reason='BOM row does not name an active assembly component.')
                elif not counts[cid]:
                    check['reason'] = 'Component has no resolved world geometry.'
                elif not group and native.get('truncated'):
                    check['reason'] = 'BOM page truncated before this component.'
                elif len(group) != 1:
                    check.update(status='fail', reason='Placed component has missing or duplicate BOM rows.')
                else:
                    row = group[0]
                    check.update(bom_quantity=row.get('quantity'), source=row.get('source'))
                    if row.get('quantity') != counts[cid]:
                        check.update(status='fail', reason='BOM quantity differs from placed instances.')
                    elif row.get('source') != (comp.get('source') or {}).get('path', ''):
                        check.update(status='fail', reason='BOM source differs from assembly component source.')
                    else:
                        try:
                            record, generated = _metadata(_source_meta(d, comp))
                            metadata[cid] = (record, generated)
                            check['part_ref'] = record.get('part_id')
                            if record or generated:
                                check['status'] = 'pass'
                                if record.get('part_id'):
                                    source = os.path.realpath(component_source_file(comp, d))
                                    identity_groups.setdefault((record['part_id'], source), []).append(cid)
                                from services.mcp.assembly_fastener_checks import _FASTENERS
                                if _fastener_meta(record, generated).get('family') in _FASTENERS:
                                    offers = record.get('sourcing')
                                    sourced = isinstance(offers, list) and any(isinstance(o, dict) and (
                                        o.get('product_url') or o.get('mpn') or
                                        (o.get('supplier') and o.get('part_number'))) for o in offers)
                                    if not sourced and not record.get('mpn') and not comp.get('mpn'):
                                        unsourced.append({'component': cid, 'part_ref': record.get('part_id'),
                                                          'status': 'not_evaluated',
                                                          'reason': 'No supplier or manufacturer part number in component source metadata.'})
                            else:
                                check['reason'] = 'No source catalog identity or generated metadata available.'
                        except Exception as exc:
                            source_errors[cid] = str(exc)
                            if check['status'] != 'fail':
                                check['status'] = 'not_evaluated'
                                check['reason'] = f'Source metadata unavailable: {exc}'
                checks.append(check)
            repeated = [{'part_ref': key[0], 'source': key[1], 'components': ids,
                         'status': 'not_evaluated',
                         'reason': 'Multiple component rows share one source identity; separate placements may be intentional.'}
                        for key, ids in identity_groups.items() if len(ids) > 1]
            coverage = _hardware_coverage(d, world.get('bodies') or [], comps, metadata)
            if source_errors or any(cid not in metadata or not any(metadata[cid]) for cid in comps):
                coverage['status'] = 'not_evaluated'
                coverage['source_evidence_incomplete'] = True
            status = _status(checks)
            if (native.get('truncated') or coverage['status'] != 'pass' or unsourced) and status != 'fail':
                status = 'not_evaluated'
            return _dumps({'ok': True, 'status': status, 'checks': checks, 'hardware_coverage': coverage,
                           'repeated_source_lines': repeated, 'unsourced_components': unsourced,
                           'truncated': bool(native.get('truncated')), 'next_cursor': native.get('next_cursor'),
                           'assumptions': ['Counts compare direct live component instances, including patterns.',
                                           'Repeated component rows can be intentional instances, not necessarily BOM duplicates.',
                                           'Missing supplier metadata is not proof that a source does not exist.']})
        except Exception as exc:
            return _err(exc)
