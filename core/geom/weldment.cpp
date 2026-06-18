// Weldment feature geometry — faithful C++ port of engine/doc/features/
// weldment.py (+ engine/doc/weld_profiles.py). See weldment.h for the contract.
//
// PARITY DISCIPLINE: every profile outline (vertex order, hollow bore), the
// section orientation perpendicular to the path, and the plate extrusion mirror
// the oracle EXACTLY so the resulting BRep volume matches the differential gate.
#include "geom/weldment.h"

#include "geom/booleans.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

#include <BRep_Tool.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepGProp.hxx>
#include <BRepOffsetAPI_MakePipe.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepTools.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax3.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

namespace {

constexpr double PI = 3.14159265358979323846;

inline double jnum(const nlohmann::json& params, const char* key, double dflt) {
    // Read a profile dimension. The host sends either a bare number or a dim
    // object {value,expr}; document.cpp has already resolved expr->value by the
    // time params land here for non-profile dims, but profile dims pass straight
    // through, so accept both shapes (mirrors weld_profiles.ProfileSpec.resolve,
    // which reads supplied[k] as a float).
    auto it = params.find(key);
    if (it == params.end() || it->is_null()) return dflt;
    if (it->is_number()) return it->get<double>();
    if (it->is_object()) {
        auto v = it->find("value");
        if (v != it->end() && v->is_number()) return v->get<double>();
    }
    return dflt;
}

// Closed polygon wire through (x,y) points in local XY (z=0). Mirrors
// weld_profiles._polyline_wire / _rect_wire.
TopoDS_Wire poly_wire(const std::vector<std::pair<double, double>>& pts) {
    BRepBuilderAPI_MakePolygon mk;
    for (const auto& p : pts) mk.Add(gp_Pnt(p.first, p.second, 0.0));
    mk.Close();
    return mk.Wire();
}

std::vector<std::pair<double, double>> rect_pts(double w, double h, double cx, double cy) {
    double hw = w / 2.0, hh = h / 2.0;
    return {{cx - hw, cy - hh}, {cx + hw, cy - hh}, {cx + hw, cy + hh}, {cx - hw, cy + hh}};
}

// Face from an outer wire minus inner (void) wires (weld_profiles._face_from).
TopoDS_Face face_from(const TopoDS_Wire& outer, const std::vector<TopoDS_Wire>& inners) {
    BRepBuilderAPI_MakeFace mf(outer, Standard_True);
    for (const TopoDS_Wire& w : inners) {
        TopoDS_Wire hole = w;
        hole.Reverse();  // a hole wire is added with opposite orientation
        mf.Add(hole);
    }
    return mf.Face();
}

TopoDS_Wire circle_wire(double r) {
    gp_Circ c(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), r);
    BRepBuilderAPI_MakeEdge me(c);
    BRepBuilderAPI_MakeWire mw(me.Edge());
    return mw.Wire();
}

// ---- per-profile builders (centred on the local origin) --------------------
// Each returns a Face and writes area + (w,h) envelope; returns false on a
// degenerate (oracle ValueError) dimension.

bool build_square_tube(const nlohmann::json& p, TopoDS_Face& f, double& area,
                       double& ew, double& eh) {
    double size = jnum(p, "size", 20.0), t = jnum(p, "thickness", 2.0);
    if (t <= 0 || t >= size / 2.0) return false;
    TopoDS_Wire outer = poly_wire(rect_pts(size, size, 0, 0));
    TopoDS_Wire inner = poly_wire(rect_pts(size - 2 * t, size - 2 * t, 0, 0));
    f = face_from(outer, {inner});
    area = size * size - (size - 2 * t) * (size - 2 * t);
    ew = size; eh = size;
    return true;
}

bool build_rect_tube(const nlohmann::json& p, TopoDS_Face& f, double& area,
                     double& ew, double& eh) {
    double w = jnum(p, "w", 40.0), h = jnum(p, "h", 20.0), t = jnum(p, "thickness", 2.0);
    if (t <= 0 || t >= std::min(w, h) / 2.0) return false;
    TopoDS_Wire outer = poly_wire(rect_pts(w, h, 0, 0));
    TopoDS_Wire inner = poly_wire(rect_pts(w - 2 * t, h - 2 * t, 0, 0));
    f = face_from(outer, {inner});
    area = w * h - (w - 2 * t) * (h - 2 * t);
    ew = w; eh = h;
    return true;
}

