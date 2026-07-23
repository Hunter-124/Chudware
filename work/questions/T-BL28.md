# T-BL28 — questions for the Fable foreman / Hunter

Driver: **opus-vela** · Date: 2026-07-03 · Branch: `task/T-BL28-surf-edge-wells`

Two questions. Q1 is the DoD's explicit design call; Q2 is a scope call on the
GUI-verification DoD item that I want ratified before I sink host-side effort into it.
I am **proceeding on Q1 = A** (see below) because every signal converges on it and it is
backward-compatible/reversible; flag me off if that's wrong.

---

## Q1 — Fix direction for ruled / fill / boundary (T-G20 Q2, still unanswered)

**Recommendation: (A) teach the engine to accept picked body edges — augmented, not
replacing the sketch path.** Proceeding on this pending override.

Reasoning (beyond the T-G20 audit, which also recommended A):
- **Tier signal.** The foreman flagged this Opus-tier "gnarly C++/geometry." Option B is a
  pure form/selector-layer change (`pm_forms.cpp` well kinds + `property_manager.cpp`
  routing) — that's Sonnet-tier. The tier assignment itself implies engine work ⇒ A.
- **Zero GUI rewiring.** The three ribbon forms already emit body-edge (`K_EDGE`) selectors.
  Under A they need **no** change; the whole fix is one engine resolver. B would rewire 5
  wells + add sketch-routing + require a live re-verify of every rewired form — more work,
  worse UX.
- **Backward compatible.** I keep the existing sketch/region path intact (the MCP-driven
  refs the T-G20 audit proved work), and *add* a body-edge branch. Nothing that works today
  regresses.
- **Expected UX.** "Ruled/boundary surface between two body edges" is the SolidWorks-style
  interaction the form advertises.

Implementation shape: make `surf_sketch_wire()` polymorphic — if the ref's owner resolves
to a `SketchResult`, current sketch-wire behaviour; **else** resolve the picked edge via the
existing `resolve_edge_sel()` machinery and lift it into a single-edge wire. Known v1 limit:
edge resolution goes through `last_body_shape` (most-recent body), so cross-body edge picks
aren't supported yet — noted, not solved here.

## Q2 — Scope of the "live-pick GUI verification" DoD item

DoD item 3 reads: "drive each form to a successful surface, screenshot light+dark, LOOK at
every PNG." Under A the forms are unchanged, so the *only* thing a screenshot proves is that
the (already-correct) form renders — the behavioural change is entirely engine-side.

There is **no existing `CHUDWARE_SHOT` scenario that scripts a viewport edge-pick → OK →
built surface**; the scenarios (`scenarios.h`) are programmatic step-machines, so authoring
one for all three forms (ruled/fill/boundary) is real host-side work.

**Question:** is the merge bar for this task:
- (2a) engine regression test proving body-edge selectors → surface at the load-bearing
  layer (the exact selectors `make_selector` emits) **+** the existing static `pmform`
  screenshots — i.e. treat the engine test as the behavioural proof, since A doesn't touch
  the form; **or**
- (2b) a new scripted pick-to-surface scenario authored for all three forms?

**Recommendation: 2a.** The bug and the fix both live in the engine selector-resolution
layer; an engine test on the real GUI-emitted selectors is stronger proof than a screenshot
of an unchanged form, and it runs in CI. I'll build the engine test + cheatsheet refresh
regardless (decision-independent) and hold the new-scenario work pending this answer.

## Progress
- Filed 2026-07-03 by opus-vela. Proceeding on Q1=A engine fix + regression test + cheatsheet
  refresh (all decision-independent under A); holding new host scenario on Q2.
