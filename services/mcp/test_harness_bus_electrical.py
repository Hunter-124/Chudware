"""Behavioral checks for the pure harness bus electrical calculations."""
import pytest

from services.harness.bus_electrical import analyze_bus, harness_to_analysis_links, harness_to_bus_links


CABLE = {'kind': 'twisted_pair', 'datasheet': {'z0_ohm': 120, 'delay_ns_per_m': 5}}
HARNESS = {'wires': [
    {'id': 'w1', 'kind': 'twisted_pair', 'length_mm': 2000,
     'from': {'connector': 'a', 'pin': '1'}, 'to': {'connector': 'b', 'pin': '1'}},
    {'id': 'w2', 'kind': 'wire', 'length_mm': None, 'insulation_od_mm': 2},
]}


def checks(result):
    return {c['name']: c for c in result['checks']}


def test_conversion_uses_explicit_datasheet_and_route():
    link = harness_to_analysis_links(HARNESS, ['w1'], CABLE)[0]
    assert link['length_m'] == 2
    assert link['cable_model']['z0_ohm']['value'] == pytest.approx(120)
    assert link['cable_model']['td_ns_per_m']['value'] == pytest.approx(5)
    assert link['from']['connector'] == 'a'
    assert harness_to_analysis_links(HARNESS, ['w2'])[0]['cable_model'] is None
    assert harness_to_analysis_links(HARNESS, ['w2'], CABLE)[0]['length_m'] is None
    with pytest.raises(ValueError):
        harness_to_analysis_links(HARNESS, ['missing'], CABLE)
    with pytest.raises(ValueError):
        harness_to_analysis_links(HARNESS, ['w1', 'w1'], CABLE)


def test_i2c_rise_and_current_and_missing_capacitance():
    links = harness_to_analysis_links(HARNESS, ['w1'], CABLE)
    params = {'pullup_ohm': 1000, 'supply_v': 3.3, 'vol_v': .4, 'sink_current_ma': 4,
              'max_rise_time_ns': 100, 'device_capacitance_pf': 20, 'mode': 'fast'}
    result = analyze_bus('i2c', params, links)
    assert result['status'] == 'pass'
    assert checks(result)['pullup_sink_current_ma']['value'] == pytest.approx(2.9)
    assert checks(result)['pullup_minimum_ohm']['limit'] == pytest.approx(725)
    assert checks(result)['pullup_maximum_ohm']['limit'] == pytest.approx(100000 / (0.8473 * (20 + 2 * 5 / 120 * 1000)))
    assert checks(analyze_bus('i2c', {**params, 'pullup_ohm': 500}, links))['pullup_minimum_ohm']['status'] == 'fail'
    assert checks(analyze_bus('i2c', {**params, 'pullup_ohm': 2000}, links))['pullup_maximum_ohm']['status'] == 'fail'
    assert checks(analyze_bus('i2c', {**params, 'vol_v': None}, links))['pullup_minimum_ohm']['status'] == 'unknown'
    assert checks(result)['rise_time_ns']['value'] == pytest.approx(0.8473 * (20 + 2 * 5 / 120 * 1000))
    assert analyze_bus('i2c', {**params, 'max_rise_time_ns': 1}, links)['status'] == 'fail'
    assert analyze_bus('i2c', {**params, 'device_capacitance_pf': None}, links)['status'] == 'unknown'
    assert analyze_bus('i2c', params, [])['status'] == 'unknown'
    assert checks(analyze_bus('i2c', {**params, 'device_capacitance_pf': 400}, links))['bus_capacitance_pf']['status'] == 'fail'
    assert analyze_bus('i2c', {**params, 'mode': None}, links)['status'] == 'unknown'


@pytest.mark.parametrize('protocol', ['can', 'can_fd', 'rs485'])
def test_differential_termination_stub_and_bias(protocol):
    links = harness_to_analysis_links(HARNESS, ['w1'], CABLE)
    params = {'termination_ohms': [120, 120], 'termination_tolerance_fraction': .1,
              'stub_lengths_m': [0.2], 'stub_delay_ns_per_m': 5, 'max_stub_delay_ns': 2,
              'bias_pullup_ohm': 470, 'bias_pulldown_ohm': 470,
              'bias_load_ohm': 12000, 'supply_v': 5, 'min_bias_v': .2,
              'bitrate_hz': 1000000, 'sample_point': .8, 'loop_delay_ns': 100}
    result = analyze_bus(protocol, params, links)
    assert result['status'] == 'pass'
    assert checks(result)['stub_one_way_delay_ns']['value'] == pytest.approx(1)
    assert checks(result)['round_trip_to_sample_ns']['value'] == pytest.approx(120)
    assert checks(analyze_bus(protocol, {**params, 'bitrate_hz': 10000000}, links))['round_trip_to_sample_ns']['status'] == 'fail'
    assert checks(analyze_bus(protocol, {**params, 'loop_delay_ns': None}, links))['round_trip_to_sample_ns']['status'] == 'unknown'
    assert analyze_bus(protocol, {**params, 'termination_ohms': [60, 120]}, links)['status'] == 'fail'
    assert analyze_bus(protocol, {**params, 'termination_ohms': []}, links)['status'] == 'fail'
    assert analyze_bus(protocol, {**params, 'stub_delay_ns_per_m': None}, links)['status'] == 'unknown'
    assert analyze_bus(protocol, {**params, 'max_stub_delay_ns': .5}, links)['status'] == 'fail'
    if protocol == 'rs485':
        assert analyze_bus(protocol, {**params, 'bias_load_ohm': None}, links)['status'] == 'unknown'
        assert analyze_bus(protocol, {**params, 'min_bias_v': 1}, links)['status'] == 'fail'


