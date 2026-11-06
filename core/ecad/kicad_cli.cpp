#include "ecad/kicad_cli.h"

#include "ecad/kicad.h"
#include "ecad/kicad_libs.h"
#include "ecad/ops.h"
#include "ecad/session.h"
#include "util/subprocess.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <sddl.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace chudware::ecad {
namespace fs = std::filesystem;
using nlohmann::json;
namespace {

std::string text_path(const fs::path& path) { return path.u8string(); }
fs::path path_of(const std::string& path) { return fs::u8path(path); }

struct TempDir {
    fs::path path;
    TempDir() {
        std::random_device random;
        const auto base = fs::temp_directory_path();
        for (int i = 0; i < 64; ++i) {
            const auto name = "chudware-kicad-" + std::to_string(random()) + "-" +
                              std::to_string(random());
            const auto candidate = base / name;
#ifdef _WIN32
            PSECURITY_DESCRIPTOR descriptor = nullptr;
            if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    L"D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)", SDDL_REVISION_1,
                    &descriptor, nullptr))
                throw OpError("io_error", "Cannot create private KiCad directory security descriptor");
            SECURITY_ATTRIBUTES attributes{sizeof(attributes), descriptor, FALSE};
            const BOOL created = CreateDirectoryW(candidate.c_str(), &attributes);
            const DWORD error = created ? ERROR_SUCCESS : GetLastError();
            LocalFree(descriptor);
            if (created) { path = candidate; return; }
            if (error != ERROR_ALREADY_EXISTS)
                throw OpError("io_error", "Cannot create private KiCad temporary directory",
                              {{"error", error}});
#else
            if (::mkdir(candidate.c_str(), 0700) == 0) { path = candidate; return; }
            if (errno != EEXIST)
                throw OpError("io_error", "Cannot create private KiCad temporary directory",
                              {{"error", errno}});
#endif
        }
        throw OpError("io_error", "Cannot create KiCad temporary directory");
    }
    ~TempDir() {
        if (!path.empty()) { std::error_code ec; fs::remove_all(path, ec); }
    }
};

const kicad_libs::Install& require_install() {
    // detect() is cached; keep a copy here so all fields remain valid after return.
    static const kicad_libs::Install install = kicad_libs::detect();
    if (!install.found || install.cli.empty())
        throw OpError("kicad_unavailable",
                      "KiCad CLI is unavailable. Install KiCad 10 or set CHUDWARE_KICAD_CLI.");
    return install;
}

void write_text(const fs::path& path, const std::string& data) {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out || !out.write(data.data(), static_cast<std::streamsize>(data.size())))
        throw OpError("io_error", "Cannot write KiCad temporary file", {{"path", text_path(path)}});
}

std::string read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw OpError("io_error", "Cannot read KiCad output", {{"path", text_path(path)}});
    std::string data{std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
    if (in.bad()) throw OpError("io_error", "Cannot read KiCad output", {{"path", text_path(path)}});
    return data;
}

void replace_all(std::string& text, const std::string& from, const std::string& to) {
    std::size_t pos = 0;
    while ((pos = text.find(from, pos)) != std::string::npos) {
        text.replace(pos, from.size(), to);
        pos += to.size();
    }
}

std::string sexpr_escaped_path(const fs::path& path) {
    std::string out;
    for (const char c : text_path(path)) {
        if (c == '\\' || c == '"') out.push_back('\\');
        out.push_back(c);
    }
    return out;
}

