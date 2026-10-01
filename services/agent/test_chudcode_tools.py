from __future__ import annotations

import importlib.util
import json
import queue
import tempfile
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
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["details"], {"step": 1})
        self.assertEqual(json.loads(updates[0]["content"][0]["text"]), {"step": 1})

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
        from services.agent import ledger, workspace
        from services.mcp import tool_registry
        from services.mcp.ledger_tools import register_ledger_tools
        from services.mcp.workspace_tools import register_workspace_tools

        mcp = FastMCP("selector-regression")
        with mock.patch.dict(tool_registry._CATALOG, {}, clear=True):
            register_workspace_tools(tool_registry.view(mcp, "workspace"))
            register_ledger_tools(tool_registry.view(mcp, "ledger"))
        entries = mcp._tool_manager._tools
        context = _context(entries, run_id="gui-orientation", actor="modeler")
        bindings = {b.name: b for b in build_host_tools(context)}

        def call(name, args):
            return dispatch_host_tool(bindings[name], args, None, None)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            ledger, "cache_root", return_value=Path(root)
        ), mock.patch.dict(workspace._session_registry, {}, clear=True):
            empty = call("workspace_status", {})
            self.assertTrue(empty["ok"])
            self.assertEqual(empty["run_id"], "")
            self.assertEqual(empty["docs"], [])
            unknown = call("workspace_status", {"run_id": "not-a-workspace"})
            self.assertFalse(unknown["ok"])
            self.assertEqual(unknown["run_id"], "not-a-workspace")
            self.assertIn("describe_project()", unknown["do_instead"])

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


# The runtime venv carries chudcode_rpc (vendored Chudcode client); a bare service venv may not.
_HAS_CHUDCODE_RPC = importlib.util.find_spec("chudcode_rpc") is not None


@unittest.skipUnless(_HAS_CHUDCODE_RPC, "chudcode_rpc client not installed in this venv")
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
        from chudcode_rpc import RpcClient, host_tool

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

    def test_json_text_result_carries_structured_details_and_its_failure(self):
        records = []
        out, _ = self._execute(
            lambda: json.dumps({"ok": False, "error": "fillet too large"}, indent=2),
            tool_calls=records,
        )
        self.assertEqual(out["details"], {"ok": False, "error": "fillet too large"})
        self.assertEqual(json.loads(out["content"][0]["text"])["error"], "fillet too large")
        self.assertIs(records[-1]["ok"], False)

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
        with open(body["_truncated"]["full_result"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), rows)

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


if __name__ == "__main__":
    unittest.main()
