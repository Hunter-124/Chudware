"""Dev / Settings previews of the real prompt assembly (not stubs).

Used by Settings → AI → Dev Preview and the Tools → Options "Model receives"
panel.

* :func:`preview_main_chat_system` defaults to the ONLINE lane every normal chat
  uses: ``prompt_cache.compose_online`` — the role prompt
  (``mission_dispatch.role_prompt``) Chudcode receives as its appended system
  prompt, plus the native host-tool definitions
  (``chudcode_tools.build_host_tools``). ``lane="offline"`` shows the explicit
  local-model fallback instead: the ``prompt_cache`` static prompt that
  ``agent_turn.run_turn`` renders (harness protocol, catalogs, tools slot).
* :func:`preview_tool_entry` shows one offline tools-slot entry with the
  ``agent.tool_options`` overrides applied.

Token figures are ~4 chars/token estimates. The user-request slot is marked
with ``USER_REQUEST_PLACEHOLDER`` so the GUI can colour it; never send that
marker to a model.
"""
from __future__ import annotations

import json
from typing import Any

# GUI colour-span marker — must match host/src/ui/settings_modal.cpp Dev Preview.
USER_REQUEST_PLACEHOLDER = (
    "⟦USER REQUEST — sample⟧ "
    "Sketch a 50mm flange with 4× M6 holes on a 40mm bolt circle."
)



