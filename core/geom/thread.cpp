#include "geom/thread.h"

#include "geom/curves.h"
#include "geom/features.h"  // sweep_profile

#include <algorithm>
#include <cmath>
#include <exception>
#include <limits>
#include <utility>
#include <vector>

#include <BRepAdaptor_CompCurve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRep_Tool.hxx>
#include <BOPAlgo_Alerts.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepBndLib.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <Bnd_Box.hxx>
#include <BRepBuilderAPI_Copy.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <Message_ListOfAlert.hxx>
#include <Message_Report.hxx>
#include <Precision.hxx>
#include <ShapeExtend_Status.hxx>
#include <ShapeUpgrade_ShapeDivideClosed.hxx>
#include <Standard_Failure.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Vertex.hxx>
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

// A real ISO 68-1 / ASME B1.1 60° thread cross-section in a local (r, z) plane: r runs
// radially, z runs along the axis (the pitch direction). This is a proper TRUNCATED-V
// with exact 30° flanks (60° included angle), crest and root FLATS sized from the pitch —
// NOT a razor-sharp triangle (a hairline apex makes OCCT's swept-coil boolean fragile).
//
// The flat truncations come straight from the standard fundamental triangle
// (H = P·cos30° = 0.866·P): the tool depth for a full thread is H1 = 0.625·H = 0.5413·P,
// which is exactly (major − minor)/2 for the basic minor Ø d1 = d − 1.0825·P. With crest
// flat P/8 and root flat P/4 the two flanks come out at EXACTLY 30° from the radial, so
// the swept coil is a geometrically correct 60° thread, not an approximation.
//
// r=0 is the cylinder skin; the document frame orients +r along `groove_dir`.
//   CUT  (raised=false): a V-GROOVE removed between the crests. The mouth sits at/just
//        past the skin (crest, wide — P·7/8 open) and the root is at −cut_depth (narrow,
//        the P/4 root flat). Cutting it leaves the thread crests at the skin.
//   RAISED (raised=true): a RIDGE added onto the skin. The base sits at/just below the
//        skin (wide, the thread flanks meet the root) and the crest is at +cut_depth
//        (narrow, the P/8 crest flat). Fusing it grows a helical thread outward — the
//        selected cylinder is treated as the ROOT (minor) Ø; the ridge builds to major.
//
// A small `over` overshoot pushes the skin-side edge just past r=0 so the swept tool
// crosses the cylinder boundary cleanly (a profile only tangent to the skin leaves a
// sliver / fails the boolean). Returns the (r,z) polygon points (closed loop).
std::vector<std::pair<double, double>> iso_profile_pts(double pitch, double cut_depth,
                                                       bool raised, ThreadForm form) {
    // Per-FORM flank angle (measured from the radial groove axis) + crest/root flats. The
    // construction below is identical for every form; only these three numbers change.
    //   V60  ISO/UN 60° V   : 30°  flanks, crest flat P/8, root flat P/4.
    //   ACME 29° trapezoid  : 14.5° flanks, crest flat 0.3707·P (root ≈ same).
    //   TRAP metric 30°     : 15°  flanks, crest flat 0.366·P.
    //   SQUARE              : 0°   flanks (straight walls), flats P/2.
    double flank_deg, crest_flat, root_flat;
    switch (form) {
        case ThreadForm::ACME:   flank_deg = 14.5; crest_flat = 0.3707 * pitch; root_flat = 0.3707 * pitch; break;
        case ThreadForm::TRAP:   flank_deg = 15.0; crest_flat = 0.366 * pitch;  root_flat = 0.366 * pitch;  break;
        case ThreadForm::SQUARE: flank_deg = 0.0;  crest_flat = 0.5 * pitch;    root_flat = 0.5 * pitch;    break;
        case ThreadForm::V60:
        default:                 flank_deg = 30.0;  crest_flat = pitch / 8.0;    root_flat = pitch / 4.0;    break;
    }
    const double ftan = std::tan(flank_deg * kPi / 180.0);

    // The skin-side edge must clearly PROTRUDE past the cylinder surface, not graze it: a
    // tool whose mouth only touches the skin tangentially leaves a helical face-face contact
    // band that makes OCCT's many-turn boolean fail. We add a straight RECTANGULAR lip that
    // pokes `over` past the skin at CONSTANT width — the exact flanks run from the root up to
    // the skin, then the lip juts straight out — so the contact becomes a clean transversal
    // intersection WITHOUT widening the groove past the pitch (no self-overlap).
    const double over = std::max(cut_depth * 0.35, pitch * 0.15);

    if (!raised) {
        // GROOVE: root flat at r=−cut_depth; flanks up to the skin (r=0) at half-width W0;
        // then a straight lip out to r=+over at the same width W0.
        const double root_half = root_flat * 0.5;
        const double w0 = root_half + cut_depth * ftan;              // half-width at the skin
        std::vector<std::pair<double, double>> p;
        p.emplace_back(+over, +w0);                // lip outer, protruding past skin (+z)
        p.emplace_back(0.0, +w0);                  // at the skin (+z)
        p.emplace_back(-cut_depth, +root_half);    // root flat corner (+z)
        p.emplace_back(-cut_depth, -root_half);    // root flat corner (−z)
        p.emplace_back(0.0, -w0);                  // at the skin (−z)
        p.emplace_back(+over, -w0);                // lip outer, protruding past skin (−z)
        return p;
    }
    // RIDGE: crest flat at r=+cut_depth; flanks down to the skin (r=0) at half-width W0;
    // then a straight lip INTO the body to r=−over at the same width W0 so the ridge welds
    // to the shaft along a clean transversal contact.
    const double crest_half = crest_flat * 0.5;
    const double w0 = crest_half + cut_depth * ftan;               // half-width at the skin
    std::vector<std::pair<double, double>> p;
    p.emplace_back(-over, +w0);                    // lip, into the body past the skin (+z)
    p.emplace_back(0.0, +w0);                      // at the skin (+z)
    p.emplace_back(+cut_depth, +crest_half);       // crest flat corner (+z)
    p.emplace_back(+cut_depth, -crest_half);       // crest flat corner (−z)
    p.emplace_back(0.0, -w0);                      // at the skin (−z)
    p.emplace_back(-over, -w0);                    // lip, into the body past the skin (−z)
    return p;
}

