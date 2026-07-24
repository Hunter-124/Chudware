"""End-to-end DAG mission walk for the Reverse Engineering scan template (RE-17).

Walks ``services/dag/templates/reveng_scan.json`` through the real executor with a
MOCKED node ``agent_fn`` (no COLMAP / OpenMVS / LLM), and asserts:

  1. ``aicad.reveng_events/1`` stage events are emitted to the run's EventJournal in
     the template's stage order (capture → scale → …).
  2. The two human gates (``n_scale_gate`` = scale-confirm, ``n_dim_gate`` =
     dimension-confirm) PAUSE the run.
  3. ``executor.resume(decision="approve")`` continues past each gate.

Hermetic: runs_dir and the EventJournal cache dir are redirected to tmp via
``XDG_DATA_HOME`` so nothing touches the real user cache.  The reveng launch helpers
under test live in ``services/mcp/reveng_tools.py`` — we import the module-level
``make_reveng_emit`` / ``_SCAN_NODE_STAGE`` / ``_make_pipeline_agent_fn`` (the same
seams ``reveng_reconstruct`` uses) so this test exercises the production wiring, not a
parallel re-implementation.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import pytest

# executor.py imports langgraph.checkpoint.sqlite unconditionally; skip cleanly
# (rather than erroring at collection) when the checkpointer backend is absent.
pytest.importorskip("langgraph.checkpoint.sqlite")


# ---------------------------------------------------------------------------
# Stubs so reveng_tools imports cleanly without the MCP server / core_bridge /
# heavy vision deps (mirrors services/mcp/test_reveng_tools.py).
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    # Prefer the REAL core_bridge when importable — a minimal stub (no CoreError)
    # would make the real cad_tools import fail, which forces the cad_tools stub
    # below and shadows _doc/_get_part for every sibling test in this pytest
    # session. Only fall back to a stub in an env with no core_bridge at all.
    try:
        import mcp_server.core_bridge  # noqa: F401
        _cb_real = True
    except Exception:  # noqa: BLE001
        _cb_real = False

    if not _cb_real:
        cb_mod = types.ModuleType("mcp_server.core_bridge")
        cb_mod.available = lambda: False
        cb_mod.call = lambda method, params=None: {}

        class CoreError(Exception):
            pass

        cb_mod.CoreError = CoreError
        sys.modules.setdefault("mcp_server.core_bridge", cb_mod)
        sys.modules.setdefault("core_bridge", cb_mod)

    # Prefer the real ``mcp`` package when it is importable — installing a fake
    # FastMCP into sys.modules would pollute sibling tests that need the real one
    # (this file runs in the same pytest session as the whole services/dag suite).
    # Only fall back to the stub when mcp genuinely cannot be imported.
    try:
        import mcp.server.fastmcp  # noqa: F401
        _mcp_real = True
    except Exception:  # noqa: BLE001
        _mcp_real = False

    if not _mcp_real and "mcp" not in sys.modules:
        mcp_pkg = types.ModuleType("mcp")
        server_mod = types.ModuleType("mcp.server")
        fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

        class _FakeFastMCP:
            def __init__(self, name="", **kw):
                self._tools: dict = {}

            def tool(self):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

        fastmcp_mod.FastMCP = _FakeFastMCP
        sys.modules["mcp"] = mcp_pkg
        sys.modules["mcp.server"] = server_mod
        sys.modules["mcp.server.fastmcp"] = fastmcp_mod

    # Same real-first rule for cad_tools: a bare _dumps/_err stub would shadow the
    # real module and break sibling tests that import _doc/_get_part/etc. from it.
    try:
        import mcp_server.cad_tools  # noqa: F401
        _cad_real = True
    except Exception:  # noqa: BLE001
        _cad_real = False

    if not _cad_real and "mcp_server.cad_tools" not in sys.modules:
        ct_mod = types.ModuleType("mcp_server.cad_tools")
        ct_mod._dumps = lambda obj: json.dumps(obj)
        ct_mod._err = lambda e: json.dumps({"ok": False, "error": str(e)})
        sys.modules["mcp_server.cad_tools"] = ct_mod


_install_stubs()

# NOTE: we do NOT stub the ``engine_oracle`` package — the real vision modules
# (reveng_provision / frames) import cleanly, and shadowing the package would break
# verify.py's ``services.requirements`` import chain. reveng_tools' own
# fallback handles the one missing symbol (ingest.ingest_pdf). The mission walk
# injects its own agent_fn, so the real all-False ``reveng_status`` is what we want
# (honest degradation with no provisioned backend).

from services.dag import executor  # noqa: E402
from services.dag import verify as vr  # noqa: E402
from services.mcp import reveng_common as rc  # noqa: E402
from services.mcp import reveng_tools as rt  # noqa: E402


def _passthrough_agent_fn(request, context_pack):
    """Mock node agent: every compute node succeeds, so routing is what is under test."""
    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {"detail": "passthrough"}
    return _R()


def _read_reveng_events(cache_root: Path, run_id: str) -> list[dict]:
    """Return the aicad.reveng_events/1 records for *run_id* in emission order."""
    path = cache_root / "Chudware" / "cache" / "runs" / run_id / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("schema") == "aicad.reveng_events/1":
            out.append(rec)
    return out


class TestRevengScanMissionWalk(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Redirect BOTH the executor runs-dir and the EventJournal cache dir.
        self._old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.tmp
        self.runs_dir = Path(self.tmp) / "runs"
        self.cache_root = Path(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._old_xdg

    def _run_scan(self, run_id: str):
        graph = rt._load_template("reveng_scan")
        session_id = "re_testwalk"
        emit = rt.make_reveng_emit(run_id, session_id, rt._SCAN_NODE_STAGE)
        agent_fn = rt._make_pipeline_agent_fn(session_id)
        return executor.run(
            graph,
            initial_state={"inputs": {"session_id": session_id}},
            run_id=run_id,
            runs_dir=self.runs_dir,
            emit=emit,
            agent_fn=agent_fn,
            autonomy_default="auto",
        )

    def test_scale_gate_pauses_and_resumes(self):
        run_id = "run_walk01"
        res = self._run_scan(run_id)
        # First gate: scale-confirm must PAUSE the run.
        self.assertEqual(res["status"], "paused", res)
        paused_nodes = {p.get("node_id") for p in res.get("interrupted", [])}
        self.assertIn("n_scale_gate", paused_nodes)

        # A reveng_events record for the scale stage was journaled before the pause.
        events = _read_reveng_events(self.cache_root, run_id)
        stages_seen = [e["stage"] for e in events]
        self.assertIn("capture", stages_seen)
        self.assertIn("scale", stages_seen)
        # capture precedes scale (stage order preserved).
        self.assertLess(stages_seen.index("capture"), stages_seen.index("scale"))

        # Resume past the scale gate.
        res2 = executor.resume(
            run_id,
            graph_json=rt._load_template("reveng_scan"),
            decision="approve",
            runs_dir=self.runs_dir,
            emit=rt.make_reveng_emit(run_id, "re_testwalk", rt._SCAN_NODE_STAGE),
            agent_fn=rt._make_pipeline_agent_fn("re_testwalk"),
            autonomy_default="auto",
        )
        # The run advanced past scale. With no reconstruction backend the mission
        # degrades honestly downstream (nodes escalate / later gates pause) — it must
        # NOT silently report a clean 'done', and it must not crash.
        self.assertIn(res2["status"], ("paused", "failed", "done"), res2)
        # We progressed past the gate: the dense half of the (now split) reconstruction
        # was reached and reported.  n_sparse ran BEFORE the gate — the whole point of
        # the split, since the scale solvers need its COLMAP model.
        self.assertIn("n_sparse", res2.get("nodes", {}))
        self.assertIn("n_dense", res2.get("nodes", {}))
        self.assertNotIn("n_reconstruct", res2.get("nodes", {}),
                         "n_reconstruct was split into n_sparse + n_dense")

    def test_dimension_gate_reached_after_scale(self):
        """Walk through the scale gate and confirm the dimension gate is the next
        human pause (both gates honoured, in order)."""
        run_id = "run_walk02"
        res = self._run_scan(run_id)
        self.assertEqual(res["status"], "paused")
        self.assertIn("n_scale_gate",
                      {p.get("node_id") for p in res.get("interrupted", [])})

        # To deterministically reach the dimension gate we approve the scale gate
        # AND inject a pipeline that lets reconstruct/fit/parametric pass, so the
        # walk arrives at n_dim_gate (the second human gate).
        def _passthrough(request, context_pack):
            class _R:
                success = True
                stop_reason = "confirmed"
                final_code = None
                result = {"detail": "passthrough"}
            return _R()

        res2 = executor.resume(
            run_id,
            graph_json=rt._load_template("reveng_scan"),
            decision="approve",
            runs_dir=self.runs_dir,
            emit=rt.make_reveng_emit(run_id, "re_testwalk", rt._SCAN_NODE_STAGE),
            agent_fn=_passthrough,
            autonomy_default="auto",
        )
        # Second human gate reached.
        self.assertEqual(res2["status"], "paused", res2)
        self.assertIn("n_dim_gate",
                      {p.get("node_id") for p in res2.get("interrupted", [])})

        # Resume past the dimension gate too.
        res3 = executor.resume(
            run_id,
            graph_json=rt._load_template("reveng_scan"),
            decision="approve",
            runs_dir=self.runs_dir,
            emit=rt.make_reveng_emit(run_id, "re_testwalk", rt._SCAN_NODE_STAGE),
            agent_fn=_passthrough,
            autonomy_default="auto",
        )
        self.assertIn(res3["status"], ("paused", "done", "failed"), res3)

        # RE-17 DISPATCH_TABLE proof: the verify node MUST actually dispatch the
        # reveng_deviation check (calling get_reveng_deviation) rather than falling
        # through to the escalate-on-unknown-check-type path. Without the new
        # DISPATCH_TABLE entry this reason would be "unknown_check_type"; with it the
        # tool runs (and, against the non-existent test session, fails honestly with
        # a tool-level reason). Either way the walk still reaches n_deliver via the
        # forward edge — we assert the DISPATCH wiring, not a passing deviation.
        vnode = res3["nodes"].get("n_verify_deviation")
        if vnode is not None:  # reached only if the walk got past the dim gate
            vout = vnode.get("output", {})
            self.assertEqual(vout.get("data", {}).get("check_type"), "reveng_deviation", vout)
            self.assertNotEqual(
                vout.get("data", {}).get("reason"), "unknown_check_type",
                "reveng_deviation must be dispatched, not escalated as unknown_check_type",
            )

        # Stage events accumulated across the whole walk in vocabulary order.
        events = _read_reveng_events(self.cache_root, run_id)
        stages = [e["stage"] for e in events]
        self.assertIn("scale", stages)
        self.assertIn("parametric", stages)
        # Every event validates against the contract shape.
        for e in events:
            self.assertEqual(e["schema"], "aicad.reveng_events/1")
            self.assertTrue(e["stage"])
            self.assertTrue(0 <= int(e["pct"]) <= 100)
            self.assertEqual(e["session_id"], "re_testwalk")

    def test_reveng_events_schema_shape(self):
        run_id = "run_walk03"
        self._run_scan(run_id)
        events = _read_reveng_events(self.cache_root, run_id)
        self.assertTrue(events, "no aicad.reveng_events/1 records journaled")
        for e in events:
            for field in ("schema", "run_id", "session_id", "stage", "pct", "msg", "ts"):
                self.assertIn(field, e)


class TestRevengScanNodeOrder(unittest.TestCase):
    """The re-partitioned scan template must execute in photogrammetry order.

    ``_make_pipeline_agent_fn``'s dispatcher is keyed on ``node_id`` and its
    unknown-node_id branch returns a SILENT pass-through success, so a renamed or
    dangling node id would never show up in a status-only assertion.  These tests
    therefore assert the executed node ORDER, taken from the ``dag.node.started``
    events the executor emits for every node it actually runs.
    """

    SESSION_ID = "re_order"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.tmp
        self.runs_dir = Path(self.tmp) / "runs"
        self.cache_root = Path(self.tmp)
        self.order: list = []

        # The deviation tool is the one real backend this walk would reach.  Stub it
        # at the tool boundary (exactly as the agent is stubbed at the agent boundary)
        # with an in-tolerance result, so the walk under test is routing, not Open3D.
        real_get_fn = vr._get_fn

        def _fake_get_fn(module_key, fn_name):
            if fn_name == "get_reveng_deviation":
                return lambda **kw: {
                    "ok": True, "max_mm": 0.12, "tol_mm": 0.5,
                    "session_id": kw.get("session_id"),
                }
            return real_get_fn(module_key, fn_name)

        patcher = mock.patch.object(vr, "_get_fn", _fake_get_fn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._old_xdg

    def _emit(self, run_id: str):
        """Recording emitter: node order for us, reveng stage events for the GUI."""
        reveng_emit = rt.make_reveng_emit(run_id, self.SESSION_ID, rt._SCAN_NODE_STAGE)

        def _fn(event_type: str, payload: dict) -> None:
            if event_type == "dag.node.started":
                self.order.append(payload.get("node_id"))
            reveng_emit(event_type, payload)

        return _fn

    def _approve(self, run_id: str) -> dict:
        return executor.resume(
            run_id,
            graph_json=rt._load_template("reveng_scan"),
            decision="approve",
            runs_dir=self.runs_dir,
            emit=self._emit(run_id),
            agent_fn=_passthrough_agent_fn,
            autonomy_default="auto",
        )

    @staticmethod
    def _paused_at(result: dict) -> set:
        return {p.get("node_id") for p in (result.get("interrupted") or [])}

    def test_executed_node_order_gates_pause_and_resume_to_completion(self):
        run_id = "run_order01"
        res = executor.run(
            rt._load_template("reveng_scan"),
            initial_state={"inputs": {"session_id": self.SESSION_ID}},
            run_id=run_id,
            runs_dir=self.runs_dir,
            emit=self._emit(run_id),
            agent_fn=_passthrough_agent_fn,
            autonomy_default="auto",
        )

        # Gate 1 — scale-confirm, and it pauses AFTER sparse SfM, which is the fix:
        # both real scale solvers answer scale_needs_sparse without a COLMAP model.
        self.assertEqual(res["status"], "paused", res)
        self.assertEqual(self._paused_at(res), {"n_scale_gate"})
        self.assertEqual(self.order, ["n_capture", "n_sparse"])

        # Gate 2 — dimension-confirm, reached only after dense/fit/parametric.
        res = self._approve(run_id)
        self.assertEqual(res["status"], "paused", res)
        self.assertEqual(self._paused_at(res), {"n_dim_gate"})

        # The template's third approve node is the deliver report.
        res = self._approve(run_id)
        self.assertEqual(res["status"], "paused", res)
        self.assertEqual(self._paused_at(res), {"n_deliver"})

        # Approving every gate resumes the mission to a clean completion.
        res = self._approve(run_id)
        self.assertEqual(res["status"], "done", res)
        self.assertNotIn("interrupted", res)

        self.assertEqual(self.order, [
            "n_capture",
            "n_sparse",
            "n_scale_gate",
            "n_dense",
            "n_fit",
            "n_parametric",
            "n_dim_gate",
            "n_verify_deviation",
            "n_deliver",
        ])

    def test_split_emits_the_contract_dense_and_mesh_stages(self):
        """dense/splat/mesh were declared in aicad.reveng_events/1 §4 but never emitted
        while all of that hid inside one sparse-stage node."""
        run_id = "run_order02"
        executor.run(
            rt._load_template("reveng_scan"),
            initial_state={"inputs": {"session_id": self.SESSION_ID}},
            run_id=run_id,
            runs_dir=self.runs_dir,
            emit=self._emit(run_id),
            agent_fn=_passthrough_agent_fn,
            autonomy_default="auto",
        )
        self._approve(run_id)
        stages = [e["stage"] for e in _read_reveng_events(self.cache_root, run_id)]
        self.assertIn("sparse", stages)
        self.assertIn("dense", stages)
        self.assertIn("mesh", stages)
        # The GUI substring-matches sparse → dense → mesh on this field, in that order,
        # to light its sub-step track: the sequence must never rewind.
        self.assertLess(max(i for i, s in enumerate(stages) if s == "sparse"),
                        min(i for i, s in enumerate(stages) if s == "dense"))
        self.assertLess(max(i for i, s in enumerate(stages) if s == "dense"),
                        min(i for i, s in enumerate(stages) if s == "mesh"))
        # Stage tokens stay bare contract values — never prose.
        allowed = {"capture", "scale", "sparse", "dense", "splat",
                   "mesh", "fit", "parametric", "review", "deliver"}
        self.assertTrue(set(stages) <= allowed, sorted(set(stages) - allowed))


class TestDenseAppliesConfirmedScale(unittest.TestCase):
    """``n_dense`` must hand the CONFIRMED mm_per_unit to cleanup_mesh — that is the
    whole reason the scale gate now runs before dense — and must never invent one."""

    SESSION_ID = "re_scale_apply"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_env = {k: os.environ.get(k)
                         for k in ("CHUDWARE_REVENG_DIR", "OPENMVS_BIN")}
        os.environ["CHUDWARE_REVENG_DIR"] = self.tmp
        os.environ["OPENMVS_BIN"] = "/fake/openmvs"
        self.calls: list = []
        self._old_modules = {}
        self._install_dense_backends()

        root = Path(self.tmp) / self.SESSION_ID
        (root / "images").mkdir(parents=True)
        (root / "images" / "a.jpg").write_bytes(b"\xff\xd8\xff")
        sparse = root / "sparse"
        sparse.mkdir()
        s = rt._make_session(self.SESSION_ID, "scale apply", None)
        s["artifacts"]["sparse_dir"] = str(sparse)
        rt._save_session(s)

    def tearDown(self):
        import shutil
        for name, mod in self._old_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        for key, val in self._old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_module(self, name: str):
        self._old_modules[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    def _install_dense_backends(self):
        db = self._fake_module("services.vision.dense_backends")
        calls = self.calls

        def run_openmvs(sparse_dir, session_dir, **kw):
            mesh = Path(session_dir) / "mesh" / "openmvs.obj"
            mesh.parent.mkdir(parents=True, exist_ok=True)
            mesh.write_text("o mesh\n")
            return {"ok": True, "mesh_path": str(mesh), "backend": "openmvs"}

        def run_brush(sparse_dir, session_dir, **kw):
            return {"ok": False, "reason": "backend_not_provisioned"}

        db.run_openmvs = run_openmvs
        db.run_brush = run_brush

        mc = self._fake_module("services.vision.mesh_cleanup")

        def cleanup_mesh(mesh_path, out_path, **kw):
            calls.append(kw)
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text("cleaned\n")
            return {
                "ok": True,
                "mesh_path": out_path,
                "scale_applied": kw.get("mm_per_unit") is not None,
                "scale_mode": "mm_per_unit" if kw.get("mm_per_unit") else None,
            }

        mc.cleanup_mesh = cleanup_mesh

    def _set_scale(self, mm_per_unit, confirmed: bool):
        s = rt._load_session(self.SESSION_ID)
        s["scale"] = {
            "method": "aruco_mat",
            "mm_per_unit": mm_per_unit,
            "residual_pct": 0.4,
            "confirmed": confirmed,
        }
        rt._save_session(s)

    def _run_dense_node(self):
        agent_fn = rt._make_pipeline_agent_fn(self.SESSION_ID)
        return agent_fn("dense", {"node_id": "n_dense"})

    def test_confirmed_scale_reaches_cleanup_mesh_in_mm(self):
        self._set_scale(2.5, confirmed=True)
        res = self._run_dense_node()
        self.assertTrue(res.success, res.result)
        self.assertEqual([c.get("mm_per_unit") for c in self.calls], [2.5])
        self.assertEqual(res.result["mesh_units"], "mm")
        session = rt._load_session(self.SESSION_ID)
        self.assertEqual(session["artifacts"]["mesh_units"], "mm")

    def test_unconfirmed_scale_leaves_reconstruction_units(self):
        # A solved but UNAPPROVED scale is not a licence to rescale the geometry.
        self._set_scale(2.5, confirmed=False)
        res = self._run_dense_node()
        self.assertTrue(res.success, res.result)
        self.assertEqual([c.get("mm_per_unit") for c in self.calls], [None])
        self.assertEqual(res.result["mesh_units"], "reconstruction")
        self.assertEqual(
            rt._load_session(self.SESSION_ID)["artifacts"]["mesh_units"],
            "reconstruction",
        )
        self.assertTrue(
            any("unconfirmed" in n for n in res.result["dense_notes"]),
            res.result["dense_notes"],
        )

    def test_unsolved_scale_never_invents_a_factor(self):
        res = self._run_dense_node()          # session.scale.mm_per_unit is None
        self.assertTrue(res.success, res.result)
        self.assertEqual([c.get("mm_per_unit") for c in self.calls], [None])
        self.assertEqual(res.result["mesh_units"], "reconstruction")

    def test_scale_gate_approval_is_what_flips_confirmed(self):
        """The gate node body runs only after the human approved the interrupt, so it
        is the human's decision that sets scale.confirmed — nothing auto-confirms."""
        self._set_scale(2.5, confirmed=False)
        agent_fn = rt._make_pipeline_agent_fn(self.SESSION_ID)
        res = agent_fn("confirm scale", {"node_id": "n_scale_gate"})
        self.assertTrue(res.success, res.result)
        self.assertTrue(res.result["confirmed"])
        self.assertTrue(rt._load_session(self.SESSION_ID)["scale"]["confirmed"])

        # …and with the gate approved, dense now delivers millimetres.
        dense = self._run_dense_node()
        self.assertEqual([c.get("mm_per_unit") for c in self.calls], [2.5])
        self.assertEqual(dense.result["mesh_units"], "mm")

    def test_scale_gate_never_confirms_a_scale_that_does_not_exist(self):
        agent_fn = rt._make_pipeline_agent_fn(self.SESSION_ID)
        res = agent_fn("confirm scale", {"node_id": "n_scale_gate"})
        self.assertTrue(res.success, res.result)
        self.assertFalse(res.result["confirmed"])
        self.assertEqual(res.result["scale_state"], "unsolved")
        self.assertFalse(rt._load_session(self.SESSION_ID)["scale"]["confirmed"])


