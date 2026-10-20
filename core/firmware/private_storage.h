#pragma once

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstdio>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <aclapi.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace chudware::firmware {
namespace private_storage_detail {
namespace fs = std::filesystem;

[[noreturn]] inline void refuse(const fs::path& path) {
    throw std::runtime_error("unsafe or inaccessible firmware storage: " + path.string());
}

inline fs::path absolute_path(const fs::path& path) {
    if (path.empty()) refuse(path);
    // Do not normalize away symlinks followed by '..'. Every supplied component
    // must be checked, and parent traversal is not a firmware storage path.
    for (const auto& component : path)
        if (component == "..") refuse(path);
    return fs::absolute(path).lexically_normal();
}

#ifdef _WIN32
struct Handle {
    HANDLE value = INVALID_HANDLE_VALUE;
    explicit Handle(HANDLE h = INVALID_HANDLE_VALUE) : value(h) {}
    ~Handle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    Handle(Handle&& other) noexcept : value(other.value) { other.value = INVALID_HANDLE_VALUE; }
};

struct PrivateSecurity {
    std::vector<unsigned char> user;
    PACL acl = nullptr;
    SECURITY_DESCRIPTOR descriptor{};
    SECURITY_ATTRIBUTES attributes{};
    PrivateSecurity() {
        HANDLE raw = nullptr;
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw)) refuse("process token");
        Handle token(raw);
        DWORD size = 0;
        GetTokenInformation(raw, TokenUser, nullptr, 0, &size);
        user.resize(size);
        if (!GetTokenInformation(raw, TokenUser, user.data(), size, &size)) refuse("process token");
        EXPLICIT_ACCESSW entry{};
        entry.grfAccessPermissions = GENERIC_ALL;
        entry.grfAccessMode = SET_ACCESS;
        entry.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT;
        entry.Trustee.TrusteeForm = TRUSTEE_IS_SID;
        entry.Trustee.TrusteeType = TRUSTEE_IS_USER;
        entry.Trustee.ptstrName = reinterpret_cast<LPWSTR>(sid());
        if (SetEntriesInAclW(1, &entry, nullptr, &acl) != ERROR_SUCCESS) refuse("private ACL");
        // Elevated tokens otherwise default to Administrators, not TokenUser.
        if (!InitializeSecurityDescriptor(&descriptor, SECURITY_DESCRIPTOR_REVISION) ||
            !SetSecurityDescriptorOwner(&descriptor, sid(), FALSE) ||
            !SetSecurityDescriptorDacl(&descriptor, TRUE, acl, FALSE) ||
            !SetSecurityDescriptorControl(&descriptor, SE_DACL_PROTECTED, SE_DACL_PROTECTED)) {
            LocalFree(acl);
            acl = nullptr;
            refuse("private ACL");
        }
        attributes = {sizeof(SECURITY_ATTRIBUTES), &descriptor, FALSE};
    }
    ~PrivateSecurity() { if (acl) LocalFree(acl); }
    PSID sid() const { return reinterpret_cast<const TOKEN_USER*>(user.data())->User.Sid; }
};

inline bool trusted_system_sid(PSID sid) {
    return IsWellKnownSid(sid, WinLocalSystemSid) ||
           IsWellKnownSid(sid, WinBuiltinAdministratorsSid);
}