bool build_round_tube(const nlohmann::json& p, TopoDS_Face& f, double& area,
                      double& ew, double& eh) {
    double od = jnum(p, "od", 20.0), t = jnum(p, "thickness", 2.0);
    if (t <= 0 || t >= od / 2.0) return false;
    TopoDS_Wire outer = circle_wire(od / 2.0);
    TopoDS_Wire inner = circle_wire(od / 2.0 - t);
    f = face_from(outer, {inner});
    area = PI * ((od / 2.0) * (od / 2.0) - (od / 2.0 - t) * (od / 2.0 - t));
    ew = od; eh = od;
    return true;
}

bool build_angle_l(const nlohmann::json& p, TopoDS_Face& f, double& area,
                   double& ew, double& eh) {
    double leg = jnum(p, "leg", 30.0), t = jnum(p, "thickness", 3.0);
    if (t <= 0 || t >= leg) return false;
    std::vector<std::pair<double, double>> pts = {
        {0, 0}, {leg, 0}, {leg, t}, {t, t}, {t, leg}, {0, leg}};
    for (auto& q : pts) { q.first -= leg / 2.0; q.second -= leg / 2.0; }
    f = face_from(poly_wire(pts), {});
    area = 2 * leg * t - t * t;
    ew = leg; eh = leg;
    return true;
}

bool build_flat_bar(const nlohmann::json& p, TopoDS_Face& f, double& area,
                    double& ew, double& eh) {
    double w = jnum(p, "w", 40.0), t = jnum(p, "thickness", 6.0);
    if (w <= 0 || t <= 0) return false;
    f = face_from(poly_wire(rect_pts(w, t, 0, 0)), {});
    area = w * t;
    ew = w; eh = t;
    return true;
}

bool build_c_channel(const nlohmann::json& p, TopoDS_Face& f, double& area,
                     double& ew, double& eh) {
    double h = jnum(p, "h", 50.0), w = jnum(p, "w", 25.0), t = jnum(p, "thickness", 3.0);
    if (t <= 0 || t >= w || 2 * t >= h) return false;
    std::vector<std::pair<double, double>> pts = {
        {0, 0}, {w, 0}, {w, t}, {t, t}, {t, h - t}, {w, h - t}, {w, h}, {0, h}};
    for (auto& q : pts) { q.first -= w / 2.0; q.second -= h / 2.0; }
    f = face_from(poly_wire(pts), {});
    area = h * w - (h - 2 * t) * (w - t);
    ew = w; eh = h;
    return true;
}

bool build_i_beam(const nlohmann::json& p, TopoDS_Face& f, double& area,
                  double& ew, double& eh) {
    double h = jnum(p, "h", 100.0), w = jnum(p, "w", 50.0);
    double tw = jnum(p, "tw", 5.0), tf = jnum(p, "tf", 7.0);
    if (tw <= 0 || tf <= 0 || tw >= w || 2 * tf >= h) return false;
    double hw = w / 2.0, hh = h / 2.0, hwt = tw / 2.0, yf = hh - tf;
    std::vector<std::pair<double, double>> pts = {
        {-hw, -hh}, {hw, -hh}, {hw, -yf}, {hwt, -yf}, {hwt, yf}, {hw, yf},
        {hw, hh},   {-hw, hh}, {-hw, yf}, {-hwt, yf}, {-hwt, -yf}, {-hw, -yf}};
    f = face_from(poly_wire(pts), {});
    area = h * tw + 2.0 * tf * (w - tw);
    ew = w; eh = h;
    return true;
}

bool build_t_section(const nlohmann::json& p, TopoDS_Face& f, double& area,
                     double& ew, double& eh) {
    double h = jnum(p, "h", 60.0), w = jnum(p, "w", 60.0);
    double tw = jnum(p, "tw", 6.0), tf = jnum(p, "tf", 6.0);
    if (tw <= 0 || tf <= 0 || tw >= w || tf >= h) return false;
    double hw = w / 2.0, hh = h / 2.0, hwt = tw / 2.0, yf = hh - tf;
    std::vector<std::pair<double, double>> pts = {
        {-hwt, -hh}, {hwt, -hh}, {hwt, yf}, {hw, yf}, {hw, hh},
        {-hw, hh},   {-hw, yf},  {-hwt, yf}};
    f = face_from(poly_wire(pts), {});
    area = w * tf + tw * (h - tf);
    ew = w; eh = h;
    return true;
}

}  // namespace

