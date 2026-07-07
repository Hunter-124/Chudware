// Airfoil profile generator — NACA 4-/5-digit closed forms + Selig/Lednicer .dat import.
//
// Pure math (no ImGui / no engine deps) so the formulas are testable in isolation and the
// sketcher (SketchTool::Airfoil) just consumes the resulting point arrays. Every generator
// returns the profile as two surface polylines, each ordered LEADING-EDGE -> TRAILING-EDGE
// and SHARING the leading-edge point (upper.front() == lower.front()). The sketcher emits
// the upper and lower surfaces as two through-point splines that chain into one closed loop
// (plus a short trailing-edge line when the TE is open), which the C++ core profile builder
// now lifts into an extrudable face.
//
// Coordinates are in millimetres: the unit-chord airfoil is scaled by `chord` and rotated by
// the angle of attack about the leading edge (origin). Positive `aoaDeg` pitches the nose up
// relative to a +X freestream (the profile is rotated clockwise, so the trailing edge drops).
#pragma once

#include <array>
#include <string>
#include <vector>

namespace ui::airfoil {

struct Profile {
    std::vector<std::array<double, 2>> upper;  // LE -> TE, upper surface (mm)
    std::vector<std::array<double, 2>> lower;  // LE -> TE, lower surface (mm)
    bool sharpTE = false;        // upper.back() ~ lower.back() (closed trailing edge)
    std::string name;            // display label, e.g. "NACA 2412"
    std::string error;           // non-empty -> generation failed (message for the toast)
    bool ok() const { return error.empty() && upper.size() >= 2 && lower.size() >= 2; }
};

// NACA 4-digit (e.g. "2412", "0012"). `nPts` = points per surface (cosine-spaced; clustered at
// the leading & trailing edges). `closedTE` uses the -0.1036 thickness coefficient so the TE
// closes to a point (vs the classic -0.1015 open TE).
Profile naca4(const std::string& code, double chord, double aoaDeg, int nPts, bool closedTE);

// NACA 5-digit (e.g. "23012" standard, "23112" reflex). Third digit selects standard (0) or
// reflex (1) mean lines. Camber position is taken from the documented 5-station table.
Profile naca5(const std::string& code, double chord, double aoaDeg, int nPts, bool closedTE);

// Parse an airfoil coordinate file in Selig (single TE->LE->TE loop) or Lednicer (count line +
// two LE->TE surfaces) format. The file is treated as unit-chord and scaled by `chord`.
Profile fromDatFile(const std::string& path, double chord, double aoaDeg);

}  // namespace ui::airfoil
