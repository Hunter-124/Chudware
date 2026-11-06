#include "ecad/kicad_libs.h"

#include "ecad/kicad.h"
#include "util/subprocess.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <system_error>
#include <vector>

#ifndef _WIN32
#include <unistd.h>
#endif

namespace chudware::ecad::kicad_libs {
namespace {
using nlohmann::json;
namespace fs = std::filesystem;

std::string env(const char* name) {
    const char* value = std::getenv(name);
    return value ? value : "";
}
std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}
std::string trim(std::string value) {
    const auto start = value.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) return {};
    return value.substr(start, value.find_last_not_of(" \t\r\n") - start + 1);
}
bool is_file(const fs::path& path) {
    std::error_code ec;
    return fs::is_regular_file(path, ec);
}
bool is_dir(const fs::path& path) {
    std::error_code ec;
    return fs::is_directory(path, ec);
}
bool executable(const fs::path& path) {
    if (!is_file(path)) return false;
#ifndef _WIN32
    return ::access(path.c_str(), X_OK) == 0;
#else
    return true;
#endif
}
std::string exe_name(const std::string& name) {
#ifdef _WIN32
    return name + ".exe";
#else
    return name;
#endif
}
std::string absolute_path(const fs::path& path) {
    std::error_code ec;
    auto p = fs::absolute(path, ec);
    return (ec ? path : p).lexically_normal().string();
}
std::string find_on_path(const std::string& name) {
    if (name.empty()) return {};
    const fs::path requested(name);
    if (requested.has_parent_path() || requested.is_absolute())
        return executable(requested) ? absolute_path(requested) : std::string();
#ifdef _WIN32
    constexpr char separator = ';';
#else
    constexpr char separator = ':';
#endif
    std::istringstream paths(env("PATH"));
    std::string part;
    while (std::getline(paths, part, separator)) {
        fs::path candidate = fs::path(part.empty() ? "." : part) / name;
        if (executable(candidate)) return absolute_path(candidate);
#ifdef _WIN32
        if (candidate.extension().empty()) {
            candidate += ".exe";
            if (executable(candidate)) return absolute_path(candidate);
        }
#endif
    }
    return {};
}

std::vector<fs::path> default_bins() {
    std::vector<fs::path> bins;
#ifdef _WIN32
    for (const char* variable : {"ProgramFiles", "ProgramFiles(x86)"}) {
        if (env(variable).empty()) continue;
        const fs::path base = fs::path(env(variable)) / "KiCad";
        std::error_code ec;
        std::vector<fs::path> versions;
        for (fs::directory_iterator it(base, ec), end; !ec && it != end; it.increment(ec))
            if (it->is_directory(ec)) versions.push_back(it->path());
        auto version_numbers = [](const fs::path& path) {
            const std::string name = path.filename().string();
            std::vector<unsigned long> numbers;
            for (std::size_t i = 0; i < name.size();) {
                if (!std::isdigit(static_cast<unsigned char>(name[i]))) { ++i; continue; }
                char* end = nullptr;
                numbers.push_back(std::strtoul(name.c_str() + i, &end, 10));
                i = static_cast<std::size_t>(end - name.c_str());
            }
            return numbers;
        };
        std::sort(versions.begin(), versions.end(), [&](const fs::path& a, const fs::path& b) {
            const auto av = version_numbers(a), bv = version_numbers(b);
            return av != bv ? av > bv : a.string() > b.string();
        });
        for (const auto& version : versions) bins.push_back(version / "bin");
        bins.push_back(base / "bin");
    }
#elif defined(__APPLE__)
    bins.emplace_back("/Applications/KiCad/KiCad.app/Contents/MacOS");
    bins.emplace_back("/Applications/KiCad.app/Contents/MacOS");
    bins.emplace_back("/opt/homebrew/bin");
    bins.emplace_back("/usr/local/bin");
    bins.emplace_back("/usr/bin");
#else
    // /app is the KiCad Flatpak's prefix when Chudware runs in the same sandbox.
    bins.emplace_back("/usr/bin");
    bins.emplace_back("/usr/local/bin");
    bins.emplace_back("/app/bin");
#endif
    return bins;
}

