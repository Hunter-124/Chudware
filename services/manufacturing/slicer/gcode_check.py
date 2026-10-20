"""Streaming text G-code analysis and deterministic preflight checks.

Raster checks use 0.2 mm cropped grids, sampled extrusion centerlines and actual
line widths, not mesh assumptions. Hard extrusion must remain supported; bridges
and overhang perimeters have a bounded unanchored span. Infill/interface spans
are informational, because these intentionally bridge sparse patterns below.
Support tower slenderness estimates risk, not adhesion; commanded waits/targets
are not measured temperature. Arc/non-mm extrusion is not called safe.
``by_type_mm`` and ``support_share`` use deposited XY path length, not mass.
Printed bounds omit custom startup priming; thermal/homing checks include it.
"""
from __future__ import annotations

import math
import re
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

_WORD = re.compile(r'([A-Za-z])\s*([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)')
_EXCLUDED = ('Skirt', 'Brim', 'Wipe tower', 'Custom')
_FLOATING_CLASSES = {
    'hard': ('Perimeter', 'External perimeter', 'Support material'),
    'anchored': ('Overhang perimeter', 'Bridge infill'),
    'gap_spanning': ('Support material interface', 'Solid infill', 'Top solid infill',
                     'Internal infill', 'Gap fill', 'Ironing'),
}
_RES = 0.2
# Deepest gap a support-base line may hang over and still be backed (organic branch splits).
_SUPPORT_DROP_MM = 1.5
_MAX_WIDTH_MM = 10.0
_MAX_KERNEL_SAMPLES = 4_096
_MAX_PATH_SAMPLES = 4_000_000


class _AnalysisRefused(ValueError):
    """Input cannot be safely verified within the analysis budget."""


def _valid_width(width):
    if not math.isfinite(width) or not 0 < width <= _MAX_WIDTH_MM:
        raise _AnalysisRefused(f'Extrusion WIDTH must be finite and in (0, {_MAX_WIDTH_MM:g}] mm.')
    return width


def _disk(radius_squared):
    if not math.isfinite(radius_squared) or radius_squared < 0:
        raise _AnalysisRefused('Invalid morphology kernel radius.')
    n = math.ceil(math.sqrt(radius_squared))
    if (2*n + 1)**2 > _MAX_KERNEL_SAMPLES:
        raise _AnalysisRefused(f'Morphology kernel exceeds {_MAX_KERNEL_SAMPLES} samples.')
    y, x = np.ogrid[-n:n+1, -n:n+1]
    return x*x + y*y <= radius_squared


def _refusal(error):
    return dict(ok=False, status='fail',
                checks=[_row('analysis_budget', 'fail', str(error),
                             limits={'max_width_mm': _MAX_WIDTH_MM,
                                     'max_kernel_samples': _MAX_KERNEL_SAMPLES,
                                     'max_path_samples': _MAX_PATH_SAMPLES})],
                stats={})


@dataclass
class GcodeLayer:
    z: float
    height: float
    # x0,y0,x1,y1,width,extruded filament,length,type index
    segments: np.ndarray
    # Thick bridge extrudates can reach a contact interface farther below than
    # the nominal plane pitch, without making the nominal layer itself thicker.
    max_extrusion_height: float = 0.0


@dataclass
class GcodeModel:
    layers: list[GcodeLayer] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    by_type_mm: dict[str, float] = field(default_factory=dict)
    config: dict[str, str] = field(default_factory=dict)
    slicer: str | None = None
    filament_mm: float = 0.0
    filament_g: float | None = None
    print_time_s: float | None = None
    temperatures: list[tuple[str, float, int]] = field(default_factory=list)
    printing_nozzle_c: set[float] = field(default_factory=set)
    printing_bed_c: set[float] = field(default_factory=set)
    retract_mm: list[float] = field(default_factory=list)
    homing_violations: int = 0
    cold_extrusions: int = 0
    e_mode_violations: int = 0
    absolute_extrusions: int = 0
    relative_extrusions: int = 0
    unknown_motion: int = 0
    fan_layer: int | None = None
    final_nozzle_c: float | None = None
    final_bed_c: float | None = None
    first_extrusion_z: float | None = None
    units_mm: bool = True
    # Filled only by parse_gcode(track_objects=True): one dict per printed object instance
    # {key, points (x,y float32 pairs of physical extrusion), z_max, first, last} where
    # first/last index ``moves``, the x0,y0,x1,y1,z_min float32 rows of EVERY motion.
    objects: list[dict] = field(default_factory=list)
    moves: array | None = None


def _tail_flag(path, key: str) -> bool:
    """True when the trailing slicer config block sets ``; key = 1`` (cheap pre-scan)."""
    with Path(path).open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - (1 << 20)))
        tail = stream.read().decode('utf-8', errors='replace')
    return re.search(r'^;\s*' + re.escape(key) + r'\s*=\s*1\s*$', tail, re.M) is not None


