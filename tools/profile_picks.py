"""Exact-selector A/B benchmark in a private, in-process Chudware engine.

Build a fresh deterministic plate, 24 bores, and fillet, discover every live face,
edge, and vertex pick, then time fixed full sweeps. Never attaches to shared hostd.
No sampler is needed: JSON includes every selector and every individual call time.

Run each library in a separate process (no builds are performed):
  python tools/profile_picks.py run --library /path/before.so --out before.json
  python tools/profile_picks.py run --library /path/after.so --out after.json
  python tools/profile_picks.py compare before.json after.json --out comparison.json

Comparison is exact for workload ops, discovered topology, and full pick replies.
Feature IDs are explicit and deterministic; owner_feature, sid, hint_index, normals,
and all other selector fields are never discarded or rounded for comparison. Only
run metadata (including the separately recorded random document ID), build replies,
engine provenance, and timings are outside the selector comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path


_KINDS = ("face", "edge", "vertex")
_SCHEMA = "chudware-pick-sweep/1"


def _positive_int(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stats(samples):
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("no successful pick samples")
    return {
        "calls": len(ordered), "total_ns": sum(ordered),
        "min_ns": ordered[0], "median_ns": statistics.median(ordered),
        "mean_ns": statistics.mean(ordered),
        "p95_ns": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_ns": ordered[-1],
    }


def _write_report(path, report):
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def _profile(args):
    report = {
        "schema": _SCHEMA, "ok": False,
        "platform": {"platform": platform.platform(), "machine": platform.machine(),
                     "python": platform.python_version(), "executable": sys.executable},
        "engine": {"mode": "in_process", "daemon": False},
        "parameters": {"sweeps": args.sweeps, "warmup_sweeps": args.warmup_sweeps},
        "errors": [], "topology": [], "selections": [], "raw_sweeps": [],
        "comparison_fields": ["workload_ops", "topology", "selections"],
    }
    doc_id = None
    modeling = None
    try:
        if args.library is not None:
            library = args.library.expanduser().resolve()
            if not library.is_file():
                raise ValueError(f"library does not exist: {library}")
            os.environ["CHUDWARE_LIBCHUDWARE"] = str(library)
        explicit = os.environ.get("CHUDWARE_LIBCHUDWARE")
        if explicit and not Path(explicit).is_file():
            raise ValueError(f"explicit CHUDWARE_LIBCHUDWARE does not exist: {explicit}")
        # main scopes private endpoints before this bridge import/load.
        from tools import profile_modeling as modeling

        cb = modeling.cb
        library = Path(cb._load()._name).resolve()
        digest = hashlib.sha256()
        with library.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        report["engine"].update({"path": str(library), "sha256": digest.hexdigest(),
                                 "size_bytes": library.stat().st_size})
        # Explicit feature IDs make owner_feature just as comparable as geometry.
        ops = modeling._heavy_build()
        for op, feature_id in zip(ops, ("pick_plate", "pick_bores", "pick_round"), strict=True):
            op["feature"]["id"] = feature_id
        report["workload_ops"] = ops
        created = modeling._checked("doc_new", {"kind": "part", "name": "Pick Sweep"})
        doc_id = created.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            raise RuntimeError(f"doc_new returned no document ID: {created!r}")
        report["document_id"] = doc_id
        setup = modeling._checked("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        report["setup"] = setup
        modeling._validate_build(setup, len(ops), "pick sweep setup")
        if len(setup["tess"]["bodies"]) != 1:
            raise RuntimeError("heavy plate workload must produce exactly one body")

        def pick(target):
            response = modeling._checked("doc_resolve_pick", {"doc_id": doc_id, **target})
            selector = response.get("selector")
            if not isinstance(selector, dict) or selector.get("kind") != target["kind"]:
                raise RuntimeError(f"pick returned no matching selector: {target!r}: {response!r}")
            if selector.get("hint_index") != target["topo_id"] - 1:
                raise RuntimeError(f"pick returned wrong topology ordinal: {target!r}: {response!r}")
            return response

        # Query actual MapShapes IDs; never assume the first N are the whole model.
        # Queries support faces and edges only, so vertices are discovered separately
        # until the native, structured invalid-pick boundary (not any arbitrary error).
        bodies = set()
        for kind in ("face", "edge"):
            query = modeling._checked("doc_query", {"doc_id": doc_id, "query": {"of": kind}})
            matches = query.get("matches")
            if not isinstance(matches, list) or not matches or query.get("count") != len(matches):
                raise RuntimeError(f"incomplete {kind} query: {query!r}")
            identities = set()
            for match in matches:
                body, topo = match.get("body"), match.get("topo")
                if type(body) is not int or body < 1 or type(topo) is not int or topo < 1:
                    raise RuntimeError(f"query lacks live topology IDs: {match!r}")
                if match.get("kind") != kind or match.get("hint_index") != topo - 1:
                    raise RuntimeError(f"query has inconsistent topology ordinal: {match!r}")
                identities.add((body, topo))
            if len(identities) != len(matches):
                raise RuntimeError(f"query returned duplicate {kind} IDs")
            for body, topo in sorted(identities):
                bodies.add(body)
                target = {"body_id": body, "kind": kind, "topo_id": topo}
                report["topology"].append(target)
                report["selections"].append({"target": target, "response": pick(target)})
        if len(bodies) != 1:
            raise RuntimeError(f"expected one queried body, got {sorted(bodies)!r}")
        for body in sorted(bodies):
            topo = 1
            while True:
                target = {"body_id": body, "kind": "vertex", "topo_id": topo}
                try:
                    response = pick(target)
                except cb.CoreError as exc:
                    detail = exc.detail
                    if (exc.code != "invalid_op" or not isinstance(detail, dict)
                            or detail.get("reason") != "invalid pick"
                            or detail.get("body_id") != body or detail.get("topo_id") != topo):
                        raise
                    if topo == 1:
                        raise RuntimeError("heavy plate workload has no live vertex picks") from exc
                    break
                report["topology"].append(target)
                report["selections"].append({"target": target, "response": response})
                topo += 1

        expected = [_canonical(row["response"]) for row in report["selections"]]
        targets = report["topology"]
        for _ in range(args.warmup_sweeps):
            for index, target in enumerate(targets):
                response = pick(target)
                if _canonical(response) != expected[index]:
                    raise RuntimeError(f"selector changed during warmup: {target!r}: {response!r}")
        for sweep in range(args.sweeps):
            samples = []
            started = time.perf_counter_ns()
            for index, target in enumerate(targets):
                before = time.perf_counter_ns()
                response = pick(target)
                samples.append(time.perf_counter_ns() - before)
                if _canonical(response) != expected[index]:
                    raise RuntimeError(f"selector changed during sweep {sweep}: {target!r}: {response!r}")
            report["raw_sweeps"].append({"sweep": sweep, "wall_ns": time.perf_counter_ns() - started,
                                         "samples_ns": samples})
        all_samples = [value for sweep in report["raw_sweeps"] for value in sweep["samples_ns"]]
        by_kind = {
            kind: _stats([sweep["samples_ns"][index]
                          for sweep in report["raw_sweeps"]
                          for index, target in enumerate(targets) if target["kind"] == kind])
            for kind in _KINDS
        }
        report["timing"] = {"clock": "perf_counter_ns", "unit": "ns", "all": _stats(all_samples),
                             "by_kind": by_kind, "completed_sweeps": len(report["raw_sweeps"]),
                             "samples_order": "topology (one full fixed sweep per samples_ns)",
                             "scope": "in-process JSON RPC, reply decoding, and pick validation; "
                                      "selector comparison is outside each per-call sample"}
    except (Exception, KeyboardInterrupt) as exc:
        report["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    finally:
        if doc_id is not None and modeling is not None:
            try:
                modeling._checked("doc_close", {"doc_id": doc_id})
            except (Exception, KeyboardInterrupt) as exc:
                report["errors"].append({"context": "closing private document",
                                          "type": type(exc).__name__, "message": str(exc)})
    report["ok"] = not report["errors"]
    path = _write_report(args.out, report)
    if not report["ok"]:
        print(json.dumps(report["errors"], indent=2), file=sys.stderr)
        print(f"Failed pick report: {path}", file=sys.stderr)
        return 1
    counts = {kind: sum(target["kind"] == kind for target in report["topology"]) for kind in _KINDS}
    print(f"Engine: {report['engine']['path']} ({report['engine']['sha256']})")
    print(f"Topology: {counts}; completed {args.sweeps} full sweeps")
    print(json.dumps(report["timing"], indent=2))
    print(f"Wrote {path}")
    return 0


def _differences(before, after, path="$", result=None):
    if result is None:
        result = []
    if len(result) >= 20:
        return result
    if type(before) is not type(after):
        result.append({"path": path, "before": before, "after": after})
    elif isinstance(before, dict):
        for key in sorted(before.keys() | after.keys()):
            if key not in before or key not in after:
                result.append({"path": f"{path}.{key}", "before_present": key in before,
                               "after_present": key in after})
            else:
                _differences(before[key], after[key], f"{path}.{key}", result)
            if len(result) >= 20:
                break
    elif isinstance(before, list):
        if len(before) != len(after):
            result.append({"path": path, "before_length": len(before), "after_length": len(after)})
        for index, (left, right) in enumerate(zip(before, after)):
            _differences(left, right, f"{path}[{index}]", result)
            if len(result) >= 20:
                break
    elif before != after:
        result.append({"path": path, "before": before, "after": after})
    return result


def _compare(args):
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    for name, report in (("before", before), ("after", after)):
        if report.get("schema") != _SCHEMA or report.get("ok") is not True:
            raise ValueError(f"{name} is not a successful {_SCHEMA} result")
    fields = ("workload_ops", "topology", "selections")
    left = {field: before[field] for field in fields}
    right = {field: after[field] for field in fields}
    identical = _canonical(left) == _canonical(right)
    result = {
        "ok": identical, "selectors_identical": identical,
        "before": {"path": str(args.before.resolve()), "engine": before["engine"]},
        "after": {"path": str(args.after.resolve()), "engine": after["engine"]},
        "compared_fields": list(fields), "ignored_selector_fields": [],
        "excluded_run_metadata": "document_id, setup reply, platform, engine, parameters, timing",
        "selection_count": len(before["selections"]),
        "differences": _differences(left, right), "latency": {},
    }
    for kind in ("all", *_KINDS):
        b = before["timing"]["all"] if kind == "all" else before["timing"]["by_kind"][kind]
        a = after["timing"]["all"] if kind == "all" else after["timing"]["by_kind"][kind]
        result["latency"][kind] = {
            "before_mean_ns": b["mean_ns"], "after_mean_ns": a["mean_ns"],
            "mean_speedup": b["mean_ns"] / a["mean_ns"],
            "before_median_ns": b["median_ns"], "after_median_ns": a["median_ns"],
            "median_speedup": b["median_ns"] / a["median_ns"],
        }
    if args.out is not None:
        _write_report(args.out, result)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if identical else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="build and measure a new private document")
    run.add_argument("--library", type=Path, help="explicit native library (or CHUDWARE_LIBCHUDWARE)")
    run.add_argument("--out", type=Path, required=True, help="complete JSON result path")
    run.add_argument("--sweeps", type=_positive_int, default=10, help="measured full sweeps (default: 10)")
    run.add_argument("--warmup-sweeps", type=_positive_int, default=1, help="untimed full sweeps (default: 1)")
    compare = subparsers.add_parser("compare", help="compare full selector replies and show latency ratios")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--out", type=Path, help="optional comparison JSON result")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from tools.perf_regression import _private_engine
            with _private_engine():
                return _profile(args)
        return _compare(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
