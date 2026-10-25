#include "security/release_keys.h"
#include "update/updater.h"
#include "update/windows_update_helper.h"

#include <algorithm>
#include <cstdlib>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;
using chudware::update::Manifest;
using chudware::update::State;
using chudware::update::Updater;

namespace {

void require(bool condition, const char* message) {
    if (!condition) {
        std::cerr << "FAIL: " << message << '\n';
        std::exit(1);
    }
}

void writeFile(const fs::path& path, const std::string& bytes) {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    require(static_cast<bool>(out), "could not write test fixture");
}

std::string readFile(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(in), {});
}

Manifest corruptManifest() {
    Manifest value;
    value.version = "1.2.0";
    value.platform = "linux";
    value.arch = "x86_64";
    value.keyId = "release-v1";
    value.sha256 = std::string(64, '0');
    value.size = 22;
    value.signature = std::string(88, 'A');
    value.signature.replace(86, 2, "==");
    value.notesUrl = "https://chudware.com/releases/1.2.0";
    return value;
}

Manifest wrongKeyManifest() {
    Manifest value;
    value.version = "1.2.1";
    value.platform = "linux";
    value.arch = "x86_64";
    value.keyId = "release-v1";
    value.sha256 = "45ac4000573891c15d2f881d944f9c98234332693bae7c1410aa2c14ed8a209f";
    value.size = 22;
    // A real Ed25519 signature over the legacy bare digest, produced by
    // private seed 000102...1f. It must not verify under the artifact-v1 domain.
    value.signature = "UHkVz77DialONbSu3wW4Mk0gV2xyyEnAYDbqJpdNe0eLrGwUXBw8HI2AlL8qZ/8eGd/bgvgXVRXsAGTiVFDJCg==";
    value.notesUrl = "https://chudware.com/releases/1.2.1";
    return value;
}

#ifdef CHUDWARE_DEVELOPER_BUILD
Manifest validDomainManifest() {
    Manifest value;
    value.version = "1.2.2";
    value.platform = "linux";
    value.arch = "x86_64";
    value.keyId = "dev-test-v1";
    value.sha256 = "45ac4000573891c15d2f881d944f9c98234332693bae7c1410aa2c14ed8a209f";
    value.size = 22;
    value.signature = "v/JjOpXwWicDr8iiQL5t5H2/sLs/omo31Cej2GnWa1vpGNx8t8t9KywDJKNJjaXSArbl3xHGjq6pRFiOUbCUDg==";
    value.notesUrl = "https://chudware.com/releases/1.2.2";
    return value;
}
#endif

Manifest unknownKeyManifest() {
    Manifest value = wrongKeyManifest();
    value.version = "1.2.3";
    value.keyId = "unknown-release-key";
    return value;
}

void versionTests() {
    using chudware::update::compareVersions;
    require(compareVersions("1.0.0-alpha", "1.0.0") < 0, "prerelease sorts before release");
    require(compareVersions("1.0.0-alpha.1", "1.0.0-alpha.beta") < 0,
            "numeric prerelease sorts before text");
    require(compareVersions("1.0.0+build.1", "1.0.0+build.99") == 0,
            "build metadata does not affect precedence");
    // Prerelease builds such as <base>-beta.<n> must order among themselves
    // numerically and below the release they promote into.
    require(compareVersions("0.1.0-beta.42", "0.1.0-beta.43") < 0,
            "beta builds order by prerelease number");
    require(compareVersions("0.1.0-beta.43", "0.1.0") < 0,
            "a beta sorts below the release it promotes into");
    require(compareVersions("0.1.0-beta.9", "0.1.0-beta.10") < 0,
            "beta run numbers compare numerically, not lexically");
}

