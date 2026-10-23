"""Real private CLI and shell lifecycle against disposable HTTP protocol boundaries."""
from __future__ import annotations

import base64
import contextlib
import fcntl
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
ADMIN_SESSION = "fixture_admin_session_" + "a" * 32
USER_SESSION = "fixture_user_session_" + "b" * 32
LEASE = "1" * 64
COUNTERS = ("engine_bridges", "cell_jobs", "provider_streams", "http_requests", "gateway_jobs",
            "cells_active", "cells_unknown", "cells_exiting")


class GatewayBoundary:
    """A deterministic HTTP peer, not a product gateway or service-manager substitute."""
    def __init__(self, *, busy=None, unknown=False, cells=1, foreign=False,
                 complete_after=None, health=True, malformed=None, lose_release_reply=False,
                 unknown_during_exit=False, identity=None):
        self.lock = threading.Lock()
        self.lease = "f" * 64 if foreign else None
        self.busy = busy
        self.unknown = unknown
        self.cells = cells
        self.exiting = 0
        self.complete_after = complete_after
        self.status_calls = 0
        self.health = health
        self.identity = ({"email": "operator@example.test", "role": "admin"}
                         if identity is None else identity)
        self.malformed = malformed
        self.lose_release_reply = lose_release_reply
        self.unknown_during_exit = unknown_during_exit
        self.events = []
        self.cookies = []
        self.exit_requested = False
        boundary = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.handle_operation(False)

            def do_POST(self):
                self.handle_operation(True)

            def handle_operation(self, post):
                with boundary.lock:
                    cookie = self.headers.get("Cookie", "")
                    if self.path == "/healthz":
                        boundary.events.append("health")
                        self.respond(200 if boundary.health else 503, {"ok": boundary.health})
                        return
                    if self.path == "/fixture/customer":
                        boundary.events.append("customer_refused" if boundary.lease else "customer_accepted")
                        self.respond(503 if boundary.lease else 200, {"ok": boundary.lease is None})
                        return
                    boundary.cookies.append(cookie)
                    if cookie != "chudware_sid=" + ADMIN_SESSION:
                        self.respond(403 if cookie else 401, {"detail": "admin only"})
                        return
                    if self.path == "/api/me":
                        boundary.events.append("identity")
                        self.respond(200, boundary.identity)
                        return
                    action = self.path.split("?")[0].rsplit("/", 1)[-1]
                    if not self.path.startswith("/api/_ops/deployment-drain/"):
                        self.respond(404, {})
                        return
                    if post:
                        if self.headers.get("Origin") != "https://chudware.com":
                            self.respond(403, {"detail": "origin refused"})
                            return
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 256:
                            self.respond(400, {})
                            return
                        body = json.loads(self.rfile.read(length))
                        if set(body) != {"lease_id"}:
                            self.respond(400, {})
                            return
                        lease = body["lease_id"]
                    else:
                        lease = self.path.partition("?lease_id=")[2]
                    if action == "begin" and boundary.lease is None:
                        boundary.lease = lease
                    if boundary.lease != lease:
                        boundary.events.append("foreign_refused")
                        self.respond(409, {"detail": "foreign lease"})
                        return
                    boundary.events.append(action)
                    if action == "status":
                        boundary.status_calls += 1
                        if boundary.complete_after and boundary.status_calls >= boundary.complete_after:
                            boundary.busy = None
                        if boundary.exit_requested:
                            # Observe one real transition before the authoritative inactive proof.
                            if boundary.exiting:
                                boundary.exiting = 0
                            else:
                                boundary.cells = 0
                                if boundary.unknown_during_exit:
                                    boundary.unknown = False
                    counts = {name: 0 for name in COUNTERS}
                    if boundary.busy:
                        counts[boundary.busy] = 1
                    counts["cells_active"] = boundary.cells
                    counts["cells_unknown"] = int(boundary.unknown)
                    counts["cells_exiting"] = boundary.exiting
                    quiescent = not boundary.busy and not boundary.unknown and not boundary.exiting
                    if action == "exit-cells":
                        if not quiescent:
                            self.respond(409, {"detail": "not quiescent"})
                            return
                        boundary.exit_requested = True
                        boundary.exiting = int(boundary.cells > 0)
                        counts["cells_exiting"] = boundary.exiting
                        quiescent = not boundary.exiting
                        if boundary.unknown_during_exit and boundary.exiting:
                            boundary.unknown = True
                            counts["cells_unknown"] = 1
                    if action == "release":
                        boundary.lease = None
                        if boundary.lose_release_reply:
                            boundary.lose_release_reply = False
                            self.close_connection = True
                            return
                    response = {"schema": 1, "lease_id": lease,
                                "admission": "open" if boundary.lease is None else "closed",
                                "complete": not boundary.unknown, "quiescent": quiescent, **counts}
                    if boundary.malformed:
                        boundary.malformed(response)
                    self.respond(200, response)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@contextlib.contextmanager
