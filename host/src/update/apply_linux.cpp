#include "updater.h"
#include "security/integrity.h"

#if defined(__linux__)

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <sys/wait.h>
#include <vector>

namespace chudware::update {
namespace {

class TemporaryDirectory {
public:
    TemporaryDirectory() {
        std::string pattern = (std::filesystem::temp_directory_path() /
                               "chudware-appimage-XXXXXX").string();
        storage_.assign(pattern.begin(), pattern.end());
        storage_.push_back('\0');
        if (char* made = ::mkdtemp(storage_.data())) path_ = made;
    }
    ~TemporaryDirectory() {
        std::error_code ignored;
        if (!path_.empty()) std::filesystem::remove_all(path_, ignored);
    }
    const std::filesystem::path& path() const { return path_; }

private:
    std::vector<char> storage_;
    std::filesystem::path path_;
};

bool runAppImageExtract(const std::filesystem::path& artifact,
                        const std::filesystem::path& destination,
                        std::string* error) {
    const pid_t child = ::fork();
    if (child < 0) {
        if (error) *error = "could not fork AppImage extractor: " +
                            std::string(std::strerror(errno));
        return false;
    }
    if (child == 0) {
        if (::chdir(destination.c_str()) != 0) ::_exit(126);
        ::execl(artifact.c_str(), artifact.c_str(), "--appimage-extract",
                static_cast<char*>(nullptr));
        ::_exit(127);
    }
    int status = 0;
    pid_t waited;
    do {
        waited = ::waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    if (waited != child || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        if (error) *error = "could not unpack staged AppImage for payload verification";
        return false;
    }
    return true;
}

bool verifyStagedAppImage(const std::filesystem::path& artifact, std::string* error) {
    TemporaryDirectory temporary;
    if (temporary.path().empty()) {
        if (error) *error = "could not create AppImage verification directory: " +
                            std::string(std::strerror(errno));
        return false;
    }
    if (!runAppImageExtract(std::filesystem::absolute(artifact), temporary.path(), error))
        return false;

    const std::filesystem::path root = temporary.path() / "squashfs-root";
    const auto result = security::verifyPayloadManifest(
        root, root / "payload-manifest.v1.json", root / "payload-manifest.v1.sig",
        security::VerifyMode::Full, root / "usr/bin/chudware");
    if (!result.ok) {
        if (error) *error = "staged AppImage payload verification failed: " + result.detail;
        return false;
    }
    return true;
}

bool copyDurable(const std::filesystem::path& source,
                 const std::filesystem::path& destination,
                 std::string* error) {
    const int in = ::open(source.c_str(), O_RDONLY | O_CLOEXEC);
    if (in < 0) {
        if (error) *error = "cannot open staged AppImage: " + std::string(std::strerror(errno));
        return false;
    }
    const int out = ::open(destination.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0755);
    if (out < 0) {
        if (error) *error = "install location is not writable: " + std::string(std::strerror(errno));
        ::close(in);
        return false;
    }
    char buffer[1024 * 1024];
    bool ok = true;
    while (true) {
        const ssize_t count = ::read(in, buffer, sizeof(buffer));
        if (count == 0) break;
        if (count < 0) {
            if (errno == EINTR) continue;
            if (error) *error = "reading staged AppImage failed: " + std::string(std::strerror(errno));
            ok = false;
            break;
        }
        ssize_t written = 0;
        while (written < count) {
            const ssize_t n = ::write(out, buffer + written, static_cast<std::size_t>(count - written));
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) {
                if (error) *error = "writing AppImage beside installation failed: " +
                                    std::string(std::strerror(errno));
                ok = false;
                break;
            }
            written += n;
        }
        if (!ok) break;
    }
    if (ok && ::fchmod(out, 0755) != 0) {
        if (error) *error = "chmod +x failed: " + std::string(std::strerror(errno));
        ok = false;
    }
    if (ok && ::fsync(out) != 0) {
        if (error) *error = "fsync of replacement AppImage failed: " + std::string(std::strerror(errno));
        ok = false;
    }
    ::close(out);
    ::close(in);
    if (!ok) std::filesystem::remove(destination);
    return ok;
}

}  // namespace

ApplyResult applyPlatformArtifact(const std::filesystem::path& verifiedArtifact,
                                  const std::filesystem::path& executablePath,
                                  const std::string&,
                                  std::string* error) {
    const char* appImage = std::getenv("APPIMAGE");
    const std::filesystem::path installed = appImage && *appImage
        ? std::filesystem::path(appImage) : executablePath;
    if (installed.empty()) {
        if (error) *error = "cannot determine the installed AppImage path";
        return ApplyResult::ManualDownload;
    }
    if (!verifyStagedAppImage(verifiedArtifact, error))
        return ApplyResult::Rejected;
    const std::filesystem::path replacement = installed.string() + ".update";
    if (!copyDurable(verifiedArtifact, replacement, error))
        return ApplyResult::ManualDownload;
    if (::rename(replacement.c_str(), installed.c_str()) != 0) {
        if (error) *error = "atomic AppImage replacement failed: " + std::string(std::strerror(errno));
        std::filesystem::remove(replacement);
        return errno == EACCES || errno == EPERM || errno == EROFS
            ? ApplyResult::ManualDownload : ApplyResult::Failed;
    }
    const int directory = ::open(installed.parent_path().c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory >= 0) {
        (void)::fsync(directory);
        ::close(directory);
    }
    char* const argv[] = {const_cast<char*>(installed.c_str()), nullptr};
    ::execv(installed.c_str(), argv);
    if (error) *error = "updated AppImage installed but relaunch failed: " +
                        std::string(std::strerror(errno));
    return ApplyResult::Failed;
}

}  // namespace chudware::update

#endif
