"""Host adapters — normalize a host result into engine :class:`Finding`s (T-W5).

Per the advisor review: the engine is pure over a normalized observation; the
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
    ``doc_sketch_health`` RPCs, where feature ids and measured diameters DO exist,
    so the flagship ``interference → fit_calc`` computed fix runs for real.
"""
from __future__ import annotations

from typing import Any, Optional

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
                  user_override: bool = False) -> Observation:
    """Convenience: wrap a loop result into an :class:`Observation`.

    Default stage is ``assembly`` when the result is an assembly (interference is
    an assembly-stage check), else ``feature``.
    """
    if _get(res, "is_assembly", False) or isinstance(_get(res, "fit", None), dict):
        stage = "assembly"
    return Observation(
        stage=stage,
        findings=findings_from_build(res),
        seq=seq,
        doc_id="",
        user_override=user_override,
    )


# ---------------------------------------------------------------------------
# Doc path — doc_id + stage battery → findings
# ---------------------------------------------------------------------------

def findings_from_doc(doc_id: str, stage: str, rpc: Optional[Any] = None) -> list[Finding]:
    """Run *stage*'s battery against a live doc via ``rpc`` and normalize results.

    ``rpc`` is a callable ``rpc(method, **kwargs) -> dict`` (the core bridge /
    session RPC surface).  Injected so tests can drive fixtures without the
    engine; ``None`` ⇒ resolve the default core bridge lazily.

    Only the checks in ``stages.check_battery(stage)`` run.  Cheap, read-only.
    """
    call = rpc or _default_rpc()
    if call is None:
        return []

    findings: list[Finding] = []
    for check in stages.check_battery(stage):
        try:
            findings.extend(_run_check(check, doc_id, call))
        except Exception:  # noqa: BLE001
            continue  # a failing check must never break the pass
    return findings


def observe_doc(doc_id: str, stage: str, seq: int = 0,
                rpc: Optional[Any] = None, user_override: bool = False) -> Observation:
    return Observation(
        stage=stage,
        findings=findings_from_doc(doc_id, stage, rpc=rpc),
        seq=seq,
        doc_id=doc_id,
        user_override=user_override,
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
    # part_verify / sim / cam presence checks are advisory-only presence flags.
    return []


def _check_interference(doc_id: str, call: Any) -> list[Finding]:
    r = call("doc_eval_interference", doc_id=doc_id) or {}
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
        vol = it.get("volume")
        out.append(Finding(
            kind="interference",
            check=stages.CHECK_INTERFERENCE,
            severity="blocker",  # assembly interference gates stage exit (§9.3)
            finding=it.get("summary")
                    or (f"bodies {it.get('a')}/{it.get('b')} interfere "
                        f"{vol} mm³"),
            numbers={"overlap_mm3": vol,
                     **({"bore_d": it["bore_d"], "shaft_d": it["shaft_d"]}
                        if extra else {})},
            refs={"doc_id": doc_id, "bodies": [it.get("a"), it.get("b")],
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
    r = call("doc_sketch_health", doc_id=doc_id) or {}
    out: list[Finding] = []
    if check == stages.CHECK_OPEN_PROFILE and not r.get("closed", True):
        out.append(Finding(
            kind="open_profile",
            check=stages.CHECK_OPEN_PROFILE,
            severity="blocker",  # open profile gates sketch exit
            finding=r.get("summary") or "sketch profile is not closed",
            numbers={"open_chains": r.get("open_chains")},
            refs={"doc_id": doc_id, "sketch": r.get("sketch")},
            source_kind="kernel",
            source_detail="doc_sketch_health",
            extra={},
        ))
    if check == stages.CHECK_SKETCH_HEALTH:
        dof = r.get("dof")
        if isinstance(dof, (int, float)) and dof > 0:
            out.append(Finding(
                kind="underconstrained",
                check=stages.CHECK_SKETCH_HEALTH,
                severity="hint",
                finding=r.get("dof_summary") or f"sketch under-constrained: {dof} DOF remain",
                numbers={"dof": dof},
                refs={"doc_id": doc_id, "sketch": r.get("sketch")},
                source_kind="kernel",
                source_detail="doc_sketch_health",
                extra={},
            ))
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
    """Lazily resolve the core bridge RPC surface; ``None`` if unavailable."""
    try:
        from services.mcp import core_bridge  # type: ignore
        def _call(method: str, **kwargs):
            fn = getattr(core_bridge, method, None)
            return fn(**kwargs) if callable(fn) else None
        return _call
    except Exception:  # noqa: BLE001
        return None
