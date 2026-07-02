"""Verify battery — deterministic check dispatcher for RL reward, CI gate, design_review.

verify(doc_id, checks, **kwargs) → {"scores": {name: float}, "findings": [...]}

The battery is a THIN DISPATCHER — zero reimplementation of check logic. It:
  1. Builds a private FastMCP + registers all tool modules once (cached singleton)
  2. Extracts inner .fn references for direct sync calls (no async, no MCP-over-wire)
  3. Calls the requested subset, parses JSON returns, extracts a scalar score
  4. Returns deterministic scores + findings list (sorted by check name for stability)

Per-check metadata:
  {name, cost: "subsecond"|"seconds"|"minutes", platform: "any"|"posix",
   reward_shape: "binary"|"scalar"|"findings"}
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Check registry
# Tuple: (module_key, fn_name, cost, platform, reward_shape)
# ---------------------------------------------------------------------------
CHECKS: dict[str, tuple[str, str, str, str, str]] = {
    # --- geometry / sketch
    "check_sketch":              ("cad",  "check_sketch",              "subsecond", "any",   "binary"),
    "measure_part":              ("cad",  "measure_part",              "subsecond", "any",   "scalar"),
    "query_geometry":            ("cad",  "query_geometry",            "subsecond", "any",   "findings"),
    "measure_between":           ("cad",  "measure_between",           "subsecond", "any",   "scalar"),
    # cost classes follow docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md §4.1/4.2
    "check_interference":        ("cad",  "check_interference",        "seconds",   "any",   "binary"),
    "check_feature_overlap":     ("cad",  "check_feature_overlap",     "seconds",   "any",   "binary"),
    "check_symmetry":            ("cad",  "check_symmetry",            "seconds",   "any",   "binary"),
    "wall_thickness":            ("cad",  "wall_thickness",            "seconds",   "any",   "findings"),
    "draft_analysis":            ("cad",  "draft_analysis",            "seconds",   "any",   "findings"),
    "solve_mates":               ("cad",  "solve_mates",               "seconds",   "any",   "binary"),
    "review_drawing":            ("cad",  "review_drawing",            "seconds",   "any",   "binary"),
    # --- simulation
    "run_fea_static":            ("cad",  "run_fea_static",            "minutes",   "any",   "scalar"),
    "run_fea_modal":             ("cad",  "run_fea_modal",             "minutes",   "any",   "scalar"),
    "run_cfd_flow":              ("cad",  "run_cfd_flow",              "minutes",   "any",   "scalar"),
    "run_cfd_accurate":          ("cad",  "run_cfd_accurate",          "minutes",   "posix", "scalar"),
    # --- recon
    "compare_parts":             ("recon","compare_parts",             "seconds",   "any",   "scalar"),
    # --- ecad
    "ecad_run_erc":              ("ecad", "ecad_run_erc",              "subsecond", "any",   "binary"),
    "ecad_simulate":             ("ecad", "ecad_simulate",             "seconds",   "any",   "scalar"),
    "ecad_run_drc":              ("ecad", "ecad_run_drc",              "seconds",   "any",   "binary"),
    "ecad_run_dfm":              ("ecad", "ecad_run_dfm",              "seconds",   "any",   "findings"),
    "ecad_check_impedance":      ("ecad", "ecad_check_impedance",      "subsecond", "any",   "scalar"),
    "ecad_check_diff_impedance": ("ecad", "ecad_check_diff_impedance", "subsecond", "any",   "scalar"),
    # --- cam
    "cam_simulate":              ("cam",  "cam_simulate",              "seconds",   "any",   "scalar"),
    # --- gap verifiers (implemented directly in this module)
    "is_manifold":               ("gap",  "is_manifold",               "subsecond", "any",   "binary"),
    "round_trip":                ("gap",  "round_trip",                "seconds",   "any",   "scalar"),
    "groundedness":              ("gap",  "groundedness",              "subsecond", "any",   "scalar"),
    "unit_sanity":               ("gap",  "unit_sanity",               "subsecond", "any",   "findings"),
    "dim_coverage":              ("gap",  "dim_coverage",              "subsecond", "any",   "scalar"),
    "req_join":                  ("gap",  "req_join",                  "subsecond", "any",   "scalar"),
    "eval_check":                ("gap",  "eval_check_direct",         "subsecond", "any",   "binary"),
}

# ---------------------------------------------------------------------------
# Private MCP singleton — one FastMCP per module_key, built on first use
# ---------------------------------------------------------------------------
_PRIVATE_MCP: dict[str, Any] = {}  # module_key → FastMCP instance (built once per process)


def _get_fn(module_key: str, fn_name: str):
    """Return the raw Python function for a tool registered on the private MCP."""
    if module_key not in _PRIVATE_MCP:
        from mcp.server.fastmcp import FastMCP
        _mcp = FastMCP(f"battery-{module_key}")
        if module_key == "cad":
            from mcp_server.cad_tools import register_cad_tools
            register_cad_tools(_mcp)
        elif module_key == "recon":
            from mcp_server.recon_tools import register_recon_tools
            register_recon_tools(_mcp)
        elif module_key == "ecad":
            from mcp_server.ecad_tools import register_ecad_tools
            register_ecad_tools(_mcp)
        elif module_key == "cam":
            from mcp_server.cam_tools import register_cam_tools
            register_cam_tools(_mcp)
        _PRIVATE_MCP[module_key] = _mcp
    mcp = _PRIVATE_MCP[module_key]
    tool = mcp._tool_manager._tools.get(fn_name)
    if tool is None:
        raise KeyError(f"Tool {fn_name!r} not found in {module_key} module")
    return tool.fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cur_doc() -> str:
    from mcp_server.cad_tools import _CUR
    d = _CUR.get("doc_id")
    if not d:
        raise ValueError("No current document")
    return d


# ---------------------------------------------------------------------------
# Gap verifiers — implemented directly here, not via private MCP
# ---------------------------------------------------------------------------

def _gap_is_manifold(doc_id: str = None, **_) -> str:
    """Check: free_edges == 0 (manifold solid)."""
    from mcp_server import core_bridge as cb
    from mcp_server.core_bridge import CoreError
    d = doc_id or _cur_doc()
    try:
        r = cb.call("eval_check", {"doc_id": d})
        fe = r.get("free_edges", 0)
        tiny = r.get("tiny_edges", 0)
        ok = fe == 0
        return json.dumps({
            "ok": ok,
            "free_edges": fe,
            "tiny_edges": tiny,
            "summary": "manifold" if ok else f"{fe} free edge(s) — not watertight",
        })
    except CoreError as e:
        return json.dumps({"ok": False, "error": str(e)})


def _gap_eval_check_direct(doc_id: str = None, **_) -> str:
    """Direct eval_check exposure (BRepCheck + free/tiny-edge census)."""
    from mcp_server import core_bridge as cb
    from mcp_server.core_bridge import CoreError
    d = doc_id or _cur_doc()
    try:
        r = cb.call("eval_check", {"doc_id": d})
        ok = r.get("free_edges", 0) == 0 and r.get("invalid_faces", 0) == 0
        return json.dumps({"ok": ok, "doc_id": d, **r})
    except CoreError as e:
        return json.dumps({"ok": False, "error": str(e)})


def _gap_round_trip(doc_id: str = None, threshold: float = 99.0, **_) -> str:
    """Export STEP → re-import → compare_parts. match_pct >= threshold + eval_check clean."""
    import tempfile
    import os
    from mcp_server import core_bridge as cb
    d = doc_id or _cur_doc()
    step = tempfile.mktemp(suffix=".step")  # noqa: S306 — same pattern as cad_tools._tmp
    try:
        cb.call("doc_export_step", {"doc_id": d, "path": step})
        # doc_open is the engine's import path (same op import_part / recover_parts use)
        r_imp = cb.call("doc_open", {"path": step})
        b_id = r_imp.get("doc_id")
        # compare via compare_parts fn
        compare_fn = _get_fn("recon", "compare_parts")
        cmp_json = compare_fn(doc_a=d, doc_b=b_id)
        cmp = json.loads(cmp_json)
        match_pct = cmp.get("match_pct", 0.0)
        chk = cb.call("eval_check", {"doc_id": b_id})
        clean = chk.get("free_edges", 0) == 0
        ok = match_pct >= threshold and clean
        cb.call("doc_close", {"doc_id": b_id})
        return json.dumps({
            "ok": ok,
            "match_pct": match_pct,
            "eval_check_clean": clean,
            "threshold": threshold,
        })
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)})
    finally:
        try:
            os.unlink(step)
        except OSError:
            pass


def _gap_groundedness(model_text: str = "", doc_id: str = None, **_) -> str:
    """Slot-match numerals in model_text vs the doc's state serialization.

    State = the feature tree (parameter values the text is most likely to cite:
    lengths, diameters, counts) merged with the mass properties (volume, mass,
    com). Unmentioned state slots are never penalized, so the breadth is safe.
    """
    from mcp_server import core_bridge as cb
    d = doc_id or _cur_doc()
    state: dict = {}
    try:
        state["tree"] = cb.call("doc_tree", {"doc_id": d})
    except Exception:  # noqa: BLE001
        pass
    try:
        state["mass"] = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 7.85})
    except Exception:  # noqa: BLE001
        pass
    from tools.translator.groundedness import check_groundedness
    result = check_groundedness(model_text, state)
    ok = not result.get("spurious")  # any ungrounded numeral fails the gate (IP-7)
    summary = (f"{len(result.get('matched', []))} grounded, "
               f"{len(result.get('spurious', []))} spurious numeral(s)")
    return json.dumps({"ok": ok, "summary": summary, **result})


def _gap_unit_sanity(doc_id: str = None, **_) -> str:
    """Range-check all numeric quantities in the doc's mass properties."""
    from mcp_server import core_bridge as cb
    from services.verify.unit_ranges import check_unit_ranges
    d = doc_id or _cur_doc()
    try:
        state = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 7.85})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})
    violations = check_unit_ranges(state)
    return json.dumps({"ok": len(violations) == 0, "violations": violations})


