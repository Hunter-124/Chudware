#include "windows_update_helper.h"
#include "security/integrity.h"

#if defined(_WIN32)

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shlobj.h>
#include <bcrypt.h>
#include <shellapi.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <memory>
#include <cctype>
#include <fstream>
#include <set>
#include <vector>

namespace chudware::update {
namespace {
namespace fs = std::filesystem;
using json = nlohmann::json;
constexpr wchar_t kManifest[] = L"payload-manifest.v1.json";
constexpr wchar_t kSignature[] = L"payload-manifest.v1.sig";
constexpr wchar_t kHeldManifest[] = L"installed-manifest.v1.json";
constexpr wchar_t kHeldSignature[] = L"installed-manifest.v1.sig";

struct HandleCloser {
    void operator()(void* value) const noexcept {
        if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value);
    }
};
using UniqueHandle = std::unique_ptr<void, HandleCloser>;

UniqueHandle openPlain(const fs::path& path, DWORD access, DWORD share, bool directory,
                       DWORD creation = OPEN_EXISTING) {
    const DWORD flags = FILE_FLAG_OPEN_REPARSE_POINT |
                        (directory ? FILE_FLAG_BACKUP_SEMANTICS : 0);
    UniqueHandle handle(CreateFileW(path.c_str(), access, share, nullptr, creation, flags, nullptr));
    if (!handle) return {};
    FILE_ATTRIBUTE_TAG_INFO info{};
    if (!GetFileInformationByHandleEx(handle.get(), FileAttributeTagInfo, &info, sizeof(info)) ||
        (info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) ||
        (!!(info.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != directory)) return {};
    return handle;
}

bool sameIdentity(HANDLE left, HANDLE right) {
    FILE_ID_INFO a{}, b{};
    return GetFileInformationByHandleEx(left, FileIdInfo, &a, sizeof(a)) &&
           GetFileInformationByHandleEx(right, FileIdInfo, &b, sizeof(b)) &&
           a.VolumeSerialNumber == b.VolumeSerialNumber &&
           std::equal(std::begin(a.FileId.Identifier), std::end(a.FileId.Identifier),
                      std::begin(b.FileId.Identifier));
}

bool readHeld(HANDLE handle, std::size_t cap, std::string& bytes) {
    LARGE_INTEGER zero{};
    if (!SetFilePointerEx(handle, zero, nullptr, FILE_BEGIN)) return false;
    std::array<char, 64 * 1024> buffer{};
    for (;;) {
        DWORD count = 0;
        if (!ReadFile(handle, buffer.data(), static_cast<DWORD>(buffer.size()), &count, nullptr)) return false;
        if (!count) return true;
        if (bytes.size() + count > cap) return false;
        bytes.append(buffer.data(), count);
    }
}

std::string hexDigest(const unsigned char* bytes, std::size_t count) {
    static constexpr char hex[] = "0123456789abcdef";
    std::string result; result.reserve(count * 2);
    for (std::size_t i = 0; i < count; ++i) { result += hex[bytes[i] >> 4]; result += hex[bytes[i] & 15]; }
    return result;
}

bool hmacSha256(std::string_view key, std::string_view message, std::string& digest) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    std::array<unsigned char, 32> output{};
    bool ok = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr,
                                          BCRYPT_ALG_HANDLE_HMAC_FLAG) == 0 &&
              BCryptCreateHash(algorithm, &hash, nullptr, 0,
                  reinterpret_cast<PUCHAR>(const_cast<char*>(key.data())),
                  static_cast<ULONG>(key.size()), 0) == 0 &&
              BCryptHashData(hash,
                  reinterpret_cast<PUCHAR>(const_cast<char*>(message.data())),
                  static_cast<ULONG>(message.size()), 0) == 0 &&
              BCryptFinishHash(hash, output.data(), static_cast<ULONG>(output.size()), 0) == 0;
    if (hash) BCryptDestroyHash(hash);
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    if (ok) digest = hexDigest(output.data(), output.size());
    return ok;
}

