#!/usr/bin/env python3
"""
tools/gui_parity/common.py — shared infra for the T-G9 host↔AI-CAD2 parity harnesses.

How it works (see work/tasks/T-G9.md scope additions):
  1. Both GUI binaries link services/worker; WorkerClient::call() is the single funnel
     every panel RPC passes through regardless of routing (Python worker subprocess,
     in-process libaicad, ECAD/agent C++ paths). With AI_CAD_RPC_TRACE=<file> set it
     appends one JSONL record {method, params, ok, result|err} per RPC.
  2. Each harness runs the SAME AI_CAD_SHOT scenario on BOTH binaries offscreen and
     captures the two streams.
  3. Request-parity: the domain-relevant subsequence (method allowlist) of the two
     request streams must match after normalization. The new shell legitimately fires
     extra panel-init RPCs (gallery/parts/artifact list, etc.) — those are reported as
     informational, never as failures, and never hidden.
  4. Artifact-parity (gcode/DXF/PDF): the state-building requests from EACH binary's
     captured stream are replayed through mcp_server.core_bridge (the same libaicad the
     binaries load in-process), then the artifact RPCs (cam_post x8, export_drawing) are
     issued on top of each replayed state. host-derived vs aicad2-derived artifacts are
     byte-diffed. A panel that drove different engine calls => different artifacts.

Stdlib only (core_bridge itself needs AI_CAD_LIBAICAD; see resolve_env()).
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRATCH = os.path.join(REPO, "scratch", "gui_parity")

HOST_BIN = os.path.join(REPO, "host", "build-linux", "bin", "AI-CAD")
NEW_BIN = os.path.join(REPO, "ui", "imgui", "build", "bin", "AI-CAD2")
# ShopCraft is a post-build symlink/copy of AI-CAD2 (T-G10). Both names work.
SHOPCRAFT_BIN = os.path.join(REPO, "ui", "imgui", "build", "bin", "ShopCraft")

BINARIES = {"host": HOST_BIN, "aicad2": NEW_BIN}

# Response keys that are legitimately volatile between two identical runs (timings,
# absolute paths into per-run temp dirs). Kept deliberately short — anything else that
# differs is a FINDING and must show up in the diff.
VOLATILE_KEYS = {"elapsed_ms", "elapsed_s", "wall_ms", "time_ms", "path", "png", "written"}



def _default_libaicad() -> str:
    """Prefer a core build in this checkout; fall back to known prebuilt locations
    (worktrees usually have no core build — T-G8 finisher pattern)."""
    cands = [
        os.path.join(REPO, "core", "build-occt-linux", "bin", "libaicad.so"),
        os.path.join(REPO, "core", "build-linux", "bin", "libaicad.so"),
        "/home/hunter/Repositories/CAD/core/build-occt-linux/bin/libaicad.so",
        "/home/hunter/Repositories/CAD/core/build-linux/bin/libaicad.so",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]

def resolve_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.setdefault("AI_CAD_PYTHON", shutil.which("python3") or sys.executable)
    env.setdefault("AI_CAD_LIBAICAD",
                   _default_libaicad())
    env["AI_CAD_NO_FILE_DIALOGS"] = "1"
    env["AI_CAD_REPO"] = REPO
    if extra:
        env.update(extra)
    return env


def run_scenario(binary_key: str, scenario: str, timeout_s: int = 300,
                 run_dir: str | None = None) -> str:
    """Run AI_CAD_SHOT=<scenario> on one binary offscreen with the RPC trace enabled.
    Returns the trace path. Raises on missing binary or nonzero exit."""
    bin_path = BINARIES[binary_key]
    if not os.path.isfile(bin_path):
        raise FileNotFoundError(f"{binary_key} binary missing: {bin_path} (build it first)")
    run_dir = run_dir or os.path.join(SCRATCH, binary_key)
    os.makedirs(run_dir, exist_ok=True)
    trace = os.path.join(run_dir, f"{scenario}.rpc.jsonl")
    shot = os.path.join(run_dir, f"{scenario}.png")
    if os.path.exists(trace):
        os.unlink(trace)
    env = resolve_env({"AI_CAD_SHOT": scenario, "AI_CAD_RPC_TRACE": trace})
    # Both binaries accept --shot; host also honors --ui-screenshot but --shot is common.
    flag = "--ui-screenshot" if binary_key == "host" else "--shot"
    cmd = ["xvfb-run", "-a", bin_path, flag, shot]
    t0 = time.time()
    # cwd: a per-binary dir so any cwd-relative artifact writes don't collide.
    proc = subprocess.run(cmd, env=env, cwd=run_dir, timeout=timeout_s,
                          capture_output=True, text=True)
    log = os.path.join(run_dir, f"{scenario}.log")
    with open(log, "w") as fh:
        fh.write(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"{binary_key}/{scenario}: exit {proc.returncode} "
                           f"after {time.time()-t0:.0f}s (log: {log})")
    if not os.path.exists(trace):
        raise RuntimeError(f"{binary_key}/{scenario}: no RPC trace written ({trace}) — "
                           f"is the AI_CAD_RPC_TRACE build current?")
    return trace


def load_stream(trace_path: str) -> list[dict]:
    recs = []
    with open(trace_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def _scrub(obj, extra_volatile: set[str] = frozenset()):
    """Recursively null volatile keys so runs are comparable; everything else is kept."""
    vol = VOLATILE_KEYS | set(extra_volatile)
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in vol else _scrub(v, extra_volatile))
                for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_scrub(v, extra_volatile) for v in obj]
    if isinstance(obj, float):
        return round(obj, 9)  # kill cross-build printf jitter, keep real divergence
    return obj


def domain_requests(stream: list[dict], prefixes: tuple[str, ...],
                    extra_volatile: set[str] = frozenset()) -> list[str]:
    """The normalized request subsequence for a domain (method + params), one canonical
    JSON string per RPC, order-preserving."""
    out = []
    for rec in stream:
        m = rec.get("method", "")
        if any(m == p or m.startswith(p) for p in prefixes):
            out.append(json.dumps({"method": m,
                                   "params": _scrub(rec.get("params", {}), extra_volatile)},
                                  sort_keys=True))
    return out


def domain_responses(stream: list[dict], methods: set[str],
                     extra_volatile: set[str] = frozenset()) -> list[str]:
    out = []
    for rec in stream:
        m = rec.get("method", "")
        if m in methods:
            body = rec.get("result") if rec.get("ok") else {"err": rec.get("err")}
            out.append(json.dumps({"method": m, "ok": rec.get("ok"),
                                   "response": _scrub(body, extra_volatile)},
                                  sort_keys=True))
    return out


def unified_diff(a: list[str], b: list[str], la: str, lb: str, n: int = 1) -> str:
    return "\n".join(difflib.unified_diff(a, b, fromfile=la, tofile=lb, lineterm="", n=n))


def diff_streams(name: str, host_lines: list[str], new_lines: list[str]) -> dict:
    """PASS iff the two normalized sequences are identical."""
    ok = host_lines == new_lines
    d = "" if ok else unified_diff(host_lines, new_lines, f"host/{name}", f"aicad2/{name}")
    return {"name": name, "pass": ok, "host_n": len(host_lines), "new_n": len(new_lines),
            "diff": d}


# ---- core_bridge replay --------------------------------------------------------------------

def bridge():
    """Import mcp_server.core_bridge with the repo on sys.path."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    os.environ.setdefault("AI_CAD_LIBAICAD",
                          _default_libaicad())
    from mcp_server import core_bridge  # noqa: PLC0415
    return core_bridge


