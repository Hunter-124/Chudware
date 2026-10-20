#!/usr/bin/env python3
"""Offline redacted readiness; explicit key provisioning and private operator maintenance."""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import stat
import re
import sys
from datetime import timedelta
from pathlib import Path
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.deployment_config import (  # noqa: E402
    ConfigurationError, MAIL_KEY, PROVIDER_KEY, absolute_path, decode_encryption_key,
    mail_configuration_errors, open_parent, read_environment_file,
)
from gateway.storage_config import configured_storage  # noqa: E402

PreflightError = ConfigurationError


def _environment(values: dict[str, str], environment: str) -> None:
    if environment not in {"prod", "staging"} or values.get("CHUDWARE_DEPLOY_ENV", "prod") != environment:
        raise PreflightError("gateway_environment_mismatch")


def configuration_errors(values: dict[str, str], environment: str) -> list[str]:
    _environment(values, environment)
    errors = mail_configuration_errors(values, environment)
    if not values.get("CHUDWARE_DB_URL", "").strip():
        errors.append("explicit_database_url_required")
    if values.get("CHUDWARE_ENTITLEMENT_KEY_PATH"):
        errors.append("plaintext_entitlement_key_path_refused")
    if values.get("CHUDWARE_WEB_DEV", "").strip().lower() in {"1", "true", "yes", "on"}:
        errors.append("deployed_dev_auth_refused")
    stripe = values.get("CHUDWARE_WEB_STRIPE_SECRET_KEY", "")
    if environment == "staging" and stripe and not stripe.startswith(("sk_test_", "rk_test_")):
        errors.append("staging_live_stripe_refused")
    try:
        # All roots retain raw lexical authority for no-follow checks. A missing
        # directory is a provisioning prerequisite, not evidence of a symlink.
        storage = configured_storage(environment, values)
        for raw in storage.raw_paths.values():
            path = absolute_path(raw)
            try:
                parent = open_parent(path, protected=False)
            except FileNotFoundError:
                continue
            try:
                try:
                    info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    raise PreflightError("storage_root_not_directory")
            finally:
                os.close(parent)
    except (ConfigurationError, OSError):
        errors.append("storage_configuration_invalid")
    return errors


def load_deploy_environment(path: str | Path, environment: str) -> dict[str, str]:
    values, _, _ = read_environment_file(path)
    errors = configuration_errors(values, environment)
    if errors:
        raise PreflightError(",".join(errors))
    return values


def qualification_report(environment: str, errors: list[str]) -> dict:
    return {
        "schema": "chudware-mail-qualification-v1",
        "environment": environment,
        "configuration": {"status": "FAIL" if errors else "PASS", "refusals": errors},
        "deliverability": {
            "status": "UNKNOWN",
            "reason": "offline_configuration_is_not_delivery_evidence",
            "external_prerequisites": ["DNS", "SPF", "DKIM", "provider acceptance", "recipient receipt"],
        },
    }


def ops_session(values: dict[str, str], environment: str, email: str, ttl_seconds: int,
                *, require_admin: bool = False) -> str:
    if not 1 <= ttl_seconds <= 3600:
        raise PreflightError("ops_session_ttl_invalid")
    # Do not inherit gateway secrets, provider SDK credentials, Python injection
    # variables or optional billing/OAuth configuration from the caller. Every
    # required key is real protected-file authority, never a synthetic crypto key.
    minimal = {name: values[name] for name in ("CHUDWARE_DB_URL", MAIL_KEY, PROVIDER_KEY)}
    minimal["CHUDWARE_DEPLOY_ENV"] = environment
    storage = configured_storage(environment, values, discovery={})
    for label, name in (("data", "CHUDWARE_WEB_DATA_ROOT"), ("workspace", "CHUDWARE_WORKSPACE_ROOT"),
                        ("bucket", "CHUDWARE_WEB_BUCKET_ROOT"), ("cells", "CHUDWARE_CELLS_ROOT")):
        minimal[name] = str(storage.roots[label])
    os.environ.clear()
    os.environ.update(minimal)
    logging.disable(logging.CRITICAL)
    # Settings construction happens only after minimal protected authority is set.
    from gateway import accounts, authn, db

    address = authn.normalize_email(email)
    if address and not authn.valid_email(address):
        raise PreflightError("ops_session_email_invalid")
    try:
        with db.tx() as cur:
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute("SET LOCAL statement_timeout = '15s'")
            if not address and require_admin:
                cur.execute("SELECT email FROM accounts WHERE status='active' AND role='admin' ORDER BY email LIMIT 1")
                row = cur.fetchone()
                address = row["email"] if row else ""
            if not address:
                raise PreflightError("ops_session_email_invalid")
            authn._lock_account(address)
            account = accounts.get_account(address)
            if account is None or not accounts.is_authorized(account):
                raise PreflightError("ops_session_account_not_authorized")
            if require_admin and accounts.effective_role(account) != "admin":
                raise PreflightError("deployment_drain_admin_required")
            return authn.create_session(address, user_agent="chudware-ops-cli", ttl=timedelta(seconds=ttl_seconds))
    finally:
        db.close_pool()


