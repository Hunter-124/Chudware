"""Controlled-impedance + propagation-delay calculators for PCB traces.

This is the "signal-integrity" bridge between the PCB stackup geometry and the native circuit
simulator: it turns trace geometry (width, dielectric height, copper thickness, relative
permittivity) into the characteristic impedance ``Z0`` and propagation delay of a transmission
line, using the standard closed-form models a controlled-impedance fab house uses. The companion
device ``T`` in ``core/sim/`` consumes ``Z0`` + delay so a controlled-impedance
net can actually be simulated for reflections / ringing through the native circuit RPCs.

Closed-form models (cited per-function in the docstrings):

  * **Microstrip** -- IPC-2141A surface-microstrip formula (the classic Kaupp/IPC closed form),
    with the Hammerstad-Jensen effective-permittivity model for the propagation delay so the delay
    uses the dispersion-free quasi-static ``eeff`` rather than ``er`` directly.
  * **Stripline** -- IPC-2141A symmetric (centered) stripline formula. The dielectric fills the
    full space between the two reference planes, so ``eeff == er`` exactly and the line is slower
    (lower ``Z0``) than a microstrip of the same geometry.
  * **Coplanar waveguide with ground (CPWG, "grounded CPW")** -- Wheeler/Wadell conformal-mapping
    approximation using complete elliptic integrals ``K(k)`` (Wen's CPW model extended with the
    lower ground plane). ``eeff`` is the average of the air and dielectric half-space values.
  * **Edge-coupled differential pair** -- the standard single-ended ``Z0`` from the chosen model
    plus the empirical coupling de-rating ``Z_diff = 2*Z0_se*(1 - 0.48*exp(-0.96*s/h))`` (the
    common National/IPC microstrip-diff-pair approximation), so ``Z_diff < 2*Z0_se`` always.

All geometry is in **millimetres** (the canonical PCB-document unit, see pcb_schema.md). Delay is
reported as **ps/mm** (one-way) plus the absolute one-way delay for a supplied length.

Honesty about approximations:
  * These are quasi-static, low-loss, single-frequency-independent (dispersion-free) closed forms.
    They are accurate to roughly +/-5% for typical FR-4 geometries -- the same ballpark a fab
    house quotes -- and intentionally ignore conductor/dielectric loss, surface roughness,
    frequency dispersion, and solder-mask loading. They are not a 2-D field solver.
  * The CPWG uses the standard "thin-conductor" conformal-mapping result (copper thickness ``t``
    enters only as a small effective-width correction); for ``t`` comparable to the gap the result
    degrades gracefully but is approximate.

GPL-clean: pure numpy/scipy maths.
"""
from __future__ import annotations

import math
from typing import Any

from scipy.special import ellipk  # complete elliptic integral K(m), m = k**2

# Speed of light in vacuum (mm/s) -- delay is reported per-mm so we keep mm units throughout.
_C_MM_PER_S = 299_792_458_000.0
# One-way delay of a vacuum line, ps per mm (1/c expressed in ps/mm).
_PS_PER_MM_VACUUM = 1.0e12 / _C_MM_PER_S  # ~3.3356 ps/mm


def _delay_ps_per_mm(eeff: float) -> float:
    """One-way propagation delay in ps/mm for an effective permittivity ``eeff``.

    ``v = c / sqrt(eeff)`` so ``delay = sqrt(eeff)/c``. Pure speed-of-light relation -- this is the
    single source of truth for delay across every model here."""
    return _PS_PER_MM_VACUUM * math.sqrt(max(eeff, 1.0))


# --------------------------------------------------------------------------- microstrip


