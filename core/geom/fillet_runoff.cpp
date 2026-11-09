#include "geom/fillet_runoff.h"

#include <algorithm>
#include <array>
#include <cmath>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBndLib.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <gp_Trsf.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS_Compound.hxx>
#include <Geom2d_Curve.hxx>
#include <Geom_Surface.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopoDS.hxx>
#include <gp_Ax3.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Vec.hxx>

#include "geom/booleans.h"
#include "geom/edge_analysis.h"

namespace geom {
namespace {

// Two faces meet at a right angle when their normals have no component along each other.
// Geometry from a sketch extrusion is exact to far better than this.
constexpr double kPerpendicular = 1e-6;

// Outward unit normal of `face` at `fraction` along `edge`'s pcurve. `face` carries the
// orientation it has in the solid, which flips the surface normal outward.
bool outward_normal(const TopoDS_Edge& edge, const TopoDS_Face& face, double fraction,
                    gp_Vec& normal) {
    Standard_Real first = 0, last = 0;
    const auto pcurve = BRep_Tool::CurveOnSurface(edge, face, first, last);
    if (pcurve.IsNull() || !std::isfinite(first) || !std::isfinite(last) || first == last)
        return false;
    const gp_Pnt2d uv = pcurve->Value(first + fraction * (last - first));
    const auto surface = BRep_Tool::Surface(face);
    if (surface.IsNull()) return false;
    gp_Pnt p;
    gp_Vec du, dv;
    surface->D1(uv.X(), uv.Y(), p, du, dv);
    normal = du.Crossed(dv);
    if (normal.SquareMagnitude() < 1e-24) return false;
    if (face.Orientation() == TopAbs_REVERSED) normal.Reverse();
    normal.Normalize();
    return true;
}

gp_Vec plane_outward_normal(const TopoDS_Face& face) {
    gp_Vec n(BRepAdaptor_Surface(face).Plane().Axis().Direction());
    if (face.Orientation() == TopAbs_REVERSED) n.Reverse();
    return n;
}

enum class Rim {
    open,     // a convex rim meeting its neighbour at a right angle: the support ends here
    swept,    // a convex rim meeting its neighbour at another angle, or tangent to it
    ignored,  // a concave rim: a wall rises from the support, which does not end here
    refused   // not convex, or a surface no extension can continue
};

// How `support` ends along its boundary edge `rim`, whose neighbouring face is `wall`.
Rim classify_rim(const TopoDS_Edge& rim, const TopoDS_Face& support, const TopoDS_Face& wall,
                 const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces) {
    const auto frame = chudware::geom::edge_blend_frame(rim, edge_faces);
    if (!frame || !frame->concave) return Rim::refused;
    if (*frame->concave) return Rim::ignored;
    for (const double fraction : {0.25, 0.5, 0.75}) {
        gp_Vec support_normal, wall_normal;
        if (!outward_normal(rim, support, fraction, support_normal) ||
            !outward_normal(rim, wall, fraction, wall_normal))
            return Rim::refused;
        if (std::abs(support_normal.Dot(wall_normal)) > kPerpendicular) return Rim::swept;
    }
    return Rim::open;
}

TopoDS_Shape prism_solid(const TopoDS_Shape& base, const gp_Vec& vector) {
    BRepPrimAPI_MakePrism sweep(base, vector);
    sweep.Build();
    if (!sweep.IsDone()) return {};
    TopoDS_Shape solid = sweep.Shape();
    GProp_GProps props;
    BRepGProp::VolumeProperties(solid, props);
    if (props.Mass() < 0.0) solid.Reverse();
    return solid;
}

// Every edge of `body` that lies on one of `targets`, when together they cover the targets'
// total length. Collective, because merging same-domain faces may join two target arcs of
// one circle into a single edge.
bool image_edges(const TopTools_IndexedMapOfShape& body_edges,
                 const std::vector<TopoDS_Edge>& targets, std::vector<TopoDS_Edge>& out) {
    constexpr double kOn = 1e-5;
    const auto samples = [](const TopoDS_Edge& edge) {
        const BRepAdaptor_Curve curve(edge);
        std::array<gp_Pnt, 3> points;
        int n = 0;
        for (const double fraction : {0.0, 0.5, 1.0})
            points[n++] = curve.Value(curve.FirstParameter() +
                fraction * (curve.LastParameter() - curve.FirstParameter()));
        return points;
    };
    const auto on_shape = [&](const gp_Pnt& p, const TopoDS_Shape& shape) {
        BRepExtrema_DistShapeShape distance(BRepBuilderAPI_MakeVertex(p).Vertex(), shape);
        return distance.IsDone() && distance.Value() <= kOn;
    };
    BRep_Builder builder;
    TopoDS_Compound all;
    builder.MakeCompound(all);
    Bnd_Box target_box;
    for (const TopoDS_Edge& target : targets) {
        builder.Add(all, target);
        BRepBndLib::Add(target, target_box);
    }
    target_box.Enlarge(kOn);
    for (int i = 1; i <= body_edges.Extent(); ++i) {
        const TopoDS_Edge& candidate = TopoDS::Edge(body_edges(i));
        if (BRep_Tool::Degenerated(candidate)) continue;
        Bnd_Box box;
        BRepBndLib::Add(candidate, box);
        if (box.IsOut(target_box)) continue;
        const auto points = samples(candidate);
        bool lies = std::all_of(points.begin(), points.end(),
                                [&](const gp_Pnt& p) { return on_shape(p, all); });
        bool carries = false;
        if (!lies)
            for (const TopoDS_Edge& target : targets) {
                const auto ends = samples(target);
                if (std::all_of(ends.begin(), ends.end(),
                                [&](const gp_Pnt& p) { return on_shape(p, candidate); })) {
                    carries = true;
                    break;
                }
            }
        if (lies || carries) out.push_back(candidate);
    }
    if (out.empty()) return false;
    TopoDS_Compound images;
    builder.MakeCompound(images);
    for (const TopoDS_Edge& image : out) builder.Add(images, image);
    for (const TopoDS_Edge& target : targets)
        for (const gp_Pnt& p : samples(target))
            if (!on_shape(p, images)) return false;
    return true;
}

// Whether the blend of any of `border` can overrun `rim`: some sampled point of the edge's
// interior has the rim within `reach`, off to the side of the edge. A rim straight ahead of
// the edge (the plate end a rib runs out to) is met by a blend that ends flush there, which
// is not an overflow.
bool within_reach(const TopoDS_Edge& rim, const std::vector<TopoDS_Edge>& border, double reach) {
    for (const TopoDS_Edge& edge : border) {
        const BRepAdaptor_Curve curve(edge);
        for (const double fraction : {0.15, 0.3, 0.5, 0.7, 0.85}) {
            gp_Pnt p;
            gp_Vec tangent;
            curve.D1(curve.FirstParameter() +
                         fraction * (curve.LastParameter() - curve.FirstParameter()), p, tangent);
            if (tangent.SquareMagnitude() < 1e-24) continue;
            tangent.Normalize();
            BRepExtrema_DistShapeShape distance(BRepBuilderAPI_MakeVertex(p).Vertex(), rim);
            if (!distance.IsDone() || distance.NbSolution() < 1 || !(distance.Value() < reach))
                continue;
            const gp_Vec ahead(p, distance.PointOnShape2(1));
            if (std::abs(ahead.Dot(tangent)) <= 0.5 * distance.Value()) return true;
        }
    }
    return false;
}

}  // namespace

FilletRunoffPlan fillet_runoff_plan(const TopoDS_Shape& shape,
                                    const std::vector<TopoDS_Edge>& edges,
                                    const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
                                    double radius) {
    FilletRunoffPlan plan;
    const auto refuse = [&](const char* why) {
        plan.rings.clear();
        plan.caps.clear();
        plan.separable = false;
        plan.why = why;
        return plan;
    };
    try {
        if (shape.IsNull() || edges.empty() || !(radius > 0.0) || !std::isfinite(radius))
            return refuse("run-off needs a body, edges and a positive radius");
        int solids = 0;
        for (TopExp_Explorer s(shape, TopAbs_SOLID); s.More(); s.Next()) ++solids;
        if (solids != 1) return refuse("run-off works on a single solid");
        Bnd_Box box;
        BRepBndLib::Add(shape, box);
        if (box.IsVoid() || box.IsWhole()) return refuse("the body has no finite extent");
        double x0, y0, z0, x1, y1, z1;
        box.Get(x0, y0, z0, x1, y1, z1);
        // A blend larger than the whole body is a typo, not an overflow to terminate.
        if (radius > std::max({x1 - x0, y1 - y0, z1 - z0}))
            return refuse("the radius is larger than the body");

        plan.separable = edges.size() > 1;
        TopTools_IndexedMapOfShape selected;
        std::vector<TopoDS_Face> candidates;
        for (const TopoDS_Edge& edge : edges) {
            const auto frame = chudware::geom::edge_blend_frame(edge, edge_faces);
            if (!frame || !frame->concave || !*frame->concave)
                return refuse("run-off terminates inside-corner blends only");
            selected.Add(edge);
            for (TopTools_ListIteratorOfListOfShape f(edge_faces.FindFromKey(edge)); f.More();
                 f.Next()) {
                const TopoDS_Face face = TopoDS::Face(f.Value());
                const GeomAbs_SurfaceType type = BRepAdaptor_Surface(face).GetType();
                if (type != GeomAbs_Plane && type != GeomAbs_Cylinder) continue;
                if (std::none_of(candidates.begin(), candidates.end(),
                                 [&](const TopoDS_Face& c) { return c.IsSame(face); }))
                    candidates.push_back(face);
            }
        }
        const auto remember = [](std::vector<TopoDS_Face>& list, const TopoDS_Face& face) {
            if (std::none_of(list.begin(), list.end(),
                             [&](const TopoDS_Face& f) { return f.IsSame(face); }))
                list.push_back(face);
        };
        for (const TopoDS_Face& support : candidates) {
            const BRepAdaptor_Surface surface(support);
            const bool flat = surface.GetType() == GeomAbs_Plane;
            std::vector<TopoDS_Edge> border;  // selected edges on this support
            for (const TopoDS_Edge& edge : edges)
                for (TopTools_ListIteratorOfListOfShape f(edge_faces.FindFromKey(edge)); f.More();
                     f.Next())
                    if (f.Value().IsSame(support)) { border.push_back(edge); break; }
            bool open = false, ring_ok = flat, cap_ok = true, refused = false;
            std::vector<FilletRunoffCap> support_caps;
            for (TopExp_Explorer it(support, TopAbs_EDGE); it.More() && !refused; it.Next()) {
                const TopoDS_Edge rim = TopoDS::Edge(it.Current());
                if (selected.Contains(rim) || BRep_Tool::Degenerated(rim)) continue;
                if (!within_reach(rim, border, 2.0 * radius)) continue;
                if (!edge_faces.Contains(rim)) { refused = true; break; }
                TopoDS_Face wall;
                int distinct = 0;
                for (TopTools_ListIteratorOfListOfShape f(edge_faces.FindFromKey(rim)); f.More();
                     f.Next()) {
                    const TopoDS_Face face = TopoDS::Face(f.Value());
                    if (face.IsSame(support)) continue;
                    wall = face;
                    ++distinct;
                }
                if (distinct != 1) { refused = true; break; }
                const Rim verdict = classify_rim(rim, support, wall, edge_faces);
                if (verdict == Rim::ignored) continue;
                if (verdict == Rim::refused) { refused = true; continue; }
                open = true;
                const BRepAdaptor_Surface wall_surface(wall);
                const BRepAdaptor_Curve rim_curve(rim);
                gp_Vec wall_normal;
                if (!outward_normal(rim, wall, 0.5, wall_normal)) { refused = true; continue; }
                gp_Pnt mid;
                gp_Vec tangent;
                rim_curve.D1(0.5 * (rim_curve.FirstParameter() + rim_curve.LastParameter()), mid,
                             tangent);
                if (tangent.SquareMagnitude() < 1e-24) { refused = true; continue; }
                FilletRunoffCap cap{wall, wall_normal, mid};
                bool extendable = false;
                if (verdict == Rim::open) {
                    // A right-angled end: the support is a prism along the wall's normal,
                    // and the cap is cut back to the wall's own plane.
                    extendable = wall_surface.GetType() == GeomAbs_Plane &&
                        (flat || std::abs(gp_Vec(surface.Cylinder().Axis().Direction())
                                              .Dot(wall_normal)) > 1.0 - 1e-9);
                    if (extendable) cap.origin = wall_surface.Plane().Location();
                } else {
                    // A chamfered or rounded end of a planar support along a straight rim:
                    // sweep the end profile outward, square to the support, and cut back
                    // flush with the plane through the rim.
                    ring_ok = false;
                    const GeomAbs_SurfaceType kind = wall_surface.GetType();
                    const bool profile = kind == GeomAbs_Plane ||
                        (kind == GeomAbs_Cylinder &&
                         std::abs(gp_Vec(wall_surface.Cylinder().Axis().Direction())
                                      .Dot(tangent.Normalized())) > 1.0 - 1e-9);
                    if (flat && profile && rim_curve.GetType() == GeomAbs_Line &&
                        tangent.SquareMagnitude() > 1e-24) {
                        // The support's interior lies to the left of its wire, seen from
                        // outside: "away" is the in-plane direction square to the rim that
                        // leaves the support.
                        if (it.Current().Orientation() == TopAbs_REVERSED) tangent.Reverse();
                        gp_Vec away = plane_outward_normal(support).Crossed(tangent).Reversed();
                        if (away.SquareMagnitude() > 1e-24) {
                            away.Normalize();
                            cap.direction = away;
                            extendable = true;
                        }
                    }
                }
                if (extendable) {
                    const auto same = [&](const FilletRunoffCap& c) { return c.face.IsSame(wall); };
                    if (std::none_of(support_caps.begin(), support_caps.end(), same))
                        support_caps.push_back(cap);
                } else {
                    cap_ok = false;
                }
            }
            if (refused || !open) continue;
            if (ring_ok) remember(plan.rings, support);
            if (cap_ok)
                for (const FilletRunoffCap& cap : support_caps)
                    if (std::none_of(plan.caps.begin(), plan.caps.end(),
                                     [&](const FilletRunoffCap& c) { return c.face.IsSame(cap.face); }))
                        plan.caps.push_back(cap);
        }
        if (plan.rings.empty() && plan.caps.empty() && !plan.separable)
            plan.why = "no support face of the selected edges ends at an open edge";
    } catch (const Standard_Failure&) {
        return refuse("could not analyse the support faces");
    }
    return plan;
}

FilletRunoff fillet_runoff_extend(const TopoDS_Shape& shape,
                                  const std::vector<TopoDS_Edge>& edges,
                                  const FilletRunoffPlan& plan, double radius,
                                  FilletRunoffMode mode) {
    FilletRunoff out;
    const auto fail = [&out](const char* why) {
        out = FilletRunoff();
        out.why = why;
        return out;
    };
    try {
        if (!plan.applicable(mode) || mode == FilletRunoffMode::separate)
            return fail("run-off does not apply");
        Bnd_Box box;
        BRepBndLib::Add(shape, box);
        if (box.IsVoid() || box.IsWhole()) return fail("the body has no finite extent");
        double x0, y0, z0, x1, y1, z1;
        box.Get(x0, y0, z0, x1, y1, z1);
        const double diagonal = std::sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0) +
                                          (z1 - z0) * (z1 - z0));
        // The blend of an inside corner reaches about one radius onto a face; the extension
        // clears several, and the trim reaches past everything the blend can touch.
        const double pad = std::min(4.0 * radius, 2.0 * diagonal);
        const std::array<gp_Pnt, 8> corners{
            gp_Pnt(x0, y0, z0), gp_Pnt(x1, y0, z0), gp_Pnt(x0, y1, z0), gp_Pnt(x1, y1, z0),
            gp_Pnt(x0, y0, z1), gp_Pnt(x1, y0, z1), gp_Pnt(x0, y1, z1), gp_Pnt(x1, y1, z1)};

