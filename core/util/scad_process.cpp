#include "scad_process.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <limits>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#ifdef __linux__
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace chudware::util::scad_process {
namespace fs = std::filesystem;
namespace detail {
#ifdef _WIN32
Result run_windows(const Request&);
Result capability_windows(const std::string&);
#elif defined(__APPLE__)
Result run_macos(const Request&);
Result capability_macos(const std::string&);
#endif
}
namespace {

Result failure(Status status, std::string message) {
    return {status, -1, std::move(message)};
}

#ifdef __linux__
constexpr std::size_t kMaxDiagnostics = 4 * 1024 * 1024;
constexpr std::size_t kMaxArtifact = 1024ULL * 1024 * 1024;
constexpr std::uintmax_t kMaxInput = 256ULL * 1024 * 1024;
constexpr std::size_t kMaxFiles = 4096;

bool contains_nul(const std::string& s) { return s.find('\0') != std::string::npos; }

bool executable(const fs::path& path) {
    std::error_code ec;
    return fs::is_regular_file(path, ec) && ::access(path.c_str(), X_OK) == 0;
}

fs::path find_program(const std::string& name) {
    if (name.empty() || contains_nul(name)) return {};
    if (name.find('/') != std::string::npos) {
        std::error_code ec;
        auto path = fs::canonical(name, ec);
        return !ec && executable(path) ? path : fs::path{};
    }
    const char* raw = ::getenv("PATH");
    std::string paths = raw ? raw : "";
    std::size_t pos = 0;
    while (pos <= paths.size()) {
        auto end = paths.find(':', pos);
        if (end == std::string::npos) end = paths.size();
        // Empty and relative PATH entries are not trusted for executable discovery.
        fs::path dir(paths.substr(pos, end - pos));
        if (dir.is_absolute()) {
            std::error_code ec;
            auto path = fs::canonical(dir / name, ec);
            if (!ec && executable(path)) return path;
        }
        pos = end + 1;
    }
    return {};
}

fs::path find_bubblewrap() {
    // PATH is user-controlled: a substituted bwrap could claim isolation while
    // executing the renderer directly. Only accept a system-owned installation.
    for (const char* candidate : {"/usr/bin/bwrap", "/bin/bwrap"}) {
        std::error_code ec;
        const auto path = fs::canonical(candidate, ec);
        struct stat st{};
        if (!ec && executable(path) && ::stat(path.c_str(), &st) == 0 &&
            st.st_uid == 0 && !(st.st_mode & (S_IWGRP | S_IWOTH))) return path;
    }
    return {};
}

// OpenSCAD can use threads, but cannot launch separate processes. This makes
// RLIMIT_AS and RLIMIT_CPU bound the entire renderer, not each of a fork bomb's
// independently limited children. clone3 returns ENOSYS so libc can use clone.
std::vector<sock_filter> renderer_filter() {
    std::vector<sock_filter> code;
#if defined(__x86_64__) || defined(__aarch64__)
    code.push_back(BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)));
#if defined(__x86_64__)
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0));
#else
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_AARCH64, 1, 0));
#endif
    code.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS));
    code.push_back(BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)));
#if defined(__x86_64__)
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000U, 0, 1));
    code.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM));
#endif
#ifdef __NR_fork
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_fork, 0, 1));
    code.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM));
#endif
#ifdef __NR_vfork
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_vfork, 0, 1));
    code.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM));
#endif
#ifdef __NR_clone3
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone3, 0, 1));
    code.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS));
#endif
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone, 0, 3));
    code.push_back(BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, args[0])));
    code.push_back(BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CLONE_THREAD, 1, 0));
    code.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM));
    code.push_back(BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW));
#endif
    return code;
}

struct TempDir {
    fs::path path;
    ~TempDir() { if (!path.empty()) { std::error_code ec; fs::remove_all(path, ec); } }
    bool create() {
        // Never use ambient TMPDIR: it could expose a user workspace or session path.
        char pattern[] = "/tmp/chudware-scad-XXXXXX";
        char* p = ::mkdtemp(pattern);
        if (!p) return false;
        path = p;
        return true;
    }
};

