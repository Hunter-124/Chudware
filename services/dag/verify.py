"""verify.py — Verify node runner for the aicad.dag/1 orchestrator.

Contract: contracts/dag_schema.md §3 (node type "verify"), §8 (NodeState).
Called from graph_loader.py.
Reused by: the design_review() battery (DISPATCH_TABLE is the public API).

Each verify node specifies a ``check_type`` in its ``inputs`` dict and (optionally) an
``end_condition`` predicate string that is evaluated against the tool result.  The runner
calls the real MCP tool function (via the battery._get_fn singleton so there is ONE live
tool registry), evaluates the predicate, and returns ``(verdict, output_state)`` matching
node_runner.py's contract so graph_loader routes identically.

FAIL-CLOSED policy (mandatory — never fabricate pass verdicts):
  - Backend unavailable / import error           → ESCALATE (reason: "backend_unavailable")
  - Tool returns ``{"ok": false}``               → ESCALATE (reason: "tool_error")
  - Predicate evaluation error                   → ESCALATE (reason: "predicate_error")
  - Predicate evaluates False / default check fails → RETRY (count against budget)
  - All clear                                    → DONE

Mapping (check_type → MCP tool name, module_key for battery._get_fn):
  interference   → check_interference     (cad)
  erc            → ecad_run_erc           (ecad)
  drc            → ecad_run_drc           (ecad)
  firmware       → fw_sim_run             (firmware)  [~] may be absent on base
  dc_ratio       → struct_check           (structural) [~] may be absent on base
  budget         → bom_price             (sourcing)   [~] may be absent on base
  fatigue        → run_fea_fatigue        (cad)       infinite-life gate
  thermal        → run_fea_thermal        (cad)       ccx subprocess
  buckling       → run_fea_buckling       (cad)       load-factor≥1 gate
  contact        → run_fea_contact        (cad)       engine unilateral-contact gate

[~] = backend may be absent on non-merged base branches; calls degrade to ESCALATE
      with reason "backend_unavailable" (never a silent pass).
"""
from __future__ import annotations

import json
import math
import time
import warnings
from typing import Any, Callable, Optional

# Re-use the battery's private MCP singleton (one registry per module_key, built once).
# This keeps design_review from building a second, divergent registry.
from services.verify.battery import _get_fn as _battery_get_fn

# Verdicts (same names as node_runner so graph_loader routes identically).
VERDICT_DONE = "done"
VERDICT_RETRY = "retry"
VERDICT_ESCALATE = "escalate"

EmitFn = Optional[Callable[[str, dict[str, Any]], None]]

# ---------------------------------------------------------------------------
# Dispatch table — PUBLIC API (reused by design_review)
#
# Each entry:
#   check_type → (module_key, tool_fn_name, default_predicate_fn)
#
# default_predicate_fn(result_dict) → bool
#   Called when no end_condition is supplied.  Must be conservative (fail-closed):
#   if the result dict lacks the expected key, return False.
# ---------------------------------------------------------------------------

def _check_interference_default(r: dict) -> bool:
    """Pass when the check completed (ok=True) AND found no interference (clear=True).

    check_interference returns ok=True when the check ran to completion, also when it
    finds interference; ``clear`` is the clash verdict. An incomplete check is ok=False.
    """
    return r.get("ok") is True and r.get("clear") is True

def _check_erc_default(r: dict) -> bool:
    """Pass when error count is zero (warnings are advisory only)."""
    return int(r.get("errors", 1)) == 0

def _check_drc_default(r: dict) -> bool:
    """Pass when error count is zero."""
    return int(r.get("errors", 1)) == 0

def _check_firmware_default(r: dict) -> bool:
    """Pass when passed=True (all assertions held)."""
    return bool(r.get("passed", False))

def _check_dc_ratio_default(r: dict) -> bool:
    """Pass when every member's dc_ratio ≤ 1.0 (passed=True in each result row)."""
    results = r.get("results", [])
    if not results:
        return False
    return all(bool(row.get("passed", False)) for row in results)