bool weld_profile_known(const std::string& name) {
    return name == "square_tube" || name == "rect_tube" || name == "round_tube" ||
           name == "angle_L" || name == "flat_bar" || name == "c_channel" ||
           name == "i_beam" || name == "t_section";
}

TopoDS_Face weld_profile_face(const std::string& profile, const nlohmann::json& params,
                              const std::string& justify, double angle,
                              double& area_out, std::string& error,
                              double* env_w_out, double* env_h_out) {
    TopoDS_Face f;
    double area = 0.0, ew = 0.0, eh = 0.0;
    bool ok = false;
    if (profile == "square_tube")      ok = build_square_tube(params, f, area, ew, eh);
    else if (profile == "rect_tube")   ok = build_rect_tube(params, f, area, ew, eh);
    else if (profile == "round_tube")  ok = build_round_tube(params, f, area, ew, eh);
    else if (profile == "angle_L")     ok = build_angle_l(params, f, area, ew, eh);
    else if (profile == "flat_bar")    ok = build_flat_bar(params, f, area, ew, eh);
    else if (profile == "c_channel")   ok = build_c_channel(params, f, area, ew, eh);
    else if (profile == "i_beam")      ok = build_i_beam(params, f, area, ew, eh);
    else if (profile == "t_section")   ok = build_t_section(params, f, area, ew, eh);
    else { error = "unknown profile '" + profile + "'"; return TopoDS_Face(); }
    if (!ok) { error = "degenerate profile dimensions"; return TopoDS_Face(); }

    // Rotate about the section normal (+Z), then shift for justify — mirrors
    // weld_profiles.build_profile_face (Rot(0,0,angle) then Pos(w/2,h/2,0)).
    if (std::abs(angle) > 1e-9) {
        gp_Trsf rot;
        rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), angle * PI / 180.0);
        f = TopoDS::Face(BRepBuilderAPI_Transform(f, rot, true).Shape());
    }
    if (justify == "corner") {
        gp_Trsf tr;
        tr.SetTranslation(gp_Vec(ew / 2.0, eh / 2.0, 0.0));
        f = TopoDS::Face(BRepBuilderAPI_Transform(f, tr, true).Shape());
    }
    area_out = area;
    if (env_w_out) *env_w_out = ew;
    if (env_h_out) *env_h_out = eh;
    return f;
}

TopoDS_Shape weld_sweep_member(const TopoDS_Face& face_local,
                               const TopoDS_Edge& path_edge,
                               const Eigen::Vector3d& start,
                               const Eigen::Vector3d& tangent) {
    if (face_local.IsNull() || path_edge.IsNull()) return TopoDS_Shape();
    Eigen::Vector3d n = tangent;
    if (n.norm() < 1e-9) return TopoDS_Shape();
    n.normalize();
    // Profile plane: origin at the segment start, normal along the path tangent
    // (oracle Plane(origin=start, z_dir=n)). Map the local section's +Z onto n
    // and its origin onto `start` (the location transform build123d applies).
    gp_Ax2 ax(gp_Pnt(start.x(), start.y(), start.z()), gp_Dir(n.x(), n.y(), n.z()));
    // SetTransformation(toAx3, fromAx3) maps coords in `fromAx3` (= the global frame,
    // i.e. the local section's own coordinates) to where they land once that frame is
    // moved onto `toAx3` — i.e. it IS the local->world placement. The local section is
    // drawn in XY with +Z normal, so `from` is the default XY/Z frame at the origin.
    gp_Trsf to_world;
    to_world.SetTransformation(gp_Ax3(ax),
                               gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)));
    TopoDS_Shape placed = BRepBuilderAPI_Transform(face_local, to_world, true).Shape();
    TopoDS_Face face = TopoDS::Face(placed);

    BRepBuilderAPI_MakeWire mw(path_edge);
    if (!mw.IsDone()) return TopoDS_Shape();
    BRepOffsetAPI_MakePipe mk(mw.Wire(), face);
    mk.Build();
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

