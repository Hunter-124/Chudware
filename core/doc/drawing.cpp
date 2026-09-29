// 2D drawing projection via OCCT HLR — see drawing.h. Port of engine/doc/drawing.py.

#include "doc/drawing.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>

#include <BRep_Builder.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepGProp.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepTools.hxx>
#include <GCPnts_TangentialDeflection.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <HLRAlgo_EdgeIterator.hxx>
#include <HLRAlgo_Projector.hxx>
#include <HLRBRep.hxx>
#include <HLRBRep_Algo.hxx>
#include <HLRBRep_Data.hxx>
#include <HLRBRep_EdgeData.hxx>
#include <HLRBRep_FaceData.hxx>
#include <HLRBRep_FaceIterator.hxx>
#include <HLRBRep_PolyAlgo.hxx>
#include <HLRBRep_PolyHLRToShape.hxx>
#include <HLRBRep_ShapeBounds.hxx>
#include <HLRTopoBRep_Data.hxx>
#include <HLRTopoBRep_OutLiner.hxx>
#include <Standard_Handle.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_DataMapOfShapeShape.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopoDS.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include "doc/picks.h"

using nlohmann::json;

namespace chudware::doc {

namespace {

#ifndef CHUDWARE_PI
#define CHUDWARE_PI 3.14159265358979323846
#endif

using Vec3 = std::array<double, 3>;

Vec3 normalize(Vec3 v) {
    double n = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    if (n < 1e-30) n = 1.0;
    return {v[0] / n, v[1] / n, v[2] / n};
}

Vec3 cross(const Vec3& a, const Vec3& b) {
    return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]};
}

double dot(const Vec3& a, const Vec3& b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

// orientation -> (view_dir, screen_x) per contracts/doc_schema.md §8 "Orientation -> projection
// direction": view_dir is the line of sight (eye -> model), screen_x the sheet +x axis. The
// sheet +y axis is screen_x x view_dir (hlr_frame), so front/back/left/right read +Z up, top
// reads +Y up and bottom -Y up.
bool orient_axes(const std::string& orientation, Vec3& view_dir, Vec3& screen_x) {
    if (orientation == "front") {
        view_dir = {0, 1, 0};
        screen_x = {1, 0, 0};
    } else if (orientation == "back") {
        view_dir = {0, -1, 0};
        screen_x = {-1, 0, 0};
    } else if (orientation == "top") {
        view_dir = {0, 0, -1};
        screen_x = {1, 0, 0};
    } else if (orientation == "bottom") {
        view_dir = {0, 0, 1};
        screen_x = {1, 0, 0};
    } else if (orientation == "right") {
        view_dir = {-1, 0, 0};
        screen_x = {0, 1, 0};
    } else if (orientation == "left") {
        view_dir = {1, 0, 0};
        screen_x = {0, -1, 0};
    } else if (orientation == "iso") {
        // Eye at (1,1,1) -> view dir = -(1,1,1) normalized; screen_x = dir x up, the
        // horizontal that leaves world +Z pointing up the sheet.
        Vec3 d = normalize({-1, -1, -1});
        Vec3 up = {0, 0, 1};
        Vec3 sx = normalize(cross(d, up));
        view_dir = d;
        screen_x = sx;
    } else {
        return false;
    }
    return true;
}

// The HLR frame for a line of sight. HLRAlgo_Projector puts the eye on the +Z side of its
// frame and projects onto (X, Z x X), so Z must point back at the eye (-view_dir): the drawn
// sheet +y is then screen_x x view_dir, and what is visible is what faces the viewer.
gp_Ax2 hlr_frame(const Vec3& view_dir, const Vec3& screen_x) {
    return gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(-view_dir[0], -view_dir[1], -view_dir[2]),
                  gp_Dir(screen_x[0], screen_x[1], screen_x[2]));
}

// Discretize one projected (2D, z~0) edge into a list of [x,y] sheet points.
// Mirrors _edge_to_polyline: lines stay 2-point, curves use GCPnts_TangentialDeflection.
std::vector<std::array<double, 2>> edge_to_polyline(const TopoDS_Edge& edge,
                                                    double lin_defl, double ang_defl) {
    std::vector<std::array<double, 2>> pts;
    BRepAdaptor_Curve ad(edge);
    if (ad.GetType() == GeomAbs_Line) {
        gp_Pnt a = ad.Value(ad.FirstParameter());
        gp_Pnt b = ad.Value(ad.LastParameter());
        pts.push_back({a.X(), a.Y()});
        pts.push_back({b.X(), b.Y()});
    } else {
        try {
            GCPnts_TangentialDeflection disc(ad, lin_defl, ang_defl);
            for (int i = 1; i <= disc.NbPoints(); ++i) {
                gp_Pnt p = disc.Value(i);
                pts.push_back({p.X(), p.Y()});
            }
        } catch (...) {
            gp_Pnt a = ad.Value(ad.FirstParameter());
            gp_Pnt b = ad.Value(ad.LastParameter());
            pts.push_back({a.X(), a.Y()});
            pts.push_back({b.X(), b.Y()});
        }
    }
    return pts;
}

// Every edge in an HLR result compound -> scaled, positioned 2D polylines.
// Mirrors _compound_polylines. A null/empty compound yields none.
void compound_polylines(const TopoDS_Shape& compound, double scale,
                        std::array<double, 2> position, double lin_defl,
                        double ang_defl,
                        std::vector<std::vector<std::array<double, 2>>>& out) {
    if (compound.IsNull()) return;
    TopTools_IndexedMapOfShape emap;
    TopExp::MapShapes(compound, TopAbs_EDGE, emap);
    double px = position[0], py = position[1];
    for (int i = 1; i <= emap.Extent(); ++i) {
        TopoDS_Edge edge = TopoDS::Edge(emap.FindKey(i));
        double s = scale > 1e-6 ? scale : 1e-6;
        auto raw = edge_to_polyline(edge, lin_defl / s, ang_defl);
        if (raw.size() < 2) continue;
        std::vector<std::array<double, 2>> pl;
        pl.reserve(raw.size());
        for (const auto& p : raw)
            pl.push_back({p[0] * scale + px, p[1] * scale + py});
        out.push_back(std::move(pl));
    }
}

