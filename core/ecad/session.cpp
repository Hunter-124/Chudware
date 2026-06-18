// ECAD session implementation — see session.h. Mirrors engine/ecad/session.py
// (bounded JSON-snapshot undo/redo, stale-revision guard, revision bumps) and
// engine/ecad/rpc.py response shapes. Pure JSON; no OCCT.

#include "ecad/session.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include "ecad/dfm.h"
#include "ecad/drc.h"
#include "ecad/erc.h"
#include "ecad/fabformats.h"
#include "ecad/impedance.h"
#include "ecad/netlist.h"
#include "ecad/schema.h"
#include "ecad/zonefill.h"

namespace aicad::ecad {

using nlohmann::json;

namespace {

constexpr std::size_t kUndoLimit = 100;

struct Session {
    std::string doc_id;
    json doc;
    long revision = 0;
    std::string path;
    bool dirty = false;
    std::vector<std::string> undo_stack;  // canonical JSON snapshots (doc only)
    std::vector<std::string> redo_stack;
    json last_compile = nullptr;
};

std::mutex g_mtx;
std::map<std::string, Session> g_sessions;
std::atomic<unsigned long long> g_seq{1};

std::string new_doc_id() {
    unsigned long long n = g_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "ecadcpp%08llx", n);
    return std::string(buf);
}

Session& require_session(const std::string& doc_id) {
    auto it = g_sessions.find(doc_id);
    if (it == g_sessions.end())
        throw OpError("unknown_doc", "no open ECAD document '" + doc_id + "'", {{"doc_id", doc_id}});
    return it->second;
}

// Canonical snapshot (sorted keys, compact) — same intent as Python's
// json.dumps(sort_keys=True, separators=(",", ":")). nlohmann::json::parse round-
// trips it back to an identical object.
std::string snapshot(const json& doc) { return doc.dump(); }

void push_undo(Session& s) {
    s.undo_stack.push_back(snapshot(s.doc));
    if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
    s.redo_stack.clear();
}

// Interactive compile uses a COARSER zone-fill grid than the export path
// (engine.ecad.zonefill.DEFAULT_RESOLUTION = 0.1mm) — mirrors
// engine/ecad/session.py::_INTERACTIVE_FILL_RESOLUTION.
constexpr double kInteractiveFillResolution = 0.25;

// Recompute every copper zone's filled_polys IN PLACE on `doc`
// (engine/ecad/session.py::_fill_zones_into). Returns the number of zones filled,
// or -1 if there were no zones. fill_zones is pure (returns a filled copy); we
// copy each zone's freshly-computed filled_polys back onto the working doc.
int fill_zones_into(json& doc, double resolution) {
    if (!doc.contains("zones") || !doc["zones"].is_array() || doc["zones"].empty()) return -1;
    json filled = fill_zones(doc, resolution);
    std::map<json, json> by_id;
    if (filled.contains("zones") && filled["zones"].is_array()) {
        for (const auto& z : filled["zones"])
            by_id[z.contains("id") ? z["id"] : json(nullptr)] =
                z.contains("filled_polys") ? z["filled_polys"] : json::array();
    }
    int n = 0;
    for (auto& zone : doc["zones"]) {
        json key = zone.contains("id") ? zone["id"] : json(nullptr);
        auto it = by_id.find(key);
        zone["filled_polys"] = (it != by_id.end()) ? it->second : json::array();
        ++n;
    }
    return n;
}

// The `compile` field (engine/ecad/session.py::_compile). SCHEMATIC: netlist + ERC.
// BOARD: refresh zone fills in place (so `tree` carries current filled_polys), then
// DRC + ratsnest. `stats.ms` is omitted (non-deterministic); the harness compares
// compile.netlist + compile.erc (sch) and compile.drc + compile.ratsnest +
// zone.filled_polys (pcb).
json compile_field(json& doc) {
    json out = {{"netlist", nullptr}, {"erc", json::array()}, {"drc", json::array()},
                {"ratsnest", json::array()}, {"stats", json::object()}};
    if (schema::is_schematic(doc)) {
        json netlist = build_netlist(doc);
        out["netlist"] = netlist;
        out["erc"] = run_erc(doc, netlist);
        out["stats"]["nets"] = netlist.contains("nets") && netlist["nets"].is_object()
                                   ? netlist["nets"].size()
                                   : 0;
    } else if (schema::is_board(doc)) {
        int zfilled = fill_zones_into(doc, kInteractiveFillResolution);
        if (zfilled >= 0) out["stats"]["zones_filled"] = zfilled;
        out["drc"] = run_drc(doc);
        out["ratsnest"] = ratsnest(doc);
        out["stats"]["nets"] = doc.contains("nets") && doc["nets"].is_array() ? doc["nets"].size() : 0;
        out["stats"]["unrouted"] = out["ratsnest"].size();
    }
    return out;
}

json ops_response(Session& s, const json& id_map, const json& warnings) {
    json cf = compile_field(s.doc);
    s.last_compile = cf;
    json resp = {{"revision", s.revision}, {"id_map", id_map}, {"kind", schema::doc_kind(s.doc)},
                 {"tree", s.doc}, {"compile", cf}};
    if (warnings.is_array() && !warnings.empty()) resp["warnings"] = warnings;
    return resp;
}

}  // namespace

