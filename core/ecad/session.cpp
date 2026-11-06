// ECAD session implementation — see session.h. Mirrors engine/ecad/session.py
// (bounded JSON-snapshot undo/redo, stale-revision guard, revision bumps) and
// engine/ecad/rpc.py response shapes. Pure JSON; no OCCT.

#include "ecad/session.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
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
#include "ecad/impedance.h"
#include "ecad/kicad.h"
#include "ecad/kicad_libs.h"
#include "ecad/netlist.h"
#include "ecad/recipes.h"
#include "ecad/schema.h"
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

struct FileStamp {
    bool exists = false;
    std::uintmax_t size = 0;
    std::int64_t mtime = 0;
    std::uint64_t hash = 0;
};

bool operator==(const FileStamp& a, const FileStamp& b) {
    return a.exists == b.exists && (!a.exists ||
        (a.size == b.size && a.mtime == b.mtime && a.hash == b.hash));
}
bool operator!=(const FileStamp& a, const FileStamp& b) { return !(a == b); }

struct Session {
    std::string doc_id;
    json doc;
    long revision = 0;
    std::string path;
    FileStamp stamp;
    FileStamp project_stamp;
    FileStamp rules_stamp;
    json project_document = nullptr;
    std::string custom_rules;
    json saved_settings = nullptr;
    bool dirty = false;
    std::vector<std::string> undo_stack;
    std::vector<std::string> redo_stack;
    json last_compile = nullptr;
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
    const fs::path bound = fs::u8path(it->second.path);
    kicad_libs::set_project_dir(bound.empty() ? std::string() :
                                fs::absolute(bound).parent_path().u8string());
    return it->second;
}

[[noreturn]] void file_error(const std::string& message, const fs::path& path) {
    throw OpError("io_error", message + ": " + path.u8string(), {{"path", path.u8string()}});
}

std::string extension(const fs::path& path) {
    std::string out = path.extension().u8string();
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return out;
}

bool same_path(const fs::path& a, const fs::path& b) {
    std::error_code ec1, ec2;
    const auto ca = fs::weakly_canonical(a, ec1);
    const auto cb = fs::weakly_canonical(b, ec2);
    return !ec1 && !ec2 ? ca == cb : a.lexically_normal() == b.lexically_normal();
}

std::uint64_t content_hash(const std::string& bytes) {
    std::uint64_t hash = 14695981039346656037ULL;
    for (unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

FileStamp file_stamp(const fs::path& path) {
    std::error_code ec;
    FileStamp result;
    result.exists = fs::exists(path, ec);
    if (ec) file_error("cannot stat ECAD file", path);
    if (!result.exists) return result;
    result.size = fs::file_size(path, ec);
    if (ec) file_error("cannot stat ECAD file", path);
    const auto time = fs::last_write_time(path, ec);
    if (ec) file_error("cannot stat ECAD file", path);
    result.mtime = static_cast<std::int64_t>(time.time_since_epoch().count());
    std::ifstream in(path, std::ios::binary);
    if (!in) file_error("cannot read ECAD file", path);
    result.hash = 14695981039346656037ULL;
    char buffer[65536];
    std::uintmax_t read = 0;
    while (in) {
        in.read(buffer, sizeof(buffer));
        const auto count = in.gcount();
        read += static_cast<std::uintmax_t>(count);
        for (std::streamsize i = 0; i < count; ++i) {
            result.hash ^= static_cast<unsigned char>(buffer[i]);
            result.hash *= 1099511628211ULL;
        }
    }
    if (in.bad()) file_error("cannot read ECAD file", path);
    const auto size_after = fs::file_size(path, ec);
    if (ec) file_error("ECAD file changed while reading", path);
    const auto time_after = fs::last_write_time(path, ec);
    if (ec || read != result.size || size_after != result.size || time_after != time)
        file_error("ECAD file changed while reading", path);
    return result;
}

json stamp_json(const FileStamp& stamp) {
    return {{"exists", stamp.exists}, {"size", stamp.size}, {"mtime", stamp.mtime},
            {"hash", std::to_string(stamp.hash)}};
}

FileStamp stamp_from_json(const json& data) {
    FileStamp stamp;
    if (!data.is_object()) return stamp;
    try {
        stamp.exists = data.value("exists", false);
        stamp.size = data.value("size", std::uintmax_t{0});
        stamp.mtime = data.value("mtime", std::int64_t{0});
        stamp.hash = std::stoull(data.value("hash", std::string("0")));
    } catch (const std::exception&) {
        throw OpError("io_error", "invalid ECAD recovery fingerprint");
    }
    return stamp;
}

bool kicad_locked(const fs::path& path) {
    if (path.empty()) return false;
    std::error_code ec;
    const auto lock = path.parent_path() / fs::u8path("~" + path.filename().u8string() + ".lck");
    const bool exists = fs::exists(lock, ec);
    if (ec) file_error("cannot inspect KiCad lock", lock);
    return exists;
}

void ensure_parent(const fs::path& path) {
    const fs::path parent = path.has_parent_path() ? path.parent_path() : fs::path(".");
    std::error_code ec;
    fs::create_directories(parent, ec);
    if (ec) file_error("cannot create ECAD directory", parent);
}

// Stage in the destination directory, sync, and publish atomically. The optional
// final check closes the serialization window for lock/conflict detection.
void atomic_write(const fs::path& path, const std::string& bytes,
                  const std::function<void()>& before_commit = {}, bool if_missing = false,
                  bool fail_if_exists = false) {
    ensure_parent(path);
    static std::atomic<unsigned long long> seq{0};
    fs::path stage;
#ifdef _WIN32
    HANDLE file = INVALID_HANDLE_VALUE;
    for (;;) {
        stage = path;
        stage += ".tmp." + std::to_string(GetCurrentProcessId()) + "." +
                 std::to_string(seq.fetch_add(1));
        file = CreateFileW(stage.wstring().c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file != INVALID_HANDLE_VALUE) break;
        if (GetLastError() != ERROR_FILE_EXISTS) file_error("cannot create ECAD stage", stage);
    }
    bool ok = true;
    for (std::size_t pos = 0; ok && pos < bytes.size();) {
        DWORD written = 0;
        const DWORD n = static_cast<DWORD>(std::min<std::size_t>(bytes.size() - pos, 65536));
        ok = WriteFile(file, bytes.data() + pos, n, &written, nullptr) && written > 0;
        pos += written;
    }
    ok = ok && FlushFileBuffers(file);
    ok = CloseHandle(file) && ok;
#else
    int file = -1;
    for (;;) {
        stage = path;
        stage += ".tmp." + std::to_string(getpid()) + "." +
                 std::to_string(seq.fetch_add(1));
        file = ::open(stage.c_str(), O_CREAT | O_EXCL | O_WRONLY, 0666);
        if (file >= 0) break;
        if (errno != EEXIST) file_error("cannot create ECAD stage", stage);
    }
    bool ok = true;
    for (std::size_t pos = 0; ok && pos < bytes.size();) {
        const auto n = ::write(file, bytes.data() + pos, bytes.size() - pos);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) ok = false;
        else pos += static_cast<std::size_t>(n);
    }
    if (ok) ok = ::fsync(file) == 0;
    if (::close(file) != 0) ok = false;
#endif
    if (!ok) {
        std::error_code ec;
        fs::remove(stage, ec);
        file_error("cannot write ECAD stage", stage);
    }
    try {
        if (before_commit) before_commit();
#ifdef _WIN32
        const DWORD flags = MOVEFILE_WRITE_THROUGH | (if_missing ? 0 : MOVEFILE_REPLACE_EXISTING);
        if (!MoveFileExW(stage.wstring().c_str(), path.wstring().c_str(), flags)) {
            if (!if_missing || fail_if_exists || (GetLastError() != ERROR_ALREADY_EXISTS &&
                                                   GetLastError() != ERROR_FILE_EXISTS))
                file_error("cannot replace ECAD file", path);
        }
#else
        if (if_missing) {
            if (::link(stage.c_str(), path.c_str()) != 0 && (errno != EEXIST || fail_if_exists))
                file_error("cannot create ECAD file", path);
        } else if (::rename(stage.c_str(), path.c_str()) != 0) {
            file_error("cannot replace ECAD file", path);
        }
        const fs::path parent = path.has_parent_path() ? path.parent_path() : fs::path(".");
        int dir = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY);
        if (dir < 0) file_error("cannot sync ECAD directory", parent);
        const bool synced = ::fsync(dir) == 0;
        const bool closed = ::close(dir) == 0;
        if (!synced || !closed) file_error("cannot sync ECAD directory", parent);
#endif
    } catch (...) {
        std::error_code ec;
        fs::remove(stage, ec);
        throw;
    }
    std::error_code ec;
    fs::remove(stage, ec);
}

