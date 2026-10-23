"""MCP tools for preflight dry-run validation (aicad.preflight/1).

Registered onto the FastMCP instance by ``register_preflight_tools(mcp)``.

External tool:
    ``preflight_ops(doc_id, tool, ops)`` — read-risk class; never mutates.
    Returns an aicad.preflight/1 JSON string.

Internal seam:
    ``run_auto_preflight(doc_id, tool, ops)`` — called by add_features / mutating
    tools when ``CHUDWARE_PREFLIGHT`` is on (default).  Returns the preflight
    result dict; caller attaches ``advisory_items`` to its own response and, if
    the verdict is ``fail``, calls ``log_preflight_override`` after applying.

    Prediction uses native scratch state with a copied compile-cache index, not
    a replay of the complete source history. ``skip_reason`` avoids prediction
    for batches whose verdict none of the four checks could plausibly change
    (a lone primitive with no refs, or metadata-only changes).
    Every decision — skip or run — is recorded in ``preflight_decisions()`` and
    the skip reason rides back on the envelope, so it is never silent.

Env:
    ``CHUDWARE_PREFLIGHT=0``       — off entirely (verdict "skipped", "disabled").
    ``CHUDWARE_PREFLIGHT=always``  — force prediction even for trivial batches.
    ``CHUDWARE_PREFLIGHT_MAX_OPS`` — batch size above which the gate always runs
                                     prediction (default 3).

Freedom rule: a fail verdict is advisory; engine connection loss still aborts.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .cad_common import require_list

_log = logging.getLogger(__name__)


# ------------------------------------------------------------------ complexity gate

# Metadata-only ops: no geometry or refs for the rebuild to predict.
# Op kinds are the closed vocabulary in core/doc/op_registry.cpp;
# everything NOT listed here runs prediction.
_METADATA_OPS: frozenset[str] = frozenset({
    "rename", "set_units", "set_material", "set_appearance", "set_folder", "eq_set",
})

# Primitive solids whose whole definition is a handful of positive numbers.
_SIMPLE_FEATURE_TYPES: frozenset[str] = frozenset({
    "box", "cylinder", "sphere", "cone", "torus",
})

# Dimension params _check_params_sane would flag as non-positive.  A batch that
# trips this locally still runs the real preflight so the model gets the advisory.
_DIM_KEYS: frozenset[str] = frozenset({
    "radius", "length", "width", "height", "depth", "diameter", "thickness",
    "r1", "r2", "major_r", "minor_r", "size",
})

# Boolean/subtractive placement benefits from native rebuild prediction.
_SUBTRACTIVE_MODES: frozenset[str] = frozenset({
    "cut", "subtract", "intersect", "common", "remove",
})

_DEFAULT_MAX_OPS = 3

# Last N gate decisions, newest last — observability for a behaviour change that
# is otherwise invisible from the add_features response.
_DECISIONS: deque = deque(maxlen=32)


def preflight_decisions() -> list[dict]:
    """The last gate decisions (oldest first): {doc_id, tool, n_ops, ran, reason}."""
    return list(_DECISIONS)


def _gate_max_ops() -> int:
    try:
        return max(1, int(os.environ.get("CHUDWARE_PREFLIGHT_MAX_OPS", "")
                          or _DEFAULT_MAX_OPS))
    except ValueError:
        return _DEFAULT_MAX_OPS


def _num(raw: Any) -> Optional[float]:
    """Unwrap the {"value": x, "expr": ...} dimension form; None if not numeric."""
    v = raw.get("value") if isinstance(raw, dict) else raw
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def skip_reason(ops: Any) -> Optional[str]:
    """Why native scratch prediction is pointless for *ops* — or None to run it.

    Runs prediction for anything preflight can actually rule on: a ref to resolve
    (``refs_resolve``), a cut/boolean/shell/fillet/chamfer/pattern or any sketch,
    assembly, delete, reorder, rollback, suppress or paste op (``rebuild_predict``
    / ``downstream_impact``), a non-positive dimension (``params_sane``), or a
    batch bigger than ``CHUDWARE_PREFLIGHT_MAX_OPS``.
    """
    if not isinstance(ops, list):
        return None                      # unknown shape — let the real thing look
    if not ops:
        return "empty op batch"
    limit = _gate_max_ops()
    if len(ops) > limit:
        return None
    for op in ops:
        if not isinstance(op, dict):
            return None
        kind = str(op.get("op") or "")
        if kind in _METADATA_OPS:
            continue
        if kind != "add_feature":
            return None                  # sketch_*/asm_*/delete/reorder/rollback/…
        feat = op.get("feature")
        if not isinstance(feat, dict):
            return None
        if feat.get("refs"):
            return None                  # something to resolve → refs_resolve earns it
        if str(feat.get("type") or "") not in _SIMPLE_FEATURE_TYPES:
            return None                  # cut/shell/fillet/chamfer/pattern/sketch/…
        params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
        mode = str(feat.get("mode") or params.get("mode") or "").strip().lower()
        if mode in _SUBTRACTIVE_MODES:
            return None
        for key, raw in params.items():
            if key in _DIM_KEYS:
                v = _num(raw)
                if v is not None and v <= 0:
                    return None          # params_sane has something to say
    return (f"{len(ops)} cheap op(s): primitive/metadata only, no refs, "
            f"no boolean, sketch or destructive step")


def _record(doc_id: str, tool: str, ops: Any, ran: bool, reason: str) -> None:
    n = len(ops) if isinstance(ops, list) else -1
    _DECISIONS.append({"doc_id": doc_id, "tool": tool, "n_ops": n,
                       "ran": ran, "reason": reason})
    _log.debug("preflight gate: doc=%s tool=%s n_ops=%s ran=%s (%s)",
               doc_id, tool, n, ran, reason)


def _skipped(doc_id: str, tool: str, reason: str, detail: str) -> dict:
    return {
        "schema": "aicad.preflight/1",
        "id": f"pf_{reason}",
        "target": {"doc_id": doc_id, "tool": tool, "ops_hash": ""},
        "verdict": "skipped",
        "skipped": reason,
        "skipped_detail": detail,
        "checks": [],
        "predicted": {},
        "downstream": [],
        "advisory_items": [],
    }


# ------------------------------------------------------------------ public seam

def run_auto_preflight(doc_id: str, tool: str, ops: list) -> dict:
    """Run preflight automatically inside mutating tools.

    Returns an aicad.preflight/1 dict; engine connection loss propagates. ``verdict`` is "skipped"
    with a ``skipped``/``skipped_detail`` pair when the run was suppressed —
    ``disabled`` (CHUDWARE_PREFLIGHT=0), ``trivial`` (complexity gate, see
    :func:`skip_reason`) or ``error``.  ``CHUDWARE_PREFLIGHT=always`` forces the
    prediction past the gate for debugging.
    """
    mode = os.environ.get("CHUDWARE_PREFLIGHT", "1").strip().lower()
    if mode in ("0", "false", "off", "no"):
        _record(doc_id, tool, ops, False, "disabled")
        return _skipped(doc_id, tool, "disabled", "CHUDWARE_PREFLIGHT=0")
    forced = mode in ("always", "force", "all", "2")
    if not forced:
        why = skip_reason(ops)
        if why:
            _record(doc_id, tool, ops, False, why)
            return _skipped(doc_id, tool, "trivial", why)
    try:
        from services.agent.advisor.preflight import run_preflight
        result = run_preflight(doc_id, tool, ops)
        _record(doc_id, tool, ops, True,
                f"forced (CHUDWARE_PREFLIGHT={mode})" if forced else "non-trivial batch")
        return result
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        _record(doc_id, tool, ops, False, f"error: {exc}")
        return _skipped(doc_id, tool, "error", str(exc))


def log_preflight_override(doc_id: str, tool: str, pf_result: dict) -> None:
    """Log a preflight_overridden event when a failed preflight is applied.

    Call this AFTER a mutating tool applies ops when ``pf_result["verdict"] == "fail"``.
    """
    try:
        from services.agent.advisor.preflight import log_override
        log_override(
            doc_id=doc_id,
            tool=tool,
            pf_id=pf_result.get("id", "pf_unknown"),
            verdict=pf_result.get("verdict", "unknown"),
        )
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ registration

def register_preflight_tools(mcp: FastMCP) -> None:
    """Register the ``preflight_ops`` MCP tool onto *mcp*."""

    @mcp.tool()
    def preflight_ops(
        ops: list,
        doc_id: Optional[str] = None,
        tool: str = "add_features",
    ) -> str:
        """Dry-run planned ops in cached native scratch state.

        Checks planned ops BEFORE they touch the document:
        - ``refs_resolve``: stable ids / names in ops exist in the current tree.
        - ``params_sane``: local positive dimension values. Geometry feasibility is
          decided by the full native rebuild, not a radius/wall-size heuristic.
        - ``rebuild_predict``: native candidate feature_status and document status,
          without changing live geometry, history, revisions or cache state.
        - ``downstream_impact``: live features referencing entities you are editing.

        Returns an ``aicad.preflight/1`` JSON object.  ``verdict`` is one of:
        ``pass``, ``warn``, ``fail``, or ``skipped`` (budget exceeded or disabled).
        A ``fail`` verdict is advisory — it does NOT block; the caller decides
        whether to proceed (freedom rule, §2.10).

        **Read-only**: this tool NEVER mutates the live document.  It is safe to
        call speculatively before any destructive edit.
        Existing history is not cold-replayed. One cumulative 10 s candidate budget
        covers every native geometry probe and response serialization/transfer.
        POSIX runs the rebuild in a private child, kills and reaps it on timeout,
        and returns ``verdict:"skipped", skipped:"budget_exceeded",
        still_running:false`` — never a geometry/radius failure. Unsupported
        platforms return ``skipped:"isolation_unavailable"`` rather than execute
        an unbounded fallback. Lock/metadata/transport waits, OS scheduling and
        process cleanup are not a hard end-to-end wall-clock guarantee. Applying
        real edits still performs complete, unchanged native geometry validation.

        Typical use:
            1. Before ``add_features``: call ``preflight_ops(ops)`` to catch
               dangling refs, impossible fillets, or downstream breaks.
            2. Read ``advisory_items`` — severity ``action`` items need attention,
               ``hint`` items are warnings.
            3. Call ``add_features(ops)`` as normal.  If preflight said fail and
               you proceed anyway, a ``preflight_overridden`` audit event is logged.
        """
        # Relative imports: resolve to the SAME module identity this package was
        # loaded under (services.mcp or services.mcp). An absolute services.mcp
        # import here creates a second cad_tools/core_bridge module object with
        # its own _CUR/_PERIODIC_TIMER/fallback state — the 12x-autosave bug class.
        from . import core_bridge as cb
        from .cad_tools import _CUR, _DOC_ALIASES

        def _resolve(did: Optional[str]) -> str:
            if did:
                return _DOC_ALIASES.get(did, did)
            cur = _CUR.get("doc_id")
            if not cur:
                raise ValueError("No current document — pass doc_id or call new_part() first")
            return cur

        # A stringified ops batch (JSON or Python-repr) used to sail straight into
        # run_preflight/_run, which downgraded advisory errors into a "warn" item whose
        # detail was a raw internal AttributeError instead of ever validating the batch.
        # Coerce at THIS entry point so the model gets a teaching error, not a wasted
        # dry-run that silently skipped every check.
        try:
            ops = require_list(ops, "ops",
                                'a list of ops, e.g. [{"op":"add_feature","feature":{...}}]')
        except ValueError as e:
            return json.dumps({"error": str(e)})

        try:
            d = _resolve(doc_id)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        try:
            from services.agent.advisor.preflight import run_preflight
            result = run_preflight(d, tool, ops)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc)})
