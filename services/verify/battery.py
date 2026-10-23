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
import math
import sys
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Argument adapters for checks that require non-doc_id args.
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
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        tree = cb.call("doc_tree", {"doc_id": doc_id}) or {}
        feats = ((tree.get("tree") or {}).get("features")) or []
        return [f for f in feats if isinstance(f, dict)]
    except Exception:  # noqa: BLE001
        return []


# Checks that need a solid body. On a study-only document (a hydraulic circuit, piping
# network, vessel or duct sizing saved with no geometry) they have nothing to check, and
# scoring them 0.0 turned a finished engineering study into a "failed" battery the agent
# then reported to the user as geometry failures (2026-10-08, clamp HPU run).
_SOLID_CHECKS = frozenset({"eval_check", "is_manifold", "measure_part"})


def _study_only_kinds(doc_id: Optional[str]) -> list[str]:
    """Study types of a document that has studies and no solid geometry; else ``[]``.

    Solid geometry is a feature, an assembly component, or a world solid. An assembly
    has no features of its own: a features-only test called a 36-component arm assembly
    with Motion studies "study-only" and skipped its solid checks (2026-10-10).
    """
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        did = doc_id or _cur_doc()
        tree = (cb.call("doc_tree", {"doc_id": did}) or {}).get("tree") or {}
        if tree.get("features") or tree.get("components"):
            return []
        if (cb.call("asm_world_bodies", {"doc_id": did, "explode": False}) or {}).get(
                "bodies"):
            return []
        studies = (cb.call("sim_study_list", {"doc_id": did}) or {}).get("studies") or []
    except Exception:  # noqa: BLE001 — unreadable: score the checks as before
        return []
    return sorted({str(s.get("type") or "study") for s in studies if isinstance(s, dict)})


def _measure_probe_chosen_by_battery(caller_kwargs: dict, call_kwargs: dict) -> bool:
    """Only wholly automatic probes may turn an ambiguous ref into unavailable.

    If either endpoint was supplied by the caller, preserve its refusal: the engine
    does not say which endpoint was ambiguous.
    """
    return all(caller_kwargs.get(key) is None and call_kwargs.get(key) is not None
               for key in ("a_query", "b_query"))


def _is_ambiguous_ref_failure(result: dict) -> bool:
    """True for the engine's structured "ref matched multiple elements" refusal."""
    detail = result.get("detail")
    return (result.get("ok") is False and isinstance(detail, dict)
            and detail.get("reason") == "ambiguous")


_VALID_SIDE_WORDS = frozenset({
    "top", "bottom", "left", "right", "front", "back",
    "+x", "-x", "+y", "-y", "+z", "-z",
})


def _normalize_side_query(q: Any) -> Any:
    """Map invalid plain words like 'top face' → valid side word 'top'."""
    if not isinstance(q, str):
        return q
    s = q.strip()
    if not s:
        return q
    low = s.lower()
    if low in _VALID_SIDE_WORDS:
        return low
    # Strip trailing ' face' / ' faces' / ' side'.
    for suf in (" faces", " face", " side", " surface"):
        if low.endswith(suf):
            base = low[: -len(suf)].strip()
            if base in _VALID_SIDE_WORDS:
                return base
    return q


def _query_geometry_faces(doc_id: Optional[str]) -> list[dict]:
    """Read the complete live face roster, including topology and owner stamps."""
    if not doc_id:
        try:
            doc_id = _cur_doc()
        except Exception:  # noqa: BLE001
            return []
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        raw = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}}) or {}
        matches = raw.get("matches") or []
        return [m for m in matches if isinstance(m, dict)]
    except Exception:  # noqa: BLE001
        return []


