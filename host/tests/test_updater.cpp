#include "update/updater.h"
#include "update/windows_update_helper.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

namespace fs = std::filesystem;
using chudware::update::Manifest;
using chudware::update::Updater;
using chudware::update::ApplyResult;
using chudware::update::applyPlatformArtifact;

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
    value.minSupported = "1.0.0";
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
    value.minSupported = "1.0.0";
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
    value.minSupported = "1.0.0";
    value.keyId = "dev-test-v1";
    value.sha256 = "45ac4000573891c15d2f881d944f9c98234332693bae7c1410aa2c14ed8a209f";
    value.size = 22;
    value.signature = "87lx6JNOomUKj2m3U0IlXc8zj4Msxq1hyC23kFkb0f/veyakI4BU+uBwWa6iUaj+Ve0oNkuXqdAPpCuwJxcvCQ==";
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
    using chudware::update::requiresMandatoryUpdate;
    require(compareVersions("1.0.0-alpha", "1.0.0") < 0, "prerelease sorts before release");
    require(compareVersions("1.0.0-alpha.1", "1.0.0-alpha.beta") < 0,
            "numeric prerelease sorts before text");
    require(compareVersions("1.0.0+build.1", "1.0.0+build.99") == 0,
            "build metadata does not affect precedence");
    require(requiresMandatoryUpdate("0.0.0", "0.0.1"), "0.0.0 build hits kill switch");
    require(requiresMandatoryUpdate("1.0.0-rc.1", "1.0.0"),
            "prerelease build hits release kill switch");
    require(!requiresMandatoryUpdate("1.2.0", "1.0.0"), "supported release is not killed");
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

    require(readFile(running) == original, "running application was modified before verification");
    require(!fs::exists(root / "data/updates/staged.json"),
            "rejected artifact must never create staged metadata");
    std::cout << "security: corrupt=rejected+deleted+reported-once wrong-key=rejected+deleted+reported-once running-app=untouched\n";
}
#if defined(__linux__)
void linuxInnerManifestFailureTest(const fs::path& root) {
    fs::remove_all(root);
    fs::create_directories(root);
    const fs::path running = root / "installed.AppImage";
    const std::string original = "RUNNING APP MUST STAY UNTOUCHED\n";
    writeFile(running, original);

    const fs::path staged = root / "staged.AppImage";
    const std::string manifest =
        "{\"arch\":\"x86_64\",\"created_at\":\"2026-08-12T00:00:00Z\","
        "\"files\":[],\"key_id\":\"dev-test-v1\",\"platform\":\"linux\","
        "\"product\":\"Chudware\",\"schema\":\"chudware-payload-v1\","
        "\"version\":\"1.2.3\"}";
    writeFile(staged,
              "#!/bin/sh\n"
              "[ \"$1\" = --appimage-extract ] || exit 2\n"
              "mkdir -p squashfs-root/usr/bin\n"
              "cat > squashfs-root/payload-manifest.v1.json <<'EOF'\n" + manifest +
              "\nEOF\n"
              "printf '%s' 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==' > squashfs-root/payload-manifest.v1.sig\n"
              "exit 0\n");
    fs::permissions(staged, fs::perms::owner_exec | fs::perms::owner_read |
                            fs::perms::owner_write);

    std::string error;
    require(applyPlatformArtifact(staged, running, "1.2.3", &error) ==
                ApplyResult::Rejected,
            "invalid inner AppImage signature must be rejected before replacement");
    require(error.find("payload verification failed") != std::string::npos,
            "inner AppImage rejection must identify payload verification");
    require(readFile(running) == original,
            "invalid inner AppImage manifest touched installed application");
    require(!fs::exists(running.string() + ".update"),
            "invalid inner AppImage manifest created a replacement file");
    std::cout << "linux-inner-security: invalid-signature=rejected running-app=untouched\n";
}
#endif


#if defined(_WIN32)
void windowsRollbackTests(const fs::path& root) {
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

}  // namespace

int main(int argc, char** argv) {
    versionTests();
    if (argc == 5 && std::string(argv[1]) == "--served") {
        servedSecurityTest(argv[2], argv[3], argv[4]);
    } else {
        localSecurityTests(fs::temp_directory_path() / "chudware-updater-test");
    }
#if defined(__linux__)
        linuxInnerManifestFailureTest(fs::temp_directory_path() /
                                      "chudware-updater-inner-test");
#endif
#if defined(_WIN32)
        windowsRollbackTests(fs::temp_directory_path() / "chudware-updater-test");
#endif
    std::cout << "versions: semver+prerelease+0.0.0+min-supported=ok\n";
    return 0;
}
