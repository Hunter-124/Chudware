"""OMP Auto-Learn sidecar — standing guidance + optional post-stop capture.

Mirrors vendor OMP ``autolearn/controller.ts`` + settings keys:

  * ``autolearn.enabled``      → ``agent.autolearn_enabled``
  * ``autolearn.autoContinue`` → ``agent.autolearn_auto_continue``
  * ``autolearn.minToolCalls`` → ``agent.autolearn_min_tool_calls``

CAD adaptations (no ``manage_skill`` / ``learn`` MCP tools in our registry):

  * Standing guidance points at ``ledger_note`` + project memory (written when
    ``agent.memory_learn`` is on) instead of OMP managed-skills.
  * Auto-run capture at stop writes a compact "lesson" note via
    ``project_memory.remember`` when a memory backend is available; never
    raises into the turn path. When memory is disabled / backend unavailable,
    the capture is a no-op (graceful degrade).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("chudware.autolearn")

# Standing system-prompt guidance (adapted from OMP autolearn-guidance.md +
# autolearn-guidance-learn.md for CAD's ledger / project_memory surface).
_GUIDANCE = """## Auto-Learn (experimental)

You can grow durable project memory as you work.

- When you discover a repeatable procedure, a project convention, a non-obvious fix, or a user preference worth keeping, record it with `ledger_note` (kind that fits) so the mission ledger keeps the trail.
- When project memory is enabled, also capture durable facts/conventions so future turns can recall them — prefer specific, reusable notes over vague summaries.
- Capture sparingly. A note earns its place only if it will help next time; prefer updating an existing convention over near-duplicates.
"""

_CAPTURE_SUMMARY_PREFIX = "[auto-learn]"


def standing_guidance(cfg: dict | None = None) -> str:
    """Return standing Auto-Learn system guidance, or ``\"\"`` when disabled."""
    try:
        from services.agent.config import autolearn_enabled  # noqa: PLC0415
        if not autolearn_enabled(cfg):
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return _GUIDANCE.strip()


def maybe_capture_after_turn(
    *,
    request: str,
    result: Any,
    tool_calls: int,
    project_root: str | None = None,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """Post-stop Auto-Learn capture (OMP ``autolearn.autoContinue``).

    When enabled and *tool_calls* ≥ min threshold, write one project-memory
    note summarizing that the turn produced reusable work. Returns a small
    status dict for telemetry; never raises.
    """
    out: dict[str, Any] = {"ok": False, "skipped": True, "reason": "disabled"}
    try:
        from services.agent.config import (  # noqa: PLC0415
            autolearn_auto_continue,
            autolearn_enabled,
            autolearn_min_tool_calls,
            memory_enabled,
            memory_learn_enabled,
            workspace_dir,
        )
        if not autolearn_enabled(cfg):
            return out
        if not autolearn_auto_continue(cfg):
            out["reason"] = "auto_continue_off"
            return out
        min_calls = autolearn_min_tool_calls(cfg)
        if int(tool_calls or 0) < min_calls:
            out["reason"] = f"below_min_tool_calls({tool_calls}<{min_calls})"
            return out
        if not memory_enabled(cfg) or not memory_learn_enabled(cfg):
            out["reason"] = "memory_learn_off"
            return out

        from services.agent import project_memory as _pm  # noqa: PLC0415

        root = project_root
        if not root:
            try:
                root = workspace_dir()
            except Exception:  # noqa: BLE001
                root = None
        if not root:
            out["reason"] = "no_project_root"
            return out

        stop = ""
        summary = ""
        try:
            if hasattr(result, "stop_reason"):
                stop = str(getattr(result, "stop_reason", "") or "")
            res = getattr(result, "result", None)
            if isinstance(res, dict):
                ho = res.get("handoff") if isinstance(res.get("handoff"), dict) else {}
                summary = str(ho.get("summary") or "")[:400]
        except Exception:  # noqa: BLE001
            pass

        req_snip = (request or "").strip().replace("\n", " ")[:160]
        body = (
            f"{_CAPTURE_SUMMARY_PREFIX} After {tool_calls} tool call(s) "
            f"(stop={stop or 'n/a'}): {summary or req_snip or 'turn completed'}."
        )
        mid = _pm.remember(root, body, kind="auto_learn", source="autolearn", importance=0.4)
        out = {"ok": True, "skipped": False, "memory_id": mid, "tool_calls": tool_calls}
        return out
    except Exception as exc:  # noqa: BLE001 — never break the turn
        log.debug("autolearn capture failed: %s", exc)
        return {"ok": False, "skipped": True, "reason": f"{type(exc).__name__}: {exc}"}