std::string first_directory(const std::vector<fs::path>& choices) {
    for (const auto& p : choices) if (!p.empty() && is_dir(p)) return absolute_path(p);
    return {};
}
Install detect_uncached() {
    Install result;
    const std::string cli_override = env("CHUDWARE_KICAD_CLI");
    const std::string dir_override = env("CHUDWARE_KICAD_DIR");
    if (!cli_override.empty()) {
        result.cli = find_on_path(cli_override);
    } else if (!dir_override.empty()) {
        const fs::path root(dir_override);
        for (const auto& p : {root / "bin" / exe_name("kicad-cli"),
                              root / exe_name("kicad-cli"),
                              root / "Contents" / "MacOS" / exe_name("kicad-cli")}) {
            if (executable(p)) { result.cli = absolute_path(p); break; }
        }
    } else {
        result.cli = find_on_path(exe_name("kicad-cli"));
        if (result.cli.empty())
            for (const auto& bin : default_bins()) {
                const fs::path p = bin / exe_name("kicad-cli");
                if (executable(p)) { result.cli = absolute_path(p); break; }
            }
    }
    const fs::path bin = result.cli.empty() ? fs::path() : fs::path(result.cli).parent_path();
    const fs::path prefix = !dir_override.empty() ? fs::path(dir_override) : bin.parent_path();
    auto sibling = [&](const std::string& name) {
        const fs::path p = bin / exe_name(name);
        if (!bin.empty() && executable(p)) return absolute_path(p);
        return find_on_path(exe_name(name));
    };
    result.kicad = sibling("kicad");
    result.eeschema = sibling("eeschema");
    result.pcbnew = sibling("pcbnew");
    std::vector<fs::path> roots;
    if (!prefix.empty()) {
        roots.push_back(prefix / "share" / "kicad");
        roots.push_back(prefix / "share");
        roots.push_back(prefix / "Contents" / "SharedSupport");
        roots.push_back(prefix / "Contents" / "Resources");
    }
    if (!bin.empty()) {
        roots.push_back(bin.parent_path() / "share" / "kicad");
        roots.push_back(bin.parent_path() / "SharedSupport");
        roots.push_back(bin.parent_path() / "Resources");
    }
#ifdef __APPLE__
    roots.emplace_back("/Applications/KiCad/KiCad.app/Contents/SharedSupport");
    roots.emplace_back("/Applications/KiCad/KiCad.app/Contents/Resources");
#else
    roots.emplace_back("/usr/share/kicad");
    roots.emplace_back("/usr/local/share/kicad");
    roots.emplace_back("/app/share/kicad");
#endif
    auto data_dir = [&](const char* variable, const char* name) {
        const std::string override_dir = env(variable);
        if (!override_dir.empty()) return absolute_path(override_dir);
        std::vector<fs::path> candidates;
        for (const auto& root : roots) {
            candidates.push_back(root / name);
            candidates.push_back(root / "kicad" / name);
        }
        return first_directory(candidates);
    };
    result.symbol_dir = data_dir("KICAD10_SYMBOL_DIR", "symbols");
    result.footprint_dir = data_dir("KICAD10_FOOTPRINT_DIR", "footprints");
    result.model_dir = data_dir("KICAD10_3DMODEL_DIR", "3dmodels");
    result.template_dir = data_dir("KICAD10_TEMPLATE_DIR", "template");
    if (!result.cli.empty()) {
        // A first run in a fresh HOME initialises KiCad's settings and can take
        // several seconds; stderr diagnostics share the stream, so take the
        // last line that looks like a version.
        util::ProcessOptions options;
        options.timeout = std::chrono::seconds(30);
        options.max_output_bytes = 16384;
        const auto process = util::run_process({result.cli, "version"}, options);
        if (!process.timed_out && process.exit_code == 0) {
            std::istringstream lines(process.output);
            for (std::string line; std::getline(lines, line);) {
                line = trim(line);
                if (!line.empty() && std::isdigit(static_cast<unsigned char>(line[0])) &&
                    line.find('.') != std::string::npos)
                    result.version = line;
            }
            result.found = !result.version.empty();
        }
    }
    return result;
}

