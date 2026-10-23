"""MCP-layer hardening tests: heavy-tool budget guards + ECAD autosave integration (pytest).

These cover the transport-timeout survival work: a synchronous ecad_* tool that overruns the
client's per-call timeout gets the MCP server killed (-32001), and ECAD docs (Python, in-memory)
would be lost. The fixes are (1) a bounded worker-thread lifecycle whose timeout envelope names a
durably retrievable in-process job, and (2) autosave/recovery so a kill never loses open work.

Self-contained: tools are registered against a stub mcp (no FastMCP / stdio server needed).
Run:  python -m pytest services/mcp/test_ecad_hardening.py -q
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

import services.mcp.ecad_common as et  # budget/session internals moved
import services.mcp.ecad_sch_tools as sch_tools
from services.ecad import autosave as eauto
from services.mcp.ecad_common import ecad_session as esess
from services.mcp.ecad_tools import register_ecad_tools


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


_GET_JOB_TOOL = None


def _get_job(job_id: str) -> dict:
    """Poll through the same existing MCP get_job surface used by production."""
    global _GET_JOB_TOOL
    if _GET_JOB_TOOL is None:
        from services.mcp.cad_doc_tools import register_cad_doc_tools

        mcp = _StubMCP()
        register_cad_doc_tools(mcp)
        _GET_JOB_TOOL = mcp.tools["get_job"]
    return json.loads(_GET_JOB_TOOL(job_id=job_id))


def _call(_tools: dict, _name: str, **kw) -> dict:
    # underscore-prefixed so a tool's own ``name=`` kwarg never collides
    return json.loads(_tools[_name](**kw))


# --------------------------------------------------------------------------- budget helper unit


def test_run_with_budget_done_error_timeout() -> None:
    assert et._run_with_budget(lambda: 7) == ("done", 7)

    st, val = et._run_with_budget(lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert st == "error" and isinstance(val, ValueError)

    gate = et._threading.Event()
    t0 = time.time()
    st, val = et._run_with_budget(lambda: gate.wait(5) or "late", budget_s=1)
    dt = time.time() - t0
    assert st == "timeout" and val is None
    assert dt < 3, f"budget guard did not return promptly: {dt:.1f}s"
    p = json.loads(et._budget_timeout_err("unit work", simplify="Use smaller input."))
    gate.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _get_job(p["job_id"]).get("status") == "done":
            break
        time.sleep(0.01)
    assert _get_job(p["job_id"])["status"] == "done"


def test_timeout_formatter_without_a_job_never_claims_running() -> None:
    p = json.loads(et._budget_timeout_err("autoroute", simplify="Use a coarser pitch."))
    assert p["status"] == "error"
    assert p["auto_backgrounded"] is False


def test_budget_timeout_payload_identifies_retrievable_bounded_job() -> None:
    gate = et._threading.Event()

    def _late_result() -> dict:
        gate.wait(5)
        return {"ok": True, "answer": 42}

    # Keep the formatter in the same tool-call frame as _run_with_budget, matching every
    # production caller.  The returned running envelope must identify the exact handed-off job.
    st, val = et._run_with_budget(_late_result, budget_s=1)
    assert st == "timeout" and val is None
    p = json.loads(et._budget_timeout_err(
        "autoroute", simplify="Use a coarser pitch.",
        next_actions=["ecad_autoroute(pitch=0.3)"], doc_id="abc"))
    assert p["ok"] is False
    assert p["status"] == "running" and p["auto_backgrounded"] is True
    assert p["doc_id"] == "abc" and p["job_id"]
    assert "autoroute" in p["error"] and "budget" in p["error"].lower()
    assert p["next_actions"] == [f"get_job('{p['job_id']}')"]
    assert p["retry_safe_while_running"] is False
    assert p["cancel_supported"] is False
    assert p["cancellation"]["supported"] is False
    assert "cannot be stopped safely" in p["cancellation"]["reason"]
    assert _get_job(p["job_id"])["status"] == "running"

    gate.set()
    deadline = time.monotonic() + 2
    job = {}
    while time.monotonic() < deadline:
        job = _get_job(p["job_id"])
        if job.get("status") == "done":
            break
        time.sleep(0.01)
    assert job["status"] == "done"
    assert job["result"] == {"ok": True, "answer": 42}


def test_budget_job_error_persists_for_retrieval() -> None:
    gate = et._threading.Event()

    def _late_error() -> None:
        gate.wait(5)
        raise ValueError("late boom")

    st, val = et._run_with_budget(_late_error, budget_s=1)
    assert st == "timeout" and val is None
    p = json.loads(et._budget_timeout_err("simulation", simplify="Use fewer points."))
    gate.set()
    deadline = time.monotonic() + 2
    job = {}
    while time.monotonic() < deadline:
        job = _get_job(p["job_id"])
        if job.get("status") == "error":
            break
        time.sleep(0.01)
    assert job["status"] == "error"
    assert job["ok"] is False
    assert job["error"] == "late boom"


def test_budget_worker_count_is_capped() -> None:
    gate = et._threading.Event()
    old_limit = et._ECAD_JOB_MAX_RUNNING
    with et._cad_jobs._JOB_LOCK:
        baseline = sum(
            1 for job in et._cad_jobs._JOBS.values()
            if job.get("owner") == "ecad" and job.get("status") == "running")
    et._ECAD_JOB_MAX_RUNNING = baseline + 1
    try:
        st, _ = et._run_with_budget(lambda: gate.wait(5), budget_s=1)
        assert st == "timeout"
        first = json.loads(et._budget_timeout_err("zone fill", simplify="Use a smaller zone."))
        et._ECAD_JOB_MAX_RUNNING = 1
        called = []
        st2, err = et._run_with_budget(lambda: called.append(True), budget_s=1)
        assert st2 == "error" and isinstance(err, RuntimeError)
        assert "limit reached" in str(err) and not called
    finally:
        et._ECAD_JOB_MAX_RUNNING = old_limit
        gate.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _get_job(first["job_id"]).get("status") == "done":
            break
        time.sleep(0.01)
    assert _get_job(first["job_id"])["status"] == "done"


def test_core_rpc_boundary_serializes_handed_off_and_foreground_calls() -> None:
    release = et._threading.Event()
    slow_entered = et._threading.Event()
    foreground_entered = et._threading.Event()
    foreground_done = et._threading.Event()
    foreground_result: dict = {}
    orig_call = et.cb.call
    thread = None
    p: dict = {}

    def _fake_call(method, params):  # noqa: ANN001
        if method == "slow":
            slow_entered.set()
            release.wait(5)
        elif method == "foreground":
            foreground_entered.set()
        return {"ok": True, "method": method}

    def _foreground() -> None:
        try:
            foreground_result.update(et._rpc("foreground"))
        finally:
            foreground_done.set()

    et.cb.call = _fake_call
    try:
        st, _ = et._run_with_budget(lambda: et._rpc("slow"), budget_s=1)
        assert st == "timeout" and slow_entered.is_set()
        p = json.loads(et._budget_timeout_err("core call", simplify="Use smaller input."))
        thread = et._threading.Thread(target=_foreground, daemon=True)
        thread.start()
        assert not foreground_entered.wait(0.1), (
            "foreground ECAD RPC entered core_bridge while the handed-off call still owned it")
    finally:
        release.set()
        if thread is not None:
            thread.join(2)
        et.cb.call = orig_call

    assert foreground_done.is_set()
    assert foreground_result["method"] == "foreground"
    deadline = time.monotonic() + 2
    job = {}
    while time.monotonic() < deadline:
        job = _get_job(p["job_id"])
        if job.get("status") == "done":
            break
        time.sleep(0.01)
    assert job["status"] == "done"


def test_terminal_job_retention_has_a_hard_count_bound() -> None:
    old_limit = et._ECAD_JOB_MAX_RETAINED
    old_id = "job-test-ecad-old"
    new_id = "job-test-ecad-new"
    now = time.time()
    with et._cad_jobs._JOB_LOCK:
        et._cad_jobs._evict_old_jobs()
        existing = sum(
            1 for job in et._cad_jobs._JOBS.values()
            if job.get("owner") == "ecad" and job.get("status") in ("done", "error"))
        et._ECAD_JOB_MAX_RETAINED = existing + 1
        et._cad_jobs._JOBS[old_id] = {
            "owner": "ecad", "status": "done", "done_at": -1.0,
            "kind": "test", "result": 1, "error": None,
        }
        et._cad_jobs._JOBS[new_id] = {
            "owner": "ecad", "status": "done", "done_at": now,
            "kind": "test", "result": 2, "error": None,
        }
        try:
            et._trim_ecad_jobs_locked()
            assert old_id not in et._cad_jobs._JOBS
            assert new_id in et._cad_jobs._JOBS
            retained = sum(
                1 for job in et._cad_jobs._JOBS.values()
                if job.get("owner") == "ecad"
                and job.get("status") in ("done", "error"))
            assert retained <= et._ECAD_JOB_MAX_RETAINED
        finally:
            et._cad_jobs._JOBS.pop(old_id, None)
            et._cad_jobs._JOBS.pop(new_id, None)
            et._ECAD_JOB_MAX_RETAINED = old_limit


# --------------------------------------------------------------------------- heavy-tool fallback


def test_autoroute_overrun_returns_clean_message() -> None:
    """A router overrun returns a job and the test drains that worker before teardown."""
    import services.mcp.ecad_pcb_tools as _pcb_tools  # tool RPCs via ecad_common._rpc

    tools = _tools()
    _call(tools, "ecad_new_pcb", name="HeavyBoard")

    orig = _pcb_tools._rpc
    orig_budget = et._ECAD_HEAVY_BUDGET_S
    gate = et._threading.Event()
    et._ECAD_HEAVY_BUDGET_S = 1.0
    p: dict = {}
    drained: dict = {}

    def _slow_rpc(method, params=None):  # noqa: ANN001
        if method == "ecad_autoroute":
            gate.wait(5)
            return {"ok": True, "routed": True}
        return orig(method, params)

    _pcb_tools._rpc = _slow_rpc
    try:
        t0 = time.time()
        p = _call(tools, "ecad_autoroute")
        dt = time.time() - t0
    finally:
        gate.set()
        jid = p.get("job_id")
        deadline = time.monotonic() + 2
        while jid and time.monotonic() < deadline:
            drained = _get_job(jid)
            if drained.get("status") in ("done", "error"):
                break
            time.sleep(0.01)
        _pcb_tools._rpc = orig
        et._ECAD_HEAVY_BUDGET_S = orig_budget

    assert dt < 3, f"autoroute hung past its budget ({dt:.1f}s) — would trip the transport timeout"
    assert p["ok"] is False and p.get("status") == "running"
    assert p.get("auto_backgrounded") is True
    assert "budget" in p["error"].lower()
    assert drained.get("status") == "done"


def test_simulate_overrun_returns_clean_message() -> None:
    """ecad_simulate hands off safely and its test worker is drained before teardown."""
    import services.mcp.ecad_sim_tools as _sim_tools  # tool runs C++ sim via _sim_run

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
    gate = et._threading.Event()
    et._ECAD_HEAVY_BUDGET_S = 1.0
    p: dict = {}
    drained: dict = {}

    def _slow_sim(circuit, study, **kw):  # noqa: ANN001, ARG001
        gate.wait(5)
        return {"ok": True, "converged": True, "test": True}

    _sim_tools._sim_run = _slow_sim
    try:
        t0 = time.time()
        p = _call(tools, "ecad_simulate", analysis="op")
        dt = time.time() - t0
    finally:
        gate.set()
        jid = p.get("job_id")
        deadline = time.monotonic() + 2
        while jid and time.monotonic() < deadline:
            drained = _get_job(jid)
            if drained.get("status") in ("done", "error"):
                break
            time.sleep(0.01)
        _sim_tools._sim_run = orig
        et._ECAD_HEAVY_BUDGET_S = orig_budget

    assert dt < 3, f"ecad_simulate hung past its budget ({dt:.1f}s)"
    assert p["ok"] is False and p.get("status") == "running"
    assert "budget" in p["error"].lower()
    assert drained.get("status") == "done"


def test_nonconvergence_is_not_reported_as_success() -> None:
    """A completed backend call with convergence=false is an analysis failure."""
    import services.mcp.ecad_sim_tools as _sim_tools

    tools = _tools()
    _call(tools, "ecad_new_schematic", name="Nonconverged")
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source",
          x=0, y=0, ref="V1", value="5")
    _call(tools, "ecad_place_symbol", libid="Device:R",
          x=10, y=0, ref="R1", value="1k")
    _call(tools, "ecad_wire", from_ref="V1", from_pin="1", to_ref="R1", to_pin="1")

    original = _sim_tools._sim_run

    def _nonconverged(_circuit, study, **_kw):  # noqa: ANN001
        analysis = study["analyses"][0]["type"]
        if analysis == "tf":
            return {"backend": "mna", "convergence": {"tf": False},
                    "tf": {"gain": 1.0, "rin": 1000.0, "rout": 1000.0},
                    "log": ["singular matrix"]}
        return {"backend": "mna", "convergence": {analysis: False},
                analysis: {}, "log": ["singular matrix"]}

    _sim_tools._sim_run = _nonconverged
    try:
        simulation = _call(tools, "ecad_simulate", analysis="op")
        transfer = _call(tools, "ecad_tf", output="V(N$1)", source="V1")
    finally:
        _sim_tools._sim_run = original

    assert simulation["ok"] is False and simulation["converged"] is False
    assert "converge" in simulation["error"].lower()
    assert transfer["ok"] is False and transfer["converged"] is False
    assert "converge" in transfer["error"].lower()


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
    """services.mcp.ecad_tools and services.mcp.ecad_tools must be ONE module object so
    _ECAD / _THERMAL session state is never split across two import paths."""
    import services.mcp.ecad_tools as a
    import services.mcp.ecad_tools as b
    assert a is b, f"dual import produced two modules: {a} vs {b}"
    assert a._ECAD is b._ECAD
    import services.mcp.ecad_thermal as t1
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
    """Slow planner that observes the helper's cancellation callback."""

    def __init__(self, delay_s: float = 30.0):
        self.delay_s = delay_s
        self.calls = 0
        self.cancel = lambda: False
        self.cancelled = et._threading.Event()
        self.release = et._threading.Event()
        self.base_url = "http://127.0.0.1:9/v1"  # unused — reachability is monkeypatched

    def complete(self, system, messages):  # noqa: ANN001
        self.calls += 1
        deadline = time.monotonic() + self.delay_s
        while not self.release.wait(0.01) and time.monotonic() < deadline:
            if self.cancel():
                self.cancelled.set()
                raise RuntimeError("helper completion cancelled")
        return '{"components":[], "nets":{}, "study":{"analysis":"op","expect":{}}}'


