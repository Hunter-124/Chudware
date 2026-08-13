#include "updater.h"
#include "security/integrity.h"

#if defined(__APPLE__)

#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace chudware::update {
namespace {

bool runDitto(const std::filesystem::path& archive,
              const std::filesystem::path& destination,
              std::string* error) {
    const pid_t child = ::fork();
    if (child < 0) {
        if (error) *error = "could not fork ditto: " + std::string(std::strerror(errno));
        return false;
    }
    if (child == 0) {
        ::execl("/usr/bin/ditto", "ditto", "-x", "-k", archive.c_str(),
                destination.c_str(), static_cast<char*>(nullptr));
        ::_exit(127);
    }
    int status = 0;
    while (::waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        if (error) *error = "could not expand the verified Chudware zip";
        return false;
    }
    return true;
}

}  // namespace

ApplyResult applyPlatformArtifact(const std::filesystem::path& verifiedArtifact,
                                  const std::filesystem::path& executablePath,
                                  const std::string&,
                                  std::string* error) {
    const std::filesystem::path bundle = executablePath.parent_path().parent_path().parent_path();
    if (bundle.extension() != ".app" || ::access(bundle.c_str(), W_OK) != 0 ||
        ::access(bundle.parent_path().c_str(), W_OK) != 0) {
        if (error) *error = "the installed Chudware.app is not writable";
        return ApplyResult::ManualDownload;
    }

    std::string pattern = (std::filesystem::temp_directory_path() /
                           "chudware-update-XXXXXX").string();
    std::vector<char> mutablePattern(pattern.begin(), pattern.end());
    mutablePattern.push_back('\0');
    char* made = ::mkdtemp(mutablePattern.data());
    if (!made) {
        if (error) *error = "could not create update expansion directory: " +
                            std::string(std::strerror(errno));
        return ApplyResult::Failed;
    }
    const std::filesystem::path expanded(made);
    if (!runDitto(verifiedArtifact, expanded, error)) {
        std::filesystem::remove_all(expanded);
        return ApplyResult::Failed;
    }
    const std::filesystem::path source = expanded / "Chudware.app";
    if (!std::filesystem::is_directory(source)) {
        if (error) *error = "verified update zip does not contain Chudware.app";
        std::filesystem::remove_all(expanded);
        return ApplyResult::Failed;
    }
    const auto integrity = security::verifyPayloadManifest(
        source, source / "payload-manifest.v1.json", source / "payload-manifest.v1.sig",
        security::VerifyMode::Full, source / "Contents/MacOS/Chudware-bin");
    if (!integrity.ok) {
        if (error) *error = "expanded Chudware.app payload verification failed: " +
                            integrity.detail;
        std::filesystem::remove_all(expanded);
        return ApplyResult::Rejected;
    }

    const std::filesystem::path script = expanded / "apply-update.sh";
    {
        std::ofstream out(script, std::ios::binary | std::ios::trunc);
        out << "#!/bin/sh\n"
               "parent=$1\nsource=$2\ntarget=$3\nroot=$4\n"
               "while /bin/kill -0 \"$parent\" 2>/dev/null; do /bin/sleep 1; done\n"
               "/usr/bin/rsync -a --delete \"$source/\" \"$target/\" || exit 1\n"
               "/usr/bin/open \"$target\"\n"
               "/bin/rm -rf \"$root\"\n";
        if (!out) {
            if (error) *error = "could not write detached update script";
            std::filesystem::remove_all(expanded);
            return ApplyResult::Failed;
        }
    }
    if (::chmod(script.c_str(), 0700) != 0) {
        if (error) *error = "could not make detached update script executable";
        std::filesystem::remove_all(expanded);
        return ApplyResult::Failed;
    }
    const std::string parent = std::to_string(static_cast<long long>(::getpid()));
    const pid_t child = ::fork();
    if (child < 0) {
        if (error) *error = "could not detach update script: " + std::string(std::strerror(errno));
        std::filesystem::remove_all(expanded);
        return ApplyResult::Failed;
    }
    if (child == 0) {
        (void)::setsid();
        ::execl("/bin/sh", "sh", script.c_str(), parent.c_str(), source.c_str(),
                bundle.c_str(), expanded.c_str(), static_cast<char*>(nullptr));
        ::_exit(127);
    }
    return ApplyResult::Relaunching;
}

}  // namespace chudware::update

#endif