void ensure_project(const fs::path& document, bool require_missing = false) {
    fs::path project = document;
    project.replace_extension(".kicad_pro");
    if (fs::exists(project)) {
        if (require_missing)
            throw OpError("io_error", "ecad_new_project: file already exists",
                          {{"path", project.u8string()}});
        return;
    }
    auto check_lock = [&]() {
        if (kicad_locked(project))
            throw OpError("kicad_locked", "KiCad has the project file open",
                          {{"path", project.u8string()}});
    };
    check_lock();
    const json data = {
        {"board", {{"design_settings", {{"defaults", json::object()},
            {"diff_pair_dimensions", json::array()}, {"drc_exclusions", json::array()},
            {"rules", json::object()}, {"track_widths", json::array()},
            {"via_dimensions", json::array()}}}}},
        {"boards", json::array()},
        {"libraries", {{"pinned_footprint_libs", json::array()},
                       {"pinned_symbol_libs", json::array()}}},
        {"meta", {{"filename", project.filename().u8string()}, {"version", 1}}},
        {"net_settings", {{"classes", json::array()}, {"meta", {{"version", 0}}}}},
        {"pcbnew", {{"page_layout_descr_file", ""}}},
        {"sheets", json::array()}, {"text_variables", json::object()}};
    atomic_write(project, data.dump(2) + "\n", check_lock, true, require_missing);
}

std::string project_path(const std::string& document) {
    if (document.empty()) return {};
    fs::path project = fs::u8path(document);
    project.replace_extension(".kicad_pro");
    std::error_code ec;
    const bool exists = fs::is_regular_file(project, ec);
    if (ec && ec != std::errc::no_such_file_or_directory)
        file_error("cannot inspect KiCad project", project);
    return exists ? project.u8string() : std::string();
}

fs::path companion_project(const fs::path& document) {
    fs::path path = document;
    path.replace_extension(".kicad_pro");
    return path;
}

fs::path companion_rules(const fs::path& document) {
    fs::path path = document;
    path.replace_extension(".kicad_dru");
    return path;
}

struct ProjectSnapshot {
    json data = json::object();
    FileStamp stamp;
    FileStamp rules_stamp;
    std::string rules;
};

