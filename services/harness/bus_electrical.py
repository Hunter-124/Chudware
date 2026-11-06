"""Screen bus electrical constraints from caller-supplied device and cable data.

``harness_to_analysis_links(harness, wire_ids, cable)`` converts series sections
of one signal path to screening links. ``harness_to_bus_links`` converts explicit
complete firmware templates to firmware bus links. Pairing and electronics are
never inferred. Wire length is the routed centreline, not cut length.

``analyze_bus(protocol, params, links)`` returns checks with pass/fail/unknown
and an aggregate status. Inputs are SI units except where named. ``links`` are
series sections, not parallel branches or multiple independent signal wires.
For I2C specify pullup_ohm, supply_v, sink_current_ma, vol_v,
max_rise_time_ns, device_capacitance_pf (total on the analyzed line, including
PCB), and mode (``standard`` or ``fast``), or an explicit max_capacitance_pf. For CAN,
CAN FD and RS-485 specify termination_ohms (the two physical end resistors),
termination_tolerance_fraction, stub_lengths_m (empty only if known absent),
and max_stub_delay_ns (one-way limit from the applicable device/design). For
nonzero stubs also provide stub_delay_ns_per_m from their cable datasheet.
For CAN, CAN FD and RS-485 timing supply bitrate_hz, sample_point (fraction
of a bit), and loop_delay_ns (the complete non-cable round-trip delay).
RS-485 also needs bias_pullup_ohm, bias_pulldown_ohm, bias_load_ohm (total
receiver/leakage differential load), supply_v and min_bias_v. UART
and SPI require max_delay_ns, the allowable one-way path delay derived by the
caller from the actual timing budget; SPI round-trip budgets must be converted
by the caller. The caller must include all sections, loads and relevant margins.

These are screening calculations, not protocol or EMC certification. No bus
speed, transceiver threshold, termination tolerance or geometry is assumed.
"""
from __future__ import annotations

import math

from services.harness import cable_models
from services.harness.wire_data import NotTabulated


def _number(value, *, zero=False):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and (n >= 0 if zero else n > 0) else None


