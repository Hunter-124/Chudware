#pragma once
// Post-fork, pre-exec child hardening. POSIX only — the Windows daemon gets the
// same invariant from a Job Object with KILL_ON_JOB_CLOSE
// (core/hostd/child_supervisor_win.cpp), so nothing here is declared there.
//
// Everything below runs between fork() and exec() in a multi-threaded parent
// and is therefore restricted to async-signal-safe work: raw syscalls only, no
// allocation, no locks, no stdio.

#if !defined(_WIN32)

namespace chudware::platform {

// Bind this child's lifetime to `parentPid`: when that process dies, this
// child's whole process group is SIGKILLed. This is the invariant
// core/hostd/child_supervisor.h states — daemon death kills every child.
//
// Call AFTER setpgid(0, 0) (the group is what gets killed) and BEFORE exec.
//
//  * Linux  prctl(PR_SET_PDEATHSIG, SIGKILL) — the kernel does the work — plus
//           the getppid() re-check that closes the fork/prctl race.
//  * Darwin has no PDEATHSIG, and a kqueue registration survives neither fork
//           nor execve, so the only working shape is a babysitter: this process
//           forks once more, the grandchild returns from here and execs, and
//           THIS process stays behind watching `parentPid` with
//           EVFILT_PROC/NOTE_EXIT. The babysitter is transparent to the caller
//           — it keeps the pid and pgid the caller already recorded, and exits
//           with the exec'd child's exact status (re-raising its terminating
//           signal when it was killed), so existing waitpid() handling and exit
//           codes are unchanged.
//
// Does not return in the process that must die: _exit(125) when `parentPid` is
// already gone. A failed second fork on Darwin degrades to an unguarded child —
// a spawned-but-unguarded child beats a failed spawn, and it is exactly the
// silent degradation a failing prctl() already gives on Linux.
void guardParentDeath(long parentPid);

// Close every inherited descriptor from `lowest` up, so a daemon's listen and
// client sockets never leak into an exec'd worker. Linux has close_range(2);
// every other POSIX gets the sysconf(_SC_OPEN_MAX) loop.
void closeInheritedFds(int lowest);

}  // namespace chudware::platform

#endif  // !_WIN32
