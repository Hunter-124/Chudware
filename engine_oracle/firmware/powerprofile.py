"""Emulation power/duty-cycle profile — ``aicad.powerprofile/1`` (T-FIN7).

Derives a power-relevant **activity profile** from a T-FIN1 emulation trace
(``aicad.firmware.trace/1``) and combines it with per-component supply-rail current
metadata (T-FIN8 ``eng.supply.rails`` — ``contracts/ecad/boardphys.md``) into a
duty-cycle-weighted per-rail current profile.

This is deliberately **BOOKKEEPING, not a SPICE power simulation**: currents are
datasheet envelope numbers scaled by observed/derived duty cycles. The artifact says so
(``model`` field) and every rail carries provenance. Consumed by the system power budget
(T-FIN23) and directly usable as a transient-thermal duty schedule (T-FIN18 —
``schedule`` is exactly the ``{"duty", "period_s"}`` / ``{"points", "period_s"}`` shape
``run_fea_thermal`` accepts on heat loads).

Honesty rules (binding, same as boardphys):
- unknown currents stay ``null`` — never an invented 0; gaps are listed in ``coverage``;
- every duty/residency figure names its source heuristic (``source`` fields);
- when the trace gives no way to know a component's activity, it is assumed ALWAYS
  ACTIVE (conservative for power) and the assumption is recorded.

CPU run/sleep residency heuristics (in priority order, documented per contract §11):
1. ``AICAD_CPU_EVT <run|sleep> t=<n>ms`` UART marker lines — the optional firmware-side
   ``AICAD_CPU_TRACE(state, t_ms)`` macro (``templates/cmake_arm/include/aicad_trace.h``)
   prints them around WFI/sleep entry/exit. Residency is integrated piecewise over the
   run window (state before the first marker is assumed ``run`` — reset starts running).
2. No markers → residency is *assumed* 100 % active with ``source: "assumed_active"``.
   We do NOT guess sleep from UART silence — that would be dishonest.

GPIO duty heuristics per pin (in priority order):
1. Time-stamped events: high-time integrated over the run window (level before the
   first stamped event is assumed 0 — GPIO ports reset low on every supported family).
   ``period_s`` is the mean rising-edge interval when >= 2 stamped rising edges exist.
2. Only unstamped events (Renode hook prints carry no timestamp): duty is ``null`` —
   order is known but time weighting is not; the toggle count is reported instead.
3. No events but the pin appears in ``gpio_final``: duty = its final level
   (``source: "final_state_only"``).

Pure module — no I/O except :func:`write_profile_artifact` (atomic tmp+replace,
best-effort, never raises), mirroring ``emulator.write_trace_artifact``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = "aicad.powerprofile/1"
MODEL_NOTE = ("duty-cycle bookkeeping over datasheet rail currents — "
              "NOT a SPICE/instruction-level power simulation")

# Firmware-side CPU state marker (optional AICAD_CPU_TRACE macro):
#   AICAD_CPU_EVT run t=120ms   |   AICAD_CPU_EVT sleep t=120000us
_CPU_EVT_RE = re.compile(
    r"AICAD_CPU_EVT\s+(?P<state>run|sleep)"
    r"(?:\s+t=(?P<t>[0-9]+(?:\.[0-9]+)?)(?P<unit>ms|us|s)?)?")

# Ground-ish rails never carry a current budget of their own (return path).
_GROUND_NAMES = {"GND", "GNDA", "GNDD", "AGND", "DGND", "PGND", "EGND", "VSS", "VSSA",
                 "EARTH", "CHASSIS"}


def _t_ms(t: str, unit: Optional[str]) -> float:
    v = float(t)
    u = unit or "ms"
    return v * 1000.0 if u == "s" else (v / 1000.0 if u == "us" else v)


# ---------------------------------------------------------------------------
# Trace access
# ---------------------------------------------------------------------------

def load_trace(trace_path: str) -> Optional[dict]:
    """Load an ``aicad.firmware.trace/1`` artifact; None on any failure (never raises)."""
    try:
        data = json.loads(Path(trace_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def split_events(trace: dict) -> tuple[list[dict], list[dict]]:
    """``(gpio_events, uart_events)`` from a trace artifact dict **or** a SimResult-shaped
    dict (``gpio_events`` only — no uart event list). Seq order preserved. Pure."""
    events = trace.get("events")
    if isinstance(events, list):
        gpio = [e for e in events if isinstance(e, dict) and e.get("type") == "gpio"]
        uart = [e for e in events if isinstance(e, dict) and e.get("type") == "uart"]
        return gpio, uart
    gpio = [e for e in trace.get("gpio_events", []) or [] if isinstance(e, dict)]
    return gpio, []


def run_window_ms(gpio_events: list[dict], uart_events: list[dict],
                  explicit_ms: Optional[float] = None) -> Optional[float]:
    """The observable run window: an explicit caller value wins, else the max stamped
    ``t_ms`` across all events, else None (nothing time-stamped — no time axis)."""
    if explicit_ms is not None and explicit_ms > 0:
        return float(explicit_ms)
    stamps = [e["t_ms"] for e in gpio_events + uart_events
              if isinstance(e.get("t_ms"), (int, float))]
    return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# CPU residency
# ---------------------------------------------------------------------------

def cpu_markers(uart_events: list[dict]) -> list[tuple[Optional[float], str]]:
    """Extract ``(t_ms, state)`` CPU run/sleep markers from UART event text. Pure."""
    out: list[tuple[Optional[float], str]] = []
    for e in uart_events:
        m = _CPU_EVT_RE.search(str(e.get("text", "")))
        if not m:
            continue
        t = _t_ms(m.group("t"), m.group("unit")) if m.group("t") is not None \
            else (e.get("t_ms") if isinstance(e.get("t_ms"), (int, float)) else None)
        out.append((t, m.group("state")))
    return out


def cpu_residency(uart_events: list[dict], window_ms: Optional[float]) -> dict:
    """CPU run/sleep residency over the run window.

    Heuristic 1 (``source: "cpu_markers"``): integrate ``AICAD_CPU_EVT`` markers
    piecewise; the CPU is assumed ``run`` before the first marker (reset starts running).
    Needs >= 1 time-stamped marker AND a known window. Heuristic 2
    (``source: "assumed_active"``): no usable markers — assume 100 % active, honestly
    labelled. Returns ``{active_frac, sleep_frac, active_s|None, sleep_s|None, source,
    note}``. Pure.
    """
    marks = [(t, s) for (t, s) in cpu_markers(uart_events) if t is not None]
    if marks and window_ms and window_ms > 0:
        marks.sort(key=lambda m: m[0])
        active = 0.0
        now, state = 0.0, "run"
        for t, s in marks:
            t = min(max(t, 0.0), window_ms)
            if state == "run":
                active += t - now
            now, state = t, s
        if state == "run":
            active += window_ms - now
        frac = min(max(active / window_ms, 0.0), 1.0)
        return {"active_frac": round(frac, 6), "sleep_frac": round(1.0 - frac, 6),
                "active_s": round(frac * window_ms / 1000.0, 6),
                "sleep_s": round((1.0 - frac) * window_ms / 1000.0, 6),
                "source": "cpu_markers",
                "note": f"{len(marks)} AICAD_CPU_EVT marker(s) integrated over "
                        f"{window_ms:g}ms window"}
    note = ("no time-stamped AICAD_CPU_EVT markers in trace — assuming CPU always "
            "active (conservative). Instrument sleep entry/exit with AICAD_CPU_TRACE "
            "in aicad_trace.h for measured residency.")
    return {"active_frac": 1.0, "sleep_frac": 0.0,
            "active_s": round(window_ms / 1000.0, 6) if window_ms else None,
            "sleep_s": 0.0 if window_ms else None,
            "source": "assumed_active", "note": note}


# ---------------------------------------------------------------------------
# GPIO duty
# ---------------------------------------------------------------------------

def pin_duty(gpio_events: list[dict], pin: str, window_ms: Optional[float],
             gpio_final: Optional[dict] = None) -> dict:
    """Duty-cycle record for one pin (module-docstring heuristics). Pure.

    Returns ``{duty: float|None, period_s: float|None, toggles: int, source, note?}``.
    """
    evs = [e for e in gpio_events if e.get("pin") == pin]
    levels = [e.get("level") for e in evs]
    toggles = sum(1 for a, b in zip(levels, levels[1:]) if a != b)
    stamped = [e for e in evs if isinstance(e.get("t_ms"), (int, float))]

    period_s: Optional[float] = None
    rises = [e["t_ms"] for e in stamped if e.get("level") == 1]
    if len(rises) >= 2:
        deltas = [b - a for a, b in zip(rises, rises[1:])]
        period_s = round((sum(deltas) / len(deltas)) / 1000.0, 9)

    if stamped and window_ms and window_ms > 0:
        # Integrate high-time; level before the first stamped event assumed 0
        # (GPIO ports reset low on every supported family).
        high = 0.0
        now, level = 0.0, 0
        for e in sorted(stamped, key=lambda x: x["t_ms"]):
            t = min(max(float(e["t_ms"]), 0.0), window_ms)
            if level == 1:
                high += t - now
            now, level = t, int(e["level"])
        if level == 1:
            high += window_ms - now
        duty = min(max(high / window_ms, 0.0), 1.0)
        return {"duty": round(duty, 6), "period_s": period_s, "toggles": toggles,
                "source": "stamped_events"}
    if evs:
        return {"duty": None, "period_s": period_s, "toggles": toggles,
                "source": "unstamped_events",
                "note": f"{len(evs)} event(s) without timestamps (Renode hook prints "
                        "carry no t) — event order known, time weighting unknown"}
    lvl = (gpio_final or {}).get(pin)
    if lvl is not None:
        return {"duty": float(int(lvl)), "period_s": None, "toggles": 0,
                "source": "final_state_only",
                "note": "no events observed — duty taken from the end-of-run snapshot"}
    return {"duty": None, "period_s": None, "toggles": 0, "source": "no_data"}


def gpio_duty_cycles(gpio_events: list[dict], gpio_final: Optional[dict],
                     window_ms: Optional[float]) -> dict[str, dict]:
    """Duty record per pin — every pin seen in events or the final snapshot. Pure."""
    pins = sorted({str(e["pin"]) for e in gpio_events if e.get("pin")} |
                  set((gpio_final or {}).keys()))
    return {p: pin_duty(gpio_events, p, window_ms, gpio_final) for p in pins}


# ---------------------------------------------------------------------------
# Board rail resolution + profile assembly
# ---------------------------------------------------------------------------

def _pin_net_map(board: dict, mcu_family: Optional[str]) -> dict[int, str]:
    """``{physical pin_id: net name}`` for positional nets (via ``pinmap.pin_model``) —
    lets an observed trace pin (e.g. "PA5") be mapped back to a board net."""
    from engine.firmware import pinmap
    out: dict[int, str] = {}
    for rec in pinmap.pin_model(board, mcu_family=mcu_family):
        if rec.get("positional") and isinstance(rec.get("pin_id"), int):
            out[rec["pin_id"]] = str(rec["net"])
    return out


def _component_activity(fp: dict, is_mcu: bool, cpu: dict,
                        net_duty: dict[str, dict]) -> tuple[Optional[float], str]:
    """``(active_frac | None, source)`` for one component.

    - MCU footprints follow the CPU residency.
    - A component whose pads sit on a net mapped to an observed GPIO pin follows that
      pin's duty (a load switched by the firmware).
    - Otherwise (and whenever the duty is unknowable): assumed always active.
    """
    if is_mcu:
        return cpu.get("active_frac"), f"cpu_residency:{cpu.get('source')}"
    for pad in fp.get("pads", []) or []:
        net = str(pad.get("net") or "")
        rec = net_duty.get(net)
        if rec is not None:
            if rec.get("duty") is not None:
                return float(rec["duty"]), f"gpio_duty:{net} ({rec['source']})"
            return 1.0, (f"assumed_active (net {net} observed but duty unknown: "
                         f"{rec.get('source')})")
    return 1.0, "assumed_active (no observed activity mapping for this component)"


def build_power_profile(trace: dict, *, board: Optional[dict] = None,
                        mcu_family: Optional[str] = None,
                        trace_ref: Optional[str] = None,
                        window_ms: Optional[float] = None) -> dict:
    """Assemble the ``aicad.powerprofile/1`` artifact dict (contract §11). Pure.

    ``trace`` is an ``aicad.firmware.trace/1`` dict (or a SimResult-shaped dict);
    ``board`` an ECAD board doc (already resolved) whose footprints carry T-FIN8
    ``eng.supply.rails`` metadata. Without a board the profile still carries the
    activity half (CPU residency + per-pin duty) with an empty ``rails`` list and a
    coverage note.
    """
    gpio_events, uart_events = split_events(trace)
    gpio_final = trace.get("gpio_final") or trace.get("gpio_states") or {}
    window = run_window_ms(gpio_events, uart_events, window_ms)
    cpu = cpu_residency(uart_events, window)
    pins = gpio_duty_cycles(gpio_events, gpio_final, window)

    notes: list[str] = [MODEL_NOTE]
    if window is None:
        notes.append("no time-stamped events in trace — duty cycles limited to "
                     "final-state/assumed values (run a scenario for AICAD_T_MARK "
                     "timestamps, or use AICAD_GPIO_TRACE/AICAD_CPU_TRACE)")

    rails_out: list[dict] = []
    coverage: dict[str, Any] = {"components_total": 0, "components_with_rails": 0,
                                "unknown_active_current": [], "notes": notes}

    if isinstance(board, dict):
        from engine.firmware import mcu_pinouts
        try:
            from engine.ecad import metadata
        except Exception:  # pragma: no cover - metadata module always ships
            metadata = None
        family = mcu_family or mcu_pinouts.select_mcu_family(board)
        pin_nets = _pin_net_map(board, family)
        # observed trace pin ("PA5") -> board net -> duty record
        net_duty: dict[str, dict] = {}
        for pin_name, rec in pins.items():
            net_duty.setdefault(pin_name, rec)  # net literally named like the pin
            pid = mcu_pinouts.resolve_pin(pin_name, family)
            if pid is not None and pid in pin_nets:
                net_duty.setdefault(pin_nets[pid], rec)

        agg: dict[str, dict] = {}
        for fp in board.get("footprints", []) or []:
            if not isinstance(fp, dict):
                continue
            coverage["components_total"] += 1
            eng = metadata.eng_for_footprint(fp) if metadata else {}
            rails = (eng.get("supply") or {}).get("rails") or []
            if not rails:
                continue
            coverage["components_with_rails"] += 1
            ref = str(fp.get("ref", "?"))
            is_mcu = bool(mcu_pinouts.component_family(fp))
            duty, act_source = _component_activity(fp, is_mcu, cpu, net_duty)
            for rail in rails:
                name = str(rail.get("name") or "")
                if not name or name.upper() in _GROUND_NAMES:
                    continue
                r = agg.setdefault(name, {
                    "i_avg_a": None, "i_peak_a": None, "voltage_v_nom": None,
                    "duties": [], "provenance": [], "unknown": []})
                if r["voltage_v_nom"] is None and \
                        isinstance(rail.get("voltage_v_nom"), (int, float)):
                    r["voltage_v_nom"] = float(rail["voltage_v_nom"])
                i_act = rail.get("current_a_active")
                i_slp = rail.get("current_a_sleep")
                if not isinstance(i_act, (int, float)):
                    r["unknown"].append(ref)
                    coverage["unknown_active_current"].append(f"{ref}:{name}")
                    r["provenance"].append(
                        f"{ref}: active current unknown (null) — excluded from sums")
                    continue
                d = duty if duty is not None else 1.0
                avg = d * float(i_act)
                slp_note = ""
                if isinstance(i_slp, (int, float)):
                    avg += (1.0 - d) * float(i_slp)
                elif d < 1.0:
                    slp_note = " (sleep current unknown — sleep term omitted, avg is a floor)"
                r["i_avg_a"] = (r["i_avg_a"] or 0.0) + avg
                r["i_peak_a"] = (r["i_peak_a"] or 0.0) + float(i_act)
                r["duties"].append(d)
                r["provenance"].append(
                    f"{ref}: duty {d:g} via {act_source}; i_active {i_act:g}A"
                    + (f", i_sleep {i_slp:g}A" if isinstance(i_slp, (int, float))
                       else "") + slp_note)

        for name in sorted(agg):
            r = agg[name]
            duty = (sum(r["duties"]) / len(r["duties"])) if r["duties"] else None
            # schedule: the direct T-FIN18 transient-thermal shape. Period from the
            # dominant observed pin period when one exists, else 1.0 s (noted).
            periods = [rec["period_s"] for rec in pins.values()
                       if rec.get("period_s")]
            sched = None
            if duty is not None:
                sched = {"duty": round(duty, 6),
                         "period_s": round(periods[0], 9) if periods else 1.0}
                if not periods:
                    r["provenance"].append(
                        "schedule.period_s defaulted to 1.0s (no measured pin period)")
            rails_out.append({
                "name": name,
                "voltage_v_nom": r["voltage_v_nom"],
                "i_avg_a": round(r["i_avg_a"], 9) if r["i_avg_a"] is not None else None,
                "i_peak_a": round(r["i_peak_a"], 9) if r["i_peak_a"] is not None else None,
                "residency": {
                    "active_frac": round(duty, 6) if duty is not None else None,
                    "sleep_frac": round(1.0 - duty, 6) if duty is not None else None,
                    "active_s": (round(duty * window / 1000.0, 6)
                                 if duty is not None and window else None),
                    "sleep_s": (round((1.0 - duty) * window / 1000.0, 6)
                                if duty is not None and window else None),
                },
                "schedule": sched,
                "provenance": r["provenance"],
            })
        if not rails_out:
            notes.append("board resolved but no component carries eng.supply.rails "
                         "metadata (T-FIN8) — rails empty; seed the library or add "
                         "per-instance sim_params.eng overrides")
    else:
        notes.append("no board metadata available (board_doc_ref missing or "
                     "unresolvable) — activity profile only, rails empty")

    return {
        "schema": SCHEMA,
        "model": MODEL_NOTE,
        "generated_at": time.time(),
        "trace_ref": trace_ref,
        "window_ms": window,
        "cpu": cpu,
        "pins": pins,
        "rails": rails_out,
        "coverage": coverage,
    }


def write_profile_artifact(profile: dict, elf_path: str) -> Optional[str]:
    """Write the profile next to the ELF as ``<elf>.power.json`` (atomic tmp+replace).
    Best-effort: any failure returns None, never raises — an artifact, not a gate."""
    try:
        if not Path(elf_path).exists():
            return None
        out = Path(elf_path).with_suffix(Path(elf_path).suffix + ".power.json")
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        tmp.replace(out)
        return str(out)
    except Exception:
        return None