// A drawn circular edge -> an analytic arc entity in sheet mm (mirrors _compound_arcs per
// edge). False for any other curve type.
bool edge_arc(const TopoDS_Edge& edge, double scale, std::array<double, 2> position,
              const std::string& layer, json& arc) {
    double r, fp, lp, cx0, cy0;
    try {
        BRepAdaptor_Curve ad(edge);
        if (ad.GetType() != GeomAbs_Circle) return false;
        gp_Circ circ = ad.Circle();
        gp_Pnt loc = circ.Location();
        r = circ.Radius();
        fp = ad.FirstParameter();
        lp = ad.LastParameter();
        cx0 = loc.X();
        cy0 = loc.Y();
    } catch (...) {
        return false;
    }
    double span = lp - fp;
    arc = json{
        {"center", json::array({cx0 * scale + position[0], cy0 * scale + position[1]})},
        {"radius", r * scale},
        {"start_angle", fp},
        {"end_angle", lp},
        {"span", span},
        {"closed", std::fabs(std::fabs(span) - 2.0 * CHUDWARE_PI) < 1e-6},
        {"layer", layer},
    };
    return true;
}

// One edge an HLR extraction pass drew, with the HLRBRep_Data edge index it was drawn from.
struct HlrPiece {
    TopoDS_Edge edge;
    int ie = 0;
};

// Port of HLRBRep_HLRToShape::InternalCompound(typ, visible, <whole data structure>, 2D)
// with DrawFace/DrawEdge inlined (OCCT 7.9 HLRBRep_HLRToShape.cxx). HLRToShape builds the
// same edges in the same order but throws away the data-edge index each one came from, and
// that index is the only route from a drawn line back to the model edge. typ: 5 sharp
// (VCompound/HCompound), 2 outline (OutLine*Compound), 3 smooth G1 (Rg1Line*Compound).
void hlr_pieces(const Handle(HLRBRep_Data)& ds, int typ, bool visible,
                std::vector<HlrPiece>& out) {
    if (ds.IsNull()) return;
    ds->Projector().Scaled(Standard_True);
    const int ne = ds->NbEdges();
    const int nf = ds->NbFaces();
    for (int ie = 1; ie <= ne; ++ie) {
        HLRBRep_EdgeData& ed = ds->EDataArray().ChangeValue(ie);
        if (ed.Selected() && !ed.Vertical()) {
            ed.Used(Standard_False);
            ed.HideCount(0);
        } else {
            ed.Used(Standard_True);
        }
    }
    auto draw = [&](HLRBRep_EdgeData& ed, int ie, bool in_face) {
        bool todraw = in_face;
        if (!in_face) {
            if (typ == 3) todraw = ed.Rg1Line() && !ed.RgNLine();
            else if (typ == 4) todraw = ed.RgNLine();
            else todraw = !ed.Rg1Line();
        }
        if (!todraw) return;
        Standard_Real sta, end;
        Standard_ShortReal tolsta, tolend;
        HLRAlgo_EdgeIterator it;
        if (visible) {
            for (it.InitVisible(ed.Status()); it.MoreVisible(); it.NextVisible()) {
                it.Visible(sta, tolsta, end, tolend);
                TopoDS_Edge e = HLRBRep::MakeEdge(ed.Geometry(), sta, end);
                if (!e.IsNull()) out.push_back({e, ie});
            }
        } else {
            for (it.InitHidden(ed.Status()); it.MoreHidden(); it.NextHidden()) {
                it.Hidden(sta, tolsta, end, tolend);
                TopoDS_Edge e = HLRBRep::MakeEdge(ed.Geometry(), sta, end);
                if (!e.IsNull()) out.push_back({e, ie});
            }
        }
    };
    for (int iface = 1; iface <= nf; ++iface) {
        HLRBRep_FaceIterator itf;
        for (itf.InitEdge(ds->FDataArray().ChangeValue(iface)); itf.MoreEdge(); itf.NextEdge()) {
            const int ie = itf.Edge();
            HLRBRep_EdgeData& edf = ds->EDataArray().ChangeValue(ie);
            if (edf.Used()) continue;
            bool todraw;
            if (typ == 1) todraw = itf.IsoLine();
            else if (typ == 2) todraw = itf.Internal();
            else if (typ == 3) todraw = edf.Rg1Line() && !edf.RgNLine() && !itf.OutLine();
            else if (typ == 4) todraw = edf.RgNLine() && !itf.OutLine();
            else todraw = !(itf.IsoLine() || itf.Internal() || (edf.Rg1Line() && !itf.OutLine()));
            if (todraw) {
                draw(edf, ie, true);
                edf.Used(Standard_True);
            } else if ((typ > 4 || typ == 2) && edf.Rg1Line() && !itf.OutLine()) {
                // A smooth edge gets a second chance through its other face.
                const int hc = edf.HideCount();
                if (hc > 0) edf.Used(Standard_True);
                else edf.HideCount(hc + 1);
            } else {
                edf.Used(Standard_True);
            }
        }
    }
    if (typ >= 3)
        for (int ie = 1; ie <= ne; ++ie) {
            HLRBRep_EdgeData& ed = ds->EDataArray().ChangeValue(ie);
            if (ed.Used()) continue;
            draw(ed, ie, false);
            ed.Used(Standard_True);
        }
    ds->Projector().Scaled(Standard_False);
}

// Where one projected body came from: the SOURCE body's sub-shape maps (1-based ids, the
// numbering doc_query stamps as `topo`) and, for a section's cut body, the boolean history
// from each rebuilt cut sub-shape to the source sub-shape it replaces. Sub-shapes the cut did
// not touch keep their identity and are found in the maps directly.
struct BodyTrace {
    int body = 0;  // 1-based source body ordinal (doc_query `body`)
    TopTools_IndexedMapOfShape edges, faces;
    TopTools_DataMapOfShapeShape origin;
};

void fill_trace(BodyTrace& tr, const TopoDS_Shape& source, int body) {
    tr.body = body;
    if (source.IsNull()) return;
    TopExp::MapShapes(source, TopAbs_EDGE, tr.edges);
    TopExp::MapShapes(source, TopAbs_FACE, tr.faces);
}

int traced_id(const TopoDS_Shape& s, const TopTools_IndexedMapOfShape& ids, const BodyTrace& tr) {
    int id = ids.FindIndex(s);
    if (id == 0 && tr.origin.IsBound(s)) id = ids.FindIndex(tr.origin.Find(s));
    return id;
}

json sid_at(const std::vector<std::string>& sids, int id) {
    if (id < 1 || id > static_cast<int>(sids.size()) || sids[id - 1].empty()) return json(nullptr);
    return json(sids[id - 1]);
}