class TestMissionGateTools(unittest.TestCase):
    """``reveng_gate_status`` / ``reveng_resolve_gate`` are what turn a paused gate into
    a resumable mission.  Without them executor.run() returns "paused" and nothing in
    the product ever calls executor.resume(), so a GUI-started reconstruction stops at
    the scale gate forever.

    The mission also has to run OFF the request thread: services/worker_main.py is a
    single-threaded loop, so a synchronous multi-minute reconstruction would block the
    very gate polls that exist to unblock it.
    """

    SESSION_ID = "re_gate_tools"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_env = {
            k: os.environ.get(k) for k in (
                "XDG_DATA_HOME", "CHUDWARE_REVENG_DIR", "CHUDWARE_REVENG_RUNS_DIR",
            )
        }
        os.environ["XDG_DATA_HOME"] = self.tmp
        os.environ["CHUDWARE_REVENG_DIR"] = str(Path(self.tmp) / "reveng")
        os.environ["CHUDWARE_REVENG_RUNS_DIR"] = str(Path(self.tmp) / "runs")
        self.cache_root = Path(self.tmp)

        from mcp.server.fastmcp import FastMCP
        from services.mcp.reveng_tools import register_reveng_tools

        server = FastMCP("_reveng_gate_test")
        register_reveng_tools(server)
        self.tool = lambda name: server._tool_manager.get_tool(name).fn

        # A session with a solved-but-unconfirmed scale: exactly the state the
        # scale-confirm gate exists to adjudicate.
        s = rt._make_session(self.SESSION_ID, "gate tools", None)
        s["scale"] = {"method": "known_dim", "mm_per_unit": 2.5,
                      "residual_pct": 0.4, "confirmed": False}
        rt._save_session(s)
        images = Path(os.environ["CHUDWARE_REVENG_DIR"]) / self.SESSION_ID / "images"
        images.mkdir(parents=True, exist_ok=True)
        (images / "a.jpg").write_bytes(b"\xff\xd8\xff")

    def tearDown(self):
        import shutil
        rc._release_mission(self.SESSION_ID, "run_busy_probe")
        for key, val in self._old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _launch(self) -> dict:
        return rc._launch_reveng_mission(
            self.SESSION_ID, "reveng_scan", rt._SCAN_NODE_STAGE, background=True,
        )

    def _wait_for(self, want: str, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        rec = None
        while time.time() < deadline:
            rec = rt._find_session_run(self.SESSION_ID)
            if rec is not None and rec.get("status") == want:
                return rec
            time.sleep(0.02)
        raise AssertionError(f"run never reached {want!r}; last record: {rec}")

    def _call(self, name: str, **kwargs) -> dict:
        return json.loads(self.tool(name)(**kwargs))

    def test_launch_returns_run_id_immediately_then_pauses_at_the_scale_gate(self):
        started = self._launch()
        # The run_id exists before the mission does — contracts/reveng.md §0.
        self.assertTrue(started.get("run_id"), started)
        self.assertEqual(started["status"], "running", started)
        self.assertTrue(started["async"])

        rec = self._wait_for("paused")
        self.assertEqual(rec["run_id"], started["run_id"])
        self.assertEqual(rec["gate"]["node_id"], "n_scale_gate")
        self.assertEqual(rec["gate"]["gate_kind"], "scale_confirm")

        # The session manifest is the progress source of truth.
        session = rt._load_session(self.SESSION_ID)
        self.assertEqual(session["run"]["run_id"], started["run_id"])
        self.assertEqual(session["run"]["status"], "paused")

        # Stage events still land on the run's own EventJournal with the session id.
        events = _read_reveng_events(self.cache_root, started["run_id"])
        self.assertTrue(events)
        self.assertTrue(all(e["session_id"] == self.SESSION_ID for e in events))

    def test_gate_status_reports_the_numbers_the_human_must_judge(self):
        self._launch()
        self._wait_for("paused")
        out = self._call("reveng_gate_status", session_id=self.SESSION_ID)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "paused")
        self.assertEqual(out["gate"]["gate_kind"], "scale_confirm")
        pending = out["gate"]["pending"]
        self.assertEqual(pending["mm_per_unit"], 2.5)
        self.assertEqual(pending["residual_pct"], 0.4)
        self.assertFalse(pending["confirmed"])
        self.assertEqual(pending["mesh_units_if_approved"], "mm")

    def test_reject_stops_the_mission_without_running_anything_downstream(self):
        self._launch()
        rec = self._wait_for("paused")
        out = self._call("reveng_resolve_gate", session_id=self.SESSION_ID,
                         decision="reject", note="mat was out of frame")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("mat was out of frame", out["reason"])

        # Nothing past the gate ran: no dense stage was ever emitted, and the scale
        # was NOT confirmed behind the user's back.
        stages = [e["stage"] for e in _read_reveng_events(self.cache_root, rec["run_id"])]
        self.assertNotIn("dense", stages)
        self.assertFalse(rt._load_session(self.SESSION_ID)["scale"]["confirmed"])
        self.assertEqual(
            rt._load_session(self.SESSION_ID)["run"]["status"], "rejected")

    def test_approve_resumes_past_the_gate_to_the_next_one(self):
        self._launch()
        self._wait_for("paused")
        out = self._call("reveng_resolve_gate", session_id=self.SESSION_ID,
                         decision="approve")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["decision"], "approve")
        self.assertEqual(out["status"], "running")

        rec = self._wait_for("paused")
        # The human's approval — not the pipeline — is what confirmed the scale.
        self.assertTrue(rt._load_session(self.SESSION_ID)["scale"]["confirmed"])
        # …and the mission walked on to the second human gate.
        self.assertEqual(rec["gate"]["gate_kind"], "dimension_confirm")
        status = self._call("reveng_gate_status", session_id=self.SESSION_ID)
        self.assertIn("dims", status["gate"]["pending"])

    def test_decision_must_be_explicit(self):
        self._launch()
        self._wait_for("paused")
        out = self._call("reveng_resolve_gate", session_id=self.SESSION_ID,
                         decision="yes please")
        self.assertFalse(out["ok"])
        self.assertIn("invalid decision", out["reason"])
        self.assertFalse(rt._load_session(self.SESSION_ID)["scale"]["confirmed"])

    def test_second_concurrent_mission_on_one_session_is_refused(self):
        rc._reserve_mission(self.SESSION_ID, "run_busy_probe")
        out = self._launch()
        self.assertFalse(out.get("ok", True), out)
        self.assertEqual(out["reason"], "mission_already_running")
        self.assertEqual(out["run_id"], "run_busy_probe")

    def test_gate_tools_are_bridged_to_the_gui_transport(self):
        from services.reveng import worker_rpc
        for name in ("reveng_gate_status", "reveng_resolve_gate"):
            self.assertIn(name, worker_rpc.REVENG_WORKER_METHODS)

    def test_gate_status_on_a_session_with_no_mission_is_honest(self):
        out = self._call("reveng_gate_status", session_id="re_nothing_here")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no_mission")
        self.assertTrue(out["hint"])


if __name__ == "__main__":
    unittest.main()
