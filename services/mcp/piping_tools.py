"""AI-Piping MCP tools — steady-state 1-D pipe-network hydraulic solve + pressure-drop report
+ 3D CFD handoff for pipe internals.

Same design contract as ``ecad_tools`` (design principle: "the AI is extremely limited; its tools
must do the bookkeeping and return rich, actionable context"). Each tool returns a JSON string
``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}`` produced by the C++ core
(the 1-D solve / report / operating-point / fluid / CFD-BC / verify envelopes
are owned by ``core/piping`` behind the libchudware ``piping_*`` RPCs — reached via
:mod:`services.mcp.core_bridge`); the MCP layer keeps input parsing, its tiny session state
(so ``piping_report`` can reuse the document / result of the last ``piping_solve``), and the
kernel orchestration legs (``piping_internal_volume`` bore extraction + the
``piping_cfd_handoff`` bore→CFD pipeline), which still call ``services.piping.cfd_handoff``
(OCCT sweep — ported by a later leaf).

Tools:
  piping_solve           aicad.piping/1 doc + boundary conditions -> node pressures/heads + link
                         flows; also consumes externally-supplied networks (source_handoff=
                         an aicad.simhandoff/1 envelope, e.g. from hydraulic_piping_handoff)
  piping_report          per-segment pressure-drop table with an explicit K-factor breakdown
  piping_internal_volume extract the internal fluid-domain solid from a pipe run (OCCT boolean)
  piping_cfd_handoff     1-D solve -> extract bore -> invoke run_cfd_flow (LBM) in one step
  piping_verify_cfd      DAG verify node: compare 1D vs 3D pressure drop + flag velocity limits
  piping_route           orthogonal obstacle-avoiding A* route of a new run (+ LR elbows)
  piping_spec_check      ASME B31.3 wall / MAWP / flange-rating / support-span check
  piping_bom             line list, pipe + fitting BOM with masses, shop spools
  piping_load            restore a network persisted in a document's studies into the session
  piping_pump_drive      electric motor for a network pump from its solved duty point
  vessel_size            ASME VIII-1 shell + head sizing, hydrotest, masses
  duct_size              equal-friction duct sizing + fan static (critical path)
  run_pipe_flow          internal-flow LBM (durable job) over a fluid volume / pipe-run bore

Networks live in a small session table keyed by NETWORK NAME (default ``"piping"``): every
piping input accepts an inline ``aicad.piping/1`` doc or that name. With ``doc_id`` the
network and every analysis on it persist into the document's ``piping_network`` study (and
vessel / duct / pipe-flow results into ``vessel`` / ``duct_system`` / ``pipe_flow`` studies),
so the work round-trips through the ``.step+`` and ``piping_load`` brings it back.

Registration is deferred behind :func:`register_piping_tools` so ``server.py`` wires us in with a
tiny fenced block, exactly like ``register_ecad_tools`` / ``register_cam_tools``.

CONFORMANCE: ``piping_cfd_handoff`` and ``piping_verify_cfd`` conform to
``aicad.simhandoff/1`` (contracts/sim_handoff.md) — the generalized cross-sim handoff &
verify contract this pair originally proved. Successful results additionally carry a
validated envelope under the reserved ``simhandoff`` key (built through
``services.mcp.simhandoff``); all pre-existing result fields are unchanged.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from typing import Any, Optional, Union
import time

from services.mcp.tool_registry import model_selectors

# The bore-extraction leg (OCCT sweep) stays Python until a kernel leaf ports it —
# engine_oracle/piping/cfd_handoff.py is NOT deleted in this round.
from services.piping import cfd_handoff as piping_cfd_mod
from services.sim import jobs as simjobs

from . import core_bridge as cb
from .cad_common import (
    _autosave_all_open,
    _doc,
    _persist_study,
    register_durable_collector,
    solve_durable_or_handoff,
)
from services.mcp import simhandoff as sh
from services.mcp._envelopes import plain_dumps as _dumps, reason_err as _err

# Session bookkeeping: the last solved doc + result so piping_report can be called with no args,
# and the name of the session network (see _NETWORKS) that doc belongs to.
_PIPING: dict = {"doc": None, "result": None, "boundary": None, "fluid": None, "pumps": None,
                 "network": None}


# ------------------------------------------------------- unaddressable nodes (live fix)
# Measured 2026-07-30 driving cad_guide("piping")'s numbered procedure: node ids are DERIVED
# by snapping coincident run endpoints, and the only place they are published is a successful
# solve's `node_menu`. Guessing {"node":"n2"} on a two-node network refuses with
#   "boundary reference {'demand': 20.0, 'node': 'n2'} did not resolve to a node"
# and a hint naming the alternative shapes but not a single addressable node — so the caller
# is told to fix a name it had no way to learn. The run endpoints ARE knowable from the doc
# the caller just passed, so publish them on the refusal instead of only on success.
_NODE_REFUSAL_MARK = "did not resolve to a node"


def _with_run_endpoints(res: dict, doc: Any) -> dict:
    """Attach the doc's addressable run endpoints to a node-resolution refusal."""
    if res.get("ok") or _NODE_REFUSAL_MARK not in str(res.get("reason", "")):
        return res
    if not isinstance(doc, dict):
        return res
    menu = []
    for run in doc.get("runs") or []:
        if not isinstance(run, dict) or run.get("suppressed"):
            continue
        path = run.get("path") or []
        if len(path) < 2:
            continue
        rid = run.get("id")
        menu.append({"run": rid, "end": "start", "xyz": list(path[0])})
        menu.append({"run": rid, "end": "end", "xyz": list(path[-1])})
    if menu:
        res["addressable_endpoints"] = menu
        res["hint"] = (str(res.get("hint") or "") +
                       " Node ids only exist after a solve; address these run endpoints "
                       "instead, e.g. {\"run\": \"%s\", \"end\": \"%s\", \"pressure\": <kPa>}."
                       % (menu[0]["run"], menu[0]["end"])).strip()
    return res


def _load_doc(doc: Union[str, dict, None]) -> Any:
    """Accept a piping doc as a dict, a JSON string, or a path to a ``.json`` file."""
    if doc is None:
        return None
    if isinstance(doc, dict):
        return doc
    if isinstance(doc, str):
        s = doc.strip()
        if s and s[0] != "{" and os.path.isfile(s):
            with open(s) as fh:
                return json.load(fh)
        return json.loads(s)
    raise TypeError(f"doc must be a dict, JSON string, or file path (got {type(doc).__name__})")


def _as_obj(x: Union[str, list, dict, None]) -> Any:
    """Parse a boundary/fluid/pumps argument that may arrive as a JSON string or native object."""
    if x is None or isinstance(x, (list, dict)):
        return x
    if isinstance(x, str):
        s = x.strip()
        return json.loads(s) if s else None
    return x


def network_from_handoff(env: Any) -> "tuple[Optional[dict], Optional[list], Optional[dict], list]":
    """Extract (doc, boundary, fluid, errors) from an ``aicad.simhandoff/1`` handoff.

    The hydraulic→piping coupling (and any future doc-to-doc producer) carries the
    generated ``aicad.piping/1`` document INLINE in a geometry ref of ``kind:"doc"``
    and the boundary conditions + fluid in the envelope's ``meta`` — this helper is
    the consumer side of that shape. Module-level so tests can drive it engine-less.
    """
    errs: list = []
    if not isinstance(env, dict):
        return None, None, None, ["source_handoff must be an envelope object"]
    if env.get("schema") != sh.SCHEMA and isinstance(env.get("simhandoff"), dict):
        env = env["simhandoff"]  # accept a whole producer result too
    verrs = sh.validate_envelope(env)
    if verrs:
        return None, None, None, [f"source_handoff invalid: {e}" for e in verrs]
    if env.get("kind") != "handoff":
        return None, None, None, ["source_handoff must be a handoff envelope"]
    if env.get("coupling") != "hydraulic_to_piping":
        errs.append("source_handoff coupling must be 'hydraulic_to_piping'")
    source = env.get("source") or {}
    target = env.get("target") or {}
    if source.get("domain") != "hydraulic":
        errs.append("source_handoff source.domain must be 'hydraulic'")
    if target.get("domain") != "piping" or target.get("tool") != "piping_solve":
        errs.append("source_handoff target must identify piping/piping_solve")
    doc = None
    for g in env.get("geometry", []):
        if g.get("kind") == "doc" and isinstance(g.get("inline"), dict):
            doc = g["inline"]
            break
    if doc is None:
        errs.append("source_handoff carries no inline piping doc "
                    "(geometry ref kind 'doc' with an inline payload)")
    meta = env.get("meta") or {}
    boundary = meta.get("boundary")
    fluid = meta.get("fluid")
    if boundary is None:
        errs.append("source_handoff.meta.boundary missing")
    return doc, boundary, fluid, errs


def _schema_errors_via_core(d: dict) -> list:
    """Validate an ``aicad.piping/1`` doc through the core validator.
    Python schema helpers remain in ``services.piping.schema`` for handoff consumers.
    A boundary-less ``piping_solve`` returns ``errors`` for an invalid doc, or the
    boundary-conditions error (without ``errors``) for a valid one."""
    r = cb.call("piping_solve", {"doc": d})
    if isinstance(r, dict) and not r.get("ok") and isinstance(r.get("errors"), list):
        return r["errors"]
    return []


def _handoff_envelope(solve_res: dict, bc_cfd: dict, step_path: Optional[str],
                      volume_mm3: Optional[float], bore_doc_id: Optional[str]) -> dict:
    """Build the aicad.simhandoff/1 handoff envelope for the piping→CFD coupling.

    Pure translation of already-computed pieces (1D solve, CFD BC extraction, bore
    STEP) into the contract shape — contracts/sim_handoff.md §6 is this envelope.
    Module-level (not a closure) so tests can exercise it engine-less with mock dicts.
    """
    fluid = solve_res.get("fluid") or {}
    total_dp = sum(abs(lk.get("dp_kpa", 0.0)) for lk in solve_res.get("links", []))
    geometry = []
    if step_path:
        geometry.append(sh.geometry_ref(
            "fluid_domain", "step", path=step_path, doc_id=bore_doc_id,
            derived_by="piping_internal_volume", units="mm",
            meta={"volume_mm3": volume_mm3} if volume_mm3 is not None else None))
    conditions = [
        sh.condition("inlet_velocity", "velocity", bc_cfd["inlet_velocity_ms"], "m/s",
                     f"1D solve link {bc_cfd.get('inlet_link')} velocity_ms",
                     applied_to="inlet"),
        sh.condition("inlet_pressure", "pressure", bc_cfd["inlet_pressure_kpa"], "kPa",
                     "1D solve inlet node pressure_kpa", applied_to="inlet"),
        sh.condition("outlet_pressure", "pressure", bc_cfd["outlet_pressure_kpa"], "kPa",
                     f"1D solve node {bc_cfd.get('outlet_node')} pressure_kpa",
                     applied_to="outlet"),
    ]
    expectations = [sh.expectation(
        "pressure_drop", bc_cfd["pressure_drop_1d_kpa"], "kPa",
        "1D inlet−outlet pressure (cfd_boundary_from_solve pressure_drop_1d_kpa)")]
    return sh.build_handoff(
        "piping_to_cfd",
        sh.study_ref("piping", "piping_solve",
                     result_digest={"converged": solve_res.get("converged"),
                                    "total_dp_kpa": round(total_dp, 3),
                                    "fluid": fluid.get("name") or fluid}),
        sh.study_ref("cfd", "run_cfd_flow", doc_id=bore_doc_id),
        geometry=geometry, conditions=conditions, expectations=expectations)


