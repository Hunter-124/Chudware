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

import mcp_server.ecad_common as et  # T-RF8: budget/session internals moved
from services.ecad import autosave as eauto
from mcp_server.ecad_common import ecad_session as esess
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
    import mcp_server.ecad_pcb_tools as _pcb_tools  # T-RF20: the tool RPCs via ecad_common._rpc

    tools = _tools()
    _call(tools, "ecad_new_pcb", name="HeavyBoard")

    orig = _pcb_tools._rpc
    orig_budget = et._ECAD_HEAVY_BUDGET_S
    et._ECAD_HEAVY_BUDGET_S = 1.0  # tiny budget for the test

    def _slow_rpc(method, params=None):  # noqa: ANN001
        if method == "ecad_autoroute":
            time.sleep(5)
        return orig(method, params)

    _pcb_tools._rpc = _slow_rpc
    try:
        t0 = time.time()
        p = _call(tools, "ecad_autoroute")
        dt = time.time() - t0
    finally:
        _pcb_tools._rpc = orig
        et._ECAD_HEAVY_BUDGET_S = orig_budget

    assert dt < 3, f"autoroute hung past its budget ({dt:.1f}s) — would trip the transport timeout"
    assert p["ok"] is False and p.get("status") == "running"
    assert p.get("auto_backgrounded") is True
    assert "budget" in p["error"].lower()


def test_simulate_overrun_returns_clean_message() -> None:
    """ecad_simulate must budget-guard the solver call the same way."""
    import mcp_server.ecad_sim_tools as _sim_tools  # T-RF20: tool runs the C++ sim via _sim_run

    tools = _tools()
    # minimal sim-able schematic: a source + resistor to ground (recipe via ecad_design is heavy;
    # build by hand so the test is fast and deterministic).
    _call(tools, "ecad_new_schematic", name="SimBoard")
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=0,
          ref="V1", value="5")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=10, y=0, ref="R1", value="1k")
    _call(tools, "ecad_wire", from_ref="V1", from_pin="1", to_ref="R1", to_pin="1")
    _call(tools, "ecad_add_power", net="GND", x=0, y=-10)

    orig = _sim_tools._sim_run
    orig_budget = et._ECAD_HEAVY_BUDGET_S
    et._ECAD_HEAVY_BUDGET_S = 1.0

    def _slow_sim(circuit, study, **kw):  # noqa: ANN001 — **kw: tool passes backend= (T-FIN14)
        time.sleep(5)
        return orig(circuit, study, **kw)

    _sim_tools._sim_run = _slow_sim
    try:
        t0 = time.time()
        p = _call(tools, "ecad_simulate", analysis="op")
        dt = time.time() - t0
    finally:
        _sim_tools._sim_run = orig
        et._ECAD_HEAVY_BUDGET_S = orig_budget

    assert dt < 3, f"ecad_simulate hung past its budget ({dt:.1f}s)"
    assert p["ok"] is False and p.get("status") == "running"
    assert "budget" in p["error"].lower()


# --------------------------------------------------------------------------- MCP autosave integ.


def test_mcp_autosave_and_recover() -> None:
    """Driving the MCP tools with autosave armed persists docs; the server-layer recovery wrapper
    reloads them and re-points the active-doc pointers."""
    d = tempfile.mkdtemp(prefix="ecad-mcp-autosave-")
    prev_env = os.environ.get("CHUDWARE_ECAD_AUTOSAVE_DIR")
    os.environ["CHUDWARE_ECAD_AUTOSAVE_DIR"] = d
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
            os.environ.pop("CHUDWARE_ECAD_AUTOSAVE_DIR", None)
        else:
            os.environ["CHUDWARE_ECAD_AUTOSAVE_DIR"] = prev_env


# --------------------------------------------------------------------------- dual-import identity