def _derive_measure_face_pair(doc_id: Optional[str]) -> Optional[tuple[Any, Any]]:
    """Choose a real opposing planar-face span, never a broad side-word query.

    A SID is a geometry fingerprint, not a globally unique topology ID. Count it
    within its body/component provenance before using it; identical instances or
    duplicate fingerprints without an exact owner are unsupported, not first-match
    picks. Query readbacks are world-space, so keep body_id flat (a selector envelope
    would reinterpret an assembly readback in component-local coordinates).
    """
    faces = _query_geometry_faces(doc_id)
    counts: dict[tuple, int] = {}
    sid_counts: dict[str, int] = {}
    candidates = []
    for face in faces:
        sid = face.get("sid")
        if not isinstance(sid, str) or not sid:
            continue
        body = face.get("body_id", face.get("body"))
        body = body if type(body) is int and body > 0 else None
        component = face.get("component", face.get("component_id"))
        key = (body, sid)
        counts[key] = counts.get(key, 0) + 1
        sid_counts[sid] = sid_counts.get(sid, 0) + 1
        candidates.append((face, body, component, sid, key))

    def _vec(value):
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        try:
            result = tuple(float(v) for v in value)
        except (TypeError, ValueError):
            return None
        return result if all(math.isfinite(v) for v in result) else None

    usable = []
    for face, body, component, sid, key in candidates:
        # Component alone is not an eval_measure owner. Without body identity the
        # SID must be unique across the entire document, including other instances.
        if counts[key] != 1 or (body is None and sid_counts[sid] != 1):
            continue
        if face.get("surf") != "plane":
            continue
        center, normal = _vec(face.get("centroid")), _vec(face.get("normal"))
        if center is None or normal is None:
            continue
        length = math.sqrt(sum(v * v for v in normal))
        if length <= 1e-12:
            continue
        ref = {"sid": sid}
        if body is not None:
            ref["body_id"] = body
        usable.append((ref, (body, component), center, tuple(v / length for v in normal)))

    best = None
    best_rank = None
    for i, (a_ref, a_owner, a_center, a_normal) in enumerate(usable):
        for b_ref, b_owner, b_center, b_normal in usable[i + 1:]:
            if sum(a * b for a, b in zip(a_normal, b_normal)) > -0.999:
                continue
            span = abs(sum((b - a) * n for a, b, n in
                           zip(a_center, b_center, a_normal)))
            if span <= 1e-6:
                continue
            # Prefer a span on one body/component, then the widest measured plane
            # separation. Stable refs break ties, independent of query ordering.
            pair = tuple(sorted((a_ref, b_ref), key=lambda ref: json.dumps(ref, sort_keys=True)))
            rank = (a_owner == b_owner, span, json.dumps(pair, sort_keys=True))
            if best_rank is None or rank > best_rank:
                best, best_rank = pair, rank
    return best



def _sketch_ids(doc_id: Optional[str]) -> list[str]:
    """Read every 2D sketch id; an unavailable roster is an error, not an empty doc."""
    from services.mcp import core_bridge as cb  # noqa: PLC0415

    tree = (cb.call("doc_tree", {"doc_id": doc_id or _cur_doc()}) or {}).get("tree")
    if not isinstance(tree, dict) or not isinstance(tree.get("features"), list):
        raise RuntimeError("doc_tree returned no feature roster")
    out: list[str] = []
    for f in tree["features"]:
        if not isinstance(f, dict):
            continue
        ftype = str(f.get("type") or f.get("kind") or "").lower()
        if ftype in ("sketch", "sketch2d") or (
            ftype.startswith("sketch") and "3d" not in ftype
        ):
            fid = f.get("id")
            if not fid:
                raise RuntimeError("doc_tree returned a sketch without a feature id")
            out.append(str(fid))
    return out


