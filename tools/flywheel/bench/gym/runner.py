"""Design-gym runner (T-W27).

Runs each challenge through the REAL ``run_mission`` pipeline (services/agent/loop.py)
per seed, on the configured tier(s), and:

  1. injects a fixture ``intake_fn`` (the challenge's §16 manifest — deterministic; no
     model call, no fabrication of the mission's definition of done),
  2. replays the challenge's scripted per-deliverable turns into the SAME ledger the
     mission uses, via ``ledger.append`` (generic, verbatim event write) — so the ledger
     the classifier reads is genuine event data, not the pipeline's structural PASS floor,
  3. runs run_mission with ``execute_dag`` gated on model availability (see
     ``execution_probe``): with no real tool-executing agent + no provisioned model + no
     core, the mission's DAG would only stub, so cycle-1 MOCK mode runs the phase machine
     to PLAN and relies on the injected ledger turns for the gradeable signal — this is
     labeled MOCK in the run record and the report,
  4. GRADES each challenge's acceptance criteria ITSELF against the produced ledger
     (challenge.grade — independent of the pipeline verdict),
  5. CLASSIFIES failures by taxonomy over the ledger, evidence-ref'd.

Every run writes a replayable run dir under ``reports/runs/<cycle>/<challenge>_<tier>_<seed>/``
containing ledger.json, graded.json, classifications.json, and mission.json.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .challenges import (
    Challenge, load_challenges, assert_division_eyes_allowed,
    tools_for_division, VISION_TOOLS,
)
from .taxonomy import classify_ledger

_HERE = Path(__file__).resolve().parent
REPORTS_DIR = _HERE / "reports"


# ---------------------------------------------------------------------------
# Execution-reality probe — decides real vs mock, honestly, from the environment.
# ---------------------------------------------------------------------------

@dataclass
class ExecProbe:
    core_available: bool
    frontier_key: bool
    local_model: bool
    real_agent_fn: bool
    # T-DP19: is the DAG executor's checkpoint dependency importable in THIS
    # interpreter? run_mission's PLAN phase imports services.dag.executor, whose
    # top-level `from langgraph.checkpoint.sqlite import SqliteSaver` raises under an
    # interpreter that lacks it (system python3). Without this gate the probe reports
    # "real", the mission dies at PLAN, and every cell ties at win_rate 0.00 — a
    # misleading signal that looks like a model-quality result. Default True so
    # externally-constructed probes keep their old meaning.
    dag_deps: bool = True

    @property
    def can_run_real(self) -> bool:
        # A genuine model-in-the-loop cycle needs a tool-executing agent, a model, AND
        # the DAG deps in-process (execute_dag=True imports services.dag.executor).
        return self.real_agent_fn and self.dag_deps and (self.frontier_key or self.local_model)

    @property
    def mode(self) -> str:
        return "real" if self.can_run_real else "mock"

    def to_dict(self) -> dict[str, Any]:
        return {"core_available": self.core_available, "frontier_key": self.frontier_key,
                "local_model": self.local_model, "real_agent_fn": self.real_agent_fn,
                "dag_deps": self.dag_deps, "mode": self.mode}


def execution_probe() -> ExecProbe:
    """Probe the environment for real-execution capability — NO side effects.

    Crucially this must not *load* the C++ core or mutate ``sys.path``: doing either
    leaks global state into sibling tests (loading the core enriches ``cad_guide_text``,
    which flips the manual-drift check). So core presence is detected by inspecting the
    resolvable ``.so`` path only — never by importing core_bridge and calling
    ``available()`` (which dlopen's the library)."""
    core = _core_so_present()

    frontier = any(os.environ.get(k) for k in
                   ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
                    "GEMINI_API_KEY"))

    local = _local_model_present()

    # A "real" tool-executing agent_fn is the node_runner's default path being a
    # genuine tool-calling turn rather than the silent stub.  T-W30 fixed the
    # historical bug where node_runner imported a non-existent
    # ``services.agent.providers.anthropic`` module (the module is
    # ``anthropic_provider``) and every "live" node degraded to _StubAgentResult.
    # We now probe the REAL seam: the default agent_fn must resolve to a turn built
    # on the tool-calling loop (agent_turn) via the config provider seam — importable
    # AND wired, not a stub.
    real_agent = _real_agent_fn_present()

    return ExecProbe(core_available=core, frontier_key=frontier,
                     local_model=local, real_agent_fn=real_agent,
                     dag_deps=_dag_deps_importable())


