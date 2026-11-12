#include "scad_process.h"

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <userenv.h>
#include <sddl.h>
#include <bcrypt.h>
#include <aclapi.h>
#include <objbase.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <climits>
#include <cstdint>
#include <cstring>
#include <cwchar>
#include <filesystem>
#include <limits>
#include <set>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

namespace chudware::util::scad_process::detail {
namespace {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
constexpr std::size_t kDiagnostics = 4 * 1024 * 1024;
constexpr std::uint64_t kArtifact = 1024ULL * 1024 * 1024;
constexpr std::uint64_t kInputs = 256ULL * 1024 * 1024;
constexpr std::size_t kEntries = 4096;

Result fail(Status s, std::string message) { return {s, -1, std::move(message)}; }
bool nul(const std::string& s) { return s.find('\0') != std::string::npos; }

struct Handle {
    HANDLE value = INVALID_HANDLE_VALUE;
    explicit Handle(HANDLE h = INVALID_HANDLE_VALUE) : value(h) {}
    ~Handle() { reset(); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    Handle(Handle&& other) noexcept : value(other.value) { other.value = INVALID_HANDLE_VALUE; }
    Handle& operator=(Handle&& other) noexcept {
        if (this != &other) { reset(); value = other.value; other.value = INVALID_HANDLE_VALUE; }
        return *this;
    }
    void reset(HANDLE h = INVALID_HANDLE_VALUE) {
        if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value);
        value = h;
    }
    explicit operator bool() const { return value && value != INVALID_HANDLE_VALUE; }
};
struct Local {
    void* p = nullptr;
    ~Local() { if (p) LocalFree(p); }
};
std::wstring wide(const std::string& s) {
    if (s.empty()) return {};
    if (s.size() > static_cast<std::size_t>(INT_MAX) || nul(s)) return {};
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (!n) return {};
    std::wstring result(static_cast<std::size_t>(n), L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()), result.data(), n)) return {};
    return result;
}
std::wstring lower(std::wstring s) {
    for (auto& c : s) if (c >= L'A' && c <= L'Z') c += L'a' - L'A';
    return s;
}
bool valid_name(const std::wstring& name) {
    if (name.empty() || name == L"." || name == L".." || name.back() == L'.' || name.back() == L' ') return false;
    for (wchar_t c : name)
        if (c < 32 || c == L'/' || c == L'\\' || c == L':' || c == L'"' || c == L'<' || c == L'>' || c == L'|' || c == L'?' || c == L'*') return false;
    auto base = lower(name.substr(0, name.find(L'.')));
    while (!base.empty() && base.back() == L' ') base.pop_back();
    if (base == L"con" || base == L"prn" || base == L"aux" || base == L"nul" || base == L"conin$" || base == L"conout$") return false;
    if (base.size() == 4 && (base.substr(0, 3) == L"com" || base.substr(0, 3) == L"lpt") &&
        ((base[3] >= L'1' && base[3] <= L'9') || base[3] == L'\u00b9' || base[3] == L'\u00b2' || base[3] == L'\u00b3')) return false;
    return true;
}
bool regular(HANDLE h, std::uint64_t* size = nullptr) {
    FILE_ATTRIBUTE_TAG_INFO tag{};
    BY_HANDLE_FILE_INFORMATION info{};
    LARGE_INTEGER n{};
    if (GetFileType(h) != FILE_TYPE_DISK || !GetFileInformationByHandleEx(h, FileAttributeTagInfo, &tag, sizeof(tag)) ||
        (tag.FileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
        !GetFileInformationByHandle(h, &info) || info.nNumberOfLinks != 1 || !GetFileSizeEx(h, &n) || n.QuadPart < 0) return false;
    if (size) *size = static_cast<std::uint64_t>(n.QuadPart);
    return true;
}
bool directory(HANDLE h) {
    FILE_ATTRIBUTE_TAG_INFO tag{};
    return GetFileInformationByHandleEx(h, FileAttributeTagInfo, &tag, sizeof(tag)) &&
        (tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) && !(tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT);
}
// Keep every component open without FILE_SHARE_DELETE until operations on it
// finish. An attacker cannot substitute a junction or rename an ancestor between
// validation and opening a child. The leaf itself is opened without following it.
bool lock_path(const fs::path& input, std::vector<Handle>& locks, fs::path& absolute) {
    std::error_code ec;
    absolute = fs::absolute(input, ec);
    if (ec) return false;
    for (const auto& part : absolute.relative_path()) if (part == L"." || part == L"..") return false;
    absolute = absolute.lexically_normal();
    if (absolute != absolute.root_path() && absolute.filename().empty()) absolute = absolute.parent_path();
    if (ec || !absolute.is_absolute() || absolute.native().rfind(L"\\\\", 0) == 0) return false;
    fs::path current = absolute.root_path();
    Handle root(CreateFileW(current.c_str(), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!root || !directory(root.value)) return false;
    locks.push_back(std::move(root));
    for (const auto& component : absolute.relative_path()) {
        if (!valid_name(component.native())) return false;
        current /= component;
        Handle next(CreateFileW(current.c_str(), FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES,
                                FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
        if (!next || !directory(next.value)) return false;
        locks.push_back(std::move(next));
    }
    return true;
}
std::wstring token_sid() {
    Handle token;
    HANDLE raw = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw)) return {};
    token.reset(raw);
    DWORD n = 0;
    GetTokenInformation(token.value, TokenUser, nullptr, 0, &n);
    if (!n || n > 65536) return {};
    std::vector<unsigned char> bytes(n);
    if (!GetTokenInformation(token.value, TokenUser, bytes.data(), n, &n)) return {};
    LPWSTR sid = nullptr;
    if (!ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(bytes.data())->User.Sid, &sid)) return {};
    std::wstring result(sid); LocalFree(sid); return result;
}
std::wstring random_name() {
    std::array<unsigned char, 16> bytes{};
    if (BCryptGenRandom(nullptr, bytes.data(), static_cast<ULONG>(bytes.size()), BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) return {};
    static const wchar_t digits[] = L"0123456789abcdef";
    std::wstring result = L"chudware-scad-";
    for (unsigned char c : bytes) { result += digits[c >> 4]; result += digits[c & 15]; }
    return result;
}
struct Profile {
    std::wstring name;
    PSID sid = nullptr;
    ~Profile() { if (sid) FreeSid(sid); if (!name.empty()) DeleteAppContainerProfile(name.c_str()); }
    bool create() {
        name = random_name();
        if (name.empty()) return false;
        HRESULT hr = CreateAppContainerProfile(name.c_str(), name.c_str(), L"Chudware SCAD isolated renderer", nullptr, 0, &sid);
        if (FAILED(hr)) { name.clear(); return false; }
        return sid != nullptr;
    }
};
// CreateAppContainerProfile also creates package-local writable storage. Remove
// the package's access to that unique profile tree before launching: the only
// file the renderer may write in its staged filesystem is the output file.
bool restrict_profile_tree(const fs::path& path, PACL acl, Clock::time_point end,
                           std::size_t& count, unsigned depth = 0) {
    if (depth > 128 || ++count > kEntries || Clock::now() >= end) return false;
    Handle h(CreateFileW(path.c_str(), READ_CONTROL | WRITE_DAC | FILE_READ_ATTRIBUTES,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                         FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!h) return false;
    FILE_ATTRIBUTE_TAG_INFO tag{};
    if (!GetFileInformationByHandleEx(h.value, FileAttributeTagInfo, &tag, sizeof(tag)) ||
        (tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) return false;
    if (SetSecurityInfo(h.value, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                        nullptr, nullptr, acl, nullptr) != ERROR_SUCCESS) return false;
    if (!(tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY)) return regular(h.value);
    std::error_code ec;
    fs::directory_iterator it(path, fs::directory_options::none, ec), finish;
    if (ec) return false;
    for (; it != finish; it.increment(ec)) {
        if (ec || !valid_name(it->path().filename().native()) ||
            !restrict_profile_tree(it->path(), acl, end, count, depth + 1)) return false;
    }
    return !ec;
}
bool restrict_profile(const Profile& profile, const std::wstring& user, Clock::time_point end) {
    LPWSTR sid_text = nullptr;
    if (!ConvertSidToStringSidW(profile.sid, &sid_text)) return false;
    PWSTR folder = nullptr;
    HRESULT hr = GetAppContainerFolderPath(sid_text, &folder);
    LocalFree(sid_text);
    if (FAILED(hr) || !folder) return false;
    fs::path app_data(folder);
    CoTaskMemFree(folder);
    if (app_data != app_data.root_path() && app_data.filename().empty()) app_data = app_data.parent_path();
    if (lower(app_data.filename().native()) != L"ac" ||
        lower(app_data.parent_path().filename().native()) != lower(profile.name)) return false;
    auto root = app_data.parent_path();
    std::vector<Handle> locks; fs::path locked;
    if (!lock_path(root, locks, locked)) return false;
    const auto sddl = L"O:" + user + L"D:P(A;OICI;FA;;;" + user + L")(A;OICI;FA;;;SY)";
    Local descriptor;
    PSECURITY_DESCRIPTOR raw = nullptr;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &raw, nullptr)) return false;
    descriptor.p = raw;
    BOOL present = FALSE, defaulted = FALSE;
    PACL acl = nullptr;
    if (!GetSecurityDescriptorDacl(raw, &present, &acl, &defaulted) || !present || !acl) return false;
    std::size_t count = 0;
    return restrict_profile_tree(locked, acl, end, count);
}
struct Security {
    Local descriptor;
    SECURITY_ATTRIBUTES attributes{};
    bool create(const std::wstring& user, PSID package, bool output) {
        LPWSTR sid = nullptr;
        if (!ConvertSidToStringSidW(package, &sid)) return false;
        std::wstring sddl = L"O:" + user + L"D:P(A;OICI;FA;;;" + user + L")(A;OICI;FA;;;SY)(A;";
        sddl += output ? L";0x12019f;;;" : L"OICI;0x1200a9;;;";
        sddl += sid; sddl += L")"; LocalFree(sid);
        if (output) sddl += L"S:(ML;;NW;;;LW)";
        PSECURITY_DESCRIPTOR d = nullptr;
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &d, nullptr)) return false;
        descriptor.p = d;
        attributes.nLength = sizeof(attributes);
        attributes.lpSecurityDescriptor = d;
        return true;
    }
};
struct Temp {
    fs::path path;
    ~Temp() { if (!path.empty()) { std::error_code ec; fs::remove_all(path, ec); } }
    bool create(SECURITY_ATTRIBUTES* security) {
        std::vector<wchar_t> base(32768);
        DWORD n = GetTempPathW(static_cast<DWORD>(base.size()), base.data());
        if (!n || n >= base.size()) return false;
        std::vector<Handle> locks; fs::path parent;
        if (!lock_path(fs::path(base.data()), locks, parent)) return false;
        for (unsigned i = 0; i != 16; ++i) {
            auto name = random_name(); if (name.empty()) return false;
            auto candidate = parent / name;
            if (CreateDirectoryW(candidate.c_str(), security)) { path = candidate; return true; }
            if (GetLastError() != ERROR_ALREADY_EXISTS) return false;
        }
        return false;
    }
};
struct Budget {
    std::size_t entries = 0;
    std::uint64_t bytes = 0;
    Clock::time_point end;
    bool expired() const { return Clock::now() >= end; }
};
bool copy_open(HANDLE input, const fs::path& dest, Budget& budget, SECURITY_ATTRIBUTES* security, std::string& error) {
    std::uint64_t size = 0;
    if (!regular(input, &size) || size > kInputs - budget.bytes) { error = "SCAD input is not a bounded regular file"; return false; }
    Handle output(CreateFileW(dest.c_str(), GENERIC_WRITE, 0, security, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr));
    if (!output) { error = "Cannot create private staged file"; return false; }
    std::array<char, 32768> buffer{};
    for (;;) {
        if (budget.expired()) { error = "SCAD staging exceeded its deadline"; return false; }
        DWORD n = 0;
        if (!ReadFile(input, buffer.data(), static_cast<DWORD>(buffer.size()), &n, nullptr)) { error = "Cannot read SCAD input"; return false; }
        if (!n) break;
        if (n > kInputs - budget.bytes) { error = "SCAD input size limit exceeded"; return false; }
        budget.bytes += n;
        DWORD offset = 0;
        while (offset < n) {
            DWORD written = 0;
            if (!WriteFile(output.value, buffer.data() + offset, n - offset, &written, nullptr) || !written) { error = "Cannot copy SCAD input"; return false; }
            offset += written;
        }
    }
    return true;
}
bool copy_file(const fs::path& src, const fs::path& dst, Budget& budget, SECURITY_ATTRIBUTES* security, std::string& error) {
    if (++budget.entries > kEntries) { error = "SCAD input count limit exceeded"; return false; }
    Handle in(CreateFileW(src.c_str(), GENERIC_READ | FILE_READ_ATTRIBUTES, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                          FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
    if (!in) { error = "Cannot open SCAD input"; return false; }
    return copy_open(in.value, dst, budget, security, error);
}
bool copy_tree(const fs::path& source, const fs::path& dest, Budget& budget,
               SECURITY_ATTRIBUTES* security, const std::wstring& skip, std::string& error, unsigned depth = 0) {
    if (depth > 128 || budget.expired()) { error = "SCAD staging exceeded nesting or time limit"; return false; }
    Handle lock(CreateFileW(source.c_str(), FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!lock || !directory(lock.value)) { error = "SCAD input directory is a reparse point or inaccessible"; return false; }
    std::error_code ec;
    fs::directory_iterator it(source, fs::directory_options::none, ec), end;
    if (ec) { error = "Cannot enumerate SCAD inputs"; return false; }
    for (; it != end; it.increment(ec)) {
        if (ec || budget.expired()) { error = "Cannot enumerate SCAD inputs within deadline"; return false; }
        const auto name = it->path().filename().native();
        if (!valid_name(name)) { error = "Unsafe SCAD input name"; return false; }
        DWORD attr = GetFileAttributesW(it->path().c_str());
        if (attr == INVALID_FILE_ATTRIBUTES || (attr & FILE_ATTRIBUTE_REPARSE_POINT)) { error = "SCAD inputs cannot contain reparse points"; return false; }
        if (depth == 0 && !skip.empty() && lower(name) == lower(skip)) continue;
        if (attr & FILE_ATTRIBUTE_DIRECTORY) {
            if (++budget.entries > kEntries || !CreateDirectoryW((dest / name).c_str(), security)) { error = "Cannot create staged SCAD directory"; return false; }
            if (!copy_tree(it->path(), dest / name, budget, security, L"", error, depth + 1)) return false;
        } else if (!copy_file(it->path(), dest / name, budget, security, error)) return false;
    }
    if (ec) { error = "Cannot enumerate SCAD inputs"; return false; }
    return true;
}

// Extract import names from an on-disk PE image without loading untrusted code.
// Both ordinary and delay imports are bounded and every RVA must resolve to raw
// bytes in one section. The loader may use OS DLLs from System32; non-system
// imports are copied individually from the renderer directory, never its tree.
bool imports(const fs::path& image, std::vector<std::wstring>& names, Clock::time_point end) {
    Handle file(CreateFileW(image.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    std::uint64_t size = 0;
    if (!file || !regular(file.value, &size) || size < sizeof(IMAGE_DOS_HEADER) || size > 128ULL * 1024 * 1024 || Clock::now() >= end) return false;
    std::vector<unsigned char> data(static_cast<std::size_t>(size));
    DWORD read = 0;
    if (!ReadFile(file.value, data.data(), static_cast<DWORD>(data.size()), &read, nullptr) || read != data.size()) return false;
    auto at = [&](std::size_t offset, std::size_t n) -> const unsigned char* {
        return offset <= data.size() && n <= data.size() - offset ? data.data() + offset : nullptr;
    };
    auto dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(at(0, sizeof(IMAGE_DOS_HEADER)));
    if (!dos || dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0) return false;
    std::size_t nt = static_cast<std::size_t>(dos->e_lfanew);
    auto sig = at(nt, 4 + sizeof(IMAGE_FILE_HEADER));
    if (!sig) return false;
    DWORD signature; std::memcpy(&signature, sig, sizeof(signature));
    if (signature != IMAGE_NT_SIGNATURE) return false;
    IMAGE_FILE_HEADER header{}; std::memcpy(&header, sig + 4, sizeof(header));
    if (!header.NumberOfSections || header.NumberOfSections > 96) return false;
    const std::size_t opt = nt + 4 + sizeof(header);
    auto optional = at(opt, header.SizeOfOptionalHeader);
    if (!optional || header.SizeOfOptionalHeader < sizeof(WORD)) return false;
    WORD magic; std::memcpy(&magic, optional, sizeof(magic));
    std::size_t dirs = 0, num_offset = 0;
    if (magic == IMAGE_NT_OPTIONAL_HDR64_MAGIC) {
        dirs = offsetof(IMAGE_OPTIONAL_HEADER64, DataDirectory); num_offset = offsetof(IMAGE_OPTIONAL_HEADER64, NumberOfRvaAndSizes);
    } else if (magic == IMAGE_NT_OPTIONAL_HDR32_MAGIC) {
        dirs = offsetof(IMAGE_OPTIONAL_HEADER32, DataDirectory); num_offset = offsetof(IMAGE_OPTIONAL_HEADER32, NumberOfRvaAndSizes);
    } else return false;
    if (num_offset + sizeof(DWORD) > header.SizeOfOptionalHeader) return false;
    DWORD count; std::memcpy(&count, optional + num_offset, sizeof(count));
    const auto sections_offset = opt + header.SizeOfOptionalHeader;
    if (!at(sections_offset, static_cast<std::size_t>(header.NumberOfSections) * sizeof(IMAGE_SECTION_HEADER))) return false;
    auto rva = [&](DWORD address, std::size_t n) -> const unsigned char* {
        for (unsigned i = 0; i < header.NumberOfSections; ++i) {
            IMAGE_SECTION_HEADER section{};
            std::memcpy(&section, data.data() + sections_offset + i * sizeof(section), sizeof(section));
            if (address >= section.VirtualAddress) {
                std::uint64_t delta = static_cast<std::uint64_t>(address) - section.VirtualAddress;
                if (delta <= section.SizeOfRawData && n <= section.SizeOfRawData - delta)
                    return at(static_cast<std::size_t>(section.PointerToRawData) + static_cast<std::size_t>(delta), n);
            }
        }
        return nullptr;
    };
    auto import_name = [&](DWORD addr) -> bool {
        std::wstring name;
        for (unsigned j = 0; j < 256; ++j) {
            if (addr > UINT32_MAX - j) return false;
            auto p = rva(addr + j, 1);
            if (!p) return false;
            if (!*p) {
                if (!valid_name(name) || lower(fs::path(name).extension().native()) != L".dll") return false;
                names.push_back(lower(name)); return true;
            }
            if (*p < 33 || *p > 126) return false;
            name.push_back(static_cast<wchar_t>(*p));
        }
        return false;
    };
    for (unsigned kind : {IMAGE_DIRECTORY_ENTRY_IMPORT, IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT}) {
        if (kind >= count) continue;
        std::size_t off = dirs + kind * sizeof(IMAGE_DATA_DIRECTORY);
        if (off + sizeof(IMAGE_DATA_DIRECTORY) > header.SizeOfOptionalHeader) return false;
        IMAGE_DATA_DIRECTORY dir{}; std::memcpy(&dir, optional + off, sizeof(dir));
        if (!dir.VirtualAddress && !dir.Size) continue;
        const std::size_t stride = kind == IMAGE_DIRECTORY_ENTRY_IMPORT ? 20 : 32;
        if (!dir.VirtualAddress || dir.Size < stride || dir.Size > 65536) return false;
        bool terminated = false;
        for (std::size_t i = 0; i + stride <= dir.Size; i += stride) {
            if (Clock::now() >= end || dir.VirtualAddress > UINT32_MAX - i) return false;
            auto p = rva(dir.VirtualAddress + static_cast<DWORD>(i), stride);
            if (!p) return false;
            bool zero = true; for (std::size_t j = 0; j < stride; ++j) zero = zero && p[j] == 0;
            if (zero) { terminated = true; break; }
            DWORD name = 0;
            if (kind == IMAGE_DIRECTORY_ENTRY_IMPORT) std::memcpy(&name, p + 12, 4);
            else { DWORD attr; std::memcpy(&attr, p, 4); if (!(attr & 1)) return false; std::memcpy(&name, p + 4, 4); }
            if (!import_name(name)) return false;
        }
        if (!terminated) return false;
    }
    return true;
}
bool system_library(const std::wstring& name) {
    if (name.rfind(L"api-ms-win-", 0) == 0 || name.rfind(L"ext-ms-win-", 0) == 0) return true;
    std::vector<wchar_t> path(32768);
    UINT n = GetSystemDirectoryW(path.data(), static_cast<UINT>(path.size()));
    if (!n || n >= path.size()) return false;
    auto file = fs::path(path.data()) / name;
    DWORD attr = GetFileAttributesW(file.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT));
}
bool runtime(const fs::path& exe, const fs::path& dest, Budget& budget, SECURITY_ATTRIBUTES* security, std::string& error) {
    std::vector<Handle> locks; fs::path parent;
    if (!lock_path(exe.parent_path(), locks, parent) || !valid_name(exe.filename().native())) { error = "Unsafe renderer path"; return false; }
    const auto target = dest / exe.filename();
    if (!copy_file(parent / exe.filename(), target, budget, security, error)) return false;
    std::vector<fs::path> queue{target};
    std::set<std::wstring> copied;
    // Qt may load its platform plugin dynamically, so it is not present in the
    // PE import table. Copy only the two named platform implementations; scan
    // their imports through the same closure as the executable.
    bool plugin_dir_created = false;
    for (const wchar_t* plugin : {L"qoffscreen.dll", L"qwindows.dll"}) {
        for (const fs::path& relative : {fs::path(L"platforms"), fs::path(L"plugins") / L"platforms"}) {
            auto source_dir = parent / relative;
            auto source = source_dir / plugin;
            DWORD attr = GetFileAttributesW(source.c_str());
            if (attr == INVALID_FILE_ATTRIBUTES) {
                DWORD code = GetLastError();
                if (code == ERROR_FILE_NOT_FOUND || code == ERROR_PATH_NOT_FOUND) continue;
                error = "Cannot inspect renderer plugin"; return false;
            }
            std::vector<Handle> plugin_locks; fs::path locked;
            if (!lock_path(source_dir, plugin_locks, locked) ||
                (attr & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
                error = "Unsafe renderer plugin"; return false;
            }
            if (!plugin_dir_created) {
                if (!CreateDirectoryW((dest / L"platforms").c_str(), security)) {
                    error = "Cannot stage renderer plugins"; return false;
                }
                plugin_dir_created = true;
            }
            auto staged = dest / L"platforms" / plugin;
            if (!copy_file(locked / plugin, staged, budget, security, error)) return false;
            queue.push_back(staged);
            break;
        }
    }
    for (std::size_t i = 0; i < queue.size(); ++i) {
        std::vector<std::wstring> names;
        if (!imports(queue[i], names, budget.end)) { error = "Renderer has invalid or unsupported PE dependencies"; return false; }
        for (const auto& name : names) {
            if (system_library(name) || !copied.insert(name).second) continue;
            if (!copy_file(parent / name, dest / name, budget, security, error)) { error = "Cannot stage renderer dependency"; return false; }
            queue.push_back(dest / name);
        }
    }
    // OpenSCAD's published Windows package places these resources beside the
    // executable. Only named resource trees are copied, with the same no-link,
    // byte/count and deadline checks as the project.
    for (const wchar_t* name : {L"libraries", L"fonts", L"color-schemes", L"locale"}) {
        auto source = parent / name;
        DWORD attr = GetFileAttributesW(source.c_str());
        if (attr == INVALID_FILE_ATTRIBUTES) {
            if (GetLastError() == ERROR_FILE_NOT_FOUND || GetLastError() == ERROR_PATH_NOT_FOUND) continue;
            error = "Cannot inspect renderer resources"; return false;
        }
        if (!(attr & FILE_ATTRIBUTE_DIRECTORY) || (attr & FILE_ATTRIBUTE_REPARSE_POINT) ||
            !CreateDirectoryW((dest / name).c_str(), security) ||
            !copy_tree(source, dest / name, budget, security, L"", error)) return false;
    }
    return true;
}
std::wstring quoteWindowsArgument(const std::wstring& arg) {
    std::wstring out = L"\"";
    std::size_t slashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') { ++slashes; continue; }
        if (c == L'"') { out.append(slashes * 2 + 1, L'\\'); out += c; }
        else { out.append(slashes, L'\\'); out += c; }
        slashes = 0;
    }
    out.append(slashes * 2, L'\\'); out += L'"'; return out;
}
struct Attributes {
    std::vector<unsigned char> storage;
    LPPROC_THREAD_ATTRIBUTE_LIST list = nullptr;
    ~Attributes() { if (list) DeleteProcThreadAttributeList(list); }
    bool create(SECURITY_CAPABILITIES& caps, HANDLE* handles, std::size_t count) {
        SIZE_T n = 0;
        InitializeProcThreadAttributeList(nullptr, 2, 0, &n);
        if (!n) return false;
        storage.resize(n); list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage.data());
        if (!InitializeProcThreadAttributeList(list, 2, 0, &n)) { list = nullptr; return false; }
        return UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, &caps, sizeof(caps), nullptr, nullptr) &&
            UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, count * sizeof(HANDLE), nullptr, nullptr);
    }
};
Result execute(const Request& request, const fs::path& exe, const fs::path& project,
               PSID sid, Clock::time_point end) {
    SECURITY_ATTRIBUTES inheritable{sizeof(inheritable), nullptr, TRUE};
    HANDLE read_raw = nullptr, write_raw = nullptr;
    if (!CreatePipe(&read_raw, &write_raw, &inheritable, 65536)) return fail(Status::sandbox_unavailable, "Cannot create sandbox diagnostics pipe");
    Handle read_pipe(read_raw), write_pipe(write_raw);
    if (!SetHandleInformation(read_pipe.value, HANDLE_FLAG_INHERIT, 0)) return fail(Status::sandbox_unavailable, "Cannot protect diagnostics pipe");
    Handle null_input(CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &inheritable, OPEN_EXISTING, 0, nullptr));
    if (!null_input) return fail(Status::sandbox_unavailable, "Cannot create sandbox stdin");
    Handle job(CreateJobObjectW(nullptr, nullptr));
    if (!job) return fail(Status::sandbox_unavailable, "Cannot create sandbox job");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
    limits.BasicLimitInformation.ActiveProcessLimit = 1;
    limits.JobMemoryLimit = static_cast<SIZE_T>(2ULL * 1024 * 1024 * 1024);
    if (!SetInformationJobObject(job.value, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) return fail(Status::sandbox_unavailable, "Cannot enforce sandbox job limits");
    SECURITY_CAPABILITIES capabilities{}; capabilities.AppContainerSid = sid;
    HANDLE handles[] = {write_pipe.value, null_input.value};
    Attributes attributes;
    if (!attributes.create(capabilities, handles, 2)) return fail(Status::sandbox_unavailable, "Cannot configure AppContainer confinement");
    std::wstring command = quoteWindowsArgument(exe.native());
    for (const auto& arg : request.arguments) {
        auto w = wide(arg);
        if ((!arg.empty() && w.empty()) || command.size() + w.size() * 2 + 4 > 32767) return fail(Status::invalid_request, "Invalid or oversized renderer argument");
        command += L' '; command += quoteWindowsArgument(w);
    }
    // Environment is explicit. No user profile, proxy, credentials or PATH.
    std::wstring env = L"HOME=" + project.native(); env.push_back(L'\0');
    env += L"QT_QPA_PLATFORM=offscreen"; env.push_back(L'\0');
    std::vector<wchar_t> windows_dir(32768);
    UINT windows_size = GetWindowsDirectoryW(windows_dir.data(), static_cast<UINT>(windows_dir.size()));
    if (!windows_size || windows_size >= windows_dir.size()) return fail(Status::sandbox_unavailable, "Cannot determine Windows runtime directory");
    env += L"SystemRoot="; env += windows_dir.data(); env.push_back(L'\0');
    env += L"TEMP=" + project.native(); env.push_back(L'\0');
    env += L"TMP=" + project.native(); env.push_back(L'\0');
    // CreateProcess fails with ERROR_ENVVAR_NOT_FOUND for an AppContainer when this is
    // absent: it rebases LOCALAPPDATA, TEMP and TMP to <LOCALAPPDATA>\Packages\<profile>\AC.
    // Rooting it in the project keeps the user's profile path out of the child; the
    // rebased directories do not exist there, so the renderer gains nothing writable.
    env += L"LOCALAPPDATA=" + project.native(); env.push_back(L'\0');
    env.push_back(L'\0');
    STARTUPINFOEXW startup{}; startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = null_input.value;
    startup.StartupInfo.hStdOutput = write_pipe.value; startup.StartupInfo.hStdError = write_pipe.value;
    startup.lpAttributeList = attributes.list;
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, TRUE,
                        CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW,
                        env.data(), project.c_str(), &startup.StartupInfo, &pi))
        return fail(Status::sandbox_unavailable, "Cannot start confined AppContainer renderer (Windows error " + std::to_string(GetLastError()) + ")");
    Handle process(pi.hProcess), thread(pi.hThread);
    write_pipe.reset(); null_input.reset();
    if (!AssignProcessToJobObject(job.value, process.value) || ResumeThread(thread.value) == static_cast<DWORD>(-1)) {
        TerminateProcess(process.value, 1); WaitForSingleObject(process.value, INFINITE);
        return fail(Status::sandbox_unavailable, "Cannot attach renderer to restricted job");
    }
    thread.reset();
    std::string diagnostics;
    bool timeout = false, limit = false, wait_error = false;
    auto drain = [&]() {
        for (unsigned i = 0; i < 16; ++i) {
            DWORD available = 0;
            if (!PeekNamedPipe(read_pipe.value, nullptr, 0, nullptr, &available, nullptr) || !available) break;
            char bytes[4096]; DWORD n = 0;
            if (!ReadFile(read_pipe.value, bytes, std::min<DWORD>(available, sizeof(bytes)), &n, nullptr) || !n) break;
            diagnostics.append(bytes, std::min<std::size_t>(n, request.max_diagnostic_bytes - diagnostics.size()));
        }
    };
    for (;;) {
        drain();
        DWORD wait = WaitForSingleObject(process.value, 10);
        if (wait == WAIT_OBJECT_0) break;
        if (wait != WAIT_TIMEOUT) { wait_error = true; break; }
        if (Clock::now() >= end) { timeout = true; break; }
        if (!request.output_file.empty()) {
            WIN32_FILE_ATTRIBUTE_DATA info{};
            if (!GetFileAttributesExW((project / wide(request.output_file)).c_str(), GetFileExInfoStandard, &info) ||
                (info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY))) { limit = true; break; }
            const std::uint64_t size = (static_cast<std::uint64_t>(info.nFileSizeHigh) << 32) | info.nFileSizeLow;
            if (size > request.max_output_bytes) { limit = true; break; }
        }
    }
    // Close the job even after a successful parent exit: no descendant survives.
    if (timeout || limit || wait_error) TerminateJobObject(job.value, 1);
    DWORD exit = 0;
    if (timeout || limit || wait_error) WaitForSingleObject(process.value, INFINITE);
    if (!GetExitCodeProcess(process.value, &exit)) wait_error = true;
    job.reset(); drain();
    if (timeout) return {Status::timed_out, -1, std::move(diagnostics)};
    if (limit) return {Status::resource_limit, -1, std::move(diagnostics)};
    if (wait_error) return {Status::sandbox_unavailable, -1, std::move(diagnostics)};
    if (exit) return {Status::model_failure, static_cast<int>(exit), std::move(diagnostics)};
    return {Status::success, 0, std::move(diagnostics)};
}