def provision_keys(path: str | Path, environment: str) -> None:
    """Explicit missing-key provisioning only; preserve valid existing keys."""
    path = absolute_path(path)
    if path.name.lower() == "backup.env":
        raise PreflightError("backup_environment_cannot_hold_encryption_keys")
    values, text, original = read_environment_file(path)
    _environment(values, environment)
    decoded: dict[str, bytes] = {}
    replacements: dict[str, str] = {}
    for name in (MAIL_KEY, PROVIDER_KEY):
        if values.get(name):
            decoded[name] = decode_encryption_key(values[name].strip() if name == PROVIDER_KEY else values[name], name)
        else:
            replacements[name] = secrets.token_urlsafe(32)
            decoded[name] = decode_encryption_key(replacements[name], name)
    if decoded[MAIL_KEY] == decoded[PROVIDER_KEY]:
        raise PreflightError("encryption_keys_must_be_independent")
    if not replacements:
        return
    lines = []
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip()
        if name in replacements and not line.lstrip().startswith("#"):
            lines.append(name + "=" + replacements.pop(name))
        else:
            lines.append(line)
    lines.extend(name + "=" + value for name, value in replacements.items())
    payload = ("\n".join(lines) + "\n").encode()
    if len(payload) > 65536:
        raise PreflightError("environment_file_oversized")
    parent = open_parent(path)
    temporary = ".gateway-key-provision-" + secrets.token_hex(16) + ".env"
    try:
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        stable = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                               item.st_ctime_ns, item.st_mode, item.st_uid, item.st_nlink)
        if stable(current) != stable(original):
            raise PreflightError("environment_file_changed")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if stable(current) != stable(original):
            raise PreflightError("environment_file_changed")
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


_DRAIN_COUNTERS = (
    "engine_bridges", "cell_jobs", "provider_streams", "http_requests", "gateway_jobs",
    "cells_active", "cells_unknown", "cells_exiting",
)
_DRAIN_WORK = ("engine_bridges", "cell_jobs", "provider_streams", "http_requests", "gateway_jobs")


def private_fd_value(fd: int, *, lease: bool) -> str:
    """Read only an explicitly inherited, owner-only anonymous regular file."""
    info = os.fstat(fd)
    maximum = 65 if lease else 513
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 0
            or not 1 <= info.st_size <= maximum):
        raise PreflightError("deployment_private_channel_refused")
    value = os.pread(fd, maximum + 1, 0).decode("ascii").strip()
    pattern = r"[0-9a-f]{64}" if lease else r"[A-Za-z0-9_-]{32,512}"
    if not re.fullmatch(pattern, value):
        raise PreflightError("deployment_private_value_refused")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise PreflightError("deployment_gateway_redirect_refused")