namespace {

// Volume of a shape (0 on null) — used by the trim's "would erase the member"
// guard, mirroring the oracle's getattr(result,'volume',0) <= 1e-9 check.
double shape_volume(const TopoDS_Shape& s) {
    if (s.IsNull()) return 0.0;
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}

// A large box whose near face passes through `point` with outward `normal`: the
// SOLID on the +normal side. Subtracting it removes everything past the plane.
// Mirrors weldment.py::_half_space (make_box(2r,2r,2r).translate(-r,-r,0) placed
// so its -Z face is on the plane and the body extends +n). Built directly in the
// plane frame (corner at origin - u*r - v*r, axes u,v,n) — identical span:
// u,v∈[-r,r], n∈[0,2r].
TopoDS_Shape half_space_solid(const Eigen::Vector3d& point,
                              const Eigen::Vector3d& normal, double reach) {
    Eigen::Vector3d n = normal.normalized();
    // Pick any unit u ⟂ n, then v = n × u (orthonormal frame; box is symmetric in
    // u,v so the exact choice is irrelevant to the cut result).
    Eigen::Vector3d ref = std::abs(n.dot(Eigen::Vector3d(1, 0, 0))) > 0.9
                              ? Eigen::Vector3d(0, 1, 0)
                              : Eigen::Vector3d(1, 0, 0);
    Eigen::Vector3d u = (ref - n * ref.dot(n)).normalized();
    Eigen::Vector3d v = n.cross(u);
    Eigen::Vector3d corner = point - u * reach - v * reach;
    gp_Ax2 frame(gp_Pnt(corner.x(), corner.y(), corner.z()),
                 gp_Dir(n.x(), n.y(), n.z()), gp_Dir(u.x(), u.y(), u.z()));
    return BRepPrimAPI_MakeBox(frame, 2 * reach, 2 * reach, 2 * reach).Shape();
}

// Subtract the half-space (point, outward-normal) from `solid`. On any hiccup, or
// if the cut would erase the member, return `solid` unchanged (oracle
// _subtract_halfspace: trim is best-effort, never fails the feature).
TopoDS_Shape subtract_halfspace(const TopoDS_Shape& solid, const Eigen::Vector3d& point,
                                const Eigen::Vector3d& outward, double reach) {
    TopoDS_Shape hs = half_space_solid(point, outward, reach);
    if (hs.IsNull()) return solid;
    TopoDS_Shape result = ::geom::cut(solid, hs);
    if (result.IsNull() || shape_volume(result) <= 1e-9) return solid;
    return result;
}

inline bool near_pt(const Eigen::Vector3d& a, const Eigen::Vector3d& b) {
    return (a - b).norm() < 1e-4;
}

// If members i and j share an endpoint, return true and the (vertex, outgoing
// tangent into i, outgoing tangent into j). Mirrors _shared_vertex_tangents.
bool shared_vertex_tangents(const WeldMember& mi, const WeldMember& mj,
                            Eigen::Vector3d& vtx, Eigen::Vector3d& ti, Eigen::Vector3d& tj) {
    struct Cand { Eigen::Vector3d vi, tin_i, vj, tin_j; };
    const Cand cands[4] = {
        {mi.start, mi.t_start, mj.start, mj.t_start},
        {mi.start, mi.t_start, mj.end, -mj.t_end},
        {mi.end, -mi.t_end, mj.start, mj.t_start},
        {mi.end, -mi.t_end, mj.end, -mj.t_end},
    };
    for (const Cand& c : cands) {
        if (near_pt(c.vi, c.vj)) {
            vtx = c.vi; ti = c.tin_i; tj = c.tin_j;
            return true;
        }
    }
    return false;
}

// Is `pt` on member m's straight axis interior (foot strictly between ends)?
// Mirrors _point_on_axis (perp<1e-4, 0.02<t<0.98).
bool point_on_axis_interior(const WeldMember& m, const Eigen::Vector3d& pt) {
    Eigen::Vector3d ab = m.end - m.start;
    double L = ab.norm();
    if (L < 1e-9) return false;
    double t = std::max(0.0, std::min(1.0, (pt - m.start).dot(ab) / (L * L)));
    Eigen::Vector3d foot = m.start + ab * t;
    double perp = (pt - foot).norm();
    return perp < 1e-4 && (0.02 < t && t < 0.98);
}

// Distance from the through member's axis to its outer surface along `approach`
// (oracle _through_half_extent: conservative half-diagonal of the envelope).
double through_half_extent(const WeldMember& through, const Eigen::Vector3d& approach) {
    double w = through.env_w, h = through.env_h;
    if (w <= 0.0 && h <= 0.0) return 0.0;
    Eigen::Vector3d taxis = through.end - through.start;
    if (taxis.norm() < 1e-9) return std::max(w, h) / 2.0;
    taxis.normalize();
    Eigen::Vector3d perp = approach - taxis * approach.dot(taxis);
    if (perp.norm() < 1e-9) return std::max(w, h) / 2.0;
    return 0.5 * std::hypot(w, h);
}

// --- miter (mutual bevel at a shared vertex), oracle _mitre_trim ---
void mitre_trim(std::vector<WeldMember>& members, double reach) {
    constexpr double EPS = 1e-6;
    const std::size_t n = members.size();
    for (std::size_t i = 0; i < n; ++i) {
        if (!members[i].straight) continue;
        for (std::size_t j = i + 1; j < n; ++j) {
            if (!members[j].straight) continue;
            Eigen::Vector3d shared, ti, tj;
            if (!shared_vertex_tangents(members[i], members[j], shared, ti, tj)) continue;
            ti.normalize();
            tj.normalize();
            double cosang = std::max(-1.0, std::min(1.0, ti.dot(tj)));
            double ang = std::acos(cosang) * 180.0 / PI;
            if (ang > 179.0 || ang < 1.0) continue;  // near-collinear / overlapping
            Eigen::Vector3d bis = ti + tj;
            if (bis.norm() < 1e-9) continue;
            // Remove the side toward the OTHER member's tangent from each.
            Eigen::Vector3d ni = tj - ti;  // points from i's side toward j's side
            Eigen::Vector3d nj = ti - tj;
            if (ni.norm() > EPS)
                members[i].solid = subtract_halfspace(members[i].solid, shared, ni, reach);
            if (nj.norm() > EPS)
                members[j].solid = subtract_halfspace(members[j].solid, shared, nj, reach);
        }
    }
}

// Classify a joint: returns true and fills (through_idx, trimmed_idx, joint).
// Mirrors _through_trimmed (T-joint: endpoint on other's interior; else shared
// vertex with the longer member running through).
bool through_trimmed(const std::vector<WeldMember>& members, std::size_t i, std::size_t j,
                     std::size_t& through, std::size_t& trimmed, Eigen::Vector3d& joint) {
    const WeldMember& mi = members[i];
    const WeldMember& mj = members[j];
    // End-to-mid: does one member's endpoint land on the OTHER's interior span?
    const std::pair<std::size_t, std::size_t> pairs[2] = {{i, j}, {j, i}};
    for (auto [ei, si] : pairs) {
        const WeldMember& ender = members[ei];
        const WeldMember& spanner = members[si];
        for (const Eigen::Vector3d& ep : {ender.start, ender.end}) {
            if (point_on_axis_interior(spanner, ep)) {
                through = si; trimmed = ei; joint = ep;
                return true;
            }
        }
    }
    // Shared endpoint: the longer member runs through, the shorter is trimmed.
    Eigen::Vector3d shared, ti, tj;
    if (shared_vertex_tangents(mi, mj, shared, ti, tj)) {
        if (mi.length >= mj.length) { through = i; trimmed = j; }
        else { through = j; trimmed = i; }
        joint = shared;
        return true;
    }
    return false;
}

// Square-cut the trimmed member against the through member's near face
// (oracle _butt_cut).
TopoDS_Shape butt_cut(const WeldMember& trimmed, const WeldMember& through,
                      const Eigen::Vector3d& joint, double reach) {
    Eigen::Vector3d axis = trimmed.end - trimmed.start;
    if (axis.norm() < 1e-9) return trimmed.solid;
    axis.normalize();
    Eigen::Vector3d far_end =
        (trimmed.end - joint).norm() < (trimmed.start - joint).norm() ? trimmed.start
                                                                      : trimmed.end;
    Eigen::Vector3d into = joint - far_end;
    if (into.dot(axis) < 0) axis = -axis;
    double half = through_half_extent(through, axis);
    Eigen::Vector3d cut_at = joint - axis * half;
    return subtract_halfspace(trimmed.solid, cut_at, axis, reach);
}

// Cope (fish-mouth): boolean-subtract the through member's ORIGINAL solid
// (oracle _cope_cut, keeping the largest piece on a split).
TopoDS_Shape cope_cut(const WeldMember& trimmed, const TopoDS_Shape& through_solid) {
    TopoDS_Shape result = ::geom::cut(trimmed.solid, through_solid);
    if (result.IsNull()) return trimmed.solid;
    // A cut can split the member; keep the largest solid piece.
    std::vector<TopoDS_Shape> solids;
    for (TopExp_Explorer ex(result, TopAbs_SOLID); ex.More(); ex.Next())
        solids.push_back(ex.Current());
    if (solids.size() > 1) {
        TopoDS_Shape best = solids.front();
        double bestv = shape_volume(best);
        for (const TopoDS_Shape& s : solids) {
            double v = shape_volume(s);
            if (v > bestv) { bestv = v; best = s; }
        }
        result = best;
    }
    if (shape_volume(result) <= 1e-9) return trimmed.solid;
    return result;
}

// --- butt / cope, oracle _butt_cope_trim ---
void butt_cope_trim(std::vector<WeldMember>& members, double reach, const std::string& mode) {
    constexpr double EPS = 1e-6;
    // Snapshot original solids so cope subtraction is independent of trim order.
    std::vector<TopoDS_Shape> orig;
    orig.reserve(members.size());
    for (const WeldMember& m : members) orig.push_back(m.solid);

    const std::size_t n = members.size();
    for (std::size_t i = 0; i < n; ++i) {
        if (!members[i].straight) continue;
        for (std::size_t j = i + 1; j < n; ++j) {
            if (!members[j].straight) continue;
            std::size_t through_i, trimmed_i;
            Eigen::Vector3d joint;
            if (!through_trimmed(members, i, j, through_i, trimmed_i, joint)) continue;
            Eigen::Vector3d ta = members[through_i].end - members[through_i].start;
            Eigen::Vector3d tb = members[trimmed_i].end - members[trimmed_i].start;
            if (ta.norm() < EPS || tb.norm() < EPS) continue;
            double cosang = std::abs(ta.normalized().dot(tb.normalized()));
            if (cosang > 0.999) continue;  // near-collinear
            if (mode == "cope")
                members[trimmed_i].solid = cope_cut(members[trimmed_i], orig[through_i]);
            else
                members[trimmed_i].solid =
                    butt_cut(members[trimmed_i], members[through_i], joint, reach);
        }
    }
}

}  // namespace

