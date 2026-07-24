"""services.plm.ecad_diff — semantic diff for canonical ECAD documents (PLM ext).

``doc_diff`` (services/mcp/doc_diff_tools.py) only understands the part/asm
feature-tree shape (``features``/``mates``), so comparing an ECAD item through
``plm_compare`` used to fall through to empty change lists. :func:`ecad_diff`
fills that gap for the ECAD document family (``aicad.pcb/1``, ``aicad.sch/1``,
``aicad.ecad/1`` — contracts/ecad/{pcb,sch}_schema.md): an id-based diff over
the same ordered/id-keyed sections branches.py's 3-way merge treats specially
(``_LIST_KEYS_ECAD``, services/plm/branches.py:47-48) — here narrowed to the
four sections a PLM change list actually surfaces per-entry: nets, components,
footprints, tracks. This module is independent of branches.py (no merge
internals imported); it only mirrors the same section names/id convention.

Output mirrors doc_diff's envelope style: three lists per section —
``<section>_added`` / ``<section>_removed`` / ``<section>_changed`` — each
entry carrying its stable ``id`` and a short human ``summary``; changed
entries additionally carry a ``changes`` list of ``{field, old, new}``
records (mirrors doc_diff's ``params_changed``). Float noise below µm (6dp)
is suppressed the same way doc_diff does it, via
``services.plm.canonical.round_floats_doc``.

Consumers: services/plm/compare.py ``semantic_compare`` (routes here instead
of ``doc_diff`` for any document whose schema head is aicad.pcb/aicad.sch/
aicad.ecad) and services/mcp/doc_diff_tools.py ``doc_diff`` (same routing, so
the standalone ``doc_diff`` MCP tool also answers correctly for ECAD docs).
"""
from __future__ import annotations

from typing import Any

from services.plm.canonical import round_floats_doc

# The id-keyed ECAD sections this diff surfaces individually (the subset of
# branches.py's _LIST_KEYS_ECAD a PLM change list calls out by name; vias/
# zones/pads fold into their parent footprint/track entry's field diff).
_SECTIONS = ("nets", "components", "footprints", "tracks")

_SINGULAR = {"nets": "net", "components": "component",
            "footprints": "footprint", "tracks": "track"}

# schema heads recognized as ECAD documents (mirrors
# services/plm/branches.py:_list_keys_for_doc's aicad.pcb/aicad.sch/aicad.ecad group).
_ECAD_SCHEMA_HEADS = ("aicad.pcb", "aicad.sch", "aicad.ecad")


def is_ecad_schema(doc: dict[str, Any] | None) -> bool:
    """True when *doc*'s ``schema`` head names an ECAD document."""
    schema = str((doc or {}).get("schema") or "")
    return schema.split("/", 1)[0] in _ECAD_SCHEMA_HEADS


def _by_id(items: Any) -> dict[str, dict]:
    """Index a section's list by its 'id' field ({} for a missing/non-list section)."""
    if not isinstance(items, list):
        return {}
    return {item["id"]: item for item in items if isinstance(item, dict) and "id" in item}


def _label(section: str, entity: dict, entity_id: str) -> str:
    """A short display label for one entity — prefers the field a person would
    recognize (ref designator / net name) over the raw id."""
    if section == "tracks":
        net = entity.get("net")
        layer = entity.get("layer")
        if net and layer:
            return f"{entity_id} ({net}/{layer})"
        if net:
            return f"{entity_id} ({net})"
        return entity_id
    for key in ("ref", "name", "value"):
        v = entity.get(key)
        if v:
            return str(v)
    return entity_id


def _field_changes(old: dict, new: dict) -> list[dict[str, Any]]:
    """Top-level field diffs between two same-id entities (skips ``id``)."""
    changes = []
    for k in sorted(set(old) | set(new)):
        if k == "id":
            continue
        ov, nv = old.get(k), new.get(k)
        if ov != nv:
            changes.append({"field": k, "old": ov, "new": nv})
    return changes


def _section_diff(section: str, old_list: Any, new_list: Any) -> dict[str, list]:
    old = _by_id(old_list)
    new = _by_id(new_list)
    singular = _SINGULAR[section]

    added = []
    for eid in sorted(set(new) - set(old)):
        label = _label(section, new[eid], eid)
        added.append({"id": eid, "summary": f"{singular} {label} added"})

    removed = []
    for eid in sorted(set(old) - set(new)):
        label = _label(section, old[eid], eid)
        removed.append({"id": eid, "summary": f"{singular} {label} removed"})

    changed = []
    for eid in sorted(set(old) & set(new)):
        if old[eid] == new[eid]:
            continue
        changes = _field_changes(old[eid], new[eid])
        if not changes:
            continue
        label = _label(section, new[eid], eid)
        fields = ", ".join(c["field"] for c in changes)
        changed.append({"id": eid, "summary": f"{singular} {label} changed ({fields})",
                        "changes": changes})

    return {f"{section}_added": added, f"{section}_removed": removed,
            f"{section}_changed": changed}


def ecad_diff(rev_a: dict[str, Any], rev_b: dict[str, Any]) -> dict[str, Any]:
    """Compute an id-based semantic diff between two ECAD document dicts.

    Both inputs should be canonical ECAD documents (contracts/ecad/{pcb,sch}
    _schema.md); float noise below 6dp is suppressed internally via
    ``round_floats_doc`` so a rebuilt-but-unchanged board/schematic produces
    an empty diff (mirrors doc_diff — services/mcp/doc_diff_tools.py).

    Returns a dict with three lists per section (nets/components/footprints/
    tracks): ``<section>_added`` / ``<section>_removed`` / ``<section>_changed``.
    Every entry carries its stable id and a short human summary; changed
    entries additionally carry a ``changes`` list of ``{field, old, new}``.
    A section absent from both documents (e.g. ``tracks`` on a schematic)
    simply comes back with three empty lists.
    """
    a = round_floats_doc(rev_a)
    b = round_floats_doc(rev_b)
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}

    out: dict[str, Any] = {}
    for section in _SECTIONS:
        out.update(_section_diff(section, a.get(section), b.get(section)))
    return out


_MAX_SUMMARY_CLAUSES = 6


def summarize_ecad_diff(diff: dict[str, Any]) -> str:
    """Plain-language one-line summary of an :func:`ecad_diff` result — same
    cadence as ``services.plm.summarize.summarize_diff`` (deterministic,
    git-vocabulary-free per contract §8.1); reuses each entry's own summary
    clause rather than re-deriving one."""
    clauses: list[str] = []
    for section in _SECTIONS:
        for kind in ("added", "removed", "changed"):
            for rec in diff.get(f"{section}_{kind}", []) or []:
                clauses.append(rec["summary"])

    if not clauses:
        return "No design changes."

    overflow = len(clauses) > _MAX_SUMMARY_CLAUSES
    text = "; ".join(clauses[:_MAX_SUMMARY_CLAUSES])
    text = text[0].upper() + text[1:]
    if overflow:
        text += "; and other changes"
    return text + "."
