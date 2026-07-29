"""task_list_tools.py — the ``task_list`` planning tool (live copilot Task list).

Family: ``workspace``.
Risk: write (default) — it mutates the run's published plan state.

Contract: ``contracts/agent_events.md`` §plan_state. The store and the event
emission live in ``services/agent/task_list.py``; this module is only the
model-facing surface, so it is written to be maximally forgiving of model
dialects and to NEVER raise — a planning tool that throws would abort the very
turn it exists to organize.
"""
from __future__ import annotations

from typing import Any

# Item-list kwarg spellings models write instead of `items`.
_ITEMS_ALIASES = ("tasks", "todos", "steps", "plan", "list", "task_list",
                  "checklist", "entries")

_SHAPE_HINT = (
    "task_list(items=[{'title': str, 'status': "
    "'pending|running|done|fail|abandoned'}], title=str) — items is the FULL "
    "list every call."
)


def _coerce_items(value: Any) -> list[Any] | None:
    """Best-effort list-of-items out of whatever the model passed, or None.

    Models hand this tool a JSON string, a newline-joined block, a single dict,
    or a bare string as often as they hand it a list. Rejecting those would turn
    a formatting slip into a hard tool failure mid-plan.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        # {"1": "step", ...} or {"items": [...]} or a single item dict.
        for key in ("items", *_ITEMS_ALIASES):
            inner = value.get(key)
            if isinstance(inner, (list, tuple)):
                return list(inner)
        if any(k in value for k in ("title", "name", "text", "task", "step")):
            return [value]
        return [v for v in value.values() if v is not None] or None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[0] in "[{":
            import json
            try:
                return _coerce_items(json.loads(text))
            except Exception:  # noqa: BLE001 — fall through to line splitting
                pass
        lines = [ln.strip(" \t-*•").strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        return lines or None
    return None


def register_task_list_tools(mcp) -> None:
    """Register the ``task_list`` planning tool."""

    @mcp.tool()
    def task_list(
        items: Any = None,
        title: str | None = None,
        summary: str | None = None,
        shape: str = "checklist",
        run_id: str = "",
        **extra: Any,
    ) -> dict:
        """Publish the LIVE task list shown in the copilot's Task list panel.

        Call this at the START of any multi-step job, then call it AGAIN after
        each step with the updated statuses. The user watches this panel to see
        what you are doing — an out-of-date list is worse than none.

        FULL SNAPSHOT SEMANTICS: every call REPLACES the entire list. Always
        pass EVERY item, each with its current status — items you leave out
        vanish from the panel. Keep the order (and the ids, if you set any)
        identical between calls so rows update in place instead of duplicating.

        Args:
            items: The whole list. Either plain strings, or dicts
                ``{id?, title, status?, desc?, phase?}``. ``title`` may also be
                spelled ``name``/``text``/``task``/``step``. Max 40 items.
            title: Heading for the panel, e.g. "Bracket build".
            summary: Optional one-line status under the heading.
            shape: ``checklist`` (default), ``build_plan``, ``phased_todo``,
                ``objectives`` or ``dag``.
            run_id: Leave empty — resolved from the running turn.

        Status vocabulary (anything else is folded onto these):
            ``pending`` not started · ``running`` in progress now ·
            ``done`` finished · ``pass`` verified · ``fail`` failed ·
            ``abandoned`` skipped/dropped.

        Example — second call of a three-step job, step 1 finished:

            task_list(
                title="Bracket build",
                items=[
                    {"title": "Sketch the base profile", "status": "done"},
                    {"title": "Extrude 5mm and add fillets", "status": "running"},
                    {"title": "Save and register in PLM", "status": "pending"},
                ],
            )

        Returns ``{"ok": True, "revision": n, "total": n, "done": n,
        "items": [{id, title, status}]}``.
        """
        from services.agent import task_list as store

        try:
            raw = items
            if raw is None:
                for alias in _ITEMS_ALIASES:
                    if extra.get(alias) is not None:
                        raw = extra[alias]
                        break
            coerced = _coerce_items(raw)
            if coerced is None:
                return {
                    "ok": False,
                    "error": "task_list needs a non-empty `items` list",
                    "hint": _SHAPE_HINT,
                }
            snap = store.publish(
                run_id or None, coerced,
                title=title or extra.get("heading") or extra.get("name"),
                summary=summary or extra.get("note"),
                shape=shape,
            )
            out = [{"id": it["id"], "title": it["title"], "status": it["status"]}
                   for it in snap["items"]]
            if not out:
                return {
                    "ok": False,
                    "error": "no usable items — every entry lacked a title",
                    "hint": _SHAPE_HINT,
                }
            return {
                "ok": True,
                "revision": snap["revision"],
                "total": len(out),
                "done": sum(1 for it in out if it["status"] in ("done", "pass")),
                "items": out,
            }
        except Exception as exc:  # noqa: BLE001 — a planning tool must never
            # abort the turn it exists to organize; hand the model the shape back.
            return {"ok": False, "error": f"task_list failed: {exc}",
                    "hint": _SHAPE_HINT}