def parse_gcode(path, *, track_objects: bool = False) -> GcodeModel:
    """Read once, retaining compact per-layer extrusion arrays, not source lines.

    ``track_objects`` additionally attributes extrusion to the slicer's object labels
    (``; printing object <name> id:N copy M``; without labels, a layer Z that drops back
    starts a new object) and keeps every motion segment, for the sequential-print check."""
    model = GcodeModel()
    buffers: dict[float, array] = {}
    heights: dict[float, float] = {}
    extrusion_heights: dict[float, float] = {}
    extrusion_height = 0.0
    xyz = [0.0, 0.0, 0.0]
    absolute = True
    e_absolute = True
    e_declared = False
    e = 0.0
    debt = 0.0
    homed: set[str] = set()
    hot_wait = False
    nozzle_target = None
    bed_target = None
    role = 'Unknown'
    width = 0.45
    layer_z = None
    # Plane pitch is the ;HEIGHT: right after a layer's ;Z:. A ;HEIGHT: after a ;TYPE:
    # describes one extrudate (thick bridges are nozzle-diameter tall), not the plane.
    layer_height = 0.0
    after_z = False
    layer_number = 0
    type_ids = {}
    # Object attribution (track_objects only).
    object_label = None
    auto_object = 0
    object_ids: dict[str, int] = {}
    moves = array('f') if track_objects else None
    move_count = 0
    untracked_roles = tuple(_EXCLUDED)
    with Path(path).open('r', encoding='utf-8', errors='replace') as stream:
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(';'):
                if line.startswith(';TYPE:'):
                    role = line[6:].strip()
                    after_z = False
                elif line.startswith(';WIDTH:'):
                    try:
                        width = _valid_width(float(line[7:]))
                    except ValueError as exc:
                        raise _AnalysisRefused(f'Invalid extrusion WIDTH: {line[7:]!r}') from exc
                elif line.startswith(';LAYER_CHANGE'):
                    layer_number += 1
                elif line.startswith(';Z:'):
                    previous_layer_z = layer_z
                    try: layer_z = round(float(line[3:]), 5)
                    except ValueError: pass
                    if (track_objects and previous_layer_z is not None and layer_z is not None
                            and layer_z < previous_layer_z - 0.05):
                        auto_object += 1
                    after_z = True
                elif line.startswith('; printing object '):
                    object_label = line[len('; printing object '):].strip() or None
                elif line.startswith('; stop printing object'):
                    object_label = None
                elif line.startswith(';HEIGHT:'):
                    try: extrusion_height = float(line[8:])
                    except ValueError: pass
                    if after_z:
                        try: layer_height = float(line[8:])
                        except ValueError: pass
                    after_z = False
                elif 'generated by ' in line:
                    match = re.search(r'generated by (PrusaSlicer\s+[\w.+-]+)', line)
                    if match: model.slicer = match.group(1)
                elif '=' in line:
                    key, value = line[1:].strip().split('=', 1)
                    model.config[key.strip()] = value.strip()
                    if key.strip() == 'filament used [g]':
                        try: model.filament_g = sum(float(v) for v in value.split(','))
                        except ValueError: pass
                    if key.strip() == 'estimated printing time (normal mode)':
                        model.print_time_s = sum(float(n) * {'d':86400, 'h':3600, 'm':60, 's':1}[unit]
                                                 for n, unit in re.findall(r'(\d+(?:\.\d+)?)\s*([dhms])', value))
                continue
            command = line.split(';', 1)[0].strip().upper()
            match = re.match(r'^(?:N\d+\s*)?([GMT]\d+)\b', command)
            if not match:
                continue
            code = match.group(1)
            params = {k.upper(): float(v) for k, v in _WORD.findall(command[match.end():])}
            if code == 'G28':
                axes = {axis for axis in 'XYZ' if re.search(r'\b' + axis, command[match.end():])}
                homed.update(axes or set('XYZ'))
            elif code in ('G90', 'G91'):
                absolute = code == 'G90'
                e_absolute = absolute
            elif code in ('M82', 'M83'):
                e_absolute = code == 'M82'
                e_declared = True
            elif code == 'G20':
                model.units_mm = False
            elif code == 'G21':
                model.units_mm = True
            elif code == 'G92':
                if 'E' in params: e = params['E']
                for i, axis in enumerate('XYZ'):
                    if axis in params: xyz[i] = params[axis]
            elif code in ('M104', 'M109', 'M140', 'M190'):
                temperature = params.get('S', params.get('R'))
                if temperature is None: continue
                kind = 'nozzle' if code in ('M104', 'M109') else 'bed'
                model.temperatures.append((kind, temperature, layer_number))
                if kind == 'nozzle':
                    nozzle_target = temperature
                    if temperature < 150: hot_wait = False
                    if code == 'M109': hot_wait = temperature >= 150
                else:
                    bed_target = temperature
            elif code == 'M106':
                if params.get('S', 255) > 0 and model.fan_layer is None:
                    model.fan_layer = layer_number
            elif code in ('G0', 'G1', 'G2', 'G3'):
                target = xyz.copy()
                for i, axis in enumerate('XYZ'):
                    if axis in params:
                        target[i] = params[axis] if absolute else xyz[i] + params[axis]
                        if axis not in homed and abs(target[i] - xyz[i]) > 1e-8:
                            model.homing_violations += 1
                de = 0.0
                if 'E' in params:
                    de = params['E'] - e if e_absolute else params['E']
                    e = params['E'] if e_absolute else e + params['E']
                if de < -1e-7:
                    model.retract_mm.append(-de)
                    debt += -de
                deposited = max(0.0, de - debt)
                if de > 0: debt = max(0.0, debt - de)
                if deposited > 1e-7:
                    if not hot_wait: model.cold_extrusions += 1
                    if nozzle_target is not None: model.printing_nozzle_c.add(nozzle_target)
                    if bed_target is not None: model.printing_bed_c.add(bed_target)
                    if not model.units_mm: model.unknown_motion += 1
                    if not e_declared: model.e_mode_violations += 1
                    if role != 'Custom':
                        if e_absolute: model.absolute_extrusions += 1
                        else: model.relative_extrusions += 1
                    model.filament_mm += deposited
                    if model.first_extrusion_z is None and role != 'Custom':
                        model.first_extrusion_z = target[2]
                    length = math.hypot(target[0] - xyz[0], target[1] - xyz[1])
                    if code in ('G2', 'G3'):
                        model.unknown_motion += 1
                    if length > 1e-7:
                        type_id = type_ids.setdefault(role, len(type_ids))
                        model.by_type_mm[role] = model.by_type_mm.get(role, 0.0) + length
                        # Unannotated files are grouped by extrusion Z, ignoring Z-hop travels.
                        z = layer_z if layer_z is not None and role != 'Custom' else round(target[2], 5)
                        if role != 'Custom':
                            buffers.setdefault(z, array('f')).extend((*xyz[:2], *target[:2], width, deposited, length, type_id))
                            heights.setdefault(z, layer_height)
                            extrusion_heights[z] = max(extrusion_heights.get(z,0.0),extrusion_height)
                if track_objects and target != xyz:
                    moves.extend((xyz[0], xyz[1], target[0], target[1], min(xyz[2], target[2])))
                    move_count += 1
                    if deposited > 1e-7 and not role.startswith(untracked_roles):
                        key = object_label or f'object {auto_object + 1}'
                        index = object_ids.get(key)
                        if index is None:
                            index = object_ids[key] = len(model.objects)
                            model.objects.append({'key': key, 'points': array('f'), 'z_max': 0.0,
                                                  'first': move_count - 1, 'last': move_count - 1})
                        record = model.objects[index]
                        record['points'].extend((*xyz[:2], *target[:2]))
                        record['z_max'] = max(record['z_max'], target[2])
                        record['last'] = move_count - 1
                xyz = target
    model.types = list(type_ids)
    previous_z = 0.0
    for z, buffer in sorted(buffers.items()):
        model.layers.append(GcodeLayer(z, heights[z] or z - previous_z,
                                      np.frombuffer(buffer, dtype=np.float32).reshape(-1, 8),
                                      extrusion_heights.get(z,0.0)))
        previous_z = z
    model.final_nozzle_c = nozzle_target
    model.final_bed_c = bed_target
    model.moves = moves
    return model


