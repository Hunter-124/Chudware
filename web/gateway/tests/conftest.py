"""Test database: a real PostgreSQL, never a fake.

There is deliberately no SQLite fallback and no stub cursor. A fake would let a
``%s``/``?`` placeholder slip, an ``ON CONFLICT`` clause go untested, or a
transaction-semantics bug pass green here and fail only in production — which
is precisely the class of bug the port exists to avoid.

By default the session spins up its own throwaway cluster with ``initdb`` into
a temporary directory, listening on a unix socket inside that directory: no
TCP port to collide with a parallel CI job, no daemon to install, no root. Set
``CHUDWARE_TEST_DB_URL`` to point at an existing server instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Fixture-only queue key, configured before any gateway Settings import.
os.environ["CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))




@pytest.fixture(autouse=True)
def protected_admission_state(tmp_path, monkeypatch):
    """Direct module tests must never read/write a real deployment lease."""
    import deployment_drain
    import gateway.deployment_drain
    root = tmp_path / "gateway-admission"
    root.mkdir(mode=0o700)
    for module in (deployment_drain, gateway.deployment_drain):
        monkeypatch.setattr(module, "_controller", module.AdmissionController(root))


@pytest.fixture
def unix_socket_path() -> Iterator[Path]:
    """Keep AF_UNIX addresses short even when CI's TMPDIR is deeply nested."""
    with tempfile.TemporaryDirectory(prefix="cw-webd-", dir="/tmp") as root:
        yield Path(root) / "webd.sock"


