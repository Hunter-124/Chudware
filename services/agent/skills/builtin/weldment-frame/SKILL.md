---
name: weldment-frame
description: Build structural tube, angle, channel, or beam frames from path sketches with stock profiles and explicit joint treatment instead of hand-placed boxes.
inject: auto
triggers:
  keywords: [weldment, welded frame, tube frame, structural frame, chassis frame, square tube frame, angle frame, channel frame, mitered frame, coped joint]
  tools: []
  phases: []
---

# Weldment Frame

## Sequence

1. Sketch the member centerlines as open line or arc segments; those segments become the members.
2. Add `weld_member` with a real stock profile, dimensions, justification, roll angle, and `miter`, `butt`, or `cope` joint intent.
3. Pass `refs.sketch` as the bare sketch id, not a region selector.
4. Resolve concrete member faces before optional `weld_gusset` or `weld_end_cap` features.
5. Render the joints and verify member count, section dimensions, overall envelope, and unintended overlaps.

## Flexibility

- Use only the members and reinforcement the load path needs; do not add gussets or caps by default.
- Prefer weldment features for stock frames, but use ordinary solid features for a one-off machined bracket or non-stock section.