def gateway(**kwargs):
    boundary = GatewayBoundary(**kwargs)
    try:
        yield boundary
    finally:
        boundary.close()


@contextlib.contextmanager
def private_value(value):
    # Never persist the bearer or put it in argv/environment. The CLI validates the FD.
    with tempfile.TemporaryFile() as temporary:
        temporary.write((value + "\n").encode("ascii"))
        temporary.flush()
        descriptor = fcntl.fcntl(temporary.fileno(), fcntl.F_DUPFD_CLOEXEC, 20)
        try:
            yield descriptor
        finally:
            os.close(descriptor)


def configuration(tmp_path, **overrides):
    values = {
        "CHUDWARE_DEPLOY_ENV": "prod", "CHUDWARE_WEB_DEV": "0",
        "CHUDWARE_DB_URL": "postgresql://fixture:fixture-secret@127.0.0.1/fixture",
        "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"m" * 32).decode(),
        "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"p" * 32).decode(),
        "CHUDWARE_MAIL_BACKEND": "smtp", "CHUDWARE_SMTP_HOST": "127.0.0.1",
        "CHUDWARE_SMTP_PORT": "587", "CHUDWARE_SMTP_TLS": "starttls",
        "CHUDWARE_SMTP_USER": "fixture", "CHUDWARE_SMTP_PASS": "fixture-mail-secret",
    }
    for label, name in (("data", "CHUDWARE_WEB_DATA_ROOT"), ("workspace", "CHUDWARE_WORKSPACE_ROOT"),
                        ("bucket", "CHUDWARE_WEB_BUCKET_ROOT"), ("cells", "CHUDWARE_CELLS_ROOT")):
        root = tmp_path / label
        root.mkdir(exist_ok=True)
        values[name] = str(root)
    values.update(overrides)
    path = tmp_path / "gateway.env"
    path.write_text("".join(name + "=" + value + "\n" for name, value in values.items()))
    path.chmod(0o600)
    return path


def cli(tmp_path, boundary, action, *, lease=LEASE, session=ADMIN_SESSION, wait=0, expected_email=None):
    values = {} if expected_email is None else {"CHUDWARE_DEPLOY_HEALTH_EMAIL": expected_email}
    with private_value(lease) as lease_fd, private_value(session) as session_fd:
        args = [sys.executable, "-I", str(DEPLOY / "gateway_preflight.py"), "deployment-drain", action,
                "--env-file", str(configuration(tmp_path, **values)), "--environment", "prod",
                "--lease-fd", str(lease_fd), "--session-fd", str(session_fd),
                "--port", str(boundary.port), "--max-wait", str(wait)]
        result = subprocess.run(args, env={"PATH": os.defpath, "http_proxy": "http://invalid.invalid:1"},
                                pass_fds=(lease_fd, session_fd), capture_output=True, text=True, timeout=15)
    assert session not in " ".join(args) + result.stdout + result.stderr
    assert lease not in " ".join(args) + result.stdout + result.stderr
    return result


SHELL = r'''set -euo pipefail
source "$DRAIN_SCRIPT"
MUTATED=0
cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "$rc" != 0 ]]; then
    deployment_drain_abort || true
    if [[ "$MUTATED" = 0 ]]; then exit 4; else exit 1; fi
  fi
}
trap cleanup EXIT
deployment_drain_acquire
if [[ "$CASE" = premutation ]]; then exit 77; fi
if [[ "$CASE" = race ]]; then
  "$GATEWAY_PY" -I -c 'import os, urllib.error, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:" + os.environ["CHUDWARE_WEB_GATEWAY_PORT"] + "/fixture/customer")
except urllib.error.HTTPError as error:
    assert error.code == 503
else:
    raise SystemExit(77)'
fi
if [[ "$CASE" = nested ]]; then
  bash -eu -c 'source "$DRAIN_SCRIPT"; MUTATED=0; deployment_drain_acquire; deployment_drain_abort; deployment_drain_complete'
fi
printf mutated > "$MUTATION"
MUTATED=1
if [[ "$CASE" = postmutation ]]; then exit 77; fi
if [[ "$CASE" = reexec ]]; then
  exec bash -eu -c 'source "$DRAIN_SCRIPT"; MUTATED=1; deployment_drain_acquire; deployment_drain_complete'
fi
deployment_drain_complete
'''


