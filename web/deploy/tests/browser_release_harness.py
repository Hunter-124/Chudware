#!/usr/bin/env python3
"""Disposable real-gateway browser qualification; never launches a browser.

Admin only (no root, cells, or CAD claim)::
    "$GATEWAY_PY" Webservice/deploy/tests/browser_release_harness.py \
        serve --admin-only --metadata /tmp/browser-release.json

GATEWAY_PY must have the gateway requirements plus pytest; the known .venv-mcp
is independently staged as the acceptance-only native worker runtime.

For CAD, the operator first mounts an OWNED sparse XFS/ext4 project-quota image
in a private root mount namespace, starts the helper explicitly as root, then
runs prepare/serve as the ordinary user in that SAME namespace::
    ... browser_release_harness.py quota-helper --storage-root /PRIVATE-MOUNT/fixture-parent \
        --proof-root /run/PRIVATE-ROOT-PROOF --socket /run/PRIVATE-ROOT-PROOF/helper.sock \
        --service-uid ORDINARY_UID
    ... browser_release_harness.py prepare --root /tmp/br-UNIQUE \
        --storage-root /PRIVATE-MOUNT/fixture-parent/browser \
        --quota-helper /run/PRIVATE-ROOT-PROOF/helper.sock --chud /already/local/pinned/chud
    ... browser_release_harness.py serve --root /tmp/br-UNIQUE \
        --metadata /tmp/browser-release.json

The quota-helper command is explicitly root-authorized and loop-image-only;
prepare/serve run as the ordinary user in that same private mount namespace.
The helper provisions real quota proof and pins the read-only known venv mount.
No user namespace surrounds launch admission. The production launcher itself
creates the normal nested sandbox. There is no path-mount/admission fallback,
implicit sudo, product unit, shared daemon, or live upstream provider.

READY means the indicated surface is available, not production qualification.
The source-approved release installers/test reports are inert STAGING TEST
fixtures signed by a fresh disposable Ed25519 key trusted only by this process.
CAD, cache, cells, gateway, pinned SDK and browser WASM are real prebuilt artifacts;
provider responses come from a local official-wire SSE fixture behind the real broker.
SIGTERM ends only this server, its socket-only PostgreSQL, local wire fixture and
own cells, then asks the same helper to unbind its owned runtime. Root proof/image
and any explicitly reported failed unmount remain the operator's namespace cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace

sys.dont_write_bytecode = True

from _two_tenant_support import (
    A, B, C, BYOK, REPO, VENV, Harness, open_disposable_postgres,
    open_real_payload, private_json, quota_plan, quota_helper_request,
)
from _provider_wire_support import OfficialWireFixture, TEXT, THINKING, SIGNATURE

STATE_SCHEMA = "chudware-browser-release-harness-v1"
BOX_PATH = "Browser-qualification-box.step+"
ASSETS = REPO / "host/build-wasm/bin"


def owned_directory(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink():
        raise RuntimeError("scratch directory must not be a symlink")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"scratch directory must be private and owned by this user: {path}")
    if path.resolve() != path:
        raise RuntimeError("scratch path ancestors must not be symlinks")
    return path


def load_state(root: Path) -> dict:
    owned_directory(root)
    path = root / "fixture.json"
    with path.open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise RuntimeError("oversized fixture state")
    state = json.loads(raw)
    if state.get("schema") != STATE_SCHEMA or state.get("root") != str(root):
        raise RuntimeError("fixture state identity mismatch")
    owned_directory(Path(state["storage_root"]))
    return state


def stage_browser_assets(root: Path) -> Path:
    """Use build.sh's entry/companion layout without rebuilding or editing inputs."""
    app = root / "app"
    app.mkdir(mode=0o700, exist_ok=True)
    sources = {name: ASSETS / name for name in
               ("Chudware.html", "Chudware.js", "Chudware.wasm", "Chudware.data")}
    sources.update({path.name: path for path in ASSETS.glob("Chudware*worker*.js")})
    sources["index.html"] = ASSETS / "Chudware.html"
    sources["coi-serviceworker.js"] = REPO / "host/wasm/coi-serviceworker.js"
    for name, source in sources.items():
        if not source.is_file() or source.stat().st_size > 256 * 1024 * 1024:
            raise RuntimeError("existing browser asset missing or oversized: " + str(source))
        target = app / name
        if target.exists():
            with source.open("rb") as original, target.open("rb") as staged:
                if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(staged, "sha256").digest():
                    raise RuntimeError("prepared browser asset changed; prepare a new fixture: " + name)
        else:
            shutil.copyfile(source, target)
            target.chmod(0o444)
    return app


