"""services.plm.manifest — the aicad.plm/1 project registry (contract §4).

The manifest lives at the shadow-tree root (``manifest.json``) and is re-committed
with every history entry. It is the item registry (part numbers, rev letters,
lifecycle), release pins, BOM freezes, remotes, and lock ref.

This module owns the *schema* and pure manipulation helpers; the vault engine
(``vault.py``) reads/writes it through here so the aicad.plm/1 shape is enforced
in one place.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from services.plm.git_security import (
    LocationCredentialError, SECRET_FIELDS, reject_location_credentials,
)

SCHEMA = "aicad.plm/1"
LOCKS_SCHEMA = "aicad.plm.locks/1"

# fixed lifecycle state machine (contract §4) — no custom states
LIFECYCLE_IN_WORK = "In work"
LIFECYCLE_RELEASED = "Released"
LIFECYCLE_SUPERSEDED = "Superseded"

ITEM_KINDS = {
    "part", "assembly", "drawing", "ecad", "cam", "requirements", "deliverable",
}
AI_MODES = {"manual", "assisted", "managed"}
REMOTE_KINDS = {"folder", "ssh", "https", "plm-server", "chudware-cloud"}
REMOTE_CAPABILITIES = ("snapshot_sync", "advisory_locks")
LIVE_REMOTE_CAPABILITIES = ("snapshot_sync", "live_coedit", "presence")


def remote_mode(profile: dict[str, Any]) -> str:
    """Select the frozen remote behavior without inferring from its URL."""
    capabilities = profile.get("capabilities")
    if (
        profile.get("kind") == "plm-server"
        and isinstance(capabilities, list)
        and "live_coedit" in capabilities
    ):
        return "live"
    return "snapshot"


def new_item_id() -> str:
    """Mint a stable PLM item id ``"i" + 8 hex`` (contract §0)."""
    return "i" + uuid.uuid4().hex[:8]


def new_project_id() -> str:
    return "p" + uuid.uuid4().hex[:8]


def new_manifest(name: str, created_at: str, *, part_number_prefix: str | None = None) -> dict[str, Any]:
    """Create a fresh empty project manifest (contract §4)."""
    prefix = (part_number_prefix or _prefix_from_name(name)).upper()
    return {
        "schema": SCHEMA,
        "project": {"id": new_project_id(), "name": name, "created_at": created_at},
        "part_number_prefix": prefix,
        "ai_mode": "assisted",
        "items": [],
        "bom_freezes": [],
        "remotes": [],
        "lock_ref": "refs/aicad/locks",
    }


def _prefix_from_name(name: str) -> str:
    letters = [c for c in name.upper() if c.isalnum()]
    return "".join(letters[:3]) or "PRJ"


def dumps(manifest: dict[str, Any]) -> bytes:
    """Serialize a manifest deterministically (sorted keys, indent=2)."""
    _validate(manifest)
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


def loads(raw: bytes) -> dict[str, Any]:
    m = json.loads(raw.decode("utf-8"))
    _validate(m)
    return m


def loads_local(raw: bytes) -> dict[str, Any]:
    """A read-only local view: withhold unsafe remote URLs, never repair history.

    Peer manifests still use strict ``loads``. A blocked profile has no usable
    destination; only explicit remote replacement can remove its repair marker.
    """
    m = json.loads(raw.decode("utf-8"))
    _validate_schema(m)
    for remote in m.get("remotes") or []:
        try:
            _validate_remote(remote)
            if (remote.get("kind") in {"folder", "ssh", "https", "plm-server"}
                    and not remote.get("url")):
                raise ValueError("remote destination needs explicit replacement")
        except ValueError:
            remote["url"] = None
            for key in list(remote):
                if key.casefold() in SECRET_FIELDS:
                    del remote[key]
            remote["credential_repair_required"] = True
    return m


def _validate(m: dict[str, Any]) -> None:
    _validate_schema(m)
    for remote in m.get("remotes") or []:
        _validate_remote(remote)


def _validate_schema(m: dict[str, Any]) -> None:
    """Reject an unknown MAJOR schema (forward-tolerance, contract §4)."""
    schema = m.get("schema", "")
    if not schema.startswith("aicad.plm/"):
        raise ValueError(f"not a PLM manifest: schema={schema!r}")
    major = schema.split("/", 1)[1]
    if major != "1":
        raise ValueError(f"unsupported PLM manifest major version: {schema!r}")


def _validate_remote(remote: dict[str, Any]) -> None:
    if "credential_repair_required" in remote:
        raise ValueError("internal remote diagnostics must not be stored in PLM history")
    if any(value for key, value in remote.items() if key.casefold() in SECRET_FIELDS):
        raise LocationCredentialError("remote credentials must not be stored in PLM history")
    url = remote.get("url")
    if isinstance(url, str):
        reject_location_credentials(url)


def find_item(manifest: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for it in manifest["items"]:
        if it["item_id"] == item_id:
            return it
    return None


def find_item_by_path(manifest: dict[str, Any], path: str) -> dict[str, Any] | None:
    for it in manifest["items"]:
        if it["path"] == path:
            return it
    return None


def next_part_number(manifest: dict[str, Any]) -> str:
    """Return the next auto-assigned ``<prefix>-NNNN`` (unique in project)."""
    prefix = manifest["part_number_prefix"]
    used = {it.get("part_number") for it in manifest["items"]}
    n = 1
    while f"{prefix}-{n:04d}" in used:
        n += 1
    return f"{prefix}-{n:04d}"


def register_item(manifest: dict[str, Any], *, kind: str, path: str,
                  doc_uuid: str = "", doc_type: str | None = None) -> dict[str, Any]:
    """Register a new item at first check-in; returns the new item dict.

    ``doc_uuid`` is empty for docless generic items (a spec PDF, a source tree —
    they have no document UUID). ``doc_type`` is the OPTIONAL 2026-07 taxonomy
    refinement (:mod:`doctypes`); it is added to the item ONLY when given so
    legacy CAD items stay byte-identical (forward-tolerance, contract §4).
    """
    if kind not in ITEM_KINDS:
        kind = "part"
    item = {
        "item_id": new_item_id(),
        "doc_uuid": doc_uuid,
        "kind": kind,
        "path": path,
        "part_number": next_part_number(manifest),
        "rev": None,  # no released rev yet; "In work"
        "lifecycle": LIFECYCLE_IN_WORK,
        "released_revs": [],
    }
    if doc_type:
        item["doc_type"] = doc_type
    manifest["items"].append(item)
    return item


def next_rev_letter(current: str | None) -> str:
    """A → B → … ; None → 'A' at first release (contract §4, §5)."""
    if not current:
        return "A"
    # AA after Z (unlikely in practice, but total)
    idx = 0
    for ch in current:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    idx += 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters
