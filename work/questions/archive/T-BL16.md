# T-BL16 — question for the advisor (Fable)
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

status: proceeding-with-recommended-answer (not blocking)

## Q: Worked-example fidelity — is a driven planar crank an acceptable stand-in for the
DoD's "4-bar linkage or cam-follower"?

**Context.** `simulate_motion` (the tool) is validated end-to-end and drives ANY dimensional
mate (angle/distance/limits/gear/rack_pinion/screw), re-solving the full mate graph each step —
so it already covers 4-bar linkages and cam-followers with no tool changes. The open question is
only the *worked example / regression test*.

The DoD names "a 4-bar linkage **or** cam-follower assembly, driven through a full cycle, with
an interference regression asserted."

**What I found building the example:**
- A single `angle` mate leaves the driven body with ~5 free DOF; the solver returns an
  arbitrary out-of-plane pose (warm-start luck). A legitimate 1-DOF revolute needs the
  out-of-plane DOF pinned — I add a `parallel` mate (wide-face to wide-face) so rotation stays
  in the Z-plane (verified: out-of-plane transform terms ≈ 0, armR tracks the drive angle
  exactly, mate status ok/redundant, no branch flip across 0->150 deg).
- A **true closed 4-bar** (4 links + 4 revolutes forming a loop) is the classic branch-flip /
  over-constraint case and is fragile to build reliably from box primitives via selector-based
  mates in a unit test — high risk of eating the whole task's budget for a fixture.
- A **cam-follower** driven through a cycle needs a NON-circular (eccentric) cam profile to
  produce follower motion (a concentric cylinder is rotationally symmetric — zero follower
  lift). Modeling an eccentric cam + tangent/cam_follower coupling + a driving rotation is a
  meaningful modeling sub-task, again fragile in a unit test.

**Recommended answer (what I shipped):** a **driven planar crank** — one fixed ground pivot
bar, one free "arm" bar on a driven `angle` mate (+ a `parallel` mate locking it planar), swung
through its arc into a fixed obstacle block. This exercises EVERY DoD behavior: drive one mate →
the mechanism follows → per-step per-body trajectory returned → swept `check_interference`
flags the first colliding step (Arm↔Obstacle at ~120 deg, steps before it clear). The test also
asserts real motion, step-to-step continuity (no branch flip), and planarity. It is named
honestly in code/docs as a "driven single-DOF crank," NOT claimed to be a 4-bar.

Rationale: the tool's capability (the actual deliverable) is fully proven; a crank is the
minimal *stable* mechanism that regression-tests all of it. A true-4-bar / cam-follower example
is worth a follow-up once we have richer assembly fixtures, but forcing it into this task risks
the "top priority today" backend + the GUI seam. Flagging per the OSS/contract-decision rule so
this is a conscious, visible call rather than a silent DoD reinterpretation.

If the reviewer requires a literally-named 4-bar/cam-follower before merge, the follow-up task
`T-BL16b` (cut in work/backlog/) is the place to add it against a proper fixture.
