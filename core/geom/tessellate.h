// OCCT tessellation -> AICM (C++ port of engine/doc/tessellate.py::tessellate_body).
// Meshes a TopoDS_Shape with BRepMesh_IncrementalMesh and emits an aicm::AicmBody:
// per-face contiguous triangle ranges, edge polylines, B-rep vertices. The
// resulting body is serialized by geom/aicm.{h,cpp}; the host's render/aicm_loader
// is the round-trip consumer. Algorithm + tolerances mirror the Python reference.
#pragma once

#include <string>

#include "geom/aicm.h"

class TopoDS_Shape;

namespace geom {

// Tessellate one OCCT shape into an AICM body.
//   lin_deflection / ang_deflection : BRepMesh tolerances (mm, radians) — defaults
//   match tessellate.py (0.05 / 0.35). metallic/roughness fill the C32-A trailer.
aicm::AicmBody tessellate_body(const TopoDS_Shape& shape, uint32_t body_id,
                               const std::string& name, const float color[3],
                               double lin_deflection = 0.05,
                               double ang_deflection = 0.35,
                               float metallic = 0.0f, float roughness = 0.5f);

// --- tessellation micro-profiling (opt-in; near-zero when unread) ---------------
// Splits tessellate_body's wall time into the BRepMesh meshing pass vs the
// triangle/edge extraction that builds the AicmBody, so the compile profiler can
// tell whether an incremental edit's tessellation cost is meshing (geometry truly
// changed) or extraction (re-walking unchanged faces — the reuse opportunity).
// Process-wide accumulators; reset before a timed region, read after. Cheap: two
// steady_clock reads per tessellate_body call.
struct TessTiming {
    double mesh_ms = 0.0;
    double extract_ms = 0.0;
    long bodies = 0;
    long faces = 0;
};
void tess_timing_reset();
TessTiming tess_timing_read();

}  // namespace geom