def microstrip_z0(w: float, h: float, t: float, er: float) -> dict[str, float]:
    """Surface-microstrip characteristic impedance + delay (IPC-2141A).

    IPC-2141A surface-microstrip closed form (also known as the Kaupp / "IPC" formula)::

        Z0 = 87 / sqrt(er + 1.41) * ln( 5.98*h / (0.8*w + t) )      [ohm]

    valid for ``0.1 < w/h < 2.0`` and ``1 < er < 15`` (the IPC validity band; outside it the
    formula degrades but is still a useful estimate).

    The propagation delay uses the Hammerstad-Jensen quasi-static effective permittivity::

        eeff = (er+1)/2 + (er-1)/2 * (1 + 12*h/w)^(-1/2)            [+ thin-trace edge term]

    which is the dispersion-free ``eeff`` a microstrip presents (between 1 and ``er`` because half
    the field is in air). Delay = sqrt(eeff)/c.

    Args (all mm except ``er``): ``w`` trace width, ``h`` dielectric height to the reference plane,
    ``t`` copper thickness, ``er`` substrate relative permittivity.

    Returns ``{z0, eeff, delay_ps_per_mm, model}``.
    """
    w = float(w); h = float(h); t = float(t); er = float(er)
    if w <= 0 or h <= 0 or er < 1.0:
        raise ValueError("microstrip_z0: w,h must be > 0 and er >= 1")

    # IPC-2141A surface microstrip Z0.
    z0 = (87.0 / math.sqrt(er + 1.41)) * math.log(5.98 * h / (0.8 * w + t))
    z0 = max(z0, 0.0)

    eeff = _microstrip_eeff(w, h, t, er)
    return {
        "z0": z0,
        "eeff": eeff,
        "delay_ps_per_mm": _delay_ps_per_mm(eeff),
        "model": "microstrip-ipc2141",
    }


def _microstrip_eeff(w: float, h: float, t: float, er: float) -> float:
    """Hammerstad-Jensen quasi-static effective permittivity for a microstrip.

    ``eeff = (er+1)/2 + (er-1)/2 * (1 + 12 h/w)^(-1/2)`` with the standard small extra term for
    ``w/h < 1`` and a thin-trace correction that nudges ``eeff`` down slightly for finite ``t``."""
    u = w / h
    a = 1.0 + 12.0 / u
    if u >= 1.0:
        eeff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (a ** -0.5)
    else:
        eeff = ((er + 1.0) / 2.0
                + (er - 1.0) / 2.0 * (a ** -0.5 + 0.04 * (1.0 - u) ** 2))
    # Thin-conductor correction (Hammerstad): finite t lowers eeff a touch.
    if t > 0.0:
        eeff -= (er - 1.0) * (t / h) / (4.6 * math.sqrt(u))
    return max(eeff, 1.0)


# --------------------------------------------------------------------------- stripline


def stripline_z0(w: float, h: float, t: float, er: float) -> dict[str, float]:
    """Symmetric (centered) stripline characteristic impedance + delay (IPC-2141A).

    IPC-2141A symmetric stripline closed form -- the trace is centered in a homogeneous dielectric
    of total height ``h`` between two reference planes::

        Z0 = 60 / sqrt(er) * ln( 4*h / (0.67*pi*(0.8*w + t)) )      [ohm]

    Because the field is entirely inside the dielectric, ``eeff == er`` exactly, so a stripline is
    slower and lower-impedance than a microstrip of the same ``w/h`` -- which the tests assert.

    Args (mm / er): ``w`` width, ``h`` *total* plane-to-plane dielectric height, ``t`` copper
    thickness, ``er`` permittivity. Returns ``{z0, eeff, delay_ps_per_mm, model}``.
    """
    w = float(w); h = float(h); t = float(t); er = float(er)
    if w <= 0 or h <= 0 or er < 1.0:
        raise ValueError("stripline_z0: w,h must be > 0 and er >= 1")

    z0 = (60.0 / math.sqrt(er)) * math.log(4.0 * h / (0.67 * math.pi * (0.8 * w + t)))
    z0 = max(z0, 0.0)
    eeff = er  # fully embedded -> effective permittivity is the bulk er
    return {
        "z0": z0,
        "eeff": eeff,
        "delay_ps_per_mm": _delay_ps_per_mm(eeff),
        "model": "stripline-ipc2141",
    }