// The CI signing key must be trusted by packaged builds, not only developer
// builds: CI-built release artifacts carry key id release-ci-v1.
void releaseKeyringTests() {
    using chudware::security::release::findKey;
    const auto* ci = findKey("release-ci-v1");
    require(ci != nullptr, "release-ci-v1 must be a trusted release key");
    require(ci->publicKey[0] == 0xd7 && ci->publicKey[7] == 0x11 &&
                ci->publicKey[31] == 0xf6,
            "release-ci-v1 public key bytes must match the published key");
    require(findKey("release-v1") != nullptr, "release-v1 must stay trusted");
    require(findKey("unknown-release-key") == nullptr, "unknown key ids stay untrusted");
#ifndef CHUDWARE_DEVELOPER_BUILD
    require(findKey("dev-test-v1") == nullptr,
            "developer test key must not be trusted by packaged builds");
#endif
}

void setEnv(const char* name, const char* value) {
#ifdef _WIN32
    _putenv_s(name, value);
#else
    if (*value) setenv(name, value, 1);
    else unsetenv(name);
#endif
}

// The updater polls one GitHub release asset per target; the asset names are
// the platform/arch identity the release workflow publishes and signs.
void manifestUrlTests() {
    using chudware::update::manifestUrl;
    using chudware::update::releaseManifestUrl;
    const std::string base = "https://github.com/Hunter-124/Chudware/releases/latest/download/";
    require(releaseManifestUrl("linux", "x86_64") == base + "update-linux-x86_64.json",
            "linux x86_64 manifest URL");
    require(releaseManifestUrl("macos", "arm64") == base + "update-macos-arm64.json",
            "macOS arm64 manifest URL");
    require(releaseManifestUrl("macos", "x86_64") == base + "update-macos-x86_64.json",
            "macOS x86_64 manifest URL");
    require(releaseManifestUrl("windows", "x86_64") == base + "update-windows-x86_64.json",
            "Windows x86_64 manifest URL");
#if defined(_WIN32)
    const char* platform = "windows";
#elif defined(__APPLE__)
    const char* platform = "macos";
#else
    const char* platform = "linux";
#endif
#if defined(__aarch64__) || defined(_M_ARM64)
    const char* arch = "arm64";
#else
    const char* arch = "x86_64";
#endif
    setEnv("CHUDWARE_UPDATE_MANIFEST_URL", "");
    require(manifestUrl() == releaseManifestUrl(platform, arch),
            "default manifest URL must be this build's GitHub release asset");
    const std::string custom = "https://updates.example.test/custom/update.json";
    setEnv("CHUDWARE_UPDATE_MANIFEST_URL", custom.c_str());
    require(manifestUrl() == custom,
            "CHUDWARE_UPDATE_MANIFEST_URL must replace the default verbatim");
    setEnv("CHUDWARE_UPDATE_MANIFEST_URL", "");
}

void stagedLifecycleTests(const fs::path& root) {
    using chudware::update::testing::StagedTransactionState;
    using chudware::update::testing::stagedTransactionState;
    using chudware::update::testing::transitionStagedTransaction;
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path metadata = root / "staged.json";
    writeFile(metadata, R"({"state":"staged","artifact":"update.bin","version":"2.0.0"})");
    std::string error;
    require(stagedTransactionState(metadata) == StagedTransactionState::Staged,
            "new transaction must begin staged");
    require(transitionStagedTransaction(metadata, StagedTransactionState::Staged,
                                        StagedTransactionState::Applying, &error),
            "handoff must atomically consume staged transaction");
    require(!transitionStagedTransaction(metadata, StagedTransactionState::Staged,
                                         StagedTransactionState::Applying, &error),
            "restart must not consume an applying transaction twice");
    require(transitionStagedTransaction(metadata, StagedTransactionState::Applying,
                                        StagedTransactionState::Staged, &error),
            "immediate handoff failure must restore retryable state");
    require(transitionStagedTransaction(metadata, StagedTransactionState::Staged,
                                        StagedTransactionState::Applying, &error),
            "restored transaction must be retryable");
    require(transitionStagedTransaction(metadata, StagedTransactionState::Applying,
                                        StagedTransactionState::Committed, &error),
            "verified replacement must commit transaction");
    require(!transitionStagedTransaction(metadata, StagedTransactionState::Staged,
                                         StagedTransactionState::Applying, &error),
            "committed transaction must never reapply");
}

