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


# Outbound mail backends. "console" logs the rendered message and is what a
# dev box and the test suite run on; the two real ones are Resend's HTTPS API
# and any SMTP relay (Amazon SES is the documented fallback above 100 mail/day).
MAIL_BACKENDS = frozenset({"console", "resend", "smtp"})
# How the SMTP submission session is encrypted. "starttls" upgrades a plaintext
# connection (port 587); "implicit" is TLS from the first byte (port 465, which
# is all Stalwart's submission listener offers). Both verify the peer chain.
SMTP_TLS_MODES = frozenset({"starttls", "implicit"})
DEFAULT_MAIL_FROM = "Chudware <noreply@chudware.com>"

# Origin the gateway is reached at. Verification links, password-reset links,
# and OAuth redirect_uri values are all built from it, so it is configuration
# rather than something derived from a request header.
DEFAULT_PUBLIC_BASE_URL = "https://chudware.com"


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
    dev_mode: bool = field(default_factory=lambda: _env_bool("CHUDWARE_WEB_DEV", False))
    # ---- Multi-tenant abuse controls -------------------------------------
    # Per-account limits that keep one user from consuming the whole service.
    # They matter the moment identities stop being hand-approved teammates.
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
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
    # ---- Transactional mail ----------------------------------------------
    # Every send is queued in mail_outbox and drained by a single asyncio
    # task (mailer.py), so a dead SMTP host or a rate-limited Resend account
    # can never turn a signup into a 500. "console" is the default because a
    # dev box and the test suite must work with no credentials at all.
    mail_backend: str = field(
        default_factory=lambda: (
            _env("CHUDWARE_MAIL_BACKEND", "console") or "console"
        ).strip().lower()
    )
    mail_from: str = field(
        default_factory=lambda: _env("CHUDWARE_MAIL_FROM", DEFAULT_MAIL_FROM)
        or DEFAULT_MAIL_FROM
    )
    resend_api_key: str = field(
        default_factory=lambda: _env("CHUDWARE_RESEND_API_KEY", "") or ""
    )
    smtp_host: str = field(default_factory=lambda: _env("CHUDWARE_SMTP_HOST", "") or "")
    smtp_port: int = field(default_factory=lambda: _env_int("CHUDWARE_SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("CHUDWARE_SMTP_USER", "") or "")
    smtp_pass: str = field(default_factory=lambda: _env("CHUDWARE_SMTP_PASS", "") or "")
    # STARTTLS on 587 is the common case, but a submission listener that only
    # speaks implicit TLS (Stalwart publishes 465 and refuses 587) needs the
    # session encrypted from the first byte. There is no plaintext option: a
    # relay password must never cross even a loopback hop in the clear.
    smtp_tls: str = field(
        default_factory=lambda: (
            _env("CHUDWARE_SMTP_TLS", "starttls") or "starttls"
        ).strip().lower()
    )
    # Admin browser-terminal (an interactive shell as the gateway account).
    # Off by default: it must be an explicit operator opt-in, never something a
    # public-facing deployment inherits silently.
    terminal_enabled: bool = field(
        default_factory=lambda: _env_bool("CHUDWARE_WEB_TERMINAL_ENABLED", False)
    )
    # Single-tenant compatibility only: let web RPCs use this host account's
    # provider profile. Fail closed when unset because a multi-tenant gateway
    # otherwise spends the operator's keys outside the credit ledger.
    share_provider_keys: bool = field(
        default_factory=lambda: _env_bool("CHUDWARE_WEB_SHARE_PROVIDER_KEYS", False)
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
    # ---- Self-serve authentication ---------------------------------------
    # session_secret is the fourth accepted answer to "is strong auth
    # configured?" in AuthMiddleware: a deployment that authenticates its own
    # users with passwords and OAuth needs neither Cloudflare Access nor an
    # edge IdP, and must not be refused a boot for lacking them. It signs the
    # OAuth state parameter; session ids themselves are random rows, not
    # signed blobs, so rotating this value does not log anybody out.
    session_secret: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_SESSION_SECRET", "") or ""
    )
    # URL-safe base64 encoding of 32 random bytes used only for AES-256-GCM
    # provider-key envelopes. It is deliberately independent of session_secret
    # so either secret can rotate without invalidating the other subsystem.
    provider_key_encryption_key: str = field(
        default_factory=lambda: _env(
            "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY", ""
        )
        or ""
    )
    session_ttl_days: int = field(
        # Absolute expiry. last_seen_at slides within the window; this does not.
        default_factory=lambda: _env_int("CHUDWARE_WEB_SESSION_TTL_DAYS", 30)
    )
    trial_days: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_TRIAL_DAYS", 14)
    )
    # Absolute origin used to build verification, reset, and OAuth redirect
    # URLs. It has to be configuration, not a Host header: a link built from
    # attacker-supplied Host is a credential-delivery vulnerability.
    public_base_url: str = field(
        default_factory=lambda: (
            _env("CHUDWARE_WEB_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL)
            or DEFAULT_PUBLIC_BASE_URL
        ).rstrip("/")
    )
    google_client_id: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_GOOGLE_CLIENT_ID", "") or ""
    )
    google_client_secret: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_GOOGLE_CLIENT_SECRET", "") or ""
    )
    github_client_id: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_GITHUB_CLIENT_ID", "") or ""
    )
    github_client_secret: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_GITHUB_CLIENT_SECRET", "") or ""
    )
    # ---- Stripe billing -------------------------------------------------
    # Unset is a supported deployment state. Once any subscription billing
    # credential is present, __post_init__ requires the complete set so the
    # gateway cannot take payment without being able to resolve its webhook.
    stripe_secret_key: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_STRIPE_SECRET_KEY", "") or ""
    )
    stripe_webhook_secret: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET", "") or ""
    )
    stripe_success_url: str = field(
        default_factory=lambda: _env(
            "CHUDWARE_WEB_STRIPE_SUCCESS_URL",
            "https://app.chudware.com/?billing=success",
        )
        or "https://app.chudware.com/?billing=success"
    )
    stripe_cancel_url: str = field(
        default_factory=lambda: _env(
            "CHUDWARE_WEB_STRIPE_CANCEL_URL",
            "https://app.chudware.com/?billing=cancel",
        )
        or "https://app.chudware.com/?billing=cancel"
    )
    stripe_price_desktop: str = field(
        default_factory=lambda: _env("CHUDWARE_STRIPE_PRICE_DESKTOP", "") or ""
    )
    stripe_price_desktop_edu: str = field(
        default_factory=lambda: _env("CHUDWARE_STRIPE_PRICE_DESKTOP_EDU", "") or ""
    )
    stripe_price_studio: str = field(
        default_factory=lambda: _env("CHUDWARE_STRIPE_PRICE_STUDIO", "") or ""
    )
    stripe_price_studio_edu: str = field(
        default_factory=lambda: _env("CHUDWARE_STRIPE_PRICE_STUDIO_EDU", "") or ""
    )
    stripe_price_credits_starter: str = field(
        default_factory=lambda: _env("CHUDWARE_STRIPE_PRICE_CREDITS_STARTER", "") or ""
    )
    stripe_price_credits_standard: str = field(
        default_factory=lambda: _env("CHUDWARE_STRIPE_PRICE_CREDITS_STANDARD", "") or ""
    )
    stripe_price_credits_bulk: str = field(
        default_factory=lambda: _env("CHUDWARE_STRIPE_PRICE_CREDITS_BULK", "") or ""
    )


    def __post_init__(self) -> None:
        if self.max_engine_bridges_per_user < 1:
            raise ValueError(
                "CHUDWARE_WEB_MAX_ENGINE_BRIDGES_PER_USER must be at least 1"
            )
        if self.mail_backend not in MAIL_BACKENDS:
            raise ValueError(
                "CHUDWARE_MAIL_BACKEND must be one of "
                f"{', '.join(sorted(MAIL_BACKENDS))} — got {self.mail_backend!r}"
            )
        # Fail at boot, not on the first password-reset request: a backend
        # selected without its credential can never deliver anything.
        if self.mail_backend == "resend" and not self.resend_api_key:
            raise ValueError(
                "CHUDWARE_MAIL_BACKEND=resend requires CHUDWARE_RESEND_API_KEY"
            )
        if self.mail_backend == "smtp" and not self.smtp_host:
            raise ValueError("CHUDWARE_MAIL_BACKEND=smtp requires CHUDWARE_SMTP_HOST")
        if self.mail_backend == "smtp" and self.smtp_tls not in SMTP_TLS_MODES:
            raise ValueError(
                "CHUDWARE_SMTP_TLS must be one of "
                f"{', '.join(sorted(SMTP_TLS_MODES))} — got {self.smtp_tls!r}"
            )
        if self.session_ttl_days < 1:
            raise ValueError("CHUDWARE_WEB_SESSION_TTL_DAYS must be at least 1")
        if self.trial_days < 1:
            raise ValueError("CHUDWARE_WEB_TRIAL_DAYS must be at least 1")
        # The OAuth state parameter is HMAC-signed with the session secret, so
        # a provider configured without one has no CSRF protection on its
        # callback at all. Refuse the combination at boot.
        if not self.session_secret and (
            (self.google_client_id and self.google_client_secret)
            or (self.github_client_id and self.github_client_secret)
        ):
            raise ValueError(
                "OAuth sign-in requires CHUDWARE_WEB_SESSION_SECRET "
                "(it signs the single-use OAuth state parameter)"
            )
        stripe_required = {
            "CHUDWARE_WEB_STRIPE_SECRET_KEY": self.stripe_secret_key,
            "CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret,
            "CHUDWARE_STRIPE_PRICE_DESKTOP": self.stripe_price_desktop,
            "CHUDWARE_STRIPE_PRICE_DESKTOP_EDU": self.stripe_price_desktop_edu,
            "CHUDWARE_STRIPE_PRICE_STUDIO": self.stripe_price_studio,
            "CHUDWARE_STRIPE_PRICE_STUDIO_EDU": self.stripe_price_studio_edu,
        }
        if any(stripe_required.values()) and not all(stripe_required.values()):
            missing = ", ".join(
                name for name, value in stripe_required.items() if not value
            )
            raise ValueError(
                "Stripe billing is partially configured; missing " + missing
            )
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
        except OSError:
            # Don't crash gateway boot if the path is unwritable; list endpoints
            # will still 503 with a clear detail.
            pass
        try:
            self.bucket_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()


settings = load_settings()
