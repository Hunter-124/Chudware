"""MCU/FPGA host-board rule pack (T-FIN9) — decoupling, sequencing, crystal, reset, boot-strap.

The first device-specific verification layer above the generic geometric DRC and the
pin-type ERC. ``run_hostcheck(board)`` audits a *board* document for the layout/topology
mistakes that brick an MCU/FPGA host board even when DRC and ERC are clean:

  * per-power-pin decoupling coverage — a suitably-valued cap between the power pad's
    net and ground, close enough, on the same side (via-quality heuristic),
  * bulk capacitance per supply rail,
  * power-supply sequencing — the ``eng.supply.sequencing`` requirements from the
    T-FIN8 metadata layer (contracts/ecad/boardphys.md) validated against the board's
    actual enable/PGOOD chains. NO part knowledge is hardcoded here: the requirement
    comes exclusively from the component's resolved metadata,
  * crystal/oscillator layout — load caps present + matched, close (loop-area
    heuristic), no other-net track crowding the crystal pads (keepout heuristic),
  * reset line conditioning — pull resistor to a rail + optional filter cap,
  * boot-strap pins — tied to a valid level (directly or through one resistor) and not
    shared with an actively-driven signal.

Violations use the DRC shape (``{"rule","severity","locs","items","message"}``,
contracts/ecad — deterministic, sorted, byte-stable) **plus** an additive ``"hint"``
key carrying the actionable fix. Rule ids are STABLE (suppression keys):

  host_decoupling_missing   error    power pad has no suitable cap on its net
  host_decoupling_far       warning  nearest suitable cap beyond the distance budget
  host_decoupling_via       warning  nearest suitable cap on the opposite board side
  host_bulk_cap             warning  supply rail has no bulk cap (>= bulk_min_f)
  host_sequencing           error    sequencing requirement with no enable/PGOOD chain
  host_sequencing_unparsed  warning  sequencing requirement string not parseable
  host_sequencing_unverifiable warning rail source not found — cannot verify the chain
  host_xtal_loadcap         error    crystal pin net lacks a load cap to ground
  host_xtal_loadcap_match   warning  the two load caps differ in value
  host_xtal_loop            warning  load cap farther than the loop-area budget
  host_xtal_keepout         warning  other-net track inside the crystal keepout
  host_reset_pull           error    reset net has no pull resistor to a rail/ground
  host_reset_cap            warning  reset net has no filter cap to ground
  host_bootstrap_float      error    boot-strap net not tied to a valid level
  host_bootstrap_shared     warning  boot-strap net shared with an active driver

Identification is metadata- and convention-driven, never per-part:
  * A **host** is any footprint whose resolved T-FIN8 metadata has a non-empty
    ``supply.rails`` list (multi- or single-rail MCU/FPGA/IC class) OR whose symbol
    def exposes at least one ``power_in`` pin.
  * **Capacitors / resistors** are recognized by libid stem (``Device:C…`` /
    ``Device:R…``) or ref prefix, with values parsed from the standard value field
    (``100n``, ``4.7uF``, ``10k`` …).
  * **Crystals** by libid containing ``crystal``/``resonator``/``xtal`` or ref prefix
    ``Y``/``XTAL``. **Reset / boot-strap / enable / PGOOD** pins by symbol pin-name
    pattern (nRST/RESET/MCLR…, BOOT/MODE/CFG/MSEL…, EN/SHDN/RUN…, PGOOD/PG/POK…).

Pure + advisory, mirrors :mod:`engine.ecad.drc`: never raises on a bad board, never
mutates its input, output fully sorted. All thresholds live in :data:`DEFAULTS` and can
be overridden per-call (``opts``) or per-board (``board["rules"]["hostcheck"]``).
"""

from __future__ import annotations

import math
import re
from typing import Any

from services.ecad import metadata, schema

Point = tuple[float, float]

# ── tunable thresholds (per-board override: board["rules"]["hostcheck"]) ─────────

DEFAULTS: dict[str, float] = {
    "decoupling_dist_mm": 3.0,     # power pad -> decoupling cap budget
    "decoupling_min_f": 1e-8,      # 10 nF   — smallest cap that counts as decoupling
    "decoupling_max_f": 1e-6,      # 1 uF    — largest cap that counts as decoupling
    "bulk_min_f": 4.7e-6,          # 4.7 uF  — smallest cap that counts as bulk
    "xtal_loop_dist_mm": 5.0,      # crystal -> load-cap loop-area budget
    "xtal_keepout_mm": 1.0,        # other-net track clearance around crystal pads
}