def configure(root: Path, storage: Path, dsn: str, origin: str) -> None:
    # Do not inherit provider/OAuth/payment/R2 credentials or arbitrary env files.
    os.environ.clear()
    os.environ.update({
        "PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "user-data"),
        "XDG_CACHE_HOME": str(root / "cache"), "XDG_RUNTIME_DIR": str(root / "run"),
        "CHUDWARE_DB_URL": dsn, "CHUDWARE_DEPLOY_ENV": "staging",
        "CHUDWARE_WEBD_ENV": str(root / "absent-webd.env"),
        "CHUDWARE_WEB_DEV": "1", "CHUDWARE_WEB_HOST": "127.0.0.1",
        "CHUDWARE_WEB_DATA_ROOT": str(storage), "CHUDWARE_CELLS_ROOT": str(storage / "cells"),
        "CHUDWARE_WORKSPACE_ROOT": str(storage / "workspaces"),
        "CHUDWARE_WEB_BUCKET_ROOT": str(storage / "bucket"),
        "CHUDWARE_WEB_APP_DIR": str(stage_browser_assets(root)),
        "CHUDWARE_WEB_PERF_TRACES_DIR": str(root / "perf"),
        "CHUDWARE_WEB_PROJECT_ROOTS": str(storage / "workspaces") + ":" + str(storage / "bucket"),
        "CHUDWARE_WEB_PUBLIC_BASE_URL": origin,
        "CHUDWARE_RELEASES_DIR": str(storage / "releases"),
        "CHUDWARE_RELEASES_PUBLIC_BASE_URL": origin,
        "CHUDWARE_GATEWAY_RELEASE_TOKEN": secrets.token_urlsafe(32),
        "CHUDWARE_MAIL_BACKEND": "console",
        "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"m" * 32).decode(),
        "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"p" * 32).decode(),
    })
    for name in ("home", "config", "user-data", "cache", "run", "perf"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    sys.path.insert(0, str(REPO / "Webservice"))
    sys.path.insert(0, str(REPO / "Webservice/gateway"))
    # Staging's real DB guard requires its own role/database even in a wholly
    # disposable cluster. Do not weaken that guard to use the postgres login.
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    with psycopg.connect(dsn, autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname='chudware_staging'").fetchone() is None:
            connection.execute("CREATE ROLE chudware_staging LOGIN")
        if connection.execute("SELECT 1 FROM pg_database WHERE datname='chudware_staging'").fetchone() is None:
            connection.execute("CREATE DATABASE chudware_staging OWNER chudware_staging")
    fields = conninfo_to_dict(dsn)
    fields.update(user="chudware_staging", dbname="chudware_staging")
    os.environ["CHUDWARE_DB_URL"] = make_conninfo(**fields)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    credential = root / "fixture-entitlement.key"
    credential.write_bytes(key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    credential.chmod(0o600)
    os.environ["CHUDWARE_ENTITLEMENT_KEY_PATH"] = str(credential)
    os.environ["CHUDWARE_STAGING_ENTITLEMENT_PUBLIC_KEY"] = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def gateway_modules():
    names = ("db", "accounts", "cells", "provider_keys", "provider_broker")
    modules = {name: importlib.import_module("gateway." + name) for name in names}
    from gateway.config import settings
    return SimpleNamespace(**modules, settings=settings)


def close_gateway_pools() -> None:
    # Marketplace follows the gateway suite's bare-import convention. Close
    # both legitimate import spellings if they opened independent pools.
    modules = {sys.modules[name] for name in ("gateway.db", "db") if name in sys.modules}
    for module in modules:
        module.close_pool()


def seed_accounts(gateway) -> None:
    for email in (A, B, C):
        gateway.accounts.ensure_account(email, display="Disposable browser qualification " + email)
        gateway.accounts.set_status(email, "active", decided_by="local-fixture")
        gateway.accounts.set_plan(email, "studio")
        # Synthetic fixture keys; never invokes live provider validation.
        for provider in ("openai", "anthropic"):
            gateway.provider_keys._persist_validated_key(email, provider, BYOK[email])
    with gateway.db.tx() as cur:
        cur.execute("UPDATE accounts SET role='admin' WHERE email=%s", (A,))




def prepare(args) -> None:
    root, storage = args.root.absolute(), args.storage_root.absolute()
    if root == storage or root.is_relative_to(storage) or storage.is_relative_to(root):
        raise RuntimeError("fixture control root and mounted storage root must be separate")
    root.mkdir(mode=0o700)
    storage_created = False
    gateway = harness = None
    mounted_runtime = None
    try:
        owned_directory(root)
        storage.mkdir(mode=0o700)
        storage_created = True
        owned_directory(storage)
        with open_disposable_postgres(root / "pg", retain=True) as dsn:
            configure(root, storage, dsn, "http://127.0.0.1")
            gateway = gateway_modules()
            gateway.db.migrate()
            seed_accounts(gateway)
            harness = Harness(root, gateway, None)
            plan = quota_plan(harness)
            private_json(root / "quota-plan.json", plan)
            proof = quota_helper_request({"operation": "provision", "plan": plan}, helper=args.quota_helper)
            with open_real_payload(directory=storage / "runtime", retain=True, chud_candidate=args.chud) as payload:
                if payload.chud is None or payload.sdk_blocked:
                    raise RuntimeError("provider fixture requires actual pinned native SDK: " +
                                       (payload.chud_blocked if payload.chud is None else payload.sdk_blocked))
                # Mark the mount attempt before sending; a lost reply must never
                # let cleanup traverse a root-owned bind created by the helper.
                mounted_runtime = payload.path / "venv"
                quota_helper_request({"operation": "runtime-bind", "target": str(mounted_runtime)},
                                     helper=args.quota_helper)
                state = {"schema": STATE_SCHEMA, "root": str(root), "storage_root": str(storage),
                         "quota_proof_root": proof["proof_root"], "quota_helper": str(args.quota_helper.absolute()),
                         "organization": [harness.org, harness.team],
                         "payload": str(payload.path), "bwrap": payload.bwrap,
                         "runtime": payload.runtime, "chud_blocked": payload.chud_blocked,
                         "sdk_blocked": payload.sdk_blocked}
                private_json(root / "fixture.json", state)
            gateway.db.close_pool()
        result = {**state, "quota_plan": str(root / "quota-plan.json"),
                  "root_preparation": {
                      "explicit_loop_image_quota_helper": str(args.quota_helper.absolute()),
                      "readonly_bind": {"source": str(VENV), "target": state["payload"] + "/venv"},
                      "same_private_mount_namespace_required": True,
                  }, "serve_argv": [sys.executable, str(Path(__file__).absolute()), "serve", "--root", str(root)]}
        print("BROWSER_RELEASE_PREPARED " + json.dumps(result, sort_keys=True), flush=True)
    except BaseException:
        if gateway is not None:
            gateway.db.close_pool()
        if mounted_runtime is not None:
            # A failed root-helper unmount must not lead rmtree into the venv.
            quota_helper_request({"operation": "runtime-unbind", "target": str(mounted_runtime)},
                                 helper=args.quota_helper)
        shutil.rmtree(root)
        if storage_created:
            shutil.rmtree(storage)
        raise
    finally:
        if harness is not None:
            harness.close()
        if gateway is not None:
            gateway.db.close_pool()


def quota_helper(args) -> None:
    """An explicitly operator-started root service for disposable fixtures only."""
    sys.path.insert(0, str(REPO / "Webservice/deploy"))
    from provision_cell_quota import provision
    import storage_quota as quota
    quota.initial_user_namespace()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                          ctypes.c_ulong, ctypes.c_void_p)
    libc.mount.restype = ctypes.c_int
    libc.umount2.argtypes = (ctypes.c_char_p, ctypes.c_int)
    libc.umount2.restype = ctypes.c_int

    def mount_pinned(source_fd, target_fd, *, readonly=False):
        source = f"/proc/self/fd/{source_fd}".encode("ascii")
        target = f"/proc/self/fd/{target_fd}".encode("ascii")
        flags = 4096  # MS_BIND
        if readonly:
            flags |= 32 | 1  # MS_REMOUNT | MS_RDONLY, bind-local only
        if libc.mount(source, target, None, flags, None) != 0:
            error = ctypes.get_errno()
            operation = "readonly bind remount" if readonly else "bind mount"
            raise OSError(error, f"pinned {operation}: {os.strerror(error)}")

    def unmount_pinned(mounted_fd):
        # The ledger's open descriptor itself pins this mount. Detach only that
        # attachment, then close the ledger fd; never force/unmount its source.
        target = f"/proc/self/fd/{mounted_fd}".encode("ascii")
        if libc.umount2(target, 2) != 0:  # MNT_DETACH
            error = ctypes.get_errno()
            raise OSError(error, f"pinned bind detach: {os.strerror(error)}")

    storage = args.storage_root.absolute()
    storage_fd = quota.open_nofollow(str(storage))
    proof_parent = args.proof_root.absolute()
    proof_fd = quota.open_nofollow(str(proof_parent), root_authority=True)
    socket_path = args.socket.absolute()
    listener = socket.socket(socket.AF_UNIX)
    bound = False
    mounts = {}
    proofs = []
    projects = set()
    try:
        info = os.fstat(storage_fd)
        if info.st_uid != args.service_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError("helper storage parent must be the service user's private scratch directory")
        evidence = quota.mount_evidence(storage_fd, str(storage))
        # This helper must never assign quotas on a physical/product filesystem.
        source = Path(evidence["source"]).name
        if os.major(info.st_dev) != 7 or not source.startswith("loop") or not source[4:].isdigit():
            raise RuntimeError("fixture helper requires an explicitly mounted disposable loop-image filesystem")
        if socket_path.parent != proof_parent or socket_path.exists():
            raise RuntimeError("new helper socket must be directly beneath the root-protected proof parent")
        listener.bind(str(socket_path))
        bound = True
        os.chown(socket_path, args.service_uid, -1)
        socket_path.chmod(0o600)
        listener.listen(8)
        listener.settimeout(0.25)
        print("BROWSER_QUOTA_HELPER_READY " + json.dumps({
            "pid": os.getpid(), "socket": str(socket_path), "storage_root": str(storage),
            "proof_root": str(proof_parent), "service_uid": args.service_uid,
        }, sort_keys=True), flush=True)

        def beneath(path):
            path = Path(path)
            if path == storage or not path.is_relative_to(storage):
                raise RuntimeError("helper request escaped configured disposable storage")
            fd = quota.open_nofollow(str(path))
            try:
                current = os.fstat(fd)
                if current.st_uid != args.service_uid:
                    raise RuntimeError("helper target must be owned by the configured fixture user")
            finally:
                os.close(fd)
            return path

        while True:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(120)
                operation, target = None, None
                try:
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != args.service_uid:
                        raise RuntimeError("quota helper peer UID refused")
                    with connection.makefile("rb") as stream:
                        raw = stream.readline(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024 or not raw.endswith(b"\n"):
                        raise RuntimeError("invalid bounded quota helper request")
                    request = json.loads(raw, object_pairs_hook=quota._unique_object)
                    if not isinstance(request, dict):
                        raise RuntimeError("quota helper request must be an object")
                    operation = request.get("operation")
                    if operation == "provision":
                        plan = request["plan"]
                        quota.validate_policy(plan, quota.PLAN_SCHEMA)
                        if plan["service_uid"] != args.service_uid:
                            raise RuntimeError("quota plan service UID mismatch")
                        if (sum(owner["budget_bytes"] for owner in plan["owners"]) > 2 * 1024**3
                                or sum(owner["budget_inodes"] for owner in plan["owners"]) > 40000):
                            raise RuntimeError("quota plan exceeds the disposable fixture reservation ceiling")
                        for owner in plan["owners"]:
                            for group in owner["groups"]:
                                for root in group["roots"]:
                                    target = beneath(root["path"])
                                    current = target.stat()
                                    if current.st_dev != info.st_dev:
                                        raise RuntimeError("quota root is outside the configured disposable image")
                                project = secrets.randbelow((1 << 30) - 1) + 1
                                while project in projects:
                                    project = secrets.randbelow((1 << 30) - 1) + 1
                                projects.add(project)
                                group["project_id"] = project
                        proof = proof_parent / ("fixture-" + secrets.token_hex(12))
                        proof.mkdir(mode=0o755)
                        proofs.append(proof)
                        provision(plan, str(proof), apply=True)
                        response = {"ok": True, "proof_root": str(proof)}
                    elif operation in {"runtime-bind", "runtime-unbind"}:
                        target = beneath(request["target"])
                        if target.name != "venv" or target.parent.name != "payload":
                            raise RuntimeError("only the scratch payload venv mount is supported")
                        if operation == "runtime-bind":
                            if target in mounts or target in mount_points():
                                raise RuntimeError("scratch runtime mount target must be new")
                            source_fd = quota.open_nofollow(str(VENV))
                            parent_fd = quota.open_nofollow(str(target.parent))
                            target_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                dir_fd=parent_fd)
                            try:
                                with os.scandir(target_fd) as entries:
                                    if next(entries, None) is not None or os.fstat(target_fd).st_dev != info.st_dev:
                                        raise RuntimeError("runtime target must be empty on the disposable image")
                                # Pin BOTH sides. A caller-owned ancestor rename
                                # cannot redirect a privileged mount/remount.
                                mount_pinned(source_fd, target_fd)
                                mounted_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                     dir_fd=parent_fd)
                                mounts[target] = mounted_fd
                                original, mounted = os.fstat(source_fd), os.fstat(mounted_fd)
                                if ((original.st_dev, original.st_ino) != (mounted.st_dev, mounted.st_ino)
                                        or quota.mount_id(source_fd) == quota.mount_id(mounted_fd)
                                        or quota.mount_id(target_fd) == quota.mount_id(mounted_fd)):
                                    raise RuntimeError("runtime bind inode/mount identity mismatch")
                                mount_pinned(source_fd, mounted_fd, readonly=True)
                                if not os.fstatvfs(mounted_fd).f_flag & os.ST_RDONLY:
                                    raise RuntimeError("runtime bind did not become read-only")
                            finally:
                                os.close(target_fd)
                                os.close(parent_fd)
                                os.close(source_fd)
                        else:
                            if target not in mounts:
                                raise RuntimeError("runtime mount was not created by this helper")
                            mounted_fd = mounts[target]
                            unmount_pinned(mounted_fd)
                            os.close(mounted_fd)
                            del mounts[target]
                        response = {"ok": True}
                    else:
                        raise RuntimeError("unknown disposable quota helper operation")
                except Exception as exc:
                    if operation == "runtime-bind" and target in mounts:
                        mounted_fd = mounts[target]
                        try:
                            unmount_pinned(mounted_fd)
                            os.close(mounted_fd)
                            del mounts[target]
                        except OSError:
                            pass  # Ledger retains only this owned mount for shutdown cleanup.
                    response = {"ok": False, "error": str(exc)[:4096]}
                try:
                    connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                except OSError:
                    pass  # A gone client cannot terminate the operator's helper.
    finally:
        listener.close()
        if bound:
            socket_path.unlink(missing_ok=True)
        failures = []
        for target, mounted_fd in mounts.items():
            try:
                unmount_pinned(mounted_fd)
            except OSError as exc:
                failures.append(str(exc))
            finally:
                os.close(mounted_fd)
        for proof in proofs:
            shutil.rmtree(proof)
        os.close(storage_fd)
        os.close(proof_fd)
        print("BROWSER_QUOTA_HELPER_CLEANED " + json.dumps({"pid": os.getpid(), "errors": failures}), flush=True)
        if failures:
            raise RuntimeError("owned disposable runtime mounts failed to unmount")


def refuse_gateway_egress(*ports: int) -> None:
    """Allow only exact own origin/official-wire fixture sockets, never a vendor."""
    def audit(event, arguments):
        if event == "socket.connect":
            connection, address = arguments
            if connection.family in (socket.AF_INET, socket.AF_INET6):
                if connection.family != socket.AF_INET or address[0] != "127.0.0.1" or address[1] not in ports:
                    raise PermissionError("browser fixture refuses non-fixture gateway network connections")
        elif event == "socket.getaddrinfo":
            host, destination_port = arguments[:2]
            if host != "127.0.0.1" or destination_port not in ports:
                raise PermissionError("browser fixture refuses non-fixture DNS resolution")
    sys.addaudithook(audit)


def release_helpers():
    path = REPO / "Webservice/gateway/tests/test_releases_ci.py"
    spec = importlib.util.spec_from_file_location("browser_fixture_release_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def seed_releases(client) -> list[dict]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from gateway import releases
    helpers = release_helpers()
    signer = Ed25519PrivateKey.generate()
    helpers._STAGE_SIGNER = signer
    releases._RELEASE_PUBLIC_KEYS[releases._CI_KEY_ID] = signer.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    headers = {"Authorization": "Bearer " + os.environ["CHUDWARE_GATEWAY_RELEASE_TOKEN"]}
    for platform in ("linux", "windows", "macos"):
        files = helpers._stage(platform=platform)
        body = helpers._body(signer, files, platform=platform)
        body["notes_url"] = "https://fixture.invalid/disposable-test-key-not-a-release"
        response = await client.post("/api/releases/ci/publish", json=body, headers=headers)
        response.raise_for_status()
    response = await client.get("/api/admin/release-manifests", headers={"X-Dev-User": A})
    response.raise_for_status()
    rows = response.json()
    by_platform = {row["platform"]: row for row in rows}
    if (by_platform["linux"].get("promotable") is not True
            or any(by_platform[p].get("promotable") is not False for p in ("windows", "macos"))
            or any(row.get("has_stable_pair") is not True or row.get("evidence_complete") is not True
                   for row in rows)):
        raise RuntimeError("real release API did not produce the intended complete-evidence fixture gating")
    return [{key: row.get(key) for key in ("channel", "version", "stable_version", "platform", "arch",
                                         "format", "promotable", "has_stable_pair", "evidence_complete")}
            for row in rows]


async def seed_cached_box(origin: str) -> dict:
    from websockets.asyncio.client import connect
    async with connect(origin.replace("http://", "ws://") + "/engine",
                       additional_headers={"X-Dev-User": A}, proxy=None,
                       max_size=4 * 1024 * 1024, open_timeout=40) as ws:
        counter = 0
        async def rpc(method, params=None):
            nonlocal counter
            counter += 1
            await ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
            async with asyncio.timeout(60):
                while True:
                    frame = json.loads(await ws.recv())
                    if frame.get("id") != counter:
                        continue
                    if "result" not in frame or frame["result"].get("ok") is False:
                        raise RuntimeError(f"real gateway CAD RPC {method} refused: {frame}")
                    return frame["result"]
        await rpc("hello", {"client": "gui", "proto": 1})
        created = await rpc("doc_new", {"kind": "part", "name": "Browser qualification box"})
        ident = created["doc_id"]
        await rpc("doc_apply_ops", {"doc_id": ident, "base_revision": -1, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Qualification box",
             "params": {"length": 40, "width": 30, "height": 20}}}]})
        saved = await rpc("doc_save", {"doc_id": ident, "path": BOX_PATH})
        if saved.get("cache", {}).get("written") is not True:
            raise RuntimeError(f"built part cache was not saved: {saved}")
        await rpc("doc_close", {"doc_id": ident})
        opened = await rpc("doc_open", {"path": BOX_PATH})
        if (opened.get("cache_hit") is not True or opened.get("warm") is not True
                or opened.get("cache_trust") != "verified"
                or opened.get("cache", {}).get("features_executed") != 0):
            raise RuntimeError(f"saved box did not reopen warm from verified cache: {opened}")
        await rpc("doc_close", {"doc_id": opened["doc_id"]})
        return {"path": BOX_PATH, "dimensions_mm": [40, 30, 20], "expected_volume_mm3": 24000,
                "save_cache": saved["cache"], "reopen": {key: opened.get(key) for key in
                ("cache_hit", "warm", "cache_trust", "cache")}}