def shell(tmp_path, boundary, case="success", *, session=ADMIN_SESSION, wait=0):
    path = configuration(tmp_path)
    with private_value(session) as session_fd:
        environment = {"PATH": os.defpath, "HOME": str(tmp_path),
                       "GATEWAY_PY": sys.executable, "GATEWAY_ENV": str(path), "ENVIRONMENT": "prod",
                       "GATEWAY_PORT": str(boundary.port), "CHUDWARE_WEB_GATEWAY_PORT": str(boundary.port),
                       "CHUDWARE_DEPLOY_SESSION_FD": str(session_fd), "CHUDWARE_DEPLOY_HEALTH_ATTEMPTS": "1",
                       "CHUDWARE_WEBD_RESTART_MAX_WAIT": str(wait), "CASE": case,
                       "DRAIN_SCRIPT": str(DEPLOY / "deployment_drain.sh"),
                       "MUTATION": str(tmp_path / "mutated"), "TMPDIR": str(tmp_path)}
        result = subprocess.run(["bash", "-x", "-c", SHELL], env=environment, pass_fds=(session_fd,),
                                capture_output=True, text=True, timeout=20)
    assert session not in result.stdout + result.stderr
    assert boundary.lease is None or boundary.lease not in result.stdout + result.stderr
    assert not list(tmp_path.glob("tmp.*"))
    return result


@pytest.mark.parametrize("busy", ["engine_bridges", "cell_jobs", "provider_streams", "http_requests", "gateway_jobs"])
def test_busy_refuses_without_exit_or_mutation_and_releases_only_own_lease(tmp_path, busy):
    with gateway(busy=busy) as boundary:
        result = shell(tmp_path, boundary)
        assert result.returncode == 4, result.stderr
        assert not (tmp_path / "mutated").exists()
        assert "exit-cells" not in boundary.events
        assert boundary.events == ["begin", "release"]
        assert boundary.lease is None


def test_unknown_refuses_without_self_exit_or_mutation_and_reopens_own_lease(tmp_path):
    with gateway(unknown=True) as boundary:
        result = shell(tmp_path, boundary)
        assert result.returncode == 4, result.stderr
        assert "deployment_drain_census_unknown" in result.stderr
        assert not (tmp_path / "mutated").exists()
        assert boundary.events == ["begin", "release"]
        assert boundary.lease is None


@pytest.mark.parametrize("malformed", [
    lambda body: body.pop("http_requests"),
    lambda body: body.update(cell_jobs=False),
    lambda body: body.update(schema=True),
    lambda body: body.update(quiescent=True, gateway_jobs=1),
])
def test_missing_or_inconsistent_authority_is_never_idle(tmp_path, malformed):
    with gateway(malformed=malformed) as boundary:
        result = shell(tmp_path, boundary)
        assert result.returncode == 4, result.stderr
        assert not (tmp_path / "mutated").exists()
        assert "exit-cells" not in boundary.events


def test_foreign_lease_is_never_taken_over_or_released(tmp_path):
    with gateway(foreign=True) as boundary:
        foreign = boundary.lease
        result = shell(tmp_path, boundary)
        assert result.returncode == 4, result.stderr
        assert boundary.lease == foreign
        assert boundary.events == ["foreign_refused", "foreign_refused"]
        assert not (tmp_path / "mutated").exists()


def test_nonadmin_session_cannot_begin_or_release_and_is_not_promoted(tmp_path):
    with gateway() as boundary:
        result = cli(tmp_path, boundary, "begin", session=USER_SESSION)
        assert result.returncode == 4
        assert "deployment_drain_admin_required" in result.stderr
        assert boundary.lease is None and boundary.events == []


@pytest.mark.parametrize("case", ["success", "race", "nested", "reexec"])
def test_idle_cutover_self_exits_observes_inactive_then_health_and_releases(tmp_path, case):
    with gateway() as boundary:
        result = shell(tmp_path, boundary, case, wait=3)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "mutated").read_text() == "mutated"
        assert boundary.cells == 0 and boundary.lease is None
        assert boundary.events[0] == "begin"
        assert boundary.events.index("exit-cells") < boundary.events.index("health") < boundary.events.index("release")
        assert boundary.events.count("release") == 1
        assert boundary.events.count("begin") == 1
        assert boundary.status_calls >= 2
        if case == "race":
            assert "customer_refused" in boundary.events and "customer_accepted" not in boundary.events
        assert all(cookie == "chudware_sid=" + ADMIN_SESSION for cookie in boundary.cookies)