# ── name patterns (conventions, not part knowledge) ──────────────────────────────

_GND_RE = re.compile(r"^(GND\w*|AGND|DGND|PGND|VSS\w*|0)$", re.IGNORECASE)
_RAIL_RE = re.compile(r"^(V(CC|DD|BAT|IN|OUT|SYS|BUS)\w*|\+?\d+V\d*|\d+V\d+|"
                      r"\+\d+(\.\d+)?V?|VCORE\w*|VIO\w*|AVDD\w*|DVDD\w*)$",
                      re.IGNORECASE)
_RESET_RE = re.compile(r"^[~/!n]?(RST|RESET|MCLR|SRST|POR)[~/!n]?$", re.IGNORECASE)
_BOOT_RE = re.compile(r"^(BOOT\d*|MODE\d*|CFG\d*|CONFIG\d*|MSEL\d*|M[01]|BSEL\d*|"
                      r"STRAP\d*|SEL\d*)$", re.IGNORECASE)
_EN_RE = re.compile(r"^[~/!n]?(EN\d*|ENABLE\d*|SHDN|RUN|INH|CE|ON/?OFF|ON)[~/!n]?$",
                    re.IGNORECASE)
_PGOOD_RE = re.compile(r"^[~/!n]?(PGOOD\d*|PG\d*|POK|POWER_?GOOD|PWRGD|RESET_?OUT)[~/!n]?$",
                       re.IGNORECASE)
_SEQ_RE = re.compile(r"^\s*(\S+)\s*(?:before|->|then|→)\s*(\S+)\s*$", re.IGNORECASE)

_ACTIVE_DRIVER_TYPES = {"output", "bidirectional", "tri_state"}

_CAP_VALUE_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?|\.\d+)\s*([pnumkMGµ]?)F?\s*$")
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
       "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9}


def parse_cap_value(value: Any) -> float | None:
    """Parse a capacitor value field ('100n', '4.7uF', '0.1u') to farads, or None."""
    if not isinstance(value, str):
        return None
    m = _CAP_VALUE_RE.match(value)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return num * _SI.get(m.group(2), 1.0)


# ── violation constructor (DRC shape + additive "hint") ──────────────────────────

def _violation(rule: str, severity: str, locs: list, items: list, message: str,
               hint: str) -> dict:
    return {
        "rule": rule,
        "severity": severity,
        "locs": [[float(x), float(y), str(layer)] for (x, y, layer) in locs],
        "items": sorted(str(i) for i in items),
        "message": message,
        "hint": hint,
    }


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ── board indexing ────────────────────────────────────────────────────────────────

def _lib(library: Any):
    if library is not None:
        return library
    from services.ecad import library as lib_mod
    return lib_mod


class _Comp:
    """One indexed footprint: resolved metadata + pad->pin mapping via the symbol def."""

    __slots__ = ("fp", "fpid", "libid", "ref", "value", "side", "pos", "eng", "pads")

    def __init__(self, fp: dict, library: Any) -> None:
        self.fp = fp
        self.fpid = str(fp.get("fpid") or "")
        self.libid = str(fp.get("libid") or "")
        self.ref = str(fp.get("ref") or fp.get("id") or "")
        self.value = str(fp.get("value") or "")
        self.side = str(fp.get("side") or "top")
        self.pos = tuple(float(c) for c in (fp.get("pos") or [0.0, 0.0])[:2])
        self.eng = metadata.eng_for_footprint(fp, library)
        # pad -> {"net","at","pin_name","pin_type"} keyed by pad id
        sdef = None
        if self.libid:
            try:
                sdef = _lib(library).resolve(self.libid)
            except Exception:  # noqa: BLE001 — a broken lib entry must not kill the check
                sdef = None
        pins_by_number: dict[str, dict] = {}
        for p in (sdef or {}).get("pins", []) or []:
            pins_by_number[str(p.get("number", ""))] = p
        self.pads = []
        for pad in fp.get("pads", []) or []:
            pin = pins_by_number.get(str(pad.get("number", "")))
            try:
                at = schema.pad_world(fp, pad)
            except Exception:  # noqa: BLE001
                at = self.pos
            self.pads.append({
                "id": str(pad.get("id", "")),
                "number": str(pad.get("number", "")),
                "net": pad.get("net"),
                "at": (float(at[0]), float(at[1])),
                "pin_name": str((pin or {}).get("name", "") or ""),
                "pin_type": str((pin or {}).get("type", "") or ""),
            })
        self.pads.sort(key=lambda p: p["id"])


