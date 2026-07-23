// Structural document model — see model.h. Direct port of
// engine_oracle/structural/{elements,analysis(materials+sections),load_cases}.py.

#include "structural/model.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <regex>
#include <stdexcept>

namespace aicad::structural {

const char* kSchema = "aicad.structure/1";

namespace {

const std::array<double, 16> kIdentity4x4 = {
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
};

const char* kElementTypes[] = {"beam", "column", "post", "joist", "stud_wall", "footing"};

std::string lower(const std::string& s) {
    std::string out = s;
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return out;
}

std::string upper(const std::string& s) {
    std::string out = s;
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    return out;
}

std::string strip(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\n\r");
    if (b == std::string::npos) return "";
    const auto e = s.find_last_not_of(" \t\n\r");
    return s.substr(b, e - b + 1);
}

bool starts_with(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

}  // namespace

bool is_structure(const nlohmann::json& doc) {
    return doc.is_object() && doc.value("schema", "") == kSchema;
}

StructureDoc structure_doc_from_dict(const nlohmann::json& d) {
    if (!is_structure(d)) {
        throw std::invalid_argument("not an aicad.structure document (schema=" +
                                    d.value("schema", nlohmann::json(nullptr)).dump() + ")");
    }
    StructureDoc doc;
    doc.name = d.value("name", "Structure1");
    doc.units = d.value("units", "mm");
    if (d.contains("load_cases") && d["load_cases"].is_array()) {
        doc.load_cases = d["load_cases"].get<std::vector<nlohmann::json>>();
    }
    if (d.contains("elements") && d["elements"].is_array()) {
        for (const nlohmann::json& e : d["elements"]) {
            const std::string et = e.value("element_type", "");
            bool known = false;
            for (const char* t : kElementTypes) known = known || et == t;
            if (!known) {
                throw std::invalid_argument("unknown element_type '" + et + "'");
            }
            StructuralElement el;
            el.element_type = et;
            el.id = e.value("id", "");
            el.section_name = e.value("section_name", "");
            el.material = e.value("material", "");
            el.length_mm = e.value("length_mm", 0.0);
            el.transform = kIdentity4x4;
            if (e.contains("transform") && e["transform"].is_array() &&
                e["transform"].size() == 16) {
                for (int i = 0; i < 16; ++i) el.transform[i] = e["transform"][i].get<double>();
            }
            doc.elements.push_back(std::move(el));
        }
    }
    return doc;
}

std::pair<std::array<double, 3>, std::array<double, 3>> element_endpoints(
    const StructuralElement& el) {
    const auto& m = el.transform;
    auto apply = [&m](double x, double y, double z) {
        return std::array<double, 3>{
            m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11],
        };
    };
    return {apply(0.0, 0.0, 0.0), apply(0.0, 0.0, el.length_mm)};
}

// ---------------------------------------------------------------- materials

MaterialProps material_properties(const std::string& material) {
    // Insertion order matters — mirrors analysis._MATERIALS dict order.
    static const std::pair<const char*, MaterialProps> kMaterials[] = {
        {"cedar", {12000.0, 750.0, 0.30}},
        {"df", {11000.0, 690.0, 0.30}},
        {"spf", {9500.0, 590.0, 0.30}},
        {"steel", {200000.0, 77000.0, 0.30}},
        {"concrete", {25000.0, 10400.0, 0.20}},
        {"aluminum", {69000.0, 26000.0, 0.33}},
    };
    const std::string key = lower(strip(material));
    for (const auto& [fam, props] : kMaterials) {
        if (key == fam || starts_with(key, fam)) return props;
    }
    for (const char* w : {"douglas", "fir", "lumber", "wood"}) {
        if (starts_with(key, w)) return kMaterials[1].second;  // df
    }
    if (starts_with(key, "alu")) return {69000.0, 26000.0, 0.33};
    return {9000.0, 560.0, 0.30};  // generic softwood fallback
}

// ---------------------------------------------------------------- sections

namespace {

// A tiny AISC-ish table of named I-shapes → (h, b, tw, tf) mm (brep._NAMED_I_SHAPES).
const std::pair<const char*, std::array<double, 4>> kNamedIShapes[] = {
    {"W150X100", {148.0, 100.0, 4.3, 7.0}},
    {"W200X100", {200.0, 100.0, 5.5, 8.0}},
    {"W250X150", {250.0, 150.0, 6.5, 9.0}},
    {"W310X165", {310.0, 165.0, 7.5, 11.8}},
};

// Per-element-type default section (brep._DEFAULT_SECTION).
const std::pair<const char*, const char*> kDefaultSection[] = {
    {"beam", "W200x100"}, {"column", "W200x100"}, {"post", "100x100"},
    {"joist", "38x235"},  {"stud_wall", "89x2400"}, {"footing", "600x600"},
};

std::vector<double> dims_of(const std::string& key) {
    static const std::regex re(R"(\d+(?:\.\d+)?)");
    std::vector<double> out;
    for (std::sregex_iterator it(key.begin(), key.end(), re), end; it != end; ++it) {
        out.push_back(std::stod(it->str()));
    }
    return out;
}

// (A, Iy, Iz, J) for a solid rectangle — mirrors analysis._rect_props.
std::array<double, 4> rect_props(double w, double h) {
    const double A = w * h;
    const double Iz = w * h * h * h / 12.0;
    const double Iy = h * w * w * w / 12.0;
    const double a = h >= w ? h : w;
    const double b = h >= w ? w : h;
    const double J = a * b * b * b * (1.0 / 3.0 - 0.21 * (b / a) * (1.0 - b * b * b * b / (12.0 * a * a * a * a)));
    return {A, Iy, Iz, J};
}

// (A, Iy, Iz, J) for an I-shape — mirrors analysis._i_props.
std::array<double, 4> i_props(double h, double b, double tw, double tf) {
    const double hw = h - 2.0 * tf;
    const double A = 2.0 * b * tf + hw * tw;
    const double Iz = b * h * h * h / 12.0 - (b - tw) * hw * hw * hw / 12.0;
    const double Iy = 2.0 * (tf * b * b * b / 12.0) + hw * tw * tw * tw / 12.0;
    const double J = (2.0 * b * tf * tf * tf + (h - tf) * tw * tw * tw) / 3.0;
    return {A, Iy, Iz, J};
}

}  // namespace

SectionProps section_properties(const std::string& section_name, const std::string& material,
                                const std::string& element_type) {
    const MaterialProps mp = material_properties(material);
    const std::string key = upper(strip(section_name));

    std::array<double, 4> p{};
    bool have = false;
    for (const auto& [name, dims] : kNamedIShapes) {
        if (key == name) {
            p = i_props(dims[0], dims[1], dims[2], dims[3]);
            have = true;
            break;
        }
    }
    if (!have && starts_with(key, "I")) {
        const auto d = dims_of(key);
        if (d.size() >= 4) {
            p = i_props(d[0], d[1], d[2], d[3]);
            have = true;
        }
    }
    if (!have && starts_with(key, "RHS")) {
        const auto d = dims_of(key);
        if (d.size() >= 2) {
            p = rect_props(d[0], d[1]);  // solid outer rectangle (oracle NOTE)
            have = true;
        }
    }
    if (!have) {
        const auto d = dims_of(key);
        if (d.size() >= 2) {
            p = rect_props(d[0], d[1]);
            have = true;
        } else if (d.size() == 1) {
            p = rect_props(d[0], d[0]);
            have = true;
        }
    }
    if (!have) {
        // Per-element-type default; recurse with element_type cleared so an
        // unknown type lands on the "100x100" catch-all (oracle recursion).
        std::string def = "100x100";
        for (const auto& [et, sec] : kDefaultSection) {
            if (element_type == et) {
                def = sec;
                break;
            }
        }
        return section_properties(def, material, "");
    }
    return {mp.E, mp.G, mp.nu, p[0], p[1], p[2], p[3]};
}

// ---------------------------------------------------------------- load combos

std::vector<std::pair<std::string, double>> LoadCombo::factors() const {
    std::vector<std::pair<std::string, double>> out;
    if (dead != 0.0) out.emplace_back("dead", dead);
    if (live != 0.0) out.emplace_back("live", live);
    if (wind != 0.0) out.emplace_back("wind", wind);
    if (snow != 0.0) out.emplace_back("snow", snow);
    return out;
}

LoadCombo get_combo(const std::string& name) {
    static const LoadCombo kPresets[] = {
        {"DEAD_ONLY", 1.4, 0.0, 0.0, 0.0},
        {"DEAD_LIVE", 1.2, 1.6, 0.0, 0.0},
        {"DEAD_LIVE_WIND", 1.2, 1.6, 0.5, 0.0},
    };
    const std::string key = upper(strip(name));
    for (const LoadCombo& c : kPresets) {
        if (c.name == key) return c;
    }
    throw std::out_of_range("unknown load combo '" + name + "'");
}

}  // namespace aicad::structural
