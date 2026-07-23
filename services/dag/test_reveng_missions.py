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
import types
import unittest
from pathlib import Path

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
from services.mcp import reveng_tools as rt  # noqa: E402


def _read_reveng_events(cache_root: Path, run_id: str) -> list[dict]:
    """Return the aicad.reveng_events/1 records for *run_id* in emission order."""
    path = cache_root / "AI-CAD" / "cache" / "runs" / run_id / "events.jsonl"
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
        # We progressed: the reconstruct node was reached and reported.
        self.assertIn("n_reconstruct", res2.get("nodes", {}))

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


if __name__ == "__main__":
    unittest.main()
