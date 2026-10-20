// Owner-private daemon state primitives. Never rely on the process umask.
#pragma once

#include <cstdio>
#include <cstdint>
#include <filesystem>
#include <string>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
#include <fcntl.h>
#include <io.h>
#include <vector>
#else
#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace chudware_ipc {
#ifdef _WIN32
// Protected DACL: this account and SYSTEM only, including inherited children.
class PrivateSecurity {
public:
    PrivateSecurity() {
        HANDLE token = nullptr;
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return;
        DWORD size = 0;
        GetTokenInformation(token, TokenUser, nullptr, 0, &size);
        user_.resize(size);
        const bool ok = size && GetTokenInformation(token, TokenUser, user_.data(), size, &size);
        CloseHandle(token);
        if (!ok) return;
        LPWSTR sid = nullptr;
        if (!ConvertSidToStringSidW(userSid(), &sid)) return;
        // Elevated tokens can default to Administrators as object owner. Keep
        // ownership bound to TokenUser, matching protect() and ordinary launches.
        const std::wstring sddl = L"O:" + std::wstring(sid) +
            L"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + sid + L")";
        LocalFree(sid);
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.c_str(), SDDL_REVISION_1, &descriptor_, nullptr)) return;
        attributes.nLength = sizeof attributes;
        attributes.lpSecurityDescriptor = descriptor_;
    }
    ~PrivateSecurity() { if (descriptor_) LocalFree(descriptor_); }
    bool valid() const { return descriptor_ != nullptr; }
    bool protect(HANDLE handle) const {
        PSID owner = nullptr;
        PSECURITY_DESCRIPTOR existing = nullptr;
        if (!valid() || GetSecurityInfo(handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION,
                                       &owner, nullptr, nullptr, nullptr, &existing) != ERROR_SUCCESS)
            return false;
        const bool ours = owner && EqualSid(owner, userSid());
        LocalFree(existing);
        if (!ours) return false;
        PACL acl = nullptr;
        BOOL present = FALSE, defaulted = FALSE;
        if (!GetSecurityDescriptorDacl(descriptor_, &present, &acl, &defaulted) || !present)
            return false;
        return SetSecurityInfo(handle, SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            nullptr, nullptr, acl, nullptr) == ERROR_SUCCESS;
    }
    SECURITY_ATTRIBUTES attributes{};
private:
    PSID userSid() const {
        return user_.empty() ? nullptr : reinterpret_cast<const TOKEN_USER*>(user_.data())->User.Sid;
    }
    std::vector<unsigned char> user_;
    PSECURITY_DESCRIPTOR descriptor_ = nullptr;
};

inline PrivateSecurity& privateSecurity() {
    static PrivateSecurity security;
    return security;
}
#endif

