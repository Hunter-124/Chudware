"""services.plm.items — item identity & lifecycle rules (contract §0/§4).

The "P" in PLM, kept invisible until it helps (plan §3.4): every document in a
project has a stable ``item_id`` and an auto-assigned, human-editable part
number (``PRJ-0001``). Rev letters move ONLY at release. The lifecycle state
machine is fixed and minimal (contract §4):

    In work → Released → Superseded        (no custom states, ever)

* ``In work → Released`` happens in :meth:`services.plm.vault.Vault.release`.
* Releasing rev N auto-supersedes rev N−1 (per released-rev pin).
* A **Released item is never edited in place**: changing it requires the
  explicit "start a new revision" action (:func:`start_new_rev`), which flips
  the item back to *In work* — the working file simply carries forward and the
  next release stamps the next letter. The vault's save path enforces this
  (edit-in-place of Released raises ``locked``).

Registration itself (auto item_id + part number at first check-in) already
lives in :mod:`manifest`; this module adds the editing and transition
rules on top.
"""
from __future__ import annotations

import re
from typing import Any

from services.plm import manifest as manifest_mod

# part numbers: printable, no whitespace, 1–64 chars (prefix-NNNN by default,
# but the user may type anything sane — ease-of-use: don't police style)
_PART_NUMBER_RE = re.compile(r"^\S{1,64}$")


class ItemRuleError(Exception):
    """Raised with a contract §7 reason code when an item rule is violated."""

    def __init__(self, reason: str, hint: str = "") -> None:
        super().__init__(hint or reason)
        self.reason = reason
        self.hint = hint


def set_part_number(m: dict[str, Any], item_id: str, part_number: str) -> dict[str, Any]:
    """Edit an item's part number in the manifest (contract §4: editable,
    unique within the project). Returns the updated item dict."""
    item = manifest_mod.find_item(m, item_id)
    if item is None:
        raise ItemRuleError("invalid_item", f"no such item: {item_id}")
    part_number = (part_number or "").strip()
    if not _PART_NUMBER_RE.match(part_number):
        raise ItemRuleError("invalid_mode",
                            "a part number is a short label without spaces")
    for other in m["items"]:
        if other["item_id"] != item_id and other.get("part_number") == part_number:
            raise ItemRuleError(
                "preflight_failed",
                f"{part_number} is already used by {other.get('path', 'another item')}")
    item["part_number"] = part_number
    return item


def start_new_rev(m: dict[str, Any], item_id: str) -> dict[str, Any]:
    """The "start rev C" action: flip a Released item back to *In work* so it
    can be edited again (contract §4; DoD "no edit-in-place of Released").

    The working file carries forward as-is ("copies forward"); the next
    release stamps the next rev letter. Idempotent on an In-work item.
    Returns ``{item, working_rev}`` where ``working_rev`` is the letter the
    NEXT release will stamp.
    """
    item = manifest_mod.find_item(m, item_id)
    if item is None:
        raise ItemRuleError("invalid_item", f"no such item: {item_id}")
    if item["lifecycle"] == manifest_mod.LIFECYCLE_RELEASED:
        item["lifecycle"] = manifest_mod.LIFECYCLE_IN_WORK
    return {"item": item,
            "working_rev": manifest_mod.next_rev_letter(item.get("rev"))}


def guard_released_edit(m: dict[str, Any], changed_item_ids: list[str]) -> None:
    """Reject a save that would edit a Released item in place (contract §4).

    ``changed_item_ids`` are the items whose canonical ``document.json``
    differs from the last history entry. Raises ``locked`` naming the first
    offender; the fix is one click ("start a new revision").
    """
    for iid in changed_item_ids:
        item = manifest_mod.find_item(m, iid)
        if item is not None and item["lifecycle"] == manifest_mod.LIFECYCLE_RELEASED:
            label = item.get("part_number") or item.get("path") or iid
            raise ItemRuleError(
                "locked",
                f"{label} is released — start a new revision to change it")


def require_in_work_for_release(item: dict[str, Any]) -> None:
    """A release moves *In work → Released*; releasing an already-Released
    item is a no-op state-machine-wise and is rejected with a one-click fix
    (start a new revision first)."""
    if item["lifecycle"] == manifest_mod.LIFECYCLE_RELEASED:
        label = item.get("part_number") or item.get("path") or item["item_id"]
        raise ItemRuleError(
            "invalid_mode",
            f"{label} is already released — start a new revision first")


def item_view(item: dict[str, Any]) -> dict[str, Any]:
    """The status/badge view of an item, including the derived working rev
    (the letter the next release will stamp) for "B · In work" chips."""
    working = ""
    if item["lifecycle"] == manifest_mod.LIFECYCLE_IN_WORK:
        working = manifest_mod.next_rev_letter(item.get("rev")) or ""
    # Strings, never null: items predating part_number/rev stamping have None here,
    # and a null string field crashes strict clients (GUI json::value type mismatch).
    view = {
        "item_id": item["item_id"], "path": item["path"], "kind": item["kind"],
        "part_number": item.get("part_number") or "", "rev": item.get("rev") or "",
        "lifecycle": item["lifecycle"], "working_rev": working,
    }
    if item.get("doc_type"):
        view["doc_type"] = item["doc_type"]
    return view


def untrack_item(m: dict[str, Any], item_id: str) -> dict[str, Any]:
    """Remove an item from the registry (contract §4 forward-only: history is
    NOT touched — old entries keep the item's blobs, so a restore of an earlier
    point still recovers it; only the live registry drops the row)."""
    item = manifest_mod.find_item(m, item_id)
    if item is None:
        raise ItemRuleError("invalid_item", f"no such item: {item_id}")
    m["items"] = [it for it in m["items"] if it["item_id"] != item_id]
    return item
