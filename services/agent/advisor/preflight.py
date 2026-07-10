"""Preflight — dry-run plan validation on shadow doc clones (aicad.preflight/1).

Pillar 20 §2.10 + contracts/advisor.md §10.  Shape frozen 2026-07-04.

Every mutating-tool family can call ``run_preflight(doc_id, tool, ops)`` before
touching the live document.  The function:

1.  Serialises the live doc via ``doc_tree`` (read-only).
2.  Creates a throwaway clone via ``doc_new`` + ``doc_apply_ops``.
3.  Applies the *planned* ops on the clone.
4.  Runs the four check classes (refs_resolve, params_sane, rebuild_predict,
    downstream_impact).
5.  Returns an ``aicad.preflight/1`` envelope with advisory_items ready for
    injection.
6.  Closes the throwaway clone — zero live-doc side effects guaranteed by
    structure (the live doc_id is never passed to a mutating RPC).

Freedom rule: ``verdict: "fail"`` NEVER blocks the caller.  Applying a
failed-preflight mutation without a dispute is a ``preflight_overridden`` ledger
event (audit trigger per §11).

Performance guard: clone+apply is skipped for docs exceeding the size threshold
(``_CLONE_BUDGET_S = 2.0`` s wall-clock, threshold ``_FEATURE_COUNT_LIMIT``).
Skips emit ``verdict: "skipped"`` with ``skipped: "budget"`` — never silent.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

# ------------------------------------------------------------------ constants

_FEATURE_COUNT_LIMIT: int = int(os.environ.get("AI_CAD_PREFLIGHT_FEATURE_LIMIT", "200"))
_CLONE_BUDGET_S: float = float(os.environ.get("AI_CAD_PREFLIGHT_BUDGET_S", "2.0"))

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
        "AI_CAD_PREFLIGHT_LEDGER",
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
    """Dry-run *ops* on a shadow clone of *doc_id*.

    Returns an ``aicad.preflight/1`` dict.  Never raises — any unexpected error
    degrades to a single ``warn`` item so the caller always gets a usable envelope.

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

    # 1. Read live doc state (read-only — live doc_id never used for mutation).
    live_tree_resp = cb.call("doc_tree", {"doc_id": doc_id})
    live_revision_before = live_tree_resp.get("revision", -1)
    live_tree = live_tree_resp.get("tree") or {}

    # Performance guard: skip large docs rather than silently slow down.
    features = live_tree.get("features") or []
    if len(features) > _FEATURE_COUNT_LIMIT:
        return _skipped_envelope(doc_id, tool, ops, reason="budget",
                                 detail=f"doc has {len(features)} features (limit {_FEATURE_COUNT_LIMIT})")

    # 2. Build throwaway clone via serialization (never in-place).
    clone_id: str | None = None
    try:
        clone_id, clone_ops = _clone_doc(live_tree, cb)
        t0 = time.monotonic()

        try:
            cb.call("doc_apply_ops", {"doc_id": clone_id, "ops": clone_ops})
        except CoreError:
            pass  # malformed original; clone_id is still a valid (empty) doc

        elapsed = time.monotonic() - t0

        # Performance guard on clone+apply wall time.
        if elapsed > _CLONE_BUDGET_S:
            _close_clone(clone_id, cb)
            return _skipped_envelope(doc_id, tool, ops, reason="budget",
                                     detail=f"clone+apply took {elapsed:.1f}s (limit {_CLONE_BUDGET_S}s)")

        # 3. Run four check classes.
        checks: list[dict] = []
        predicted: dict = {}
        downstream: list[dict] = []

        checks.extend(_check_refs_resolve(ops, clone_id, cb))
        checks.extend(_check_params_sane(ops, clone_id, cb))
        checks.extend(_check_rebuild_predict(ops, clone_id, cb, CoreError, predicted))
        downstream = _check_downstream_impact(ops, clone_id, cb)
        if downstream:
            checks.append({
                "check": "downstream_impact",
                "result": "warn",
                "detail": f"{len(downstream)} downstream feature(s) reference edited entities",
                "refs": {"features": [d["feature"] for d in downstream]},
            })
        else:
            checks.append({
                "check": "downstream_impact",
                "result": "pass",
                "detail": "no downstream features affected",
                "refs": {},
            })

        # 4. Verify live doc is unchanged (assertion — belt+suspenders).
        live_tree_after = cb.call("doc_tree", {"doc_id": doc_id})
        live_revision_after = live_tree_after.get("revision", -1)
        if live_revision_after != live_revision_before:
            # Something went wrong — report it but do not block.
            checks.append({
                "check": "refs_resolve",
                "result": "warn",
                "detail": (
                    f"INTERNAL: live doc revision changed during preflight "
                    f"({live_revision_before} -> {live_revision_after}); "
                    "preflight results may be invalid"
                ),
                "refs": {},
            })

    finally:
        if clone_id:
            _close_clone(clone_id, cb)

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