# --------------------------------------------------------------------------- CPWG


def _ellip_ratio(k: float) -> float:
    """K(k)/K(k') for the complete elliptic integral, with the standard small-/large-k asymptotes
    for numerical robustness near k->0 and k->1 (scipy's ``ellipk`` takes the parameter m=k^2)."""
    k = min(max(k, 1e-9), 1.0 - 1e-12)
    kp = math.sqrt(1.0 - k * k)
    return float(ellipk(k * k) / ellipk(kp * kp))


def coplanar_z0(w: float, s: float, h: float, t: float, er: float) -> dict[str, float]:
    """Grounded coplanar waveguide (CPWG / conductor-backed CPW) Z0 + delay.

    Standard conformal-mapping (Wen) CPW result extended with a lower ground plane (Wadell), using
    complete elliptic integrals::

        k1  = a/b,                       a = w/2, b = w/2 + s
        k3  = tanh(pi*a/(2h)) / tanh(pi*b/(2h))
        eeff = 1 + q*(er - 1),           q = (K(k3)/K(k3')) / (K(k1)/K(k1') + K(k3)/K(k3'))
        Z0   = 60*pi / sqrt(eeff) / ( K(k1)/K(k1') + K(k3)/K(k3') )      [ohm]

    where ``w`` is the signal-trace width, ``s`` the gap to the coplanar ground, ``h`` the
    dielectric height to the lower ground plane, ``t`` the copper thickness (entered as a small
    effective-width widening), ``er`` the permittivity.

    This is the textbook grounded-CPW approximation -- accurate for thin conductors; for thick
    copper relative to the gap it is approximate. Returns ``{z0, eeff, delay_ps_per_mm, model}``.
    """
    w = float(w); s = float(s); h = float(h); t = float(t); er = float(er)
    if w <= 0 or s <= 0 or h <= 0 or er < 1.0:
        raise ValueError("coplanar_z0: w,s,h must be > 0 and er >= 1")

    # Finite-thickness effective-width correction (widen the conductor, narrow the gap a touch).
    if t > 0.0:
        dw = (1.25 * t / math.pi) * (1.0 + math.log(4.0 * math.pi * w / t)) if w > 0 else 0.0
        we = w + dw
        se = s - dw
        if se <= 1e-9:
            we, se = w, s  # correction unstable for very small gaps -> fall back to thin model
    else:
        we, se = w, s

    a = we / 2.0
    b = we / 2.0 + se

    k1 = a / b
    r1 = _ellip_ratio(k1)

    # Lower ground plane (conductor-backed): hyperbolic-tangent mapping.
    k3 = math.tanh(math.pi * a / (2.0 * h)) / math.tanh(math.pi * b / (2.0 * h))
    r3 = _ellip_ratio(k3)

    q = r3 / (r1 + r3)
    eeff = 1.0 + q * (er - 1.0)
    z0 = (60.0 * math.pi / math.sqrt(eeff)) / (r1 + r3)
    return {
        "z0": z0,
        "eeff": eeff,
        "delay_ps_per_mm": _delay_ps_per_mm(eeff),
        "model": "cpwg-conformal",
    }


# --------------------------------------------------------------------------- diff pair


