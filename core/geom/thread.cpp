#include "geom/thread.h"

#include "geom/booleans.h"
#include "geom/curves.h"
#include "geom/features.h"  // sweep_profile

#include <algorithm>
#include <cmath>
#include <utility>
#include <vector>

#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <Standard_Failure.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax3.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

namespace {
constexpr double kPi = 3.14159265358979323846;

// A simplified, ROUNDED ISO-60° thread cross-section in a local (r, z) plane: r runs
// radially (the cut depth), z runs along the axis (the pitch direction). The profile
// is a short polygon — a 60° V whose crest and root are clipped by small flats (crest
// radius / root radius ~ pitch/6) — NOT a razor-sharp triangle. A hairline-sharp apex
// makes OCCT's swept-coil boolean fragile (degenerate edges, self-intersection); the
// clipped V keeps every edge well above tolerance so the helical CUT stays robust.
//
// `cut_depth` is the radial groove depth (major-minor)/2 plus a small overshoot so the
// V fully clears the cylinder wall on the cut. `pitch` sets the axial half-width. The
// profile is centred on (r=0, z=0); the document positions r=0 at the cylinder skin and
// the +r side INTO the material (external) or OUT through the wall (internal) via the
// frame the profile is transformed by. Returns the (r,z) polygon points (closed loop).
std::vector<std::pair<double, double>> iso_profile_pts(double pitch, double cut_depth) {
    // Axial half-width of the V at the cylinder skin (the open mouth of the groove).
    // For an ISO 60° thread the flank half-angle is 30°, so the groove half-width at a
    // radial depth d is d*tan(30°). Use the cut_depth to set the mouth width.
    const double tan30 = std::tan(kPi / 6.0);  // 0.57735
    // The mouth must extend slightly PAST the skin (a small +r overshoot) so the swept
    // tool crosses the cylinder boundary cleanly — a profile that only touches the skin
    // tangentially leaves a sliver or makes the boolean cut fail. r runs +over -> -depth.
    const double over = std::max(cut_depth * 0.10, pitch * 0.04);
    const double mouth_half = (cut_depth + over) * tan30;  // half-width at the mouth lip
    const double flat = pitch / 6.0;                       // crest/root clip flat
    const double root_half = std::max(flat * 0.5, mouth_half * 0.18);

    // Polygon walk: from the outer mouth lip (+over, past the skin), down one flank to the
    // clipped root (-cut_depth, into the material), back up the other flank. The document
    // frame orients +r INTO the material (external) or OUT through the wall (internal).
    std::vector<std::pair<double, double>> p;
    p.emplace_back(+over, +mouth_half);                    // mouth lip (past the skin), +z
    p.emplace_back(-cut_depth + flat * 0.5, +root_half);   // near root, +z
    p.emplace_back(-cut_depth, +root_half * 0.4);          // clipped root corner +z
    p.emplace_back(-cut_depth, -root_half * 0.4);          // clipped root corner -z
    p.emplace_back(-cut_depth + flat * 0.5, -root_half);   // near root, -z
    p.emplace_back(+over, -mouth_half);                    // mouth lip (past the skin), -z
    return p;
}

// Build the thread profile FACE in world space: a small closed polygon lying in the
// plane spanned by `radial` (the +r groove direction) and `axial` (+z pitch direction),
// centred at `origin` (a point ON the cylinder skin at the helix start). Null on failure.
TopoDS_Face build_profile_face(const gp_Pnt& origin, const gp_Dir& radial,
                               const gp_Dir& axial, double pitch, double cut_depth) {
    const std::vector<std::pair<double, double>> pts = iso_profile_pts(pitch, cut_depth);
    const gp_Vec vr(radial), vz(axial);
    BRepBuilderAPI_MakePolygon poly;
    for (const auto& rc : pts) {
        gp_Pnt q(origin.X() + vr.X() * rc.first + vz.X() * rc.second,
                 origin.Y() + vr.Y() * rc.first + vz.Y() * rc.second,
                 origin.Z() + vr.Z() * rc.first + vz.Z() * rc.second);
        poly.Add(q);
    }
    poly.Close();
    if (!poly.IsDone()) return TopoDS_Face();
    BRepBuilderAPI_MakeFace mkf(poly.Wire(), Standard_True);
    if (!mkf.IsDone()) return TopoDS_Face();
    return mkf.Face();
}

double solid_volume(const TopoDS_Shape& s) {
    if (s.IsNull()) return 0.0;
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}
}  // namespace

