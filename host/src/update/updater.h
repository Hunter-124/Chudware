#pragma once

#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <string_view>

namespace chudware::update {

enum class State {
    Idle,
    Checking,
    Current,
    Available,
    Downloading,
    Staged,
    ManualDownload,
    Error,
};

struct Manifest {
    std::string version;
    std::string platform;
    std::string arch;
    std::string keyId;
    std::string sha256;
    std::uint64_t size = 0;
    std::string signature;
    std::string notesUrl;
    std::string url;
};

struct Snapshot {
    State state = State::Idle;
    std::string version;
    std::string message;
    std::string downloadUrl;
    std::uint64_t reportSerial = 0;
};

enum class ApplyResult {
    NothingStaged,
    Relaunching,
    ManualDownload,
    Rejected,
    Failed,
};

// Semantic-version precedence per semver.org. Build metadata is ignored.
// Invalid inputs compare lower than valid ones; two invalid strings compare equal.
int compareVersions(const std::string& lhs, const std::string& rhs);

// The signed release manifest one target polls: the `update-<platform>-<arch>.json`
// asset of the latest GitHub release. `platform` is windows|macos|linux and
// `arch` is x86_64|arm64, the same identity the manifest's signature covers.
std::string releaseManifestUrl(std::string_view platform, std::string_view arch);
// The manifest the update check fetches: CHUDWARE_UPDATE_MANIFEST_URL verbatim
// when set, otherwise releaseManifestUrl() for this build's platform and arch.
std::string manifestUrl();

class Updater {
public:
    Updater();
    ~Updater();
    Updater(const Updater&) = delete;
    Updater& operator=(const Updater&) = delete;

    void configure(std::string currentVersion, std::filesystem::path dataDir,
                   std::filesystem::path executablePath);
    ApplyResult applyStagedBeforeWindow();
    void start();
    void stop();
    // Wakes the polling worker so a manual "Check now" does not have to wait out
    // the remaining poll interval. No-op when no worker is running.
    void requestCheckNow();
    Snapshot snapshot() const;

    // The download is streamed into the update directory, then size, SHA-256 and
    // Ed25519 are verified before an atomic stage rename. Exposed so the exact
    // production path can be exercised by the security regression test.
    bool downloadAndStage(const Manifest& manifest, std::string* error = nullptr);
    bool verifyAndStageDownloaded(const Manifest& manifest,
                                  const std::filesystem::path& downloaded,
                                  std::string* error = nullptr);

    // Headless UI scenario seam. It never starts a worker or performs I/O.
    void setTestSnapshot(Snapshot value);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

Updater& globalUpdater();

// Implemented once by the current platform TU. The caller invokes this only for
// an artifact that has just passed all three verification checks.
ApplyResult applyPlatformArtifact(const std::filesystem::path& verifiedArtifact,
                                  const std::filesystem::path& executablePath,
                                  const std::string& version,
                                  std::string* error);

namespace testing {
enum class StagedTransactionState { Missing, Staged, Applying, Committed, Invalid };
StagedTransactionState stagedTransactionState(const std::filesystem::path& metadata);
bool transitionStagedTransaction(const std::filesystem::path& metadata,
                                 StagedTransactionState expected,
                                 StagedTransactionState desired,
                                 std::string* error = nullptr);
}

}  // namespace chudware::update