def _is_cap(c: _Comp) -> bool:
    stem = c.libid.split(":")[-1] if c.libid else ""
    if stem and (stem == "C" or stem.startswith(("C_", "CP", "C0", "C1"))):
        return True
    return bool(re.match(r"^C\d", c.ref))


def _is_res(c: _Comp) -> bool:
    stem = c.libid.split(":")[-1] if c.libid else ""
    if stem and (stem == "R" or stem.startswith(("R_", "R0", "R1"))):
        return True
    return bool(re.match(r"^R\d", c.ref))


def _is_crystal(c: _Comp) -> bool:
    lid = c.libid.lower()
    if any(k in lid for k in ("crystal", "resonator", "xtal", "oscillator")):
        return True
    return bool(re.match(r"^(Y\d|XTAL)", c.ref, re.IGNORECASE))


def _is_gnd_net(name: Any) -> bool:
    return isinstance(name, str) and bool(_GND_RE.match(name))


def _is_rail_net(name: Any) -> bool:
    return isinstance(name, str) and bool(_RAIL_RE.match(name))


def _two_pin_nets(c: _Comp) -> tuple[Any, Any] | None:
    """The (net_a, net_b) of a two-terminal passive, or None."""
    nets = [p["net"] for p in c.pads]
    if len(nets) != 2:
        return None
    return (nets[0], nets[1])


def _cap_between(caps: list[_Comp], net: Any, min_f: float | None = None,
                 max_f: float | None = None) -> list[_Comp]:
    """Caps with one terminal on ``net`` and the other on a ground net, value in range.

    A cap with an unparseable/absent value is included only when no value bound is
    requested (honesty: unknown is not a claim either way — but a *sized* requirement
    can only be satisfied by a cap whose value is actually known).
    """
    out = []
    for c in caps:
        tp = _two_pin_nets(c)
        if tp is None:
            continue
        a, b = tp
        if not ((a == net and _is_gnd_net(b)) or (b == net and _is_gnd_net(a))):
            continue
        f = parse_cap_value(c.value)
        if min_f is None and max_f is None:
            out.append(c)
            continue
        if f is None:
            continue
        if (min_f is None or f >= min_f) and (max_f is None or f <= max_f):
            out.append(c)
    return out


# ── the rule families ─────────────────────────────────────────────────────────────

def _check_decoupling(host: _Comp, caps: list[_Comp], cfg: dict, out: list[dict]) -> None:
    """host_decoupling_missing / _far / _via — per power_in pad of the host."""
    for pad in host.pads:
        if pad["pin_type"] != "power_in":
            continue
        net = pad["net"]
        if not net or _is_gnd_net(net):
            continue  # ground pins need no decoupling cap of their own
        pin_label = f"{host.ref}.{pad['number']}" + (
            f" ({pad['pin_name']})" if pad["pin_name"] else "")
        loc = (pad["at"][0], pad["at"][1], "F.Cu" if host.side == "top" else "B.Cu")
        suitable = _cap_between(caps, net, cfg["decoupling_min_f"], cfg["decoupling_max_f"])
        if not suitable:
            out.append(_violation(
                "host_decoupling_missing", "error", [loc], [host.ref, pad["id"]],
                f"power pin {pin_label} on net {net!r} has no decoupling capacitor "
                f"({cfg['decoupling_min_f'] * 1e9:.0f} nF – "
                f"{cfg['decoupling_max_f'] * 1e6:.2g} uF) to ground",
                f"Add a 100 nF ceramic (X7R) from net {net!r} to GND within "
                f"{cfg['decoupling_dist_mm']:.1f} mm of pin {pin_label}."))
            continue
        nearest = min(suitable, key=lambda c: (_dist(c.pos, pad["at"]), c.ref))
        d = _dist(nearest.pos, pad["at"])
        if d > cfg["decoupling_dist_mm"]:
            out.append(_violation(
                "host_decoupling_far", "warning", [loc], [host.ref, nearest.ref],
                f"nearest decoupling cap {nearest.ref} ({nearest.value or '?'}) for pin "
                f"{pin_label} is {d:.2f} mm away (> {cfg['decoupling_dist_mm']:.1f} mm)",
                f"Move {nearest.ref} to within {cfg['decoupling_dist_mm']:.1f} mm of pin "
                f"{pin_label} — decoupling inductance grows with loop length."))
        elif nearest.side != host.side:
            out.append(_violation(
                "host_decoupling_via", "warning", [loc], [host.ref, nearest.ref],
                f"decoupling cap {nearest.ref} for pin {pin_label} sits on the "
                f"{nearest.side} side while {host.ref} is on the {host.side} side "
                f"(every connection adds via inductance)",
                f"Place {nearest.ref} on the {host.side} side next to pin {pin_label}, "
                f"or use at least two low-inductance vias per terminal."))


