#include "updater.h"

#if defined(_WIN32)

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

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
                                  const std::filesystem::path&,
                                  const std::string&,
                                  std::string* error) {
    const std::wstring installer = wide(verifiedArtifact.u8string());
    if (installer.empty()) {
        if (error) *error = "installer path is not valid UTF-8";
        return ApplyResult::Failed;
    }
    std::wstring command = L"\"" + installer + L"\" /SILENT /NORESTART";
    std::vector<wchar_t> mutableCommand(command.begin(), command.end());
    mutableCommand.push_back(L'\0');
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(nullptr, mutableCommand.data(), nullptr, nullptr, FALSE,
                        CREATE_NEW_PROCESS_GROUP, nullptr, nullptr, &startup, &process)) {
        if (error) *error = windowsError("could not start the verified Chudware installer");
        return GetLastError() == ERROR_ACCESS_DENIED
            ? ApplyResult::ManualDownload : ApplyResult::Failed;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return ApplyResult::Relaunching;
}

}  // namespace chudware::update

#endif
