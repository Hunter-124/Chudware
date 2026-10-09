"""Facilities tasks F1–F4: saved-study engineering deliverables graded from the artifact.

Every check reopens nothing itself: it reads the ONE declared ``deliverable.step+`` through
:class:`~benchmarks.artifact_validity.evidence.Evidence` — the saved study record
(``params`` = the inputs the agent actually used, ``results`` = what the tool persisted)
and the native bodies. Numeric results are compared to :mod:`..oracles`, which recomputes
them independently; where the task leaves a choice to the agent (rectangular duct section),
the reference is recomputed from the study's own choice. Inputs are checked against the
prompt, so a study solved for a different problem cannot pass on self-consistency alone.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from benchmarks.artifact_validity import geometry_checks as geo
from benchmarks.artifact_validity import oracles as orc
from benchmarks.artifact_validity.evidence import Evidence, gate
from benchmarks.artifact_validity.tasks.schema import CheckResult, Status, Task, check

DELIVERABLE = "deliverable.step+"

# ─────────────────────────────────────────────────────────────── shared helpers


def _num(x: Any) -> float | None:
    """A finite float, else None (bools are not numbers here)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _get(d: Any, *keys: Any) -> Any:
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and -len(d) <= k < len(d):
            d = d[k]
        else:
            return None
    return d


class _Cmp:
    """Accumulates labelled comparisons; one CheckResult lists every failing label."""

    def __init__(self) -> None:
        self.measured: dict[str, Any] = {}
        self.expected: dict[str, Any] = {}
        self.tolerance: dict[str, Any] = {}
        self.failed: list[str] = []

    def _row(self, label: str, ok: bool, measured: Any, expected: Any, tol: Any) -> bool:
        self.measured[label] = measured
        self.expected[label] = expected
        if tol is not None:
            self.tolerance[label] = tol
        if not ok:
            self.failed.append(label)
        return ok

    def close(self, label: str, measured: Any, expected: float, *, rel: float = 0.0,
              abs_: float = 0.0) -> bool:
        m = _num(measured)
        band = max(abs_, rel * abs(expected))
        ok = m is not None and abs(m - expected) <= band
        tol = {"rel": rel} if rel and not abs_ else {"abs": abs_} if abs_ and not rel else {
            "rel": rel, "abs": abs_}
        return self._row(label, ok, measured, round(expected, 6), tol)

    def at_least(self, label: str, measured: Any, minimum: float, *, slack: float = 0.0) -> bool:
        m = _num(measured)
        return self._row(label, m is not None and m >= minimum - slack, measured,
                         f">= {round(minimum, 6)}", {"abs": slack} if slack else None)

    def at_most(self, label: str, measured: Any, maximum: float, *, slack: float = 0.0) -> bool:
        m = _num(measured)
        return self._row(label, m is not None and m <= maximum + slack, measured,
                         f"<= {round(maximum, 6)}", {"abs": slack} if slack else None)

    def equal(self, label: str, measured: Any, expected: Any) -> bool:
        return self._row(label, measured == expected, measured, expected, None)

    def true(self, label: str, cond: bool, measured: Any = None, expected: Any = True) -> bool:
        return self._row(label, bool(cond), measured if measured is not None else bool(cond),
                         expected, None)

    def result(self, cid: str, detail: str = "") -> CheckResult:
        status = Status.FAIL if self.failed else Status.PASS
        why = f"failed: {', '.join(self.failed)}" if self.failed else "all comparisons within tolerance"
        return CheckResult(cid, status, self.measured, self.expected, self.tolerance or None,
                           f"{detail + '; ' if detail else ''}{why}")


def _fail(cid: str, detail: str, measured: Any = None, expected: Any = None) -> CheckResult:
    return CheckResult(cid, Status.FAIL, measured, expected, None, detail)


def _the_study(ev: Evidence, cid: str, stype: str, name: str) -> tuple[dict | None, CheckResult | None]:
    """The ONE saved study of ``stype`` named ``name``; FAIL when absent or ambiguous."""
    g = gate(ev, cid)
    if g is not None:
        return None, g
    if "sim_study_list" in ev.probe_errors:
        return None, CheckResult(cid, Status.ERROR,
                                 detail=f"grader could not list studies: {ev.probe_errors['sim_study_list']}")
    same_type = [s for s in ev.studies if s.get("type") == stype]
    hits = [s for s in same_type if s.get("name") == name]
    if len(hits) != 1:
        return None, _fail(cid, f"expected exactly one saved {stype} study named {name!r}, found "
                                f"{len(hits)}", measured=[s.get("name") for s in same_type],
                           expected=name)
    st = hits[0]
    if not isinstance(st.get("params"), dict) or not isinstance(st.get("results"), dict):
        return None, _fail(cid, f"{stype} study {name!r} has no params/results record")
    return st, None


def _geometry_ready(ev: Evidence, cid: str) -> CheckResult | None:
    g = gate(ev, cid)
    if g is not None:
        return g
    broken = {k: v for k, v in ev.probe_errors.items()
              if k in ("asm_world_bodies", "eval_solid_health", "doc_tree")}
    if broken:
        return CheckResult(cid, Status.ERROR, detail=f"grader geometry probe failed: {broken}")
    return None


