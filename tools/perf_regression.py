"""Chudware modeling perf-regression harness — time the geometry kernel, catch slowdowns.

Drives the SAME libchudware the app + MCP use (via core_bridge), times a fixed battery of
deterministic modeling workloads, and compares against a committed baseline. Built for CI:
a regression beyond the threshold exits non-zero with a readable diff.

What it measures (all wall-clock, kernel-only — no GUI, no LLM):
  * BUILD workloads   — doc_new + apply_ops + close, median of N iters (fresh compile each time,
                        so OCCT extrude/fillet/boolean/tessellate actually run).
  * OPEN (cold)       — build+save once, then re-open a .step+ N times. This is the
                        rebuild-on-open cost (every feature re-run through OCCT on open); the
                        number that justifies (or doesn't) a persisted-geometry cache.
Diagnostic artifacts captured alongside the timings (not regression-gated, just recorded):
  * per-feature ms    — which feature dominates a rebuild (from feature_status[].ms).
  * g_mtx contention  — lock wait/hold/queue-depth under a short concurrent burst (prof_contention).

Usage:
  python tools/perf_regression.py                 # run + compare to baseline, exit 1 on regression
  python tools/perf_regression.py --update        # run + overwrite the baseline (accept current perf)
  python tools/perf_regression.py --iters 9 --threshold 0.30
  python tools/perf_regression.py --json out.json # also write the full run record

Point at a specific engine with CHUDWARE_LIBCHUDWARE=/path/to/libchudware.so (else the resolved dist .so).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # repo root -> services.mcp.core_bridge

from services.mcp import core_bridge as cb  # noqa: E402

BASELINE = _HERE / "perf_baseline.json"

# ------------------------------------------------------------------ op builders
def _box(n, l, w, h):
    return {"op": "add_feature", "feature": {"type": "box", "name": n,
            "params": {"length": cb.dim(l), "width": cb.dim(w), "height": cb.dim(h)}}}

def _fillet(r, n="Round"):
    return {"op": "add_feature", "feature": {"type": "fillet", "name": n,
            "params": {"radius": cb.dim(r)}, "refs": {"edges": {"of": "edge"}}}}

def _chamfer(d, n="Chamfer"):
    return {"op": "add_feature", "feature": {"type": "chamfer", "name": n,
            "params": {"distance": cb.dim(d)}, "refs": {"edges": {"of": "edge"}}}}

def _shell(t, n="Shell"):
    return {"op": "add_feature", "feature": {"type": "shell", "name": n,
            "params": {"thickness": cb.dim(t), "outward": False},
            "refs": {"remove": {"of": "face", "side": "+z"}}}}

def _bolt(count, pcd, dia, n="BoltCircle"):
    return {"op": "add_feature", "feature": {"type": "bolt_circle", "name": n,
            "params": {"diameter": dia, "count": count, "pcd": pcd},
            "refs": {"face": {"of": "face", "side": "top"}}}}

# ------------------------------------------------------------------ workloads
# Deterministic; each dominated by the op we want to track. Keep these STABLE — changing a
# workload invalidates its baseline entry (a new key, not a regression).
WORKLOADS = {
    "box":       [_box("Block", 80, 60, 40)],
    "fillet":    [_box("Block", 80, 60, 40), _fillet(3)],
    "chamfer":   [_box("Block", 80, 60, 40), _chamfer(3)],
    "shell":     [_box("Block", 80, 60, 40), _shell(2)],
    "boolean":   [_box("Plate", 120, 120, 20), _bolt(24, 90, 6)],
    "pattern":   [_box("Plate", 140, 140, 20), _bolt(48, 110, 5)],
    "heavy_mix": [_box("Block", 100, 80, 40), _fillet(3), _bolt(16, 70, 6)],
}
# The part used for the cold-OPEN benchmark (rebuild-on-open cost).
OPEN_PART = [_box("Plate", 160, 160, 20), _bolt(24, 120, 6), _fillet(2)]


def _time_build(ops, iters, warmup=1):
    """Median/min/max ms for a full doc_new + apply_ops + close cycle."""
    for _ in range(warmup):  # warm OCCT lazy init so the first cold compile isn't counted
        d = cb.call("doc_new", {"kind": "part", "name": "warm"})["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
        _close(d)
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        d = cb.call("doc_new", {"kind": "part", "name": "perf"})["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
        samples.append((time.perf_counter() - t0) * 1000.0)
        _close(d)
    return _stats(samples)


def _time_open(iters, warmup=1):
    """Median/min/max ms to cold-open a saved .step+ (full rebuild each open)."""
    path = str(Path(tempfile.gettempdir()) / "chudware_perf_open.step+")
    d = cb.call("doc_new", {"kind": "part", "name": "perf_open"})["doc_id"]
    cb.call("doc_apply_ops", {"doc_id": d, "ops": OPEN_PART})
    cb.call("doc_save", {"doc_id": d, "path": path})
    _close(d)
    for _ in range(warmup):
        _close(cb.call("doc_open", {"path": path})["doc_id"])
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        od = cb.call("doc_open", {"path": path})["doc_id"]
        samples.append((time.perf_counter() - t0) * 1000.0)
        _close(od)
    return _stats(samples)


def _close(doc_id):
    try:
        cb.call("doc_close", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001
        pass


def _stats(samples):
    return {"median_ms": round(statistics.median(samples), 3),
            "min_ms": round(min(samples), 3),
            "max_ms": round(max(samples), 3),
            "n": len(samples)}


# ------------------------------------------------------------------ diagnostics (recorded, not gated)
def _per_feature_ms(ops):
    d = cb.call("doc_new", {"kind": "part", "name": "diag"})["doc_id"]
    r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
    fs = [{"id": s.get("id"), "ms": s.get("ms")} for s in
          (r.get("rebuild") or {}).get("feature_status", [])]
    _close(d)
    return fs


def _contention_burst(threads=6, per=12):
    """Short concurrent burst to snapshot g_mtx contention. On the REAL (separate-process)
    deployment this stays ~0; it spikes only when subagents share one engine process. Recorded
    so the CI artifact shows whether the run was serialization-bound."""
    if not _has_rpc("prof_contention"):
        return {"available": False}
    cb.call("prof_contention", {"reset": True})

    def w():
        for _ in range(per):
            d = cb.call("doc_new", {"kind": "part", "name": "c"})["doc_id"]
            cb.call("doc_apply_ops", {"doc_id": d, "ops": WORKLOADS["heavy_mix"]})
            _close(d)
    ts = [threading.Thread(target=w) for _ in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    con = cb.call("prof_contention", {})
    return {"available": True,
            "threads": threads,
            "pct_wall_blocked": round(con.get("pct_wall_blocked", 0), 1),
            "contended_pct": round(con.get("contended_pct", 0), 1),
            "max_waiters": con.get("max_waiters"),
            "avg_wait_ms": round(con.get("avg_wait_ms", 0), 3),
            "top_methods": con.get("top_methods", [])[:5]}


def _has_rpc(name):
    try:
        r = cb.call(name, {"reset": False} if "contention" in name else {})
        return isinstance(r, dict)
    except Exception:  # noqa: BLE001
        return False


def _git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=str(_HERE.parent), text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


# ------------------------------------------------------------------ run + compare
def run(iters):
    builds = {name: _time_build(ops, iters) for name, ops in WORKLOADS.items()}
    builds["open_cold"] = _time_open(iters)
    return {
        "engine": os.environ.get("CHUDWARE_LIBCHUDWARE", str(getattr(cb, "dist_dir", lambda: "?")())),
        "git_sha": _git_sha(),
        "iters": iters,
        "metrics": builds,
        "diagnostics": {
            "per_feature_ms_heavy_mix": _per_feature_ms(WORKLOADS["heavy_mix"]),
            "contention_burst": _contention_burst(),
        },
    }


def compare(current, baseline, threshold):
    """Return (regressions, table_rows). A regression = median grew by > threshold fraction
    AND the absolute increase is > 1ms (ignore sub-ms jitter on trivial ops)."""
    rows, regressions = [], []
    base_m = baseline.get("metrics", {})
    for name, cur in current["metrics"].items():
        b = base_m.get(name)
        cur_ms = cur["median_ms"]
        if not b:
            rows.append((name, cur_ms, None, None, "new"))
            continue
        base_ms = b["median_ms"]
        delta = (cur_ms - base_ms) / base_ms if base_ms else 0.0
        flag = ""
        if delta > threshold and (cur_ms - base_ms) > 1.0:
            flag = "REGRESSION"
            regressions.append((name, base_ms, cur_ms, delta))
        elif delta < -threshold and (base_ms - cur_ms) > 1.0:
            flag = "faster"
        rows.append((name, cur_ms, base_ms, delta, flag))
    return regressions, rows


def _print_table(rows):
    print(f"  {'workload':14} {'median_ms':>10} {'baseline':>10} {'delta':>8}  flag")
    print("  " + "-" * 52)
    for name, cur, base, delta, flag in rows:
        d = f"{delta*100:+.0f}%" if isinstance(delta, float) else "—"
        b = f"{base:.2f}" if isinstance(base, (int, float)) else "—"
        print(f"  {name:14} {cur:10.2f} {b:>10} {d:>8}  {flag}")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iters", type=int, default=7, help="timed iterations per workload")
    ap.add_argument("--threshold", type=float, default=0.25,
                    help="fractional median increase that counts as a regression (default 0.25)")
    ap.add_argument("--update", action="store_true", help="overwrite the baseline with this run")
    ap.add_argument("--json", default="", help="also write the full run record here")
    args = ap.parse_args(argv[1:])

    if not cb.available():
        print("ERROR: libchudware not loadable. Build core (build-occt-linux) and set "
              "CHUDWARE_LIBCHUDWARE, or stage services/dist. See docs/PROFILER.md.")
        return 2

    print(f"Chudware perf-regression  ·  iters={args.iters}  threshold={args.threshold:.0%}  "
          f"sha={_git_sha()}")
    current = run(args.iters)

    if args.json:
        Path(args.json).write_text(json.dumps(current, indent=2))
        print(f"  wrote run record -> {args.json}")

    # Diagnostics (always printed — not gated).
    diag = current["diagnostics"]
    pf = diag["per_feature_ms_heavy_mix"]
    print("\n  per-feature ms (heavy_mix):", ", ".join(f"{f['id']}={f['ms']:.1f}" for f in pf))
    con = diag["contention_burst"]
    if con.get("available"):
        print(f"  g_mtx contention burst (6 threads): pct_wall_blocked={con['pct_wall_blocked']}%  "
              f"max_waiters={con['max_waiters']}  avg_wait={con['avg_wait_ms']}ms")
    else:
        print("  g_mtx contention: prof_contention unavailable in this engine (old build)")

    if args.update or not BASELINE.exists():
        BASELINE.write_text(json.dumps(current, indent=2))
        print(f"\n  {'updated' if BASELINE.exists() else 'wrote'} baseline -> {BASELINE.name} "
              f"(sha {current['git_sha']})")
        if not args.update:
            print("  (no prior baseline existed; recorded current as baseline — no gate this run)")
        return 0

    baseline = json.loads(BASELINE.read_text())
    print(f"\n  comparing against baseline sha {baseline.get('git_sha')}:")
    regressions, rows = compare(current, baseline, args.threshold)
    _print_table(rows)

    if regressions:
        print(f"\n  ✘ {len(regressions)} REGRESSION(S) beyond {args.threshold:.0%}:")
        for name, base_ms, cur_ms, delta in regressions:
            print(f"    {name}: {base_ms:.2f}ms -> {cur_ms:.2f}ms ({delta*100:+.0f}%)")
        print("  (if intentional, re-run with --update to accept the new baseline)")
        return 1
    print("\n  ✓ no regressions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
