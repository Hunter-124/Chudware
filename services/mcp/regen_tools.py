"""IP-18 regen-diagnosis MCP surface — the callable backend seam for rebuild-failure repair.

When a parametric document fails to rebuild, ``diagnose_failed_document`` runs the IP-18
diagnosis (``services.agent.regen_diagnosis``), verifies the proposed fix against the live core
when present, and returns a fix-preview card (``services.agent.regen_card``). The model call
is logged as an IP-18 correction trace via the agent LoggingProvider.

The model step is one tool-free completion through the Chudcode runtime
(``providers.chudcode_provider.helper_provider``); an explicit offline/local-only
configuration binds an offline-safe provider instead. When the step cannot run, the card
still carries the detected failures and the runtime's reason.

This is the shared surface the copilot / orchestrator call on a rebuild-failure event.
LIVE: ``add_features`` (cad_tools.py) invokes ``diagnose_failed_document`` on
every rebuild failure and attaches the fix-preview card to its failure response, and
``regen_resolve`` records the user's accept/edit/reject as an ``ai_artifact_action``
correction trace. When no caller-supplied event sink exists (the MCP tool path), events are
journaled to ``CACHE/telemetry/events.jsonl`` (the spec_sheet_matcher precedent) so the
"every LLM call is flywheel-logged" guarantee stays real.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from services.agent.chudcode_host import HelperCompletionError
from services.agent.providers.base import ProviderUnreachable
from services.agent.regen_diagnosis import detect_failures, diagnostic_failures


def _journal_sink() -> Callable[[dict], None]:
    """Durable default event sink: append each telemetry event to the journal.

    Mirrors engine/spec_sheet_matcher._make_journal_sink — used when no caller-supplied
    sink exists (the MCP tool path), so IP-18 correction traces persist instead of being
    dropped. Failures are swallowed; telemetry must never break a call.
    """
    def _sink(event: dict) -> None:
        try:
            from services import paths
            out = paths.CACHE / "telemetry" / "events.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass
    return _sink


def _resolve_provider(cancel: Optional[Callable[[], bool]] = None):
    """The helper provider for the fixer's model step (see ``helper_provider``)."""
    from services.agent.providers.chudcode_provider import helper_provider
    return helper_provider("fixer", cancel=cancel)


def _verify(document: dict, diagnosis, reference_document: Optional[dict]):
    """Score the proposed fix with the strongest available signal:
      * core + reference doc  -> binary rebuild AND compare_parts vs the last-good revision;
      * core, no reference    -> does the fix rebuild? (no known-good to compare an in-the-wild
                                 failure against);
      * no core               -> structural only; native-only failures remain unverified
    Returns (score|None, detail)."""
    claimed = diagnosis.n_applied > 0
    try:
        from tools.flywheel.bench.benches import regen_fix as RF
        opener, compare_fn = RF._make_live_verifier()
    except Exception:
        opener = compare_fn = None

    if opener is None:
        if any(f.kind == "native_rebuild" for f in diagnostic_failures(document)):
            return None, {"mode": "unavailable", "reason": "native rebuild verification unavailable"}
        cleared = not detect_failures(diagnosis.fixed_document)
        return (1.0 if (claimed and cleared) else 0.0), {"mode": "fixture", "anomaly_gone": cleared}

    if reference_document is not None:
        score, _passed, detail = RF.verify_fix(
            diagnosis.fixed_document, claimed, {},
            good_document=reference_document, opener=opener, compare_fn=compare_fn)
        return score, detail

    res = opener(diagnosis.fixed_document)
    try:
        return (1.0 if (claimed and res["rebuilt"]) else 0.0), {
            "mode": "live-rebuild-only", "rebuilt": res["rebuilt"], "rebuild_error": res.get("error")}
    finally:
        closer = getattr(opener, "close", None)
        if closer and res.get("doc_id"):
            closer(res["doc_id"])


def _no_llm_result(document: dict, reason: str) -> dict:
    """The honest ``ok=False`` card: no model was consulted, but the failures still are.

    ``detect_failures`` is pure analysis, so a missing or unreachable provider costs the
    caller the PROPOSED FIX, not the diagnosis. Returning the failure list keeps the tool
    useful and gives an agent something concrete to act on.
    """
    return {
        "ok": False,
        "reason": reason,
        "failures": [f.to_dict() for f in diagnostic_failures(document)],
    }


