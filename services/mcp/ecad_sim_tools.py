"""ECAD simulation/analysis MCP tools (split out of ecad_tools.py): ecad_simulate, run_pdn, sweep, fpga_check, impedance checks, run_sicheck, tf, design."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional, Union
import json
import os
import math
import threading

from .cad_common import coerce_json_or_repr, require_list, require_mapping
from .ecad_common import (
    OpError,
    _ECAD,
    _PROVIDER_PLAN_BUSY,
    _PROVIDER_PLAN_LOCK,
    _apply_sim_source_params,
    _autosave_all_open_ecad,
    _budget_timeout_err,
    _build_analysis,
    _default_out_dir,
    _dumps,
    _eng,
    _erc_lines,
    _erc_summary,
    _err,
    _finite,
    _get_pcb,
    _get_sch,
    _netlist_to_circuit,
    _provider_reachable,
    _round,
    _rpc,
    _rs,
    _run_with_budget,
    _sim_run,
    _simulate_advanced,
    _summarize_ac,
    _summarize_sweep,
    _summarize_sweep_steps,
    _summarize_tran,
    netlist_mod,
)


# --------------------------------------------------------------------------- impedance helpers
#
# engine.ecad.impedance is C++-ported (core/ecad/impedance.cpp) and dispatched via the
# ecad_impedance RPC (modes: microstrip/stripline/coplanar/diff_pair/track). The two pure
# doc-walking helpers it used client-side (_stackup_geometry, _track_length_mm) have no
# per-helper RPC, so they are ported verbatim here.


def _finite_float(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric, not boolean")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def _copper_thickness(layer: dict, default: float = 0.035) -> float:
    raw = layer.get("thickness")
    value = default if raw is None else _finite_float(raw, "copper thickness")
    if value <= 0:
        raise ValueError("copper thickness must be > 0 mm")
    return value


def _validate_stackup_for_sim(board: dict) -> list[dict]:
    """Return a physically usable ordered stackup or raise before a solve/model call."""
    stackup = board.get("stackup")
    if not isinstance(stackup, dict):
        raise ValueError("board stackup must be an object")
    layers = stackup.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("board stackup.layers must be a non-empty ordered list")
    if not all(isinstance(layer, dict) for layer in layers):
        raise ValueError("every stackup layer must be an object")
    ids = [str(layer.get("id") or "") for layer in layers]
    if any(not layer_id for layer_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("stackup layer ids must be non-empty and unique")
    copper = [layer for layer in layers if layer.get("type") == "copper"]
    declared = stackup.get("copper_layers")
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 2:
        raise ValueError("stackup.copper_layers must be an integer >= 2 for simulation")
    if len(copper) != declared:
        raise ValueError(
            f"stackup declares {declared} copper layers but layers contains {len(copper)}")
    for layer in layers:
        thickness = _finite_float(layer.get("thickness"), f"stackup layer {layer['id']} thickness")
        if thickness <= 0:
            raise ValueError(f"stackup layer {layer['id']} thickness must be > 0 mm")
        if layer.get("type") != "copper":
            er = _finite_float(layer.get("er"), f"stackup dielectric {layer['id']} er")
            if er < 1.0:
                raise ValueError(f"stackup dielectric {layer['id']} er must be >= 1")
    board_thickness = _finite_float(stackup.get("thickness"), "stackup thickness")
    if board_thickness <= 0:
        raise ValueError("stackup thickness must be > 0 mm")
    layer_total = sum(float(layer["thickness"]) for layer in layers)
    tolerance_mm = max(0.05, board_thickness * 0.05)
    if abs(layer_total - board_thickness) > tolerance_mm:
        raise ValueError(
            f"stackup layer thicknesses total {layer_total:.4g} mm but board thickness "
            f"is {board_thickness:.4g} mm")
    return layers


def _stackup_geometry(board: dict, layer: str) -> dict:
    """Resolve a transmission-line geometry from an explicit ordered stackup.

    The nearest copper layer(s) are exposed because the closed-form model assumes
    they are continuous reference planes; it cannot infer plane continuity from routing.
    """
    layers = _validate_stackup_for_sim(board)
    copper_idx = [i for i, ly in enumerate(layers) if ly.get("type") == "copper"]
    pos = next((i for i, ly in enumerate(layers)
                if ly.get("id") == layer and ly.get("type") == "copper"), None)
    if pos is None:
        raise ValueError(f"track layer {layer!r} is not present as copper in stackup.layers")
    t = _copper_thickness(layers[pos])
    is_outer = pos in (copper_idx[0], copper_idx[-1])

    def _diel(i: int) -> dict:
        if not (0 <= i < len(layers)) or layers[i].get("type") == "copper":
            raise ValueError("signal copper must be separated from its reference copper by dielectric")
        return layers[i]

    def _reference(i: int) -> str:
        if not (0 <= i < len(layers)) or layers[i].get("type") != "copper":
            raise ValueError("no adjacent copper reference plane beyond the dielectric")
        return str(layers[i]["id"])

    def _dielectric_values(rec: dict) -> tuple[float, float]:
        h = _finite_float(rec.get("thickness"), f"dielectric {rec['id']} thickness")
        er = _finite_float(rec.get("er"), f"dielectric {rec['id']} er")
        if h <= 0 or er < 1:
            raise ValueError("dielectric thickness must be > 0 mm and er >= 1")
        return h, er

    if is_outer:
        direction = 1 if pos == copper_idx[0] else -1
        h, er = _dielectric_values(_diel(pos + direction))
        reference = _reference(pos + 2 * direction)
        return {"kind": "microstrip", "h": h, "t": t, "er": er, "ref_planes": 1,
                "reference_layers": [reference]}

    h_above, er_above = _dielectric_values(_diel(pos - 1))
    h_below, er_below = _dielectric_values(_diel(pos + 1))
    h = h_above + h_below
    er = (er_above * h_above + er_below * h_below) / h
    return {"kind": "stripline", "h": h, "t": t, "er": er, "ref_planes": 2,
            "reference_layers": [_reference(pos - 2), _reference(pos + 2)]}


def _track_length_mm(board: dict, net: str | None, layer: str | None) -> float:
    """Sum finite, non-zero route lengths on ``net`` and optional ``layer``."""
    total = 0.0
    for tr in board.get("tracks", []):
        if not isinstance(tr, dict):
            raise ValueError("board tracks must be objects")
        if net is not None and tr.get("net") != net:
            continue
        if layer is not None and tr.get("layer") != layer:
            continue
        if tr.get("kind", "segment") == "arc":
            r = _finite_float(tr.get("radius"), "track arc radius")
            a0 = math.radians(_finite_float(tr.get("a0"), "track arc a0"))
            a1 = math.radians(_finite_float(tr.get("a1"), "track arc a1"))
            length = abs(a1 - a0) * r
        else:
            p1, p2 = tr.get("p1"), tr.get("p2")
            if not (isinstance(p1, list) and len(p1) >= 2
                    and isinstance(p2, list) and len(p2) >= 2):
                raise ValueError("track segment endpoints must be [x, y] points")
            x1 = _finite_float(p1[0], "track p1.x")
            y1 = _finite_float(p1[1], "track p1.y")
            x2 = _finite_float(p2[0], "track p2.x")
            y2 = _finite_float(p2[1], "track p2.y")
            length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0:
            raise ValueError("track geometry must have non-zero length")
        total += length
    return total


def _parallel_segment_spacing(tr_a: dict, tr_b: dict) -> float:
    """Centreline spacing for two overlapping parallel straight segments."""
    if tr_a.get("kind", "segment") != "segment" or tr_b.get("kind", "segment") != "segment":
        raise ValueError("differential impedance requires straight representative segments")
    coords = []
    for name, tr in (("positive", tr_a), ("negative", tr_b)):
        p1, p2 = tr.get("p1"), tr.get("p2")
        if not (isinstance(p1, list) and len(p1) >= 2
                and isinstance(p2, list) and len(p2) >= 2):
            raise ValueError(f"{name} track endpoints must be [x, y] points")
        coords.append(tuple(_finite_float(v, f"{name} track coordinate")
                            for v in (p1[0], p1[1], p2[0], p2[1])))
    ax, ay, bx, by = coords[0]
    cx, cy, dx, dy = coords[1]
    ux, uy = bx - ax, by - ay
    vx, vy = dx - cx, dy - cy
    len_u, len_v = math.hypot(ux, uy), math.hypot(vx, vy)
    if len_u <= 0 or len_v <= 0:
        raise ValueError("differential-pair segments must have non-zero length")
    if abs(ux * vy - uy * vx) / (len_u * len_v) > 1e-3:
        raise ValueError("differential-pair representative segments are not parallel")
    nx, ny = ux / len_u, uy / len_u
    q0 = (cx - ax) * nx + (cy - ay) * ny
    q1 = (dx - ax) * nx + (dy - ay) * ny
    overlap = min(len_u, max(q0, q1)) - max(0.0, min(q0, q1))
    if overlap <= 0:
        raise ValueError("differential-pair representative segments do not overlap")
    spacing = abs(ux * (cy - ay) - uy * (cx - ax)) / len_u
    if spacing <= 0:
        raise ValueError("differential-pair centrelines overlap")
    return spacing


def _track_impedance(session, track_or_net) -> dict:
    """engine.ecad.impedance.track_impedance -> C++ ecad_impedance RPC (mode='track')."""
    return _rpc("ecad_impedance", {"mode": "track", "doc_id": session.doc_id,
                                   "track_or_net": track_or_net})


def _diff_pair_z0(*, w: float, s: float, h: float, t: float, er: float, kind: str) -> dict:
    """engine.ecad.impedance.diff_pair_z0 -> C++ ecad_impedance RPC (mode='diff_pair')."""
    return _rpc("ecad_impedance", {"mode": "diff_pair", "w": w, "s": s, "h": h, "t": t,
                                   "er": er, "kind": kind})

def register_ecad_sim_tools(mcp: FastMCP) -> None:
    """ECAD simulation/analysis MCP tools (split out of ecad_tools.py): ecad_simulate, run_pdn, sweep, fpga_check, impedance checks, run_sicheck, tf, design."""


    # ---------------------------------------------------------------- simulate

    @mcp.tool()
    def ecad_simulate(analysis: str = "op", params: Optional[Union[dict, str]] = None,
                      probes: Optional[Union[list, str]] = None, doc_id: str = "",
                      backend: str = "mna") -> str:
        """Build the netlist -> SPICE circuit -> run the simulator, and return the KEY
        results with a plain-English reading.

        ``backend`` picks the solver:
          'mna'     -- the validated internal MNA solver (DEFAULT, deterministic)
          'ngspice' -- an installed ngspice binary as a subprocess; unlocks vendor
                       .SUBCKT/.MODEL libraries (real op-amps/regulators). If ngspice is
                       NOT installed this degrades honestly to 'mna' and says so in the
                       result's ``backend_note`` (install: dnf install ngspice).
          'auto'    -- mna unless the circuit carries vendor .lib/.subckt attachments
                       AND ngspice is installed.
        Advanced analyses (four/mc/temp/noise) always run on the internal MNA solver.
        The result always carries ``backend`` = the solver that actually ran.

        ``analysis`` is one of:
          'op'    -- DC operating point (no params needed)
          'dc'    -- source sweep {"source":"V1","start":0,"stop":5,"step":0.05}
          'tran'  -- transient {"tstep":1e-6,"tstop":1e-3,"uic":false}
          'ac'    -- frequency response {"sweep":"dec","points":20,"fstart":1,"fstop":1e6}
          'four'  -- Fourier/THD on a transient trace {"f0":1e3,"tstop":5e-3,"probe":"V(OUT)"}
                     -> returns THD% + dominant harmonics; reading: "THD=0.12%, H1=4.99 V"
          'mc'    -- Monte-Carlo tolerance {"runs":50,"probe":"V(OUT)","tol":{"R":0.01}}
                     -> returns mean/std/yield; reading: "mean=2.5V, sigma=0.03V, yield=98%"
          'temp'  -- temperature sweep {"values":[-40,27,85],"inner":"op","probe":"V(OUT)"}
                     -> returns output trend vs temperature
          'noise' -- output noise density {"output":"V(OUT)","fstart":10,"fstop":1e6}
                     -> returns total RMS noise + per-resistor contributions
        If ``probes`` is omitted, defaults to a BOUNDED set: named/OUT nets first
        (and other human net names), then a hard cap of ~12 node voltages, plus
        I(V*) for every voltage source. Pass an explicit probes list for full
        coverage. Returns op node voltages, or for tran/ac a compact summary
        (final value / -3 dB point — NEVER per-timestep arrays) plus a sentence
        like 'V(OUT) settles to 2.49 V'.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        # LIVE CRASH / SILENT-DISCARD classes: `params` used to reach `_build_analysis`
        # (which does `.get()` on it) as a bare string — 'str' object has no attribute
        # 'get' — and a stringified `probes` list used to be handed to `list(probes)`,
        # which iterates a STRING CHARACTER BY CHARACTER, silently building a garbage
        # probe list with no error at all.
        try:
            if params is not None:
                params = require_mapping(
                    params, "params",
                    "an analysis-params dict, e.g. {'tstep':1e-6,'tstop':1e-3}")
            if probes is not None:
                probes = require_list(
                    probes, "probes",
                    'a list of probe names, e.g. ["V(OUT)", "I(V1)"]')
                if any(not isinstance(probe, str) or not probe.strip() for probe in probes):
                    raise ValueError("probes must contain non-empty strings")
        except (TypeError, ValueError) as e:
            return _err(f"invalid simulation setup: {e}")
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        circuit, warns = _netlist_to_circuit(nl)  # C++ sim_netlist_to_circuit RPC
        # tran/four honor per-symbol source waveforms (SIN/PULSE/PWL); every analysis
        # gets transistor polarity restored (see _apply_sim_source_params).
        _apply_sim_source_params(doc, circuit, warns,
                                 apply_waveforms=analysis in ("tran", "four"))
        if not circuit.get("devices"):
            return _err("nothing to simulate — the netlist has no simulatable devices",
                        hint="Place parts with sim models (R/C/L/V/I/D/Q) and give them VALUES "
                             "(a resistor needs '1k', a source '5'); wire them into a circuit.",
                        next_actions=["ecad_find_part('voltage')  # add a V source",
                                      "ecad_status()"],
                        warnings=warns)

        # default probes = named/OUT nets first, then a bounded count (never dump
        # every anonymous N$ node on a large netlist).
        nodes = [n for n in circuit.get("nodes", []) if str(n) not in ("0", "gnd", "GND")]
        if probes:
            probe_list = list(probes)
        else:
            probe_list = _rs.select_default_probes(nodes, circuit.get("devices") or [])

        try:
            an = _build_analysis(analysis, params or {}, circuit)
        except OpError as e:
            return _err(e.message,
                        hint="Use finite values with positive time/frequency steps and ordered bounds.")
        if an is None:
            return _err(f"unknown analysis {analysis!r}",
                        hint="Use one of: 'op', 'dc', 'tran', 'ac', 'four', 'mc', 'temp', 'noise'.",
                        next_actions=["ecad_simulate('op')"])

        # Advanced analyses: four/mc/temp/noise each wrap or extend an inner analysis. These are the
        # heaviest sim paths (Monte-Carlo runs many solves) — budget-guard so an overrun never times
        # out the transport and kills the server (Gap (b)).
        if an["type"] in ("four", "mc", "temp", "noise"):
            if (backend or "mna").lower() in ("ngspice", "auto"):
                warns.append(f"{an['type']} always runs on the internal MNA solver — "
                             "the ngspice backend covers op/dc/tran/ac/tf only.")
            _autosave_all_open_ecad()
            status, val = _run_with_budget(
                lambda: _simulate_advanced(session, circuit, an, probe_list, warns))
            if status == "timeout":
                return _budget_timeout_err(
                    f"{an['type']} analysis",
                    simplify=("Reduce the work: fewer Monte-Carlo runs, fewer temperature points, a "
                              "shorter tstop / coarser tstep, or a narrower frequency span."),
                    next_actions=["ecad_simulate('op')  # a single fast operating point",
                                  "ecad_status()"],
                    analysis=an["type"])
            if status == "error":
                return _err(f"{an['type']} analysis failed: "
                            f"{type(val).__name__}: {val}",
                            analysis=an["type"])
            return val

        # AC needs a small-signal stimulus. The structured netlist carries only DC
        # values, so unless a source already has an AC magnitude (an ecad_design
        # filter now persists one) an AC sweep would read a flat zero at every
        # frequency (Bug E-1). Honor an explicit ac_source/ac_mag in params; else
        # auto-drive a source with AC=1 and SAY SO, so the agent never silently
        # gets zero and has no idea why.
        if an["type"] == "ac":
            p = params or {}

            def _ac_mag(d: dict) -> float:
                try:
                    value = float(((d.get("params") or {}).get("ac") or {}).get("mag", 0.0)
                                  or 0.0)
                    return value if math.isfinite(value) else 0.0
                except (TypeError, ValueError):
                    return 0.0

            vsrcs = [d for d in circuit["devices"] if d.get("kind") == "V"]
            try:
                want_mag = _finite_float(p.get("ac_mag", 1.0), "ac_mag")
            except ValueError as e:
                return _err(f"invalid AC setup: {e}")
            if want_mag <= 0:
                return _err("invalid AC setup: ac_mag must be > 0")
            want_src = p.get("ac_source") or p.get("source")
            if not vsrcs:
                return _err("AC sweep has no independent voltage source to drive",
                            hint="Add a V source and wire it to the input.")
            if want_src:
                tgt = next((d for d in vsrcs if d.get("name") == want_src), None)
                if tgt is None:
                    return _err(f"AC source {want_src!r} was not found",
                                hint=f"Available voltage sources: "
                                     f"{[d.get('name') for d in vsrcs]}")
                tgt.setdefault("params", {})["ac"] = {"mag": want_mag, "phase": 0.0}
            elif not any(_ac_mag(d) > 0 for d in vsrcs):
                tgt = vsrcs[0]
                tgt.setdefault("params", {})["ac"] = {"mag": want_mag, "phase": 0.0}
                warns.append(
                    f"no source carried an AC magnitude — auto-drove {tgt['name']} with AC={want_mag} "
                    f"so the sweep has a stimulus. Pass params={{'ac_source':'V1','ac_mag':1}} to set it explicitly.")

        study = {"analyses": [an], "probes": probe_list}
        _autosave_all_open_ecad()
        status, result = _run_with_budget(lambda: _sim_run(circuit, study, backend=backend))
        if status == "timeout":
            return _budget_timeout_err(
                f"{an['type']} simulation",
                simplify=("Simplify the circuit (fewer nodes) or the analysis — for tran shorten "
                          "tstop / coarsen tstep, for ac narrow the frequency span, or just run "
                          "'op'."),
                next_actions=["ecad_simulate('op')  # a single fast operating point",
                              "ecad_status()"],
                analysis=an["type"])
        if status == "error":
            return _err(f"{an['type']} simulation failed: "
                        f"{type(result).__name__}: {result}",
                        analysis=an["type"])
        conv = result.get("convergence", {})
        converged = bool(conv.get(an["type"], False))

        if result.get("backend_note"):
            warns.append(result["backend_note"])
        payload: dict = {
            "ok": converged, "doc_id": session.doc_id, "analysis": an["type"],
            "backend": result.get("backend", "mna"),
            "converged": converged,
            "device_count": len(circuit["devices"]),
            "nodes": nodes, "probes": probe_list,
            "warnings": warns + ([] if converged else
                                 [f"{an['type']} did not converge — see sim_log"]),
            "sim_log": result.get("log", [])[-6:],
        }
        if not converged:
            payload["error"] = f"{an['type']} analysis did not converge"

        reading_lines: list[str] = []
        if an["type"] == "op":
            op = result.get("op", {})
            payload["op"] = {k: _round(v) for k, v in op.items()}
            for k, v in sorted(op.items()):
                if k.startswith("V(") and _finite(v):
                    reading_lines.append(f"{k} = {_round(v)} V")
            for k, v in sorted(op.items()):
                if k.startswith("I(") and _finite(v):
                    reading_lines.append(f"{k} = {_eng(v)}A")
        elif an["type"] == "dc":
            payload["dc"] = _summarize_sweep(result.get("dc", {}), reading_lines, unit="V")
        elif an["type"] == "tran":
            payload["tran"] = _summarize_tran(result.get("tran", {}), reading_lines)
        elif an["type"] == "ac":
            payload["ac"] = _summarize_ac(result.get("ac", {}), reading_lines)

        reading = ("; ".join(reading_lines) if reading_lines else
                   "no finite probe values — check connectivity / source values")
        payload["reading"] = reading
        payload["hint"] = (
            (f"{an['type'].upper()} solved: {reading}." if converged else
             f"{an['type'].upper()} did NOT converge: {reading}. Check that every node has a DC "
             "path to ground and that source/part values are sane (run ecad_run_erc).")
        )
        payload["next_actions"] = (
            ["ecad_status()  # the result matches the topology you built"] if converged else
            ["ecad_run_erc()  # find the connectivity/driver problem",
             "ecad_status()  # confirm wiring"]
        )
        return _dumps(payload)



    # ---------------------------------------------------------------- PDN

    @mcp.tool()
    def ecad_run_pdn(net: str = "", vsrc: float = 0.0,
                     source_refs: Optional[Union[list, str]] = "",
                     sink_overrides: Optional[Union[dict, str]] = None,
                     resolution_mm: float = 0.0, j_limit_a_mm2: float = 0.0,
                     doc_id: str = "") -> str:
        """DC power-delivery (IR-drop) analysis of the active board's power-net copper.

        Meshes the net's pours/tracks/pads into a 2.5D resistor grid (sheet resistance
        from the stackup copper thickness) with via barrels as annular resistors, then
        does a sparse DC solve. Sources = regulator/connector output pads (auto-detected
        from ``power_out`` symbol pins, or pass ``source_refs='U3,J1'``) held at ``vsrc``
        (default: the rail's ``voltage_v_nom`` metadata, else parsed from names like
        '+3V3'). Sinks = per-component supply currents from the aicad.boardphys/1 rail
        metadata; override/provide with ``sink_overrides='{"U2": 0.35}'`` (amps).

        ``net`` may be one net name, a comma list, or empty = every net with a detected
        source (GND excluded). Reports per-component voltage drop vs the rail's
        voltage_v_min tolerance, current-density hotspots vs ``j_limit_a_mm2`` (default
        30 A/mm²), open PDN paths, and refs with unknown current draw (honest coverage
        gap — metadata missing is never modelled as 0 A).

        Also exports the per-element I²R dissipation map as the ``pdn`` extension of the
        aicad.boardphys/1 sidecar (contracts/ecad/boardphys.md) — the trace-heating
        input for the ECAD→thermal handoff: merged into the existing
        ``board.step.boardphys.json`` when ecad_to_3d already wrote one, else written to
        ``pdn.boardphys.json`` in the board's export dir.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="PDN analysis needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        try:
            _validate_stackup_for_sim(doc)
            vsrc_value = _finite_float(vsrc, "vsrc")
            resolution_value = _finite_float(resolution_mm, "resolution_mm")
            j_limit_value = _finite_float(j_limit_a_mm2, "j_limit_a_mm2")
            if resolution_value < 0:
                raise ValueError("resolution_mm must be 0 (automatic) or > 0 mm")
            if j_limit_value < 0:
                raise ValueError("j_limit_a_mm2 must be 0 (default) or > 0 A/mm^2")
            raw_overrides = (require_mapping(
                sink_overrides, "sink_overrides",
                "a ref->amps object, e.g. {'U2': 0.35}") if sink_overrides else {})
            overrides = {}
            for raw_ref, raw_current in raw_overrides.items():
                ref = str(raw_ref).strip()
                current = _finite_float(raw_current, f"sink_overrides[{ref!r}]")
                if not ref or current < 0:
                    raise ValueError("sink override refs must be non-empty and currents >= 0 A")
                overrides[ref] = current
            if isinstance(source_refs, list):
                srcs = [str(ref).strip() for ref in source_refs if str(ref).strip()] or None
            elif isinstance(source_refs, str):
                srcs = [ref.strip() for ref in source_refs.split(",") if ref.strip()] or None
            elif source_refs is None:
                srcs = None
            else:
                raise ValueError("source_refs must be a comma-separated string or list of refs")
            if not isinstance(net, str):
                raise ValueError("net must be a net name or comma-separated string")
            nets = list(dict.fromkeys(name.strip() for name in net.split(",") if name.strip()))
        except (TypeError, ValueError) as e:
            return _err(f"invalid PDN setup: {e}",
                        hint="Use finite SI-facing values: volts, amps, mm, and A/mm^2.")
        try:
            from services.ecad import pdn as pdn_mod  # STAYS: no C++ counterpart; lazy import (pulls scipy)
        except Exception as e:  # noqa: BLE001
            return _err(f"PDN engine unavailable: {e}",
                        hint="numpy/scipy are required for the sparse solve.",
                        available=False)
        if not nets:
            nets = sorted({n.get("name") for n in doc.get("nets", [])
                           if n.get("name") and n.get("name") != "GND"
                           and pdn_mod._auto_sources(doc, n.get("name"))})
            if not nets:
                return _err("no power net with a detectable source (power_out pin) — "
                            "pass net='+5V' and source_refs='<regulator/connector ref>'.",
                            next_actions=["ecad_run_pdn(net='+5V', source_refs='J1')"])
        declared_nets = {str(item.get("name")) for item in doc.get("nets", [])
                         if isinstance(item, dict) and item.get("name")}
        unknown_nets = [name for name in nets if name not in declared_nets]
        if unknown_nets:
            return _err("PDN net(s) are not declared on this board: "
                        + ", ".join(unknown_nets),
                        failed_nets=[{"net": name, "error": "net is not declared"}
                                     for name in unknown_nets],
                        hint="Use ecad_status() to inspect board net names.",
                        next_actions=["ecad_status()"])
        if srcs:
            placed_refs = {str(fp.get("ref")) for fp in doc.get("footprints", [])
                           if isinstance(fp, dict) and fp.get("ref")}
            missing_refs = [ref for ref in srcs if ref not in placed_refs]
            if missing_refs:
                return _err("PDN source_refs are not placed on this board: "
                            + ", ".join(missing_refs),
                            hint="source_refs must name placed regulator/connector footprints.",
                            next_actions=["ecad_status()"])
        _autosave_all_open_ecad()

        def _solve_all():
            return [pdn_mod.solve_pdn(
                doc, n,
                vsrc=(vsrc_value if vsrc_value != 0.0 else None),
                source_refs=srcs,
                sink_overrides=overrides,
                resolution_mm=(resolution_value if resolution_value > 0 else None),
                j_limit_a_mm2=(j_limit_value if j_limit_value > 0
                               else pdn_mod.DEFAULT_J_LIMIT_A_MM2),
            ) for n in nets]

        status, results = _run_with_budget(_solve_all)
        if status == "timeout":
            return _budget_timeout_err(
                "PDN solve",
                simplify="The copper mesh is large — pass resolution_mm=0.5 (or 1.0) to "
                         "coarsen, or solve one net at a time (net='+5V').",
                next_actions=["ecad_run_pdn(net='+5V', resolution_mm=0.5)"])
        if status == "error":
            return _err(f"PDN solve failed: {type(results).__name__}: {results}",
                        hint="Check the net is routed (ecad_status) and re-run.")

        ok_results = [r for r in results if r.get("ok")]
        failed = [{"net": r.get("net"), "error": r.get("error")}
                  for r in results if not r.get("ok")]
        if not ok_results:
            # every requested net failed — return a real error (previously this fell
            # through to ok=False with NO 'error' key and a "PDN clean on 0 net(s)"
            # verdict, which read as a pass to a weak model).
            return _err("PDN solve failed for every requested net: "
                        + "; ".join(f"{f['net']}: {f['error']}" for f in failed),
                        failed_nets=failed,
                        hint="Check the net names and that each has copper + a source "
                             "(ecad_status lists the board's nets).",
                        next_actions=["ecad_status()",
                                      "ecad_run_pdn(net='+5V', source_refs='J1')"])

        # ---- persist the I2R map as the boardphys `pdn` sidecar extension
        sidecar_path = None
        sidecar_warnings: list[str] = []
        if ok_results:
            out_dir = _default_out_dir(doc.get("name", "board"))
            existing = os.path.join(str(out_dir), "board.step.boardphys.json")
            try:
                sidecar = None
                if os.path.exists(existing):
                    with open(existing, encoding="utf-8") as fh:
                        candidate = json.load(fh)
                    candidate_board = candidate.get("board") if isinstance(candidate, dict) else None
                    identity_matches = (
                        candidate.get("schema") == "aicad.boardphys/1"
                        and isinstance(candidate_board, dict)
                        and candidate_board.get("id") == doc.get("id"))
                    if identity_matches:
                        sidecar = candidate
                        sidecar_path = existing
                    else:
                        sidecar_warnings.append(
                            "ignored stale board.step.boardphys.json: board id/schema "
                            "does not match the active board")
                if sidecar is None:
                    sidecar = {
                        "schema": "aicad.boardphys/1",
                        "generated_by": "services.ecad.pdn.solve_pdn",
                        "board": {"name": doc.get("name"), "id": doc.get("id"),
                                  "thickness_mm": doc.get("stackup", {}).get("thickness")},
                        "components": [],
                        "note": "PDN-only sidecar — run ecad_to_3d for the full "
                                "component power/thermal map.",
                    }
                    os.makedirs(str(out_dir), exist_ok=True)
                    sidecar_path = os.path.join(str(out_dir), "pdn.boardphys.json")
                pdn_mod.attach_pdn(sidecar, ok_results)
                with open(sidecar_path, "w", encoding="utf-8") as fh:
                    json.dump(sidecar, fh, indent=1)
            except Exception as e:  # noqa: BLE001 — sidecar failure must not void the solve
                sidecar_warnings.append(f"boardphys pdn sidecar write failed: {e}")
                sidecar_path = None

        violations = [f"[{r['net']}] {v}" for r in ok_results for v in r["violations"]]
        unknown = sorted({u for r in ok_results for u in r["unknown_sinks"]})
        worst = None
        for r in ok_results:
            w = r.get("worst_drop")
            if w and w.get("drop_v") is not None and (
                    worst is None or w["drop_v"] > worst["drop_v"]):
                worst = {**w, "net": r["net"]}
        complete = not failed and not unknown
        analysis_pass = complete and not violations
        if failed:
            verdict = (f"INCOMPLETE: {len(failed)} of {len(results)} requested net(s) failed; "
                       f"{len(violations)} violation(s) in the solved subset.")
        elif unknown:
            verdict = (f"INCOMPLETE: current draw is unknown for {len(unknown)} component(s); "
                       "zero-current assumptions cannot support a clean PDN verdict.")
        elif violations:
            verdict = f"FAIL: {len(violations)} PDN violation(s) across {len(ok_results)} net(s)."
        else:
            verdict = f"PASS: PDN clean on {len(ok_results)} net(s) with complete load coverage."
        if worst and worst.get("drop_v") is not None:
            verdict += (f" worst drop {worst['drop_v'] * 1000:.1f} mV at "
                        f"{worst['ref']} ({worst['net']}).")
        summaries = [{k: r[k] for k in ("net", "vsrc", "sources", "resolution_mm",
                                        "n_nodes", "drops", "worst_drop", "max_j_a_mm2",
                                        "j_limit_a_mm2", "hotspots", "violations",
                                        "unknown_sinks", "elapsed_s")}
                     for r in ok_results]
        payload = {
            "ok": not failed, "pass": analysis_pass, "complete": complete,
            "doc_id": session.doc_id, "revision": session.revision,
            "verdict": verdict,
            "nets": summaries,
            "failed_nets": failed,
            "violations": violations,
            "unknown_current_refs": unknown,
            "boardphys_path": sidecar_path,
            "warnings": sidecar_warnings + (
                [f"current draw unknown for {', '.join(unknown)} — drops there assume "
                 "0 A from them; add rail metadata or sink_overrides"] if unknown else []),
            "hint": verdict + (f" I2R heat map -> {sidecar_path} (pdn block, for the "
                               "thermal handoff)." if sidecar_path else ""),
            "next_actions": (
                ["Fix failed net setup/current coverage, then re-run ecad_run_pdn()."]
                if not complete else
                ["Widen the flagged copper / add a pour or stitching vias, then re-run "
                 "ecad_run_pdn()."] if violations else
                ["ecad_to_3d()  # export STEP + full boardphys sidecar for thermal"]),
        }
        if failed:
            payload["error"] = "one or more requested PDN nets did not solve"
        return _dumps(payload)



    # ---------------------------------------------------------------- parameter sweep

    @mcp.tool()
    def ecad_sweep(param: str, values: Optional[Union[list, str]] = None,
                   analysis: str = "op", probe: str = "", doc_id: str = "") -> str:
        """Parameter sweep: re-simulate the active schematic once per value of ``param`` and report
        how the output moves (a SPICE ``.step``).

        ``param`` is a device name (e.g. 'R1', 'V1' — sets its value) or a circuit .param. ``values``
        is the list to sweep (e.g. [1000, 2000, 5000]); start/stop/step is not exposed here — pass an
        explicit list. ``analysis`` is 'op'/'dc'/'tran'/'ac' (default 'op'). ``probe`` picks the
        output trace to summarize (default: the first non-ground node voltage). Returns the swept
        trend in plain English ('V(OUT) rises 2.5 V -> 4.0 V as R1 goes 1k -> 5k').
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Sweeps run on a schematic.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        try:
            if isinstance(values, str) and values.strip():
                values = require_list(
                    values, "values",
                    "a list of sweep values, e.g. [1000, 2000, 5000]")
            if not isinstance(values, list) or len(values) < 2:
                raise ValueError("values must contain at least two sweep points")
            if not isinstance(param, str) or not param.strip():
                raise ValueError("param must be a non-empty device or .param name")
            if analysis not in ("op", "dc", "tran", "ac"):
                raise ValueError("analysis must be one of 'op', 'dc', 'tran', or 'ac'")
            if probe and (not isinstance(probe, str) or not probe.strip()):
                raise ValueError("probe must be a non-empty string")
            fvals = [_finite_float(value, f"values[{index}]")
                     for index, value in enumerate(values)]
            if len(set(fvals)) != len(fvals):
                raise ValueError("sweep values must be distinct")
        except (TypeError, ValueError) as e:
            return _err(f"invalid parameter sweep: {e}",
                        hint="Pass finite explicit points, e.g. "
                             "ecad_sweep('R1', [1000, 2000, 5000]).",
                        next_actions=["ecad_sweep('R1', [1000, 2000, 5000])"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        circuit, warns = _netlist_to_circuit(nl)  # C++ sim_netlist_to_circuit RPC
        _apply_sim_source_params(doc, circuit, warns,
                                 apply_waveforms=analysis in ("tran", "four"))
        if not circuit.get("devices"):
            return _err("nothing to sweep — the netlist has no simulatable devices",
                        hint="Place R/C/L/V/I/D parts with VALUES and wire them up first.",
                        next_actions=["ecad_status()"], warnings=warns)

        nodes = [n for n in circuit.get("nodes", []) if str(n) not in ("0", "gnd", "GND")]
        probe_list = [probe] if probe else [f"V({n})" for n in nodes]
        try:
            an = _build_analysis(analysis, {}, circuit)
        except OpError as e:
            return _err(e.message)
        if an is None:
            return _err(f"unknown analysis {analysis!r}",
                        hint="Use one of: 'op', 'dc', 'tran', 'ac'.",
                        next_actions=["ecad_sweep('R1', [1000, 2000])"])
        study = {"analyses": [an], "probes": probe_list,
                 "step": {"param": param, "values": fvals}}
        status, result = _run_with_budget(lambda: _sim_run(circuit, study))
        if status == "timeout":
            return _budget_timeout_err(
                "parameter sweep",
                simplify="Use fewer values or a cheaper inner analysis.",
                next_actions=["ecad_sweep('R1', [1000, 2000])"])
        if status == "error":
            return _err(f"parameter sweep failed: {type(result).__name__}: {result}")
        step = result.get("step", {})
        runs = step.get("runs", [])
        trend, sweep_table, chosen = _summarize_sweep_steps(
            runs, an["type"], probe or None, probe_order=probe_list)
        converged = bool(result.get("convergence", {}).get("step", False))
        complete = (converged and len(runs) == len(fvals)
                    and len(sweep_table) == len(fvals) and bool(chosen)
                    and all(_finite(row.get(chosen)) for row in sweep_table))
        return _dumps({
            "ok": complete, "doc_id": session.doc_id, "analysis": an["type"],
            **({} if complete else
               {"error": "parameter sweep did not converge at every requested finite point"}),
            "param": param.strip(), "values": fvals, "converged": converged,
            "complete": complete,
            "probe": chosen, "sweep": sweep_table,
            "trend": trend,
            "warnings": warns + ([] if complete else
                                 ["one or more sweep points did not converge or map to a finite probe"]),
            "hint": (f"Swept {param} over {len(fvals)} value(s): {trend}" if trend else
                     f"Swept {param} but found no finite {chosen or 'probe'} value — "
                     "check connectivity / values (ecad_run_erc)."),
            "next_actions": ["ecad_simulate('op')  # inspect one operating point in detail",
                             "ecad_status()"],
        })



    # ---------------------------------------------------------------- FPGA

    @mcp.tool()
    def ecad_fpga_check(ref: str = "", io: Optional[Union[dict, str]] = None, doc_id: str = "") -> str:
        """Bank-aware FPGA design checks on the active schematic.

        Finds every placed FPGA (a symbol whose value/libid matches an
        ``aicad.fpga_device/1`` seed — 'FPGA:iCE40UP5K-SG48' ships full; ECP5 CABGA256
        and XC7A35T-CPG236 ship as partial seeds — or any symbol tagged with fields
        ``{"fpga_device": "<device>"}``) and validates:

        1. bank VCCIO: every IO pin of a bank compatible with ONE VCCIO (inferred from the
           net on the bank's VCCIO pins + each net's IOSTANDARD or voltage-tagged name);
        2. differential nets (``X_P``/``X_N``, ``X+``/``X-``) on TRUE pair pins, right polarity;
        3. clock-named nets (``*CLK*``) on clock-capable pins (GBIN/MRCC/SRCC);
        4. required config/JTAG pins wired per datasheet (e.g. iCE40 CRESET_B).

        ``ref`` limits the check to one component. ``io`` optionally maps net name ->
        {"iostandard": "LVCMOS18"} to declare IO standards. Partial device seeds SKIP the
        checks they lack data for (reported as warnings — never guessed). Returns
        violations [{check, ref, message, ...}] and a per-device coverage summary.
        Constraint files for the HDL flow: ecad_export('constraints').
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="FPGA checks run on a schematic.",
                        next_actions=["ecad_new_schematic('myfpga')"])
        if io is not None:
            io = require_mapping(io, "io",
                                 'a {net_name: {"iostandard": "LVCMOS18"}} map')
        try:
            from services.ecad import fpga  # STAYS: no C++ counterpart
        except Exception as e:  # noqa: BLE001
            return _err(f"fpga module unavailable ({type(e).__name__}: {e})")
        nl = netlist_mod.build_netlist(session.doc)
        res = fpga.fpga_check(nl, ref=ref or "", io=io or None)
        if not res["devices"]:
            return _err("no FPGA found on the schematic",
                        hint="Place 'FPGA:iCE40UP5K-SG48' or tag a symbol with "
                             "fields={'fpga_device': '<device>'}; known devices: "
                             + ", ".join(d["device"] for d in fpga.load_devices().values()),
                        next_actions=["ecad_place_symbol('FPGA:iCE40UP5K-SG48', 100, 100)"])
        n = len(res["violations"])
        return _dumps({
            "ok": res["ok"], "doc_id": session.doc_id, "devices": res["devices"],
            "violations": res["violations"], "warnings": res["warnings"],
            "hint": ("FPGA checks clean across "
                     f"{len(res['devices'])} device(s)." if res["ok"] else
                     f"{n} FPGA violation(s) — fix bank/pair/clock/config wiring "
                     "before generating constraints."),
            "next_actions": ["ecad_export('constraints')  # XDC/LPF/CSV pinout",
                             "ecad_run_erc()"],
        })



    # ---------------------------------------------------------------- autonomous design

    @mcp.tool()
    def ecad_design(request: str, cancel=None) -> str:
        """Autonomous ECAD design: natural-language request -> verified schematic + simulation.

        Runs the full deterministic orchestrator pipeline (PLAN -> BUILD -> ERC -> SIMULATE ->
        REPORT) and returns a summary with the doc_id, stage results, ERC counts, and sim
        pass/fail. The model proposes the topology through a tool-free agent-runtime helper
        (an offline-safe provider when offline/local-only is configured); when that step is
        unavailable, slow, or rejected, the closest built-in recipe is used and the reason is
        reported, so the pipeline ALWAYS produces a verified schematic.

        After this call the produced schematic becomes the active one -- ecad_status() and
        ecad_simulate() operate on it immediately.

        ``request`` is a plain-English design description, e.g.
          '5V voltage divider with 2.5V output'
          'LED driver for a red LED from a 3.3V supply'
          'inverting op-amp with gain 10'
        """
        if callable(cancel) and cancel():
            return _err("ecad_design cancelled")
        # Lazy-import so a missing scipy/numpy degrades gracefully.
        try:
            from services import ecad_design as _orch  # type: ignore
        except ImportError as exc:
            return _err(f"orchestrator unavailable ({type(exc).__name__}: {exc})",
                        hint="services.ecad_design could not be imported "
                             "(numpy/scipy may be missing). "
                             "Build the schematic manually with ecad_place_symbol + ecad_wire.",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_find_part('resistor')"])

        # Resolve the helper model step (best-effort; fall back to None -> recipe path).
        # WHY the model path did not run must never be swallowed: a canned recipe standing in
        # for model-authored design is only honest if the caller is told it happened AND why.
        # Online the planner is a tool-free completion through the agent runtime, which owns
        # auth/retry/model; offline/local-only binds an offline-safe provider. Resolution
        # raises when the selection cannot be honoured.
        # An abandoned plan (budget overrun / error) or a host Stop cancels the helper so it
        # never keeps a model request running behind the recipe fallback.
        plan_abandoned = threading.Event()

        def _plan_cancelled() -> bool:
            return plan_abandoned.is_set() or bool(callable(cancel) and cancel())

        provider = None
        provider_setup_err: Exception | None = None
        try:
            from services.agent.providers.chudcode_provider import helper_provider
            provider = helper_provider("ecad_engineer", cancel=_plan_cancelled)
        except Exception as exc:  # noqa: BLE001 -- offline refusal / role resolution / module absent
            provider_setup_err = exc
        provider_absent = provider is None and provider_setup_err is None

        # Fast pre-flight: if the provider points at a LOCAL model server that is down (the most
        # common case — LM Studio/llama.cpp not started), a real provider.complete() would burn
        # ~14 s on connect-retries and overrun the MCP deadline before falling back. A sub-second
        # socket probe lets us skip straight to the instant recipe path instead.
        provider_unreachable = False
        if provider is not None and not _provider_reachable(provider):
            provider_unreachable = True
            provider = None

        # CRITICAL TRANSPORT SAFETY:
        #   1. PLAN (provider.complete) is the only stage that can stall on a remote/local model.
        #   2. BUILD/ERC/SIM create a real EcadSession and mutate _ECAD current_*.
        #   3. A timed-out provider thread MUST NOT keep running the full pipeline — otherwise it
        #      races the recipe fallback and can overwrite current_sch/current after we already
        #      returned a verified recipe schematic (and the next ecad_status sees the wrong doc
        #      or blocks on a still-busy provider).
        # So: budget ONLY the plan; build EXACTLY ONCE on the main thread with a frozen design.
        #
        # BUDGET: plan + recipe-build must finish under the client's MCP tool timeout (~30 s).
        # Default 12 s plan budget leaves ~2 s for the deterministic build (usually <<1 s).
        # Override with CHUDWARE_ECAD_PLAN_BUDGET_S when the client allows a longer call.
        try:
            _PLAN_BUDGET_S = float(os.environ.get("CHUDWARE_ECAD_PLAN_BUDGET_S", "") or 12.0)
        except (TypeError, ValueError):
            _PLAN_BUDGET_S = 12.0
        if not (math.isfinite(_PLAN_BUDGET_S) and _PLAN_BUDGET_S > 0.0):
            _PLAN_BUDGET_S = 12.0

        plan_box: dict = {}
        provider_busy = False
        provider_timed_out = False
        provider_plan_err: Exception | None = None
        # Generation token: a late (abandoned after timeout) plan worker must never
        # populate plan_box after we have already fallen back to the recipe.
        plan_gen = {"v": 0}

        def _plan_worker(gen: int) -> None:
            try:
                dsg, rcp, evs = _orch.plan_design(request, provider=provider)
            except Exception as exc:  # noqa: BLE001
                if plan_gen["v"] == gen:
                    plan_box["err"] = exc
                return
            if plan_gen["v"] != gen:
                return  # abandoned — do not touch plan_box
            plan_box["design"] = dsg
            plan_box["recipe_used"] = rcp
            plan_box["events"] = evs

        # Only ONE provider plan runs at a time (Bug E4): if another ecad_design already holds
        # the provider, this call takes the instant recipe path rather than queueing behind it
        # on a single-threaded local model server (which would overrun the MCP deadline).
        claimed = False
        if provider is not None:
            with _PROVIDER_PLAN_LOCK:
                if _PROVIDER_PLAN_BUSY["v"]:
                    provider_busy = True
                else:
                    _PROVIDER_PLAN_BUSY["v"] = True
                    claimed = True

        if provider is not None and claimed:
            # Budgeted plan ONLY — never build inside the worker.
            my_gen = plan_gen["v"] + 1
            plan_gen["v"] = my_gen
            try:
                st, val = _run_with_budget(lambda: _plan_worker(my_gen),
                                           budget_s=_PLAN_BUDGET_S)
                if st == "timeout":
                    provider_timed_out = True
                    # Invalidate so a late worker cannot fill plan_box after we fall back.
                    plan_gen["v"] = my_gen + 1
                    plan_box.clear()
                elif st == "error":
                    provider_plan_err = val if isinstance(val, Exception) else None
                    plan_gen["v"] = my_gen + 1
                    plan_box.clear()
                elif plan_box.get("err") is not None:
                    provider_plan_err = plan_box["err"]
                    plan_box.clear()
                # st == "done" and no err: plan_box holds design/recipe_used.
            finally:
                # The plan phase is over: a still-running (timed-out) helper stops now.
                plan_abandoned.set()
                with _PROVIDER_PLAN_LOCK:
                    _PROVIDER_PLAN_BUSY["v"] = False
        elif provider is None:
            # Instant recipe plan (no model). Safe on the caller's thread.
            my_gen = plan_gen["v"] + 1
            plan_gen["v"] = my_gen
            _plan_worker(my_gen)
        # provider_busy: leave plan_box empty -> recipe plan below.
        if callable(cancel) and cancel():
            return _err("ecad_design cancelled")

        design = plan_box.get("design")
        recipe_used = plan_box.get("recipe_used")
        # plan_design() NEVER returns design=None — it falls back to the closest recipe
        # internally and reports it via recipe_used. So when a provider DID plan and its
        # proposal was rejected (unparseable / failed validate_design / provider error), the
        # only record was plan_box["events"], which this tool used to discard: the caller saw
        # a bare stop_reason='fallback' with no reason. Carry the planner's own last verdict.
        plan_rejected: str | None = None
        if provider is not None and recipe_used and plan_box.get("events"):
            for _ev in reversed(plan_box["events"]):
                _detail = (_ev.get("detail") if isinstance(_ev, dict)
                           else getattr(_ev, "detail", None))
                if _detail and not str(_detail).startswith("used recipe fallback"):
                    plan_rejected = str(_detail)
                    break
        if design is None:
            # Provider path overran, was busy, or errored — plan via the deterministic recipe.
            # This never touches a provider and is instant.
            try:
                design, recipe_used, _evs = _orch.plan_design(request, provider=None)
            except Exception as exc:  # noqa: BLE001
                return _err(f"ecad_design failed: {type(exc).__name__}: {exc}",
                            hint="Check that services.ecad_design and its deps are available.",
                            next_actions=["ecad_status()"])

        # Snapshot the active-doc pointers BEFORE the single build so a late abandoned
        # provider-plan thread (which can never create a session now) cannot confuse us, and
        # so we only ever adopt the doc we ourselves just produced.
        prior_sch = _ECAD.get("current_sch")
        prior_cur = _ECAD.get("current")

        # ONE build on the caller's thread with the frozen design. No provider re-entry.
        try:
            run = _orch.run_design(
                request, provider=None, design=design, recipe_used=recipe_used)
        except Exception as exc:  # noqa: BLE001
            return _err(f"ecad_design failed: {type(exc).__name__}: {exc}",
                        hint="Check that services.ecad_design and its deps are available.",
                        next_actions=["ecad_status()"])

        doc_id: str | None = run.doc_id

        # Register the produced session as the active schematic so downstream tools work.
        # Guard: only adopt if nothing else claimed current_sch mid-call (monotonic recovery /
        # concurrent design). Our own build always owns a fresh session, so this is the
        # intended common path.
        if doc_id is not None:
            # Always set for the doc we just produced — this is the intentional result of
            # THIS call. A concurrent recover_ecad_autosaved will refuse to overwrite a
            # non-None pointer, so the race is one-way safe.
            _ECAD["current_sch"] = doc_id
            _ECAD["current"] = doc_id

        # Summarise stages concisely.  run.stages is a list[dict] (asdict was called internally).
        stages_raw = run.stages  # type: ignore[attr-defined]
        stage_names = [
            (s.get("stage") if isinstance(s, dict) else getattr(s, "stage", "?"))
            for s in stages_raw
        ]
        erc = run.erc  # type: ignore[attr-defined]
        sim = run.sim  # type: ignore[attr-defined]
        recipe = run.recipe_used  # type: ignore[attr-defined]
        stop = run.stop_reason  # type: ignore[attr-defined]
        ok = bool(run.ok)  # type: ignore[attr-defined]

        # Build a human summary of the sim result.
        sim_line = ""
        if sim:
            probe = sim.get("probe", "")
            got = sim.get("got")
            exp = sim.get("expect")
            passed = sim.get("pass", False)
            if got is not None:
                try:
                    sim_line = (f"{probe}={float(got):.4g} (expect {exp}, "
                                f"{'PASS' if passed else 'MISS'})")
                except (TypeError, ValueError):
                    sim_line = f"{probe}={got} (expect {exp}, {'PASS' if passed else 'MISS'})"
            elif sim.get("log"):
                sim_line = sim["log"][-1] if sim["log"] else ""

        # A fallback is honest only if it can never be read as success. Two rules here:
        #   1. Say plainly that a CANNED built-in recipe stood in for model-authored design.
        #   2. Never claim "still verified" when the run did not verify — ERC errors or a
        #      simulation that missed its target mean this is not a working design.
        verified_note = ("the schematic is still verified" if ok
                         else "and the result did NOT verify — see below")
        recipe_note = ("A CANNED built-in recipe was substituted for model-authored design: "
                       f"{recipe!r}. " if recipe else "")
        causes: list[str] = []
        if sim and not sim.get("pass"):
            causes.append("the simulation did not meet its target")
        if erc and erc.get("errors", 0) > 0:
            causes.append(f"{erc.get('errors')} ERC error(s) remain")
        verdict = ("" if ok else
                   " NOT VERIFIED — do not treat this as a working design: "
                   + ("; ".join(causes) if causes else "the run did not confirm") + ".")

        busy_note = (
            "Another ecad_design is already using the model server — used the built-in "
            f"recipe instead ({verified_note}). "
            if provider_busy else "")
        hint = ((f"The configured model server did not plan within {_PLAN_BUDGET_S:.0f}s — used "
                 f"the built-in recipe instead (it is loading/slow; {verified_note}). "
                 if provider_timed_out else "")
                + busy_note
                + ("The configured model server is not reachable — used the built-in recipe "
                   f"instead ({verified_note}). "
                   if provider_unreachable else "")
                + ("No model provider is configured, so the model design path never ran — "
                   "used the built-in recipe instead. "
                   if provider_absent else "")
                + (f"The model provider could not be resolved "
                   f"({type(provider_setup_err).__name__}: {provider_setup_err}) — used the "
                   "built-in recipe instead. "
                   if provider_setup_err is not None else "")
                + (f"Provider plan error ({type(provider_plan_err).__name__}: {provider_plan_err}) "
                   "— used the built-in recipe instead. "
                   if provider_plan_err is not None else "")
                + (f"The model's own plan was discarded ({plan_rejected}) — used the built-in "
                   "recipe instead. " if plan_rejected else "")
                + f"Design '{request}' produced with stop_reason={stop!r}. "
                + recipe_note
                + (f"ERC: {erc.get('errors', 0)} error(s), "
                   f"{erc.get('warnings', 0)} warning(s). " if erc else "")
                + (f"Sim: {sim_line}." if sim_line else "")
                + verdict
                + (" Use ecad_status() to inspect the schematic."
                   if doc_id else " No document was produced."))

        next_actions: list[str] = []
        if doc_id:
            next_actions.append("ecad_status()  # inspect the generated schematic")
            next_actions.append("ecad_simulate('op')  # re-run simulation or change params")
        if erc and erc.get("errors", 0) > 0:
            next_actions.insert(0,
                "ecad_run_erc()  # investigate the ERC errors in the generated schematic")
        if not next_actions:
            next_actions = ["ecad_status()", "ecad_simulate('op')"]

        # Silence unused-var lint for the intentional prior snapshot (documents the race fence).
        _ = (prior_sch, prior_cur)

        return _dumps({
            "ok": ok,
            "doc_id": doc_id,
            "stop_reason": stop,
            "recipe_used": recipe,
            "stages": stage_names,
            "ops_applied": run.ops_applied,  # type: ignore[attr-defined]
            "erc": erc,
            "sim": sim,
            "warnings": [],
            "hint": hint,
            "next_actions": next_actions,
            "provider_timed_out": bool(provider_timed_out),
            "provider_busy": bool(provider_busy),
            "provider_unreachable": bool(provider_unreachable),
            # Machine-readable companions to the hint clauses above, so a caller can branch
            # on WHY the model path was skipped instead of parsing prose.
            "provider_absent": bool(provider_absent),
            "provider_setup_error": (None if provider_setup_err is None else
                                     f"{type(provider_setup_err).__name__}: {provider_setup_err}"),
            "plan_rejected": plan_rejected,
        })



    # ---------------------------------------------------------------- signal-integrity

    @mcp.tool()
    def ecad_check_impedance(net_or_ref: str = "", target: float = 50.0,
                             doc_id: str = "") -> str:
        """Compute the characteristic impedance (Z0) and one-way propagation delay for a routed
        trace on the active board, and compare it to a target impedance.

        ``net_or_ref`` may be a net name ('CLK', 'USB_D+') or a track id ('t3'). If omitted,
        the tool scans ALL routed nets and summarises which ones are likely controlled-impedance
        candidates (width >= 0.1 mm on an outer layer, or any inner layer).

        Returns a plain-English reading like:
          "Net CLK on F.Cu: Z0=52.1 ohm (target 50.0, +4.2%) -- within 10% tolerance;
           delay=6.7 ps/mm, length=23.4 mm, one-way delay=157 ps"
        plus ``next_actions`` to widen/narrow the trace if the target is missed.

        Impedance model: outer copper -> IPC-2141A microstrip; inner copper -> stripline.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Impedance check needs a board with routed tracks.",
                        next_actions=["ecad_new_pcb('myboard')", "ecad_autoroute()"])
        doc = session.doc
        tracks = doc.get("tracks", [])
        if not tracks:
            return _err("no routed tracks on this board",
                        hint="Route the board first (ecad_autoroute or ecad_update_from_sch).",
                        next_actions=["ecad_autoroute()", "ecad_update_from_sch()"])
        try:
            target = _finite_float(target, "target")
            if target <= 0:
                raise ValueError("target must be > 0 ohm")
            if not isinstance(net_or_ref, str):
                raise ValueError("net_or_ref must be a net name or track id string")
            _validate_stackup_for_sim(doc)
        except (TypeError, ValueError) as e:
            return _err(f"invalid impedance setup: {e}",
                        hint="Define an ordered physical stackup and use a positive-ohm target.")

        tol_pct = 10.0  # +/-10% is the standard controlled-impedance fab tolerance

        def _pct(z0: float) -> float:
            return 100.0 * (z0 - target) / target

        def _reading(r: dict) -> str:
            pct = _pct(r["z0"])
            status = ("within tolerance" if abs(pct) <= tol_pct else
                      "OUT OF TOLERANCE -- widen trace to lower Z0, or narrow to raise Z0")
            return (f"Net {r.get('net') or '?'} on {r.get('layer')}: "
                    f"Z0={r['z0']:.1f} ohm (target {target:.1f}, {pct:+.1f}%) -- {status}; "
                    f"delay={r['delay_ps_per_mm']:.2f} ps/mm, "
                    f"length={r['length_mm']:.2f} mm, "
                    f"one-way delay={r['delay_ps']:.1f} ps "
                    f"[{r['kind']}, w={r['width']:.3f} mm, h={r['h']:.3f} mm, er={r['er']:.2f}]")

        if net_or_ref:
            try:
                r = _track_impedance(session, net_or_ref)
            except Exception as e:  # noqa: BLE001
                nets_on_board = sorted({tr.get("net", "") for tr in tracks
                                        if isinstance(tr, dict) and tr.get("net")})
                return _err(f"could not compute impedance for {net_or_ref!r}: {e}",
                            hint=f"Routed nets on this board: {', '.join(nets_on_board[:10])}. "
                                 "Pass a net name or a track id (from ecad_status).",
                            next_actions=["ecad_status()"])
            required = ("z0", "eeff", "delay_ps_per_mm", "length_mm", "delay_ps")
            if any(not _finite(r.get(key)) for key in required) or r["z0"] <= 0:
                return _err("impedance backend returned non-finite/non-positive results",
                            backend_result=r)
            pct = _pct(r["z0"])
            reading = _reading(r)
            within = abs(pct) <= tol_pct
            # Guidance: wider -> lower Z0; narrower -> higher Z0.
            if not within:
                if pct > 0:  # Z0 too high -> widen
                    adj = "Widen the trace (increase width) to lower Z0 toward the target."
                else:  # Z0 too low -> narrow
                    adj = "Narrow the trace (decrease width) to raise Z0 toward the target."
            else:
                adj = "Trace impedance is within the standard +/-10% controlled-impedance tolerance."
            return _dumps({
                "ok": True, "pass": within, "doc_id": session.doc_id,
                "net": r.get("net"), "layer": r.get("layer"),
                "z0": round(r["z0"], 2), "target": target,
                "pct_error": round(pct, 2), "within_tolerance": within,
                "kind": r["kind"], "model": r["model"],
                "width": r["width"], "h": r["h"], "er": r["er"], "t": r["t"],
                "reference_layers": r.get("reference_layers", []),
                "reference_plane_assumption": r.get(
                    "reference_plane_assumption",
                    "nearest copper layer(s) are continuous reference planes"),
                "length_mm": round(r["length_mm"], 3),
                "delay_ps_per_mm": round(r["delay_ps_per_mm"], 3),
                "delay_ps": round(r["delay_ps"], 2),
                "eeff": round(r["eeff"], 4),
                "warnings": ([] if within else
                             [f"Z0 {pct:+.1f}% from target -- trace needs resizing"]),
                "reading": reading,
                "hint": reading + " " + adj,
                "next_actions": ([adj] if not within else
                                 ["ecad_export('gerber')  # impedance OK -- proceed to fab"]),
            })

        # No net/ref given: summarise every net's impedance as a quick SI health check.
        seen_nets: set = set()
        summaries: list[dict] = []
        failed_nets: list[dict] = []
        for tr in tracks:
            if not isinstance(tr, dict):
                failed_nets.append({"net": None, "error": "track entry is not an object"})
                continue
            net = tr.get("net", "")
            if net in seen_nets or not net:
                continue
            seen_nets.add(net)
            try:
                r = _track_impedance(session, tr)
                if any(not _finite(r.get(key)) for key in (
                        "z0", "delay_ps_per_mm", "length_mm", "delay_ps")) or r["z0"] <= 0:
                    raise ValueError("backend returned non-finite/non-positive results")
                pct = _pct(r["z0"])
                summaries.append({
                    "net": net, "layer": r["layer"], "z0": round(r["z0"], 1),
                    "pct_error": round(pct, 1), "length_mm": round(r["length_mm"], 2),
                    "delay_ps": round(r["delay_ps"], 1),
                    "reference_layers": r.get("reference_layers", []),
                    "within_tolerance": abs(pct) <= tol_pct,
                })
            except Exception as e:  # noqa: BLE001
                failed_nets.append({"net": net, "error": str(e)})
        if not summaries:
            return _err("no routed net had a valid impedance setup",
                        failed_nets=failed_nets,
                        hint="Check track widths/geometry and stackup dielectric/reference layers.")
        out_of_tol = [s for s in summaries if not s["within_tolerance"]]
        worst = (sorted(out_of_tol, key=lambda s: abs(s["pct_error"]), reverse=True)[:3]
                 if out_of_tol else [])
        complete = not failed_nets
        analysis_pass = complete and not out_of_tol
        head = (f"{len(summaries)} net(s) analysed; "
                f"{len(out_of_tol)} out of +/-{tol_pct:.0f}% target={target:.0f} ohm tolerance"
                + (f"; {len(failed_nets)} net(s) invalid." if failed_nets else "."))
        return _dumps({
            "ok": complete, "pass": analysis_pass, "complete": complete,
            **({} if complete else {"error": "one or more routed nets had invalid impedance setup"}),
            "doc_id": session.doc_id, "target": target,
            "nets_analysed": len(summaries), "out_of_tolerance": len(out_of_tol),
            "summaries": summaries,
            "failed_nets": failed_nets,
            "worst": worst,
            "warnings": ([f"{w['net']}: Z0={w['z0']} ohm ({w['pct_error']:+.1f}%)"
                          for w in worst]
                         + [f"{item['net']}: {item['error']}" for item in failed_nets]),
            "reading": head,
            "hint": (head + (" Fix the worst offenders: "
                             + ", ".join(w["net"] for w in worst)) if worst else
                     head + (" All controlled-impedance traces are within tolerance."
                             if complete else " Fix invalid nets before sign-off.")),
            "next_actions": (
                [f"ecad_check_impedance('{worst[0]['net']}', target={target})  # inspect worst offender"]
                if worst else
                ["ecad_status()  # inspect invalid track/stackup setup"] if failed_nets else
                ["ecad_export('gerber')  # SI check passed"]),
        })



    # ---------------------------------------------------------------- differential impedance

    @mcp.tool()
    def ecad_check_diff_impedance(net_p: str, net_n: str, target: float = 100.0,
                                  doc_id: str = "") -> str:
        """Compute the differential impedance (Z_diff) of an edge-coupled differential pair on the
        active board and compare it to a target (default 100 ohm, the common PCIe/USB3 spec).

        ``net_p`` / ``net_n`` are the positive and negative net names of the pair (e.g. 'CLK_P',
        'CLK_N' or 'USB3_TX+', 'USB3_TX-'). Both must have routed tracks on the board.

        Uses the IPC-2141A edge-coupled coupling formula:
          Z_diff = 2 * Z0_se * (1 - 0.48*exp(-0.96*s/h))   [microstrip]
          Z_diff = 2 * Z0_se * (1 - 0.347*exp(-2.9*s/h))   [stripline]
        where ``s`` is the intra-pair edge gap (inferred from the tracks' centre-to-centre distance
        minus both widths) and ``h`` is the dielectric height from the stackup.

        Returns a plain-English reading like:
          "Pair CLK_P/CLK_N: Z_diff=98.2 ohm (target 100, -1.8%) -- within 10% tolerance;
           Z0_se=52.1 ohm, gap=0.20mm, delay=6.7 ps/mm, length=23.4mm, one-way delay=157ps"
        plus next_actions to widen/narrow/gap the traces.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Differential impedance needs a board with routed tracks.",
                        next_actions=["ecad_new_pcb('myboard')", "ecad_autoroute()"])
        doc = session.doc
        tracks = doc.get("tracks", [])
        if not tracks:
            return _err("no routed tracks on this board",
                        hint="Route the board first (ecad_autoroute).",
                        next_actions=["ecad_autoroute()"])
        try:
            target = _finite_float(target, "target")
            if target <= 0:
                raise ValueError("target must be > 0 ohm")
            if (not isinstance(net_p, str) or not net_p.strip()
                    or not isinstance(net_n, str) or not net_n.strip()
                    or net_p == net_n):
                raise ValueError("net_p and net_n must be distinct non-empty net names")
            _validate_stackup_for_sim(doc)
            if not all(isinstance(track, dict) for track in tracks):
                raise ValueError("board tracks must be objects")
        except (TypeError, ValueError) as e:
            return _err(f"invalid differential-impedance setup: {e}")

        def _first_track(net: str) -> dict | None:
            return next((track for track in tracks if track.get("net") == net), None)

        tr_p = _first_track(net_p)
        tr_n = _first_track(net_n)
        routed_nets = sorted({t.get("net", "") for t in tracks if t.get("net")})

        if tr_p is None or tr_n is None:
            missing = ([net_p] if tr_p is None else []) + ([net_n] if tr_n is None else [])
            return _err(
                f"nets {', '.join(missing)} are not routed on this board",
                hint=f"Routed nets: {', '.join(routed_nets[:10])}. "
                     "Run ecad_autoroute with diff_pairs=[['{net_p}','{net_n}']] first.",
                next_actions=[f"ecad_autoroute(diff_pairs=[['{net_p}','{net_n}']])"],
                routed_nets=routed_nets)

        try:
            width_p = _finite_float(tr_p.get("width"), f"{net_p} width")
            width_n = _finite_float(tr_n.get("width"), f"{net_n} width")
            if width_p <= 0 or width_n <= 0:
                raise ValueError("both trace widths must be > 0 mm")
            if abs(width_p - width_n) > max(1e-6, 0.01 * max(width_p, width_n)):
                raise ValueError("closed-form differential model requires equal trace widths")
            layer = tr_p.get("layer")
            if not isinstance(layer, str) or not layer or tr_n.get("layer") != layer:
                raise ValueError("pair traces must be routed on the same copper layer")
            centre_dist = _parallel_segment_spacing(tr_p, tr_n)
            gap = centre_dist - width_p / 2.0 - width_n / 2.0
            if gap <= 0:
                raise ValueError("pair copper overlaps or has zero edge gap")
            geo = _stackup_geometry(doc, layer)
        except (TypeError, ValueError) as e:
            return _err(f"invalid differential-pair geometry: {e}",
                        hint="Use equal-width, overlapping parallel segments on one layer "
                             "with a physical edge gap and explicit reference stackup.")
        h, t_cu, er = geo["h"], geo["t"], geo["er"]
        kind = geo["kind"]

        try:
            dz = _diff_pair_z0(w=width_p, s=gap, h=h, t=t_cu, er=er, kind=kind)
        except Exception as e:  # noqa: BLE001
            return _err(f"diff_pair_z0 failed: {e}",
                        hint="Check that width/gap/h are positive and er >= 1.",
                        next_actions=["ecad_status()"])
        if (any(not _finite(dz.get(key)) for key in (
                "z0_diff", "z0_se", "z0_odd", "delay_ps_per_mm", "eeff"))
                or dz["z0_diff"] <= 0 or dz["z0_se"] <= 0):
            return _err("differential impedance backend returned invalid numeric results",
                        backend_result=dz)

        z_diff = dz["z0_diff"]
        z0_se = dz["z0_se"]
        z0_odd = dz["z0_odd"]
        delay_ps_per_mm = dz["delay_ps_per_mm"]
        model = dz["model"]

        try:
            length_p = _track_length_mm(doc, net_p, layer)
            length_n = _track_length_mm(doc, net_n, layer)
        except (TypeError, ValueError) as e:
            return _err(f"invalid differential-pair route length: {e}")
        if length_p <= 0 or length_n <= 0:
            return _err("differential-pair nets must each have non-zero routed length")
        length_mm = (length_p + length_n) / 2.0
        skew_mm = abs(length_p - length_n)
        delay_ps = delay_ps_per_mm * length_mm

        tol_pct = 10.0
        pct = 100.0 * (z_diff - target) / target
        within = abs(pct) <= tol_pct

        status = "within tolerance" if within else "OUT OF TOLERANCE"
        reading = (f"Pair {net_p}/{net_n}: Z_diff={z_diff:.1f} ohm "
                   f"(target {target:.0f}, {pct:+.1f}%) -- {status}; "
                   f"Z0_se={z0_se:.1f} ohm, Z0_odd={z0_odd:.1f} ohm, "
                   f"gap={gap:.3f}mm, delay={delay_ps_per_mm:.2f}ps/mm, "
                   f"length={length_mm:.2f}mm (skew={skew_mm:.3f}mm), "
                   f"one-way delay={delay_ps:.1f}ps [{kind}, w={width_p:.3f}mm, h={h:.3f}mm, "
                   f"er={er:.2f}] model={model}")

        # Guidance: Z_diff too high -> widen traces or reduce gap; too low -> narrow / widen gap.
        if not within:
            if pct > 0:  # Z_diff above target
                adj = ("Widen traces (increase width) or reduce intra-pair gap to lower Z_diff. "
                       "Current gap={:.3f}mm; try gap={:.3f}mm.".format(gap, gap * 0.8))
            else:  # Z_diff below target
                adj = ("Narrow traces or increase intra-pair gap to raise Z_diff. "
                       "Current gap={:.3f}mm; try gap={:.3f}mm.".format(gap, gap * 1.2))
        else:
            adj = ("Differential impedance is within the standard +/-10% tolerance. "
                   f"Skew={skew_mm:.3f}mm" +
                   (" -- consider length matching if skew > 0.2mm." if skew_mm > 0.2 else "."))

        next_actions: list[str] = [adj]
        if skew_mm > 0.2:
            next_actions.append(
                f"Length skew {skew_mm:.3f}mm exceeds 0.2mm: re-route with "
                f"ecad_autoroute(diff_pairs=[['{net_p}','{net_n}']]) for automatic "
                "length-matching meanders.")
        if within:
            next_actions.append("ecad_export('gerber')  # SI check passed -- proceed to fab")

        return _dumps({
            "ok": True, "pass": within, "doc_id": session.doc_id,
            "net_p": net_p, "net_n": net_n,
            "z_diff": round(z_diff, 2), "z0_se": round(z0_se, 2),
            "z0_odd": round(z0_odd, 2),
            "target": target, "pct_error": round(pct, 2), "within_tolerance": within,
            "gap_mm": round(gap, 4), "width_mm": round(width_p, 4),
            "h": round(h, 4), "er": round(er, 2), "t": round(t_cu, 4),
            "kind": kind, "model": model,
            "reference_layers": geo["reference_layers"],
            "reference_plane_assumption": "nearest copper layer(s) are continuous reference planes",
            "length_mm": round(length_mm, 3),
            "length_p_mm": round(length_p, 3), "length_n_mm": round(length_n, 3),
            "skew_mm": round(skew_mm, 4),
            "delay_ps_per_mm": round(delay_ps_per_mm, 3),
            "delay_ps": round(delay_ps, 2),
            "eeff": round(dz["eeff"], 4),
            "warnings": ([] if within else
                         [f"Z_diff {pct:+.1f}% from target -- pair needs retuning"])
                      + ([f"skew {skew_mm:.3f}mm > 0.2mm"] if skew_mm > 0.2 else []),
            "reading": reading,
            "hint": reading + " " + adj,
            "next_actions": next_actions,
        })



    @mcp.tool()
    def ecad_run_sicheck(doc_id: str = "") -> str:
        """Run the high-speed / signal-integrity rule pack on the active board: length-match
        groups (per-net deltas vs the group median), diff-pair intra-pair skew, termination
        presence/topology (series R near driver, parallel/Thevenin at the end), via-stub length
        vs rise time (tr/3 heuristic), and BGA fanout (via-in-pad, escape feasibility, courtyard
        congestion). Rules fire from the net-class declarations made with ecad_set_net_class
        (match_tolerance_mm / pair_skew_mm / termination / rise_time_ps).

        HONEST LIMITS (also in the response's ``limitations``): closed-form IPC-2141A math +
        topology heuristics only — NOT a field solver and NOT an IBIS simulation; no crosstalk,
        return-path, or eye-diagram analysis. Treat a clean result as "no classic layout
        mistakes", not a sign-off for multi-gigabit interfaces.

        Advisory like ecad_run_drc: returns grouped violations + a per-group length-match table
        (``match_report``) with each net's length and delta.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a board first.",
                        next_actions=["ecad_new_pcb('myboard')"])
        try:
            from services.ecad import si_rules as si_mod  # STAYS: no C++ counterpart
            res = si_mod.run_sicheck(session.doc)
        except Exception as e:  # noqa: BLE001
            return _err(f"SI check engine error: {e}", hint="Inspect with ecad_status().")
        violations = res["violations"]
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)
        worst = lines[:5]
        declared = sorted(
            k for k, v in (session.doc.get("net_classes") or {}).items()
            if isinstance(v, dict) and any(key in v for key in (
                "match_tolerance_mm", "pair_skew_mm", "termination", "rise_time_ps")))
        complete = bool(declared)
        si_pass = complete and not violations
        verdict = (
            (f"{summary['errors']} error(s), {summary['warnings']} warning(s) across "
             f"{len(declared)} SI class(es)."
             + (" worst: " + " | ".join(worst) if worst else " Declared SI checks are clean."))
            if declared else
            "INCOMPLETE: no SI rules declared — no signal-integrity pass can be claimed; "
            "declare match groups / termination / rise time with ecad_set_net_class first.")
        return _dumps({
            "ok": True, "pass": si_pass, "complete": complete,
            "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warning_count": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "match_report": res["match_report"],
            "si_classes": declared,
            "limitations": res["limitations"],
            "warnings": worst[:3] + ([] if declared else ["no SI rule classes declared"]),
            "hint": verdict,
            "next_actions": (
                ["Fix the worst SI offenders (lengthen short match-group nets, add/relocate "
                 "termination Rs, back-drill or re-layer stubbed vias), then re-run "
                 "ecad_run_sicheck()."] if violations else
                (["ecad_run_drc()  # SI pack clean — confirm physical rules too"] if declared else
                 ["ecad_set_net_class('ddr_byte0', params={'match_tolerance_mm': 0.5}, "
                  "nets=[...])  # declare SI rules, then re-run"])
            ),
        })



    # ---------------------------------------------------------------- .tf transfer function

    @mcp.tool()
    def ecad_tf(output: str, source: str, doc_id: str = "", backend: str = "mna") -> str:
        """Run a DC small-signal transfer function (.tf) on the active schematic's circuit.

        ``backend``: 'mna' (default, internal solver) | 'ngspice' (installed ngspice binary,
        needed for vendor .SUBCKT models) | 'auto'. Absent ngspice degrades honestly to
        'mna' with a ``backend_note``.

        Computes:
          * DC gain  -- output / source (V/V for V-source input, V/A transimpedance for I-source)
          * Rin      -- resistance seen looking into the source terminals
          * Rout     -- resistance seen looking into the output terminals (all sources nulled)

        ``output`` is a voltage probe: 'V(OUT)', 'V(out,in)', etc.
        ``source`` is the name of an independent V or I source: 'V1', 'I1', etc.

        Reading example: "DC gain V(OUT)/V1 = -10.00, Rin = 1.00 kohm, Rout = 100 ohm"
        This is exactly the SPICE .tf analysis, useful for amplifier gain/loading checks.
        """
        if not isinstance(output, str) or not output.strip():
            return _err("output must be a non-empty voltage probe or node name")
        if not isinstance(source, str) or not source.strip():
            return _err("source must name an independent V or I source")
        output = output.strip()
        source = source.strip()
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Transfer function needs an open schematic.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        circuit, warns = _netlist_to_circuit(nl)  # C++ sim_netlist_to_circuit RPC
        if not circuit.get("devices"):
            return _err("nothing to analyse -- netlist has no simulatable devices",
                        hint="Place R/C/V/I/op-amp parts with VALUES and wire them first.",
                        next_actions=["ecad_find_part('voltage')", "ecad_status()"],
                        warnings=warns)
        source_devices = [
            device for device in circuit["devices"]
            if device.get("kind") in ("V", "I") and device.get("name") == source
        ]
        if not source_devices:
            available = [
                device.get("name") for device in circuit["devices"]
                if device.get("kind") in ("V", "I")
            ]
            return _err(f"transfer-function source {source!r} was not found",
                        hint=f"Available independent sources: {available}")

        study = {"analyses": [{"type": "tf", "output": output, "source": source}],
                 "probes": [output]}
        _autosave_all_open_ecad()
        status, result = _run_with_budget(
            lambda: _sim_run(circuit, study, backend=backend))
        if status == "timeout":
            return _budget_timeout_err(
                "transfer-function analysis",
                simplify="Simplify the circuit or use backend='mna'.")
        if status == "error":
            return _err(f"transfer-function analysis failed: {result}")
        if not isinstance(result, dict):
            return _err("transfer-function backend returned a non-object response")
        if result.get("backend_note"):
            warns.append(result["backend_note"])
        tf = result.get("tf")
        if not isinstance(tf, dict):
            return _err("transfer-function backend returned no tf result")
        conv = bool(result.get("convergence", {}).get("tf", False))
        gain = tf.get("gain", float("nan"))
        rin = tf.get("rin", float("nan"))
        rout = tf.get("rout", float("nan"))
        if conv and not _finite(gain):
            conv = False
            warns.append("tf backend reported convergence but returned a non-finite gain")

        def _ohm(v: float) -> str:
            if not _finite(v):
                return "n/a"
            a = abs(v)
            if a >= 1e6:
                return f"{v/1e6:.3g} Mohm"
            if a >= 1e3:
                return f"{v/1e3:.3g} kohm"
            return f"{v:.3g} ohm"

        gain_str = f"{gain:.4g}" if _finite(gain) else "n/a"
        reading = (f"DC gain {output}/{source} = {gain_str}, "
                   f"Rin = {_ohm(rin)}, Rout = {_ohm(rout)}")
        hint = reading
        if not conv:
            hint += " -- did NOT converge (check connectivity/source names)"

        # Guidance based on results.
        next_actions: list[str] = []
        if not conv:
            next_actions += ["ecad_run_erc()  # find connectivity problems",
                             "ecad_status()  # confirm wiring"]
        else:
            next_actions.append("ecad_simulate('ac')  # see frequency response, not just DC gain")
            if _finite(rin) and rin < 100.0:
                next_actions.append(
                    f"Low Rin={_ohm(rin)} -- the source may be heavily loaded by the circuit.")
            if _finite(rout) and rout > 1e3:
                next_actions.append(
                    f"High Rout={_ohm(rout)} -- output may not drive low-impedance loads well.")

        return _dumps({
            "ok": conv, "doc_id": session.doc_id,
            **({"error": "transfer-function analysis did not converge"}
               if not conv else {}),
            "backend": result.get("backend", "mna"),
            "output": output, "source": source,
            "gain": _round(gain), "rin": _round(rin), "rout": _round(rout),
            "converged": conv,
            "device_count": len(circuit["devices"]),
            "warnings": warns + ([] if conv else ["tf did not converge"]),
            "reading": reading,
            "hint": hint,
            "next_actions": next_actions or ["ecad_status()"],
            "sim_log": result.get("log", [])[-4:],
        })
