#pragma once

#include <chrono>
#include <functional>

#include <TopoDS_Shape.hxx>

namespace chudware::util {

struct IsolatedShapeResult {
    bool ok = false;
    TopoDS_Shape shape;
};

// Run an OCCT shape-producing operation out of process on POSIX. The child serializes
// the result; crashes and timeouts are reported as ordinary failure. Elsewhere this
// executes in-process so callers remain portable.
IsolatedShapeResult isolate_shape_build(const std::function<TopoDS_Shape()>& build,
                                        std::chrono::milliseconds timeout);

// Configured on (CHUDWARE_BLEND_ISOLATION != "0").
bool shape_isolation_enabled();

// Configured on AND not yet retired for this process. A fork timeout is treated as
// "fork is unusable here" and permanently falls back in-process (see the .cpp), so
// callers that need to know whether the NEXT build will actually be isolated — a test
// injecting a fault, a diagnostic — must ask this, not just `shape_isolation_enabled`.
bool shape_isolation_active();

}  // namespace chudware::util