# ------------------------------------------------------------------ clone helpers

def _clone_doc(live_tree: dict, cb: Any) -> tuple[str, list[dict]]:
    """Create a throwaway doc and return (clone_id, ops_to_replay).

    The clone is a new empty part; we rebuild it by replaying the original tree's
    equations and features in order — the same pattern used by the verify bench.
    """
    name = (live_tree.get("name") or "preflight_clone") + "_pf"
    r = cb.call("doc_new", {"kind": "part", "name": name})
    clone_id = r.get("doc_id") or r.get("id")
    if not clone_id:
        raise RuntimeError("doc_new returned no doc_id")

    ops: list[dict] = []
    # Equations first (features may reference them).
    for eq in (live_tree.get("equations") or {}).get("globals") or []:
        ops.append({"op": "eq_set", "name": eq["name"], "expr": str(eq.get("expr", eq.get("value", 0)))})
    # Features in tree order.
    for feat in (live_tree.get("features") or []):
        ops.append({"op": "add_feature", "feature": feat})

    return clone_id, ops


def _close_clone(clone_id: str, cb: Any) -> None:
    # cb.call dispatches ENGINE RPCs — the engine verb is `doc_close` (see cad_tools.close_part,
    # which itself calls doc_close). `close_part` is an MCP *tool* name, not an engine RPC, so the
    # old call raised "unknown method" every time and the throwaway preflight clone leaked forever
    # (auto-preflight fires on every add_features → dozens of stale `_pf` docs pinned in the engine).
    try:
        cb.call("doc_close", {"doc_id": clone_id})
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ check runners

def _check_refs_resolve(ops: list[dict], clone_id: str, cb: Any) -> list[dict]:
    """Check that stable ids/names referenced in planned ops exist in the clone."""
    checks = []
    missing: list[str] = []

    try:
        tree_resp = cb.call("doc_tree", {"doc_id": clone_id})
        tree = tree_resp.get("tree") or {}
        known_ids: set[str] = set()
        known_names: set[str] = set()
        for f in (tree.get("features") or []):
            if f.get("id"):
                known_ids.add(f["id"])
            if f.get("name"):
                known_names.add(f["name"])
    except Exception:  # noqa: BLE001
        # If doc_tree fails, skip this check rather than false-fail.
        return []

    for op in ops:
        if not isinstance(op, dict):
            continue
        feat = op.get("feature") or {}
        refs = feat.get("refs") or {}
        for _key, ref_val in (refs.items() if isinstance(refs, dict) else []):
            if isinstance(ref_val, dict):
                ref_id = ref_val.get("id") or ref_val.get("feature")
                if ref_id and not ref_id.startswith("$") and \
                   ref_id not in known_ids and ref_id not in known_names:
                    missing.append(ref_id)

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


