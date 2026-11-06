#include "scad_process.h"

#ifdef __APPLE__
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <filesystem>
#include <poll.h>
#include <signal.h>
#include <string>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace chudware::util::scad_process::detail {
namespace {
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
constexpr size_t max_diagnostics = 4 * 1024 * 1024;
constexpr size_t max_artifact = 1024ULL * 1024 * 1024;
constexpr size_t max_input = 256ULL * 1024 * 1024;
constexpr size_t max_entries = 4096;

Result fail(Status status, std::string message) { return {status, -1, std::move(message)}; }
bool nul(const std::string& s) { return s.find('\0') != std::string::npos; }

struct Fd {
    int value = -1;
    explicit Fd(int fd = -1) : value(fd) {}
    ~Fd() { if (value >= 0) ::close(value); }
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;
};
struct Temp {
    fs::path path;
    ~Temp() { if (!path.empty()) { std::error_code ec; fs::remove_all(path, ec); } }
    bool create() {
        char pattern[] = "/private/tmp/chudware-scad-XXXXXX";
        char* p = ::mkdtemp(pattern);
        if (!p) return false;
        path = p;
        return true;
    }
};

fs::path program(const std::string& name) {
    if (name.empty() || nul(name)) return {};
    auto check = [](const fs::path& p) -> fs::path {
        std::error_code ec;
        auto c = fs::canonical(p, ec);
        struct stat st{};
        if (ec || ::stat(c.c_str(), &st) || !S_ISREG(st.st_mode) || ::access(c.c_str(), X_OK)) return {};
        return c;
    };
    if (name.find('/') != std::string::npos) return check(name);
    const char* env = ::getenv("PATH");
    std::string paths = env ? env : "";
    size_t begin = 0;
    while (begin <= paths.size()) {
        size_t end = paths.find(':', begin);
        if (end == std::string::npos) end = paths.size();
        fs::path dir(paths.substr(begin, end - begin));
        if (dir.is_absolute()) { auto p = check(dir / name); if (!p.empty()) return p; }
        begin = end + 1;
    }
    return {};
}

bool write_all(int fd, const char* data, size_t size) {
    while (size) {
        ssize_t n = ::write(fd, data, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        data += n; size -= static_cast<size_t>(n);
    }
    return true;
}

// Every component is opened relative to an already opened directory, and never
// followed through a symlink. Count actual bytes read, not a racy stat size.
bool copy_tree(int src, int dst, size_t& entries, size_t& bytes, Clock::time_point end,
               unsigned depth, std::string& error) {
    if (depth > 128) { error = "SCAD project nesting is too deep"; return false; }
    int duplicate = ::dup(src);
    if (duplicate < 0) { error = "Cannot enumerate SCAD project"; return false; }
    DIR* dir = ::fdopendir(duplicate);
    if (!dir) { ::close(duplicate); error = "Cannot enumerate SCAD project"; return false; }
    bool ok = true;
    for (;;) {
        if (Clock::now() >= end) { error = "SCAD project staging exceeded its deadline"; ok = false; break; }
        errno = 0;
        dirent* ent = ::readdir(dir);
        if (!ent) { if (errno) { error = "Cannot enumerate SCAD project"; ok = false; } break; }
        if (!std::strcmp(ent->d_name, ".") || !std::strcmp(ent->d_name, "..")) continue;
        if (++entries > max_entries) { error = "SCAD project has too many entries"; ok = false; break; }
        struct stat st{};
        if (::fstatat(src, ent->d_name, &st, AT_SYMLINK_NOFOLLOW) ||
            (!S_ISREG(st.st_mode) && !S_ISDIR(st.st_mode))) {
            error = "SCAD project may contain only regular files and directories, not links or devices"; ok = false; break;
        }
        if (S_ISDIR(st.st_mode)) {
            Fd in(::openat(src, ent->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
            if (in.value < 0 || ::mkdirat(dst, ent->d_name, 0700)) { error = "Cannot stage SCAD directory"; ok = false; break; }
            Fd out(::openat(dst, ent->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
            if (out.value < 0 || !copy_tree(in.value, out.value, entries, bytes, end, depth + 1, error)) { ok = false; break; }
        } else {
            Fd in(::openat(src, ent->d_name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
            struct stat actual{};
            if (in.value < 0 || ::fstat(in.value, &actual) || !S_ISREG(actual.st_mode) ||
                actual.st_size < 0 || static_cast<uint64_t>(actual.st_size) > max_input - bytes) {
                error = "Invalid or oversized SCAD project input"; ok = false; break;
            }
            Fd out(::openat(dst, ent->d_name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600));
            if (out.value < 0) { error = "Cannot stage SCAD input"; ok = false; break; }
            char buffer[32768];
            for (;;) {
                if (Clock::now() >= end) { error = "SCAD project staging exceeded its deadline"; ok = false; break; }
                ssize_t n = ::read(in.value, buffer, sizeof(buffer));
                if (n < 0 && errno == EINTR) continue;
                if (n < 0 || (n > 0 && static_cast<size_t>(n) > max_input - bytes)) { error = "Cannot read bounded SCAD input"; ok = false; break; }
                if (!n) break;
                bytes += static_cast<size_t>(n);
                if (!write_all(out.value, buffer, static_cast<size_t>(n))) { error = "Cannot stage SCAD input"; ok = false; break; }
            }
            if (!ok) break;
        }
    }
    ::closedir(dir);
    return ok;
}

bool beneath(const fs::path& p, const fs::path& root) {
    auto a = p.begin(), b = root.begin();
    for (; b != root.end() && a != p.end() && *a == *b; ++a, ++b) {}
    return b == root.end();
}

// SBPL strings are never interpolated unescaped. Reject control characters
// rather than depending on undocumented parser escape forms.
bool quoted(const std::string& s, std::string& out) {
    out = "\"";
    for (unsigned char c : s) {
        if (c < 0x20 || c == 0x7f) return false;
        if (c == '\\' || c == '"') out += '\\';
        out += static_cast<char>(c);
    }
    out += '"';
    return true;
}
bool rule(std::string& profile, const char* operation, const char* kind, const fs::path& p) {
    std::string q;
    if (!quoted(p.string(), q)) return false;
    profile += "(allow "; profile += operation; profile += " ("; profile += kind;
    profile += " "; profile += q; profile += "))\n";
    return true;
}

bool publish(int staged, int destination, const std::string& name, size_t limit,
             Clock::time_point end, std::string& error) {
    Fd in(::openat(staged, name.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
    struct stat st{};
    if (in.value < 0 || ::fstat(in.value, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0 ||
        static_cast<uint64_t>(st.st_size) > limit) { error = "Renderer did not produce a bounded regular output file"; return false; }
    // O_EXCL and renameat anchor the publication to the original opened work directory.
    std::string tmp;
    int fd = -1;
    for (unsigned i = 0; i < 100; ++i) {
        tmp = ".chudware-scad-output-" + std::to_string(::getpid()) + "-" + std::to_string(::arc4random());
        fd = ::openat(destination, tmp.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd >= 0 || errno != EEXIST) break;
    }
    if (fd < 0) { error = "Cannot create temporary output"; return false; }
    bool ok = true; size_t total = 0; char buffer[32768];
    while (ok) {
        if (Clock::now() >= end) { error = "SCAD output publication exceeded its deadline"; ok = false; break; }
        ssize_t n = ::read(in.value, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 || (n > 0 && static_cast<size_t>(n) > limit - total)) { ok = false; break; }
        if (!n) break;
        total += static_cast<size_t>(n);
        ok = write_all(fd, buffer, static_cast<size_t>(n));
    }
    if (::close(fd)) ok = false;
    if (ok && Clock::now() >= end) { error = "SCAD output publication exceeded its deadline"; ok = false; }
    if (ok && ::renameat(destination, tmp.c_str(), destination, name.c_str())) ok = false;
    if (!ok) { ::unlinkat(destination, tmp.c_str(), 0); if (error.empty()) error = "Cannot atomically publish bounded SCAD output"; }
    return ok;
}

Result execute(const Request& r, const fs::path& exe, const fs::path& work,
               Clock::time_point end) {
    // sandbox-exec is deprecated by Apple, but where present it is a kernel
    // enforced deny-default sandbox. No alternative unconfined path exists.
    struct stat sb{};
    const char* sandbox = "/usr/bin/sandbox-exec";
    if (::stat(sandbox, &sb) || !S_ISREG(sb.st_mode) || sb.st_uid != 0 ||
        (sb.st_mode & (S_IWGRP | S_IWOTH)) || ::access(sandbox, X_OK))
        return fail(Status::sandbox_unavailable, "macOS sandbox-exec is unavailable; confined OpenSCAD cannot run");
    std::string profile = "(version 1)\n(deny default)\n"
        "(allow sysctl-read)\n(allow process-info* (target self))\n(allow signal (target self))\n";
    if (!rule(profile, "process-exec", "literal", exe) ||
        !rule(profile, "file-read*", "literal", exe) ||
        !rule(profile, "file-read*", "subpath", work))
        return fail(Status::invalid_request, "SCAD paths contain unsupported control characters");
    if (!r.output_file.empty() && !rule(profile, "file-write-data", "literal", work / r.output_file))
        return fail(Status::invalid_request, "Invalid SCAD output path");
    for (const char* path : {"/usr/lib", "/System/Library", "/Library/Fonts",
                             "/private/var/db/dyld", "/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"})
        if (!rule(profile, "file-read*", "subpath", path)) return fail(Status::invalid_request, "Invalid runtime path");
    for (const char* path : {"/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"})
        if (!rule(profile, "file-read*", "literal", path)) return fail(Status::invalid_request, "Invalid device path");
    if (!rule(profile, "file-write-data", "literal", "/dev/null"))
        return fail(Status::invalid_request, "Invalid device path");
    // Only conventional bundle resource trees; never grant the containing folder.
    fs::path contents = exe.parent_path().parent_path();
    bool bundle = exe.parent_path().filename() == "MacOS" && contents.filename() == "Contents" &&
                  contents.parent_path().extension() == ".app";
    std::vector<fs::path> allowed = {"/System/Library/Fonts", "/Library/Fonts"};
    if (bundle) {
        for (const char* part : {"Frameworks", "Resources", "PlugIns"}) {
            std::error_code ec;
            auto root = fs::canonical(contents / part, ec);
            if (ec) continue;
            if (!beneath(root, contents) || !fs::is_directory(root, ec) || ec)
                return fail(Status::invalid_request, "Renderer bundle resource escapes its package");
            allowed.push_back(root);
            if (!rule(profile, "file-read*", "subpath", root)) return fail(Status::invalid_request, "Invalid bundle path");
        }
    }
    for (const auto& resource : r.renderer_resource_roots) {
        std::error_code ec; auto root = fs::canonical(resource, ec);
        bool accepted = false;
        if (!ec) for (const auto& base : allowed) if (beneath(root, base)) { accepted = true; break; }
        if (!accepted) return fail(Status::invalid_request, "Renderer resource root is not a reviewed bundle or system font resource: " + resource);
    }
    // Traversal metadata only, not recursive access to parent directories.
    for (const auto& p : {exe, work}) {
        for (fs::path parent = p.parent_path(); !parent.empty(); parent = parent.parent_path()) {
            if (!rule(profile, "file-read-metadata", "literal", parent)) return fail(Status::invalid_request, "Invalid SCAD path");
            if (parent == parent.root_path()) break;
        }
    }
    std::vector<std::string> args = {sandbox, "-p", profile, exe.string()};
    args.insert(args.end(), r.arguments.begin(), r.arguments.end());
    std::vector<char*> argv; for (auto& s : args) argv.push_back(s.data()); argv.push_back(nullptr);
    char home[] = "HOME=/nonexistent", config[] = "XDG_CONFIG_HOME=/nonexistent";
    char cache[] = "XDG_CACHE_HOME=/nonexistent", tmp[] = "TMPDIR=/nonexistent";
    char path[] = "PATH=/usr/bin:/bin", qt[] = "QT_QPA_PLATFORM=offscreen";
    char* env[] = {home, config, cache, tmp, path, qt, nullptr};
    int pipes[2];
    if (::pipe(pipes)) return fail(Status::sandbox_unavailable, "Cannot create SCAD diagnostics pipe");
    ::fcntl(pipes[0], F_SETFD, FD_CLOEXEC); ::fcntl(pipes[1], F_SETFD, FD_CLOEXEC);
    int output = ::fcntl(pipes[1], F_DUPFD_CLOEXEC, 10);
    if (output < 0) { ::close(pipes[0]); ::close(pipes[1]); return fail(Status::sandbox_unavailable, "Cannot prepare SCAD diagnostics pipe"); }
    pid_t pid = ::fork();
    if (!pid) {
        ::setpgid(0, 0);
        int nullfd = ::open("/dev/null", O_RDONLY);
        if (nullfd < 0 || ::dup2(nullfd, 0) < 0 || ::dup2(output, 1) < 0 || ::dup2(output, 2) < 0 || ::chdir(work.c_str())) ::_exit(125);
        for (int fd = 3; fd < 65536; ++fd) ::close(fd);
        rlim_t seconds = static_cast<rlim_t>((r.deadline.count() + 999) / 1000 + 1);
        struct rlimit cpu{seconds, seconds}, memory{2ULL * 1024 * 1024 * 1024, 2ULL * 1024 * 1024 * 1024};
        struct rlimit file{static_cast<rlim_t>(r.max_output_bytes), static_cast<rlim_t>(r.max_output_bytes)}, descriptors{128, 128};
        if (::setrlimit(RLIMIT_CPU, &cpu) || ::setrlimit(RLIMIT_AS, &memory) ||
            ::setrlimit(RLIMIT_FSIZE, &file) || ::setrlimit(RLIMIT_NOFILE, &descriptors)) ::_exit(125);
        ::execve(sandbox, argv.data(), env); ::_exit(127);
    }
    ::close(output); ::close(pipes[1]);
    if (pid < 0) { ::close(pipes[0]); return fail(Status::sandbox_unavailable, "Cannot start macOS sandbox"); }
    ::setpgid(pid, pid); ::fcntl(pipes[0], F_SETFL, O_NONBLOCK);
    std::string diagnostics; int status = 0; bool timeout = false, wait_error = false;
    auto drain = [&]() {
        char buf[4096];
        for (int i = 0; i < 16; ++i) {
            ssize_t n = ::read(pipes[0], buf, sizeof(buf));
            if (n <= 0) break;
            diagnostics.append(buf, std::min(r.max_diagnostic_bytes - diagnostics.size(), static_cast<size_t>(n)));
        }
    };
    for (;;) {
        drain();
        if (Clock::now() >= end) { timeout = true; break; }
        pid_t result = ::waitpid(pid, &status, WNOHANG);
        if (result == pid) break;
        if (result < 0 && errno != EINTR) { wait_error = true; break; }
        struct pollfd pfd{pipes[0], POLLIN, 0}; ::poll(&pfd, 1, 10);
    }
    if (timeout || wait_error) {
        ::kill(-pid, SIGKILL); ::kill(pid, SIGKILL);
        while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    }
    drain(); ::close(pipes[0]);
    if (timeout) return {Status::timed_out, -1, "OpenSCAD exceeded its deadline. " + diagnostics};
    if (wait_error) return {Status::sandbox_unavailable, -1, "Could not wait for macOS sandbox. " + diagnostics};
    if (WIFSIGNALED(status)) return {Status::resource_limit, -1, "OpenSCAD terminated by signal " + std::to_string(WTERMSIG(status)) + ". " + diagnostics};
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    if (code == 125 || code == 127 || diagnostics.find("sandbox-exec: sandbox_") != std::string::npos)
        return {Status::sandbox_unavailable, code, "macOS sandbox-exec could not establish confinement. " + diagnostics};
    if (code != 0) return {Status::model_failure, code, std::move(diagnostics)};
    return {Status::success, 0, std::move(diagnostics)};
}
} // namespace

Result run_macos(const Request& request) {
    if (request.deadline.count() <= 0 || request.deadline > std::chrono::hours(1) ||
        !request.max_diagnostic_bytes || request.max_diagnostic_bytes > max_diagnostics ||
        !request.max_output_bytes || request.max_output_bytes > max_artifact ||
        nul(request.work_dir) || nul(request.output_file) || request.output_file == "." || request.output_file == ".." ||
        request.output_file.find('/') != std::string::npos ||
        std::any_of(request.arguments.begin(), request.arguments.end(), nul) ||
        std::any_of(request.renderer_resource_roots.begin(), request.renderer_resource_roots.end(), nul))
        return fail(Status::invalid_request, "Invalid SCAD process limits, arguments or output filename");
    const auto end = Clock::now() + request.deadline;
    auto exe = program(request.executable);
    if (exe.empty()) return fail(Status::dependency_unavailable, "OpenSCAD executable not found or not executable; install OpenSCAD or configure CHUDWARE_OPENSCAD_BIN");
    if (request.work_dir.empty()) return fail(Status::invalid_request, "A SCAD work directory is required");
    Fd source(::open(request.work_dir.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    if (source.value < 0) return fail(Status::invalid_request, "SCAD work directory must be a real directory");
    Temp temp;
    if (!temp.create()) return fail(Status::sandbox_unavailable, "Cannot create private SCAD staging directory");
    fs::path work = temp.path / "project";
    if (::mkdir(work.c_str(), 0700)) return fail(Status::sandbox_unavailable, "Cannot create SCAD project staging directory");
    Fd staged(::open(work.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    if (staged.value < 0) return fail(Status::sandbox_unavailable, "Cannot open SCAD project staging directory");
    size_t count = 0, bytes = 0; std::string error;
    if (!copy_tree(source.value, staged.value, count, bytes, end, 0, error))
        return fail(Clock::now() >= end ? Status::timed_out : Status::invalid_request, std::move(error));
    if (!request.output_file.empty()) {
        if (::unlinkat(staged.value, request.output_file.c_str(), 0) && errno != ENOENT)
            return fail(Status::invalid_request, "Cannot prepare SCAD output filename");
        Fd out(::openat(staged.value, request.output_file.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600));
        if (out.value < 0) return fail(Status::invalid_request, "Cannot create SCAD output file");
    }
    if (Clock::now() >= end) return fail(Status::timed_out, "SCAD project staging exceeded its deadline");
    Result result = execute(request, exe, work, end);
    if (result.status == Status::success && !request.output_file.empty() &&
        !publish(staged.value, source.value, request.output_file, request.max_output_bytes, end, error)) {
        result.status = Clock::now() >= end ? Status::timed_out : Status::resource_limit;
        result.diagnostics += "\n" + error;
    }
    if (result.diagnostics.size() > request.max_diagnostic_bytes) result.diagnostics.resize(request.max_diagnostic_bytes);
    return result;
}

Result capability_macos(const std::string& executable) {
    Temp temp;
    if (!temp.create()) return fail(Status::sandbox_unavailable, "Cannot create SCAD capability directory");
    Request request;
    request.executable = executable; request.arguments = {"--version"};
    request.work_dir = temp.path.string(); request.deadline = std::chrono::seconds(10);
    return run_macos(request);
}
} // namespace chudware::util::scad_process::detail
#endif
