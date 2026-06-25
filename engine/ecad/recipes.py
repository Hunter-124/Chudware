"""Parameterized known-good ECAD designs -- the orchestrator's few-shot library + fallback.

Each recipe returns a STRUCTURED design dict (not ops, not a netlist) that the orchestrator
materializes deterministically::

    {
      "name":       str,                                  # short label
      "request":    str,                                  # the canonical request this answers
      "components": [{"ref","libid","value", ...}],        # real libids (resolve in the library)
      "nets":       {netname: [{"ref","pin"}]},            # by-NAME connectivity (robust)
      "study":      {"analysis", "params", "expect":{"probe","value","tol"}},
      "notes":      str,
    }

These serve THREE purposes at once:

  1. **Few-shot examples** for the planner prompt -- a weak local model is shown a real, valid
     design (real libids, real nets, a sane study) so its own plan lands in-distribution.
  2. **The validation target shape** -- :func:`validate_design` checks a model-produced plan against
     exactly the structure these emit, so a recipe and a model plan are interchangeable downstream.
  3. **The deterministic fallback** -- when the model can't produce a valid plan after bounded
     retries, the orchestrator picks the closest recipe so the run ALWAYS yields a real schematic.

Connectivity is expressed by NET NAME (not geometric wires): each net lists the (ref, pin) members
that share it. The orchestrator realizes a net by dropping a global LABEL on each member pin's world
coordinate (and a power port for GND/supply rails) -- robust by-name nets that the netlist builder
unions deterministically, exactly as KiCad does. No fragile autorouting.

Pin references use the symbol's pin NUMBER as it appears in the starter library (Device:R -> "1"/"2";
Simulation_SPICE:V_source -> "+"/"-"; OPAMP_dual -> "1"=IN-, "2"=IN+, "3"=OUT). These are validated
against the resolved SymbolDef so a typo is caught before BUILD.

All values are honest closed-form designs:
  * voltage_divider(vin, vout): R_top/R_bot picked from vout = vin * Rb/(Rt+Rb).
  * rc_lowpass(fc):             R fixed, C = 1/(2*pi*fc*R); expect -3 dB at fc.
  * inverting_amp(gain):        Rf = |gain| * Rin; expect Vout = -gain * Vin (op-amp subckt).
  * rc_555_astable(freq):       standard 555 astable Ra/Rb/C for ~freq (50% approx).
"""

from __future__ import annotations

import math


# --------------------------------------------------------------------------- value formatting

def _fmt_ohms(r: float) -> str:
    """Format a resistance as a compact engineering string (e.g. 10000 -> '10k')."""
    if r >= 1e6:
        return _trim(r / 1e6) + "Meg"
    if r >= 1e3:
        return _trim(r / 1e3) + "k"
    return _trim(r)


def _fmt_farads(c: float) -> str:
    """Format a capacitance as a compact engineering string (e.g. 1e-7 -> '100n')."""
    if c >= 1e-3:
        return _trim(c / 1e-3) + "m"
    if c >= 1e-6:
        return _trim(c / 1e-6) + "u"
    if c >= 1e-9:
        return _trim(c / 1e-9) + "n"
    return _trim(c / 1e-12) + "p"


def _trim(x: float) -> str:
    """Compact numeric string: drop a trailing .0 but keep real fractions."""
    s = f"{x:.4g}"
    return s


# --------------------------------------------------------------------------- recipes


