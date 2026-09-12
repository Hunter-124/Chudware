"""Exact planar neutral-line routing over an ordered set of circular pulleys.

For a directed tangent with left normal n, contact i is c_i - s_i*r_i*n,
where s=+1 is counterclockwise wrap and s=-1 is clockwise wrap. Orthogonality
therefore gives (c_j-c_i).n = s_j*r_j-s_i*r_i. The positive-length tangent
and directed circular arcs determine the route without a small-angle or
center-distance approximation. Opposite wrap signs model backside idlers.
"""
from __future__ import annotations

import math

from .registry import calc_envelope, calc_fail, register


_TAU = 2 * math.pi
_EPS = 64 * math.ulp(1.0)


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _point_distance(p, line):
    v = _sub(p, line['start'])
    along = min(line['length'], max(0.0, _dot(v, line['direction'])))
    return math.hypot(v[0] - along * line['direction'][0],
                      v[1] - along * line['direction'][1])


def _on_arc(point, arc):
    v = _sub(point, arc['center'])
    angle = math.atan2(v[1], v[0])
    delta = (arc['sign'] * (angle - arc['start_angle'])) % _TAU
    tolerance = _EPS / arc['radius']
    return delta <= arc['sweep'] + tolerance or _TAU - delta <= tolerance


def _intersects(a, b):
    if a['kind'] == 'arc' and b['kind'] == 'line':
        return _intersects(b, a)
    if a['kind'] == b['kind'] == 'line':
        offset = _sub(b['start'], a['start'])
        denominator = _cross(a['direction'], b['direction'])
        if abs(denominator) <= _EPS:
            return min(_point_distance(p, other)
                       for line, other in ((a, b), (b, a))
                       for p in (line['start'], line['end'])) <= _EPS
        ta = _cross(offset, b['direction']) / denominator
        tb = _cross(offset, a['direction']) / denominator
        return (-_EPS <= ta <= a['length'] + _EPS
                and -_EPS <= tb <= b['length'] + _EPS)
    if a['kind'] == 'line':
        offset = _sub(b['center'], a['start'])
        projection = _dot(offset, a['direction'])
        distance = abs(_cross(offset, a['direction']))
        if distance > b['radius'] + _EPS:
            return False
        half = math.sqrt(max(0.0, (b['radius'] - distance) * (b['radius'] + distance)))
        for along in (projection - half, projection + half):
            if -_EPS <= along <= a['length'] + _EPS:
                point = tuple(a['start'][k] + along * a['direction'][k] for k in (0, 1))
                if _on_arc(point, b):
                    return True
        return False
    delta = _sub(b['center'], a['center'])
    distance = math.hypot(*delta)
    if distance <= _EPS:
        return abs(a['radius'] - b['radius']) <= _EPS
    if (distance > a['radius'] + b['radius'] + _EPS
            or distance < abs(a['radius'] - b['radius']) - _EPS):
        return False
    along = (distance * distance + a['radius'] ** 2 - b['radius'] ** 2) / (2 * distance)
    half = math.sqrt(max(0.0, a['radius'] ** 2 - along * along))
    direction = tuple(v / distance for v in delta)
    for side in (-1, 1):
        point = (a['center'][0] + along * direction[0] - side * half * direction[1],
                 a['center'][1] + along * direction[1] + side * half * direction[0])
        if _on_arc(point, a) and _on_arc(point, b):
            return True
    return False


