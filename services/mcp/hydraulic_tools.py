"""AI-Hydraulic MCP tools — ISO 1219 hydraulic circuit design + component sizing.

Same design contract as ``piping_tools`` (design principle: "the AI is extremely limited; its tools
must do the bookkeeping and return rich, actionable context"). Each tool returns a JSON string
``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}`` produced by the C++ core
(the tool envelopes are owned by ``core/hydraulic`` behind the libchudware RPCs
``hydraulic_*`` — reached via :mod:`services.mcp.core_bridge`); the MCP layer keeps only input
parsing and its tiny session state so tools can reuse the last active circuit document.

Tools:
  hydraulic_new             create a blank or native-template aicad.hydraulic/1 circuit
  hydraulic_templates       list native starter circuits and their component/hose counts
  hydraulic_add_component   add an ISO 1219 component to the active circuit
  hydraulic_add_hose        add a hose/line net connecting two or more component ports
  hydraulic_size_cylinder   size a hydraulic cylinder (force, flow, cycle time)
  hydraulic_size_pump       size a hydraulic pump (displacement, power, torque)
  hydraulic_size_motor      size a hydraulic motor (displacement, catalog step, flow, power)
  pump_drive_size           electric motor for a hydraulic or centrifugal pump (IEC / NEMA)
  hydraulic_simulate        lumped-parameter transient of the circuit under a valve schedule
  hydraulic_bom             extract a sourcing BOM from the active circuit
  hydraulic_layout_3d       place circuit components in the shared shaded 3D layout (mm)
  hydraulic_load            restore a circuit persisted in a document's studies
  hydraulic_piping_handoff  hose/tube runs -> piping 1D solver (real per-line pressure drops)
  hydraulic_verify_piping   cross-check per-hose estimates vs piping solve + pump margin
  hydraulic_fea_handoff     cylinder mount loads at max pressure -> static FEA load cases
  hydraulic_verify_fea      cross-check envelope mount forces vs independent recompute

With ``doc_id`` the circuit and every analysis on it (sizing, bom, simulate) persist into the
document's ``hydraulic_circuit`` study (one per circuit name), so the work round-trips through
the ``.step+`` and ``hydraulic_load`` brings it back. Editing the circuit drops the stored
simulate / bom results (they described the old circuit); hand-calc sizing is kept.

Registration is deferred behind :func:`register_hydraulic_tools` so ``server.py`` wires us in
with a tiny fenced block — mirrors :func:`register_piping_tools`.

CONFORMANCE: ``hydraulic_piping_handoff`` / ``hydraulic_verify_piping`` and
``hydraulic_fea_handoff`` / ``hydraulic_verify_fea`` conform to ``aicad.simhandoff/1``
(contracts/sim_handoff.md — couplings ``hydraulic_to_piping`` and ``hydraulic_to_fea``).
Successful results carry a validated envelope under the reserved ``simhandoff`` key
(built exclusively through ``services.mcp.simhandoff``); pre-existing fields unchanged.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from typing import Any, Optional, Union

from . import core_bridge as cb
from .cad_common import _doc, _persist_study, _project_dir
from services.mcp import simhandoff as sh
from services.mcp._envelopes import plain_dumps as _dumps, reason_err as _err

# Session bookkeeping: the last active circuit document
_HYD: dict = {"doc": None}


# ------------------------------------------------------- component-type dialect (live fix)
# Measured 2026-07-30 driving cad_guide("hydraulic")'s own workflow verbatim: the guide says
# "Add pump, valve, cylinder, reservoir, etc." and the core refuses `cylinder`, `motor`,
# `check_valve`, `relief_valve` and `linear_cylinder` outright ("unknown component type").
# Every mapping below is a SPELLING of one member of the frozen ISO 1219 enum in
# contracts/hydraulic_schema.md — a word-order flip (check_valve -> valve_check) or the
# generic noun whose enum has exactly one non-degenerate member (`motor` -> motor_hydraulic;
# `cylinder` -> cylinder_linear, since a rotary cylinder is always named as such). Nothing
# here spans two enum members, so no reading is lost.
#
# Bare `valve` is DELIBERATELY absent: four valve types exist (directional / check / relief /
# flow_control) and picking one would be inventing the circuit's function. It stays refused,
# and _resolve_component_type says why instead of listing twelve types with no guidance.
_TYPE_ALIASES: dict[str, str] = {
    "cylinder": "cylinder_linear",
    "linearcylinder": "cylinder_linear",
    "actuator": "cylinder_linear",
    "ram": "cylinder_linear",
    "rotarycylinder": "cylinder_rotary",
    "rotaryactuator": "cylinder_rotary",
    "motor": "motor_hydraulic",
    "hydraulicmotor": "motor_hydraulic",
    "checkvalve": "valve_check",
    "nonreturnvalve": "valve_check",
    "reliefvalve": "valve_relief",
    "pressurereliefvalve": "valve_relief",
    "directionalvalve": "valve_directional",
    "directionalcontrolvalve": "valve_directional",
    "spoolvalve": "valve_directional",
    "flowcontrolvalve": "valve_flow_control",
    "throttlevalve": "valve_flow_control",
    "tank": "reservoir",
    "oiltank": "reservoir",
    "heatexchanger": "heat_exchanger",
    "cooler": "heat_exchanger",
}

_AMBIGUOUS_TYPES: dict[str, tuple] = {
    "valve": ("valve_directional", "valve_check", "valve_relief", "valve_flow_control"),
}


def _resolve_component_type(t: str) -> tuple:
    """A supplied component `type` -> (canonical enum member, note or None).

    Raises ValueError for a generic noun that spans several enum members, because guessing
    which valve a circuit needs would change what the circuit DOES.
    """
    raw = str(t or "")
    key = raw.lower().replace("_", "").replace("-", "").replace(" ", "")
    if key in _AMBIGUOUS_TYPES:
        opts = _AMBIGUOUS_TYPES[key]
        raise ValueError(
            f"component type {raw!r} is ambiguous — {len(opts)} ISO 1219 valve types exist "
            f"({', '.join(opts)}) and they do different things, so there is nothing safe to "
            "assume. Name the one the circuit needs.")
    canon = _TYPE_ALIASES.get(key)
    if canon and canon != raw:
        return canon, f"type {raw!r} read as {canon!r} (ISO 1219 enum spelling)"
    return raw, None


# Axis WORDS a model writes instead of a 3-vector, e.g. hydraulic_fea_handoff(axis="z")
# — measured 2026-07-30: `_as_obj("z")` fails JSON parsing and the whole call is refused
# with "could not parse inputs". Same table and Z-up convention as cad_sim_tools._AXIS_DIRS.
_AXIS_WORDS: dict[str, list] = {
    "x": [1.0, 0.0, 0.0], "+x": [1.0, 0.0, 0.0], "-x": [-1.0, 0.0, 0.0],
    "y": [0.0, 1.0, 0.0], "+y": [0.0, 1.0, 0.0], "-y": [0.0, -1.0, 0.0],
    "z": [0.0, 0.0, 1.0], "+z": [0.0, 0.0, 1.0], "-z": [0.0, 0.0, -1.0],
    "up": [0.0, 0.0, 1.0], "down": [0.0, 0.0, -1.0],
    "vertical": [0.0, 0.0, 1.0], "horizontal": [1.0, 0.0, 0.0],
}


def _axis_arg(axis):
    """An `axis` argument -> a 3-vector, accepting the signed-axis WORDS as well as JSON."""
    if isinstance(axis, str):
        word = axis.strip().lower()
        if word in _AXIS_WORDS:
            return list(_AXIS_WORDS[word])
    return _as_obj(axis)


def _load_doc(doc: Union[str, dict, None]) -> Any:
    """Accept a hydraulic doc as a dict, a JSON string, or a path to a ``.json`` file."""
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
    """Parse an argument that may arrive as a JSON string or native object."""
    if x is None or isinstance(x, (list, dict)):
        return x
    if isinstance(x, str):
        s = x.strip()
        return json.loads(s) if s else None
    return x


def _active_doc() -> Optional[dict]:
    return _HYD.get("doc")


# ------------------------------------------------------- circuit studies (persistence)
#: Study type/engine a circuit and every analysis on it persist under (doc ``studies[]``).
_CIRCUIT_STUDY = "hydraulic_circuit"
_CIRCUIT_ENGINE = "cpp-hydraulic"
_DEFAULT_CIRCUIT = "Hydraulic Circuit"
#: Results that describe inputs rather than the circuit (hand-calc sizing) survive a circuit
#: edit; simulate / bom / piping_handoff describe THIS circuit and are dropped with it.
_KEEP_ON_CIRCUIT_CHANGE = ("sizing",)
_SIZING_KINDS = ("cylinders", "pumps", "motors", "drives")
#: Envelope keys that describe the call rather than the result — never stored in a study.
_ENVELOPE_KEYS = ("ok", "hint", "next_actions")
#: Points kept per returned time series; the full-resolution run goes to ``series_path``.
_SERIES_MAX_POINTS = 400


def _body(res: dict) -> dict:
    """A result minus its call envelope — what a study stores."""
    return {k: v for k, v in res.items() if k not in _ENVELOPE_KEYS}


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


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


def _target_doc(doc_id: Optional[str]) -> "tuple[Optional[str], Optional[dict]]":
    """Resolve an OPTIONAL persistence target; no doc_id means nothing is persisted."""
    if not doc_id:
        return None, None
    try:
        return _doc(doc_id), None
    except Exception as exc:  # noqa: BLE001 — DocRefError names the open documents
        return None, _err("unknown_doc", hint=str(exc))


def _circuit_studies(doc_id: str) -> list:
    try:
        res = cb.call("sim_study_list", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001 — an unreadable study list reads as empty
        return []
    return [s for s in (res.get("studies") or [])
            if isinstance(s, dict) and s.get("type") == _CIRCUIT_STUDY]


def _find_circuit_study(doc_id: str, name: str) -> Optional[dict]:
    for s in _circuit_studies(doc_id):
        if s.get("name") == name:
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


def _circuit_name(circuit: Optional[dict]) -> str:
    name = circuit.get("name") if isinstance(circuit, dict) else None
    return str(name) if name else _DEFAULT_CIRCUIT


def _merge_results(old: dict, new: dict) -> dict:
    """``old`` + ``new``; ``sizing`` merges per kind and per key, everything else replaces."""
    out = dict(old)
    for k, v in new.items():
        if k == "sizing" and isinstance(v, dict) and isinstance(out.get("sizing"), dict):
            merged = dict(out["sizing"])
            for kind, rows in v.items():
                prior = merged.get(kind) if isinstance(merged.get(kind), dict) else {}
                merged[kind] = {**prior, **(rows or {})}
            out["sizing"] = merged
        else:
            out[k] = v
    return out


def _persist_circuit(doc_id: str, name: str, circuit: Optional[dict] = None, *,
                     params: Optional[dict] = None, results: Optional[dict] = None) -> dict:
    """Merge into ``doc_id``'s ``hydraulic_circuit`` study ``name`` (one study per circuit).

    ``circuit=None`` keeps the stored circuit (sizing recorded before or beside it). A
    changed circuit invalidates every analysis of the old one: only ``sizing`` (hand calcs
    on their own inputs) is kept, exactly like ``piping_tools._persist_network`` keeps
    routed-run records.
    """
    existing = _find_circuit_study(doc_id, name) or {}
    old_params = existing.get("params") if isinstance(existing.get("params"), dict) else {}
    old_results = existing.get("results") if isinstance(existing.get("results"), dict) else {}
    stored = old_params.get("circuit")
    if circuit is None:
        circuit = stored
    elif stored != circuit:
        old_results = {k: v for k, v in old_results.items() if k in _KEEP_ON_CIRCUIT_CHANGE}
    rec: dict = {"type": _CIRCUIT_STUDY, "name": name, "engine": _CIRCUIT_ENGINE,
                 "params": {**old_params, **(params or {}), "circuit": circuit},
                 "results": _merge_results(old_results, results or {})}
    if existing.get("id"):
        rec["id"] = existing["id"]
    return _saved(doc_id, _persist_study(doc_id, rec))


def _restore_circuit(study: dict) -> dict:
    """Make a persisted circuit the active session circuit; returns the circuit."""
    circuit = (study.get("params") or {})["circuit"]
    _HYD["doc"] = circuit
    return circuit


def _layout_3d(circuit: dict, authored: Optional[dict] = None) -> dict:
    """Shared physical millimetre layout; authored positions override schematic XY defaults."""
    merged = {}
    for component in circuit.get("components") or []:
        xy = component.get("position") or [0, 0]
        merged[component["id"]] = [2.0 * float(xy[0]), 2.0 * float(xy[1]), 0.0]
    merged.update({cid: xyz for cid, xyz in (authored or {}).items() if cid in merged})
    return merged


def _saved_circuit(target: str, name: Optional[str] = None) -> "tuple[Optional[dict], list]":
    """The circuit study to restore from ``target``: by name, else the doc's only circuit."""
    nets = [s for s in _circuit_studies(target)
            if isinstance((s.get("params") or {}).get("circuit"), dict)]
    names = [s.get("name") for s in nets]
    if name is not None:
        return next((s for s in nets if s.get("name") == name), None), names
    return (nets[0] if len(nets) == 1 else None), names