def _gap_dim_coverage(doc_id: str = None, source_doc_id: str = None, **_) -> str:
    """Drawing dimension coverage — annotation count vs the source part's feature count.

    doc_id must be a DRAWING document (from make_drawing). Annotations live per-view
    in drawing_get's sheets->views JSON (core/doc/drawing_session.cpp). When the
    source part is known (source_doc_id, or recorded in the drawing), coverage =
    dims / features capped at 1.0; otherwise it degrades to "has any dims at all".
    """
    from mcp_server import core_bridge as cb
    d = doc_id or _cur_doc()
    try:
        full = cb.call("drawing_get", {"doc_id": d})
        n_dims = 0
        n_views = 0
        for sh in full.get("sheets", []) or []:
            for v in sh.get("views", []) or []:
                n_views += 1
                anns = v.get("annotations")
                if isinstance(anns, list):
                    n_dims += len(anns)
        n_features = None
        src = source_doc_id or full.get("source_doc_id") or full.get("source")
        if src:
            try:
                tree = cb.call("doc_tree", {"doc_id": src})
                feats = (tree.get("tree") or {}).get("features", [])
                n_features = len(feats)
            except Exception:  # noqa: BLE001 — source may have been closed
                n_features = None
        if n_features:
            ratio = min(n_dims / n_features, 1.0)
            ok = ratio >= 0.5  # at least half the features carry a dimension
        else:
            ratio = 1.0 if n_dims > 0 else 0.0
            ok = n_dims > 0
        return json.dumps({
            "ok": ok,
            "score": round(ratio, 4),
            "n_dims": n_dims,
            "n_views": n_views,
            "n_features": n_features,
        })
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)})


