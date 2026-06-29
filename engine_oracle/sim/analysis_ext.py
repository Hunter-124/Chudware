"""Extended circuit analyses layered on top of the native MNA simulator (``circuit.py``).

These are *additive* study types - callers passing the existing ``op``/``dc``/``tran``/``ac``/``step``
analyses behave identically. ``circuit.simulate`` dispatches the new ``type`` values to the runners
here and writes new top-level result keys; nothing in the frozen Circuit/result shapes changes.

What this module adds (the "better than KiCad's ngspice integration" depth analyses):

* ``four``  - Fourier / THD on a completed transient trace. Resamples the last ``periods`` cycles of
  a probe at ``f0`` onto a uniform grid, runs a real FFT, and reports harmonic magnitudes
  (H1..Hn), phases, and the total-harmonic-distortion percentage.
* ``mc``    - Monte-Carlo tolerance analysis. Re-runs an inner analysis ``runs`` times, perturbing
  each device ``value`` by a deterministic gaussian draw inside its per-kind tolerance, and
  collects a scalar (op value / tran final / ac peak) per run -> mean/std/min/max. Seeded with
  numpy ``default_rng(seed)`` so two runs with the same study are bit-identical.
* ``temp``  - Temperature sweep. Rebuilds the sim with ``params["TEMP"]`` set per point and re-runs
  the inner analysis. Diode IS / thermal voltage and resistor ``tc1``/``tc2`` tempco are
  temperature-aware (see ``devices.py``), so results actually move with temperature.
* ``noise`` - Resistor-thermal output-noise spectral density referred to ``output`` via the AC
  transfer impedance at each frequency, plus the band-integrated RMS noise. Scope note: only
  resistor thermal noise (4kTR) is modeled; device shot/flicker noise is intentionally omitted
  (documented, honest scope).

The runners take a ``simulate`` callback (injected by ``circuit.py``) so this module never imports
``circuit`` at module load time, keeping the dependency one-directional and additive.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np

# Boltzmann constant (J/K). Local copy so this module is import-light.
_K = 1.380649e-23

# np.trapz was removed in numpy 2.x in favour of np.trapezoid; support both.
_trapz = getattr(np, "trapezoid", None) or getattr(np, "trapz")


# =============================================================================
# Fourier / THD  (study type "four")
# =============================================================================

def run_fourier(tran_result: dict, an: dict, log: list) -> Optional[dict]:
    """Fourier / THD on a completed transient trace.

    ``tran_result`` is the sim.md sec.4 ``tran`` block ({"x_t":[...], "traces":{probe:[...]}}).
    ``an`` carries ``{"f0":float, "probe":str, "nharm":int=9, "periods":int=auto}``.

    Returns ``{"probe", "f0", "harmonics":[(n, mag, phase_deg), ...], "thd_pct", "dc"}`` or None if
    the trace is unusable (too short / probe missing). The harmonic magnitudes are single-sided
    amplitudes (volts), H1 is the fundamental; THD% = 100 * sqrt(sum_{n>=2} Hn^2) / H1.
    """
    if not tran_result:
        log.append("four: no transient result to analyze")
        return None
    f0 = float(an.get("f0", 0.0))
    if f0 <= 0.0:
        log.append("four: f0 must be > 0")
        return None
    probe = an.get("probe")
    traces = tran_result.get("traces", {})
    if probe is None:
        # Default to the first available trace.
        probe = next(iter(traces), None)
    if probe is None or probe not in traces:
        log.append(f"four: probe '{probe}' not in transient traces {list(traces)}")
        return None
    nharm = int(an.get("nharm", 9))
    nharm = max(1, min(nharm, 64))

    t = np.asarray(tran_result.get("x_t", []), dtype=float)
    y = np.asarray(traces[probe], dtype=float)
    if t.size < 4 or y.size != t.size:
        log.append("four: transient trace too short for Fourier")
        return None

    period = 1.0 / f0
    tspan = float(t[-1] - t[0])
    if tspan < period:
        log.append(f"four: trace span {tspan:.3e}s shorter than one period {period:.3e}s")
        return None
    # Use the last whole number of periods (default: as many as fit, capped for resolution).
    max_periods = int(tspan / period)
    periods = int(an.get("periods", max_periods))
    periods = max(1, min(periods, max_periods))
    t_end = float(t[-1])
    t_start = t_end - periods * period

    # Resample onto a uniform grid: a power-of-two count per the requested period window so the FFT
    # bins land exactly on harmonics of f0. Oversample to comfortably resolve nharm.
    samples_per_period = 1
    target = 4 * (nharm + 1)
    while samples_per_period < target:
        samples_per_period *= 2
    nfft = samples_per_period * periods
    # Uniform grid over exactly `periods` periods (exclude the duplicate endpoint).
    tg = np.linspace(t_start, t_end, nfft, endpoint=False)
    yg = np.interp(tg, t, y)

    spec = np.fft.rfft(yg)
    scale = 2.0 / nfft  # single-sided amplitude
    mags = np.abs(spec) * scale
    if mags.size > 0:
        mags[0] *= 0.5  # DC bin is not doubled
    phases = np.degrees(np.angle(spec))

    dc = float(mags[0]) if mags.size else 0.0
    harmonics: list[tuple] = []
    h_amps: list[float] = []
    for n in range(1, nharm + 1):
        bin_idx = n * periods  # harmonic n lands on bin n*periods
        if bin_idx >= mags.size:
            break
        m = float(mags[bin_idx])
        p = float(phases[bin_idx])
        harmonics.append((n, m, p))
        h_amps.append(m)

    if not h_amps:
        log.append("four: no harmonics resolvable from trace")
        return None
    h1 = h_amps[0]
    rest = math.sqrt(sum(a * a for a in h_amps[1:]))
    thd_pct = (rest / h1 * 100.0) if h1 > 1e-30 else 0.0

    log.append(f"four: {probe} f0={f0:g}Hz H1={h1:.5g} THD={thd_pct:.4f}% "
               f"(over {periods} periods, {nfft} pts)")
    return {
        "probe": probe,
        "f0": f0,
        "dc": dc,
        "harmonics": harmonics,
        "thd_pct": thd_pct,
    }


# =============================================================================
# Monte-Carlo tolerance  (study type "mc")
# =============================================================================

# Default per-kind tolerances (fractional, 1-sigma interpreted below) when the study omits "tol".
_MC_DEFAULT_TOL = {"R": 0.01, "C": 0.05, "L": 0.05, "V": 0.0, "I": 0.0}


def _scalar_from_result(res: dict, probe: str) -> Optional[float]:
    """Extract the key scalar for a probe from a single inner-analysis result.

    Priority: op value -> tran final value -> ac peak magnitude. Returns None if nothing usable.
    """
    op = res.get("op")
    if isinstance(op, dict) and probe in op:
        v = op[probe]
        if v is not None and math.isfinite(v):
            return float(v)
    tran = res.get("tran")
    if isinstance(tran, dict):
        tr = tran.get("traces", {})
        if probe in tr and tr[probe]:
            v = tr[probe][-1]
            if v is not None and math.isfinite(v):
                return float(v)
    ac = res.get("ac")
    if isinstance(ac, dict):
        mag = ac.get("mag", {})
        if probe in mag and mag[probe]:
            arr = [m for m in mag[probe] if m is not None and math.isfinite(m)]
            if arr:
                return float(max(arr))
    dc = res.get("dc")
    if isinstance(dc, dict):
        tr = dc.get("traces", {})
        if probe in tr and tr[probe]:
            finite = [v for v in tr[probe] if v is not None and math.isfinite(v)]
            if finite:
                return float(finite[-1])
    return None


def run_monte_carlo(circuit: dict, options: dict, an: dict, probes: list, log: list,
                    simulate_fn: Callable, seed_default: int) -> dict:
    """Monte-Carlo tolerance analysis.

    ``an`` = ``{"type":"mc", "runs":N, "tol":{"R":0.05,...}|float, "analysis":{...},
    "probe":"V(OUT)", "seed":int}``. Each run deep-copies the circuit, perturbs every device
    ``value`` by a gaussian draw ``value * (1 + tol_kind * z)`` (z ~ N(0,1)), and runs the single
    inner ``analysis`` via ``simulate_fn``. The probe scalar is collected per run.

    Determinism: a numpy ``default_rng(seed)`` is created once; the same seed -> identical draws ->
    identical results. ``tol`` may be a dict per-kind or a single float applied to all R/C/L.
    """
    import copy

    runs = int(an.get("runs", 100))
    runs = max(1, min(runs, 100000))
    inner = an.get("analysis") or {"type": "op"}
    probe = an.get("probe")
    if probe is None:
        probe = probes[0] if probes else None
    if probe is None:
        log.append("mc: no probe specified and none default")
        return {"values": [], "mean": float("nan"), "std": float("nan"),
                "min": float("nan"), "max": float("nan"), "runs": 0}

    tol_spec = an.get("tol")
    if isinstance(tol_spec, (int, float)):
        tol = {"R": float(tol_spec), "C": float(tol_spec), "L": float(tol_spec)}
    elif isinstance(tol_spec, dict):
        tol = {k: float(v) for k, v in tol_spec.items()}
    else:
        tol = dict(_MC_DEFAULT_TOL)

    seed = int(an.get("seed", seed_default))
    rng = np.random.default_rng(seed)

    # Devices eligible for perturbation: those with a numeric value and a nonzero tolerance.
    base_devices = circuit.get("devices", [])

    values: list[float] = []
    inner_probes = [probe]
    n_ok = 0
    for _ in range(runs):
        c = copy.deepcopy(circuit)
        for d in c.get("devices", []):
            kind = d.get("kind")
            t = tol.get(kind, 0.0)
            v = d.get("value")
            if t and v is not None:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                z = float(rng.standard_normal())
                d["value"] = fv * (1.0 + t * z)
        run_study = {"analyses": [inner], "probes": inner_probes, "options": options}
        res = simulate_fn(c, run_study)
        sc = _scalar_from_result(res, probe)
        if sc is not None and math.isfinite(sc):
            values.append(sc)
            n_ok += 1

    if not values:
        log.append(f"mc: {runs} runs produced no finite samples for {probe}")
        return {"values": [], "mean": float("nan"), "std": float("nan"),
                "min": float("nan"), "max": float("nan"), "runs": 0,
                "probe": probe, "seed": seed}

    arr = np.asarray(values, dtype=float)
    out = {
        "probe": probe,
        "seed": seed,
        "runs": n_ok,
        "values": [float(v) for v in arr],
        "mean": float(arr.mean()),
        "std": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
        "min": float(arr.min()),
        "max": float(arr.max()),
        "tol": tol,
    }
    log.append(f"mc: {n_ok}/{runs} runs {probe} mean={out['mean']:.6g} std={out['std']:.4g} "
               f"(seed={seed})")
    return out


# =============================================================================
# Temperature sweep  (study type "temp")
# =============================================================================

def run_temp_sweep(circuit: dict, options: dict, an: dict, probes: list, log: list,
                   simulate_fn: Callable) -> dict:
    """Temperature sweep: re-run an inner analysis at each TEMP value.

    ``an`` = ``{"type":"temp", "values":[-40,25,85], "analysis":{...}}``. Each point deep-copies the
    circuit, sets ``params["TEMP"]`` (and ``options["temp"]``), and runs the inner analysis via
    ``simulate_fn``. Temp-aware devices (see devices.py): diode (IS + Vt) and resistor (tc1/tc2).

    Returns ``{"values":[...], "points":[{"temp":T, <inner sim.md sec.4 result>}, ...]}``.
    """
    import copy

    vals = an.get("values")
    if vals is None:
        start = float(an.get("start", 27.0))
        stop = float(an.get("stop", 27.0))
        step = float(an.get("step", 0.0))
        vals = [start]
        if step:
            n = int(round((stop - start) / step)) + 1
            vals = [start + i * step for i in range(max(n, 1))]
    temps = [float(v) for v in vals]
    inner = an.get("analysis") or {"type": "op"}

    points: list[dict] = []
    for tc in temps:
        c = copy.deepcopy(circuit)
        c.setdefault("params", {})["TEMP"] = tc
        opt = dict(options)
        opt["temp"] = tc
        run_study = {"analyses": [inner], "probes": list(probes), "options": opt}
        res = simulate_fn(c, run_study)
        entry: dict = {"temp": tc}
        for key in ("op", "dc", "tran", "ac", "convergence"):
            if key in res:
                entry[key] = res[key]
        points.append(entry)

    log.append(f"temp: swept {len(temps)} points {temps}")
    return {"values": temps, "points": points}


# =============================================================================
# Basic noise  (study type "noise") - resistor thermal noise only
# =============================================================================

def run_noise(sim, an: dict, log: list, helpers: dict) -> tuple[dict, bool]:
    """Resistor-thermal output-noise analysis.

    ``an`` = ``{"type":"noise", "output":"V(OUT)", "source":"V1"|None, "fstart","fstop","points",
    "sweep":"dec"|"oct"|"lin"}``.

    Method (textbook ngspice ``.noise`` for the thermal-only case): at each frequency, for every
    resistor R_k, the thermal noise is a current source ``i_n^2 = 4kT/R_k`` in parallel with the
    resistor. The contribution to the output PSD is ``|Z_k(f)|^2 * 4kT/R_k`` where ``Z_k(f)`` is the
    transfer impedance from a unit current injected across R_k's nodes to the output voltage. We get
    ``Z_k`` by solving the complex AC MNA with a 1 A source stamped across R_k's terminals (the same
    linear network the ``ac`` analysis builds). Summing over resistors gives the total output-noise
    PSD; the integrated RMS is sqrt(integral of PSD over the band).

    Scope (honest): ONLY resistor thermal noise (4kTR) is modeled. Diode shot noise, BJT/MOSFET
    shot+flicker, and 1/f noise are intentionally omitted. For passive/RC/RL/divider networks this
    is exact; for active circuits it captures the resistor-noise floor only. ``source`` is accepted
    for API compatibility (input-referred noise = output / |gain|) but input-referral is not
    computed here.

    Returns ``({"x_f":[...], "output", "onoise_spectrum":[V/sqrtHz...], "total_noise_rms":float,
    "per_resistor":{name: contribution_rms,...}}, converged)``.
    """
    new_system = helpers["new_system"]
    solve_dc_op = helpers["solve_dc_op"]
    eval_probe = helpers["eval_probe"]
    freq_grid = helpers["freq_grid"]

    output = an.get("output") or "V(OUT)"
    freqs = freq_grid(an)
    temp_c = float(sim.params.get("TEMP", sim.params.get("temp", 27.0)))
    tkelvin = temp_c + 273.15
    four_kt = 4.0 * _K * tkelvin

    # Resistors and their nominal resistance (post-tempco, via the device).
    resistors = [d for d in sim.devices if d.kind == "R"]
    if not resistors:
        log.append("noise: no resistors -> no thermal-noise sources modeled")
        return {"x_f": list(map(float, freqs)), "output": output,
                "onoise_spectrum": [0.0] * len(freqs), "total_noise_rms": 0.0,
                "per_resistor": {}}, True

    # DC op for linearization of any nonlinear devices.
    x_op = None
    if sim.has_nonlinear():
        x_op, _ok = solve_dc_op(sim, log)

    mna = new_system(complex_mode=True)
    n = mna.dim
    x_op_real = x_op if x_op is not None else np.zeros(n)

    onoise_psd = np.zeros(len(freqs))                            # V^2/Hz at the output (total)
    converged = True

    # Pre-resolve each resistor's node rows + (tempco-aware) resistance.
    res_nodes = []
    for d in resistors:
        a = mna.n(d.node_names[0])
        b = mna.n(d.node_names[1])
        rval = d._r() if hasattr(d, "_r") else (float(d.value) if d.value else 1e-9)
        res_nodes.append((d.name, a, b, rval))

    # Per-resistor output-noise spectra (V^2/Hz); summed gives the total.
    per_spec = {name: np.zeros(len(freqs)) for (name, _a, _b, _r) in res_nodes}

    for fi, f in enumerate(freqs):
        omega = 2.0 * math.pi * f
        for (rname, a, b, rval) in res_nodes:
            if rval <= 0.0:
                continue
            # Build the linear AC network (no independent excitation), then inject 1 A across R_k to
            # read the transfer impedance Z_k = V(out)/I. The same convention as ISource: push the
            # source current from node a to node b.
            mna.begin_solve()
            mna.src_scales = {}
            for dev in sim.devices:
                try:
                    dev.stamp_ac(mna, x_op_real, omega)
                except Exception:  # noqa: BLE001 - a device with no AC stamp contributes nothing
                    pass
            mna.stamp_I(a, 1.0)
            mna.stamp_I(b, -1.0)
            xsol, status = mna.solve(omega=omega)
            if xsol is None:
                converged = False
                continue
            ztrans = eval_probe(sim, mna, xsol, output)   # V(out) per 1 A = transfer impedance
            if ztrans is None:
                continue
            contrib = (abs(complex(ztrans)) ** 2) * (four_kt / rval)  # |Z|^2 * i_n^2  (V^2/Hz)
            per_spec[rname][fi] = contrib
            onoise_psd[fi] += contrib

    # Integrated RMS over the band: sqrt(integral of PSD (V^2/Hz) df).
    f_arr = np.asarray(freqs, dtype=float)
    if f_arr.size >= 2:
        total_var = float(_trapz(onoise_psd, f_arr))
        per_res_rms = {name: math.sqrt(max(float(_trapz(spec, f_arr)), 0.0))
                       for name, spec in per_spec.items()}
    else:
        total_var = 0.0
        per_res_rms = {name: 0.0 for name in per_spec}
    total_rms = math.sqrt(max(total_var, 0.0))

    onoise_density = np.sqrt(np.maximum(onoise_psd, 0.0))  # V/sqrt(Hz)

    log.append(f"noise: {output} resistor-thermal only, {len(freqs)} pts "
               f"{f_arr[0]:.3g}..{f_arr[-1]:.3g}Hz, integrated={total_rms:.4g} Vrms")
    return {
        "x_f": list(map(float, f_arr)),
        "output": output,
        "onoise_spectrum": [float(v) for v in onoise_density],
        "onoise_psd": [float(v) for v in onoise_psd],
        "total_noise_rms": total_rms,
        "per_resistor": {k: float(v) for k, v in per_res_rms.items()},
        "temp_c": temp_c,
    }, converged
