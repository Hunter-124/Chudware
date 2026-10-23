"""SPICE netlist import/export + schematic-netlist→Circuit bridge.

Public API
----------
parse_spice(text) -> Circuit
write_spice(circuit, study=None) -> str
parse_spice_deck(text) -> (Circuit, study | None)
netlist_to_spice_circuit(netlist, sim_params=None) -> (Circuit, list[str])

Circuit shape is FROZEN per contracts/ecad/sim.md §1:
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

import math
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
    value = num * _SUFFIX.get((m.group(2) or "").lower(), 1.0)
    if not math.isfinite(value):
        raise ValueError(f"SPICE value must be finite: {token!r}")
    return value


def _fmt_value(v: float) -> str:
    """Format a float back to a compact SPICE token (best suffix)."""
    if not math.isfinite(float(v)):
        raise ValueError("cannot format a non-finite SPICE value")
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
            if not inner:
                raise ValueError("PULSE source needs a closing parenthesis")
            vals = inner.group(1).split()
            if len(vals) < 2 or len(vals) > 7:
                raise ValueError("PULSE source needs 2 to 7 numeric parameters")
            keys = ["v1", "v2", "td", "tr", "tf", "pw", "per"]
            params["waveform"] = {"type": "PULSE",
                                  **{k: _safe_val(v) for k, v in zip(keys, vals)}}
            # skip to after closing paren
            skip = 1
            for j in range(i, len(tokens)):
                if ")" in tokens[j]:
                    skip = j - i + 1
                    break
            i += skip
        elif tok.startswith("SIN(") or tok == "SIN":
            full = " ".join(tokens[i:])
            inner = re.search(r"SIN\(([^)]*)\)", full, re.IGNORECASE)
            if not inner:
                raise ValueError("SIN source needs a closing parenthesis")
            vals = inner.group(1).split()
            if len(vals) < 3 or len(vals) > 5:
                raise ValueError("SIN source needs 3 to 5 numeric parameters")
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
            if not inner:
                raise ValueError("PWL source needs a closing parenthesis")
            nums = inner.group(1).split()
            if len(nums) < 4 or len(nums) % 2:
                raise ValueError("PWL source needs at least two complete time/value pairs")
            pairs = []
            for k in range(0, len(nums), 2):
                pairs.append((_safe_val(nums[k]), _safe_val(nums[k + 1])))
            params["waveform"] = {"type": "PWL", "points": pairs}
            skip = 1
            for j in range(i, len(tokens)):
                if ")" in tokens[j]:
                    skip = j - i + 1
                    break
            i += skip
        elif tok == "AC":
            if i + 1 >= len(tokens):
                raise ValueError("AC source needs a numeric magnitude")
            magnitude = _safe_val(tokens[i + 1])
            phase = 0.0
            advance = 2
            if i + 2 < len(tokens):
                try:
                    phase = _parse_value(tokens[i + 2])
                    advance = 3
                except ValueError:
                    pass
            params["ac"] = {"mag": magnitude, "phase": phase}
            i += advance
        elif tok == "DC":
            if i + 1 >= len(tokens):
                raise ValueError("DC source needs a numeric value")
            params["dc"] = _safe_val(tokens[i + 1])
            i += 2
        else:
            # A bare numeric token is a DC value. Anything else is malformed,
            # not an instruction to manufacture a zero-valued source.
            try:
                params["dc"] = _parse_value(tokens[i])
            except ValueError as exc:
                raise ValueError(f"unsupported source token {tokens[i]!r}") from exc
            i += 1
    return params


def _safe_val(s: str) -> float:
    """Parse a required numeric token without silently substituting zero."""
    return _parse_value(s)


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
    """Parse and validate .tran/.dc/.ac/.op tokens into a study analysis dict."""
    kw = keyword.upper()
    if kw == ".OP":
        return {"type": "op"}
    if kw == ".TRAN":
        if len(tokens) < 2:
            raise ValueError(".TRAN needs tstep and tstop")
        tstep, tstop = _parse_value(tokens[0]), _parse_value(tokens[1])
        if tstep <= 0.0 or tstop <= 0.0:
            raise ValueError(".TRAN tstep and tstop must be greater than zero")
        if tstop / tstep > 1_000_000:
            raise ValueError(".TRAN exceeds 1000000 nominal steps")
        analysis: dict[str, Any] = {"type": "tran", "tstep": tstep, "tstop": tstop}
        if len(tokens) >= 3 and tokens[2].upper() != "UIC":
            tstart = _parse_value(tokens[2])
            if tstart < 0.0 or tstart >= tstop:
                raise ValueError(".TRAN tstart must be non-negative and less than tstop")
            analysis["tstart"] = tstart
        analysis["uic"] = any(token.upper() == "UIC" for token in tokens)
        return analysis
    if kw == ".DC":
        if len(tokens) < 4:
            raise ValueError(".DC needs source, start, stop, and step")
        start, stop, step = map(_parse_value, tokens[1:4])
        if start != stop and (step == 0.0 or (stop - start) * step < 0.0):
            raise ValueError(".DC step must be non-zero and point toward stop")
        return {"type": "dc", "source": tokens[0],
                "start": start, "stop": stop, "step": step}
    if kw == ".AC":
        if len(tokens) < 4:
            raise ValueError(".AC needs sweep, points, fstart, and fstop")
        sweep = tokens[0].lower()
        if sweep not in ("dec", "oct", "lin"):
            raise ValueError(".AC sweep must be dec, oct, or lin")
        points_value = _parse_value(tokens[1])
        if int(points_value) != points_value or points_value < 1:
            raise ValueError(".AC points must be a positive integer")
        fstart, fstop = _parse_value(tokens[2]), _parse_value(tokens[3])
        if fstart <= 0.0 or fstop <= fstart:
            raise ValueError(".AC frequencies must be positive and increasing")
        span = (math.log10(fstop) - math.log10(fstart)
                if sweep == "dec"
                else math.log2(fstop) - math.log2(fstart)
                if sweep == "oct"
                else 1.0)
        samples = points_value if sweep == "lin" else points_value * span + 1.0
        if not math.isfinite(samples) or samples > 1_000_000:
            raise ValueError(".AC exceeds 1000000 frequency samples")
        return {"type": "ac", "sweep": sweep, "points": int(points_value),
                "fstart": fstart, "fstop": fstop}
    raise ValueError(f"unsupported SPICE analysis {keyword!r}")


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
        try:
            value = _parse_value(tokens[3])
        except ValueError as exc:
            raise ValueError(f"{name}: invalid {kind_letter} value {tokens[3]!r}") from exc
        if value <= 0.0:
            raise ValueError(f"{name}: {kind_letter} value must be greater than zero")
        params: dict[str, Any] = {}
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
        if value is None and "dc" in src_params:
            value = src_params.pop("dc")
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
        gain = _parse_value(tokens[5])
        return _dev(kind_letter, name, nodes, None, None, {"gain": gain})

    if kind_letter in ("F", "H"):
        # F/H  name  n+ n-  Vcontrol  gain
        if len(tokens) < 5:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2])]
        ctrl = tokens[3]
        gain = _parse_value(tokens[4])
        return _dev(kind_letter, name, nodes, None, None,
                    {"gain": gain, "ctrl": ctrl})

    if kind_letter == "K":
        # Kname  Lx  Ly  k (mutual-inductance coupling between two inductors)
        if len(tokens) < 4:
            return None
        l1, l2 = tokens[1], tokens[2]
        k = _parse_value(tokens[3])
        if abs(k) > 1.0:
            raise ValueError(f"{name}: coupling coefficient must be in [-1, 1]")
        return _dev("K", name, [], k, None, {"inductors": [l1, l2], "k": k})

    if kind_letter == "B":
        # Bname  n+ n-  V=<expr> | I=<expr>   (behavioral / arbitrary source)
        if len(tokens) < 4:
            return None
        nodes = [_gnd(tokens[1]), _gnd(tokens[2])]
        btype, expr = _parse_behavioral_rhs(tokens[3:])
        if expr is None:
            return None
        return _dev("B", name, nodes, None, None, {"btype": btype, "expr": expr})

    # Unknown element letter → silently skip
    return None


def _parse_behavioral_rhs(rest: list[str]) -> tuple[str, str | None]:
    """Parse the ``V=<expr>`` / ``I=<expr>`` tail of a B-source card.

    The expression may contain spaces (e.g. ``V = 2 * V(in)``), so the whole tail is re-joined and the
    leading ``V=`` / ``I=`` stripped. Returns ``(btype, expr_or_None)`` where ``btype`` is ``"V"`` or
    ``"I"``. Anything else (no V=/I=) yields ``(_, None)`` so the caller skips the card.
    """
    tail = " ".join(rest).strip()
    m = re.match(r"^\s*([VvIi])\s*=\s*(.+)$", tail, re.DOTALL)
    if not m:
        return "V", None
    btype = m.group(1).upper()
    expr = m.group(2).strip()
    return btype, (expr or None)


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
    from services.sim import subckt as _sub  # local import: subckt imports spice helpers

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

_IDENTIFIER_TOKEN = r"[A-Za-z0-9_:/+-][A-Za-z0-9_:/+.-]*"
_IDENTIFIER = re.compile(_IDENTIFIER_TOKEN, re.ASCII)
_PROBE = re.compile(rf"[VvIi]\(({_IDENTIFIER_TOKEN})(?:,({_IDENTIFIER_TOKEN}))?\)", re.ASCII)
_EXPRESSION_TOKEN = re.compile(
    r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?(?:meg|mil|[gkmunpf])?"
    r"|[A-Za-z_][A-Za-z0-9_]*|&&|\|\||[()+*/^<>=!?:,-])", re.IGNORECASE | re.ASCII)


def spice_identifier(value: Any) -> str:
    """A single data token, never a command, comment, quoted string or new line."""
    if (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
            or value.lower() == "shell"):
        raise ValueError(f"invalid SPICE identifier: {value!r}")
    return value


def spice_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("SPICE numbers cannot be booleans")
    try:
        if isinstance(value, str) and not _SUFFIX_RE.fullmatch(value):
            raise ValueError("SPICE numbers must be single numeric tokens")
        number = _parse_value(value) if isinstance(value, str) else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid SPICE number: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError("SPICE numbers must be finite")
    return number


def spice_probe(value: Any) -> str:
    match = _PROBE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"invalid SPICE probe: {value!r}")
    for node in match.groups():
        if node is not None:
            spice_identifier(node)
    return value


def _spice_expression(value: Any) -> None:
    """Arithmetic/probe expression tokens only; no command/string syntax."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("empty SPICE behavioral expression")
    offset = 0
    while offset < len(value):
        if value[offset] == " ":
            offset += 1
            continue
        token = _EXPRESSION_TOKEN.match(value, offset)
        if token is None or token.group().lower() == "shell":
            raise ValueError("invalid SPICE behavioral expression")
        offset = token.end()


