"""Sensor stimulus scenarios — ``aicad.fwscenario/1`` (T-FIN3).

Declarative time-series stimulus tracks bound to emulated peripherals, compiled into
Renode monitor commands spliced into the generated ``.resc`` (see
``emulator._build_resc``). The schema is documented in ``contracts/firmware_rpc.md`` §9
(append-only).

Design posture (same as the rest of the firmware pillar):
- **Pure + honest.** Parsing/validation and .resc-line generation are pure functions,
  fully testable without Renode. Anything a platform cannot stimulate (per the T-FIN2
  capability matrix) is a validation *error* with an explanation — never a silent no-op.
- **Documented Renode idioms only.** Stimulus is built from upstream-documented
  constructs: ``Miscellaneous.Button`` peripherals gated onto GPIO pins
  (``Press``/``Release`` monitor commands), ``Sensors.TMP103`` I2C temperature model
  (``Temperature`` property), ``Mocks.DummyI2CSlave`` (``EnqueueResponseByte``),
  ``Mocks.DummySPISlave`` (fixed-pattern stub), ``sysbus.<uart> WriteChar`` for RX
  injection, and ``Python.PythonPeripheral`` for the memory-mapped ADC stub (no ADC
  peripheral model ships upstream for these families — the stub feeds programmed samples
  through the family's real ADC register window). Renode 1.16.1's PythonPeripheral takes an
  inline ``script:`` (not ``filename:``) with capitalized request properties
  (``IsInit``/``IsRead``/``Offset``/``Value``); the ADC track emits an aux ``.repl`` that
  carries that script and is loaded via ``machine LoadPlatformDescription``.
- Timeline actions are realised as monitor ``sleep`` interleaving (the same wall-clock
  idiom the base .resc already uses for its run window), with ``echo "AICAD_T_MARK
  <ms>"`` markers after every sleep so UART lines can be coarsely time-stamped by the
  parser (enables the ``uart_within:`` assertion form).
- **Time-varying I2C (Wave-3 S1).** Track kind ``i2c_register_stream`` (alias of the
  sample-and-hold stream form of ``i2c_registers``) attaches one ``Mocks.DummyI2CSlave``
  and, at each ``samples[i].t_ms``, re-enqueues that sample's ``response`` byte list so
  a moving sensor value plays back over the emulation. Existing static ``i2c_registers``
  stays byte-stable (setup-only EnqueueResponseByte).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

SCHEMA = "aicad.fwscenario/1"

_PIN_RE = re.compile(r"^P(?P<port>[A-K])(?P<num>\d{1,2})$")

# Memory-mapped ADC stub windows (Python peripheral) per family. Upstream Renode ships
# NO ADC model for any family in the T-FIN2 pack, so the only honest emulated-ADC path
# is a PythonPeripheral mapped over the family's real ADC register block, feeding
# programmed samples through the status/data registers the firmware actually polls.
# Families absent from this map refuse adc tracks with an explanation.
_ADC_STUBS: dict[str, dict] = {
    # STM32F4: ADC1 block at 0x40012000 — SR at +0x00 (EOC bit1), DR at +0x4C.
    "stm32f4": {"base": 0x4001_2000, "size": 0x400, "sr_offset": 0x00,
                "sr_value": 0x3, "dr_offset": 0x4C},
    # STM32F1: ADC1 block at 0x40012400 — SR at +0x00 (EOC bit1), DR at +0x4C.
    "stm32f1": {"base": 0x4001_2400, "size": 0x400, "sr_offset": 0x00,
                "sr_value": 0x3, "dr_offset": 0x4C},
}

_TRACK_KINDS = ("gpio_edges", "button", "quadrature", "i2c_sensor", "i2c_registers",
                "i2c_register_stream", "spi_stub", "uart_rx", "adc_waveform")


@dataclass
class ScenarioPlan:
    """Compiled scenario: everything ``emulator._build_resc`` needs.

    ``setup_lines`` go right after the platform load (peripheral attachment +
    programming); ``timeline`` is a t_ms-sorted list of ``(t_ms, monitor_line)``
    stimulus actions realised via sleep interleaving; ``aux_files`` maps relative
    filename → content (e.g. the ADC stub ``.repl`` with an inline ``script:``) that the
    caller must write next to the .resc; ``duration_ms`` is the minimum run window the
    timeline needs.
    ``errors`` non-empty ⇒ the plan must NOT run (honest refusal).
    """
    setup_lines: list[str] = field(default_factory=list)
    timeline: list[tuple[float, str]] = field(default_factory=list)
    aux_files: dict[str, str] = field(default_factory=dict)
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading / validation
# ---------------------------------------------------------------------------

def load_scenario(raw: Any) -> tuple[Optional[dict], list[str]]:
    """Accept a scenario as dict, JSON string, or fixture reference.

    Fixture references: ``"fixture:<name>"`` or a bare fixture name that exists in
    :mod:`engine.firmware.fixtures`. Returns ``(scenario_dict, errors)`` — exactly one
    of the two is meaningful.
    """
    if raw is None or raw == "":
        return None, []
    if isinstance(raw, dict):
        return raw, validate_scenario(raw)
    if isinstance(raw, str):
        s = raw.strip()
        name = s[len("fixture:"):] if s.startswith("fixture:") else s
        if not s.startswith("{"):
            from engine.firmware import fixtures
            fx = fixtures.get_fixture(name)
            if fx is None:
                return None, [f"unknown scenario fixture '{name}' "
                              f"(available: {', '.join(fixtures.list_fixtures())})"]
            return fx, validate_scenario(fx)
        try:
            obj = json.loads(s)
        except ValueError as e:
            return None, [f"scenario is not valid JSON: {e}"]
        if not isinstance(obj, dict):
            return None, ["scenario JSON must be an object"]
        return obj, validate_scenario(obj)
    return None, [f"scenario must be a dict, JSON string or fixture name, "
                  f"got {type(raw).__name__}"]


def validate_scenario(sc: dict) -> list[str]:
    """Structural validation of an ``aicad.fwscenario/1`` dict. Pure; returns error strings."""
    errors: list[str] = []
    if sc.get("schema") != SCHEMA:
        errors.append(f"schema must be '{SCHEMA}', got {sc.get('schema')!r}")
    tracks = sc.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        errors.append("scenario must carry a non-empty 'tracks' list")
        return errors
    for i, tr in enumerate(tracks):
        tag = f"tracks[{i}]"
        if not isinstance(tr, dict):
            errors.append(f"{tag}: must be an object")
            continue
        kind = tr.get("kind")
        if kind not in _TRACK_KINDS:
            errors.append(f"{tag}: unknown kind {kind!r} (one of {', '.join(_TRACK_KINDS)})")
            continue
        errors.extend(f"{tag}: {e}" for e in _validate_track(tr))
    return errors


def _validate_pin(pin: Any) -> Optional[str]:
    if not isinstance(pin, str) or not _PIN_RE.match(pin):
        return f"pin must be STM32-style 'P<A-K><0-15>', got {pin!r}"
    if int(_PIN_RE.match(pin).group("num")) > 15:
        return f"pin number out of range in {pin!r}"
    return None

def _validate_i2c_samples(samples: Any, kind: str) -> list[str]:
    """Validate a time-varying I2C sample-and-hold list."""
    errs: list[str] = []
    if not isinstance(samples, list) or not samples:
        errs.append(f"{kind} needs a non-empty 'samples' list of "
                    "{{t_ms:number, response:[byte]}}")
        return errs
    prev_t: Optional[float] = None
    for i, s in enumerate(samples):
        if not isinstance(s, dict):
            errs.append(f"{kind} samples[{i}] must be an object")
            continue
        if not isinstance(s.get("t_ms"), (int, float)):
            errs.append(f"{kind} samples[{i}] needs numeric 't_ms'")
        elif prev_t is not None and float(s["t_ms"]) < prev_t:
            errs.append(f"{kind} samples[{i}].t_ms must be non-decreasing")
        if isinstance(s.get("t_ms"), (int, float)):
            prev_t = float(s["t_ms"])
        resp = s.get("response")
        if not isinstance(resp, list) or not resp or \
                not all(isinstance(b, int) and 0 <= b <= 255 for b in resp):
            errs.append(f"{kind} samples[{i}] needs non-empty 'response' byte list (0-255)")
    return errs



def _validate_track(tr: dict) -> list[str]:
    kind = tr["kind"]
    errs: list[str] = []
    if kind == "gpio_edges":
        e = _validate_pin(tr.get("pin"))
        if e:
            errs.append(e)
        evs = tr.get("events")
        if not isinstance(evs, list) or not evs:
            errs.append("gpio_edges needs a non-empty 'events' list of {t_ms, level}")
        else:
            for ev in evs:
                if not isinstance(ev, dict) or not isinstance(ev.get("t_ms"), (int, float)) \
                        or ev.get("level") not in (0, 1):
                    errs.append(f"bad edge event {ev!r} (need {{t_ms:number, level:0|1}})")
    elif kind == "button":
        e = _validate_pin(tr.get("pin"))
        if e:
            errs.append(e)
        presses = tr.get("presses")
        if not isinstance(presses, list) or not presses:
            errs.append("button needs a non-empty 'presses' list of {t_ms, hold_ms?}")
        else:
            for p in presses:
                if not isinstance(p, dict) or not isinstance(p.get("t_ms"), (int, float)):
                    errs.append(f"bad press {p!r} (need {{t_ms:number, hold_ms?:number}})")
    elif kind == "quadrature":
        for key in ("pin_a", "pin_b"):
            e = _validate_pin(tr.get(key))
            if e:
                errs.append(f"{key}: {e}")
        if not isinstance(tr.get("steps"), int) or tr["steps"] < 1:
            errs.append("quadrature needs integer 'steps' >= 1")
        if not isinstance(tr.get("step_ms"), (int, float)) or tr["step_ms"] <= 0:
            errs.append("quadrature needs 'step_ms' > 0")
        if tr.get("direction", "cw") not in ("cw", "ccw"):
            errs.append("quadrature 'direction' must be 'cw' or 'ccw'")
    elif kind == "i2c_sensor":
        if tr.get("model") != "tmp103":
            errs.append(f"i2c_sensor model {tr.get('model')!r} unsupported "
                        "(supported: 'tmp103' — Renode's upstream Sensors.TMP103)")
        if not isinstance(tr.get("address"), int):
            errs.append("i2c_sensor needs an integer 7-bit 'address'")
        if not isinstance(tr.get("temperature_c"), (int, float)):
            errs.append("i2c_sensor(tmp103) needs numeric 'temperature_c'")
    elif kind == "i2c_registers":
        if not isinstance(tr.get("address"), int):
            errs.append("i2c_registers needs an integer 7-bit 'address'")
        # Static form: response:[byte]. Time-varying form (sample-and-hold):
        # samples:[{t_ms, response:[byte]}, ...] — accepted on i2c_registers for
        # forward-compat, or on the explicit i2c_register_stream kind.
        samples = tr.get("samples")
        resp = tr.get("response")
        if samples is not None:
            errs.extend(_validate_i2c_samples(samples, kind))
        elif not isinstance(resp, list) or not resp or \
                not all(isinstance(b, int) and 0 <= b <= 255 for b in resp):
            errs.append("i2c_registers needs a non-empty 'response' byte list (0-255) "
                        "or a non-empty 'samples' list of {t_ms, response}")
    elif kind == "i2c_register_stream":
        if not isinstance(tr.get("address"), int):
            errs.append("i2c_register_stream needs an integer 7-bit 'address'")
        samples = tr.get("samples")
        if samples is None and isinstance(tr.get("response"), list):
            # allow a single static response coerced to one sample at t=0
            pass
        else:
            errs.extend(_validate_i2c_samples(samples, kind))
    elif kind == "spi_stub":
        if not isinstance(tr.get("bus"), str) or not tr["bus"]:
            errs.append("spi_stub needs the platform SPI peripheral name in 'bus' (e.g. 'spi1')")
    elif kind == "uart_rx":
        evs = tr.get("events")
        if not isinstance(evs, list) or not evs:
            errs.append("uart_rx needs a non-empty 'events' list of {t_ms, text}")
        else:
            for ev in evs:
                if not isinstance(ev, dict) or not isinstance(ev.get("t_ms"), (int, float)) \
                        or not isinstance(ev.get("text"), str) or not ev["text"]:
                    errs.append(f"bad uart_rx event {ev!r} (need {{t_ms:number, text:str}})")
    elif kind == "adc_waveform":
        wf = tr.get("waveform")
        if not isinstance(wf, dict):
            errs.append("adc_waveform needs a 'waveform' object")
        else:
            wtype = wf.get("type")
            if wtype in ("sine", "ramp"):
                for key in ("min", "max", "period_ms"):
                    if not isinstance(wf.get(key), (int, float)):
                        errs.append(f"waveform '{wtype}' needs numeric '{key}'")
            elif wtype == "samples":
                vals = wf.get("values")
                if not isinstance(vals, list) or not vals or \
                        not all(isinstance(v, (int, float)) for v in vals):
                    errs.append("waveform 'samples' needs a non-empty numeric 'values' list")
            else:
                errs.append(f"waveform type {wtype!r} unsupported (sine | ramp | samples)")
    return errs


# ---------------------------------------------------------------------------
# Waveform expansion (pure)
# ---------------------------------------------------------------------------

def waveform_samples(wf: dict, sample_ms: float = 1.0, n: int = 256) -> list[int]:
    """Expand a waveform spec into 12-bit ADC counts (clamped 0..4095). Pure."""
    def clamp(v: float) -> int:
        return max(0, min(4095, int(round(v))))

    if wf["type"] == "samples":
        return [clamp(v) for v in wf["values"]]
    lo, hi, period = float(wf["min"]), float(wf["max"]), float(wf["period_ms"])
    count = max(2, min(n, int(round(period / max(sample_ms, 1e-6)))))
    out: list[int] = []
    for i in range(count):
        ph = i / count
        if wf["type"] == "sine":
            v = lo + (hi - lo) * (0.5 + 0.5 * math.sin(2 * math.pi * ph))
        else:  # ramp
            v = lo + (hi - lo) * ph
        out.append(clamp(v))
    return out


def _quadrature_edges(track: dict) -> list[tuple[float, str, int]]:
    """Expand a quadrature track into ``(t_ms, pin, level)`` edges. Pure.

    Standard x4 Gray sequence — CW: (A,B) 00→10→11→01→…, CCW reversed.
    Both pins start low at ``start_ms``; each step advances one quadrature phase.
    """
    phases_cw = [(0, 0), (1, 0), (1, 1), (0, 1)]
    phases = phases_cw if track.get("direction", "cw") == "cw" else list(reversed(phases_cw))
    start = float(track.get("start_ms", 0.0))
    step_ms = float(track["step_ms"])
    pa, pb = track["pin_a"], track["pin_b"]
    edges: list[tuple[float, str, int]] = []
    prev = phases[0]
    for i in range(1, int(track["steps"]) + 1):
        a, b = phases[i % 4]
        t = start + i * step_ms
        if a != prev[0]:
            edges.append((t, pa, a))
        if b != prev[1]:
            edges.append((t, pb, b))
        prev = (a, b)
    return edges


# ---------------------------------------------------------------------------
# Renode compilation
# ---------------------------------------------------------------------------

def _btn_name(pin: str) -> str:
    return f"aicadBtn{pin}"


_ADC_SCRIPT_TEMPLATE = """\
# AI-CAD ADC stub (aicad.fwscenario/1) — Python.PythonPeripheral script.
# Feeds programmed samples through the family's real ADC register window:
# SR read returns the ready flags; each DR read pops the next sample (cycling).
# Renode 1.16.1 request API: IsInit/IsRead/IsWrite/Offset/Value (capitalized).
if request.IsInit:
    samples = {samples}
    idx = 0