def preview_tool_entry(tool_name: str, *, trim: bool | None = None) -> dict[str, Any]:
    """Assemble the exact tools-slot entry the model sees for *tool_name*.

    Returns ``{ok, tool, entry, text, meta, error?}``. ``entry`` is the single
    ``- name(...)\n    desc…`` block; ``text`` is that entry wrapped in the
    ``[TOOLS]`` header (same shape as ``render_tools_block`` for one tool).
    """
    name = (tool_name or "").strip()
    if not name:
        return {"ok": False, "error": "tool name required", "tool": "", "entry": "", "text": ""}
    try:
        from services.agent import agent_turn as _at  # noqa: PLC0415
        from services.agent.tool_schemas import (  # noqa: PLC0415
            _description_line,
            describe_callable,
            render_tools_block,
        )
        from services.agent.config import get_tool_options  # noqa: PLC0415

        resolver = _at.resolver_for(None)
        fn = None
        try:
            fn = resolver(name)
        except Exception:  # noqa: BLE001
            fn = None

        opts = get_tool_options(None, name) or {}
        use_trim = bool(trim) if trim is not None else True
        st = opts.get("schema_trim")
        if trim is None and st is not None:
            use_trim = bool(st)

        if fn is None:
            # Same fallbacks as render_tools_block for known meta tools.
            text, meta = render_tools_block(
                [name], resolver=resolver, trim=use_trim, include_category_menu=False,
            )
            entry = ""
            if text:
                lines = text.splitlines()
                # Drop the [TOOLS] header line for the single-entry preview.
                entry = "\n".join(lines[1:]) if len(lines) > 1 else text
            return {
                "ok": True,
                "tool": name,
                "entry": entry,
                "text": text or f"- {name}(…)  # schema unavailable",
                "meta": meta,
                "live_description": "",
                "unresolved": True,
            }

        schema = describe_callable(name, fn)
        live_desc = schema.get("description") or ""
        override = (opts.get("description_override") or "").strip()
        if override:
            schema = dict(schema)
            schema["description"] = override
        note = (opts.get("prompt_note") or "").strip()
        defaults = dict(opts.get("param_defaults") or {})
        desc = _description_line(schema["description"], trim=use_trim)
        entry = f"- {schema['signature']}"
        if desc:
            entry += f"\n    {desc}"
        if defaults:
            rendered_defaults = ", ".join(
                f"{key}={value!r}" for key, value in sorted(defaults.items())
            )
            entry += (
                f"\n    Effective defaults when omitted: {rendered_defaults}."
            )
        if note:
            entry += f"\n    Note: {note}"
        text = "[TOOLS] Call tools with EXACTLY these kwargs:\n" + entry
        return {
            "ok": True,
            "tool": name,
            "entry": entry,
            "text": text,
            "meta": {
                "trimmed": use_trim,
                "has_override": bool(override),
                "has_note": bool(note),
                "has_param_defaults": bool(defaults),
            },
            "live_description": live_desc,
            "unresolved": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "tool": name,
            "entry": "",
            "text": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def tool_catalog() -> dict[str, Any]:
    """Full live tool catalog for Settings → AI → Tools (worker ``ai_tool_catalog``).

    Returns ``{ok, tools, meta, error?}`` where each tool row is
    ``{name, family, description, resolved, guidance}``:

      * family / name come from the live ``tool_registry`` (after warm_catalog),
        plus agent pseudo-tools such as delegate that have no registry callable
        outside a mission;
      * description is the live docstring's first paragraph collapsed to one
        line — exactly what the panel shows as "Live (no override)";
      * guidance is the built-in curated entry (tool_guidance) the panel shows
        as dimmed "(default: …)" placeholders in the per-tool Options popup.
    """
    try:
        from services.mcp import tool_registry as _tr  # noqa: PLC0415
        from services.agent import agent_turn as _at  # noqa: PLC0415
        from services.agent.tool_schemas import (  # noqa: PLC0415
            _FALLBACK_SCHEMAS, _description_line, describe_callable,
        )
        from services.agent.tool_guidance import (  # noqa: PLC0415
            PSEUDO_TOOLS, guidance_for,
        )

        _tr.warm_catalog()
        cat = _tr.catalog() or {}
        resolver = _at.resolver_for(None)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in sorted(cat):
            tags = cat.get(name)
            fam = (tags or {}).get("family", "?") if isinstance(tags, dict) else "?"
            fn = None
            try:
                fn = resolver(name)
            except Exception:  # noqa: BLE001
                fn = None
            desc = ""
            if fn is not None:
                desc = _description_line(
                    (describe_callable(name, fn) or {}).get("description") or "",
                    trim=False)
            if not desc:
                # Meta-tools (enable_tools, list_tool_categories) are registry
                # entries with no in-process callable — the model still gets a
                # description from the fallback schema, so the panel must too
                # rather than showing an empty "Included tool context".
                _, _, _desc_line = (_FALLBACK_SCHEMAS.get(name) or "").partition("\n")
                desc = _desc_line.strip()
            rows.append({
                "name": name,
                "family": fam or "?",
                "description": desc,
                "resolved": fn is not None,
                "guidance": guidance_for(name),
            })
            seen.add(name)
        # Pseudo-tools with no registry entry (fallback-schema driven).
        for name in sorted(PSEUDO_TOOLS - seen):
            schema = _FALLBACK_SCHEMAS.get(name) or ""
            sig_line, _, desc_line = schema.partition("\n")
            rows.append({
                "name": name,
                "family": "workspace",
                "description": desc_line.strip(),
                "signature": sig_line.lstrip("- ").strip(),
                "resolved": False,
                "guidance": guidance_for(name),
            })
        fams = sorted({r["family"] for r in rows})
        return {"ok": True, "tools": rows,
                "meta": {"count": len(rows), "families": fams}}
    except Exception as exc:  # noqa: BLE001 — never raise across the worker seam
        return {"ok": False, "tools": [], "error": f"{type(exc).__name__}: {exc}"}


def preview_main_chat_system(
    *, include_tools: bool = True, lane: str = "online"
) -> dict[str, Any]:
    """Build the system prompt a fresh main-chat / ``general_agent`` turn gets.

    *lane* ``"online"`` (default) is the Chudcode lane: ``system`` is exactly the
    CAD system prompt appended to Chudcode's own, and with *include_tools* the
    CAD host-tool definitions are returned beside it (``host_tools`` /
    ``host_tools_text``) — they are native tool definitions, not system-prompt
    text. *lane* ``"offline"`` is the explicit local-model fallback's static
    prompt; *include_tools* there keeps or drops its text tools slot. Does
    **not** call a provider.

    Returns ``{ok, lane, system, user_placeholder, messages_preview, meta,
    error?}`` (online adds ``host_tools`` / ``host_tools_text``).
    ``messages_preview`` is the system message plus a user message holding the
    coloured placeholder so the GUI can render the full turn shape.
    """
    meta: dict[str, Any] = {"role": "general_agent", "lane": lane, "estimate": True}
    try:
        from services.agent import prompt_cache as _pcache  # noqa: PLC0415

        extra: dict[str, Any] = {}
        if lane == "online":
            op = _pcache.compose_online(role="general_agent")
            system = op.system
            meta["allowed_tool_count"] = len(op.host_tools)
            meta["autolearn"] = False
            meta["warnings"] = op.warnings
            if include_tools:
                extra["host_tools"] = op.host_tools
                extra["host_tools_text"] = "\n".join(
                    f"- {tool['name']}: {tool['description']}\n"
                    f"    parameters: {json.dumps(tool['parameters'], ensure_ascii=False)}"
                    for tool in op.host_tools
                )
                meta["host_tool_names"] = [tool["name"] for tool in op.host_tools]
                meta["host_tool_tokens_est"] = op.host_tool_tokens
        elif lane == "offline":
            sp = _pcache.get(role="general_agent")
            segments = [
                seg for seg in sp.segments if include_tools or seg.label != "tools"
            ]
            system = _pcache.render_segments(segments)
            present = {seg.label for seg in segments if seg.text}
            meta["allowed_tool_count"] = len(sp.allowed)
            meta["autolearn"] = "autolearn" in present
            meta["warnings"] = sp.warnings
            meta["segments"] = [
                row for row in sp.breakdown() if row["label"] in present
            ]
        else:
            raise ValueError(f"unknown lane {lane!r} (online|offline)")

        meta["system_chars"] = len(system)
        meta["system_tokens_est"] = _pcache._count_tokens(system)
        return {
            "ok": True,
            "lane": lane,
            "system": system,
            "user_placeholder": USER_REQUEST_PLACEHOLDER,
            "messages_preview": [
                {"role": "system", "content": system},
                {"role": "user", "content": USER_REQUEST_PLACEHOLDER},
            ],
            "meta": meta,
            **extra,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "lane": lane,
            "system": "",
            "user_placeholder": USER_REQUEST_PLACEHOLDER,
            "messages_preview": [],
            "meta": meta,
            "error": f"{type(exc).__name__}: {exc}",
        }
