#include "geom/features.h"

#include "geom/booleans.h"

#include <algorithm>
#include <cmath>

#include <BRep_Tool.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <Geom_Surface.hxx>
#include <BRepBuilderAPI_TransitionMode.hxx>
#include <Standard_Failure.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepOffsetAPI_MakeOffset.hxx>
#include <BRepOffsetAPI_MakePipe.hxx>
#include <BRepOffsetAPI_MakePipeShell.hxx>
#include <BRepOffsetAPI_ThruSections.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCone.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepPrimAPI_MakeRevol.hxx>
#include <BRepPrimAPI_MakeSphere.hxx>
#include <BRepPrimAPI_MakeTorus.hxx>
#include <BRepBuilderAPI_FindPlane.hxx>
#include <BRepTools.hxx>
#include <BRepTools_WireExplorer.hxx>
#include <GeomAbs_JoinType.hxx>
#include <GProp_GProps.hxx>
#include <BRepGProp.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax1.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

TopoDS_Shape make_box(double dx, double dy, double dz) {
    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape();
}

TopoDS_Shape extrude_rect(double w, double h, double depth) {
    // Closed rectangular wire in the world XY plane, min corner at the origin.
    BRepBuilderAPI_MakePolygon poly;
    poly.Add(gp_Pnt(0.0, 0.0, 0.0));
    poly.Add(gp_Pnt(w, 0.0, 0.0));
    poly.Add(gp_Pnt(w, h, 0.0));
    poly.Add(gp_Pnt(0.0, h, 0.0));
    poly.Close();
    TopoDS_Wire wire = poly.Wire();

    // Planar face bounded by the wire, then prism it +Z by `depth`.
    TopoDS_Face face = BRepBuilderAPI_MakeFace(wire).Face();
    return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, depth)).Shape();
}

TopoDS_Shape extrude_face(const TopoDS_Face& face, const Eigen::Vector3d& dir,
                          double depth, bool mid_plane) {
    const gp_Vec full(dir.x() * depth, dir.y() * depth, dir.z() * depth);
    TopoDS_Shape prism;
    if (mid_plane) {
        // Center the solid on the face: shift the face back by half the depth,
        // then prism the full depth so it straddles the sketch plane symmetrically.
        gp_Trsf trsf;
        trsf.SetTranslation(gp_Vec(-0.5 * full.X(), -0.5 * full.Y(), -0.5 * full.Z()));
        TopoDS_Shape shifted = BRepBuilderAPI_Transform(face, trsf, true).Shape();
        prism = BRepPrimAPI_MakePrism(shifted, full).Shape();
    } else {
        prism = BRepPrimAPI_MakePrism(face, full).Shape();
    }
    // Collinear adjacent segments in the profile (one straight edge drawn as 2+ segments)
    // would each sweep into a SEPARATE coplanar side face joined by a phantom edge. Unify
    // same-domain faces/edges (build123d .clean() parity) so the wall is one face and the
    // phantom edge is gone — it reads, selects, and fillets like SolidWorks. No-op when the
    // profile has no collinear runs.
    return unify_same_domain(prism);
}

namespace {
// Planar face normal at its UV midpoint (build123d-parity normal probe).
Eigen::Vector3d face_plane_normal(const TopoDS_Face& face) {
    Standard_Real u0, u1, v0, v1;
    BRepTools::UVBounds(face, u0, u1, v0, v1);
    Handle(Geom_Surface) gs = BRep_Tool::Surface(face);
    if (gs.IsNull()) return Eigen::Vector3d::Zero();
    gp_Pnt p; gp_Vec du, dv;
    gs->D1((u0 + u1) / 2.0, (v0 + v1) / 2.0, p, du, dv);
    gp_Vec nv = du.Crossed(dv);
    if (nv.Magnitude() < 1e-12) return Eigen::Vector3d::Zero();
    nv.Normalize();
    return Eigen::Vector3d(nv.X(), nv.Y(), nv.Z());
}

// Min/max of (point - axis_pt)·v over the face's outer wire, sampling each edge so a
// circular/spline edge (whose extent is NOT captured by its vertices) is measured too.
void face_v_extent(const TopoDS_Face& face, const Eigen::Vector3d& axis_pt,
                   const Eigen::Vector3d& v, double& vmin, double& vmax) {
    vmin = 1e300; vmax = -1e300;
    for (TopExp_Explorer ex(face, TopAbs_EDGE); ex.More(); ex.Next()) {
        BRepAdaptor_Curve ac(TopoDS::Edge(ex.Current()));
        const double t0 = ac.FirstParameter(), t1 = ac.LastParameter();
        const int N = 24;
        for (int i = 0; i <= N; ++i) {
            gp_Pnt p = ac.Value(t0 + (t1 - t0) * (double(i) / N));
            const double d = (Eigen::Vector3d(p.X(), p.Y(), p.Z()) - axis_pt).dot(v);
            vmin = std::min(vmin, d);
            vmax = std::max(vmax, d);
        }
    }
}
}  // namespace

