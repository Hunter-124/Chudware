// ECAD session implementation — see session.h. Mirrors engine/ecad/session.py
// (bounded JSON-snapshot undo/redo, stale-revision guard, revision bumps) and
// engine/ecad/rpc.py response shapes. Pure JSON; no OCCT.

#include "ecad/session.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cerrno>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

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
#include "ecad/recipes.h"
#include "ecad/schema.h"
#include "ecad/schexport.h"
#include "ecad/schplot.h"
#include "ecad/symlib.h"
#include "ecad/sync.h"
#include "ecad/zonefill.h"
#include "sim/netlist.h"
#include "sim/sim_cancel.h"
#include "sim/spice.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

constexpr std::size_t kUndoLimit = 100;

struct Session {
    std::string doc_id;
    json doc;
    long revision = 0;
    std::string path;          // native .ecad+ save target ("" until first save)
    std::string source_path;   // foreign file this doc was imported from ("" if native)
    std::string source_format;  // "kicad_sch" | "kicad_pcb" | "" (native)
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

class EcadSimJobScope {
public:
    explicit EcadSimJobScope(const std::string& job_id)
        : job_id_(job_id), token_(chudware::sim::sim_job_register(job_id_)) {}
    ~EcadSimJobScope() { chudware::sim::sim_job_unregister(job_id_, token_); }
    const chudware::sim::SimCancelToken& token() const { return token_; }

    EcadSimJobScope(const EcadSimJobScope&) = delete;
    EcadSimJobScope& operator=(const EcadSimJobScope&) = delete;

private:
    std::string job_id_;
    chudware::sim::SimCancelToken token_;
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
json compile_field(json& doc, bool refresh_zone_fill = true) {
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
        int zfilled = refresh_zone_fill ? fill_zones_into(doc, kInteractiveFillResolution)
                                        : (doc.contains("zones") && doc["zones"].is_array()
                                               ? static_cast<int>(doc["zones"].size()) : -1);
        if (zfilled >= 0) out["stats"]["zones_filled"] = zfilled;
        out["drc"] = run_drc(doc);
        out["ratsnest"] = ratsnest(doc);
        out["stats"]["nets"] = doc.contains("nets") && doc["nets"].is_array() ? doc["nets"].size() : 0;
        out["stats"]["unrouted"] = out["ratsnest"].size();
    }
    return out;
}

json ops_response(Session& s, const json& id_map, const json& warnings,
                  bool refresh_zone_fill = true) {
    json cf = compile_field(s.doc, refresh_zone_fill);
    s.last_compile = cf;
    json resp = {{"revision", s.revision}, {"id_map", id_map}, {"kind", schema::doc_kind(s.doc)},
                 {"tree", s.doc}, {"compile", cf}};
    if (warnings.is_array() && !warnings.empty()) resp["warnings"] = warnings;
    return resp;
}

}  // namespace

bool dirty_document_count(std::uint64_t& count) {
    std::unique_lock<std::mutex> lock(g_mtx, std::try_to_lock);
    if (!lock.owns_lock()) return false;
    for (const auto& [id, session] : g_sessions) if (session.dirty) ++count;
    return true;
}

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

