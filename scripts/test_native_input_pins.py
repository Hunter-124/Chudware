#!/usr/bin/env python3
"""Offline consumer-boundary smoke tests; no SDK builds or upstream execution.

Run: python3 -m unittest discover -s scripts -p test_native_input_pins.py
PowerShell cases run when powershell/pwsh is installed. All caches and any
harmless CMake markers stay inside disposable temporary directories.
"""

import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CMAKE = shutil.which("cmake")
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def cmake_quote(path):
    return '"' + Path(path).as_posix() + '"'


@unittest.skipUnless(CMAKE, "CMake is unavailable")
class NativeArchiveTests(unittest.TestCase):
    def run_cmake(self, directory, text):
        script = directory / "smoke.cmake"
        script.write_text(text, encoding="utf-8")
        return subprocess.run([CMAKE, "-P", str(script)], cwd=directory,
                              capture_output=True, text=True, timeout=30)

    def test_sdk_cached_archive_rejected_even_with_extracted_config(self):
        cases = [("ProvisionOCCT.cmake", "OCCT_ROOT", "occt-combined.zip",
                  "opencascade-7.9.3-vc14-64/cmake/OpenCASCADEConfig.cmake"),
                 ("ProvisionLib3mf.cmake", "LIB3MF_ROOT", "lib3mf-2.5.0-Windows.zip",
                  "lib3mf-2.5.0-Windows/include/Bindings/CppDynamic/lib3mf_dynamic.hpp")]
        for module, variable, archive_name, stale_name in cases:
            with self.subTest(module=module), tempfile.TemporaryDirectory() as work:
                directory = Path(work)
                stale = directory / stale_name
                stale.parent.mkdir(parents=True)
                marker = directory / "executed.marker"
                payload = f'file(WRITE {cmake_quote(marker)} "untrusted")\n'
                stale.write_text(payload, encoding="utf-8")
                with zipfile.ZipFile(directory / archive_name, "w") as archive:
                    archive.writestr(stale_name, payload)
                result = self.run_cmake(directory,
                    f'set(WIN32 TRUE)\nset(CMAKE_BINARY_DIR {cmake_quote(directory / "build")})\n'
                    f'set({variable} {cmake_quote(directory)})\n'
                    f'include({cmake_quote(ROOT / "core/cmake" / module)})\n')
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("SHA-256 mismatch", result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse((directory / "build/authenticated-sdk").exists())

    def test_authenticated_archive_reconstructs_tree_and_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            archive_path = directory / "fixture.zip"
            destination = directory / "managed"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("include/sdk.h", "reviewed inert fixture\n")
            expected = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            script = (f'include({cmake_quote(ROOT / "core/cmake/AuthenticatedArchive.cmake")})\n'
                      f'chudware_authenticated_archive("https://invalid.invalid/never-download" '
                      f'{cmake_quote(archive_path)} "{expected}" {cmake_quote(destination)})\n')
            result = self.run_cmake(directory, script)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            header = destination / "include/sdk.h"
            self.assertEqual(header.read_text(), "reviewed inert fixture\n")
            header.write_text("tampered extracted tree")
            (destination / "extra.cmake").write_text("untrusted cache")
            result = self.run_cmake(directory, script)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(header.read_text(), "reviewed inert fixture\n")
            self.assertFalse((destination / "extra.cmake").exists())
            archive_path.write_bytes(b"tampered archive")
            result = self.run_cmake(directory, script)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertEqual(header.read_text(), "reviewed inert fixture\n")

    def test_reviewed_local_sdk_override_is_explicit_and_not_a_release_bypass(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            local_sdk = directory / "reviewed-local"
            config = local_sdk / "opencascade-7.9.3-vc14-64/cmake/OpenCASCADEConfig.cmake"
            config.parent.mkdir(parents=True)
            marker = directory / "local-source.marker"
            config.write_text(
                'set(OpenCASCADE_VERSION "7.9.3")\n'
                f'file(WRITE {cmake_quote(marker)} "reviewed local source")\n')
            script = (
                'unset(ENV{CI})\nunset(ENV{CHUDWARE_RELEASE_CHANNEL})\nset(WIN32 TRUE)\n'
                f'set(OCCT_LOCAL_SOURCE_DIR {cmake_quote(local_sdk)})\n'
                'set(CHUDWARE_DEVELOPER_BUILD FALSE)\n'
                f'include({cmake_quote(ROOT / "core/cmake/ProvisionOCCT.cmake")})\n')
            result = self.run_cmake(directory, script)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("development-only", result.stderr)
            self.assertFalse(marker.exists())
            result = self.run_cmake(directory, script.replace(
                "set(CHUDWARE_DEVELOPER_BUILD FALSE)", "set(CHUDWARE_DEVELOPER_BUILD TRUE)"))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(marker.read_text(), "reviewed local source")

    @unittest.skipUnless(shutil.which("git"), "Git is unavailable")
    def test_git_source_requires_exact_clean_commit_before_cmake(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            source = directory / "source"
            source.mkdir()
            marker = directory / "executed.marker"
            tracked = source / "CMakeLists.txt"
            tracked.write_text(f'file(WRITE {cmake_quote(marker)} "reviewed fixture")\n')
            def git(*args):
                return subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(source), *args],
                                      check=True, capture_output=True, text=True, timeout=20).stdout.strip()
            git("init")
            git("add", "CMakeLists.txt")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-m", "Disposable inert source")
            commit = git("rev-parse", "HEAD")
            materialized = directory / "authenticated"
            script = (f'include({cmake_quote(ROOT / "core/cmake/PinnedGitSource.cmake")})\n'
                      f'chudware_materialize_pinned_git({cmake_quote(source)} "{commit}" '
                      f'{cmake_quote(materialized)})\n'
                      f'include({cmake_quote(materialized / "CMakeLists.txt")})\n')
            result = self.run_cmake(directory, script)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(marker.read_text(), "reviewed fixture")
            marker.unlink()
            tracked.write_text(f'file(WRITE {cmake_quote(marker)} "untrusted source")\n')
            result = self.run_cmake(directory, script)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Modified managed Git source rejected", result.stderr)
            self.assertFalse(marker.exists())
            wrong_identity = script.replace(commit, "0" * 40)
            result = self.run_cmake(directory, wrong_identity)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unverified Git source identity", result.stderr)
            self.assertFalse(marker.exists())
            tracked.write_text(f'file(WRITE {cmake_quote(marker)} "replacement source")\n')
            git("add", "CMakeLists.txt")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-m", "Disposable replacement source")
            replacement = git("rev-parse", "HEAD")
            git("replace", commit, replacement)
            git("--no-replace-objects", "checkout", "--detach", commit)
            tracked.write_text(f'file(WRITE {cmake_quote(marker)} "replacement source")\n')
            result = self.run_cmake(directory, script)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())


