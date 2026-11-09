// Fault isolation for OCCT shape builders. Strict fillets use a fresh executable;
// legacy blend callbacks retain their existing POSIX fork policy.
#if !defined(_GNU_SOURCE)
#define _GNU_SOURCE
#endif
#include "util/process_isolation.h"
#include "util/parent_death.h"
#include "util/platform_paths.h"
#include "security/integrity.h"

#include <algorithm>
#include <atomic>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <istream>
#include <limits>
#include <locale>
#include <ostream>
#include <streambuf>
#include <string>
#include <utility>
#include <vector>
#include <thread>

#include <BinTools.hxx>

#if defined(__unix__) || defined(__APPLE__)
#include <cerrno>
#include <dirent.h>
#include <dlfcn.h>
#include <csignal>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#if defined(__linux__)
#include <sys/syscall.h>
#endif
#if defined(__APPLE__)
#include <crt_externs.h>
#else
extern char** environ;
#endif
#if defined(__linux__) && defined(__GLIBC__)
#if !__GLIBC_PREREQ(2, 34)
#define CHUDWARE_WORKER_POST_EXEC_CLOSE
#endif
#elif !defined(__APPLE__) || !defined(POSIX_SPAWN_CLOEXEC_DEFAULT)
#define CHUDWARE_WORKER_POST_EXEC_CLOSE
#endif
#endif

