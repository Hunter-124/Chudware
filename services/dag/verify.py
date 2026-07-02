"""verify.py — Verify node runner for the aicad.dag/1 orchestrator.

Contract: contracts/dag_schema.md §3 (node type "verify"), §8 (NodeState).
Wired by: T-A4. Called from graph_loader.py instead of the T-A3 stub.
Reused by: T-F12's design_review() battery (DISPATCH_TABLE is the public API).

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

[~] = backend may be absent on non-merged base branches; calls degrade to ESCALATE
      with reason "backend_unavailable" (never a silent pass).
"""
from __future__ import annotations

import json
import time
import warnings
from typing import Any, Callable, Optional

# Re-use the battery's private MCP singleton (one registry per module_key, built once).
# This keeps T-F12's design_review from building a second, divergent registry.
from services.verify.battery import _get_fn as _battery_get_fn

# Verdicts (same names as node_runner so graph_loader routes identically).
VERDICT_DONE = "done"
VERDICT_RETRY = "retry"
VERDICT_ESCALATE = "escalate"

EmitFn = Optional[Callable[[str, dict[str, Any]], None]]

# ---------------------------------------------------------------------------
# Dispatch table — PUBLIC API for T-F12 reuse
#
# Each entry:
#   check_type → (module_key, tool_fn_name, default_predicate_fn)
#
# default_predicate_fn(result_dict) → bool
#   Called when no end_condition is supplied.  Must be conservative (fail-closed):
#   if the result dict lacks the expected key, return False.
# ---------------------------------------------------------------------------

def _check_interference_default(r: dict) -> bool:
    """Pass when ok=True AND no interferences present.

    check_interference returns ok=True even when interferences exist (it reports
    the findings); ok=True only means the tool ran successfully.  The real pass
    condition is zero interference pairs.
    """
    if not r.get("ok", False):
        return False
    return len(r.get("interferences", [])) == 0

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


# DISPATCH_TABLE is the T-F12-reusable public symbol.
DISPATCH_TABLE: dict[str, tuple[str, str, Callable[[dict], bool]]] = {
    "interference": ("cad",        "check_interference",   _check_interference_default),
    "erc":          ("ecad",       "ecad_run_erc",         _check_erc_default),
    "drc":          ("ecad",       "ecad_run_drc",         _check_drc_default),
    "firmware":     ("firmware",   "fw_sim_run",           _check_firmware_default),
    "dc_ratio":     ("structural", "struct_check",         _check_dc_ratio_default),
    "budget":       ("sourcing",   "bom_price",            _check_budget_default),
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
                from mcp_server.firmware_tools import register_firmware_tools
                register_firmware_tools(_mcp)
            except ImportError:
                pass  # [~] WS-C may not be merged yet
        elif module_key == "structural":
            try:
                from mcp_server.structural_tools import register_structural_tools
                register_structural_tools(_mcp)
            except ImportError:
                pass  # [~] WS-D
        elif module_key == "sourcing":
            try:
                from mcp_server.sourcing_tools import register_sourcing_tools
                register_sourcing_tools(_mcp)
            except ImportError:
                pass  # [~] WS-B
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
    ns: dict[str, Any] = {"result": result}
    if extra_ns:
        ns.update(extra_ns)
    # Minimal safe builtins: comparison helpers only.
    ns["__builtins__"] = {"abs": abs, "int": int, "float": float, "bool": bool,
                          "len": len, "all": all, "any": any, "min": min, "max": max}
    try:
        return bool(eval(predicate, ns)), None  # noqa: S307 — restricted namespace
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
        # T-F12 reads "evidence" to fill the design_review verdict card.
        "evidence": result,
        "doc_id": node_inputs.get("doc_id"),
    }


def _emit_ev(emit: EmitFn, event_type: str, payload: dict[str, Any]) -> None:
    if emit is None:
        return
    try:
        emit(event_type, payload)
    except Exception:  # noqa: BLE001
        pass
