"""Environment-driven gateway configuration (CHUDWARE_WEB_* and CHUDWARE_* names)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _default_home() -> str:
    data = os.environ.get("XDG_DATA_HOME", "").strip() or "~/.local/share"
    return str(Path(data) / "chudware-web")


@dataclass
class Settings:
    """Runtime settings, loaded once from the process environment."""

    # The single local operator every request acts as. It names the operator's
    # workspace and cell; it is not a credential.
    operator: str = field(default_factory=lambda: _env("CHUDWARE_WEB_OPERATOR", "operator"))
    # Host header values the gateway answers to. Anything else is refused so a
    # DNS-rebinding page cannot reach a loopback listener under its own name.
    allowed_hosts: list[str] = field(default_factory=lambda: [
        host.strip() for host in _env("CHUDWARE_WEB_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
        if host.strip()
    ])
    # Install root shared with web/deploy/install_cells.sh: cell sockets and state
    # live in <home>/cells, the cell payload in <home>/current.
    home: Path = field(default_factory=lambda: _path(_env("CHUDWARE_WEB_HOME", _default_home())))
    # The operator's CAD files, mounted as /work inside the engine cell.
    # Defaults to <home>/workspace.
    workspace_root: Path = field(default_factory=lambda: Path(_env("CHUDWARE_WORKSPACE_ROOT")))
    # Staged WASM client served at /app (./build.sh wasm writes it here).
    app_dir: Path = field(default_factory=lambda: _path(
        _env("CHUDWARE_WEB_APP", str(REPO_ROOT / "host" / "build-wasm" / "app"))))
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
    )
    # Per-frame cap on the /engine bridge. hostd accepts far larger lines, but
    # forwarding anything near that would exhaust the gateway's memory.
    ws_max_message_bytes: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_WS_MAX_MESSAGE_BYTES", 4 * 1024 * 1024)
    )
    # Concurrent /engine websockets. The client opens one and reconnects on drop;
    # more than one covers a reconnect overlapping its predecessor and extra tabs.
    max_engine_bridges: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_MAX_ENGINE_BRIDGES", 4)
    )

    def __post_init__(self) -> None:
        if not OPERATOR_RE.fullmatch(self.operator):
            raise ValueError("CHUDWARE_WEB_OPERATOR must be 1-64 characters of A-Z a-z 0-9 _ -")
        if not self.allowed_hosts:
            raise ValueError("CHUDWARE_WEB_ALLOWED_HOSTS must name at least one host")
        if self.max_engine_bridges < 1:
            raise ValueError("CHUDWARE_WEB_MAX_ENGINE_BRIDGES must be at least 1")
        if str(self.workspace_root) in {"", "."}:
            self.workspace_root = self.home / "workspace"
        else:
            self.workspace_root = _path(str(self.workspace_root))

    @property
    def cells_root(self) -> Path:
        return self.home / "cells"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()


settings = load_settings()
