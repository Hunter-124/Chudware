"""Task contract for the artifact-graded agent suite.

A :class:`Task` is a model-visible prompt plus a FIXED tuple of mandatory checks. Every
check reads one declared final artifact (``final_artifact`` inside the trial workspace)
through :class:`~benchmarks.artifact_validity.evidence.Evidence`; nothing grades prose,
tool previews or a "best" document picked per criterion.

The denominator of a task is ``len(task.checks)`` and never shrinks: a check that raises
or returns garbage is recorded as ``ERROR`` under its declared id (see :func:`run_check`).
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from benchmarks.artifact_validity.evidence import Evidence


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"            # the agent's deliverable does not meet the requirement
    ERROR = "error"          # grader/runtime/infrastructure fault — not a model verdict
    UNAVAILABLE = "unavailable"  # a required capability is absent from this build


@dataclass(frozen=True)
class CheckResult:
    id: str
    status: Status
    measured: Any = None
    expected: Any = None
    tolerance: Any = None
    detail: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "status": self.status.value, "measured": self.measured,
                "expected": self.expected, "tolerance": self.tolerance,
                "detail": self.detail}

    @classmethod
    def from_json(cls, data: dict) -> "CheckResult":
        return cls(id=str(data["id"]), status=Status(data["status"]),
                   measured=data.get("measured"), expected=data.get("expected"),
                   tolerance=data.get("tolerance"), detail=str(data.get("detail") or ""))


#: A check must never raise; :func:`run_check` still converts a raise into ERROR.
Check = Callable[["Evidence"], CheckResult]


@dataclass(frozen=True)
class Task:
    id: str
    version: int
    domain: str                     # "facilities" | "cad" | "sim"
    prompt: str                     # model-visible; names the deliverable path
    checks: tuple[Check, ...]       # ALL mandatory; denominator = len(checks)
    final_artifact: str = "deliverable.step+"
    fixtures: tuple[str, ...] = ()  # under benchmarks/artifact_validity/fixtures/
    budget_s: int = 900
    notes: str = ""                 # human-only oracle derivation; never shown to the model

    def __post_init__(self) -> None:
        if not self.checks:
            raise ValueError(f"task {self.id}: a task needs at least one mandatory check")
        ids = check_ids(self)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"task {self.id}: duplicate check ids {dupes}")
        if self.final_artifact not in self.prompt:
            raise ValueError(f"task {self.id}: the prompt must name the deliverable "
                             f"{self.final_artifact!r}")


def check(check_id: str) -> Callable[[Callable[["Evidence"], CheckResult]], Check]:
    """Decorator: give a check a stable declared id and make it raise-proof.

    The declared id is what the runner reports even when the check body crashes, so a
    broken check stays in the denominator as ERROR instead of vanishing."""

    def deco(fn: Callable[["Evidence"], CheckResult]) -> Check:
        @functools.wraps(fn)
        def wrapped(ev: "Evidence") -> CheckResult:
            return run_check(fn, ev, check_id)

        wrapped.check_id = check_id  # type: ignore[attr-defined]
        return wrapped

    return deco


def declared_id(fn: Check, index: int = 0) -> str:
    """The id a check is reported under before it has run."""
    cid = getattr(fn, "check_id", None)
    if isinstance(cid, str) and cid:
        return cid
    name = getattr(fn, "__name__", "")
    return name if name and name != "<lambda>" else f"check_{index}"


def check_ids(task: Task) -> list[str]:
    return [declared_id(fn, i) for i, fn in enumerate(task.checks)]


def run_check(fn: Check, ev: "Evidence", check_id: str) -> CheckResult:
    """Run one check; any raise or non-CheckResult return becomes ERROR under ``check_id``."""
    try:
        res = fn(ev)
    except Exception as exc:  # noqa: BLE001 — a grader crash is infra, never a vanished check
        return CheckResult(check_id, Status.ERROR,
                           detail=f"check raised {type(exc).__name__}: {exc}")
    if not isinstance(res, CheckResult):
        return CheckResult(check_id, Status.ERROR,
                           detail=f"check returned {type(res).__name__}, not CheckResult")
    if not isinstance(res.status, Status):
        return CheckResult(check_id, Status.ERROR, measured=res.measured,
                           detail=f"check returned invalid status {res.status!r}")
    if res.id != check_id:
        # Reported id is the declared one; the denominator is keyed on declarations.
        return CheckResult(check_id, res.status, res.measured, res.expected,
                           res.tolerance, f"(check reported id {res.id!r}) {res.detail}")
    return res


def task_status(turn: Status, artifact_exists: bool, results: list[CheckResult],
                expected_ids: list[str]) -> Status:
    """Trial status from the classified turn outcome and the full mandatory check set.

    ``turn`` is PASS (turn completed ok), FAIL (agent-attributable: budget exhausted, no
    output, ok=false from the agent loop) or ERROR (provider/worker/infrastructure fault).

    * turn ERROR, any check ERROR, or a declared check without exactly one result -> ERROR
    * any UNAVAILABLE -> UNAVAILABLE (capability absent; never a model pass)
    * turn FAIL / artifact missing / any check FAIL -> FAIL
    * PASS only when the turn completed and every declared check passed.
    """
    got = [r.id for r in results]
    if sorted(got) != sorted(expected_ids):
        return Status.ERROR
    statuses = [r.status for r in results]
    if turn is Status.ERROR or Status.ERROR in statuses:
        return Status.ERROR
    if Status.UNAVAILABLE in statuses or turn is Status.UNAVAILABLE:
        return Status.UNAVAILABLE
    if turn is not Status.PASS or not artifact_exists or Status.FAIL in statuses:
        return Status.FAIL
    return Status.PASS
