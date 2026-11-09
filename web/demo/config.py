"""Environment-driven front-door configuration (CHUDWARE_DEMO_* names).

Every tunable has a safe default so ``config.example.env`` only needs the secrets.
Values that reach ``podman`` argument strings are validated here, so a typo in the
environment file cannot smuggle an extra mount option or flag into a container.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

GIB = 1024 ** 3
MIB = 1024 ** 2

# All tool-capable free OpenRouter models at the time of writing. OpenRouter retires
# free models often, so the AI lane also intersects this list with OpenRouter's live
# model catalogue before advertising anything.
DEFAULT_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "google/gemma-4-31b-it:free",
    "cohere/north-mini-code:free",
)
DEFAULT_IMAGE_REPO = "ghcr.io/hunter-124/chudware-web"
DEFAULT_PUBLIC_HOSTS = ("app.chudware.com", "localhost", "127.0.0.1")
# Plain ``--network pasta``: measured on the target host, host loopback is unreachable
# from the container; LAN egress is blocked by deploy/egress.nft, not by pasta.
DEFAULT_PASTA_OPTIONS: tuple[str, ...] = ()
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
# Free OpenRouter models that NVIDIA's own API also serves, under NVIDIA's id. With an NVIDIA
# key the lane serves these from NVIDIA instead (no daily cap there, only a per-minute rate).
NVIDIA_MODELS: dict[str, str] = {
    "nvidia/nemotron-3-ultra-550b-a55b:free": "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-super-vl:free": "nvidia/nemotron-3.5-super-vl-120b-a12b",
}
# Further models served from NVIDIA's free API only (listed after the free OpenRouter-mapped
# ones), with their display names. Each answered tool calls quickly and correctly when chosen
# (2026-11-09); DeepSeek V4.1 Flash (minutes or no answer), GLM-5.3 Flash (minutes), Gemma 4
# (its content screen refused a plain CAD prompt) and Kimi K2.6 (not served to free accounts)
# were not reliable enough.
NVIDIA_MODEL_NAMES: dict[str, str] = {
    "mistralai/mistral-large-4-1t-a52b": "Mistral Large 4",
    "minimaxai/minimax-m3.1-flash": "MiniMax M3.1 Flash",
}
DEFAULT_NVIDIA_MODELS = tuple(NVIDIA_MODEL_NAMES)
DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"


@dataclass(frozen=True)
class ClaudePrice:
    """USD per million tokens (platform.claude.com pricing, 2026-11)."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class ClaudeModel:
    name: str
    # Advertised to OMP as the context window, so compaction keeps every request inside it.
    context: int
    price: ClaudePrice
    # A cheaper card up to ``tier_tokens`` prompt tokens (input + cache read + cache write).
    tier_tokens: int | None = None
    tier_price: ClaudePrice | None = None


# The only Claude models the demo may serve: the 5.5 generation of Opus, Sonnet and Haiku.
# Fable and Mythos are deliberately absent; config refuses any id not listed here.
CLAUDE_MODELS: dict[str, ClaudeModel] = {
    "claude-sonnet-5-5": ClaudeModel("Claude Sonnet 5.5", 200_000,
                                     ClaudePrice(2.0, 2.5, 4.0, 0.10, 10.0)),
    "claude-opus-5-5": ClaudeModel("Claude Opus 5.5", 200_000,
                                   ClaudePrice(4.0, 5.0, 8.0, 0.20, 20.0)),
    "claude-haiku-5-5": ClaudeModel("Claude Haiku 5.5", 200_000,
                                    ClaudePrice(0.50, 0.625, 1.0, 0.05, 2.50),
                                    tier_tokens=100_000,
                                    tier_price=ClaudePrice(0.10, 0.125, 0.20, 0.01, 0.50)),
}
DEFAULT_CLAUDE_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")

_MODEL_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)*:free\Z")
_NVIDIA_MODEL_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
_SIZE_RE = re.compile(r"[1-9][0-9]*[kmg]\Z")
_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]{1,128})?(@sha256:[0-9a-f]{64})?\Z")
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z")
_PASTA_RE = re.compile(r"[A-Za-z0-9._/:=-]+\Z")
_SECOPT_RE = re.compile(r"[a-z]+[=:][A-Za-z0-9._/:=*-]+\Z")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")


class ConfigError(ValueError):
    """The environment holds a value the front door refuses to run with."""


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def default_session_root(environ: Mapping[str, str]) -> Path:
    runtime = environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime) / "chudware-demo"
    return Path("/tmp") / f"chudware-demo-{os.getuid()}"


