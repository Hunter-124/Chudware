"""sim → firmware sensor bridge + time-varying i2c_register_stream (Wave-3 S1).

Deterministic pure-function tests. Prints wall-times. Renode is not required to
build or validate scenarios; if present we note it, never fabricate a live run.
"""
from __future__ import annotations

import shutil
import time

import pytest

from engine.firmware.platform_db import get_capabilities, get_platform_file
from engine.firmware.scenario import (
    SCHEMA,
    build_scenario_plan,
    validate_scenario,
)
from engine.firmware.sim_sensor_bridge import (
    build_sensor_scenario,
    climb_turn_profile,
)
from engine.firmware import mcp_tools as fw


STM32F4_CAPS = get_capabilities("stm32f4")
STM32F4_REPL = get_platform_file("stm32f4").read_text(encoding="utf-8")


def _wall(label: str, t0: float) -> None:
    print(f"WALL {label}: {(time.perf_counter() - t0) * 1000.0:.3f} ms")


# ---------------------------------------------------------------------------
# Time-varying i2c_register_stream
# ---------------------------------------------------------------------------

def test_i2c_register_stream_compiles_and_bytes_change():
    """New track kind compiles; timeline carries >1 distinct register payloads."""
    t0 = time.perf_counter()
    samples = [
        {"t_ms": 0.0, "response": [0x01, 0x02]},
        {"t_ms": 50.0, "response": [0x03, 0x04]},
        {"t_ms": 100.0, "response": [0x05, 0x06]},
    ]
    sc = {
        "schema": SCHEMA,
        "name": "stream-smoke",
        "tracks": [{
            "kind": "i2c_register_stream",
            "bus": "i2c1",
            "address": 0x68,
            "samples": samples,
        }],
    }
    assert validate_scenario(sc) == []
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == [], plan.errors
    joined_setup = "\n".join(plan.setup_lines)
    assert "Mocks.DummyI2CSlave @ i2c1 0x68" in joined_setup
    # first sample seeded at setup
    assert "EnqueueResponseByte 0x1" in joined_setup
    assert "EnqueueResponseByte 0x2" in joined_setup
    # later samples on the timeline
    acts = [ln for _, ln in plan.timeline]
    assert any("EnqueueResponseByte 0x3" in ln for ln in acts)
    assert any("EnqueueResponseByte 0x5" in ln for ln in acts)
    # >1 distinct register values over the timeline (response payloads)
    distinct = {tuple(s["response"]) for s in samples}
    assert len(distinct) > 1
    # also accept samples= form on legacy i2c_registers kind
    sc2 = {
        "schema": SCHEMA,
        "tracks": [{
            "kind": "i2c_registers",
            "address": 0x76,
            "samples": samples,
        }],
    }
    assert validate_scenario(sc2) == []
    plan2 = build_scenario_plan(sc2, STM32F4_CAPS, STM32F4_REPL)
    assert plan2.errors == []
    assert any("EnqueueResponseByte" in ln for _, ln in plan2.timeline)
    # static i2c_registers remains byte-stable (setup only, empty timeline)
    sc3 = {
        "schema": SCHEMA,
        "tracks": [{
            "kind": "i2c_registers",
            "address": 0x50,
            "response": [0xAA, 0xBB],
        }],
    }
    plan3 = build_scenario_plan(sc3, STM32F4_CAPS, STM32F4_REPL)
    assert plan3.errors == []
    assert plan3.timeline == []
    assert "EnqueueResponseByte 0xaa" in "\n".join(plan3.setup_lines)
    _wall("i2c_register_stream", t0)


def test_i2c_register_stream_validation_errors():
    t0 = time.perf_counter()
    bad = {
        "schema": SCHEMA,
        "tracks": [{"kind": "i2c_register_stream", "address": 0x10, "samples": []}],
    }
    errs = validate_scenario(bad)
    assert errs and any("samples" in e for e in errs)
    bad2 = {
        "schema": SCHEMA,
        "tracks": [{
            "kind": "i2c_register_stream",
            "address": 0x10,
            "samples": [{"t_ms": 10, "response": [1]},
                        {"t_ms": 5, "response": [2]}],
        }],
    }
    errs2 = validate_scenario(bad2)
    assert any("non-decreasing" in e for e in errs2)
    _wall("i2c_stream_validate", t0)


# ---------------------------------------------------------------------------
# build_sensor_scenario
# ---------------------------------------------------------------------------

