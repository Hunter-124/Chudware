"""Device companion-model stamps for the native MNA circuit simulator.

Each device kind is a small class registered under its single-letter ``kind`` via the
``@device("R")`` decorator (mirroring the CAD feature-compiler registry in
``engine/doc/features/base.py``). A device exposes up to three stamps:

  * ``stamp_dc(self, mna, x)``            — DC / Newton iteration (linearized about ``x``)
  * ``stamp_tran(self, mna, x, dt, hist)``— transient companion (trapezoidal / backward-Euler)
  * ``stamp_ac(self, mna, x_op, omega)``  — complex small-signal stamp about the DC op-point

``mna`` is a :class:`MnaSystem` accumulator (see ``circuit.py``) that exposes the primitives a
stamp needs:

  * ``mna.n(name) -> int``                 node name -> matrix row index (ground "0" -> -1)
  * ``mna.add_branch(name) -> int``         allocate an extra MNA branch row, return its index
  * ``mna.stamp_G(i, j, g)``                add conductance/admittance between matrix rows
  * ``mna.stamp_C(i, j, c)``                add capacitance (transient/AC reactive) term
  * ``mna.stamp_I(i, val)``                 add a current into the RHS at row ``i``
  * ``mna.x_at(i) -> float``                read the solution at row ``i`` (ground -> 0.0)

A negative matrix index means ground and stamping into it is a silent no-op, so the device code
need not special-case node "0".

Waveform sources (V/I) own the time/frequency-dependent value via :func:`source_value` and
:func:`source_breakpoints` so the transient driver can add edges as breakpoints.

GPL-clean: pure numpy/scipy, BSD-style maths only.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np

# --------------------------------------------------------------------------- registry

_DEVICE_REGISTRY: dict[str, type["Device"]] = {}


def device(kind: str) -> Callable[[type["Device"]], type["Device"]]:
    """Decorator: register a Device subclass under its single-letter ``kind``."""

    def deco(cls: type["Device"]) -> type["Device"]:
        if kind in _DEVICE_REGISTRY:
            raise ValueError(f"device already registered for kind '{kind}'")
        cls.kind = kind
        _DEVICE_REGISTRY[kind] = cls
        return cls

    return deco


def make_device(spec: dict, models: dict, params: dict) -> "Device":
    """Instantiate the registered Device for ``spec['kind']``.

    Raises :class:`DeviceError` for an unknown kind so the solver can surface a clear diagnostic
    rather than emitting silent NaNs.
    """
    kind = spec.get("kind")
    cls = _DEVICE_REGISTRY.get(kind)
    if cls is None:
        raise DeviceError(f"device kind '{kind}' is not modeled")
    return cls(spec, models, params)


def registered_kinds() -> set[str]:
    return set(_DEVICE_REGISTRY)


class DeviceError(Exception):
    """Raised when a device cannot be built or modeled (clear, non-NaN failure)."""


# --------------------------------------------------------------------------- constants

_K = 1.380649e-23          # Boltzmann (J/K)
_Q = 1.602176634e-19       # electron charge (C)
_GMIN = 1e-12              # default conductance floor


def _vt(temp_c: float) -> float:
    """Thermal voltage kT/q at the given temperature in Celsius."""
    return _K * (temp_c + 273.15) / _Q


# Nominal temperature (deg C) at which device parameters are specified.
_TNOM_C = 27.0
# Silicon bandgap energy used for the diode IS temperature law (eV).
_EG_SI = 1.11


# --------------------------------------------------------------------------- base

class Device:
    """Base device. Subclasses override the stamp methods they support.

    ``spec`` is the Circuit-dict Device (see sim.md §1). ``branches`` holds any extra MNA branch
    rows allocated for this device (filled lazily on first stamp via ``mna.add_branch``).
    """

    kind: str = "?"
    # Number of extra MNA branch rows this device needs (V, L, E, H, OPAMP ...).
    n_branches: int = 0

    def __init__(self, spec: dict, models: dict, params: dict) -> None:
        self.spec = spec
        self.name: str = spec.get("name", self.kind)
        self.node_names: list[str] = list(spec.get("nodes", []))
        self.value = spec.get("value")
        self.model_name = spec.get("model")
        self.model: dict = dict(models.get(self.model_name, {})) if self.model_name else {}
        self.dparams: dict = dict(spec.get("params", {}))
        self.temp_c: float = float(params.get("TEMP", params.get("temp", 27.0)))
        self.gmin: float = _GMIN
        # branch-row indices (allocated once the MnaSystem dimensions are known)
        self.branch_rows: list[int] = []

    # -- helpers ----------------------------------------------------------------

    def _mp(self, key: str, default: float) -> float:
        """Read a model/device param (device params override .model card)."""
        if key in self.dparams:
            return float(self.dparams[key])
        for k in (key, key.upper(), key.lower()):
            if k in self.model:
                return float(self.model[k])
        return float(default)

    def alloc_branches(self, mna: "MnaLike") -> None:
        if self.n_branches and not self.branch_rows:
            self.branch_rows = [mna.add_branch(f"{self.name}#{i}")
                                for i in range(self.n_branches)]

    # -- stamps (default: linear no-op) ----------------------------------------

    def stamp_dc(self, mna: "MnaLike", x: np.ndarray) -> None:  # noqa: ARG002
        raise NotImplementedError(f"{self.kind} has no DC stamp")

    def stamp_tran(self, mna: "MnaLike", x: np.ndarray, dt: float, hist: dict) -> None:
        # Default: time-invariant device behaves like DC.
        self.stamp_dc(mna, x)

    def stamp_ac(self, mna: "MnaLike", x_op: np.ndarray, omega: float) -> None:
        raise NotImplementedError(f"{self.kind} has no AC stamp")

    def is_nonlinear(self) -> bool:
        return False

    def update_state(self, x: np.ndarray, dt: float, hist: dict) -> None:
        """After a transient step converges, store per-device history for the next step."""
        return None


# Typing shim — MnaSystem lives in circuit.py; we only need its interface here.
MnaLike = Any


# --------------------------------------------------------------------------- R

@device("R")
class Resistor(Device):
    def _r(self) -> float:
        """Resistance at the current temperature.

        Optional tempco: ``R(T) = R0 * (1 + tc1*(T-Tnom) + tc2*(T-Tnom)^2)`` where ``tc1`` (1/degC)
        and ``tc2`` (1/degC^2) come from the device/model params (default 0 -> temperature-independent).
        ``Tnom`` is the param-spec temperature (27 degC). This makes the resistor temp-aware for the
        ``temp`` sweep analysis without affecting any caller that omits a tempco.
        """
        r = float(self.value) if self.value is not None else 0.0
        if abs(r) < 1e-12:
            r = 1e-9  # 0-ohm guard -> tiny series R (sim.md §2)
        tc1 = self._mp("tc1", 0.0)
        tc2 = self._mp("tc2", 0.0)
        if tc1 != 0.0 or tc2 != 0.0:
            dt = self.temp_c - _TNOM_C
            r = r * (1.0 + tc1 * dt + tc2 * dt * dt)
            if abs(r) < 1e-12:
                r = 1e-9
        return r

    def _g(self) -> float:
        return 1.0 / self._r()

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        g = self._g()
        mna.stamp_G(a, a, g); mna.stamp_G(b, b, g)
        mna.stamp_G(a, b, -g); mna.stamp_G(b, a, -g)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.stamp_dc(mna, x_op)


# --------------------------------------------------------------------------- C

@device("C")
class Capacitor(Device):
    def _c(self) -> float:
        return float(self.value) if self.value is not None else 0.0

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        # Open circuit at DC — nothing to stamp.
        return None

    def stamp_tran(self, mna: MnaLike, x: np.ndarray, dt: float, hist: dict) -> None:
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        c = self._c()
        method = hist.get("_method", "trap")
        v_prev = hist.get(self.name, {}).get("v", 0.0)
        i_prev = hist.get(self.name, {}).get("i", 0.0)
        if method == "trap":
            geq = 2.0 * c / dt
            ieq = geq * v_prev + i_prev          # Norton equivalent current
        else:  # backward Euler
            geq = c / dt
            ieq = geq * v_prev
        mna.stamp_G(a, a, geq); mna.stamp_G(b, b, geq)
        mna.stamp_G(a, b, -geq); mna.stamp_G(b, a, -geq)
        # ieq flows from b to a inside the companion model (charges node a)
        mna.stamp_I(a, ieq); mna.stamp_I(b, -ieq)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        c = self._c()
        mna.stamp_C(a, a, c); mna.stamp_C(b, b, c)
        mna.stamp_C(a, b, -c); mna.stamp_C(b, a, -c)

    def update_state(self, x: np.ndarray, dt: float, hist: dict) -> None:
        a, b = self._a, self._b  # filled by record_indices
        va, vb = _xat(x, a), _xat(x, b)
        v = va - vb
        c = self._c()
        st = hist.setdefault(self.name, {})
        v_prev = st.get("v", 0.0)
        i_prev = st.get("i", 0.0)
        method = hist.get("_method", "trap")
        if method == "trap":
            i_new = 2.0 * c / dt * (v - v_prev) - i_prev
        else:
            i_new = c / dt * (v - v_prev)
        st["v"] = v
        st["i"] = i_new

    def record_indices(self, mna: MnaLike) -> None:
        self._a = mna.n(self.node_names[0])
        self._b = mna.n(self.node_names[1])


# --------------------------------------------------------------------------- L

@device("L")
class Inductor(Device):
    n_branches = 1  # inductor current is an MNA unknown

    def _l(self) -> float:
        return float(self.value) if self.value is not None else 0.0

    def _br(self) -> int:
        return self.branch_rows[0]

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        # DC: ideal short — V(a)-V(b) = 0, branch current is the unknown.
        self.alloc_branches(mna)
        a, b, br = mna.n(self.node_names[0]), mna.n(self.node_names[1]), self._br()
        mna.stamp_G(a, br, 1.0); mna.stamp_G(b, br, -1.0)
        mna.stamp_G(br, a, 1.0); mna.stamp_G(br, b, -1.0)

    def stamp_tran(self, mna: MnaLike, x: np.ndarray, dt: float, hist: dict) -> None:
        self.alloc_branches(mna)
        a, b, br = mna.n(self.node_names[0]), mna.n(self.node_names[1]), self._br()
        L = self._l()
        method = hist.get("_method", "trap")
        st = hist.get(self.name, {})
        i_prev = st.get("i", 0.0)
        v_prev = st.get("v", 0.0)
        # KCL: branch current enters node a, leaves node b
        mna.stamp_G(a, br, 1.0); mna.stamp_G(b, br, -1.0)
        # Branch equation: V(a)-V(b) - Req*I = Veq
        if method == "trap":
            req = 2.0 * L / dt
            veq = -(req * i_prev + v_prev)
        else:
            req = L / dt
            veq = -req * i_prev
        mna.stamp_G(br, a, 1.0); mna.stamp_G(br, b, -1.0)
        mna.stamp_G(br, br, -req)
        mna.stamp_I(br, veq)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.alloc_branches(mna)
        a, b, br = mna.n(self.node_names[0]), mna.n(self.node_names[1]), self._br()
        L = self._l()
        mna.stamp_G(a, br, 1.0); mna.stamp_G(b, br, -1.0)
        mna.stamp_G(br, a, 1.0); mna.stamp_G(br, b, -1.0)
        # V = jωL I  ->  branch row: V(a)-V(b) - jωL I = 0
        mna.stamp_C(br, br, -L)  # contributes -jωL on the branch diagonal

    def update_state(self, x: np.ndarray, dt: float, hist: dict) -> None:
        a, b, br = self._a, self._b, self._br()
        v = _xat(x, a) - _xat(x, b)
        i = _xat(x, br)
        st = hist.setdefault(self.name, {})
        st["i"] = i
        st["v"] = v

    def record_indices(self, mna: MnaLike) -> None:
        self.alloc_branches(mna)
        self._a = mna.n(self.node_names[0])
        self._b = mna.n(self.node_names[1])


# --------------------------------------------------------------------------- waveforms

def source_value(spec_params: dict, value: float | None, t: float) -> float:
    """Evaluate an independent-source waveform at time ``t``.

    Supports DC (constant ``value``), PULSE(v1 v2 td tr tf pw per), SIN(vo va f td theta),
    and PWL(t0 v0 t1 v1 ...). AC magnitude is ignored in transient (used only by AC analysis).
    """
    wf = spec_params.get("waveform") if spec_params else None
    base = float(value) if value is not None else float(spec_params.get("dc", 0.0)) if spec_params else 0.0
    if not wf:
        # Possibly a bare DC with no waveform; "dc" key wins if present.
        if spec_params and "dc" in spec_params:
            return float(spec_params["dc"])
        return base

    kind = wf.get("type", "DC").upper()
    if kind == "DC":
        return float(wf.get("v", base))

    if kind == "PULSE":
        v1 = float(wf.get("v1", 0.0)); v2 = float(wf.get("v2", 0.0))
        td = float(wf.get("td", 0.0)); tr = float(wf.get("tr", 0.0))
        tf = float(wf.get("tf", 0.0)); pw = float(wf.get("pw", 0.0))
        per = float(wf.get("per", 0.0))
        if t < td:
            return v1
        tt = t - td
        if per > 0.0:
            tt = math.fmod(tt, per)
        if tr > 0.0 and tt < tr:
            return v1 + (v2 - v1) * (tt / tr)
        tt -= tr
        if tt < pw:
            return v2
        tt -= pw
        if tf > 0.0 and tt < tf:
            return v2 + (v1 - v2) * (tt / tf)
        return v1

    if kind == "SIN":
        vo = float(wf.get("vo", 0.0)); va = float(wf.get("va", 0.0))
        freq = float(wf.get("f", wf.get("freq", 0.0)))
        td = float(wf.get("td", 0.0)); theta = float(wf.get("theta", 0.0))
        if t < td:
            return vo
        tt = t - td
        return vo + va * math.exp(-tt * theta) * math.sin(2.0 * math.pi * freq * tt)

    if kind == "PWL":
        pts = wf.get("points") or wf.get("pwl") or []
        if not pts:
            return base
        # pts is a flat [t0,v0,t1,v1,...] or list of (t,v)
        if isinstance(pts[0], (list, tuple)):
            ts = [float(p[0]) for p in pts]; vs = [float(p[1]) for p in pts]
        else:
            ts = [float(pts[i]) for i in range(0, len(pts), 2)]
            vs = [float(pts[i]) for i in range(1, len(pts), 2)]
        if t <= ts[0]:
            return vs[0]
        if t >= ts[-1]:
            return vs[-1]
        for i in range(len(ts) - 1):
            if ts[i] <= t <= ts[i + 1]:
                f = (t - ts[i]) / (ts[i + 1] - ts[i]) if ts[i + 1] > ts[i] else 0.0
                return vs[i] + (vs[i + 1] - vs[i]) * f
        return vs[-1]

    return base


def source_breakpoints(spec_params: dict, tstop: float) -> list[float]:
    """Return waveform edge times to add as transient breakpoints (BE bootstrap there)."""
    wf = spec_params.get("waveform") if spec_params else None
    if not wf:
        return []
    kind = wf.get("type", "DC").upper()
    bps: list[float] = []
    if kind == "PULSE":
        td = float(wf.get("td", 0.0)); tr = float(wf.get("tr", 0.0))
        tf = float(wf.get("tf", 0.0)); pw = float(wf.get("pw", 0.0))
        per = float(wf.get("per", 0.0))
        edges = [td, td + tr, td + tr + pw, td + tr + pw + tf]
        if per > 0.0:
            t0 = td
            n = 0
            while t0 + n * per <= tstop and n < 100000:
                for e in (0.0, tr, tr + pw, tr + pw + tf):
                    bp = td + n * per + e
                    if 0.0 <= bp <= tstop:
                        bps.append(bp)
                n += 1
        else:
            bps.extend([e for e in edges if 0.0 <= e <= tstop])
    elif kind == "PWL":
        pts = wf.get("points") or wf.get("pwl") or []
        if pts and isinstance(pts[0], (list, tuple)):
            bps.extend(float(p[0]) for p in pts)
        elif pts:
            bps.extend(float(pts[i]) for i in range(0, len(pts), 2))
        bps = [b for b in bps if 0.0 <= b <= tstop]
    return sorted(set(bps))


def _ac_phasor(spec_params: dict, value: float | None) -> complex:
    """Complex AC excitation (magnitude/phase) for an independent source."""
    wf = spec_params.get("waveform") if spec_params else None
    ac = spec_params.get("ac") if spec_params else None
    if ac is not None:
        mag = float(ac.get("mag", 1.0)); ph = float(ac.get("phase", 0.0))
        return mag * np.exp(1j * math.radians(ph))
    if wf and wf.get("ac"):
        a = wf["ac"]
        return float(a.get("mag", 1.0)) * np.exp(1j * math.radians(float(a.get("phase", 0.0))))
    # No AC spec: source is a short (V) / open (I) at AC unless mag given on value.
    return 0.0 + 0.0j


# --------------------------------------------------------------------------- V

@device("V")
class VSource(Device):
    n_branches = 1

    def _br(self) -> int:
        return self.branch_rows[0]

    def stamp_dc(self, mna: MnaLike, x: np.ndarray, t: float = 0.0) -> None:
        self.alloc_branches(mna)
        a, b, br = mna.n(self.node_names[0]), mna.n(self.node_names[1]), self._br()
        mna.stamp_G(a, br, 1.0); mna.stamp_G(b, br, -1.0)
        mna.stamp_G(br, a, 1.0); mna.stamp_G(br, b, -1.0)
        v = source_value(self.dparams, self.value, t) * mna.src_scale(self.name)
        mna.stamp_I(br, v)

    def stamp_tran(self, mna: MnaLike, x: np.ndarray, dt: float, hist: dict) -> None:
        t = hist.get("_t", 0.0)
        self.stamp_dc(mna, x, t=t)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.alloc_branches(mna)
        a, b, br = mna.n(self.node_names[0]), mna.n(self.node_names[1]), self._br()
        mna.stamp_G(a, br, 1.0); mna.stamp_G(b, br, -1.0)
        mna.stamp_G(br, a, 1.0); mna.stamp_G(br, b, -1.0)
        mna.stamp_I(br, _ac_phasor(self.dparams, self.value))

    def record_indices(self, mna: MnaLike) -> None:
        self.alloc_branches(mna)


# --------------------------------------------------------------------------- I

@device("I")
class ISource(Device):
    def stamp_dc(self, mna: MnaLike, x: np.ndarray, t: float = 0.0) -> None:
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        i = source_value(self.dparams, self.value, t) * mna.src_scale(self.name)
        # Current source pushes current from node a to node b (a -> b inside source).
        mna.stamp_I(a, -i); mna.stamp_I(b, i)

    def stamp_tran(self, mna: MnaLike, x: np.ndarray, dt: float, hist: dict) -> None:
        self.stamp_dc(mna, x, t=hist.get("_t", 0.0))

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        ph = _ac_phasor(self.dparams, self.value)
        mna.stamp_I(a, -ph); mna.stamp_I(b, ph)


# --------------------------------------------------------------------------- D

@device("D")
class Diode(Device):
    """Shockley diode with series resistance RS and (transient) junction cap CJO."""

    def is_nonlinear(self) -> bool:
        return True

    def _params(self) -> tuple[float, float, float, float, float]:
        IS = self._is_temp()
        N = self._mp("N", 1.0)
        RS = self._mp("RS", 0.0)
        BV = self._mp("BV", 1e30)
        CJO = self._mp("CJO", 0.0)
        return IS, N, RS, BV, CJO

    def _is_temp(self) -> float:
        """Saturation current at the current temperature (SPICE IS temperature law).

        ``IS(T) = IS0 * (T/Tnom)^(XTI/N) * exp( -Eg*q/(N*k) * (1/T - 1/Tnom) )`` with the silicon
        bandgap ``Eg`` (1.11 eV), emission coefficient ``N``, and saturation-current temperature
        exponent ``XTI`` (default 3). Combined with the temperature rise in the thermal voltage
        ``Vt = kT/q``, this gives the textbook forward-drop tempco of about -2 mV/degC at fixed
        current. At T == Tnom the scaling is exactly 1, so 27 degC behaviour is unchanged.
        """
        IS0 = self._mp("IS", 1e-14)
        N = self._mp("N", 1.0)
        XTI = self._mp("XTI", 3.0)
        tk = self.temp_c + 273.15
        tnomk = _TNOM_C + 273.15
        if abs(tk - tnomk) < 1e-9 or IS0 <= 0.0:
            return IS0
        ratio = tk / tnomk
        # Eg*q/k has units of Kelvin; Eg in eV -> multiply by q to get Joules, divide by k.
        eg_term = _EG_SI * _Q / _K
        try:
            scale = (ratio ** (XTI / N)) * math.exp(-(eg_term / N) * (1.0 / tk - 1.0 / tnomk))
        except (OverflowError, ValueError):
            return IS0
        return IS0 * scale

    def _linearize(self, vd: float) -> tuple[float, float]:
        """Return (Id, gd) for the junction (excluding RS) at diode voltage vd."""
        IS, N, RS, BV, CJO = self._params()
        vt = N * _vt(self.temp_c)
        # Limit the exponential argument to avoid overflow (junction limiting).
        vcrit = vt * math.log(vt / (math.sqrt(2.0) * IS)) if IS > 0 else 0.6
        v = vd
        if v > vcrit:
            v = vcrit + vt * math.log1p((vd - vcrit) / vt) if (vd - vcrit) / vt > -1 else vcrit
        if v >= 0 or v > -5 * vt:
            ev = math.exp(min(v / vt, 80.0))
            Id = IS * (ev - 1.0)
            gd = IS / vt * ev
        else:
            # Reverse / breakdown region
            Id = -IS
            gd = self.gmin
            if -vd > BV:  # simple breakdown model
                ibrk = IS * (math.exp(min((-(vd) - BV) / vt, 80.0)) - 1.0)
                Id = -IS - ibrk
                gd = IS / vt * math.exp(min((-(vd) - BV) / vt, 80.0))
        gd = max(gd, self.gmin)
        return Id, gd

    def _vd(self, mna: MnaLike, x: np.ndarray) -> float:
        IS, N, RS, BV, CJO = self._params()
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        # With RS we use an internal node; approximate vd from terminal voltage for the
        # companion (RS folded as series). For RS=0 this is exact.
        vt = N * _vt(self.temp_c)
        vterm = _xat(x, a) - _xat(x, b)
        if RS <= 0.0:
            return vterm
        # Solve series: vterm = vd + RS*Id(vd) by a few fixed-point/Newton steps.
        vd = getattr(self, "_last_vd", min(vterm, 0.7))
        for _ in range(20):
            ev = math.exp(min(vd / vt, 80.0))
            Id = IS * (ev - 1.0)
            gd = IS / vt * ev + self.gmin
            f = vd + RS * Id - vterm
            dfdv = 1.0 + RS * gd
            step = f / dfdv
            vd -= step
            if abs(step) < 1e-9:
                break
        return vd

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        IS, N, RS, BV, CJO = self._params()
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        vd = self._vd(mna, x)
        self._last_vd = vd
        Id, gd = self._linearize(vd)
        # Effective conductance through RS in series.
        geff = gd / (1.0 + RS * gd) if RS > 0 else gd
        # Norton companion: I_eq = Id - gd*vd, then re-referenced through RS.
        ieq = (Id - gd * vd)
        ieq_eff = ieq / (1.0 + RS * gd) if RS > 0 else ieq
        mna.stamp_G(a, a, geff); mna.stamp_G(b, b, geff)
        mna.stamp_G(a, b, -geff); mna.stamp_G(b, a, -geff)
        # Companion current source pushes ieq from a into the device (out of node a).
        mna.stamp_I(a, -ieq_eff); mna.stamp_I(b, ieq_eff)

    def stamp_tran(self, mna: MnaLike, x: np.ndarray, dt: float, hist: dict) -> None:
        # DC stamp + linear junction capacitance companion (treated like a small C).
        self.stamp_dc(mna, x)
        IS, N, RS, BV, CJO = self._params()
        if CJO <= 0.0:
            return
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        st = hist.get(self.name, {})
        v_prev = st.get("vcap", 0.0)
        i_prev = st.get("icap", 0.0)
        method = hist.get("_method", "trap")
        if method == "trap":
            geq = 2.0 * CJO / dt; ieq = geq * v_prev + i_prev
        else:
            geq = CJO / dt; ieq = geq * v_prev
        mna.stamp_G(a, a, geq); mna.stamp_G(b, b, geq)
        mna.stamp_G(a, b, -geq); mna.stamp_G(b, a, -geq)
        mna.stamp_I(a, ieq); mna.stamp_I(b, -ieq)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        IS, N, RS, BV, CJO = self._params()
        a, b = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        vd = self._vd(mna, x_op)
        Id, gd = self._linearize(vd)
        geff = gd / (1.0 + RS * gd) if RS > 0 else gd
        mna.stamp_G(a, a, geff); mna.stamp_G(b, b, geff)
        mna.stamp_G(a, b, -geff); mna.stamp_G(b, a, -geff)
        if CJO > 0.0:
            mna.stamp_C(a, a, CJO); mna.stamp_C(b, b, CJO)
            mna.stamp_C(a, b, -CJO); mna.stamp_C(b, a, -CJO)

    def update_state(self, x: np.ndarray, dt: float, hist: dict) -> None:
        IS, N, RS, BV, CJO = self._params()
        if CJO <= 0.0:
            return
        a, b = self._a, self._b
        v = _xat(x, a) - _xat(x, b)
        st = hist.setdefault(self.name, {})
        v_prev = st.get("vcap", 0.0)
        i_prev = st.get("icap", 0.0)
        method = hist.get("_method", "trap")
        if method == "trap":
            i_new = 2.0 * CJO / dt * (v - v_prev) - i_prev
        else:
            i_new = CJO / dt * (v - v_prev)
        st["vcap"] = v; st["icap"] = i_new

    def record_indices(self, mna: MnaLike) -> None:
        self._a = mna.n(self.node_names[0]); self._b = mna.n(self.node_names[1])


# --------------------------------------------------------------------------- Q (BJT)

@device("Q")
class BJT(Device):
    """Ebers-Moll BJT with forward Early effect. nodes = [C, B, E]. type npn|pnp."""

    def is_nonlinear(self) -> bool:
        return True

    def _params(self):
        IS = self._mp("IS", 1e-16)
        BF = self._mp("BF", 100.0)
        BR = self._mp("BR", 1.0)
        VAF = self._mp("VAF", 1e30)
        NF = self._mp("NF", 1.0)
        NR = self._mp("NR", 1.0)
        typ = str(self.model.get("type", self.dparams.get("type", "npn"))).lower()
        return IS, BF, BR, VAF, NF, NR, typ

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        IS, BF, BR, VAF, NF, NR, typ = self._params()
        c = mna.n(self.node_names[0]); b = mna.n(self.node_names[1]); e = mna.n(self.node_names[2])
        vt = _vt(self.temp_c)
        sign = 1.0 if typ == "npn" else -1.0
        vbe = sign * (_xat(x, b) - _xat(x, e))
        vbc = sign * (_xat(x, b) - _xat(x, c))

        def diode(v: float, n: float):
            v = min(v, 0.95)  # clamp to keep exp bounded near the op point
            ev = math.exp(min(v / (n * vt), 80.0))
            i = IS * (ev - 1.0)
            g = IS / (n * vt) * ev + self.gmin
            return i, g

        ibe, gbe = diode(vbe, NF)   # forward junction current (collector-ref)
        ibc, gbc = diode(vbc, NR)
        # Early effect: base-width modulation on forward transport current.
        qb = 1.0
        if VAF < 1e29:
            qb = 1.0 / max(1.0 - vbc / VAF, 0.1)

        af = BF / (1.0 + BF)
        ar = BR / (1.0 + BR)
        # Transport currents
        ic = (ibe * qb - ibc) - ibc / BR
        ib = ibe / BF + ibc / BR
        ie = -(ic + ib)

        # Conductances of the linearized 2-port (simplified Gummel-Poon-ish).
        gpi = gbe / BF
        gmu = gbc / BR
        gm = gbe * qb - gbc  # transconductance approx
        go = gm * 0.0  # output conductance folded via qb already (kept simple)

        # Stamp the small-signal Jacobian about the operating point, in device polarity.
        # Currents into collector, base, emitter (KCL). Build companion RHS too.
        # Using node-referenced conductances:
        gcc = gbc + go
        # Base node
        mna.stamp_G(b, b, gpi + gmu)
        mna.stamp_G(b, e, -gpi)
        mna.stamp_G(b, c, -gmu)
        # Collector node
        mna.stamp_G(c, b, gm - gmu)
        mna.stamp_G(c, e, -gm)
        mna.stamp_G(c, c, gmu + go)
        # Emitter node
        mna.stamp_G(e, b, -gpi - gm)
        mna.stamp_G(e, e, gpi + gm)
        mna.stamp_G(e, c, go)

        # RHS companion: I_eq = I(op) - J @ v(op)
        # Base current eq
        ieq_b = ib - (gpi + gmu) * (sign * (_xat(x, b))) \
            + gpi * (sign * _xat(x, e)) + gmu * (sign * _xat(x, c))
        # Build from incremental form: easier to compute from vbe/vbc directly.
        ieq_b = ib - gpi * vbe - gmu * vbc
        ieq_c = ic - gm * vbe + gmu * vbc - go * (sign * (_xat(x, c) - _xat(x, e)))
        ieq_e = -(ieq_b + ieq_c)

        mna.stamp_I(b, -sign * ieq_b)
        mna.stamp_I(c, -sign * ieq_c)
        mna.stamp_I(e, -sign * ieq_e)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        # Reuse the DC Jacobian (conductances) for small-signal; cap terms omitted (CJE/CJC v2).
        IS, BF, BR, VAF, NF, NR, typ = self._params()
        c = mna.n(self.node_names[0]); b = mna.n(self.node_names[1]); e = mna.n(self.node_names[2])
        vt = _vt(self.temp_c)
        sign = 1.0 if typ == "npn" else -1.0
        vbe = sign * (_xat(x_op, b) - _xat(x_op, e))
        vbc = sign * (_xat(x_op, b) - _xat(x_op, c))
        gbe = IS / (NF * vt) * math.exp(min(min(vbe, 0.95) / (NF * vt), 80.0)) + self.gmin
        gbc = IS / (NR * vt) * math.exp(min(min(vbc, 0.95) / (NR * vt), 80.0)) + self.gmin
        gpi = gbe / BF; gmu = gbc / BR; gm = gbe
        mna.stamp_G(b, b, gpi + gmu); mna.stamp_G(b, e, -gpi); mna.stamp_G(b, c, -gmu)
        mna.stamp_G(c, b, gm - gmu); mna.stamp_G(c, e, -gm); mna.stamp_G(c, c, gmu)
        mna.stamp_G(e, b, -gpi - gm); mna.stamp_G(e, e, gpi + gm); mna.stamp_G(e, c, 0.0)


# --------------------------------------------------------------------------- E (VCVS)

@device("E")
class VCVS(Device):
    """Voltage-controlled voltage source. nodes=[out+, out-, ctrl+, ctrl-]; value=gain."""

    n_branches = 1

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        self.alloc_branches(mna)
        op, on = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        cp, cn = mna.n(self.node_names[2]), mna.n(self.node_names[3])
        br = self.branch_rows[0]
        gain = float(self.value if self.value is not None else self._mp("gain", 1.0))
        mna.stamp_G(op, br, 1.0); mna.stamp_G(on, br, -1.0)
        mna.stamp_G(br, op, 1.0); mna.stamp_G(br, on, -1.0)
        mna.stamp_G(br, cp, -gain); mna.stamp_G(br, cn, gain)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.stamp_dc(mna, x_op)


# --------------------------------------------------------------------------- G (VCCS)

@device("G")
class VCCS(Device):
    """Voltage-controlled current source. nodes=[out+, out-, ctrl+, ctrl-]; value=gm."""

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        op, on = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        cp, cn = mna.n(self.node_names[2]), mna.n(self.node_names[3])
        gm = float(self.value if self.value is not None else self._mp("gain", 1.0))
        mna.stamp_G(op, cp, gm); mna.stamp_G(op, cn, -gm)
        mna.stamp_G(on, cp, -gm); mna.stamp_G(on, cn, gm)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.stamp_dc(mna, x_op)


# --------------------------------------------------------------------------- F (CCCS)

@device("F")
class CCCS(Device):
    """Current-controlled current source. nodes=[out+,out-]; control branch = V source name.

    F1 out+ out- Vcontrol gain  (mirrors the current through Vcontrol scaled by gain).
    """

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        op, on = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        ctrl = self.dparams.get("control") or self.model_name
        gain = float(self.value if self.value is not None else self._mp("gain", 1.0))
        br = mna.branch_index(ctrl)
        if br is None:
            raise DeviceError(f"{self.name}: control branch '{ctrl}' not found")
        mna.stamp_G(op, br, gain); mna.stamp_G(on, br, -gain)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.stamp_dc(mna, x_op)


# --------------------------------------------------------------------------- H (CCVS)

@device("H")
class CCVS(Device):
    """Current-controlled voltage source. nodes=[out+,out-]; control = V source name."""

    n_branches = 1

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        self.alloc_branches(mna)
        op, on = mna.n(self.node_names[0]), mna.n(self.node_names[1])
        br = self.branch_rows[0]
        ctrl = self.dparams.get("control") or self.model_name
        cbr = mna.branch_index(ctrl)
        if cbr is None:
            raise DeviceError(f"{self.name}: control branch '{ctrl}' not found")
        r = float(self.value if self.value is not None else self._mp("gain", 1.0))
        mna.stamp_G(op, br, 1.0); mna.stamp_G(on, br, -1.0)
        mna.stamp_G(br, op, 1.0); mna.stamp_G(br, on, -1.0)
        mna.stamp_G(br, cbr, -r)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.stamp_dc(mna, x_op)


# --------------------------------------------------------------------------- OPAMP

@device("OPAMP")
class OpAmp(Device):
    """Operational amplifier. nodes=[in+, in-, out, (ref?)].

    Two flavours selected by params/model:
      * ideal (default): infinite gain, virtual short via an MNA branch enforcing
        V(in+) - V(in-) = 0, with the op-amp output current as the branch unknown.
      * 1-pole macromodel: when ``gbw`` is given — finite DC gain ``gain`` rolling off at
        the pole f_p = gbw / gain (modeled in AC; DC uses the finite-gain VCVS form).
    """

    n_branches = 1

    def _gain(self) -> float:
        return float(self._mp("gain", 1e6))

    def _is_ideal(self) -> bool:
        return self._mp("gbw", 0.0) <= 0.0 and self._mp("gain", 0.0) <= 0.0

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        self.alloc_branches(mna)
        ip = mna.n(self.node_names[0]); inn = mna.n(self.node_names[1])
        out = mna.n(self.node_names[2])
        ref = mna.n(self.node_names[3]) if len(self.node_names) > 3 else -1
        br = self.branch_rows[0]
        # Output branch: current flows out of 'out' (and into 'ref').
        mna.stamp_G(out, br, 1.0)
        if ref >= 0:
            mna.stamp_G(ref, br, -1.0)
        if self._is_ideal():
            # Constraint row: V(in+) - V(in-) = 0  (ideal nulling).
            mna.stamp_G(br, ip, 1.0); mna.stamp_G(br, inn, -1.0)
        else:
            # Finite gain: V(out)-V(ref) = A * (V(in+)-V(in-))
            A = self._gain()
            mna.stamp_G(br, out, 1.0)
            if ref >= 0:
                mna.stamp_G(br, ref, -1.0)
            mna.stamp_G(br, ip, -A); mna.stamp_G(br, inn, A)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.alloc_branches(mna)
        ip = mna.n(self.node_names[0]); inn = mna.n(self.node_names[1])
        out = mna.n(self.node_names[2])
        ref = mna.n(self.node_names[3]) if len(self.node_names) > 3 else -1
        br = self.branch_rows[0]
        mna.stamp_G(out, br, 1.0)
        if ref >= 0:
            mna.stamp_G(ref, br, -1.0)
        gbw = self._mp("gbw", 0.0)
        gain = self._mp("gain", 0.0)
        if self._is_ideal():
            mna.stamp_G(br, ip, 1.0); mna.stamp_G(br, inn, -1.0)
            return
        # 1-pole: A(jw) = A0 / (1 + jw/wp), wp = 2*pi*gbw/A0
        A0 = gain if gain > 0 else (gbw if gbw > 0 else 1e6)
        if gbw > 0 and A0 > 0:
            wp = 2.0 * math.pi * gbw / A0
            Aw = A0 / (1.0 + 1j * (omega / wp))
        else:
            Aw = A0
        mna.stamp_G(br, out, 1.0)
        if ref >= 0:
            mna.stamp_G(br, ref, -1.0)
        # V(out) - Aw*(Vin+ - Vin-) = 0  -> stamp complex Aw into the G (real+imag) matrix.
        mna.stamp_Gc(br, ip, -Aw); mna.stamp_Gc(br, inn, Aw)


# --------------------------------------------------------------------------- M (stub)

@device("M")
class Mosfet(Device):
    """MOSFET level-1 (square-law). nodes=[D, G, S, (B)]. DC + small-signal AC (no caps in v1)."""

    def is_nonlinear(self) -> bool:
        return True

    def _params(self):
        VTO = self._mp("VTO", 1.0)
        KP = self._mp("KP", 2e-5)
        LAMBDA = self._mp("LAMBDA", 0.0)
        W = self._mp("W", 1e-4)
        L = self._mp("L", 1e-4)
        typ = str(self.model.get("type", self.dparams.get("type", "nmos"))).lower()
        beta = KP * W / L
        return VTO, beta, LAMBDA, typ

    def _bias(self, x: np.ndarray, mna: MnaLike) -> tuple[float, float, float, float, float]:
        """Recompute the level-1 operating point from the (op-point) node voltages.

        Returns ``(sign, Id, gm, gds, vgs)`` in device polarity (the controlled drain current is
        ``gm*vgs`` referred drain->source). Both ``stamp_dc`` and ``stamp_ac`` linearize the
        square-law about this bias -- the AC path passes the converged DC solution ``x_op`` (same
        approach as the BJT / diode), so the small-signal parameters are evaluated exactly at the
        operating point with no stored state needed.
        """
        VTO, beta, lam, typ = self._params()
        d = mna.n(self.node_names[0]); g = mna.n(self.node_names[1]); s = mna.n(self.node_names[2])
        sign = 1.0 if typ in ("nmos", "n") else -1.0
        vgs = sign * (_xat(x, g) - _xat(x, s))
        vds = sign * (_xat(x, d) - _xat(x, s))
        vth = VTO
        if vgs <= vth:
            id_ = 0.0; gm = 0.0; gds = self.gmin
        elif vds < (vgs - vth):  # triode
            id_ = beta * ((vgs - vth) * vds - 0.5 * vds * vds) * (1 + lam * vds)
            gm = beta * vds * (1 + lam * vds)
            gds = beta * ((vgs - vth) - vds) * (1 + lam * vds) + self.gmin
        else:  # saturation
            id_ = 0.5 * beta * (vgs - vth) ** 2 * (1 + lam * vds)
            gm = beta * (vgs - vth) * (1 + lam * vds)
            gds = 0.5 * beta * (vgs - vth) ** 2 * lam + self.gmin
        return sign, id_, gm, gds, vgs

    def _stamp_conductances(self, mna: MnaLike, gm: float, gds: float) -> None:
        """Stamp the linear small-signal conductances: gds across D-S plus the controlled drain
        current source gm*Vgs (referred drain->source). Polarity-symmetric, so it is identical for
        n- and p-channel (the channel-type sign only enters the bias computation)."""
        d = mna.n(self.node_names[0]); g = mna.n(self.node_names[1]); s = mna.n(self.node_names[2])
        # gds : drain-source output conductance.
        mna.stamp_G(d, d, gds); mna.stamp_G(d, s, -gds)
        mna.stamp_G(s, d, -gds); mna.stamp_G(s, s, gds)
        # gm*Vgs : VCCS from drain to source controlled by V(g)-V(s).
        mna.stamp_G(d, g, gm); mna.stamp_G(d, s, -gm)
        mna.stamp_G(s, g, -gm); mna.stamp_G(s, s, gm)

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        d = mna.n(self.node_names[0]); g = mna.n(self.node_names[1]); s = mna.n(self.node_names[2])
        sign, id_, gm, gds, vgs = self._bias(x, mna)
        vds = sign * (_xat(x, d) - _xat(x, s))
        self._stamp_conductances(mna, gm, gds)
        # Newton companion current source: I_eq = Id - gm*Vgs - gds*Vds (in device polarity),
        # injected drain->source. Multiply by the channel sign to map back to terminal polarity.
        ieq = id_ - gm * vgs - gds * vds
        mna.stamp_I(d, -sign * ieq); mna.stamp_I(s, sign * ieq)

    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        """Level-1 small-signal AC stamp linearized at the DC operating point.

        Transconductance ``gm`` and output conductance ``gds`` are recomputed from the op-point
        node voltages (the AC driver passes the converged DC solution as ``x_op``). Only the linear
        conductances are stamped: the controlled source ``gm*Vgs`` (drain->source) plus ``gds``
        across drain-source. Junction/overlap capacitances (Cgs, Cgd, Cds) are intentionally OMITTED
        in v1 -- the AC response is frequency-flat, exact for the resistive small-signal model and a
        valid low-frequency approximation. Works for both n- and p-channel."""
        _sign, _id, gm, gds, _vgs = self._bias(x_op, mna)
        self._stamp_conductances(mna, gm, gds)


# --------------------------------------------------------------------------- S (stub)

@device("S")
class Switch(Device):
    """Voltage-controlled switch — NICE-TO-HAVE; registered stub."""

    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        raise DeviceError("voltage-controlled switch (S) not yet modeled")


# --------------------------------------------------------------------------- T (lossless line)

@device("T")
class TLine(Device):
    """Ideal lossless transmission line (controlled-impedance interconnect).

    SPICE ``T`` element: a two-port lossless line of characteristic impedance ``Z0`` and one-way
    propagation delay ``TD``. ``nodes = [a+, a-, b+, b-]`` (port-A pair, port-B pair); the two
    return nodes are usually ground. Params: ``{z0, td}`` (seconds) or ``{z0, freq, nl}`` where the
    delay is derived as ``td = nl / freq`` (electrical length ``nl`` wavelengths at ``freq``).

    **Transient model -- Branin method of characteristics (the classic lossless-line companion).**
    Each port is, instantaneously, a resistor ``Z0`` to its own reference in series with a history
    voltage source carrying the wave that left the *other* port one delay ``TD`` ago::

        v_a(t) - Z0*i_a(t) = E_a(t) = 2*v_b(t-TD) - [ v_b(t-TD) - Z0*i_b(t-TD) ]
                                    = v_b(t-TD) + Z0*i_b(t-TD)         (incident from B)
        v_b(t) - Z0*i_b(t) = E_b(t) = v_a(t-TD) + Z0*i_a(t-TD)         (incident from A)

    where ``i`` flows *into* each port. The delayed history is interpolated linearly between stored
    samples (we keep the full ``(t, v_port, i_port)`` history per port), so an arbitrary timestep
    works -- this is the honest model used: ideal lossless Branin with linear history interpolation
    (no loss, no dispersion). Implemented with one MNA branch current per port (like the inductor),
    so the port currents are exact unknowns rather than finite-difference estimates.

    **DC.** A lossless line at DC is a pure ``Z0``-to-Z0 wire: ``E = v(t-TD) + Z0*i(t-TD)`` becomes
    the static condition, which collapses to ``V(a)=V(b)``, ``i_a = -i_b`` -- i.e. an ideal pass
    through (the line is a short for the differential signal, both ends at the same potential). We
    stamp it as the static fixed point of the Branin equations: a ``Z0`` series resistor at each
    port driven by the steady incident wave, which yields a DC wire. (SPICE convention: the line
    passes DC.)

    **AC.** Exact lossless two-port. With ``gamma*l = j*omega*TD`` (lossless -> purely imaginary),
    the ABCD/admittance two-port of a line of impedance ``Z0`` is stamped exactly using
    ``Y11=Y22 = 1/(Z0*tanh(j*beta*l))`` and ``Y12=Y21 = -1/(Z0*sinh(j*beta*l))``. This reproduces
    the quarter-wave impedance inverter, flat input impedance for a matched termination, etc.
    """

    n_branches = 2  # one branch current per port (i into a+, i into b+)

    # ---- parameters ----
    def _z0_td(self) -> tuple[float, float]:
        z0 = self._mp("z0", self._mp("Z0", 50.0))
        if z0 <= 0.0:
            z0 = 50.0
        td = self.dparams.get("td", self.dparams.get("TD"))
        if td is not None:
            return float(z0), max(float(td), 0.0)
        # {z0, freq, nl}: electrical length nl (in wavelengths) at freq -> td = nl / freq.
        freq = self._mp("freq", self._mp("f", 0.0))
        nl = self._mp("nl", self._mp("NL", 0.25))
        if freq > 0.0:
            return float(z0), nl / freq
        return float(z0), 0.0

    def _ports(self, mna: MnaLike):
        ap = mna.n(self.node_names[0]); an = mna.n(self.node_names[1])
        bp = mna.n(self.node_names[2]); bn = mna.n(self.node_names[3])
        return ap, an, bp, bn

    # ---- DC: lossless line passes DC (ideal Z0 wire, both ends equal) ----
    def stamp_dc(self, mna: MnaLike, x: np.ndarray) -> None:
        self.alloc_branches(mna)
        ap, an, bp, bn = self._ports(mna)
        bra, brb = self.branch_rows[0], self.branch_rows[1]
        # Branch currents enter each port.
        mna.stamp_G(ap, bra, 1.0); mna.stamp_G(an, bra, -1.0)
        mna.stamp_G(bp, brb, 1.0); mna.stamp_G(bn, brb, -1.0)
        # At DC the Branin incident waves settle to the steady values, giving V(a)=V(b) and
        # i_a = -i_b: stamp the two constraint rows (ideal pass-through / Z0->wire limit).
        # Row a: V(a) - V(b) = 0
        mna.stamp_G(bra, ap, 1.0); mna.stamp_G(bra, an, -1.0)
        mna.stamp_G(bra, bp, -1.0); mna.stamp_G(bra, bn, 1.0)
        # Row b: i_a + i_b = 0  (KCL of an ideal lossless wire at DC)
        mna.stamp_G(brb, bra, 1.0); mna.stamp_G(brb, brb, 1.0)

    # ---- transient: Branin method of characteristics ----
    def stamp_tran(self, mna: MnaLike, x: np.ndarray, dt: float, hist: dict) -> None:
        self.alloc_branches(mna)
        ap, an, bp, bn = self._ports(mna)
        bra, brb = self.branch_rows[0], self.branch_rows[1]
        z0, td = self._z0_td()
        if td <= 0.0:
            # Degenerate zero-delay line -> behave like a DC pass-through wire.
            self.stamp_dc(mna, x)
            return
        t_now = hist.get("_t", 0.0)
        st = hist.get(self.name, {})
        # Incident wave arriving at each port now = wave that left the other port at t-td.
        ea = self._incident(st, "b", z0, t_now - td)  # from B into A
        eb = self._incident(st, "a", z0, t_now - td)  # from A into B

        # KCL: branch currents enter the ports.
        mna.stamp_G(ap, bra, 1.0); mna.stamp_G(an, bra, -1.0)
        mna.stamp_G(bp, brb, 1.0); mna.stamp_G(bn, brb, -1.0)
        # Port-A branch eq: V(a+) - V(a-) - Z0*i_a = Ea
        mna.stamp_G(bra, ap, 1.0); mna.stamp_G(bra, an, -1.0)
        mna.stamp_G(bra, bra, -z0)
        mna.stamp_I(bra, ea)
        # Port-B branch eq: V(b+) - V(b-) - Z0*i_b = Eb
        mna.stamp_G(brb, bp, 1.0); mna.stamp_G(brb, bn, -1.0)
        mna.stamp_G(brb, brb, -z0)
        mna.stamp_I(brb, eb)

    def _incident(self, st: dict, port: str, z0: float, t_delayed: float) -> float:
        """Incident wave from ``port`` evaluated at the (possibly fractional) delayed time.

        Branin: the wave launched at the far port at ``t-TD`` is ``v_far + Z0*i_far`` (i into the
        port). History samples are ``(t, v, i)``; linearly interpolate. Before t=0 the line is
        quiescent -> 0."""
        samples = st.get(port)  # list of (t, v, i)
        if not samples or t_delayed <= samples[0][0]:
            return 0.0
        if t_delayed >= samples[-1][0]:
            t, v, i = samples[-1]
            return v + z0 * i
        # Binary-ish linear scan (histories are short relative to total steps near the front).
        lo = 0
        hi = len(samples) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if samples[mid][0] <= t_delayed:
                lo = mid
            else:
                hi = mid
        t0, v0, i0 = samples[lo]
        t1, v1, i1 = samples[hi]
        frac = (t_delayed - t0) / (t1 - t0) if t1 > t0 else 0.0
        v = v0 + (v1 - v0) * frac
        i = i0 + (i1 - i0) * frac
        return v + z0 * i

    def update_state(self, x: np.ndarray, dt: float, hist: dict) -> None:
        ap, an, bp, bn = self._a, self._an, self._b, self._bn
        bra, brb = self.branch_rows[0], self.branch_rows[1]
        t_now = hist.get("_t", 0.0)
        va = _xat(x, ap) - _xat(x, an)
        vb = _xat(x, bp) - _xat(x, bn)
        ia = _xat(x, bra)
        ib = _xat(x, brb)
        st = hist.setdefault(self.name, {})
        st.setdefault("a", [(0.0, 0.0, 0.0)])
        st.setdefault("b", [(0.0, 0.0, 0.0)])
        # Overwrite the t=0 seed on the first real sample; else append.
        for port, v, i in (("a", va, ia), ("b", vb, ib)):
            lst = st[port]
            if len(lst) == 1 and lst[0][0] == 0.0 and t_now > 0.0:
                lst[0] = (t_now, v, i)
            else:
                lst.append((t_now, v, i))

    def record_indices(self, mna: MnaLike) -> None:
        self.alloc_branches(mna)
        self._a = mna.n(self.node_names[0]); self._an = mna.n(self.node_names[1])
        self._b = mna.n(self.node_names[2]); self._bn = mna.n(self.node_names[3])

    # ---- AC: exact lossless two-port ----
    def stamp_ac(self, mna: MnaLike, x_op: np.ndarray, omega: float) -> None:
        self.alloc_branches(mna)
        ap, an, bp, bn = self._ports(mna)
        z0, td = self._z0_td()
        # The AC two-port is stamped directly as node admittances, so the DC/transient port-current
        # branch rows are unused here. Tie them off (I_branch = 0) so they do not leave singular
        # zero rows in the complex MNA matrix.
        for br in self.branch_rows:
            mna.stamp_Gc(br, br, 1.0)
        # Lossless: gamma*l = j*omega*td. Y-parameters of a line of length l:
        #   Y11 = Y22 = coth(gamma l)/Z0 ;  Y12 = Y21 = -csch(gamma l)/Z0.
        gl = 1j * omega * td
        # Guard the near-DC singularity of coth/csch (gl -> 0) with a tiny offset.
        if abs(gl) < 1e-15:
            gl = 1e-15 + 0j
        th = np.tanh(gl)
        sh = np.sinh(gl)
        y11 = (1.0 / (z0 * th))
        y12 = (-1.0 / (z0 * sh))
        # Stamp the 4-node admittance two-port (ports a+/a-, b+/b-) directly via complex G-stamps.
        for (i, j, y) in (
            (ap, ap, y11), (an, an, y11), (ap, an, -y11), (an, ap, -y11),
            (bp, bp, y11), (bn, bn, y11), (bp, bn, -y11), (bn, bp, -y11),
            (ap, bp, y12), (bp, ap, y12), (an, bn, y12), (bn, an, y12),
            (ap, bn, -y12), (bn, ap, -y12), (an, bp, -y12), (bp, an, -y12),
        ):
            mna.stamp_Gc(i, j, y)


# --------------------------------------------------------------------------- util

def _xat(x: np.ndarray, i: int) -> float:
    """Read solution at matrix row ``i``; ground row (-1) -> 0.0."""
    if i < 0:
        return 0.0
    return float(x[i])
