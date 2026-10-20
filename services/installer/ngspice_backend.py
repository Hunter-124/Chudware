"""Optional ngspice subprocess backend for the circuit simulator (T-FIN14).

The validated MNA solver (C++ ``sim_spice``; formerly :mod:`engine.sim.circuit`)
stays the deterministic DEFAULT. This module runs the SAME Circuit dict + study through a
real ngspice binary as a subprocess and maps the results back into the exact
result shape ``simulate()`` returns, so callers (services/mcp ecad_simulate /
ecad_tf) can flip backends with one parameter and compare numbers 1:1.

Contract:
  * ngspice is OPTIONAL. Nothing here installs it; :func:`ngspice_status`
    detects it (env ``CHUDWARE_NGSPICE`` wins, else PATH) and reports an honest
    install hint (``dnf install ngspice``). When it is absent, callers degrade
    to the MNA backend with a note — never an exception.
  * Supported analyses: op, dc, tran, ac, tf. Everything else (four/mc/temp/
    noise/step wrappers) stays on the MNA solver.
  * Vendor models: a Circuit may carry two ADDITIVE optional keys —
        circuit["includes"]  : list[str]  — data-only .lib/.subckt files read and inlined
        circuit["xdevices"]  : list[{"name","nodes","subckt"}] — validated X instances
    These come from netlist_to_spice_circuit when a component's sim dict
    attaches a vendor library (``sim={"model":"SUBCKT","subckt":"UA741",
    "lib":"/path/ua741.lib", "pin_order":[...]}``). The MNA solver cannot run
    them; they are exactly what this backend unlocks.

Result shape parity (sim.md §4):
  op   -> {probe: value}
  dc   -> {"sweep": src, "x": [...], "traces": {probe: [...]}}
  tran -> {"x_t": [...], "traces": {probe: [...]}}
  ac   -> {"x_f": [...], "mag": {probe: [...]}, "phase_deg": {probe: [...]}}
  tf   -> {"gain","rin","rout","output","source"}
plus result["log"], result["convergence"][atype], result["ms"].
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Optional

# Analyses this backend can hand to ngspice; anything else stays on MNA.
SUPPORTED_ANALYSES = {"op", "dc", "tran", "ac", "tf"}

_TIMEOUT_S = float(os.environ.get("CHUDWARE_NGSPICE_TIMEOUT_S", "60"))

# --------------------------------------------------------------------------- detection

_FIND_CACHE: dict = {}


def find_ngspice() -> Optional[str]:
    """Absolute path to the ngspice binary, or None. Env CHUDWARE_NGSPICE overrides PATH."""
    key = os.environ.get("CHUDWARE_NGSPICE", "")
    if key in _FIND_CACHE:
        return _FIND_CACHE[key]
    path: Optional[str] = None
    if key:
        if os.path.isfile(key) and os.access(key, os.X_OK):
            path = key
    else:
        path = shutil.which("ngspice")
    _FIND_CACHE[key] = path
    return path


def ngspice_available() -> bool:
    return find_ngspice() is not None


def ngspice_version(path: Optional[str] = None) -> str:
    """Best-effort version string ('ngspice-44', '' when unknown/absent)."""
    p = path or find_ngspice()
    if not p:
        return ""
    try:
        out = subprocess.run([p, "--version"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"ngspice-(\d+(?:\.\d+)*)", out)
    return f"ngspice-{m.group(1)}" if m else (out.splitlines()[0].strip() if out else "")


def ngspice_status() -> dict:
    """Provisioning-registry probe: is the optional ngspice backend usable?"""
    path = find_ngspice()
    return {
        "tool": "ngspice",
        "installed": path is not None,
        "path": path or "",
        "version": ngspice_version(path) if path else "",
        "purpose": ("optional SPICE backend for ecad_simulate/ecad_tf "
                    "(vendor .SUBCKT/.MODEL libraries the internal MNA solver can't run)"),
        "install_hint": "" if path else
            "sudo dnf install ngspice   (or set CHUDWARE_NGSPICE=/path/to/ngspice)",
        "fallback": "internal MNA solver (backend='mna', the default) — always available",
    }


def resolve_backend(requested: str, circuit: dict, study: dict) -> tuple[str, str]:
    """Pick the backend that will actually run and say why.

    Returns ``(backend, note)`` with backend in {"mna","ngspice"}. ``note`` is ""
    when the request is honoured silently; otherwise it explains the degrade.
    """
    req = (requested or "mna").strip().lower()
    analyses = [a.get("type") for a in study.get("analyses", [])]
    has_step = study.get("step") is not None or "step" in analyses
    unsupported = sorted({a for a in analyses if a not in SUPPORTED_ANALYSES} |
                         ({"step"} if has_step else set()))
    needs_vendor = bool(circuit.get("xdevices") or circuit.get("includes"))

    if req == "mna":
        note = ""
        if needs_vendor:
            note = ("circuit carries vendor .lib/.subckt attachments the MNA solver "
                    "ignores — use backend='ngspice' to simulate them")
        return "mna", note

    if req not in ("ngspice", "auto"):
        return "mna", f"unknown backend {requested!r} — used the internal MNA solver"

    if not ngspice_available():
        if req == "ngspice":
            return "mna", ("ngspice is not installed on this machine — degraded to the "
                           "internal MNA solver. Install: sudo dnf install ngspice "
                           "(or set CHUDWARE_NGSPICE).")
        # auto: mna is the honest default when ngspice is absent
        note = ""
        if needs_vendor:
            note = ("vendor .lib/.subckt attachments need ngspice, which is not installed "
                    "(sudo dnf install ngspice) — they were ignored by the MNA solver")
        return "mna", note

    if unsupported:
        return "mna", (f"analysis {unsupported} is not supported by the ngspice backend — "
                       "ran the internal MNA solver instead")

    if req == "auto" and not needs_vendor:
        # auto prefers the deterministic default unless vendor models force ngspice.
        return "mna", ""
    return "ngspice", ""


# --------------------------------------------------------------------------- deck emission

def emit_deck(circuit: dict, study: dict) -> str:
    from services.sim.spice import _encode_spice_names
    circuit, study, _ = _encode_spice_names(circuit, study)
    return _emit_deck_tokens(circuit, study)


def _emit_deck_tokens(circuit: dict, study: dict) -> str:
    """Emit only circuit data and renderer-owned analysis directives.

    Vendor libraries are read once and inlined after a data-directive allowlist
    check: ngspice never reopens an unchecked include or nested include.
    """
    from services.sim.spice import _write_spice_tokens, spice_identifier

    body = _write_spice_tokens(circuit, None)
    lines = [ln for ln in body.splitlines() if ln.strip().lower() != ".end"]

    for inc in circuit.get("includes", []) or []:
        if not isinstance(inc, str) or not inc or any(ord(c) < 32 for c in inc):
            raise ValueError("invalid vendor library path")
        with open(inc, "r", encoding="utf-8") as library:
            text = library.read(8 * 1024 * 1024 + 1)
        if len(text) > 8 * 1024 * 1024:
            raise ValueError("vendor library exceeds 8 MiB")
        for line in text.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("*"):
                continue
            if stripped.startswith("."):
                directive = stripped.split(None, 1)[0].lower()
                if directive not in {".subckt", ".ends", ".model", ".param", ".func", ".global"}:
                    raise ValueError(f"vendor library directive not permitted: {directive}")
            if "\x00" in line:
                raise ValueError("invalid vendor library data")
        lines.extend(text.splitlines())
    for xd in circuit.get("xdevices", []) or []:
        name = spice_identifier(xd["name"])
        if not name.upper().startswith("X"):
            raise ValueError("subcircuit instance must begin with X")
        nodes = " ".join(spice_identifier(node) for node in xd["nodes"])
        lines.append(f"{name} {nodes} {spice_identifier(xd['subckt'])}")

    lines.append("")
    for an in study.get("analyses", []):
        lines.append(_analysis_card(an))

    lines += ["", ".end", ""]
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    from services.sim.spice import spice_number
    return f"{spice_number(v):.9g}"


def _analysis_card(an: dict) -> str:
    t = an.get("type")
    from services.sim.spice import spice_identifier, spice_probe
    if t == "op":
        return ".op"
    if t == "dc":
        return (f".dc {spice_identifier(an.get('source', 'V1'))} {_fmt(an.get('start', 0.0))} "
                f"{_fmt(an.get('stop', 1.0))} {_fmt(an.get('step', 0.01))}")
    if t == "tran":
        uic = " uic" if an.get("uic") else ""
        return (f".tran {_fmt(an.get('tstep', 1e-6))} {_fmt(an.get('tstop', 1e-3))} "
                f"{_fmt(an.get('tstart', 0.0))}{uic}")
    if t == "ac":
        sweep = an.get("sweep", "dec")
        if sweep not in {"dec", "oct", "lin"}:
            raise ValueError("invalid AC sweep")
        points = an.get("points", 10)
        if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
            raise ValueError("AC points must be a positive integer")
        return (f".ac {sweep} {points} "
                f"{_fmt(an.get('fstart', 1.0))} {_fmt(an.get('fstop', 1e6))}")
    if t == "tf":
        return f".tf {spice_probe(an.get('output', 'V(OUT)'))} {spice_identifier(an.get('source', 'V1'))}"
    raise ValueError(f"ngspice backend cannot emit analysis {t!r}")


# --------------------------------------------------------------------------- rawfile parsing

def _parse_rawfile(text: str) -> dict:
    """Parse an ASCII ngspice rawfile into {"vars":[names], "complex":bool,
    "data":{name_lower:[values]}} (complex values as python complex)."""
    lines = text.splitlines()
    names: list[str] = []
    is_complex = False
    n_vars = n_points = 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        low = ln.lower()
        if low.startswith("flags:"):
            is_complex = "complex" in low
        elif low.startswith("no. variables:"):
            n_vars = int(ln.split(":")[1])
        elif low.startswith("no. points:"):
            n_points = int(ln.split(":")[1])
        elif low.startswith("variables:"):
            for j in range(n_vars):
                parts = lines[i + 1 + j].split()
                names.append(parts[1])
            i += n_vars
        elif low.startswith("values:"):
            i += 1
            break
        i += 1

    data: dict[str, list] = {nm.lower(): [] for nm in names}
    vals: list[str] = []
    for ln in lines[i:]:
        vals.extend(ln.replace("\t", " ").split())
    # Each point: point-index token then n_vars value tokens.
    k = 0
    for _pt in range(n_points):
        k += 1  # skip point index
        for nm in names:
            if k >= len(vals):
                break
            tok = vals[k]
            k += 1
            if "," in tok:
                re_s, im_s = tok.split(",")
                data[nm.lower()].append(complex(float(re_s), float(im_s)))
            else:
                data[nm.lower()].append(float(tok))
    return {"vars": names, "complex": is_complex, "data": data}


def _lookup(raw: dict, probe: str) -> Optional[list]:
    """Map a study probe ('V(OUT)', 'I(V1)') onto rawfile vector names.

    mega-23 AC5: tolerate case / format variants (v(3v3) vs V(3V3) vs bare 3v3).
    """
    data = raw["data"]
    p = probe.strip()
    m = re.match(r"^([VvIi])\((.+)\)$", p)
    cands: list[str] = [p.lower()]
    if m:
        kind, arg = m.group(1).upper(), m.group(2).strip().lower()
        bare = arg.lstrip("+")
        if kind == "V":
            cands = [
                f"v({arg})", arg, bare, f"v({bare})",
                f"v(+{bare})", f"+{bare}",
            ]
        else:
            cands = [f"i({arg})", f"{arg}#branch", f"i({bare})", f"{bare}#branch"]
    # Also try casefold match against all vectors when exact miss.
    for c in cands:
        if c in data:
            return data[c]
    low_map = {str(k).lower(): k for k in data.keys()}
    for c in cands:
        k = low_map.get(c.lower())
        if k is not None:
            return data[k]
    return None


def _scale_vec(raw: dict) -> list:
    """The sweep/scale vector (time / frequency / swept value) = first variable."""
    if not raw["vars"]:
        return []
    return raw["data"].get(raw["vars"][0].lower(), [])


# --------------------------------------------------------------------------- run

class NgspiceError(RuntimeError):
    pass


def _default_probes(circuit: dict) -> list[str]:
    """Default probe set when the study specifies none: V(node) for every non-ground node, plus
    I(Vname) for every independent voltage source.

    Local home after engine.sim.circuit was deleted in the zero-Python cutover (the MNA
    solver is C++ sim_spice now; this backend is the ngspice subprocess leg). Ground
    aliases are excluded.
    """
    probes: list[str] = []
    seen: set[str] = set()
    grounds = {"0", "gnd", "GND"}
    # Collect node names in a stable order: declared nodes first, then any device-only nodes.
    nodes: list[str] = list(circuit.get("nodes", []))
    for d in circuit.get("devices", []):
        for nm in d.get("nodes", []):
            if nm not in nodes:
                nodes.append(nm)
    for nm in nodes:
        s = str(nm)
        if s in grounds or s in seen:
            continue
        seen.add(s)
        probes.append(f"V({s})")
    for d in circuit.get("devices", []):
        if d.get("kind") == "V":
            probes.append(f"I({d.get('name')})")
    return probes


def _run_ngspice(deck: str, want_raw: bool) -> tuple[str, str]:
    """Run ngspice -b on ``deck``; return (rawfile_text, stdout). Raises NgspiceError."""
    exe = find_ngspice()
    if not exe:
        raise NgspiceError("ngspice binary not found")
    with tempfile.TemporaryDirectory(prefix="chudware-ngspice-") as td:
        cir = os.path.join(td, "deck.cir")
        rawp = os.path.join(td, "out.raw")
        with open(cir, "w", encoding="utf-8") as f:
            f.write(deck)
        cmd = [exe, "-n", "-b"]  # do not load a user's .spiceinit command file
        if want_raw:
            cmd += ["-r", rawp]
        cmd.append(cir)
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot") if key in os.environ}
        env.update({"HOME": td, "SPICE_ASCIIRAWFILE": "1"})
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=_TIMEOUT_S, env=env, cwd=td)
        except subprocess.TimeoutExpired as e:
            raise NgspiceError(f"ngspice timed out after {_TIMEOUT_S:.0f}s") from e
        except OSError as e:
            raise NgspiceError(f"ngspice failed to launch: {e}") from e
        raw_text = ""
        if want_raw and os.path.isfile(rawp):
            with open(rawp, "r", encoding="utf-8", errors="replace") as f:
                raw_text = f.read()
        if proc.returncode != 0 and not raw_text:
            tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-6:])
            raise NgspiceError(f"ngspice exit {proc.returncode}: {tail}")
        return raw_text, (proc.stdout or "") + "\n" + (proc.stderr or "")


_TF_PATTERNS = {
    "gain": re.compile(r"[Tt]ransfer[_ ]function\s*=?\s*([-+0-9.eE]+)"),
    "rout": re.compile(r"output_impedance[^=\n]*=?\s*([-+0-9.eE]+)"),
    "rin":  re.compile(r"input_impedance[^=\n]*=?\s*([-+0-9.eE]+)"),
}


def simulate_ngspice(circuit: dict, study: dict) -> dict:
    """Run the study through ngspice; return a sim.md §4 result dict.

    Raises :class:`NgspiceError` on any subprocess/parse failure — the seam in
    :func:`services.mcp.ecad_common._sim_run` reports it and falls back to MNA.
    """
    t0 = time.perf_counter()
    log: list[str] = []
    result: dict = {"log": log, "convergence": {}}
    probes = list(study.get("probes", []))
    if not probes:
        probes = _default_probes(circuit)
        if probes:
            log.append(f"probes: none specified — defaulting to all {len(probes)} "
                       "node/source outputs")

    from services.sim.spice import _encode_spice_names
    mapped_circuit, mapped_study, mapped_probes = _encode_spice_names(
        circuit, {**study, "probes": probes})
    for an, mapped_analysis in zip(study.get("analyses", []), mapped_study["analyses"]):
        atype = an.get("type")
        if atype not in SUPPORTED_ANALYSES:
            raise NgspiceError(f"unsupported analysis {atype!r}")
        deck = _emit_deck_tokens(mapped_circuit, {"analyses": [mapped_analysis]})
        want_raw = atype != "tf"
        raw_text, stdout = _run_ngspice(deck, want_raw)

        if atype == "tf":
            tf = {"output": an.get("output"), "source": an.get("source")}
            ok = True
            for key, pat in _TF_PATTERNS.items():
                m = pat.search(stdout)
                tf[key] = float(m.group(1)) if m else float("nan")
                ok = ok and m is not None
            result["tf"] = tf
            result["convergence"]["tf"] = ok
            log.append(f"ngspice tf: gain={tf.get('gain')} rin={tf.get('rin')} "
                       f"rout={tf.get('rout')}")
            continue

        if not raw_text:
            tail = "\n".join(stdout.splitlines()[-6:])
            raise NgspiceError(f"ngspice produced no rawfile for {atype}: {tail}")
        raw = _parse_rawfile(raw_text)
        n_pts = len(_scale_vec(raw))
        ok = n_pts > 0

        if atype == "op":
            op: dict = {}
            for p in probes:
                vec = _lookup(raw, mapped_probes[p])
                v = vec[0] if vec else float("nan")
                op[p] = float(v.real) if isinstance(v, complex) else float(v)
                if vec is None:
                    log.append(f"ngspice op: probe {p} not in rawfile")
            result["op"] = op
            result["convergence"]["op"] = ok
            log.append(f"ngspice op: {len(op)} probes")

        elif atype == "dc":
            xs = [float(v.real) if isinstance(v, complex) else float(v)
                  for v in _scale_vec(raw)]
            traces = {}
            for p in probes:
                vec = _lookup(raw, mapped_probes[p])
                traces[p] = ([float(v.real) if isinstance(v, complex) else float(v)
                              for v in vec] if vec else [float("nan")] * len(xs))
            result["dc"] = {"sweep": an.get("source", "V1"), "x": xs, "traces": traces}
            result["convergence"]["dc"] = ok
            log.append(f"ngspice dc: swept {an.get('source')} ({len(xs)} points)")

        elif atype == "tran":
            ts = [float(v.real) if isinstance(v, complex) else float(v)
                  for v in _scale_vec(raw)]
            traces = {}
            for p in probes:
                vec = _lookup(raw, mapped_probes[p])
                traces[p] = ([float(v.real) if isinstance(v, complex) else float(v)
                              for v in vec] if vec else [float("nan")] * len(ts))
            result["tran"] = {"x_t": ts, "traces": traces}
            result["convergence"]["tran"] = ok
            log.append(f"ngspice tran: {len(ts)} points to t={ts[-1] if ts else 0:.3e}")

        elif atype == "ac":
            freqs = [abs(v) if isinstance(v, complex) else float(v)
                     for v in _scale_vec(raw)]
            mag: dict = {}
            phase: dict = {}
            for p in probes:
                vec = _lookup(raw, mapped_probes[p])
                if vec is None:
                    mag[p] = [float("nan")] * len(freqs)
                    phase[p] = [float("nan")] * len(freqs)
                    continue
                cs = [complex(v) for v in vec]
                mag[p] = [abs(c) for c in cs]
                phase[p] = [math.degrees(math.atan2(c.imag, c.real)) for c in cs]
            result["ac"] = {"x_f": freqs, "mag": mag, "phase_deg": phase}
            result["convergence"]["ac"] = ok
            log.append(f"ngspice ac: {len(freqs)} points")

    result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return result