json ecad_describe(const std::string& doc_id, long budget) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    const json& doc = s.doc;
    if (budget <= 0) budget = 4000;

    // The compile is authoritative. A session that has never compiled (or whose last
    // compile threw) must say so instead of reporting zero violations — a silent 0 is
    // indistinguishable from a clean design.
    const bool compiled = s.last_compile.is_object();
    json compile_info = compiled ? s.last_compile : json::object();

    auto arr = [&](const char* k) -> const json* {
        auto it = doc.find(k);
        return (it != doc.end() && it->is_array()) ? &*it : nullptr;
    };
    auto len = [&](const char* k) -> int {
        const json* a = arr(k);
        return a ? (int)a->size() : 0;
    };
    auto tally = [&](const char* key, int& errors, int& warnings) {
        auto it = compile_info.find(key);
        if (it == compile_info.end() || !it->is_array()) return;
        for (const auto& v : *it) {
            if (v.value("severity", std::string()) == "error")
                ++errors;
            else
                ++warnings;
        }
    };
    // The worst violations first, bounded — an unbounded dump blows the AI's context.
    auto top_violations = [&](const char* key, std::size_t cap) {
        json out = json::array();
        auto it = compile_info.find(key);
        if (it == compile_info.end() || !it->is_array()) return out;
        std::vector<const json*> sorted;
        sorted.reserve(it->size());
        for (const auto& v : *it) sorted.push_back(&v);
        std::stable_sort(sorted.begin(), sorted.end(), [](const json* a, const json* b) {
            bool ae = a->value("severity", std::string()) != "error";
            bool be = b->value("severity", std::string()) != "error";
            if (ae != be) return !ae;
            return a->value("rule", std::string()) < b->value("rule", std::string());
        });
        for (const json* v : sorted) {
            if (out.size() >= cap) break;
            out.push_back(json{{"rule", v->value("rule", std::string())},
                               {"severity", v->value("severity", std::string())},
                               {"net", v->value("net", std::string())},
                               {"message", v->value("message", std::string())}});
        }
        return out;
    };

    json out{{"kind", schema::doc_kind(doc)},
             {"name", doc.value("name", std::string())},
             {"revision", s.revision},
             {"compiled", compiled},
             {"path", s.path},
             {"source_path", s.source_path}};

    if (schema::is_schematic(doc)) {
        int erc_err = 0, erc_warn = 0;
        tally("erc", erc_err, erc_warn);
        out["counts"] = json{{"sheets", std::max(len("sheets"), 1)},
                             {"symbols", len("symbols")},
                             {"wires", len("wires")},
                             {"buses", len("buses")},
                             {"bus_entries", len("bus_entries")},
                             {"junctions", len("junctions")},
                             {"labels", len("labels")},
                             {"power", len("power")},
                             {"no_connect", len("no_connect")},
                             {"texts", len("texts")},
                             {"graphics", len("graphics")},
                             {"sheet_symbols", len("sheet_symbols")}};
        json sheets = json::array();
        if (const json* sh = arr("sheets"))
            for (const auto& e : *sh)
                sheets.push_back(json{{"id", e.value("id", std::string())},
                                      {"name", e.value("name", std::string())}});
        out["sheets"] = sheets;

        // Net table, largest nets first, trimmed to the character budget.
        json nets = json::array();
        int net_count = 0;
        auto ni = compile_info.find("netlist");
        if (ni != compile_info.end() && ni->is_object()) {
            auto nn = ni->find("nets");
            if (nn != ni->end() && nn->is_object()) {
                net_count = (int)nn->size();
                std::vector<std::pair<std::string, int>> by_size;
                by_size.reserve(nn->size());
                for (auto it2 = nn->begin(); it2 != nn->end(); ++it2)
                    by_size.emplace_back(it2.key(),
                                         it2.value().is_array() ? (int)it2.value().size() : 0);
                std::stable_sort(by_size.begin(), by_size.end(),
                                 [](const std::pair<std::string, int>& a,
                                    const std::pair<std::string, int>& b) {
                                     if (a.second != b.second) return a.second > b.second;
                                     return a.first < b.first;
                                 });
                long spent = 0;
                for (const auto& kv : by_size) {
                    spent += (long)kv.first.size() + 12;
                    if (spent > budget) break;
                    nets.push_back(json{{"net", kv.first}, {"pins", kv.second}});
                }
            }
        }
        out["nets"] = net_count;
        out["net_table"] = nets;
        out["net_table_truncated"] = (int)nets.size() < net_count;
        out["erc"] = json{{"available", compiled}, {"errors", erc_err}, {"warnings", erc_warn},
                          {"top", top_violations("erc", 12)}};
        // Kept for callers written against the pre-budget shape.
        out["symbols"] = len("symbols");
        out["wires"] = len("wires");
        out["erc_violations"] = erc_err + erc_warn;
        return out;
    }

    int drc_err = 0, drc_warn = 0;
    tally("drc", drc_err, drc_warn);
    int unrouted = 0;
    auto st = compile_info.find("stats");
    if (st != compile_info.end() && st->is_object() && st->contains("unrouted") &&
        (*st)["unrouted"].is_number())
        unrouted = (*st)["unrouted"].get<int>();
    out["counts"] = json{{"footprints", len("footprints")}, {"tracks", len("tracks")},
                         {"vias", len("vias")}, {"zones", len("zones")},
                         {"nets", len("nets")}};
    out["nets"] = len("nets");
    out["unrouted"] = unrouted;
    out["drc"] = json{{"available", compiled}, {"errors", drc_err}, {"warnings", drc_warn},
                      {"top", top_violations("drc", 12)}};
    out["footprints"] = len("footprints");
    out["tracks"] = len("tracks");
    out["vias"] = len("vias");
    out["drc_violations"] = drc_err + drc_warn;
    return out;
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
    // wrapper is not used; the libchudware dispatch passes a board when doc_id empty).
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

namespace {

void require_svg_leaf(const std::string& name, bool allow_empty = false) {
    if (name.empty() && allow_empty) return;
    if (name.empty() || name == "." || name == ".." ||
        name.find_first_of("/\\:*?\"<>|") != std::string::npos ||
        name.back() == '.' || name.back() == ' ' ||
        std::any_of(name.begin(), name.end(), [](unsigned char c) { return c < 32 || c == 127; }))
        throw OpError("invalid_op", "ecad_export: SVG sheet must be a safe filename leaf",
                      {{"reason", "unsafe SVG leaf"}});
}

// SVG outputs have a checked parent and a non-following final open. In particular,
// a pre-existing symlink must not be truncated between the containment check and
// the write. POSIX opens relative to the pinned destination directory descriptor.
void write_svg_file(const fs::path& target, const fs::path& directory,
                    const std::string& text) {
    require_svg_leaf(target.filename().string());
    std::error_code ec;
    const fs::path root = fs::canonical(directory, ec);
    if (ec) throw OpError("io_error", "ecad_export: cannot resolve SVG destination");
    const fs::path resolved = fs::weakly_canonical(target, ec);
    if (ec || resolved.parent_path() != root)
        throw OpError("io_error", "ecad_export: SVG output escapes destination",
                      {{"path", target.string()}});
    auto fail = [&]() {
        throw OpError("io_error", "ecad_export: cannot write SVG '" + target.string() + "'",
                      {{"path", target.string()}, {"kind", "svg"}});
    };
#ifdef _WIN32
    struct HandleGuard {
        HANDLE value;
        ~HandleGuard() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    };
    HandleGuard file{CreateFileW((root / target.filename()).wstring().c_str(), GENERIC_WRITE,
                                0, nullptr, OPEN_ALWAYS, FILE_FLAG_OPEN_REPARSE_POINT, nullptr)};
    if (file.value == INVALID_HANDLE_VALUE) fail();
    BY_HANDLE_FILE_INFORMATION st{};
    if (!GetFileInformationByHandle(file.value, &st) ||
        (st.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)))
        fail();
    LARGE_INTEGER zero{};
    if (!SetFilePointerEx(file.value, zero, nullptr, FILE_BEGIN) || !SetEndOfFile(file.value))
        fail();
    std::size_t written = 0;
    while (written < text.size()) {
        DWORD n = 0;
        const DWORD chunk = static_cast<DWORD>(std::min<std::size_t>(text.size() - written,
                                                                    1024 * 1024 * 1024));
        if (!WriteFile(file.value, text.data() + written, chunk, &n, nullptr) || !n) fail();
        written += n;
    }
#else
    struct FdGuard {
        int value;
        ~FdGuard() { if (value >= 0) ::close(value); }
    };
    FdGuard dir{::open(root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)};
    if (dir.value < 0) fail();
    FdGuard file{::openat(dir.value, target.filename().c_str(),
                         O_WRONLY | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0666)};
    if (file.value < 0) fail();
    struct stat st{};
    if (::fstat(file.value, &st) || !S_ISREG(st.st_mode) || ::ftruncate(file.value, 0)) fail();
    std::size_t written = 0;
    while (written < text.size()) {
        const ssize_t n = ::write(file.value, text.data() + written,
                                 std::min<std::size_t>(text.size() - written, 1024 * 1024 * 1024));
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) fail();
        written += static_cast<std::size_t>(n);
    }
