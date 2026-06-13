"""SPICE netlist import/export + schematic-netlist→Circuit bridge.

Public API
----------
parse_spice(text) -> Circuit
write_spice(circuit, study=None) -> str
parse_spice_deck(text) -> (Circuit, study | None)
netlist_to_spice_circuit(netlist, sim_params=None) -> (Circuit, list[str])

Circuit shape is FROZEN per docs/contracts/ecad/sim.md §1:
    {
        "nodes":   list[str],
        "devices": list[Device],
        "models":  {name: {**params}},
        "params":  {"TEMP": 27, ...},
    }
    Device = {"kind": str, "name": str, "nodes": list[str],
              "value": float|None, "model": str|None, "params": dict}
"""

from __future__ import annotations

import re
import warnings
from typing import Any

# ---------------------------------------------------------------------------
# Value-suffix table  (SPICE canonical, case-insensitive where unambiguous)
# ---------------------------------------------------------------------------
_SUFFIX: dict[str, float] = {
    "meg": 1e6,
    "g":   1e9,
    "k":   1e3,
    "m":   1e-3,
    "u":   1e-6,
    "n":   1e-9,
    "p":   1e-12,
    "f":   1e-15,
    "mil": 25.4e-6,   # 1 mil = 25.4 µm (mechanical SPICE)
}

_SUFFIX_RE = re.compile(
    r"^([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)"  # numeric part
    r"(meg|mil|[gkmunpf])?$",                          # optional suffix
    re.IGNORECASE,
)


def _parse_value(token: str) -> float:
    """Parse a SPICE value token to float.  Raises ValueError on bad input."""
    m = _SUFFIX_RE.match(token.strip())
    if not m:
        raise ValueError(f"Cannot parse SPICE value: {token!r}")
    num = float(m.group(1))
    suf = (m.group(2) or "").lower()
    return num * _SUFFIX.get(suf, 1.0)


def _fmt_value(v: float) -> str:
    """Format a float back to a compact SPICE token (best suffix)."""
    if v == 0.0:
        return "0"
    abs_v = abs(v)
    best_suf = ""
    best_num = v
    for suf, scale in sorted(_SUFFIX.items(), key=lambda x: -x[1]):
        if suf == "mil":
            continue  # mil is mechanical; skip on output
        q = v / scale
        if abs(q) >= 0.1 and abs(q) < 1000.0:
            best_suf = suf
            best_num = q
            break
    if best_suf == "":
        return f"{v:g}"
    return f"{best_num:g}{best_suf}"


# ---------------------------------------------------------------------------
# Ground-node canonicalisation
# ---------------------------------------------------------------------------

def _gnd(name: str) -> str:
    """Return "0" for any ground alias, else the original name."""
    if name.lower() in ("0", "gnd"):
        return "0"
    return name


# ---------------------------------------------------------------------------
# Deck pre-processing: strip comments, join continuations, split into lines
# ---------------------------------------------------------------------------

def _preprocess(text: str) -> list[str]:
    """Return a list of logical SPICE lines (comments stripped, continuations joined)."""
    physical = text.splitlines()
    logical: list[str] = []
    current: str | None = None

    for raw in physical:
        # strip inline ; comment (but not inside strings — good enough for SPICE)
        line = raw.split(";")[0].rstrip()

        # full-line comment
        if line.lstrip().startswith("*"):
            continue

        if line.startswith("+"):
            # continuation
            if current is None:
                current = ""
            current = current + " " + line[1:].strip()
        else:
            if current is not None:
                logical.append(current)
            current = line.strip()

    if current is not None and current:
        logical.append(current)

    return [l for l in logical if l]


# ---------------------------------------------------------------------------
# Waveform / source-type param parsing  (V/I PULSE, SIN, PWL, AC, DC)
# ---------------------------------------------------------------------------

