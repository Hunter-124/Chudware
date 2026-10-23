"""Authentication middleware.

Three ways a request can arrive with a proven identity, tried in this order:

1. ``X-Dev-User`` — local development only (``CHUDWARE_WEB_DEV=1``).
2. A **``chudware_sid`` session cookie** minted by ``authn.py`` after a password
   or OAuth sign-in. This is the self-serve path and the one real users take.
3. A **Cloudflare Access JWT**, verified against the team's JWKS. Access is
   scoped to ``/admin``; it is the operator's door, not the front door.

The oauth2-proxy / Microsoft Entra shared-secret front door that used to sit
between 2 and 3 is gone: the gateway authenticates its own users now, and a
second edge IdP was one more trusted party for no remaining benefit.

Authentication answers *who is this*. Authorization — pending / active / denied
/ suspended / trial-expired — is ``accounts.py``'s answer, applied by
``_resolve_user`` regardless of which branch above proved the email.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import jwt
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

try:
    from . import accounts, authn
    from .config import Settings, settings
except ImportError:  # uvicorn app:app from the package dir
    import accounts  # type: ignore
    import authn  # type: ignore
    from config import Settings, settings  # type: ignore

log = logging.getLogger("chudware-web.auth")

# Where a stuck user is told to write. Not derived from a request header: these
# strings end up in pages served to unauthenticated callers.
SUPPORT_EMAIL = "support@chudware.com"

# /healthz is liveness. The Stripe webhook and desktop entitlement refresh cannot
# present a session cookie; each verifies its own signed request before parsing
# or trusting payload fields. /api/auth is the sign-in surface itself.
#
# The three release entries are soft-auth pass-throughs, NOT unguarded routes:
#   base-version  genuinely anonymous (a rate-limited integer CI reads)
#   ci            Bearer CHUDWARE_GATEWAY_RELEASE_TOKEN, enforced in the handler
#                 because a CI runner has no browser session to present
#   artifact      _may_download decides inside the route, so an entitlement
#                 token works exactly as it already does for /api/releases/latest
#
# The marketplace entries are the storefront, and they are public by product
# requirement: a model marketplace that cannot be linked, shared or crawled has
# no discovery. They are soft-auth like the release entries — the middleware
# still attaches a session when one is presented, and every route reads it
# through marketplace_authz.optional_user to personalise (owned, favourited,
# can-review) without ever requiring it.
#
# Scope is deliberately narrow. "/api/marketplace" covers the whole READ subtree
# including /listings, /meta, /sellers, /assets and /handle-check, and each of
# those routes still enforces its own rules: gated bytes require an entitlement,
# evidence assets 404 for everyone here, and every write route inside the subtree
# calls require_user itself and so 401s an anonymous caller regardless of this
# list. A suspended or trial-expired session is also refused: _resolve_user
# raises for a non-active account, the soft-auth path swallows that and leaves
# the caller anonymous, so they may browse but cannot buy or review.
# "/studio" is NOT here — the seller surface requires a session, and
# serving its shell anonymously would render a page whose every request 401s.
#
# "/marketplace-assets" rather than "/portal-assets", and the difference matters.
# The storefront needs its CSS and JS to load for a signed-out visitor, but
# /portal-assets also serves the account dashboard's and the file bucket's
# bundles, which belong to authenticated surfaces. Opening that whole prefix to
# reach two files would be a widening with no upside, so there is a dedicated
# public prefix whose route additionally requires the filename to start with
# "marketplace" — exactly the restriction /auth-assets already applies to
# "login". "/static/base.css" is an exact entry, not a prefix, so it does not
# open /static: it is the shared design-token stylesheet, it contains no data,
# and the storefront cannot render without it.
#
# The three download entries below are the same shape of decision.
#
# "/api/releases/available" is the anonymous download catalogue: platform,
# arch, format, version, size, sha256 and a notes URL for the newest build of
# each tuple. It deliberately carries NO artifact URL and NO signature for any
# caller, so it grants nothing an attacker could not learn by installing the
# product, and the downloads page has to render real versions and sizes before
# anyone signs in. Downloading still requires a session, an entitlement or a
# download token, enforced by _may_download inside the artifact route.
#
# "/install.sh" is the Linux installer script. The command customers are given
# is `curl -fsSL .../install.sh | sh -s -- --token <token>`; the token
# authorises the download, not the fetch of the script, and gating the script
# behind a session would only pipe an HTML sign-in page into a shell. It is a
# static file in the checkout, it contains no secret, and it is a single exact
# path rather than a prefix.
#
# "/brand-assets" is two content-hashed WOFF2 typefaces and nothing else. It
# exists because neither existing public asset prefix can serve them:
# /portal-assets is not public at all, and /auth-assets requires the filename to
# start with "login". base.css — already public — asks for those families by
# name, so without this the anonymous sign-in page and storefront render in a
# system font. The route refuses anything that is not a bare hashed *.woff2
# resolved inside static/fonts/, so opening the prefix does not open /static.
#
# The favicon set and robots.txt are exact paths to fixed files (see
# portal_serve._SITE_FILES). Crawlers fetch them without a session, and Google
# Search shows the generic globe for a host whose icon it cannot crawl.
PUBLIC_PATHS = frozenset(
    {
        "/healthz", "/api/billing/webhook", "/api/entitlement/refresh",
        "/api/security/events", "/api/releases/latest", "/api/auth",
        "/api/releases/base-version", "/api/releases/ci", "/api/releases/artifact",
        "/api/releases/available",
        "/signin", "/signup", "/forgot", "/reset", "/auth-assets",
        "/marketplace", "/api/marketplace", "/marketplace-assets",
        "/static/base.css", "/install.sh", "/brand-assets",
        "/favicon.ico", "/favicon.svg", "/icon-192.png", "/apple-touch-icon.png",
        "/robots.txt",
    }
)


def _is_public_path(path: str) -> bool:
    """Exact match, or a '<public>/' subpath — never a shared string prefix.

    A bare startswith("/healthz") also passes /healthzFOO, silently skipping
    auth on any route that happens to share the prefix. That same '/' boundary
    is what lets the single "/api/auth" entry cover the whole sign-in subtree
    while leaving a hypothetical "/api/authorizations" protected.
    """
    return path in PUBLIC_PATHS or any(
        path.startswith(p + "/") for p in PUBLIC_PATHS
    )


@dataclass(frozen=True)
class User:
    email: str
    display: str
    role: str
    heavy: bool


class JWKSCache:
    """Cached JWKS (15 min TTL).

    Used twice: for Cloudflare Access's team keys below, and for Google's OIDC
    keys in ``oauth.py``. Same shape, same refresh policy, one implementation.
    """

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


# ---------------------------------------------------------------------------
# The "you are signed in but cannot use the service" page
# ---------------------------------------------------------------------------

# One entry per non-active state, each carrying the ONE action that actually
# unblocks that user. The page this replaces told everybody to email the
# operator to be added to users.yaml, which stopped being true the moment
# registration opened — and was never true for a suspended or expired account.
_STATUS_COPY: dict[str, tuple[str, str]] = {
    "pending": (
        "Confirm your email address",
        "<p>Use the button below to request a fresh verification link for {who}."
        " Open the link we email you to continue. Your trial starts when you verify.</p>"
        '<p><button type="button" data-resend-verification data-email="{email}">'
        "Send verification email</button></p>"
        '<p id="resend-verification-status" role="status" aria-live="polite"></p>'
        '<script src="/auth-assets/login-pending.650a485c0a6c.js" defer></script>',
    ),
    "denied": (
        "This account was declined",
        "<p>{who} was reviewed and not approved for Chudware.</p>"
        '<p>If you think that is a mistake, write to <a href="mailto:{support}">'
        "{support}</a> and we will take another look.</p>",
    ),
    "suspended": (
        "This account is suspended",
        "<p>Access for {who} is suspended, so nothing here will load.</p>"
        '<p>Contact <a href="mailto:{support}">{support}</a> to sort it out —'
        " your files and projects are untouched.</p>",
    ),
    "trial_expired": (
        "Your trial has ended",
        "<p>The free trial for {who} is over. Your work is safe and stays"
        " exactly where you left it.</p>"
        '<p><a href="/#pricing">Pick a plan</a> to pick up where you stopped.</p>',
    ),
}

_STATUS_FALLBACK = (
    "This account cannot sign in",
    "<p>{who} is not currently authorized for Chudware.</p>"
    '<p>Contact <a href="mailto:{support}">{support}</a> for help.</p>',
)


def status_page_html(email: str | None, status: str | None) -> str:
    """The page a signed-in-but-unauthorized caller sees, keyed on WHY."""
    escaped_email = _html_escape(email or "")
    who = f"<code>{escaped_email}</code>" if email else "your account"
    heading, body = _STATUS_COPY.get(status or "", _STATUS_FALLBACK)
    return authn.page_html(
        heading,
        heading,
        f"    {body.format(email=escaped_email, who=who, support=SUPPORT_EMAIL)}",
    )


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
    def __init__(
        self,
        message: str,
        status: int = 401,
        email: str | None = None,
        account_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.email = email
        # pending / denied / suspended / trial_expired — selects the page copy.
        self.account_status = account_status


class AuthMiddleware:
    """Pure ASGI middleware — covers HTTP and WebSocket scopes."""

    def __init__(
        self,
        app,
        cfg: Settings | None = None,
        jwks: JWKSCache | None = None,
    ) -> None:
        self.app = app
        self.cfg = cfg or settings
        # Fail fast on a misconfigured production deploy: with neither a
        # self-serve session secret nor Cloudflare Access there is NO strong
        # auth path at all — refuse to construct rather than run silently
        # broken. session_secret alone is enough: a self-serve deployment
        # authenticates its own users and needs no edge IdP.
        if (
            not self.cfg.dev_mode
            and not self.cfg.session_secret
            and not (self.cfg.cf_team_domain and self.cfg.cf_aud)
        ):
            raise RuntimeError(
                "no strong auth configured: set CHUDWARE_WEB_SESSION_SECRET "
                "(self-serve sign-in) or CHUDWARE_WEB_CF_TEAM_DOMAIN + "
                "CHUDWARE_WEB_CF_AUD (Cloudflare Access); "
                "CHUDWARE_WEB_DEV=1 is for local development only"
            )
        self._jwks = jwks
        if not self.cfg.dev_mode and self.cfg.cf_team_domain:
            self._jwks = self._jwks or JWKSCache(self.cfg.cf_jwks_url)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        cookies = _parse_cookies(headers.get("cookie", ""))
        query: dict[str, str] = {}
        raw_qs = scope.get("query_string") or b""
        if raw_qs:
            from urllib.parse import parse_qs
            for k, vals in parse_qs(raw_qs.decode("latin-1"), keep_blank_values=False).items():
                if vals:
                    query[k.lower()] = vals[0]

        if _is_public_path(path):
            # Soft-auth: valid credentials riding along attach the user (so
            # /healthz can serve its detailed body to admins), but anonymous
            # callers are never denied on a public path.
            try:
                pub_user, _ = self._authenticate(headers, cookies, query)
            except AuthError:
                pub_user = None
            if pub_user is not None:
                self._attach_user(scope, pub_user)
            await self.app(scope, receive, send)
            return

        set_dev_cookie: str | None = None
        try:
            workspace_surface = (
                path in {"/", "/api/me", "/api/portal/account", "/bucket", "/app", "/engine"}
                or path.startswith(("/workspace/", "/api/bucket/", "/api/orgs",
                                    "/app/", "/portal-assets/", "/files/aicm"))
            )
            user, set_dev_cookie = self._authenticate(
                headers, cookies, query, allow_university=workspace_surface
            )
        except AuthError as exc:
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4401 if exc.status == 401 else 4403,
                    }
                )
                return
            response = self._deny_http(
                path,
                headers,
                exc,
                method=str(scope.get("method") or "GET").upper(),
                next_path=path + (f"?{raw_qs.decode('latin-1')}" if raw_qs else ""),
            )
            await response(scope, receive, send)
            return

        if user is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            response = self._deny_http(
                path,
                headers,
                AuthError("not authenticated", status=401),
                method=str(scope.get("method") or "GET").upper(),
                next_path=path + (f"?{raw_qs.decode('latin-1')}" if raw_qs else ""),
            )
            await response(scope, receive, send)
            return

        self._attach_user(scope, user)

        if set_dev_cookie and scope["type"] == "http":
            # SameSite=Strict: the dev cookie must never ride a cross-site
            # request. Secure everywhere EXCEPT plain-http localhost, where a
            # Secure cookie would never be stored and local dev would break.
            host = headers.get("host", "").split(":", 1)[0].strip("[]").lower()
            local_http = scope.get("scheme") == "http" and host in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
            secure = "" if local_http else "; Secure"
            cookie_val = (
                f"dev_user={set_dev_cookie}; Path=/; HttpOnly; SameSite=Strict{secure}"
            )
            cookie_header = (b"set-cookie", cookie_val.encode("latin-1"))

            async def send_with_cookie(message):
                if message["type"] == "http.response.start":
                    headers_list = list(message.get("headers") or [])
                    headers_list.append(cookie_header)
                    message = {**message, "headers": headers_list}
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _attach_user(scope, user: User) -> None:
        """Populate scope state — the ONLY source of request identity downstream."""
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

    @staticmethod
    def _university_access(email: str) -> bool:
        try:
            from .orgs import university_eligible
        except ImportError:
            from orgs import university_eligible
        return university_eligible(email)

    def _resolve_user(self, email: str, *, allow_university: bool = False) -> User:
        """Map a verified email to an authorized User, or raise 403.

        Authorization lives in the account store (``accounts.py``): an identity
        the store has never seen is REGISTERED as ``pending`` rather than
        rejected outright, so someone arriving through Cloudflare Access lands
        in a state the operator can act on instead of a dead end. Only
        ``status == "active"`` yields a session, and an active account whose
        trial window has closed does not get one either.
        """
        email = email.strip().lower()
        account = accounts.ensure_account(email, display=email.split("@")[0])
        if not accounts.is_authorized(account):
            raise AuthError(
                "account not approved",
                status=403,
                email=email,
                account_status=account.status,
            )
        # The trial is the one entitlement whose expiry the status column does
        # not encode. Checked here so every entry path enforces it identically.
        if account.plan == "trial" and authn.trial_expired(email) and not (allow_university and self._university_access(email)):
            raise AuthError(
                "trial expired",
                status=403,
                email=email,
                account_status="trial_expired",
            )
        return User(
            email=account.email,
            display=account.display or email.split("@")[0],
            # Role and heavy come from the store, never from a self-asserted
            # claim — a self-registered account can never be admin.
            role=accounts.effective_role(account),
            heavy=account.heavy,
        )


    def _authenticate(
        self,
        headers: dict[str, str],
        cookies: dict[str, str],
        query: dict[str, str] | None = None,
        *, allow_university: bool = False,
    ) -> tuple[User | None, str | None]:
        """Return (user, set_dev_cookie_email).

        set_dev_cookie_email is non-None only when auth succeeded via the
        ``?dev_user=`` / ``?x-dev-user=`` query param in dev mode — the caller
        then Set-Cookies it so subresource requests stick without the query.
        """
        if self.cfg.dev_mode:
            # Header preferred; cookie for sticky browser sessions; query for
            # one-shot local smoke (CHUDWARE_WEB_DEV=1 only).
            from_query = False
            email = headers.get("x-dev-user") or cookies.get("dev_user")
            if not email:
                email = (query or {}).get("dev_user") or (query or {}).get("x-dev-user")
                if email:
                    from_query = True
            if not email:
                raise AuthError(
                    "dev mode requires X-Dev-User header",
                    status=401,
                    email=None,
                )
            email = email.strip().lower()
            user = self._resolve_user(email, allow_university=allow_university)
            return user, (email if from_query else None)

        # The account store is the authority on identity and role, so the
        # self-serve session cookie is the front door and is tried FIRST — even
        # when a Cloudflare Access assertion rides along. Preferring the edge
        # assertion instead meant any browser that had ever completed Access
        # for the zone carried a CF_Authorization cookie that shadowed its live
        # session on EVERY request, and was denied outright once that cookie
        # expired or stopped matching cf_aud. Access remains a fallback
        # credential for callers with no gateway session (and matches
        # revalidate_websocket_user, which has always preferred the session).
        sid = cookies.get(authn.SESSION_COOKIE, "")
        if sid:
            session_email = authn.lookup_session(sid)
            if session_email:
                try:
                    return self._resolve_user(session_email, allow_university=allow_university), None
                except AuthError as exc:
                    # This is the whole reason sessions are rows rather than
                    # signed cookies: a denied or suspended decision has to END
                    # them, not merely be re-checked beside them. Kill every
                    # session the account owns so the decision sticks.
                    if exc.account_status in {"denied", "suspended"}:
                        killed = authn.revoke_sessions(session_email)
                        if killed:
                            log.info(
                                "auth: revoked %d session(s) for %s account %s",
                                killed,
                                exc.account_status,
                                session_email,
                            )
                    raise

        # Cloudflare Access is an OPTIONAL second credential, so decide whether
        # it is even configured before looking at what the caller sent. With no
        # team domain and no audience there is nothing to verify a token
        # against, and the caller is simply not signed in.
        #
        # The order matters because a `CF_Authorization` cookie outlives the
        # Access application that minted it — browsers kept presenting ours for
        # weeks after that app was deleted. Testing the token first meant such a
        # cookie reached a "CF Access not configured" branch that answered 500,
        # letting a stale client-side cookie turn every page of a self-serve
        # deployment into a server error.
        if not (self.cfg.cf_team_domain and self.cfg.cf_aud):
            raise AuthError("not signed in", status=401)

        token = headers.get("cf-access-jwt-assertion") or cookies.get("CF_Authorization")
        if not token:
            raise AuthError("missing Cf-Access-Jwt-Assertion", status=401)

        if self._jwks is None:
            self._jwks = JWKSCache(self.cfg.cf_jwks_url)

        try:
            # JWKS lookup parses only the header. Reject malformed payloads and
            # missing mandatory claims first, before fetching a signing key.
            # Identity still comes exclusively from the verified decode below.
            jwt.decode(token, options={
                "verify_signature": False, "require": ["exp", "iss", "aud"],
            })
            signing_key = self._jwks.get_signing_key(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.cfg.cf_aud,
                issuer=self.cfg.cf_issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except (jwt.PyJWTError, RecursionError) as exc:
            log.warning("JWT verification failed: %s", exc)
            raise AuthError("invalid access token", status=401) from exc

        email = (payload.get("email") or "").strip().lower()
        if not email or "@" not in email:
            email = str(payload.get("common_name") or "").strip().lower()
        if not email or "@" not in email:
            raise AuthError("token missing email claim", status=401)

        user = self._resolve_user(email, allow_university=allow_university)
        return user, None

    def _deny_http(
        self,
        path: str,
        headers: dict[str, str],
        exc: AuthError,
        *,
        method: str,
        next_path: str,
    ):
        wants_html = "text/html" in headers.get("accept", "")
        if exc.status == 401 and wants_html and method == "GET" and not path.startswith("/api"):
            return RedirectResponse(
                "/signin?next=" + quote(next_path, safe=""),
                status_code=303,
            )
        if exc.status == 403 and exc.account_status:
            if wants_html or not path.startswith("/api"):
                return HTMLResponse(
                    status_page_html(exc.email, exc.account_status), status_code=403
                )
            return JSONResponse(
                {
                    "error": "not_authorized",
                    "account_status": exc.account_status,
                    "email": exc.email,
                    "detail": exc.message,
                },
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
def revalidate_websocket_user(
    scope: dict[str, Any], *, required_role: str | None = None, require_cloud: bool = False
) -> User:
    """Re-read the credential and account behind a live WebSocket frame.

    Middleware authentication is only a handshake snapshot.  This helper binds
    every later decision to the original credential and current account row so
    revocation, suspension, role changes, and entitlement expiry take effect on
    the next inbound frame.
    """
    state = scope.get("state") or {}
    user = state.get("user") if isinstance(state, dict) else getattr(state, "user", None)
    if user is None:
        raise AuthError("not authenticated", status=401)

    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers") or []
    }
    cookies = _parse_cookies(headers.get("cookie", ""))
    if settings.dev_mode:
        principal = (headers.get("x-dev-user") or cookies.get("dev_user") or "").strip().lower()
        if not principal or principal != user.email:
            raise AuthError("invalid session", status=401)
    else:
        sid = cookies.get(authn.SESSION_COOKIE, "")
        if sid:
            principal = authn.lookup_session(sid)
            if principal is None or principal.strip().lower() != user.email:
                raise AuthError("invalid session", status=401)
        else:
            # Access JWTs are authoritative credentials too; re-run signature,
            # expiry, and principal checks rather than trusting handshake state.
            principal_user, _ = AuthMiddleware(lambda *_: None)._authenticate(
                headers, cookies, allow_university=require_cloud
            )
            if principal_user is None or principal_user.email != user.email:
                raise AuthError("invalid session", status=401)

    account = accounts.get_account(user.email)
    if not accounts.is_authorized(account):
        raise AuthError("account not approved", status=403, email=user.email)
    if account.plan == "trial" and authn.trial_expired(user.email) and not (require_cloud and AuthMiddleware._university_access(user.email)):
        raise AuthError("trial expired", status=403, email=user.email)
    current = User(
        email=account.email,
        display=account.display or account.email.split("@")[0],
        role=accounts.effective_role(account),
        heavy=account.heavy,
    )
    if required_role is not None and current.role != required_role:
        raise AuthError("role no longer authorized", status=403, email=current.email)
    if require_cloud:
        try:
            from .plans import limits_for
        except ImportError:
            from plans import limits_for  # type: ignore
        try:
            cloud = bool(limits_for(account.plan).cloud)
        except ValueError:
            cloud = False
        if not cloud and not AuthMiddleware._university_access(current.email):
            raise AuthError("plan no longer authorized", status=403, email=current.email)
    return current


def get_current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise AuthError("not authenticated", status=401)
    return user
