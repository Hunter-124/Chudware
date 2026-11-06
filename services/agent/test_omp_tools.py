from __future__ import annotations

import contextlib
import importlib.util
import json
import queue
import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError

from services.agent.omp_tools import (
    HostToolBinding,
    _turn_policy_refusal,
    build_host_tools,
    dispatch_host_tool,
    host_tool_result,
)


class _Policy:
    def __init__(self, denied=()):
        self.denied = set(denied)

    def deny_reason(self, name):
        return f"{name} denied by existing CAD gate" if name in self.denied else None

    def refusal(self, name):
        return OrderedDict(
            (
                ("ok", False),
                ("reason", self.deny_reason(name)),
                ("hint", "existing gate"),
            )
        )


@dataclass
class _Entry:
    fn: object
    description: str
    input_schema: dict
    load_mode: str = "always"
    cad_owned: bool = False


def _alpha(value: str, doc_id: str | None = None):
    return OrderedDict((("ok", True), ("value", value), ("doc_id", doc_id)))


def _beta(count: int = 1):
    return {"ok": True, "count": count}


def _entry(fn, description, schema=None, load_mode="always", cad_owned=False):
    return _Entry(
        fn=fn,
        description=description,
        input_schema=schema or {"type": "object", "properties": {}},
        load_mode=load_mode,
        cad_owned=cad_owned,
    )


# Turn-loop primitives the adapter floors onto every surface, restricted or not.
_META_FLOOR = frozenset({"enable_tools", "list_tool_categories"})


def _context(entries, **extra):
    context = {
        "tool_entries": entries,
        "resolver": lambda name: getattr(entries.get(name), "fn", None),
        "policy_registry": _Policy(),
        "omp_builtin_names": (),
    }
    context.update(extra)
    return context


@contextlib.contextmanager
def _unimportable(module):
    """Make ``import module`` raise ImportError, touching only that sys.modules key."""
    saved = sys.modules.get(module)
    sys.modules[module] = None
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop(module, None)
        else:
            sys.modules[module] = saved


class HostToolDefinitionTests(unittest.TestCase):
    def test_lite_does_not_advertise_unregistered_role_tools(self):
        entries = {"describe_part": _entry(_alpha, "Describe a part")}
        context = {
            "allowed_tools": ["describe_part", "mission_start", "advise", "workspace_status"],
            "resolver": lambda name: _alpha,
            "policy_registry": _Policy(),
            "omp_builtin_names": (),
        }
        with mock.patch("services.agent.omp_tools._load_registry_entries", return_value=entries), \
             mock.patch("services.mcp.server._LITE", True):
            names = {binding.name for binding in build_host_tools(context)}
        self.assertIn("describe_part", names)
        self.assertFalse({"mission_start", "advise", "workspace_status"} & names)
        self.assertTrue(_META_FLOOR <= names)

    def test_delegate_definition_advertises_live_roles_and_effort(self):
        from types import SimpleNamespace
        from services.agent.mission_dispatch import MissionDispatch
        from services.agent.role_catalog import DELEGATE_SPECIALISTS
        from services.agent.roles import load_all_roles

        dispatch = MissionDispatch(run_id="delegate-definition")
        entry = SimpleNamespace(fn=dispatch.delegate, description="", input_schema=None)
        bindings = build_host_tools(_context({"delegate": entry}, allowed_tools=["delegate"]))
        binding = next(b for b in bindings if b.name == "delegate")
        cards = load_all_roles()
        lines = binding.description.splitlines()
        for role in DELEGATE_SPECIALISTS:
            if role in cards:
                line = next(line for line in lines if line.startswith(f"{role}: "))
                words = line.split(": ", 1)[1].split()
                self.assertLessEqual(len(words), 8)
                self.assertTrue(all(word.strip(".,;:—-") in cards[role].goal for word in words))
        self.assertFalse(any(line.startswith("orchestrator: ") for line in lines))
        for field in ("thinking", "reasoning_effort", "effort"):
            self.assertIn(field, binding.input_schema["properties"])

    def test_general_agent_loads_domain_schemas_only_on_request(self):
        from services.agent.roles import load_role
        from services.agent.tool_categories import MAIN_CHAT_CORE_TOOLS

        entries = {
            "cad_guide": _entry(_alpha, "CAD guide", {"type": "object", "properties": {"value": {"type": "string"}}}),
            "add_features": _entry(_beta, "Build features"),
        }
        context = _context(entries, role_card=load_role("general_agent"),
                           allowed_tools=list(MAIN_CHAT_CORE_TOOLS), mode="agent")
        initial = {binding.name: binding for binding in build_host_tools(context)}
        self.assertEqual(initial["cad_guide"].load_mode, "discoverable")
        self.assertEqual(initial["add_features"].load_mode, "discoverable")
        self.assertFalse(initial["cad_guide"].active)
        self.assertFalse(initial["add_features"].active)
        context["categories"] = ["cad_guide", "add_features"]
        expanded = {binding.name: binding for binding in build_host_tools(context)}
        self.assertIn("cad_guide", expanded)
        self.assertIn("add_features", expanded)
        self.assertEqual(expanded["cad_guide"].input_schema, entries["cad_guide"].input_schema)
        self.assertTrue(expanded["cad_guide"].active)
        self.assertTrue(expanded["add_features"].active)
        agent_definitions = [binding.as_dict() for binding in build_host_tools(context)]
        context["mode"] = "ask"
        self.assertEqual(
            [binding.as_dict() for binding in build_host_tools(context)], agent_definitions,
        )

    def test_unrestricted_main_matches_live_registry_order_schema_and_load_mode(self):
        alpha_schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }
        entries = OrderedDict(
            (
                (
                    "alpha",
                    _entry(
                        _alpha, "Alpha from live registry", alpha_schema, "deferred"
                    ),
                ),
                ("beta", _entry(_beta, "Beta from live registry")),
            )
        )
        bindings = build_host_tools(_context(entries, allowed_tools=None))

        # Unrestricted access publishes the full catalog, but presents only core
        # discovery tools until explicit category enablement.
        names = [binding.name for binding in bindings]
        self.assertEqual({name for name in names if name in entries}, set(entries))
        by_name = {binding.name: binding for binding in bindings}
        self.assertEqual(by_name["alpha"].description, entries["alpha"].description)
        self.assertEqual(by_name["alpha"].input_schema, alpha_schema)
        self.assertEqual(by_name["alpha"].load_mode, "discoverable")
        self.assertFalse(by_name["alpha"].active)
        self.assertEqual(by_name["beta"].load_mode, "discoverable")
        self.assertFalse(by_name["beta"].active)
        self.assertTrue(by_name["enable_tools"].active)
        self.assertTrue(by_name["list_tool_categories"].active)
        self.assertEqual(
            list(by_name["alpha"].definition),
            ["name", "description", "input_schema", "load_mode", "active"],
        )

    def test_unrestricted_root_and_compact_fixture_keep_catalog_deferred(self):
        from services.agent.tool_categories import MAIN_CHAT_CORE_TOOLS

        entries = {
            "get_part": _entry(_alpha, "Read part"),
            "add_features": _entry(_beta, "Edit part"),
            **{f"domain_{i}": _entry(_beta, f"Domain {i}") for i in range(100)},
        }
        root = {binding.name: binding for binding in build_host_tools(
            _context(entries, allowed_tools=None, mode="agent", categories=[]))}
        fixture = {binding.name: binding for binding in build_host_tools(
            _context(entries, allowed_tools=list(MAIN_CHAT_CORE_TOOLS),
                     mode="agent", categories=[]))}
        self.assertEqual(set(root), set(fixture))
        self.assertEqual({n for n, b in root.items() if b.active},
                         {n for n, b in fixture.items() if b.active})
        self.assertTrue(root["get_part"].active)
        self.assertTrue(all(not root[f"domain_{i}"].active for i in range(100)))
        self.assertFalse(root["add_features"].active)
        self.assertLessEqual(sum(b.active for b in root.values()), 30)

    def test_category_enablement_publishes_active_catalog_subset(self):
        from types import SimpleNamespace
        from services.agent.omp_roles import _register_category_refresh

        entries = {"get_part": _entry(_alpha, "Read part"),
                   "add_features": _entry(_beta, "Edit part")}
        context = _context(entries, allowed_tools=None, categories=[])
        bindings = build_host_tools(context)
        published = []
        client = SimpleNamespace(_omp_bridge=SimpleNamespace(
            publish_tools=lambda definitions: published.append(definitions)))
        _register_category_refresh(context, bindings)(client)
        result = context["enable_categories"](["add_features"])
        self.assertIn("add_features", result["added"])
        by_name = {item["name"]: item for item in published[-1]}
        self.assertTrue(by_name["get_part"]["active"])
        self.assertTrue(by_name["add_features"]["active"])
        self.assertEqual(by_name["add_features"]["load_mode"], "essential")

    def test_category_publication_failure_rolls_back_selection(self):
        from types import SimpleNamespace
        from services.agent.omp_roles import _register_category_refresh

        context = _context({"add_features": _entry(_beta, "Edit part")},
                           allowed_tools=None, categories=[])
        bindings = build_host_tools(context)
        def reject(_definitions):
            raise RuntimeError("bridge unavailable")
        client = SimpleNamespace(_omp_bridge=SimpleNamespace(publish_tools=reject))
        _register_category_refresh(context, bindings)(client)
        with self.assertRaisesRegex(RuntimeError, "bridge unavailable"):
            context["enable_categories"](["add_features"])
        self.assertEqual(context["categories"], [])

    def test_extension_descriptor_and_factory_use_official_load_modes(self):
        binding = HostToolBinding("example", "Example", {"type": "object"},
                                  load_mode="discoverable", handler=_beta)
        self.assertEqual(binding.as_dict()["load_mode"], "discoverable")
        projected = binding.to_host_tool(lambda **kwargs: kwargs)
        self.assertEqual(projected["load_mode"], "discoverable")
        self.assertEqual(projected["parameters"], binding.input_schema)

    def test_callable_schemas_resolve_postponed_array_annotations(self):
        def select(indices: list[int], labels: list[str] | None = None):
            return {"ok": True, "total": sum(indices), "labels": labels}

        entries = {"select": {"fn": select, "description": "Select indices"}}
        bindings = build_host_tools(_context(entries, allowed_tools=None,
            resolver=lambda name: select if name == "select" else None))
        binding = next(item for item in bindings if item.name == "select")
        validator = Draft202012Validator(binding.input_schema)
        for args in (
            {"indices": [1, 2]},
            {"indices": [3], "labels": None},
            {"indices": [4], "labels": ["bore"]},
        ):
            validator.validate(args)
            result = dispatch_host_tool(binding, args, None, None)
            self.assertEqual(result["total"], sum(args["indices"]))
        for args in ({}, {"indices": 3}, {"indices": ["1"]}, {"indices": [1], "labels": "bore"}):
            with self.assertRaises(ValidationError):
                validator.validate(args)

    def test_enable_tools_advertises_string_arrays_for_dynamic_loading(self):
        bindings = build_host_tools(_context({}, allowed_tools=None))
        binding = next(item for item in bindings if item.name == "enable_tools")
        validator = Draft202012Validator(binding.input_schema)
        validator.validate({"tools": ["add_features", "query_geometry"]})
        validator.validate({"categories": ["cad"], "tools": None})
        for args in ({"tools": [123]}, {"categories": "cad"}):
            with self.assertRaises(ValidationError):
                validator.validate(args)

    def test_a_catch_all_is_never_advertised_as_a_tool_argument(self):
        """A ``**kwargs`` catch-all is not a parameter a caller can name.

        FastMCP derives ``inputSchema`` from the signature through Pydantic,
        which models ``**extra: Any`` as a field named ``extra`` and — having no
        default for it — marks that field REQUIRED. Chudcode validates every
        host call against the advertised schema, so the real ``task_list`` tool
        could not be called at all: every attempt came back ``Validation failed
        for tool "task_list": - extra: is required`` and the live plan panel
        published nothing. An optional catch-all is no better — it invites the
        model to fill a field that lands in the tool's user-intent channel.
        """

        def planner(items=None, title=None, run_id="", **extra):
            return {"ok": True, "items": items, "title": title}

        pydantic_shaped = {
            "type": "object",
            "properties": {
                "items": {"default": None, "title": "Items"},
                "title": {"default": None, "title": "Title"},
                "run_id": {"default": "", "title": "Run Id"},
                "extra": {"title": "Extra"},
            },
            "required": ["extra"],
        }
        entries = OrderedDict(
            (("task_list", _entry(planner, "Publish the plan", pydantic_shaped)),)
        )
        binding = build_host_tools(_context(entries, allowed_tools=None))[0]

        self.assertEqual(binding.name, "task_list")
        self.assertNotIn("extra", binding.input_schema["properties"])
        self.assertNotIn("required", binding.input_schema)
        # The real arguments survive untouched.
        self.assertEqual(
            sorted(binding.input_schema["properties"]), ["items", "run_id", "title"]
        )

    def test_a_genuinely_required_argument_still_survives_the_catch_all_strip(self):
        def sized(width, depth=1, **extra):
            return {"ok": True, "width": width, "depth": depth}

        entries = OrderedDict(
            (
                (
                    "sized",
                    _entry(
                        sized,
                        "Sized",
                        {
                            "type": "object",
                            "properties": {
                                "width": {"type": "number"},
                                "depth": {"type": "number", "default": 1},
                                "extra": {"title": "Extra"},
                            },
                            "required": ["width", "extra"],
                        },
                    ),
                ),
            )
        )
        binding = next(b for b in build_host_tools(_context(entries, allowed_tools=None))
                       if b.name == "sized")

        self.assertEqual(binding.input_schema["required"], ["width"])
        self.assertNotIn("extra", binding.input_schema["properties"])

    def test_restricted_subagent_preserves_explicit_empty_and_nonempty_states(self):
        entries = OrderedDict(
            (
                ("alpha", _entry(_alpha, "alpha")),
                ("beta", _entry(_beta, "beta")),
            )
        )
        empty = build_host_tools(
            _context(entries, allowed_tools=[], subagent_id="sub-1")
        )
        selected = build_host_tools(
            _context(entries, allowed_tools=["beta"], subagent_id="sub-1")
        )

        # Unselected catalog entries stay discoverable even with a role allowlist.
        empty_modes = {binding.name: binding for binding in empty}
        selected_modes = {binding.name: binding for binding in selected}
        self.assertEqual(empty_modes["alpha"].load_mode, "discoverable")
        self.assertFalse(empty_modes["alpha"].active)
        self.assertEqual(selected_modes["beta"].load_mode, "discoverable")
        self.assertFalse(selected_modes["beta"].active)

    def test_role_and_category_finalization_reuses_live_helpers(self):
        class Role:
            concrete_tools: ClassVar[list[str]] = ["alpha"]

        entries = OrderedDict(
            (
                ("alpha", _entry(_alpha, "role tool")),
                ("measure_part", _entry(_beta, "category tool")),
                ("unrelated", _entry(_beta, "must stay out")),
            )
        )
        bindings = build_host_tools(
            _context(entries, role_card=Role(), categories=["measure"])
        )
        names = [binding.name for binding in bindings]

        self.assertIn("alpha", names)
        self.assertIn("measure_part", names)
        self.assertEqual(next(b.load_mode for b in bindings if b.name == "unrelated"), "discoverable")


    def test_omp_builtin_collision_requires_explicit_cad_ownership(self):
        entries = OrderedDict(
            (
                ("read", _entry(_alpha, "collision")),
                ("write", _entry(_alpha, "owned", cad_owned=True)),
                ("cad_unique", _entry(_alpha, "unique")),
            )
        )
        context = _context(entries, allowed_tools=None)
        context.pop("omp_builtin_names")
        names = [binding.name for binding in build_host_tools(context)]
        # The meta floor rides along on every surface; the collision verdict is about
        # `read` (dropped, unowned) versus `write` (kept, explicitly cad_owned).
        self.assertNotIn("read", names)
        self.assertIn("write", names)
        self.assertIn("cad_unique", names)

    def test_effective_policy_removes_manufacturing_execution_tools(self):
        entries = OrderedDict(
            (
                ("alpha", _entry(_alpha, "ordinary")),
                ("cnc_execute", _entry(_beta, "physical execution")),
            )
        )
        context = _context(entries, allowed_tools=None)
        context["policy_registry"] = _Policy({"cnc_execute"})
        self.assertEqual(
            [
                item.name
                for item in build_host_tools(context)
                if item.name not in _META_FLOOR
            ],
            ["alpha"],
        )

    def test_every_binding_carries_a_description_including_the_meta_floor(self):
        """Chudcode rejects a whole set_host_tools call over one empty description, so an
        undocumented tool takes the entire turn down with
        `provider_error - set_host_tools: Host tool "..." must provide a non-empty
        description` before any tool runs. enable_tools/list_tool_categories are the
        exposure: they have no registry entry, so their description can only come from the
        handler docstring, and they are on the meta floor of every restricted mode."""
        entries = OrderedDict((("alpha", _entry(_alpha, "ordinary")),))
        bindings = build_host_tools(
            _context(entries, mode="agent", allowed_tools=["alpha"])
        )
        names = [binding.name for binding in bindings]
        self.assertIn("enable_tools", names)
        self.assertIn("list_tool_categories", names)
        self.assertEqual(
            [binding.name for binding in bindings if not binding.description.strip()],
            [],
        )

    def test_unrestricted_surface_still_carries_the_meta_tool_floor(self):
        """`allowed_tools=None` is what agent_admin_turn sends for an ordinary agent turn.
        The meta floor was guarded on a restriction existing, so the ONE case with the
        largest tool surface was also the only one without `enable_tools` /
        `list_tool_categories` -- they are turn-loop primitives, not registry entries, so
        the unrestricted registry passthrough cannot supply them. The model then reasoned
        about enabling tool categories, found no such tool, and spent the turn in `bash`
        and `write` instead of CAD geometry."""
        entries = OrderedDict(
            (("alpha", _entry(_alpha, "ordinary")), ("beta", _entry(_beta, "ordinary")))
        )
        names = [
            binding.name
            for binding in build_host_tools(
                _context(entries, mode="agent", allowed_tools=None)
            )
        ]
        self.assertIn("alpha", names)
        self.assertIn("beta", names)
        self.assertIn("enable_tools", names)
        self.assertIn("list_tool_categories", names)

    def _mode_entries(self):
        return OrderedDict(
            (
                ("add_features", _entry(_beta, "geometry mutator")),
                ("measure_part", _entry(_beta, "read-only measure")),
            )
        )

    def test_mode_and_preferences_never_change_the_published_surface(self):
        """Prefix-cache invariant: Agent/Ask/Plan and any derived preferences publish
        identical definitions (names, order, descriptions, schemas, activation);
        read-only policy is enforced at dispatch instead."""
        def preferences(**changes):
            values = {"mode": "agent", "delegation_depth": 2, "advisor": True,
                      "task_list": True, "verify_battery": True, **changes}
            return type("Preferences", (), values)()

        loaded = mock.patch(
            "services.agent.tool_categories.resolve_tool_requests",
            return_value=({"add_features"}, []),
        )
        with loaded:
            surfaces = [
                [b.as_dict() for b in build_host_tools(
                    _context(self._mode_entries(), categories=["modeling"], **extra)
                )]
                for extra in (
                    {"mode": "agent"},
                    {"mode": "ask"},
                    {"mode": "plan"},
                    {"mode": "agent", "plan_only": True},
                    {"mode": "ask", "turn_preferences": preferences(mode="ask")},
                    {"mode": "agent", "turn_preferences": preferences(
                        delegation_depth=0, advisor=False, task_list=False,
                        verify_battery=False)},
                )
            ]
        names = [item["name"] for item in surfaces[0]]
        self.assertIn("add_features", names)
        self.assertIn("measure_part", names)
        for surface in surfaces[1:]:
            self.assertEqual(surface, surfaces[0])

    def test_floor_module_import_error_fails_the_build(self):
        with _unimportable("services.agent.copilot_mode_tools"):
            with self.assertRaises(ImportError):
                build_host_tools(_context(self._mode_entries()))

    def test_registry_import_error_is_not_an_empty_surface(self):
        context = _context(OrderedDict())
        del context["tool_entries"]
        with _unimportable("services.mcp.server"):
            with self.assertRaises(ImportError):
                build_host_tools(context)

    def test_policy_evaluation_error_fails_the_build(self):
        class _BrokenPolicy:
            def deny_reason(self, name):
                raise RuntimeError(f"policy for {name} unreadable")

        context = _context(self._mode_entries(), allowed_tools=None)
        context["policy_registry"] = _BrokenPolicy()
        with self.assertRaisesRegex(RuntimeError, "unreadable"):
            build_host_tools(context)

    def test_live_registry_settings_error_fails_the_build(self):
        """A Settings read that raises refuses every policed tool and then fails the
        build visibly, instead of a silent meta-only (or unpoliced) surface."""
        context = _context(self._mode_entries(), mode="agent", allowed_tools=None)
        del context["policy_registry"]  # the live tool_registry decides
        with mock.patch(
            "services.agent.config.get_tool_options",
            side_effect=RuntimeError("tool_options unreadable"),
        ), self.assertRaisesRegex(RuntimeError, "tool_options unreadable"):
            build_host_tools(context)


