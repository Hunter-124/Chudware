"""Adapter from Chudware's effective CAD tool surface to Chudcode host tools.

The adapter deliberately owns no CAD tool catalog.  Names, descriptions, schemas,
policy decisions, and dispatch all come from the same live registries used by
:mod:`services.agent.agent_turn`.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin

_MISSING = object()
_LOG = logging.getLogger(__name__)


def _debug_fallback(area: str, exc: BaseException) -> None:
    """Record an optional-path failure without emitting startup noise."""
    _LOG.debug("chudcode tool adapter fallback in %s: %s", area, exc, exc_info=exc)


# Chudcode owns these names.  A CAD registry entry may opt back in explicitly via
# ``cad_owned``/``owns_name``, and embedders may replace or extend both sets in the
# run context.  This is a namespace guard, not a second CAD tool catalog.
_CHUDCODE_BUILTINS = frozenset(
    {
        "read",
        "write",
        "edit",
        "bash",
        "grep",
        "glob",
        "computer",
        "browser",
        "task",
        "hub",
        "web_search",
        "eval",
        "yield",
        "security_scan",
        "ast_grep",
        "ast_edit",
        "debug",
        "github",
        "image_analyze",
        "cpp_static",
        "cpp_runtime",
        "binary_inspect",
        "firmware",
        "memory_edit",
        "retain",
        "recall",
        "reflect",
    }
)


@dataclass(frozen=True)
class HostToolBinding:
    """One Chudcode host-tool definition plus its run-scoped CAD binding."""

    name: str
    description: str
    input_schema: dict[str, Any]
    load_mode: str = "always"
    handler: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    context: Any = field(default=None, repr=False, compare=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        """Return the dependency-free binding definition."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _json_safe(self.input_schema),
            "load_mode": self.load_mode,
        }

    def to_host_tool(self, factory: Callable[..., Any]) -> Any:
        """Lazily construct a ``chudcode_rpc.host_tool`` without importing it."""
        if not callable(factory):
            raise TypeError("host-tool factory must be callable")

        def execute(params: Mapping[str, Any] | None, host_context: Any) -> Any:
            send_update = getattr(host_context, "send_update", None)
            progress = (
                (lambda payload: send_update(host_tool_result(payload)))
                if callable(send_update)
                else None
            )
            cancel = lambda: bool(getattr(host_context, "cancelled", False))
            result = host_tool_result(
                dispatch_host_tool(self, params, progress, cancel)
            )
            return _with_skill_injection(self, result)

        return factory(
            name=self.name,
            description=self.description,
            parameters=_json_safe(self.input_schema),
            execute=execute,
            label=self.metadata.get("label"),
            hidden=bool(self.metadata.get("hidden", False)),
        )

    @property
    def definition(self) -> dict[str, Any]:
        return self.as_dict()


def host_tool_result(result: Any) -> dict[str, Any]:
    """Wrap a CAD tool's return value in the envelope Chudcode's RPC requires.

    A ``host_tool_result`` whose ``result`` lacks ``content`` NEVER resolves inside
    Chudcode: the ``write xd://<tool>`` dispatch waits forever, the worker's
    ``wait_for_idle`` waits with it, and the GUI shows "Thinking..." until the 2-hour
    cap (verified live with ``chud --mode rpc`` 0.3.0 for ``{"ok": false, ...}``,
    ``{"ok": true, ...}`` and ``{"docs": [...]}``; a bare string or a
    ``{"content": [...]}`` mapping completes immediately).  Every CAD tool returns a
    raw JSON object, so this is the single place the envelope is applied: the model
    reads the JSON as text and ``details`` keeps the structured result for the event
    mapper.
    """
    if isinstance(result, str):
        return {"content": [{"type": "text", "text": result}]}
    safe = _json_safe(result)
    text = json.dumps(safe, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "details": safe}


