"""run_bounded.py against real process trees: deadlines, status, cleanup.

Standard library unittest, so the macOS Command Line Tools interpreter runs it
as well as pytest does:

    python3 scripts/test_run_bounded.py
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent / "run_bounded.py")


def bounded(*args, **kwargs):
    return subprocess.run([sys.executable, SCRIPT] + [str(a) for a in args], **kwargs)


def alive(pid):
    """True while pid is a live (non-zombie) process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # An orphan whose new parent never reaps it (PID 1 in a bare container)
    # lingers as a zombie: it is stopped, which is all a bound promises.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class RunBoundedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pid_from(self, name):
        path = self.dir / name
        for _ in range(100):
            if path.exists() and path.read_text().strip():
                return int(path.read_text())
            time.sleep(0.05)
        self.fail("{0} was never written".format(name))


    def test_child_failure_status_propagates(self):
        self.assertEqual(bounded(30, "sh", "-c", "exit 7").returncode, 7)
        self.assertEqual(bounded(30, "false").returncode, 1)

    def test_child_killed_by_signal_reports_128_plus_n(self):
        self.assertEqual(bounded(30, "sh", "-c", "kill -KILL $$").returncode, 128 + signal.SIGKILL)

    def test_missing_command_is_127(self):
        self.assertEqual(bounded(30, str(self.dir / "no-such-tool")).returncode, 127)

    def test_nonfinite_budgets_do_not_start_the_command(self):
        marker = self.dir / "ran"
        for value in ("nan", "inf", "-inf"):
            self.assertEqual(bounded(value, "touch", marker).returncode, 2)
            self.assertEqual(bounded("--not-after", value, 30, "touch", marker).returncode, 2)
        self.assertFalse(marker.exists())

    def test_deadline_stops_the_group_and_returns_124(self):
        script = "sleep 300 & echo $! > {0}/grandchild; wait".format(self.dir)
        start = time.monotonic()
        result = bounded(1, "sh", "-c", script, stderr=subprocess.PIPE, text=True)
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 124)
        self.assertLess(elapsed, 10)
        self.assertFalse(alive(self.pid_from("grandchild")))

    def test_term_ignoring_group_is_killed_after_the_grace(self):
        script = "trap '' TERM; sleep 300 & echo $! > {0}/grandchild; wait".format(self.dir)
        start = time.monotonic()
        result = bounded(1, "sh", "-c", script)
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 124)
        self.assertGreaterEqual(elapsed, 15)
        self.assertLess(elapsed, 25)
        self.assertFalse(alive(self.pid_from("grandchild")))

    def test_leftover_processes_are_stopped_when_the_command_exits(self):
        script = "sleep 300 & echo $! > {0}/straggler; exit 0".format(self.dir)
        result = bounded(30, "sh", "-c", script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(alive(self.pid_from("straggler")))

    def test_children_do_not_inherit_the_callers_extra_descriptors(self):
        # build_arm_sdk.sh prints its prefix on an extra descriptor (fd 3) that
        # the supervisor inherits; no bounded child may hold it open.
        read_end, write_end = os.pipe()
        child = "import os; os.write({0}, b'leaked')".format(write_end)
        try:
            result = bounded(30, sys.executable, "-c", child, pass_fds=(write_end,),
                             stderr=subprocess.DEVNULL)
        finally:
            os.close(write_end)
        with os.fdopen(read_end) as reader:
            self.assertEqual(reader.read(), "")
        self.assertNotEqual(result.returncode, 0)

    def test_past_shared_deadline_does_not_start_the_command(self):
        marker = self.dir / "ran"
        result = bounded("--not-after", int(time.time()) - 1, 30, "touch", marker)
        self.assertEqual(result.returncode, 124)
        self.assertFalse(marker.exists())

    def test_shared_deadline_caps_the_budget(self):
        start = time.monotonic()
        result = bounded("--not-after", int(time.time()) + 2, 300, "sleep", 300)
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - start, 15)

    def test_nested_bounds_leave_no_orphans(self):
        inner = "{0} {1} 300 sh -c 'sleep 300 & echo $! > {2}/grandchild; wait'".format(
            sys.executable, SCRIPT, self.dir)
        start = time.monotonic()
        result = bounded(2, "sh", "-c", inner)
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 124)
        # The inner supervisor's 10 s cancel grace ends inside the outer 15 s.
        self.assertLess(elapsed, 2 + 15)
        self.assertFalse(alive(self.pid_from("grandchild")))

    def test_cancelled_supervisor_stops_its_group(self):
        proc = subprocess.Popen([sys.executable, SCRIPT, "300", "sh", "-c",
                                 "sleep 300 & echo $! > {0}/grandchild; wait".format(self.dir)])
        grandchild = self.pid_from("grandchild")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 128 + signal.SIGTERM)
        self.assertFalse(alive(grandchild))


if __name__ == "__main__":
    unittest.main()
