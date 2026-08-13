#include "updater.h"

#if defined(_WIN32)

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <objbase.h>


#include <filesystem>
#include <algorithm>
#include <string>
#include <vector>

namespace chudware::update {
namespace {

std::wstring wide(const std::string& value) {
    if (value.empty()) return {};
    const int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                                         static_cast<int>(value.size()), nullptr, 0);
    if (size <= 0) return {};
    std::wstring out(static_cast<std::size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                        static_cast<int>(value.size()), out.data(), size);
    return out;
}

std::string windowsError(const char* prefix) {
    return std::string(prefix) + " (Windows error " + std::to_string(GetLastError()) + ")";
}

}  // namespace

ApplyResult applyPlatformArtifact(const std::filesystem::path& verifiedArtifact,
                                  const std::filesystem::path& executablePath,
                                  const std::string&,
                                  std::string* error) {
    const std::filesystem::path installRoot = executablePath.parent_path();
    const std::filesystem::path sourceHelper = installRoot / L"chudware-update-helper.exe";
    if ((GetFileAttributesW(sourceHelper.c_str()) & FILE_ATTRIBUTE_REPARSE_POINT) != 0 ||
        !std::filesystem::is_regular_file(sourceHelper)) {
        if (error) *error = "installed update helper is missing or reparse-backed";
        return ApplyResult::Failed;
    }
    wchar_t tempRaw[MAX_PATH + 1]{};
    if (!GetTempPathW(MAX_PATH, tempRaw)) {
        if (error) *error = windowsError("could not locate temporary directory");
        return ApplyResult::Failed;
    }
    GUID guid{};
    if (CoCreateGuid(&guid) != S_OK) {
        if (error) *error = "could not create update session id";
        return ApplyResult::Failed;
    }
    wchar_t sessionRaw[40]{};
    if (!StringFromGUID2(guid, sessionRaw, 40)) {
        if (error) *error = "could not format update session id";
        return ApplyResult::Failed;
    }
    std::wstring session(sessionRaw);
    session.erase(std::remove_if(session.begin(), session.end(),
                                 [](wchar_t c) { return c == L'{' || c == L'}'; }),
                  session.end());
    const std::filesystem::path helperDir = std::filesystem::path(tempRaw) /
        (L"chudware-update-" + session);
    std::error_code ec;
    if (!std::filesystem::create_directory(helperDir, ec) ||
        (GetFileAttributesW(helperDir.c_str()) & FILE_ATTRIBUTE_REPARSE_POINT)) {
        if (error) *error = "could not create private update helper directory";
        return ApplyResult::Failed;
    }
    const std::filesystem::path helper = helperDir / L"chudware-update-helper.exe";
    if (!CopyFileW(sourceHelper.c_str(), helper.c_str(), TRUE)) {
        if (error) *error = windowsError("could not copy update helper");
        std::filesystem::remove_all(helperDir, ec);
        return ApplyResult::Failed;
    }
    auto quote = [](const std::filesystem::path& p) { return L"\"" + p.wstring() + L"\""; };
    std::wstring command = quote(helper) + L" " + quote(verifiedArtifact) + L" " +
        quote(executablePath) + L" " + quote(installRoot) + L" " + session;
    std::vector<wchar_t> mutableCommand(command.begin(), command.end());
    mutableCommand.push_back(L'\0');
    STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(helper.c_str(), mutableCommand.data(), nullptr, nullptr, FALSE,
                        CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
                        nullptr, helperDir.c_str(), &startup, &process)) {
        if (error) *error = windowsError("could not start verified update helper");
        std::filesystem::remove_all(helperDir, ec);
        return GetLastError() == ERROR_ACCESS_DENIED ? ApplyResult::ManualDownload
                                                     : ApplyResult::Failed;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return ApplyResult::Relaunching;
}

}  // namespace chudware::update

#endif
