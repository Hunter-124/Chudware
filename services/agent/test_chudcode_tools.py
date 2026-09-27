from __future__ import annotations

import queue
import threading
import unittest
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from unittest import mock

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


# Turn-loop primitives the adapter floors onto every surface, restricted or not.
_META_FLOOR = frozenset({"enable_tools", "list_tool_categories"})


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

        # Registry order is preserved as the leading prefix; the meta floor follows it.
        names = [binding.name for binding in bindings]
        self.assertEqual(names[: len(entries)], list(entries))
        self.assertEqual(
            sorted(names[len(entries) :]), ["enable_tools", "list_tool_categories"]
        )
        self.assertEqual(bindings[0].description, entries["alpha"].description)
        self.assertEqual(bindings[0].input_schema, alpha_schema)
        self.assertEqual(bindings[0].load_mode, "deferred")
        self.assertEqual(
            list(bindings[0].definition),
            ["name", "description", "input_schema", "load_mode"],
        )

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
        binding = build_host_tools(_context(entries, allowed_tools=None))[0]

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
        # The meta floor rides along on every surface; the collision verdict is about
        # `read` (dropped, unowned) versus `write` (kept, explicitly cad_owned).
        self.assertEqual(
            [name for name in names if name not in _META_FLOOR], ["write", "cad_unique"]
        )

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
        self.assertEqual(updates, [{
            "content": [{"type": "text", "text": '{\n  "step": 1\n}'}],
            "details": {"step": 1},
        }])

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


if __name__ == "__main__":
    unittest.main()
