"""Headless driver for the AI-CAD sampling profiler — reproduce + localize MODELING slowness.

Loads the staged libaicad.dll (the same engine the app uses), runs a deliberately heavy,
deterministic modeling workload, samples it with the in-process profiler, and writes a flame
graph + a hotspot report. No GUI, no LLM — just the geometry kernel under load, so the
numbers are reproducible and the slow op is easy to spot.

Each iteration builds a fresh document and applies the workload ops (so the OCCT B-rep
compiler — extrude/fillet/boolean/tessellate — actually runs every time, not a cache hit).

Usage:
  python tools/profile_modeling.py                      # default "all" workload, 5 s
  python tools/profile_modeling.py --workload boolean --seconds 8 --hz 2000
  python tools/profile_modeling.py --list               # show available workloads
  python tools/profile_modeling.py --workload fillet --out runs/fillet

Output (default prefix "prof"):
  <out>.folded            Brendan-Gregg folded stacks  (also -> speedscope.app)
  <out>.speedscope.json   speedscope profile           (-> https://speedscope.app)
  <out>.svg               flame graph (open in a browser)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                      # for flamegraph
sys.path.insert(0, str(_HERE.parent))               # for mcp_server.core_bridge

from mcp_server import core_bridge as cb  # noqa: E402
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


# A deliberately non-trivial part used by the INTERACTIVE workloads (pick / open): a plate with
# a dense ring of holes + a fillet. Picking and opening both re-run the kernel over this whole
# tree, which is exactly what made clicking a surface / opening a doc slow.
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


def main(argv):
    ap = argparse.ArgumentParser(description="Sampling-profile the AI-CAD modeling kernel.")
    ap.add_argument("--workload", default="all")
    ap.add_argument("--seconds", type=float, default=5.0, help="how long to sample")
    ap.add_argument("--hz", type=int, default=1000, help="sample rate (samples/sec)")
    ap.add_argument("--top", type=int, default=30, help="rows in the hotspot tables")
    ap.add_argument("--out", default="prof", help="output path prefix")
    ap.add_argument("--list", action="store_true", help="list workloads and exit")
    args = ap.parse_args(argv[1:])

    wls = _workloads()
    all_names = sorted(list(wls) + list(INTERACTIVE))
    if args.list:
        print("workloads:", ", ".join(all_names))
        print("  (pick/open are INTERACTIVE: build once, then sample the per-click / per-open RPC)")
        return 0
    if args.workload not in all_names:
        print(f"unknown workload '{args.workload}'. choices: {', '.join(all_names)}")
        return 2
    interactive = args.workload in INTERACTIVE
    ops = _heavy_build() if interactive else wls[args.workload]

    if not cb.available():
        print("ERROR: libaicad.dll not found/loadable. Build + stage the core first "
              "(see docs/PROFILER.md). Set AI_CAD_DIST to override the dist dir.")
        return 1

    print(f"AI-CAD modeling profiler  ·  workload={args.workload}  hz={args.hz}  "
          f"seconds={args.seconds}")
    print(f"  dll: {cb.dist_dir() / 'libaicad.dll'}")

    # Sanity build once so we can report the feature status / catch a bad workload early.
    d0 = cb.call("doc_new", {"kind": "part", "name": "prof_probe"}).get("doc_id")
    r0 = cb.call("doc_apply_ops", {"doc_id": d0, "ops": ops})
    _print_status(r0)
    try:
        cb.call("doc_close", {"doc_id": d0})
    except cb.CoreError:
        pass

    # Per-workload SETUP runs BEFORE prof_start so it never pollutes the flame graph. For the
    # interactive workloads this also WARMS the kernel (one cold compile + OCCT lazy init) so the
    # graph attributes the STEADY-STATE per-click / per-open cost, not the one-time first build.
    pick_doc = None
    open_path = None
    if args.workload == "pick":
        pick_doc = cb.call("doc_new", {"kind": "part", "name": "prof"}).get("doc_id")
        cb.call("doc_apply_ops", {"doc_id": pick_doc, "ops": ops})
        for i in range(12):  # warm up: first pick compiles+caches; the rest are steady-state
            cb.call("doc_resolve_pick",
                    {"doc_id": pick_doc, "body_id": 1,
                     "kind": ("face", "edge")[i % 2], "topo_id": 1 + (i % 6)})
    elif args.workload == "open":
        import tempfile
        open_path = str(Path(tempfile.gettempdir()) / "aicad_prof_open.step+")
        db = cb.call("doc_new", {"kind": "part", "name": "prof_open"}).get("doc_id")
        cb.call("doc_apply_ops", {"doc_id": db, "ops": ops})
        cb.call("doc_save", {"doc_id": db, "path": open_path})
        try:
            cb.call("doc_close", {"doc_id": db})
        except cb.CoreError:
            pass
        cb.call("doc_close", {"doc_id": cb.call("doc_open", {"path": open_path})["doc_id"]})  # warm

    # Sample. Modeling workloads rebuild a fresh doc per iteration (so the kernel recompiles).
    # Interactive workloads hammer the per-click / per-open RPC on the pre-built doc.
    started = cb.call("prof_start", {"hz": args.hz, "mode": "modeling"})
    print(f"  profiler: {started}")
    t0 = time.time()
    iters = 0

    if args.workload == "pick":
        kinds = ("face", "edge")
        while time.time() - t0 < args.seconds:
            k = kinds[iters % 2]
            topo = 1 + (iters % 8)               # cycle a handful of sub-shapes
            try:
                cb.call("doc_resolve_pick",
                        {"doc_id": pick_doc, "body_id": 1, "kind": k, "topo_id": topo})
            except cb.CoreError:
                pass
            iters += 1
        try:
            cb.call("doc_close", {"doc_id": pick_doc})
        except cb.CoreError:
            pass
    elif args.workload == "open":
        while time.time() - t0 < args.seconds:
            try:
                r = cb.call("doc_open", {"path": open_path})
                od = r.get("doc_id")
                if od:
                    cb.call("doc_close", {"doc_id": od})
            except cb.CoreError:
                pass
            iters += 1
    else:
        while time.time() - t0 < args.seconds:
            d = cb.call("doc_new", {"kind": "part", "name": "prof"}).get("doc_id")
            cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
            try:
                cb.call("doc_close", {"doc_id": d})
            except cb.CoreError:
                pass
            iters += 1
    elapsed = time.time() - t0

    folded_path = f"{args.out}.folded"
    ss_path = f"{args.out}.speedscope.json"
    rep = cb.call("prof_stop", {
        "top": args.top,
        "write_folded": str(Path(folded_path).resolve()),
        "write_speedscope": str(Path(ss_path).resolve())})

    if not rep.get("ok"):
        print(f"\nNO REPORT: {rep.get('error')}")
        return 1

    s = rep["summary"]
    unit = {"pick": "picks", "open": "opens"}.get(args.workload, "builds")
    print(f"\n  {iters} {unit} in {elapsed:.1f}s ({iters/max(elapsed,1e-9):.0f}/s)  ·  "
          f"{s['samples']} samples  "
          f"({s['unique_stacks']} unique stacks, {s['unresolved_frames']} unresolved frames, "
          f"interval {s['interval_us']}us)")

    print("\nTIME BY MODULE (self-time — where the CPU actually is):")
    _fmt_table(rep["modules"],
               [("module", "MODULE", 22), ("self", "SAMPLES", 10), ("pct", "%", 8)])

    print("\nHOTTEST FRAMES (self-time):")
    _fmt_table(rep["flat_self"],
               [("frame", "FRAME", 64), ("self", "SAMPLES", 10), ("pct", "%", 8)])

    # Render the flame graph.
    svg_path = f"{args.out}.svg"
    try:
        svg = flamegraph.render_svg(rep["folded"],
                                    title=f"AI-CAD modeling · {args.workload}")
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg)
        wrote = rep.get("wrote", [])
        print(f"\nwrote:\n  {svg_path}   (flame graph — open in a browser)")
        for w in wrote:
            print(f"  {w}")
        print("  (drop the .folded or .speedscope.json into https://speedscope.app for a "
              "zoomable view)")
    except Exception as e:  # noqa: BLE001
        print(f"flame graph render failed: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
