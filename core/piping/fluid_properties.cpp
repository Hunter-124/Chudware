// core/piping/fluid_properties.cpp — port of engine_oracle/piping/properties.py
// (built-in cited backend: tabulated IAPWS/NIST liquid water; ideal-gas +
// Sutherland gases). Envelope strings match the oracle byte-for-byte.
#include "piping/fluid_properties.h"

#include <algorithm>
#include <cctype>

namespace chudware::piping {
namespace {

using pyutil::fmt_f;
using pyutil::jerr;
using pyutil::py_float;
using pyutil::py_repr;
using pyutil::pyround;

// Ideal-gas molar mass M (kg/mol) + Sutherland viscosity constants
// (White, "Viscous Fluid Flow" 3rd ed. Table 1-2; CRC Handbook / Sutherland-fit
// of NIST data for methane; IUPAC 2021 molar masses).
struct GasModel { double M, mu_ref, T_ref, S; };
const GasModel* gas_model(const std::string& canon) {
    static const GasModel kAir{0.0289647, 1.716e-5, 273.15, 110.4};
    static const GasModel kMethane{0.0160425, 1.024e-5, 273.15, 164.0};
    static const GasModel kNitrogen{0.0280134, 1.663e-5, 273.15, 111.0};
    static const GasModel kCO2{0.0440095, 1.370e-5, 273.15, 222.0};
    if (canon == "air") return &kAir;
    if (canon == "methane") return &kMethane;
    if (canon == "nitrogen") return &kNitrogen;
    if (canon == "carbon_dioxide") return &kCO2;
    return nullptr;
}
// Natural gas: pipeline-representative pseudo-composition (G=0.60 vs air).
const GasModel kNaturalGas{0.0179, 1.10e-5, 273.15, 164.0};

// Built-in liquid-water reference table (1 atm), 10 °C steps, 0–100 °C.
// rho & pvap: IAPWS-IF97 / NIST Chemistry WebBook saturation line;
// mu: IAPWS 2008 formulation / CRC Handbook 97th ed.
const double kWaterTC[] = {0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100};
const double kWaterRho[] = {999.84, 999.70, 998.21, 995.65, 992.22, 988.03,
                            983.20, 977.76, 971.79, 965.31, 958.35};
const double kWaterMu[] = {1.7916e-3, 1.3060e-3, 1.0016e-3, 0.7972e-3, 0.6527e-3,
                           0.5465e-3, 0.4660e-3, 0.4035e-3, 0.3540e-3, 0.3142e-3,
                           0.2818e-3};
const double kWaterPvap[] = {611.3, 1228.0, 2339.0, 4246.0, 7384.0, 12350.0,
                             19940.0, 31190.0, 47390.0, 70140.0, 101325.0};
const char* kWaterSource =
    "IAPWS-IF97 / NIST Chemistry WebBook (rho, pvap); "
    "IAPWS 2008 / CRC Handbook 97th ed. (mu)";

double interp(double x, const double* xs, const double* ys, int n) {
    if (x <= xs[0]) return ys[0];
    if (x >= xs[n - 1]) return ys[n - 1];
    for (int i = 0; i < n - 1; ++i) {
        if (xs[i] <= x && x <= xs[i + 1]) {
            const double f = (x - xs[i]) / (xs[i + 1] - xs[i]);
            return ys[i] + f * (ys[i + 1] - ys[i]);
        }
    }
    return ys[n - 1];
}

ojson builtin_props(const std::string& canon, double T, double P) {
    if (canon == "water") {
        const double Tc = T - T0_C;
        if (Tc < kWaterTC[0] - 0.5 || Tc > kWaterTC[10] + 0.5) {
            return jerr("water property table covers 0–100 °C; T=" + fmt_f(Tc, 1) +
                            " °C is out of range",
                        "install CoolProp (MIT) for the full IAPWS-IF97 range");
        }
        ojson out = ojson::object();
        out["rho"] = interp(Tc, kWaterTC, kWaterRho, 11);
        out["mu"] = interp(Tc, kWaterTC, kWaterMu, 11);
        out["pvap"] = interp(Tc, kWaterTC, kWaterPvap, 11);
        out["source"] = std::string("built-in table [") + kWaterSource +
                        "], linear interp @ " + fmt_f(Tc, 1) + " °C";
        return out;
    }
    // gases
    const GasModel* g = nullptr;
    std::string src;
    if (canon == "natural_gas") {
        g = &kNaturalGas;
        src = "ideal-gas rho (M=0.0179 kg/mol, G=0.60 pipeline gas, ISO 6976 basis); "
              "Sutherland mu (methane-dominant, White VFF Table 1-2)";
    } else {
        g = gas_model(canon);
        if (g == nullptr) {
            return jerr("no built-in property model for " + py_repr(canon),
                        "supported: water, air, methane, natural_gas, nitrogen, "
                        "carbon_dioxide");
        }
        src = "ideal-gas rho (M=" + py_float(g->M) + " kg/mol, IUPAC 2021); "
              "Sutherland mu (mu_ref=" + py_float(g->mu_ref) + " @ " +
              py_float(g->T_ref) + " K, S=" + py_float(g->S) +
              " K; White VFF Table 1-2 / CRC)";
    }
    if (T <= 0) {
        return jerr("absolute temperature must be > 0 K (got " + py_float(T) + ")");
    }
    const double rho = P * g->M / (R_UNIVERSAL * T);  // ideal gas, Z≈1
    const double mu =
        g->mu_ref * std::pow(T / g->T_ref, 1.5) * (g->T_ref + g->S) / (T + g->S);
    ojson out = ojson::object();
    out["rho"] = rho;
    out["mu"] = mu;
    out["pvap"] = nullptr;
    out["source"] = src;
    out["molar_mass"] = g->M;
    return out;
}

}  // namespace

std::string canon_fluid(const std::string& name) {
    std::string key = name;
    // str(name).strip().lower()
    key.erase(key.begin(),
              std::find_if(key.begin(), key.end(),
                           [](unsigned char c) { return !std::isspace(c); }));
    key.erase(std::find_if(key.rbegin(), key.rend(),
                           [](unsigned char c) { return !std::isspace(c); })
                  .base(),
              key.end());
    std::transform(key.begin(), key.end(), key.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    if (key == "water" || key == "h2o") return "water";
    if (key == "air") return "air";
    if (key == "methane" || key == "ch4") return "methane";
    if (key == "natural_gas" || key == "natural gas" || key == "naturalgas" ||
        key == "ng")
        return "natural_gas";
    if (key == "nitrogen" || key == "n2") return "nitrogen";
    if (key == "carbon_dioxide" || key == "carbon dioxide" || key == "co2")
        return "carbon_dioxide";
    return "";
}

ojson fluid_properties(const ojson& name, double T, double P, const ojson& prefer) {
    (void)prefer;  // every backend here is the cited built-in model
    const std::string canon =
        name.is_string() ? canon_fluid(name.get<std::string>())
                         : canon_fluid(pyutil::py_str(name));
    if (canon.empty()) {
        return jerr("unknown fluid " + py_repr(name),
                    "supported: water, air, methane, natural_gas, nitrogen, "
                    "carbon_dioxide");
    }
    if (T <= 0) {
        return jerr("absolute temperature must be > 0 K (got " + py_float(T) + ")",
                    "pass T in kelvin, e.g. T=293.15 for 20 °C");
    }
    if (P <= 0) {
        return jerr("absolute pressure must be > 0 Pa (got " + py_float(P) + ")",
                    "pass P in pascals absolute, e.g. P=101325 for 1 atm");
    }

    ojson props = builtin_props(canon, T, P);
    const std::string backend = "builtin";
    if (props.value("ok", true) == false && props.contains("reason")) {
        return props;  // out-of-range from builtin
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["name"] = canon;
    out["phase"] = (canon == "water") ? "liquid" : "gas";
    out["T_K"] = pyround(T, 4);
    out["P_Pa"] = pyround(P, 3);
    out["rho"] = props["rho"];
    out["mu"] = props["mu"];
    out["pvap"] = props.value("pvap", ojson(nullptr));
    out["molar_mass"] = props.value("molar_mass", ojson(nullptr));
    out["source"] = props["source"];
    out["backend"] = backend;
    return out;
}

ojson resolve_solver_fluid(const ojson& fluid) {
    if (fluid.is_null()) {
        const ojson p = fluid_properties("water", 293.15, P_ATM);
        ojson out = ojson::object();
        out["name"] = "water";
        out["rho"] = p["rho"];
        out["mu"] = p["mu"];
        out["pvap"] = p["pvap"];
        out["phase"] = "liquid";
        out["source"] = p["source"];
        out["backend"] = p["backend"];
        out["T_K"] = 293.15;
        out["P_Pa"] = P_ATM;
        out["molar_mass"] = nullptr;
        return out;
    }
    if (fluid.is_object() && fluid.contains("rho") && fluid.contains("mu")) {
        ojson out = fluid;  // used verbatim (explicit override)
        if (!out.contains("name")) out["name"] = fluid.value("name", std::string("custom"));
        if (!out.contains("source")) out["source"] = "caller-supplied rho/mu";
        if (!out.contains("pvap")) out["pvap"] = fluid.value("pvap", ojson(nullptr));
        if (!out.contains("phase")) out["phase"] = fluid.value("phase", std::string("liquid"));
        return out;
    }
    if (fluid.is_object() && fluid.contains("name")) {
        double T = 293.15;
        bool have_T = false;
        if (fluid.contains("T") && !fluid["T"].is_null()) {
            T = pyutil::to_double(fluid["T"]);
            have_T = true;
        }
        if (!have_T && fluid.contains("T_C")) {
            T = pyutil::to_double(fluid["T_C"]) + T0_C;
            have_T = true;
        }
        const double P = fluid.contains("P") ? pyutil::to_double(fluid["P"]) : P_ATM;
        const ojson prefer = fluid.value("prefer", ojson(nullptr));
        const ojson p = fluid_properties(fluid["name"], T, P, prefer);
        if (!p.value("ok", false)) return p;
        ojson out = ojson::object();
        out["name"] = p["name"];
        out["rho"] = p["rho"];
        out["mu"] = p["mu"];
        out["pvap"] = p["pvap"];
        out["phase"] = p["phase"];
        out["source"] = p["source"];
        out["backend"] = p["backend"];
        out["T_K"] = p["T_K"];
        out["P_Pa"] = p["P_Pa"];
        out["molar_mass"] = p["molar_mass"];
        return out;
    }
    return jerr("fluid must be null, {'rho','mu'} or {'name', 'T', 'P'}",
                "e.g. {'name':'air','T':293.15,'P':500000} or "
                "{'rho':998.2,'mu':1.002e-3}");
}

}  // namespace chudware::piping
