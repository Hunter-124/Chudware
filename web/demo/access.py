"""Cloudflare Access authentication for /admin.

The only way in is a Cloudflare Access one-time email code: Access (in front of
``chudware.com/admin``) emails a code, and on success Cloudflare signs a JWT and
sends it to the origin as ``Cf-Access-Jwt-Assertion`` (via the Pages proxy).
This module verifies that JWT properly -- RS256 signature against Cloudflare's
published keys for the team, issuer, audience, expiry -- and then checks the
``email`` claim against the single allowed address. The email *header*
(``Cf-Access-Authenticated-User-Email``) is never trusted on its own: anyone
can send that header to an origin they can reach, only Cloudflare can sign.

There is deliberately no password/key login any more. Break-glass when Access
itself is broken: the front door listens on loopback only, so an SSH tunnel to
the host (``ssh -L 8300:127.0.0.1:8300 livingroom-pc``) is accepted as the
operator -- reaching loopback already requires the host's SSH key.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

KEYS_TTL = 600.0


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key_from_jwk(jwk: dict[str, Any]) -> rsa.RSAPublicKey:
    n = int.from_bytes(_unb64url(jwk["n"]), "big")
    e = int.from_bytes(_unb64url(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


class AccessAuth:
    """Verifies Cloudflare Access JWTs for one team domain + one app audience."""

    def __init__(self, team_domain: str, audience: str, admin_email: str, *,
                 keys: dict[str, rsa.RSAPublicKey] | None = None,
                 clock: Callable[[], float] = time.time,
                 http: httpx.AsyncClient | None = None):
        # team_domain like https://example.cloudflareaccess.com (no trailing slash)
        self.team = team_domain.rstrip("/")
        self.audience = audience
        self.admin_email = admin_email.strip().lower()
        self.clock = clock
        self._keys: dict[str, rsa.RSAPublicKey] = dict(keys or {})
        self._keys_at = 0.0
        self._http = http

    @property
    def configured(self) -> bool:
        return bool(self.team and self.audience and self.admin_email)

    async def _refresh_keys(self) -> None:
        client = self._http or httpx.AsyncClient(timeout=10.0, trust_env=False)
        try:
            resp = await client.get(f"{self.team}/cdn-cgi/access/certs")
            resp.raise_for_status()
            jwks = resp.json().get("keys", [])
        finally:
            if self._http is None:
                await client.aclose()
        self._keys = {j["kid"]: public_key_from_jwk(j) for j in jwks if j.get("kid")}
        self._keys_at = self.clock()

    async def verify(self, token: str | None) -> str | None:
        """The authenticated email if ``token`` is a valid Access JWT for the
        admin app and the one allowed user, else None."""
        if not token or not self.configured:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            header = json.loads(_unb64url(parts[0]))
            claims = json.loads(_unb64url(parts[1]))
            signature = _unb64url(parts[2])
        except (ValueError, json.JSONDecodeError):
            return None
        if header.get("alg") != "RS256" or not header.get("kid"):
            return None
        if self.clock() - self._keys_at > KEYS_TTL or header["kid"] not in self._keys:
            try:
                await self._refresh_keys()
            except (httpx.HTTPError, ValueError, KeyError):
                return None
        key = self._keys.get(header["kid"])
        if key is None:
            return None
        try:
            key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"),
                       padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return None
        now = self.clock()
        if claims.get("iss") != self.team or now >= float(claims.get("exp", 0)):
            return None
        if now < float(claims.get("nbf", claims.get("iat", 0)) - 60):
            return None
        aud = claims.get("aud")
        if self.audience not in (aud if isinstance(aud, list) else [aud]):
            return None
        email = str(claims.get("email", "")).strip().lower()
        return email if email and email == self.admin_email else None
