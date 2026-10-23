"""services.plm.planning — the project planning store (schema ``aicad.planning/1``).

The "plan the work" half of PLM: tasks (a kanban board), issues (a bug/defect
log linkable to items + revisions), and milestones (a roadmap tied to releases).
It lives IN the project vault — under a ``planning/`` sibling dir in the shadow
tree — so it is versioned with the design, travels on sync, freezes at release,
and restores with everything else, for free.

This is a SEPARATE schema from the frozen ``aicad.plm/1`` manifest, carried in
its own files, so the manifest contract is untouched (contract `plm.md` top:
additive sibling dirs readers MUST ignore). This module owns the shapes + pure
helpers; the vault does the shadow-tree I/O.

Future-proofing (2026-07-07): every *actor* (creator/assignee) is an
object ``{kind, id, display}`` with ``kind:"profile"`` today; an ``org_member``
kind attaches later with no migration. Every mutation is routed through
:mod:`permissions` (allow-all today) so organization permissions bolt on at one
seam. An ``external`` field on tasks/issues reserves the deferred external-tracker
link (:mod:`trackers`).

No user-facing git vocabulary (contract §8.1) — these are plain project artifacts.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

SCHEMA = "aicad.planning/1"

TASK_STATUS = ("todo", "doing", "done", "blocked")
TASK_PRIORITY = ("p0", "p1", "p2", "p3")
ISSUE_SEVERITY = ("low", "med", "high", "critical")
ISSUE_STATE = ("open", "closed")
MILESTONE_STATUS = ("open", "done")

# entity registry: id prefix, the collection key inside its file, the file name.
ENTITIES: dict[str, dict[str, str]] = {
    "task":      {"prefix": "t", "key": "tasks",      "file": "planning/tasks.json"},
    "issue":     {"prefix": "b", "key": "issues",     "file": "planning/issues.json"},
    "milestone": {"prefix": "m", "key": "milestones", "file": "planning/milestones.json"},
}


class PlanningError(Exception):
    """Raised with a contract reason code on an invalid planning mutation."""

    def __init__(self, reason: str, hint: str = "") -> None:
        super().__init__(hint or reason)
        self.reason = reason
        self.hint = hint


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8]


def file_name(entity: str) -> str:
    _require_entity(entity)
    return ENTITIES[entity]["file"]


def all_files() -> list[str]:
    return [d["file"] for d in ENTITIES.values()]


def _require_entity(entity: str) -> None:
    if entity not in ENTITIES:
        raise PlanningError("invalid_mode",
                            f"unknown planning kind: {entity!r}")


def empty_collection(entity: str) -> dict[str, Any]:
    _require_entity(entity)
    return {"schema": SCHEMA, ENTITIES[entity]["key"]: []}


def load(raw: bytes | None, entity: str) -> dict[str, Any]:
    """Parse a collection file (or return an empty one). Forward-tolerant:
    rejects an unknown MAJOR schema, ignores unknown keys."""
    _require_entity(entity)
    if not raw:
        return empty_collection(entity)
    coll = json.loads(raw.decode("utf-8"))
    schema = str(coll.get("schema", ""))
    if schema and not schema.startswith("aicad.planning/"):
        raise PlanningError("invalid_mode", "not a planning file")
    coll.setdefault(ENTITIES[entity]["key"], [])
    return coll


def dump(collection: dict[str, Any]) -> bytes:
    return json.dumps(collection, indent=2, sort_keys=True).encode("utf-8")


def entities(collection: dict[str, Any], entity: str) -> list[dict[str, Any]]:
    return collection.get(ENTITIES[entity]["key"], [])


def normalize_actor(actor: dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerce an actor to the ``{kind, id, display}`` object shape (None passes
    through — an unassigned task/issue)."""
    if actor is None:
        return None
    if isinstance(actor, str):
        return {"kind": "profile", "id": actor, "display": actor}
    return {
        "kind": actor.get("kind", "profile"),
        "id": actor.get("id", ""),
        "display": actor.get("display") or actor.get("id", ""),
    }


# ---------------------------------------------------------------------------
# CRUD (pure — the vault persists the returned collection)
# ---------------------------------------------------------------------------

def create(collection: dict[str, Any], entity: str, data: dict[str, Any],
           actor: dict[str, Any] | None, now: str) -> dict[str, Any]:
    """Append a new entity to *collection*; returns the created entity."""
    _require_entity(entity)
    ent = _build(entity, data or {}, actor, now)
    collection.setdefault(ENTITIES[entity]["key"], []).append(ent)
    return ent


def update(collection: dict[str, Any], entity: str, ent_id: str,
           data: dict[str, Any], now: str) -> dict[str, Any]:
    """Apply *data* to an existing entity in place; returns the updated entity."""
    _require_entity(entity)
    ent = _find(collection, entity, ent_id)
    if ent is None:
        raise PlanningError("not_found", "that item no longer exists")
    _apply_fields(entity, ent, data or {})
    ent["updated_at"] = now
    return ent