class PosixCefTests(unittest.TestCase):
    def test_tampered_and_absent_cache_refuse_offline_before_extraction(self):
        import platform
        flavor = {("Linux", "x86_64"): "linux64", ("Darwin", "arm64"): "macosarm64",
                  ("Darwin", "x86_64"): "macosx64"}.get((platform.system(), platform.machine()))
        if not flavor or not shutil.which("bash"):
            self.skipTest("No pinned CEF identity for this platform")
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            cache = directory / "cache"
            destination = directory / "destination"
            cache.mkdir()
            archive = cache / f"cef_binary_149.0.6+g0d0eeb6+chromium-149.0.7827.201_{flavor}_minimal.tar.bz2"
            archive.write_bytes(b"tampered cache")
            command = ["bash", str(ROOT / "scripts/fetch_cef_posix.sh"), "--offline",
                       "--cache-root", str(cache), "--destination-root", str(destination)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertFalse(destination.exists())
            archive.unlink()
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("archive missing (offline)", result.stderr)
            self.assertFalse(destination.exists())


@unittest.skipUnless(shutil.which("bash"), "Bash is unavailable")
class RunnerPythonBootstrapTests(unittest.TestCase):
    def test_invalid_image_inputs_preserve_existing_venv_without_executing_it(self):
        import os
        import shlex
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is unavailable")
        consumers = [
            ("runner-provision.yml", "provision-macos",
             "Provision authenticated complete release Python", "chudware-ci-tools/release-python"),
            ("runner-provision.yml", "provision-linux",
             "Provision authenticated complete release Python", "chudware-ci-tools/release-python"),
            ("release-prepare.yml", "prepare",
             "Provision authenticated release Python (POSIX)", "temp/release-python"),
        ]
        for workflow, job, step_name, venv_path in consumers:
            doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
            body = next(step["run"] for step in doc["jobs"][job]["steps"]
                        if step.get("name") == step_name)
            for invalid in ("missing", "relative"):
                for variable in ("CHUDWARE_CI_BASE_PYTHON", "CHUDWARE_WHEELHOUSE"):
                    with self.subTest(workflow=workflow, job=job, invalid=invalid, variable=variable), tempfile.TemporaryDirectory() as work:
                        directory = Path(work)
                        marker = directory / "python-executed.marker"
                        base = directory / "image-python"
                        base.write_text(f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\nexit 99\n")
                        base.chmod(0o700)
                        stale = directory / venv_path / "bin/python"
                        stale.parent.mkdir(parents=True)
                        stale.write_bytes(b"unauthenticated existing venv; must remain untouched")
                        environment = dict(os.environ)
                        environment.update(HOME=str(directory), RUNNER_TEMP=str(directory / "temp"),
                                           RELEASE_VENV_SUBDIR="chudware-ci-tools/release-python",
                                           GITHUB_ENV=str(directory / "github-env"), GITHUB_PATH=str(directory / "github-path"),
                                           CHUDWARE_CI_BASE_PYTHON=str(base), CHUDWARE_WHEELHOUSE=str(directory / "wheelhouse"))
                        if invalid == "missing":
                            environment.pop(variable, None)
                        else:
                            environment[variable] = "image-python" if variable == "CHUDWARE_CI_BASE_PYTHON" else "wheelhouse"
                        result = subprocess.run(["bash", "-c", body], cwd=ROOT, env=environment,
                                                capture_output=True, text=True, timeout=30)
                        self.assertNotEqual(result.returncode, 0)
                        if invalid == "missing":
                            self.assertIn(variable, result.stderr)
                        else:
                            message = "base Python must be an absolute" if variable == "CHUDWARE_CI_BASE_PYTHON" else "wheelhouse must be an absolute"
                            self.assertIn(message, result.stdout + result.stderr)
                        self.assertFalse(marker.exists())
                        self.assertEqual(stale.read_bytes(), b"unauthenticated existing venv; must remain untouched")


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
class PowerShellBootstrapTests(unittest.TestCase):
    def test_cef_rejects_tampered_cache_without_touching_extracted_sdk(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            (directory / "cef_binary_149.0.6_windows64_minimal.tar.bz2").write_bytes(b"tampered")
            dll = directory / "cef_binary_149.0.6+g0d0eeb6+chromium-149.0.7827.201_windows64_minimal/Release/libcef.dll"
            dll.parent.mkdir(parents=True)
            dll.write_bytes(b"do not load")
            result = subprocess.run([POWERSHELL, "-NoProfile", "-File",
                                     str(ROOT / "scripts/fetch_cef_windows.ps1"),
                                     "-DestinationRoot", str(directory), "-Offline"],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertEqual(dll.read_bytes(), b"do not load")

    def test_workflow_bootstraps_reject_cached_bytes_before_execution(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is unavailable")
        cases = [
            ("chudcode-runtime-windows-build.yml", "build",
             "Authenticate and install pinned Bun 1.4.0", "bun-windows-x64.zip", "temp"),
            ("chudcode-runtime-windows-build.yml", "build",
             "Authenticate rustup and install locked Rust toolchain", "rustup-init-1.28.2.exe", "temp"),
            ("runner-provision.yml", "provision-windows",
             "Unpack Inno Setup 6 (no admin needed)",
             "chudware-ci-tools/bootstrap-archives/innosetup-6.7.3.exe", "profile"),
        ]
        for workflow, job, name, cache, home in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as work:
                directory = Path(work)
                doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
                body = next(step["run"] for step in doc["jobs"][job]["steps"] if step.get("name") == name)
                target = directory / home / cache
                target.parent.mkdir(parents=True)
                target.write_bytes(b"tampered bootstrap, never execute")
                marker = directory / "extracted.marker"
                script = directory / "bootstrap.ps1"
                script.write_text(
                    "$env:RUNNER_TEMP = '" + str(directory / "temp").replace("'", "''") + "'\n"
                    "$env:USERPROFILE = '" + str(directory / "profile").replace("'", "''") + "'\n"
                    "$env:RUST_TOOLCHAIN = 'nightly-2026-01-01'\n"
                    "function Invoke-WebRequest { throw 'Network forbidden in offline smoke' }\n"
                    "function Expand-Archive { Set-Content '" + str(marker).replace("'", "''") + "' 'unsafe'; throw 'Unexpected extraction' }\n"
                    + body, encoding="utf-8")
                result = subprocess.run([POWERSHELL, "-NoProfile", "-File", str(script)],
                                        capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SHA-256 mismatch", result.stderr)
                self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
