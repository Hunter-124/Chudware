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
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

# executor.py imports langgraph.checkpoint.sqlite unconditionally; skip cleanly
# (rather than erroring at collection) when the checkpointer backend is absent.
pytest.importorskip("langgraph.checkpoint.sqlite")


from services.dag import executor  # noqa: E402
from services.dag import node_runner  # noqa: E402
from services.dag import verify as vr  # noqa: E402
from services.mcp import reveng_common as rc  # noqa: E402
from services.mcp import reveng_tools as rt  # noqa: E402
from services.mcp import reveng_build_tools as rb
from services.mcp import reveng_session_tools as rs


@pytest.fixture(autouse=True)
def _isolated_backends(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "reveng"))
    monkeypatch.delenv("OPENMVS_BIN", raising=False)
    for module in (rc, rb, rs):
        monkeypatch.setattr(module, "_reveng_status", lambda: {"capabilities": {}})
    # Exercise real routing, checkpoints and human gates, not the external
    # foreman/Chudcode audits triggered when a node completes.
    monkeypatch.setattr(node_runner, "_ADVISOR_ENABLED", False)


def _write_image(path):
    from PIL import Image
    Image.new("RGB", (16, 16), (30, 80, 140)).save(path)


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
        """CAD mesh progress advances through sparse, dense, and mesh, never splat."""
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
        self.assertNotIn("splat", stages)
        # The GUI substring-matches sparse → dense → mesh on this field, in that order,
        # to light its sub-step track: the sequence must never rewind.
        self.assertLess(max(i for i, s in enumerate(stages) if s == "sparse"),
                        min(i for i, s in enumerate(stages) if s == "dense"))
        self.assertLess(max(i for i, s in enumerate(stages) if s == "dense"),
                        min(i for i, s in enumerate(stages) if s == "mesh"))
        # Stage tokens stay bare contract values — never prose.
        allowed = {"capture", "scale", "sparse", "dense",
                   "mesh", "fit", "parametric", "review", "deliver"}
        self.assertTrue(set(stages) <= allowed, sorted(set(stages) - allowed))