#endif
}

// Conventional on-disk filename for one exported logical key. Gerber keys are layer
// ids and map to the RS-274X extensions fabricators expect; everything else is a
// single well-known artifact.
std::string export_filename(const std::string& kind, const std::string& key,
                            const std::string& stem, const json& opts) {
    auto plain = [&](const char* ext) { return stem + ext; };
    if (kind == "gerber") {
        static const std::map<std::string, std::string> kExt = {
            {"F.Cu", ".GTL"},     {"B.Cu", ".GBL"},    {"F.SilkS", ".GTO"},
            {"B.SilkS", ".GBO"},  {"F.Mask", ".GTS"},  {"B.Mask", ".GBS"},
            {"F.Paste", ".GTP"},  {"B.Paste", ".GBP"}, {"Edge.Cuts", ".GKO"},
            {"F.Fab", ".GM2"},    {"B.Fab", ".GM3"},   {"F.CrtYd", ".GM4"},
            {"B.CrtYd", ".GM5"}};
        auto it = kExt.find(key);
        if (it != kExt.end()) return stem + it->second;
        // Inner copper: In1.Cu -> .G2, In2.Cu -> .G3, ...
        if (key.rfind("In", 0) == 0) {
            const std::size_t dot = key.find('.');
            const std::string n = key.substr(2, dot == std::string::npos ? dot : dot - 2);
            if (!n.empty() && n.find_first_not_of("0123456789") == std::string::npos)
                return stem + ".G" + std::to_string(std::stoi(n) + 1);
        }
        std::string safe = key;
        for (char& c : safe)
            if (c == '.' || c == '/' || c == '\\') c = '_';
        return stem + "-" + safe + ".gbr";
    }
    if (kind == "drill") return stem + (key == "npth" ? "-NPTH.drl" : "-PTH.drl");
    if (kind == "cpl") return plain("-cpl.csv");
    if (kind == "bom")
        return plain(opts.value("fmt", std::string("csv")) == "json" ? "-bom.json" : "-bom.csv");
    if (kind == "netlist") return plain(".net");
    if (kind == "spice") return plain(".cir");
    if (kind == "kicad") return plain(".kicad_sch");
    if (kind == "pdf") return plain(".pdf");
    if (kind == "svg") {
        require_svg_leaf(key, true);
        return key.empty() ? plain(".svg") : stem + "-" + key + ".svg";
    }
    return stem + "-" + key;
}

// True when `p` should be treated as a DIRECTORY to drop files into rather than the
// literal output file: it exists as a directory, ends in a separator, or has no
// extension on its final component.
bool looks_like_directory(const std::string& p) {
    if (p.empty()) return false;
    std::error_code ec;
    if (fs::is_directory(fs::path(p), ec)) return true;
    const char last = p.back();
    if (last == '/' || last == '\\') return true;
    const std::string leaf = fs::path(p).filename().string();
    return leaf.find('.') == std::string::npos;
}

// Write every produced file. `path` empty -> nothing is written and `paths` is empty
// (the caller keeps the in-memory text). Never reports a write it did not do.
json write_export_files(const json& files, const std::string& kind, const std::string& path,
                        const std::string& stem, const json& opts) {
    json paths = json::array();
    if (path.empty() || !files.is_object() || files.empty()) return paths;

    const bool as_dir = files.size() > 1 || looks_like_directory(path);
    // Validate every sheet before creating directories or writing any page.
    if (kind == "svg")
        for (auto it = files.begin(); it != files.end(); ++it) {
            require_svg_leaf(it.key(), true);
            require_svg_leaf(export_filename(kind, it.key(), stem, opts));
        }
    std::error_code ec;
    if (as_dir) fs::create_directories(fs::path(path), ec);
    else if (fs::path(path).has_parent_path())
        fs::create_directories(fs::path(path).parent_path(), ec);

    for (auto it = files.begin(); it != files.end(); ++it) {
        if (!it.value().is_string()) continue;
        const fs::path target =
            as_dir ? fs::path(path) / export_filename(kind, it.key(), stem, opts) : fs::path(path);
        if (kind == "svg") {
            const fs::path directory = as_dir ? fs::path(path)
                                              : (target.has_parent_path() ? target.parent_path()
                                                                          : fs::path("."));
            write_svg_file(target, directory, it.value().get_ref<const std::string&>());
            paths.push_back(target.string());
            continue;
        }
        std::ofstream fh(target, std::ios::binary | std::ios::trunc);
        if (!fh)
            throw OpError("io_error", "ecad_export: cannot write '" + target.string() + "'",
                          {{"path", target.string()}, {"kind", kind}});
        const std::string& text = it.value().get_ref<const std::string&>();
        fh.write(text.data(), (std::streamsize)text.size());
        if (!fh)
            throw OpError("io_error", "ecad_export: write failed for '" + target.string() + "'",
                          {{"path", target.string()}, {"kind", kind}});
        paths.push_back(target.string());
    }
    return paths;
}

