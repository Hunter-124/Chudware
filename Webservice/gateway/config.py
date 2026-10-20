"""Env-driven gateway configuration (CHUDWARE_WEB_* prefix)."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .deployment_config import (
        DEFAULT_MAIL_FROM, DEFAULT_PUBLIC_BASE_URL, MAIL_BACKENDS, SMTP_TLS_MODES,
        MAIL_KEY, PROVIDER_KEY, decode_encryption_key,
    )
    from .storage_config import configured_storage, data_root as storage_data_root, expand_path as _expand_path
except ImportError:
    from deployment_config import (
        DEFAULT_MAIL_FROM, DEFAULT_PUBLIC_BASE_URL, MAIL_BACKENDS, SMTP_TLS_MODES,
        MAIL_KEY, PROVIDER_KEY, decode_encryption_key,
    )
    from storage_config import configured_storage, data_root as storage_data_root, expand_path as _expand_path


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




def _canonical_uuid(value: str) -> str:
    """Canonical lowercase UUID text, or ValueError.

    Used for the org/team path components of the shared bucket tree: only the
    exact 8-4-4-4-12 form is accepted, so no separator, traversal sequence, or
    absolute path can ever reach a path join.
    """
    raw = (value or "").strip()
    parsed = uuid.UUID(raw)
    if str(parsed) != raw.lower():
        raise ValueError(f"non-canonical uuid: {value!r}")
    return str(parsed)


def _normalize_team_domain(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw



@dataclass
class Settings:
    """Runtime settings loaded once from the process environment."""

    deploy_environment: str = field(
        default_factory=lambda: _env("CHUDWARE_DEPLOY_ENV", "prod") or "prod"
    )

    data_root: Path = field(
        default_factory=lambda: storage_data_root(_env("CHUDWARE_DEPLOY_ENV", "prod") or "prod")
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
        # exists to stop one account exhausting its cell and gateway bridges.
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
    # OS-isolated cell directory and retained workspace discovery roots.
    # webd.env supplies legacy workspace storage only, never an engine endpoint.
    cells_root: Path = field(default_factory=Path)
    workspace_root: Path = field(default_factory=Path)
    # File bucket storage: the default scope is private to one account; explicit
    # team scopes live under organizations/<org UUID>/teams/<team UUID> and are
    # authorized from PostgreSQL memberships on every request.
    bucket_root: Path = field(default_factory=Path)
    # Operator-provisioned institutional collaboration, never a paid/AI grant.
    university_org_id: str = field(default_factory=lambda: _env("CHUDWARE_WEB_UNIVERSITY_ORG_ID", "") or "")
    university_team_id: str = field(default_factory=lambda: _env("CHUDWARE_WEB_UNIVERSITY_TEAM_ID", "") or "")
    university_workspace_root: str = field(default_factory=lambda: _env("CHUDWARE_WEB_UNIVERSITY_WORKSPACE_ROOT", "") or "")
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
    mail_context_encryption_key: bytes = field(default=b"", repr=False)
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
    # How long an organization invitation link stays redeemable. Long enough to
    # survive a weekend and a forwarded mailbox, short enough that a link
    # sitting in an old inbox is not a standing key to a customer's files.
    org_invite_ttl_days: int = field(
        default_factory=lambda: _env_int("CHUDWARE_WEB_ORG_INVITE_TTL_DAYS", 14)
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
    # Microsoft (Entra ID). The tenant is part of every endpoint URL:
    # "common" accepts both work/school and personal accounts, "organizations"
    # only work/school, or a concrete tenant id/domain locks sign-in to one
    # directory.
    microsoft_client_id: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_MICROSOFT_CLIENT_ID", "") or ""
    )
    microsoft_client_secret: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_MICROSOFT_CLIENT_SECRET", "") or ""
    )
    microsoft_tenant: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_MICROSOFT_TENANT", "common")
        or "common"
    )
    # ---- Stripe billing -------------------------------------------------
    # Unset is a supported deployment state. Once any subscription billing
    # credential is present, __post_init__ requires the complete set so the
    # gateway cannot take payment without being able to resolve its webhook.
    # Credit-pack checkout is unfinished and therefore opt-in independently of
    # subscription billing. Legacy price ids stay configured so delayed
    # webhooks and historic admin reporting can still recognize old sessions.
    credit_pack_checkout_enabled: bool = field(
        default_factory=lambda: _env_bool("CHUDWARE_CREDIT_PACK_CHECKOUT_ENABLED", False)
    )
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
    # Published to the browser for Stripe.js. Safe to expose by design; it is
    # here so the front end never hard-codes a key and a test/live swap is one
    # env change.
    stripe_publishable_key: str = field(
        default_factory=lambda: _env("CHUDWARE_WEB_STRIPE_PUBLISHABLE_KEY", "") or ""
    )
    # ---- Marketplace ----------------------------------------------------
    # The marketplace is off unless switched on: it introduces public,
    # anonymously readable routes, and a half-configured deployment should serve
    # 404 rather than an empty storefront.
    marketplace_enabled: bool = field(
        default_factory=lambda: _env_bool("CHUDWARE_MARKETPLACE_ENABLED", False)
    )
    marketplace_root_raw: str = field(
        default_factory=lambda: _env("CHUDWARE_MARKETPLACE_ROOT", "") or ""
    )
    # Platform cut on a cash sale, in basis points. Charged as a Stripe Connect
    # ``application_fee_amount`` on a destination charge, so this is the only
    # place the number exists and Stripe enforces it.
    marketplace_fee_bps: int = field(
        default_factory=lambda: _env_int("CHUDWARE_MARKETPLACE_FEE_BPS", 1500)
    )
    # Deliberately lower than the cash fee. A credit sale costs us no card
    # processing and the credits stay on the platform, so taking the same cut
    # would be charging for a service we did not perform.
    marketplace_credit_fee_bps: int = field(
        default_factory=lambda: _env_int("CHUDWARE_MARKETPLACE_CREDIT_FEE_BPS", 1000)
    )
    # 1 credit == $0.001 of upstream model cost (credits.py's module docstring),
    # so 1000 credits to the dollar is the identity rate. Changing it re-prices
    # every seller cash-out, which is why mk_payout_requests freezes the rate it
    # was created under.
    marketplace_credits_per_dollar: int = field(
        default_factory=lambda: _env_int("CHUDWARE_MARKETPLACE_CREDITS_PER_DOLLAR", 1000)
    )
    # Cash-out floor. Below this a Stripe transfer's fixed cost eats the payout.
    marketplace_payout_min_credits: int = field(
        default_factory=lambda: _env_int("CHUDWARE_MARKETPLACE_PAYOUT_MIN_CREDITS", 25_000)
    )
    # Per-asset ceiling, separate from max_upload_bytes: a purchasable CAD
    # assembly is legitimately far larger than a scratch upload.
    marketplace_max_asset_bytes: int = field(
        default_factory=lambda: _env_int(
            "CHUDWARE_MARKETPLACE_MAX_ASSET_BYTES", 512 * 1024 * 1024
        )
    )
    # Total bytes one seller's published listings may occupy.
    marketplace_seller_quota_bytes: int = field(
        default_factory=lambda: _env_int(
            "CHUDWARE_MARKETPLACE_SELLER_QUOTA_BYTES", 20 * 1024 * 1024 * 1024
        )
    )
    # A first paid listing goes through human review; later ones publish
    # immediately. Off means everything publishes immediately, which is only
    # appropriate on a staging host.
    marketplace_review_first_paid: bool = field(
        default_factory=lambda: _env_bool("CHUDWARE_MARKETPLACE_REVIEW_FIRST_PAID", True)
    )
    # Where a Connect onboarding return/refresh lands. Stripe requires absolute
    # URLs and will not accept a relative path.
    marketplace_connect_return_url: str = field(
        default_factory=lambda: _env(
            "CHUDWARE_MARKETPLACE_CONNECT_RETURN_URL",
            "https://app.chudware.com/studio#payouts",
        )
        or "https://app.chudware.com/studio#payouts"
    )
    marketplace_success_url: str = field(
        default_factory=lambda: _env(
            "CHUDWARE_MARKETPLACE_SUCCESS_URL",
            "https://app.chudware.com/marketplace#purchased",
        )
        or "https://app.chudware.com/marketplace#purchased"
    )
    marketplace_cancel_url: str = field(
        default_factory=lambda: _env(
            "CHUDWARE_MARKETPLACE_CANCEL_URL",
            "https://app.chudware.com/marketplace#canceled",
        )
        or "https://app.chudware.com/marketplace#canceled"
    )
    # ---- Marketplace import sources -------------------------------------
    # Each is optional and independent. An unset credential degrades its adapter
    # to public-metadata mode rather than disabling the source, because reading a
    # public page needs no key and is the majority of real imports.
    mk_thingiverse_token: str = field(
        default_factory=lambda: _env("CHUDWARE_MK_THINGIVERSE_TOKEN", "") or ""
    )
    mk_sketchfab_token: str = field(
        default_factory=lambda: _env("CHUDWARE_MK_SKETCHFAB_TOKEN", "") or ""
    )
    mk_myminifactory_token: str = field(
        default_factory=lambda: _env("CHUDWARE_MK_MYMINIFACTORY_TOKEN", "") or ""
    )
    mk_github_token: str = field(
        default_factory=lambda: _env("CHUDWARE_MK_GITHUB_TOKEN", "") or ""
    )
    mk_onshape_client_id: str = field(
        default_factory=lambda: _env("CHUDWARE_MK_ONSHAPE_CLIENT_ID", "") or ""
    )
    mk_onshape_client_secret: str = field(
        default_factory=lambda: _env("CHUDWARE_MK_ONSHAPE_CLIENT_SECRET", "") or ""
    )


    def __post_init__(self) -> None:
        if self.deploy_environment not in {"prod", "staging"}:
            raise ValueError("CHUDWARE_DEPLOY_ENV must be prod or staging")
        if not self.mail_context_encryption_key:
            self.mail_context_encryption_key = decode_encryption_key(
                _env(MAIL_KEY, "") or "", MAIL_KEY,
            )
        if not isinstance(self.mail_context_encryption_key, bytes) or len(self.mail_context_encryption_key) != 32:
            raise ValueError("CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY must encode 32 bytes")
        if self.provider_key_encryption_key:
            provider_key = decode_encryption_key(self.provider_key_encryption_key.strip(), PROVIDER_KEY)
            if provider_key == self.mail_context_encryption_key:
                raise ValueError("mail and provider encryption keys must be independent")
        if self.max_engine_bridges_per_user < 1:
            raise ValueError(
                "CHUDWARE_WEB_MAX_ENGINE_BRIDGES_PER_USER must be at least 1"
            )
        # Marketplace money knobs. Validated at boot because every one of them
        # is read inside a transaction that moves value: a nonsense fee
        # discovered at checkout time has already taken the customer's money.
        if not 0 <= self.marketplace_fee_bps <= 10_000:
            raise ValueError(
                "CHUDWARE_MARKETPLACE_FEE_BPS must be between 0 and 10000 basis points"
            )
        if not 0 <= self.marketplace_credit_fee_bps <= 10_000:
            raise ValueError(
                "CHUDWARE_MARKETPLACE_CREDIT_FEE_BPS must be between 0 and 10000 "
                "basis points"
            )
        if self.marketplace_credits_per_dollar < 1:
            raise ValueError(
                "CHUDWARE_MARKETPLACE_CREDITS_PER_DOLLAR must be at least 1"
            )
        if self.marketplace_payout_min_credits < 1:
            raise ValueError(
                "CHUDWARE_MARKETPLACE_PAYOUT_MIN_CREDITS must be at least 1"
            )
        if self.marketplace_max_asset_bytes < 1:
            raise ValueError("CHUDWARE_MARKETPLACE_MAX_ASSET_BYTES must be positive")
        if self.marketplace_seller_quota_bytes < self.marketplace_max_asset_bytes:
            raise ValueError(
                "CHUDWARE_MARKETPLACE_SELLER_QUOTA_BYTES must be at least "
                "CHUDWARE_MARKETPLACE_MAX_ASSET_BYTES, or no seller could upload "
                "a single largest-allowed asset"
            )
        # Cash sales need Connect, which needs a secret key. Enabling the
        # marketplace without one yields a storefront where every paid listing's
        # buy button 503s, so refuse the combination outright.
        if self.marketplace_enabled and not self.stripe_secret_key:
            raise ValueError(
                "CHUDWARE_MARKETPLACE_ENABLED=1 requires "
                "CHUDWARE_WEB_STRIPE_SECRET_KEY: cash listings settle through "
                "Stripe Connect and cannot be served without it"
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
            or (self.microsoft_client_id and self.microsoft_client_secret)
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
        staging = self.deploy_environment == "staging"
        storage = configured_storage(
            self.deploy_environment, data=self.data_root, workspace=self.workspace_root,
            bucket=self.bucket_root, cells=self.cells_root, university=self.university_workspace_root,
        )
        self.workspace_root = storage.roots["workspace"]
        self.bucket_root = storage.roots["bucket"]
        self.cells_root = storage.roots["cells"]
        if not self.app_dir or str(self.app_dir) in {".", ""}:
            raw = _env("CHUDWARE_WEB_APP_DIR") or (
                str(self.data_root / "app") if staging else "~/chudware-web/app"
            )
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
    def gateway_unit(self) -> str:
        if self.deploy_environment == "staging":
            return "chudware-gateway-staging"
        if self.deploy_environment == "prod":
            return "chudware-gateway"
        raise ValueError("unknown deployment environment")

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
        if self.deploy_environment == "staging":
            return [self.workspace_root.resolve(), self.bucket_root.resolve()]
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

    def team_bucket_root(self, org_id: str, team_id: str) -> Path:
        """Shared team file drop. Isolation boundary for /api/bucket/*?team=.

        Both identifiers become path components, so both are re-validated here
        as canonical UUID text even though the caller already validated them:
        this is the last function before the join, and a helper that accepts
        ``"../"`` because somebody skipped a check upstream is the whole bug
        class. Raises ``ValueError`` — a caller that reaches this with garbage
        has a logic error, not a client with a bad request.
        """
        return (
            self.bucket_root
            / "orgs"
            / _canonical_uuid(org_id)
            / "teams"
            / _canonical_uuid(team_id)
        )

    @property
    def marketplace_root(self) -> Path:
        """Root of all marketplace asset storage.

        Deliberately NOT under ``workspace_root`` or ``bucket_root``. Those two
        trees are per-tenant private storage whose whole contract is that nobody
        else can read them; marketplace assets are the opposite — published
        bytes served to strangers — and mixing the two would put a public route
        one path-handling bug away from a private file. Defaults beside the rest
        of the app's own state rather than in a customer tree.
        """
        raw = self.marketplace_root_raw.strip()
        if raw:
            return _expand_path(raw)
        return self.data_root / "marketplace"

    def listing_asset_root(self, listing_id: str) -> Path:
        """Asset directory for one listing.

        The listing UUID is re-validated as canonical UUID text here even though
        every caller already holds it from a database row, for the same reason
        ``team_bucket_root`` does it: this is the last function before the join,
        and it is the one place that has to be right if something upstream is
        ever wrong. Sharded on the first two hex digits so a directory listing of
        the root stays usable at a million listings.
        """
        canonical = _canonical_uuid(listing_id)
        return self.marketplace_root / "listings" / canonical[:2] / canonical

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