def _with_skill_injection(
    binding: HostToolBinding, envelope: dict[str, Any]
) -> dict[str, Any]:
    """Prefix a trigger-matched skill body to this tool's FIRST result text.

    The offline loop injects a skill's body when a turn is about to reach one of
    its ``triggers.tools`` (``agent_turn`` calls ``skills.activate_for_prompt``
    with ``tools_about_to_run``). Online, the model's turn is inside Chudcode and
    the only CAD chokepoint left is this dispatch, so the body rides back on the
    tool's own result — the first time that conversation touches the tool, once,
    within ``skills.SKILL_BODY_CHAR_LIMIT``. ``details`` is untouched, so the
    event mapper and every structured consumer see exactly the tool's own result.
    """
    content = envelope.get("content")
    if not isinstance(content, list) or not content:
        return envelope
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return envelope
    try:
        from services.agent import skills

        bodies = skills.activate_for_prompt(
            "",
            session_id=str(
                _ctx(binding.context, "session_id", None)
                or _ctx(binding.context, "run_id", None)
                or ""
            )
            or None,
            project_root=_ctx(binding.context, "project_dir", None),
            tools_about_to_run=[binding.name],
            role=_ctx(binding.context, "actor", None),
            _aggregate_char_limit=skills.SKILL_BODY_CHAR_LIMIT,
        )
    except Exception as exc:  # noqa: BLE001 — a broken skills dir never fails a call
        _debug_fallback(f"skill injection for {binding.name}", exc)
        return envelope
    if not bodies:
        return envelope
    prefix = "\n\n".join(bodies)
    merged = dict(envelope)
    merged["content"] = [
        {
            **first,
            "text": f"{prefix}\n\n[{binding.name} result]\n{first.get('text') or ''}",
        },
        *content[1:],
    ]
    return merged

# --------------------------------------------------------------------------- context


def _ctx(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, Mapping):
        return context.get(name, default)
    return getattr(context, name, default)


def _ctx_present(context: Any, name: str) -> bool:
    if isinstance(context, Mapping):
        return name in context
    return hasattr(context, name)


