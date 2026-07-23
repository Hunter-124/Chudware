// ECAD session implementation — see session.h. Mirrors engine/ecad/session.py
// (bounded JSON-snapshot undo/redo, stale-revision guard, revision bumps) and
// engine/ecad/rpc.py response shapes. Pure JSON; no OCCT.

#include "ecad/session.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;

#include "ecad/autoroute.h"
#include "ecad/container.h"
#include "ecad/dfm.h"
#include "ecad/drc.h"
#include "ecad/erc.h"
#include "ecad/fabformats.h"
#include "ecad/impedance.h"
#include "ecad/kicad.h"
#include "ecad/netlist.h"
#include "ecad/schema.h"
#include "ecad/sync.h"
#include "ecad/zonefill.h"
#include "sim/netlist.h"
#include "sim/spice.h"

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
    std::map<std::string, std::string> extra_entries;  // unknown .ecad+ entries (verbatim)
};

std::mutex g_mtx;
// Owner-tracking for crash recovery (see force_release_ecad_lock / session.h).
// Mirrors the identical pattern in core/doc/session.cpp (DocLock / force_release_doc_lock).
std::atomic<bool> g_mtx_held{false};
std::thread::id g_mtx_owner;  // guarded by g_mtx (only read/written while held)

// RAII lock for the ECAD session store. Mirrors DocLock in doc/session.cpp exactly.
class EcadLock {
public:
    EcadLock() {
        g_mtx.lock();
        g_mtx_owner = std::this_thread::get_id();
        g_mtx_held.store(true, std::memory_order_release);
    }
    ~EcadLock() {
        g_mtx_held.store(false, std::memory_order_release);
        g_mtx.unlock();
    }
    EcadLock(const EcadLock&) = delete;
    EcadLock& operator=(const EcadLock&) = delete;
};

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

// Flush bounded undo stack to per-doc telemetry journal next to the .ecad+ container.
// Journal path: <doc_path>.ecad_journal.jsonl. Best-effort; never throws.
static void flush_ecad_journal(const Session& s) {
    if (s.path.empty()) return;
    try {
        fs::path journal = fs::path(s.path).replace_extension("").string() + ".ecad_journal.jsonl";
        std::ofstream fh(journal, std::ios::binary | std::ios::trunc);
        if (!fh) return;
        for (const auto& snap : s.undo_stack) {
            fh << snap << "\n";  // undo_stack entries are already serialized strings
        }
    } catch (...) {}
}

void push_undo(Session& s) {
    s.undo_stack.push_back(snapshot(s.doc));
    if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
    s.redo_stack.clear();
    flush_ecad_journal(s);
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
    EcadLock lk;
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
    EcadLock lk;
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
    EcadLock lk;
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
    EcadLock lk;
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
    EcadLock lk;
    Session& s = require_session(doc_id);
    json cf = s.last_compile.is_null() ? compile_field(s.doc) : s.last_compile;
    s.last_compile = cf;
    return json{{"revision", s.revision}, {"kind", schema::doc_kind(s.doc)},
                {"tree", s.doc}, {"compile", cf}};
}

json ecad_rebuild(const std::string& doc_id) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    return ops_response(s, json::object(), json::array());
}

bool ecad_close(const std::string& doc_id) {
    EcadLock lk;
    return g_sessions.erase(doc_id) > 0;
}

json ecad_describe(const std::string& doc_id) {
    EcadLock lk;
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
    EcadLock lk;
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
        EcadLock lk;
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

// ------------------------------------------------------------------- save / open

json ecad_save(const std::string& doc_id, const std::string& path) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    const std::string target = !path.empty() ? path : s.path;
    if (target.empty())
        throw OpError("io_error",
                      "ecad_save: no path given and document has no current path",
                      {{"reason", "missing path"}});
    std::string out = ecad_container_save(s.doc, target, s.extra_entries);
    s.path = out;
    s.dirty = false;
    return json{{"path", out}};
}

