"""services.plm.ecad_diff — semantic diff for canonical ECAD documents (PLM ext).

KiCad files are imported by the native ECAD engine before reaching this module.
The diff covers board and schematic entities as well as the original
nets/components/footprints/tracks change-list sections.

Output mirrors doc_diff's envelope style: three lists per section —
``<section>_added`` / ``<section>_removed`` / ``<section>_changed`` — each
entry carrying its stable ``id`` and a short human ``summary``; changed
entries additionally carry a ``changes`` list of ``{field, old, new}``
records (mirrors doc_diff's ``params_changed``). Numeric values are compared
without quantization, matching lossless canonical document persistence.

Consumers: services/plm/compare.py ``semantic_compare`` (routes here instead
of ``doc_diff`` for any document whose schema head is aicad.pcb/aicad.sch/
aicad.ecad) and services/mcp/doc_diff_tools.py ``doc_diff`` (same routing, so
the standalone ``doc_diff`` MCP tool also answers correctly for ECAD docs).
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


# Importers assign sequential internal ids on each open. KiCad UUIDs are the
# stable identities across revisions; nets have names but no KiCad UUID.
_SECTIONS = ("nets", "components", "footprints", "tracks", "vias", "zones", "outline",
             "symbols", "wires", "junctions", "labels", "buses", "bus_entries",
             "power", "no_connect", "texts", "graphics", "sheets", "sheet_symbols")

_SINGULAR = {"nets": "net", "components": "component", "footprints": "footprint",
             "tracks": "track", "vias": "via", "zones": "zone", "outline": "outline",
             "symbols": "symbol",
             "wires": "wire", "junctions": "junction", "labels": "label",
             "buses": "bus", "bus_entries": "bus entry", "power": "power symbol",
             "no_connect": "no-connect", "texts": "text", "graphics": "graphic",
             "sheets": "sheet", "sheet_symbols": "sheet symbol"}

# schema heads recognized as ECAD documents (mirrors
# services/plm/branches.py:_list_keys_for_doc's aicad.pcb/aicad.sch/aicad.ecad group).
_ECAD_SCHEMA_HEADS = ("aicad.pcb", "aicad.sch", "aicad.ecad")


def is_ecad_schema(doc: dict[str, Any] | None) -> bool:
    """True when *doc*'s ``schema`` head names an ECAD document."""
    schema = str((doc or {}).get("schema") or "")
    return schema.split("/", 1)[0] in _ECAD_SCHEMA_HEADS


def _by_id(section: str, items: Any) -> dict[str, dict]:
    """Index by UUID, native id or net name; legacy outline shapes use position."""
    if not isinstance(items, list):
        return {}
    indexed = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        fields = item.get("fields")
        legacy_uuid = fields.get("kicad_uuid") if isinstance(fields, dict) else None
        key = item.get("uuid") or legacy_uuid or item.get("id")
        if not key and section == "nets":
            key = item.get("name")
        if not key and section == "outline":
            key = f"outline-{index + 1}"
        if isinstance(key, str) and key:
            indexed[key] = item
    return indexed


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
    for key in ("ref", "name", "text", "value"):
        v = entity.get(key)
        if v:
            return str(v)
    return entity_id


def _comparable(value: Any) -> Any:
    """Ignore native import bookkeeping and regenerated ids beneath UUIDs."""
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    if not isinstance(value, dict):
        return value
    fields = value.get("fields")
    has_uuid = value.get("uuid") or (isinstance(fields, dict) and fields.get("kicad_uuid"))
    return {key: _comparable(item) for key, item in value.items()
            if key != "kicad_original" and not (key == "id" and has_uuid)}


def _field_changes(old: dict, new: dict) -> list[dict[str, Any]]:
    """Top-level field diffs between two same-id entities (skips ``id``)."""
    changes = []
    for k in sorted(set(old) | set(new)):
        if k in ("id", "kicad_original"):
            continue
        ov, nv = old.get(k), new.get(k)
        if _comparable(ov) != _comparable(nv):
            changes.append({"field": k, "old": ov, "new": nv})
    return changes


def _section_diff(section: str, old_list: Any, new_list: Any) -> dict[str, list]:
    old = _by_id(section, old_list)
    new = _by_id(section, new_list)
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
    """Compute a stable-identity semantic diff between imported ECAD documents.

    Returns added/removed/changed lists for each board and schematic section.
    Records carry a stable id and human summary; changed records also carry
    ``{field, old, new}`` entries. Missing sections produce empty lists.
    """
    a = rev_a if isinstance(rev_a, dict) else {}
    b = rev_b if isinstance(rev_b, dict) else {}

    out: dict[str, Any] = {}
    for section in _SECTIONS:
        old_section, new_section = a.get(section), b.get(section)
        if section == "outline":
            old_section = old_section.get("shapes") if isinstance(old_section, dict) else []
            new_section = new_section.get("shapes") if isinstance(new_section, dict) else []
        out.update(_section_diff(section, old_section, new_section))
    return deepcopy(out)


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