elif request.IsRead:
    if request.Offset == {sr_offset:#x}:
        request.Value = {sr_value:#x}
    elif request.Offset == {dr_offset:#x}:
        request.Value = samples[idx % len(samples)]
        idx = idx + 1
    else:
        request.Value = 0
"""


_ADC_REPL_TEMPLATE = """\
aicadAdc: Python.PythonPeripheral @ sysbus {base:#x}
    size: {size:#x}
    initable: true
    script: '''
{script}\
'''
"""


def build_scenario_plan(scenario: dict, caps: dict, platform_text: str) -> ScenarioPlan:
    """Compile a validated scenario into Renode monitor lines + aux files.

    ``caps`` is the T-FIN2 capability entry (``platform_db.get_capabilities``);
    ``platform_text`` is the .repl content (used to check declared peripherals, the
    same honesty check ``_build_resc`` applies to GPIO hooks). Pure — aux files are
    returned as content, not written.
    """
    plan = ScenarioPlan()
    errors = validate_scenario(scenario)
    if errors:
        plan.errors = errors
        return plan

    family = caps.get("family", "?")

    def gpio_port_ok(pin: str) -> bool:
        m = _PIN_RE.match(pin)
        return bool(m) and f"gpioPort{m.group('port')}" in platform_text

    def add_gpio_pin(pin: str) -> bool:
        """Attach a Button stimulus peripheral gated onto the pin (documented idiom:
        Miscellaneous.Button drives its GPIO connection on Press/Release)."""
        if not caps.get("gpio") or not gpio_port_ok(pin):
            plan.errors.append(
                f"gpio stimulus on {pin} unsupported: platform '{caps.get('file')}' "
                f"(family {family}) does not declare gpioPort{pin[1]} — {caps.get('notes', '')}")
            return False
        m = _PIN_RE.match(pin)
        line = (f'machine LoadPlatformDescriptionFromString '
                f'"{_btn_name(pin)}: Miscellaneous.Button @ gpioPort{m.group("port")} '
                f'{int(m.group("num"))} {{ -> gpioPort{m.group("port")}@{int(m.group("num"))} }}"')
        if line not in plan.setup_lines:
            plan.setup_lines.append(line)
        return True

    def edge_line(pin: str, level: int) -> str:
        m = _PIN_RE.match(pin)
        verb = "Press" if level else "Release"
        return f"sysbus.gpioPort{m.group('port')}.{_btn_name(pin)} {verb}"

    i2c_seq = 0
    for tr in scenario["tracks"]:
        kind = tr["kind"]
        if kind == "gpio_edges":
            if add_gpio_pin(tr["pin"]):
                for ev in tr["events"]:
                    plan.timeline.append((float(ev["t_ms"]), edge_line(tr["pin"], ev["level"])))
        elif kind == "button":
            if add_gpio_pin(tr["pin"]):
                for p in tr["presses"]:
                    t = float(p["t_ms"])
                    hold = float(p.get("hold_ms", 50.0))
                    plan.timeline.append((t, edge_line(tr["pin"], 1)))
                    plan.timeline.append((t + hold, edge_line(tr["pin"], 0)))
        elif kind == "quadrature":
            if add_gpio_pin(tr["pin_a"]) and add_gpio_pin(tr["pin_b"]):
                for (t, pin, level) in _quadrature_edges(tr):
                    plan.timeline.append((t, edge_line(pin, level)))
        elif kind in ("i2c_sensor", "i2c_registers", "i2c_register_stream"):
            bus = tr.get("bus", "i2c1")
            if not caps.get("i2c") or f"{bus}:" not in platform_text:
                plan.errors.append(
                    f"i2c stimulus unsupported: platform '{caps.get('file')}' (family "
                    f"{family}) does not declare I2C bus '{bus}' — {caps.get('notes', '')}")
                continue
            addr = int(tr["address"])
            if kind == "i2c_sensor":  # upstream Sensors.TMP103 temperature model
                name = f"aicadTemp{i2c_seq}"
                plan.setup_lines.append(
                    f'machine LoadPlatformDescriptionFromString '
                    f'"{name}: Sensors.TMP103 @ {bus} {addr:#x}"')
                plan.setup_lines.append(
                    f"sysbus.{bus}.{name} Temperature {float(tr['temperature_c']):g}")
            else:
                # Static i2c_registers OR time-varying stream (i2c_register_stream /
                # i2c_registers with samples[]): Mocks.DummyI2CSlave + EnqueueResponseByte.
                # Sample-and-hold: at each samples[i].t_ms re-enqueue that response so a
                # moving sensor value plays back. DummyI2CSlave has no documented
                # ClearResponse API, so each sample appends a fresh frame; firmware that
                # reads once per sample window sees the newest enqueued bytes first when
                # the queue is consumed per transaction (Renode drains one response
                # sequence per master read). We also emit an echo mark for traceability.
                name = f"aicadI2C{i2c_seq}"
                plan.setup_lines.append(
                    f'machine LoadPlatformDescriptionFromString '
                    f'"{name}: Mocks.DummyI2CSlave @ {bus} {addr:#x}"')
                samples = tr.get("samples")
                if samples is None and kind == "i2c_register_stream" and \
                        isinstance(tr.get("response"), list):
                    samples = [{"t_ms": 0.0, "response": tr["response"]}]
                if samples:
                    # Seed with the first sample at setup so a read before t0 still works.
                    first = samples[0]["response"]
                    for b in first:
                        plan.setup_lines.append(
                            f"sysbus.{bus}.{name} EnqueueResponseByte {int(b):#x}")
                    # Subsequent samples (including re-asserting sample 0 at its t_ms)
                    # go on the timeline. For sample 0 with t_ms>0 the setup seed holds
                    # until then; for t_ms==0 we skip the duplicate timeline enqueue.
                    for si, s in enumerate(samples):
                        t = float(s["t_ms"])
                        if si == 0 and t <= 0.0:
                            continue
                        # Mark + re-enqueue full response (sample-and-hold update).
                        plan.timeline.append(
                            (t, f'echo "AICAD_I2C_SAMPLE {bus} {addr:#x} t_ms={t:g}"'))
                        for b in s["response"]:
                            plan.timeline.append(
                                (t, f"sysbus.{bus}.{name} EnqueueResponseByte {int(b):#x}"))
                else:
                    for b in tr["response"]:
                        plan.setup_lines.append(
                            f"sysbus.{bus}.{name} EnqueueResponseByte {int(b):#x}")
            i2c_seq += 1
        elif kind == "spi_stub":
            bus = tr["bus"]
            if not caps.get("spi") or f"{bus}:" not in platform_text:
                plan.errors.append(
                    f"spi stimulus unsupported: platform '{caps.get('file')}' (family "
                    f"{family}) does not declare SPI bus '{bus}' — {caps.get('notes', '')}")
                continue
            name = tr.get("name", "aicadSpiStub")
            plan.setup_lines.append(
                f'machine LoadPlatformDescriptionFromString '
                f'"{name}: Mocks.DummySPISlave @ {bus}"')
            plan.warnings.append(
                f"spi_stub '{name}': DummySPISlave answers every frame with a fixed "
                "pattern (connectivity stub) — programmable SPI register models have no "
                "documented upstream Renode idiom yet")
        elif kind == "uart_rx":
            uart = tr.get("uart")
            if uart is None:
                m = re.search(r"^(?P<name>\w+)\s*:\s*UART\.", platform_text, re.MULTILINE)
                uart = m.group("name") if m else None
            if not uart or not caps.get("uart"):
                plan.errors.append(
                    f"uart_rx stimulus unsupported: platform '{caps.get('file')}' "
                    f"(family {family}) declares no UART — {caps.get('notes', '')}")
                continue
            for ev in tr["events"]:
                t = float(ev["t_ms"])
                for ch in ev["text"].encode("utf-8"):
                    plan.timeline.append((t, f"sysbus.{uart} WriteChar {ch:#x}"))
        elif kind == "adc_waveform":
            stub = _ADC_STUBS.get(family)
            if stub is None:
                plan.errors.append(
                    f"adc stimulus unsupported on family '{family}': upstream Renode "
                    "ships no ADC model for these parts and no Python-peripheral ADC "
                    "stub window is registered for this family "
                    f"(registered: {', '.join(sorted(_ADC_STUBS))})")
                continue
            samples = waveform_samples(tr["waveform"], float(tr.get("sample_ms", 1.0)))
            script_body = _ADC_SCRIPT_TEMPLATE.format(
                samples=samples, sr_offset=stub["sr_offset"], sr_value=stub["sr_value"],
                dr_offset=stub["dr_offset"])
            # Renode 1.16.1: PythonPeripheral takes inline script: (filename: + @path is
            # E00). Emit an aux .repl so the triple-quoted script survives intact; the
            # single-line LoadPlatformDescriptionFromString path cannot carry it.
            repl_name = "aicad_adc_stub.repl"
            plan.aux_files[repl_name] = _ADC_REPL_TEMPLATE.format(
                base=stub["base"], size=stub["size"], script=script_body)
            plan.setup_lines.append(
                f"machine LoadPlatformDescription "
                f"@{{AICAD_SCENARIO_DIR}}/{repl_name}")

    plan.timeline.sort(key=lambda item: item[0])
    if plan.timeline:
        plan.duration_ms = plan.timeline[-1][0]
    return plan


def timeline_resc_lines(timeline: list[tuple[float, str]], run_seconds: float) -> list[str]:
    """Render a t_ms-sorted timeline into monitor sleep/action lines covering
    ``run_seconds``. Emits ``echo "AICAD_T_MARK <ms>"`` after every sleep so the
    parser can coarsely time-stamp subsequent UART output. Pure."""
    lines: list[str] = ['echo "AICAD_T_MARK 0"']
    now_ms = 0.0
    for t_ms, action in timeline:
        dt = max(0.0, t_ms - now_ms)
        if dt > 0:
            lines.append(f"sleep {dt / 1000.0:.3f}")
            now_ms = t_ms
            lines.append(f'echo "AICAD_T_MARK {now_ms:g}"')
        lines.append(action)
    remaining = run_seconds * 1000.0 - now_ms
    if remaining > 0:
        lines.append(f"sleep {remaining / 1000.0:.3f}")
        lines.append(f'echo "AICAD_T_MARK {run_seconds * 1000.0:g}"')
    return lines


def materialize_aux_files(plan: ScenarioPlan, directory: Path) -> None:
    """Write the plan's aux files into ``directory`` and substitute the
    ``{AICAD_SCENARIO_DIR}`` placeholder in setup lines with its absolute path."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in plan.aux_files.items():
        (directory / name).write_text(content, encoding="utf-8")
    plan.setup_lines = [ln.replace("{AICAD_SCENARIO_DIR}", str(directory))
                        for ln in plan.setup_lines]