@pytest.mark.parametrize('protocol', ['uart', 'spi'])
def test_serial_timing_uses_upper_bound_and_unknowns(protocol):
    link = {'length_m': 2, 'cable_model': {'td_ns_per_m': {'lo': 4, 'hi': 6, 'value': None}}}
    assert analyze_bus(protocol, {'max_delay_ns': 13}, [link])['status'] == 'pass'
    assert analyze_bus(protocol, {'max_delay_ns': 11}, [link])['status'] == 'fail'
    link['cable_model']['td_ns_per_m']['hi'] = None
    assert analyze_bus(protocol, {'max_delay_ns': 13}, [link])['status'] == 'unknown'
    assert analyze_bus(protocol, {}, [link])['status'] == 'unknown'


def test_missing_geometry_and_invalid_inputs_never_false_pass():
    link = harness_to_analysis_links(HARNESS, ['w2'], {'kind': 'parallel', 'er_eff': 2})[0]
    assert link['cable_model'] is None
    assert link['model_error']
    assert analyze_bus('uart', {'max_delay_ns': 100}, [link])['status'] == 'unknown'
    with pytest.raises(ValueError):
        analyze_bus('unsupported', {}, [])


def _endpoint(name):
    return {'id': name, 'at': name, 'drive': 'push_pull', 'input_capacitance_f': 0,
            'rise_s': 0, 'fall_s': 0, 'tx_delay_s': 0, 'rx_delay_s': 0,
            'v_low_v': 0, 'v_high_v': 3.3, 'r_low_ohm': 10, 'r_high_ohm': 10,
            'vil_v': .8, 'vih_v': 2}


def _uart_template(model, cable):
    signals = []
    for name in ('tx', 'rx'):
        signal = {'name': name, 'mode': 'single_ended', 'model': model,
                  'endpoints': [_endpoint('mcu'), _endpoint('peer')]}
        if model == 'lossless_tline':
            signal['segments'] = [{'from': 'mcu', 'to': 'peer', 'wire_id': 'w1', 'cable': cable}]
        else:
            signal['harness_wires'] = [{'wire_id': 'w1', 'cable': cable}]
        signals.append(signal)
    return {'id': 'serial', 'bus': 'usart1', 'protocol': 'uart', 'controller_clock_hz': 16000000,
            'peer': {'baud_hz': 9600, 'electrical_variant': 'ttl'}, 'electrical': {'signals': signals}}


def test_firmware_link_conversion_requires_complete_inputs():
    template = _uart_template('lossless_tline', CABLE)
    result = harness_to_bus_links(HARNESS, [template])
    segment = result[0]['electrical']['signals'][0]['segments'][0]
    assert segment == {'from': 'mcu', 'to': 'peer', 'z0_ohm': pytest.approx(120),
                       'propagation_s': pytest.approx(10e-9)}
    assert 'wire_id' in template['electrical']['signals'][0]['segments'][0]
    template['electrical']['signals'][0]['endpoints'][0].pop('vih_v')
    with pytest.raises(ValueError):
        harness_to_bus_links(HARNESS, [template])
    template = _uart_template('lossless_tline', CABLE)
    template['electrical']['signals'][0]['segments'][0]['wire_id'] = 'w2'
    with pytest.raises(ValueError):
        harness_to_bus_links(HARNESS, [template])


def test_firmware_lumped_rc_requires_resistance_and_capacitance():
    cable = {'datasheet': {'capacitance_pf_per_m': 50, 'resistance_ohm_per_km': 100}}
    template = _uart_template('lumped_rc', cable)
    signal = harness_to_bus_links(HARNESS, [template])[0]['electrical']['signals'][0]
    assert signal['series_r_ohm'] == pytest.approx(.2)
    assert signal['wire_capacitance_f'] == pytest.approx(100e-12)
    del cable['datasheet']['resistance_ohm_per_km']
    with pytest.raises(ValueError):
        harness_to_bus_links(HARNESS, [template])


def test_zero_length_is_not_a_routed_link():
    harness = {'wires': [{'id': 'w1', 'length_mm': 0, 'kind': 'wire'}]}
    assert harness_to_analysis_links(harness, ['w1'], CABLE)[0]['length_m'] is None
    with pytest.raises(ValueError):
        harness_to_bus_links(harness, [_uart_template('lossless_tline', CABLE)])


def test_can_lumped_requires_explicit_differential_loop_resistance():
    cable = {'datasheet': {'capacitance_pf_per_m': 50, 'resistance_ohm_per_km': 100}}
    endpoints = []
    for name in ('mcu', 'peer'):
        endpoint = _endpoint(name)
        endpoint.update({'drive': 'can_dominant', 'recessive_max_v': .5, 'dominant_min_v': .9})
        endpoints.append(endpoint)
    template = {'id': 'can_link', 'bus': 'can1', 'protocol': 'can',
                'controller_clock_hz': 16000000, 'peer': {'bitrate_hz': 500000},
                'electrical': {'signals': [{'name': 'can_diff', 'mode': 'differential',
                    'model': 'lumped_rc', 'endpoints': endpoints,
                    'harness_wires': [{'wire_id': 'w1', 'cable': cable}]}]}}
    with pytest.raises(ValueError, match='loop resistance'):
        harness_to_bus_links(HARNESS, [template])
    cable['datasheet']['loop_resistance_ohm_per_km'] = 240
    signal = harness_to_bus_links(HARNESS, [template])[0]['electrical']['signals'][0]
    assert signal['series_r_ohm'] == pytest.approx(.48)
    assert signal['wire_capacitance_f'] == pytest.approx(100e-12)