def _row(name, status, note, metrics=None, limits=None, regions=None, fix=None):
    return dict(check=name, status=status, ok=False if status == 'fail' else True if status in ('pass', 'warn') else None,
                note=note, metrics=metrics or {}, limits=limits or {}, regions=regions or [], fix=fix or [])


def _samples(segments, origin, shape):
    """Vectorize all samples in a layer; avoid Python loops over extrusion moves."""
    if len(segments) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64), np.array([], dtype=np.int64)
    lengths = segments[:, 6].astype(np.float64)
    if (not np.isfinite(lengths).all() or np.any(lengths < 0)
            or np.any(lengths > _MAX_PATH_SAMPLES * (_RES * 0.75))):
        raise _AnalysisRefused('Extrusion path exceeds sampling budget.')
    counts = np.maximum(2, np.ceil(lengths / (_RES * 0.75)).astype(np.int64) + 1)
    if sum(int(count) for count in counts) > _MAX_PATH_SAMPLES:
        raise _AnalysisRefused(f'Extrusion path exceeds {_MAX_PATH_SAMPLES} samples.')
    starts = np.cumsum(counts) - counts
    owners = np.repeat(np.arange(len(segments)), counts)
    fraction = (np.arange(int(counts.sum())) - np.repeat(starts, counts)) / (counts[owners] - 1)
    x = segments[owners, 0] + (segments[owners, 2] - segments[owners, 0]) * fraction
    y = segments[owners, 1] + (segments[owners, 3] - segments[owners, 1]) * fraction
    ix = np.clip(np.rint((x - origin[0]) / _RES).astype(np.int64), 0, shape[1] - 1)
    iy = np.clip(np.rint((y - origin[1]) / _RES).astype(np.int64), 0, shape[0] - 1)
    return iy, ix, owners


def _raster(segments, origin, shape):
    raster = np.zeros(shape, dtype=bool)
    iy, ix, owners = _samples(segments, origin, shape)
    if not len(owners): return raster
    # Widths yielding the same disk on this grid share one dilation. Arachne
    # emits many nearly-identical WIDTH values; dilating each is needlessly slow.
    radii_squared = np.floor((segments[:, 4] / (2 * _RES) + 0.5)**2).astype(int)
    sample_radii = radii_squared[owners]
    for radius_squared in np.unique(radii_squared):
        selected = sample_radii == radius_squared
        center = np.zeros(shape, dtype=bool)
        center[iy[selected], ix[selected]] = True
        disk = _disk(int(radius_squared))
        raster |= ndimage.binary_dilation(center, structure=disk)
    return raster


def _inside(points, polygon):
    """Boundary-inclusive winding/ray test; no shapely dependency."""
    polygon = np.asarray(polygon, dtype=float)
    inside = np.zeros(len(points), dtype=bool)
    boundary = np.zeros(len(points), dtype=bool)
    x, y = points[:, 0], points[:, 1]
    for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
        cross = (x-a[0])*(b[1]-a[1]) - (y-a[1])*(b[0]-a[0])
        boundary |= (np.abs(cross) < 1e-5) & (x >= min(a[0],b[0])-1e-5) & (x <= max(a[0],b[0])+1e-5) & (y >= min(a[1],b[1])-1e-5) & (y <= max(a[1],b[1])+1e-5)
        if b[1] != a[1]:
            inside ^= ((a[1] > y) != (b[1] > y)) & (x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0])
    return inside | boundary


