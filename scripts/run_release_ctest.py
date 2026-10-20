#!/usr/bin/env python3
"""Run both native CTest suites and retain process-backed release evidence."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


SCHEMA = "chudware-ctest-results-v1"
COUNT_KEYS = ("tests", "failures", "errors", "skipped")

def parse_junit_counts(raw: bytes) -> dict[str, int]:
    """Count actual cases, rejecting inconsistent CTest JUnit summaries.

    CTest represents disabled cases separately from skipped cases, sometimes
    without a <skipped> child. Both are unexecuted for release evidence.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("malformed JUnit XML: " + str(exc)) from exc
    if root.tag != "testsuite":
        raise ValueError("expected CTest JUnit testsuite root")

    def declared(name, default=None):
        value = root.get(name)
        if value is None:
            if default is not None:
                return default
            raise ValueError("JUnit summary is missing " + name)
        if not value.isascii() or not value.isdigit():
            raise ValueError("JUnit summary has invalid " + name)
        return int(value)

    counts = dict.fromkeys(COUNT_KEYS, 0)
    disabled = 0
    skipped = 0
    for case in root.findall("testcase"):
        counts["tests"] += 1
        outcomes = {name: len(case.findall(name)) for name in
                    ("failure", "error", "skipped")}
        if any(value > 1 for value in outcomes.values()) or sum(outcomes.values()) > 1:
            raise ValueError("JUnit testcase has conflicting outcomes")
        status = case.get("status")
        if status not in ("run", "fail", "notrun", "disabled"):
            raise ValueError("JUnit testcase has missing or unknown CTest status")
        if status == "disabled":
            if outcomes["failure"] or outcomes["error"]:
                raise ValueError("disabled JUnit testcase has a failure/error")
            disabled += 1
        elif status == "notrun":
            if not outcomes["skipped"]:
                raise ValueError("notrun JUnit testcase lacks skipped outcome")
            skipped += 1
        elif status == "fail":
            if not (outcomes["failure"] or outcomes["error"]):
                raise ValueError("failed JUnit testcase lacks failure/error outcome")
        elif sum(outcomes.values()):
            raise ValueError("run JUnit testcase has nonpassing outcome")
        counts["failures"] += outcomes["failure"]
        counts["errors"] += outcomes["error"]

    expected = {
        "tests": counts["tests"],
        "failures": counts["failures"],
        "errors": counts["errors"],
        "disabled": disabled,
        "skipped": skipped,
    }
    for key, actual in expected.items():
        reported = declared(key, 0 if key in ("errors", "disabled", "skipped") else None)
        if reported != actual:
            raise ValueError("JUnit count disagreement for %s: summary=%d, cases=%d"
                             % (key, reported, actual))
    counts["skipped"] = skipped + disabled
    return counts


def sha256(path):
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_suite(name, directory, out, timeout):
    junit = out / (name + "-junit.xml")
    log = out / (name + "-ctest.log")
    # A failed launch or no-tests run must not reuse a previous successful XML.
    if junit.exists():
        junit.unlink()
    argv = ["ctest", "--test-dir", str(directory.resolve()), "--timeout", timeout,
            "--no-tests=error", "--output-on-failure", "--output-junit", str(junit)]
    record = {
        "command": "ctest " + name,
        "argv": argv,
        "exit_code": None,
        **dict.fromkeys(COUNT_KEYS),
        "passed": False,
        "junit": junit.name,
        "log": log.name,
    }
    reasons = []
    with log.open("wb") as sink:
        try:
            with subprocess.Popen(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT) as process:
                while True:
                    chunk = process.stdout.read1(65536)
                    if not chunk:
                        break
                    sink.write(chunk)
                    sink.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                record["exit_code"] = process.wait()
        except OSError as exc:
            reasons.append("could not launch CTest: " + str(exc))
            message = (reasons[-1] + "\n").encode("utf-8")
            sink.write(message)
            sys.stderr.write(reasons[-1] + "\n")

    try:
        record.update(parse_junit_counts(junit.read_bytes()))
    except (OSError, ValueError) as exc:
        reasons.append("invalid or missing JUnit evidence: " + str(exc))
    if record["exit_code"] is not None and record["exit_code"] != 0:
        reasons.append("CTest exited with status %d" % record["exit_code"])
    if record["tests"] is not None:
        if record["tests"] - record["skipped"] <= 0:
            reasons.append("CTest suite executed no tests")
        if record["failures"] or record["errors"]:
            reasons.append("JUnit reports test failures/errors")
    record["passed"] = not reasons and record["exit_code"] == 0
    if reasons:
        record["reason"] = "; ".join(reasons)
    record["junit_sha256"] = sha256(junit)
    record["log_sha256"] = sha256(log)
    return record


def positive_seconds(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return format(seconds, ".15g")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-dir", type=Path, required=True)
    parser.add_argument("--host-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=positive_seconds, default="900")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "test-results.json"
    if results_path.exists():
        results_path.unlink()
    commands = [run_suite("core", args.core_dir, out, args.timeout),
                run_suite("host", args.host_dir, out, args.timeout)]
    results = {"schema": SCHEMA, "passed": all(item["passed"] for item in commands),
               "commands": commands}
    results_path.write_bytes((json.dumps(results, sort_keys=True, ensure_ascii=True,
                                        separators=(",", ":"), allow_nan=False) + "\n").encode("ascii"))
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