def _validate_spice_data(circuit: dict, study: dict | None) -> None:
    for node in circuit.get("nodes", []):
        spice_identifier(node)
    for name, model in circuit.get("models", {}).items():
        spice_identifier(name)
        if str(model.get("type", "")).upper() not in {"D", "NPN", "PNP", "NMOS", "PMOS", "SW", "CSW", "NJF", "PJF"}:
            raise ValueError("invalid SPICE model type")
        for key, value in model.items():
            if key != "type":
                spice_identifier(key)
                spice_number(value)
    for dev in circuit.get("devices", []):
        kind = spice_identifier(dev["kind"])
        name = spice_identifier(dev["name"])
        if not name.upper().startswith(kind.upper()[0]):
            raise ValueError("SPICE device name must begin with its device kind")
        for node in dev["nodes"]:
            spice_identifier(node)
        if dev.get("model"):
            spice_identifier(dev["model"])
        if dev.get("value") is not None:
            spice_number(dev["value"])
        for key, value in dev.get("params", {}).items():
            spice_identifier(key)
            if key in {"ctrl", "type", "btype"}:
                spice_identifier(value)
                if key == "btype" and value.upper() not in {"V", "I"}:
                    raise ValueError("invalid behavioral source type")
            elif key == "expr":
                _spice_expression(value)
            elif key == "inductors":
                for inductor in value:
                    spice_identifier(inductor)
            elif key == "waveform":
                if value.get("type", "DC").upper() not in {"DC", "PULSE", "SIN", "PWL"}:
                    raise ValueError("invalid source waveform")
                for field, scalar in value.items():
                    if field == "type":
                        continue
                    if field == "points":
                        for pair in scalar:
                            for number in pair:
                                spice_number(number)
                    else:
                        spice_identifier(field)
                        spice_number(scalar)
            elif key == "ac":
                for field, number in value.items():
                    spice_identifier(field)
                    spice_number(number)
            else:
                spice_number(value)
    if study:
        for analysis in study.get("analyses", []):
            # The shared analysis emitter validates every interpolation.
            from services.installer.ngspice_backend import _analysis_card
            _analysis_card(analysis)
        for probe in study.get("probes", []):
            spice_probe(probe)
        for key, value in study.get("options", {}).items():
            spice_identifier(key)
            spice_number(value)


