#if defined(_WIN32)

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#include <winreg.h>
#include <winternl.h>

#include "security/integrity.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <set>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace {

constexpr wchar_t kRegistryKey[] =
    L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{6B9E2E6B-6B7B-4B7B-9B7B-AICADSETUP01}_is1";
constexpr wchar_t kManifestName[] = L"payload-manifest.v1.json";
constexpr wchar_t kSignatureName[] = L"payload-manifest.v1.sig";

struct HandleCloser {
    void operator()(void* value) const noexcept {
        if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value);
    }
};
using UniqueHandle = std::unique_ptr<void, HandleCloser>;

UniqueHandle openPath(HANDLE parent, const std::wstring& component, DWORD access,
                      DWORD share, DWORD flags) {
    if (component.empty() || component == L"." || component == L".." ||
        component.find_first_of(L"\\/:") != std::wstring::npos)
        return {};
    const std::wstring path = component;
    UNICODE_STRING name{};
    name.Buffer = const_cast<PWSTR>(path.c_str());
    name.Length = static_cast<USHORT>(path.size() * sizeof(wchar_t));
    name.MaximumLength = name.Length;
    OBJECT_ATTRIBUTES attributes{};
    InitializeObjectAttributes(&attributes, &name, OBJ_CASE_INSENSITIVE, parent, nullptr);
    IO_STATUS_BLOCK status{};
    HANDLE result = nullptr;
    using NtCreateFileFn = NTSTATUS(NTAPI*)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES,
        PIO_STATUS_BLOCK, PLARGE_INTEGER, ULONG, ULONG, ULONG, ULONG, PVOID, ULONG);
    static const auto ntCreateFile = reinterpret_cast<NtCreateFileFn>(
        GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtCreateFile"));
    if (!ntCreateFile) return {};
    const ULONG options = FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT |
        (flags & FILE_FLAG_BACKUP_SEMANTICS ? FILE_DIRECTORY_FILE : FILE_NON_DIRECTORY_FILE);
    const NTSTATUS rc = ntCreateFile(&result, access | SYNCHRONIZE, &attributes, &status,
        nullptr, FILE_ATTRIBUTE_NORMAL, share, FILE_OPEN, options, nullptr, 0);
    return rc >= 0 ? UniqueHandle(result) : UniqueHandle{};
}

bool isPlain(HANDLE handle, bool directory) {
    FILE_ATTRIBUTE_TAG_INFO info{};
    return GetFileInformationByHandleEx(handle, FileAttributeTagInfo, &info, sizeof(info)) &&
        !(info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) &&
        (!!(info.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) == directory);
}

bool sameIdentity(HANDLE a, HANDLE b) {
    FILE_ID_INFO left{}, right{};
    return GetFileInformationByHandleEx(a, FileIdInfo, &left, sizeof(left)) &&
        GetFileInformationByHandleEx(b, FileIdInfo, &right, sizeof(right)) &&
        left.VolumeSerialNumber == right.VolumeSerialNumber &&
        std::equal(std::begin(left.FileId.Identifier), std::end(left.FileId.Identifier),
                   std::begin(right.FileId.Identifier));
}

std::wstring registeredRoot() {
    HKEY raw = nullptr;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, kRegistryKey, 0, KEY_QUERY_VALUE, &raw) != ERROR_SUCCESS)
        return {};
    std::unique_ptr<std::remove_pointer_t<HKEY>, decltype(&RegCloseKey)> key(raw, RegCloseKey);
    DWORD type = 0, bytes = 0;
    if (RegQueryValueExW(raw, L"InstallLocation", nullptr, &type, nullptr, &bytes) != ERROR_SUCCESS ||
        (type != REG_SZ && type != REG_EXPAND_SZ) || bytes < sizeof(wchar_t)) return {};
    std::wstring value(bytes / sizeof(wchar_t), L'\0');
    if (RegQueryValueExW(raw, L"InstallLocation", nullptr, &type,
                         reinterpret_cast<BYTE*>(value.data()), &bytes) != ERROR_SUCCESS) return {};
    value.resize(wcsnlen(value.c_str(), value.size()));
    return value;
}

UniqueHandle openCanonicalRoot(const std::wstring& requested) {
    const std::wstring registered = registeredRoot();
    if (registered.empty() || _wcsicmp(registered.c_str(), requested.c_str()) != 0) return {};
    HANDLE raw = CreateFileW(registered.c_str(), FILE_LIST_DIRECTORY | FILE_TRAVERSE | DELETE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    UniqueHandle root(raw);
    if (!root || !isPlain(root.get(), true)) return {};
    wchar_t finalPath[32768];
    const DWORD n = GetFinalPathNameByHandleW(root.get(), finalPath, 32768,
                                               FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!n || n >= 32768) return {};
    HANDLE checkRaw = CreateFileW(finalPath, FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    UniqueHandle check(checkRaw);
    return check && isPlain(check.get(), true) && sameIdentity(root.get(), check.get())
        ? std::move(root) : UniqueHandle{};
}

bool splitPath(const std::string& value, std::vector<std::wstring>& parts) {
    if (value.empty() || value.front() == '/' || value.find('\\') != std::string::npos ||
        value.find('\0') != std::string::npos) return false;
    std::size_t begin = 0;
    while (begin < value.size()) {
        const auto end = value.find('/', begin);
        const std::string part = value.substr(begin, end - begin);
        if (part.empty() || part == "." || part == ".." || part.find(':') != std::string::npos)
            return false;
        const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, part.data(),
                                               static_cast<int>(part.size()), nullptr, 0);
        if (count <= 0) return false;
        std::wstring wide(count, L'\0');
        if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, part.data(),
                                 static_cast<int>(part.size()), wide.data(), count)) return false;
        if (wide.back() == L'.' || wide.back() == L' ') return false;
        parts.push_back(std::move(wide));
        if (end == std::string::npos) break;
        begin = end + 1;
    }
    return !parts.empty();
}

