"""Power/duty-cycle profile tests (T-FIN7) — ``aicad.powerprofile/1``.

Canned traces only (Renode absent on the dev box, per the T-FIN1/T-FIN3 precedent):
residency/duty heuristics are pure functions; the board half uses seeded footprints
with T-FIN8 ``eng_overrides`` rail metadata.

Run:  pytest engine_oracle/firmware/test_powerprofile.py
"""

from __future__ import annotations

import json

import pytest

from engine.ecad import schema
from engine.firmware import mcp_tools as fw
from engine.firmware import powerprofile as pp


@pytest.fixture(autouse=True)
def _isolated_firmware_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_FIRMWARE_DIR", str(tmp_path / "fw"))


# ── fixtures ────────────────────────────────────────────────────────────────────

def _uart(seq: int, text: str, t_ms=None) -> dict:
    return {"type": "uart", "seq": seq, "t_ms": t_ms, "text": text}


def _gpio(seq: int, pin: str, level: int, t_ms=None) -> dict:
    return {"type": "gpio", "seq": seq, "t_ms": t_ms, "pin": pin, "level": level}


def _trace(events: list[dict], gpio_final: dict | None = None) -> dict:
    return {"schema": "aicad.firmware.trace/1", "source": "renode",
            "generated_at": 0.0, "events": events, "gpio_final": gpio_final or {}}


def _pad(num: str, net: str) -> dict:
    return {"id": f"p{num}", "number": num, "pos": [0.0, float(num)], "angle": 0.0,
            "shape": "rect", "size": [0.5, 0.5], "type": "smd",
            "layers": ["F.Cu"], "drill": None, "net": net, "pin": num}


def _fp(fid: str, ref: str, value: str, fpid: str, pad_nets: list[str],
        rails: list[dict] | None = None) -> dict:
    d = {"id": fid, "fpid": fpid, "ref": ref, "value": value, "pos": [10.0, 10.0],
         "angle": 0.0, "side": "top", "attr": "smd", "fields": {},
         "pads": [_pad(str(i + 1), n) for i, n in enumerate(pad_nets)],
         "graphics": [], "model_3d": None}
    if rails is not None:
        d["eng_overrides"] = {"supply": {"rails": rails}}
    return d


def _board() -> dict:
    """STM32 board: MCU (VDD, run/sleep currents), LED load on PA5 (sleep unknown),
    and a sensor with UNKNOWN active current (must land in coverage, not the sums)."""
    b = schema.new_board("t_fin7")
    for n in ("PA5", "VDD", "NRST"):
        b["nets"].append({"name": n, "class": "default"})
    b["footprints"] = [
        _fp("fp1", "U1", "STM32F407", "Package_QFP:LQFP-64", ["PA5", "VDD", "GND"],
            rails=[{"name": "VDD", "voltage_v_nom": 3.3,
                    "current_a_active": 0.05, "current_a_sleep": 0.001}]),
        _fp("fp2", "D1", "LED", "LED_SMD:LED_0603", ["PA5", "GND"],
            rails=[{"name": "VDD", "voltage_v_nom": 3.3,
                    "current_a_active": 0.02, "current_a_sleep": None}]),
        _fp("fp3", "U2", "SENSOR", "Sensor:SOT-23", ["VDD", "GND"],
            rails=[{"name": "VDD", "current_a_active": None,
                    "current_a_sleep": None}]),
    ]
    return b


# CPU 50 % run (sleep at 500, run mark at 1000 closes the window),
# PA5 50 % duty (high 0-250 and 500-750), rising edges at 0 and 500 → 0.5 s period.
_EVENTS = [
    _gpio(0, "PA5", 1, 0.0), _gpio(1, "PA5", 0, 250.0),
    _uart(2, "AICAD_CPU_EVT sleep t=500ms", 500.0),
    _gpio(3, "PA5", 1, 500.0), _gpio(4, "PA5", 0, 750.0),
    _uart(5, "AICAD_CPU_EVT run t=1000ms", 1000.0),
]


# ── residency / duty heuristics ─────────────────────────────────────────────────

