"""Dev / Settings previews of the real prompt assembly (not stubs).

Used by Settings → AI → Dev Preview and the Tools → Options "Model receives"
panel. Both call the same paths a live ``general_agent`` turn uses:

  * ``roles.load_role`` + ``assemble_system_for_capability``
  * ``tool_schemas.render_tools_block`` (with ``agent.tool_options`` overrides)
  * ``agent_turn._SYSTEM_BASE`` + optional autolearn standing guidance
  * role catalog / harness blocks (best-effort)

The user-request slot is marked with ``USER_REQUEST_PLACEHOLDER`` so the GUI can
colour it; never send that marker to a model.
"""
from __future__ import annotations

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


def preview_main_chat_system(*, include_tools: bool = True) -> dict[str, Any]:
    """Build the system prompt a typical main-chat / ``general_agent`` turn gets.

    Mirrors ``mission_dispatch.run_linear_role_turn`` + the injections inside
    ``agent_turn.run_turn`` (base, harness/role catalog, memory block stub,
    tools slot, autolearn guidance). Does **not** call a provider.

    Returns ``{ok, system, user_placeholder, messages_preview, meta, error?}``.
    ``messages_preview`` is a single user message containing the coloured
    placeholder so the GUI can render the full turn shape.
    """
    meta: dict[str, Any] = {"role": "general_agent"}
    try:
        from services.agent.roles import load_role  # noqa: PLC0415
        from services.agent import agent_turn as _at  # noqa: PLC0415
        from services.agent.autolearn import standing_guidance  # noqa: PLC0415
        from services.agent.mission_dispatch import (  # noqa: PLC0415
            _apply_session_preferences,
            effective_role_system_prompt,
            linear_role_allowed_tools,
        )

        card = load_role("general_agent")
        base_system_prompt = effective_role_system_prompt(card)
        system_prompt = base_system_prompt
        try:
            from services.agent.prompt_tiers import (  # noqa: PLC0415
                assemble_system_for_capability,
                letter_from_capability,
            )
            system_prompt = assemble_system_for_capability(
                base_system_prompt, letter_from_capability(None))
        except Exception:  # noqa: BLE001
            system_prompt = base_system_prompt
        allowed = linear_role_allowed_tools(card)
        allowed, system_prompt = _apply_session_preferences(
            "general_agent", allowed, system_prompt)
        system = (system_prompt or "").rstrip() + "\n\n" + _at._SYSTEM_BASE

        try:
            from services.agent.role_catalog import (  # noqa: PLC0415
                format_role_catalog_block,
                harness_context_block,
            )
            system = system.rstrip() + "\n\n" + harness_context_block()
            system = system.rstrip() + "\n\n" + format_role_catalog_block()
            meta["role_catalog"] = True
        except Exception:  # noqa: BLE001
            meta["role_catalog"] = False


        guidance = standing_guidance()
        if guidance:
            system = system.rstrip() + "\n\n" + guidance
            meta["autolearn"] = True
        else:
            meta["autolearn"] = False

        tools_text = ""
        if include_tools:
            try:
                from services.agent.tool_schemas import render_tools_block  # noqa: PLC0415
                tools_text, tools_meta = render_tools_block(
                    allowed, resolver=_at.resolver_for(None),
                )
                meta["tools"] = tools_meta
            except Exception as exc:  # noqa: BLE001
                meta["tools_error"] = f"{type(exc).__name__}: {exc}"
                tools_text = ""
            if tools_text:
                system = system.rstrip() + "\n\n" + tools_text
            elif allowed:
                system = system.rstrip() + "\n\nAllowed tools: " + ", ".join(allowed[:40])
                if len(allowed) > 40:
                    system += f", … (+{len(allowed) - 40} more)"

        try:
            from services.agent.skills import catalog_block  # noqa: PLC0415
            skill_catalog = catalog_block()
            if skill_catalog:
                system = system.rstrip() + "\n\n" + skill_catalog
                meta["skills_catalog"] = True
            else:
                meta["skills_catalog"] = False
        except Exception:  # noqa: BLE001
            meta["skills_catalog"] = False

        meta["allowed_tool_count"] = len(allowed)
        meta["system_chars"] = len(system)
        try:
            from services.agent.tiers import _count_tokens  # noqa: PLC0415
            meta["system_tokens_est"] = _count_tokens(system)
        except Exception:  # noqa: BLE001
            meta["system_tokens_est"] = max(1, (len(system) + 3) // 4)

        return {
            "ok": True,
            "system": system,
            "user_placeholder": USER_REQUEST_PLACEHOLDER,
            "messages_preview": [
                {"role": "system", "content": system},
                {"role": "user", "content": USER_REQUEST_PLACEHOLDER},
            ],
            "meta": meta,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "system": "",
            "user_placeholder": USER_REQUEST_PLACEHOLDER,
            "messages_preview": [],
            "meta": meta,
            "error": f"{type(exc).__name__}: {exc}",
        }
