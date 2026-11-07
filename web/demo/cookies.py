"""Signed anonymous session handle.

The cookie carries only a random session id and its MAC. There is no account behind
it: the id names one queue entry / container, and the server holds all state. A
tampered or foreign value fails verification and is treated as "no session".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

# __Host- makes browsers insist on Secure, Path=/ and no Domain attribute, so a
# sibling subdomain cannot plant or shadow it.
COOKIE_NAME = "__Host-cw_demo"
SID_RE = re.compile(r"[0-9a-f]{24}\Z")
_MAC_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DOMAIN = b"chudware-demo-session-v1\0"


def new_sid() -> str:
    return secrets.token_hex(12)


def _mac(key: bytes, sid: str) -> str:
    digest = hmac.digest(key, _DOMAIN + sid.encode("ascii"), hashlib.sha256)
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sign(key: bytes, sid: str) -> str:
    if not SID_RE.fullmatch(sid):
        raise ValueError("malformed session id")
    return f"{sid}.{_mac(key, sid)}"


def verify(key: bytes, value: str | None) -> str | None:
    """The session id inside a genuine cookie value, else None."""
    if not value or len(value) > 128:
        return None
    sid, dot, mac = value.partition(".")
    if not dot or not SID_RE.fullmatch(sid) or not _MAC_RE.fullmatch(mac):
        return None
    if not hmac.compare_digest(mac.encode("ascii"), _mac(key, sid).encode("ascii")):
        return None
    return sid