def delete(collection: dict[str, Any], entity: str, ent_id: str) -> None:
    _require_entity(entity)
    key = ENTITIES[entity]["key"]
    before = len(collection.get(key, []))
    collection[key] = [e for e in collection.get(key, []) if e.get("id") != ent_id]
    if len(collection[key]) == before:
        raise PlanningError("not_found", "that item no longer exists")


def _find(collection: dict[str, Any], entity: str, ent_id: str) -> dict[str, Any] | None:
    for e in collection.get(ENTITIES[entity]["key"], []):
        if e.get("id") == ent_id:
            return e
    return None


def _build(entity: str, data: dict[str, Any], actor: dict[str, Any] | None,
           now: str) -> dict[str, Any]:
    base = {
        "id": _new_id(ENTITIES[entity]["prefix"]),
        "title": str(data.get("title", "")).strip() or "Untitled",
        "body": str(data.get("body", "")),
        "created_at": now,
        "updated_at": now,
        "created_by": normalize_actor(actor),
        "links": _clean_links(data.get("links")),
        "external": data.get("external"),
    }
    if entity == "task":
        base.update({
            "status": _one_of(data.get("status"), TASK_STATUS, "todo"),
            "priority": _one_of(data.get("priority"), TASK_PRIORITY, "p2"),
            "assignee": normalize_actor(data.get("assignee")),
            "labels": list(data.get("labels", []) or []),
            "milestone_id": data.get("milestone_id"),
        })
    elif entity == "issue":
        base.update({
            "severity": _one_of(data.get("severity"), ISSUE_SEVERITY, "med"),
            "state": _one_of(data.get("state"), ISSUE_STATE, "open"),
            "assignee": normalize_actor(data.get("assignee")),
            "found_in": _ref(data.get("found_in")),
            "fixed_in": _ref(data.get("fixed_in")),
        })
    elif entity == "milestone":
        base.update({
            "status": _one_of(data.get("status"), MILESTONE_STATUS, "open"),
            "due": data.get("due"),
            "release_link": _ref(data.get("release_link")),
            "notes": str(data.get("notes", "")),
        })
    return base


# fields a client may set/patch per entity (id/created_* are never patchable)
_PATCHABLE = {
    "task": {"title", "body", "status", "priority", "assignee", "labels",
             "milestone_id", "links", "external"},
    "issue": {"title", "body", "severity", "state", "assignee", "found_in",
              "fixed_in", "links", "external"},
    "milestone": {"title", "body", "status", "due", "release_link", "notes",
                  "links", "external"},
}
_ENUM_FIELDS = {
    ("task", "status"): (TASK_STATUS, "todo"),
    ("task", "priority"): (TASK_PRIORITY, "p2"),
    ("issue", "severity"): (ISSUE_SEVERITY, "med"),
    ("issue", "state"): (ISSUE_STATE, "open"),
    ("milestone", "status"): (MILESTONE_STATUS, "open"),
}
_ACTOR_FIELDS = {"assignee"}
_REF_FIELDS = {"found_in", "fixed_in", "release_link"}


def _apply_fields(entity: str, ent: dict[str, Any], data: dict[str, Any]) -> None:
    for field, value in data.items():
        if field not in _PATCHABLE[entity]:
            continue
        if (entity, field) in _ENUM_FIELDS:
            allowed, default = _ENUM_FIELDS[(entity, field)]
            ent[field] = _one_of(value, allowed, ent.get(field, default))
        elif field in _ACTOR_FIELDS:
            ent[field] = normalize_actor(value)
        elif field in _REF_FIELDS:
            ent[field] = _ref(value)
        elif field == "links":
            ent[field] = _clean_links(value)
        else:
            ent[field] = value


def _one_of(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").lower()
    return v if v in allowed else default


def _ref(value: Any) -> dict[str, Any] | None:
    """A reference to a design item (± its revision): ``{item_id, rev?}``."""
    if not isinstance(value, dict) or not value.get("item_id"):
        return None
    out = {"item_id": value["item_id"]}
    if value.get("rev"):
        out["rev"] = value["rev"]
    return out


def _clean_links(links: Any) -> list[dict[str, Any]]:
    """Cross-links to items/history-entries/issues: ``[{kind, …}]``."""
    out: list[dict[str, Any]] = []
    for ln in links or []:
        if not isinstance(ln, dict):
            continue
        kind = ln.get("kind")
        if kind == "item" and ln.get("item_id"):
            out.append({"kind": "item", "item_id": ln["item_id"]})
        elif kind == "entry" and ln.get("commit"):
            out.append({"kind": "entry", "commit": ln["commit"]})
        elif kind == "issue" and ln.get("id"):
            out.append({"kind": "issue", "id": ln["id"]})
    return out
