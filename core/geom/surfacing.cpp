// Surfacing feature geometry — C++ port of engine/doc/features/surf_*.py.
// See surfacing.h for the per-op kernel-call contract and parity discipline.

#include "geom/surfacing.h"

#include "geom/booleans.h"

#include <algorithm>
#include <cmath>
#include <vector>

#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <BRepAlgoAPI_Splitter.hxx>
#include <BRepBuilderAPI_MakeSolid.hxx>
#include <BRepBuilderAPI_Sewing.hxx>
#include <BRepFill.hxx>
#include <BRepFill_Filling.hxx>
#include <BRepGProp.hxx>
#include <BRepOffset_MakeOffset.hxx>
#include <BRepOffset_Mode.hxx>
#include <BRepOffsetAPI_MakeOffsetShape.hxx>
#include <BRepOffsetAPI_MakePipe.hxx>
#include <BRepOffsetAPI_ThruSections.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepPrimAPI_MakeRevol.hxx>
#include <BRepTools.hxx>
#include <Geom_Surface.hxx>
#include <GeomAbs_Shape.hxx>
#include <GProp_GProps.hxx>
#include <ShapeFix_Shape.hxx>
#include <ShapeFix_Solid.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Solid.hxx>
#include <gp_Ax1.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>
#include <BRepBuilderAPI_Transform.hxx>

