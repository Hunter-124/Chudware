#include "geom/thread.h"

#include "geom/booleans.h"
#include "geom/curves.h"
#include "geom/features.h"  // sweep_profile

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <utility>
#include <vector>

#include <BRepAdaptor_CompCurve.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepBndLib.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <Bnd_Box.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <ShapeFix_Shape.hxx>
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

// Diagnostic trace, gated on $CHUDWARE_THREAD_DEBUG (dev-only; silent in production).
inline void tt(const char* msg, double a = 0, double b = 0) {
    static const bool on = std::getenv("CHUDWARE_THREAD_DEBUG") != nullptr;
    if (on) std::fprintf(stderr, "[thread] %s a=%.4f b=%.4f\n", msg, a, b);
}

// Helical booleans are OCCT's most fragile: a many-turn coil in near-tangent contact
// with the shaft skin routinely makes a plain BRepAlgoAPI_Cut/Fuse return IsDone()==false
// (an empty result) even though both operands are valid solids. A FUZZY value (a tolerance
// that lets the BOP treat near-coincident faces as coincident) fixes this. We run the
// boolean with an increasing fuzzy ladder and take the first result that is a valid solid
// whose volume moved the right way. Returns a null shape if every attempt fails.
double solid_volume(const TopoDS_Shape& s);  // fwd — defined just below

TopoDS_Shape helical_bool(const TopoDS_Shape& body, const TopoDS_Shape& tool, bool raised,
                          double base_len, double vol_before) {
    // 0 = exact; then ~1e-4..2e-2 of the feature size — enough to bridge the hairline
    // helical contact without smearing real geometry. We accept the FIRST result that is a
    // valid solid AND whose volume actually moved the right way: a fuzzy BOP can return a
    // valid-but-UNCHANGED body when it silently drops the intersection, so requiring the
    // volume to move forces escalation to a larger fuzzy value instead of a no-op success.
    const double fuzz[] = {0.0, base_len * 5e-4, base_len * 5e-3};
    // The move must be a MEANINGFUL fraction of the tool volume, not a rounding wobble.
    // A fuzzy BOP on a fine, many-turn coil can silently drop the intersection and hand
    // back a valid-but-essentially-UNCHANGED body; volume noise (~1e-3 on a big solid)
    // then sneaks past a 1e-6 gate, so the ladder accepts a no-op instead of escalating.
    // Requiring |Δvol| > 2% of the tool volume rejects that no-op and forces a larger
    // fuzzy value (which does engage) — while any genuine thread moves far more than 2%.
    const double min_move = std::max(1e-4, solid_volume(tool) * 0.02);
    for (double f : fuzz) {
        try {
            TopoDS_Shape r;
            if (raised) {
                BRepAlgoAPI_Fuse op(body, tool);
                op.SetRunParallel(Standard_True);   // OCCT BOP is single-threaded by default
                if (f > 0.0) op.SetFuzzyValue(f);
                op.Build();
                if (op.IsDone()) r = op.Shape();
            } else {
                BRepAlgoAPI_Cut op(body, tool);
                op.SetRunParallel(Standard_True);
                if (f > 0.0) op.SetFuzzyValue(f);
                op.Build();
                if (op.IsDone()) r = op.Shape();
            }
            if (r.IsNull() || !BRepCheck_Analyzer(r).IsValid()) continue;
            const double v = solid_volume(r);
            const bool moved = raised ? (v > vol_before + min_move)
                                      : (v < vol_before - min_move);
            if (moved) { tt("helical_bool ok at fuzzy", f, v); return r; }
        } catch (const Standard_Failure&) { /* try the next fuzzy value */ }
    }
    return TopoDS_Shape();
}

double solid_volume(const TopoDS_Shape& s) {
    if (s.IsNull()) return 0.0;
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}

// Axial coordinate of a point measured along `dir` from `origin` (a signed distance).
inline double axial_of(const gp_Pnt& p, const gp_Pnt& origin, const gp_Dir& dir) {
    return gp_Vec(origin, p).Dot(gp_Vec(dir));
}

