// core/piping/schema.cpp — port of engine_oracle/piping/schema.py.
#include "piping/schema.h"

#include <algorithm>
#include <cctype>
#include <set>

namespace aicad::piping {
namespace {

using pyutil::py_repr;

// ── enums (mirror the oracle's module constants) ─────────────────────────────
const std::set<std::string> kSchedules = {"5",  "5S", "10", "10S", "20", "30",
                                          "40", "40S", "60", "80", "80S", "100",
                                          "120", "140", "160", "STD", "XS", "XXS"};
const std::set<std::string> kFittingTypes = {"elbow", "tee", "reducer", "flange"};
const std::set<std::string> kElbowRadii = {"LR", "SR"};
const std::set<std::string> kFlangeFaces = {"RF", "FF", "RTJ"};
const std::set<std::string> kMaterials = {"A106-B", "A53-B", "A312-304", "A312-316",
                                          "A333-6", "PVC", "CPVC", "copper"};
const std::set<std::string> kEndTypes = {"BW", "SW", "THD", "FLG"};

bool is_num(const ojson& v) { return v.is_number() && !v.is_boolean(); }

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
        if (!ang.is_null() && !in_num_set(ang, {45, 90})) {
            errs.push_back(loc + ".params.angle: " + py_repr(ang) + " not in ELBOW_ANGLES");
        }
        const ojson rad = params.value("radius", ojson(nullptr));
        if (!rad.is_null() && !(rad.is_string() && kElbowRadii.count(rad.get<std::string>()))) {
            errs.push_back(loc + ".params.radius: " + py_repr(rad) + " not in ELBOW_RADII");
        }
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
    return errs;
}

}  // namespace aicad::piping