void weld_corner_trim(std::vector<WeldMember>& members, double reach,
                      const std::string& joint) {
    if (members.size() < 2) return;
    if (joint == "miter")
        mitre_trim(members, reach);
    else
        butt_cope_trim(members, reach, joint);
}

Eigen::Vector3d weld_closest_point_between_lines(const Eigen::Vector3d& p1,
                                                 const Eigen::Vector3d& d1,
                                                 const Eigen::Vector3d& p2,
                                                 const Eigen::Vector3d& d2) {
    double a = d1.dot(d1), b = d1.dot(d2), c = d2.dot(d2);
    Eigen::Vector3d w0 = p1 - p2;
    double d = d1.dot(w0), e = d2.dot(w0);
    double denom = a * c - b * b;
    if (std::abs(denom) < 1e-12) return p1;
    double s = (b * e - c * d) / denom;
    double t = (a * e - b * d) / denom;
    Eigen::Vector3d q1 = p1 + d1 * s;
    Eigen::Vector3d q2 = p2 + d2 * t;
    return (q1 + q2) * 0.5;
}

Eigen::Vector3d weld_longest_edge_dir(const TopoDS_Face& f, double& len_out) {
    Eigen::Vector3d best(1, 0, 0);
    double best_len = 0.0;
    for (TopExp_Explorer ex(f, TopAbs_EDGE); ex.More(); ex.Next()) {
        TopoDS_Edge e = TopoDS::Edge(ex.Current());
        TopoDS_Vertex a, b;
        TopExp::Vertices(e, a, b);
        if (a.IsNull() || b.IsNull()) continue;
        gp_Pnt pa = BRep_Tool::Pnt(a), pb = BRep_Tool::Pnt(b);
        Eigen::Vector3d d(pb.X() - pa.X(), pb.Y() - pa.Y(), pb.Z() - pa.Z());
        double ln = d.norm();
        if (ln > best_len) { best_len = ln; best = d / ln; }
    }
    len_out = best_len;
    return best;
}

