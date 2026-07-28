#!/usr/bin/env python3
"""Replay every tool call that FAILED in a recorded agent run, against today's code.

Why this exists
---------------
The gym writes a ``ledger.json`` per run under
``tools/flywheel/bench/gym/reports/runs/**``.  Each ledger holds the full
``tool_call`` stream: tool name, the exact ``args`` the model wrote, whether it
worked, and the result.  That is a free, already-paid-for corpus of *real model
dialect* -- every way a frontier model actually phrases a CAD tool call, including
all the ways our tools used to reject it.

Re-running the gym costs provider spend and is non-deterministic, so it is a bad
regression signal.  Replaying the recorded failures is deterministic, free, and
answers the only question that matters after a tool-robustness change: **of the
calls that failed in the field, how many work now?**

What it does and does not prove
-------------------------------
Replay is faithful for *argument-shape* failures -- unknown kwarg, wrong
container type, bad enum spelling, missing-but-defaultable param.  Those are
pure functions of the call.  It is NOT faithful for failures that depended on
run state we cannot reconstruct (a mission ledger, a delegation tree, a document
built by 40 earlier ops).  Those are reported separately as ``skipped`` with the
reason, and are never counted as fixed.  A number this tool prints is a floor,
never a flattering estimate.

Two corpora
-----------
``--runs`` defaults to the gym ledger tree (``ledger.json``, full ``args``).  The
*other* corpus is the one the user actually generates: live desktop runs under
``~/.local/share/Chudware/cache/runs/<run_id>/events.jsonl``.  Pass ``--live`` to
read those instead.  Live events store ``args_preview`` (capped at 2048 chars by
``agent_events.tool_call_event``) rather than raw args, so a preview that does not
parse as JSON is reported as ``skipped: truncated_args`` and never scored --
measured at 1 of 60 on the current tree, so the corpus is ~98% usable.

``--live`` also filters to genuine desktop runs (``run_id`` matching ``run-<digits>``),
dropping synthetic bench/self-test runs that would otherwise dominate the count.

Usage
-----
    python3 tools/flywheel/replay_ledger_failures.py              # gym summary
    python3 tools/flywheel/replay_ledger_failures.py --live       # live desktop runs
    python3 tools/flywheel/replay_ledger_failures.py --live --since-days 7
    python3 tools/flywheel/replay_ledger_failures.py --detail     # per-call
    python3 tools/flywheel/replay_ledger_failures.py --tool fit_calc
    python3 tools/flywheel/replay_ledger_failures.py --json out.json
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import pathlib
import re
import sys
from typing import Any, Iterator

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_RUNS = REPO / "tools" / "flywheel" / "bench" / "gym" / "reports" / "runs"
LIVE_RUNS = pathlib.Path.home() / ".local" / "share" / "Chudware" / "cache" / "runs"

# A genuine desktop run.  Bench harnesses and self-tests write their own run ids
# ("srctest_run", "mega8-e2e-run", ...) into the SAME tree, and they outnumber real
# runs ~20:1, so counting them would drown the signal we care about.
_LIVE_RUN_ID = re.compile(r"^run-\d+$")

# Args naming state we cannot rebuild.  A call carrying one of these is replayed
# only if the state happens to exist; otherwise it is skipped, not scored.
_STATEFUL_ARGS = ("run_id", "mission_id", "node_id", "card", "checkpoint", "job_id")

# Tools whose whole purpose is live run state.  Replaying them out of context
# measures the harness stub, not the fix.
_STATEFUL_TOOLS = frozenset({
    "ledger_get", "ledger_note", "ledger_close", "mission_start", "mission_status",
    "delegate", "kill_delegation_tree", "delegation_budget", "council_convene",
    "workspace_status", "dag_status", "dag_replan", "buildgraph_status",
    "get_job", "ask_user",
})


def _iter_nodes(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_nodes(v)


def load_failures(runs_dir: pathlib.Path) -> list[dict]:
    """Every recorded ``tool_call`` whose ``ok`` was false, with its args."""
    out: list[dict] = []
    for ledger in sorted(runs_dir.rglob("ledger.json")):
        try:
            data = json.loads(ledger.read_text())
        except (OSError, ValueError):
            continue
        for node in _iter_nodes(data):
            if node.get("event") != "tool_call" or node.get("ok") is not False:
                continue
            tool = node.get("tool")
            if not isinstance(tool, str) or not tool:
                continue
            args = node.get("args")
            out.append({
                "run": str(ledger.relative_to(runs_dir).parent),
                "tool": tool,
                "args": args if isinstance(args, dict) else {},
                "was": _message(node.get("result")),
            })
    return out


def load_live_failures(runs_dir: pathlib.Path, *, since_days: float = 0.0) -> list[dict]:
    """Every failed tool call from the LIVE desktop run tree (``events.jsonl``).

    This is the corpus that reflects what the user actually hits.  Args come from
    ``args_preview`` (capped, so occasionally truncated); a preview that does not
    parse as JSON is kept with ``truncated=True`` and skipped at replay rather than
    silently scored.
    """
    import time  # noqa: PLC0415 — only this loader needs a clock

    cutoff = time.time() - since_days * 86400 if since_days > 0 else 0.0
    out: list[dict] = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        events = run_dir / "events.jsonl"
        if not events.exists():
            continue
        try:
            if cutoff and events.stat().st_mtime < cutoff:
                continue
            text = events.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if '"agent.tool_call"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("kind") != "agent.tool_call" or ev.get("ok") is not False:
                continue
            if not _LIVE_RUN_ID.match(str(ev.get("run_id", ""))):
                continue
            tool = ev.get("tool_name")
            if not isinstance(tool, str) or not tool:
                continue
            try:
                args = json.loads(ev.get("args_preview") or "{}")
            except ValueError:
                args, truncated = {}, True
            else:
                truncated = not isinstance(args, dict)
                if truncated:
                    args = {}
            out.append({
                "run": run_dir.name,
                "tool": tool,
                "args": args,
                "was": ev.get("error_preview") or "",
                "truncated": truncated,
            })
    return out


def _message(result: Any) -> str:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result.strip()
    if isinstance(result, dict):
        for key in ("error", "reason", "hint", "message"):
            v = result.get(key)
            if isinstance(v, str) and v:
                return v
        return json.dumps(result)
    return str(result)


def classify(message: str) -> str:
    """Collapse a raw error into a stable failure class for counting."""
    m = message.lower()
    if "could not be resolved" in m:
        return "tool-unresolved"
    if "unexpected keyword argument" in m or "unknown argument" in m:
        return "bad-kwarg"
    if "not a tool in this harness" in m:
        return "tool-invented"
    if "failed validation" in m:
        return "op-validation"
    if "must be a list" in m or "object has no attribute" in m:
        return "bad-arg-type"
    if "no such document" in m:
        return "doc-not-found"
    if "no current part" in m or "there is no single current part" in m:
        return "doc-unspecified"
    if "nothing measured" in m:
        return "missing-prerequisite"
    if "no bodies" in m:
        return "empty-document"
    if "unknown kind" in m or "unknown section" in m:
        return "vocab"
    if "no delegation tree" in m or "no active workspace" in m:
        return "context-unbound"
    if "not found" in m:
        return "lookup-miss"
    return "other"


def _redact(message: str) -> str:
    message = re.sub(r"[0-9a-f]{6,}", "<id>", message)
    return re.sub(r"\d+", "N", message)[:110]


def _substitute_doc(fn: Any, args: dict, doc_id: str | None) -> dict:
    """Point a recorded call at the live scratch document.

    Two rewrites, both needed to isolate the thing we are actually measuring:
    the document the call originally targeted is long gone, so keeping its dead
    id would make every replay fail on lookup; and a call that omitted ``doc_id``
    entirely resolved against "the current part" back then, which here means
    whichever docs earlier replays happened to leave open -- an artefact that
    shows up as a spurious "N parts are open" ambiguity. Pinning both cases to
    the scratch doc leaves the ARGUMENT SHAPE as the only variable.
    """
    out = dict(args)
    if doc_id is None:
        return out
    for key in ("doc_id", "document", "part_id"):
        if key in out:
            out[key] = doc_id
    if "doc_id" not in out:
        try:
            spec = _agent_turn()._signature_spec(fn)
        except Exception:  # noqa: BLE001
            return out
        if "doc_id" in spec.get("params", ()):
            out["doc_id"] = doc_id
    return out


def _skip_reason(tool: str, args: dict, *, truncated: bool = False) -> str | None:
    if truncated:
        # A live args_preview capped mid-JSON is not the call the model made.
        # Replaying a guess at it would invent a number.
        return "truncated_args (live preview exceeded the 2048-char cap)"
    if tool in _STATEFUL_TOOLS:
        return f"needs live run state ({tool} is mission-scoped)"
    for key in _STATEFUL_ARGS:
        if key in args:
            return f"args reference unreproducible state ({key})"
    return None


def replay(failures: list[dict], *, doc_id: str | None) -> list[dict]:
    at = _agent_turn()

    results: list[dict] = []
    for rec in failures:
        tool, args = rec["tool"], rec["args"]
        row = dict(rec, klass=classify(rec["was"]))

        skip = _skip_reason(tool, args, truncated=bool(rec.get("truncated")))
        if skip is not None:
            results.append(dict(row, status="skipped", now=skip))
            results[-1]["fixed"] = None
            continue

        fn = at._bind_tool(tool)
        if fn is None:
            err = at._unresolved_tool_error(tool)
            results.append(dict(row, status=err.get("status", "unbound"),
                                now=err.get("error", ""), fixed=False))
            continue

        call_args = _substitute_doc(fn, args, doc_id)
        kwargs, notes, arg_error = at._reconcile_kwargs(tool, fn, call_args)
        if arg_error is not None:
            results.append(dict(row, status="rejected-args",
                                now=str(arg_error.get("error", ""))[:400], fixed=False))
            continue

        try:
            raw = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a raise IS the failure signal
            results.append(dict(row, status="raised", now=f"{type(exc).__name__}: {exc}"[:400],
                                fixed=False))
            continue

        payload = raw
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"ok": True, "raw": raw[:200]}
        ok = payload.get("ok", True) if isinstance(payload, dict) else True
        results.append(dict(row, status="ok" if ok else "failed",
                            now=("" if ok else _message(payload))[:400],
                            fixed=bool(ok), notes=notes))
    return results


def _agent_turn():
    """The built-in agent's dispatch module — the path that actually ships."""
    from services.agent import agent_turn as at  # noqa: PLC0415
    return at


