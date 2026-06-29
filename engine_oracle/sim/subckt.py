"""Subcircuit (``.subckt``) parsing + X-instance expansion for the SPICE front-end.

A subcircuit is a named block of primitive elements with a list of *port* nodes::

    .subckt NAME p1 p2 ... [param=val ...]
        <element cards using port nodes + internal nodes>
    .ends [NAME]

An instance card::

    X<name> n1 n2 ... NAME [param=val ...]

is expanded by *inlining* the subckt body: every body element is emitted as a real Device with its
nodes remapped — ports map to the instance's connection nodes, and every other (internal) node is
*uniquified* by prefixing the instance name so two instances never collide. Element names are
likewise prefixed (``X1.R1``). Nested ``X`` instances are expanded recursively.

This module reuses the element parser in ``engine.sim.spice`` (passed in as a callback to avoid an
import cycle) so the Circuit-dict Device shape (sim.md §1) is produced byte-for-byte the same way as
for top-level cards.

Built-in library
----------------
:func:`builtin_subckts` provides ready-to-use behavioural models so callers (and the weak AI) get
useful parts without authoring SPICE:

* ``OPAMP`` — single-pole op-amp macromodel (high-gain VCVS + RC dominant pole + output buffer).
  Ports ``in+ in- out`` (3-pin); the 5-pin KiCad order ``in+ in- v+ v- out`` is also accepted by the
  netlist bridge, which drops the two supply pins (the behavioural model is rail-less).
* ``NE555`` — behavioural 555 timer. Ports ``GND TRIG OUT RST CV THR DIS VCC`` (datasheet order).

See :data:`SUBCKT_FIDELITY` for the documented modelling fidelity of each, and :data:`BUILTIN_MODELS`
for the ``.model`` cards the built-ins rely on.
"""
from __future__ import annotations

import re
from typing import Callable


class SubcktError(Exception):
    """Raised when a ``.subckt`` definition or ``X`` instance cannot be expanded."""


# ---------------------------------------------------------------------------
# Subckt definition container
# ---------------------------------------------------------------------------

class SubcktDef:
    """A parsed ``.subckt`` body: name, ordered port list, default params, raw element lines."""

    __slots__ = ("name", "ports", "params", "lines")

    def __init__(self, name: str, ports: list[str], params: dict, lines: list[str]) -> None:
        self.name = name
        self.ports = ports
        self.params = params
        self.lines = lines


def _parse_subckt_header(tokens: list[str]) -> tuple[str, list[str], dict]:
    """Parse the tokens following ``.subckt`` → (name, ports, default_params)."""
    if not tokens:
        raise SubcktError(".subckt with no name")
    name = tokens[0]
    ports: list[str] = []
    params: dict[str, str] = {}
    for tok in tokens[1:]:
        m = re.match(r"^(\w+)=(.+)$", tok)
        if m:
            params[m.group(1).lower()] = m.group(2)
        else:
            ports.append(tok)
    return name, ports, params


def collect_subckts(lines: list[str]) -> tuple[dict[str, SubcktDef], list[str]]:
    """Split preprocessed deck ``lines`` into (subckt defs, remaining top-level lines).

    ``lines`` must already be comment-stripped + continuation-joined (see ``spice._preprocess``).
    """
    subckts: dict[str, SubcktDef] = {}
    top: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        toks = line.split()
        if toks and toks[0].lower() == ".subckt":
            name, ports, params = _parse_subckt_header(toks[1:])
            body: list[str] = []
            i += 1
            depth = 1
            while i < n:
                inner = lines[i]
                itoks = inner.split()
                if itoks:
                    head = itoks[0].lower()
                    if head == ".subckt":
                        depth += 1
                    elif head == ".ends":
                        depth -= 1
                        if depth == 0:
                            break
                body.append(inner)
                i += 1
            subckts[name.upper()] = SubcktDef(name, ports, params, body)
        else:
            top.append(line)
        i += 1
    return subckts, top


# ---------------------------------------------------------------------------
# Instance expansion
# ---------------------------------------------------------------------------

