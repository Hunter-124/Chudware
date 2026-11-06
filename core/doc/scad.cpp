#ifdef _WIN32
// ACL headers must precede OCCT's Handle and Windows feature macros.
#include "firmware/private_storage.h"
#endif

#include "doc/scad.h"

#include "doc/formats.h"
#include "doc/scad_projection.h"
#include "doc/native_policy.h"
#include "util/expr.h"
#include "util/scad_process.h"
#include "util/sha256.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <locale>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <utility>
#include <vector>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <winternl.h>
#include <io.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <cerrno>
#include <unistd.h>
#endif

namespace chudware::doc::scad {
namespace {
namespace fs = std::filesystem;
constexpr std::size_t kSourceLimit = 4 * 1024 * 1024;
constexpr std::size_t kProjectLimit = 64 * 1024 * 1024;
constexpr std::size_t kFileLimit = 32 * 1024 * 1024;
constexpr std::size_t kFileCount = 128;
constexpr std::size_t kTriangles = 100000;
const std::string kMarker = "// CHUDWARE_SCAD_PROJECT_V1 ";

[[noreturn]] void fail(const std::string& message) { throw std::runtime_error("SCAD: " + message); }
std::string b64encode(const std::string& s) {
    static constexpr char table[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve((s.size() + 2) / 3 * 4);
    for (std::size_t i = 0; i < s.size(); i += 3) {
        unsigned v = static_cast<unsigned char>(s[i]) << 16;
        if (i + 1 < s.size()) v |= static_cast<unsigned char>(s[i + 1]) << 8;
        if (i + 2 < s.size()) v |= static_cast<unsigned char>(s[i + 2]);
        out += table[(v >> 18) & 63]; out += table[(v >> 12) & 63];
        out += i + 1 < s.size() ? table[(v >> 6) & 63] : '=';
        out += i + 2 < s.size() ? table[v & 63] : '=';
    }
    return out;
}
std::string b64decode(const std::string& s) {
    if (s.size() % 4 || s.size() > (kProjectLimit + kSourceLimit) * 2) fail("invalid or oversized base64 data");
    auto digit = [](char c) -> int {
        if (c >= 'A' && c <= 'Z') return c - 'A';
        if (c >= 'a' && c <= 'z') return c - 'a' + 26;
        if (c >= '0' && c <= '9') return c - '0' + 52;
        if (c == '+') return 62; if (c == '/') return 63; return -1;
    };
    std::string out; out.reserve(s.size() / 4 * 3);
    for (std::size_t i = 0; i < s.size(); i += 4) {
        int a = digit(s[i]), b = digit(s[i+1]);
        int c = s[i+2] == '=' ? -2 : digit(s[i+2]);
        int d = s[i+3] == '=' ? -2 : digit(s[i+3]);
        if (a < 0 || b < 0 || c == -1 || d == -1 || (c == -2 && d != -2) ||
            (i + 4 != s.size() && (c == -2 || d == -2)) ||
            (c == -2 && (b & 15)) || (d == -2 && c >= 0 && (c & 3))) fail("invalid base64 data");
        unsigned v = (a << 18) | (b << 12) | ((c < 0 ? 0 : c) << 6) | (d < 0 ? 0 : d);
        out += static_cast<char>(v >> 16);
        if (c >= 0) out += static_cast<char>(v >> 8);
        if (d >= 0) out += static_cast<char>(v);
    }
    return out;
}
bool identifier(const std::string& s) {
    if (s.empty() || !(std::isalpha(static_cast<unsigned char>(s[0])) || s[0] == '_' || s[0] == '$')) return false;
    return std::all_of(s.begin() + 1, s.end(), [](unsigned char c) { return std::isalnum(c) || c == '_'; });
}
std::string portable_path(const std::string& s) {
    if (s.empty() || s.size() > 1024 || s.find('\0') != s.npos || s.find('\\') != s.npos || s.find(':') != s.npos)
        fail("dependency path must be a portable relative path");
    fs::path p = fs::u8path(s);
    if (p.is_absolute() || p.has_root_name()) fail("absolute dependency path refused");
    for (const auto& part : p) if (part == ".." || part == "." || part.empty()) fail("dependency path traversal refused");
    if (native_policy::generic_path_utf8(p) != s || s.back() == '/') fail("dependency path is not normalized");
    return s;
}
std::string read_bounded(const fs::path& path, std::size_t limit) {
    std::error_code ec;
    auto size = fs::file_size(path, ec);
    if (ec || size > limit) fail("cannot read file or file exceeds size limit: " + native_policy::path_utf8(path));
    std::ifstream in(path, std::ios::binary);
    if (!in) fail("cannot open file: " + native_policy::path_utf8(path));
    std::string data(static_cast<std::size_t>(size), '\0');
    if (size && !in.read(&data[0], static_cast<std::streamsize>(size))) fail("file changed while reading");
    if (in.peek() != std::char_traits<char>::eof()) fail("file changed while reading");
    return data;
}
#ifdef _WIN32
// Open each component relative to an already-open directory, without following
// reparse points, so a junction swap cannot redirect dependencies out of root.
struct WinHandle {
    HANDLE value = INVALID_HANDLE_VALUE;
    explicit WinHandle(HANDLE h = INVALID_HANDLE_VALUE) : value(h) {}
    ~WinHandle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    WinHandle(const WinHandle&) = delete;
    WinHandle& operator=(const WinHandle&) = delete;
    WinHandle(WinHandle&& other) noexcept : value(other.value) { other.value = INVALID_HANDLE_VALUE; }
    WinHandle& operator=(WinHandle&& other) noexcept {
        if (this != &other) {
            if (value != INVALID_HANDLE_VALUE) CloseHandle(value);
            value = other.value; other.value = INVALID_HANDLE_VALUE;
        }
        return *this;
    }
};
bool win_safe_handle(HANDLE handle, bool directory) {
    BY_HANDLE_FILE_INFORMATION info{};
    return handle != INVALID_HANDLE_VALUE && GetFileType(handle) == FILE_TYPE_DISK &&
        GetFileInformationByHandle(handle, &info) &&
        !(info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) &&
        !!(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) == directory;
}
WinHandle win_root(const fs::path& root, bool write) {
    DWORD access = FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY | SYNCHRONIZE;
    if (write) access |= FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY;
    WinHandle h(CreateFileW(root.c_str(), access, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                            nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!win_safe_handle(h.value, true)) fail("cannot open project directory safely");
    return h;
}
WinHandle win_relative(HANDLE parent, const fs::path& component, ACCESS_MASK access, bool directory, bool create,
                       PSECURITY_DESCRIPTOR security = nullptr, bool share_delete = true) {
    using NtCreateFileFn = NTSTATUS (NTAPI *)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES, PIO_STATUS_BLOCK,
        PLARGE_INTEGER, ULONG, ULONG, ULONG, ULONG, PVOID, ULONG);
    static auto create_file = reinterpret_cast<NtCreateFileFn>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtCreateFile"));
    if (!create_file) fail("secure Windows project file API is unavailable");
    auto name = component.native();
    if (name.empty() || name.size() > 32767 || name.find(L'\\') != name.npos || name.find(L'/') != name.npos)
        fail("invalid project file component");
    UNICODE_STRING unicode{};
    unicode.Buffer = const_cast<wchar_t*>(name.data());
    unicode.Length = static_cast<USHORT>(name.size() * sizeof(wchar_t));
    unicode.MaximumLength = unicode.Length;
    OBJECT_ATTRIBUTES attrs{};
    attrs.Length = sizeof(attrs); attrs.RootDirectory = parent; attrs.ObjectName = &unicode;
    attrs.Attributes = 0x40; // OBJ_CASE_INSENSITIVE
    attrs.SecurityDescriptor = security;
    IO_STATUS_BLOCK status{};
    HANDLE handle = INVALID_HANDLE_VALUE;
    constexpr ULONG open_existing = 1, create_new = 2;
    constexpr ULONG directory_file = 0x00000001, non_directory_file = 0x00000040;
    constexpr ULONG synchronous = 0x00000020, open_reparse_point = 0x00200000;
    NTSTATUS result = create_file(&handle, access | SYNCHRONIZE | FILE_READ_ATTRIBUTES, &attrs, &status, nullptr,
        directory ? FILE_ATTRIBUTE_DIRECTORY : FILE_ATTRIBUTE_NORMAL,
        FILE_SHARE_READ | FILE_SHARE_WRITE | (share_delete ? FILE_SHARE_DELETE : 0), create ? create_new : open_existing,
        (directory ? directory_file : non_directory_file) | synchronous | open_reparse_point, nullptr, 0);
    if (result < 0) return WinHandle{};
    WinHandle opened(handle);
    if (!win_safe_handle(opened.value, directory)) {
        if (create) {
            FILE_DISPOSITION_INFO disposition{}; disposition.DeleteFile = TRUE;
            SetFileInformationByHandle(opened.value, FileDispositionInfo, &disposition, sizeof(disposition));
        }
        return WinHandle{};
    }
    return opened;
}
std::string win_read(HANDLE file, std::size_t limit) {
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file, &size) || size.QuadPart < 0 || static_cast<unsigned long long>(size.QuadPart) > limit)
        fail("missing or oversized dependency");
    std::string data(static_cast<std::size_t>(size.QuadPart), '\0');
    std::size_t offset = 0;
    while (offset < data.size()) {
        DWORD count = 0, wanted = static_cast<DWORD>(std::min<std::size_t>(data.size() - offset, 1024 * 1024));
        if (!ReadFile(file, &data[offset], wanted, &count, nullptr) || !count) fail("dependency changed while reading");
        offset += count;
    }
    char extra; DWORD count = 0;
    if (!ReadFile(file, &extra, 1, &count, nullptr) || count) fail("dependency changed while reading");
    return data;
}
void win_write(HANDLE file, const std::string& data) {
    std::size_t offset = 0;
    while (offset < data.size()) {
        DWORD count = 0, wanted = static_cast<DWORD>(std::min<std::size_t>(data.size() - offset, 1024 * 1024));
        if (!WriteFile(file, data.data() + offset, wanted, &count, nullptr) || !count) fail("cannot write SCAD project export");
        offset += count;
    }
}
#endif
std::string read_dependency(const fs::path& root, const fs::path& relative) {
#ifdef _WIN32
    auto dir = win_root(root, false);
    for (const auto& component : relative.parent_path()) {
        auto next = win_relative(dir.value, component, FILE_LIST_DIRECTORY, true, false);
        if (next.value == INVALID_HANDLE_VALUE) fail("missing or reparse-point dependency directory");
        dir = std::move(next);
    }
    auto file = win_relative(dir.value, relative.filename(), GENERIC_READ, false, false);
    if (file.value == INVALID_HANDLE_VALUE) fail("missing or reparse-point dependency");
    return win_read(file.value, kFileLimit);
#else
    struct Fd {
        int value;
        explicit Fd(int fd) : value(fd) {}
        ~Fd() { if (value >= 0) ::close(value); }
    };
    Fd dir(::open(root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (dir.value < 0) fail("cannot open dependency root safely");
    for (const auto& component : relative.parent_path()) {
        int next = ::openat(dir.value, native_policy::path_utf8(component).c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        if (next < 0) fail("missing or symlinked dependency directory");
        ::close(dir.value); dir.value = next;
    }
    Fd file(::openat(dir.value, native_policy::path_utf8(relative.filename()).c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
    struct stat st {};
    if (file.value < 0 || ::fstat(file.value, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 ||
        static_cast<std::size_t>(st.st_size) > kFileLimit) fail("missing, unsafe, or oversized dependency");
    std::string data(static_cast<std::size_t>(st.st_size), '\0');
    std::size_t offset = 0;
    while (offset < data.size()) {
        auto n = ::read(file.value, &data[offset], data.size() - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) fail("dependency changed while reading");
        offset += static_cast<std::size_t>(n);
    }
    char extra; if (::read(file.value, &extra, 1) != 0) fail("dependency changed while reading");
    return data;
#endif
}
void write_file(const fs::path& path, const std::string& data) {
    fs::create_directories(path.parent_path());
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out || !out.write(data.data(), static_cast<std::streamsize>(data.size()))) fail("cannot stage project file");
}
struct Token { std::string text; std::size_t begin, end; bool string = false; };
std::vector<Token> tokens(const std::string& s) {
    std::vector<Token> out;
    for (std::size_t i = 0; i < s.size();) {
        unsigned char c = s[i];
        if (std::isspace(c)) { ++i; continue; }
        if (c == '/' && i + 1 < s.size() && s[i+1] == '/') { i += 2; while (i < s.size() && s[i] != '\n') ++i; continue; }
        if (c == '/' && i + 1 < s.size() && s[i+1] == '*') {
            auto end = s.find("*/", i + 2); if (end == s.npos) fail("unterminated comment"); i = end + 2; continue;
        }
        std::size_t start = i++;
        if (c == '"') {
            bool escaped = false, closed = false;
            while (i < s.size()) { char x = s[i++]; if (!escaped && x == '"') { closed = true; break; } if (!escaped && x == '\\') escaped = true; else escaped = false; }
            if (!closed) fail("unterminated string");
            out.push_back({s.substr(start, i-start), start, i, true}); continue;
        }
        if (std::isalpha(c) || c == '_' || c == '$') while (i < s.size() && (std::isalnum(static_cast<unsigned char>(s[i])) || s[i] == '_')) ++i;
        else if (std::isdigit(c) || (c == '.' && i < s.size() && std::isdigit(static_cast<unsigned char>(s[i])))) {
            while (i < s.size() && (std::isdigit(static_cast<unsigned char>(s[i])) || s[i] == '.')) ++i;
            if (i < s.size() && (s[i] == 'e' || s[i] == 'E')) { ++i; if (i < s.size() && (s[i] == '+' || s[i] == '-')) ++i; while (i < s.size() && std::isdigit(static_cast<unsigned char>(s[i]))) ++i; }
        }
        out.push_back({s.substr(start, i-start), start, i, false});
    }
    return out;
}
std::string string_literal(const Token& token) {
    if (!token.string) fail("dependency must be a literal string");
    try { return json::parse(token.text).get<std::string>(); }
    catch (...) { fail("unsupported string escape in dependency"); }
}
struct Decl { json value; std::string kind; };
std::map<std::string, Decl> declarations(const std::string& source) {
    auto ts = tokens(source); std::map<std::string, Decl> result; int depth = 0, nesting = 0;
    for (std::size_t i = 0; i < ts.size(); ++i) {
        if (ts[i].text == "{") { ++depth; continue; }
        if (ts[i].text == "}") { --depth; continue; }
        if (ts[i].text == "(" || ts[i].text == "[") { ++nesting; continue; }
        if (ts[i].text == ")" || ts[i].text == "]") { --nesting; continue; }
        if (depth != 0 || nesting != 0 || i + 3 >= ts.size() || !identifier(ts[i].text) || ts[i+1].text != "=") continue;
        // A later code-owned assignment supersedes any earlier literal control.
        result.erase(ts[i].text);
        std::size_t n = i + 2; std::string value = ts[n].text;
        if ((value == "-" || value == "+") && n + 1 < ts.size()) value += ts[++n].text;
        if (n + 1 >= ts.size() || ts[n+1].text != ";") continue;
        try {
            json v;
            std::string kind;
            if (ts[i+2].string) { v = json::parse(value); if (!v.is_string()) continue; kind = "string"; }
            else if (value == "true" || value == "false") { v = value == "true"; kind = "boolean"; }
            else { std::size_t used = 0; double d = std::stod(value, &used); if (used != value.size() || !std::isfinite(d)) continue; v = d; kind = "number"; }
            result[ts[i].text] = {v, kind};
        } catch (...) { continue; }
        i = n + 1;
    }
    return result;
}
std::vector<std::string> dependencies(const std::string& source, bool allow_dynamic = false) {
    auto ts = tokens(source); std::vector<std::string> paths;
    for (std::size_t i = 0; i < ts.size(); ++i) {
        const auto& t = ts[i].text;
        if (t == "include" || t == "use") {
            if (i + 2 >= ts.size() || ts[i+1].text != "<") fail("malformed include/use dependency");
            std::size_t j = i + 2; while (j < ts.size() && ts[j].text != ">") ++j;
            if (j == ts.size()) fail("malformed include/use dependency");
            paths.push_back(source.substr(ts[i+1].end, ts[j].begin - ts[i+1].end)); i = j;
        } else if ((t == "import" || t == "import_stl" || t == "import_dxf" || t == "surface") && i + 1 < ts.size() && ts[i+1].text == "(") {
            std::size_t j = i + 2;
            if (j + 1 < ts.size() && ts[j].text == "file" && ts[j+1].text == "=") j += 2;
            if (j >= ts.size() || !ts[j].string || j + 1 >= ts.size() || (ts[j+1].text != "," && ts[j+1].text != ")")) {
                if (allow_dynamic) continue;
                fail("dynamic import/surface dependency cannot be captured; embed it explicitly before import");
            }
            paths.push_back(string_literal(ts[j]));
        }
    }
    return paths;
}
struct Project { std::string source; double factor; json params; std::map<std::string, Decl> decls; json schema; json files; };
Project project(const json& p) {
    if (!p.is_object() || !p.contains("source") || !p["source"].is_string()) fail("source must be a string");
    Project r; r.source = p["source"].get<std::string>();
    if (r.source.size() > kSourceLimit || r.source.find('\0') != std::string::npos) fail("source exceeds limit or contains NUL");
    if (p.contains("mm_per_source_unit") && !p["mm_per_source_unit"].is_number()) fail("unit factor must be numeric");
    r.factor = p.value("mm_per_source_unit", 1.0);
    if (!std::isfinite(r.factor) || r.factor <= 0) fail("unit factor must be finite and positive");
    r.params = p.value("parameters", json::object()); r.schema = p.value("parameter_schema", json::array()); r.files = p.value("files", json::array());
    if (!r.params.is_object() || !r.schema.is_array() || !r.files.is_array() || r.files.size() > kFileCount) fail("invalid parameters, schema, or file list");
    r.decls = declarations(r.source);
    std::set<std::string> names, file_names; std::size_t total = 0;
    for (const auto& file : r.files) {
        if (!file.is_object() || !file.contains("path") || !file["path"].is_string() || !file.contains("data_b64") || !file["data_b64"].is_string()) fail("invalid dependency entry");
        auto path = portable_path(file["path"].get<std::string>());
        if (!file_names.insert(path).second) fail("duplicate dependency path: " + path);
        auto data = b64decode(file["data_b64"].get<std::string>());
        if (data.size() > kFileLimit || (total += data.size()) > kProjectLimit) fail("embedded dependency size limit exceeded");
    }
    for (const auto& item : r.schema) {
        if (!item.is_object() || !item.contains("name") || !item["name"].is_string() || !item.contains("kind") || !item["kind"].is_string()) fail("invalid parameter descriptor");
        auto name = item["name"].get<std::string>(); auto kind = item["kind"].get<std::string>();
        if (!identifier(name) || !names.insert(name).second) fail("invalid or duplicate parameter name");
        if (kind != "length" && kind != "number" && kind != "integer" && kind != "angle" && kind != "boolean" && kind != "string") fail("unsupported parameter kind");
        if (item.contains("label") && !item["label"].is_string()) fail("parameter label must be a string");
        auto d = r.decls.find(name);
        if (d == r.decls.end() && !r.params.contains(name)) fail("parameter descriptor has no literal default or override: " + name);
        if (d != r.decls.end() && ((kind == "boolean" && d->second.kind != "boolean") || (kind == "string" && d->second.kind != "string") || (kind != "boolean" && kind != "string" && d->second.kind != "number"))) fail("parameter descriptor does not match source literal: " + name);
        if (d != r.decls.end()) d->second.kind = kind;
        else r.decls[name] = {nullptr, kind};
    }
    for (auto it = r.params.begin(); it != r.params.end(); ++it)
        if (!identifier(it.key()) || r.decls.find(it.key()) == r.decls.end()) fail("override has no declared parameter: " + it.key());
    return r;
}
void validate_dependencies(const Project& p, bool allow_dynamic = false) {
    std::map<std::string, std::string> files;
    for (const auto& file : p.files)
        files.emplace(file["path"].get<std::string>(), b64decode(file["data_b64"].get<std::string>()));
    std::set<std::string> visited;
    auto visit = [&](auto&& self, const std::string& source, const fs::path& base) -> void {
        for (const auto& raw : dependencies(source, allow_dynamic)) {
            if (raw.empty() || raw.find('\\') != raw.npos || raw.find(':') != raw.npos || raw.find('\0') != raw.npos || fs::u8path(raw).is_absolute())
                fail("dependency path must be relative to the project");
            auto relative = (base / fs::u8path(raw)).lexically_normal();
            auto key = portable_path(native_policy::generic_path_utf8(relative));
            auto file = files.find(key);
            if (file == files.end()) fail("missing embedded dependency: " + key);
            if (!visited.insert(key).second) continue;
            auto ext = relative.extension().string();
            std::transform(ext.begin(), ext.end(), ext.begin(), [](unsigned char c) { return std::tolower(c); });
            if (ext == ".scad") self(self, file->second, relative.parent_path());
        }
    };
    visit(visit, p.source, {});
}
json effective(const json& input, const std::string& kind, const std::map<std::string,double>& env, bool allow_unresolved) {
    if (kind == "boolean") { if (!input.is_boolean()) fail("boolean override required"); return input; }
    if (kind == "string") { if (!input.is_string()) fail("string override required"); return input; }
    double v;
    if (input.is_number()) v = input.get<double>();
    else if (input.is_object()) {
        if (!input.contains("value") || !input["value"].is_number()) fail("numeric override needs a numeric value");
        if (input.contains("expr") && !input["expr"].is_null() && !input["expr"].is_string()) fail("numeric expression must be a string");
        std::string e = input.value("expr", json()).is_string() ? input["expr"].get<std::string>() : "";
        v = !e.empty() && !allow_unresolved ? chudware::util::evaluate(e, env) : input["value"].get<double>();
    } else fail("numeric override required");
    if (!std::isfinite(v) || (kind == "integer" && std::trunc(v) != v)) fail("numeric override is non-finite or not an integer");
    return v;
}
std::map<std::string,double> document_environment(const json& doc) {
    std::map<std::string,double> env;
    if (!doc.contains("equations") || !doc["equations"].is_object() ||
        !doc["equations"].contains("globals") || !doc["equations"]["globals"].is_array()) return env;
    // Match session.cpp: globals are resolved in listed order and invalid
    // expressions are skipped rather than evaluated with a different fallback.
    for (const auto& global : doc["equations"]["globals"]) {
        if (!global.is_object()) fail("invalid managed equation metadata");
        const auto name = global.value("name", std::string());
        const auto expr = global.value("expr", std::string());
        if (name.empty()) continue;
        try { env[name] = util::evaluate(expr, env); }
        catch (...) {}
    }
    return env;
}
std::string scad_literal(const json& v) {
    if (v.is_string()) {
        auto s = v.get<std::string>();
        for (unsigned char c : s) if (c < 32 && c != '\n' && c != '\r' && c != '\t') fail("string contains a control character not representable in SCAD");
    }
    return v.dump(-1, ' ', false, json::error_handler_t::strict);
}
struct PreparedSource { std::string source; std::string uses; };

// OpenSCAD accepts `use` only at file scope. A module body accepts arbitrary
// declarations, but cannot contain `use`, even when it comes from an include.
// Expand includes only when needed to hoist those directives; leave all original
// project bytes intact and rebase included-file imports to their original folder.
PreparedSource prepare_source(const Project& p) {
    std::map<std::string, const json*> files;
    std::map<std::string, std::string> included_data;
    for (const auto& f : p.files) files.emplace(f["path"].get<std::string>(), &f);
    auto file_data = [&](const std::string& key) -> const std::string& {
        auto cached = included_data.find(key);
        if (cached != included_data.end()) return cached->second;
        auto f = files.find(key);
        if (f == files.end()) fail("missing embedded dependency: " + key);
        return included_data.emplace(key, b64decode((*f->second)["data_b64"].get<std::string>())).first->second;
    };
    auto directive = [](const std::string& text, const std::vector<Token>& ts, std::size_t i,
                        const fs::path& base) -> std::pair<std::size_t, std::string> {
        if (i + 2 >= ts.size() || ts[i+1].text != "<") fail("malformed include/use dependency");
        std::size_t j = i + 2;
        while (j < ts.size() && ts[j].text != ">") ++j;
        if (j == ts.size()) fail("malformed include/use dependency");
        auto raw = text.substr(ts[i+1].end, ts[j].begin - ts[i+1].end);
        if (raw.empty() || raw.find('\\') != raw.npos || raw.find(':') != raw.npos ||
            raw.find('\0') != raw.npos || fs::u8path(raw).is_absolute()) fail("dependency path must be relative to the project");
        auto key = portable_path(native_policy::generic_path_utf8((base / fs::u8path(raw)).lexically_normal()));
        return {j, key};
    };
    std::set<std::string> scanned;
    auto has_use = [&](auto&& self, const std::string& text, const fs::path& base) -> bool {
        auto ts = tokens(text);
        for (std::size_t i = 0; i < ts.size(); ++i) {
            if (ts[i].text == "use" && i + 1 < ts.size() && ts[i+1].text == "<") return true;
            if (ts[i].text != "include" || i + 1 >= ts.size() || ts[i+1].text != "<") continue;
            auto item = directive(text, ts, i, base); i = item.first;
            if (scanned.insert(item.second).second && self(self, file_data(item.second), fs::u8path(item.second).parent_path())) return true;
        }
        return false;
    };
    if (!has_use(has_use, p.source, {})) return {p.source, {}};
    PreparedSource result;
    std::set<std::string> stack;
    std::size_t expansions = 0, generated = 0;
    auto expand = [&](auto&& self, const std::string& text, const fs::path& base, int& depth) -> std::string {
        auto ts = tokens(text); std::string out; std::size_t cursor = 0;
        auto append = [&](const std::string& value) {
            if (value.size() > kProjectLimit - generated) fail("expanded SCAD source exceeds project limit");
            generated += value.size(); out += value;
        };
        for (std::size_t i = 0; i < ts.size(); ++i) {
            if (ts[i].text == "{") ++depth;
            else if (ts[i].text == "}") --depth;
            if ((ts[i].text == "include" || ts[i].text == "use") && i + 1 < ts.size() && ts[i+1].text == "<") {
                auto item = directive(text, ts, i, base);
                append(text.substr(cursor, ts[i].begin - cursor));
                if (ts[i].text == "use") {
                    if (depth != 0 || (i && ts[i-1].text != ";" && ts[i-1].text != "}" && ts[i-1].text != ">"))
                        fail("use directive must occur at file scope");
                    result.uses += "use <" + item.second + ">\n";
                    if (result.uses.size() > kSourceLimit) fail("too many SCAD use directives");
                } else {
                    if (++expansions > 1024 || !stack.insert(item.second).second) fail("cyclic or excessive SCAD includes");
                    auto included = self(self, file_data(item.second), fs::u8path(item.second).parent_path(), depth);
                    stack.erase(item.second);
                    append("\n"); append(included); append("\n");
                }
                cursor = ts[item.first].end; i = item.first;
            } else if (!base.empty() && (ts[i].text == "import" || ts[i].text == "import_stl" ||
                       ts[i].text == "import_dxf" || ts[i].text == "surface") &&
                       i + 1 < ts.size() && ts[i+1].text == "(") {
                std::size_t start = i + 2;
                if (start + 1 < ts.size() && ts[start].text == "file" && ts[start+1].text == "=") start += 2;
                if (start >= ts.size()) fail("malformed import/surface dependency");
                std::size_t end = start; int nested = 0;
                for (; end < ts.size(); ++end) {
                    const auto& token = ts[end].text;
                    if (nested == 0 && (token == "," || token == ")")) break;
                    if (token == "(" || token == "[" || token == "{") ++nested;
                    else if (token == ")" || token == "]" || token == "}") --nested;
                    if (nested < 0) fail("malformed import/surface dependency");
                }
                if (end == ts.size() || end == start || nested != 0) fail("malformed import/surface dependency");
                append(text.substr(cursor, ts[start].begin - cursor));
                if (end == start + 1 && ts[start].string) {
                    auto path = portable_path(native_policy::generic_path_utf8((base / fs::u8path(string_literal(ts[start]))).lexically_normal()));
                    append(scad_literal(path));
                } else {
                    auto prefix = native_policy::generic_path_utf8(base) + "/";
                    append("str(" + scad_literal(prefix) + ",(" + text.substr(ts[start].begin, ts[end].begin - ts[start].begin) + "))");
                }
                cursor = ts[end].begin; i = end - 1;
            }
        }
        append(text.substr(cursor));
        return out;
    };
    int depth = 0;
    result.source = expand(expand, p.source, {}, depth);
    return result;
}
std::string executable_source(const Project& p, const std::map<std::string,double>& env) {
    std::ostringstream out; out.imbue(std::locale::classic()); out << std::setprecision(std::numeric_limits<double>::max_digits10);
    auto prepared = prepare_source(p);
    out << prepared.uses << "module __chudware_source() {\n" << prepared.source << "\n";
    for (auto it = p.params.begin(); it != p.params.end(); ++it) {
        auto kind = p.decls.at(it.key()).kind;
        json value = effective(it.value(), kind, env, false);
        if (kind == "length") {
            double source_value = value.get<double>() / p.factor;
            if (!std::isfinite(source_value)) fail("length override cannot be represented in source units");
            value = source_value;
        }
        out << it.key() << " = " << scad_literal(value) << ";\n";
    }
    out << "}\nscale([" << p.factor << "," << p.factor << "," << p.factor << "]) __chudware_source();\n";
    return out.str();
}
std::string discover_executable() {
    const char* configured = std::getenv("CHUDWARE_OPENSCAD_BIN");
    auto executable = [](const fs::path& p) {
        std::error_code ec; if (!fs::is_regular_file(p, ec) || ec) return false;
#ifdef _WIN32
        return _waccess(p.c_str(), 0) == 0;
#else
        return ::access(p.c_str(), X_OK) == 0;
#endif
    };
    if (configured && *configured) {
        fs::path p = fs::u8path(configured);
        if (!executable(p)) return {};
        return native_policy::path_utf8(fs::absolute(p));
    }
    const char* env = std::getenv("PATH"); if (!env) return {};
#ifdef _WIN32
    constexpr char separator = ';'; const char* name = "openscad.exe";
#else
    constexpr char separator = ':'; const char* name = "openscad";
#endif
    std::string path(env); std::size_t start = 0;
    while (start <= path.size()) {
        auto end = path.find(separator, start); if (end == std::string::npos) end = path.size();
        if (end > start) { auto candidate = fs::u8path(path.substr(start, end-start)) / name; if (executable(candidate)) return native_policy::path_utf8(fs::absolute(candidate)); }
        start = end + 1;
    }
    return {};
}
struct TemporaryDirectory {
    fs::path path;
#ifdef _WIN32
    std::vector<firmware::private_storage_detail::Handle> ancestors;
    WinHandle directory;
#endif
    TemporaryDirectory() {
#ifdef _WIN32
        namespace storage = firmware::private_storage_detail;
        auto root = storage::absolute_path(fs::temp_directory_path());
        ancestors = storage::open_directory(root, false);
        WinHandle root_handle(CreateFileW(root.c_str(), FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY |
                                         FILE_ADD_SUBDIRECTORY | SYNCHRONIZE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                         nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
        if (!win_safe_handle(root_handle.value, true)) fail("cannot open private SCAD staging parent");
        storage::PrivateSecurity security;
        std::random_device random;
        for (int i = 0; i < 32; ++i) {
            auto name = fs::u8path("chudware-scad-" + std::to_string(random()) + "-" + std::to_string(random()));
            auto candidate = win_relative(root_handle.value, name,
                FILE_LIST_DIRECTORY | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | DELETE | READ_CONTROL,
                true, true, &security.descriptor, false);
            if (candidate.value == INVALID_HANDLE_VALUE) continue;
            path = root / name;
            directory = std::move(candidate);
            try { storage::validate_handle(directory.value, path, true, true); }
            catch (...) {
                FILE_DISPOSITION_INFO disposition{}; disposition.DeleteFile = TRUE;
                SetFileInformationByHandle(directory.value, FileDispositionInfo, &disposition, sizeof(disposition));
                throw;
            }
            return;
        }
        fail("cannot create private SCAD staging directory");
#else
        auto pattern = native_policy::path_utf8(fs::temp_directory_path() / "chudware-scad-XXXXXX");
        std::vector<char> writable(pattern.begin(), pattern.end());
        writable.push_back('\0');
        if (!::mkdtemp(writable.data())) fail("cannot create private staging directory");
        path = fs::u8path(writable.data());
#endif
    }
    ~TemporaryDirectory() {
#ifdef _WIN32
        // Ancestor locks remain held until the private project is removed.
        directory = WinHandle{};
#endif
        std::error_code ec; if (!path.empty()) fs::remove_all(path, ec);
    }
};
} // namespace

json inspect(const json& params) {
    try {
        auto p = project(params); json controls = json::array();
        std::map<std::string, std::string> labels;
        for (const auto& desc : p.schema) labels[desc["name"].get<std::string>()] = desc.value("label", desc["name"].get<std::string>());
        for (const auto& entry : p.decls) {
            const auto& name = entry.first; const auto& decl = entry.second;
            bool overridden = p.params.contains(name); json value = overridden ? effective(p.params[name], decl.kind, {}, true) : decl.value;
            if (!overridden && decl.kind == "length") {
                double canonical = value.get<double>() * p.factor;
                if (!std::isfinite(canonical)) fail("length default cannot be represented in millimetres");
                value = canonical;
            }
            if (decl.kind == "integer" && (!value.is_number() || std::trunc(value.get<double>()) != value.get<double>())) fail("integer default is not integral: " + name);
            controls.push_back({{"name",name},{"kind",decl.kind},{"label",labels.count(name) ? labels[name] : name},{"value",value},{"overridden",overridden}});
        }
        return {{"ok",true},{"controls",controls},{"mm_per_source_unit",p.factor}};
    } catch (const std::exception& e) { return {{"ok",false},{"error",e.what()}}; }
}
std::string cache_identity() {
    auto exe = discover_executable(); if (exe.empty()) return {};
    std::ifstream input(fs::u8path(exe), std::ios::binary);
    if (!input) return {};
    util::Sha256 digest;
    std::array<char, 65536> buffer{};
    while (input) {
        input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        auto count = input.gcount();
        if (count > 0) digest.update(reinterpret_cast<const uint8_t*>(buffer.data()), static_cast<std::size_t>(count));
    }
    if (input.bad()) return {};
    return exe + ":sha256:" + digest.finish_hex();
}
json capability() {
    auto exe = discover_executable();
    json result = {{"available",false},{"renderer_available",!exe.empty()},{"sandbox_available",false},{"executable",exe},{"version",""},{"geometry_fidelity","faceted"},{"history_recovered",false}};
    if (exe.empty()) { result["error"] = "OpenSCAD not found; install OpenSCAD or set CHUDWARE_OPENSCAD_BIN to its executable"; return result; }
    auto status = util::scad_process::capability(exe);
    result["sandbox_available"] = status.status == util::scad_process::Status::success;
    if (status.status != util::scad_process::Status::success) { result["error"] = status.diagnostics; return result; }
    result["available"] = true; result["version"] = status.diagnostics; return result;
}
json import_project(const std::string& path) {
    auto reference = native_policy::authored_reference(path);
    auto resolved = native_policy::resolve_reference(reference);
    fs::path entry = fs::absolute(fs::u8path(resolved));
    auto source = read_bounded(entry, (kProjectLimit + kSourceLimit) * 2);
    if (source.compare(0, kMarker.size(), kMarker) == 0) {
        auto end = source.find('\n'); if (end == source.npos) fail("invalid embedded project metadata");
        json saved;
        try { saved = json::parse(b64decode(source.substr(kMarker.size(), end-kMarker.size()))); }
        catch (const std::exception&) { fail("invalid embedded project metadata"); }
        if (!saved.is_object() || !saved.contains("params") || !saved.contains("env") || !saved["env"].is_object()) fail("invalid embedded project metadata");
        auto env = saved["env"].get<std::map<std::string, double>>();
        auto p = project(saved["params"]);
        if (export_source(saved["params"], env) != source) fail("source differs from embedded project metadata");
        for (const auto& file : p.files) {
            if (read_dependency(entry.parent_path(), fs::u8path(file["path"].get<std::string>())) != b64decode(file["data_b64"].get<std::string>()))
                fail("exported dependency differs from embedded project metadata");
        }
        // Only verified managed projections can restore their equation environment.
        json result = saved["params"];
        json restoration = result.contains("native_projection") ? restore_native(result) : json::object();
        bool managed = restoration.value("ok", false);
        if (managed) {
            const auto native_env = document_environment(restoration.at("document"));
            for (auto it = p.params.begin(); it != p.params.end(); ++it) {
                if (!it.value().is_object() || !it.value().contains("expr") ||
                    !it.value()["expr"].is_string() || it.value()["expr"].get<std::string>().empty()) continue;
                try {
                    const auto kind = p.decls.at(it.key()).kind;
                    if (effective(it.value(), kind, native_env, false) != effective(it.value(), kind, env, false))
                        fail("managed expression does not match the exported equation environment: " + it.key());
                } catch (const util::ExprError&) {
                    fail("managed expression cannot be resolved in the restored equation environment: " + it.key());
                }
            }
        }
        for (auto it = p.params.begin(); it != p.params.end(); ++it)
            if (!managed && it.value().is_object() && it.value().contains("expr") && it.value()["expr"].is_string() && !it.value()["expr"].get<std::string>().empty())
                result["parameters"][it.key()] = effective(it.value(), p.decls.at(it.key()).kind, env, false);
        return result;
    }
    if (source.size() > kSourceLimit) fail("source exceeds size limit");
    fs::path root = entry.parent_path();
    json files = json::array(); std::set<std::string> visited; std::size_t total = source.size();
    auto capture = [&](auto&& self, const std::string& content, const fs::path& base) -> void {
        for (const auto& raw : dependencies(content)) {
            if (raw.empty() || raw.find('\\') != raw.npos || raw.find(':') != raw.npos || raw.find('\0') != raw.npos || fs::u8path(raw).is_absolute())
                fail("dependency path must be relative to the project");
            auto relative = (base / fs::u8path(raw)).lexically_normal();
            auto spelling = portable_path(native_policy::generic_path_utf8(relative));
            if (!visited.insert(spelling).second) continue;
            if (visited.size() > kFileCount) fail("too many dependencies");
            auto data = read_dependency(root, relative);
            total += data.size(); if (total > kProjectLimit) fail("project exceeds size limit");
            files.push_back({{"path",spelling},{"data_b64",b64encode(data)}});
            auto ext = relative.extension().string(); std::transform(ext.begin(), ext.end(), ext.begin(), [](unsigned char c){ return std::tolower(c); });
            if (ext == ".scad") self(self, data, relative.parent_path());
        }
    };
    capture(capture, source, {});
    return {{"source",source},{"files",files},{"parameters",json::object()},{"parameter_schema",json::array()},{"mm_per_source_unit",1.0}};
}
std::string export_source(const json& params, const std::map<std::string,double>& env) {
    auto p = project(params);
    validate_dependencies(p, !p.files.empty());
    auto state = inspect(params); if (!state.value("ok", false)) fail(state.value("error", "invalid source controls"));
    for (const auto& pair : env) if (!std::isfinite(pair.second)) fail("non-finite export environment");
    json metadata = {{"params",params},{"env",env}};
    auto output = kMarker + b64encode(metadata.dump()) + "\n" + executable_source(p, env);
    if (output.size() > (kProjectLimit + kSourceLimit) * 2) fail("exported project exceeds portable source limit");
    return output;
}
json export_project(const json& params, const std::string& path, const std::map<std::string,double>& env) {
    auto p = project(params);
    const auto text = export_source(params, env);
    auto reference = native_policy::authored_reference(path);
    auto resolved = native_policy::resolve_reference(reference);
    auto output = fs::absolute(fs::u8path(resolved));
    const auto name = native_policy::path_utf8(output.filename());
    portable_path(name);
    json exported = json::array({native_policy::path_utf8(output)});
    json reused = json::array();
    json values = json::object();
    for (auto it = p.params.begin(); it != p.params.end(); ++it)
        values[it.key()] = effective(it.value(), p.decls.at(it.key()).kind, env, false);
    for (const auto& file : p.files) {
        auto dependency = file["path"].get<std::string>();
        if (dependency == name || dependency.compare(0, name.size() + 1, name + "/") == 0)
            fail("export path conflicts with a dependency");
#ifdef _WIN32
        auto dependency_path = fs::u8path(dependency);
        auto first = dependency_path.begin()->native();
        auto output_name = output.filename().native();
        if (CompareStringOrdinal(first.c_str(), static_cast<int>(first.size()),
                                 output_name.c_str(), static_cast<int>(output_name.size()), TRUE) == CSTR_EQUAL)
            fail("export path conflicts with a dependency");
#endif
    }
#ifdef _WIN32
    auto root = win_root(output.parent_path(), true);
    struct Cleanup {
        std::vector<WinHandle> created;
        bool committed = false;
        ~Cleanup() {
            if (!committed) for (auto it = created.rbegin(); it != created.rend(); ++it) {
                FILE_DISPOSITION_INFO disposition{}; disposition.DeleteFile = TRUE;
                SetFileInformationByHandle(it->value, FileDispositionInfo, &disposition, sizeof(disposition));
                CloseHandle(it->value); it->value = INVALID_HANDLE_VALUE;
            }
        }
        void track(HANDLE handle) {
            HANDLE duplicate = INVALID_HANDLE_VALUE;
            if (!DuplicateHandle(GetCurrentProcess(), handle, GetCurrentProcess(), &duplicate, 0, FALSE, DUPLICATE_SAME_ACCESS)) {
                FILE_DISPOSITION_INFO disposition{}; disposition.DeleteFile = TRUE;
                SetFileInformationByHandle(handle, FileDispositionInfo, &disposition, sizeof(disposition));
                fail("cannot track SCAD project output");
            }
            created.emplace_back(duplicate);
        }
    } cleanup;
    for (const auto& file : p.files) {
        auto relative = fs::u8path(file["path"].get<std::string>());
        WinHandle dir;
        HANDLE duplicate = INVALID_HANDLE_VALUE;
        if (!DuplicateHandle(GetCurrentProcess(), root.value, GetCurrentProcess(), &duplicate, 0, FALSE, DUPLICATE_SAME_ACCESS))
            fail("cannot duplicate project directory");
        dir = WinHandle(duplicate);
        for (const auto& component : relative.parent_path()) {
            auto next = win_relative(dir.value, component,
                FILE_LIST_DIRECTORY | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | DELETE, true, true);
            if (next.value != INVALID_HANDLE_VALUE) cleanup.track(next.value);
            else next = win_relative(dir.value, component, FILE_LIST_DIRECTORY | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY, true, false);
            if (next.value == INVALID_HANDLE_VALUE) fail("dependency directory is missing or is a reparse point");
            dir = std::move(next);
        }
        auto data = b64decode(file["data_b64"].get<std::string>());
        auto target = win_relative(dir.value, relative.filename(), GENERIC_WRITE | DELETE, false, true);
        if (target.value != INVALID_HANDLE_VALUE) {
            cleanup.track(target.value);
            win_write(target.value, data);
            exported.push_back(native_policy::path_utf8(output.parent_path() / relative));
        } else {
            auto existing = win_relative(dir.value, relative.filename(), GENERIC_READ, false, false);
            if (existing.value == INVALID_HANDLE_VALUE || win_read(existing.value, kFileLimit) != data)
                fail("exported dependency conflicts with an existing file or reparse point");
            reused.push_back(native_policy::path_utf8(output.parent_path() / relative));
        }
    }
    std::random_device random;
    WinHandle target;
    for (int i = 0; i < 32 && target.value == INVALID_HANDLE_VALUE; ++i) {
        fs::path temporary = ".chudware-scad-" + std::to_string(random()) + "-" + std::to_string(random());
        target = win_relative(root.value, temporary, GENERIC_WRITE | DELETE, false, true);
    }
    if (target.value == INVALID_HANDLE_VALUE) fail("cannot create temporary source export");
    cleanup.track(target.value);
    win_write(target.value, text);
    if (!FlushFileBuffers(target.value)) fail("cannot flush SCAD source export");
    auto filename = output.filename().native();
    const std::size_t bytes = offsetof(FILE_RENAME_INFO, FileName) + filename.size() * sizeof(wchar_t);
    std::vector<std::max_align_t> storage((bytes + sizeof(std::max_align_t) - 1) / sizeof(std::max_align_t));
    auto* rename = reinterpret_cast<FILE_RENAME_INFO*>(storage.data());
    rename->ReplaceIfExists = TRUE; rename->RootDirectory = root.value;
    rename->FileNameLength = static_cast<DWORD>(filename.size() * sizeof(wchar_t));
    std::copy(filename.begin(), filename.end(), rename->FileName);
    if (!SetFileInformationByHandle(target.value, FileRenameInfo, rename, static_cast<DWORD>(bytes)))
        fail("cannot publish source export");
    cleanup.committed = true;
#else
    struct Cleanup {
        struct Entry { int parent; std::string name; bool directory; };
        std::vector<Entry> created;
        bool committed = false;
        ~Cleanup() {
            for (auto i = created.rbegin(); i != created.rend(); ++i) {
                if (!committed) ::unlinkat(i->parent, i->name.c_str(), i->directory ? AT_REMOVEDIR : 0);
                ::close(i->parent);
            }
        }
        void track(int parent, const std::string& n, bool dir) {
            int copy = ::dup(parent);
            if (copy < 0) { ::unlinkat(parent, n.c_str(), dir ? AT_REMOVEDIR : 0); fail("cannot track export output"); }
            created.push_back({copy, n, dir});
        }
    } cleanup;
    struct Fd {
        int value;
        explicit Fd(int fd) : value(fd) {}
        ~Fd() { if (value >= 0) ::close(value); }
        Fd(const Fd&) = delete; Fd& operator=(const Fd&) = delete;
    };
    Fd root(::open(output.parent_path().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (root.value < 0) fail("cannot open export directory safely");
    auto write_all = [](int fd, const std::string& data) {
        std::size_t off = 0;
        while (off < data.size()) {
            auto n = ::write(fd, data.data() + off, data.size() - off);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) fail("cannot write SCAD project export");
            off += static_cast<std::size_t>(n);
        }
    };
    for (const auto& file : p.files) {
        const auto relative = fs::u8path(file["path"].get<std::string>());
        Fd dir(::dup(root.value)); if (dir.value < 0) fail("cannot duplicate export directory");
        for (const auto& component : relative.parent_path()) {
            const auto part = native_policy::path_utf8(component);
            if (::mkdirat(dir.value, part.c_str(), 0700) == 0) cleanup.track(dir.value, part, true);
            else if (errno != EEXIST) fail("cannot create dependency directory");
            int next = ::openat(dir.value, part.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
            if (next < 0) fail("dependency directory is missing or is a symlink");
            ::close(dir.value); dir.value = next;
        }
        auto filename = native_policy::path_utf8(relative.filename());
        auto data = b64decode(file["data_b64"].get<std::string>());
        int fd = ::openat(dir.value, filename.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd >= 0) {
            Fd target(fd); cleanup.track(dir.value, filename, false); write_all(target.value, data);
            exported.push_back(native_policy::path_utf8(output.parent_path() / relative));
        } else {
            if (errno != EEXIST) fail("cannot create exported dependency");
            Fd existing(::openat(dir.value, filename.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK));
            struct stat st {};
            if (existing.value < 0 || ::fstat(existing.value, &st) || !S_ISREG(st.st_mode) ||
                st.st_size < 0 || static_cast<std::size_t>(st.st_size) != data.size())
                fail("exported dependency conflicts with an existing file");
            std::array<char, 8192> buffer{}; std::size_t offset = 0;
            while (offset < data.size()) {
                auto n = ::read(existing.value, buffer.data(), std::min(buffer.size(), data.size() - offset));
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0 || data.compare(offset, static_cast<std::size_t>(n), buffer.data(), static_cast<std::size_t>(n)) != 0)
                    fail("exported dependency conflicts with an existing file");
                offset += static_cast<std::size_t>(n);
            }
            reused.push_back(native_policy::path_utf8(output.parent_path() / relative));
        }
    }
    std::random_device random;
    std::string temporary;
    int fd = -1;
    for (int i = 0; i < 32 && fd < 0; ++i) {
        temporary = ".chudware-scad-" + std::to_string(random()) + "-" + std::to_string(random());
        fd = ::openat(root.value, temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd < 0 && errno != EEXIST) fail("cannot create temporary source export");
    }
    if (fd < 0) fail("cannot create temporary source export");
    Fd target(fd); cleanup.track(root.value, temporary, false);
    write_all(target.value, text);
    if (::renameat(root.value, temporary.c_str(), root.value, name.c_str()) != 0) fail("cannot publish source export");
    cleanup.committed = true;
#endif
    return {{"ok",true},{"path",native_policy::path_utf8(output)},{"exported_files",exported},
            {"reused_files",reused},{"mm_per_source_unit",p.factor},{"effective_overrides",values},
            {"geometry_fidelity","faceted"},{"history_recovered",false}};
}
Build build(const json& params, const std::map<std::string,double>& env) {
    auto p = project(params);
    validate_dependencies(p, !p.files.empty());
    auto state = inspect(params); if (!state.value("ok", false)) fail(state.value("error", "invalid source controls"));
    auto cap = capability(); if (!cap.value("available", false)) fail(cap.value("error", "renderer or sandbox unavailable"));
    TemporaryDirectory tmp;
    for (const auto& file : p.files) write_file(tmp.path / fs::u8path(file["path"].get<std::string>()), b64decode(file["data_b64"].get<std::string>()));
    // Reserve names so embedded dependencies cannot replace renderer inputs or output.
    for (const auto& file : p.files) if (file["path"] == "__chudware_entry.scad" || file["path"] == "__chudware_output.stl") fail("dependency uses a reserved renderer filename");
    write_file(tmp.path / "__chudware_entry.scad", executable_source(p, env));
    util::scad_process::Request request;
    request.executable = cap["executable"].get<std::string>(); request.work_dir = native_policy::path_utf8(tmp.path);
    request.arguments = {"--export-format", "binstl", "-o", "__chudware_output.stl", "__chudware_entry.scad"};
    request.output_file = "__chudware_output.stl";
    request.max_output_bytes = 84 + kTriangles * 50;
    auto rendered = util::scad_process::run(request);
    if (rendered.status != util::scad_process::Status::success) fail("render failed: " + rendered.diagnostics);
    std::string diagnostic = rendered.diagnostics;
    std::transform(diagnostic.begin(), diagnostic.end(), diagnostic.begin(), [](unsigned char c) { return std::tolower(c); });
    if (diagnostic.find("can't open") != std::string::npos || diagnostic.find("cannot open") != std::string::npos ||
        diagnostic.find("could not open") != std::string::npos || diagnostic.find("can't find") != std::string::npos ||
        diagnostic.find("unable to open") != std::string::npos)
        fail("renderer reported a missing dependency: " + rendered.diagnostics);
    auto output = tmp.path / request.output_file;
    auto bytes = read_bounded(output, request.max_output_bytes);
    if (bytes.size() < 84) fail("renderer returned an invalid binary STL");
    const auto* b = reinterpret_cast<const unsigned char*>(bytes.data() + 80);
    std::uint32_t count = static_cast<std::uint32_t>(b[0]) | (static_cast<std::uint32_t>(b[1]) << 8) | (static_cast<std::uint32_t>(b[2]) << 16) | (static_cast<std::uint32_t>(b[3]) << 24);
    if (!count || count > kTriangles || bytes.size() != 84 + static_cast<std::size_t>(count) * 50) fail("renderer output exceeds triangle limit or is invalid");
    auto mesh = read_mesh(native_policy::path_utf8(output), "stl");
    if (mesh.triangle_count() != count || mesh.vertex_count() > count * 3) fail("invalid rendered mesh");
    for (double coord : mesh.verts) if (!std::isfinite(coord)) fail("renderer produced non-finite mesh coordinates");
    auto shape = mesh_to_shape(mesh); if (shape.IsNull()) fail("renderer produced no usable geometry");
    return {shape, {{"geometry_fidelity","faceted"},{"history_recovered",false},{"renderer",cap["executable"]},{"renderer_version",cap["version"]},{"diagnostics",rendered.diagnostics},{"triangles",count},{"mm_per_source_unit",p.factor}}};
}
} // namespace chudware::doc::scad