std::string pendingMacKey(std::string_view manifestBytes, std::string_view signatureBytes) {
    std::string material("chudware-windows-recovery-key-v1\0", 33);
    material.append(manifestBytes); material.push_back('\0'); material.append(signatureBytes);
    BCRYPT_ALG_HANDLE algorithm = nullptr; BCRYPT_HASH_HANDLE hash = nullptr;
    std::array<unsigned char, 32> output{};
    bool ok = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0 &&
              BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) == 0 &&
              BCryptHashData(hash, reinterpret_cast<PUCHAR>(material.data()),
                             static_cast<ULONG>(material.size()), 0) == 0 &&
              BCryptFinishHash(hash, output.data(), static_cast<ULONG>(output.size()), 0) == 0;
    if (hash) BCryptDestroyHash(hash); if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    return ok ? std::string(reinterpret_cast<char*>(output.data()), output.size()) : std::string{};
}

bool constantEqual(std::string_view a, std::string_view b) {
    if (a.size() != b.size()) return false;
    unsigned char mismatch = 0;
    for (std::size_t i = 0; i < a.size(); ++i) mismatch |= static_cast<unsigned char>(a[i] ^ b[i]);
    return mismatch == 0;
}

std::wstring quote(const fs::path& p) {
    std::wstring s = p.wstring();
    std::wstring out = L"\"";
    unsigned slashes = 0;
    for (wchar_t c : s) {
        if (c == L'\\') { ++slashes; continue; }
        if (c == L'\"') { out.append(slashes * 2 + 1, L'\\'); out += c; }
        else { out.append(slashes, L'\\'); out += c; }
        slashes = 0;
    }
    out.append(slashes * 2, L'\\');
    return out + L'\"';
}

bool realDirectory(const fs::path& p) {
    const DWORD attrs = GetFileAttributesW(p.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY) &&
           !(attrs & FILE_ATTRIBUTE_REPARSE_POINT);
}

bool realFile(const fs::path& p) {
    const DWORD attrs = GetFileAttributesW(p.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY) &&
           !(attrs & FILE_ATTRIBUTE_REPARSE_POINT);
}

bool safeRelative(const std::string& value, fs::path& out) {
    if (value.empty() || value.find('\0') != std::string::npos ||
        value.find('\\') != std::string::npos || value.front() == '/' ||
        (value.size() > 1 && value[1] == ':')) return false;
    out.clear();
    std::size_t begin = 0;
    while (begin < value.size()) {
        const auto end = value.find('/', begin);
        const std::string part = value.substr(begin, end - begin);
        if (part.empty() || part == "." || part == "..") return false;
        out /= fs::u8path(part);
        if (end == std::string::npos) break;
        begin = end + 1;
    }
    return !out.empty();
}

bool checkedParent(const fs::path& root, const fs::path& relative, bool create,
                   fs::path& target) {
    fs::path current = root;
    if (!realDirectory(current)) return false;
    auto it = relative.begin();
    for (; it != relative.end(); ++it) {
        auto next = it; ++next;
        if (next == relative.end()) break;
        current /= *it;
        if (!fs::exists(current)) {
            if (!create || !fs::create_directory(current)) return false;
        }
        if (!realDirectory(current)) return false;
    }
    target = root / relative;
    return true;
}

fs::path updatesRoot() {
    PWSTR raw = nullptr;
    if (FAILED(SHGetKnownFolderPath(FOLDERID_LocalAppData, KF_FLAG_CREATE, nullptr, &raw))) return {};
    fs::path result(raw);
    CoTaskMemFree(raw);
    return result / L"Chudware" / L"updates";
}