plot::PlotOpts plot_opts_from(const json& opts) {
    plot::PlotOpts po;
    po.sheet = opts.value("sheet", std::string());
    if (opts.contains("frame") && opts["frame"].is_boolean()) po.frame = opts["frame"].get<bool>();
    if (opts.contains("title_block") && opts["title_block"].is_boolean())
        po.title_block = opts["title_block"].get<bool>();
    if (opts.contains("pin_numbers") && opts["pin_numbers"].is_boolean())
        po.pin_numbers = opts["pin_numbers"].get<bool>();
    if (opts.contains("pin_names") && opts["pin_names"].is_boolean())
        po.pin_names = opts["pin_names"].get<bool>();
    po.paper = opts.value("paper", std::string("A4"));
    if (opts.contains("margin_mm") && opts["margin_mm"].is_number())
        po.margin_mm = opts["margin_mm"].get<double>();
    return po;
}

// A filesystem-safe stem for the exported artifacts, from the document name.
std::string export_stem(const json& doc) {
    std::string stem = doc.is_object() ? doc.value("name", std::string()) : std::string();
    if (stem.empty()) stem = schema::is_board(doc) ? "board" : "schematic";
    for (char& c : stem)
        if (c == '/' || c == '\\' || c == ':' || c == '*' || c == '?' || c == '"' || c == '<' ||
            c == '>' || c == '|')
            c = '_';
    return stem;
}

}  // namespace

json ecad_export(const json& params) {
    const std::string kind = params.value("kind", std::string());
    const json opts = params.contains("opts") && params["opts"].is_object() ? params["opts"]
                                                                            : json::object();
    const std::string path = params.value("path", std::string());

    // Resolve the document: a stateless inline `board`/`doc` wins (parity-harness path),
    // else the open session.
    json doc;
    if (params.contains("board") && params["board"].is_object()) {
        doc = params["board"];
    } else if (params.contains("doc") && params["doc"].is_object()) {
        doc = params["doc"];
    } else {
        EcadLock lk;
        Session& s = require_session(params.value("doc_id", std::string()));
        doc = s.doc;
    }

    static const std::set<std::string> kPcbKinds = {"gerber", "drill", "cpl"};
    static const std::set<std::string> kSchKinds = {"netlist", "spice", "kicad", "svg", "pdf"};
    const bool is_pcb = schema::is_board(doc);
    const bool is_sch = schema::is_schematic(doc);

    if (kPcbKinds.count(kind)) {
        if (!is_pcb)
            throw OpError("invalid_op", kind + " export requires a pcb document",
                          {{"reason", "wrong doc kind"}, {"kind", kind}});
    } else if (kSchKinds.count(kind)) {
        if (!is_sch)
            throw OpError("invalid_op", kind + " export requires a sch document",
                          {{"reason", "wrong doc kind"}, {"kind", kind}});
    } else if (kind == "bom") {
        // BOM is meaningful from EITHER side: the board knows placements, the schematic
        // knows the netlist's components. Both are supported.
        if (!is_pcb && !is_sch)
            throw OpError("invalid_op", "bom export requires an ECAD document",
                          {{"reason", "wrong doc kind"}, {"kind", kind}});
    } else {
        throw OpError("invalid_op", "ecad_export: unknown kind '" + kind + "'", {{"kind", kind}});
    }

    json files = json::object();
    json warnings = json::array();
    if (kind == "gerber") {
        for (auto& kv : fab::write_gerbers(doc)) files[kv.first] = kv.second;
    } else if (kind == "drill") {
        for (auto& kv : fab::write_drill(doc)) files[kv.first] = kv.second;
    } else if (kind == "cpl") {
        const bool excl_tht = opts.contains("exclude_tht") && opts["exclude_tht"].is_boolean()
                                  ? opts["exclude_tht"].get<bool>()
                                  : true;
        const bool excl_virt = opts.contains("exclude_virtual") && opts["exclude_virtual"].is_boolean()
                                   ? opts["exclude_virtual"].get<bool>()
                                   : true;
        files["cpl"] = fab::write_placement(doc, excl_tht, excl_virt);
    } else if (kind == "bom") {
        const std::string fmt = opts.value("fmt", std::string("csv"));
        const bool excl = opts.contains("exclude_dnp") && opts["exclude_dnp"].is_boolean()
                              ? opts["exclude_dnp"].get<bool>()
                              : true;
        files["bom"] = is_pcb ? fab::write_bom(doc, fmt, excl)
                              : fab::write_sch_bom(build_netlist(doc), fmt, excl);
    } else if (kind == "netlist") {
        files["netlist"] = fab::write_sch_netlist(doc, build_netlist(doc));
    } else if (kind == "spice") {
        files["spice"] = fab::write_spice_deck(doc, build_netlist(doc));
    } else if (kind == "kicad") {
        files["kicad"] = kicad::export_kicad_sch(doc);
        warnings.push_back(
            "kicad export is best-effort: chudware-only fields are written as properties");
    } else if (kind == "svg") {
        require_svg_leaf(opts.value("sheet", std::string()), true);
        for (const auto& page : plot::schematic_svg_pages(doc, plot_opts_from(opts))) {
            require_svg_leaf(page.first, true);
            files[page.first] = page.second;
        }
    } else if (kind == "pdf") {
        files["pdf"] = plot::schematic_pdf(doc, plot_opts_from(opts));
    }

    const std::string stem = export_stem(doc);
    json out{{"files", files},
             {"paths", write_export_files(files, kind, path, stem, opts)},
             {"kind", kind}};
    if (!warnings.empty()) out["warnings"] = warnings;
    return out;
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
    // Refuse to write a ZIP container over a foreign source file even if the caller
    // asks explicitly — that silently destroys the user's .kicad_sch.
    if (!s.source_path.empty() && fs::path(target) == fs::path(s.source_path))
        throw OpError("io_error",
                      "ecad_save: refusing to overwrite the imported source '" + s.source_path +
                          "' with a .ecad+ container — save elsewhere, or use "
                          "ecad_export(kind=\"kicad\") to write KiCad text back",
                      {{"reason", "foreign source"}, {"source_path", s.source_path}});
    std::string out = ecad_container_save(s.doc, target, s.extra_entries);
    s.path = out;
    s.dirty = false;
    json resp{{"path", out}, {"format", "ecad+"}};
    if (!s.source_path.empty()) resp["source_path"] = s.source_path;
    return resp;
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
    if (suffix == ".kicad_sch" || suffix == ".kicad_pcb") {
        // Imported foreign file: remember where it came from, and point the NATIVE save
        // target at a sibling .ecad+ so a pathless Save can never clobber the source.
        s.source_path = path;
        s.source_format = suffix == ".kicad_sch" ? "kicad_sch" : "kicad_pcb";
        s.path = fs::path(path).replace_extension(".ecad+").string();
        load_warnings.push_back("imported " + s.source_format + " — Save writes the native '" +
                                s.path + "'; the source file is left untouched");
    } else {
        s.path = path;
    }
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
json swap_doc_locked(Session& s, const json& new_doc, long base_revision, const json& warnings,
                     bool refresh_zone_fill = true) {
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
    return ops_response(s, json::object(), warnings, refresh_zone_fill);
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
    for (const json& violation : run_erc(sch_s.doc, netlist)) {
        const std::string rule = violation.value("rule", std::string());
        if (rule == "duplicate_ref" || rule == "unit_ref_conflict")
            throw OpError("schematic_ref_conflict",
                          "ecad_update_from_sch refused: " + violation.value("message", rule),
                          {{"rule", rule}, {"violation", violation}, {"doc_id", sch_doc_id}});
    }
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

    json resp = swap_doc_locked(s, filled_board, base_revision, json::array(), false);
    resp["fill_summary"] = summary;
    resp["doc_id"] = doc_id;
    return resp;
}

json ecad_simulate(const std::string& doc_id, const json& study_in, const json& sim_params,
                   const std::string& job_id) {
    EcadSimJobScope sim_job(job_id);
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

    json conv = chudware::sim::netlist_to_spice_circuit(nl, sim_params);
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

    json results = chudware::sim::spice_simulate(circuit, study, sim_job.token());
    json conv_field = results.is_object() && results.contains("convergence")
                          ? results["convergence"]
                          : json::object();
    const bool cancelled =
        results.is_object() && results.value("cancelled", false);
    bool converged = !cancelled && conv_field.is_object() && !conv_field.empty();
    if (converged)
        for (auto it = conv_field.begin(); it != conv_field.end(); ++it)
            if (!(it.value().is_boolean() ? it.value().get<bool>() : (bool)it.value()))
                converged = false;

    json response = {
        {"ok", !cancelled},
        {"converged", converged},
        {"results", results},
        {"warnings", warns},
        {"log", results.is_object() && results.contains("log") ? results["log"] : json::array()},
        {"convergence", conv_field.is_object() ? conv_field : json::object()},
        {"ms", results.is_object() && results.contains("ms") ? results["ms"] : json(nullptr)},
    };
    if (cancelled) {
        response["cancelled"] = true;
        response["error"] = results.value("error", std::string("simulation cancelled"));
        response["message"] =
            results.value("message", std::string("SPICE simulation cancelled"));
    }
    return response;
}

json ecad_session_doc(const std::string& doc_id) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    return s.doc;  // value semantics -> deep copy
}