def _parse_source_params(tokens: list[str]) -> dict:
    """Parse the remaining tokens of a V/I element card into params dict."""
    params: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i].upper()
        if tok.startswith("PULSE(") or tok == "PULSE":
            # collect everything inside PULSE(...)
            full = " ".join(tokens[i:])
            inner = re.search(r"PULSE\(([^)]*)\)", full, re.IGNORECASE)
            if inner:
                vals = inner.group(1).split()
                keys = ["v1", "v2", "td", "tr", "tf", "pw", "per"]
                params["waveform"] = {"type": "PULSE",
                                      **{k: _safe_val(v) for k, v in zip(keys, vals)}}
            # skip to after closing paren
            combined = " ".join(tokens[i:])
            skip = 1
            for j in range(i, len(tokens)):
                if ")" in tokens[j]:
                    skip = j - i + 1
                    break
            i += skip
        elif tok.startswith("SIN(") or tok == "SIN":
            full = " ".join(tokens[i:])
            inner = re.search(r"SIN\(([^)]*)\)", full, re.IGNORECASE)
            if inner:
                vals = inner.group(1).split()
                keys = ["vo", "va", "f", "td", "theta"]
                params["waveform"] = {"type": "SIN",
                                      **{k: _safe_val(v) for k, v in zip(keys, vals)}}
            skip = 1
            for j in range(i, len(tokens)):
                if ")" in tokens[j]:
                    skip = j - i + 1
                    break
            i += skip
        elif tok.startswith("PWL(") or tok == "PWL":
            full = " ".join(tokens[i:])
            inner = re.search(r"PWL\(([^)]*)\)", full, re.IGNORECASE)
            if inner:
                nums = inner.group(1).split()
                pairs = []
                for k in range(0, len(nums) - 1, 2):
                    pairs.append((_safe_val(nums[k]), _safe_val(nums[k + 1])))
                params["waveform"] = {"type": "PWL", "points": pairs}
            skip = 1
            for j in range(i, len(tokens)):
                if ")" in tokens[j]:
                    skip = j - i + 1
                    break
            i += skip
        elif tok == "AC":
            params["ac"] = {"mag": _safe_val(tokens[i + 1]) if i + 1 < len(tokens) else 1.0,
                            "phase": _safe_val(tokens[i + 2]) if i + 2 < len(tokens) else 0.0}
            i += 3
        elif tok == "DC":
            params["dc"] = _safe_val(tokens[i + 1]) if i + 1 < len(tokens) else 0.0
            i += 2
        else:
            # bare DC value
            try:
                params["dc"] = _parse_value(tokens[i])
            except ValueError:
                pass
            i += 1
    return params


def _safe_val(s: str) -> float:
    try:
        return _parse_value(s)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# .model card parsing
# ---------------------------------------------------------------------------

_MODEL_PARAM_RE = re.compile(r"(\w+)\s*=\s*([^\s,)]+)")


def _parse_model_card(tokens: list[str]) -> tuple[str, str, dict]:
    """Parse ['modelname', 'TYPE(params)' | 'TYPE', 'param=val', ...].

    Returns (name, type_str, params_dict).
    """
    name = tokens[0]
    rest = " ".join(tokens[1:])
    # match  TYPE(key=val ...)  or just  TYPE
    m = re.match(r"(\w+)\s*\(?(.*?)\)?$", rest, re.DOTALL)
    if not m:
        return name, "", {}
    type_str = m.group(1).upper()
    inner = m.group(2)
    params = {k.upper(): _safe_val(v) for k, v in _MODEL_PARAM_RE.findall(inner)}
    return name, type_str, params


# ---------------------------------------------------------------------------
# Analysis / study card parsing
# ---------------------------------------------------------------------------

def _parse_analysis(keyword: str, tokens: list[str]) -> dict:
    """Parse .tran/.dc/.ac/.op tokens into a study analysis dict."""
    kw = keyword.upper()
    if kw == ".OP":
        return {"type": "op"}
    if kw == ".TRAN":
        d: dict = {"type": "tran"}
        if len(tokens) >= 1:
            d["tstep"] = _safe_val(tokens[0])
        if len(tokens) >= 2:
            d["tstop"] = _safe_val(tokens[1])
        if len(tokens) >= 3:
            d["tstart"] = _safe_val(tokens[2])
        d["uic"] = any(t.upper() == "UIC" for t in tokens)
        return d
    if kw == ".DC":
        # .DC Vsource start stop step
        d = {"type": "dc"}
        if len(tokens) >= 1:
            d["source"] = tokens[0]
        if len(tokens) >= 2:
            d["start"] = _safe_val(tokens[1])
        if len(tokens) >= 3:
            d["stop"] = _safe_val(tokens[2])
        if len(tokens) >= 4:
            d["step"] = _safe_val(tokens[3])
        return d
    if kw == ".AC":
        d = {"type": "ac"}
        if len(tokens) >= 1:
            d["sweep"] = tokens[0].lower()   # dec/lin/oct
        if len(tokens) >= 2:
            d["points"] = int(_safe_val(tokens[1]))
        if len(tokens) >= 3:
            d["fstart"] = _safe_val(tokens[2])
        if len(tokens) >= 4:
            d["fstop"] = _safe_val(tokens[3])
        return d
    return {"type": kw.lstrip(".")}


