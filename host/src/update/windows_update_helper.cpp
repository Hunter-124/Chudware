#include "windows_update_helper.h"

#if defined(_WIN32)

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shlobj.h>

#include <nlohmann/json.hpp>

#include <algorithm>
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

bool loadOwned(const fs::path& root, std::vector<fs::path>& owned, std::string* error) {
    try {
        if (!realFile(root / kManifest) || !realFile(root / kSignature)) throw std::runtime_error("installed payload metadata is missing or reparse-backed");
        const json manifest = json::parse(std::ifstream(root / kManifest));
        if (!manifest.contains("files") || !manifest["files"].is_array()) throw std::runtime_error("payload manifest has no files array");
        std::set<std::wstring> unique;
        for (const auto& row : manifest["files"]) {
            fs::path relative;
            if (!row.is_object() || !row.contains("path") || !safeRelative(row.at("path").get<std::string>(), relative))
                throw std::runtime_error("payload manifest contains an unsafe path");
            std::wstring folded = relative.generic_wstring();
            std::transform(folded.begin(), folded.end(), folded.begin(), ::towlower);
            if (!unique.insert(folded).second) throw std::runtime_error("payload manifest contains a duplicate path");
            owned.push_back(relative);
        }
        owned.emplace_back(kManifest); owned.emplace_back(kSignature);
        return true;
    } catch (const std::exception& e) { if (error) *error = e.what(); return false; }
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
    for (const auto& row : files) {
        fs::path relative;
        if (!safeRelative(row.at("path").get<std::string>(), relative)) { if (error) *error = "unsafe rollback record"; return false; }
        fs::path target;
        if (!checkedParent(install, relative, true, target)) { if (error) *error = "reparse point in restore path"; return false; }
        if (!row.at("existed").get<bool>()) {
            if (fs::exists(target) && !realFile(target)) { if (error) *error = "refusing to remove non-file during rollback"; return false; }
            std::error_code ec; fs::remove(target, ec); if (ec) { if (error) *error = "could not remove newly installed owned file"; return false; }
        } else {
            fs::path source;
            if (!checkedParent(rollback, relative, false, source) || !realFile(source)) { if (error) *error = "rollback snapshot is incomplete"; return false; }
            if (!CopyFileW(source.c_str(), target.c_str(), FALSE)) { if (error) *error = "could not restore installed payload"; return false; }
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

bool recoverPendingWindowsUpdate(const fs::path&, std::string* error) {
    const fs::path sessions = updatesRoot() / L"sessions";
    if (!fs::exists(sessions)) return true;
    if (!realDirectory(sessions)) { if (error) *error = "update sessions root is a reparse point"; return false; }
    for (const auto& entry : fs::directory_iterator(sessions)) {
        if (!entry.is_directory() || entry.is_symlink()) continue;
        const fs::path pending = entry.path() / L"pending.json";
        const fs::path committed = entry.path() / L"committed.json";
        if (!fs::exists(pending) || fs::exists(committed)) continue;
        try {
            const json state = json::parse(std::ifstream(pending));
            if (!restore(fs::u8path(state.at("install_root").get<std::string>()), entry.path() / L"rollback", state.at("files"), error)) return false;
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
    std::vector<fs::path> owned; std::string error; if (!loadOwned(install, owned, &error)) return 4;
    json files = json::array(); if (!snapshot(install, session / L"rollback", owned, files, &error)) return 5;
    json pending{{"session_id", std::string(sessionId.begin(), sessionId.end())}, {"install_root", install.generic_u8string()}, {"executable", executable.generic_u8string()}, {"files", files}};
    if (!writeJsonAtomic(session / L"pending.json", pending)) return 6;
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
