"""services.plm.trackers — the external-tracker seam (deferred, stub only).

Native planning (:mod:`planning`) is the ground-up default. Teams that already
live in GitHub Issues / Jira / Linear should be able to *migrate off* them, or
keep using the parts they like, without the native board being second-class.
This module is the frozen seam for that: a :class:`Tracker` protocol and the
``external`` link field carried on tasks/issues. NO tracker is implemented in
v1 — importing/syncing an external tracker is future work that lands here without
any schema change (the ``external`` field already reserves the link).

External link shape (stored on a task/issue ``external`` field)::

    {"tracker": "github", "id": "#42", "url": "https://github.com/...",
     "synced_at": "2026-07-07T..."}
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# tracker kinds we reserve identifiers for (none implemented in v1).
KNOWN_TRACKERS = ("github", "gitlab", "jira", "linear")


@runtime_checkable
class Tracker(Protocol):
    """A read/write bridge to an external issue tracker (deferred).

    An implementation maps external issues onto native planning entities and
    back. v1 ships none; the protocol fixes the shape so an importer/syncer can
    be dropped in later behind a per-project remote config, exactly like the PLM
    remote seam (contract §6.1).
    """

    kind: str

    def pull_issues(self, since: str | None = None) -> list[dict[str, Any]]:
        """Fetch external issues as native-issue dicts (id-mapped)."""
        ...

    def push_status(self, entity: dict[str, Any]) -> dict[str, Any]:
        """Reflect a native entity's status change back to the tracker."""
        ...


def external_link(tracker: str, ext_id: str, url: str = "",
                  synced_at: str = "") -> dict[str, Any]:
    """Build the ``external`` link object stored on a task/issue."""
    return {"tracker": tracker, "id": ext_id, "url": url, "synced_at": synced_at}


def available_trackers() -> list[str]:
    """The trackers with an active implementation — empty in v1 (all deferred)."""
    return []