def test_cpu_residency_from_markers():
    gpio, uart = pp.split_events(_trace(_EVENTS))
    window = pp.run_window_ms(gpio, uart)
    assert window == 1000.0
    cpu = pp.cpu_residency(uart, window)
    assert cpu["source"] == "cpu_markers"
    assert cpu["active_frac"] == pytest.approx(0.5)
    assert cpu["sleep_frac"] == pytest.approx(0.5)
    assert cpu["active_s"] == pytest.approx(0.5)


def test_cpu_residency_assumed_active_without_markers():
    cpu = pp.cpu_residency([_uart(0, "hello", 10.0)], 1000.0)
    assert cpu["source"] == "assumed_active"
    assert cpu["active_frac"] == 1.0
    assert "AICAD_CPU_TRACE" in cpu["note"]  # tells the user how to instrument


def test_pin_duty_stamped_events():
    gpio, _ = pp.split_events(_trace(_EVENTS))
    rec = pp.pin_duty(gpio, "PA5", 1000.0)
    assert rec["source"] == "stamped_events"
    assert rec["duty"] == pytest.approx(0.5)
    assert rec["period_s"] == pytest.approx(0.5)
    assert rec["toggles"] == 3


def test_pin_duty_unstamped_is_honest_null():
    evs = [_gpio(0, "PB1", 1), _gpio(1, "PB1", 0), _gpio(2, "PB1", 1)]
    rec = pp.pin_duty(evs, "PB1", 1000.0)
    assert rec["source"] == "unstamped_events"
    assert rec["duty"] is None
    assert rec["toggles"] == 2


def test_pin_duty_final_state_only():
    rec = pp.pin_duty([], "PC3", 1000.0, gpio_final={"PC3": 1})
    assert rec["source"] == "final_state_only"
    assert rec["duty"] == 1.0


# ── profile assembly ────────────────────────────────────────────────────────────

def test_profile_without_board_is_activity_only():
    prof = pp.build_power_profile(_trace(_EVENTS), trace_ref="/tmp/x.trace.json")
    assert prof["schema"] == pp.SCHEMA
    assert "not a SPICE" in prof["model"] or "NOT a SPICE" in prof["model"]
    assert prof["rails"] == []
    assert prof["cpu"]["active_frac"] == pytest.approx(0.5)
    assert prof["pins"]["PA5"]["duty"] == pytest.approx(0.5)
    assert any("no board metadata" in n for n in prof["coverage"]["notes"])


def test_profile_with_board_rail_math():
    prof = pp.build_power_profile(_trace(_EVENTS), board=_board(),
                                  mcu_family="cortex-m4")
    assert len(prof["rails"]) == 1
    rail = prof["rails"][0]
    assert rail["name"] == "VDD"
    assert rail["voltage_v_nom"] == pytest.approx(3.3)
    # MCU: 0.5*0.05 + 0.5*0.001 = 0.0255 ; LED (duty 0.5, sleep unknown → floor): 0.01
    assert rail["i_avg_a"] == pytest.approx(0.0355)
    assert rail["i_peak_a"] == pytest.approx(0.07)
    # T-FIN18 direct schedule shape
    assert rail["schedule"] == {"duty": pytest.approx(0.5), "period_s": pytest.approx(0.5)}
    assert rail["residency"]["active_frac"] == pytest.approx(0.5)
    assert rail["residency"]["active_s"] == pytest.approx(0.5)
    # honesty: unknown active current never becomes 0 — it's a coverage gap
    assert "U2:VDD" in prof["coverage"]["unknown_active_current"]
    prov = "\n".join(rail["provenance"])
    assert "cpu_residency:cpu_markers" in prov
    assert "gpio_duty:PA5" in prov
    assert "sleep current unknown" in prov


def test_profile_board_without_rail_metadata_notes_gap():
    b = schema.new_board("bare")
    b["footprints"] = [_fp("fp1", "U1", "STM32F407", "MCU:LQFP48", ["VDD", "GND"])]
    prof = pp.build_power_profile(_trace(_EVENTS), board=b)
    assert prof["rails"] == []
    assert any("eng.supply.rails" in n for n in prof["coverage"]["notes"])


def test_no_time_axis_degrades_honestly():
    evs = [_gpio(0, "PA5", 1), _gpio(1, "PA5", 0)]
    prof = pp.build_power_profile(_trace(evs), board=_board(), mcu_family="cortex-m4")
    assert prof["window_ms"] is None
    assert prof["pins"]["PA5"]["duty"] is None
    rail = prof["rails"][0]
    # unknown duty ⇒ assumed always active (conservative): 0.05 + 0.02
    assert rail["i_avg_a"] == pytest.approx(0.07)
    assert any("no time-stamped events" in n for n in prof["coverage"]["notes"])


# ── artifact + handlers ─────────────────────────────────────────────────────────

def test_write_profile_artifact_next_to_elf(tmp_path):
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"\x7fELF")
    prof = pp.build_power_profile(_trace(_EVENTS))
    out = pp.write_profile_artifact(prof, str(elf))
    assert out == str(tmp_path / "firmware.elf.power.json")
    data = json.loads((tmp_path / "firmware.elf.power.json").read_text())
    assert data["schema"] == pp.SCHEMA


def test_write_profile_artifact_missing_elf_is_none(tmp_path):
    assert pp.write_profile_artifact({}, str(tmp_path / "nope.elf")) is None


def test_fw_power_profile_requires_trace_path():
    res = fw.fw_power_profile({})
    assert res["code"] == "invalid_request"


def test_fw_power_profile_unreadable_trace(tmp_path):
    res = fw.fw_power_profile({"trace_path": str(tmp_path / "missing.json")})
    assert res["code"] == "invalid_trace"


def test_fw_power_profile_over_canned_trace_with_board_file(tmp_path):
    trace_p = tmp_path / "fw.elf.trace.json"
    trace_p.write_text(json.dumps(_trace(_EVENTS)), encoding="utf-8")
    board_p = tmp_path / "board.json"
    board_p.write_text(json.dumps(_board()), encoding="utf-8")
    res = fw.fw_power_profile({"trace_path": str(trace_p),
                               "board_doc_ref": str(board_p)})
    assert res["schema"] == pp.SCHEMA
    assert res["trace_ref"] == str(trace_p)
    assert res["rails"][0]["i_avg_a"] == pytest.approx(0.0355)


def test_fw_sim_run_emits_power_profile_for_board_linked_project(tmp_path):
    """Mocked run_simulation whose trace_path exists + board-linked project ⇒
    the SimResult dict gains power_profile_path and the artifact is on disk."""
    from unittest.mock import patch
    from engine.ecad import session as ecad_session
    from engine.firmware.emulator import SimResult
    import engine.firmware.project as fw_proj

    doc_id = ecad_session.open_session(_board()).doc_id
    proj = fw.fw_new_project({"mcu_family": "cortex-m4", "board_doc_ref": doc_id})
    pid = proj["project_id"]
    loaded = fw_proj.load(pid)
    elf = loaded.root_dir / "build" / "firmware.elf"
    elf.parent.mkdir(parents=True, exist_ok=True)
    elf.write_bytes(b"\x7fELF")
    trace_p = elf.parent / (elf.name + ".trace.json")
    trace_p.write_text(json.dumps(_trace(_EVENTS)), encoding="utf-8")

    mock = SimResult(passed=True, uart_log="ok", trace_path=str(trace_p))
    with patch("engine.firmware.mcp_tools.run_simulation", return_value=mock):
        res = fw.fw_sim_run({"project_id": pid, "elf_path": str(elf)})

    assert res.get("power_profile_path") == str(elf) + ".power.json"
    data = json.loads((elf.parent / "firmware.elf.power.json").read_text())
    assert data["schema"] == pp.SCHEMA
    assert data["rails"][0]["name"] == "VDD"


def test_fw_sim_run_no_board_link_no_profile(tmp_path):
    from unittest.mock import patch
    from engine.firmware.emulator import SimResult
    import engine.firmware.project as fw_proj

    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    pid = proj["project_id"]
    loaded = fw_proj.load(pid)
    elf = loaded.root_dir / "build" / "firmware.elf"
    elf.parent.mkdir(parents=True, exist_ok=True)
    elf.write_bytes(b"\x7fELF")

    mock = SimResult(passed=True, uart_log="ok", trace_path=str(elf) + ".trace.json")
    with patch("engine.firmware.mcp_tools.run_simulation", return_value=mock):
        res = fw.fw_sim_run({"project_id": pid, "elf_path": str(elf)})
    assert "power_profile_path" not in res
