"""Host adapters — normalize a host result into engine :class:`Finding`s.

The engine is pure over a normalized observation; the
loop-vs-doc mismatch lives here in ~two thin adapters, not smeared through the
engine.

Two sources exist:

  * :func:`findings_from_build` — the **loop.py path**.  The agent generates
    build123d *code*; there is no doc_id, no feature ids, no sketch entities.
    The only signals are ``res.fit`` (interference/clearance by part *name* +
    volume) and ``res.error`` (rebuild).  So only ``interference`` and
    ``rebuild`` findings can fire here.  Interference carries **no diameters**
    (kernel ``_fit_report`` gives ``{a, b, volume}`` only), so the engine emits a
    heuristic move/resize fix + ``fit_calc`` helper — never a synthesized bore Ø.

  * :func:`findings_from_doc` — the **doc path** (``advise(doc_id)`` MCP tool and
    DAG nodes carrying a doc_id).  Runs the stage battery against ``doc_eval_*`` /
    ``sketch_health`` RPCs, where feature ids and measured diameters DO exist,
    so the flagship ``interference → fit_calc`` computed fix runs for real.
"""
from __future__ import annotations

from typing import Any, Optional
import json

from . import stages
from .engine import Finding, Observation


# ---------------------------------------------------------------------------
# Loop path — build123d result → findings
# ---------------------------------------------------------------------------

def findings_from_build(res: Any) -> list[Finding]:
    """Extract findings from a loop.py build result (ModelResult/AssemblyResult).

    Accepts either the dataclass or its ``asdict`` form.  Returns findings for
    interference (assembly ``fit``) and rebuild (build ``error``).  No doc_id, no
    diameters — those checks that need a live doc simply do not fire here.
    """
    findings: list[Finding] = []

    ok = _get(res, "ok", True)
    error = _get(res, "error", None)
    if not ok and error:
        findings.append(Finding(
            kind="rebuild",
            check=stages.CHECK_REBUILD,
            severity="action",
            finding=_first_error_line(error),
            numbers={},
            refs={},
            source_kind="kernel",
            source_detail="build123d",
            extra={},
        ))

    fit = _get(res, "fit", None)
    if isinstance(fit, dict):
        for it in fit.get("interferences", []):
            vol = it.get("volume")
            findings.append(Finding(
                kind="interference",
                check=stages.CHECK_INTERFERENCE,
                # action, not blocker: the loop has no diameters to compute the
                # canonical fit fix, so we advise (heuristic) rather than gate.
                severity="action",
                finding=(f"bodies {it.get('a')}/{it.get('b')} interfere "
                         f"{vol:.0f} mm³" if isinstance(vol, (int, float))
                         else f"bodies {it.get('a')}/{it.get('b')} interfere"),
                numbers={"overlap_mm3": round(vol, 1) if isinstance(vol, (int, float)) else vol},
                refs={"bodies": [it.get("a"), it.get("b")]},
                source_kind="kernel",
                source_detail="_fit_report",
                extra={},  # NO bore_d/shaft_d on the loop path (see module docstring)
            ))
    return findings


def observe_build(res: Any, stage: str = "feature", seq: int = 0,
                  user_override: bool = False,
                  output_text: str = "", tool_names: Optional[list[str]] = None,
                  cooldown_state: Optional[dict] = None) -> Observation:
    """Convenience: wrap a loop result into an :class:`Observation`.

    Default stage is ``assembly`` when the result is an assembly (interference is
    an assembly-stage check), else ``feature``.

    *output_text* / *tool_names* / *cooldown_state* (§20) thread the
    trigger-injection signals through to :func:`engine.build_advisory`.
    agent_turn.py's turn loop is also a caller of this adapter — it has no
    build123d result, so it passes ``res={}`` purely for the trigger path.
    """
    if _get(res, "is_assembly", False) or isinstance(_get(res, "fit", None), dict):
        stage = "assembly"
    return Observation(
        stage=stage,
        findings=findings_from_build(res),
        seq=seq,
        doc_id="",
        user_override=user_override,
        output_text=output_text,
        tool_names=list(tool_names or []),
        cooldown_state=cooldown_state,
    )