def _is_drawing_doc(doc_id: Optional[str]) -> bool:
    """True when drawing_get succeeds for *doc_id* (has sheets/views)."""
    if not doc_id:
        return False
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
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
        from services.mcp.ecad_common import ecad_session as esess  # noqa: PLC0415
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
        from services.mcp.ecad_common import ecad_session as esess  # noqa: PLC0415
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


    if check_name == "measure_between":
        # Explicit endpoints belong to the caller, including invalid or ambiguous
        # ones. Normalize historical side spellings but never replace their target.
        for key in ("a_query", "b_query"):
            if key in call_kwargs:
                call_kwargs[key] = _normalize_side_query(call_kwargs[key])
        missing = [key for key in ("a_query", "b_query") if call_kwargs.get(key) is None]
        if missing:
            pair = _derive_measure_face_pair(call_kwargs.get("doc_id") or doc_id)
            if pair is None:
                return None, json.dumps({
                    **_parse_jsonish(_skip_json(
                        check_name,
                        "preflight unsupported: no unambiguous opposing planar-face span "
                        "in the resolved topology; no measurement was attempted. The document "
                        "may have no features or no supported face pair. Supply explicit "
                        "unique body-scoped refs to measure a specific design dimension.",
                    )),
                    "not_evaluated": True,
                    "reason": "probe_ref_unsupported",
                })
            for key, ref in zip(("a_query", "b_query"), pair):
                if key in missing:
                    call_kwargs[key] = ref
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
        # ERC needs a schematic. Look for one regardless of what doc_id says: the
        # caller often passes a mechanical doc (or nothing at all), and the check
        # must resolve to the real schematic session or be skipped.
        sch_doc = None
        try:
            from services.mcp.ecad_common import ecad_session as esess  # noqa: PLC0415
            for s in esess.iter_sessions():
                kind = str(getattr(s, "kind", "") or "").lower()
                did = getattr(s, "doc_id", None)
                if did and kind in ("sch", "schematic", ""):
                    doc = getattr(s, "doc", None) or {}
                    if isinstance(doc, dict) and (
                        doc.get("symbols") is not None
                        or kind in ("sch", "schematic")
                    ):
                        sch_doc = did
                        break
        except Exception:  # noqa: BLE001
            sch_doc = None
        if sch_doc is not None:
            call_kwargs["doc_id"] = sch_doc
            return call_kwargs, None
        # No schematic anywhere: ERC cannot apply. Running it anyway scored 0.0,
        # which is indistinguishable from a real failure — live 2026-07-27 that
        # phantom zero made haiku re-run verify_battery three times chasing a
        # score it could never move, until the loop guard refused it. Every
        # sibling ecad check already skips in this situation.
        return None, _skip_json(
            check_name,
            "no schematic open — ERC does not apply to a mechanical part",
        )

    return call_kwargs, None


def _check_all_sketches(fn: Any, doc_id: Optional[str], kwargs: dict) -> dict:
    """Keep the per-sketch tool verdicts and require every requested sketch to pass."""
    call_kwargs, _ = _adapt_check_kwargs("check_sketch", fn, doc_id, kwargs)
    call_kwargs = call_kwargs or {}
    sketches = ([call_kwargs["sketch"]] if call_kwargs.get("sketch")
                else _sketch_ids(doc_id))
    if not sketches:
        return _parse_jsonish(_skip_json("check_sketch", "no sketches on document"))
    results: list[dict] = []
    findings: list[dict] = []
    failures: list[dict] = []
    for sketch in sketches:
        try:
            result = _parse_jsonish(fn(**{**call_kwargs, "sketch": sketch}))
            if not result:
                raise RuntimeError("check_sketch returned no result object")
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        result = {**result, "sketch": sketch}
        result["pinned_only"] = result.get("pinned_only") is True
        dof = result.get("dof")
        closed = result.get("closure") or {}
        fully_defined = (
            result.get("ok") is True and result.get("fully_defined") is True
            and not result["pinned_only"] and not result.get("skipped")
            and result.get("status") not in ("under", "over")
            and not (isinstance(dof, (int, float)) and dof > 0)
            and not result.get("conflicting") and result.get("converged") is not False
            and closed.get("closed_chains_ok") is not False
            and not closed.get("open_endpoints") and not result.get("open_endpoints")
            and not any(isinstance(f, dict) and (
                f.get("ok") is False or f.get("severity") in ("error", "fail", "blocker"))
                for f in result.get("findings") or [])
        )
        result["fully_defined"] = fully_defined
        results.append(result)
        for finding in result.get("findings") or []:
            if isinstance(finding, dict):
                findings.append({**finding, "sketch": sketch})
        if not fully_defined:
            failure = {
                "sketch": sketch, "ok": False,
                "detail": result.get("error") or (
                    "Profile is pinned instead of constrained." if result["pinned_only"]
                    else "Sketch is not fully defined, closed, and relation/dimension constrained."),
                "fully_defined": False, "pinned_only": result["pinned_only"],
            }
            failures.append(failure)
    return {
        "ok": all(r.get("ok") is True and not r.get("skipped") for r in results),
        "doc_id": doc_id,
        "fully_defined": all(r["fully_defined"] for r in results),
        "pinned_only": any(r["pinned_only"] for r in results),
        "sketches": results, "findings": findings, "failures": failures,
        "summary": (f"Checked {len(results)} sketch(es): "
                    f"{len(failures)} failed sketch quality."),
    }


