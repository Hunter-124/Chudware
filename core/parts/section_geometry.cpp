#include "parts/section_geometry.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <stdexcept>
#include <string>
#include <utility>

#include "structural/sections.h"

namespace chudware::parts {
namespace {

using Point = std::array<double, 2>;

json dim(double value) {
    return {{"value", value}, {"expr", nullptr}};
}

// Keep a single, closed editable wire, with independent critical dimensions
// and geometric relations instead of fixing each line's coordinates.
class Outline {
public:
    template <std::size_t N>
    explicit Outline(const std::array<Point, N>& points) {
        for (std::size_t i = 0; i < N; ++i) {
            const Point& a = points[i];
            const Point& b = points[(i + 1) % N];
            const std::string id = "e" + std::to_string(i + 1);
            const std::string next = "e" + std::to_string((i + 1) % N + 1);
            entities_.push_back({{"id", id}, {"type", "line"},
                                 {"p1", {a[0], a[1]}}, {"p2", {b[0], b[1]}}});
            relation("coincident", json::array({id + ".p2", next + ".p1"}));
            relation(a[1] == b[1] ? "horizontal" : "vertical", json::array({id}));
        }
    }

    void relation(const char* type, json refs) {
        constraints_.push_back({{"id", next_id()}, {"type", type}, {"refs", std::move(refs)}});
    }

    void length(const char* entity, double value) {
        constraints_.push_back({{"id", next_id()}, {"type", "length"},
                                {"refs", json::array({entity})}, {"value", dim(value)}});
    }

    void distance(const char* first, const char* second, const char* axis, double value) {
        constraints_.push_back({{"id", next_id()}, {"type", "distance"},
                                {"refs", json::array({first, second})},
                                {"axis", axis}, {"value", dim(value)}});
    }

