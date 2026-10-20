"""Isolated modeling performance measurements and compatible-baseline comparisons.

BUILD measures doc_new + doc_apply_ops through Python/ctypes response decoding, including
geometry and tessellation. Validation and doc_close are outside the timed interval.
OPEN_WARM measures only doc_open of a freshly saved, persisted-cache fixture; every open
must report cache_hit=true, warm=true and cache.features_executed=0. It is not a cold-open
or rebuild benchmark. Warmups, fixture construction and diagnostics are validated too.

Wall and process CPU times use monotonic clocks. JSON includes every timed sample,
median/nearest-rank p95/min/max in ms, process-lifetime peak RSS where supported, loaded
library identity, ABI/build ID, machine and thread settings. RSS is not a per-op delta.

Usage:
  python tools/perf_regression.py --baseline /tmp/perf.json --workload box --update
  python tools/perf_regression.py --baseline /tmp/perf.json --workload box --json /tmp/run.json
  python tools/perf_regression.py --workload 'fil*' --workload boolean --iters 9

--update replaces the selected baseline with this successful run. A missing baseline is
created as before, explicitly without a comparison. Existing legacy/incompatible/empty
baselines are refused; open_cold is never aliased to open_warm. The default baseline path
remains tools/perf_baseline.json. Use a scratch baseline for machine-local investigations.

CHUDWARE_LIBCHUDWARE selects the library; daemon mode is forcibly disabled, including its
live-endpoint liveness probe. No user documents or shared daemon sessions are used.
Exit codes: 0 pass/baseline recorded, 1 regression, 2 engine unavailable, 3 invalid
arguments/workload/baseline or failed measurement/report write.
"""
from __future__ import annotations

import argparse
import ctypes
import fnmatch
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

try:
    import resource
except ImportError:  # Windows has no stdlib resource module.
    resource = None

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # repo root -> services.mcp.core_bridge

from services.mcp import core_bridge as cb  # noqa: E402

BASELINE = _HERE / "perf_baseline.json"
SCHEMA_VERSION = 2
WARMUP = 1
BUILD_TIMING = "doc_new+doc_apply_ops; through response decoding; excludes validation+doc_close"
OPEN_TIMING = "doc_open; persisted cache hit; through response decoding; excludes validation+doc_close"


@contextmanager
def _private_engine():
    """Isolate execution, not imports; restore caller discovery even on failure."""
    names = ("CHUDWARE_ENGINE_DAEMON", "CHUDWARE_HOSTD_SOCKET",
             "CHUDWARE_HOSTD_PIPE", "CHUDWARE_LIBCHUDWARE")
    previous = {name: os.environ.get(name) for name in names}
    token = uuid.uuid4().hex
    # The bridge's legacy warning probes hostd even in in-process mode.
    os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"
    os.environ["CHUDWARE_HOSTD_SOCKET"] = str(
        Path(tempfile.gettempdir()) / f"chudware-perf-{token}.sock")
    os.environ["CHUDWARE_HOSTD_PIPE"] = rf"\\.\pipe\chudware-perf-{token}"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

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
# Saved-cache fixture for open_warm, never compared with the legacy open_cold metric.
OPEN_PART = [_box("Plate", 160, 160, 20), _bolt(24, 120, 6), _fillet(2)]


class MeasurementError(RuntimeError):
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


class BaselineError(ValueError):
    pass


def _failure(exc):
    result = {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
    if getattr(exc, "code", None):
        result["code"] = exc.code
    if getattr(exc, "detail", None) is not None:
        # Keep invalid engine numbers as explicit text in an otherwise strict JSON failure record.
        result["detail"] = json.loads(json.dumps(exc.detail, default=str), parse_constant=str)
    return result


def _rpc(method, params):
    response = cb.call(method, params)
    if not isinstance(response, dict) or response.get("ok") is False or response.get("error"):
        raise MeasurementError(f"{method}: unsuccessful response", response)
    return response


def _doc_id(response):
    doc_id = response.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id:
        raise MeasurementError("engine returned no document ID", response)
    return doc_id


def _close(doc_id):
    _rpc("doc_close", {"doc_id": doc_id})


@contextmanager
def _document(response):
    doc_id = _doc_id(response)
    error = None
    try:
        yield doc_id
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            _close(doc_id)
        except Exception as exc:
            if error is None:
                raise
            raise MeasurementError(
                f"{error}; document cleanup also failed: {exc}",
                {"measurement": _failure(error), "cleanup": _failure(exc)}) from error


def _finite_number(value, minimum=0):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= minimum)