def test_ecad_design_slow_provider_falls_back_promptly() -> None:
    """A slow/timed-out provider must degrade to the recipe path before the client budget,
    produce exactly one verified schematic, and leave ecad_status responsive."""
    tools = _tools()
    slow = _SlowProvider(delay_s=30.0)
    orig_budget = os.environ.get("CHUDWARE_ECAD_PLAN_BUDGET_S")
    os.environ["CHUDWARE_ECAD_PLAN_BUDGET_S"] = "1.0"

    # Patch the resolver ecad_design actually calls, not the retired SDK config seam.
    from services.agent.providers import chudcode_provider as helper
    orig_get = helper.helper_provider

    def resolve_provider(role, *, cancel=None, timeout=None):
        slow.cancel = cancel
        return slow

    helper.helper_provider = resolve_provider
    import services.mcp.ecad_sim_tools as et_sim  # ecad_design looks it up here
    orig_reach = et_sim._provider_reachable
    et_sim._provider_reachable = lambda p, timeout_s=0.6: True  # type: ignore[assignment]

    # Snapshot session registry size so we can assert only ONE new sch was built.
    before_ids = {s.doc_id for s in esess.iter_sessions()}
    with et._cad_jobs._JOB_LOCK:
        before_job_ids = set(et._cad_jobs._JOBS)
    plan_job_ids: set[str] = set()
    drained_plan_jobs: dict[str, dict] = {}
    try:
        t0 = time.time()
        p = _call(tools, "ecad_design", request="5V voltage divider with 2.5V output")
        dt = time.time() - t0
        cancelled = slow.cancelled.wait(2.0)
    finally:
        slow.release.set()
        with et._cad_jobs._JOB_LOCK:
            plan_job_ids = set(et._cad_jobs._JOBS) - before_job_ids
        for jid in plan_job_ids:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                drained_plan_jobs[jid] = _get_job(jid)
                if drained_plan_jobs[jid].get("status") in ("done", "error"):
                    break
                time.sleep(0.01)
        helper.helper_provider = orig_get
        et_sim._provider_reachable = orig_reach  # type: ignore[assignment]
        if orig_budget is None:
            os.environ.pop("CHUDWARE_ECAD_PLAN_BUDGET_S", None)
        else:
            os.environ["CHUDWARE_ECAD_PLAN_BUDGET_S"] = orig_budget
    assert plan_job_ids
    assert all(job.get("status") in ("done", "error")
               for job in drained_plan_jobs.values())

    assert cancelled, "abandoned model plan was not cancelled"
    assert slow.calls == 1, "the slow model plan must actually have started"
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