// The provenance record documented on project_view (drawing.h).
json source_ref(bool is_edge, const BodyTrace& tr, int id, const SourceTopology* topo) {
    json src = {{"kind", is_edge ? "edge" : "outline"}, {"body", tr.body},
                {is_edge ? "edge_id" : "face_id", id}};
    if (!topo) return src;
    src["sid"] = sid_at(is_edge ? topo->edge_sids : topo->face_sids, id);
    json face_ids = json::array();
    if (!is_edge)
        face_ids.push_back(id);
    else if (id <= static_cast<int>(topo->edge_faces.size()))
        for (int f : topo->edge_faces[id - 1]) face_ids.push_back(f);
    json face_sids = json::array();
    for (const json& f : face_ids) face_sids.push_back(sid_at(topo->face_sids, f.get<int>()));
    src["face_ids"] = std::move(face_ids);
    src["face_sids"] = std::move(face_sids);
    return src;
}

// Source of one HLR outliner edge. The outliner keeps every source edge it did not split (same
// TShape) and records the rest in its NewSOldS history: a split piece -> its source edge, a
// silhouette / internal outline -> the source face it lies on. Null when neither leads to the
// source (an edge the section cut generated on its plane).
json trace_edge(const TopoDS_Shape& e, const HLRTopoBRep_Data& tds, const BodyTrace& tr,
                const SourceTopology* topo) {
    if (const int id = traced_id(e, tr.edges, tr)) return source_ref(true, tr, id, topo);
    const TopoDS_Shape old = tds.NewSOldS(e);
    if (old.IsNull() || old.IsSame(e)) return json(nullptr);
    if (old.ShapeType() == TopAbs_EDGE) {
        if (const int id = traced_id(old, tr.edges, tr)) return source_ref(true, tr, id, topo);
    } else if (old.ShapeType() == TopAbs_FACE) {
        if (const int id = traced_id(old, tr.faces, tr)) return source_ref(false, tr, id, topo);
    }
    return json(nullptr);
}

struct HlrLayers {
    std::vector<std::vector<std::array<double, 2>>> visible, hidden;
    json visible_src = json::array();
    json hidden_src = json::array();
    json arcs = json::array();
};

// Exact HLR of `shapes` (traces[i] routes shape i back to its source body) into sheet-mm
// polylines + analytic arcs, each carrying its source. Draws the sets project_view has always
// drawn: visible = sharp + outline + smooth-G1, hidden = sharp + outline; arcs from the sharp
// and outline sets of both layers.
void hlr_project(const std::vector<TopoDS_Shape>& shapes, const std::vector<BodyTrace>& traces,
                 const std::vector<SourceTopology>* topology, const gp_Ax2& ax, double scale,
                 std::array<double, 2> position, double lin_defl, double ang_defl,
                 bool show_hidden, HlrLayers& out) {
    HLRAlgo_Projector projector(ax);
    Handle(HLRBRep_Algo) algo = new HLRBRep_Algo();
    for (const auto& sh : shapes) algo->Add(sh);
    algo->Projector(projector);
    algo->Update();
    algo->Hide();
    Handle(HLRBRep_Data) ds = algo->DataStructure();
    if (ds.IsNull()) return;

    std::vector<json> src_of(static_cast<std::size_t>(ds->NbEdges()) + 1, json(nullptr));
    const int nshapes = std::min(algo->NbShapes(), static_cast<int>(traces.size()));
    for (int i = 1; i <= nshapes; ++i) {
        int v1, v2, e1, e2, f1, f2;
        algo->ShapeBounds(i).Bounds(v1, v2, e1, e2, f1, f2);
        const Handle(HLRTopoBRep_OutLiner)& outliner = algo->ShapeBounds(i).Shape();
        if (e2 < e1 || outliner.IsNull() || outliner->OutLinedShape().IsNull()) continue;
        // HLRBRep_ShapeToHLR::Load numbers a shape's data edges in exactly this map order,
        // offset by the shape's first edge index.
        TopTools_IndexedMapOfShape local;
        TopExp::MapShapes(outliner->OutLinedShape(), TopAbs_EDGE, local);
        const SourceTopology* topo =
            topology && i <= static_cast<int>(topology->size()) ? &(*topology)[i - 1] : nullptr;
        for (int k = 1; k <= local.Extent() && e1 + k - 1 <= e2; ++k)
            src_of[static_cast<std::size_t>(e1 + k - 1)] =
                trace_edge(local(k), outliner->DataStructure(), traces[i - 1], topo);
    }

    const double s = scale > 1e-6 ? scale : 1e-6;
    auto emit = [&](int typ, bool visible, bool with_arcs) {
        std::vector<HlrPiece> pieces;
        hlr_pieces(ds, typ, visible, pieces);
        auto& pls = visible ? out.visible : out.hidden;
        json& srcs = visible ? out.visible_src : out.hidden_src;
        for (const HlrPiece& p : pieces) {
            const json& src = src_of[static_cast<std::size_t>(p.ie)];
            auto raw = edge_to_polyline(p.edge, lin_defl / s, ang_defl);
            if (raw.size() >= 2) {
                std::vector<std::array<double, 2>> pl;
                pl.reserve(raw.size());
                for (const auto& q : raw)
                    pl.push_back({q[0] * scale + position[0], q[1] * scale + position[1]});
                pls.push_back(std::move(pl));
                srcs.push_back(src);
            }
            json arc;
            if (with_arcs && edge_arc(p.edge, scale, position, visible ? "visible" : "hidden", arc)) {
                arc["src"] = src;
                out.arcs.push_back(std::move(arc));
            }
        }
    };
    emit(5, true, true);
    emit(2, true, true);
    emit(3, true, false);
    if (show_hidden) {
        emit(5, false, true);
        emit(2, false, true);
    }
}

// bbox over all polyline points (+ extra polygon lists). Null when empty.
json bbox_of(const std::vector<std::vector<std::array<double, 2>>>& a,
             const std::vector<std::vector<std::array<double, 2>>>& b) {
    bool any = false;
    double minx = 0, miny = 0, maxx = 0, maxy = 0;
    auto scan = [&](const std::vector<std::vector<std::array<double, 2>>>& pls) {
        for (const auto& pl : pls)
            for (const auto& p : pl) {
                if (!any) {
                    minx = maxx = p[0];
                    miny = maxy = p[1];
                    any = true;
                } else {
                    minx = std::min(minx, p[0]);
                    miny = std::min(miny, p[1]);
                    maxx = std::max(maxx, p[0]);
                    maxy = std::max(maxy, p[1]);
                }
            }
    };
    scan(a);
    scan(b);
    if (!any) return json(nullptr);
    return json::array({minx, miny, maxx, maxy});
}