def _gap_req_join(doc_id: str = None, requirements: list = None, **_) -> str:
    """Join requirement statuses vs verifier evidence: what fraction have passing evidence?"""
    if not requirements:
        return json.dumps({"ok": True, "score": 1.0, "note": "no requirements provided"})
    passed = sum(1 for r in requirements if r.get("status") in ("pass", "verified", "ok"))
    total = len(requirements)
    score = passed / total
    return json.dumps({
        "ok": score >= 1.0,
        "score": score,
        "passed": passed,
        "total": total,
        "unmet": [r.get("id", i) for i, r in enumerate(requirements)
                  if r.get("status") not in ("pass", "verified", "ok")],
    })


GAP_FNS: dict[str, Any] = {
    "is_manifold":       _gap_is_manifold,
    "round_trip":        _gap_round_trip,
    "groundedness":      _gap_groundedness,
    "unit_sanity":       _gap_unit_sanity,
    "dim_coverage":      _gap_dim_coverage,
    "req_join":          _gap_req_join,
    "eval_check_direct": _gap_eval_check_direct,
}


# ---------------------------------------------------------------------------
# Score extraction from JSON return
# ---------------------------------------------------------------------------

def _extract_score(check_name: str, reward_shape: str, result_json: str) -> tuple[float, list]:
    """Parse JSON return from a check function → (score 0..1, findings list)."""
    try:
        r = json.loads(result_json) if isinstance(result_json, str) else {}
    except Exception:
        return 0.0, [{"check": check_name, "error": "parse_error", "raw": str(result_json)[:200]}]

    findings: list[dict] = []

    if reward_shape == "binary":
        score = 1.0 if r.get("ok", False) else 0.0
        if not r.get("ok", False):
            findings.append({
                "check": check_name,
                "ok": False,
                "detail": r.get("summary") or r.get("error") or r.get("verdict") or "",
            })

    elif reward_shape == "scalar":
        score = 0.0  # default
        found = False
        for key in ("match_pct", "score", "min_fos", "cd", "max_von_mises_mpa",
                    "coverage_ratio"):
            if key in r:
                val = r[key]
                if key == "match_pct":
                    score = min(float(val) / 100.0, 1.0)
                elif key == "min_fos":
                    score = min(float(val) / 2.0, 1.0)  # FoS 2+ → 1.0
                elif key == "max_von_mises_mpa":
                    score = 1.0 if r.get("ok", True) else 0.0
                else:
                    fval = float(val)
                    if 0.0 <= fval <= 1.0:
                        score = fval
                    else:
                        score = min(abs(fval) / 100.0, 1.0)
                found = True
                break
        if not found:
            score = 1.0 if r.get("ok", False) else 0.0
        if not r.get("ok", True):
            findings.append({
                "check": check_name,
                "ok": False,
                "detail": r.get("summary") or r.get("error") or "",
            })

    else:  # "findings"
        items = (r.get("pairs") or r.get("faces_below_threshold") or
                 r.get("violations") or r.get("spurious") or [])
        score = 1.0 if not items else max(0.0, 1.0 - len(items) * 0.1)
        for item in items:
            findings.append({"check": check_name, "ok": False, "detail": str(item)})

    return score, findings