def _validate_build(response, ops):
    """A returned RPC is not proof of a committed, displayable successful build."""
    rebuild = response.get("rebuild")
    statuses = rebuild.get("feature_status") if isinstance(rebuild, dict) else None
    if not isinstance(statuses, list) or len(statuses) != len(ops):
        raise MeasurementError("missing/incomplete feature statuses", response)
    for status in statuses:
        if (not isinstance(status, dict) or status.get("ok") is not True
                or status.get("error") or status.get("rolled_back")
                or status.get("dangling_refs") or not _finite_number(status.get("ms"))):
            raise MeasurementError("feature build failed or returned invalid status", status)
        data = status.get("data")
        if isinstance(data, dict) and (data.get("ok") is False or data.get("error")):
            raise MeasurementError("feature result reports failure", status)
    tree = response.get("tree")
    features = tree.get("features") if isinstance(tree, dict) else None
    tess = response.get("tess")
    if (response.get("ok") is False or response.get("error") or "rejected_tree" in response
            or not isinstance(features, list) or len(features) != len(ops)
            or not _finite_number(response.get("volume")) or response["volume"] <= 0
            or not isinstance(tess, dict) or not tess.get("bodies")
            or not tess.get("aicm_path")):
        raise MeasurementError("build was rejected or produced no valid geometry/display result",
                               response)


def _peak_rss():
    result = {"available": False, "unit": "bytes", "scope": "process lifetime high-water mark"}
    if sys.platform == "linux":
        # ru_maxrss can retain a fork launcher's peak across exec. VmHWM belongs
        # to this executable's address space, not the process that launched it.
        result["scope"] = "current executable image lifetime high-water mark"
        try:
            with Path("/proc/self/status").open(encoding="ascii") as status:
                for line in status:
                    if line.startswith("VmHWM:"):
                        fields = line.split()
                        if len(fields) != 3 or fields[2] != "kB":
                            raise ValueError("unexpected VmHWM units")
                        result.update(available=True, value=int(fields[1]) * 1024,
                                      source="/proc/self/status VmHWM")
                        return result
            result["reason"] = "VmHWM is unavailable in /proc/self/status"
        except (OSError, ValueError) as exc:
            result["reason"] = str(exc)
        return result
    if resource is None or sys.platform != "darwin":
        result["reason"] = "stdlib peak RSS units unavailable on this platform"
        return result
    try:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError) as exc:
        result["reason"] = str(exc)
        return result
    result.update(available=True, value=int(value),
                  source="getrusage(RUSAGE_SELF).ru_maxrss")
    return result


def _summary(values):
    if not values or any(not _finite_number(value) for value in values):
        raise MeasurementError("timing samples must be nonempty, finite and nonnegative", values)
    ordered = sorted(values)
    return {"median_ms": statistics.median(values),
            "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
            "min_ms": ordered[0], "max_ms": ordered[-1]}


def _stats(samples):
    return {"status": "ok", "unit": "ms", "n": len(samples), "samples": samples,
            **_summary([sample["wall_ms"] for sample in samples]),
            "cpu_time": {"available": True, "unit": "ms",
                         "scope": "process CPU; all engine threads; same timed interval",
                         **_summary([sample["cpu_ms"] for sample in samples])},
            "peak_rss": _peak_rss()}


def _elapsed(wall_start, cpu_start):
    wall_end = time.perf_counter_ns()
    cpu_end = time.process_time_ns()
    return {"wall_ms": (wall_end - wall_start) / 1_000_000,
            "cpu_ms": (cpu_end - cpu_start) / 1_000_000}