json ecad_open(const std::string& path) {
    EcadLock lk;

    // Dispatch by extension (engine/ecad/rpc.py::_ecad_open): KiCad files route to
    // their S-expr importers; .ecad+/.ecad/.json and anything else falls through to
    // the container open below. The Python ImportError ("not_available") branch does
    // not exist here — the C++ importers are always linked.
    std::string suffix;
    {
        const std::size_t dot = path.find_last_of('.');
        suffix = dot == std::string::npos ? "" : path.substr(dot);
        for (char& c : suffix) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    json doc;
    std::map<std::string, std::string> extra_entries;
    std::vector<std::string> load_warnings;
    if (suffix == ".kicad_sch" || suffix == ".kicad_pcb") {
        const char* which = suffix == ".kicad_sch" ? "import_kicad_sch" : "import_kicad_pcb";
        try {
            doc = suffix == ".kicad_sch" ? kicad::import_kicad_sch(path)
                                         : kicad::import_kicad_pcb(path);
        } catch (const std::exception& exc) {
            throw OpError("io_error",
                          std::string(which) + " failed for '" + path + "': " + exc.what(),
                          {{"path", path}});
        }
    } else {
        EcadContainerDoc loaded = ecad_container_open(path);  // throws OpError on a bad file
        doc = std::move(loaded.doc);
        extra_entries = std::move(loaded.extra);
        load_warnings = std::move(loaded.warnings);
    }

    if (!(schema::is_schematic(doc) || schema::is_board(doc))) {
        std::string sch = doc.is_object() ? doc.value("schema", std::string()) : "";
        throw OpError("invalid_op", "not an ECAD document (" + sch + ")",
                      {{"reason", "unknown schema"}});
    }

    Session s;
    s.doc_id = new_doc_id();
    s.doc = doc;                   // open_session deep-copies the doc
    s.doc["id"] = s.doc_id;        // Python uses uuid4; the session id is faithful here
    s.revision = 0;
    s.path = path;
    s.extra_entries = std::move(extra_entries);
    Session& stored = g_sessions[s.doc_id] = std::move(s);

    // ecad_open -> rebuild() (no op application, no rev bump) + doc_id + warnings.
    json resp = ops_response(stored, json::object(), json::array());
    resp["doc_id"] = stored.doc_id;
    json warnings = json::array();
    for (const std::string& w : load_warnings) warnings.push_back(w);
    resp["warnings"] = warnings;
    return resp;
}

// ------------------------------------------------------------- update_from_sch

namespace {
// swap_doc (engine/ecad/session.py::swap_doc): validate -> undo snapshot -> swap ->
// rev+1 -> compile -> ops-response (empty id_map). Caller holds g_mtx.
json swap_doc_locked(Session& s, const json& new_doc, long base_revision, const json& warnings) {
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision",
                      "base_revision " + std::to_string(base_revision) + " != current " +
                          std::to_string(s.revision),
                      {{"current_revision", s.revision}});
    std::vector<std::string> errs = schema::validate(new_doc);
    if (!errs.empty()) {
        std::string joined;
        for (std::size_t i = 0; i < errs.size() && i < 5; ++i) {
            if (i) joined += "; ";
            joined += errs[i];
        }
        throw OpError("invalid_op", "resulting document failed validation: " + joined,
                      {{"reason", errs.empty() ? "invalid document" : errs[0]}});
    }
    push_undo(s);
    s.doc = new_doc;  // deep copy (value semantics)
    s.revision += 1;
    s.dirty = true;
    return ops_response(s, json::object(), warnings);
}
}  // namespace

json ecad_swap_doc(const std::string& doc_id, const json& new_doc, long base_revision,
                   const json& warnings) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    return swap_doc_locked(s, new_doc, base_revision,
                           warnings.is_array() ? warnings : json::array());
}

json ecad_update_from_sch(const std::string& board_doc_id, const std::string& sch_doc_id, bool prune,
                          const json& place_origin, long base_revision) {
    EcadLock lk;
    Session& board_s = require_session(board_doc_id);
    Session& sch_s = require_session(sch_doc_id);

    if (!schema::is_board(board_s.doc))
        throw OpError("invalid_op", "ecad_update_from_sch: board_doc_id is not a pcb document",
                      {{"reason", "wrong doc kind"}, {"doc_id", board_doc_id}});
    if (!schema::is_schematic(sch_s.doc))
        throw OpError("invalid_op", "ecad_update_from_sch: sch_doc_id is not a sch document",
                      {{"reason", "wrong doc kind"}, {"doc_id", sch_doc_id}});

    json netlist = build_netlist(sch_s.doc);
    // Carry the schematic's id so sync records board.linked_sch = the source schematic.
    netlist["id"] = sch_s.doc.contains("id") ? sch_s.doc["id"] : json(nullptr);

    json result = update_board_from_sch(board_s.doc, netlist,
                                        place_origin.is_null() ? json(nullptr) : place_origin, prune);
    json report = result["report"];

    json warnings = json::array();
    if (report.contains("unresolved") && report["unresolved"].is_array() &&
        !report["unresolved"].empty()) {
        std::string msg = "unresolved footprints (no def found): ";
        bool first = true;
        for (const auto& pr : report["unresolved"]) {
            if (!first) msg += ", ";
            first = false;
            msg += pr[0].get<std::string>() + "(" + pr[1].get<std::string>() + ")";
        }
        warnings.push_back(msg);
    }
    if (report.contains("removed") && report["removed"].is_array() && !report["removed"].empty() &&
        !prune) {
        std::string msg = "footprints no longer in schematic (not pruned): ";
        bool first = true;
        for (const auto& r : report["removed"]) {
            if (!first) msg += ", ";
            first = false;
            msg += r.get<std::string>();
        }
        warnings.push_back(msg);
    }

    json resp = swap_doc_locked(board_s, result["mutated_board"], base_revision,
                                warnings.empty() ? json::array() : warnings);
    resp["report"] = report;
    resp["doc_id"] = board_doc_id;
    return resp;
}