# ---------------------------------------------------------------------------
# Canonical aicad.req/1 status constants (services/requirements).
# Imported here so _gap_req_join doesn't carry its own stub copies.
# ---------------------------------------------------------------------------
from services.requirements.requirements import VALID_STATUSES as _REQ_VALID_STATUSES

# "waived" is the one aicad.req/1 status that exempts a requirement from scoring.
# Derived from the canonical set so any schema rename propagates automatically.
_WAIVED_STATUS: str = next(s for s in _REQ_VALID_STATUSES if s == "waived")

# The canonical "met" status from aicad.req/1 — derived so any ladder rename propagates.
_VERIFIED_STATUS: str = next(s for s in _REQ_VALID_STATUSES if s == "verified")

# The "met" set: canonical status + legacy aliases accepted for interoperability.
# "pass" and "ok" are NOT in VALID_STATUSES; they are explicit aliases kept here so
# older requirement documents don't silently fail.  The canonical anchor (_VERIFIED_STATUS)
# is derived from the ladder — if the schema renames "verified", this tuple tracks it.
# (Dropping the legacy aliases entirely would be a schema-level change.)
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
    "dfm_check":                 ("intake", "dfm_check",               "seconds",   "any",   "dfm"),
    "solve_mates":               ("cad",  "solve_mates",               "seconds",   "any",   "binary"),
    # Design-time interface alignment (hole patterns, coaxiality, contact planes,
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
    # Floating / overhanging / edge-touching additive features (engine eval_feature_attachment).
    "feature_attachment":        ("gap",  "feature_attachment",        "subsecond", "any",   "findings"),
    "round_trip":                ("gap",  "round_trip",                "seconds",   "any",   "scalar"),
    "groundedness":              ("gap",  "groundedness",              "subsecond", "any",   "scalar"),
    "unit_sanity":               ("gap",  "unit_sanity",               "subsecond", "any",   "findings"),
    "dim_coverage":              ("gap",  "dim_coverage",              "subsecond", "any",   "scalar"),
    "req_join":                  ("gap",  "req_join",                  "subsecond", "any",   "scalar"),
    "eval_check":                ("gap",  "eval_check_direct",         "subsecond", "any",   "binary"),
    # Layout-first critical-dim & interface coverage gate
    "layout_coverage":           ("gap",  "layout_coverage",           "subsecond", "any",   "scalar"),
    # System power/energy budget (rail tree + duty cycle + battery verdict).
    # Runs only when a power_source kwarg is supplied; otherwise passes with a skip note.
    "power_budget":              ("gap",  "power_budget",              "subsecond", "any",   "findings"),
}

