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
    struct kevent ev[2];
    EV_SET(&ev[0], (uintptr_t)parentPid, EVFILT_PROC, EV_ADD | EV_ENABLE, NOTE_EXIT, 0, nullptr);
    EV_SET(&ev[1], (uintptr_t)child, EVFILT_PROC, EV_ADD | EV_ENABLE, NOTE_EXIT, 0, nullptr);
    const bool watching = kq >= 0 && ::kevent(kq, ev, 2, nullptr, 0, nullptr) == 0;

    // The ordering race PDEATHSIG exists to prevent: the parent may have died
    // between fork() and EV_ADD, in which case no NOTE_EXIT will ever arrive
    // and blocking here would strand the child forever. Registration failing
    // with ESRCH says the same thing; so does being reparented away.
    if (!watching || ::getppid() != (pid_t)parentPid) {
        ::killpg(::getpgrp(), SIGKILL);  // takes the exec'd child, then us
        _exit(125);
    }

    for (;;) {
        struct kevent out;
        const int n = ::kevent(kq, nullptr, 0, &out, 1, nullptr);
        if (n < 0) {
            if (errno == EINTR) continue;
            break;  // unusable queue: fall through to a plain wait on the child
        }
        if (n == 0) continue;
        if ((long)out.ident == parentPid) {
            ::killpg(::getpgrp(), SIGKILL);
            _exit(125);
        }
        if ((pid_t)out.ident == child) break;
    }

    // Transparent exit: the caller's waitpid() must observe exactly what it
    // would have observed had it forked the exec'd child directly.
    int st = 0;
    while (::waitpid(child, &st, 0) < 0 && errno == EINTR) {
    }
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