// ------------------------------------------------------------------ resolve_pick

namespace {

// Squared distance from p to segment ab.
double seg_dist2(double px, double py, double ax, double ay, double bx, double by) {
    const double vx = bx - ax, vy = by - ay;
    const double len2 = vx * vx + vy * vy;
    double t = 0.0;
    if (len2 > 1e-18) t = std::max(0.0, std::min(1.0, ((px - ax) * vx + (py - ay) * vy) / len2));
    const double dx = px - (ax + t * vx), dy = py - (ay + t * vy);
    return dx * dx + dy * dy;
}

bool xy_of(const json& j, const char* key, double& x, double& y) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_array() || it->size() < 2) return false;
    if (!(*it)[0].is_number() || !(*it)[1].is_number()) return false;
    x = (*it)[0].get<double>();
    y = (*it)[1].get<double>();
    return true;
}

bool polyline_hit(const json& obj, const char* key, double px, double py, double tol2,
                  double& best2) {
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_array() || it->size() < 2) return false;
    bool hit = false;
    for (std::size_t i = 0; i + 1 < it->size(); ++i) {
        const json& a = (*it)[i];
        const json& b = (*it)[i + 1];
        if (!a.is_array() || a.size() < 2 || !b.is_array() || b.size() < 2) continue;
        const double d2 = seg_dist2(px, py, a[0].get<double>(), a[1].get<double>(),
                                    b[0].get<double>(), b[1].get<double>());
        if (d2 <= tol2 && d2 < best2) {
            best2 = d2;
            hit = true;
        }
    }
    return hit;
}

}  // namespace

