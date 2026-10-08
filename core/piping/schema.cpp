// core/piping/schema.cpp — port of engine_oracle/piping/schema.py.
#include "piping/schema.h"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <set>

namespace chudware::piping {
namespace {

using pyutil::py_repr;

// ── enums (mirror the oracle's module constants) ─────────────────────────────
const std::set<std::string> kSchedules = {"5",  "5S", "10", "10S", "20", "30",
                                          "40", "40S", "60", "80", "80S", "100",
                                          "120", "140", "160", "STD", "XS", "XXS"};
const std::set<std::string> kFittingTypes = {"elbow", "tee", "reducer", "flange",
                                          "valve", "orifice", "custom"};
const std::set<std::string> kElbowRadii = {"LR", "SR"};
const std::set<std::string> kFlangeFaces = {"RF", "FF", "RTJ"};
const std::set<std::string> kMaterials = {"A106-B", "A53-B", "A312-304", "A312-316",
                                          "A333-6", "PVC", "CPVC", "copper"};
const std::set<std::string> kEndTypes = {"BW", "SW", "THD", "FLG"};

bool is_num(const ojson& v) {
    return v.is_number() && !v.is_boolean() && std::isfinite(v.get<double>());
}

bool in_num_set(const ojson& v, std::initializer_list<double> vals) {
    if (!is_num(v)) return false;
    const double d = v.get<double>();
    for (double x : vals)
        if (d == x) return true;
    return false;
}

bool is_xyz(const ojson& v) {
    if (!v.is_array() || v.size() != 3) return false;
    for (const auto& c : v)
        if (!is_num(c)) return false;
    return true;
}

// id prefix -> the document list key it indexes.
const char* prefix_key(const std::string& prefix) {
    if (prefix == "r") return "runs";
    if (prefix == "ft") return "fittings";
    return nullptr;
}

// ^<prefix>\d+$ (prefix is regex-escaped in the oracle; ours are literal).
bool id_matches(const std::string& id, const std::string& prefix, bool need_digits) {
    if (id.size() <= prefix.size()) return false;
    if (id.compare(0, prefix.size(), prefix) != 0) return false;
    const std::string rest = id.substr(prefix.size());
    if (rest.empty()) return false;
    const bool digits = std::all_of(rest.begin(), rest.end(),
                                    [](unsigned char c) { return std::isdigit(c); });
    return need_digits ? digits : digits;
}

void check_ids(const ojson& items, const std::string& prefix, const std::string& loc,
               std::vector<std::string>& errs) {
    std::set<std::string> seen;
    int i = 0;
    for (const auto& it : items) {
        const std::string li = loc + "[" + std::to_string(i) + "]";
        if (!it.is_object()) {
            errs.push_back(li + ": not an object");
            ++i;
            continue;
        }
        const ojson oid = it.value("id", ojson(nullptr));
        if (!oid.is_string() || !id_matches(oid.get<std::string>(), prefix, true)) {
            errs.push_back(li + ".id: missing or bad prefix (want " + prefix +
                           "<n>) got " + py_repr(oid));
        } else if (seen.count(oid.get<std::string>())) {
            errs.push_back(li + ".id: duplicate " + py_repr(oid));
        } else {
            seen.insert(oid.get<std::string>());
        }
        ++i;
    }
}

void check_size(const ojson& item, const std::string& loc,
                std::vector<std::string>& errs, const std::string& key = "nps",
                bool required = true) {
    const ojson v = item.value(key, ojson(nullptr));
    if (v.is_null()) {
        if (required) errs.push_back(loc + "." + key + ": required");
        return;
    }
    if (!is_num(v) || v.get<double>() <= 0) {
        errs.push_back(loc + "." + key + ": must be a positive number (inches), got " +
                       py_repr(v));
    }
}

void check_schedule(const ojson& item, const std::string& loc,
                    std::vector<std::string>& errs) {
    const ojson s = item.value("schedule", ojson(nullptr));
    if (!s.is_null() && !(s.is_string() && kSchedules.count(s.get<std::string>()))) {
        errs.push_back(loc + ".schedule: " + py_repr(s) + " not in SCHEDULES");
    }
}

void check_material(const ojson& item, const std::string& loc,
                    std::vector<std::string>& errs) {
    const ojson m = item.value("material", ojson(nullptr));
    if (!m.is_null() && !(m.is_string() && kMaterials.count(m.get<std::string>()))) {
        errs.push_back(loc + ".material: " + py_repr(m) + " not in MATERIALS");
    }
}

}  // namespace

ojson new_piping(const std::string& name) {
    ojson doc = ojson::object();
    doc["schema"] = SCHEMA_PIPING;
    doc["id"] = pyutil::uuid4();
    doc["name"] = name;
    doc["units"] = {{"system", "MMGS"}};
    doc["material"] = "A106-B";
    doc["runs"] = ojson::array();
    doc["fittings"] = ojson::array();
    return doc;
}

ojson new_pipe_run(const std::string& id, const ojson& path, const ojson& nps,
                   const std::string& schedule, const std::string& name,
                   const ojson& bore_mm) {
    ojson run = ojson::object();
    run["id"] = id;
    run["name"] = name;
    run["suppressed"] = false;
    ojson pts = ojson::array();
    for (const auto& p : path) {
        ojson pt = ojson::array();
        for (const auto& c : p) pt.push_back(pyutil::to_double(c));
        pts.push_back(std::move(pt));
    }
    run["path"] = std::move(pts);
    run["nps"] = nps;
    run["schedule"] = schedule;
    run["material"] = nullptr;
    run["bend_radius"] = nullptr;
    run["end_type"] = "BW";
    run["bore_mm"] = bore_mm;
    return run;
}

ojson new_fitting(const std::string& id, const std::string& type, const ojson& nps,
                  const ojson& origin, const std::string& schedule,
                  const std::string& name, const ojson& params) {
    ojson ft = ojson::object();
    ft["id"] = id;
    ft["type"] = type;
    ft["name"] = name;
    ft["suppressed"] = false;
    ojson o = ojson::array();
    const ojson orig = origin.is_null() ? ojson::array({0.0, 0.0, 0.0}) : origin;
    for (const auto& c : orig) o.push_back(pyutil::to_double(c));
    ft["origin"] = std::move(o);
    ft["orient"] = ojson::array({0.0, 0.0, 0.0});
    ft["nps"] = nps;
    ft["nps2"] = nullptr;
    ft["schedule"] = schedule;
    ft["material"] = nullptr;
    ft["catalog_ref"] = nullptr;
    ft["params"] = params.is_object() ? params : ojson::object();
    return ft;
}

bool is_piping(const ojson& doc) {
    return doc.is_object() && doc.value("schema", "") == SCHEMA_PIPING;
}

std::string next_id(const ojson& doc, const std::string& prefix) {
    const char* key = prefix_key(prefix);
    long n = 0;
    if (key != nullptr && doc.is_object() && doc.contains(key) && doc[key].is_array()) {
        for (const auto& it : doc[key]) {
            if (!it.is_object()) continue;
            const ojson id = it.value("id", ojson(nullptr));
            if (!id.is_string()) continue;
            const std::string s = id.get<std::string>();
            if (id_matches(s, prefix, true)) {
                n = std::max(n, std::stol(s.substr(prefix.size())));
            }
        }
    }
    return prefix + std::to_string(n + 1);
}

std::vector<std::string> validate_run(const ojson& run, const std::string& loc) {
    std::vector<std::string> errs;
    if (!run.is_object()) return {loc + ": not an object"};
    const ojson path = run.value("path", ojson(nullptr));
    if (!path.is_array() || path.size() < 2) {
        errs.push_back(loc + ".path: need >= 2 points");
    } else {
        for (size_t j = 0; j < path.size(); ++j) {
            if (!is_xyz(path[j])) {
                errs.push_back(loc + ".path[" + std::to_string(j) +
                               "]: expected [x,y,z] numbers, got " + py_repr(path[j]));
            }
        }
        for (size_t j = 0; j + 1 < path.size(); ++j) {
            const ojson& a = path[j];
            const ojson& b = path[j + 1];
            if (is_xyz(a) && is_xyz(b) && a == b) {
                errs.push_back(loc + ".path[" + std::to_string(j) +
                               "]: zero-length segment (duplicate point)");
            }
        }
        if (is_xyz(path.front()) && is_xyz(path.back()) &&
            path.front() == path.back()) {
            errs.push_back(loc + ".path: start and end points must be distinct");
        }
    }
    check_size(run, loc, errs);
    check_schedule(run, loc, errs);
    check_material(run, loc, errs);
    const ojson et = run.value("end_type", ojson(nullptr));
    if (!et.is_null() && !(et.is_string() && kEndTypes.count(et.get<std::string>()))) {
        errs.push_back(loc + ".end_type: " + py_repr(et) + " not in END_TYPES");
    }
    const ojson br = run.value("bend_radius", ojson(nullptr));
    if (!br.is_null() && (!is_num(br) || br.get<double>() <= 0)) {
        errs.push_back(loc +
                       ".bend_radius: must be a positive number (mm) or null, got " +
                       py_repr(br));
    }
    const ojson bore = run.value("bore_mm", ojson(nullptr));
    if (!bore.is_null() && (!is_num(bore) || bore.get<double>() <= 0)) {
        errs.push_back(loc +
                       ".bore_mm: must be a positive number (mm) or null, got " +
                       py_repr(bore));
    }
    return errs;
}

std::vector<std::string> validate_k_model(const ojson& model, const std::string& loc) {
    if (!model.is_object() || !model.value("kind", ojson(nullptr)).is_string())
        return {loc + ": requires a model object and kind"};
    std::vector<std::string> errs;
    const std::string kind = model["kind"].get<std::string>();
    auto number = [&](const char* key, bool positive = false) {
        const auto v = model.value(key, ojson(nullptr));
        if (!is_num(v) || (positive ? v.get<double>() <= 0 : v.get<double>() < 0))
            errs.push_back(loc + "." + key + ": requires a finite " +
                           (positive ? "positive" : "nonnegative") + " number");
    };
    if (kind == "constant") number("K");
    else if (kind == "two_k") { number("K1"); number("Kinf"); }
    else if (kind == "three_k") { number("K1"); number("Ki"); number("Kd"); }
    else if (kind == "cv") number("Cv", true);
    else if (kind == "table") {
        const auto points = model.value("points", ojson(nullptr));
        if (!points.is_array() || points.empty())
            errs.push_back(loc + ".points: requires at least one point");
        else {
            double last = 0;
            for (const auto& p : points) {
                if (!p.is_object() || !is_num(p.value("re", ojson(nullptr))) ||
                    !is_num(p.value("K", ojson(nullptr))) ||
                    p["re"].get<double>() <= last || p["K"].get<double>() < 0) {
                    errs.push_back(loc + ".points: positive increasing re and nonnegative K required");
                    break;
                }
                last = p["re"].get<double>();
            }
        }
        const auto source = model.value("source", ojson(nullptr));
        if (source != "cfd" && source != "user")
            errs.push_back(loc + ".source: requires cfd or user");
        if (model.contains("fit")) {
            auto fit = model["fit"];
            if (fit.is_object()) fit["kind"] = "two_k";
            const auto fit_errs = validate_k_model(fit, loc + ".fit");
            errs.insert(errs.end(), fit_errs.begin(), fit_errs.end());
        }
        if (model.contains("provenance") && !model["provenance"].is_object())
            errs.push_back(loc + ".provenance: requires an object");
    } else errs.push_back(loc + ".kind: unknown K model");
    return errs;
}

std::vector<std::string> validate_fitting(const ojson& ft, const std::string& loc) {
    std::vector<std::string> errs;
    if (!ft.is_object()) return {loc + ": not an object"};
    const ojson ftype = ft.value("type", ojson(nullptr));
    if (!(ftype.is_string() && kFittingTypes.count(ftype.get<std::string>()))) {
        errs.push_back(loc + ".type: " + py_repr(ftype) + " not in FITTING_TYPES");
    }
    if (!is_xyz(ft.value("origin", ojson(nullptr)))) {
        errs.push_back(loc + ".origin: expected [x,y,z] numbers, got " +
                       py_repr(ft.value("origin", ojson(nullptr))));
    }
    if (!is_xyz(ft.value("orient", ojson(nullptr)))) {
        errs.push_back(loc + ".orient: expected [x,y,z] degrees, got " +
                       py_repr(ft.value("orient", ojson(nullptr))));
    }
    check_size(ft, loc, errs);
    check_schedule(ft, loc, errs);
    check_material(ft, loc, errs);

    const bool is_reducer = ftype.is_string() && ftype.get<std::string>() == "reducer";
    if (is_reducer) {
        check_size(ft, loc, errs, "nps2", true);
    } else if (!ft.value("nps2", ojson(nullptr)).is_null()) {
        check_size(ft, loc, errs, "nps2", false);
    }

    ojson params = ft.value("params", ojson::object());
    if (!params.is_object()) {
        errs.push_back(loc + ".params: must be an object");
        params = ojson::object();
    }
    if (ftype.is_string() && ftype.get<std::string>() == "elbow") {
        const ojson ang = params.value("angle", ojson(nullptr));
        if (!ang.is_null() && (!is_num(ang) || ang.get<double>() <= 0 || ang.get<double>() >= 180)) {
            errs.push_back(loc + ".params.angle: " + py_repr(ang) + " not in ELBOW_ANGLES (0,180)");
        }
        const ojson rad = params.value("radius", ojson(nullptr));
        if (!rad.is_null() && !(rad.is_string() && kElbowRadii.count(rad.get<std::string>()))) {
            errs.push_back(loc + ".params.radius: " + py_repr(rad) + " not in ELBOW_RADII");
        }
        const auto radius_mm = params.value("radius_mm", ojson(nullptr));
        if (!radius_mm.is_null() && (!is_num(radius_mm) || radius_mm.get<double>() <= 0))
            errs.push_back(loc + ".params.radius_mm: requires a positive finite radius");
    } else if (ftype.is_string() && ftype.get<std::string>() == "flange") {
        const ojson fc = params.value("flange_class", ojson(nullptr));
        if (!fc.is_null() && !in_num_set(fc, {150, 300, 600, 900, 1500, 2500})) {
            errs.push_back(loc + ".params.flange_class: " + py_repr(fc) +
                           " not in FLANGE_CLASSES");
        }
        const ojson ff = params.value("flange_face", ojson(nullptr));
        if (!ff.is_null() && !(ff.is_string() && kFlangeFaces.count(ff.get<std::string>()))) {
            errs.push_back(loc + ".params.flange_face: " + py_repr(ff) +
                           " not in FLANGE_FACES");
        }
    }
    if (ft.contains("run_id") &&
        (!ft["run_id"].is_string() || !id_matches(ft["run_id"].get<std::string>(), "r", true)))
        errs.push_back(loc + ".run_id: requires r<n>");
    if (params.contains("k_model")) {
        const auto km = validate_k_model(params["k_model"], loc + ".params.k_model");
        errs.insert(errs.end(), km.begin(), km.end());
    } else if (ftype == "custom") {
        errs.push_back(loc + ".params.k_model: required for custom");
    }
    if (ftype == "valve") {
        static const std::set<std::string> valves = {"gate", "globe", "angle", "ball", "plug",
                                                     "butterfly", "check_swing", "check_lift"};
        const auto vt = params.value("valve_type", ojson(nullptr));
        if (!vt.is_string() || !valves.count(vt.get<std::string>()))
            errs.push_back(loc + ".params.valve_type: unknown valve type");
    }
    if (ftype == "orifice") {
        const auto beta = params.value("beta", ojson(nullptr));
        const auto bore = params.value("bore_mm", ojson(nullptr));
        if (!beta.is_null() ? !(is_num(beta) && beta.get<double>() > 0 && beta.get<double>() < 1) :
                             !(is_num(bore) && bore.get<double>() > 0))
            errs.push_back(loc + ".params: requires 0<beta<1 or positive bore_mm");
    }
    if (ftype == "tee" && params.contains("run_ids")) {
        const auto ids = params["run_ids"];
        const auto branch = params.value("branch_run", ojson(nullptr));
        if (!ids.is_array() || ids.size() != 2 || !ids[0].is_string() ||
            !ids[1].is_string() || !branch.is_string() || ids[0] == ids[1] ||
            ids[0] == branch || ids[1] == branch)
            errs.push_back(loc + ".params: tee requires two distinct run_ids and branch_run");
    }
    return errs;
}

std::vector<std::string> validate_piping(const ojson& doc) {
    std::vector<std::string> errs;
    if (!doc.is_object() || doc.value("schema", ojson(nullptr)) != SCHEMA_PIPING) {
        return {std::string("schema: expected ") + py_repr(SCHEMA_PIPING) + " got " +
                py_repr(doc.is_object() ? doc.value("schema", ojson(nullptr))
                                        : ojson(nullptr))};
    }
    if (!doc.value("id", ojson(nullptr)).is_string()) errs.push_back("id: missing");
    const ojson units = doc.value("units", ojson(nullptr));
    if (!units.is_object() || units.value("system", ojson(nullptr)) != "MMGS") {
        errs.push_back("units.system: expected 'MMGS' (path and bore dimensions are mm)");
    }
    check_material(doc, "doc", errs);

    ojson runs = doc.value("runs", ojson::array());
    ojson fittings = doc.value("fittings", ojson::array());
    if (!runs.is_array()) {
        errs.push_back("runs: must be a list");
        runs = ojson::array();
    }
    if (!fittings.is_array()) {
        errs.push_back("fittings: must be a list");
        fittings = ojson::array();
    }
    check_ids(runs, "r", "runs", errs);
    check_ids(fittings, "ft", "fittings", errs);

    for (size_t i = 0; i < runs.size(); ++i) {
        for (auto& e : validate_run(runs[i], "runs[" + std::to_string(i) + "]"))
            errs.push_back(std::move(e));
    }
    for (size_t i = 0; i < fittings.size(); ++i) {
        for (auto& e : validate_fitting(fittings[i], "fittings[" + std::to_string(i) + "]"))
            errs.push_back(std::move(e));
    }
    std::set<std::string> run_ids;
    for (const auto& run : runs)
        if (run.is_object() && run.value("id", ojson(nullptr)).is_string())
            run_ids.insert(run["id"].get<std::string>());
    for (size_t i = 0; i < fittings.size(); ++i) {
        const auto& ft = fittings[i];
        if (!ft.is_object()) continue;
        const std::string loc = "fittings[" + std::to_string(i) + "]";
        auto reference = [&](const ojson& id, const std::string& key) {
            if (!id.is_string() || !run_ids.count(id.get<std::string>()))
                errs.push_back(loc + "." + key + ": unknown run");
        };
        if (ft.contains("run_id")) reference(ft["run_id"], "run_id");
        const auto params = ft.value("params", ojson(nullptr));
        if (params.is_object() && ft.value("type", ojson(nullptr)) == "tee" &&
            params.contains("run_ids") && params["run_ids"].is_array()) {
            for (const auto& id : params["run_ids"]) reference(id, "params.run_ids");
            reference(params.value("branch_run", ojson(nullptr)), "params.branch_run");
        }
    }
    return errs;
}

}  // namespace chudware::piping