// Expand `bbox` so it reaches the TRUE analytic extent of each CLOSED circle in `arcs`.
// Curved edges are tessellated (GCPnts_TangentialDeflection) for the polylines, and the
// tessellation chords fall slightly INSIDE the real curve, so a bbox taken from polyline
// points alone undershoots a round face's extremes by up to the deflection (~0.05mm). That
// loss reads back as e.g. an 80mm cylinder height auto-dimensioning to 79.90. The arcs[]
// entities carry the exact center/radius, so for each full circle we widen the bbox to
// cx±r / cy±r — giving clean, integer-accurate projected dimensions. (D6)
void expand_bbox_with_arcs(json& bbox, const json& arcs) {
    if (!arcs.is_array() || arcs.empty()) return;
    bool any = bbox.is_array() && bbox.size() == 4;
    double minx = any ? bbox[0].get<double>() : 0.0;
    double miny = any ? bbox[1].get<double>() : 0.0;
    double maxx = any ? bbox[2].get<double>() : 0.0;
    double maxy = any ? bbox[3].get<double>() : 0.0;
    auto hit = [&](double x, double y) {
        if (!any) { minx = maxx = x; miny = maxy = y; any = true; return; }
        minx = std::min(minx, x); miny = std::min(miny, y);
        maxx = std::max(maxx, x); maxy = std::max(maxy, y);
    };
    // Only CLOSED circles get the analytic extent: a full circle reaches cx±r and cy±r on
    // all four sides unconditionally (the cylinder-rim / hole case that drives D6). Partial
    // arcs are left to their tessellation — their start/end parameters are in the circle's
    // OWN local frame (not screen X), so we cannot reliably map a world-cardinal extreme to
    // the curve span without re-deriving the local axes, and their tiny undershoot rarely
    // sets the dimensioned extreme anyway. Conservative: never widen past the real geometry.
    for (const auto& arc : arcs) {
        if (!arc.is_object() || !arc.contains("center")) continue;
        if (!arc.value("closed", false)) continue;
        double cx = arc["center"][0].get<double>();
        double cy = arc["center"][1].get<double>();
        double r = arc.value("radius", 0.0);
        if (r <= 0.0) continue;
        hit(cx + r, cy);
        hit(cx - r, cy);
        hit(cx, cy + r);
        hit(cx, cy - r);
    }
    if (any) bbox = json::array({minx, miny, maxx, maxy});
}

json polylines_to_json(const std::vector<std::vector<std::array<double, 2>>>& pls) {
    json arr = json::array();
    for (const auto& pl : pls) {
        json j = json::array();
        for (const auto& p : pl) j.push_back(json::array({p[0], p[1]}));
        arr.push_back(std::move(j));
    }
    return arr;
}

// R4 — polygonal HLR fallback for purely-curved (revolved/lofted) solids. The exact
// HLRBRep_Algo can return ZERO edges for a body whose visible outline is entirely a
// surface silhouette (no sharp edges) — e.g. a sphere or a smooth solid of revolution —
// because its outline computation on exact curved geometry is fragile and occasionally
// yields an empty result. HLRBRep_PolyAlgo runs the same hidden-line removal on each
// shape's TRIANGULATION, which robustly produces the silhouette outline as polyline
// edges. We mesh each shape first (BRepMesh) then project. The outline comes back as line
// edges (polyline-approximated silhouette) with no analytic circles and no HLR data edge to
// trace — that is acceptable: the round silhouette still draws, and the common faceted
// path keeps using the exact algo (sharper, analytic arcs, provenance). Appends into
// `visible`/`hidden`; never throws (a failed poly pass just adds nothing).
void poly_hlr_fallback(const std::vector<TopoDS_Shape>& shapes, const gp_Ax2& ax, double scale,
                       std::array<double, 2> position, double lin_defl, double ang_defl,
                       bool show_hidden,
                       std::vector<std::vector<std::array<double, 2>>>& visible,
                       std::vector<std::vector<std::array<double, 2>>>& hidden) {
    try {
        HLRAlgo_Projector projector(ax);
        Handle(HLRBRep_PolyAlgo) algo = new HLRBRep_PolyAlgo();
        bool any = false;
        for (const auto& sh : shapes) {
            if (sh.IsNull()) continue;
            // A reasonably fine mesh so the silhouette reads smooth at common scales.
            try {
                BRepMesh_IncrementalMesh mesher(sh, 0.1, Standard_False, 0.35, Standard_True);
                mesher.Perform();
            } catch (...) { /* fall through — Load anyway, it may already be meshed */ }
            algo->Load(sh);
            any = true;
        }
        if (!any) return;
        algo->Projector(projector);
        algo->Update();
        HLRBRep_PolyHLRToShape hlr;
        hlr.Update(algo);
        compound_polylines(hlr.VCompound(), scale, position, lin_defl, ang_defl, visible);
        compound_polylines(hlr.OutLineVCompound(), scale, position, lin_defl, ang_defl, visible);
        if (show_hidden) {
            compound_polylines(hlr.HCompound(), scale, position, lin_defl, ang_defl, hidden);
            compound_polylines(hlr.OutLineHCompound(), scale, position, lin_defl, ang_defl, hidden);
        }
    } catch (...) { /* poly fallback failed -> leave visible/hidden as the exact pass left them */ }
}

// The polygonal fallback's lines have no HLR data edge to trace, so they carry null sources.
void pad_sources(HlrLayers& layers) {
    while (layers.visible_src.size() < layers.visible.size()) layers.visible_src.push_back(nullptr);
    while (layers.hidden_src.size() < layers.hidden.size()) layers.hidden_src.push_back(nullptr);
}

}  // namespace