def _check_bulk(host: _Comp, caps: list[_Comp], cfg: dict, out: list[dict],
                done_rails: set) -> None:
    """host_bulk_cap — one bulk cap per supply rail feeding the host."""
    rail_nets = sorted({p["net"] for p in host.pads
                        if p["pin_type"] == "power_in" and p["net"]
                        and not _is_gnd_net(p["net"])})
    for net in rail_nets:
        if net in done_rails:
            continue
        done_rails.add(net)
        bulk = _cap_between(caps, net, cfg["bulk_min_f"], None)
        if not bulk:
            pad = next(p for p in host.pads if p["net"] == net)
            loc = (pad["at"][0], pad["at"][1], "F.Cu" if host.side == "top" else "B.Cu")
            out.append(_violation(
                "host_bulk_cap", "warning", [loc], [host.ref],
                f"supply rail {net!r} has no bulk capacitor "
                f">= {cfg['bulk_min_f'] * 1e6:.2g} uF",
                f"Add a bulk cap (e.g. 10 uF ceramic or tantalum) from {net!r} to GND "
                f"near the rail's entry/regulator to hold up transient load steps."))


def _rail_source(comps: list[_Comp], rail: str) -> _Comp | None:
    """The component sourcing a rail net: a power_out pin on a net matching the rail name."""
    for c in comps:
        for p in c.pads:
            if p["pin_type"] == "power_out" and isinstance(p["net"], str) \
                    and p["net"].upper() == rail.upper():
                return c
    return None


def _check_sequencing(host: _Comp, comps: list[_Comp], out: list[dict]) -> None:
    """host_sequencing / _unparsed / _unverifiable — from the T-FIN8 metadata ONLY.

    ``supply.sequencing == []`` means "known: none required" (nothing to check);
    ``None`` means unknown (honest silence — we cannot invent a requirement).
    """
    seq = (host.eng.get("supply") or {}).get("sequencing")
    if not isinstance(seq, list):
        return
    loc = (host.pos[0], host.pos[1], "F.Cu" if host.side == "top" else "B.Cu")
    for req in seq:
        if not isinstance(req, str):
            continue
        m = _SEQ_RE.match(req)
        if not m:
            out.append(_violation(
                "host_sequencing_unparsed", "warning", [loc], [host.ref],
                f"{host.ref} sequencing requirement {req!r} is not machine-checkable "
                f"(expected '<RAIL_A> before <RAIL_B>')",
                f"Rewrite the eng.supply.sequencing entry as '<RAIL_A> before <RAIL_B>' "
                f"(rail names matching the board nets) so the chain can be verified."))
            continue
        rail_a, rail_b = m.group(1), m.group(2)
        src_b = _rail_source(comps, rail_b)
        if src_b is None:
            out.append(_violation(
                "host_sequencing_unverifiable", "warning", [loc], [host.ref],
                f"{host.ref} requires {rail_a!r} before {rail_b!r} but no component "
                f"with a power_out pin drives a net named {rail_b!r} — cannot verify "
                f"the enable chain",
                f"Name the regulator output net {rail_b!r} (matching the sequencing "
                f"requirement) or add the regulator so the chain can be checked."))
            continue
        # the later rail's regulator must have an EN-class pin gated by rail A:
        # EN net == rail A itself, or EN net shared with a PGOOD-class pin of rail A's
        # source (or of any component sitting on rail A).
        src_a = _rail_source(comps, rail_a)
        en_pads = [p for p in src_b.pads if _EN_RE.match(p["pin_name"] or "")]
        ok = False
        for en in en_pads:
            en_net = en["net"]
            if not en_net:
                continue
            if isinstance(en_net, str) and en_net.upper() == rail_a.upper():
                ok = True
                break
            # PGOOD chain: any PGOOD-class pin on the EN net belonging to rail A's
            # source (preferred) or to a component with a pin on rail A.
            for c in comps:
                for p in c.pads:
                    if p["net"] == en_net and _PGOOD_RE.match(p["pin_name"] or ""):
                        on_rail_a = (src_a is not None and c is src_a) or any(
                            isinstance(q["net"], str) and q["net"].upper() == rail_a.upper()
                            for q in c.pads)
                        if on_rail_a:
                            ok = True
            if ok:
                break
        if not ok:
            out.append(_violation(
                "host_sequencing", "error", [loc], [host.ref, src_b.ref],
                f"{host.ref} requires {rail_a!r} before {rail_b!r}, but {src_b.ref} "
                f"(the {rail_b!r} source) has no enable/PGOOD chain enforcing that "
                f"order" + ("" if en_pads else " (no EN-class pin found)"),
                f"Gate {src_b.ref}'s EN pin from {rail_a!r} — tie EN to the "
                f"{rail_a!r} net or to the PGOOD output of the {rail_a!r} regulator "
                f"so {rail_b!r} can only rise after {rail_a!r} is up."))


