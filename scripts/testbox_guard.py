#!/usr/bin/env python3
"""Refuse to run Chudware tests directly on a developer host.

Tests share the host's daemon socket, build trees, loopback ports, data roots and CPU
with the developer's live Chudware session, so they run only inside an isolated
container (scripts/testbox.py, locally or offloaded over ssh) or on a CI runner.
pytest (root conftest.py), ctest (CTestCustom.cmake from cmake/ChudwareTestSuites.cmake),
scripts/test_lite.py and scripts/gate.sh all call this check.

    python3 scripts/testbox_guard.py    exit 0 when isolated, else print why and exit 3
"""
from __future__ import annotations

import os
import sys

MESSAGE = """\
Chudware tests do not run on the developer host: they collide with the live app's
daemon, build trees, ports and CPU. Run them isolated instead:

    scripts/testbox.py --suite <name>    one suite (scripts/test_lite.py --list-suites)
    scripts/testbox.py                   change-selected tests
    scripts/testbox.py --gate            the full gate

testbox runs in a podman container on a private snapshot of the working tree and
offloads to the first reachable host in ~/.config/chudware/testbox.json (else local
podman). See CONTRIBUTING.md "Running tests"."""


def isolated() -> bool:
    """True inside a podman/docker container or on a GitHub Actions runner.

    Not `CI`: agent shells export CI=true on developer hosts to silence prompts.
    """
    if os.path.exists('/run/.containerenv') or os.path.exists('/.dockerenv'):
        return True
    return os.environ.get('GITHUB_ACTIONS') == 'true'


def main() -> int:
    if isolated():
        return 0
    print(MESSAGE, file=sys.stderr)
    return 3


if __name__ == '__main__':
    sys.exit(main())