class HostToolDispatchTests(unittest.TestCase):
    def _binding(self, name="alpha", handler=_alpha, **context):
        ctx = {"policy_registry": _Policy(), **context}
        return HostToolBinding(
            name=name,
            description=name,
            input_schema={"type": "object"},
            handler=handler,
            context=ctx,
        )

    def test_mode_policy_error_at_dispatch_never_runs_the_mutator(self):
        ran = []
        binding = self._binding(
            "add_features", handler=lambda: ran.append("add_features"), mode="ask"
        )
        with mock.patch(
            "services.agent.copilot_mode_tools.apply_mode_tools",
            side_effect=RuntimeError("mode contract unreadable"),
        ), self.assertRaisesRegex(RuntimeError, "mode contract unreadable"):
            dispatch_host_tool(binding, {}, None, None)
        self.assertEqual(ran, [])

    def test_turn_preferences_refuse_at_dispatch_what_they_no_longer_hide(self):
        """Delegation depth 0 and an off advisor leave the tools published, so the
        call itself is refused (with a reason the model can act on)."""
        ran = []
        preferences = type("Preferences", (), {
            "mode": "agent", "delegation_depth": 0, "advisor": False,
            "task_list": False, "verify_battery": False,
        })()
        for name in ("delegate", "mission_start", "run_dag", "advise"):
            binding = self._binding(
                name, handler=lambda name=name: ran.append(name), mode="agent",
                turn_preferences=preferences,
            )
            out = dispatch_host_tool(binding, {}, None, None)
            self.assertEqual(out["status"], "turn_policy_refused", name)
            self.assertIs(out["executed"], False)
            self.assertIn("this turn", out["error"])
        self.assertEqual(ran, [])
        permissive = type("Preferences", (), {
            "mode": "agent", "delegation_depth": 2, "advisor": True,
            "task_list": True, "verify_battery": True,
        })()
        for name in ("delegate", "advise", "add_features"):
            self.assertIsNone(_turn_policy_refusal(name, {"mode": "agent", "turn_preferences": permissive}))
        self.assertIsNone(_turn_policy_refusal("describe_part", {
            "mode": "agent", "turn_preferences": preferences}))
        # Edit mode keeps its roster at dispatch rather than by hiding tools.
        self.assertEqual(_turn_policy_refusal("new_assembly", {"mode": "edit"})["status"], "mode_refused")

    def test_policy_evaluation_error_at_dispatch_never_runs_the_tool(self):
        class _BrokenPolicy:
            def deny_reason(self, name):
                raise RuntimeError("policy unreadable")

        ran = []
        binding = self._binding("alpha", handler=lambda: ran.append("alpha"))
        binding.context["policy_registry"] = _BrokenPolicy()
        with self.assertRaisesRegex(RuntimeError, "policy unreadable"):
            dispatch_host_tool(binding, {}, None, None)
        self.assertEqual(ran, [])

    def test_live_registry_settings_error_refuses_dispatch_in_shape(self):
        ran = []
        binding = self._binding("alpha", handler=lambda: ran.append("alpha"))
        del binding.context["policy_registry"]  # the live tool_registry decides
        with mock.patch(
            "services.agent.config.get_tool_options",
            side_effect=RuntimeError("tool_options unreadable"),
        ):
            result = dispatch_host_tool(binding, {}, None, None)
        self.assertEqual(list(result), ["ok", "error", "code", "reason", "hint"])
        self.assertEqual(result["code"], "policy_unevaluable")
        self.assertEqual(result["error"], result["reason"])
        self.assertFalse(result["ok"])
        self.assertIn("could not be evaluated", result["reason"])
        self.assertIn("tool_options unreadable", result["reason"])
        self.assertEqual(ran, [])

    def test_success_forwards_progress_cancel_and_document_context(self):
        observed = []
        progress_events = []

        def dispatcher(name, args, progress=None, cancel=None, context=None):
            progress({"step": 1})
            observed.append((name, dict(args), cancel, context["actor"]))
            return OrderedDict((("ok", True), ("path", Path("part.step"))))

        cancel = lambda: False
        binding = self._binding(
            tool_dispatch=dispatcher,
            doc_id="doc-7",
            actor="feature_modeler",
        )
        result = dispatch_host_tool(
            binding, {"value": "x"}, progress_events.append, cancel
        )

        self.assertEqual(progress_events, [{"step": 1}])
        self.assertEqual(observed[0][0], "alpha")
        self.assertEqual(observed[0][1]["doc_id"], "doc-7")
        self.assertIs(observed[0][2], cancel)
        self.assertEqual(observed[0][3], "feature_modeler")
        self.assertEqual(list(result), ["ok", "path"])
        self.assertEqual(result["path"], "part.step")

    def test_tool_records_keep_latest_64_calls(self):
        records = []
        counts = {}
        for index in range(70):
            binding = self._binding(
                name=f"tool_{index}",
                handler=lambda value: {"ok": True, "value": value},
                tool_calls=records,
                tool_call_counts=counts,
            )
            dispatch_host_tool(binding, {"value": index}, None, None)
        self.assertEqual(len(records), 64)
        self.assertEqual(
            [record["result"]["value"] for record in records],
            list(range(6, 70)),
        )

    def test_host_tool_execute_wraps_every_result_in_the_content_envelope(self):
        """A `host_tool_result` whose result lacks `content` never resolves inside
        Chudcode (write xd://<tool> hangs until the turn cap), and CAD tools return
        raw JSON objects — refusals included. The RPC-facing execute must always
        hand back {content:[text], details:<raw>}; progress updates likewise."""
        updates = []

        class Ctx:
            cancelled = False

            def send_update(self, payload):
                updates.append(payload)

        def dispatcher(name, args, progress=None):
            progress({"step": 1})
            return {"ok": False, "reason": "workspace_root_unavailable"}

        binding = self._binding(tool_dispatch=dispatcher)
        captured = {}
        tool = binding.to_host_tool(lambda **kw: captured.update(kw) or kw)
        out = tool["execute"]({}, Ctx())
        self.assertEqual(out["details"], {"ok": False, "reason": "workspace_root_unavailable"})
        self.assertEqual(out["content"][0]["type"], "text")
        self.assertIn('"workspace_root_unavailable"', out["content"][0]["text"])
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["details"], {"step": 1})
        self.assertEqual(json.loads(updates[0]["content"][0]["text"]), {"step": 1})

    def test_nested_report_preserves_exception_and_trusted_child_identity(self):
        from services.agent.omp_bridge import _ToolContext

        events = []

        def fail():
            raise ValueError("invalid geometry")

        binding = self._binding("alpha", handler=fail, host_call_observer=events.append)
        execute = binding.to_host_tool(lambda **kw: kw)["execute"]
        context = _ToolContext({
            "tool_call_id": "js-write-failure", "agent_id": "Builder",
            "parent_agent_id": "Main", "session_id": "session",
        })
        with self.assertRaisesRegex(ValueError, "invalid geometry"):
            execute({}, context)
        self.assertEqual([event["type"] for event in events], ["host_tool_start", "host_tool_end"])
        self.assertTrue(all(event["agent_id"] == "Builder" and event["parent_agent_id"] == "Main"
                            for event in events))
        self.assertIs(events[-1]["isError"], True)
        self.assertIn("invalid geometry", events[-1]["error"])

    def test_nested_report_keeps_failure_when_result_envelope_was_shortened(self):
        from services.agent.omp_bridge import _ToolContext

        events = []
        binding = self._binding(
            "alpha", handler=lambda: {"ok": False, "error": "no visible circle"},
            host_call_observer=events.append,
        )
        execute = binding.to_host_tool(lambda **kw: kw)["execute"]
        context = _ToolContext({
            "tool_call_id": "js-write-shortened", "agent_id": "Main",
            "parent_agent_id": None, "session_id": "session",
        })
        shortened = {"content": [{"type": "text", "text": "{}"}],
                     "details": {"_truncated": {"full_result": "result.json"}}}
        with mock.patch("services.agent.omp_tools.host_tool_result", return_value=shortened):
            self.assertIs(execute({}, context), shortened)
        self.assertIs(events[-1]["ok"], False)
        self.assertIs(events[-1]["result"], shortened)

    def test_nested_binding_refusal_is_reported_without_cross_cell_leak(self):
        from services.agent.omp_bridge import _ToolContext

        events = []
        binding = self._binding("alpha", handler=lambda: {"ok": True},
                                host_call_observer=events.append)
        execute = binding.to_host_tool(lambda **kw: kw)["execute"]
        context = _ToolContext({"tool_call_id": "js-write-refused", "agent_id": "Main",
                                "session_id": "session"})
        refusal = {"ok": False, "reason": "workspace_root_unavailable"}
        with mock.patch("services.agent.omp_tools._binding_context_refusal", return_value=refusal):
            self.assertEqual(execute({}, context)["details"], refusal)
        self.assertEqual([event["type"] for event in events], ["host_tool_start", "host_tool_end"])
        self.assertIs(events[-1]["ok"], False)

        events.clear()
        not_found = {"ok": False, "reason": "not_found", "code": "not_found"}
        with mock.patch("services.agent.omp_tools._binding_context_refusal", return_value=not_found):
            self.assertEqual(execute({"private": "value"}, context)["details"], not_found)
        self.assertEqual(events, [])

    def test_direct_call_and_observer_failure_do_not_change_tool_result(self):
        from services.agent.omp_bridge import _ToolContext

        events = []
        binding = self._binding("alpha", handler=lambda: {"ok": True},
                                host_call_observer=events.append)
        execute = binding.to_host_tool(lambda **kw: kw)["execute"]
        direct = _ToolContext({"tool_call_id": "direct-call", "agent_id": "Main",
                               "session_id": "session"})
        self.assertEqual(execute({}, direct)["details"], {"ok": True})
        self.assertEqual(events, [])

        def broken_observer(_frame):
            raise RuntimeError("feed unavailable")

        binding.context["host_call_observer"] = broken_observer
        nested = _ToolContext({"tool_call_id": "js-write-call", "agent_id": "Main",
                               "session_id": "session"})
        self.assertEqual(execute({}, nested)["details"], {"ok": True})

    def test_tool_json_reaches_the_model_smaller_but_identical(self):
        """c219: get_part's indent=2 text put every topology pair's numbers on their
        own lines (4,067 lines, 65.8 KB) and spilled to an artifact the model spent
        eight calls paging. The model text must parse back to exactly the tool's
        result, keep a line per record, keep trailing notes, and never grow."""
        tree = {
            "owner_map": {
                f"f{i}": {"bodies": [1], "edges": [[1, 3 * i + k] for k in range(12)],
                          "faces": [[1, 2 * i + k] for k in range(6)]}
                for i in range(1, 40)
            },
            "tree": {"features": [
                {"id": f"f{i}", "name": f"Rib — {i}", "type": "extrude",
                 "params": {"depth": {"value": 2.5, "expr": None}}, "suppressed": False}
                for i in range(1, 40)
            ]},
        }
        pretty = json.dumps(tree, indent=2)
        note = "\nnote: another client edited part doc-1 since your last call"
        for raw in (tree, pretty + note):
            text = host_tool_result(raw)["content"][0]["text"]
            value, end = json.JSONDecoder().raw_decode(text)
            self.assertEqual(value, tree)
            self.assertLess(len(text), 0.6 * len(pretty))
            self.assertGreater(text.count("\n"), 39)
        self.assertEqual(text[end:], note)
        for unchanged in ('{"ok":true}', "Rendered 3 views.", "[not json"):
            self.assertEqual(host_tool_result(unchanged)["content"][0]["text"], unchanged)

    def test_a_kwargs_catch_all_is_not_a_plumbing_channel(self):
        """`add_features(ops=None, doc_id=None, background=False, **extra)` reads
        every unrecognised keyword as USER INTENT — a feature param, a design-intent
        note, or a hard error naming the real parameter set. Injecting run context
        into that catch-all made the core modeling tool reject every call, so the
        agent could not build geometry at all and fell back to writing STEP files by
        hand. Only an explicitly declared parameter receives run context."""
        seen = {}

        def add_features(ops=None, doc_id=None, background=False, **extra):
            seen["extra"] = dict(extra)
            seen["doc_id"] = doc_id
            return {"ok": True}

        binding = self._binding(
            name="add_features",
            handler=add_features,
            doc_id="doc-7",
            actor="feature_modeler",
            run_id="gui-1",
            node_id="n1",
            parent_id="p1",
            subagent_id="s1",
        )
        result = dispatch_host_tool(
            binding, {"ops": [{"op": "add_feature"}]}, lambda _p: None, lambda: False
        )
        self.assertEqual(result, {"ok": True})
        # doc_id is declared, so it still arrives; nothing else leaks.
        self.assertEqual(seen["doc_id"], "doc-7")
        self.assertEqual(seen["extra"], {})

    def test_a_declared_parameter_still_receives_run_context(self):
        seen = {}

        def wants_context(
            ops=None, *, doc_id=None, actor=None, run_id=None, progress=None, cancel=None
        ):
            seen.update(
                doc_id=doc_id, actor=actor, run_id=run_id,
                progress=progress, cancel=cancel,
            )
            return {"ok": True}

        cancel = lambda: False
        binding = self._binding(
            name="wants_context",
            handler=wants_context,
            doc_id="doc-9",
            actor="feature_modeler",
            run_id="gui-2",
        )
        dispatch_host_tool(binding, {}, None, cancel)
        self.assertEqual(seen["doc_id"], "doc-9")
        self.assertEqual(seen["actor"], "feature_modeler")
        self.assertEqual(seen["run_id"], "gui-2")
        self.assertIs(seen["cancel"], cancel)

    def test_model_selectors_preserve_omission_but_ledger_keeps_run_context(self):
        from mcp.server.fastmcp import FastMCP
        from services.agent import ledger, request_context as tenancy, workspace
        from services.mcp import core_bridge, tool_registry
        from services.mcp.ledger_tools import register_ledger_tools
        from services.mcp.workspace_tools import register_workspace_tools

        mcp = FastMCP("selector-regression")
        with mock.patch.dict(tool_registry._CATALOG, {}, clear=True):
            register_workspace_tools(tool_registry.view(mcp, "workspace"))
            register_ledger_tools(tool_registry.view(mcp, "ledger"))
        entries = mcp._tool_manager._tools
        context = _context(entries, run_id="gui-orientation", actor="modeler")

        def call(name, args):
            return dispatch_host_tool(bindings[name], args, None, None)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            ledger, "cache_root", return_value=Path(root)
        ), mock.patch.dict(workspace._session_registry, {}, clear=True):
            # c237/c239: a GUI chat turn registers no multi-doc session. The
            # omitted run_id stays omitted and the snapshot shows the real
            # workspace: the turn's project and the documents open in the engine.
            project = Path(root) / "project"
            project.mkdir()
            rows = [
                {"doc_id": "doc-in", "name": "arm", "path": str(project / "arm.step+")},
                {"doc_id": "doc-out", "name": "ref", "path": str(Path(root) / "ref.step+")},
                {"doc_id": "doc-new", "name": "Part1", "path": ""},
            ]

            def engine(method, params=None):
                self.assertEqual(method, "doc_list")
                return {"docs": rows}

            with tenancy.bind_roots(str(project)), mock.patch.object(core_bridge, "call", engine):
                bindings = {b.name: b for b in build_host_tools(context)}
                turn = call("workspace_status", {})
            self.assertTrue(turn["ok"], turn)
            self.assertEqual(turn["scope"], "turn")
            self.assertEqual(turn["run_id"], "")
            self.assertEqual(turn["project_dir"], str(project))
            self.assertEqual(
                {d["doc_id"]: d["location"] for d in turn["docs"]},
                {"doc-in": "project", "doc-out": "outside_project", "doc-new": "unsaved"},
            )

            def engine_down(method, params=None):
                raise core_bridge.CoreError("engine_connection_lost")

            with mock.patch.object(core_bridge, "call", engine_down):
                down = call("workspace_status", {})
            self.assertFalse(down["ok"])
            self.assertEqual(down["reason"], "engine_unavailable")
            self.assertNotIn("docs", down)

            unknown = call("workspace_status", {"run_id": "gui-1790799388213556-0"})
            self.assertFalse(unknown["ok"])
            self.assertEqual(unknown["reason"], "unknown_run_id")
            self.assertIn("workspace_status()", unknown["do_instead"])

            session = workspace.WorkspaceSession(run_id="workspace-alpha")
            session.open_assembly_doc("assembly-doc", "orchestrator")
            workspace.register_session(session)
            ambient = call("workspace_status", {})
            self.assertTrue(ambient["ok"])
            self.assertEqual(ambient["run_id"], "workspace-alpha")
            self.assertEqual(ambient["docs"][0]["doc_id"], "assembly-doc")
            unknown = call("workspace_status", {"run_id": "workspace-alpho"})
            self.assertFalse(unknown["ok"])
            self.assertEqual(unknown["did_you_mean"], "workspace-alpha")
            self.assertEqual(unknown["known_run_ids"], ["workspace-alpha"])

            written = call("ledger_note", {"kind": "note", "payload": {"text": "orient"}})
            self.assertTrue(written["ok"])
            self.assertEqual(written["run_id"], "gui-orientation")
            events = call("ledger_events", {})
            self.assertEqual(events["events"][0]["payload"], {"text": "orient"})
            self.assertEqual(events["events"][0]["actor"], "modeler")
            self.assertEqual(events["run_id"], "gui-orientation")

    def test_parts_replace_the_run_context_document_in_slice_part(self):
        """2026-10-10 6-axis trace: slice_part(parts=[...]) with no top-level doc_id was
        refused "not both", because the run's active doc_id was injected beside parts."""
        from mcp.server.fastmcp import FastMCP
        from services.mcp import tool_registry
        from services.mcp.print_tools import register_print_tools

        mcp = FastMCP("slice-parts-regression")
        with mock.patch.dict(tool_registry._CATALOG, {}, clear=True):
            register_print_tools(tool_registry.view(mcp, "cam"))
        entries = mcp._tool_manager._tools
        context = _context(entries, doc_id="doc-ambient", run_id="gui-print")
        binding = {b.name: b for b in build_host_tools(context)}["slice_part"]

        def call(args):
            result = dispatch_host_tool(binding, args, None, None)
            return json.loads(result) if isinstance(result, str) else result

        parts = [{"doc_id": "doc-a", "build_dir": [0, 1, 0]},
                 {"doc_id": "doc-b", "build_dir": [0, 1, 0]}]
        # No printer: target validation passes, then the tool asks for the printer.
        for args in ({"parts": parts}, {"parts": parts, "doc_id": None, "path": None}):
            result = call(args)
            self.assertEqual(result.get("reason"), "printer_required", result)
        mixed = call({"parts": parts, "doc_id": "doc-a"})
        self.assertFalse(mixed["ok"])
        self.assertIn("top-level doc_id or path", json.dumps(mixed))

    def test_context_doc_unless_keeps_the_run_document_when_the_argument_is_absent(self):
        from services.mcp.tool_registry import context_doc_unless

        seen = []

        @context_doc_unless("parts")
        def plate(doc_id=None, parts=None):
            seen.append(doc_id)
            return {"ok": True}

        binding = self._binding(name="plate", handler=plate, doc_id="doc-run")
        dispatch_host_tool(binding, {}, None, None)
        dispatch_host_tool(binding, {"parts": None}, None, None)
        dispatch_host_tool(binding, {"parts": [{"doc_id": "doc-x"}]}, None, None)
        self.assertEqual(seen, ["doc-run", "doc-run", None])

    def test_tool_error_propagates_unchanged(self):
        error = RuntimeError("CAD rejected feature")

        def dispatcher(_name, _args):
            raise error

        binding = self._binding(tool_dispatch=dispatcher)
        with self.assertRaises(RuntimeError) as caught:
            dispatch_host_tool(binding, {}, None, None)
        self.assertIs(caught.exception, error)

    def test_precancel_returns_without_dispatch(self):
        called = []
        binding = self._binding(tool_dispatch=lambda *_: called.append(True))
        result = dispatch_host_tool(binding, {}, None, lambda: True)
        self.assertEqual(
            result,
            {"ok": False, "status": "cancelled", "cancelled": True, "executed": False},
        )
        self.assertEqual(called, [])

    def test_guardrail_refusal_runs_before_tool_body(self):
        called = []
        binding = self._binding(
            "export_part",
            handler=lambda **_: called.append(True),
            actor="reviewer",
            pre_dispatch_guardrail=lambda tool, args, ledger, actor: (
                f"{actor} blocked {tool}" if tool == "export_part" else None
            ),
        )
        result = dispatch_host_tool(binding, {"doc_id": "doc-1"}, None, None)
        self.assertEqual(result["status"], "guardrail_blocked")
        self.assertFalse(result["executed"])
        self.assertEqual(called, [])

    def test_host_topology_refs_reach_live_resolver_for_add_and_edit(self):
        from services.agent.agent_turn import _bind_tool
        from services.mcp import core_bridge as cb

        if not cb.available():
            self.skipTest("libchudware not built/staged")

        new = _bind_tool("new_part")
        add = _bind_tool("add_features")
        query = _bind_tool("query_geometry")
        close = _bind_tool("close_part")

        def result(value):
            return json.loads(value) if isinstance(value, str) else value

        def discard(doc):
            closed = result(close(doc_id=doc, discard=True))
            self.assertTrue(closed.get("ok"), closed)

        def new_scratch(name):
            created = result(new(name=name, fresh=True))
            doc = created.get("doc_id")
            self.assertTrue(created.get("ok") and doc, created)
            # Run even if the test fails partway through, and only on documents
            # this test created. Otherwise a later turn's save sweep sees them.
            self.addCleanup(discard, doc)
            return doc

        def tree(doc):
            response = cb.call("doc_tree", {"doc_id": doc})
            return response.get("tree") or response

        def host(binding, doc, ops):
            return result(dispatch_host_tool(binding, {"doc_id": doc, "ops": ops}, None, None))

        rod = new_scratch("guard_sid_rod")
        seeded = result(add(doc_id=rod, ops=[{"op": "add_feature", "feature": {
            "type": "cylinder", "name": "Rod",
            "params": {"radius": 12.5, "height": 100},
            "centered": False, "at": [0, 0, 0]}}]))
        self.assertTrue(seeded["ok"], seeded)
        plate = new_scratch("guard_sid_plate")
        seeded = result(add(doc_id=plate, ops=[
            {"op": "add_feature", "feature": {"type": "box", "name": "Plate",
             "params": {"length": 40, "width": 30, "height": 20}}},
            {"op": "add_feature", "feature": {"type": "sketch", "name": "PlateSketch",
             "refs": {"plane": "plane.top"}, "params": {"entities": [
                 {"type": "circle", "center": [0, 0], "radius": 3}],
                 "constraints": []}}}], auto_define=False))
        self.assertTrue(seeded["ok"], seeded)
        sketch = next(f for f in tree(plate)["features"] if f.get("name") == "PlateSketch")
        sketch_id = sketch["id"]

        binding = self._binding("add_features", handler=add, doc_id=rod)
        with mock.patch("services.agent.guardrails.auto_checkpoint_enabled", return_value=False), \
                mock.patch("services.agent.config.get_tool_options", return_value=_NEUTRAL_TOOL_OPTIONS):
            edges = result(query(doc_id=rod, query={"of": "edge", "surf": "circle"}))["matches"]
            sid = max(edges, key=lambda edge: edge["centroid"][2])["sid"]
            added = host(binding, rod, [{"op": "add_feature", "feature": {
                "type": "chamfer", "name": "Break", "params": {"distance": 1},
                "refs": {"edges": [sid]}}}])
            self.assertTrue(added["ok"], added)
            cones = result(query(doc_id=rod, query={"of": "face", "surf": "conical"}))
            self.assertTrue(any(face["centroid"][2] > 50 for face in cones["matches"]), cones)
            rod_before_edit = tree(rod)["features"]

            face = result(query(doc_id=plate, query={"of": "face", "side": "top"}))["matches"][0]
            edited = host(binding, plate, [{"op": "edit_feature", "id": sketch_id,
                                            "refs": {"plane": face["sid"]}}])
            self.assertTrue(edited["ok"], edited)
            saved_sketch = next(f for f in tree(plate)["features"] if f["id"] == sketch_id)
            self.assertEqual(saved_sketch["refs"]["plane"]["sid"], face["sid"])
            self.assertEqual(saved_sketch["params"], sketch["params"])
            self.assertEqual(tree(rod)["features"], rod_before_edit)

            before = tree(plate)["features"]
            cases = (
                ({"op": "edit_feature", "id": "no_such_feature",
                  "refs": {"plane": face["sid"]}}, True),
                ({"op": "edit_feature", "id": sketch_id, "refs": {"plane": "F000000"}}, False),
                ({"op": "edit_feature", "id": sketch_id, "refs": {"plane": "FbadSID"}}, True),
                ({"op": "add_feature", "feature": {"type": "chamfer", "name": "Bad",
                  "params": {"distance": 1}, "refs": {"edges": ["E000000"]}}}, False),
                ({"op": "add_feature", "feature": {"type": "chamfer", "name": "BadQuery",
                  "params": {"distance": 1},
                  "refs": {"edges": [{"of": "edge", "sid": "NOPE_NOT_A_SID"}]}}}, False),
            )
            for op, blocked in cases:
                failed = host(binding, plate, [op])
                self.assertFalse(failed["ok"], failed)
                self.assertEqual(failed.get("status") == "guardrail_blocked", blocked, failed)
                self.assertEqual(tree(plate)["features"], before)

    def test_mission_governance_dispatch_precedes_global_handler(self):
        calls = []

        class Mission:
            _handlers: ClassVar[dict[str, object]] = {
                "delegate": object(),
                "mission_start": object(),
                "coordinate_progress": object(),
            }

            def resolve(self, name):
                if name == "delegate":
                    return lambda request, actor=None: (
                        calls.append(("delegate", request, actor))
                        or {"ok": True, "agent_id": "sub-1"}
                    )
                if name == "mission_start":
                    return lambda request, doc_id=None: (
                        calls.append(("mission_start", request, doc_id))
                        or {"ok": True, "run_id": "run-1"}
                    )
                if name == "coordinate_progress":
                    return lambda note, actor=None: (
                        calls.append(("coordinate_progress", note, actor))
                        or {"ok": True, "posted": True}
                    )
                return None

        mission = Mission()
        delegate = self._binding(
            "delegate",
            handler=lambda **_: self.fail("global handler ran"),
            mission_dispatch=mission,
            actor="manager",
        )
        started = self._binding(
            "mission_start",
            handler=lambda **_: self.fail("global handler ran"),
            mission_dispatch=mission,
            doc_id="doc-9",
        )
        progress = self._binding(
            "coordinate_progress",
            handler=lambda **_: self.fail("global handler ran"),
            mission_dispatch=mission,
            actor="manager",
        )

        self.assertTrue(
            dispatch_host_tool(delegate, {"request": "review"}, None, None)["ok"]
        )
        self.assertTrue(
            dispatch_host_tool(started, {"request": "build"}, None, None)["ok"]
        )
        self.assertTrue(
            dispatch_host_tool(progress, {"note": "safety"}, None, None)["ok"]
        )
        self.assertEqual(
            calls,
            [
                ("delegate", "review", "manager"),
                ("mission_start", "build", "doc-9"),
                ("coordinate_progress", "safety", "manager"),
            ],
        )

    def test_manufacturing_denial_keeps_existing_refusal_shape_and_order(self):
        binding = self._binding("cnc_execute", handler=_beta)
        binding.context["policy_registry"] = _Policy({"cnc_execute"})
        result = dispatch_host_tool(binding, {}, None, None)
        self.assertEqual(list(result), ["ok", "reason", "hint"])
        self.assertFalse(result["ok"])
        self.assertIn("existing CAD gate", result["reason"])

    def test_list_categories_and_enable_categories_use_live_catalog(self):
        listed = self._binding("list_tool_categories", handler=lambda: None)
        catalog = dispatch_host_tool(listed, {}, None, None)
        self.assertTrue(catalog["ok"])
        self.assertTrue(
            any(item["name"] == "measure" for item in catalog["categories"])
        )
        page = dispatch_host_tool(listed, {"category": "measure", "limit": 1}, None, None)
        self.assertTrue(page["ok"])
        self.assertEqual(len(page["tools"]), 1)
        self.assertTrue(page["tools"][0]["description"])
        self.assertIsNotNone(page["next_offset"])
        next_page = dispatch_host_tool(
            listed, {"category": "measure", "limit": 1, "offset": page["next_offset"]},
            None, None,
        )
        self.assertNotEqual(page["tools"][0]["name"], next_page["tools"][0]["name"])
        found = dispatch_host_tool(listed, {"query": "show_in_app"}, None, None)
        self.assertIn("show_in_app", [item["name"] for item in found["tools"]])
        self.assertFalse(dispatch_host_tool(listed, {"limit": "1"}, None, None)["ok"])

        enabled_calls = []
        enabled = self._binding(
            "enable_tools",
            handler=lambda categories: None,
            enable_categories=lambda categories: enabled_calls.append(categories),
        )
        result = dispatch_host_tool(enabled, {"categories": ["measure"]}, None, None)
        self.assertEqual(enabled_calls, [["measure"]])
        self.assertIn("measure_part", result["added"])

    def test_enable_tools_explains_device_access_after_successful_refresh(self):
        binding = self._binding(
            "enable_tools",
            handler=lambda categories: None,
            enable_categories=lambda tokens: {
                "ok": True, "enabled": tokens, "added": ["measure_part"], "unknown": [],
            },
        )
        result = dispatch_host_tool(binding, {"tools": ["measure_part"]}, None, None)
        self.assertEqual(result["added"], ["measure_part"])
        self.assertIn("read xd://<tool_name>", result["tool_access"])
        self.assertIn("write its JSON arguments", result["tool_access"])
        binding.context["enable_categories"] = lambda _tokens: {"ok": False, "error": "denied"}
        refused = dispatch_host_tool(binding, {"tools": ["measure_part"]}, None, None)
        self.assertEqual(refused, {"ok": False, "error": "denied"})

    def test_enable_tools_refuses_disabled_tool_before_refresh(self):
        calls = []
        binding = self._binding(
            "enable_tools", handler=lambda categories: None,
            enable_categories=lambda tokens: calls.append(tokens),
        )
        binding.context["policy_registry"] = _Policy({"cnc_execute"})
        result = dispatch_host_tool(binding, {"tools": ["cnc_execute"]}, None, None)
        self.assertFalse(result["ok"])
        self.assertIn("cnc_execute denied", result["reason"])
        self.assertEqual(calls, [])

    def test_cad_skill_mounts_the_tools_its_pack_teaches(self):
        """2026-10-08 cooling-water run: piping-design loaded, then six calls hit
        "No such tool: xd://piping_solve" because nothing mounted the pack's tools."""
        enabled_calls = []
        skill = self._binding(
            "cad_skill",
            handler=lambda name, path=None: f"[SKILL: {name} (builtin)]\n# body",
            enable_categories=lambda tools: enabled_calls.append(tools) or {
                "ok": True, "added": ["piping_solve", "duct_size"]},
        )
        result = dispatch_host_tool(skill, {"name": "piping-design"}, None, None)
        self.assertEqual(len(enabled_calls), 1)
        self.assertIn("piping_solve", enabled_calls[0])
        self.assertIn("vessel_size", enabled_calls[0])
        self.assertTrue(result.startswith("[SKILL: piping-design"))
        self.assertIn("now loaded: duct_size, piping_solve", result)
        self.assertIn("read xd://<tool_name>", result)
        self.assertIn("write its JSON arguments", result)

        # A file inside the pack is reference reading, not a load: nothing mounts.
        dispatch_host_tool(skill, {"name": "piping-design", "path": "x.md"}, None, None)
        self.assertEqual(len(enabled_calls), 1)