std::vector<SourceTopology> index_source_topology(const std::vector<TopoDS_Shape>& shapes) {
    std::vector<SourceTopology> out(shapes.size());
    for (std::size_t b = 0; b < shapes.size(); ++b) {
        const TopoDS_Shape& body = shapes[b];
        if (body.IsNull()) continue;
        SourceTopology& t = out[b];
        // Contained per body: a body whose topology cannot be indexed still projects, and its
        // lines keep body + edge ids with null sids rather than failing the whole drawing.
        try {
            // An unfiltered query lists every face / edge in TopExp::MapShapes order, so the
            // i-th selector IS id i + 1 — and its sid is byte-for-byte the one doc_query hands
            // out for that entity (same resolver, same fingerprint).
            const QueryPicks faces = resolve_geom_query(body, json{{"of", "face"}});
            const QueryPicks edges = resolve_geom_query(body, json{{"of", "edge"}});
            for (const json& m : faces.selectors) t.face_sids.push_back(m.value("sid", std::string()));
            for (const json& m : edges.selectors) t.edge_sids.push_back(m.value("sid", std::string()));
            TopTools_IndexedMapOfShape fmap, emap;
            TopExp::MapShapes(body, TopAbs_FACE, fmap);
            TopExp::MapShapes(body, TopAbs_EDGE, emap);
            TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
            TopExp::MapShapesAndAncestors(body, TopAbs_EDGE, TopAbs_FACE, edge_faces);
            t.edge_faces.resize(static_cast<std::size_t>(emap.Extent()));
            for (int e = 1; e <= emap.Extent(); ++e) {
                const int k = edge_faces.FindIndex(emap(e));
                if (k == 0) continue;
                std::vector<int>& ids = t.edge_faces[static_cast<std::size_t>(e - 1)];
                for (TopTools_ListIteratorOfListOfShape it(edge_faces(k)); it.More(); it.Next()) {
                    const int f = fmap.FindIndex(it.Value());
                    if (f != 0 && std::find(ids.begin(), ids.end(), f) == ids.end()) ids.push_back(f);
                }
            }
        } catch (...) {
            t = SourceTopology{};
        }
    }
    return out;
}

json project_view(const std::vector<TopoDS_Shape>& shapes,
                  const std::string& orientation, double scale,
                  std::array<double, 2> position, bool show_hidden,
                  const std::string& view_id,
                  const std::vector<SourceTopology>* topology) {
    json base = {{"view_id", view_id}, {"scale", scale},
                 {"orientation", orientation}, {"visible", json::array()},
                 {"hidden", json::array()}, {"visible_src", json::array()},
                 {"hidden_src", json::array()}, {"arcs", json::array()},
                 {"bbox", json(nullptr)}, {"ms", 0.0}, {"error", json(nullptr)}};
    if (shapes.empty()) {
        base["error"] = "source has no bodies to project";
        return base;
    }
    try {
        Vec3 view_dir, screen_x;
        if (!orient_axes(orientation, view_dir, screen_x)) {
            base["error"] = "unknown orientation '" + orientation + "'";
            return base;
        }
        const gp_Ax2 ax = hlr_frame(view_dir, screen_x);

        std::vector<BodyTrace> traces(shapes.size());
        for (std::size_t i = 0; i < shapes.size(); ++i)
            fill_trace(traces[i], shapes[i], static_cast<int>(i) + 1);

        const double lin_defl = 0.05, ang_defl = 0.35;
        HlrLayers layers;
        hlr_project(shapes, traces, topology, ax, scale, position, lin_defl, ang_defl,
                    show_hidden, layers);
        std::vector<std::vector<std::array<double, 2>>>& visible = layers.visible;
        std::vector<std::vector<std::array<double, 2>>>& hidden = layers.hidden;

        // R4 — a purely-curved solid (revolve/loft, e.g. a sphere or smooth body of
        // revolution) can yield ZERO edges from the exact HLR (its outline is a surface
        // silhouette the exact algo failed to emit). Retry on the triangulated mesh, which
        // robustly produces the silhouette outline as polylines, so revolved parts project
        // into proper views instead of an empty drawing.
        if (visible.empty() && hidden.empty())
            poly_hlr_fallback(shapes, ax, scale, position, lin_defl, ang_defl, show_hidden,
                              visible, hidden);
        pad_sources(layers);

        base["visible"] = polylines_to_json(visible);
        base["hidden"] = polylines_to_json(hidden);
        base["visible_src"] = layers.visible_src;
        base["hidden_src"] = layers.hidden_src;
        const json& arcs = layers.arcs;
        base["arcs"] = arcs;
        json bbox = bbox_of(visible, hidden);
        // Use the exact arc extents (not the tessellated polyline points) so round-face
        // dimensions come out clean — e.g. a cylinder height reads 80, not 79.90 (D6).
        expand_bbox_with_arcs(bbox, arcs);
        base["bbox"] = bbox;
    } catch (const std::exception& exc) {
        base["error"] = std::string("exception: ") + exc.what();
    } catch (...) {
        base["error"] = "unknown HLR exception";
    }
    return base;
}