thread_local std::string project_dir;
struct Stamp {
    bool exists = false;
    fs::file_time_type time{};
    std::uintmax_t size = 0;
    bool operator==(const Stamp& other) const {
        return exists == other.exists && (!exists || (time == other.time && size == other.size));
    }
};
Stamp stamp(const fs::path& path) {
    std::error_code ec;
    Stamp s;
    s.time = fs::last_write_time(path, ec);
    if (ec) return s;
    s.size = fs::file_size(path, ec);
    if (ec) return s;
    s.exists = true;
    return s;
}
std::optional<json> parse_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return std::nullopt;
    std::ostringstream text;
    text << in.rdbuf();
    if (in.bad()) return std::nullopt;
    try { return kicad::sexpr_parse(text.str()); }
    catch (const std::exception&) { return std::nullopt; }
}
std::string atom(const json& node, std::size_t index) {
    return node.is_array() && node.size() > index && node[index].is_string()
               ? node[index].get<std::string>() : std::string();
}
bool tagged(const json& node, const char* tag) { return atom(node, 0) == tag; }
// The KiCad parser deliberately bounds a single tree. Installed symbol libraries
// can exceed that budget, so parse each top-level symbol independently using the
// same parser instead of enlarging the import limit or retaining every library.
std::optional<json> parse_symbol_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return std::nullopt;
    std::ostringstream stream;
    stream << in.rdbuf();
    if (in.bad()) return std::nullopt;
    const std::string text = stream.str();
    const auto root = text.find_first_not_of(" \t\r\n");
    const std::string header = "(kicad_symbol_lib";
    if (root == std::string::npos || text.compare(root, header.size(), header) != 0)
        return std::nullopt;
    json tree = json::array({"kicad_symbol_lib"});
    int depth = 0;
    bool quoted = false, escaped = false, closed = false;
    std::size_t start = 0;
    for (std::size_t i = root; i < text.size(); ++i) {
        const char c = text[i];
        if (quoted) {
            if (escaped) escaped = false;
            else if (c == '\\') escaped = true;
            else if (c == '"') quoted = false;
            continue;
        }
        if (c == '"') { quoted = true; continue; }
        if (c == '(') {
            if (depth == 1) start = i;
            ++depth;
        } else if (c == ')') {
            if (--depth < 0) return std::nullopt;
            if (depth == 1 && text.compare(start, 7, "(symbol") == 0 &&
                start + 7 < text.size() &&
                std::isspace(static_cast<unsigned char>(text[start + 7]))) {
                try {
                    json node = kicad::sexpr_parse(text.substr(start, i - start + 1));
                    if (!tagged(node, "symbol")) return std::nullopt;
                    tree.push_back(std::move(node));
                } catch (const std::exception&) { return std::nullopt; }
            }
            if (depth == 0) {
                closed = true;
                if (text.find_first_not_of(" \t\r\n", i + 1) != std::string::npos)
                    return std::nullopt;
                break;
            }
        }
    }
    return closed && !quoted ? std::optional<json>(std::move(tree)) : std::nullopt;
}
const json* child(const json& node, const char* tag) {
    if (node.is_array()) for (const auto& item : node) if (tagged(item, tag)) return &item;
    return nullptr;
}
std::string child_atom(const json& node, const char* tag) {
    const json* c = child(node, tag);
    return c ? atom(*c, 1) : std::string();
}
std::string property(const json& node, const std::string& name) {
    if (node.is_array()) for (const auto& item : node)
        if (tagged(item, "property") && atom(item, 1) == name) return atom(item, 2);
    return {};
}
std::string expanded(const std::string& uri, const Install& install) {
    auto variable = [&](const std::string& name) {
        if (name == "KIPRJMOD") return project_dir;
        std::string value = env(name.c_str());
        if (!value.empty()) return value;
        if (name == "KICAD10_SYMBOL_DIR") return install.symbol_dir;
        if (name == "KICAD10_FOOTPRINT_DIR") return install.footprint_dir;
        if (name == "KICAD10_3DMODEL_DIR") return install.model_dir;
        if (name == "KICAD10_TEMPLATE_DIR") return install.template_dir;
        return value;
    };
    std::string result;
    for (std::size_t i = 0; i < uri.size();) {
        if (uri[i] == '$' && i + 1 < uri.size() &&
            (uri[i + 1] == '{' || uri[i + 1] == '(')) {
            const char closing = uri[i + 1] == '{' ? '}' : ')';
            const auto end = uri.find(closing, i + 2);
            if (end != std::string::npos) {
                const std::string name = uri.substr(i + 2, end - i - 2);
                const std::string value = variable(name);
                result += value.empty() ? uri.substr(i, end - i + 1) : value;
                i = end + 1;
                continue;
            }
        }
        if (uri[i] == '%') {
            const auto end = uri.find('%', i + 1);
            if (end != std::string::npos && end > i + 1) {
                const std::string name = uri.substr(i + 1, end - i - 1);
                const std::string value = variable(name);
                result += value.empty() ? uri.substr(i, end - i + 1) : value;
                i = end + 1;
                continue;
            }
        }
        result += uri[i++];
    }
    if (result.size() >= 2 && result[0] == '~' && (result[1] == '/' || result[1] == '\\')) {
        const std::string home = env("HOME").empty() ? env("USERPROFILE") : env("HOME");
        if (!home.empty()) result.replace(0, 1, home);
    }
    return result;
}
fs::path config_dir() {
#ifdef _WIN32
    return fs::path(env("APPDATA")) / "kicad" / "10.0";
#elif defined(__APPLE__)
    return fs::path(env("HOME")) / "Library" / "Preferences" / "kicad" / "10.0";
#else
    std::string config = env("XDG_CONFIG_HOME");
    if (config.empty()) config = (fs::path(env("HOME")) / ".config").string();
    return fs::path(config) / "kicad" / "10.0";
#endif
}
struct Lib { std::string nickname, uri, scope; };
struct TableCache { Stamp stamp; std::vector<std::pair<std::string, std::string>> entries; };
struct SymbolCache { Stamp stamp; std::map<std::string, json> entries; };
struct FootprintCache { Stamp stamp; json entry; };
std::mutex cache_mutex;
std::map<std::string, TableCache> table_cache;
std::map<std::string, SymbolCache> symbol_cache;
std::map<std::string, FootprintCache> footprint_cache;
struct RawCache { std::string path; Stamp stamp; json tree; } raw_cache;