def harness_to_analysis_links(harness: dict, wire_ids: list[str], cable: dict | None = None) -> list[dict]:
    """Convert selected routed wires to series links; missing IDs raise ValueError.

    Supply explicit cable geometry or datasheet values as accepted by
    :func:`cable_models.evaluate_cable`. No conductor spacing is inferred from
    insulation OD and no conductor diameter is inferred from gauge.
    """
    wires = {w['id']: w for w in harness.get('wires', [])}
    if len(set(wire_ids)) != len(wire_ids):
        raise ValueError('wire_ids must not contain duplicates')
    links = []
    for wid in wire_ids:
        if wid not in wires:
            raise ValueError(f'unknown wire {wid!r}')
        w = wires[wid]
        length = _number(w.get('length_mm'))
        model = None
        error = None
        try:
            model = cable_models.evaluate_cable(cable, wire={'kind': w.get('kind')})
        except (NotTabulated, ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            error = str(exc)
        links.append({'wire_id': wid, 'from': w.get('from'), 'to': w.get('to'),
                      'length_m': None if length is None else length / 1000,
                      'cable_model': model, 'model_error': error})
    return links


def _bound(link, key, side):
    model = link.get('cable_model')
    band = model.get(key) if isinstance(model, dict) else None
    if not isinstance(band, dict):
        return None
    return _number(band.get('value') if band.get('value') is not None else band.get(side))


def _sum_links(links, key, side):
    if not links:
        return None
    total = 0.0
    for link in links:
        if not isinstance(link, dict):
            return None
        length = _number(link.get('length_m'))
        rate = _bound(link, key, side)
        if length is None or rate is None:
            return None
        total += length * rate
    return total


def _check(name, value, limit, basis, *, minimum=False):
    status = 'unknown' if value is None or limit is None else (
        'pass' if (value >= limit if minimum else value <= limit) else 'fail')
    return {'name': name, 'status': status, 'value': value, 'limit': limit, 'basis': basis}


def analyze_bus(protocol: str, params: dict, links: list[dict] | None = None) -> dict:
    """Calculate conservative checks using explicit parameters (see module docstring).

    Cable capacitance and delay use the upper model bound, never the lower bound
    as a substitute. Termination checks require both impedance bounds. Results
    describe only the listed checks and do not establish complete bus compliance.
    """
    p = str(protocol).lower().replace('-', '_')
    if p not in ('i2c', 'can', 'can_fd', 'rs485', 'uart', 'spi'):
        raise ValueError(f'unsupported bus protocol {protocol!r}')
    if not isinstance(params, dict):
        raise ValueError('params must be a dictionary')
    links = links or []
    checks = []
    basis = []
    limitations = ['Screening only; topology, signal integrity, transceiver tolerances, EMC and all unlisted requirements are not verified.',
                   'Links must represent all series sections of one path; branch loading and duplicate paired conductors are not inferred.']
    if p == 'i2c':
        r = _number(params.get('pullup_ohm'))
        v = _number(params.get('supply_v'))
        sink = _number(params.get('sink_current_ma'))
        cdev = _number(params.get('device_capacitance_pf'), zero=True)
        cable_c = _sum_links(links, 'c_pf_per_m', 'hi')
        cap = None if cable_c is None or cdev is None else cable_c + cdev
        rise = None if r is None or cap is None else 0.8473 * r * cap / 1000
        checks.append(_check('rise_time_ns', rise, _number(params.get('max_rise_time_ns')),
                             '30%-to-70% lumped RC rise time = 0.8473 R C; NXP UM10204 I2C-bus specification, pull-up resistor calculation.'))
        mode = str(params.get('mode', '')).lower()
        cap_limit = _number(params.get('max_capacitance_pf'))
        if mode in ('standard', 'fast'):
            cap_limit = min(cap_limit, 400.0) if cap_limit is not None else 400.0
        checks.append(_check('bus_capacitance_pf', cap, cap_limit,
                             'NXP UM10204 I2C-bus specification, Standard-mode and Fast-mode maximum bus capacitance 400 pF; otherwise caller-supplied limit.'))
        vol = _number(params.get('vol_v'), zero=True)
        valid_vol = vol is not None and v is not None and vol < v
        current = None if r is None or not valid_vol else (v - vol) / r * 1000
        min_r = None if sink is None or not valid_vol else (v - vol) * 1000 / sink
        rise_limit = _number(params.get('max_rise_time_ns'))
        max_r = None if rise_limit is None or cap is None or cap <= 0 else rise_limit * 1000 / (0.8473 * cap)
        checks.append(_check('pullup_sink_current_ma', current, sink,
                             'Current (Vcc - VOL)/R at supplied VOL compared with guaranteed sink capability.'))
        checks.append(_check('pullup_minimum_ohm', r, min_r,
                             'NXP UM10204: Rmin = (Vcc - VOL)/IOL at the supplied guaranteed VOL and sink current.', minimum=True))
        checks.append(_check('pullup_maximum_ohm', r, max_r,
                             'NXP UM10204: Rmax = tr(max)/(0.8473 C), for a lumped 30%-to-70% rise.'))
        limitations.append('Lumped RC assumes a single pull-up and ignores distributed line effects and device input leakage.')
    elif p in ('can', 'can_fd', 'rs485'):
        term = params.get('termination_ohms')
        tolerance = _number(params.get('termination_tolerance_fraction'), zero=True)
        count_status = 'unknown' if not isinstance(term, list) else ('pass' if len(term) == 2 else 'fail')
        checks.append({'name': 'termination_count', 'status': count_status,
                       'value': len(term) if isinstance(term, list) else None, 'limit': 2,
                       'basis': 'Two physical end terminations required for this transmission-line screening.'})
        resistors = None
        if isinstance(term, list) and len(term) == 2:
            resistors = [_number(x) for x in term]
            if any(x is None for x in resistors):
                resistors = None
        lows = [_bound(link, 'z0_ohm', 'lo') for link in links if isinstance(link, dict)]
        highs = [_bound(link, 'z0_ohm', 'hi') for link in links if isinstance(link, dict)]
        mismatch = None
        if resistors and links and len(lows) == len(links) and all(x is not None for x in lows + highs):
            mismatch = max(abs(r / z - 1) for r in resistors for z in lows + highs)
        checks.append(_check('termination_fractional_mismatch', mismatch, tolerance,
                             'Worst |R/Z0 - 1| over both end resistors and cable impedance bounds; tolerance supplied by caller.'))
        stubs = params.get('stub_lengths_m')
        stub_rate = _number(params.get('stub_delay_ns_per_m'))
        stub_delay = None
        if isinstance(stubs, list):
            lengths = [_number(x, zero=True) for x in stubs]
            if all(x is not None for x in lengths):
                if not lengths or max(lengths) == 0:
                    stub_delay = 0.0
                elif stub_rate is not None:
                    stub_delay = max(lengths) * stub_rate
        checks.append(_check('stub_one_way_delay_ns', stub_delay, _number(params.get('max_stub_delay_ns'), zero=True),
                             'Longest stub times caller-supplied stub cable propagation delay; caller supplies the allowable one-way delay.'))
        limitations.append('Termination check assumes exactly two physical end terminations.')
        bitrate = _number(params.get('bitrate_hz'))
        sample = _number(params.get('sample_point'))
        if sample is not None and sample > 1:
            sample = None
        loop = _number(params.get('loop_delay_ns'), zero=True)
        cable_delay = _sum_links(links, 'td_ns_per_m', 'hi')
        round_trip = None if cable_delay is None or loop is None else 2 * cable_delay + loop
        budget = None if bitrate is None or sample is None else 1e9 * sample / bitrate
        checks.append(_check('round_trip_to_sample_ns', round_trip, budget,
                             'Conservative timing heuristic: twice the upper-bound one-way cable delay plus caller-supplied non-cable loop delay versus sample point / bitrate.'))
        limitations.append('Timing is a screening heuristic, not a complete CAN bit-timing or RS-485 receiver analysis; include oscillator tolerance, synchronization, edge settling, and all other margins in the supplied budget/delays. CAN FD phases must be assessed separately.')
        if p == 'rs485':
            up = _number(params.get('bias_pullup_ohm'))
            down = _number(params.get('bias_pulldown_ohm'))
            receiver_load = _number(params.get('bias_load_ohm'))
            v = _number(params.get('supply_v'))
            bias = None
            if resistors and up is not None and down is not None and receiver_load is not None and v is not None:
                load = 1 / (sum(1 / r for r in resistors) + 1 / receiver_load)
                bias = v * load / (up + load + down)
            checks.append(_check('idle_bias_v', bias, _number(params.get('min_bias_v')),
                                 'DC divider: Vcc * (Rterm1 || Rterm2 || Rload)/(Rpullup + parallel load + Rpulldown); compare caller threshold.', minimum=True))
            limitations.append('Bias calculation uses the supplied aggregate differential load; supply and resistor tolerances, leakage and common-mode constraints must be included in the margin.')
    else:
        delay = _sum_links(links, 'td_ns_per_m', 'hi')
        checks.append(_check('one_way_delay_ns', delay, _number(params.get('max_delay_ns')),
                             'Sum of section length times upper propagation-delay bound compared with caller timing budget.'))
        limitations.append('The timing budget must include device delays, skew, setup/hold, clocking and any return path.')
    for link in links:
        if isinstance(link, dict) and isinstance(link.get('cable_model'), dict):
            for item in link['cable_model'].get('basis', []):
                if item not in basis:
                    basis.append(item)
            for item in link['cable_model'].get('citations', []):
                if item not in basis:
                    basis.append(item)
    states = {c['status'] for c in checks}
    status = 'fail' if 'fail' in states else 'unknown' if 'unknown' in states else 'pass'
    return {'protocol': p, 'status': status, 'checks': checks, 'basis': basis, 'limitations': limitations}


def harness_to_bus_links(harness: dict, templates: list[dict]) -> list[dict]:
    """Build firmware ``bus_links`` from explicit endpoint and topology templates.

    Each template is a firmware link, including peer, clock and complete signal
    endpoints. For ``lossless_tline``, each signal segment supplies ``from``,
    ``to``, ``wire_id`` and ``cable``. For ``lumped_rc``, a signal supplies
    ``harness_wires``: a nonempty list of ``{wire_id, cable}`` series sections.
    Cable data must resolve to exact impedance/delay or resistance/capacitance,
    respectively; interval bounds cannot be represented by the firmware model.
    A CAN lumped section additionally requires the explicitly specified
    ``cable.datasheet.loop_resistance_ohm_per_km`` (both conductors together),
    not the single-conductor resistance used for single-ended signals.
    Templates are copied, and the native firmware validator remains authoritative.
    """
    import copy

    required = {'i2c': {'scl', 'sda'}, 'uart': {'tx', 'rx'},
                'spi': {'sck', 'mosi', 'miso', 'cs'}, 'can': {'can_diff'}}
    if not isinstance(templates, list) or not templates:
        raise ValueError('templates must be a nonempty list')
    result = copy.deepcopy(templates)
    ids, buses = set(), set()
    for link in result:
        if not isinstance(link, dict):
            raise ValueError('each link template must be an object')
        protocol = link.get('protocol')
        if protocol not in required:
            raise ValueError('firmware supports i2c, uart, spi and classical can')
        for key, seen in (('id', ids), ('bus', buses)):
            value = link.get(key)
            if not isinstance(value, str) or not value or value in seen:
                raise ValueError(f'{key} must be nonempty and unique')
            seen.add(value)
        if _number(link.get('controller_clock_hz')) is None or not isinstance(link.get('peer'), dict):
            raise ValueError('explicit controller_clock_hz and peer are required')
        peer = link['peer']
        if protocol == 'uart' and (_number(peer.get('baud_hz')) is None or peer.get('electrical_variant') != 'ttl'):
            raise ValueError('UART peer needs baud_hz and electrical_variant ttl')
        if protocol == 'can' and _number(peer.get('bitrate_hz')) is None:
            raise ValueError('CAN peer needs bitrate_hz')
        electrical = link.get('electrical')
        signals = electrical.get('signals') if isinstance(electrical, dict) else None
        if not isinstance(signals, list) or any(not isinstance(s, dict) for s in signals):
            raise ValueError('complete electrical.signals required')
        names = [s.get('name') for s in signals]
        if len(names) != len(required[protocol]) or set(names) != required[protocol]:
            raise ValueError('all protocol signals must appear exactly once')
        for signal in signals:
            if signal.get('mode') != ('differential' if protocol == 'can' else 'single_ended'):
                raise ValueError('signal mode does not match protocol')
            endpoints = signal.get('endpoints')
            if not isinstance(endpoints, list) or any(not isinstance(e, dict) for e in endpoints):
                raise ValueError('complete endpoints required')
            endpoint_ids = [e.get('id') for e in endpoints]
            if not {'mcu', 'peer'}.issubset(endpoint_ids) or len(set(endpoint_ids)) != len(endpoint_ids):
                raise ValueError('unique mcu and peer endpoints required')
            for endpoint in endpoints:
                if not isinstance(endpoint.get('id'), str) or not endpoint['id'] or not isinstance(endpoint.get('at'), str) or not endpoint['at']:
                    raise ValueError('endpoint id and location required')
                for key in ('input_capacitance_f', 'rise_s', 'fall_s', 'tx_delay_s', 'rx_delay_s', 'v_low_v', 'v_high_v'):
                    if _number(endpoint.get(key), zero=True) is None:
                        raise ValueError(f'endpoint {key} must be explicit and nonnegative')
                for key in ('r_low_ohm', 'r_high_ohm'):
                    if _number(endpoint.get(key)) is None:
                        raise ValueError(f'endpoint {key} must be explicit and positive')
                lo, hi = ('recessive_max_v', 'dominant_min_v') if protocol == 'can' else ('vil_v', 'vih_v')
                low, high = _number(endpoint.get(lo), zero=True), _number(endpoint.get(hi))
                if low is None or high is None or low >= high:
                    raise ValueError('explicit ordered receiver thresholds required')
                drive = 'can_dominant' if protocol == 'can' else ('open_drain' if protocol == 'i2c' else 'push_pull')
                external = protocol == 'can' and endpoint.get('role') == 'external_driver' and endpoint['id'] not in ('mcu', 'peer')
                if endpoint.get('drive') != drive and not (external and endpoint.get('drive') == 'push_pull'):
                    raise ValueError('endpoint drive does not match protocol')
            model = signal.get('model')
            if model == 'lossless_tline':
                segments = signal.get('segments')
                if not isinstance(segments, list) or not segments or signal.get('wire_capacitance_f', 0) != 0:
                    raise ValueError('lossless_tline requires segments and zero lumped wire capacitance')
                for segment in segments:
                    if not isinstance(segment, dict) or not isinstance(segment.get('from'), str) or not isinstance(segment.get('to'), str) or not segment['from'] or not segment['to'] or segment['from'] == segment['to']:
                        raise ValueError('segment needs distinct explicit from/to locations')
                    wire_id, cable = segment.pop('wire_id', None), segment.pop('cable', None)
                    section = harness_to_analysis_links(harness, [wire_id], cable)[0]
                    cm, length = section['cable_model'], section['length_m']
                    z = _bound(section, 'z0_ohm', 'value')
                    delay = _bound(section, 'td_ns_per_m', 'value')
                    if length is None or cm is None or z is None or delay is None:
                        raise ValueError(f'wire {wire_id!r} requires positive routed length and exact cable impedance and delay')
                    segment['z0_ohm'] = z
                    segment['propagation_s'] = length * delay * 1e-9
            elif model == 'lumped_rc':
                sections = signal.pop('harness_wires', None)
                if not isinstance(sections, list) or not sections or signal.get('segments'):
                    raise ValueError('lumped_rc requires harness_wires and no segments')
                resistance = capacitance = 0.0
                for item in sections:
                    if not isinstance(item, dict):
                        raise ValueError('harness_wires entries must be objects')
                    section = harness_to_analysis_links(harness, [item.get('wire_id')], item.get('cable'))[0]
                    cm, length = section['cable_model'], section['length_m']
                    if protocol == 'can':
                        cable = item.get('cable')
                        datasheet = cable.get('datasheet') if isinstance(cable, dict) else None
                        loop_r = _number(datasheet.get('loop_resistance_ohm_per_km')) if isinstance(datasheet, dict) else None
                        r = None if loop_r is None else loop_r / 1000
                    else:
                        r = _number(cm.get('r_dc_ohm_per_m'), zero=True) if isinstance(cm, dict) else None
                    c = _bound(section, 'c_pf_per_m', 'value')
                    if length is None or r is None or c is None:
                        raise ValueError(f"wire {item.get('wire_id')!r} requires positive routed length, exact capacitance and explicit {'loop resistance' if protocol == 'can' else 'resistance'}")
                    resistance += r * length
                    capacitance += c * length * 1e-12
                signal['series_r_ohm'] = resistance
                signal['wire_capacitance_f'] = capacitance
            else:
                raise ValueError('signal model must be lumped_rc or lossless_tline')
    return result
