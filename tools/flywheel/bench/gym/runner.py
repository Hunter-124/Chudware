"""Design-gym runner.

Runs each challenge through the real DAG pipeline per seed on the configured
tier and:

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

Every run writes a replayable run dir under ``reports/runs/<cycle>/<portable-cell>/``
containing exact ``cell.json`` identity plus ledger.json, graded.json,
classifications.json, and mission.json.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .challenges import (
    Challenge,
    load_challenges,
    assert_division_eyes_allowed,
    tools_for_division,
    VISION_TOOLS,
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
    # Is the DAG executor's checkpoint dependency importable in THIS
    # interpreter? run_mission's PLAN phase imports services.dag.executor, whose
    # top-level `from langgraph.checkpoint.sqlite import SqliteSaver` raises under an
    # interpreter that lacks it (system python3). Without this gate the probe reports
    # "real", the mission dies at PLAN, and every cell ties at win_rate 0.00 — a
    # misleading signal that looks like a model-quality result. Default True so
    # externally-constructed probes keep their old meaning.
    dag_deps: bool = True

    @property
    def can_run_real(self) -> bool:
        # A genuine CAD/model-in-the-loop cycle needs the engine, a tool-executing
        # agent, the DAG deps in-process, and at least one provisioned model path.
        return (
            self.core_available
            and self.real_agent_fn
            and self.dag_deps
            and (self.frontier_key or self.local_model)
        )

    @property
    def mode(self) -> str:
        return "real" if self.can_run_real else "mock"

    def to_dict(self) -> dict[str, Any]:
        return {
            "core_available": self.core_available,
            "frontier_key": self.frontier_key,
            "local_model": self.local_model,
            "real_agent_fn": self.real_agent_fn,
            "dag_deps": self.dag_deps,
            "mode": self.mode,
        }


class InfrastructureUnavailable(RuntimeError):
    """A requested REAL gym matrix cannot be executed on this machine."""

    status = "infrastructure_unavailable"

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


class UnknownChallengeError(ValueError):
    """The requested challenge set contains ids absent from the registry."""

    status = "unknown_challenge"

    def __init__(self, challenge_ids: list[str]):
        self.challenge_ids = list(challenge_ids)
        super().__init__(", ".join(self.challenge_ids))


def _frontier_credentials_present() -> bool:
    """Report configured online capability without constructing a Python provider."""
    try:
        from services.agent import config

        cfg = config.load_config()
        keys = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
        nested_keys = [
            section.get("api_key")
            for section in cfg.values()
            if isinstance(section, dict)
        ]
        if cfg.get("api_key") or any(keys.values()) or any(nested_keys):
            return True
        configured_name = (
            str(os.environ.get("CHUDWARE_PROVIDER") or cfg.get("provider") or "")
            .strip()
            .lower()
        )
        if configured_name and configured_name not in {
            "mock",
            "llamacpp",
            "llama.cpp",
            "llama-cpp",
            "vllm",
        }:
            return True
        subscriptions = cfg.get("subscription_auth")
        if isinstance(subscriptions, dict) and any(
            isinstance(value, dict) and bool(value) for value in subscriptions.values()
        ):
            return True
    except Exception:
        pass

    return any(
        os.environ.get(key)
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "XAI_API_KEY",
            "MOONSHOT_API_KEY",
        )
    )


def execution_probe() -> ExecProbe:
    """Probe the environment for real-execution capability — NO side effects.

    Crucially this must not *load* the C++ core or mutate ``sys.path``: doing either
    leaks global state into sibling tests (loading the core enriches ``cad_guide_text``,
    which flips the manual-drift check). So core presence is detected by inspecting the
    resolvable ``.so`` path only — never by importing core_bridge and calling
    ``available()`` (which dlopen's the library)."""
    core = _core_so_present()
    frontier = _frontier_credentials_present()
    local = _local_model_present()

    # A real agent function is the node runner's shared Chudcode role-turn path,
    # not a silent/stubbed result and not the retired Python cloud-provider loop.
    real_agent = _real_agent_fn_present()

    return ExecProbe(
        core_available=core,
        frontier_key=frontier,
        local_model=local,
        real_agent_fn=real_agent,
        dag_deps=_dag_deps_importable(),
    )


# ---------------------------------------------------------------------------
# Interpreter guard — the gym real-run path runs run_mission IN-PROCESS,
# whose PLAN phase imports services.dag.executor → `from langgraph.checkpoint.sqlite
# import SqliteSaver` at module load. Launched under an interpreter lacking that
# package (system python3), execute_dag raises ImportError, the mission dies at PLAN,
# and every tuning cell ties at win_rate 0.00. The deps live in the shared mcp-venv;
# re-exec into a capable interpreter AT ENTRY so real runs actually reach BUILD.
#
# The whole mission→DAG path is in-process, so the fix is a re-exec-at-entry guard,
# not interpreter propagation across a subprocess spawn.
# ---------------------------------------------------------------------------

_REEXEC_SENTINEL = "_CHUDWARE_GYM_REEXEC"
_MCP_VENV_PY = (
    Path(os.path.expanduser("~")) / ".local/share/Chudware/mcp-venv/bin/python"
)


def _dag_deps_importable() -> bool:
    """True when the DAG executor's checkpoint dependency imports in THIS interpreter.

    Uses find_spec so we do NOT actually import langgraph (no side effects, no cost)."""
    try:
        import importlib.util

        return importlib.util.find_spec("langgraph.checkpoint.sqlite") is not None
    except Exception:  # noqa: BLE001 — a broken import machinery counts as "absent"
        return False


def _capable_interpreter() -> str | None:
    """Resolve a python that has the DAG deps: $CHUDWARE_PYTHON, then the shared mcp-venv.

    Mirrors how scripts/gate.sh resolves its runner. Returns None when neither resolves
    to an existing executable distinct from the current one (e.g. CI without the
    mcp-venv) — the caller then falls through and runs (mock still works).

    NB: compare LITERAL abspaths, not os.path.realpath — a venv python is a symlink to
    its base interpreter, so realpath would collapse the mcp-venv onto /usr/bin/python3
    and wrongly skip it, discarding exactly the site-packages (langgraph) we need."""
    import sys

    here = os.path.abspath(sys.executable)
    for cand in (os.environ.get("CHUDWARE_PYTHON"), str(_MCP_VENV_PY)):
        if cand and os.path.exists(cand) and os.path.abspath(cand) != here:
            return cand
    return None


def ensure_capable_interpreter(module: str) -> None:
    """Re-exec the gym into an interpreter that has the DAG deps — once.

    *module* is the dotted entrypoint module (e.g. "tools.flywheel.bench.gym.runner")
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
    print(
        f"[gym] re-exec into {target}\n"
        f"      (this interpreter lacks langgraph.checkpoint.sqlite; real DAG runs "
        f"would die at PLAN)",
        file=sys.stderr,
        flush=True,
    )
    os.execv(target, [target, "-m", module, *sys.argv[1:]])


def _real_agent_fn_present() -> bool:
    """True when DAG nodes are wired to the shared Chudcode role runner."""
    try:
        import importlib

        importlib.import_module("services.agent.chudcode_roles")
        nr = importlib.import_module("services.dag.node_runner")
        src = importlib.import_module("inspect").getsource(nr._default_agent_fn)
        return (
            "run_role_turn" in src
            and "get_provider" not in src
            and "providers.anthropic" not in src
        )
    except Exception:
        return False


def _core_so_present() -> bool:
    """True when the production core-library resolver finds an existing library.

    This stays a pure filesystem probe: importing ``core_bridge`` defines its
    resolver but does not call ``_resolve_lib``/``ctypes.CDLL``.
    """
    try:
        from services.mcp import core_bridge

        return any(path.exists() for path in core_bridge._lib_candidates())
    except Exception:
        return False


def _local_model_present() -> bool:
    home = Path(os.path.expanduser("~"))
    for base in (home / ".local/share/Chudware/models",):
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
    ledger.append(
        "intake_analyst",
        "artifact",
        {
            "kind": "manifest",
            "id": "manifest",
            "deliverables": man.get("deliverables", []),
            "ambiguities": man.get("ambiguities", []),
            "assumptions": man.get("assumptions", []),
        },
    )
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
        "dimension": "spec_writer",
        "decision": "feature_modeler",
        "tool_call": "feature_modeler",
        "artifact": "reviewer",
        "advisory": "foreman",
        "question": "clarifier",
    }.get(kind, "feature_modeler")


# Tools that count as BUILD-stage mutating ops for GAP-INJECT.
_BUILD_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "add_features",
        "new_part",
        "extrude",
        "revolve",
        "loft",
        "sweep",
        "fillet",
        "chamfer",
        "shell",
        "hole",
        "pattern",
        "mirror",
        "boolean",
        "cut",
        "delete_feature",
        "doc_apply_ops",
    }
)


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
    """GAP-INJECT: apply ``challenge.inject_faults`` after N build tool_calls.

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
        ledger.append(
            "gym_inject",
            "tool_call",
            {
                "tool": str(feature.get("op") or "add_features"),
                "args": dict(feature) if isinstance(feature, dict) else {},
                "ok": False,
                "error": f"injected_fault:{fault.get('fault', 'unknown')}",
                "injected": True,
                "fault": fault.get("fault"),
                "note": note,
            },
        )
        ledger.append(
            "gym_inject",
            "artifact",
            {
                "id": f"injected_fault:{fault.get('fault', 'unknown')}",
                "kind": "injected_fault",
                "fault": fault.get("fault"),
                "after_build_calls": after_n,
                "feature": feature,
                "note": note,
            },
        )
        ledger.append(
            "gym_inject",
            "decision",
            {
                "id": "injected_fault",
                "fault": fault.get("fault"),
                "text": note,
            },
        )
        applied.append(dict(fault))
    return applied


def default_tools_for_division(division: str) -> list[str]:
    """Representative tools_slot list used for division HARD filtering tests."""
    base = [
        "new_part",
        "add_features",
        "measure_part",
        "query_geometry",
        "describe_part",
        "describe_sketch",
        "describe_scene",
        "describe_assembly",
        "sketch_health",
        "check_sketch",
        "verify_battery",
        "check_interference",
        "plm_checkpoint",
        "undo",
        "fit_calc",
        "render_part",
        "contact_sheet",
        "inspect_part",
        "look_around",
    ]
    return tools_for_division(division, base)


# ---------------------------------------------------------------------------
# One cell: challenge x tier x seed
# ---------------------------------------------------------------------------
# Characters Windows forbids in a path component, plus the separators. Model
# ids routinely contain ':' and '/', so a cell id is not a legal directory name
# on NTFS and is a nested path on POSIX. Everything illegal collapses to '-'.
_UNSAFE_PATH_CHARS = re.compile(r'[:*?"<>|/\\]+')


def _path_component(name: str) -> str:
    """Return *name* reduced to one directory component that is legal everywhere.

    This is deliberately lossy and deliberately not reversible: the authoritative
    cell identity lives in ``CellResult`` and in the run's JSON, so the directory
    only has to be unique and portable, not parseable. Two cells whose ids differ
    solely by an illegal character would collide, which cannot happen here because
    the illegal characters come from the model id and the model id is echoed in
    full inside the run's own files.
    """
    return _UNSAFE_PATH_CHARS.sub("-", name).strip(". ")


@dataclass
class CellResult:
    challenge: str
    tier: str
    seed: int
    mode: str  # real | mock
    passed: bool
    graded: dict = field(default_factory=dict)
    classifications: list = field(default_factory=list)
    mission_status: str = ""
    mission_phase: str = ""
    run_dir: str = ""
    division: str = "local"  # "local" | "open"
    # tools offered after division filter (LOCAL strips vision)
    tools_offered: list[str] = field(default_factory=list)
    cycle: str = ""
    model: str = ""
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge": self.challenge,
            "tier": self.tier,
            "seed": self.seed,
            "division": self.division,
            "cycle": self.cycle,
            "model": self.model,
            "run_id": self.run_id,
            "mode": self.mode,
            "passed": self.passed,
            "graded": self.graded,
            "classifications": [
                c.to_dict() if hasattr(c, "to_dict") else dict(c)
                for c in self.classifications
            ],
            "mission_status": self.mission_status,
            "mission_phase": self.mission_phase,
            "run_dir": self.run_dir,
            "tools_offered": list(self.tools_offered),
        }


