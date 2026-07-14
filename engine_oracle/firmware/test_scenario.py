"""Tests for the sensor stimulus scenario system (T-FIN3, aicad.fwscenario/1).

Renode is absent on this machine (and in CI), so — per the T-C3/T-FIN1 precedent —
coverage is carried by pure-function tests: schema validation, fixture library,
waveform expansion, .resc-line generation, timeline rendering, the new assertion
vocabulary, and mocked-subprocess plumbing through fw_sim_run. The ADC PythonPeripheral
script form is additionally live-checked against Renode 1.16.1 (inline script: + Is*).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.firmware import fixtures
from engine.firmware.emulator import (
    _build_resc, _pins_from_assertions, evaluate_assertions, parse_uart_events,
    run_simulation,
)
from engine.firmware.platform_db import get_capabilities, get_platform_file
from engine.firmware.scenario import (
    SCHEMA, build_scenario_plan, load_scenario, materialize_aux_files,
    timeline_resc_lines, validate_scenario, waveform_samples,
)

STM32F4_CAPS = get_capabilities("stm32f4")
STM32F4_REPL = get_platform_file("stm32f4").read_text(encoding="utf-8")


def _sc(*tracks) -> dict:
    return {"schema": SCHEMA, "tracks": list(tracks)}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_validate_rejects_wrong_schema_and_empty_tracks():
    assert validate_scenario({"schema": "nope", "tracks": []})
    assert any("schema" in e for e in validate_scenario({"tracks": [{}]}))
    assert any("tracks" in e for e in validate_scenario({"schema": SCHEMA}))


def test_validate_unknown_kind_and_bad_pin():
    errs = validate_scenario(_sc({"kind": "warp_drive"}))
    assert any("unknown kind" in e for e in errs)
    errs = validate_scenario(_sc({"kind": "gpio_edges", "pin": "XX9",
                                  "events": [{"t_ms": 1, "level": 1}]}))
    assert any("pin" in e for e in errs)


def test_validate_good_tracks_pass():
    sc = _sc(
        {"kind": "gpio_edges", "pin": "PA0", "events": [{"t_ms": 10, "level": 1}]},
        {"kind": "button", "pin": "PB3", "presses": [{"t_ms": 5, "hold_ms": 20}]},
        {"kind": "quadrature", "pin_a": "PA0", "pin_b": "PA1", "steps": 8, "step_ms": 2},
        {"kind": "i2c_sensor", "model": "tmp103", "address": 0x48, "temperature_c": 25},
        {"kind": "i2c_registers", "address": 0x50, "response": [1, 2, 3]},
        {"kind": "spi_stub", "bus": "spi1"},
        {"kind": "uart_rx", "events": [{"t_ms": 100, "text": "go\n"}]},
        {"kind": "adc_waveform", "waveform": {"type": "sine", "min": 0, "max": 4095,
                                              "period_ms": 100}},
    )
    assert validate_scenario(sc) == []


def test_load_scenario_json_dict_fixture_and_errors():
    obj, errs = load_scenario(json.dumps(_sc({"kind": "spi_stub", "bus": "spi1"})))
    assert errs == [] and obj["schema"] == SCHEMA
    obj, errs = load_scenario(_sc({"kind": "spi_stub", "bus": "spi1"}))
    assert errs == []
    obj, errs = load_scenario("fixture:i2c_temp_tmp103")
    assert errs == [] and obj["tracks"][0]["model"] == "tmp103"
    obj, errs = load_scenario("user_button")  # bare fixture name
    assert errs == []
    obj, errs = load_scenario("no_such_fixture")
    assert obj is None and "unknown scenario fixture" in errs[0]
    obj, errs = load_scenario("{not json")
    assert obj is None and "not valid JSON" in errs[0]
    obj, errs = load_scenario(None)
    assert obj is None and errs == []


# ---------------------------------------------------------------------------
# Fixture library (DoD: >= 4 archetypes)
# ---------------------------------------------------------------------------

def test_fixture_library_covers_archetypes_and_validates():
    names = fixtures.list_fixtures()
    assert len(names) >= 4
    for required in ("i2c_temp_tmp103", "spi_imu_stub", "quadrature_encoder",
                     "adc_thermistor"):
        assert required in names
    for name in names:
        fx = fixtures.get_fixture(name)
        assert validate_scenario(fx) == [], name


def test_get_fixture_returns_deep_copy():
    a = fixtures.get_fixture("user_button")
    a["tracks"][0]["presses"].clear()
    b = fixtures.get_fixture("user_button")
    assert b["tracks"][0]["presses"]


def test_adc_thermistor_curve_is_monotonic_decreasing():
    # NTC in a divider to GND: heating up -> resistance drops -> counts drop.
    vals = fixtures.get_fixture("adc_thermistor")["tracks"][0]["waveform"]["values"]
    assert all(a >= b for a, b in zip(vals, vals[1:]))
    assert all(0 <= v <= 4095 for v in vals)


# ---------------------------------------------------------------------------
# Waveforms
# ---------------------------------------------------------------------------

def test_waveform_sine_ramp_samples():
    sine = waveform_samples({"type": "sine", "min": 0, "max": 4095, "period_ms": 64})
    assert max(sine) > 3500 and min(sine) < 600 and all(0 <= v <= 4095 for v in sine)
    ramp = waveform_samples({"type": "ramp", "min": 100, "max": 200, "period_ms": 10})
    assert ramp[0] == 100 and ramp[-1] < 200 and ramp == sorted(ramp)
    assert waveform_samples({"type": "samples", "values": [-5, 9999, 42]}) == [0, 4095, 42]


# ---------------------------------------------------------------------------
# Plan compilation → Renode lines
# ---------------------------------------------------------------------------

def test_plan_button_and_edges_use_button_peripheral():
    sc = _sc({"kind": "button", "pin": "PA0",
              "presses": [{"t_ms": 100, "hold_ms": 50}]})
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == []
    assert any("Miscellaneous.Button @ gpioPortA 0" in ln for ln in plan.setup_lines)
    acts = [ln for _, ln in plan.timeline]
    assert "sysbus.gpioPortA.aicadBtnPA0 Press" in acts
    assert "sysbus.gpioPortA.aicadBtnPA0 Release" in acts
    assert plan.duration_ms == 150.0


def test_plan_quadrature_generates_gray_sequence():
    sc = _sc({"kind": "quadrature", "pin_a": "PA0", "pin_b": "PA1",
              "start_ms": 0, "steps": 4, "step_ms": 10})
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == []
    # 4 steps -> one edge per step (x4 sequence changes one channel at a time)
    assert len(plan.timeline) == 4
    ts = [t for t, _ in plan.timeline]
    assert ts == sorted(ts) and ts[0] == 10.0


def test_plan_i2c_tmp103_and_registers():
    sc = _sc(
        {"kind": "i2c_sensor", "model": "tmp103", "bus": "i2c1", "address": 0x70,
         "temperature_c": 31.0},
        {"kind": "i2c_registers", "bus": "i2c1", "address": 0x48,
         "response": [0x12, 0x34]},
    )
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == []
    joined = "\n".join(plan.setup_lines)
    assert "Sensors.TMP103 @ i2c1 0x70" in joined
    assert "Temperature 31" in joined
    assert "Mocks.DummyI2CSlave @ i2c1 0x48" in joined
    assert joined.count("EnqueueResponseByte") == 2


def test_plan_uart_rx_injects_bytes_at_time():
    sc = _sc({"kind": "uart_rx", "events": [{"t_ms": 50, "text": "hi"}]})
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == []
    assert [(t, ln) for t, ln in plan.timeline] == [
        (50.0, "sysbus.usart1 WriteChar 0x68"),
        (50.0, "sysbus.usart1 WriteChar 0x69"),
    ]


def test_plan_adc_waveform_python_peripheral_on_stm32():
    sc = _sc({"kind": "adc_waveform",
              "waveform": {"type": "samples", "values": [1, 2, 3]}})
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == []
    joined = "\n".join(plan.setup_lines)
    assert "LoadPlatformDescription @{AICAD_SCENARIO_DIR}/aicad_adc_stub.repl" in joined
    assert "Python.PythonPeripheral @ sysbus 0x40012000" not in joined  # lives in aux .repl
    repl = plan.aux_files["aicad_adc_stub.repl"]
    assert "Python.PythonPeripheral @ sysbus 0x40012000" in repl
    assert "script: '''" in repl
    assert "[1, 2, 3]" in repl
    assert "request.IsInit" in repl and "request.IsRead" in repl
    assert "request.Offset" in repl and "request.Value" in repl
    assert "0x4c" in repl
    # Old Renode API must not appear (filename: + lowercase request props).
    assert "filename:" not in repl and "request.isInit" not in repl
    assert "request.isRead" not in repl and "request.value" not in repl


def test_plan_adc_refuses_honestly_off_stm32():
    caps = get_capabilities("nrf52")
    repl = get_platform_file("nrf52").read_text(encoding="utf-8")
    sc = _sc({"kind": "adc_waveform",
              "waveform": {"type": "sine", "min": 0, "max": 100, "period_ms": 10}})
    plan = build_scenario_plan(sc, caps, repl)
    assert plan.errors and "adc stimulus unsupported" in plan.errors[0]


def test_plan_gpio_refuses_when_port_missing():
    caps = get_capabilities("cortex-m")
    sc = _sc({"kind": "button", "pin": "PA0", "presses": [{"t_ms": 1}]})
    plan = build_scenario_plan(sc, caps, "cpu: CPU.CortexM @ sysbus\n")
    assert plan.errors and "gpio stimulus on PA0 unsupported" in plan.errors[0]


def test_plan_spi_stub_carries_warning():
    sc = _sc({"kind": "spi_stub", "bus": "spi1", "name": "imu"})
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == []
    assert any("Mocks.DummySPISlave @ spi1" in ln for ln in plan.setup_lines)
    assert plan.warnings


def test_materialize_aux_files_substitutes_dir(tmp_path):
    sc = _sc({"kind": "adc_waveform",
              "waveform": {"type": "samples", "values": [7]}})
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    materialize_aux_files(plan, tmp_path / "aux")
    assert (tmp_path / "aux" / "aicad_adc_stub.repl").exists()
    assert all("{AICAD_SCENARIO_DIR}" not in ln for ln in plan.setup_lines)
    assert any(str(tmp_path / "aux") in ln for ln in plan.setup_lines)
    # Materialized .repl is loadable as-is by Renode (inline script: form).
    repl_text = (tmp_path / "aux" / "aicad_adc_stub.repl").read_text(encoding="utf-8")
    assert "script: '''" in repl_text and "request.IsInit" in repl_text


# ---------------------------------------------------------------------------
# Timeline rendering + .resc integration
# ---------------------------------------------------------------------------

def test_timeline_resc_lines_sleeps_and_marks():
    lines = timeline_resc_lines([(100.0, "A"), (100.0, "B"), (350.0, "C")], 1.0)
    assert lines[0] == 'echo "AICAD_T_MARK 0"'
    assert "sleep 0.100" in lines and "A" in lines and "B" in lines
    assert 'echo "AICAD_T_MARK 100"' in lines
    assert "sleep 0.250" in lines and "C" in lines
    assert "sleep 0.650" in lines  # remainder up to 1000 ms
    assert lines[-1] == 'echo "AICAD_T_MARK 1000"'


def test_build_resc_splices_scenario():
    plan = build_scenario_plan(
        _sc({"kind": "button", "pin": "PA0", "presses": [{"t_ms": 100, "hold_ms": 50}]}),
        STM32F4_CAPS, STM32F4_REPL)
    resc = _build_resc("/tmp/fw.elf", str(get_platform_file("stm32f4")),
                       run_seconds=2.0, gpio_pins=[("A", 5)],
                       scenario_setup_lines=plan.setup_lines,
                       scenario_timeline=plan.timeline)
    # setup before LoadELF, timeline replaces the single sleep, marks present
    assert resc.index("Miscellaneous.Button") < resc.index("LoadELF")
    assert "sleep 2.0\n" not in resc
    assert "AICAD_T_MARK" in resc
    assert "aicadBtnPA0 Press" in resc
    assert "AddStateChangedHook 5" in resc  # T-FIN1 hooks unaffected


def test_build_resc_without_scenario_unchanged():
    resc = _build_resc("/tmp/fw.elf", str(get_platform_file("stm32f4")), run_seconds=3.0)
    assert "sleep 3.0" in resc and "AICAD_T_MARK" not in resc


# ---------------------------------------------------------------------------
# New assertion vocabulary
# ---------------------------------------------------------------------------

def _ev(pin, level, t_ms=None, seq=0):
    return {"type": "gpio", "seq": seq, "t_ms": t_ms, "pin": pin, "level": level}


def test_toggles_assertion_counts_transitions():
    events = [_ev("PA5", 0), _ev("PA5", 1), _ev("PA5", 0), _ev("PA5", 1),
              _ev("PB0", 1)]
    r = evaluate_assertions("", {}, ["toggles:PA5>=3"], gpio_events=events)
    assert r[0].passed and "3 time(s)" in r[0].detail
    r = evaluate_assertions("", {}, ["toggles:PA5==2"], gpio_events=events)
    assert not r[0].passed
    r = evaluate_assertions("", {}, ["toggles:PB0<1"], gpio_events=events)
    assert r[0].passed
    r = evaluate_assertions("", {}, ["toggles:PA5"], gpio_events=events)
    assert not r[0].passed and "must be" in r[0].detail


def test_period_assertion_mean_rising_edges():
    events = [_ev("PA5", 1, 0.0), _ev("PA5", 0, 50.0), _ev("PA5", 1, 100.0),
              _ev("PA5", 0, 150.0), _ev("PA5", 1, 202.0)]
    r = evaluate_assertions("", {}, ["period:PA5=100ms±5ms"], gpio_events=events)
    assert r[0].passed, r[0].detail
    r = evaluate_assertions("", {}, ["period:PA5=80ms+-5ms"], gpio_events=events)
    assert not r[0].passed
    # not enough stamped rising edges
    r = evaluate_assertions("", {}, ["period:PB1=10ms+-1ms"], gpio_events=events)
    assert not r[0].passed and ">=2 time-stamped rising edges" in r[0].detail


def test_uart_within_assertion():
    uart_events = [
        {"type": "uart", "seq": 1, "t_ms": 100.0, "text": "boot ok"},
        {"type": "uart", "seq": 2, "t_ms": 400.0, "text": "temp=31.0"},
    ]
    r = evaluate_assertions("", {}, ["uart_within:temp=31@500ms"],
                            uart_events=uart_events)
    assert r[0].passed
    r = evaluate_assertions("", {}, ["uart_within:temp=31@200ms"],
                            uart_events=uart_events)
    assert not r[0].passed and "first match at t=400ms" in r[0].detail
    r = evaluate_assertions("", {}, ["uart_within:nope@500ms"],
                            uart_events=uart_events)
    assert not r[0].passed
    r = evaluate_assertions("", {}, ["uart_within:temp@500ms"], uart_events=[])
    assert not r[0].passed and "no time-stamped uart events" in r[0].detail


def test_parse_uart_events_assigns_marks():
    raw = ('AICAD_T_MARK 0\nboot ok\nAICAD_T_MARK 250\ntemp=31.0\n'
           'AICAD_GPIO_EVT PA5=1\n\nAICAD_T_MARK 500\ndone\n')
    events = parse_uart_events(raw)
    assert [(e["text"], e["t_ms"]) for e in events] == [
        ("boot ok", 0.0), ("temp=31.0", 250.0), ("done", 500.0)]


def test_pins_from_assertions_includes_new_forms():
    pins = _pins_from_assertions(
        ["gpio:PA5=1", "toggles:PB3>=2", "period:PC7=10ms+-1ms", "uart:hello"])
    assert pins == [("A", 5), ("B", 3), ("C", 7)]


# ---------------------------------------------------------------------------
# run_simulation / fw_sim_run plumbing (mocked emulators)
# ---------------------------------------------------------------------------

def test_run_simulation_scenario_refuses_on_qemu_only():
    which = lambda b: "/usr/bin/qemu-system-arm" if "qemu" in b else None
    with patch("engine.firmware.emulator.shutil.which", side_effect=which):
        res = run_simulation("/tmp/fw.elf", "stm32f4", ["uart:x"],
                             scenario=fixtures.get_fixture("user_button"))
    assert not res.passed and "scenario_unsupported" in res.uart_log
    assert res.assertion_results[0].detail == "not evaluated: scenario requires Renode"


def test_run_simulation_scenario_error_is_honest_refusal():
    # Renode "installed" (mocked), but the scenario needs an ADC stub nrf52 lacks.
    with patch("engine.firmware.emulator.shutil.which",
               return_value="/usr/bin/renode"):
        res = run_simulation("/tmp/fw.elf", "nrf52", ["uart:x"],
                             scenario=fixtures.get_fixture("adc_thermistor"))
    assert not res.passed and "scenario_error" in res.uart_log
    assert "adc stimulus unsupported" in res.uart_log


def test_run_simulation_scenario_generates_resc_with_stimulus(tmp_path):
    """Renode mocked at subprocess level — verify the .resc handed to it carries the
    scenario stimulus and the run 'passes' assertions from canned output."""
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["resc"] = Path(cmd[-1]).read_text(encoding="utf-8")

        class P:
            returncode = 0
            stdout = ("AICAD_T_MARK 0\nAICAD_GPIO_EVT PA5=1\n"
                      "AICAD_T_MARK 200\ntemp=25.5\n")
            stderr = ""
        return P()

    with patch("engine.firmware.emulator.shutil.which",
               return_value="/usr/bin/renode"), \
         patch("engine.firmware.emulator.subprocess.run", side_effect=fake_run):
        res = run_simulation(str(elf), "stm32f4",
                             ["uart_within:temp=25.5@300ms", "toggles:PA5>=0"],
                             scenario=fixtures.get_fixture("i2c_temp_tmp103"))
    assert "Sensors.TMP103" in captured["resc"]
    assert res.passed, [a.to_dict() for a in res.assertion_results]
    # trace artifact carries the time-stamped uart event
    trace = json.loads(Path(res.trace_path).read_text(encoding="utf-8"))
    uart = [e for e in trace["events"] if e["type"] == "uart"]
    assert uart and uart[-1]["t_ms"] == 200.0


def test_fw_sim_run_rejects_invalid_scenario(tmp_path, monkeypatch):
    from engine.firmware import mcp_tools, project as fw_project
    monkeypatch.setenv("AI_CAD_FIRMWARE_DIR", str(tmp_path))
    proj = fw_project.new_project(None, "stm32f4", "cmake", name="scen")
    out = mcp_tools.fw_sim_run({"project_id": proj.project_id,
                                "elf_path": str(tmp_path / "x.elf"),
                                "scenario": '{"schema": "bogus", "tracks": []}'})
    assert out.get("code") == "invalid_scenario"


def test_fw_sim_run_accepts_fixture_reference(tmp_path, monkeypatch):
    from engine.firmware import mcp_tools, project as fw_project
    monkeypatch.setenv("AI_CAD_FIRMWARE_DIR", str(tmp_path))
    proj = fw_project.new_project(None, "stm32f4", "cmake", name="scen2")
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    with patch("engine.firmware.emulator.shutil.which", return_value=None):
        out = mcp_tools.fw_sim_run({"project_id": proj.project_id,
                                    "elf_path": str(elf),
                                    "scenario": "fixture:user_button"})
    # no emulator installed -> graceful SimResult, not an error envelope
    assert out["passed"] is False and "emulator_not_found" in out["uart_log"]
