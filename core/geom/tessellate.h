// OCCT tessellation -> AICM (contract: contracts/mesh_format.md). Ported from the
// Python tessellate_body now in services/sim/tessellate.py; the defaults here have
// since moved on (size-relative linear deflection, display-quality tiers below).
// Meshes a TopoDS_Shape with BRepMesh_IncrementalMesh and emits an aicm::AicmBody:
// per-face contiguous triangle ranges, edge polylines, B-rep vertices. The
// resulting body is serialized by geom/aicm.{h,cpp}; host/src/render/aicm_loader
// is the round-trip consumer.
#pragma once

#include <string>

#include "geom/aicm.h"

class TopoDS_Shape;
class gp_Trsf;

namespace geom {

// Size-relative default linear deflection:
// clamp(bbox_diag * 6e-4, 0.02 mm, 1.0 mm). A ~50 mm part (diag ~86.6 mm) lands
// on ~0.05 mm — the old fixed default — so small/medium parts keep their prior
// mesh density, while multi-metre parts no longer mesh at millions of triangles.
// Falls back to 0.05 mm when the shape has no measurable bbox.
double auto_linear_deflection(const TopoDS_Shape& shape);

// ---- display tessellation quality (viewport / render smoothness) --------------
// One knob for "how smooth does the DISPLAY mesh look", used by the document
// compile AICM build and every session tessellation path. Imported real B-rep
// (frame tubes, 508 mm wheels) rendered visibly faceted at the historical
// Draft numbers: a 2.6 m frame meshed at a 1.0 mm chord with ~0.35 rad angular
// deflection (~18 segments per full circle). Finer tiers scale BOTH terms down.
//
//   tier     | lin = clamp(bbox_diag * rel, floor, ceil) mm      | ang (rad)
//   ---------|--------------------------------------------------|----------
//   Draft    | rel 6.0e-4, floor 0.02,  ceil 1.0                | 0.35
//   Standard | rel 3.0e-4, floor 0.01,  ceil 0.5                | 0.20
//   Fine     | rel 1.5e-4, floor 0.005, ceil 0.25               | 0.10
//
// Fine is the default (see default_display_quality). tessellate_body still
// applies its kDisplayMaxAngularDeflection ceiling, so Draft's 0.35 rad is
// bounded to that clamp and Draft/Standard differ in chord tolerance only.
enum class DisplayQuality { Draft = 0, Standard = 1, Fine = 2 };

// BRepMesh tolerances for one quality tier applied to one shape (mm, radians).
struct DisplayDeflections {
    double lin = 0.0;
    double ang = 0.0;
};
DisplayDeflections display_deflections(DisplayQuality q, const TopoDS_Shape& shape);

// Wire names: "draft" | "standard" | "fine". parse returns false (leaving `out`
// untouched) for anything else, so an RPC can refuse instead of guessing.
const char* display_quality_name(DisplayQuality q);
bool display_quality_parse(const std::string& name, DisplayQuality& out);

// Process default for a new document: `CHUDWARE_DISPLAY_QUALITY` (draft|standard|
// fine) when set to a valid name, else Fine. Read once and cached, so headless
// renders and the GUI in the same process agree.
DisplayQuality default_display_quality();

// Populate the same per-face triangulations as tessellate_body, without extracting
// or allocating an AICM packet. Component compilers retain these exact meshes for
// the final assembly display pass rather than serializing discarded part packets.
DisplayDeflections mesh_body(const TopoDS_Shape& shape, double lin_deflection = 0.0,
                              double ang_deflection = 0.35);

// Tessellate one OCCT shape into an AICM body.
//   lin_deflection / ang_deflection : BRepMesh tolerances (mm, radians).
//   lin_deflection <= 0 (the default) selects the size-relative auto value
//   (auto_linear_deflection above); an explicit positive value wins unchanged.
//   ang_deflection default 0.35 rad (as services/sim/tessellate.py). metallic/roughness
//   fill the C32-A trailer.
aicm::AicmBody tessellate_body(const TopoDS_Shape& shape, uint32_t body_id,
                               const std::string& name, const float color[3],
                               double lin_deflection = 0.0,
                               double ang_deflection = 0.35,
                               float metallic = 0.0f, float roughness = 0.5f);

// Rigidly transform an already-tessellated body IN PLACE: positions, normals
// (rotation only), edge polylines and B-rep vertex records. Lets the assembly
// path tessellate each component ONCE in its local frame, cache the AicmBody,
// and emit per-instance world meshes with a cheap vertex walk instead of
// re-running BRepMesh per open/re-solve. Rigid transforms only (no scale/mirror
// — callers keep the tessellate-the-world-shape path for those).
void transform_body_rigid(aicm::AicmBody& body, const gp_Trsf& t);

// --- tessellation micro-profiling (opt-in; near-zero when unread) ---------------
// Splits tessellate_body's wall time into the BRepMesh meshing pass vs the
// triangle/edge extraction that builds the AicmBody, so the compile profiler can
// tell whether an incremental edit's tessellation cost is meshing (geometry truly
// changed) or extraction (re-walking unchanged faces — the reuse opportunity).
// Per-compiling-thread accumulators; reset before the document's sequential
// tessellation phase, read after it.
struct TessTiming {
    double mesh_ms = 0.0;
    double extract_ms = 0.0;
    long bodies = 0;
    long faces = 0;
};
void tess_timing_reset();
TessTiming tess_timing_read();

}  // namespace geom
