"""Turn decoding, artifact grading and aggregation for the artifact-graded task suite.

* :func:`decode_turn` reads the terminal record ``scripts/run_mission.py --result-json``
  writes (the current ``agent_admin_turn`` response: ``turn.result`` is terminal TEXT, tool
  activity arrives as ``agent.tool_call`` events) and classifies the turn PASS/FAIL/ERROR.
* :func:`grade` reopens ONE declared artifact and runs EVERY mandatory check of a task;
  ``python -m benchmarks.artifact_validity.scoring grade`` is the fresh grader process the
  runner launches. :func:`grade_in_subprocess` keeps the denominator when that process dies.
* :func:`summarize`/:func:`format_report` aggregate trial manifests per task × model.

Prose is never graded; tool previews in the event log are diagnostics, never evidence.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.artifact_validity.tasks.schema import (
    CheckResult, Status, Task, check_ids, declared_id, run_check, task_status,
)

REPO = Path(__file__).resolve().parents[2]
VENV_PY = REPO / ".venv-mcp" / "bin" / "python"

#: Host stop reasons that mean the provider/runtime failed, not the agent.
_INFRA_STOPS = ("provider_error", "provider_auth", "provider_unavailable")
_INFRA_TEXT = re.compile(r"\b(429|401|403|5\d\d)\b|rate.?limit|quota|overloaded|"
                         r"unauthori[sz]ed|authenticat|credential|provider (error|unavailable)|"
                         r"no endpoints|connection (reset|refused)", re.I)


# ---------------------------------------------------------------- turn decoding


@dataclass
class TurnOutcome:
    status: Status          # PASS = completed ok; FAIL = agent-attributable; ERROR = infra
    reason: str
    verdict: str | None = None          # run_mission's own driver verdict
    ok: bool | None = None
    stop_reason: str | None = None
    terminal: bool = False
    cancelled: bool = False
    text: str = ""
    usage: dict | None = None           # None = unknown (never coerced to zero)
    persistence: dict | None = None
    finalization_errors: list = field(default_factory=list)
    chudcode_session_file: str | None = None
    started_at: float | None = None
    ended_at: float | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def _infra(text: str) -> bool:
    return bool(_INFRA_TEXT.search(text or ""))


def decode_turn(capture: dict | None, *, driver_returncode: int | None = None,
                driver_timed_out: bool = False) -> TurnOutcome:
    """Classify one executor run from its ``--result-json`` record."""
    if not isinstance(capture, dict):
        why = ("bench driver timed out waiting for run_mission" if driver_timed_out
               else f"run_mission wrote no result record (exit {driver_returncode})")
        return TurnOutcome(Status.ERROR, why)
    resp = capture.get("response") if isinstance(capture.get("response"), dict) else {}
    turn = resp.get("turn") if isinstance(resp.get("turn"), dict) else {}
    raw_text = turn.get("result")
    text = raw_text if isinstance(raw_text, str) else (
        json.dumps(raw_text, default=str) if raw_text is not None else "")
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else (
        turn.get("usage") if isinstance(turn.get("usage"), dict) else None)
    if usage is not None and not usage.get("calls") and not usage.get("total_tokens"):
        # The worker accumulator saw no provider_usage event: unknown, not zero.
        usage = None
    out = TurnOutcome(
        Status.PASS, "completed",
        verdict=capture.get("verdict"),
        ok=resp.get("ok") if "ok" in resp else None,
        stop_reason=str(resp.get("stop_reason") or turn.get("stop_reason") or "") or None,
        terminal=bool(turn.get("terminal")),
        cancelled=bool(turn.get("cancelled")),
        text=text,
        usage=usage,
        persistence=resp.get("persistence") if isinstance(resp.get("persistence"), dict)
        else None,
        finalization_errors=list(resp.get("finalization_errors") or []),
        chudcode_session_file=turn.get("chudcode_session_file"),
        started_at=capture.get("started_at"),
        ended_at=capture.get("ended_at"),
    )
    verdict = str(capture.get("verdict") or "")
    detail = str(capture.get("verdict_detail") or "")
    reason = str(resp.get("reason") or detail)
    stop = out.stop_reason or ""

    if driver_timed_out:
        out.status, out.reason = Status.ERROR, "bench driver timed out waiting for run_mission"
    elif verdict == "driver_failure":
        out.status, out.reason = Status.ERROR, f"executor failure: {detail}"
    elif resp.get("ok") is not True:
        if stop == "turn_budget":
            out.status, out.reason = Status.FAIL, f"turn budget exhausted: {reason}"
        elif stop in _INFRA_STOPS or _infra(reason):
            out.status, out.reason = Status.ERROR, f"provider/runtime failure: {reason}"
        else:
            out.status, out.reason = Status.FAIL, f"turn not ok: {reason}"
    elif verdict == "not_terminal" or not out.terminal:
        out.status, out.reason = Status.ERROR, "turn ended without a terminal agent_end"
    elif out.cancelled or verdict == "cancelled":
        out.status, out.reason = Status.ERROR, f"turn cancelled (stop_reason={stop})"
    elif verdict == "run_failed":
        out.status = Status.ERROR if _infra(detail) else Status.FAIL
        out.reason = f"run failed: {detail}"
    elif verdict == "no_output":
        prov = str(capture.get("provider_error") or "")
        out.status = Status.ERROR if prov else Status.FAIL
        out.reason = f"no assistant output{f' (provider: {prov})' if prov else ''}"
    elif verdict != "ok":
        out.status, out.reason = Status.ERROR, f"unknown executor verdict {verdict!r}"
    return out


def session_usage(sessions_dir: Path) -> dict | None:
    """Token usage and price summed over every assistant message of the trial's Chudcode
    session files (main + subagents). None when no message carries usage (unknown, never
    zero). ``cost_usd`` is Chudcode's per-message price estimate at the model's API rate;
    subscription billing differs, and a message without a price leaves it None."""
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total_tokens": 0}
    cost: float | None = 0.0
    messages = 0
    models: set[str] = set()
    files = sorted(sessions_dir.rglob("*.jsonl")) if sessions_dir.is_dir() else []
    for f in files:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message") if isinstance(rec, dict) else None
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                messages += 1
                for src, dst in (("input", "input"), ("output", "output"),
                                 ("cacheRead", "cache_read"), ("cacheWrite", "cache_write"),
                                 ("totalTokens", "total_tokens")):
                    v = u.get(src)
                    if isinstance(v, (int, float)):
                        totals[dst] += int(v)
                price = (u.get("cost") or {}).get("total") if isinstance(u.get("cost"), dict) \
                    else None
                if isinstance(price, (int, float)) and cost is not None:
                    cost += float(price)
                else:
                    cost = None
                if msg.get("model"):
                    models.add(f"{msg.get('provider') or ''}/{msg['model']}".lstrip("/"))
    if not messages:
        return None
    return {**totals, "messages": messages, "session_files": len(files),
            "models": sorted(models), "cost_usd": round(cost, 6) if cost is not None else None}


def summarize_events(path: Path) -> dict:
    """Diagnostic digest of the executor's event JSONL (never grading evidence)."""
    kinds: dict[str, int] = {}
    calls: list[dict] = []
    runtime: set[tuple[str, str]] = set()
    finished: dict | None = None
    bad_lines = 0
    if not path.is_file():
        return {"present": False}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(d, dict):
                continue
            kind = str(d.get("kind") or "")
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == "agent.tool_call":
                calls.append({"tool": d.get("tool_name"), "ok": bool(d.get("ok")),
                              "latency_ms": d.get("latency_ms")})
            elif kind in ("agent.context", "agent.provider_usage") and d.get("model"):
                runtime.add((str(d.get("provider") or ""), str(d["model"])))
            elif kind == "agent.model_assignment" and d.get("picked_model"):
                runtime.add((str(d.get("picked_provider") or ""), str(d["picked_model"])))
            elif kind == "run_finished":
                finished = {k: d.get(k) for k in ("status", "stop_reason", "error_detail")}
    return {
        "present": True, "kinds": kinds, "bad_lines": bad_lines,
        "tool_calls": len(calls), "tool_calls_failed": sum(1 for c in calls if not c["ok"]),
        "tools_used": sorted({str(c["tool"]) for c in calls}),
        "calls": calls,
        "runtime_models": sorted(f"{p}/{m}" if p else m for p, m in runtime),
        "run_finished": finished,
    }


