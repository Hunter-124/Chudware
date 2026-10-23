#!/usr/bin/env python3
"""gsl-ser/v1 — the frozen canonical GSL text serialization.

This module is the EXECUTABLE reference for `contracts/gsl_ser.md`. The contract prose
is normative for wording; this code is normative for behaviour. Two directions:

    serialize(state)         state dict  -> canonical gsl-ser/v1 text  (the prompt input)
    extract_slots(text)      gsl-ser/v1 text -> ordered list[Slot]     (round-trip / verify)
    enumerate_slots(state)   state dict  -> ordered list[Slot]         (ground-truth slots)
    render_caption(state)    state dict  -> deterministic prose caption (the GSL template)

The round-trip invariant the tests assert:

    extract_slots(serialize(state)) == enumerate_slots(state)

holds exactly because both sides carry the *canonical rounded* value (`Slot.value` is the
value AS SERIALIZED, at the narration.md precision for its unit — never the raw float).

Design boundary: model-produced text never invents a numeral; every numeral in a
caption must trace to a Slot enumerated here. The groundedness checker
(`tools/translator/groundedness.py`) scores text against these slots and flags
extrinsic (invented) numerals. This file is engine-free: it operates on state DICTS
(the `_digest`-shaped payload the deterministic describers already produce), so it runs
in CI with no libchudware.

Versioning: the header line `#gsl-ser/v1 ...` is tagged in every serialization AND must
be tagged in every prompt (per the spec). Bump to `/v2` only via a versioned
contract change; extractors accept any `gsl-ser/vN` header they know.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

SER_VERSION = "gsl-ser/v1"

# --------------------------------------------------------------------------- units
# Canonical unit vocabulary + narration.md precision. `fmt` renders a value to its
# canonical string; the parsed-back float of that string IS the canonical value, so
# round-trips are exact by construction. `dimensionless`/enum slots use unit "none".
#
# kind ∈ {dim, count, tolerance, material, enum}. `dim`/`tolerance` carry a physical
# unit; `count` is an integer with unit "count"; `material`/`enum` are categorical
# (unit "none", value is a string).

_UNIT_FMT: dict[str, str] = {
    "mm": "{:.3g}",        # linear dimensions — 3 sig figs (narration.md)
    "mm3": "{:.3g}",       # volumes
    "deg": "{:.4g}",       # angles (keeps 360, 45, 22.5)
    "MPa": "{:.1f}",       # stresses — 1 decimal
    "Hz": "{:.1f}",        # frequencies — 1 decimal
    "mm/min": "{:.0f}",    # feed rates — integer
    "s": "{:.1f}",         # cycle/est times — 1 decimal
    "pct": "{:.1f}",       # percentages — 1 decimal
    "ohm": "{:.1f}",       # trace impedance — 1 decimal
    "ratio": "{:.2f}",     # dimensionless ratios (factor of safety) — 2 decimals
    "count": "{:.0f}",     # integer counts
    "none": "{}",          # categorical
}

# Dimensionless numeric units that appear BARE in prose (no unit token). A bare number
# in a caption is an acceptable candidate for these; `count` compares as an integer,
# `ratio` within tolerance.
_BARE_UNITS = ("count", "ratio")

# Aliases normalised on extraction of FREE text (captions/paraphrases). The strict
# serialization only ever emits canonical units; free-text extraction tolerates a model
# that writes an equivalent unit. Unit *sanity* (ksi/MPa 6.9x etc.) is the verify
# battery's unit_sanity check, not this file — we only fold exact-equivalent spellings.
_UNIT_ALIAS: dict[str, tuple[str, float]] = {
    "mm": ("mm", 1.0), "millimeter": ("mm", 1.0), "millimeters": ("mm", 1.0),
    "cm": ("mm", 10.0), "m": ("mm", 1000.0),
    "mm^3": ("mm3", 1.0), "mm3": ("mm3", 1.0), "cubic mm": ("mm3", 1.0),
    "deg": ("deg", 1.0), "degrees": ("deg", 1.0), "°": ("deg", 1.0),
    "mpa": ("MPa", 1.0), "MPa": ("MPa", 1.0), "n/mm2": ("MPa", 1.0),
    "hz": ("Hz", 1.0), "Hz": ("Hz", 1.0),
    "mm/min": ("mm/min", 1.0),
    "s": ("s", 1.0), "sec": ("s", 1.0), "seconds": ("s", 1.0),
    "%": ("pct", 1.0), "pct": ("pct", 1.0), "percent": ("pct", 1.0),
    "ohm": ("ohm", 1.0), "ohms": ("ohm", 1.0), "Ω": ("ohm", 1.0),
    "count": ("count", 1.0), "ratio": ("ratio", 1.0),
}


def canon(value: float, unit: str) -> float:
    """The canonical (serialized) numeric value for `value` in `unit`: format then
    re-parse, so `enumerate_slots` and `extract_slots` agree to the last digit."""
    fmt = _UNIT_FMT.get(unit, "{}")
    return float(fmt.format(float(value)))


def fmt_value(value: Any, unit: str) -> str:
    """Render one slot value + unit as its canonical token, e.g. `6.5mm`, `6count`,
    `1018_steel` (categorical, no unit suffix)."""
    if unit == "none":
        return _enum_token(value)
    return _UNIT_FMT.get(unit, "{}").format(float(value)) + unit


def _enum_token(value: Any) -> str:
    """Categorical value -> a bare, whitespace-free token (so it survives extraction)."""
    return re.sub(r"\s+", "_", str(value).strip())


def prose_token(value: Any, unit: str) -> str:
    """Render a value for natural-language captions: counts and enums appear bare
    (`6`, `bolt_circle`), physical dims keep their unit (`6.5mm`). Distinct from the
    strict `fmt_value`, which suffixes `count` for exact machine round-trips."""
    if unit == "none":
        return _enum_token(value)
    if unit in _BARE_UNITS:
        return _UNIT_FMT.get(unit, "{}").format(float(value))
    return fmt_value(value, unit)


# --------------------------------------------------------------------------- Slot


@dataclass(frozen=True)
class Slot:
    """One atomic, verifiable fact: a fully-qualified path, its canonical value, unit
    and kind. Numeric slots compare unit-normalised; categorical slots compare by token."""

    path: str
    value: Any        # float for numeric kinds, str for material/enum
    unit: str
    kind: str         # dim | count | tolerance | material | enum

    @property
    def numeric(self) -> bool:
        return self.kind in ("dim", "count", "tolerance")

    def token(self) -> str:
        return fmt_value(self.value, self.unit)

    def matches(self, other_value: float, other_unit: str, *, rel_tol: float = 1e-3,
                abs_tol: float = 1e-6) -> bool:
        """Does a candidate (value, unit) equal this slot, unit-normalised? For counts
        the match is exact; for dims/tolerances it is within rel/abs tolerance."""
        if not self.numeric:
            return _enum_token(self.value) == _enum_token(other_value)
        base_u, factor = _UNIT_ALIAS.get(other_unit, (other_unit, 1.0))
        if self.unit in _BARE_UNITS:
            # Dimensionless: reads as a bare number in prose ("6 holes", "FOS 1.43"),
            # so a unitless numeral is an acceptable candidate.
            if base_u not in (self.unit, "none"):
                return False
            cand = float(other_value) * factor
            if self.unit == "count":
                return round(cand) == round(float(self.value))
            return abs(cand - float(self.value)) <= max(abs_tol, rel_tol * abs(float(self.value)))
        if base_u != self.unit:
            return False
        cand = float(other_value) * factor
        return abs(cand - float(self.value)) <= max(abs_tol, rel_tol * abs(float(self.value)))


# --------------------------------------------------------------- state -> slots (truth)
#
# The state dict is the `_digest`-shaped payload (services/mcp/gsl_tools.py `_digest`)
# extended with optional `domain`, `sketch`, `cam`, `fea`, `ecad` sections. Only the keys
# present contribute slots — a partial state (e.g. FEA-only) enumerates only its slots.


def _num_slot(path: str, value: Any, unit: str, kind: str,
              out: list[Slot]) -> None:
    if value is None:
        return
    try:
        out.append(Slot(path, canon(value, unit), unit, kind))
    except (TypeError, ValueError):
        return


def enumerate_slots(state: dict) -> list[Slot]:
    """The ground-truth ordered slot list for a state. Deterministic order == the order
    `serialize` emits, so `extract_slots(serialize(state))` compares 1:1."""
    out: list[Slot] = []
    part = state.get("part")
    if part is not None:
        out.append(Slot("part.name", _enum_token(part), "none", "enum"))
    bb = state.get("bbox_mm")
    if isinstance(bb, (list, tuple)) and len(bb) == 3:
        for axis, v in zip(("x", "y", "z"), bb):
            _num_slot(f"part.bbox.{axis}", v, "mm", "dim", out)
    _num_slot("part.faces", state.get("faces"), "count", "count", out)
    _num_slot("part.edges", state.get("edges"), "count", "count", out)

    sym = state.get("symmetry")
    if isinstance(sym, dict) and sym.get("order"):
        _num_slot("part.sym.order", sym.get("order"), "count", "count", out)
        if sym.get("type"):
            out.append(Slot("part.sym.type", _enum_token(sym["type"]), "none", "enum"))

    for fg in state.get("feature_groups") or []:
        if not isinstance(fg, dict):
            continue
        fid = _enum_token(fg.get("name") or fg.get("id") or "feat")
        base = f"feat.{fid}"
        if fg.get("type"):
            out.append(Slot(f"{base}.type", _enum_token(fg["type"]), "none", "enum"))
        _num_slot(f"{base}.faces", fg.get("faces"), "count", "count", out)
        for k, unit in (("diameter", "mm"), ("pcd", "mm"), ("count", "count"),
                        ("depth", "mm"), ("thickness", "mm"), ("angle", "deg"),
                        ("height", "mm"), ("radius", "mm")):
            if k in (fg.get("params") or {}):
                _num_slot(f"{base}.{k}", fg["params"][k], unit,
                          "count" if unit == "count" else "dim", out)

    for i, b in enumerate(state.get("bores") or []):
        if isinstance(b, dict):
            _num_slot(f"bore.{_enum_token(b.get('sid') or i)}.radius",
                      b.get("radius"), "mm", "dim", out)

    _domain_slots(state, out)
    return out


def _domain_slots(state: dict, out: list[Slot]) -> None:
    """Sketch / CAM / FEA / ECAD domain slots. Each section is optional;
    the shapes mirror the deterministic describers (narration.md / gsl_tools)."""
    sk = state.get("sketch")
    if isinstance(sk, dict):
        _num_slot("sketch.dof", sk.get("dof"), "count", "count", out)
        for d in sk.get("dims") or []:
            if isinstance(d, dict) and d.get("id") is not None:
                unit = "deg" if d.get("type") == "angle" else "mm"
                _num_slot(f"sketch.dim.{_enum_token(d['id'])}", d.get("value"),
                          unit, "tolerance" if d.get("driven") else "dim", out)

    cam = state.get("cam")
    if isinstance(cam, dict):
        _num_slot("cam.setups", cam.get("setups"), "count", "count", out)
        _num_slot("cam.operations", cam.get("operations"), "count", "count", out)
        for op in cam.get("ops") or []:
            if not isinstance(op, dict):
                continue
            oid = _enum_token(op.get("name") or op.get("id") or "op")
            base = f"cam.op.{oid}"
            if op.get("type"):
                out.append(Slot(f"{base}.type", _enum_token(op["type"]), "none", "enum"))
            _num_slot(f"{base}.tool_diameter", op.get("tool_diameter"), "mm", "dim", out)
            _num_slot(f"{base}.machined", op.get("machined_pct"), "pct", "dim", out)
            _num_slot(f"{base}.cycle_time", op.get("cycle_time_s"), "s", "dim", out)
            _num_slot(f"{base}.feed", op.get("feed_mm_min"), "mm/min", "dim", out)

    fea = state.get("fea")
    if isinstance(fea, dict):
        if fea.get("material"):
            out.append(Slot("fea.material", _enum_token(fea["material"]), "none", "material"))
        _num_slot("fea.yield", fea.get("yield_mpa"), "MPa", "tolerance", out)
        _num_slot("fea.max_von_mises", fea.get("max_von_mises_mpa"), "MPa", "dim", out)
        _num_slot("fea.max_disp", fea.get("max_disp_mm"), "mm", "dim", out)
        _num_slot("fea.min_fos", fea.get("min_fos"), "ratio", "dim", out)
        for i, f in enumerate(fea.get("freqs_hz") or []):
            _num_slot(f"fea.mode.{i + 1}", f, "Hz", "dim", out)

    ecad = state.get("ecad")
    if isinstance(ecad, dict):
        _num_slot("ecad.nets", ecad.get("nets"), "count", "count", out)
        _num_slot("ecad.components", ecad.get("components"), "count", "count", out)
        for tr in ecad.get("traces") or []:
            if isinstance(tr, dict) and tr.get("net"):
                base = f"ecad.trace.{_enum_token(tr['net'])}"
                _num_slot(f"{base}.width", tr.get("width_mm"), "mm", "dim", out)
                _num_slot(f"{base}.impedance", tr.get("impedance_ohm"), "ohm", "tolerance", out)


# --------------------------------------------------------------- state -> text (serialize)


def _degree_ordered_adjacency(state: dict) -> list[str]:
    """Edges as `A-B`, ordered by descending face degree then sid (a stable canonical
    order — degree-ordered adjacency is a documented gsl-ser/v1 requirement)."""
    adj = state.get("adjacency")
    if not isinstance(adj, dict):
        return []
    edges = []
    for e in adj.get("edges") or []:
        pair = e.get("between") if isinstance(e, dict) else None
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            edges.append((str(pair[0]), str(pair[1])))
    if not edges:
        return []
    degree: dict[str, int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    canon_edges = sorted(
        (tuple(sorted((a, b), key=lambda s: (-degree[s], s))) for a, b in edges),
        key=lambda e: (-degree[e[0]], e[0], -degree[e[1]], e[1]))
    return [f"{a}-{b}" for a, b in canon_edges]


def serialize(state: dict, *, domain: Optional[str] = None) -> str:
    """State dict -> canonical gsl-ser/v1 text. Every Slot from `enumerate_slots` appears
    as one `path = token` line, in enumeration order; structural (degree-ordered
    adjacency) follows. This text is the prompt INPUT and round-trips exactly."""
    dom = domain or state.get("domain") or "brep"
    lines = [f"#{SER_VERSION} domain={dom}"]
    for s in enumerate_slots(state):
        lines.append(f"{s.path} = {s.token()}")
    adj = _degree_ordered_adjacency(state)
    if adj:
        lines.append("adj = " + ",".join(adj))
    return "\n".join(lines)


# --------------------------------------------------------------- text -> slots (extract)

# `path = value` where value is `<number><unit>`, `<number>` (unitless), or a bare token.
_LINE_RE = re.compile(r"^(?P<path>[\w][\w.\-]*)\s*=\s*(?P<val>.+?)\s*$")
_NUM_UNIT_RE = re.compile(r"^(?P<num>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)(?P<unit>[a-zA-Z%/°^0-9]*)$")


def _parse_value(raw: str) -> Optional[tuple[Any, str, str]]:
    """(value, unit, kind) from a serialized token, or None if it is not a slot line
    (e.g. the `adj = ...` structural line)."""
    m = _NUM_UNIT_RE.match(raw)
    if not m:
        return (_enum_token(raw), "none", "enum")
    num = float(m.group("num"))
    unit_raw = m.group("unit") or "none"
    base_u, factor = _UNIT_ALIAS.get(unit_raw, (unit_raw, 1.0))
    kind = "count" if base_u == "count" else "dim"
    return (num * factor, base_u, kind)


def extract_slots(text: str) -> list[Slot]:
    """gsl-ser/v1 text -> ordered Slots. The inverse of `serialize` for the canonical
    format; `adj`/`#header` lines are skipped (structural, not slots)."""
    slots: list[Slot] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        path = m.group("path")
        if path == "adj":
            continue
        parsed = _parse_value(m.group("val"))
        if parsed is None:
            continue
        value, unit, kind = parsed
        if unit != "none":
            value = canon(value, unit)
        slots.append(Slot(path, value, unit, kind))
    return slots


# --------------------------------------------------------------- free-text extraction
# For scoring model captions / paraphrases (NOT the strict serialization). Pulls every
# `<number><unit>` occurrence out of prose; the eval harness matches these against the
# ground-truth Slots by value+unit. This is the "extraction-precision over free text"
# secondary channel the master plan flags as lossy — the slot harness treats a low
# recall here as a real miss, never papers over it.
_PROSE_NUM_RE = re.compile(
    # A numeral must not be embedded in an identifier: reject a leading letter/digit/
    # underscore/dot (so `pcb_7870`, `1018_steel`, `f7`, `v1.2` do NOT leak digits).
    r"(?<![A-Za-z0-9_.])(?P<num>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    # then EITHER a unit (which must end at a word boundary — else `s` eats the 's' of
    # "setup", `m` the 'm' of "machined") OR a non-identifier boundary (bare number).
    r"(?:\s*(?P<unit>mm/min|mm3|mm\^3|MPa|N/mm2|mm|cm|deg|degrees|°|Hz|sec|s|%|pct|percent|"
    r"ohms|ohm|Ω|count|m)(?![A-Za-z])|(?![_A-Za-z0-9]))",
    re.IGNORECASE)


def extract_numerals(text: str) -> list[tuple[float, str]]:
    """Every (value, canonical_unit) numeral in free prose. Unit-less numbers get unit
    'none'. Used by the eval harness and the groundedness seam for caption scoring."""
    out: list[tuple[float, str]] = []
    for m in _PROSE_NUM_RE.finditer(text):
        num = float(m.group("num"))
        unit_raw = (m.group("unit") or "none").lower()
        base_u, factor = _UNIT_ALIAS.get(unit_raw, _UNIT_ALIAS.get(m.group("unit") or "none",
                                                                   ("none", 1.0)))
        out.append((num * factor, base_u))
    return out


# --------------------------------------------------------------- round-trip verification


def slots_equal(a: Iterable[Slot], b: Iterable[Slot]) -> bool:
    """Exact ordered equality of two canonical Slot lists over the SERIALIZED content —
    (path, unit, value). `kind` is a semantic annotation (dim vs tolerance, material vs
    enum) that is deliberately NOT written to the text and cannot be recovered from
    "250MPa" or "6061_T6" alone, so it is not part of the round-trip guarantee; the
    extractor infers a best-effort kind for downstream use only. This is the
    round-trip check and matches `roundtrip_diff` (which compares tokens)."""
    la, lb = list(a), list(b)
    if len(la) != len(lb):
        return False
    for x, y in zip(la, lb):
        if x.path != y.path or x.unit != y.unit:
            return False
        if x.numeric and y.numeric:
            if abs(float(x.value) - float(y.value)) > 1e-9:
                return False
        elif x.numeric != y.numeric or _enum_token(x.value) != _enum_token(y.value):
            return False
    return True


def roundtrip_diff(state: dict) -> list[str]:
    """[] iff `extract_slots(serialize(state))` reproduces `enumerate_slots(state)`;
    otherwise a human-readable list of the first mismatches (for the eval report)."""
    truth = enumerate_slots(state)
    back = extract_slots(serialize(state))
    diffs: list[str] = []
    ti = {s.path: s for s in truth}
    bi = {s.path: s for s in back}
    for p in sorted(set(ti) | set(bi)):
        t, b = ti.get(p), bi.get(p)
        if t is None:
            diffs.append(f"+{p} (extracted, not in truth) = {b.token()}")
        elif b is None:
            diffs.append(f"-{p} (truth, not re-extracted) = {t.token()}")
        elif t.token() != b.token():
            diffs.append(f"~{p}: truth={t.token()} extracted={b.token()}")
    return diffs


# --------------------------------------------------------------- deterministic caption
# The "raw GSL template" the eval harness scores as its baseline: prose that mentions
# every Slot's canonical token. By construction it contains all and only grounded
# numerals, so it must score ~100% slot fidelity — that is what validates the harness.


def render_caption(state: dict) -> str:
    """A deterministic prose caption naming every ground-truth Slot (the GSL template /
    training target). Numerals are the canonical tokens, so the caption is fully
    grounded by construction."""
    slots = enumerate_slots(state)
    dom = state.get("domain") or "brep"
    parts = [f"[{SER_VERSION} domain={dom}]"]
    for s in slots:
        # Keep the dotted path as the label: a numeric path index (e.g. `fea.mode.1`)
        # stays attached to its dot, so the identifier-guard never mistakes the ordinal
        # `1` for a slot VALUE. Word-separators inside a segment stay readable.
        parts.append(f"{s.path} is {prose_token(s.value, s.unit)}.")
    return " ".join(parts)


__all__ = [
    "SER_VERSION", "Slot", "canon", "fmt_value",
    "enumerate_slots", "serialize", "extract_slots", "extract_numerals",
    "slots_equal", "roundtrip_diff", "render_caption",
]