def _verify_envelope(result: dict) -> dict:
    """Build the aicad.simhandoff/1 verify envelope from a verify_pressure_drop result.

    Pure translation — the pass/deviation/verdict numbers are the existing ones,
    re-expressed in the contract's check shape (contracts/sim_handoff.md §6).
    """
    checks = [sh.check("pressure_drop", "kPa",
                       result["dp_1d_kpa"], result["dp_3d_kpa"],
                       result["deviation_pct"], result["tolerance_pct"],
                       result["pass"])]
    viol = (result.get("velocity_limits") or {}).get("violations") or []
    advisories = ([{"name": "velocity_limit", "detail": result["velocity_limits"]}]
                  if viol else None)
    return sh.build_verify(
        "piping_to_cfd",
        sh.study_ref("piping", "piping_solve"),
        sh.study_ref("cfd", "run_cfd_flow"),
        checks, result.get("verdict", ""),
        advisories=advisories,
        warnings=result.get("warnings") or None)


# ------------------------------------------------------- named networks + persistence
#: Session networks by name -> {"doc", "result", "boundary", "fluid", "pumps", "design",
#: "spec_check", "bom"}. An analysis is only kept while the network's doc is unchanged.
_NETWORKS: dict = {}
_DEFAULT_NETWORK = "piping"
#: Study types/engines a piping / industrial result persists under (doc `studies[]`).
_NETWORK_STUDY = "piping_network"
_INDUSTRIAL_STUDIES = ("vessel", "duct_system", "pipe_flow")
_PIPE_FLOW_KIND = "pipe_flow"
#: Envelope keys that describe the call rather than the result — never stored in a study.
_ENVELOPE_KEYS = ("ok", "hint", "next_actions")


def _rpc(method: str, params: dict) -> dict:
    """One native RPC as a contract-shaped dict; an engine failure is a refusal, never a raise."""
    try:
        res = cb.call(method, params)
    except Exception as exc:  # noqa: BLE001 — contract-shaped soft-fail
        return _err("engine_unavailable",
                    hint=f"{method} could not run in the core engine: {exc}")
    if not isinstance(res, dict):
        return _err("engine_bad_reply", hint=f"{method} returned {res!r}")
    return res


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _body(res: dict) -> dict:
    """A result minus its call envelope — what a study stores."""
    return {k: v for k, v in res.items() if k not in _ENVELOPE_KEYS}


def _active_runs(doc: dict) -> list:
    return [r for r in (doc.get("runs") or [])
            if isinstance(r, dict) and not r.get("suppressed") and len(r.get("path") or []) >= 2]


def _remember(name: str, doc: dict, **fields: Any) -> dict:
    """Make ``doc`` the session network ``name``; analyses of a different doc are dropped."""
    entry = _NETWORKS.get(name)
    if entry is None or entry.get("doc") != doc:
        entry = {"doc": doc}
        _NETWORKS[name] = entry
    entry.update(fields)
    if _PIPING.get("doc") != doc:
        _PIPING.update({"doc": doc, "result": None, "boundary": None, "fluid": None,
                        "pumps": None})
    _PIPING["network"] = name
    return entry


def _resolve_piping(piping: Union[str, dict, None],
                    network: Optional[str]) -> "tuple[Optional[dict], Optional[str], Optional[dict]]":
    """``(doc, network_name, refusal)`` for a piping input.

    ``piping`` is an inline ``aicad.piping/1`` doc (dict / JSON / ``.json`` path), a whole
    ``piping_route``/``piping_load`` result (its ``piping`` key), the NAME of a session
    network, or None (the session network ``network``, else the last one used).
    """
    if piping is None:
        name = network or _PIPING.get("network")
        entry = _NETWORKS.get(name) if name else None
        if entry and isinstance(entry.get("doc"), dict):
            return entry["doc"], name, None
        if network is None and isinstance(_PIPING.get("doc"), dict):
            return _PIPING["doc"], _DEFAULT_NETWORK, None
        return None, None, _err(
            "no_piping",
            hint=("pass piping=<aicad.piping/1 doc> or the name of a session network "
                  "(piping_route / piping_solve / piping_load create one); with doc_id a "
                  "network saved in that document is restored automatically"),
            session_networks=sorted(_NETWORKS))
    if isinstance(piping, str):
        key = piping.strip()
        entry = _NETWORKS.get(key)
        if entry and isinstance(entry.get("doc"), dict):
            return entry["doc"], network or key, None
        if key and key[0] not in "{[" and not os.path.isfile(key):
            return None, None, _err(
                "unknown_network",
                hint=(f"{key!r} is neither a session network nor a piping JSON file; session "
                      f"networks: {sorted(_NETWORKS) or 'none'} — piping_load(doc_id) "
                      "restores one saved in a document"),
                session_networks=sorted(_NETWORKS))
    try:
        d = _load_doc(piping)
    except (ValueError, TypeError, OSError) as exc:
        return None, None, _err("bad_piping", hint=f"could not parse piping: {exc}")
    if isinstance(d, dict) and d.get("schema") is None and isinstance(d.get("piping"), dict):
        d = d["piping"]
    if not isinstance(d, dict):
        return None, None, _err("bad_piping", hint="piping must be an aicad.piping/1 object")
    return d, network or _DEFAULT_NETWORK, None


def _target_doc(doc_id: Optional[str]) -> "tuple[Optional[str], Optional[dict]]":
    """Resolve an OPTIONAL persistence target; no doc_id means nothing is persisted."""
    if not doc_id:
        return None, None
    try:
        return _doc(doc_id), None
    except Exception as exc:  # noqa: BLE001 — DocRefError names the open documents
        return None, _err("unknown_doc", hint=str(exc))