def test_native_exit_unknown_transition_waits_for_complete_inactive_proof(tmp_path):
    with gateway(unknown_during_exit=True) as boundary:
        result = shell(tmp_path, boundary, wait=3)
        assert result.returncode == 0, result.stderr
        assert boundary.status_calls >= 2
        assert boundary.cells == 0 and boundary.unknown is False
        assert (tmp_path / "mutated").exists()
        assert boundary.lease is None


def test_already_accepted_work_completes_before_native_exit(tmp_path):
    with gateway(busy="gateway_jobs", complete_after=2) as boundary:
        result = shell(tmp_path, boundary, wait=4)
        assert result.returncode == 0, result.stderr
        exit_index = boundary.events.index("exit-cells")
        assert boundary.events[:exit_index].count("status") == 2
        assert boundary.lease is None


def test_premutation_abort_releases_its_closed_lease(tmp_path):
    with gateway(cells=0) as boundary:
        result = shell(tmp_path, boundary, "premutation")
        assert result.returncode == 4
        assert not (tmp_path / "mutated").exists()
        assert boundary.lease is None and boundary.events[-1] == "release"


@pytest.mark.parametrize("case,healthy", [("postmutation", True), ("success", False)])
def test_postmutation_failure_remains_failed_and_keeps_maintenance_closed(tmp_path, case, healthy):
    with gateway(cells=0, health=healthy) as boundary:
        result = shell(tmp_path, boundary, case)
        assert result.returncode == 1, result.stderr
        assert (tmp_path / "mutated").exists()
        assert boundary.lease is not None
        assert "release" not in boundary.events
        assert "maintenance remains CLOSED" in result.stderr


def test_lost_release_reply_fails_and_recloses_maintenance_with_same_lease(tmp_path):
    with gateway(cells=0, lose_release_reply=True) as boundary:
        result = shell(tmp_path, boundary)
        assert result.returncode == 1, result.stderr
        assert (tmp_path / "mutated").exists()
        assert boundary.lease is not None
        assert boundary.events[-2:] == ["release", "begin"]
        assert "maintenance remains CLOSED" in result.stderr


def test_cli_replay_is_same_lease_idempotent_and_foreign_release_refuses(tmp_path):
    with gateway(cells=0) as boundary:
        assert cli(tmp_path, boundary, "begin").returncode == 0
        assert cli(tmp_path, boundary, "begin").returncode == 0
        result = cli(tmp_path, boundary, "release", lease="2" * 64)
        assert result.returncode == 4 and boundary.lease == LEASE
        result = cli(tmp_path, boundary, "status")
        assert result.returncode == 0
        assert json.loads(result.stdout)["admission"] == "closed"
        assert cli(tmp_path, boundary, "complete").returncode == 0
        assert boundary.lease is None


def test_linked_or_unprotected_secret_descriptor_is_rejected_before_http(tmp_path):
    with gateway(cells=0) as boundary, private_value(ADMIN_SESSION) as session_fd:
        linked = tmp_path / "lease"
        linked.write_text(LEASE)
        linked.chmod(0o600)
        with linked.open("rb") as lease_file:
            result = subprocess.run(
                [sys.executable, "-I", str(DEPLOY / "gateway_preflight.py"), "deployment-drain", "begin",
                 "--env-file", str(configuration(tmp_path)), "--environment", "prod",
                 "--lease-fd", str(lease_file.fileno()), "--session-fd", str(session_fd),
                 "--port", str(boundary.port)], pass_fds=(lease_file.fileno(), session_fd),
                env={"PATH": os.defpath}, capture_output=True, text=True, timeout=10)
        assert result.returncode == 4
        assert boundary.events == []
        assert LEASE not in result.stdout + result.stderr


@pytest.mark.parametrize("identity,accepted", [
    ({"email": "operator@example.test", "role": "admin"}, True),
    ({"email": "another@example.test", "role": "admin"}, False),
    ({"email": "operator@example.test", "role": "editor"}, False),
    ({"email": "", "role": "admin"}, False),
])
def test_private_fd_health_requires_the_selected_admin_identity_without_secret_artifacts(
        tmp_path, identity, accepted):
    with gateway(cells=0, identity=identity) as boundary:
        result = cli(tmp_path, boundary, "health", expected_email=" Operator@Example.Test ")
        assert result.returncode == (0 if accepted else 4), result.stderr
        assert boundary.events == ["health", "identity"]
        assert boundary.cookies == ["chudware_sid=" + ADMIN_SESSION]
        assert boundary.lease is None
    assert not list(tmp_path.glob("tmp.*"))
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert ADMIN_SESSION.encode() not in path.read_bytes()