inline void validate_handle(HANDLE handle, const fs::path& path, bool directory, bool private_leaf) {
    BY_HANDLE_FILE_INFORMATION info{};
    if (!GetFileInformationByHandle(handle, &info) ||
        (info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) ||
        bool(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != directory ||
        (!directory && info.nNumberOfLinks != 1)) refuse(path);
    PrivateSecurity security;
    PSID owner = nullptr;
    PACL acl = nullptr;
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    if (GetSecurityInfo(handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                        &owner, nullptr, &acl, nullptr, &descriptor) != ERROR_SUCCESS) refuse(path);
    bool safe = owner && (EqualSid(owner, security.sid()) ||
                         (!private_leaf && trusted_system_sid(owner))) && acl;
    const DWORD writable = GENERIC_WRITE | GENERIC_ALL | WRITE_DAC | WRITE_OWNER | DELETE |
                           FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES |
                           FILE_DELETE_CHILD;
    for (DWORD i = 0; safe && i < acl->AceCount; ++i) {
        void* raw = nullptr;
        if (!GetAce(acl, i, &raw)) { safe = false; break; }
        auto* header = static_cast<ACE_HEADER*>(raw);
        if (header->AceFlags & INHERIT_ONLY_ACE) continue;
        if (header->AceType == ACCESS_DENIED_ACE_TYPE) continue;
        if (header->AceType != ACCESS_ALLOWED_ACE_TYPE) { safe = false; break; }
        auto* ace = static_cast<ACCESS_ALLOWED_ACE*>(raw);
        PSID trustee = &ace->SidStart;
        if ((ace->Mask & writable) && !EqualSid(trustee, security.sid()) &&
            !trusted_system_sid(trustee)) safe = false;
    }
    if (descriptor) LocalFree(descriptor);
    if (!safe) refuse(path);
}

// Keep every ancestor handle open without FILE_SHARE_DELETE while using a
// pathname: Windows cannot swap a checked ancestor for a junction mid-operation.
inline std::vector<Handle> open_directory(const fs::path& path, bool create) {
    std::vector<Handle> handles;
    const fs::path absolute = absolute_path(path);
    fs::path current = absolute.root_path();
    PrivateSecurity security;
    Handle root(CreateFileW(current.c_str(), FILE_READ_ATTRIBUTES,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    BY_HANDLE_FILE_INFORMATION root_info{};
    if (root.value == INVALID_HANDLE_VALUE || !GetFileInformationByHandle(root.value, &root_info) ||
        !(root_info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
        (root_info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) refuse(current);
    handles.push_back(std::move(root));
    for (const auto& part : absolute.relative_path()) {
        if (part.empty() || part == ".") continue;
        current /= part;
        if (create && !CreateDirectoryW(current.c_str(), &security.attributes) &&
            GetLastError() != ERROR_ALREADY_EXISTS) refuse(current);
        Handle handle(CreateFileW(current.c_str(), FILE_READ_ATTRIBUTES | READ_CONTROL |
                                  (create && current == absolute ? WRITE_DAC : 0),
                                  FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                                  FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
        if (handle.value == INVALID_HANDLE_VALUE) refuse(current);
        validate_handle(handle.value, current, true, current == absolute);
        if (create && current == absolute &&
            SetSecurityInfo(handle.value, SE_FILE_OBJECT,
                            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                            nullptr, nullptr, security.acl, nullptr) != ERROR_SUCCESS) refuse(current);
        handles.push_back(std::move(handle));
    }
    if (handles.size() == 1) refuse(path);
    return handles;
}
#else
struct Descriptor {
    int value = -1;
    explicit Descriptor(int fd = -1) : value(fd) {}
    ~Descriptor() { if (value >= 0) ::close(value); }
    Descriptor(const Descriptor&) = delete;
    Descriptor& operator=(const Descriptor&) = delete;
    Descriptor(Descriptor&& other) noexcept : value(other.value) { other.value = -1; }
    Descriptor& operator=(Descriptor&& other) noexcept {
        if (value >= 0) ::close(value);
        value = other.value;
        other.value = -1;
        return *this;
    }
};

inline void validate_stat(const struct stat& st, const fs::path& path, bool directory,
                          bool private_leaf) {
    if ((directory ? !S_ISDIR(st.st_mode) : !S_ISREG(st.st_mode)) ||
        (st.st_uid != ::geteuid() && (private_leaf || st.st_uid != 0)) ||
        (!directory && st.st_nlink != 1)) refuse(path);
    // A root-owned sticky ancestor (e.g. /tmp in explicit test overrides) cannot
    // replace another user's private child. It is never our default data root.
    if ((st.st_mode & 0022) &&
        !(directory && !private_leaf && st.st_uid == 0 && (st.st_mode & S_ISVTX))) refuse(path);
}

inline Descriptor open_directory(const fs::path& path, bool create) {
    const fs::path absolute = absolute_path(path);
    Descriptor current(::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (current.value < 0) refuse(path);
    struct stat root_stat{};
    if (::fstat(current.value, &root_stat) != 0) refuse(path);
    validate_stat(root_stat, "/", true, false);
    bool visited = false;
    for (const auto& part : absolute.relative_path()) {
        if (part.empty() || part == ".") continue;
        visited = true;
        if (create && ::mkdirat(current.value, part.c_str(), 0700) != 0 && errno != EEXIST)
            refuse(path);
        Descriptor next(::openat(current.value, part.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
        if (next.value < 0) refuse(path);
        struct stat st{};
        if (::fstat(next.value, &st) != 0) refuse(path);
        // All ancestors must be trusted; the returned directory is user-owned.
        validate_stat(st, path, true, false);
        current = std::move(next);
    }
    if (!visited) refuse(path);
    struct stat st{};
    if (::fstat(current.value, &st) != 0) refuse(path);
    validate_stat(st, path, true, true);
    if (create && ::fchmod(current.value, 0700) != 0) refuse(path);
    return current;
}
#endif

inline std::string random_token() {
    std::array<unsigned char, 16> bytes{};
#ifdef _WIN32
    using RandomFunction = BOOLEAN (WINAPI*)(void*, ULONG);
    auto random = reinterpret_cast<RandomFunction>(GetProcAddress(GetModuleHandleW(L"advapi32.dll"), "SystemFunction036"));
    if (!random || !random(bytes.data(), static_cast<ULONG>(bytes.size()))) refuse("random source");
#else
    Descriptor random(::open("/dev/urandom", O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (random.value < 0) refuse("random source");
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const auto count = ::read(random.value, bytes.data() + offset, bytes.size() - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) refuse("random source");
        offset += static_cast<std::size_t>(count);
    }
#endif
    static constexpr char hex[] = "0123456789abcdef";
    std::string result;
    result.reserve(bytes.size() * 2);
    for (unsigned char byte : bytes) { result += hex[byte >> 4]; result += hex[byte & 15]; }
    return result;
}

inline fs::path data_root() {
    if (const char* override_dir = std::getenv("CHUDWARE_FIRMWARE_DIR"); override_dir && *override_dir)
        return absolute_path(override_dir);
#ifdef _WIN32
    const char* base = std::getenv("APPDATA");
    if (!base || !*base) refuse("APPDATA is not set");
    fs::path root(base);
#else
    fs::path root;
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && *xdg) {
        root = xdg;
    } else {
        const char* home = std::getenv("HOME");
        if (!home || !*home) refuse("HOME is not set");
        root = fs::path(home) / ".local" / "share";
    }
#endif
    if (!root.is_absolute()) refuse(root);
    return root / "Chudware" / "firmware";
}

inline bool read_file(const fs::path& path, std::string& contents,
                      std::size_t max_bytes = 64 * 1024 * 1024) {
    auto parent = open_directory(path.parent_path(), false);
    contents.clear();
#ifdef _WIN32
    Handle file(CreateFileW(path.c_str(), GENERIC_READ | READ_CONTROL, FILE_SHARE_READ, nullptr,
                            OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (file.value == INVALID_HANDLE_VALUE) {
        if (GetLastError() == ERROR_FILE_NOT_FOUND) return false;
        refuse(path);
    }
    validate_handle(file.value, path, false, true);
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file.value, &size) || size.QuadPart < 0 ||
        static_cast<unsigned long long>(size.QuadPart) > max_bytes) refuse(path);
    std::array<char, 8192> buffer{};
    DWORD count = 0;
    do {
        if (!ReadFile(file.value, buffer.data(), static_cast<DWORD>(buffer.size()), &count, nullptr)) refuse(path);
        if (count > max_bytes - contents.size()) refuse(path);
        contents.append(buffer.data(), count);
    } while (count);
#else
    Descriptor file(::openat(parent.value, path.filename().c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
    if (file.value < 0) { if (errno == ENOENT) return false; refuse(path); }
    struct stat st{};
    if (::fstat(file.value, &st) != 0) refuse(path);
    validate_stat(st, path, false, true);
    if (st.st_size < 0 || static_cast<std::uintmax_t>(st.st_size) > max_bytes) refuse(path);
    std::array<char, 8192> buffer{};
    for (;;) {
        const auto count = ::read(file.value, buffer.data(), buffer.size());
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) refuse(path);
        if (count == 0) break;
        if (static_cast<std::size_t>(count) > max_bytes - contents.size()) refuse(path);
        contents.append(buffer.data(), static_cast<std::size_t>(count));
    }
#endif
    return true;
}

inline std::string read_file(const fs::path& path, std::size_t max_bytes = 64 * 1024 * 1024) {
    std::string contents;
    if (!read_file(path, contents, max_bytes)) refuse(path);
    return contents;
}

inline void atomic_write(const fs::path& path, const std::string& contents) {
    auto parent = open_directory(path.parent_path(), true);
    const std::string name = path.filename().string();
    if (name.empty() || name == "." || name == "..") refuse(path);
    const std::string temporary = ".firmware-" + random_token() + ".tmp";
#ifdef _WIN32
    // Validate existing destinations, then close the leaf handle before publishing:
    // MoveFileExW cannot replace an open target, even with FILE_SHARE_DELETE.
    // The checked ancestor handles stay open throughout to prevent path swaps.
    {
        Handle destination(CreateFileW(path.c_str(), FILE_READ_ATTRIBUTES | READ_CONTROL,
                                       FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                                       OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS, nullptr));
        if (destination.value != INVALID_HANDLE_VALUE) validate_handle(destination.value, path, false, true);
        else if (GetLastError() != ERROR_FILE_NOT_FOUND) refuse(path);
    }
    PrivateSecurity security;
    const fs::path staging = path.parent_path() / temporary;
    Handle file(CreateFileW(staging.c_str(), GENERIC_WRITE, 0, &security.attributes,
                            CREATE_NEW, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (file.value == INVALID_HANDLE_VALUE) refuse(staging);
    try {
        std::size_t offset = 0;
        while (offset < contents.size()) {
            const DWORD requested = static_cast<DWORD>((std::min)(contents.size() - offset, std::size_t(1 << 20)));
            DWORD written = 0;
            if (!WriteFile(file.value, contents.data() + offset, requested, &written, nullptr) || !written) refuse(path);
            offset += written;
        }
        if (!FlushFileBuffers(file.value)) refuse(path);
        CloseHandle(file.value);
        file.value = INVALID_HANDLE_VALUE;
        if (!MoveFileExW(staging.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) refuse(path);
    } catch (...) {
        if (file.value != INVALID_HANDLE_VALUE) { CloseHandle(file.value); file.value = INVALID_HANDLE_VALUE; }
        DeleteFileW(staging.c_str());
        throw;
    }
#else
    struct stat st{};
    if (::fstatat(parent.value, name.c_str(), &st, AT_SYMLINK_NOFOLLOW) == 0)
        validate_stat(st, path, false, true);
    else if (errno != ENOENT) refuse(path);
    Descriptor file(::openat(parent.value, temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600));
    if (file.value < 0) refuse(path);
    try {
        std::size_t offset = 0;
        while (offset < contents.size()) {
            const auto count = ::write(file.value, contents.data() + offset, contents.size() - offset);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) refuse(path);
            offset += static_cast<std::size_t>(count);
        }
        if (::fsync(file.value) != 0) refuse(path);
        // renameat replaces the directory entry, and never follows a destination link.
        if (::renameat(parent.value, temporary.c_str(), parent.value, name.c_str()) != 0) refuse(path);
        if (::fsync(parent.value) != 0) refuse(path);
    } catch (...) {
        ::unlinkat(parent.value, temporary.c_str(), 0);
        throw;
    }
#endif
}

inline fs::path unique_directory(const fs::path& parent_path, const std::string& prefix) {
    auto parent = open_directory(parent_path, true);
    for (int attempt = 0; attempt < 100; ++attempt) {
        const std::string token = random_token();
        const std::string name = prefix.empty() ? token.substr(0, 12) : prefix + "-" + token;
#ifdef _WIN32
        PrivateSecurity security;
        if (CreateDirectoryW((parent_path / name).c_str(), &security.attributes)) return parent_path / name;
        if (GetLastError() != ERROR_ALREADY_EXISTS) refuse(parent_path);
#else
        if (::mkdirat(parent.value, name.c_str(), 0700) == 0) return parent_path / name;
        if (errno != EEXIST) refuse(parent_path);
#endif
    }
    refuse(parent_path);
}
}  // namespace private_storage_detail

inline std::filesystem::path private_directory(const std::filesystem::path& path) {
    const auto absolute = private_storage_detail::absolute_path(path);
    auto directory = private_storage_detail::open_directory(absolute, true);
    (void)directory;
    return absolute;
}

inline std::filesystem::path private_temp_directory(const std::string& prefix) {
    if (prefix.empty() || prefix.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") != std::string::npos)
        throw std::invalid_argument("invalid firmware temporary directory prefix");
    const auto root = private_directory(private_storage_detail::data_root());
    const auto parent_path = private_directory(root / "tmp");
    return private_storage_detail::unique_directory(parent_path, prefix);
}
}  // namespace chudware::firmware