def _parse_instance(tokens: list[str]) -> tuple[str, list[str], str, dict]:
    """Parse an ``X`` instance card → (inst_name, conn_nodes, subckt_name, params).

    Grammar:  Xname n1 n2 ... SUBCKTNAME [k=v ...]
    The subckt name is the *last* bare (non ``k=v``) token; preceding bare tokens are connection
    nodes.
    """
    inst = tokens[0]
    bare: list[int] = []
    params: dict[str, str] = {}
    for idx in range(1, len(tokens)):
        tok = tokens[idx]
        m = re.match(r"^(\w+)=(.+)$", tok)
        if m:
            params[m.group(1).lower()] = m.group(2)
        else:
            bare.append(idx)
    if not bare:
        raise SubcktError(f"{inst}: X instance has no subckt name")
    sub_idx = bare[-1]
    subname = tokens[sub_idx]
    conn = [tokens[j] for j in bare[:-1]]
    return inst, conn, subname, params


def _gnd_or(name: str) -> str:
    return "0" if name.lower() in ("0", "gnd") else name


def _remap_node(node: str, port_map: dict[str, str], inst: str,
                local: dict[str, str]) -> str:
    """Map a body node name to its expanded name.

    Ports → the instance's connection node. Ground stays ground. Anything else becomes an internal
    node uniquified as ``<inst>.<node>`` (cached in ``local`` for stable reuse within one instance).
    """
    if node in port_map:
        return port_map[node]
    if node.lower() in ("0", "gnd"):
        return "0"
    if node not in local:
        local[node] = f"{inst}.{node}"
    return local[node]


def expand_instances(
    top_lines: list[str],
    subckts: dict[str, SubcktDef],
    parse_element: Callable[[list[str]], dict | None],
) -> tuple[list[dict], list[str]]:
    """Expand every ``X`` instance in ``top_lines`` into a flat list of Device dicts.

    Returns ``(devices, warnings)``. ``parse_element`` is ``spice._parse_element`` (injected to
    avoid an import cycle). Non-element / non-instance control lines are ignored here — the caller
    handles ``.model`` / analysis cards separately.
    """
    devices: list[dict] = []
    warnings: list[str] = []
    for line in top_lines:
        toks = line.split()
        if not toks:
            continue
        if toks[0].startswith(".") or toks[0].startswith("*"):
            continue
        if toks[0][0].upper() == "X":
            inst, conn, subname, _params = _parse_instance(toks)
            sub = subckts.get(subname.upper())
            if sub is None:
                warnings.append(f"{inst}: subckt '{subname}' not defined — skipped")
                continue
            if len(conn) != len(sub.ports):
                warnings.append(
                    f"{inst}: {len(conn)} nodes but subckt '{sub.name}' has "
                    f"{len(sub.ports)} ports — skipped"
                )
                continue
            port_map = {p: _gnd_or(conn[k]) for k, p in enumerate(sub.ports)}
            local: dict[str, str] = {}
            sub_devs, sub_warn = _expand_body(
                sub, inst, port_map, local, subckts, parse_element, 0
            )
            devices.extend(sub_devs)
            warnings.extend(sub_warn)
    return devices, warnings


def _expand_body(
    sub: SubcktDef,
    inst: str,
    port_map: dict[str, str],
    local: dict[str, str],
    subckts: dict[str, SubcktDef],
    parse_element: Callable[[list[str]], dict | None],
    depth: int,
) -> tuple[list[dict], list[str]]:
    """Inline one subckt body for instance ``inst`` with the given port mapping (recursive)."""
    if depth > 50:
        raise SubcktError("subckt expansion exceeded depth 50 (recursive instance?)")
    devices: list[dict] = []
    warnings: list[str] = []
    for line in sub.lines:
        toks = line.split()
        if not toks:
            continue
        head = toks[0]
        if head.startswith("*"):
            continue
        if head.startswith("."):
            # .model / .param inside a subckt are handled out-of-band (BUILTIN_MODELS / caller).
            continue
        if head[0].upper() == "X":
            # Nested instance: remap its connection nodes, then recurse.
            ninst, nconn, nsubname, _p = _parse_instance(toks)
            child = subckts.get(nsubname.upper())
            if child is None:
                warnings.append(f"{inst}.{ninst}: subckt '{nsubname}' not defined — skipped")
                continue
            if len(nconn) != len(child.ports):
                warnings.append(
                    f"{inst}.{ninst}: {len(nconn)} nodes vs {len(child.ports)} ports — skipped"
                )
                continue
            mapped_conn = [_remap_node(c, port_map, inst, local) for c in nconn]
            child_port_map = {p: mapped_conn[k] for k, p in enumerate(child.ports)}
            cdev, cwarn = _expand_body(
                child, f"{inst}.{ninst}", child_port_map, {},
                subckts, parse_element, depth + 1,
            )
            devices.extend(cdev)
            warnings.extend(cwarn)
            continue
        # Primitive element: rename + remap nodes, then parse via the shared element parser.
        remapped = _remap_element_line(toks, port_map, inst, local)
        if remapped is None:
            warnings.append(f"{inst}: could not remap '{line}'")
            continue
        dev = parse_element(remapped)
        if dev is not None:
            devices.append(dev)
        else:
            warnings.append(f"{inst}: unparseable element '{line}'")
    return devices, warnings