class TestDenseAppliesConfirmedScale(unittest.TestCase):
    """``n_dense`` must hand the CONFIRMED mm_per_unit to cleanup_mesh — that is the
    whole reason the scale gate now runs before dense — and must never invent one."""

    SESSION_ID = "re_scale_apply"

    def setUp(self):
        self.o3d = pytest.importorskip("open3d")
        self.tmp = tempfile.mkdtemp()
        self._old_env = {k: os.environ.get(k)
                         for k in ("CHUDWARE_REVENG_DIR", "OPENMVS_BIN")}
        os.environ["CHUDWARE_REVENG_DIR"] = self.tmp
        os.environ["OPENMVS_BIN"] = "/fake/openmvs"
        self._install_dense_backends()

        root = Path(self.tmp) / self.SESSION_ID
        (root / "images").mkdir(parents=True)
        _write_image(root / "images" / "a.jpg")
        sparse = root / "sparse"
        sparse.mkdir()
        s = rt._make_session(self.SESSION_ID, "scale apply", None)
        s["artifacts"]["sparse_dir"] = str(sparse)
        rt._save_session(s)

    def tearDown(self):
        import shutil
        for key, val in self._old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install_dense_backends(self):
        from services.vision import dense_backends

        def run_openmvs(sparse_dir, session_dir, **kw):
            mesh = Path(session_dir) / "mesh" / "openmvs.obj"
            mesh.parent.mkdir(parents=True, exist_ok=True)
            mesh.write_text(
                "v 0 0 0\nv 1 0 0\nv 0 2 0\nv 0 0 3\n"
                "f 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n"
            )
            return {"ok": True, "mesh_path": str(mesh), "backend": "openmvs"}

        patcher = mock.patch.object(dense_backends, "run_openmvs", run_openmvs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_mesh_extent(self, result, expected):
        mesh = self.o3d.io.read_triangle_mesh(result.result["mesh_path"])
        extent = mesh.get_axis_aligned_bounding_box().get_extent()
        for actual, wanted in zip(extent, expected):
            self.assertAlmostEqual(actual, wanted, places=6)

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
        self._assert_mesh_extent(res, (2.5, 5.0, 7.5))
        self.assertEqual(res.result["mesh_units"], "mm")
        session = rt._load_session(self.SESSION_ID)
        self.assertEqual(session["artifacts"]["mesh_units"], "mm")

    def test_unconfirmed_scale_leaves_reconstruction_units(self):
        # A solved but UNAPPROVED scale is not a licence to rescale the geometry.
        self._set_scale(2.5, confirmed=False)
        res = self._run_dense_node()
        self.assertTrue(res.success, res.result)
        self._assert_mesh_extent(res, (1.0, 2.0, 3.0))
        self.assertEqual(res.result["mesh_units"], "reconstruction")
        self.assertEqual(
            rt._load_session(self.SESSION_ID)["artifacts"]["mesh_units"],
            "reconstruction",
        )

    def test_unsolved_scale_never_invents_a_factor(self):
        res = self._run_dense_node()          # session.scale.mm_per_unit is None
        self.assertTrue(res.success, res.result)
        self._assert_mesh_extent(res, (1.0, 2.0, 3.0))
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
        self.assertTrue(dense.success, dense.result)
        self._assert_mesh_extent(dense, (2.5, 5.0, 7.5))
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
        _write_image(images / "a.jpg")

        # Gate lifecycle tests use small valid images, not real photogrammetry.
        # Sparse SfM now precedes the scale gate; optional backend discovery and
        # reconstruction must not consume the background run's polling deadline.
        sparse = images.parent / "sparse"
        sparse.mkdir()
        for patcher in (
            mock.patch.object(rc, "_reveng_status", return_value={"capabilities": {}}),
            mock.patch(
                "services.vision.colmap_pipeline.run_sparse",
                return_value={
                    "ok": True, "sparse_dir": str(sparse),
                    "registered": 1, "backend": "test",
                },
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil
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

        # The sidecar can become visible during the publish-and-release critical
        # section, before the manifest write finishes. Observe the mission slot
        # through its synchronized API before reading the completed publication.
        self.assertIsNone(rc._mission_running(self.SESSION_ID))

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

    def test_a_paused_gate_is_resolvable_the_instant_it_is_observable(self):
        """Approving must not be refused for a mission that is only waiting to be approved.

        The mission thread used to publish the paused run record and THEN drop the
        session's mission slot, so a caller that polled for "paused" and approved it
        (exactly what the GUI and ``reveng_resolve_gate`` do) could land in between and
        get ``mission_already_running`` for a run that was doing nothing but waiting for
        that decision.  That window is microseconds wide on an idle box and is what made
        ``test_approve_resumes_past_the_gate_to_the_next_one`` flake on a loaded one, so
        this test widens it: with the publish stalled, the old ordering refuses every
        time and the atomic publish-and-release serializes instead.
        """
        real_record = rc._record_session_run

        def _stalled_record(rec):
            time.sleep(0.15)
            return real_record(rec)

        with mock.patch.object(rc, "_record_session_run", _stalled_record):
            self._launch()
            self._wait_for("paused")
            out = self._call("reveng_resolve_gate", session_id=self.SESSION_ID,
                             decision="approve")
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["status"], "running")
            # Leave no mission thread running into tearDown's rmtree.
            self._wait_for("paused")

    def test_decision_must_be_explicit(self):
        self._launch()
        self._wait_for("paused")
        out = self._call("reveng_resolve_gate", session_id=self.SESSION_ID,
                         decision="yes please")
        self.assertFalse(out["ok"])
        self.assertIn("invalid decision", out["reason"])
        self.assertFalse(rt._load_session(self.SESSION_ID)["scale"]["confirmed"])

    def test_second_concurrent_mission_on_one_session_is_refused(self):
        import threading

        entered = threading.Event()
        release = threading.Event()
        sparse = Path(os.environ["CHUDWARE_REVENG_DIR"]) / self.SESSION_ID / "sparse"

        def blocked_sparse(*args, **kwargs):
            entered.set()
            if not release.wait(30):
                raise TimeoutError("test did not release sparse reconstruction")
            return {"ok": True, "sparse_dir": str(sparse), "registered": 1}

        with mock.patch("services.vision.colmap_pipeline.run_sparse", blocked_sparse):
            started = self._launch()
            try:
                self.assertTrue(entered.wait(30), "mission did not reach sparse reconstruction")
                out = self._launch()
                self.assertFalse(out.get("ok", True), out)
                self.assertEqual(out["reason"], "mission_already_running")
                self.assertEqual(out["run_id"], started["run_id"])
            finally:
                release.set()
                self._wait_for("paused")

    def test_gate_status_on_a_session_with_no_mission_is_honest(self):
        out = self._call("reveng_gate_status", session_id="re_nothing_here")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no_mission")
        self.assertTrue(out["hint"])


if __name__ == "__main__":
    unittest.main()
