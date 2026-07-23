// Section library lookups — see sections.h. Direct port of
// engine_oracle/structural/sections.py against the embedded tables.

#include "structural/sections.h"

#include <algorithm>
#include <cctype>

#include "structural/data_aisc.h"
#include "structural/data_lumber.h"

namespace aicad::structural {
namespace {

std::string key_upper(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        if (c == ' ') continue;
        out.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    }
    return out;
}

std::string key_lower(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        if (c == ' ') continue;
        out.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
    }
    return out;
}

}  // namespace

std::optional<nlohmann::json> get_aisc_section(const std::string& name) {
    const std::string key = key_upper(name);
    for (const AiscShape& s : kAiscShapes) {
        if (key == s.name) {
            return nlohmann::json{
                {"A", s.A},   {"d", s.d},   {"bf", s.bf}, {"tf", s.tf}, {"tw", s.tw},
                {"Ix", s.Ix}, {"Sx", s.Sx}, {"Zx", s.Zx}, {"Iy", s.Iy}, {"Sy", s.Sy},
                {"Zy", s.Zy}, {"J", s.J},   {"Cw", s.Cw},
            };
        }
    }
    return std::nullopt;
}

std::optional<nlohmann::json> get_lumber_section(const std::string& nominal) {
    // The oracle table is keyed by lowercase nominal ("2x4"); it also accepts
    // an uppercase key if one exists in the table (none do today).
    const std::string lower = key_lower(nominal);
    const std::string upper = key_upper(nominal);
    for (const LumberShape& s : kLumberShapes) {
        if (lower == s.name || upper == s.name) {
            return nlohmann::json{
                {"b_actual_mm", s.b_actual_mm}, {"d_actual_mm", s.d_actual_mm},
                {"A", s.A_mm2}, {"Ix", s.Ix_mm4}, {"Sx", s.Sx_mm3},
                {"Iy", s.Iy_mm4}, {"Sy", s.Sy_mm3},
            };
        }
    }
    return std::nullopt;
}

}  // namespace aicad::structural