def default_state_dir(environ: Mapping[str, str]) -> Path:
    base = environ.get("XDG_STATE_HOME", "").strip()
    if not base:
        base = str(Path(environ.get("HOME", "").strip() or "/tmp") / ".local" / "state")
    return Path(base) / "chudware-demo"


@dataclass
class DemoConfig:
    # -- public surface ----------------------------------------------------
    public_hosts: tuple[str, ...] = DEFAULT_PUBLIC_HOSTS
    # HMAC key for the session cookie. Ephemeral keys cannot outlive the process, so
    # sessions are never adopted after a restart unless the key came from the env.
    cookie_key: bytes = field(default=b"", repr=False)
    cookie_key_persistent: bool = False
    cookie_max_age: int = 12 * 3600

    # -- admission ---------------------------------------------------------
    max_active: int = 3
    max_queue: int = 30
    idle_seconds: int = 15 * 60
    hard_seconds: int = 45 * 60
    warn_seconds: int = 5 * 60
    queue_poll_timeout: int = 60
    start_timeout: int = 90
    join_limit: int = 6
    join_window: int = 600
    status_wait_max: int = 25
    tick_seconds: float = 1.0
    reconcile_seconds: float = 15.0
    # Host capacity guard: refuse to admit below either floor. A session that is
    # still starting is assumed to need ``start_mem_reserve`` more than it shows.
    min_mem_bytes: int = 4 * GIB
    min_disk_bytes: int = 3 * GIB
    start_mem_reserve: int = 1 * GIB

    # -- proxy limits ------------------------------------------------------
    max_request_bytes: int = 110 * MIB
    ws_max_message_bytes: int = 4 * MIB

    # -- AI lane -----------------------------------------------------------
    openrouter_key: str = field(default="", repr=False)
    openrouter_base: str = DEFAULT_OPENROUTER_BASE
    ai_models: tuple[str, ...] = DEFAULT_MODELS
    # Free models carry no demo quota. OpenRouter's own account allowance (50 free-model
    # requests per UTC day without purchased credits) still applies to the models it serves.
    nvidia_key: str = field(default="", repr=False)
    nvidia_base: str = DEFAULT_NVIDIA_BASE
    nvidia_models: tuple[str, ...] = DEFAULT_NVIDIA_MODELS
    ai_models_cache_seconds: int = 600
    ai_max_output_tokens: int = 16_384
    ai_max_body_bytes: int = 4 * MIB
    # Claude (owner's Claude API key, paid from its monthly credits). Spend caps in USD,
    # enforced before forwarding: per session, per client network per UTC day, all
    # sessions per UTC day, all sessions per UTC calendar month.
    anthropic_key: str = field(default="", repr=False)
    anthropic_base: str = DEFAULT_ANTHROPIC_BASE
    claude_models: tuple[str, ...] = DEFAULT_CLAUDE_MODELS
    claude_session_usd: float = 10.0
    claude_client_daily_usd: float = 10.0
    claude_daily_usd: float = 30.0
    claude_monthly_usd: float = 99.0
    claude_max_output_tokens: int = 32_000

    # -- containers --------------------------------------------------------
    image: str = f"{DEFAULT_IMAGE_REPO}:edge"
    podman_bin: str = "podman"
    session_root: Path = field(default_factory=lambda: default_session_root(os.environ))
    # Survives reboots (the session root is tmpfs); holds the daily AI budget.
    state_dir: Path = field(default_factory=lambda: default_state_dir(os.environ))
    port_base: int = 18300
    port_count: int = 64
    cpus: str = "2"
    memory: str = "3500m"
    pids_limit: int = 512
    shm_size: str = "256m"
    tmpfs_data: str = "256m"
    tmpfs_work: str = "1g"
    tmpfs_state: str = "1g"
    tmpfs_run: str = "64m"
    tmpfs_tmp: str = "512m"
    operator: str = "demo"
    pasta_options: tuple[str, ...] = DEFAULT_PASTA_OPTIONS
    extra_security_opts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.public_hosts or any(not _HOST_RE.fullmatch(h) for h in self.public_hosts):
            raise ConfigError("CHUDWARE_DEMO_PUBLIC_HOSTS must be a comma list of host names")
        if not self.ai_models:
            raise ConfigError("CHUDWARE_DEMO_AI_MODELS must name at least one model")
        for model in self.ai_models:
            if not _MODEL_RE.fullmatch(model):
                raise ConfigError(
                    f"CHUDWARE_DEMO_AI_MODELS entry {model!r} is not a free OpenRouter model "
                    "(it must be <vendor>/<name>:free)")
        for model in self.nvidia_models:
            if not _NVIDIA_MODEL_RE.fullmatch(model):
                raise ConfigError(f"CHUDWARE_DEMO_NVIDIA_MODELS entry {model!r} is not an NVIDIA API "
                                  "model id (<vendor>/<name>)")
        for model in self.claude_models:
            if model not in CLAUDE_MODELS:
                raise ConfigError(
                    f"CHUDWARE_DEMO_CLAUDE_MODELS entry {model!r} is not allowed "
                    f"(only {', '.join(CLAUDE_MODELS)})")
        for name in ("claude_session_usd", "claude_client_daily_usd", "claude_daily_usd",
                     "claude_monthly_usd"):
            if not getattr(self, name) > 0:
                raise ConfigError(f"{name} must be a positive amount")
        for name in ("max_active", "max_queue", "idle_seconds", "hard_seconds", "warn_seconds",
                     "queue_poll_timeout", "start_timeout", "join_limit", "join_window",
                     "ai_max_output_tokens",
                     "claude_max_output_tokens",
                     "ai_models_cache_seconds",
                     "ai_max_body_bytes", "pids_limit", "port_count", "max_request_bytes",
                     "ws_max_message_bytes", "cookie_max_age"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        if self.warn_seconds >= self.hard_seconds:
            raise ConfigError("warning lead time must be shorter than the hard lifetime")
        if not 1024 <= self.port_base <= 65535 - self.port_count:
            raise ConfigError("CHUDWARE_DEMO_PORT_BASE/PORT_COUNT leave the valid port range")
        if self.port_count < self.max_active:
            raise ConfigError("CHUDWARE_DEMO_PORT_COUNT must cover every active slot")
        for name in ("memory", "shm_size", "tmpfs_data", "tmpfs_work", "tmpfs_state",
                     "tmpfs_run", "tmpfs_tmp"):
            if not _SIZE_RE.fullmatch(getattr(self, name)):
                raise ConfigError(f"{name} must look like 512m or 1g")
        if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", self.cpus) or float(self.cpus) <= 0:
            raise ConfigError("CHUDWARE_DEMO_CPUS must be a positive number")
        if not _IMAGE_RE.fullmatch(self.image):
            raise ConfigError("CHUDWARE_DEMO_IMAGE_REPO/IMAGE_TAG is not a valid image reference")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.operator):
            raise ConfigError("CHUDWARE_DEMO_OPERATOR must be 1-64 characters of A-Z a-z 0-9 _ -")
        if any(not _PASTA_RE.fullmatch(opt) for opt in self.pasta_options):
            raise ConfigError("CHUDWARE_DEMO_PASTA_OPTIONS holds an invalid option")
        for opt in self.extra_security_opts:
            if not _SECOPT_RE.fullmatch(opt) or opt.split("=", 1)[0].split(":", 1)[0] in {
                    "seccomp", "privileged"}:
                raise ConfigError(f"CHUDWARE_DEMO_EXTRA_SECURITY_OPTS entry {opt!r} is refused")
        if not self.cookie_key:
            self.cookie_key = secrets.token_bytes(32)
            self.cookie_key_persistent = False
        elif len(self.cookie_key) < 32:
            raise ConfigError("CHUDWARE_DEMO_COOKIE_KEY must be at least 32 characters")
        self.session_root = Path(self.session_root)
        self.state_dir = Path(self.state_dir)

    @property
    def ai_enabled(self) -> bool:
        """Some free model is routable: through OpenRouter, or NVIDIA for the models it serves."""
        return bool(self.openrouter_key or (self.nvidia_key and (
            self.nvidia_models or set(self.ai_models) & set(NVIDIA_MODELS))))

    @property
    def nvidia_enabled(self) -> bool:
        return bool(self.nvidia_key)

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_key and self.claude_models)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DemoConfig":
        env = os.environ if environ is None else environ

        def get(name: str, default: str = "") -> str:
            return env.get("CHUDWARE_DEMO_" + name, "").strip() or default

        def num(name: str, default: int) -> int:
            raw = get(name)
            try:
                return int(raw) if raw else default
            except ValueError:
                raise ConfigError(f"CHUDWARE_DEMO_{name} must be an integer") from None

        def usd(name: str, default: float) -> float:
            raw = get(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                raise ConfigError(f"CHUDWARE_DEMO_{name} must be a number") from None

        tag = get("IMAGE_TAG", "edge")
        if not _TAG_RE.fullmatch(tag):
            raise ConfigError("CHUDWARE_DEMO_IMAGE_TAG is not a valid image tag")
        cookie_key = get("COOKIE_KEY")
        raw_nvidia = get("NVIDIA_MODELS")
        nvidia_models = () if raw_nvidia == "none" else _csv(raw_nvidia) or DEFAULT_NVIDIA_MODELS
        return cls(
            public_hosts=_csv(get("PUBLIC_HOSTS")) or DEFAULT_PUBLIC_HOSTS,
            cookie_key=cookie_key.encode("utf-8"),
            cookie_key_persistent=bool(cookie_key),
            max_active=num("MAX_ACTIVE", 3),
            max_queue=num("MAX_QUEUE", 30),
            idle_seconds=num("IDLE_SECONDS", 15 * 60),
            hard_seconds=num("HARD_SECONDS", 45 * 60),
            warn_seconds=num("WARN_SECONDS", 5 * 60),
            queue_poll_timeout=num("QUEUE_POLL_TIMEOUT", 60),
            start_timeout=num("START_TIMEOUT", 90),
            join_limit=num("JOIN_LIMIT", 6),
            join_window=num("JOIN_WINDOW", 600),
            min_mem_bytes=num("MIN_MEM_GIB", 4) * GIB,
            min_disk_bytes=num("MIN_DISK_GIB", 3) * GIB,
            openrouter_key=env.get("CHUDWARE_DEMO_OPENROUTER_KEY", "").strip(),
            openrouter_base=get("OPENROUTER_BASE", DEFAULT_OPENROUTER_BASE).rstrip("/"),
            ai_models=_csv(get("AI_MODELS")) or DEFAULT_MODELS,
            nvidia_key=env.get("CHUDWARE_DEMO_NVIDIA_KEY", "").strip(),
            nvidia_base=get("NVIDIA_BASE", DEFAULT_NVIDIA_BASE).rstrip("/"),
            # Unset: the defaults; "none": no NVIDIA-only models.
            nvidia_models=nvidia_models,
            ai_max_output_tokens=num("AI_MAX_OUTPUT_TOKENS", 16_384),
            anthropic_key=env.get("CHUDWARE_DEMO_ANTHROPIC_KEY", "").strip(),
            anthropic_base=get("ANTHROPIC_BASE", DEFAULT_ANTHROPIC_BASE).rstrip("/"),
            claude_models=_csv(get("CLAUDE_MODELS")) if get("CLAUDE_MODELS") else DEFAULT_CLAUDE_MODELS,
            claude_session_usd=usd("CLAUDE_SESSION_USD", 10.0),
            claude_client_daily_usd=usd("CLAUDE_CLIENT_DAILY_USD", 10.0),
            claude_daily_usd=usd("CLAUDE_DAILY_USD", 30.0),
            claude_monthly_usd=usd("CLAUDE_MONTHLY_USD", 99.0),
            claude_max_output_tokens=num("CLAUDE_MAX_OUTPUT_TOKENS", 32_000),
            image=f"{get('IMAGE_REPO', DEFAULT_IMAGE_REPO)}:{tag}",
            podman_bin=get("PODMAN_BIN", "podman"),
            session_root=Path(get("SESSION_ROOT")) if get("SESSION_ROOT") else default_session_root(env),
            state_dir=Path(get("STATE_DIR")) if get("STATE_DIR") else default_state_dir(env),
            port_base=num("PORT_BASE", 18300),
            port_count=num("PORT_COUNT", 64),
            cpus=get("CPUS", "2"),
            memory=get("MEMORY", "3500m"),
            pids_limit=num("PIDS_LIMIT", 512),
            tmpfs_data=get("TMPFS_DATA", "256m"),
            tmpfs_work=get("TMPFS_WORK", "1g"),
            tmpfs_state=get("TMPFS_STATE", "1g"),
            tmpfs_run=get("TMPFS_RUN", "64m"),
            tmpfs_tmp=get("TMPFS_TMP", "512m"),
            operator=get("OPERATOR", "demo"),
            pasta_options=_csv(get("PASTA_OPTIONS")) or DEFAULT_PASTA_OPTIONS,
            extra_security_opts=_csv(get("EXTRA_SECURITY_OPTS")),
        )
