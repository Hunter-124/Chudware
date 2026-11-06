// Thread feature geometry: a helical thread cut or raised ridge on a cylinder.
//
// The document compiler resolves the target face and thread size. This builder
// sweeps a thread-form profile around the face's axis and subtracts it from or
// fuses it to the body. External threads follow a shaft, internal threads a bore.
//
// This self-contained shape -> shape builder receives the resolved axis and size
// from the document compiler. A failed real cut or ridge is reported as an error;
// it must never be mistaken for a successfully threaded, unchanged body.
//
// ROBUSTNESS CONTRACT: a successful result is a valid solid with changed volume.
// Invalid input, incomplete runs, empty/invalid sweeps or booleans, and kernel
// exceptions return ok=false with a diagnostic. The original body is retained in
// shape for recovery only; it is not a successful substitute for real geometry.
// Cosmetic threads are a separate, explicit document operation.
#pragma once

#include <string>

#include <TopoDS_Shape.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

namespace geom {

struct ThreadResult {
    TopoDS_Shape shape;       // modified body on success; original body on failure
    bool ok = false;          // true only when the requested real geometry succeeded
    bool fell_back = false;   // true when real geometry failed and shape is unchanged
    std::string warning;      // diagnostic on failure (or a best-effort note on success)
};

// Thread cross-section FORM. The cut/raised geometry is otherwise identical — only the
// swept profile (flank angle, thread depth, crest/root flats) changes.
enum class ThreadForm {
    V60,    // ISO 68-1 / ASME B1.1 unified 60° V (metric + SAE) — the default.
    ACME,   // ASME B1.5 trapezoidal 29° included, depth 0.5·P (power/lead screws).
    TRAP,   // ISO 2904 metric trapezoidal 30° included, depth 0.5·P.
    SQUARE, // square thread — 0° flanks, depth 0.5·P (efficient power transmission).
};

// Cut a helical ISO-60° thread into `body` along the cylindrical face whose axis is
// (axis_loc, axis_dir) and outer/bore radius is `cyl_radius`.
//   pitch       axial advance per turn (mm).
//   major_dia   nominal major diameter (mm) — sets the V-groove depth.
//   length      axial run of the threaded length (mm), measured from the face's axis
//               location along +axis_dir.
//   starts      number of thread starts (>=1); multi-start multiplies the lead.
//   lefthand    left-hand helix when true.
//   internal    true = thread a bore (cut OUTWARD into the wall); false = external
//               shaft thread (cut INWARD).
//   raised      false (default) = CUT a V-groove (subtract material, the classic
//               machined thread). true = ADD/EXTRUDE a raised helical ridge (fuse
//               material — a cast/moulded/3D-printed external thread or lead screw).
//               The selected cylinder is the root diameter; the ridge grows outward.
//   turns_cap   cap on the complete run's helical turns, including end extensions
//               (default 24). Runs exceeding it fail rather than silently truncate.
//               An additional 128-section work limit bounds multi-start complexity.
//
// Never throws. See the ROBUSTNESS CONTRACT above.
ThreadResult cut_thread(const TopoDS_Shape& body, const gp_Pnt& axis_loc,
                        const gp_Dir& axis_dir, double cyl_radius, double pitch,
                        double major_dia, double length, int starts, bool lefthand,
                        bool internal, bool raised, double turns_cap,
                        ThreadForm form = ThreadForm::V60);

}  // namespace geom
