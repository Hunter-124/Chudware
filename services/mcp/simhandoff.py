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
(services/mcp/piping_tools.py). Further consumers: ECAD→thermal FEA,
thermal→CFD conjugate, motion→FEA reactions, hydraulic.
"""

from __future__ import annotations

import datetime as _dt
import math
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

def _is_finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _validate_condition_value(value: Any, path: str, errs: list) -> None:
    """Reject non-JSON/non-finite condition payloads before they cross solvers."""
    if _is_finite_number(value):
        return
    if isinstance(value, bool) or value is None:
        errs.append(f"{path}: must be a finite number, non-empty list, or object")
        return
    if isinstance(value, list):
        if not value:
            errs.append(f"{path}: list must not be empty")
            return
        for i, item in enumerate(value):
            _validate_condition_value(item, f"{path}[{i}]", errs)
        return
    if isinstance(value, dict):
        if not value:
            errs.append(f"{path}: object must not be empty")
            return
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                errs.append(f"{path}: object keys must be non-empty strings")
            if isinstance(item, (str, bool)):
                continue
            _validate_condition_value(item, f"{path}.{key}", errs)
        return
    errs.append(f"{path}: unsupported value type {type(value).__name__}")


def _validate_json_finite(value: Any, path: str, errs: list) -> None:
    """Validate arbitrary JSON metadata while rejecting NaN/Inf."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not _is_finite_number(value):
            errs.append(f"{path}: numeric value must be finite")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _validate_json_finite(item, f"{path}[{i}]", errs)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                errs.append(f"{path}: object keys must be non-empty strings")
            _validate_json_finite(item, f"{path}.{key}", errs)
        return
    errs.append(f"{path}: unsupported non-JSON value type {type(value).__name__}")


def _require_finite_builder(value: Any, name: str) -> float:
    if not _is_finite_number(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)

