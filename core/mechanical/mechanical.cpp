// core/mechanical/mechanical.cpp — see mechanical.h. Mirrors
// engine_oracle/mechanical/{gear_check,bearing_life}.py, including the oracle's exact
// validation messages and unrounded float outputs.
#include "mechanical/mechanical.h"

#include <cctype>
#include <cmath>

namespace chudware {
namespace mechanical {

const char* const kDisclaimer =
    "Preliminary engineering estimate — not a certified analysis. "
    "Verify with a qualified mechanical engineer before use in safety-critical applications.";

namespace {

// Required numeric parameter (the Python MCP signature makes these mandatory).
double req_num(const json& p, const char* key) {
    const auto it = p.find(key);
    if (it == p.end() || !it->is_number())
        throw MechError(std::string("missing or non-numeric required parameter '") + key + "'");
    return it->get<double>();
}

double opt_num(const json& p, const char* key, double dflt) {
    const auto it = p.find(key);
    if (it == p.end() || it->is_null()) return dflt;
    if (it->is_number()) return it->get<double>();
    return dflt;
}

// Optional-None numeric: null/absent -> no value (Python's Optional[float] = None).
bool opt_none_num(const json& p, const char* key, double& out) {
    const auto it = p.find(key);
    if (it == p.end() || it->is_null() || !it->is_number()) return false;
    out = it->get<double>();
    return true;
}

std::string opt_str(const json& p, const char* key, const std::string& dflt) {
    const auto it = p.find(key);
    if (it == p.end() || !it->is_string()) return dflt;
    return it->get<std::string>();
}

}  // namespace

json gear_check(const json& params) {
    const double Wt = req_num(params, "Wt");
    const double module = req_num(params, "module");
    const double face_width = req_num(params, "face_width");
    const double pitch_diameter = req_num(params, "pitch_diameter");
    const double J = req_num(params, "J");
    const double I = req_num(params, "I");
    const double Ko = opt_num(params, "Ko", 1.0);
    const double Kv = opt_num(params, "Kv", 1.0);
    const double Ks = opt_num(params, "Ks", 1.0);
    const double Km = opt_num(params, "Km", 1.0);
    const double KB = opt_num(params, "KB", 1.0);
    const double Cf = opt_num(params, "Cf", 1.0);
    const double Cp = opt_num(params, "Cp", 191.0);

    if (face_width <= 0 || module <= 0 || pitch_diameter <= 0)
        throw MechError("face_width, module, and pitch_diameter must be positive.");
    if (J <= 0 || I <= 0)
        throw MechError("Geometry factors J and I must be positive.");
    if (Wt <= 0)
        throw MechError("Tangential load Wt must be positive.");

    // AGMA bending stress (Shigley's Eq. 14-15, SI form).
    const double sigma_b = Wt * Ko * Kv * Ks * (Km * KB / J) / (face_width * module);
    // AGMA contact stress (Shigley's Eq. 14-16, SI form).
    const double under_root = Wt * Ko * Kv * Ks * Km * Cf / (pitch_diameter * face_width * I);
    const double sigma_c = Cp * std::sqrt(under_root);

    double sf_b, sf_c;
    const json sf_b_j = opt_none_num(params, "allowable_bending_stress", sf_b)
                            ? json(sf_b / sigma_b)
                            : json();
    const json sf_c_j = opt_none_num(params, "allowable_contact_stress", sf_c)
                            ? json(sf_c / sigma_c)
                            : json();

    return {
        {"sigma_bending_MPa", sigma_b},
        {"sigma_contact_MPa", sigma_c},
        {"SF_bending", sf_b_j},
        {"SF_contact", sf_c_j},
        {"disclaimer", kDisclaimer},
    };
}

json bearing_life(const json& params) {
    const double C = req_num(params, "C");
    const double Fr = req_num(params, "Fr");
    const double Fa = opt_num(params, "Fa", 0.0);
    const double n_rpm = opt_num(params, "n_rpm", 1000.0);
    const std::string bearing_type = opt_str(params, "bearing_type", "ball");
    const double X = opt_num(params, "X", 1.0);
    const double Y = opt_num(params, "Y", 0.0);
    const std::string designation = opt_str(params, "bearing_designation", "");

    if (C <= 0)
        throw MechError(
            "Dynamic load rating C must be positive. "
            "Supply C from the bearing manufacturer's datasheet.");
    if (Fr <= 0 && Fa <= 0)
        throw MechError("At least one of Fr (radial) or Fa (axial) must be positive.");

    // btype = bearing_type.lower().strip()
    std::string btype = bearing_type;
    const size_t first = btype.find_first_not_of(" \t\n\r\f\v");
    const size_t last = btype.find_last_not_of(" \t\n\r\f\v");
    btype = first == std::string::npos ? "" : btype.substr(first, last - first + 1);
    for (char& ch : btype) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));

    double p;
    if (btype == "ball")
        p = 3.0;
    else if (btype == "roller")
        p = 10.0 / 3.0;
    else
        throw MechError("bearing_type must be 'ball' or 'roller', got '" + bearing_type + "'.");

    const double P = X * Fr + Y * Fa;
    if (P <= 0)
        throw MechError("Equivalent bearing load P = X*Fr + Y*Fa must be positive.");

    const double L10_mrev = std::pow(C / P, p);
    if (n_rpm <= 0)
        throw MechError("Shaft speed n_rpm must be positive.");
    const double L10h = 1000000.0 / (60.0 * n_rpm) * L10_mrev;

    return {
        {"L10_million_rev", L10_mrev},
        {"L10h_hours", L10h},
        {"P_equiv_N", P},
        {"p_exponent", p},
        {"bearing_designation", designation},
        {"disclaimer", kDisclaimer},
    };
}

bool call(const std::string& method, const json& params, json& result, std::string& err) {
    if (method != "gear_check" && method != "bearing_life") return false;
    err.clear();
    try {
        json out = method == "gear_check" ? gear_check(params) : bearing_life(params);
        // The MCP wrapper returns {"ok": True, **result.to_dict()}.
        json envelope = {{"ok", true}};
        envelope.update(out);
        result = std::move(envelope);
    } catch (const MechError& e) {
        const std::string msg = e.what();
        if (msg.rfind("missing or non-numeric", 0) == 0)
            result = {{"ok", false}, {"error", msg}, {"code", "invalid_input"}};
        else
            // cad_common._err(): a plain engine exception -> {"ok":false,"error"} (no code).
            result = {{"ok", false}, {"error", msg}};
    }
    return true;
}

}  // namespace mechanical
}  // namespace chudware
