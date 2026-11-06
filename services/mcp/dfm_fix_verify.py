"""Verify proposed operations on a private detached document, never on the source."""
from __future__ import annotations

from services.mcp import core_bridge as cb
from services.mcp.dfm_common import ShadowSource, num


def verify_fix_ops(doc_id, ops, measure=None):
    """Build ops in a shadow clone and optionally measure both documents.

    ``measure`` is a callable accepting a document ID and returning JSON evidence.
    A successful build alone is not proof of an intended geometric effect; callers
    must compare before and after when claiming an effect was verified.
    """
    result = {"builds": None, "before": None, "after": None,
              "side_effects": {"volume_delta_mm3": None, "failed_features": [],
                               "fit_and_clearance": "not_evaluated",
                               "assembly_interference": "not_evaluated"}, "error": None}
    try:
        revision = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("revision")
        if revision is None:
            raise ValueError("Source revision unavailable")
        if measure is not None:
            result["before"] = measure(doc_id)
        try:
            before_volume = num((cb.call("eval_mass", {"doc_id": doc_id}) or {}).get("volume_mm3"))
        except Exception:  # noqa: BLE001 -- volume is optional evidence
            before_volume = None
        with ShadowSource(doc_id) as source:
            with source.clone() as clone:
                response = cb.call("doc_apply_ops", {"doc_id": clone, "ops": ops, "with_tess": False}) or {}
                rebuild = response.get("rebuild") or {}
                statuses = rebuild.get("feature_status") or []
                failed = [s for s in statuses if isinstance(s, dict)
                          and (s.get("ok") is False or s.get("rolled_back") is True)]
                result["side_effects"]["failed_features"] = failed
                result["side_effects"]["feature_status"] = statuses
                if (response.get("ok") is False or response.get("rejected_tree")
                        or rebuild.get("ok") is False or failed or not statuses):
                    result["builds"] = False
                    result["error"] = response.get("error") or "Clone rebuild failed or returned no feature status"
                else:
                    result["builds"] = True
                    if measure is not None:
                        result["after"] = measure(clone)
                    try:
                        after_volume = num((cb.call("eval_mass", {"doc_id": clone}) or {}).get("volume_mm3"))
                        if before_volume is not None and after_volume is not None:
                            result["side_effects"]["volume_delta_mm3"] = after_volume - before_volume
                    except Exception:  # noqa: BLE001 -- volume is optional evidence
                        pass
        current = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("revision")
        if current != revision:
            result["builds"] = None
            result["error"] = "Source revision changed during verification"
    except Exception as exc:  # noqa: BLE001 -- a failed measurement is never verification
        result["builds"] = None
        result["error"] = str(exc)
    return result


def verify_row_fixes(ctx, row, producer):
    """Keep only bounded proposals that clear the same finding on a detached clone.

    ``producer`` receives a fresh DfmContext and returns the corresponding row.
    A changed status is geometric evidence only to the extent of that row's basis.
    """
    from dataclasses import replace

    proposals = row.get("fix_ops") or []
    if not proposals:
        return row
    if ctx.options.get("verify_fixes", True) is False:
        return {**row, "fix_ops": [], "fix_verification_rejected": [{"reason": "Fix verification disabled"}]}
    kept = []
    rejected = []
    for proposal in proposals[:5]:
        ops = proposal.get("ops") if isinstance(proposal, dict) else None
        if ops is None and isinstance(proposal, dict) and proposal.get("op"):
            ops = [proposal]
        if not isinstance(ops, list) or not ops:
            rejected.append({"reason": "Proposal has no executable operations"})
            continue

        def measure(doc):
            fresh = replace(ctx, doc_id=doc,
                            options={**ctx.options, "verify_fixes": False}, _cache={})
            return producer(fresh)

        result = verify_fix_ops(ctx.doc_id, ops, measure)
        before = result.get("before") or {}
        after = result.get("after") or {}
        if (result.get("builds") is True and before.get("check") == row.get("check")
                and after.get("check") == row.get("check")
                and before.get("status") in ("fail", "warn") and after.get("status") == "pass"
                and not before.get("error") and not after.get("error")):
            kept.append({**proposal, "verified": True,
                         "verification": {"before": before.get("status"), "after": after.get("status"),
                                          "side_effects": result["side_effects"]}})
        else:
            rejected.append({"reason": result.get("error") or
                             "The same check did not change from fail/warn to pass",
                             "before": before.get("status"), "after": after.get("status")})
    if len(proposals) > 5:
        rejected.append({"reason": "Verification cap reached", "count": len(proposals) - 5})
    return {**row, "fix_ops": kept, "fix_verification_rejected": rejected}
