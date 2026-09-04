// Fault isolation for OCCT shape builders (T-BL61).
//
// OCCT's fillet corner resolver does not throw when it fails on a convergent-blend
// vertex — it dereferences into Geom2dAdaptor_Curve::D0 and segfaults, which killed
// chudware-hostd and every document open in it. A catch() cannot contain a signal, so
// the build runs in a forked child instead: the child serializes the finished shape and
// the parent maps a signal, a non-zero exit or a timeout onto the same "this edge set is
// unbuildable" answer the drop-failing path already handles.
#include "util/process_isolation.h"

#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

#include <BinTools.hxx>

#if defined(__unix__) || defined(__APPLE__)
#include <cerrno>
#include <csignal>
#include <fcntl.h>
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
}  // namespace

IsolatedShapeResult isolate_shape_build(const std::function<TopoDS_Shape()>& build,
                                        std::chrono::milliseconds timeout) {
#if defined(__unix__) || defined(__APPLE__)
    if (g_fork_unusable || !shape_isolation_enabled()) {
        TopoDS_Shape s = build();
        return {!s.IsNull(), s};
    }

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
                std::ofstream out(filename, std::ios::binary | std::ios::trunc);
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
            TopoDS_Shape s = build();
            return {!s.IsNull(), s};
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
            TopoDS_Shape s = build();
            return {!s.IsNull(), s};
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
    try {
        std::ifstream in(filename, std::ios::binary);
        BinTools::Read(shape, in);
    } catch (...) {
        // A truncated or unreadable payload is an unbuildable subset, not a throw.
        shape = TopoDS_Shape();
    }
    const bool ok = !shape.IsNull();
    ::unlink(filename.c_str());
    return {ok, shape};
#else
    (void)timeout;
    TopoDS_Shape s = build();
    return {!s.IsNull(), s};
#endif
}

}  // namespace chudware::util