json ecad_resolve_pick(const std::string& doc_id, const json& xy, const json& kinds, double tol,
                       const std::string& layer) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    const json& doc = s.doc;
    if (!xy.is_array() || xy.size() < 2 || !xy[0].is_number() || !xy[1].is_number())
        throw OpError("invalid_op", "ecad_resolve_pick: xy must be [x, y] in mm",
                      {{"reason", "bad xy"}});
    const double px = xy[0].get<double>(), py = xy[1].get<double>();
    if (!(tol > 0.0)) tol = 1.27;  // half a 2.54 mm grid step
    const double tol2 = tol * tol;

    std::set<std::string> want;
    if (kinds.is_array())
        for (const auto& k : kinds)
            if (k.is_string()) want.insert(k.get<std::string>());
    auto wanted = [&](const char* kind) { return want.empty() || want.count(kind) > 0; };

    // Best hit wins within a kind; kinds are tried most-specific first so a pin beats the
    // symbol it belongs to and a pad beats its footprint.
    json best = nullptr;
    double best2 = tol2 * 4.0 + 1.0;
    auto take = [&](json sel, double d2) {
        if (d2 < best2) {
            best2 = d2;
            best = std::move(sel);
        }
    };
    auto point_hit = [&](const json& obj, const char* key, double& d2) {
        double x = 0, y = 0;
        if (!xy_of(obj, key, x, y)) return false;
        d2 = (px - x) * (px - x) + (py - y) * (py - y);
        return d2 <= tol2;
    };

    if (schema::is_schematic(doc)) {
        // 1. pins (highest priority — this is what a wire must snap to)
        if (wanted("pin") && doc.contains("symbols") && doc["symbols"].is_array()) {
            const json lib = doc.value("lib_cache", json::object());
            for (const auto& sym : doc["symbols"]) {
                const json def = lib.value(sym.value("libid", std::string()), json::object());
                if (!def.contains("pins") || !def["pins"].is_array()) continue;
                const int unit = sym.contains("unit") && sym["unit"].is_number()
                                     ? sym["unit"].get<int>()
                                     : 1;
                for (const auto& pin : def["pins"]) {
                    const int pu = pin.contains("unit") && pin["unit"].is_number()
                                       ? pin["unit"].get<int>()
                                       : 1;
                    if (pu != unit && pu != 0) continue;
                    const std::pair<double, double> w = schema::pin_world(sym, pin);
                    const double d2 = (px - w.first) * (px - w.first) +
                                      (py - w.second) * (py - w.second);
                    if (d2 <= tol2)
                        take(json{{"kind", "pin"},
                                  {"id", sym.value("id", std::string())},
                                  {"sym", sym.value("id", std::string())},
                                  {"ref", sym.value("ref", std::string())},
                                  {"pin", pin.value("number", std::string())},
                                  {"sheet", sym.value("sheet", std::string("sh1"))},
                                  {"xy", json::array({w.first, w.second})}},
                             d2);
                }
            }
        }
        // 2. point entities
        struct PointKind { const char* arr; const char* kind; };
        static const PointKind kPointKinds[] = {{"junctions", "junction"},
                                                {"no_connect", "no_connect"},
                                                {"power", "power"},
                                                {"labels", "label"},
                                                {"bus_entries", "bus_entry"},
                                                {"texts", "text"},
                                                {"sheet_symbols", "sheet_symbol"}};
        for (const PointKind& pk : kPointKinds) {
            if (!wanted(pk.kind) || !doc.contains(pk.arr) || !doc[pk.arr].is_array()) continue;
            for (const auto& e : doc[pk.arr]) {
                double d2 = 0;
                if (!point_hit(e, "pos", d2)) continue;
                json sel{{"kind", pk.kind}, {"id", e.value("id", std::string())},
                         {"sheet", e.value("sheet", std::string("sh1"))}};
                if (e.contains("text")) sel["text"] = e["text"];
                if (e.contains("net")) sel["net"] = e["net"];
                take(std::move(sel), d2);
            }
        }
        // 3. symbol bodies (a click inside the body, not on a pin)
        if (wanted("symbol") && doc.contains("symbols") && doc["symbols"].is_array()) {
            for (const auto& sym : doc["symbols"]) {
                double d2 = 0;
                if (!point_hit(sym, "pos", d2)) continue;
                take(json{{"kind", "symbol"}, {"id", sym.value("id", std::string())},
                          {"ref", sym.value("ref", std::string())},
                          {"libid", sym.value("libid", std::string())},
                          {"sheet", sym.value("sheet", std::string("sh1"))}},
                     d2);
            }
        }
        // 4. polylines
        struct LineKind { const char* arr; const char* kind; };
        static const LineKind kLineKinds[] = {{"wires", "wire"}, {"buses", "bus"},
                                              {"graphics", "graphic"}};
        for (const LineKind& lk2 : kLineKinds) {
            if (!wanted(lk2.kind) || !doc.contains(lk2.arr) || !doc[lk2.arr].is_array()) continue;
            for (const auto& e : doc[lk2.arr]) {
                double d2 = best2;
                if (!polyline_hit(e, "pts", px, py, tol2, d2)) continue;
                json sel{{"kind", lk2.kind}, {"id", e.value("id", std::string())},
                         {"sheet", e.value("sheet", std::string("sh1"))}};
                if (e.contains("name")) sel["name"] = e["name"];
                take(std::move(sel), d2);
            }
        }
        // Attach the resolved net when the compile knows one.
        if (best.is_object() && s.last_compile.is_object()) {
            const json nl = s.last_compile.value("netlist", json::object());
            const json by_pin = nl.is_object() ? nl.value("by_pin", json::object()) : json::object();
            if (best.value("kind", std::string()) == "pin") {
                const std::string key = best.value("sym", std::string()) + "." +
                                        best.value("pin", std::string());
                if (by_pin.contains(key)) best["net"] = by_pin[key];
            }
        }
        return json{{"selector", best}};
    }

    // Board picking: pad > via > track > zone > footprint, honoring an optional layer filter.
    auto on_layer = [&](const json& e) {
        if (layer.empty()) return true;
        if (e.contains("layer") && e["layer"].is_string()) return e["layer"] == layer;
        if (e.contains("layers") && e["layers"].is_array())
            for (const auto& l : e["layers"])
                if (l.is_string() && l.get<std::string>() == layer) return true;
        return !e.contains("layer") && !e.contains("layers");
    };
    if (wanted("pad") && doc.contains("footprints") && doc["footprints"].is_array()) {
        for (const auto& fp : doc["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                if (!on_layer(pad)) continue;
                const std::pair<double, double> w = schema::pad_world(fp, pad);
                const double d2 = (px - w.first) * (px - w.first) + (py - w.second) * (py - w.second);
                if (d2 <= tol2)
                    take(json{{"kind", "pad"}, {"id", fp.value("id", std::string())},
                              {"fp", fp.value("id", std::string())},
                              {"ref", fp.value("ref", std::string())},
                              {"pad", pad.value("number", std::string())},
                              {"net", pad.value("net", std::string())},
                              {"xy", json::array({w.first, w.second})}},
                         d2);
            }
        }
    }
    if (wanted("via") && doc.contains("vias") && doc["vias"].is_array())
        for (const auto& v : doc["vias"]) {
            double d2 = 0;
            if (point_hit(v, "at", d2))
                take(json{{"kind", "via"}, {"id", v.value("id", std::string())},
                          {"net", v.value("net", std::string())}},
                     d2);
        }
    if (wanted("track") && doc.contains("tracks") && doc["tracks"].is_array())
        for (const auto& t : doc["tracks"]) {
            if (!on_layer(t)) continue;
            double ax = 0, ay = 0, bx = 0, by = 0;
            if (!xy_of(t, "p1", ax, ay) || !xy_of(t, "p2", bx, by)) continue;
            const double half = t.contains("width") && t["width"].is_number()
                                    ? t["width"].get<double>() * 0.5
                                    : 0.0;
            const double reach = tol + half;
            const double d2 = seg_dist2(px, py, ax, ay, bx, by);
            if (d2 <= reach * reach)
                take(json{{"kind", "track"}, {"id", t.value("id", std::string())},
                          {"net", t.value("net", std::string())},
                          {"layer", t.value("layer", std::string())}},
                     d2);
        }
    if (wanted("zone") && doc.contains("zones") && doc["zones"].is_array())
        for (const auto& z : doc["zones"]) {
            if (!on_layer(z)) continue;
            double d2 = best2;
            if (polyline_hit(z, "polygon", px, py, tol2, d2))
                take(json{{"kind", "zone"}, {"id", z.value("id", std::string())},
                          {"net", z.value("net", std::string())},
                          {"layer", z.value("layer", std::string())}},
                     d2);
        }
    if (wanted("footprint") && doc.contains("footprints") && doc["footprints"].is_array())
        for (const auto& fp : doc["footprints"]) {
            double d2 = 0;
            if (point_hit(fp, "pos", d2))
                take(json{{"kind", "footprint"}, {"id", fp.value("id", std::string())},
                          {"ref", fp.value("ref", std::string())},
                          {"side", fp.value("side", std::string("top"))}},
                     d2);
        }
    return json{{"selector", best}};
}

