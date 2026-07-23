"""ECAD simulation/analysis MCP tools (T-RF8 split of ecad_tools.py): ecad_simulate, run_pdn, sweep, fpga_check, impedance checks, run_sicheck, tf, design."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
import json
import os

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


def _copper_thickness(layer: dict, default: float = 0.035) -> float:
    return float(layer.get("thickness", default) or default)


def _stackup_geometry(board: dict, layer: str) -> dict:
    """Classify a copper layer in the stackup and pull the controlled-impedance geometry
    (verbatim port of engine.ecad.impedance._stackup_geometry; reference-plane assumptions
    documented there)."""
    layers = board.get("stackup", {}).get("layers", [])
    copper_idx = [i for i, ly in enumerate(layers) if ly.get("type") == "copper"]
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


def _track_length_mm(board: dict, net: str | None, layer: str | None) -> float:
    """Sum the length of every routed segment/arc on ``net`` (optionally restricted to ``layer``)."""
    import math as _math
    total = 0.0
    for tr in board.get("tracks", []):
        if net is not None and tr.get("net") != net:
            continue
        if layer is not None and tr.get("layer") != layer:
            continue
        if tr.get("kind", "segment") == "arc":
            r = float(tr.get("radius", 0.0))
            a0 = _math.radians(float(tr.get("a0", 0.0)))
            a1 = _math.radians(float(tr.get("a1", 0.0)))
            total += abs(a1 - a0) * r
        else:
            p1 = tr.get("p1", [0.0, 0.0]); p2 = tr.get("p2", [0.0, 0.0])
            total += _math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    return total


def _track_impedance(session, track_or_net) -> dict:
    """engine.ecad.impedance.track_impedance -> C++ ecad_impedance RPC (mode='track')."""
    return _rpc("ecad_impedance", {"mode": "track", "doc_id": session.doc_id,
                                   "track_or_net": track_or_net})


def _diff_pair_z0(*, w: float, s: float, h: float, t: float, er: float, kind: str) -> dict:
    """engine.ecad.impedance.diff_pair_z0 -> C++ ecad_impedance RPC (mode='diff_pair')."""
    return _rpc("ecad_impedance", {"mode": "diff_pair", "w": w, "s": s, "h": h, "t": t,
                                   "er": er, "kind": kind})

def register_ecad_sim_tools(mcp: FastMCP) -> None:
    """ECAD simulation/analysis MCP tools (T-RF8 split of ecad_tools.py): ecad_simulate, run_pdn, sweep, fpga_check, impedance checks, run_sicheck, tf, design."""


    # ---------------------------------------------------------------- simulate

    @mcp.tool()
    def ecad_simulate(analysis: str = "op", params: Optional[dict] = None,
                      probes: Optional[list] = None, doc_id: str = "",
                      backend: str = "mna") -> str:
        """Build the netlist -> SPICE circuit -> run the simulator, and return the KEY
        results with a plain-English reading.

        ``backend`` picks the solver (T-FIN14):
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

        an = _build_analysis(analysis, params or {}, circuit)
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
                raise val
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
                    return float(((d.get("params") or {}).get("ac") or {}).get("mag", 0.0) or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            vsrcs = [d for d in circuit["devices"] if d.get("kind") == "V"]
            want_mag = float(p.get("ac_mag", 1.0) or 1.0)
            want_src = p.get("ac_source") or p.get("source")
            if not vsrcs:
                warns.append("AC sweep has no voltage source to drive — add a V source "
                             "and wire it to the input.")
            elif want_src:
                tgt = next((d for d in vsrcs if d.get("name") == want_src), None)
                if tgt is None:
                    warns.append(f"ac_source {want_src!r} not found among "
                                 f"{[d['name'] for d in vsrcs]} — drove {vsrcs[0]['name']} instead.")
                    tgt = vsrcs[0]
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
            raise result
        conv = result.get("convergence", {})
        converged = bool(conv.get(an["type"], False))

        if result.get("backend_note"):
            warns.append(result["backend_note"])
        payload: dict = {
            "ok": True, "doc_id": session.doc_id, "analysis": an["type"],
            "backend": result.get("backend", "mna"),
            "converged": converged,
            "device_count": len(circuit["devices"]),
            "nodes": nodes, "probes": probe_list,
            "warnings": warns + ([] if converged else
                                 [f"{an['type']} did not converge — see sim_log"]),
            "sim_log": result.get("log", [])[-6:],
        }

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



    # ---------------------------------------------------------------- PDN (T-FIN13)

    @mcp.tool()
    def ecad_run_pdn(net: str = "", vsrc: float = 0.0, source_refs: str = "",
                     sink_overrides: str = "", resolution_mm: float = 0.0,
                     j_limit_a_mm2: float = 0.0, doc_id: str = "") -> str:
        """DC power-delivery (IR-drop) analysis of the active board's power-net copper.

        Meshes the net's pours/tracks/pads into a 2.5D resistor grid (sheet resistance
        from the stackup copper thickness) with via barrels as annular resistors, then
        does a sparse DC solve. Sources = regulator/connector output pads (auto-detected
        from ``power_out`` symbol pins, or pass ``source_refs='U3,J1'``) held at ``vsrc``
        (default: the rail's ``voltage_v_nom`` metadata, else parsed from names like
        '+3V3'). Sinks = per-component supply currents from the aicad.boardphys/1 rail
        metadata (T-FIN8); override/provide with ``sink_overrides='{"U2": 0.35}'`` (amps).

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
            from engine.ecad import pdn as pdn_mod  # STAYS (T-RF20): no C++ counterpart; lazy import (pulls scipy)
        except Exception as e:  # noqa: BLE001
            return _err(f"PDN engine unavailable: {e}",
                        hint="numpy/scipy are required for the sparse solve.")
        try:
            overrides = {str(k): float(v)
                         for k, v in (json.loads(sink_overrides) if sink_overrides
                                      else {}).items()}
        except Exception as e:  # noqa: BLE001
            return _err(f"sink_overrides must be a JSON object of ref->amps: {e}")
        srcs = [s.strip() for s in source_refs.split(",") if s.strip()] or None

        nets = [n.strip() for n in net.split(",") if n.strip()]
        if not nets:
            nets = sorted({n.get("name") for n in doc.get("nets", [])
                           if n.get("name") and n.get("name") != "GND"
                           and pdn_mod._auto_sources(doc, n.get("name"))})
            if not nets:
                return _err("no power net with a detectable source (power_out pin) — "
                            "pass net='+5V' and source_refs='<regulator/connector ref>'.",
                            next_actions=["ecad_run_pdn(net='+5V', source_refs='J1')"])

        _autosave_all_open_ecad()

        def _solve_all():
            return [pdn_mod.solve_pdn(
                doc, n,
                vsrc=(vsrc if vsrc else None),
                source_refs=srcs,
                sink_overrides=overrides,
                resolution_mm=(resolution_mm if resolution_mm > 0 else None),
                j_limit_a_mm2=(j_limit_a_mm2 if j_limit_a_mm2 > 0
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
                if os.path.exists(existing):
                    with open(existing, encoding="utf-8") as fh:
                        sidecar = json.load(fh)
                    pdn_mod.attach_pdn(sidecar, ok_results)
                    sidecar_path = existing
                else:
                    sidecar = pdn_mod.attach_pdn({
                        "schema": "aicad.boardphys/1",
                        "generated_by": "engine.ecad.pdn.solve_pdn",
                        "board": {"name": doc.get("name"), "id": doc.get("id"),
                                  "thickness_mm": doc.get("stackup", {}).get("thickness")},
                        "components": [],
                        "note": "PDN-only sidecar — run ecad_to_3d for the full "
                                "component power/thermal map.",
                    }, ok_results)
                    os.makedirs(str(out_dir), exist_ok=True)
                    sidecar_path = os.path.join(str(out_dir), "pdn.boardphys.json")
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
        verdict = (f"{len(violations)} PDN violation(s) across {len(ok_results)} net(s)."
                   if violations else
                   f"PDN clean on {len(ok_results)} net(s)."
                   ) + (f" worst drop {worst['drop_v']*1000:.1f} mV at {worst['ref']} "
                        f"({worst['net']})." if worst and worst.get("drop_v") else "")
        summaries = [{k: r[k] for k in ("net", "vsrc", "sources", "resolution_mm",
                                        "n_nodes", "drops", "worst_drop", "max_j_a_mm2",
                                        "j_limit_a_mm2", "hotspots", "violations",
                                        "unknown_sinks", "elapsed_s")}
                     for r in ok_results]
        return _dumps({
            "ok": bool(ok_results), "doc_id": session.doc_id, "revision": session.revision,
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
                ["Widen the flagged copper / add a pour or stitching vias, then re-run "
                 "ecad_run_pdn()."] if violations else
                ["ecad_to_3d()  # export STEP + full boardphys sidecar for thermal"]),
        })



    # ---------------------------------------------------------------- parameter sweep

    @mcp.tool()
    def ecad_sweep(param: str, values: Optional[list] = None,
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
        if not values or not isinstance(values, list):
            return _err("ecad_sweep needs a non-empty 'values' list",
                        hint="Pass the values to sweep, e.g. ecad_sweep('R1', [1000, 2000, 5000]).",
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
        an = _build_analysis(analysis, {}, circuit)
        if an is None:
            return _err(f"unknown analysis {analysis!r}",
                        hint="Use one of: 'op', 'dc', 'tran', 'ac'.",
                        next_actions=["ecad_sweep('R1', [1000, 2000])"])
        fvals = [float(v) for v in values]
        study = {"analyses": [an], "probes": probe_list,
                 "step": {"param": param, "values": fvals}}
        result = _sim_run(circuit, study)
        step = result.get("step", {})
        runs = step.get("runs", [])
        trend, sweep_table, chosen = _summarize_sweep_steps(runs, an["type"], probe or None,
                                                            probe_order=probe_list)
        converged = bool(result.get("convergence", {}).get("step", False))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "analysis": an["type"],
            "param": param, "values": fvals, "converged": converged,
            "probe": chosen, "sweep": sweep_table,
            "trend": trend,
            "warnings": warns + ([] if converged else
                                 ["one or more sweep points did not converge"]),
            "hint": (f"Swept {param} over {len(fvals)} value(s): {trend}" if trend else
                     f"Swept {param} but found no finite {chosen or 'probe'} value — "
                     "check connectivity / values (ecad_run_erc)."),
            "next_actions": ["ecad_simulate('op')  # inspect one operating point in detail",
                             "ecad_status()"],
        })



    # ---------------------------------------------------------------- FPGA (T-FIN11)

    @mcp.tool()
    def ecad_fpga_check(ref: str = "", io: Optional[dict] = None, doc_id: str = "") -> str:
        """Bank-aware FPGA design checks on the active schematic (T-FIN11).

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
        try:
            from engine.ecad import fpga  # STAYS (T-RF20): no C++ counterpart
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
    def ecad_design(request: str) -> str:
        """Autonomous ECAD design: natural-language request -> verified schematic + simulation.

        Runs the full deterministic orchestrator pipeline (PLAN -> BUILD -> ERC -> SIMULATE ->
        REPORT) and returns a summary with the doc_id, stage results, ERC counts, and sim
        pass/fail. With a provider configured, the model proposes the topology; without one (or
        if the provider is offline/unavailable) the closest built-in recipe is used so the
        pipeline ALWAYS produces a verified schematic.

        After this call the produced schematic becomes the active one -- ecad_status() and
        ecad_simulate() operate on it immediately.

        ``request`` is a plain-English design description, e.g.
          '5V voltage divider with 2.5V output'
          'LED driver for a red LED from a 3.3V supply'
          'inverting op-amp with gain 10'
        """
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

        # Resolve the configured provider (best-effort; fall back to None -> recipe path).
        provider = None
        try:
            from agent import config as _cfg  # type: ignore
            provider = _cfg.get_provider()
        except Exception:  # noqa: BLE001 -- offline / unconfigured / module absent
            pass

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
        # Override with AI_CAD_ECAD_PLAN_BUDGET_S when the client allows a longer call.
        try:
            _PLAN_BUDGET_S = float(os.environ.get("AI_CAD_ECAD_PLAN_BUDGET_S", "") or 12.0)
        except (TypeError, ValueError):
            _PLAN_BUDGET_S = 12.0
        if not (_PLAN_BUDGET_S > 0.0):
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
                with _PROVIDER_PLAN_LOCK:
                    _PROVIDER_PLAN_BUSY["v"] = False
        elif provider is None:
            # Instant recipe plan (no model). Safe on the caller's thread.
            my_gen = plan_gen["v"] + 1
            plan_gen["v"] = my_gen
            _plan_worker(my_gen)
        # provider_busy: leave plan_box empty -> recipe plan below.

        design = plan_box.get("design")
        recipe_used = plan_box.get("recipe_used")
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

        busy_note = (
            "Another ecad_design is already using the model server — used the built-in "
            "recipe instead (the schematic is still verified). "
            if provider_busy else "")
        hint = ((f"The configured model server did not plan within {_PLAN_BUDGET_S:.0f}s — used "
                 "the built-in recipe instead (it is loading/slow; the schematic is still verified). "
                 if provider_timed_out else "")
                + busy_note
                + ("The configured model server is not reachable — used the built-in recipe "
                   "instead (the schematic is still verified). "
                   if provider_unreachable else "")
                + (f"Provider plan error ({type(provider_plan_err).__name__}: {provider_plan_err}) "
                   "— used the built-in recipe instead. "
                   if provider_plan_err is not None else "")
                + f"Design '{request}' produced with stop_reason={stop!r}. "
                + (f"Recipe fallback used: {recipe!r}. " if recipe else "")
                + (f"ERC: {erc.get('errors', 0)} error(s), "
                   f"{erc.get('warnings', 0)} warning(s). " if erc else "")
                + (f"Sim: {sim_line}." if sim_line else "")
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

        tol_pct = 10.0  # +/-10% is the standard controlled-impedance fab tolerance

        def _pct(z0: float) -> float:
            return 100.0 * (z0 - target) / target if target else 0.0

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
                nets_on_board = sorted({tr.get("net", "") for tr in tracks if tr.get("net")})
                return _err(f"could not compute impedance for {net_or_ref!r}: {e}",
                            hint=f"Routed nets on this board: {', '.join(nets_on_board[:10])}. "
                                 "Pass a net name or a track id (from ecad_status).",
                            next_actions=["ecad_status()"])
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
                "ok": True, "doc_id": session.doc_id,
                "net": r.get("net"), "layer": r.get("layer"),
                "z0": round(r["z0"], 2), "target": target,
                "pct_error": round(pct, 2), "within_tolerance": within,
                "kind": r["kind"], "model": r["model"],
                "width": r["width"], "h": r["h"], "er": r["er"], "t": r["t"],
                "length_mm": round(r["length_mm"], 3),
                "delay_ps_per_mm": round(r["delay_ps_per_mm"], 3),
                "delay_ps": round(r["delay_ps"], 2),
                "eeff": round(r["eeff"], 4),
                "warnings": ([] if within else [f"Z0 {pct:+.1f}% from target -- trace needs resizing"]),
                "reading": reading,
                "hint": reading + " " + adj,
                "next_actions": ([adj] if not within else
                                 ["ecad_export('gerber')  # impedance OK -- proceed to fab"]),
            })

        # No net/ref given: summarise every net's impedance as a quick SI health check.
        seen_nets: set = set()
        summaries: list[dict] = []
        for tr in tracks:
            net = tr.get("net", "")
            if net in seen_nets or not net:
                continue
            seen_nets.add(net)
            try:
                r = _track_impedance(session, tr)
                pct = _pct(r["z0"])
                summaries.append({
                    "net": net, "layer": r["layer"], "z0": round(r["z0"], 1),
                    "pct_error": round(pct, 1), "length_mm": round(r["length_mm"], 2),
                    "delay_ps": round(r["delay_ps"], 1),
                    "within_tolerance": abs(pct) <= tol_pct,
                })
            except Exception:  # noqa: BLE001
                pass
        out_of_tol = [s for s in summaries if not s["within_tolerance"]]
        worst = (sorted(out_of_tol, key=lambda s: abs(s["pct_error"]), reverse=True)[:3]
                 if out_of_tol else [])
        head = (f"{len(summaries)} net(s) analysed; "
                f"{len(out_of_tol)} out of +/-{tol_pct:.0f}% target={target:.0f} ohm tolerance.")
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "target": target,
            "nets_analysed": len(summaries), "out_of_tolerance": len(out_of_tol),
            "summaries": summaries,
            "worst": worst,
            "warnings": ([f"{w['net']}: Z0={w['z0']} ohm ({w['pct_error']:+.1f}%)" for w in worst]
                         if worst else []),
            "reading": head,
            "hint": (head + (" Fix the worst offenders: "
                             + ", ".join(w["net"] for w in worst)) if worst else
                     head + " All controlled-impedance traces are within tolerance."),
            "next_actions": (
                [f"ecad_check_impedance('{worst[0]['net']}', target={target})  # inspect worst offender"]
                if worst else
                ["ecad_export('gerber')  # SI check passed"]
            ),
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

        # Locate routed tracks for each net.
        def _first_track(net: str) -> dict | None:
            return next((t for t in tracks if t.get("net") == net), None)

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

        # Use the positive net's first track for geometry (width, layer).
        width_p = float(tr_p.get("width", 0.25))
        width_n = float(tr_n.get("width", width_p))
        layer = tr_p.get("layer", "F.Cu")

        # Infer intra-pair gap: centre-to-centre of the two tracks minus half-widths of each.
        # Track digitisation direction is arbitrary (a router may emit the return net
        # reversed, p1<->p2 swapped), so take the MIN distance over the four endpoint
        # pairings — for parallel co-extensive segments that is the pair separation
        # regardless of direction. (Using p1-p1 only inflated the gap to ~the track
        # LENGTH for a reversed pair, wrecking Z_diff.)
        import math as _math
        _ends_p = [tr_p.get("p1", [0.0, 0.0]), tr_p.get("p2") or tr_p.get("p1", [0.0, 0.0])]
        _ends_n = [tr_n.get("p1", [0.0, 0.0]), tr_n.get("p2") or tr_n.get("p1", [0.0, 0.0])]
        centre_dist = min(
            _math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))
            for a in _ends_p for b in _ends_n)
        # Clamp gap to something physical (0.05mm minimum; fall back to DEFAULT_DIFF_GAP=0.2)
        gap = max(centre_dist - width_p / 2.0 - width_n / 2.0, 0.05)
        if centre_dist < 0.01:
            gap = 0.2  # overlapping start points -- use default gap

        geo = _stackup_geometry(doc, layer)
        h, t_cu, er = geo["h"], geo["t"], geo["er"]
        kind = geo["kind"]  # 'microstrip' or 'stripline'

        try:
            dz = _diff_pair_z0(w=width_p, s=gap, h=h, t=t_cu, er=er, kind=kind)
        except Exception as e:  # noqa: BLE001
            return _err(f"diff_pair_z0 failed: {e}",
                        hint="Check that width/gap/h are positive and er >= 1.",
                        next_actions=["ecad_status()"])

        z_diff = dz["z0_diff"]
        z0_se = dz["z0_se"]
        z0_odd = dz["z0_odd"]
        delay_ps_per_mm = dz["delay_ps_per_mm"]
        model = dz["model"]

        length_p = _track_length_mm(doc, net_p, layer)
        length_n = _track_length_mm(doc, net_n, layer)
        length_mm = (length_p + length_n) / 2.0  # average pair length
        skew_mm = abs(length_p - length_n)
        delay_ps = delay_ps_per_mm * length_mm

        tol_pct = 10.0
        pct = 100.0 * (z_diff - target) / target if target else 0.0
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
            "ok": True, "doc_id": session.doc_id,
            "net_p": net_p, "net_n": net_n,
            "z_diff": round(z_diff, 2), "z0_se": round(z0_se, 2), "z0_odd": round(z0_odd, 2),
            "target": target, "pct_error": round(pct, 2), "within_tolerance": within,
            "gap_mm": round(gap, 4), "width_mm": round(width_p, 4),
            "h": round(h, 4), "er": round(er, 2), "t": round(t_cu, 4),
            "kind": kind, "model": model,
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
            from engine.ecad import si_rules as si_mod  # STAYS (T-RF20): no C++ counterpart
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
        verdict = (f"{summary['errors']} error(s), {summary['warnings']} warning(s) across "
                   f"{len(declared)} SI class(es)."
                   + (" worst: " + " | ".join(worst) if worst else " SI-clean board.")
                   + ("" if declared else
                      " NOTE: no SI rules declared — declare match groups / termination / "
                      "rise time with ecad_set_net_class first."))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warning_count": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "match_report": res["match_report"],
            "si_classes": declared,
            "limitations": res["limitations"],
            "warnings": worst[:3],
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

        study = {"analyses": [{"type": "tf", "output": output, "source": source}],
                 "probes": [output]}
        result = _sim_run(circuit, study, backend=backend)
        if result.get("backend_note"):
            warns.append(result["backend_note"])
        tf = result.get("tf", {})
        conv = bool(result.get("convergence", {}).get("tf", False))
        gain = tf.get("gain", float("nan"))
        rin = tf.get("rin", float("nan"))
        rout = tf.get("rout", float("nan"))

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
