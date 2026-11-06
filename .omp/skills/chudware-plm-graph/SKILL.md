---
name: chudware-plm-graph
description: Work on PLM graph layout, camera, overlays, and cache behavior without regressing interaction.
---

# chudware-plm-graph

Start with `host/src/ui/plm/graph_tab.cpp`, `graph_changes.*`, and related graph services; reuse existing UI kit and animation primitives. Preserve hierarchy packing and scope entry/exit position restoration; layout policy changes need a cache-version review. Keep camera updates coordinated through the graph's camera tick, and check cold and warm-cache fit as well as 2D/3D depth ordering and hit testing.

Graph changes are explicitly user-triggered: `graph_changes.h` documents why the expensive diff must not be polled from rendering or automatic refresh. Check scoped graphs, watch overlays and animation frame requests. Do not create placeholder stubs in another session's work; report a missing prerequisite instead. Use isolated fixtures.

Verify: `scripts/testbox.py --suite parts`