@pytest.fixture(scope="session", autouse=True)
def entitlement_signing_key(tmp_path_factory) -> Iterator[Path]:
    """A throwaway Ed25519 entitlement key, and the matching embedded public key.

    ``app.py``'s startup hook calls ``entitlement._load_private_key()``, which
    refuses to boot unless a 32-byte 0600 key file exists AND its public half
    equals the compiled-in ``entitlement.EMBEDDED_PUBLIC_KEY``. The real private
    key exists only on the production host, which is the entire point of an
    offline signing root and is not something to weaken in the product.

    Without this fixture, 125 of the suite's tests ERROR -- every test that builds
    the FastAPI app, which is most of test_orgs, test_tenant_isolation,
    test_gateway, test_headers_csrf and test_admin_hardening. They failed
    identically on master before the marketplace work; the suite has simply not
    been runnable off the gateway host.

    Generating a key and pointing the embedded public key at it is deliberately
    the TEST's job, not a dev branch inside ``entitlement.py``: a production code
    path that accepts a self-signed entitlement root is a real risk, and a
    session-scoped monkeypatch is none.

    Autouse and session-scoped: the requirement is a property of importing the
    app, not of any one test, and opting in per test would leave the next new
    test file erroring for a reason that has nothing to do with it.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    key_path = tmp_path_factory.mktemp("entitlement", numbered=False) / "signing.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    # _load_private_key refuses a key readable by group or other.
    key_path.chmod(0o600)

    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    # Patch EVERY importable spelling of the module, not just one.
    #
    # This conftest puts BOTH the gateway directory and its parent on sys.path,
    # so `entitlement` and `gateway.entitlement` resolve to two DISTINCT module
    # objects with independent globals. `app.py` tries a relative import first
    # and falls back to the bare one, so which object ends up holding the
    # constant that `_load_private_key` reads depends on how the app happened to
    # be imported. Patching one and hoping produced exactly the confusing symptom
    # you would expect: the key file was found, read and parsed, and the run
    # still died on "entitlement private key does not match the shipped public
    # key" because the comparison was against the other module's copy.
    #
    # Both spellings are imported eagerly here so both exist in sys.modules
    # before any test runs; every later import is then a cache hit on an
    # already-patched object. The sys.modules sweep afterwards catches any third
    # spelling a future sys.path entry might introduce.
    import entitlement  # noqa: F401 - imported for the side effect of caching it

    try:
        import gateway.entitlement  # noqa: F401 - the package spelling
    except ImportError:
        pass

    targets = [
        module
        for name, module in list(sys.modules.items())
        if module is not None
        and (name == "entitlement" or name.endswith(".entitlement"))
        and hasattr(module, "EMBEDDED_PUBLIC_KEY")
    ]
    assert targets, "no entitlement module was importable; conftest sys.path setup changed"

    originals = [(module, module.EMBEDDED_PUBLIC_KEY) for module in targets]
    original_env = os.environ.get("CHUDWARE_ENTITLEMENT_KEY_PATH")
    for module in targets:
        module.EMBEDDED_PUBLIC_KEY = public
    os.environ["CHUDWARE_ENTITLEMENT_KEY_PATH"] = str(key_path)
    try:
        yield key_path
    finally:
        for module, value in originals:
            module.EMBEDDED_PUBLIC_KEY = value
        if original_env is None:
            os.environ.pop("CHUDWARE_ENTITLEMENT_KEY_PATH", None)
        else:
            os.environ["CHUDWARE_ENTITLEMENT_KEY_PATH"] = original_env


@pytest.fixture()
def reload_settings(monkeypatch):
    """Re-read ``Settings`` from the current environment WITHOUT replacing the
    singleton, and undo it after the test.

    ``auth``, ``accounts``, ``limits`` and friends all do ``from config import
    settings`` at import time, so they hold a REFERENCE to one object. Fixtures
    that instead did ``config.settings = config.load_settings()`` left whichever
    module imported first reading the discarded object — and which module that is
    depends purely on pytest's file order. Every symptom of that has been an
    order-dependent result rather than an honest one: ``test_auth_accounts_gate``
    failing "no strong auth configured" only when ``test_accounts`` ran first, and
    ``test_chat``'s WS broadcast blocking forever because ``AuthMiddleware`` hit
    that same fail-fast inside the message loop and no frame was ever sent.

    Mutating field-by-field through ``monkeypatch`` keeps one object for everyone
    and restores each value at teardown, so a later module is never handed a
    settings object shaped by an earlier one.
    """
    import dataclasses  # noqa: PLC0415

    def _reload():
        import config  # noqa: PLC0415

        fresh = config.load_settings()
        for field in dataclasses.fields(fresh):
            monkeypatch.setattr(
                config.settings, field.name, getattr(fresh, field.name), raising=False
            )
        return config.settings

    return _reload


# Fedora installs the server binaries in /usr/bin, but only from
# postgresql-server (the client package gives psql and no initdb); versioned
# side-by-side installs land under /usr/pgsql-NN/bin or
# /usr/lib64/pgsql/postgresql-NN/bin; Homebrew uses the two brew prefixes.
_BIN_GLOBS = (
    "/usr/pgsql-*/bin",
    "/usr/lib64/pgsql/postgresql-*/bin",
    "/opt/homebrew/opt/postgresql@*/bin",
    "/usr/local/opt/postgresql@*/bin",
)
_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

_INSTALL_HINT = """PostgreSQL server binaries not found: the gateway test suite runs against a
real database and needs `initdb` and `postgres`, not just `psql`.

  Fedora/RHEL : sudo dnf install postgresql16-server   (the plain `postgresql`
                package is client-only and does NOT ship initdb)
  Debian      : sudo apt install postgresql
  macOS       : brew install postgresql@16

Or point the suite at a server you already have:

  CHUDWARE_TEST_DB_URL=postgresql://user:pass@127.0.0.1:5432/scratch pytest tests

