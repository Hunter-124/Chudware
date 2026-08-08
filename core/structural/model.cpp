// Structural document model — see model.h. Direct port of
// engine_oracle/structural/{elements,analysis(materials+sections),load_cases}.py.

#include "structural/model.h"
#include "structural/sections.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <regex>
#include <stdexcept>
#include <set>

namespace chudware::structural {

const char* kSchema = "aicad.structure/1";

namespace {

const std::array<double, 16> kIdentity4x4 = {
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
};

const char* kElementTypes[] = {"beam", "column", "post", "joist", "stud_wall", "footing"};
const char* kDofs[] = {"ux", "uy", "uz", "rx", "ry", "rz"};

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

int dof_index(const std::string& name) {
    const std::string key = lower(strip(name));
    for (int i = 0; i < 6; ++i) {
        if (key == kDofs[i]) return i;
    }
    throw std::invalid_argument("unknown structural DOF '" + name +
                                "' (expected ux, uy, uz, rx, ry, or rz)");
}

template <size_t N>
void parse_dof_set(const nlohmann::json& value, std::array<bool, N>& out, size_t offset,
                   const std::string& field) {
    if (!value.is_array()) {
        throw std::invalid_argument(field + " must be an array of DOF names or six booleans");
    }
    if (value.size() == 6 && std::all_of(value.begin(), value.end(),
                                         [](const auto& v) { return v.is_boolean(); })) {
        for (size_t i = 0; i < 6; ++i) out[offset + i] = value[i].get<bool>();
        return;
    }
    for (const auto& value_dof : value) {
        if (!value_dof.is_string()) {
            throw std::invalid_argument(field + " DOF names must be strings");
        }
        out[offset + static_cast<size_t>(dof_index(value_dof.get<std::string>()))] = true;
    }
}

void validate_transform(const std::array<double, 16>& m, const std::string& id) {
    for (double v : m) {
        if (!std::isfinite(v)) {
            throw std::invalid_argument("element '" + id + "' transform contains a non-finite value");
        }
    }
    constexpr double tol = 1e-7;
    if (std::abs(m[12]) > tol || std::abs(m[13]) > tol || std::abs(m[14]) > tol ||
        std::abs(m[15] - 1.0) > tol) {
        throw std::invalid_argument("element '" + id + "' transform must be an affine 4x4 matrix");
    }
    const std::array<std::array<double, 3>, 3> cols = {{
        {{m[0], m[4], m[8]}}, {{m[1], m[5], m[9]}}, {{m[2], m[6], m[10]}},
    }};
    for (int i = 0; i < 3; ++i) {
        double norm2 = 0.0;
        for (int k = 0; k < 3; ++k) norm2 += cols[i][k] * cols[i][k];
        if (std::abs(norm2 - 1.0) > tol) {
            throw std::invalid_argument("element '" + id +
                                        "' transform rotation must not scale member length");
        }
        for (int j = i + 1; j < 3; ++j) {
            double dot = 0.0;
            for (int k = 0; k < 3; ++k) dot += cols[i][k] * cols[j][k];
            if (std::abs(dot) > tol) {
                throw std::invalid_argument("element '" + id +
                                            "' transform rotation axes must be orthogonal");
            }
        }
    }
    const double det =
        m[0] * (m[5] * m[10] - m[6] * m[9]) -
        m[1] * (m[4] * m[10] - m[6] * m[8]) +
        m[2] * (m[4] * m[9] - m[5] * m[8]);
    if (std::abs(std::abs(det) - 1.0) > tol) {
        throw std::invalid_argument("element '" + id +
                                    "' transform rotation must be orthonormal");
    }
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
    if (doc.units != "mm") {
        throw std::invalid_argument("aicad.structure/1 units must be canonical 'mm', got '" +
                                    doc.units + "'");
    }
    if (d.contains("load_cases")) {
        if (!d["load_cases"].is_array()) {
            throw std::invalid_argument("load_cases must be an array");
        }
        for (const auto& lc : d["load_cases"]) {
            if (!lc.is_object()) throw std::invalid_argument("each load case must be an object");
            doc.load_cases.push_back(lc);
        }
    }
    if (!d.contains("elements") || !d["elements"].is_array() || d["elements"].empty()) {
        throw std::invalid_argument("elements must be a non-empty array");
    }

    std::set<std::string> element_ids;
    for (const nlohmann::json& e : d["elements"]) {
        if (!e.is_object()) throw std::invalid_argument("each structural element must be an object");
        const std::string et = e.value("element_type", "");
        bool known = false;
        for (const char* t : kElementTypes) known = known || et == t;
        if (!known) throw std::invalid_argument("unknown element_type '" + et + "'");

        StructuralElement el;
        el.element_type = et;
        el.id = strip(e.value("id", ""));
        el.section_name = strip(e.value("section_name", ""));
        el.material = strip(e.value("material", ""));
        el.length_mm = e.value("length_mm", 0.0);
        if (el.id.empty()) throw std::invalid_argument("structural element id must not be empty");
        if (!element_ids.insert(el.id).second) {
            throw std::invalid_argument("duplicate structural element id '" + el.id + "'");
        }
        if (el.section_name.empty()) {
            throw std::invalid_argument("element '" + el.id + "' section_name must not be empty");
        }
        if (el.material.empty()) {
            throw std::invalid_argument("element '" + el.id + "' material must not be empty");
        }
        if (!std::isfinite(el.length_mm) || el.length_mm <= 0.0) {
            throw std::invalid_argument("element '" + el.id +
                                        "' length_mm must be finite and greater than zero");
        }

        el.transform = kIdentity4x4;
        if (e.contains("transform")) {
            if (!e["transform"].is_array() || e["transform"].size() != 16) {
                throw std::invalid_argument("element '" + el.id +
                                            "' transform must contain exactly 16 numbers");
            }
            for (int i = 0; i < 16; ++i) {
                if (!e["transform"][i].is_number()) {
                    throw std::invalid_argument("element '" + el.id +
                                                "' transform entries must be numeric");
                }
                el.transform[i] = e["transform"][i].get<double>();
            }
        }
        validate_transform(el.transform, el.id);

        if (e.contains("releases")) {
            if (!e["releases"].is_object()) {
                throw std::invalid_argument("element '" + el.id + "' releases must be an object");
            }
            const auto& releases = e["releases"];
            for (const auto& [key, value] : releases.items()) {
                (void)value;
                if (key != "i" && key != "j") {
                    throw std::invalid_argument("element '" + el.id +
                                                "' releases has unknown end '" + key + "'");
                }
            }
            if (releases.contains("i")) {
                parse_dof_set(releases["i"], el.end_releases, 0,
                              "element '" + el.id + "' releases.i");
            }
            if (releases.contains("j")) {
                parse_dof_set(releases["j"], el.end_releases, 6,
                              "element '" + el.id + "' releases.j");
            }
            if (std::none_of(el.end_releases.begin(), el.end_releases.end(),
                             [](bool value) { return value; })) {
                throw std::invalid_argument("element '" + el.id + "' releases no DOFs");
            }
        }
        doc.elements.push_back(std::move(el));
    }

    if (d.contains("supports")) {
        if (!d["supports"].is_array()) throw std::invalid_argument("supports must be an array");
        for (const auto& s : d["supports"]) {
            if (!s.is_object()) throw std::invalid_argument("each support must be an object");
            StructuralSupport support;
            support.element_id = strip(s.value("element_id", ""));
            if (!element_ids.count(support.element_id)) {
                throw std::invalid_argument("support references unknown element_id '" +
                                            support.element_id + "'");
            }
            const std::string end = lower(strip(s.value("end", "")));
            if (end != "i" && end != "j") {
                throw std::invalid_argument("support end must be 'i' or 'j'");
            }
            support.at_j = end == "j";
            if (!s.contains("restraints")) {
                throw std::invalid_argument("support for element '" + support.element_id +
                                            "' requires restraints");
            }
            parse_dof_set(s["restraints"], support.restrained, 0,
                          "support for element '" + support.element_id + "'");
            if (std::none_of(support.restrained.begin(), support.restrained.end(),
                             [](bool v) { return v; })) {
                throw std::invalid_argument("support for element '" + support.element_id +
                                            "' restrains no DOFs");
            }
            doc.supports.push_back(std::move(support));
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
        if (starts_with(key, w)) return kMaterials[1].second;
    }
    if (starts_with(key, "alu")) return {69000.0, 26000.0, 0.33};
    throw std::invalid_argument("unsupported structural material '" + material +
                                "'; provide a known steel, wood, concrete, or aluminum key");
}

// ---------------------------------------------------------------- sections

namespace {

// Metric catalogue shorthand → (overall depth, flange width, web, flange) mm.
const std::pair<const char*, std::array<double, 4>> kNamedIShapes[] = {
    {"W150X100", {148.0, 100.0, 4.3, 7.0}},
    {"W200X100", {200.0, 100.0, 5.5, 8.0}},
    {"W250X150", {250.0, 150.0, 6.5, 9.0}},
    {"W310X165", {310.0, 165.0, 7.5, 11.8}},
};

std::vector<double> dims_of(const std::string& key) {
    static const std::regex re(R"(\d+(?:\.\d+)?)");
    std::vector<double> out;
    for (std::sregex_iterator it(key.begin(), key.end(), re), end; it != end; ++it) {
        out.push_back(std::stod(it->str()));
    }
    return out;
}

void require_dims(const std::vector<double>& dims, const std::string& name) {
    for (double d : dims) {
        if (!std::isfinite(d) || d <= 0.0) {
            throw std::invalid_argument("section '" + name +
                                        "' dimensions must be finite and greater than zero");
        }
    }
}

// (A, Iy, Iz, J, Smajor, Sminor), with section height along local y.
std::array<double, 6> rect_props(double w, double h) {
    const double A = w * h;
    const double Iz = w * h * h * h / 12.0;
    const double Iy = h * w * w * w / 12.0;
    const double a = h >= w ? h : w;
    const double b = h >= w ? w : h;
    const double J =
        a * b * b * b *
        (1.0 / 3.0 - 0.21 * (b / a) * (1.0 - b * b * b * b / (12.0 * a * a * a * a)));
    return {A, Iy, Iz, J, Iz / (h / 2.0), Iy / (w / 2.0)};
}

std::array<double, 6> hollow_rect_props(double w, double h, double t,
                                        const std::string& name) {
    if (2.0 * t >= std::min(w, h)) {
        throw std::invalid_argument("hollow section '" + name +
                                    "' wall thickness must be less than half its smallest side");
    }
    const double wi = w - 2.0 * t;
    const double hi = h - 2.0 * t;
    const double A = w * h - wi * hi;
    const double Iz = (w * h * h * h - wi * hi * hi * hi) / 12.0;
    const double Iy = (h * w * w * w - hi * wi * wi * wi) / 12.0;
    // Thin-wall closed-section St Venant constant, using median-line dimensions.
    const double wm = w - t, hm = h - t;
    const double J = 2.0 * t * wm * wm * hm * hm / (wm + hm);
    return {A, Iy, Iz, J, Iz / (h / 2.0), Iy / (w / 2.0)};
}

std::array<double, 6> circular_hollow_props(double od, double t,
                                            const std::string& name) {
    if (2.0 * t >= od) {
        throw std::invalid_argument("circular section '" + name +
                                    "' wall thickness must be less than its radius");
    }
    constexpr double pi = 3.14159265358979323846;
    const double id = od - 2.0 * t;
    const double A = pi * (od * od - id * id) / 4.0;
    const double I = pi * (od * od * od * od - id * id * id * id) / 64.0;
    return {A, I, I, 2.0 * I, I / (od / 2.0), I / (od / 2.0)};
}

std::array<double, 6> i_props(double h, double b, double tw, double tf,
                              const std::string& name) {
    if (2.0 * tf >= h || tw >= b) {
        throw std::invalid_argument("I-section '" + name +
                                    "' web/flange dimensions do not fit its envelope");
    }
    const double hw = h - 2.0 * tf;
    const double A = 2.0 * b * tf + hw * tw;
    const double Iz = b * h * h * h / 12.0 - (b - tw) * hw * hw * hw / 12.0;
    const double Iy = 2.0 * (tf * b * b * b / 12.0) + hw * tw * tw * tw / 12.0;
    const double J = (2.0 * b * tf * tf * tf + hw * tw * tw * tw) / 3.0;
    return {A, Iy, Iz, J, Iz / (h / 2.0), Iy / (b / 2.0)};
}

bool is_wood_material(const std::string& material) {
    const std::string key = lower(strip(material));
    for (const char* prefix : {"cedar", "df", "spf", "douglas", "fir", "lumber", "wood"}) {
        if (starts_with(key, prefix)) return true;
    }
    return false;
}

}  // namespace

SectionProps section_properties(const std::string& section_name, const std::string& material,
                                const std::string& element_type) {
    (void)element_type;
    const MaterialProps mp = material_properties(material);
    const std::string key = upper(strip(section_name));
    if (key.empty()) throw std::invalid_argument("section_name must not be empty");

    if (const auto aisc = get_aisc_section(key)) {
        return {mp.E,
                mp.G,
                mp.nu,
                (*aisc)["A"].get<double>(),
                (*aisc)["Iy"].get<double>(),
                (*aisc)["Ix"].get<double>(),
                (*aisc)["J"].get<double>(),
                (*aisc)["Sx"].get<double>(),
                (*aisc)["Sy"].get<double>()};
    }
    if (is_wood_material(material)) {
        if (const auto lumber = get_lumber_section(key)) {
            // The lumber table's Ix/Sx are the major (depth) axis.
            return {mp.E,
                    mp.G,
                    mp.nu,
                    (*lumber)["A"].get<double>(),
                    (*lumber)["Iy"].get<double>(),
                    (*lumber)["Ix"].get<double>(),
                    // A rectangle's torsion constant is not the polar moment.
                    rect_props((*lumber)["b_actual_mm"].get<double>(),
                               (*lumber)["d_actual_mm"].get<double>())[3],
                    (*lumber)["Sx"].get<double>(),
                    (*lumber)["Sy"].get<double>()};
        }
    }

    std::array<double, 6> p{};
    bool have = false;
    for (const auto& [name, dims] : kNamedIShapes) {
        if (key == name) {
            p = i_props(dims[0], dims[1], dims[2], dims[3], section_name);
            have = true;
            break;
        }
    }

    const auto d = dims_of(key);
    require_dims(d, section_name);
    if (!have && starts_with(key, "I")) {
        if (d.size() != 4) {
            throw std::invalid_argument("I-section '" + section_name +
                                        "' requires HxBxTWxTF dimensions");
        }
        p = i_props(d[0], d[1], d[2], d[3], section_name);
        have = true;
    } else if (!have && (starts_with(key, "RHS") || starts_with(key, "HSS"))) {
        if (d.size() != 3) {
            throw std::invalid_argument("hollow section '" + section_name +
                                        "' requires WxHxT dimensions");
        }
        p = hollow_rect_props(d[0], d[1], d[2], section_name);
        have = true;
    } else if (!have && (starts_with(key, "CHS") || starts_with(key, "PIPE"))) {
        if (d.size() != 2) {
            throw std::invalid_argument("circular section '" + section_name +
                                        "' requires ODxT dimensions");
        }
        p = circular_hollow_props(d[0], d[1], section_name);
        have = true;
    } else if (!have && starts_with(key, "W")) {
        throw std::invalid_argument("unknown steel section '" + section_name + "'");
    } else if (!have && d.size() == 2) {
        p = rect_props(d[0], d[1]);
        have = true;
    } else if (!have && d.size() == 3) {
        p = hollow_rect_props(d[0], d[1], d[2], section_name);
        have = true;
    }
    if (!have) {
        throw std::invalid_argument("unsupported structural section '" + section_name + "'");
    }
    return {mp.E, mp.G, mp.nu, p[0], p[1], p[2], p[3], p[4], p[5]};
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

}  // namespace chudware::structural