json ecad_new(const std::string& kind, const std::string& name) {
    std::lock_guard<std::mutex> lk(g_mtx);
    json doc;
    if (kind == "sch")
        doc = schema::new_schematic(name.empty() ? "Schematic1" : name);
    else if (kind == "pcb")
        doc = schema::new_board(name.empty() ? "Board1" : name);
    else
        throw OpError("invalid_op", "doc kind must be sch|pcb, got '" + kind + "'",
                      {{"reason", "bad kind"}});
    Session s;
    s.doc_id = new_doc_id();
    s.doc = std::move(doc);
    s.doc["id"] = s.doc_id;  // Python uses a uuid4; the session id is faithful here
    s.revision = 0;
    Session& stored = g_sessions[s.doc_id] = std::move(s);
    // ecad_new -> rebuild() (no op application, no rev bump) + doc_id. Compile FIRST
    // (it may write filled_polys into the doc) so `tree` carries the current fills.
    json cf = compile_field(stored.doc);
    stored.last_compile = cf;
    json resp = {{"revision", stored.revision}, {"id_map", json::object()},
                 {"kind", schema::doc_kind(stored.doc)}, {"tree", stored.doc},
                 {"compile", cf}};
    resp["doc_id"] = stored.doc_id;
    return resp;
}

json ecad_apply_ops(const std::string& doc_id, long base_revision, const json& ops) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision",
                      "base_revision " + std::to_string(base_revision) + " != current " +
                          std::to_string(s.revision),
                      {{"current_revision", s.revision}});

    json res = apply_ops(s.doc, ops.is_null() ? json::array() : ops);  // throws on failure

    push_undo(s);
    s.doc = res["doc"];
    s.revision += 1;
    s.dirty = true;
    return ops_response(s, res["id_map"],
                        res.contains("warnings") ? res["warnings"] : json::array());
}

json ecad_undo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.undo_stack.empty())
        throw OpError("invalid_op", "nothing to undo", {{"reason", "empty undo stack"}});
    s.redo_stack.push_back(snapshot(s.doc));
    std::string snap = s.undo_stack.back();
    s.undo_stack.pop_back();
    s.doc = json::parse(snap);
    s.revision += 1;
    s.dirty = true;
    return ops_response(s, json::object(), json::array());
}

json ecad_redo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.redo_stack.empty())
        throw OpError("invalid_op", "nothing to redo", {{"reason", "empty redo stack"}});
    s.undo_stack.push_back(snapshot(s.doc));
    if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
    std::string snap = s.redo_stack.back();
    s.redo_stack.pop_back();
    s.doc = json::parse(snap);
    s.revision += 1;
    s.dirty = true;
    return ops_response(s, json::object(), json::array());
}

json ecad_tree(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json cf = s.last_compile.is_null() ? compile_field(s.doc) : s.last_compile;
    s.last_compile = cf;
    return json{{"revision", s.revision}, {"kind", schema::doc_kind(s.doc)},
                {"tree", s.doc}, {"compile", cf}};
}

