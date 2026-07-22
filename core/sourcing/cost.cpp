// core/sourcing/cost.cpp — made-part cost model. Port of engine_oracle/cost_model.py:
// material_cost / machining_cost / estimate_made_cost with the provenance-carrying seed
// table, friendly aliases, and the user-config override (config.json "cost_model"
// section). No fabricated numbers: an unknown material is a contract-shaped unknown
// (ok:false), never a guessed numeral. The optional Commodities-API live-price feed is
// NOT ported (graceful no-op in the oracle without a key; the seeded rates are the
// authoritative lane — see the module docstring there).
#include "sourcing/sourcing.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>

namespace aicad {
namespace sourcing {

namespace {

const double kDefaultMachineRatePerHr = 75.0;
const double kDefaultSetupTimeS = 900.0;  // 15 min
const char* kCurrency = "USD";

struct MaterialSeed {
    double density_g_cm3;
    double rate_usd_per_kg;
    const char* citation;
    const char* asof;
};

// Provenance-carrying seed table — identical numbers/citations to the oracle.
const std::map<std::string, MaterialSeed>& material_seed() {
    static const std::map<std::string, MaterialSeed> seed = {
        {"steel", {7.85, 2.0, "hot-rolled carbon-steel bar, US mill, order-of-magnitude", "2026-01"}},
        {"stainless", {8.0, 5.5, "304 stainless bar stock, distributor list, order-of-magnitude", "2026-01"}},
        {"aluminum", {2.70, 3.5, "6061 aluminium bar stock, distributor list, order-of-magnitude", "2026-01"}},
        {"titanium", {4.43, 35.0, "Ti-6Al-4V bar stock, distributor list, order-of-magnitude", "2026-01"}},
        {"brass", {8.5, 8.0, "C360 brass bar stock, distributor list, order-of-magnitude", "2026-01"}},
        {"abs", {1.04, 3.0, "ABS pellet/stock, order-of-magnitude", "2026-01"}},
        {"pla", {1.24, 25.0, "PLA 3D-printer filament, retail spool, order-of-magnitude", "2026-01"}},
        {"cedar_lumber", {0.38, 1.2, "western red cedar dimensional lumber, retail, order-of-magnitude", "2026-01"}},
    };
    return seed;
}

const std::map<std::string, std::string>& material_aliases() {
    static const std::map<std::string, std::string> aliases = {
        {"mild steel", "steel"}, {"carbon steel", "steel"}, {"a36", "steel"}, {"1018", "steel"},
        {"ss", "stainless"}, {"304", "stainless"}, {"stainless steel", "stainless"}, {"ss304", "stainless"},
        {"aluminium", "aluminum"}, {"al", "aluminum"}, {"6061", "aluminum"}, {"alu", "aluminum"},
        {"alu_6061", "aluminum"}, {"aluminum_6061", "aluminum"},
        {"ti", "titanium"}, {"ti-6al-4v", "titanium"}, {"grade5", "titanium"},
        {"cedar", "cedar_lumber"}, {"lumber", "cedar_lumber"}, {"wood", "cedar_lumber"},
    };
    return aliases;
}

std::string lower_trim(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    const auto e = s.find_last_not_of(" \t\r\n");
    std::string out = (b == std::string::npos) ? std::string() : s.substr(b, e - b + 1);
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return out;
}

// The "cost_model" section of config.json ({} on any problem — never hard-fails).
json config_overrides() {
    const json cfg = load_config();
    const json section = cfg.value("cost_model", json());
    return section.is_object() ? section : json::object();
}

json override_map(const char* key) {
    const json m = config_overrides().value(key, json());
    return m.is_object() ? m : json::object();
}

// repr() of a Python string for error text: single quotes.
std::string py_repr(const std::string& s) { return "'" + s + "'"; }

std::string resolve_key(const std::string& material_name) {
    std::string key = lower_trim(material_name);
    const auto alias = material_aliases().find(key);
    if (alias != material_aliases().end()) key = alias->second;
    const json rates = override_map("material_rates");
    if (material_seed().count(key) || rates.contains(key)) return key;
    std::set<std::string> known;
    for (const auto& kv : material_seed()) known.insert(kv.first);
    for (auto it = rates.begin(); it != rates.end(); ++it) known.insert(it.key());
    std::string list;
    for (const std::string& k : known) list += (list.empty() ? "" : ", ") + py_repr(k);
    throw CostError(
        "unknown material " + py_repr(material_name) +
        "; no seeded rate/density and no config override. Known: [" + list + "]. Add a "
        "'cost_model.material_rates'/'cost_model.densities' entry to " + config_path() +
        " to price it — a cost is never fabricated for an unknown material.");
}

double to_double(const json& v) {
    if (v.is_number()) return v.get<double>();
    if (v.is_string()) {
        try { return std::stod(v.get<std::string>()); } catch (...) { return 0.0; }
    }
    return 0.0;
}

double default_machine_rate() {
    const json over = config_overrides().value("machine_rate_per_hr", json());
    return over.is_null() ? kDefaultMachineRatePerHr : to_double(over);
}

double default_setup_time_s() {
    const json over = config_overrides().value("setup_time_s", json());
    return over.is_null() ? kDefaultSetupTimeS : to_double(over);
}

}  // namespace

// Python round(x, 6): round-half-EVEN at 6 decimals (std::round is half-away).
double round6(double v) {
    const double scaled = v * 1e6;
    const double fl = std::floor(scaled);
    const double diff = scaled - fl;
    double r;
    if (diff > 0.5)
        r = fl + 1.0;
    else if (diff < 0.5)
        r = fl;
    else
        r = (std::fmod(fl, 2.0) == 0.0) ? fl : fl + 1.0;
    return r / 1e6;
}

double rate_per_kg(const std::string& material) {
    const std::string key = resolve_key(material);
    const json over = override_map("material_rates");
    if (over.contains(key) && !over[key].is_null()) return to_double(over[key]);
    return material_seed().at(key).rate_usd_per_kg;
}

double density_g_cm3(const std::string& material) {
    const std::string key = resolve_key(material);
    const json over = override_map("densities");
    if (over.contains(key) && !over[key].is_null()) return to_double(over[key]);
    const auto it = material_seed().find(key);
    if (it != material_seed().end()) return it->second.density_g_cm3;
    throw CostError(
        "material " + py_repr(material) + " has a configured rate but no density; add "
        "'cost_model.densities." + key + "' to " + config_path() +
        " so mass (and material cost) can be computed.");
}

double material_cost(double volume_mm3, const std::string& material) {
    if (volume_mm3 < 0)
        throw CostError("volume_mm3 must be >= 0, got " + py_repr(std::to_string(volume_mm3)));
    const double mass_kg = (volume_mm3 / 1000.0) * density_g_cm3(material) / 1000.0;
    return mass_kg * rate_per_kg(material);
}

double machining_cost(double cycle_time_s, double machine_rate_per_hr) {
    if (cycle_time_s < 0)
        throw CostError("cycle_time_s must be >= 0, got " + py_repr(std::to_string(cycle_time_s)));
    return (cycle_time_s / 3600.0) * machine_rate_per_hr;
}

json estimate_made_cost(const json& params) {
    const json volume = params.value("volume_mm3", json());
    const json material = params.value("material", json());
    const json cycle = params.value("cycle_time_s", json());
    const json process = params.value("process", json("cnc_milling"));

    int qty = 1;
    {
        const json q = params.value("quantity", json(1));
        if (q.is_number_integer() || q.is_number_unsigned()) {
            const long long v = q.get<long long>();
            if (v >= 1) qty = static_cast<int>(v);
        }
    }
    const json mrate = params.value("machine_rate_per_hr", json());
    const json ssetup = params.value("setup_time_s", json());
    const double rate = mrate.is_null() ? default_machine_rate() : to_double(mrate);
    const double setup_s = ssetup.is_null() ? default_setup_time_s() : to_double(ssetup);

    auto degraded = [&](const std::string& reason, const std::string& hint) {
        json out;
        out["ok"] = false;
        out["source"] = "model";
        out["process"] = process;
        out["material"] = material;
        out["unit_cost"] = nullptr;
        out["currency"] = kCurrency;
        out["reason"] = reason;
        out["hint"] = hint;
        return out;
    };

    if (volume.is_null() || material.is_null() || cycle.is_null()) {
        std::string missing;
        if (volume.is_null()) missing = "volume_mm3";
        if (material.is_null()) missing += (missing.empty() ? "" : ", ") + std::string("material");
        if (cycle.is_null()) missing += (missing.empty() ? "" : ", ") + std::string("cycle_time_s");
        return degraded(
            "cost model missing input(s): " + missing,
            "provide part volume (measure), material, and CAM cycle_time_s (cam_simulate) "
            "so the made-part cost can be computed without guessing.");
    }

    const std::string material_name = material.is_string() ? material.get<std::string>()
                                                           : material.dump();
    double mat, run, setup;
    json prov;
    try {
        mat = material_cost(to_double(volume), material_name);
        run = machining_cost(to_double(cycle), rate);
        setup = machining_cost(setup_s, rate) / qty;
        const std::string key = resolve_key(material_name);
        const auto seed = material_seed().find(key);
        prov["rate_usd_per_kg"] = rate_per_kg(material_name);
        prov["density_g_cm3"] = density_g_cm3(material_name);
        prov["citation"] = seed != material_seed().end() ? seed->second.citation : "config override";
        prov["asof"] = seed != material_seed().end() ? seed->second.asof : "";
    } catch (const CostError& e) {
        return degraded(e.what(),
                        "add a 'cost_model' entry (rate + density) to the config to price "
                        "this material.");
    }

    const double unit = mat + run + setup;
    json out;
    out["ok"] = true;
    out["source"] = "model";
    out["process"] = process;
    out["material"] = material;
    out["material_cost"] = round6(mat);
    out["machine_cost"] = round6(run);
    out["setup_cost"] = round6(setup);
    out["unit_cost"] = round6(unit);
    out["currency"] = kCurrency;
    out["inputs"] = {{"volume_mm3", to_double(volume)},
                     {"cycle_time_s", to_double(cycle)},
                     {"quantity", qty},
                     {"machine_rate_per_hr", rate},
                     {"setup_time_s", setup_s}};
    out["provenance"] = prov;
    return out;
}

}  // namespace sourcing
}  // namespace aicad