# ---------------------------------------------------------------------------
# Interpreter guard (T-DP19) — the gym real-run path runs run_mission IN-PROCESS,
# whose PLAN phase imports services.dag.executor → `from langgraph.checkpoint.sqlite
# import SqliteSaver` at module load. Launched under an interpreter lacking that
# package (system python3), execute_dag raises ImportError, the mission dies at PLAN,
# and every tuning cell ties at win_rate 0.00. The deps live in the shared mcp-venv;
# re-exec into a capable interpreter AT ENTRY so real runs actually reach BUILD.
#
# NOTE: the T-DP19 work order framed this as "propagate sys.executable across a
# subprocess boundary" — but the whole mission→DAG path is in-process, so the correct
# fix is a re-exec-at-entry guard, not interpreter propagation across a spawn.
# ---------------------------------------------------------------------------

_REEXEC_SENTINEL = "_AICAD_GYM_REEXEC"
_MCP_VENV_PY = Path(os.path.expanduser("~")) / ".local/share/AI-CAD/mcp-venv/bin/python"


def _dag_deps_importable() -> bool:
    """True when the DAG executor's checkpoint dependency imports in THIS interpreter.

    Uses find_spec so we do NOT actually import langgraph (no side effects, no cost)."""
    try:
        import importlib.util
        return importlib.util.find_spec("langgraph.checkpoint.sqlite") is not None
    except Exception:  # noqa: BLE001 — a broken import machinery counts as "absent"
        return False


def _capable_interpreter() -> str | None:
    """Resolve a python that has the DAG deps: $AI_CAD_PYTHON, then the shared mcp-venv.

    Mirrors how scripts/gate.sh resolves its runner. Returns None when neither resolves
    to an existing executable distinct from the current one (e.g. CI without the
    mcp-venv) — the caller then falls through and runs (mock still works).

    NB: compare LITERAL abspaths, not os.path.realpath — a venv python is a symlink to
    its base interpreter, so realpath would collapse the mcp-venv onto /usr/bin/python3
    and wrongly skip it, discarding exactly the site-packages (langgraph) we need."""
    import sys
    here = os.path.abspath(sys.executable)
    for cand in (os.environ.get("AI_CAD_PYTHON"), str(_MCP_VENV_PY)):
        if cand and os.path.exists(cand) and os.path.abspath(cand) != here:
            return cand
    return None


def ensure_capable_interpreter(module: str) -> None:
    """Re-exec the gym into an interpreter that has the DAG deps — once.

    *module* is the dotted entrypoint module (e.g. "tools.flywheel.bench.gym.tuning")
    so the re-exec preserves the ``-m`` package invocation (and thus the repo-root
    sys.path[0] the original launch relied on).

    No-op when (a) the deps already import here, (b) we've already re-exec'd once (loop
    sentinel — a target that ALSO lacks the dep must not spin forever), or (c) no better
    interpreter exists. In cases (b)/(c) we fall through and run: the ``--mock`` path
    works under any interpreter, and the probe now reports mock honestly."""
    import sys
    if _dag_deps_importable():
        return
    if os.environ.get(_REEXEC_SENTINEL):
        return
    target = _capable_interpreter()
    if not target:
        return
    os.environ[_REEXEC_SENTINEL] = "1"
    print(f"[gym] re-exec into {target}\n"
          f"      (this interpreter lacks langgraph.checkpoint.sqlite; real DAG runs "
          f"would die at PLAN — see T-DP19)", file=sys.stderr, flush=True)
    os.execv(target, [target, "-m", module, *sys.argv[1:]])


def _real_agent_fn_present() -> bool:
    """True when node_runner's default agent_fn is the real tool-executing turn.

    Checks that (a) the tool-calling turn module imports, (b) the provider config
    seam imports, and (c) node_runner wires them (no ``providers.anthropic`` stub
    import).  Pure import inspection — no model call, no network, no side effect."""
    try:
        import importlib
        importlib.import_module("services.agent.agent_turn")
        importlib.import_module("services.agent.config")
        nr = importlib.import_module("services.dag.node_runner")
        src = importlib.import_module("inspect").getsource(nr._default_agent_fn)
        # The fixed path calls the config provider seam + agent_turn; the old stub
        # path imported the (nonexistent) providers.anthropic module.
        return ("agent_turn" in src and "config" in src
                and "providers.anthropic " not in src
                and "providers.anthropic\n" not in src)
    except Exception:
        return False