bool stage_inputs(const fs::path& source, const fs::path& dest, std::string& error) {
    std::error_code ec;
    if (!fs::is_directory(fs::symlink_status(source, ec)) || ec) {
        error = "SCAD work directory must be a real directory";
        return false;
    }
    std::uintmax_t bytes = 0;
    std::size_t count = 0;
    fs::recursive_directory_iterator it(source, fs::directory_options::none, ec), end;
    if (ec) { error = "Cannot read SCAD work directory: " + ec.message(); return false; }
    for (; it != end; it.increment(ec)) {
        if (ec) { error = "Cannot enumerate SCAD project: " + ec.message(); return false; }
        if (++count > kMaxFiles) { error = "SCAD project has too many files"; return false; }
        const auto st = it->symlink_status(ec);
        if (ec || fs::is_symlink(st) || (!fs::is_regular_file(st) && !fs::is_directory(st))) {
            error = "SCAD project may contain only regular files and directories, not links or devices";
            return false;
        }
        const auto target = dest / it->path().lexically_relative(source);
        if (fs::is_directory(st)) {
            fs::create_directory(target, ec);
        } else {
            const auto n = fs::file_size(it->path(), ec);
            if (ec || n > kMaxInput - bytes) { error = "SCAD project exceeds input size limit"; return false; }
            bytes += n;
            fs::copy_file(it->path(), target, fs::copy_options::none, ec);
        }
        if (ec) { error = "Cannot stage SCAD project: " + ec.message(); return false; }
    }
    if (ec) { error = "Cannot enumerate SCAD project: " + ec.message(); return false; }
    return true;
}

// Only these renderer-owned system resources may be added by a caller. Canonical
// component comparison prevents symlink and prefix escapes (e.g. fonts-private).
bool resource_allowed(const fs::path& path) {
    static const char* const roots[] = {"/usr/share/openscad", "/usr/share/fonts",
        "/usr/local/share/openscad", "/usr/local/share/fonts", "/etc/fonts",
        "/usr/share/fontconfig", "/usr/local/lib/openscad"};
    for (const char* root : roots) {
        std::error_code ec;
        const fs::path base = fs::canonical(root, ec);
        if (ec) continue;
        auto p = path.begin(), b = base.begin();
        for (; b != base.end() && p != path.end() && *b == *p; ++b, ++p) {}
        if (b == base.end()) return true;
    }
    return false;
}

void bind_if_present(std::vector<std::string>& args, const char* path) {
    std::error_code ec;
    if (fs::exists(path, ec) && !ec) {
        args.emplace_back("--ro-bind"); args.emplace_back(path); args.emplace_back(path);
    }
}