bool publish(const fs::path& source, const fs::path& work, const std::wstring& name,
             std::size_t maximum, Clock::time_point end, std::string& error) {
    Handle in(CreateFileW(source.c_str(), GENERIC_READ | FILE_READ_ATTRIBUTES, FILE_SHARE_READ,
                          nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
    std::uint64_t size = 0;
    if (!in || !regular(in.value, &size) || !size || size > maximum) { error = "Renderer did not produce a bounded regular output"; return false; }
    fs::path temp;
    Handle out;
    for (unsigned i = 0; i < 16; ++i) {
        auto name = random_name(); if (name.empty()) break;
        temp = work / name;
        out.reset(CreateFileW(temp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr));
        if (out || GetLastError() != ERROR_FILE_EXISTS) break;
    }
    if (!out) { error = "Cannot create atomic output"; return false; }
    bool ok = true; std::size_t total = 0; std::array<char, 32768> buffer{};
    while (ok) {
        if (Clock::now() >= end) { error = "SCAD output publication exceeded its deadline"; ok = false; break; }
        DWORD n = 0;
        if (!ReadFile(in.value, buffer.data(), static_cast<DWORD>(buffer.size()), &n, nullptr)) { ok = false; break; }
        if (!n) break;
        if (n > maximum - total) { ok = false; break; }
        total += n;
        DWORD offset = 0;
        while (offset < n) {
            DWORD written = 0;
            if (!WriteFile(out.value, buffer.data() + offset, n - offset, &written, nullptr) || !written) { ok = false; break; }
            offset += written;
        }
    }
    if (ok && !FlushFileBuffers(out.value)) ok = false;
    out.reset();
    if (ok && Clock::now() >= end) { error = "SCAD output publication exceeded its deadline"; ok = false; }
    if (ok && !MoveFileExW(temp.c_str(), (work / name).c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) ok = false;
    if (!ok) { DeleteFileW(temp.c_str()); if (error.empty()) error = "Cannot atomically publish SCAD output"; }
    return ok;
}
fs::path executable(const std::string& name) {
    const auto w = wide(name);
    if (w.empty()) return {};
    fs::path p(w);
    if (p.is_absolute()) {
        DWORD attrs = GetFileAttributesW(p.c_str());
        return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ? p : fs::path{};
    }
    if (p.has_parent_path()) return {};
    // Never consult relative or empty PATH components.
    DWORD size = GetEnvironmentVariableW(L"PATH", nullptr, 0);
    if (!size || size > 32768) return {};
    std::wstring paths(size, L'\0');
    if (!GetEnvironmentVariableW(L"PATH", paths.data(), size)) return {};
    paths.resize(wcslen(paths.c_str()));
    std::size_t pos = 0;
    while (pos <= paths.size()) {
        auto end = paths.find(L';', pos); if (end == std::wstring::npos) end = paths.size();
        fs::path dir(paths.substr(pos, end - pos));
        if (dir.is_absolute()) {
            auto candidate = dir / p;
            DWORD attrs = GetFileAttributesW(candidate.c_str());
            if (attrs != INVALID_FILE_ATTRIBUTES && !(attrs & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) return candidate;
        }
        pos = end + 1;
    }
    return {};
}
} // namespace

Result run_windows(const Request& request) {
    try {
        if (request.deadline.count() <= 0 || request.deadline > std::chrono::hours(1) ||
            !request.max_diagnostic_bytes || request.max_diagnostic_bytes > kDiagnostics ||
            !request.max_output_bytes || request.max_output_bytes > kArtifact || request.work_dir.empty() ||
            nul(request.work_dir) || nul(request.executable) || nul(request.output_file) ||
            std::any_of(request.arguments.begin(), request.arguments.end(), nul) ||
            !request.renderer_resource_roots.empty())
            return fail(Status::invalid_request, "Invalid SCAD limits, arguments or resource roots");
        const auto output = wide(request.output_file);
        if (!request.output_file.empty() && (output.empty() || !valid_name(output))) return fail(Status::invalid_request, "Unsafe SCAD output filename");
        const auto end = Clock::now() + request.deadline;
        const auto program = executable(request.executable);
        if (program.empty()) return fail(Status::dependency_unavailable, "OpenSCAD executable not found; install OpenSCAD or configure CHUDWARE_OPENSCAD_BIN");
        std::vector<Handle> work_locks; fs::path work;
        const auto work_w = wide(request.work_dir);
        if (work_w.empty() || !lock_path(fs::path(work_w), work_locks, work)) return fail(Status::invalid_request, "SCAD work directory is unsafe or inaccessible");
        Profile profile;
        if (!profile.create()) return fail(Status::sandbox_unavailable, "Cannot create per-invocation AppContainer profile");
        auto user = token_sid(); Security readonly, writable;
        if (user.empty() || !restrict_profile(profile, user, end))
            return fail(Clock::now() >= end ? Status::timed_out : Status::sandbox_unavailable,
                        "Cannot restrict AppContainer profile storage");
        if (user.empty() || !readonly.create(user, profile.sid, false) || !writable.create(user, profile.sid, true))
            return fail(Status::sandbox_unavailable, "Cannot create private AppContainer ACLs");
        Temp temp;
        if (!temp.create(&readonly.attributes)) return fail(Status::sandbox_unavailable, "Cannot create private AppContainer staging");
        std::vector<Handle> staging_locks; fs::path staged_absolute;
        if (!lock_path(temp.path, staging_locks, staged_absolute))
            return fail(Status::sandbox_unavailable, "Cannot protect private SCAD staging path");
        const auto project = temp.path / L"project", bin = temp.path / L"runtime";
        if (!CreateDirectoryW(project.c_str(), &readonly.attributes) || !CreateDirectoryW(bin.c_str(), &readonly.attributes))
            return fail(Status::sandbox_unavailable, "Cannot create SCAD staging directories");
        Budget budget{0, 0, end}; std::string error;
        if (!copy_tree(work, project, budget, &readonly.attributes, output, error) ||
            !runtime(program, bin, budget, &readonly.attributes, error))
            return fail(budget.expired() ? Status::timed_out : Status::invalid_request, std::move(error));
        if (!output.empty()) {
            Handle file(CreateFileW((project / output).c_str(), GENERIC_WRITE, 0, &writable.attributes, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr));
            if (!file) return fail(Status::sandbox_unavailable, "Cannot prepare confined output file");
        }
        if (budget.expired()) return fail(Status::timed_out, "SCAD staging exceeded its deadline");
        Result result = execute(request, bin / program.filename(), project, profile.sid, end);
        if (result.status == Status::success && !output.empty() &&
            !publish(project / output, work, output, request.max_output_bytes, end, error)) {
            result.status = Clock::now() >= end ? Status::timed_out : Status::resource_limit;
            if (result.diagnostics.size() < request.max_diagnostic_bytes) {
                result.diagnostics += "\n" + error;
                if (result.diagnostics.size() > request.max_diagnostic_bytes) result.diagnostics.resize(request.max_diagnostic_bytes);
            }
        }
        return result;
    } catch (const fs::filesystem_error&) {
        return fail(Status::invalid_request, "Invalid SCAD filesystem path");
    }
}
Result capability_windows(const std::string& executable_name) {
    const auto user = token_sid();
    if (user.empty()) return fail(Status::sandbox_unavailable, "Cannot identify SCAD sandbox owner");
    // This directory is an empty caller workspace. run_windows still creates a
    // separate per-invocation AppContainer staging tree for the version probe.
    Local descriptor; PSECURITY_DESCRIPTOR d = nullptr;
    const std::wstring sddl = L"O:" + user + L"D:P(A;OICI;FA;;;" + user + L")(A;OICI;FA;;;SY)";
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &d, nullptr)) return fail(Status::sandbox_unavailable, "Cannot create SCAD capability workspace ACL");
    descriptor.p = d; SECURITY_ATTRIBUTES security{sizeof(security), d, FALSE};
    Temp temp;
    if (!temp.create(&security)) return fail(Status::sandbox_unavailable, "Cannot create SCAD capability workspace");
    int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, temp.path.c_str(), -1, nullptr, 0, nullptr, nullptr);
    if (!n) return fail(Status::sandbox_unavailable, "Cannot encode SCAD capability workspace");
    std::string path(static_cast<std::size_t>(n), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, temp.path.c_str(), -1, path.data(), n, nullptr, nullptr)) return fail(Status::sandbox_unavailable, "Cannot encode SCAD capability workspace");
    path.pop_back();
    Request request; request.executable = executable_name; request.work_dir = std::move(path);
    request.arguments = {"--version"}; request.deadline = std::chrono::seconds(10);
    return run_windows(request);
}
} // namespace chudware::util::scad_process::detail
#endif
