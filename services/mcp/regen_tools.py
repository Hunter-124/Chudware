"""IP-18 regen-diagnosis MCP surface — the callable backend seam for rebuild-failure repair.

When a parametric document fails to rebuild, ``diagnose_failed_document`` runs the IP-18
diagnosis (``services.agent.regen_diagnosis``), verifies the proposed fix against the live core
when present, and returns a fix-preview card (``services.agent.regen_card``). The provider call
is logged as an IP-18 correction trace via T-H7's LoggingProvider.

This is the shared surface the copilot / orchestrator call on a rebuild-failure event.
LIVE SINCE T-BL22: ``add_features`` (cad_tools.py) invokes ``diagnose_failed_document`` on
every rebuild failure and attaches the fix-preview card to its failure response, and
``regen_resolve`` records the user's accept/edit/reject as a T-H1 ``ai_artifact_action``
correction trace. When no caller-supplied event sink exists (the MCP tool path), events are
journaled to ``CACHE/telemetry/events.jsonl`` (the spec_sheet_matcher precedent) so the
"every LLM call is flywheel-logged" guarantee stays real.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from services.agent.regen_diagnosis import detect_failures


def _journal_sink() -> Callable[[dict], None]:
    """Durable default event sink: append each telemetry event to the journal.

    Mirrors engine/spec_sheet_matcher._make_journal_sink — used when no caller-supplied
    sink exists (the MCP tool path), so IP-18 correction traces persist instead of being
    dropped. Failures are swallowed; telemetry must never break a call.
    """
    def _sink(event: dict) -> None:
        try:
            from engine import paths
            out = paths.CACHE / "telemetry" / "events.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass
    return _sink


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
    if on_event is None:
        # T-BL22: no caller sink (the MCP tool path) -> journal the IP-18 correction trace
        # to CACHE/telemetry/events.jsonl instead of dropping it on the floor.
        on_event = _journal_sink()

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

    @mcp.tool()
    def regen_resolve(card_json: str, action: str, edit_ops_json: str = "",
                      note: str = "") -> str:
        """Record the user's/agent's verdict on a regen fix-preview card: accept, edit, or reject.

        ``card_json``: the ``card`` object from ``regen_diagnose`` (or the ``regen_diagnosis``
        field of a failed ``add_features`` response). ``action``: ``"accept"`` | ``"edit"`` |
        ``"reject"``. ``edit_ops_json`` (edit only): the amended fix-ops JSON array, re-applied
        to the ORIGINAL corrupted document.

        Emits the T-H1 ``ai_artifact_action`` correction trace (journaled to the telemetry
        events log, correlated to the diagnosis's IP-18 artifact and carrying its verifier
        score). Returns ``{ok, action, document}`` — ``document`` is the fixed doc to load on
        accept/edit (null on reject, or when the card carries no document handles).
        """
        try:
            card = json.loads(card_json)
        except json.JSONDecodeError as e:
            return json.dumps({"ok": False, "error": f"card_json: invalid JSON — {e}"})
        if not isinstance(card, dict) or card.get("kind") != "fix_preview_card":
            return json.dumps({"ok": False,
                               "error": "card_json must be a fix_preview_card object"})
        edit_ops = None
        if edit_ops_json.strip():
            try:
                edit_ops = json.loads(edit_ops_json)
            except json.JSONDecodeError as e:
                return json.dumps({"ok": False, "error": f"edit_ops_json: invalid JSON — {e}"})
        from services.agent.regen_card import ACCEPT, EDIT, REJECT, resolve
        if action not in (ACCEPT, EDIT, REJECT):
            return json.dumps({"ok": False,
                               "error": f"action must be one of {(ACCEPT, EDIT, REJECT)}"})
        try:
            from services.agent.config import consent_tier, load_config
            consent = consent_tier(load_config())
        except Exception:  # noqa: BLE001
            consent = "meta"
        doc = resolve(card, action, _journal_sink(), edit_ops=edit_ops,
                      consent=consent, note=note)
        return json.dumps({"ok": True, "action": action, "document": doc}, default=str)