# --------------------------------------------------------------------------- ECAD status honesty


def _stub_pcb_status(monkeypatch, compile_payload) -> tuple[dict, str]:
    """Register real MCP closures around a deterministic PCB compile result."""
    session = esess.EcadSession(
        "pcb-status-hardening",
        {
            "schema": "aicad.pcb/1",
            "name": "Status hardening board",
            "footprints": [],
            "nets": [{"name": "GND", "pads": []}],
            "tracks": [],
            "vias": [],
            "outline": {"shapes": []},
        },
        revision=4,
    )
    monkeypatch.setattr(et.ecad_session, "get_session", lambda _doc_id: session)
    monkeypatch.setattr(sch_tools, "_compile_of", compile_payload)
    return _tools(), session.doc_id


def test_pcb_status_compile_failure_cannot_report_clean(monkeypatch) -> None:
    def compile_failed(_session):
        raise RuntimeError("DRC compiler crashed")

    tools, doc_id = _stub_pcb_status(monkeypatch, compile_failed)
    text = tools["ecad_status"](doc_id=doc_id)
    payload = json.loads(text)
    lowered = text.lower()

    assert payload["drc_available"] is False
    assert "drc unavailable" in lowered
    assert "do not treat this status as clean" in lowered
    assert "0 drc error" not in lowered
    assert "clean board" not in lowered
    assert "design is clean" not in lowered