def _build_once(ops):
    cpu_start, wall_start = time.process_time_ns(), time.perf_counter_ns()
    with _document(_rpc("doc_new", {"kind": "part", "name": "perf"})) as doc_id:
        response = _rpc("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        sample = _elapsed(wall_start, cpu_start)
        _validate_build(response, ops)
        return sample


def _time_iterations(sample_fn, iters, warmup):
    if not isinstance(iters, int) or isinstance(iters, bool) or iters <= 0:
        raise MeasurementError("iters must be a positive integer")
    if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 0:
        raise MeasurementError("warmup must be a nonnegative integer")
    samples, warmups = [], []
    for phase, count, target in (("warmup", warmup, warmups), ("sample", iters, samples)):
        for index in range(count):
            try:
                target.append(sample_fn())
            except Exception as exc:
                raise MeasurementError(
                    f"{phase} {index + 1} failed: {exc}",
                    {"phase": phase, "iteration": index + 1, "cause": _failure(exc),
                     "completed_samples": samples, "completed_warmups": warmups}) from exc
    result = _stats(samples)
    result["warmup_count"] = warmup
    result["warmups"] = warmups
    return result


def _time_build(ops, iters, warmup=WARMUP):
    """Time new+apply through response decoding; validate and close outside the timer."""
    return _time_iterations(lambda: _build_once(ops), iters, warmup)


def _time_open(iters, warmup=WARMUP):
    with tempfile.TemporaryDirectory(prefix="chudware-perf-open-") as directory:
        path = str(Path(directory) / "fixture.step+")
        with _document(_rpc("doc_new", {"kind": "part", "name": "perf_open"})) as doc_id:
            response = _rpc("doc_apply_ops", {"doc_id": doc_id, "ops": OPEN_PART})
            _validate_build(response, OPEN_PART)
            saved = _rpc("doc_save", {"doc_id": doc_id, "path": path})
            cache = saved.get("cache")
            if (not isinstance(cache, dict) or cache.get("written") is not True
                    or cache.get("skipped_reason") or cache.get("error")
                    or not Path(path).is_file()):
                raise MeasurementError("open fixture did not persist its build cache", saved)

        def sample_open():
            cpu_start, wall_start = time.process_time_ns(), time.perf_counter_ns()
            response = _rpc("doc_open", {"path": path})
            sample = _elapsed(wall_start, cpu_start)
            with _document(response):
                _validate_build(response, OPEN_PART)
                cache = response.get("cache")
                if (response.get("cache_hit") is not True or response.get("warm") is not True
                        or response.get("cache_miss_reason") or not isinstance(cache, dict)
                        or cache.get("features_executed") != 0 or cache.get("hydrate_error")):
                    raise MeasurementError("open_warm did not restore a warm cache hit", response)
                sample["cache"] = {"cache_hit": response["cache_hit"], "warm": response["warm"],
                                   **cache}
                return sample

        return _time_iterations(sample_open, iters, warmup)


# ------------------------------------------------------------------ diagnostics
def _per_feature_ms(ops):
    with _document(_rpc("doc_new", {"kind": "part", "name": "diag"})) as doc_id:
        response = _rpc("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        _validate_build(response, ops)
        return response["rebuild"]["feature_status"]


def _contention_burst(ops, threads=6, per=12):
    """Concurrent in-process builds; every worker failure is returned to the caller."""
    try:
        _rpc("prof_contention", {"reset": True})
    except cb.CoreError as exc:
        if exc.code == "unknown_method" or "unknown method" in str(exc).lower():
            return {"available": False, "reason": str(exc)}
        raise

    def worker():
        for _ in range(per):
            _build_once(ops)

    errors = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker) for _ in range(threads)]
        for index, future in enumerate(futures):
            try:
                future.result()
            except Exception as exc:
                errors.append({"worker": index + 1, **_failure(exc)})
    if errors:
        raise MeasurementError("contention workers failed", errors)
    response = _rpc("prof_contention", {})
    for field in ("pct_wall_blocked", "contended_pct", "max_waiters", "avg_wait_ms"):
        if not _finite_number(response.get(field)):
            raise MeasurementError("invalid contention diagnostic", response)
    return {"available": True, "threads": threads, "builds_per_thread": per, **response}


