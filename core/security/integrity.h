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

// Verifies exactly one named manifest row after authenticating the canonical manifest.
// Fails closed when the path is absent, duplicated, or has a different kind.
FileVerdict verifyInstalledManifestEntry(const std::filesystem::path& executable,
                                         std::string_view relativePath,
                                         std::string_view expectedKind);

#if defined(_WIN32)
FileVerdict verifyInstalledManifestHandle(const std::filesystem::path& executable,
                                          std::string_view relativePath,
                                          std::string_view expectedKind,
                                          void* fileHandle);
#else
FileVerdict verifyInstalledManifestFd(const std::filesystem::path& executable,
                                      std::string_view relativePath,
                                      std::string_view expectedKind,
                                      int fd);
#endif

IntegrityResult verifyPayloadManifest(const std::filesystem::path& root,
                                      const std::filesystem::path& manifest,
                                      const std::filesystem::path& signature,
                                      VerifyMode mode = VerifyMode::Full,
                                      const std::filesystem::path& executable = {});

// Verifies the exact caller-held manifest/signature bytes. Callers that pin file handles
// use this overload so the authenticated bytes are the same bytes they subsequently parse.
IntegrityResult verifyPayloadManifestBytes(const std::filesystem::path& root,
                                           std::string_view manifestBytes,
                                           std::string_view signatureBytes,
                                           VerifyMode mode = VerifyMode::Full,
                                           const std::filesystem::path& executable = {});

IntegrityResult verifyInstalledPayload(const std::filesystem::path& executable,
                                       VerifyMode mode);

const char* integrityReasonName(IntegrityReason reason) noexcept;

}  // namespace chudware::security