// Keep KiCad-writable project documents private. Other project assets are exposed
// read-only to these commands via links, preserving relative sheet/model/library paths.
// Windows installations without symlink privileges use private copies instead.
void stage_project(const fs::path& source, const fs::path& target) {
    std::error_code ec;
    for (fs::directory_iterator it(source, ec), end; !ec && it != end; it.increment(ec)) {
        const fs::path from = it->path();
        const fs::path to = target / from.filename();
        const std::string name = text_path(from.filename());
        const std::string ext = text_path(from.extension());
        const bool table = name == "sym-lib-table" || name == "fp-lib-table";
        const bool private_file = table || ext == ".kicad_pro" || ext == ".kicad_sch" ||
                                  ext == ".kicad_pcb" || ext == ".kicad_dru";
        if (private_file && it->is_regular_file(ec)) {
            if (table) {
                auto data = read_file(from);
                // Some CLI commands do not support -D. Environment KIPRJMOD is ignored
                // by KiCad; expanding it in the copied tables works for all commands.
                replace_all(data, "${KIPRJMOD}", sexpr_escaped_path(source));
                write_text(to, data);
            } else {
                fs::copy_file(from, to, fs::copy_options::overwrite_existing, ec);
            }
        } else if (it->is_directory(ec)) {
            fs::create_directory_symlink(from, to, ec);
            if (ec) {
                ec.clear();
                fs::copy(from, to, fs::copy_options::recursive, ec);
            }
        } else if (it->is_regular_file(ec)) {
            fs::create_symlink(from, to, ec);
            if (ec) {
                ec.clear();
                fs::copy_file(from, to, fs::copy_options::overwrite_existing, ec);
            }
        }
        if (ec) break;
    }
    if (ec) throw OpError("io_error", "Cannot stage KiCad project: " + ec.message(),
                          {{"path", text_path(source)}});
}

struct Input {
    TempDir scratch;
    fs::path file;
    fs::path original_dir;
    bool pcb = false;
    bool staged = false;

    explicit Input(const std::string& doc_id) {
        json info = session_file_info(doc_id, false);
        fs::path original;
        auto use_clean_bound_file = [&]() {
            const auto kind = info.value("kind", std::string());
            pcb = kind == "pcb";
            if (!pcb && kind != "sch")
                throw OpError("invalid_op", "KiCad operation requires an ECAD document");
            const auto bound = info.value("path", std::string());
            original.clear();
            original_dir.clear();
            if (!bound.empty()) {
                std::error_code ec;
                original = fs::absolute(path_of(bound), ec);
                if (ec) throw OpError("io_error", "Cannot resolve bound KiCad path");
                original_dir = original.parent_path();
            }
            if (!original.empty() && !info.value("dirty", false) && fs::is_regular_file(original)) {
                file = original;
                return true;
            }
            return false;
        };
        if (use_clean_bound_file()) return;
        // Take the document and its binding under one lock; the session may have
        // changed since the inexpensive metadata-only snapshot.
        info = session_file_info(doc_id);
        if (use_clean_bound_file()) return;
        staged = true;
        const fs::path project = scratch.path / "project";
        fs::create_directory(project);
        if (!original_dir.empty()) stage_project(original_dir, project);
        const fs::path name = original.empty()
            ? fs::path(pcb ? "board.kicad_pcb" : "schematic.kicad_sch") : original.filename();
        file = project / name;
        kicad_libs::set_project_dir(text_path(original_dir));
        json doc = std::move(info["doc"]);
        kicad::assign_uuids(doc);
        if (pcb) {
            fs::path settings_path = file;
            settings_path.replace_extension(".kicad_pro");
            json settings{{"meta", {{"version", 1}, {"filename", text_path(settings_path.filename())}}}};
            if (fs::is_regular_file(settings_path)) {
                try {
                    settings = json::parse(read_file(settings_path));
                } catch (const json::exception& error) {
                    throw OpError("kicad_error", std::string("Invalid KiCad project settings: ") +
                                                   error.what());
                }
                if (!settings.is_object())
                    throw OpError("kicad_error", "KiCad project settings must be a JSON object");
            }
            settings = kicad_project_settings(settings, doc);
            write_text(settings_path, settings.dump(2) + "\n");
        }
        write_text(file, pcb ? kicad::write_kicad_pcb(doc) : kicad::write_kicad_sch(doc));
    }