def _engine_metadata():
    dll = cb._load()
    path = Path(dll._name).resolve(strict=True)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as library:
        for block in iter(lambda: library.read(1024 * 1024), b""):
            digest.update(block)
    # Identify the exact loaded library, independently of RPC routing/wrappers.
    try:
        abi_fn = dll.chudware_abi_version
    except AttributeError as exc:
        raise MeasurementError("loaded engine exports no chudware_abi_version") from exc
    abi_fn.argtypes = []
    abi_fn.restype = ctypes.c_char_p
    value = abi_fn()
    if not value:
        raise MeasurementError("loaded engine returned no ABI metadata")
    abi = value.decode("utf-8")
    ping = _rpc("ping", {})
    try:
        build_fn = dll.chudware_build_id
    except AttributeError:
        build = {"available": False, "reason": "library exports no chudware_build_id"}
    else:
        build_fn.argtypes = []
        build_fn.restype = ctypes.c_char_p
        value = build_fn()
        build = {"available": bool(value), "value": value.decode("utf-8") if value else None,
                 "source": "loaded library chudware_build_id()"}
    return {"path": str(path), "sha256": digest.hexdigest(), "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "build_id": build, "abi": abi,
            "capabilities": ping.get("capabilities", []), "mode": "in_process"}


def _environment():
    thread_names = {"CHUDWARE_SIM_THREADS", "OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OMP_DYNAMIC",
                    "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "TBB_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"}
    thread_names.update(name for name in os.environ if "THREAD" in name.upper())
    return {"system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "hostname": platform.node(),
            "processor": platform.processor(), "cpu_count": os.cpu_count(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "python": platform.python_version(), "python_implementation": platform.python_implementation(),
            "thread_env": {name: os.environ.get(name) for name in sorted(thread_names)}}


def _select_workloads(filters=None):
    available = [*WORKLOADS, "open_warm"]
    if not filters:
        return available
    selected = set()
    for pattern in filters:
        matches = fnmatch.filter(available, pattern)
        if not matches:
            raise ValueError(f"workload {pattern!r} matches nothing; choose from {', '.join(available)}")
        selected.update(matches)
    return [name for name in available if name in selected]


def _workload_contract(name):
    timing = OPEN_TIMING if name == "open_warm" else BUILD_TIMING
    ops = OPEN_PART if name == "open_warm" else WORKLOADS[name]
    payload = {"ops": ops, "timing": timing, "warmup": WARMUP}
    signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {"signature": signature, "timing": timing, "warmup_count": WARMUP}


# ------------------------------------------------------------------ run + compare
def run(iters, workloads=None, engine=None):
    selected = _select_workloads(workloads)
    if not isinstance(iters, int) or isinstance(iters, bool) or iters <= 0:
        raise ValueError("iters must be a positive integer")
    current = {"schema_version": SCHEMA_VERSION, "status": "ok", "engine": engine or _engine_metadata(),
               "environment": _environment(), "iters": iters, "workloads": selected,
               "metrics": {}, "diagnostics": {}, "failures": [],
               "clocks": {"wall": "perf_counter_ns", "cpu": "process_time_ns",
                          "percentile": "nearest-rank p95"}}
    for name in selected:
        try:
            metric = _time_open(iters) if name == "open_warm" else _time_build(WORKLOADS[name], iters)
        except Exception as exc:
            metric = _failure(exc)
            current["failures"].append({"workload": name, **metric})
        current["metrics"][name] = {**metric, **_workload_contract(name)}
    build_names = [name for name in selected if name in WORKLOADS]
    for name in build_names:
        key = f"per_feature_ms_{name}"
        try:
            current["diagnostics"][key] = {"status": "ok", "features": _per_feature_ms(WORKLOADS[name])}
        except Exception as exc:
            failure = _failure(exc)
            current["diagnostics"][key] = failure
            current["failures"].append({"diagnostic": key, **failure})
    if build_names:
        name = build_names[-1]
        try:
            current["diagnostics"]["contention_burst"] = {
                "status": "ok", "workload": name, **_contention_burst(WORKLOADS[name])}
        except Exception as exc:
            failure = _failure(exc)
            current["diagnostics"]["contention_burst"] = failure
            current["failures"].append({"diagnostic": "contention_burst", **failure})
    else:
        current["diagnostics"]["contention_burst"] = {
            "available": False, "reason": "no build workload selected"}
    current["peak_rss"] = _peak_rss()
    if current["failures"]:
        current["status"] = "failed"
    return current


def _valid_metric(name, metric):
    if not isinstance(metric, dict) or metric.get("status") != "ok" or metric.get("unit") != "ms":
        raise BaselineError(f"{name}: metric is not a successful ms measurement")
    n, samples = metric.get("n"), metric.get("samples")
    if (not isinstance(n, int) or isinstance(n, bool) or n <= 0
            or not isinstance(samples, list) or len(samples) != n):
        raise BaselineError(f"{name}: nonempty raw samples matching n are required")
    for sample in samples:
        if (not isinstance(sample, dict) or not _finite_number(sample.get("wall_ms"))
                or sample["wall_ms"] <= 0 or not _finite_number(sample.get("cpu_ms"))):
            raise BaselineError(f"{name}: invalid raw timing sample")
    expected = _summary([sample["wall_ms"] for sample in samples])
    for field, value in expected.items():
        if not _finite_number(metric.get(field)) or not math.isclose(metric[field], value, rel_tol=1e-12):
            raise BaselineError(f"{name}: {field} disagrees with its raw samples")


def compare(current, baseline, threshold):
    """Return regression/table rows only when every selected metric is comparable."""
    if not _finite_number(threshold):
        raise BaselineError("threshold must be finite and nonnegative")
    for label, record in (("current", current), ("baseline", baseline)):
        if (not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION
                or record.get("status") != "ok" or record.get("failures")
                or not isinstance(record.get("metrics"), dict) or not record["metrics"]):
            raise BaselineError(f"{label} is failed, empty or uses an incompatible measurement schema; "
                                "capture a successful scratch baseline with --update")
        if not isinstance(record.get("environment"), dict) or not record["environment"]:
            raise BaselineError(f"{label} has no machine/thread environment")
        if not isinstance(record.get("engine"), dict) or not record["engine"].get("abi"):
            raise BaselineError(f"{label} has no engine ABI metadata")
    if current["environment"] != baseline["environment"]:
        raise BaselineError("baseline machine, Python or thread settings differ from this run")
    if current["engine"]["abi"] != baseline["engine"]["abi"]:
        raise BaselineError("baseline engine ABI differs from this run")
    rows, regressions = [], []
    for name, metric in current["metrics"].items():
        prior = baseline["metrics"].get(name)
        if prior is None:
            raise BaselineError(f"baseline has no {name!r} metric (open_cold is not open_warm)")
        _valid_metric(name, metric)
        _valid_metric(name, prior)
        for field in ("signature", "timing", "warmup_count"):
            if metric.get(field) is None or metric[field] != prior.get(field):
                raise BaselineError(f"{name}: baseline workload/timing contract differs ({field})")
        cur_ms, base_ms = metric["median_ms"], prior["median_ms"]
        delta = (cur_ms - base_ms) / base_ms
        flag = ""
        if delta > threshold and cur_ms - base_ms > 1.0:
            flag = "REGRESSION"
            regressions.append((name, base_ms, cur_ms, delta))
        elif delta < -threshold and base_ms - cur_ms > 1.0:
            flag = "faster"
        rows.append((name, cur_ms, base_ms, delta, flag))
    return regressions, rows


def _print_table(rows):
    print(f"  {'workload':14} {'median_ms':>10} {'baseline':>10} {'delta':>8}  flag")
    for name, current, baseline, delta, flag in rows:
        print(f"  {name:14} {current:10.3f} {baseline:10.3f} {delta:>+8.1%}  {flag}")


def _write_record(path, record):
    Path(path).write_text(json.dumps(record, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


@_private_engine()
def main(argv):
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iters", type=int, default=7, help="positive timed iterations per workload")
    parser.add_argument("--threshold", type=float, default=0.25, help="finite nonnegative regression fraction")
    parser.add_argument("--baseline", type=Path, default=BASELINE, help="baseline path (default: %(default)s)")
    parser.add_argument("--workload", action="append", help="workload name/glob; repeat to select several")
    parser.add_argument("--update", action="store_true", help="replace baseline with a successful selected run")
    parser.add_argument("--json", default="", help="also write the full run record, including failures")
    args = None
    report_path = None
    current = {"schema_version": SCHEMA_VERSION, "status": "failed", "failures": []}
    code = 3
    try:
        args = parser.parse_args(argv[1:])
        if args.json and Path(args.json).resolve() == args.baseline.resolve():
            raise ValueError("--json must not overwrite --baseline")
        report_path = args.json
        if args.iters <= 0 or not _finite_number(args.threshold):
            raise ValueError("--iters must be >0 and --threshold finite and nonnegative")
        selected = _select_workloads(args.workload)
        try:
            cb._load()
        except Exception as exc:
            current["failures"].append({"stage": "engine_load", **_failure(exc)})
            print(f"ERROR: libchudware unavailable: {exc}", file=sys.stderr)
            code = 2
        else:
            current = run(args.iters, selected)
            print(f"Chudware perf-regression: iters={args.iters}, engine={current['engine']['path']}")
            print(f"  sha256={current['engine']['sha256']}, build={current['engine']['build_id']}")
            print(f"  BUILD interval: {BUILD_TIMING}")
            print(f"  OPEN interval: {OPEN_TIMING}")
            print(f"  thread env: {current['environment']['thread_env']}")
            print(f"  peak RSS: {current['peak_rss']}")
            for name, metric in current["metrics"].items():
                if metric["status"] == "ok":
                    print(f"  {name}: median={metric['median_ms']:.3f}ms p95={metric['p95_ms']:.3f}ms "
                          f"min={metric['min_ms']:.3f}ms max={metric['max_ms']:.3f}ms "
                          f"CPU median={metric['cpu_time']['median_ms']:.3f}ms")
            for failure in current["failures"]:
                print(f"ERROR: {failure}", file=sys.stderr)
            if current["status"] != "ok":
                current["comparison"] = {"status": "not_run", "reason": "failed measurements"}
            elif args.update or not args.baseline.exists():
                existed = args.baseline.exists()
                _write_record(args.baseline, current)
                current["comparison"] = {"status": "not_run", "reason": "baseline recorded"}
                print(f"  {'updated' if existed else 'created'} baseline -> {args.baseline}; no comparison")
                code = 0
            else:
                baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
                regressions, rows = compare(current, baseline, args.threshold)
                _print_table(rows)
                current["comparison"] = {"status": "regression" if regressions else "pass",
                                         "baseline": str(args.baseline), "threshold": args.threshold,
                                         "regressions": regressions}
                code = 1 if regressions else 0
                print(f"  {len(regressions)} regression(s)" if regressions else "  no regressions")
    except Exception as exc:
        current["status"] = "failed"
        current["failures"].append({"stage": "measurement_or_comparison", **_failure(exc)})
        print(f"ERROR: {exc}", file=sys.stderr)
        code = 3
    if report_path:
        try:
            _write_record(report_path, current)
            print(f"  wrote run record -> {report_path}")
        except Exception as exc:
            print(f"ERROR: cannot write run record: {exc}", file=sys.stderr)
            code = 3
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