def _circuit_input(doc: Union[str, dict, None],
                   target: Optional[str]) -> "tuple[Optional[dict], Optional[dict]]":
    """``(circuit, refusal)``: the inline ``doc``, else the session circuit, else (with a
    persistence target) the circuit saved in that document — the save/reopen path."""
    if doc is not None:
        try:
            d = _load_doc(doc)
        except (ValueError, TypeError, OSError) as exc:
            return None, _err("bad_circuit", hint=f"could not parse doc: {exc}")
        if isinstance(d, dict) and d.get("schema") is None and isinstance(d.get("circuit"), dict):
            d = d["circuit"]  # a whole hydraulic_load result
        if not isinstance(d, dict):
            return None, _err("bad_circuit", hint="doc must be an aicad.hydraulic/1 object")
        return d, None
    active = _active_doc()
    if isinstance(active, dict):
        return active, None
    if target is not None:
        st, _names = _saved_circuit(target)
        if st is not None:
            return _restore_circuit(st), None
    return None, _err(
        "no_circuit",
        hint=("no active hydraulic circuit: hydraulic_new(name) starts one, "
              "hydraulic_load(doc_id) restores one saved in a document, or pass doc=<circuit>"))


def _sizing_study_name(target: str) -> str:
    """Which circuit study a hand-calc sizing belongs to: the active circuit, else the
    document's only saved circuit, else the default circuit name."""
    active = _active_doc()
    if isinstance(active, dict):
        return _circuit_name(active)
    studies = _circuit_studies(target)
    if len(studies) == 1 and studies[0].get("name"):
        return str(studies[0]["name"])
    return _DEFAULT_CIRCUIT


def _persist_sizing(doc_id: Optional[str], kind: str, key: str, res: dict) -> dict:
    """Record one sizing result under ``results.sizing.<kind>.<key>`` (nothing w/o doc_id)."""
    target, refusal = _target_doc(doc_id)
    if refusal:
        return {"study_not_saved_reason": refusal["hint"], "saved_to_study": False}
    if target is None:
        return {}
    name = _sizing_study_name(target)
    active = _active_doc()
    circuit = active if isinstance(active, dict) and _circuit_name(active) == name else None
    out = _persist_circuit(target, name, circuit,
                           results={"sizing": {kind: {key: _body(res)}}})
    out["circuit_study"] = name
    out["sizing_key"] = key
    return out


def _num_label(v: Any) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


# ------------------------------------------------------- transient simulation payload
def _sample_indices(n: int, max_points: int) -> list:
    """Evenly spaced sample indices over ``n`` points, always keeping the first and last."""
    if n <= max_points:
        return list(range(n))
    m = max(2, max_points)
    return sorted({round(i * (n - 1) / (m - 1)) for i in range(m)})


def _sample(series: Any, n: int, idx: list) -> Any:
    if isinstance(series, list) and len(series) == n:
        return [series[i] for i in idx]
    return series


def compact_simulation(res: dict, max_points: int = _SERIES_MAX_POINTS) -> dict:
    """A ``hydraulic_simulate`` result with every time series cut to ≤ ``max_points``
    samples on ONE shared index set (so ``time_s`` still aligns with every series).

    Summary, solver and warnings are untouched; ``downsampled`` records the reduction.
    """
    out = dict(res)
    time_s = res.get("time_s")
    n = len(time_s) if isinstance(time_s, list) else 0
    cap = max(2, min(int(max_points), _SERIES_MAX_POINTS))
    idx = _sample_indices(n, cap)
    out["time_s"] = _sample(time_s, n, idx)
    nodes = res.get("nodes")
    if isinstance(nodes, dict):
        out["nodes"] = {nid: ({k: _sample(v, n, idx) for k, v in nd.items()}
                              if isinstance(nd, dict) else nd)
                        for nid, nd in nodes.items()}
    comps = res.get("components")
    if isinstance(comps, dict):
        new_comps = {}
        for cid, c in comps.items():
            if isinstance(c, dict) and isinstance(c.get("series"), dict):
                c = {**c, "series": {k: _sample(v, n, idx) for k, v in c["series"].items()}}
            new_comps[cid] = c
        out["components"] = new_comps
    out["downsampled"] = {"points": len(idx), "of": n, "full_resolution": n <= cap}
    return out


def _series_root() -> str:
    """The bound project root series files live under ('' when no project is bound)."""
    from services.agent.request_context import root_value
    return root_value("CHUDWARE_PROJECT_SAVE_DIR").strip() or _project_dir() or ""


def _series_dir() -> str:
    """``<project>/.chudware/hydraulic`` when a project is bound, else the temp dir."""
    root = _series_root()
    base = (os.path.join(root, ".chudware", "hydraulic") if root
            else os.path.join(tempfile.gettempdir(), "chudware-hydraulic"))
    os.makedirs(base, exist_ok=True)
    return base


def portable_series_path(path: str) -> str:
    """The ``series_path`` a STUDY stores: project-relative (POSIX) when the file sits under
    the bound project, so a moved or copied project keeps its full-resolution run; an
    out-of-project (temp) file stays absolute."""
    root = _series_root()
    if not root or not path:
        return path
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:  # different drive on Windows
        return path
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return path
    return rel.replace(os.sep, "/")


def resolve_series_path(stored: Any) -> Any:
    """A study's stored ``series_path`` as an absolute path against the bound project root."""
    if not isinstance(stored, str) or not stored or os.path.isabs(stored):
        return stored
    root = _series_root()
    return os.path.normpath(os.path.join(root, stored)) if root else stored