def _core_so_present() -> bool:
    """Is a libaicad .so resolvable WITHOUT loading it? Checks the same env knobs
    core_bridge honours ($AI_CAD_LIBAICAD / $AI_CAD_DIST) plus the conventional build
    output path — pure filesystem inspection, no dlopen, no import side effect."""
    direct = os.environ.get("AI_CAD_LIBAICAD")
    if direct and Path(direct).exists():
        return True
    dist = os.environ.get("AI_CAD_DIST")
    if dist and (Path(dist) / "libaicad.so").exists():
        return True
    for p in (_repo_root() / "core" / "build-linux" / "bin" / "libaicad.so",
              _repo_root() / "core" / "build" / "bin" / "libaicad.so"):
        if p.exists():
            return True
    return False


def _local_model_present() -> bool:
    home = Path(os.path.expanduser("~"))
    for base in (home / ".local/share/AI-CAD/models",):
        if base.exists() and any(base.rglob("*.gguf")):
            return True
    return False


def _repo_root() -> Path:
    return _HERE.parents[3]


# ---------------------------------------------------------------------------
# Scripted intake + agent seam (writes REAL ledger events verbatim)
# ---------------------------------------------------------------------------

def make_intake_fn(challenge: Challenge) -> Callable[[str], dict]:
    """Deterministic intake: return the challenge's fixture manifest (no model)."""
    def _intake(_request: str) -> dict:
        # A fresh copy each call so run_mission's mutations don't leak across seeds.
        return json.loads(json.dumps(challenge.manifest))
    return _intake


def replay_turns_into_ledger(ledger, challenge: Challenge, seed: int) -> None:
    """Append the challenge's scripted turns for *seed* into *ledger* verbatim.

    Uses ledger.append (generic write) so arbitrary event kinds (tool_call, advisory,
    ladder_bounce...) land with the exact payload the classifier reads. Also writes the
    manifest as an artifact event carrying the deliverables (the classifier's
    known-ids / wrong_question source)."""
    # 1) manifest artifact (so known-ids + wrong_question have their anchor).
    man = challenge.manifest
    ledger.append("intake_analyst", "artifact", {
        "kind": "manifest", "id": "manifest",
        "deliverables": man.get("deliverables", []),
        "ambiguities": man.get("ambiguities", []),
        "assumptions": man.get("assumptions", []),
    })
    # 2) the seed's scripted per-deliverable turns.
    per_dl = challenge.scripts.get(seed, {})
    for dl_id, turns in per_dl.items():
        for turn in turns:
            payload = dict(turn)
            kind = payload.pop("_kind", "decision")
            actor = payload.pop("_actor", _actor_for(kind, dl_id))
            ledger.append(actor, kind, payload)


def _actor_for(kind: str, dl_id: str) -> str:
    return {
        "dimension": "spec_writer", "decision": "feature_modeler",
        "tool_call": "feature_modeler", "artifact": "reviewer",
        "advisory": "foreman", "question": "clarifier",
    }.get(kind, "feature_modeler")



# Tools that count as BUILD-stage mutating ops for GAP-INJECT (WI-6).
_BUILD_MUTATING_TOOLS: frozenset[str] = frozenset({
    "add_features", "new_part", "extrude", "revolve", "loft", "sweep",
    "fillet", "chamfer", "shell", "hole", "pattern", "mirror",
    "boolean", "cut", "delete_feature", "doc_apply_ops",
})


def _is_build_mutating_tool(tool: str) -> bool:
    t = (tool or "").lower()
    if t in _BUILD_MUTATING_TOOLS:
        return True
    # heuristic: feature-ish ops
    for key in ("extrude", "revolve", "fillet", "pattern", "hole", "cut", "boolean"):
        if key in t:
            return True
    return False


