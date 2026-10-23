# Interference pre-op context pack (aicad.trigger/1)

**When this fires:** the model is about to place, assemble, mate, or move a body.

**Key facts:**
- Run `check_interference` after placing bodies. Any `interference > 0 mm³` is a
  **blocker** — the advisory will name the colliding bodies, overlap volume, MTS axis,
  and penetration depth.
- The advisor derives the minimum-translation-separation axis from the kernel AABB:
  the axis with the smallest overlap extent is reported (e.g. "+X, 2.1 mm") — move or
  shrink along that axis to resolve the clash with minimum geometry change.
- `clearances` in the kernel return shows gap distances to monitor tight-fit pairs.
- `check_interference` is a read-only call — safe to run before and after a placement.

**Common resolutions (advisor will specify):**
- Move the interfering body along the named axis by at least the stated depth.
- Reduce a boss height / pocket depth so the features clear.
- For bore/shaft pairs: run `fit_calc` to get the ISO 286 nominal bore diameter.