    fs::path sibling_schematic() const {
        fs::path sibling = file;
        sibling.replace_extension(".kicad_sch");
        return fs::is_regular_file(sibling) ? sibling : fs::path{};
    }

    void define_project(std::vector<std::string>& args) const {
        if (staged && !original_dir.empty()) {
            args.push_back("-D");
            args.push_back("KIPRJMOD=" + text_path(original_dir));
        }
    }
};

// KiCad preserves model-library paths in its generated deck. Resolve those paths
// against the original project, not a disposable staged project or the eventual
// ngspice working directory.
void rebase_spice_includes(const fs::path& deck_path, const Input& input) {
    if (input.original_dir.empty()) return;
    std::string deck = read_file(deck_path);
    bool changed = false;
    std::size_t line = 0;
    while (line < deck.size()) {
        const auto newline = deck.find('\n', line);
        const auto end = newline == std::string::npos ? deck.size() : newline;
        std::size_t pos = line;
        while (pos < end && (deck[pos] == ' ' || deck[pos] == '\t')) ++pos;
        static const std::string directive = ".include";
        if (pos + directive.size() < end &&
            std::equal(directive.begin(), directive.end(), deck.begin() + pos,
                       [](char a, char b) {
                           return a == static_cast<char>(std::tolower(static_cast<unsigned char>(b)));
                       }) &&
            (deck[pos + directive.size()] == ' ' || deck[pos + directive.size()] == '\t')) {
            pos += directive.size();
            while (pos < end && (deck[pos] == ' ' || deck[pos] == '\t')) ++pos;
            const std::size_t token_begin = pos;
            char quote = 0;
            if (pos < end && (deck[pos] == '\"' || deck[pos] == '\'')) quote = deck[pos++];
            const std::size_t path_begin = pos;
            if (quote) {
                while (pos < end && deck[pos] != quote) ++pos;
                if (pos == end) throw OpError("kicad_error", "Malformed SPICE model include");
            } else {
                while (pos < end && deck[pos] != ' ' && deck[pos] != '\t' && deck[pos] != '\r') ++pos;
            }
            std::string name = deck.substr(path_begin, pos - path_begin);
            if (name.empty()) throw OpError("kicad_error", "Empty SPICE model include path");
            const std::size_t token_end = quote ? pos + 1 : pos;
            const bool project_var = name.find("${KIPRJMOD}") != std::string::npos;
            replace_all(name, "${KIPRJMOD}", input.original_dir.generic_u8string());
            fs::path model = path_of(name);
            bool rebase = project_var;
            if (model.is_relative() && name.find("${") == std::string::npos) {
                model = input.original_dir / model;
                rebase = true;
            } else if (input.staged && model.is_absolute()) {
                const auto relative = model.lexically_normal().lexically_relative(
                    (input.scratch.path / "project").lexically_normal());
                if (!relative.empty() && !relative.is_absolute() && *relative.begin() != "..") {
                    model = input.original_dir / relative;
                    rebase = true;
                }
            }
            if (rebase) {
                const std::string absolute = model.lexically_normal().generic_u8string();
                if (absolute.find_first_of("\"\r\n") != std::string::npos)
                    throw OpError("kicad_error", "SPICE model path cannot be quoted safely",
                                  {{"path", absolute}});
                deck.replace(token_begin, token_end - token_begin, "\"" + absolute + "\"");
                changed = true;
            }
        }
        const auto next = deck.find('\n', line);
        if (next == std::string::npos) break;
        line = next + 1;
    }
    if (changed) write_text(deck_path, deck);
}