# ---------------------------------------------------------------------------
# Doc path — doc_id + stage battery → findings
class _CheckUnavailable(RuntimeError):
    """A battery check has no executable backend in this process."""


# ---------------------------------------------------------------------------

def findings_from_doc(doc_id: str, stage: str, rpc: Optional[Any] = None) -> list[Finding]:
    """Run *stage*'s battery against a live doc via ``rpc`` and normalize results.

    ``rpc`` is a callable ``rpc(method, **kwargs) -> dict`` (the core bridge /
    session RPC surface).  Injected so tests can drive fixtures without the
    engine; ``None`` ⇒ resolve the default core bridge lazily.

    Only the checks in ``stages.check_battery(stage)`` run.  Cheap, read-only.
    """
    call = rpc or _default_rpc()

    findings: list[Finding] = []
    for check in stages.check_battery(stage):
        try:
            findings.extend(_run_check(check, doc_id, call))
        except _CheckUnavailable as exc:
            findings.append(_check_outcome_finding(
                check, doc_id, severity="info", outcome="skipped", error=exc,
            ))
        except Exception as exc:  # noqa: BLE001
            severity = "blocker" if check in stages.exit_gate(stage) else "action"
            findings.append(_check_outcome_finding(
                check, doc_id, severity=severity, outcome="error", error=exc,
            ))
    return findings


def observe_doc(doc_id: str, stage: str, seq: int = 0,
                rpc: Optional[Any] = None, user_override: bool = False,
                output_text: str = "", tool_names: Optional[list[str]] = None,
                cooldown_state: Optional[dict] = None) -> Observation:
    """§20: *output_text* / *tool_names* / *cooldown_state* are optional
    trigger-injection signals threaded through to :func:`engine.build_advisory`;
    absent by default so the advise() MCP doc path (no turn signals) is unchanged.
    """
    return Observation(
        stage=stage,
        findings=findings_from_doc(doc_id, stage, rpc=rpc),
        seq=seq,
        doc_id=doc_id,
        user_override=user_override,
        output_text=output_text,
        tool_names=list(tool_names or []),
        cooldown_state=cooldown_state,
    )


def _check_outcome_finding(
    check: str,
    doc_id: str,
    *,
    severity: str,
    outcome: str,
    error: BaseException,
) -> Finding:
    detail = str(error).strip() or error.__class__.__name__
    return Finding(
        kind="check_error" if outcome == "error" else "check_skipped",
        check=check,
        severity=severity,
        finding=f"{check} {outcome}: {detail}",
        numbers={},
        refs={"doc_id": doc_id},
        source_kind="harness",
        source_detail="advisor_check_dispatch",
        extra={
            "outcome": outcome,
            "exception": error.__class__.__name__,
            "error": detail,
        },
    )


def _run_check(check: str, doc_id: str, call: Any) -> list[Finding]:
    if check == stages.CHECK_INTERFERENCE:
        return _check_interference(doc_id, call)
    if check == stages.CHECK_FEATURE_OVERLAP:
        return _check_overlap(doc_id, call)
    if check in (stages.CHECK_SKETCH_HEALTH, stages.CHECK_OPEN_PROFILE):
        return _check_sketch(doc_id, call, check)
    if check == stages.CHECK_REBUILD:
        return _check_rebuild(doc_id, call)
    # Diagnostic advisor checks
    if check == stages.CHECK_WALL_THICKNESS:
        return _check_wall_thickness(doc_id, call)
    if check == stages.CHECK_DFM:
        return _check_dfm(doc_id, call)
    if check == stages.CHECK_INTERFACE_FITMENT:
        return _check_interface_fitment(doc_id, call)
    # part_verify / sim / cam presence checks do not have an adapter backend.
    raise _CheckUnavailable(f"{check} has no advisor check adapter")