def _ordered_unique(values: Sequence[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _role_card(context: Any) -> Any | None:
    card = _ctx(context, "role_card", _ctx(context, "role", None))
    if card is None:
        card = _ctx(context, "role_id", None)
    if isinstance(card, str) and card.strip():
        from services.agent.roles import load_role

        return load_role(card.strip())
    return card


def _role_policy_tools(role: Any) -> list[str] | None:
    """Resolve a role through ``RoleCard.apply_tool_policy`` without mutating globals."""
    apply_policy = getattr(role, "apply_tool_policy", None)
    if not callable(apply_policy):
        return list(getattr(role, "concrete_tools", ()) or ())

    class _Capture:
        value: Any = _MISSING

        def set_policy(self, allow=None, **_kwargs):
            self.value = allow

    capture = _Capture()
    try:
        tokens = apply_policy(capture)
    except Exception as exc:  # noqa: BLE001 - role/config plug-ins are heterogeneous
        _debug_fallback("role policy", exc)
        return list(getattr(role, "concrete_tools", ()) or ())
    if capture.value is None:
        return None

    resolved: list[str] = []
    try:
        from services.agent.tool_categories import _expand_token

        for token in tokens or ():
            values = sorted(_expand_token(str(token)))
            resolved.extend(
                values or ([str(token)] if not str(token).startswith("family:") else [])
            )
    except Exception as exc:  # noqa: BLE001 - optional catalog family expansion
        _debug_fallback("role family expansion", exc)
        resolved = [
            str(token) for token in tokens or () if not str(token).startswith("family:")
        ]
    # ``concrete_tools`` supplies the role module's curated family fallback when
    # the MCP catalog is intentionally cold (unit tests and early worker start).
    return _ordered_unique([*resolved, *(getattr(role, "concrete_tools", ()) or ())])


def _effective_names(context: Any, available: Sequence[str]) -> list[str]:
    """Finalize the run allowlist with the live role/mode/category helpers.

    The three allowlist states remain distinct: ``None`` is unrestricted, ``[]``
    is restricted to the live meta/config floors, and a non-empty sequence is an
    ordinary restriction.

    Order matters: on-demand loads (``enable_tools`` tokens in ``categories``)
    are unioned BEFORE the mode policy runs, so a user-picked read-only mode
    (ask/plan) stays read-only no matter what the model loads mid-turn. The
    policy is a user choice; the loads are the model's.
    """
    role = _role_card(context)
    explicit = _ctx_present(context, "allowed_tools")
    raw_allow = _ctx(context, "allowed_tools", None)
    restricted = explicit and raw_allow is not None

    if explicit:
        names = None if raw_allow is None else _ordered_unique(raw_allow)
    elif role is not None:
        names = _role_policy_tools(role)
        restricted = names is not None
    else:
        names = None

    categories = _ctx(context, "categories", _ctx(context, "enabled_categories", ()))
    if categories:
        from services.agent.tool_categories import META_TOOLS, resolve_tool_requests

        expanded, _unknown = resolve_tool_requests(categories)
        if names is None:
            names = []
            restricted = True
        names = _ordered_unique([*names, *sorted(META_TOOLS), *sorted(expanded)])

    mode = _ctx(context, "mode", None)
    plan_only = _ctx(context, "plan_only", None)
    try:
        from services.agent.copilot_mode_tools import apply_mode_tools

        # Plan narrows the SAME set agent would get (minus mutators + the shared
        # floor) rather than the contract's broad PLAN_TOOLS list, so the main
        # chat's Plan mode is "Agent, read-only" and costs no extra context.
        narrowed = apply_mode_tools(
            mode,
            names,
            plan_only=plan_only,
            subagent=bool(
                _ctx(context, "subagent_id", None) or _ctx(context, "subagent", False)
            ),
            preferences=_ctx(context, "turn_preferences", None),
        )
        if narrowed is not None:
            names = _ordered_unique(narrowed)
            restricted = True
    except Exception as exc:  # noqa: BLE001 - mode helpers are optional at startup
        _debug_fallback("mode policy", exc)

    if names is not None:
        try:
            from services.agent.copilot_mode_tools import apply_meta_tool_floor

            floored = apply_meta_tool_floor(names)
            if floored is not None:
                names = _ordered_unique(floored)
        except Exception as exc:  # noqa: BLE001 - optional mode floor
            _debug_fallback("meta-tool floor", exc)

    if names is not None:
        try:
            from services.agent.config import bugreport_floor

            names = _ordered_unique([*names, *bugreport_floor()])
        except Exception as exc:  # noqa: BLE001 - optional config floor
            _debug_fallback("bug-report floor", exc)
        try:
            from services.agent.guardrails import MUTATING_TOOLS

            if set(names).intersection(MUTATING_TOOLS):
                names = _ordered_unique([*names, "verify_battery"])
        except Exception as exc:  # noqa: BLE001 - optional guardrail catalog
            _debug_fallback("mutation verification floor", exc)
    preferences = _ctx(context, "turn_preferences", None)
    if preferences is not None:
        from services.agent.copilot_mode_tools import apply_preference_tools

        return apply_preference_tools(names if names is not None else available,
                                      preferences, mode=mode)

    if names is None and not restricted:
        # Unrestricted: the whole live registry. `enable_tools` and
        # `list_tool_categories` are turn-loop primitives, not registry entries, so an
        # unrestricted surface is the ONE case that loses them -- the meta floor above is
        # guarded on `names is not None`. That is backwards: they are the model's on-ramp
        # for managing a large tool surface, and a roleless `agent` turn (allowed_tools
        # explicitly None, which is what agent_admin_turn sends) is exactly where the
        # surface is largest. Live symptom: the model reasoned "I'll enable the modeling
        # and sketch tool categories", found no such tool, and fell back to `bash` and
        # `write` for the rest of the turn without ever touching CAD geometry.
        unrestricted = _ordered_unique(available)
        try:
            from services.agent.copilot_mode_tools import apply_meta_tool_floor

            floored = apply_meta_tool_floor(unrestricted)
            if floored is not None:
                # Append only what the floor ADDS. Live registry order is this list's
                # contract (it is the order the model sees), and the floor helper returns
                # its own ordering, so adopting it wholesale would reshuffle the catalog.
                present = set(unrestricted)
                unrestricted = [
                    *unrestricted,
                    *(name for name in floored if name not in present),
                ]
        except Exception as exc:  # noqa: BLE001 - optional mode floor
            _debug_fallback("meta-tool floor", exc)
        return unrestricted
    return _ordered_unique(names or ())


# --------------------------------------------------------------------------- registry


def _entry_value(entry: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(entry, Mapping) and name in entry:
            return entry[name]
        if hasattr(entry, name):
            return getattr(entry, name)
    return default


def _tool_manager_entries(owner: Any) -> Mapping[str, Any] | None:
    if owner is None:
        return None
    if isinstance(owner, Mapping):
        return owner
    manager = getattr(owner, "_tool_manager", None)
    for candidate in (
        getattr(manager, "_tools", None),
        getattr(owner, "_tools", None),
        getattr(owner, "tools", None),
    ):
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _load_registry_entries(context: Any) -> dict[str, Any]:
    injected = _ctx(context, "tool_entries", _MISSING)
    if injected is _MISSING:
        injected = _ctx(context, "tool_registry", _MISSING)
    entries = _tool_manager_entries(injected) if injected is not _MISSING else None
    if entries is None:
        try:
            from services.mcp.server import mcp

            entries = _tool_manager_entries(mcp)
        except Exception as exc:  # noqa: BLE001 - full MCP server is optional in tests
            _debug_fallback("MCP registry import", exc)
            entries = {}
    return {str(name): entry for name, entry in (entries or {}).items()}


def _mission_names(context: Any) -> list[str]:
    mission = _ctx(context, "mission_dispatch", None)
    if mission is None:
        return []
    for attr in ("handlers", "_handlers", "tools", "tool_names"):
        value = getattr(mission, attr, None)
        if isinstance(value, Mapping):
            return _ordered_unique(list(value))
        if isinstance(value, (list, tuple, set, frozenset)):
            return _ordered_unique(list(value))
    return []


def _resolver(context: Any) -> Callable[[str], Any]:
    supplied = _ctx(context, "resolver", None)
    if callable(supplied):
        return supplied
    from services.agent.agent_turn import resolver_for

    return resolver_for(_ctx(context, "mission_dispatch", None))


def _policy_refusal(name: str, context: Any) -> dict[str, Any] | None:
    registry = _ctx(context, "policy_registry", None)
    if registry is None:
        try:
            from services.mcp import tool_registry as registry
        except Exception as exc:  # noqa: BLE001 - policy module is optional in tests
            _debug_fallback("policy registry import", exc)
            return None
    deny = getattr(registry, "deny_reason", None)
    if not callable(deny):
        allowed = getattr(registry, "is_allowed", None)
        if callable(allowed) and not allowed(name):
            return {"ok": False, "reason": f"tool {name!r} is not permitted"}
        return None
    reason = deny(name)
    if not reason:
        return None
    refusal = getattr(registry, "refusal", None)
    if callable(refusal):
        return _json_safe(refusal(name))
    return {"ok": False, "reason": str(reason)}


# --------------------------------------------------------------------------- schemas


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation in (inspect.Parameter.empty, Any, object, None):
        return {}
    if isinstance(annotation, str):
        low = annotation.lower()
        if "bool" in low:
            return {"type": "boolean"}
        if "int" in low:
            return {"type": "integer"}
        if "float" in low or "number" in low:
            return {"type": "number"}
        if "list" in low or "tuple" in low or "set" in low:
            return {"type": "array"}
        if "dict" in low or "mapping" in low:
            return {"type": "object"}
        return {"type": "string"}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        variants = [_annotation_schema(arg) for arg in args if arg is not type(None)]
        if len(variants) == 1:
            return variants[0]
        return {"anyOf": variants} if variants else {}
    if origin in (list, tuple, set, frozenset, Sequence):
        return {"type": "array", "items": _annotation_schema(args[0]) if args else {}}
    if origin in (dict, Mapping):
        return {"type": "object"}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation in (dict, Mapping):
        return {"type": "object"}
    if annotation in (list, tuple, set, frozenset):
        return {"type": "array"}
    return {}


def _schema_from_callable(fn: Any) -> dict[str, Any]:
    if not callable(fn):
        return {"type": "object", "properties": {}}
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        _debug_fallback("callable schema inspection", exc)
        return {"type": "object", "properties": {}}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls", "ctx", "context", "progress", "cancel"}:
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        schema = _annotation_schema(parameter.annotation)
        if parameter.default is not inspect.Parameter.empty:
            schema = dict(schema)
            schema["default"] = _json_safe(parameter.default)
        else:
            required.append(parameter.name)
        properties[parameter.name] = schema
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def _var_param_names(fn: Any) -> frozenset[str]:
    """Names of *fn*'s ``*args`` / ``**kwargs`` catch-alls.

    A catch-all is not a parameter a caller can name, so it must never appear in
    the tool schema handed to the model.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        _debug_fallback("catch-all probe", exc)
        return frozenset()
    return frozenset(
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    )


def _without_catch_alls(schema: dict[str, Any], fn: Any) -> dict[str, Any]:
    """Drop catch-all pseudo-properties from a registry-supplied tool schema.

    FastMCP derives a tool's ``inputSchema`` from its signature through Pydantic,
    which models a ``**extra: Any`` catch-all as a field named ``extra`` — and,
    with no default to give it, marks that field REQUIRED. Chudcode validates
    every host call against the schema it was given, so the advertised contract
    made a satisfiable call impossible: ``task_list`` failed 100% of the time
    with ``Validation failed for tool "task_list": - extra: is required``, and
    the live plan panel therefore never published a single item. Thirty-four
    further tools advertised the same phantom argument as optional, inviting the
    model to fill it with junk that lands in the tool's user-intent channel.
    """
    names = _var_param_names(fn)
    if not names:
        return schema
    out = dict(schema)
    properties = out.get("properties")
    if isinstance(properties, Mapping):
        kept = {
            key: value for key, value in properties.items() if key not in names
        }
        if len(kept) != len(properties):
            out["properties"] = kept
    required = out.get("required")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        kept_required = [item for item in required if item not in names]
        if len(kept_required) != len(required):
            if kept_required:
                out["required"] = kept_required
            else:
                out.pop("required", None)
    return out


def _entry_schema(entry: Any, fn: Any) -> dict[str, Any]:
    schema = _entry_value(
        entry,
        "input_schema",
        "inputSchema",
        "parameters",
        "parameter_schema",
        default=None,
    )
    if callable(schema):
        try:
            schema = schema()
        except TypeError as exc:
            _debug_fallback("callable schema", exc)
            schema = None
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            _debug_fallback("JSON schema decoding", exc)
            schema = None
    if not isinstance(schema, Mapping):
        dump = getattr(schema, "model_dump", None)
        json_schema = getattr(schema, "model_json_schema", None)
        try:
            if callable(dump):
                schema = dump(mode="json")
            elif callable(json_schema):
                schema = json_schema()
        except Exception as exc:  # noqa: BLE001 - supports Pydantic v1/v2 schema objects
            _debug_fallback("schema object conversion", exc)
            schema = None
    if isinstance(schema, Mapping):
        return _without_catch_alls(_json_safe(schema), fn)
    return _schema_from_callable(fn)


def _entry_description(entry: Any, fn: Any) -> str:
    description = _entry_value(entry, "description", "help", default="")
    if not description and callable(fn):
        description = inspect.getdoc(fn) or ""
    return str(description or "").strip()


def _entry_load_mode(name: str, entry: Any) -> str:
    value = _entry_value(entry, "load_mode", "loadMode", default=None)
    try:
        from services.agent.config import get_tool_options

        options = get_tool_options(None, name) or {}
        value = options.get("load_mode", value)
    except Exception as exc:  # noqa: BLE001 - per-tool config is optional at startup
        _debug_fallback("tool load mode", exc)
    return str(value or "always")


def _collision_allowed(name: str, entry: Any, context: Any) -> bool:
    builtins = set(_ctx(context, "chudcode_builtin_names", _CHUDCODE_BUILTINS) or ())
    builtins.update(_ctx(context, "additional_builtin_names", ()) or ())
    owned = set(
        _ctx(context, "cad_owned_names", _ctx(context, "cad_owned_collisions", ()))
        or ()
    )
    owns_entry = bool(_entry_value(entry, "cad_owned", "owns_name", default=False))
    return name not in builtins or name in owned or owns_entry


def _meta_handler(name: str) -> Callable[..., Any] | None:
    # These two calls are turn-loop primitives rather than MCP registrations.
    # Giving them real callables lets the host schema path treat them exactly
    # like the rest of the effective surface without inventing registry entries.
    #
    # The docstrings are load-bearing: with no registry entry to read a
    # description from, _entry_description falls back to inspect.getdoc, and
    # Chudcode rejects a whole set_host_tools call that carries a tool with an
    # empty description. Both names sit on the shared floor of every mode, so an
    # undocumented handler here failed EVERY turn with
    # `provider_error - set_host_tools: Host tool "enable_tools" must provide a
    # non-empty description` before a single tool ran.
    if name == "list_tool_categories":

        def list_tool_categories() -> dict[str, Any]:
            """List every registered CAD tool grouped by area.

            Returns each group's name, what it covers, and the exact tool names
            in it. The same names are what enable_tools accepts, as a group or
            one tool at a time.
            """
            from services.agent.agent_turn import _tool_category_catalog_result

            return _tool_category_catalog_result()

        return list_tool_categories
    if name == "enable_tools":

        def enable_tools(
            categories: list[str] | None = None, tools: list[str] | None = None
        ) -> dict[str, Any]:
            """Load more CAD tools into this turn's callable tool set.

            `tools` takes exact tool names from the MORE TOOLS menu (or
            list_tool_categories); `categories` takes whole group names. The
            schemas arrive immediately and stay for the rest of the turn.
            Unknown names are reported back, never silently dropped.
            """
            from services.agent.tool_categories import resolve_tool_requests

            tokens = [*(categories or ()), *(tools or ())]
            added, unknown = resolve_tool_requests(tokens)
            return {
                "ok": True,
                "enabled": _ordered_unique(tokens),
                "added": sorted(added),
                "unknown": unknown,
            }

        return enable_tools
    return None


# --------------------------------------------------------------------------- public API


def build_host_tools(context: Any) -> list[HostToolBinding]:
    """Build Chudcode definitions from the effective live CAD tool registry."""
    entries = _load_registry_entries(context)
    available = _ordered_unique([*entries.keys(), *_mission_names(context)])
    names = _effective_names(context, available)
    resolve = _resolver(context)
    call_counts = _ctx(context, "tool_call_counts", None)
    if call_counts is None:
        call_counts = {}
    call_records = _ctx(context, "tool_calls", _ctx(context, "calls", None))
    if call_records is None:
        call_records = []
    out: list[HostToolBinding] = []
    for name in names:
        entry = entries.get(name)
        if not _collision_allowed(name, entry, context):
            continue
        refusal = _policy_refusal(name, context)
        if refusal is not None:
            continue
        try:
            fn = resolve(name)
        except Exception as exc:  # noqa: BLE001 - injected resolvers may be plug-ins
            _debug_fallback(f"resolver for {name}", exc)
            fn = None
        if fn is None and entry is not None:
            candidate = _entry_value(entry, "fn", "handler", "callable", default=None)
            try:
                from services.agent.agent_turn import _directly_callable

                fn = candidate if _directly_callable(candidate) else None
            except Exception as exc:  # noqa: BLE001 - live resolver import is optional
                _debug_fallback(f"callability check for {name}", exc)
                fn = (
                    candidate
                    if callable(candidate)
                    and not inspect.iscoroutinefunction(candidate)
                    else None
                )
        if fn is None:
            fn = _meta_handler(name)
        # An advertised but unbound name is not a host tool.  This matches the
        # live resolver and prevents Chudcode from receiving a schema for a call
        # that CAD would reject as known-but-unbound.
        try:
            from services.agent.agent_turn import _directly_callable

            if not _directly_callable(fn):
                continue
        except Exception as exc:  # noqa: BLE001 - live resolver import is optional
            _debug_fallback(f"final callability check for {name}", exc)
            if not callable(fn) or inspect.iscoroutinefunction(fn):
                continue
        description = _entry_description(entry, fn)
        preferences = _ctx(context, "turn_preferences", None)
        if preferences is not None and preferences.trim_tool_schemas:
            from services.agent.tool_schemas import _description_line

            description = _description_line(description, trim=True)
        out.append(
            HostToolBinding(
                name=name,
                description=description,
                input_schema=_entry_schema(entry, fn),
                load_mode=_entry_load_mode(name, entry),
                handler=fn,
                context=context,
                metadata={
                    "registry_entry": entry,
                    "tool_call_counts": call_counts,
                    "tool_calls": call_records,
                },
            )
        )
    return out


def _cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if isinstance(cancel, bool):
        return cancel
    for attr in ("is_set", "cancelled", "is_cancelled"):
        value = getattr(cancel, attr, None)
        if callable(value):
            try:
                return bool(value())
            except TypeError as exc:
                _debug_fallback(f"cancellation probe {attr}", exc)
                continue
        if value is not None:
            return bool(value)
    if callable(cancel):
        return bool(cancel())
    return False


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    """True only when *fn* DECLARES *name* as a keyword-passable parameter.

    A ``**kwargs`` catch-all deliberately does NOT count. Several CAD tools —
    ``add_features`` above all — end in ``**extra`` and treat every unrecognised
    keyword as USER INTENT: a feature parameter, a design-intent note, or a hard
    error naming the real parameter set. Letting a catch-all stand in for a
    declaration meant the harness posted ``actor``, ``run_id``, ``node_id``,
    ``parent_id``, ``subagent_id``, ``progress`` and ``cancel`` into that
    channel, so THE core modeling tool rejected every call it was given. Live
    2026-09-03, in the model's own words: "add_features consistently rejects the
    harness-injected kwargs like actor, cancel, progress, and run_id ...
    add_features is unusable through this device and I need to find an
    alternative route to apply document operations" — after which it spent
    thirteen minutes writing STEP files by hand through `eval`. A tool that
    wants run context asks for it by name.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        _debug_fallback(f"signature probe for {name}", exc)
        return False
    parameter = sig.parameters.get(name)
    if parameter is None:
        return False
    return parameter.kind in (
        parameter.POSITIONAL_OR_KEYWORD,
        parameter.KEYWORD_ONLY,
    )


def _document_args(
    fn: Callable[..., Any] | None, args: dict[str, Any], context: Any
) -> dict[str, Any]:
    out = dict(args)
    if not callable(fn):
        return out
    for name in ("doc_id", "actor", "run_id", "node_id", "parent_id", "subagent_id"):
        value = _ctx(context, name, None)
        if value is not None and name not in out and _accepts(fn, name):
            out[name] = value
    return out


def _call_run_dispatcher(
    dispatcher: Callable[..., Any],
    name: str,
    args: dict[str, Any],
    context: Any,
    progress: Any,
    cancel: Any,
) -> Any:
    kwargs: dict[str, Any] = {}
    if _accepts(dispatcher, "progress"):
        kwargs["progress"] = progress
    if _accepts(dispatcher, "cancel"):
        kwargs["cancel"] = cancel
    if _accepts(dispatcher, "context"):
        kwargs["context"] = context
    return dispatcher(name, args, **kwargs)


def _enable_tokens(args: Mapping[str, Any]) -> list[str]:
    """Flatten an ``enable_tools`` call's ``categories``/``category``/``tools``
    arguments into one ordered token list (groups, packs, ``family:``, names)."""
    tokens: list[str] = []
    for key in ("categories", "category", "tools", "tool"):
        raw = args.get(key)
        if raw is None:
            continue
        items = [raw] if isinstance(raw, str) else list(raw or ())
        tokens.extend(str(item) for item in items if str(item or "").strip())
    return _ordered_unique(tokens)


def _dispatch_meta(binding: HostToolBinding, args: dict[str, Any]) -> Any:
    if binding.name == "list_tool_categories":
        from services.agent.agent_turn import _tool_category_catalog_result

        return _tool_category_catalog_result()
    if binding.name != "enable_tools":
        return _MISSING
    tokens = _enable_tokens(args)
    callback = _ctx(binding.context, "enable_categories", None)
    if callable(callback):
        result = callback(tokens)
        if result is not None:
            return result
    from services.agent.tool_categories import resolve_tool_requests

    added, unknown = resolve_tool_requests(tokens)
    return {
        "ok": True,
        "enabled": tokens,
        "added": sorted(added),
        "unknown": unknown,
    }


def _guardrail_block(binding: HostToolBinding, args: dict[str, Any]) -> str | None:
    context = binding.context
    ledger = _ctx(context, "ledger", None)
    actor = str(_ctx(context, "actor", None) or "chudcode_host")
    calls = _ctx(context, "tool_calls", _ctx(context, "calls", None))
    if calls is None:
        calls = binding.metadata.get("tool_calls", [])

    custom = _ctx(context, "pre_dispatch_guardrail", None)
    if callable(custom):
        block = custom(binding.name, args, ledger, actor)
        if block is not None:
            return str(block)

    capability = _ctx(context, "capability", None)
    if capability is not None:
        try:
            from services.agent import scaffolds

            block = scaffolds.pre_dispatch_scaffolds(
                binding.name,
                args,
                profile=capability,
                ledger=ledger,
                calls=calls,
                actor=actor,
                effective_allow_set=None,
            )
            if block is not None:
                return str(block)
        except Exception as exc:  # noqa: BLE001 - scaffold subsystem is optional
            _debug_fallback(f"scaffold guard for {binding.name}", exc)

    from services.agent.agent_turn import (
        _drawing_export_review_block,
        _pre_dispatch_guardrail,
    )

    block = _pre_dispatch_guardrail(binding.name, args, ledger, actor)
    if block is None and binding.name == "export_drawing":
        block = _drawing_export_review_block(calls, args)
    return str(block) if block is not None else None


def dispatch_host_tool(
    binding: HostToolBinding,
    args: Mapping[str, Any] | None,
    progress: Any,
    cancel: Any,
) -> Any:
    """Dispatch one host call through CAD's run-scoped dispatch chokepoint.

    Policy refusals are returned in their existing machine-readable shape.  Tool
    exceptions deliberately propagate so Chudcode records a tool error rather
    than a fabricated successful result.
    """
    if not isinstance(binding, HostToolBinding):
        raise TypeError("binding must be a HostToolBinding")
    if args is None:
        supplied: dict[str, Any] = {}
    elif isinstance(args, Mapping):
        supplied = dict(args)
    else:
        raise TypeError("tool arguments must be a JSON object")
    # Validate input without reordering its keys.
    json.dumps(_json_safe(supplied), ensure_ascii=False, allow_nan=False)
    if _cancelled(cancel):
        return {
            "ok": False,
            "status": "cancelled",
            "cancelled": True,
            "executed": False,
        }

    refusal = _policy_refusal(binding.name, binding.context)
    if refusal is not None:
        return refusal

    meta = _dispatch_meta(binding, supplied)
    if meta is not _MISSING:
        return _json_safe(meta)

    mission = _ctx(binding.context, "mission_dispatch", None)
    mission_resolve = getattr(mission, "resolve", None)
    mission_fn = mission_resolve(binding.name) if callable(mission_resolve) else None
    dispatcher = _ctx(
        binding.context, "tool_dispatch", _ctx(binding.context, "dispatcher", None)
    )
    target = mission_fn if callable(mission_fn) else binding.handler
    call_args = _document_args(target, supplied, binding.context)

    blocked = _guardrail_block(binding, call_args)
    if blocked is not None:
        return {
            "ok": False,
            "status": "guardrail_blocked",
            "policy_refusal": True,
            "executed": False,
            "error": blocked,
        }

    def inner(name: str, merged: dict[str, Any]) -> Any:
        actual = _document_args(target, merged, binding.context)
        if callable(mission_fn):
            if _accepts(mission_fn, "progress"):
                actual.setdefault("progress", progress)
            if _accepts(mission_fn, "cancel"):
                actual.setdefault("cancel", cancel)
            return mission_fn(**actual)
        if callable(dispatcher):
            return _call_run_dispatcher(
                dispatcher, name, actual, binding.context, progress, cancel
            )
        if not callable(target):
            raise TypeError(f"tool {name!r} is no longer bound in this run")
        if _accepts(target, "progress"):
            actual.setdefault("progress", progress)
        if _accepts(target, "cancel"):
            actual.setdefault("cancel", cancel)
        return target(**actual)

    from services.agent.agent_turn import _dispatch_tool

    call_counts = _ctx(binding.context, "tool_call_counts", None)
    if call_counts is None and isinstance(binding.context, dict):
        call_counts = binding.context.setdefault("tool_call_counts", {})
    if call_counts is None:
        call_counts = binding.metadata.get("tool_call_counts")
    result = _dispatch_tool(
        binding.name,
        call_args,
        _call_counts=call_counts,
        _inner=inner,
        _loop_guard=_ctx(binding.context, "tool_loop_guard", None),
    )
    safe_result = _json_safe(result)
    records = _ctx(binding.context, "tool_calls", _ctx(binding.context, "calls", None))
    if records is None:
        records = binding.metadata.get("tool_calls")
    if isinstance(records, list):
        ok = not (isinstance(safe_result, Mapping) and safe_result.get("ok") is False)
        records.append(
            {
                "tool": binding.name,
                "args": _json_safe(call_args),
                "ok": ok,
                "result": safe_result,
            }
        )
    return safe_result


# --------------------------------------------------------------------------- JSON boundary


def _json_safe(value: Any, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    seen = _seen if _seen is not None else set()
    if isinstance(value, Mapping):
        ident = id(value)
        if ident in seen:
            return "<recursive>"
        seen.add(ident)
        try:
            return {str(key): _json_safe(item, seen) for key, item in value.items()}
        finally:
            seen.remove(ident)
    if isinstance(value, (list, tuple, set, frozenset)):
        ident = id(value)
        if ident in seen:
            return ["<recursive>"]
        seen.add(ident)
        try:
            return [_json_safe(item, seen) for item in value]
        finally:
            seen.remove(ident)
    return str(value)