// Build the thread profile FACE in world space: a small closed polygon lying in the
// plane spanned by `radial` (the +r groove direction) and `axial` (+z pitch direction),
// centred at `origin` (a point ON the cylinder skin at the helix start). Null on failure.
TopoDS_Face build_profile_face(const gp_Pnt& origin, const gp_Dir& radial,
                               const gp_Dir& axial, double pitch, double cut_depth,
                               bool raised, ThreadForm form) {
    const std::vector<std::pair<double, double>> pts = iso_profile_pts(pitch, cut_depth, raised, form);
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

int solid_count(const TopoDS_Shape& s) {
    int count = 0;
    for (TopExp_Explorer ex(s, TopAbs_SOLID); ex.More(); ex.Next()) ++count;
    return count;
}

template <typename Operation>
void build_thread_boolean(Operation& op, const TopoDS_Shape& body,
                          const TopTools_ListOfShape& tools, double fuzzy) {
    TopTools_ListOfShape args;
    args.Append(body);
    op.SetArguments(args);
    op.SetTools(tools);
    op.SetNonDestructive(Standard_True);
    op.SetRunParallel(Standard_True);
    if (fuzzy > 0.0) op.SetFuzzyValue(fuzzy);
    op.Build();
}

template <typename Operation>
TopoDS_Shape thread_boolean(const TopoDS_Shape& body, const TopTools_ListOfShape& tools,
                            double fuzzy) {
    Operation op;
    build_thread_boolean(op, body, tools, fuzzy);
    // OCCT can report IsDone and a BRep-valid but incomplete result together
    // with self-intersection / unused-face warnings. Such a result is not safe
    // evidence for a completed cut, fuse, trim, or coverage check.
    return op.IsDone() && !op.HasErrors() && !op.HasWarnings()
               ? op.Shape() : TopoDS_Shape();
}

enum class ResidualStatus { Clear, PeriodicSeamWarning, Failed };

template <typename Operation>
ResidualStatus thread_residual(const TopoDS_Shape& body, const TopTools_ListOfShape& tools) {
    Operation op;
    build_thread_boolean(op, body, tools, 0.0);
    if (!op.IsDone() || op.HasErrors()) return ResidualStatus::Failed;
    const TopoDS_Shape residual = op.Shape();
    if (residual.IsNull() || !BRepCheck_Analyzer(residual).IsValid() ||
        TopExp_Explorer(residual, TopAbs_SOLID).More())
        return ResidualStatus::Failed;
    if (!op.HasWarnings()) return ResidualStatus::Clear;

    // This warning alone can arise from an unclassified internal edge of a
    // periodic seam. It is NOT accepted as proof: it only permits the exact
    // closed-face division below, followed by a fresh warning-free residual.
    const Message_ListOfAlert& alerts = op.GetReport()->GetAlerts(Message_Warning);
    if (alerts.IsEmpty()) return ResidualStatus::Failed;
    for (Message_ListOfAlert::Iterator it(alerts); it.More(); it.Next()) {
        if (it.Value().IsNull() ||
            !it.Value()->IsKind(STANDARD_TYPE(BOPAlgo_AlertFaceBuilderUnusedEdges)))
            return ResidualStatus::Failed;
    }
    return ResidualStatus::PeriodicSeamWarning;
}

bool has_closed_periodic_face(const TopoDS_Shape& shape) {
    for (TopExp_Explorer fs(shape, TopAbs_FACE); fs.More(); fs.Next()) {
        const TopoDS_Face face = TopoDS::Face(fs.Current());
        const BRepAdaptor_Surface surface(face, Standard_False);
        if (!surface.IsUPeriodic() && !surface.IsVPeriodic()) continue;
        for (TopExp_Explorer es(face, TopAbs_EDGE); es.More(); es.Next())
            if (BRep_Tool::IsClosed(TopoDS::Edge(es.Current()), face)) return true;
    }
    return false;
}

double max_shape_tolerance(const TopoDS_Shape& shape) {
    double result = Precision::Confusion();
    for (TopExp_Explorer fs(shape, TopAbs_FACE); fs.More(); fs.Next())
        result = std::max(result, BRep_Tool::Tolerance(TopoDS::Face(fs.Current())));
    for (TopExp_Explorer es(shape, TopAbs_EDGE); es.More(); es.Next())
        result = std::max(result, BRep_Tool::Tolerance(TopoDS::Edge(es.Current())));
    for (TopExp_Explorer vs(shape, TopAbs_VERTEX); vs.More(); vs.Next())
        result = std::max(result, BRep_Tool::Tolerance(TopoDS::Vertex(vs.Current())));
    return result;
}

// Divide periodic faces on a deep copy solely for the coverage proof. OCCT's
// non-segmenting mode creates trimmed patches of the same underlying surfaces;
// it must neither increase topology tolerances nor change the returned body.
TopoDS_Shape split_periodic_faces_for_proof(const TopoDS_Shape& candidate) {
    if (!has_closed_periodic_face(candidate)) return TopoDS_Shape();
    const double tolerance = max_shape_tolerance(candidate);
    const double before = solid_volume(candidate);
    BRepBuilderAPI_Copy copy(candidate, Standard_True, Standard_False);
    if (!copy.IsDone() || copy.Shape().IsNull()) return TopoDS_Shape();
    ShapeUpgrade_ShapeDivideClosed divide(copy.Shape());
    divide.SetNbSplitPoints(1);
    divide.SetSurfaceSegmentMode(Standard_False);
    divide.SetMaxTolerance(tolerance);
    if (!divide.Perform() || divide.Status(ShapeExtend_FAIL) ||
        !divide.Status(ShapeExtend_DONE2))
        return TopoDS_Shape();
    TopoDS_Shape split = divide.Result();
    if (split.IsNull() || solid_count(split) != solid_count(candidate) ||
        !BRepCheck_Analyzer(split).IsValid())
        return TopoDS_Shape();
    const double tolerance_slack =
        std::numeric_limits<double>::epsilon() * std::max(1.0, tolerance);
    const double after = solid_volume(split);
    if (max_shape_tolerance(split) > tolerance + tolerance_slack ||
        !std::isfinite(before) || !std::isfinite(after) ||
        std::abs(after - before) > std::max(1e-9, std::abs(before) * 1e-6))
        return TopoDS_Shape();
    return split;
}

// A volume change alone does not prove that every start and axial section was
// applied. For cuts each tool's intersection with the result must have no
// solid; for ridges each tool must be contained in the result. A warned
// residual is never accepted. A known seam-only warning permits one isolated
// closed-face division, after which the affected and remaining checks must all
// be warning-free on that equivalent copy.
bool tools_fully_applied(const TopoDS_Shape& candidate, const TopTools_ListOfShape& tools,
                         bool raised) {
    auto check = [&](const TopoDS_Shape& subject, const TopoDS_Shape& tool) {
        TopTools_ListOfShape operand;
        if (raised) {
            operand.Append(subject);
            return thread_residual<BRepAlgoAPI_Cut>(tool, operand);
        }
        operand.Append(tool);
        return thread_residual<BRepAlgoAPI_Common>(subject, operand);
    };
    TopoDS_Shape subject = candidate;
    bool divided = false;
    for (TopTools_ListIteratorOfListOfShape it(tools); it.More(); it.Next()) {
        ResidualStatus status = check(subject, it.Value());
        if (status == ResidualStatus::PeriodicSeamWarning && !divided) {
            subject = split_periodic_faces_for_proof(candidate);
            if (subject.IsNull()) return false;
            divided = true;
            status = check(subject, it.Value());
        }
        if (status != ResidualStatus::Clear) return false;
    }
    return true;
}

// The body's axial extent [lo, hi] along the thread axis (signed distances from
// axis_loc). Used to CLAMP the thread band so it can never float past the material —
// a `length` longer than the shaft (or that would run into a shoulder/bolt head the
// caller has bounded via `length`) is trimmed back to the solid it actually threads.
std::pair<double, double> body_axial_extent(const TopoDS_Shape& body,
                                            const gp_Pnt& axis_loc, const gp_Dir& axis_dir) {
    // A world-space AABB projected onto an oblique axis can greatly overstate
    // the run. Measure the bounds in the thread frame instead. A rigid
    // transformation with copy=false only relocates the shape, preserving the
    // cached input. Ignore mesh deflection so bounds do not depend on prior
    // tessellation.
    gp_Trsf to_axis;
    to_axis.SetTransformation(gp_Ax3(axis_loc, axis_dir));
    const TopoDS_Shape local = BRepBuilderAPI_Transform(body, to_axis, false).Shape();
    Bnd_Box bb;
    BRepBndLib::AddOptimal(local, bb, Standard_False, Standard_False);
    if (bb.IsVoid()) return {0.0, 0.0};
    double xm, ym, zm, xM, yM, zM;
    bb.Get(xm, ym, zm, xM, yM, zM);
    return {zm, zM};
}

// TRIM the overextended coil to a clean axial SLAB [z_lo, z_hi] measured along the
// axis from `axis_loc`. The coil is built running >=1 pitch PAST each end (so the
// helical BOP is robust and the trim planes slice through FULL-DEPTH thread, not the
// helix's own start/end caps); intersecting it with a generous box slab flush-cuts
// both thread ends exactly at the selected face plane / requested run. This is what
// gives a real lead-in (the first thread ends flush at the face, ready to start a nut)
// AND stops the thread cleanly at z_hi (a shoulder / bolt-head bound the caller passes
// via `length`), for BOTH cut and raised. Returns null if the intersection is empty.
TopoDS_Shape trim_coil_to_band(const TopoDS_Shape& coil, const gp_Pnt& axis_loc,
                               const gp_Dir& xdir, const gp_Dir& ydir, const gp_Dir& zdir,
                               double half, double z_lo, double z_hi) {
    if (coil.IsNull() || !(z_hi > z_lo)) return TopoDS_Shape();
    // Corner of the slab: the band's low face, backed off `half` in both radial dirs.
    const gp_Vec vx(xdir), vy(ydir), vz(zdir);
    const gp_Vec off = vx.Multiplied(-half) + vy.Multiplied(-half) + vz.Multiplied(z_lo);
    gp_Pnt corner(axis_loc.X() + off.X(), axis_loc.Y() + off.Y(), axis_loc.Z() + off.Z());
    try {
        gp_Ax2 boxframe(corner, zdir, xdir);            // Z=axis, X=radial; Y derived
        TopoDS_Shape slab =
            BRepPrimAPI_MakeBox(boxframe, 2.0 * half, 2.0 * half, z_hi - z_lo).Shape();
        if (slab.IsNull()) return TopoDS_Shape();
        TopTools_ListOfShape tools;
        tools.Append(slab);
        return thread_boolean<BRepAlgoAPI_Common>(coil, tools, 0.0);
    } catch (const Standard_Failure&) {
        return TopoDS_Shape();
    }
}
}  // namespace