TopoDS_Shape revolve_face(const TopoDS_Face& face, const Eigen::Vector3d& axis_pt,
                          const Eigen::Vector3d& axis_dir, double angle_rad) {
    gp_Ax1 axis(gp_Pnt(axis_pt.x(), axis_pt.y(), axis_pt.z()),
                gp_Dir(axis_dir.x(), axis_dir.y(), axis_dir.z()));

    // When the profile STRADDLES the axis (e.g. a full circle centered on the axis, the
    // sphere case), revolving it sweeps each half through the other → a self-intersecting,
    // degenerate solid. The kernel-correct CAD behavior (SolidWorks parity) is to revolve a
    // profile that lies on ONE side of the axis. So if the face crosses the axis line, clip
    // it to its larger half-plane first: a half-disk revolved 360° is a clean sphere.
    TopoDS_Face prof = face;
    const Eigen::Vector3d N = face_plane_normal(face);
    if (N.squaredNorm() > 0.5) {
        // In-plane basis: u along the axis (projected into the face plane), v perpendicular
        // to the axis within the plane. v is the direction the profile is split across.
        Eigen::Vector3d u = axis_dir - N * axis_dir.dot(N);
        if (u.norm() > 1e-9) {
            u.normalize();
            Eigen::Vector3d v = N.cross(u);
            if (v.norm() > 1e-9) {
                v.normalize();
                double vmin, vmax;
                face_v_extent(face, axis_pt, v, vmin, vmax);
                const double tol = 1e-7 * std::max(1.0, std::abs(vmax) + std::abs(vmin));
                if (vmin < -tol && vmax > tol) {
                    // Straddles the axis — keep the larger side (b>=0 vs b<=0). Build a big
                    // coplanar half-plane rectangle on that side and intersect the profile.
                    const bool keep_pos = (vmax >= -vmin);
                    const double big = (std::abs(vmax) + std::abs(vmin)) * 4.0 + 1.0;
                    const double b0 = keep_pos ? 0.0 : -big;
                    const double b1 = keep_pos ? big : 0.0;
                    auto P = [&](double a, double b) {
                        Eigen::Vector3d q = axis_pt + u * a + v * b;
                        return gp_Pnt(q.x(), q.y(), q.z());
                    };
                    BRepBuilderAPI_MakePolygon poly;
                    poly.Add(P(-big, b0));
                    poly.Add(P(big, b0));
                    poly.Add(P(big, b1));
                    poly.Add(P(-big, b1));
                    poly.Close();
                    TopoDS_Shape half = BRepBuilderAPI_MakeFace(poly.Wire()).Shape();
                    TopoDS_Shape clipped = common(face, half);
                    // Take the resulting (largest) face; fall back to the raw face if the
                    // intersection failed so a non-degenerate profile is never lost.
                    double best = 0.0;
                    for (TopExp_Explorer fx(clipped, TopAbs_FACE); fx.More(); fx.Next()) {
                        TopoDS_Face cf = TopoDS::Face(fx.Current());
                        GProp_GProps g; BRepGProp::SurfaceProperties(cf, g);
                        if (g.Mass() > best) { best = g.Mass(); prof = cf; }
                    }
                }
            }
        }
    }

    TopoDS_Shape solid = BRepPrimAPI_MakeRevol(prof, axis, angle_rad).Shape();
    // Same collinear-segment cleanup as extrude_face: collinear profile runs revolve into
    // separate cocylindrical/coplanar faces split by a phantom edge — merge them so the
    // revolved body has one face per real wall and no spurious edges to confuse fillet.
    return unify_same_domain(solid);
}

TopoDS_Shape make_box_centered(double l, double w, double h) {
    if (!(l > 0 && w > 0 && h > 0)) return TopoDS_Shape();
    return BRepPrimAPI_MakeBox(gp_Pnt(-l / 2, -w / 2, -h / 2), l, w, h).Shape();
}

