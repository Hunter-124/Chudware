"""Headless driver for the Chudware sampling profiler — reproduce + localize MODELING slowness.

Loads the resolved platform-native libchudware in a private, in-process engine, runs a
deterministic modeling workload, and writes a flame graph and a complete JSON report.
Never connects to the shared hostd or edits its documents. No GUI, no LLM — just the
geometry kernel under load.

Each iteration builds a fresh document and applies the workload ops (so the OCCT B-rep
compiler — extrude/fillet/boolean/tessellate — actually runs every time, not a cache hit).

Usage:
  python tools/profile_modeling.py                      # default "all" workload, 5 s
  python tools/profile_modeling.py --workload boolean --seconds 8 --hz 2000
  python tools/profile_modeling.py --list               # show available workloads
  python tools/profile_modeling.py --workload fillet --out runs/fillet
  CHUDWARE_LIBCHUDWARE=/absolute/path/libchudware.so python tools/profile_modeling.py --workload pattern

Output (default prefix "prof"):
  <out>.folded            Brendan-Gregg folded stacks  (also -> speedscope.app)
  <out>.speedscope.json   speedscope profile           (-> https://speedscope.app)
  <out>.svg               flame graph (open in a browser)
  <out>.report.json       throughput, engine provenance, raw sampler and native phase report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                      # for flamegraph
sys.path.insert(0, str(_HERE.parent))               # for services.mcp.core_bridge


from services.mcp import core_bridge as cb  # noqa: E402
from tools.perf_regression import _private_engine  # noqa: E402
import flamegraph  # noqa: E402


# ----------------------------------------------------------------- op builders
def _box(name, l, w, h):
    return {"op": "add_feature", "feature": {
        "type": "box", "name": name,
        "params": {"length": cb.dim(l), "width": cb.dim(w), "height": cb.dim(h)}}}


def _fillet(r, name="Round"):
    return {"op": "add_feature", "feature": {
        "type": "fillet", "name": name,
        "params": {"radius": cb.dim(r)}, "refs": {"edges": {"of": "edge"}}}}


def _chamfer(d, name="Chamfer"):
    # NB: chamfer uses "distance" (the validated param name); "length" is the footgun alias.
    return {"op": "add_feature", "feature": {
        "type": "chamfer", "name": name,
        "params": {"distance": cb.dim(d)}, "refs": {"edges": {"of": "edge"}}}}


def _shell(t, name="Shell"):
    # shell opens the face(s) named under refs.remove; "+z" = the top face (open box).
    return {"op": "add_feature", "feature": {
        "type": "shell", "name": name,
        "params": {"thickness": cb.dim(t), "outward": False},
        "refs": {"remove": {"of": "face", "side": "+z"}}}}


def _bolt_circle(count, pcd, dia, name="BoltCircle"):
    # plain numbers per the bolt_circle cheatsheet; a RING of `count` through-holes = a
    # boolean-heavy cut (each hole is a cut from the body).
    return {"op": "add_feature", "feature": {
        "type": "bolt_circle", "name": name,
        "params": {"diameter": dia, "count": count, "pcd": pcd},
        "refs": {"face": {"of": "face", "side": "top"}}}}


# ----------------------------------------------------------------- workloads
# Each is a list of ops dominated by the op type we want to attribute.
def _workloads():
    return {
        "fillet":   [_box("Block", 80, 60, 40), _fillet(3)],
        "chamfer":  [_box("Block", 80, 60, 40), _chamfer(3)],
        "shell":    [_box("Block", 80, 60, 40), _shell(2)],
        # boolean / pattern: a ring of holes -> many cuts from the body
        "boolean":  [_box("Plate", 120, 120, 20), _bolt_circle(24, 90, 6)],
        "pattern":  [_box("Plate", 140, 140, 20), _bolt_circle(48, 110, 5)],
        # the realistic mix — extrude + blend + boolean in one flame graph (shell omitted
        # here because shelling AFTER a fillet is fragile; profile shell on its own)
        "all":      [_box("Block", 100, 80, 40), _fillet(3), _bolt_circle(16, 70, 6)],
    }


# A deliberately non-trivial part used by the INTERACTIVE workloads (pick / open).
# Build once, then measure steady-state picks or saved-cache opens; neither workload
# deliberately invalidates the compiled state.
def _heavy_build():
    return [_box("Plate", 160, 160, 20), _bolt_circle(24, 120, 6), _fillet(2)]


# Interactive workloads don't rebuild a doc per iteration — they build ONCE then exercise an
# interactive RPC in the sample loop (so the flame graph attributes the per-click / per-open cost).
INTERACTIVE = ("pick", "open")


def _print_status(resp):
    fs = (resp.get("rebuild") or {}).get("feature_status", [])
    bad = [s for s in fs if not s.get("ok")]
    bodies = (resp.get("tess") or {}).get("bodies", [])
    print(f"  build: {len(fs)} features, {len(bodies)} body(ies), "
          f"{len(bad)} failed")
    for s in bad:
        print(f"    ! {s.get('id')}: {s.get('error')}")


def _positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return seconds


def _positive_int(value):
    number = int(value)
    if not 0 < number <= 2_147_483_647:
        raise argparse.ArgumentTypeError("must be a positive 32-bit integer")
    return number


def _checked(method, params):
    response = cb.call(method, params)
    if response.get("ok") is False or response.get("error"):
        raise RuntimeError(f"{method} failed: {response}")
    return response


def _validate_build(response, expected_features, context):
    statuses = (response.get("rebuild") or {}).get("feature_status")
    if not isinstance(statuses, list) or len(statuses) != expected_features:
        raise RuntimeError(f"{context}: expected {expected_features} feature statuses, got {statuses!r}")
    bad = [s for s in statuses
           if not isinstance(s, dict) or s.get("ok") is not True or s.get("rolled_back")]
    if bad:
        raise RuntimeError(f"{context}: failed or rolled-back features: {bad!r}")
    if not (response.get("tess") or {}).get("bodies"):
        raise RuntimeError(f"{context}: build produced no bodies")


def _pick(doc_id, iteration):
    response = _checked("doc_resolve_pick", {
        "doc_id": doc_id, "body_id": 1,
        "kind": ("face", "edge")[iteration % 2], "topo_id": 1 + (iteration % 8)})
    if not isinstance(response.get("selector"), dict) or not response["selector"]:
        raise RuntimeError(f"doc_resolve_pick returned no selector: {response}")
    return response


def _fmt_table(rows, cols):
    # rows: list of dict; cols: list of (key, header, width)
    line = "  " + "".join(h.ljust(w) for (_, h, w) in cols)
    print(line)
    print("  " + "-" * (len(line) - 2))
    for r in rows:
        cells = []
        for (k, _, w) in cols:
            v = r.get(k, "")
            if isinstance(v, float):
                v = f"{v:.1f}"
            cells.append(str(v)[:w - 1].ljust(w))
        print("  " + "".join(cells))


@_private_engine()
def main(argv):
    ap = argparse.ArgumentParser(
        description="Sampling-profile a private, in-process Chudware modeling engine.",
        epilog="Set CHUDWARE_LIBCHUDWARE to an explicit .so/.dylib/.dll path, or "
               "CHUDWARE_DIST to its directory. Shared hostd is never used.")
    ap.add_argument("--workload", default="all")
    ap.add_argument("--seconds", type=_positive_seconds, default=5.0, help="how long to sample")
    ap.add_argument("--hz", type=_positive_int, default=1000, help="sample rate (samples/sec)")
    ap.add_argument("--top", type=_positive_int, default=30, help="rows in the hotspot tables")
    ap.add_argument("--out", default="prof", help="output path prefix (parents are created)")
    ap.add_argument("--list", action="store_true", help="list workloads and exit")
    args = ap.parse_args(argv[1:])

    wls = _workloads()
    all_names = sorted(list(wls) + list(INTERACTIVE))
    if args.list:
        print("workloads:", ", ".join(all_names))
        print("  (pick/open: build once, then sample steady-state picks / saved-cache opens)")
        return 0
    if args.workload not in all_names:
        ap.error(f"unknown workload '{args.workload}'. choices: {', '.join(all_names)}")
    ops = _heavy_build() if args.workload in INTERACTIVE else wls[args.workload]
    prefix = Path(args.out).expanduser().resolve()
    paths = {kind: Path(f"{prefix}.{suffix}") for kind, suffix in (
        ("folded", "folded"), ("speedscope", "speedscope.json"),
        ("svg", "svg"), ("report", "report.json"))}
    errors = []
    report = {
        "ok": False, "args": vars(args), "argv": list(argv),
        "platform": {"platform": platform.platform(), "system": platform.system(),
                     "machine": platform.machine(), "python": platform.python_version(),
                     "executable": sys.executable},
        "engine": {"mode": "in_process", "daemon": False},
        "workload_ops": ops, "errors": errors,
        "outputs": {kind: str(path) for kind, path in paths.items()},
    }
    docs = set()
    temporary = None
    profiler_active = False
    raw = None
    started = None
    iters = 0
    elapsed = 0.0
    t0 = None

    def error(context, exc):
        errors.append({"context": context, "type": type(exc).__name__, "message": str(exc)})

    def track(response, context):
        doc_id = response.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            raise RuntimeError(f"{context}: missing doc_id: {response}")
        docs.add(doc_id)
        return doc_id

    def new_doc(name):
        return track(_checked("doc_new", {"kind": "part", "name": name}), "doc_new")

    def close_doc(doc_id):
        _checked("doc_close", {"doc_id": doc_id})
        docs.remove(doc_id)

    def build(doc_id, context):
        response = _checked("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        _validate_build(response, len(ops), context)
        return response

    def open_doc(path, context):
        response = _checked("doc_open", {"path": str(path)})
        doc_id = track(response, context)
        _validate_build(response, len(ops), context)
        cache = response.get("cache")
        if (response.get("cache_hit") is not True or response.get("warm") is not True
                or response.get("cache_miss_reason") or not isinstance(cache, dict)
                or cache.get("features_executed") != 0 or cache.get("hydrate_error")):
            raise RuntimeError(f"{context}: did not restore a warm cache hit: {response}")
        return doc_id, response

    try:
        prefix.parent.mkdir(parents=True, exist_ok=True)
        explicit = os.environ.get("CHUDWARE_LIBCHUDWARE")
        if explicit and not Path(explicit).is_file():
            raise RuntimeError(f"explicit CHUDWARE_LIBCHUDWARE does not exist: {explicit}")
        # _name is the actual path handed to CDLL, not a guessed Windows stage path.
        library = Path(cb._load()._name).resolve()
        digest = hashlib.sha256()
        with library.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        report["engine"].update({"path": str(library), "sha256": digest.hexdigest(),
                                 "size_bytes": library.stat().st_size})
        print(f"Chudware modeling profiler · workload={args.workload} hz={args.hz} "
              f"seconds={args.seconds}\n  engine: {library}")

        probe = new_doc("prof_probe")
        report["probe"] = build(probe, "probe")
        _print_status(report["probe"])
        close_doc(probe)

        pick_doc = None
        open_path = None
        if args.workload == "pick":
            pick_doc = new_doc("prof_pick")
            report["setup"] = build(pick_doc, "pick setup")
            report["warmup"] = [_pick(pick_doc, i) for i in range(12)]
        elif args.workload == "open":
            temporary = tempfile.TemporaryDirectory(prefix="chudware-prof-open-")
            open_path = Path(temporary.name) / "model.step+"
            db = new_doc("prof_open")
            report["setup"] = build(db, "open setup")
            saved = _checked("doc_save", {"doc_id": db, "path": str(open_path)})
            if not saved.get("path") or not open_path.is_file() or not open_path.stat().st_size:
                raise RuntimeError(f"doc_save did not write the workload: {saved}")
            cache = saved.get("cache")
            if (not isinstance(cache, dict) or cache.get("written") is not True
                    or cache.get("skipped_reason") or cache.get("error")):
                raise RuntimeError(f"open fixture did not persist its build cache: {saved}")
            report["save"] = saved
            close_doc(db)
            warm, report["warmup"] = open_doc(open_path, "open warmup")
            close_doc(warm)

        # Mark active before the call so even a failed start response goes through stop.
        profiler_active = True
        started = _checked("prof_start", {"hz": args.hz, "mode": "modeling"})
        if started.get("started") is not True:
            raise RuntimeError(f"profiler did not start a fresh sampling run: {started}")
        print(f"  profiler: {started}")
        t0 = time.perf_counter()
        try:
            while time.perf_counter() - t0 < args.seconds:
                if args.workload == "pick":
                    _pick(pick_doc, iters)
                elif args.workload == "open":
                    doc_id, response = open_doc(open_path, f"measured open {iters + 1}")
                    report["last_measured"] = response
                    close_doc(doc_id)
                else:
                    doc_id = new_doc("prof")
                    report["last_measured"] = build(doc_id, f"measured build {iters + 1}")
                    close_doc(doc_id)
                # Only fully successful iterations count, including closing their documents.
                iters += 1
        finally:
            elapsed = time.perf_counter() - t0
    except (Exception, KeyboardInterrupt) as exc:
        error("workload", exc)
    finally:
        if profiler_active:
            try:
                raw = cb.call("prof_stop", {
                    "top": args.top, "write_folded": str(paths["folded"]),
                    "write_speedscope": str(paths["speedscope"])})
                if raw.get("ok") is not True:
                    raise RuntimeError(f"prof_stop failed: {raw}")
            except (Exception, KeyboardInterrupt) as exc:
                error("profiler stop", exc)
        for doc_id in tuple(docs):
            try:
                close_doc(doc_id)
            except (Exception, KeyboardInterrupt) as exc:
                error(f"closing document {doc_id}", exc)
        if temporary is not None:
            try:
                temporary.cleanup()
            except (Exception, KeyboardInterrupt) as exc:
                error("temporary workload cleanup", exc)

    report["profiler_start"] = started
    report["profiler"] = raw  # Preserve every native field, including folded stacks and losses.
    report["summary"] = (raw or {}).get("summary")
    report["native_phases"] = (raw or {}).get("phases")
    summary = report["summary"] or {}
    report["sampling_diagnostics"] = {
        key: summary.get(key) for key in
        ("samples", "ticks", "ticks_with_target", "dropped", "unresolved_frames",
         "interval_us", "wall_seconds")}
    report["sampling_diagnostics"]["unavailable_fields"] = [
        key for key, value in report["sampling_diagnostics"].items() if value is None]
    if raw and raw.get("ok") is True:
        # Native export failures are reported only by absence from `wrote`, not ok:false.
        for kind in ("folded", "speedscope"):
            try:
                path = paths[kind]
                if str(path) not in raw.get("wrote", []):
                    raise RuntimeError(f"prof_stop did not export {path}")
                text = path.read_text(encoding="utf-8")
                if not text:
                    raise RuntimeError(f"empty profiler export: {path}")
                if kind == "speedscope":
                    json.loads(text)
                elif text != raw["folded"]:
                    raise RuntimeError(f"folded export differs from native report: {path}")
            except Exception as exc:
                error(f"{kind} export", exc)
        try:
            paths["svg"].write_text(
                flamegraph.render_svg(raw["folded"],
                                      title=f"Chudware modeling · {args.workload}"),
                encoding="utf-8")
        except Exception as exc:
            error("flamegraph export", exc)
    measurement_valid = t0 is not None and not errors
    report["throughput"] = {
        "valid": measurement_valid, "completed_iterations": iters,
        "unit": {"pick": "picks", "open": "opens"}.get(args.workload, "builds"),
        "elapsed_seconds": elapsed,
        "iterations_per_second": iters / elapsed if measurement_valid and elapsed > 0 else None,
    }

    report["ok"] = not errors
    try:
        paths["report"].write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                                   encoding="utf-8")
    except Exception as exc:
        error("JSON report export", exc)
        report["ok"] = False
    if not report["ok"]:
        for item in errors:
            print(f"ERROR [{item['context']}]: {item['message']}", file=sys.stderr)
        return 1

    s = raw["summary"]
    throughput = report["throughput"]
    print(f"\n  {iters} {throughput['unit']} in {elapsed:.3f}s "
          f"({throughput['iterations_per_second']:.2f}/s) · {s['samples']} samples")
    print("  sampler summary:", json.dumps(s, sort_keys=True))
    print("\nTIME BY MODULE (self-time — where the CPU actually is):")
    _fmt_table(raw["modules"],
               [("module", "MODULE", 22), ("self", "SAMPLES", 10), ("pct", "%", 8)])
    print("\nHOTTEST FRAMES (self-time):")
    _fmt_table(raw["flat_self"],
               [("frame", "FRAME", 64), ("self", "SAMPLES", 10), ("pct", "%", 8)])
    print("\nwrote:\n  " + "\n  ".join(str(path) for path in paths.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
