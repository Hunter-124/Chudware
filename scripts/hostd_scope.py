#!/usr/bin/env python3
"""Find or stop only the chudware-hostd one run started (Linux /proc).

A smoke run launches its daemon under a scratch CHUDWARE_INSTANCE_KEY. Leftover
detection and cleanup must never touch another run's daemon or the user's own
one, so a process is this run's only when ALL hold:

  * every real/effective/saved/filesystem uid is this user's;
  * its executable (/proc/<pid>/exe) is named chudware-hostd — not comm, which an
    fexecve'd daemon reports as an fd number on pre-6.14 kernels;
  * its environment carries exactly one CHUDWARE_INSTANCE_KEY, equal to the key;
  * it started no earlier than the run's start mark (``now``).

Signals go through a pidfd opened BEFORE the identity check and re-validated
after it, so a recycled pid never receives them. Nothing matches by argv or path.

  hostd_scope.py now                          print this run's start mark
  hostd_scope.py list --key K --since MARK    print "pid exe" per match
  hostd_scope.py reap --key K --since MARK    SIGTERM, then SIGKILL, each match
"""
from __future__ import annotations

import argparse
import os
import select
import signal
import sys
from pathlib import Path

HOSTD = "chudware-hostd"
KEY_VAR = b"CHUDWARE_INSTANCE_KEY="


def start_ticks(pid: str) -> int:
    """Process start time in clock ticks since boot (/proc/<pid>/stat field 22)."""
    stat = Path(f"/proc/{pid}/stat").read_bytes()
    # comm may hold spaces or parens; the numeric fields resume after the last ')'.
    return int(stat[stat.rindex(b")") + 2:].split()[19])


def _owned_by(pid: str, uid: int) -> bool:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Uid:"):
            return all(int(value) == uid for value in line.split()[1:])
    return False


def match(pid: str, key: str, since: int, uid: int) -> str | None:
    """Return the executable when pid is the keyed run's hostd, else None."""
    try:
        if not _owned_by(pid, uid):
            return None
        exe = os.readlink(f"/proc/{pid}/exe").removesuffix(" (deleted)")
        if os.path.basename(exe) != HOSTD or start_ticks(pid) < since:
            return None
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except (OSError, ValueError, IndexError):
        return None  # exited, not ours to read, or not a hostd
    keys = [entry for entry in environ if entry.startswith(KEY_VAR)]
    return exe if keys == [KEY_VAR + os.fsencode(key)] else None


def candidates(key: str, since: int, uid: int) -> list[tuple[str, str]]:
    own = str(os.getpid())
    found = []
    for pid in os.listdir("/proc"):
        if pid.isdigit() and pid != own:
            exe = match(pid, key, since, uid)
            if exe is not None:
                found.append((pid, exe))
    return found


def _exited(pidfd: int, seconds: float) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(int(seconds * 1000)))


def reap(pid: str, key: str, since: int, uid: int, grace: float) -> bool:
    """Stop pid if it is still the keyed hostd; True when it is gone afterwards."""
    try:
        pidfd = os.pidfd_open(int(pid))
    except ProcessLookupError:
        return True
    try:
        if match(pid, key, since, uid) is None:
            return True  # no longer (or never) this run's daemon: leave it alone
        try:
            # The pidfd still names a live process, so the checks above read IT,
            # not a successor that recycled the pid.
            signal.pidfd_send_signal(pidfd, 0)
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            if _exited(pidfd, grace):
                return True
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            return True
        return _exited(pidfd, 5.0)
    finally:
        os.close(pidfd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("now")
    for name in ("list", "reap"):
        command = sub.add_parser(name)
        command.add_argument("--key", required=True)
        command.add_argument("--since", required=True, type=int)
        if name == "reap":
            command.add_argument("--grace", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.command == "now":
        print(start_ticks("self"))
        return 0
    if not args.key:
        parser.error("--key must be non-empty")
    uid = os.geteuid()
    found = candidates(args.key, args.since, uid)
    if args.command == "list":
        for pid, exe in found:
            print(pid, exe)
        return 0
    stuck = [pid for pid, _ in found if not reap(pid, args.key, args.since, uid, args.grace)]
    for pid, exe in found:
        print(("still running " if pid in stuck else "stopped ") + pid, exe)
    return 1 if stuck else 0


if __name__ == "__main__":
    sys.exit(main())