# ---------------------------------------------------------------------------
# Main verify() function
# ---------------------------------------------------------------------------

def verify(doc_id: Optional[str], checks: list[str], **kwargs) -> dict:
    """Run a subset of checks and return scores + findings.

    Args:
        doc_id: the document to check (None = use current doc)
        checks: list of check names from CHECKS registry; empty = run all subsecond/any checks
        **kwargs: forwarded to each check function (e.g. fixtures, loads for FEA)

    Returns:
        {
          "scores": {check_name: float 0..1, ...},   # deterministic order (sorted)
          "findings": [{check, ok, detail}, ...],    # sorted by check name
          "elapsed_ms": float,
          "skipped": [check_name, ...],  # platform-filtered checks
        }
    """
    import inspect

    t0 = time.monotonic()
    is_posix = sys.platform != "win32"

    if not checks:
        checks = [n for n, meta in CHECKS.items()
                  if meta[2] == "subsecond" and (meta[3] == "any" or is_posix)]

    scores: dict[str, float] = {}
    findings: list[dict] = []
    skipped: list[str] = []

    for name in sorted(checks):  # deterministic order
        if name not in CHECKS:
            findings.append({
                "check": name,
                "ok": False,
                "detail": f"unknown check {name!r}",
            })
            scores[name] = 0.0
            continue

        module_key, fn_name, cost, platform_req, reward_shape = CHECKS[name]

        if platform_req == "posix" and not is_posix:
            skipped.append(name)
            continue

        try:
            if module_key == "gap":
                fn = GAP_FNS[fn_name]
                call_kwargs: dict[str, Any] = dict(doc_id=doc_id, **kwargs)
                result = fn(**call_kwargs)
            else:
                fn = _get_fn(module_key, fn_name)
                call_kwargs = {}
                if doc_id is not None:
                    call_kwargs["doc_id"] = doc_id
                # Merge relevant kwargs by inspecting fn signature
                sig_params = set(inspect.signature(fn).parameters.keys())
                for k, v in kwargs.items():
                    if k in sig_params:
                        call_kwargs[k] = v
                result = fn(**call_kwargs)
        except Exception as e:
            result = json.dumps({"ok": False, "error": str(e)})

        score, flist = _extract_score(name, reward_shape, result)
        scores[name] = score
        findings.extend(flist)

    elapsed = (time.monotonic() - t0) * 1000
    return {
        "scores": dict(sorted(scores.items())),
        "findings": sorted(findings, key=lambda f: f.get("check", "")),
        "elapsed_ms": round(elapsed, 1),
        "skipped": sorted(skipped),
    }


def check_metadata() -> list[dict]:
    """Return per-check metadata table (for RL loop, CI config)."""
    return [
        {
            "name": name,
            "cost": meta[2],
            "platform": meta[3],
            "reward_shape": meta[4],
        }
        for name, meta in sorted(CHECKS.items())
    ]
