// Cross-document back-annotation — see sync.h. Faithful port of engine/ecad/sync.py.

#include "ecad/sync.h"

#include <algorithm>
#include <set>
#include <string>
#include <vector>

#include "ecad/library.h"
#include "ecad/ops.h"
#include "ecad/schema.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

// Grid placement pitch (mm) — mirrors engine/ecad/sync.py::_GRID_PITCH / _GRID_COLS.
constexpr double kGridPitch = 12.7;
constexpr int kGridCols = 8;

std::string as_str(const json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_null()) return "";
    return v.dump();
}

// engine/ecad/sync.py::_is_power_component
bool is_power_component(const json& comp) {
           std::string ref = as_str(comp.value("ref", json("")));
    std::string libid = as_str(comp.value("libid", json("")));
    return ref.rfind("#PWR", 0) == 0 || ref.rfind("#", 0) == 0 || libid.rfind("power:", 0) == 0;
}

// engine/ecad/sync.py::_on_board (missing key => on board)
bool on_board(const json& comp) {
    if (comp.contains("on_board")) return comp["on_board"].is_boolean() ? comp["on_board"].get<bool>()
                                                                        : !comp["on_board"].is_null();
    return true;
}

// engine/ecad/sync.py::_placeable
bool placeable(const json& comp) {
    if (comp.value("dnp", false)) return false;
    if (!on_board(comp)) return false;
    if (is_power_component(comp)) return false;
    if (!comp.contains("footprint") || !comp["footprint"].is_string()) return false;
    std::string fpid = comp["footprint"].get<std::string>();
    // bool(fpid.strip())
    std::string trimmed = fpid;
    auto notspace = [](unsigned char c) { return !std::isspace(c); };
    trimmed.erase(trimmed.begin(), std::find_if(trimmed.begin(), trimmed.end(), notspace));
    trimmed.erase(std::find_if(trimmed.rbegin(), trimmed.rend(), notspace).base(), trimmed.end());
    return !trimmed.empty();
}

// engine/ecad/sync.py::_ensure_net — register `net` in board["nets"] if missing.
bool ensure_net(json& board, const std::string& net) {
    if (net.empty()) return false;
    if (board.contains("nets") && board["nets"].is_array()) {
        for (const auto& n : board["nets"])
            if (n.is_object() && n.value("name", std::string()) == net) return false;
    }
    if (!board.contains("nets") || !board["nets"].is_array()) board["nets"] = json::array();
    board["nets"].push_back(json{{"name", net}, {"class", "default"}});
    return true;
}

// engine/ecad/sync.py::_fp_for_ref — find an existing board footprint (linked_symbol first, then ref).
json* fp_for_ref(json& board, const std::string& ref, const std::string& sym_id) {
    if (!board.contains("footprints") || !board["footprints"].is_array()) return nullptr;
    if (!sym_id.empty()) {
        for (auto& fp : board["footprints"])
            if (fp.is_object() && fp.contains("linked_symbol") && fp["linked_symbol"].is_string() &&
                fp["linked_symbol"].get<std::string>() == sym_id)
                return &fp;
    }
    if (!ref.empty()) {
        for (auto& fp : board["footprints"])
            if (fp.is_object() && fp.value("ref", std::string()) == ref) return &fp;
    }
    return nullptr;
}

// engine/ecad/sync.py::_pad_pin_key
std::string pad_pin_key(const json& pad) {
    if (pad.contains("pin") && !pad["pin"].is_null()) {
        std::string p = as_str(pad["pin"]);
        if (!p.empty()) return p;
    }
    return as_str(pad.value("number", json("")));
}

// engine/ecad/sync.py::_bind_pads -> (changed, bound). Mutates pads + registers nets.
std::pair<int, int> bind_pads(json& pads, const json& comp_pins, json& board) {
    int changed = 0, bound = 0;
    for (auto& pad : pads) {
        std::string key = pad_pin_key(pad);
        json new_net = nullptr;
        if (comp_pins.is_object() && comp_pins.contains(key)) {
            const json& net = comp_pins[key];
            if (net.is_string() && !net.get<std::string>().empty()) new_net = net;
        }
        if (!new_net.is_null()) {
            ensure_net(board, new_net.get<std::string>());
            ++bound;
        }
        json cur = pad.contains("net") ? pad["net"] : json(nullptr);
        if (cur != new_net) {
            pad["net"] = new_net;
            ++changed;
        }
    }
    return {changed, bound};
}