async def seed_provider_replay(origin: str, harness) -> dict:
    """Exercise actual gateway/cell/native SSE and signed-history transport before READY."""
    from websockets.asyncio.client import connect
    wire = harness.provider_wire
    async with connect(origin.replace("http://", "ws://") + "/engine",
                       additional_headers={"X-Dev-User": A}, proxy=None,
                       max_size=4 * 1024 * 1024, open_timeout=40) as ws:
        counter = 0
        sessions = []
        async def rpc(method, params=None, *, held=False):
            nonlocal counter
            counter += 1
            ident = counter
            await ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
            first_delta = False
            async with asyncio.timeout(60):
                while True:
                    frame = json.loads(await ws.recv())
                    if frame.get("event") == "agent.event" and frame.get("data", {}).get("kind") == "agent.session":
                        sessions.append((frame["data"]["run_id"], frame["data"]["chudcode_session_id"]))
                    if held and not first_delta and TEXT.strip() in json.dumps(frame):
                        first_delta = True
                        row = await asyncio.to_thread(wire.wait_request, BYOK[A], offset=offset, primary=True)
                        if row.completed or not row.first_delta.is_set():
                            raise RuntimeError("native browser provider delta was buffered until completion")
                        wire.release(BYOK[A])
                    if frame.get("id") != ident:
                        continue
                    if "result" not in frame or frame["result"].get("ok") is False:
                        raise RuntimeError(f"actual provider fixture RPC {method} refused: {frame}")
                    if held and not first_delta:
                        raise RuntimeError("actual provider fixture turn returned without live SSE delta")
                    return frame["result"]
        await rpc("hello", {"client": "gui", "proto": 1})
        handles = await rpc("agent_handles_new")
        offset = len(wire.records)
        wire.hold(BYOK[A])
        await rpc("agent_admin_turn", {**handles, "provider": "anthropic", "model": "claude-sonnet-4-5",
            "request": "Local browser first replay turn; no tools.", "workspace_dir": "/work"}, held=True)
        initial = await asyncio.to_thread(wire.wait_request, BYOK[A], offset=offset, primary=True)
        cell_id = harness.principal(A).cell_id
        native_identity = harness.native_session_identity(cell_id)
        continued = await rpc("agent_handles_new", {"session_id": handles["session_id"]})
        offset = len(wire.records)
        wire.hold(BYOK[A])
        await rpc("agent_admin_turn", {**continued, "provider": "anthropic", "model": "claude-sonnet-4-5",
            "request": "Local browser second replay turn; preserve context; no tools.", "workspace_dir": "/work"}, held=True)
        replay = await asyncio.to_thread(wire.wait_request, BYOK[A], offset=offset, primary=True)
        blocks = [block for message in replay.body["messages"] if message["role"] == "assistant"
                  for block in message["content"] if isinstance(block, dict)]
        checks = {
            "bound_both_runs": {run_id for run_id, _ in sessions} == {handles["run_id"], continued["run_id"]},
            "stable_public_handle": bool(sessions) and all(handle == handles["session_id"] for _, handle in sessions),
            "same_native_session": harness.native_session_identity(cell_id) == native_identity,
            "stable_system": initial.body["system"] == replay.body["system"],
            "stable_tools": initial.body.get("tools") == replay.body.get("tools"),
            "marked_cache_prefix": '"cache_control"' in json.dumps(initial.body["system"]) + json.dumps(initial.body.get("tools")),
            "exact_signed_thinking": any(block.get("type") == "thinking" and block.get("thinking") == THINKING
                                        and block.get("signature") == SIGNATURE for block in blocks),
            "exact_text": any(block.get("type") == "text" and block.get("text") == TEXT for block in blocks),
        }
        if not all(checks.values()):
            rows = [{"model": row.body.get("model"), "tool_names": [tool.get("name") for tool in row.body.get("tools", [])],
                     "roles": [message.get("role") for message in row.body.get("messages", [])],
                     "system_sha256": hashlib.sha256(json.dumps(row.body.get("system"), sort_keys=True).encode()).hexdigest()}
                    for row in wire.records[offset:]]
            raise RuntimeError("actual provider fixture failed native session/signed-thought/cache-prefix replay: " +
                               json.dumps({"checks": checks, "sessions": sessions, "requests": rows}, sort_keys=True))
        for cell in harness.running.values():
            if any(target.startswith(("api.anthropic.com:", "api.openai.com:")) for target in cell.broker.targets):
                raise RuntimeError("actual provider traffic reached the tool CONNECT lane")
        return {"transport": "real gateway-owned broker /run/provider.sock, cell/actor loopback18081",
                "wire": "local official Anthropic messages SSE; synthetic fixture keys only",
                "first_delta_before_completion": True, "same_native_session": native_identity[1],
                "exact_fixture_thinking_signature_text_replayed": True, "stable_marked_cache_prefix": True,
                "fixture_signature_is_not_provider_authenticity": True, "actual_cache_hits_not_claimed": True}