void localSecurityTests(const fs::path& root) {
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path running = root / "running-app";
    writeFile(running, "RUNNING APP MUST STAY UNTOUCHED\n");
    const std::string original = readFile(running);

    Updater updater;
    updater.configure("1.0.0", root / "data", running);
    std::string error;

#ifdef CHUDWARE_DEVELOPER_BUILD
    const fs::path valid = root / "valid.download";
    Manifest relabeled = validDomainManifest();
    relabeled.version = "1.2.3";
    const fs::path relabeledPath = root / "relabeled.download";
    writeFile(relabeledPath, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(relabeled, relabeledPath, &error),
            "artifact signature relabeled to a newer version must be rejected");
    Manifest wrongPlatform = validDomainManifest();
    wrongPlatform.platform = "windows";
    writeFile(relabeledPath, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(wrongPlatform, relabeledPath, &error),
            "artifact signature relabeled to another platform must be rejected");

    Updater equalUpdater;
    equalUpdater.configure("1.2.2", root / "equal-data", running);
    writeFile(relabeledPath, "signed artifact bytes\n");
    require(!equalUpdater.verifyAndStageDownloaded(validDomainManifest(), relabeledPath, &error),
            "equal semantic version must not stage");
    require(error.find("strict semantic-version upgrade") != std::string::npos,
            "equal version rejection must identify upgrade requirement");
    Updater downgradeUpdater;
    downgradeUpdater.configure("2.0.0", root / "downgrade-data", running);
    writeFile(relabeledPath, "signed artifact bytes\n");
    require(!downgradeUpdater.verifyAndStageDownloaded(validDomainManifest(), relabeledPath, &error),
            "downgrade must not stage");
    fs::remove_all(root / "data/updates");
    updater.configure("1.0.0", root / "data", running);
    writeFile(valid, "signed artifact bytes\n");
    require(updater.verifyAndStageDownloaded(validDomainManifest(), valid, &error),
            "artifact-v1 domain signature with known key id must verify");
    require(readFile(root / "data/updates/staged.json").find("\"key_id\":\"dev-test-v1\"") != std::string::npos,
            "staged metadata must retain the verified release key id");
    fs::remove_all(root / "data/updates");
#endif

    const fs::path corrupt = root / "corrupt.download";
    writeFile(corrupt, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(corruptManifest(), corrupt, &error),
            "corrupt artifact must be rejected");
    require(!fs::exists(corrupt), "corrupt artifact must be deleted");
    require(updater.snapshot().reportSerial == 1, "corrupt artifact must be reported once");
    writeFile(corrupt, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(corruptManifest(), corrupt, &error),
            "same corrupt artifact must still be rejected");
    require(!fs::exists(corrupt), "retried corrupt artifact must be deleted");
    require(updater.snapshot().reportSerial == 1,
            "same corrupt artifact must not be reported every retry loop");

    const fs::path wrong = root / "wrong-key.download";
    writeFile(wrong, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(wrongKeyManifest(), wrong, &error),
            "right checksum with wrong signing key must be rejected");
    require(error.find("Ed25519") != std::string::npos,
            "wrong-key rejection must name signature verification");
    require(!fs::exists(wrong), "wrong-key artifact must be deleted");
    require(updater.snapshot().reportSerial == 2, "wrong-key artifact must be reported once");
    writeFile(wrong, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(wrongKeyManifest(), wrong, &error),
            "retried wrong-key artifact must still be rejected");
    require(updater.snapshot().reportSerial == 2,
            "same wrong-key artifact must not be reported every retry loop");

    const fs::path unknown = root / "unknown-key.download";
    writeFile(unknown, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(unknownKeyManifest(), unknown, &error),
            "unknown release key id must be rejected");
    require(error.find("key id") != std::string::npos,
            "unknown-key rejection must name the release key id");
    require(!fs::exists(unknown), "unknown-key artifact must be deleted");
    require(updater.snapshot().reportSerial == 3,
            "unknown release key id must be reported once");

    Manifest truncated = wrongKeyManifest();
    truncated.size = 23;
    const fs::path shortDownload = root / "short.download";
    writeFile(shortDownload, "signed artifact bytes\n");
    require(!updater.verifyAndStageDownloaded(truncated, shortDownload, &error),
            "artifact shorter than the manifest size must be rejected");
    require(error.find("size does not match") != std::string::npos,
            "size rejection must name the size check");
    require(!fs::exists(shortDownload), "size-mismatched artifact must be deleted");

    require(readFile(running) == original, "running application was modified before verification");
    require(!fs::exists(root / "data/updates/staged.json"),
            "rejected artifact must never create staged metadata");
    std::cout << "security: corrupt=rejected+deleted+reported-once wrong-key=rejected+deleted+reported-once size-mismatch=rejected+deleted running-app=untouched\n";
}


