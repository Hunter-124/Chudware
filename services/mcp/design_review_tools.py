"""design_review_tools.py — MCP tool: design_review battery.

Registered by: services/mcp/server.py (append-only).
Narration contract: contracts/narration.md (never raw JSON dumps).

design_review() dispatches every check in DISPATCH_TABLE (from services/dag/verify.py)
plus check_feature_overlap() and dfm_check() via the same battery._get_fn singleton.
It returns a narrated verdict card — not a raw JSON dump — matching contracts/narration.md.

DISPATCH_TABLE covers:
  interference / erc / drc / firmware / dc_ratio / budget

Additional checks (not in DISPATCH_TABLE — called directly via battery._get_fn):
  check_feature_overlap  — single-part own-feature collisions (cad module)
  dfm_check              — DFM/manufacturability check (intake module)

These two are kept separate from DISPATCH_TABLE intentionally: adding them would
require editing verify.py, which is out of this module's scope.  They share the same
_get_fn registry, so there is still ONE live registry for all checks.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_design_review_tools(mcp: "FastMCP") -> None:
    """Register design_review as an MCP tool onto *mcp*."""

    @mcp.tool()
    def design_review(
        doc_id: Optional[str] = None,
        checks: Optional[list] = None,
        target_budget: Optional[float] = None,
        doc_path: Optional[str] = None,
        req_ids: Optional[list] = None,
        req_path: Optional[str] = None,
    ) -> str:
        """Run a comprehensive design review battery and return a narrated verdict card.

        Dispatches all standard checks (interference, ERC, DRC, firmware, DC-ratio,
        budget) via the DISPATCH_TABLE from services/dag/verify.py, plus
        check_feature_overlap and dfm_check directly via the battery registry.

        Returns a narrated text verdict — not a JSON dump.  Numbers are narrated
        verbatim from tool payloads (contracts/narration.md: narrate, never recompute).

        Args:
            doc_id:        Target document id (default: current active doc).
            checks:        Optional subset of check names to run (default: all).
                           Names: interference, erc, drc, firmware, dc_ratio, budget,
                                  feature_overlap, dfm.
            target_budget: Optional budget cap (float) for the 'budget' check.
                           When supplied, the budget check uses this as its pass threshold.
            doc_path:      Optional path to an aicad.structure/1 doc for dc_ratio/budget checks.
            req_ids:       Optional list of aicad.req/1 requirement IDs (e.g. ["R1"]).
                           Each is auto-verified by running its named verify_by verifiers
                           via the battery; the result (status flip + evidence ref) is
                           written back into the requirements doc automatically.
            req_path:      Path to the .req.json requirements doc for req_ids (omit when
                           the req doc is stored as doc-meta on doc_id).

        Returns:
            Narrated verdict card (str).  Sections:
              Design Review — <doc_id>
              ✓ / ✗ per check with key findings
              Overall: PASS / FAIL / PARTIAL
              Caveats / unavailable backends
              (Source: DISPATCH_TABLE + battery._get_fn — numbers narrated verbatim)
        """
        return _run_design_review(
            doc_id=doc_id,
            checks=checks,
            target_budget=target_budget,
            doc_path=doc_path,
            req_ids=req_ids,
            req_path=req_path,
        )


# ---------------------------------------------------------------------------
# Core implementation (importable by tests without going through MCP)
# ---------------------------------------------------------------------------

# All check names available in design_review
_ALL_CHECKS = [
    "interference",
    "interface_check",
    "erc",
    "drc",
    "feature_overlap",
    "dfm",
    "firmware",
    "dc_ratio",
    "budget",
    "layout_coverage",  # critical-dim & interface coverage gate
]

# Checks that use DISPATCH_TABLE directly
_DISPATCH_CHECKS = ["interference", "erc", "drc", "firmware", "dc_ratio", "budget"]

# Checks handled via direct battery._get_fn calls
_DIRECT_CHECKS = ["feature_overlap", "dfm", "interface_check"]

# Checks handled via battery GAP_FNS (gap verifiers implemented directly in battery.py)
_GAP_CHECKS = ["layout_coverage"]


def _run_design_review(
    doc_id: Optional[str] = None,
    checks: Optional[list] = None,
    target_budget: Optional[float] = None,
    doc_path: Optional[str] = None,
    req_ids: Optional[list] = None,
    req_path: Optional[str] = None,
    *,
    tool_fn_overrides: Optional[dict[str, Any]] = None,
    req_tool_fn_override: Optional[Any] = None,
) -> str:
    """Run the design review battery and return narrated verdict text.

    tool_fn_overrides: dict {check_name: callable} for test injection.
                       Overrides ONLY the specified checks; others use real tools.
    req_tool_fn_override: callable injected as the verifier tool for ALL req_ids
                       checks (test injection, mirrors verify.run's tool_fn_override).
    """
    active_checks = checks if checks is not None else _ALL_CHECKS
    overrides = tool_fn_overrides or {}

    results: dict[str, Any] = {}
    t_start = time.monotonic()

    for check_name in _ALL_CHECKS:
        if check_name not in active_checks:
            results[check_name] = {"status": "skipped", "reason": "not_requested"}
            continue

        if check_name in overrides:
            results[check_name] = _call_with_override(check_name, overrides[check_name],
                                                       doc_id, doc_path, target_budget)
        elif check_name in _DISPATCH_CHECKS:
            results[check_name] = _call_dispatch_check(check_name, doc_id, doc_path, target_budget)
        elif check_name in _GAP_CHECKS:
            results[check_name] = _call_gap_check(check_name, doc_id)
        else:
            results[check_name] = _call_direct_check(check_name, doc_id)

    # Requirement auto-verification — each req_id runs its verify_by
    # verifiers via the same battery registry and writes status + evidence back
    # into the requirements doc (services/dag/verify._run_requirement_verify).
    req_results: dict[str, Any] = {}
    if req_ids:
        for rid in req_ids:
            req_results[str(rid)] = _call_requirement_check(
                str(rid), doc_id, req_path, req_tool_fn_override,
            )

    elapsed_ms = (time.monotonic() - t_start) * 1000.0
    return _narrate(results, doc_id, elapsed_ms, req_results=req_results)


def _call_requirement_check(
    req_id: str,
    doc_id: Optional[str],
    req_path: Optional[str],
    tool_fn_override: Optional[Any] = None,
) -> dict[str, Any]:
    """Auto-verify one aicad.req/1 requirement via the DAG requirement path."""
    try:
        import time as _time
        from services.dag.verify import _run_requirement_verify, VERDICT_DONE
    except ImportError as exc:
        return {"status": "unavailable", "reason": f"verify module unavailable: {exc}"}

    node_inputs: dict[str, Any] = {
        "check_type": "requirement",
        "req_id": req_id,
        "req_path": req_path,
        # Only use doc_id as the req-doc holder when no req_path was given
        # (req_tools._load_doc prioritizes doc_id over req_path).
        "req_doc_id": None if req_path else doc_id,
        "doc_id": doc_id,
    }
    try:
        verdict, output_state = _run_requirement_verify(
            node_id=f"design-review-req-{req_id}",
            node_name=f"design_review({req_id})",
            node_inputs=node_inputs,
            retry_remaining=1,
            emit=None,
            tool_fn_override=tool_fn_override,
            t0=_time.monotonic(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"requirement check raised: {exc}", "passed": False}

    data = output_state.get("data", {})
    if verdict == VERDICT_DONE:
        return {"status": "done", "passed": True, "result": data.get("tool_result")}
    reason = data.get("reason", "check_failed")
    if reason == "check_failed":
        return {"status": "done", "passed": False, "result": data.get("tool_result")}
    return {"status": "error", "reason": output_state.get("summary", reason),
            "passed": False, "result": data.get("tool_result")}


def _call_gap_check(check_name: str, doc_id: Optional[str]) -> dict[str, Any]:
    """Call a GAP_FNS verifier directly from battery (layout_coverage and similar).

    GAP verifiers live in battery.GAP_FNS — they are pure Python functions that call
    core_bridge directly, not tool-manager functions.  We look them up via the CHECKS
    registry to find the fn_name key, then call GAP_FNS[fn_name] directly.
    """
    try:
        from services.verify.battery import CHECKS, GAP_FNS
    except ImportError as exc:
        return {"status": "unavailable", "reason": f"battery module unavailable: {exc}"}

    if check_name not in CHECKS:
        return {"status": "unavailable", "reason": f"unknown gap check: {check_name!r}"}

    _, fn_name, _, _, reward_shape = CHECKS[check_name]
    fn = GAP_FNS.get(fn_name)
    if fn is None:
        return {"status": "unavailable", "reason": f"GAP_FNS missing fn {fn_name!r}"}

    def default_pred(r: dict) -> bool:
        return bool(r.get("ok", False))

    return _invoke_tool(check_name, lambda **kw: fn(doc_id=doc_id, **kw), {}, default_pred)


def _call_dispatch_check(
    check_name: str,
    doc_id: Optional[str],
    doc_path: Optional[str],
    target_budget: Optional[float],
) -> dict[str, Any]:
    """Dispatch a check via DISPATCH_TABLE + battery._get_fn."""
    try:
        from services.dag.verify import DISPATCH_TABLE, _get_fn, _build_tool_kwargs
    except ImportError as exc:
        return {"status": "unavailable", "reason": f"verify module unavailable: {exc}"}

    if check_name not in DISPATCH_TABLE:
        return {"status": "unavailable", "reason": f"unknown check_type: {check_name!r}"}

    module_key, tool_fn_name, default_pred = DISPATCH_TABLE[check_name]

    try:
        tool_fn = _get_fn(module_key, tool_fn_name)
    except Exception as exc:
        return {"status": "unavailable", "reason": f"backend unavailable: {exc}"}

    # Build kwargs from a minimal node inputs dict
    node_inputs: dict[str, Any] = {}
    if doc_id:
        node_inputs["doc_id"] = doc_id
    if doc_path:
        node_inputs["doc_path"] = doc_path
    if target_budget is not None:
        node_inputs["target_budget"] = target_budget

    tool_kwargs = _build_tool_kwargs(check_name, node_inputs)

    return _invoke_tool(check_name, tool_fn, tool_kwargs, default_pred)


def _call_direct_check(check_name: str, doc_id: Optional[str]) -> dict[str, Any]:
    """Call check_feature_overlap or dfm_check via battery._get_fn (not in DISPATCH_TABLE)."""
    try:
        from services.verify.battery import _get_fn as battery_get_fn
    except ImportError as exc:
        return {"status": "unavailable", "reason": f"battery module unavailable: {exc}"}

    if check_name == "feature_overlap":
        module_key = "cad"
        tool_fn_name = "check_feature_overlap"
        kwargs: dict[str, Any] = {}
        if doc_id:
            kwargs["doc_id"] = doc_id

        def default_pred(r: dict) -> bool:
            return bool(r.get("ok", False)) and len(r.get("overlaps", [])) == 0

    elif check_name == "dfm":
        module_key = "intake"
        tool_fn_name = "dfm_check"
        kwargs = {}
        if doc_id:
            kwargs["doc_id"] = doc_id

        def default_pred(r: dict) -> bool:  # type: ignore[misc]
            return bool(r.get("ok", False)) and int(r.get("violations", 1)) == 0

    elif check_name == "interface_check":
        # interface_check lives in its own module (not a private-MCP module), so call
        # its implementation directly rather than via battery._get_fn.
        try:
            from services.mcp.interface_check_tools import run_interface_check as tool_fn
        except ImportError as exc:
            return {"status": "unavailable", "reason": f"interface_check unavailable: {exc}"}
        kwargs = {}
        if doc_id:
            kwargs["doc_id"] = doc_id
        return _invoke_tool(check_name, tool_fn, kwargs, _interface_check_passed)

    else:
        return {"status": "unavailable", "reason": f"unknown direct check: {check_name!r}"}

    try:
        tool_fn = battery_get_fn(module_key, tool_fn_name)
    except Exception as exc:
        return {"status": "unavailable", "reason": f"backend unavailable: {exc}"}

    return _invoke_tool(check_name, tool_fn, kwargs, default_pred)


def _interface_check_passed(r: dict) -> bool:
    """interface_check: ok means the check ran; status "fail" is an alignment finding.

    status "skip" (no mating pair to compare) does not fail the review.
    """
    return r.get("ok") is True and r.get("status") in ("pass", "skip")


def _call_with_override(
    check_name: str,
    override_fn: Any,
    doc_id: Optional[str],
    doc_path: Optional[str],
    target_budget: Optional[float],
) -> dict[str, Any]:
    """Call a test-injected override function for a check.

    Uses the real default_pred from DISPATCH_TABLE (or the direct-check predicate)
    so failure conditions are evaluated correctly even with mock tool functions.
    """
    kwargs: dict[str, Any] = {}
    if doc_id:
        kwargs["doc_id"] = doc_id
    if doc_path:
        kwargs["doc_path"] = doc_path
    if target_budget is not None:
        kwargs["target_budget"] = target_budget

    # Resolve the correct predicate for this check_name
    if check_name in _DISPATCH_CHECKS:
        try:
            from services.dag.verify import DISPATCH_TABLE
            _, _, default_pred = DISPATCH_TABLE[check_name]
        except Exception:
            default_pred = lambda r: bool(r.get("ok", True))  # noqa: E731
    elif check_name == "feature_overlap":
        def default_pred(r: dict) -> bool:
            return bool(r.get("ok", False)) and len(r.get("overlaps", [])) == 0
    elif check_name == "dfm":
        def default_pred(r: dict) -> bool:  # type: ignore[misc]
            return bool(r.get("ok", False)) and int(r.get("violations", 1)) == 0
    elif check_name == "interface_check":
        default_pred = _interface_check_passed
    else:
        default_pred = lambda r: bool(r.get("ok", True))  # noqa: E731

    return _invoke_tool(check_name, override_fn, kwargs, default_pred)


def _invoke_tool(
    check_name: str,
    tool_fn: Any,
    kwargs: dict[str, Any],
    default_pred: Any,
) -> dict[str, Any]:
    """Call tool_fn(**kwargs), parse result, evaluate default_pred."""
    try:
        raw = tool_fn(**kwargs)
    except Exception as exc:
        return {"status": "error", "reason": f"tool raised: {exc}", "passed": False}

    if isinstance(raw, str):
        try:
            result = json.loads(raw)
        except Exception as exc:
            return {"status": "error", "reason": f"non-JSON response: {exc}", "passed": False}
    elif isinstance(raw, dict):
        result = raw
    else:
        return {"status": "error", "reason": f"unexpected return type: {type(raw).__name__}", "passed": False}

    if not result.get("ok", True):
        tool_err = result.get("reason") or result.get("error") or "tool_error"
        return {"status": "error", "reason": tool_err, "passed": False, "result": result}

    try:
        passed = bool(default_pred(result))
    except Exception as exc:
        return {"status": "error", "reason": f"predicate failed: {exc}", "passed": False, "result": result}

    return {"status": "done", "passed": passed, "result": result}


# ---------------------------------------------------------------------------
# Narration (contracts/narration.md — narrate verbatim, never recompute)
# ---------------------------------------------------------------------------

# Human-friendly labels for each check
_CHECK_LABELS: dict[str, str] = {
    "interference":    "Interference (clash check)",
    "interface_check": "Interface alignment (holes/coaxiality/contact/fastener)",
    "erc":             "ERC (electrical rule check)",
    "drc":             "DRC (design rule check)",
    "feature_overlap": "Feature overlap (own-part collision)",
    "dfm":             "DFM (design for manufacturing)",
    "firmware":        "Firmware simulation assertions",
    "dc_ratio":        "Structural DC-ratio",
    "budget":          "BOM budget check",
    "layout_coverage": "Layout coverage (critical dims & interfaces)",
}


def _narrate(
    results: dict[str, Any],
    doc_id: Optional[str],
    elapsed_ms: float,
    req_results: Optional[dict[str, Any]] = None,
) -> str:
    """Build a narrated verdict card from battery results.

    Format follows contracts/narration.md:
      header → per-check (pass/fail/skip/unavail) → requirements → overall →
      caveats → source.
    """
    lines: list[str] = []

    # Header
    doc_label = doc_id or "current document"
    lines.append(f"Design Review — {doc_label}")
    lines.append("=" * (len(lines[0])))
    lines.append("")

    pass_count = 0
    fail_count = 0
    skip_count = 0
    unavail_count = 0
    caveats: list[str] = []

    for check_name in _ALL_CHECKS:
        label = _CHECK_LABELS.get(check_name, check_name)
        r = results.get(check_name, {"status": "skipped", "reason": "not_requested"})
        status = r.get("status", "skipped")

        if status == "skipped":
            lines.append(f"  — {label}: skipped (not requested)")
            skip_count += 1

        elif status == "unavailable":
            reason = r.get("reason", "backend unavailable")
            lines.append(f"  ? {label}: backend unavailable — {reason}")
            caveats.append(f"{label}: backend unavailable — {reason}")
            unavail_count += 1

        elif status == "error":
            reason = r.get("reason", "tool error")
            lines.append(f"  ✗ {label}: ERROR — {reason}")
            fail_count += 1
            caveats.append(f"{label} raised an error: {reason}")

        elif status == "done":
            passed = r.get("passed", False)
            result = r.get("result") or {}

            if passed:
                detail = _check_detail_pass(check_name, result)
                lines.append(f"  ✓ {label}: PASS{(' — ' + detail) if detail else ''}")
                pass_count += 1
            else:
                detail = _check_detail_fail(check_name, result)
                lines.append(f"  ✗ {label}: FAIL{(' — ' + detail) if detail else ''}")
                fail_count += 1

        else:
            lines.append(f"  ? {label}: unknown status={status!r}")
            skip_count += 1

    # Requirements section (auto-verified aicad.req/1 requirements)
    if req_results:
        lines.append("")
        lines.append("Requirements (auto-verified, evidence written back):")
        for rid in sorted(req_results):
            r = req_results[rid]
            status = r.get("status")
            if status == "done" and r.get("passed"):
                lines.append(f"  ✓ {rid}: VERIFIED — status + battery evidence written to req doc")
                pass_count += 1
            elif status == "done":
                lines.append(f"  ✗ {rid}: FAILED — status 'failed' + battery evidence written to req doc")
                fail_count += 1
            else:
                reason = r.get("reason", "requirement check unavailable")
                lines.append(f"  ? {rid}: {reason}")
                caveats.append(f"Requirement {rid}: {reason}")
                unavail_count += 1

    # Overall verdict
    lines.append("")
    checked = pass_count + fail_count
    if checked == 0:
        overall = "NO CHECKS RAN"
    elif fail_count == 0 and unavail_count == 0:
        overall = "PASS"
    elif fail_count > 0:
        overall = "FAIL"
    else:
        overall = "PARTIAL (some backends unavailable)"

    lines.append(f"Overall: {overall}")
    lines.append(
        f"  {pass_count} passed, {fail_count} failed, "
        f"{skip_count} skipped, {unavail_count} unavailable  "
        f"(elapsed: {elapsed_ms / 1000:.1f}s)"
    )

    # Caveats
    if caveats:
        lines.append("")
        lines.append("Caveats:")
        for c in caveats:
            lines.append(f"  - {c}")

    # Attribution (narration.md §source)
    lines.append("")
    lines.append(
        "(Source: DISPATCH_TABLE (services/dag/verify.py) + battery._get_fn "
        "— numbers narrated verbatim from tool payloads, never recomputed)"
    )

    return "\n".join(lines)


def _check_detail_pass(check_name: str, result: dict) -> str:
    """One-line pass detail narrated verbatim from result keys."""
    if check_name == "layout_coverage":
        covered = result.get("covered", 0)
        total = result.get("total", 0)
        note = result.get("note", "")
        if total == 0:
            return f"no critical dims/interfaces on file — {note}  (source: layout_plan meta)"
        return f"{covered}/{total} critical dim(s)/interface(s) verified  (source: layout_plan meta)"
    if check_name == "interference":
        n = len(result.get("interferences", []))
        return f"0 interference pairs (source: check_interference.interferences)"
    if check_name == "interface_check":
        n = len(result.get("pairs", []))
        warns = len([f for f in result.get("findings", [])
                     if isinstance(f, dict) and f.get("severity") == "warn"])
        return (f"{n} interface(s) aligned"
                + (f", {warns} advisory note(s)" if warns else "")
                + "  (source: interface_check)")
    if check_name == "erc":
        errs = result.get("errors", 0)
        warns = result.get("warnings", 0)
        return f"{errs} error(s), {warns} warning(s)  (source: ecad_run_erc)"
    if check_name == "drc":
        errs = result.get("errors", 0)
        warns = result.get("warnings", 0)
        return f"{errs} error(s), {warns} warning(s)  (source: ecad_run_drc)"
    if check_name == "firmware":
        a = result.get("assertions", [])
        count = len(a) if isinstance(a, list) else "?"
        return f"all {count} assertion(s) held  (source: fw_sim_run)"
    if check_name == "dc_ratio":
        rows = result.get("results", [])
        max_dc = max((float(r.get("dc_ratio", 0)) for r in rows), default=0.0)
        return f"max dc_ratio={max_dc:.3g} ≤ 1.0  (source: struct_check)"
    if check_name == "budget":
        cost = result.get("unit_cost")
        if cost is not None:
            return f"unit_cost=${float(cost):.2f}  (source: bom_price)"
        return "(source: bom_price)"
    if check_name == "feature_overlap":
        n = len(result.get("overlaps", []))
        return f"0 overlapping features  (source: check_feature_overlap)"
    if check_name == "dfm":
        v = result.get("violations", 0)
        return f"{v} DFM violation(s)  (source: dfm_check)"
    return ""


def _check_detail_fail(check_name: str, result: dict) -> str:
    """One-line fail detail narrated verbatim from result keys."""
    if check_name == "layout_coverage":
        covered = result.get("covered", 0)
        total = result.get("total", 0)
        uncovered = result.get("uncovered", [])
        ids = ", ".join(u.get("id", "?") for u in uncovered[:3])
        more = f" + {len(uncovered) - 3} more" if len(uncovered) > 3 else ""
        return (
            f"{total - covered}/{total} critical dim(s)/interface(s) unverified: "
            f"{ids}{more}  (source: layout_plan meta)"
        )
    if check_name == "interference":
        items = result.get("interferences", [])
        n = len(items)
        summary = ""
        if n and isinstance(items[0], dict):
            a = items[0].get("a", "?")
            b = items[0].get("b", "?")
            summary = f" (e.g. {a} ↔ {b})"
        return f"{n} interference pair(s){summary}  (source: check_interference)"
    if check_name == "interface_check":
        fails = [f for f in result.get("findings", [])
                 if isinstance(f, dict) and f.get("severity") == "fail"]
        first = ""
        if fails:
            f0 = fails[0]
            first = f" (e.g. {f0.get('detail', '?')})"
        return f"{len(fails)} alignment failure(s){first}  (source: interface_check)"
    if check_name == "erc":
        errs = result.get("errors", "?")
        return f"{errs} ERC error(s)  (source: ecad_run_erc)"
    if check_name == "drc":
        errs = result.get("errors", "?")
        return f"{errs} DRC error(s)  (source: ecad_run_drc)"
    if check_name == "firmware":
        a = result.get("assertions", [])
        failed = [x for x in a if isinstance(x, dict) and not x.get("passed", True)]
        return f"{len(failed)} assertion(s) failed  (source: fw_sim_run)"
    if check_name == "dc_ratio":
        rows = result.get("results", [])
        failed_rows = [r for r in rows if not r.get("passed", True)]
        if failed_rows:
            worst = max(failed_rows, key=lambda r: float(r.get("dc_ratio", 0)))
            elem = worst.get("element_id", "?")
            dc = worst.get("dc_ratio", "?")
            return f"{len(failed_rows)} member(s) exceed dc_ratio=1.0; worst: {elem} dc={dc:.3g}  (source: struct_check)"
        return "dc_ratio check failed  (source: struct_check)"
    if check_name == "budget":
        cost = result.get("unit_cost")
        if cost is not None:
            return f"unit_cost=${float(cost):.2f}  (source: bom_price)"
        return "cost resolution failed  (source: bom_price)"
    if check_name == "feature_overlap":
        items = result.get("overlaps", [])
        return f"{len(items)} overlap(s) detected  (source: check_feature_overlap)"
    if check_name == "dfm":
        v = result.get("violations", "?")
        return f"{v} DFM violation(s)  (source: dfm_check)"
    return ""
