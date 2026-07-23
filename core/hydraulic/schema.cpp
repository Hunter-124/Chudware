// core/hydraulic/schema.cpp — port of engine_oracle/hydraulic/schema.py.
#include "hydraulic/schema.h"

#include <algorithm>
#include <cctype>
#include <set>

namespace aicad::hydraulic {
namespace {

using pyutil::py_repr;

// ISO 1219 component types.
const std::set<std::string> kComponentTypes = {
    "pump",           "cylinder_linear", "cylinder_rotary", "valve_directional",
    "valve_check",    "valve_relief",    "valve_flow_control", "accumulator",
    "reservoir",      "motor_hydraulic", "filter",          "heat_exchanger",
};
const std::set<std::string> kPortTypes = {"pressure", "tank",  "work_a",
                                          "work_b",   "drain", "generic"};

bool is_num(const ojson& v) { return v.is_number() && !v.is_boolean(); }

bool is_xy(const ojson& v) {
    if (!v.is_array() || v.size() != 2) return false;
    for (const auto& c : v)
        if (!is_num(c)) return false;
    return true;
}

const char* prefix_key(const std::string& prefix) {
    if (prefix == "c") return "components";
    if (prefix == "h") return "hoses";
    return nullptr;
}

// ^<prefix>\d+$
bool id_matches(const std::string& id, const std::string& prefix) {
    if (id.size() <= prefix.size()) return false;
    if (id.compare(0, prefix.size(), prefix) != 0) return false;
    const std::string rest = id.substr(prefix.size());
    return !rest.empty() && std::all_of(rest.begin(), rest.end(),
                                        [](unsigned char c) { return std::isdigit(c); });
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
        if (!oid.is_string() || !id_matches(oid.get<std::string>(), prefix)) {
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

}  // namespace

const std::vector<std::string>& hyd_component_types_sorted() {
    static const std::vector<std::string> types(kComponentTypes.begin(),
                                                kComponentTypes.end());
    return types;
}

bool is_component_type(const std::string& type) { return kComponentTypes.count(type) != 0; }

ojson new_hydraulic(const std::string& name) {
    ojson doc = ojson::object();
    doc["schema"] = SCHEMA_HYD;
    doc["id"] = pyutil::uuid4();
    doc["name"] = name;
    doc["units"] = {{"pressure", "bar"}, {"flow", "L/min"}, {"force", "kN"},
                    {"position", "mm"}};
    doc["components"] = ojson::array();
    doc["hoses"] = ojson::array();
    doc["annotations"] = ojson::array();
    return doc;
}

ojson new_component(const std::string& id, const std::string& type,
                    const std::string& name, double x, double y, const ojson& extra) {
    ojson comp = ojson::object();
    comp["id"] = id;
    comp["type"] = type;
    comp["name"] = name.empty() ? type : name;
    comp["position"] = ojson::array({x, y});
    comp["angle"] = 0.0;
    comp["ports"] = ojson::array();
    comp["params"] = ojson::object();
    comp["sourcing"] = {{"mpn", nullptr},         {"manufacturer", nullptr},
                        {"description", nullptr}, {"quantity", 1},
                        {"unit", "ea"},           {"category", nullptr}};
    // Absorb recognised top-level fields; everything else goes into params.
    if (extra.is_object()) {
        for (auto it = extra.begin(); it != extra.end(); ++it) {
            if (it.key() == "angle" || it.key() == "ports") {
                comp[it.key()] = it.value();
            } else {
                comp["params"][it.key()] = it.value();
            }
        }
    }
    return comp;
}

ojson new_hose(const std::string& id, const std::string& net_name,
               const ojson& connections) {
    ojson hose = ojson::object();
    hose["id"] = id;
    hose["net_name"] = net_name;
    hose["connections"] = connections.is_array() ? connections : ojson::array();
    return hose;
}

bool is_hydraulic(const ojson& doc) {
    return doc.is_object() && doc.value("schema", "") == SCHEMA_HYD;
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
            if (id_matches(s, prefix)) n = std::max(n, std::stol(s.substr(prefix.size())));
        }
    }
    return prefix + std::to_string(n + 1);
}

std::vector<std::string> validate_component(const ojson& comp, const std::string& loc) {
    std::vector<std::string> errs;
    if (!comp.is_object()) return {loc + ": not an object"};

    const ojson ctype = comp.value("type", ojson(nullptr));
    if (!(ctype.is_string() && is_component_type(ctype.get<std::string>()))) {
        errs.push_back(loc + ".type: " + py_repr(ctype) + " not in HYD_COMPONENT_TYPES");
    }

    const ojson pos = comp.value("position", ojson(nullptr));
    if (!is_xy(pos)) {
        errs.push_back(loc + ".position: expected [x, y] numbers, got " + py_repr(pos));
    }

    const ojson angle = comp.value("angle", ojson(nullptr));
    if (!angle.is_null() && !is_num(angle)) {
        errs.push_back(loc + ".angle: must be a number (degrees), got " + py_repr(angle));
    }

    const ojson ports = comp.value("ports", ojson::array());
    if (!ports.is_array()) {
        errs.push_back(loc + ".ports: must be a list");
    } else {
        for (size_t j = 0; j < ports.size(); ++j) {
            const auto& port = ports[j];
            const std::string pj =
                loc + ".ports[" + std::to_string(j) + "]";
            if (!port.is_object()) {
                errs.push_back(pj + ": not an object");
                continue;
            }
            const ojson pt = port.value("port_type", ojson(nullptr));
            if (!(pt.is_string() && kPortTypes.count(pt.get<std::string>()))) {
                errs.push_back(pj + ".port_type: " + py_repr(pt) + " not in HYD_PORT_TYPES");
            }
        }
    }

    const ojson params = comp.value("params", ojson(nullptr));
    if (!params.is_null() && !params.is_object()) {
        errs.push_back(loc + ".params: must be an object or omitted");
    }
    const ojson sourcing = comp.value("sourcing", ojson(nullptr));
    if (!sourcing.is_null() && !sourcing.is_object()) {
        errs.push_back(loc + ".sourcing: must be an object or omitted");
    }
    return errs;
}

std::vector<std::string> validate_hose(const ojson& hose,
                                       const std::vector<ojson>& component_ids,
                                       const std::string& loc) {
    std::vector<std::string> errs;
    if (!hose.is_object()) return {loc + ": not an object"};

    auto known = [&](const ojson& cid) {
        for (const auto& id : component_ids)
            if (id == cid) return true;
        return false;
    };

    const ojson conns = hose.value("connections", ojson(nullptr));
    if (!conns.is_array()) {
        errs.push_back(loc + ".connections: must be a list");
    } else {
        for (size_t j = 0; j < conns.size(); ++j) {
            const auto& conn = conns[j];
            const std::string cj = loc + ".connections[" + std::to_string(j) + "]";
            if (!conn.is_object()) {
                errs.push_back(cj + ": not an object");
                continue;
            }
            const ojson cid = conn.value("component_id", ojson(nullptr));
            if (!cid.is_null() && !known(cid)) {
                errs.push_back(cj + ".component_id: " + py_repr(cid) +
                               " not a known component");
            }
            if (!conn.contains("port")) {
                errs.push_back(cj + ".port: required");
            }
        }
    }
    return errs;
}

std::vector<std::string> validate_hydraulic(const ojson& doc) {
    std::vector<std::string> errs;
    if (!doc.is_object()) return {"doc: not an object"};
    if (doc.value("schema", ojson(nullptr)) != SCHEMA_HYD) {
        return {std::string("schema: expected ") + py_repr(SCHEMA_HYD) + " got " +
                py_repr(doc.value("schema", ojson(nullptr)))};
    }

    if (!doc.value("id", ojson(nullptr)).is_string()) {
        errs.push_back("id: missing or not a string");
    }

    ojson components = doc.value("components", ojson::array());
    ojson hoses = doc.value("hoses", ojson::array());
    if (!components.is_array()) {
        errs.push_back("components: must be a list");
        components = ojson::array();
    }
    if (!hoses.is_array()) {
        errs.push_back("hoses: must be a list");
        hoses = ojson::array();
    }

    check_ids(components, "c", "components", errs);
    check_ids(hoses, "h", "hoses", errs);

    for (size_t i = 0; i < components.size(); ++i) {
        for (auto& e : validate_component(components[i],
                                          "components[" + std::to_string(i) + "]"))
            errs.push_back(std::move(e));
    }

    std::vector<ojson> comp_ids;
    for (const auto& c : components)
        if (c.is_object()) comp_ids.push_back(c.value("id", ojson(nullptr)));
    for (size_t i = 0; i < hoses.size(); ++i) {
        for (auto& e : validate_hose(hoses[i], comp_ids, "hoses[" + std::to_string(i) + "]"))
            errs.push_back(std::move(e));
    }
    return errs;
}

}  // namespace aicad::hydraulic
