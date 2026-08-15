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

IsolatedShapeResult isolate_shape_build(const std::function<TopoDS_Shape()>& build,
                                        std::chrono::milliseconds timeout) {
#if defined(__unix__) || defined(__APPLE__)
    if (!shape_isolation_enabled()) {
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
            return {};
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            // Timeout. A fork() in a multithreaded process can deadlock in the child on
            // a malloc lock another thread held at fork time, so this is not necessarily
            // a hung blend; either way the caller treats it as an unbuildable subset.
            ::kill(pid, SIGKILL);
            ::waitpid(pid, &status, 0);
            ::close(fd);
            ::unlink(filename.c_str());
            return {};
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
