"""PLM Git boundaries: fixed transports, credential-free URLs and isolated Git env.

Only add_remote locally approves a destination; versioned manifests are not
transport authority. SSH usernames in scp-style locations are login identities,
not embedded passwords. HTTP auth belongs in a local credential helper.
"""
from __future__ import annotations

import os
import re
from urllib.parse import parse_qsl, urlsplit

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}\Z")
SECRET_FIELDS = frozenset({"token", "access_token", "bearer", "authorization", "password"})


class LocationCredentialError(ValueError):
    """A credential-bearing location, with a message that never echoes it."""


def reject_location_credentials(value: str) -> None:
    """Keep URL userinfo and authentication query fields out of shared history."""
    if "://" not in value:
        return  # scp-style SSH usernames are login identities, not URL userinfo
    try:
        parsed = urlsplit(value)
    except ValueError:
        # urllib may include the entire authority in its NFKC error message.
        raise ValueError("remote URL is malformed") from None
    if (parsed.username is not None or parsed.password is not None
            or any(key.casefold() in SECRET_FIELDS
                   for key, _ in parse_qsl(parsed.query, keep_blank_values=True))):
        raise LocationCredentialError("credentials must not be stored in remote URLs")


def valid_name(value: str) -> bool:
    return (isinstance(value, str) and bool(_NAME.fullmatch(value))
            and ".." not in value and "//" not in value
            and not value.endswith(("/", ".", ".lock")))


def validate_location(value: str) -> str:
    """Accept local paths, file, HTTPS and SSH only, never helper/option syntax."""
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise ValueError("remote location must be nonempty and not an option")
    if any(ord(c) < 32 for c in value) or "\x7f" in value:
        raise ValueError("remote location contains control characters")
    if "://" not in value:
        # Explicit scp-style SSH locations; reject helper syntax (ext:: etc.).
        if "::" in value:
            raise ValueError("remote helper transports are not allowed")
        if ":" in value and not re.match(r"^[A-Za-z]:[\\/]", value):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[^\s]+", value):
                raise ValueError("use explicit user@host:path SSH syntax or an ssh:// URL")
            host = value.split(":", 1)[0].rsplit("@", 1)[-1]
            if host.startswith("-"):
                raise ValueError("SSH remote host must not be an option")
            if value.split(":", 1)[1].startswith("-"):
                raise ValueError("SSH remote path must not be an option")
        return value
    reject_location_credentials(value)
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "ssh", "file"):
        raise ValueError("remote must use a local path, file://, https:// or ssh://")
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost") or not parsed.path.startswith("/"):
            raise ValueError("file remote must be a local absolute path")
    elif not parsed.hostname or parsed.hostname.startswith("-"):
        raise ValueError("remote URL needs a valid host")
    if parsed.fragment or parsed.query:
        raise ValueError("query strings and fragments must not be stored in Git remote URLs")
    _ = parsed.port  # reject malformed ports before a transport is selected
    return value


def git_env() -> dict[str, str]:
    """No inherited Git config, executable overrides, provider keys or askpass."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")
           if key in os.environ}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"})
    return env


def git_argv(args: list[str]) -> list[str]:
    return ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
            "-c", "core.pager=false", "-c", "diff.external=", "-c", "protocol.allow=never",
            "-c", "protocol.file.allow=always", "-c", "protocol.https.allow=always",
            "-c", "protocol.ssh.allow=always", "-c", "core.sshCommand=ssh -F /dev/null", *args]
