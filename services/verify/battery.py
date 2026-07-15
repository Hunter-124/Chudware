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
# mega-15: argument adapters for checks that require non-doc_id args.
# Unfulfillable checks honest-SKIP (omit from scores) — never signature-zero.
# A check that CAN run MUST run with derived args (never weaken by skipping).
# ---------------------------------------------------------------------------

def _skip_json(check_name: str, reason: str) -> str:
    """Honest SKIP envelope — not a failure; battery omits from scores."""
    return json.dumps({
        "ok": True,
        "skipped": True,
        "skip_reason": reason,
        "check": check_name,
        "score": None,
        "summary": f"SKIP: {reason}",
    })


def _parse_jsonish(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _doc_features(doc_id: Optional[str]) -> list[dict]:
    """Best-effort feature list from doc_tree (empty on miss)."""
    if not doc_id:
        try:
            doc_id = _cur_doc()
        except Exception:  # noqa: BLE001
            return []
    try:
        from mcp_server import core_bridge as cb  # noqa: PLC0415
        tree = cb.call("doc_tree", {"doc_id": doc_id}) or {}
        feats = ((tree.get("tree") or {}).get("features")) or []
        return [f for f in feats if isinstance(f, dict)]
    except Exception:  # noqa: BLE001
        return []


def _sketch_ids(doc_id: Optional[str]) -> list[str]:
    """Sketch feature ids on *doc_id* (2D sketches only)."""
    out: list[str] = []
    for f in _doc_features(doc_id):
        ftype = str(f.get("type") or f.get("kind") or "").lower()
        fid = f.get("id") or f.get("name")
        if not fid:
            continue
        if ftype in ("sketch", "sketch2d") or (
            ftype.startswith("sketch") and "3d" not in ftype
        ):
            out.append(str(fid))
    return out


def _is_drawing_doc(doc_id: Optional[str]) -> bool:
    """True when drawing_get succeeds for *doc_id* (has sheets/views)."""
    if not doc_id:
        return False
    try:
        from mcp_server import core_bridge as cb  # noqa: PLC0415
        full = cb.call("drawing_get", {"doc_id": doc_id}) or {}
        if full.get("_is_error") or full.get("error"):
            return False
        sheets = full.get("sheets")
        return isinstance(sheets, list)
    except Exception:  # noqa: BLE001
        return False


def _find_ecad_board_doc(preferred: Optional[str] = None) -> Optional[str]:
    """Locate an open PCB session doc_id for impedance checks."""
    try:
        from engine.ecad import session as esess  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        esess = None
    if preferred and esess is not None:
        try:
            s = esess.get_session(preferred)
            if s is not None and getattr(s, "kind", None) in ("pcb", "board", None):
                doc = getattr(s, "doc", None) or {}
                if isinstance(doc, dict) and (
                    doc.get("tracks") is not None
                    or doc.get("footprints") is not None
                    or str(getattr(s, "kind", "")).lower() in ("pcb", "board")
                ):
                    return preferred
        except Exception:  # noqa: BLE001
            pass
    if esess is None:
        return preferred
    try:
        for s in esess.iter_sessions():
            kind = str(getattr(s, "kind", "") or "").lower()
            doc = getattr(s, "doc", None) or {}
            did = getattr(s, "doc_id", None)
            if not did:
                continue
            if kind in ("pcb", "board"):
                return did
            if isinstance(doc, dict) and (
                doc.get("tracks") is not None or doc.get("footprints") is not None
            ):
                return did
    except Exception:  # noqa: BLE001
        pass
    return None


def _board_routed_nets(board_doc_id: str) -> list[str]:
    """Net names that have routed tracks on *board_doc_id*."""
    try:
        from engine.ecad import session as esess  # noqa: PLC0415
        s = esess.get_session(board_doc_id)
        doc = getattr(s, "doc", None) or {}
        tracks = doc.get("tracks") or []
        nets: list[str] = []
        for tr in tracks:
            if not isinstance(tr, dict):
                continue
            n = tr.get("net")
            if isinstance(n, str) and n and n not in nets:
                nets.append(n)
        return nets
    except Exception:  # noqa: BLE001
        return []


def _diff_pair_nets(nets: list[str]) -> Optional[tuple[str, str]]:
    """Heuristic differential pair from net names (CLK_P/CLK_N, USB_D+/USB_D-)."""
    lower = {n.lower(): n for n in nets}
    for n in nets:
        nl = n.lower()
        cand = None
        if nl.endswith("_p"):
            cand = lower.get(nl[:-2] + "_n")
        elif nl.endswith("+"):
            cand = lower.get(nl[:-1] + "-") or lower.get(nl[:-1] + "_n")
        elif "_p" in nl:
            cand = lower.get(nl.replace("_p", "_n"))
        if cand and cand != n:
            return n, cand
    return None


def _adapt_check_kwargs(
    check_name: str,
    fn: Any,
    doc_id: Optional[str],
    kwargs: dict,
) -> tuple[Optional[dict], Optional[str]]:
    """Return (call_kwargs, None) or (None, skip_json) for unfulfillable checks.

    Fills required args from the open doc / ECAD sessions when possible.
    """
    import inspect  # noqa: PLC0415

    sig_params = set(inspect.signature(fn).parameters.keys())
    call_kwargs: dict[str, Any] = {}
    if doc_id is not None and "doc_id" in sig_params:
        call_kwargs["doc_id"] = doc_id
    for k, v in kwargs.items():
        if k in sig_params:
            call_kwargs[k] = v

    if check_name == "check_sketch":
        if not call_kwargs.get("sketch"):
            sketches = _sketch_ids(doc_id)
            if not sketches:
                return None, _skip_json(
                    check_name, "no sketches on document"
                )
            call_kwargs["sketch"] = sketches[0]
        return call_kwargs, None

    if check_name == "measure_between":
        if call_kwargs.get("a_query") in (None, ""):
            # Prefer plain-word face pair; engine resolves side words.
            # Derive from features when present (solid exists → faces exist).
            feats = _doc_features(doc_id)
            if not feats:
                return None, _skip_json(
                    check_name, "no features to measure between"
                )
            call_kwargs["a_query"] = "top face"
            if call_kwargs.get("b_query") in (None, ""):
                call_kwargs["b_query"] = "bottom face"
        return call_kwargs, None

    if check_name == "ecad_check_impedance":
        board = call_kwargs.get("doc_id") or doc_id
        board = _find_ecad_board_doc(board if isinstance(board, str) else None)
        if not board:
            return None, _skip_json(
                check_name, "no open board for impedance check"
            )
        call_kwargs["doc_id"] = board
        nets = _board_routed_nets(board)
        if not nets:
            return None, _skip_json(
                check_name, "no routed nets on board for impedance"
            )
        # net_or_ref optional on tool (empty = all nets); leave empty for summary path
        # unless caller supplied one.
        if "net_or_ref" in sig_params and not call_kwargs.get("net_or_ref"):
            # Prefer a signal-ish net over GND/power when present.
            non_pwr = [
                n for n in nets
                if n.upper() not in ("GND", "VCC", "3V3", "+3V3", "5V", "+5V", "VDD")
            ]
            call_kwargs["net_or_ref"] = (non_pwr or nets)[0]
        return call_kwargs, None

    if check_name == "ecad_check_diff_impedance":
        board = call_kwargs.get("doc_id") or doc_id
        board = _find_ecad_board_doc(board if isinstance(board, str) else None)
        if not board:
            return None, _skip_json(
                check_name, "no open board for diff impedance"
            )
        call_kwargs["doc_id"] = board
        nets = _board_routed_nets(board)
        pair = None
        if call_kwargs.get("net_p") and call_kwargs.get("net_n"):
            pair = (call_kwargs["net_p"], call_kwargs["net_n"])
        else:
            pair = _diff_pair_nets(nets)
        if not pair:
            return None, _skip_json(
                check_name,
                "no differential pair nets on board (need net_p/net_n)",
            )
        call_kwargs["net_p"], call_kwargs["net_n"] = pair[0], pair[1]
        return call_kwargs, None

    if check_name == "ecad_run_erc":
        # Prefer schematic session when mechanical doc_id is wrong.
        if doc_id and "doc-cpp" in str(doc_id):
            try:
                from engine.ecad import session as esess  # noqa: PLC0415
                for s in esess.iter_sessions():
                    kind = str(getattr(s, "kind", "") or "").lower()
                    did = getattr(s, "doc_id", None)
                    if did and kind in ("sch", "schematic", ""):
                        doc = getattr(s, "doc", None) or {}
                        if isinstance(doc, dict) and (
                            doc.get("symbols") is not None
                            or kind in ("sch", "schematic")
                        ):
                            call_kwargs["doc_id"] = did
                            break
            except Exception:  # noqa: BLE001
                pass
        return call_kwargs, None

    return call_kwargs, None


# ---------------------------------------------------------------------------
# Canonical aicad.req/1 status constants (T-H3, engine_oracle/requirements).
# Imported here so _gap_req_join doesn't carry its own stub copies.
# ---------------------------------------------------------------------------
from engine_oracle.requirements.requirements import VALID_STATUSES as _REQ_VALID_STATUSES

# "waived" is the one aicad.req/1 status that exempts a requirement from scoring.
# Derived from the canonical set so any schema rename propagates automatically.
_WAIVED_STATUS: str = next(s for s in _REQ_VALID_STATUSES if s == "waived")

# The canonical "met" status from aicad.req/1 — derived so any ladder rename propagates.
_VERIFIED_STATUS: str = next(s for s in _REQ_VALID_STATUSES if s == "verified")

# The "met" set: canonical status + legacy aliases accepted for interoperability.
# "pass" and "ok" are NOT in VALID_STATUSES; they are explicit aliases kept here so
# older requirement documents don't silently fail.  The canonical anchor (_VERIFIED_STATUS)
# is derived from the ladder — if the schema renames "verified", this tuple tracks it.
# (T-BL23 DoD item 5, option (a).  Option (b) — drop aliases entirely — requires an
# advisor ruling; see work/questions/T-BL23.md.)
_REQ_MET_STATUSES: tuple[str, ...] = (_VERIFIED_STATUS, "pass", "ok")

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
    # T-BL49: design-time interface alignment (hole patterns, coaxiality, contact planes,
    # fastener pass-through) between mating components.  Implemented directly (gap) since it
    # lives in its own module; "findings" reward shape reads its `findings` list.
    "interface_check":           ("gap",  "interface_check",           "seconds",   "any",   "findings"),
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
    # T-BL50: layout-first critical-dim & interface coverage gate
    "layout_coverage":           ("gap",  "layout_coverage",           "subsecond", "any",   "scalar"),
    # T-FIN23: system power/energy budget (rail tree + duty cycle + battery verdict).
    # Runs only when a power_source kwarg is supplied; otherwise passes with a skip note.
    "power_budget":              ("gap",  "power_budget",              "subsecond", "any",   "findings"),
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
    """Check: body is a closed manifold solid (no free-edge or error-severity issues).

    Reads the ``issues`` list from eval_check (OCCT BRepCheck) rather than the bare
    ``free_edges`` scalar, which eval_check never emits on its own (T-BL19 finding).
    Non-manifold = any issue with kind=="free_edges" OR severity=="error".
    tiny_edges warnings on curved seams/poles are cosmetic and do NOT fail manifold.
    Falls back to ``free_edges`` scalar if the issues list is absent (forward-compat).
    """
    from mcp_server import core_bridge as cb
    from mcp_server.core_bridge import CoreError
    d = doc_id or _cur_doc()
    try:
        r = cb.call("eval_check", {"doc_id": d})
        iss = r.get("issues", [])
        if iss:
            # Primary path: inspect the structured issue list.
            bad = [i for i in iss
                   if i.get("kind") == "free_edges" or i.get("severity") == "error"]
            ok = len(bad) == 0
            fe_count = sum(1 for i in iss if i.get("kind") == "free_edges")
        else:
            # Fallback: scalar field (older engine / no issue list).
            fe_count = r.get("free_edges", 0)
            ok = fe_count == 0
        tiny = r.get("tiny_edges", 0)
        return json.dumps({
            "ok": ok,
            "free_edges": fe_count,
            "tiny_edges": tiny,
            "issues": iss,
            "summary": "manifold" if ok else f"{fe_count} free edge(s) — not watertight",
        })
    except CoreError as e:
        return json.dumps({"ok": False, "error": str(e)})


def _gap_eval_check_direct(doc_id: str = None, **_) -> str:
    """Direct eval_check exposure (BRepCheck + free/tiny-edge census).

    Reads the ``issues`` list from eval_check (OCCT BRepCheck) rather than the bare
    ``free_edges``/``invalid_faces`` scalars, which eval_check never emits on its own
    (same latent bug T-BL23 fixed in ``_gap_is_manifold``; see T-BL19 finding).
    Not ok = any issue with kind=="free_edges" OR severity=="error".
    Falls back to the scalar fields if the issues list is absent (forward-compat).
    """
    from mcp_server import core_bridge as cb
    from mcp_server.core_bridge import CoreError
    d = doc_id or _cur_doc()
    try:
        r = cb.call("eval_check", {"doc_id": d})
        iss = r.get("issues", [])
        if iss:
            bad = [i for i in iss
                   if i.get("kind") == "free_edges" or i.get("severity") == "error"]
            ok = len(bad) == 0
        else:
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

    mega-15: when the mission has no drawing (mechanical doc / airframe), honest
    SKIP — not a zero-score FAIL. A check that CAN run (drawing open) still runs.
    """
    from mcp_server import core_bridge as cb
    d = doc_id or _cur_doc()
    # Honest SKIP when no drawing in mission scope (not a drawing doc).
    if not _is_drawing_doc(d):
        return _skip_json(
            "dim_coverage",
            "no drawing in mission scope",
        )
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
        err = str(e)
        # Drawing miss class → honest SKIP (mega-15 AC1 residual).
        if "no open drawing" in err.lower() or "drawing" in err.lower():
            return _skip_json("dim_coverage", "no drawing in mission scope")
        return json.dumps({"ok": False, "error": err})


def _gap_req_join(doc_id: str = None, requirements: list = None, **_) -> str:
    """aicad.req/1 join — does each requirement's STATUS agree with its verifier EVIDENCE?

    Status ladder from aicad.req/1 (T-H3, engine_oracle/requirements):
    proposed | user_confirmed | verified | failed | waived (plus legacy aliases pass/ok).
    A "verified"/"pass" claim with an explicitly EMPTY trace.evidence list is
    INCONSISTENT — the exact failure this join exists to catch. "waived" is exempt
    (excluded from the denominator). score = met / non-waived total.
    """
    if not requirements:
        return json.dumps({"ok": True, "score": 1.0, "note": "no requirements provided"})
    met = 0
    unmet: list = []
    inconsistent: list = []
    n_scored = 0
    for i, r in enumerate(requirements):
        rid = r.get("id", i)
        status = r.get("status")
        if status == _WAIVED_STATUS:
            continue
        n_scored += 1
        if status not in _REQ_MET_STATUSES:
            unmet.append(rid)
            continue
        trace = r.get("trace")
        if isinstance(trace, dict) and trace.get("evidence") == []:
            inconsistent.append(rid)   # claims verified, carries no evidence
            unmet.append(rid)
            continue
        met += 1
    score = met / n_scored if n_scored else 1.0
    return json.dumps({
        "ok": score >= 1.0,
        "score": score,
        "passed": met,
        "total": n_scored,
        "unmet": unmet,
        "inconsistent": inconsistent,
    })


def _gap_interface_check(doc_id: str = None, fastener: str = None, components: list = None, **_) -> str:
    """T-BL49 interface alignment check — thin passthrough to the tool implementation.

    Returns the tool's stable JSON verbatim (ok + findings); the battery's "findings" reward
    shape reads the `findings` list. Kept as a gap fn because interface_check lives in its own
    module (not one of the private-MCP modules)."""
    from services.mcp.interface_check_tools import run_interface_check
    return run_interface_check(doc_id=doc_id, fastener=fastener, components=components)


def _layout_coverage_cb():
    """Return core_bridge for layout_coverage (lazy import, patchable in tests)."""
    try:
        from mcp_server import core_bridge as _cb
        return _cb
    except Exception:  # noqa: BLE001
        return None


def _gap_layout_coverage(doc_id: str = None, **_) -> str:
    """Layout-plan critical-dim & interface coverage gate (T-BL50).

    Reads the ``layout_plan`` doc-meta envelope (schema aicad.layout/1) to find which
    requirement IDs are flagged as critical_dim or interface, then checks each one in
    the associated aicad.req/1 doc for verification evidence.

    A requirement is considered COVERED when:
      - status == "verified"  AND  trace.evidence is non-empty, OR
      - status == "user_confirmed"  (user explicitly signed off — counts as evidence), OR
      - status == "waived"  (user exempted it)

    A requirement is UNCOVERED when status is "proposed" or "failed", or when
    status == "verified" but trace.evidence == [] (inconsistent — same rule as req_join).

    Score = covered / total_critical.  ok = score >= 1.0 (all covered).
    If no layout_plan meta exists: ok=True, score=1.0, note="no layout plan on file".
    """
    d = doc_id or _cur_doc()
    try:
        cb = _layout_coverage_cb()
        if cb is None:
            return json.dumps({"ok": False, "error": "core_bridge unavailable"})

        # Load layout_plan envelope
        try:
            raw_plan = cb.call("doc_get_meta", {"doc_id": d, "key": "layout_plan"}).get("value")
        except Exception:
            raw_plan = None

        if not isinstance(raw_plan, dict) or raw_plan.get("schema") != "aicad.layout/1":
            return json.dumps({
                "ok": True,
                "score": 1.0,
                "note": "no layout plan on file — layout_plan() not called before modeling",
                "covered": 0,
                "total": 0,
                "uncovered": [],
            })

        critical_ids = list(raw_plan.get("critical_req_ids") or [])
        interface_ids = list(raw_plan.get("interface_req_ids") or [])
        all_layout_ids = critical_ids + interface_ids

        if not all_layout_ids:
            return json.dumps({
                "ok": True,
                "score": 1.0,
                "note": "layout plan exists but no critical dims or interfaces recorded",
                "covered": 0,
                "total": 0,
                "uncovered": [],
            })

        # Load the aicad.req/1 doc
        try:
            raw_req = cb.call("doc_get_meta", {"doc_id": d, "key": "requirements"}).get("value")
        except Exception:
            raw_req = None

        if not isinstance(raw_req, dict):
            # No req doc — all uncovered
            return json.dumps({
                "ok": False,
                "score": 0.0,
                "note": "layout plan references requirements but no req doc found on this doc",
                "covered": 0,
                "total": len(all_layout_ids),
                "uncovered": all_layout_ids,
            })

        req_by_id: dict[str, dict] = {
            r["id"]: r for r in raw_req.get("requirements", []) if isinstance(r, dict)
        }

        covered = 0
        uncovered: list[dict] = []

        for rid in all_layout_ids:
            req = req_by_id.get(rid)
            if req is None:
                uncovered.append({"id": rid, "reason": "requirement not found in req doc"})
                continue

            status = req.get("status", "proposed")
            evidence = (req.get("trace") or {}).get("evidence") or []
            kind = req.get("kind", "?")

            if status == _WAIVED_STATUS:
                covered += 1  # user explicitly waived
            elif status == "user_confirmed":
                covered += 1  # user signed off explicitly
            elif status == _VERIFIED_STATUS and evidence:
                covered += 1  # verified with evidence
            elif status == _VERIFIED_STATUS and not evidence:
                # Inconsistent: claims verified but no evidence (same rule as req_join)
                uncovered.append({
                    "id": rid,
                    "kind": kind,
                    "status": status,
                    "reason": "status=verified but trace.evidence is empty (inconsistent)",
                })
            else:
                qty = req.get("quantity") or {}
                param = qty.get("param", "?") if qty else "?"
                uncovered.append({
                    "id": rid,
                    "kind": kind,
                    "status": status,
                    "param": param,
                    "reason": f"not yet verified (status={status!r})",
                })

        total = len(all_layout_ids)
        score = covered / total if total else 1.0
        ok = len(uncovered) == 0

        return json.dumps({
            "ok": ok,
            "score": round(score, 4),
            "covered": covered,
            "total": total,
            "uncovered": uncovered,
            "critical_count": len(critical_ids),
            "interface_count": len(interface_ids),
            "note": (
                "all layout-critical dims and interfaces verified"
                if ok else
                f"{len(uncovered)} layout-critical item(s) lack verification evidence"
            ),
        })

    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)})


def _gap_power_budget(doc_id: str = None, power_source: dict = None,
                      power_converters: list = None, board_path: str = None,
                      power_profile_path: str = None, power_board: dict = None,
                      power_profile: dict = None, power_scenario: str = "",
                      **_) -> str:
    """T-FIN23 system power/energy budget as a verify check.

    Delegates to mcp_server.power_budget.solve_power_budget: rail tree
    (source → converters → boardphys loads), duty-cycle weighting from an
    aicad.powerprofile/1 artifact, PDN cross-check, battery-life verdict.
    Without a power_source kwarg the check passes with a skip note (the battery's
    default all-subsecond sweep must stay green on non-electronic parts).
    Findings shape: overloads are severity="fail", other warnings severity="warn".
    """
    if not power_source:
        return json.dumps({"ok": True, "findings": [],
                           "note": "no power_source specified — power budget skipped"})
    try:
        from mcp_server.power_budget import solve_power_budget, _load_json
        board = power_board
        if board is None and board_path:
            board = _load_json(board_path, "boardphys sidecar")
        profile = power_profile
        if profile is None and power_profile_path:
            profile = _load_json(power_profile_path, "power profile")
        return json.dumps(solve_power_budget(
            power_source, converters=power_converters, board=board,
            power_profile=profile, scenario=power_scenario))
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)})


GAP_FNS: dict[str, Any] = {
    "interface_check":   _gap_interface_check,
    "is_manifold":       _gap_is_manifold,
    "round_trip":        _gap_round_trip,
    "groundedness":      _gap_groundedness,
    "unit_sanity":       _gap_unit_sanity,
    "dim_coverage":      _gap_dim_coverage,
    "req_join":          _gap_req_join,
    "eval_check_direct": _gap_eval_check_direct,
    "layout_coverage":   _gap_layout_coverage,  # T-BL50
    "power_budget":      _gap_power_budget,     # T-FIN23
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

        # CAM gouge pre-gate: any gouge is a hard fail regardless of machined_fraction.
        # Must be checked BEFORE the key loop so a high machined_fraction never masks it.
        gouges = r.get("gouges")
        if gouges is not None and int(gouges) > 0:
            score = 0.0
            findings.append({
                "check": check_name,
                "ok": False,
                "detail": f"gouge(s) detected: {int(gouges)} (max_gouge_mm={r.get('max_gouge_mm', '?')})",
            })
        else:
            found = False
            for key in ("match_pct", "score", "min_fos", "cd", "max_von_mises_mpa",
                        "coverage_ratio", "machined_fraction"):
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
        # interface_check (T-BL49) reports a `findings` list of {severity, detail, fix, ...};
        # only severity=="fail" entries should drive the score/fail (warns are advisory). Other
        # findings-shaped checks use pairs/violations/etc. and every listed item counts.
        if "findings" in r and isinstance(r.get("findings"), list):
            fails = [f for f in r["findings"]
                     if isinstance(f, dict) and f.get("severity") == "fail"]
            score = 1.0 if r.get("ok", not fails) and not fails else max(0.0, 1.0 - len(fails) * 0.1)
            for f in fails:
                detail = f.get("detail") or str(f)
                if f.get("fix"):
                    detail = f"{detail}  -> fix: {f['fix']}"
                findings.append({"check": check_name, "ok": False, "detail": detail})
        else:
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
                if name == "dim_coverage":
                    # Adapter lives inside _gap_dim_coverage (honest SKIP).
                    call_kwargs: dict[str, Any] = dict(doc_id=doc_id, **kwargs)
                    result = fn(**call_kwargs)
                else:
                    call_kwargs = dict(doc_id=doc_id, **kwargs)
                    result = fn(**call_kwargs)
            else:
                fn = _get_fn(module_key, fn_name)
                adapted, skip_payload = _adapt_check_kwargs(
                    name, fn, doc_id, kwargs
                )
                if skip_payload is not None:
                    # Honest SKIP: record reason, omit zero score (mega-15).
                    skipped.append(name)
                    skip_obj = _parse_jsonish(skip_payload)
                    findings.append({
                        "check": name,
                        "ok": True,
                        "skipped": True,
                        "detail": skip_obj.get("skip_reason")
                        or skip_obj.get("summary")
                        or "skipped",
                    })
                    continue
                call_kwargs = adapted or {}
                result = fn(**call_kwargs)
        except Exception as e:
            result = json.dumps({"ok": False, "error": str(e)})

        # Gap / tool may also return honest SKIP envelope.
        parsed_early = _parse_jsonish(result)
        if parsed_early.get("skipped") is True:
            skipped.append(name)
            findings.append({
                "check": name,
                "ok": True,
                "skipped": True,
                "detail": parsed_early.get("skip_reason")
                or parsed_early.get("summary")
                or "skipped",
            })
            continue

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
