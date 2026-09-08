// Chudware's user-facing element-sizing and memory-governor policy.
//
// Polyhedral-FEA owns geometry classification and volume filling; this seam
// translates the product fineness control into a bounded millimetre size band
// before meshing begins.
#pragma once

#include <cstdint>
#include <string>

#include <TopoDS_Shape.hxx>

#include "sim/mesher.h"

namespace chudware::sim {

struct SizeBand {
    bool ok = false;
    std::string error;
    std::string note;   // governor / interactivity coarsening message, if any
    double fineness = 0.5;
    double base = 0.0;      // target element size, mm
    // The size the geometry and the study's own controls asked for, BEFORE the
    // interactivity/memory element-count budget coarsened it (== base when
    // nothing was coarsened). The mesher re-meshes here when a coarsened fill
    // comes back in more connected components than the shape has solids: the
    // element-count prediction over-estimates by 6-9x on plate-like parts, and
    // a coarsening it justifies can fragment the mesh instead of cheapening it.
    double geometry_base = 0.0;
    double min_len = 0.0;   // size-band floor, mm
    double max_len = 0.0;   // size-band ceiling, mm
    double diag = 0.0;      // bounding-box diagonal, mm
    // Hard element/DOF ceilings handed to polymesh's pre-flight guard (its
    // "raise --max-elems/--max-dof" refusal). NOT a constant any more: it is
    // this machine's study memory budget divided by the MEASURED mesh-stage
    // cost per element, floored at polymesh's own default so no box meshes
    // less than it used to. See resolve_size_band for the measurements.
    uint64_t element_ceiling = 0;
    uint64_t dof_ceiling = 0;
    // Unmeasured bounding-box envelope at geometry_base, used ONLY to choose
    // the initial mesh-only probe. It is not a measured study cost; forecasts
    // fit this geometry's measured counts in mesh_loaded_model.
    double predicted_elements = 0.0;
    uint64_t element_limit = 0;
    // The memory envelope moved base to a mesh-only bootstrap. Probe back
    // toward geometry_base and decide delivery on actual counts, not forecasts.
    bool memory_coarsened = false;
};

// fineness -> base element size off the bbox DIAGONAL, clamped by the part's smallest real
// dimension, the volume-aware interactivity budget and the host-memory governor, then a
// tight 2:1 min/max band. `error` set (ok=false) on degenerate geometry.
SizeBand resolve_size_band(const TopoDS_Shape& shape, const MeshOptions& options);

}  // namespace chudware::sim