json ecad_rebuild(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    return ops_response(s, json::object(), json::array());
}

bool ecad_close(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    return g_sessions.erase(doc_id) > 0;
}

json ecad_describe(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    const json& doc = s.doc;
    json compile_info = s.last_compile.is_null() ? json::object() : s.last_compile;
    int nets = 0;
    if (compile_info.is_object() && compile_info.contains("stats") &&
        compile_info["stats"].contains("nets"))
        nets = compile_info["stats"]["nets"].get<int>();
    int erc_violations = 0;
    if (compile_info.is_object() && compile_info.contains("erc") && compile_info["erc"].is_array())
        erc_violations = (int)compile_info["erc"].size();
    int drc_violations = 0;
    if (compile_info.is_object() && compile_info.contains("drc") && compile_info["drc"].is_array())
        drc_violations = (int)compile_info["drc"].size();
    int unrouted = 0;
    if (compile_info.is_object() && compile_info.contains("stats") &&
        compile_info["stats"].contains("unrouted"))
        unrouted = compile_info["stats"]["unrouted"].get<int>();
    auto len = [&](const char* k) -> int {
        return doc.contains(k) && doc[k].is_array() ? (int)doc[k].size() : 0;
    };
    if (schema::is_schematic(doc)) {
        return json{{"kind", "sch"}, {"name", doc.value("name", std::string())},
                    {"revision", s.revision}, {"symbols", len("symbols")}, {"wires", len("wires")},
                    {"nets", nets}, {"erc_violations", erc_violations}};
    }
    return json{{"kind", "pcb"}, {"name", doc.value("name", std::string())},
                {"revision", s.revision}, {"footprints", len("footprints")},
                {"tracks", len("tracks")}, {"vias", len("vias")}, {"nets", nets},
                {"unrouted", unrouted}, {"drc_violations", drc_violations}};
}

json ecad_schema(const std::string& kind) {
    if (kind == "pcb") return schema::new_board("Board");
    return schema::new_schematic("Schematic");
}

// Internal: build the {violations, summary} response from a board doc + rules.
static json dfm_response(const json& board, const json& rules) {
    json violations = run_dfm(board, rules.is_null() ? json(nullptr) : rules);

    // Group violations by rule (same shape as the Python rpc summary).
    int errors = 0, warnings = 0;
    for (const auto& v : violations) {
        if (v.value("severity", std::string()) == "error")
            ++errors;
        else
            ++warnings;
    }
    // by_rule keyed by rule, preserving first-seen order then re-sorted at the end.
    std::vector<std::string> rule_order;
    std::map<std::string, json> by_rule;
    for (const auto& v : violations) {
        std::string rule = v.value("rule", std::string("?"));
        std::string sev = v.value("severity", std::string());
        if (!by_rule.count(rule)) {
            rule_order.push_back(rule);
            by_rule[rule] = json{{"rule", rule}, {"severity", sev}, {"count", 0},
                                 {"messages", json::array()}};
        }
        json& slot = by_rule[rule];
        slot["count"] = slot["count"].get<int>() + 1;
        if (sev == "error") slot["severity"] = "error";
        if (slot["messages"].size() < 3) slot["messages"].push_back(v.value("message", std::string()));
    }
    std::vector<json> groups;
    for (const auto& r : rule_order) groups.push_back(by_rule[r]);
    std::stable_sort(groups.begin(), groups.end(), [](const json& a, const json& b) {
        bool ae = a["severity"].get<std::string>() != "error";
        bool be = b["severity"].get<std::string>() != "error";
        if (ae != be) return !ae;  // errors (false) sort first
        return a["rule"].get<std::string>() < b["rule"].get<std::string>();
    });
    json by_rule_arr = json::array();
    for (const auto& g : groups) by_rule_arr.push_back(g);

    json summary = {{"errors", errors}, {"warnings", warnings}, {"by_rule", by_rule_arr}};
    return json{{"violations", violations}, {"summary", summary}};
}