class _SubagentCall:
    """The chudcode_rpc HostToolContext fields dispatch reads."""

    cancelled = False

    def __init__(self, agent_id=None, parent_agent_id=None):
        self.agent_id = agent_id
        self.parent_agent_id = parent_agent_id

    def send_update(self, _payload):
        pass


class SubagentHostToolDispatchTests(unittest.TestCase):
    """Chudcode task sub-agents call the parent's bindings concurrently."""

    def _tool(self, name, handler, **context):
        binding = HostToolBinding(
            name=name,
            description=name,
            input_schema={"type": "object"},
            handler=handler,
            context={"policy_registry": _Policy(), **context},
        )
        return binding.to_host_tool(lambda **kw: kw)["execute"]

    def test_parallel_subagent_calls_overlap_and_records_name_each_caller(self):
        # Both bodies must be inside the tool at once: a dispatch lock around the
        # CAD call would serialize them and break the barrier.
        barrier = threading.Barrier(2, timeout=10)

        def build(size: int):
            if size > 1:  # the two sub-agent calls rendezvous; the last call does not
                barrier.wait()
            return {"ok": True, "size": size}

        records = []
        execute = self._tool("new_part", build, tool_calls=records, tool_call_counts={})
        results = {}

        def call(agent_id, size):
            results[agent_id] = execute({"size": size}, _SubagentCall(agent_id, "Main"))

        workers = [
            threading.Thread(target=call, args=("CubeBuilder", 10)),
            threading.Thread(target=call, args=("CylinderBuilder", 5)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        self.assertEqual(results["CubeBuilder"]["details"], {"ok": True, "size": 10})
        self.assertEqual(results["CylinderBuilder"]["details"], {"ok": True, "size": 5})
        self.assertEqual(
            sorted((r["agent_id"], r["parent_agent_id"], r["args"]["size"]) for r in records),
            [("CubeBuilder", "Main", 10), ("CylinderBuilder", "Main", 5)],
        )
        # The top-level agent's own calls stay unattributed.
        execute({"size": 1}, _SubagentCall())
        self.assertNotIn("agent_id", records[-1])

    def test_parallel_new_parts_keep_agent_current_docs_across_call_threads(self):
        from services.mcp import cad_common, cad_doc_tools

        class Registry:
            def __init__(self):
                self.tools = {}

            def tool(self, *args, **kwargs):
                def register(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return register

        registry = Registry()
        cad_doc_tools.register_cad_doc_tools(registry)
        docs = []
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)
        results, errors = {}, []

        def engine(method, params=None):
            with lock:
                if method == "doc_new":
                    doc = {
                        "doc_id": f"doc-cpp{len(docs) + 1:08x}",
                        "name": params["name"],
                        "origin_kind": "mcp",
                    }
                    docs.append(doc)
                    return dict(doc)
                if method == "doc_list":
                    return {"docs": list(docs)}
            raise AssertionError(f"unexpected engine call {method}")

        def inspect(doc_id=None):
            return {"ok": True, "doc_id": cad_common._doc(doc_id)}

        context = {"run_id": "isolated-turn", "doc_id": "turn-part"}
        create = self._tool("new_part", registry.tools["new_part"], **context)
        read = self._tool("get_part", inspect, **context)

        def make(agent):
            try:
                result = create({"name": agent, "fresh": True}, _SubagentCall(agent, "Main"))
                results[agent] = json.loads(result["details"]) if isinstance(
                    result["details"], str
                ) else result["details"]
                barrier.wait()
            except Exception as exc:
                errors.append(exc)

        def check(agent):
            try:
                result = read({}, _SubagentCall(agent, "Main"))["details"]
                self.assertEqual(result["doc_id"], results[agent]["doc_id"])
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(cad_common._CUR, "_slots", {None: {"doc_id": "process-doc"}}), \
                mock.patch.object(cad_common.cb, "call", side_effect=engine), \
                mock.patch.object(cad_common.cb, "lease_document"), \
                mock.patch.object(cad_common, "_PROCESS_DOCS", set()), \
                mock.patch.object(cad_common, "_TOUCHED_DOCS", {}), \
                mock.patch.object(cad_common, "_project_save", return_value=None), \
                mock.patch("services.agent.config.get_tool_options", return_value=_NEUTRAL_TOOL_OPTIONS):
            # The second tool calls use NEW threads: identity follows the agent, not
            # the RPC worker thread used for its previous call.
            for target in (make, check):
                workers = [threading.Thread(target=target, args=(agent,)) for agent in ("A", "B")]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=10)
                self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(errors, [])
            self.assertNotEqual(results["A"]["doc_id"], results["B"]["doc_id"])
            self.assertEqual(cad_common._CUR["doc_id"], "process-doc")

    def test_subagent_document_injection_uses_own_part_then_turn_fallback(self):
        from services.agent.omp_tools import _document_args
        from services.mcp import cad_common

        def inspect(doc_id=None):
            return {"doc_id": doc_id}

        with mock.patch.object(cad_common._CUR, "_slots", {None: {"doc_id": "process-doc"}}):
            with cad_common.agent_doc_context("Builder", scope="turn"):
                context = {"doc_id": "turn-doc"}
                self.assertEqual(
                    _document_args(inspect, {}, context, agent_id="Builder"),
                    {"doc_id": "turn-doc"},
                )
                cad_common._CUR["doc_id"] = "own-doc"
                self.assertEqual(
                    _document_args(inspect, {}, context, agent_id="Builder"),
                    {"doc_id": "own-doc"},
                )
                self.assertEqual(
                    _document_args(inspect, {"doc_id": "explicit-doc"}, context, agent_id="Builder"),
                    {"doc_id": "explicit-doc"},
                )
                # Root calls preserve the existing explicit turn-doc injection.
                self.assertEqual(_document_args(inspect, {}, context), {"doc_id": "turn-doc"})

    def test_agent_context_restores_after_tool_exception(self):
        from services.mcp import cad_common

        previous = cad_common.cb.CURRENT_AGENT.get()

        def fail():
            self.assertEqual(cad_common.cb.CURRENT_AGENT.get(), ("turn", "Builder"))
            raise RuntimeError("tool failed")

        execute = self._tool("new_part", fail, run_id="turn")
        with self.assertRaisesRegex(RuntimeError, "tool failed"):
            execute({}, _SubagentCall("Builder", "Main"))
        self.assertEqual(cad_common.cb.CURRENT_AGENT.get(), previous)

    def test_call_cap_holds_across_concurrent_subagent_calls(self):
        release = threading.Event()
        ran = []

        def build():
            ran.append(1)
            release.wait(timeout=10)
            return {"ok": True}

        counts = {}
        execute = self._tool("new_part", build, tool_call_counts=counts, tool_calls=[])
        options = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": 2,
            "param_defaults": {},
            "prompt_note": "",
        }
        refused = queue.Queue()
        outcomes = []

        def call(index):
            out = execute({}, _SubagentCall(f"Builder{index}", "Main"))
            outcomes.append(out)
            if out["details"].get("ok") is False:
                refused.put(out)

        with mock.patch("services.agent.config.get_tool_options", return_value=options):
            workers = [threading.Thread(target=call, args=(i,)) for i in range(6)]
            for worker in workers:
                worker.start()
            # Four calls past the cap return at once while two bodies still hold.
            for _ in range(4):
                refused.get(timeout=10)
            release.set()
            for worker in workers:
                worker.join(timeout=20)

        self.assertEqual(len(ran), 2)
        self.assertEqual(counts, {"new_part": 2})
        self.assertEqual(
            sum(1 for out in outcomes if out["details"].get("ok") is True), 2
        )

    def test_read_only_modes_refuse_subagent_mutations_at_dispatch(self):
        ran = []

        def add_features(ops=None):
            ran.append(ops)
            return {"ok": True}

        for mode, plan_only in (("ask", None), ("plan", None), ("agent", True)):
            execute = self._tool(
                "add_features", add_features, mode=mode, plan_only=plan_only
            )
            out = execute({"ops": []}, _SubagentCall("Builder", "Main"))
            self.assertEqual(out["details"]["status"], "mode_refused", mode)
            self.assertIs(out["details"]["executed"], False)
        self.assertEqual(ran, [])

        # A read-only tool still runs under Plan; a mutator runs under Agent.
        measured = self._tool("measure_part", lambda: {"ok": True}, mode="plan")
        self.assertTrue(measured({}, _SubagentCall("Builder", "Main"))["details"]["ok"])
        agent = self._tool("add_features", add_features, mode="agent")
        self.assertTrue(agent({"ops": []}, _SubagentCall("Builder", "Main"))["details"]["ok"])
        self.assertEqual(ran, [[]])