bool writeJsonAtomic(const fs::path& path, const json& value) {
    const fs::path temp = path.wstring() + L".tmp";
    { std::ofstream out(temp, std::ios::binary | std::ios::trunc); out << value.dump(); if (!out) return false; }
    return MoveFileExW(temp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
}

bool parseOwned(std::string_view manifestBytes, std::vector<fs::path>& owned, std::string* error) {
    try {
        const json manifest = json::parse(manifestBytes);
        if (!manifest.contains("files") || !manifest["files"].is_array())
            throw std::runtime_error("payload manifest has no files array");
        std::set<std::wstring> unique;
        for (const auto& row : manifest["files"]) {
            fs::path relative;
            if (!row.is_object() || !row.contains("path") ||
                !safeRelative(row.at("path").get<std::string>(), relative))
                throw std::runtime_error("payload manifest contains an unsafe path");
            std::wstring folded = relative.generic_wstring();
            std::transform(folded.begin(), folded.end(), folded.begin(), ::towlower);
            if (!unique.insert(folded).second)
                throw std::runtime_error("payload manifest contains a duplicate path");
            owned.push_back(relative);
        }
        owned.emplace_back(kManifest); owned.emplace_back(kSignature);
        return true;
    } catch (const std::exception& e) { if (error) *error = e.what(); return false; }
}

bool loadInstalledTrust(const fs::path& root, std::string& manifestBytes,
                        std::string& signatureBytes, std::vector<fs::path>& owned,
                        std::string* error) {
    auto manifest = openPlain(root / kManifest, GENERIC_READ, FILE_SHARE_READ, false);
    auto signature = openPlain(root / kSignature, GENERIC_READ, FILE_SHARE_READ, false);
    if (!manifest || !signature || !readHeld(manifest.get(), 16 * 1024 * 1024, manifestBytes) ||
        !readHeld(signature.get(), 256, signatureBytes)) {
        if (error) *error = "installed payload metadata is missing or reparse-backed";
        return false;
    }
    const auto verified = chudware::security::verifyPayloadManifestBytes(
        root, manifestBytes, signatureBytes, chudware::security::VerifyMode::Full,
        root / L"chudware.exe");
    if (!verified.ok) { if (error) *error = "installed payload metadata is not trusted"; return false; }
    return parseOwned(manifestBytes, owned, error);
}

bool loadHeldTrust(const fs::path& session, std::string& manifestBytes,
                   std::string& signatureBytes, std::vector<fs::path>& owned,
                   std::string* error) {
    auto manifest = openPlain(session / kHeldManifest, GENERIC_READ, FILE_SHARE_READ, false);
    auto signature = openPlain(session / kHeldSignature, GENERIC_READ, FILE_SHARE_READ, false);
    if (!manifest || !signature || !readHeld(manifest.get(), 16 * 1024 * 1024, manifestBytes) ||
        !readHeld(signature.get(), 256, signatureBytes)) {
        if (error) *error = "held recovery manifest is missing or unsafe";
        return false;
    }
    return parseOwned(manifestBytes, owned, error);
}

json existenceRows(const std::vector<fs::path>& owned, const json& snapshotRows) {
    if (!snapshotRows.is_array() || snapshotRows.size() != owned.size())
        throw std::runtime_error("pending recovery row count does not match held manifest");
    json rows = json::array();
    for (std::size_t i = 0; i < owned.size(); ++i) {
        if (!snapshotRows.at(i).is_object() || !snapshotRows.at(i).contains("existed") ||
            !snapshotRows.at(i).at("existed").is_boolean())
            throw std::runtime_error("pending recovery row is malformed");
        rows.push_back({{"path", owned[i].generic_u8string()},
                        {"existed", snapshotRows.at(i).at("existed").get<bool>()}});
    }
    return rows;
}

bool snapshot(const fs::path& install, const fs::path& rollback,
              const std::vector<fs::path>& owned, json& files, std::string* error) {
    if (!fs::create_directories(rollback) && !realDirectory(rollback)) { if (error) *error = "could not create rollback directory"; return false; }
    for (const fs::path& relative : owned) {
        fs::path source;
        if (!checkedParent(install, relative, false, source)) { if (error) *error = "reparse point in installed payload path"; return false; }
        if (!fs::exists(source)) { files.push_back({{"path", relative.generic_u8string()}, {"existed", false}}); continue; }
        if (!realFile(source)) { if (error) *error = "manifest-owned path is not a regular file"; return false; }
        fs::path destination;
        if (!checkedParent(rollback, relative, true, destination)) { if (error) *error = "unsafe rollback path"; return false; }
        if (!CopyFileW(source.c_str(), destination.c_str(), TRUE)) { if (error) *error = "could not snapshot installed payload"; return false; }
        files.push_back({{"path", relative.generic_u8string()}, {"existed", true}});
    }
    return true;
}

bool restore(const fs::path& install, const fs::path& rollback, const json& files,
             std::string* error) {
    auto rootHandle = openPlain(install, FILE_LIST_DIRECTORY | FILE_TRAVERSE,
                                FILE_SHARE_READ | FILE_SHARE_WRITE, true);
    auto rollbackHandle = openPlain(rollback, FILE_LIST_DIRECTORY | FILE_TRAVERSE,
                                    FILE_SHARE_READ, true);
    if (!rootHandle || !rollbackHandle) { if (error) *error = "restore roots are unsafe"; return false; }
    for (const auto& row : files) {
        fs::path relative;
        if (!safeRelative(row.at("path").get<std::string>(), relative)) { if (error) *error = "unsafe rollback record"; return false; }
        fs::path target;
        if (!checkedParent(install, relative, true, target)) { if (error) *error = "reparse point in restore path"; return false; }
        auto rootCheck = openPlain(install, FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE, true);
        if (!rootCheck || !sameIdentity(rootHandle.get(), rootCheck.get())) { if (error) *error = "install root identity changed"; return false; }
        if (!row.at("existed").get<bool>()) {
            if (fs::exists(target)) {
                auto held = openPlain(target, DELETE | FILE_READ_ATTRIBUTES, FILE_SHARE_READ, false);
                if (!held) { if (error) *error = "refusing to remove unsafe owned path"; return false; }
                FILE_DISPOSITION_INFO_EX disposition{};
                disposition.Flags = FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS;
                if (!SetFileInformationByHandle(held.get(), FileDispositionInfoEx,
                                                &disposition, sizeof(disposition))) {
                    if (error) *error = "could not remove newly installed owned file";
                    return false;
                }
            }
        } else {
            fs::path source;
            if (!checkedParent(rollback, relative, false, source)) { if (error) *error = "rollback snapshot is incomplete"; return false; }
            auto sourceHandle = openPlain(source, GENERIC_READ, FILE_SHARE_READ, false);
            auto rollbackCheck = openPlain(rollback, FILE_READ_ATTRIBUTES, FILE_SHARE_READ, true);
            if (!sourceHandle || !rollbackCheck || !sameIdentity(rollbackHandle.get(), rollbackCheck.get())) { if (error) *error = "rollback source identity changed"; return false; }
            auto targetHandle = openPlain(target, GENERIC_WRITE | FILE_READ_ATTRIBUTES,
                                          FILE_SHARE_READ, false, OPEN_ALWAYS);
            if (!targetHandle) { if (error) *error = "restore target is unsafe"; return false; }
            LARGE_INTEGER zero{};
            if (!SetFilePointerEx(sourceHandle.get(), zero, nullptr, FILE_BEGIN) ||
                !SetFilePointerEx(targetHandle.get(), zero, nullptr, FILE_BEGIN) ||
                !SetEndOfFile(targetHandle.get())) { if (error) *error = "could not prepare restore target"; return false; }
            std::array<unsigned char, 64 * 1024> buffer{};
            for (;;) {
                DWORD read = 0;
                if (!ReadFile(sourceHandle.get(), buffer.data(), static_cast<DWORD>(buffer.size()), &read, nullptr)) { if (error) *error = "could not read rollback snapshot"; return false; }
                if (!read) break;
                DWORD written = 0;
                if (!WriteFile(targetHandle.get(), buffer.data(), read, &written, nullptr) || written != read) { if (error) *error = "could not restore installed payload"; return false; }
            }
            if (!FlushFileBuffers(targetHandle.get())) { if (error) *error = "could not flush restored payload"; return false; }
        }
    }
    return true;
}

DWORD run(const fs::path& program, const std::wstring& args, bool elevate, std::string* error) {
    SHELLEXECUTEINFOW info{}; info.cbSize = sizeof(info); info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
    info.lpVerb = elevate ? L"runas" : L"open"; info.lpFile = program.c_str(); info.lpParameters = args.c_str(); info.nShow = SW_SHOWNORMAL;
    if (!ShellExecuteExW(&info) || !info.hProcess) { if (error) *error = "could not launch update process"; return UINT32_MAX; }
    WaitForSingleObject(info.hProcess, INFINITE); DWORD code = UINT32_MAX; GetExitCodeProcess(info.hProcess, &code); CloseHandle(info.hProcess); return code;
}

bool canonicalRoot(const fs::path& executable, const fs::path& claimed, fs::path& root) {
    std::error_code ec; const fs::path exe = fs::weakly_canonical(executable, ec); if (ec || !realFile(exe)) return false;
    const fs::path actual = fs::weakly_canonical(exe.parent_path(), ec); if (ec || !realDirectory(actual)) return false;
    const fs::path requested = fs::weakly_canonical(claimed, ec); if (ec || requested != actual) return false;
    root = actual; return true;
}
}  // namespace