def _check_budget_default(r: dict) -> bool:
    """Pass when ok=True and unit_cost is within the target_budget input.

    The target_budget is carried in the node inputs; the runner injects it
    into the evaluation namespace as 'target_budget' alongside 'result'.
    For the DEFAULT predicate (no end_condition given) we can only check
    that the tool call succeeded — the caller must supply end_condition for
    real budget gating.
    """
    return bool(r.get("ok", False))

def _check_vibration_default(r: dict) -> bool:
    """Pass when ok=True AND the tool's own spec verdict passed.

    run_fea_frequency_response returns verdict=None when the node supplied no
    vibration_spec — that is NOT a pass (fail-closed: the caller must state the
    requirement, either via vibration_spec inputs or an explicit end_condition).
    """
    if not r.get("ok", False):
        return False
    verdict = r.get("verdict")
    if not isinstance(verdict, dict):
        return False
    return bool(verdict.get("passed", False))


# Advanced-FEA default predicates (fail-closed; caller supplies an
# end_condition for a real numeric gate). A "skipped" result (backend absent)
# is NOT a pass — it escalates, never silently green.
def _check_fatigue_default(r: dict) -> bool:
    """Pass when the fatigue solve ran and the reported theory is in the INFINITE
    (runout) regime — finite/overload life fails closed."""
    if not r.get("ok", False) or r.get("skipped"):
        return False
    return r.get("regime") == "infinite"

def _check_thermal_default(r: dict) -> bool:
    """Pass when the thermal solve ran and returned a mid-plane temperature."""
    if not r.get("ok", False) or r.get("skipped"):
        return False
    return r.get("mid_plane_T_c") is not None

def _check_buckling_default(r: dict) -> bool:
    """Pass when the buckling solve ran and the lowest load factor ≥ 1.0
    (the part does NOT buckle below the applied load)."""
    if not r.get("ok", False) or r.get("skipped"):
        return False
    factor = r.get("critical_load_factor")
    return (isinstance(factor, (int, float)) and not isinstance(factor, bool)
            and math.isfinite(factor) and factor >= 1.0)

def _check_contact_default(r: dict) -> bool:
    """Pass when a real contact solve converged with load actually crossing an interface.

    run_fea_contact solves unilateral contact on the document's own bodies now, so the
    gate is the contact block itself: the active set settled and at least one pair is
    carrying compression. A run where every candidate separated is not a contact result.
    """
    if not r.get("ok", False) or r.get("skipped"):
        return False
    contact = r.get("contact")
    if not isinstance(contact, dict) or not contact.get("converged"):
        return False
    active = contact.get("active_pairs")
    return isinstance(active, int) and active > 0


def _check_reveng_deviation_default(r: dict) -> bool:
    """Pass when the mesh-vs-B-Rep deviation is within tolerance (RE-16/RE-17).

    Result shape is the contracts/reveng.md §5 deviation schema. The tolerance is
    carried on the result as ``tol_mm`` when the tool computes one; absent that we
    fall back to a conservative default so a real deviation still gates the run
    rather than auto-passing. A non-ok result (no mesh, backend absent, no
    reference) is NOT a pass — the verify node escalates and the human decides.
    """
    if not r.get("ok", False):
        return False
    max_mm = r.get("max_mm")
    if max_mm is None:
        return False
    tol_mm = r.get("tol_mm", 0.5)  # §5 default heat-bucket boundary
    try:
        return float(max_mm) <= float(tol_mm)
    except (TypeError, ValueError):
        return False