json ecad_update_from_sch_inline(const json& board, const json& sch_netlist, bool prune,
                                 const json& place_origin) {
    if (!schema::is_board(board))
        throw OpError("invalid_op", "ecad_update_from_sch: board is not a pcb document",
                      {{"reason", "wrong doc kind"}});
    return update_board_from_sch(board, sch_netlist,
                                 place_origin.is_null() ? json(nullptr) : place_origin, prune);
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
        EcadLock lk;
        Session& s = require_session(params.value("doc_id", std::string()));
        return track_impedance(s.doc, ton);
    }
    throw OpError("invalid_op", "ecad_impedance: unknown mode '" + mode + "'", {{"mode", mode}});
}

// ------------------------------------------------------------- autoroute / fill

namespace {
// Translate a router result's {tracks, vias} into pcb_add_track / pcb_add_via ops,
// forwarding net/layer/width/kind verbatim (engine/ecad/rpc.py::_ecad_autoroute).
json router_result_to_ops(const json& result) {
    json ops = json::array();
    if (result.contains("tracks") && result["tracks"].is_array()) {
        for (const json& t : result["tracks"]) {
            json op = {{"op", "pcb_add_track"}, {"net", t.value("net", json(nullptr))},
                       {"layer", t.value("layer", json(nullptr))},
                       {"p1", t.contains("p1") ? t["p1"] : json(nullptr)},
                       {"p2", t.contains("p2") ? t["p2"] : json(nullptr)},
                       {"width", t.contains("width") ? t["width"] : json(nullptr)},
                       {"kind", t.value("kind", std::string("segment"))}};
            if (op["kind"] == "arc") {
                for (const char* k : {"center", "a0", "a1"})
                    if (t.contains(k)) op[k] = t[k];
            }
            ops.push_back(op);
        }
    }
    if (result.contains("vias") && result["vias"].is_array()) {
        for (const json& v : result["vias"]) {
            ops.push_back({{"op", "pcb_add_via"}, {"net", v.value("net", json(nullptr))},
                           {"at", v.contains("at") ? v["at"] : json(nullptr)},
                           {"drill", v.contains("drill") ? v["drill"] : json(nullptr)},
                           {"diameter", v.contains("diameter") ? v["diameter"] : json(nullptr)},
                           {"layers", v.contains("layers") ? v["layers"] : json(nullptr)},
                           {"type", v.value("type", std::string("through"))}});
        }
    }
    return ops;
}

// Apply a router result to the board session (one op batch -> one revision bump),
// attaching the router report + a failed-net warning. Caller holds g_mtx. Mirrors
// the tail of engine/ecad/rpc.py::_ecad_autoroute.
json apply_route_result(Session& s, const json& result, long base_revision,
                        const std::string& doc_id) {
    json report = result.contains("report") ? result["report"] : json::object();
    json ops = router_result_to_ops(result);

    json warnings = json::array();
    if (report.contains("failed") && report["failed"].is_array() && !report["failed"].empty()) {
        std::string msg =
            "nets the autorouter could not route (need manual routing or more space): ";
        bool first = true;
        for (const json& f : report["failed"]) {
            if (!first) msg += ", ";
            first = false;
            msg += f.is_string() ? f.get<std::string>() : f.dump();
        }
        warnings.push_back(msg);
    }

    // apply_ops with the optimistic-concurrency guard (mirror ecad_apply_ops body).
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision",
                      "base_revision " + std::to_string(base_revision) + " != current " +
                          std::to_string(s.revision),
                      {{"current_revision", s.revision}});
    json res = apply_ops(s.doc, ops);
    push_undo(s);
    s.doc = res["doc"];
    s.revision += 1;
    s.dirty = true;
    json resp = ops_response(s, res["id_map"],
                             res.contains("warnings") ? res["warnings"] : json::array());
    if (!warnings.empty()) {
        json merged = resp.contains("warnings") && resp["warnings"].is_array() ? resp["warnings"]
                                                                               : json::array();
        for (const json& w : warnings) merged.push_back(w);
        resp["warnings"] = merged;
    }
    resp["report"] = report;
    resp["doc_id"] = doc_id;
    return resp;
}
}  // namespace