namespace {

// A half-space box: the region on the +normal side of the plane through `point`, extending
// `depth` along +normal (depth<=0 -> BIG, a true half-space). Returned as a TopoDS box tool.
TopoDS_Shape half_space_box(Vec3 normal, Vec3 point, double depth) {
    Vec3 n = normalize(normal);
    const double BIG = 100000.0;
    double dz = (depth > 0.0) ? depth : BIG;
    Vec3 seed = {1, 0, 0};
    if (std::fabs(dot(seed, n)) > 0.9) seed = {0, 1, 0};
    Vec3 u = normalize(cross(n, seed));
    Vec3 nu = cross(n, u);
    Vec3 origin = {point[0] - u[0] * BIG - nu[0] * BIG,
                   point[1] - u[1] * BIG - nu[1] * BIG,
                   point[2] - u[2] * BIG - nu[2] * BIG};
    gp_Ax2 ax(gp_Pnt(origin[0], origin[1], origin[2]),
              gp_Dir(n[0], n[1], n[2]), gp_Dir(u[0], u[1], u[2]));
    return BRepPrimAPI_MakeBox(ax, 2 * BIG, 2 * BIG, dz).Shape();
}

// Cut raw_shape by the +normal half-space through point. Mirrors _half_space_cut.
TopoDS_Shape half_space_cut(const TopoDS_Shape& raw, Vec3 normal, Vec3 point) {
    TopoDS_Shape tool = half_space_box(normal, point, 0.0);
    BRepAlgoAPI_Cut cut(raw, tool);
    cut.Build();
    if (!cut.IsDone()) throw std::runtime_error("section half-space cut failed");
    return cut.Shape();
}

// Section cut for a view: removes the +normal side of the plane (mode "full"), and for a
// HALF section removes only the half on the +half_axis side (intersection of the two
// half-spaces) so the other half stays an unsectioned external view. `depth` (>0) limits
// how deep behind the plane is removed. Returns the cut body; `origin` receives the cut's
// history (rebuilt edge/face -> the raw edge/face it replaces) so the section's lines keep
// their model identity.
TopoDS_Shape section_cut(const TopoDS_Shape& raw, Vec3 normal, Vec3 point, const std::string& mode,
                         double depth, const Vec3* half_axis, TopTools_DataMapOfShapeShape& origin) {
    TopoDS_Shape tool = half_space_box(normal, point, depth);
    if (mode == "half" && half_axis && std::sqrt(dot(*half_axis, *half_axis)) > 1e-9) {
        TopoDS_Shape side = half_space_box(*half_axis, point, 0.0);
        BRepAlgoAPI_Common common(tool, side);
        common.Build();
        if (common.IsDone()) tool = common.Shape();
    }
    BRepAlgoAPI_Cut cut(raw, tool);
    cut.Build();
    if (!cut.IsDone()) throw std::runtime_error("section cut failed");
    for (TopAbs_ShapeEnum kind : {TopAbs_EDGE, TopAbs_FACE}) {
        TopTools_IndexedMapOfShape subs;
        TopExp::MapShapes(raw, kind, subs);
        for (int i = 1; i <= subs.Extent(); ++i)
            for (TopTools_ListIteratorOfListOfShape it(cut.Modified(subs(i))); it.More(); it.Next())
                origin.Bind(it.Value(), subs(i));
    }
    return cut.Shape();
}

// Cut-plane face outer-wire polygons projected to (screen_x, screen_y). Mirrors
// _section_face_polygons.
void section_face_polygons(const TopoDS_Shape& cut_shape, Vec3 normal, Vec3 point,
                           Vec3 sx, Vec3 sy, double scale,
                           std::array<double, 2> position,
                           std::vector<std::vector<std::array<double, 2>>>& polys) {
    Vec3 n = normalize(normal);
    double px = position[0], py = position[1];
    TopTools_IndexedMapOfShape fmap;
    TopExp::MapShapes(cut_shape, TopAbs_FACE, fmap);
    for (int i = 1; i <= fmap.Extent(); ++i) {
        TopoDS_Face f = TopoDS::Face(fmap.FindKey(i));
        BRepAdaptor_Surface surf(f);
        if (surf.GetType() != GeomAbs_Plane) continue;
        GProp_GProps g;
        BRepGProp::SurfaceProperties(f, g);
        gp_Pnt c = g.CentreOfMass();
        Vec3 cvec = {c.X(), c.Y(), c.Z()};
        gp_Dir fn = surf.Plane().Axis().Direction();
        Vec3 fnv = {fn.X(), fn.Y(), fn.Z()};
        Vec3 cp = {cvec[0] - point[0], cvec[1] - point[1], cvec[2] - point[2]};
        if (std::fabs(dot(cp, n)) > 1e-6) continue;
        if (std::fabs(std::fabs(dot(fnv, n)) - 1.0) > 1e-6) continue;
        TopoDS_Wire ow;
        try {
            ow = BRepTools::OuterWire(f);
        } catch (...) {
            continue;
        }
        std::vector<std::array<double, 2>> poly;
        for (TopExp_Explorer exp(ow, TopAbs_EDGE); exp.More(); exp.Next()) {
            TopoDS_Edge e = TopoDS::Edge(exp.Current());
            BRepAdaptor_Curve ad(e);
            std::vector<gp_Pnt> pts3;
            if (ad.GetType() == GeomAbs_Line) {
                pts3.push_back(ad.Value(ad.FirstParameter()));
                pts3.push_back(ad.Value(ad.LastParameter()));
            } else {
                try {
                    double s = scale > 1e-6 ? scale : 1e-6;
                    GCPnts_TangentialDeflection disc(ad, 0.05 / s, 0.3);
                    for (int k = 1; k <= disc.NbPoints(); ++k) pts3.push_back(disc.Value(k));
                } catch (...) {
                    pts3.push_back(ad.Value(ad.FirstParameter()));
                    pts3.push_back(ad.Value(ad.LastParameter()));
                }
            }
            for (const gp_Pnt& q : pts3) {
                Vec3 v = {q.X(), q.Y(), q.Z()};
                poly.push_back({dot(v, sx) * scale + px, dot(v, sy) * scale + py});
            }
        }
        if (poly.size() >= 3) polys.push_back(std::move(poly));
    }
}

}  // namespace

