#!/usr/bin/env python3
"""Liveness kill-switch for mega-27 primary.

Kill + archive + signal if:
  - >120s elapse with zero new tool_calls in events.jsonl, OR
  - >3 dag.node.failed events appear

Usage:
  python liveness_watch.py [--pid PID] [--events PATH] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent
EVENTS = OUT / "events.jsonl"
PID_FILE = OUT / "primary.pid"
STALL_S = 120.0
MAX_NODE_FAILS = 3
POLL_S = 5.0


def _count_tool_calls(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    if "tool_call" in line:
                        n += 1
                    continue
                a0 = rec.get("arg0") or rec.get("kind") or ""
                if a0 in ("agent.tool_call", "tool_call") or rec.get("kind") in (
                    "agent.tool_call",
                    "tool_call",
                    "ledger_tool_call",
                ):
                    n += 1
                    continue
                # dual-arg / nested
                blob = json.dumps(rec, default=str)
                if '"tool_name"' in blob or '"tool":' in blob:
                    if "tool_call" in blob or a0 == "agent.tool_call":
                        n += 1
    except Exception:
        pass
    return n


def _count_node_fails(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "dag.node.failed" in line or "node.failed" in line:
                    n += 1
    except Exception:
        pass
    return n


def _read_pid(pid: int | None) -> int | None:
    if pid:
        return pid
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except Exception:
            return None
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _archive(reason: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = OUT / f"attempt-thrash-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in (
        "events.jsonl",
        "ledger.json",
        "status.json",
        "mission.json",
        "primary.log",
        "preflight_summary.json",
        "spawn_probe.json",
        "auto_pick_smoke.json",
    ):
        src = OUT / name
        if src.exists():
            try:
                shutil.copy2(src, dest / name)
            except Exception:
                pass
    (dest / "kill_reason.txt").write_text(reason + "\n", encoding="utf-8")
    (OUT / "liveness_kill.json").write_text(
        json.dumps(
            {
                "reason": reason,
                "archive": str(dest),
                "wall": time.time(),
                "stamp": stamp,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--events", type=Path, default=EVENTS)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--stall-s", type=float, default=STALL_S)
    ap.add_argument("--max-node-fails", type=int, default=MAX_NODE_FAILS)
    ap.add_argument("--poll-s", type=float, default=POLL_S)
    ap.add_argument("--grace-s", type=float, default=60.0, help="ignore stall until first tool or grace")
    args = ap.parse_args()

    events = args.events
    last_tool_n = 0
    last_progress_t = time.time()
    started = time.time()
    print(
        f"[liveness] watch events={events} stall_s={args.stall_s} "
        f"max_node_fails={args.max_node_fails}",
        flush=True,
    )

    while True:
        pid = _read_pid(args.pid or None)
        if pid is None:
            # wait for primary to start
            if time.time() - started > 600:
                print("[liveness] no pid after 600s — exit", flush=True)
                return 2
            time.sleep(args.poll_s)
            continue
        if not _alive(pid):
            # primary exited cleanly or crashed — stop watching
            print(f"[liveness] primary pid={pid} exited — watch done", flush=True)
            return 0

        tool_n = _count_tool_calls(events)
        fail_n = _count_node_fails(events)
        now = time.time()
        if tool_n > last_tool_n:
            last_tool_n = tool_n
            last_progress_t = now
            print(f"[liveness] progress tools={tool_n} fails={fail_n}", flush=True)

        reason = None
        if fail_n > args.max_node_fails:
            reason = (
                f"liveness kill: dag.node.failed count={fail_n} > {args.max_node_fails} "
                f"(tools={tool_n})"
            )
        elif tool_n == 0 and (now - started) > max(args.grace_s, args.stall_s):
            reason = (
                f"liveness kill: zero tool_calls after {now - started:.0f}s "
                f"(stall threshold {args.stall_s}s)"
            )
        elif tool_n > 0 and (now - last_progress_t) > args.stall_s:
            reason = (
                f"liveness kill: no new tool_calls for {now - last_progress_t:.0f}s "
                f"(last_tools={tool_n}, fails={fail_n})"
            )

        if reason:
            print(f"[liveness] {reason}", flush=True)
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
                if _alive(pid):
                    os.kill(pid, signal.SIGKILL)
            except OSError as exc:
                print(f"[liveness] kill err: {exc}", flush=True)
            dest = _archive(reason)
            print(f"[liveness] archived -> {dest}", flush=True)
            # signal file for harness peer
            (OUT / "DM_FIXER25.txt").write_text(
                f"THRASH KILL: {reason}\narchive={dest}\n"
                "Please rebind C27/D27 fix; do not burn more quota until root cause closed.\n",
                encoding="utf-8",
            )
            return 1

        # also stop if status finished
        st = OUT / "status.json"
        if st.exists():
            try:
                s = json.loads(st.read_text())
                if s.get("state") == "finished":
                    print("[liveness] status finished — watch done", flush=True)
                    return 0
            except Exception:
                pass
        time.sleep(args.poll_s)


if __name__ == "__main__":
    raise SystemExit(main())
