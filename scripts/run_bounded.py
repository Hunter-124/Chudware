#!/usr/bin/env python3
"""Run one command under a hard wall-clock deadline, in its own process group.

    run_bounded.py [--not-after EPOCH] SECONDS COMMAND [ARG...]

macOS ships no GNU timeout(1), so the ARM SDK build and the ARM rows of
native-build.yml bound every long command with this instead. The command
starts in a new session, so it and everything it spawns share one process
group that nothing else is in. Exit status:

  * the command's own status when it finishes in time (128+N when signal N
    killed it), so a failing child fails the caller;
  * 124 when SECONDS elapse first: the whole group gets SIGTERM, then SIGKILL
    15 s later;
  * 128+N when this supervisor itself receives SIGTERM, SIGINT or SIGHUP (an
    outer run_bounded expiring, a cancelled job): the group gets the same
    TERM-then-KILL with a 10 s grace, which finishes inside an outer
    supervisor's 15 s grace, so nested bounds never orphan a group;
  * 127/126 when the command cannot be started.

Processes the command leaves behind in its group are stopped the same way
when it exits, so nothing outlives the bound. --not-after caps SECONDS at an
absolute Unix time, for a deadline several steps share (a CI job's budget); a
deadline already past exits 124 without starting the command.

Standard library only, and Python 3.9 compatible (the macOS Command Line Tools
interpreter).
"""

import os
import math
import shlex
import signal
import subprocess
import sys
import time

GRACE = 15.0
CANCEL_GRACE = 10.0
USAGE = "usage: run_bounded.py [--not-after EPOCH] SECONDS COMMAND [ARG...]"


def note(message, error=True):
    prefix = "::error::" if error and os.environ.get("GITHUB_ACTIONS") == "true" else ""
    print("{0}run_bounded: {1}".format(prefix, message), file=sys.stderr, flush=True)


def signal_group(pgid, sig):
    """Send sig to the group; False once no process is left in it."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS reports EPERM for a group that holds only zombies.
        return False


def stop_group(proc, grace):
    """SIGTERM the command's group, SIGKILL whatever is left after grace."""
    signal_group(proc.pid, signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        # Reap the leader first: a zombie leader keeps the group probe alive.
        if proc.poll() is not None and not signal_group(proc.pid, 0):
            return
        time.sleep(0.1)
    signal_group(proc.pid, signal.SIGKILL)
    proc.wait()


def main(argv):
    args = list(argv)
    not_after = None
    try:
        if args[:1] == ["--not-after"]:
            not_after = float(args[1])
            args = args[2:]
        seconds = float(args[0])
        command = args[1:]
    except (IndexError, ValueError):
        print(USAGE, file=sys.stderr)
        return 2
    if not math.isfinite(seconds) or seconds <= 0 or not command or (
        not_after is not None and not math.isfinite(not_after)
    ):
        print(USAGE, file=sys.stderr)
        return 2

    label = " ".join(shlex.quote(part) for part in command)
    if not_after is not None:
        seconds = min(seconds, not_after - time.time())
        if seconds <= 0:
            note("the shared deadline has already passed; not starting: {0}".format(label))
            return 124

    received = []

    def on_signal(signum, _frame):
        received.append(signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    try:
        proc = subprocess.Popen(command, start_new_session=True)
    except FileNotFoundError:
        note("command not found: {0}".format(label))
        return 127
    except PermissionError:
        note("command not executable: {0}".format(label))
        return 126

    deadline = time.monotonic() + seconds
    while True:
        try:
            status = proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            status = None
        if status is not None:
            if signal_group(proc.pid, 0):
                note("stopping processes left in the group by: {0}".format(label), error=False)
                stop_group(proc, GRACE)
            return 128 - status if status < 0 else status
        if received:
            note("received signal {0}; stopping the process group of: {1}".format(received[0], label))
            stop_group(proc, CANCEL_GRACE)
            return 128 + received[0]
        if time.monotonic() >= deadline:
            note("deadline of {0:.0f} s expired; stopping the process group of: {1}".format(seconds, label))
            stop_group(proc, GRACE)
            return 124


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
