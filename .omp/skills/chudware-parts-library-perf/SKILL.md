---
name: chudware-parts-library-perf
description: Diagnose Parts Library latency across indexing, search, supplier fetches, thumbnails, and rendering.
---

# chudware-parts-library-perf

Start with `services/parts/` and `host/src/ui/parts/`. Measure cold and warm bootstrap, local search, remote fetch, thumbnail processing, and progressive UI rendering separately; do not assume the bottleneck or reuse old timings. `services/parts/gui_rpc.py` exposes search and thumbnail endpoints.

Use unique private HOME/XDG and benchmark directories. Control cache state and network use when comparing measurements. If a baseline is needed, use a separate isolated worktree or source export, never stash the shared tree. Check that performance changes preserve search results and visible progressive behavior.

Verify: `scripts/testbox.py --suite parts`