json ecad_autoroute(const std::string& doc_id, const json& opts, long base_revision) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    if (!schema::is_board(s.doc))
        throw OpError("invalid_op", "ecad_autoroute requires a pcb document",
                      {{"reason", "wrong doc kind"}, {"doc_id", doc_id}});
    json result = autoroute(s.doc, opts.is_null() ? json::object() : opts);
    return apply_route_result(s, result, base_revision, doc_id);
}

json ecad_route_diff_pair(const std::string& doc_id, const std::string& net_p,
                          const std::string& net_n, const json& opts, long base_revision) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    if (!schema::is_board(s.doc))
        throw OpError("invalid_op", "ecad_route_diff_pair requires a pcb document",
                      {{"reason", "wrong doc kind"}, {"doc_id", doc_id}});
    json result = route_diff_pair(s.doc, net_p, net_n, opts.is_null() ? json::object() : opts);
    return apply_route_result(s, result, base_revision, doc_id);
}

json ecad_fill_zones(const std::string& doc_id, const json& resolution, long base_revision) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    if (!schema::is_board(s.doc))
        throw OpError("invalid_op", "ecad_fill_zones requires a pcb document",
                      {{"reason", "wrong doc kind"}, {"doc_id", doc_id}});

    json filled_board = (resolution.is_number())
                            ? fill_zones(s.doc, resolution.get<double>())
                            : fill_zones(s.doc);

    // fill_summary (engine/ecad/rpc.py::_zone_fill_summary): zones, zones_filled,
    // polygons, copper_area_mm2 (abs shoelace, rounded to 4dp).
    auto poly_area = [](const json& poly) -> double {
        if (!poly.is_array() || poly.size() < 3) return 0.0;
        double a = 0.0;
        std::size_t n = poly.size();
        for (std::size_t i = 0; i < n; ++i) {
            const json& p = poly[i];
            const json& q = poly[(i + 1) % n];
            double x1 = p[0].get<double>(), y1 = p[1].get<double>();
            double x2 = q[0].get<double>(), y2 = q[1].get<double>();
            a += x1 * y2 - x2 * y1;
        }
        return std::fabs(a) / 2.0;
    };
    int zones = 0, filled = 0, polys = 0;
    double area = 0.0;
    if (filled_board.contains("zones") && filled_board["zones"].is_array()) {
        zones = (int)filled_board["zones"].size();
        for (const json& z : filled_board["zones"]) {
            const json& fp = z.contains("filled_polys") && z["filled_polys"].is_array()
                                 ? z["filled_polys"]
                                 : json::array();
            if (!fp.empty()) ++filled;
            polys += (int)fp.size();
            for (const json& poly : fp) area += poly_area(poly);
        }
    }
    // round(area, 4) — match Python's round-half-to-even at 4 dp via the JSON repr.
    double rounded = std::round(area * 1e4) / 1e4;
    json summary = {{"zones", zones}, {"zones_filled", filled}, {"polygons", polys},
                    {"copper_area_mm2", rounded}};

    json resp = swap_doc_locked(s, filled_board, base_revision, json::array());
    resp["fill_summary"] = summary;
    resp["doc_id"] = doc_id;
    return resp;
}