json project_section(const std::vector<TopoDS_Shape>& shapes, const json& plane,
                     double scale, std::array<double, 2> position, bool show_hidden,
                     const std::string& view_id,
                     const std::vector<SourceTopology>* topology) {
    json base = {{"view_id", view_id}, {"scale", scale}, {"orientation", "section"},
                 {"visible", json::array()}, {"hidden", json::array()},
                 {"visible_src", json::array()}, {"hidden_src", json::array()},
                 {"section_faces", json::array()}, {"bbox", json(nullptr)},
                 {"ms", 0.0}, {"error", json(nullptr)}};
    if (shapes.empty()) {
        base["error"] = "source has no bodies to project";
        return base;
    }
    // Resolve the explicit world plane {normal, point} (cut_line form deferred).
    if (!plane.is_object() || !plane.contains("normal") || !plane.contains("point"))
        throw std::runtime_error("section needs a world 'plane' {normal, point}");
    Vec3 normal = {plane["normal"][0], plane["normal"][1], plane["normal"][2]};
    Vec3 point = {plane["point"][0], plane["point"][1], plane["point"][2]};
    if (std::sqrt(dot(normal, normal)) < 1e-9)
        throw std::runtime_error("section plane.normal must be nonzero");
    normal = normalize(normal);
    // Custom-section controls (optional): mode (full|half), depth clip, reverse side, and
    // the in-plane half_axis along which a half section keeps the unsectioned half.
    std::string mode = plane.value("mode", std::string("full"));
    double depth = plane.value("depth", 0.0);
    if (plane.value("reverse", false)) normal = {-normal[0], -normal[1], -normal[2]};
    Vec3 half_axis{0, 0, 0};
    bool have_half = false;
    if (plane.contains("half_axis") && plane["half_axis"].is_array() &&
        plane["half_axis"].size() == 3) {
        half_axis = {plane["half_axis"][0], plane["half_axis"][1], plane["half_axis"][2]};
        have_half = std::sqrt(dot(half_axis, half_axis)) > 1e-9;
    }
    // The cut removes the +normal half and the eye sits on that removed side, looking back
    // at the cut face: the line of sight is -normal.
    const Vec3 view_dir = {-normal[0], -normal[1], -normal[2]};
    // screen_x: a world axis most perpendicular to the plane normal.
    Vec3 seed = {0, 0, 1};
    if (std::fabs(dot(seed, normal)) > 0.9) seed = {1, 0, 0};
    Vec3 sx = normalize(cross(seed, normal));

    try {
        // Re-orthogonalize sx against the normal.
        double sl = dot(sx, normal);
        sx = {sx[0] - normal[0] * sl, sx[1] - normal[1] * sl, sx[2] - normal[2] * sl};
        if (std::sqrt(dot(sx, sx)) < 1e-9) {
            Vec3 sd = {0, 0, 1};
            if (std::fabs(dot(sd, normal)) > 0.9) sd = {1, 0, 0};
            sx = cross(sd, normal);
        }
        sx = normalize(sx);
        Vec3 sy = normalize(cross(sx, view_dir));

        std::vector<TopoDS_Shape> cut_shapes;
        std::vector<BodyTrace> traces(shapes.size());
        std::vector<std::vector<std::array<double, 2>>> section_faces;
        json section_bodies = json::array();
        std::size_t body_index = 0;
        for (const auto& raw : shapes) {
            BodyTrace& trace = traces[body_index];
            fill_trace(trace, raw, static_cast<int>(body_index) + 1);
            TopoDS_Shape cs;
            try {
                cs = section_cut(raw, normal, point, mode, depth,
                                 have_half ? &half_axis : nullptr, trace.origin);
            } catch (...) {
                trace.origin.Clear();
                cs = raw;
            }
            cut_shapes.push_back(cs);
            std::vector<std::vector<std::array<double, 2>>> body_faces;
            section_face_polygons(cs, normal, point, sx, sy, scale, position, body_faces);
            section_faces.insert(section_faces.end(), body_faces.begin(), body_faces.end());
            json body = {{"body_index", body_index},
                         {"faces", polylines_to_json(body_faces)}};
            if (plane.contains("hatch_styles") && plane["hatch_styles"].is_array() &&
                body_index < plane["hatch_styles"].size() &&
                plane["hatch_styles"][body_index].is_object())
                body["hatch_style"] = plane["hatch_styles"][body_index];
            else if (plane.contains("hatch_style") && plane["hatch_style"].is_object())
                body["hatch_style"] = plane["hatch_style"];
            else
                body["hatch_style"] = json{{"pattern", "single"},
                                            {"angle_deg", 45.0 + 90.0 * (body_index % 2)},
                                            {"spacing_mm", 3.0}};
            section_bodies.push_back(std::move(body));
            ++body_index;
        }

        const gp_Ax2 ax = hlr_frame(view_dir, sx);
        const double lin_defl = 0.05, ang_defl = 0.35;
        HlrLayers layers;
        hlr_project(cut_shapes, traces, topology, ax, scale, position, lin_defl, ang_defl,
                    show_hidden, layers);
        std::vector<std::vector<std::array<double, 2>>>& visible = layers.visible;
        std::vector<std::vector<std::array<double, 2>>>& hidden = layers.hidden;

        // R4 — section of a purely-curved solid: if the exact HLR produced no edges, retry
        // the cut bodies through the polygonal (triangulated) HLR so a revolved part still
        // shows its outline in the section view.
        if (visible.empty() && hidden.empty())
            poly_hlr_fallback(cut_shapes, ax, scale, position, lin_defl, ang_defl, show_hidden,
                              visible, hidden);
        pad_sources(layers);

        // HALF section: draw the division boundary line where the sectioned half meets the
        // unsectioned external half, so it no longer reads as a full section. The division
        // plane has normal `half_axis` through `point`; projected into the (sx,sy) screen it
        // is the line through the projected `point`, running PERPENDICULAR to the projected
        // half_axis. We clip it to the geometry bbox so it reaches both edges of the part. (D11)
        std::vector<std::vector<std::array<double, 2>>> all = visible;
        all.insert(all.end(), hidden.begin(), hidden.end());
        all.insert(all.end(), section_faces.begin(), section_faces.end());
        json bbox = bbox_of(all, {});
        if (mode == "half" && have_half && bbox.is_array() && bbox.size() == 4) {
            // Projected division point + half_axis direction in sheet (scaled) coordinates.
            double bdx = dot(half_axis, sx), bdy = dot(half_axis, sy);
            double blen = std::sqrt(bdx * bdx + bdy * bdy);
            double minx = bbox[0].get<double>(), miny = bbox[1].get<double>();
            double maxx = bbox[2].get<double>(), maxy = bbox[3].get<double>();
            if (blen > 1e-9 && maxx > minx && maxy > miny) {
                // Boundary runs perpendicular to the projected half_axis through the cut point.
                double dirx = -bdy / blen, diry = bdx / blen;
                double cx = dot(point, sx) * scale + position[0];
                double cy = dot(point, sy) * scale + position[1];
                // Clip the infinite division line to the bbox rectangle (Liang–Barsky) so it
                // spans the part without overshooting / re-inflating the view bounds.
                double t0 = -1e30, t1 = 1e30;
                const double pq[4][2] = {{-dirx, cx - minx}, {dirx, maxx - cx},
                                         {-diry, cy - miny}, {diry, maxy - cy}};
                bool ok = true;
                for (auto& e : pq) {
                    double p = e[0], q = e[1];
                    if (std::fabs(p) < 1e-12) { if (q < 0) { ok = false; break; } continue; }
                    double t = q / p;
                    if (p < 0) t0 = std::max(t0, t); else t1 = std::min(t1, t);
                }
                if (ok && t1 > t0) {
                    std::vector<std::array<double, 2>> boundary = {
                        {cx + dirx * t0, cy + diry * t0},
                        {cx + dirx * t1, cy + diry * t1}};
                    visible.push_back(boundary);
                    layers.visible_src.push_back(nullptr);
                }
            }
        }

        base["visible"] = polylines_to_json(visible);
        base["hidden"] = polylines_to_json(hidden);
        base["visible_src"] = layers.visible_src;
        base["hidden_src"] = layers.hidden_src;
        base["section_faces"] = polylines_to_json(section_faces);
        base["section_bodies"] = section_bodies;
        base["bbox"] = bbox;
        if (section_faces.empty()) base["error"] = "cut plane does not intersect the model";
    } catch (const std::exception& exc) {
        base["error"] = std::string("exception: ") + exc.what();
    } catch (...) {
        base["error"] = "unknown section exception";
    }
    return base;
}