bool hashAndDelete(HANDLE root, const std::vector<std::wstring>& parts,
                   std::uint64_t expectedSize, const std::string& expectedHash) {
    HANDLE parent = root;
    std::vector<UniqueHandle> parents;
    for (std::size_t i = 0; i + 1 < parts.size(); ++i) {
        auto next = openPath(parent, parts[i], FILE_LIST_DIRECTORY | FILE_TRAVERSE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                             FILE_FLAG_BACKUP_SEMANTICS);
        if (!next || !isPlain(next.get(), true)) return false;
        parent = next.get();
        parents.push_back(std::move(next));
    }
    auto file = openPath(parent, parts.back(), FILE_READ_DATA | FILE_READ_ATTRIBUTES | DELETE,
                         FILE_SHARE_READ, 0);
    if (!file || !isPlain(file.get(), false)) return false;
    FILE_STANDARD_INFO standard{};
    if (!GetFileInformationByHandleEx(file.get(), FileStandardInfo, &standard, sizeof(standard)) ||
        standard.EndOfFile.QuadPart < 0 ||
        static_cast<std::uint64_t>(standard.EndOfFile.QuadPart) != expectedSize) return false;

    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    std::array<unsigned char, 32> digest{};
    bool ok = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0 &&
              BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) == 0;
    std::array<unsigned char, 64 * 1024> buffer{};
    LARGE_INTEGER zero{};
    ok = ok && SetFilePointerEx(file.get(), zero, nullptr, FILE_BEGIN);
    while (ok) {
        DWORD read = 0;
        if (!ReadFile(file.get(), buffer.data(), static_cast<DWORD>(buffer.size()), &read, nullptr)) {
            ok = false; break;
        }
        if (!read) break;
        ok = BCryptHashData(hash, buffer.data(), read, 0) == 0;
    }
    ok = ok && BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) == 0;
    if (hash) BCryptDestroyHash(hash);
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    static constexpr char hex[] = "0123456789abcdef";
    std::string actual; actual.reserve(64);
    for (unsigned char byte : digest) { actual += hex[byte >> 4]; actual += hex[byte & 15]; }
    if (!ok || actual != expectedHash) return false;

    FILE_DISPOSITION_INFO_EX disposition{};
    disposition.Flags = FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS;
    return SetFileInformationByHandle(file.get(), FileDispositionInfoEx,
                                      &disposition, sizeof(disposition)) != FALSE;
}

int uninstall(const std::wstring& requestedRoot) {
    auto root = openCanonicalRoot(requestedRoot);
    if (!root) return 10;
    const fs::path rootPath(requestedRoot);
    const auto verified = chudware::security::verifyPayloadManifest(
        rootPath, rootPath / kManifestName, rootPath / kSignatureName,
        chudware::security::VerifyMode::Full, rootPath / L"chudware.exe");
    if (!verified.ok) return 11;

    json manifest;
    try { manifest = json::parse(std::ifstream(rootPath / kManifestName, std::ios::binary)); }
    catch (...) { return 12; }
    if (!manifest.is_object() || manifest.value("schema", "") != "chudware-payload-v1" ||
        manifest.value("product", "") != "Chudware" || manifest.value("platform", "") != "windows" ||
        manifest.value("arch", "") != "x86_64" || !manifest.contains("files") ||
        !manifest["files"].is_array() || manifest["files"].empty()) return 13;

    struct Owned { std::vector<std::wstring> parts; std::uint64_t size; std::string hash; };
    std::vector<Owned> owned;
    std::set<std::wstring> unique;
    for (const auto& row : manifest["files"]) {
        if (!row.is_object() || !row.contains("path") || !row.contains("size") ||
            !row.contains("sha256") || !row["path"].is_string() ||
            !row["size"].is_number_unsigned() || !row["sha256"].is_string()) return 14;
        Owned item{};
        const std::string relative = row["path"].get<std::string>();
        if (!splitPath(relative, item.parts)) return 14;
        std::wstring folded;
        for (const auto& part : item.parts) { if (!folded.empty()) folded += L'\\'; folded += part; }
        std::transform(folded.begin(), folded.end(), folded.begin(), ::towlower);
        if (!unique.insert(folded).second) return 14;
        item.size = row["size"].get<std::uint64_t>();
        item.hash = row["sha256"].get<std::string>();
        if (item.hash.size() != 64) return 14;
        owned.push_back(std::move(item));
    }
    std::sort(owned.begin(), owned.end(), [](const Owned& a, const Owned& b) {
        return a.parts.size() > b.parts.size();
    });
    for (const auto& item : owned)
        if (!hashAndDelete(root.get(), item.parts, item.size, item.hash)) return 15;
    return 0;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc != 3 || wcscmp(argv[1], L"--uninstall") != 0) return 2;
    return uninstall(argv[2]);
}

#endif