@contextlib.contextmanager
def public_site(*, login_ok):
    """A loopback HTTP consumer for the unmodified public-verification script."""
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, code, body=b"", **headers):
            self.send_response(code)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/api/auth/login"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("login", payload))
            if login_ok:
                self.respond(200, b"{}", **{"Set-Cookie": f"chudware_sid={ADMIN_SESSION}; Path=/; HttpOnly"})
            else:
                self.respond(401, b'{"detail":"invalid login"}')

        def do_GET(self):
            site = self.headers["X-Fixture-Site"]
            authenticated = self.headers.get("Cookie") == "chudware_sid=" + ADMIN_SESSION
            requests.append((self.path, authenticated))
            if site != "app.chudware.com" or self.path in ("/signin", "/signup", "/healthz"):
                self.respond(200, b"{}")
            elif self.path == "/admin" and not authenticated:
                self.respond(303, Location="/signin?next=%2Fadmin")
            elif self.path == "/api/me" and authenticated:
                self.respond(200, b'{"email":"operator@example.test","role":"admin"}')
            elif authenticated:
                self.respond(200, b"{}")
            else:
                self.respond(401, b'{"detail":"sign in required"}')

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("login_ok", [True, False])
def test_public_verification_password_uses_stdin_not_argv_or_logs_and_cleans_cookie_jar(
        tmp_path, login_ok):
    real_curl = shutil.which("curl")
    if real_curl is None:
        pytest.skip("curl is required for the local public-verification protocol fixture")
    password = 'fixture-owner-password-"\\-$-not-an-argument'
    binaries = tmp_path / "bin"
    binaries.mkdir()
    artifacts = tmp_path / "private"
    artifacts.mkdir(mode=0o700)
    trace = tmp_path / "children.jsonl"
    # Preserve the product curl invocation and its stdin; only substitute the
    # destination, refusing every host other than the script's three public sites.
    relay = binaries / "curl"
    relay.write_text(f"""#!{sys.executable}
import json, os, pathlib, stat, subprocess, sys
from urllib.parse import urlsplit
args = sys.argv[1:]
target = urlsplit(args[-1])
if target.scheme != "https" or target.netloc not in (
        "chudware.com", "www.chudware.com", "app.chudware.com"):
    sys.exit(90)
record = {{"argv": args}}
body = None
if "--data-binary" in args:
    body = sys.stdin.buffer.read()
    record["stdin"] = body.decode()
for flag in ("-c", "-b"):
    if flag in args:
        jar = pathlib.Path(args[args.index(flag) + 1])
        record.update(jar_path=str(jar), jar_mode=stat.S_IMODE(jar.stat().st_mode))
with open(os.environ["FIXTURE_CHILD_TRACE"], "a") as out:
    out.write(json.dumps(record) + "\\n")
local = "http://127.0.0.1:" + os.environ["FIXTURE_HTTP_PORT"]
args[-1] = local + target.path + ("?" + target.query if target.query else "")
args.extend(["--noproxy", "*", "-H", "X-Fixture-Site: " + target.netloc])
result = subprocess.run([{real_curl!r}, *args], input=body, capture_output=True)
# Match curl's public effective URL while all transport stays on loopback.
sys.stdout.buffer.write(result.stdout.replace(local.encode(), b"https://" + target.netloc.encode()))
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
""", encoding="utf-8")
    relay.chmod(0o700)
    with public_site(login_ok=login_ok) as (port, requests):
        environment = {"PATH": str(binaries) + os.pathsep + os.defpath,
                       "HOME": str(tmp_path), "TMPDIR": str(artifacts),
                       "OWNER_PASSWORD": password, "FIXTURE_CHILD_TRACE": str(trace),
                       "FIXTURE_HTTP_PORT": str(port)}
        result = subprocess.run(["bash", str(DEPLOY / "verify_public.sh")],
                                env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == (0 if login_ok else 1), result.stdout + result.stderr
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    login = [record for record in records if "stdin" in record]
    assert len(login) == 1
    payload = json.loads(login[0]["stdin"])
    assert payload["password"] == password
    assert [request[1] for request in requests if request[0] == "login"] == [payload]
    assert login[0]["jar_mode"] == 0o600
    assert not Path(login[0]["jar_path"]).exists()
    assert not list(artifacts.iterdir())
    for record in records:
        assert all(password not in arg and ADMIN_SESSION not in arg for arg in record["argv"])
    assert password not in result.stdout + result.stderr
    assert ADMIN_SESSION not in result.stdout + result.stderr
    if login_ok:
        assert ("/api/me", True) in requests
        assert ("/admin", True) in requests