def _check_crystal(xtal: _Comp, caps: list[_Comp], comps: list[_Comp],
                   tracks: list[dict], cfg: dict, out: list[dict]) -> None:
    """host_xtal_loadcap / _loadcap_match / _loop / _keepout."""
    loc = (xtal.pos[0], xtal.pos[1], "F.Cu" if xtal.side == "top" else "B.Cu")
    xtal_nets = sorted({p["net"] for p in xtal.pads
                        if p["net"] and not _is_gnd_net(p["net"])
                        and not _is_rail_net(p["net"])})
    load_vals: list[tuple[str, float | None, _Comp]] = []  # (net, farads, cap)
    for net in xtal_nets:
        load = _cap_between(caps, net)  # any value counts as present
        if not load:
            out.append(_violation(
                "host_xtal_loadcap", "error", [loc], [xtal.ref],
                f"crystal {xtal.ref} pin net {net!r} has no load capacitor to ground",
                f"Add a load cap (typ. 12–22 pF, from the crystal's CL spec) from "
                f"{net!r} to GND right at the crystal."))
            continue
        nearest = min(load, key=lambda c: (_dist(c.pos, xtal.pos), c.ref))
        load_vals.append((net, parse_cap_value(nearest.value), nearest))
        d = _dist(nearest.pos, xtal.pos)
        if d > cfg["xtal_loop_dist_mm"]:
            out.append(_violation(
                "host_xtal_loop", "warning", [loc], [xtal.ref, nearest.ref],
                f"load cap {nearest.ref} is {d:.2f} mm from crystal {xtal.ref} "
                f"(> {cfg['xtal_loop_dist_mm']:.1f} mm) — oscillator loop area too large",
                f"Move {nearest.ref} next to {xtal.ref} and keep the ground return "
                f"short; large loops radiate and pick up noise."))
    if len(load_vals) == 2:
        (na, fa, ca), (nb, fb, cb) = load_vals
        if fa is not None and fb is not None and fa != fb:
            out.append(_violation(
                "host_xtal_loadcap_match", "warning", [loc],
                [xtal.ref, ca.ref, cb.ref],
                f"crystal {xtal.ref} load caps are mismatched: {ca.ref}={ca.value} on "
                f"{na!r} vs {cb.ref}={cb.value} on {nb!r}",
                f"Use the same load-cap value on both crystal pins (from CL = "
                f"2*(C_load) - C_stray) — mismatch pulls the frequency and can stall "
                f"startup."))
    # keepout heuristic: any other-net track segment too close to a crystal pad.
    for pad in xtal.pads:
        if not pad["net"]:
            continue
        for t in tracks:
            if t.get("kind", "segment") != "segment":
                continue
            if t.get("net") in (None, "", pad["net"]) or t.get("net") in xtal_nets \
                    or _is_gnd_net(t.get("net")):
                continue
            p1 = tuple(float(c) for c in t.get("p1", [0, 0]))
            p2 = tuple(float(c) for c in t.get("p2", [0, 0]))
            # point-to-segment distance
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            denom = dx * dx + dy * dy
            if denom <= 1e-12:
                d = _dist(pad["at"], p1)
            else:
                tt = max(0.0, min(1.0, ((pad["at"][0] - p1[0]) * dx +
                                        (pad["at"][1] - p1[1]) * dy) / denom))
                d = _dist(pad["at"], (p1[0] + tt * dx, p1[1] + tt * dy))
            d -= float(t.get("width", 0.0)) / 2.0
            if d < cfg["xtal_keepout_mm"]:
                out.append(_violation(
                    "host_xtal_keepout", "warning",
                    [(pad["at"][0], pad["at"][1], str(t.get("layer", "F.Cu")))],
                    [xtal.ref, t.get("id", "")],
                    f"track {t.get('id', '')} (net {t.get('net')!r}) passes "
                    f"{max(d, 0.0):.2f} mm from crystal {xtal.ref} pad {pad['number']} "
                    f"(< {cfg['xtal_keepout_mm']:.1f} mm keepout)",
                    f"Route net {t.get('net')!r} away from the crystal — keep a "
                    f"{cfg['xtal_keepout_mm']:.1f} mm quiet zone (ideally a local "
                    f"ground pour) around the oscillator."))