// Creates missing ancestors privately; tightens only the requested leaf, never
// the user's existing home/XDG ancestors. Rejects symlinks/reparse points.
inline bool privateDir(const std::string& path) {
    if (path.empty()) return false;
    const auto target = std::filesystem::u8path(path).lexically_normal();
#ifdef _WIN32
    auto& security = privateSecurity();
    if (!security.valid()) return false;
    std::filesystem::path current;
    for (const auto& part : target) {
        current /= part;
        if (current == current.root_name() || current == current.root_path()) continue;
        if (!CreateDirectoryW(current.c_str(), &security.attributes) &&
            GetLastError() != ERROR_ALREADY_EXISTS) return false;
        const DWORD attrs = GetFileAttributesW(current.c_str());
        if (attrs == INVALID_FILE_ATTRIBUTES || !(attrs & FILE_ATTRIBUTE_DIRECTORY) ||
            (attrs & FILE_ATTRIBUTE_REPARSE_POINT)) return false;
    }
    HANDLE dir = CreateFileW(target.c_str(), READ_CONTROL | WRITE_DAC,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (dir == INVALID_HANDLE_VALUE) return false;
    FILE_ATTRIBUTE_TAG_INFO info{};
    const bool ok = GetFileInformationByHandleEx(dir, FileAttributeTagInfo, &info, sizeof info) &&
        !(info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) && security.protect(dir);
    CloseHandle(dir);
    return ok;
#else
    int fd = ::open(target.is_absolute() ? "/" : ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return false;
    for (const auto& part : target.relative_path()) {
        const std::string name = part.string();
        if (name == ".") continue;
        if (name == ".." || (::mkdirat(fd, name.c_str(), 0700) != 0 && errno != EEXIST)) {
            ::close(fd);
            return false;
        }
        const int next = ::openat(fd, name.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        ::close(fd);
        if (next < 0) return false;
        fd = next;
    }
    struct stat st{};
    const bool ok = ::fstat(fd, &st) == 0 && st.st_uid == ::geteuid() &&
                    ::fchmod(fd, 0700) == 0;
    ::close(fd);
    return ok;
#endif
}

// Opens without following a leaf link, validates ownership/type BEFORE truncate,
// and establishes private permissions before any confidential bytes are written.
inline FILE* privateFile(const std::string& path, bool append = false, bool exclusive = false) {
#ifdef _WIN32
    auto& security = privateSecurity();
    if (!security.valid()) return nullptr;
    HANDLE handle = CreateFileW(std::filesystem::u8path(path).c_str(),
        GENERIC_WRITE | READ_CONTROL | WRITE_DAC, FILE_SHARE_READ, &security.attributes,
        exclusive ? CREATE_NEW : OPEN_ALWAYS, FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return nullptr;
    FILE_ATTRIBUTE_TAG_INFO info{};
    BY_HANDLE_FILE_INFORMATION fileInfo{};
    if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &info, sizeof info) ||
        (info.FileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)) ||
        !GetFileInformationByHandle(handle, &fileInfo) || fileInfo.nNumberOfLinks != 1 ||
        !security.protect(handle)) {
        CloseHandle(handle);
        return nullptr;
    }
    LARGE_INTEGER zero{};
    if (!SetFilePointerEx(handle, zero, nullptr, append ? FILE_END : FILE_BEGIN) ||
        (!append && !SetEndOfFile(handle))) {
        CloseHandle(handle);
        return nullptr;
    }
    const int fd = _open_osfhandle(reinterpret_cast<intptr_t>(handle), _O_BINARY | _O_WRONLY);
    if (fd < 0) { CloseHandle(handle); return nullptr; }
    FILE* stream = _fdopen(fd, append ? "ab" : "wb");
    if (!stream) _close(fd);
#else
    const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_NOFOLLOW | O_CLOEXEC |
                         (append ? O_APPEND : 0) | (exclusive ? O_EXCL : 0), 0600);
    if (fd < 0) return nullptr;
    struct stat st{};
    if (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_uid != ::geteuid() ||
        st.st_nlink != 1 || ::fchmod(fd, 0600) != 0 || (!append && ::ftruncate(fd, 0) != 0)) {
        ::close(fd);
        return nullptr;
    }
    FILE* stream = ::fdopen(fd, append ? "ab" : "wb");
    if (!stream) ::close(fd);
#endif
    return stream;
}

inline bool privateWrite(const std::string& path, const std::string& text) {
    FILE* stream = privateFile(path);
    if (!stream) return false;
    const bool written = std::fwrite(text.data(), 1, text.size(), stream) == text.size();
    return std::fclose(stream) == 0 && written;
}

inline bool privateCopy(const std::string& source, const std::string& destination) {
    FILE* input = std::fopen(source.c_str(), "rb");
    if (!input) return false;
    FILE* output = privateFile(destination, false, true);
    if (!output) { std::fclose(input); return false; }
    char buffer[16384];
    size_t size;
    bool ok = true;
    while ((size = std::fread(buffer, 1, sizeof buffer, input)) != 0)
        if (std::fwrite(buffer, 1, size, output) != size) { ok = false; break; }
    ok = ok && !std::ferror(input);
    std::fclose(input);
    ok = std::fclose(output) == 0 && ok;
    if (!ok) { std::error_code ec; std::filesystem::remove(destination, ec); }
    return ok;
}
}  // namespace chudware_ipc