def _numbers(obj: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """Every scalar number in a JSON tree, with its path (bools excluded)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _numbers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _numbers(v, f"{path}[{i}]")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield path, obj


def _finite_results(cid: str, results: dict, required: Iterable[str]) -> CheckResult:
    """Every required result section is present/nonempty and every number in it finite."""
    missing = [k for k in required if not results.get(k)]
    if missing:
        return _fail(cid, f"study results lack {missing}", measured=sorted(results),
                     expected=list(required))
    bad: list[str] = []
    count = 0
    for k in required:
        for p, v in _numbers(results[k], k):
            count += 1
            if not math.isfinite(float(v)):
                bad.append(p)
    if count == 0:
        return _fail(cid, "result sections carry no numbers")
    if bad:
        return _fail(cid, f"{len(bad)} non-finite result value(s)", measured=bad[:20])
    return CheckResult(cid, Status.PASS, measured={"numbers": count}, detail="all result numbers finite")


def _bbox(body: dict) -> tuple[list[float], list[float], list[float]] | None:
    bb = body.get("bbox") or {}
    try:
        lo = [float(v) for v in bb["min"]]
        hi = [float(v) for v in bb["max"]]
    except (KeyError, TypeError, ValueError):
        return None
    return lo, hi, [h - l for l, h in zip(lo, hi)]


def _box_matches(body: dict, lo_exp: list[float], hi_exp: list[float], tol: list[float]) -> bool:
    bb = _bbox(body)
    if bb is None:
        return False
    lo, hi, _ = bb
    return all(abs(lo[i] - lo_exp[i]) <= tol[i] and abs(hi[i] - hi_exp[i]) <= tol[i]
               for i in range(3))


def _sound(body: dict) -> bool:
    return body.get("valid") is True and body.get("closed") is True


# ═══════════════════════════════════════════════════════════════ F1 hydraulic HPU

F1_CIRCUIT = "PRESS-HPU"
F1 = {"displacement_cc_rev": 10.0, "speed_rpm": 1500.0, "vol_efficiency": 0.95,
      "relief_bar": 160.0, "bore_mm": 50.0, "rod_mm": 28.0, "stroke_mm": 300.0,
      "force_n": 10_000.0, "mass_kg": 200.0, "shift_s": 0.1, "min_duration_s": 4.0,
      "plate_mm": (800.0, 500.0, 20.0)}
F1_Q_LPM = orc.pump_flow_lpm(F1["displacement_cc_rev"], F1["speed_rpm"], F1["vol_efficiency"])
F1_V_MMS = orc.cylinder_speed_mms(F1_Q_LPM, F1["bore_mm"])
F1_T_STROKE = orc.stroke_time_s(F1["stroke_mm"], F1_V_MMS)
F1_A_CAP = orc.circle_area_mm2(F1["bore_mm"])
F1_A_ROD_SIDE = F1_A_CAP - orc.circle_area_mm2(F1["rod_mm"])
F1_TYPES = ("pump", "valve_relief", "valve_directional", "cylinder_linear", "reservoir")

F1_PROMPT = f"""\
Design the hydraulic power unit (HPU) and press circuit below and deliver it as ONE native \
Chudware part saved as `{DELIVERABLE}` in the working directory.

Hydraulic circuit (ISO 1219), named `{F1_CIRCUIT}`, consisting of exactly these five components:
- Fixed-displacement pump: 10 cm³/rev at 1500 rpm, volumetric efficiency 0.95.
- Pressure-relief valve, cracking pressure 160 bar, on the pump pressure line, draining to tank.
- 4/3 directional control valve with tandem centre (P→T in centre), rated 30 L/min at 5 bar.
- Double-acting cylinder: bore 50 mm, rod 28 mm, stroke 300 mm; cap side (A) to valve port A, \
rod side (B) to valve port B; it starts fully retracted.
- Reservoir (tank) receiving the relief-valve and directional-valve tank returns.

Duty: the cylinder extends against a constant 10 kN resisting force with a 200 kg moving mass. \
The valve stays centred until t = 0.1 s, is then shifted to extend the cylinder and held there. \
Run a transient simulation of at least 4.0 s of this duty, and keep the simulation results \
and the circuit's sourcing bill of materials saved with the circuit.

Mounting: model the HPU mounting plate as a solid steel plate 800 mm × 500 mm × 20 mm, and \
place every circuit component in the circuit's 3D layout on that plate (inside its footprint, \
at or above its top face).

The saved file must contain the plate geometry and the saved hydraulic circuit (circuit, valve \
schedule, load, simulation results, bill of materials and 3D layout)."""

F1_NOTES = f"""\
Oracle: Q = D·n·ηv = 10·1500·0.95/1000 = {F1_Q_LPM:.4f} L/min; A = π·50²/4 = {F1_A_CAP:.3f} mm²;
v = Q/A = {F1_V_MMS:.3f} mm/s; ideal stroke time 300/v = {F1_T_STROKE:.4f} s (±0.25 s for the
0.1 s shift and 200 kg acceleration). Steady window 1.0–2.0 s: mean velocity within 3 %, force
balance pA·A_cap − pB·A_annulus within 3 % of 10 kN. Past the end stop the pump deadheads into
the relief: peak in [160, 184] bar (cracking + ≤15 % override). Inputs read from
params.circuit / params.valve_schedule / params.loads; results from results.simulate (compacted
≤400-sample series in the file plus the full-resolution series_path file)."""


def _f1_parts(circuit: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in circuit.get("components") or []:
        if isinstance(c, dict):
            out.setdefault(str(c.get("type")), []).append(c)
    return out


def _f1_circuit(ev: Evidence, cid: str) -> tuple[dict | None, dict | None, CheckResult | None]:
    st, bad = _the_study(ev, cid, "hydraulic_circuit", F1_CIRCUIT)
    if bad:
        return None, None, bad
    circuit = st["params"].get("circuit")
    if not isinstance(circuit, dict):
        return None, None, _fail(cid, "study has no params.circuit")
    return st, circuit, None


def _f1_ids(circuit: dict) -> dict[str, str] | None:
    parts = _f1_parts(circuit)
    if any(len(parts.get(t, [])) != 1 for t in F1_TYPES):
        return None
    return {t: str(parts[t][0]["id"]) for t in F1_TYPES}


def _f1_nets(circuit: dict) -> dict[tuple[str, str], int]:
    """Union-find of (component_id, PORT) over every hose net."""
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for hose in circuit.get("hoses") or []:
        conns = [(str(c.get("component_id")), str(c.get("port", "")).upper())
                 for c in (hose.get("connections") or []) if isinstance(c, dict)]
        for a in conns:
            find(a)
        for a, b in zip(conns, conns[1:]):
            parent[find(a)] = find(b)
    roots: dict[tuple[str, str], int] = {}
    labels: dict[tuple[str, str], int] = {}
    for node in parent:
        r = find(node)
        labels[node] = roots.setdefault(r, len(roots))
    return labels


def _f1_extend_position(circuit: dict, ids: dict[str, str]) -> str | None:
    """Spool position that pressurises the cylinder cap: the valve work port on the cap net."""
    nets = _f1_nets(circuit)
    cap = nets.get((ids["cylinder_linear"], "A"))
    if cap is None:
        return None
    for port in ("A", "B"):
        if nets.get((ids["valve_directional"], port)) == cap:
            return port.lower()
    return None


@check("F1.study")
def f1_study(ev: Evidence) -> CheckResult:
    st, _c, bad = _f1_circuit(ev, "F1.study")
    if bad:
        return bad
    missing = [k for k in ("simulate", "bom") if not isinstance(st["results"].get(k), dict)]
    if missing:
        return _fail("F1.study", f"saved circuit lacks results {missing}",
                     measured=sorted(st["results"]), expected=["bom", "simulate"])
    return CheckResult("F1.study", Status.PASS, measured=st.get("id"),
                       detail="one saved hydraulic_circuit with simulate + bom results")


@check("F1.components")
def f1_components(ev: Evidence) -> CheckResult:
    cid = "F1.components"
    _st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    parts = _f1_parts(circuit)
    c = _Cmp()
    c.equal("component types", sorted((t, len(v)) for t, v in parts.items()),
            sorted((t, 1) for t in F1_TYPES))
    if c.failed:
        return c.result(cid)
    p = {t: parts[t][0].get("params") or {} for t in F1_TYPES}
    c.close("pump displacement_cc_rev", p["pump"].get("displacement_cc_rev"), 10.0, abs_=1e-6)
    c.close("pump speed_rpm", p["pump"].get("speed_rpm"), 1500.0, abs_=1e-6)
    c.close("pump vol_efficiency", p["pump"].get("vol_efficiency", 0.95), 0.95, abs_=1e-9)
    c.close("relief set_pressure_bar", p["valve_relief"].get("set_pressure_bar"), 160.0, abs_=1e-6)
    v = p["valve_directional"]
    c.equal("valve center_condition", v.get("center_condition"), "tandem")
    c.close("valve ways", v.get("ways", 4), 4.0, abs_=0)
    c.close("valve positions", v.get("positions", 3), 3.0, abs_=0)
    c.close("valve rated_flow_lpm", v.get("rated_flow_lpm"), 30.0, abs_=1e-6)
    c.close("valve rated_dp_bar", v.get("rated_dp_bar", 5.0), 5.0, abs_=1e-6)
    y = p["cylinder_linear"]
    c.close("cylinder bore_mm", y.get("bore_mm"), 50.0, abs_=1e-6)
    c.close("cylinder rod_mm", y.get("rod_mm"), 28.0, abs_=1e-6)
    c.close("cylinder stroke_mm", y.get("stroke_mm"), 300.0, abs_=1e-6)
    c.equal("cylinder actuator_type", y.get("actuator_type", "double_acting"), "double_acting")
    return c.result(cid)


@check("F1.connectivity")
def f1_connectivity(ev: Evidence) -> CheckResult:
    cid = "F1.connectivity"
    _st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    ids = _f1_ids(circuit)
    if ids is None:
        return _fail(cid, "circuit does not have exactly one of each required component")
    nets = _f1_nets(circuit)
    pump, rel, val, cyl, tank = (ids[t] for t in F1_TYPES)
    n = lambda comp, port: nets.get((comp, port))  # noqa: E731
    tank_nets = {lab for (comp, _port), lab in nets.items() if comp == tank}
    c = _Cmp()
    p_net = n(pump, "P")
    c.true("pump P, relief P and valve P share one net",
           p_net is not None and p_net == n(rel, "P") == n(val, "P"),
           measured=[n(pump, "P"), n(rel, "P"), n(val, "P")])
    t_net = n(val, "T")
    c.true("relief T, valve T and reservoir share one net",
           t_net is not None and t_net == n(rel, "T") and t_net in tank_nets,
           measured=[n(rel, "T"), t_net, sorted(tank_nets)])
    c.true("pressure and tank nets are distinct", p_net is not None and p_net != t_net,
           measured=[p_net, t_net])
    c.true("valve A on cylinder cap (A)", n(val, "A") is not None and n(val, "A") == n(cyl, "A"),
           measured=[n(val, "A"), n(cyl, "A")])
    c.true("valve B on cylinder rod side (B)",
           n(val, "B") is not None and n(val, "B") == n(cyl, "B"),
           measured=[n(val, "B"), n(cyl, "B")])
    c.true("work lines isolated from P/T",
           len({n(val, "A"), n(val, "B"), p_net, t_net}) == 4,
           measured=[n(val, "A"), n(val, "B"), p_net, t_net])
    return c.result(cid)


@check("F1.duty")
def f1_duty(ev: Evidence) -> CheckResult:
    cid = "F1.duty"
    st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    ids = _f1_ids(circuit)
    if ids is None:
        return _fail(cid, "circuit does not have exactly one of each required component")
    params = st["params"]
    c = _Cmp()
    load = _get(params, "loads", ids["cylinder_linear"]) or {}
    c.close("load force_n", load.get("force_n"), F1["force_n"], abs_=1e-6)
    c.close("load mass_kg", load.get("mass_kg"), F1["mass_kg"], abs_=1e-6)
    c.at_least("duration_s", params.get("duration_s"), F1["min_duration_s"])
    extend = _f1_extend_position(circuit, ids)
    sched = sorted((e for e in (params.get("valve_schedule") or [])
                    if isinstance(e, dict) and str(e.get("component_id")) == ids["valve_directional"]),
                   key=lambda e: _num(e.get("t_s")) or 0.0)
    early = [e for e in sched if (_num(e.get("t_s")) or 0.0) < F1["shift_s"] - 1e-9]
    c.true("valve centred before 0.1 s",
           all(str(e.get("position")).lower() == "center" for e in early),
           measured=[(e.get("t_s"), e.get("position")) for e in early])
    shift = [e for e in sched if abs((_num(e.get("t_s")) or -1.0) - F1["shift_s"]) <= 1e-6]
    c.true("shift to the extend position at t = 0.1 s",
           extend is not None and len(shift) == 1 and str(shift[0].get("position")).lower() == extend,
           measured=[(e.get("t_s"), e.get("position")) for e in shift],
           expected={"t_s": F1["shift_s"], "position": extend})
    later = [e for e in sched if (_num(e.get("t_s")) or 0.0) > F1["shift_s"] + 1e-6]
    c.true("held in the extend position afterwards",
           all(str(e.get("position")).lower() == extend for e in later),
           measured=[(e.get("t_s"), e.get("position")) for e in later])
    return c.result(cid)


def _f1_series(st: dict, cyl: str) -> tuple[list[float], dict] | None:
    sim = st["results"].get("simulate") or {}
    t = sim.get("time_s")
    series = _get(sim, "components", cyl, "series")
    if not isinstance(t, list) or not isinstance(series, dict):
        return None
    return t, series


@check("F1.steady_extension")
def f1_steady(ev: Evidence) -> CheckResult:
    cid = "F1.steady_extension"
    st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    ids = _f1_ids(circuit)
    got = _f1_series(st, ids["cylinder_linear"]) if ids else None
    if got is None:
        return _fail(cid, "saved simulation has no time_s / cylinder series")
    t, s = got
    rows = [i for i, ti in enumerate(t) if _num(ti) is not None and 1.0 <= ti <= 2.0]
    if len(rows) < 20:
        return _fail(cid, f"only {len(rows)} saved samples in the 1.0–2.0 s steady window",
                     measured=len(rows), expected=">= 20")
    try:
        vel = [float(s["velocity_mms"][i]) for i in rows]
        force = [float(s["pressure_a_bar"][i]) * 0.1 * F1_A_CAP
                 - float(s["pressure_b_bar"][i]) * 0.1 * F1_A_ROD_SIDE for i in rows]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return _fail(cid, f"cylinder series incomplete: {exc}")
    c = _Cmp()
    c.close("mean velocity 1–2 s (mm/s)", sum(vel) / len(vel), F1_V_MMS, rel=0.03)
    c.close("mean hydraulic force pA·A − pB·Aann (N)", sum(force) / len(force), F1["force_n"],
            rel=0.03)
    return c.result(cid)


@check("F1.stroke")
def f1_stroke(ev: Evidence) -> CheckResult:
    cid = "F1.stroke"
    st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    ids = _f1_ids(circuit)
    if ids is None:
        return _fail(cid, "circuit does not have exactly one of each required component")
    cyl = _get(st["results"], "simulate", "summary", "cylinders", ids["cylinder_linear"]) or {}
    got = _f1_series(st, ids["cylinder_linear"])
    pos = [p for p in ((got[1].get("position_mm") or []) if got else []) if _num(p) is not None]
    c = _Cmp()
    c.equal("end_reached", cyl.get("end_reached"), True)
    c.close("stroke_time_s", cyl.get("stroke_time_s"), F1_T_STROKE, abs_=0.25)
    c.close("max position_mm", max(pos) if pos else None, F1["stroke_mm"], abs_=1.0)
    return c.result(cid)


@check("F1.relief")
def f1_relief(ev: Evidence) -> CheckResult:
    cid = "F1.relief"
    st, _c, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    peak = _get(st["results"], "simulate", "summary", "peak_pressure_bar")
    c = _Cmp()
    c.at_least("peak_pressure_bar (deadhead reaches relief)", peak, F1["relief_bar"], slack=0.5)
    c.at_most("peak_pressure_bar (relief limits pressure)", peak, F1["relief_bar"] * 1.15)
    return c.result(cid)


@check("F1.series_file")
def f1_series_file(ev: Evidence) -> CheckResult:
    cid = "F1.series_file"
    st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    ids = _f1_ids(circuit)
    sim = st["results"].get("simulate") or {}
    raw = sim.get("series_path")
    if not raw or not ids:
        return _fail(cid, "saved simulation names no full-resolution series file")
    path = Path(str(raw))
    if not path.is_file() or path.stat().st_size == 0:
        return _fail(cid, f"series file missing or empty: {path}", measured=str(path))
    try:
        full = json.loads(path.read_text(encoding="utf-8"))
        t = full["time_s"]
        vel = full["components"][ids["cylinder_linear"]]["series"]["velocity_mms"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _fail(cid, f"series file unreadable: {exc}", measured=str(path))
    c = _Cmp()
    c.true("aligned nonempty series", isinstance(t, list) and isinstance(vel, list)
           and len(t) == len(vel) > 0, measured=[len(t) if isinstance(t, list) else None,
                                                  len(vel) if isinstance(vel, list) else None])
    c.at_least("full-resolution samples", len(t) if isinstance(t, list) else None,
               len(sim.get("time_s") or []))
    c.true("finite series", all(_num(x) is not None for x in (t or []) + (vel or [])))
    return c.result(cid, str(path))


@check("F1.results_finite")
def f1_results_finite(ev: Evidence) -> CheckResult:
    cid = "F1.results_finite"
    st, _c, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    sim = st["results"].get("simulate") or {}
    t = sim.get("time_s") or []
    if len(t) < 2 or any(_num(x) is None for x in t) or any(b <= a for a, b in zip(t, t[1:])):
        return _fail(cid, "saved time base is empty, non-finite or not increasing",
                     measured=len(t))
    if (_num(t[-1]) or 0.0) < F1["min_duration_s"] - 1e-6:
        return _fail(cid, "saved run ends before the required 4.0 s", measured=t[-1],
                     expected=F1["min_duration_s"])
    return _finite_results(cid, sim, ("summary", "components", "nodes"))


@check("F1.bom")
def f1_bom(ev: Evidence) -> CheckResult:
    cid = "F1.bom"
    st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    lines = _get(st["results"], "bom", "bom_lines")
    if not isinstance(lines, list) or not lines:
        return _fail(cid, "saved circuit has no BOM lines")
    comp_ids = sorted(str(x.get("id")) for x in circuit.get("components") or [])
    c = _Cmp()
    c.equal("BOM line ids", sorted(str(x.get("id")) for x in lines), comp_ids)
    c.true("every quantity >= 1", all((_num(x.get("quantity")) or 0) >= 1 for x in lines),
           measured=[x.get("quantity") for x in lines])
    return c.result(cid)


def _f1_plate(ev: Evidence) -> dict | None:
    want = sorted(F1["plate_mm"])
    for b in ev.bodies:
        bb = _bbox(b)
        if bb and all(abs(s - w) <= 0.5 for s, w in zip(sorted(bb[2]), want)):
            return b
    return None


@check("F1.mounting_plate")
def f1_mounting_plate(ev: Evidence) -> CheckResult:
    cid = "F1.mounting_plate"
    bad = _geometry_ready(ev, cid)
    if bad:
        return bad
    plate = _f1_plate(ev)
    sizes = [sorted(_bbox(b)[2]) for b in ev.bodies if _bbox(b)]
    c = _Cmp()
    c.true("a body measuring 800 × 500 × 20 mm (±0.5)", plate is not None,
           measured=[[round(v, 2) for v in s] for s in sizes], expected=sorted(F1["plate_mm"]))
    c.true("plate body valid and closed", plate is not None and _sound(plate),
           measured={k: (plate or {}).get(k) for k in ("valid", "closed")})
    faults = geo.history_faults(ev)
    c.true("editable feature history", not faults, measured=faults or "ok")
    return c.result(cid)


@check("F1.layout")
def f1_layout(ev: Evidence) -> CheckResult:
    cid = "F1.layout"
    st, circuit, bad = _f1_circuit(ev, cid)
    if bad:
        return bad
    bad = _geometry_ready(ev, cid)
    if bad:
        return bad
    plate = _f1_plate(ev)
    if plate is None:
        return _fail(cid, "no 800 × 500 × 20 mm plate body to place the components on")
    lo, hi, _ = _bbox(plate)
    layout = st["params"].get("layout3d")
    if not isinstance(layout, dict):
        return _fail(cid, "saved circuit has no 3D layout (params.layout3d)")
    c = _Cmp()
    for comp in circuit.get("components") or []:
        key = str(comp.get("id"))
        xyz = layout.get(key)
        ok = (isinstance(xyz, list) and len(xyz) == 3 and all(_num(v) is not None for v in xyz)
              and lo[0] - 1 <= xyz[0] <= hi[0] + 1 and lo[1] - 1 <= xyz[1] <= hi[1] + 1
              and xyz[2] >= hi[2] - 1)
        c.true(f"{key} ({comp.get('type')}) on the plate", ok, measured=xyz,
               expected={"x": [lo[0], hi[0]], "y": [lo[1], hi[1]], "z_min": hi[2]})
    return c.result(cid)


F1_TASK = Task(
    id="F1", version=1, domain="facilities", prompt=F1_PROMPT,
    checks=(f1_study, f1_components, f1_connectivity, f1_duty, f1_steady, f1_stroke,
            f1_relief, f1_series_file, f1_results_finite, f1_bom, f1_mounting_plate, f1_layout),
    notes=F1_NOTES)


# ═══════════════════════════════════════════════════════════════ F2 cooling water

F2_NETWORK = "CW-1"
F2_NODES = {"S": (0.0, 0.0, 0.0), "T": (10_000.0, 0.0, 0.0), "A": (25_000.0, 0.0, 0.0),
            "B": (10_000.0, 20_000.0, 0.0)}
#: role → (from node, to node, length m, flow m³/h, ΣK on that link)
F2_LINKS = {"main": ("S", "T", 10.0, 12.0, 0.0),
            "a": ("T", "A", 15.0, 6.0, 0.38 + 5.0),
            "b": ("T", "B", 20.0, 6.0, 1.14 + 5.0)}
F2 = {"nps": 2.0, "schedule": "40", "material": "A106-B", "roughness_mm": 0.046,
      "rho": 998.2, "mu": 0.001002, "supply_kpa": 250.0, "demand_m3h": 6.0,
      "design_kpa": 1000.0, "design_c": 40.0, "ca_mm": 1.5, "mill": 0.125, "e": 1.0, "w": 1.0,
      "s_mpa": 137.9, "valve_k": 5.0, "valve_offset_mm": 1000.0}
F2_OD, F2_WALL = orc.pipe_od_wall_mm(F2["nps"], F2["schedule"])
F2_ID = F2_OD - 2.0 * F2_WALL
F2_SEG = {role: orc.pipe_segment(q, F2_ID, length, k, F2["rho"], F2["mu"], F2["roughness_mm"])
          for role, (_a, _b, length, q, k) in F2_LINKS.items()}
F2_P = {"T": F2["supply_kpa"] - F2_SEG["main"]["dp_kpa"]}
F2_P["A"] = F2_P["T"] - F2_SEG["a"]["dp_kpa"]
F2_P["B"] = F2_P["T"] - F2_SEG["b"]["dp_kpa"]
F2_T_P = orc.b31_3_pressure_thickness_mm(F2["design_kpa"] / 1000.0, F2_OD, F2["s_mpa"], F2["e"],
                                         F2["w"])
F2_T_REQ = orc.b31_3_required_nominal_mm(F2_T_P, F2["ca_mm"], F2["mill"])
F2_MAWP = orc.b31_3_mawp_kpa(F2_WALL, F2["ca_mm"], F2["mill"], F2_OD, F2["s_mpa"], F2["e"], F2["w"])

F2_PROMPT = f"""\
Design a cooling-water distribution network and deliver it as ONE native Chudware part saved as \
`{DELIVERABLE}` in the working directory, containing a saved piping network named `{F2_NETWORK}`.

Layout (coordinates in mm, everything at elevation Z = 0, straight runs, no bends):
- Main: supply connection S (0, 0, 0) to the branch tee T (10 000, 0, 0) — 10 m.
- Branch A: tee T to consumer A (25 000, 0, 0) — 15 m, straight through the tee.
- Branch B: tee T to consumer B (10 000, 20 000, 0) — 20 m, off the tee's branch outlet.
Pipe: NPS 2 Schedule 40, ASTM A106 Grade B carbon steel throughout; absolute roughness 0.046 mm.
Fluid: water, density 998.2 kg/m³, dynamic viscosity 0.001002 Pa·s.
Minor losses (constant K, on the velocity of the pipe they sit in): tee straight-through K = 0.38 \
(on branch A), tee branch K = 1.14 (on branch B), and one balancing valve with constant K = 5.0 on \
each branch, located 1 m upstream of its consumer. No other fittings or losses.
Boundary conditions: S held at 250 kPa gauge; consumers A and B each draw 6 m³/h.

Deliver, saved with the network: the steady-state network solution (flows, velocities, pressure \
drops, node pressures); an ASME B31.3 pressure-design check of every run at a design pressure of \
1000 kPa gauge and 40 °C with corrosion allowance 1.5 mm, mill under-tolerance 12.5 %, \
longitudinal joint factor E = 1.0 and weld strength reduction factor W = 1.0; and the piping bill \
of materials."""

F2_NOTES = f"""\
Catalog NPS 2 Sch 40 (B36.10M): OD {F2_OD} / wall {F2_WALL} / ID {F2_ID:.2f} mm. Colebrook
(iterative) + Darcy–Weisbach + constant K, ρ 998.2, μ 1.002e-3, ε 0.046 mm:
main v {F2_SEG['main']['velocity_ms']:.4f} m/s Δp {F2_SEG['main']['dp_kpa']:.4f} kPa;
A Δp {F2_SEG['a']['dp_kpa']:.4f} kPa (K 5.38); B Δp {F2_SEG['b']['dp_kpa']:.4f} kPa (K 6.14).
Node pressures T {F2_P['T']:.3f}, A {F2_P['A']:.3f}, B {F2_P['B']:.3f} kPa(g); Δp bands 2 %.
B31.3 S(A106-B, 40 °C) 137.9 MPa, Y 0.4: t = 60.3/[2(137.9+0.4)] = {F2_T_P:.4f} mm;
t_m = (t + 1.5)/0.875 = {F2_T_REQ:.4f} mm ≤ 3.91 nominal; MAWP {F2_MAWP:.1f} kPa. The tee
constants are Crane TP-410 20·fT / 60·fT with fT(NPS 2) = 0.019, stated as plain K values."""


def _f2_net(ev: Evidence, cid: str) -> tuple[dict | None, CheckResult | None]:
    st, bad = _the_study(ev, cid, "piping_network", F2_NETWORK)
    if bad:
        return None, bad
    if not isinstance(st["params"].get("piping"), dict):
        return None, _fail(cid, "network study has no params.piping document")
    return st, None


def _near(p: Any, q: tuple[float, float, float], tol: float = 1.0) -> bool:
    try:
        return math.dist([float(v) for v in p], q) <= tol
    except (TypeError, ValueError):
        return False


def _polyline_mm(path: list) -> float:
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def _f2_roles(piping: dict) -> tuple[dict[str, dict], list[str]]:
    """Map main/a/b to the run joining their prescribed end nodes; problems listed."""
    runs = [r for r in piping.get("runs") or []
            if isinstance(r, dict) and not r.get("suppressed") and len(r.get("path") or []) >= 2]
    roles: dict[str, dict] = {}
    problems: list[str] = []
    for role, (na, nb, length, _q, _k) in F2_LINKS.items():
        a, b = F2_NODES[na], F2_NODES[nb]
        hits = [r for r in runs
                if (_near(r["path"][0], a) and _near(r["path"][-1], b))
                or (_near(r["path"][0], b) and _near(r["path"][-1], a))]
        if len(hits) != 1:
            problems.append(f"{role}: {len(hits)} runs join {na}–{nb}")
            continue
        roles[role] = hits[0]
    if len(runs) != len(F2_LINKS):
        problems.append(f"{len(runs)} active runs, expected {len(F2_LINKS)}")
    return roles, problems


def _f2_links(st: dict) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    roles, problems = _f2_roles(st["params"]["piping"])
    links = {str(lk.get("id")): lk for lk in _get(st["results"], "solve", "links") or []
             if isinstance(lk, dict)}
    out = {}
    for role, run in roles.items():
        lk = links.get(str(run.get("id")))
        if lk is None:
            problems.append(f"{role}: run {run.get('id')} not in the saved solve")
        else:
            out[role] = lk
    return roles, out, problems


@check("F2.study")
def f2_study(ev: Evidence) -> CheckResult:
    st, bad = _f2_net(ev, "F2.study")
    if bad:
        return bad
    missing = [k for k in ("solve", "spec_check", "bom") if not isinstance(st["results"].get(k), dict)]
    if missing:
        return _fail("F2.study", f"saved network lacks results {missing}",
                     measured=sorted(st["results"]), expected=["bom", "solve", "spec_check"])
    return CheckResult("F2.study", Status.PASS, measured=st.get("id"),
                       detail="one saved piping_network with solve + spec_check + bom")


@check("F2.route")
def f2_route(ev: Evidence) -> CheckResult:
    cid = "F2.route"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    roles, problems = _f2_roles(st["params"]["piping"])
    c = _Cmp()
    c.true("runs S–T, T–A, T–B present exactly once", not problems, measured=problems or "ok")
    for role, run in roles.items():
        c.close(f"{role} centreline length (mm)", _polyline_mm(run["path"]),
                F2_LINKS[role][2] * 1000.0, abs_=1.0)
        c.true(f"{role} at elevation 0", all(abs(float(p[2])) <= 1.0 for p in run["path"]),
               measured=[p[2] for p in run["path"]])
    return c.result(cid)


@check("F2.pipe_spec")
def f2_pipe_spec(ev: Evidence) -> CheckResult:
    cid = "F2.pipe_spec"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    piping = st["params"]["piping"]
    runs = [r for r in piping.get("runs") or [] if isinstance(r, dict) and not r.get("suppressed")]
    if not runs:
        return _fail(cid, "network has no runs")
    c = _Cmp()
    for r in runs:
        rid = r.get("id")
        c.close(f"{rid} nps", r.get("nps"), F2["nps"], abs_=1e-9)
        c.equal(f"{rid} schedule", str(r.get("schedule")), F2["schedule"])
        c.equal(f"{rid} material", r.get("material") or piping.get("material"), F2["material"])
    return c.result(cid)


@check("F2.fittings")
def f2_fittings(ev: Evidence) -> CheckResult:
    cid = "F2.fittings"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    roles, links, problems = _f2_links(st)
    if problems:
        return _fail(cid, "; ".join(problems))
    c = _Cmp()
    for role, (_na, _nb, _l, _q, k) in F2_LINKS.items():
        c.close(f"{role} link k_total", links[role].get("k_total"), k, abs_=0.03)
    fittings = [f for f in st["params"]["piping"].get("fittings") or []
                if isinstance(f, dict) and not f.get("suppressed")]
    for role in ("a", "b"):
        rid = roles[role].get("id")
        inline = [f for f in fittings if f.get("type") != "tee" and f.get("run_id") == rid]
        far = F2_NODES[F2_LINKS[role][1]]
        dist = [round(math.dist([float(v) for v in f.get("origin")], far), 2)
                for f in inline if isinstance(f.get("origin"), list)]
        c.true(f"{role}: balancing valve 1 m upstream of the consumer",
               any(abs(d - F2["valve_offset_mm"]) <= 5.0 for d in dist),
               measured=dist, expected=F2["valve_offset_mm"])
    tees = [f for f in fittings if f.get("type") == "tee"]
    c.true("one tee at T", len(tees) == 1 and _near(tees[0].get("origin"), F2_NODES["T"]),
           measured=[f.get("origin") for f in tees])
    return c.result(cid)


@check("F2.boundary")
def f2_boundary(ev: Evidence) -> CheckResult:
    cid = "F2.boundary"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    solve = st["results"].get("solve") or {}
    nodes = [n for n in solve.get("nodes") or [] if isinstance(n, dict)]

    def at(key: str) -> dict:
        hits = [n for n in nodes if _near(n.get("xyz"), F2_NODES[key])]
        return hits[0] if len(hits) == 1 else {}

    c = _Cmp()
    c.equal("network_type", st["params"].get("network_type", "liquid"), "liquid")
    c.close("fluid rho (kg/m³)", _get(solve, "fluid", "rho"), F2["rho"], abs_=0.01)
    c.close("fluid mu (Pa·s)", _get(solve, "fluid", "mu"), F2["mu"], abs_=1e-9)
    c.equal("S fixed", at("S").get("fixed"), True)
    c.close("S pressure_kpa", at("S").get("pressure_kpa"), F2["supply_kpa"], abs_=0.01)
    c.close("A demand_m3h", at("A").get("demand_m3h"), F2["demand_m3h"], abs_=1e-3)
    c.close("B demand_m3h", at("B").get("demand_m3h"), F2["demand_m3h"], abs_=1e-3)
    c.close("T demand_m3h", at("T").get("demand_m3h"), 0.0, abs_=1e-3)
    return c.result(cid)


@check("F2.mass_balance")
def f2_mass_balance(ev: Evidence) -> CheckResult:
    cid = "F2.mass_balance"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    _roles, links, problems = _f2_links(st)
    if problems:
        return _fail(cid, "; ".join(problems))
    q = {role: _num(links[role].get("flow_m3h")) for role in F2_LINKS}
    c = _Cmp()
    c.equal("solve converged", _get(st["results"], "solve", "converged"), True)
    for role, (_a, _b, _l, flow, _k) in F2_LINKS.items():
        c.close(f"{role} |flow| (m³/h)", abs(q[role]) if q[role] is not None else None, flow,
                rel=0.005)
    ok = all(v is not None for v in q.values())
    c.close("continuity at T |Qmain| − |QA| − |QB| (m³/h)",
            abs(q["main"]) - abs(q["a"]) - abs(q["b"]) if ok else None, 0.0, abs_=0.01)
    return c.result(cid)


@check("F2.hydraulics")
def f2_hydraulics(ev: Evidence) -> CheckResult:
    cid = "F2.hydraulics"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    _roles, links, problems = _f2_links(st)
    if problems:
        return _fail(cid, "; ".join(problems))
    c = _Cmp()
    for role, ref in F2_SEG.items():
        lk = links[role]
        c.close(f"{role} id_mm", lk.get("id_mm"), F2_ID, abs_=0.01)
        v = _num(lk.get("velocity_ms"))
        c.close(f"{role} |velocity| (m/s)", abs(v) if v is not None else None, ref["velocity_ms"],
                rel=0.005)
        c.close(f"{role} reynolds", lk.get("reynolds"), ref["reynolds"], rel=0.01)
        c.close(f"{role} friction_factor", lk.get("friction_factor"), ref["friction_factor"],
                rel=0.015)
        dp = _num(lk.get("dp_kpa"))
        c.close(f"{role} |dp| (kPa)", abs(dp) if dp is not None else None, ref["dp_kpa"], rel=0.02)
    return c.result(cid)


@check("F2.node_pressures")
def f2_node_pressures(ev: Evidence) -> CheckResult:
    cid = "F2.node_pressures"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    nodes = [n for n in _get(st["results"], "solve", "nodes") or [] if isinstance(n, dict)]
    c = _Cmp()
    for key, p_ref in F2_P.items():
        hits = [n for n in nodes if _near(n.get("xyz"), F2_NODES[key])]
        drop = F2["supply_kpa"] - p_ref
        c.close(f"{key} pressure_kpa", hits[0].get("pressure_kpa") if len(hits) == 1 else None,
                p_ref, abs_=max(0.05, 0.02 * drop))
    return c.result(cid)


@check("F2.b31_3")
def f2_b31_3(ev: Evidence) -> CheckResult:
    cid = "F2.b31_3"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    design = st["params"].get("design") or {}
    spec = st["results"].get("spec_check") or {}
    c = _Cmp()
    c.close("design_pressure_kpa", design.get("design_pressure_kpa"), F2["design_kpa"], abs_=1e-6)
    c.close("design_temp_c", design.get("design_temp_c"), F2["design_c"], abs_=1e-6)
    c.close("corrosion_allowance_mm", design.get("corrosion_allowance_mm"), F2["ca_mm"], abs_=1e-9)
    c.close("mill_tolerance_frac", design.get("mill_tolerance_frac"), F2["mill"], abs_=1e-9)
    c.close("joint_factor", design.get("joint_factor"), F2["e"], abs_=1e-9)
    c.close("weld strength reduction W", _get(spec, "design", "W"), F2["w"], abs_=1e-9)
    rows = {str(r.get("id")): r for r in spec.get("runs") or [] if isinstance(r, dict)}
    run_ids = sorted(str(r.get("id")) for r in st["params"]["piping"].get("runs") or []
                     if isinstance(r, dict) and not r.get("suppressed"))
    c.equal("checked runs", sorted(rows), run_ids)
    for rid in run_ids:
        r = rows.get(rid, {})
        c.close(f"{rid} od_mm", r.get("od_mm"), F2_OD, abs_=0.05)
        c.close(f"{rid} t_nominal_mm (catalog wall)", r.get("t_nominal_mm"), F2_WALL, abs_=0.005)
        c.close(f"{rid} allowable_mpa", r.get("allowable_mpa"), F2["s_mpa"], abs_=0.05)
        c.close(f"{rid} t_pressure_mm", r.get("t_pressure_mm"), F2_T_P, rel=0.01)
        c.close(f"{rid} t_required_mm", r.get("t_required_mm"), F2_T_REQ, rel=0.01)
        c.close(f"{rid} mawp_kpa", r.get("mawp_kpa"), F2_MAWP, rel=0.02)
        c.equal(f"{rid} pass", r.get("pass"), F2_WALL >= F2_T_REQ)
    return c.result(cid)


@check("F2.bom")
def f2_bom(ev: Evidence) -> CheckResult:
    cid = "F2.bom"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    bom = st["results"].get("bom") or {}
    items = [i for i in bom.get("items") or [] if isinstance(i, dict)]
    run_ids = sorted(str(r.get("id")) for r in st["params"]["piping"].get("runs") or []
                     if isinstance(r, dict) and not r.get("suppressed"))
    c = _Cmp()
    c.close("centreline_length_m", _get(bom, "totals", "centreline_length_m"),
            sum(v[2] for v in F2_LINKS.values()), abs_=0.05)
    c.equal("line list runs", sorted(str(x.get("run_id")) for x in bom.get("line_list") or []
                                     if isinstance(x, dict)), run_ids)
    pipe = [i for i in items if i.get("kind") == "pipe"]
    c.true("pipe items NPS 2 Sch 40 only", bool(pipe) and all(
        _num(i.get("nps")) == F2["nps"] and str(i.get("schedule")) == F2["schedule"] for i in pipe),
        measured=[(i.get("nps"), i.get("schedule"), i.get("qty")) for i in pipe])
    c.close("tee quantity", sum(_num(i.get("qty")) or 0 for i in items if i.get("kind") == "tee"),
            1.0, abs_=0)
    return c.result(cid)


@check("F2.results_finite")
def f2_results_finite(ev: Evidence) -> CheckResult:
    cid = "F2.results_finite"
    st, bad = _f2_net(ev, cid)
    if bad:
        return bad
    return _finite_results(cid, st["results"], ("solve", "spec_check", "bom"))


F2_TASK = Task(
    id="F2", version=1, domain="facilities", prompt=F2_PROMPT,
    checks=(f2_study, f2_route, f2_pipe_spec, f2_fittings, f2_boundary, f2_mass_balance,
            f2_hydraulics, f2_node_pressures, f2_b31_3, f2_bom, f2_results_finite),
    notes=F2_NOTES)


# ═══════════════════════════════════════════════════════════════ F3 pressure vessel

F3_VESSEL = "V-101"
PLATE_SERIES_MM = (3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 32)
F3 = {"id_mm": 1500.0, "tt_mm": 3000.0, "p_kpa": 1000.0, "temp_c": 40.0,
      "material": "SA-516-70", "s_mpa": 137.9, "e": 0.85, "ca_mm": 3.0,
      "head": "ellipsoidal_2_1", "orientation": "vertical", "rho": 998.0, "fill": 0.8,
      "water_rho": 1000.0}
F3_DEPTH = orc.ellipsoidal_2_1_depth_mm(F3["id_mm"])
F3_STATIC = orc.static_head_kpa(F3["rho"], F3["tt_mm"] + 2.0 * F3_DEPTH)
F3_PCALC = (F3["p_kpa"] + F3_STATIC) / 1000.0
F3_R_CORR = F3["id_mm"] / 2.0 + F3["ca_mm"]
F3_D_CORR = F3["id_mm"] + 2.0 * F3["ca_mm"]
F3_T_SHELL = orc.ug27_shell_thickness_mm(F3_PCALC, F3_R_CORR, F3["s_mpa"], F3["e"]) + F3["ca_mm"]
F3_T_HEAD = orc.ug32d_ellipsoidal_thickness_mm(F3_PCALC, F3_D_CORR, F3["s_mpa"], F3["e"]) + F3["ca_mm"]
F3_NOM_SHELL = orc.round_up_to_series(F3_T_SHELL, PLATE_SERIES_MM)
F3_NOM_HEAD = orc.round_up_to_series(F3_T_HEAD, PLATE_SERIES_MM)
F3_MAWP_SHELL = orc.ug27_shell_mawp_mpa(F3_NOM_SHELL - F3["ca_mm"], F3_R_CORR, F3["s_mpa"],
                                        F3["e"]) * 1000.0 - F3_STATIC
F3_MAWP_HEAD = orc.ug32d_ellipsoidal_mawp_mpa(F3_NOM_HEAD - F3["ca_mm"], F3_D_CORR, F3["s_mpa"],
                                              F3["e"]) * 1000.0 - F3_STATIC
F3_MAWP = min(F3_MAWP_SHELL, F3_MAWP_HEAD)
F3_HYDRO = orc.ug99b_hydrotest_kpa(F3_MAWP, 1.0)
F3_VOLUME = orc.vessel_volume_m3(F3["id_mm"], F3["tt_mm"])
F3_METAL_MM3 = orc.vessel_metal_volume_mm3(F3["id_mm"], F3["tt_mm"], F3_NOM_SHELL, F3_NOM_HEAD)

F3_PROMPT = f"""\
Size and model a vertical pressure vessel and deliver it as ONE native Chudware part saved as \
`{DELIVERABLE}` in the working directory, containing the saved vessel sizing named `{F3_VESSEL}` \
and the vessel geometry.

Design basis (ASME Section VIII Division 1, simplified sizing — not a code certification):
- Material SA-516 Grade 70 plate; allowable stress S = 137.9 MPa at the design temperature and \
at the hydrotest temperature.
- Inside diameter 1500 mm; 3000 mm between tangent lines; two 2:1 semi-ellipsoidal heads; vertical axis.
- Design pressure 1000 kPa gauge at the top of the vessel; design temperature 40 °C; joint \
efficiency E = 0.85 for shell and heads; corrosion allowance 3.0 mm.
- Contents: liquid of density 998 kg/m³; normal operating level 80 % of the vessel volume.
- Calculation pressure for shell and heads = design pressure + hydrostatic head of the vessel \
completely full of the contents over its full internal height (bottom of the lower head to top \
of the upper head), g = 9.80665 m/s².
- Shell per UG-27(c)(1), heads per UG-32(d), both on the corroded dimensions. Nominal plate = \
the smallest of 3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 32 mm that is at least \
the required thickness including corrosion allowance.
- Report the vessel MAWP at the top (net of that hydrostatic head), the UG-99(b) hydrostatic \
test pressure at the top, the internal volume, and the empty, operating, and full-of-water \
(1000 kg/m³) masses.

Geometry: model the pressure boundary — shell plus both heads — as one hollow solid of \
revolution about the vertical Z axis at the selected nominal thicknesses, with its inside \
surfaces at the inside dimensions above and its outside surfaces offset outward by the plate \
thickness."""

F3_NOTES = f"""\
UG-22 static head ρgH, H = 3000 + 2·375 = 3750 mm → {F3_STATIC:.3f} kPa; P_calc = {F3_PCALC*1000:.3f} kPa.
Corroded R = 753, D = 1506 mm. UG-27(c)(1) t = P·753/(137.9·0.85 − 0.6P) + 3 = {F3_T_SHELL:.4f} mm →
{F3_NOM_SHELL:g} mm; UG-32(d) t = P·1506/(2·137.9·0.85 − 0.2P) + 3 = {F3_T_HEAD:.4f} mm → {F3_NOM_HEAD:g} mm.
(At P = 1000 kPa and S = 138 MPa these are the audit's 9.4524 / 9.4249 mm, unit-tested in
test_facilities.) MAWP back-calculated on t_nom − CA, minus the static head: shell
{F3_MAWP_SHELL:.2f}, head {F3_MAWP_HEAD:.2f}, vessel {F3_MAWP:.2f} kPa; UG-99(b) 1.3·MAWP·1.0 =
{F3_HYDRO:.2f} kPa. Volume π/4·1.5²·3 + 2·π·1.5³/24 = {F3_VOLUME:.5f} m³. Numeric bands 2 %;
geometry: OD 1500 + 2·t_shell ±2 mm, height 3000 + 2(375 + t_head) ±0.5 %, metal volume
{F3_METAL_MM3/1e9:.4f} m³ ±15 % (head offset approximated by grown semi-axes)."""


def _f3_vessel(ev: Evidence, cid: str) -> tuple[dict | None, CheckResult | None]:
    return _the_study(ev, cid, "vessel", F3_VESSEL)


@check("F3.study")
def f3_study(ev: Evidence) -> CheckResult:
    st, bad = _f3_vessel(ev, "F3.study")
    if bad:
        return bad
    missing = [k for k in ("shell", "heads", "design", "material") if not isinstance(
        st["results"].get(k), dict)]
    if missing:
        return _fail("F3.study", f"saved vessel lacks result sections {missing}")
    return _finite_results("F3.study", st["results"], ("shell", "heads", "design", "material"))


@check("F3.inputs")
def f3_inputs(ev: Evidence) -> CheckResult:
    cid = "F3.inputs"
    st, bad = _f3_vessel(ev, cid)
    if bad:
        return bad
    p, r = st["params"], st["results"]
    c = _Cmp()
    c.close("inner_diameter_mm", p.get("inner_diameter_mm"), F3["id_mm"], abs_=1e-6)
    c.close("tan_tan_length_mm (result)", r.get("tan_tan_length_mm"), F3["tt_mm"], abs_=1.0)
    c.close("design_pressure_kpa", p.get("design_pressure_kpa"), F3["p_kpa"], abs_=1e-6)
    c.close("design_temp_c", p.get("design_temp_c"), F3["temp_c"], abs_=1e-6)
    c.equal("material", p.get("material"), F3["material"])
    c.close("allowable_mpa (result)", _get(r, "material", "allowable_mpa"), F3["s_mpa"], abs_=0.05)
    c.close("allowable_test_mpa (result)", _get(r, "material", "allowable_test_mpa"), F3["s_mpa"],
            abs_=0.05)
    c.close("corrosion_allowance_mm", p.get("corrosion_allowance_mm"), F3["ca_mm"], abs_=1e-9)
    c.close("joint_efficiency", p.get("joint_efficiency"), F3["e"], abs_=1e-9)
    c.equal("head_type", p.get("head_type"), F3["head"])
    c.equal("orientation", p.get("orientation"), F3["orientation"])
    c.close("fluid_density_kgm3", p.get("fluid_density_kgm3"), F3["rho"], abs_=1e-6)
    c.close("fill_fraction", p.get("fill_fraction"), F3["fill"], abs_=1e-9)
    return c.result(cid)


@check("F3.calc_pressure")
def f3_calc_pressure(ev: Evidence) -> CheckResult:
    cid = "F3.calc_pressure"
    st, bad = _f3_vessel(ev, cid)
    if bad:
        return bad
    d = st["results"].get("design") or {}
    c = _Cmp()
    c.close("static_head_kpa", d.get("static_head_kpa"), F3_STATIC, rel=0.005)
    c.close("calc_pressure_kpa", d.get("calc_pressure_kpa"), F3_PCALC * 1000.0, rel=0.002)
    return c.result(cid)


@check("F3.shell")
def f3_shell(ev: Evidence) -> CheckResult:
    cid = "F3.shell"
    st, bad = _f3_vessel(ev, cid)
    if bad:
        return bad
    s = st["results"].get("shell") or {}
    c = _Cmp()
    c.close("t_required_mm (UG-27(c)(1) + CA)", s.get("t_required_mm"), F3_T_SHELL, rel=0.02)
    c.close("t_nominal_mm (smallest listed plate)", s.get("t_nominal_mm"), F3_NOM_SHELL, abs_=1e-6)
    c.at_least("t_nominal_mm >= reference required", s.get("t_nominal_mm"), F3_T_SHELL)
    return c.result(cid)


@check("F3.heads")
def f3_heads(ev: Evidence) -> CheckResult:
    cid = "F3.heads"
    st, bad = _f3_vessel(ev, cid)
    if bad:
        return bad
    h = st["results"].get("heads") or {}
    c = _Cmp()
    c.equal("head type", h.get("type"), F3["head"])
    c.close("depth_mm (D/4)", h.get("depth_mm"), F3_DEPTH, abs_=0.5)
    c.close("t_required_mm (UG-32(d) + CA)", h.get("t_required_mm"), F3_T_HEAD, rel=0.02)
    c.close("t_nominal_mm (smallest listed plate)", h.get("t_nominal_mm"), F3_NOM_HEAD, abs_=1e-6)
    c.at_least("t_nominal_mm >= reference required", h.get("t_nominal_mm"), F3_T_HEAD)
    return c.result(cid)


@check("F3.mawp")
def f3_mawp(ev: Evidence) -> CheckResult:
    cid = "F3.mawp"
    st, bad = _f3_vessel(ev, cid)
    if bad:
        return bad
    r = st["results"]
    c = _Cmp()
    c.close("shell mawp_kpa", _get(r, "shell", "mawp_kpa"), F3_MAWP_SHELL, rel=0.02)
    c.close("head mawp_kpa", _get(r, "heads", "mawp_kpa"), F3_MAWP_HEAD, rel=0.02)
    c.close("vessel mawp_kpa (governing, at top)", r.get("mawp_kpa"), F3_MAWP, rel=0.02)
    c.at_least("vessel mawp_kpa >= design pressure", r.get("mawp_kpa"), F3["p_kpa"])
    return c.result(cid)


@check("F3.hydrotest")
def f3_hydrotest(ev: Evidence) -> CheckResult:
    cid = "F3.hydrotest"
    st, bad = _f3_vessel(ev, cid)
    if bad:
        return bad
    r = st["results"]
    c = _Cmp()
    c.close("lowest_stress_ratio", r.get("lowest_stress_ratio"), 1.0, abs_=1e-3)
    c.close("hydrotest_kpa (UG-99(b), top)", r.get("hydrotest_kpa"), F3_HYDRO, rel=0.02)
    return c.result(cid)


@check("F3.volume_mass")
def f3_volume_mass(ev: Evidence) -> CheckResult:
    cid = "F3.volume_mass"
    st, bad = _f3_vessel(ev, cid)
    if bad:
        return bad
    r = st["results"]
    vals = {k: _num(r.get(k)) for k in ("mass_empty_kg", "mass_operating_kg", "mass_full_water_kg")}
    c = _Cmp()
    c.close("volume_m3", r.get("volume_m3"), F3_VOLUME, rel=0.01)
    ok = all(v is not None for v in vals.values())
    c.true("0 < empty < operating < full of water",
           ok and 0 < vals["mass_empty_kg"] < vals["mass_operating_kg"] < vals["mass_full_water_kg"],
           measured=vals)
    if ok:
        c.close("full − empty (kg)", vals["mass_full_water_kg"] - vals["mass_empty_kg"],
                F3_VOLUME * F3["water_rho"], rel=0.01)
        c.close("operating − empty (kg)", vals["mass_operating_kg"] - vals["mass_empty_kg"],
                F3_VOLUME * F3["rho"] * F3["fill"], rel=0.01)
    return c.result(cid)


def _f3_body(ev: Evidence, cid: str) -> tuple[dict | None, CheckResult | None]:
    bad = _geometry_ready(ev, cid)
    if bad:
        return None, bad
    if len(ev.bodies) != 1:
        return None, _fail(cid, f"expected one hollow vessel body, found {len(ev.bodies)}",
                           measured=len(ev.bodies), expected=1)
    return ev.bodies[0], None


@check("F3.geometry_envelope")
def f3_geometry_envelope(ev: Evidence) -> CheckResult:
    cid = "F3.geometry_envelope"
    body, bad = _f3_body(ev, cid)
    if bad:
        return bad
    bb = _bbox(body)
    if bb is None:
        return _fail(cid, "vessel body has no bounding box")
    size = bb[2]
    od = F3["id_mm"] + 2.0 * F3_NOM_SHELL
    height = F3["tt_mm"] + 2.0 * (F3_DEPTH + F3_NOM_HEAD)
    c = _Cmp()
    c.close("X size = OD (mm)", size[0], od, abs_=2.0)
    c.close("Y size = OD (mm)", size[1], od, abs_=2.0)
    c.close("Z size = overall height (mm)", size[2], height, rel=0.005)
    c.true("body valid and closed", _sound(body),
           measured={k: body.get(k) for k in ("valid", "closed")})
    faults = geo.history_faults(ev)
    c.true("editable feature history", not faults, measured=faults or "ok")
    return c.result(cid)


@check("F3.geometry_wall")
def f3_geometry_wall(ev: Evidence) -> CheckResult:
    cid = "F3.geometry_wall"
    body, bad = _f3_body(ev, cid)
    if bad:
        return bad
    c = _Cmp()
    c.close("metal volume (mm³) — hollow at nominal thickness", body.get("volume_mm3"),
            F3_METAL_MM3, rel=0.15)
    return c.result(cid)


F3_TASK = Task(
    id="F3", version=1, domain="facilities", prompt=F3_PROMPT,
    checks=(f3_study, f3_inputs, f3_calc_pressure, f3_shell, f3_heads, f3_mawp, f3_hydrotest,
            f3_volume_mass, f3_geometry_envelope, f3_geometry_wall),
    notes=F3_NOTES)


# ═══════════════════════════════════════════════════════════════ F4 duct tree

F4_SYSTEM = "AHU-1 Supply"
F4_SEGMENTS = {
    "main": {"parent": None, "airflow_m3s": 1.0, "length_m": 10.0, "k_fittings": 0.5, "shape": "round"},
    "a": {"parent": "main", "airflow_m3s": 0.6, "length_m": 8.0, "k_fittings": 0.3, "shape": "round"},
    "b": {"parent": "main", "airflow_m3s": 0.4, "length_m": 15.0, "k_fittings": 0.3, "shape": "rect",
          "aspect_ratio": 2.0},
}
F4 = {"rate_pa_m": 1.0, "vmax_ms": 10.0, "temp_c": 20.0, "roughness_mm": 0.09,
      "rect_step_mm": 50.0, "rect_min_mm": 100.0, "aspect_band": (1.5, 2.5), "de_oversize": 1.25}
F4_RHO = orc.air_density(F4["temp_c"])
F4_MU = orc.air_viscosity(F4["temp_c"])
F4_D_EXACT = {sid: orc.equal_friction_diameter_mm(s["airflow_m3s"], F4["rate_pa_m"],
                                                   F4["roughness_mm"], F4_RHO, F4_MU)
              for sid, s in F4_SEGMENTS.items()}
F4_ROUND = {sid: orc.select_round_duct_mm(s["airflow_m3s"], F4["rate_pa_m"], F4["vmax_ms"],
                                          F4["roughness_mm"], F4_RHO, F4_MU)
            for sid, s in F4_SEGMENTS.items() if s["shape"] == "round"}
#: Layout: segment → (start point mm, unit direction); ducts on Z = 0.
F4_LAYOUT = {"main": ((0.0, 0.0, 0.0), (1, 0, 0)),
             "a": ((10_000.0, 0.0, 0.0), (0, 1, 0)),
             "b": ((10_000.0, 0.0, 0.0), (0, -1, 0))}

F4_PROMPT = f"""\
Size an equal-friction supply-air duct tree and deliver it as ONE native Chudware part saved as \
`{DELIVERABLE}` in the working directory, containing the saved duct-system sizing named \
`{F4_SYSTEM}` and the duct geometry.

Air: 20 °C at 101.325 kPa. Galvanised steel duct, absolute roughness 0.09 mm. Design criteria in \
every segment: friction rate at most 1.0 Pa/m and velocity at most 10 m/s.
Segments (a tree rooted at the fan; use exactly these segment ids):
- `main`: from the fan; 1.0 m³/s; 10 m long; fitting loss coefficient K = 0.5; round.
- `a`: branch off `main`; 0.6 m³/s; 8 m; K = 0.3; round.
- `b`: branch off `main`; 0.4 m³/s; 15 m; K = 0.3; rectangular, nominal aspect ratio 2:1 \
(width:height).
Round ducts: the smallest EN 1506 / ISO R20 preferred diameter meeting both criteria (100, 112, \
125, 140, 150, 160, 180, 200, 224, 250, 280, 300, 315, 355, 400, 450, 500, 560, 600, 630, 710, \
800, 900, 1000 mm). Rectangular ducts: sides in 50 mm steps (minimum 100 mm), rounded up from the \
equal-friction size and keeping width:height close to 2:1. Fitting losses act on each segment's \
mean velocity. Report per segment the size, velocity, Reynolds number, friction factor, friction \
and fitting pressure drops, and the critical (highest-loss) fan-to-terminal path with the fan \
static pressure it requires.

Geometry: model each segment as its own solid duct body (outer envelope at the selected size, \
no wall thickness needed), duct centrelines at Z = 0 (mm): `main` along +X from the fan outlet \
at (0, 0, 0) to the junction at (10 000, 0, 0); `a` along +Y from the junction to \
(10 000, 8 000, 0); `b` along −Y from the junction to (10 000, −15 000, 0) with its width \
horizontal."""

F4_NOTES = f"""\
Air (ideal gas, Sutherland): ρ {F4_RHO:.5f} kg/m³, μ {F4_MU:.5e} Pa·s. Exact equal-friction
diameters (Colebrook, bisection) main {F4_D_EXACT['main']:.1f}, a {F4_D_EXACT['a']:.1f},
b {F4_D_EXACT['b']:.1f} mm → round main {F4_ROUND['main']:g}, a {F4_ROUND['a']:g} mm. The rect
section of b is the agent's choice: accepted when W, H are 50 mm multiples ≥ 100, W/H in
[1.5, 2.5], friction ≤ 1.0 Pa/m (+0.5 %), v ≤ 10 m/s and Huebscher De ≤ 1.25·D*. Every
velocity/De/Re/f/Δp is recomputed from the saved section (bands 0.5–2 %); the critical path and
fan static are re-summed from the recomputed Δp (2 %). Geometry: one body per segment matching
the saved section (±1 % + 1 mm) and the prescribed centreline extent (± section size)."""


def _f4_study(ev: Evidence, cid: str) -> tuple[dict | None, CheckResult | None]:
    return _the_study(ev, cid, "duct_system", F4_SYSTEM)


def _f4_rows(st: dict) -> dict[str, dict]:
    return {str(r.get("id")): r for r in _get(st["results"], "segments") or [] if isinstance(r, dict)}


def _f4_reference(row: dict, sid: str) -> dict:
    """Pressure terms recomputed at the SAVED section of segment ``sid``."""
    seg = F4_SEGMENTS[sid]
    kw = {"density": F4_RHO, "viscosity": F4_MU, "roughness_mm": F4["roughness_mm"]}
    if seg["shape"] == "round":
        return orc.duct_segment(seg["airflow_m3s"], seg["length_m"], seg["k_fittings"],
                                diameter_mm=float(row["diameter_mm"]), **kw)
    return orc.duct_segment(seg["airflow_m3s"], seg["length_m"], seg["k_fittings"],
                            width_mm=float(row["width_mm"]), height_mm=float(row["height_mm"]), **kw)


def _f4_sections_ok(rows: dict[str, dict]) -> list[str]:
    bad = []
    for sid, seg in F4_SEGMENTS.items():
        r = rows.get(sid) or {}
        keys = ("diameter_mm",) if seg["shape"] == "round" else ("width_mm", "height_mm")
        if any((_num(r.get(k)) or 0.0) <= 0 for k in keys):
            bad.append(f"{sid}: no positive {'/'.join(keys)}")
    return bad


@check("F4.study")
def f4_study(ev: Evidence) -> CheckResult:
    st, bad = _f4_study(ev, "F4.study")
    if bad:
        return bad
    rows = _f4_rows(st)
    c = _Cmp()
    c.equal("segment ids", sorted(rows), sorted(F4_SEGMENTS))
    c.true("positive sizes", not _f4_sections_ok(rows), measured=_f4_sections_ok(rows) or "ok")
    if c.failed:
        return c.result("F4.study")
    fin = _finite_results("F4.study", st["results"], ("segments", "air"))
    if fin.status is not Status.PASS:
        return fin
    c.true("fan_static_pa finite and positive", (_num(st["results"].get("fan_static_pa")) or 0) > 0,
           measured=st["results"].get("fan_static_pa"))
    return c.result("F4.study")


@check("F4.inputs")
def f4_inputs(ev: Evidence) -> CheckResult:
    cid = "F4.inputs"
    st, bad = _f4_study(ev, cid)
    if bad:
        return bad
    p = st["params"]
    c = _Cmp()
    c.equal("method", p.get("method", "equal_friction"), "equal_friction")
    c.close("friction_pa_per_m", p.get("friction_pa_per_m"), F4["rate_pa_m"], abs_=1e-9)
    c.close("max_velocity_ms", p.get("max_velocity_ms"), F4["vmax_ms"], abs_=1e-9)
    c.close("air_temp_c", p.get("air_temp_c"), F4["temp_c"], abs_=1e-9)
    c.close("roughness_mm", p.get("roughness_mm"), F4["roughness_mm"], abs_=1e-9)
    segs = {str(s.get("id")): s for s in p.get("segments") or [] if isinstance(s, dict)}
    c.equal("segment ids", sorted(segs), sorted(F4_SEGMENTS))
    for sid, want in F4_SEGMENTS.items():
        s = segs.get(sid, {})
        c.equal(f"{sid} parent", s.get("parent") or None, want["parent"])
        c.close(f"{sid} airflow_m3s", s.get("airflow_m3s"), want["airflow_m3s"], abs_=1e-9)
        c.close(f"{sid} length_m", s.get("length_m"), want["length_m"], abs_=1e-9)
        c.close(f"{sid} k_fittings", s.get("k_fittings", 0.0), want["k_fittings"], abs_=1e-9)
        c.equal(f"{sid} shape", s.get("shape", "round"), want["shape"])
        if want["shape"] == "rect":
            c.close(f"{sid} aspect_ratio", s.get("aspect_ratio"), want["aspect_ratio"], abs_=1e-9)
    children = sum(s["airflow_m3s"] for s in F4_SEGMENTS.values() if s["parent"] == "main")
    c.at_least("main carries its branches' flow (continuity)",
               _get(segs, "main", "airflow_m3s"), children, slack=1e-9)
    return c.result(cid)


@check("F4.air")
def f4_air(ev: Evidence) -> CheckResult:
    cid = "F4.air"
    st, bad = _f4_study(ev, cid)
    if bad:
        return bad
    air = st["results"].get("air") or {}
    c = _Cmp()
    c.close("density_kgm3", air.get("density_kgm3"), F4_RHO, rel=0.01)
    c.close("viscosity_pas", air.get("viscosity_pas"), F4_MU, rel=0.02)
    return c.result(cid)


@check("F4.sizes")
def f4_sizes(ev: Evidence) -> CheckResult:
    cid = "F4.sizes"
    st, bad = _f4_study(ev, cid)
    if bad:
        return bad
    rows = _f4_rows(st)
    broken = _f4_sections_ok(rows)
    if broken or sorted(rows) != sorted(F4_SEGMENTS):
        return _fail(cid, "; ".join(broken) or "segment ids differ", measured=sorted(rows))
    c = _Cmp()
    for sid, seg in F4_SEGMENTS.items():
        r = rows[sid]
        ref = _f4_reference(r, sid)
        if seg["shape"] == "round":
            c.close(f"{sid} diameter_mm (smallest preferred size)", r.get("diameter_mm"),
                    F4_ROUND[sid], abs_=1e-6)
        else:
            w, h = float(r["width_mm"]), float(r["height_mm"])
            step = F4["rect_step_mm"]
            c.true(f"{sid} sides 50 mm steps >= 100 mm",
                   all(abs(x / step - round(x / step)) < 1e-9 and x >= F4["rect_min_mm"] for x in (w, h)),
                   measured=[w, h])
            lo, hi = F4["aspect_band"]
            c.true(f"{sid} width/height in [{lo}, {hi}]", lo <= w / h <= hi, measured=round(w / h, 4))
            c.at_most(f"{sid} equivalent diameter <= 1.25·D* (not oversized)",
                      ref["equivalent_diameter_mm"], F4["de_oversize"] * F4_D_EXACT[sid])
        c.at_most(f"{sid} friction rate (Pa/m)", ref["friction_rate_pa_per_m"],
                  F4["rate_pa_m"] * 1.005)
        c.at_most(f"{sid} velocity (m/s)", ref["velocity_ms"], F4["vmax_ms"] + 1e-6)
    return c.result(cid)


@check("F4.section_properties")
def f4_section_properties(ev: Evidence) -> CheckResult:
    cid = "F4.section_properties"
    st, bad = _f4_study(ev, cid)
    if bad:
        return bad
    rows = _f4_rows(st)
    if _f4_sections_ok(rows) or sorted(rows) != sorted(F4_SEGMENTS):
        return _fail(cid, "saved segments incomplete", measured=sorted(rows))
    c = _Cmp()
    for sid in F4_SEGMENTS:
        r, ref = rows[sid], _f4_reference(rows[sid], sid)
        c.close(f"{sid} velocity_ms (Q/A)", r.get("velocity_ms"), ref["velocity_ms"], rel=0.005)
        c.close(f"{sid} equivalent_diameter_mm", r.get("equivalent_diameter_mm"),
                ref["equivalent_diameter_mm"], rel=0.005)
    return c.result(cid)


@check("F4.pressure_terms")
def f4_pressure_terms(ev: Evidence) -> CheckResult:
    cid = "F4.pressure_terms"
    st, bad = _f4_study(ev, cid)
    if bad:
        return bad
    rows = _f4_rows(st)
    if _f4_sections_ok(rows) or sorted(rows) != sorted(F4_SEGMENTS):
        return _fail(cid, "saved segments incomplete", measured=sorted(rows))
    c = _Cmp()
    for sid in F4_SEGMENTS:
        r, ref = rows[sid], _f4_reference(rows[sid], sid)
        c.close(f"{sid} reynolds", r.get("reynolds"), ref["reynolds"], rel=0.015)
        c.close(f"{sid} friction_factor", r.get("friction_factor"), ref["friction_factor"], rel=0.015)
        c.close(f"{sid} dp_friction_pa", r.get("dp_friction_pa"), ref["dp_friction_pa"], rel=0.02)
        c.close(f"{sid} dp_fittings_pa", r.get("dp_fittings_pa"), ref["dp_fittings_pa"], rel=0.02)
        c.close(f"{sid} dp_total_pa", r.get("dp_total_pa"), ref["dp_total_pa"], rel=0.02)
    return c.result(cid)


@check("F4.critical_path")
def f4_critical_path(ev: Evidence) -> CheckResult:
    cid = "F4.critical_path"
    st, bad = _f4_study(ev, cid)
    if bad:
        return bad
    rows = _f4_rows(st)
    if _f4_sections_ok(rows) or sorted(rows) != sorted(F4_SEGMENTS):
        return _fail(cid, "saved segments incomplete", measured=sorted(rows))
    tree = [{"id": sid, "parent": seg["parent"],
             "dp_total_pa": _f4_reference(rows[sid], sid)["dp_total_pa"]}
            for sid, seg in F4_SEGMENTS.items()]
    path, fan = orc.critical_path(tree)
    c = _Cmp()
    c.equal("critical_path", list(st["results"].get("critical_path") or []), path)
    c.close("fan_static_pa", st["results"].get("fan_static_pa"), fan, rel=0.02)
    return c.result(cid)


@check("F4.geometry")
def f4_geometry(ev: Evidence) -> CheckResult:
    cid = "F4.geometry"
    bad = _geometry_ready(ev, cid)
    if bad:
        return bad
    st, bad = _f4_study(ev, cid)
    if bad:
        if bad.status is not Status.FAIL:
            return bad
        return _fail(cid, f"duct sections unknown without the saved sizing: {bad.detail}")
    rows = _f4_rows(st)
    if _f4_sections_ok(rows) or sorted(rows) != sorted(F4_SEGMENTS):
        return _fail(cid, "saved segments incomplete", measured=sorted(rows))
    c = _Cmp()
    c.equal("body count (one per segment)", len(ev.bodies), len(F4_SEGMENTS))
    for sid, seg in F4_SEGMENTS.items():
        r = rows[sid]
        start, d = F4_LAYOUT[sid]
        length = seg["length_m"] * 1000.0
        end = [start[i] + d[i] * length for i in range(3)]
        axis = next(i for i in range(3) if d[i])
        if seg["shape"] == "round":
            cross = [float(r["diameter_mm"])] * 3
        else:
            w, h = float(r["width_mm"]), float(r["height_mm"])
            cross = [w, w, h]  # width horizontal (X or Y), height along Z
        lo, hi, tol = [], [], []
        for i in range(3):
            if i == axis:
                lo.append(min(start[i], end[i]))
                hi.append(max(start[i], end[i]))
                tol.append(max(cross))
            else:
                half = cross[i] / 2.0
                lo.append(start[i] - half)
                hi.append(start[i] + half)
                tol.append(0.01 * cross[i] + 1.0)
        hits = [b for b in ev.bodies if _box_matches(b, lo, hi, tol)]
        c.true(f"{sid} body at its section and centreline", len(hits) == 1,
               measured=[[[round(v, 1) for v in _bbox(b)[0]], [round(v, 1) for v in _bbox(b)[1]]]
                         for b in ev.bodies if _bbox(b)],
               expected={"min": [round(v, 1) for v in lo], "max": [round(v, 1) for v in hi]})
        if len(hits) == 1:
            c.true(f"{sid} body valid and closed", _sound(hits[0]),
                   measured={k: hits[0].get(k) for k in ("valid", "closed")})
    faults = geo.history_faults(ev)
    c.true("editable feature history", not faults, measured=faults or "ok")
    return c.result(cid)


F4_TASK = Task(
    id="F4", version=1, domain="facilities", prompt=F4_PROMPT,
    checks=(f4_study, f4_inputs, f4_air, f4_sizes, f4_section_properties, f4_pressure_terms,
            f4_critical_path, f4_geometry),
    notes=F4_NOTES)


TASKS: tuple[Task, ...] = (F1_TASK, F2_TASK, F3_TASK, F4_TASK)