// --------------------------------------------------------------------- ecad_design

namespace {

// Supply-net heuristic: these get a power PORT (which merges globally and reads like a
// real schematic) instead of a plain net label.
bool is_supply_net(const std::string& net) {
    static const std::set<std::string> kExact = {"GND",  "GNDA", "GNDD", "AGND", "DGND",
                                                 "VCC",  "VDD",  "VSS",  "VEE",  "VBUS",
                                                 "VIN",  "VOUT_RAIL"};
    if (kExact.count(net)) return true;
    // +5V, +3V3, -12V, 3V3 …
    if (net.size() >= 2 && (net[0] == '+' || net[0] == '-')) return true;
    return false;
}

// The power symbol libid for a supply net name.
std::string power_symbol_for(const std::string& net) {
    if (net.rfind("GND", 0) == 0 || net == "AGND" || net == "DGND" || net == "VSS")
        return "power:GND";
    return "power:" + net;
}

// Root (body-side) world position of a pin — pin_world with the stub length removed.
std::pair<double, double> pin_root_world(const json& sym, const json& pin) {
    json p = pin;
    p["length"] = 0.0;
    return schema::pin_world(sym, p);
}

// Locate a pin definition by NUMBER, falling back to NAME (the recipe vocabulary uses
// "+"/"-" for sources but "1"/"2" for passives).
const json* find_pin(const json& def, const std::string& want) {
    auto pins = def.find("pins");
    if (pins == def.end() || !pins->is_array()) return nullptr;
    for (const auto& p : *pins)
        if (p.value("number", std::string()) == want) return &p;
    for (const auto& p : *pins)
        if (p.value("name", std::string()) == want) return &p;
    return nullptr;
}

}  // namespace