def _mint_doc() -> str | None:
    """A live part with REAL geometry, so the replay measures the tool.

    An empty document would make every evaluate/measure/describe call fail with
    "no bodies" no matter how good the tool is, hiding the argument-shape result
    we came here for. A plate with a through hole is the smallest shape that has
    a body, more than one planar face, a cylindrical face, and a subtractive
    feature — enough for every eval tool in the corpus to have something to say.
    """
    at = _agent_turn()
    new_part = at._bind_tool("new_part")
    add_features = at._bind_tool("add_features")
    if new_part is None:
        return None
    try:
        doc_id = json.loads(new_part(name="replay_scratch")).get("doc_id")
    except Exception:  # noqa: BLE001
        return None
    if doc_id is None or add_features is None:
        return doc_id
    try:
        add_features(doc_id=doc_id, ops=[
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Plate",
                "params": {"length": 80, "width": 60, "height": 10}}},
            {"op": "add_feature", "feature": {
                "type": "cylinder", "name": "Bore",
                "params": {"radius": 6, "height": 40},
                "mode": "cut"}},
        ])
    except Exception:  # noqa: BLE001
        pass  # geometry is a nicety; the replay still measures arg shape without it
    return doc_id


def report(results: list[dict], *, detail: bool) -> int:
    scored = [r for r in results if r["fixed"] is not None]
    fixed = [r for r in scored if r["fixed"]]
    skipped = [r for r in results if r["fixed"] is None]

    print(f"recorded failures : {len(results)}")
    print(f"  replayable      : {len(scored)}")
    print(f"  still failing   : {len(scored) - len(fixed)}")
    print(f"  now succeed     : {len(fixed)}")
    print(f"  not scoreable   : {len(skipped)}  (live run state; never counted as fixed)")
    if scored:
        print(f"  fix rate        : {100.0 * len(fixed) / len(scored):.1f}% of replayable")

    print("\nBY FAILURE CLASS (replayable only)")
    per: dict[str, list[dict]] = collections.defaultdict(list)
    for r in scored:
        per[r["klass"]].append(r)
    for klass in sorted(per, key=lambda k: -len(per[k])):
        rows = per[klass]
        n_ok = sum(1 for r in rows if r["fixed"])
        print(f"  {klass:22} {n_ok:4}/{len(rows):<4} fixed")

    still = [r for r in scored if not r["fixed"]]
    if still:
        print("\nSTILL FAILING (grouped)")
        grouped = collections.Counter((r["tool"], _redact(r["now"])) for r in still)
        for (tool, msg), n in grouped.most_common():
            print(f"  {n:3}x {tool:26} {msg}")

    if detail:
        print("\nDETAIL")
        for r in results:
            mark = {True: "FIXED", False: "FAIL ", None: "skip "}[r["fixed"]]
            print(f"  [{mark}] {r['tool']:24} {json.dumps(r['args'])[:90]}")
            print(f"          was: {_redact(r['was'])}")
            if r["now"]:
                print(f"          now: {_redact(r['now'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=pathlib.Path, default=None,
                    help="directory scanned recursively for ledger.json "
                         f"(default: {DEFAULT_RUNS})")
    ap.add_argument("--live", action="store_true",
                    help="replay the LIVE desktop run tree (events.jsonl) instead of "
                         f"the gym ledgers (default dir: {LIVE_RUNS})")
    ap.add_argument("--since-days", type=float, default=0.0,
                    help="with --live, only runs touched in the last N days")
    ap.add_argument("--tool", action="append", default=[],
                    help="only replay this tool (repeatable)")
    ap.add_argument("--detail", action="store_true", help="print every call")
    ap.add_argument("--json", type=pathlib.Path, help="also write raw results here")
    args = ap.parse_args(argv)

    runs = args.runs or (LIVE_RUNS if args.live else DEFAULT_RUNS)
    if not runs.is_dir():
        print(f"no such runs dir: {runs}", file=sys.stderr)
        return 2

    failures = (load_live_failures(runs, since_days=args.since_days) if args.live
                else load_failures(runs))
    if args.tool:
        wanted = set(args.tool)
        failures = [f for f in failures if f["tool"] in wanted]
    if not failures:
        print("no recorded failures matched")
        return 0

    os.environ.setdefault("CHUDWARE_PREFLIGHT", "0")  # replay measures the tool, not preflight

    # Replay MINTS documents. Against the daemon behind the user's open window that
    # fans out doc.opened and buries their tab strip in scratch parts that outlive the
    # run — the same way a bare `pytest services/mcp` did before conftest isolated it.
    from services.mcp.private_hostd import private_daemon  # noqa: PLC0415

    with private_daemon() as isolated:
        if not isolated:
            print("warning: no private daemon — replaying against the ambient engine",
                  file=sys.stderr)
        doc_id = _mint_doc()
        if doc_id is None:
            print("warning: no engine available; doc-targeted calls will fail on lookup",
                  file=sys.stderr)
        results = replay(failures, doc_id=doc_id)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, default=str))
    return report(results, detail=args.detail)


if __name__ == "__main__":
    raise SystemExit(main())
