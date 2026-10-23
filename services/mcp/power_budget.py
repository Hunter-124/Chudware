"""System power/energy budget — rail tree + duty cycle + battery verdict.

``aicad.powerbudget/1``: a rail-tree bookkeeping model that unifies the power story:

    source (battery or bench supply)
      → converters (LDO / buck / boost, data-driven efficiency)
        → per-rail loads (boardphys component supply metadata)

Duty-cycle weighting comes from an ``aicad.powerprofile/1`` artifact
(``contracts/firmware_rpc.md`` §11, produced by ``fw_sim_run`` / ``fw_power_profile``)
when one is provided. Without one the budget still works in **degraded mode**:
datasheet-typical active currents, every component assumed always active, and every
such number LABELLED as a degraded assumption — never silently presented as measured.

Per-rail currents are cross-checked against PDN results (``contracts/ecad/boardphys.md``
§3 ``pdn`` extension, ``ecad_run_pdn``) when the boardphys sidecar carries them:
copper capability (hotspots / current-density violations) and delivered-voltage drop.

This is deliberately **BOOKKEEPING, not SPICE** (same honesty rules as boardphys and
powerprofile, binding):

- unknown currents stay ``null`` — never an invented 0; gaps land in ``coverage``;
- every number carries a provenance string naming where it came from;
- when the sum omits unknown contributors it is a FLOOR and says so.

Pure module — no engine kernel needed; file I/O only when the MCP tool is handed
artifact paths. ``register_power_budget_tools`` wires the ``power_budget`` MCP tool;
``services/verify/battery.py`` exposes the same solve as the ``power_budget`` check
inside ``verify_battery``.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

SCHEMA = "aicad.powerbudget/1"
MODEL_NOTE = ("rail-tree duty-cycle bookkeeping over datasheet rail currents — "
              "NOT a SPICE/transient power simulation")

DEGRADED_LABEL = ("degraded: datasheet-typ active current, assumed always active "
                  "(no firmware power profile)")

# Ground-ish rails never carry a budget of their own (return path) — mirrors
# engine/firmware/powerprofile.py.
_GROUND_NAMES = {"GND", "GNDA", "GNDD", "AGND", "DGND", "PGND", "EGND",
                 "VSS", "VSSA"}

_TOPOLOGIES = ("ldo", "buck", "boost")


# ---------------------------------------------------------------------------
# Efficiency curves
# ---------------------------------------------------------------------------

def _eta_at(efficiency: Any, load_frac: Optional[float]) -> tuple[Optional[float], str]:
    """Resolve a converter efficiency spec at a load fraction.

    ``efficiency`` is either a fixed float (0..1] or a list of ``[load_frac, eta]``
    points (piecewise-linear, clamped at the ends). Returns ``(eta, provenance)``.
    """
    if efficiency is None:
        return None, "efficiency unknown (null) — input current cannot be derived"
    if isinstance(efficiency, (int, float)):
        e = float(efficiency)
        if not (0.0 < e <= 1.0):
            return None, f"efficiency {e} out of (0,1] — rejected"
        return e, f"fixed efficiency {e}"
    # Curve: [[load_frac, eta], ...]
    try:
        pts = sorted((float(p[0]), float(p[1])) for p in efficiency)
    except Exception:
        return None, f"unparseable efficiency spec {efficiency!r} — rejected"
    if not pts:
        return None, "empty efficiency curve — rejected"
    if load_frac is None:
        # No rating to normalise load against — honest fallback: curve mean, labelled.
        mean = sum(e for _, e in pts) / len(pts)
        return mean, (f"efficiency curve mean {round(mean, 4)} used — load_frac "
                      "unknown (converter has no i_out_max_a rating)")
    lf = max(0.0, float(load_frac))
    if lf <= pts[0][0]:
        return pts[0][1], f"efficiency {pts[0][1]} at curve low end (load_frac={round(lf, 4)})"
    if lf >= pts[-1][0]:
        return pts[-1][1], f"efficiency {pts[-1][1]} at curve high end (load_frac={round(lf, 4)})"
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= lf <= x1:
            t = 0.0 if x1 == x0 else (lf - x0) / (x1 - x0)
            eta = y0 + t * (y1 - y0)
            return eta, f"efficiency {round(eta, 4)} interpolated at load_frac={round(lf, 4)}"
    return pts[-1][1], "efficiency curve fallback (unreachable)"


# ---------------------------------------------------------------------------
# Load extraction from boardphys
# ---------------------------------------------------------------------------

def _loads_from_board(board: Optional[dict]) -> dict[str, list[dict]]:
    """rail name → [{ref, i_active_a, i_sleep_a, voltage_v_nom}] from a boardphys sidecar."""
    rails: dict[str, list[dict]] = {}
    if not isinstance(board, dict):
        return rails
    for comp in board.get("components") or []:
        supply = (comp or {}).get("supply") or {}
        for rail in supply.get("rails") or []:
            name = rail.get("name")
            if not name or name.upper() in _GROUND_NAMES:
                continue
            rails.setdefault(name, []).append({
                "ref": comp.get("ref"),
                "i_active_a": rail.get("current_a_active"),
                "i_sleep_a": rail.get("current_a_sleep"),
                "voltage_v_nom": rail.get("voltage_v_nom"),
            })
    return rails


def _sequencing_note(board: Optional[dict]) -> str:
    """Sequencing sanity note per boardphys honesty semantics ([]=known none, null=unknown)."""
    if not isinstance(board, dict):
        return "no board metadata — sequencing not assessed"
    constraints: list[str] = []
    unknown: list[str] = []
    for comp in board.get("components") or []:
        seq = ((comp or {}).get("supply") or {}).get("sequencing")
        if seq is None:
            unknown.append(comp.get("ref") or "?")
        else:
            constraints.extend(f"{comp.get('ref')}: {s}" for s in seq)
    if constraints:
        return ("sequencing constraints declared — verify enable order: "
                + "; ".join(sorted(constraints)))
    if unknown:
        return ("no sequencing constraints declared, but sequencing is UNKNOWN for "
                + ", ".join(sorted(unknown)) + " — check datasheets")
    return "no sequencing requirements declared by any component (known-none)"


# ---------------------------------------------------------------------------
# Rail current: profile-weighted or degraded
# ---------------------------------------------------------------------------

def _rail_currents(name: str, loads: list[dict], profile: Optional[dict],
                   coverage_unknown: list[str], notes: list[str]) -> dict:
    """Resolve (i_avg_a, i_peak_a, provenance, mode) for one rail's external loads."""
    prov: list[str] = []
    # Profile mode: the powerprofile already integrated duty per rail — trust it.
    if isinstance(profile, dict):
        for prail in profile.get("rails") or []:
            if prail.get("name") == name:
                prov.append(f"rail currents from aicad.powerprofile/1 "
                            f"(trace_ref={profile.get('trace_ref')!r})")
                prov.extend(str(p) for p in prail.get("provenance") or [])
                return {"i_avg_a": prail.get("i_avg_a"),
                        "i_peak_a": prail.get("i_peak_a"),
                        "provenance": prov, "mode": "profile"}
    # Degraded mode (also used for rails the profile doesn't cover).
    known = [ld for ld in loads if ld.get("i_active_a") is not None]
    for ld in loads:
        if ld.get("i_active_a") is None:
            coverage_unknown.append(f"{ld.get('ref')}:{name}")
            prov.append(f"{ld.get('ref')}: active current unknown (null) — "
                        "omitted from sum (sum is a FLOOR)")
        else:
            prov.append(f"{ld.get('ref')}: {ld['i_active_a']} A — {DEGRADED_LABEL}")
    if not known:
        if loads:
            notes.append(f"rail {name}: no contributor has a known active current — "
                         "currents null")
        return {"i_avg_a": None, "i_peak_a": None, "provenance": prov,
                "mode": "degraded" if loads else "none"}
    total = sum(ld["i_active_a"] for ld in known)
    return {"i_avg_a": total, "i_peak_a": total, "provenance": prov, "mode": "degraded"}


