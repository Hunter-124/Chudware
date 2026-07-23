// Structural cost takeoff — see estimate.h. Direct port of
// engine_oracle/structural/estimate.py.

#include "structural/estimate.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <utility>

namespace aicad::structural {

const char* kEstimateDisclaimer =
    "Preliminary cost estimate — not a contractor bid.  "
    "Rates are approximate national-average estimates (see rate-file provenance); verify with "
    "local suppliers before procurement.";

namespace {

// Steel weight: lbs per linear foot for common AISC sections (AISC Manual
// Table 1-1 dimensional facts). Keyed by designation, uppercase, no spaces.
const std::pair<const char*, double> kAiscLbsPerFt[] = {
    {"W4X13", 13.0},  {"W5X16", 16.0},  {"W6X9", 9.0},    {"W6X12", 12.0},  {"W6X15", 15.0},
    {"W6X20", 20.0},  {"W8X10", 10.0},  {"W8X15", 15.0},  {"W8X18", 18.0},  {"W8X21", 21.0},
    {"W8X24", 24.0},  {"W8X28", 28.0},  {"W8X31", 31.0},  {"W8X35", 35.0},  {"W8X40", 40.0},
    {"W8X48", 48.0},  {"W10X12", 12.0}, {"W10X17", 17.0}, {"W10X22", 22.0}, {"W10X26", 26.0},
    {"W10X30", 30.0}, {"W10X33", 33.0}, {"W10X39", 39.0}, {"W10X45", 45.0}, {"W10X49", 49.0},
    {"W12X16", 16.0}, {"W12X19", 19.0}, {"W12X22", 22.0}, {"W12X26", 26.0}, {"W12X30", 30.0},
    {"W12X35", 35.0}, {"W12X40", 40.0}, {"W14X22", 22.0}, {"W14X26", 26.0}, {"W14X30", 30.0},
    {"W14X38", 38.0}, {"W14X48", 48.0}, {"W14X53", 53.0},
    {"HSS4X4X1/4", 12.21}, {"HSS5X5X1/4", 15.62}, {"HSS6X6X1/4", 19.02},
};

double length_ft(const StructuralElement& el) { return el.length_mm / 304.8; }

// Python round(x, ndigits) — ties-to-even on the scaled value.
double pyround(double v, int ndigits) {
    const double scale = std::pow(10.0, ndigits);
    return std::nearbyint(v * scale) / scale;
}

std::string lower(const std::string& s) {
    std::string out = s;
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return out;
}

std::string upper_strip(const std::string& s) {
    std::string out;
    for (char c : s) {
        if (c == ' ') continue;
        out.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    }
    return out;
}

bool is_lumber(const std::string& material) {
    const std::string mat = lower(material);
    for (const char* kw : {"lumber", "cedar", "pine", "fir", "spruce", "hemlock", "wood",
                           "timber", "douglas", "larch", "redwood", "oak", "maple"}) {
        if (mat.find(kw) != std::string::npos) return true;
    }
    return false;
}

// Python int() semantics for the strict cases the oracle hits: optional sign
// followed by digits, whole string consumed. Returns nullopt otherwise.
std::optional<int> python_int(const std::string& s) {
    if (s.empty()) return std::nullopt;
    size_t i = 0;
    if (s[i] == '+' || s[i] == '-') ++i;
    if (i >= s.size()) return std::nullopt;
    for (size_t j = i; j < s.size(); ++j) {
        if (!std::isdigit(static_cast<unsigned char>(s[j]))) return std::nullopt;
    }
    return std::stoi(s);
}

// Parse "4x4"-style nominal → (thickness_nom_in, width_nom_in).
std::optional<std::pair<int, int>> parse_nominal_lumber(const std::string& section_name) {
    const std::string name = upper_strip(section_name);
    const auto pos = name.find('X');
    if (pos == std::string::npos) return std::nullopt;
    const auto t = python_int(name.substr(0, pos));
    const auto w = python_int(name.substr(pos + 1));
    if (!t || !w) return std::nullopt;
    return std::make_pair(*t, *w);
}

// Python str.title() for the element-type strings ("stud wall" → "Stud Wall").
std::string title_case(std::string s) {
    bool new_word = true;
    for (char& c : s) {
        if (std::isalpha(static_cast<unsigned char>(c))) {
            c = static_cast<char>(new_word ? std::toupper(static_cast<unsigned char>(c))
                                           : std::tolower(static_cast<unsigned char>(c)));
            new_word = false;
        } else {
            new_word = true;
        }
    }
    return s;
}

// Python f"{v:.2f}" — matches glibc %.2f (both correctly round the binary value).
std::string fmt2(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.2f", v);
    return buf;
}

nlohmann::json line_dict(const std::string& element_id, const std::string& element_type,
                         const std::string& section_name, const std::string& material,
                         double quantity, const std::string& unit, double unit_rate,
                         double material_cost, double labor_hours, double labor_cost,
                         const std::string& description) {
    return {{"element_id", element_id},     {"element_type", element_type},
            {"section_name", section_name}, {"material", material},
            {"quantity", quantity},         {"unit", unit},
            {"unit_rate", unit_rate},       {"material_cost", material_cost},
            {"labor_hours", labor_hours},   {"labor_cost", labor_cost},
            {"description", description}};
}

}  // namespace

double RateTable::install_hour(const std::string& element_type) const {
    for (const auto& [et, hrs] : install_hours) {
        if (et == element_type) return hrs;
    }
    return 0.5;  // oracle fallback
}

RateTable load_rates(const std::string& rates_path) {
    RateTable rates;
    std::string path = rates_path;
    if (path.empty()) {
        if (const char* env = std::getenv("AICAD_STRUCTURAL_RATES")) path = env;
    }
    if (path.empty()) return rates;
    std::ifstream fh(path);
    if (!fh) return rates;  // unreadable → embedded defaults
    nlohmann::json j = nlohmann::json::parse(fh, nullptr, false);
    if (j.is_discarded() || !j.is_object()) return rates;
    auto num = [&j](const char* key, double& out) {
        if (j.contains(key) && j[key].is_number()) out = j[key].get<double>();
    };
    num("cedar_lumber_per_bf", rates.cedar_lumber_per_bf);
    num("steel_per_lb", rates.steel_per_lb);
    num("concrete_per_cy", rates.concrete_per_cy);
    num("labor_per_hr", rates.labor_per_hr);
    num("fastener_per_ea", rates.fastener_per_ea);
    if (j.contains("_install_hours") && j["_install_hours"].is_object()) {
        for (const auto& [key, val] : j["_install_hours"].items()) {
            if (!val.is_number() || (!key.empty() && key.front() == '_')) continue;
            bool replaced = false;
            for (auto& [et, hrs] : rates.install_hours) {
                if (et == key) {
                    hrs = val.get<double>();
                    replaced = true;
                    break;
                }
            }
            if (!replaced) rates.install_hours.emplace_back(key, val.get<double>());
        }
    }
    return rates;
}

double board_feet(const StructuralElement& el) {
    const auto dims = parse_nominal_lumber(el.section_name);
    if (!dims) {
        throw std::invalid_argument("Cannot parse nominal lumber size from section_name='" +
                                    el.section_name + "'. Expected format like '4x4', '2x10'.");
    }
    return pyround((dims->first * dims->second * length_ft(el)) / 12.0, 4);
}

double steel_weight_lbs(const StructuralElement& el) {
    const std::string key = upper_strip(el.section_name);
    for (const auto& [name, lbs] : kAiscLbsPerFt) {
        if (key == name) return lbs * length_ft(el);
    }
    throw std::out_of_range("Steel section '" + key + "' not in lbs/ft table.");
}

int fastener_count(const StructureDoc& doc) {
    int total = 0;
    for (const StructuralElement& el : doc.elements) {
        const std::string& et = el.element_type;
        if (et == "post" || et == "column") {
            total += 4 + 4;  // post_base + post_cap
        } else if (et == "beam") {
            total += 6;  // beam_splice
        } else if (et == "joist") {
            total += 2 * 10;  // two joist hangers
        } else if (et == "stud_wall") {
            total += 6;  // stud_sheathing
        } else if (et == "footing") {
            total += 4;  // column_base
        }
    }
    return total;
}

nlohmann::json estimate(const StructureDoc& doc, const RateTable& rates) {
    nlohmann::json line_items = nlohmann::json::array();
    for (const StructuralElement& el : doc.elements) {
        const std::string& et = el.element_type;
        const std::string& mat = el.material;
        const double hrs = rates.install_hour(et);

        double qty, unit_rate, mat_cost;
        std::string unit;
        if (is_lumber(mat)) {
            try {
                qty = board_feet(el);
            } catch (const std::invalid_argument&) {
                qty = 0.0;  // unparseable size — skip material cost, keep labor
            }
            unit = "BF";
            unit_rate = rates.cedar_lumber_per_bf;
            mat_cost = pyround(qty * unit_rate, 2);
        } else if ((et == "beam" || et == "column") && !is_lumber(mat)) {
            try {
                qty = steel_weight_lbs(el);
            } catch (const std::out_of_range&) {
                qty = 0.0;  // unknown section — unestimated
            }
            unit = "lbs";
            unit_rate = rates.steel_per_lb;
            mat_cost = pyround(qty * unit_rate, 2);
        } else if (et == "footing") {
            qty = 0.0;  // concrete volume not available from length alone
            unit = "cy";
            unit_rate = rates.concrete_per_cy;
            mat_cost = 0.0;
        } else {
            qty = 0.0;
            unit = "ea";
            unit_rate = 0.0;
            mat_cost = 0.0;
        }

        const double labor_cost = pyround(hrs * rates.labor_per_hr, 2);
        std::string et_title = et;
        std::replace(et_title.begin(), et_title.end(), '_', ' ');
        const std::string desc = title_case(et_title) + " — " + el.section_name + " × " +
                                 fmt2(length_ft(el)) + " ft " + mat;

        line_items.push_back(line_dict(el.id, et, el.section_name, mat, pyround(qty, 4), unit,
                                       unit_rate, mat_cost, hrs, labor_cost, desc));
    }

    const int n_fasteners = fastener_count(doc);
    const double fastener_mat = pyround(n_fasteners * rates.fastener_per_ea, 2);
    const nlohmann::json fastener_line = line_dict(
        "__fasteners__", "fasteners", "", "hardware", static_cast<double>(n_fasteners), "ea",
        rates.fastener_per_ea, fastener_mat, 0.0, 0.0,
        "Structural fasteners — " + std::to_string(n_fasteners) +
            " ea (anchor bolts, hangers, screws)");

    double total_mat = fastener_mat, total_labor = 0.0;
    for (const nlohmann::json& ln : line_items) {
        total_mat += ln["material_cost"].get<double>();
        total_labor += ln["labor_cost"].get<double>();
    }
    total_mat = pyround(total_mat, 2);
    total_labor = pyround(total_labor, 2);

    return {{"line_items", std::move(line_items)},
            {"fastener_line", fastener_line},
            {"total_material_cost", total_mat},
            {"total_labor_cost", total_labor},
            {"total_cost", pyround(total_mat + total_labor, 2)},
            {"currency", "USD"},
            {"rate_table_date", "ENR Q4-2025"},
            {"disclaimer", kEstimateDisclaimer}};
}

}  // namespace aicad::structural