def apply_inject_faults(ledger, challenge: Challenge) -> list[dict]:
    """GAP-INJECT: apply ``challenge.inject_faults`` after N build tool_calls (WI-6).

    Each fault dict: ``{after_build_calls: int, fault: str, feature?: dict, note?: str}``.
    Emits ledger events so C12 graders can observe the inject without editing runner.
    Returns the list of applied fault records.
    """
    faults = list(getattr(challenge, "inject_faults", None) or [])
    if not faults:
        return []

    # Count build mutating tool_calls currently in the ledger.
    from .taxonomy import _tool_call_events  # local import; same package
    events = list(getattr(ledger, "events", None) or [])
    # Ledger object may expose .events; if empty, try to_dict path.
    if not events and hasattr(ledger, "to_dict"):
        events = list((ledger.to_dict() or {}).get("events") or [])
    # Prefer reading from the on-disk document via the ledger's internal list.
    if hasattr(ledger, "events"):
        events = list(ledger.events)

    build_count = 0
    for _e, p in _tool_call_events(events):
        if _is_build_mutating_tool(str(p.get("tool") or "")):
            build_count += 1

    applied: list[dict] = []
    for fault in faults:
        after_n = int(fault.get("after_build_calls") or 0)
        if build_count < after_n:
            continue
        note = fault.get("note") or f"injected fault {fault.get('fault')!r}"
        feature = fault.get("feature") or {}
        # Emit an injected bad feature tool_call + artifact so graders see it.
        ledger.append("gym_inject", "tool_call", {
            "tool": str(feature.get("op") or "add_features"),
            "args": dict(feature) if isinstance(feature, dict) else {},
            "ok": False,
            "error": f"injected_fault:{fault.get('fault', 'unknown')}",
            "injected": True,
            "fault": fault.get("fault"),
            "note": note,
        })
        ledger.append("gym_inject", "artifact", {
            "id": f"injected_fault:{fault.get('fault', 'unknown')}",
            "kind": "injected_fault",
            "fault": fault.get("fault"),
            "after_build_calls": after_n,
            "feature": feature,
            "note": note,
        })
        ledger.append("gym_inject", "decision", {
            "id": "injected_fault",
            "fault": fault.get("fault"),
            "text": note,
        })
        applied.append(dict(fault))
    return applied


def default_tools_for_division(division: str) -> list[str]:
    """Representative tools_slot list used for division HARD filtering tests."""
    base = [
        "new_part", "add_features", "measure_part", "query_geometry",
        "describe_part", "describe_sketch", "describe_scene", "describe_assembly",
        "sketch_health", "check_sketch", "verify_battery", "check_interference",
        "plm_checkpoint", "undo", "fit_calc",
        "render_part", "contact_sheet", "inspect_part", "look_around",
    ]
    return tools_for_division(division, base)


# ---------------------------------------------------------------------------
# One cell: challenge x tier x seed
# ---------------------------------------------------------------------------

@dataclass
class CellResult:
    challenge: str
    tier: str
    seed: int
    mode: str                         # real | mock
    passed: bool
    graded: dict = field(default_factory=dict)
    classifications: list = field(default_factory=list)
    mission_status: str = ""
    mission_phase: str = ""
    run_dir: str = ""
    division: str = "local"           # "local" | "open" (WI-1)
    # tools offered after division filter (LOCAL strips vision)
    tools_offered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge": self.challenge, "tier": self.tier, "seed": self.seed,
            "division": self.division,
            "mode": self.mode, "passed": self.passed, "graded": self.graded,
            "classifications": [c.to_dict() for c in self.classifications],
            "mission_status": self.mission_status, "mission_phase": self.mission_phase,
            "run_dir": self.run_dir,
            "tools_offered": list(self.tools_offered),
        }