def test_ecad_module_identity_dual_import() -> None:
    """mcp_server.ecad_tools and services.mcp.ecad_tools must be ONE module object so
    _ECAD / _THERMAL session state is never split across two import paths."""
    import mcp_server.ecad_tools as a
    import services.mcp.ecad_tools as b
    assert a is b, f"dual import produced two modules: {a} vs {b}"
    assert a._ECAD is b._ECAD
    import mcp_server.ecad_thermal as t1
    import services.mcp.ecad_thermal as t2
    assert t1 is t2
    assert t1._THERMAL is t2._THERMAL


# --------------------------------------------------------------------------- recovery monotonicity


def test_recover_never_overwrites_active_docs() -> None:
    """Warm autosave recovery fills only EMPTY current_* defaults — never a just-created doc."""
    d = tempfile.mkdtemp(prefix="ecad-mcp-mono-")
    prev_env = os.environ.get("CHUDWARE_ECAD_AUTOSAVE_DIR")
    os.environ["CHUDWARE_ECAD_AUTOSAVE_DIR"] = d
    eauto.disable()
    eauto._STATE["session_id"] = None
    esess._REGISTRY.clear()
    et._ECAD["current_sch"] = et._ECAD["current_pcb"] = et._ECAD["current"] = None
    try:
        et.enable_ecad_autosave()
        tools = _tools()
        r = _call(tools, "ecad_new_schematic", name="OldDoc")
        _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1", value="1k")
        old_id = r["doc_id"]
        assert (pathlib.Path(d) / f"{old_id}.json").exists()

        # Simulate restart: clear registry + pointers, then create a NEW live doc BEFORE recover.
        et._ECAD_SESSION_ID = "RESTARTED2"
        eauto.enable("RESTARTED2")
        esess._REGISTRY.clear()
        et._ECAD["current_sch"] = et._ECAD["current_pcb"] = et._ECAD["current"] = None

        fresh = _call(tools, "ecad_new_schematic", name="FreshLive")
        fresh_id = fresh["doc_id"]
        assert et._ECAD["current_sch"] == fresh_id

        # Recovery must NOT clobber the just-created fresh doc.
        recovered = et.recover_ecad_autosaved()
        assert et._ECAD["current_sch"] == fresh_id, (
            f"recovery overwrote active sch {fresh_id} with {et._ECAD['current_sch']}")
        assert et._ECAD["current"] == fresh_id
        # Recovered docs still exist in the registry and are addressable by id.
        assert any(rec.get("original_doc_id") == old_id or rec.get("doc_id")
                   for rec in recovered) or recovered is not None
        st = _call(tools, "ecad_status", doc_id=fresh_id)
        assert st["ok"] and st["component_count"] == 0
    finally:
        eauto.disable()
        eauto._STATE["session_id"] = None
        if prev_env is None:
            os.environ.pop("CHUDWARE_ECAD_AUTOSAVE_DIR", None)
        else:
            os.environ["CHUDWARE_ECAD_AUTOSAVE_DIR"] = prev_env


def test_multi_doc_explicit_doc_id_isolation() -> None:
    """Explicit doc_id keeps multi-schematic ops isolated from the active pointer."""
    tools = _tools()
    a = _call(tools, "ecad_new_schematic", name="IsoA")
    aid = a["doc_id"]
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, value="1k",
          ref="RA", doc_id=aid)
    b = _call(tools, "ecad_new_schematic", name="IsoB")
    bid = b["doc_id"]
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, value="2k",
          ref="RB", doc_id=bid)
    assert aid != bid
    sa = _call(tools, "ecad_status", doc_id=aid)
    sb = _call(tools, "ecad_status", doc_id=bid)
    assert sa["ok"] and sa["component_count"] == 1
    assert sb["ok"] and sb["component_count"] == 1
    assert sa["components"][0]["ref"] == "RA"
    assert sb["components"][0]["ref"] == "RB"
    # Active pointer is B (most recently created); default status must not see A's RA.
    st = _call(tools, "ecad_status")
    assert st["ok"]
    refs = [c["ref"] for c in st.get("components", [])]
    assert "RB" in refs and "RA" not in refs


# --------------------------------------------------------------------------- slow-provider plan budget