namespace chudware::util {

bool shape_isolation_enabled() {
    const char* v = std::getenv("CHUDWARE_BLEND_ISOLATION");
    return !v || (v[0] != '0' || v[1] != '\0');
}

namespace {
// A process where fork() is unsafe cannot use fork-isolation at all, and must not pay the
// timeout for every blend it ever builds.
//
// fork() in a MULTITHREADED process gives the child a snapshot of every lock, so if another
// thread held the malloc arena at fork time the child deadlocks on its first allocation - and
// OCCT allocates immediately. That is not hypothetical: the desktop GUI links libchudware
// in-process alongside its worker-bridge and RPC threads, and CHUDWARE_SHOT=fea went from a
// 90 s scenario to a hard hang because each blend burned the full child timeout.
//
// Under the default policy a timeout retires legacy fork-isolation and retries
// in process. Strict executable workers are independent of this state.
std::atomic<bool> g_fork_unusable{false};
// Private to the forked advisory child. Its outer boundary already contains all
// nested geometry and must never inherit the ordinary blend fallback policy.
bool g_bounded_task = false;
}  // namespace

bool shape_isolation_active() {
#if defined(__unix__) || defined(__APPLE__)
    return !g_bounded_task && !g_fork_unusable.load(std::memory_order_relaxed) &&
           shape_isolation_enabled();
#else
    return false;
#endif
}

bool strict_shape_isolation_enabled() {
#if defined(__unix__) || defined(__APPLE__)
    return !g_bounded_task && shape_isolation_enabled();
#else
    return false;
#endif
}


IsolatedShapeResult isolate_shape_build(const std::function<TopoDS_Shape()>& build,
                                        std::chrono::milliseconds timeout,
                                        std::string* sidecar,
                                        bool retry_after_timeout) {
    using Failure = ShapeBuildFailure;
    if (sidecar) sidecar->clear();
    // Called only inside an expendable child to exercise its watchdog.
    const auto inject_test_delay = [] {
        if (const char* delay = std::getenv("CHUDWARE_TEST_BLEND_DELAY_MS")) {
            const long ms = std::strtol(delay, nullptr, 10);
            if (ms > 0) std::this_thread::sleep_for(std::chrono::milliseconds(ms));
        }
    };
    auto in_process = [&]() -> IsolatedShapeResult {
        if (sidecar) sidecar->clear();
        if (g_bounded_task) inject_test_delay();
        try {
            TopoDS_Shape s = build();
            if (s.IsNull()) {
                if (sidecar && retry_after_timeout) sidecar->clear();
                return {false, {}, Failure::construction};
            }
            return {true, std::move(s), Failure::none};
        } catch (...) {
            if (sidecar) sidecar->clear();
            return {false, {}, Failure::construction};
        }
    };
#if defined(__unix__) || defined(__APPLE__)
    // Arbitrary callbacks cannot safely be run after fork in a multithreaded
    // host. Strict callers must use ShapeBuildExecSession instead.
    if (!retry_after_timeout && !g_bounded_task && shape_isolation_enabled())
        return {false, {}, Failure::unavailable};
    if (g_bounded_task || g_fork_unusable.load(std::memory_order_relaxed) ||
        !shape_isolation_enabled()) return in_process();

    // Distinguish a normal null build, transport failure and child crash.
    enum class ChildExit : int { success = 0, construction = 101, unavailable = 103 };

    // Honour TMPDIR: /tmp can be a tiny tmpfs, and a large blended shape is megabytes.
    std::string tmpl;
    if (const char* tmpdir = std::getenv("TMPDIR"); tmpdir && *tmpdir) {
        tmpl = tmpdir;
        if (tmpl.back() != '/') tmpl.push_back('/');
    } else {
        tmpl = "/tmp/";
    }
    tmpl += "chudware-blend-XXXXXX";
    std::vector<char> path(tmpl.begin(), tmpl.end());
    path.push_back('\0');
    const int fd = ::mkstemp(path.data());
    if (fd < 0) return {};
    const std::string filename(path.data());

    const pid_t pid = ::fork();
    if (pid < 0) {
        ::close(fd);
        ::unlink(filename.c_str());
        return {};
    }
    if (pid == 0) {
        // The child inherits every signal disposition of the host process, so a crash in
        // OCCT would run the HOST's fatal handler inside the child: doctest prints "test
        // case CRASHED" and mangles the harness (this test passed unoptimized and crashed
        // the release LTO build), and a packaged app would run its crash reporter from a
        // process whose only job is to die quietly. The contract here is "a signal means
        // the geometry killed OCCT and the parent reports a failed feature", which needs
        // the DEFAULT disposition: terminate, no handler.
        for (const int sig : {SIGSEGV, SIGBUS, SIGFPE, SIGILL, SIGABRT, SIGTRAP}) {
            ::signal(sig, SIG_DFL);
        }
        ChildExit result = ChildExit::unavailable;
        try {
            if (const char* fault = std::getenv("CHUDWARE_TEST_BLEND_SIGSEGV");
                fault && fault[0] == '1') {
                ::raise(SIGSEGV);
            }
            TopoDS_Shape s = build();
            if (s.IsNull()) {
                result = ChildExit::construction;
            } else {
                try {
                    // Payload: [u64 sidecar length][sidecar bytes][BinTools shape].
                    std::ofstream out(filename, std::ios::binary | std::ios::trunc);
                    const std::uint64_t n = sidecar ? sidecar->size() : 0;
                    out.write(reinterpret_cast<const char*>(&n), sizeof(n));
                    if (sidecar)
                        out.write(sidecar->data(), static_cast<std::streamsize>(n));
                    BinTools::Write(s, out);
                    out.close();
                    if (out) result = ChildExit::success;
                } catch (...) {
                    // Serialization and temporary-file failures are unavailable.
                }
            }
        } catch (...) {
            result = ChildExit::construction;
        }
        ::close(fd);
        ::_exit(static_cast<int>(result));
    }

    int status = 0;
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    // Escalating wait: a blend can run for seconds, so a fixed 1 ms poll would spin
    // thousands of times. Start tight (most builds are milliseconds) and back off.
    useconds_t nap = 200;
    for (;;) {
        const pid_t got = ::waitpid(pid, &status, WNOHANG);
        if (got == pid) break;
        if (got < 0 && errno != EINTR) {
            // ECHILD means the process is no longer ours; never signal a reused PID.
            if (errno != ECHILD) {
                ::kill(pid, SIGKILL);
                while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
            }
            ::close(fd);
            ::unlink(filename.c_str());
            g_fork_unusable.store(true, std::memory_order_relaxed);
            return in_process();
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            ::kill(pid, SIGKILL);
            while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
            ::close(fd);
            ::unlink(filename.c_str());
            g_fork_unusable.store(true, std::memory_order_relaxed);
            return in_process();
        }
        ::usleep(nap);
        if (nap < 10000) nap *= 2;
    }
    ::close(fd);
    if (WIFSIGNALED(status)) {
        ::unlink(filename.c_str());
        return {false, {}, Failure::crash};
    }
    if (!WIFEXITED(status)) {
        ::unlink(filename.c_str());
        return {false, {}, Failure::unavailable};
    }
    const int child_exit = WEXITSTATUS(status);
    if (child_exit == static_cast<int>(ChildExit::construction)) {
        ::unlink(filename.c_str());
        return {false, {}, Failure::construction};
    }
    if (child_exit == static_cast<int>(ChildExit::unavailable)) {
        ::unlink(filename.c_str());
        return {false, {}, Failure::unavailable};
    }
    if (child_exit != static_cast<int>(ChildExit::success)) {
        ::unlink(filename.c_str());
        return {false, {}, Failure::crash};
    }
    TopoDS_Shape shape;
    std::string side;
    try {
        std::ifstream in(filename, std::ios::binary);
        std::uint64_t n = 0;
        in.read(reinterpret_cast<char*>(&n), sizeof(n));
        if (!in || n > (std::uint64_t{1} << 32)) throw std::runtime_error("bad payload");
        side.resize(static_cast<std::size_t>(n));
        in.read(side.data(), static_cast<std::streamsize>(n));
        if (!in) throw std::runtime_error("bad payload");
        BinTools::Read(shape, in);
        if (shape.IsNull()) throw std::runtime_error("bad shape payload");
    } catch (...) {
        ::unlink(filename.c_str());
        return {false, {}, Failure::unavailable};
    }
    ::unlink(filename.c_str());
    if (sidecar) *sidecar = std::move(side);
    return {true, std::move(shape), Failure::none};
#else
    (void)timeout;
    return in_process();
#endif
}

#if defined(__unix__) || defined(__APPLE__)
namespace {

enum class ExecExit : int { success = 0, construction = 101, unavailable = 103,
                            timed_out = 104 };
constexpr int kWorkerInput = 3;
constexpr int kWorkerOutput = 4;
#if defined(__linux__)
constexpr int kWorkerExecutable = 5;
#endif

// The input is serialized once per search; both streams use bounded buffers,
// rather than copying a potentially large B-rep into temporary strings.
class FdOutputBuffer : public std::streambuf {
public:
    explicit FdOutputBuffer(int fd) : fd_(fd) {
        setp(buffer_.data(), buffer_.data() + buffer_.size());
    }
protected:
    int sync() override {
        const char* data = pbase();
        std::size_t remaining = static_cast<std::size_t>(pptr() - pbase());
        while (remaining) {
            const ssize_t n = ::write(fd_, data, remaining);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) return -1;
            data += n;
            remaining -= static_cast<std::size_t>(n);
        }
        setp(buffer_.data(), buffer_.data() + buffer_.size());
        return 0;
    }
    int_type overflow(int_type value) override {
        if (sync() != 0) return traits_type::eof();
        if (!traits_type::eq_int_type(value, traits_type::eof())) {
            *pptr() = traits_type::to_char_type(value);
            pbump(1);
        }
        return traits_type::not_eof(value);
    }
    pos_type seekoff(off_type offset, std::ios_base::seekdir direction,
                     std::ios_base::openmode mode) override {
        if (!(mode & std::ios_base::out)) return pos_type(off_type(-1));
        if (direction == std::ios_base::cur && offset == 0) {
            const off_t position = ::lseek(fd_, 0, SEEK_CUR);
            return position < 0 ? pos_type(off_type(-1))
                                : pos_type(position + (pptr() - pbase()));
        }
        if (sync() != 0) return pos_type(off_type(-1));
        const int whence = direction == std::ios_base::beg ? SEEK_SET :
                           direction == std::ios_base::cur ? SEEK_CUR : SEEK_END;
        const off_t position = ::lseek(fd_, static_cast<off_t>(offset), whence);
        return position < 0 ? pos_type(off_type(-1)) : pos_type(position);
    }
    pos_type seekpos(pos_type position, std::ios_base::openmode mode) override {
        return seekoff(static_cast<off_type>(position), std::ios_base::beg, mode);
    }
private:
    int fd_;
    std::array<char, 16384> buffer_{};
};

class FdInputBuffer : public std::streambuf {
public:
    explicit FdInputBuffer(int fd) : fd_(fd) { setg(buffer_.data(), buffer_.data(), buffer_.data()); }
    bool failed() const { return failed_; }
protected:
    int_type underflow() override {
        if (gptr() < egptr()) return traits_type::to_int_type(*gptr());
        ssize_t n;
        do { n = ::read(fd_, buffer_.data(), buffer_.size()); } while (n < 0 && errno == EINTR);
        if (n < 0) failed_ = true;
        if (n <= 0) return traits_type::eof();
        setg(buffer_.data(), buffer_.data(), buffer_.data() + n);
        return traits_type::to_int_type(*gptr());
    }
    pos_type seekoff(off_type offset, std::ios_base::seekdir direction,
                     std::ios_base::openmode mode) override {
        if (!(mode & std::ios_base::in)) return pos_type(off_type(-1));
        const off_t unread = static_cast<off_t>(egptr() - gptr());
        if (direction == std::ios_base::cur && offset == 0) {
            const off_t position = ::lseek(fd_, 0, SEEK_CUR);
            return position < 0 ? pos_type(off_type(-1)) : pos_type(position - unread);
        }
        const int whence = direction == std::ios_base::beg ? SEEK_SET :
                           direction == std::ios_base::cur ? SEEK_CUR : SEEK_END;
        const off_t adjustment = direction == std::ios_base::cur ? unread : 0;
        const off_t position = ::lseek(fd_, static_cast<off_t>(offset) - adjustment, whence);
        if (position < 0) {
            failed_ = true;
            return pos_type(off_type(-1));
        }
        setg(buffer_.data(), buffer_.data(), buffer_.data());
        return pos_type(position);
    }
    pos_type seekpos(pos_type position, std::ios_base::openmode mode) override {
        return seekoff(static_cast<off_type>(position), std::ios_base::beg, mode);
    }
private:
    int fd_;
    bool failed_ = false;
    std::array<char, 16384> buffer_{};
};

int anonymous_temp_file() {
    std::string path;
    if (const char* tmpdir = std::getenv("TMPDIR"); tmpdir && *tmpdir) {
        path = tmpdir;
        if (path.back() != '/') path.push_back('/');
    } else {
        path = "/tmp/";
    }
    path += "chudware-shape-worker-XXXXXX";
    std::vector<char> writable(path.begin(), path.end());
    writable.push_back('\0');
#if defined(__linux__)
    const int fd = ::mkostemp(writable.data(), O_CLOEXEC);
#else
    const int fd = ::mkstemp(writable.data());
#endif
    if (fd < 0) return -1;
    if (::unlink(writable.data()) != 0) {
        ::close(fd);
        return -1;
    }
#if !defined(__linux__)
    if (::fcntl(fd, F_SETFD, FD_CLOEXEC) < 0) {
        ::close(fd);
        return -1;
    }
#endif
    if (fd >= 5) return fd;
    // Reserve the worker's fd 3 and 4 as destinations, never as sources of
    // another dup2 action.
    const int safe = ::fcntl(fd, F_DUPFD_CLOEXEC, 5);
    ::close(fd);
    return safe;
}

#if defined(CHUDWARE_WORKER_POST_EXEC_CLOSE)
// Some POSIX spawn implementations lack a close-from action. In that case
// the fresh executable closes every other descriptor before doing any work.
bool close_worker_descriptors() {
#if defined(__linux__) && defined(SYS_close_range)
    if (::syscall(SYS_close_range, 5u, ~0u, 0u) == 0) return true;
#endif
    for (const char* directory : {"/proc/self/fd", "/dev/fd"}) {
        DIR* dir = ::opendir(directory);
        if (!dir) continue;
        bool complete = true;
        for (;;) {
            errno = 0;
            const dirent* entry = ::readdir(dir);
            if (!entry) {
                complete = errno == 0;
                break;
            }
            int fd = -1;
            const char* end = entry->d_name + std::strlen(entry->d_name);
            const auto parsed = std::from_chars(entry->d_name, end, fd);
            if (parsed.ec == std::errc{} && parsed.ptr == end &&
                fd >= 5 && fd != ::dirfd(dir)) ::close(fd);
        }
        ::closedir(dir);
        return complete;
    }
    const long limit = ::sysconf(_SC_OPEN_MAX);
    if (limit < 0 || limit > std::numeric_limits<int>::max()) return false;
    for (int fd = 5; fd < limit; ++fd) ::close(fd);
    return true;
}
#endif

#if defined(__linux__)
// Packaged Linux loads the verified engine through /proc/self/fd/N and closes N
// after dlopen. dladdr then retains that stale spelling; the mapping names the
// actual library even if the descriptor number has since been reused.
std::string mapped_module_path(const void* symbol) {
    const auto address = reinterpret_cast<std::uintptr_t>(symbol);
    std::ifstream maps("/proc/self/maps");
    std::string line;
    while (std::getline(maps, line)) {
        const std::size_t dash = line.find('-');
        const std::size_t end = line.find(' ', dash);
        if (dash == std::string::npos || end == std::string::npos) continue;
        std::uintptr_t first = 0, last = 0;
        const auto a = std::from_chars(line.data(), line.data() + dash, first, 16);
        const auto b = std::from_chars(line.data() + dash + 1, line.data() + end, last, 16);
        if (a.ec != std::errc{} || a.ptr != line.data() + dash ||
            b.ec != std::errc{} || b.ptr != line.data() + end ||
            address < first || address >= last) continue;
        std::size_t position = end;
        // Skip permissions, file offset, device and inode, preserving spaces
        // in the pathname itself.
        for (int field = 0; field < 4; ++field) {
            position = line.find_first_not_of(" \t", position);
            if (position == std::string::npos) return {};
            position = line.find_first_of(" \t", position);
            if (position == std::string::npos) return {};
        }
        position = line.find_first_not_of(" \t", position);
        if (position != std::string::npos && line[position] == '/')
            return line.substr(position);
        return {};
    }
    return {};
}
#endif

std::string worker_executable() {
#if defined(__linux__)
    const std::string mapped = mapped_module_path(reinterpret_cast<const void*>(&worker_executable));
    if (!mapped.empty())
        return (std::filesystem::path(mapped).parent_path() /
                "chudware-shape-build-worker").string();
#endif
    Dl_info info{};
    if (!::dladdr(reinterpret_cast<const void*>(&worker_executable), &info) ||
        !info.dli_fname || !*info.dli_fname) return {};
    std::filesystem::path module(info.dli_fname);
#if defined(__linux__)
    if (module.string().rfind("/proc/self/fd/", 0) == 0) return {};
#endif
    std::error_code error;
    auto resolved = std::filesystem::canonical(module, error);
    if (error) {
        // dladdr may report argv[0] for a statically linked test executable.
        // Do not substitute the Python/GUI executable for a missing shared library.
        const std::filesystem::path executable = chudware::platform::executablePath();
        if (executable.empty() || module.filename() != executable.filename()) return {};
        resolved = executable;
    }
    return (resolved.parent_path() / "chudware-shape-build-worker").string();
}

int open_verified_worker(const std::string& name) {
    const int opened = ::open(name.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (opened < 0) return -1;
    // The Linux spawn action reserves fd 5 for exec; keep all sources above it.
    int fd = opened;
    if (fd < 6) {
        fd = ::fcntl(opened, F_DUPFD_CLOEXEC, 6);
        ::close(opened);
        if (fd < 0) return -1;
    }
    try {
        struct stat st{};
        if (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
            ::close(fd);
            return -1;
        }
        const std::filesystem::path worker(name);
        std::filesystem::path anchor = worker;
#if defined(__linux__)
        // installedPayloadRoot recognizes the signed Linux usr/bin layout.
        // Derive that anchor from the mapped engine directory, never /usr/bin/python.
        const auto directory = worker.parent_path();
        if (directory.filename() == "lib" && directory.parent_path().filename() == "usr")
            anchor = directory.parent_path() / "bin" / "chudware-hostd";
#endif
        std::string relative;
#if defined(CHUDWARE_DEVELOPER_BUILD)
        relative = worker.filename().string();
#else
        const auto root = chudware::security::installedPayloadRoot(anchor);
        std::error_code error;
        relative = std::filesystem::relative(worker, root, error).generic_string();
        if (root.empty() || error || relative.empty() || relative == "." ||
            relative == ".." || relative.rfind("../", 0) == 0) {
            ::close(fd);
            return -1;
        }
#endif
        if (!chudware::security::verifyInstalledManifestFd(anchor, relative, "worker", fd).ok) {
            ::close(fd);
            return -1;
        }
        return fd;
    } catch (...) {
        ::close(fd);
        return -1;
    }
}

std::uint64_t double_bits(double value) {
    std::uint64_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

double bits_double(std::uint64_t bits) {
    double value = 0;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

template <class Integer>
bool parse_integer(const char* text, Integer& value, int base = 10) {
    if (!text || !*text) return false;
    const char* end = text + std::strlen(text);
    const auto parsed = std::from_chars(text, end, value, base);
    return parsed.ec == std::errc{} && parsed.ptr == end;
}

}  // namespace
#endif

ShapeBuildExecSession::ShapeBuildExecSession(const TopoDS_Shape& input,
                                             const std::vector<ShapeWorkerEdge>& edges) {
#if defined(__unix__) || defined(__APPLE__)
    int fd = -1, pinned = -1;
    try {
        worker_path_ = worker_executable();
        if (worker_path_.empty() || input.IsNull() ||
            (pinned = open_verified_worker(worker_path_)) < 0) return;
        if ((fd = anonymous_temp_file()) < 0) {
            ::close(pinned);
            return;
        }
        FdOutputBuffer buffer(fd);
        std::ostream out(&buffer);
        out.imbue(std::locale::classic());
        BinTools::Write(input, out);
        const std::uint64_t count = edges.size();
        out.write(reinterpret_cast<const char*>(&count), sizeof(count));
        for (const auto& edge : edges) {
            out.write(reinterpret_cast<const char*>(&edge.index), sizeof(edge.index));
            out.write(reinterpret_cast<const char*>(&edge.orientation), sizeof(edge.orientation));
        }
        out.flush();
        if (!out || ::lseek(fd, 0, SEEK_SET) < 0) {
            ::close(fd);
            ::close(pinned);
            return;
        }
        input_fd_ = fd;
        worker_fd_ = pinned;
    } catch (...) {
        if (fd >= 0) ::close(fd);
        if (pinned >= 0) ::close(pinned);
    }
#else
    (void)input;
    (void)edges;
#endif
}

ShapeBuildExecSession::~ShapeBuildExecSession() {
#if defined(__unix__) || defined(__APPLE__)
    if (input_fd_ >= 0) ::close(input_fd_);
    if (worker_fd_ >= 0) ::close(worker_fd_);
#endif
}

IsolatedShapeResult ShapeBuildExecSession::run(const FilletWorkerParameters& parameters,
                                                std::string& sidecar) {
    sidecar.clear();
#if defined(__unix__) || defined(__APPLE__)
    using Failure = ShapeBuildFailure;
    if (std::chrono::steady_clock::now() >= parameters.deadline)
        return {false, {}, Failure::timed_out};
    if (input_fd_ < 0 || worker_fd_ < 0 || ::lseek(input_fd_, 0, SEEK_SET) < 0) return {};

    int output = -1;
    try {
        const std::string& executable = worker_path_;
        if (executable.empty() || (output = anonymous_temp_file()) < 0) return {};
        const unsigned flags = (parameters.variable ? 1u : 0u) |
                               (parameters.unequal_requested ? 2u : 0u) |
                               (parameters.recording ? 4u : 0u) |
                               (parameters.document_validation ? 8u : 0u) |
                               ((parameters.runoff & 3u) << 4) |
                               ((parameters.sweep & 3u) << 6);
        const auto deadline_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            parameters.deadline.time_since_epoch()).count();
        std::array<std::string, 7> args{
            executable, "--fillet-v1", std::to_string(double_bits(parameters.radius)),
            std::to_string(double_bits(parameters.radius2)), std::to_string(flags),
            std::to_string(deadline_ns), std::to_string(static_cast<long>(::getpid()))};
        std::array<char*, 8> argv{};
        for (std::size_t i = 0; i < args.size(); ++i) argv[i] = args[i].data();

        posix_spawn_file_actions_t actions;
        posix_spawnattr_t attributes;
        if (::posix_spawn_file_actions_init(&actions) != 0) {
            ::close(output);
            return {};
        }
        if (::posix_spawnattr_init(&attributes) != 0) {
            ::posix_spawn_file_actions_destroy(&actions);
            ::close(output);
            return {};
        }
        int error = ::posix_spawn_file_actions_adddup2(&actions, input_fd_, kWorkerInput);
        if (!error) error = ::posix_spawn_file_actions_adddup2(&actions, output, kWorkerOutput);
#if defined(__linux__)
        // Execute the same inode that passed manifest verification, even if
        // another process replaces the installed pathname during this search.
        if (!error) error = ::posix_spawn_file_actions_adddup2(&actions, worker_fd_,
                                                                kWorkerExecutable);
#endif
        short spawn_flags = POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF |
                            POSIX_SPAWN_SETSIGMASK;
        // Use close-from before exec when available; other POSIX workers
        // close inherited descriptors immediately after exec.
#if defined(__linux__) && defined(__GLIBC__)
#if __GLIBC_PREREQ(2, 34)
        if (!error) error = ::posix_spawn_file_actions_addclosefrom_np(&actions, 6);
#endif
#elif defined(__APPLE__) && defined(POSIX_SPAWN_CLOEXEC_DEFAULT)
        spawn_flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
        sigset_t defaults, mask;
        ::sigemptyset(&defaults);
        ::sigemptyset(&mask);
        for (const int sig : {SIGSEGV, SIGBUS, SIGFPE, SIGILL, SIGABRT, SIGTRAP, SIGPIPE})
            ::sigaddset(&defaults, sig);
        if (!error) error = ::posix_spawnattr_setflags(&attributes, spawn_flags);
        if (!error) error = ::posix_spawnattr_setpgroup(&attributes, 0);
        if (!error) error = ::posix_spawnattr_setsigdefault(&attributes, &defaults);
        if (!error) error = ::posix_spawnattr_setsigmask(&attributes, &mask);
        pid_t pid = -1;
#if defined(__APPLE__)
        char** environment = *_NSGetEnviron();
#else
        char** environment = environ;
#endif
#if defined(__linux__)
        const char* spawn_path = "/proc/self/fd/5";
#else
        const char* spawn_path = executable.c_str();
#endif
        if (!error)
            error = ::posix_spawn(&pid, spawn_path, &actions, &attributes,
                                  argv.data(), environment);
        ::posix_spawnattr_destroy(&attributes);
        ::posix_spawn_file_actions_destroy(&actions);
        if (error) {
            ::close(output);
            return {};
        }

        int status = 0;
        useconds_t nap = 200;
        for (;;) {
            const pid_t got = ::waitpid(pid, &status, WNOHANG);
            if (got == pid) break;
            if (got < 0 && errno != EINTR) {
                // A SIGCHLD handler may already have reaped it: do not signal a reused PID.
                ::close(output);
                return {};
            }
            if (std::chrono::steady_clock::now() >= parameters.deadline) {
                ::kill(-pid, SIGKILL);
                ::kill(pid, SIGKILL);
                while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
                ::close(output);
                return {false, {}, Failure::timed_out};
            }
            ::usleep(nap);
            if (nap < 10000) nap *= 2;
        }
        if (std::chrono::steady_clock::now() >= parameters.deadline) {
            ::close(output);
            return {false, {}, Failure::timed_out};
        }
        if (WIFSIGNALED(status)) {
            ::close(output);
            return {false, {}, Failure::crash};
        }
        if (!WIFEXITED(status)) {
            ::close(output);
            return {};
        }
        const int exit_code = WEXITSTATUS(status);
        if (exit_code == static_cast<int>(ExecExit::timed_out)) {
            ::close(output);
            return {false, {}, Failure::timed_out};
        }
        if (exit_code == static_cast<int>(ExecExit::unavailable) || exit_code == 127) {
            ::close(output);
            return {};
        }
        const bool construction = exit_code == static_cast<int>(ExecExit::construction);
        if (exit_code != static_cast<int>(ExecExit::success) && !construction) {
            ::close(output);
            return {false, {}, Failure::crash};
        }
        if (::lseek(output, 0, SEEK_SET) < 0) {
            ::close(output);
            return {};
        }
        FdInputBuffer buffer(output);
        std::istream in(&buffer);
        in.imbue(std::locale::classic());
        std::uint64_t length = 0;
        in.read(reinterpret_cast<char*>(&length), sizeof(length));
        struct stat output_stat{};
        if (!in || ::fstat(output, &output_stat) != 0 ||
            output_stat.st_size < static_cast<off_t>(sizeof(length)) ||
            length > static_cast<std::uint64_t>(output_stat.st_size - sizeof(length)) ||
            length > (std::uint64_t{1} << 32) ||
            length > std::numeric_limits<std::size_t>::max() ||
            length > static_cast<std::uint64_t>(std::numeric_limits<std::streamsize>::max())) {
            ::close(output);
            return {};
        }
        std::string received(static_cast<std::size_t>(length), '\0');
        in.read(received.data(), static_cast<std::streamsize>(length));
        TopoDS_Shape shape;
        if (!in) {
            ::close(output);
            return {};
        }
        if (!construction) BinTools::Read(shape, in);
        if (!in || buffer.failed() || (!construction && shape.IsNull())) {
            ::close(output);
            return {};
        }
        ::close(output);
        output = -1;
        if (std::chrono::steady_clock::now() >= parameters.deadline)
            return {false, {}, Failure::timed_out};
        sidecar = std::move(received);
        return construction ? IsolatedShapeResult{false, {}, Failure::construction}
                            : IsolatedShapeResult{true, std::move(shape), Failure::none};
    } catch (...) {
        if (output >= 0) ::close(output);
        if (std::chrono::steady_clock::now() >= parameters.deadline)
            return {false, {}, Failure::timed_out};
        return {};
    }
#else
    (void)parameters;
    return {};
#endif
}

int shape_build_worker_main(int argc, char** argv, const ShapeWorkerBuild& build) {
#if defined(__unix__) || defined(__APPLE__)
    constexpr int unavailable = static_cast<int>(ExecExit::unavailable);
    constexpr int timed_out = static_cast<int>(ExecExit::timed_out);
    if (argc != 7 || std::strcmp(argv[1], "--fillet-v1") != 0 || !build) return unavailable;
#if defined(__linux__)
    // The parent keeps its verified fd for subsequent probes; this duplicate
    // was needed only to resolve /proc/self/fd/5 during exec.
    ::close(kWorkerExecutable);
#endif
    std::uint64_t first_bits = 0, second_bits = 0;
    unsigned flags = 0;
    std::int64_t deadline_ns = 0;
    long parent = 0;
    if (!parse_integer(argv[2], first_bits) || !parse_integer(argv[3], second_bits) ||
        !parse_integer(argv[4], flags) || !parse_integer(argv[5], deadline_ns) ||
        !parse_integer(argv[6], parent) || (flags & ~255u) != 0 ||
        ((flags & 2u) && !(flags & 1u)) || parent <= 1 || ::getppid() != parent)
        return unavailable;
    FilletWorkerParameters parameters;
    parameters.radius = bits_double(first_bits);
    parameters.radius2 = bits_double(second_bits);
    parameters.variable = (flags & 1u) != 0;
    parameters.unequal_requested = (flags & 2u) != 0;
    parameters.recording = (flags & 4u) != 0;
    parameters.document_validation = (flags & 8u) != 0;
    parameters.runoff = (flags >> 4) & 3u;
    parameters.sweep = (flags >> 6) & 3u;
    parameters.deadline = std::chrono::steady_clock::time_point(
        std::chrono::duration_cast<std::chrono::steady_clock::duration>(
            std::chrono::nanoseconds(deadline_ns)));
    if (!std::isfinite(parameters.radius) ||
        (parameters.variable && !std::isfinite(parameters.radius2))) return unavailable;

#if defined(__linux__)
    // The process group was established atomically by posix_spawn.
    chudware::platform::guardParentDeath(parent);
#else
    // Darwin's guardParentDeath forks a babysitter. Do not fork again before
    // geometry: a fresh worker can monitor its owning process without cloning
    // any library locks.
    try {
        std::thread([parent] {
            while (::getppid() == parent)
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            ::_exit(unavailable);
        }).detach();
    } catch (...) {
        return unavailable;
    }
#endif
#if defined(CHUDWARE_WORKER_POST_EXEC_CLOSE)
    if (!close_worker_descriptors()) return unavailable;
#endif
    if (std::chrono::steady_clock::now() >= parameters.deadline) return timed_out;
    if (const char* fault = std::getenv("CHUDWARE_TEST_BLEND_SIGSEGV");
        fault && fault[0] == '1') ::raise(SIGSEGV);
    try {
        if (::lseek(kWorkerInput, 0, SEEK_SET) < 0 ||
            ::lseek(kWorkerOutput, 0, SEEK_SET) < 0) return unavailable;
        FdInputBuffer input_buffer(kWorkerInput);
        std::istream input(&input_buffer);
        input.imbue(std::locale::classic());
        TopoDS_Shape source;
        BinTools::Read(source, input);
        if (input_buffer.failed() || source.IsNull()) return unavailable;
        std::uint64_t count = 0;
        input.read(reinterpret_cast<char*>(&count), sizeof(count));
        const auto position = input.tellg();
        struct stat st{};
        if (!input || position < 0 || ::fstat(kWorkerInput, &st) != 0 ||
            st.st_size < static_cast<off_t>(position) ||
            count > static_cast<std::uint64_t>(st.st_size - static_cast<off_t>(position)) /
                        (sizeof(std::uint32_t) * 2) ||
            count > std::numeric_limits<std::size_t>::max()) return unavailable;
        std::vector<ShapeWorkerEdge> edges(static_cast<std::size_t>(count));
        for (auto& edge : edges) {
            input.read(reinterpret_cast<char*>(&edge.index), sizeof(edge.index));
            input.read(reinterpret_cast<char*>(&edge.orientation), sizeof(edge.orientation));
            if (!input || edge.index == 0 || edge.orientation > 3) return unavailable;
        }
        g_bounded_task = true;
        std::string sidecar;
        IsolatedShapeResult result;
        try {
            result = build(source, edges, parameters, sidecar);
        } catch (...) {
            result = {false, {}, ShapeBuildFailure::construction};
            sidecar.clear();
        }
        if (std::chrono::steady_clock::now() >= parameters.deadline) return timed_out;
        if (!result.ok && result.failure == ShapeBuildFailure::timed_out) return timed_out;
        if ((result.ok && (result.failure != ShapeBuildFailure::none || result.shape.IsNull())) ||
            (!result.ok && result.failure != ShapeBuildFailure::construction)) return unavailable;

        FdOutputBuffer output_buffer(kWorkerOutput);
        std::ostream output(&output_buffer);
        output.imbue(std::locale::classic());
        const std::uint64_t length = sidecar.size();
        output.write(reinterpret_cast<const char*>(&length), sizeof(length));
        output.write(sidecar.data(), static_cast<std::streamsize>(sidecar.size()));
        if (result.ok) BinTools::Write(result.shape, output);
        output.flush();
        if (!output) return unavailable;
        if (std::chrono::steady_clock::now() >= parameters.deadline) return timed_out;
        return static_cast<int>(result.ok ? ExecExit::success : ExecExit::construction);
    } catch (...) {
        return unavailable;
    }
#else
    (void)argc;
    (void)argv;
    (void)build;
    return 103;
#endif
}

IsolatedTaskResult isolate_task(const std::function<std::string()>& run,
                               std::chrono::steady_clock::time_point deadline) {
#if defined(__unix__) || defined(__APPLE__)
    using Status = IsolatedTaskStatus;
    if (std::chrono::steady_clock::now() >= deadline) return {Status::timed_out, {}};
    int output[2];
#if defined(__linux__)
    if (::pipe2(output, O_CLOEXEC) != 0) return {};
#else
    if (::pipe(output) != 0) return {};
#endif
    if (::fcntl(output[0], F_SETFL, O_NONBLOCK) < 0 ||
        ::fcntl(output[0], F_SETFD, FD_CLOEXEC) < 0 ||
        ::fcntl(output[1], F_SETFD, FD_CLOEXEC) < 0) {
        ::close(output[0]);
        ::close(output[1]);
        return {};
    }
    const pid_t parent = ::getpid();
    const pid_t pid = ::fork();
    if (pid < 0) {
        ::close(output[0]);
        ::close(output[1]);
        return {};
    }
    if (pid == 0) {
        // Never retain a daemon listener, worker control socket, or GUI stream.
        // fd 3 is the sole result channel; normal native diagnostics go to null.
        if (::setpgid(0, 0) != 0) ::_exit(1);
        ::close(output[0]);
        if (output[1] != 3 && ::dup2(output[1], 3) < 0) ::_exit(1);
        if (::fcntl(3, F_SETFD, FD_CLOEXEC) < 0) ::_exit(1);
        const int nullfd = ::open("/dev/null", O_RDWR);
        if (nullfd < 0) ::_exit(1);
        for (int fd = 0; fd < 3; ++fd)
            if (::dup2(nullfd, fd) < 0) ::_exit(1);
        chudware::platform::closeInheritedFds(4);
        chudware::platform::guardParentDeath(parent);
        for (const int sig : {SIGSEGV, SIGBUS, SIGFPE, SIGILL, SIGABRT, SIGTRAP, SIGPIPE})
            ::signal(sig, SIG_DFL);
        g_bounded_task = true;
        try {
            const std::string payload = run();
            std::size_t sent = 0;
            while (sent < payload.size()) {
                const ssize_t n = ::write(3, payload.data() + sent, payload.size() - sent);
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) ::_exit(1);
                sent += static_cast<std::size_t>(n);
            }
            ::_exit(0);
        } catch (...) {
            ::_exit(1);
        }
    }
    ::setpgid(pid, pid);  // mirror the child; either side may win the race
    ::close(output[1]);
    int status = 0;
    bool reaped = false;
    auto finish = [&](Status result) -> IsolatedTaskResult {
        // Include unexpected descendants, not just the immediate rebuild process.
        ::kill(-pid, SIGKILL);
        if (!reaped) {
            ::kill(pid, SIGKILL);
            while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
        }
        ::close(output[0]);
        return {result, {}};
    };
    std::string payload;
    bool eof = false;
    constexpr std::size_t max_payload = 16 * 1024 * 1024;
    for (;;) {
        if (std::chrono::steady_clock::now() >= deadline) return finish(Status::timed_out);
        char chunk[16384];
        const ssize_t n = ::read(output[0], chunk, sizeof(chunk));
        if (n > 0) {
            if (payload.size() + static_cast<std::size_t>(n) > max_payload)
                return finish(Status::failed);
            payload.append(chunk, static_cast<std::size_t>(n));
            continue;
        }
        if (n == 0) eof = true;
        else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
            return finish(Status::failed);
        if (!reaped) {
            const pid_t got = ::waitpid(pid, &status, WNOHANG);
            if (got == pid) reaped = true;
            else if (got < 0 && errno != EINTR) return finish(Status::failed);
        }
        if (reaped && eof) {
            if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) return finish(Status::failed);
            finish(Status::completed);
            return {Status::completed, std::move(payload)};
        }
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        struct pollfd pfd = {output[0], POLLIN, 0};
        if (::poll(&pfd, 1, static_cast<int>(std::clamp<long long>(remaining, 1, 10))) < 0 &&
            errno != EINTR)
            return finish(Status::failed);
    }
#else
    (void)run;
    (void)deadline;
    return {};
#endif
}

}  // namespace chudware::util