namespace {

// Clip a sheet polyline to the disc; mirrors _clip_polyline_to_circle.
std::vector<std::vector<std::array<double, 2>>> clip_to_circle(
    const std::vector<std::array<double, 2>>& pl, double cx, double cy, double r) {
    double r2 = r * r;
    auto inside = [&](const std::array<double, 2>& p) {
        return (p[0] - cx) * (p[0] - cx) + (p[1] - cy) * (p[1] - cy) <= r2;
    };
    auto intersect = [&](const std::array<double, 2>& p0,
                         const std::array<double, 2>& p1, double& t) -> bool {
        double dx = p1[0] - p0[0], dy = p1[1] - p0[1];
        double fx = p0[0] - cx, fy = p0[1] - cy;
        double a = dx * dx + dy * dy;
        if (a < 1e-12) return false;
        double b = 2 * (fx * dx + fy * dy);
        double c = fx * fx + fy * fy - r2;
        double disc = b * b - 4 * a * c;
        if (disc < 0) return false;
        double sq = std::sqrt(disc);
        double t0 = (-b - sq) / (2 * a), t1 = (-b + sq) / (2 * a);
        bool ok = false;
        double best = 2.0;
        for (double tt : {t0, t1})
            if (tt >= 0.0 && tt <= 1.0 && tt < best) {
                best = tt;
                ok = true;
            }
        if (ok) t = best;
        return ok;
    };
    std::vector<std::vector<std::array<double, 2>>> out;
    std::vector<std::array<double, 2>> cur;
    for (std::size_t i = 0; i < pl.size(); ++i) {
        const auto& pt = pl[i];
        if (inside(pt)) {
            if (cur.empty() && i > 0) {
                double t;
                if (intersect(pl[i - 1], pt, t))
                    cur.push_back({pl[i - 1][0] + t * (pt[0] - pl[i - 1][0]),
                                   pl[i - 1][1] + t * (pt[1] - pl[i - 1][1])});
            }
            cur.push_back(pt);
        } else {
            if (!cur.empty()) {
                double t;
                if (intersect(pl[i - 1], pt, t))
                    cur.push_back({pl[i - 1][0] + t * (pt[0] - pl[i - 1][0]),
                                   pl[i - 1][1] + t * (pt[1] - pl[i - 1][1])});
                if (cur.size() >= 2) out.push_back(cur);
                cur.clear();
            } else if (i > 0) {
                const auto& p0 = pl[i - 1];
                const auto& p1 = pt;
                double dx = p1[0] - p0[0], dy = p1[1] - p0[1];
                double fx = p0[0] - cx, fy = p0[1] - cy;
                double a = dx * dx + dy * dy;
                if (a >= 1e-12) {
                    double b = 2 * (fx * dx + fy * dy);
                    double cc = fx * fx + fy * fy - r2;
                    double dsc = b * b - 4 * a * cc;
                    if (dsc > 0) {
                        double sq = std::sqrt(dsc);
                        double t0 = (-b - sq) / (2 * a), t1 = (-b + sq) / (2 * a);
                        if (t0 >= 0.0 && t0 <= 1.0 && t1 >= 0.0 && t1 <= 1.0 && t1 > t0)
                            out.push_back({{p0[0] + t0 * dx, p0[1] + t0 * dy},
                                           {p0[0] + t1 * dx, p0[1] + t1 * dy}});
                    }
                }
            }
        }
    }
    if (cur.size() >= 2) out.push_back(cur);
    return out;
}

}  // namespace

json project_detail(const json& parent_geom, std::array<double, 2> center,
                    double radius, double scale, std::array<double, 2> position,
                    const std::string& view_id) {
    json base = {{"view_id", view_id}, {"scale", scale}, {"orientation", "detail"},
                 {"visible", json::array()}, {"hidden", json::array()},
                 {"visible_src", json::array()}, {"hidden_src", json::array()},
                 {"bbox", json(nullptr)}, {"ms", 0.0}, {"error", json(nullptr)}};
    double cx = center[0], cy = center[1], r = radius;
    if (r <= 0) {
        base["error"] = "detail radius must be positive";
        return base;
    }
    double parent_scale = parent_geom.value("scale", 1.0);
    if (parent_scale == 0.0) parent_scale = 1.0;
    double mag = scale / parent_scale;
    double px = position[0], py = position[1];

    try {
        std::vector<std::vector<std::array<double, 2>>> vis_out, hid_out;
        json vis_src = json::array(), hid_src = json::array();
        for (const char* layer : {"visible", "hidden"}) {
            const bool is_visible = std::string(layer) == "visible";
            std::vector<std::vector<std::array<double, 2>>>& kept = is_visible ? vis_out : hid_out;
            json& kept_src = is_visible ? vis_src : hid_src;
            if (!parent_geom.contains(layer) || !parent_geom[layer].is_array()) continue;
            const std::string src_key = std::string(layer) + "_src";
            const json& parent_src =
                parent_geom.contains(src_key) && parent_geom[src_key].is_array()
                    ? parent_geom[src_key] : json::array();
            const json& layer_pls = parent_geom[layer];
            for (std::size_t j = 0; j < layer_pls.size(); ++j) {
                const json& jpl = layer_pls[j];
                const json src = j < parent_src.size() ? parent_src[j] : json(nullptr);
                std::vector<std::array<double, 2>> pl;
                for (const auto& jp : jpl) pl.push_back({jp[0], jp[1]});
                for (auto& sub : clip_to_circle(pl, cx, cy, r)) {
                    if (sub.size() < 2) continue;
                    std::vector<std::array<double, 2>> remapped;
                    for (const auto& p : sub)
                        remapped.push_back({(p[0] - cx) * mag + px, (p[1] - cy) * mag + py});
                    kept.push_back(std::move(remapped));
                    kept_src.push_back(src);
                }
            }
        }
        base["visible"] = polylines_to_json(vis_out);
        base["hidden"] = polylines_to_json(hid_out);
        base["visible_src"] = std::move(vis_src);
        base["hidden_src"] = std::move(hid_src);
        base["bbox"] = bbox_of(vis_out, hid_out);
        if (vis_out.empty() && hid_out.empty())
            base["error"] = "detail circle encloses no parent geometry";
    } catch (const std::exception& exc) {
        base["error"] = std::string("exception: ") + exc.what();
    } catch (...) {
        base["error"] = "unknown detail exception";
    }
    return base;
}

}  // namespace chudware::doc