def belt_route(pulleys: list, prefilled: dict | None = None) -> dict:
    """Closed neutral-line route, visiting each explicitly ordered pulley once.

    Each row: center_mm=[x,y], pitch_radius_mm>0, wrap='ccw'|'cw'. Project
    measured coplanar CAD centers into one common oriented 2D plane. Radii
    must already describe the belt neutral/pitch line, including a sourced
    backside-idler offset; this calculator never substitutes outside radii.
    Crossed/twisted belts and routes cutting another pitch circle are refused.
    """
    if not isinstance(pulleys, list) or len(pulleys) < 2:
        return calc_fail('bad_query', 'pulleys must be an ordered list of at least two circles.')
    circles = []
    for i, row in enumerate(pulleys):
        if (not isinstance(row, dict)
                or set(row) != {'center_mm', 'pitch_radius_mm', 'wrap'}):
            return calc_fail('bad_query', f'pulley {i} requires exactly center_mm, pitch_radius_mm, wrap.')
        center, radius, wrap = row['center_mm'], row['pitch_radius_mm'], row['wrap']
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            return calc_fail('bad_query', f'pulley {i}: center_mm must contain two coordinates.')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (*center, radius)):
            return calc_fail('bad_query', f'pulley {i}: coordinates/radius must be finite numbers, not booleans.')
        if radius <= 0 or wrap not in ('ccw', 'cw'):
            return calc_fail('bad_query', f'pulley {i}: positive pitch_radius_mm and explicit ccw/cw wrap required.')
        circles.append({'center': tuple(float(v) for v in center), 'radius': float(radius),
                        'sign': 1 if wrap == 'ccw' else -1})
    # Work in a translated, normalized frame: world offsets and squared large
    # lengths must not pollute tangent/intersection roundoff.
    origin = circles[0]['center']
    offsets = [_sub(c['center'], origin) for c in circles]
    scale = max(*(math.hypot(*p) for p in offsets), *(c['radius'] for c in circles))
    if not math.isfinite(scale):
        return calc_fail('bad_query', 'The coordinate range is not representable in finite arithmetic.')
    for circle, offset in zip(circles, offsets):
        circle['center'] = tuple(v / scale for v in offset)
        circle['radius'] /= scale
        if circle['radius'] <= _EPS:
            return calc_fail('bad_query', 'The geometry scale cannot resolve the smallest pitch radius.')

    lines = []
    for i, a in enumerate(circles):
        j = (i + 1) % len(circles)
        b = circles[j]
        delta = _sub(b['center'], a['center'])
        distance = math.hypot(*delta)
        signed_difference = b['sign'] * b['radius'] - a['sign'] * a['radius']
        if distance <= abs(signed_difference) + _EPS:
            return calc_fail('no_tangent', f'No positive-length directed tangent from pulley {i} to {j}.')
        q = signed_difference / distance
        cosine = math.sqrt((1 - q) * (1 + q))
        e = tuple(v / distance for v in delta)
        normal = (q * e[0] - cosine * e[1], q * e[1] + cosine * e[0])
        start = tuple(a['center'][k] - a['sign'] * a['radius'] * normal[k] for k in (0, 1))
        end = tuple(b['center'][k] - b['sign'] * b['radius'] * normal[k] for k in (0, 1))
        lines.append({'kind': 'line', 'from': i, 'to': j, 'start': start, 'end': end,
                      'length': distance * cosine, 'direction': (normal[1], -normal[0])})

    arcs = []
    for i, circle in enumerate(circles):
        start, end = lines[i - 1]['end'], lines[i]['start']
        u, v = _sub(start, circle['center']), _sub(end, circle['center'])
        angle = math.atan2(u[1], u[0])
        sweep = (circle['sign'] * math.atan2(_cross(u, v), _dot(u, v))) % _TAU
        if min(sweep, _TAU - sweep) * circle['radius'] <= _EPS:
            sweep = 0.0
        arcs.append({'kind': 'arc', 'pulley': i, **circle, 'start': start, 'end': end,
                     'start_angle': angle, 'sweep': sweep, 'length': circle['radius'] * sweep})
    winding = math.fsum(a['sign'] * a['sweep'] for a in arcs)
    if abs(abs(winding) - _TAU) > len(circles) * _EPS * 16:
        return calc_fail('non_simple_route', 'Wrap/order creates a crossed or multiply wound planar route.')

    for line in lines:
        for j, circle in enumerate(circles):
            if j not in (line['from'], line['to']) and _point_distance(circle['center'], line) < circle['radius'] - _EPS:
                return calc_fail('route_obstructed', f'Span {line["from"]}->{line["to"]} cuts pitch circle {j}.')
    for arc in arcs:
        for j, circle in enumerate(circles):
            if j == arc['pulley']:
                continue
            delta = _sub(circle['center'], arc['center'])
            distance = math.hypot(*delta)
            nearest = min(math.dist(circle['center'], arc['start']), math.dist(circle['center'], arc['end']))
            if distance > _EPS:
                point = tuple(arc['center'][k] + arc['radius'] * delta[k] / distance for k in (0, 1))
                if _on_arc(point, arc):
                    nearest = min(nearest, abs(distance - arc['radius']))
            if nearest < circle['radius'] - _EPS:
                return calc_fail('route_obstructed', f'Wrap on pulley {arc["pulley"]} cuts pitch circle {j}.')
    pieces = []
    for i, line in enumerate(lines):
        pieces.append(line)
        arc = arcs[(i + 1) % len(arcs)]
        if arc['length'] > _EPS:
            pieces.append(arc)
    for i, a in enumerate(pieces):
        for j in range(i + 2, len(pieces)):
            if i == 0 and j == len(pieces) - 1:
                continue
            if _intersects(a, pieces[j]):
                return calc_fail('non_simple_route', 'Nonadjacent neutral-line spans/arcs intersect or touch.')

    def world(point):
        result = [origin[k] + scale * point[k] for k in (0, 1)]
        if not all(math.isfinite(v) for v in result):
            raise ValueError('A tangent endpoint exceeds the finite coordinate range.')
        return result

    straight = math.fsum(line['length'] for line in lines) * scale
    curved = math.fsum(arc['length'] for arc in arcs) * scale
    total = straight + curved
    if not math.isfinite(total):
        return calc_fail('bad_query', 'The total route length is not representable in finite arithmetic.')
    return calc_envelope(
        calc='belt_route', answer={
            'length_mm': total, 'straight_length_mm': straight, 'arc_length_mm': curved,
            'signed_wrap_rad': winding, 'numeric_tolerance_mm': _EPS * scale,
            'spans': [{'from_pulley': line['from'], 'to_pulley': line['to'],
                       'start_mm': world(line['start']), 'end_mm': world(line['end']),
                       'length_mm': line['length'] * scale} for line in lines],
            'wraps': [{'pulley': i, 'wrap': pulleys[i]['wrap'],
                       'entry_mm': world(arc['start']), 'exit_mm': world(arc['end']),
                       'angle_deg': math.degrees(arc['sweep']), 'length_mm': arc['length'] * scale}
                      for i, arc in enumerate(arcs)]},
        units='mm', equation='L = sum(sqrt(D_ij^2-(s_j*r_j-s_i*r_i)^2)) + sum(r_i*theta_i)',
        inputs={'pulleys': pulleys}, source_kind='closed_form',
        citation='Euclidean common-tangent geometry: radius perpendicular to tangent; directed circular arc length r*theta.',
        notes=['Ordered coplanar neutral/pitch circles; ccw/cw is belt travel around each circle, not shaft speed.',
               'Backside idlers use opposite wrap sense and caller-sourced neutral-line radii.',
               'Geometry only: no preload, compliance, slip, strength, physical belt/pulley clearance or supplier-tolerance qualification.',
               'Center-distance tolerances are not belt-length tolerances; evaluate their actual measurement geometry separately.'],
        prefilled=prefilled)


register('belt_route', belt_route, input_schema={
    'pulleys': {'type': 'array', 'minItems': 2, 'unit': 'mm', 'family': 'dimensionless',
                'description': 'Ordered closed route: [{center_mm:[x,y],pitch_radius_mm:positive number,wrap:"ccw"|"cw"}, ...]. Radii are neutral-line radii, not uncorrected outside radii.',
                'items': {'type': 'object', 'required': ['center_mm', 'pitch_radius_mm', 'wrap'],
                          'additionalProperties': False,
                          'properties': {'center_mm': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 2, 'maxItems': 2},
                                         'pitch_radius_mm': {'type': 'number', 'exclusiveMinimum': 0},
                                         'wrap': {'type': 'string', 'enum': ['ccw', 'cw']}}}}})
