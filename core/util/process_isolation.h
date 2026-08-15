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

bool shape_isolation_enabled();

}  // namespace chudware::util