ProjectSnapshot read_project(const fs::path& document) {
    ProjectSnapshot result;
    const fs::path path = companion_project(document);
    result.stamp = file_stamp(path);
    if (result.stamp.exists) {
        std::ifstream input(path, std::ios::binary);
        try {
            input >> result.data;
            if (!result.data.is_object()) throw std::runtime_error("expected a JSON object");
        } catch (const std::exception& exc) {
            throw OpError("io_error", "invalid KiCad project '" + path.u8string() + "': " + exc.what(),
                          {{"path", path.u8string()}});
        }
        if (file_stamp(path) != result.stamp)
            throw OpError("external_change", "KiCad project changed while reading",
                          {{"path", path.u8string()}});
    }
    const fs::path rules = companion_rules(document);
    result.rules_stamp = file_stamp(rules);
    if (result.rules_stamp.exists) {
        std::ifstream input(rules, std::ios::binary);
        if (!input) file_error("cannot read KiCad custom rules", rules);
        result.rules.assign(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
        if (input.bad()) file_error("cannot read KiCad custom rules", rules);
        if (file_stamp(rules) != result.rules_stamp)
            throw OpError("external_change", "KiCad custom rules changed while reading",
                          {{"path", rules.u8string()}});
    }
    return result;
}

const json* object_member(const json& parent, const char* key) {
    if (!parent.is_object()) return nullptr;
    const auto it = parent.find(key);
    return it == parent.end() ? nullptr : &*it;
}

json& ensure_object(json& parent, const char* key) {
    json& value = parent[key];
    if (value.is_null()) value = json::object();
    if (!value.is_object())
        throw OpError("io_error", std::string("invalid KiCad project object: ") + key);
    return value;
}

bool same_project_document(const json& left, const json& right) {
    if (!left.is_object() || !right.is_object() || left.size() != right.size()) return false;
    for (const auto& [key, value] : left.items()) {
        auto other = right.find(key);
        if (other == right.end()) return false;
        if (key != "meta" || !value.is_object() || !other->is_object()) {
            if (value != *other) return false;
            continue;
        }
        const auto left_count = value.size() - (value.contains("filename") ? 1 : 0);
        const auto right_count = other->size() - (other->contains("filename") ? 1 : 0);
        if (left_count != right_count) return false;
        for (const auto& [meta_key, meta_value] : value.items()) {
            if (meta_key == "filename") continue;
            auto found = other->find(meta_key);
            if (found == other->end() || *found != meta_value) return false;
        }
    }
    return true;
}

std::string native_class(const std::string& name) { return name == "Default" ? "default" : name; }
std::string kicad_class(const std::string& name) { return name == "default" ? "Default" : name; }

// KiCad's net-class patterns use '*' and '?' wildcards over complete net names.
bool net_pattern_matches(const std::string& pattern, const std::string& name) {
    std::size_t p = 0, n = 0, star = std::string::npos, retry = 0;
    while (n < name.size()) {
        if (p < pattern.size() && (pattern[p] == '?' || pattern[p] == name[n])) {
            ++p; ++n;
        } else if (p < pattern.size() && pattern[p] == '*') {
            star = p++; retry = n;
        } else if (star != std::string::npos) {
            p = star + 1; n = ++retry;
        } else {
            return false;
        }
    }
    while (p < pattern.size() && pattern[p] == '*') ++p;
    return p == pattern.size();
}

constexpr const char* kAnalysisVariable = "CHUDWARE_ECAD_ANALYSIS_V1";
const std::pair<const char*, const char*> kClassFields[] = {
    {"clearance", "clearance"}, {"track_width", "track_width"},
    {"via_diameter", "via_diameter"}, {"via_drill", "via_drill"},
    {"diff_gap", "diff_pair_gap"}, {"diff_width", "diff_pair_width"}};
const std::pair<const char*, const char*> kRuleFields[] = {
    {"clearance", "min_clearance"}, {"track_width_min", "min_track_width"},
    {"via_diameter_min", "min_via_diameter"}, {"via_drill_min", "min_through_hole_diameter"},
    {"annular_ring_min", "min_via_annular_width"}, {"hole_to_hole_min", "min_hole_to_hole"},
    {"edge_clearance", "min_copper_edge_clearance"}, {"silk_to_mask_min", "min_silk_clearance"}};

bool mapped_field(const std::string& name, const std::pair<const char*, const char*>* fields,
                  std::size_t count) {
    for (std::size_t i = 0; i < count; ++i) if (name == fields[i].first) return true;
    return false;
}

json board_settings(const json& doc) {
    json assignments = json::object();
    if (doc.contains("nets") && doc["nets"].is_array()) {
        for (const auto& net : doc["nets"]) {
            if (net.is_object() && net.contains("name") && net["name"].is_string())
                assignments[net["name"].get<std::string>()] =
                    net.contains("class") && net["class"].is_string() ? net["class"] : json("default");
        }
    }
    return {{"classes", doc.value("net_classes", json::object())},
            {"rules", doc.value("rules", json::object())},
            {"assignments", std::move(assignments)}};
}

void restore_board_settings(json& doc, const json& settings) {
    if (!settings.is_object()) return;
    if (settings.contains("classes") && settings["classes"].is_object())
        doc["net_classes"] = settings["classes"];
    if (settings.contains("rules") && settings["rules"].is_object()) doc["rules"] = settings["rules"];
    if (settings.contains("assignments") && settings["assignments"].is_object() &&
        doc.contains("nets") && doc["nets"].is_array()) {
        for (auto& net : doc["nets"]) {
            if (!net.is_object() || !net.contains("name") || !net["name"].is_string()) continue;
            const auto it = settings["assignments"].find(net["name"].get<std::string>());
            if (it != settings["assignments"].end() && it->is_string()) net["class"] = *it;
        }
    }
}

void import_project_settings(json& doc, const json& project) {
    if (!schema::is_board(doc)) return;
    const json* settings = object_member(project, "net_settings");
    const json* classes = settings ? object_member(*settings, "classes") : nullptr;
    if (classes && classes->is_array() && !classes->empty()) {
        json loaded = json::object();
        for (const auto& entry : *classes) {
            if (!entry.is_object() || !entry.contains("name") || !entry["name"].is_string()) continue;
            json values = json::object();
            for (const auto& [native, kicad] : kClassFields)
                if (entry.contains(kicad) && entry[kicad].is_number()) values[native] = entry[kicad];
            loaded[native_class(entry["name"].get<std::string>())] = std::move(values);
        }
        if (!loaded.empty()) doc["net_classes"] = std::move(loaded);
    }
    const json* board = object_member(project, "board");
    const json* design = board ? object_member(*board, "design_settings") : nullptr;
    const json* rules = design ? object_member(*design, "rules") : nullptr;
    if (rules && rules->is_object()) {
        for (const auto& [native, kicad] : kRuleFields)
            if (rules->contains(kicad) && (*rules)[kicad].is_number())
                doc["rules"][native] = (*rules)[kicad];
    }
    const json* variables = object_member(project, "text_variables");
    if (variables && variables->is_object() && variables->contains(kAnalysisVariable)) {
        const auto& stored = (*variables)[kAnalysisVariable];
        if (!stored.is_string()) throw OpError("io_error", "invalid Chudware project analysis data");
        json extra = json::parse(stored.get<std::string>(), nullptr, false);
        if (!extra.is_object()) throw OpError("io_error", "invalid Chudware project analysis data");
        if (extra.contains("rules") && extra["rules"].is_object())
            for (const auto& [key, value] : extra["rules"].items())
                if (!mapped_field(key, kRuleFields, std::size(kRuleFields))) doc["rules"][key] = value;
        if (extra.contains("classes") && extra["classes"].is_object()) {
            for (const auto& [name, values] : extra["classes"].items()) {
                if (!doc["net_classes"].contains(name) || !values.is_object()) continue;
                for (const auto& [key, value] : values.items())
                    if (!mapped_field(key, kClassFields, std::size(kClassFields)))
                        doc["net_classes"][name][key] = value;
            }
        }
    }
    const json* assignments = settings ? object_member(*settings, "netclass_assignments") : nullptr;
    const json* patterns = settings ? object_member(*settings, "netclass_patterns") : nullptr;
    if (!doc.contains("nets") || !doc["nets"].is_array()) return;
    for (auto& net : doc["nets"]) {
        if (!net.is_object() || !net.contains("name") || !net["name"].is_string()) continue;
        const std::string name = net["name"].get<std::string>();
        std::string cls;
        if (assignments && assignments->is_object()) {
            const auto it = assignments->find(name);
            if (it != assignments->end()) {
                if (it->is_string()) cls = it->get<std::string>();
                else if (it->is_array() && !it->empty() && (*it)[0].is_string())
                    cls = (*it)[0].get<std::string>();
            }
        }
        if (cls.empty() && patterns && patterns->is_array()) {
            for (const auto& entry : *patterns) {
                if (entry.is_object() && entry.contains("pattern") && entry["pattern"].is_string() &&
                    entry.contains("netclass") && entry["netclass"].is_string() &&
                    net_pattern_matches(entry["pattern"].get<std::string>(), name)) {
                    cls = entry["netclass"].get<std::string>();
                    break;
                }
            }
        }
        if (!cls.empty()) net["class"] = native_class(cls);
    }
}

json merge_project_settings(json project, const json& current, const json& baseline, bool same_target) {
    const json old_classes = baseline.is_object() ? baseline.value("classes", json::object())
                                                   : json::object();
    const json old_rules = baseline.is_object() ? baseline.value("rules", json::object())
                                                 : json::object();
    const json old_assignments = baseline.is_object() ? baseline.value("assignments", json::object())
                                                       : json::object();
    const json& classes = current.at("classes");
    const json& rules = current.at("rules");
    const json& assignments = current.at("assignments");
    const bool classes_changed = !same_target || !baseline.is_object() || classes != old_classes;
    const bool rules_changed = !same_target || !baseline.is_object() || rules != old_rules;
    const bool assignments_changed = !same_target || !baseline.is_object() ||
                                     assignments != old_assignments;
    json extra = json::object();
    const json* vars = object_member(project, "text_variables");
    if (vars && vars->is_object() && vars->contains(kAnalysisVariable)) {
        const json& stored = (*vars)[kAnalysisVariable];
        if (!stored.is_string()) throw OpError("io_error", "invalid Chudware project analysis data");
        extra = json::parse(stored.get<std::string>(), nullptr, false);
        if (!extra.is_object()) throw OpError("io_error", "invalid Chudware project analysis data");
    }
    if (classes_changed) {
        json& net_settings = ensure_object(project, "net_settings");
        json existing = net_settings.value("classes", json::array());
        if (!existing.is_array()) throw OpError("io_error", "invalid KiCad project net classes");
        json updated = json::array();
        std::set<std::string> seen;
        auto merge_class = [&](const std::string& name, json entry) {
            const json& values = classes.at(name);
            entry["name"] = kicad_class(name);
            for (const auto& [native, kicad] : kClassFields)
                if (values.contains(native) && values[native].is_number()) entry[kicad] = values[native];
            if (values.contains("diff_gap") && !values.contains("diff_width") &&
                values.contains("track_width") && values["track_width"].is_number())
                entry["diff_pair_width"] = values["track_width"];
            updated.push_back(std::move(entry));
            seen.insert(name);
        };
        for (const auto& entry : existing) {
            if (!entry.is_object() || !entry.contains("name") || !entry["name"].is_string()) {
                updated.push_back(entry);
                continue;
            }
            const std::string name = native_class(entry["name"].get<std::string>());
            if (classes.contains(name) && classes[name].is_object()) merge_class(name, entry);
            else if (!same_target || !old_classes.contains(name)) updated.push_back(entry);
        }
        for (const auto& [name, values] : classes.items()) {
            if (!values.is_object() || seen.count(name)) continue;
            json entry{{"bus_width", 12}, {"line_style", 0}, {"microvia_diameter", 0.3},
                       {"microvia_drill", 0.1}, {"pcb_color", "rgba(0, 0, 0, 0.000)"},
                       {"schematic_color", "rgba(0, 0, 0, 0.000)"}, {"wire_width", 6},
                       {"priority", name == "default" ? 2147483647 : 0}};
            merge_class(name, std::move(entry));
        }
        net_settings["classes"] = std::move(updated);
        json& meta = ensure_object(net_settings, "meta");
        if (!meta.contains("version") || !meta["version"].is_number_integer() ||
            meta["version"].get<int>() < 5) meta["version"] = 5;
        json& class_extra = ensure_object(extra, "classes");
        if (same_target && old_classes.is_object())
            for (const auto& [name, unused] : old_classes.items())
                if (!classes.contains(name)) class_extra.erase(name);
        for (const auto& [name, values] : classes.items()) {
            if (!values.is_object()) continue;
            json unknown = json::object();
            for (const auto& [key, value] : values.items())
                if (!mapped_field(key, kClassFields, std::size(kClassFields))) unknown[key] = value;
            if (unknown.empty()) class_extra.erase(name);
            else class_extra[name] = std::move(unknown);
        }
        if (class_extra.empty()) extra.erase("classes");
    }
    if (rules_changed) {
        json& board = ensure_object(project, "board");
        json& design = ensure_object(board, "design_settings");
        json& target_rules = ensure_object(design, "rules");
        for (const auto& [native, kicad] : kRuleFields)
            if (rules.contains(native) && rules[native].is_number()) target_rules[kicad] = rules[native];
        json unknown = json::object();
        for (const auto& [key, value] : rules.items())
            if (!mapped_field(key, kRuleFields, std::size(kRuleFields))) unknown[key] = value;
        if (unknown.empty()) extra.erase("rules");
        else extra["rules"] = std::move(unknown);
    }
    if (assignments_changed) {
        json& net_settings = ensure_object(project, "net_settings");
        json& target_assignments = ensure_object(net_settings, "netclass_assignments");
        for (const auto& [name, value] : assignments.items()) {
            if (!value.is_string()) continue;
            if (same_target && old_assignments.contains(name) && old_assignments[name] == value) continue;
            target_assignments[name] = json::array({kicad_class(value.get<std::string>())});
        }
    }
    if (classes_changed || rules_changed) {
        json& variables = ensure_object(project, "text_variables");
        if (extra.empty()) variables.erase(kAnalysisVariable);
        else variables[kAnalysisVariable] = extra.dump();
    }
    return project;
}

json import_file(const fs::path& path) {
    const std::string ext = extension(path);
    const char* which = ext == ".kicad_sch" ? "import_kicad_sch" : "import_kicad_pcb";
    try {
        return ext == ".kicad_sch" ? kicad::import_kicad_sch(path.u8string())
                                    : kicad::import_kicad_pcb(path.u8string());
    } catch (const std::exception& exc) {
        throw OpError("io_error", std::string(which) + " failed for '" + path.u8string() +
                      "': " + exc.what(), {{"path", path.u8string()}});
    }
}

json save_locked(Session& s, const std::string& path, const json& recovery,
                 bool require_missing = false);

// Canonical snapshot (sorted keys, compact) — same intent as Python's
// json.dumps(sort_keys=True, separators=(",", ":")). nlohmann::json::parse round-
// trips it back to an identical object.
std::string snapshot(const json& doc) { return doc.dump(); }

// Flush bounded undo snapshots to a per-document journal.
// Journal path: <doc_path>.ecad_journal.jsonl. Best-effort; never throws.
static void flush_ecad_journal(const Session& s) {
    if (s.path.empty()) return;
    try {
        fs::path journal = fs::u8path(s.path).replace_extension("");
        journal += ".ecad_journal.jsonl";
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

// Refresh native zones for interactive feedback. Imported KiCad fills stay
// untouched until an explicit ecad_fill_zones request.
int fill_zones_into(json& doc, double resolution) {
    if (!doc.contains("zones") || !doc["zones"].is_array() || doc["zones"].empty()) return -1;
    const auto& zones = doc["zones"];
    if (std::all_of(zones.begin(), zones.end(),
                    [](const json& zone) { return zone.contains("kicad_extra"); }))
        return static_cast<int>(zones.size());
    json filled = fill_zones(doc, resolution);
    std::map<json, json> by_id;
    if (filled.contains("zones") && filled["zones"].is_array()) {
        for (const auto& z : filled["zones"])
            by_id[z.contains("id") ? z["id"] : json(nullptr)] =
                z.contains("filled_polys") ? z["filled_polys"] : json::array();
    }
    int n = 0;
    for (auto& zone : doc["zones"]) {
        if (zone.contains("kicad_extra")) {
            ++n;
            continue;
        }
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

json reload_locked(Session& s) {
    if (s.path.empty())
        throw OpError("needs_path", "ecad_reload: document has no bound KiCad path");
    const fs::path path = fs::u8path(s.path);
    const FileStamp before = file_stamp(path);
    if (!before.exists) file_error("ECAD file does not exist", path);
    json doc = import_file(path);
    const FileStamp after = file_stamp(path);
    if (before != after)
        throw OpError("external_change", "ECAD file changed while reloading", {{"path", s.path}});
    if (schema::doc_kind(doc) != schema::doc_kind(s.doc))
        throw OpError("invalid_op", "reloaded document kind does not match session");
    ProjectSnapshot project = read_project(path);
    if (schema::is_board(doc)) import_project_settings(doc, project.data);
    doc["id"] = s.doc_id;
    s.doc = std::move(doc);
    s.stamp = after;
    s.project_stamp = project.stamp;
    s.rules_stamp = project.rules_stamp;
    s.project_document = project.stamp.exists ? std::move(project.data) : json(nullptr);
    s.custom_rules = std::move(project.rules);
    s.saved_settings = schema::is_board(s.doc) ? board_settings(s.doc) : json(nullptr);
    s.dirty = false;
    s.undo_stack.clear();
    s.redo_stack.clear();
    ++s.revision;
    json result = ops_response(s, json::object(), json::array(), false);
    result["doc_id"] = s.doc_id;
    result["path"] = s.path;
    result["project"] = project_path(s.path);
    return result;
}

}  // namespace

json kicad_project_settings(const json& project, const json& board) {
    if (!project.is_object() || !schema::is_board(board))
        throw OpError("invalid_op", "KiCad project settings require a project object and PCB");
    return merge_project_settings(project, board_settings(board), nullptr, false);
}

bool dirty_document_count(std::uint64_t& count) {
    std::unique_lock<std::mutex> lock(g_mtx, std::try_to_lock);
    if (!lock.owns_lock()) return false;
    for (const auto& [id, session] : g_sessions) if (session.dirty) ++count;
    return true;
}

json ecad_new(const std::string& kind, const std::string& name, const std::string& path) {
    EcadLock lk;
    kicad_libs::set_project_dir(path.empty() ? std::string() :
                                fs::absolute(fs::u8path(path)).parent_path().u8string());
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
    s.doc["id"] = s.doc_id;
    const std::string id = s.doc_id;
    Session& stored = g_sessions[id] = std::move(s);
    try {
        if (!path.empty() && kind == "pcb") {
            const auto project = read_project(fs::u8path(path));
            import_project_settings(stored.doc, project.data);
            const json* net_settings = object_member(project.data, "net_settings");
            const json* classes = net_settings ? object_member(*net_settings, "classes") : nullptr;
            if (classes && classes->is_array() && !classes->empty())
                stored.saved_settings = board_settings(stored.doc);
        }
        if (!path.empty()) save_locked(stored, path, nullptr, true);
        json resp = ops_response(stored, json::object(), json::array(), false);
        resp["doc_id"] = id;
        resp["path"] = stored.path;
        resp["project"] = project_path(stored.path);
        return resp;
    } catch (...) {
        g_sessions.erase(id);
        throw;
    }
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
    if (res["doc"] == s.doc)
        return ops_response(s, res["id_map"],
                            res.contains("warnings") ? res["warnings"] : json::array());

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
    const FileStamp current = s.path.empty() ? FileStamp{} : file_stamp(fs::u8path(s.path));
    const bool project_changed = !s.path.empty() &&
        (file_stamp(companion_project(fs::u8path(s.path))) != s.project_stamp ||
         file_stamp(companion_rules(fs::u8path(s.path))) != s.rules_stamp);
    const bool changed = !s.path.empty() && (current != s.stamp || project_changed);
    if (changed && current.exists && !s.dirty) reload_locked(s);
    json cf = s.last_compile.is_null() ? compile_field(s.doc, false) : s.last_compile;
    s.last_compile = cf;
    return json{{"revision", s.revision}, {"kind", schema::doc_kind(s.doc)},
                {"tree", s.doc}, {"compile", cf}, {"path", s.path},
                {"file_changed", changed}, {"kicad_locked", kicad_locked(fs::u8path(s.path))},
                {"dirty", s.dirty}, {"project", project_path(s.path)}};
}

json ecad_reload(const std::string& doc_id, bool discard) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    if (s.dirty && !discard)
        throw OpError("dirty", "ecad_reload: document has unsaved edits", {{"doc_id", doc_id}});
    return reload_locked(s);
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
                               {"net", v->contains("net") ? v->at("net") : json("")},
                               {"message", v->value("message", std::string())}});
        }
        return out;
    };

    json out{{"kind", schema::doc_kind(doc)},
             {"name", doc.value("name", std::string())},
             {"revision", s.revision},
             {"compiled", compiled},
             {"path", s.path}};

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