def _check_interference(doc_id: str, call: Any) -> list[Finding]:
    r = call("doc_eval_interference", doc_id=doc_id) or {}
    # Build a name→bbox lookup from the ``parts`` list so the engine can derive
    # the minimum-translation-separation axis without a second RPC call.
    # Kernel returns {parts:[{name, bbox:{min,max,size}, volume}], interferences:[{a,b,volume}]}
    bbox_by_name: dict[str, dict] = {}
    for part in r.get("parts", []):
        name = part.get("name")
        bbox = part.get("bbox")
        if name and bbox:
            bbox_by_name[name] = bbox

    out: list[Finding] = []
    for it in r.get("interferences", []):
        # The doc path CAN carry cylindrical-face diameters + the feature id when
        # the eval identifies a bore/shaft pair — that unlocks the fit_calc fix.
        extra: dict[str, Any] = {}
        if it.get("bore_d") is not None and it.get("shaft_d") is not None:
            extra = {
                "bore_d": it["bore_d"], "shaft_d": it["shaft_d"],
                "feature": it.get("feature"),
                "fit": it.get("fit", "H7/g6"),
                "nominal": it.get("nominal", it.get("shaft_d")),
            }
        # Carry bbox data so engine._fix_interference can derive the MTS axis.
        a_name = it.get("a", "")
        b_name = it.get("b", "")
        if a_name in bbox_by_name:
            extra["bbox_a"] = bbox_by_name[a_name]
        if b_name in bbox_by_name:
            extra["bbox_b"] = bbox_by_name[b_name]

        vol = it.get("volume")
        out.append(Finding(
            kind="interference",
            check=stages.CHECK_INTERFERENCE,
            severity="blocker",  # assembly interference gates stage exit (§9.3)
            finding=it.get("summary")
                    or (f"bodies {a_name}/{b_name} interfere "
                        f"{vol} mm³"),
            numbers={"overlap_mm3": vol,
                     **({"bore_d": it["bore_d"], "shaft_d": it["shaft_d"]}
                        if it.get("bore_d") is not None else {})},
            refs={"doc_id": doc_id, "bodies": [a_name, b_name],
                  "features": [extra["feature"]] if extra.get("feature") else []},
            source_kind="kernel",
            source_detail="doc_eval_interference",
            extra=extra,
        ))
    return out


def _check_overlap(doc_id: str, call: Any) -> list[Finding]:
    r = call("doc_eval_feature_overlap", doc_id=doc_id) or {}
    out: list[Finding] = []
    for ov in r.get("overlaps", []):
        out.append(Finding(
            kind="overlap",
            check=stages.CHECK_FEATURE_OVERLAP,
            severity="action",
            finding=ov.get("summary") or "feature overlap detected",
            numbers={"classification": ov.get("classification")},
            refs={"doc_id": doc_id, "features": ov.get("features", [])},
            source_kind="kernel",
            source_detail="doc_eval_feature_overlap",
            extra={"classification": ov.get("classification")},
        ))
    return out


