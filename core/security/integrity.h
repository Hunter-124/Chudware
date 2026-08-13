#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

namespace chudware::security {

enum class VerifyMode { Startup, ProtectedOperation, Full };

enum class IntegrityReason {
    Ok,
    DeveloperBuild,
    RootNotFound,
    ManifestMissing,
    SignatureMissing,
    IoError,
    InvalidJson,
    NonCanonicalManifest,
    InvalidSchema,
    UnknownKey,
    InvalidSignature,
    UnsafePath,
    MissingFile,
    NotRegularFile,
    SymlinkRejected,
    SizeMismatch,
    HashMismatch,
    ModeMismatch,
};

struct FileVerdict {
    bool ok = false;
    IntegrityReason reason = IntegrityReason::IoError;
    std::string path;
    std::string detail;
};

struct IntegrityResult {
    bool ok = false;
    IntegrityReason reason = IntegrityReason::IoError;
    std::filesystem::path root;
    std::string detail;
    std::vector<FileVerdict> files;
};

inline constexpr int kIntegrityFailureExitCode = 78;

std::filesystem::path installedPayloadRoot(const std::filesystem::path& executable);

FileVerdict verifyManifestEntry(const std::filesystem::path& root,
                                std::string_view relativePath,
                                std::uintmax_t expectedSize,
                                std::string_view expectedSha256,
                                std::uint32_t expectedMode = 0);

IntegrityResult verifyPayloadManifest(const std::filesystem::path& root,
                                      const std::filesystem::path& manifest,
                                      const std::filesystem::path& signature,
                                      VerifyMode mode = VerifyMode::Full,
                                      const std::filesystem::path& executable = {});

IntegrityResult verifyInstalledPayload(const std::filesystem::path& executable,
                                       VerifyMode mode);

const char* integrityReasonName(IntegrityReason reason) noexcept;

}  // namespace chudware::security