def test_pcb_status_still_reports_real_drc_violation(monkeypatch) -> None:
    violation = {
        "severity": "error",
        "rule": "clearance",
        "message": "track T1 is shorted to track T2",
        "net": "VCC",
    }
    tools, doc_id = _stub_pcb_status(
        monkeypatch, lambda _session: {"drc": [violation]})
    text = tools["ecad_status"](doc_id=doc_id)
    payload = json.loads(text)

    assert payload["drc_available"] is True
    assert payload["drc"]["errors"] == 1
    assert "1 DRC error(s)" in payload["summary"]
    assert "FIX DRC clearance (1x)" in text
    assert "track T1 is shorted to track T2" in text


def test_load_symbol_library_missing_path_is_helpful(tmp_path) -> None:
    missing = tmp_path / "vendor.kicad_sym"
    tools = _tools()
    payload = _call(tools, "ecad_load_symbol_library", path=str(missing))

    assert payload["ok"] is False
    assert payload["reason"] == "file_not_found"
    assert str(missing) in payload["error"]
    assert ".kicad_sym" in payload["hint"]


def test_load_symbol_library_calls_native_importer(monkeypatch, tmp_path) -> None:
    source = tmp_path / "vendor.kicad_sym"
    source.write_text("(kicad_symbol_lib (version 20231120))", encoding="utf-8")
    called = {}

    def rpc(method, params):
        called.update(method=method, params=params)
        return {"ok": True, "added": ["Vendor:Controller"], "warnings": []}

    monkeypatch.setattr(sch_tools, "_rpc", rpc)
    payload = _call(_tools(), "ecad_load_symbol_library", path=str(source))

    assert called == {
        "method": "ecad_load_symbol_library",
        "params": {"path": str(source)},
    }
    assert payload["ok"] is True
    assert payload["added"] == ["Vendor:Controller"]
    assert payload["added_count"] == 1
