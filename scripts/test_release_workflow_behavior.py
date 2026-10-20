#!/usr/bin/env python3
"""Execute release workflow failure paths using inert disposable process fixtures."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_workflow_policy", ROOT / "scripts/verify-ci-workflows.py")
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


def step_script(job, name):
    document = policy.parse_workflow((ROOT / ".github/workflows/nightly-release.yml").read_text(encoding="utf-8"))
    return next(step["run"] for step in document["jobs"][job]["steps"] if step.get("name") == name)


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"), "POSIX workflow interpreters unavailable")
class ReleaseFailureBehaviorTests(unittest.TestCase):
    def execute(self, job, step, failure, retained_artifact=False, native_inputs=False):
        with tempfile.TemporaryDirectory(prefix="chudware-release-failure-") as directory:
            temporary = Path(directory)
            fixture = temporary / "fixture-python"
            fixture.write_text(
                "#!" + sys.executable + "\n"
                "import os, pathlib, sys\n"
                "name = pathlib.Path(sys.argv[1]).name\n"
                "if name == 'finalize_release.py':\n"
                "    output = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
                "    output.mkdir(parents=True, exist_ok=True)\n"
                "    if os.environ['RETAIN_ARTIFACT'] == '1':\n"
                "        (output / 'Chudware-1.2.3-beta.1-linux-x86_64.AppImage').write_bytes(b'inert retained output')\n"
                "    print('inert native finalizer failure')\n"
                "    sys.exit(int(os.environ['FIXTURE_EXIT']))\n"
                "if name == 'generate_release_evidence.py':\n"
                "    print('inert evidence validation failure')\n"
                "    sys.exit(int(os.environ['FIXTURE_EXIT']))\n"
                "if name == 'ci_sign.py':\n"
                "    pathlib.Path(os.environ['SIGN_MARKER']).write_text('signature attempted')\n"
                "    sys.exit(0)\n"
                "raise SystemExit('unexpected fixture process invocation: ' + name)\n",
                encoding="utf-8",
            )
            fixture.chmod(0o700)
            artifact = temporary / "inert.zip"
            artifact.write_bytes(b"inert unsigned fixture")
            environment = {
                "PATH": os.environ.get("PATH", os.defpath), "HOME": str(temporary),
                "RUNNER_TEMP": str(temporary), "GITHUB_ENV": str(temporary / "github-env"),
                "GITHUB_STEP_SUMMARY": str(temporary / "summary"),
                "CHUDWARE_RELEASE_PYTHON": str(fixture), "RELEASE_PYTHON": str(fixture),
                "CHUDWARE_RELEASE_CI_PUBLIC_KEY": "11" * 32,
                "CHUDWARE_RELEASE_KEY_ID": "release-ci-v1",
                "RELEASE_VERSION": "1.2.3-beta.1", "RELEASE_STABLE_VERSION": "1.2.3",
                "RELEASE_COMMIT": "a" * 40, "RELEASE_PLATFORM": "linux" if job == "linux-beta" else "macos",
                "RELEASE_ARCH": "x86_64", "ARTIFACT": str(artifact), "ARTIFACT_SHA256": "b" * 64,
                "FIXTURE_EXIT": str(failure), "RETAIN_ARTIFACT": "1" if retained_artifact else "0",
                "SIGN_MARKER": str(temporary / "sign-marker"),
            }
            if native_inputs:
                environment.update({
                    "CHUDWARE_PLATFORM_PUBLISHER_POLICY": str(temporary / "fixture-policy.json"),
                    "CHUDWARE_PLATFORM_PUBLISHER_POLICY_SIGNATURE": str(temporary / "fixture-policy.sig"),
                    "CHUDWARE_SIGN_IDENTITY": "inert fixture selector",
                    "CHUDWARE_NOTARIZE_PROFILE": "inert fixture profile",
                })
            result = subprocess.run(
                [shutil.which("bash"), "-c", step_script(job, step)], cwd=ROOT,
                env=environment, text=True, capture_output=True, timeout=30, check=False,
            )
            return result, (temporary / "sign-marker").exists(), (
                (temporary / "github-env").read_text(encoding="utf-8")
                if (temporary / "github-env").exists() else ""
            )

    def test_failed_native_finalizer_cannot_salvage_retained_appimage(self):
        result, signed, assignments = self.execute(
            "linux-beta", "Finalize the AppImage with complete retained test evidence", 7, True,
        )
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertFalse(signed)
        self.assertNotIn("ARTIFACT=", assignments)
        self.assertIn("retained output is not signable", result.stdout)

    def test_success_exit_with_missing_evidence_cannot_release_appimage(self):
        result, signed, assignments = self.execute(
            "linux-beta", "Finalize the AppImage with complete retained test evidence", 0, True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(signed)
        self.assertNotIn("ARTIFACT=", assignments)

    def test_mac_evidence_failure_stops_before_artifact_identity_signing(self):
        result, signed, _ = self.execute(
            "macos-beta", "Sign the verified app artifact pair and complete evidence", 9,
        )
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertFalse(signed)

    def test_attempted_mac_native_attestation_failure_cannot_downgrade_to_negative(self):
        result, signed, _ = self.execute(
            "macos-beta", "Sign the verified app artifact pair and complete evidence", 6,
            native_inputs=True,
        )
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        self.assertFalse(signed)


if __name__ == "__main__":
    unittest.main()