# DISPATCH_TABLE is the reusable public symbol.
# check_type 'requirement' is handled separately (see _run_requirement_verify) — not in this table.
DISPATCH_TABLE: dict[str, tuple[str, str, Callable[[dict], bool]]] = {
    "interference": ("cad",        "check_interference",   _check_interference_default),
    "erc":          ("ecad",       "ecad_run_erc",         _check_erc_default),
    "drc":          ("ecad",       "ecad_run_drc",         _check_drc_default),
    "firmware":     ("firmware",   "fw_sim_run",           _check_firmware_default),
    "dc_ratio":     ("structural", "struct_check",         _check_dc_ratio_default),
    "budget":       ("sourcing",   "bom_price",            _check_budget_default),
    # Advanced FEA verify nodes (module_key "cad", tools in cad_tools.py).
    "fatigue":      ("cad",        "run_fea_fatigue",      _check_fatigue_default),
    "thermal":      ("cad",        "run_fea_thermal",      _check_thermal_default),
    "buckling":     ("cad",        "run_fea_buckling",     _check_buckling_default),
    "contact":      ("cad",        "run_fea_contact",      _check_contact_default),
    "vibration":    ("cad",        "run_fea_frequency_response", _check_vibration_default),
    # RE-17 — reverse-engineering mesh-vs-B-Rep deviation verify node
    # (get_reveng_deviation lives in the reveng MCP module, not recon).
    "reveng_deviation": ("reveng",  "get_reveng_deviation", _check_reveng_deviation_default),
}


# ---------------------------------------------------------------------------
# Private MCP singleton extension — firmware / structural / sourcing
#
# battery._get_fn covers cad/recon/ecad/cam.  We extend it for the three
# additional module families used by verify nodes, sharing the SAME _PRIVATE_MCP
# cache dict so both battery.py and verify.py see one registry.
# ---------------------------------------------------------------------------

def _get_fn(module_key: str, fn_name: str):
    """Resolve a tool function by module_key + fn_name.

    Delegates to battery._get_fn for the families it already handles (cad/recon/ecad/cam).
    Handles firmware/structural/sourcing locally using the same _PRIVATE_MCP cache.
    """
    # Battery handles these four natively.
    if module_key in ("cad", "recon", "ecad", "cam"):
        return _battery_get_fn(module_key, fn_name)

    # Extend the battery's cache for additional families.
    from services.verify.battery import _PRIVATE_MCP
    if module_key not in _PRIVATE_MCP:
        from mcp.server.fastmcp import FastMCP
        _mcp = FastMCP(f"battery-{module_key}")
        if module_key == "firmware":
            try:
                from services.mcp.firmware_tools import register_firmware_tools
                register_firmware_tools(_mcp)
            except ImportError:
                pass  # [~] optional module
        elif module_key == "structural":
            try:
                from services.mcp.structural_tools import register_structural_tools
                register_structural_tools(_mcp)
            except ImportError:
                pass  # [~] optional module
        elif module_key == "sourcing":
            try:
                from services.mcp.sourcing_tools import register_sourcing_tools
                register_sourcing_tools(_mcp)
            except ImportError:
                pass  # [~] optional module
        elif module_key == "reveng":
            # RE-17 — get_reveng_deviation for the reveng_deviation verify node.
            try:
                from services.mcp.reveng_tools import register_reveng_tools
                register_reveng_tools(_mcp)
            except ImportError:
                pass  # [~] RE wave not merged / backend absent
        _PRIVATE_MCP[module_key] = _mcp

    mcp = _PRIVATE_MCP[module_key]
    tool = mcp._tool_manager._tools.get(fn_name)
    if tool is None:
        raise KeyError(f"Tool {fn_name!r} not found in module_key={module_key!r}")
    return tool.fn


# ---------------------------------------------------------------------------
# Predicate evaluation
# ---------------------------------------------------------------------------