// ------------------------------------------------------------------- save / open

namespace {

fs::path recovery_dir(const json& metadata) {
    if (metadata.is_object() && metadata.contains("recovery_dir")) {
        if (!metadata["recovery_dir"].is_string() || metadata["recovery_dir"].get<std::string>().empty())
            throw OpError("invalid_op", "ecad_save: recovery_dir must be a nonempty path");
        return fs::u8path(metadata["recovery_dir"].get<std::string>());
    }
    if (const char* p = std::getenv("CHUDWARE_ECAD_AUTOSAVE_DIR"); p && *p) return fs::u8path(p);
    const char* appdata = std::getenv("APPDATA");
    return (appdata && *appdata ? fs::u8path(appdata) : fs::temp_directory_path()) /
           "Chudware" / "ecad_autosave";
}

unsigned long recovery_pid() {
#ifdef _WIN32
    return GetCurrentProcessId();
#else
    return static_cast<unsigned long>(getpid());
#endif
}

bool in_recovery_dir(const fs::path& target, const fs::path& directory) {
    std::error_code ec;
    const auto root = fs::weakly_canonical(directory, ec);
    if (ec) file_error("cannot resolve ECAD recovery directory", directory);
    const auto resolved = fs::weakly_canonical(target, ec);
    if (ec) file_error("cannot resolve ECAD recovery path", target);
    const auto relative = resolved.lexically_relative(root);
    return !relative.empty() && relative != "." && *relative.begin() != "..";
}

json save_locked(Session& s, const std::string& path, const json& recovery, bool require_missing) {
    const bool snapshot = recovery.is_object() || (recovery.is_boolean() && recovery.get<bool>());
    const fs::path root = snapshot ? recovery_dir(recovery) : fs::path{};
    const std::string kind = schema::doc_kind(s.doc);
    const bool board = kind == "pcb";
    const std::string expected = board ? ".kicad_pcb" : ".kicad_sch";
    std::string target = path.empty() ? s.path : path;
    if (snapshot && path.empty()) {
        static std::atomic<unsigned long long> sequence{0};
        target = (root /
            ("recovery-" + std::to_string(recovery_pid()) + "-" + s.doc_id + "-" +
             std::to_string(std::chrono::system_clock::now().time_since_epoch().count()) + "-" +
             std::to_string(sequence.fetch_add(1)) + expected)).u8string();
    }
    if (target.empty()) throw OpError("needs_path", "ecad_save: choose a KiCad file path");
    const fs::path target_path = fs::u8path(target);
    const fs::path bound_path = fs::u8path(s.path);
    const fs::path project_path = companion_project(target_path);
    const fs::path rules_path = companion_rules(target_path);
    const bool same_target = !s.path.empty() && same_path(target_path, bound_path);
    const bool save_as = !snapshot && !s.path.empty() && !same_target;
    const bool bound_schematic = !snapshot && same_target && !board;
    const bool require_new = require_missing || (!snapshot && s.path.empty());
    if (snapshot && (!in_recovery_dir(target_path, root) || same_target))
        throw OpError("io_error", "ecad_save: recovery requires a separate path in the recovery directory");
    if (extension(target_path) != expected)
        throw OpError("invalid_op", "ecad_save: " + kind + " requires a " + expected + " path",
                      {{"path", target}});
    bool watch_project = false, write_project = false;
    bool write_rules = false;
    FileStamp target_project_stamp, target_rules_stamp;
    auto check_project = [&]() {
        if (write_project && kicad_locked(project_path))
            throw OpError("kicad_locked", "KiCad has the project file open",
                          {{"path", project_path.u8string()}});
        if (file_stamp(project_path) != target_project_stamp)
            throw OpError("external_change", "KiCad project changed outside this session",
                          {{"path", project_path.u8string()}});
    };
    auto check_rules = [&]() {
        if (write_rules && kicad_locked(rules_path))
            throw OpError("kicad_locked", "KiCad has the custom rules file open",
                          {{"path", rules_path.u8string()}});
        if (file_stamp(rules_path) != target_rules_stamp)
            throw OpError("external_change", "KiCad custom rules changed outside this session",
                          {{"path", rules_path.u8string()}});
    };
    auto check = [&]() {
        if (kicad_locked(target_path))
            throw OpError("kicad_locked", "KiCad has the target file open", {{"path", target}});
        if (!snapshot && same_target && file_stamp(bound_path) != s.stamp)
            throw OpError("external_change", "KiCad file changed outside this session",
                          {{"path", s.path}});
        if (!snapshot && board && same_target &&
            file_stamp(companion_project(bound_path)) != s.project_stamp)
            throw OpError("external_change", "KiCad project changed outside this session",
                          {{"path", companion_project(bound_path).u8string()}});
        if (require_new && fs::exists(target_path))
            throw OpError("io_error", "ecad_save: target already exists", {{"path", target}});
        if (watch_project) check_project();
    };
    check();
    const fs::path library_doc = !s.path.empty() ? bound_path : target_path;
    kicad_libs::set_project_dir(fs::absolute(library_doc).parent_path().u8string());
    json document = s.doc;
    kicad::assign_uuids(document);
    const std::string bytes = board ? kicad::write_kicad_pcb(document)
                                    : kicad::write_kicad_sch(document);
    ProjectSnapshot target_project;
    json settings;
    json merged_project;
    std::string project_bytes;
    bool update_project = false;
    if (!snapshot && !bound_schematic) {
        target_project = read_project(target_path);
        const bool existed = target_project.stamp.exists;
        const ProjectSnapshot source = save_as ? read_project(bound_path) : ProjectSnapshot{};
        const bool source_project = save_as && source.stamp.exists;
        if (save_as && existed &&
            (!source_project || !same_project_document(source.data, target_project.data)))
            throw OpError("external_change", "Save As target has a different KiCad project",
                          {{"path", project_path.u8string()}});
        if (save_as && target_project.rules_stamp.exists &&
            (!source.rules_stamp.exists || target_project.rules != source.rules))
            throw OpError("external_change", "Save As target has different KiCad custom rules",
                          {{"path", rules_path.u8string()}});

        if (!existed) {
            if (source_project) {
                json copy = source.data;
                ensure_object(copy, "meta")["filename"] = project_path.filename().u8string();
                const std::string copy_bytes = copy.dump(2) + "\n";
                target_project_stamp = target_project.stamp;
                write_project = true;
                check_project();
                atomic_write(project_path, copy_bytes, check_project, true, true);
                target_project.stamp = file_stamp(project_path);
                if (!target_project.stamp.exists || target_project.stamp.size != copy_bytes.size() ||
                    target_project.stamp.hash != content_hash(copy_bytes))
                    throw OpError("external_change", "KiCad project changed while copying",
                                  {{"path", project_path.u8string()}});
                target_project.data = std::move(copy);
                write_project = false;
            } else {
                ensure_project(target_path, true);
                target_project = read_project(target_path);
                if (same_target) s.project_stamp = target_project.stamp;
            }
        }
        if (save_as && source.rules_stamp.exists && !target_project.rules_stamp.exists) {
            target_rules_stamp = target_project.rules_stamp;
            write_rules = true;
            check_rules();
            atomic_write(rules_path, source.rules, check_rules, true, true);
            target_project.rules_stamp = file_stamp(rules_path);
            if (!target_project.rules_stamp.exists ||
                target_project.rules_stamp.size != source.rules.size() ||
                target_project.rules_stamp.hash != content_hash(source.rules))
                throw OpError("external_change", "KiCad custom rules changed while copying",
                              {{"path", rules_path.u8string()}});
            target_project.rules = source.rules;
            write_rules = false;
        }
        target_project_stamp = target_project.stamp;
        target_rules_stamp = target_project.rules_stamp;
        if (board && same_target && target_project.stamp != s.project_stamp)
            throw OpError("external_change", "KiCad project changed outside this session",
                          {{"path", project_path.u8string()}});
        if (board) {
            settings = board_settings(document);
            const bool existing_new_board = require_missing && s.saved_settings.is_object() &&
                                            settings == s.saved_settings && existed;
            const bool source_baseline = same_target || source_project;
            update_project = !existing_new_board &&
                ((!existed && !source_project) || !source_baseline ||
                 !s.saved_settings.is_object() || settings != s.saved_settings);
            if (update_project) {
                watch_project = true;
                write_project = true;
                check_project();
                merged_project = merge_project_settings(target_project.data, settings,
                                                        s.saved_settings, source_baseline);
                project_bytes = merged_project.dump(2) + "\n";
            }
        }
    }
    atomic_write(target_path, bytes, check, require_new, require_new);
    const FileStamp written = file_stamp(target_path);
    if (!written.exists || written.size != bytes.size() || written.hash != content_hash(bytes))
        throw OpError("external_change", "ECAD file changed while saving", {{"path", target}});
    if (snapshot) {
        json metadata = recovery.is_object() ? recovery : json::object();
        if (!metadata.contains("pid")) metadata["pid"] = recovery_pid();
        if (!metadata.contains("doc_id")) metadata["doc_id"] = s.doc_id;
        if (!metadata.contains("name")) metadata["name"] = s.doc.value("name", std::string());
        if (!metadata.contains("kind")) metadata["kind"] = kind;
        metadata["human_dirty"] = true;
        metadata["revision"] = s.revision;
        metadata["path"] = s.path;
        metadata["snapshot"] = target;
        metadata["baseline"] = stamp_json(s.stamp);
        metadata["snapshot_size"] = written.size;
        metadata["snapshot_hash"] = std::to_string(written.hash);
        metadata["project_baseline"] = stamp_json(s.project_stamp);
        metadata["rules_baseline"] = stamp_json(s.rules_stamp);
        metadata["project_document"] = s.project_document;
        if (s.rules_stamp.exists) metadata["custom_rules"] = s.custom_rules;
        if (board) {
            metadata["board_settings"] = board_settings(document);
            metadata["saved_board_settings"] = s.saved_settings;
        }
        fs::path sidecar = target_path; sidecar += ".recovery.json";
        atomic_write(sidecar, metadata.dump(2) + "\n");
        s.doc = std::move(document);
    } else {
        s.path = target;
        s.stamp = written;
        s.doc = std::move(document);
        s.dirty = true;
        if (bound_schematic) {
            // Saving a schematic does not write project settings or rules.
            // Do not let unrelated companion edits block it, and discard any
            // cached companion content whose fingerprint is no longer current.
            const auto project_stamp = file_stamp(project_path);
            const auto rules_stamp = file_stamp(rules_path);
            if (project_stamp != s.project_stamp) s.project_document = nullptr;
            if (rules_stamp != s.rules_stamp) s.custom_rules.clear();
            s.project_stamp = project_stamp;
            s.rules_stamp = rules_stamp;
        } else {
            s.project_stamp = target_project.stamp;
            s.rules_stamp = target_project.rules_stamp;
            s.project_document = target_project.stamp.exists ? std::move(target_project.data) : json(nullptr);
            s.custom_rules = std::move(target_project.rules);
        }
        if (board) {
            s.saved_settings = nullptr;
            if (update_project) {
                atomic_write(project_path, project_bytes, check_project);
                const auto project_written = file_stamp(project_path);
                if (!project_written.exists || project_written.size != project_bytes.size() ||
                    project_written.hash != content_hash(project_bytes))
                    throw OpError("external_change", "KiCad project changed while saving",
                                  {{"path", project_path.u8string()}});
                // A PCB save may update the shared project settings. Advance
                // sibling schematic baselines only when they still match the
                // exact project revision this save replaced.
                for (auto& [other_id, other] : g_sessions) {
                    if (&other == &s || other.path.empty() || !schema::is_schematic(other.doc) ||
                        other.project_stamp != target_project_stamp ||
                        !same_path(companion_project(fs::u8path(other.path)), project_path))
                        continue;
                    other.project_stamp = project_written;
                    other.project_document = merged_project;
                }
                s.project_stamp = project_written;
                s.project_document = std::move(merged_project);
            }
            s.saved_settings = std::move(settings);
        }
        s.dirty = false;
    }
    return {{"path", target}, {"format", "kicad"}, {"saved_revision", s.revision}};
}

}  // namespace

