"""aicad.simhandoff/1 — cross-sim handoff & verify envelope helpers.

Executable form of ``contracts/sim_handoff.md``: builders for the three envelope shapes
(study ref, handoff, verify) plus a structural validator. One physics domain solves,
hands geometry + generated BCs/loads (with explicit units and provenance) to another
solver, and a verify step cross-checks the two with a tolerance verdict.

Design rules (mirroring the contract):
  * No FastMCP / engine imports — pure dict builders so tool modules AND engine-less
    tests can use it.
  * Conformance is ADDITIVE: producing tools embed the envelope under the reserved
    ``simhandoff`` key of their existing result payload via :func:`attach`; existing
    result fields are never touched.
  * Open vocabularies: coupling ids, condition kinds, geometry roles and check
    quantities are open strings — :func:`validate_envelope` checks structure only.
  * Units and provenance are explicit, always.

First conforming pair: ``piping_cfd_handoff`` / ``piping_verify_cfd``
(services/mcp/piping_tools.py). Future consumers: T-FIN16 (ECAD→thermal FEA),
T-FIN19 (thermal→CFD conjugate), T-FIN20 (motion→FEA reactions), T-FIN22 (hydraulic).
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Optional, Union

SCHEMA = "aicad.simhandoff/1"

_Number = Union[int, float]
_CondValue = Union[int, float, list, dict]


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(d: dict) -> dict:
    """Drop keys whose value is None so envelopes stay compact."""
    return {k: v for k, v in d.items() if v is not None}


# ── builders ──────────────────────────────────────────────────────────────────

def study_ref(domain: str, tool: str, *,
              doc_id: Optional[str] = None,
              study: Optional[str] = None,
              handoff_id: Optional[str] = None,
              result_digest: Optional[dict] = None) -> dict:
    """Provenance atom (contract §3): names what produced (or will consume) a result.

    ``handoff_id`` links a target-side study back to the handoff envelope that spawned
    it, closing the chain source study → handoff → target study → verify.
    ``result_digest`` is a SMALL inline excerpt (key numbers only), never a full result.
    """
    return _clean({
        "domain": str(domain),
        "tool": str(tool),
        "doc_id": doc_id,
        "study": study,
        "handoff_id": handoff_id,
        "result_digest": result_digest,
    })


def geometry_ref(role: str, kind: str, *,
                 path: Optional[str] = None,
                 doc_id: Optional[str] = None,
                 inline: Optional[dict] = None,
                 derived_by: Optional[str] = None,
                 units: str = "mm",
                 meta: Optional[dict] = None) -> dict:
    """Derived-geometry entry (contract §4). At least one of path/doc_id/inline required.

    ``role`` is the semantic role in the coupling (fluid_domain, board,
    component_bodies, doc, …); ``kind`` the payload kind (step, stl, doc, doc_id, mesh).
    ``inline`` carries small generated documents (e.g. an aicad.piping/1 doc for
    doc-to-doc couplings like hydraulic→piping).
    """
    return _clean({
        "role": str(role),
        "kind": str(kind),
        "path": path,
        "doc_id": doc_id,
        "inline": inline,
        "derived_by": derived_by,
        "units": units,
        "meta": meta,
    })


def condition(name: str, kind: str, value: _CondValue, units: str, provenance: str, *,
              applied_to: Optional[str] = None,
              case: Optional[str] = None) -> dict:
    """Generated boundary condition / load (contract §4).

    ``value`` may be a scalar, an [x, y, z] vector (forces/moments), or an object
    (tables). ``units`` is the explicit unit string ("m/s", "kPa", "W", "N", "1", …).
    ``provenance`` names the source-result field(s) it was derived from.
    ``applied_to`` binds it to a region/face/component/node; ``case`` labels a load
    case (e.g. motion→FEA per-pose cases).
    """
    return _clean({
        "name": str(name),
        "kind": str(kind),
        "value": value,
        "units": str(units),
        "applied_to": applied_to,
        "case": case,
        "provenance": str(provenance),
    })


def expectation(quantity: str, value: _Number, units: str, provenance: str) -> dict:
    """Source-side prediction a later verify will compare against (contract §4)."""
    return {
        "quantity": str(quantity),
        "value": value,
        "units": str(units),
        "provenance": str(provenance),
    }


def build_handoff(coupling: str, source: dict, target: dict, *,
                  geometry: Optional[list] = None,
                  conditions: Optional[list] = None,
                  expectations: Optional[list] = None,
                  warnings: Optional[list] = None,
                  meta: Optional[dict] = None) -> dict:
    """Assemble a handoff envelope (contract §4); stamps schema/kind/id/created."""
    return _clean({
        "schema": SCHEMA,
        "kind": "handoff",
        "id": str(uuid.uuid4()),
        "created": _now_iso(),
        "coupling": str(coupling),
        "source": source,
        "target": target,
        "geometry": list(geometry or []),
        "conditions": list(conditions or []),
        "expectations": list(expectations) if expectations else None,
        "warnings": list(warnings) if warnings else None,
        "meta": meta,
    })


def check(quantity: str, units: str, source_value: _Number, target_value: _Number,
          deviation_pct: _Number, tolerance_pct: _Number, passed: bool) -> dict:
    """One quantitative comparison inside a verify envelope (contract §5)."""
    return {
        "quantity": str(quantity),
        "units": str(units),
        "source_value": source_value,
        "target_value": target_value,
        "deviation_pct": deviation_pct,
        "tolerance_pct": tolerance_pct,
        "pass": bool(passed),
    }


def build_verify(coupling: str, source: dict, target: dict, checks: list,
                 verdict: str, *,
                 handoff_id: Optional[str] = None,
                 advisories: Optional[list] = None,
                 warnings: Optional[list] = None) -> dict:
    """Assemble a verify envelope (contract §5). Overall ``pass`` is COMPUTED as the
    AND of the individual check passes (never caller-supplied, so it cannot drift)."""
    return _clean({
        "schema": SCHEMA,
        "kind": "verify",
        "id": str(uuid.uuid4()),
        "created": _now_iso(),
        "coupling": str(coupling),
        "source": source,
        "target": target,
        "handoff_id": handoff_id,
        "checks": list(checks or []),
        "pass": all(c.get("pass") for c in (checks or [])) and bool(checks),
        "verdict": str(verdict),
        "advisories": list(advisories) if advisories else None,
        "warnings": list(warnings) if warnings else None,
    })


# ── validator ─────────────────────────────────────────────────────────────────

def _req_str(env: dict, key: str, path: str, errs: list) -> None:
    v = env.get(key)
    if not isinstance(v, str) or not v:
        errs.append(f"{path}.{key}: required non-empty string")


def _validate_study_ref(ref: Any, path: str, errs: list) -> None:
    if not isinstance(ref, dict):
        errs.append(f"{path}: required study-ref object")
        return
    _req_str(ref, "domain", path, errs)
    _req_str(ref, "tool", path, errs)


def validate_envelope(env: Any) -> list:
    """Structural validation of a handoff or verify envelope.

    Returns a list of JSON-path-prefixed error strings (empty = valid), matching the
    ``engine.doc.schema`` / ``engine.piping.schema`` style. Checks structure only —
    coupling ids, condition kinds, geometry roles and quantities are open vocabularies.
    """
    errs: list = []
    if not isinstance(env, dict):
        return ["$: envelope must be an object"]
    if env.get("schema") != SCHEMA:
        errs.append(f"$.schema: must be {SCHEMA!r}")
    kind = env.get("kind")
    if kind not in ("handoff", "verify"):
        errs.append("$.kind: must be 'handoff' or 'verify'")
        return errs
    _req_str(env, "id", "$", errs)
    _req_str(env, "created", "$", errs)
    _req_str(env, "coupling", "$", errs)
    _validate_study_ref(env.get("source"), "$.source", errs)
    _validate_study_ref(env.get("target"), "$.target", errs)

    if kind == "handoff":
        geom = env.get("geometry")
        if not isinstance(geom, list):
            errs.append("$.geometry: required list (may be empty for doc-to-doc)")
        else:
            for i, g in enumerate(geom):
                p = f"$.geometry[{i}]"
                if not isinstance(g, dict):
                    errs.append(f"{p}: must be an object")
                    continue
                _req_str(g, "role", p, errs)
                _req_str(g, "kind", p, errs)
                if not any(g.get(k) is not None for k in ("path", "doc_id", "inline")):
                    errs.append(f"{p}: needs at least one of path/doc_id/inline")
        conds = env.get("conditions")
        if not isinstance(conds, list):
            errs.append("$.conditions: required list")
        else:
            seen: set = set()
            for i, c in enumerate(conds):
                p = f"$.conditions[{i}]"
                if not isinstance(c, dict):
                    errs.append(f"{p}: must be an object")
                    continue
                _req_str(c, "name", p, errs)
                _req_str(c, "kind", p, errs)
                _req_str(c, "units", p, errs)
                _req_str(c, "provenance", p, errs)
                if c.get("value") is None:
                    errs.append(f"{p}.value: required")
                n = c.get("name")
                if n in seen:
                    errs.append(f"{p}.name: duplicate name {n!r}")
                seen.add(n)
        exps = env.get("expectations")
        if exps is not None:
            if not isinstance(exps, list):
                errs.append("$.expectations: must be a list when present")
            else:
                for i, e in enumerate(exps):
                    p = f"$.expectations[{i}]"
                    if not isinstance(e, dict):
                        errs.append(f"{p}: must be an object")
                        continue
                    _req_str(e, "quantity", p, errs)
                    _req_str(e, "units", p, errs)
                    _req_str(e, "provenance", p, errs)
                    if e.get("value") is None:
                        errs.append(f"{p}.value: required")

    else:  # verify
        checks = env.get("checks")
        if not isinstance(checks, list) or not checks:
            errs.append("$.checks: required non-empty list")
            checks = []
        for i, c in enumerate(checks):
            p = f"$.checks[{i}]"
            if not isinstance(c, dict):
                errs.append(f"{p}: must be an object")
                continue
            _req_str(c, "quantity", p, errs)
            _req_str(c, "units", p, errs)
            for k in ("source_value", "target_value", "deviation_pct", "tolerance_pct"):
                if not isinstance(c.get(k), (int, float)) or isinstance(c.get(k), bool):
                    errs.append(f"{p}.{k}: required number")
            if not isinstance(c.get("pass"), bool):
                errs.append(f"{p}.pass: required boolean")
        if not isinstance(env.get("pass"), bool):
            errs.append("$.pass: required boolean")
        elif checks and all(isinstance(c, dict) and isinstance(c.get("pass"), bool) for c in checks):
            if env["pass"] != all(c["pass"] for c in checks):
                errs.append("$.pass: must equal the AND of all check passes")
        _req_str(env, "verdict", "$", errs)

    return errs


def attach(result: dict, env: dict) -> dict:
    """Validate ``env`` and embed it under ``result['simhandoff']``.

    Raises ``ValueError`` on an invalid envelope — an invalid envelope is a PRODUCER
    bug (the tool built it wrong), never bad user input, so failing loudly in tests is
    correct. Returns ``result`` for chaining. Existing result fields are untouched.
    """
    errs = validate_envelope(env)
    if errs:
        raise ValueError(f"{SCHEMA} envelope invalid: " + "; ".join(errs))
    result["simhandoff"] = env
    return result