const std::vector<std::pair<std::string, std::string>>& table_entries(const fs::path& path) {
    const std::string key = path.string();
    const Stamp current = stamp(path);
    auto it = table_cache.find(key);
    if (it != table_cache.end() && it->second.stamp == current) return it->second.entries;
    TableCache entry;
    entry.stamp = current;
    if (current.exists) {
        const auto tree = parse_file(path);
        if (tree && (tagged(*tree, "sym_lib_table") || tagged(*tree, "fp_lib_table")))
            for (const auto& node : *tree) {
                if (!tagged(node, "lib") || child(node, "disabled")) continue;
                const std::string type = child_atom(node, "type");
                if (!type.empty() && lower(type) != "kicad") continue;
                const std::string name = child_atom(node, "name");
                const std::string uri = child_atom(node, "uri");
                if (!name.empty() && !uri.empty()) entry.entries.emplace_back(name, uri);
            }
    }
    return table_cache.insert_or_assign(key, std::move(entry)).first->second.entries;
}
std::vector<Lib> collect_libraries(bool symbols) {
    const Install install = detect();
    const char* filename = symbols ? "sym-lib-table" : "fp-lib-table";
    fs::path global = config_dir() / filename;
    std::string scope = "user";
    if (!is_file(global)) {
        global = install.template_dir.empty() ? fs::path() : fs::path(install.template_dir) / filename;
        scope = "template";
    }
    std::map<std::string, Lib> merged;
    auto add_table = [&](const fs::path& path, const std::string& source) {
        for (const auto& [nickname, raw_uri] : table_entries(path)) {
            std::string uri = expanded(raw_uri, install);
            fs::path p(uri);
            if (p.is_relative() && uri.find("${") == std::string::npos &&
                uri.find("$(") == std::string::npos) p = path.parent_path() / p;
            merged[nickname] = Lib{nickname, p.lexically_normal().string(), source};
        }
    };
    if (!global.empty()) add_table(global, scope);
    if (!project_dir.empty()) add_table(fs::path(project_dir) / filename, "project");
    std::vector<Lib> result;
    for (auto& [name, lib] : merged) result.push_back(std::move(lib));
    return result;
}