# Common verification vocabulary models use in build briefs.  These are exact semantic
# rewrites to existing checks, not fuzzy guesses; verify() echoes every rewrite.
CHECK_ALIASES: dict[str, str] = {
    "watertight": "is_manifold",
    "manifold": "is_manifold",
    "solid_health": "eval_check",
    "attachment": "feature_attachment",
    "floating_features": "feature_attachment",
    "overhang": "feature_attachment",
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
            from services.mcp.cad_tools import register_cad_tools
            register_cad_tools(_mcp)
        elif module_key == "recon":
            from services.mcp.recon_tools import register_recon_tools
            register_recon_tools(_mcp)
        elif module_key == "ecad":
            from services.mcp.ecad_tools import register_ecad_tools
            register_ecad_tools(_mcp)
        elif module_key == "cam":
            from services.mcp.cam_tools import register_cam_tools
            register_cam_tools(_mcp)
        elif module_key == "intake":
            from services.mcp.intake_tools import register_intake_tools
            register_intake_tools(_mcp)
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
    from services.mcp.cad_tools import _CUR
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
    ``free_edges`` scalar, which eval_check never emits on its own.
    Non-manifold = any issue with kind=="free_edges" OR severity=="error".
    tiny_edges warnings on curved seams/poles are cosmetic and do NOT fail manifold.
    Falls back to ``free_edges`` scalar if the issues list is absent (forward-compat).
    """
    from services.mcp import core_bridge as cb
    from services.mcp.core_bridge import CoreError
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
    (same latent bug fixed in ``_gap_is_manifold``).
    Not ok = any issue with kind=="free_edges" OR severity=="error".
    Falls back to the scalar fields if the issues list is absent (forward-compat).
    """
    from services.mcp import core_bridge as cb
    from services.mcp.core_bridge import CoreError
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


def _closeup_view_for(entry: dict, center) -> dict:
    """The camera that shows an attachment defect: from outside the part toward the problem
    location, LOW for an overhang (the unbacked underside is the evidence), level for a
    floating body (the gap under it), slightly above for an edge contact. Returned in
    render_part's {"azimuth", "elevation"} vocabulary (az 0 = front -Y, 90 = right +X)."""
    loc = entry.get("location") or center
    dx, dy = float(loc[0]) - float(center[0]), float(loc[1]) - float(center[1])
    if math.hypot(dx, dy) < 1e-6:
        dx, dy = 0.0, -1.0
    elevation = {"overhang": -25, "floating": 5}.get(entry.get("verdict"), 20)
    return {"azimuth": round(math.degrees(math.atan2(dx, -dy))), "elevation": elevation}


def _gap_feature_attachment(doc_id: str = None, **_) -> str:
    """Check: every additive feature is SEATED on the part — none floating as its own solid,
    overhanging its supporting face, or touching only along an edge (engine
    eval_feature_attachment). Each failure names the feature, where it is, and the close-up
    render_part call that shows it."""
    from services.mcp import core_bridge as cb
    from services.mcp.core_bridge import CoreError
    d = doc_id or _cur_doc()
    if _is_drawing_doc(d):
        return _skip_json("feature_attachment", "drawing document — nothing to attach")
    try:
        r = cb.call("eval_feature_attachment", {"doc_id": d})
    except CoreError as e:
        return json.dumps({"ok": False, "error": str(e)})
    if r.get("skipped"):
        return _skip_json("feature_attachment", r.get("skip_reason") or "skipped")
    try:
        bb = cb.call("doc_bbox", {"doc_id": d}) or {}
        center = bb.get("center") or [0.0, 0.0, 0.0]
    except CoreError:
        center = [0.0, 0.0, 0.0]
    findings = []
    for e in r.get("features") or []:
        view = _closeup_view_for(e, center)
        findings.append({
            "severity": "fail",
            "feature_id": e.get("feature_id"),
            "verdict": e.get("verdict"),
            "location": e.get("location"),
            "detail": e.get("detail"),
            "fix": (f'look: render_part(doc_id="{d}", target="{e.get("feature_id")}", '
                    f"view={json.dumps(view)}); then move its sketch/start plane onto the "
                    "supporting face or trim its profile inside the support"),
        })
    return json.dumps({"ok": not findings, "summary": r.get("summary"),
                       "solid_count": r.get("solid_count"), "flagged": r.get("flagged"),
                       "independent": r.get("independent"), "findings": findings})


def _gap_round_trip(doc_id: str = None, threshold: float = 99.0, **_) -> str:
    """Export STEP → re-import → compare_parts. match_pct >= threshold + eval_check clean."""
    import tempfile
    import os
    from services.mcp import core_bridge as cb
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
    from services.mcp import core_bridge as cb
    d = doc_id or _cur_doc()
    state: dict = {}
    try:
        state["tree"] = cb.call("doc_tree", {"doc_id": d})
    except Exception:  # noqa: BLE001
        pass
    try:
        state["mass"] = cb.call("eval_mass", {"doc_id": d})  # the model's applied materials
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
    from services.mcp import core_bridge as cb
    from services.verify.unit_ranges import check_unit_ranges
    d = doc_id or _cur_doc()
    try:
        state = cb.call("eval_mass", {"doc_id": d})
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

    When the mission has no drawing (mechanical doc / airframe), honest
    SKIP — not a zero-score FAIL. A check that CAN run (drawing open) still runs.
    """
    from services.mcp import core_bridge as cb
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
        # Drawing miss class → honest SKIP.
        if "no open drawing" in err.lower() or "drawing" in err.lower():
            return _skip_json("dim_coverage", "no drawing in mission scope")
        return json.dumps({"ok": False, "error": err})


def _gap_req_join(doc_id: str = None, requirements: list = None, **_) -> str:
    """aicad.req/1 join — does each requirement's STATUS agree with its verifier EVIDENCE?

    Status ladder from aicad.req/1 (services/requirements):
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
    """Interface alignment check — thin passthrough to the tool implementation.

    Returns the tool's stable JSON verbatim (ok + findings); the battery's "findings" reward
    shape reads the `findings` list. Kept as a gap fn because interface_check lives in its own
    module (not one of the private-MCP modules)."""
    from services.mcp.interface_check_tools import run_interface_check
    return run_interface_check(doc_id=doc_id, fastener=fastener, components=components)


def _layout_coverage_cb():
    """Return core_bridge for layout_coverage (lazy import, patchable in tests)."""
    try:
        from services.mcp import core_bridge as _cb
        return _cb
    except Exception:  # noqa: BLE001
        return None


def _gap_layout_coverage(doc_id: str = None, **_) -> str:
    """Layout-plan critical-dim & interface coverage gate.

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
    """System power/energy budget as a verify check.

    Delegates to services.mcp.power_budget.solve_power_budget: rail tree
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
        from services.mcp.power_budget import solve_power_budget, _load_json
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
    "feature_attachment": _gap_feature_attachment,
    "round_trip":        _gap_round_trip,
    "groundedness":      _gap_groundedness,
    "unit_sanity":       _gap_unit_sanity,
    "dim_coverage":      _gap_dim_coverage,
    "req_join":          _gap_req_join,
    "eval_check_direct": _gap_eval_check_direct,
    "layout_coverage":   _gap_layout_coverage,
    "power_budget":      _gap_power_budget,
}


