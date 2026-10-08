#!/usr/bin/env python3
"""Runner policy must inspect resolved YAML aliases without document leakage."""

import importlib.util
from pathlib import Path
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
    runs-on: %s
    timeout-minutes: 10
    steps:
      - run: echo policy-fixture
""" % (branch_anchor, selector)

    def test_aliased_hosted_runner_is_rejected(self):
        self.assertEqual(
            self.check(self.workflow("      - ubuntu-latest", "*labels")),
            {"hosted-runner"},
        )

    def test_aliased_private_runner_is_accepted(self):
        self.assertEqual(
            self.check(self.workflow(
                "      - self-hosted\n      - Linux\n      - X64\n      - cc-linux-build",
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


if __name__ == "__main__":
    unittest.main()