TopoDS_Shape weld_make_solid_plate(const std::vector<Eigen::Vector3d>& corners,
                                   const Eigen::Vector3d& normal, double thickness,
                                   double offset) {
    if (corners.size() < 3) return TopoDS_Shape();
    // Drop consecutive duplicates + a closing duplicate (oracle cleanup, tol 1e-7).
    std::vector<Eigen::Vector3d> cleaned;
    for (const Eigen::Vector3d& v : corners)
        if (cleaned.empty() || (v - cleaned.back()).norm() > 1e-7) cleaned.push_back(v);
    if (cleaned.size() >= 2 && (cleaned.front() - cleaned.back()).norm() <= 1e-7)
        cleaned.pop_back();
    if (cleaned.size() < 3) return TopoDS_Shape();

    Eigen::Vector3d n = normal.normalized();
    double base = -thickness / 2.0 + offset;
    std::vector<Eigen::Vector3d> moved;
    for (const Eigen::Vector3d& v : cleaned) moved.push_back(v + n * base);

    BRepBuilderAPI_MakeWire mw;
    const std::size_t m = moved.size();
    for (std::size_t i = 0; i < m; ++i) {
        const Eigen::Vector3d& a = moved[i];
        const Eigen::Vector3d& b = moved[(i + 1) % m];
        BRepBuilderAPI_MakeEdge me(gp_Pnt(a.x(), a.y(), a.z()), gp_Pnt(b.x(), b.y(), b.z()));
        if (!me.IsDone()) return TopoDS_Shape();
        mw.Add(me.Edge());
    }
    if (!mw.IsDone()) return TopoDS_Shape();
    BRepBuilderAPI_MakeFace mf(mw.Wire(), Standard_True);
    if (!mf.IsDone()) return TopoDS_Shape();
    gp_Vec ext(n.x() * thickness, n.y() * thickness, n.z() * thickness);
    BRepPrimAPI_MakePrism mk(mf.Face(), ext);
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

namespace {

// Extrude a face plate: translate by base_shift along n, then prism by thickness
// (oracle _extrude_face_plate).
TopoDS_Shape extrude_face_plate(const TopoDS_Face& face, const Eigen::Vector3d& n,
                                double thickness, double base_shift) {
    TopoDS_Shape moved = face;
    if (std::abs(base_shift) > 1e-12) {
        gp_Trsf tr;
        tr.SetTranslation(gp_Vec(n.x() * base_shift, n.y() * base_shift, n.z() * base_shift));
        moved = BRepBuilderAPI_Transform(face, tr, true).Shape();
    }
    gp_Vec ext(n.x() * thickness, n.y() * thickness, n.z() * thickness);
    BRepPrimAPI_MakePrism mk(moved, ext);
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

}  // namespace

TopoDS_Shape weld_end_cap_plate(const TopoDS_Face& end_face, const Eigen::Vector3d& n_in,
                                const std::string& cap_type, double thickness,
                                double offset, double overhang, std::string& error) {
    if (end_face.IsNull()) { error = "null end face"; return TopoDS_Shape(); }
    Eigen::Vector3d n = n_in.normalized();

    TopoDS_Wire outer = BRepTools::OuterWire(end_face);
    // Collect inner wires (wires != outer).
    std::vector<TopoDS_Wire> inner_wires;
    for (TopExp_Explorer wx(end_face, TopAbs_WIRE); wx.More(); wx.Next()) {
        TopoDS_Wire w = TopoDS::Wire(wx.Current());
        if (!w.IsSame(outer)) inner_wires.push_back(w);
    }

    if (cap_type == "outer") {
        // NOTE: overhang (face offset) is not ported — fixtures use overhang=0 so
        // the plate is exactly the OUTER boundary covering the whole end.
        if (overhang > 1e-9) { error = "end_cap overhang not supported in C++ port"; return TopoDS_Shape(); }
        BRepBuilderAPI_MakeFace mf(outer, Standard_True);
        if (!mf.IsDone()) { error = "outer cap face build failed"; return TopoDS_Shape(); }
        double base_shift = offset;
        return extrude_face_plate(mf.Face(), n, thickness, base_shift);
    }
    // inner: plate matching the bore.
    if (inner_wires.empty()) {
        error = "type 'inner' needs a hollow member (no inner boundary)";
        return TopoDS_Shape();
    }
    BRepBuilderAPI_MakeFace mf(inner_wires.front(), Standard_True);
    if (!mf.IsDone()) { error = "inner cap face build failed"; return TopoDS_Shape(); }
    double base_shift = -thickness - offset;
    return extrude_face_plate(mf.Face(), n, thickness, base_shift);
}

}  // namespace geom