def run_cell(
    challenge: Challenge,
    tier: str,
    seed: int,
    *,
    cycle: str,
    probe: ExecProbe,
    grind_threshold: int = 3,
    runs_dir: Path | None = None,
    real: bool = False,
    division: str = "local",
) -> CellResult:
    """Run one (challenge, division, tier, seed) cell end to end and grade + classify it.

    *runs_dir* overrides the output root (default ``reports/runs``); tests pass a tmp
    dir so the suite never writes under the default ``reports/`` tree.

    *division* is ``"local"`` or ``"open"``. Vision-eyes challenges refuse
    LOCAL. LOCAL cells strip vision tools from the offered tools surface.

    *real* is explicit execution intent. ``False`` always produces a MOCK cell,
    regardless of machine capability. ``True`` is accepted only after ``run_cycle``
    preflights the complete matrix; direct callers must likewise supply a capable
    probe, otherwise the cell refuses before creating a run directory.
    """
    from services.agent.ledger import Ledger

    assert_division_eyes_allowed(challenge, division)
    if division not in getattr(challenge, "divisions", ("local", "open")):
        raise ValueError(
            f"challenge {challenge.id} does not support division={division!r} "
            f"(allowed={challenge.divisions})"
        )

    if real:
        reasons = _real_unavailable_reasons(probe, [tier])
        if reasons:
            raise InfrastructureUnavailable(reasons)
    real_mode = bool(real)
    run_id = f"gym_{challenge.id}_{division}_{tier}_s{seed}"
    base = runs_dir if runs_dir is not None else (REPORTS_DIR / "runs")
    # Keep legacy dir shape when division is default-local for backward paths;
    # include division in the folder so local/open cells do not collide.
    #
    # The cell id keeps its real shape (`C1_open_xai:grok-4-5_s1`) in run_id, in
    # CellResult, and in every report — that is the logical name and reports
    # cite it. The DIRECTORY name cannot: tier is a model id like
    # `xai:grok-4-5`, `ollama:llama3.2:latest` or
    # `openrouter:deepseek/deepseek-chat-v3.1`, and NTFS rejects ':' outright
    # while '/' would silently fan the cell out into nested directories. A repo
    # holding such a path cannot be checked out on Windows at all — `git clone`
    # aborts with "invalid path" before any file lands, which took the whole
    # Windows CI leg down until 2026-08-10.
    run_dir = (
        Path(base)
        / cycle
        / _path_component(f"{challenge.id}_{division}_{tier}_s{seed}")
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    # Fresh ledger for this cell (its own file under the run dir). A cell is
    # deterministic, so a re-run must start CLEAN — the Ledger loads-from-disk if the
    # file exists (appending would double-write events on re-run).
    #
    # The Ledger now keeps its events in a `ledger.jsonl` SIDECAR (services/agent/
    # ledger.py: `_events_path = path.with_suffix(".jsonl")`) and ledger.json holds
    # only the materialized state snapshot. Deleting ledger.json alone therefore left
    # the sidecar behind and the next Ledger() APPENDED to the previous cell's events
    # — exactly the double-write this cleanup exists to prevent. Drop BOTH.
    ledger_path = run_dir / "ledger.json"
    for stale in (ledger_path, ledger_path.with_suffix(".jsonl")):
        if stale.exists():
            stale.unlink()
    ledger = Ledger(run_id, path=ledger_path)
    ledger.set_brief(
        "pipeline", title=f"Gym {challenge.id}", summary=challenge.request[:400]
    )

    from dataclasses import asdict
    from services.agent.preferences import resolve_turn_preferences

    turn_preferences = resolve_turn_preferences(challenge.request)
    preference_snapshot = asdict(turn_preferences)
    ledger.append("harness", "preferences", preference_snapshot)

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

    # GAP-INJECT: after scripted/real build tool_calls, apply declared faults.
    apply_inject_faults(ledger, challenge)

    # The gym run dir is a self-contained replay bundle, and its ledger.json is the
    # WHOLE-document (`events` + `state`) shape every gym consumer reads: the graders
    # and classifier below, cli.py, monitor.py, report.py, leaderboard.py and the
    # offline replay_ledger_failures corpus miner. Since the live Ledger writes only
    # the state snapshot to that path, re-reading the file yielded a doc with NO
    # events — every acceptance criterion then graded FAIL on an empty ledger. Publish
    # the full document from the in-memory ledger instead.
    ledger_doc = {"events": list(ledger.events), "state": ledger.state}
    ledger_path.write_text(json.dumps(ledger_doc, indent=2), encoding="utf-8")

    # Division HARD tool surface: LOCAL never offers vision tools.
    tools_offered = default_tools_for_division(division)

    # Independent grading + taxonomy classification.
    ctx = {
        "challenge": challenge.id,
        "seed": seed,
        "tier": tier,
        "division": division,
        "preferences": preference_snapshot,
        "tools_offered": tools_offered,
    }
    graded = challenge.grade(ledger_doc, ctx)
    classifications = classify_ledger(
        ledger_doc,
        manifest=challenge.manifest,
        graded=graded,
        grind_threshold=grind_threshold,
    )
    # The challenge graders read LEDGER claims (recorded dimensions, decisions, battery
    # verdicts) — no native file or saved study is reopened. In MOCK mode that is the
    # point (labeled scripts exercise the grader + taxonomy). In REAL mode a model's
    # self-reported ledger is not artifact proof, so the verdict is kept as a diagnostic
    # (`ledger_passed`) and the cell never counts as a pass. Artifact-verified real
    # grading lives in benchmarks/artifact_validity.
    if real_mode:
        graded["ledger_passed"] = bool(graded["passed"])
        graded["passed"] = False
        graded["verification"] = "ledger_only_unverified"
    else:
        graded["verification"] = "scripted_replay"

    cell = CellResult(
        challenge=challenge.id,
        tier=tier,
        seed=seed,
        mode="real" if real_mode else "mock",
        passed=bool(graded["passed"]),
        graded=graded,
        classifications=classifications,
        mission_status=(mission or {}).get("status", "n/a"),
        mission_phase=(mission or {}).get("phase", "n/a"),
        run_dir=str(run_dir),
        division=division,
        tools_offered=tools_offered,
        cycle=cycle,
        model=_model_for_tier(tier),
        run_id=run_id,
    )

    # Persist the replayable artifacts.
    (run_dir / "graded.json").write_text(json.dumps(graded, indent=2), encoding="utf-8")
    (run_dir / "classifications.json").write_text(
        json.dumps([c.to_dict() for c in classifications], indent=2), encoding="utf-8"
    )
    (run_dir / "mission.json").write_text(
        json.dumps(mission or {}, indent=2, default=str), encoding="utf-8"
    )
    (run_dir / "cell.json").write_text(
        json.dumps(cell.to_dict(), indent=2), encoding="utf-8"
    )
    return cell


# Explicit local tiers construct one offline-safe provider and cache managed
# llama.cpp instances because VRAM is finite. Paid tiers never enter this cache:
# they carry provider/model identifiers into Chudcode.
_LOCAL_PROVIDER_CACHE: dict[str, Any] = {}
_DEFAULT_LOCAL_MODEL = "qwen3-4b"  # the bundled CUDA model the tuning loop benchmarks (tiers.yaml local_models.fanout)

# Local tiers: three local models are bundled/served via llama.cpp
# (services/installer/llamacpp.py, model_manager._REGISTRY). Tier names are suffixed variants of the existing
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
# Local tier/model mapping remains in services.agent.tiers so the gym and the
# deterministic provider factory share one source of truth. Paid benchmark tiers
# carry only provider/model identifiers into Chudcode; no Python cloud provider
# is constructed.


def _parse_ollama_tier(tier: str) -> str | None:
    """Return the Ollama model id when *tier* is ``ollama:<model-id>``.

    Model ids may themselves contain colons (e.g. ``llama3.2:latest``), so only
    the first ``ollama:`` prefix is stripped. Returns ``None`` for other tiers.
    """
    if not tier or not isinstance(tier, str):
        return None
    prefix = "ollama:"
    if not tier.startswith(prefix):
        return None
    model_id = tier[len(prefix) :].strip()
    return model_id or None


def _load_benchmark_config() -> dict[str, Any]:
    try:
        from services.agent import config

        return dict(config.load_config() or {})
    except Exception:
        return {}


def _offline_provider_for_tier(tier: str, cfg: dict[str, Any]):
    """Construct only an explicit loopback/deterministic benchmark provider."""
    from services.agent import config
    from services.agent.tiers import (
        LOCAL_TIER_MODELS,
        load_local_models,
        provider_for_tier,
    )

    override = str(os.environ.get("CHUDWARE_PROVIDER") or "").strip().lower()
    configured = str(cfg.get("provider") or "").strip().lower()
    local_names = {"mock", "llamacpp", "llama.cpp", "llama-cpp", "vllm"}
    offline_cfg = {**cfg, "offline_mode": True}
    if override in local_names:
        provider = config.get_provider(name=override, cfg=offline_cfg)
    else:
        ollama_model = _parse_ollama_tier(tier)
        if ollama_model is not None:
            ocfg = dict(cfg.get("ollama") or {})
            base = (
                ocfg.get("base_url")
                or (cfg.get("openai") or {}).get("base_url")
                or "http://127.0.0.1:11434/v1"
            )
            if not str(base).rstrip("/").endswith("/v1"):
                base = str(base).rstrip("/") + "/v1"
            merged = {
                **offline_cfg,
                "model": ollama_model,
                "api_key": ocfg.get("api_key") or "ollama",
                "openai": {
                    **(offline_cfg.get("openai") or {}),
                    "base_url": base,
                    "api_key": ocfg.get("api_key") or "ollama",
                },
            }
            provider = config.get_provider("openai", cfg=merged)
            provider.name = "ollama"
        elif tier.startswith("local"):
            model_id = LOCAL_TIER_MODELS.get(tier)
            if model_id is None:
                try:
                    model_id = load_local_models().get("fanout") or _DEFAULT_LOCAL_MODEL
                except Exception:
                    model_id = _DEFAULT_LOCAL_MODEL
            provider = _LOCAL_PROVIDER_CACHE.get(model_id)
            if provider is None:
                provider = provider_for_tier(tier, cfg=offline_cfg)
                _LOCAL_PROVIDER_CACHE[model_id] = provider
        elif configured in local_names:
            provider = config.get_provider(name=configured, cfg=offline_cfg)
        else:
            raise ValueError(f"tier {tier!r} is not an explicit offline benchmark tier")
    if not config.provider_offline_safe(provider):
        raise config.OfflineModeError(
            f"benchmark tier {tier!r} resolved a non-local provider"
        )
    return provider


def _online_settings_for_tier(tier: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve provider/model identifiers for Chudcode without creating an adapter."""
    from services.agent.tiers import parse_openrouter_tier, parse_xai_tier

    openrouter_model = parse_openrouter_tier(tier)
    if openrouter_model is not None:
        return {"provider": "openrouter", "model": openrouter_model}
    xai_model = parse_xai_tier(tier)
    if xai_model is not None:
        return {"provider": "xai", "model": xai_model}
    settings = {
        "provider": str(
            os.environ.get("CHUDWARE_PROVIDER") or cfg.get("provider") or ""
        ).strip(),
        "model": str(cfg.get("model") or "").strip(),
        "reasoning_effort": cfg.get("reasoning_effort"),
    }
    return {key: value for key, value in settings.items() if value not in (None, "")}


def _tier_execution(tier: str) -> dict[str, Any]:
    """Return one honest benchmark route: local provider or Chudcode settings."""
    cfg = _load_benchmark_config()
    override = str(os.environ.get("CHUDWARE_PROVIDER") or "").strip().lower()
    configured = str(cfg.get("provider") or "").strip().lower()
    local_names = {"mock", "llamacpp", "llama.cpp", "llama-cpp", "vllm"}
    paid_tier = tier.startswith("openrouter:") or tier.startswith("xai:")
    explicit_offline = (
        override in local_names
        or (not override and tier.startswith("local"))
        or (not override and _parse_ollama_tier(tier) is not None)
        or (not override and not paid_tier and configured in local_names)
    )
    if explicit_offline:
        provider = _offline_provider_for_tier(tier, cfg)
        return {
            "offline": True,
            "provider": provider,
            "settings": {
                "provider": str(getattr(provider, "name", "") or ""),
                "model": str(getattr(provider, "model", "") or ""),
            },
        }
    return {
        "offline": False,
        "provider": None,
        "settings": _online_settings_for_tier(tier, cfg),
    }


def _message_payload(messages: list[dict]) -> tuple[str, list[dict]]:
    lines: list[str] = []
    attachments: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").upper()
        content = message.get("content")
        parts = (
            content
            if isinstance(content, list)
            else [{"type": "text", "text": content}]
        )
        text_parts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image" and part.get("path"):
                attachments.append(
                    {
                        "kind": "image",
                        "path": str(part["path"]),
                        "mime": str(part.get("mime") or "image/png"),
                    }
                )
            elif part.get("text") is not None:
                text_parts.append(str(part.get("text") or ""))
        if text_parts:
            lines.append(f"{role}: {' '.join(text_parts)}")
    return "\n\n".join(lines) or "Analyze the attached input.", attachments


class _ChudcodeCompletionProvider:
    """Provider-shaped diagnostic adapter backed by the shared Chudcode host."""

    supports_vision = True

    def __init__(self, settings: dict[str, Any]):
        self.settings = dict(settings)
        self.name = str(settings.get("provider") or "chudcode")
        self.model = str(settings.get("model") or "")
        self._calls = 0

    def complete(
        self, system: str, messages: list[dict], max_tokens: int = 8000
    ) -> str:
        from services.agent.chudcode_host import get_default_chudcode_host

        self._calls += 1
        session_id = f"gym-diag-{id(self):x}-{self._calls}"
        request, attachments = _message_payload(messages)
        host = get_default_chudcode_host()
        try:
            result = host.run_turn(
                {
                    "request": request,
                    "run_id": session_id,
                    "session_id": session_id,
                    "system_prompt": system,
                    "host_tools": [],
                    "settings": self.settings,
                    "attachments": attachments or None,
                    "max_tokens": max_tokens,
                },
                lambda _frame: None,
            )
        finally:
            try:
                host.close_session(session_id)
            except Exception:
                pass
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (
                result.get("reason")
                if isinstance(result, dict)
                else "Chudcode returned a non-object result"
            )
            raise RuntimeError(str(reason or "Chudcode diagnostic completion failed"))
        if isinstance(result.get("result"), str):
            return result["result"]
        terminal = result.get("terminal")
        if isinstance(terminal, dict):
            for key in ("text", "result", "message", "content"):
                if isinstance(terminal.get(key), str):
                    return terminal[key]
        raise RuntimeError("Chudcode diagnostic completion returned no text")


def _completion_provider_for_tier(tier: str):
    route = _tier_execution(tier)
    if route["offline"]:
        return route["provider"]
    return _ChudcodeCompletionProvider(route["settings"])


def _benchmark_agent_fn(base_agent_fn, role_settings: dict[str, Any], offline: bool):
    """Bind one benchmark route into every DAG node context."""

    def agent_fn(request: str, context_pack: dict[str, Any]):
        scoped = dict(context_pack)
        if role_settings:
            scoped["role_model"] = dict(role_settings)
        if offline:
            scoped["offline"] = True
        return base_agent_fn(request, scoped)

    return agent_fn


def _drive_mission_pipeline(
    challenge: Challenge,
    run_id: str,
    *,
    provider,
    role_settings: dict[str, Any],
    offline: bool,
    execute_dag: bool,
) -> dict:
    """Drive the mission pipeline for ONE gym cell and return its mission record.

    The Python mission front door (``services.agent.loop.run_mission``) was removed in
    favour of the C++ core's ``agent_mission_v1``.
    The gym grades the PYTHON ledger the pipeline writes, so it cannot go through the
    core RPC; it composes the surviving deterministic primitives itself, in the same
    order and with the same phase/status vocabulary the deleted front door used:

      INTAKE (fixture ``intake_fn``) → ``validate_manifest`` → manifest + AC mirror
      into the ledger → ``PhaseMachine`` INTAKE gate → ``build_mission_graph`` →
      ``services.dag.executor.run`` when ``execute_dag``.

    With ``execute_dag=False`` the mission is PLANNED only: status ``running``, phase
    ``plan``, ``intake`` completed — the contract the gym's mock cells grade against.
    No phase past PLAN is ever reported without a DAG result behind it; this function
    never fabricates a gate verdict it did not run.
    """
    from services.agent.ledger import Ledger
    from services.agent.pipeline import (
        Phase,
        PhaseMachine,
        PipelineFlags,
        blocking_ambiguities,
        manifest_valid,
        validate_manifest,
    )
    from services.dag.mission_template import build_mission_graph

    flags = PipelineFlags.from_env()
    ledger = Ledger.load(run_id)
    ledger.set_brief(
        "pipeline", title=f"Mission {run_id}", summary=challenge.request[:400]
    )

    manifest = make_intake_fn(challenge)(challenge.request)
    findings = validate_manifest(manifest)
    finding_strs = [str(f) for f in findings]
    if not manifest_valid(findings):
        return {
            "status": "blocked",
            "phase": Phase.INTAKE.value,
            "phases_completed": [],
            "findings": finding_strs,
            "stop_reason": "manifest_invalid",
        }

    # Mirror the manifest (and every AC as a requirement) into the ledger, as the
    # front door did: ledger_get(section="manifest"/"requirements") and the gym's
    # own graders both read these, and an empty requirements list used to send
    # models thrashing (mega-2).
    ledger.add_artifact(
        "intake_analyst",
        "manifest",
        "manifest",
        path="",
        stage=Phase.INTAKE.value,
        note=f"{len(manifest.get('deliverables', []))} deliverables",
        manifest=manifest,
    )
    for deliverable in manifest.get("deliverables") or []:
        if not isinstance(deliverable, dict):
            continue
        did = deliverable.get("id") or "?"
        for ac in deliverable.get("acceptance_criteria") or []:
            if not isinstance(ac, dict):
                continue
            ac_id = ac.get("id") or "?"
            ledger.add_requirement(
                "intake_analyst",
                f"{did}/{ac_id}",
                ac.get("text") or f"{did} {ac_id}",
                source="manifest",
                status="open",
            )

    machine = PhaseMachine(ledger=ledger, flags=flags)
    machine.enter(Phase.INTAKE)
    machine.exit(
        Phase.INTAKE, machine.run_gate(Phase.INTAKE, {"manifest_findings": findings})
    )
    completed = [Phase.INTAKE.value]

    # A blocking ambiguity halts at CLARIFY — the pipeline never invents answers.
    if blocking_ambiguities(findings):
        machine.enter(Phase.CLARIFY)
        return {
            "status": "blocked",
            "phase": Phase.CLARIFY.value,
            "phases_completed": completed,
            "findings": finding_strs,
            "stop_reason": "blocking_ambiguity",
        }

    graph = build_mission_graph(
        manifest, graph_id=run_id, org_layer=flags.org_layer, default_autonomy="auto"
    )
    if not execute_dag:
        return {
            "status": "running",
            "phase": Phase.PLAN.value,
            "phases_completed": completed,
            "findings": finding_strs,
            "stop_reason": None,
        }

    from services.dag import executor as dag_exec
    from services.dag import node_runner

    # Online nodes receive provider/model identifiers and execute through the
    # shared Chudcode role runner. Explicit local tiers alone receive a Python
    # provider and the offline marker required by node_runner.
    base_agent_fn = node_runner.make_default_agent_fn(ledger=ledger, provider=provider)

    agent_fn = _benchmark_agent_fn(base_agent_fn, role_settings, offline)

    dag_result = dag_exec.run(
        graph, run_id=run_id, agent_fn=agent_fn, autonomy_default="auto"
    )
    dag_status = str(dag_result.get("status") or "")
    # Report the DAG's own verdict; do NOT claim phases whose exit gates never ran.
    status = {"done": "done", "paused": "blocked"}.get(dag_status, "failed")
    return {
        "status": status,
        "phase": Phase.PLAN.value,
        "phases_completed": completed,
        "findings": finding_strs,
        "stop_reason": None if status == "done" else f"dag_{dag_status or 'unknown'}",
        "dag_status": dag_status,
        "mode": "real",
    }


def _run_mission_real(challenge: Challenge, run_id: str, tier: str) -> dict | None:
    """Drive one real cell through Chudcode or an explicit offline local provider.

    Failures remain mission data so the benchmark records the observed cause
    instead of silently substituting a mock.
    """
    # A cell is deterministic, so a re-run must start CLEAN: the mission ledger is
    # loaded by run_id and APPENDS to whatever is on disk — a stale file from an
    # earlier attempt would contaminate the folded events. Same rule as the gym cell
    # ledger above, and same sidecar caveat: events live in the .jsonl next to it.
    try:
        from services.agent.ledger import run_ledger_path

        mp = run_ledger_path(run_id)
        for stale in (mp, mp.with_suffix(".jsonl")):
            if stale.exists():
                stale.unlink()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
    try:
        route = _tier_execution(tier)
        rec = _drive_mission_pipeline(
            challenge,
            run_id,
            provider=route["provider"],
            role_settings=route["settings"],
            offline=bool(route["offline"]),
            execute_dag=True,
        )
        rec.setdefault("mode", "real")
        return rec
    except Exception as exc:  # a real-run failure is DATA, not a crash
        return {
            "status": "error",
            "phase": "intake",
            "mode": "real",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _merge_mission_ledger(run_id: str, gym_ledger) -> None:
    """Fold the mission's own ledger into the gym cell ledger, so the real events land
    in the committed run dir.

    The mission ledger lives at ``run_ledger_path(run_id)``; the gym ledger is a
    separate file under the run dir. We copy the mission events across (skipping the
    gym's own brief) so grading/classification see the real turns.

    Read the events through ``Ledger`` rather than by parsing the json: events live in
    the ``.jsonl`` sidecar now, so reading ledger.json directly folded NOTHING."""
    try:
        from services.agent.ledger import Ledger, run_ledger_path

        mp = run_ledger_path(run_id)
        if not mp.exists() and not mp.with_suffix(".jsonl").exists():
            return
        for ev in Ledger(run_id, path=mp).events:
            if ev.get("kind") == "brief":
                continue  # the gym ledger already has its own brief
            gym_ledger.append(
                ev.get("actor", "mission"),
                ev.get("kind", "note"),
                ev.get("payload", {}),
            )
    except Exception:  # noqa: BLE001 — merge is best-effort, never fatal
        pass


def _run_mission_safe(challenge: Challenge, run_id: str) -> dict | None:
    """Drive the pipeline's INTAKE + validate + compile phases (no DAG). Never fatal."""
    try:
        return _drive_mission_pipeline(
            challenge,
            run_id,
            provider=None,
            role_settings={},
            offline=False,
            execute_dag=False,
        )
    except Exception as exc:  # a pipeline error must not sink the gym cell
        return {
            "status": "error",
            "phase": "intake",
            "error": f"{type(exc).__name__}: {exc}",
        }


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
        return {
            "cycle": self.cycle,
            "tiers": self.tiers,
            "probe": self.probe.to_dict(),
            "cells": [c.to_dict() for c in self.cells],
        }


def _default_divisions_for_tier(tier: str) -> list[str]:
    """Map a tier string to the divisions it can schedule under.

    Local tiers → LOCAL only by default; mid/frontier/openrouter/xai → OPEN
    (and LOCAL when the challenge is in the calibration subset — handled by the
    challenge.divisions filter + explicit --division CLI).
    """
    t = (tier or "").lower()
    if t.startswith("local") or t.startswith("llamacpp") or t.startswith("ollama"):
        return ["local"]
    if (
        t.startswith("openrouter:")
        or t.startswith("xai:")
        or "frontier" in t
        or t == "mid"
    ):
        return ["open"]
    return ["local", "open"]


def _model_for_tier(tier: str) -> str:
    """Exact model identity persisted with a cell; never reconstructed from paths."""
    from services.agent.tiers import (
        LOCAL_TIER_MODELS,
        load_local_models,
        parse_openrouter_tier,
        parse_xai_tier,
    )

    parameterized = parse_openrouter_tier(tier) or parse_xai_tier(tier)
    if parameterized:
        return parameterized
    ollama = _parse_ollama_tier(tier)
    if ollama:
        return ollama
    if tier.startswith("local"):
        try:
            return (
                LOCAL_TIER_MODELS.get(tier) or load_local_models().get("fanout") or tier
            )
        except Exception:
            return LOCAL_TIER_MODELS.get(tier) or tier
    return tier


def _real_unavailable_reasons(probe: ExecProbe, tiers: list[str]) -> list[str]:
    """Explain every missing prerequisite for the requested REAL tier set."""
    reasons: list[str] = []
    if not probe.core_available:
        reasons.append("core library unavailable")
    if not probe.real_agent_fn:
        reasons.append("tool-executing agent unavailable")
    if not probe.dag_deps:
        reasons.append("DAG dependencies unavailable")

    local_tiers = [
        tier for tier in tiers if _default_divisions_for_tier(tier) == ["local"]
    ]
    cloud_tiers = [tier for tier in tiers if tier not in local_tiers]
    if local_tiers and not probe.local_model:
        reasons.append("local model unavailable for: " + ", ".join(local_tiers))
    if cloud_tiers and not probe.frontier_key:
        reasons.append(
            "provider credentials unavailable for: " + ", ".join(cloud_tiers)
        )
    if not reasons and not probe.can_run_real:
        reasons.append("real execution unavailable")
    return reasons


def run_cycle(
    cycle: str = "cycle-1",
    *,
    tiers: list[str] | None = None,
    challenge_ids: list[str] | None = None,
    grind_threshold: int = 3,
    runs_dir: Path | None = None,
    real: bool = False,
    divisions: list[str] | None = None,
) -> CycleResult:
    """Run the full challenge x division x tier x seed matrix and return the cycle result.

    *runs_dir* overrides the output root (tests pass a tmp dir; the CLI leaves it None
    so the default ``reports/runs`` tree is written).

    *divisions* restricts which divisions to schedule (default: derive per tier —
    local tiers → local; frontier/openrouter → open). Calibration challenges that
    list both divisions still only run the divisions present in this list.

    *real* is an all-or-nothing request. The complete tier set is preflighted before
    any cell directory is created; unavailable infrastructure raises
    ``InfrastructureUnavailable`` rather than substituting scripted MOCK cells."""
    probe = execution_probe()
    tiers = tiers or ["local-24k", "frontier-200k"]
    challenges = load_challenges()
    if challenge_ids:
        requested = {str(challenge_id).strip() for challenge_id in challenge_ids}
        known = {challenge.id for challenge in challenges}
        unknown = sorted(requested - known)
        if unknown:
            raise UnknownChallengeError(unknown)
        challenges = [
            challenge for challenge in challenges if challenge.id in requested
        ]
    if real:
        reasons = _real_unavailable_reasons(probe, tiers)
        if reasons:
            raise InfrastructureUnavailable(reasons)
    result = CycleResult(cycle=cycle, tiers=tiers, probe=probe)
    for ch in challenges:
        ch_divs = set(getattr(ch, "divisions", ("local", "open")) or ("local", "open"))
        for tier in tiers:
            tier_divs = (
                divisions
                if divisions is not None
                else _default_divisions_for_tier(tier)
            )
            for division in tier_divs:
                if division not in ch_divs:
                    continue
                # HARD refuse: vision-eyes never under LOCAL
                if division == "local" and getattr(ch, "eyes", "either") == "vision":
                    continue
                for seed in ch.seeds:
                    result.cells.append(
                        run_cell(
                            ch,
                            tier,
                            seed,
                            cycle=cycle,
                            probe=probe,
                            grind_threshold=grind_threshold,
                            runs_dir=runs_dir,
                            real=real,
                            division=division,
                        )
                    )
    return result