ThreadResult cut_thread(const TopoDS_Shape& body, const gp_Pnt& axis_loc_in,
                        const gp_Dir& axis_dir, double cyl_radius, double pitch,
                        double major_dia, double length, int starts, bool lefthand,
                        bool internal, bool raised, double turns_cap, ThreadForm form) {
    gp_Pnt axis_loc = axis_loc_in;
    ThreadResult res;
    res.shape = body;  // retained for recovery only; ok remains false on failure
    auto fail = [&](const std::string& reason) {
        res.fell_back = true;
        res.warning = reason;
        return res;
    };
    if (body.IsNull()) return fail("thread: empty body");

    try {
        if (!std::isfinite(pitch) || !std::isfinite(major_dia) ||
            !std::isfinite(length) || !std::isfinite(cyl_radius) ||
            !std::isfinite(turns_cap) || !(pitch > 1e-6) || !(major_dia > 1e-6) ||
            !(length > 1e-6) || !(cyl_radius > 1e-6)) {
            return fail("thread: pitch, major diameter, length and radius must be finite and positive");
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
        // Radial groove depth depends on the thread FORM: a 60° V engages 0.5413·P (basic
        // minor Ø d1 = d − 1.0825·P); trapezoidal/square power threads (ACME/TRAP/SQUARE)
        // are a full half-pitch deep (0.5·P). Clamp to a sane minimum for tiny custom pitches.
        const double v_depth = 0.5 * (major_dia - minor_dia);            // 0.5413·P
        const double form_depth = (form == ThreadForm::V60) ? v_depth : 0.5 * pitch;
        const double cut_depth = std::max(form_depth, pitch * 0.10);

        // Bound the requested run to the body's actual axial extent *before*
        // determining the work limit. An over-long request can still cover the
        // entire body within the cap; truncating a longer helix to the cap and
        // calling that partial geometry a success would lose requested threads.
        const auto body_extent = body_axial_extent(body, axis_loc_in, axis_dir);
        const double run_lo = std::max(0.0, body_extent.first);
        const double run_hi = std::min(length, body_extent.second);
        if (!(run_hi > run_lo + 1e-6))
            return fail("thread: requested run does not intersect the body");
        const double run_length = run_hi - run_lo;
        const double lead = pitch * static_cast<double>(starts);
        const double ext = lead;  // one complete lead beyond each end
        const double required_turns = run_length / lead + 2.0;
        const double cap = (turns_cap > 0.0) ? turns_cap : 24.0;
        if (!std::isfinite(required_turns) || required_turns > cap) {
            return fail("thread: complete run requires " + std::to_string(required_turns) +
                        " turns including end extensions, exceeding the cap of " +
                        std::to_string(cap));
        }
        if (!std::isfinite(run_length + 2.0 * ext))
            return fail("thread: helical run is too large");
        axis_loc = axis_loc.Translated(gp_Vec(axis_dir).Multiplied(run_lo - ext));

        // A single very long swept B-spline can make OCCT's cylinder/helix BOP
        // self-intersect. Build bounded eight-turn sections instead. Each
        // boundary is a whole lead, so the start phase (including handedness
        // and multi-start offsets) is identical on both sides of the boundary.
        // Trim each overextended sweep to its own band and submit all bands to
        // one boolean, preserving the complete requested run.
        constexpr double kTurnsPerSection = 8.0;
        constexpr int kMaxSections = 128;
        double section_ratio = (run_length / lead) / kTurnsPerSection;
        const double nearest = std::round(section_ratio);
        if (nearest >= 1.0 &&
            std::abs(section_ratio - nearest) <=
                8.0 * std::numeric_limits<double>::epsilon() * std::max(1.0, section_ratio))
            section_ratio = nearest;
        const double section_count_value = std::ceil(section_ratio);
        if (!std::isfinite(section_count_value) || section_count_value < 1.0 ||
            section_count_value > static_cast<double>(kMaxSections) / starts)
            return fail("thread: complete run exceeds the helical section work limit");
        const int section_count = static_cast<int>(section_count_value);
        const double section_span = section_count == 1 ? run_length : kTurnsPerSection * lead;
        const gp_Ax3 frame(axis_loc, axis_dir);
        const gp_Dir fx = frame.XDirection();
        const gp_Dir fy = frame.YDirection();
        const gp_Dir fz = frame.Direction();
        const gp_Vec vfx(fx), vaxis(axis_dir);
        const gp_Dir groove_dir = internal ? gp_Dir(-vfx.X(), -vfx.Y(), -vfx.Z()) : fx;
        const double phase_step = 360.0 / static_cast<double>(starts);
        const double half = 2.0 * (cyl_radius + pitch + cut_depth);
        TopTools_ListOfShape tools;
        double vol_tool = 0.0;

        for (int section = 0; section < section_count; ++section) {
            const double section_lo = section * section_span;
            const double section_hi = section + 1 == section_count
                                        ? run_length : (section + 1) * section_span;
            const double section_length = section_hi - section_lo;
            const gp_Pnt anchor = axis_loc.Translated(vaxis.Multiplied(section_lo));
            gp_Trsf lift;
            lift.SetValues(fx.X(), fy.X(), fz.X(), anchor.X(),
                           fx.Y(), fy.Y(), fz.Y(), anchor.Y(),
                           fx.Z(), fy.Z(), fz.Z(), anchor.Z());
            const gp_Pnt skin = anchor.Translated(vfx.Multiplied(cyl_radius));

            for (int s = 0; s < starts; ++s) {
                const double start_angle = phase_step * static_cast<double>(s);
                TopoDS_Wire hl = make_helix(lead, section_length + 2.0 * ext, cyl_radius,
                                            /*cone_angle*/ 0.0, start_angle, lefthand);
                if (hl.IsNull()) return fail("thread: could not construct the helix");
                TopoDS_Wire spine = TopoDS::Wire(
                    BRepBuilderAPI_Transform(hl, lift, true).Shape());
                if (spine.IsNull()) return fail("thread: could not transform the helix");

                gp_Trsf rot;
                rot.SetRotation(gp_Ax1(anchor, axis_dir), start_angle * kPi / 180.0);
                const gp_Pnt p_skin = skin.Transformed(rot);
                const gp_Dir p_groove = groove_dir.Transformed(rot);
                BRepAdaptor_CompCurve cc(spine);
                gp_Pnt sp;
                gp_Vec tan;
                cc.D1(cc.FirstParameter(), sp, tan);
                if (tan.Magnitude() <= 1e-9)
                    return fail("thread: helix has a degenerate start tangent");
                const gp_Vec pax = gp_Vec(gp_Dir(tan)).Crossed(gp_Vec(p_groove));
                if (pax.Magnitude() <= 1e-9)
                    return fail("thread: could not orient the thread profile");
                const gp_Dir prof_axial(pax);
                const TopoDS_Face prof = build_profile_face(
                    p_skin, p_groove, prof_axial, pitch, cut_depth, raised, form);
                if (prof.IsNull()) return fail("thread: could not construct the thread profile");

                // True Frenet keeps the profile radial along the helical spine.
                const TopoDS_Shape coil = sweep_profile(prof, spine, /*Frenet*/ 1);
                if (coil.IsNull() || !TopExp_Explorer(coil, TopAbs_SOLID).More() ||
                    !BRepCheck_Analyzer(coil).IsValid())
                    return fail("thread: helical profile sweep did not produce a valid solid");
                TopoDS_Shape band = trim_coil_to_band(
                    coil, anchor, fx, fy, fz, half, ext, ext + section_length);
                if (band.IsNull() || !TopExp_Explorer(band, TopAbs_SOLID).More() ||
                    !BRepCheck_Analyzer(band).IsValid())
                    return fail("thread: trimming the helical tool did not produce a valid solid");
                const double section_volume = solid_volume(band);
                if (!std::isfinite(section_volume) || section_volume <= 1e-9)
                    return fail("thread: trimmed helical tool is empty");
                vol_tool += section_volume;
                tools.Append(band);
            }
        }
        const double vol_before = solid_volume(body);
        const int solids_before = solid_count(body);
        if (!std::isfinite(vol_before) || vol_before <= 1e-9 || solids_before == 0)
            return fail("thread: input body is not a nonempty solid");
        if (!std::isfinite(vol_tool) || vol_tool <= 1e-9)
            return fail("thread: trimmed helical tool is empty");

        // First try an exact, deferred, non-destructive BOP. On some OCCT builds
        // intersecting a valid helix with a cylinder yields an empty compound
        // even though IsDone and BRepCheck both report success. Only on an
        // unsuccessful/invalid result, use the existing bounded fuzzy values.
        // The smallest ISO crest is pitch/8; limiting fuzzy tolerance to 1/16 of
        // that feature prevents a large diameter or fine pitch from merging it.
        const double fuzzy_limit = pitch / 128.0;
        const double fuzz[] = {0.0, std::min(major_dia * 5e-4, fuzzy_limit),
                               std::min(major_dia * 5e-3, fuzzy_limit)};
        // Keep the existing meaningful-change guard: a small volume-integration
        // wobble on an unchanged body is not evidence of a real groove or ridge.
        const double min_move = std::max(1e-4, vol_tool * 0.02);
        const double max_move = vol_tool + std::max(1e-6, vol_tool * 1e-6);
        std::string reason = "thread: helical boolean did not produce a valid, material-changing solid";
        double previous = -1.0;
        for (const double f : fuzz) {
            if (f == previous) continue;
            previous = f;
            TopoDS_Shape candidate;
            try {
                candidate = raised ? thread_boolean<BRepAlgoAPI_Fuse>(body, tools, f)
                                   : thread_boolean<BRepAlgoAPI_Cut>(body, tools, f);
            } catch (const Standard_Failure& e) {
                const char* message = e.GetMessageString();
                reason = message && *message ? std::string("thread: OCCT boolean: ") + message
                                             : "thread: OCCT helical boolean failed";
                continue;
            }
            if (candidate.IsNull()) {
                reason = "thread: helical boolean failed or reported unsafe kernel warnings";
                continue;
            }
            const int solids_after = solid_count(candidate);
            if (solids_after == 0) {
                reason = "thread: helical boolean produced no solid";
                continue;
            }
            if (raised && solids_after > solids_before) {
                reason = "thread: helical ridge was not attached to the body";
                continue;
            }
            if (!BRepCheck_Analyzer(candidate).IsValid()) {
                reason = "thread: helical boolean produced an invalid solid";
                continue;
            }
            const double after = solid_volume(candidate);
            const double moved = raised ? after - vol_before : vol_before - after;
            if (!std::isfinite(after) || after <= 1e-9) {
                reason = "thread: helical boolean produced an empty or non-finite volume";
                continue;
            }
            if (moved <= min_move) {
                reason = raised ? "thread: helical ridge did not add material"
                                : "thread: helical cut did not remove material";
                continue;
            }
            // A boolean cannot add or remove more material than the tool contains.
            if (moved > max_move) {
                reason = "thread: helical boolean changed more material than the tool";
                continue;
            }
            try {
                if (!tools_fully_applied(candidate, tools, raised)) {
                    reason = "thread: helical boolean could not verify every thread section";
                    continue;
                }
            } catch (const Standard_Failure&) {
                reason = "thread: could not verify every thread section";
                continue;
            }
            res.shape = candidate;
            res.ok = true;
            return res;
        }
        return fail(reason);
    } catch (const Standard_Failure& e) {
        const char* message = e.GetMessageString();
        return fail(message && *message ? std::string("thread: OCCT exception: ") + message
                                       : "thread: OCCT exception during helical construction");
    } catch (const std::exception& e) {
        return fail(std::string("thread: helical construction failed: ") + e.what());
    } catch (...) {
        return fail("thread: unknown error during helical construction");
    }
}

}  // namespace geom
