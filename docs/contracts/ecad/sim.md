# Contract: Circuit Simulation — v1

> Native Modified-Nodal-Analysis simulator (`engine/sim/circuit.py` + `devices.py`), GPL-clean
> (numpy/scipy, BSD). Runs in the sim worker. Inputs: an internal `Circuit` (from a netlist or a
> SPICE deck) + a `study`. The mandate is to be **better-coupled and more transparent than KiCad's
> ngspice integration**, not to out-feature SPICE on device count.

## 1. Internal Circuit (output of `netlist_to_spice_circuit` / `parse_spice`)

```json
{
  "nodes": ["0","VCC","N$3", ...],          // "0" is ground (forced)
  "devices": [Device, ...],
  "models": {modelname: {...params}},       // .model cards
  "params": {"TEMP":27}
}
Device: {"kind":"R|L|C|V|I|D|Q|M|E|F|G|H|S|OPAMP", "name":"R1",
         "nodes":["N$3","GND"], "value":10000.0|null, "model":"D1N4148"|null,
         "params":{...}}
```

**Independent-source `params` encoding (NORMATIVE — the parser↔solver boundary).** A V/I source's
excitation lives in `params` under these exact keys (the SPICE parser emits them; the solver reads
them — keep them in sync):
- `params["dc"]` : float DC level (or carry it in the device `value`; the solver uses `value` if
  non-null else `params["dc"]`).
- `params["ac"]` : `{"mag":float, "phase":float}` — small-signal AC excitation (lowercase `ac`).
- `params["waveform"]` : `{"type":"DC|PULSE|SIN|PWL", ...}` — transient waveform. Field names by type:
  `PULSE{v1,v2,td,tr,tf,pw,per}`, `SIN{vo,va,f,td,theta}`, `PWL{points:[[t,v],...]}`.
Capacitor/inductor initial conditions use `params["ic"]`.

## 2. Device models (`devices.py`)

Each device contributes a **stamp** (linear: into G/C; nonlinear: a companion model linearized each
Newton iteration). v1 device set + key params:

| kind | model | params |
|---|---|---|
| R | linear conductance | `value` Ω (0 → tiny series R guard) |
| C | companion (trap/BE) | `value` F, `ic` V |
| L | companion + current state | `value` H, `ic` A |
| V | independent voltage (extra MNA branch) | DC; or `PULSE(v1 v2 td tr tf pw per)`, `SIN(vo va f td)`, `PWL(t v ...)`, `AC mag phase` |
| I | independent current | same waveform grammar |
| D | diode | Shockley `IS,N,RS,CJO,VJ,BV` (1N4148/LED defaults) |
| Q | BJT | Ebers-Moll + Early: `IS,BF,BR,VAF,RE,RC,RB,CJE,CJC` |
| M | MOSFET | level-1: `VTO,KP,LAMBDA,W,L,GAMMA,PHI`; n/p |
| E,G | VCVS, VCCS | `gain` |
| F,H | CCCS, CCVS | `gain`, control branch |
| S | voltage-controlled switch | `von,voff,ron,roff` |
| OPAMP | ideal (∞ gain, diff-in) + 1-pole macromodel | `gain,gbw,vsat` |

Models are registered like CAD feature compilers: `@device("D")` → `class Diode(Device)` with
`stamp_dc(self, sys, x)`, `stamp_tran(self, sys, x, dt, hist)`, `stamp_ac(self, sys, omega)`.

## 3. Study schema (input)

```json
{
  "analyses": [
    {"type":"op"},
    {"type":"dc", "source":"V1", "start":0, "stop":5, "step":0.05},
    {"type":"tran", "tstep":1e-6, "tstop":5e-3, "tstart":0, "uic":false},
    {"type":"ac", "sweep":"dec", "points":20, "fstart":1, "fstop":1e6}
  ],
  "probes": ["V(OUT)", "V(N$3)", "I(V1)", "V(OUT,IN)"],   // node V, branch I, differential
  "options": {"reltol":1e-3, "abstol":1e-12, "vntol":1e-6, "gmin":1e-12,
              "maxiter":100, "method":"trap|gear", "temp":27}
}
```

## 4. Results (output)

```json
{
  "op": {"V(OUT)":2.49, "V(N$3)":1.66, "I(V1)":-0.0031},
  "dc": {"sweep":"V1", "x":[...], "traces":{"V(OUT)":[...], ...}},
  "tran": {"x_t":[...], "traces":{"V(OUT)":[...], ...}},
  "ac": {"x_f":[...], "mag":{"V(OUT)":[...]}, "phase_deg":{"V(OUT)":[...]}},
  "log": ["op: converged in 6 iters", ...],
  "convergence": {"op":true, "tran":true},
  "ms": 41.2
}
```

For the GUI, transient/AC traces are *also* emitted as **LINS/SCLR overlay chunks** (reusing the
FEA/CFD overlay path) so waveforms render without a new pipeline; node-voltage operating points can
be drawn on the schematic.

## 5. Numerical contract (normative)

- Newton with damping; **gmin stepping** then **source stepping** fallback on DC non-convergence.
- Transient: trapezoidal default with the standard LTE-based time-step control; backward-Euler
  bootstrap for the first step and after breakpoints (source edges added as breakpoints).
- Singular-matrix / non-convergence → return `convergence.*: false` + a diagnostic in `log`
  (never NaNs silently). Matrix solve via `scipy.sparse.linalg.splu` (LU) with a dense fallback.
- **Accuracy gates (tests, vs closed form, ≤1% unless noted):** resistive divider (exact);
  RC step τ=RC; RL step; series-RLC ring frequency & damping; diode clamp clamp-voltage; inverting
  op-amp gain −Rf/Rin; common-emitter DC bias; RC low-pass −3 dB corner & −20 dB/dec slope (AC).