def _geometry(model):
    floating = []
    towers = []
    first_area = 0.0
    empty = np.empty((0, 8), dtype=np.float32)
    lower_planes = deque()
    last_support_z = 0.0
    nominal_height = float(model.config.get('layer_height', '0.2'))
    contact_gap = float(model.config.get('support_material_contact_distance', '0.2'))
    retention_mm = max(nominal_height, max((max(l.height,l.max_extrusion_height)
                                           for l in model.layers),default=nominal_height)) + contact_gap
    # Organic support branches split out of merged hollow outlines: the new branch wall
    # starts over the filled support base a few layers down, not over open air. A support
    # line counts as backed when deposited material lies within this drop beneath it.
    retention_mm = max(retention_mm, _SUPPORT_DROP_MM)
    support_base_id = model.types.index('Support material') if 'Support material' in model.types else -1
    excluded_ids = {i for i, t in enumerate(model.types) if any(t.startswith(x) for x in _EXCLUDED)}
    support_ids = {i for i, t in enumerate(model.types) if t.startswith('Support material')}
    tower_history = []
    class_for_type = {role: category for category, roles in _FLOATING_CLASSES.items() for role in roles}
    floating_metrics = {
        category: {
            'total_floating_area_mm2': 0.0, 'worst_layer_area_mm2': 0.0,
            'max_run_mm': 0.0, 'max_unanchored_run_mm': 0.0,
            'floating_layers': 0, 'worst_run': None,
            'by_type': {role: {'total_floating_area_mm2': 0.0, 'worst_layer_area_mm2': 0.0,
                              'max_run_mm': 0.0} for role in roles},
        } for category, roles in _FLOATING_CLASSES.items()
    }
    floating_metrics['anchored']['backed_bridge_spans'] = {
        'count':0, 'total_span_mm':0.0, 'max_span_mm':0.0, 'worst_span':None}
    for index, layer in enumerate(model.layers):
        segments = layer.segments
        ids = segments[:, 7].astype(int)
        physical = segments[~np.isin(ids, list(excluded_ids))]
        coverage = segments if index == 0 else physical
        support = physical[np.isin(physical[:, 7].astype(int), list(support_ids))]
        # Non-synchronized support/object/organic-branch planes interleave.
        # Retain ALL deposited strata within one pitch + the contact gap: the
        # most recent support-only plane must not erase an interface beneath it.
        lower_z = layer.z-max(nominal_height,layer.height,layer.max_extrusion_height)-contact_gap-0.001
        while lower_planes and lower_planes[0][0] < layer.z-retention_mm-0.001:
            lower_planes.popleft()
        lower = [data for z,data in lower_planes if z >= lower_z]
        previous = np.concatenate(lower) if lower else empty
        if not len(coverage):
            continue
        combined = np.concatenate((coverage[:, :4].reshape(-1, 2), previous[:, :4].reshape(-1, 2)))
        origin = np.floor((combined.min(axis=0) - 2.0) / _RES) * _RES
        size = np.ceil((combined.max(axis=0) + 2.0 - origin) / _RES).astype(int) + 1
        shape = (int(size[1]), int(size[0]))
        if math.prod(shape) > 16_000_000 or np.any(size > 12000):
            raise _AnalysisRefused('Extrusion raster exceeds analysis grid budget.')
        if index == 0:
            first_area = float(_raster(coverage, origin, shape).sum()) * _RES**2
        else:
            # Union of recent deposited strata, including contact interfaces.
            below = _raster(previous, origin, shape)
            distance = ndimage.distance_transform_edt(~below) * _RES if below.any() else np.full(shape, np.inf)
            # One vectorized sampling/run pass covers every role. Roles have
            # different safety semantics; a support interface bridging its sparse
            # base must not be treated as a unsupported outer wall.
            iy, ix, owners = _samples(physical, origin, shape)
            if len(owners):
                bad = distance[iy, ix] > physical[owners, 4] + _RES * 0.5
                drop = bad & (physical[owners, 7].astype(int) == support_base_id)
                if drop.any():
                    if layer.z <= _SUPPORT_DROP_MM + 1e-6:
                        bad[drop] = False  # the bed itself is within the drop
                    else:
                        deep = [data for z, data in lower_planes
                                if z >= layer.z - _SUPPORT_DROP_MM - 0.001]
                        deep_raster = (_raster(np.concatenate(deep), origin, shape) if deep
                                       else np.zeros(shape, dtype=bool))
                        deep_distance = (ndimage.distance_transform_edt(~deep_raster) * _RES
                                         if deep_raster.any() else np.full(shape, np.inf))
                        bad[drop] = (deep_distance[iy[drop], ix[drop]]
                                     > physical[owners[drop], 4] + _RES * 0.5)
                starts = np.r_[0, np.flatnonzero(np.diff(owners)) + 1]
                counts = np.diff(np.r_[starts, len(owners)])
                steps = physical[:, 6] / np.maximum(1, counts - 1)
                weights = steps[owners].astype(np.float64)
                weights[starts] = 0
                cumulative = np.cumsum(weights)
                breaks = np.zeros(len(owners), dtype=bool)
                disconnected = np.r_[True,
                    (np.linalg.norm(physical[1:, :2]-physical[:-1, 2:4], axis=1) > 0.05)
                    | (physical[1:, 7] != physical[:-1, 7])]
                breaks[starts[disconnected]] = True
                run_starts = np.flatnonzero(bad & (np.r_[True, ~bad[:-1]] | breaks))
                run_ends = np.flatnonzero(bad & (np.r_[~bad[1:], True] | np.r_[breaks[1:], False]))
                run_lengths = cumulative[run_ends]-cumulative[run_starts]
                sample_roles = physical[owners, 7].astype(int)
                run_roles = sample_roles[run_starts]
                before = np.maximum(0,run_starts-1)
                after = np.minimum(len(bad)-1,run_ends+1)
                left_anchor = (run_starts > 0) & ~bad[before] & ~breaks[run_starts]
                right_anchor = (run_ends < len(bad)-1) & ~bad[after] & ~breaks[after]
                anchor_counts = left_anchor.astype(int)+right_anchor.astype(int)
                bridge_id = model.types.index('Bridge infill') if 'Bridge infill' in model.types else -1
                # A bridge line, or a support-base line (organic branch walls spanning
                # between neighbouring walls), held at BOTH ends spans like a bridge.
                backed_bridge = (((run_roles == bridge_id) | (run_roles == support_base_id))
                                 & left_anchor & right_anchor)
                sample_radii = np.floor((physical[owners, 4] / (2*_RES)+0.5)**2).astype(int)
                class_masks = {}
                for type_id in np.unique(sample_roles[bad]):
                    role = model.types[int(type_id)]
                    category = class_for_type.get(role, 'hard')
                    metrics = floating_metrics[category]
                    type_metrics = metrics['by_type'].setdefault(role, {
                        'total_floating_area_mm2': 0.0, 'worst_layer_area_mm2': 0.0, 'max_run_mm': 0.0})
                    selected = bad & (sample_roles == type_id)
                    mask = np.zeros(shape, dtype=bool)
                    for radius_squared in np.unique(sample_radii[selected]):
                        centers = np.zeros(shape, dtype=bool)
                        points = selected & (sample_radii == radius_squared)
                        centers[iy[points], ix[points]] = True
                        mask |= ndimage.binary_dilation(centers, structure=_disk(int(radius_squared)))
                    area = float(mask.sum()) * _RES**2
                    if category not in class_masks:
                        class_masks[category] = mask
                    else:
                        class_masks[category] |= mask
                    type_metrics['total_floating_area_mm2'] += area
                    type_metrics['worst_layer_area_mm2'] = max(type_metrics['worst_layer_area_mm2'], area)
                    matching_runs = np.flatnonzero(run_roles == type_id)
                    if not len(matching_runs):
                        continue
                    unbacked = matching_runs[~backed_bridge[matching_runs]]
                    backed = matching_runs[backed_bridge[matching_runs]]
                    worst_all = float(np.max(run_lengths[matching_runs]))
                    type_metrics['max_run_mm'] = max(type_metrics['max_run_mm'],worst_all)
                    worst_unanchored = float(np.max(run_lengths[unbacked])) if len(unbacked) else 0.0
                    type_metrics['max_unanchored_run_mm'] = max(
                        type_metrics.get('max_unanchored_run_mm',0.0),worst_unanchored)
                    metrics['max_unanchored_run_mm'] = max(metrics['max_unanchored_run_mm'],worst_unanchored)
                    if len(backed):
                        spans = floating_metrics['anchored']['backed_bridge_spans']
                        spans['count'] += len(backed)
                        spans['total_span_mm'] += float(np.sum(run_lengths[backed]))
                    for candidates in (unbacked,backed):
                        if not len(candidates):
                            continue
                        longest = int(candidates[np.argmax(run_lengths[candidates])])
                        worst = float(run_lengths[longest])
                        start, end = int(run_starts[longest]), int(run_ends[longest])+1
                        region = dict(layer=index+1, type=role, **{'class':category},
                                      z_mm=round(layer.z,3), max_run_mm=round(worst,3),
                                      area_mm2=round(area,1), anchor_count=int(anchor_counts[longest]),
                                      backed_bridge=bool(backed_bridge[longest]),
                                      centroid=[round(float(origin[0]+ix[start:end].mean()*_RES),3),
                                                round(float(origin[1]+iy[start:end].mean()*_RES),3),
                                                round(layer.z,3)])
                        if metrics['worst_run'] is None or worst > metrics['max_run_mm']:
                            metrics['worst_run'] = region
                        metrics['max_run_mm'] = max(metrics['max_run_mm'],worst)
                        if backed_bridge[longest]:
                            if spans['worst_span'] is None or worst > spans['max_span_mm']:
                                spans['worst_span'] = region
                            spans['max_span_mm'] = max(spans['max_span_mm'],worst)
                        if category != 'gap_spanning':
                            floating.append(region)
                for category, mask in class_masks.items():
                    area = float(mask.sum()) * _RES**2
                    metrics = floating_metrics[category]
                    metrics['total_floating_area_mm2'] += area
                    metrics['worst_layer_area_mm2'] = max(metrics['worst_layer_area_mm2'], area)
                    metrics['floating_layers'] += 1
        next_history = []
        if len(support):
            mask = _raster(support, origin, shape)
            # Connect nearby sparse support traces into actual support islands.
            mask = ndimage.binary_dilation(mask, iterations=8)
            labels, count = ndimage.label(mask)
            slices = ndimage.find_objects(labels)
            for number, box in enumerate(slices, 1):
                if box is None: continue
                ys, xs = box
                x0, x1 = float(origin[0]+xs.start*_RES), float(origin[0]+xs.stop*_RES)
                y0, y1 = float(origin[1]+ys.start*_RES), float(origin[1]+ys.stop*_RES)
                width = max(_RES, min(x1-x0, y1-y0)-3.2)
                start_z = layer.z
                for old in tower_history:
                    if min(x1,old[1])-max(x0,old[0]) > 0 and min(y1,old[3])-max(y0,old[2]) > 0:
                        start_z = min(start_z, old[4])
                height = layer.z - start_z + layer.height
                ratio = height / width
                next_history.append((x0,x1,y0,y1,start_z))
                if height >= 20 and ratio > 8:
                    towers.append(dict(z_mm=round(layer.z,3), centroid=[round((x0+x1)/2,3),round((y0+y1)/2,3),round(layer.z,3)],
                                       height_mm=round(height,3), width_mm=round(width,3), slenderness=round(ratio,2)))
        if len(support):
            tower_history = next_history
        elif tower_history and layer.z-last_support_z > nominal_height+contact_gap+0.001:
            tower_history = []
        if len(physical): lower_planes.append((layer.z,physical))
        if len(support): last_support_z = layer.z
    # Keep distinct worst islands, not fifty repeated layers of one tower.
    unique_towers = []
    for row in sorted(towers, key=lambda r: -r['slenderness']):
        if not any(np.linalg.norm(np.array(row['centroid'][:2])-old['centroid'][:2]) < 3 for old in unique_towers):
            unique_towers.append(row)
    return floating, unique_towers, first_area, floating_metrics


