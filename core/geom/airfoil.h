// core/geom/airfoil.h — NACA / .dat airfoil coordinate generator (OCCT-free).
//
// Port of host/src/ui/airfoil.cpp so the AI agent can drop a parametric aerofoil into a
// sketch (the "airfoil" sketch entity expands to upper/lower splines via these functions).
// Geometry is in unit-chord space, then chord-scaled and AoA-rotated about the leading edge.
#pragma once

#include <array>
#include <string>
#include <vector>

namespace chudware {
namespace geom {

struct AirfoilProfile {
    std::vector<std::array<double, 2>> upper;  // LE -> TE (mm)
    std::vector<std::array<double, 2>> lower;  // LE -> TE (mm)
    bool sharpTE = false;                       // closed (sharp) trailing edge
    std::string name;
    std::string error;  // non-empty = failure
};

// NACA 4-digit (e.g. "2412") / 5-digit (e.g. "23012"). chord in mm, aoaDeg nose-up, nPts per
// surface (cosine-spaced), closedTE = sharp trailing edge.
AirfoilProfile naca4(const std::string& code, double chord, double aoaDeg, int nPts, bool closedTE);
AirfoilProfile naca5(const std::string& code, double chord, double aoaDeg, int nPts, bool closedTE);

// Selig / Lednicer .dat file import.
AirfoilProfile from_dat_file(const std::string& path, double chord, double aoaDeg);

// Dispatch helper for the "airfoil" sketch entity: pick naca4/naca5 by digit count (when
// `naca` is set) or .dat import (when `dat` is set). Empty inputs -> an error in .error.
AirfoilProfile airfoil_profile(const std::string& naca, const std::string& dat, double chord,
                               double aoaDeg, int nPts, bool closedTE);

}  // namespace geom
}  // namespace chudware
