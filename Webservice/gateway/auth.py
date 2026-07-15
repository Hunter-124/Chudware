"""Authentication middleware: CF Access JWT (prod) or X-Dev-User (dev)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import yaml
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

try:
    from .config import Settings, settings
except ImportError:
    from config import Settings, settings

log = logging.getLogger("billet-web.auth")

PUBLIC_PATHS = frozenset({"/healthz"})


@dataclass(frozen=True)
class User:
    email: str
    display: str
    role: str
    heavy: bool
    project: str = ""  # default PLM project opened at session start (/plm/<name>.git)


class UserDirectory:
    """Load + reload users.yaml; map email → User."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._by_email: dict[str, User] = {}
        self.default_project: str = ""  # site-wide default; users without a project inherit it
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            if force:
                self._by_email = {}
                self._mtime = None
                log.warning("users file missing: %s", self.path)
            return
        if not force and self._mtime is not None and st.st_mtime == self._mtime:
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        users = data.get("users") or data if isinstance(data, list) else data.get("users") or []
        if isinstance(data, dict):
            self.default_project = str(data.get("default_project") or "").strip()
        by_email: dict[str, User] = {}
        for row in users:
            if not isinstance(row, dict):
                continue
            email = str(row.get("email", "")).strip().lower()
            if not email:
                continue
            by_email[email] = User(
                email=email,
                display=str(row.get("display") or email.split("@")[0]),
                role=str(row.get("role") or "user"),
                heavy=bool(row.get("heavy", False)),
                project=str(row.get("project") or "").strip(),
            )
        self._by_email = by_email
        self._mtime = st.st_mtime
        log.info("loaded %d users from %s", len(by_email), self.path)

    def get(self, email: str) -> User | None:
        self.reload()
        return self._by_email.get(email.strip().lower())

    def all(self) -> list[User]:
        self.reload()
        return list(self._by_email.values())

    def upsert(self, user: User) -> None:
        """Add or update a user and persist to users.yaml (atomic write)."""
        self.reload()
        self._by_email[user.email.strip().lower()] = user
        self._write()

    def set_default_project(self, project: str) -> None:
        """Site-wide default project (users with no explicit project inherit it)."""
        self.reload()
        self.default_project = project.strip()
        self._write()

    def effective_project(self, user: User) -> str:
        return user.project or self.default_project

    def remove(self, email: str) -> bool:
        self.reload()
        removed = self._by_email.pop(email.strip().lower(), None) is not None
        if removed:
            self._write()
        return removed

    def _write(self) -> None:
        payload = {
            "default_project": self.default_project,
            "users": [
                {
                    "email": u.email,
                    "display": u.display,
                    "role": u.role,
                    "heavy": u.heavy,
                    "project": u.project,
                }
                for u in sorted(self._by_email.values(), key=lambda u: u.email)
            ]
        }
        tmp = self.path.with_suffix(".yaml.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write("# billet-web allowlist — managed via /admin; edits here are fine too.\n")
            fh.write("# After manual edits run: python3 Webservice/deploy/cf_provision.py --sync-users\n")
            yaml.safe_dump(payload, fh, sort_keys=False)
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime
        log.info("wrote %d users to %s", len(self._by_email), self.path)


class JWKSCache:
    """Cached Cloudflare Access JWKS (15 min TTL)."""

    def __init__(self, url: str, ttl_sec: int = 15 * 60) -> None:
        self.url = url
        self.ttl_sec = ttl_sec
        self._fetched_at: float = 0.0
        self._client: jwt.PyJWKClient | None = None

    def _refresh_if_needed(self) -> jwt.PyJWKClient:
        now = time.time()
        if self._client is None or (now - self._fetched_at) > self.ttl_sec:
            self._client = jwt.PyJWKClient(self.url, cache_keys=True, lifespan=self.ttl_sec)
            self._fetched_at = now
        return self._client

    def get_signing_key(self, token: str) -> Any:
        client = self._refresh_if_needed()
        return client.get_signing_key_from_jwt(token)


def not_on_list_html(email: str | None = None) -> str:
    who = f"<code>{_html_escape(email)}</code>" if email else "your account"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Not on the list — billet-web</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #0f1419; color: #e7ecf1;
           display: flex; min-height: 100vh; align-items: center; justify-content: center; margin: 0; }}
    .card {{ max-width: 32rem; padding: 2rem; border-radius: 12px; background: #1a2332;
             border: 1px solid #2a3a4e; box-shadow: 0 8px 32px rgba(0,0,0,.4); }}
    h1 {{ margin: 0 0 .75rem; font-size: 1.35rem; }}
    p {{ line-height: 1.5; color: #b0bcc8; }}
    code {{ background: #0f1419; padding: .1rem .35rem; border-radius: 4px; }}
    a {{ color: #6cb6ff; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>You're not on the list</h1>
    <p>{who} authenticated successfully, but is not authorized for billet-web.</p>
    <p>Ask the operator (Hunter) to add your email to
       <code>Webservice/gateway/users.yaml</code> and re-deploy the Access policy if needed.</p>
    <p>Contact: the host operator for this instance.</p>
  </div>
</body>
</html>
"""


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not header:
        return out
    for part in header.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401, email: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.email = email


class AuthMiddleware:
    """Pure ASGI middleware — covers HTTP and WebSocket scopes."""

    def __init__(
        self,
        app,
        cfg: Settings | None = None,
        directory: UserDirectory | None = None,
        jwks: JWKSCache | None = None,
    ) -> None:
        self.app = app
        self.cfg = cfg or settings
        self.directory = directory or UserDirectory(self.cfg.users_file)
        self._jwks = jwks
        if not self.cfg.dev_mode and self.cfg.cf_team_domain:
            self._jwks = self._jwks or JWKSCache(self.cfg.cf_jwks_url)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if path in PUBLIC_PATHS or path.startswith("/healthz"):
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        cookies = _parse_cookies(headers.get("cookie", ""))

        try:
            user = self._authenticate(headers, cookies)
        except AuthError as exc:
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4401 if exc.status == 401 else 4403,
                    }
                )
                return
            response = self._deny_http(path, headers, exc)
            await response(scope, receive, send)
            return

        if user is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            response = self._deny_http(
                path, headers, AuthError("not authenticated", status=401)
            )
            await response(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["user"] = user
            state["user_email"] = user.email
        else:
            try:
                state.user = user
                state.user_email = user.email
            except Exception:
                scope["state"] = {"user": user, "user_email": user.email}

        await self.app(scope, receive, send)

    def _authenticate(self, headers: dict[str, str], cookies: dict[str, str]) -> User | None:
        if self.cfg.dev_mode:
            email = headers.get("x-dev-user") or cookies.get("dev_user")
            if not email:
                raise AuthError(
                    "dev mode requires X-Dev-User header",
                    status=401,
                    email=None,
                )
            email = email.strip().lower()
            user = self.directory.get(email)
            if user is None:
                raise AuthError("not on the list", status=403, email=email)
            return user

        token = headers.get("cf-access-jwt-assertion")
        if not token:
            token = cookies.get("CF_Authorization")
        if not token:
            raise AuthError("missing Cf-Access-Jwt-Assertion", status=401)

        if not self.cfg.cf_team_domain or not self.cfg.cf_aud:
            raise AuthError("CF Access not configured", status=500)

        if self._jwks is None:
            self._jwks = JWKSCache(self.cfg.cf_jwks_url)

        try:
            signing_key = self._jwks.get_signing_key(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.cfg.cf_aud,
                issuer=self.cfg.cf_issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            log.warning("JWT verification failed: %s", exc)
            raise AuthError("invalid access token", status=401) from exc

        email = (payload.get("email") or "").strip().lower()
        if not email or "@" not in email:
            email = str(payload.get("common_name") or "").strip().lower()
        if not email or "@" not in email:
            raise AuthError("token missing email claim", status=401)

        user = self.directory.get(email)
        if user is None:
            raise AuthError("not on the list", status=403, email=email)
        return user

    def _deny_http(self, path: str, headers: dict[str, str], exc: AuthError):
        accept = headers.get("accept", "")
        wants_html = "text/html" in accept or path == "/" or path.startswith("/static")
        if exc.status == 403 and (exc.email or "not on the list" in exc.message.lower()):
            if wants_html or not path.startswith("/api"):
                return HTMLResponse(not_on_list_html(exc.email), status_code=403)
            return JSONResponse(
                {"error": "not_on_list", "email": exc.email, "detail": exc.message},
                status_code=403,
            )
        if wants_html and not path.startswith("/api"):
            return HTMLResponse(
                f"<!DOCTYPE html><html><body><h1>{exc.status}</h1>"
                f"<p>{_html_escape(exc.message)}</p></body></html>",
                status_code=exc.status,
            )
        return JSONResponse(
            {"error": exc.message, "email": exc.email}, status_code=exc.status
        )


def get_current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise AuthError("not authenticated", status=401)
    return user
