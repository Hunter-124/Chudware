---
name: chudware-worker-segv-json-ub
description: Diagnose worker stdout closure and native crashes, including unsafe const JSON indexing.
---

# Worker crashes and JSON access

An `admin worker closed stdout` error is an EOF symptom, not proof of a segfault. Inspect child exit status, signal, stderr and any existing core dump before attributing a cause. A Python worker may load the native library through ctypes, so a native fault can terminate the worker. Do not attach to or interrupt an open user session.

For an isolated reproduction, use a private HOME, XDG directories and workspace, and an isolated engine (`CHUDWARE_ENGINE_DAEMON=0` when exercising the direct engine). Replay the actual request through the relevant tool path; bypassing normalization can exercise a different input. Keep private payloads out of public artifacts.

On a `const nlohmann::json`, indexing a missing key or out-of-range index with `operator[]` is undefined behavior and need not throw. Inspect neighboring const JSON reads for the same issue and use checked access consistent with the existing accessors in `core/sketch/sketch.cpp`. Check the resulting rejection or geometry too: eliminating a crash does not necessarily make the original request valid.

Inspect `core/hostd/child_supervisor.cpp` for stderr draining, child reaping, and signal reporting; avoid claiming work was lost without evidence. For comparisons use an isolated source export with its own build, never stash shared changes.

Verify: `scripts/testbox.py --suite platform,agent`
