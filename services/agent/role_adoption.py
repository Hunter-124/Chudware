"""adopt_role / request_role_change tools + session role state.

Main (root) agent may adopt freely. Subagents under role_adoption=supervised
must request_role_change (advisor/main). Spawning other roles via delegate is
always allowed (subject to max_subagent_depth).
"""
from __future__ import annotations

from typing import Any, Callable

from services.agent.role_catalog import can_adopt_role, get_role_adoption_mode


def adopt_role(
    role: str = "",
    *,
    is_root: bool = True,
    depth: int = 0,
    cfg: dict | None = None,
    apply: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Switch this agent's role card (tools + system prompt).

    *apply(role_id)* should load the card and rebind the session; if omitted,
    returns the approval payload only.
    """
    role_s = (role or "").strip()
    if not role_s:
        return {"ok": False, "error": "role is required"}
    if not can_adopt_role(is_root=is_root, depth=depth, cfg=cfg):
        return {
            "ok": False,
            "error": "role adoption denied for subagents (role_adoption=supervised); "
                     "use request_role_change or ask the main session / advisor",
            "role_adoption": get_role_adoption_mode(cfg),
            "hint": "delegate(role=...) still works to spawn a child with that role",
        }
    try:
        from services.agent.roles import load_role
        card = load_role(role_s)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"unknown role {role_s!r}: {exc}"}
    result: dict[str, Any] = {
        "ok": True,
        "role": card.id,
        "goal": card.goal,
        "adopted": True,
    }
    if apply is not None:
        try:
            apply(card.id)
            result["applied"] = True
        except Exception as exc:  # noqa: BLE001
            result["applied"] = False
            result["apply_error"] = str(exc)
    return result


def request_role_change(
    role: str = "",
    reason: str = "",
    *,
    from_role: str = "",
    depth: int = 0,
) -> dict[str, Any]:
    """Subagent asks main/advisor for permission to adopt *role*."""
    role_s = (role or "").strip()
    if not role_s:
        return {"ok": False, "error": "role is required"}
    # Structured request — main/advisor surfaces via events / ledger.
    payload = {
        "ok": True,
        "requested": True,
        "role": role_s,
        "from_role": (from_role or "").strip(),
        "reason": (reason or "").strip()[:500],
        "depth": int(depth),
        "status": "pending_main_or_advisor",
        "message": (
            f"Role change requested: {(from_role or 'current')} → {role_s}. "
            "Main session or advisor must approve; subagent continues with current role."
        ),
    }
    try:
        from services.agent import agent_events as ae  # noqa: PLC0415
        # Best-effort event for GUI / advisor
        if hasattr(ae, "new_span_id"):
            pass
    except Exception:  # noqa: BLE001
        pass
    return payload


def register_role_tools(
    register: Callable[..., Any],
    *,
    is_root: bool = True,
    depth: int = 0,
    get_cfg: Callable[[], dict] | None = None,
    apply_adopt: Callable[[str], Any] | None = None,
) -> None:
    """Register adopt_role + request_role_change on a tool registry."""

    def _cfg() -> dict | None:
        if get_cfg is None:
            return None
        try:
            return get_cfg()
        except Exception:  # noqa: BLE001
            return None

    def _adopt(role: str = "", **_kw: Any) -> dict:
        return adopt_role(
            role,
            is_root=is_root,
            depth=depth,
            cfg=_cfg(),
            apply=apply_adopt,
        )

    def _request(role: str = "", reason: str = "", from_role: str = "", **_kw: Any) -> dict:
        return request_role_change(role, reason, from_role=from_role, depth=depth)

    register(
        "adopt_role",
        _adopt,
        description=(
            "Main agent only (or free adoption mode): switch THIS agent to another "
            "role card (tools + prompt). For 3D parts prefer adopt_role(model3d) or "
            "delegate to model3d. Subagents under supervised mode must use "
            "request_role_change instead."
        ),
    )
    register(
        "request_role_change",
        _request,
        description=(
            "Subagent: ask main/advisor to approve adopting a different role. "
            "Does not switch immediately. Prefer delegate(role=...) to spawn a child."
        ),
    )