# Extrusion centerlines sit half a line inside the printed edge.
_HALF_LINE_MM = 0.25
# A nozzle this far inside the clearance radius is a collision; closer to it, a warning.
_SEQUENTIAL_TOLERANCE_MM = 1.0


def _config_number(value):
    try:
        number = float(str(value).split(',')[0].strip().rstrip('%'))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _segment_polygon_distance(segments, polygon):
    """Distance from each segment (N x [x0,y0,x1,y1]) to a convex CCW polygon; 0 inside/through."""
    p0, p1 = segments[:, None, :2], segments[:, None, 2:4]
    a = polygon[None, :, :]
    b = np.roll(polygon, -1, axis=0)[None, :, :]
    edge = b - a

    def cross(u, v):
        return u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]

    def point_segment(p, s, e):
        v = e - s
        length = (v * v).sum(-1)
        t = np.clip(((p - s) * v).sum(-1) / np.where(length > 0, length, 1.0), 0.0, 1.0)
        return np.linalg.norm(p - s - t[..., None] * v, axis=-1)

    inside = ((cross(edge, p0 - a) >= -1e-9).all(axis=1)
              | (cross(edge, p1 - a) >= -1e-9).all(axis=1))
    direction = p1 - p0
    crosses = ((cross(direction, a - p0) * cross(direction, b - p0) < 0)
               & (cross(edge, p0 - a) * cross(edge, p1 - a) < 0)).any(axis=1)
    distance = np.minimum.reduce([point_segment(p0, a, b), point_segment(p1, a, b),
                                  point_segment(a, p0, p1)]).min(axis=1)
    return np.where(inside | crosses, 0.0, distance)


def _sequential_clearance(model, detail=False):
    """Complete-individual-objects safety from the toolpath itself.

    After an object's last extrusion it stands at its printed height. Every later motion
    below that height must keep the nozzle at least extruder_clearance_radius from the
    object's footprint (hotend body), and must not run more than extruder_clearance_height
    below its top (the gantry spans the bed). Objects must also print one after another."""
    radius = _config_number(model.config.get('extruder_clearance_radius'))
    gantry = _config_number(model.config.get('extruder_clearance_height'))
    objects = sorted(model.objects, key=lambda o: o['first'])
    limits = {'extruder_clearance_radius_mm': radius, 'extruder_clearance_height_mm': gantry,
              'collision_inside_radius_by_mm': _SEQUENTIAL_TOLERANCE_MM}
    if not objects or model.moves is None or radius is None or gantry is None:
        return _row('sequential_clearance', 'not_evaluated',
                    'Complete individual objects is on, but the file names no printed objects or '
                    'no extruder clearance radius/height; extruder and gantry collisions were not checked.',
                    limits=limits, fix=['Slice with slice_part so the printer clearances and object labels are present.'])
    from .plate import footprint_hull  # noqa: PLC0415

    moves = np.frombuffer(model.moves, dtype=np.float32).reshape(-1, 5).astype(np.float64)
    interleaved = []
    latest_end, latest_key = -1, None
    for obj in objects:
        if obj['first'] < latest_end:
            interleaved.append([latest_key, obj['key']])
        if obj['last'] > latest_end:
            latest_end, latest_key = obj['last'], obj['key']
    summary, regions = [], []
    extruder_hits = gantry_hits = 0
    min_clearance = math.inf
    for order, obj in enumerate(objects, 1):
        points = np.frombuffer(obj['points'], dtype=np.float32).reshape(-1, 2).astype(np.float64)
        hull = np.asarray(footprint_hull(points), dtype=np.float64)
        top = obj['z_max']
        low_x, low_y = (float(v) for v in hull.min(axis=0))
        high_x, high_y = (float(v) for v in hull.max(axis=0))
        summary.append({'object': obj['key'], 'print_order': order, 'height_mm': round(top, 3),
                        'bbox_mm': [round(low_x, 3), round(low_y, 3), round(high_x, 3), round(high_y, 3)]})
        later = np.flatnonzero(moves[obj['last'] + 1:, 4] < top - 0.05) + obj['last'] + 1
        if not len(later):
            continue
        segments = moves[later]
        under_gantry = segments[:, 4] + gantry < top - 0.05
        if under_gantry.any():
            gantry_hits += int(under_gantry.sum())
            first = int(np.flatnonzero(under_gantry)[0])
            regions.append({'kind': 'gantry', 'finished_object': obj['key'],
                            'object_height_mm': round(top, 3),
                            'z_mm': round(float(segments[first, 4]), 3),
                            'xy': [round(float(segments[first, 2]), 3), round(float(segments[first, 3]), 3)],
                            'moves': int(under_gantry.sum())})
        near = ((np.maximum(segments[:, 0], segments[:, 2]) >= low_x - radius)
                & (np.minimum(segments[:, 0], segments[:, 2]) <= high_x + radius)
                & (np.maximum(segments[:, 1], segments[:, 3]) >= low_y - radius)
                & (np.minimum(segments[:, 1], segments[:, 3]) <= high_y + radius))
        candidates = segments[near]
        if not len(candidates):
            continue
        clearance = np.concatenate([
            _segment_polygon_distance(candidates[start:start + 20000, :4], hull)
            for start in range(0, len(candidates), 20000)]) - _HALF_LINE_MM
        min_clearance = min(min_clearance, float(clearance.min()))
        hits = clearance < radius - _SEQUENTIAL_TOLERANCE_MM
        if hits.any():
            extruder_hits += int(hits.sum())
            worst = int(np.argmin(clearance))
            regions.append({'kind': 'extruder', 'finished_object': obj['key'],
                            'object_height_mm': round(top, 3),
                            'z_mm': round(float(candidates[worst, 4]), 3),
                            'xy': [round(float(candidates[worst, 2]), 3), round(float(candidates[worst, 3]), 3)],
                            'clearance_mm': round(float(clearance[worst]), 3),
                            'moves': int(hits.sum())})
    failed = bool(interleaved or extruder_hits or gantry_hits)
    close = math.isfinite(min_clearance) and min_clearance < radius
    status = 'fail' if failed else 'warn' if close else 'pass'
    shown = 'none within the radius' if not math.isfinite(min_clearance) else f'{min_clearance:.1f} mm'
    note = (f'{len(objects)} objects printed one at a time; closest nozzle approach to a finished, '
            f'taller object {shown} (clearance radius {radius:g} mm); {extruder_hits} moves inside the '
            f'hotend clearance, {gantry_hits} moves with the gantry ({gantry:g} mm) below a finished '
            f'object top; {len(interleaved)} interleaved objects')
    fix = []
    if failed:
        fix = ['Space objects at least the clearance radius apart (plus brims) and print them in '
               'ascending height, with at most one object taller than the clearance height, '
               'printed last; slice_part(parts=[...], complete_individual_objects=True) lays the '
               'plate out that way.',
               'Or print the plate layer by layer (complete_individual_objects=False).']
    return _row('sequential_clearance', status, note,
                {'objects': summary if detail or len(summary) <= 24 else summary[:24],
                 'object_count': len(objects),
                 'min_clearance_mm': round(min_clearance, 3) if math.isfinite(min_clearance) else None,
                 'extruder_collision_moves': extruder_hits, 'gantry_collision_moves': gantry_hits,
                 'interleaved': interleaved},
                limits, regions[:50 if detail else 5], fix)