def _encode_spice_names(circuit: dict, study: dict | None = None):
    """Bijectively encode display labels as solver tokens; never emit a label."""
    from copy import deepcopy
    data = deepcopy(circuit)
    request = deepcopy(study) if study is not None else None
    nodes: dict[str, str] = {}
    devices: dict[str, str] = {}

    def label(value):
        if (not isinstance(value, str) or not value or value.startswith(".")
                or re.search(r"(?:^|\s)shell(?:$|\s)", value, re.IGNORECASE)
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError(f"invalid SPICE label: {value!r}")
        return value

    def node(value):
        value = label(value)
        if value not in nodes:
            nodes[value] = "0" if value.lower() in {"0", "gnd"} else f"cw_node_{len(nodes)}"
        return nodes[value]

    def device(value, kind=None):
        value = label(value)
        if value not in devices:
            prefix = spice_identifier(kind or value[0])[0].upper()
            if not prefix.isalpha():
                raise ValueError("invalid device kind")
            devices[value] = f"{prefix}cw_device_{len(devices)}"
        return devices[value]

    data["nodes"] = [node(value) for value in data.get("nodes", [])]
    for dev in data.get("devices", []):
        dev["name"] = device(dev["name"], dev["kind"])
        dev["nodes"] = [node(value) for value in dev["nodes"]]
    for dev in data.get("xdevices", []):
        if not label(dev["name"]).upper().startswith("X"):
            raise ValueError("subcircuit instance must begin with X")
        dev["name"] = device(dev["name"], "X")
        dev["nodes"] = [node(value) for value in dev["nodes"]]

    def probe(value):
        label(value)
        match = re.fullmatch(r"([VvIi])\((.+)\)", value)
        if match is None:
            raise ValueError("invalid SPICE probe")
        kind, argument = match.groups()
        if kind.upper() == "I":
            return f"I({device(argument)})"
        # Exact lookup first permits EDA net labels containing parentheses/commas.
        if argument in nodes:
            return f"V({nodes[argument]})"
        arguments = argument.split(",")
        if len(arguments) not in {1, 2}:
            raise ValueError("invalid voltage probe")
        return f"V({','.join(node(arg) for arg in arguments)})"

    for dev in data.get("devices", []):
        params = dev.get("params", {})
        if "ctrl" in params:
            params["ctrl"] = device(params["ctrl"])
        if "inductors" in params:
            params["inductors"] = [device(value, "L") for value in params["inductors"]]
        if "expr" in params:
            expression = params["expr"]
            if not isinstance(expression, str):
                raise ValueError("invalid behavioral expression")
            for kind, names in (("V", nodes), ("I", devices)):
                if names:
                    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
                    expression = re.sub(
                        rf"[{kind}{kind.lower()}]\(({alternatives})\)",
                        lambda match, names=names, k=kind: f"{k}({names[match.group(1)]})",
                        expression)
            params["expr"] = expression
    probes = {}
    if request:
        for analysis in request.get("analyses", []):
            if analysis.get("type") in {"dc", "tf"}:
                analysis["source"] = device(analysis.get("source", "V1"))
            if analysis.get("type") == "tf":
                analysis["output"] = probe(analysis.get("output", "V(OUT)"))
        probes = {value: probe(value) for value in request.get("probes", [])}
        request["probes"] = list(probes.values())
    return data, request, probes


def write_spice(circuit: dict, study: dict | None = None) -> str:
    """Serialize data with generated solver identifiers, never display labels."""
    circuit, study, _ = _encode_spice_names(circuit, study)
    return _write_spice_tokens(circuit, study)


def _write_spice_tokens(circuit: dict, study: dict | None = None) -> str:
    _validate_spice_data(circuit, study)
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

        elif kind == "K":
            inds = params.get("inductors", [])
            k = value if value is not None else params.get("k", 1.0)
            if len(inds) >= 2:
                lines.append(f"{name} {inds[0]} {inds[1]} {_fmt_value(k)}")

        elif kind == "B":
            btype = str(params.get("btype", "V")).upper()
            expr = params.get("expr", "")
            lines.append(f"{name} {nodes} {btype}={expr}")

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
    from services.ecad.library.starter import SYMBOLS  # local import; avoid cycle
    from services.sim import subckt as _sub             # local import; avoid cycle

    warn_msgs: list[str] = []
    devices:   list[dict] = []
    models:    dict[str, dict] = {}
    params:    dict = {"TEMP": 27}
    if isinstance(sim_params, dict):
        params.update(sim_params)
    elif sim_params is not None:
        warn_msgs.append("sim_params must be an object — ignored")

    subckts = _sub.builtin_subckts()
    x_lines: list[str] = []  # X-instance cards to expand after the component loop
    vendor_x: list[dict] = []        # vendor-subckt X instances (ngspice backend only)
    vendor_includes: list[str] = []  # .lib/.subckt file paths to .include

    components = netlist.get("components", []) if isinstance(netlist, dict) else []
    if not isinstance(components, list):
        return _make_circuit([], models, params), ["netlist.components must be an array"]
    for comp in components:
        if not isinstance(comp, dict):
            warn_msgs.append("non-object component — skipped")
            continue
        ref = str(comp.get("ref") or "?")
        libid = str(comp.get("libid") or "")
        value = comp.get("value", "")
        pins = comp.get("pins", {})
        if not isinstance(pins, dict):
            warn_msgs.append(f"{ref}: pins must be an object — skipped")
            continue
        dnp = bool(comp.get("dnp", False))

        if dnp:
            warn_msgs.append(f"{ref}: DNP — skipped")
            continue

        # resolve sim dict — prefer one carried on the component,
        # fall back to the starter library's SymbolDef
        sim: dict | None = comp.get("sim")
        if sim is not None and not isinstance(sim, dict):
            warn_msgs.append(f"{ref}: sim model specification must be an object — skipped")
            continue
        if sim is None:
            sym_def = SYMBOLS.get(libid)
            if sym_def:
                sim = sym_def.get("sim")

        if sim is None:
            warn_msgs.append(f"{ref} ({libid}): no sim model — skipped")
            continue
        if not isinstance(sim, dict):
            warn_msgs.append(f"{ref}: resolved sim model must be an object — skipped")
            continue

        model_name = str(sim.get("model") or "")
        model_upper = model_name.upper()
        if not model_upper:
            warn_msgs.append(f"{ref} ({libid}): empty sim model — skipped")
            continue

        # Power / ground symbols — not real devices, skip silently
        if model_upper in ("GND", "VCC", "+5V", "+3V3", "-5V"):
            continue

        raw_pin_order = sim.get("pin_order", [])
        if not isinstance(raw_pin_order, list) or not all(
                isinstance(pin, (str, int)) for pin in raw_pin_order):
            warn_msgs.append(f"{ref}: sim pin_order must be an array of pin names — skipped")
            continue
        pin_order: list[str] = [str(pin) for pin in raw_pin_order]

        # A sim.pin_order entry may be a pin NUMBER (matching the netlist's
        # number-keyed pins, e.g. R "1"/"2") OR a pin NAME (e.g. op-amp
        # "IN+"/"IN-"/"OUT").  Build a name->number map from the SymbolDef so a
        # name entry resolves against the number-keyed netlist pins.
        name_to_num: dict[str, str] = {}
        _sdef = SYMBOLS.get(libid)
        if _sdef:
            for _p in _sdef.get("pins", []):
                _nm, _num = _p.get("name"), _p.get("number")
                if _nm is not None and _num is not None:
                    name_to_num[str(_nm)] = str(_num)

        # Build ordered node list. A missing pin invalidates the component;
        # never manufacture a literal "?" node and pass it to a backend.
        node_list: list[str] = []
        unresolved = False
        for pname in pin_order:
            net = pins.get(pname)
            if net is None and pname in name_to_num:
                net = pins.get(name_to_num[pname])
            if not isinstance(net, str) or not net:
                warn_msgs.append(f"{ref}: pin '{pname}' is unresolved — component skipped")
                unresolved = True
            else:
                node_list.append(_gnd(net))
        if unresolved:
            continue

        # If pin_order is empty, fall back to sorted pin order.
        if not pin_order:
            fallback = [pins[key] for key in sorted(pins.keys())]
            if any(not isinstance(net, str) or not net for net in fallback):
                warn_msgs.append(f"{ref}: unresolved net in fallback pin order — component skipped")
                continue
            node_list = [_gnd(net) for net in fallback]

        # ── subcircuit-backed parts → emit an X instance, expand later ──────
        if model_upper in ("SUBCKT", "OPAMP"):
            sub_name = _resolve_subckt_name(model_upper, sim)
            sub_def = subckts.get(sub_name.upper())
            if sub_def is None:
                # Vendor model attachment: a component may carry a .lib/.subckt
                # FILE on sim["lib"]. The internal MNA solver can't expand it, but the
                # optional ngspice backend can — record it as an additive X instance +
                # .include so `simulate(..., backend='ngspice')` runs the real model.
                lib_path = sim.get("lib") or sim.get("include")
                if lib_path:
                    vendor_x.append({"name": f"X{ref}", "nodes": list(node_list),
                                     "subckt": sub_name})
                    if str(lib_path) not in vendor_includes:
                        vendor_includes.append(str(lib_path))
                    warn_msgs.append(
                        f"{ref} ({libid}): vendor subckt '{sub_name}' from {lib_path} — "
                        "needs backend='ngspice' (the internal MNA solver ignores it)")
                    continue
                warn_msgs.append(f"{ref} ({libid}): subckt '{sub_name}' not built-in — skipped")
                continue
            # Map this part's ordered nodes onto the subckt's ports, dropping extra (supply) pins
            # the behavioural model does not use (e.g. OPAMP V+/V- on the 5-pin KiCad symbol).
            inst_nodes = _map_subckt_nodes(ref, model_upper, node_list, sub_def.ports, warn_msgs)
            if inst_nodes is None:
                continue
            x_lines.append(f"X{ref} {' '.join(inst_nodes)} {sub_def.name}")
            continue

        # Parse finite numeric values from either schematic strings or JSON numbers.
        float_value: float | None = None
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                float_value = float(value)
                if not math.isfinite(float_value):
                    raise ValueError("non-finite")
            elif value not in ("", None):
                float_value = _parse_value(str(value))
        except (TypeError, ValueError):
            float_value = None

        kind = _model_to_kind(model_upper, ref)
        if kind in ("X", "OPAMP"):
            warn_msgs.append(f"{ref} ({libid}): unsupported sim model '{model_name}' — skipped")
            continue
        expected_nodes = {"R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "D": 2,
                          "Q": 3, "M": 3, "E": 4, "G": 4, "F": 2, "H": 2,
                          "S": 4}.get(kind)
        if expected_nodes is None or len(node_list) < expected_nodes:
            warn_msgs.append(
                f"{ref}: sim model {model_name} needs at least {expected_nodes or 0} nodes — skipped")
            continue
        if kind in ("R", "C", "L") and (float_value is None or float_value <= 0.0):
            warn_msgs.append(f"{ref}: invalid or non-positive {kind} value — skipped")
            continue

        carried_params = sim.get("params", {}) if isinstance(sim, dict) else {}
        dev_params: dict = dict(carried_params) if isinstance(carried_params, dict) else {}
        _pol = {"NMOS": "nmos", "PMOS": "pmos", "NPN": "npn", "PNP": "pnp"}.get(model_upper)
        if _pol:
            dev_params["type"] = _pol
        # Independent sources (V/I) may carry a small-signal AC magnitude so an AC
        # sweep has a stimulus. The structured netlist path parses only the DC
        # `value`, so without this an AC analysis sees 0 at every frequency (Bug
        # E-1). Honor `ac_mag` carried on the component (set by build_design) or an
        # `ac` mapping on the sim dict; a phase may ride along on `ac_phase`.
        if kind in ("V", "I"):
            ac_mag = comp.get("ac_mag")
            ac_phase = comp.get("ac_phase", 0.0)
            if ac_mag is None and isinstance(sim, dict):
                _sa = sim.get("ac")
                if isinstance(_sa, dict):
                    ac_mag = _sa.get("mag")
                    ac_phase = _sa.get("phase", ac_phase)
                elif _sa is not None:
                    ac_mag = _sa
            if ac_mag is not None:
                try:
                    magnitude = float(ac_mag)
                    phase = float(ac_phase)
                    if not math.isfinite(magnitude) or not math.isfinite(phase):
                        raise ValueError("non-finite AC source")
                    dev_params["ac"] = {"mag": magnitude, "phase": phase}
                except (TypeError, ValueError):
                    warn_msgs.append(f"{ref}: invalid AC magnitude/phase ignored")

        dev = _dev(kind, ref, node_list, float_value, None, dev_params)
        devices.append(dev)

    # Inline X instances (built-in subckts) + pull in the models the built-ins need.
    if x_lines:
        sub_devs, sub_warn = _sub.expand_instances(x_lines, subckts, _parse_element)
        devices.extend(sub_devs)
        warn_msgs.extend(sub_warn)
        for mname, mparams in _sub.BUILTIN_MODELS.items():
            models.setdefault(mname, dict(mparams))

    circuit = _make_circuit(devices, models, params)
    # Additive optional keys: vendor attachments for the ngspice backend.
    # The MNA solver reads only nodes/devices/models/params, so these are transparent to it.
    if vendor_x:
        circuit["xdevices"] = vendor_x
        circuit["includes"] = vendor_includes
        # Make sure the X-instance nodes exist in the node list (probe defaults etc.).
        known = set(circuit.get("nodes", []))
        for xd in vendor_x:
            for nd in xd["nodes"]:
                if nd not in known and nd != "?":
                    circuit.setdefault("nodes", []).append(nd)
                    known.add(nd)
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