json ecad_design(const std::string& request, const std::string& name) {
    if (request.empty())
        throw OpError("invalid_op", "ecad_design: request must not be empty",
                      {{"reason", "empty request"}});

    // 1. Pick + parameterize the closest deterministic recipe.
    const json design = recipes::closest_recipe(request);
    const std::string recipe_used = design.value("name", std::string("unknown"));

    // 2. Fresh schematic session (takes and releases the store lock itself).
    const std::string doc_name = name.empty() ? recipe_used : name;
    json created = ecad_new("sch", doc_name);
    const std::string doc_id = created.value("doc_id", std::string());
    long revision = created.value("revision", 0L);

    json warnings = json::array();

    // 3. Place every component on a 2.54 mm grid, four per row.
    std::vector<json> comps;
    if (design.contains("components") && design["components"].is_array())
        for (const auto& c : design["components"]) comps.push_back(c);
    std::stable_sort(comps.begin(), comps.end(), [](const json& a, const json& b) {
        return a.value("ref", std::string()) < b.value("ref", std::string());
    });

    constexpr double kColStep = 38.1;   // 15 grid steps
    constexpr double kRowStep = 38.1;
    constexpr int kPerRow = 4;
    json place_ops = json::array();
    for (std::size_t i = 0; i < comps.size(); ++i) {
        const json& c = comps[i];
        const double x = (double)(i % kPerRow) * kColStep;
        const double y = -(double)(i / kPerRow) * kRowStep;
        json op{{"op", "sch_add_symbol"},
                {"libid", c.value("libid", std::string("Device:R"))},
                {"pos", json::array({x, y})},
                {"ref", c.value("ref", std::string())},
                {"value", c.value("value", std::string())}};
        // Everything the recipe knows that is not a placement key becomes a user field, so
        // ac_mag / model hints survive into the document and reach the simulator.
        json fields = json::object();
        for (auto it = c.begin(); it != c.end(); ++it) {
            const std::string& k = it.key();
            if (k == "ref" || k == "libid" || k == "value" || k == "sim") continue;
            fields[k] = it.value();
        }
        if (!fields.empty()) op["fields"] = fields;
        if (c.contains("sim")) op["sim_params"] = c["sim"];
        place_ops.push_back(std::move(op));
    }
    if (!place_ops.empty()) {
        json resp = ecad_apply_ops(doc_id, revision, place_ops);
        revision = resp.value("revision", revision);
    }

    // 4. Read the placed document back so pin geometry comes from the REAL resolved
    //    symbol defs, never from an assumption about what the library contains.
    json doc = ecad_session_doc(doc_id);
    const json lib = doc.value("lib_cache", json::object());
    std::map<std::string, json> by_ref;
    if (doc.contains("symbols") && doc["symbols"].is_array())
        for (const auto& s : doc["symbols"]) by_ref[s.value("ref", std::string())] = s;

    // 5. Stub + name every net. A pin gets a short wire out of its tip; the far end
    //    carries a power port (supply nets) or a net label (signal nets). Equal names
    //    merge in the netlist, so this is real connectivity, not decoration.
    json wire_ops = json::array();
    std::vector<std::string> net_names;
    if (design.contains("nets") && design["nets"].is_object())
        for (auto it = design["nets"].begin(); it != design["nets"].end(); ++it)
            net_names.push_back(it.key());
    std::sort(net_names.begin(), net_names.end());

    constexpr double kStub = 5.08;  // two grid steps
    for (const std::string& net : net_names) {
        const json& members = design["nets"][net];
        if (!members.is_array()) continue;
        for (const auto& m : members) {
            const std::string ref = m.value("ref", std::string());
            const std::string pin_key = m.value("pin", std::string());
            auto sit = by_ref.find(ref);
            if (sit == by_ref.end()) {
                warnings.push_back("net " + net + ": no placed symbol for ref '" + ref + "'");
                continue;
            }
            const json& sym = sit->second;
            const json def = lib.value(sym.value("libid", std::string()), json::object());
            const json* pin = find_pin(def, pin_key);
            if (!pin) {
                warnings.push_back("net " + net + ": " + ref + " has no pin '" + pin_key + "'");
                continue;
            }
            const std::pair<double, double> tip = schema::pin_world(sym, *pin);
            const std::pair<double, double> root = pin_root_world(sym, *pin);
            double dx = tip.first - root.first, dy = tip.second - root.second;
            const double mag = std::sqrt(dx * dx + dy * dy);
            if (mag < 1e-9) { dx = 1.0; dy = 0.0; } else { dx /= mag; dy /= mag; }
            const double ex = tip.first + dx * kStub, ey = tip.second + dy * kStub;

            wire_ops.push_back(json{{"op", "sch_add_wire"},
                                    {"pts", json::array({json::array({tip.first, tip.second}),
                                                         json::array({ex, ey})})}});
            if (is_supply_net(net))
                wire_ops.push_back(json{{"op", "sch_add_power"},
                                        {"pos", json::array({ex, ey})},
                                        {"net", net},
                                        {"symbol", power_symbol_for(net)}});
            else
                wire_ops.push_back(json{{"op", "sch_add_label"},
                                        {"pos", json::array({ex, ey})},
                                        {"text", net},
                                        {"scope", "global"}});
        }
    }
    if (!wire_ops.empty()) {
        json resp = ecad_apply_ops(doc_id, revision, wire_ops);
        revision = resp.value("revision", revision);
    }

    // 6. Title block from the recipe so a plotted sheet is self-describing.
    json meta_ops = json::array();
    meta_ops.push_back(json{{"op", "sch_set_title_block"},
                            {"title", doc_name},
                            {"company", "Chudware"},
                            {"rev", "A"},
                            {"comment1", design.value("request", request)},
                            {"comment2", design.value("notes", std::string())}});
    json final_resp = ecad_apply_ops(doc_id, revision, meta_ops);

    final_resp["doc_id"] = doc_id;
    final_resp["recipe_used"] = recipe_used;
    final_resp["design"] = design;
    final_resp["request"] = request;
    if (design.contains("study")) final_resp["study"] = design["study"];
    if (design.contains("notes")) final_resp["notes"] = design["notes"];

    // Honesty: report the placement/naming problems AND whatever ERC says about the
    // produced sheet. A design that did not fully wire up must not read as a success.
    json existing = final_resp.contains("warnings") && final_resp["warnings"].is_array()
                        ? final_resp["warnings"]
                        : json::array();
    for (const auto& w : warnings) existing.push_back(w);
    int erc_errors = 0;
    if (final_resp.contains("compile") && final_resp["compile"].is_object() &&
        final_resp["compile"].contains("erc") && final_resp["compile"]["erc"].is_array())
        for (const auto& v : final_resp["compile"]["erc"])
            if (v.value("severity", std::string()) == "error") ++erc_errors;
    final_resp["ok"] = warnings.empty() && erc_errors == 0;
    final_resp["erc_errors"] = erc_errors;
    if (!existing.empty()) final_resp["warnings"] = existing;
    return final_resp;
}

// ----------------------------------------------------------------- crash recovery
// Drop an orphaned g_mtx lock left held by THIS thread after a Windows structured
// exception (access violation) unwound past an EcadLock without running its C++
// destructor under /EHsc. Mirrors force_release_doc_lock in core/doc/session.cpp —
// same pattern, same safety contract, same idempotency guarantee.
// Called from the libchudware C dispatch boundary's __except filter alongside
// force_release_doc_lock and force_release_drawing_lock so ANY hardware fault releases
// ALL orphaned registry locks.
bool force_release_ecad_lock() {
    if (!g_mtx_held.load(std::memory_order_acquire)) return false;
    if (g_mtx_owner != std::this_thread::get_id()) return false;
    g_mtx_held.store(false, std::memory_order_release);
    g_mtx.unlock();
    return true;
}

}  // namespace chudware::ecad
