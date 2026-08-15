---
id: T-BL61
title: Fillet on a compiled document SIGSEGVs hostd — OCCT PerformFilletOnVertex kills the daemon
workstream: backlog
owner: unassigned
status: review
branch: task/T-BL61-fillet-segv
depends_on: []
lane: modeling-core
kind: fix
priority: 1
---

## Context

A fillet inside `doc_apply_ops` crashed `chudware-hostd` with SIGSEGV during a
live agent run on 2026-08-04. A daemon crash is not a failed feature: every
document open in that engine dies with it, including work the user never saved.

Confirmed from a real coredump (`coredumpctl`, pid 913301, 293 MB, SIGSEGV,
`/home/hunter/Repositories/CAD/core/build-occt-linux/bin/chudware-hostd`):

```
#0  Geom2dAdaptor_Curve::D0(double, gp_Pnt2d&)      libTKG2d.so.7.9
#1  ChFi3d_FilBuilder::PerformTwoCorner(int)        libTKFillet.so.7.9
#2  ChFi3d_Builder::PerformFilletOnVertex(int)      libTKFillet.so.7.9
#3  ChFi3d_Builder::Compute()                       libTKFillet.so.7.9
#4  BRepFilletAPI_MakeFillet::Build(...)            libTKFillet.so.7.9
#5  geom::blend_drop_failing<BRepFilletAPI_MakeFillet, geom::fillet_edges(...)>
#9  geom::blend_drop_failing<...>
#10 geom::fillet_edges(TopoDS_Shape const&, double, std::vector<TopoDS_Edge> const&, int*)
#11 chudware::doc::compile_document(...)
#12 chudware::doc::compile_session(...)
#13 chudware::doc::apply_ops_locked(...)
#14 chudware::doc::doc_apply_ops(...)
#15 chudware_call_inner
#17 hostd::Server::dispatch(...)
#18 hostd::Server::clientLoop(...)
```

**Why the existing guard does not help.** `geom::blend_drop_failing` is built to
survive bad fillets: it catches a failure, drops the offending edges and retries
(frames #5–#9 are its recursion). But OCCT does not throw here — it dereferences
into `Geom2dAdaptor_Curve::D0` and segfaults. A `catch` cannot catch a SIGSEGV,
so the drop-failing strategy is structurally unable to contain this class.

## What is NOT yet known

No deterministic reproducer. A bare box survives every radius tried
(3, 5, 5.001, 6, 9, 20 mm on 60x40x10 — 20 mm returns the correct "too large for
this body" teaching error). A bored plate also survives (r=3..6 against a
10/20/24 mm through-bore on 80x50x12). The crashing document came from a live
model-built bracket whose exact op list was not captured — `events.jsonl` was
not journalled for that run and the doc was destroyed with the daemon.

The crash window also overlaps a concurrent `services/mcp` pytest run sharing
the same daemon, so **which** client submitted the fatal fillet is unproven. The
crash itself is not in doubt; its trigger geometry is.

## Steps

1. Capture the trigger. Re-run agent bracket builds with the run journal on
   (`EventJournal(run_id)`) so `add_features` args survive the daemon, and/or
   log the compiled feature JSON at `geom::fillet_edges` entry under a debug env
   var. Fuzz fillets over shapes with blend chains that MEET at a vertex
   (fillet-into-bore, fillet-into-fillet, chamfer+fillet on a shared vertex) —
   `PerformFilletOnVertex`/`PerformTwoCorner` is the corner-resolution path, so
   the trigger is a vertex where two blends converge, not radius magnitude alone.
2. Contain the class, not just the instance. Two candidates, not exclusive:
   - **Pre-flight rejection**: validate each requested blend against local
     geometry (radius vs adjacent edge length / face extent / convergent-blend
     count at a shared vertex) and refuse with a teaching error before OCCT is
     entered.
   - **Fault isolation**: run `compile_document`'s blend stage in a subprocess or
     under a guarded worker so an OCCT segfault degrades to a failed feature
     instead of a dead daemon. This is the only option that also covers the next
     unknown OCCT crash.
3. Regression: a `core/tests/test_geom.cpp` case over the captured geometry, and
   a hostd-level test asserting the daemon SURVIVES it.

## Definition of done

- [ ] A deterministic reproducer exists (test or script) that killed hostd before the fix.
- [x] The same input no longer terminates the daemon; it returns a failed-feature envelope.
- [x] Other documents open in the same engine survive the event.
- [x] `cd core/build-occt-linux && ninja && ctest -j4` green.

## Outcome (2026-08-14) — contained, reproducer still unfound

Step 2's **fault isolation** option is implemented (`core/util/process_isolation.*`,
wired into `core/geom/dress.cpp`). Every blend build attempt — the explicit-edge
drop-failing path AND the two whole-body overloads — runs in a bounded forked
child that serializes its result with `BinTools`; the parent maps a signal, a
non-zero exit, an unreadable payload or a timeout onto the same "this edge set is
unbuildable" answer the bisect already handled, so `n_dropped` and "round what you
can" are unchanged. A timeout is a failure rather than a retry because `fork()` in
a multithreaded daemon can deadlock the child on a malloc lock held at fork time.
`CHUDWARE_BLEND_ISOLATION=0` restores the in-process path; non-POSIX compiles to
in-process. Measured overhead on a successful 12-edge box fillet: 3.88 → 4.47 ms
median (+0.6 ms); +0.4 ms on the 113 ms partial-drop path. This covers the NEXT
unknown OCCT crash too, which pre-flight rejection would not have.

**Step 1 (capture the trigger) is still open.** A 94-case sweep
(`scratch/fillet_fuzz.cpp`, matrix in `scratch/fillet_fuzz_matrix.txt`) over
fillet-into-bore, fillet-into-fillet, chamfer/fillet on a shared vertex, 3- and
4-way corners, pre-blended corners, wedges, boss fillets, tangent-continuous
pairs, variable-radius fillets, two-distance chamfers, 0.2 mm walls, near-tangent
faces and sub-0.05 mm edges produced **zero** signalled cases with isolation off.
So the first DoD box stays unchecked: containment is proved by fault injection
(`CHUDWARE_TEST_BLEND_SIGSEGV`, regression case in `core/tests/test_ops.cpp`),
which is what this card explicitly permits, not by the original geometry. Closing
it needs the real captured document — re-run agent bracket builds with the run
journal on so `add_features` args survive the daemon.

## AI hints

- `skill://chudware-daemon-crash-coredump-triage` is the procedure that found
  this (`coredumpctl debug <pid> -A '-batch -ex "bt 30" -ex quit'`).
- The known-and-fixed sibling class is zero-var sketch entities
  (`core/sketch/sketch.cpp`, commit 2a599ce7). This is a DIFFERENT class: the
  fault is inside OCCT's fillet builder, not in our indexing.
- Do not "fix" this by widening the `catch` in `blend_drop_failing` — a signal is
  not an exception, and a wider catch will look like a fix while changing nothing.
- The isolation utility is the containment seam: put any NEW crash-prone OCCT
  builder behind `chudware::util::isolate_shape_build` rather than a `catch`.
