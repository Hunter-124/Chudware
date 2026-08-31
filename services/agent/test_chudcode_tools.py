from __future__ import annotations

import unittest
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from services.agent.chudcode_tools import (
    HostToolBinding,
    build_host_tools,
    dispatch_host_tool,
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


def _context(entries, **extra):
    context = {
        "tool_entries": entries,
        "resolver": lambda name: getattr(entries.get(name), "fn", None),
        "policy_registry": _Policy(),
        "chudcode_builtin_names": (),
    }
    context.update(extra)
    return context


class HostToolDefinitionTests(unittest.TestCase):
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

        self.assertEqual([binding.name for binding in bindings], list(entries))
        self.assertEqual(bindings[0].description, entries["alpha"].description)
        self.assertEqual(bindings[0].input_schema, alpha_schema)
        self.assertEqual(bindings[0].load_mode, "deferred")
        self.assertEqual(
            list(bindings[0].definition),
            ["name", "description", "input_schema", "load_mode"],
        )

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

        # The restricted floor may add meta-tools, but never arbitrary CAD tools.
        self.assertNotIn("alpha", [binding.name for binding in empty])
        self.assertEqual(
            [binding.name for binding in selected if binding.name in entries],
            ["beta"],
        )

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
        self.assertNotIn("unrelated", names)

    def test_registry_addition_appears_without_adapter_catalog_change(self):
        entries = OrderedDict(
            (
                (
                    "brand_new_cad_tool",
                    _entry(
                        _beta,
                        "new live tool",
                        {
                            "type": "object",
                            "properties": {"count": {"type": "integer"}},
                        },
                    ),
                ),
            )
        )
        binding = build_host_tools(_context(entries, allowed_tools=None))[0]
        self.assertEqual(binding.name, "brand_new_cad_tool")
        self.assertEqual(binding.description, "new live tool")
        self.assertIn("count", binding.input_schema["properties"])

    def test_chudcode_builtin_collision_requires_explicit_cad_ownership(self):
        entries = OrderedDict(
            (
                ("read", _entry(_alpha, "collision")),
                ("write", _entry(_alpha, "owned", cad_owned=True)),
                ("cad_unique", _entry(_alpha, "unique")),
            )
        )
        context = _context(entries, allowed_tools=None)
        context.pop("chudcode_builtin_names")
        names = [binding.name for binding in build_host_tools(context)]
        self.assertEqual(names, ["write", "cad_unique"])

    def test_effective_policy_removes_manufacturing_execution_tools(self):
        entries = OrderedDict(
            (
                ("alpha", _entry(_alpha, "ordinary")),
                ("cnc_execute", _entry(_beta, "physical execution")),
            )
        )
        context = _context(entries, allowed_tools=None)
        context["policy_registry"] = _Policy({"cnc_execute"})
        self.assertEqual([item.name for item in build_host_tools(context)], ["alpha"])


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

    def test_mission_governance_dispatch_precedes_global_handler(self):
        calls = []

        class Mission:
            _handlers: ClassVar[dict[str, object]] = {
                "delegate": object(),
                "mission_start": object(),
                "council_convene": object(),
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
                if name == "council_convene":
                    return lambda topic, actor=None: (
                        calls.append(("council_convene", topic, actor))
                        or {"ok": True, "council_id": "council-1"}
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
        council = self._binding(
            "council_convene",
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
            dispatch_host_tool(council, {"topic": "safety"}, None, None)["ok"]
        )
        self.assertEqual(
            calls,
            [
                ("delegate", "review", "manager"),
                ("mission_start", "build", "doc-9"),
                ("council_convene", "safety", "manager"),
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

        enabled_calls = []
        enabled = self._binding(
            "enable_tools",
            handler=lambda categories: None,
            enable_categories=lambda categories: enabled_calls.append(categories),
        )
        result = dispatch_host_tool(enabled, {"categories": ["measure"]}, None, None)
        self.assertEqual(enabled_calls, [["measure"]])
        self.assertIn("measure_part", result["added"])


if __name__ == "__main__":
    unittest.main()