    json extrude(const std::string& name, double length_mm) && {
        json ops = json::array();
        ops.push_back({{"op", "add_feature"},
                       {"feature", {{"type", "sketch"}, {"name", name + " profile"},
                                    {"params", {{"entities", std::move(entities_)},
                                                {"constraints", std::move(constraints_)}}},
                                    {"refs", {{"plane", "plane.top"}}}}}});
        // Select the whole profile, not a guessed point in the void of a channel.
        const json region{{"kind", "region"}, {"sketch", "$0"}, {"hint_index", 0}};
        ops.push_back({{"op", "add_feature"},
                       {"feature", {{"type", "extrude"}, {"name", name + " extrusion"},
                                    {"params", {{"mode", "new"}, {"end", "blind"},
                                                {"depth", dim(length_mm)}}},
                                    {"refs", {{"profile", json::array({region})}}}}}});
        return ops;
    }

private:
    std::string next_id() const { return "c" + std::to_string(constraints_.size() + 1); }
    json entities_ = json::array();
    json constraints_ = json::array();
};

std::string compact_upper(const std::string& text) {
    std::string result;
    result.reserve(text.size());
    for (unsigned char ch : text) {
        if (!std::isspace(ch)) result.push_back(static_cast<char>(std::toupper(ch)));
    }
    return result;
}

double required_dimension(const json& section, const char* key, const std::string& size) {
    const auto it = section.find(key);
    if (it == section.end() || !it->is_number())
        throw std::invalid_argument("section " + size + " is missing dimension " + key);
    const double value = it->get<double>();
    if (!std::isfinite(value) || value <= 0.0)
        throw std::invalid_argument("section " + size + " has invalid dimension " + key);
    return value;
}

double section_length(double requested, double default_basis) {
    const double result = requested == 0.0 ? 3.0 * default_basis : requested;
    if (!std::isfinite(result) || result <= 0.0)
        throw std::invalid_argument("section length must be positive and finite");
    return result;
}

void center_outline(Outline& outline, const char* vertical_midpoint) {
    outline.relation("point_on", json::array({"e1.mid", "axis.v"}));
    outline.relation("point_on", json::array({vertical_midpoint, "axis.h"}));
}

}  // namespace

PartBuild build_section_geometry(const std::string& size, double length_mm) {
    if (!std::isfinite(length_mm) || length_mm < 0.0)
        throw std::invalid_argument("section length must be positive and finite");

    const std::size_t separator = size.find(':');
    if (separator == std::string::npos || size.find(':', separator + 1) != std::string::npos)
        throw std::invalid_argument("section size must be <standard>:<designation>");
    const std::string standard = compact_upper(size.substr(0, separator));
    const std::string key = compact_upper(size.substr(separator + 1));
    if (standard.empty() || key.empty())
        throw std::invalid_argument("section size must be <standard>:<designation>");
    if (standard != "AISC360" && standard != "NDS")
        throw std::invalid_argument("unsupported section standard: " + standard);

    std::string designation = key;
    std::replace(designation.begin(), designation.end(), 'X', 'x');
    const std::string canonical_size = standard + ":" + designation;
    const auto found = standard == "AISC360" ? structural::get_aisc_section(key)
                                              : structural::get_lumber_section(key);
    if (!found)
        throw std::invalid_argument("section not found in structural catalog: " + canonical_size);
    const json& catalog = *found;
    json specs;
    json ops;
    double length;

    if (standard == "AISC360") {
        // Only W/S I-sections and C channels are represented by these catalog
        // fields. Refuse a future catalog entry with an unsupported prefix.
        if (key.size() < 2 || !std::isdigit(static_cast<unsigned char>(key[1])) ||
            (key[0] != 'W' && key[0] != 'S' && key[0] != 'C'))
            throw std::invalid_argument("unsupported AISC section profile: " + designation);
        const double d = required_dimension(catalog, "d", canonical_size);
        const double bf = required_dimension(catalog, "bf", canonical_size);
        const double tf = required_dimension(catalog, "tf", canonical_size);
        const double tw = required_dimension(catalog, "tw", canonical_size);
        if (tw >= bf || tf >= d / 2.0)
            throw std::invalid_argument("section " + canonical_size + " has degenerate flange or web dimensions");
        length = section_length(length_mm, d);
        const double left = -bf / 2.0, right = bf / 2.0;
        const double bottom = -d / 2.0, top = d / 2.0;
        const bool channel = key[0] == 'C';
        if (channel) {
            const double web = left + tw;
            const std::array<Point, 8> points{{
                {left, bottom}, {right, bottom}, {right, bottom + tf},
                {web, bottom + tf}, {web, top - tf}, {right, top - tf},
                {right, top}, {left, top},
            }};
            Outline outline(points);
            outline.length("e1", bf);
            outline.length("e2", tf);
            outline.length("e8", d);
            outline.distance("e1.p1", "e3.p2", "h", tw);
            outline.relation("equal", json::array({"e1", "e7"}));
            outline.relation("equal", json::array({"e2", "e6"}));
            center_outline(outline, "e8.mid");
            ops = std::move(outline).extrude(canonical_size, length);
        } else {
            const double web = tw / 2.0;
            const std::array<Point, 12> points{{
                {left, bottom}, {right, bottom}, {right, bottom + tf},
                {web, bottom + tf}, {web, top - tf}, {right, top - tf},
                {right, top}, {left, top}, {left, top - tf},
                {-web, top - tf}, {-web, bottom + tf}, {left, bottom + tf},
            }};
            Outline outline(points);
            outline.length("e1", bf);
            outline.length("e2", tf);
            outline.distance("e1.p1", "e7.p2", "v", d);
            outline.distance("e4.p1", "e10.p1", "h", tw);
            outline.relation("equal", json::array({"e2", "e6"}));
            outline.relation("equal", json::array({"e2", "e8"}));
            outline.relation("equal", json::array({"e2", "e12"}));
            outline.relation("equal", json::array({"e1", "e7"}));
            outline.relation("equal", json::array({"e3", "e5"}));
            outline.relation("equal", json::array({"e3", "e11"}));
            center_outline(outline, "e4.mid");
            ops = std::move(outline).extrude(canonical_size, length);
        }
        json omissions = json::array({"rolled root and toe radii",
                                      "manufacturing tolerances and material grade"});
        if (key[0] == 'C' || key[0] == 'S')
            omissions.push_back("tapered flange surfaces");
        specs = {{"standard", standard}, {"designation", designation},
                 {"section_type", channel ? "channel" : "i_section"},
                 {"d", d}, {"bf", bf}, {"tf", tf}, {"tw", tw},
                 {"source", "core/structural/data_aisc.h: curated AISC W/S/C section table (mm)"},
                 {"profile_basis", "idealized parallel flanges and sharp corners using catalog dimensions"},
                 {"idealized_profile_area_mm2", (d - 2.0 * tf) * tw + 2.0 * bf * tf},
                 {"catalog_properties", catalog}, {"omissions", std::move(omissions)}};
    } else {
        const double b = required_dimension(catalog, "b_actual_mm", canonical_size);
        const double d = required_dimension(catalog, "d_actual_mm", canonical_size);
        length = section_length(length_mm, std::max(b, d));
        const std::array<Point, 4> points{{
            {-b / 2.0, -d / 2.0}, {b / 2.0, -d / 2.0},
            {b / 2.0, d / 2.0}, {-b / 2.0, d / 2.0},
        }};
        Outline outline(points);
        outline.length("e1", b);
        outline.length("e2", d);
        center_outline(outline, "e2.mid");
        ops = std::move(outline).extrude(canonical_size, length);
        specs = {{"standard", standard}, {"designation", designation},
                 {"section_type", "rectangle"}, {"b_actual_mm", b}, {"d_actual_mm", d},
                 {"source", "core/structural/data_lumber.h: NDS/PS 20 dressed S4S table (mm)"},
                 {"profile_basis", "sharp-cornered rectangle using catalog dimensions rounded to mm"},
                 {"idealized_profile_area_mm2", b * d}, {"catalog_properties", catalog},
                 {"omissions", json::array({"edge rounding", "manufacturing tolerances and material grade"})}};
    }

    PartBuild result;
    result.name = canonical_size;
    result.ops = std::move(ops);
    result.meta = {{"family", "section_profile"}, {"size", canonical_size},
                   {"length_mm", length}, {"geometry_quality", "idealized_catalog_profile"},
                   {"specs", std::move(specs)}};
    return result;
}

}  // namespace chudware::parts