class DrainClient:
    def __init__(self, port: int, session: str, lease_id: str, origin: str, *, expected_email: str = ""):
        if not 1 <= port <= 65535 or not re.fullmatch(r"[0-9a-f]{64}", lease_id):
            raise PreflightError("deployment_drain_arguments_invalid")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", session):
            raise PreflightError("deployment_session_invalid")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise PreflightError("deployment_origin_invalid")
        self.base = f"http://127.0.0.1:{port}"
        self.session = session
        self.lease_id = lease_id
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.expected_email = expected_email.strip().lower()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, path: str, *, post: bool = False, authenticated: bool = True) -> dict:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Cookie"] = "chudware_sid=" + self.session
        data = None
        if post:
            headers.update({"Content-Type": "application/json", "Origin": self.origin})
            data = json.dumps({"lease_id": self.lease_id}).encode("ascii")
        request = urllib.request.Request(self.base + path, data=data, headers=headers,
                                         method="POST" if post else "GET")
        try:
            with self.opener.open(request, timeout=5) as response:
                if response.status != 200:
                    raise PreflightError("deployment_gateway_refused")
                raw = response.read(65537)
            if len(raw) > 65536:
                raise PreflightError("deployment_gateway_response_oversized")
            body = json.loads(raw)
            if type(body) is not dict:
                raise PreflightError("deployment_gateway_response_invalid")
            return body
        except urllib.error.HTTPError as exc:
            code = ("deployment_drain_admin_required" if exc.code in {401, 403}
                    else "deployment_drain_foreign_lease" if exc.code == 409
                    else "deployment_gateway_refused")
            raise PreflightError(code) from None
        except (OSError, ValueError):
            raise PreflightError("deployment_gateway_unavailable") from None

    def operation(self, action: str) -> dict:
        path = "/api/_ops/deployment-drain/" + action
        if action == "status":
            path += "?lease_id=" + self.lease_id
        body = self.request(path, post=action != "status")
        if (type(body.get("schema")) is not int or body["schema"] != 1
                or body.get("lease_id") != self.lease_id
                or body.get("admission") != ("open" if action == "release" else "closed")
                or type(body.get("complete")) is not bool or type(body.get("quiescent")) is not bool
                or any(type(body.get(key)) is not int or body[key] < 0 for key in _DRAIN_COUNTERS)):
            raise PreflightError("deployment_drain_census_invalid")
        if body["quiescent"] and (not body["complete"] or body["cells_unknown"]
                                   or body["cells_exiting"] or any(body[key] for key in _DRAIN_WORK)):
            raise PreflightError("deployment_drain_census_inconsistent")
        return body

    @staticmethod
    def ready(body: dict, *, inactive: bool = False) -> bool:
        if not body["complete"] or body["cells_unknown"]:
            raise PreflightError("deployment_drain_census_unknown")
        return (body["quiescent"] and not any(body[key] for key in _DRAIN_WORK)
                and body["cells_exiting"] == 0 and (not inactive or body["cells_active"] == 0))

    def wait_ready(self, body: dict, deadline: float, *, inactive: bool = False) -> None:
        while True:
            try:
                if self.ready(body, inactive=inactive):
                    return
            except PreflightError:
                # After an acknowledged self-exit, a disappearing native socket or
                # deactivating unit is not idle proof. Wait only for the bounded
                # operator operation; never mutate until the census is complete.
                if not inactive or time.monotonic() >= deadline:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreflightError("deployment_drain_busy")
            time.sleep(min(1, remaining))
            body = self.operation("status")

    def prepare(self, max_wait: int) -> None:
        if not 0 <= max_wait <= 3540:
            raise PreflightError("deployment_drain_wait_invalid")
        deadline = time.monotonic() + max_wait
        self.wait_ready(self.operation("begin"), deadline)
        # This endpoint asks hostd to recheck and exit itself, never stops a unit.
        self.operation("exit-cells")
        self.wait_ready(self.operation("status"), deadline, inactive=True)

    def check(self) -> None:
        if not self.ready(self.operation("status"), inactive=True):
            raise PreflightError("deployment_cells_not_inactive")

    def health(self) -> None:
        health = self.request("/healthz", authenticated=False)
        identity = self.request("/api/me")
        if (health != {"ok": True} or identity.get("role") != "admin" or not identity.get("email")
                or (self.expected_email and identity.get("email") != self.expected_email)):
            raise PreflightError("deployment_gateway_health_failed")


