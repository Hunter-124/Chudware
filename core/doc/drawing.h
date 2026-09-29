// 2D drawing projection — C++ port of engine/doc/drawing.py (W12-W15).
//
// Projects 3D solids to 2D views via OCCT's hidden-line-removal (HLRBRep_Algo, with
// HLRBRep_HLRToShape's extraction ported so every line keeps its source edge). Each
// orientation is the line of sight of contracts/doc_schema.md §8: the HLR frame's Z points
// back at the eye (-view_dir) with X = screen_x, so sheet +y = screen_x x view_dir. Same
// VCompound/OutLine/Rg1Line visible set + HCompound/OutLineH hidden set, same
// GCPnts_TangentialDeflection discretizer (lin=0.05, ang=0.35), same analytic-arc mining,
// same scale+position mapping.
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

// Source-model identity of one projected body's topology, in the numbering doc_query uses:
// per-body 1-based TopExp::MapShapes ids (doc_query's `topo`) and make_selector's stable sid
// (the handle doc_query / describe_part / query_geometry hand out).
struct SourceTopology {
    std::vector<std::string> edge_sids;        // [edge_id - 1]
    std::vector<std::string> face_sids;        // [face_id - 1]
    std::vector<std::vector<int>> edge_faces;  // [edge_id - 1] -> adjacent face ids
};

// One SourceTopology per shape, in the same order (shape i is source body i + 1).
std::vector<SourceTopology> index_source_topology(const std::vector<TopoDS_Shape>& shapes);

// HLR-project `shapes` for one named orientation (front/back/top/bottom/right/left/
// iso) into 2D sheet geometry. `scale` + `position` (sheet mm) map the projected
// model. `show_hidden` toggles the hidden-edge set. Returns the project_view JSON:
//   {view_id, scale, orientation, visible:[[ [x,y],... ],...], hidden:[...],
//    visible_src:[src|null,...], hidden_src:[...],
//    arcs:[{center:[x,y],radius,start_angle,end_angle,span,closed,layer,src}],
//    bbox:[minx,miny,maxx,maxy]|null, ms, error}
// PROVENANCE: visible_src[i] / hidden_src[i] name the source of visible[i] / hidden[i], and
// every arc carries its own `src`. It is read off OCCT's HLR data structure (each drawn piece
// is emitted from one HLRBRep_EdgeData, whose outliner edge is either the source edge itself,
// a piece of it, or a silhouette of a source face), never joined by geometry:
//   {"kind":"edge","body":b,"edge_id":e[,"sid","face_ids","face_sids"]}   a model edge
//   {"kind":"outline","body":b,"face_id":f[,"sid","face_ids","face_sids"]} a face silhouette
//   null  — geometry with no model edge (polygonal fallback, cut-plane edges, half-section line)
// body/edge_id/face_id are 1-based and equal doc_query's `body`/`topo`. The bracketed keys
// (sids from `topology`, one entry per shape) are present only when `topology` is given.
// A failure (bad orientation / OCCT throw) is CONTAINED: error is set + geometry
// empty (never throws across this boundary), mirroring the Python contract.
nlohmann::json project_view(const std::vector<TopoDS_Shape>& shapes,
                            const std::string& orientation, double scale,
                            std::array<double, 2> position, bool show_hidden,
                            const std::string& view_id,
                            const std::vector<SourceTopology>* topology = nullptr);

// HLR-project the CUT model for a section view + the hatched section-face polygons.
// `plane` is {normal:[x,y,z], point:[x,y,z]} (explicit world plane). Returns the
// project_section JSON (visible/hidden/visible_src/hidden_src/section_faces/bbox/ms/error);
// provenance follows project_view, routed through the section cut's boolean history so a
// cut piece of a source edge still names that edge. The 2D cut_line parent-frame form is
// DEFERRED (the explicit-plane form is the gate).
nlohmann::json project_section(const std::vector<TopoDS_Shape>& shapes,
                               const nlohmann::json& plane, double scale,
                               std::array<double, 2> position, bool show_hidden,
                               const std::string& view_id,
                               const std::vector<SourceTopology>* topology = nullptr);

// Magnified detail crop of a parent view's already-projected geometry (project_detail).
// `parent_geom` is a prior project_view result; clips visible+hidden to the circle of
// `radius` about `center` (parent sheet mm), re-origins on the center, magnifies by
// scale/parent_scale, and translates to `position`. Every clipped piece keeps its parent
// polyline's visible_src/hidden_src entry. Returns the project_detail JSON.
nlohmann::json project_detail(const nlohmann::json& parent_geom,
                              std::array<double, 2> center, double radius,
                              double scale, std::array<double, 2> position,
                              const std::string& view_id);

}  // namespace chudware::doc