ThreadResult cut_thread(const TopoDS_Shape& body, const gp_Pnt& axis_loc,
                        const gp_Dir& axis_dir, double cyl_radius, double pitch,
                        double major_dia, double length, int starts, bool lefthand,
                        bool internal, double turns_cap) {
    ThreadResult res;
    res.shape = body;  // default: hand back the body UNCHANGED (the fall-back)
    if (body.IsNull()) {
        res.warning = "thread: empty body";
        return res;  // ok stays false — a truly unusable input
    }
    res.ok = true;  // we always at least return the body; ok=true means "feature ran"

    try {
        if (!(pitch > 1e-6) || !(major_dia > 1e-6) || !(length > 1e-6) ||
            !(cyl_radius > 1e-6)) {
            res.fell_back = true;
            res.warning = "thread: degenerate size (pitch/major/length/radius must be > 0)";
            return res;
        }
        if (starts < 1) starts = 1;

        // ISO geometry: minor ≈ major − 1.0825*pitch (the basic minor diameter). The
        // radial groove depth is (major − minor)/2; the pitch diameter is halfway between
        // and sets the thread's effective radius. NOTE: we build the helix at the SKIN
        // radius (cyl_radius) and anchor the profile AT the skin (its mouth) grooving to
        // the root — so the profile sits ON the helix start (the sweep's pierce rule) and
        // the swept tool's depth carries the cut to the minor radius. This is equivalent
        // to a pitch-diameter helix with a centred profile, but keeps the sweep robust.
        const double minor_dia = major_dia - 1.0825 * pitch;
        // Radial groove depth (clamped to a sane minimum for tiny custom pitches).
        const double cut_depth = std::max(0.5 * (major_dia - minor_dia), pitch * 0.10);

        // Turn cap: never sweep more turns than length/lead, and never more than the
        // hard cap (huge length / tiny pitch would otherwise sweep thousands of turns
        // and hang OCCT). +1 turn of run-out so the cut spans the full length.
        const double lead = pitch * static_cast<double>(starts);
        double turns = length / lead + 1.0;
        const double cap = (turns_cap > 0.0) ? turns_cap : 30.0;
        if (turns > cap) turns = cap;
        const double helix_height = std::min(length, turns * lead);

        // The local-frame helix winds about +Z from (pitch_radius, 0, 0). Lift it onto
        // the target cylinder's axis frame: +Z -> axis_dir, origin -> axis_loc, with an
        // arbitrary in-plane X. Lefthand flips the winding sense.
        gp_Ax3 frame(axis_loc, axis_dir);              // builds an orthonormal X,Y from axis_dir
        const gp_Dir fx = frame.XDirection();
        const gp_Dir fy = frame.YDirection();
        const gp_Dir fz = frame.Direction();
        gp_Trsf lift;
        lift.SetValues(fx.X(), fy.X(), fz.X(), axis_loc.X(),
                       fx.Y(), fy.Y(), fz.Y(), axis_loc.Y(),
                       fx.Z(), fy.Z(), fz.Z(), axis_loc.Z());

        // The thread profile sits at the helix START (angle 0): a point on the cylinder
        // SKIN (axis_loc + cyl_radius·fx, where fx is the OUTWARD radial direction), in the
        // plane spanned by the groove direction and the axial direction (fz). The profile's
        // +r is its OUTER MOUTH lip, -r its ROOT (see iso_profile_pts):
        //   EXTERNAL (shaft): the root cuts INWARD (toward the axis), the mouth lip sits
        //     just OUTSIDE the skin -> +r = OUTWARD = +fx.
        //   INTERNAL (bore):  the root cuts OUTWARD (into the wall), the mouth lip sits just
        //     INSIDE the bore (into the hole void) -> +r = INWARD = -fx.
        const gp_Vec vfx(fx);
        gp_Pnt skin(axis_loc.X() + vfx.X() * cyl_radius,
                    axis_loc.Y() + vfx.Y() * cyl_radius,
                    axis_loc.Z() + vfx.Z() * cyl_radius);
        const gp_Dir groove_dir = internal ? gp_Dir(-vfx.X(), -vfx.Y(), -vfx.Z()) : fx;

        // One coil per start, each phase-rotated by 360/starts about the axis. Fuse the
        // start coils into one tool, then CUT the tool out of the body.
        TopoDS_Shape tool;
        const double phase_step = 360.0 / static_cast<double>(starts);
        for (int s = 0; s < starts; ++s) {
            // make_helix builds at the cyl_radius (so the swept groove pierces the skin),
            // local +Z, height = helix_height. We sweep the profile (anchored at the skin)
            // along it; OCCT's CorrectedFrenet keeps the profile pierced and ⟂ the path.
            const double start_angle = phase_step * static_cast<double>(s);
            TopoDS_Wire hl = make_helix(lead, helix_height, cyl_radius,
                                        /*cone_angle*/ 0.0, start_angle, lefthand);
            if (hl.IsNull()) { res.fell_back = true; break; }
            TopoDS_Wire spine = TopoDS::Wire(
                BRepBuilderAPI_Transform(hl, lift, true).Shape());
            if (spine.IsNull()) { res.fell_back = true; break; }

            // The profile for THIS start sits where the helix start lands: rotate the skin
            // point + groove/axial dirs by start_angle about the axis (the local helix was
            // rotated the same amount). Build the profile in world space at that pose.
            gp_Trsf rot;
            rot.SetRotation(gp_Ax1(axis_loc, axis_dir), start_angle * kPi / 180.0);
            gp_Pnt p_skin = skin.Transformed(rot);
            gp_Dir p_groove = groove_dir.Transformed(rot);
            TopoDS_Face prof = build_profile_face(p_skin, p_groove, fz, pitch, cut_depth);
            if (prof.IsNull()) { res.fell_back = true; break; }

            TopoDS_Shape coil = sweep_profile(prof, spine, /*orientation*/ 0);
            if (coil.IsNull() || solid_volume(coil) <= 1e-9) { res.fell_back = true; break; }
            tool = tool.IsNull() ? coil : fuse(tool, coil);
            if (tool.IsNull()) { res.fell_back = true; break; }
        }
        if (res.fell_back || tool.IsNull()) {
            res.fell_back = true;
            res.warning = "thread: real geometry failed (OCCT) — reduce length/pitch or "
                          "use mode:cosmetic; left the body unthreaded";
            res.shape = body;
            return res;
        }

        const double vol_before = solid_volume(body);
        TopoDS_Shape cutres = cut(body, tool);
        if (cutres.IsNull() || solid_volume(cutres) <= 1e-9 ||
            !BRepCheck_Analyzer(cutres).IsValid() ||
            solid_volume(cutres) >= vol_before - 1e-9) {
            // Empty / invalid / no-material-removed -> graceful fall-back to the plain body.
            res.fell_back = true;
            res.warning = "thread: real geometry failed (OCCT) — the helical cut did not "
                          "produce a valid solid; reduce length/pitch or use mode:cosmetic; "
                          "left the body unthreaded";
            res.shape = body;
            return res;
        }
        res.shape = cutres;
        res.fell_back = false;
        return res;
    } catch (const Standard_Failure&) {
        res.fell_back = true;
        res.warning = "thread: real geometry failed (OCCT exception) — reduce length/pitch "
                      "or use mode:cosmetic; left the body unthreaded";
        res.shape = body;
        return res;
    } catch (...) {
        res.fell_back = true;
        res.warning = "thread: real geometry failed — left the body unthreaded";
        res.shape = body;
        return res;
    }
}

}  // namespace geom
