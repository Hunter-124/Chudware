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

from .challenges import Challenge, load_challenges
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

    @property
    def can_run_real(self) -> bool:
        # A genuine model-in-the-loop cycle needs a tool-executing agent AND a model.
        return self.real_agent_fn and (self.frontier_key or self.local_model)

    @property
    def mode(self) -> str:
        return "real" if self.can_run_real else "mock"

    def to_dict(self) -> dict[str, Any]:
        return {"core_available": self.core_available, "frontier_key": self.frontier_key,
                "local_model": self.local_model, "real_agent_fn": self.real_agent_fn,
                "mode": self.mode}


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
                     local_model=local, real_agent_fn=real_agent)


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge": self.challenge, "tier": self.tier, "seed": self.seed,
            "mode": self.mode, "passed": self.passed, "graded": self.graded,
            "classifications": [c.to_dict() for c in self.classifications],
            "mission_status": self.mission_status, "mission_phase": self.mission_phase,
            "run_dir": self.run_dir,
        }


def run_cell(challenge: Challenge, tier: str, seed: int, *, cycle: str,
             probe: ExecProbe, grind_threshold: int = 3,
             runs_dir: Path | None = None, real: bool = False) -> CellResult:
    """Run one (challenge, tier, seed) cell end to end and grade + classify it.

    *runs_dir* overrides the output root (default ``reports/runs``); tests pass a tmp
    dir so the suite never writes under the committed ``reports/`` tree.

    *real* (T-W30): when True AND the probe reports a real-runnable environment,
    drive the FULL pipeline with execute_dag=True, the node_runner's real
    tool-executing agent_fn, and a resolved local/frontier provider — the mission
    genuinely executes tool calls and the reviewer genuinely runs the acceptance
    checks (no scripted ledger replay).  Otherwise the cell runs in MOCK mode: the
    real phase machine to PLAN + scripted ledger turns as the gradeable signal.
    """
    from services.agent.ledger import Ledger

    real_mode = bool(real and probe.can_run_real)
    run_id = f"gym_{challenge.id}_{tier}_s{seed}"
    base = runs_dir if runs_dir is not None else (REPORTS_DIR / "runs")
    run_dir = Path(base) / cycle / f"{challenge.id}_{tier}_s{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Fresh ledger for this cell (its own file under the run dir). A cell is
    # deterministic, so a re-run must start CLEAN — the Ledger loads-from-disk if the
    # file exists (appending would double-write events on re-run). Remove any stale one.
    ledger_path = run_dir / "ledger.json"
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = Ledger(run_id, path=ledger_path)
    ledger.set_brief("pipeline", title=f"Gym {challenge.id}", summary=challenge.request[:400])

    if real_mode:
        # REAL: run the full pipeline, then FOLD the mission's own ledger (written at
        # the default run path by run_mission) into THIS gym ledger so the real
        # tool_call / agent_turn / criterion_result events are graded/classified.
        mission = _run_mission_real(challenge, run_id)
        _merge_mission_ledger(run_id, ledger)
    else:
        # MOCK: drive the phase machine to PLAN (execute_dag=False) + scripted turns.
        mission = _run_mission_safe(challenge, run_id)
        replay_turns_into_ledger(ledger, challenge, seed)

    ledger_doc = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))

    # Independent grading + taxonomy classification.
    ctx = {"challenge": challenge.id, "seed": seed, "tier": tier}
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
        run_dir=str(run_dir))

    # Persist the replayable artifacts.
    (run_dir / "graded.json").write_text(json.dumps(graded, indent=2), encoding="utf-8")
    (run_dir / "classifications.json").write_text(
        json.dumps([c.to_dict() for c in classifications], indent=2), encoding="utf-8")
    (run_dir / "mission.json").write_text(json.dumps(mission or {}, indent=2, default=str),
                                          encoding="utf-8")
    return cell


def _run_mission_real(challenge: Challenge, run_id: str) -> dict | None:
    """Drive the FULL pipeline for a REAL cell (T-W30): execute_dag=True with the
    node_runner's real tool-executing agent_fn and a resolved provider.

    Honest failure handling: a mission/DAG/model error is CAPTURED into the mission
    record (and the ledger already holds whatever real events were emitted), never
    swallowed — the C1 proof needs the observed failure class, not a hidden stub."""
    try:
        from services.agent import loop, config
        provider = config.get_provider()  # AI_CAD_PROVIDER / routing / local default
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


def run_cycle(cycle: str = "cycle-1", *, tiers: list[str] | None = None,
              challenge_ids: list[str] | None = None,
              grind_threshold: int = 3,
              runs_dir: Path | None = None,
              real: bool = False) -> CycleResult:
    """Run the full challenge x tier x seed matrix and return the cycle result.

    *runs_dir* overrides the output root (tests pass a tmp dir; the CLI leaves it None
    so the committed ``reports/runs`` tree is written for cycle-1).

    *real* (T-W30): request REAL execution (honoured per-cell only when the probe
    reports a real-runnable environment)."""
    probe = execution_probe()
    tiers = tiers or ["local-24k", "frontier-200k"]
    challenges = load_challenges()
    if challenge_ids:
        challenges = [c for c in challenges if c.id in set(challenge_ids)]

    result = CycleResult(cycle=cycle, tiers=tiers, probe=probe)
    for ch in challenges:
        for tier in tiers:
            for seed in ch.seeds:
                result.cells.append(
                    run_cell(ch, tier, seed, cycle=cycle, probe=probe,
                             grind_threshold=grind_threshold, runs_dir=runs_dir,
                             real=real))
    return result