# Node-count by element letter so we know which tokens are nodes vs. value/model/params.
# (Matches engine.sim.spice._parse_element expectations.)
_NODE_COUNT = {
    "R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "D": 2,
    "Q": 3, "M": 4, "E": 4, "G": 4, "F": 2, "H": 2, "S": 4,
}


def _remap_element_line(
    toks: list[str], port_map: dict[str, str], inst: str, local: dict[str, str]
) -> list[str] | None:
    """Return a new token list with the element name prefixed + node tokens remapped.

    F/H control a *V-source by name*; that name is also instance-local, so it is prefixed too.
    """
    name = toks[0]
    letter = name[0].upper()
    nc = _NODE_COUNT.get(letter)
    if nc is None:
        return None
    # Keep the kind letter first (the element parser dispatches on name[0]); append the instance
    # path as a suffix so two instances of the same subckt never collide: e.g. "Rin:X1".
    new_name = f"{name}:{inst}"
    nodes = [_remap_node(t, port_map, inst, local) for t in toks[1 : 1 + nc]]
    rest = list(toks[1 + nc :])
    if letter in ("F", "H") and rest:
        rest[0] = f"{rest[0]}:{inst}"
    return [new_name, *nodes, *rest]


# ---------------------------------------------------------------------------
# Built-in subckt library (plain SPICE-deck fragments, expanded through the same path)
# ---------------------------------------------------------------------------

# Single-pole op-amp macromodel. Ports: in+ in- out.
#   Egain: high-gain VCVS senses (in+ - in-) -> internal node ope (A0 = 200k)
#   Rp/Cp: dominant pole. fp = 1/(2*pi*Rp*Cp) ~ 10 Hz; GBW = A0*fp ~ 2 MHz.
#   Eout/Rout: unity buffer to OUT with a 10 Ohm output resistance.
_OPAMP_SUBCKT = """\
.subckt OPAMP inp inn out
Rin inp inn 1e12
Egain ope 0 inp inn 200000
Rp ope opi 1k
Cp opi 0 15.915u
Eout outb 0 opi 0 1
Rout outb out 10
.ends OPAMP
"""

