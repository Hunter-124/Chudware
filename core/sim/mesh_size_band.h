// Shared element-sizing brain for every mesher backend.
//
// "Fineness 0.5" must mean the same element size whichever backend runs, and the
// host-memory governor must clamp both — otherwise switching mesher silently changes the
// physics (a coarser mesh reads a lower stress) or lets a 12 GB solve start on a laptop.
// So the sizing decision lives here, once, and both mesher.cpp (Netgen) and
// polymesh_backend.cpp consume it.
//
// This header names OCCT types, unlike the backend-free sim/mesher.h; it is internal to
// the mesher targets and is not part of the FEA solver seam.
#pragma once

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
    double min_len = 0.0;   // size-band floor, mm
    double max_len = 0.0;   // size-band ceiling, mm
    double diag = 0.0;      // bounding-box diagonal, mm
};

// fineness -> base element size off the bbox DIAGONAL, clamped by the part's smallest real
// dimension, the volume-aware interactivity budget and the host-memory governor, then a
// tight 2:1 min/max band. `error` set (ok=false) on degenerate geometry.
SizeBand resolve_size_band(const TopoDS_Shape& shape, const MeshOptions& options);

}  // namespace chudware::sim
