// 2D drawing projection — C++ port of engine/doc/drawing.py (W12-W15).
//
// Projects 3D solids to 2D views via OCCT's hidden-line-removal (HLRBRep_Algo /
// HLRBRep_HLRToShape), matching drawing.py exactly: same orientation->projector
// axes (gp_Ax2 with view_dir + screen_x), same VCompound/OutLine/Rg1Line visible
// set + HCompound/OutLineH hidden set, same GCPnts_TangentialDeflection discretizer
// (lin=0.05, ang=0.35), same analytic-arc mining, same scale+position mapping.
//
// The result data structure mirrors project_view's JSON: per-view visible[] +
// hidden[] polylines (each a list of [x,y] sheet-mm points), arcs[], and the bbox.
// OCCT-vs-build123d edge ENUMERATION ORDER will differ, so the parity harness gates
// on edge COUNT + total projected length + bbox + arc count/radii, NOT per-edge
// identity (see tools/cpp_drawing_parity.py).
//
// SECTION + DETAIL views are ported (project_section / project_detail). The stateful
// DrawingSession registry + the drawing_* RPC lifecycle (new/add_view/add_dim/...)
// is DEFERRED — this layer exposes the pure projection so the host's drawing_get
// geometry + export2d can run on the C++ core via a stateless diagnostic RPC.
#pragma once

#include <array>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>

namespace chudware::doc {

// HLR-project `shapes` for one named orientation (front/back/top/bottom/right/left/
// iso) into 2D sheet geometry. `scale` + `position` (sheet mm) map the projected
// model. `show_hidden` toggles the hidden-edge set. Returns the project_view JSON:
//   {view_id, scale, orientation, visible:[[ [x,y],... ],...], hidden:[...],
//    arcs:[{center:[x,y],radius,start_angle,end_angle,span,closed,layer}],
//    bbox:[minx,miny,maxx,maxy]|null, ms, error}
// A failure (bad orientation / OCCT throw) is CONTAINED: error is set + geometry
// empty (never throws across this boundary), mirroring the Python contract.
nlohmann::json project_view(const std::vector<TopoDS_Shape>& shapes,
                            const std::string& orientation, double scale,
                            std::array<double, 2> position, bool show_hidden,
                            const std::string& view_id);

// HLR-project the CUT model for a section view + the hatched section-face polygons.
// `plane` is {normal:[x,y,z], point:[x,y,z]} (explicit world plane). Returns the
// project_section JSON (visible/hidden/section_faces/bbox/ms/error). The 2D
// cut_line parent-frame form is DEFERRED (the explicit-plane form is the gate).
nlohmann::json project_section(const std::vector<TopoDS_Shape>& shapes,
                               const nlohmann::json& plane, double scale,
                               std::array<double, 2> position, bool show_hidden,
                               const std::string& view_id);

// Magnified detail crop of a parent view's already-projected geometry (project_detail).
// `parent_geom` is a prior project_view result; clips visible+hidden to the circle of
// `radius` about `center` (parent sheet mm), re-origins on the center, magnifies by
// scale/parent_scale, and translates to `position`. Returns the project_detail JSON.
nlohmann::json project_detail(const nlohmann::json& parent_geom,
                              std::array<double, 2> center, double radius,
                              double scale, std::array<double, 2> position,
                              const std::string& view_id);

}  // namespace chudware::doc