def _png(path, size=(64, 48), noise=False):
    """Write a real PNG (solid, or incompressible noise) and return its path."""
    import random

    from PIL import Image

    if noise:
        pixels = random.Random(7).randbytes(size[0] * size[1] * 3)
        image = Image.frombytes("RGB", size, pixels)
    else:
        image = Image.new("RGB", size, (40, 90, 160))
    image.save(path, format="PNG")
    return str(path)


def _decoded(block):
    import base64
    import io

    from PIL import Image

    raw = base64.b64decode(block["data"], validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        return raw, image.format, image.size


# Neutral per-tool options: the dispatch must not read the developer's Settings.
_NEUTRAL_TOOL_OPTIONS = {
    "enabled": True,
    "timeout_s": None,
    "max_calls_per_turn": None,
    "param_defaults": {},
    "prompt_note": "",
}


# The official client may be absent from a bare service venv.
_HAS_OMP_RPC = importlib.util.find_spec("omp_rpc") is not None


@unittest.skipUnless(_HAS_OMP_RPC, "omp_rpc client not installed in this venv")
class HostToolImageDeliveryTests(unittest.TestCase):
    """Render tools return FastMCP ``[Image, caption]``. c205/c213/c217: the RPC
    ``host_tool_result`` frame carried only ``"<mcp.server.fastmcp.utilities.types.
    Image object at 0x…>"`` text, so the model never saw a single render. Every
    render-producing tool family must reach the frame as an image content block
    with the caption kept as text, whoever called it (top level, a task sub-agent,
    or code inside eval)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        patches = (
            mock.patch("services.agent.config.get_tool_options",
                       return_value=dict(_NEUTRAL_TOOL_OPTIONS)),
            # Inline image bytes are written to the render cache: keep it in tmp.
            mock.patch("services.paths.CACHE", self.tmp / "cache"),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _frame(self, name, handler, **frame):
        """Run *handler* as host tool *name* through the real chudcode_rpc client and
        return the ``host_tool_result`` frame it writes back to Chudcode."""
        from omp_rpc import RpcClient, host_tool

        binding = HostToolBinding(
            name=name,
            description=name,
            input_schema={"type": "object"},
            handler=handler,
            context={"policy_registry": _Policy(), "tool_calls": []},
        )
        client = RpcClient(custom_tools=[binding.to_host_tool(host_tool)])
        sent = queue.Queue()
        with mock.patch.object(client, "_send_notification", sent.put):
            client._handle_host_tool_call({
                "type": "host_tool_call",
                "id": "req-1",
                "toolCallId": frame.pop("toolCallId", "toolu_render"),
                "toolName": name,
                "arguments": {},
                **frame,
            })
            out = sent.get(timeout=30)
        self.assertEqual(out["type"], "host_tool_result")
        self.assertNotIn("isError", out, out)
        return out, binding.context["tool_calls"]

    def test_text_guidance_rpc_frame_keeps_details_for_eval(self):
        for name in ("cad_guide", "cad_skill"):
            with self.subTest(name=name):
                text = "# Guide\nKeep the shaft Ø6 and the case centered — 🔧."
                frame, records = self._frame(
                    name, lambda: text, toolCallId=f"js-{name}-text"
                )
                self.assertEqual(frame["result"]["details"], text)
                self.assertEqual(
                    frame["result"]["content"], [{"type": "text", "text": text}]
                )
                self.assertEqual(records[-1]["result"], text)

    def _assert_image_frame(self, frame, caption, size):
        content = frame["result"]["content"]
        self.assertEqual(content[0]["type"], "text")
        text = content[0]["text"]
        self.assertIn(caption, text)
        self.assertNotIn("Image object at", text)
        images = [block for block in content if block.get("type") == "image"]
        self.assertEqual(len(images), 1, content)
        self.assertEqual(images[0]["mimeType"], "image/png")
        raw, fmt, decoded_size = _decoded(images[0])
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((fmt, decoded_size), ("PNG", size))
        # The structured copy keeps the path reference, never the pixels.
        self.assertNotIn(images[0]["data"], json.dumps(frame["result"]["details"]))
        return text

    def test_oversized_image_caption_preserves_image_and_structured_result(self):
        from mcp.server.fastmcp import Image

        png = _png(self.tmp / "render.png")
        caption = "synthetic caption 🔧 " * 6000
        out = host_tool_result([Image(path=png), caption])
        self.assertLessEqual(len(out["content"][0]["text"].encode("utf-8")), 46_000)
        self.assertEqual(json.loads(out["content"][0]["text"]), out["details"])
        self.assertIsInstance(out["details"], dict)
        self.assertEqual(out["details"]["_truncated"]["original_type"], "object")
        images = [block for block in out["content"] if block.get("type") == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(_decoded(images[0])[2], (64, 48))

    def test_every_render_tool_family_reaches_the_frame_as_an_image_block(self):
        from mcp.server.fastmcp import Image

        png = _png(self.tmp / "render.png")
        review_json = json.dumps({"ok": True, "views": [{"view_id": "v1"}]})
        families = {
            # _captioned_render / _with_save_note pairs.
            "render_part": lambda: [Image(path=png), "iso view of 'bracket'"],
            "contact_sheet": lambda: [Image(path=png), "Contact sheet, 4 tiles"],
            "render_views": lambda: [Image(path=png), "render_views grid (2 tiles)"],
            "render_study_setup": lambda: [Image(path=png), "fixtures blue, load red"],
            # Close-up tools return [Image(path=out), caption].
            "inspect_feature": lambda: [Image(path=png), "feature F3 close-up"],
            "inspect_part": lambda: [Image(path=png), "target face close-up"],
            "look_around": lambda: [Image(path=png), "4 views around F9"],
            "measure": lambda: [Image(path=png), "distance 12.5 mm"],
            # review_drawing pairs the sheet with its JSON diagnostics.
            "review_drawing": lambda: [Image(path=png), review_json],
            # _captioned_render's caption-less fallback is a bare Image.
            "sketch_render": lambda: Image(path=png),
        }
        captions = {
            "review_drawing": "view_id",
            "sketch_render": png,
        }
        for name, handler in families.items():
            with self.subTest(tool=name):
                frame, records = self._frame(name, handler)
                caption = captions.get(name) or handler()[1]
                text = self._assert_image_frame(frame, caption, (64, 48))
                self.assertIn(png, text)
                # Run records (artifact events, the GUI feed) keep the path reference.
                recorded = records[-1]["result"]
                refs = recorded if isinstance(recorded, list) else [recorded]
                self.assertIn({"kind": "image", "path": png, "mime": "image/png"}, refs)

    def test_image_reaches_subagent_eval_and_arg_note_wrapped_calls(self):
        from mcp.server.fastmcp import Image

        png = _png(self.tmp / "render.png")
        pair = lambda: [Image(path=png), "iso view of 'bracket'"]
        # A task sub-agent's call carries its registry ids on the frame.
        frame, _ = self._frame(
            "render_part", pair, agentId="Builder", parentAgentId="Main"
        )
        self._assert_image_frame(frame, "iso view of 'bracket'", (64, 48))
        # eval's tool bridge calls the same host tool under a js-* call id.
        frame, _ = self._frame(
            "render_part", pair, toolCallId="js-render_part-7f1c"
        )
        self._assert_image_frame(frame, "iso view of 'bracket'", (64, 48))
        # _attach_arg_notes wraps a list result in {"ok", "result", ...}.
        wrapped = lambda: {
            "ok": True,
            "result": [Image(path=png), "iso view of 'bracket'"],
            "arg_interpretation": "interpreted 'part' as 'doc_id'",
        }
        frame, _ = self._frame("render_part", wrapped)
        text = self._assert_image_frame(frame, "iso view of 'bracket'", (64, 48))
        self.assertIn("arg_interpretation", text)

    def test_inline_image_bytes_reach_the_frame_as_an_image_block(self):
        from mcp.server.fastmcp import Image

        data = Path(_png(self.tmp / "inline.png", size=(80, 60))).read_bytes()
        frame, records = self._frame(
            "render_part", lambda: [Image(data=data, format="png"), "inline render"]
        )
        self._assert_image_frame(frame, "inline render", (80, 60))
        ref = records[-1]["result"][0]
        self.assertEqual(ref["kind"], "image")
        self.assertTrue(ref["path"].startswith(str(self.tmp / "cache" / "renders")))
        self.assertEqual(Path(ref["path"]).read_bytes(), data)

    def test_oversized_image_is_downscaled_to_the_model_edge_not_dropped(self):
        from mcp.server.fastmcp import Image

        from services.agent.tool_images import MAX_EDGE_PX

        sheet = _png(self.tmp / "sheet.png", size=(4000, 3000))
        frame, _ = self._frame(
            "review_drawing", lambda: [Image(path=sheet), '{"ok": true}']
        )
        text = self._assert_image_frame(
            frame, "original 4000x3000", (MAX_EDGE_PX, 1176)
        )
        self.assertIn(sheet, text)

    def test_image_over_the_byte_limit_is_reencoded_under_it(self):
        from mcp.server.fastmcp import Image

        from services.agent.tool_images import MAX_BYTES

        noisy = _png(self.tmp / "noisy.png", size=(1500, 1400), noise=True)
        self.assertGreater(Path(noisy).stat().st_size, MAX_BYTES)
        frame, _ = self._frame("render_part", lambda: [Image(path=noisy), "noisy"])
        images = [b for b in frame["result"]["content"] if b.get("type") == "image"]
        self.assertEqual(len(images), 1)
        raw, fmt, size = _decoded(images[0])
        self.assertEqual(fmt, "PNG")
        self.assertLessEqual(len(raw), MAX_BYTES)
        self.assertLess(size[0], 1500)
        self.assertAlmostEqual(size[0] / size[1], 1500 / 1400, places=2)

    def test_missing_image_file_is_named_in_the_text_not_silently_dropped(self):
        from mcp.server.fastmcp import Image

        gone = str(self.tmp / "gone.png")
        frame, _ = self._frame("render_part", lambda: [Image(path=gone), "iso"])
        content = frame["result"]["content"]
        self.assertEqual([block["type"] for block in content], ["text"])
        self.assertIn(f"[image not attached: {gone} is unreadable", content[0]["text"])

    def test_eval_bridged_render_reads_as_one_mapping_and_keeps_the_image(self):
        """c250: eval code did ``r.items()`` on render_part's ``[image, caption]``."""
        from mcp.server.fastmcp import Image

        png = _png(self.tmp / "render.png")
        saved = str(self.tmp / "out dir" / "review.png")
        caption = f"iso view of 'bracket' saved: {saved} (1234 B, sha256 0123456789ab)"
        frame, records = self._frame(
            "render_part",
            lambda: [Image(path=png), caption],
            toolCallId="js-render_part-7f1c",
        )
        self._assert_image_frame(frame, "iso view of 'bracket'", (64, 48))
        details = frame["result"]["details"]
        # eval code reads either details or json.loads(text): both are the mapping.
        self.assertEqual(json.loads(frame["result"]["content"][0]["text"]), details)
        self.assertEqual(
            details,
            {
                "ok": True,
                "caption": caption,
                "images": [{"kind": "image", "path": png, "mime": "image/png",
                            "width": 64, "height": 48}],
                "saved_path": saved,
            },
        )
        # Run records (artifact events, the GUI feed) keep the tool's own return.
        self.assertEqual(records[-1]["result"][1], caption)

        # review_drawing's JSON diagnostics become keys of the same mapping.
        frame, _ = self._frame(
            "review_drawing",
            lambda: [Image(path=png), '{"ok": false, "views": [{"view_id": "v1"}]}'],
            toolCallId="js-review_drawing-1",
        )
        details = frame["result"]["details"]
        self.assertIs(details["ok"], False)
        self.assertEqual(details["views"], [{"view_id": "v1"}])
        self.assertEqual((details["caption"], details["images"][0]["path"]), ("", png))

        # An arg-note wrapper around the pair flattens into the mapping.
        frame, _ = self._frame(
            "render_part",
            lambda: {"ok": True, "result": [Image(path=png), "iso"],
                     "arg_interpretation": "interpreted 'part' as 'doc_id'"},
            toolCallId="js-render_part-2",
        )
        details = frame["result"]["details"]
        self.assertEqual(details["caption"], "iso")
        self.assertEqual(details["arg_interpretation"], "interpreted 'part' as 'doc_id'")
        self.assertNotIn("result", details)

        # An image that cannot attach is named in the mapping; the text stays JSON.
        gone = str(self.tmp / "gone.png")
        frame, _ = self._frame(
            "render_part", lambda: Image(path=gone), toolCallId="js-render_part-3"
        )
        details = json.loads(frame["result"]["content"][0]["text"])
        self.assertEqual((details["caption"], details["images"]), ("", [
            {"kind": "image", "path": gone, "mime": "image/png"}
        ]))
        self.assertIn(f"[image not attached: {gone} is unreadable", details["image_notes"][0])


class _CallContext:
    """The chudcode_rpc HostToolContext fields ``execute`` reads."""

    cancelled = False
    agent_id = None
    parent_agent_id = None

    def __init__(self, tool_call_id):
        self.tool_call_id = tool_call_id


class HostToolResultEdgeTests(unittest.TestCase):
    """c211-c230: CAD results reached eval as unparseable text and every ok:false
    looked like a success to structured consumers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch("services.paths.ROOT", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _execute(self, handler, params=None, *, call_id="toolu_1", name="probe", **context):
        binding = HostToolBinding(
            name=name,
            description=name,
            input_schema={"type": "object"},
            handler=handler,
            context={"policy_registry": _Policy(), "session_id": "c1", **context},
        )
        tool = binding.to_host_tool(lambda **kw: kw)
        return tool["execute"](params or {}, _CallContext(call_id)), binding.context

    def _recover_text(self, text):
        prefix, notice = text.rsplit("\n\n[TRUNCATED: ", 1)
        self.assertIn("load the complete JSON string", notice)
        path, _ = json.JSONDecoder().raw_decode(notice.split("full_result=", 1)[1])
        with open(path, encoding="utf-8") as handle:
            return prefix, json.load(handle)

    def test_plain_text_details_preserve_unicode_and_non_json_without_fake_success(self):
        for text in ("", "CAD guide text", "轴 Ø6 — 🔧\nKeep this line.", "[not json"):
            with self.subTest(text=text):
                out = host_tool_result(text)
                self.assertEqual(out["content"], [{"type": "text", "text": text}])
                self.assertEqual(out["details"], text)

    def test_registered_result_contract_survives_projection_and_execution(self):
        from mcp.server.fastmcp import FastMCP
        from services.mcp import tool_registry

        mcp = FastMCP("result-contract-regression")
        with mock.patch.dict(tool_registry._CATALOG, {}, clear=True):
            cad = tool_registry.view(mcp, "cad")

            @cad.tool()
            @tool_registry.result_contract("Inspect nested operation results.")
            def contract_probe(kind: str = "success") -> dict:
                """Inspect a result.

                A longer description that may be trimmed.
                """
                if kind == "failure":
                    return {"ok": False, "error": "operation failed"}
                if kind == "large":
                    return {"ok": True, "values": list(range(20000))}
                return {"ok": True, "value": 7}

        entries = mcp._tool_manager._tools
        context = _context(entries, allowed_tools=list(entries))
        binding = next(b for b in build_host_tools(context) if b.name == "contract_probe")
        self.assertEqual(binding.handler._cad_result_contract, "Inspect nested operation results.")
        self.assertIn(binding.handler._cad_result_contract, binding.description)
        tool = binding.to_host_tool(lambda **kwargs: kwargs)
        execute = tool["execute"]
        call = _CallContext("js-contract-probe")
        self.assertEqual(execute({}, call)["details"], {"ok": True, "value": 7})
        if _HAS_OMP_RPC:
            from omp_rpc import host_tool

            sdk_tool = binding.to_host_tool(host_tool)
            self.assertIn(binding.handler._cad_result_contract, sdk_tool.description)
            self.assertEqual(sdk_tool.execute({}, call)["details"], {"ok": True, "value": 7})
        failure = execute({"kind": "failure"}, call)["details"]
        self.assertEqual(failure, {"ok": False, "error": "operation failed", "code": "error"})
        large = execute({"kind": "large"}, call)["details"]
        self.assertTrue(large["ok"])
        with open(large["_truncated"]["full_result"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"ok": True, "values": list(range(20000))})
        with mock.patch.object(
            tool_registry, "_denial", return_value=("not_permitted", "denied by test policy"),
        ):
            refusal = execute({}, call)["details"]
        self.assertFalse(refusal["ok"])
        self.assertIn("reason", refusal)

    def test_real_doc_producer_contracts_survive_projection(self):
        from mcp.server.fastmcp import FastMCP
        from services.mcp import tool_registry
        from services.mcp.cad_doc_tools import register_cad_doc_tools

        mcp = FastMCP("doc-result-contract-regression")
        with mock.patch.dict(tool_registry._CATALOG, {}, clear=True):
            register_cad_doc_tools(tool_registry.view(mcp, "cad"))
        names = ("cad_guide", "cad_skill", "get_part", "add_features", "get_job", "wait_job")
        all_entries = mcp._tool_manager._tools
        entries = {name: all_entries[name] for name in names}
        bindings = {b.name: b for b in build_host_tools(
            _context(entries, allowed_tools=list(entries))
        )}
        for name in names:
            with self.subTest(name=name):
                producer = entries[name].fn
                self.assertTrue(producer._cad_result_contract)
                self.assertIs(bindings[name].handler, producer)
                self.assertIn(producer._cad_result_contract, bindings[name].description)

        guide = bindings["cad_guide"].to_host_tool(lambda **kwargs: kwargs)
        result = guide["execute"]({"topic": "fillet"}, _CallContext("js-cad_guide"))
        self.assertIsInstance(result["details"], str)
        self.assertTrue(result["details"])

    def test_real_guides_and_skills_reach_eval_as_text_details(self):
        from services.agent.skills import skill_reference_text
        from services.mcp.cad_guide import cad_guide_text

        for name, handler, params in (
            ("cad_guide", cad_guide_text, {}),
            ("cad_guide", cad_guide_text, {"topic": "fillet"}),
            ("cad_skill", skill_reference_text, {"name": "edit-part"}),
        ):
            with self.subTest(name=name, params=params):
                expected = handler(**params)
                self.assertTrue(expected)
                out, _ = self._execute(
                    handler, params, name=name, call_id=f"js-{name}-text"
                )
                self.assertLessEqual(len(out["content"][0]["text"].encode("utf-8")), 46_000)
                value = out["details"]
                self.assertIsInstance(value, str)
                if "\n\n[TRUNCATED: " in value:
                    prefix, recovered = self._recover_text(value)
                    self.assertTrue(expected.startswith(prefix))
                    self.assertEqual(recovered, expected)
                else:
                    self.assertEqual(value, expected)
                    self.assertEqual(out["content"][0]["text"], expected)

    def test_plain_text_budget_boundary_keeps_the_same_type(self):
        below = "x" * 46_000
        above = "x" * 46_001
        for source in (below, above):
            out, _ = self._execute(lambda: source, call_id="js-probe-boundary")
            self.assertIsInstance(out["details"], str)
            self.assertEqual(out["details"], out["content"][0]["text"])
            self.assertLessEqual(len(out["details"].encode("utf-8")), 46_000)
            if source is below:
                self.assertEqual(out["details"], source)
            else:
                prefix, recovered = self._recover_text(out["details"])
                self.assertTrue(source.startswith(prefix))
                self.assertEqual(recovered, source)

    def test_oversized_plain_unicode_text_is_bounded_and_spills_the_complete_string(self):
        source = "🔧轴Ø\n" * 9000
        # Character count fits the cap; UTF-8 byte count does not.
        self.assertLess(len(source), 46_000)
        self.assertGreater(len(source.encode("utf-8")), 46_000)
        out, _ = self._execute(
            lambda: source, name="cad_skill", call_id="js-cad_skill-large"
        )
        text = out["content"][0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 46_000)
        self.assertEqual(text, out["details"])
        prefix, recovered = self._recover_text(text)
        self.assertTrue(source.startswith(prefix))
        self.assertEqual(recovered, source)
        self.assertIn(f"original text is {len(source.encode('utf-8'))} UTF-8 bytes", text)

    def test_plain_text_budget_reserves_the_unicode_skill_prefix(self):
        source = "Guide line 🔧轴\n" * 8000
        prefix = "[SKILL: probe]\n" + "轴🔧" * 2000 + "\n"
        with mock.patch("services.agent.omp_tools._skill_prefix", return_value=prefix):
            out, _ = self._execute(lambda: source, name="cad_guide")
        text = out["content"][0]["text"]
        self.assertTrue(text.startswith(prefix))
        self.assertLessEqual(len(text.encode("utf-8")), 46_000)
        body = out["details"]
        self.assertIsInstance(body, str)
        self.assertEqual(text, prefix + "\n\n[cad_guide result]\n" + body)
        start, recovered = self._recover_text(body)
        self.assertTrue(source.startswith(start))
        self.assertEqual(recovered, source)

    def test_oversized_unicode_prefix_remains_below_runtime_spill_threshold(self):
        source = "result 🔧\n" * 9000
        prefix = "🔧" * 12_000
        with mock.patch("services.agent.omp_tools._skill_prefix", return_value=prefix):
            out, _ = self._execute(lambda: source, name="probe")
        text = out["content"][0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 46_000)
        self.assertIn("Skill prefix truncated to fit the tool-result budget", text)
        self.assertIn("\n\n[probe result]\n", text)
        _, recovered = self._recover_text(out["details"])
        self.assertEqual(recovered, source)

    def test_plain_text_spill_failure_stays_bounded_without_claiming_a_saved_file(self):
        source = "大型指南 🔧\n" * 12000
        with mock.patch(
            "services.agent.omp_tools._spill_full_result",
            side_effect=OSError("cache is read-only"),
        ):
            out, _ = self._execute(lambda: source, call_id="js-cad_guide-no-spill")
        self.assertLessEqual(len(out["content"][0]["text"].encode("utf-8")), 46_000)
        body = out["details"]
        self.assertIsInstance(body, str)
        self.assertEqual(out["content"][0]["text"], body)
        self.assertIn("[TRUNCATED:", body)
        self.assertIn("full text not saved; narrow the call", body)
        self.assertNotIn("full_result=", body)

    def test_host_envelope_does_not_turn_handler_exceptions_into_success(self):
        error = RuntimeError("CAD guide unavailable")

        def fail():
            raise error

        with self.assertRaises(RuntimeError) as caught:
            self._execute(fail, name="cad_guide", call_id="js-cad_guide-error")
        self.assertIs(caught.exception, error)

    def test_json_text_result_carries_structured_details_and_its_failure(self):
        records = []
        out, _ = self._execute(
            lambda: json.dumps({"ok": False, "error": "fillet too large"}, indent=2),
            tool_calls=records,
        )
        self.assertEqual(out["details"], {"ok": False, "error": "fillet too large"})
        self.assertEqual(json.loads(out["content"][0]["text"])["error"], "fillet too large")
        self.assertIs(records[-1]["ok"], False)

    def test_oversized_failure_keeps_failure_and_nested_tree_shape(self):
        full = {
            "ok": False,
            "error": "synthetic failure",
            "tree": {"features": [{"name": f"F{i}", "note": "x" * 40} for i in range(5000)]},
        }
        out, _ = self._execute(lambda: full)
        body = out["details"]
        self.assertEqual(json.loads(out["content"][0]["text"]), body)
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error"], full["error"])
        self.assertEqual(body["tree"]["features"][0], full["tree"]["features"][0])
        self.assertNotIn("features", body)
        self.assertEqual(body["_truncated"]["original_type"], "object")
        with open(body["_truncated"]["full_result"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), full)

    def test_oversized_result_is_valid_json_under_budget_with_the_full_value_saved(self):
        faces = [{"sid": f"F{i:05d}", "area": i * 1.5, "side": "top"} for i in range(6000)]
        full = {"ok": True, "doc_id": "doc-9", "summary": "6000 faces", "faces": faces}
        out, _ = self._execute(lambda: json.dumps(full, indent=2))
        text = out["content"][0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 46_000)
        body = json.loads(text)
        self.assertEqual(body, out["details"])
        # What a caller branches on survives; the long list keeps its head.
        self.assertEqual((body["ok"], body["doc_id"], body["summary"]), (True, "doc-9", "6000 faces"))
        self.assertEqual(body["faces"][0], faces[0])
        marker = body["_truncated"]
        self.assertEqual(marker["shortened"]["$.faces"], 6000)
        with open(marker["full_result"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), full)

    def test_shortening_cuts_the_long_list_not_each_records_small_fields(self):
        """A partial 4x4 transform is wrong data, not less data (live: the first
        uniform cut halved every component's 16 numbers)."""
        comps = [{"id": f"k{i}", "name": f"Part {i}", "transform": [float(i)] * 16,
                  "source": {"path": f"parts/p{i}.step+"}} for i in range(900)]
        out, _ = self._execute(lambda: {"ok": True, "components": comps})
        body = json.loads(out["content"][0]["text"])
        self.assertLess(len(body["components"]), 900)
        self.assertTrue(all(len(c["transform"]) == 16 for c in body["components"]))
        self.assertEqual(body["_truncated"]["shortened"], {"$.components": 900})

    def test_oversized_top_level_list_stays_parseable(self):
        rows = [{"i": i, "name": "x" * 40} for i in range(5000)]
        out, _ = self._execute(lambda: rows)
        body = json.loads(out["content"][0]["text"])
        self.assertEqual(body["items"][0], rows[0])
        self.assertEqual(body["_truncated"]["original_type"], "array")
        with open(body["_truncated"]["full_result"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), rows)

    def test_a_cut_string_carries_a_visible_marker_and_eval_gets_the_full_result(self):
        """c250: eval read a 100+ KB file's text, the cap halved it to a bare prefix,
        and json.loads failed mid-string with nothing to say the text was cut."""
        source = json.dumps({"rows": [{"i": i, "name": f"r{i}"} for i in range(9000)]})
        full = {"ok": True, "path": "result.json", "text": source}
        out, _ = self._execute(lambda: full, call_id="js-write-5e7a")
        text = out["content"][0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 46_000)
        body = json.loads(text)
        self.assertEqual(body, out["details"])
        start, marker = body["text"].split(" …[cut: ", 1)
        self.assertTrue(source.startswith(start))
        self.assertEqual(
            marker,
            f"kept {len(start)} of {len(source)} characters; "
            "full text in _truncated.full_result]",
        )
        with open(body["_truncated"]["full_result"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), full)

    def test_eval_bridged_call_defers_first_touch_skill_body_to_a_direct_call(self):
        delivered = set()

        def activate(_text, *, session_id=None, **_kw):
            if session_id in delivered:
                return []
            delivered.add(session_id)
            return ["[SKILL: probe]\nbody"]

        handler = lambda: {"ok": True, "n": 1}
        with mock.patch("services.agent.skills.activate_for_prompt", side_effect=activate):
            from_eval, _ = self._execute(handler, call_id="js-write-0b1c")
            direct, _ = self._execute(handler, call_id="toolu_2")
            again, _ = self._execute(handler, call_id="toolu_3")
        self.assertEqual(json.loads(from_eval["content"][0]["text"]), {"ok": True, "n": 1})
        self.assertTrue(direct["content"][0]["text"].startswith("[SKILL: probe]"))
        self.assertEqual(json.loads(again["content"][0]["text"]), {"ok": True, "n": 1})

    def test_unknown_keyword_is_a_teaching_error_that_charges_nothing(self):
        """Live: plm_save(message=…) surfaced as "register_plm_tools.<locals>.
        plm_save() got an unexpected keyword argument 'message'"."""
        calls = []

        def plm_save(path: str | None = None, doc_id: str | None = None):
            calls.append(path)
            return {"ok": True}

        counts = {}
        with mock.patch(
            "services.agent.config.effective_tool_call_cap", return_value=1
        ):
            result = dispatch_host_tool(
                HostToolBinding(
                    name="plm_save",
                    description="plm_save",
                    input_schema={"type": "object"},
                    handler=plm_save,
                    context={"policy_registry": _Policy(), "tool_call_counts": counts},
                ),
                {"path": "/p", "zzz_unrelated_flag": 7},
                None,
                None,
            )
        self.assertIs(result["ok"], False)
        self.assertIn("zzz_unrelated_flag", json.dumps(result))
        self.assertEqual(calls, [])
        self.assertEqual(counts, {})

    def test_synonym_argument_is_mapped_and_the_mapping_reported(self):
        seen = {}

        def measure(doc_id: str | None = None, density_g_cm3: float | None = None):
            seen["density"] = density_g_cm3
            return {"ok": True}

        result = dispatch_host_tool(
            HostToolBinding(
                name="measure_part",
                description="measure_part",
                input_schema={"type": "object"},
                handler=measure,
                context={"policy_registry": _Policy()},
            ),
            {"density": 1.24},
            None,
            None,
        )
        self.assertEqual(seen["density"], 1.24)
        self.assertIn("density_g_cm3", result["arg_interpretation"])

    def test_host_cancel_is_hidden_from_the_schema_and_never_model_supplied(self):
        seen = {}

        def wait_job(job_id: str, timeout_s: float = 1.0, cancel=None):
            seen["cancel"] = cancel
            return {"ok": True, "stopped": cancel()}

        entries = {"wait_job": _entry(wait_job, "wait", {
            "type": "object",
            "properties": {"job_id": {"type": "string"}, "timeout_s": {"type": "number"},
                           "cancel": {"default": None, "title": "Cancel"}},
            "required": ["job_id"],
        })}
        [binding] = [b for b in build_host_tools(_context(entries)) if b.name == "wait_job"]
        self.assertNotIn("cancel", binding.input_schema["properties"])
        probes = iter((False, True))  # dispatch's own pre-check, then the tool's
        stop = lambda: next(probes)
        result = dispatch_host_tool(binding, {"job_id": "job-1", "cancel": False}, None, stop)
        self.assertIs(seen["cancel"], stop)
        self.assertIs(result["stopped"], True)

    def test_a_pure_wait_is_woken_by_the_runs_unread_user_message(self):
        seen = []

        def wait_job(job_id: str, timeout_s: float = 1.0, cancel=None, wake=None):
            seen.append(wake() if wake else "no-probe")
            return {"ok": True}

        entries = {"wait_job": _entry(wait_job, "wait", {
            "type": "object",
            "properties": {"job_id": {"type": "string"},
                           "wake": {"default": None, "title": "Wake"}},
            "required": ["job_id"],
        })}
        context = _context(entries)
        [binding] = [b for b in build_host_tools(context) if b.name == "wait_job"]
        self.assertNotIn("wake", binding.input_schema["properties"])
        dispatch_host_tool(binding, {"job_id": "j"}, None, None)   # outside a live turn
        unread = []
        binding.context["user_steer_pending"] = lambda: bool(unread)
        dispatch_host_tool(binding, {"job_id": "j", "wake": "model junk"}, None, None)
        unread.append("add a roof")
        dispatch_host_tool(binding, {"job_id": "j"}, None, None)
        self.assertEqual(seen, ["no-probe", None, "user_message"])


class HelperRuntimeScopeTests(unittest.TestCase):
    def test_model_cannot_replace_hidden_helper_factory_or_leak_it_into_records(self):
        from services.agent.online_turn import current_helper_runtime

        ran = []
        factory = lambda *args, **kwargs: "host-only"

        def repair():
            ran.append(current_helper_runtime() is factory)
            return {"ok": True}

        records = []
        [binding] = [b for b in build_host_tools(_context(
            {"repair": _entry(repair, "repair")},
            helper_runtime=factory, tool_calls=records)) if b.name == "repair"]
        self.assertNotIn("helper_runtime", binding.input_schema.get("properties", {}))
        refused = dispatch_host_tool(binding, {"helper_runtime": "attacker"}, None, None)
        self.assertFalse(refused["ok"])
        self.assertEqual(ran, [])
        self.assertEqual(records, [])
        self.assertTrue(dispatch_host_tool(binding, {}, None, None)["ok"])
        self.assertEqual(ran, [True])
        self.assertIsNone(current_helper_runtime())
        self.assertNotIn("helper_runtime", json.dumps(records))

    def test_helper_runtime_is_thread_local_and_restored_after_failure(self):
        from services.agent.online_turn import current_helper_runtime, current_tool_session_id

        barrier = threading.Barrier(2)
        observed = {}

        def execute(actor):
            def repair():
                barrier.wait(timeout=2)
                observed[actor] = (current_helper_runtime()(), current_tool_session_id())
                raise RuntimeError("diagnosis failed")

            binding = HostToolBinding(
                "repair", "repair", {"type": "object", "properties": {}}, handler=repair,
                context={"policy_registry": _Policy(), "helper_runtime": lambda: actor,
                         "session_id": f"session-{actor}"})
            try:
                dispatch_host_tool(binding, {}, None, None)
            except RuntimeError:
                observed[actor + "-cleared"] = (
                    current_helper_runtime() is None and current_tool_session_id() is None
                )

        threads = [threading.Thread(target=execute, args=(actor,)) for actor in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(observed, {
            "a": ("a", "session-a"), "b": ("b", "session-b"),
            "a-cleared": True, "b-cleared": True,
        })


if __name__ == "__main__":
    unittest.main()


def test_cell_sdk_tool_threads_rebind_trusted_owner_and_explicit_roots(monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from services.agent import request_context as tenancy

    actor = "tool-owner"
    ctx = {
        "cell": True, "workspace_key": "team:tools/work", "actor_key": actor,
        "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
        "right": "editor", "epoch": 7,
    }
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", "/work/ambient")
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", "/work/ambient-save")

    def probe():
        return {
            "owner": list(tenancy.owner_key()),
            "project": tenancy.root_value("CHUDWARE_PROJECT_DIR"),
            "save": tenancy.root_value("CHUDWARE_PROJECT_SAVE_DIR"),
            "epoch": tenancy.current_context()["epoch"],
        }

    with tenancy.bind_context(ctx), tenancy.bind_roots("/work/selected", "/work/selected/save"):
        binding = HostToolBinding(
            "probe", "Inspect the trusted request scope", {"type": "object"},
            handler=probe, context=_context({"probe": _entry(probe, "Inspect scope")}),
        )
        tool = binding.to_host_tool(lambda **kwargs: kwargs)
    results, failures = [], []

    def sdk_thread():
        try:
            # No contextvars are inherited into this new thread.
            assert tenancy.current_context() == {}
            results.append(tool["execute"]({}, SimpleNamespace(cancelled=False)))
            assert tenancy.current_context() == {}
            assert tenancy.root_value("CHUDWARE_PROJECT_DIR") == "/work/ambient"
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=sdk_thread)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert not failures
    assert results[0]["details"] == {
        "owner": ["team:tools/work", actor], "project": "/work/selected",
        "save": "/work/selected/save", "epoch": 7,
    }


def test_timeout_worker_preserves_full_host_context_and_turn_pool(monkeypatch):
    import hashlib
    from contextvars import ContextVar
    from services.agent import config, request_context as tenancy
    from services.agent.online_turn import current_helper_runtime, current_tool_session_id
    from services.mcp import core_bridge, daemon_bridge

    # Bound arbitrary caller state must cross the timeout thread along with the
    # known authority fields; a tenancy-only rebind would still lose this marker.
    marker = ContextVar("timeout_caller_marker", default=None)
    ctx = {"cell": True, "workspace_key": "team:tools/work", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    caller_thread = threading.get_ident()
    connections = []

    class Connection:
        def __init__(self, *, writer_group, agent_run_scope=""):
            self.owner = tenancy.owner_key()
            self.writer_group = writer_group
            self.closed = False
            connections.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(daemon_bridge, "DaemonClient", Connection)
    monkeypatch.setattr(config, "get_tool_options", lambda *args: {"timeout_s": 10})
    helper = object()
    observed = []

    def probe():
        connection = daemon_bridge.client()
        observed.append({
            "context": tenancy.current_context(),
            "roots": (tenancy.root_value("CHUDWARE_PROJECT_DIR"),
                      tenancy.root_value("CHUDWARE_PROJECT_SAVE_DIR")),
            "session": current_tool_session_id(),
            "helper": current_helper_runtime(),
            "document_owner": core_bridge.CURRENT_AGENT.get(),
            "marker": marker.get(),
            "thread": threading.get_ident(),
            "connection": connection,
        })
        marker.set("worker-local")
        return {"ok": True}

    with tenancy.bind_context(ctx), tenancy.bind_roots("/work/selected", "/work/save"):
        pool = daemon_bridge.TurnConnections()
        binding = HostToolBinding(
            "scope_probe", "Inspect owning context", {"type": "object"},
            handler=probe, context=_context(
                {"scope_probe": _entry(probe, "Inspect owning context")},
                helper_runtime=helper, session_id="parent-session",
                run_id="owning-run", agent_id="child", daemon_connections=pool,
            ),
        )
        token = marker.set("caller-owned")
        try:
            for _ in range(2):
                assert dispatch_host_tool(binding, {}, None, None) == {"ok": True}
                assert marker.get() == "caller-owned"
                assert tenancy.current_context() == ctx
                assert current_helper_runtime() is None and current_tool_session_id() is None
            assert len(connections) == 1
            for scope in observed:
                assert scope["context"] == ctx
                assert scope["roots"] == ("/work/selected", "/work/save")
                assert scope["session"] == "parent-session"
                assert scope["helper"] is helper
                assert scope["document_owner"] == ("owning-run", "child")
                assert scope["marker"] == "caller-owned"
                assert scope["thread"] != caller_thread
                assert scope["connection"] is connections[0]
            assert connections[0].owner == ("team:tools/work", "alice")
            assert not connections[0].closed
        finally:
            marker.reset(token)
            pool.close()
        assert connections[0].closed


def _root_tool_entries():
    from mcp.server.fastmcp import FastMCP
    from services.mcp import tool_registry
    from services.mcp.fs_tools import register_fs_tools
    from services.mcp.workspace_tools import register_workspace_tools

    mcp = FastMCP("fresh-thread-root-regression")
    with mock.patch.dict(tool_registry._CATALOG, {}, clear=True):
        register_fs_tools(tool_registry.view(mcp, "files"))
        register_workspace_tools(tool_registry.view(mcp, "workspace"))
    return mcp._tool_manager._tools


def test_initial_and_refreshed_sdk_threads_use_each_tenants_nested_roots(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from services.agent import omp_roles, config, request_context as tenancy, workspace
    from services.mcp import core_bridge

    entries = _root_tool_entries()
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(repository))
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(repository))
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(repository / "ambient-save"))
    monkeypatch.setattr(config, "get_tool_options", lambda *_: dict(_NEUTRAL_TOOL_OPTIONS))
    monkeypatch.setattr(config, "tool_timeout_s_default", lambda: 0)
    monkeypatch.setattr(workspace, "_session_registry", {})
    tenants = {}
    for actor in ("alice", "bob"):
        root = repository / actor / "selected"
        root.mkdir(parents=True)
        (root / f"{actor}.stl").write_text(f"solid {actor}\nendsolid {actor}\n")
        save = root / "saved"
        ctx = {
            "cell": True, "workspace_key": f"team:{actor}/workspace", "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": "editor", "epoch": 1,
        }
        context = _context(
            entries, project_dir=str(root / ".." / "selected"), save_dir=str(save),
            run_id=f"run-{actor}", session_id=f"session-{actor}", categories=[],
            allowed_tools=["workspace_status", "browse_models", "fs_list"],
        )

        class Client:
            _omp_bridge = SimpleNamespace(publish_tools=lambda definitions: None)
            def set_custom_tools(self, tools):
                self.tools = {tool["name"]: tool for tool in tools}

        client = Client()
        # The real worker builds its initial tools before binding the turn roots,
        # so there is no bind_roots around this assembly.
        with tenancy.bind_context(ctx):
            bindings = build_host_tools(context)
            client.set_custom_tools([
                binding.to_host_tool(lambda **kwargs: kwargs) for binding in bindings
            ])
            omp_roles._register_category_refresh(context, bindings)(client)
        tenants[actor] = (root, save, ctx, context, client)

    engine_scopes = []

    def engine(method, params=None):
        assert method == "doc_list"
        actor = tenancy.owner_key()[1]
        root, save, _, _, _ = tenants[actor]
        engine_scopes.append((actor, tenancy.root_value("CHUDWARE_PROJECT_SAVE_DIR")))
        return {"docs": [{"doc_id": actor, "path": str(root / f"{actor}.stl")}]}

    monkeypatch.setattr(core_bridge, "call", engine)
    failures = []
    seen = {}

    def callback(actor, phase):
        try:
            assert tenancy.current_context() == {}
            root, save, _, _, client = tenants[actor]
            host_context = SimpleNamespace(
                cancelled=False, tool_call_id=f"js-write-{actor}-{phase}",
                agent_id="ArmBuilder", parent_agent_id="Main",
            )
            results = {}
            for name, args in (
                ("workspace_status", {}),
                ("browse_models", {"sheets": False, "pattern": "*.stl"}),
                ("fs_list", {}),
            ):
                out = client.tools[name]["execute"](args, host_context)
                results[name] = json.loads(out["content"][0]["text"])
                assert results[name] == out["details"]
            seen[actor, phase] = results
            if phase == 0:
                loaded = client.tools["enable_tools"]["execute"](
                    {"tools": ["fs_read"]}, host_context,
                )["details"]
                assert loaded["ok"], loaded
                assert "fs_read" in client.tools
            assert tenancy.current_context() == {}
            assert tenancy.root_value("CHUDWARE_FS_ROOT") == str(repository)
        except BaseException as exc:
            failures.append(exc)

    for phase in range(2):
        threads = [threading.Thread(target=callback, args=(actor, phase)) for actor in tenants]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert all(not thread.is_alive() for thread in threads)
    assert not failures
    for (actor, phase), results in seen.items():
        root, save, _, _, _ = tenants[actor]
        status = results["workspace_status"]
        assert status["ok"], status
        assert status["project_dir"] == str(root)
        assert status["docs"] == [{
            "doc_id": actor, "path": str(root / f"{actor}.stl"), "location": "project",
        }]
        browse = results["browse_models"]
        assert browse["ok"], browse
        assert browse["root"] == str(root)
        assert [row["file"] for row in browse["models"]] == [f"{actor}.stl"]
        listing = results["fs_list"]
        assert listing["ok"], listing
        assert listing["path"] == str(root)
        assert [row["name"] for row in listing["entries"]] == [f"{actor}.stl"]
    assert len(seen) == 4
    assert sorted(engine_scopes) == sorted(
        (actor, str(tenants[actor][1])) for actor in tenants for _ in range(2)
    )
    alice = tenants["alice"]
    bob = tenants["bob"]
    with tenancy.bind_context(bob[2]):
        refused = alice[4].tools["fs_list"]["execute"](
            {}, SimpleNamespace(cancelled=False),
        )["details"]
    assert refused["reason"] == "not_found"


def test_explicit_blank_tenant_root_never_uses_ambient_workspace(tmp_path, monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from services.agent import config, request_context as tenancy

    entries = _root_tool_entries()
    (tmp_path / "private.txt").write_text("ambient tenant data")
    for name in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT", "CHUDWARE_PROJECT_SAVE_DIR"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setattr(config, "get_tool_options", lambda *_: dict(_NEUTRAL_TOOL_OPTIONS))
    ctx = {
        "cell": True, "workspace_key": "team:blank/workspace", "actor_key": "blank",
        "actor_hex": hashlib.sha256(b"blank").hexdigest()[:32],
        "right": "editor", "epoch": 1,
    }
    with tenancy.bind_context(ctx):
        bindings = build_host_tools(_context(
            entries, project_dir="", workspace_dir=str(tmp_path), save_dir="",
            allowed_tools=["fs_list", "browse_models", "workspace_status"],
        ))
    results = []

    def callback():
        with tenancy.bind_roots(str(tmp_path)):
            for binding in bindings:
                tool = binding.to_host_tool(lambda **kwargs: kwargs)
                results.append(tool["execute"]({}, SimpleNamespace(cancelled=False))["details"])

    thread = threading.Thread(target=callback)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert len(results) == len(bindings)
    assert all(result["ok"] is False for result in results)
    assert all(result["reason"] == "workspace_root_unavailable" for result in results)


def test_refreshed_binding_recovers_the_original_wait_with_context_roots(tmp_path, monkeypatch):
    from services.agent import config, request_context as tenancy

    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    invocations = []
    options = dict(_NEUTRAL_TOOL_OPTIONS, timeout_s=0.02)
    monkeypatch.setattr(config, "get_tool_options", lambda *_: dict(options))
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path.parent))
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path.parent))

    def pending(value=1):
        invocations.append(value)
        entered.set()
        try:
            assert release.wait(5)
            return {"ok": True, "value": value,
                    "project": tenancy.root_value("CHUDWARE_PROJECT_DIR")}
        finally:
            completed.set()

    context = _context(
        {"pending": _entry(pending, "Compute a value")},
        project_dir=str(tmp_path), save_dir=str(tmp_path / "save"),
        run_id="same-run", session_id="same-session", allowed_tools=["pending"],
    )
    binding = next(tool for tool in build_host_tools(context) if tool.name == "pending")
    try:
        first = dispatch_host_tool(binding, {"value": 7}, None, None, agent_id="Builder")
        assert entered.wait(1)
        assert first["status"] == "still_running"
        # Refresh under the active turn roots used to change recovery identity.
        with tenancy.bind_roots(str(tmp_path), str(tmp_path / "save")):
            refreshed = next(tool for tool in build_host_tools(context) if tool.name == "pending")
        retry = dispatch_host_tool(refreshed, {"value": 7}, None, None, agent_id="Builder")
        assert retry["status"] == "still_running"
        assert retry["started_new"] is False
        assert invocations == [7]
        release.set()
        options["timeout_s"] = 1
        result = dispatch_host_tool(refreshed, {"value": 7}, None, None, agent_id="Builder")
        assert result == {"ok": True, "value": 7, "project": str(tmp_path)}
        assert invocations == [7]
    finally:
        release.set()
        assert completed.wait(5)


