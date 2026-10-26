// See util/parent_death.h.
#include "util/parent_death.h"

#if !defined(_WIN32)

#include <cerrno>
#include <csignal>
#include <sys/wait.h>
#include <unistd.h>

#if defined(__linux__)
#  include <sys/prctl.h>
#elif defined(__APPLE__)
#  include <sys/event.h>
#  include <sys/types.h>
#endif

namespace chudware::platform {

void closeInheritedFds(int lowest) {
    if (lowest < 0) lowest = 0;
#if defined(__linux__)
    ::close_range((unsigned)lowest, ~0U, 0);
#else
    long maxfd = ::sysconf(_SC_OPEN_MAX);
    if (maxfd < 0) maxfd = 4096;
    // _SC_OPEN_MAX follows RLIMIT_NOFILE, which a caller may have raised into
    // the millions. A million close(2) calls before exec is a visible stall, so
    // stop at a bound no real descriptor table reaches in this daemon.
    if (maxfd > 65536) maxfd = 65536;
    for (int fd = lowest; fd < (int)maxfd; ++fd) ::close(fd);
#endif
}

#if defined(__APPLE__)
namespace {

// Runs in the process the caller believes IS the child: same pid, same pgid.
[[noreturn]] void babysit(pid_t child, long parentPid) {
    // Hold nothing the caller expects the exec'd child to own. child_supervisor
    // reads its CLOEXEC exec-error pipe to EOF; an extra writer here would hang
    // that read forever. stdin/out/err stay — they are shared with the child.
    closeInheritedFds(3);

    const int kq = ::kqueue();
    auto kill_group = []() -> void {
        ::killpg(::getpgrp(), SIGKILL);
        _exit(125);
    };
    struct kevent ev;
    EV_SET(&ev, (uintptr_t)parentPid, EVFILT_PROC, EV_ADD | EV_ENABLE,
           NOTE_EXIT, 0, nullptr);
    int registered = -1;
    if (kq >= 0) {
        do {
            registered = ::kevent(kq, &ev, 1, nullptr, 0, nullptr);
        } while (registered < 0 && errno == EINTR);
    }
    // Watch the parent independently: failure to watch a child which has
    // ALREADY exited must not be mistaken for failure to guard its parent.
    if (registered < 0 || ::getppid() != (pid_t)parentPid) kill_group();

    EV_SET(&ev, (uintptr_t)child, EVFILT_PROC, EV_ADD | EV_ENABLE,
           NOTE_EXIT, 0, nullptr);
    do {
        registered = ::kevent(kq, &ev, 1, nullptr, 0, nullptr);
    } while (registered < 0 && errno == EINTR);
    // proc_find cannot attach a NOTE_EXIT watch to an already-exited process.
    // This is common for short callbacks: closing inherited fds above can take
    // longer than the child's entire task. ESRCH alone is NOT a successful
    // child exit; only waitpid below proves that and supplies its exact status.
    bool exiting = registered < 0 && errno == ESRCH;
    if (registered < 0 && !exiting) kill_group();

    int st = 0;
    const struct timespec exit_poll = {0, 10 * 1000 * 1000};
    for (;;) {
        const pid_t got = ::waitpid(child, &st, WNOHANG);
        if (got == child) break;
        if (got < 0) {
            if (errno == EINTR) continue;
            kill_group();
        }
        struct kevent out;
        // NOTE_EXIT can precede waitpid's reapable state. During that narrow
        // window (or an ESRCH registration), keep watching the parent instead
        // of blocking in waitpid and abandoning parent-death containment.
        const int n = ::kevent(kq, nullptr, 0, &out, 1,
                               exiting ? &exit_poll : nullptr);
        if (n < 0) {
            if (errno == EINTR) continue;
            kill_group();
        }
        if (n == 0) continue;
        if ((long)out.ident == parentPid) kill_group();
        if ((pid_t)out.ident == child) exiting = true;
    }

    // Transparent exit: the caller's waitpid() observes the child's real
    // status even when the child finished before its NOTE_EXIT registration.
    if (WIFSIGNALED(st)) {
        const int sig = WTERMSIG(st);
        ::signal(sig, SIG_DFL);
        sigset_t unblock;
        sigemptyset(&unblock);
        sigaddset(&unblock, sig);
        ::sigprocmask(SIG_UNBLOCK, &unblock, nullptr);
        ::raise(sig);
    }
    _exit(WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st));
}

}  // namespace
#endif  // __APPLE__

void guardParentDeath(long parentPid) {
#if defined(__linux__)
    ::prctl(PR_SET_PDEATHSIG, SIGKILL);
    // Race guard: if the parent died between fork and prctl, PDEATHSIG never
    // fires — re-check the parent is still the one we were forked from.
    if (::getppid() != (pid_t)parentPid) _exit(125);
#elif defined(__APPLE__)
    const pid_t child = ::fork();
    if (child == 0) return;  // grandchild: proceed to exec
    if (child < 0) return;   // out of processes: unguarded beats unspawned
    babysit(child, parentPid);
#else
    (void)parentPid;  // no parent-death primitive on this POSIX
#endif
}

}  // namespace chudware::platform

#endif  // !_WIN32