Or name the directory holding the binaries with CHUDWARE_TEST_PG_BIN."""


def _pg_bin_dir() -> Path:
    """Directory containing initdb + postgres, searched widest-net first."""
    explicit = (os.environ.get("CHUDWARE_TEST_PG_BIN") or "").strip()
    if explicit:
        d = Path(explicit)
        if (d / "initdb").exists():
            return d
        raise RuntimeError(f"CHUDWARE_TEST_PG_BIN={explicit} has no initdb\n\n{_INSTALL_HINT}")

    found = shutil.which("initdb")
    if found and (Path(found).parent / "postgres").exists():
        return Path(found).parent

    candidates: list[Path] = []
    for pattern in _BIN_GLOBS:
        candidates.extend(Path("/").glob(pattern.lstrip("/")))
    candidates.extend(Path(d) for d in _BIN_DIRS)
    # Newest version first: "postgresql@16" sorts after "postgresql@9".
    for d in sorted(candidates, reverse=True):
        if (d / "initdb").exists() and (d / "postgres").exists():
            return d
    raise RuntimeError(_INSTALL_HINT)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str]:
    """A PostgreSQL instance for the whole session. Yields a base DSN."""
    external = (os.environ.get("CHUDWARE_TEST_DB_URL") or "").strip()
    if external:
        yield external
        return

    bindir = _pg_bin_dir()
    # Short path: the unix socket path has a hard ~107 byte limit, and pytest's
    # tmp_path_factory roots are long enough to blow it.
    root = Path(tempfile.mkdtemp(prefix="chwpg-"))
    datadir = root / "d"
    subprocess.run(
        [
            str(bindir / "initdb"),
            "-D", str(datadir),
            "-U", "postgres",
            "-A", "trust",           # socket-only, inside a 0700 temp dir
            "-E", "UTF8",
            "--locale=C",
            "--no-sync",             # throwaway cluster; durability is noise
        ],
        check=True,
        capture_output=True,
    )
    logfile = root / "postgres.log"
    subprocess.run(
        [
            str(bindir / "pg_ctl"),
            "-D", str(datadir),
            "-l", str(logfile),
            "-w",
            "-o",
            # No TCP listener at all: nothing to collide with a parallel job.
            f"-k {root} -h '' -F -c full_page_writes=off -c synchronous_commit=off",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://postgres@/postgres?host={root}"
    finally:
        subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"],
            capture_output=True,
        )
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def pg(pg_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A virgin schema with every migration applied. Yields the DSN.

    Autouse, deliberately. Half the suite reaches the store indirectly — an
    admin route that renders a pending count, an auth middleware that calls
    ensure_account — and requiring each of those to remember an opt-in fixture
    would leave tests silently pointed at whatever CHUDWARE_DB_URL happens to
    be set on the machine. Which, on the production host, is production.

    Resetting the schema rather than creating a database per test keeps this at
    a few milliseconds; the migrations are small and the isolation is identical
    because nothing in this schema lives outside ``public``.
    """
    import psycopg

    import db  # noqa: PLC0415 - after sys.path is set up above

    monkeypatch.setenv("CHUDWARE_DB_URL", pg_server)
    db.close_pool()
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    db.migrate()
    try:
        yield pg_server
    finally:
        # psycopg's pool runs worker threads; leaving them alive across the
        # session trips PythonFinalizationError at interpreter exit.
        db.close_pool()


# --- Marketplace fixtures ----------------------------------------------------
#
# The marketplace needs more setup than any other feature here, for two reasons
# that are both deliberate product decisions rather than accidents:
#
#   * every route 404s unless `settings.marketplace_enabled`, so a test that
#     forgets to switch it on sees "not found" and looks like a routing bug;
#   * `config.Settings.__post_init__` treats Stripe as all-or-nothing, so
#     setting the secret key alone raises ValueError at import. The full price
#     and webhook set has to be present together.
#
# `marketplace_env` handles both, so a marketplace test opts in with one fixture
# instead of six monkeypatch lines it can get subtly wrong.

# Deliberately not real Stripe ids. Anything that would actually call Stripe
# must use `fake_stripe`; a test that reaches api.stripe.com is a test that fails
# in CI on a network blip and silently costs sandbox state.
_TEST_STRIPE_ENV = {
    "CHUDWARE_WEB_STRIPE_SECRET_KEY": "sk_test_conftest_not_a_real_key",
    "CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET": "whsec_conftest_not_a_real_secret",
    "CHUDWARE_STRIPE_PRICE_DESKTOP": "price_test_desktop",
    "CHUDWARE_STRIPE_PRICE_DESKTOP_EDU": "price_test_desktop_edu",
    "CHUDWARE_STRIPE_PRICE_STUDIO": "price_test_studio",
    "CHUDWARE_STRIPE_PRICE_STUDIO_EDU": "price_test_studio_edu",
}