def write_metadata(path: Path | None, value: dict) -> None:
    if path is None:
        return
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def mount_points() -> set[Path]:
    with open("/proc/self/mountinfo", "rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("oversized mount table")
    result = set()
    for line in raw.decode("utf-8").splitlines():
        words = line.split()
        if len(words) < 7:
            raise RuntimeError("invalid mount table")
        point = words[4]
        for escaped, literal in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            point = point.replace(escaped, literal)
        result.add(Path(point))
    return result


def remove_unmounted_scratch(path: Path, retained: list[str], mounts: set[Path]) -> None:
    """Never walk into a root-owned bind mount, even though its path is scratch."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    if path in mounts:
        retained.append(str(path))
        return
    for child in path.iterdir():
        remove_unmounted_scratch(child, retained, mounts)
    if not any(path.iterdir()):
        path.rmdir()


async def serve(args) -> None:
    import httpx
    import uvicorn
    class FixtureServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # This CLI owns SIGTERM; uvicorn's signal re-raise would otherwise
            # interrupt our own deterministic PostgreSQL/cell cleanup.
            yield
    root = args.root.absolute() if args.root else Path(tempfile.mkdtemp(prefix="br-", dir="/tmp"))
    owned_directory(root)
    state = None if args.admin_only else load_state(root)
    storage = Path(state["storage_root"]) if state else root / "storage"
    if args.admin_only:
        storage.mkdir(mode=0o700)
    if state:
        venv = Path(state["payload"]) / "venv"
        known_runtime = VENV.stat()
        mounted_runtime = venv.stat()
        if (venv not in mount_points() or not os.statvfs(venv).f_flag & os.ST_RDONLY
                or (known_runtime.st_dev, known_runtime.st_ino) !=
                   (mounted_runtime.st_dev, mounted_runtime.st_ino)):
            raise RuntimeError("CAD requires the prepared read-only known venv bind in this mount namespace")
    if args.metadata is not None and (args.metadata.absolute().is_relative_to(root)
                                      or args.metadata.absolute().is_relative_to(storage)):
        raise RuntimeError("metadata must be outside disposable scratch roots")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(128)
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    harness = gateway = server = None
    serving = None
    cell_pids = []
    ready = False
    retained = []
    try:
        with ExitStack() as resources:
            dsn = resources.enter_context(open_disposable_postgres(root / "pg"))
            configure(root, storage, dsn, origin)
            gateway = gateway_modules()
            resources.callback(close_gateway_pools)
            gateway.db.migrate()
            seed_accounts(gateway)
            wire = None
            if state:
                if state.get("sdk_blocked") or not (Path(state["payload"]) / "bin/chud").is_file():
                    raise RuntimeError("CAD/provider serve requires an already-local verified pinned chud and actual SDK")
                wire = OfficialWireFixture(BYOK.values())
                resources.callback(wire.close)
                gateway.provider_broker._broker = gateway.provider_broker.ProviderBroker(
                    upstream_origins={provider: wire.origin for provider in ("openai", "anthropic")})
            if state:
                harness = Harness(root, gateway, None, organization=state["organization"],
                                  runtime_configuration={"quota_proof_root": state["quota_proof_root"]},
                                  provider_wire=wire)
                harness.payload = SimpleNamespace(path=Path(state["payload"]), bwrap=state["bwrap"])
                # The sole fixture lifecycle seam: own listener/process activation,
                # not a product unit and not an HTTP/RPC/daemon implementation.
                gateway.cells.start_cell = harness.activate
                gateway.cells.service_state = harness.service_state
                gateway.cells.service_units = harness.service_units
            from gateway.app import app
            refuse_gateway_egress(port, *((wire.port,) if wire else ()))
            server = FixtureServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                  access_log=False, lifespan="on", timeout_graceful_shutdown=10))
            loop = asyncio.get_running_loop()
            def request_stop():
                server.should_exit = True
            loop.add_signal_handler(signal.SIGTERM, request_stop)
            loop.add_signal_handler(signal.SIGINT, request_stop)
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(45):
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError("gateway exited before startup")
                        await asyncio.sleep(0.05)
                async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=60) as client:
                    rows = await seed_releases(client)
                cad = await seed_cached_box(origin) if state else None
                provider = await seed_provider_replay(origin, harness) if state else None
                if server.should_exit or serving.done():
                    raise RuntimeError("fixture stopped before readiness")
                metadata = {
                    "schema": STATE_SCHEMA, "pid": os.getpid(), "origin": origin,
                    "admin_url": origin + "/admin?dev_user=" + A + "#releases",
                    "app_url": origin + "/app/?dev_user=" + A,
                    "tenant_app_url": origin + "/app/?dev_user=" + B,
                    "authentication": "Scratch CHUDWARE_WEB_DEV query mints dev_user HttpOnly cookie; use separate browser contexts for A and B",
                    "admin_email": A, "tenant_email": B, "root": str(root), "storage_root": str(storage),
                    "postgres_socket_root": str(root / "pg"),
                    "cells": [{"cell_id": cell.principal.cell_id, "pid": cell.proc.pid,
                               "directory": str(cell.directory)} for cell in harness.running.values()] if harness else [],
                    "release_rows": rows, "cad": cad, "provider": provider, "wasm_assets": str(ASSETS),
                    "served_wasm_assets": str(gateway.settings.app_dir),
                    "selectors": {"release_rows": "#rel-rows > tr", "action_cell": ".col-act",
                                  "linux_action": ".col-act button.btn.primary",
                                  "native_refusal": ".col-act .ac-dim",
                                  "promotion_status": "#rel-status", "wasm_canvas": "#canvas",
                                  "wasm_status": "#status", "workspace_open_dialog": "#chudware-ws-browse"},
                    "interactions": [
                        "Admin: inspect Linux beta has exactly one Promote to stable; Windows/macOS beta have none and publisher verification required.",
                        "Click Linux Promote to stable and accept confirmation; inspect stable Linux 0.1.0 row and unchanged native beta rows.",
                        "CAD: wait runtimeReady and no __chudwareUnsupported; focus #canvas, press Control+O; in #chudware-ws-browse double-click " + BOX_PATH + "; press F to fit if needed.",
                        "Provider: open Copilot, select Anthropic claude-sonnet-4-5 or OpenAI gpt-4o-mini and send an Ask turn; observe LOCAL-WIRE-READY streamed from the local fixture, never a live LLM.",
                        "Keep browser origin restrictions to the gateway; the upstream official-wire fixture remains host-only and cells can reach it only through their scoped provider.sock.",
                        "Use a fresh isolated Chromium profile; restrict browser requests/navigation to this exact origin and capture the real canvas, not only loading chrome.",
                    ],
                    "fixture_limits": [
                        "Source-approved release artifacts are inert STAGING TEST bytes; signed reports use synthetic identities, not real CTest/platform/signing/CI evidence.",
                        "Fresh disposable release-ci-v1 trust override is local to this process; no production key or publisher policy used.",
                        "Windows/macOS deliberately have no owner-authorized native publisher verification.",
                        "Dev cookie/account fixtures are not password/OAuth/edge authentication qualification.",
                        "Gateway sockets/DNS allow only this origin and the exact own local official-wire fixture; ordinary CONNECT tools retain the separate refusing broker.",
                        "Real pinned SDK/provider transport and signed-history replay use synthetic keys/responses; no live LLM, genuine signature, cache hit, BYOK validation, pool or subscription qualification.",
                        "Existing dirty prebuilt WASM/core and acceptance-only read-only .venv-mcp, not reproducible or authenticated production runtime.",
                        "Kernel quota policy is parent-provisioned scratch authority, not installed product storage/cgroup attestation.",
                        "Admin-only mode has no cell/CAD qualification." if args.admin_only else "Root proof and bind/image mounts need parent private-namespace cleanup after SIGTERM.",
                    ],
                }
                write_metadata(args.metadata, metadata)
                ready = True
                print("BROWSER_RELEASE_READY " + json.dumps(metadata, sort_keys=True), flush=True)
                await serving
            finally:
                if server is not None:
                    server.should_exit = True
                try:
                    if serving is not None:
                        await serving
                finally:
                    try:
                        if harness is not None:
                            harness.close()
                            cell_pids = [cell.proc.pid for cell in harness.running.values()
                                         if cell.proc is not None and cell.proc.poll() is not None]
                    finally:
                        close_gateway_pools()
    finally:
        listener.close()
        runtime_unbind_error = None
        if state:
            try:
                quota_helper_request({"operation": "runtime-unbind", "target": state["payload"] + "/venv"},
                                     helper=state["quota_helper"])
            except (OSError, RuntimeError) as exc:
                # Retain any remaining mount below; never remove the bound venv.
                runtime_unbind_error = str(exc)
        mounts = mount_points()
        remove_unmounted_scratch(root, retained, mounts)
        if state:
            remove_unmounted_scratch(storage, retained, mounts)
        cleanup = {"schema": STATE_SCHEMA, "pid": os.getpid(), "ready_was_emitted": ready,
                   "own_cell_pids_reaped": cell_pids, "postgres_socket_root_removed": not (root / "pg").exists(),
                   "storage_root_removed": not storage.exists(), "root_removed": not root.exists(),
                   "mounts_retained_for_root_cleanup": retained, "runtime_unbind_error": runtime_unbind_error}
        if args.metadata:
            write_metadata(args.metadata.with_name(args.metadata.name + ".cleanup.json"), cleanup)
        print("BROWSER_RELEASE_CLEANED " + json.dumps(cleanup, sort_keys=True), flush=True)
        if runtime_unbind_error is not None:
            raise RuntimeError("explicit quota helper could not unbind the owned runtime: " + runtime_unbind_error)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("prepare", help="Stage scratch DB/accounts/runtime and request real quotas/readonly bind from the explicit root helper; launch no cells")
    pre.add_argument("--root", type=Path, required=True)
    pre.add_argument("--storage-root", type=Path, required=True, help="New private child directory on the operator's disposable project-quota mount")
    pre.add_argument("--quota-helper", type=Path, required=True, help="Explicit operator-started root quota-helper socket in this private loop-image namespace")
    pre.add_argument("--chud", type=Path, help="Already-local CLI candidate, verified against the repository pin; no discovery fallback if supplied")
    run = sub.add_parser("serve", help="Serve until SIGTERM, cleaning only owned processes/resources")
    run.add_argument("--root", type=Path)
    run.add_argument("--admin-only", action="store_true")
    run.add_argument("--port", type=int, default=0, help="Loopback only; zero selects a free port")
    run.add_argument("--metadata", type=Path, help="New private JSON evidence/instruction file outside scratch roots")
    helper = sub.add_parser("quota-helper", help="Explicit root-only disposable loop-image fixture supervisor; never starts a product unit")
    helper.add_argument("--storage-root", type=Path, required=True)
    helper.add_argument("--proof-root", type=Path, required=True)
    helper.add_argument("--socket", type=Path, required=True)
    helper.add_argument("--service-uid", type=int, required=True)
    args = parser.parse_args()
    if args.command == "quota-helper":
        if os.geteuid() != 0 or args.service_uid <= 0:
            parser.error("quota-helper requires explicit root authority and a non-root fixture UID")
    elif os.geteuid() == 0:
        parser.error("run prepare/serve as the ordinary fixture user, never as root")
    if args.command == "serve":
        if not args.admin_only and args.root is None:
            parser.error("CAD serve requires --root from prepare; use --admin-only otherwise")
        if args.admin_only and args.root is not None:
            parser.error("admin-only creates its own disposable root; do not supply a prepared root")
        if not 0 <= args.port <= 65535:
            parser.error("port must be 0..65535")
        if args.metadata is not None and (args.metadata.exists() or args.metadata.with_name(args.metadata.name + ".cleanup.json").exists()):
            parser.error("metadata and cleanup report paths must be new")
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.command == "quota-helper":
            quota_helper(args)
        else:
            asyncio.run(serve(args))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("BROWSER_RELEASE_REFUSED " + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
