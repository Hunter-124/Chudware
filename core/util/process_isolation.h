#pragma once

#include <chrono>
#include <functional>
#include <string>

#include <TopoDS_Shape.hxx>

namespace chudware::util {

struct IsolatedShapeResult {
    bool ok = false;
    TopoDS_Shape shape;
};

// Run an OCCT shape-producing operation out of process on POSIX. The child serializes
// the result; crashes and timeouts are reported as ordinary failure. Elsewhere this
// executes in-process so callers remain portable.
// `sidecar` (optional): a string the `build` lambda fills alongside its shape (e.g. the
// op's face provenance, geom/op_history.h). In-process the lambda writes it directly;
// across the fork the child ships it back with the shape and the parent restores it.
// It is cleared first and left empty whenever the build fails.
IsolatedShapeResult isolate_shape_build(const std::function<TopoDS_Shape()>& build,
                                        std::chrono::milliseconds timeout,
                                        std::string* sidecar = nullptr);

// Configured on (CHUDWARE_BLEND_ISOLATION != "0").
bool shape_isolation_enabled();

// Configured on AND not yet retired for this process. A fork timeout is treated as
// "fork is unusable here" and permanently falls back in-process (see the .cpp), so
// callers that need to know whether the NEXT build will actually be isolated — a test
// injecting a fault, a diagnostic — must ask this, not just `shape_isolation_enabled`.
bool shape_isolation_active();

enum class IsolatedTaskStatus { completed, timed_out, unavailable, failed };

struct IsolatedTaskResult {
    IsolatedTaskStatus status = IsolatedTaskStatus::unavailable;
    std::string payload;
};

// POSIX-only, fail-closed execution for advisory work. The absolute deadline covers
// the WHOLE callback, its serialization and pipe transfer, not each geometry probe.
// Timeout/error kills the owned process group and reaps the direct child before
// returning; there is NEVER an in-process retry. Windows returns unavailable.
// The child closes inherited IPC descriptors and runs nested shape builders directly:
// the outer process boundary already contains faults, and its one deadline still
// contains a slow build or an inherited-lock deadlock. No parent settings are changed.
// Scheduling, initial fork/OS cleanup and caller-side lock/transport waits are not
// hard real-time bounded. A completed payload is at most 16 MiB.
IsolatedTaskResult isolate_task(const std::function<std::string()>& run,
                               std::chrono::steady_clock::time_point deadline);

}  // namespace chudware::util
