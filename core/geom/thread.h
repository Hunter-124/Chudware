// Thread feature geometry (helical ISO-60° thread cut on a cylindrical face).
//
// A `thread` document feature (core/doc/document.cpp) threads a cylindrical face by
// cutting a real helical V-groove out of the body: it builds a helix on the target
// cylinder's axis at the pitch diameter, sweeps a SIMPLIFIED rounded ISO-60° thread
// profile along it, and booleans the swept coil out of the solid. External threads
// cut a groove into a shaft; internal threads cut into a bore wall.
//
// WHY a separate translation unit: like curves.cpp (spine wires) and features.cpp
// (primitives), this is a self-contained shape -> shape geometry builder with no
// feature-tree knowledge. The document compiler resolves the target face (axis +
// radius) and the size lookup, then hands this builder the raw numbers. Keeping it
// here mirrors the Python split and keeps the helical-boolean fragility (which needs
// careful turn caps + a graceful fallback) in one auditable place.
//
// ROBUSTNESS CONTRACT (helical booleans are fragile in OCCT): on ANY failure — an
// empty sweep, an empty cut, a thrown kernel exception, or a cut whose result fails
// a BRepCheck validity test — cut_thread NEVER returns a corrupt body. It returns
// the ORIGINAL body unchanged with fell_back=true and a human-readable warning, so a
// thread that can't be cut degrades to a plain cylinder rather than breaking the part.
#pragma once

#include <string>

#include <TopoDS_Shape.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

namespace geom {

struct ThreadResult {
    TopoDS_Shape shape;       // the modified body (or the original on a fall-back)
    bool ok = false;          // false only on a truly unusable input (e.g. null body)
    bool fell_back = false;   // true: geometry failed, `shape` is the body UNTHREADED
    std::string warning;      // non-empty on a fall-back (and some best-effort notes)
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
//   turns_cap   hard cap on the number of helical turns swept (robustness — a huge
//               length/tiny pitch would otherwise sweep thousands of turns and hang).
//
// Never throws. See the ROBUSTNESS CONTRACT above.
ThreadResult cut_thread(const TopoDS_Shape& body, const gp_Pnt& axis_loc,
                        const gp_Dir& axis_dir, double cyl_radius, double pitch,
                        double major_dia, double length, int starts, bool lefthand,
                        bool internal, double turns_cap);

}  // namespace geom