json ecad_save(const std::string& doc_id, const std::string& path, const json& recovery) {
    EcadLock lk;
    return save_locked(require_session(doc_id), path, recovery);
}

json ecad_open(const std::string& path) {
    EcadLock lk;
    if (path.empty()) throw OpError("io_error", "ecad_open: path is required");
    const fs::path input = fs::u8path(path);
    kicad_libs::set_project_dir(fs::absolute(input).parent_path().u8string());
    const std::string suffix = extension(input);
    const bool kicad_file = suffix == ".kicad_sch" || suffix == ".kicad_pcb";
    json doc, metadata;
    FileStamp stamp;
    std::vector<std::string> load_warnings;
    if (kicad_file) {
        const FileStamp before = file_stamp(input);
        if (!before.exists) file_error("ECAD file does not exist", input);
        doc = import_file(input);
        stamp = file_stamp(input);
        if (before != stamp)
            throw OpError("external_change", "ECAD file changed while opening", {{"path", path}});
        fs::path sidecar = input; sidecar += ".recovery.json";
        if (fs::exists(sidecar)) {
            std::ifstream in(sidecar);
            try {
                in >> metadata;
                if (!metadata.value("human_dirty", false) ||
                    !metadata.contains("snapshot") || !metadata["snapshot"].is_string() ||
                    !same_path(fs::u8path(metadata["snapshot"].get<std::string>()), input) ||
                    metadata.value("snapshot_size", std::uintmax_t{0}) != stamp.size ||
                    metadata.value("snapshot_hash", std::string()) != std::to_string(stamp.hash))
                    throw std::runtime_error("snapshot metadata does not match file");
            } catch (const std::exception& exc) {
                throw OpError("io_error", std::string("invalid ECAD recovery metadata: ") + exc.what(),
                              {{"path", sidecar.u8string()}});
            }
        }
    } else if (suffix == ".ecad+" || suffix == ".ecad" || suffix == ".json") {
        std::ifstream stream(input, std::ios::binary);
        if (!stream) file_error("cannot read legacy ECAD file", input);
        if ((stream >> std::ws).peek() == '{') {
            try {
                json payload;
                stream >> payload;
                if (payload.is_object() && payload.contains("doc") && payload["doc"].is_object()) {
                    doc = payload["doc"];
                    metadata = payload;
                } else {
                    doc = std::move(payload);
                }
            } catch (const std::exception& exc) {
                throw OpError("io_error", std::string("invalid legacy ECAD JSON: ") + exc.what(),
                              {{"path", path}});
            }
        } else {
            auto loaded = ecad_container_open(path);
            doc = std::move(loaded.doc);
            load_warnings = std::move(loaded.warnings);
            const auto marker = loaded.extra.find("chudware/recovery.json");
            if (marker != loaded.extra.end()) metadata = json::parse(marker->second, nullptr, false);
            for (const auto& [name, bytes] : loaded.extra) {
                (void)bytes;
                if (name != "thumbnail.png" && name != "chudware/recovery.json")
                    load_warnings.push_back("Legacy attachment is not included in KiCad migration: " + name);
            }
        }
        load_warnings.push_back("Legacy ECAD document imported for migration; save to a KiCad file.");
    } else {
        throw OpError("invalid_op", "ecad_open: unsupported ECAD file extension", {{"path", path}});
    }
    if (!(schema::is_schematic(doc) || schema::is_board(doc)))
        throw OpError("invalid_op", "not an ECAD document", {{"path", path}});
    const bool recovered = metadata.is_object() && metadata.value("human_dirty", false);
    ProjectSnapshot project;
    if (recovered && schema::is_board(doc) && metadata.contains("board_settings"))
        restore_board_settings(doc, metadata["board_settings"]);
    else if (kicad_file && !recovered) {
        project = read_project(input);
        if (schema::is_board(doc)) import_project_settings(doc, project.data);
    }

    Session s;
    s.doc_id = new_doc_id();
    s.doc = std::move(doc);
    s.doc["id"] = s.doc_id;
    if (recovered) {
        if (metadata.contains("name") && metadata["name"].is_string() &&
            !metadata["name"].get<std::string>().empty())
            s.doc["name"] = metadata["name"];
        const std::string original = metadata.contains("path") && metadata["path"].is_string()
                                         ? metadata["path"].get<std::string>() : std::string();
        const std::string expected = schema::is_schematic(s.doc) ? ".kicad_sch" : ".kicad_pcb";
        if (!original.empty() && extension(fs::u8path(original)) == expected) {
            s.path = original;
            // An old recovery with no fingerprint must not overwrite an existing file.
            s.stamp = stamp_from_json(metadata.value("baseline", json::object()));
            s.project_stamp = stamp_from_json(metadata.value("project_baseline", json::object()));
            s.rules_stamp = stamp_from_json(metadata.value("rules_baseline", json::object()));
            if (metadata.contains("project_document") && metadata["project_document"].is_object())
                s.project_document = metadata["project_document"];
            if (metadata.contains("custom_rules") && metadata["custom_rules"].is_string())
                s.custom_rules = metadata["custom_rules"].get<std::string>();
            if (schema::is_board(s.doc) && metadata.contains("saved_board_settings") &&
                metadata["saved_board_settings"].is_object())
                s.saved_settings = metadata["saved_board_settings"];
        }
        s.dirty = true;
        load_warnings.push_back("Recovered unsaved ECAD work.");
    } else if (kicad_file) {
        s.path = path;
        s.stamp = stamp;
        s.project_stamp = project.stamp;
        s.rules_stamp = project.rules_stamp;
        s.project_document = project.stamp.exists ? std::move(project.data) : json(nullptr);
        s.custom_rules = std::move(project.rules);
        if (schema::is_board(s.doc)) s.saved_settings = board_settings(s.doc);
    }
    const std::string id = s.doc_id;
    Session& stored = g_sessions[id] = std::move(s);
    try {
        json resp = ops_response(stored, json::object(), json::array(), false);
        resp["doc_id"] = id;
        resp["path"] = stored.path;
        resp["project"] = project_path(stored.path);
        resp["warnings"] = load_warnings;
        return resp;
    } catch (...) {
        g_sessions.erase(id);
        throw;
    }
}