def verify_gcode(path, *, bed_polygon=None, max_height_mm=None, material=None, nozzle_mm=None,
                 expect_nozzle_c=None, expect_bed_c=None, detail=False, max_unanchored_mm: float=10.0,
                 max_hard_run_mm: float=2.0, max_hard_area_mm2: float=2.0, anchored_warn_fraction: float=0.5,
                 anchored_bridge_warn_mm: float=25.0) -> dict:
    """Check commanded safety and role-aware support; do not certify a printer.

    Hard floats fail by area/run; overhangs and missing-end bridges fail by
    unanchored run. Two-ended bridges only warn above the sag-span limit.
    Intentional infill/interface spans are reported but cannot fail this row.
    """
    if (not all(math.isfinite(v) and v > 0 for v in
                (max_unanchored_mm, max_hard_run_mm, max_hard_area_mm2, anchored_bridge_warn_mm))
            or not 0 < anchored_warn_fraction <= 1):
        raise ValueError('Floating-extrusion limits must be positive; warning fraction must be in (0,1].')
    # Complete individual objects (``complete_objects = 1``) needs per-object attribution;
    # the flag sits in the trailing config block, so peek at it before the single pass.
    try:
        model = parse_gcode(path, track_objects=_tail_flag(path, 'complete_objects'))
        if model.config.get('complete_objects') == '1' and model.moves is None:
            model = parse_gcode(path, track_objects=True)
    except _AnalysisRefused as exc:
        return _refusal(exc)
    checks = []
    all_segments = [layer.segments for layer in model.layers]
    height = max((layer.z for layer in model.layers), default=0.0)
    if bed_polygon is None:
        raw = model.config.get('bed_shape')
        if raw:
            try: bed_polygon = [tuple(float(v) for v in p.split('x')) for p in raw.split(',')]
            except ValueError: pass
    if nozzle_mm is None:
        try: nozzle_mm = float(model.config.get('nozzle_diameter','').split(',')[0])
        except ValueError: pass
    if max_height_mm is None:
        try: max_height_mm = float(model.config.get('max_print_height',''))
        except ValueError: pass
    material = (material or model.config.get('filament_type', '').strip('"')).upper()
    points = np.concatenate([s[:, :4].reshape(-1,2) for s in all_segments]) if all_segments else np.empty((0,2))
    violations = int((~_inside(points, bed_polygon)).sum()) if bed_polygon is not None and len(points) else 0
    checks.append(_row('bounds', 'fail' if violations else 'pass' if bed_polygon is not None else 'not_evaluated',
                       f'{violations} extrusion endpoints outside bed polygon', {'outside_endpoints':violations},
                       {'bed_polygon':bed_polygon}, fix=['Reposition or split the part within the vendor bed polygon.'] if violations else []))
    too_high = max_height_mm is not None and height > max_height_mm + 0.001
    checks.append(_row('max_height', 'fail' if too_high else 'pass' if max_height_mm is not None else 'not_evaluated',
                       f'Printed height {height:.3f} mm; limit {max_height_mm} mm', {'height_mm':round(height,3)}, {'max_height_mm':max_height_mm},
                       fix=['Choose a shorter orientation or split the part.'] if too_high else []))
    checks.append(_row('homing_before_motion','fail' if model.homing_violations else 'pass',
                       f'{model.homing_violations} axis moves before homing', {'unhomed_moves':model.homing_violations}, fix=['Use the vendor start G-code with G28 before motion.'] if model.homing_violations else []))
    checks.append(_row('heat_before_extrude','fail' if model.cold_extrusions else 'pass',
                       f'{model.cold_extrusions} extrusion commands without a completed nozzle heat-wait command', {'cold_extrusions':model.cold_extrusions}, {'minimum_wait_target_c':150},
                       fix=['Use M109 to wait for printing temperature before extrusion.'] if model.cold_extrusions else []))
    windows = {'PLA':(180,235,40,70), 'PETG':(220,265,60,95), 'ABS':(230,270,80,115), 'ASA':(230,275,80,115), 'TPU':(200,250,25,70)}
    window = windows.get(material)
    errors = []
    # Check the targets active during deposition; 150 C standby before homing
    # must not fail, while an M109 to 170 C actually used for PLA must fail.
    active = {'nozzle':sorted(model.printing_nozzle_c), 'bed':sorted(model.printing_bed_c)}
    for kind, values in active.items():
        for value in values:
            lo, hi = (window[:2] if kind == 'nozzle' else window[2:]) if window else (-math.inf, math.inf)
            if not lo <= value <= hi: errors.append(f'{kind} {value:g} C outside {lo:g}..{hi:g} C')
    for kind, expected in [('nozzle', expect_nozzle_c), ('bed', expect_bed_c)]:
        if expected is not None and (not active[kind] or any(abs(v-expected)>1 for v in active[kind])):
            errors.append(f'{kind} commands differ from expected {expected:g} C')
    checks.append(_row('temperatures','fail' if errors else 'pass' if window or expect_nozzle_c is not None or expect_bed_c is not None else 'not_evaluated',
                       '; '.join(errors) or f'{material or "unknown material"}: nozzle {sorted(set(active["nozzle"]))}, bed {sorted(set(active["bed"]))} C',
                       {'nozzle_c':sorted(set(active['nozzle'])), 'bed_c':sorted(set(active['bed']))},
                       {'material_window_c':window,'expect_nozzle_c':expect_nozzle_c,'expect_bed_c':expect_bed_c}, fix=['Select a stock filament preset and confirm spool temperature limits.'] if errors else []))
    try:
        floating, towers, first_area, floating_metrics = _geometry(model)
    except _AnalysisRefused as exc:
        return _refusal(exc)
    first_z = model.layers[0].z if model.layers else None
    first_bad = first_z is None or first_z <= 0 or (nozzle_mm is not None and first_z > nozzle_mm*0.8+0.001) or first_area < 2
    checks.append(_row('first_layer','fail' if first_bad else 'warn' if first_area < 20 else 'pass',
                       f'First layer Z {first_z} mm, deposited coverage {first_area:.1f} mm2 (includes brim)',
                       {'z_mm':first_z,'coverage_mm2':round(first_area,1)}, {'minimum_coverage_mm2':2,'warn_coverage_mm2':20,'maximum_z_mm':None if nozzle_mm is None else nozzle_mm*0.8},
                       fix=['Ensure the part is on the bed; add brim or choose a larger contact face.'] if first_bad or first_area<20 else []))
    layer_max = max((layer.height for layer in model.layers),default=0)
    bad_heights = sum(layer.height <= 0 or layer.height > nozzle_mm*0.8+0.001 for layer in model.layers) if nozzle_mm is not None else 0
    checks.append(_row('layer_heights','fail' if bad_heights else 'pass' if nozzle_mm else 'not_evaluated',
                       f'{bad_heights} layers exceed 0.8 nozzle or have nonpositive height; maximum {layer_max:.3f} mm',
                       {'invalid_layers':bad_heights,'max_layer_height_mm':round(layer_max,3)}, {'max_layer_height_mm':nozzle_mm*0.8 if nozzle_mm else None},
                       fix=['Use a compatible layer-height print preset.'] if bad_heights else []))
    expected_relative = model.config.get('use_relative_e_distances')
    mode_mismatch = (model.absolute_extrusions if expected_relative == '1' else
                     model.relative_extrusions if expected_relative == '0' else 0)
    mode_bad = model.e_mode_violations + mode_mismatch
    # Missing explicit E mode is unsafe across firmware defaults; G92 resets themselves are legal.
    checks.append(_row('e_mode_consistency','fail' if mode_bad else 'pass',
                       f'{model.e_mode_violations} undeclared-mode extrusions; {mode_mismatch} commands disagree with configured E mode',
                       {'undeclared_mode_extrusions':model.e_mode_violations,'config_mismatches':mode_mismatch,'relative_e_config':expected_relative},
                       fix=['Declare the configured M82/M83 mode after G90/G91 and preserve G92 resets.'] if mode_bad else []))
    retract = max(model.retract_mm, default=0.0)
    configured = model.config.get('retract_length')
    direct = bool(re.search(r'direct', model.config.get('printer_notes',''), re.I))
    limit = 3.0 if direct else 8.0
    retract_bad = retract > limit
    checks.append(_row('retraction','warn' if retract_bad else 'pass' if model.retract_mm else 'not_applicable',
                       f'Maximum retract {retract:.3f} mm; {"direct" if direct else "Bowden/unknown"} warning limit {limit:g} mm',
                       {'max_retract_mm':round(retract,3),'count':len(model.retract_mm),'configured_mm':configured}, {'warning_above_mm':limit},
                       fix=['Use the printer vendor retraction preset; check extruder type.'] if retract_bad else []))
    fan_bad = material == 'PLA' and len(model.layers) > 3 and (model.fan_layer is None or model.fan_layer > 3)
    checks.append(_row('fan','warn' if fan_bad else 'pass' if material == 'PLA' else 'not_applicable',
                       f'{material or "unknown"} fan first enabled on layer {model.fan_layer}; expected by layer 3', {'first_fan_layer':model.fan_layer}, {'latest_pla_fan_layer':3},
                       fix=['Enable PLA cooling by layer 3 in the filament preset.'] if fan_bad else []))
    off = model.final_nozzle_c == 0 and model.final_bed_c == 0
    checks.append(_row('end_state','pass' if off else 'fail', f'Final nozzle target {model.final_nozzle_c} C; bed target {model.final_bed_c} C',
                       {'nozzle_c':model.final_nozzle_c,'bed_c':model.final_bed_c}, {'final_target_c':0}, fix=['Use vendor end G-code that switches both heaters off.'] if not off else []))
    hard = floating_metrics['hard']
    anchored = floating_metrics['anchored']
    # A floating RUN is the failure signature (an outer wall or support line laid over air).
    # A layer that only sums to a larger floating area out of short fragments (organic
    # branch tips, 45° slopes at a coarse raster) is a review warning, not spaghetti.
    floating_bad = (hard['max_unanchored_run_mm'] > max_hard_run_mm
                    or anchored['max_unanchored_run_mm'] > max_unanchored_mm)
    backed_spans = anchored['backed_bridge_spans']
    long_bridge = backed_spans['max_span_mm'] > anchored_bridge_warn_mm
    floating_warn = (anchored['max_unanchored_run_mm'] > anchored_warn_fraction * max_unanchored_mm
                     or hard['worst_layer_area_mm2'] > max_hard_area_mm2
                     or long_bridge)
    floating_status = ('fail' if floating_bad else 'not_evaluated' if model.unknown_motion
                       else 'warn' if floating_warn else 'pass')
    for category in floating_metrics.values():
        for key in ('total_floating_area_mm2', 'worst_layer_area_mm2'):
            category[key] = round(category[key],1)
        category['max_run_mm'] = round(category['max_run_mm'],3)
        category['max_unanchored_run_mm'] = round(category['max_unanchored_run_mm'],3)
        for values in category['by_type'].values():
            for key in ('total_floating_area_mm2', 'worst_layer_area_mm2'):
                values[key] = round(values[key],1)
            values['max_run_mm'] = round(values['max_run_mm'],3)
            if 'max_unanchored_run_mm' in values:
                values['max_unanchored_run_mm'] = round(values['max_unanchored_run_mm'],3)
    backed_spans['total_span_mm'] = round(backed_spans['total_span_mm'],3)
    backed_spans['max_span_mm'] = round(backed_spans['max_span_mm'],3)
    safety_regions = [r for r in floating if not r['backed_bridge'] or r['max_run_mm'] > anchored_bridge_warn_mm]
    gap = floating_metrics['gap_spanning']
    checks.append(_row('floating_extrusion',floating_status,
                       f'Hard floating: {hard["worst_layer_area_mm2"]:.1f} mm2/layer, '
                       f'{hard["max_unanchored_run_mm"]:.3f} mm unanchored run; '
                       f'unanchored air run {anchored["max_unanchored_run_mm"]:.3f} mm (limit {max_unanchored_mm:g}); '
                       f'backed bridge {backed_spans["max_span_mm"]:.3f} mm'
                       f'{" (long bridge, expect sag)" if long_bridge else ""}; '
                       f'infill/interface spans informational ({gap["total_floating_area_mm2"]:.1f} mm2 total)',
                       {'classes':floating_metrics,
                        'worst_layer_area_mm2':hard['worst_layer_area_mm2'],
                        'total_floating_area_mm2':round(sum(c['total_floating_area_mm2'] for c in floating_metrics.values()),1),
                        'max_run_mm':max(hard['max_unanchored_run_mm'],anchored['max_unanchored_run_mm']),
                        'unsupported_motion_commands':model.unknown_motion},
                       {'warn_hard_layer_area_mm2':max_hard_area_mm2,'fail_hard_run_mm':max_hard_run_mm,
                        'max_unanchored_mm':max_unanchored_mm,
                        'warn_anchored_run_mm':anchored_warn_fraction*max_unanchored_mm,
                        'anchored_bridge_warn_mm':anchored_bridge_warn_mm,
                        'raster_resolution_mm':_RES,'support_distance_line_widths':1},
                       sorted(safety_regions, key=lambda r:(-r['max_run_mm'],-r['area_mm2'],r['layer'],r['type']))[:50 if detail else 5],
                       ['Reorient or support long unanchored outer-wall and bridge paths.',
                        'Review the flagged type, layer and XY positions in PrusaSlicer before printing.']
                       if floating_bad or floating_warn else []))
    checks.append(_row('support_towers','warn' if towers else 'pass' if any(t.startswith('Support') for t in model.by_type_mm) else 'not_applicable',
                       f'{len(towers)} distinct tall/slender support islands (height >=20 mm, height/width >8)', {'risky_islands':len(towers)},
                       {'minimum_height_mm':20,'warning_slenderness':8}, towers[:50 if detail else 5],
                       ['Use a flatter orientation or grid/organic supports with wider anchored bases.','Add a support sheath/brim and reduce unsupported tower height.'] if towers else []))
    sequential = model.config.get('complete_objects') == '1'
    checks.append(_sequential_clearance(model, detail) if sequential else
                  _row('sequential_clearance', 'not_applicable',
                       'Objects print together layer by layer (Complete individual objects off).'))
    status = 'fail' if any(r['status']=='fail' for r in checks) else 'warn' if any(r['status'] in ('warn','not_evaluated') for r in checks) else 'pass'
    total_path = sum(model.by_type_mm.values())
    support_path = sum(v for t,v in model.by_type_mm.items() if t.startswith('Support'))
    return dict(ok=status != 'fail', status=status, checks=checks,
                stats=dict(layers=len(model.layers),height_mm=round(height,3),filament_mm=round(model.filament_mm,3),filament_g=model.filament_g,
                           print_time_s=model.print_time_s,support_share=round(support_path/total_path,4) if total_path else 0,
                           by_type_mm={k:round(v,3) for k,v in sorted(model.by_type_mm.items())},slicer=model.slicer))
