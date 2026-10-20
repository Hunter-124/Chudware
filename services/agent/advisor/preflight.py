"""Preflight — cached native scratch validation (aicad.preflight/1).

Pillar 20 §2.10 + contracts/advisor.md §10.  Shape frozen 2026-07-04.

Every mutating-tool family can call ``run_preflight(doc_id, tool, ops)`` before
touching the live document.  The function:

1. Reads the live tree and runs reference/parameter/downstream checks.
2. Predicts the candidate through ``doc_dry_run_ops``: an unregistered native
   scratch session with a private copy of the live incremental-cache index.
3. Returns an ``aicad.preflight/1`` envelope with advisory items.

No registered clone, history replay, mesh publication, or live mutation occurs.

Freedom rule: ``verdict: "fail"`` NEVER blocks the caller.  Applying a
failed-preflight mutation without a dispute is a ``preflight_overridden`` ledger
event (audit trigger per §11).

The feature-count guard and a cumulative 10 s native candidate budget emit explicit
skips. POSIX kills/reaps the private rebuild child at expiry; unsupported platforms
skip rather than run an unbounded fallback. Metadata/lock/transport waits and OS
scheduling/cleanup are not a hard end-to-end deadline.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import time
from typing import Any

# ------------------------------------------------------------------ constants

_FEATURE_COUNT_LIMIT: int = int(os.environ.get("CHUDWARE_PREFLIGHT_FEATURE_LIMIT", "200"))
_REBUILD_BUDGET_MS = 10_000

# ------------------------------------------------------------------ ledger shim
#
# If T-W1 (design-ledger) is merged, import its append function.  Until then use
# a local JSONL shim with the same aicad.ledger/1 event shape so T-W13/W5 can
# consume records without coupling to the merge order.

def _ledger_append(event: dict) -> None:
    """Append one ledger event.  Swapped for the real ledger once T-W1 merges."""
    try:
        from services.mcp.intake_ledger import append_event  # type: ignore[import]
        append_event(event)
        return
    except Exception:  # noqa: BLE001
        pass
    # Local JSONL shim — same event shape as aicad.ledger/1.
    shim_path = os.environ.get(
        "CHUDWARE_PREFLIGHT_LEDGER",
        str(_repo_root() / "preflight_events.jsonl"),
    )
    try:
        import pathlib
        p = pathlib.Path(shim_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _repo_root():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ public API

def run_preflight(
    doc_id: str,
    tool: str,
    ops: list[dict],
) -> dict:
    """Dry-run *ops* in a cached native scratch session for *doc_id*.

    Returns an ``aicad.preflight/1`` dict. Unexpected advisory errors degrade to
    a warn item; engine connection loss propagates because document identity is uncertain.

    Parameters
    ----------
    doc_id:
        The live document to clone (read-only throughout).
    tool:
        Name of the mutating tool (e.g. ``"add_features"``), recorded in the
        preflight target for traceability.
    ops:
        The planned ops list the tool *would* apply.
    """
    try:
        return _run(doc_id, tool, ops)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return _envelope(
            doc_id=doc_id, tool=tool, ops=ops,
            verdict="warn",
            checks=[{
                "check": "refs_resolve",
                "result": "warn",
                "detail": f"preflight internal error: {exc}",
                "refs": {},
            }],
            predicted={},
            downstream=[],
        )


def log_override(doc_id: str, tool: str, pf_id: str, verdict: str) -> None:
    """Emit a ``preflight_overridden`` ledger event (audit trigger §11).

    Called by the mutating-tool seam when a failed preflight is applied without
    an ``advisory_dispute``.
    """
    import datetime
    _ledger_append({
        "seq": None,  # filled by ledger impl
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "actor": "preflight",
        "kind": "preflight_overridden",
        "payload": {
            "doc_id": doc_id,
            "tool": tool,
            "preflight_id": pf_id,
            "preflight_verdict": verdict,
        },
    })


# ------------------------------------------------------------------ internals

def _ops_hash(ops: list) -> str:
    raw = json.dumps(ops, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _run(doc_id: str, tool: str, ops: list[dict]) -> dict:
    from services.mcp import core_bridge as cb
    from services.mcp.core_bridge import CoreError

    deadline = time.monotonic() + _REBUILD_BUDGET_MS / 1000
    # 1. Read live doc state (read-only — live doc_id never used for mutation).
    live_tree_resp = cb.call("doc_tree", {"doc_id": doc_id})
    live_revision_before = live_tree_resp.get("revision", -1)
    live_tree = live_tree_resp.get("tree") or {}

    # Performance guard: skip large docs rather than silently slow down.
    features = live_tree.get("features") or []
    if len(features) > _FEATURE_COUNT_LIMIT:
        return _skipped_envelope(doc_id, tool, ops, reason="budget",
                                 detail=f"doc has {len(features)} features (limit {_FEATURE_COUNT_LIMIT})")

    checks: list[dict] = []
    predicted: dict = {}
    checks.extend(_check_refs_resolve(ops, doc_id, cb))
    checks.extend(_check_params_sane(ops))
    downstream = _check_downstream_impact(ops, doc_id, cb)
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        return _skipped_envelope(doc_id, tool, ops, reason="budget_exceeded",
                                 detail="preflight budget exhausted before native rebuild")
    checks.extend(_check_rebuild_predict(ops, doc_id, cb, CoreError, predicted,
                                         budget_ms=remaining_ms))
    if predicted.get("skipped"):
        return _skipped_envelope(doc_id, tool, ops, reason=predicted["skipped"],
                                 detail=checks[-1]["detail"])
    checks.append({
        "check": "downstream_impact",
        "result": "warn" if downstream else "pass",
        "detail": (f"{len(downstream)} downstream feature(s) reference edited entities"
                   if downstream else "no downstream features affected"),
        "refs": {"features": [d["feature"] for d in downstream]} if downstream else {},
    })

    # Other clients may edit between read-only RPCs. A prediction then describes
    # a different revision than the checks; never advertise it as a clean pass.
    live_tree_after = cb.call("doc_tree", {"doc_id": doc_id})
    live_revision_after = live_tree_after.get("revision", -1)
    source_revision = predicted.pop("source_revision", live_revision_before)
    if live_revision_after != live_revision_before or source_revision != live_revision_before:
        checks.append({
            "check": "rebuild_predict",
            "result": "warn",
            "detail": "live revision changed during preflight; prediction may be stale",
            "refs": {},
        })

    # 5. Compute overall verdict.
    results = [c["result"] for c in checks]
    if "fail" in results:
        verdict = "fail"
    elif "warn" in results:
        verdict = "warn"
    else:
        verdict = "pass"

    return _envelope(
        doc_id=doc_id, tool=tool, ops=ops,
        verdict=verdict,
        checks=checks,
        predicted=predicted,
        downstream=downstream,
    )



# ------------------------------------------------------------------ check runners

def _check_refs_resolve(ops: list[dict], doc_id: str, cb: Any) -> list[dict]:
    """Check stable ids/names against the live document, before candidate ops."""
    checks = []
    missing: list[str] = []

    try:
        tree_resp = cb.call("doc_tree", {"doc_id": doc_id})
        tree = tree_resp.get("tree") or {}
        known_ids: set[str] = set()
        known_names: set[str] = set()
        for f in (tree.get("features") or []):
            if f.get("id"):
                known_ids.add(f["id"])
            if f.get("name"):
                known_names.add(f["name"])
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        # If doc_tree fails, skip this check rather than false-fail.
        return []

    # The queried tree is the LIVE tree — the planned ops have not been
    # applied — so a ref to a feature an earlier op in this same batch
    # creates is legitimate. Accumulate declarations as we walk (mirrors
    # guardrails.check_refs_resolve_ids, which hard-blocks on the same rule).
    # RESERVED_DATUMS comes from the same module for the same reason: a kernel
    # datum ("plane.top", "axis.z") has no feature to find in any tree.
    from services.agent.guardrails import RESERVED_DATUMS, declared_by_op

    declared: set[str] = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        feat = op.get("feature") or {}
        refs = feat.get("refs") or {}
        for _key, ref_val in (refs.items() if isinstance(refs, dict) else []):
            if isinstance(ref_val, dict):
                ref_id = ref_val.get("id") or ref_val.get("feature")
                if ref_id and not ref_id.startswith("$") and \
                   ref_id not in known_ids and ref_id not in known_names and \
                   ref_id not in declared and ref_id not in RESERVED_DATUMS:
                    missing.append(ref_id)
        declared |= declared_by_op(op)

    if missing:
        checks.append({
            "check": "refs_resolve",
            "result": "fail",
            "detail": f"dangling refs: {', '.join(missing)}",
            "refs": {"features": missing},
        })
    else:
        checks.append({
            "check": "refs_resolve",
            "result": "pass",
            "detail": "all refs resolve",
            "refs": {},
        })
    return checks





def _check_params_sane(ops: list[dict]) -> list[dict]:
    """Check local positive dimensions; native rebuild checks geometry feasibility."""
    issues: list[str] = []

    for op in ops:
        if not isinstance(op, dict) or op.get("op") != "add_feature":
            continue
        feat = op.get("feature") or {}
        params = feat.get("params") or {}
        fname = feat.get("name", feat.get("type", "feature"))

        # Positive dimension checks.
        for pname, pval in params.items():
            v = pval.get("value") if isinstance(pval, dict) else pval
            if isinstance(v, (int, float)) and v <= 0 and pname in (
                "radius", "length", "width", "height", "depth", "diameter", "thickness"
            ):
                issues.append(f"{fname}.{pname}={v} must be positive")


    if issues:
        return [{
            "check": "params_sane",
            "result": "fail",
            "detail": "; ".join(issues),
            "refs": {},
        }]
    return [{
        "check": "params_sane",
        "result": "pass",
        "detail": "local dimension values look sane; geometry feasibility is checked by rebuild_predict",
        "refs": {},
    }]


def _check_rebuild_predict(
    ops: list[dict],
    doc_id: str,
    cb: Any,
    CoreError: type,
    predicted: dict,
    *,
    budget_ms: int = _REBUILD_BUDGET_MS,
) -> list[dict]:
    """Run candidate ops in native scratch state and retain rebuild diagnostics."""
    try:
        resp = cb.call("doc_dry_run_ops", {"doc_id": doc_id, "ops": ops,
                                          "budget_ms": budget_ms})
    except CoreError as exc:
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        predicted["feature_status"] = []
        predicted["dof"] = None
        return [{
            "check": "rebuild_predict",
            "result": "fail",
            "detail": f"schema/ref error (CoreError): {exc}",
            "refs": {},
        }]
    except Exception as exc:  # noqa: BLE001
        predicted["feature_status"] = []
        predicted["dof"] = None
        return [{
            "check": "rebuild_predict",
            "result": "warn",
            "detail": f"unexpected error during native dry-run: {exc}",
            "refs": {},
        }]

    if resp.get("skipped"):
        predicted["skipped"] = resp["skipped"]
        return [{
            "check": "rebuild_predict", "result": "skipped",
            "detail": str(resp.get("error") or "native rebuild was not completed"),
            "refs": {},
        }]

    if "source_revision" in resp:
        predicted["source_revision"] = resp["source_revision"]
    rebuild = resp.get("rebuild") or {}
    fs = rebuild.get("feature_status") or []
    predicted["feature_status"] = [
        {"id": s.get("id"), "ok": s.get("ok"),
         "error": s.get("error"), "warning": s.get("warning")}
        for s in fs
    ]
    predicted["dof"] = rebuild.get("dof")
    predicted["interference_delta"] = None  # populated by downstream check if needed
    predicted["doc_status"] = rebuild.get("doc_status")

    failed = [s for s in fs if not s.get("ok")]
    warned = [s for s in fs if s.get("ok") and s.get("warning")]

    if failed:
        detail = "; ".join(
            f"{s.get('id','?')}: {s.get('error','?')}" for s in failed
        )
        return [{
            "check": "rebuild_predict",
            "result": "fail",
            "detail": f"{len(failed)} feature(s) fail rebuild: {detail}",
            "refs": {"features": [s.get("id") for s in failed]},
        }]
    if resp.get("ok") is False:
        return [{
            "check": "rebuild_predict", "result": "fail",
            "detail": str(resp.get("error") or "native dry-run refused candidate"),
            "refs": {},
        }]
    if resp.get("ok") is not True or not isinstance(rebuild.get("feature_status"), list):
        return [{
            "check": "rebuild_predict", "result": "warn",
            "detail": "native dry-run did not provide candidate rebuild diagnostics",
            "refs": {},
        }]
    if warned:
        detail = "; ".join(
            f"{s.get('id','?')}: {s.get('warning','?')}" for s in warned
        )
        return [{
            "check": "rebuild_predict",
            "result": "warn",
            "detail": f"{len(warned)} feature(s) have warnings: {detail}",
            "refs": {"features": [s.get("id") for s in warned]},
        }]
    return [{
        "check": "rebuild_predict",
        "result": "pass",
        "detail": f"all {len(fs)} feature(s) rebuild ok in native scratch state",
        "refs": {},
    }]


def _check_downstream_impact(ops: list[dict], doc_id: str, cb: Any) -> list[dict]:
    """Walk the feature tree for features referencing entities touched by the planned ops.

    Returns a list of downstream impact entries: [{feature, relation}, ...].
    Uses doc_adjacency and tree refs; best-effort (degrades to empty on any error).
    """
    # Collect the feature ids / names being edited.
    edited_ids: set[str] = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        feat = op.get("feature") or {}
        fid = op.get("id") or feat.get("id")
        fname = feat.get("name")
        if op.get("op") in ("edit_feature", "set_param") and fid:
            edited_ids.add(fid)
        if op.get("op") == "add_feature" and fname:
            edited_ids.add(fname)

    if not edited_ids:
        return []

    downstream: list[dict] = []
    try:
        tree_resp = cb.call("doc_tree", {"doc_id": doc_id})
        tree = tree_resp.get("tree") or {}
        for feat in (tree.get("features") or []):
            fid = feat.get("id") or ""
            fname = feat.get("name") or ""
            if fid in edited_ids or fname in edited_ids:
                continue  # skip the feature itself

            refs = feat.get("refs") or {}
            # Walk refs recursively for any reference to an edited id/name.
            if _refs_touch(refs, edited_ids):
                downstream.append({
                    "feature": fid or fname,
                    "relation": "references edited feature",
                })
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        pass

    # Also try doc_adjacency if available.
    for eid in edited_ids:
        try:
            adj = cb.call("doc_adjacency", {"doc_id": doc_id, "feature": eid})
            for dep in (adj.get("dependents") or []):
                dep_id = dep.get("id") or dep.get("feature") or ""
                if dep_id and dep_id not in edited_ids:
                    entry = {
                        "feature": dep_id,
                        "relation": f"references edited face f_{eid}",
                    }
                    if entry not in downstream:
                        downstream.append(entry)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) == "engine_connection_lost":
                raise
            pass

    return downstream


def _refs_touch(refs: Any, edited_ids: set) -> bool:
    """Recursively check if any ref value names an edited id/name."""
    if isinstance(refs, dict):
        for k, v in refs.items():
            if k in ("id", "feature", "of") and isinstance(v, str) and v in edited_ids:
                return True
            if _refs_touch(v, edited_ids):
                return True
    elif isinstance(refs, list):
        return any(_refs_touch(item, edited_ids) for item in refs)
    return False


# ------------------------------------------------------------------ envelope builders

def _envelope(
    *,
    doc_id: str,
    tool: str,
    ops: list,
    verdict: str,
    checks: list[dict],
    predicted: dict,
    downstream: list[dict],
) -> dict:
    pf_id = f"pf_{uuid.uuid4().hex[:12]}"
    advisory_items = _checks_to_advisory_items(checks, pf_id)
    return {
        "schema": "aicad.preflight/1",
        "id": pf_id,
        "target": {
            "doc_id": doc_id,
            "tool": tool,
            "ops_hash": _ops_hash(ops),
        },
        "verdict": verdict,
        "checks": checks,
        "predicted": predicted,
        "downstream": downstream,
        "advisory_items": advisory_items,
    }


def _skipped_envelope(doc_id: str, tool: str, ops: list, reason: str, detail: str) -> dict:
    pf_id = f"pf_{uuid.uuid4().hex[:12]}"
    return {
        "schema": "aicad.preflight/1",
        "id": pf_id,
        "target": {
            "doc_id": doc_id,
            "tool": tool,
            "ops_hash": _ops_hash(ops),
        },
        "verdict": "skipped",
        "skipped": reason,
        "skipped_detail": detail,
        **({"budget_ms": _REBUILD_BUDGET_MS, "timed_out": reason == "budget_exceeded",
            "still_running": False} if reason in
           ("budget_exceeded", "isolation_unavailable", "isolation_failed") else {}),
        "checks": [],
        "predicted": {},
        "downstream": [],
        "advisory_items": [],
    }


def _checks_to_advisory_items(checks: list[dict], pf_id: str) -> list[dict]:
    """Map check results to aicad.advisory/1 items (severity: fail->action, warn->hint)."""
    items = []
    for c in checks:
        result = c.get("result", "pass")
        if result == "pass":
            continue
        severity = "action" if result == "fail" else "hint"
        items.append({
            "id": f"adv_{uuid.uuid4().hex[:8]}",
            "severity": severity,
            "check": c.get("check", "preflight"),
            "finding": c.get("detail", ""),
            "refs": c.get("refs") or {},
            "numbers": {},
            "candidate_fixes": [],
            "helper_tools": [],
            "source": {
                "kind": "kernel",
                "detail": f"preflight/{pf_id}",
            },
        })
    return items