def diagnose_failed_document(document: dict, *, provider=None,
                             reference_document: Optional[dict] = None,
                             engine_error: Optional[str] = None,
                             on_event=None, consent_fn=None, verify: bool = True,
                             cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Diagnose a failed-rebuild ``document`` and return a fix-preview card.

    ``document`` is a full aicad.part/1 doc or a ``{revision, tree, owner_map}`` wrapper.
    ``reference_document`` (optional) is the last known-good revision, enabling the compare_parts
    drift check. ``provider`` defaults to the helper provider (Chudcode runtime online, the
    bound offline-safe provider offline); ``cancel`` stops its model step. When the model step
    cannot be set up or does not answer, the tool returns ``ok=False`` with the detected
    failures (still useful) and the reason, rather than guessing.
    """
    prov = provider
    if prov is None:
        try:
            prov = _resolve_provider(cancel)
        except Exception as e:  # noqa: BLE001 -- offline refusal / role resolution is result data
            return _no_llm_result(
                document, f"no model available for regen diagnosis: {type(e).__name__}: {e}")
    if on_event is None:
        # No caller sink (the MCP tool path) -> journal the IP-18 correction trace
        # to CACHE/telemetry/events.jsonl instead of dropping it on the floor.
        on_event = _journal_sink()

    from services.agent.regen_diagnosis import diagnose
    from services.agent.regen_card import build_fix_preview

    try:
        d = diagnose(document, prov, engine_error=engine_error,
                     on_event=on_event, consent_fn=consent_fn)
    except ProviderUnreachable as e:
        # A configured-but-down endpoint (typically a local LM Studio that is not
        # running) must not turn a diagnosis into a crash: failure DETECTION needs no
        # model, so hand back the same honest card the no-provider path returns.
        return _no_llm_result(document, f"provider unreachable: {e}")
    except HelperCompletionError as e:
        # The runtime's own reason: cancelled, over budget, model rejected, provider error.
        return _no_llm_result(document, f"model step failed: {e}")
    score, detail = _verify(document, d, reference_document) if verify else (None, {})
    card = build_fix_preview(document, d, verifier_score=score, verifier_detail=detail)
    return {"ok": True, "artifact_id": d.artifact_id, "card": card}


def register_regen_tools(mcp) -> None:
    """Register the ``regen_diagnose`` MCP tool onto *mcp* (append-only, IP-18)."""

    @mcp.tool()
    def regen_diagnose(document_json: str, reference_json: str = "",
                       engine_error: str = "", cancel=None) -> str:
        """Diagnose a document that FAILED TO REBUILD and return a fix-preview card the user can
        accept/edit/reject.

        ``document_json``: the failed document as JSON — a full ``aicad.part/1`` doc or a
        ``{"revision","tree","owner_map"}`` wrapper. ``reference_json`` (optional): the last
        known-good revision, enabling a compare_parts drift check on the proposed fix.
        ``engine_error`` (optional): the engine's feature_status error text, added to the
        diagnosis context.

        Returns a JSON card: ``{ok, artifact_id, card:{diagnosis, fix_ops, changes, semantic_diff,
        verifier, actions}}``. The fix ops are editable doc-ops (R1 parity). The call is logged as
        an IP-18 correction trace. ``ok=false`` (with the detected failures and the reason) when
        the model step is unavailable or fails.
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
            document, reference_document=reference, engine_error=engine_error or None,
            cancel=cancel if callable(cancel) else None)
        return json.dumps(result, default=str)

    @mcp.tool()
    def regen_resolve(card_json: str, action: str, edit_ops_json: str = "",
                      note: str = "") -> str:
        """Record the user's/agent's verdict on a regen fix-preview card: accept, edit, or reject.

        ``card_json``: the ``card`` object from ``regen_diagnose`` (or the ``regen_diagnosis``
        field of a failed ``add_features`` response). ``action``: ``"accept"`` | ``"edit"`` |
        ``"reject"``. ``edit_ops_json`` (edit only): the amended fix-ops JSON array, re-applied
        to the ORIGINAL corrupted document.

        Emits the ``ai_artifact_action`` correction trace (journaled to the telemetry
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
