// hex_mesher.h — structured hexahedral (hex8) meshing for prismatic parts.
//
// Unstructured tet meshing (mesher.cpp / gmsh) never produces a symmetric mesh on a box
// — the Delaunay pattern is irregular by nature. For a box / rectangular prism we can
// instead lay down a perfectly regular nx*ny*nz grid of 8-node hexahedra, which IS
// symmetric and which the hex8 path in fea_static consumes (nodes_per_elem==8).
//
// This is OCCT-backed (reads the STEP, checks the solid is an axis-aligned box) and lives
// in the aicad_mesh target, which already links aicad_geom_occt for OCCT access.
#pragma once

#include <string>

#include "sim/mesher.h"  // MeshOut, MeshOptions

class TopoDS_Shape;  // OCCT fwd decl — shape overloads below skip the STEP re-parse (P4)

namespace aicad::sim {

// Cheap geometry classification shared by the structured-hex path and the Simple-mode
// auto-pick in mesher.cpp. Reads the STEP and reports whether the solid is a mappable
// axis-aligned rectangular prism, plus its bbox dimensions. is_box is true only when the
// part has exactly 6 faces AND its actual volume matches the axis-aligned bbox volume (so a
// rotated or curved solid is rejected). Lx/Ly/Lz/diag are the vertex-bbox extents (exact for
// a true box; for a non-box prefer a real Bnd_Box). error is set when is_box is false.
// Never throws.
struct BoxGeomInfo {
    bool is_box = false;
    double Lx = 0.0, Ly = 0.0, Lz = 0.0, diag = 0.0;
    std::string error;
};
BoxGeomInfo detect_box_geometry(const std::string& step_path);
// Shape overload (P4): identical classification on an ALREADY-PARSED shape, so a caller
// that owns the STEP parse (mesh_step reads the file once and threads the shape through
// detect/pick/hex/netgen) doesn't pay another STEPControl_Reader pass here.
BoxGeomInfo detect_box_geometry(const TopoDS_Shape& shape);

// Mesh an axis-aligned box/rectangular-prism STEP into a regular grid of hex8 elements.
// Sizing follows the same fineness model as the tet mesher (base from the bbox diagonal,
// or an explicit target_size_mm). Returns MeshOut with nodes_per_elem==8 and the quality
// metrics filled. If the solid is NOT a mappable axis-aligned box (volume != bbox volume,
// not exactly 6 faces, rotated, curved, …), returns ok=false with an actionable error
// telling the caller to use tetrahedral meshing instead. Never throws.
MeshOut mesh_box_hex(const std::string& step_path, const MeshOptions& options);
// Shape overload (P4): same mesh from an already-parsed shape (no STEP re-read).
MeshOut mesh_box_hex(const TopoDS_Shape& shape, const MeshOptions& options);

}  // namespace aicad::sim
