#!/usr/bin/env python3
"""Offline release evidence checks using real CTest and disposable test files.

Run with: python3 scripts/test_run_release_ctest.py
No configure, compiler, build tree, credential, or daemon is needed.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


SCRIPT = Path(__file__).with_name("run_release_ctest.py").resolve()
_SPEC = importlib.util.spec_from_file_location("release_ctest", SCRIPT)
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


def cmake_argument(value):
    return "[===[" + str(value) + "]===]"


@unittest.skipUnless(shutil.which("ctest"), "real CTest must be installed")
class ReleaseCTestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chudware-ctest-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = self.root / "core suite"
        self.host = self.root / "host suite"
        self.out = self.root / "retained results"
        self.core.mkdir()
        self.host.mkdir()

    def suite(self, directory, commands, disabled=(), skipped=()):
        lines = []
        for index, command in enumerate(commands):
            name = directory.name.replace(" ", "_") + "_" + str(index)
            lines.append("add_test(%s %s -c %s)" % (
                cmake_argument(name), cmake_argument(sys.executable),
                cmake_argument(command)))
            if index in disabled:
                lines.append("set_tests_properties(%s PROPERTIES DISABLED TRUE)"
                             % cmake_argument(name))
            if index in skipped:
                lines.append("set_tests_properties(%s PROPERTIES SKIP_RETURN_CODE 77)"
                             % cmake_argument(name))
        (directory / "CTestTestfile.cmake").write_text("\n".join(lines) + "\n",
                                                        encoding="utf-8")

    def invoke(self, timeout=None):
        argv = [sys.executable, str(SCRIPT), "--core-dir", str(self.core),
                "--host-dir", str(self.host), "--out", str(self.out)]
        if timeout is not None:
            argv.extend(["--timeout", str(timeout)])
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45,
                                encoding="utf-8", errors="replace", check=False)
        results_path = self.out / "test-results.json"
        self.assertTrue(results_path.is_file(), result.stdout + result.stderr)
        raw = results_path.read_bytes()
        evidence = json.loads(raw)
        canonical = (json.dumps(evidence, sort_keys=True, ensure_ascii=True,
                                separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
        self.assertEqual(raw, canonical)
        self.assertEqual(evidence["schema"], "chudware-ctest-results-v1")
        self.assertEqual([item["command"] for item in evidence["commands"]],
                         ["ctest core", "ctest host"])
        for name, directory, item in zip(("core", "host"), (self.core, self.host),
                                         evidence["commands"]):
            self.assertEqual(item["junit"], name + "-junit.xml")
            self.assertEqual(item["log"], name + "-ctest.log")
            self.assertEqual(item["argv"], [
                "ctest", "--test-dir", str(directory.resolve()), "--timeout",
                "900" if timeout is None else runner.positive_seconds(str(timeout)),
                "--no-tests=error", "--output-on-failure", "--output-junit",
                str(self.out / item["junit"]),
            ])
            log = (self.out / item["log"]).read_bytes()
            self.assertEqual(item["log_sha256"], hashlib.sha256(log).hexdigest())
            streamed = log.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            self.assertIn(streamed, result.stdout)
            junit = self.out / item["junit"]
            if junit.is_file():
                self.assertEqual(item["junit_sha256"],
                                 hashlib.sha256(junit.read_bytes()).hexdigest())
            else:
                self.assertIsNone(item["junit_sha256"])
            if item["passed"]:
                self.assertEqual(item["exit_code"], 0)
                self.assertEqual({key: item[key] for key in runner.COUNT_KEYS},
                                 runner.parse_junit_counts(junit.read_bytes()))
                self.assertGreater(item["tests"] - item["skipped"], 0)
                self.assertEqual(item["failures"], 0)
                self.assertEqual(item["errors"], 0)
        self.assertEqual(evidence["passed"], all(item["passed"] for item in
                                               evidence["commands"]))
        return result, evidence

    def test_unicode_paths_produce_canonical_ascii_json_bytes(self):
        self.core = self.root / "core \u03c0"
        self.host = self.root / "host \u03c0"
        self.out = self.root / "results \u03c0"
        self.core.mkdir()
        self.host.mkdir()
        self.positive()
        raw = (self.out / "test-results.json").read_bytes()
        self.assertTrue(raw.isascii())
        self.assertIn(b"\\u03c0", raw)

    def positive(self):
        marker = self.root / "core-finished"
        self.suite(self.core, ["print('core-first')",
                              "from pathlib import Path; Path(%r).write_text('finished')"
                              % str(marker)])
        self.suite(self.host, [
            "from pathlib import Path; assert Path(%r).read_text() == 'finished'"
            % str(marker), "print('host-second')", "print('host-third')",
        ])
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(evidence["passed"])
        return evidence

    def test_positive_counts_and_sequential_execution(self):
        evidence = self.positive()
        self.assertEqual([item["tests"] for item in evidence["commands"]], [2, 3])
        for item in evidence["commands"]:
            self.assertEqual(item["skipped"], 0)

    def test_failure_keeps_output_and_still_runs_host(self):
        self.suite(self.core, ["print('consumer-visible-failure'); raise SystemExit(7)"])
        self.suite(self.host, ["print('host-still-runs')"])
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(evidence["passed"])
        core, host = evidence["commands"]
        self.assertNotEqual(core["exit_code"], 0)
        self.assertEqual((core["tests"], core["failures"], core["errors"], core["skipped"]),
                         (1, 1, 0, 0))
        self.assertFalse(core["passed"])
        self.assertIn("consumer-visible-failure", result.stdout)
        self.assertIn("consumer-visible-failure",
                      (self.out / core["log"]).read_text(encoding="utf-8"))
        self.assertTrue(host["passed"])
        self.assertEqual(host["tests"], 1)

    def test_zero_tests_cannot_pass(self):
        self.suite(self.core, [])
        self.suite(self.host, ["print('real-host-test')"])
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        core, host = evidence["commands"]
        self.assertFalse(core["passed"])
        self.assertNotEqual(core["exit_code"], 0)
        self.assertIn(core["tests"], (None, 0))
        self.assertTrue(core["reason"])
        self.assertTrue(host["passed"])

    def test_all_disabled_tests_are_refused(self):
        self.suite(self.core, ["raise SystemExit(9)", "raise SystemExit(9)"],
                   disabled=(0, 1))
        self.suite(self.host, ["print('real-host-test')"])
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        core, host = evidence["commands"]
        self.assertEqual((core["tests"], core["skipped"]), (2, 2))
        self.assertFalse(core["passed"])
        self.assertIn("executed no tests", core["reason"])
        self.assertTrue(host["passed"])

    def test_mixed_disabled_and_skipped_counts_are_normalized(self):
        self.suite(self.core, ["print('executed')", "raise SystemExit(9)",
                              "raise SystemExit(77)"], disabled=(1,), skipped=(2,))
        self.suite(self.host, ["print('real-host-test')"])
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        core = evidence["commands"][0]
        self.assertEqual((core["tests"], core["skipped"]), (3, 2))
        self.assertTrue(core["passed"])

    def test_timeout_records_real_failure_and_runs_host(self):
        self.suite(self.core, ["import time; time.sleep(20)"])
        self.suite(self.host, ["print('host-after-timeout')"])
        result, evidence = self.invoke(timeout=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        core, host = evidence["commands"]
        self.assertNotEqual(core["exit_code"], 0)
        self.assertFalse(core["passed"])
        self.assertEqual((core["tests"], core["failures"]), (1, 1))
        self.assertTrue(host["passed"])

    def test_missing_xml_does_not_reuse_previous_success(self):
        self.positive()
        (self.core / "CTestTestfile.cmake").write_text(
            'message(FATAL_ERROR "invalid disposable CTest configuration")\n',
            encoding="utf-8")
        result, evidence = self.invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        core, host = evidence["commands"]
        self.assertNotEqual(core["exit_code"], 0)
        self.assertFalse(core["passed"])
        self.assertFalse((self.out / core["junit"]).exists())
        for key in runner.COUNT_KEYS:
            self.assertIsNone(core[key])
        self.assertIn("missing JUnit evidence", core["reason"])
        self.assertTrue(host["passed"])

    def test_malformed_and_count_disagreements_are_rejected(self):
        evidence = self.positive()
        raw = (self.out / evidence["commands"][0]["junit"]).read_bytes()
        with self.assertRaises(ValueError):
            runner.parse_junit_counts(raw[:len(raw) // 2])
        for key in ("tests", "failures", "errors", "disabled", "skipped"):
            with self.subTest(key=key):
                root = ET.fromstring(raw)
                root.set(key, str(int(root.get(key, "0")) + 1))
                with self.assertRaisesRegex(ValueError, "count disagreement"):
                    runner.parse_junit_counts(ET.tostring(root))
        root = ET.fromstring(raw)
        del root.attrib["tests"]
        with self.assertRaisesRegex(ValueError, "missing tests"):
            runner.parse_junit_counts(ET.tostring(root))
        root = ET.fromstring(raw)
        root.set("tests", "-1")
        with self.assertRaisesRegex(ValueError, "invalid tests"):
            runner.parse_junit_counts(ET.tostring(root))

    def test_error_cases_are_counted_not_passed(self):
        self.suite(self.core, ["raise SystemExit(9)"])
        self.suite(self.host, ["print('real-host-test')"])
        _, evidence = self.invoke()
        root = ET.fromstring((self.out / evidence["commands"][0]["junit"]).read_bytes())
        root.set("failures", "0")
        root.set("errors", "1")
        root.find("testcase/failure").tag = "error"
        self.assertEqual(runner.parse_junit_counts(ET.tostring(root)),
                         {"tests": 1, "failures": 0, "errors": 1, "skipped": 0})


if __name__ == "__main__":
    unittest.main()