        // A square sheet in `plane`, centred over `around` and `margin` wider than it.
        const auto sheet_over = [&](const gp_Pln& plane, const TopoDS_Shape& around,
                                    double margin, gp_Pnt& center) -> TopoDS_Face {
            const gp_Dir axis = plane.Axis().Direction();
            Bnd_Box around_box;
            BRepBndLib::Add(around, around_box);
            double a0, b0, c0, a1, b1, c1;
            around_box.Get(a0, b0, c0, a1, b1, c1);
            const gp_Pnt mid(0.5 * (a0 + a1), 0.5 * (b0 + b1), 0.5 * (c0 + c1));
            const double half = 0.5 * std::sqrt((a1 - a0) * (a1 - a0) + (b1 - b0) * (b1 - b0) +
                                                (c1 - c0) * (c1 - c0)) + margin;
            const double off = gp_Vec(plane.Location(), mid).Dot(gp_Vec(axis));
            center = mid.Translated(gp_Vec(axis).Multiplied(-off));
            const gp_Pln square(gp_Ax3(center, axis, plane.XAxis().Direction()));
            return BRepBuilderAPI_MakeFace(square, -half, half, -half, half).Face();
        };

        TopoDS_Shape extended = shape;
        if (mode == FilletRunoffMode::ring) {
            // Extend each planar support in its own plane by a ring of material around its
            // footprint, as deep as the radius.
            const double depth = radius;
            for (const TopoDS_Face& support : plan.rings) {
                const gp_Vec normal = plane_outward_normal(support);
                gp_Pnt center;
                const TopoDS_Face sheet =
                    sheet_over(BRepAdaptor_Surface(support).Plane(), support, pad, center);
                const gp_Vec down = normal.Reversed().Multiplied(depth);
                const TopoDS_Shape slab = prism_solid(sheet, down);
                const TopoDS_Shape footprint = prism_solid(support, down);
                if (slab.IsNull() || footprint.IsNull())
                    return fail("could not sweep the support face");
                const TopoDS_Shape skirt = cut(slab, footprint);
                if (skirt.IsNull()) return fail("could not form the support skirt");
                extended = fuse(extended, skirt);
                if (extended.IsNull()) return fail("could not extend the support face");

                // The overflow is the material the skirt added and everything above it, up
                // past the body. Nothing else is trimmed: the blends of other supports, and
                // of the body's own features, stay where they are.
                const TopoDS_Shape added = cut(skirt, shape);
                if (added.IsNull()) return fail("could not isolate the added skirt");
                double height = 0.0;
                for (const gp_Pnt& corner : corners)
                    height = std::max(height, gp_Vec(center, corner).Dot(normal));
                const gp_Vec up = normal.Multiplied(height + 2.0 * radius);
                const gp_Pnt on_plane = BRepAdaptor_Surface(support).Plane().Location();
                std::vector<TopoDS_Shape> lifted;
                for (TopExp_Explorer f(added, TopAbs_FACE); f.More(); f.Next()) {
                    const TopoDS_Face top = TopoDS::Face(f.Current());
                    const BRepAdaptor_Surface top_surface(top);
                    if (top_surface.GetType() != GeomAbs_Plane) continue;
                    if (plane_outward_normal(top).Dot(normal) < 1.0 - 1e-9) continue;
                    if (std::abs(gp_Vec(on_plane, top_surface.Plane().Location()).Dot(normal)) > 1e-7)
                        continue;
                    const TopoDS_Shape column = prism_solid(top, up);
                    if (column.IsNull()) return fail("could not sweep the overflow region");
                    lifted.push_back(column);
                }
                const TopoDS_Shape overflow = fuse_many(added, lifted);
                if (overflow.IsNull()) return fail("could not form the overflow region");
                out.trim.push_back(overflow);
            }
        } else {
            // Extend the body by sweeping each cap face along its direction; the overflow
            // is everything beyond the plane through the support's rim that the body did
            // not have.
            for (const FilletRunoffCap& cap : plan.caps) {
                const TopoDS_Shape skirt = prism_solid(cap.face, cap.direction.Multiplied(pad));
                if (skirt.IsNull()) return fail("could not sweep the end face");
                extended = fuse(extended, skirt);
                if (extended.IsNull()) return fail("could not extend the support face");
                gp_Pnt center;
                const gp_Pln cut_plane(gp_Ax3(cap.origin, gp_Dir(cap.direction)));
                const TopoDS_Face sheet = sheet_over(cut_plane, cap.face, 2.0 * radius, center);
                const TopoDS_Shape field =
                    prism_solid(sheet, cap.direction.Multiplied(pad + 2.0 * radius));
                if (field.IsNull()) return fail("could not sweep the overflow region");
                const TopoDS_Shape overflow = cut(field, shape);
                if (overflow.IsNull()) return fail("could not form the overflow region");
                out.trim.push_back(overflow);
            }
        }
        out.extended = unify_same_domain(extended);
        if (out.extended.IsNull()) return fail("could not merge the extended support face");