# ---------------------------------------------------------------------------
# Element-card dispatcher
# ---------------------------------------------------------------------------

def _parse_element(tokens: list[str]) -> dict | None:
    """Parse a single SPICE element card.  Returns a Device dict or None."""
    name = tokens[0]
    kind_letter = name[0].upper()

    # Map letter → (kind, expected_node_count)
    # M=4 (G D S B), Q=3 (C B E), D=2 (A K), R/C/L/V/I/E/F/G/H=2 nodes
    if kind_letter in ("R", "C", "L"):
        # Rname n+ n-  value  [params]
        if len(tokens) < 4:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2])]
        value: float | None = None
        params: dict = {}
        try:
            value = _parse_value(tokens[3])
        except ValueError:
            pass
        # extra params (e.g. ic= for C/L)
        for tok in tokens[4:]:
            m = re.match(r"(\w+)=(.+)", tok, re.IGNORECASE)
            if m:
                try:
                    params[m.group(1).lower()] = _parse_value(m.group(2))
                except ValueError:
                    params[m.group(1).lower()] = m.group(2)
        return _dev(kind_letter, name, nodes, value, None, params)

    if kind_letter in ("V", "I"):
        if len(tokens) < 3:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2])]
        rest_tokens = tokens[3:]
        # try bare numeric first
        value = None
        if rest_tokens and not any(rest_tokens[0].upper().startswith(k)
                                   for k in ("PULSE", "SIN", "PWL", "AC", "DC")):
            try:
                value = _parse_value(rest_tokens[0])
                rest_tokens = rest_tokens[1:]
            except ValueError:
                pass
        src_params = _parse_source_params(rest_tokens)
        # canonical DC value
        if value is None and "DC" in src_params:
            value = src_params.pop("DC")
        return _dev(kind_letter, name, nodes, value, None, src_params)

    if kind_letter == "D":
        # Dname A K [model]
        if len(tokens) < 3:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2])]
        model = tokens[3] if len(tokens) > 3 else None
        params = {}
        for tok in tokens[4:]:
            m = re.match(r"(\w+)=(.+)", tok)
            if m:
                try:
                    params[m.group(1).upper()] = _parse_value(m.group(2))
                except ValueError:
                    params[m.group(1).upper()] = m.group(2)
        return _dev("D", name, nodes, None, model, params)

    if kind_letter == "Q":
        # Qname C B E [model]
        if len(tokens) < 4:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2]), _gnd(tokens[3])]
        model = tokens[4] if len(tokens) > 4 else None
        return _dev("Q", name, nodes, None, model, {})

    if kind_letter == "M":
        # Mname D G S B  model  [params]
        if len(tokens) < 6:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2]),
                 _gnd(tokens[3]), _gnd(tokens[4])]
        model = tokens[5] if len(tokens) > 5 else None
        params = {}
        for tok in tokens[6:]:
            m = re.match(r"(\w+)=(.+)", tok)
            if m:
                try:
                    params[m.group(1).upper()] = _parse_value(m.group(2))
                except ValueError:
                    params[m.group(1).upper()] = m.group(2)
        return _dev("M", name, nodes, None, model, params)

    if kind_letter in ("E", "G"):
        # E/G  name  n+ n-  nc+ nc-  gain
        if len(tokens) < 6:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2]),
                 _gnd(tokens[3]), _gnd(tokens[4])]
        gain = _safe_val(tokens[5]) if len(tokens) > 5 else 1.0
        return _dev(kind_letter, name, nodes, None, None, {"gain": gain})

    if kind_letter in ("F", "H"):
        # F/H  name  n+ n-  Vcontrol  gain
        if len(tokens) < 5:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2])]
        ctrl = tokens[3]
        gain = _safe_val(tokens[4]) if len(tokens) > 4 else 1.0
        return _dev(kind_letter, name, nodes, None, None,
                    {"gain": gain, "ctrl": ctrl})

    # Unknown element letter → silently skip
    return None


