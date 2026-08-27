#!/usr/bin/env python3
"""Measure CAD's OMP bridge through its real newline-delimited stdio RPC channel."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# The bridge is a bun workspace under vendor/omp; every probe below is written relative to
# that dir, so resolve it explicitly rather than by counting parents from this file.
ROOT = Path(__file__).resolve().parents[1] / "vendor" / "omp"
BRIDGE = ROOT / "bridge/src/bridge.ts"
MODULE_PROBE = """await Promise.all([
 import('./bridge/src/models.ts'), import('./bridge/src/auth-context.ts'),
 import('./bridge/src/oauth-flow.ts'), import('./bridge/src/compact.ts'),
 import('./bridge/src/snap-render.ts'), import('./bridge/src/cache.ts'),
 import('./bridge/src/memory.ts'), import('./bridge/src/stt/index.ts')
]); process.stderr.write('PROBE_READY\\n')"""
EMPTY_PROBE = "process.stderr.write('PROBE_READY\\n')"


def now_ns() -> int:
    return time.perf_counter_ns()


def ms(delta_ns: int) -> float:
    return delta_ns / 1_000_000.0


class Bridge:
    def __init__(self, auth_db: Path):
        env = os.environ.copy()
        env["CAD_OMP_AUTH_DB"] = str(auth_db)
        self.spawn_ns = now_ns()
        self.proc = subprocess.Popen(
            ["bun", "run", str(BRIDGE)], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, bufsize=-1,
        )
        self.ready_ns: int | None = None
        self.last_response_ns: int | None = None
        self.ready = threading.Event()
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            self.stderr_lines.append(line)
            if "[omp-bridge] ready" in line and self.ready_ns is None:
                self.ready_ns = now_ns()
                self.ready.set()

    def await_ready(self, timeout: float = 10.0) -> float:
        if not self.ready.wait(timeout):
            raise RuntimeError(f"bridge did not become ready: {self.stderr_lines}")
        assert self.ready_ns is not None
        return ms(self.ready_ns - self.spawn_ns)

    def call(self, request_id: int, method: str, params: dict[str, Any]) -> tuple[float, dict[str, Any], bytes]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        wire = json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":")).encode() + b"\n"
        started = now_ns()
        self.proc.stdin.write(wire)
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        ended = now_ns()
        self.last_response_ns = ended
        if not raw:
            raise RuntimeError(f"bridge EOF for {method}: {self.stderr_lines}")
        frame = json.loads(raw)
        if frame.get("id") != request_id or frame.get("ok") is not True:
            raise RuntimeError(f"bad response for {method}: {frame}")
        return ms(ended - started), frame["result"], raw.rstrip(b"\n")

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)


def probe_ready_ms(code: str) -> float:
    started = now_ns()
    proc = subprocess.Popen(
        ["bun", "-e", code], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stderr is not None
    line = proc.stderr.readline()
    seen = now_ns()
    proc.wait(timeout=10)
    if line.rstrip("\n") != "PROBE_READY":
        raise RuntimeError(f"probe failed ({proc.returncode}): {line!r} {proc.stderr.read()!r}")
    return ms(seen - started)


def summarize(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
        "p95_ms": ordered[max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))],
    }


def one_result(auth_db: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    bridge = Bridge(auth_db)
    try:
        bridge.await_ready()
        _, result, _ = bridge.call(1, method, params)
        return result
    finally:
        bridge.close()


def benchmark_method(
    auth_db: Path, method: str, params: dict[str, Any], trials: int, warm_reps: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    first: list[float] = []
    warm: list[float] = []
    sample_result: dict[str, Any] | None = None
    sample_frame = b""
    for _ in range(trials):
        bridge = Bridge(auth_db)
        try:
            bridge.await_ready()
            elapsed, result, frame = bridge.call(1, method, params)
            first.append(elapsed)
            if sample_result is None:
                sample_result = result
                sample_frame = frame
            for repeat in range(warm_reps):
                elapsed, _, _ = bridge.call(2 + repeat, method, params)
                warm.append(elapsed)
        finally:
            bridge.close()
    assert sample_result is not None
    result_wire = json.dumps(sample_result, separators=(",", ":"), ensure_ascii=False).encode()
    return summarize(first), summarize(warm), {
        "result_bytes": len(result_wire), "frame_bytes": len(sample_frame), "result": sample_result,
    }


def stringify_benchmark(value: dict[str, Any], reps: int = 100) -> dict[str, float | int]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as fixture:
        json.dump(value, fixture, ensure_ascii=False, separators=(",", ":"))
        fixture.flush()
        env = os.environ.copy()
        env["PERF_JSON_PATH"] = fixture.name
        env["PERF_REPS"] = str(reps)
        code = """
