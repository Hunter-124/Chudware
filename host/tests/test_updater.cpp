#include "update/updater.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

namespace fs = std::filesystem;
using chudware::update::Manifest;
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
    value.minSupported = "1.0.0";
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
    value.sha256 = "45ac4000573891c15d2f881d944f9c98234332693bae7c1410aa2c14ed8a209f";
    value.size = 22;
    // A real Ed25519 signature over the 64 lowercase digest bytes, produced by
    // private seed 000102...1f. Its public key is intentionally NOT Chudware's.
    value.signature = "UHkVz77DialONbSu3wW4Mk0gV2xyyEnAYDbqJpdNe0eLrGwUXBw8HI2AlL8qZ/8eGd/bgvgXVRXsAGTiVFDJCg==";
    value.notesUrl = "https://chudware.com/releases/1.2.1";
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

    require(readFile(running) == original, "running application was modified before verification");
    require(!fs::exists(root / "data/updates/staged.json"),
            "rejected artifact must never create staged metadata");
    std::cout << "security: corrupt=rejected+deleted+reported-once wrong-key=rejected+deleted+reported-once running-app=untouched\n";
}

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
    if (argc == 4 && std::string(argv[1]) == "--served") {
        const std::string url = argv[2];
        const std::string kind = url.find("wrong-key") != std::string::npos
            ? "wrong-key" : "corrupt";
        servedSecurityTest(kind, url, argv[3]);
    } else {
        localSecurityTests(fs::temp_directory_path() / "chudware-updater-test");
    }
    std::cout << "versions: semver+prerelease+0.0.0+min-supported=ok\n";
    return 0;
}