def diff_pair_z0(w: float, s: float, h: float, t: float, er: float,
                 kind: str = "microstrip") -> dict[str, float]:
    """Edge-coupled differential-pair single-ended + differential impedance.

    Computes the single-ended ``Z0`` from the chosen base model (microstrip or stripline) for the
    trace geometry, then applies the standard edge-coupled coupling de-rating::

        Z_diff = 2 * Z0_se * (1 - 0.48 * exp(-0.96 * s/h))     [microstrip]
        Z_diff = 2 * Z0_se * (1 - 0.347 * exp(-2.9 * s/h))     [stripline]

    These are the widely-used National-Semiconductor / IPC approximations: the coupling term is
    always in (0,1), so ``Z_diff < 2*Z0_se`` (broadside/edge coupling lowers the odd-mode
    impedance) -- which the tests assert. ``s`` is the edge-to-edge spacing between the two traces,
    ``h`` the dielectric height.

    Returns ``{z0_se, z0_diff, z0_odd, eeff, delay_ps_per_mm, kind, model}`` where ``z0_odd`` is
    half the differential impedance (the per-line odd-mode impedance).
    """
    kind = str(kind).lower()
    if kind in ("stripline", "strip"):
        base = stripline_z0(w, h, t, er)
        coupling = 1.0 - 0.347 * math.exp(-2.9 * (s / h))
        model = "diffpair-stripline"
    else:
        base = microstrip_z0(w, h, t, er)
        coupling = 1.0 - 0.48 * math.exp(-0.96 * (s / h))
        kind = "microstrip"
        model = "diffpair-microstrip"

    z0_se = base["z0"]
    z0_diff = 2.0 * z0_se * coupling
    return {
        "z0_se": z0_se,
        "z0_diff": z0_diff,
        "z0_odd": z0_diff / 2.0,
        "eeff": base["eeff"],
        "delay_ps_per_mm": base["delay_ps_per_mm"],
        "kind": kind,
        "model": model,
    }


# --------------------------------------------------------------------------- stackup helpers


def _copper_thickness(layer: dict, default: float = 0.035) -> float:
    return float(layer.get("thickness", default) or default)


def _track_length_mm(board: dict, net: str | None, layer: str | None) -> float:
    """Sum the length of every routed segment/arc on ``net`` (optionally restricted to ``layer``)."""
    total = 0.0
    for tr in board.get("tracks", []):
        if net is not None and tr.get("net") != net:
            continue
        if layer is not None and tr.get("layer") != layer:
            continue
        if tr.get("kind", "segment") == "arc":
            r = float(tr.get("radius", 0.0))
            a0 = math.radians(float(tr.get("a0", 0.0)))
            a1 = math.radians(float(tr.get("a1", 0.0)))
            total += abs(a1 - a0) * r
        else:
            p1 = tr.get("p1", [0.0, 0.0]); p2 = tr.get("p2", [0.0, 0.0])
            total += math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    return total


def _stackup_geometry(board: dict, layer: str) -> dict[str, Any]:
    """Classify a copper layer in the stackup and pull the controlled-impedance geometry.

    Reference-plane assumptions (documented, deterministic):

      * The physical ``stackup.layers`` list is ordered top->bottom and alternates copper /
        dielectric (per pcb_schema.md sec.2).
      * **Outer copper** (``F.Cu`` / ``B.Cu``, the first/last copper in the stack) is treated as a
        **microstrip** over the *nearest single dielectric* to the adjacent copper layer (its
        reference plane). ``h`` = that one dielectric's thickness, ``er`` = its ``er``.
      * **Inner copper** (``In*.Cu``) is treated as a **symmetric stripline**: ``h`` = the total
        dielectric thickness between the reference copper above and below it (sum of the two
        adjacent dielectric thicknesses), ``er`` = thickness-weighted mean of those dielectrics.
      * ``t`` = the copper layer's own thickness (default 0.035 mm if unset).
      * ``er`` defaults to 4.3 (typical FR-4) when a dielectric omits it.

    Returns ``{kind: 'microstrip'|'stripline', h, t, er, ref_planes: int}``.
    """
    layers = board.get("stackup", {}).get("layers", [])
    # Index of copper layers in physical order.
    copper_idx = [i for i, ly in enumerate(layers) if ly.get("type") == "copper"]
    # Find the requested copper layer's position in the physical list.
    pos = None
    for i, ly in enumerate(layers):
        if ly.get("id") == layer and ly.get("type") == "copper":
            pos = i
            break
    if pos is None:
        # Layer not in the explicit stackup -> fall back to a default 1.6mm/2 FR-4 microstrip.
        return {"kind": "microstrip", "h": 0.2, "t": 0.035, "er": 4.3, "ref_planes": 1}

    t = _copper_thickness(layers[pos])
    is_outer = pos == copper_idx[0] or pos == copper_idx[-1]

    def _diel(i: int) -> dict | None:
        if 0 <= i < len(layers) and layers[i].get("type") != "copper":
            return layers[i]
        return None

    if is_outer:
        # Microstrip: the single dielectric between this copper and the next copper inward.
        d = _diel(pos + 1) if pos == copper_idx[0] else _diel(pos - 1)
        h = float(d.get("thickness", 0.2)) if d else 0.2
        er = float(d.get("er", 4.3)) if d and d.get("er") is not None else 4.3
        return {"kind": "microstrip", "h": h, "t": t, "er": er, "ref_planes": 1}

    # Inner stripline: dielectric above + below to the nearest reference copper layers.
    above = _diel(pos - 1)
    below = _diel(pos + 1)
    h_above = float(above.get("thickness", 0.2)) if above else 0.2
    h_below = float(below.get("thickness", 0.2)) if below else 0.2
    er_above = float(above.get("er", 4.3)) if above and above.get("er") is not None else 4.3
    er_below = float(below.get("er", 4.3)) if below and below.get("er") is not None else 4.3
    h = h_above + h_below
    er = (er_above * h_above + er_below * h_below) / h if h > 0 else er_above
    return {"kind": "stripline", "h": h, "t": t, "er": er, "ref_planes": 2}