# ---------------------------------------------------------------------------
# Score extraction from JSON return
# ---------------------------------------------------------------------------
# Binary checks whose `ok` means "the check ran to completion", not "the design passed".
# The named field carries the pass verdict.
_BINARY_VERDICT_FIELDS: dict[str, str] = {
    "check_interference": "clear",
    "check_sketch": "fully_defined",
}


def _extract_score(check_name: str, reward_shape: str, result_json: str | dict) -> tuple[float, list]:
    """Parse JSON return from a check function → (score 0..1, findings list)."""
    try:
        r = result_json if isinstance(result_json, dict) else json.loads(result_json)
    except Exception:
        return 0.0, [{"check": check_name, "error": "parse_error", "raw": str(result_json)[:200]}]

    findings: list[dict] = []
    if check_name == "check_sketch":
        passed = (r.get("ok") is True and r.get("fully_defined") is True
                  and not r.get("pinned_only") and not r.get("failures"))
        for item in r.get("findings") or []:
            if isinstance(item, dict):
                findings.append({**item, "check": check_name,
                                 "detail": item.get("message") or item.get("detail") or ""})
        for failure in r.get("failures") or []:
            if isinstance(failure, dict):
                findings.append({**failure, "check": check_name, "ok": False})
        if not passed and not r.get("failures"):
            findings.append({
                "check": check_name, "ok": False,
                "detail": r.get("error") or r.get("summary")
                          or "Sketch quality was not established.",
            })
        return (1.0 if passed else 0.0), findings

    if check_name == "measure_between":
        distance = r.get("distance")
        try:
            measured = (r.get("ok") is True and not isinstance(distance, bool)
                        and distance is not None and math.isfinite(float(distance)))
        except (TypeError, ValueError):
            measured = False
        if not measured:
            return 0.0, [{
                "check": check_name, "ok": False,
                "detail": r.get("error") or r.get("summary")
                          or "measure_between returned no finite distance evidence",
            }]
        if abs(float(distance)) <= 1e-9:
            findings.append({
                "check": check_name, "ok": True,
                "detail": "distance 0.0 mm — coincident/adjacent faces "
                          "(honest finding, not a soft-zero failure)",
            })
        return 1.0, findings


    if reward_shape == "binary":
        # For a check whose ok only means "the check ran", the verdict is a separate field.
        verdict_field = _BINARY_VERDICT_FIELDS.get(check_name)
        passed = bool(r.get("ok", False)) and (
            verdict_field is None or r.get(verdict_field) is True)
        score = 1.0 if passed else 0.0
        if not passed:
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
            if not found:
                # Scalar metrics may sit at the top level OR nested under
                # `summary`. The sim tools (run_fea_static / _modal / cfd)
                # return {ok, doc_id, material, summary:{min_fos, …}}, so a
                # top-level-only scan matched NOTHING for them and fell through
                # to the `ok` fallback below — scoring 1.0 for any solve that
                # merely COMPLETED. Measured 2026-08-04: an 80x50x4 aluminium
                # plate at 50 kN solves ok=True with min_fos 0.9944 (the part
                # FAILS) and scored a perfect 1.0. That is the fabricated-success
                # shape in the very check that gates mission done-ness.
                _nested = r.get("summary")
                _sources = [r, _nested] if isinstance(_nested, dict) else [r]
                for key in ("match_pct", "score", "min_fos", "cd", "max_von_mises_mpa",
                            "coverage_ratio", "machined_fraction"):
                    src = next((s for s in _sources if key in s), None)
                    if src is not None:
                        val = src[key]
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

    elif reward_shape == "dfm":
        checks = r.get("checks") if isinstance(r.get("checks"), list) else []
        failed = [c for c in checks if isinstance(c, dict) and c.get("ok") is False]
        score = 1.0 if r.get("dfm_pass") is True and not failed else 0.0
        for item in failed:
            findings.append({
                "check": check_name,
                "ok": False,
                "detail": item.get("note") or str(item),
            })
        if not checks and r.get("dfm_pass") is not True:
            findings.append({
                "check": check_name,
                "ok": False,
                "detail": r.get("error") or "DFM check returned no process checks",
            })

    else:  # "findings"
        # interface_check reports a `findings` list of {severity, detail, fix, ...};
        # only severity=="fail" entries should drive the score/fail (warns are advisory). Other
        # findings-shaped checks use pairs/violations/etc. and every listed item counts.
        if check_name == "interface_check" and r.get("ok") is not True:
            # ok=false is a refusal or an error: the alignment was not checked.
            score = 0.0
            findings.append({
                "check": check_name,
                "ok": False,
                "detail": r.get("error") or r.get("summary") or "interface_check did not run",
            })
        elif "findings" in r and isinstance(r.get("findings"), list):
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
          "interpreted": [{from, to}, ...],  # accepted check-name aliases
          "check_results": {"check_sketch": {fully_defined, pinned_only, sketches,
                                             findings, failures, ...}},
        }
    """
    import inspect

    t0 = time.monotonic()
    is_posix = sys.platform != "win32"

    if not checks:
        checks = [n for n, meta in CHECKS.items()
                  if meta[2] == "subsecond" and (meta[3] == "any" or is_posix)]

    interpreted: list[dict[str, str]] = []
    normalized: list[str] = []
    for requested in checks:
        key = str(requested).strip()
        canonical = CHECK_ALIASES.get(key.lower(), key)
        if canonical != key:
            interpreted.append({"from": key, "to": canonical})
        if canonical not in normalized:
            normalized.append(canonical)
    checks = normalized
    valid_checks = [entry["name"] for entry in check_metadata()]
    valid_check_set = set(valid_checks)
    unknown_checks = sorted(name for name in checks if name not in valid_check_set)
    if unknown_checks:
        raise ValueError(
            "verify_battery() got unknown check(s) "
            + ", ".join(repr(name) for name in unknown_checks)
            + f" — it takes checks= containing only names from {valid_checks} "
            "(see check_metadata())."
        )


    scores: dict[str, float] = {}
    findings: list[dict] = []
    skipped: list[str] = []
    check_results: dict[str, dict] = {}
    study_only = _study_only_kinds(doc_id) if _SOLID_CHECKS.intersection(checks) else []

    for name in sorted(checks):  # deterministic order

        module_key, fn_name, cost, platform_req, reward_shape = CHECKS[name]

        if platform_req == "posix" and not is_posix:
            skipped.append(name)
            continue

        if study_only and name in _SOLID_CHECKS:
            skipped.append(name)
            findings.append({
                "check": name,
                "ok": True,
                "skipped": True,
                "detail": "study-only document (" + ", ".join(study_only) + "): no solid "
                          "geometry to check — the studies' own results are the evidence",
            })
            continue

        probe_chosen = False
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
                    # Honest SKIP: record reason, omit zero score.
                    skipped.append(name)
                    skip_obj = _parse_jsonish(skip_payload)
                    findings.append({
                        "check": name,
                        "ok": True,
                        "skipped": True,
                        **{key: skip_obj[key] for key in ("not_evaluated", "reason")
                           if key in skip_obj},
                        "detail": skip_obj.get("skip_reason")
                        or skip_obj.get("summary")
                        or "skipped",
                    })
                    continue
                call_kwargs = adapted or {}
                probe_chosen = (name == "measure_between"
                                and _measure_probe_chosen_by_battery(kwargs, call_kwargs))
                result = (_check_all_sketches(fn, doc_id, kwargs)
                          if name == "check_sketch" else fn(**call_kwargs))
        except Exception as e:
            result = {"ok": False, "error": str(e)}
            if name == "check_sketch":
                result.update({
                    "doc_id": doc_id, "fully_defined": False, "pinned_only": False,
                    "sketches": [], "findings": [],
                    "failures": [{"ok": False, "detail": str(e)}],
                })
            else:
                result = json.dumps(result)

        # Gap / tool may also return honest SKIP envelope.
        parsed_early = _parse_jsonish(result)
        if name == "check_sketch":
            check_results[name] = parsed_early
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

        # The battery chose the probe refs itself. If a ref matches more than one element,
        # the probe has no single target: that is a battery selection problem, not a
        # geometry failure (2026-10-10, 36-solid arm assembly). Report it, do not score it.
        if probe_chosen and _is_ambiguous_ref_failure(parsed_early):
            skipped.append(name)
            findings.append({
                "check": name,
                "ok": True,
                "skipped": True,
                "not_evaluated": True,
                "reason": "probe_ref_ambiguous",
                "detail": (
                    "Not evaluated. The battery selected a measure ref that matches more "
                    "than one element. This result is not a geometry failure. To get a "
                    "measurement, call measure_between with refs that each match one "
                    "element. Engine message: " + str(parsed_early.get("error") or "")
                ),
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
        "interpreted": interpreted,
        "check_results": check_results,
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
