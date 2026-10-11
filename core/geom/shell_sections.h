// Sectional shell: the fallback hollowing builder for tube-like bodies whose skin OCCT's
// offset algorithm refuses.
//
// WHY a second shell builder (not another rung of dress.cpp's MakeThickSolid ladder):
//   BRepOffset_MakeOffset rejects any edge between two B-spline faces whose faces meet at
//   a crease along part of it and tangentially along the rest (BRepOffset_MixedConnectivity).
//   A rectangle-to-circle loft has four such seams (90° at the rectangle, G1 at the circle)
//   and no join type, tolerance or face split gets past that check. This builder does not
//   offset faces at all: it slices the body across an axis and computes the exact eroded
//   cross-section at each station from point-to-skin distances, then lofts the cavity
//   through those sections and cuts it from the body.
//
// Scope: the opened faces are planar end caps perpendicular to one axis, every other face
// either spans one height band ("segment") of the body between vertex levels, or is a closed
// planar end cap at the far end (a cup bottom). Each station's slice must be one loop. The
// wall is inward only. Anything else returns a null shape with the reason in *why.
#pragma once

#include <string>
#include <vector>

#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>

namespace geom {

// Hollow `body` to wall `t` (inward), opening the planar `remove` faces. Bands whose side
// faces are planes/cylinders along the axis get exact inner planes/cylinders; freeform bands
// are lofted through exact eroded sections. The result is validated before it is returned:
// BRepCheck-valid, one closed watertight solid, and every sampled point of the new inner
// wall lies t from the kept skin (within 2% of t; the measured error sits at band junctions,
// ~1% on a loft duct, and is ~0.01% elsewhere). Null on any failure, with the reason in
// *why. Records only the accepted cavity cut in the open op-history scope.
TopoDS_Shape shell_by_sections(const TopoDS_Shape& body, double t,
                               const std::vector<TopoDS_Face>& remove, std::string* why);

}  // namespace geom
