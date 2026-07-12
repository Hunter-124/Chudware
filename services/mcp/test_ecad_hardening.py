"""MCP-layer hardening tests: heavy-tool budget guards + ECAD autosave integration (pytest).

These cover the transport-timeout survival work: a synchronous ecad_* tool that overruns the
client's per-call timeout gets the MCP server killed (-32001), and ECAD docs (Python, in-memory)
would be lost. The fixes are (1) a worker-thread + wall-clock budget on the heavy tools that returns
a clean "still running — simplify" payload instead of blocking, and (2) autosave/recovery so a kill
never loses open work.

Self-contained: tools are registered against a stub mcp (no FastMCP / stdio server needed).
Run:  python -m pytest mcp_server/test_ecad_hardening.py -q
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

import mcp_server.ecad_tools as et
from engine.ecad import autosave as eauto
from engine.ecad import session as esess
from mcp_server.ecad_tools import register_ecad_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools() -> dict:
    mcp = _StubMCP()
    register_ecad_tools(mcp)
    return mcp.tools


def _call(_tools: dict, _name: str, **kw) -> dict:
    # underscore-prefixed so a tool's own ``name=`` kwarg never collides
    return json.loads(_tools[_name](**kw))


# --------------------------------------------------------------------------- budget helper unit


def test_run_with_budget_done_error_timeout() -> None:
    assert et._run_with_budget(lambda: 7) == ("done", 7)

    st, val = et._run_with_budget(lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert st == "error" and isinstance(val, ValueError)

    t0 = time.time()
    st, val = et._run_with_budget(lambda: time.sleep(5) or "late", budget_s=1)
    dt = time.time() - t0
    assert st == "timeout" and val is None
    assert dt < 3, f"budget guard did not return promptly: {dt:.1f}s"


def test_budget_timeout_payload_shape() -> None:
    p = json.loads(et._budget_timeout_err(
        "autoroute", simplify="Use a coarser pitch.",
        next_actions=["ecad_autoroute(pitch=0.3)"], doc_id="abc"))
    assert p["ok"] is False
    assert p["status"] == "running" and p["auto_backgrounded"] is True
    assert p["doc_id"] == "abc"
    assert "autoroute" in p["error"] and "budget" in p["error"].lower()
    assert p["next_actions"] and p["hint"]


# --------------------------------------------------------------------------- heavy-tool fallback


def test_autoroute_overrun_returns_clean_message() -> None:
    """A router that runs past the budget must yield a clean 'still running' payload, NOT hang the
    call until the client kills the server."""
    from engine.ecad import rpc as ecad_rpc

    tools = _tools()
    _call(tools, "ecad_new_pcb", name="HeavyBoard")

    orig = ecad_rpc._ecad_autoroute
    orig_budget = et._ECAD_HEAVY_BUDGET_S
    et._ECAD_HEAVY_BUDGET_S = 1.0  # tiny budget for the test

    def _slow_autoroute(params, emit):  # noqa: ANN001
        time.sleep(5)
        return orig(params, emit)

    ecad_rpc._ecad_autoroute = _slow_autoroute
    try:
        t0 = time.time()
        p = _call(tools, "ecad_autoroute")
        dt = time.time() - t0
    finally:
        ecad_rpc._ecad_autoroute = orig
        et._ECAD_HEAVY_BUDGET_S = orig_budget

    assert dt < 3, f"autoroute hung past its budget ({dt:.1f}s) — would trip the transport timeout"
    assert p["ok"] is False and p.get("status") == "running"
    assert p.get("auto_backgrounded") is True
    assert "budget" in p["error"].lower()


def test_simulate_overrun_returns_clean_message() -> None:
    """ecad_simulate must budget-guard the solver call the same way."""
    import engine.sim.circuit as circ

    tools = _tools()
    # minimal sim-able schematic: a source + resistor to ground (recipe via ecad_design is heavy;
    # build by hand so the test is fast and deterministic).
    _call(tools, "ecad_new_schematic", name="SimBoard")
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=0,
          ref="V1", value="5")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=10, y=0, ref="R1", value="1k")
    _call(tools, "ecad_wire", from_ref="V1", from_pin="1", to_ref="R1", to_pin="1")
    _call(tools, "ecad_add_power", net="GND", x=0, y=-10)

    orig = circ.simulate
    orig_budget = et._ECAD_HEAVY_BUDGET_S
    et._ECAD_HEAVY_BUDGET_S = 1.0

    def _slow_sim(circuit, study, **kw):  # noqa: ANN001 — **kw: tool passes backend= (T-FIN14)
        time.sleep(5)
        return orig(circuit, study, **kw)

    circ.simulate = _slow_sim
    try:
        t0 = time.time()
        p = _call(tools, "ecad_simulate", analysis="op")
        dt = time.time() - t0
    finally:
        circ.simulate = orig
        et._ECAD_HEAVY_BUDGET_S = orig_budget

    assert dt < 3, f"ecad_simulate hung past its budget ({dt:.1f}s)"
    assert p["ok"] is False and p.get("status") == "running"
    assert "budget" in p["error"].lower()


# --------------------------------------------------------------------------- MCP autosave integ.


def test_mcp_autosave_and_recover() -> None:
    """Driving the MCP tools with autosave armed persists docs; the server-layer recovery wrapper
    reloads them and re-points the active-doc pointers."""
    d = tempfile.mkdtemp(prefix="ecad-mcp-autosave-")
    prev_env = os.environ.get("AI_CAD_ECAD_AUTOSAVE_DIR")
    os.environ["AI_CAD_ECAD_AUTOSAVE_DIR"] = d
    eauto.disable()
    eauto._STATE["session_id"] = None
    esess._REGISTRY.clear()
    et._ECAD["current_sch"] = et._ECAD["current_pcb"] = et._ECAD["current"] = None
    try:
        # arm autosave exactly like server._warm_core does, then build via the tools.
        et.enable_ecad_autosave()
        assert eauto.is_enabled()
        tools = _tools()
        r = _call(tools, "ecad_new_schematic", name="AutosaveMe")
        _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1", value="4k7")
        sch_id = r["doc_id"]
        assert (pathlib.Path(d) / f"{sch_id}.json").exists(), "tool mutation did not autosave"

        # simulate a restart: a fresh process session id + cleared registry, then recover.
        et._ECAD_SESSION_ID = "RESTARTED"
        eauto.enable("RESTARTED")
        esess._REGISTRY.clear()
        et._ECAD["current_sch"] = et._ECAD["current"] = None

        recovered = et.recover_ecad_autosaved()
        assert any(rec["original_doc_id"] == sch_id for rec in recovered), recovered
        # the active-schematic pointer is restored, so a default-doc tool call works again.
        assert et._ECAD["current_sch"] is not None
        status = _call(tools, "ecad_status")
        assert status["ok"]
        assert any(c.get("ref") == "R1" for c in status.get("components", [])), status
    finally:
        # hermetic teardown: never leave autosave armed for other suites in this process.
        eauto.disable()
        eauto._STATE["session_id"] = None
        if prev_env is None:
            os.environ.pop("AI_CAD_ECAD_AUTOSAVE_DIR", None)
        else:
            os.environ["AI_CAD_ECAD_AUTOSAVE_DIR"] = prev_env
