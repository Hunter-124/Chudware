// See profile3d.h. Edge building mirrors profile.cpp's build_edge but in world
// 3D coordinates (no plane lift); chaining reuses the tolerant FreeBounds
// stitcher so a line endpoint and a trig-computed arc endpoint bridge float gaps.
#include "geom/profile3d.h"

#include <BRepAdaptor_Curve.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GeomAPI_Interpolate.hxx>
#include <Geom_BSplineCurve.hxx>
#include <ShapeAnalysis_FreeBounds.hxx>
#include <Standard_Failure.hxx>
#include <TColgp_HArray1OfPnt.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_HSequenceOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <gp_Pnt.hxx>

#include <algorithm>
#include <map>
#include <string>

namespace aicad::geom {

namespace {

using nlohmann::json;

double num(const json& v) { return v.is_number() ? v.get<double>() : 0.0; }

bool pnt(const json& a, gp_Pnt& out) {
    if (!a.is_array() || a.size() < 3) return false;
    out = gp_Pnt(num(a[0]), num(a[1]), num(a[2]));
    return true;
}

// One world-space edge per entity; false = not an edge (point/construction/bad).
bool build_edge3(const json& ent, TopoDS_Edge& out) {
    if (!ent.is_object()) return false;
    if (ent.value("construction", false)) return false;
    const std::string t = ent.value("type", std::string());
    try {
        if (t == "line") {
            gp_Pnt a, b;
            if (!pnt(ent.at("p1"), a) || !pnt(ent.at("p2"), b)) return false;
            if (a.Distance(b) < 1e-9) return false;  // degenerate
            out = BRepBuilderAPI_MakeEdge(a, b).Edge();
            return true;
        }
        if (t == "arc") {
            gp_Pnt a, m, b;
            if (!pnt(ent.at("p1"), a) || !pnt(ent.at("pm"), m) || !pnt(ent.at("p2"), b))
                return false;
            GC_MakeArcOfCircle mk(a, m, b);  // start / through / end
            if (!mk.IsDone()) return false;  // collinear -> degenerate
            out = BRepBuilderAPI_MakeEdge(mk.Value()).Edge();
            return true;
        }
        if (t == "eq_curve3d") {
            // Equation-driven 3D curve: sketch3d.cpp already sampled x(t)/y(t)/z(t)
            // into "samples" (extract_entities); interpolate a BSpline through them,
            // same recipe as "spline" below (mirrors the 2D eq_curve's sample-to-
            // spline compile).
            const json& pts = ent.contains("samples") ? ent["samples"] : json();
            if (!pts.is_array() || pts.size() < 2) return false;
            Handle(TColgp_HArray1OfPnt) arr =
                new TColgp_HArray1OfPnt(1, static_cast<Standard_Integer>(pts.size()));
            for (Standard_Integer i = 0; i < static_cast<Standard_Integer>(pts.size()); ++i) {
                gp_Pnt p;
                if (!pnt(pts[i], p)) return false;
                arr->SetValue(i + 1, p);
            }
            GeomAPI_Interpolate interp(arr, /*periodic=*/Standard_False, 1e-7);
            interp.Perform();
            if (!interp.IsDone()) return false;
            out = BRepBuilderAPI_MakeEdge(interp.Curve()).Edge();
            return true;
        }
        if (t == "spline") {
            const json& pts = ent.contains("points") ? ent["points"] : json();
            if (!pts.is_array() || pts.size() < 2) return false;
            Handle(TColgp_HArray1OfPnt) arr =
                new TColgp_HArray1OfPnt(1, static_cast<Standard_Integer>(pts.size()));
            for (Standard_Integer i = 0; i < static_cast<Standard_Integer>(pts.size()); ++i) {
                gp_Pnt p;
                if (!pnt(pts[i], p)) return false;
                arr->SetValue(i + 1, p);
            }
            GeomAPI_Interpolate interp(arr, /*periodic=*/Standard_False, 1e-7);
            interp.Perform();
            if (!interp.IsDone()) return false;
            out = BRepBuilderAPI_MakeEdge(interp.Curve()).Edge();
            return true;
        }
    } catch (const Standard_Failure&) {
        return false;
    }
    return false;  // point / unknown types never edge
}

}  // namespace

std::vector<TopoDS_Wire> wires_from_sketch3d(const json& solved_entities) {
    std::vector<TopoDS_Wire> wires;
    if (!solved_entities.is_object()) return wires;

    // Deterministic entity order: nlohmann::json objects iterate key-sorted, which
    // puts "e10" before "e2" — order by the numeric suffix instead so wire indexing
    // follows authoring order ("e1", "e2", … like the features array).
    std::vector<std::pair<std::string, const json*>> ents;
    for (auto it = solved_entities.begin(); it != solved_entities.end(); ++it)
        ents.emplace_back(it.key(), &it.value());
    auto ord = [](const std::string& id) {
        size_t i = 0;
        while (i < id.size() && !std::isdigit(static_cast<unsigned char>(id[i]))) ++i;
        return i < id.size() ? std::atol(id.c_str() + i) : 0L;
    };
    std::sort(ents.begin(), ents.end(), [&](const auto& a, const auto& b) {
        long oa = ord(a.first), ob = ord(b.first);
        return oa != ob ? oa < ob : a.first < b.first;
    });

    std::vector<TopoDS_Edge> edges;   // in entity order
    for (const auto& kv : ents) {
        TopoDS_Edge e;
        if (build_edge3(*kv.second, e)) edges.push_back(e);
    }
    if (edges.empty()) return wires;

    Handle(TopTools_HSequenceOfShape) in = new TopTools_HSequenceOfShape();
    for (const TopoDS_Edge& e : edges) in->Append(e);
    Handle(TopTools_HSequenceOfShape) out;
    ShapeAnalysis_FreeBounds::ConnectEdgesToWires(in, 1.0e-4, Standard_False, out);
    if (out.IsNull()) return wires;

    // Sort wires by the lowest input-edge index they contain (deterministic
    // "wire 0 = chain of the earliest-authored entity"). The stitcher may REBUILD
    // edges (shared vertices within tolerance), so IsSame against the inputs is
    // unreliable — match by curve MIDPOINT proximity instead, which survives
    // reorientation and vertex rewrites.
    std::vector<gp_Pnt> mids;
    mids.reserve(edges.size());
    for (const TopoDS_Edge& e : edges) {
        BRepAdaptor_Curve c(e);
        mids.push_back(c.Value(0.5 * (c.FirstParameter() + c.LastParameter())));
    }
    std::vector<std::pair<int, TopoDS_Wire>> ranked;
    for (Standard_Integer i = 1; i <= out->Length(); ++i) {
        const TopoDS_Shape& s = out->Value(i);
        if (s.IsNull() || s.ShapeType() != TopAbs_WIRE) continue;
        TopoDS_Wire w = TopoDS::Wire(s);
        int best = static_cast<int>(edges.size());
        for (TopExp_Explorer ex(w, TopAbs_EDGE); ex.More(); ex.Next()) {
            BRepAdaptor_Curve c(TopoDS::Edge(ex.Current()));
            gp_Pnt m = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()));
            for (size_t k = 0; k < mids.size(); ++k)
                if (m.Distance(mids[k]) < 1e-6) best = std::min(best, (int)k);
        }
        ranked.emplace_back(best, w);
    }
    std::stable_sort(ranked.begin(), ranked.end(),
                     [](const auto& a, const auto& b) { return a.first < b.first; });
    for (auto& r : ranked) wires.push_back(r.second);
    return wires;
}

}  // namespace aicad::geom