bool publish(const fs::path& staged, const fs::path& destination,
             std::size_t max_bytes, std::string& error) {
    // No symlinks, devices, or partial files escape the private sandbox.
    int in = ::open(staged.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    struct stat st{};
    if (in < 0 || ::fstat(in, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0 ||
        static_cast<std::uintmax_t>(st.st_size) > max_bytes) {
        if (in >= 0) ::close(in);
        error = "Renderer did not produce a bounded regular output file";
        return false;
    }
    std::string pattern = (destination.parent_path() / ".chudware-scad-output-XXXXXX").string();
    std::vector<char> name(pattern.begin(), pattern.end()); name.push_back('\0');
    const int out = ::mkstemp(name.data());
    if (out < 0) { ::close(in); error = "Cannot create temporary output"; return false; }
    bool ok = true;
    std::size_t total = 0;
    char buffer[32768];
    for (;;) {
        ssize_t n = ::read(in, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { ok = false; break; }
        if (!n) break;
        if (static_cast<std::size_t>(n) > max_bytes - total) { ok = false; break; }
        total += static_cast<std::size_t>(n);
        ssize_t written = 0;
        while (written < n) {
            ssize_t k = ::write(out, buffer + written, static_cast<std::size_t>(n - written));
            if (k < 0 && errno == EINTR) continue;
            if (k <= 0) { ok = false; break; }
            written += k;
        }
        if (!ok) break;
    }
    ::close(in);
    if (::close(out)) ok = false;
    if (ok && ::rename(name.data(), destination.c_str())) ok = false;
    if (!ok) { ::unlink(name.data()); error = "Cannot atomically publish bounded SCAD output"; }
    return ok;
}

Result execute(const Request& request, const fs::path& exe, const fs::path& bwrap,
               const fs::path& work) {
    std::vector<std::string> args = {bwrap.string(), "--die-with-parent", "--new-session",
        "--unshare-all", "--unshare-user", "--disable-userns", "--clearenv", "--dir", "/usr", "--dir", "/usr/share",
        "--dir", "/usr/local", "--dir", "/usr/local/share", "--dir", "/usr/local/lib",
        "--dir", "/etc", "--dir", "/opt", "--dir", "/opt/scad",
        "--dir", "/opt/scad/usr", "--dir", "/opt/scad/usr/bin",
        "--dir", "/opt/scad/usr/share", "--dir", "/work",
        "--size", "134217728", "--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc"};
    // These are runtime library and font directories, not a host home/workspace.
    for (const char* p : {"/usr/lib", "/usr/lib64", "/lib", "/lib64", "/etc/ld.so.cache",
                           "/usr/share/openscad", "/usr/local/share/openscad",
                           "/usr/share/fonts", "/usr/local/share/fonts", "/etc/fonts",
                           "/usr/share/fontconfig", "/usr/local/lib/openscad"}) bind_if_present(args, p);
    // An extracted AppImage may supply its own Qt and OpenSCAD resources.
    // Recognize only the conventional usr/bin/openscad + AppRun layout and mount
    // individually named resource trees, never the parent or entire AppDir.
    const fs::path app_usr = exe.parent_path().parent_path();
    const fs::path app_root = app_usr.parent_path();
    std::error_code app_ec;
    const bool app_dir = exe.filename() == "openscad" && exe.parent_path().filename() == "bin" &&
        app_usr.filename() == "usr" && fs::is_regular_file(app_root / "AppRun", app_ec) && !app_ec;
    if (app_dir) {
        for (const char* relative : {"usr/lib", "usr/lib64", "usr/plugins", "usr/share/openscad",
                                     "usr/share/fonts"}) {
            const fs::path source = app_root / relative;
            std::error_code ec;
            const auto canonical = fs::canonical(source, ec);
            if (ec) continue;
            auto p = canonical.begin(), b = app_root.begin();
            for (; b != app_root.end() && p != canonical.end() && *b == *p; ++b, ++p) {}
            if (b != app_root.end() || !fs::is_directory(canonical, ec) || ec)
                return failure(Status::invalid_request, "AppDir renderer resource escapes its package");
            args.insert(args.end(), {"--ro-bind", canonical.string(),
                                     "/opt/scad/" + std::string(relative)});
        }
        args.insert(args.end(), {"--setenv", "LD_LIBRARY_PATH",
            "/opt/scad/usr/lib:/opt/scad/usr/lib64:/opt/scad/usr/lib/x86_64-linux-gnu",
            "--setenv", "QT_PLUGIN_PATH", "/opt/scad/usr/plugins:/opt/scad/usr/lib/qt5/plugins"});
    }
    for (const auto& root : request.renderer_resource_roots) {
        std::error_code ec;
        const auto path = fs::canonical(root, ec);
        if (ec || !resource_allowed(path)) return failure(Status::invalid_request,
            "Renderer resource root is not a reviewed OpenSCAD/font resource: " + root);
        // Reviewed roots are already mounted as whole trees above.
    }
    args.insert(args.end(), {"--ro-bind", exe.string(), "/opt/scad/usr/bin/openscad",
        "--ro-bind", work.string(), "/work"});
    if (!request.output_file.empty()) {
        args.insert(args.end(), {"--bind", (work / request.output_file).string(),
                                "/work/" + request.output_file});
    }
    args.insert(args.end(), {"--remount-ro", "/", "--chdir", "/work",
        "--setenv", "HOME", "/nonexistent", "--setenv", "XDG_CONFIG_HOME", "/nonexistent",
        "--setenv", "XDG_CACHE_HOME", "/tmp/cache", "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "QT_QPA_PLATFORM", "offscreen",
        "--seccomp", "4", "--info-fd", "3", "--", "/opt/scad/usr/bin/openscad"});
    args.insert(args.end(), request.arguments.begin(), request.arguments.end());
    std::vector<char*> argv;
    argv.reserve(args.size() + 1);
    for (auto& arg : args) argv.push_back(arg.data());
    argv.push_back(nullptr);
    char env_path[] = "PATH=/usr/bin:/bin";
    char* env[] = {env_path, nullptr};
    const auto filter = renderer_filter();
    if (filter.empty()) return failure(Status::sandbox_unavailable,
        "SCAD sandbox process limits are unavailable for this CPU architecture");
    int logpipe[2], infopipe[2], secpipe[2];
    if (::pipe2(logpipe, O_CLOEXEC) < 0) return failure(Status::sandbox_unavailable, "Cannot create sandbox diagnostics pipe");
    if (::pipe2(infopipe, O_CLOEXEC) < 0) {
        ::close(logpipe[0]); ::close(logpipe[1]);
        return failure(Status::sandbox_unavailable, "Cannot create sandbox status pipe");
    }
    if (::pipe2(secpipe, O_CLOEXEC) < 0) {
        ::close(logpipe[0]); ::close(logpipe[1]); ::close(infopipe[0]); ::close(infopipe[1]);
        return failure(Status::sandbox_unavailable, "Cannot create sandbox filter pipe");
    }
    const auto bytes = filter.size() * sizeof(sock_filter);
    const bool filter_written = ::write(secpipe[1], filter.data(), bytes) == static_cast<ssize_t>(bytes);
    ::close(secpipe[1]);
    // Keep all sources above the fixed bwrap descriptor numbers before dup2.
    int out_fd = ::fcntl(logpipe[1], F_DUPFD_CLOEXEC, 10);
    int info_fd = ::fcntl(infopipe[1], F_DUPFD_CLOEXEC, 10);
    int filter_fd = ::fcntl(secpipe[0], F_DUPFD_CLOEXEC, 10);
    ::close(secpipe[0]);
    if (!filter_written || out_fd < 0 || info_fd < 0 || filter_fd < 0) {
        if (out_fd >= 0) ::close(out_fd);
        if (info_fd >= 0) ::close(info_fd);
        if (filter_fd >= 0) ::close(filter_fd);
        ::close(logpipe[0]); ::close(logpipe[1]); ::close(infopipe[0]); ::close(infopipe[1]);
        return failure(Status::sandbox_unavailable, "Cannot prepare sandbox process filter");
    }
    const auto end = std::chrono::steady_clock::now() + request.deadline;
    const pid_t pid = ::fork();
    if (pid == 0) {
        // No allocation or library locks after fork in this multithreaded process.
        ::setpgid(0, 0);
        int nullfd = ::open("/dev/null", O_RDONLY);
        if (nullfd < 0 || ::dup2(nullfd, 0) < 0 || ::dup2(out_fd, 1) < 0 ||
            ::dup2(out_fd, 2) < 0 || ::dup2(info_fd, 3) < 0 ||
            ::dup2(filter_fd, 4) < 0) ::_exit(125);
        ::fcntl(3, F_SETFD, 0);
        ::fcntl(4, F_SETFD, 0);
        for (int fd = 5; fd < 65536; ++fd) ::close(fd);
        const rlim_t seconds = static_cast<rlim_t>((request.deadline.count() + 999) / 1000 + 1);
        struct rlimit cpu{seconds, seconds};
        struct rlimit memory{2ULL * 1024 * 1024 * 1024, 2ULL * 1024 * 1024 * 1024};
        struct rlimit file{static_cast<rlim_t>(request.max_output_bytes), static_cast<rlim_t>(request.max_output_bytes)};
        struct rlimit descriptors{128, 128};
        if (::setrlimit(RLIMIT_CPU, &cpu) || ::setrlimit(RLIMIT_AS, &memory) ||
            ::setrlimit(RLIMIT_FSIZE, &file) || ::setrlimit(RLIMIT_NOFILE, &descriptors)) ::_exit(125);
        ::execve(argv[0], argv.data(), env);
        ::_exit(127);
    }
    ::close(logpipe[1]); ::close(infopipe[1]);
    ::close(out_fd); ::close(info_fd); ::close(filter_fd);
    if (pid < 0) {
        ::close(logpipe[0]); ::close(infopipe[0]);
        return failure(Status::sandbox_unavailable, "Cannot start bubblewrap sandbox");
    }
    ::setpgid(pid, pid);
    ::fcntl(logpipe[0], F_SETFL, O_NONBLOCK);
    ::fcntl(infopipe[0], F_SETFL, O_NONBLOCK);
    std::string diagnostics;
    bool started = false, timed_out = false, wait_error = false, diagnostic_overflow = false;
    int status = 0;
    for (;;) {
        char buffer[4096];
        for (int reads = 0; reads < 16; ++reads) {
            const ssize_t n = ::read(logpipe[0], buffer, sizeof(buffer));
            if (n <= 0) break;
            const auto room = request.max_diagnostic_bytes - diagnostics.size();
            if (static_cast<std::size_t>(n) > room) diagnostic_overflow = true;
            diagnostics.append(buffer, std::min(room, static_cast<std::size_t>(n)));
        }
        while (::read(infopipe[0], buffer, sizeof(buffer)) > 0) started = true;
        if (diagnostic_overflow) break;
        const pid_t r = ::waitpid(pid, &status, WNOHANG);
        if (r == pid) break;
        if (r < 0 && errno != EINTR) { wait_error = true; break; }
        if (std::chrono::steady_clock::now() >= end) { timed_out = true; break; }
        struct pollfd fds[2] = {{logpipe[0], POLLIN, 0}, {infopipe[0], POLLIN, 0}};
        ::poll(fds, 2, 10);
    }
    if (timed_out || wait_error || diagnostic_overflow) {
        ::kill(-pid, SIGKILL); ::kill(pid, SIGKILL);
        while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    }
    // The bwrap PID namespace and --die-with-parent terminate descendants even if
    // they changed process groups. Drain already-buffered status and diagnostics.
    char buffer[4096];
    ssize_t n;
    while ((n = ::read(logpipe[0], buffer, sizeof(buffer))) > 0) {
        const auto room = request.max_diagnostic_bytes - diagnostics.size();
        if (static_cast<std::size_t>(n) > room) diagnostic_overflow = true;
        diagnostics.append(buffer, std::min(room, static_cast<std::size_t>(n)));
    }
    while (::read(infopipe[0], buffer, sizeof(buffer)) > 0) started = true;
    ::close(logpipe[0]); ::close(infopipe[0]);
    if (timed_out) return {Status::timed_out, -1, "OpenSCAD exceeded its deadline. " + diagnostics};
    if (wait_error) return {Status::sandbox_unavailable, -1, "Could not wait for the SCAD sandbox. " + diagnostics};
    if (!started) return {Status::sandbox_unavailable, -1,
        "Bubblewrap could not establish the SCAD sandbox; enable unprivileged user namespaces and install a working bubblewrap. " + diagnostics};
    if (diagnostic_overflow)
        return {Status::resource_limit, -1, "OpenSCAD diagnostics exceeded the configured limit; output was discarded."};
    if (WIFSIGNALED(status)) return {Status::resource_limit, -1,
        "OpenSCAD was terminated by signal " + std::to_string(WTERMSIG(status)) + ". " + diagnostics};
    const int code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    if (code != 0) return {Status::model_failure, code, std::move(diagnostics)};
    return {Status::success, 0, std::move(diagnostics)};
}
#endif
}  // namespace

static Result run_impl(const Request& request) {
#if defined(_WIN32)
    return detail::run_windows(request);
#elif defined(__APPLE__)
    return detail::run_macos(request);
#elif !defined(__linux__)
    (void)request;
    return failure(Status::sandbox_unavailable,
        "Confined OpenSCAD execution is not available on this platform; no unconfined renderer will be run.");
#else
    if (request.deadline.count() <= 0 || request.deadline > std::chrono::hours(1) ||
        request.max_diagnostic_bytes == 0 || request.max_diagnostic_bytes > kMaxDiagnostics ||
        request.max_output_bytes == 0 || request.max_output_bytes > kMaxArtifact ||
        contains_nul(request.work_dir) || contains_nul(request.output_file) ||
        request.output_file == "." || request.output_file == ".." ||
        request.output_file.find('/') != std::string::npos ||
        std::any_of(request.arguments.begin(), request.arguments.end(), contains_nul) ||
        std::any_of(request.renderer_resource_roots.begin(), request.renderer_resource_roots.end(), contains_nul))
        return failure(Status::invalid_request, "Invalid SCAD process limits, arguments or output filename");
    const auto absolute_deadline = std::chrono::steady_clock::now() + request.deadline;
    const auto exe = find_program(request.executable);
    if (exe.empty()) return failure(Status::dependency_unavailable,
        "OpenSCAD executable not found or not executable; install OpenSCAD or configure CHUDWARE_OPENSCAD_BIN.");
    const auto bwrap = find_bubblewrap();
    if (bwrap.empty()) return failure(Status::sandbox_unavailable,
        "Bubblewrap is required to run OpenSCAD safely; install bubblewrap and enable user namespaces.");
    std::error_code ec;
    const auto work = fs::absolute(request.work_dir, ec);
    if (ec || request.work_dir.empty()) return failure(Status::invalid_request, "A SCAD work directory is required");
    TempDir temp;
    if (!temp.create()) return failure(Status::sandbox_unavailable, "Cannot create private SCAD staging directory");
    const auto staged = temp.path / "project";
    if (!fs::create_directory(staged, ec) || ec)
        return failure(Status::sandbox_unavailable, "Cannot create private SCAD project directory");
    std::string error;
    if (!stage_inputs(work, staged, error)) return failure(Status::invalid_request, std::move(error));
    // Make the project read-only in the sandbox; only this pre-created regular
    // file receives a writable bind mount. It cannot be replaced or renamed there.
    if (!request.output_file.empty()) {
        fs::remove(staged / request.output_file, ec);
        if (ec) return failure(Status::invalid_request, "Cannot prepare SCAD output filename");
        int fd = ::open((staged / request.output_file).c_str(),
                        O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd < 0) return failure(Status::invalid_request, "Cannot create SCAD output file");
        ::close(fd);
    }
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        absolute_deadline - std::chrono::steady_clock::now());
    if (remaining.count() <= 0)
        return failure(Status::timed_out, "SCAD project staging exceeded its deadline");
    Request bounded = request;
    bounded.deadline = remaining;
    Result result = execute(bounded, exe, bwrap, staged);
    if (result.status == Status::success && !request.output_file.empty() &&
        !publish(staged / request.output_file, work / request.output_file, request.max_output_bytes, error)) {
        result.status = Status::resource_limit;
        result.diagnostics += "\n" + error;
    }
    return result;
#endif
}

Result run(const Request& request) {
    Result result = run_impl(request);
    if (request.max_diagnostic_bytes && result.diagnostics.size() > request.max_diagnostic_bytes)
        result.diagnostics.resize(request.max_diagnostic_bytes);
    return result;
}

Result capability(const std::string& executable) {
#ifdef __linux__
    TempDir temp;
    if (!temp.create()) return failure(Status::sandbox_unavailable, "Cannot create private SCAD capability directory");
    Request request;
    request.executable = executable;
    request.arguments = {"--version"};
    request.work_dir = temp.path.string();
    request.deadline = std::chrono::seconds(10);
    return run(request);
#elif defined(_WIN32)
    return detail::capability_windows(executable);
#elif defined(__APPLE__)
    return detail::capability_macos(executable);
#else
    (void)executable;
    return failure(Status::sandbox_unavailable,
        "Confined OpenSCAD execution is not available on this platform; no unconfined renderer will be run.");
#endif
}

}  // namespace chudware::util::scad_process