#if defined(__linux__) && defined(CHUDWARE_DEVELOPER_BUILD)
void innerPayloadMismatchTest(const fs::path& root) {
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path running = root / "running.AppImage";
    const std::string original = "RUNNING APP MUST STAY UNTOUCHED\n";
    writeFile(running, original);
    const fs::path artifact = root / "hostile.AppImage";
    const std::string inner =
        "{\"arch\":\"x86_64\",\"created_at\":\"2026-08-12T00:00:00Z\","
        "\"files\":[],\"key_id\":\"dev-test-v1\",\"platform\":\"linux\","
        "\"product\":\"Chudware\",\"schema\":\"chudware-payload-v1\","
        "\"version\":\"1.2.3\"}";
    writeFile(artifact,
        "#!/bin/sh\n[ \"$1\" = --appimage-extract ] || exit 2\n"
        "mkdir -p squashfs-root/usr/bin\n"
        "cat > squashfs-root/payload-manifest.v1.json <<'EOF'\n" + inner +
        "\nEOF\nprintf '%s' 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==' > squashfs-root/payload-manifest.v1.sig\n"
        "exit 0\n");
    // 0600, exactly as a streamed download lands: the inspector must make the
    // artifact executable itself, or every real update is rejected before its
    // inner payload is ever read.
    fs::permissions(artifact, fs::perms::owner_read | fs::perms::owner_write);
    Manifest outer;
    outer.version = "1.2.4";
    outer.platform = "linux";
    outer.arch = "x86_64";
    outer.keyId = "dev-test-v1";
    outer.sha256 = "ff7996098767e61f9636a82732d9df71498a7e3e4158502c2afc3f5a0934326e";
    outer.size = 465;
    outer.signature = "p10PNvDDkd9HSRzCZIfElWxtBlgvMSSxHiNQ0sG8fgii6BvLHzURsp4tqZsgzkIF0T6tMk19zvVKa+kBs74oBQ==";
    outer.url = "fixture://signed-outer";
    Updater updater;
    updater.configure("1.0.0", root / "data", running);
    std::string error;
    require(!updater.verifyAndStageDownloaded(outer, artifact, &error),
            "valid outer artifact with invalid inner payload must be rejected");
    require(error.find("inner payload integrity failed") != std::string::npos ||
                error.find("Ed25519 identity signature") != std::string::npos,
            "payload rejection must identify the failing signed trust layer");
    require(!fs::exists(artifact), "inner-invalid artifact must be deleted");
    require(!fs::exists(root / "data/updates/staged.json"),
            "inner-invalid artifact must not create staged metadata");
    require(readFile(running) == original,
            "inner-invalid artifact must not touch the running fixture");
}
#endif

#if defined(_WIN32)
void windowsRollbackTests(const fs::path& root) {
    fs::remove_all(root);
    const fs::path install = root / "rollback-install";
    const fs::path rollback = root / "rollback-copy";
    fs::create_directories(install);
    fs::create_directories(rollback);
    writeFile(install / "owned.exe", "changed");
    writeFile(install / "unowned.txt", "leave me");
    writeFile(install / "new-owned.dll", "new file");
    writeFile(rollback / "owned.exe", "original");
    const fs::path state = root / "rollback-state.json";
    writeFile(state,
              R"({"files":[{"path":"owned.exe","existed":true},{"path":"new-owned.dll","existed":false}]})");
    std::string error;
    require(chudware::update::testing::restoreWindowsSnapshotForTest(
                install, rollback, state, &error),
            "Windows rollback must restore a valid snapshot");
    require(readFile(install / "owned.exe") == "original",
            "Windows rollback must restore exact owned bytes");
    require(!fs::exists(install / "new-owned.dll"),
            "Windows rollback must remove newly installed owned files");
    require(readFile(install / "unowned.txt") == "leave me",
            "Windows rollback must never alter unowned files");
}
#endif