def replay_doc_stream(cb, stream: list[dict], mutating: tuple[str, ...]) -> dict[str, str]:
    """Replay a binary's captured state-building requests into fresh core_bridge docs.
    Returns {captured_doc_id -> replayed_doc_id}. Any captured doc_new is re-minted; all
    later requests get their doc_id/params remapped through the map. Requests replay in
    capture order with byte-identical params (bar the id remap) — a panel that sent
    different ops produces a different replayed doc, which is the point."""
    idmap: dict[str, str] = {}

    def remap(obj):
        if isinstance(obj, dict):
            return {k: remap(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [remap(v) for v in obj]
        if isinstance(obj, str) and obj in idmap:
            return idmap[obj]
        return obj

    for rec in stream:
        m = rec.get("method", "")
        if not any(m == p or m.startswith(p) for p in mutating):
            continue
        params = remap(rec.get("params", {}))
        res = cb.call(m, params)
        # Track any id the engine minted so later captured calls resolve to the replay.
        for k in ("doc_id", "cam_doc_id", "drawing_doc_id"):
            old = (rec.get("result") or {}).get(k)
            new = (res or {}).get(k)
            if old and new and old != new:
                idmap[old] = new
    return idmap


# ---- report ---------------------------------------------------------------------------------

def emit(results: list[dict], title: str) -> bool:
    os.makedirs(SCRATCH, exist_ok=True)
    all_ok = all(r["pass"] for r in results)
    lines = [f"== {title} ==", ""]
    for r in results:
        status = "PASS" if r["pass"] else "FAIL"
        extra = r.get("note", "")
        lines.append(f"  [{status}] {r['name']}"
                     + (f"  (host={r.get('host_n','?')} rpc, aicad2={r.get('new_n','?')} rpc)"
                        if "host_n" in r else "")
                     + (f"  {extra}" if extra else ""))
        if not r["pass"] and r.get("diff"):
            lines.append("    ---- diff (first 60 lines) ----")
            lines.extend("    " + l for l in r["diff"].splitlines()[:60])
    text = "\n".join(lines)
    print(text)
    with open(os.path.join(SCRATCH, "report.txt"), "a") as fh:
        fh.write(text + "\n\n")
    return all_ok