json ecad_new_project(const std::string& dir, const std::string& name) {
    if (dir.empty() || name.empty() || name == "." || name == ".." ||
        fs::u8path(name).filename().u8string() != name)
        throw OpError("invalid_op", "ecad_new_project requires a directory and a project name");
    const fs::path base = fs::u8path(dir) / fs::u8path(name);
    fs::path project = base; project += ".kicad_pro";
    fs::path sch_path = base; sch_path += ".kicad_sch";
    fs::path pcb_path = base; pcb_path += ".kicad_pcb";
    for (const auto& file : {project, sch_path, pcb_path})
        if (fs::exists(file))
            throw OpError("io_error", "ecad_new_project: file already exists", {{"path", file.u8string()}});
    ensure_project(sch_path, true);
    json sch, pcb;
    try {
        sch = ecad_new("sch", name, sch_path.u8string());
        pcb = ecad_new("pcb", name, pcb_path.u8string());
    } catch (...) {
        if (sch.is_object()) ecad_close(sch.value("doc_id", std::string()));
        if (pcb.is_object()) ecad_close(pcb.value("doc_id", std::string()));
        throw;
    }
    return {{"project", project.u8string()}, {"sch", sch}, {"pcb", pcb}};
}

json ecad_open_project(const std::string& path) {
    const fs::path input = fs::u8path(path);
    const std::string ext = extension(input);
    if (path.empty() || (ext != ".kicad_pro" && ext != ".kicad_sch" && ext != ".kicad_pcb"))
        throw OpError("invalid_op", "ecad_open_project requires a KiCad project, schematic, or board");
    if (!fs::exists(input)) file_error("KiCad project file does not exist", input);
    fs::path project = input; project.replace_extension(".kicad_pro");
    fs::path sch_path = input; sch_path.replace_extension(".kicad_sch");
    fs::path pcb_path = input; pcb_path.replace_extension(".kicad_pcb");
    json sch = nullptr, pcb = nullptr;
    try {
        if (fs::exists(sch_path)) sch = ecad_open(sch_path.u8string());
        if (fs::exists(pcb_path)) pcb = ecad_open(pcb_path.u8string());
    } catch (...) {
        if (sch.is_object()) ecad_close(sch.value("doc_id", std::string()));
        if (pcb.is_object()) ecad_close(pcb.value("doc_id", std::string()));
        throw;
    }
    return {{"project", project.u8string()}, {"sch", sch}, {"pcb", pcb}};
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
    if (s.doc == new_doc)
        return ops_response(s, json::object(), warnings, refresh_zone_fill);
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
    // Netlist resolution uses the schematic's tables; footprint placement uses
    // the board project's tables, which can belong to a different project.
    (void)require_session(board_doc_id);

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
    if (s.doc != res["doc"]) {
        push_undo(s);
        s.doc = res["doc"];
        s.revision += 1;
        s.dirty = true;
    }
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

json session_file_info(const std::string& doc_id, bool include_doc) {
    EcadLock lk;
    Session& s = require_session(doc_id);
    json result{{"path", s.path}, {"kind", schema::doc_kind(s.doc)}, {"dirty", s.dirty},
                {"revision", s.revision}};
    if (include_doc) result["doc"] = s.doc;
    return result;
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
            return schema::pad_has_layer(e, layer);
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

// Recipes address actual KiCad pins by number.
const json* find_pin(const json& def, const std::string& want) {
    auto pins = def.find("pins");
    if (pins == def.end() || !pins->is_array()) return nullptr;
    for (const auto& p : *pins)
        if (p.value("number", std::string()) == want) return &p;
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
    std::set<std::string> flagged_supplies;
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

            const bool supply = is_supply_net(net);
            const bool flag = supply && flagged_supplies.insert(net).second;
            const json tip_pos = json::array({tip.first, tip.second});
            const json end_pos = json::array({ex, ey});
            if (flag) {
                // KiCad connects a flag to a wire endpoint, not its interior.
                const json midpoint = json::array({(tip.first + ex) / 2.0,
                                                   (tip.second + ey) / 2.0});
                wire_ops.push_back(json{{"op", "sch_add_wire"},
                                        {"pts", json::array({tip_pos, midpoint})}});
                wire_ops.push_back(json{{"op", "sch_add_wire"},
                                        {"pts", json::array({midpoint, end_pos})}});
                wire_ops.push_back(json{{"op", "sch_add_power"}, {"pos", midpoint},
                                        {"net", net}, {"symbol", "PWR_FLAG"}});
            } else {
                wire_ops.push_back(json{{"op", "sch_add_wire"},
                                        {"pts", json::array({tip_pos, end_pos})}});
            }
            if (supply) {
                wire_ops.push_back(json{{"op", "sch_add_power"},
                                        {"pos", json::array({ex, ey})},
                                        {"net", net},
                                        {"symbol", power_symbol_for(net)}});
            } else
                wire_ops.push_back(json{{"op", "sch_add_label"},
                                        {"pos", json::array({ex, ey})},
                                        {"text", net},
                                        {"scope", "local"}});
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
    if (design.contains("spice_directives") && design["spice_directives"].is_string())
        meta_ops.push_back(json{{"op", "sch_add_text"}, {"pos", json::array({0.0, -90.0})},
                                {"text", design["spice_directives"]}, {"size", 1.0}});
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