def run_cell(challenge: Challenge, tier: str, seed: int, *, cycle: str,
             probe: ExecProbe, grind_threshold: int = 3,
             runs_dir: Path | None = None, real: bool = False,
             division: str = "local") -> CellResult:
    """Run one (challenge, division, tier, seed) cell end to end and grade + classify it.

    *runs_dir* overrides the output root (default ``reports/runs``); tests pass a tmp
    dir so the suite never writes under the committed ``reports/`` tree.

    *division* is ``"local"`` or ``"open"`` (WI-1). Vision-eyes challenges refuse
    LOCAL. LOCAL cells strip vision tools from the offered tools surface.

    *real* (T-W30): when True AND the probe reports a real-runnable environment,
    drive the FULL pipeline with execute_dag=True, the node_runner's real
    tool-executing agent_fn, and a resolved local/frontier provider — the mission
    genuinely executes tool calls and the reviewer genuinely runs the acceptance
    checks (no scripted ledger replay).  Otherwise the cell runs in MOCK mode: the
    real phase machine to PLAN + scripted ledger turns as the gradeable signal.
    """
    from services.agent.ledger import Ledger

    assert_division_eyes_allowed(challenge, division)
    if division not in getattr(challenge, "divisions", ("local", "open")):
        raise ValueError(
            f"challenge {challenge.id} does not support division={division!r} "
            f"(allowed={challenge.divisions})"
        )

    real_mode = bool(real and probe.can_run_real)
    run_id = f"gym_{challenge.id}_{division}_{tier}_s{seed}"
    base = runs_dir if runs_dir is not None else (REPORTS_DIR / "runs")
    # Keep legacy dir shape when division is default-local for backward paths;
    # include division in the folder so local/open cells do not collide.
    run_dir = Path(base) / cycle / f"{challenge.id}_{division}_{tier}_s{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Fresh ledger for this cell (its own file under the run dir). A cell is
    # deterministic, so a re-run must start CLEAN — the Ledger loads-from-disk if the
    # file exists (appending would double-write events on re-run). Remove any stale one.
    ledger_path = run_dir / "ledger.json"
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = Ledger(run_id, path=ledger_path)
    ledger.set_brief("pipeline", title=f"Gym {challenge.id}", summary=challenge.request[:400])

    # T-W28: the gym reads the SAME effort resolution path as the mission
    # pipeline and the settings GUI (tiers.resolve_effort → presets + user
    # overrides), and snapshots it into the cell ledger like a real mission.
    from services.agent.tiers import resolve_effort, effort_tier_for
    from services.agent.effort import record_effort_snapshot
    # openrouter:<model-id> reuses the frontier budget profile for effort.
    effort_profile = resolve_effort(effort_tier_for(tier) or tier)
    record_effort_snapshot(ledger, active_tier=effort_profile.tier)

    if real_mode:
        # REAL: run the full pipeline, then FOLD the mission's own ledger (written at
        # the default run path by run_mission) into THIS gym ledger so the real
        # tool_call / agent_turn / criterion_result events are graded/classified.
        mission = _run_mission_real(challenge, run_id, tier)
        _merge_mission_ledger(run_id, ledger)
    else:
        # MOCK: drive the phase machine to PLAN (execute_dag=False) + scripted turns.
        mission = _run_mission_safe(challenge, run_id)
        replay_turns_into_ledger(ledger, challenge, seed)

    # GAP-INJECT (WI-6): after scripted/real build tool_calls, apply declared faults.
    apply_inject_faults(ledger, challenge)

    ledger_doc = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))

    # Division HARD tool surface (WI-1.4): LOCAL never offers vision tools.
    tools_offered = default_tools_for_division(division)

    # Independent grading + taxonomy classification.
    ctx = {"challenge": challenge.id, "seed": seed, "tier": tier,
           "division": division, "effort": effort_profile.to_payload(),
           "tools_offered": tools_offered}
    graded = challenge.grade(ledger_doc, ctx)
    classifications = classify_ledger(
        ledger_doc, manifest=challenge.manifest, graded=graded,
        grind_threshold=grind_threshold)

    cell = CellResult(
        challenge=challenge.id, tier=tier, seed=seed,
        mode="real" if real_mode else "mock",
        passed=bool(graded["passed"]), graded=graded, classifications=classifications,
        mission_status=(mission or {}).get("status", "n/a"),
        mission_phase=(mission or {}).get("phase", "n/a"),
        run_dir=str(run_dir),
        division=division,
        tools_offered=tools_offered)

    # Persist the replayable artifacts.
    (run_dir / "graded.json").write_text(json.dumps(graded, indent=2), encoding="utf-8")
    (run_dir / "classifications.json").write_text(
        json.dumps([c.to_dict() for c in classifications], indent=2), encoding="utf-8")
    (run_dir / "mission.json").write_text(json.dumps(mission or {}, indent=2, default=str),
                                          encoding="utf-8")
    return cell


