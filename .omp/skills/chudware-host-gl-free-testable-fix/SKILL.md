---
name: chudware-host-gl-free-testable-fix
description: Make host UI logic testable without GL while exercising real dispatch.
---

# chudware-host-gl-free-testable-fix

- Search for parallel C++ and Python implementations of the affected behavior and update each required path. Online agent tools dispatch through OMP host bindings; the Python resolver is relevant only to its offline path.
- Extract suitable pure logic into a GL-free translation unit and link the same implementation into the host and its tests; `host/src/ui/copilot/stream_rows.cpp` is an example. Avoid tests of a disconnected copy.
- Cover the actual dispatch path and regression case, including glyph handling or mirrored constants when relevant. Use isolated fixtures.

Verify: `scripts/testbox.py --suite agent`
