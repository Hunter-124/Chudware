"""Shared fixtures: a disposable gateway home, workspace and WASM bundle.

The gateway reads its configuration once at import, so the environment is set
here before any test module imports it. Each test then points the shared
settings object at its own temporary tree.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Import the gateway only as the ``gateway`` package so every test shares one
# module object per file.
WEB_DIR = Path(__file__).resolve().parents[2]
if str(WEB_DIR) not in sys.path:
    sys.path.insert(0, str(WEB_DIR))

_IMPORT_HOME = tempfile.mkdtemp(prefix="cw-gw-import-")
os.environ["CHUDWARE_WEB_HOME"] = _IMPORT_HOME
os.environ["CHUDWARE_WEB_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ.pop("CHUDWARE_WORKSPACE_ROOT", None)
os.environ.pop("CHUDWARE_WEB_APP", None)
os.environ.pop("CHUDWARE_WEB_OPERATOR", None)


@pytest.fixture
def gateway_home(monkeypatch) -> Iterator[Path]:
    """A short private home (AF_UNIX socket paths have a ~107 byte limit)."""
    from gateway.config import settings

    with tempfile.TemporaryDirectory(prefix="cw-gw-", dir="/tmp") as root:
        home = Path(root)
        monkeypatch.setattr(settings, "home", home)
        monkeypatch.setattr(settings, "workspace_root", home / "workspace")
        settings.ensure_dirs()
        yield home


@pytest.fixture
def workspace(gateway_home) -> Path:
    return gateway_home / "workspace"


@pytest.fixture
def app_tree(tmp_path, monkeypatch) -> Path:
    """Point settings.app_dir at a tiny fake WASM client tree."""
    from gateway.config import settings

    root = tmp_path / "app"
    root.mkdir()
    (root / "index.html").write_text(
        '<!doctype html><title>chudware</title><script src="Chudware.js"></script>\n',
        encoding="utf-8",
    )
    (root / "Chudware.js").write_text("/* wasm glue */\n", encoding="utf-8")
    (root / "chudware.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (root / "chudware.data").write_bytes(b"PACK")
    monkeypatch.setattr(settings, "app_dir", root)
    return root


@pytest.fixture
def client(gateway_home):
    from fastapi.testclient import TestClient

    from gateway.app import app

    with TestClient(app) as test_client:
        yield test_client