def _check_sketch(doc_id: str, call: Any, check: str) -> list[Finding]:
    from services.mcp.ascii2d import sketch_design_intent, sketch_feeds_feature

    snapshot = call("doc_tree", doc_id=doc_id) or {}
    features = (snapshot.get("tree") or {}).get("features")
    if not isinstance(features, list):
        raise RuntimeError("doc_tree returned no feature roster")

    out: list[Finding] = []
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") not in ("sketch", "sketch2d"):
            continue
        sketch = feature.get("id")
        if not sketch:
            raise RuntimeError("doc_tree returned a sketch without a feature id")
        refs = {"doc_id": doc_id, "sketch": sketch}
        try:
            r = call("sketch_health", doc_id=doc_id, sketch=sketch)
            if not isinstance(r, dict) or r.get("ok") is False:
                raise RuntimeError(f"sketch_health failed: {r}")
            closure = r.get("closure") or {}
            if check == stages.CHECK_OPEN_PROFILE:
                if not isinstance(closure.get("closed_chains_ok"), bool):
                    raise RuntimeError("sketch_health returned no closure verdict")
                endpoints = closure.get("open_endpoints") or []
                if not closure["closed_chains_ok"] or endpoints:
                    out.append(Finding(
                        kind="open_profile", check=check, severity="blocker",
                        finding=f"sketch {sketch} profile is not closed: "
                                f"{len(endpoints)} open endpoint(s)",
                        numbers={"open_endpoints": len(endpoints)},
                        refs={**refs, "handles": endpoints}, source_kind="kernel",
                        source_detail="sketch_health", extra={"closure": closure},
                    ))
                continue

            dof, status = r.get("dof"), r.get("status")
            if not isinstance(dof, (int, float)) or not status:
                raise RuntimeError("sketch_health returned no DOF/status verdict")
            params = feature.get("params") or {}
            intent = sketch_design_intent(
                params.get("entities") or [], params.get("constraints") or [],
                health=r, feeds_feature=sketch_feeds_feature(features, str(sketch)),
            )
            quality_findings = list(intent["findings"])
            if status == "under" and dof <= 0:
                quality_findings.append({
                    "kind": "under_dimensioned", "severity": "warning",
                    "message": "Sketch reports under-defined status; inspect free variables "
                               "and add geometric relations/driving dimensions.",
                })
            if status == "over" or r.get("conflicting") or r.get("converged") is False:
                quality_findings.append({
                    "kind": "sketch_conflict", "severity": "error",
                    "message": "Sketch cannot solve cleanly; resolve conflicting constraints.",
                })
            for finding in quality_findings:
                severity = finding.get("severity")
                out.append(Finding(
                    kind=finding["kind"], check=check,
                    severity=("blocker" if severity in ("error", "fail", "blocker")
                              else "info" if severity == "info" else "action"),
                    finding=f"sketch {sketch}: {finding['message']}",
                    numbers={"dof": dof, "status": status,
                             "driving_dimensions": intent["driving_dimensions"]},
                    refs={**refs, "entities": finding.get("entities", [])},
                    source_kind="kernel", source_detail="sketch_health",
                    extra={**finding, "pinned_only": intent["pinned_only"],
                           "anchored": intent["anchored"],
                           "free_variables": r.get("free_variables") or [],
                           "dof_narration": r.get("dof_narration"),
                           "conflicting": r.get("conflicting") or []},
                ))
        except Exception as exc:  # noqa: BLE001
            failure = _check_outcome_finding(
                check, doc_id, severity="blocker", outcome="error", error=exc,
            )
            failure.refs["sketch"] = sketch
            out.append(failure)
    return out


def _check_rebuild(doc_id: str, call: Any) -> list[Finding]:
    r = call("doc_feature_status", doc_id=doc_id) or {}
    out: list[Finding] = []
    for feat in r.get("failed", []):
        out.append(Finding(
            kind="rebuild",
            check=stages.CHECK_REBUILD,
            severity="action",
            finding=feat.get("summary") or f"feature {feat.get('id')} failed to rebuild",
            numbers={},
            refs={"doc_id": doc_id, "features": [feat.get("id")]},
            source_kind="kernel",
            source_detail="doc_feature_status",
            extra={"diagnosis": feat.get("diagnosis")},
        ))
    return out


# ---------------------------------------------------------------------------
# Diagnostic advisors: wall_thickness, DFM, and interface_fitment checks
# ---------------------------------------------------------------------------