# ---------------------------------------------------------------------------
# PDN cross-check
# ---------------------------------------------------------------------------

def _pdn_check(name: str, i_avg: Optional[float], i_peak: Optional[float],
               board: Optional[dict], warnings: list[str]) -> Optional[dict]:
    """Cross-check one rail against aicad.pdn/1 results when present."""
    pdn = (board or {}).get("pdn") if isinstance(board, dict) else None
    if not isinstance(pdn, dict):
        return None
    entry = next((n for n in pdn.get("nets") or [] if n.get("net") == name), None)
    if entry is None:
        return None
    drops = entry.get("drops") or []
    modelled = [d.get("i_a") for d in drops if d.get("i_a") is not None]
    modelled_i = sum(modelled) if modelled else None
    check: dict[str, Any] = {
        "net": name,
        "modelled_i_a": modelled_i,
        "worst_drop": entry.get("worst_drop"),
        "hotspots": len(entry.get("hotspots") or []),
        "violations": list(entry.get("violations") or []),
        "unknown_sinks": list(entry.get("unknown_sinks") or []),
        "provenance": [f"pdn cross-check: aicad.pdn/1 net {name!r} "
                       f"(generated_by={pdn.get('generated_by')!r})"],
    }
    budget_i = i_peak if i_peak is not None else i_avg
    if modelled_i is not None and budget_i is not None and budget_i > modelled_i * 1.2:
        warnings.append(f"rail {name}: budget current {round(budget_i, 6)} A exceeds "
                        f"PDN-modelled {round(modelled_i, 6)} A by >20% — PDN results "
                        "are stale for this scenario, re-run ecad_run_pdn")
    wd = entry.get("worst_drop") or {}
    if wd.get("within_tolerance") is False:
        warnings.append(f"rail {name}: PDN worst drop at {wd.get('ref')} delivers "
                        f"{wd.get('v_pad')} V (below rail minimum) — copper drop violation")
    if entry.get("hotspots"):
        warnings.append(f"rail {name}: {len(entry['hotspots'])} PDN current-density "
                        "hotspot(s) exceed the copper limit")
    return check