def test_build_sensor_scenario_climb_turn():
    """Canned climb+turn profile → valid fwscenario consumed by the compiler."""
    t0 = time.perf_counter()
    profile = climb_turn_profile(duration_s=2.0, dt_s=0.1)
    sensor_map = [
        {"device": "mpu6050", "bus": "i2c1", "address": 0x68},
        {"device": "bmp280", "bus": "i2c1", "address": 0x76},
        {"device": "lis3mdl", "bus": "i2c1", "address": 0x1C},
        {"device": "pitot", "bus": "i2c1", "address": 0x28},
        {"device": "neo6m", "kind": "uart_rx", "uart": "usart1"},
    ]
    sc = build_sensor_scenario(profile, sensor_map, dt_s=0.1, name="climb_turn")
    assert sc["schema"] == SCHEMA
    assert validate_scenario(sc) == []
    assert len(sc["tracks"]) == 5
    kinds = {tr["kind"] for tr in sc["tracks"]}
    assert "i2c_register_stream" in kinds
    assert "uart_rx" in kinds

    # Moving reality: baro (altitude climb) + mag (heading turn) must change.
    # Steady coordinated turn → IMU body rates/accel are constant (physics); pitot
    # airspeed is constant too — assert those streams are well-formed instead.
    changing = {"bmp280", "lis3mdl"}
    for tr in sc["tracks"]:
        dev = tr.get("device")
        if dev in changing:
            payloads = [tuple(s["response"]) for s in tr["samples"]]
            assert len(payloads) >= 2
            assert len(set(payloads)) > 1, f"{dev} responses did not change"
        elif dev in ("mpu6050", "pitot"):
            payloads = [tuple(s["response"]) for s in tr["samples"]]
            assert len(payloads) >= 2
            # non-empty valid bytes
            assert all(len(p) >= 2 for p in payloads)
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == [], plan.errors
    setup = "\n".join(plan.setup_lines)
    assert setup.count("Mocks.DummyI2CSlave") == 4  # imu/baro/mag/pitot
    assert plan.timeline  # stream updates + uart chars
    # duration meta
    assert sc["meta"]["n_samples"] >= 2
    _wall("bridge_climb_turn", t0)


def test_build_sensor_scenario_from_dict_profile():
    t0 = time.perf_counter()
    profile = {
        "t_s": [0.0, 1.0, 2.0],
        "altitude_m": [100.0, 150.0, 200.0],
        "yaw_rad": [0.0, 0.3, 0.6],
        "vel_ned_m_s": [[20.0, 0.0, -5.0]] * 3,
        "omega_body_rad_s": [[0.0, 0.0, 0.3]] * 3,
        "temperature_c": 20.0,
    }
    sc = build_sensor_scenario(
        profile,
        [{"device": "imu", "address": 0x68}, {"device": "baro", "address": 0x76}],
        dt_s=0.5,
    )
    assert validate_scenario(sc) == []
    assert sc["meta"]["devices"] == ["mpu6050", "bmp280"]
    plan = build_scenario_plan(sc, STM32F4_CAPS, STM32F4_REPL)
    assert plan.errors == []
    _wall("bridge_dict_profile", t0)


def test_build_sensor_scenario_cfd_summary():
    t0 = time.perf_counter()
    cfd = {
        "t_s": [0.0, 0.5, 1.0],
        "airspeed_m_s": [0.0, 15.0, 30.0],
        "altitude_m": [50.0, 50.0, 50.0],
        "heading_rad": 0.0,
    }
    sc = build_sensor_scenario(cfd, [{"device": "pitot"}, {"device": "mpu6050"}], dt_s=0.25)
    assert validate_scenario(sc) == []
    pitot = next(tr for tr in sc["tracks"] if tr.get("device") == "pitot")
    payloads = [tuple(s["response"]) for s in pitot["samples"]]
    assert len(set(payloads)) > 1
    _wall("bridge_cfd", t0)


def test_fw_sensor_scenario_handler():
    t0 = time.perf_counter()
    profile = climb_turn_profile(duration_s=1.0, dt_s=0.2)
    # handler accepts FlightProfile via dict form
    from engine.firmware.sensor_models import FlightProfile
    # pass as plain dict
    d = {
        "t_s": [s.t_s for s in profile.samples],
        "pos_ned_m": [list(s.pos_ned_m) for s in profile.samples],
        "vel_ned_m_s": [list(s.vel_ned_m_s) for s in profile.samples],
        "omega_body_rad_s": [list(s.omega_body_rad_s) for s in profile.samples],
        "quat_body_from_ned": [list(s.quat_body_from_ned) for s in profile.samples],
        "airspeed_m_s": profile.airspeed_m_s,
    }
    res = fw.fw_sensor_scenario({
        "profile": d,
        "sensor_map": [{"device": "mpu6050"}, {"device": "bmp280"}],
        "dt_s": 0.2,
        "name": "handler-smoke",
    })
    assert "code" not in res, res
    assert res["n_tracks"] == 2
    assert validate_scenario(res["scenario"]) == []
    assert res["ms"] >= 0.0
    # missing map → error envelope
    err = fw.fw_sensor_scenario({"profile": d})
    assert err.get("code") == "invalid_request"
    _wall("fw_sensor_scenario_handler", t0)


def test_renode_presence_note():
    """Honest note: Renode is expected for live playback; building needs none."""
    t0 = time.perf_counter()
    renode = shutil.which("renode")
    if renode:
        print(f"RENODE present at {renode} — live fw_sim_run path available")
    else:
        print("RENODE absent — fw_sim_run would honest-skip scenario playback "
              "(scenario build/validate still works; this is not a test failure)")
    # building a scenario never requires Renode
    sc = build_sensor_scenario(
        climb_turn_profile(duration_s=0.5, dt_s=0.1),
        [{"device": "mpu6050"}],
        dt_s=0.1,
    )
    assert validate_scenario(sc) == []
    _wall("renode_note", t0)
