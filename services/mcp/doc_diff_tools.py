"""MCP tool: doc_diff — semantic feature-level diff between two document revisions.

doc_diff(rev_a, rev_b) compares two canonical document JSON dicts (aicad.part/1 or
aicad.asm/1) and returns a structured delta keyed on stable ids (f*/e*/c*/k*/m*):

    features_added / features_removed / features_changed
    params_changed   [{feature, param, old, new}]
    sketch_deltas    [{feature, entities_added/removed, constraints_added/removed}]
    mate_deltas      [{id, kind, old, new}]
    suppression_flips [{id, old, new}]

Refs churn (re-numbered selector strings after paste/undo) is suppressed when the
resolved target ID is unchanged — callers must NOT treat a refs key rename as a
semantic change.

ECAD documents (``aicad.pcb/1``, ``aicad.sch/1``, ``aicad.ecad/1``) are detected by
schema and delegated to :func:`services.plm.ecad_diff.ecad_diff`, which returns the
id-keyed nets/components/footprints/tracks shape documented there instead of the
feature-tree shape above.

Consumers: correction traces, change cards, PDM-lite, GSL diff().

Registration: register_doc_diff_tools(mcp) → adds tool ``doc_diff``.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import FastMCP

from services.plm.ecad_diff import ecad_diff as _ecad_semantic_diff, is_ecad_schema


# ---------------------------------------------------------------------------
# Core diff logic (pure Python, no engine dependency)
# ---------------------------------------------------------------------------

def _by_id(items: list[dict]) -> dict[str, dict]:
    """Index a list of dicts by their 'id' field."""
    return {item["id"]: item for item in items if isinstance(item, dict) and "id" in item}


def _params_diff(fid: str, old_feature: dict, new_feature: dict) -> list[dict]:
    """Return a list of {feature, param, old, new} records for changed params."""
    old_p = old_feature.get("params", {})
    new_p = new_feature.get("params", {})
    changes = []
    all_keys = set(old_p) | set(new_p)
    for k in sorted(all_keys):
        # Skip sketch params — handled separately by _sketch_diff
        if k in ("entities", "constraints"):
            continue
        oval = old_p.get(k)
        nval = new_p.get(k)
        if oval != nval:
            changes.append({"feature": fid, "param": k, "old": oval, "new": nval})
    return changes


def _sketch_diff(fid: str, old_feature: dict, new_feature: dict) -> dict | None:
    """Return a sketch delta record if sketch entities/constraints changed."""
    old_p = old_feature.get("params", {})
    new_p = new_feature.get("params", {})
    old_ents = _by_id(old_p.get("entities", []))
    new_ents = _by_id(new_p.get("entities", []))
    old_cons = _by_id(old_p.get("constraints", []))
    new_cons = _by_id(new_p.get("constraints", []))

    ents_added = sorted(set(new_ents) - set(old_ents))
    ents_removed = sorted(set(old_ents) - set(new_ents))
    # Changed entities (same id, different content)
    ents_changed = sorted(
        eid for eid in set(old_ents) & set(new_ents)
        if old_ents[eid] != new_ents[eid]
    )
    cons_added = sorted(set(new_cons) - set(old_cons))
    cons_removed = sorted(set(old_cons) - set(new_cons))
    cons_changed = sorted(
        cid for cid in set(old_cons) & set(new_cons)
        if old_cons[cid] != new_cons[cid]
    )

    if any([ents_added, ents_removed, ents_changed,
            cons_added, cons_removed, cons_changed]):
        return {
            "feature": fid,
            "entities_added": ents_added,
            "entities_removed": ents_removed,
            "entities_changed": ents_changed,
            "constraints_added": cons_added,
            "constraints_removed": cons_removed,
            "constraints_changed": cons_changed,
        }
    return None


def _mate_diff(old_mates: dict, new_mates: dict) -> list[dict]:
    """Return mate delta records (added/removed/changed)."""
    deltas = []
    all_ids = sorted(set(old_mates) | set(new_mates))
    for mid in all_ids:
        if mid not in old_mates:
            deltas.append({"id": mid, "kind": "added", "old": None, "new": new_mates[mid]})
        elif mid not in new_mates:
            deltas.append({"id": mid, "kind": "removed", "old": old_mates[mid], "new": None})
        elif old_mates[mid] != new_mates[mid]:
            deltas.append({"id": mid, "kind": "changed",
                           "old": old_mates[mid], "new": new_mates[mid]})
    return deltas


def doc_diff(rev_a: dict, rev_b: dict) -> dict:
    """Compute a semantic feature-level diff between two document dicts.

    Numeric values are compared without quantization: even a small change to an
    axis or transform can alter the assembly constraints.

    ECAD documents (schema head aicad.pcb/aicad.sch/aicad.ecad) are delegated to
    services.plm.ecad_diff.ecad_diff, which returns an id-keyed nets/components/
    footprints/tracks shape instead of the keys documented below.

    Returns a dict with keys:
        features_added, features_removed, features_changed,
        params_changed, sketch_deltas, mate_deltas, suppression_flips.
    """
    if is_ecad_schema(rev_a) or is_ecad_schema(rev_b):
        return _ecad_semantic_diff(rev_a, rev_b)

    a = rev_a
    b = rev_b

    old_feats = _by_id(a.get("features", []))
    new_feats = _by_id(b.get("features", []))

    features_added = sorted(set(new_feats) - set(old_feats))
    features_removed = sorted(set(old_feats) - set(new_feats))

    params_changed: list[dict] = []
    sketch_deltas: list[dict] = []
    suppression_flips: list[dict] = []
    features_changed: list[str] = []

    common_ids = sorted(set(old_feats) & set(new_feats))
    for fid in common_ids:
        of = old_feats[fid]
        nf = new_feats[fid]

        # Suppression flip
        old_sup = of.get("suppressed", False)
        new_sup = nf.get("suppressed", False)
        if old_sup != new_sup:
            suppression_flips.append({"id": fid, "old": old_sup, "new": new_sup})

        # Param changes
        pchanges = _params_diff(fid, of, nf)
        if pchanges:
            params_changed.extend(pchanges)

        # Sketch entity/constraint deltas
        if of.get("type") == "sketch" or nf.get("type") == "sketch":
            sdelta = _sketch_diff(fid, of, nf)
            if sdelta:
                sketch_deltas.append(sdelta)

        # Mark feature changed if any sub-diff fired
        if pchanges or (of.get("type") == "sketch" and
                        _sketch_diff(fid, of, nf) is not None) or old_sup != new_sup:
            if fid not in features_changed:
                features_changed.append(fid)

    # Rebuild features_changed in id order
    features_changed = sorted(features_changed)

    # Mate deltas (assembly docs only)
    old_mates = _by_id(a.get("mates", []))
    new_mates = _by_id(b.get("mates", []))
    mate_deltas = _mate_diff(old_mates, new_mates)

    return deepcopy({
        "features_added": features_added,
        "features_removed": features_removed,
        "features_changed": features_changed,
        "params_changed": params_changed,
        "sketch_deltas": sketch_deltas,
        "mate_deltas": mate_deltas,
        "suppression_flips": suppression_flips,
    })


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

def register_doc_diff_tools(mcp: FastMCP) -> None:
    """Register the doc_diff MCP tool onto *mcp*."""

    @mcp.tool()
    def doc_diff_tool(
        rev_a: str,
        rev_b: str,
    ) -> str:
        """Compute a semantic feature-level diff between two document JSON revisions.

        Both ``rev_a`` and ``rev_b`` must be JSON strings containing a full document.
        For a part/asm document (``aicad.part/1`` / ``aicad.asm/1``) returns a JSON
        object with the following keys:

        - ``features_added`` / ``features_removed`` / ``features_changed``: lists
          of feature ids.
        - ``params_changed``: list of ``{feature, param, old, new}`` records.
        - ``sketch_deltas``: per-feature ``{feature, entities_added, entities_removed,
          entities_changed, constraints_added, constraints_removed, constraints_changed}``.
        - ``mate_deltas``: list of ``{id, kind, old, new}`` for assembly mates.
        - ``suppression_flips``: list of ``{id, old, new}`` suppression state changes.

        For an ECAD document (``aicad.pcb/1`` / ``aicad.sch/1`` / ``aicad.ecad/1``)
        returns ``{nets,components,footprints,tracks}_{added,removed,changed}``
        instead (services.plm.ecad_diff) — each entry carries ``id`` + ``summary``,
        and changed entries also carry a ``changes`` list of ``{field, old, new}``.

        Float noise below µm (6 dp) is suppressed so rebuilt-but-unchanged
        geometry produces an empty diff.
        """
        try:
            a = json.loads(rev_a)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"rev_a: invalid JSON — {e}"})
        try:
            b = json.loads(rev_b)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"rev_b: invalid JSON — {e}"})

        result = doc_diff(a, b)
        return json.dumps(result, indent=2, sort_keys=True)