# ---------------------------------------------------------------------------
# The solve
# ---------------------------------------------------------------------------

def solve_power_budget(source: dict,
                       converters: Optional[list] = None,
                       board: Optional[dict] = None,
                       power_profile: Optional[dict] = None,
                       scenario: str = "") -> dict:
    """Solve the rail tree and return the ``aicad.powerbudget/1`` verdict dict.

    Args:
        source: ``{"kind": "battery"|"supply", "name": "VBAT", "voltage_v": 3.6,
                 "capacity_mah": 2400 (battery), "i_max_a": optional rating}``.
        converters: ``[{"name", "input": <source name or another converter's output>,
                 "output": <rail name>, "voltage_out_v", "topology": "ldo|buck|boost",
                 "efficiency": 0.9 | [[load_frac, eta], ...], "i_out_max_a",
                 "i_q_a": optional quiescent}]``.
        board: an ``aicad.boardphys/1`` sidecar dict (loads + sequencing + pdn).
        power_profile: an ``aicad.powerprofile/1`` dict (duty-cycle-weighted rail
                 currents). Omitted → degraded mode, labelled.
        scenario: free-text scenario name carried into provenance.
    """
    if not isinstance(source, dict) or source.get("voltage_v") is None:
        return {"ok": False, "error": "source must be a dict with at least voltage_v"}
    converters = list(converters or [])
    warnings: list[str] = []
    hard_failures: list[str] = []  # overload / rating-exceeded — drive verdict="fail"
    notes: list[str] = []
    coverage_unknown: list[str] = []

    if isinstance(power_profile, dict) and power_profile.get("schema") not in (
            None, "aicad.powerprofile/1"):
        warnings.append(f"power_profile schema {power_profile.get('schema')!r} is not "
                        "aicad.powerprofile/1 — used best-effort")

    src_name = source.get("name") or "SOURCE"
    v_src = float(source["voltage_v"])

    by_output: dict[str, dict] = {}
    for cv in converters:
        out = cv.get("output")
        if not out:
            return {"ok": False, "error": f"converter {cv.get('name')!r} has no output rail"}
        if out in by_output:
            return {"ok": False, "error": f"two converters drive rail {out!r}"}
        topo = (cv.get("topology") or "ldo").lower()
        if topo not in _TOPOLOGIES:
            return {"ok": False,
                    "error": f"converter {cv.get('name')!r}: unknown topology {topo!r}"}
        by_output[out] = cv

    board_loads = _loads_from_board(board)
    all_rails = sorted(set(board_loads) | set(by_output))

    # Rail external-load currents (loads only; converter chaining added below).
    ext: dict[str, dict] = {
        r: _rail_currents(r, board_loads.get(r, []), power_profile,
                          coverage_unknown, notes)
        for r in all_rails
    }
    if src_name in board_loads and src_name not in ext:
        ext[src_name] = _rail_currents(src_name, board_loads[src_name], power_profile,
                                       coverage_unknown, notes)
    modes = {e["mode"] for e in ext.values() if e["mode"] != "none"}
    degraded = not isinstance(power_profile, dict)
    if degraded:
        notes.append("DEGRADED MODE: no aicad.powerprofile/1 supplied — datasheet-typ "
                     "currents, every load assumed always active; battery life is a "
                     "conservative bound, not a duty-cycled estimate")
    elif "degraded" in modes:
        notes.append("partial profile coverage: some rails fell back to degraded "
                     "datasheet-typ assumptions (see rail provenance)")

    # Feed map: input rail → converters hanging off it (for chained trees).
    children: dict[str, list[dict]] = {}
    for cv in converters:
        children.setdefault(cv.get("input") or src_name, []).append(cv)

    # Every converter must be reachable from the source (a dangling/cyclic input
    # would silently drop its rail from the budget — reject instead).
    reachable: set[str] = set()
    stack = [src_name]
    while stack:
        rail = stack.pop()
        if rail in reachable:
            continue
        reachable.add(rail)
        stack.extend(cv["output"] for cv in children.get(rail, []))
    orphans = sorted(cv.get("name") or cv["output"] for cv in converters
                     if (cv.get("input") or src_name) not in reachable)
    if orphans:
        return {"ok": False,
                "error": f"converter(s) {orphans} not reachable from source "
                         f"{src_name!r} — dangling input or cycle in the rail tree"}

    def _v_in_of(rail: str) -> Optional[float]:
        if rail == src_name:
            return v_src
        cv = by_output.get(rail)
        return None if cv is None else cv.get("voltage_out_v")

    rail_reports: list[dict] = []
    visiting: set[str] = set()

    def _rail_demand(rail: str) -> tuple[Optional[float], Optional[float]]:
        """Total (i_avg, i_peak) drawn FROM ``rail``: its loads + child converter inputs.

        Nulls propagate honestly: an unknown component makes the total a floor, an
        unknown converter input makes the total null.
        """
        if rail in visiting:
            raise ValueError(f"converter cycle through rail {rail!r}")
        visiting.add(rail)
        try:
            e = ext.get(rail) or {"i_avg_a": None, "i_peak_a": None,
                                  "provenance": [], "mode": "none"}
            avg, peak = e["i_avg_a"], e["i_peak_a"]
            prov = list(e["provenance"])
            for cv in children.get(rail, []):
                child_rail = cv["output"]
                c_avg, c_peak = _rail_demand(child_rail)
                in_avg, p_avg = _converter_input(cv, rail, c_avg)
                in_peak, _ = _converter_input(cv, rail, c_peak)
                prov.append(f"{cv.get('name')}: draws {_fmt(in_avg)} A avg / "
                            f"{_fmt(in_peak)} A peak from {rail} — {p_avg}")
                avg = _add(avg, in_avg)
                peak = _add(peak, in_peak)
                _report_rail(cv, child_rail, c_avg, c_peak, in_avg, in_peak, p_avg)
            ext.setdefault(rail, {"i_avg_a": None, "i_peak_a": None,
                                  "provenance": [], "mode": "none"})
            ext[rail]["provenance"] = prov
            return avg, peak
        finally:
            visiting.discard(rail)

    def _converter_input(cv: dict, in_rail: str,
                         i_out: Optional[float]) -> tuple[Optional[float], str]:
        """Input current a converter draws for a given output current."""
        i_q = cv.get("i_q_a") or 0.0
        if i_out is None:
            return None, "output current unknown — input current null"
        topo = (cv.get("topology") or "ldo").lower()
        v_out = cv.get("voltage_out_v")
        v_in = _v_in_of(in_rail)
        if topo == "ldo":
            eta_note = "LDO: i_in = i_out + i_q"
            return i_out + i_q, eta_note
        i_max = cv.get("i_out_max_a")
        load_frac = (i_out / i_max) if i_max else None
        eta, eta_note = _eta_at(cv.get("efficiency"), load_frac)
        if eta is None or v_out is None or v_in is None or v_in <= 0:
            return None, f"{eta_note}; v_in/v_out/eta incomplete — input current null"
        return (v_out * i_out) / (eta * v_in) + i_q, eta_note

    reported: dict[str, dict] = {}

    def _report_rail(cv: dict, rail: str, i_avg, i_peak, in_avg, in_peak, eta_note):
        e = ext.get(rail) or {"provenance": [], "mode": "none"}
        i_max = cv.get("i_out_max_a")
        basis = i_peak if i_peak is not None else i_avg
        overload = bool(i_max is not None and basis is not None and basis > i_max)
        margin = (None if i_max is None or basis is None
                  else round(1.0 - basis / i_max, 4))
        if overload:
            msg = (f"rail {rail}: {_fmt(basis)} A demanded exceeds "
                   f"{cv.get('name')} rating {i_max} A (margin {margin})")
            warnings.append(msg)
            hard_failures.append(msg)
        elif i_max is not None and basis is None:
            warnings.append(f"rail {rail}: demand unknown (null) — cannot prove it fits "
                            f"{cv.get('name')} rating {i_max} A")
        prov = list(e["provenance"])
        prov.append(f"converter {cv.get('name')} ({(cv.get('topology') or 'ldo').lower()}): "
                    f"{eta_note}; scenario={scenario or 'unspecified'}")
        reported[rail] = {
            "name": rail,
            "voltage_v_nom": cv.get("voltage_out_v"),
            "i_avg_a": i_avg, "i_peak_a": i_peak,
            "mode": e["mode"] if e["mode"] != "none" else ("profile" if not degraded else "degraded"),
            "converter": {
                "name": cv.get("name"),
                "topology": (cv.get("topology") or "ldo").lower(),
                "i_out_max_a": i_max,
                "i_in_avg_a": _round(in_avg), "i_in_peak_a": _round(in_peak),
                "load_frac": (None if i_max is None or basis is None
                              else round(basis / i_max, 4)),
                "margin_frac": margin,
                "overload": overload,
            },
            "pdn_check": _pdn_check(rail, i_avg, i_peak, board, warnings),
            "provenance": prov,
        }

    try:
        src_avg, src_peak = _rail_demand(src_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Loads hanging directly on the source rail get a report row too.
    if src_name in ext and ext[src_name]["mode"] != "none":
        e = ext[src_name]
        reported.setdefault(src_name, {
            "name": src_name, "voltage_v_nom": v_src,
            "i_avg_a": e["i_avg_a"], "i_peak_a": e["i_peak_a"],
            "mode": e["mode"], "converter": None,
            "pdn_check": _pdn_check(src_name, e["i_avg_a"], e["i_peak_a"],
                                    board, warnings),
            "provenance": list(e["provenance"]),
        })
    rail_reports = [reported[k] for k in sorted(reported)]

    worst = None
    margins = [(r["converter"]["margin_frac"], r["name"]) for r in rail_reports
               if r.get("converter") and r["converter"]["margin_frac"] is not None]
    if margins:
        m, n = min(margins)
        worst = {"rail": n, "margin_frac": m}
        if 0.0 <= m < 0.2:
            warnings.append(f"rail {n}: worst converter margin {m} (<20%) — "
                            "little headroom for scenario growth")

    # Source verdict.
    i_max_src = source.get("i_max_a")
    src_basis = src_peak if src_peak is not None else src_avg
    if i_max_src is not None and src_basis is not None and src_basis > i_max_src:
        msg = (f"source {src_name}: {_fmt(src_basis)} A demanded exceeds its "
               f"{i_max_src} A rating")
        warnings.append(msg)
        hard_failures.append(msg)

    battery = None
    if (source.get("kind") or "battery") == "battery":
        cap_mah = source.get("capacity_mah")
        life_h = None
        label = "battery life unknown — capacity or average draw unknown"
        if cap_mah is not None and src_avg:
            life_h = (float(cap_mah) / 1000.0) / src_avg
            floor = bool(coverage_unknown)
            label = (f"life = capacity_ah / i_avg = {round(float(cap_mah) / 1000.0, 6)} / "
                     f"{_fmt(src_avg)} = {round(life_h, 1)} h"
                     + (" — UPPER BOUND: unknown contributors omitted from i_avg"
                        if floor else "")
                     + (" — conservative (degraded always-active assumption)"
                        if degraded else " — duty-cycled via aicad.powerprofile/1"))
        battery = {"capacity_mah": cap_mah, "i_source_avg_a": _round(src_avg),
                   "i_source_peak_a": _round(src_peak),
                   "life_h": None if life_h is None else round(life_h, 2),
                   "note": label}

    verdict = "fail" if hard_failures else ("warn" if warnings else "ok")
    findings = ([{"severity": "fail", "detail": w} for w in hard_failures] +
                [{"severity": "warn", "detail": w} for w in warnings
                 if w not in hard_failures])

    return {
        "ok": verdict != "fail",
        "schema": SCHEMA,
        "model": MODEL_NOTE,
        "generated_at": time.time(),
        "scenario": scenario or None,
        "degraded": degraded,
        "source": {"name": src_name, "kind": source.get("kind") or "battery",
                   "voltage_v": v_src, "i_max_a": i_max_src,
                   "i_avg_a": _round(src_avg), "i_peak_a": _round(src_peak)},
        "battery": battery,
        "rails": rail_reports,
        "worst_rail": worst,
        "sequencing_note": _sequencing_note(board),
        "coverage": {"unknown_active_current": sorted(coverage_unknown),
                     "notes": notes},
        "warnings": warnings,
        "findings": findings,
        "verdict": verdict,
    }


def _add(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _round(x: Optional[float], nd: int = 12) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _fmt(x: Optional[float]) -> str:
    return "null" if x is None else str(round(float(x), 6))


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

def _load_json(path: str, what: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{what} at {path} is not a JSON object")
    return data


def register_power_budget_tools(mcp) -> None:
    """Register the system power-budget tool on the given FastMCP instance."""

    @mcp.tool()
    def power_budget(source: dict,
                     converters: list = None,
                     board_path: str = "",
                     power_profile_path: str = "",
                     board: dict = None,
                     power_profile: dict = None,
                     scenario: str = "") -> str:
        """System power/energy budget — rail tree, duty cycle, battery verdict (aicad.powerbudget/1).

        Bookkeeping, NOT SPICE: source (battery/supply) → converters (LDO/buck/boost with
        fixed or [[load_frac, eta]] efficiency curves) → per-rail loads taken from an
        aicad.boardphys/1 sidecar (ecad_to_3d). Duty-cycle weighting comes from an
        aicad.powerprofile/1 artifact (fw_sim_run / fw_power_profile) when given; without
        one the budget runs DEGRADED (datasheet-typ currents, always-active, labelled).
        Per-rail currents are cross-checked against PDN results (ecad_run_pdn) when the
        sidecar carries them. Nulls stay null — never an invented 0; every number carries
        a provenance string.

        source: {"kind":"battery"|"supply","name":"VBAT","voltage_v":3.6,
                 "capacity_mah":2400,"i_max_a":?}. converters: [{"name","input","output",
                 "voltage_out_v","topology":"ldo|buck|boost","efficiency",
                 "i_out_max_a","i_q_a"}]. board_path/power_profile_path load the
        artifacts from disk (inline board/power_profile dicts win).

        Returns {ok, verdict: ok|warn|fail, battery:{life_h,...}, rails:[{i_avg_a,
        i_peak_a, converter:{margin_frac, overload}, pdn_check, provenance}], worst_rail,
        sequencing_note, coverage, warnings}. Also runs inside verify_battery as the
        'power_budget' check.
        """
        try:
            b = board
            if b is None and board_path:
                b = _load_json(board_path, "boardphys sidecar")
            p = power_profile
            if p is None and power_profile_path:
                p = _load_json(power_profile_path, "power profile")
            return json.dumps(
                solve_power_budget(source, converters=converters, board=b,
                                   power_profile=p, scenario=scenario),
                indent=2)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})