bool recoverPendingWindowsUpdate(const fs::path& executable, std::string* error) {
    fs::path install;
    if (!canonicalRoot(executable, executable.parent_path(), install)) {
        if (error) *error = "running executable parent is not a canonical install root";
        return false;
    }
    const fs::path sessions = updatesRoot() / L"sessions";
    if (!fs::exists(sessions)) return true;
    if (!realDirectory(sessions)) { if (error) *error = "update sessions root is a reparse point"; return false; }
    for (const auto& entry : fs::directory_iterator(sessions)) {
        if (!entry.is_directory() || entry.is_symlink() || !realDirectory(entry.path())) continue;
        const fs::path pending = entry.path() / L"pending.json";
        const fs::path committed = entry.path() / L"committed.json";
        if (!fs::exists(pending) || fs::exists(committed)) continue;
        try {
            if (!realFile(pending)) throw std::runtime_error("pending recovery state is unsafe");
            const json envelope = json::parse(std::ifstream(pending));
            const json state = envelope.at("state");
            if (!state.is_object() || !envelope.at("mac").is_string() ||
                fs::u8path(state.at("install_root").get<std::string>()) != install)
                throw std::runtime_error("pending recovery root does not match running executable");
            std::string manifestBytes, signatureBytes; std::vector<fs::path> owned;
            if (!loadHeldTrust(entry.path(), manifestBytes, signatureBytes, owned, error)) return false;
            std::string expected;
            const std::string key = pendingMacKey(manifestBytes, signatureBytes);
            if (key.empty() || !hmacSha256(key, state.dump(), expected) ||
                !constantEqual(expected, envelope.at("mac").get<std::string>()))
                throw std::runtime_error("pending recovery authentication failed");
            const json rows = existenceRows(owned, state.at("files"));
            if (!restore(install, entry.path() / L"rollback", rows, error)) return false;
            fs::remove(pending);
        } catch (const std::exception& e) { if (error) *error = e.what(); return false; }
    }
    return true;
}

