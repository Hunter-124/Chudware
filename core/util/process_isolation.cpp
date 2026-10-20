// Fault isolation for OCCT shape builders (T-BL61).
//
// OCCT's fillet corner resolver does not throw when it fails on a convergent-blend
// vertex — it dereferences into Geom2dAdaptor_Curve::D0 and segfaults, which killed
// chudware-hostd and every document open in it. A catch() cannot contain a signal, so
// the build runs in a forked child instead: the child serializes the finished shape and
// the parent maps a signal, a non-zero exit or a timeout onto the same "this edge set is
// unbuildable" answer the drop-failing path already handles.
#include "util/process_isolation.h"
#include "util/parent_death.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <thread>

#include <BinTools.hxx>

#if defined(__unix__) || defined(__APPLE__)
#include <cerrno>
#include <csignal>
#include <fcntl.h>
#include <poll.h>
#include <sys/wait.h>
#include <unistd.h>
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
// So a TIMEOUT is treated as "fork is unusable here", not as "this geometry is bad": the build
// is retried in-process (identical inputs, identical result) and isolation is disabled for the
// rest of the process. A SIGNAL still means the geometry killed OCCT and is still contained -
// which is the case T-BL61 exists for, and it is reported immediately, with no timeout.
bool g_fork_unusable = false;
// Private to the forked advisory child. Its outer boundary already contains all
// nested geometry and must never inherit the ordinary blend fallback policy.
bool g_bounded_task = false;
}  // namespace

bool shape_isolation_active() {
#if defined(__unix__) || defined(__APPLE__)
    return !g_bounded_task && !g_fork_unusable && shape_isolation_enabled();
#else
    return false;
#endif
}

IsolatedShapeResult isolate_shape_build(const std::function<TopoDS_Shape()>& build,
                                        std::chrono::milliseconds timeout,
                                        std::string* sidecar) {
    if (sidecar) sidecar->clear();
    // In-process build: the lambda fills *sidecar itself; a failed build leaves it empty.
    auto in_process = [&]() -> IsolatedShapeResult {
        if (sidecar) sidecar->clear();
        // Deterministic native-stall injection, confined to an expendable advisory
        // child. It exercises the same watchdog as a non-returning OCCT builder.
        if (g_bounded_task) {
            if (const char* delay = std::getenv("CHUDWARE_TEST_BLEND_DELAY_MS")) {
                const long ms = std::strtol(delay, nullptr, 10);
                if (ms > 0) std::this_thread::sleep_for(std::chrono::milliseconds(ms));
            }
        }
        TopoDS_Shape s = build();
        if (s.IsNull() && sidecar) sidecar->clear();
        return {!s.IsNull(), s};
    };
#if defined(__unix__) || defined(__APPLE__)
    if (g_bounded_task || g_fork_unusable || !shape_isolation_enabled()) return in_process();

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
        bool ok = false;
        try {
            if (const char* fault = std::getenv("CHUDWARE_TEST_BLEND_SIGSEGV");
                fault && fault[0] == '1') {
                ::raise(SIGSEGV);
            }
            TopoDS_Shape s = build();
            if (!s.IsNull()) {
                // Payload: [u64 sidecar length][sidecar bytes][BinTools shape].
                std::ofstream out(filename, std::ios::binary | std::ios::trunc);
                const std::string side = sidecar ? *sidecar : std::string();
                const std::uint64_t n = side.size();
                out.write(reinterpret_cast<const char*>(&n), sizeof(n));
                out.write(side.data(), static_cast<std::streamsize>(side.size()));
                BinTools::Write(s, out);
                ok = out.good();
            }
        } catch (...) {}
        ::close(fd);
        ::_exit(ok ? 0 : 1);
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
            ::kill(pid, SIGKILL);
            ::waitpid(pid, &status, 0);
            ::close(fd);
            ::unlink(filename.c_str());
            g_fork_unusable = true;
            return in_process();
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            // The child never finished. Almost certainly a fork-in-a-multithreaded-process
            // deadlock rather than a slow blend (a document-scale blend is milliseconds, and
            // the budget is seconds), so stop isolating in THIS process and answer in-process.
            ::kill(pid, SIGKILL);
            ::waitpid(pid, &status, 0);
            ::close(fd);
            ::unlink(filename.c_str());
            g_fork_unusable = true;
            return in_process();
        }
        ::usleep(nap);
        if (nap < 10000) nap *= 2;
    }
    ::close(fd);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        ::unlink(filename.c_str());
        return {};
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
    } catch (...) {
        // A truncated or unreadable payload is an unbuildable subset, not a throw.
        shape = TopoDS_Shape();
    }
    const bool ok = !shape.IsNull();
    ::unlink(filename.c_str());
    if (ok && sidecar) *sidecar = std::move(side);
    return {ok, shape};
#else
    (void)timeout;
    return in_process();
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