def write_series(res: dict, name: str) -> "tuple[Optional[str], Optional[str]]":
    """Write the full-resolution run to a JSON file; ``(path, None)`` or ``(None, reason)``."""
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name.strip()) or "circuit"
    payload = {k: res.get(k) for k in ("time_s", "nodes", "components", "summary", "solver",
                                       "warnings")}
    try:
        fd, path = tempfile.mkstemp(prefix=f"{stem}-", suffix=".json", dir=_series_dir())
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError as exc:
        return None, f"could not write the full-resolution series: {exc}"
    return path, None


#: Pressure-rating keys read from a component's params or a hose (object or its params).
_RATING_KEYS = ("max_pressure_bar", "pressure_rating_bar", "working_pressure_bar",
                "rated_pressure_bar")


def _rating(obj: Any) -> Optional[float]:
    if not isinstance(obj, dict):
        return None
    for src in (obj, obj.get("params")):
        if isinstance(src, dict):
            for k in _RATING_KEYS:
                try:
                    v = float(src[k])
                except (KeyError, TypeError, ValueError):
                    continue
                if v > 0:
                    return v
    return None


def rating_check(circuit: dict, res: dict) -> dict:
    """Peak simulated pressure at every rated component / hose vs its pressure rating.

    A node's peak is the max of its full-resolution ``pressure_bar`` series; a component's
    peak is the max over the nodes its ports sit on (node ``members`` are
    ``"<component_id>.<port>"``), a hose's the node its connections join.
    """
    peak_by_member: dict = {}
    for node in (res.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        series = [v for v in (node.get("pressure_bar") or []) if isinstance(v, (int, float))]
        if not series:
            continue
        peak = max(series)
        for m in node.get("members") or []:
            key = str(m).lower()
            peak_by_member[key] = max(peak, peak_by_member.get(key, peak))

    def _peak_of(cid: str, ports: Optional[list] = None) -> Optional[float]:
        cid = str(cid).lower()
        vals = [v for k, v in peak_by_member.items()
                if k.split(".", 1)[0] == cid
                and (ports is None or k.split(".", 1)[-1] in ports)]
        return max(vals) if vals else None

    checked: list = []
    for comp in circuit.get("components") or []:
        rating = _rating(comp)
        peak = _peak_of(comp.get("id", "")) if rating else None
        if rating and peak is not None:
            checked.append({"item": f"{comp.get('id')} ({comp.get('type')})",
                            "rating_bar": rating, "peak_bar": round(peak, 3)})
    for hose in circuit.get("hoses") or []:
        rating = _rating(hose)
        if not rating:
            continue
        peaks = [p for p in (_peak_of(c.get("component_id", ""),
                                      [str(c.get("port", "")).lower()])
                             for c in hose.get("connections") or [] if isinstance(c, dict))
                 if p is not None]
        if peaks:
            checked.append({"item": f"{hose.get('id')} ({hose.get('net_name')})",
                            "rating_bar": rating, "peak_bar": round(max(peaks), 3)})
    for row in checked:
        row["utilization"] = round(row["peak_bar"] / row["rating_bar"], 4)
        row["pass"] = row["peak_bar"] <= row["rating_bar"]
    out = {"checked": checked, "exceeded": [r for r in checked if not r["pass"]]}
    if not checked:
        out["note"] = ("no component or hose carries a pressure rating (params "
                       "max_pressure_bar / pressure_rating_bar) — nothing to check against")
    return out


# ── hydraulic → piping / FEA handoff helpers (module-level: engine-less testable) ──
#
# The translators/verifies the tools used before the C++ port now live in the C++ core
# (core/hydraulic/handoff.cpp — estimate_line_dp, hoses_to_piping, both envelope
# builders, the margin feedback); the tool bodies below are thin core_bridge
# wrappers. The pure-Python copies kept here are the simhandoff-envelope
# compositions the MCP test suite exercises engine-less (same shapes the core
# emits — byte-parity proven by the port parity harness).

# ISO VG32 mineral hydraulic oil at ~40 °C — the default working fluid for line-loss
# estimates and the piping solve (ν = 32 cSt, ρ = 870 kg/m³ → μ = ρ·ν ≈ 0.0278 Pa·s).
HYDRAULIC_OIL = {"name": "hydraulic_oil_iso_vg32", "rho": 870.0, "mu": 0.0278}

# Default absolute roughness for hose/tube bore (m) — matches the piping solver's
# commercial-steel default so estimate vs solve compare like-for-like.
_HOSE_EPS_M = 4.6e-5


def estimate_line_dp(*, flow_lpm: float, id_mm: float, length_mm: float,
                     rho: float = HYDRAULIC_OIL["rho"],
                     mu: float = HYDRAULIC_OIL["mu"],
                     eps_m: float = _HOSE_EPS_M) -> dict:
    """Cheap per-hose Darcy–Weisbach pressure-drop estimate (pure math, no deps).

    This is the hydraulic-side prediction that ``hydraulic_verify_piping`` later
    cross-checks against the piping 1-D solve: laminar f = 64/Re below Re 2300,
    Swamee–Jain explicit Colebrook approximation above. Returns
    ``{ok, velocity_ms, reynolds, friction, regime, dp_kpa}``.
    """
    values = (flow_lpm, id_mm, length_mm, rho, mu, eps_m)
    if not all(isinstance(v, (int, float)) and math.isfinite(float(v))
               for v in values):
        return {"ok": False, "reason": "line-loss inputs must all be finite numbers"}
    if flow_lpm <= 0 or id_mm <= 0 or length_mm <= 0:
        return {"ok": False,
                "reason": "flow_lpm, id_mm, length_mm must all be finite and > 0"}
    if rho <= 0 or mu <= 0 or eps_m < 0:
        return {"ok": False,
                "reason": "rho and mu must be > 0; eps_m must be >= 0"}
    try:
        d_m = id_mm / 1000.0
        area = math.pi / 4.0 * d_m ** 2
        q_m3s = flow_lpm / 60_000.0
        v = q_m3s / area
        re = rho * v * d_m / mu
        if re < 2300.0:
            f = 64.0 / max(re, 1e-9)
            regime = "laminar"
        else:
            f = 0.25 / math.log10(
                eps_m / (3.7 * d_m) + 5.74 / re ** 0.9) ** 2
            regime = "turbulent (Swamee-Jain)"
        dp_pa = f * (length_mm / 1000.0) / d_m * 0.5 * rho * v ** 2
    except (OverflowError, ValueError, ZeroDivisionError):
        return {"ok": False, "reason": "line pressure-drop calculation overflowed"}
    if (not all(math.isfinite(v) for v in (area, q_m3s, v, re, f, dp_pa))
            or area <= 0 or q_m3s <= 0 or v <= 0 or re <= 0
            or f <= 0 or dp_pa < 0):
        return {"ok": False, "reason": "line pressure-drop calculation overflowed"}
    return {
        "ok": True,
        "velocity_ms": round(v, 4),
        "reynolds": round(re, 1),
        "friction": round(f, 5),
        "regime": regime,
        "dp_kpa": round(dp_pa / 1000.0, 4),
    }


def _piping_handoff_envelope(hyd_doc: dict, net: dict,
                             supply_pressure_bar: float) -> dict:
    """aicad.simhandoff/1 handoff envelope for the hydraulic→piping coupling.

    Doc-to-doc coupling: the generated ``aicad.piping/1`` document rides INLINE in a
    ``geometry_ref`` (contract §1 table, ``hydraulic_to_piping`` row) together with
    the boundary conditions + fluid in ``meta`` so ``piping_solve(source_handoff=...)``
    can consume the envelope with no other arguments.
    """
    conditions = [sh.condition(
        "supply_pressure", "pressure", supply_pressure_bar * 100.0, "kPa",
        "hydraulic circuit operating pressure (supply_pressure_bar x 100)",
        applied_to="all run starts")]
    expectations = []
    for row in net["lines"]:
        rid = row["run_id"]
        conditions.append(sh.condition(
            f"{rid}_flow", "flow", row["flow_m3h"], "m3/h",
            f"hydraulic hose {row['hose_id']} ({row['net_name']}) flow_lpm "
            "converted by x0.06 to m3/h",
            applied_to=rid))
        est = net["estimates"].get(rid) or {}
        if est.get("ok"):
            expectations.append(sh.expectation(
                f"pressure_drop:{rid}", est["dp_kpa"], "kPa",
                f"per-hose Darcy estimate (estimate_line_dp, {est['regime']}) "
                f"for hose {row['hose_id']}"))
    return sh.build_handoff(
        "hydraulic_to_piping",
        sh.study_ref("hydraulic", "hydraulic_piping_handoff",
                     doc_id=hyd_doc.get("id"),
                     result_digest={"hoses": len(net["lines"]),
                                    "supply_pressure_bar": supply_pressure_bar}),
        sh.study_ref("piping", "piping_solve"),
        geometry=[sh.geometry_ref("doc", "doc", inline=net["piping_doc"],
                                  derived_by="hoses_to_piping", units="mm")],
        conditions=conditions,
        expectations=expectations or None,
        meta={"boundary": net["boundary"], "fluid": net["fluid"],
              "lines": net["lines"],
              "topology_model": (
                  "independent_hose_line_cases (each run has its own "
                  "fixed-pressure start and prescribed-flow end; component-port "
                  "continuity is provenance only)")})


def _margin_update(supply_pressure_bar: float, load_pressure_bar: Optional[float],
                   est_loss_bar: Optional[float],
                   solved_loss_bar: Optional[float]) -> dict:
    """Pump sizing margin fed back from line losses (real losses change the margin)."""
    out: dict[str, Any] = {"supply_pressure_bar": supply_pressure_bar,
                           "load_pressure_bar": load_pressure_bar,
                           "line_loss_estimate_bar": est_loss_bar,
                           "line_loss_solved_bar": solved_loss_bar}
    if load_pressure_bar is not None:
        if est_loss_bar is not None:
            out["pump_margin_estimate_bar"] = round(
                supply_pressure_bar - load_pressure_bar - est_loss_bar, 4)
        if solved_loss_bar is not None:
            out["pump_margin_solved_bar"] = round(
                supply_pressure_bar - load_pressure_bar - solved_loss_bar, 4)
            out["pump_margin_solved_pct"] = round(
                out["pump_margin_solved_bar"] / supply_pressure_bar * 100.0, 2)
    return out


def _verify_piping_envelope(handoff: dict, solve_res: dict,
                            tolerance_pct: float) -> "tuple[dict, list]":
    """Build the hydraulic→piping verify envelope: per-line estimate vs solved dP.

    Returns ``(envelope, checks)``. Expectation quantities are
    ``pressure_drop:<run_id>``; solved values come from the matching solve link's
    ``dp_kpa``. Deviation is relative to the SOLVED value (the trusted number).
    """
    links = {lk.get("id"): lk for lk in solve_res.get("links", [])}
    checks = []
    unavailable = []
    for exp in handoff.get("expectations") or []:
        q = exp.get("quantity", "")
        rid = q.split(":", 1)[1] if ":" in q else q
        lk = links.get(rid)
        try:
            solved = abs(float(lk["dp_kpa"])) if lk is not None else math.nan
            est = float(exp["value"])
        except (KeyError, TypeError, ValueError):
            solved = est = math.nan
        if not math.isfinite(solved) or not math.isfinite(est):
            unavailable.append(
                f"{q or rid}: comparison unavailable (missing or non-finite "
                "estimate/solved pressure drop)")
            checks.append(sh.check(f"availability:{q or rid}", "1", 1.0, 0.0,
                                   100.0, 0.0, False))
            continue
        base = max(abs(solved), 1e-9)
        stored_dev = round(abs(solved - est) / base * 100.0, 2)
        checks.append(sh.check(q, "kPa", est, round(solved, 4),
                               stored_dev, tolerance_pct,
                               stored_dev <= tolerance_pct))
    if unavailable:
        verdict = ("FAIL: pressure-drop verification unavailable for "
                   f"{len(unavailable)} expected hose line(s)")
    else:
        n_pass = sum(1 for c in checks if c["pass"])
        verdict = (f"PASS: {n_pass}/{len(checks)} hose-line dP estimates within "
                   f"{tolerance_pct}% of the piping 1D solve"
                   if checks and n_pass == len(checks) else
                   f"FAIL: {len(checks) - n_pass}/{len(checks)} hose-line dP estimates "
                   f"deviate more than {tolerance_pct}% from the piping 1D solve")
    env = sh.build_verify(
        "hydraulic_to_piping",
        sh.study_ref("hydraulic", "hydraulic_piping_handoff"),
        sh.study_ref("piping", "piping_solve", handoff_id=handoff.get("id")),
        checks, verdict, handoff_id=handoff.get("id"),
        warnings=unavailable or None)
    return env, checks


def _unit3(axis) -> list:
    source = [0.0, 0.0, 1.0] if axis is None else axis
    if not isinstance(source, (list, tuple)) or len(source) != 3:
        raise ValueError("axis must be a 3-vector")
    v = [float(c) for c in source]
    if not all(math.isfinite(c) for c in v):
        raise ValueError("axis components must be finite")
    n = math.sqrt(sum(c * c for c in v))
    if not math.isfinite(n) or n <= 0:
        raise ValueError("axis must be non-zero")
    return [c / n for c in v]


def _cylinder_mount_forces(*, bore_mm: float, rod_mm: float, max_pressure_bar: float,
                           efficiency: float = 1.0) -> dict:
    """Mount reaction magnitudes (N) at max pressure — full bore extend, annulus retract."""
    a_bore = math.pi / 4.0 * (bore_mm * 1e-3) ** 2
    a_ann = math.pi / 4.0 * ((bore_mm * 1e-3) ** 2 - (rod_mm * 1e-3) ** 2)
    p_pa = max_pressure_bar * 1e5
    return {"extend_n": a_bore * p_pa * efficiency,
            "retract_n": a_ann * p_pa * efficiency}


def _fea_handoff_envelope(*, bore_mm: float, rod_mm: float, max_pressure_bar: float,
                          axis: list, efficiency: float,
                          load_face: str, fixture_face: str,
                          doc_id: Optional[str] = None,
                          target_doc_id: Optional[str] = None) -> dict:
    """aicad.simhandoff/1 handoff envelope for the hydraulic→FEA coupling.

    Mirrors the ``motion_to_fea`` pattern (contract §1 table): one force condition per
    load case (``case`` labels group extend/retract), plus the shared fixture. Extend
    thrust reacts INTO the mount (−axis); retract pulls the mount toward the load
    (+axis). Geometry may be empty (doc-to-doc) — the mounting geometry lives in the
    target FEA document when ``target_doc_id`` is given.
    """
    f = _cylinder_mount_forces(bore_mm=bore_mm, rod_mm=rod_mm,
                               max_pressure_bar=max_pressure_bar,
                               efficiency=efficiency)
    ax = _unit3(axis)
    ext_vec = [round(-c * f["extend_n"], 3) for c in ax]
    ret_vec = [round(c * f["retract_n"], 3) for c in ax]
    prov = (f"hydraulic cylinder bore {bore_mm} mm rod {rod_mm} mm at max pressure "
            f"{max_pressure_bar} bar (area x pressure x efficiency {efficiency})")
    conditions = [
        sh.condition("mount_force_extend", "force", ext_vec, "N",
                     prov + " - extend, full-bore area, reaction into mount (-axis)",
                     applied_to=load_face, case="extend_max_pressure"),
        sh.condition("mount_force_retract", "force", ret_vec, "N",
                     prov + " - retract, annulus area, reaction toward load (+axis)",
                     applied_to=load_face, case="retract_max_pressure"),
        sh.condition("mount_fixture", "fixture", {"type": "fixed"}, "1",
                     "mounting structure support (held fixed in every load case)",
                     applied_to=fixture_face),
    ]
    expectations = [
        sh.expectation("mount_force:extend_max_pressure", round(f["extend_n"], 3), "N",
                       "pi/4*bore^2 x max pressure x efficiency"),
        sh.expectation("mount_force:retract_max_pressure", round(f["retract_n"], 3), "N",
                       "pi/4*(bore^2-rod^2) x max pressure x efficiency"),
    ]
    return sh.build_handoff(
        "hydraulic_to_fea",
        sh.study_ref("hydraulic", "hydraulic_fea_handoff", doc_id=doc_id,
                     result_digest={"bore_mm": bore_mm, "rod_mm": rod_mm,
                                    "max_pressure_bar": max_pressure_bar,
                                    "efficiency": efficiency,
                                    "extend_force_n": round(f["extend_n"], 3),
                                    "retract_force_n": round(f["retract_n"], 3)}),
        sh.study_ref("fea", "run_fea_static", doc_id=target_doc_id),
        geometry=([sh.geometry_ref("mount", "doc_id", doc_id=target_doc_id,
                                   derived_by="caller-supplied mounting geometry")]
                  if target_doc_id else []),
        conditions=conditions,
        expectations=expectations,
        meta={"axis": ax, "bore_mm": bore_mm, "rod_mm": rod_mm,
              "max_pressure_bar": max_pressure_bar, "efficiency": efficiency})


def _verify_fea_envelope(handoff: dict, tolerance_pct: float,
                         fea_result: Optional[dict] = None) -> dict:
    """Verify both unambiguous cylinder load cases against independent A×P math."""
    meta = handoff.get("meta") or {}
    checks = []
    warnings: list = []
    unavailable: list = []
    try:
        bore = float(meta["bore_mm"])
        rod = float(meta.get("rod_mm", 0.0))
        pressure = float(meta["max_pressure_bar"])
        efficiency = float(meta.get("efficiency", 1.0))
        if (not all(math.isfinite(v) for v in
                    (bore, rod, pressure, efficiency)) or
                bore <= 0 or rod < 0 or rod >= bore or pressure <= 0 or
                not 0 < efficiency <= 1):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        unavailable.append(
            "hydraulic→FEA comparison unavailable: handoff metadata is "
            "missing or physically invalid")
        checks.append(sh.check("availability:handoff_metadata", "1", 1.0, 0.0,
                               100.0, 0.0, False))
        want = {}
        axis = None
    else:
        f = _cylinder_mount_forces(
            bore_mm=bore, rod_mm=rod, max_pressure_bar=pressure,
            efficiency=efficiency)
        want = {"extend_max_pressure": f["extend_n"],
                "retract_max_pressure": f["retract_n"]}
        try:
            if "axis" not in meta:
                raise ValueError("axis is missing")
            axis = _unit3(meta["axis"])
        except (TypeError, ValueError) as exc:
            axis = None
            message = f"invalid cylinder axis provenance: {exc}"
            unavailable.append(message)
            checks.append(sh.check("availability:cylinder_axis", "1", 1.0, 0.0,
                                   100.0, 0.0, False))

    vectors = {case: [] for case in want}
    for cond in handoff.get("conditions") or []:
        case = cond.get("case")
        if cond.get("kind") == "force" and case in vectors:
            vectors[case].append(cond.get("value"))
    for case, expect in want.items():
        candidates = vectors[case]
        vec = candidates[0] if len(candidates) == 1 else None
        try:
            if not isinstance(vec, list) or len(vec) != 3:
                raise ValueError
            numbers = [float(c) for c in vec]
            if not all(math.isfinite(c) for c in numbers):
                raise ValueError
        except (TypeError, ValueError):
            unavailable.append(
                f"{case}: comparison unavailable because exactly one finite "
                "3-vector force condition is required")
            checks.append(sh.check(f"availability:mount_force:{case}", "1",
                                   1.0, 0.0, 100.0, 0.0, False))
            continue
        mag = math.sqrt(sum(c * c for c in numbers))
        stored_dev = round(abs(mag - expect) / max(expect, 1e-9) * 100.0, 3)
        checks.append(sh.check(f"mount_force:{case}", "N", round(expect, 3),
                               round(mag, 3), stored_dev, tolerance_pct,
                               stored_dev <= tolerance_pct))
        if axis is not None and mag > 0:
            cosang = abs(sum(a * c for a, c in zip(axis, numbers))) / mag
            if cosang < 0.999:
                warnings.append(f"{case}: force vector not colinear with cylinder "
                                f"axis (|cos| = {cosang:.4f})")
    ok = bool(checks) and all(c["pass"] for c in checks)
    verdict = (f"PASS: all {len(checks)} mount-load case force magnitudes match "
               f"the independent area x pressure recompute within {tolerance_pct}%"
               if ok else "FAIL: mount-load cases are missing, ambiguous, invalid, "
                          "or deviate from the independent area x pressure recompute")
    advisories = None
    if isinstance(fea_result, dict):
        summ = fea_result.get("summary") or fea_result
        detail = {k: summ.get(k) for k in
                  ("max_von_mises_mpa", "max_disp_mm", "min_fos") if k in summ}
        if detail:
            advisories = [{"name": "fea_static_summary", "detail": detail}]
    return sh.build_verify(
        "hydraulic_to_fea",
        sh.study_ref("hydraulic", "hydraulic_fea_handoff"),
        sh.study_ref("fea", "run_fea_static", handoff_id=handoff.get("id")),
        checks, verdict, handoff_id=handoff.get("id"),
        advisories=advisories, warnings=(warnings + unavailable) or None)


def register_hydraulic_tools(mcp) -> None:
    """Register the AI-Hydraulic tools on the given FastMCP instance.

    Called from ``services/mcp/server.py`` after ``mcp`` is created. A function (not import-time
    side effects) so the tools can be driven against a stub ``mcp`` in tests — mirrors
    :func:`register_piping_tools`.
    """

    def _persist_edit(r: dict, target: Optional[str], params: Optional[dict] = None) -> dict:
        """After a successful circuit edit, merge the new circuit into its study."""
        if target and r.get("ok") and isinstance(r.get("doc"), dict):
            r.update(_persist_circuit(target, _circuit_name(r["doc"]), r["doc"], params=params))
            r["circuit_study"] = _circuit_name(r["doc"])
        return r

    @mcp.tool()
    def hydraulic_new(name: str = "Hydraulic Circuit", doc_id: Optional[str] = None,
                      template: str = "blank") -> str:
        """Create a new ``aicad.hydraulic/1`` hydraulic circuit document.

        ``template`` selects a native starting circuit: ``blank`` (default, empty),
        ``power_unit`` (tank, gear pump, relief and return filter), ``cylinder_circuit``
        (power unit + 4/3 valve + double-acting cylinder), or ``motor_circuit``
        (power unit + 4/3 valve + bidirectional motor). Unknown ids return reason + hint.
        All templates use ISO 1219 units (bar / L/min / kN / mm); their definitions and
        suggested ``simulation`` setups come only from the native engine.
        The new document becomes the active session circuit for subsequent hydraulic tools.

        With ``doc_id`` the circuit AND its suggested simulation setup are saved into that
        CAD document's ``hydraulic_circuit`` study named ``name`` (saved into the ``.step+``
        by save_part); later edits and analyses passing the same ``doc_id`` merge into it
        and ``hydraulic_load`` restores it.
        A saved circuit of the same name is replaced (its sizing results are kept).

        Returns the document envelope, ``template`` echo and ``simulation`` setup. Adapt
        the example loads to the real duty, then pass ``**simulation`` to hydraulic_simulate.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        r = _rpc("hydraulic_new", {"name": name, "template": template})
        if r.get("ok") and isinstance(r.get("doc"), dict):
            _HYD["doc"] = r["doc"]
            if target:
                prior = _find_circuit_study(target, _circuit_name(r["doc"]))
                old = ((prior or {}).get("params") or {}).get("circuit")
                if isinstance(old, dict) and (old.get("components") or old.get("hoses")):
                    r.setdefault("warnings", []).append(
                        f"replaced the circuit saved as {_circuit_name(r['doc'])!r} in "
                        f"{target} ({len(old.get('components') or [])} components); use "
                        "hydraulic_load to continue a saved circuit instead")
        return _dumps(_persist_edit(r, target, params=r.get("simulation")))

    @mcp.tool()
    def hydraulic_templates() -> str:
        """List native circuit templates: id, name, description, component count, hose count.

        Pass an entry's ``id`` as ``hydraulic_new(template=..., doc_id=...)`` to create
        its circuit and suggested simulation setup. The catalog comes from the same native
        definitions used by the desktop Hydraulics workspace.
        """
        return _dumps(_rpc("hydraulic_templates", {}))

    @mcp.tool()
    def hydraulic_add_component(
        type: str,
        name: str = "",
        x: float = 0.0,
        y: float = 0.0,
        params: Union[str, dict, None] = None,
        sourcing: Union[str, dict, None] = None,
        doc: Union[str, dict, None] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Add an ISO 1219 hydraulic component to the active circuit document.

        ``type`` must be one of the ISO 1219 component types defined in
        ``contracts/hydraulic_schema.md``:
        pump, cylinder_linear, cylinder_rotary, valve_directional, valve_check,
        valve_relief, valve_flow_control, accumulator, reservoir, motor_hydraulic,
        filter, heat_exchanger.
        Common spellings of those members are accepted and echoed under ``interpreted``
        (``cylinder``/``actuator``->cylinder_linear, ``motor``->motor_hydraulic,
        ``check_valve``->valve_check, ``relief_valve``->valve_relief, ``tank``->reservoir,
        ...). A bare ``valve`` is refused: four valve types exist and they do different
        things.

        ``x``, ``y`` set the schematic canvas position (mm). ``params`` is a JSON object
        of type-specific parameters (bore_mm, stroke_mm, displacement_cc_rev, etc.) —
        see the contract for per-type param keys. ``sourcing`` carries mpn/manufacturer/
        description/quantity/unit/category for BOM roll-up.

        ``doc`` overrides the session document (optional — if omitted, uses the last
        ``hydraulic_new`` document; with ``doc_id`` and no session circuit, the circuit saved
        in that document). With ``doc_id`` the edited circuit is saved into its
        ``hydraulic_circuit`` study (stored simulate/bom results are dropped — they described
        the old circuit).

        For ``hydraulic_simulate`` a component needs its required params (pump
        displacement_cc_rev + speed_rpm, cylinder_linear bore_mm + stroke_mm (+ rod_mm),
        motor_hydraulic displacement_cc_rev, valve_relief set_pressure_bar, accumulator
        volume_l + pre_charge_bar, valve_flow_control set_flow_lpm | Kv | Cv).

        Returns the updated document with the new component appended.
        """
        try:
            type, type_note = _resolve_component_type(type)
        except ValueError as exc:
            return _dumps(_err(str(exc),
                               hint="pick the specific ISO 1219 member — see the type list "
                                    "in contracts/hydraulic_schema.md"))
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            p = _as_obj(params) or {}
            s = _as_obj(sourcing) or {}
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        if d is None and target:
            d, _miss = _circuit_input(None, target)

        try:
            r = cb.call("hydraulic_add_component", {
                "doc": d, "type": type, "name": name, "x": x, "y": y,
                "params": p, "sourcing": s})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and isinstance(r.get("doc"), dict):
            _HYD["doc"] = r["doc"]  # additive "doc" key: the updated active circuit
        if r.get("ok") and type_note:
            r["interpreted"] = [type_note]
        return _dumps(_persist_edit(r, target))

    @mcp.tool()
    def hydraulic_add_hose(
        net_name: str,
        connections: Union[str, list],
        doc: Union[str, dict, None] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Add a hose/line net connecting two or more hydraulic component ports.

        ``net_name`` is a label for the net (e.g. ``"PRESSURE_LINE"``, ``"RETURN_LINE"``).
        ``connections`` is a JSON list of ``{"component_id": "c1", "port": "P"}`` objects
        identifying which port on which component each end of the hose connects to.

        Port names ``hydraulic_simulate`` resolves (case-insensitive): pump P (outlet) /
        T (suction) / L; cylinder_linear A (cap) / B (rod); motor_hydraulic and
        cylinder_rotary A / B / L; valve_directional P / T / A / B; valve_relief P / T;
        valve_check, valve_flow_control, filter, heat_exchanger IN / OUT; reservoir and
        accumulator any name. Other names must be declared in the component's ``ports``
        with a port_type (pressure, tank, work_a, work_b, drain, generic).

        ``doc`` overrides the session document (optional). With ``doc_id`` the edited
        circuit is saved into its ``hydraulic_circuit`` study.

        Returns the updated document with the new hose appended.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            conns = _as_obj(connections)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        if d is None and target:
            d, _miss = _circuit_input(None, target)

        try:
            r = cb.call("hydraulic_add_hose", {
                "doc": d, "net_name": net_name, "connections": conns})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and isinstance(r.get("doc"), dict):
            _HYD["doc"] = r["doc"]  # additive "doc" key: the updated active circuit
        return _dumps(_persist_edit(r, target))

    @mcp.tool()
    def hydraulic_size_cylinder(
        bore_mm: float,
        pressure_bar: float,
        stroke_mm: float,
        rod_mm: float = 0.0,
        velocity_mm_s: float = 50.0,
        efficiency: float = 0.95,
        component_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Size a hydraulic cylinder: extend/retract force, flow, and cycle time.

        Physics:
        - Extend force (kN) = bore area (m²) × pressure (Pa) × efficiency / 1000
        - Retract force uses the annulus area (bore² − rod²)
        - Flow (L/min) = area (m²) × velocity (m/s) × 60 × 1000
        - Cycle time (s) = stroke_mm / velocity_mm_s

        Hand-calc check: 50 mm bore, 200 bar, efficiency=1 → ≈ 39.27 kN extend force.

        All inputs in the circuit's native units (mm, bar). Returns a contract-shaped dict
        with all sizing results plus recommended pump flow. With ``doc_id`` the result is
        saved under the active circuit's study ``results.sizing.cylinders.<component_id>``
        (a bore/pressure label when ``component_id`` is omitted).
        """
        try:
            r = cb.call("hydraulic_size_cylinder", {
                "bore_mm": bore_mm, "rod_mm": rod_mm, "pressure_bar": pressure_bar,
                "stroke_mm": stroke_mm, "velocity_mm_s": velocity_mm_s,
                "efficiency": efficiency})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok"):
            key = component_id or f"bore{_num_label(bore_mm)}mm@{_num_label(pressure_bar)}bar"
            r.update(_persist_sizing(doc_id, "cylinders", key, r))
        return _dumps(r)

    @mcp.tool()
    def hydraulic_size_pump(
        flow_lpm: float,
        pressure_bar: float,
        efficiency: float = 0.85,
        speed_rpm: float = 1500.0,
        component_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Size a hydraulic pump: displacement, hydraulic power, shaft power, torque.

        Physics:
        - Displacement (cc/rev) = Q (L/min) × 1000 / speed (rpm)
        - Hydraulic power (kW) = Q (m³/s) × ΔP (Pa) / 1000
        - Shaft power (kW) = hydraulic power / efficiency
        - Torque (N·m) = shaft power × 1000 / (2π × speed/60)

        Hand-calc check: 10 L/min, 200 bar, η=0.85 → shaft ≈ 3.92 kW.

        Returns displacement, power, and torque in a contract-shaped dict ready to pass to
        component selection; size its electric motor next with ``pump_drive_size``. With
        ``doc_id`` the result is saved under ``results.sizing.pumps.<component_id>``.
        """
        try:
            r = cb.call("hydraulic_size_pump", {
                "flow_lpm": flow_lpm, "pressure_bar": pressure_bar,
                "efficiency": efficiency, "speed_rpm": speed_rpm})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok"):
            key = component_id or f"{_num_label(flow_lpm)}lpm@{_num_label(pressure_bar)}bar"
            r.update(_persist_sizing(doc_id, "pumps", key, r))
        return _dumps(r)

    @mcp.tool()
    def hydraulic_size_motor(
        torque_nm: float,
        speed_rpm: float,
        pressure_bar: float,
        mech_efficiency: float = 0.9,
        vol_efficiency: float = 0.95,
        type: Optional[str] = None,
        component_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Size a hydraulic motor: displacement (exact + catalog step), flow, output/input power.

        ``torque_nm`` is the load torque at the shaft, ``speed_rpm`` the required speed and
        ``pressure_bar`` the DIFFERENTIAL pressure available across the motor (supply minus
        return/back pressure). Physics: D = 2π·T / (Δp·η_m) → ``displacement_cc_rev``;
        ``standard_displacement_cc_rev`` is the next catalog step up for ``type``
        (gear | vane | piston | orbital); Q = D·n / η_v → ``flow_lpm``; output P = T·ω,
        input hydraulic P = Δp·Q.

        Hand-calc check: 100 N·m at 100 bar, η_m = 1 → D = 62.83 cc/rev.

        Feed ``flow_lpm`` + the supply pressure to ``hydraulic_size_pump`` and put the chosen
        displacement on the circuit's motor_hydraulic (``displacement_cc_rev``). With
        ``doc_id`` the result is saved under ``results.sizing.motors.<component_id>``.
        """
        res = _rpc("hydraulic_size_motor", _drop_none({
            "torque_nm": torque_nm, "speed_rpm": speed_rpm, "pressure_bar": pressure_bar,
            "mech_efficiency": mech_efficiency, "vol_efficiency": vol_efficiency,
            "type": type}))
        if res.get("ok"):
            key = component_id or (f"{_num_label(torque_nm)}Nm@{_num_label(speed_rpm)}rpm"
                                   f"@{_num_label(pressure_bar)}bar")
            res.update(_persist_sizing(doc_id, "motors", key, res))
        return _dumps(res)

    @mcp.tool()
    def pump_drive_size(
        kind: str = "hydraulic",
        flow_lpm: Optional[float] = None,
        flow_m3h: Optional[float] = None,
        pressure_bar: Optional[float] = None,
        head_m: Optional[float] = None,
        fluid_density_kgm3: Optional[float] = None,
        pump_efficiency: Optional[float] = None,
        speed_rpm: Optional[float] = None,
        standard: str = "IEC",
        service_factor: float = 1.15,
        voltage_v: float = 400.0,
        phases: int = 3,
        frequency_hz: float = 50.0,
        efficiency_class: str = "IE3",
        component_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Size the electric motor driving a pump (hydraulic or centrifugal): kW/hp, poles, frame, current.

        ``kind`` = ``"hydraulic"`` (positive displacement: give ``flow_lpm`` + ``pressure_bar``)
        or ``"centrifugal"`` (give ``flow_m3h`` + ``head_m``, or flow + pressure). Hydraulic
        power = Q·Δp (or ρ·g·Q·H); shaft power = hydraulic / ``pump_efficiency`` (default 0.88
        hydraulic overall, 0.7 centrifugal); required motor = shaft × ``service_factor``,
        rounded UP the ``standard`` ladder (IEC 60072 kW or NEMA MG-1 hp). ``speed_rpm``
        (default 1500) picks the pole count at ``frequency_hz``; ``efficiency_class`` (IE2 |
        IE3 | IE4) gives the IEC 60034-30-1 motor efficiency, hence input power and
        full-load current at ``voltage_v`` / ``phases``. ``fluid_density_kgm3`` defaults to
        870 (oil) / 998 (water).

        Returns hydraulic_power_kw, shaft_power_kw, required_motor_kw, motor_rating_kw or
        motor_rating_hp, poles, sync_speed_rpm, motor_efficiency, input_power_kw,
        full_load_current_a and iec_frame / nema_frame. With ``doc_id`` a hydraulic drive
        is saved under the circuit study's ``results.sizing.drives.<component_id>``; for a
        piping network's pump use ``piping_pump_drive`` (it reads the solved duty point and
        saves into the ``piping_network`` study).
        """
        res = _rpc("pump_drive_size", _drop_none({
            "kind": kind, "flow_lpm": flow_lpm, "flow_m3h": flow_m3h,
            "pressure_bar": pressure_bar, "head_m": head_m,
            "fluid_density_kgm3": fluid_density_kgm3, "pump_efficiency": pump_efficiency,
            "speed_rpm": speed_rpm, "standard": standard, "service_factor": service_factor,
            "voltage_v": voltage_v, "phases": phases, "frequency_hz": frequency_hz,
            "efficiency_class": efficiency_class}))
        if res.get("ok") and doc_id:
            if str(kind).lower() == "hydraulic":
                flow = flow_lpm if flow_lpm is not None else flow_m3h
                key = component_id or (f"drive{_num_label(flow)}"
                                       f"{'lpm' if flow_lpm is not None else 'm3h'}"
                                       f"@{_num_label(pressure_bar)}bar")
                res.update(_persist_sizing(doc_id, "drives", key, res))
            else:
                res["saved_to_study"] = False
                res["study_not_saved_reason"] = (
                    "a centrifugal pump drive is saved with its piping network — call "
                    "piping_pump_drive(network=..., doc_id=...) to size and save it from the "
                    "solved duty point")
        return _dumps(res)

    @mcp.tool()
    def hydraulic_simulate(
        duration_s: float = 5.0,
        valve_schedule: Union[str, list, None] = None,
        loads: Union[str, dict, None] = None,
        fluid: Union[str, dict, None] = None,
        initial: Union[str, dict, None] = None,
        output_dt_s: float = 0.01,
        max_step_s: Optional[float] = None,
        max_points: int = _SERIES_MAX_POINTS,
        doc: Union[str, dict, None] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Transient simulation of the hydraulic circuit: pressures, cylinder/motor motion, flows, energy.

        Lumped-parameter model of the active circuit (or inline ``doc``; with ``doc_id`` and
        no session circuit, the one saved in that document): every hose net is a node with
        hydraulic capacitance V/β; pumps are flow sources (displacement × speed × η_v),
        relief valves crack at set_pressure_bar with linear override, directional valves
        meter Q = Cd·A·√(2Δp/ρ) per spool position, cylinders/motors integrate load, mass,
        inertia and friction with hard end stops, accumulators follow the polytropic gas law.
        Stiff adaptive integrator with energy bookkeeping.

        ``valve_schedule`` = ``[{"t_s":0,"component_id":"c3","position":"a"|"b"|"center"|0..1}]``
        (0..1 = proportional spool). ``loads`` = ``{"c4":{"force_n":..,"mass_kg":..,
        "friction_n":..}}`` for cylinders, ``{"c5":{"torque_nm":..,"inertia_kgm2":..}}`` for
        motors. ``fluid`` = ``{"density_kgm3":870,"bulk_modulus_bar":15000,
        "viscosity_cst":32}``. ``initial`` = ``{"c4":{"position_mm":0}}`` /
        ``{"<accumulator>":{"pressure_bar":..}}``. Units: bar, L/min, mm, N, N·m, rpm, kW, s.

        Returns ``summary`` (peak_pressure_bar, pump_energy_kj, useful_work_kj, heat_kj,
        efficiency, relief_open_time_s, per-cylinder stroke_time_s / max_velocity_mms /
        end_reached, per-motor steady speed/torque, per-pump mean pressure/power),
        ``ratings`` (peak node pressure vs every component/hose max_pressure_bar),
        ``time_s`` + ``nodes`` + ``components`` series cut to ≤ ``max_points`` (≤ 400)
        samples and ``series_path``, a JSON file with the full-resolution run. Unconnected
        ports, unknown schedule ids and missing params refuse by name. With ``doc_id`` the
        run (inputs + results) is saved into the circuit's ``hydraulic_circuit`` study.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        circuit, refusal = _circuit_input(doc, target)
        if refusal:
            return _dumps(refusal)
        try:
            vs = _as_obj(valve_schedule)
            ld = _as_obj(loads)
            fl = _as_obj(fluid)
            ini = _as_obj(initial)
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_input", hint=f"could not parse inputs: {exc}"))
        setup = _drop_none({"duration_s": duration_s, "max_step_s": max_step_s,
                            "output_dt_s": output_dt_s, "valve_schedule": vs, "loads": ld,
                            "fluid": fl, "initial": ini})
        res = _rpc("hydraulic_simulate", {"doc": circuit, **setup})
        if not res.get("ok"):
            return _dumps(res)
        name = _circuit_name(circuit)
        ratings = rating_check(circuit, res)
        path, why = write_series(res, name)
        out = compact_simulation(res, max_points)
        out["ratings"] = ratings
        out["warnings"] = list(out.get("warnings") or []) + [
            f"{e['item']}: peak {e['peak_bar']} bar exceeds its {e['rating_bar']} bar rating"
            for e in ratings["exceeded"]]
        if path:
            out["series_path"] = path
        else:
            out["warnings"].append(why)
        out["circuit_study"] = name
        out["next_actions"] = list(out.get("next_actions") or []) + [
            "compare summary.peak_pressure_bar with the relief set pressure and ratings.exceeded",
            "hydraulic_piping_handoff(supply_pressure_bar=..., flow_lpm=...) for rigid lines",
            "hydraulic_bom() once every component is sourced",
        ]
        if target:
            # max_points sizes the REPLY only; the study keeps the standard compaction so a
            # terse call (max_points=20) never decimates the saved run below what reviewers
            # and the GUI plot read.
            series_keys = ("time_s", "nodes", "components", "downsampled")
            stored = {**_body(out),
                      **{k: v for k, v in compact_simulation(res, _SERIES_MAX_POINTS).items()
                         if k in series_keys}}
            if path:
                stored["series_path"] = portable_series_path(path)
            out.update(_persist_circuit(target, name, circuit, params=setup,
                                        results={"simulate": stored}))
        return _dumps(out)

    @mcp.tool()
    def hydraulic_bom(doc: Union[str, dict, None] = None, doc_id: Optional[str] = None) -> str:
        """Extract a sourcing bill of materials from the active hydraulic circuit.

        Emits VendorPart-compatible sourcing lines for every component in the circuit:
        id, type, name, mpn, manufacturer, description, quantity, unit, category.

        Components without an MPN are included but flagged as un-sourced. A summary
        counts components by type and lists those missing MPNs.

        ``doc`` overrides the session document (optional — uses the last ``hydraulic_new``
        document if omitted). With ``doc_id`` the BOM is saved into the circuit's
        ``hydraulic_circuit`` study.

        Returns a list of BOM lines and a sourcing summary.
        """
        target, refusal = _target_doc(doc_id)
        if refusal:
            return _dumps(refusal)
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        if d is None and target:
            d, _miss = _circuit_input(None, target)

        try:
            r = cb.call("hydraulic_bom", {"doc": d})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if target and r.get("ok") and isinstance(d, dict):
            r.update(_persist_circuit(target, _circuit_name(d), d,
                                      results={"bom": _body(r)}))
            r["circuit_study"] = _circuit_name(d)
        return _dumps(r)

    @mcp.tool()
    def hydraulic_layout_3d(
        positions: Union[str, dict],
        circuit: Union[str, dict, None] = None,
        doc_id: Optional[str] = None,
    ) -> str:
        """Merge component positions (mm) into the document's hydraulic 3D layout.

        ``positions`` = ``{component_id: [x_mm, y_mm, z_mm]}``. ``circuit`` is an inline
        aicad.hydraulic/1 circuit, its saved name, or omitted for the document's only saved
        circuit / active session circuit. Layout lives in the hydraulic_circuit study's
        params.layout3d, shared with Hydraulics > 3D Layout. Unplaced components default
        to twice their schematic XY position on Z=0. Moving components preserves simulation
        results and schematic positions. Returns the merged layout and persistence receipt.
        """
        try:
            target = _doc(doc_id)
        except Exception as exc:  # noqa: BLE001 — same document resolution as hydraulic_load
            return _dumps(_err("unknown_doc", hint=str(exc)))
        study = None
        if isinstance(circuit, str) and not circuit.lstrip().startswith("{") and not os.path.isfile(circuit):
            study, names = _saved_circuit(target, circuit)
            if study is None:
                return _dumps(_err("circuit_not_found", hint="Pass a saved circuit name or an inline circuit.",
                                   circuits=names))
            definition = (study.get("params") or {})["circuit"]
        elif circuit is None:
            study, names = _saved_circuit(target)
            if study is not None:
                definition = (study.get("params") or {})["circuit"]
            elif len(names) > 1:
                return _dumps(_err("circuit_ambiguous", hint="Pass circuit=<saved circuit name>.", circuits=names))
            else:
                definition, refusal = _circuit_input(None, target)
                if refusal:
                    return _dumps(refusal)
        else:
            definition, refusal = _circuit_input(circuit, target)
            if refusal:
                return _dumps(refusal)
        name = _circuit_name(definition)
        study = study or _find_circuit_study(target, name) or {}
        params = study.get("params") or {}
        merged = _layout_3d(definition, params.get("layout3d"))
        ids = set(merged)
        try:
            updates = _as_obj(positions)
            if not isinstance(updates, dict):
                raise ValueError("positions must map component IDs to three coordinates")
            for cid, xyz in updates.items():
                if cid not in ids:
                    return _dumps(_err("unknown_component", hint=f"No component {cid!r} in circuit {name!r}."))
                if (not isinstance(xyz, (list, tuple)) or len(xyz) != 3 or
                        any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in xyz)):
                    raise ValueError(f"{cid}: position must contain three finite numbers in mm")
                merged[cid] = [float(v) for v in xyz]
        except (ValueError, TypeError) as exc:
            return _dumps(_err("bad_layout", hint=str(exc)))
        receipt = _persist_circuit(target, name, definition, params={"layout3d": merged})
        if not receipt.get("saved_to_study"):
            return _dumps(_err("study_not_saved", hint=receipt.get("study_not_saved_reason", "Could not save 3D layout."),
                               **receipt))
        _HYD["doc"] = definition
        return _dumps({"ok": True, "name": name, "layout3d": merged, **receipt})

    @mcp.tool()
    def hydraulic_load(doc_id: Optional[str] = None, name: Optional[str] = None) -> str:
        """Restore a hydraulic circuit saved in a document (with its sizing, BOM and simulation).

        Reads the document's ``hydraulic_circuit`` studies (written by hydraulic_new /
        add_component / add_hose / size_* / pump_drive_size / bom / simulate with ``doc_id``,
        or by the Hydraulics panel). ``name`` picks one by circuit name; omitted, the
        document's only circuit is loaded (several → refused with their names). The circuit
        becomes the active session circuit, so every hydraulic tool works on it without
        ``doc``; the saved valve_schedule / loads / fluid come back for a re-run.
        """
        try:
            target = _doc(doc_id)
        except Exception as exc:  # noqa: BLE001 — DocRefError names the open documents
            return _dumps(_err("unknown_doc", hint=str(exc)))
        st, names = _saved_circuit(target, name)
        if st is None:
            if not names:
                sizing_only = [s.get("name") for s in _circuit_studies(target)]
                return _dumps(_err(
                    "no_hydraulic_circuit",
                    hint=(f"document {target} has no saved hydraulic circuit; build one with "
                          "hydraulic_new(name, doc_id=...)"),
                    sizing_only_studies=sizing_only))
            return _dumps(_err(
                "circuit_not_found" if name else "circuit_ambiguous",
                hint=f"document {target} saves these circuits: {names}; pass name=<circuit>",
                circuits=names))
        circuit = _restore_circuit(st)
        params = st.get("params") or {}
        results = dict(st.get("results") or {})
        if isinstance(results.get("simulate"), dict) and results["simulate"].get("series_path"):
            results["simulate"] = {**results["simulate"], "series_path":
                                   resolve_series_path(results["simulate"]["series_path"])}
        cname = st.get("name") or _circuit_name(circuit)
        setup = {k: params.get(k) for k in ("valve_schedule", "loads", "fluid", "initial",
                                            "duration_s", "output_dt_s", "max_step_s")
                 if params.get(k) is not None}
        return _dumps({
            "ok": True, "doc_id": target, "name": cname, "study_id": st.get("id"),
            "circuit": circuit,
            "layout3d": _layout_3d(circuit, params.get("layout3d")),
            "components": len(circuit.get("components") or []),
            "hoses": len(circuit.get("hoses") or []),
            "simulation_setup": setup,
            "restored_results": sorted(k for k, v in results.items() if v is not None),
            "results": results, "warnings": [],
            "hint": f'circuit "{cname}" is the active session circuit',
            "next_actions": [
                f'hydraulic_simulate(doc_id="{target}", ...) — re-run with the saved setup',
                f'hydraulic_add_component(type=..., doc_id="{target}") to keep editing',
                f'hydraulic_bom(doc_id="{target}")',
            ],
        })

    @mcp.tool()
    def hydraulic_piping_handoff(supply_pressure_bar: float,
                                 flow_lpm: float,
                                 load_pressure_bar: Optional[float] = None,
                                 lines: Union[str, dict, None] = None,
                                 fluid: Union[str, dict, None] = None,
                                 default_id_mm: float = 12.7,
                                 default_length_mm: float = 1000.0,
                                 tolerance_pct: float = 30.0,
                                 doc: Union[str, dict, None] = None) -> str:
        """Hand the circuit's hose/tube runs to the piping 1-D solver for REAL per-line
        pressure drops at the operating point — replacing per-hose estimates — and feed
        the losses back into the pump sizing margin.

        Each hose in the active ``aicad.hydraulic/1`` circuit becomes a straight pipe
        run with its exact bore (``bore_mm``, since SAE hose sizes aren't NPS catalog
        pipes). ``supply_pressure_bar`` is the circuit operating pressure (fixed at
        every run start); ``flow_lpm`` the default line flow. Per-hose overrides go in
        ``lines`` = ``{hose_id: {"id_mm":.., "length_mm":.., "flow_lpm":..}}`` (the
        same keys are also read from fields stored on the hose object itself).
        ``fluid`` overrides the default ISO VG32 oil ``{"rho":870,"mu":0.0278}``.

        The piping solve ALWAYS runs live in the C++ core (it IS the solver — the old
        fluids-absent ``solve_pending`` stage does not exist on the core path): the
        result includes per-line dP + the updated pump margin
        (``margin.pump_margin_solved_bar`` = supply − load − real line losses, needs
        ``load_pressure_bar``) plus the estimate-vs-solve verify verdict. The generated
        network can always be re-solved with ``piping_solve(source_handoff=<simhandoff>)``.

        Conforms to ``aicad.simhandoff/1`` (coupling ``hydraulic_to_piping``): the
        generated piping doc rides INLINE in the envelope's geometry ref, per-line
        flows are conditions, and the per-hose Darcy estimates are expectations that
        ``hydraulic_verify_piping`` cross-checks at ``tolerance_pct``.
        """
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            ln = _as_obj(lines)
            fl = _as_obj(fluid)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        try:
            r = cb.call("hydraulic_piping_handoff", {
                "doc": d, "supply_pressure_bar": supply_pressure_bar,
                "flow_lpm": flow_lpm, "load_pressure_bar": load_pressure_bar,
                "lines": ln, "fluid": fl, "default_id_mm": default_id_mm,
                "default_length_mm": default_length_mm,
                "tolerance_pct": tolerance_pct})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and r.get("stage") == "solved":
            # Session state stays MCP-side: remember the live handoff+solve pair
            # so hydraulic_verify_piping can be called with no arguments.
            _HYD["piping_handoff"] = {"handoff": r.get("simhandoff"),
                                      "solve": r.get("solve")}
        return _dumps(r)

    @mcp.tool()
    def hydraulic_verify_piping(handoff: Union[str, dict, None] = None,
                                solve_result: Union[str, dict, None] = None,
                                supply_pressure_bar: Optional[float] = None,
                                load_pressure_bar: Optional[float] = None,
                                tolerance_pct: float = 30.0) -> str:
        """Verify step for the hydraulic→piping coupling: per-hose Darcy estimate vs
        piping 1-D solve, per line, plus the pump-margin feedback.

        ``handoff`` is the ``simhandoff`` envelope from ``hydraulic_piping_handoff``
        (or the whole result — the envelope is pulled from its ``simhandoff`` key);
        ``solve_result`` is the ``piping_solve`` output for the generated network.
        With no arguments, uses the session's last live handoff+solve pair.

        Each check compares one line's estimated dP (envelope expectation
        ``pressure_drop:<run_id>``) against the solved link ``dp_kpa`` at
        ``tolerance_pct`` (deviation relative to the solved value). Pass
        ``supply_pressure_bar``/``load_pressure_bar`` to also get the updated pump
        margin from the REAL losses. Returns ``{ok, pass, verdict, checks, margin}``
        with a validated ``aicad.simhandoff/1`` verify envelope under ``simhandoff``.
        """
        try:
            h = _as_obj(handoff)
            srs = _as_obj(solve_result)
        except (ValueError, TypeError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        sess = _HYD.get("piping_handoff") or {}
        if h is None:
            h = sess.get("handoff")
        if srs is None:
            srs = sess.get("solve")

        try:
            # The core also accepts the whole producer result for `handoff` (it pulls
            # the envelope from its `simhandoff` key), exactly like the old handler.
            r = cb.call("hydraulic_verify_piping", {
                "handoff": h, "solve_result": srs,
                "supply_pressure_bar": supply_pressure_bar,
                "load_pressure_bar": load_pressure_bar,
                "tolerance_pct": float(tolerance_pct)})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(r)

    @mcp.tool()
    def hydraulic_fea_handoff(bore_mm: float,
                              max_pressure_bar: float,
                              rod_mm: float = 0.0,
                              axis: Union[str, list, None] = None,
                              load_face: str = "the cylinder mount face",
                              fixture_face: str = "the base",
                              efficiency: float = 1.0,
                              target_doc_id: Optional[str] = None,
                              doc: Union[str, dict, None] = None) -> str:
        """Turn cylinder/actuator mount loads at max pressure into static-FEA load
        cases on the mounting geometry (companion of the motion→FEA pattern).

        Computes the mount reaction at ``max_pressure_bar``: extend thrust =
        π/4·bore² · P (full bore, reacts INTO the mount along −axis) and retract pull
        = π/4·(bore²−rod²) · P (annulus, +axis). ``axis`` is the cylinder axis in the
        mount document's world frame (default [0,0,1]) as a 3-vector or a signed-axis
        word (``"z"``/``"+z"``/``"-x"``/``"up"``/``"down"``); ``load_face`` /
        ``fixture_face`` are plain-word face queries on the mounting part;
        ``target_doc_id`` names the open FEA target document when known.

        Returns ready-to-call ``run_fea_static`` argument sets under ``fea_args``
        (one per load case: ``extend_max_pressure``, ``retract_max_pressure``) plus a
        validated ``aicad.simhandoff/1`` handoff envelope (coupling
        ``hydraulic_to_fea``) whose conditions carry the force vectors with ``case``
        labels and whose expectations record the A×P magnitudes for
        ``hydraulic_verify_fea`` to cross-check. This tool does NOT run the solve —
        pass ``fea_args`` straight to ``run_fea_static(doc_id=target_doc_id, ...)``.
        """
        try:
            ax = _axis_arg(axis)
            d = _load_doc(doc) if doc is not None else _active_doc()
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}",
                               hint='axis must be a non-zero [x,y,z] vector or a signed-axis '
                                    'word like "z" / "-x" / "up"'))

        try:
            r = cb.call("hydraulic_fea_handoff", {
                "bore_mm": float(bore_mm), "max_pressure_bar": float(max_pressure_bar),
                "rod_mm": float(rod_mm), "axis": ax, "load_face": load_face,
                "fixture_face": fixture_face, "efficiency": float(efficiency),
                "target_doc_id": target_doc_id, "doc": d})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and isinstance(r.get("simhandoff"), dict):
            _HYD["fea_handoff"] = r["simhandoff"]  # session state stays MCP-side
        return _dumps(r)

    @mcp.tool()
    def hydraulic_verify_fea(handoff: Union[str, dict, None] = None,
                             fea_result: Union[str, dict, None] = None,
                             tolerance_pct: float = 0.5) -> str:
        """Verify step for the hydraulic→FEA coupling: recompute the mount forces
        independently (area × max pressure from the envelope's recorded cylinder
        parameters) and check each load-case force condition matches in magnitude at
        ``tolerance_pct`` — plus a colinearity warning if a vector drifted off the
        cylinder axis. ``handoff`` defaults to the session's last
        ``hydraulic_fea_handoff`` envelope (or pass that tool's result).
        ``fea_result`` (a ``run_fea_static`` output) is attached as an advisory
        (max stress / displacement / FoS) — it does not gate the verdict.

        Returns ``{ok, pass, verdict, checks}`` with a validated
        ``aicad.simhandoff/1`` verify envelope under ``simhandoff``.
        """
        try:
            h = _as_obj(handoff)
            fr = _as_obj(fea_result)
        except (ValueError, TypeError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        if h is None:
            h = _HYD.get("fea_handoff")

        try:
            # The core unwraps a whole producer result (pulls `simhandoff`) itself.
            r = cb.call("hydraulic_verify_fea", {
                "handoff": h, "fea_result": fr,
                "tolerance_pct": float(tolerance_pct)})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(r)