util::ProcessResult invoke(const kicad_libs::Install& install,
                           const std::vector<std::string>& arguments) {
    std::vector<std::string> argv{install.cli};
    argv.insert(argv.end(), arguments.begin(), arguments.end());
    util::ProcessOptions opts;
    opts.timeout = std::chrono::minutes(5);
    opts.max_output_bytes = 4 * 1024 * 1024;
    // Older KiCad projects commonly reference versioned model variables. When not
    // configured by the user, the current installed library is the correct fallback.
    if (!install.model_dir.empty()) {
        for (int version = 6; version <= 10; ++version) {
            const auto key = "KICAD" + std::to_string(version) + "_3DMODEL_DIR";
            if (!std::getenv(key.c_str())) opts.env[key] = install.model_dir;
        }
    }
    auto result = util::run_process(argv, opts);
    if (!result.error.empty() || result.timed_out || result.exit_code != 0) {
        const std::string reason = result.timed_out ? "deadline exceeded" :
                                   !result.error.empty() ? result.error : result.output;
        throw OpError("kicad_error", "KiCad CLI failed: " + reason,
                      {{"exit_code", result.exit_code}, {"timed_out", result.timed_out}});
    }
    return result;
}

json parse_violation(const json& source) {
    if (!source.is_object() || !source.contains("severity") || !source["severity"].is_string() ||
        !source.contains("type") || !source["type"].is_string() ||
        !source.contains("description") || !source["description"].is_string() ||
        !source.contains("items") || !source["items"].is_array())
        throw OpError("kicad_error", "Malformed KiCad check violation");
    json items = json::array();
    for (const auto& item : source["items"]) {
        if (!item.is_object() || !item.contains("uuid") || !item["uuid"].is_string() ||
            !item.contains("description") || !item["description"].is_string() ||
            !item.contains("pos") || !item["pos"].is_object() ||
            !item["pos"].contains("x") || !item["pos"]["x"].is_number() ||
            !item["pos"].contains("y") || !item["pos"]["y"].is_number())
            throw OpError("kicad_error", "Malformed KiCad check item");
        items.push_back({{"description", item["description"]}, {"uuid", item["uuid"]},
                         {"xy", json::array({item["pos"]["x"],
                                              -item["pos"]["y"].get<double>()})}});
    }
    return json{{"severity", source.value("severity", std::string())},
                {"type", source.value("type", std::string())},
                {"description", source.value("description", std::string())},
                {"items", std::move(items)}};
}

void append_violations(const json& source, json& target, int& errors, int& warnings) {
    if (!source.is_array()) throw OpError("kicad_error", "Malformed KiCad violation list");
    for (const auto& item : source) {
        auto violation = parse_violation(item);
        const auto severity = violation.value("severity", std::string());
        if (severity == "error") ++errors;
        else if (severity == "warning") ++warnings;
        else throw OpError("kicad_error", "Unknown KiCad check severity '" + severity + "'");
        target.push_back(std::move(violation));
    }
}

bool is_utf8_text(const std::string& data) {
    for (std::size_t i = 0; i < data.size();) {
        const auto c = static_cast<unsigned char>(data[i]);
        if (c == 0) return false;
        if (c < 0x80) { ++i; continue; }
        unsigned count = 0;
        unsigned value = 0;
        if (c >= 0xc2 && c <= 0xdf) { count = 2; value = c & 0x1f; }
        else if (c >= 0xe0 && c <= 0xef) { count = 3; value = c & 0x0f; }
        else if (c >= 0xf0 && c <= 0xf4) { count = 4; value = c & 0x07; }
        else return false;
        if (i + count > data.size()) return false;
        for (unsigned n = 1; n < count; ++n) {
            const auto next = static_cast<unsigned char>(data[i + n]);
            if ((next & 0xc0) != 0x80) return false;
            value = (value << 6) | (next & 0x3f);
        }
        if ((count == 3 && value < 0x800) || (count == 4 && value < 0x10000) ||
            (value >= 0xd800 && value <= 0xdfff) || value > 0x10ffff) return false;
        i += count;
    }
    return true;
}