def deployment_drain(values: dict[str, str], args) -> None:
    lease_id = private_fd_value(args.lease_fd, lease=True)
    managed_session = args.session_fd is None
    token = (ops_session(values, args.environment,
                         values.get("CHUDWARE_DEPLOY_HEALTH_EMAIL") or values.get("CHUDWARE_WEBD_HEALTH_EMAIL", ""),
                         3600, require_admin=True) if managed_session
             else private_fd_value(args.session_fd, lease=False))
    try:
        port = args.port if args.port is not None else int(
            values.get("CHUDWARE_WEB_GATEWAY_PORT") or (8310 if args.environment == "staging" else 8300))
        client = DrainClient(
            port, token, lease_id, values.get("CHUDWARE_WEB_PUBLIC_BASE_URL") or "https://chudware.com",
            expected_email=values.get("CHUDWARE_DEPLOY_HEALTH_EMAIL") or values.get("CHUDWARE_WEBD_HEALTH_EMAIL", ""))
        if args.action == "prepare":
            client.prepare(args.max_wait)
        elif args.action == "check":
            client.check()
        elif args.action == "complete":
            client.check()
            client.health()
            client.operation("release")
        elif args.action == "health":
            client.health()
        else:
            body = client.operation(args.action)
            if args.action == "status":
                print(json.dumps({key: body[key] for key in ("schema", "admission", "complete", "quiescent", *_DRAIN_COUNTERS)},
                                 sort_keys=True))
    finally:
        if managed_session:
            from gateway import authn, db
            try:
                authn.revoke_session(token)
            finally:
                db.close_pool()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "ops-session", "provision-keys", "deployment-drain"):
        item = commands.add_parser(command)
        item.add_argument("--env-file", type=Path, required=True)
        item.add_argument("--environment", choices=("prod", "staging"), required=True)
        if command == "ops-session":
            item.add_argument("--email", required=True)
            item.add_argument("--ttl-seconds", type=int, default=900)
        if command == "provision-keys":
            item.add_argument("--apply", action="store_true", help="explicitly write independent missing keys; never rotate existing keys")
        if command == "deployment-drain":
            item.add_argument("action", choices=("prepare", "check", "complete", "health", "begin", "status", "release"))
            item.add_argument("--lease-fd", type=int, required=True, help="inherited protected anonymous lease descriptor")
            item.add_argument("--session-fd", type=int, help="optional inherited protected admin-session descriptor")
            item.add_argument("--port", type=int, help="selected loopback gateway port")
            item.add_argument("--max-wait", type=int, default=600, help="operator wait only; never terminates customer work")
    args = parser.parse_args(argv)
    try:
        if args.command == "provision-keys":
            if not args.apply:
                raise PreflightError("explicit_apply_required")
            provision_keys(args.env_file, args.environment)
            return 0
        values, _, _ = read_environment_file(args.env_file)
        errors = configuration_errors(values, args.environment)
        if args.command == "validate":
            print(json.dumps(qualification_report(args.environment, errors), sort_keys=True))
            return 4 if errors else 0
        if errors:
            raise PreflightError(",".join(errors))
        if args.command == "deployment-drain":
            deployment_drain(values, args)
            return 0
        token = ops_session(values, args.environment, args.email, args.ttl_seconds)
        print(token)
        return 0
    except Exception as exc:
        # Parser/OS/DB/provider errors may carry paths, DSNs or secret values.
        # The only outward error is a fixed refusal; validate still reports UNKNOWN.
        if args.command == "validate":
            print(json.dumps(qualification_report(args.environment, ["protected_configuration_refused"]), sort_keys=True))
        else:
            drain_refusals = {
                "deployment_drain_admin_required", "deployment_drain_foreign_lease",
                "deployment_drain_census_invalid", "deployment_drain_census_inconsistent",
                "deployment_drain_census_unknown", "deployment_drain_busy",
                "deployment_cells_not_inactive", "deployment_gateway_health_failed",
            }
            reason = str(exc) if args.command == "deployment-drain" and str(exc) in drain_refusals else "protected_configuration_or_operation_refused"
            print("gateway preflight refused: " + reason if args.command == "deployment-drain"
                  else "gateway preflight refused protected configuration or operation", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