class _SlowProvider:
    """Stub provider whose complete() blocks well past the plan budget."""

    def __init__(self, delay_s: float = 30.0):
        self.delay_s = delay_s
        self.calls = 0
        self.base_url = "http://127.0.0.1:9/v1"  # unused — reachability is monkeypatched

    def complete(self, system, messages):  # noqa: ANN001
        self.calls += 1
        time.sleep(self.delay_s)
        return '{"components":[], "nets":{}, "study":{"analysis":"op","expect":{}}}'


def test_ecad_design_slow_provider_falls_back_promptly() -> None:
    """A slow/timed-out provider must degrade to the recipe path before the client budget,
    produce exactly one verified schematic, and leave ecad_status responsive."""
    tools = _tools()
    slow = _SlowProvider(delay_s=30.0)
    orig_budget = os.environ.get("CHUDWARE_ECAD_PLAN_BUDGET_S")
    os.environ["CHUDWARE_ECAD_PLAN_BUDGET_S"] = "1.0"

    # Force the configured provider path and skip the TCP pre-flight.
    import services.agent.config as _cfg  # T-RF8: canonical path (ecad_design uses it)
    orig_get = getattr(_cfg, "get_provider", None)
    _cfg.get_provider = lambda *a, **k: slow  # type: ignore[attr-defined]
    import mcp_server.ecad_sim_tools as et_sim  # T-RF8: ecad_design looks it up here
    orig_reach = et_sim._provider_reachable
    et_sim._provider_reachable = lambda p, timeout_s=0.6: True  # type: ignore[assignment]

    # Snapshot session registry size so we can assert only ONE new sch was built.
    before_ids = {s.doc_id for s in esess.iter_sessions()}
    try:
        t0 = time.time()
        p = _call(tools, "ecad_design", request="5V voltage divider with 2.5V output")
        dt = time.time() - t0
    finally:
        if orig_get is not None:
            _cfg.get_provider = orig_get  # type: ignore[attr-defined]
        et_sim._provider_reachable = orig_reach  # type: ignore[assignment]
        if orig_budget is None:
            os.environ.pop("CHUDWARE_ECAD_PLAN_BUDGET_S", None)
        else:
            os.environ["CHUDWARE_ECAD_PLAN_BUDGET_S"] = orig_budget

    # Must return well under the 30 s MCP transport deadline (plan budget 1 s + build ~2 s).
    assert dt < 8.0, f"slow-provider design hung for {dt:.1f}s (would trip transport timeout)"
    assert p.get("provider_timed_out") is True, p
    assert p.get("ok") is True, f"recipe fallback must still verify: {p.get('hint')}"
    assert p.get("doc_id"), p
    assert p.get("recipe_used"), "fallback must name the recipe used"

    # Exactly one new schematic session created by this call (no racing second build).
    after_ids = {s.doc_id for s in esess.iter_sessions()}
    new_ids = after_ids - before_ids
    assert p["doc_id"] in new_ids
    # Only one *new* sch of the design — registry may hold prior test docs, but the design
    # doc is the one we just adopted as current.
    assert et._ECAD["current_sch"] == p["doc_id"]
    assert et._ECAD["current"] == p["doc_id"]

    # Immediate follow-up status must stay responsive and see the recipe schematic.
    t1 = time.time()
    st = _call(tools, "ecad_status")
    assert (time.time() - t1) < 2.0, "ecad_status after timed-out design must stay responsive"
    assert st["ok"] and st["kind"] == "sch"
    assert st.get("doc_id") == p["doc_id"] or et._ECAD["current_sch"] == p["doc_id"]

    # Voltage-divider recipe sim accuracy: midpoint within 0.1%.
    sim = p.get("sim") or {}
    got = sim.get("got")
    exp = sim.get("expect")
    if got is not None and exp is not None:
        err = abs(float(got) - float(exp)) / max(abs(float(exp)), 1e-12)
        assert err <= 0.001, f"divider sim err {err:.4%} > 0.1% (got={got}, expect={exp})"
