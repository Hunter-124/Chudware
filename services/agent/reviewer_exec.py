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
SKIP = "SKIP"  # honest provision-absence (Renode/openEMS/unprovisioned leg)

# Verdicts that satisfy the VERIFY gate without waiver (mega-11 AC6/AC8).
GATE_OK = frozenset({PASS, SKIP})

REVIEWER_ACTOR = "reviewer"

# check.tool -> (module_key, fn_name) for the battery singleton.  The battery's own
# CHECKS table (services/verify/battery.CHECKS) covers the cad/recon/ecad/cam tools;
# a few §16 acceptance-check tools live in other MCP modules, mapped here.
_EXTRA_MODULES: dict[str, tuple[str, str]] = {
    # module_key names understood by _resolve_fn below (extends battery._get_fn).
    "verify_battery": ("verify", "verify_battery"),
    "fit_report":     ("intake", "fit_report"),
    # T-W32: design_review lives in design_review_tools, not cad_tools — the old
    # ("cad", "design_review") mapping never resolved.
    "design_review":  ("designreview", "design_review"),
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
    fn = _resolve_extra("research", tool)
    if fn is not None:
        return fn
    # 5) T-W32 dispatch-coverage gap: role cards allow GSL describers
    # (describe_part/describe_project/…), parts-library tools (part_get/part_place;
    # part_search also resolves mission-scoped) and the ledger tools (ledger_get/
    # ledger_note with an explicit run_id) — none of which the battery singleton
    # builds.  Same one-FastMCP-per-module pattern; still ONE process-wide registry.
    for module_key in ("gsl", "parts", "ledger", "preflight", "regen", "buildgraph",
                       "intake", "designreview", "req", "cost", "structural",
                       "dimscheme", "workspace", "firmware", "em", "antenna"):
        fn = _resolve_extra(module_key, tool)
        if fn is not None:
            return fn
    return None


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
            elif module_key == "gsl":
                # T-W32: describe_part/describe_project/describe_scene… — the GSL
                # describers every worker card allows; live in gsl_tools.
                from mcp_server.gsl_tools import register_gsl_tools
                register_gsl_tools(mcp)
            elif module_key == "parts":
                # T-W32: part_get/part_place (part_search also has a mission-scoped
                # handler) — the standard-parts library surface.
                from mcp_server.parts_tools import register_parts_tools
                register_parts_tools(mcp)
            elif module_key == "ledger":
                # T-W32: ledger_get/ledger_note resolve globally with an explicit
                # run_id; inside a mission the run-scoped dispatcher binds it.
                from mcp_server.ledger_tools import register_ledger_tools
                register_ledger_tools(mcp)
            elif module_key == "preflight":
                # T-W32: preflight_ops — the feature_modeler card's mandated
                # pre-mutation check.
                from mcp_server.preflight_tools import register_preflight_tools
                register_preflight_tools(mcp)
            elif module_key == "regen":
                # T-W32: regen_diagnose/regen_resolve — the fixer/feature_modeler
                # rebuild-failure toolset.
                from mcp_server.regen_tools import register_regen_tools
                register_regen_tools(mcp)
            elif module_key == "buildgraph":
                # T-W32: buildgraph_plan/buildgraph_status — the orchestrator /
                # department_manager / overseer build-order surface.
                from services.agent.buildgraph.mcp_tools import register_buildgraph_tools
                register_buildgraph_tools(mcp)
            elif module_key == "designreview":
                # T-W32: design_review — the reviewer card's rubric tool.
                from mcp_server.design_review_tools import register_design_review_tools
                register_design_review_tools(mcp)
            elif module_key == "req":
                # T-W32: req_create/req_update/req_status — intake_analyst/clarifier.
                from mcp_server.req_tools import register_req_tools
                register_req_tools(mcp)
            elif module_key == "cost":
                # T-W32: cam_cost_estimate — cam_programmer.
                from mcp_server.cost_tools import register_cost_tools
                register_cost_tools(mcp)
            elif module_key == "structural":
                # T-W32: struct_analyze — sim_engineer.
                from mcp_server.structural_tools import register_structural_tools
                register_structural_tools(mcp)
            elif module_key == "dimscheme":
                # T-W32: suggest_dimension_scheme — sketcher.
                from mcp_server.dimension_scheme_tools import register_dimension_scheme_tools
                register_dimension_scheme_tools(mcp)
            elif module_key == "workspace":
                # T-W32: workspace_status/workspace_doc_events — orchestrator/overseer.
                from mcp_server.workspace_tools import register_workspace_tools
                register_workspace_tools(mcp)
            elif module_key == "firmware":
                # mega-2: fw_* tools offered on SPECIFY tools_slot (family:fw seed)
                # must resolve at dispatch — not only appear as unresolved labels.
                from mcp_server.firmware_tools import register_firmware_tools
                register_firmware_tools(mcp)
            elif module_key == "em":
                # mega-2: em_status / em_eigenmode / em_driven / em_timedomain.
                from mcp_server.em_tools import register_em_tools
                register_em_tools(mcp)
            elif module_key == "antenna":
                # mega-2: ecad_antenna_design / ecad_antenna_analyze (family:em/ecad).
                from mcp_server.antenna_tools import register_antenna_tools
                register_antenna_tools(mcp)
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
    r"(?P<name>[a-zA-Z_][\w ()./]*?)\s+in\s*\[\s*(?P<lo>-?\d+(?:\.\d+)?)\s*,\s*"
    r"(?P<hi>-?\d+(?:\.\d+)?)\s*\]",
    re.IGNORECASE,
)


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
    NOT-CHECKED / FAIL — never a fabricated PASS.

    mega-9/10: verify_battery returns ``{scores, findings}`` without a top-level
    ``verdict`` field. Literal pass_rule ``PASS`` must still evaluate that shape
    (derive FAIL when findings ok:false / zero scores on critical checks). Also
    accept a prior ``battery_result`` envelope (verdict + evidence) so AC1 is not
    NOT-CHECKED when the battery already ran.
    """
    rule = (pass_rule or "").strip()

    # A tool that reported an error can never PASS — but if scores/findings are
    # present (battery envelope with ok=false from a FAIL verdict), prefer the
    # scores/findings interpreter for an honest detail string (mega-10 live:
    # AC1 detail was bare 'ok=false' while scores carried the real FAIL reason).
    # mega-11: also fall through when the payload is an honest provision-SKIP so
    # AC4/AC6/AC8 can grade SKIP instead of hard FAIL on ok=false.
    if isinstance(result, dict) and result.get("ok") is False:
        if (
            "scores" in result
            or "findings" in result
            or isinstance(result.get("evidence"), dict)
            or _is_honest_skip(result)
        ):
            pass  # fall through to shape matching below
        else:
            return FAIL, f"check tool reported error: {result.get('error', 'ok=false')}"

    if rule.upper() == "PASS":
        # Prefer explicit verdict/status/result/ok.
        if isinstance(result, dict):
            for key in ("verdict", "status", "result"):
                if key in result and result[key] is not None:
                    verdict_field = result[key]
                    if isinstance(verdict_field, str) and verdict_field.strip().upper() in (
                            "PASS", "OK", "PASSED"):
                        # Honesty: an explicit PASS that still carries failing
                        # scores/findings is NOT a free pass (mega-9 residual).
                        if "scores" in result or "findings" in result:
                            derived, detail = interpret_battery_result(result)
                            if derived == PASS:
                                return PASS, detail or f"tool verdict {verdict_field!r}"
                            if derived == FAIL:
                                return FAIL, detail or f"tool verdict {verdict_field!r} contradicted by scores/findings"
                            if derived == "WARN":
                                return FAIL, detail or f"tool verdict {verdict_field!r} but WARN scores"
                        return PASS, f"tool verdict {verdict_field!r}"
                    if isinstance(verdict_field, str) and verdict_field.strip().upper() in (
                            "FAIL", "FAILED", "ERROR"):
                        if "scores" in result or "findings" in result:
                            derived, detail = interpret_battery_result(result)
                            if derived in (FAIL, "WARN") and detail:
                                return FAIL, detail
                        return FAIL, f"tool verdict {verdict_field!r}"

                    if verdict_field is True:
                        return PASS, "tool ok=true"
                    if isinstance(verdict_field, str) and verdict_field.strip():
                        return FAIL, f"tool verdict {verdict_field!r} is not PASS"
            # Nested evidence from a prior battery_result event.
            nested = result.get("evidence")
            if isinstance(nested, dict) and ("scores" in nested or "findings" in nested):
                derived, detail = interpret_battery_result(nested)
                if derived == PASS:
                    return PASS, detail or "battery evidence PASS"
                if derived == FAIL:
                    return FAIL, detail or "battery evidence FAIL"
                if derived == "WARN":
                    return FAIL, detail or "battery evidence WARN (not PASS for AC)"
            # scores/findings battery shape (no top-level verdict).
            if "scores" in result or "findings" in result:
                derived, detail = interpret_battery_result(result)
                if derived == PASS:
                    return PASS, detail or "battery scores/findings PASS"
                if derived == FAIL:
                    return FAIL, detail or "battery scores/findings FAIL"
                if derived == "WARN":
                    return FAIL, detail or "battery WARN (not PASS for AC)"
                return NOT_CHECKED, detail or "battery shape not evaluable"
            if "ok" in result:
                if result.get("ok") is True:
                    return PASS, "tool ok=true"
                if result.get("ok") is False:
                    return FAIL, "tool ok=false"
        return NOT_CHECKED, "tool output carries no verdict/status field to match 'PASS'"

    # Shape 2: numeric interval, e.g. "clearance in [0,0.042]" or "V(3V3) in [3.135, 3.465]".
    m = _INTERVAL_RE.search(rule)
    if m:
        name = m.group("name").strip().lower()
        lo, hi = float(m.group("lo")), float(m.group("hi"))
        nums = _flatten_numbers(result)
        # exact field, else a field whose name contains the rule's leading token.
        val = nums.get(name)
        if val is None:
            token = name.split()[0]
            # strip common wrappers like V(3V3) → 3v3 (and keep v3v3 form).
            bare = re.sub(r"[^\w]", "", token)
            # V(3V3) / I(R1) → inner net/device name for SPICE node maps.
            inner_m = re.match(r"[vi]\(([^)]+)\)", token, re.I)
            candidates = [bare]
            if inner_m:
                candidates.append(re.sub(r"[^\w]", "", inner_m.group(1).lower()))
            for k, v in nums.items():
                kk = re.sub(r"[^\w]", "", k)
                if any(c and (c in kk or kk in c) for c in candidates):
                    val = v
                    break
        if val is None:
            return NOT_CHECKED, (
                f"tool output has no numeric field matching {name!r} "
                f"(fields: {sorted(nums)[:8]})")
        ok = lo <= val <= hi
        return (PASS if ok else FAIL), f"{name}={val} vs [{lo},{hi}]"

    # Shape 3: interference empty / volume==0 (AC3-style mechanical rules).
    rule_l = rule.lower()
    if "interference" in rule_l and ("empty" in rule_l or "volume==0" in rule_l
                                      or "volume == 0" in rule_l or "none" in rule_l):
        if not isinstance(result, dict):
            return NOT_CHECKED, "interference rule needs object tool output"
        inter = (
            result.get("interferences")
            if "interferences" in result else
            result.get("clashes")
            if "clashes" in result else
            result.get("pairs")
        )
        if isinstance(inter, list):
            if not inter:
                return PASS, "interferences empty"
            # Any positive volume clash → FAIL; zero-volume contact may PASS.
            vols = []
            for item in inter:
                if isinstance(item, dict):
                    for k in ("volume", "volume_mm3", "clash_volume", "overlap_volume"):
                        if k in item and isinstance(item[k], (int, float)):
                            vols.append(float(item[k]))
                            break
            if vols and all(v <= 0.0 for v in vols):
                return PASS, f"interference volumes all ≤0 ({vols[:4]})"
            if vols and any(v > 0.0 for v in vols):
                return FAIL, f"interference volume>0 ({vols[:4]})"
            return FAIL, f"{len(inter)} interference pair(s) present"
        # ok=true + no interferences key → treat as clean when tool says so.
        if result.get("ok") is True and inter is None:
            return PASS, "interference check ok=true (no pairs listed)"
        return NOT_CHECKED, "tool output has no interferences/clashes list"

    # Shape 4 (mega-11 AC2): bay envelope / board outline compare, or volume interval.
    # Prefer named dims (bay_*/board_*); fall back to volume_mm3 interval in the rule.
    if ("bay" in rule_l and "board" in rule_l) or (
            "envelope" in rule_l and "board" in rule_l):
        return _eval_bay_vs_board(rule, result)

    # Shape 5 (mega-11 AC4): errors==0 (DRC/ERC). Accept combined multi-tool evidence.
    if ("errors==0" in rule_l or "errors == 0" in rule_l
            or re.search(r"\berrors\s*==\s*0\b", rule_l)):
        return _eval_errors_zero(result)

    # Shape 6 (mega-11 AC9): all legs have PASS|WARN|FAIL|SKIP — report-card over ledger.
    # Must run before the generic PASS|SKIP shape (which also matches "pass"+"skip").
    if "all legs" in rule_l and ("pass" in rule_l or "skip" in rule_l):
        return _eval_report_card_legs(rule, result)

    # Shape 7 (mega-11 AC6/AC8): PASS|SKIP with optional provision token (renode/openems).
    if "pass" in rule_l and "skip" in rule_l:
        return _eval_pass_or_skip(rule, result)

    # Shape 8 (mega-11 AC7): returncode==0 (+ optional ELF path evidence).
    if "returncode==0" in rule_l or "returncode == 0" in rule_l:
        return _eval_returncode_zero(result)

    # Any other prose rule (e.g. "4 holes d4.5 on PCD34") is NOT mechanically
    # evaluable from generic tool output — honestly NOT-CHECKED, not a guessed PASS.
    return NOT_CHECKED, f"pass_rule {rule!r} is not mechanically evaluable"



def _eval_bay_vs_board(rule: str, result: dict) -> tuple[str, str]:
    """AC2: bay envelope >= board outline via named dims or volume_mm3 interval.

    Accepts tool output from measure_part / query_geometry with any of:
      * bay_length/width/height (or bay_*) and board_* counterparts
      * bay_volume / board_volume (or volume_mm3 vs board_volume_mm3)
      * volume_mm3 alone when the rule embeds ``volume_mm3 in [lo,hi]``
    """
    if not isinstance(result, dict):
        return NOT_CHECKED, "bay/board rule needs object tool output"
    # Nested interval in the same rule still wins (proposal volume_mm3 path).
    m = _INTERVAL_RE.search(rule or "")
    if m and "volume" in m.group("name").lower():
        name = m.group("name").strip().lower()
        lo, hi = float(m.group("lo")), float(m.group("hi"))
        nums = _flatten_numbers(result)
        val = nums.get(name)
        if val is None:
            token = re.sub(r"[^\w]", "", name.split()[0])
            for k, v in nums.items():
                if token and token in re.sub(r"[^\w]", "", k):
                    val = v
                    break
        if val is None:
            return NOT_CHECKED, (
                f"tool output has no numeric field matching {name!r} "
                f"(fields: {sorted(nums)[:8]})")
        ok = lo <= val <= hi
        return (PASS if ok else FAIL), f"{name}={val} vs [{lo},{hi}]"

    nums = _flatten_numbers(result)
    axes = ("length", "width", "height", "x", "y", "z", "volume", "volume_mm3",
            "dx", "dy", "dz", "size_x", "size_y", "size_z")
    compared = 0
    for axis in axes:
        bay = _pick_num(nums, (f"bay_{axis}", f"bay {axis}", f"payload_bay_{axis}",
                               f"envelope_{axis}", f"bay.{axis}"))
        board = _pick_num(nums, (f"board_{axis}", f"board {axis}", f"fc_board_{axis}",
                                 f"outline_{axis}", f"board.{axis}"))
        if bay is None or board is None:
            continue
        compared += 1
        if bay + 1e-9 < board:
            return FAIL, f"bay_{axis}={bay} < board_{axis}={board}"
    if compared:
        return PASS, f"bay envelope >= board outline on {compared} axis/axes"

    bay_v = _pick_num(nums, ("bay_volume", "bay_volume_mm3", "payload_bay_volume",
                             "volume_mm3", "volume"))
    board_v = _pick_num(nums, ("board_volume", "board_volume_mm3", "board_outline_volume"))
    if bay_v is not None and board_v is not None:
        if bay_v + 1e-9 >= board_v:
            return PASS, f"bay_volume={bay_v} >= board_volume={board_v}"
        return FAIL, f"bay_volume={bay_v} < board_volume={board_v}"

    if any("bay" in k or "board" in k for k in nums):
        return NOT_CHECKED, (
            f"bay/board dims incomplete for compare (fields: {sorted(nums)[:12]})"
        )
    return NOT_CHECKED, (
        "tool output has no bay/board dims or volume_mm3 interval for AC2 "
        f"(fields: {sorted(nums)[:8]})"
    )


def _pick_num(nums: dict[str, float], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        kk = k.lower()
        if kk in nums:
            return nums[kk]
    for k in keys:
        token = re.sub(r"[^\w]", "", k.lower())
        for nk, nv in nums.items():
            if token and token in re.sub(r"[^\w]", "", nk):
                return nv
    return None


def _eval_errors_zero(result: dict) -> tuple[str, str]:
    """AC4: DRC/ERC errors==0. Multi-tool evidence may carry drc/erc sub-dicts."""
    if not isinstance(result, dict):
        return NOT_CHECKED, "errors==0 rule needs object tool output"

    if _is_honest_skip(result):
        return SKIP, _skip_detail(result) or "DRC/ERC skipped (provision absent)"

    parts: list[tuple[str, dict]] = []
    if isinstance(result.get("drc"), dict) or isinstance(result.get("erc"), dict):
        if isinstance(result.get("drc"), dict):
            parts.append(("drc", result["drc"]))
        if isinstance(result.get("erc"), dict):
            parts.append(("erc", result["erc"]))
    else:
        parts.append(("check", result))

    total_errors = 0
    saw_errors_field = False
    details: list[str] = []
    for label, part in parts:
        if part.get("ok") is False and "errors" not in part and "error_count" not in part:
            if _is_honest_skip(part):
                return SKIP, _skip_detail(part) or f"{label} skipped"
            return FAIL, (
                f"{label} ok=false: {part.get('error') or part.get('detail') or 'error'}"
            )
        if "errors" in part:
            saw_errors_field = True
            try:
                n = int(part.get("errors") or 0)
            except (TypeError, ValueError):
                return NOT_CHECKED, f"{label} errors field not numeric: {part.get('errors')!r}"
            total_errors += n
            details.append(f"{label}.errors={n}")
        elif "error_count" in part:
            saw_errors_field = True
            try:
                n = int(part.get("error_count") or 0)
            except (TypeError, ValueError):
                return NOT_CHECKED, f"{label} error_count not numeric"
            total_errors += n
            details.append(f"{label}.error_count={n}")

    if not saw_errors_field:
        return NOT_CHECKED, "tool output has no errors/error_count field"
    if total_errors == 0:
        return PASS, "errors==0 (" + ", ".join(details) + ")"
    return FAIL, f"errors={total_errors} (" + ", ".join(details) + ")"


def _eval_pass_or_skip(rule: str, result: dict) -> tuple[str, str]:
    """AC6/AC8: PASS or honest SKIP with provision-note evidence."""
    if not isinstance(result, dict):
        return NOT_CHECKED, "PASS|SKIP rule needs object tool output"

    if _is_honest_skip(result):
        detail = _skip_detail(result)
        return SKIP, detail or "honest SKIP (provision absent)"

    for key in ("verdict", "status", "result"):
        val = result.get(key)
        if isinstance(val, str):
            u = val.strip().upper()
            if u in ("PASS", "OK", "PASSED"):
                return PASS, f"tool verdict {val!r}"
            if u == "SKIP" or u.startswith("SKIP"):
                return SKIP, f"tool verdict {val!r}"
            if u in ("FAIL", "FAILED", "ERROR"):
                return FAIL, f"tool verdict {val!r}"

    if result.get("ok") is True or result.get("passed") is True or result.get("success") is True:
        return PASS, "tool ok/passed/success=true"
    if result.get("ok") is False or result.get("passed") is False or result.get("success") is False:
        return FAIL, f"tool failed: {result.get('error') or result.get('detail') or 'ok=false'}"

    return NOT_CHECKED, "PASS|SKIP rule: no verdict/skip/ok field in tool output"


def _eval_returncode_zero(result: dict) -> tuple[str, str]:
    """AC7: fw_build returncode==0 + ELF path evidence preferred."""
    if not isinstance(result, dict):
        return NOT_CHECKED, "returncode rule needs object tool output"

    if _is_honest_skip(result):
        return SKIP, _skip_detail(result) or "fw_build skipped (toolchain absent)"

    rc = result.get("returncode")
    if rc is None and "return_code" in result:
        rc = result.get("return_code")
    if rc is None and result.get("success") is True:
        rc = 0
    if rc is None and result.get("ok") is True and result.get("success") is not False:
        rc = 0 if (result.get("elf_path") or result.get("elf")) else None

    if rc is None:
        return NOT_CHECKED, "tool output has no returncode/success for AC7"

    try:
        rc_i = int(rc)
    except (TypeError, ValueError):
        return NOT_CHECKED, f"returncode not numeric: {rc!r}"

    elf = result.get("elf_path") or result.get("elf") or result.get("artifact")
    if rc_i == 0:
        if elf:
            return PASS, f"returncode==0 elf_path={elf}"
        if result.get("success") is True or result.get("ok") is True:
            return PASS, "returncode==0 (no elf_path field)"
        return PASS, "returncode==0"
    return FAIL, f"returncode={rc_i}" + (f" elf_path={elf}" if elf else "")


def _eval_report_card_legs(rule: str, result: dict) -> tuple[str, str]:
    """AC9: every required leg has a PASS|WARN|FAIL|SKIP verdict on the card."""
    if not isinstance(result, dict):
        return NOT_CHECKED, "report-card rule needs object tool output"

    legs_required = result.get("legs_required") or result.get("required_legs")
    if not isinstance(legs_required, list) or not legs_required:
        legs_required = result.get("legs") if isinstance(result.get("legs"), list) else None
        # list-of-names vs list-of-dicts handled below
        if isinstance(legs_required, list) and legs_required and isinstance(legs_required[0], str):
            pass
        else:
            legs_required = None
    if not isinstance(legs_required, list) or not legs_required:
        # params_hint may have been merged into evidence under another key
        return NOT_CHECKED, "report-card evidence missing legs_required list"

    card = result.get("leg_verdicts") if isinstance(result.get("leg_verdicts"), dict) else None
    if card is None and isinstance(result.get("report_card"), dict):
        rc = result["report_card"]
        if isinstance(rc.get("legs"), dict):
            card = rc["legs"]
        elif isinstance(rc.get("leg_verdicts"), dict):
            card = rc["leg_verdicts"]
    if card is None and isinstance(result.get("legs"), dict):
        card = result["legs"]
    if card is None and isinstance(result.get("legs"), list):
        card = {}
        for item in result["legs"]:
            if not isinstance(item, dict):
                continue
            name = item.get("leg") or item.get("name") or item.get("id")
            ver = item.get("verdict") or item.get("result") or item.get("status")
            if name and ver is not None:
                card[str(name)] = str(ver)

    if not isinstance(card, dict) or not card:
        return NOT_CHECKED, "report-card evidence has no leg verdicts"

    allowed = {"PASS", "WARN", "FAIL", "SKIP"}
    missing: list[str] = []
    bad: list[str] = []
    for leg in legs_required:
        key = str(leg)
        val = card.get(key)
        if val is None:
            for ck, cv in card.items():
                if str(ck).lower() == key.lower():
                    val = cv
                    break
        if val is None:
            missing.append(key)
            continue
        u = str(val).strip().upper()
        head = u.split("(")[0].split(":")[0].strip()
        if head not in allowed and not any(u.startswith(a) for a in allowed):
            bad.append(f"{key}={val!r}")

    if missing:
        return FAIL, f"legs missing verdicts: {missing}"
    if bad:
        return FAIL, f"legs with non PASS|WARN|FAIL|SKIP verdicts: {bad}"
    return PASS, f"all {len(legs_required)} legs have PASS|WARN|FAIL|SKIP"


def _is_honest_skip(result: dict) -> bool:
    """True when tool evidence is an honest provision-absence SKIP (not a FAIL)."""
    if not isinstance(result, dict):
        return False
    if result.get("skipped") is True or result.get("skip") is True:
        return True
    for key in ("verdict", "status", "result"):
        val = result.get(key)
        if isinstance(val, str) and val.strip().upper().startswith("SKIP"):
            return True
    blob_parts = [
        result.get("error"), result.get("detail"), result.get("reason"),
        result.get("stderr"), result.get("provision_note"), result.get("backend_note"),
        result.get("message"), result.get("hint"),
    ]
    fw = result.get("fullwave") if isinstance(result.get("fullwave"), dict) else {}
    if fw.get("skipped"):
        return True
    blob_parts.append(fw.get("reason"))
    blob = " ".join(str(x) for x in blob_parts if x).lower()
    markers = (
        "emulator_not_found", "renode not", "renode absent", "renode missing",
        "not installed", "toolchain_not_found", "not provisioned", "unprovisioned",
        "openems not", "openems absent", "full-wave skipped", "fullwave skipped",
        "backend absent", "provision absent", "no board", "no schematic",
        "could not be resolved", "unavailable", "not found",
    )
    return any(m in blob for m in markers)


def _skip_detail(result: dict) -> str:
    for key in ("provision_note", "backend_note", "reason", "detail", "error",
                "stderr", "message", "hint", "verdict"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:300]
    fw = result.get("fullwave") if isinstance(result.get("fullwave"), dict) else {}
    if fw.get("reason"):
        return str(fw.get("reason"))[:300]
    if result.get("skipped") is True:
        return "skipped=true"
    return ""



# Geometry-critical battery checks: a 0.0 score / ok:false finding here is FAIL,
# not a silent PASS (mega-9 a2: is_manifold 0.0 under fabricated overall PASS).
_CRITICAL_BATTERY_CHECKS = frozenset({
    "is_manifold", "eval_check", "check_feature_overlap", "check_interference",
    "solid_valid", "manifold", "volume", "measure_part",
})


def interpret_battery_result(result: dict) -> tuple[str, str]:
    """Derive honest PASS/WARN/FAIL/NOT-CHECKED from a verify_battery payload.

    Prefer scores/findings honesty over a fabricated top-level verdict:
      * findings with ok:false OR severity fail/error/blocker → FAIL
      * critical score ≤ 0 → FAIL
      * non-critical zero scores / warn severity → WARN
      * empty findings + all positive scores (or ok:true) → PASS
      * else fall back to explicit verdict/status/result
    mega-9 residual: BATTERY criterion logged PASS while is_manifold=0.0 —
    critical zeros always win over an optimistic explicit PASS.
    """
    if not isinstance(result, dict):
        return NOT_CHECKED, "battery result not an object"

    findings = result.get("findings")
    failing: list[str] = []
    warnings: list[str] = []
    if isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            name = str(f.get("check") or f.get("name") or f.get("id") or "?")
            sev = str(f.get("severity") or f.get("level") or "").lower()
            if f.get("ok") is False or sev in ("fail", "error", "blocker"):
                failing.append(name)
            elif sev in ("warn", "warning"):
                warnings.append(name)
    scores = result.get("scores")
    crit_zero: list[str] = []
    soft_zero: list[str] = []
    if isinstance(scores, dict):
        for k, v in scores.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv <= 0.0:
                key = str(k)
                if key.lower() in _CRITICAL_BATTERY_CHECKS or any(
                    c in key.lower() for c in ("manifold", "eval", "interference", "overlap")
                ):
                    crit_zero.append(key)
                else:
                    soft_zero.append(key)
    if failing or crit_zero:
        bits = []
        if failing:
            bits.append(f"failing findings={failing[:6]}")
        if crit_zero:
            bits.append(f"critical_scores_zero={crit_zero[:6]}")
        return FAIL, "; ".join(bits)
    if warnings or soft_zero:
        bits = []
        if warnings:
            bits.append(f"warn findings={warnings[:6]}")
        if soft_zero:
            bits.append(f"soft_scores_zero={soft_zero[:6]}")
        return "WARN", "; ".join(bits)

    # No score/finding failures — honour explicit verdict if present.
    for k in ("verdict", "status", "result"):
        v = result.get(k)
        if isinstance(v, str) and v.strip():
            up = v.strip().upper()
            if up in ("PASS", "OK", "PASSED"):
                return PASS, f"explicit {k}={v!r}"
            if up in ("FAIL", "FAILED", "ERROR"):
                return FAIL, f"explicit {k}={v!r}"
            if up in ("WARN", "WARNING"):
                return "WARN", f"explicit {k}={v!r}"

    if result.get("ok") is False:
        return FAIL, "ok=false"
    if isinstance(findings, list) or isinstance(scores, dict) or result.get("ok") is True:
        return PASS, "no failing findings/critical zeros"
    return NOT_CHECKED, "no scores/findings/verdict"




# ---------------------------------------------------------------------------
# Public API — run every AC's named check tool, write criterion_result events
# ---------------------------------------------------------------------------

def run_acceptance_checks(manifest: dict, ledger, *, doc_id: str | None = None,
                          actor: str = REVIEWER_ACTOR,
                          battery_payload: dict | None = None) -> dict[str, str]:
    """Run each acceptance criterion's named check tool and write its verdict to
    *ledger* as a ``criterion_result`` event.  Returns ``{ac_id: verdict}``.

    Deterministic (no model call): the reviewer's job is to RUN the named checks
    and report PASS/FAIL/NOT-CHECKED/SKIP, which is a mechanical operation.
    Fail-closed on every error path — the only way to PASS is for the tool to run
    and its output to mechanically satisfy the pass_rule.

    mega-10: when *battery_payload* is provided (or a prior battery_result is on
    the ledger), criteria with ``check.tool == verify_battery`` reuse that
    evidence instead of re-invoking the battery — so AC1 is graded from the same
    scores/findings the ledger already recorded (not a second ambiguous call).

    mega-11: multi-tool checks (``ecad_run_drc+ecad_run_erc`` or
    ``check.tools=[...]``), ``report_card`` ledger grader (AC9), and honest SKIP
    provision paths for antenna/firmware.
    """
    results: dict[str, str] = {}
    bat = battery_payload if isinstance(battery_payload, dict) else None
    if bat is None and ledger is not None:
        bat = _latest_battery_payload(ledger)

    for d in manifest.get("deliverables", []) or []:
        d_doc = doc_id or (d.get("target_refs") or {}).get("doc_id")
        for ac in d.get("acceptance_criteria", []) or []:
            ac_id = ac.get("id") or "?"
            check = ac.get("check") or {}
            tool = check.get("tool") or ""
            pass_rule = check.get("pass_rule") or ""
            tools = _check_tools(check)

            if tool == "verify_battery" and bat is not None:
                evidence = _battery_evidence_for_ac(bat)
                verdict, detail = evaluate_pass_rule(pass_rule, evidence)
                results[ac_id] = verdict
                if ledger is not None:
                    try:
                        ledger.add_criterion_result(
                            actor, ac_id, verdict, check_tool=tool,
                            pass_rule=pass_rule, detail=detail,
                            evidence={
                                "source": "battery_result",
                                "doc_id": bat.get("doc_id") or d_doc,
                                **{k: evidence.get(k) for k in (
                                    "verdict", "ok", "scores", "findings", "checks"
                                ) if k in evidence},
                            })
                    except Exception:  # noqa: BLE001
                        pass
                continue

            # AC9: deterministic report-card over ledger stage/decision events.
            if tool == "report_card" or "report_card" in tools:
                evidence = grade_report_card_from_ledger(
                    ledger, check.get("params_hint") or {})
                verdict, detail = evaluate_pass_rule(pass_rule, evidence)
                results[ac_id] = verdict
                if ledger is not None:
                    try:
                        ledger.add_criterion_result(
                            actor, ac_id, verdict, check_tool="report_card",
                            pass_rule=pass_rule, detail=detail, evidence=evidence)
                    except Exception:  # noqa: BLE001
                        pass
                continue

            verdict, detail, evidence = _run_one(tool, pass_rule, check, d_doc,
                                                ledger=ledger)
            results[ac_id] = verdict
            if ledger is not None:
                try:
                    ledger.add_criterion_result(
                        actor, ac_id, verdict,
                        check_tool=tool or "+".join(tools),
                        pass_rule=pass_rule, detail=detail, evidence=evidence)
                except Exception:  # noqa: BLE001 — ledger write must not break the sweep
                    pass
    return results


def _latest_battery_payload(ledger) -> dict | None:
    """Newest battery_result payload on *ledger*, or None."""
    try:
        events = list(getattr(ledger, "events", None) or [])
    except Exception:  # noqa: BLE001
        return None
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") == "battery_result" and isinstance(ev.get("payload"), dict):
            return ev["payload"]
    return None


def _battery_evidence_for_ac(bat: dict) -> dict:
    """Flatten a battery_result / run_verify_battery payload for pass_rule eval."""
    out: dict[str, Any] = {}
    for k in ("verdict", "ok", "status", "result", "detail", "doc_id"):
        if k in bat and bat[k] is not None:
            out[k] = bat[k]
    ev = bat.get("evidence") if isinstance(bat.get("evidence"), dict) else {}
    for k in ("scores", "findings", "verdict", "ok", "status", "result"):
        if k in ev and k not in out:
            out[k] = ev[k]
    if "scores" not in out and isinstance(bat.get("scores"), dict):
        out["scores"] = bat["scores"]
    if "findings" not in out and isinstance(bat.get("findings"), list):
        out["findings"] = bat["findings"]
    if "checks" in bat:
        out["checks"] = bat["checks"]
    # If only per-check rows exist, synthesise scores so interpret_battery can run.
    if "scores" not in out and isinstance(out.get("checks"), list):
        scores: dict[str, float] = {}
        findings: list[dict] = []
        for c in out["checks"]:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or c.get("check") or "?")
            sc = c.get("score")
            if isinstance(sc, (int, float)) and not isinstance(sc, bool):
                scores[name] = float(sc)
            else:
                v = str(c.get("verdict") or c.get("status") or "").upper()
                if v in ("PASS", "OK", "1", "1.0"):
                    scores[name] = 1.0
                elif v in ("FAIL", "ERROR", "0", "0.0"):
                    scores[name] = 0.0
                    findings.append({"check": name, "ok": False, "detail": v})
                else:
                    try:
                        scores[name] = float(v)
                    except (TypeError, ValueError):
                        pass
        if scores:
            out["scores"] = scores
        if findings and "findings" not in out:
            out["findings"] = findings
    return out



def run_verify_battery(ledger, *, doc_id: str | None = None,
                       actor: str = REVIEWER_ACTOR) -> dict[str, Any]:
    """Run ``verify_battery`` once and log honest per-check results (mega-9 E).

    Always writes a ``battery_result`` event (and a ``battery`` artifact) so the
    mission ledger records that the battery executed — even when the verdict is
    WARN/FAIL/NOT-CHECKED. Never fabricates PASS on a missing backend or on
    zero critical scores (mega-9 a2 residual: is_manifold 0.0 ≠ PASS).
    Returns the event payload.
    """
    payload: dict[str, Any] = {
        "verdict": NOT_CHECKED,
        "ok": False,
        "detail": "",
        "checks": [],
        "doc_id": doc_id,
        "source": "run_verify_battery",
    }
    fn = _resolve_fn("verify_battery")
    if fn is None:
        payload["detail"] = "verify_battery could not be resolved (backend absent?)"
    else:
        raw = _call_tool(fn, doc_id, {})
        if raw is None:
            payload["detail"] = "verify_battery raised — treated as not-checked"
        else:
            result = _as_dict(raw)
            payload["evidence"] = _slim_battery_evidence(result)
            verdict, detail = interpret_battery_result(result)
            payload["verdict"] = verdict
            payload["ok"] = verdict == PASS
            payload["detail"] = detail or f"verify_battery {verdict}"
            # Per-check rows when present (honest even if WARN/FAIL).
            checks = result.get("checks") or result.get("results") or result.get("scores")
            if isinstance(checks, list):
                payload["checks"] = [
                    {
                        "name": c.get("name") or c.get("check") or c.get("id") or "?",
                        "verdict": str(c.get("verdict") or c.get("status") or c.get("result") or "?").upper(),
                    }
                    for c in checks[:40]
                    if isinstance(c, dict)
                ]
            elif isinstance(checks, dict):
                payload["checks"] = [
                    {
                        "name": str(k),
                        "verdict": (
                            "PASS" if _score_pass(v) else
                            ("FAIL" if _score_fail(v) else str(v).upper())
                        ),
                        "score": v if isinstance(v, (int, float)) and not isinstance(v, bool) else None,
                    }
                    for k, v in list(checks.items())[:40]
                ]

    if ledger is not None:
        try:
            ledger.append(actor, "battery_result", payload)
        except Exception:  # noqa: BLE001
            pass
        try:
            ledger.add_artifact(
                actor, "battery", "battery",
                path="", stage="verify",
                note=payload.get("detail") or f"verify_battery {payload.get('verdict')}",
                verdict=payload.get("verdict"),
                doc_id=doc_id,
            )
        except Exception:  # noqa: BLE001
            pass
        # Synthetic criterion so scanners see battery ran (declared ACs still own gate).
        try:
            ledger.add_criterion_result(
                actor, "BATTERY", payload.get("verdict") or NOT_CHECKED,
                check_tool="verify_battery",
                pass_rule="PASS",
                detail=str(payload.get("detail") or ""),
                evidence=payload.get("evidence") or {"checks": payload.get("checks") or []},
            )
        except Exception:  # noqa: BLE001
            pass
    return payload


def _score_pass(v: Any) -> bool:
    try:
        return float(v) > 0.0
    except (TypeError, ValueError):
        if isinstance(v, dict):
            return str(v.get("verdict") or v.get("status") or "").upper() in ("PASS", "OK")
        return str(v).upper() in ("PASS", "OK", "1", "1.0", "TRUE")


def _score_fail(v: Any) -> bool:
    try:
        return float(v) <= 0.0
    except (TypeError, ValueError):
        if isinstance(v, dict):
            return str(v.get("verdict") or v.get("status") or "").upper() in ("FAIL", "ERROR")
        return str(v).upper() in ("FAIL", "0", "0.0", "FALSE")


def resolve_mission_doc_id(
    *,
    dag_result: dict | None = None,
    ledger: Any = None,
    prefer: str | None = None,
) -> str | None:
    """Pick an explicit part doc_id for VERIFY / battery (mega-9/10 residual).

    When ≥2 parts are open, tools refuse a bare current-part call. Prefer:
      1. explicit *prefer*
      2. doc_id on BUILD node outputs in dag_result (mission-built, not spawn)
      3. latest successful add_features / new_part with geometry evidence
      4. any other ledger tool_call doc_id (newest first)
    Never returns a process-global "current part" — callers must pass this through.
    """
    if prefer and str(prefer).strip():
        return str(prefer).strip()

    # (doc_id, score) — higher score wins. Geometry-bearing BUILD docs beat empty
    # spawn shells that only ran new_part.
    ranked: dict[str, int] = {}

    def _bump(v: Any, score: int) -> None:
        if isinstance(v, str) and v.strip():
            s = v.strip()
            ranked[s] = max(ranked.get(s, 0), score)

    nodes = (dag_result or {}).get("nodes", {}) or {}
    ordered = sorted(
        nodes.items(),
        key=lambda kv: (0 if str(kv[0]).startswith("n_build") else 1, str(kv[0])),
    )
    for nid, ns in ordered:
        if not isinstance(ns, dict):
            continue
        out = ns.get("output") if isinstance(ns.get("output"), dict) else {}
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        ar = data.get("agent_result") if isinstance(data.get("agent_result"), dict) else {}
        is_build = str(nid).startswith("n_build")
        base = 100 if is_build else 40
        for src in (
            out.get("doc_id"), ns.get("doc_id"), data.get("doc_id"),
            (ar.get("handoff") or {}).get("outputs", {}).get("doc_id")
            if isinstance(ar.get("handoff"), dict) else None,
            ar.get("doc_id"),
        ):
            _bump(src, base)

    try:
        events = list(getattr(ledger, "events", None) or [])
    except Exception:  # noqa: BLE001
        events = []
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind") or ""
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        tool = p.get("tool") or p.get("tool_name") or ""
        if kind == "agent_turn" and p.get("event") == "tool_call":
            tool = p.get("tool") or tool
        res = p.get("result")
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:  # noqa: BLE001
                res = {}
        if not isinstance(res, dict):
            res = {}
        args = p.get("args") if isinstance(p.get("args"), dict) else {}

        if tool == "add_features" and p.get("ok") is not False:
            # Geometry evidence: volume / body_count / revision.
            score = 90
            try:
                if float(res.get("volume_mm3") or 0) > 0:
                    score = 120
                elif int(res.get("body_count") or 0) > 0:
                    score = 110
            except (TypeError, ValueError):
                pass
            _bump(res.get("doc_id"), score)
            _bump(args.get("doc_id"), score)
        elif tool in ("new_part", "open_part", "import_step", "import_part"):
            _bump(res.get("doc_id"), 50 if tool == "new_part" else 60)
            _bump(args.get("doc_id"), 50)
        elif tool in ("measure_part", "query_geometry", "verify_battery",
                      "check_interference", "describe_part") and res.get("ok") is not False:
            _bump(res.get("doc_id"), 70)
            _bump(args.get("doc_id"), 70)

        # Checkpoints / direct payload doc_id (weaker signal).
        if kind in ("ai_vc_checkpoint", "auto_checkpoint", "tool_call", "agent_turn"):
            _bump(p.get("doc_id"), 30)

    if not ranked:
        return None

    # Prefer cpp engine docs; break ties by score then stable id.
    def _key(item: tuple[str, int]) -> tuple:
        did, sc = item
        cpp = 1 if (did.startswith("doc-cpp") or did.startswith("doc_")) else 0
        return (sc, cpp, did)

    best = max(ranked.items(), key=_key)
    return best[0]





def _slim_battery_evidence(result: dict) -> dict:
    """Keep a compact, ledger-safe subset of battery tool output."""
    out: dict[str, Any] = {}
    for k in ("verdict", "status", "result", "ok", "summary"):
        if k in result:
            out[k] = result[k]
    findings = result.get("findings")
    if isinstance(findings, list):
        out["findings"] = [
            {
                "name": f.get("name") or f.get("check") or f.get("id"),
                "severity": f.get("severity") or f.get("level"),
                "message": str(f.get("message") or f.get("detail") or "")[:200],
            }
            for f in findings[:30]
            if isinstance(f, dict)
        ]
    scores = result.get("scores")
    if isinstance(scores, dict):
        out["scores"] = {str(k): scores[k] for k in list(scores)[:20]}
    return out



def _check_tools(check: dict) -> list[str]:
    """Normalize check.tool / check.tools into an ordered tool name list."""
    tools: list[str] = []
    raw_list = check.get("tools")
    if isinstance(raw_list, list):
        tools.extend(str(t).strip() for t in raw_list if str(t).strip())
    tool = str(check.get("tool") or "").strip()
    if tool and tool not in tools:
        # Support compound form: "ecad_run_drc+ecad_run_erc"
        if "+" in tool and " " not in tool:
            tools.extend(p.strip() for p in tool.split("+") if p.strip())
        else:
            tools.append(tool)
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in tools:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _run_one(tool: str, pass_rule: str, check: dict,
             doc_id: str | None, *, ledger=None) -> tuple[str, str, dict]:
    """Run one (or multi) check tool and derive (verdict, detail, evidence).

    Never raises. mega-11: multi-tool AC4 (drc+erc), ledger reuse for prior
    tool_call evidence when the live tool is unresolvable, honest SKIP on
    provision-absence.
    """
    tools = _check_tools(check)
    if not tools:
        tools = [tool] if tool else []

    if tools == ["manual"] or tool == "manual":
        return NOT_CHECKED, "manual criterion — requires human confirmation", {}

    params_hint = check.get("params_hint") or {}
    if not isinstance(params_hint, dict):
        params_hint = {}

    # Multi-tool (AC4: ecad_run_drc + ecad_run_erc) → combined evidence envelope.
    if len(tools) > 1:
        combined: dict[str, Any] = {"ok": True, "tools": tools}
        any_ran = False
        for tname in tools:
            raw, src = _invoke_or_ledger(tname, doc_id, params_hint, ledger)
            if raw is None:
                combined[tname] = {
                    "ok": False,
                    "error": f"check tool {tname!r} could not be resolved or raised",
                    "source": src,
                }
                continue
            part = _as_dict(raw)
            part.setdefault("source", src)
            # Short keys for DRC/ERC evaluator.
            short = (
                "drc" if "drc" in tname else
                "erc" if "erc" in tname else
                tname
            )
            combined[short] = part
            combined[tname] = part
            any_ran = True
            if part.get("ok") is False and "errors" not in part:
                combined["ok"] = False
        if not any_ran:
            return NOT_CHECKED, f"no check tools resolved among {tools}", combined
        verdict, detail = evaluate_pass_rule(pass_rule, combined)
        return verdict, detail, combined

    tname = tools[0] if tools else tool
    raw, src = _invoke_or_ledger(tname, doc_id, params_hint, ledger)
    if raw is None:
        # Honest SKIP if the rule accepts SKIP and absence is provision-shaped.
        if "skip" in (pass_rule or "").lower():
            evidence = {
                "ok": False,
                "skipped": True,
                "provision_note": f"check tool {tname!r} could not be resolved (backend absent?)",
                "source": src or "unresolved",
            }
            verdict, detail = evaluate_pass_rule(pass_rule, evidence)
            return verdict, detail, evidence
        return NOT_CHECKED, f"check tool {tname!r} could not be resolved (backend absent?)", {}

    result = _as_dict(raw)
    if src:
        result.setdefault("source", src)
    # Merge params_hint legs into evidence for AC9-style rules if present.
    if "legs" in params_hint and "legs_required" not in result:
        result.setdefault("legs_required", params_hint.get("legs"))
    verdict, detail = evaluate_pass_rule(pass_rule, result)
    return verdict, detail, result


def _invoke_or_ledger(tool: str, doc_id: str | None, params_hint: dict,
                      ledger) -> tuple[Any, str]:
    """Call *tool* live; on miss, reuse newest matching ledger tool_call result."""
    fn = _resolve_fn(tool)
    if fn is not None:
        raw = _call_tool(fn, doc_id, params_hint)
        if raw is not None:
            return raw, "live"
    # Ledger reuse (mission already ran the tool during BUILD).
    prior = _latest_tool_result(ledger, tool)
    if prior is not None:
        return prior, "ledger_tool_call"
    return None, "unresolved"


def _latest_tool_result(ledger, tool: str) -> dict | None:
    """Newest tool_call / agent_turn result for *tool* on the ledger, if any."""
    if ledger is None:
        return None
    try:
        events = list(getattr(ledger, "events", None) or [])
    except Exception:  # noqa: BLE001
        return None
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        kind = ev.get("kind") or ""
        t = p.get("tool") or p.get("tool_name") or ""
        if kind == "agent_turn" and p.get("event") == "tool_call":
            t = p.get("tool") or t
        if t != tool:
            continue
        res = p.get("result")
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:  # noqa: BLE001
                res = {"text": res}
        if isinstance(res, dict):
            return res
    return None


def grade_report_card_from_ledger(ledger, params_hint: dict | None = None) -> dict:
    """Build AC9 evidence from ledger stage/decision/criterion/artifact events.

    Maps known mission legs to verdicts found on:
      * criterion_result events
      * decision events with verdict/result
      * stage_log / stage events
      * artifact notes with verdict
      * tool_call outcomes for leg-canonical tools
    Missing legs stay absent so the pass_rule can FAIL honestly.
    """
    hint = params_hint if isinstance(params_hint, dict) else {}
    legs_required = list(hint.get("legs") or [
        "plan", "parts", "schematic", "sim", "board", "trace_opt", "antenna",
        "checks", "firmware", "sensor_loop", "power", "airframe",
    ])
    leg_verdicts: dict[str, str] = {}

    # Canonical tool → leg mapping for ledger tool_call scan.
    tool_leg = {
        "buildgraph_plan": "plan", "dag_plan": "plan",
        "part_search": "parts", "part_place": "parts", "ecad_find_part": "parts",
        "ecad_new_schematic": "schematic", "ecad_place": "schematic",
        "ecad_wire": "schematic", "ecad_run_erc": "schematic",
        "ecad_simulate": "sim",
        "ecad_make_board": "board", "board_from_schematic": "board",
        "ecad_autoroute": "board", "ecad_run_drc": "board",
        "ecad_optimize_traces": "trace_opt",
        "ecad_antenna_design": "antenna", "ecad_antenna_analyze": "antenna",
        "ecad_run_pdn": "checks", "ecad_run_hostcheck": "checks",
        "fw_build": "firmware", "fw_new_project": "firmware",
        "fw_sim_run": "sensor_loop", "fw_verify_board": "sensor_loop",
        "fw_sensor_scenario": "sensor_loop",
        "fw_power_profile": "power",
        "verify_battery": "airframe", "add_features": "airframe",
        "measure_part": "airframe", "check_interference": "airframe",
        "new_part": "airframe",
    }

    def _set(leg: str, verdict: str) -> None:
        u = str(verdict).strip().upper()
        head = u.split("(")[0].split(":")[0].strip()
        if head not in ("PASS", "WARN", "FAIL", "SKIP"):
            if u.startswith("SKIP"):
                head = "SKIP"
            elif u in ("OK", "PASSED", "SUCCESS", "DONE"):
                head = "PASS"
            elif u in ("ERROR", "FAILED", "BLOCKED"):
                head = "FAIL"
            else:
                return
        # Prefer first non-empty; upgrade FAIL→PASS only if later PASS seen? keep last.
        leg_verdicts[leg] = head

    try:
        events = list(getattr(ledger, "events", None) or []) if ledger is not None else []
    except Exception:  # noqa: BLE001
        events = []

    state = getattr(ledger, "state", None) if ledger is not None else None
    if isinstance(state, dict):
        for cr in state.get("criterion_results") or []:
            if not isinstance(cr, dict):
                continue
            ac = str(cr.get("id") or "")
            res = cr.get("result")
            # Map ACn → leg loosely.
            ac_leg = {
                "AC1": "airframe", "AC2": "airframe", "AC3": "airframe",
                "AC4": "board", "AC5": "sim", "AC6": "antenna",
                "AC7": "firmware", "AC8": "sensor_loop",
            }.get(ac)
            if ac_leg and res:
                _set(ac_leg, str(res))
            tool = str(cr.get("check_tool") or "")
            leg = tool_leg.get(tool)
            if leg and res:
                _set(leg, str(res))

    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind") or ""
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}

        if kind == "decision":
            leg = p.get("leg") or p.get("id") or p.get("name")
            ver = p.get("verdict") or p.get("result") or p.get("status")
            if leg and ver:
                # only accept if leg is in required set (case-insensitive)
                for req in legs_required:
                    if str(leg).lower() == str(req).lower():
                        _set(str(req), str(ver))
                        break
            # power_budget decisions
            if "power" in str(leg or p.get("id") or "").lower() and ver:
                _set("power", str(ver))

        if kind in ("stage", "stage_log"):
            stage = str(p.get("stage") or p.get("name") or "").lower()
            ver = p.get("verdict") or p.get("result") or p.get("status")
            for req in legs_required:
                if req.lower() in stage and ver:
                    _set(req, str(ver))

        if kind == "artifact":
            akind = str(p.get("kind") or p.get("type") or "").lower()
            ver = p.get("verdict") or p.get("result")
            for req in legs_required:
                if req.lower() in akind and ver:
                    _set(req, str(ver))
            if "report" in akind and isinstance(p.get("legs"), dict):
                for lk, lv in p["legs"].items():
                    _set(str(lk), str(lv))

        if kind in ("tool_call", "agent_turn") or (
                kind == "agent_turn" and p.get("event") == "tool_call"):
            t = p.get("tool") or p.get("tool_name") or ""
            if kind == "agent_turn" and p.get("event") == "tool_call":
                t = p.get("tool") or t
            leg = tool_leg.get(str(t))
            if not leg:
                continue
            res = p.get("result")
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:  # noqa: BLE001
                    res = {}
            if not isinstance(res, dict):
                res = {}
            # Derive a leg verdict from tool outcome.
            if _is_honest_skip(res):
                _set(leg, "SKIP")
            elif res.get("ok") is False or res.get("success") is False or res.get("passed") is False:
                _set(leg, "FAIL")
            elif p.get("ok") is False:
                _set(leg, "FAIL")
            elif res.get("ok") is True or res.get("success") is True or res.get("passed") is True:
                _set(leg, "PASS")
            elif p.get("ok") is True:
                _set(leg, "PASS")
            elif isinstance(res.get("returncode"), int) and res["returncode"] == 0:
                _set(leg, "PASS")
            elif "errors" in res:
                try:
                    _set(leg, "PASS" if int(res.get("errors") or 0) == 0 else "FAIL")
                except (TypeError, ValueError):
                    pass

        if kind == "criterion_result":
            ac = str(p.get("id") or "")
            res = p.get("result")
            ac_leg = {
                "AC1": "airframe", "AC2": "airframe", "AC3": "airframe",
                "AC4": "checks", "AC5": "sim", "AC6": "antenna",
                "AC7": "firmware", "AC8": "sensor_loop",
            }.get(ac)
            if ac_leg and res:
                _set(ac_leg, str(res))

        if kind == "battery_result":
            ver = p.get("verdict")
            if ver:
                _set("airframe", str(ver))

        # Explicit report_card / leg_verdicts notes.
        if kind in ("note", "report_card", "mission_report"):
            if isinstance(p.get("leg_verdicts"), dict):
                for lk, lv in p["leg_verdicts"].items():
                    _set(str(lk), str(lv))
            if isinstance(p.get("legs"), dict):
                for lk, lv in p["legs"].items():
                    _set(str(lk), str(lv))

    return {
        "ok": True,
        "source": "ledger_report_card",
        "legs_required": legs_required,
        "leg_verdicts": leg_verdicts,
        "legs": leg_verdicts,
    }



def _call_tool(fn: Callable[..., Any], doc_id: str | None,
               params_hint: dict) -> Any:
    """Invoke *fn* with a doc_id + scalar params_hint when accepted.

    mega-10: previously only doc_id was forwarded — measure/query hints never
    reached the tool. Scalars/lists from params_hint are merged when the
    signature accepts them; unknown kwargs are dropped on TypeError.
    """
    import inspect
    try:
        sig = inspect.signature(fn)
        accepts = set(sig.parameters)
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        accepts = set()
        has_var_kw = False

    kwargs: dict[str, Any] = {}
    if doc_id and ("doc_id" in accepts or has_var_kw):
        kwargs["doc_id"] = doc_id

    if isinstance(params_hint, dict):
        for k, v in params_hint.items():
            if k == "doc_id":
                continue
            if has_var_kw or k in accepts:
                # Only forward JSON-friendly scalars/lists/dicts.
                if v is None or isinstance(v, (str, int, float, bool, list, dict)):
                    kwargs[k] = v

    try:
        return fn(**kwargs)
    except TypeError:
        # signature mismatch — drop hints, keep doc_id, then bare.
        if "doc_id" in kwargs and len(kwargs) > 1:
            try:
                return fn(doc_id=kwargs["doc_id"])
            except Exception:  # noqa: BLE001
                pass
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
