#!/usr/bin/env python3
"""Offline consumer-visible runner routing and YAML alias policy regressions."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


_SPEC = importlib.util.spec_from_file_location(
    "chudware_ci_workflow_policy", Path(__file__).with_name("verify-ci-workflows.py")
)
policy = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = policy
_SPEC.loader.exec_module(policy)


class AliasRunnerPolicyTests(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory(prefix="chudware-ci-alias-") as directory:
            path = Path(directory) / "alias-policy.yml"
            path.write_text(text, encoding="utf-8")
            return {finding.rule for finding in policy.check_workflow(path)}

    def workflow(self, branch_anchor, selector):
        return """name: alias-policy
on:
  push:
    branches: &labels
%s
permissions:
  contents: read
concurrency:
  group: alias-policy
  cancel-in-progress: false
jobs:
  check:
    runs-on:
      group: chudware-untrusted-ephemeral
      labels: %s
    timeout-minutes: 10
    steps:
      - run: echo policy-fixture
        shell: powershell
""" % (branch_anchor, selector)

    def test_aliased_hosted_runner_is_rejected(self):
        self.assertEqual(
            self.check(self.workflow("      - ubuntu-latest", "*labels")),
            {"hosted-runner"},
        )

    def test_aliased_private_runner_is_accepted(self):
        self.assertEqual(
            self.check(self.workflow(
                "      - self-hosted\n      - Linux\n      - X64\n      - cc-linux-disposable",
                "*labels",
            )),
            set(),
        )

    def test_aliases_do_not_leak_between_workflow_documents(self):
        self.check(self.workflow("      - ubuntu-latest", "*labels"))
        unbound = self.workflow("      - main", "*labels").replace(
            "branches: &labels", "branches:"
        )
        self.assertEqual(self.check(unbound), {"workflow-parse-error"})


class RunnerBoundaryCliTests(unittest.TestCase):
    """Exercise fixture repositories through the same CLI used in CI."""

    def workflow(self, group=policy.UNTRUSTED_RUNNER_GROUP, guard=None,
                 environment=None, trigger="pull_request", steps=None,
                 extra_job="", global_env="", permissions="contents: read"):
        protected = ""
        if guard is not None:
            protected += "    if: " + guard + "\n"
        if environment is not None:
            protected += "    environment: " + environment + "\n"
        capacity = "cc-linux-build" if group == policy.TRUSTED_RUNNER_GROUP else "cc-linux-disposable"
        return (
            "name: boundary-fixture\non:\n  " + trigger + ":\n"
            "permissions:\n  " + permissions + "\n"
            "concurrency:\n  group: fixture\n  cancel-in-progress: false\n"
            + global_env + "jobs:\n  probe:\n"
            + protected + "    runs-on:\n      group: " + group + "\n"
            + "      labels: [self-hosted, Linux, X64, " + capacity + "]\n"
            "    timeout-minutes: 5\n" + extra_job + "    steps:\n"
            + (steps or "      - run: echo fixture\n")
        )

    def invoke(self, files):
        with tempfile.TemporaryDirectory(prefix="chudware-routing-fixture-") as directory:
            root = Path(directory)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            for name, text in files.items():
                (workflows / name).write_text(text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(Path(policy.__file__).resolve()), "--root", str(root),
                 "--scope-all", "--skip-required-set", "--no-annotations"],
                capture_output=True, text=True, timeout=30, check=False,
            )

    def assert_denied(self, text, rule):
        result = self.invoke({"fixture.yml": text})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[" + rule + "]", result.stdout)

    def trusted(self, **kwargs):
        return self.workflow(
            group=policy.TRUSTED_RUNNER_GROUP, guard="${{ " + policy.TRUSTED_JOB_GUARD + " }}",
            environment="chudware-release-protected", trigger="workflow_dispatch", **kwargs,
        )

    def test_disposable_pr_and_branch_jobs_are_accepted(self):
        for trigger in ("pull_request", "push", "workflow_dispatch"):
            with self.subTest(trigger=trigger):
                result = self.invoke({"fixture.yml": self.workflow(trigger=trigger)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_guarded_protected_release_is_accepted(self):
        result = self.invoke({"fixture.yml": self.trusted()})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_label_only_route_is_rejected(self):
        text = self.workflow().replace(
            "    runs-on:\n      group: chudware-untrusted-ephemeral\n"
            "      labels: [self-hosted, Linux, X64, cc-linux-disposable]",
            "    runs-on: [self-hosted, Linux, X64, cc-linux-build]",
        )
        self.assert_denied(text, "runner-trust-boundary")

    def test_computed_or_unknown_group_is_rejected(self):
        for group in ("${{ inputs.group }}", "default", "chudware-release-protected"):
            with self.subTest(group=group):
                self.assert_denied(self.workflow(group=group), "runner-trust-boundary")

    def test_pr_cannot_target_protected_group_even_with_guard(self):
        for event in ("pull_request", "pull_request_target", "workflow_run"):
            with self.subTest(event=event):
                self.assert_denied(self.trusted().replace(
                    "  workflow_dispatch:", "  " + event + ":"
                ), "protected-job-guard")

    def test_missing_guard_and_boolean_bypass_are_rejected(self):
        for guard in (None, "${{ github.ref_protected }}",
                      "${{ " + policy.TRUSTED_JOB_GUARD + " || true }}",
                      "${{ (" + policy.TRUSTED_JOB_GUARD + ") && (true) || true }}"):
            with self.subTest(guard=guard):
                self.assert_denied(self.workflow(
                    group=policy.TRUSTED_RUNNER_GROUP, guard=guard,
                    environment="chudware-release-protected", trigger="push",
                ), "protected-job-guard")

    def test_additional_fully_enclosed_release_condition_is_accepted(self):
        text = self.trusted().replace(
            "${{ " + policy.TRUSTED_JOB_GUARD + " }}",
            "${{ (" + policy.TRUSTED_JOB_GUARD + ") && (needs.plan.outputs.skip != 'true') }}",
        )
        result = self.invoke({"fixture.yml": text})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_protected_environment_is_required(self):
        self.assert_denied(self.workflow(
            group=policy.TRUSTED_RUNNER_GROUP, guard=policy.TRUSTED_JOB_GUARD,
            trigger="push",
        ), "protected-job-environment")

    def test_pr_secret_delivery_is_rejected(self):
        for extra in ("    env:\n      KEY: ${{ secrets.CHUDWARE_RELEASE_CI_KEY }}\n",
                      '    env:\n      KEY: ${{ secrets["CHUDWARE_RELEASE_CI_KEY"] }}\n'):
            with self.subTest(extra=extra):
                self.assert_denied(self.workflow(extra_job=extra), "untrusted-secret-delivery")
        self.assert_denied(self.workflow(environment="chudware-release-protected"),
                           "untrusted-secret-delivery")
        self.assert_denied(self.workflow(
            global_env="env:\n  KEY: ${{ secrets.CHUDWARE_RELEASE_CI_KEY }}\n"
        ), "untrusted-secret-delivery")

    def test_pr_write_capable_token_is_rejected(self):
        self.assert_denied(self.workflow(permissions="contents: write"),
                           "untrusted-write-permission")

    def test_protected_jobs_cannot_consume_pr_artifacts_or_caches(self):
        for action in ("actions/download-artifact", "actions/cache", "actions/cache/restore"):
            with self.subTest(action=action):
                self.assert_denied(self.trusted(
                    steps="      - uses: " + action + "@" + "a" * 40 + "\n"
                ), "protected-untrusted-input")

    def test_protected_checkout_cannot_select_pr_ref(self):
        self.assert_denied(self.trusted(
            steps="      - uses: actions/checkout@" + "a" * 40 + "\n"
                  "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
        ), "protected-untrusted-input")

    def reusable_fixture(self, secrets="", guard=True, callee=None):
        condition = "    if: ${{ " + policy.TRUSTED_JOB_GUARD + " }}\n" if guard else ""
        caller = (
            "on:\n  workflow_dispatch:\npermissions:\n  contents: read\n"
            "concurrency:\n  group: caller\n  cancel-in-progress: false\n"
            "jobs:\n  call:\n" + condition + "    uses: ./.github/workflows/callee.yml\n"
            + secrets
        )
        callee = callee or self.trusted().replace(
            "  workflow_dispatch:", "  workflow_call:\n    secrets:\n      SANDBOX:\n        required: false"
        )
        return {"caller.yml": caller, "callee.yml": callee}

    def test_named_guarded_reusable_secret_delivery_is_accepted(self):
        result = self.invoke(self.reusable_fixture(
            secrets="    secrets:\n      SANDBOX: ${{ secrets.SANDBOX }}\n"
        ))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reusable_inherited_or_unguarded_secrets_are_rejected(self):
        for secrets, guard in (("    secrets: inherit\n", True),
                               ("    secrets:\n      SANDBOX: ${{ secrets.SANDBOX }}\n", False),
                               ("    secrets:\n      SIGNING: ${{ secrets.SIGNING }}\n", True)):
            with self.subTest(secrets=secrets, guard=guard):
                result = self.invoke(self.reusable_fixture(secrets=secrets, guard=guard))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("[reusable-secret-boundary]", result.stdout)

    def test_reusable_call_does_not_authorize_unsafe_callee(self):
        callee = self.workflow().replace(
            "    runs-on:\n      group: chudware-untrusted-ephemeral\n"
            "      labels: [self-hosted, Linux, X64, cc-linux-disposable]",
            "    runs-on: [self-hosted, Linux, X64, cc-linux-build]",
        ).replace("  pull_request:", "  workflow_call:")
        result = self.invoke(self.reusable_fixture(callee=callee))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[runner-trust-boundary]", result.stdout)

    def shell_workflow(self, shell="bash", condition=None, defaults="", matrix=None):
        steps = "      - run: echo fixture\n"
        if shell is not None:
            steps += "        shell: " + shell + "\n"
        if condition is not None:
            steps += "        if: " + condition + "\n"
        text = self.workflow(steps=steps, extra_job=defaults)
        if matrix is None:
            return text.replace(
                "[self-hosted, Linux, X64, cc-linux-disposable]",
                "[self-hosted, Windows, X64, cc-win-disposable]",
            )
        return text.replace(
            "[self-hosted, Linux, X64, cc-linux-disposable]",
            '[self-hosted, "${{ matrix.runner_os }}", X64, cc-fixture]',
        ).replace("    timeout-minutes: 5\n", "    timeout-minutes: 5\n" + matrix)

    def assert_shell_accepted(self, text):
        result = self.invoke({"fixture.yml": text})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_windows_bash_pwsh_and_implicit_pwsh_are_rejected(self):
        for shell in ("bash", "pwsh", "sh", None):
            with self.subTest(shell=shell):
                self.assert_denied(self.shell_workflow(shell=shell),
                                   "incompatible-windows-shell")

    def test_windows_powershell_direct_and_defaults_are_accepted(self):
        self.assert_shell_accepted(self.shell_workflow(shell="powershell"))
        defaults = "    defaults:\n      run:\n        shell: powershell\n"
        self.assert_shell_accepted(self.shell_workflow(shell=None, defaults=defaults))
        workflow_default = self.shell_workflow(shell=None).replace(
            "jobs:\n", "defaults:\n  run:\n    shell: powershell\njobs:\n"
        )
        self.assert_shell_accepted(workflow_default)
        self.assert_denied(self.shell_workflow(shell="bash", defaults=defaults),
                           "incompatible-windows-shell")
        self.assert_denied(self.shell_workflow(
            shell=None, defaults=defaults.replace("powershell", "pwsh")
        ), "incompatible-windows-shell")

    def test_literal_windows_positive_and_negative_os_guards(self):
        for condition in ("runner.os == 'Windows'", "${{ runner.os == 'Windows' }}",
                          "runner.os != 'Linux'",
                          "runner.os != 'Windows' || true",
                          "runner.os != 'Windows' || inputs.force",
                          "!(runner.os == 'Linux')"):
            with self.subTest(condition=condition):
                self.assert_denied(self.shell_workflow(condition=condition),
                                   "incompatible-windows-shell")
        for condition in ("runner.os != 'Windows'", "${{ runner.os != 'Windows' }}",
                          "runner.os == 'macOS'", "!(runner.os == 'Windows')",
                          "always() && (runner.os != 'Windows' || false)",
                          "runner.os != 'Windows' && inputs.force"):
            with self.subTest(condition=condition):
                self.assert_shell_accepted(self.shell_workflow(condition=condition))
        self.assert_shell_accepted(self.shell_workflow(
            shell="powershell", condition="runner.os == 'Windows'"
        ))

    def test_mixed_static_matrix_rejects_unguarded_shells_and_preserves_split(self):
        for matrix in (
            "    strategy:\n      matrix:\n        runner_os: [Linux, Windows, macOS]\n",
            "    strategy:\n      matrix:\n        include:\n"
            "          - runner_os: Linux\n          - runner_os: Windows\n",
        ):
            with self.subTest(matrix=matrix):
                for shell in ("bash", "pwsh"):
                    self.assert_denied(self.shell_workflow(shell=shell, matrix=matrix),
                                       "incompatible-windows-shell")
                split = self.shell_workflow(
                    matrix=matrix, condition="runner.os != 'Windows'"
                ) + "      - run: Write-Output fixture\n" \
                    "        shell: powershell\n        if: runner.os == 'Windows'\n"
                self.assert_shell_accepted(split)

    def test_include_row_only_guards_preserve_native_build_idioms(self):
        matrix = (
            "    strategy:\n      matrix:\n        include:\n"
            "          - runner_os: Linux\n            arm_sdk: arm64\n"
            "            test_mode: qemu\n            preset: linux-core\n"
            "          - runner_os: Windows\n            arm_sdk: ''\n"
            "            test_mode: native\n            preset: windows-occt\n"
        )
        for condition in ("matrix.arm_sdk != ''", "matrix.test_mode == 'qemu'",
                          "matrix.preset == 'linux-core'"):
            with self.subTest(condition=condition):
                self.assert_shell_accepted(self.shell_workflow(
                    matrix=matrix, condition=condition
                ))
        self.assert_denied(self.shell_workflow(
            matrix=matrix, condition="matrix.test_mode == 'native'"
        ), "incompatible-windows-shell")

    def test_dynamic_or_absent_matrix_os_is_windows_possible(self):
        for matrix in (
            "    strategy:\n      matrix: ${{ fromJSON(needs.inventory.outputs.matrix) }}\n",
            "    strategy:\n      matrix:\n        runner_arch: [X64, ARM64]\n",
        ):
            with self.subTest(matrix=matrix):
                self.assert_denied(self.shell_workflow(matrix=matrix),
                                   "incompatible-windows-shell")
                self.assert_shell_accepted(self.shell_workflow(
                    matrix=matrix, condition="runner.os != 'Windows'"
                ))
                self.assert_shell_accepted(self.shell_workflow(
                    matrix=matrix, condition="runner.os == 'macOS'"
                ))

    def test_aliased_job_defaults_resolve_before_shell_dispatch(self):
        text = self.shell_workflow(shell=None).replace(
            "jobs:\n", "defaults: &run_defaults\n  run:\n    shell: powershell\njobs:\n"
        ).replace("    steps:\n", "    defaults: *run_defaults\n    steps:\n")
        self.assert_shell_accepted(text)
        self.assert_denied(text.replace("shell: powershell", "shell: pwsh"),
                           "incompatible-windows-shell")

    def test_matrix_label_arrays_resolve_windows_candidates(self):
        matrix = (
            "    strategy:\n      matrix:\n        runner:\n"
            "          - [self-hosted, Linux, X64, cc-linux-disposable]\n"
            "          - [self-hosted, Windows, X64, cc-win-disposable]\n"
        )
        text = self.shell_workflow(matrix=matrix).replace(
            '[self-hosted, "${{ matrix.runner_os }}", X64, cc-fixture]',
            "${{ matrix.runner }}",
        )
        self.assert_denied(text, "incompatible-windows-shell")
        self.assert_shell_accepted(text.replace(
            "        shell: bash\n", "        shell: bash\n        if: runner.os != 'Windows'\n"
        ))

    def test_mixed_release_prepare_bash_is_rejected_by_consumer_cli(self):
        matrix = (
            "    strategy:\n      matrix:\n        include:\n"
            "          - runner_os: Linux\n          - runner_os: Windows\n"
        )
        result = self.invoke({"release-prepare.yml": self.shell_workflow(matrix=matrix)})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[incompatible-windows-shell]", result.stdout)

    def test_alias_expanded_steps_are_checked_in_each_job_context(self):
        for shell in ("bash", "pwsh", "powershell"):
            with self.subTest(shell=shell):
                text = self.workflow(
                    steps="      - run: echo fixture\n        shell: " + shell + "\n"
                ).replace("    steps:\n", "    steps: &shared_steps\n")
                text += (
                    "  windows:\n    runs-on:\n"
                    "      group: chudware-untrusted-ephemeral\n"
                    "      labels: [self-hosted, Windows, X64, cc-win-disposable]\n"
                    "    timeout-minutes: 5\n    steps: *shared_steps\n"
                )
                if shell == "powershell":
                    self.assert_shell_accepted(text)
                else:
                    self.assert_denied(text, "incompatible-windows-shell")
        guarded = text.replace("shell: powershell", "shell: bash").replace(
            "        shell: bash\n", "        shell: bash\n        if: runner.os != 'Windows'\n"
        )
        self.assert_shell_accepted(guarded)


if __name__ == "__main__":
    unittest.main()
