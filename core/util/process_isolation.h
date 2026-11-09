#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include <TopoDS_Shape.hxx>

namespace chudware::util {

enum class ShapeBuildFailure { none, construction, crash, timed_out, unavailable };

struct IsolatedShapeResult {
    bool ok = false;
    TopoDS_Shape shape;
    ShapeBuildFailure failure = ShapeBuildFailure::unavailable;
};

// Legacy blend callback isolation. Signals are crashes; null builds and caught
// exceptions are construction failures. Fork and transport failures are unavailable.
// A default-policy timeout retires this legacy mechanism and retries in process.
// Strict callback requests (retry_after_timeout=false) are unavailable while
// isolation is enabled: a callback cannot safely run after fork in a threaded host.
// With explicit opt-out, on Windows, or inside an already bounded task, the callback
// runs directly; its normal null result retains sidecar diagnostics for strict calls.
IsolatedShapeResult isolate_shape_build(const std::function<TopoDS_Shape()>& build,
                                        std::chrono::milliseconds timeout,
                                        std::string* sidecar = nullptr,
                                        bool retry_after_timeout = true);

// Configured on (CHUDWARE_BLEND_ISOLATION != "0").
bool shape_isolation_enabled();

// Configured on AND not yet retired for this process. A default-policy fork timeout
// retires isolation and falls back in-process; callers needing to know whether the
// NEXT build will be isolated must ask this, not just `shape_isolation_enabled`.
bool shape_isolation_active();

// Strict fillets use a fresh executable, never a callback in a post-fork copy
// of a multithreaded host. Edge references are 1-based TopExp::MapShapes indices
// with the selected orientation; serialization never modifies the source shape.
struct ShapeWorkerEdge {
    std::uint32_t index = 0;
    std::uint32_t orientation = 0;
};
struct FilletWorkerParameters {
    double radius = 0.0;
    double radius2 = 0.0;
    bool variable = false;
    bool unequal_requested = false;
    bool recording = false;
    bool document_validation = false;
    unsigned runoff = 0;  // 0 none, else a FilletRunoffMode
    unsigned sweep = 0;   // 0 none, else a geom::SweepBlendScope
    std::chrono::steady_clock::time_point deadline;
};

// One immutable serialized input and verified worker are reused for all probes
// in one search. Linux executes the held verified inode; other POSIX platforms
// execute the verified canonical path with its descriptor held.
class ShapeBuildExecSession {
public:
    ShapeBuildExecSession(const TopoDS_Shape& input, const std::vector<ShapeWorkerEdge>& edges);
    ~ShapeBuildExecSession();
    ShapeBuildExecSession(const ShapeBuildExecSession&) = delete;
    ShapeBuildExecSession& operator=(const ShapeBuildExecSession&) = delete;

    IsolatedShapeResult run(const FilletWorkerParameters& parameters, std::string& sidecar);

private:
    std::string worker_path_;
    int worker_fd_ = -1;
    int input_fd_ = -1;
};

// Configured strict policy; unlike legacy blend isolation, a prior timeout never
// retires it. False only on unsupported platforms, explicit opt-out, or within
// an already bounded advisory child.
bool strict_shape_isolation_enabled();

// Called only by the dedicated executable after exec. The callback runs in that
// fresh process and may safely construct/validate shapes using ordinary C++.
using ShapeWorkerBuild = std::function<IsolatedShapeResult(
    const TopoDS_Shape&, const std::vector<ShapeWorkerEdge>&,
    const FilletWorkerParameters&, std::string&)>;
int shape_build_worker_main(int argc, char** argv, const ShapeWorkerBuild& build);

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