def _check_params_sane(ops: list[dict], clone_id: str, cb: Any) -> list[dict]:
    """Check parameters for obvious invalidity (negative dims, fillet vs wall, hole depth vs extent)."""
    issues: list[str] = []

    for op in ops:
        if not isinstance(op, dict) or op.get("op") != "add_feature":
            continue
        feat = op.get("feature") or {}
        ftype = feat.get("type", "")
        params = feat.get("params") or {}
        fname = feat.get("name", ftype)

        # Positive dimension checks.
        for pname, pval in params.items():
            v = pval.get("value") if isinstance(pval, dict) else pval
            if isinstance(v, (int, float)) and v <= 0 and pname in (
                "radius", "length", "width", "height", "depth", "diameter", "thickness"
            ):
                issues.append(f"{fname}.{pname}={v} must be positive")

        # Fillet / chamfer: radius should not exceed adjacent wall (best-effort via measure).
        if ftype in ("fillet", "chamfer"):
            r_val = params.get("radius") or params.get("size")
            radius = (r_val.get("value") if isinstance(r_val, dict) else r_val) if r_val else None
            if radius and isinstance(radius, (int, float)) and radius > 0:
                try:
                    meas = cb.call("doc_eval_measure", {"doc_id": clone_id,
                                                         "target": {"of": "wall", "adjacent": True}})
                    wall = meas.get("min_thickness") or meas.get("thickness")
                    if wall and isinstance(wall, (int, float)) and radius >= wall:
                        issues.append(
                            f"{fname}: radius {radius} >= adjacent wall {wall:.3f} on clone"
                        )
                except Exception:  # noqa: BLE001
                    pass  # measure may not apply — skip, not a hard fail

        # Hole: depth should not exceed body extent (best-effort).
        if ftype in ("hole", "counterbore", "countersink"):
            d_val = params.get("depth")
            depth = (d_val.get("value") if isinstance(d_val, dict) else d_val) if d_val else None
            if depth and isinstance(depth, (int, float)) and depth > 0:
                try:
                    meas = cb.call("doc_eval_measure", {"doc_id": clone_id,
                                                         "target": {"of": "body", "axis": "all"}})
                    extents = meas.get("extents") or {}
                    max_extent = max(
                        (extents.get(k) or 0) for k in ("x", "y", "z")
                    )
                    if max_extent > 0 and depth > max_extent:
                        issues.append(
                            f"{fname}: hole depth {depth} > body max extent {max_extent:.3f}"
                        )
                except Exception:  # noqa: BLE001
                    pass

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
        "detail": "parameters look sane",
        "refs": {},
    }]


def _check_rebuild_predict(
    ops: list[dict],
    clone_id: str,
    cb: Any,
    CoreError: type,
    predicted: dict,
) -> list[dict]:
    """Apply planned ops on clone; capture feature_status and solver dof."""
    try:
        resp = cb.call("doc_apply_ops", {"doc_id": clone_id, "ops": ops})
    except CoreError as exc:
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
            "detail": f"unexpected error during clone apply: {exc}",
            "refs": {},
        }]

    rebuild = resp.get("rebuild") or {}
    fs = rebuild.get("feature_status") or []
    predicted["feature_status"] = [
        {"id": s.get("id"), "ok": s.get("ok"),
         "error": s.get("error"), "warning": s.get("warning")}
        for s in fs
    ]
    predicted["dof"] = rebuild.get("dof")
    predicted["interference_delta"] = None  # populated by downstream check if needed

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
        "detail": f"all {len(fs)} feature(s) rebuild ok on clone",
        "refs": {},
    }]


def _check_downstream_impact(ops: list[dict], clone_id: str, cb: Any) -> list[dict]:
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
        fid = feat.get("id")
        fname = feat.get("name")
        if op.get("op") in ("edit_feature", "set_param") and fid:
            edited_ids.add(fid)
        if op.get("op") == "add_feature" and fname:
            edited_ids.add(fname)

    if not edited_ids:
        return []

    downstream: list[dict] = []
    try:
        tree_resp = cb.call("doc_tree", {"doc_id": clone_id})
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
    except Exception:  # noqa: BLE001
        pass

    # Also try doc_adjacency if available.
    for eid in edited_ids:
        try:
            adj = cb.call("doc_adjacency", {"doc_id": clone_id, "feature": eid})
            for dep in (adj.get("dependents") or []):
                dep_id = dep.get("id") or dep.get("feature") or ""
                if dep_id and dep_id not in edited_ids:
                    entry = {
                        "feature": dep_id,
                        "relation": f"references edited face f_{eid}",
                    }
                    if entry not in downstream:
                        downstream.append(entry)
        except Exception:  # noqa: BLE001
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
