#!/usr/bin/env python3
"""artifact_validity runner — artifact-graded trials of the in-app agent.

ONE executor: every trial is one ``scripts/run_mission.py`` subprocess (one headless
``agent_admin_turn`` in a fresh workspace, sessions dir and private engine instance).
ONE grader: a fresh ``python -m benchmarks.artifact_validity.scoring grade`` process
reopens the task's declared final artifact in an in-process engine and runs EVERY
mandatory check. Nothing grades prose.

    python -m benchmarks.artifact_validity.runner run --tag T --tasks F1,C1 \\
        --model openai-codex:gpt-5.5:high --replicates 3 [--dry-run]
    python -m benchmarks.artifact_validity.runner rescore --tag T
    python -m benchmarks.artifact_validity.runner report --tag T [--rescored]

Trials live in ``benchmarks/artifact_validity/runs/<tag>/<task>/<model>/<rep>/`` (git-
ignored). A trial directory is never reused: a completed trial (manifest.json present) is
skipped, a partial one aborts the run. See README.md for statuses and limits.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from benchmarks.artifact_validity import provenance as prov  # noqa: E402
from benchmarks.artifact_validity.scoring import (  # noqa: E402
    TurnOutcome, decode_turn, format_report, grade_in_subprocess, session_usage,
    summarize_events, trial_verdict,
)
from benchmarks.artifact_validity.tasks.schema import Task, check_ids  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
#: Live trial workspaces. They MUST sit outside every project-marked folder: the turn's
#: project root is the OUTERMOST ancestor carrying .plm/.aicadvault/.chudware
#: (services/agent/project_context.resolve_project_root), and this repo carries them, so
#: a workspace under runs/ would hand the agent the whole repository as its cwd. After
#: the turn the workspace is bundled into the trial record (``runs/.../work``).
WORKSPACES = Path(os.environ.get("CHUDWARE_AVBENCH_WORKSPACES")
                  or Path.home() / ".cache" / "chudware-avbench")
FIXTURES = HERE / "fixtures"
RUN_MISSION = _REPO / "scripts" / "run_mission.py"
MANIFEST_SCHEMA = "chudware.artifact_validity.trial/2"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
#: Seconds the executor subprocess may outlive the task budget before the bench kills it
#: (run_mission's own RPC wait is budget + 60 s; worker shutdown adds up to ~20 s).
DRIVER_SLACK_S = 240.0


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    effort: str = ""

    @property
    def label(self) -> str:
        return ":".join(p for p in (self.provider, self.model, self.effort) if p)

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", self.label.replace(":", "__")).strip("-")


def parse_model(spec: str) -> ModelSpec:
    """``provider:model[:effort]``; the effort suffix is recognised only if it is a known
    effort level, so model ids containing ``:`` survive."""
    provider, sep, rest = spec.partition(":")
    if not sep or not provider or not rest:
        raise argparse.ArgumentTypeError(
            f"--model must be provider:model[:effort], got {spec!r}")
    model, effort = rest, ""
    head, sep2, tail = rest.rpartition(":")
    if sep2 and head and tail in EFFORTS:
        model, effort = head, tail
    return ModelSpec(provider, model, effort)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _default_hostd() -> str:
    try:
        from services.mcp.daemon_bridge import resolve_hostd_bin

        p = resolve_hostd_bin()
        return str(p) if p and Path(p).is_file() else ""
    except Exception:  # noqa: BLE001 — resolution is best effort; the run records "none"
        return ""


def _driver_python() -> str:
    venv = _REPO / ".venv-mcp" / "bin" / "python"
    return str(venv if venv.exists() else Path(sys.executable))


def trial_dir(tag: str, task: Task, model: ModelSpec, rep: int) -> Path:
    return RUNS / tag / task.id / model.slug / f"{rep:02d}"


def live_workspace(tag: str, task: Task, model: ModelSpec, rep: int) -> Path:
    return WORKSPACES / tag / task.id / model.slug / f"{rep:02d}"


def mission_command(trial: Path, workspace: Path, task: Task, model: ModelSpec, *,
                    tag: str, rep: int, hostd: str) -> list[str]:
    """The exact run_mission.py invocation for one trial."""
    key = f"avbench-{tag}-{task.id}-{model.slug}-{rep:02d}"
    cmd = [_driver_python(), str(RUN_MISSION),
           "--workspace", str(workspace),
           "--sessions-dir", str(trial / "sessions"),
           "--prompt-file", str(trial / "prompt.txt"),
           "--provider", model.provider, "--model", model.model]
    if model.effort:
        cmd += ["--reasoning-effort", model.effort]
    cmd += ["--mode", "agent", "--role", "general_agent",
            "--session-id", key, "--instance-key", key]
    if hostd:
        cmd += ["--hostd-bin", hostd]
    cmd += ["--log", str(trial / "events.jsonl"),
            "--timeout-s", str(task.budget_s),
            "--quiet-worker",
            "--result-json", str(trial / "result.json")]
    return cmd


def executor_env() -> dict[str, str]:
    env = dict(os.environ)
    # The private instance key alone must decide which daemon the turn uses: an inherited
    # socket path would attach the trial to someone else's (possibly the user's) daemon.
    env.pop("CHUDWARE_HOSTD_SOCKET", None)
    # The private daemon exits shortly after its last client instead of lingering.
    env.setdefault("CHUDWARE_HOSTD_LINGER_SECS", "20")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _input_hashes(task: Task) -> set[str]:
    return {h for h in (prov.sha256_path(FIXTURES / f) for f in task.fixtures) if h}


def _workspace_inventory(work: Path) -> list[dict]:
    """Every file the trial left (diagnostic: extra/stale artifacts are visible, never
    graded)."""
    out = []
    if not work.is_dir():
        return out
    for p in sorted(work.rglob("*")):
        if p.is_file():
            rel = p.relative_to(work).as_posix()
            row = {"path": rel, "bytes": p.stat().st_size}
            if p.suffix.lower() in (".step+", ".step", ".stp") or p.name.endswith(".step+"):
                row["sha256"] = prov.sha256_path(p)
            out.append(row)
    return out


def workspace_root_fault(workspace: Path) -> str:
    """Why the agent would NOT get ``workspace`` itself as its project root ('' if it
    would)."""
    from services.agent.project_context import resolve_project_root

    root = Path(resolve_project_root(workspace)).resolve()
    if root != workspace.resolve():
        return (f"workspace {workspace} resolves to project root {root} (an ancestor "
                f"carries a .plm/.aicadvault/.chudware marker); set "
                f"CHUDWARE_AVBENCH_WORKSPACES to a location outside any project")
    return ""


def prepare_trial(trial: Path, workspace: Path, task: Task) -> None:
    """Fresh trial record (sessions/, prompt.txt) and fresh live workspace with the task
    fixtures copied in. Neither may pre-exist."""
    for d in (trial, workspace):
        if d.exists():
            raise SystemExit(f"refusing to reuse existing trial directory {d}; "
                             f"pick a new --tag")
    workspace.mkdir(parents=True)
    fault = workspace_root_fault(workspace)
    if fault:
        shutil.rmtree(workspace)
        raise SystemExit(fault)
    (trial / "sessions").mkdir(parents=True)
    for name in task.fixtures:
        src = FIXTURES / name
        if not src.is_file():
            raise SystemExit(f"task {task.id}: fixture missing: {src}")
        shutil.copy2(src, workspace / Path(name).name)
    (trial / "prompt.txt").write_text(task.prompt.strip() + "\n", encoding="utf-8")


def bundle_workspace(workspace: Path, trial: Path) -> None:
    """Move the live workspace into the trial record (metadata/mtimes preserved) so the
    record is self-contained and ``rescore`` grades exactly what the turn left."""
    shutil.copytree(workspace, trial / "work", copy_function=shutil.copy2, symlinks=True)
    shutil.rmtree(workspace)


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_trial(*, tag: str, task: Task, model: ModelSpec, rep: int, hostd: str,
              executor_program: list[str] | None = None,
              grade_timeout_s: float = 900.0) -> dict:
    """Execute + grade one trial and write its manifest. ``executor_program`` replaces the
    ``[python, scripts/run_mission.py]`` program while keeping the exact run_mission
    argument list (offline tests only); production always runs run_mission.py."""
    trial = trial_dir(tag, task, model, rep)
    workspace = live_workspace(tag, task, model, rep)
    prepare_trial(trial, workspace, task)
    cmd = mission_command(trial, workspace, task, model, tag=tag, rep=rep, hostd=hostd)
    if executor_program:
        cmd = list(executor_program) + cmd[2:]
    env = executor_env()
    started_iso, started = _now(), time.time()
    timed_out, rc = False, None
    with (trial / "driver.log").open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, cwd=str(_REPO), env=env, stdout=log,
                                  stderr=subprocess.STDOUT,
                                  timeout=task.budget_s + DRIVER_SLACK_S)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    ended = time.time()
    bundle_workspace(workspace, trial)
    turn = decode_turn(_load_json(trial / "result.json"), driver_returncode=rc,
                       driver_timed_out=timed_out)
    return finalize_trial(trial, task=task, model=model, rep=rep, tag=tag, hostd=hostd,
                          cmd=cmd, env=env, turn=turn, started=started,
                          started_iso=started_iso, ended=ended, driver_returncode=rc,
                          driver_timed_out=timed_out, grade_timeout_s=grade_timeout_s,
                          live_workspace=workspace)


def finalize_trial(trial: Path, *, task: Task, model: ModelSpec, rep: int, tag: str,
                   hostd: str, cmd: list[str], env: dict[str, str], turn: TurnOutcome,
                   started: float, started_iso: str, ended: float,
                   driver_returncode: int | None, driver_timed_out: bool,
                   grade_timeout_s: float, live_workspace: Path) -> dict:
    grade = grade_in_subprocess(task, task.id if _registered(task) else _task_ref(task),
                                trial / "work", trial / "grade.json", grade_timeout_s)
    verdict = trial_verdict(task, turn, grade, started, _input_hashes(task))
    session = session_usage(trial / "sessions")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "tag": tag,
        "trial_dir": str(trial),
        "replicate": rep,
        "task": {"id": task.id, "version": task.version, "domain": task.domain,
                 "final_artifact": task.final_artifact, "budget_s": task.budget_s,
                 "fixtures": {f: prov.sha256_path(FIXTURES / f) for f in task.fixtures},
                 "task_sha256": prov.task_hash(task),
                 "scorer": prov.scorer_hash(task),
                 "check_ids": check_ids(task)},
        "model": {"label": model.label, "provider": model.provider, "model": model.model,
                  "effort": model.effort or None},
        "executor": {"command": cmd, "driver_returncode": driver_returncode,
                     "driver_timed_out": driver_timed_out,
                     "env": prov.env_snapshot(env), "mode": "agent",
                     "role": "general_agent",
                     "live_workspace": str(live_workspace),
                     "memory_policy": "per-project memory keyed by a fresh per-trial "
                                      "workspace outside any project; no resume"},
        "repo": prov.repo_state(),
        "binaries": {"hostd": prov.binary_identity(hostd or None),
                     "hostd_stale_core_sources": prov.stale_core_sources(hostd or None)},
        "runtime": prov.runtime_identity(),
        "timing": {"started": started_iso, "ended": _now(), "started_epoch": started,
                   "wall_s": round(ended - started, 1)},
        "turn": turn.to_json(),
        "usage": {
            # None = unknown; never coerced to zero.
            "worker_tokens": turn.usage,
            "session": session,
            "cost_usd": (session or {}).get("cost_usd"),
        },
        "events": summarize_events(trial / "events.jsonl"),
        "artifact": {
            "path": task.final_artifact,
            **{k: (grade.get("evidence") or {}).get(k)
               for k in ("exists", "sha256", "size_bytes", "mtime", "open_error")},
        },
        "workspace_files": _workspace_inventory(trial / "work"),
        "grade": grade,
        "verdict": verdict,
    }
    (trial / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str),
                                         encoding="utf-8")
    return manifest


def _registered(task: Task) -> bool:
    from benchmarks.artifact_validity.tasks import REGISTRY

    return REGISTRY.get(task.id) is task


def _task_ref(task: Task) -> str:
    """``<file.py>:ATTR`` for an unregistered (test) task so the grader process can load
    it regardless of the module name its importer used."""
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path or not path.endswith(".py"):
            continue
        for attr, value in list(vars(module).items()):
            if value is task:
                return f"{path}:{attr}"
    raise LookupError(f"task {task.id} is neither registered nor a module attribute")


# ---------------------------------------------------------------- CLI


def _select(ids: str) -> list[Task]:
    from benchmarks.artifact_validity.tasks import REGISTRY

    wanted = [s.strip() for s in ids.split(",") if s.strip()]
    missing = [i for i in wanted if i not in REGISTRY]
    if missing:
        raise SystemExit(f"unknown task id(s) {missing}; registered: {sorted(REGISTRY)}")
    return [REGISTRY[i] for i in wanted]


def _plan(tasks: list[Task], models: list[ModelSpec], reps: int):
    """Sequential plan blocked by task and replicate; model order rotates per replicate so
    no model always runs first."""
    for task in tasks:
        for rep in range(1, reps + 1):
            k = (rep - 1) % len(models)
            for model in models[k:] + models[:k]:
                yield task, model, rep


def _refuse_stale(hostd: str, allow: bool) -> None:
    if not hostd:
        msg = ("no chudware-hostd binary resolved; the turn's engine identity would be "
               "unrecorded. Pass --hostd-bin.")
        if not allow:
            raise SystemExit(msg)
        print("WARNING: " + msg, flush=True)
        return
    stale = prov.stale_core_sources(hostd) or []
    if stale and not allow:
        raise SystemExit(f"ENGINE IS STALE: core sources newer than {hostd}: {stale[:3]}. "
                         f"Rebuild, or pass --allow-stale-engine to record a knowingly "
                         f"stale run.")
    if stale:
        print(f"WARNING: stale engine ({len(stale)}+ newer core sources)", flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    tasks = _select(args.tasks)
    models = args.model
    hostd = args.hostd_bin or _default_hostd()
    plan = list(_plan(tasks, models, args.replicates))
    if args.dry_run:
        for task, model, rep in plan:
            trial = trial_dir(args.tag, task, model, rep)
            print(f"# {task.id} {model.label} rep{rep} -> {trial}")
            print(" ".join(_shell_quote(c) for c in
                           mission_command(trial, live_workspace(args.tag, task, model, rep),
                                           task, model, tag=args.tag, rep=rep,
                                           hostd=hostd)))
        return 0
    _refuse_stale(hostd, args.allow_stale_engine)
    manifests = []
    for i, (task, model, rep) in enumerate(plan, 1):
        trial = trial_dir(args.tag, task, model, rep)
        if (trial / "manifest.json").is_file():
            print(f"[{i}/{len(plan)}] {task.id} {model.label} rep{rep}: done, skipping",
                  flush=True)
            manifests.append(_load_json(trial / "manifest.json"))
            continue
        print(f"[{i}/{len(plan)}] {task.id} {model.label} rep{rep} -> {trial}", flush=True)
        m = run_trial(tag=args.tag, task=task, model=model, rep=rep, hostd=hostd)
        v = m["verdict"]
        print(f"    {v['status'].upper()} checks {v['checks_passed']}/{v['checks_total']} "
              f"turn={m['turn']['status']} wall={m['timing']['wall_s']}s", flush=True)
        manifests.append(m)
    print()
    print(format_report([m for m in manifests if m]))
    return 0


def _shell_quote(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z0-9_./:=+,@-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def _trial_manifests(tag: str) -> list[Path]:
    root = RUNS / tag
    if not root.is_dir():
        raise SystemExit(f"no trials for tag {tag!r} under {root}")
    return sorted(root.glob("*/*/*/manifest.json"))


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-grade every saved trial's declared artifact with the CURRENT graders. The
    original manifest is never modified; the rescore lands in ``rescores/<utc>.json``."""
    from benchmarks.artifact_validity.tasks import REGISTRY

    out = []
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in _trial_manifests(args.tag):
        m = _load_json(path)
        if not m:
            continue
        task = REGISTRY.get(m["task"]["id"])
        if task is None:
            print(f"skip {path}: task {m['task']['id']} no longer registered", flush=True)
            continue
        trial = path.parent
        rdir = trial / "rescores"
        rdir.mkdir(exist_ok=True)
        grade = grade_in_subprocess(task, task.id, trial / "work",
                                    rdir / f"{stamp}.grade.json")
        executor = m.get("executor") or {}
        turn = decode_turn(_load_json(trial / "result.json"),
                           driver_returncode=executor.get("driver_returncode"),
                           driver_timed_out=bool(executor.get("driver_timed_out")))
        started = (m.get("timing") or {}).get("started_epoch")
        verdict = trial_verdict(task, turn, grade, started, _input_hashes(task))
        r = dict(m)
        r.update({"rescored_at": _now(), "grade": grade, "verdict": verdict,
                  "task": {**m["task"], "task_sha256": prov.task_hash(task),
                           "scorer": prov.scorer_hash(task), "check_ids": check_ids(task)},
                  "rescored_from": str(path)})
        (rdir / f"{stamp}.json").write_text(json.dumps(r, indent=1, default=str),
                                            encoding="utf-8")
        out.append(r)
        print(f"rescored {trial}: {verdict['status']}", flush=True)
    print()
    print(format_report(out))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    manifests = []
    for path in _trial_manifests(args.tag):
        chosen = path
        if args.rescored:
            latest = sorted((path.parent / "rescores").glob("*Z.json")) \
                if (path.parent / "rescores").is_dir() else []
            if latest:
                chosen = latest[-1]
        m = _load_json(chosen)
        if m:
            manifests.append(m)
    print(format_report(manifests))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="artifact_validity", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run trials sequentially, grade, write manifests")
    r.add_argument("--tag", required=True)
    r.add_argument("--tasks", required=True, help="comma-separated task ids, e.g. F1,C1")
    r.add_argument("--model", required=True, action="append", type=parse_model,
                   help="provider:model[:effort]; repeat for several models")
    r.add_argument("--replicates", type=int, default=1)
    r.add_argument("--hostd-bin", default="",
                   help="chudware-hostd for the trial's private daemon "
                        "(default: daemon_bridge.resolve_hostd_bin())")
    r.add_argument("--allow-stale-engine", action="store_true")
    r.add_argument("--dry-run", action="store_true",
                   help="print the exact run_mission.py command per trial; run nothing")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("rescore", help="re-grade saved trials with the current graders")
    s.add_argument("--tag", required=True)
    s.set_defaults(fn=cmd_rescore)

    p = sub.add_parser("report", help="per-task k/N per model with every check result")
    p.add_argument("--tag", required=True)
    p.add_argument("--rescored", action="store_true",
                   help="use each trial's latest rescore instead of its original grade")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    if getattr(args, "replicates", 1) < 1:
        ap.error("--replicates must be >= 1")
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