json ecad_dfm(const std::string& doc_id, const json& rules) {
    // Inline-board fast path (parity harness passes the board verbatim via rules
    // wrapper is not used; the libaicad dispatch passes a board when doc_id empty).
    // Standard path: look up the open session by doc_id.
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (!schema::is_board(s.doc))
        throw OpError("invalid_op", "ecad_dfm requires a pcb document",
                      {{"reason", "wrong doc kind"}, {"doc_id", doc_id}});
    return dfm_response(s.doc, rules);
}

// Stateless inline-board variant used by the parity harness (no session needed).
json ecad_dfm_board(const json& board, const json& rules) {
    if (!schema::is_board(board))
        throw OpError("invalid_op", "ecad_dfm requires a pcb document",
                      {{"reason", "wrong doc kind"}});
    return dfm_response(board, rules);
}

json ecad_export(const json& params) {
    std::string kind = params.value("kind", std::string());
    json opts = params.contains("opts") && params["opts"].is_object() ? params["opts"]
                                                                       : json::object();

    // resolve the board doc: stateless inline `board` wins (parity-harness path),
    // else the session document.
    json board;
    if (params.contains("board") && params["board"].is_object()) {
        board = params["board"];
    } else {
        std::lock_guard<std::mutex> lk(g_mtx);
        Session& s = require_session(params.value("doc_id", std::string()));
        board = s.doc;
    }

    if (kind == "gerber" || kind == "drill" || kind == "bom" || kind == "cpl") {
        if (!schema::is_board(board))
            throw OpError("invalid_op", kind + " export requires a pcb document",
                          {{"reason", "wrong doc kind"}});
    } else {
        throw OpError("invalid_op", "ecad_export: unknown kind '" + kind + "'", {{"kind", kind}});
    }

    json files = json::object();
    if (kind == "gerber") {
        for (auto& kv : fab::write_gerbers(board)) files[kv.first] = kv.second;
    } else if (kind == "drill") {
        for (auto& kv : fab::write_drill(board)) files[kv.first] = kv.second;
    } else if (kind == "bom") {
        std::string fmt = opts.value("fmt", std::string("csv"));
        bool excl = opts.contains("exclude_dnp") && opts["exclude_dnp"].is_boolean()
                        ? opts["exclude_dnp"].get<bool>()
                        : true;
        files["bom"] = fab::write_bom(board, fmt, excl);
    } else if (kind == "cpl") {
        bool excl_tht = opts.contains("exclude_tht") && opts["exclude_tht"].is_boolean()
                            ? opts["exclude_tht"].get<bool>()
                            : true;
        bool excl_virt = opts.contains("exclude_virtual") && opts["exclude_virtual"].is_boolean()
                             ? opts["exclude_virtual"].get<bool>()
                             : true;
        files["cpl"] = fab::write_placement(board, excl_tht, excl_virt);
    }
    return json{{"files", files}};
}

json ecad_impedance(const json& params) {
    auto getd = [&](const char* k, double dflt) -> double {
        return params.contains(k) && params[k].is_number() ? params[k].get<double>() : dflt;
    };
    std::string mode = params.value("mode", std::string("microstrip"));
    if (mode == "microstrip")
        return microstrip_z0(getd("w", 0), getd("h", 0), getd("t", 0), getd("er", 0));
    if (mode == "stripline")
        return stripline_z0(getd("w", 0), getd("h", 0), getd("t", 0), getd("er", 0));
    if (mode == "coplanar")
        return coplanar_z0(getd("w", 0), getd("s", 0), getd("h", 0), getd("t", 0), getd("er", 0));
    if (mode == "diff_pair")
        return diff_pair_z0(getd("w", 0), getd("s", 0), getd("h", 0), getd("t", 0), getd("er", 0),
                            params.value("kind", std::string("microstrip")));
    if (mode == "track") {
        json ton = params.contains("track_or_net") ? params["track_or_net"] : json(nullptr);
        if (params.contains("board") && params["board"].is_object())
            return track_impedance(params["board"], ton);  // stateless inline path
        std::lock_guard<std::mutex> lk(g_mtx);
        Session& s = require_session(params.value("doc_id", std::string()));
        return track_impedance(s.doc, ton);
    }
    throw OpError("invalid_op", "ecad_impedance: unknown mode '" + mode + "'", {{"mode", mode}});
}

}  // namespace aicad::ecad