# ---------------------------------------------------------------- grading


def resolve_task(ref: str) -> Task:
    """A registry id (``F1``) or ``<module-or-file.py>:ATTR`` naming a Task object."""
    if ":" in ref:
        mod_ref, attr = ref.rsplit(":", 1)
        if mod_ref.endswith(".py"):
            spec = importlib.util.spec_from_file_location(
                f"_av_task_{abs(hash(mod_ref))}", mod_ref)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {mod_ref}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        else:
            module = importlib.import_module(mod_ref)
        task = getattr(module, attr)
    else:
        from benchmarks.artifact_validity.tasks import REGISTRY

        if ref not in REGISTRY:
            raise KeyError(f"unknown task {ref!r}; registered: {sorted(REGISTRY)}")
        task = REGISTRY[ref]
    if not isinstance(task, Task):
        raise TypeError(f"{ref} is not a Task")
    return task


def _engine_identity() -> dict:
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415

        from benchmarks.artifact_validity.provenance import binary_identity  # noqa: PLC0415

        return {"daemon": os.environ.get("CHUDWARE_ENGINE_DAEMON"),
                "lib": binary_identity(cb._resolve_lib())}
    except Exception as exc:  # noqa: BLE001
        return {"daemon": os.environ.get("CHUDWARE_ENGINE_DAEMON"),
                "error": f"{type(exc).__name__}: {exc}"}