def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


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
    for label, value in (("doc_id", doc_id), ("study", study), ("handoff_id", handoff_id)):
        if value is not None:
            _required_text(value, f"study_ref.{label}")
    if result_digest is not None:
        if not isinstance(result_digest, dict) or not result_digest:
            raise ValueError("study_ref.result_digest must be a non-empty object when present")
        digest_errs: list = []
        _validate_json_finite(result_digest, "study_ref.result_digest", digest_errs)
        if digest_errs:
            raise ValueError("; ".join(digest_errs))
    return _clean({
        "domain": _required_text(domain, "study_ref.domain"),
        "tool": _required_text(tool, "study_ref.tool"),
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
    role = _required_text(role, "geometry_ref.role")
    kind = _required_text(kind, "geometry_ref.kind")
    units = _required_text(units, "geometry_ref.units")
    if path is not None and (not isinstance(path, str) or not path.strip()):
        raise ValueError("geometry_ref.path must be a non-empty string when present")
    if doc_id is not None and (not isinstance(doc_id, str) or not doc_id.strip()):
        raise ValueError("geometry_ref.doc_id must be a non-empty string when present")
    if derived_by is not None:
        _required_text(derived_by, "geometry_ref.derived_by")
    if inline is not None and (not isinstance(inline, dict) or not inline):
        raise ValueError("geometry_ref.inline must be a non-empty object when present")
    if path is None and doc_id is None and inline is None:
        raise ValueError("geometry_ref needs at least one of path/doc_id/inline")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("geometry_ref.meta must be an object when present")
    payload_errs: list = []
    if inline is not None:
        _validate_json_finite(inline, "geometry_ref.inline", payload_errs)
    if meta is not None:
        _validate_json_finite(meta, "geometry_ref.meta", payload_errs)
    if payload_errs:
        raise ValueError("; ".join(payload_errs))
    return _clean({
        "role": role,
        "kind": kind,
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
    value_errs: list = []
    _validate_condition_value(value, "condition.value", value_errs)
    if value_errs:
        raise ValueError("; ".join(value_errs))
    name = _required_text(name, "condition.name")
    kind = _required_text(kind, "condition.kind")
    units = _required_text(units, "condition.units")
    provenance = _required_text(provenance, "condition.provenance")
    if applied_to is not None:
        _required_text(applied_to, "condition.applied_to")
    if case is not None:
        _required_text(case, "condition.case")
    return _clean({
        "name": name,
        "kind": kind,
        "value": value,
        "units": units,
        "applied_to": applied_to,
        "case": case,
        "provenance": provenance,
    })


def expectation(quantity: str, value: _Number, units: str, provenance: str) -> dict:
    """Source-side prediction a later verify will compare against (contract §4)."""
    _require_finite_builder(value, "expectation.value")
    quantity = _required_text(quantity, "expectation.quantity")
    units = _required_text(units, "expectation.units")
    provenance = _required_text(provenance, "expectation.provenance")
    return {
        "quantity": quantity,
        "value": value,
        "units": units,
        "provenance": provenance,
    }


def build_handoff(coupling: str, source: dict, target: dict, *,
                  geometry: Optional[list] = None,
                  conditions: Optional[list] = None,
                  expectations: Optional[list] = None,
                  warnings: Optional[list] = None,
                  meta: Optional[dict] = None) -> dict:
    """Assemble a handoff envelope (contract §4); stamps schema/kind/id/created."""
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise ValueError("handoff.source and handoff.target must be study-ref objects")
    for label, value in (("geometry", geometry), ("conditions", conditions),
                         ("expectations", expectations), ("warnings", warnings)):
        if value is not None and not isinstance(value, list):
            raise ValueError(f"handoff.{label} must be a list when present")
    if meta is not None:
        if not isinstance(meta, dict):
            raise ValueError("handoff.meta must be an object when present")
        meta_errs: list = []
        _validate_json_finite(meta, "handoff.meta", meta_errs)
        if meta_errs:
            raise ValueError("; ".join(meta_errs))
    return _clean({
        "schema": SCHEMA,
        "kind": "handoff",
        "id": str(uuid.uuid4()),
        "created": _now_iso(),
        "coupling": _required_text(coupling, "handoff.coupling"),
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
    source_num = _require_finite_builder(source_value, "check.source_value")
    target_num = _require_finite_builder(target_value, "check.target_value")
    deviation_num = _require_finite_builder(deviation_pct, "check.deviation_pct")
    tolerance_num = _require_finite_builder(tolerance_pct, "check.tolerance_pct")
    if deviation_num < 0.0 or tolerance_num < 0.0:
        raise ValueError("check deviation_pct and tolerance_pct must be non-negative")
    if not isinstance(passed, bool):
        raise ValueError("check.passed must be a boolean")
    expected_pass = deviation_num <= tolerance_num
    if passed != expected_pass:
        raise ValueError(
            f"check.passed={passed} contradicts deviation_pct={deviation_num:g} and "
            f"tolerance_pct={tolerance_num:g}")
    quantity = _required_text(quantity, "check.quantity")
    units = _required_text(units, "check.units")
    return {
        "quantity": quantity,
        "units": units,
        "source_value": source_num,
        "target_value": target_num,
        "deviation_pct": deviation_num,
        "tolerance_pct": tolerance_num,
        "pass": passed,
    }


def build_verify(coupling: str, source: dict, target: dict, checks: list,
                 verdict: str, *,
                 handoff_id: Optional[str] = None,
                 advisories: Optional[list] = None,
                 warnings: Optional[list] = None) -> dict:
    """Assemble a verify envelope (contract §5). Overall ``pass`` is COMPUTED as the
    AND of the individual check passes (never caller-supplied, so it cannot drift)."""
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise ValueError("verify.source and verify.target must be study-ref objects")
    if not isinstance(checks, list) or not checks:
        raise ValueError("verify.checks must be a non-empty list")
    for label, value in (("advisories", advisories), ("warnings", warnings)):
        if value is not None and not isinstance(value, list):
            raise ValueError(f"verify.{label} must be a list when present")
    if handoff_id is not None:
        _required_text(handoff_id, "verify.handoff_id")
    coupling = _required_text(coupling, "verify.coupling")
    verdict = _required_text(verdict, "verify.verdict")
    return _clean({
        "schema": SCHEMA,
        "kind": "verify",
        "id": str(uuid.uuid4()),
        "created": _now_iso(),
        "coupling": coupling,
        "source": source,
        "target": target,
        "handoff_id": handoff_id,
        "checks": list(checks or []),
        "pass": all(c.get("pass") for c in (checks or [])) and bool(checks),
        "verdict": verdict,
        "advisories": list(advisories) if advisories else None,
        "warnings": list(warnings) if warnings else None,
    })


# ── validator ─────────────────────────────────────────────────────────────────

def _req_str(env: dict, key: str, path: str, errs: list) -> None:
    v = env.get(key)
    if not isinstance(v, str) or not v.strip():
        errs.append(f"{path}.{key}: required non-empty string")


def _validate_study_ref(ref: Any, path: str, errs: list) -> None:
    if not isinstance(ref, dict):
        errs.append(f"{path}: required study-ref object")
        return
    _req_str(ref, "domain", path, errs)
    _req_str(ref, "tool", path, errs)
    for key in ("doc_id", "study", "handoff_id"):
        if key in ref and (not isinstance(ref[key], str) or not ref[key].strip()):
            errs.append(f"{path}.{key}: must be a non-empty string when present")
    if "result_digest" in ref:
        digest = ref["result_digest"]
        if not isinstance(digest, dict):
            errs.append(f"{path}.result_digest: must be an object")
        else:
            _validate_json_finite(digest, f"{path}.result_digest", errs)


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
    if "meta" in env:
        if not isinstance(env["meta"], dict):
            errs.append("$.meta: must be an object when present")
        else:
            _validate_json_finite(env["meta"], "$.meta", errs)
    for diagnostic_key in ("warnings", "advisories"):
        if diagnostic_key in env and not isinstance(env[diagnostic_key], list):
            errs.append(f"$.{diagnostic_key}: must be a list when present")

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
                _req_str(g, "units", p, errs)
                path_ok = isinstance(g.get("path"), str) and bool(g["path"].strip())
                doc_ok = isinstance(g.get("doc_id"), str) and bool(g["doc_id"].strip())
                inline_ok = isinstance(g.get("inline"), dict) and bool(g["inline"])
                if not (path_ok or doc_ok or inline_ok):
                    errs.append(f"{p}: needs a non-empty path/doc_id/inline object")
                if "inline" in g and not isinstance(g["inline"], dict):
                    errs.append(f"{p}.inline: must be an object")
                if "meta" in g and not isinstance(g["meta"], dict):
                    errs.append(f"{p}.meta: must be an object")
                if isinstance(g.get("inline"), dict):
                    _validate_json_finite(g["inline"], f"{p}.inline", errs)
                if isinstance(g.get("meta"), dict):
                    _validate_json_finite(g["meta"], f"{p}.meta", errs)
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
                if "value" not in c:
                    errs.append(f"{p}.value: required")
                else:
                    _validate_condition_value(c["value"], f"{p}.value", errs)
                for optional_key in ("applied_to", "case"):
                    if optional_key in c and (
                            not isinstance(c[optional_key], str) or
                            not c[optional_key].strip()):
                        errs.append(f"{p}.{optional_key}: must be a non-empty string")
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
                    if not _is_finite_number(e.get("value")):
                        errs.append(f"{p}.value: required finite number")

    else:  # verify
        if "handoff_id" in env and (
                not isinstance(env["handoff_id"], str) or not env["handoff_id"].strip()):
            errs.append("$.handoff_id: must be a non-empty string when present")
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
            numeric_ok = True
            for k in ("source_value", "target_value", "deviation_pct", "tolerance_pct"):
                if not _is_finite_number(c.get(k)):
                    errs.append(f"{p}.{k}: required finite number")
                    numeric_ok = False
            passed = c.get("pass")
            if not isinstance(passed, bool):
                errs.append(f"{p}.pass: required boolean")
            if numeric_ok:
                deviation = float(c["deviation_pct"])
                tolerance = float(c["tolerance_pct"])
                if deviation < 0.0:
                    errs.append(f"{p}.deviation_pct: must be non-negative")
                if tolerance < 0.0:
                    errs.append(f"{p}.tolerance_pct: must be non-negative")
                if isinstance(passed, bool) and deviation >= 0.0 and tolerance >= 0.0:
                    expected = deviation <= tolerance
                    if passed != expected:
                        errs.append(
                            f"{p}.pass: contradicts deviation_pct <= tolerance_pct")
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
