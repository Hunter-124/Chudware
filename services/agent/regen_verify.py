"""Live-engine verification of a regen fix candidate (IP-18).

A fix is judged by rebuilding it in a hidden scratch document on the native engine and, when a
known-good revision exists, comparing the geometry. Shared by the ``regen_diagnose`` tool and
the regen-fix bench (``tools/flywheel/bench/benches/regen_fix.py``); production code must not
import from the bench.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

LIVE_MATCH_THRESHOLD = 99.5  # match_pct >= this == "unaffected bodies compare ≈100%"


def core_error(e: BaseException) -> str:
    detail = getattr(e, "detail", None)
    if isinstance(detail, dict) and detail.get("reason"):
        return f"{type(e).__name__}: {e} ({detail['reason']})"[:200]
    return f"{type(e).__name__}: {e}"[:200]


def document_ops(tree: dict) -> list[dict]:
    """Replay source geometry, not compiled/readback fields hoisted as feature params."""
    if tree.get("schema", "aicad.part/1") != "aicad.part/1":
        raise ValueError("regen verifier requires a part document")
    if tree.get("rollback_after"):
        raise ValueError("regen verifier cannot replay an active rollback bar with doc ops")
    ops = []
    if tree.get("units"):
        ops.append({"op": "set_units", "system": tree["units"]["system"]})
    if tree.get("material"):
        ops.append({"op": "set_material", "key": tree["material"]["key"]})
    ops.extend({"op": "eq_set", "name": g["name"], "expr": g["expr"]}
               for g in tree.get("equations", {}).get("globals", []))
    features = tree.get("features", [])
    mapping = {f["id"]: f"${len(ops) + i}" for i, f in enumerate(features)}

    def remap(value):
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        return value

    for feature in features:
        envelope = {k: copy.deepcopy(feature[k])
                    for k in ("type", "name", "suppressed", "params", "refs", "folder")
                    if k in feature}
        for key in ("params", "refs"):
            if key in envelope:
                envelope[key] = remap(envelope[key])
        ops.append({"op": "add_feature", "feature": envelope})
    return ops


def make_live_verifier():
    """Return (opener, compare_fn) bound to the C++ core, or (None, None) when it can't be
    located/loaded — never a hardcoded path (resolution is core_bridge's, via
    $CHUDWARE_LIBCHUDWARE / $CHUDWARE_DIST / build dirs).

        opener(document) -> {"doc_id": str|None, "rebuilt": bool, "error": str|None}
        compare_fn(a, b) -> match_pct float|None   (100 == identical; None == degraded)
    """
    try:
        from services.mcp import core_bridge as cb  # Core-only path.
    except Exception:
        return None, None
    if not cb.available():
        return None, None

    def close(doc_id: str) -> None:
        cb.call("doc_close", {"doc_id": doc_id})

    def opener(document: dict) -> dict:
        tree = document.get("tree", document)
        doc_id = None
        try:
            ops = document_ops(tree)
            doc_id = cb.call("doc_new", {"kind": "part", "name": tree.get("name", "regen_doc"),
                                         "internal": True})["doc_id"]
            resp = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops, "with_tess": False})
            authored = iter(tree.get("features", []))
            source_ids = {}
            for index, op in enumerate(ops):
                if op["op"] == "add_feature":
                    feature = next(authored)
                    minted = (resp.get("id_map") or {}).get(f"${index}")
                    if minted:
                        source_ids[minted] = feature["id"]
            fs = (resp.get("rebuild", {}) or {}).get("feature_status") or resp.get("feature_status") or []
            bad = [f'{source_ids.get(s.get("id"), s.get("id"))}: {s.get("error")}'
                   for s in fs if not s.get("ok", True)]
            if bad:
                close(doc_id)
                return {"doc_id": None, "rebuilt": False, "error": "; ".join(bad)}
            return {"doc_id": doc_id, "rebuilt": True, "error": None}
        except Exception as e:
            if doc_id:
                close(doc_id)
            return {"doc_id": None, "rebuilt": False, "error": core_error(e)}

    opener.close = close

    def compare_fn(a: str, b: str):
        try:
            r = cb.call("eval_compare", {"doc_a": a, "doc_b": b})
        except Exception:
            return None
        return None if r.get("degraded") else r.get("match_pct")

    return opener, compare_fn


def verification_error(detail: dict, why: str):
    """The live verifier itself could not judge the fix: a grader/infra ERROR, never a pass.
    It does not fall back to structural checks — those would credit a repair whose rebuild
    nobody observed."""
    detail["verdict"] = "error"
    detail["live_error"] = why[:200]
    return 0.0, False, detail


def verify_live(fixed: dict, claimed: bool, reference: dict, *, opener, compare_fn,
                detail: Optional[dict] = None):
    """Binary live verdict. Returns (score{0.0|1.0}, passed, detail).

    Pass iff the strategy CLAIMED a fix, the fix rebuilds, the *reference* (a known-good
    revision) rebuilds, and compare_parts of the two is >= LIVE_MATCH_THRESHOLD. A fix that
    rebuilds but altered geometry scores 0. When the verifier itself cannot judge (an
    opener/compare exception, the reference not rebuilding, a degraded compare) the result is
    ``detail["verdict"] == "error"``: not a pass, and callers that distinguish "unverified"
    from "failed" read that verdict.
    """
    detail = {} if detail is None else detail
    detail["claimed_fixed"] = claimed
    detail["mode"] = "live"
    opened = []
    try:
        try:
            res = opener(fixed)
            if res.get("doc_id"):
                opened.append(res["doc_id"])
        except Exception as e:  # noqa: BLE001
            return verification_error(detail, f"opener(fix) {type(e).__name__}: {e}")
        detail["rebuilt"] = bool(res.get("rebuilt"))
        if res.get("error"):
            detail["rebuild_error"] = str(res["error"])[:200]
        if not (claimed and res.get("rebuilt") and res.get("doc_id")):
            detail["verdict"] = "fail"
            return 0.0, False, detail
        try:
            good_res = opener(reference)
            if good_res.get("doc_id"):
                opened.append(good_res["doc_id"])
        except Exception as e:  # noqa: BLE001
            return verification_error(detail, f"opener(known-good) {type(e).__name__}: {e}")
        if not (good_res.get("rebuilt") and good_res.get("doc_id")):
            return verification_error(
                detail, f"known-good reference did not rebuild: {good_res.get('error')}")
        try:
            match = compare_fn(res["doc_id"], good_res["doc_id"])
        except Exception as e:  # noqa: BLE001
            return verification_error(detail, f"compare {type(e).__name__}: {e}")
        detail["match_pct"] = match
        if match is None:
            return verification_error(detail, "eval_compare degraded (no match_pct)")
        passed = bool(match >= LIVE_MATCH_THRESHOLD)
        detail["verdict"] = "pass" if passed else "fail"
        return (1.0 if passed else 0.0), passed, detail
    finally:
        closer = getattr(opener, "close", None)
        if closer:
            for doc_id in opened:
                closer(doc_id)


def rebuild_only(document: dict, *, opener) -> dict:
    """Does the candidate rebuild? Reports the rebuild alone; its geometry is NOT checked."""
    res = opener(document)
    try:
        return {"mode": "live-rebuild-only", "rebuilt": bool(res.get("rebuilt")),
                "rebuild_error": res.get("error")}
    finally:
        closer = getattr(opener, "close", None)
        if closer and res.get("doc_id"):
            closer(res["doc_id"])