def voltage_divider(vin: float = 5.0, vout: float = 2.5) -> dict:
    """A resistive divider: V1 (vin) across R1 (top) + R2 (bottom); VMID = vin * R2/(R1+R2).

    R1 + R2 fixed at 20k total; R2 solved from the target ratio. Expect V(VMID) == vout.
    """
    vin = float(vin)
    vout = max(min(float(vout), vin * 0.999), vin * 0.001) if vin else 0.0
    rtotal = 20_000.0
    ratio = (vout / vin) if vin else 0.5
    r2 = rtotal * ratio          # bottom (across the output)
    r1 = rtotal - r2             # top
    return {
        "name": "voltage_divider",
        "request": f"a {_trim(vin)}V resistive divider with {_trim(vout)}V output",
        "components": [
            {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": _trim(vin)},
            {"ref": "R1", "libid": "Device:R", "value": _fmt_ohms(r1)},
            {"ref": "R2", "libid": "Device:R", "value": _fmt_ohms(r2)},
        ],
        "nets": {
            "IN":  [{"ref": "V1", "pin": "+"}, {"ref": "R1", "pin": "1"}],
            "VMID": [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            "GND": [{"ref": "V1", "pin": "-"}, {"ref": "R2", "pin": "2"}],
        },
        "study": {
            "analysis": "op",
            "params": {},
            "expect": {"probe": "V(VMID)", "value": vout, "tol": 0.02},
        },
        "notes": (f"R1={_fmt_ohms(r1)} (top), R2={_fmt_ohms(r2)} (bottom); "
                  f"VMID = {_trim(vin)} * R2/(R1+R2) = {_trim(vout)} V."),
    }


def rc_lowpass(fc: float = 1000.0) -> dict:
    """A first-order RC low-pass: V1 -> R -> OUT, C from OUT to GND. -3 dB corner at fc.

    R fixed at 1k; C = 1/(2*pi*fc*R). The study is an AC sweep; expect |H(fc)| == 1/sqrt(2).
    """
    fc = float(fc)
    r = 1000.0
    c = 1.0 / (2.0 * math.pi * fc * r) if fc > 0 else 1e-7
    return {
        "name": "rc_lowpass",
        "request": f"an RC low-pass filter with a {_trim(fc)}Hz cutoff",
        "components": [
            {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": "0",
             "ac_mag": 1.0},
            {"ref": "R1", "libid": "Device:R", "value": _fmt_ohms(r)},
            {"ref": "C1", "libid": "Device:C", "value": _fmt_farads(c)},
        ],
        "nets": {
            "IN":  [{"ref": "V1", "pin": "+"}, {"ref": "R1", "pin": "1"}],
            "OUT": [{"ref": "R1", "pin": "2"}, {"ref": "C1", "pin": "1"}],
            "GND": [{"ref": "V1", "pin": "-"}, {"ref": "C1", "pin": "2"}],
        },
        "study": {
            "analysis": "ac",
            "params": {"sweep": "dec", "points": 20,
                       "fstart": max(fc / 100.0, 0.1), "fstop": fc * 100.0,
                       "ac_source": "V1", "ac_mag": 1.0},
            "expect": {"probe": "V(OUT)", "value": 1.0 / math.sqrt(2.0),
                       "tol": 0.02, "at_freq": fc},
        },
        "notes": (f"R={_fmt_ohms(r)}, C={_fmt_farads(c)}; "
                  f"fc = 1/(2*pi*R*C) = {_trim(fc)} Hz, |H(fc)| = 0.707 (-3 dB)."),
    }


def inverting_amp(gain: float = 10.0) -> dict:
    """An inverting op-amp: Vin -> Rin -> (-); Rf from (-) to OUT; (+) to GND. Vout = -gain*Vin.

    Rin fixed at 1k, Rf = |gain| * Rin. Uses the dual op-amp symbol (its OPAMP sim subckt). The
    op-amp pins are referenced by NUMBER: "1"=IN-, "2"=IN+, "3"=OUT (unit 1).
    """
    gain = abs(float(gain))
    rin = 1000.0
    rf = gain * rin
    vin = 0.2
    vout = -gain * vin
    return {
        "name": "inverting_amp",
        "request": f"an inverting amplifier with a gain of {_trim(gain)}",
        "components": [
            {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": _trim(vin)},
            {"ref": "Rin", "libid": "Device:R", "value": _fmt_ohms(rin)},
            {"ref": "Rf", "libid": "Device:R", "value": _fmt_ohms(rf)},
            {"ref": "U1", "libid": "Amplifier_Operational:OPAMP_dual", "value": "OPAMP"},
        ],
        "nets": {
            "IN":  [{"ref": "V1", "pin": "+"}, {"ref": "Rin", "pin": "1"}],
            "INV": [{"ref": "Rin", "pin": "2"}, {"ref": "Rf", "pin": "1"},
                    {"ref": "U1", "pin": "1"}],          # op-amp IN- (pin number 1)
            "OUT": [{"ref": "Rf", "pin": "2"}, {"ref": "U1", "pin": "3"}],  # op-amp OUT
            "GND": [{"ref": "V1", "pin": "-"}, {"ref": "U1", "pin": "2"}],  # op-amp IN+ to GND
        },
        "study": {
            "analysis": "op",
            "params": {},
            "expect": {"probe": "V(OUT)", "value": vout, "tol": 0.02},
        },
        "notes": (f"Rin={_fmt_ohms(rin)}, Rf={_fmt_ohms(rf)}; gain = -Rf/Rin = -{_trim(gain)}; "
                  f"Vin={_trim(vin)} V => Vout={_trim(vout)} V."),
    }


def led_driver(vsupply: float = 3.3, vf: float = 1.8, current: float = 0.01) -> dict:
    """A series current-limited LED driver: V1 (vsupply) -> R (limit) -> LED -> GND.

    The current-limit resistor is the textbook ``R = (Vsupply - Vf) / I`` for the requested LED
    forward voltage ``vf`` and target current ``current`` (default 10 mA). R is rounded to a sane
    value so the BOM is real.

    Simulation note: the starter LED uses the generic Shockley diode model (IS=1e-14), whose forward
    drop is ~0.73 V at these currents — lower than a real LED's ~1.8 V — so the self-check expects
    the MODEL forward voltage V(LED_A) (a stable, model-determined value), not the design ``vf``.
    The DESIGNED resistor still uses the real ``vf`` so the schematic/BOM is correct for a physical
    LED, while the op-point self-check honestly confirms the LED conducts.
    """
    vsupply = float(vsupply)
    vf = max(min(float(vf), vsupply * 0.95), 0.0)
    current = float(current) if current and float(current) > 0 else 0.01
    r = (vsupply - vf) / current
    # round to a tidy resistor value (no smaller than 10 ohm so the LED is always current-limited)
    r = max(round(r / 10.0) * 10.0, 10.0)
    return {
        "name": "led_driver",
        "request": f"a {_trim(vsupply)}V LED driver (series current-limit resistor)",
        "components": [
            {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": _trim(vsupply)},
            {"ref": "R1", "libid": "Device:R", "value": _fmt_ohms(r)},
            {"ref": "D1", "libid": "Device:LED", "value": "LED"},
        ],
        "nets": {
            # V1+ -> VCC -> R1.1 ; R1.2 -> LED anode ; LED cathode -> GND ; V1- -> GND.
            "VCC":   [{"ref": "V1", "pin": "+"}, {"ref": "R1", "pin": "1"}],
            "LED_A": [{"ref": "R1", "pin": "2"}, {"ref": "D1", "pin": "A"}],
            "GND":   [{"ref": "D1", "pin": "K"}, {"ref": "V1", "pin": "-"}],
        },
        "study": {
            "analysis": "op",
            "params": {},
            # Expect the LED to conduct at its (model) forward voltage. Generous tol because the
            # generic diode model's drop varies with current; this confirms the LED is forward-biased.
            "expect": {"probe": "V(LED_A)", "value": 0.73, "tol": 0.25},
        },
        "notes": (f"R1={_fmt_ohms(r)} = (Vsupply-Vf)/I = ({_trim(vsupply)}-{_trim(vf)})/"
                  f"{_trim(current)} ; LED current ~= {_trim(current*1000)} mA. "
                  f"V1={_trim(vsupply)}V -> R1 -> LED -> GND."),
    }


def rc_555_astable(freq: float = 1.0) -> dict:
    """A 555 astable oscillator at ~freq Hz (standard Ra/Rb/C topology).

    f ~ 1.44 / ((Ra + 2*Rb) * C). C fixed at 1uF; Ra=Rb solved for the target frequency. The
    expectation is RECORDED (oscillator period) but the behavioural 555 subckt may or may not
    free-run in this MNA engine, so the orchestrator reports the discrepancy rather than looping.
    """
    freq = float(freq)
    c = 1e-6
    # f = 1.44 / ((Ra + 2 Rb) * C); pick Ra = Rb = R, so f = 1.44 / (3 R C).
    r = 1.44 / (3.0 * freq * c) if freq > 0 else 1e5
    period = 1.0 / freq if freq > 0 else 1.0
    return {
        "name": "rc_555_astable",
        "request": f"a 555 astable oscillator at about {_trim(freq)}Hz",
        "components": [
            {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": "5"},
            {"ref": "U1", "libid": "Timer:NE555", "value": "NE555"},
            {"ref": "Ra", "libid": "Device:R", "value": _fmt_ohms(r)},
            {"ref": "Rb", "libid": "Device:R", "value": _fmt_ohms(r)},
            {"ref": "C1", "libid": "Device:C", "value": _fmt_farads(c)},
        ],
        "nets": {
            # VCC rail: V1+, 555 VCC (pin 8) and RST (pin 4) tied high, Ra top.
            "VCC": [{"ref": "V1", "pin": "+"}, {"ref": "U1", "pin": "8"},
                    {"ref": "U1", "pin": "4"}, {"ref": "Ra", "pin": "1"}],
            # Ra -> Rb junction = DISCHARGE (pin 7).
            "DISCH": [{"ref": "Ra", "pin": "2"}, {"ref": "Rb", "pin": "1"},
                      {"ref": "U1", "pin": "7"}],
            # Rb -> C top = THRESHOLD (pin 6) tied to TRIGGER (pin 2).
            "THR": [{"ref": "Rb", "pin": "2"}, {"ref": "C1", "pin": "1"},
                    {"ref": "U1", "pin": "6"}, {"ref": "U1", "pin": "2"}],
            "OUT": [{"ref": "U1", "pin": "3"}],
            "GND": [{"ref": "V1", "pin": "-"}, {"ref": "U1", "pin": "1"},
                    {"ref": "C1", "pin": "2"}],
        },
        "study": {
            "analysis": "tran",
            "params": {"tstep": period / 200.0, "tstop": period * 3.0, "uic": True},
            "expect": {"probe": "V(OUT)", "value": period, "tol": 0.25, "kind": "period"},
        },
        "notes": (f"Ra=Rb={_fmt_ohms(r)}, C={_fmt_farads(c)}; "
                  f"f ~ 1.44/((Ra+2Rb)*C) = {_trim(freq)} Hz, period {_trim(period)} s."),
    }


# --------------------------------------------------------------------------- registry / selection


# (name, builder, list of trigger keywords) -- order matters for "closest" selection.
RECIPES: list[tuple[str, "callable", tuple[str, ...]]] = [
    ("led_driver", led_driver,
     ("led", "led driver", "drive an led", "indicator", "current limit",
      "current-limit", "current limiting")),
    ("inverting_amp", inverting_amp,
     ("inverting", "invert", "amplifier", "amp", "op-amp", "opamp", "gain")),
    ("rc_555_astable", rc_555_astable,
     ("555", "astable", "oscillator", "timer", "blink", "square wave", "monostable")),
    ("rc_lowpass", rc_lowpass,
     ("low-pass", "lowpass", "low pass", "filter", "rc", "cutoff", "corner")),
    ("voltage_divider", voltage_divider,
     ("divider", "voltage divider", "resistive", "divide", "potential")),
]


def all_recipes() -> list[dict]:
    """Materialize every recipe at its default parameters (used as planner few-shots)."""
    return [build(name) for name, _b, _k in RECIPES]


def build(name: str, **params) -> dict:
    """Build a recipe by name with optional parameters; raises KeyError on an unknown name."""
    for rname, builder, _kw in RECIPES:
        if rname == name:
            return builder(**params)
    raise KeyError(f"no recipe named {name!r}")


def _extract_number(request: str, *keys: str) -> float | None:
    """Pull the first plausible numeric (optionally near a keyword/unit) out of a request string."""
    import re
    low = request.lower()
    # Prefer a number adjacent to a unit (V, Hz, gain of N).
    for pat in (r"gain\s*(?:of|=|:)?\s*([0-9]*\.?[0-9]+)",
                r"([0-9]*\.?[0-9]+)\s*(?:hz|khz|hertz)",
                r"([0-9]*\.?[0-9]+)\s*v\b"):
        m = re.search(pat, low)
        if m:
            val = float(m.group(1))
            if "khz" in low and "hz" in pat:
                val *= 1000.0
            return val
    m = re.search(r"([0-9]*\.?[0-9]+)", low)
    return float(m.group(1)) if m else None


def closest_recipe(request: str) -> dict:
    """Pick + parameterize the recipe that best matches a free-text request (deterministic).

    Keyword-scores each recipe against the lowercased request; ties break by RECIPES order. Pulls
    the obvious numeric argument out of the text (gain N / N Hz / N V) when the recipe takes one, so
    "an inverting amplifier, gain 10" yields ``inverting_amp(gain=10)`` and "a 5V divider, 2.5V out"
    yields ``voltage_divider(5, 2.5)``. Always returns a fully-built design (the safety net).
    """
    low = request.lower()
    # Default to the resistive divider when NOTHING matches: best_score starts at 0 so a recipe must
    # actually hit a keyword (score >= 1) to win — otherwise every unmatched request would fall to
    # whichever recipe is listed first (Bug E2: "LED driver" silently became inverting_amp because
    # all scores were 0 and inverting_amp led the list).
    best_name = "voltage_divider"
    best_score = 0
    for rname, _builder, keywords in RECIPES:
        score = sum(1 for kw in keywords if kw in low)
        if score > best_score:
            best_score, best_name = score, rname

    num = _extract_number(request)
    if best_name == "voltage_divider":
        import re
        vs = [float(x) for x in re.findall(r"([0-9]*\.?[0-9]+)\s*v\b", low)]
        if len(vs) >= 2:
            return voltage_divider(vin=vs[0], vout=vs[1])
        if len(vs) == 1:
            return voltage_divider(vin=vs[0], vout=vs[0] / 2.0)
        return voltage_divider()
    if best_name == "led_driver":
        import re
        vs = [float(x) for x in re.findall(r"([0-9]*\.?[0-9]+)\s*v\b", low)]
        kw = {"vsupply": vs[0]} if vs else {}
        return led_driver(**kw)
    if best_name == "inverting_amp":
        return inverting_amp(gain=num) if num else inverting_amp()
    if best_name == "rc_lowpass":
        return rc_lowpass(fc=num) if num else rc_lowpass()
    if best_name == "rc_555_astable":
        return rc_555_astable(freq=num) if num else rc_555_astable()
    return voltage_divider()
