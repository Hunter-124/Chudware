"""Front-door tests: no podman, no network, no real OpenRouter.

Tests run only in a container or on CI (scripts/testbox_guard.py), like the rest of web/.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Import the front door only as the ``demo`` package, like the gateway tests do for
# ``gateway``, so every test module shares one module object per file.
WEB_DIR = Path(__file__).resolve().parents[2]
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))


def pytest_configure(config):
    spec = importlib.util.spec_from_file_location(
        "_chudware_testbox_guard", WEB_DIR.parent / "scripts" / "testbox_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    if not guard.isolated():
        pytest.exit(guard.MESSAGE, returncode=3)