// The body's axial extent [lo, hi] along the thread axis (signed distances from
// axis_loc). Used to CLAMP the thread band so it can never float past the material —
// a `length` longer than the shaft (or that would run into a shoulder/bolt head the
// caller has bounded via `length`) is trimmed back to the solid it actually threads.
std::pair<double, double> body_axial_extent(const TopoDS_Shape& body,
                                            const gp_Pnt& axis_loc, const gp_Dir& axis_dir) {
    Bnd_Box bb;
    BRepBndLib::Add(body, bb);
    if (bb.IsVoid()) return {0.0, 0.0};
    double xm, ym, zm, xM, yM, zM;
    bb.Get(xm, ym, zm, xM, yM, zM);
    double lo = 1e300, hi = -1e300;
    for (int i = 0; i < 8; ++i) {
        gp_Pnt c((i & 1) ? xM : xm, (i & 2) ? yM : ym, (i & 4) ? zM : zm);
        const double a = axial_of(c, axis_loc, axis_dir);
        lo = std::min(lo, a); hi = std::max(hi, a);
    }
    return {lo, hi};
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
        return common(coil, slab);
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
        // Radial groove depth depends on the thread FORM: a 60° V engages 0.5413·P (basic
        // minor Ø d1 = d − 1.0825·P); trapezoidal/square power threads (ACME/TRAP/SQUARE)
        // are a full half-pitch deep (0.5·P). Clamp to a sane minimum for tiny custom pitches.
        const double v_depth = 0.5 * (major_dia - minor_dia);            // 0.5413·P
        const double form_depth = (form == ThreadForm::V60) ? v_depth : 0.5 * pitch;
        const double cut_depth = std::max(form_depth, pitch * 0.10);

        // LEAD-IN / RUN-OUT: a real thread must run OFF the ends — the first and last
        // turns are partial, ramping in from beyond the end plane rather than appearing
        // abruptly at full depth. So we start the helix ONE LEAD before the requested run
        // and extend ONE LEAD past it, then anchor the sweep below `axis_loc`. For a CUT
        // the overhang is harmlessly clipped by the body's end faces (giving a clean
        // thread start that runs off the shaft); for a RAISED ridge it forms the natural
        // partial run-out threads. Without this the thread cannot actually be started.
        // LEAD-IN / RUN-OUT: a real thread must run OFF both ends so it can actually be
        // started — the helix runs PAST each end edge by at least one pitch (>= one full
        // lead for a multi-start), SYMMETRICALLY at both ends. The overhang past the sharp
        // rim gives the incomplete first/last threads a nut engages on. The swept groove
        // that crosses a 90° end rim can leave a helical knife-edge that trips OCCT's
        // validity check; we heal the cut (ShapeFix) below and, only if that still fails,
        // fall back — so the thread runs off the ends as asked whenever the kernel allows.
        const double lead = pitch * static_cast<double>(starts);
        const double ext = std::max(pitch, lead);   // >= 1 pitch of run-past, each end
        double turns = length / lead + 2.0 * (ext / lead) + 0.5;  // run-past both ends
        // Cap turns: a very high turn count (fine pitch on a long/large shaft) is where the
        // helical boolean gets both pathologically slow AND crash-prone. Bound it so the
        // op stays responsive and degrades gracefully rather than hanging or faulting.
        const double cap = (turns_cap > 0.0) ? turns_cap : 24.0;
        if (turns > cap) turns = cap;
        const double helix_height = std::min(length + 2.0 * ext, turns * lead);
        // Drop the anchor one `ext` below the requested start so the helix begins below the
        // start edge; the symmetric top overhang comes from the extra 2*ext of height.
        {
            gp_Vec back(axis_dir);
            axis_loc = axis_loc.Translated(back.Multiplied(-ext));
        }

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
            if (hl.IsNull()) { tt("helix null"); res.fell_back = true; break; }
            TopoDS_Wire spine = TopoDS::Wire(
                BRepBuilderAPI_Transform(hl, lift, true).Shape());
            if (spine.IsNull()) { tt("spine null"); res.fell_back = true; break; }

            // The profile for THIS start sits where the helix start lands: rotate the skin
            // point + groove/axial dirs by start_angle about the axis (the local helix was
            // rotated the same amount). Build the profile in world space at that pose.
            gp_Trsf rot;
            rot.SetRotation(gp_Ax1(axis_loc, axis_dir), start_angle * kPi / 180.0);
            gp_Pnt p_skin = skin.Transformed(rot);
            gp_Dir p_groove = groove_dir.Transformed(rot);

            // The profile plane must be NORMAL to the helix at its start — a helix tangent
            // is tilted off the pure axial direction by the lead angle α = atan(lead/2πR),
            // so building the profile in the (radial, axis) plane skews the swept groove
            // (badly for a coarse/steep or multi-start thread). Read the ACTUAL tangent at
            // the spine's first point and span the profile by (radial, tangent×radial):
            // radial is already ⟂ the helix tangent, so this plane is exactly ⟂ the path.
            gp_Dir prof_axial = fz;  // fallback: pure axial (shallow-thread approximation)
            try {
                BRepAdaptor_CompCurve cc(spine);
                gp_Pnt sp; gp_Vec tan;
                cc.D1(cc.FirstParameter(), sp, tan);
                if (tan.Magnitude() > 1e-9) {
                    gp_Dir tdir(tan);
                    gp_Vec pax = gp_Vec(tdir).Crossed(gp_Vec(p_groove));  // ⟂ tangent & radial
                    if (pax.Magnitude() > 1e-9) prof_axial = gp_Dir(pax);
                }
            } catch (const Standard_Failure&) { /* keep the axial fallback */ }

            TopoDS_Face prof = build_profile_face(p_skin, p_groove, prof_axial, pitch, cut_depth, raised, form);
            if (prof.IsNull()) { tt("profile null"); res.fell_back = true; break; }

            // orientation 1 = TRUE FRENET. A helix's Frenet normal is EXACTLY radial-inward
            // everywhere (r'' points straight at the axis), so a Frenet sweep keeps the profile
            // facing the axis — rotating one turn per turn WITH the radial, never inverting.
            // CorrectedFrenet (0) strips out precisely that torsion-driven rotation, leaving the
            // section fixed while the real radial sweeps 360°/turn past it -> the profile goes
            // "fully inverted at some points" (the reported bug). The start pose built above
            // (radial, tangent×radial) already spans the Frenet section plane, so law + start agree.
            TopoDS_Shape coil = sweep_profile(prof, spine, /*orientation=Frenet*/ 1);
            tt("coil vol", solid_volume(coil));
            tt("coil valid?", coil.IsNull() ? -1 : (BRepCheck_Analyzer(coil).IsValid() ? 1 : 0));
            if (coil.IsNull() || solid_volume(coil) <= 1e-9) { tt("coil empty"); res.fell_back = true; break; }
            tool = tool.IsNull() ? coil : fuse(tool, coil);
            if (tool.IsNull()) { tt("fuse null"); res.fell_back = true; break; }
        }
        if (res.fell_back || tool.IsNull()) {
            res.fell_back = true;
            res.warning = "thread: real geometry failed (OCCT) — reduce length/pitch or "
                          "use mode:cosmetic; left the body unthreaded";
            res.shape = body;
            return res;
        }

        // TRIM THE OVEREXTENDED COIL TO THE REQUESTED RUN, BEFORE the boolean. The coil
        // runs `ext` past each end (robust BOP + clean lead-in); we now flush-cut it to a
        // slab [z_lo, z_hi] along the axis so the thread starts exactly at the selected
        // face and stops exactly at the run end (a shoulder / bolt head the caller bounds
        // via `length`). Measured from `axis_loc` (the dropped anchor), the requested
        // start sits at +ext and the run ends at +ext+length. We also CLAMP that band to
        // the body's own axial extent so a `length` past the material can't leave threads
        // floating in air. Trimming first is what makes the raised ridge finish flush and
        // kills the end-rim knife-edge that trips the un-trimmed cut on steep leads.
        {
            const auto ext_lohi = body_axial_extent(body, axis_loc, axis_dir);
            double z_lo = std::max(ext, ext_lohi.first);
            double z_hi = std::min(ext + length, ext_lohi.second);
            const double half = 2.0 * (cyl_radius + pitch + cut_depth);
            TopoDS_Shape trimmed = trim_coil_to_band(tool, axis_loc, fx, fy, fz, half, z_lo, z_hi);
            tt("trim band z_lo/z_hi", z_lo, z_hi);
            tt("trimmed vol", solid_volume(trimmed));
            // Only accept the trim if it left a real solid; if the intersection came back
            // empty/invalid, fall through to the untrimmed tool (the body's own end faces
            // still bound a CUT) rather than losing the whole thread.
            if (!trimmed.IsNull() && solid_volume(trimmed) > 1e-9 &&
                BRepCheck_Analyzer(trimmed).IsValid())
                tool = trimmed;
            else
                tt("trim rejected — using untrimmed tool", 0, 0);
        }

        const double vol_before = solid_volume(body);
        // RAISED threads FUSE the helical ridge on (material UP); CUT threads subtract the
        // groove (material DOWN). helical_bool runs a fuzzy-tolerance ladder so the fragile
        // many-turn helical BOP survives instead of returning an empty shape.
        // helical_bool already guarantees a BRep-valid result whose volume moved the right
        // way (or a null shape). No post-heal step — ShapeFix on a degenerate fine-pitch
        // coil was a crash source, and the fastener-scope cases don't need it.
        TopoDS_Shape opres = helical_bool(body, tool, raised, major_dia, vol_before);
        const bool vol_ok = raised ? (solid_volume(opres) > vol_before + 1e-9)
                                   : (solid_volume(opres) < vol_before - 1e-9);
        tt("boolean result: before/after vol", vol_before, solid_volume(opres));
        tt("boolean valid?", opres.IsNull() ? -1 : (BRepCheck_Analyzer(opres).IsValid() ? 1 : 0), vol_ok ? 1 : 0);
        if (opres.IsNull() || solid_volume(opres) <= 1e-9 ||
            !BRepCheck_Analyzer(opres).IsValid() || !vol_ok) {
            // Empty / invalid / no-material-changed -> graceful fall-back to the plain body.
            res.fell_back = true;
            res.warning = raised
                ? "thread: real geometry failed (OCCT) — the helical ridge did not fuse to "
                  "a valid solid; reduce length/pitch or use mode:cosmetic; body unthreaded"
                : "thread: real geometry failed (OCCT) — the helical cut did not produce a "
                  "valid solid; reduce length/pitch or use mode:cosmetic; body unthreaded";
            res.shape = body;
            return res;
        }
        res.shape = opres;
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