# T-DP19: local-tier cells must run on the bundled local model, not on whatever
# config.json defaults to. Started once and cached (VRAM is finite — a fresh server per
# cell OOMs the GPU; the llamacpp provider reuses its fixed-port server across cells).
#
# Resolution itself now delegates to services.agent.tiers.provider_for_tier — the
# SINGLE tier->provider seam shared with services.dag.node_runner (the node_runner
# fix for the twin half of T-DP19: internal DAG nodes used to independently
# re-resolve a bare ambient provider and ignore whatever tier/provider the mission
# was started with; only the gym had a local workaround, and only for gym cells).
# This function is kept as a thin, gym-specific wrapper purely for the VRAM cache
# below — the tier->provider mapping itself is no longer duplicated here.
_LOCAL_PROVIDER_CACHE: dict[str, Any] = {}
_DEFAULT_LOCAL_MODEL = "qwen3-4b"  # the bundled CUDA model the tuning loop benchmarks (tiers.yaml local_models.fanout)

# Tokenmaxxing (Hunter, 2026-07-07): three local models are production-ready and
# bundled/served via llama.cpp (services/installer/llamacpp.py, model_manager._REGISTRY,
# benchmarks/tokenmaxx/REPORT.md). New tier names are suffixed variants of the existing
# "local-24k" tiers.yaml profile (same 24k budget/context class — effort.normalize_tier
# prefix-matches "local-24k" against them), matching the "frontier-200k" alias convention:
#
#   local-24k             -> qwen3-4b        (unchanged default; fastest, NO GBNF —
#                                              structured-output nodes fall back to
#                                              prompted-JSON + roles.validate_handoff)
#   local-24k-gemma3-qat  -> gemma3-4b-qat   (smallest footprint, FULL GBNF grammar
#                                              compliance — structured/grammar-requiring
#                                              roles actually exercise the schema_to_gbnf
#                                              path here, which qwen3-4b's tier never does)
#   local-24k-gemma4-e4b  -> gemma4-e4b      (larger/slower GBNF worker; comparison-only,
#                                              no measured upside over gemma3-4b-qat today)
#
# The tier->model_id map itself lives in services.agent.tiers.LOCAL_TIER_MODELS (the
# single shared seam also consulted by provider_for_tier) — NOT duplicated here, so the
# gym's cache key always agrees with whatever model provider_for_tier actually resolves.
# A tier not listed there falls back to tiers.yaml's local_models.fanout (_DEFAULT_LOCAL_MODEL).
#
# T-DP8 council mechanism check (2026-07-07): services/agent/councils.py's
# council_convene() DOES accept a ``providers`` list for diverse-model seats, but it is
# only wired into ONE call site — regen_diagnosis._maybe_convene_fix_council (the
# ladder's fix-stage council), reached from deep inside run_mission's DAG execution, not
# from a parameter run_mission()/loop.run_mission() itself exposes. _run_mission_real()
# calls loop.run_mission(request, provider, ...) with exactly ONE Provider for the whole
# mission — there is no role-based multi-provider seam at that call boundary today.
# Wiring council_providers through run_mission's signature down to the ladder would be
# new orchestration machinery, not a tier-mapping change — explicitly out of scope here.
# NO "council" tier is added; a stub would only produce meaningless benchmark data.