def _res_link(resistors: list[_Comp], net: Any) -> list[tuple[_Comp, Any]]:
    """Resistors with one terminal on ``net``: [(res, other_net)]."""
    out = []
    for r in resistors:
        tp = _two_pin_nets(r)
        if tp is None:
            continue
        a, b = tp
        if a == net:
            out.append((r, b))
        elif b == net:
            out.append((r, a))
    return out


def _check_reset(host: _Comp, caps: list[_Comp], resistors: list[_Comp],
                 out: list[dict]) -> None:
    """host_reset_pull / host_reset_cap."""
    for pad in host.pads:
        if not _RESET_RE.match(pad["pin_name"] or ""):
            continue
        net = pad["net"]
        pin_label = f"{host.ref}.{pad['number']} ({pad['pin_name']})"
        loc = (pad["at"][0], pad["at"][1], "F.Cu" if host.side == "top" else "B.Cu")
        if not net:
            out.append(_violation(
                "host_reset_pull", "error", [loc], [host.ref, pad["id"]],
                f"reset pin {pin_label} is unconnected",
                f"Connect {pin_label} and pull it to its inactive level through a "
                f"resistor (typ. 10 k)."))
            continue
        pulls = [(r, other) for (r, other) in _res_link(resistors, net)
                 if _is_rail_net(other) or _is_gnd_net(other)]
        if not pulls:
            out.append(_violation(
                "host_reset_pull", "error", [loc], [host.ref, pad["id"]],
                f"reset pin {pin_label} on net {net!r} has no pull resistor to a "
                f"supply rail or ground",
                f"Add a pull resistor (typ. 10 k) from {net!r} to the pin's inactive "
                f"level so the part cannot float in/out of reset."))
        if not _cap_between(caps, net):
            out.append(_violation(
                "host_reset_cap", "warning", [loc], [host.ref, pad["id"]],
                f"reset pin {pin_label} on net {net!r} has no filter capacitor to "
                f"ground",
                f"Add a small cap (typ. 100 nF) from {net!r} to GND to debounce the "
                f"reset line (check the debugger/programmer's reset-drive spec first)."))


