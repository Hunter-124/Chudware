"""Pure, redacted deployment configuration primitives; never import Settings or DB."""
from __future__ import annotations

import base64
import binascii
import os
import re
import stat
from collections.abc import Mapping
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit

MAIL_BACKENDS = frozenset({"console", "resend", "smtp"})
SMTP_TLS_MODES = frozenset({"starttls", "implicit"})
DEFAULT_MAIL_FROM = "Chudware <noreply@chudware.com>"
DEFAULT_PUBLIC_BASE_URL = "https://chudware.com"
MAX_ENV_BYTES = 65536
MAIL_KEY = "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY"
PROVIDER_KEY = "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY"


class ConfigurationError(ValueError):
    """An error identifier only; never include operator configuration values."""


def decode_encryption_key(raw: str, name: str) -> bytes:
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,48}", raw):
        raise ConfigurationError(name + " must be URL-safe base64 encoding 32 bytes")
    try:
        value = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ConfigurationError(name + " must be URL-safe base64 encoding 32 bytes") from None
    if len(value) != 32:
        raise ConfigurationError(name + " must encode 32 bytes")
    return value


def absolute_path(raw: str | Path) -> Path:
    text = str(raw)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ConfigurationError("invalid_path")
    path = Path(raw).expanduser()
    if not path.is_absolute() or any(part in {".", ".."} for part in text.split("/")):
        raise ConfigurationError("absolute_traversal_free_path_required")
    return path


def _overflow_uid() -> int:
    """Owner shown for unmapped IDs inside a user namespace (systemd user units
    with ProtectHome/PrivateUsers see root-owned ancestors as this UID)."""
    try:
        return int(Path("/proc/sys/kernel/overflowuid").read_text().strip())
    except (OSError, ValueError):
        return 65534


def open_parent(path: Path, *, protected: bool = True) -> int:
    """Pin every ancestor, refusing links; root-owned sticky /tmp is permitted.

    Root and the namespace overflow UID are equivalent here: an unmapped owner
    is by construction not this service identity, and write bits still refuse."""
    trusted = {0, _overflow_uid(), os.getuid()}
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            sticky_root = info.st_uid in trusted - {os.getuid()} and info.st_mode & stat.S_ISVTX
            if protected and (info.st_uid not in trusted or (info.st_mode & 0o022 and not sticky_root)):
                raise ConfigurationError("unprotected_environment_ancestor")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_environment_file(path: str | Path, *, private: bool = True) -> tuple[dict[str, str], str, os.stat_result]:
    """Read bounded, single-link operator input, with no shell evaluation."""
    path = absolute_path(path)
    parent = open_parent(path)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in {0, os.getuid()}
                or before.st_mode & (0o077 if private else 0o022)
                or before.st_size > MAX_ENV_BYTES):
            raise ConfigurationError("unprotected_environment_file")
        contents = source.read(MAX_ENV_BYTES + 1)
        after = os.fstat(source.fileno())
        stable = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                               item.st_ctime_ns, item.st_mode, item.st_uid, item.st_nlink)
        if len(contents) > MAX_ENV_BYTES or stable(before) != stable(after):
            raise ConfigurationError("environment_file_changed_or_oversized")
    try:
        text = contents.decode("utf-8")
    except UnicodeError:
        raise ConfigurationError("environment_file_not_utf8") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > 8192 or "=" not in line:
            raise ConfigurationError("invalid_environment_assignment")
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ConfigurationError("invalid_or_duplicate_environment_name")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0] or "\\" in value:
                raise ConfigurationError("unsupported_environment_quoting")
            value = value[1:-1]
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ConfigurationError("invalid_environment_value")
        values[name] = value
    return values, text, before


def mail_configuration_errors(values: Mapping[str, str], environment: str) -> list[str]:
    """Configuration readiness only: no DNS, provider or delivery claims."""
    errors: list[str] = []
    keys: list[bytes] = []
    for name in (MAIL_KEY, PROVIDER_KEY):
        try:
            keys.append(decode_encryption_key(values.get(name, "").strip() if name == PROVIDER_KEY else values.get(name, ""), name))
        except ConfigurationError:
            errors.append(name + "_invalid_or_missing")
    if len(keys) == 2 and keys[0] == keys[1]:
        errors.append("encryption_keys_must_be_independent")
    backend = (values.get("CHUDWARE_MAIL_BACKEND") or "console").strip().lower()
    if backend not in MAIL_BACKENDS:
        errors.append("mail_backend_invalid")
    elif environment == "prod" and backend == "console":
        errors.append("production_console_mail_refused")
    if backend == "resend" and not values.get("CHUDWARE_RESEND_API_KEY", "").strip():
        errors.append("resend_credential_missing")
    if backend == "smtp":
        host = values.get("CHUDWARE_SMTP_HOST", "").strip()
        if not host or any(char.isspace() or char in "/\\@?#" for char in host):
            errors.append("smtp_host_invalid_or_missing")
        try:
            port = int(values.get("CHUDWARE_SMTP_PORT", "587"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            errors.append("smtp_port_invalid")
        if (values.get("CHUDWARE_SMTP_TLS") or "starttls").strip().lower() not in SMTP_TLS_MODES:
            errors.append("smtp_tls_invalid")
        user = bool(values.get("CHUDWARE_SMTP_USER", "").strip())
        password = bool(values.get("CHUDWARE_SMTP_PASS", ""))
        if user != password or (environment == "prod" and not user):
            errors.append("smtp_credentials_missing_or_partial")
    sender = values.get("CHUDWARE_MAIL_FROM") or DEFAULT_MAIL_FROM
    _, address = parseaddr(sender)
    if address.count("@") != 1 or any(char.isspace() for char in address) or not all(address.split("@")):
        errors.append("mail_sender_invalid")
    origin = urlsplit(values.get("CHUDWARE_WEB_PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE_URL)
    if (origin.scheme not in ({"https"} if environment == "prod" else {"http", "https"})
            or not origin.hostname or origin.username or origin.password
            or origin.query or origin.fragment):
        errors.append("public_origin_invalid")
    return errors