def _dev(kind: str, name: str, nodes: list[str],
         value: float | None, model: str | None, params: dict) -> dict:
    return {
        "kind":   kind,
        "name":   name,
        "nodes":  nodes,
        "value":  value,
        "model":  model,
        "params": params,
    }


# ---------------------------------------------------------------------------
# _make_circuit  — assemble a proper Circuit dict from collected pieces
# ---------------------------------------------------------------------------

def _make_circuit(devices: list[dict], models: dict[str, dict],
                  global_params: dict) -> dict:
    """Build the frozen Circuit dict from parsed components."""
    node_set: list[str] = []
    seen: set[str] = set()
    # Ground must come first
    if "0" not in seen:
        node_set.append("0")
        seen.add("0")
    for dev in devices:
        for n in dev["nodes"]:
            if n not in seen:
                node_set.append(n)
                seen.add(n)
    return {
        "nodes":   node_set,
        "devices": devices,
        "models":  models,
        "params":  global_params,
    }


# ---------------------------------------------------------------------------
# PUBLIC: parse_spice_deck
# ---------------------------------------------------------------------------

def parse_spice_deck(text: str) -> tuple[dict, dict | None]:
    """Parse a full SPICE deck.

    Returns (Circuit, study) where study follows sim.md §3 shape or None.
    Circuit follows sim.md §1 shape exactly.

    ``.subckt`` definitions are collected and ``X`` instances are inlined (node-renamed) via
    :mod:`engine.sim.subckt`; the built-in subckt library (op-amp, NE555) is always available.
    """
    from engine.sim import subckt as _sub  # local import: subckt imports spice helpers

    raw_lines = _preprocess(text)

    # Pull out .subckt blocks; the remainder are top-level lines (incl. X instances).
    deck_subckts, lines = _sub.collect_subckts(raw_lines)
    subckts = _sub.builtin_subckts()
    subckts.update(deck_subckts)  # deck definitions override built-ins of the same name

    devices: list[dict] = []
    models:  dict[str, dict] = {}
    global_params: dict = {"TEMP": 27}
    analyses: list[dict] = []
    probes:   list[str]  = []
    options:  dict = {}
    x_lines:  list[str]  = []

    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        first = tokens[0].upper()

        # ── control cards ──────────────────────────────────────────────────
        if first == ".TITLE":
            continue  # ignore title

        if first in (".TRAN", ".DC", ".AC", ".OP"):
            analyses.append(_parse_analysis(first, tokens[1:]))
            continue

        if first == ".MODEL":
            if len(tokens) >= 3:
                name, type_str, params = _parse_model_card(tokens[1:])
                models[name] = {"type": type_str, **params}
            continue

        if first == ".PARAM":
            for tok in tokens[1:]:
                m = re.match(r"(\w+)=(.+)", tok)
                if m:
                    try:
                        global_params[m.group(1).upper()] = _parse_value(m.group(2))
                    except ValueError:
                        global_params[m.group(1).upper()] = m.group(2)
            continue

        if first == ".OPTIONS":
            for tok in tokens[1:]:
                m = re.match(r"(\w+)=(.+)", tok)
                if m:
                    key = m.group(1).lower()
                    try:
                        options[key] = _parse_value(m.group(2))
                    except ValueError:
                        options[key] = m.group(2)
            if "temp" in options:
                global_params["TEMP"] = options["temp"]
            continue

        if first == ".PROBE" or first == ".PRINT" or first == ".MEASURE":
            # collect probe names
            probes.extend(tokens[1:])
            continue

        if first in (".END", ".ENDS", ".SUBCKT"):
            continue  # simplified: skip subckt definitions

        if first.startswith("."):
            continue  # unknown control card — skip

        # ── X subcircuit instances (inlined after the loop) ────────────────
        if tokens[0][0].upper() == "X":
            x_lines.append(line)
            continue

        # ── element cards ──────────────────────────────────────────────────
        dev = _parse_element(tokens)
        if dev is not None:
            devices.append(dev)

    # Inline all X instances (built-in + deck subckts); merge any models the built-ins need.
    if x_lines:
        sub_devs, _warn = _sub.expand_instances(x_lines, subckts, _parse_element)
        devices.extend(sub_devs)
        for mname, mparams in _sub.BUILTIN_MODELS.items():
            models.setdefault(mname, dict(mparams))

    circuit = _make_circuit(devices, models, global_params)

    study: dict | None = None
    if analyses:
        study = {"analyses": analyses}
        if probes:
            study["probes"] = probes
        if options:
            study["options"] = options

    return circuit, study


