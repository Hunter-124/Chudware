// core/tolerance/stackup.cpp — see stackup.h. Mirrors
// engine_oracle/tolerance/stackup.py line for line, including Python round()
// (half-even) semantics and the oracle's exact error messages.
#include "tolerance/stackup.h"

#include <algorithm>
#include <cmath>
#include <vector>

namespace aicad {
namespace tolerance {

const char* const kDisclaimer =
    "Preliminary stack-up — not a certified tolerance analysis. "
    "Verify with a qualified dimensional-metrology engineer before release.";

namespace {

// Python round(x, ndigits): round-half-EVEN on the scaled value (std::round is
// half-away). Same idiom as core/sourcing/cost.cpp::round6.
double pyround(double v, int ndigits) {
    const double scale = std::pow(10.0, ndigits);
    const double scaled = v * scale;
    const double fl = std::floor(scaled);
    const double diff = scaled - fl;
    double r;
    if (diff > 0.5)
        r = fl + 1.0;
    else if (diff < 0.5)
        r = fl;
    else
        r = (std::fmod(fl, 2.0) == 0.0) ? fl : fl + 1.0;
    return r / scale;
}

// Python str(float) for error messages (shortest repr, integral floats get ".0") —
// nlohmann's dump of a double produces exactly that shape.
std::string py_float_str(double v) { return json(v).dump(); }

// Python repr(str) for the simple names these messages carry: single-quoted.
std::string py_repr(const std::string& s) { return "'" + s + "'"; }

// float(d[key]) the Python way: numbers pass through, numeric strings parse,
// anything else / missing -> invalid. Returns false on failure.
bool py_float(const json& d, const char* key, double& out) {
    const auto it = d.find(key);
    if (it == d.end() || it->is_null()) return false;
    if (it->is_number()) {
        out = it->get<double>();
        return true;
    }
    if (it->is_string()) {
        try {
            const std::string s = it->get<std::string>();
            size_t used = 0;
            out = std::stod(s, &used);
            // Python float() tolerates surrounding whitespace only.
            size_t first = s.find_first_not_of(" \t\n\r\f\v");
            if (first == std::string::npos) return false;
            return used == s.size() ||
                   s.find_first_not_of(" \t\n\r\f\v", used) == std::string::npos;
        } catch (...) {
            return false;
        }
    }
    return false;
}

struct Contrib {
    std::string name;
    double nominal = 0.0;
    double tol = 0.0;
    double direction = 1.0;
    json ref;          // null or string
    bool assumed = false;
    double contrib_wc = 0.0;
    double contrib_rss_sq = 0.0;
    double pct_wc = 0.0;
    double pct_rss = 0.0;
};

}  // namespace

json stackup_analyze(const json& dims) {
    if (!dims.is_array() || dims.empty())
        throw StackupError("stackup_analyze requires at least one dimension");

    std::vector<Contrib> contribs;
    std::vector<std::string> assumed_dims;

    int i = 0;
    for (const json& d : dims) {
        const json& dj = d.is_object() ? d : json::object();
        std::string name;
        const auto nit = dj.find("name");
        if (nit != dj.end() && nit->is_string())
            name = nit->get<std::string>();
        else if (nit != dj.end() && nit->is_number())
            name = nit->dump();
        else
            name = "dim_" + std::to_string(i);

        double nominal, tol;
        if (!py_float(dj, "nominal", nominal))
            throw StackupError("Dimension " + py_repr(name) + ": missing or invalid 'nominal'");
        if (!py_float(dj, "tol", tol))
            throw StackupError("Dimension " + py_repr(name) + ": missing or invalid 'tol'");
        if (tol < 0)
            throw StackupError("Dimension " + py_repr(name) + ": 'tol' must be ≥ 0, got " +
                               py_float_str(tol));

        double direction = 1.0;
        const auto dit = dj.find("direction");
        if (dit != dj.end() && dit->is_number()) direction = dit->get<double>();
        if (direction != 1.0 && direction != -1.0)
            direction = direction != 0.0 ? std::copysign(1.0, direction) : 1.0;

        Contrib c;
        c.name = name;
        c.nominal = nominal;
        c.tol = tol;
        c.direction = direction;
        const auto rit = dj.find("ref");
        c.ref = (rit != dj.end() && rit->is_string()) ? *rit : json();
        const auto ait = dj.find("assumed");
        c.assumed = ait != dj.end() &&
                    (ait->is_boolean() ? ait->get<bool>()
                                       : ait->is_number() && ait->get<double>() != 0.0);
        if (c.assumed) assumed_dims.push_back(name);
        contribs.push_back(std::move(c));
        ++i;
    }

    double nominal_gap = 0.0, total_tol_wc = 0.0, rss_sq_sum = 0.0;
    for (const Contrib& c : contribs) {
        nominal_gap += c.direction * c.nominal;
        total_tol_wc += std::fabs(c.direction) * c.tol;
        const double at = c.direction * c.tol;
        rss_sq_sum += at * at;
    }
    const double total_tol_rss = std::sqrt(rss_sq_sum);

    for (Contrib& c : contribs) {
        c.contrib_wc = std::fabs(c.direction) * c.tol;
        const double at = c.direction * c.tol;
        c.contrib_rss_sq = at * at;
        c.pct_wc = total_tol_wc > 0 ? c.contrib_wc / total_tol_wc * 100.0 : 0.0;
        c.pct_rss = rss_sq_sum > 0 ? c.contrib_rss_sq / rss_sq_sum * 100.0 : 0.0;
    }

    // Sort descending by worst-case contribution; Python's list.sort is stable.
    std::stable_sort(contribs.begin(), contribs.end(),
                     [](const Contrib& a, const Contrib& b) {
                         return a.contrib_wc > b.contrib_wc;
                     });

    json contributors = json::array();
    for (const Contrib& c : contribs) {
        contributors.push_back({
            {"name", c.name},
            {"nominal", c.nominal},
            {"tol_half", c.tol},
            {"direction", c.direction},
            {"ref", c.ref},
            {"assumed", c.assumed},
            {"contrib_wc", pyround(c.contrib_wc, 8)},
            {"contrib_rss_sq", pyround(c.contrib_rss_sq, 8)},
            {"pct_wc", pyround(c.pct_wc, 4)},
            {"pct_rss", pyround(c.pct_rss, 4)},
        });
    }

    return {
        {"ok", true},
        {"nominal_gap", pyround(nominal_gap, 8)},
        {"worst_case",
         {{"total_tol", pyround(total_tol_wc, 8)},
          {"gap_max", pyround(nominal_gap + total_tol_wc, 8)},
          {"gap_min", pyround(nominal_gap - total_tol_wc, 8)}}},
        {"rss",
         {{"total_tol", pyround(total_tol_rss, 8)},
          {"gap_max", pyround(nominal_gap + total_tol_rss, 8)},
          {"gap_min", pyround(nominal_gap - total_tol_rss, 8)}}},
        {"contributors", std::move(contributors)},
        {"assumed_dims", assumed_dims},
        {"disclaimer", kDisclaimer},
    };
}

bool call(const std::string& method, const json& params, json& result, std::string& err) {
    if (method != "stackup_analyze") return false;
    err.clear();

    // The MCP wrapper validates mode BEFORE running the analysis.
    std::string mode = "both";
    const auto mit = params.find("mode");
    if (mit != params.end() && mit->is_string()) mode = mit->get<std::string>();
    if (mode != "worst_case" && mode != "rss" && mode != "both") {
        result = {{"ok", false},
                  {"error", "Invalid mode " + py_repr(mode) +
                                ". Choose: ['both', 'rss', 'worst_case']"},
                  {"code", "invalid_mode"}};
        return true;
    }

    json dims;
    const auto dit = params.find("dims");
    if (dit != params.end()) dims = *dit;

    json out;
    try {
        out = stackup_analyze(dims);
    } catch (const StackupError& e) {
        result = {{"ok", false}, {"error", e.what()}, {"code", "invalid_input"}};
        return true;
    } catch (const std::exception& e) {
        result = {{"ok", false}, {"error", e.what()}};
        return true;
    }

    if (mode == "worst_case")
        out.erase("rss");
    else if (mode == "rss")
        out.erase("worst_case");
    result = std::move(out);
    return true;
}

}  // namespace tolerance
}  // namespace aicad
