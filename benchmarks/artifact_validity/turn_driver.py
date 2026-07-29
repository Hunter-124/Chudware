"""The live turn driver — ONE mechanism for driving a real agent turn, shared by this
benchmark and the ad-hoc defect sweeps.

It sends the exact RPC the GUI sends: ``agent_admin_turn`` to ``services.worker_main`` on
stdin, one turn per prompt, and returns the parsed turn dict. This is the mechanism the
scratch defect sweep established (see .claude/skills/chudware-general-lane-live-verify);
it lives here now so there is exactly one live driver in the tree instead of a second one
per consumer.

Two things the sweep did NOT do, both required for a MEASUREMENT rather than a defect hunt:

* ``CHUDWARE_ENGINE_DAEMON=0``. Daemon mode is the default, so by default a turn's engine
  is whichever chudware-hostd happens to be running — on this box that was a binary from
  a DIFFERENT build directory (core/build-occt-linux) than the one the staleness check
  verifies. A measurement whose engine you did not verify is not a measurement, so the
  benchmark forces the in-process engine at ``CHUDWARE_LIBCHUDWARE``.
* ``CHUDWARE_AUTOSAVE_DIR`` per prompt, so the artifact probe sees every doc the turn
  touched (see probe.py) and nothing from any other run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TurnConfig:
    model: str = "claude-sonnet-4-5"
    effort: str = "low"
    provider: str = "anthropic"
    timeout_s: float = 420.0
    role: str = "general_agent"
    engine: str = ""

    @classmethod
    def from_env(cls) -> "TurnConfig":
        """SWEEP_* names, so a benchmark run and a defect sweep are configured identically."""
        return cls(
            model=os.environ.get("SWEEP_MODEL", "claude-sonnet-4-5"),
            effort=os.environ.get("SWEEP_EFFORT", "low"),
            provider=os.environ.get("SWEEP_PROVIDER", "anthropic"),
            timeout_s=float(os.environ.get("SWEEP_TIMEOUT", "420")),
            engine=os.environ.get("CHUDWARE_LIBCHUDWARE", ""),
        )


def run_turn(prompt: str, run_id: str, run_dir: Path, cfg: TurnConfig) -> dict:
    """One live turn in its own workspace + autosave dir. Never raises: a dead turn is a
    result (``stop_reason``), because a benchmark that aborts on the first bad prompt
    measures nothing."""
    work = run_dir / "work"
    autosave = run_dir / "autosave"
    work.mkdir(parents=True, exist_ok=True)
    autosave.mkdir(parents=True, exist_ok=True)

    req = {"id": 1, "method": "agent_admin_turn", "params": {
        "request": prompt,
        "run_id": run_id,
        "role": cfg.role,
        "provider": cfg.provider,
        "model": cfg.model,
        "reasoning_effort": cfg.effort,
        "workspace_dir": str(work),
    }}

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # Hermetic + verified engine (see module docstring).
    env["CHUDWARE_ENGINE_DAEMON"] = "0"
    if cfg.engine:
        env["CHUDWARE_LIBCHUDWARE"] = cfg.engine
    env["CHUDWARE_AUTOSAVE_DIR"] = str(autosave)
    # The autosave pruner is count-bounded globally; a per-prompt dir must not be pruned
    # mid-run or the probe loses the very docs it exists to inspect.
    env["CHUDWARE_AUTOSAVE_MAX_FILES"] = "100000"
    env.setdefault("CHUDWARE_PROJECT_SAVE_DIR", str(work))

    jsonl = run_dir / "turn.jsonl"
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "services.worker_main"],
            input=json.dumps(req) + "\n", capture_output=True, text=True,
            cwd=str(REPO), env=env, timeout=cfg.timeout_s)
        raw, err = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout if isinstance(exc.stdout, str) else ""
        jsonl.write_text(raw, encoding="utf-8", errors="replace")
        return {"success": False, "stop_reason": "bench_timeout",
                "result": {"calls": []}, "elapsed_s": time.time() - t0}

    jsonl.write_text(raw, encoding="utf-8", errors="replace")

    turn = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "ok" in j and isinstance(j.get("result"), dict) and "turn" in j["result"]:
            turn = j["result"]["turn"]
    if turn is None:
        return {"success": False, "stop_reason": "no_turn_in_output",
                "result": {"calls": []}, "elapsed_s": time.time() - t0,
                "stderr_tail": err[-1200:]}
    turn["elapsed_s"] = time.time() - t0
    return turn


def turn_to_record(prompt_id: str, prompt: str, turn: dict) -> dict:
    """Flatten a turn into the record shape scoring.score_run consumes."""
    res = turn.get("result") or {}
    return {
        "prompt_id": prompt_id,
        "prompt": prompt,
        "success": bool(turn.get("success")),
        "stop_reason": turn.get("stop_reason"),
        "elapsed_s": round(float(turn.get("elapsed_s") or 0.0), 1),
        "calls": res.get("calls") or [],
        "reply": str(((res.get("handoff") or {}).get("summary")) or "")[:2000],
        "stderr_tail": turn.get("stderr_tail", ""),
    }