def _studies(doc_id: str) -> list:
    try:
        res = cb.call("sim_study_list", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001 — an unreadable study list reads as empty
        return []
    return [s for s in (res.get("studies") or []) if isinstance(s, dict)]


def _find_study(doc_id: str, stype: str, name: str) -> Optional[dict]:
    for s in _studies(doc_id):
        if s.get("type") == stype and s.get("name") == name:
            return s
    return None


def _saved(doc_id: str, sid: Optional[str]) -> dict:
    out: dict = {"doc_id": doc_id, "saved_to_study": bool(sid)}
    if sid:
        out["study_id"] = sid
    else:
        out["study_not_saved_reason"] = (
            f"the result could not be written into document {doc_id}'s studies; the "
            "numbers above are unaffected")
    return out


def _not_saved(res: dict, stype: str) -> dict:
    """A sizing run without ``doc_id``: say it was NOT saved and how to save it.

    The engine's own next_actions tell the caller to persist with ``sim_study_add`` —
    true for the raw RPC, wrong for this wrapper, which persists itself when given a
    ``doc_id``. Models followed neither and delivered numbers nobody could reopen.
    """
    res["saved_to_study"] = False
    res["study_not_saved_reason"] = (
        f"no doc_id — this {stype} sizing is not saved anywhere the user can open it. "
        "Re-run with doc_id=<the part> (no document yet: new_part(name=<system name>) "
        f"first); the result is then a {stype} study, and save_part writes it into the "
        ".step+.")
    res["next_actions"] = [a for a in res.get("next_actions") or []
                           if "sim_study_add" not in str(a)]
    return res


def _restore_network(name: str, study: dict) -> dict:
    """Load a persisted ``piping_network`` study into the session as network ``name``."""
    params = study.get("params") or {}
    results = study.get("results") or {}
    entry = _remember(name, params["piping"], design=params.get("design"),
                      boundary=params.get("boundary"), fluid=params.get("fluid"),
                      pumps=params.get("pumps"), result=results.get("solve"),
                      spec_check=results.get("spec_check"), bom=results.get("bom"),
                      pump_drive=results.get("pump_drive"))
    if isinstance(results.get("solve"), dict):
        _PIPING.update({"result": results["solve"], "boundary": params.get("boundary"),
                        "fluid": params.get("fluid"), "pumps": params.get("pumps")})
    return entry


def _piping_input(piping: Union[str, dict, None], network: Optional[str],
                  target: Optional[str]) -> "tuple[Optional[dict], Optional[str], Optional[dict]]":
    """:func:`_resolve_piping`, falling back to the network saved in ``target``'s studies."""
    d, net, refusal = _resolve_piping(piping, network)
    if refusal is None or target is None or refusal["reason"] not in ("no_piping",
                                                                      "unknown_network"):
        return d, net, refusal
    name = network or (piping.strip() if isinstance(piping, str) else None) or _DEFAULT_NETWORK
    st = _find_study(target, _NETWORK_STUDY, name)
    if st and isinstance((st.get("params") or {}).get("piping"), dict):
        _restore_network(name, st)
        return st["params"]["piping"], name, None
    return d, net, refusal


def _persist_network(doc_id: str, name: str, doc: dict, *, params: Optional[dict] = None,
                     results: Optional[dict] = None) -> dict:
    """Merge into ``doc_id``'s ``piping_network`` study ``name`` (one study per network).

    A changed network invalidates every analysis of the old one, so prior results are kept
    only while the stored network equals ``doc``; routed-run records accumulate.
    """
    existing = _find_study(doc_id, _NETWORK_STUDY, name) or {}
    old_params = existing.get("params") if isinstance(existing.get("params"), dict) else {}
    old_results = existing.get("results") if isinstance(existing.get("results"), dict) else {}
    if old_params.get("piping") != doc:
        old_results = {k: v for k, v in old_results.items() if k == "routes"}
    rec: dict = {"type": _NETWORK_STUDY, "name": name, "engine": "cpp-piping",
                 "params": {**old_params, **(params or {}), "piping": doc},
                 "results": {**old_results, **(results or {})}}
    if existing.get("id"):
        rec["id"] = existing["id"]
    return _saved(doc_id, _persist_study(doc_id, rec))


def _persist_record(doc_id: str, stype: str, name: str, engine: str, params: dict,
                    results: dict) -> dict:
    return _saved(doc_id, _persist_study(doc_id, {
        "type": stype, "name": name, "engine": engine, "params": params, "results": results}))


def _with_actions(res: dict, actions: list) -> dict:
    res["next_actions"] = list(res.get("next_actions") or []) + actions
    return res


# ------------------------------------------------------- internal pipe flow (durable job)

def _unit(v: list) -> Optional[list]:
    n = math.sqrt(sum(c * c for c in v))
    return [c / n for c in v] if n > 1e-12 else None


def run_flow_planes(run: dict) -> "tuple[dict, dict]":
    """Inlet/outlet planes of a pipe run: its end points, both normals pointing INTO the bore.

    The inlet normal is the first segment's direction; the outlet normal is the reverse of
    the last segment's (into-domain is the native solver's canonical orientation).
    """
    path = [[float(c) for c in p] for p in run.get("path") or []]
    if len(path) < 2:
        raise ValueError(f"run {run.get('id')!r} has fewer than two path points")
    d_in = next((u for u in (_unit([b - a for a, b in zip(path[0], p)]) for p in path[1:])
                 if u), None)
    d_out = next((u for u in (_unit([b - a for a, b in zip(path[-1], p)])
                              for p in reversed(path[:-1])) if u), None)
    if d_in is None or d_out is None:
        raise ValueError(f"run {run.get('id')!r} has zero length")
    return ({"point": path[0], "normal": d_in}, {"point": path[-1], "normal": d_out})


def _solve_defaults(solve: Any, run_id: Optional[str]) -> "tuple[Optional[dict], Optional[float]]":
    """Fluid + the run's mean velocity from a 1-D network solve, when one exists."""
    if not isinstance(solve, dict):
        return None, None
    fluid = solve.get("fluid") if isinstance(solve.get("fluid"), dict) else None
    velocity = None
    for lk in solve.get("links") or []:
        if isinstance(lk, dict) and run_id is not None and lk.get("id") == run_id:
            try:
                velocity = abs(float(lk.get("velocity_ms")))
            except (TypeError, ValueError):
                velocity = None
    return fluid, (velocity if velocity else None)


def _run_solve(kind: str, stage: str, payload: dict, meta: dict, *, background: bool,
               doc_id: Optional[str]) -> dict:
    """Start one durable solve; hand back its id (background) or wait out the sync budget."""
    if background:
        # The shared background reply shape (job id that outlives this process).
        from services.mcp.cad_sim_tools import _started  # noqa: PLC0415
        return _started(simjobs.start(kind, stage, payload, meta), doc_id)
    return solve_durable_or_handoff(kind, stage, payload, meta)


def _collect_pipe_flow(st: dict, raw: dict) -> dict:
    """COLLECT stage for ``sim_run_pipe_flow``: shape the reply, persist the study ONCE."""
    meta = st.get("meta") or {}
    d = meta.get("doc")
    if not isinstance(raw, dict) or not raw.get("ok"):
        raw = raw if isinstance(raw, dict) else {}
        return _drop_none({
            "ok": False, "doc_id": d,
            "reason": raw.get("reason") or "pipe_flow_failed",
            "hint": raw.get("hint") or raw.get("error")
            or "the internal-flow solve returned no result",
            "caveats": raw.get("caveats"), "source": meta.get("source")})
    out = dict(raw)
    out["source"] = meta.get("source")
    if d:
        out.update(_persist_record(
            d, "pipe_flow", meta.get("name") or "Pipe flow", "cpp-lbm-internal",
            {**(meta.get("params") or {}), "source": meta.get("source")}, _body(raw)))
    return out


register_durable_collector(_PIPE_FLOW_KIND, _collect_pipe_flow)


def register_piping_tools(mcp) -> None:
    """Register the AI-Piping tools on the given FastMCP instance.

    Called from ``services/mcp/server.py`` after ``mcp`` is created. A function (not import-time
    side effects) so the tools can be driven against a stub ``mcp`` in tests without importing the
    whole stdio server — mirrors :func:`register_ecad_tools`.
    """

    @mcp.tool()
    def piping_solve(doc: Union[str, dict, None] = None,
                     boundary: Union[str, list, None] = None,
                     fluid: Union[str, dict, None] = None,
                     pumps: Union[str, list, None] = None,
                     network_type: str = "liquid",
                     source_handoff: Union[str, dict, None] = None,
                     doc_id: Optional[str] = None,
                     network: Optional[str] = None) -> str:
        """Solve an ``aicad.piping/1`` pipe network for node pressures/heads and link flows.

        ``doc`` is the piping document (a dict, a JSON string, or a path to a ``.json`` file)
        as built per ``contracts/piping_schema.md`` (runs + fittings). ``boundary`` is a
        list of per-node conditions — each entry names a node (``{"node":"n0"}`` /
        ``{"at":[x,y,z]}`` / ``{"run":"r1","end":"start"|"end"}``) plus ONE of: ``pressure`` (kPa
        gauge) or ``head`` (m) to fix it, or ``demand`` (m³/h, + leaves / − enters) /
        ``demand_lps``. At least one fixed pressure/head node is required.

        ``fluid`` selects the working fluid: ``null`` (water @20 °C), an explicit
        ``{"rho":kg/m³,"mu":Pa·s}`` override, or a **sourced lookup**
        ``{"name":"water"|"air"|"methane"|"natural_gas"|"nitrogen"|"carbon_dioxide","T":K,"P":Pa}``
        (properties come from the core's cited built-in models — IAPWS/NIST water +
        ideal-gas/Sutherland gases; see ``piping_fluid``). ``pumps`` is a list of
        ``{"run":"r1","curve":<CSV text/path>,"speed_ratio":1.0,"direction":"a_to_b"}``.

        ``network_type`` = ``"liquid"`` (incompressible, default) or ``"gas"``
        (isothermal-compressible mass-balance solve; needs a named gas ``fluid``, boundary
        pressures are kPa gauge, demands are ``mass_demand`` kg/h or ``std_demand`` Nm³/h, and
        links report ``mass_flow_kgh`` / ``std_flow_nm3h`` / ``dp_kpa``).

        EXTERNALLY-SUPPLIED NETWORKS: ``source_handoff`` accepts an
        ``aicad.simhandoff/1`` handoff envelope (or a producer result carrying one
        under ``simhandoff``) — e.g. from ``hydraulic_piping_handoff``. The piping
        doc is taken from the envelope's inline ``kind:"doc"`` geometry ref and the
        boundary/fluid from its ``meta``; explicit ``doc``/``boundary``/``fluid``
        arguments override the envelope's. The result then carries ``handoff_id``
        so downstream verify steps can close the provenance chain.

        NETWORKS + PERSISTENCE: ``doc`` may also be the NAME of a session network (from
        ``piping_route`` / ``piping_load``); with no ``doc`` and no handoff the session network
        ``network`` (else the last one used) is solved. ``network`` names where the result is
        kept (default ``"piping"``). With ``doc_id`` the network, its boundary/fluid and this
        solve are saved into that document's ``piping_network`` study (``piping_load``
        restores them after a save/reopen).

        Node ids are derived by snapping coincident run endpoints; the result lists every node
        with its coordinates so you can address boundary conditions. Returns node
        pressures/heads, per-link flow / velocity / Reynolds / friction / pressure-drop, whether
        the solve converged, and the next best action. Never throws for a modelling problem — it
        degrades to ``{"ok": false, "reason", "hint"}``.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        net = network or _DEFAULT_NETWORK
        try:
            if isinstance(doc, str) and doc.strip() in _NETWORKS:
                d, net, refusal = _resolve_piping(doc, network)
            else:
                d = _load_doc(doc)
                if isinstance(d, dict) and d.get("schema") is None and isinstance(
                        d.get("piping"), dict):
                    d = d["piping"]  # a whole piping_route / piping_load result
            bc = _as_obj(boundary)
            fl = _as_obj(fluid)
            pp = _as_obj(pumps)
            env = _as_obj(source_handoff)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}",
                               hint="pass doc as JSON/dict/path and boundary as a JSON list"))
        if d is None and env is None:
            sd_net, sd_name, _miss = _piping_input(None, network, target)
            if sd_net is not None:
                d, net = sd_net, sd_name

        try:
            res = cb.call("piping_solve", {
                "doc": d, "boundary": bc, "fluid": fl, "pumps": pp,
                "network_type": network_type, "source_handoff": env})
        except Exception as exc:  # noqa: BLE001 — contract-shaped soft-fail, never raise
            return _dumps(_err(f"core engine unavailable: {exc}"))

        if res.get("ok"):
            # Session bookkeeping stays MCP-side: remember the RESOLVED doc/boundary/
            # fluid (explicit args override the handoff envelope's, exactly like the
            # core does) so piping_report can be called with no arguments.
            sd, sbc, sfl = d, bc, fl
            if env is not None:
                hdoc, hbc, hfl, _herrs = network_from_handoff(env)
                sd = sd if sd is not None else hdoc
                sbc = sbc if sbc is not None else hbc
                sfl = sfl if sfl is not None else hfl
            solved = dict(res)
            _PIPING.update({"doc": sd, "result": solved, "boundary": sbc,
                            "fluid": sfl, "pumps": pp})
            if isinstance(sd, dict):
                _remember(net, sd, result=solved, boundary=sbc, fluid=sfl, pumps=pp)
                res["network"] = net
                if target:
                    res.update(_persist_network(
                        target, net, sd,
                        params={"boundary": sbc, "fluid": sfl, "pumps": pp,
                                 "network_type": network_type},
                        results={"solve": {k: v for k, v in solved.items()
                                           if k not in ("hint", "next_actions")}}))
        return _dumps(_with_run_endpoints(res, d))

    @mcp.tool()
    def piping_report(doc: Union[str, dict, None] = None,
                      boundary: Union[str, list, None] = None,
                      fluid: Union[str, dict, None] = None,
                      pumps: Union[str, list, None] = None) -> str:
        """Per-segment pressure-drop report with an explicit K-factor breakdown.

        With no arguments, reports on the most recent ``piping_solve``. Otherwise pass the same
        ``doc`` + ``boundary`` (+ optional ``fluid``/``pumps``) and a fresh solve is run first.
        Each segment lists the friction K + dP and every fitting's K + dP (elbow/tee/reducer/
        flange), the segment total, and the whole-network ``total_dp_kpa`` — ready to size a pump
        or hand back as the pressure-drop table. Contract-shaped soft-fail like ``piping_solve``.
        """
        try:
            d = _load_doc(doc)
            bc = _as_obj(boundary)
            fl = _as_obj(fluid)
            pp = _as_obj(pumps)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        if d is None:
            # Session-free reporting: the core takes the prior solve's doc + result
            # explicitly (``piping_report`` accepts an additive ``result`` for this).
            if _PIPING.get("result") is None:
                return _dumps(_err("no prior piping_solve to report on",
                                   hint="call piping_solve first, or pass doc + boundary here"))
            try:
                rep = cb.call("piping_report", {
                    "doc": _PIPING["doc"], "result": _PIPING["result"],
                    "fluid": _PIPING["fluid"]})
            except Exception as exc:  # noqa: BLE001
                return _dumps(_err(f"core engine unavailable: {exc}"))
            return _dumps(rep)

        try:
            rep = cb.call("piping_report", {
                "doc": d, "boundary": bc, "fluid": fl, "pumps": pp})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(_with_run_endpoints(rep, d))

    @mcp.tool()
    def piping_operating_point(pump_curve: str,
                               system: Union[str, dict, None] = None,
                               doc: Union[str, dict, None] = None,
                               boundary: Union[str, list, None] = None,
                               fluid: Union[str, dict, None] = None,
                               suction: Union[str, dict, None] = None,
                               npshr_curve: Union[str, None] = None,
                               q_ref_m3h: Union[float, None] = None,
                               speed_ratio: float = 1.0) -> str:
        """Pump duty (operating) point + NPSH margin: intersect the pump curve with the system curve.

        ``pump_curve`` — a pump-curve CSV (``flow`` m³/h, ``head`` m) as text or a file path.
        ``system`` — ``{"static_head_m":h0,"k":k}`` giving H_sys = h0 + k·Q² (Q in m³/h); if
        omitted, the system curve is **derived** from ``doc`` + ``boundary`` (one fixed
        pressure/head node + one demand node whose value is the operating flow, or pass
        ``q_ref_m3h``). ``fluid`` as in ``piping_solve`` (used for density + vapour pressure).
        ``suction`` — NPSHa inputs ``{"pressure_kpa":gauge,"elevation_m":z,"friction_m":loss,
        "atm_kpa":101.325,"pvap_kpa":<override>}``. ``npshr_curve`` — a ``flow,npshr`` (m) CSV;
        NPSHr is interpolated at the duty flow to give the NPSH margin (NPSHa − NPSHr).

        Returns ``{"duty_point":{flow_m3h,head_m}, "npsh":{npsha_m,npshr_m,margin_m}, ...}`` or a
        contract-shaped failure (e.g. system head exceeds pump shutoff head → no operating point).
        """
        try:
            sysd = _as_obj(system)
            d = _load_doc(doc)
            bc = _as_obj(boundary)
            fl = _as_obj(fluid)
            suc = _as_obj(suction)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        try:
            op = cb.call("piping_operating_point", {
                "pump_curve": pump_curve, "system": sysd, "doc": d, "boundary": bc,
                "fluid": fl, "suction": suc, "npshr_curve": npshr_curve,
                "q_ref_m3h": q_ref_m3h, "speed_ratio": speed_ratio})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(op)

    @mcp.tool()
    def piping_fluid(name: str, T: float = 293.15, P: float = 101325.0) -> str:
        """Sourced thermophysical properties for a named fluid at ``T`` (K), ``P`` (Pa absolute).

        Supported: ``water, air, methane, natural_gas, nitrogen, carbon_dioxide``. Returns density
        (kg/m³), dynamic viscosity (Pa·s), saturation/vapour pressure (Pa; liquids), the backend
        used (the core's cited built-in models) and a ``source`` citation — the same values the
        solver pulls for ``fluid={"name":...}``. Never fabricates: an unknown fluid or
        out-of-range condition returns ``{"ok": false, "reason", "hint"}``.
        """
        try:
            return _dumps(cb.call("piping_fluid", {"name": name, "T": float(T), "P": float(P)}))
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))

    @mcp.tool()
    @model_selectors("run_id")
    def piping_internal_volume(doc: Union[str, dict],
                               out_path: Optional[str] = None,
                               run_id: Optional[str] = None) -> str:
        """Extract the internal fluid-domain solid (bore) from a pipe run and write it as STEP.

        The STEP file is a watertight solid of the fluid passage — suitable for import as a
        CAD part and then handing to ``run_cfd_flow`` / ``run_cfd_accurate`` for internal-flow
        CFD. The bore solid is the swept inner-diameter circle along the centreline (the void
        that the fluid occupies), not the pipe wall.

        ``doc`` is the piping document (dict, JSON string, or path). ``out_path`` is the
        destination STEP file; if omitted a temporary file is created. ``run_id`` restricts
        extraction to one named run (default: all non-suppressed runs).

        Returns ``{ok, step_path, run_ids, volume_mm3, inlet_xyz, outlet_xyz, warnings,
        next_actions}``. Soft-fails with ``{ok: false, reason, hint}`` when the kernel is
        absent or no geometry can be produced — never a bare traceback.
        """
        try:
            d = _load_doc(doc)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse doc: {exc}",
                               hint="pass doc as a dict, JSON string, or file path"))

        if not isinstance(d, dict):
            errs = ["doc is not an object"]
        else:
            try:
                errs = _schema_errors_via_core(d)
            except Exception as exc:  # noqa: BLE001
                return _dumps(_err(f"core engine unavailable: {exc}"))
        if errs:
            return _dumps(_err("piping document failed schema validation",
                               hint="fix the listed errors", errors=errs))

        if out_path is None:
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="piping_bore_")
            out_path = os.path.join(tmp_dir, "fluid_domain.step")

        try:
            result = piping_cfd_mod.internal_volume(d, out_path, run_id=run_id)
        except piping_cfd_mod.PipingCFDError as exc:
            return _dumps(_err(str(exc),
                               hint="install build123d (pip install build123d) or run on a "
                                    "machine with the OCP kernel"))
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"internal_volume failed: {exc}"))

        result.setdefault("next_actions", [
            "import the step_path as a CAD part with import_part(step_path)",
            "run run_cfd_flow on the imported part for internal-flow LBM",
            "or run piping_cfd_handoff to do solve + bore extraction + CFD in one step",
        ])
        return _dumps(result)

    @mcp.tool()
    @model_selectors("run_id")
    def piping_cfd_handoff(doc: Union[str, dict],
                           boundary: Union[str, list],
                           fluid: Union[str, dict, None] = None,
                           pumps: Union[str, list, None] = None,
                           run_id: Optional[str] = None,
                           cells: int = 96,
                           n_steps: int = 2000,
                           background: bool = False) -> str:
        """One-click 3D CFD handoff for pipe internals.

        Runs the full pipeline in one call:
        1. Solves the 1D pipe network (same as ``piping_solve``).
        2. Extracts the internal fluid-domain bore solid as STEP (same as
           ``piping_internal_volume``) — the watertight void the fluid occupies.
        3. Derives CFD inlet/outlet boundary conditions from the 1D result
           (inlet velocity, inlet/outlet pressures).
        4. Imports the bore STEP as a CAD part and invokes ``run_cfd_flow`` (LBM) with the
           1D-derived boundary conditions.

        ``run_cfd_accurate`` (OpenFOAM RANS) is NOT invoked here because it requires
        OpenFOAM to be installed; use ``piping_verify_cfd`` after a manual
        ``run_cfd_accurate`` call if you need RANS accuracy. The LBM path is always
        available and gives fast directional results for comparing configurations.

        ``run_id`` restricts the bore extraction to one named run; if omitted all runs are
        joined. ``cells`` / ``n_steps`` / ``background`` are forwarded to ``run_cfd_flow``.

        Returns ``{ok, solve, boundary_cfd, step_path, cfd, next_actions}`` — all sub-results
        included so ``piping_verify_cfd`` can be called immediately on the returned data.
        Successful results also carry a validated ``aicad.simhandoff/1`` handoff envelope
        under ``simhandoff`` (contracts/sim_handoff.md).
        """
        # ── 1D solve ──────────────────────────────────────────────────────────
        try:
            d = _load_doc(doc)
            bc = _as_obj(boundary)
            fl = _as_obj(fluid)
            pp = _as_obj(pumps)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}",
                               hint="pass doc as JSON/dict/path and boundary as a JSON list"))

        if not isinstance(d, dict):
            return _dumps(_err("piping document failed schema validation",
                               errors=["doc is not an object"]))

        try:
            res = cb.call("piping_solve", {
                "doc": d, "boundary": bc, "fluid": fl, "pumps": pp})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if not res.get("ok"):
            if isinstance(res.get("errors"), list):
                # schema-validation failure — same envelope the Python handler emitted
                return _dumps(_err(res.get("reason", "piping document failed schema "
                                                 "validation"),
                                   errors=res["errors"]))
            if res.get("reason") == "boundary conditions are required":
                return _dumps(res)  # the core's jerr shape matches the old _err exactly
            return _dumps(_with_run_endpoints(
                {"ok": False, "stage": "1d_solve",
                 "reason": res.get("reason", "1D solve failed"),
                 "solve": res}, d))

        # The old handler embedded the RAW solve (no node_menu) — strip the additive
        # key the tool-shaped RPC carries so the embedded payload is byte-identical.
        solve_res = {k: v for k, v in res.items() if k != "node_menu"}
        _PIPING.update({"doc": d, "result": solve_res, "boundary": bc, "fluid": fl, "pumps": pp})

        # ── extract CFD BCs from 1D result ────────────────────────────────────
        try:
            bc_cfd = cb.call("piping_cfd_boundary", {
                "solve_result": solve_res, "inlet_run_id": run_id})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if not bc_cfd.get("ok"):
            return _dumps({"ok": False, "stage": "bc_extraction",
                           "reason": bc_cfd.get("reason"),
                           "solve": solve_res, "boundary_cfd": bc_cfd})

        inlet_v_ms = bc_cfd["inlet_velocity_ms"]

        # ── extract bore solid ────────────────────────────────────────────────
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="piping_cfd_")
        step_path = os.path.join(tmp_dir, "fluid_domain.step")

        try:
            vol_res = piping_cfd_mod.internal_volume(d, step_path, run_id=run_id)
        except piping_cfd_mod.PipingCFDError as exc:
            # Kernel absent: return solve + BCs, note CFD skipped, never traceback
            return _dumps({
                "ok": False,
                "stage": "bore_extraction",
                "reason": str(exc),
                "hint": ("build123d/OCP kernel not available — install build123d to extract "
                         "the bore solid for CFD. The 1D solve and CFD BCs are still valid."),
                "solve": solve_res,
                "boundary_cfd": bc_cfd,
                "cfd": None,
            })
        except Exception as exc:  # noqa: BLE001
            return _dumps({
                "ok": False, "stage": "bore_extraction",
                "reason": f"internal_volume failed: {exc}",
                "solve": solve_res, "boundary_cfd": bc_cfd, "cfd": None,
            })

        # ── import bore STEP as CAD part + invoke run_cfd_flow ────────────────
        # We invoke run_cfd_flow through the MCP bridge (core_bridge) if available;
        # otherwise we record the step_path and BCs so the user can invoke CFD manually.
        try:
            # Import the bore STEP as a new part
            imp = cb.call("import_file", {"path": step_path, "format": "step"})
            bore_doc_id = imp.get("doc_id") if isinstance(imp, dict) else None
        except Exception:  # noqa: BLE001
            bore_doc_id = None

        if bore_doc_id is None:
            # Core bridge not available or import failed — return the STEP + BCs,
            # instruct the caller to import manually then run run_cfd_flow.
            return _dumps(sh.attach({
                "ok": True,
                "stage": "cfd_pending_manual_import",
                "solve": solve_res,
                "boundary_cfd": bc_cfd,
                "step_path": step_path,
                "volume_mm3": vol_res.get("volume_mm3"),
                "cfd": None,
                "next_actions": [
                    f"import_part('{step_path}') to load the bore solid into the CAD session",
                    f"run_cfd_flow(velocity={inlet_v_ms}, fluid='water', cells={cells}) "
                    "on the imported bore part",
                    "then call piping_verify_cfd(solve_result, cfd_result) to compare 1D vs 3D dP",
                ],
                "hint": (
                    "Core bridge not available in this context — import the bore STEP manually "
                    "and call run_cfd_flow. All BCs are in boundary_cfd."
                ),
            }, _handoff_envelope(solve_res, bc_cfd, step_path,
                                 vol_res.get("volume_mm3"), None)))

        # Run CFD on the imported bore part
        try:
            # Call via core_bridge sim_run_flow (what run_cfd_flow uses internally).
            # NB: never `from services.mcp import cad_tools` here — every other tool
            # module imports it as `services.mcp.cad_tools`, and a second module identity
            # would mean a second _PERIODIC_TIMER (the autosave thread-leak class).
            prog_path = os.path.join(tmp_dir, "progress.json")
            cfd_raw = cb.call("sim_run_flow", {
                "doc_id": bore_doc_id,
                "velocity": float(inlet_v_ms) if inlet_v_ms > 0 else 1.0,
                "aoa": 0.0,
                "fluid": "water",
                "cells": cells,
                "n_steps": n_steps,
                "progress_path": prog_path,
            })
            cfd_res = {"ok": cfd_raw.get("ok", False)}
            for k in ("drag_n", "lift_n", "cd", "frontal_area_mm2", "reynolds",
                      "ref_length_mm", "velocity_ms", "aoa_deg", "fluid", "grid",
                      "converged", "residual", "voxel_mm", "min_feature_mm",
                      "min_feature_voxels", "steps_run", "diverged", "warning"):
                if k in cfd_raw:
                    cfd_res[k] = cfd_raw[k]
        except Exception as exc:  # noqa: BLE001
            cfd_res = {"ok": False, "reason": f"run_cfd_flow failed: {exc}"}

        return _dumps(sh.attach({
            "ok": True,
            "solve": solve_res,
            "boundary_cfd": bc_cfd,
            "step_path": step_path,
            "volume_mm3": vol_res.get("volume_mm3"),
            "cfd": cfd_res,
            "next_actions": [
                "call piping_verify_cfd(solve_result=<solve>, cfd_result=<cfd>) "
                "to compare 1D vs 3D pressure drop and check velocity limits",
                "raise cells/n_steps for more accurate CFD (at cost of runtime)",
                "use run_cfd_accurate for RANS-quality results (requires OpenFOAM)",
            ],
        }, _handoff_envelope(solve_res, bc_cfd, step_path,
                             vol_res.get("volume_mm3"), bore_doc_id)))

    @mcp.tool()
    @model_selectors("run_id")
    def piping_verify_cfd(solve_result: Union[str, dict],
                          cfd_result: Union[str, dict],
                          tolerance_pct: float = 20.0,
                          run_id: Optional[str] = None) -> str:
        """DAG verify node: compare 1D pipe-network pressure drop with 3D CFD pressure drop.

        Takes the output of ``piping_solve`` (or the ``solve`` field from
        ``piping_cfd_handoff``) and the output of ``run_cfd_flow`` / ``run_cfd_accurate``
        (or the ``cfd`` field from ``piping_cfd_handoff``) and:

        * Computes the 1D pressure drop budget (sum of all link dp_kpa, or a single
          run when ``run_id`` is given).
        * Derives the CFD pressure drop from ``drag_n`` / ``frontal_area_mm2``
          (or falls back to Cd × ½ρV²).
        * Flags PASS / FAIL at the given ``tolerance_pct`` (default 20 %).
        * Checks every pipe link for velocity limits (> 3 m/s = erosion advisory for
          liquid service).

        If called with no live results, falls back to the most recent ``piping_cfd_handoff``
        result stored in session state. Always returns ``{ok, pass, dp_1d_kpa, dp_3d_kpa,
        deviation_pct, tolerance_pct, verdict, velocity_limits, warnings}``. Successful
        results also carry a validated ``aicad.simhandoff/1`` verify envelope under
        ``simhandoff`` (contracts/sim_handoff.md).
        """
        try:
            sr = _as_obj(solve_result)
            cr = _as_obj(cfd_result)
        except (ValueError, TypeError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}",
                               hint="pass solve_result and cfd_result as dicts or JSON strings"))

        if sr is None:
            # Session fallback stays MCP-side; the core reports the same soft-fail
            # envelope when solve_result is still null.
            sr = _PIPING.get("result")

        try:
            result = cb.call("piping_verify_cfd", {
                "solve_result": sr, "cfd_result": cr,
                "tolerance_pct": float(tolerance_pct), "run_id": run_id})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(result)

    # ----------------------------------------------------------- piping design

    @mcp.tool()
    @model_selectors("ops")
    def piping_edit(ops: Union[str, list], piping: Union[str, dict, None] = None,
                    network: Optional[str] = None, doc_id: Optional[str] = None,
                    tol_mm: Optional[float] = None) -> str:
        """Draw and edit a 3-D pipe network atomically, including connected branches and inline fittings.

        ops is a list of native edits: add_run(path=[[x,y,z],...], nps, schedule),
        split_run(run_id, point), move_vertex(run_id, index, point), set_run(id,...),
        add_fitting(type, at, run_id?, params?, nps2?), update_fitting(id, params),
        remove_run(id), or remove_fitting(id). Positions/tol_mm are mm. Elbows and tees
        are created at bends/junctions automatically. Valve, orifice and custom fittings
        are inline; custom params must include k_model. Any fitting takes
        params.k_model={kind:"constant",K} (both tee paths). A tee takes per-path
        k_model_run (straight-through, charged on run_ids[1]) and k_model_branch
        (charged on branch_run), each on its own pipe's velocity; update_fitting the
        auto-created tee. Unknown params keys refuse unknown_param naming the accepted
        keys. Missing piping starts an empty network, or edits the last/saved network.
        doc_id persists the updated study.
        Returns piping and created/removed/changed ids; an invalid batch changes nothing.
        """
        try:
            edits = _as_obj(ops)
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_input", hint=str(exc)))
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        base, net, refusal = _piping_input(piping, network, target)
        if refusal and (piping is not None or refusal.get("reason") != "no_piping"):
            return _dumps(refusal)
        net = net or network or _DEFAULT_NETWORK
        result = _rpc("piping_edit", _drop_none({"piping": base, "ops": edits, "tol_mm": tol_mm}))
        if not result.get("ok"):
            return _dumps(result)
        updated = result.get("piping")
        if not isinstance(updated, dict):
            return _dumps(_err("engine_bad_reply", hint="piping_edit returned no piping document"))
        _remember(net, updated)
        result["network"] = net
        if target:
            result.update(_persist_network(target, net, updated))
        return _dumps(_with_actions(result, [
            f'piping_fitting_k(fitting=<fitting>) — preview deterministic loss',
            f'piping_solve(doc="{net}", boundary=[...]) — recompute pressures/flows',
            f'piping_bom(piping="{net}") — update line list and fittings',
        ]))

    @mcp.tool()
    def piping_fitting_k(fitting: Union[str, dict], nps: Optional[float] = None,
                         schedule: Optional[str] = None, reynolds: Optional[float] = None,
                         velocity_ms: Optional[float] = None,
                         fluid: Union[str, dict, None] = None,
                         path: Optional[str] = None) -> str:
        """Preview a fitting's deterministic loss coefficient K and Reynolds curve without a CFD solve.

        fitting={type,params,nps?,nps2?}. Supports elbows, tees, reducers, valves
        (params.valve_type), orifices (params.beta/bore_mm), and custom k_model.
        k_model overrides correlations: constant, two_k, three_k, cv, or table; tees also
        take per-path k_model_run / k_model_branch. path="run"|"branch" picks the tee path;
        tees always return paths.run/paths.branch {K, method}.
        Returns K, method, inner_dia_mm, fT, reynolds and a 24-point curve; report
        method with K. nps/schedule resolve the bore; fluid is a name or rho/mu.
        """
        try:
            ft = _as_obj(fitting)
            fl = _as_obj(fluid) if isinstance(fluid, str) and fluid.strip().startswith("{") else fluid
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_input", hint=str(exc)))
        return _dumps(_rpc("piping_fitting_k", _drop_none({
            "fitting": ft, "nps": nps, "schedule": schedule, "reynolds": reynolds,
            "velocity_ms": velocity_ms, "fluid": fl, "path": path})))

    @mcp.tool()
    @model_selectors("fitting_id", "geometry")
    def piping_fitting_k_cfd(fitting: Union[str, dict, None] = None,
                             fitting_id: Optional[str] = None,
                             piping: Union[str, dict, None] = None,
                             network: Optional[str] = None,
                             doc_id: Optional[str] = None, schedule: str = "40",
                             fluid: Union[str, dict, None] = None,
                             reynolds: Union[str, list, None] = None,
                             quality: str = "quick", geometry: Union[str, dict, None] = None,
                             apply: bool = False, background: bool = False,
                             job_id: Optional[str] = None, cancel: bool = False) -> str:
        """Compute a fitting's CFD loss curve K(Re), optionally apply it to the saved network.

        fitting is a spec, or fitting_id selects a network fitting and its run's NPS/
        schedule. quick = two Re points, 12 cells across; standard = three, 16.
        Elbows have generated bore geometry. Other types require geometry={stl_path
        OR doc_id, body?, inlet:{point,normal}, outlet:{point,normal}, inner_dia_mm};
        the geometry must be the closed FLUID volume, not the fitting metal.
        No unsupported fitting is approximated. Returns table k_model, Hooper 2-K fit,
        convergence/provenance and deterministic correlation comparison.
        apply=True requires fitting_id and writes params.k_model using piping_edit;
        doc_id persists it. background=True returns immediately. Poll THIS tool with
        job_id=... (not get_job); cancel=True with job_id stops the active durable
        solver child. The same pipeline and worker RPCs power the GUI.
        """
        if job_id:
            status = _fitting_cfd_rpc("piping_fitting_k_cfd_cancel" if cancel else
                                      "piping_fitting_k_cfd_status", {"job_id": job_id})
            return _dumps(_fitting_cfd_reply(status))
        if cancel:
            return _dumps(_err("invalid_params", hint="cancel requires job_id"))
        try:
            ft, geo, re = _as_obj(fitting), _as_obj(geometry), _as_obj(reynolds)
            fl = _as_obj(fluid) if isinstance(fluid, str) and fluid.strip().startswith("{") else fluid
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_input", hint=str(exc)))
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        base, net = None, network or _DEFAULT_NETWORK
        if fitting_id or apply:
            base, net, refusal = _piping_input(piping, network, target)
            if refusal:
                return _dumps(refusal)
            matches = [f for f in base.get("fittings", []) if f.get("id") == fitting_id]
            if not matches:
                return _dumps(_err("unknown_fitting", hint=f"fitting ids: {[f.get('id') for f in base.get('fittings', [])]}"))
            ft = dict(matches[0])
            run = next((r for r in base.get("runs", []) if r.get("id") == ft.get("run_id")), None)
            if run:
                if ft.get("nps") is None:
                    ft["nps"] = run.get("nps")
                schedule = run.get("schedule") or schedule
        if not isinstance(ft, dict):
            return _dumps(_err("invalid_params", hint="pass fitting or fitting_id with a network"))
        request = _drop_none({"fitting": ft, "schedule": schedule, "fluid": fl,
                             "reynolds": re, "quality": quality, "geometry": geo})
        if apply:
            request["apply_context"] = _drop_none({"piping": base, "fitting_id": fitting_id,
                                                  "network": net, "doc_id": target})
        status = _fitting_cfd_rpc("piping_fitting_k_cfd_start", request)
        if not status.get("ok"):
            return _dumps(status)
        job = status["job_id"]
        if background:
            return _dumps({**status, "state": "running", "next_actions": [
                f'piping_fitting_k_cfd(job_id="{job}") — poll loss curve',
                f'piping_fitting_k_cfd(job_id="{job}", cancel=True) — stop solve']})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = _fitting_cfd_rpc("piping_fitting_k_cfd_status", {"job_id": job})
            if not status.get("ok") or status.get("state") != "running":
                return _dumps(_fitting_cfd_reply(status))
            time.sleep(0.2)
        return _dumps({**status, "next_actions": [f'piping_fitting_k_cfd(job_id="{job}") — poll loss curve']})

    @mcp.tool()
    def piping_route(start: Union[str, dict, list],
                     end: Union[str, dict, list],
                     nps: float,
                     schedule: str = "40",
                     material: Optional[str] = None,
                     obstacles: Union[str, list, None] = None,
                     clearance_mm: float = 50.0,
                     grid_mm: Optional[float] = None,
                     bend_penalty: Optional[float] = None,
                     piping: Union[str, dict, None] = None,
                     name: Optional[str] = None,
                     doc_id: Optional[str] = None,
                     ignore_bodies: Union[str, list, None] = None,
                     network: Optional[str] = None,
                     fresh: bool = False) -> str:
        """Auto-route a new pipe run between two points: orthogonal, obstacle-avoiding, LR elbows at every bend.

        ``start`` / ``end`` are ``{"point":[x,y,z], "dir":[dx,dy,dz]}`` (mm) or a bare
        ``[x,y,z]``. ``dir`` is optional and snapped to the nearest axis: ``start.dir`` = the
        direction the pipe travels LEAVING start, ``end.dir`` = the direction of travel
        ARRIVING at end (for a nozzle with outward normal n, pass ``-n``).
        ``nps`` (inches) + ``schedule`` (+ ``material``, e.g. ``"A106-B"``) size the run.
        ``obstacles`` is a list of axis-aligned boxes ``{"min":[3],"max":[3],"name"?}`` kept
        ``clearance_mm`` clear; with ``doc_id`` every solid body of that document is added as
        an obstacle automatically (minus ``ignore_bodies``, e.g. the nozzles' own equipment).
        ``grid_mm`` (default ≈ max(OD, 25)) and ``bend_penalty`` tune the A* search.

        The run is APPENDED to a network: ``piping`` (inline aicad.piping/1 doc or session
        network name), else the session network ``network`` (default ``"piping"``), else the
        network saved in ``doc_id``; ``fresh=True`` starts a new one. ``name`` names the run.
        With ``doc_id`` the updated network is saved in the document's ``piping_network``
        study. Returns ``{ok, piping, run_id, path, length_mm, bends, min_clearance_mm,
        network, warnings}``; refuses ``no_route`` / ``start_inside_obstacle`` /
        ``end_inside_obstacle`` / ``route_domain_too_large`` / ``unknown_pipe_size`` with a hint.
        """
        try:
            s = _endpoint(_as_obj(start))
            e = _endpoint(_as_obj(end))
            obs = _as_obj(obstacles)
            ign = _as_obj(ignore_bodies)
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_input", hint=f"could not parse inputs: {exc}"))
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        base, net = None, network or _DEFAULT_NETWORK
        if piping is not None or not fresh:
            found, found_name, refusal = _piping_input(
                piping, network if piping is not None else net, target)
            if refusal and (piping is not None or refusal["reason"] != "no_piping"):
                return _dumps(refusal)
            if found is not None:
                base, net = found, found_name
        res = _rpc("piping_route", _drop_none({
            "start": s, "end": e, "nps": nps, "schedule": schedule, "material": material,
            "obstacles": obs or [], "clearance_mm": clearance_mm, "grid_mm": grid_mm,
            "bend_penalty": bend_penalty, "piping": base, "name": name, "doc_id": target,
            "ignore_bodies": ign}))
        if not res.get("ok"):
            return _dumps(res)
        new_doc = res.get("piping")
        if not isinstance(new_doc, dict):
            return _dumps(_err("engine_bad_reply", hint="piping_route returned no piping doc"))
        _remember(net, new_doc)
        res["network"] = net
        if target:
            existing = _find_study(target, _NETWORK_STUDY, net) or {}
            routes = dict((existing.get("results") or {}).get("routes") or {})
            run_ids = {r.get("id") for r in new_doc.get("runs") or [] if isinstance(r, dict)}
            routes = {k: v for k, v in routes.items() if k in run_ids}
            routes[str(res.get("run_id"))] = {
                k: res.get(k) for k in ("path", "length_mm", "bends", "min_clearance_mm")}
            res.update(_persist_network(target, net, new_doc, results={"routes": routes}))
        return _dumps(_with_actions(res, [
            f'piping_spec_check(design_pressure_kpa=<kPa>, piping="{net}") — B31.3 wall/MAWP',
            f'piping_bom(piping="{net}") — line list, BOM, spools',
            f'piping_solve(doc="{net}", boundary=[...]) — flows and pressure drops',
        ]))

    @mcp.tool()
    def piping_spec_check(design_pressure_kpa: float,
                          piping: Union[str, dict, None] = None,
                          design_temp_c: float = 40.0,
                          corrosion_allowance_mm: float = 1.5,
                          mill_tolerance_frac: float = 0.125,
                          joint_factor: float = 1.0,
                          fluid_density_kgm3: float = 1000.0,
                          insulation_kgm: float = 0.0,
                          doc_id: Optional[str] = None,
                          network: Optional[str] = None) -> str:
        """ASME B31.3 pressure-design check of every run (wall, MAWP, utilization), flange ratings and support spans.

        ``piping`` is an inline aicad.piping/1 doc or a session network name (default: the
        session network ``network``, else the last one used; with ``doc_id`` the network saved
        in that document). Per run: B31.3 eq. 3a required wall t = PD/(2(SEW+PY)), Y=0.4, plus
        ``corrosion_allowance_mm`` and ``mill_tolerance_frac`` against the schedule's nominal
        wall, allowable stress for the run material (A106-B, A53-B, A312-TP304/TP316)
        interpolated at ``design_temp_c``, MAWP and utilization; per flange the B16.5 class
        P-T rating; per run the max support span (2.5 mm sag and bending-stress limits with
        pipe + ``fluid_density_kgm3`` contents + ``insulation_kgm``). Pressures kPa gauge.
        With ``doc_id`` the check is saved into the network's ``piping_network`` study.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        d, net, refusal = _piping_input(piping, network, target)
        if refusal:
            return _dumps(refusal)
        design = {"design_pressure_kpa": design_pressure_kpa, "design_temp_c": design_temp_c,
                  "corrosion_allowance_mm": corrosion_allowance_mm,
                  "mill_tolerance_frac": mill_tolerance_frac, "joint_factor": joint_factor,
                  "fluid_density_kgm3": fluid_density_kgm3, "insulation_kgm": insulation_kgm}
        res = _rpc("piping_spec_check", {"piping": d, **design})
        if not res.get("ok"):
            return _dumps(res)
        body = _body(res)
        _remember(net, d, spec_check=body, design=design)
        res["network"] = net
        if target:
            res.update(_persist_network(target, net, d, params={"design": design},
                                        results={"spec_check": body}))
        return _dumps(res)

    @mcp.tool()
    def piping_bom(piping: Union[str, dict, None] = None,
                   max_spool_length_mm: float = 12000.0,
                   doc_id: Optional[str] = None,
                   network: Optional[str] = None) -> str:
        """Piping line list + bill of materials (pipe, elbows, tees, reducers, flanges with masses) + shop spools.

        ``piping`` is an inline aicad.piping/1 doc or a session network name (resolved like
        ``piping_spec_check``). Pipe mass comes from the catalog OD/wall and steel density;
        fittings are counted by type/size/class. Spools break at flanges and at
        ``max_spool_length_mm``; totals carry pipe length, mass, shop and field welds. With
        ``doc_id`` the BOM is saved into the network's ``piping_network`` study.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        d, net, refusal = _piping_input(piping, network, target)
        if refusal:
            return _dumps(refusal)
        res = _rpc("piping_bom", {"piping": d, "max_spool_length_mm": max_spool_length_mm})
        if not res.get("ok"):
            return _dumps(res)
        body = _body(res)
        _remember(net, d, bom=body)
        res["network"] = net
        if target:
            res.update(_persist_network(
                target, net, d, params={"max_spool_length_mm": max_spool_length_mm},
                results={"bom": body}))
        return _dumps(res)

    @mcp.tool()
    def piping_load(doc_id: Optional[str] = None, network: Optional[str] = None) -> str:
        """Restore a piping network saved in a document (with its solve, spec check and BOM) into the session.

        Reads the document's ``piping_network`` studies (written by piping_route /
        piping_solve / piping_spec_check / piping_bom with ``doc_id``). ``network`` picks one
        by name; omitted, the document's only network is loaded (several → refused with their
        names). Afterwards every piping tool can address it by name and ``piping_report``
        works with no arguments when a solve was saved. Also lists the document's saved
        ``vessel`` / ``duct_system`` / ``pipe_flow`` studies.
        """
        try:
            target = _doc(doc_id)
        except Exception as exc:  # noqa: BLE001 — DocRefError names the open documents
            return _dumps(_err("unknown_doc", hint=str(exc)))
        studies = _studies(target)
        nets = [s for s in studies if s.get("type") == _NETWORK_STUDY
                and isinstance((s.get("params") or {}).get("piping"), dict)]
        others = [{"type": s.get("type"), "name": s.get("name"), "study_id": s.get("id"),
                   "results": s.get("results")}
                  for s in studies if s.get("type") in _INDUSTRIAL_STUDIES]
        names = [s.get("name") for s in nets]
        pick = ([s for s in nets if s.get("name") == network] if network
                else (nets if len(nets) == 1 else []))
        if not pick:
            if not nets:
                return _dumps(_err(
                    "no_piping_network",
                    hint=(f"document {target} has no saved piping network; build one with "
                          "piping_route(..., doc_id=...) or piping_solve(..., doc_id=...)"),
                    industrial_studies=others))
            return _dumps(_err(
                "network_not_found" if network else "network_ambiguous",
                hint=f"document {target} saves these networks: {names}; pass network=<name>",
                networks=names))
        st = pick[0]
        name = st.get("name") or _DEFAULT_NETWORK
        _restore_network(name, st)
        params = st.get("params") or {}
        results = st.get("results") or {}
        doc = params["piping"]
        return _dumps({
            "ok": True, "doc_id": target, "network": name, "study_id": st.get("id"),
            "piping": doc,
            "runs": len(doc.get("runs") or []), "fittings": len(doc.get("fittings") or []),
            "design": params.get("design"), "boundary": params.get("boundary"),
            "fluid": params.get("fluid"),
            "restored_results": sorted(k for k, v in results.items() if v is not None),
            "results": results, "industrial_studies": others, "warnings": [],
            "hint": f'network "{name}" is the active session network',
            "next_actions": [
                f'piping_spec_check(design_pressure_kpa=<kPa>, piping="{name}")',
                f'piping_route(start=..., end=..., nps=..., network="{name}", doc_id="{target}")',
                "piping_report() when a solve was restored",
            ],
        })

    @mcp.tool()
    def piping_pump_drive(piping: Union[str, dict, None] = None,
                          run_id: Optional[str] = None,
                          pump_efficiency: Optional[float] = None,
                          speed_rpm: Optional[float] = None,
                          standard: str = "IEC",
                          service_factor: float = 1.15,
                          voltage_v: float = 400.0,
                          phases: int = 3,
                          frequency_hz: float = 50.0,
                          efficiency_class: str = "IE3",
                          flow_m3h: Optional[float] = None,
                          head_m: Optional[float] = None,
                          doc_id: Optional[str] = None,
                          network: Optional[str] = None) -> str:
        """Size the electric motor for a network pump from its solved duty point (pump_drive_size).

        Reads the pump's operating point from the network's last ``piping_solve`` (the pump
        run's ``flow_m3h`` and ``pump_head_m``, fluid density from the solve) and calls
        ``pump_drive_size(kind="centrifugal")``: hydraulic power ρ·g·Q·H, shaft power /
        ``pump_efficiency`` (default 0.7), × ``service_factor`` up the ``standard`` ladder
        (IEC kW / NEMA hp), poles from ``speed_rpm`` (default 1500) at ``frequency_hz``,
        ``efficiency_class`` motor efficiency, input power and full-load current at
        ``voltage_v`` / ``phases``. ``run_id`` picks the pump run when the network has
        several. ``flow_m3h`` + ``head_m`` override the solved point (e.g. a
        piping_operating_point duty point). ``piping`` / ``network`` resolve like
        ``piping_spec_check``; with ``doc_id`` the drive is saved into the network's
        ``piping_network`` study under ``results.pump_drive.<run_id>``.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        explicit = flow_m3h is not None and head_m is not None
        d, net, refusal = _piping_input(piping, network, target)
        if refusal and not explicit:
            return _dumps(refusal)
        entry = _NETWORKS.get(net) if net else None
        solve = (entry or {}).get("result")
        if not isinstance(solve, dict) and target and isinstance(d, dict):
            st = _find_study(target, _NETWORK_STUDY, net) or {}
            if (st.get("params") or {}).get("piping") == d:
                solve = (st.get("results") or {}).get("solve")
        rho = None
        if isinstance(solve, dict) and isinstance(solve.get("fluid"), dict):
            rho = solve["fluid"].get("rho")
        source = "explicit"
        rid = run_id
        if explicit:
            q, h = abs(float(flow_m3h)), abs(float(head_m))
        else:
            if not isinstance(solve, dict):
                return _dumps(_err(
                    "no_solve",
                    hint=(f'network "{net}" has no solve: run piping_solve(doc="{net}", '
                          "boundary=[...], pumps=[{run, curve}], doc_id=...) first, or pass "
                          "flow_m3h + head_m"),
                    network=net))
            pump_runs = {p.get("run") for p in ((entry or {}).get("pumps") or [])
                         if isinstance(p, dict)}
            links = [lk for lk in solve.get("links") or [] if isinstance(lk, dict)]
            pumped = [lk for lk in links if lk.get("id") in pump_runs
                      or abs(float(lk.get("pump_head_m") or 0.0)) > 0]
            if run_id is not None:
                pumped = [lk for lk in pumped if lk.get("id") == run_id]
            if not pumped:
                return _dumps(_err(
                    "no_pump",
                    hint=(f"run {run_id!r} carries no pump in the solve" if run_id else
                          f'the solve of network "{net}" has no pump run; add pumps=[{{"run":'
                          '..,"curve":..}}] to piping_solve, or pass flow_m3h + head_m'),
                    network=net))
            if len(pumped) > 1:
                return _dumps(_err(
                    "pump_ambiguous",
                    hint="the network has several pumps; pass run_id=<one of pump_runs>",
                    pump_runs=[lk.get("id") for lk in pumped]))
            lk = pumped[0]
            rid = lk.get("id")
            q = abs(float(lk.get("flow_m3h") or 0.0))
            h = abs(float(lk.get("pump_head_m") or 0.0))
            source = "piping_solve"
            if q <= 0.0 or h <= 0.0:
                return _dumps(_err(
                    "no_duty_point",
                    hint=(f"pump run {rid!r} solved at flow {q} m³/h, head {h} m — a pump "
                          "delivering nothing has no drive duty; check the boundary and the "
                          "pump direction, or pass flow_m3h + head_m"),
                    run_id=rid))
        res = _rpc("pump_drive_size", _drop_none({
            "kind": "centrifugal", "flow_m3h": q, "head_m": h, "fluid_density_kgm3": rho,
            "pump_efficiency": pump_efficiency, "speed_rpm": speed_rpm,
            "standard": standard, "service_factor": service_factor, "voltage_v": voltage_v,
            "phases": phases, "frequency_hz": frequency_hz,
            "efficiency_class": efficiency_class}))
        if not res.get("ok"):
            return _dumps(res)
        key = rid or "pump"
        point = _drop_none({"flow_m3h": q, "head_m": h, "fluid_density_kgm3": rho,
                            "source": source})
        drive = {"run_id": rid, "operating_point": point, **_body(res)}
        res["run_id"] = rid
        res["operating_point"] = point
        if isinstance(d, dict) and net:
            prior = dict((entry or {}).get("pump_drive") or {})
            if target:
                st = _find_study(target, _NETWORK_STUDY, net) or {}
                if (st.get("params") or {}).get("piping") == d:
                    prior = {**((st.get("results") or {}).get("pump_drive") or {}), **prior}
            drives = {**prior, key: drive}
            _remember(net, d, pump_drive=drives)
            res["network"] = net
            if target:
                res.update(_persist_network(target, net, d, results={"pump_drive": drives}))
        elif target:
            res["saved_to_study"] = False
            res["study_not_saved_reason"] = (
                "no piping network to save the drive with — pass piping=<network> or run "
                "piping_solve(..., doc_id=...) first")
        return _dumps(res)

    # ----------------------------------------------------------- industrial sizing

    @mcp.tool()
    def vessel_size(inner_diameter_mm: float,
                    design_pressure_kpa: float,
                    tan_tan_length_mm: Optional[float] = None,
                    volume_m3: Optional[float] = None,
                    design_temp_c: float = 40.0,
                    material: str = "SA-516-70",
                    corrosion_allowance_mm: float = 3.0,
                    joint_efficiency: float = 0.85,
                    head_type: str = "ellipsoidal_2_1",
                    fluid_density_kgm3: float = 1000.0,
                    fill_fraction: float = 0.8,
                    orientation: str = "vertical",
                    doc_id: Optional[str] = None,
                    name: Optional[str] = None) -> str:
        """Size a pressure vessel to ASME VIII-1: shell + head thickness, MAWP, hydrotest, volume and masses.

        Give ``inner_diameter_mm`` and either ``tan_tan_length_mm`` or a required
        ``volume_m3`` (the length is then solved). Shell UG-27(c)(1) t = PR/(SE−0.6P); heads
        UG-32 for ``head_type`` = ``ellipsoidal_2_1`` | ``hemispherical`` | ``torispherical``;
        nominal thicknesses round up to standard plate after ``corrosion_allowance_mm``.
        ``material``: SA-516-70, SA-285-C, SA-240-304, SA-240-316 (allowable stress at
        ``design_temp_c``); ``joint_efficiency`` E. Hydrotest per UG-99(b). Masses: empty,
        operating (``fluid_density_kgm3`` × ``fill_fraction``) and full of water. Pressures
        kPa gauge. With ``doc_id`` the sizing is saved as a ``vessel`` study (``name``).
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        params = _drop_none({
            "inner_diameter_mm": inner_diameter_mm, "tan_tan_length_mm": tan_tan_length_mm,
            "volume_m3": volume_m3, "design_pressure_kpa": design_pressure_kpa,
            "design_temp_c": design_temp_c, "material": material,
            "corrosion_allowance_mm": corrosion_allowance_mm,
            "joint_efficiency": joint_efficiency, "head_type": head_type,
            "fluid_density_kgm3": fluid_density_kgm3, "fill_fraction": fill_fraction,
            "orientation": orientation})
        res = _rpc("vessel_size", params)
        if res.get("ok") and target:
            res.update(_persist_record(
                target, "vessel", name or f"Vessel ID {inner_diameter_mm:g} mm",
                "cpp-industrial", params, _body(res)))
        elif res.get("ok"):
            _not_saved(res, "vessel")
        return _dumps(res)

    @mcp.tool()
    def duct_size(segments: Union[str, list],
                  method: str = "equal_friction",
                  friction_pa_per_m: float = 1.0,
                  max_velocity_ms: float = 10.0,
                  air_temp_c: float = 20.0,
                  roughness_mm: float = 0.09,
                  doc_id: Optional[str] = None,
                  name: Optional[str] = None) -> str:
        """Size an HVAC/process duct tree (equal friction): diameters, velocities, pressure drops, fan static.

        ``segments`` is a list of ``{"id", "parent"?, "airflow_m3s", "length_m",
        "k_fittings"?, "shape": "round"|"rect", "aspect_ratio"?}`` forming a tree from the fan
        (``parent`` = upstream segment id). Each segment gets the smallest standard round size
        (or rect W×H at ``aspect_ratio``, Huebscher equivalent diameter) meeting
        ``friction_pa_per_m`` and ``max_velocity_ms``; Colebrook friction with
        ``roughness_mm`` and air at ``air_temp_c``. Path losses sum through the tree; the
        worst is the ``critical_path`` and sets ``fan_static_pa``. ``length_m`` and
        ``k_fittings`` are the real developed length and fitting losses; when the user gave
        none, assume a representative layout and state it — 1 m stubs with no fittings make
        ``fan_static_pa`` meaningless (a warning says so). With ``doc_id`` the design is
        saved as a ``duct_system`` study (``name``).
        """
        try:
            segs = _as_obj(segments)
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_input", hint=f"could not parse segments: {exc}"))
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        params = {"segments": segs, "method": method, "friction_pa_per_m": friction_pa_per_m,
                  "max_velocity_ms": max_velocity_ms, "air_temp_c": air_temp_c,
                  "roughness_mm": roughness_mm}
        res = _rpc("duct_size", params)
        if res.get("ok") and isinstance(segs, list) and segs and all(
                isinstance(s, dict) and float(s.get("length_m") or 0) <= 1.0
                and not float(s.get("k_fittings") or 0) for s in segs):
            res.setdefault("warnings", []).append(
                "every segment is <= 1 m long with no fitting losses, so fan_static_pa "
                "is only a per-metre figure: re-run with real (or stated representative) "
                "length_m and k_fittings before quoting a fan static pressure")
        if res.get("ok") and target:
            res.update(_persist_record(target, "duct_system", name or "Duct system",
                                       "cpp-industrial", params, _body(res)))
        elif res.get("ok"):
            _not_saved(res, "duct_system")
        return _dumps(res)

    # ----------------------------------------------------------- internal flow (3-D)

    @mcp.tool()
    @model_selectors("run_id", "body")
    def run_pipe_flow(inlet: Union[str, dict, None] = None,
                      outlet: Union[str, dict, None] = None,
                      stl_path: Optional[str] = None,
                      doc_id: Optional[str] = None,
                      body: Optional[str] = None,
                      piping: Union[str, dict, None] = None,
                      run_id: Optional[str] = None,
                      network: Optional[str] = None,
                      fluid: Union[str, dict, None] = None,
                      density_kgm3: Optional[float] = None,
                      viscosity_pas: Optional[float] = None,
                      mean_velocity_ms: Optional[float] = None,
                      flow_m3s: Optional[float] = None,
                      temp_c: Optional[float] = None,
                      voxel_mm: Optional[float] = None,
                      cells_across: Optional[int] = None,
                      max_steps: Optional[int] = None,
                      roughness_mm: Optional[float] = None,
                      name: Optional[str] = None,
                      background: bool = False) -> str:
        """3-D internal-flow CFD (LBM) through a pipe bore / duct / manifold: pressure drop, Re, regime, profile.

        The solid you give IS the fluid volume. Geometry, one of: ``piping`` (+ ``run_id``) —
        the bore of a pipe run is extracted automatically (inline aicad.piping/1 doc or a
        session network name; ``run_id`` alone uses the session network); ``stl_path`` — a
        fluid-volume STL; or ``doc_id`` (+ ``body``) — a part whose solid is the fluid volume.
        ``inlet`` / ``outlet`` are planes ``{"point":[x,y,z], "normal":[nx,ny,nz]}`` (mm,
        normal pointing into the fluid); from a pipe run they default to the run's ends.
        Velocity inlet + pressure outlet, bounce-back walls.

        Fluid: ``fluid`` name (water, air, methane, natural_gas, nitrogen, co2; ``temp_c``
        optional) or ``density_kgm3`` + ``viscosity_pas`` (also ``fluid={"rho","mu"}``). Rate:
        ``mean_velocity_ms`` or ``flow_m3s``. From a solved network (piping_solve) both
        default to that run's fluid and velocity; otherwise fluid defaults to water.
        ``voxel_mm`` (or ``cells_across`` = voxels across the hydraulic diameter; default: the
        engine picks 12 for a straight turbulent run, 16 when the path bends, 20+ laminar) and
        ``max_steps`` default to the engine's resolution and convergence budget;
        ``roughness_mm`` only feeds the Darcy and 1-D cross-checks (default smooth, like the
        lattice walls).

        Runs as a durable job (like run_cfd_flow): ``background=True`` returns a ``job_id`` at
        once (poll ``get_job`` / ``sim_status``; ``sim_cancel`` stops it); otherwise the call
        waits its sync budget and hands off a job id if the solve outlasts it. Returns
        ``{dp_pa, mean_velocity_ms, reynolds, regime, hydraulic_diameter_mm, mass_imbalance,
        converged, pressure_profile, darcy_estimate_pa, caveats, provenance}``.
        ``darcy_estimate_pa`` is straight-pipe friction only. From a single pipe run
        (``piping`` + ``run_id``) the result also carries ``one_d_estimate_pa`` (Darcy friction
        + the run's elbow/tee/reducer/flange K-factors, as piping_solve computes them),
        ``cfd_over_one_d`` and ``one_d_breakdown {friction_pa, fittings:[{id,type,K,dp_pa}]}``
        — compare dp_pa against that, and read ``bend_loss_band`` for the model's measured
        bend over-prediction at the resolution used. A run's straight end legs are extended
        to 5 D for the solve (``extended_legs``); ``dp_pa`` is then between the run's own end
        planes and ``dp_domain_pa`` spans the extended domain. With a ``doc_id`` the result
        is saved as a ``pipe_flow`` study (``name``).
        """
        try:
            inl = _as_obj(inlet)
            outl = _as_obj(outlet)
            fl = _as_obj(fluid) if isinstance(fluid, str) and fluid.strip()[:1] == "{" else fluid
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_input", hint=f"could not parse inputs: {exc}"))
        from_piping = piping is not None or run_id is not None
        if from_piping and stl_path:
            return _dumps(_err("conflicting_geometry",
                               hint="give ONE geometry: piping/run_id, stl_path, or doc_id"))
        step: Optional[str] = None
        default_fluid: Optional[dict] = None
        default_v: Optional[float] = None
        if from_piping:
            persist, refusal = _target_doc(doc_id)
            if refusal:
                return _dumps(refusal)
            d, net, refusal = _piping_input(piping, network, persist)
            if refusal:
                return _dumps(refusal)
            runs = _active_runs(d)
            if run_id is not None:
                sel = [r for r in runs if r.get("id") == run_id]
                if not sel:
                    return _dumps(_err("unknown_run", hint=f"run ids in network {net!r}: "
                                       f"{[r.get('id') for r in runs]}"))
            elif len(runs) == 1:
                sel, run_id = runs, runs[0].get("id")
            elif inl is None or outl is None:
                return _dumps(_err(
                    "run_id_required",
                    hint=(f"network {net!r} has {len(runs)} runs "
                          f"{[r.get('id') for r in runs]}; pass run_id=, or explicit inlet + "
                          "outlet planes to simulate the joined network")))
            else:
                sel = runs
            if not sel:
                return _dumps(_err("no_runs", hint=f"network {net!r} has no routed runs"))
            if inl is None or outl is None:
                try:
                    p_in, p_out = run_flow_planes(sel[0])
                except ValueError as exc:
                    return _dumps(_err("degenerate_run", hint=str(exc)))
                inl = inl if inl is not None else p_in
                outl = outl if outl is not None else p_out
            step = os.path.join(tempfile.mkdtemp(prefix="pipe_flow_"), "fluid_domain.step")
            try:
                vol = piping_cfd_mod.internal_volume(d, step, run_id=run_id)
            except piping_cfd_mod.PipingCFDError as exc:
                return _dumps(_err("bore_extraction_unavailable", hint=(
                    f"{exc} — or export the fluid volume yourself and pass stl_path")))
            except Exception as exc:  # noqa: BLE001
                return _dumps(_err("bore_extraction_failed", hint=str(exc)))
            if not vol.get("ok"):
                return _dumps(vol)
            entry = _NETWORKS.get(net) or {}
            if entry.get("doc") == d:
                default_fluid, default_v = _solve_defaults(entry.get("result"), run_id)
            source = {"piping_network": net, "run_id": run_id,
                      "volume_mm3": vol.get("volume_mm3")}
            label = run_id or net
        elif stl_path:
            persist, refusal = _target_doc(doc_id)
            if refusal:
                return _dumps(refusal)
            stl_path = os.path.abspath(os.path.expanduser(stl_path))  # the child's cwd differs
            source = {"stl_path": stl_path}
            label = os.path.basename(stl_path)
        else:
            # The child process shares no document table, so the solid travels as a file
            # it re-opens (jobs._open_geometry) — exactly like run_cfd_flow.
            from services.mcp.cad_sim_tools import _require_sim_geometry  # noqa: PLC0415
            try:
                persist = _doc(doc_id)
                _require_sim_geometry(persist)
                fd, step = tempfile.mkstemp(suffix=".step", prefix="pipe_flow_")
                os.close(fd)
                cb.call("doc_export_step", {"doc_id": persist, "path": step})
            except Exception as exc:  # noqa: BLE001
                return _dumps(_err("no_geometry", hint=(
                    f"{exc} — pass piping+run_id, stl_path, or the doc_id of a part whose "
                    "solid is the fluid volume")))
            source = {"doc_id": persist, "body": body}
            label = body or persist
        if not isinstance(inl, dict) or not isinstance(outl, dict):
            return _dumps(_err("missing_boundary_planes", hint=(
                'pass inlet={"point":[x,y,z],"normal":[nx,ny,nz]} and outlet likewise (mm, '
                "normals into the fluid) — they default only for a pipe run")))

        if isinstance(fl, dict):
            density_kgm3 = density_kgm3 if density_kgm3 is not None else fl.get(
                "rho", fl.get("density_kgm3"))
            viscosity_pas = viscosity_pas if viscosity_pas is not None else fl.get(
                "mu", fl.get("viscosity_pas"))
            fl = fl.get("name")
        if fl is None and density_kgm3 is None and viscosity_pas is None:
            if default_fluid and default_fluid.get("rho") and default_fluid.get("mu"):
                density_kgm3, viscosity_pas = default_fluid["rho"], default_fluid["mu"]
                fl = default_fluid.get("name")
            elif default_fluid and default_fluid.get("name"):
                fl = default_fluid["name"]
            else:
                fl = "water"
        if mean_velocity_ms is None and flow_m3s is None:
            if default_v is None:
                return _dumps(_err("missing_flow_rate", hint=(
                    "pass mean_velocity_ms or flow_m3s (or piping_solve the network first so "
                    "the run's velocity is known)")))
            mean_velocity_ms = default_v

        # One run of a piping document: the engine adds the fittings-aware 1-D comparison
        # (one_d_estimate_pa, cfd_over_one_d) and picks the bend resolution from its elbows.
        one_d_src = from_piping and run_id is not None and len(sel) == 1
        params = _drop_none({
            "stl_path": stl_path, "body": body if step and not from_piping else None,
            "inlet": inl, "outlet": outl, "fluid": fl, "temp_c": temp_c,
            "density_kgm3": density_kgm3, "viscosity_pas": viscosity_pas,
            "mean_velocity_ms": mean_velocity_ms, "flow_m3s": flow_m3s, "voxel_mm": voxel_mm,
            "max_steps": int(max_steps) if max_steps is not None else None,
            "cells_across": int(cells_across) if cells_across is not None else None,
            "roughness_mm": roughness_mm, "progress_path": simjobs.PROGRESS_TOKEN,
            "piping": d if one_d_src else None, "run_id": run_id if one_d_src else None})
        payload: dict = {"method": "sim_run_pipe_flow", "params": params,
                         "progress_file": simjobs.PROGRESS_TOKEN}
        if step:
            payload["open_step"] = step
        meta = {"analysis": _PIPE_FLOW_KIND, "doc": persist,
                "name": name or f"Pipe flow ({label})", "source": source,
                "params": {k: v for k, v in params.items()
                           if k not in ("progress_path", "piping")}}
        _autosave_all_open()  # protect every open doc before a long solve starts
        try:
            return _dumps(_run_solve(_PIPE_FLOW_KIND, "core_rpc", payload, meta,
                                     background=background, doc_id=persist))
        except ValueError as exc:
            return _dumps(_err("invalid_setup", hint=str(exc)))
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err("job_start_failed", hint=f"could not start the solve: {exc}"))


def _endpoint(x: Any) -> dict:
    """A route end: ``{"point":[3], "dir"?:[3]}`` or a bare ``[x, y, z]``."""
    if isinstance(x, (list, tuple)) and len(x) == 3:
        return {"point": [float(c) for c in x]}
    if isinstance(x, dict) and "point" in x:
        return x
    raise TypeError("start/end must be {point:[x,y,z], dir?:[dx,dy,dz]} or [x,y,z]")


def _fitting_cfd_rpc(method: str, params: dict) -> dict:
    # Standalone MCP's explicit legacy mode has no hostd services dispatcher.
    # Its long-lived server can still use the same registry and durable solvers.
    if cb.active_daemon_mode():
        return _rpc(method, params)
    from services.piping.worker_rpc import WORKER_METHODS
    return WORKER_METHODS[method](params)


def _fitting_cfd_reply(status: dict) -> dict:
    result = status.get("result")
    if isinstance(result, dict):
        if result.get("applied") and isinstance(result.get("piping"), dict):
            _remember(result.get("network") or _DEFAULT_NETWORK, result["piping"])
        return {**result, "job_id": status.get("job_id"), "state": status.get("state")}
    return status