def _check_wall_thickness(doc_id: str, call: Any) -> list[Finding]:
    """Run eval_thickness + resolve process minimum → thin-wall Finding.

    Numbers: ``min_thickness_mm`` (kernel ray-cast), ``limit_mm`` (ProcessProfile
    table), ``process`` (name string).  Source: kernel ray-cast — cited as such.
    Returns an ``action`` finding when wall < limit, ``info`` when OK.
    """
    th = call("eval_thickness", doc_id=doc_id) or {}
    mn = th.get("min_thickness_mm")
    if mn is None:
        return []  # open/sheet body — no thickness measurement

    # Attempt to resolve the process minimum from the process profile bound to this doc.
    limit: Optional[float] = None
    proc_name: str = "process"
    try:
        proc_info = call("doc_get_process", doc_id=doc_id) or {}
        proc_name = proc_info.get("process") or "process"
        params = proc_info.get("params") or {}
        lim_val = params.get("min_wall_mm")
        if lim_val is not None:
            limit = float(lim_val)
    except Exception:  # noqa: BLE001
        pass

    if limit is not None and mn < limit:
        finding_text = (f"thinnest wall {mn:.3f} mm < {proc_name} minimum {limit:.3f} mm "
                        f"(Δ={limit - mn:+.3f} mm) — from eval_thickness kernel ray-cast")
        severity = "action"
    elif limit is not None:
        finding_text = (f"thinnest wall {mn:.3f} mm meets {proc_name} minimum {limit:.3f} mm "
                        f"— from eval_thickness kernel ray-cast")
        # OK wall — emit info so the advisory is available but non-blocking.
        return [Finding(
            kind="wall_thickness",
            check=stages.CHECK_WALL_THICKNESS,
            severity="info",
            finding=finding_text,
            numbers={"min_thickness_mm": mn, "limit_mm": limit, "process": proc_name},
            refs={"doc_id": doc_id},
            source_kind="kernel",
            source_detail="eval_thickness",
            extra={},
        )]
    else:
        finding_text = f"thinnest wall {mn:.3f} mm — from eval_thickness kernel ray-cast"
        severity = "info"

    return [Finding(
        kind="wall_thickness",
        check=stages.CHECK_WALL_THICKNESS,
        severity=severity,
        finding=finding_text,
        numbers={"min_thickness_mm": mn,
                 **({"limit_mm": limit, "process": proc_name} if limit is not None else {})},
        refs={"doc_id": doc_id},
        source_kind="kernel",
        source_detail="eval_thickness",
        extra={"process": proc_name, "limit_mm": limit},
    )]


def _check_dfm(doc_id: str, call: Any) -> list[Finding]:
    """Run dfm_check (eval_thickness + eval_draft against ProcessProfile) → DFM Finding.

    Returns a ``blocker`` Finding when any DFM check fails (dfm_pass=False), ``info``
    otherwise.  The ``checks`` list is carried in ``extra`` so the fix provider can
    emit per-issue targeted fixes without re-calling the tool.
    """
    dfm = call("dfm_check", doc_id=doc_id) or {}
    dfm_pass = dfm.get("dfm_pass", True)
    checks = dfm.get("checks") or []
    proc = dfm.get("process", "process")

    failed = [c for c in checks if not c.get("ok", True)]
    if not dfm_pass and failed:
        issues = "; ".join(c.get("note", c.get("check", "")) for c in failed)
        finding_text = f"DFM FAIL ({proc}): {issues}"
        severity = "blocker"
    else:
        finding_text = f"DFM PASS ({proc}): all {len(checks)} check(s) OK"
        severity = "info"

    return [Finding(
        kind="dfm",
        check=stages.CHECK_DFM,
        severity=severity,
        finding=finding_text,
        numbers={"dfm_pass": int(dfm_pass), "n_checks": len(checks),
                 "n_failed": len(failed)},
        refs={"doc_id": doc_id},
        source_kind="kernel",
        source_detail="dfm_check+eval_thickness+eval_draft",
        extra={"checks": checks, "process": proc},
    )]