void servedSecurityTest(const std::string& kind, const std::string& url,
                        const fs::path& root) {
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path running = root / "running-app";
    writeFile(running, "RUNNING APP MUST STAY UNTOUCHED\n");
    const std::string original = readFile(running);
    Updater updater;
    updater.configure("1.0.0", root / "data", running);
    Manifest manifest = kind == "wrong-key" ? wrongKeyManifest() : corruptManifest();
    manifest.url = url;
    std::string error;
    require(!updater.downloadAndStage(manifest, &error), "served hostile artifact must be rejected");
    if (updater.snapshot().reportSerial != 1)
        std::cerr << "served rejection detail: " << error << '\n';
    const auto first = updater.snapshot().reportSerial;
    require(first == 1, "served hostile artifact must be reported once");
    require(!updater.downloadAndStage(manifest, &error), "served hostile retry must be rejected");
    require(updater.snapshot().reportSerial == first, "served hostile retry must not re-report");
    require(readFile(running) == original, "served hostile artifact touched running app");
    require(!fs::exists(root / "data/updates/staged.json"), "served hostile artifact was staged");
    std::cout << "served-security: " << kind
              << " rejected=yes deleted=yes reported=1 running-app=untouched\n";
}

// A 404 for the manifest means no release for this target has been published
// yet. That is the normal state before the first release and must read as a
// quiet up-to-date, never as an HTTP error in the user's face.
void noReleaseTest(const std::string& endpoint, const fs::path& root) {
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path running = root / "running-app";
    writeFile(running, "RUNNING APP MUST STAY UNTOUCHED\n");
    setEnv("CHUDWARE_UPDATE_MANIFEST_URL", endpoint.c_str());
    Updater updater;
    updater.configure("0.0.9", root / "data", running);
    updater.start();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
    for (;;) {
        const auto snap = updater.snapshot();
        // Idle (worker not yet scheduled) and Checking are both non-terminal.
        if (snap.state != State::Idle && snap.state != State::Checking) {
            require(snap.state == State::Current,
                    "missing release manifest (404) must map to Current, not Error");
            require(snap.message == "Chudware is up to date",
                    "missing release manifest must not surface an HTTP status to the user");
            break;
        }
        require(std::chrono::steady_clock::now() < deadline,
                "first update check never completed");
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    updater.stop();
    std::cout << "check-once: no-release-404=current-quiet\n";
}

// Exercise the production manifest poller as well as the artifact verifier.
// Legacy minimum-version metadata must not change the ordinary update state,
// even when it is much higher than both the running and offered versions.
void manifestMetadataTest(const std::string& kind, const std::string& endpoint,
                          const fs::path& root) {
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path running = root / "running-app";
    const std::string original = "RUNNING APP MUST STAY UNTOUCHED\n";
    writeFile(running, original);
    setEnv("CHUDWARE_UPDATE_MANIFEST_URL", endpoint.c_str());
    const State expected = kind == "invalid-signature" ? State::Error
        : kind == "current" ? State::Current
        : kind == "available" ? State::Available : State::Staged;
    Updater updater;
    updater.configure(kind == "current" ? "1.2.2" : "1.0.0", root / "data", running);
    updater.start();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
    for (;;) {
        const auto snap = updater.snapshot();
        if (snap.state == expected) break;
        require(snap.state == State::Idle || snap.state == State::Checking ||
                    (expected != State::Available &&
                     (snap.state == State::Available || snap.state == State::Downloading)),
                ("unexpected manifest update state: " + snap.message).c_str());
        require(std::chrono::steady_clock::now() < deadline,
                ("manifest update never reached the expected state: " + snap.message).c_str());
        std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }
    updater.stop();
    const auto snap = updater.snapshot();
    require(snap.state == expected, "manifest update state must remain nonblocking");
    require(readFile(running) == original, "manifest check or staging modified the running app");
    const fs::path metadata = root / "data/updates/staged.json";
    if (expected == State::Staged) {
        require(chudware::update::testing::stagedTransactionState(metadata) ==
                    chudware::update::testing::StagedTransactionState::Staged,
                "verified manifest update must persist a staged transaction");
        require(snap.version == "1.2.2", "staged update must retain the verified version");
        require(snap.reportSerial == 0, "valid manifest update must not report a rejection");
    } else {
        require(!fs::exists(metadata), "unstaged manifest update must not persist a transaction");
        if (expected == State::Error) {
            require(snap.reportSerial == 1, "invalid manifest artifact must report its rejection");
            require(snap.message.find("Ed25519") != std::string::npos,
                    "high minimum metadata must not bypass signature verification");
            for (const auto& entry : fs::directory_iterator(root / "data/updates"))
                require(entry.path().filename() == "reported-rejections.txt",
                        "invalid manifest artifact must be deleted rather than staged");
        }
    }
    std::cout << "manifest-metadata: " << kind << " state=nonblocking running-app=untouched\n";
}

// Release verification against a REAL published release: point the production
// check-and-stage path at a live manifest URL (for example
// releaseManifestUrl("linux", "x86_64")) and require that it fetches the
// manifest, follows the release-asset redirect, downloads the artifact, and
// passes every verification gate (size, SHA-256, Ed25519 over the release
// keyring, and the inner payload manifest) before the atomic stage rename.
// Only runs when invoked with --live, so ctest never depends on the network.
void liveReleaseTest(const std::string& endpoint, const std::string& currentVersion,
                     const fs::path& root) {
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path running = root / "running-app";
    writeFile(running, "RUNNING APP MUST STAY UNTOUCHED\n");
    setEnv("CHUDWARE_UPDATE_MANIFEST_URL", endpoint.c_str());
    Updater updater;
    updater.configure(currentVersion, root / "data", running);
    updater.start();
    // Staged is published only after the verified artifact and transaction
    // metadata have both been atomically persisted.
    const fs::path metadata = root / "data/updates/staged.json";
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::minutes(20);
    std::string last;
    for (;;) {
        const auto snap = updater.snapshot();
        if (snap.message != last) {
            last = snap.message;
            std::cout << "live: [" << static_cast<int>(snap.state) << "] " << last
                      << std::endl;
        }
        if (snap.state == State::Staged) {
            std::cout << "live: staged " << snap.version << " over " << currentVersion << '\n';
            require(fs::exists(metadata), "staged update must have persisted metadata");
            break;
        }
        require(snap.state != State::Error && snap.state != State::ManualDownload &&
                    snap.state != State::Current,
                ("live release did not stage an update: " + snap.message).c_str());
        require(std::chrono::steady_clock::now() < deadline,
                ("live update never staged; last status: " + last).c_str());
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    updater.stop();
    require(readFile(running) == "RUNNING APP MUST STAY UNTOUCHED\n",
            "staging must not touch the running app");
    require(fs::exists(metadata), "live staged transaction metadata is missing");
    require(chudware::update::testing::stagedTransactionState(metadata) ==
                chudware::update::testing::StagedTransactionState::Staged,
            "live staged transaction is not in the Staged state");
    std::uintmax_t staged = 0;
    for (const auto& entry : fs::directory_iterator(root / "data/updates"))
        if (entry.is_regular_file() && entry.path().extension() != ".json")
            staged = std::max(staged, entry.file_size());
    require(staged > 0, "live staged artifact is empty");
    std::cout << "live: verified size+sha256+ed25519+inner-payload, staged " << staged
              << " bytes, running app untouched\n";
}

// The other half of an update: install the staged artifact over the running
// one. applyPlatformArtifact ends in execv, which would launch the GUI, so run
// this with the install directory on a noexec mount — the replacement is
// already on disk by then, and the blocked relaunch is what makes the result
// inspectable:
//   unshare -Ur -m sh -c 'mount -t tmpfs -o noexec tmpfs /mnt &&
//     test_updater --apply-once <staged root> /mnt/Chudware.AppImage'
void applyStagedOnceTest(const fs::path& root, const fs::path& installed) {
    const fs::path metadata = root / "data/updates/staged.json";
    require(fs::exists(metadata), "no staged update under this root; run --live first");
    fs::path staged;
    for (const auto& entry : fs::directory_iterator(root / "data/updates"))
        if (entry.is_regular_file() && entry.path().extension() != ".json")
            staged = entry.path();
    require(!staged.empty(), "staged artifact is missing");
    writeFile(installed, "OLD INSTALLED BUILD\n");
#ifndef _WIN32
    setenv("APPIMAGE", installed.c_str(), 1);
#endif
    Updater updater;
    updater.configure("0.1.0-beta.0", root / "data", installed);
    const auto result = updater.applyStagedBeforeWindow();
    const char* name = result == chudware::update::ApplyResult::Relaunching ? "Relaunching"
        : result == chudware::update::ApplyResult::Failed ? "Failed"
        : result == chudware::update::ApplyResult::ManualDownload ? "ManualDownload"
        : result == chudware::update::ApplyResult::Rejected ? "Rejected" : "NothingStaged";
    std::cout << "apply-once: result=" << name << std::endl;
    require(result == chudware::update::ApplyResult::Relaunching ||
                result == chudware::update::ApplyResult::Failed,
            "apply must reach the relaunch, not refuse the staged update");
    require(fs::file_size(installed) == fs::file_size(staged),
            "installed file is not the staged artifact's size");
    std::ifstream left(installed, std::ios::binary), right(staged, std::ios::binary);
    std::vector<char> a(1 << 20), b(1 << 20);
    std::uintmax_t compared = 0;
    while (left.read(a.data(), static_cast<std::streamsize>(a.size())) || left.gcount()) {
        right.read(b.data(), left.gcount());
        require(right.gcount() == left.gcount() &&
                    std::equal(a.begin(), a.begin() + left.gcount(), b.begin()),
                "installed bytes differ from the staged artifact");
        compared += static_cast<std::uintmax_t>(left.gcount());
    }
    require((fs::status(installed).permissions() & fs::perms::owner_exec) != fs::perms::none,
            "installed AppImage must be executable");
    std::cout << "apply-once: installed " << compared << " staged bytes over " << installed
              << ", executable, relaunch " << name << '\n';
}

}  // namespace

int main(int argc, char** argv) {
    versionTests();
    manifestUrlTests();
    releaseKeyringTests();
    stagedLifecycleTests(fs::temp_directory_path() / "chudware-updater-state-test");
    if (argc == 5 && std::string(argv[1]) == "--served") {
        servedSecurityTest(argv[2], argv[3], argv[4]);
    } else if (argc == 4 && std::string(argv[1]) == "--check-once") {
        noReleaseTest(argv[2], argv[3]);
    } else if (argc == 5 && std::string(argv[1]) == "--manifest-metadata") {
        manifestMetadataTest(argv[2], argv[3], argv[4]);
    } else if (argc == 5 && std::string(argv[1]) == "--live") {
        liveReleaseTest(argv[2], argv[3], argv[4]);
    } else if (argc == 4 && std::string(argv[1]) == "--apply-once") {
        applyStagedOnceTest(argv[2], argv[3]);
    } else {
        localSecurityTests(fs::temp_directory_path() / "chudware-updater-test");
    }
#if defined(__linux__) && defined(CHUDWARE_DEVELOPER_BUILD)
        if (argc == 1)
            innerPayloadMismatchTest(fs::temp_directory_path() / "chudware-updater-inner-test");
#endif
#if defined(_WIN32)
        windowsRollbackTests(fs::temp_directory_path() / "chudware-updater-test");
#endif
    std::cout << "versions: semver+prerelease+beta-order=ok\n";
    std::cout << "manifest-url: github-release-asset per platform/arch, env override=ok\n";
    std::cout << "keyring: release-v1+release-ci-v1 trusted, unknown rejected\n";
    return 0;
}
