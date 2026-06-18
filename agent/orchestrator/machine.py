"""The deterministic orchestrator state machine.

CODE decides every transition; the model only fills each stage's bounded prompt. Stages:

    INTAKE -> PLAN -> MODEL (loop per plan step) -> REVIEW -> FIX (loop) -> MEMORY -> DONE

Per-stage attempt budgets, the ported ``_err_sig`` / identical-failure detector, a stagnation
guard, and a global iteration cap bound the run. PLAN failure DEGRADES to a single-step MODEL
(plan optional, like the legacy ``plan_mode``). Every transition emits the events.md §3 events via
:class:`~agent.orchestrator.events.RunEvents`.

The machine is decoupled from the live session: :func:`run_machine` takes a :class:`StageActions`
bundle of callables (one per role's effect on the world) so it can be unit-tested with a mock
provider + an in-memory document, then wired to the real ``engine.doc.session`` by ``run.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .events import RunEvents
from .state import DesignState, feature_status_errors

# --------------------------------------------------------------------------- budgets + caps

DEFAULT_BUDGETS = {
    "intake": 1,
    "plan": 2,
    "model": 4,    # per plan step
    "review": 2,
    "fix": 4,
    "memory": 1,
}

GLOBAL_ITER_CAP = 40        # hard ceiling on agent calls across the whole run
SAME_SIG_LIMIT = 3          # 3 identical error signatures in a stage -> stage fail (ported)
STAGNATION_LIMIT = 3        # 3 consecutive no-progress model steps -> stop


# --------------------------------------------------------------------------- error signature (ported)

_ERR_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception)\b")


def err_sig(err: str | None) -> str:
    """A stable signature for an error string: the exception line if present, else the last line.

    Ported from ``agent/loop.py._err_sig`` — lets 'the same error again' be told from a new one. For
    validation errors (no traceback) it falls back to the trimmed last line.
    """
    if not err:
        return ""
    lines = [ln.strip() for ln in err.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _ERR_LINE_RE.match(ln):
            return ln
    return lines[-1] if lines else ""


# --------------------------------------------------------------------------- stage results


@dataclass
class StageOutcome:
    """What an action reports back to the machine after running one attempt of a stage."""
    verdict: str                       # "pass" | "retry" | "fail" | "skip"
    reason: str = ""
    error: Optional[str] = None        # an error string -> feeds the identical-failure detector
    done: bool = False                 # reviewer's done verdict
    raw: str = ""                      # raw model output (for sidecar + capped event)
    parsed_ok: bool = True
    parse_error: Optional[str] = None
    ops_count: Optional[int] = None
    duration_ms: int = 0
    prompt_file: str = ""
    prompt_hash: str = ""
    context_chars: int = 0
    context_tokens_est: int = 0
    inputs_digest: str = ""
    tokens: Optional[dict] = None
    no_progress: bool = False          # model step produced no new geometry (stagnation signal)


# An action takes (state, attempt) and performs one attempt of the stage, returning a StageOutcome.
# It is responsible for the agent call(s) + applying effects to the world; the machine owns policy.
Action = Callable[[DesignState, int], StageOutcome]


@dataclass
class StageActions:
    intake: Action
    plan: Action
    model: Action
    review: Action
    fix: Action
    memory: Action
    # plan_steps(state) -> list[str]: the MODEL loop iterates these (one modeler call per step).
    plan_steps: Callable[[DesignState], list]


# --------------------------------------------------------------------------- the machine


STAGE_NAMES = ["intake", "plan", "model", "review", "fix", "memory"]


@dataclass
class RunControl:
    """Cooperative-cancel flag, checked at every stage boundary (agent_cancel)."""
    cancelled: bool = False


def run_machine(state: DesignState, actions: StageActions, events: RunEvents,
                budgets: dict | None = None, control: RunControl | None = None) -> dict:
    """Drive the state machine to completion. Returns ``{status, stop_reason}``.

    ``status`` is ``done`` | ``failed`` | ``cancelled``. The caller (run.py) wraps this with the
    final revision into a RunResult and emits ``run_finished``.
    """
    budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
    control = control or RunControl()
    global_calls = 0

    def over_global_cap() -> bool:
        return global_calls >= GLOBAL_ITER_CAP

    def cancelled() -> bool:
        return control.cancelled

    # ---- run ONE attempt of a stage (emit events + identical-failure detector) ----
    def run_attempt(stage: str, action: Action, role: str, attempt: int, budget: int) -> str:
        """One attempt: emits stage_started/agent_*/stage_result, returns the verdict.

        Returns ``cancelled`` / ``fail`` (on cap or identical-failure) / the action's verdict
        (``pass`` | ``retry`` | ``fail`` | ``skip``).
        """
        nonlocal global_calls
        if cancelled():
            return "cancelled"
        if over_global_cap():
            events.emit("stage_result", stage=stage, attempt=attempt,
                        verdict="fail", reason="global iteration cap reached")
            return "fail"
        state.ledger.bump(stage)
        events.emit("stage_started", stage=stage, role=role, attempt=attempt, budget=budget)
        global_calls += 1
        outcome = action(state, attempt)
        _emit_call_events(events, stage, role, attempt, outcome)

        # Identical-failure detector (ported): same error sig SAME_SIG_LIMIT times -> fail.
        if outcome.error:
            streak = state.ledger.note_sig(stage, err_sig(outcome.error))
            if streak >= SAME_SIG_LIMIT:
                events.emit("stage_result", stage=stage, attempt=attempt, verdict="fail",
                            reason=f"identical failure x{streak}: {err_sig(outcome.error)}")
                return "fail"
        else:
            state.ledger.note_sig(stage, "")

        events.emit("stage_result", stage=stage, attempt=attempt,
                    verdict=outcome.verdict, reason=outcome.reason)
        return outcome.verdict

    # ---- run a bounded stage to a terminal verdict (internal retry up to budget) ----
    def run_stage(stage: str, action: Action, role: str) -> str:
        """Run *stage*, retrying on ``retry`` up to its budget. Returns pass|fail|skip|cancelled."""
        budget = budgets.get(stage, 1)
        for attempt in range(1, budget + 1):
            v = run_attempt(stage, action, role, attempt, budget)
            if v in ("cancelled", "pass", "fail", "skip"):
                return v
            # v == "retry": loop until the budget is spent.
            if attempt >= budget:
                events.emit("stage_result", stage=stage, attempt=attempt,
                            verdict="fail", reason=f"budget exhausted ({budget})")
                return "fail"
        return "fail"

    # ---- INTAKE ----
    if cancelled():
        return {"status": "cancelled", "stop_reason": "cancelled"}
    v = run_stage("intake", actions.intake, "intake")
    if v == "cancelled":
        return {"status": "cancelled", "stop_reason": "cancelled"}
    # intake failure is non-fatal: proceed specless (modeler works off the raw request).

    # ---- PLAN (optional; failure degrades to single-step MODEL) ----
    if cancelled():
        return {"status": "cancelled", "stop_reason": "cancelled"}
    plan_v = run_stage("plan", actions.plan, "planner")
    if plan_v == "cancelled":
        return {"status": "cancelled", "stop_reason": "cancelled"}
    # Whether planned or not, the MODEL loop reads plan_steps(state) (single step if no plan).

    # ---- MODEL (loop per plan step) ----
    steps = actions.plan_steps(state) or [""]
    stagnant = 0
    for step in steps:
        if cancelled():
            return {"status": "cancelled", "stop_reason": "cancelled"}
        if over_global_cap():
            return {"status": "failed", "stop_reason": "max_iters"}
        state.current_step = step if isinstance(step, str) else str(step)
        v = run_stage("model", actions.model, "modeler")
        if v == "cancelled":
            return {"status": "cancelled", "stop_reason": "cancelled"}
        if v == "fail":
            # A step that can't be modeled (even via escape hatch) ends the run honestly.
            return {"status": "failed", "stop_reason": "model_failed"}
        # Stagnation: a step that produced no progress repeatedly -> stop the loop.
        if getattr(state, "_last_no_progress", False):
            stagnant += 1
            if stagnant >= STAGNATION_LIMIT:
                break
        else:
            stagnant = 0
    state.current_step = ""

    # ---- REVIEW -> FIX loop ----
    # REVIEW uses run_attempt (one judging call per round) so its "retry" verdict (issues found)
    # drives the FIX stage instead of being swallowed by an internal review retry. The review
    # budget bounds how many review->fix rounds we run.
    review_budget = budgets.get("review", 2)
    for review_round in range(1, review_budget + 1):
        if cancelled():
            return {"status": "cancelled", "stop_reason": "cancelled"}
        if over_global_cap():
            break
        v = run_attempt("review", actions.review, "reviewer", review_round, review_budget)
        if v == "cancelled":
            return {"status": "cancelled", "stop_reason": "cancelled"}
        # Reviewer "pass"/"skip" means done; "fail" = couldn't render/judge -> accept as-is.
        if v in ("pass", "skip", "fail"):
            break
        # v == "retry": issues found -> run the FIX stage, then re-review next round.
        fix_v = run_stage("fix", actions.fix, "fixer")
        if fix_v == "cancelled":
            return {"status": "cancelled", "stop_reason": "cancelled"}
        if fix_v == "fail":
            # Couldn't fix the issues; stop with what we have (the build is still valid).
            break

    # ---- MEMORY ----
    if not cancelled():
        run_stage("memory", actions.memory, "memory")

    if cancelled():
        return {"status": "cancelled", "stop_reason": "cancelled"}
    return {"status": "done", "stop_reason": "completed"}


def _emit_call_events(events: RunEvents, stage: str, role: str, attempt: int,
                      o: StageOutcome) -> None:
    """Emit the agent_call + agent_output pair for one attempt, persisting the raw sidecar."""
    events.emit("agent_call", stage=stage, role=role, attempt=attempt,
                prompt_file=o.prompt_file, prompt_hash=o.prompt_hash,
                context_chars=o.context_chars, context_tokens_est=o.context_tokens_est,
                inputs_digest=o.inputs_digest)
    sidecar = events.write_sidecar(stage, attempt, o.raw)
    from .events import cap_raw
    events.emit("agent_output", stage=stage, role=role, attempt=attempt,
                raw=cap_raw(o.raw), raw_path=sidecar,
                parsed_ok=o.parsed_ok, parse_error=o.parse_error,
                ops_count=o.ops_count, duration_ms=o.duration_ms, tokens=o.tokens)