# Behavioural NE555. Ports (datasheet order): GND TRIG OUT RST CV THR DIS VCC.
#
# Realised as a single inverting Schmitt-trigger latch with an OUTPUT-DEPENDENT threshold — the
# classic way to fold the two 555 comparators + SR latch into one numerically robust block:
#
#   vth = (Vcc + q)/3        (node `vth`, built by two VCCS into a 1k load)
#         -> 1/3 Vcc when the output state q is LOW, 2/3 Vcc when q is HIGH.
#   Ecmp/q : an inverting comparator drives the latch state node `q` HIGH when V(THR) < vth.
#            Because vth tracks q, this is a hysteretic (Schmitt) latch:
#              * output LOW  (vth = 1/3 Vcc): q flips HIGH once THR drops below 1/3 Vcc   (TRIG)
#              * output HIGH (vth = 2/3 Vcc): q flips LOW  once THR rises above 2/3 Vcc   (THR)
#            The 1/3..2/3 dead-zone is the latch memory. `q` is diode-clamped to [0, Vcc] and has a
#            47 nF node cap that slew-limits the regenerative snap so the trapezoidal integrator
#            stays stable (this cap is internal-only; the external timing R-C sets the period).
#   OUT buffers `q` to the rails through a 10 Ohm Rout.
#   DIS is the open-collector discharge pin: a rail-clamped `qlow` = clamp(Vcc - q) logic node
#   gates a VCCS that ONLY sinks current from DIS (never sources), so when the output is LOW the
#   external timing cap discharges through DIS and when the output is HIGH DIS floats.
#
# THR and TRIG are tied together by the external astable network (both sense the timing cap); the
# CV, RST, GND and VCC ports are present for pin-compatibility (CV/RST are not modelled as hard
# overrides — see SUBCKT_FIDELITY['NE555']).
_NE555_SUBCKT = """\
.subckt NE555 gnd trig out rst cv thr dis vcc
Gva 0 vth vcc 0 0.3333m
Gvq 0 vth qn 0 0.3333m
Rvth vth 0 1k
Ecmp cmpr 0 vth thr 200
Rcmp cmpr q 10k
Dqh q vcc Dclamp
Dql 0 q Dclamp
Rq q 0 100k
Cq q 0 47n
Eqn qn 0 q 0 1
Eout outraw 0 q 0 1
Rout outraw out 10
Eqlowr qlowr 0 vcc q 1
Rqlow qlowr qlow 10k
Dqlowh qlow vcc Dclamp
Dqlowl 0 qlow Dclamp
Rqlowl qlow 0 100k
Gdis dis 0 qlow 0 0.3m
Rdis dis 0 1e9
Ddisl 0 dis Dclamp
.ends NE555
"""


# ``.model`` cards the built-in subckts reference (the expander drops .model lines inside bodies).
# Dclamp is a fairly stiff diode (low N, modest RS) used to clamp high-gain internal nodes to the
# rails so the regenerative latch cannot run away numerically.
BUILTIN_MODELS: dict[str, dict] = {
    "Dclamp": {"type": "D", "IS": 1e-9, "N": 1.0, "RS": 10.0},
}


# Documented modelling fidelity (surfaced in docs / test prints / code review).
SUBCKT_FIDELITY: dict[str, str] = {
    "OPAMP": (
        "Single-pole op-amp macromodel: ideal differential input (1 TOhm), DC gain 200k, one "
        "dominant pole ~10 Hz (GBW ~2 MHz) via an internal RC, unity output buffer with 10 Ohm "
        "Rout. No slew-rate limit, no input offset/bias, no output rail clamp, no noise. Accurate "
        "for closed-loop gain/bandwidth where loop gain >> 1 (matches the ideal op-amp gate: "
        "-Rf/Rin)."
    ),
    "NE555": (
        "Behavioural 555 timer (NOT transistor-level). Modelled as a single inverting "
        "Schmitt-trigger latch whose threshold tracks the output state: vth = (Vcc + q)/3 gives "
        "1/3 Vcc when the output is low and 2/3 Vcc when high, so the timing cap on THR/TRIG "
        "charges (output high) and discharges (output low, via the DIS sink) between 1/3 and 2/3 "
        "Vcc — i.e. correct astable oscillation with a near-rail-to-rail output swing. The latch "
        "state node is diode-clamped to the rails with a slew-limiting node cap for numerical "
        "stability. Does NOT model: exact datasheet timing constants/duty cycle, output "
        "source/sink current limits, RST and CV as hard overrides, internal divider loading, or "
        "temperature drift. Use for functional / oscillation checks, not timing-accurate design."
    ),
}


_BUILTIN_CACHE: dict[str, SubcktDef] | None = None


def builtin_subckts() -> dict[str, SubcktDef]:
    """Return the built-in :class:`SubcktDef` objects (keyed by UPPER name).

    Parsed once and cached; callers should not mutate the returned defs.
    """
    global _BUILTIN_CACHE
    if _BUILTIN_CACHE is None:
        from engine.sim.spice import _preprocess  # local import: spice imports subckt

        lib: dict[str, SubcktDef] = {}
        for frag in (_OPAMP_SUBCKT, _NE555_SUBCKT):
            defs, _top = collect_subckts(_preprocess(frag))
            lib.update(defs)
        _BUILTIN_CACHE = lib
    return dict(_BUILTIN_CACHE)
