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

    # A "real" tool-executing agent_fn is the node_runner's default path succeeding.
    # In this repo that import points at services.agent.providers.anthropic (which does
    # not exist — the module is anthropic_provider), so the default agent_fn stubs.
    real_agent = False
    try:
        import importlib
        importlib.import_module("services.agent.providers.anthropic")
        real_agent = True
    except Exception:
        real_agent = False

    return ExecProbe(core_available=core, frontier_key=frontier,
                     local_model=local, real_agent_fn=real_agent)


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
             runs_dir: Path | None = None) -> CellResult:
    """Run one (challenge, tier, seed) cell end to end and grade + classify it.

    *runs_dir* overrides the output root (default ``reports/runs``); tests pass a tmp
    dir so the suite never writes under the committed ``reports/`` tree.
    """
    from services.agent.ledger import Ledger

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

    # Drive the real phase machine through run_mission (INTAKE + manifest validate +
    # graph compile), execute_dag=False so we do not depend on the (absent) real agent.
    mission = _run_mission_safe(challenge, run_id)

    # Replay the scripted turns into the ledger (the gradeable signal in mock mode).
    replay_turns_into_ledger(ledger, challenge, seed)

    ledger_doc = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))

    # Independent grading + taxonomy classification.
    ctx = {"challenge": challenge.id, "seed": seed, "tier": tier}
    graded = challenge.grade(ledger_doc, ctx)
    classifications = classify_ledger(
        ledger_doc, manifest=challenge.manifest, graded=graded,
        grind_threshold=grind_threshold)

    cell = CellResult(
        challenge=challenge.id, tier=tier, seed=seed, mode=probe.mode,
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
              runs_dir: Path | None = None) -> CycleResult:
    """Run the full challenge x tier x seed matrix and return the cycle result.

    *runs_dir* overrides the output root (tests pass a tmp dir; the CLI leaves it None
    so the committed ``reports/runs`` tree is written for cycle-1)."""
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
                             grind_threshold=grind_threshold, runs_dir=runs_dir))
    return result
