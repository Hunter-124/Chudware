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

SAMPLE_LEDGER = (
    "[LEDGER]\n"
    "- (sample) prior decision: use 6061-T6 for the chassis\n"
    "- (sample) open: confirm wall thickness ≥ 2 mm"
)

SAMPLE_STAGE = (
    "[STAGE]\n"
    "(sample) doc: Part1 — 0 features; active sketch: none"
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
        desc = _description_line(schema["description"], trim=use_trim)
        entry = f"- {schema['signature']}"
        if desc:
            entry += f"\n    {desc}"
        if note:
            entry += f"\n    Note: {note}"
        text = "[TOOLS] Call tools with EXACTLY these kwargs:\n" + entry
        return {
            "ok": True,
            "tool": name,
            "entry": entry,
            "text": text,
            "meta": {"trimmed": use_trim, "has_override": bool(override), "has_note": bool(note)},
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
        from services.agent.copilot_mode_tools import SUBAGENT_DEFAULT_TOOLS  # noqa: PLC0415
        from services.agent import agent_turn as _at  # noqa: PLC0415
        from services.agent.autolearn import standing_guidance  # noqa: PLC0415

        card = load_role("general_agent")
        system_prompt = card.system_prompt
        try:
            from services.agent.prompt_tiers import (  # noqa: PLC0415
                assemble_system_for_capability,
                letter_from_capability,
            )
            system_prompt = assemble_system_for_capability(
                card.system_prompt, letter_from_capability(None))
        except Exception:  # noqa: BLE001
            system_prompt = card.system_prompt

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

        # Sample ledger / stage slots (same §9.1 order as assemble_context).
        try:
            from services.agent.roles import assemble_context  # noqa: PLC0415
            # assemble_context prepends the role system again — we only want the
            # extra slots, so build them manually in §9.1 order after the live
            # system we already assembled above.
            _ = assemble_context  # document the dependency for graphify
        except Exception:  # noqa: BLE001
            pass
        system = system.rstrip() + "\n\n" + SAMPLE_LEDGER
        system = system.rstrip() + "\n\n" + SAMPLE_STAGE

        guidance = standing_guidance()
        if guidance:
            system = system.rstrip() + "\n\n" + guidance
            meta["autolearn"] = True
        else:
            meta["autolearn"] = False

        tools_text = ""
        allowed: list[str] = []
        if include_tools:
            allowed = sorted(set(card.concrete_tools) | SUBAGENT_DEFAULT_TOOLS)
            try:
                from services.agent.config import bugreport_floor  # noqa: PLC0415
                allowed = sorted(set(allowed) | bugreport_floor())
            except Exception:  # noqa: BLE001
                pass
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