def _check_bootstrap(host: _Comp, comps: list[_Comp], resistors: list[_Comp],
                     out: list[dict]) -> None:
    """host_bootstrap_float / host_bootstrap_shared."""
    for pad in host.pads:
        if not _BOOT_RE.match(pad["pin_name"] or ""):
            continue
        net = pad["net"]
        pin_label = f"{host.ref}.{pad['number']} ({pad['pin_name']})"
        loc = (pad["at"][0], pad["at"][1], "F.Cu" if host.side == "top" else "B.Cu")
        if not net:
            out.append(_violation(
                "host_bootstrap_float", "error", [loc], [host.ref, pad["id"]],
                f"boot-strap pin {pin_label} is unconnected — its level at reset is "
                f"undefined",
                f"Tie {pin_label} to its required logic level through a resistor "
                f"(typ. 10 k) so the boot mode is deterministic."))
            continue
        tied = _is_rail_net(net) or _is_gnd_net(net) or any(
            _is_rail_net(other) or _is_gnd_net(other)
            for (_r, other) in _res_link(resistors, net))
        if not tied:
            out.append(_violation(
                "host_bootstrap_float", "error", [loc], [host.ref, pad["id"]],
                f"boot-strap pin {pin_label} on net {net!r} is not tied to a valid "
                f"level (no direct or resistor path to a rail or ground)",
                f"Strap {net!r} to the required level through a resistor (typ. 10 k) "
                f"— a floating strap pin boots into a random mode."))
        # shared with an active signal: any output-class pin of ANOTHER component on
        # the same net (a driver fighting or toggling the strap during reset).
        drivers = sorted(
            f"{c.ref}.{p['number']}" for c in comps if c is not host
            for p in c.pads
            if p["net"] == net and p["pin_type"] in _ACTIVE_DRIVER_TYPES)
        if drivers:
            out.append(_violation(
                "host_bootstrap_shared", "warning", [loc],
                [host.ref, pad["id"]] + [d.split(".")[0] for d in drivers],
                f"boot-strap pin {pin_label} shares net {net!r} with active "
                f"driver(s): {', '.join(drivers)} — the strap level at reset depends "
                f"on their state",
                f"Give {pin_label} a dedicated strap resistor and isolate the shared "
                f"function with a series resistor, or move the signal to another pin."))


# ── main entry point ──────────────────────────────────────────────────────────────

def run_hostcheck(board: dict, library: Any = None, opts: dict | None = None) -> list[dict]:
    """Run the host-board rule pack over a board document.

    Pure + advisory (DRC conventions): deterministic sorted list of violations
    (DRC shape + ``hint``), never raises on a bad board, never mutates the input.
    ``library`` — optional resolver override (must expose ``resolve`` /
    ``resolve_footprint``); defaults to the bundled library. ``opts`` /
    ``board["rules"]["hostcheck"]`` override :data:`DEFAULTS` thresholds.
    """
    out: list[dict] = []
    if not schema.is_board(board):
        return out
    cfg = dict(DEFAULTS)
    board_over = (board.get("rules") or {}).get("hostcheck")
    if isinstance(board_over, dict):
        cfg.update({k: float(v) for k, v in board_over.items() if k in DEFAULTS})
    if isinstance(opts, dict):
        cfg.update({k: float(v) for k, v in opts.items() if k in DEFAULTS})

    comps = [_Comp(fp, library) for fp in board.get("footprints", []) or []
             if isinstance(fp, dict)]
    comps.sort(key=lambda c: c.ref)
    caps = [c for c in comps if _is_cap(c)]
    resistors = [c for c in comps if _is_res(c)]
    tracks = board.get("tracks", []) or []

    def _is_host(c: _Comp) -> bool:
        rails = (c.eng.get("supply") or {}).get("rails")
        if isinstance(rails, list) and rails:
            return True
        return any(p["pin_type"] == "power_in" for p in c.pads)

    hosts = [c for c in comps if _is_host(c) and not _is_cap(c) and not _is_res(c)
             and not _is_crystal(c)]

    done_rails: set = set()
    for host in hosts:
        _check_decoupling(host, caps, cfg, out)
        _check_bulk(host, caps, cfg, out, done_rails)
        _check_sequencing(host, comps, out)
        _check_reset(host, caps, resistors, out)
        _check_bootstrap(host, comps, resistors, out)
    for xtal in (c for c in comps if _is_crystal(c)):
        _check_crystal(xtal, caps, comps, tracks, cfg, out)

    out.sort(key=lambda v: (v["rule"], v["severity"], tuple(v["items"]),
                            tuple(tuple(loc) for loc in v["locs"]), v["message"]))
    return out


if __name__ == "__main__":  # pragma: no cover - smoke
    import json
    import sys
    doc = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else schema.new_board()
    print(json.dumps({"violations": run_hostcheck(doc)}, indent=2, sort_keys=True))
