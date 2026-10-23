"""services.plm.permissions — the permission seam (allow-all today).

Every mutating planning/release/lock action routes through :func:`can` so that
organization-level permissions ("new hires don't get nuke-the-project access")
attach at ONE place later, with no change to callers. Today there are no
organizations — identity is a local profile name — so :func:`can` allows
everything. When organizations land (linking people by account), this grows a
role/policy lookup keyed on ``actor.kind == "org_member"``; the call sites do not
change.

Design intent (2026-07-07): permissions must be EASY to set up — sensible
role presets, not a raw ACL editor — so the future policy model is coarse roles
(viewer / contributor / maintainer / owner) over resource classes, resolved here.
"""
from __future__ import annotations

from typing import Any

# resource classes a permission can be checked against (kept coarse on purpose).
RESOURCE_CLASSES = frozenset({
    "item", "history", "release", "planning", "lock", "settings", "project",
})

# actions, likewise coarse.
ACTIONS = frozenset({"view", "edit", "create", "delete", "release", "administer"})


def can(actor: dict[str, Any] | None, action: str, resource: Any = None) -> bool:
    """Return whether *actor* may perform *action* on *resource*.

    Allow-all in v1 (no organizations yet). The signature is the frozen seam;
    when organizations arrive this consults the actor's role for the resource's
    owning organization. Never raises — an unknown actor/action is simply
    allowed today (there is nothing to deny against).
    """
    return True


def require(actor: dict[str, Any] | None, action: str, resource: Any = None) -> None:
    """Raise ``PermissionDenied`` when :func:`can` is false (no-op in v1)."""
    if not can(actor, action, resource):
        raise PermissionDenied(action, resource)


class PermissionDenied(Exception):
    """Raised by :func:`require` when an action is not permitted (unused in v1)."""

    def __init__(self, action: str, resource: Any = None) -> None:
        super().__init__(f"not allowed to {action}")
        self.reason = "not_allowed"
        self.hint = "you do not have permission for that here"
        self.action = action
        self.resource = resource