def parse_spice(text: str) -> dict:
    """Parse a SPICE deck and return the Circuit dict (study is discarded)."""
    circuit, _ = parse_spice_deck(text)
    return circuit


# ---------------------------------------------------------------------------
# PUBLIC: write_spice
# ---------------------------------------------------------------------------

def write_spice(circuit: dict, study: dict | None = None) -> str:
    """Serialize a Circuit (and optional study) to a SPICE deck string."""
    lines: list[str] = ["* Generated by engine.sim.spice", ""]

    # .model cards
    for mname, mparams in circuit.get("models", {}).items():
        mtype = mparams.get("type", "")
        others = {k: v for k, v in mparams.items() if k != "type"}
        param_str = " ".join(f"{k}={v:g}" for k, v in others.items())
        if param_str:
            lines.append(f".model {mname} {mtype}({param_str})")
        else:
            lines.append(f".model {mname} {mtype}")

    if circuit.get("models"):
        lines.append("")

    # element cards
    for dev in circuit.get("devices", []):
        kind  = dev["kind"]
        name  = dev["name"]
        nodes = " ".join(dev["nodes"])
        value = dev["value"]
        model = dev["model"]
        params = dev.get("params", {})

        if kind in ("R", "C", "L"):
            val_str = _fmt_value(value) if value is not None else "0"
            extra = " ".join(f"{k}={_fmt_value(v) if isinstance(v, float) else v}"
                             for k, v in params.items())
            lines.append(f"{name} {nodes} {val_str}" + (f" {extra}" if extra else ""))

        elif kind in ("V", "I"):
            parts = [name, nodes]
            dc_level = value if value is not None else params.get("dc")
            if dc_level is not None:
                parts.append(f"DC {_fmt_value(dc_level)}")
            wf = params.get("waveform")
            if isinstance(wf, dict):
                wt = str(wf.get("type", "DC")).upper()
                if wt == "PULSE":
                    inner = " ".join(_fmt_value(wf[k]) for k in
                                     ("v1", "v2", "td", "tr", "tf", "pw", "per") if k in wf)
                    parts.append(f"PULSE({inner})")
                elif wt == "SIN":
                    inner = " ".join(_fmt_value(wf[k]) for k in
                                     ("vo", "va", "f", "td", "theta") if k in wf)
                    parts.append(f"SIN({inner})")
                elif wt == "PWL":
                    inner = " ".join(f"{_fmt_value(t)} {_fmt_value(v)}"
                                     for t, v in wf.get("points", []))
                    parts.append(f"PWL({inner})")
            ac = params.get("ac")
            if isinstance(ac, dict):
                parts.append(f"AC {_fmt_value(ac.get('mag', 1))} {_fmt_value(ac.get('phase', 0))}")
            lines.append(" ".join(parts))

        elif kind == "D":
            m_str = model or ""
            extra = " ".join(f"{k}={_fmt_value(v) if isinstance(v, float) else v}"
                             for k, v in params.items())
            lines.append(f"{name} {nodes} {m_str}" + (f" {extra}" if extra else ""))

        elif kind == "Q":
            lines.append(f"{name} {nodes} {model or ''}")

        elif kind == "M":
            extra = " ".join(f"{k}={_fmt_value(v) if isinstance(v, float) else v}"
                             for k, v in params.items())
            lines.append(f"{name} {nodes} {model or ''}" + (f" {extra}" if extra else ""))

        elif kind in ("E", "G"):
            gain = params.get("gain", 1.0)
            lines.append(f"{name} {nodes} {_fmt_value(gain)}")

        elif kind in ("F", "H"):
            gain = params.get("gain", 1.0)
            ctrl = params.get("ctrl", "")
            lines.append(f"{name} {nodes} {ctrl} {_fmt_value(gain)}")

        else:
            # OPAMP or anything else — emit a comment
            lines.append(f"* {name} {kind} {nodes}")

    lines.append("")

    # study / analysis cards
    if study:
        for analysis in study.get("analyses", []):
            atype = analysis["type"]
            if atype == "op":
                lines.append(".op")
            elif atype == "tran":
                tstep  = analysis.get("tstep", 1e-6)
                tstop  = analysis.get("tstop", 1e-3)
                tstart = analysis.get("tstart", 0.0)
                uic    = " UIC" if analysis.get("uic") else ""
                lines.append(f".tran {_fmt_value(tstep)} {_fmt_value(tstop)}"
                             f" {_fmt_value(tstart)}{uic}")
            elif atype == "dc":
                src  = analysis.get("source", "V1")
                start = analysis.get("start", 0.0)
                stop  = analysis.get("stop", 1.0)
                step  = analysis.get("step", 0.01)
                lines.append(f".dc {src} {_fmt_value(start)} {_fmt_value(stop)}"
                             f" {_fmt_value(step)}")
            elif atype == "ac":
                sweep  = analysis.get("sweep", "dec")
                pts    = analysis.get("points", 10)
                fstart = analysis.get("fstart", 1.0)
                fstop  = analysis.get("fstop", 1e6)
                lines.append(f".ac {sweep} {pts} {_fmt_value(fstart)}"
                             f" {_fmt_value(fstop)}")
            else:
                lines.append(f".{atype}")

        for probe in study.get("probes", []):
            lines.append(f".probe {probe}")

        opts = study.get("options", {})
        if opts:
            opt_str = " ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
                               for k, v in opts.items())
            lines.append(f".options {opt_str}")

    lines.append(".end")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# PUBLIC: netlist_to_spice_circuit
# ---------------------------------------------------------------------------

def netlist_to_spice_circuit(
    netlist: dict,
    sim_params: dict | None = None,
) -> tuple[dict, list[str]]:
    """Map a schematic netlist (per netlist.md §1) → (Circuit, warnings).

    Each component in netlist["components"] that has a "sim" mapping on its
    SymbolDef (looked up via libid from the starter library, or carried on the
    component dict itself as "sim") is converted to a Device.  Nodes are
    ordered by sim["pin_order"].  Net "GND" or "0" → node "0".

    sim_params is folded into Circuit["params"].
    """
    from engine.ecad.library.starter import SYMBOLS  # local import; avoid cycle
    from engine.sim import subckt as _sub             # local import; avoid cycle

    warn_msgs: list[str] = []
    devices:   list[dict] = []
    models:    dict[str, dict] = {}
    params:    dict = {"TEMP": 27}
    if sim_params:
        params.update(sim_params)

    subckts = _sub.builtin_subckts()
    x_lines: list[str] = []  # X-instance cards to expand after the component loop

    for comp in netlist.get("components", []):
        ref    = comp.get("ref", "?")
        libid  = comp.get("libid", "")
        value  = comp.get("value", "")
        pins   = comp.get("pins", {})        # {pinnum_or_pinname: netname}
        dnp    = comp.get("dnp", False)

        if dnp:
            warn_msgs.append(f"{ref}: DNP — skipped")
            continue

        # resolve sim dict — prefer one carried on the component,
        # fall back to the starter library's SymbolDef
        sim: dict | None = comp.get("sim")
        if sim is None:
            sym_def = SYMBOLS.get(libid)
            if sym_def:
                sim = sym_def.get("sim")

        if sim is None:
            warn_msgs.append(f"{ref} ({libid}): no sim model — skipped")
            continue

        model_name: str = sim.get("model", "")

        # Power / ground symbols — not real devices, skip silently
        if model_name in ("GND", "VCC", "+5V", "+3V3", "-5V"):
            continue

        pin_order: list[str] = sim.get("pin_order", [])

        # Build ordered node list
        node_list: list[str] = []
        for pname in pin_order:
            net = pins.get(pname)
            if net is None:
                warn_msgs.append(
                    f"{ref}: pin '{pname}' not found in netlist pins {list(pins.keys())}"
                )
                node_list.append("?")
            else:
                node_list.append(_gnd(net))

        # If pin_order is empty, fall back to sorted pin order
        if not pin_order:
            node_list = [_gnd(net) for net in
                         (pins[k] for k in sorted(pins.keys()))]

        # ── subcircuit-backed parts → emit an X instance, expand later ──────
        if model_name in ("SUBCKT", "OPAMP"):
            sub_name = _resolve_subckt_name(model_name, sim)
            sub_def = subckts.get(sub_name.upper())
            if sub_def is None:
                warn_msgs.append(f"{ref} ({libid}): subckt '{sub_name}' not built-in — skipped")
                continue
            # Map this part's ordered nodes onto the subckt's ports, dropping extra (supply) pins
            # the behavioural model does not use (e.g. OPAMP V+/V- on the 5-pin KiCad symbol).
            inst_nodes = _map_subckt_nodes(ref, model_name, node_list, sub_def.ports, warn_msgs)
            if inst_nodes is None:
                continue
            x_lines.append(f"X{ref} {' '.join(inst_nodes)} {sub_def.name}")
            continue

        # Parse value
        float_value: float | None = None
        if value:
            try:
                float_value = _parse_value(value)
            except ValueError:
                pass

        # Determine SPICE element name: use ref prefix + ref number
        # Kind maps model_name → SPICE kind letter
        kind = _model_to_kind(model_name, ref)

        dev = _dev(kind, ref, node_list, float_value, None, {})
        devices.append(dev)

    # Inline X instances (built-in subckts) + pull in the models the built-ins need.
    if x_lines:
        sub_devs, sub_warn = _sub.expand_instances(x_lines, subckts, _parse_element)
        devices.extend(sub_devs)
        warn_msgs.extend(sub_warn)
        for mname, mparams in _sub.BUILTIN_MODELS.items():
            models.setdefault(mname, dict(mparams))

    circuit = _make_circuit(devices, models, params)
    return circuit, warn_msgs


