"""IP-18 regen-diagnosis MCP surface — the callable backend seam for rebuild-failure repair.

When a parametric document fails to rebuild, ``diagnose_failed_document`` runs the IP-18
diagnosis (``services.agent.regen_diagnosis``), verifies the proposed fix against the live core
when present, and returns a fix-preview card (``services.agent.regen_card``). The provider call
is logged as an IP-18 correction trace via T-H7's LoggingProvider.

This is the shared surface the copilot / orchestrator call on a rebuild-failure event —
registered the way T-F16 registered its worker RPCs: a live, callable backend that fires the
moment any caller invokes it. It harvests REAL correction traces only once a caller (T-F17's
copilot binding) wires it to the rebuild-failure event; until then it is a callable-but-uncalled
surface (same posture T-F16 shipped its preview pane in).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from services.agent.regen_diagnosis import detect_failures


def _resolve_provider():
    """The configured agent provider, or None when none is resolvable (no credentials / offline
    with nothing local). The tool degrades honestly rather than fabricating a diagnosis."""
    try:
        from services.agent.config import get_provider
        return get_provider()
    except Exception:
        return None


def _verify(document: dict, diagnosis, reference_document: Optional[dict]):
    """Score the proposed fix with the strongest available signal:
      * core + reference doc  -> binary rebuild AND compare_parts vs the last-good revision;
      * core, no reference    -> does the fix rebuild? (no known-good to compare an in-the-wild
                                 failure against);
      * no core               -> structural: is the anomaly cleared?
    Returns (score|None, detail)."""
    claimed = diagnosis.n_applied > 0
    try:
        from tools.flywheel.bench.benches import regen_fix as RF
        opener, compare_fn = RF._make_live_verifier()
    except Exception:
        opener = compare_fn = None

    if opener is None:
        cleared = not detect_failures(diagnosis.fixed_document)
        return (1.0 if (claimed and cleared) else 0.0), {"mode": "fixture", "anomaly_gone": cleared}

    if reference_document is not None:
        score, _passed, detail = RF.verify_fix(
            diagnosis.fixed_document, claimed, {},
            good_document=reference_document, opener=opener, compare_fn=compare_fn)
        return score, detail

    res = opener(diagnosis.fixed_document)
    return (1.0 if (claimed and res["rebuilt"]) else 0.0), {
        "mode": "live-rebuild-only", "rebuilt": res["rebuilt"], "rebuild_error": res.get("error")}


def diagnose_failed_document(document: dict, *, provider=None,
                             reference_document: Optional[dict] = None,
                             engine_error: Optional[str] = None,
                             on_event=None, consent_fn=None, verify: bool = True) -> dict:
    """Diagnose a failed-rebuild ``document`` and return a fix-preview card.

    ``document`` is a full aicad.part/1 doc or a ``{revision, tree, owner_map}`` wrapper.
    ``reference_document`` (optional) is the last known-good revision, enabling the compare_parts
    drift check. ``provider`` defaults to the configured agent provider; when none is resolvable
    the tool returns ``ok=False`` with the detected failures (still useful) rather than guessing.
    """
    prov = provider or _resolve_provider()
    if prov is None:
        return {
            "ok": False,
            "reason": "no provider configured for regen diagnosis (set one to enable IP-18 fixes)",
            "failures": [f.to_dict() for f in detect_failures(document)],
        }

    from services.agent.regen_diagnosis import diagnose
    from services.agent.regen_card import build_fix_preview

    d = diagnose(document, prov, engine_error=engine_error, on_event=on_event, consent_fn=consent_fn)
    score, detail = _verify(document, d, reference_document) if verify else (None, {})
    card = build_fix_preview(document, d, verifier_score=score, verifier_detail=detail)
    return {"ok": True, "artifact_id": d.artifact_id, "card": card}


def register_regen_tools(mcp) -> None:
    """Register the ``regen_diagnose`` MCP tool onto *mcp* (append-only, IP-18 / T-H11)."""

    @mcp.tool()
    def regen_diagnose(document_json: str, reference_json: str = "",
                       engine_error: str = "") -> str:
        """Diagnose a document that FAILED TO REBUILD and return a fix-preview card the user can
        accept/edit/reject.

        ``document_json``: the failed document as JSON — a full ``aicad.part/1`` doc or a
        ``{"revision","tree","owner_map"}`` wrapper. ``reference_json`` (optional): the last
        known-good revision, enabling a compare_parts drift check on the proposed fix.
        ``engine_error`` (optional): the engine's feature_status error text, added to the
        diagnosis context.

        Returns a JSON card: ``{ok, artifact_id, card:{diagnosis, fix_ops, changes, semantic_diff,
        verifier, actions}}``. The fix ops are editable doc-ops (R1 parity). The call is logged as
        an IP-18 correction trace. ``ok=false`` (with the detected failures) when no model provider
        is configured.
        """
        try:
            document = json.loads(document_json)
        except json.JSONDecodeError as e:
            return json.dumps({"ok": False, "error": f"document_json: invalid JSON — {e}"})
        reference = None
        if reference_json.strip():
            try:
                reference = json.loads(reference_json)
            except json.JSONDecodeError as e:
                return json.dumps({"ok": False, "error": f"reference_json: invalid JSON — {e}"})
        result = diagnose_failed_document(
            document, reference_document=reference, engine_error=engine_error or None)
        return json.dumps(result, default=str)
