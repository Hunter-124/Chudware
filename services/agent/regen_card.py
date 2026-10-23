"""IP-18 fix-preview card — the standalone accept/edit/reject surface for a regen diagnosis.

When a rebuild fails and ``regen_diagnosis.diagnose`` proposes a fix, the user is shown a card:
the one-line diagnosis, the editable fix ops (R1 editability — doc-ops, not an opaque script),
and a ``doc_diff`` of what the fix changes, alongside the verifier verdict (did it rebuild
+ compare ≈100%?). Accept applies the fix; reject discards it; edit lets the user amend the ops
first. Each choice emits an ``ai_artifact_action`` event correlated to the diagnosis's IP-18
``ai_artifact`` — so the accepted/edited/rejected outcome (with its verifier score) becomes part
of the same correction trace the model call was logged as.

This is a STANDALONE, headless-testable surface: it builds the card as a plain dict and emits
actions through the same ``on_event`` sink the copilot uses, so the copilot UI binds to it
without changes to the agent panel. No C++ core and no GUI are required to exercise it.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .regen_diagnosis import Diagnosis, _tree

# Action verbs the card can emit (mirrors the C++ emit_ai_artifact_action `action` arg).
ACCEPT, EDIT, REJECT = "accept", "edit", "reject"


def _op_summary(op: dict) -> dict:
    """A plain-English one-liner for one editable fix op (shown in the preview)."""
    name = op.get("op")
    if name == "repoint_ref":
        text = f"repoint feature {op.get('feature')} ref {op.get('from')!r} → {op.get('to')!r}"
    elif name == "set_selector_kind":
        text = f"set feature {op.get('feature')} selector kind → {op.get('kind')!r}"
    elif name == "unsuppress":
        text = f"un-suppress feature {op.get('feature')}"
    elif name == "set_param":
        text = f"set feature {op.get('feature')} param {op.get('param')} → {op.get('value')}"
    elif name == "set_equation":
        text = f"set equation {op.get('global')!r} → {op.get('expr')!r}"
    else:
        text = f"unknown op {name!r}"
    return {"op": name, "text": text}


def build_fix_preview(corrupted_document: dict, diagnosis: Diagnosis, *,
                      verifier_score: Optional[float] = None,
                      verifier_detail: Optional[dict] = None) -> dict:
    """Build the fix-preview card payload for ``diagnosis`` over ``corrupted_document``.

    ``verifier_score``/``verifier_detail`` come from the binary verifier
    (tools.flywheel.bench.benches.regen_fix.verify_fix, or the live agent flow): score 1.0 iff
    the fix rebuilds AND unaffected geometry compares ≈100%. Shown so the user accepts an
    already-verified fix, and carried onto the action event.
    """
    # The primary, always-available preview: a plain-English change list derived from the
    # editable fix ops (R1 parity — the user sees exactly which doc-ops will apply).
    changes = [_op_summary(op) for op in diagnosis.fix_ops]

    # Optional enrichment: semantic doc_diff. Imported lazily and degraded gracefully —
    # doc_diff_tools hard-imports FastMCP at module top, which isn't always importable.
    try:
        from services.mcp.doc_diff_tools import doc_diff
        semantic_diff: dict = doc_diff(_tree(corrupted_document), _tree(diagnosis.fixed_document))
    except Exception as e:
        semantic_diff = {"unavailable": f"{type(e).__name__}: {e}"[:120]}

    return {
        "kind": "fix_preview_card",
        "insertion_point": "IP-18",
        "artifact_id": diagnosis.artifact_id,
        "run_id": diagnosis.run_id,
        "diagnosis": diagnosis.diagnosis,
        "failures": diagnosis.failures,
        "fix_ops": diagnosis.fix_ops,          # editable — the user may amend before accepting
        "changes": changes,                    # plain-English per-op preview (always present)
        "n_applied": diagnosis.n_applied,
        "semantic_diff": semantic_diff,        # doc_diff when available (enrichment)
        "verifier": {
            "score": verifier_score,
            "passed": (verifier_score is not None and verifier_score >= 1.0),
            "detail": verifier_detail or {},
        },
        "actions": [ACCEPT, EDIT, REJECT],
        # in-process handles for resolve(); underscore-prefixed (not for UI serialization)
        "_corrupted_document": corrupted_document,
        "_fixed_document": diagnosis.fixed_document,
    }


def emit_action(card: dict, action: str, on_event: Optional[Callable[[dict], None]], *,
                consent: str = "meta", revision: int = 0, dwell_ms: int = 0,
                edit_ops: Optional[list] = None, edit_ops_ref: str = "",
                note: str = "") -> dict:
    """Emit an ``ai_artifact_action`` for the user's choice on ``card`` and return the event.

    Correlates to the diagnosis's IP-18 ``ai_artifact`` via ``artifact_id`` so the exporter
    can stitch the (model call → verifier score → human accept/reject) correction trace.
    The verifier score rides on the action (DoD: apply/reject logged as artifact actions WITH
    verifier scores). At meta/off consent, ``edit_ops_ref`` must already be empty (no bodies
    leave the machine) — the caller passes refs only at full consent.
    """
    if action not in (ACCEPT, EDIT, REJECT):
        raise ValueError(f"unknown action {action!r}")
    event = {
        "kind": "ai_artifact_action",
        "artifact_id": card.get("artifact_id"),
        "run_id": card.get("run_id"),
        "insertion_point": card.get("insertion_point", "IP-18"),
        "action": action,
        "revision": revision,
        "edit_ops_ref": edit_ops_ref if consent == "full" else "",
        "undo_within_ops": 0,
        "dwell_ms": dwell_ms,
        "note": note,
        "consent": consent,
        # verifier score attached to the outcome (not in the C++ signature's positional args,
        # additive here; the exporter ignores unknown keys and reads it for reward labels).
        "verifier_scores": {"regen_fix": card.get("verifier", {}).get("score")},
    }
    if action == EDIT and edit_ops is not None:
        event["edit_op_count"] = len(edit_ops)
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # telemetry must never break the interaction
            pass
    return event


def resolve(card: dict, action: str, on_event: Optional[Callable[[dict], None]] = None, *,
            edit_ops: Optional[list] = None, **kw) -> Optional[dict]:
    """Apply the user's choice and return the document to load (or None to keep the failed doc).

    accept -> the diagnosed fixed document. edit -> re-apply the user's amended ops to the
    ORIGINAL corrupted doc (never the already-fixed one) so the edit is authoritative. reject ->
    None. Always emits the corresponding action event.
    """
    emit_action(card, action, on_event, edit_ops=edit_ops, **kw)
    if action == ACCEPT:
        return card.get("_fixed_document")
    if action == EDIT and edit_ops is not None:
        from .regen_diagnosis import apply_fix_ops
        corrupted = card.get("_corrupted_document")
        if corrupted is None:
            return None
        fixed, _ = apply_fix_ops(corrupted, edit_ops)
        return fixed
    return None