        TopTools_IndexedMapOfShape body_edges;
        TopExp::MapShapes(out.extended, TopAbs_EDGE, body_edges);
        if (!image_edges(body_edges, edges, out.edges))
            return fail("a selected edge was lost when the support face was extended");
    } catch (const Standard_Failure&) {
        return fail("the kernel failed while extending the support face");
    }
    return out;
}

TopoDS_Shape fillet_runoff_trim(const FilletRunoff& runoff, const TopoDS_Shape& filleted,
                                std::string& why) {
    try {
        const TopoDS_Shape cut_shape = cut_many(filleted, runoff.trim);
        if (cut_shape.IsNull()) {
            why = "could not trim the overflow off the blend";
            return {};
        }
        const TopoDS_Shape trimmed = unify_same_domain(cut_shape);
        TopoDS_Shape solid;
        int solids = 0;
        for (TopExp_Explorer s(trimmed, TopAbs_SOLID); s.More(); s.Next()) {
            solid = s.Current();
            ++solids;
        }
        if (solids != 1) {
            why = "trimming the blend left " + std::to_string(solids) + " solids, not one";
            return {};
        }
        return solid;
    } catch (const Standard_Failure&) {
        why = "the kernel failed while trimming the blend";
        return {};
    }
}

}  // namespace geom