std::string base64(const std::string& input) {
    static constexpr char chars[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve((input.size() + 2) / 3 * 4);
    for (std::size_t i = 0; i < input.size(); i += 3) {
        const unsigned a = static_cast<unsigned char>(input[i]);
        const unsigned b = i + 1 < input.size() ? static_cast<unsigned char>(input[i + 1]) : 0;
        const unsigned c = i + 2 < input.size() ? static_cast<unsigned char>(input[i + 2]) : 0;
        out.push_back(chars[a >> 2]);
        out.push_back(chars[((a & 3) << 4) | (b >> 4)]);
        out.push_back(i + 1 < input.size() ? chars[((b & 15) << 2) | (c >> 6)] : '=');
        out.push_back(i + 2 < input.size() ? chars[c & 63] : '=');
    }
    return out;
}

// Never follow the final output component: an existing symlink must not redirect
// an export to another file. KiCad itself only writes to the private workspace.
void copy_output(const fs::path& source, const fs::path& target) {
    std::ifstream in(source, std::ios::binary);
    if (!in) throw OpError("io_error", "Cannot read KiCad output", {{"path", text_path(source)}});
    auto fail = [&]() { throw OpError("io_error", "Cannot write export", {{"path", text_path(target)}}); };
#ifdef _WIN32
    HANDLE file = CreateFileW(target.c_str(), GENERIC_WRITE, 0, nullptr, OPEN_ALWAYS,
                              FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (file == INVALID_HANDLE_VALUE) fail();
    BY_HANDLE_FILE_INFORMATION info{};
    if (!GetFileInformationByHandle(file, &info) ||
        (info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY))) {
        CloseHandle(file); fail();
    }
    LARGE_INTEGER zero{};
    if (!SetFilePointerEx(file, zero, nullptr, FILE_BEGIN) || !SetEndOfFile(file)) {
        CloseHandle(file); fail();
    }
    char buf[65536];
    while (in.read(buf, sizeof(buf)) || in.gcount()) {
        DWORD written = 0;
        if (!WriteFile(file, buf, static_cast<DWORD>(in.gcount()), &written, nullptr) ||
            written != static_cast<DWORD>(in.gcount())) { CloseHandle(file); fail(); }
    }
    CloseHandle(file);
#else
    std::error_code ec;
    const fs::path parent = fs::canonical(target.has_parent_path() ? target.parent_path() : fs::path("."), ec);
    if (ec) fail();
    const int dir = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (dir < 0) fail();
    const int file = ::openat(dir, target.filename().c_str(),
                              O_WRONLY | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0666);
    ::close(dir);
    if (file < 0) fail();
    struct stat st{};
    if (::fstat(file, &st) || !S_ISREG(st.st_mode) || ::ftruncate(file, 0)) { ::close(file); fail(); }
    char buf[65536];
    while (in.read(buf, sizeof(buf)) || in.gcount()) {
        std::size_t offset = 0, size = static_cast<std::size_t>(in.gcount());
        while (offset < size) {
            const auto n = ::write(file, buf + offset, size - offset);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { ::close(file); fail(); }
            offset += static_cast<std::size_t>(n);
        }
    }
    ::close(file);
#endif
    if (in.bad()) fail();
}

std::string extension_for(const std::string& kind) {
    if (kind == "step") return ".step";
    if (kind == "netlist") return ".net";
    if (kind == "spice") return ".cir";
    if (kind == "cpl" || kind == "bom") return ".csv";
    if (kind == "render") return ".png";
    if (kind == "odb") return ".zip";
    if (kind == "ipc2581") return ".xml";
    return "." + kind;
}

std::string selected_layers(const json& opts, const std::string& fallback) {
    if (!opts.contains("layers")) return fallback;
    if (opts["layers"].is_string()) return opts["layers"].get<std::string>();
    if (opts["layers"].is_array()) {
        std::string out;
        for (const auto& layer : opts["layers"]) {
            if (!layer.is_string()) throw OpError("invalid_op", "layers must contain strings");
            if (!out.empty()) out += ',';
            out += layer.get<std::string>();
        }
        return out;
    }
    throw OpError("invalid_op", "layers must be a string or array of strings");
}

void collect_warnings(const util::ProcessResult& result, json& warnings) {
    std::istringstream lines(result.output);
    std::string line;
    while (std::getline(lines, line)) {
        std::string lower = line;
        std::transform(lower.begin(), lower.end(), lower.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        if (lower.find("warning") != std::string::npos ||
            lower.find("could not") != std::string::npos ||
            lower.find("not found") != std::string::npos)
            warnings.push_back(line);
    }
    if (result.output_truncated) warnings.push_back("KiCad diagnostic output was truncated");
}

}  // namespace

json ecad_kicad_status() {
    const auto install = kicad_libs::detect();
    json libraries{{"symbol", 0}, {"footprint", 0}};
    if (install.found) {
        const auto list = kicad_libs::libraries();
        if (list.contains("symbol") && list["symbol"].is_array()) libraries["symbol"] = list["symbol"].size();
        if (list.contains("footprint") && list["footprint"].is_array()) libraries["footprint"] = list["footprint"].size();
    }
    return json{{"found", install.found}, {"version", install.version}, {"cli", install.cli},
                {"kicad", install.kicad}, {"eeschema", install.eeschema}, {"pcbnew", install.pcbnew},
                {"symbol_dir", install.symbol_dir}, {"footprint_dir", install.footprint_dir},
                {"model_dir", install.model_dir}, {"libraries", libraries}};
}

json ecad_kicad_check(const std::string& doc_id) {
    const auto& install = require_install();
    Input input(doc_id);
    const fs::path report = input.scratch.path / "check.json";
    std::vector<std::string> args{input.pcb ? "pcb" : "sch", input.pcb ? "drc" : "erc",
                                  "--format", "json", "--units", "mm", "-o", text_path(report)};
    input.define_project(args);
    if (input.pcb) {
        args.push_back("--refill-zones");
        if (!input.sibling_schematic().empty()) args.push_back("--schematic-parity");
    }
    args.push_back(text_path(input.file));
    invoke(install, args);
    json raw;
    try { raw = json::parse(read_file(report)); }
    catch (const json::exception& e) { throw OpError("kicad_error", std::string("Invalid KiCad check report: ") + e.what()); }
    if (!raw.is_object() || !raw.contains("kicad_version") || !raw["kicad_version"].is_string() ||
        (raw.contains("coordinate_units") && raw["coordinate_units"] != "mm") ||
        (input.pcb && (!raw.contains("violations") || !raw.contains("unconnected_items") ||
                       !raw.contains("schematic_parity"))) ||
        (!input.pcb && (!raw.contains("sheets") || !raw["sheets"].is_array())))
        throw OpError("kicad_error", "KiCad check report has an unexpected schema or units");
    json violations = json::array(), unconnected = json::array(), parity = json::array();
    int errors = 0, warnings = 0;
    if (input.pcb) {
        append_violations(raw["violations"], violations, errors, warnings);
        append_violations(raw["unconnected_items"], unconnected, errors, warnings);
        append_violations(raw["schematic_parity"], parity, errors, warnings);
    } else {
        for (const auto& sheet : raw["sheets"]) {
            if (!sheet.is_object() || !sheet.contains("violations"))
                throw OpError("kicad_error", "Malformed KiCad sheet report");
            append_violations(sheet["violations"], violations, errors, warnings);
        }
    }
    return json{{"ok", errors == 0}, {"kind", input.pcb ? "drc" : "erc"},
                {"kicad_version", raw.value("kicad_version", install.version)},
                {"violations", violations}, {"unconnected", unconnected}, {"parity", parity},
                {"counts", {{"error", errors}, {"warning", warnings}}}};
}

json ecad_export(const json& params) {
    const auto& install = require_install();
    const std::string kind = params.value("kind", std::string());
    const json opts = params.contains("opts") && params["opts"].is_object() ? params["opts"] : json::object();
    const std::string output_path = params.value("path", std::string());
    Input input(params.value("doc_id", std::string()));
    static const std::set<std::string> pcb_only{"gerber", "drill", "cpl", "step", "odb", "ipc2581", "render"};
    static const std::set<std::string> sch_only{"netlist", "spice"};
    static const std::set<std::string> shared{"bom", "svg", "pdf"};
    if (!pcb_only.count(kind) && !sch_only.count(kind) && !shared.count(kind))
        throw OpError("invalid_op", "Unknown KiCad export kind '" + kind + "'");
    if ((!input.pcb && pcb_only.count(kind)) || (input.pcb && sch_only.count(kind)))
        throw OpError("invalid_op", kind + " export requires a " + (input.pcb ? "schematic" : "PCB"));

    const bool multi = kind == "gerber" || kind == "drill" || kind == "svg";
    const fs::path outdir = input.scratch.path / "output";
    fs::create_directories(outdir);
    const std::string stem = text_path(input.file.stem());
    const fs::path outfile = outdir / path_of(stem + extension_for(kind));
    std::vector<std::string> args;
    bool define = true;
    fs::path source = input.file;
    bool spice_output = false;
    json warnings = json::array();
    if (kind == "gerber") {
        args = {"pcb", "export", "gerbers", "--layers", selected_layers(opts, "*.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"), "-o", text_path(outdir) + "/"};
    } else if (kind == "drill") {
        args = {"pcb", "export", "drill", "--excellon-separate-th", "--generate-map", "-o", text_path(outdir) + "/"};
        define = false;
    } else if (kind == "cpl" || (kind == "bom" && input.pcb && input.sibling_schematic().empty())) {
        args = {"pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "-o", text_path(outfile)};
        if (opts.value("exclude_dnp", true)) args.push_back("--exclude-dnp");
        if (kind == "cpl" && opts.value("exclude_tht", false)) args.push_back("--exclude-fp-th");
        if (kind == "bom") warnings.push_back("No sibling schematic: BOM is a KiCad footprint position list.");
        define = false;
    } else if (kind == "bom") {
        if (input.pcb) source = input.sibling_schematic();
        args = {"sch", "export", "bom", "-o", text_path(outfile)};
        if (opts.value("exclude_dnp", true)) args.push_back("--exclude-dnp");
        define = false;
    } else if (kind == "netlist" || kind == "spice") {
        const std::string format = kind == "spice" ? "spice" : opts.value("format", std::string("kicadsexpr"));
        static const std::set<std::string> formats{"kicadsexpr", "kicadxml", "cadstar", "orcadpcb2", "spice", "spicemodel", "pads", "allegro"};
        if (!formats.count(format)) throw OpError("invalid_op", "Unknown KiCad netlist format '" + format + "'");
        args = {"sch", "export", "netlist", "--format", format, "-o", text_path(outfile)};
        spice_output = format == "spice" || format == "spicemodel";
        define = false;
    } else if (kind == "step") {
        args = {"pcb", "export", "step", "--subst-models", "--force", "-o", text_path(outfile)};
    } else if (kind == "odb" || kind == "ipc2581") {
        args = {"pcb", "export", kind, "-o", text_path(outfile)};
    } else if (kind == "render") {
        args = {"pcb", "render", "-o", text_path(outfile)};
    } else if (kind == "svg" || kind == "pdf") {
        args = {input.pcb ? "pcb" : "sch", "export", kind};
        if (input.pcb) {
            args.insert(args.end(), {kind == "svg" ? "--mode-multi" : "--mode-single", "--layers",
                                      selected_layers(opts, "F.Cu,B.Cu,F.SilkS,B.SilkS,Edge.Cuts")});
        } else {
            if (opts.contains("pages") && opts["pages"].is_string() &&
                !opts["pages"].get<std::string>().empty()) {
                args.push_back("--pages");
                args.push_back(opts["pages"].get<std::string>());
            }
            if (opts.contains("frame") && opts["frame"].is_boolean() &&
                !opts["frame"].get<bool>()) args.push_back("--exclude-drawing-sheet");
        }
        args.push_back("-o");
        args.push_back(multi ? text_path(outdir) + "/" : text_path(outfile));
    }
    if (define) input.define_project(args);
    args.push_back(text_path(source));
    collect_warnings(invoke(install, args), warnings);
    if (spice_output) rebase_spice_includes(outfile, input);

    std::vector<fs::path> artifacts;
    std::error_code ec;
    for (fs::directory_iterator it(outdir, ec), end; !ec && it != end; it.increment(ec)) {
        if (it->is_regular_file(ec) && !it->is_symlink(ec)) artifacts.push_back(it->path());
    }
    if (ec) throw OpError("io_error", "Cannot enumerate KiCad output: " + ec.message());
    std::sort(artifacts.begin(), artifacts.end());
    if (artifacts.empty()) throw OpError("kicad_error", "KiCad produced no export files");

    json files = json::array(), paths = json::array();
    fs::path destination;
    bool as_dir = multi;
    if (!output_path.empty()) {
        destination = path_of(output_path);
        const bool exists_as_dir = fs::is_directory(destination, ec);
        if (ec && ec != std::errc::no_such_file_or_directory && ec != std::errc::not_a_directory)
            throw OpError("io_error", "Cannot inspect export destination: " + ec.message());
        ec.clear();
        as_dir = multi || exists_as_dir || !destination.has_extension() ||
                 output_path.back() == '/' || output_path.back() == '\\';
        if (as_dir) fs::create_directories(destination, ec);
        else if (destination.has_parent_path()) fs::create_directories(destination.parent_path(), ec);
        if (ec) throw OpError("io_error", "Cannot create export destination: " + ec.message());
        if (!as_dir && artifacts.size() != 1)
            throw OpError("invalid_op", "Multiple export files require a directory");
    }
    for (const auto& artifact : artifacts) {
        const auto size = fs::file_size(artifact, ec);
        if (ec) throw OpError("io_error", "Cannot stat KiCad output: " + ec.message());
        json item{{"name", text_path(artifact.filename())}, {"bytes", size}};
        if (output_path.empty()) {
            if (size > 128ULL * 1024 * 1024)
                throw OpError("io_error", "KiCad output is too large to return inline; supply an output path");
            const auto contents = read_file(artifact);
            if (is_utf8_text(contents)) item["content"] = contents;
            else { item["encoding"] = "base64"; item["content"] = base64(contents); }
        } else {
            const fs::path target = as_dir ? destination / artifact.filename() : destination;
            copy_output(artifact, target);
            item["name"] = text_path(target.filename());
            paths.push_back(text_path(target));
        }
        files.push_back(std::move(item));
    }
    json out{{"kind", kind}, {"paths", paths}, {"files", files}};
    if (!warnings.empty()) out["warnings"] = warnings;
    return out;
}

json ecad_kicad_step(const std::string& doc_id, const json& opts_in, const std::string& path) {
    const auto info = session_file_info(doc_id, false);
    if (info.value("kind", std::string()) != "pcb")
        throw OpError("invalid_op", "ecad_to_3d requires a PCB document");
    const std::string bound = info.value("path", std::string());
    if (bound.empty() || !fs::is_regular_file(path_of(bound)))
        throw OpError("needs_path", "Save the board as a KiCad file first");
    const json opts = opts_in.is_object() ? opts_in : json::object();
    std::string destination = path.empty() ? opts.value("path", std::string()) : path;
    if (destination.empty()) {
        fs::path sibling = path_of(bound);
        sibling.replace_extension(".step");
        destination = text_path(sibling);
    }
    const auto exported = ecad_export(json{{"doc_id", doc_id}, {"kind", "step"},
                                           {"path", destination}, {"opts", opts}});
    const auto output = fs::absolute(path_of(exported["paths"][0].get<std::string>()));
    json result{{"step_path", text_path(output)}};
    if (exported.contains("warnings")) result["warnings"] = exported["warnings"];
    return result;
}

}  // namespace chudware::ecad