int count_tag(const json& node, const char* tag) {
    if (!node.is_array()) return 0;
    int count = tagged(node, tag) ? 1 : 0;
    for (const auto& item : node) if (item.is_array()) count += count_tag(item, tag);
    return count;
}
// Child properties override same-named parent properties; graphic units retain the
// parent's geometry but are renamed for the derived symbol before embedding.
std::optional<json> flatten_symbol(const std::map<std::string, const json*>& nodes,
                                   const std::string& name, std::set<std::string>& visiting) {
    const auto it = nodes.find(name);
    if (it == nodes.end() || !visiting.insert(name).second) return std::nullopt;
    const json& derived = *it->second;
    const std::string parent_name = child_atom(derived, "extends");
    if (parent_name.empty()) { visiting.erase(name); return derived; }
    auto parent = flatten_symbol(nodes, parent_name, visiting);
    visiting.erase(name);
    if (!parent) return std::nullopt;
    json result = json::array({"symbol", name});
    std::set<std::string> properties, singleton, units;
    for (std::size_t i = 2; i < derived.size(); ++i) {
        const auto& n = derived[i];
        const std::string tag = atom(n, 0);
        if (tag == "property") properties.insert(atom(n, 1));
        else if (tag == "symbol") {
            std::string unit = atom(n, 1);
            if (unit.rfind(name + "_", 0) == 0) unit.erase(0, name.size());
            units.insert(unit);
        } else if (!tag.empty() && tag != "extends") singleton.insert(tag);
    }
    for (std::size_t i = 2; i < parent->size(); ++i) {
        json node = (*parent)[i];
        const std::string tag = atom(node, 0);
        if (tag == "property" && properties.count(atom(node, 1))) continue;
        if (tag == "symbol") {
            std::string unit = atom(node, 1);
            if (unit.rfind(parent_name + "_", 0) == 0) unit.erase(0, parent_name.size());
            if (units.count(unit)) continue;
            if (!unit.empty() && unit[0] == '_') node[1] = name + unit;
        } else if (tag != "property" && singleton.count(tag)) continue;
        result.push_back(std::move(node));
    }
    for (std::size_t i = 2; i < derived.size(); ++i)
        if (!tagged(derived[i], "extends")) result.push_back(derived[i]);
    return result;
}
std::map<std::string, const json*> symbol_nodes(const json& tree) {
    std::map<std::string, const json*> result;
    if (tagged(tree, "kicad_symbol_lib"))
        for (const auto& node : tree)
            if (tagged(node, "symbol") && !atom(node, 1).empty()) result[atom(node, 1)] = &node;
    return result;
}
json words(const std::string& text) {
    json result = json::array();
    std::string word;
    for (char c : text) {
        if (std::isspace(static_cast<unsigned char>(c)) || c == ',') {
            if (!word.empty()) { result.push_back(word); word.clear(); }
        } else word += c;
    }
    if (!word.empty()) result.push_back(word);
    return result;
}
json symbol_summary(const json& node, const std::string& name) {
    std::string description = property(node, "Description");
    if (description.empty()) description = property(node, "ki_description");
    std::string keywords = property(node, "ki_keywords");
    if (keywords.empty()) keywords = property(node, "Keywords");
    const std::string prefix = property(node, "Reference");
    return json{{"name", name}, {"description", description}, {"keywords", words(keywords)},
                {"pin_count", count_tag(node, "pin")}, {"footprint", property(node, "Footprint")},
                {"prefix", prefix}, {"power", child(node, "power") != nullptr}};
}
const std::map<std::string, json>& symbols_in_file(const std::string& path) {
    const Stamp current = stamp(path);
    auto it = symbol_cache.find(path);
    if (it != symbol_cache.end() && it->second.stamp == current) return it->second.entries;
    SymbolCache entry;
    entry.stamp = current;
    if (current.exists) {
        const auto tree = parse_symbol_file(path);
        if (tree) {
            const auto nodes = symbol_nodes(*tree);
            for (const auto& [name, node] : nodes) {
                if (child(*node, "extends")) {
                    std::set<std::string> visiting;
                    const auto flat = flatten_symbol(nodes, name, visiting);
                    if (flat) entry.entries[name] = symbol_summary(*flat, name);
                } else entry.entries[name] = symbol_summary(*node, name);
            }
        }
    }
    return symbol_cache.insert_or_assign(path, std::move(entry)).first->second.entries;
}
const json* raw_symbol_tree(const std::string& path) {
    const Stamp current = stamp(path);
    if (!current.exists) return nullptr;
    if (raw_cache.path != path || !(raw_cache.stamp == current)) {
        auto tree = parse_symbol_file(path);
        if (!tree) { raw_cache = {}; return nullptr; }
        raw_cache = RawCache{path, current, std::move(*tree)};
    }
    return &raw_cache.tree;
}
json footprint_info(const fs::path& path) {
    const std::string key = path.string();
    const Stamp current = stamp(path);
    auto it = footprint_cache.find(key);
    if (it != footprint_cache.end() && it->second.stamp == current) return it->second.entry;
    FootprintCache entry;
    entry.stamp = current;
    if (current.exists) {
        const auto tree = parse_file(path);
        if (tree && tagged(*tree, "footprint")) {
            entry.entry = json{{"description", child_atom(*tree, "descr")},
                               {"tags", child_atom(*tree, "tags")},
                               {"pad_count", count_tag(*tree, "pad")}};
        }
    }
    return footprint_cache.insert_or_assign(key, std::move(entry)).first->second.entry;
}
std::pair<std::string, std::string> split_id(const std::string& id) {
    const auto colon = id.find(':');
    if (colon == std::string::npos || colon == 0 || colon + 1 == id.size()) return {};
    return {id.substr(0, colon), id.substr(colon + 1)};
}
int score(const std::string& query, const std::string& id, const std::string& name,
          const std::string& keywords, const std::string& description) {
    if (query.empty()) return 1;
    if (lower(id) == query) return 500;
    if (lower(name) == query) return 450;
    if (lower(id).find(query) != std::string::npos) return 400;
    if (lower(keywords).find(query) != std::string::npos) return 200;
    return lower(description).find(query) != std::string::npos ? 100 : 0;
}
struct Hit { int score; std::string id; json entry; };
json ranked(std::vector<Hit>& hits, int limit) {
    std::sort(hits.begin(), hits.end(), [](const Hit& a, const Hit& b) {
        return a.score != b.score ? a.score > b.score : a.id < b.id;
    });
    if (limit > 0 && hits.size() > static_cast<std::size_t>(limit)) hits.resize(limit);
    json result = json::array();
    for (auto& hit : hits) {
        result.push_back(std::move(hit.entry));
    }
    return result;
}
}  // namespace

