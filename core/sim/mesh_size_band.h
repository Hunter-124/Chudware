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
    // ── What the coarsening decision was made ON ──────────────────────────
    // `predicted_elements` is the band model's count at `geometry_base` — the
    // size actually requested — and `element_limit` is what the memory
    // governor allows at this order. `predict_exponent` is 2 when the
    // surface term dominated the prediction and 3 when the volume term did,
    // i.e. the power the count is assumed to follow in h.
    //
    // They are published because the model is only a model: measured against
    // polymesh on the ME 490 caliper-bracket isolate it read 397k / 706k /
    // 1313k tet elements at h = 4.0 / 3.0 / 2.2 mm where the fill delivered
    // 346k / 271k / 244k — an error of +15 %, +160 % and +440 %, i.e. NOT
    // even monotone in h, which is how a coarser leg of a ladder ended up
    // demanding more memory (25.1 GB at h = 4.0 against 17.5 GB at h = 3.0).
    // So mesh_loaded_model re-anchors the decision on the count a real fill
    // MEASURED whenever the two disagree by more than 30 %.
    double predicted_elements = 0.0;
    double predict_exponent = 0.0;
    uint64_t element_limit = 0;
    // The memory governor (not interactivity) is what moved `base` off
    // `geometry_base`. Only then is a measured re-anchor worth a re-fill.
    bool memory_coarsened = false;
};

// fineness -> base element size off the bbox DIAGONAL, clamped by the part's smallest real
// dimension, the volume-aware interactivity budget and the host-memory governor, then a
// tight 2:1 min/max band. `error` set (ok=false) on degenerate geometry.
SizeBand resolve_size_band(const TopoDS_Shape& shape, const MeshOptions& options);

}  // namespace chudware::sim