def _resolve_subckt_name(model_name: str, sim: dict) -> str:
    """Pick the built-in subckt name for a SUBCKT/OPAMP-backed component."""
    if model_name == "OPAMP":
        return "OPAMP"
    # SUBCKT: prefer the explicit sim["subckt"] (e.g. NE555), else the model name.
    return str(sim.get("subckt") or model_name)


def _map_subckt_nodes(
    ref: str, model_name: str, node_list: list[str], ports: list[str], warn_msgs: list[str]
) -> list[str] | None:
    """Map a component's ordered nodes onto a subckt's ports.

    Exact-length lists pass straight through. For the OPAMP case the 5-pin KiCad order
    ``[IN+, IN-, V+, V-, OUT]`` is reduced to the 3-port behavioural model ``[in+, in-, out]`` by
    dropping the V+/V- supply pins. Any other mismatch is a skip with a warning.
    """
    if len(node_list) == len(ports):
        return node_list
    if model_name == "OPAMP" and len(node_list) == 5 and len(ports) == 3:
        return [node_list[0], node_list[1], node_list[4]]  # [IN+, IN-, OUT]
    warn_msgs.append(
        f"{ref}: {len(node_list)} nodes do not match subckt ports {ports} — skipped"
    )
    return None


def _model_to_kind(model_name: str, ref: str) -> str:
    """Map a sim.model string to a SPICE kind letter."""
    _MAP = {
        "R": "R", "C": "C", "L": "L",
        "V": "V", "I": "I",
        "D": "D",
        "NPN": "Q", "PNP": "Q",
        "NMOS": "M", "PMOS": "M",
        "OPAMP": "OPAMP",
        "S": "S",
    }
    if model_name in _MAP:
        return _MAP[model_name]
    # fall back to first letter of ref if it looks like a SPICE kind
    letter = ref[0].upper() if ref else "X"
    if letter in ("R", "C", "L", "V", "I", "D", "Q", "M", "E", "G", "F", "H"):
        return letter
    return "X"