namespace geom {

namespace {

// Collect the deduped faces of a shape (TopExp::MapShapes order).
std::vector<TopoDS_Face> faces_of(const TopoDS_Shape& s) {
    std::vector<TopoDS_Face> out;
    TopTools_IndexedMapOfShape m;
    TopExp::MapShapes(s, TopAbs_FACE, m);
    for (int i = 1; i <= m.Extent(); ++i) out.push_back(TopoDS::Face(m.FindKey(i)));
    return out;
}

// Wrap a (possibly single-shell) compound as the tightest surface shape: a lone
// shell unwraps to that shell, a lone face to that face, else the compound itself.
TopoDS_Shape tighten_surface(const TopoDS_Shape& raw) {
    TopTools_IndexedMapOfShape shells;
    TopExp::MapShapes(raw, TopAbs_SHELL, shells);
    if (shells.Extent() == 1) {
        // If the only shell IS the whole shape (already a shell), keep it.
        return shells.FindKey(1);
    }
    TopTools_IndexedMapOfShape fcs;
    TopExp::MapShapes(raw, TopAbs_FACE, fcs);
    if (shells.Extent() == 0 && fcs.Extent() == 1) return fcs.FindKey(1);
    return raw;
}

GeomAbs_Shape continuity_from_int(int c) {
    if (c >= 2) return GeomAbs_C2;
    if (c == 1) return GeomAbs_C1;
    return GeomAbs_C0;
}

}  // namespace

// --------------------------------------------------------------------------- area/counts

double shape_area(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0.0;
    GProp_GProps props;
    BRepGProp::SurfaceProperties(shape, props);
    return props.Mass();
}

int shape_face_count(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0;
    TopTools_IndexedMapOfShape m;
    TopExp::MapShapes(shape, TopAbs_FACE, m);
    return m.Extent();
}

int shape_edge_count(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return 0;
    TopTools_IndexedMapOfShape m;
    TopExp::MapShapes(shape, TopAbs_EDGE, m);
    return m.Extent();
}

Eigen::Vector3d face_centroid(const TopoDS_Face& f) {
    GProp_GProps props;
    BRepGProp::SurfaceProperties(f, props);
    gp_Pnt c = props.CentreOfMass();
    return Eigen::Vector3d(c.X(), c.Y(), c.Z());
}

// --------------------------------------------------------------------------- surf_extrude

TopoDS_Shape surf_extrude_walls(const std::vector<TopoDS_Face>& faces,
                                const Eigen::Vector3d& dir, double amount,
                                bool mid_plane) {
    if (faces.empty() || !(std::abs(amount) > 0.0)) return {};
    Eigen::Vector3d n = dir;
    if (n.norm() > 1e-12) n.normalize();

    // mid_plane: translate the loop to -amount/2 along n, then prism the full amount.
    Eigen::Vector3d start_shift = mid_plane ? (n * (-amount / 2.0)) : Eigen::Vector3d(0, 0, 0);
    Eigen::Vector3d vec = mid_plane ? (n * amount) : (n * amount);
    gp_Vec gvec(vec.x(), vec.y(), vec.z());

    TopoDS_Compound comp;
    BRep_Builder bb;
    bb.MakeCompound(comp);
    int n_walls = 0;
    for (const TopoDS_Face& face : faces) {
        // Iterate ALL boundary wires of the face (outer + inner holes) — each its own wall.
        for (TopExp_Explorer ex(face, TopAbs_WIRE); ex.More(); ex.Next()) {
            TopoDS_Wire wire = TopoDS::Wire(ex.Current());
            TopoDS_Wire w = wire;
            if (mid_plane) {
                gp_Trsf tr;
                tr.SetTranslation(gp_Vec(start_shift.x(), start_shift.y(), start_shift.z()));
                BRepBuilderAPI_Transform mt(wire, tr, true);
                w = TopoDS::Wire(mt.Shape());
            }
            // BRepPrimAPI_MakePrism(wire, vec, Copy=false, Canonize=true) -> side-wall shell.
            BRepPrimAPI_MakePrism mk(w, gvec, false, true);
            if (!mk.IsDone()) return {};
            bb.Add(comp, mk.Shape());
            ++n_walls;
        }
    }
    if (n_walls == 0) return {};
    return tighten_surface(comp);
}

// --------------------------------------------------------------------------- surf_revolve

TopoDS_Shape surf_revolve_walls(const std::vector<TopoDS_Face>& faces,
                                const Eigen::Vector3d& axis_pt,
                                const Eigen::Vector3d& axis_dir, double angle_rad) {
    if (faces.empty() || !(std::abs(angle_rad) > 0.0)) return {};
    Eigen::Vector3d d = axis_dir;
    if (d.norm() > 1e-12) d.normalize();
    gp_Ax1 ax(gp_Pnt(axis_pt.x(), axis_pt.y(), axis_pt.z()), gp_Dir(d.x(), d.y(), d.z()));

    std::vector<TopoDS_Shape> shells;
    for (const TopoDS_Face& face : faces) {
        TopoDS_Wire wire = BRepTools::OuterWire(face);
        if (wire.IsNull()) return {};
        // MakeRevol over a WIRE (not a face) -> an open shell (surface of revolution).
        BRepPrimAPI_MakeRevol mk(wire, ax, std::abs(angle_rad), true);
        if (!mk.IsDone()) return {};
        shells.push_back(mk.Shape());
    }
    if (shells.empty()) return {};
    if (shells.size() == 1) return tighten_surface(shells.front());
    TopoDS_Compound comp;
    BRep_Builder bb;
    bb.MakeCompound(comp);
    for (const auto& s : shells) bb.Add(comp, s);
    return tighten_surface(comp);
}

// --------------------------------------------------------------------------- surf_sweep

TopoDS_Shape surf_sweep_walls(const std::vector<TopoDS_Face>& faces,
                              const TopoDS_Wire& spine) {
    if (faces.empty() || spine.IsNull()) return {};
    std::vector<TopoDS_Shape> shells;
    for (const TopoDS_Face& face : faces) {
        TopoDS_Wire wire = BRepTools::OuterWire(face);
        if (wire.IsNull()) return {};
        // MakePipe over a WIRE profile -> the open swept wall (no caps).
        BRepOffsetAPI_MakePipe mk(spine, wire);
        mk.Build();
        if (!mk.IsDone()) return {};
        shells.push_back(mk.Shape());
    }
    if (shells.empty()) return {};
    if (shells.size() == 1) return tighten_surface(shells.front());
    TopoDS_Compound comp;
    BRep_Builder bb;
    bb.MakeCompound(comp);
    for (const auto& s : shells) bb.Add(comp, s);
    return tighten_surface(comp);
}

// --------------------------------------------------------------------------- surf_loft

TopoDS_Shape surf_loft_shell(const std::vector<TopoDS_Wire>& sections, bool ruled) {
    if (sections.size() < 2) return {};
    BRepOffsetAPI_ThruSections mk(/*isSolid=*/Standard_False, /*ruled=*/ruled ? Standard_True : Standard_False, 1e-6);
    for (const TopoDS_Wire& w : sections) {
        if (w.IsNull()) return {};
        mk.AddWire(w);
    }
    mk.Build();
    if (!mk.IsDone()) return {};
    return tighten_surface(mk.Shape());
}

// --------------------------------------------------------------------------- surf_ruled

TopoDS_Shape surf_ruled_surface(const TopoDS_Wire& rail_a, const TopoDS_Wire& rail_b) {
    if (rail_a.IsNull() || rail_b.IsNull()) return {};
    // Count edges of each rail.
    auto single_edge = [](const TopoDS_Wire& w, TopoDS_Edge& out) -> bool {
        TopTools_IndexedMapOfShape m;
        TopExp::MapShapes(w, TopAbs_EDGE, m);
        if (m.Extent() == 1) { out = TopoDS::Edge(m.FindKey(1)); return true; }
        return false;
    };
    TopoDS_Edge ea, eb;
    if (single_edge(rail_a, ea) && single_edge(rail_b, eb)) {
        // Exact ruled face between two single edges.
        TopoDS_Face f = BRepFill::Face(ea, eb);
        if (f.IsNull()) return {};
        return f;
    }
    // Multi-edge rail: ruled ThruSections over the two wires (open shell).
    BRepOffsetAPI_ThruSections mk(Standard_False, Standard_True, 1e-6);
    mk.AddWire(rail_a);
    mk.AddWire(rail_b);
    mk.Build();
    if (!mk.IsDone()) return {};
    return tighten_surface(mk.Shape());
}

// --------------------------------------------------------------------------- surf_offset

TopoDS_Shape surf_offset_surface(const std::vector<TopoDS_Face>& faces, double distance) {
    if (faces.empty() || !(std::abs(distance) > 1e-9)) return {};
    TopoDS_Shape src;
    if (faces.size() == 1) {
        src = faces.front();
    } else {
        BRepBuilderAPI_Sewing sew(1e-6);
        for (const TopoDS_Face& f : faces) sew.Add(f);
        sew.Perform();
        src = sew.SewedShape();
    }
    BRepOffsetAPI_MakeOffsetShape mk;
    mk.PerformBySimple(src, distance);
    mk.Build();
    if (!mk.IsDone()) return {};
    return tighten_surface(mk.Shape());
}

// --------------------------------------------------------------------------- surf_fill / surf_boundary

TopoDS_Shape surf_fill_patch(const std::vector<TopoDS_Edge>& edges, int continuity) {
    if (edges.size() < 2) return {};
    BRepFill_Filling mk;
    GeomAbs_Shape cont = continuity_from_int(continuity);
    for (const TopoDS_Edge& e : edges) mk.Add(e, cont);
    mk.Build();
    if (!mk.IsDone()) return {};
    TopoDS_Face face = mk.Face();
    if (face.IsNull()) return {};
    GProp_GProps gp;
    BRepGProp::SurfaceProperties(face, gp);
    if (gp.Mass() <= 1e-9) return {};
    return face;
}

// --------------------------------------------------------------------------- surf_knit

TopoDS_Shape surf_knit_bodies(const std::vector<TopoDS_Shape>& bodies, double tol,
                              bool& out_solid) {
    out_solid = false;
    if (bodies.size() < 2) return {};
    BRepBuilderAPI_Sewing sew(tol);
    for (const TopoDS_Shape& b : bodies) {
        if (b.IsNull()) return {};
        sew.Add(b);
    }
    sew.Perform();
    TopoDS_Shape sewn = sew.SewedShape();
    ShapeFix_Shape fixer(sewn);
    fixer.Perform();
    TopoDS_Shape fixed = fixer.Shape();
    if (fixed.IsNull()) return {};

    // First shell (or the fixed shape itself).
    TopoDS_Shape shell_raw = fixed;
    {
        TopTools_IndexedMapOfShape shells;
        TopExp::MapShapes(fixed, TopAbs_SHELL, shells);
        if (shells.Extent() >= 1) shell_raw = shells.FindKey(1);
    }

    // Count free (boundary) edges — edges adjacent to exactly ONE face.
    int free_edges = 0;
    {
        TopTools_IndexedDataMapOfShapeListOfShape m;
        TopExp::MapShapesAndAncestors(shell_raw, TopAbs_EDGE, TopAbs_FACE, m);
        for (int i = 1; i <= m.Extent(); ++i)
            if (m.FindFromIndex(i).Extent() == 1) ++free_edges;
    }

    if (free_edges == 0 && shell_raw.ShapeType() == TopAbs_SHELL) {
        // Watertight -> promote to a solid.
        BRepBuilderAPI_MakeSolid mk;
        mk.Add(TopoDS::Shell(shell_raw));
        if (mk.IsDone()) {
            TopoDS_Solid solid = mk.Solid();
            ShapeFix_Solid sfx(solid);
            sfx.Perform();
            TopoDS_Shape healed = sfx.Solid();
            if (!healed.IsNull()) {
                GProp_GProps gp;
                BRepGProp::VolumeProperties(healed, gp);
                if (gp.Mass() > 1e-9) { out_solid = true; return healed; }
            }
        }
    }
    // Open shell stays a surface body.
    return tighten_surface(fixed);
}

// --------------------------------------------------------------------------- surf_delete_face

TopoDS_Shape surf_delete_faces(const TopoDS_Shape& solid,
                               const std::vector<Eigen::Vector3d>& delete_centroids,
                               double tol) {
    if (solid.IsNull()) return {};
    BRepBuilderAPI_Sewing sew(1e-6);
    int kept = 0;
    for (const TopoDS_Face& f : faces_of(solid)) {
        Eigen::Vector3d c = face_centroid(f);
        bool del = false;
        for (const Eigen::Vector3d& d : delete_centroids)
            if ((c - d).squaredNorm() <= tol * tol) { del = true; break; }
        if (del) continue;
        sew.Add(f);
        ++kept;
    }
    if (kept == 0) return {};
    sew.Perform();
    return tighten_surface(sew.SewedShape());
}

// --------------------------------------------------------------------------- surf_trim

TopoDS_Shape surf_trim_surface(const TopoDS_Shape& surface, const TopoDS_Face& tool_face,
                               const std::string& keep, int& out_split_count) {
    out_split_count = 0;
    if (surface.IsNull() || tool_face.IsNull()) return {};

    // The tool's local normal — the axis the keep-side is measured along.
    Eigen::Vector3d ref_normal(0, 0, 1);
    {
        GProp_GProps gp;
        BRepGProp::SurfaceProperties(tool_face, gp);
        // Use the surface normal via BRepAdaptor would be ideal; for a planar tool the
        // face's plane normal suffices. Approximate via the cross of two boundary edge
        // tangents is overkill — the gate uses a planar tool, so derive from the geom.
    }
    // Derive a robust normal: average triangle normal of the tool face via two UV samples.
    {
        Standard_Real u0, u1, v0, v1;
        BRepTools::UVBounds(tool_face, u0, u1, v0, v1);
        Handle(Geom_Surface) gs = BRep_Tool::Surface(tool_face);
        if (!gs.IsNull()) {
            gp_Pnt p;
            gp_Vec du, dv;
            gs->D1((u0 + u1) / 2.0, (v0 + v1) / 2.0, p, du, dv);
            gp_Vec nv = du.Crossed(dv);
            if (nv.Magnitude() > 1e-12) {
                nv.Normalize();
                ref_normal = Eigen::Vector3d(nv.X(), nv.Y(), nv.Z());
            }
        }
    }

    std::vector<TopoDS_Face> kept_faces;
    for (const TopoDS_Face& face : faces_of(surface)) {
        // Split this face by the tool.
        TopTools_ListOfShape args, tools;
        args.Append(face);
        tools.Append(tool_face);
        BRepAlgoAPI_Splitter splitter;
        splitter.SetArguments(args);
        splitter.SetTools(tools);
        splitter.Build();
        std::vector<TopoDS_Face> pieces;
        if (splitter.IsDone()) {
            for (TopExp_Explorer ex(splitter.Shape(), TopAbs_FACE); ex.More(); ex.Next())
                pieces.push_back(TopoDS::Face(ex.Current()));
        }
        if (pieces.size() < 2) {
            // Tool did not divide this face -> pass it through whole.
            kept_faces.push_back(face);
            continue;
        }
        ++out_split_count;
        if (keep == "both") {
            for (const auto& p : pieces) kept_faces.push_back(p);
            continue;
        }
        // Bucket by centroid projection onto ref_normal at the MEDIAN (split.py _bucket_pieces).
        std::vector<double> projs;
        projs.reserve(pieces.size());
        for (const auto& p : pieces) projs.push_back(ref_normal.dot(face_centroid(p)));
        std::vector<double> sorted_projs = projs;
        std::sort(sorted_projs.begin(), sorted_projs.end());
        size_t n = sorted_projs.size();
        double median = (n % 2) ? sorted_projs[n / 2]
                                : (sorted_projs[n / 2 - 1] + sorted_projs[n / 2]) / 2.0;
        std::vector<TopoDS_Face> kept;
        for (size_t i = 0; i < pieces.size(); ++i) {
            bool take = (keep == "positive") ? (projs[i] >= median) : (projs[i] < median);
            if (take) kept.push_back(pieces[i]);
        }
        if (kept.empty()) kept = pieces;  // degenerate — keep all
        for (const auto& p : kept) kept_faces.push_back(p);
    }

    if (out_split_count == 0 || kept_faces.empty()) return {};

    if (kept_faces.size() == 1) return kept_faces.front();
    BRepBuilderAPI_Sewing sew(1e-6);
    for (const auto& f : kept_faces) sew.Add(f);
    sew.Perform();
    return tighten_surface(sew.SewedShape());
}

// --------------------------------------------------------------------------- surf_replace_face

namespace {
double solid_volume(const TopoDS_Shape& s) {
    GProp_GProps gp;
    BRepGProp::VolumeProperties(s, gp);
    return gp.Mass();
}
bool piece_has_face_at(const TopoDS_Shape& piece, const Eigen::Vector3d& c, double tol) {
    for (const TopoDS_Face& f : faces_of(piece)) {
        Eigen::Vector3d fc = face_centroid(f);
        if ((fc - c).squaredNorm() <= tol * tol) return true;
    }
    return false;
}
}  // namespace

TopoDS_Shape surf_replace_face_solid(const TopoDS_Shape& solid,
                                     const std::vector<Eigen::Vector3d>& target_centroids,
                                     const TopoDS_Shape& surface, double diag) {
    if (solid.IsNull() || surface.IsNull()) return {};

    // BOP-split the solid by the replacement surface's faces.
    TopTools_ListOfShape args, tools;
    args.Append(solid);
    {
        TopTools_IndexedMapOfShape fm;
        TopExp::MapShapes(surface, TopAbs_FACE, fm);
        for (int i = 1; i <= fm.Extent(); ++i) tools.Append(fm.FindKey(i));
    }
    BRepAlgoAPI_Splitter splitter;
    splitter.SetArguments(args);
    splitter.SetTools(tools);
    splitter.Build();
    if (!splitter.IsDone()) return {};

    std::vector<TopoDS_Solid> pieces;
    for (TopExp_Explorer ex(splitter.Shape(), TopAbs_SOLID); ex.More(); ex.Next()) {
        TopoDS_Solid sw = TopoDS::Solid(ex.Current());
        if (solid_volume(sw) > 1e-9) pieces.push_back(sw);
    }
    if (pieces.size() < 2) return {};

    double tol = std::max(diag * 1e-4, 1e-4);
    std::vector<TopoDS_Solid> retained;
    for (const TopoDS_Solid& p : pieces) {
        bool carries = false;
        for (const Eigen::Vector3d& c : target_centroids)
            if (piece_has_face_at(p, c, tol)) { carries = true; break; }
        if (!carries) retained.push_back(p);
    }
    if (retained.empty()) {
        // Degenerate: keep the largest piece.
        TopoDS_Solid best = pieces.front();
        double bv = solid_volume(best);
        for (const auto& p : pieces) { double v = solid_volume(p); if (v > bv) { bv = v; best = p; } }
        retained.push_back(best);
    }
    TopoDS_Shape fused = retained.front();
    for (size_t i = 1; i < retained.size(); ++i) fused = ::geom::fuse(fused, retained[i]);
    if (fused.IsNull() || solid_volume(fused) <= 1e-9) return {};
    return fused;
}

// --------------------------------------------------------------------------- surf_thicken

namespace {
TopoDS_Shape thicken_one_side(const TopoDS_Shape& surface, double offset) {
    BRepOffset_MakeOffset mk;
    mk.Initialize(surface, offset, 1e-6, BRepOffset_Skin,
                  Standard_False,  // Intersection
                  Standard_False,  // SelfInter
                  GeomAbs_Arc,     // Join
                  Standard_True,   // Thickening (solid-from-sheet)
                  Standard_False); // RemoveIntEdges
    mk.MakeOffsetShape();
    if (!mk.IsDone()) return {};
    return mk.Shape();
}
}  // namespace

TopoDS_Shape surf_thicken_solid(const TopoDS_Shape& surface, double thickness,
                                const std::string& side, bool flip) {
    if (surface.IsNull() || !(thickness > 0.0)) return {};
    TopoDS_Shape result;
    if (side == "one") {
        double off = flip ? -thickness : thickness;
        result = thicken_one_side(surface, off);
    } else {  // both / mid: thicken +t/2 and -t/2 and fuse (oracle parity).
        double half = thickness / 2.0;
        TopoDS_Shape fwd = thicken_one_side(surface, half);
        TopoDS_Shape rev = thicken_one_side(surface, -half);
        if (fwd.IsNull() || rev.IsNull()) return {};
        result = ::geom::fuse(fwd, rev);
    }
    if (result.IsNull()) return {};
    GProp_GProps gp;
    BRepGProp::VolumeProperties(result, gp);
    if (gp.Mass() <= 1e-9) return {};
    return result;
}

}  // namespace geom