def _provider_for_tier(tier: str):
    """Resolve the provider a gym cell runs on (cached per model id for VRAM reuse).

    A ``local-*`` cell MUST run on the bundled local model — otherwise the ambient
    config.json (a cloud provider) silently answers and a ``local-24k`` benchmark is
    meaningless (T-DP19: real cells were routing to Gemini and tying at win_rate 0.00,
    which read like a model-quality signal but was really a routing artifact).

    Parameterized ``openrouter:<model-id>`` tiers resolve to OpenRouterProvider with
    that model (multi-provider leaderboard path). Cached by model id so paid cells
    share the same adapter instance across the matrix. Same for ``xai:<model-id>``
    (subscription OAuth when connected, else API key).

    An explicit ``AI_CAD_PROVIDER`` override always wins — tests set it to ``mock``; a
    human can force a frontier key for a frontier-tier comparison. Non-local tiers fall
    through to the default resolution. Actual tier->provider resolution is delegated to
    ``services.agent.tiers.provider_for_tier`` (shared with node_runner); the gym only
    adds the llama.cpp-server VRAM cache, keyed off the SAME per-tier model map
    (``tiers.LOCAL_TIER_MODELS``) that ``provider_for_tier`` itself consults, so
    ``local-24k-gemma3-qat`` and ``local-24k-gemma4-e4b`` each get their own cached
    server instead of colliding on the fanout default."""
    import os
    from services.agent import config
    from services.agent.tiers import (
        LOCAL_TIER_MODELS, load_local_models, parse_openrouter_tier, parse_xai_tier,
        provider_for_tier,
    )
    if os.environ.get("AI_CAD_PROVIDER"):
        return config.get_provider()
    # OpenRouter paid tiers: cache by model id (no VRAM server, but reuse adapter).
    or_model = parse_openrouter_tier(tier)
    if or_model is not None:
        cache_key = f"openrouter:{or_model}"
        prov = _LOCAL_PROVIDER_CACHE.get(cache_key)
        if prov is None:
            prov = provider_for_tier(tier)
            _LOCAL_PROVIDER_CACHE[cache_key] = prov
        return prov
    # xAI paid tiers: cache by model id (sub or API-key provider).
    xai_model = parse_xai_tier(tier)
    if xai_model is not None:
        cache_key = f"xai:{xai_model}"
        prov = _LOCAL_PROVIDER_CACHE.get(cache_key)
        if prov is None:
            prov = provider_for_tier(tier)
            _LOCAL_PROVIDER_CACHE[cache_key] = prov
        return prov
    if not tier.startswith("local"):
        return config.get_provider()
    model_id = LOCAL_TIER_MODELS.get(tier)
    if model_id is None:
        try:
            model_id = load_local_models().get("fanout") or _DEFAULT_LOCAL_MODEL
        except Exception:  # noqa: BLE001 — best-effort cache key, falls back to the constant
            model_id = _DEFAULT_LOCAL_MODEL
    prov = _LOCAL_PROVIDER_CACHE.get(model_id)
    if prov is None:
        prov = provider_for_tier(tier)
        _LOCAL_PROVIDER_CACHE[model_id] = prov
    return prov



def _run_mission_real(challenge: Challenge, run_id: str, tier: str) -> dict | None:
    """Drive the FULL pipeline for a REAL cell (T-W30): execute_dag=True with the
    node_runner's real tool-executing agent_fn and the tier's provider (T-DP19: a
    local-* tier resolves to the bundled qwen3-4b, not the ambient cloud default).

    Honest failure handling: a mission/DAG/model error is CAPTURED into the mission
    record (and the ledger already holds whatever real events were emitted), never
    swallowed — the C1 proof needs the observed failure class, not a hidden stub."""
    # A cell is deterministic, so a re-run must start CLEAN: run_mission does
    # Ledger.load(run_id) which APPENDS to any existing mission ledger — a stale
    # file from an earlier attempt would contaminate the folded events. Same rule
    # as the gym cell ledger above.
    try:
        from services.agent.ledger import run_ledger_path
        mp = run_ledger_path(run_id)
        if mp.exists():
            mp.unlink()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
    try:
        from services.agent import loop
        provider = _provider_for_tier(tier)  # local-* → bundled qwen3-4b, not ambient cloud (T-DP19)
        mr = loop.run_mission(
            challenge.request, provider,
            name=run_id, intake_fn=make_intake_fn(challenge),
            execute_dag=True, autonomy_default="auto")
        return {"status": mr.status, "phase": mr.phase,
                "phases_completed": mr.phases_completed,
                "findings": mr.findings, "stop_reason": mr.stop_reason,
                "mode": "real"}
    except Exception as exc:  # a real-run failure is DATA, not a crash
        return {"status": "error", "phase": "intake", "mode": "real",
                "error": f"{type(exc).__name__}: {exc}"}