def test_part_search_host_result_realigns_sheet_after_truncation(tmp_path, monkeypatch):
    import base64
    import io
    from PIL import Image
    from services.mcp.parts_tools import register_parts_tools
    from services.parts import thumbs
    import services.parts as parts

    class Registry:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register

    icons = []
    for i in range(16):
        image = io.BytesIO()
        Image.new("RGB", (24, 24), (i * 13, 30, 100)).save(image, format="PNG")
        icons.append(image.getvalue())
    rows = [{"part_id": f"p{i}", "title": f"Part {i}",
             "params": {"large": "x" * 4000}} for i in range(16)]
    monkeypatch.setattr(parts, "search", lambda query, **kw: rows)
    monkeypatch.setattr(thumbs, "resolve_previews",
                        lambda items, **kw: [{"ok": True, "data": icons[i],
                                              "provenance": "product_image"}
                                             for i, _ in enumerate(items)])
    monkeypatch.setattr("services.paths.CACHE", tmp_path / "cache")
    registry = Registry()
    register_parts_tools(registry)
    result = registry.tools["part_search"]("part", limit=16, detail="full", include_live=False)
    out = host_tool_result(result, max_bytes=4096)
    text = out["content"][0]["text"]
    body = json.loads(text)
    assert len(text.encode("utf-8")) <= 4096 and body == out["details"]
    assert body["_truncated"]
    indices = [row["index"] for row in body["results"]]
    assert 0 < len(indices) < 16
    assert body["preview_sheet"]["pictured"] == indices
    image_block = next(block for block in out["content"] if block["type"] == "image")
    with Image.open(io.BytesIO(base64.b64decode(image_block["data"]))) as sheet:
        assert sheet.format == "PNG"
        for position, index in enumerate(indices):
            cols = min(4, len(indices))
            px = (position % cols) * 384 + 192
            py = (position // cols) * 360 + 200
            assert sheet.getpixel((px, py))[:3] == (index * 13, 30, 100)


def test_mission_search_attaches_preview_for_category_only(tmp_path, monkeypatch):
    import io
    from PIL import Image
    from services.agent.agent_turn import _tool_result_parts
    from services.agent.mission_dispatch import MissionDispatch
    from services.parts import thumbs
    import services.parts as parts

    icon = io.BytesIO()
    Image.new("RGB", (24, 24), "blue").save(icon, format="PNG")
    calls = []

    def search(query, **kw):
        calls.append((query, kw))
        return [{"part_id": "bearing:1", "title": "Bearing", "params": {"bore": 12}}]

    monkeypatch.setattr(parts, "search", search)
    monkeypatch.setattr(thumbs, "resolve_previews",
                        lambda items, **kw: [{"ok": True, "data": icon.getvalue(),
                                              "provenance": "cad_render"}])
    monkeypatch.setattr("services.paths.CACHE", tmp_path / "cache")
    out = MissionDispatch.part_search(None, category_id="bearings_motion.ball_bearings.deep_groove",
                                      include_live=False)
    assert out["ok"] and out["preview_sheet"]["pictured"] == [0]
    assert calls[0][0] == "" and calls[0][1]["include_live"] is False
    assert out["results"][0]["part_id"] == "bearing:1"
    parts = _tool_result_parts("part_search", out)
    image = next(part for part in parts if part["type"] == "image")
    assert image["path"] == out["images"][0]["path"]
    with Image.open(image["path"]) as sheet:
        assert sheet.format == "PNG"
    disabled = MissionDispatch.part_search(None, domain="bearing", previews=False, include_live=False)
    assert disabled["ok"] and "images" not in disabled
    assert disabled["preview_sheet"]["reason"] == "previews_disabled"