@pytest.fixture()
def marketplace_env(monkeypatch, tmp_path, reload_settings):
    """Enable the marketplace with isolated storage and a complete Stripe set.

    Storage is redirected into ``tmp_path``. That matters more than it looks:
    ``settings.marketplace_root`` defaults under ``data_root``, and
    ``workspace_root``/``bucket_root`` default into a real project directory on a
    developer's machine. A test that uploads an asset without this fixture writes
    into whatever those resolve to.

    Returns the live settings object, already reloaded.
    """
    monkeypatch.setenv("CHUDWARE_WEB_DEV", "1")
    monkeypatch.setenv("CHUDWARE_MARKETPLACE_ENABLED", "1")
    monkeypatch.setenv("CHUDWARE_WEB_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("CHUDWARE_MARKETPLACE_ROOT", str(tmp_path / "marketplace"))
    # NOT CHUDWARE_WEB_WORKSPACE_ROOT: config.py reads this name, without the
    # _WEB_ infix, and its default is a real project tree.
    monkeypatch.setenv("CHUDWARE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("CHUDWARE_WEB_BUCKET_ROOT", str(tmp_path / "bucket"))
    monkeypatch.setenv("CHUDWARE_WEB_SESSION_SECRET", "conftest-session-secret")
    for name, value in _TEST_STRIPE_ENV.items():
        monkeypatch.setenv(name, value)
    settings = reload_settings()
    assert settings.marketplace_enabled, "marketplace_env failed to enable the marketplace"
    assert str(settings.marketplace_root).startswith(str(tmp_path)), (
        "marketplace storage escaped tmp_path"
    )
    return settings


@pytest.fixture()
def activate():
    """Make an account exist, be active, and hold a plan that permits everything.

    ``accounts.ensure_account`` creates rows as ``pending``/``trial``, and
    ``auth._resolve_user`` raises for a non-active account AND for an expired
    trial. A test that only calls ``ensure_account`` gets a caller the middleware
    treats as anonymous, which on a PUBLIC marketplace path means the request
    succeeds while silently unauthenticated -- the most confusing possible
    failure. Setting the plan to ``studio`` and clearing ``trial_ends_at``
    removes both traps.
    """
    import accounts
    from db import tx

    def _activate(email: str, role: str = "user") -> str:
        accounts.ensure_account(email, display=email.split("@")[0])
        accounts.set_status(email, "active", decided_by="conftest")
        if role == "admin":
            accounts.set_role(email, "admin")
        with tx() as cur:
            cur.execute(
                "UPDATE accounts SET plan='studio', trial_ends_at=NULL WHERE email=%s",
                (email.strip().lower(),),
            )
        return email

    return _activate


@pytest.fixture()
def grant_credits():
    """Put spendable credits on an account, for the credit-purchase paths."""
    import credits

    def _grant(email: str, amount: int) -> None:
        credits.grant(email, amount, note="conftest")

    return _grant


@pytest.fixture()
def as_user():
    """Dev-mode identity headers. Requires ``settings.dev_mode`` (marketplace_env)."""

    def _headers(email: str | None) -> dict[str, str]:
        return {"X-Dev-User": email} if email else {}

    return _headers


@pytest.fixture()
def fake_stripe(monkeypatch):
    """Install a fake ``stripe`` module in ``sys.modules``.

    Every marketplace module imports stripe LAZILY inside a helper, so replacing
    the ``sys.modules`` entry is enough and no import-order care is needed.

    Returns the fake so a test can assert on ``fake.calls`` -- a list of
    ``(method, kwargs)`` -- which is how the money tests check that an
    ``idempotency_key`` was passed and that an ``application_fee_amount`` matched
    the fee our own splitter computed. Asserting on recorded calls is the only
    way to test those without charging a card.
    """
    import types

    calls: list[tuple[str, dict]] = []

    def record(name, result=None):
        # *args as well as **kwargs: the SDK is called positionally in places
        # (`stripe.Account.retrieve(account_id)`), and a kwargs-only recorder
        # raises TypeError there. refresh_connect swallows that into a 502
        # "stripe_error", so a working Connect path looked like a Stripe outage
        # and every test had to stub around it.
        def _call(*args, **kwargs):
            calls.append((name, {**kwargs, "_args": list(args)} if args else kwargs))
            value = result(*args, **kwargs) if callable(result) else result
            return {} if value is None else value
        return _call

    fake = types.SimpleNamespace()
    fake.calls = calls
    fake.VERSION = "15.5.0"

    # Stripe's exception hierarchy: modules catch these by name.
    class StripeError(Exception):
        def __init__(self, message="stripe error", code=None):
            super().__init__(message)
            self.code = code

    fake.error = types.SimpleNamespace(
        StripeError=StripeError,
        CardError=type("CardError", (StripeError,), {}),
        SignatureVerificationError=type("SignatureVerificationError", (StripeError,), {}),
        InvalidRequestError=type("InvalidRequestError", (StripeError,), {}),
    )
    fake.StripeError = StripeError

    fake.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(
            create=record("checkout.Session.create", lambda *a, **kw: {
                "id": "cs_test_fake",
                "url": "https://checkout.stripe.com/c/pay/cs_test_fake",
                "payment_intent": "pi_test_fake",
                "amount_total": (kw.get("line_items") or [{}])[0]
                    .get("price_data", {})
                    .get("unit_amount"),
                "currency": "usd",
                "payment_status": "unpaid",
                "status": "open",
            }),
            retrieve=record("checkout.Session.retrieve", {"id": "cs_test_fake"}),
            list_line_items=record("checkout.Session.list_line_items", {"data": []}),
        )
    )
    fake.Account = types.SimpleNamespace(
        retrieve=record("Account.retrieve", {
            "id": "acct_test_fake",
            "charges_enabled": False,
            "payouts_enabled": False,
            "details_submitted": False,
            "capabilities": {"transfers": "inactive"},
        }),
        create=record("Account.create", {"id": "acct_test_fake"}),
    )
    fake.Balance = types.SimpleNamespace(
        retrieve=record("Balance.retrieve", {"livemode": False})
    )
    fake.Refund = types.SimpleNamespace(
        create=record("Refund.create", {"id": "re_test_fake", "status": "succeeded"})
    )
    fake.Transfer = types.SimpleNamespace(
        create=record("Transfer.create", {"id": "tr_test_fake"})
    )
    fake.Invoice = types.SimpleNamespace(list=record("Invoice.list", {"data": []}))
    fake.Price = types.SimpleNamespace(
        retrieve=record("Price.retrieve", {"id": "price_test", "unit_amount": 0}),
        list=record("Price.list", {"data": []}),
    )
    fake.Product = types.SimpleNamespace(list=record("Product.list", {"data": []}))
    fake.Subscription = types.SimpleNamespace(
        modify=record("Subscription.modify", {"id": "sub_test"}),
        retrieve=record("Subscription.retrieve", {"id": "sub_test"}),
    )
    fake.WebhookEndpoint = types.SimpleNamespace(
        list=record("WebhookEndpoint.list", {"data": []})
    )
    fake.billing_portal = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=record("billing_portal.Session.create", {
            "url": "https://billing.stripe.com/p/session/test"
        })),
        Configuration=types.SimpleNamespace(
            list=record("billing_portal.Configuration.list", {"data": []}),
            retrieve=record("billing_portal.Configuration.retrieve", {}),
        ),
    )
    fake.api_key = ""

    monkeypatch.setitem(sys.modules, "stripe", fake)
    return fake


@pytest.fixture()
def client(marketplace_env):
    """A ``TestClient`` over the real app, built after settings are in place.

    The app is imported INSIDE the fixture, not at module scope. ``app.py``
    evaluates settings-derived values at import time and its startup hook loads
    the entitlement key, so importing it before ``marketplace_env`` has run gives
    an app with the marketplace disabled and every route 404ing.
    """
    from starlette.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as test_client:
        yield test_client