json ecad_simulate(const std::string& doc_id, const json& study_in, const json& sim_params) {
    json nl, doc;
    {
        EcadLock lk;
        Session& s = require_session(doc_id);
        if (!schema::is_schematic(s.doc))
            throw OpError("invalid_op", "ecad_simulate requires a sch document",
                          {{"reason", "wrong doc kind"}, {"doc_id", doc_id}});
        doc = s.doc;
    }
    nl = build_netlist(doc);

    json conv = aicad::sim::netlist_to_spice_circuit(nl, sim_params);
    json circuit = conv["circuit"];
    json warns = conv["warnings"];

    if (!circuit.contains("devices") || !circuit["devices"].is_array() ||
        circuit["devices"].empty()) {
        return json{{"ok", false},
                    {"error", "no simulatable devices in the schematic"},
                    {"warnings", warns}};
    }

    json study = study_in.is_object()
                     ? study_in
                     : json{{"analyses", json::array({json{{"type", "tran"},
                                                            {"tstep", 5e-5},
                                                            {"tstop", 5e-3}}})}};

    // Default-all-nodes output (engine/sim/circuit.py::simulate + _default_probes): when
    // the study requests no probes, emit V(node) for every non-ground node + I(Vname) for
    // every independent V source, so a probe-less call still gets a useful result. The C++
    // spice_simulate evaluates ONLY the study's probes, so we must inject the defaults here.
    bool has_probes = study.contains("probes") && study["probes"].is_array() &&
                      !study["probes"].empty();
    if (!has_probes) {
        json probes = json::array();
        std::vector<std::string> seen;
        auto have = [&](const std::string& x) {
            for (const auto& s : seen)
                if (s == x) return true;
            return false;
        };
        std::vector<std::string> nodes;
        if (circuit.contains("nodes") && circuit["nodes"].is_array())
            for (const json& n : circuit["nodes"])
                nodes.push_back(n.is_string() ? n.get<std::string>() : n.dump());
        for (const json& d : circuit["devices"])
            if (d.contains("nodes") && d["nodes"].is_array())
                for (const json& n : d["nodes"]) {
                    std::string nm = n.is_string() ? n.get<std::string>() : n.dump();
                    if (std::find(nodes.begin(), nodes.end(), nm) == nodes.end()) nodes.push_back(nm);
                }
        for (const std::string& nm : nodes) {
            if (nm == "0" || nm == "gnd" || nm == "GND" || have(nm)) continue;
            seen.push_back(nm);
            probes.push_back("V(" + nm + ")");
        }
        for (const json& d : circuit["devices"])
            if (d.value("kind", std::string()) == "V")
                probes.push_back("I(" + d.value("name", std::string()) + ")");
        if (!probes.empty()) study["probes"] = probes;
    }

    // AC convenience: drive the first independent V source with a unit AC source if
    // an AC analysis is requested and no source carries an AC stimulus
    // (engine/ecad/rpc.py::_ecad_simulate).
    bool wants_ac = false;
    if (study.contains("analyses") && study["analyses"].is_array())
        for (const json& a : study["analyses"])
            if (a.is_object() && a.value("type", std::string()) == "ac") wants_ac = true;
    if (wants_ac && circuit.contains("devices")) {
        bool has_ac = false;
        for (const json& d : circuit["devices"])
            if (d.contains("params") && d["params"].is_object() && d["params"].contains("ac") &&
                !d["params"]["ac"].is_null())
                has_ac = true;
        if (!has_ac) {
            for (json& d : circuit["devices"]) {
                if (d.value("kind", std::string()) == "V") {
                    if (!d.contains("params") || !d["params"].is_object()) d["params"] = json::object();
                    d["params"]["ac"] = json{{"mag", 1.0}, {"phase", 0.0}};
                    break;
                }
            }
        }
    }

    json results = aicad::sim::spice_simulate(circuit, study);
    json conv_field = results.is_object() && results.contains("convergence")
                          ? results["convergence"]
                          : json::object();
    bool converged = conv_field.is_object() && !conv_field.empty();
    if (converged)
        for (auto it = conv_field.begin(); it != conv_field.end(); ++it)
            if (!(it.value().is_boolean() ? it.value().get<bool>() : (bool)it.value()))
                converged = false;

    return json{
        {"ok", true},
        {"converged", converged},
        {"results", results},
        {"warnings", warns},
        {"log", results.is_object() && results.contains("log") ? results["log"] : json::array()},
        {"convergence", conv_field.is_object() ? conv_field : json::object()},
        {"ms", results.is_object() && results.contains("ms") ? results["ms"] : json(nullptr)},
    };
}

json ecad_session_doc(const std::string& doc_id) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    return s.doc;  // value semantics -> deep copy
}

// ----------------------------------------------------------------- crash recovery
// Drop an orphaned g_mtx lock left held by THIS thread after a Windows structured
// exception (access violation) unwound past an EcadLock without running its C++
// destructor under /EHsc. Mirrors force_release_doc_lock in core/doc/session.cpp —
// same pattern, same safety contract, same idempotency guarantee.
// Called from the libaicad C dispatch boundary's __except filter alongside
// force_release_doc_lock and force_release_drawing_lock so ANY hardware fault releases
// ALL orphaned registry locks.
bool force_release_ecad_lock() {
    if (!g_mtx_held.load(std::memory_order_acquire)) return false;
    if (g_mtx_owner != std::this_thread::get_id()) return false;
    g_mtx_held.store(false, std::memory_order_release);
    g_mtx.unlock();
    return true;
}

}  // namespace aicad::ecad