int runWindowsUpdateHelper(int argc, wchar_t** argv) {
    if (argc != 5) return 2;
    const fs::path installer = argv[1], executable = argv[2], claimedRoot = argv[3];
    const std::wstring sessionId = argv[4];
    if (sessionId.empty() || sessionId.find_first_not_of(L"0123456789abcdefABCDEF-") != std::wstring::npos || !realFile(installer)) return 2;
    fs::path install; if (!canonicalRoot(executable, claimedRoot, install)) return 2;
    const fs::path session = updatesRoot() / L"sessions" / sessionId;
    if (fs::exists(session) || !fs::create_directories(session / L"rollback") || !realDirectory(session)) return 3;
    std::string manifestBytes, signatureBytes;
    std::vector<fs::path> owned; std::string error;
    if (!loadInstalledTrust(install, manifestBytes, signatureBytes, owned, &error)) return 4;
    if (!CopyFileW((install / kManifest).c_str(), (session / kHeldManifest).c_str(), TRUE) ||
        !CopyFileW((install / kSignature).c_str(), (session / kHeldSignature).c_str(), TRUE)) return 5;
    json files = json::array(); if (!snapshot(install, session / L"rollback", owned, files, &error)) return 5;
    json state{{"session_id", std::string(sessionId.begin(), sessionId.end())},
               {"install_root", install.generic_u8string()}, {"files", files}};
    std::string mac;
    const std::string key = pendingMacKey(manifestBytes, signatureBytes);
    if (key.empty() || !hmacSha256(key, state.dump(), mac) ||
        !writeJsonAtomic(session / L"pending.json", {{"state", state}, {"mac", mac}})) return 6;
    const DWORD installCode = run(installer, L"/SILENT /NORESTART /DIR=" + quote(install), true, &error);
    const DWORD verifyCode = installCode == 0 ? run(executable, L"--verify-payload-manifest " + quote(install / kManifest) + L" " + quote(install / kSignature), false, &error) : installCode;
    if (installCode != 0 || verifyCode != 0) { restore(install, session / L"rollback", files, &error); return 7; }
    if (!writeJsonAtomic(session / L"committed.json", {{"session_id", std::string(sessionId.begin(), sessionId.end())}})) { restore(install, session / L"rollback", files, &error); return 8; }
    fs::remove(session / L"pending.json");
    ShellExecuteW(nullptr, L"open", executable.c_str(), nullptr, install.c_str(), SW_SHOWNORMAL);
    return 0;
}

namespace testing {
bool restoreWindowsSnapshotForTest(const fs::path& installRoot,
                                   const fs::path& rollbackRoot,
                                   const fs::path& stateFile,
                                   std::string* error) {
    try {
        const json state = json::parse(std::ifstream(stateFile));
        return restore(installRoot, rollbackRoot, state.at("files"), error);
    } catch (const std::exception& e) {
        if (error) *error = e.what();
        return false;
    }
}
}  // namespace testing

}  // namespace chudware::update

#endif
