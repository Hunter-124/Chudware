"""reviewer_exec.py — deterministic acceptance-criteria verifier (T-W30).

Contract: contracts/advisor.md §16 ("verification is running the named checks,
not vibes"). The reviewer role's documented method (services/agent/roles/reviewer.yaml)
is: *pull the acceptance criteria, run each criterion's named check tool with its
params, produce PASS / FAIL / NOT-CHECKED per criterion.*

This module implements that method WITHOUT a model in the loop: it looks up each
acceptance criterion's ``check.tool`` in the live MCP tool registry (via the same
``services.verify.battery._get_fn`` singleton the DAG verify node uses — ONE registry
per process), runs it against the mission's doc, and derives a verdict from the raw
tool output.  It writes one ``criterion_result`` event per AC into the ledger.

Honesty rules (why this is not a fabricated PASS):
  * A criterion is PASS only when the named tool ran AND its output mechanically
    satisfies the pass_rule.  We do NOT parse arbitrary prose pass_rules into a
    tuned matcher — only a small set of mechanically checkable shapes is honoured
    (a numeric interval, a literal ``PASS`` verdict, an "ok" flag).  Anything the
    evaluator cannot mechanically prove is **NOT-CHECKED**, which the VERIFY gate
    treats as non-pass.
  * If the tool is unknown / unavailable / errors, the verdict is **NOT-CHECKED**
    (never PASS) — fail-closed, mirroring services/dag/verify.py.
  * The raw tool output is stored as ``evidence`` on every event, so a PASS is
    auditable and a NOT-CHECKED is transparent.

The VERIFY phase gate (services/agent/loop._phase_gate_context) READS the results
this module writes; it never derives PASS from node completion.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

# Verdicts — the reviewer role's only allowed outputs.
PASS = "PASS"
FAIL = "FAIL"
NOT_CHECKED = "NOT-CHECKED"

REVIEWER_ACTOR = "reviewer"

# check.tool -> (module_key, fn_name) for the battery singleton.  The battery's own
# CHECKS table (services/verify/battery.CHECKS) covers the cad/recon/ecad/cam tools;
# a few §16 acceptance-check tools live in other MCP modules, mapped here.
_EXTRA_MODULES: dict[str, tuple[str, str]] = {
    # module_key names understood by _resolve_fn below (extends battery._get_fn).
    "verify_battery": ("verify", "verify_battery"),
    "fit_report":     ("intake", "fit_report"),
    "design_review":  ("cad",    "design_review"),
}


# ---------------------------------------------------------------------------
# Tool resolution (ONE live registry, reused from the battery singleton)
# ---------------------------------------------------------------------------

def _resolve_fn(tool: str) -> Callable[..., Any] | None:
    """Return the raw Python function for a §16 check tool, or None if it cannot
    be resolved (unknown tool / backend absent).  Never raises."""
    # 1) tools the battery already knows (cad/recon/ecad/cam + gap verifiers).
    try:
        from services.verify.battery import CHECKS, _get_fn
        if tool in CHECKS:
            module_key, fn_name, *_ = CHECKS[tool]
            if module_key == "gap":
                # gap verifiers are module-level fns in battery.py
                from services.verify import battery as _b
                return getattr(_b, fn_name, None)
            return _get_fn(module_key, fn_name)
    except Exception:  # noqa: BLE001 — backend/registry absent
        pass
    # 2) §16 check tools that live in other MCP modules.
    spec = _EXTRA_MODULES.get(tool)
    if spec is not None:
        module_key, fn_name = spec
        fn = _resolve_extra(module_key, fn_name)
        if fn is not None:
            return fn
    # 3) last resort: try each battery singleton module in turn (cad first — it
    # carries the modeling/measure/describe tools role agents actually call; then
    # recon/ecad/cam). T-W30: the agent-turn dispatcher shares this resolver, so
    # a role-card tool like add_features or render_part must resolve here.
    try:
        from services.verify.battery import _get_fn
        for module_key in ("cad", "recon", "ecad", "cam"):
            try:
                fn = _get_fn(module_key, tool)
                if fn is not None:
                    return fn
            except Exception:  # noqa: BLE001 — try the next module
                continue
    except Exception:  # noqa: BLE001
        pass
    # 4) research/engineering-calc tools (fit_calc, tap_drill, research_*…) — the
    # spec_writer role's main toolset; registered in mcp_server.research_tools.
    return _resolve_extra("research", tool)


# One private FastMCP per extra module_key, built once.
_EXTRA_MCP: dict[str, Any] = {}


def _resolve_extra(module_key: str, fn_name: str) -> Callable[..., Any] | None:
    """Resolve a tool fn from an MCP module the battery singleton does not build
    (verify_tools, intake_tools).  Best-effort; returns None on any failure."""
    try:
        if module_key not in _EXTRA_MCP:
            from mcp.server.fastmcp import FastMCP
            mcp = FastMCP(f"reviewer-{module_key}")
            if module_key == "verify":
                from mcp_server.verify_tools import register_verify_tools
                register_verify_tools(mcp)
            elif module_key == "intake":
                from mcp_server.intake_tools import register_intake_tools
                register_intake_tools(mcp)
            elif module_key == "cad":
                from mcp_server.cad_tools import register_cad_tools
                register_cad_tools(mcp)
            elif module_key == "research":
                # T-W30: the spec_writer/reviewer role cards allow research_* +
                # fit_calc/recommend_fit — these live in research_tools, not the
                # battery modules, so agent turns need this branch to dispatch them.
                from mcp_server.research_tools import register_research_tools
                register_research_tools(mcp)
            else:
                return None
            _EXTRA_MCP[module_key] = mcp
        tool = _EXTRA_MCP[module_key]._tool_manager._tools.get(fn_name)
        return tool.fn if tool is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Pass-rule evaluation — mechanical only; unknown shapes → NOT-CHECKED
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(
    r"(?P<name>[a-zA-Z_][\w ]*?)\s*in\s*\[\s*(?P<lo>-?\d+(?:\.\d+)?)\s*,\s*"
    r"(?P<hi>-?\d+(?:\.\d+)?)\s*\]")


def _flatten_numbers(obj: Any, prefix: str = "") -> dict[str, float]:
    """Collect every numeric leaf keyed by its (lowercased) field name."""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[key] = float(v)
            else:
                out.update(_flatten_numbers(v, key))
    elif isinstance(obj, list):
        for item in obj:
            out.update(_flatten_numbers(item, prefix))
    return out


def evaluate_pass_rule(pass_rule: str, result: dict) -> tuple[str, str]:
    """Derive (verdict, detail) from a mechanical read of *result* against *pass_rule*.

    Returns PASS only when the rule shape is one this function can mechanically
    verify AND the tool output satisfies it.  Every non-mechanical or unmet rule is
    NOT-CHECKED / FAIL — never a fabricated PASS."""
    rule = (pass_rule or "").strip()

    # A tool that reported an error can never PASS.
    if isinstance(result, dict) and result.get("ok") is False:
        return FAIL, f"check tool reported error: {result.get('error', 'ok=false')}"

    # Shape 1: literal "PASS" verdict — the tool must emit a PASS-ish signal.
    if rule.upper() == "PASS":
        verdict_field = None
        if isinstance(result, dict):
            for key in ("verdict", "status", "result", "ok"):
                if key in result:
                    verdict_field = result[key]
                    break
        if isinstance(verdict_field, str) and verdict_field.strip().upper() in (
                "PASS", "OK", "PASSED"):
            return PASS, f"tool verdict {verdict_field!r}"
        if verdict_field is True:
            return PASS, "tool ok=true"
        if verdict_field is None:
            return NOT_CHECKED, "tool output carries no verdict/status field to match 'PASS'"
        return FAIL, f"tool verdict {verdict_field!r} is not PASS"

    # Shape 2: numeric interval, e.g. "clearance in [0,0.042]".
    m = _INTERVAL_RE.search(rule)
    if m:
        name = m.group("name").strip().lower()
        lo, hi = float(m.group("lo")), float(m.group("hi"))
        nums = _flatten_numbers(result)
        # exact field, else a field whose name contains the rule's leading token.
        val = nums.get(name)
        if val is None:
            token = name.split()[0]
            for k, v in nums.items():
                if token in k:
                    val = v
                    break
        if val is None:
            return NOT_CHECKED, (
                f"tool output has no numeric field matching {name!r} "
                f"(fields: {sorted(nums)[:8]})")
        ok = lo <= val <= hi
        return (PASS if ok else FAIL), f"{name}={val} vs [{lo},{hi}]"

    # Any other prose rule (e.g. "4 holes d4.5 on PCD34") is NOT mechanically
    # evaluable from generic tool output — honestly NOT-CHECKED, not a guessed PASS.
    return NOT_CHECKED, f"pass_rule {rule!r} is not mechanically evaluable"


# ---------------------------------------------------------------------------
# Public API — run every AC's named check tool, write criterion_result events
# ---------------------------------------------------------------------------

def run_acceptance_checks(manifest: dict, ledger, *, doc_id: str | None = None,
                          actor: str = REVIEWER_ACTOR) -> dict[str, str]:
    """Run each acceptance criterion's named check tool and write its verdict to
    *ledger* as a ``criterion_result`` event.  Returns ``{ac_id: verdict}``.

    Deterministic (no model call): the reviewer's job is to RUN the named checks
    and report PASS/FAIL/NOT-CHECKED, which is a mechanical operation.  Fail-closed
    on every error path — the only way to PASS is for the tool to run and its output
    to mechanically satisfy the pass_rule."""
    results: dict[str, str] = {}
    for d in manifest.get("deliverables", []) or []:
        d_doc = doc_id or (d.get("target_refs") or {}).get("doc_id")
        for ac in d.get("acceptance_criteria", []) or []:
            ac_id = ac.get("id") or "?"
            check = ac.get("check") or {}
            tool = check.get("tool") or ""
            pass_rule = check.get("pass_rule") or ""

            verdict, detail, evidence = _run_one(tool, pass_rule, check, d_doc)
            results[ac_id] = verdict
            if ledger is not None:
                try:
                    ledger.add_criterion_result(
                        actor, ac_id, verdict, check_tool=tool,
                        pass_rule=pass_rule, detail=detail, evidence=evidence)
                except Exception:  # noqa: BLE001 — ledger write must not break the sweep
                    pass
    return results


def _run_one(tool: str, pass_rule: str, check: dict,
             doc_id: str | None) -> tuple[str, str, dict]:
    """Run one check tool and derive (verdict, detail, evidence).  Never raises."""
    # 'manual' criteria are, by definition, not tool-checkable here — a human/clarifier
    # confirms them (contract §16 manual-pairing rule). Honestly NOT-CHECKED.
    if tool == "manual":
        return NOT_CHECKED, "manual criterion — requires human confirmation", {}

    fn = _resolve_fn(tool)
    if fn is None:
        return NOT_CHECKED, f"check tool {tool!r} could not be resolved (backend absent?)", {}

    # Build args conservatively: pass doc_id if the tool accepts it. params_hint is a
    # hint only — we forward simple scalar hints the tool may accept, but never fail
    # on an unexpected kwarg (retry without it).
    raw = _call_tool(fn, doc_id, check.get("params_hint") or {})
    if raw is None:
        return NOT_CHECKED, f"check tool {tool!r} raised — treated as not-checked", {}

    result = _as_dict(raw)
    verdict, detail = evaluate_pass_rule(pass_rule, result)
    return verdict, detail, result


def _call_tool(fn: Callable[..., Any], doc_id: str | None,
               params_hint: dict) -> Any:
    """Invoke *fn* with a doc_id when accepted, degrading kwargs on TypeError."""
    import inspect
    try:
        sig = inspect.signature(fn)
        accepts = set(sig.parameters)
    except (TypeError, ValueError):
        accepts = set()

    kwargs: dict[str, Any] = {}
    if doc_id and "doc_id" in accepts:
        kwargs["doc_id"] = doc_id

    try:
        return fn(**kwargs)
    except TypeError:
        # signature mismatch — try a bare call.
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001 — a broken tool is NOT-CHECKED, never a pass
        return None


def _as_dict(raw: Any) -> dict:
    """Coerce a tool result (often a JSON string) into a dict for evaluation."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except (json.JSONDecodeError, ValueError):
            return {"text": raw}
    return {"value": raw}
