"""Env-driven gateway configuration (CHUDWARE_WEB_* prefix)."""

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
    """KEY=VALUE defaults written by deploy/install_webd.sh (~/chudware-web/webd.env)."""
    path = Path(_env("CHUDWARE_WEBD_ENV", "~/chudware-web/webd.env") or "").expanduser()
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
            _env("CHUDWARE_WEB_DATA_ROOT", "~/chudware-web-data") or "~/chudware-web-data"
        )
    )
    host: str = field(default_factory=lambda: _env("CHUDWARE_WEB_HOST", "127.0.0.1") or "127.0.0.1")
    port: int = field(default_factory=lambda: _env_int("CHUDWARE_WEB_PORT", 8300))
    cf_team_domain: str = field(
        # Accept "team.cloudflareaccess.com" or "https://team.cloudflareaccess.com";
        # the Access JWT `iss` claim and the JWKS URL both need the https:// form.
        default_factory=lambda: _normalize_team_domain(
            _env("CHUDWARE_WEB_CF_TEAM_DOMAIN", "") or ""
        )
    )
    cf_aud: str = field(default_factory=lambda: _env("CHUDWARE_WEB_CF_AUD", "") or "")
    # Multi-tenant Microsoft Entra (UA school accounts) front door: oauth2-proxy on
    # sso.chudware.com verifies the OIDC login itself, then reverse-proxies here over
    # loopback with this value as its HTTP Basic Auth password (see auth.py). Equivalent
    # defense-in-depth to the CF Access JWT signature check — empty disables the path.
    sso_shared_secret: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_SSO_SHARED_SECRET", "") or ""
    )
    dev_mode: bool = field(default_factory=lambda: _env_bool("CHUDWARE_WEB_DEV", False))
    # ---- Multi-tenant abuse controls -------------------------------------
    # Per-account limits that keep one user from consuming the whole service.
    # They matter the moment identities stop being hand-approved teammates.
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
    )
    max_user_disk_bytes: int = field(
        # Per-account storage across workspace + bucket. 0 disables the check.
        default_factory=lambda: _env_int("CHUDWARE_WEB_MAX_USER_DISK_BYTES", 2 * 1024 * 1024 * 1024)
    )
    ws_max_message_bytes: int = field(
        # Per-frame cap on the /engine bridge. The daemon line limit is 256 MiB;
        # forwarding anything near that OOM-kills a 1 GiB gateway.
        default_factory=lambda: _env_int("CHUDWARE_WEB_WS_MAX_MESSAGE_BYTES", 4 * 1024 * 1024)
    )
    max_engine_bridges_per_user: int = field(
        # Concurrent /engine websockets per account. The browser client opens
        # one and reconnects on drop, so a small number is plenty; this only
        # exists to stop one account fanning out across the shared daemon.
        # Not 1: a reconnect can briefly overlap the socket it replaces, and a
        # user may legitimately have two tabs open. Zero is rejected during
        # initialization rather than silently taking the web-native client offline.
        default_factory=lambda: _env_int("CHUDWARE_WEB_MAX_ENGINE_BRIDGES_PER_USER", 4)
    )
    rate_writes_per_min: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_RATE_WRITES_PER_MIN", 120)
    )
    rate_uploads_per_min: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_RATE_UPLOADS_PER_MIN", 30)
    )
    rate_chat_per_min: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_RATE_CHAT_PER_MIN", 20)
    )
    # Admin browser-terminal (an interactive shell as the gateway account).
    # Off by default: it must be an explicit operator opt-in, never something a
    # public-facing deployment inherits silently.
    terminal_enabled: bool = field(
        default_factory=lambda: _env_bool("CHUDWARE_WEB_TERMINAL_ENABLED", False)
    )
    terminal_max_sessions: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_TERMINAL_MAX_SESSIONS", 8)
    )
    terminal_idle_timeout_sec: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_TERMINAL_IDLE_TIMEOUT_SEC", 30 * 60)
    )
    # Colon-separated allowlist of host directories an admin may browse / pick
    # as a project root. Empty means "the gateway account's home only".
    project_roots_raw: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_PROJECT_ROOTS", "") or ""
    )
    users_file: Path = field(default_factory=Path)
    # Web-native client: shared engine daemon socket + team workspace root.
    # Env overrides win; else webd.env (install_webd.sh); else deterministic
    # defaults (instance key "chudwareweb" is verbatim => socket name is stable).
    webd_socket: Path = field(default_factory=Path)
    workspace_root: Path = field(default_factory=Path)
    # Team file-drop bucket (/bucket page): any logged-in user can upload/organize
    # files here. Independent of workspace_root (the CAD engine's live doc tree) —
    # separate concern so uploads don't collide with .aicm mesh cache / doc autosave.
    bucket_root: Path = field(default_factory=Path)
    # WASM web client static root (served at /app) + client perf traces.
    app_dir: Path = field(default_factory=Path)
    perf_traces_dir: Path = field(default_factory=Path)

    def __post_init__(self) -> None:
        if self.max_engine_bridges_per_user < 1:
            raise ValueError(
                "CHUDWARE_WEB_MAX_ENGINE_BRIDGES_PER_USER must be at least 1"
            )
        if not self.users_file or str(self.users_file) in {".", ""}:
            override = _env("CHUDWARE_WEB_USERS_FILE")
            if override:
                self.users_file = _expand_path(override)
            else:
                self.users_file = Path(__file__).resolve().parent / "users.yaml"
        webd_env = _load_webd_env()
        if not self.webd_socket or str(self.webd_socket) in {".", ""}:
            raw = _env("CHUDWARE_WEBD_SOCKET") or webd_env.get("CHUDWARE_WEBD_SOCKET")
            if not raw:
                run = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/chudware-{os.getuid()}"
                raw = f"{run}/chudware/hostd-chudwareweb.sock"
            self.webd_socket = _expand_path(raw)
        if not self.workspace_root or str(self.workspace_root) in {".", ""}:
            raw = (
                _env("CHUDWARE_WORKSPACE_ROOT")
                or webd_env.get("CHUDWARE_WORKSPACE_ROOT")
                or "~/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build"
            )
            self.workspace_root = _expand_path(raw)
        if not self.bucket_root or str(self.bucket_root) in {".", ""}:
            raw = _env("CHUDWARE_WEB_BUCKET_ROOT") or "~/ME489"
            self.bucket_root = _expand_path(raw)
        if not self.app_dir or str(self.app_dir) in {".", ""}:
            raw = _env("CHUDWARE_WEB_APP_DIR") or "~/chudware-web/app"
            self.app_dir = _expand_path(raw)
        if not self.perf_traces_dir or str(self.perf_traces_dir) in {".", ""}:
            raw = _env("CHUDWARE_WEB_PERF_TRACES_DIR")
            if raw:
                self.perf_traces_dir = _expand_path(raw)
            else:
                # Webservice/perf/traces next to the gateway package.
                self.perf_traces_dir = (
                    Path(__file__).resolve().parents[1] / "perf" / "traces"
                )

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
    def project_roots(self) -> list[Path]:
        """Host directories an admin may browse / pick as a project root.

        Defaults to the gateway account's home. Anything outside this list is
        rejected, so a mistyped or malicious project path cannot expose /etc
        or another user's home.
        """
        raw = [p for p in self.project_roots_raw.split(":") if p.strip()]
        roots: list[Path] = []
        for entry in raw:
            try:
                roots.append(_expand_path(entry).resolve())
            except OSError:
                continue
        return roots or [Path.home().resolve()]

    def user_workspace_root(self, slug: str) -> Path:
        """Per-account CAD workspace. Isolation boundary for /workspace/*."""
        return self.workspace_root / "users" / slug

    def user_bucket_root(self, slug: str) -> Path:
        """Per-account file drop. Isolation boundary for /api/bucket/*."""
        return self.bucket_root / "users" / slug

    @property
    def terminal_layout_file(self) -> Path:
        """Shared /admin terminal window-manager layout (one doc for all admins)."""
        return self.data_root / "terminal_layout.json"

    @property
    def ai_providers_env(self) -> Path:
        """Optional host-owned env file (KEY=VALUE) of shared remote-model
        provider credentials, used as the fallback when host config carries no
        keys.* slot for a provider."""
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
        self.plm_dir.mkdir(parents=True, exist_ok=True)
        # Shared team workspace must exist: /workspace/* 503s otherwise, and a
        # migrated host often has webd.env pointing at a path that was never
        # created here. Creating the configured root is safe (empty tree).
        try:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Don't crash gateway boot if the path is unwritable; list endpoints
            # will still 503 with a clear detail.
            pass
        try:
            self.bucket_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
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