def _check_interface_fitment(doc_id: str, call: Any) -> list[Finding]:
    """Run interface_check → per-pair alignment Findings.

    Each failed/warned finding in the interface_check ``findings`` list becomes an
    ``action`` Finding; the tool's pre-computed ``fix`` string is carried in
    ``extra`` so the engine fix provider can lift it directly into a candidate_fix.
    """
    ic = call("interface_check", doc_id=doc_id) or {}
    # Only process a genuine interface_check v1 response (tool key present).
    # An empty {} from a fixture or a tool that is not yet wired returns nothing
    # so we do not inject spurious info items into unrelated tests.
    if ic.get("tool") != "interface_check":
        return []
    ic_findings = ic.get("findings") or []
    out: list[Finding] = []

    # Group findings by component pair for a compact summary.
    failed = [f for f in ic_findings if f.get("severity") in ("fail", "warn")]
    if not failed:
        # No issues — emit info so there's an explicit advisory record.
        return [Finding(
            kind="interface_fitment",
            check=stages.CHECK_INTERFACE_FITMENT,
            severity="info",
            finding=ic.get("summary", "interface alignment OK"),
            numbers={"n_findings": 0},
            refs={"doc_id": doc_id},
            source_kind="kernel",
            source_detail="interface_check",
            extra={},
        )]

    # Emit one action Finding per failing component pair (grouped by a+b).
    pairs_seen: set[tuple] = set()
    for fc in failed:
        a = fc.get("component_a", "?")
        b = fc.get("component_b", "?")
        pair_key = (min(a, b), max(a, b))
        if pair_key in pairs_seen:
            continue  # already emitted a finding for this pair — carry extra findings there
        pairs_seen.add(pair_key)
        pair_findings = [f for f in failed
                         if set([f.get("component_a"), f.get("component_b")]) == {a, b}]
        issues = "; ".join(pf.get("detail", pf.get("check", "")) for pf in pair_findings)
        finding_text = f"interface alignment FAIL {a}↔{b}: {issues}"
        out.append(Finding(
            kind="interface_fitment",
            check=stages.CHECK_INTERFACE_FITMENT,
            severity="action",
            finding=finding_text,
            numbers={"n_findings": len(pair_findings)},
            refs={"doc_id": doc_id, "bodies": [a, b]},
            source_kind="kernel",
            source_detail="interface_check",
            extra={"findings": pair_findings},
        ))
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get(obj: Any, name: str, default: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _first_error_line(error: str) -> str:
    lines = [ln.strip() for ln in str(error).strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "rebuild failed"


def _default_rpc():
    """Return the one explicit dispatcher for native and composed advisor checks."""
    try:
        from services.mcp import core_bridge  # type: ignore
    except Exception as exc:  # noqa: BLE001
        unavailable = f"core bridge unavailable: {exc}"

        def _unavailable(method: str, **kwargs):
            raise _CheckUnavailable(unavailable)

        return _unavailable

    native_methods = {
        "doc_eval_interference": "eval_interference",
        "doc_eval_feature_overlap": "eval_feature_overlap",
        "doc_tree": "doc_tree",
        "sketch_health": "sketch_health",
        "doc_feature_status": "doc_rebuild",
        "eval_thickness": "eval_thickness",
    }

    def _parse_composed(method: str, raw: Any) -> dict:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{method} returned invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(
                f"{method} returned {type(raw).__name__}, expected an object"
            )
        if raw.get("ok") is False:
            detail = (
                raw.get("error")
                or raw.get("message")
                or raw.get("reason")
                or raw.get("code")
                or "unknown error"
            )
            raise RuntimeError(f"{method} failed: {detail}")
        return raw

    def _call(method: str, **kwargs):
        if method == "dfm_check":
            try:
                from services.verify.battery import _get_fn
                fn = _get_fn("intake", "dfm_check")
            except Exception as exc:  # noqa: BLE001
                raise _CheckUnavailable(f"dfm_check unavailable: {exc}") from exc
            return _parse_composed(method, fn(**kwargs))

        if method == "interface_check":
            try:
                from services.mcp.interface_check_tools import run_interface_check
            except Exception as exc:  # noqa: BLE001
                raise _CheckUnavailable(f"interface_check unavailable: {exc}") from exc
            return _parse_composed(method, run_interface_check(**kwargs))

        native = native_methods.get(method)
        if native is None:
            raise _CheckUnavailable(f"{method} has no registered advisor backend")

        result = core_bridge.call(native, kwargs)
        if method == "doc_feature_status":
            rebuild = result.get("rebuild") or {}
            statuses = rebuild.get("feature_status") or result.get("feature_status") or []
            failed = []
            for status in statuses:
                if isinstance(status, dict) and not status.get("ok", True):
                    failed.append({
                        "id": status.get("id"),
                        "summary": status.get("error") or status.get("warning"),
                        "diagnosis": status.get("error"),
                    })
            return {"failed": failed}
        return result

    return _call