def grade(task: Task, workspace: Path) -> dict:
    """Open ``workspace/task.final_artifact`` once and run every mandatory check on it."""
    from benchmarks.artifact_validity.evidence import build_evidence  # noqa: PLC0415

    t0 = time.monotonic()
    ev = build_evidence(Path(workspace) / task.final_artifact)
    results = [run_check(fn, ev, declared_id(fn, i)) for i, fn in enumerate(task.checks)]
    return {
        "task": task.id,
        "evidence": ev.summary(),
        "checks": [r.to_json() for r in results],
        "grader": {"pid": os.getpid(), "elapsed_s": round(time.monotonic() - t0, 3),
                   "engine": _engine_identity()},
    }


def error_grade(task: Task, why: str) -> dict:
    """Every declared check as ERROR — the denominator survives a dead grader."""
    return {"task": task.id, "evidence": None,
            "checks": [CheckResult(cid, Status.ERROR, detail=why).to_json()
                       for cid in check_ids(task)],
            "grader": {"error": why}}


def grader_env() -> dict[str, str]:
    env = dict(os.environ)
    env["CHUDWARE_ENGINE_DAEMON"] = "0"
    for k in ("CHUDWARE_INSTANCE_KEY", "CHUDWARE_HOSTD_SOCKET"):
        env.pop(k, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
    return env


def grade_in_subprocess(task: Task, task_ref: str, workspace: Path, out: Path,
                        timeout_s: float = 900.0) -> dict:
    """Grade in a FRESH process (own in-process engine); never raises."""
    py = str(VENV_PY if VENV_PY.exists() else Path(sys.executable))
    cmd = [py, "-X", "utf8", "-m", "benchmarks.artifact_validity.scoring", "grade",
           "--task", task_ref, "--workspace", str(workspace), "--out", str(out)]
    try:
        proc = subprocess.run(cmd, cwd=str(REPO), env=grader_env(), capture_output=True,
                              text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return error_grade(task, f"grader timed out after {timeout_s:.0f}s")
    except OSError as exc:
        return error_grade(task, f"grader did not start: {exc}")
    if proc.returncode != 0 or not out.is_file():
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        return error_grade(task, f"grader exit {proc.returncode}: {tail}")
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return error_grade(task, f"grader output unreadable: {exc}")
    return data


def artifact_freshness(evidence: dict | None, started_at: float | None,
                       input_hashes: set[str]) -> tuple[bool, str]:
    """Was the declared artifact produced by THIS trial? (exists, newer than the trial
    start, and not byte-identical to a supplied fixture)."""
    if not evidence or not evidence.get("exists"):
        return False, "declared artifact missing"
    if evidence.get("sha256") in input_hashes:
        return False, "declared artifact is an unmodified input fixture"
    mtime = evidence.get("mtime")
    if started_at is not None and (mtime is None or mtime < started_at):
        return False, "declared artifact predates the trial"
    return True, ""


def trial_verdict(task: Task, turn: TurnOutcome, grade_data: dict, started_at: float | None,
                  input_hashes: set[str]) -> dict:
    """Combine turn outcome + artifact freshness + every declared check into one status."""
    expected = check_ids(task)
    results: list[CheckResult] = []
    malformed = ""
    for row in grade_data.get("checks") or []:
        try:
            results.append(CheckResult.from_json(row))
        except (KeyError, TypeError, ValueError) as exc:
            malformed = f"malformed check row {row!r}: {exc}"
    fresh, why = artifact_freshness(grade_data.get("evidence"), started_at, input_hashes)
    status = task_status(turn.status, fresh, results, expected)
    reasons = []
    if malformed:
        reasons.append(malformed)
    if sorted(r.id for r in results) != sorted(expected):
        reasons.append(f"check set mismatch: got {[r.id for r in results]} "
                       f"expected {expected}")
    if turn.status is not Status.PASS:
        reasons.append(f"turn {turn.status.value}: {turn.reason}")
    if not fresh:
        reasons.append(why)
    reasons += [f"{r.id}: {r.status.value} {r.detail}".strip() for r in results
                if r.status is not Status.PASS]
    return {"status": status.value, "artifact_fresh": fresh, "reasons": reasons,
            "checks_total": len(expected),
            "checks_passed": sum(1 for r in results if r.status is Status.PASS)}


# ---------------------------------------------------------------- aggregation


def summarize(manifests: list[dict]) -> dict:
    """Per task × model counts. PASS rate is over SCORED trials (PASS+FAIL); ERROR and
    UNAVAILABLE trials are reported separately and never count as passes."""
    cells: dict[tuple[str, str], dict] = {}
    for m in manifests:
        key = (str(m.get("task", {}).get("id")), str(m.get("model", {}).get("label")))
        cell = cells.setdefault(key, {"task": key[0], "model": key[1], "trials": 0,
                                      "pass": 0, "fail": 0, "error": 0, "unavailable": 0})
        cell["trials"] += 1
        status = str((m.get("verdict") or {}).get("status") or "error")
        cell[status if status in cell else "error"] += 1
    by_model: dict[str, list[float]] = {}
    for cell in cells.values():
        scored = cell["pass"] + cell["fail"]
        cell["scored"] = scored
        cell["rate"] = cell["pass"] / scored if scored else None
        if cell["rate"] is not None:
            by_model.setdefault(cell["model"], []).append(cell["rate"])
    macro = {model: sum(v) / len(v) for model, v in by_model.items()}
    return {"cells": sorted(cells.values(), key=lambda c: (c["task"], c["model"])),
            "macro_pass_rate": macro}


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) <= 60 else s[:57] + "..."


def format_report(manifests: list[dict]) -> str:
    s = summarize(manifests)
    lines = ["artifact_validity — per task × model (k = PASS, N = scored PASS+FAIL; "
             "ERROR/UNAVAILABLE are not model verdicts)", ""]
    lines.append(f"{'task':<8} {'model':<44} {'k/N':>7} {'err':>4} {'unav':>4} {'trials':>6}")
    for c in s["cells"]:
        lines.append(f"{c['task']:<8} {c['model']:<44} {c['pass']:>3}/{c['scored']:<3} "
                     f"{c['error']:>4} {c['unavailable']:>4} {c['trials']:>6}")
    if s["macro_pass_rate"]:
        lines.append("")
        for model, rate in sorted(s["macro_pass_rate"].items()):
            lines.append(f"macro pass rate {model}: {rate:.3f}")
    lines.append("")
    for m in sorted(manifests, key=lambda m: (m.get("task", {}).get("id", ""),
                                              m.get("model", {}).get("label", ""),
                                              m.get("replicate", 0))):
        v = m.get("verdict") or {}
        turn = m.get("turn") or {}
        lines.append(f"[{str(v.get('status', '?')).upper():<11}] {m['task']['id']} "
                     f"{m['model']['label']} rep{m.get('replicate')} "
                     f"checks {v.get('checks_passed')}/{v.get('checks_total')} "
                     f"turn={turn.get('status')} ({_fmt(turn.get('reason') or '')}) "
                     f"wall={m.get('timing', {}).get('wall_s')}s")
        for c in (m.get("grade") or {}).get("checks") or []:
            lines.append(f"    {c['status']:<11} {c['id']:<28} measured={_fmt(c.get('measured'))}"
                         f" expected={_fmt(c.get('expected'))} tol={_fmt(c.get('tolerance'))}"
                         + (f"  — {c['detail']}" if c.get("detail") else ""))
        if not v.get("artifact_fresh", True):
            lines.append("    artifact: " + "; ".join(
                r for r in v.get("reasons", []) if "artifact" in r))
    return "\n".join(lines)


# ---------------------------------------------------------------- grader process


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="artifact_validity.scoring")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grade", help="grade one workspace (run in a fresh process)")
    g.add_argument("--task", required=True, help="registry id or module:ATTR")
    g.add_argument("--workspace", required=True)
    g.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"
    task = resolve_task(args.task)
    data = grade(task, Path(args.workspace))
    Path(args.out).write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