Install detect() {
    static std::mutex mutex;
    static std::map<std::vector<std::string>, Install> cache;
    const std::vector<std::string> key = {env("CHUDWARE_KICAD_CLI"), env("CHUDWARE_KICAD_DIR"),
        env("PATH"), env("ProgramFiles"), env("ProgramFiles(x86)"), env("KICAD10_SYMBOL_DIR"),
        env("KICAD10_FOOTPRINT_DIR"), env("KICAD10_3DMODEL_DIR"), env("KICAD10_TEMPLATE_DIR")};
    std::lock_guard<std::mutex> lock(mutex);
    auto it = cache.find(key);
    if (it != cache.end()) return it->second;
    return cache.emplace(key, detect_uncached()).first->second;
}
void set_project_dir(const std::string& dir) {
    project_dir = dir.empty() ? std::string() : absolute_path(dir);
}
std::optional<json> symbol_sexpr(const std::string& libid) {
    const auto [nickname, name] = split_id(libid);
    if (nickname.empty()) return std::nullopt;
    std::lock_guard<std::mutex> lock(cache_mutex);
    for (const auto& lib : collect_libraries(true)) if (lib.nickname == nickname) {
        const json* tree = raw_symbol_tree(lib.uri);
        if (!tree) return std::nullopt;
        const auto nodes = symbol_nodes(*tree);
        std::set<std::string> visiting;
        return flatten_symbol(nodes, name, visiting);
    }
    return std::nullopt;
}
std::optional<json> footprint_sexpr(const std::string& fpid) {
    const auto [nickname, name] = split_id(fpid);
    if (nickname.empty() || name == "." || name == ".." ||
        name.find_first_of("/\\") != std::string::npos) return std::nullopt;
    std::lock_guard<std::mutex> lock(cache_mutex);
    for (const auto& lib : collect_libraries(false)) if (lib.nickname == nickname) {
        auto tree = parse_file(fs::path(lib.uri) / (name + ".kicad_mod"));
        if (tree && tagged(*tree, "footprint")) return tree;
        return std::nullopt;
    }
    return std::nullopt;
}
json search_symbols(const std::string& query, int limit) {
    const std::string needle = lower(trim(query));
    std::lock_guard<std::mutex> lock(cache_mutex);
    std::vector<Hit> hits;
    for (const auto& lib : collect_libraries(true)) {
        for (const auto& [name, summary] : symbols_in_file(lib.uri)) {
            const std::string id = lib.nickname + ":" + name;
            std::string keywords;
            for (const auto& word : summary["keywords"]) if (word.is_string()) keywords += word.get<std::string>() + " ";
            const int ranking = score(needle, id, name, keywords, summary.value("description", std::string()));
            if (!ranking) continue;
            json entry = summary;
            entry.erase("name");
            entry.erase("prefix");
            entry.erase("power");
            entry["libid"] = id;
            hits.push_back({ranking, id, std::move(entry)});
        }
    }
    return ranked(hits, limit);
}
json symbol_catalog() {
    std::lock_guard<std::mutex> lock(cache_mutex);
    json result = json::array();
    for (const auto& lib : collect_libraries(true))
        for (const auto& [name, summary] : symbols_in_file(lib.uri)) {
            json entry = summary;
            entry["libid"] = lib.nickname + ":" + name;
            result.push_back(std::move(entry));
        }
    return result;
}
json search_footprints(const std::string& query, int limit) {
    const std::string needle = lower(trim(query));
    std::lock_guard<std::mutex> lock(cache_mutex);
    std::vector<Hit> hits;
    for (const auto& lib : collect_libraries(false)) {
        std::error_code ec;
        for (fs::directory_iterator it(lib.uri, ec), end; !ec && it != end; it.increment(ec)) {
            const fs::path path = it->path();
            if (path.extension() != ".kicad_mod" || !it->is_regular_file(ec)) continue;
            const json info = footprint_info(path);
            if (!info.is_object()) continue;
            const std::string name = path.stem().string();
            const std::string id = lib.nickname + ":" + name;
            const int ranking = score(needle, id, name, info.value("tags", std::string()),
                                      info.value("description", std::string()));
            if (!ranking) continue;
            hits.push_back({ranking, id, json{{"fpid", id}, {"description", info["description"]},
                                            {"pad_count", info["pad_count"]}}});
        }
    }
    return ranked(hits, limit);
}
json libraries() {
    std::lock_guard<std::mutex> lock(cache_mutex);
    json result{{"symbol", json::array()}, {"footprint", json::array()}};
    for (const auto& lib : collect_libraries(true))
        result["symbol"].push_back({{"nickname", lib.nickname}, {"uri", lib.uri}, {"scope", lib.scope}});
    for (const auto& lib : collect_libraries(false))
        result["footprint"].push_back({{"nickname", lib.nickname}, {"uri", lib.uri}, {"scope", lib.scope}});
    return result;
}
}  // namespace chudware::ecad::kicad_libs