const value = JSON.parse(await Bun.file(process.env.PERF_JSON_PATH).text());
for (let i = 0; i < 5; i++) JSON.stringify(value);
const samples = [];
let bytes = 0;
for (let i = 0; i < Number(process.env.PERF_REPS); i++) {
  const started = performance.now();
  const encoded = JSON.stringify(value);
  samples.push(performance.now() - started);
  bytes += new TextEncoder().encode(encoded).byteLength;
}
samples.sort((a, b) => a - b);
const mean = samples.reduce((a, b) => a + b, 0) / samples.length;
const middle = Math.floor(samples.length / 2);
const median = samples.length % 2 ? samples[middle] : (samples[middle - 1] + samples[middle]) / 2;
console.log(JSON.stringify({
  n: samples.length, mean_ms: mean, median_ms: median, min_ms: samples[0],
  max_ms: samples[samples.length - 1],
  p95_ms: samples[Math.max(0, Math.min(samples.length - 1, Math.floor(samples.length * 0.95) - 1))],
  bytes: Math.floor(bytes / samples.length),
}));
"""
        return json.loads(subprocess.check_output(["bun", "-e", code], cwd=ROOT, env=env, text=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--cold-reps", type=int, default=15)
    parser.add_argument("--trials", type=int, default=6)
    parser.add_argument("--warm-reps", type=int, default=8)
    args = parser.parse_args()

    auth_db = Path(tempfile.gettempdir()) / "cad-omp-perf-auth.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(auth_db) + suffix).unlink()
        except FileNotFoundError:
            pass
    # Materialize the schema first; measured first calls still open/reload the existing db.
    one_result(auth_db, "auth_state", {})

    anthropic = one_result(auth_db, "models_list", {"provider": "anthropic"})
    priced = [row for row in anthropic["models"] if row.get("priced")][:8]
    events = [
        {
            "provider": row["provider"], "model": row["id"],
            "usage": {"input": 1000 + i, "output": 200 + i, "cacheRead": 300 + i, "cacheWrite": 50 + i},
        }
        for i in range(120) for row in [priced[i % len(priced)]]
    ]

    base_samples = [probe_ready_ms(EMPTY_PROBE) for _ in range(args.cold_reps)]
    module_samples = [probe_ready_ms(MODULE_PROBE) for _ in range(args.cold_reps)]
    ready_samples: list[float] = []
    ping_samples: list[float] = []
    ready_to_ping_samples: list[float] = []
    for _ in range(args.cold_reps):
        bridge = Bridge(auth_db)
        try:
            bridge.call(1, "ping", {})
            ready_samples.append(bridge.await_ready())
            assert bridge.ready_ns is not None and bridge.last_response_ns is not None
            ping_samples.append(ms(bridge.last_response_ns - bridge.spawn_ns))
            ready_to_ping_samples.append(ms(bridge.last_response_ns - bridge.ready_ns))
        finally:
            bridge.close()

    methods: dict[str, dict[str, Any]] = {
        "ping": {},
        "models_list": {},
        "cost_summary": {"events": events},
        "auth_list_providers": {},
        "auth_state": {},
    }
    report: dict[str, Any] = {
        "label": args.label,
        "runtime": {"bun": subprocess.check_output(["bun", "--version"], text=True).strip()},
        "repetitions": {"cold": args.cold_reps, "first_call_trials": args.trials, "warm_per_trial": args.warm_reps},
        "cold": {
            "bun_empty_spawn_to_sentinel": summarize(base_samples),
            "module_import_spawn_to_sentinel": summarize(module_samples),
            "bridge_spawn_to_ready": summarize(ready_samples),
            "bridge_spawn_to_ping_response": summarize(ping_samples),
            "ready_to_ping_response": summarize(ready_to_ping_samples),
        },
        "cost_input": {"events": len(events), "models": [row["id"] for row in priced]},
        "methods": {},
    }
    for method, params in methods.items():
        first, warm, payload = benchmark_method(auth_db, method, params, args.trials, args.warm_reps)
        report["methods"][method] = {
            "params": params if method != "cost_summary" else {"events": len(events)},
            "first": first,
            "warm": warm,
            "payload": {"result_bytes": payload["result_bytes"], "frame_bytes": payload["frame_bytes"]},
            "result": payload["result"],
        }
    report["methods"]["models_list"]["stringify"] = stringify_benchmark(report["methods"]["models_list"]["result"])
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    compact = {
        key: {"first_ms": value["first"]["mean_ms"], "warm_ms": value["warm"]["mean_ms"], "bytes": value["payload"]["result_bytes"]}
        for key, value in report["methods"].items()
    }
    print(json.dumps({
        "label": args.label, "cold": report["cold"], "methods": compact,
        "models_stringify": report["methods"]["models_list"]["stringify"],
    }, indent=2))


if __name__ == "__main__":
    main()
