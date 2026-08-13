#pragma once

#include <filesystem>
#include <string>

namespace chudware::update {

// Restores an interrupted Windows update before installed-payload verification.
// On non-Windows platforms this is a no-op.
bool recoverPendingWindowsUpdate(const std::filesystem::path& executable,
                                 std::string* error = nullptr);

#if defined(_WIN32)
// Entry point for the detached chudware-update-helper.exe process.
int runWindowsUpdateHelper(int argc, wchar_t** argv);

namespace testing {
bool restoreWindowsSnapshotForTest(const std::filesystem::path& installRoot,
                                   const std::filesystem::path& rollbackRoot,
                                   const std::filesystem::path& stateFile,
                                   std::string* error = nullptr);
}
#endif

}  // namespace chudware::update