def _merge_mission_ledger(run_id: str, gym_ledger) -> None:
    """Fold the mission's own ledger (written by run_mission at the default run path)
    into the gym cell ledger, so the real events land in the committed run dir.

    run_mission does ``Ledger.load(run_id)`` → ``run_ledger_path(run_id)``; the gym
    ledger is a separate file under the run dir.  We copy the mission events across
    (skipping the gym's own brief) so grading/classification see the real turns."""
    try:
        from services.agent.ledger import run_ledger_path
        import json as _json
        mp = run_ledger_path(run_id)
        if not mp.exists():
            return
        mdoc = _json.loads(mp.read_text(encoding="utf-8"))
        for ev in mdoc.get("events", []):
            if ev.get("kind") == "brief":
                continue  # the gym ledger already has its own brief
            gym_ledger.append(ev.get("actor", "mission"), ev.get("kind", "note"),
                              ev.get("payload", {}))
    except Exception:  # noqa: BLE001 — merge is best-effort, never fatal
        pass


def _run_mission_safe(challenge: Challenge, run_id: str) -> dict | None:
    """Drive run_mission for its INTAKE + validate + compile phases. Never fatal."""
    try:
        from services.agent import loop
        from services.agent.providers.mock_provider import MockProvider
        mr = loop.run_mission(
            challenge.request, MockProvider([]),
            name=run_id, intake_fn=make_intake_fn(challenge),
            execute_dag=False, autonomy_default="auto")
        return {"status": mr.status, "phase": mr.phase,
                "phases_completed": mr.phases_completed,
                "findings": mr.findings, "stop_reason": mr.stop_reason}
    except Exception as exc:  # a pipeline error must not sink the gym cell
        return {"status": "error", "phase": "intake", "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# A full cycle: the matrix
# ---------------------------------------------------------------------------

@dataclass
class CycleResult:
    cycle: str
    tiers: list[str]
    probe: ExecProbe
    cells: list[CellResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"cycle": self.cycle, "tiers": self.tiers,
                "probe": self.probe.to_dict(),
                "cells": [c.to_dict() for c in self.cells]}


def _default_divisions_for_tier(tier: str) -> list[str]:
    """Map a tier string to the divisions it can schedule under.

    Local tiers → LOCAL only by default; mid/frontier/openrouter/xai → OPEN
    (and LOCAL when the challenge is in the calibration subset — handled by the
    challenge.divisions filter + explicit --division CLI).
    """
    t = (tier or "").lower()
    if t.startswith("local") or t.startswith("llamacpp"):
        return ["local"]
    if t.startswith("openrouter:") or t.startswith("xai:") or "frontier" in t or t == "mid":
        return ["open"]
    return ["local", "open"]


def run_cycle(cycle: str = "cycle-1", *, tiers: list[str] | None = None,
              challenge_ids: list[str] | None = None,
              grind_threshold: int = 3,
              runs_dir: Path | None = None,
              real: bool = False,
              divisions: list[str] | None = None) -> CycleResult:
    """Run the full challenge x division x tier x seed matrix and return the cycle result.

    *runs_dir* overrides the output root (tests pass a tmp dir; the CLI leaves it None
    so the committed ``reports/runs`` tree is written for cycle-1).

    *divisions* restricts which divisions to schedule (default: derive per tier —
    local tiers → local; frontier/openrouter → open). Calibration challenges that
    list both divisions still only run the divisions present in this list.

    *real* (T-W30): request REAL execution (honoured per-cell only when the probe
    reports a real-runnable environment)."""
    probe = execution_probe()
    tiers = tiers or ["local-24k", "frontier-200k"]
    challenges = load_challenges()
    if challenge_ids:
        challenges = [c for c in challenges if c.id in set(challenge_ids)]

    result = CycleResult(cycle=cycle, tiers=tiers, probe=probe)
    for ch in challenges:
        ch_divs = set(getattr(ch, "divisions", ("local", "open")) or ("local", "open"))
        for tier in tiers:
            tier_divs = divisions if divisions is not None else _default_divisions_for_tier(tier)
            for division in tier_divs:
                if division not in ch_divs:
                    continue
                # HARD refuse: vision-eyes never under LOCAL
                if division == "local" and getattr(ch, "eyes", "either") == "vision":
                    continue
                for seed in ch.seeds:
                    result.cells.append(
                        run_cell(ch, tier, seed, cycle=cycle, probe=probe,
                                 grind_threshold=grind_threshold, runs_dir=runs_dir,
                                 real=real, division=division))
    return result