TopoDS_Shape make_cylinder_centered(double r, double h) {
    if (!(r > 0 && h > 0)) return TopoDS_Shape();
    gp_Ax2 ax(gp_Pnt(0, 0, -h / 2), gp_Dir(0, 0, 1));
    return BRepPrimAPI_MakeCylinder(ax, r, h).Shape();
}

TopoDS_Shape make_sphere(double r) {
    if (!(r > 0)) return TopoDS_Shape();
    return BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), r).Shape();
}

TopoDS_Shape make_cone_centered(double r1, double r2, double h) {
    if (!(h > 0) || r1 < 0 || r2 < 0 || (r1 == 0 && r2 == 0)) return TopoDS_Shape();
    gp_Ax2 ax(gp_Pnt(0, 0, -h / 2), gp_Dir(0, 0, 1));
    return BRepPrimAPI_MakeCone(ax, r1, r2, h).Shape();
}

TopoDS_Shape make_torus(double major_r, double minor_r) {
    if (!(major_r > 0 && minor_r > 0)) return TopoDS_Shape();
    gp_Ax2 ax(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    return BRepPrimAPI_MakeTorus(ax, major_r, minor_r).Shape();
}

TopoDS_Shape make_box_at(double x, double y, double z, double dx, double dy, double dz) {
    if (!(dx > 0 && dy > 0 && dz > 0)) return TopoDS_Shape();
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape();
}

TopoDS_Shape make_rib_slab(const TopoDS_Wire& contour, const Eigen::Vector3d& normal,
                           const Eigen::Vector3d& toward, double thickness, double reach) {
    if (contour.IsNull() || !(thickness > 0.0) || !(reach > 0.0)) return TopoDS_Shape();

    // Contour chord (endpoint -> endpoint); an open wire has two distinct ends.
    TopoDS_Vertex v1, v2;
    TopExp::Vertices(contour, v1, v2);
    if (v1.IsNull() || v2.IsNull()) return TopoDS_Shape();  // closed contour
    gp_Pnt q1 = BRep_Tool::Pnt(v1), q2 = BRep_Tool::Pnt(v2);
    Eigen::Vector3d a(q1.X(), q1.Y(), q1.Z()), b(q2.X(), q2.Y(), q2.Z());
    Eigen::Vector3d chord = b - a;
    if (chord.norm() < 1e-9) return TopoDS_Shape();
    Eigen::Vector3d cdir = chord.normalized();

    // Drop direction: in-plane, perpendicular to the contour, toward the body.
    Eigen::Vector3d n = normal.normalized();
    Eigen::Vector3d perp = n.cross(cdir);
    if (perp.norm() < 1e-9) return TopoDS_Shape();
    perp.normalize();
    if (perp.dot(toward) < 0.0) perp = -perp;
    const Eigen::Vector3d t_axis = n;
    const double half = thickness / 2.0;

    const gp_Vec dropv(perp.x() * reach, perp.y() * reach, perp.z() * reach);
    const gp_Vec thickv(t_axis.x() * thickness, t_axis.y() * thickness, t_axis.z() * thickness);
    gp_Trsf back;
    back.SetTranslation(gp_Vec(-t_axis.x() * half, -t_axis.y() * half, -t_axis.z() * half));

    // Per edge: extrude into a silhouette face, recenter on the sketch plane, thicken.
    TopoDS_Shape slab;
    for (TopExp_Explorer ex(contour, TopAbs_EDGE); ex.More(); ex.Next()) {
        TopoDS_Edge e = TopoDS::Edge(ex.Current());
        TopoDS_Shape sil = BRepPrimAPI_MakePrism(e, dropv).Shape();  // edge -> face
        if (sil.IsNull()) continue;
        TopoDS_Shape sil0 = BRepBuilderAPI_Transform(sil, back, true).Shape();
        TopoDS_Shape piece = BRepPrimAPI_MakePrism(sil0, thickv).Shape();  // face -> solid
        if (piece.IsNull()) continue;
        slab = slab.IsNull() ? piece : fuse(slab, piece);
        if (slab.IsNull()) return TopoDS_Shape();
    }
    return slab;
}

namespace {
// Offset a CLOSED planar wire concentrically by `off` (outward for +, inward for -),
// with GeomAbs_Intersection (sharp polygon corners — matches build123d Kind.INTERSECTION).
// Returns the original wire for ~0 offset; a null wire on failure.
TopoDS_Wire offset_wire(const TopoDS_Wire& wire, double off) {
    if (std::abs(off) < 1e-12) return wire;
    BRepOffsetAPI_MakeOffset mk(wire, GeomAbs_Intersection);
    mk.Perform(off);
    if (!mk.IsDone()) return TopoDS_Wire();
    TopoDS_Shape r = mk.Shape();
    if (r.IsNull()) return TopoDS_Wire();
    if (r.ShapeType() == TopAbs_WIRE) return TopoDS::Wire(r);
    // The offset of a wire may come back wrapped in a compound — take the first wire.
    for (TopExp_Explorer ex(r, TopAbs_WIRE); ex.More(); ex.Next())
        return TopoDS::Wire(ex.Current());
    return TopoDS_Wire();
}
}  // namespace

TopoDS_Face thin_ring_face(const TopoDS_Face& face, double outer_off, double inner_off) {
    if (face.IsNull()) return TopoDS_Face();
    TopoDS_Wire perim = BRepTools::OuterWire(face);
    if (perim.IsNull()) return TopoDS_Face();

    TopoDS_Wire outer_w = offset_wire(perim, outer_off);
    TopoDS_Wire inner_w = offset_wire(perim, inner_off);
    if (outer_w.IsNull() || inner_w.IsNull()) return TopoDS_Face();

    // Build the ring face = outer wire with the inner wire as a (reversed) hole. The
    // offset wires are co-planar with the original face; MakeFace(outer) then Add(inner
    // reversed) carves the bore, mirroring Face(outer, inner_wires=[inner]) in Python.
    BRepBuilderAPI_MakeFace mf(outer_w, /*OnlyPlane=*/true);
    if (!mf.IsDone()) return TopoDS_Face();
    mf.Add(TopoDS::Wire(inner_w.Reversed()));
    if (!mf.IsDone()) return TopoDS_Face();
    TopoDS_Face ring = mf.Face();

    GProp_GProps gp;
    BRepGProp::SurfaceProperties(ring, gp);
    if (!(std::abs(gp.Mass()) > 1e-9)) return TopoDS_Face();  // empty wall
    return ring;
}

namespace {
// Loft a tapered solid between a base wire (at the profile plane) and a top wire (the
// base wire offset by `off` and translated by `shift`). Used by extrude_face_tapered.
TopoDS_Shape tapered_lump(const TopoDS_Wire& base, double off, const gp_Vec& shift) {
    TopoDS_Wire top = offset_wire(base, off);
    if (top.IsNull()) return TopoDS_Shape();
    gp_Trsf tr;
    tr.SetTranslation(shift);
    top = TopoDS::Wire(BRepBuilderAPI_Transform(top, tr, true).Shape());
    BRepOffsetAPI_ThruSections mk(Standard_True, Standard_False, 1.0e-6);
    mk.AddWire(base);
    mk.AddWire(top);
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}
}  // namespace

TopoDS_Shape extrude_face_tapered(const TopoDS_Face& face, const Eigen::Vector3d& dir,
                                  double depth, double taper_deg, bool mid_plane) {
    if (face.IsNull() || !(depth > 0.0)) return TopoDS_Shape();
    const double t = std::tan(taper_deg * 3.14159265358979323846 / 180.0);
    TopoDS_Wire outer = BRepTools::OuterWire(face);
    if (outer.IsNull()) return TopoDS_Shape();

    auto build = [&](double seg_depth, const Eigen::Vector3d& d) -> TopoDS_Shape {
        // Positive taper narrows toward the extrude direction: offset the top INWARD.
        const double off = -seg_depth * t;
        const gp_Vec shift(d.x() * seg_depth, d.y() * seg_depth, d.z() * seg_depth);
        TopoDS_Shape lump = tapered_lump(outer, off, shift);
        if (lump.IsNull()) return lump;
        // Carve each inner (hole) wire as its own tapered lump and subtract it, so a
        // washer profile drafts both boundaries (matches build123d's tapered hole).
        for (TopExp_Explorer ex(face, TopAbs_WIRE); ex.More(); ex.Next()) {
            TopoDS_Wire w = TopoDS::Wire(ex.Current());
            if (w.IsSame(outer)) continue;
            TopoDS_Shape hole = tapered_lump(w, off, shift);
            if (!hole.IsNull()) lump = cut(lump, hole);
        }
        return lump;
    };

    if (mid_plane) {
        TopoDS_Shape fwd = build(depth / 2.0, dir);
        TopoDS_Shape rev = build(depth / 2.0, -dir);
        if (fwd.IsNull() || rev.IsNull()) return TopoDS_Shape();
        return fuse(fwd, rev);
    }
    return build(depth, dir);
}

// Sweep a single CLOSED profile wire along the spine into a capped solid via
// BRepOffsetAPI_MakePipeShell (the general sweeper — far more robust on curved /
// helical spines than MakePipe, and the only one that supports orientation laws
// + solid capping). orientation: 0=follow_path (CorrectedFrenet, the SolidWorks
// default and the most stable for helices), 1=frenet (true Frenet frame, can flip
// at inflections), 2=keep_normal (fixed +Z binormal — a profile that never twists).
// Null shape on kernel failure so the caller can fall back / error cleanly.
static TopoDS_Shape sweep_wire_solid(const TopoDS_Wire& profile, const TopoDS_Wire& spine,
                                     int orientation) {
    if (profile.IsNull() || spine.IsNull()) return TopoDS_Shape();
    try {
        BRepOffsetAPI_MakePipeShell mk(spine);
        if (orientation == 2)      mk.SetMode(gp_Dir(0, 0, 1));   // fixed binormal
        else if (orientation == 1) mk.SetMode(Standard_True);     // Frenet
        else                       mk.SetMode(Standard_False);    // CorrectedFrenet
        // Transition handling matters on a polyline spine with sharp corners
        // (e.g. an L-shaped path): round the joints so the shell stays valid.
        mk.SetTransitionMode(BRepBuilderAPI_RoundCorner);
        // withContact=false keeps the profile where the user drew it (SolidWorks
        // pierces the path through the profile); withCorrection=false leaves the
        // chosen orientation law in charge.
        mk.Add(profile, Standard_False, Standard_False);
        if (!mk.IsReady()) return TopoDS_Shape();
        mk.Build();
        if (!mk.IsDone()) return TopoDS_Shape();
        if (!mk.MakeSolid()) return TopoDS_Shape();
        return mk.Shape();
    } catch (const Standard_Failure&) {
        return TopoDS_Shape();
    }
}

// Sweep a planar profile FACE (outer boundary + any hole wires) along a spine.
// The outer wire sweeps to a solid; each hole wire sweeps to a solid that is cut
// out, so a washer cross-section becomes a hollow tube (essential for threads /
// pipes). Falls back to the simple BRepOffsetAPI_MakePipe over the face when the
// shell sweeper can't close the outer solid. Empty shape only if everything fails.
TopoDS_Shape sweep_profile(const TopoDS_Face& profile, const TopoDS_Wire& spine,
                           int orientation) {
    if (profile.IsNull() || spine.IsNull()) return TopoDS_Shape();
    TopoDS_Wire outer = BRepTools::OuterWire(profile);
    if (outer.IsNull()) return TopoDS_Shape();

    TopoDS_Shape solid = sweep_wire_solid(outer, spine, orientation);
    if (solid.IsNull()) {
        // Fallback: the classic pipe over the whole face. Less robust but covers
        // simple straight/gently-curved spines where MakePipeShell is fussy.
        try {
            BRepOffsetAPI_MakePipe mk(spine, profile);
            mk.Build();
            if (mk.IsDone()) return mk.Shape();
        } catch (const Standard_Failure&) {}
        return TopoDS_Shape();
    }
    // Subtract swept holes so washer / annular profiles yield hollow solids.
    for (TopExp_Explorer ex(profile, TopAbs_WIRE); ex.More(); ex.Next()) {
        TopoDS_Wire w = TopoDS::Wire(ex.Current());
        if (w.IsSame(outer)) continue;
        TopoDS_Shape hole = sweep_wire_solid(w, spine, orientation);
        if (!hole.IsNull()) {
            TopoDS_Shape cutres = cut(solid, hole);
            if (!cutres.IsNull()) solid = cutres;
        }
    }
    return solid;
}

TopoDS_Shape loft_sections(const std::vector<TopoDS_Wire>& sections, bool ruled) {
    if (sections.size() < 2) return TopoDS_Shape();
    // isSolid=true caps the ends so the loft is a closed solid; pres3d default tol.
    BRepOffsetAPI_ThruSections mk(/*isSolid=*/Standard_True,
                                  /*ruled=*/ruled ? Standard_True : Standard_False,
                                  /*pres3d=*/1.0e-6);
    for (const TopoDS_Wire& w : sections) {
        if (w.IsNull()) return TopoDS_Shape();
        mk.AddWire(w);
    }
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

}  // namespace geom
