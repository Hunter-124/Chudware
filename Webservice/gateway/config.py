"""Env-driven gateway configuration (BILLET_WEB_* prefix)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _expand_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()

def _normalize_team_domain(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw

def _load_webd_env() -> dict[str, str]:
    """KEY=VALUE defaults written by deploy/install_webd.sh (~/billet-web/webd.env)."""
    path = Path(_env("BILLET_WEBD_ENV", "~/billet-web/webd.env") or "").expanduser()
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@dataclass
class Settings:
    """Runtime settings loaded once from the process environment."""

    data_root: Path = field(
        default_factory=lambda: _expand_path(
            _env("BILLET_WEB_DATA_ROOT", "~/billet-web-data") or "~/billet-web-data"
        )
    )
    host: str = field(default_factory=lambda: _env("BILLET_WEB_HOST", "127.0.0.1") or "127.0.0.1")
    port: int = field(default_factory=lambda: _env_int("BILLET_WEB_PORT", 8300))
    max_sessions: int = field(default_factory=lambda: _env_int("BILLET_WEB_MAX_SESSIONS", 3))
    max_heavy: int = field(default_factory=lambda: _env_int("BILLET_WEB_MAX_HEAVY", 2))
    session_memory: str = field(
        default_factory=lambda: _env("BILLET_WEB_SESSION_MEMORY", "5g") or "5g"
    )
    session_cpus: float = field(default_factory=lambda: _env_float("BILLET_WEB_SESSION_CPUS", 2.0))
    session_pids_limit: int = field(
        default_factory=lambda: _env_int("BILLET_WEB_SESSION_PIDS", 2048)
    )
    idle_timeout_sec: int = field(
        default_factory=lambda: _env_int("BILLET_WEB_IDLE_TIMEOUT_SEC", 30 * 60)
    )
    min_host_ram_gib: float = field(
        default_factory=lambda: _env_float("BILLET_WEB_MIN_HOST_RAM_GIB", 8.0)
    )
    stream_port_base: int = field(
        default_factory=lambda: _env_int("BILLET_WEB_STREAM_PORT_BASE", 8400)
    )
    session_image: str = field(
        default_factory=lambda: _env("BILLET_WEB_SESSION_IMAGE", "localhost/billet-session:latest")
        or "localhost/billet-session:latest"
    )
    stream_backend: str = field(
        # novnc is the VERIFIED stream path on the Fedora image; selkies pip is
        # broken there (evdev/kernel-headers) — opt in explicitly if that changes.
        default_factory=lambda: _env("BILLET_WEB_STREAM_BACKEND", "novnc") or "novnc"
    )
    cf_team_domain: str = field(
        # Accept "team.cloudflareaccess.com" or "https://team.cloudflareaccess.com";
        # the Access JWT `iss` claim and the JWKS URL both need the https:// form.
        default_factory=lambda: _normalize_team_domain(
            _env("BILLET_WEB_CF_TEAM_DOMAIN", "") or ""
        )
    )
    cf_aud: str = field(default_factory=lambda: _env("BILLET_WEB_CF_AUD", "") or "")
    dev_mode: bool = field(default_factory=lambda: _env_bool("BILLET_WEB_DEV", False))
    users_file: Path = field(default_factory=Path)
    healthcheck_timeout_sec: float = field(
        default_factory=lambda: _env_float("BILLET_WEB_HEALTHCHECK_TIMEOUT_SEC", 90.0)
    )
    healthcheck_interval_sec: float = field(
        default_factory=lambda: _env_float("BILLET_WEB_HEALTHCHECK_INTERVAL_SEC", 1.5)
    )
    # Web-native client: shared engine daemon socket + team workspace root.
    # Env overrides win; else webd.env (install_webd.sh); else deterministic
    # defaults (instance key "billetweb" is verbatim => socket name is stable).
    webd_socket: Path = field(default_factory=Path)
    workspace_root: Path = field(default_factory=Path)
    # WASM web client static root (served at /app) + client perf traces.
    app_dir: Path = field(default_factory=Path)
    perf_traces_dir: Path = field(default_factory=Path)

    def __post_init__(self) -> None:
        if not self.users_file or str(self.users_file) in {".", ""}:
            override = _env("BILLET_WEB_USERS_FILE")
            if override:
                self.users_file = _expand_path(override)
            else:
                self.users_file = Path(__file__).resolve().parent / "users.yaml"
        webd_env = _load_webd_env()
        if not self.webd_socket or str(self.webd_socket) in {".", ""}:
            raw = _env("BILLET_WEBD_SOCKET") or webd_env.get("BILLET_WEBD_SOCKET")
            if not raw:
                run = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/aicad-{os.getuid()}"
                raw = f"{run}/aicad/hostd-billetweb.sock"
            self.webd_socket = _expand_path(raw)
        if not self.workspace_root or str(self.workspace_root) in {".", ""}:
            raw = (
                _env("BILLET_WORKSPACE_ROOT")
                or webd_env.get("BILLET_WORKSPACE_ROOT")
                or "~/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build"
            )
            self.workspace_root = _expand_path(raw)
        if not self.app_dir or str(self.app_dir) in {".", ""}:
            raw = _env("BILLET_WEB_APP_DIR") or "~/billet-web/app"
            self.app_dir = _expand_path(raw)
        if not self.perf_traces_dir or str(self.perf_traces_dir) in {".", ""}:
            raw = _env("BILLET_WEB_PERF_TRACES_DIR")
            if raw:
                self.perf_traces_dir = _expand_path(raw)
            else:
                # Webservice/perf/traces next to the gateway package.
                self.perf_traces_dir = (
                    Path(__file__).resolve().parents[1] / "perf" / "traces"
                )

    @property
    def homes_dir(self) -> Path:
        return self.data_root / "homes"

    @property
    def plm_dir(self) -> Path:
        return self.data_root / "plm"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def chat_db(self) -> Path:
        return self.data_root / "chat.db"

    @property
    def refuse_new_flag(self) -> Path:
        return self.data_root / "REFUSE_NEW"

    @property
    def ai_providers_env(self) -> Path:
        """Optional host-owned env file (KEY=VALUE) of remote-model provider
        credentials forwarded into every session container (shared keys).
        Per-user subscriptions are configured in-app and persist in the
        user's HOME volume instead."""
        return self.data_root / "ai_providers.env"

    def ai_provider_vars(self) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            text = self.ai_providers_env.read_text()
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.isidentifier() and k.isupper():
                out[k] = v.strip()
        return out

    @property
    def cf_issuer(self) -> str:
        return self.cf_team_domain

    @property
    def cf_jwks_url(self) -> str:
        return f"{self.cf_team_domain}/cdn-cgi/access/certs"

    def ensure_data_dirs(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.homes_dir.mkdir(parents=True, exist_ok=True)
        self.plm_dir.mkdir(parents=True, exist_ok=True)
        # Seed a runtime users file from the repo template when the configured
        # path lives outside the package and doesn't exist yet (/admin edits it).
        template = Path(__file__).resolve().parent / "users.yaml"
        if self.users_file != template and not self.users_file.exists():
            self.users_file.parent.mkdir(parents=True, exist_ok=True)
            if template.exists():
                self.users_file.write_text(template.read_text())
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()


settings = load_settings()