def _eval_predicate(
    predicate: str,
    result: dict,
    extra_ns: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Evaluate a string predicate against a result dict.

    Returns (passed: bool, error: str | None).
    Uses a restricted namespace — only ``result`` and ``extra_ns`` keys are visible.

    The predicate is stored as a Python expression, e.g.:
      "not result.get('interferences')"
      "result.get('errors', 1) == 0"
      "result.get('unit_cost', 9e9) <= target_budget"

    Error in evaluation → returns (False, error_string) so the caller can
    escalate with reason="predicate_error" (never auto-pass).
    """
    from .predicate import evaluate

    ns: dict[str, Any] = dict(extra_ns or {})
    ns["result"] = result
    try:
        return evaluate(predicate, ns), None
    except Exception as exc:
        return False, f"predicate evaluation error: {exc}"


# ---------------------------------------------------------------------------
# Build tool kwargs from node inputs
# ---------------------------------------------------------------------------

def _build_tool_kwargs(check_type: str, node_inputs: dict[str, Any]) -> dict[str, Any]:
    """Extract the correct keyword arguments for each tool from node inputs."""
    if check_type == "interference":
        return {
            k: node_inputs[k]
            for k in ("doc_id", "include_coincident", "min_clearance_mm")
            if k in node_inputs
        }
    if check_type in ("erc", "drc"):
        return {k: node_inputs[k] for k in ("doc_id",) if k in node_inputs}
    if check_type == "firmware":
        kwargs: dict[str, Any] = {}
        for k in ("project_id", "elf_path", "scenario", "assertions"):
            if k in node_inputs:
                kwargs[k] = node_inputs[k]
        return kwargs
    if check_type == "dc_ratio":
        kwargs = {}
        if "doc_path" in node_inputs:
            kwargs["doc_path"] = node_inputs["doc_path"]
        if "load_combo" in node_inputs:
            kwargs["load_combo"] = node_inputs["load_combo"]
        return kwargs
    if check_type == "budget":
        kwargs = {}
        if "doc_path" in node_inputs:
            kwargs["doc_path"] = node_inputs["doc_path"]
        return kwargs
    # Advanced FEA verify nodes — pass through the tool's own kwargs.
    if check_type == "fatigue":
        return {k: node_inputs[k] for k in (
            "fixtures", "loads", "material", "load_type", "load_ratio", "surface",
            "reliability_pct", "temperature_c", "theory", "mesh_fineness", "doc_id")
            if k in node_inputs}
    if check_type == "thermal":
        return {k: node_inputs[k] for k in (
            "hot_faces", "cold_faces", "material", "conductivity_w_mk", "doc_id")
            if k in node_inputs}
    if check_type == "buckling":
        return {k: node_inputs[k] for k in (
            "fixtures", "load", "material", "n_modes", "size_mm", "vtu_path", "doc_id")
            if k in node_inputs}
    if check_type == "contact":
        return {k: node_inputs[k] for k in (
            "pressure_mpa", "material", "doc_id") if k in node_inputs}
    if check_type == "reveng_deviation":
        return {k: node_inputs[k] for k in (
            "session_id", "ref_path", "brep_doc_id", "n_samples") if k in node_inputs}
    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    node: dict[str, Any],
    context_pack: dict[str, Any],
    *,
    emit: EmitFn = None,
    tool_fn_override: Callable | None = None,
) -> tuple[str, dict[str, Any]]:
    """Execute a 'verify' DAG node deterministically (no LLM call).

    Calls the real MCP tool mapped to node["inputs"]["check_type"], evaluates
    the end_condition predicate, and returns (verdict, output_state) in the same
    shape as node_runner.run() so graph_loader routes identically.

    Args:
        node:             aicad.dag/1 node dict (type MUST be "verify").
        context_pack:     Assembled by context_pack.assemble().
        emit:             Optional §11 event callback.
        tool_fn_override: Inject a callable in place of the real tool (for tests).

    Returns:
        (verdict, output_state) where:
          verdict       ∈ {done, retry, escalate}
          output_state  dict with status, summary, data, retry_remaining, check_type,
                        result (parsed dict), passed, evidence.
    """
    node_id: str = node["id"]
    node_name: str = node["name"]
    node_inputs: dict[str, Any] = context_pack.get("node_inputs") or {}
    retry_remaining: int = context_pack.get("retry_remaining", 1)
    end_condition: str | None = context_pack.get("end_condition") or node.get("end_condition")

    check_type: str | None = node_inputs.get("check_type")

    _emit_ev(emit, "dag.node.started", {
        "node_id": node_id,
        "node_type": "verify",
        "node_name": node_name,
        "check_type": check_type,
    })

    t0 = time.monotonic()

    # ---- Requirement auto-verification ----------------------------------------
    if check_type == "requirement":
        return _run_requirement_verify(
            node_id=node_id,
            node_name=node_name,
            node_inputs=node_inputs,
            retry_remaining=retry_remaining,
            emit=emit,
            tool_fn_override=tool_fn_override,
            t0=t0,
        )

    # -- Resolve check type ---------------------------------------------------
    if not check_type or check_type not in DISPATCH_TABLE:
        known = ", ".join(DISPATCH_TABLE)
        verdict = VERDICT_ESCALATE
        output_state = _make_output(
            verdict, node_inputs, retry_remaining, check_type or "?",
            result=None, passed=False,
            reason="unknown_check_type",
            summary=(
                f"Verify node {node_name!r}: unknown check_type={check_type!r}. "
                f"Known: {known}"
            ),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
        _emit_ev(emit, "dag.node.failed", {"node_id": node_id,
                                            "error": output_state["summary"]})
        return verdict, output_state

    module_key, tool_name, default_pred = DISPATCH_TABLE[check_type]

    # -- Resolve tool function ------------------------------------------------
    if tool_fn_override is not None:
        tool_fn = tool_fn_override
    else:
        try:
            tool_fn = _get_fn(module_key, tool_name)
        except (KeyError, ImportError, Exception) as exc:
            verdict = VERDICT_ESCALATE
            output_state = _make_output(
                verdict, node_inputs, retry_remaining, check_type,
                result=None, passed=False,
                reason="backend_unavailable",
                summary=(
                    f"Verify node {node_name!r}: backend unavailable for "
                    f"check_type={check_type!r} (tool={tool_name!r}): {exc}"
                ),
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )
            _emit_ev(emit, "dag.node.failed",
                     {"node_id": node_id, "error": output_state["summary"]})
            return verdict, output_state

    # -- Call tool ------------------------------------------------------------
    tool_kwargs = _build_tool_kwargs(check_type, node_inputs)
    try:
        raw_result = tool_fn(**tool_kwargs)
    except Exception as exc:
        verdict = VERDICT_ESCALATE
        output_state = _make_output(
            verdict, node_inputs, retry_remaining, check_type,
            result=None, passed=False,
            reason="tool_exception",
            summary=f"Verify node {node_name!r}: tool {tool_name!r} raised: {exc}",
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
        _emit_ev(emit, "dag.node.failed",
                 {"node_id": node_id, "error": output_state["summary"]})
        return verdict, output_state

    # -- Parse result ---------------------------------------------------------
    if isinstance(raw_result, str):
        try:
            result_dict = json.loads(raw_result)
        except json.JSONDecodeError as exc:
            verdict = VERDICT_ESCALATE
            output_state = _make_output(
                verdict, node_inputs, retry_remaining, check_type,
                result=None, passed=False,
                reason="tool_error",
                summary=f"Verify node {node_name!r}: tool returned non-JSON: {exc}",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )
            _emit_ev(emit, "dag.node.failed",
                     {"node_id": node_id, "error": output_state["summary"]})
            return verdict, output_state
    elif isinstance(raw_result, dict):
        result_dict = raw_result
    else:
        verdict = VERDICT_ESCALATE
        output_state = _make_output(
            verdict, node_inputs, retry_remaining, check_type,
            result=None, passed=False,
            reason="tool_error",
            summary=(f"Verify node {node_name!r}: unexpected tool return type "
                     f"{type(raw_result).__name__}"),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
        _emit_ev(emit, "dag.node.failed",
                 {"node_id": node_id, "error": output_state["summary"]})
        return verdict, output_state

    # -- Check tool-level ok --------------------------------------------------
    if not result_dict.get("ok", True):
        # Tool reported its own error (e.g. solver_unavailable, doc not found).
        reason = result_dict.get("reason") or result_dict.get("error") or "tool_error"
        verdict = VERDICT_ESCALATE
        output_state = _make_output(
            verdict, node_inputs, retry_remaining, check_type,
            result=result_dict, passed=False,
            reason="tool_error",
            summary=(
                f"Verify node {node_name!r}: tool {tool_name!r} returned ok=false: {reason}"
            ),
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
        _emit_ev(emit, "dag.node.failed",
                 {"node_id": node_id, "error": output_state["summary"]})
        return verdict, output_state

    # -- Evaluate end_condition predicate -------------------------------------
    extra_ns: dict[str, Any] = {}
    if "target_budget" in node_inputs:
        extra_ns["target_budget"] = node_inputs["target_budget"]

    if end_condition:
        passed, pred_err = _eval_predicate(end_condition, result_dict, extra_ns)
        if pred_err:
            verdict = VERDICT_ESCALATE
            output_state = _make_output(
                verdict, node_inputs, retry_remaining, check_type,
                result=result_dict, passed=False,
                reason="predicate_error",
                summary=f"Verify node {node_name!r}: {pred_err}",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )
            _emit_ev(emit, "dag.node.failed",
                     {"node_id": node_id, "error": output_state["summary"]})
            return verdict, output_state
    else:
        passed = default_pred(result_dict)

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    # -- Route by verdict -----------------------------------------------------
    if passed:
        verdict = VERDICT_DONE
        summary = (f"Verify node {node_name!r}: {check_type} PASSED "
                   f"(tool={tool_name}) in {elapsed_ms / 1000:.1f}s.")
        output_state = _make_output(
            verdict, node_inputs, retry_remaining, check_type,
            result=result_dict, passed=True,
            reason=None,
            summary=summary,
            elapsed_ms=elapsed_ms,
        )
        _emit_ev(emit, "dag.node.done", {"node_id": node_id, "output": output_state})
    else:
        # Design failed the check — retry if budget allows, escalate if exhausted.
        new_budget = max(0, retry_remaining - 1)
        if new_budget == 0:
            verdict = VERDICT_ESCALATE
        else:
            verdict = VERDICT_RETRY

        summary = (
            f"Verify node {node_name!r}: {check_type} FAILED "
            f"(tool={tool_name}) — "
            + ("escalating (budget exhausted)" if verdict == VERDICT_ESCALATE
               else f"retrying (budget remaining: {new_budget})")
            + f" in {elapsed_ms / 1000:.1f}s."
        )
        output_state = _make_output(
            verdict, node_inputs, new_budget, check_type,
            result=result_dict, passed=False,
            reason="check_failed",
            summary=summary,
            elapsed_ms=elapsed_ms,
        )
        if verdict == VERDICT_ESCALATE:
            _emit_ev(emit, "dag.node.failed",
                     {"node_id": node_id, "error": summary})
        else:
            _emit_ev(emit, "dag.node.paused",
                     {"node_id": node_id, "reason": f"retry (budget remaining: {new_budget})"})

    return verdict, output_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_output(
    verdict: str,
    node_inputs: dict,
    retry_remaining: int,
    check_type: str,
    *,
    result: dict | None,
    passed: bool,
    reason: str | None,
    summary: str,
    elapsed_ms: float,
) -> dict[str, Any]:
    """Build an output_state dict in the same shape as node_runner.py."""
    status = "done" if verdict == VERDICT_DONE else ("running" if verdict == VERDICT_RETRY else "failed")
    data: dict[str, Any] = {
        "check_type": check_type,
        "tool_result": result,
        "passed": passed,
        "elapsed_ms": round(elapsed_ms, 1),
    }
    if reason:
        data["reason"] = reason
    return {
        "status": status,
        "summary": summary,
        "data": data,
        "retry_remaining": retry_remaining,
        # design_review reads "evidence" to fill the design_review verdict card.
        "evidence": result,
        "doc_id": node_inputs.get("doc_id"),
    }


def _run_requirement_verify(
    node_id: str,
    node_name: str,
    node_inputs: dict[str, Any],
    retry_remaining: int,
    emit: EmitFn,
    tool_fn_override: Callable | None,
    t0: float,
) -> tuple[str, dict[str, Any]]:
    """Auto-verify a single requirement by running its named verify_by verifiers.

    check_type == "requirement" branch of run().
    node_inputs must include:
      req_id      — requirement ID to verify (e.g. "R1")
      req_path    — (optional) path to .req.json file
      req_doc_id  — (optional) doc-meta doc_id holding the req doc
      doc_id      — (optional) geometry doc the verifiers run against

    Fail-closed: any exception / missing req → ESCALATE.
    At least one verify_by verifier must pass (score >= 0.5) to flip to verified.
    VALID_VERIFIERS not in battery.CHECKS are skipped (v1 limitation, not a failure).
    """
    from services.verify.battery import CHECKS as _BATTERY_CHECKS
    from services.verify.battery import _extract_score as _battery_extract_score

    req_id: str | None = node_inputs.get("req_id")
    req_path: str | None = node_inputs.get("req_path")
    req_doc_id: str | None = node_inputs.get("req_doc_id")
    geo_doc_id: str | None = node_inputs.get("doc_id")

    def _escalate(reason: str, summary: str) -> tuple[str, dict[str, Any]]:
        out = _make_output(
            VERDICT_ESCALATE, node_inputs, retry_remaining, "requirement",
            result=None, passed=False, reason=reason, summary=summary,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )
        _emit_ev(emit, "dag.node.failed", {"node_id": node_id, "error": summary})
        return VERDICT_ESCALATE, out

    if not req_id:
        return _escalate("missing_req_id",
                         f"Verify node {node_name!r}: check_type='requirement' requires req_id in node_inputs.")

    # Load requirements doc
    try:
        from services.mcp.req_tools import _load_doc as _req_load_doc
        req_doc = _req_load_doc(doc_id=req_doc_id, req_path=req_path)
    except Exception as exc:
        return _escalate("req_load_error",
                         f"Verify node {node_name!r}: failed to load req doc: {exc}")

    if req_doc is None:
        return _escalate("req_doc_not_found",
                         f"Verify node {node_name!r}: req doc not found "
                         f"(req_path={req_path!r}, req_doc_id={req_doc_id!r}).")

    # Find requirement
    req_obj = None
    for r in req_doc.requirements:
        if r.id == req_id:
            req_obj = r
            break
    if req_obj is None:
        return _escalate("req_id_not_found",
                         f"Verify node {node_name!r}: requirement {req_id!r} not found in doc.")

    # Collect supported verifiers (those in battery.CHECKS)
    _UNSUPPORTED_IN_BATTERY = {"confirm_dimensions", "dfm_check", "fit_report"}
    supported: list[dict] = []
    unsupported: list[str] = []
    for vr in req_obj.verify_by:
        name = vr.verifier
        if name in _UNSUPPORTED_IN_BATTERY or name not in _BATTERY_CHECKS:
            unsupported.append(name)
        else:
            supported.append({"verifier": name, "args": vr.args})

    if not supported:
        return _escalate("no_supported_verifiers",
                         f"Verify node {node_name!r}: requirement {req_id!r} has no verifiers "
                         f"supported by the battery (v1 limitation). "
                         f"Unsupported: {unsupported}")

    # Run verifiers — at least one must pass
    verifier_results: list[dict[str, Any]] = []
    any_passed = False
    elapsed_ms = 0.0

    for vr_entry in supported:
        verifier_name = vr_entry["verifier"]
        module_key, fn_name, cost, platform_req, reward_shape = _BATTERY_CHECKS[verifier_name]

        # Platform check
        import sys as _sys
        is_posix = _sys.platform != "win32"
        if platform_req == "posix" and not is_posix:
            verifier_results.append({
                "verifier": verifier_name, "skipped": True, "reason": "platform_mismatch",
            })
            continue

        # Resolve tool function
        if tool_fn_override is not None:
            tool_fn = tool_fn_override
        else:
            try:
                tool_fn = _get_fn(module_key, fn_name)
            except Exception as exc:
                verifier_results.append({
                    "verifier": verifier_name, "error": str(exc), "passed": False,
                })
                continue

        # Build kwargs
        call_kwargs: dict[str, Any] = {}
        if geo_doc_id is not None:
            call_kwargs["doc_id"] = geo_doc_id

        # Call
        try:
            raw_result = tool_fn(**call_kwargs)
        except Exception as exc:
            verifier_results.append({
                "verifier": verifier_name, "error": str(exc), "passed": False,
            })
            continue

        # Extract score
        score, findings = _battery_extract_score(verifier_name, reward_shape, raw_result)
        passed_this = score >= 0.5

        if isinstance(raw_result, str):
            try:
                import json as _json
                result_dict = _json.loads(raw_result)
            except Exception:
                result_dict = {"raw": raw_result}
        else:
            result_dict = raw_result if isinstance(raw_result, dict) else {"raw": str(raw_result)}

        verifier_results.append({
            "verifier": verifier_name,
            "passed": passed_this,
            "score": score,
            "result": result_dict,
            "findings": findings,
            "source": "battery",
        })
        if passed_this:
            any_passed = True

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    # Build evidence list for writeback
    import datetime as _dt
    _now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence_list = [
        {
            "verifier": r["verifier"],
            "result": r.get("result", {}),
            # The battery verdict. A tool's own `ok` can mean "the check ran", not "passed".
            "passed": r["passed"],
            "ts": _now,
            "source": "battery",
        }
        for r in verifier_results
        if not r.get("skipped") and "error" not in r
    ]

    # Writeback to requirements doc — FAIL-CLOSED: a pass verdict whose status
    # flip did not land would fabricate "verified" (this file's mandatory policy).
    if evidence_list:
        try:
            _record_req_result(
                req_id=req_id,
                passed=any_passed,
                evidence=evidence_list,
                req_path=req_path,
                req_doc_id=req_doc_id,
            )
        except Exception as exc:
            return _escalate(
                "req_writeback_error",
                f"Verify node {node_name!r}: verifier(s) ran for {req_id!r} "
                f"(passed={any_passed}) but the requirements-doc writeback failed: {exc}",
            )

    result_summary = {
        "req_id": req_id,
        "verifiers_run": verifier_results,
        "unsupported": unsupported,
        "any_passed": any_passed,
        "evidence_count": len(evidence_list),
    }

    if any_passed:
        verdict = VERDICT_DONE
        summary = (
            f"Verify node {node_name!r}: requirement {req_id!r} VERIFIED "
            f"by battery ({len(verifier_results)} verifier(s) run) "
            f"in {elapsed_ms / 1000:.1f}s."
        )
        out = _make_output(
            verdict, node_inputs, retry_remaining, "requirement",
            result=result_summary, passed=True,
            reason=None, summary=summary, elapsed_ms=elapsed_ms,
        )
        _emit_ev(emit, "dag.node.done", {"node_id": node_id, "output": out})
    else:
        new_budget = max(0, retry_remaining - 1)
        verdict = VERDICT_ESCALATE if new_budget == 0 else VERDICT_RETRY
        summary = (
            f"Verify node {node_name!r}: requirement {req_id!r} FAILED "
            f"({len(verifier_results)} verifier(s) run, none passed) "
            + ("escalating (budget exhausted)" if verdict == VERDICT_ESCALATE
               else f"retrying (budget remaining: {new_budget})")
            + f" in {elapsed_ms / 1000:.1f}s."
        )
        out = _make_output(
            verdict, node_inputs, new_budget, "requirement",
            result=result_summary, passed=False,
            reason="check_failed", summary=summary, elapsed_ms=elapsed_ms,
        )
        if verdict == VERDICT_ESCALATE:
            _emit_ev(emit, "dag.node.failed", {"node_id": node_id, "error": summary})
        else:
            _emit_ev(emit, "dag.node.paused",
                     {"node_id": node_id, "reason": f"retry (budget remaining: {new_budget})"})

    return verdict, out


def _record_req_result(
    req_id: str,
    passed: bool,
    evidence: list[dict[str, Any]],
    req_path: str | None,
    req_doc_id: str | None,
) -> None:
    """Write verification result back into the requirements doc via req_tools helpers.

    Uses _load_doc / set_status / _save_doc directly (internal helpers, not MCP tools)
    so no FastMCP registration is needed from here.
    """
    from services.mcp.req_tools import _load_doc, _save_doc
    doc = _load_doc(doc_id=req_doc_id, req_path=req_path)
    if doc is None:
        raise ValueError(f"req doc not found for writeback (req_path={req_path!r}, req_doc_id={req_doc_id!r})")
    new_status = "verified" if passed else "failed"
    doc.set_status(req_id, new_status, who="agent", evidence=evidence)
    _save_doc(doc, doc_id=req_doc_id, req_path=req_path)


def _emit_ev(emit: EmitFn, event_type: str, payload: dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        emit(event_type, payload)
    except Exception:  # noqa: BLE001
        pass