def track_impedance(board: dict, track_or_net: Any) -> dict[str, Any]:
    """Controlled-impedance estimate for a routed track or a whole net on a board.

    Convenience wrapper: pulls the trace **width** from the track, the dielectric **height** and
    **er** from the stackup (nearest reference plane, see :func:`_stackup_geometry`), classifies
    the layer as **microstrip** (outer copper) vs **stripline** (inner copper), and returns the
    characteristic impedance, delay, and the absolute one-way delay over the routed length.

    ``track_or_net`` may be:
      * a track dict (``{"net","layer","width","p1","p2",...}``) -- impedance for that one segment's
        geometry; routed length is the full net length on that layer.
      * a track id string (``"t3"``) -- looked up in ``board['tracks']``.
      * a net name string -- uses the first routed track on that net for width/layer.

    Returns ``{z0, eeff, delay_ps_per_mm, kind, model, width, h, er, t, layer, net,
    length_mm, delay_ps}`` (``delay_ps`` = one-way delay over the routed length on that layer).
    Reference-plane assumptions are documented on :func:`_stackup_geometry`.
    """
    tracks = board.get("tracks", [])
    track: dict | None = None
    net: str | None = None

    if isinstance(track_or_net, dict):
        track = track_or_net
        net = track.get("net")
    elif isinstance(track_or_net, str):
        # Track id first, else treat as a net name.
        for tr in tracks:
            if tr.get("id") == track_or_net:
                track = tr
                net = tr.get("net")
                break
        if track is None:
            net = track_or_net
            for tr in tracks:
                if tr.get("net") == net:
                    track = tr
                    break
    if track is None:
        raise ValueError(f"track_impedance: no track found for {track_or_net!r}")

    width = float(track.get("width", 0.25))
    layer = track.get("layer", "F.Cu")
    geo = _stackup_geometry(board, layer)
    h, t, er = geo["h"], geo["t"], geo["er"]

    if geo["kind"] == "stripline":
        z = stripline_z0(width, h, t, er)
    else:
        z = microstrip_z0(width, h, t, er)

    length_mm = _track_length_mm(board, net, layer)
    delay_ps = z["delay_ps_per_mm"] * length_mm
    return {
        "z0": z["z0"],
        "eeff": z["eeff"],
        "delay_ps_per_mm": z["delay_ps_per_mm"],
        "kind": geo["kind"],
        "model": z["model"],
        "width": width,
        "h": h,
        "er": er,
        "t": t,
        "layer": layer,
        "net": net,
        "length_mm": length_mm,
        "delay_ps": delay_ps,
    }