// engine/ecad/sync.py::_new_footprint
json new_footprint(json& board, const json& comp, const json& fpdef, double px, double py) {
    std::string fid = schema::next_id(board, "fp");
    json pads = fpdef.contains("pads") && !fpdef["pads"].is_null() ? fpdef["pads"] : json::array();
    std::string value = as_str(comp.value("value", json("")));
    if (value.empty()) value = as_str(fpdef.value("value", json("")));
    return json{
        {"id", fid},
        {"fpid", as_str(comp.value("footprint", json("")))},
        {"ref", as_str(comp.value("ref", json("")))},
        {"value", value},
        {"pos", json::array({px, py})},
        {"angle", 0.0},
        {"side", "top"},
        {"pads", pads},
        {"graphics", fpdef.contains("graphics") && !fpdef["graphics"].is_null() ? fpdef["graphics"]
                                                                                : json::array()},
        {"attr", fpdef.contains("attr") ? fpdef["attr"] : json("smd")},
        {"linked_symbol", as_str(comp.value("sym", json("")))},
    };
}

}  // namespace

json update_board_from_sch(const json& board_in, const json& netlist, const json& place_origin,
                           bool prune) {
    json work = board_in;  // deep copy (value semantics)
    json components = netlist.contains("components") && netlist["components"].is_array()
                          ? netlist["components"]
                          : json::array();
    // Netlists supplied directly need the same lossless-sync guarantee as session
    // schematic documents: one board footprint cannot represent two components.
    std::set<std::string> component_refs;
    for (const auto& component : components) {
        if (!component.is_object() || !component.contains("ref") ||
            !component["ref"].is_string()) continue;
        const std::string ref = component["ref"].get<std::string>();
        if (!ref.empty() && !component_refs.insert(ref).second)
            throw OpError("schematic_ref_conflict",
                          "ecad_update_from_sch refused: duplicate component ref '" + ref + "'",
                          {{"rule", "duplicate_ref"}, {"ref", ref}});
    }

    json report = {{"added", json::array()}, {"updated", json::array()}, {"removed", json::array()},
                   {"unresolved", json::array()}, {"nets_bound", 0}, {"linked_sch", nullptr}};

    // Record the back-link to the schematic if the netlist carries a doc id.
    json sch_id_j = netlist.contains("id") ? netlist["id"]
                    : netlist.contains("doc_id") ? netlist["doc_id"]
                                                 : json(nullptr);
    if (sch_id_j.is_string() && !sch_id_j.get<std::string>().empty()) {
        work["linked_sch"] = sch_id_j;
        report["linked_sch"] = sch_id_j;
    }

    // Refs the schematic still owns (placeable components).
    std::set<std::string> sch_refs;
    for (const auto& c : components)
        if (placeable(c)) sch_refs.insert(as_str(c.value("ref", json(""))));

    int placed_count = work.contains("footprints") && work["footprints"].is_array()
                           ? (int)work["footprints"].size()
                           : 0;
    double ox = 0.0, oy = 0.0;
    if (place_origin.is_array() && place_origin.size() == 2) {
        ox = place_origin[0].get<double>();
        oy = place_origin[1].get<double>();
    } else {
        json origin = work.contains("origin") && !work["origin"].is_null() ? work["origin"]
                                                                           : json::array({0.0, 0.0});
        if (!origin.is_array() || origin.size() < 2) origin = json::array({0.0, 0.0});
        ox = origin[0].get<double>();
        oy = origin[1].get<double>();
    }
    int grid_i = placed_count;

    // pass 1: add/update footprints + bind pads. Sort components by (ref, sym) — deterministic.
    std::vector<json> sorted_comps(components.begin(), components.end());
    std::stable_sort(sorted_comps.begin(), sorted_comps.end(), [](const json& a, const json& b) {
        std::string ar = as_str(a.value("ref", json(""))), br = as_str(b.value("ref", json("")));
        if (ar != br) return ar < br;
        return as_str(a.value("sym", json(""))) < as_str(b.value("sym", json("")));
    });
    int nets_bound = 0;
    std::vector<std::string> added, updated;
    for (const auto& comp : sorted_comps) {
        if (!placeable(comp)) continue;
        std::string ref = as_str(comp.value("ref", json("")));
        std::string sym_id = comp.contains("sym") ? as_str(comp["sym"]) : std::string();
        std::string fpid = as_str(comp.value("footprint", json("")));
        json comp_pins = comp.contains("pins") && comp["pins"].is_object() ? comp["pins"]
                                                                           : json::object();
        json* fp = fp_for_ref(work, ref, sym_id);
        if (fp == nullptr) {
            // ADD
            json fpdef = library::resolve_footprint(fpid);
            if (!fpdef.is_object()) {
                report["unresolved"].push_back(json::array({ref, fpid}));
                continue;
            }
            double px = ox + (grid_i % kGridCols) * kGridPitch;
            double py = oy + (grid_i / kGridCols) * kGridPitch;
            ++grid_i;
            json new_fp = new_footprint(work, comp, fpdef, px, py);
            if (!work.contains("footprints") || !work["footprints"].is_array())
                work["footprints"] = json::array();
            work["footprints"].push_back(new_fp);
            // bind pads on the just-appended footprint (a reference into work)
            json& stored = work["footprints"].back();
            auto cb = bind_pads(stored["pads"], comp_pins, work);
            nets_bound += cb.second;
            added.push_back(ref);
        } else {
            // UPDATE
            bool dirty = false;
            if (as_str(fp->value("ref", json(""))) != ref) {
                (*fp)["ref"] = ref;
                dirty = true;
            }
            std::string new_val = as_str(comp.value("value", json("")));
            if (!new_val.empty() && as_str(fp->value("value", json(""))) != new_val) {
                (*fp)["value"] = new_val;
                dirty = true;
            }
            if (!sym_id.empty()) {
                json cur_link = fp->contains("linked_symbol") ? (*fp)["linked_symbol"] : json(nullptr);
                if (!(cur_link.is_string() && cur_link.get<std::string>() == sym_id)) {
                    (*fp)["linked_symbol"] = sym_id;
                    dirty = true;
                }
            }
            json& pads = (*fp).contains("pads") && (*fp)["pads"].is_array()
                             ? (*fp)["pads"]
                             : ((*fp)["pads"] = json::array());
            auto cb = bind_pads(pads, comp_pins, work);
            nets_bound += cb.second;
            if (cb.first != 0 || dirty) updated.push_back(ref);
        }
    }
    // pass 2: footprints whose ref is no longer in the schematic.
    json survivors = json::array();
    std::vector<std::string> removed;
    if (work.contains("footprints") && work["footprints"].is_array()) {
        for (const auto& fp : work["footprints"]) {
                   std::string ref = as_str(fp.value("ref", json("")));
            if (!ref.empty() && sch_refs.find(ref) == sch_refs.end()) {
                removed.push_back(ref);
                if (prune) continue;  // drop it
            }
            survivors.push_back(fp);
        }
    }
    work["footprints"] = survivors;
    // sorted(set(...)) for added/updated/removed; sorted(list-of-lists) for unresolved.
    auto sorted_unique = [](std::vector<std::string> v) {
        std::sort(v.begin(), v.end());
        v.erase(std::unique(v.begin(), v.end()), v.end());
        json arr = json::array();
        for (const auto& s : v) arr.push_back(s);
        return arr;
    };
    report["added"] = sorted_unique(added);
    report["updated"] = sorted_unique(updated);
    report["removed"] = sorted_unique(removed);
    report["nets_bound"] = nets_bound;
    // sorted(report["unresolved"]) — list of [ref, fpid] pairs; Python sorts lists lexicographically.
    std::vector<json> unres(report["unresolved"].begin(), report["unresolved"].end());
    std::sort(unres.begin(), unres.end(), [](const json& a, const json& b) {
        // element-wise string compare of the two-element arrays
        for (std::size_t i = 0; i < std::min(a.size(), b.size()); ++i) {
            std::string as = a[i].is_string() ? a[i].get<std::string>() : a[i].dump();
            std::string bs = b[i].is_string() ? b[i].get<std::string>() : b[i].dump();
            if (as != bs) return as < bs;
        }
        return a.size() < b.size();
    });
    json unres_arr = json::array();
    for (const auto& u : unres) unres_arr.push_back(u);
    report["unresolved"] = unres_arr;

    return json{{"mutated_board", work}, {"report", report}};
}

}  // namespace chudware::ecad
