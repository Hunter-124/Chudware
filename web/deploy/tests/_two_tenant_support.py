"""Scratch-only real gateway/hostd/actor fixtures for two-tenant acceptance.

The authenticated worker runtime lock is not available on development machines.
This harness alone binds .venv-mcp read-only into the disposable payload and
stages its actual editable RPC SDK. It uses production launchers and unchanged
services, never substitutes a daemon/worker, and never consults a production
database or profile. The resource test alone uses an auto-collected, uniquely
named scratch systemd scope; no persistent units or existing units are changed.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from contextlib import asynccontextmanager, contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from anyio import ClosedResourceError
from _provider_wire_support import OfficialWireFixture

REPO = Path(__file__).resolve().parents[3]
CELL = REPO / "Webservice/deploy/cell"
BUILD = REPO / "core/build-occt-linux/bin"
VENV = REPO / ".venv-mcp"
A, B, C = (f"tenant-{name}@acceptance.invalid" for name in "abc")
AMBIENT = "operator-ambient-tenant-acceptance-7cb71bb1"
BYOK = {email: f"sk-fake-acceptance-{index}-never-a-provider-key" for index, email in enumerate((A, B, C))}


def private_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def fixture_storage_root() -> Path:
    raw = os.environ.get("CHUDWARE_TEST_CELL_STORAGE_ROOT", "")
    helper = os.environ.get("CHUDWARE_TEST_CELL_QUOTA_HELPER", "")
    if not raw or not helper:
        raise RuntimeError("real quota fixtures require explicitly authorized CHUDWARE_TEST_CELL_STORAGE_ROOT and CHUDWARE_TEST_CELL_QUOTA_HELPER")
    root = Path(raw).absolute()
    info = root.stat()
    if (root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError("cell fixture storage parent must be private, owned, and no-follow")
    return root


def quota_helper_request(request: dict, *, helper: Path | str | None = None) -> dict:
    path = str(helper) if helper is not None else os.environ.get("CHUDWARE_TEST_CELL_QUOTA_HELPER", "")
    if not path:
        raise RuntimeError("explicit disposable quota helper is unavailable")
    raw = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("oversized disposable quota request")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(120)
        connection.connect(path)
        connection.sendall(raw)
        with connection.makefile("rb") as stream:
            response = stream.readline(65537)
    if len(response) > 65536 or not response.endswith(b"\n"):
        raise RuntimeError("invalid disposable quota helper reply")
    value = json.loads(response)
    if value.get("ok") is not True:
        raise RuntimeError("disposable quota helper refused: " + str(value.get("error", value)))
    return value


def provision_test_quotas(plan: dict) -> Path:
    """Only the operator-started, UID-authenticated disposable root helper applies."""
    root = fixture_storage_root()
    for owner in plan["owners"]:
        for group in owner["groups"]:
            for record in group["roots"]:
                path = Path(record["path"])
                if path == root or not path.is_relative_to(root):
                    raise RuntimeError("test quota roots must be beneath the explicit disposable storage parent")
    return Path(quota_helper_request({"operation": "provision", "plan": plan})["proof_root"])


def quota_plan(harness: Harness) -> dict:
    owners = {}
    project = 41000
    for principal in {p.cell_id: p for p in harness.principals.values()}.values():
        directory = harness.gateway.cells.prepare_cell(principal)
        state = directory / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        roots = [("workspace", principal.root), ("state", state)]
        if principal.team_id:
            owner = "user:" + harness.gateway.accounts.get_account(C).uid
        else:
            owner = principal.workspace_key
            bucket = harness.gateway.cells.personal_bucket_root(
                next(email for email in (A, B, C)
                     if harness.gateway.accounts.get_account(email).uid == principal.account_uid))
            bucket.mkdir(mode=0o700, parents=True, exist_ok=True)
            roots.append(("bucket", bucket))
        records = []
        for kind, path in roots:
            info = path.stat()
            records.append({"workspace_key": principal.workspace_key, "kind": kind,
                            "path": str(path), "device": info.st_dev, "inode": info.st_ino})
        row = owners.setdefault(owner, {"owner_key": owner, "budget_bytes": 0,
                                       "budget_inodes": 0, "groups": []})
        # A small real hard bound, not an assertion about production plan capacity.
        budget, inodes = 512 * 1024 * 1024, 10000
        row["budget_bytes"] += budget
        row["budget_inodes"] += inodes
        device = records[0]["device"]
        group = next((group for group in row["groups"] if group["roots"][0]["device"] == device), None)
        if group is None:
            group = {"project_id": project, "hard_bytes": 0, "hard_inodes": 0, "roots": []}
            row["groups"].append(group)
            project += 1
        group["hard_bytes"] += budget
        group["hard_inodes"] += inodes
        group["roots"].extend(records)
    return {"schema": "chudware-cell-quota-plan-v1", "service_uid": os.getuid(),
            "owners": list(owners.values())}


@contextmanager
def open_disposable_postgres(root: Path | None = None, *, retain=False):
    """Open only an owned socket-only cluster; retained clusters can be restarted."""
    bindir = Path(shutil.which("initdb") or "/missing").parent
    if not all((bindir / name).is_file() for name in ("initdb", "postgres", "pg_ctl")):
        raise RuntimeError("disposable PostgreSQL needs initdb, postgres and pg_ctl on PATH")
    if os.geteuid() == 0:
        raise RuntimeError("disposable PostgreSQL initdb must run as an unprivileged user")
    root = Path(tempfile.mkdtemp(prefix="tc-pg-", dir="/tmp")) if root is None else root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = False
    try:
        if not (root / "d").exists():
            subprocess.run([str(bindir / "initdb"), "-D", str(root / "d"), "-U", "postgres",
                            "-A", "trust", "-E", "UTF8", "--locale=C", "--no-sync"],
                           check=True, capture_output=True, text=True, timeout=60)
        if (root / "d/postmaster.pid").exists():
            raise RuntimeError("scratch PostgreSQL cluster must be stopped before opening")
        # Mark the attempt before pg_ctl: cancellation after it spawns must
        # still clean the cluster we alone just started.
        started = True
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(root / "d"), "-l", str(root / "postgres.log"),
                        "-w", "-o", f"-k {root} -h '' -F -c full_page_writes=off -c synchronous_commit=off", "start"],
                       check=True, capture_output=True, text=True, timeout=60)
        yield f"postgresql://postgres@/postgres?host={root}"
    finally:
        if started:
            result = subprocess.run([str(bindir / "pg_ctl"), "-D", str(root / "d"), "-m", "immediate", "stop"],
                                    capture_output=True, timeout=30)
            if result.returncode and (root / "d/postmaster.pid").exists():
                raise RuntimeError("owned scratch PostgreSQL cluster failed to stop")
        if not retain:
            shutil.rmtree(root)


@pytest.fixture(scope="module")
def disposable_postgres():
    context = open_disposable_postgres()
    try:
        dsn = context.__enter__()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    try:
        yield dsn
    finally:
        context.__exit__(None, None, None)


@contextmanager
def open_real_payload(*, directory: Path | None = None, retain=False, chud_candidate: Path | None = None):
    if sys.platform != "linux":
        raise RuntimeError("real tenant cells require Linux namespaces")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("real tenant cells require bubblewrap")
    for path in (BUILD / "chudware-hostd", BUILD / "libchudware.so", VENV / "bin/python3"):
        if not path.is_file():
            raise RuntimeError(f"real tenant cells require prebuilt artifact: {path}")
    probe = subprocess.run([bwrap, "--unshare-user", "--unshare-pid", "--unshare-net",
                            "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
                            "--symlink", "usr/lib", "/lib", "--", "/usr/bin/true"],
                           capture_output=True, text=True, timeout=10)
    if probe.returncode:
        raise RuntimeError("bubblewrap/user namespaces unavailable: " + probe.stderr.strip())
    launcher = CELL / "chudware-worker"
    assert launcher.is_file(), f"release-staged real worker launcher missing: {launcher}"
    root = Path(tempfile.mkdtemp(prefix="tc-payload-", dir="/tmp")) if directory is None else directory
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        yield _stage_real_payload(root, bwrap, chud_candidate=chud_candidate)
    finally:
        if not retain:
            shutil.rmtree(root)


def _stage_real_payload(root, bwrap, *, chud_candidate=None):
    launcher = CELL / "chudware-worker"
    payload = root / "payload"
    (payload / "bin").mkdir(parents=True)
    (payload / "venv").mkdir()
    shutil.copytree(REPO / "contracts", payload / "contracts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for source in CELL.iterdir():
        if source.is_file() and (source.name.startswith("chudware-") or source.suffix == ".py"):
            shutil.copy2(source, payload / "bin" / source.name)
    shutil.copy2(launcher, payload / "bin/chudware-worker")
    for source in BUILD.iterdir():
        if source.name == "chudware-hostd" or ".so" in source.name:
            shutil.copy2(source, payload / "bin" / source.name)
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "test_*", "*.pyc", "corpora")
    shutil.copytree(REPO / "services", payload / "services", ignore=ignore)
    shutil.copytree(REPO / "assets", payload / "assets", ignore=ignore)
    # Prompt/template files only; no user/runtime provider state is staged.
    agent_assets = REPO / "agent"
    if agent_assets.is_dir():
        shutil.copytree(agent_assets, payload / "agent", ignore=ignore)
    # The development venv's SDK is an editable install pointing outside the
    # cell. Stage that real installed package, not a site hook or fake SDK.
    sdk = subprocess.run([str(VENV / "bin/python3"), "-I", "-c",
                          "import pathlib,chudcode_rpc; print(pathlib.Path(chudcode_rpc.__file__).parent)"],
                         capture_output=True, text=True, timeout=15)
    sdk_blocked = ""
    if sdk.returncode == 0:
        shutil.copytree(Path(sdk.stdout.strip()), payload / "chudcode_rpc", ignore=ignore)
    else:
        sdk_blocked = "installed chudcode_rpc unavailable: " + sdk.stderr.strip()
    candidates = [Path(chud_candidate)] if chud_candidate is not None else [BUILD / "chud", REPO / "vendor/chudcode/chud"]
    if chud_candidate is None and (executable := shutil.which("chud")):
        candidates.append(Path(executable))
    verified, evidence = None, []
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        result = subprocess.run([sys.executable, str(REPO / "scripts/pin_chudcode.py"),
                                 "verify", "--target", "linux-x86_64", "--input", str(candidate)],
                                capture_output=True, text=True, timeout=30)
        evidence.append(f"{candidate}: rc={result.returncode}; {(result.stderr or result.stdout).strip()}")
        if result.returncode == 0:
            verified = candidate
            shutil.copy2(candidate, payload / "bin/chud")
            break
    reason = "" if verified is not None else "no already-local pinned/verified chud binary; " + ("; ".join(evidence) or "no local candidates")
    return SimpleNamespace(path=payload, bwrap=bwrap, chud=verified, chud_blocked=reason,
                           sdk_blocked=sdk_blocked,
                           runtime="acceptance-only read-only .venv-mcp plus its real editable SDK package; authenticated runtime lock unavailable")


@pytest.fixture(scope="module")
def real_payload():
    if not os.environ.get("CHUDWARE_TEST_CELL_STORAGE_ROOT") and not os.environ.get("CHUDWARE_TEST_CELL_QUOTA_HELPER"):
        pytest.skip("real cell quota fixtures need an explicitly operator-started disposable quota helper")
    parent = fixture_storage_root()
    directory = Path(tempfile.mkdtemp(prefix="tc-payload-", dir=parent))
    context = open_real_payload(directory=directory, retain=True)
    payload = None
    mounted = False
    try:
        try:
            payload = context.__enter__()
        except RuntimeError as exc:
            pytest.skip(str(exc))
        quota_helper_request({"operation": "runtime-bind", "target": str(payload.path / "venv")})
        mounted = True
        yield payload
    finally:
        try:
            if mounted:
                quota_helper_request({"operation": "runtime-unbind", "target": str(payload.path / "venv")})
        finally:
            if payload is not None:
                context.__exit__(None, None, None)
            shutil.rmtree(directory)


class RefusingBroker:
    """Record real Unix egress CONNECT requests; never dial an upstream host."""
    def __init__(self, path: Path):
        self.targets: list[str] = []
        self.requests: list[bytes] = []
        self.lock = threading.Condition()
        self.done = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(path))
        path.chmod(0o600)
        self.socket.listen(32)
        self.socket.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.done.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                data = b""
                try:
                    while b"\r\n\r\n" not in data and len(data) < 16 * 1024:
                        chunk = connection.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                    words = data.split(b"\r\n", 1)[0].split()
                    with self.lock:
                        self.requests.append(data)
                        if len(words) == 3 and words[0] == b"CONNECT":
                            self.targets.append(words[1].decode("ascii"))
                        self.lock.notify_all()
                    connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except (OSError, UnicodeError):
                    pass

    def wait_target(self, timeout=15, offset=0, target=None):
        with self.lock:
            def found():
                candidates = self.targets[offset:]
                return bool(candidates) if target is None else target in candidates
            assert self.lock.wait_for(found, timeout), (
                "expected real CONNECT did not reach scratch refusing broker", target, self.targets)
            return self.targets[offset] if target is None else target

    def close(self):
        self.done.set()
        self.socket.close()
        self.thread.join(3)
        assert not self.thread.is_alive(), "scratch egress broker leaked"


class EngineSocket:
    """Bounded receive demultiplexing for actual TestClient gateway websockets."""
    def __init__(self, client, email, team=""):
        self.context = client.websocket_connect("/engine" + (f"?team={team}" if team else ""),
                                                headers={"x-dev-user": email})
        self.ws = self.context.__enter__()
        self.condition = threading.Condition()
        self.replies, self.events = {}, []
        self.closed, self.receive_error = None, None
        self.counter = 0
        self.diagnostics = lambda: ""
        self.thread = threading.Thread(target=self._receive, daemon=True)
        self.thread.start()

    def _receive(self):
        try:
            while True:
                message = self.ws.receive()
                with self.condition:
                    if message["type"] == "websocket.close":
                        self.closed = message.get("code", 1000)
                        self.condition.notify_all()
                        return
                    frame = json.loads(message.get("text") or message["bytes"])
                    if "id" in frame:
                        self.replies[frame["id"]] = frame
                    else:
                        self.events.append(frame)
                    self.condition.notify_all()
        except BaseException as exc:
            with self.condition:
                self.receive_error = exc
                self.condition.notify_all()

    def send(self, method, params=None):
        self.counter += 1
        self.ws.send_json({"id": self.counter, "method": method, "params": params or {}})
        return self.counter

    def response(self, ident, timeout=30):
        with self.condition:
            assert self.condition.wait_for(lambda: ident in self.replies or self.closed is not None or
                                           self.receive_error is not None, timeout), f"gateway RPC {ident} timed out; {self.diagnostics()}"
            assert ident in self.replies, f"gateway closed={self.closed}, receive error={self.receive_error!r}; {self.diagnostics()}"
            return self.replies.pop(ident)

    def request(self, method, params=None, timeout=30):
        return self.response(self.send(method, params), timeout)

    def ok(self, method, params=None, timeout=30):
        response = self.request(method, params, timeout)
        assert "result" in response and response["result"].get("ok") is not False, (response, self.diagnostics())
        return response["result"]

    def wait_closed(self, code=4403, timeout=15):
        with self.condition:
            assert self.condition.wait_for(lambda: self.closed is not None or self.receive_error is not None, timeout), "silent observer stayed connected"
            assert self.closed == code, f"closed={self.closed}, error={self.receive_error!r}"

    def wait_event(self, predicate, timeout=15):
        with self.condition:
            self.condition.wait_for(lambda: any(predicate(event) for event in self.events) or
                                    self.closed is not None or self.receive_error is not None, timeout)
            event = next((event for event in self.events if predicate(event)), None)
            assert event is not None, (
                f"expected real cell event missing; closed={self.closed}; error={self.receive_error!r}; "
                f"replies={self.replies}; recent_events={self.events[-8:]}; {self.diagnostics()}")
            return event

    def close(self):
        try:
            if self.closed is None:
                self.ws.close()
        except ClosedResourceError:
            pass  # A revoked or crashed cell has already closed its ASGI stream.
        finally:
            self.context.__exit__(None, None, None)
            self.thread.join(5)
        assert not self.thread.is_alive(), "scratch websocket receiver leaked"


@dataclass
class RunningCell:
    principal: object
    directory: Path
    listener: socket.socket
    broker: RefusingBroker
    proc: subprocess.Popen | None = None
    log: object = None


class Harness:
    def __init__(self, root, gateway, client, *, organization=None, runtime_configuration=None, provider_wire=None):
        self.root, self.gateway, self.client = root, gateway, client
        self.org, self.team = organization or (str(uuid.uuid4()), str(uuid.uuid4()))
        self.running, self.sockets = {}, []
        self.payload = None
        self.scopes = {}
        self.runtime_configuration = runtime_configuration or {}
        self.provider_wire = provider_wire
        self.activation_lock = threading.Lock()
        self.ambient_file = root / "operator-auth-sentinel.json"
        private_json(self.ambient_file, {"OPENAI_API_KEY": AMBIENT})
        if organization is None:
            with gateway.db.tx() as cur:
                cur.execute("INSERT INTO organizations(id,name) VALUES (%s,%s)", (self.org, "Acceptance T"))
                cur.execute("INSERT INTO teams(id,org_id,name) VALUES (%s,%s,%s)", (self.team, self.org, "T"))
                for email, org_role, team_role in ((A, "member", "member"), (B, "member", "viewer"), (C, "owner", "manager")):
                    cur.execute("INSERT INTO org_members(org_id,email,role) VALUES (%s,%s,%s)", (self.org, email, org_role))
                    cur.execute("INSERT INTO team_members(team_id,email,role) VALUES (%s,%s,%s)", (self.team, email, team_role))
        self.principals = {(email, team): gateway.cells.resolve_workspace(email, team)
                           for email in (A, B, C) for team in ("", self.team)}
        assert [self.principals[email, self.team].right for email in (A, B, C)] == ["editor", "viewer", "manager"]

    def principal(self, email, team=""):
        return self.gateway.cells.resolve_workspace(email, team)

    def native_session_identity(self, cell_id):
        """Read actual SDK headers only in an owned personal acceptance cell."""
        root = self.running[cell_id].directory / "state"
        deadline = time.monotonic() + 10
        observed = {}
        while True:
            identities = []
            for path in root.rglob("*.jsonl"):
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as stream:
                    header = None
                    # The pinned native writer prefixes its session header with
                    # a fixed-width title record. Neither record is transcript text.
                    for _ in range(2):
                        raw = stream.readline(65537)
                        assert len(raw) <= 65536, "oversized owned session header"
                        try:
                            header = json.loads(raw)
                        except (ValueError, UnicodeError):
                            break
                        if not isinstance(header, dict) or header.get("type") != "title":
                            break
                if "Sessions" in path.parts:
                    observed[str(path.relative_to(root))] = raw[:160].decode(errors="replace")
                if isinstance(header, dict) and header.get("type") == "session":
                    assert isinstance(header.get("id"), str) and header["id"], header
                    identities.append((str(path.relative_to(root)), header["id"]))
            if identities:
                assert len(identities) == 1, identities
                return identities[0]
            if time.monotonic() >= deadline:
                raise AssertionError(("actual native session header was not persisted", observed))
            # Wait for a complete persisted header, never infer an ID from a filename.
            time.sleep(0.025)

    def socket(self, email, team=""):
        ws = EngineSocket(self.client, email, team)
        self.sockets.append(ws)
        principal = self.principal(email, team)
        ws.diagnostics = lambda: self.log(principal.cell_id) if principal.cell_id in self.running else "cell never activated"
        ws.ok("ping")  # TestClient admission completes before the async cell activation.
        return ws

    async def service_state(self, cell_id):
        """Observe only fixture-owned process lifetimes, never a product unit."""
        cell = self.running.get(cell_id)
        process = cell.proc if cell else None
        if process is None or process.poll() is not None:
            return {"cell_id": cell_id, "active": "inactive", "sub": "dead", "pid": 0, "invocation": ""}
        try:
            start = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return {"cell_id": cell_id, "active": "inactive", "sub": "dead", "pid": 0, "invocation": ""}
        invocation = hashlib.sha256(f"{process.pid}:{start}".encode()).hexdigest()[:32]
        return {"cell_id": cell_id, "active": "active", "sub": "running",
                "pid": process.pid, "invocation": invocation}

    async def service_units(self):
        return [await self.service_state(cell_id) for cell_id in tuple(self.running)]

    async def activate(self, cell_id):
        assert self.payload is not None, "real payload required before gateway cell activation"
        await asyncio.to_thread(self.start_cell, cell_id)

    def start_cell(self, cell_id):
        with self.activation_lock:
            self._start_cell(cell_id)

    def _start_cell(self, cell_id):
        if existing := self.running.get(cell_id):
            if existing.proc is not None and existing.proc.poll() is None:
                return
            cell = existing
        else:
            principal = next(value for value in self.principals.values() if value.cell_id == cell_id)
            directory = self.gateway.cells.prepare_cell(principal)
            (directory / "state").mkdir(mode=0o700, exist_ok=True)
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(directory / "gw.sock"))
            (directory / "gw.sock").chmod(0o600)
            listener.listen(64)
            cell = RunningCell(principal, directory, listener, RefusingBroker(directory / "egress.sock"))
            self.running[cell_id] = cell
        config = self.root / "runtime.json"
        private_json(config, {"schema": "chudware-cell-runtime-v1", "cells_root": str(self.gateway.settings.cells_root),
                              "allowed_roots": [str(self.gateway.settings.workspace_root), str(self.gateway.settings.bucket_root)],
                              "egress_hosts": ["api.openai.com", "provider.example"],
                              **self.runtime_configuration})
        wrapper = ("import os,sys;os.dup2(int(sys.argv[1]),3,inheritable=True);"
                   "os.environ['LISTEN_PID']=str(os.getpid());"
                   "os.execv(sys.argv[2],[sys.argv[2],sys.argv[3]])")
        # A root-prepared private mount namespace can stage the venv without
        # changing the UID view seen by the real quota admission boundary.
        command = [sys.executable, "-I", "-S", "-c", wrapper, str(cell.listener.fileno()),
                   str(self.payload.path / "bin/chudware-cell-launch"), cell_id]
        if scope := self.scopes.get(cell_id):
            command = ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                       "--unit=" + scope, "-p", "Delegate=yes", "-p", "MemoryMax=1536M",
                       "-p", "MemorySwapMax=0", "-p", "TasksMax=128",
                       "-p", "CPUQuota=100%", "--", *command]
        env = {**os.environ, "CHUDWARE_CELL_CONFIG": str(config), "LISTEN_FDS": "1",
               "OPENAI_API_KEY": AMBIENT, "ANTHROPIC_API_KEY": AMBIENT,
               "OPERATOR_ENV_SENTINEL": AMBIENT, "CHUDWARE_AGENT_AUTH_FILE": str(self.ambient_file)}
        if cell.log:
            cell.log.close()
        cell.log = (cell.directory / "acceptance.log").open("ab", buffering=0)
        cell.proc = subprocess.Popen(command, pass_fds=(cell.listener.fileno(),), env=env,
                                     stdout=cell.log, stderr=cell.log, start_new_session=True)
        # Native initialization starts inside its pid/network/user namespaces.
        try:
            deadline = time.monotonic() + 30
            while True:
                result = self.raw(cell.principal, None, timeout=20)
                state = result.get("result", {}).get("state")
                if state == "ready":
                    break
                assert state == "loading", result
                assert cell.proc.poll() is None and time.monotonic() < deadline, result
                time.sleep(0.05)
        except BaseException:
            time.sleep(0.1)
            raise AssertionError("real --cell startup failed: " + self.log(cell_id))

    def log(self, cell_id):
        path = self.running[cell_id].directory / "acceptance.log"
        return path.read_text(errors="replace")[-6000:]

    def raw(self, principal, method, params=None, timeout=15, hello=None):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(timeout)
            connection.connect(str(self.gateway.settings.cells_root / principal.cell_id / "gw.sock"))
            with connection.makefile("rwb") as stream:
                stream.write(json.dumps({"id": -1, "method": "hello", "params": {"client": "gui", "proto": 1,
                                    "principal": hello or principal.hello()}}).encode() + b"\n")
                stream.flush()
                admitted = json.loads(stream.readline())
                if "error" in admitted or method is None:
                    return admitted
                stream.write(json.dumps({"id": 1, "method": method, "params": params or {}}).encode() + b"\n")
                stream.flush()
                while True:
                    response = json.loads(stream.readline())
                    if response.get("id") == 1:
                        return response

    def kill(self, principal):
        cell = self.running[principal.cell_id]
        if cell.proc is not None and cell.proc.poll() is None:
            os.killpg(cell.proc.pid, signal.SIGKILL)
            cell.proc.wait(timeout=10)

    def remove(self, email):
        with self.gateway.db.tx() as cur:
            cur.execute("DELETE FROM team_members WHERE team_id=%s AND email=%s", (self.team, email))
        return self.gateway.cells.revoke(email=email, team_id=self.team)

    def close(self):
        errors = []
        for ws in reversed(self.sockets):
            try:
                ws.close()
            except BaseException as exc:
                errors.append(exc)
        for cell in tuple(self.running.values()):
            try:
                if cell.proc is not None and cell.proc.poll() is None:
                    os.killpg(cell.proc.pid, signal.SIGTERM)
                    try:
                        cell.proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.kill(cell.principal)
                if scope := self.scopes.get(cell.principal.cell_id):
                    # Only this UUID-named transient acceptance scope is touched.
                    subprocess.run(["systemctl", "--user", "stop", scope],
                                   capture_output=True, timeout=20)
                cell.listener.close()
                cell.broker.close()
                if cell.log:
                    cell.log.close()
            except BaseException as exc:
                errors.append(exc)
        assert not errors, f"scratch harness cleanup failed: {errors!r}"


@pytest.fixture
def gateway_harness(disposable_postgres, monkeypatch):
    explicit_quota = bool(os.environ.get("CHUDWARE_TEST_CELL_STORAGE_ROOT") or
                          os.environ.get("CHUDWARE_TEST_CELL_QUOTA_HELPER"))
    parent = fixture_storage_root() if explicit_quota else Path("/tmp")
    root = Path(tempfile.mkdtemp(prefix="tc-", dir=parent))
    for name, value in {"CHUDWARE_DB_URL": disposable_postgres, "CHUDWARE_DEPLOY_ENV": "prod",
                        "CHUDWARE_WEBD_ENV": str(root / "absent-webd.env"),
                        "CHUDWARE_WEB_DATA_ROOT": str(root / "data"), "CHUDWARE_CELLS_ROOT": str(root / "cells"),
                        "CHUDWARE_WORKSPACE_ROOT": str(root / "workspaces"), "CHUDWARE_WEB_BUCKET_ROOT": str(root / "bucket"),
                        "CHUDWARE_WEB_APP_DIR": str(root / "app"), "CHUDWARE_WEB_DEV": "1",
                        "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"m" * 32).decode(),
                        "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"p" * 32).decode()}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.syspath_prepend(str(REPO / "Webservice"))
    pytest.importorskip("psycopg")
    pytest.importorskip("fastapi")
    modules = {name: importlib.import_module("gateway." + name) for name in
               ("db", "accounts", "auth", "cells", "engine_bridge", "deployment_drain", "orgs", "workspace", "providers", "provider_keys", "provider_broker", "limits", "credits")}
    from gateway.config import settings
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import psycopg
    gateway = SimpleNamespace(**modules, settings=settings)
    gateway.db.close_pool()
    with psycopg.connect(disposable_postgres, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    gateway.db.migrate()
    for field, value in {"data_root": root / "data", "cells_root": root / "cells", "workspace_root": root / "workspaces",
                         "bucket_root": root / "bucket", "university_team_id": "", "university_org_id": "",
                         "university_workspace_root": "", "dev_mode": True, "share_provider_keys": False,
                         "provider_key_encryption_key": base64.urlsafe_b64encode(b"p" * 32).decode()}.items():
        monkeypatch.setattr(settings, field, value)
    gateway.engine_bridge._mesh_grants.clear()
    gateway.engine_bridge._bridge_owners.clear()
    for limiter in (gateway.limits.api_writes, gateway.limits.uploads, gateway.limits.chat_messages):
        with limiter._lock:
            limiter._hits.clear()
    for email in (A, B, C):
        gateway.accounts.ensure_account(email)
        gateway.accounts.set_status(email, "active")
        gateway.accounts.set_plan(email, "studio")
        for provider in ("openai", "anthropic"):
            gateway.provider_keys._persist_validated_key(email, provider, BYOK[email])
    # HTTP availability is real account metadata; its optional host catalog refresh
    # must not launch an ambient CLI or contact models.dev in this owned-only proof.
    monkeypatch.setattr(gateway.providers, "_catalog_next_refresh", float("inf"))
    wire = OfficialWireFixture(BYOK.values())
    monkeypatch.setattr(gateway.provider_broker, "_broker",
                        gateway.provider_broker.ProviderBroker(upstream_origins={
                            provider: wire.origin for provider in ("openai", "anthropic")}))
    @asynccontextmanager
    async def lifespan(app):
        settings.ensure_data_dirs()
        gateway.deployment_drain.startup()
        await gateway.provider_broker.startup()
        try:
            yield
        finally:
            gateway.deployment_drain.controller().stop_admission()
            await gateway.deployment_drain.controller().wait_work()
            await gateway.provider_broker.shutdown()
    app = FastAPI(lifespan=lifespan)
    app.include_router(gateway.engine_bridge.router)
    app.include_router(gateway.workspace.router)
    app.include_router(gateway.providers.router)
    app.include_router(gateway.provider_keys.router)
    app.add_middleware(gateway.auth.AuthMiddleware, cfg=settings)
    app.add_middleware(gateway.deployment_drain.AdmissionMiddleware)
    try:
        with TestClient(app) as client:
            harness = Harness(root, gateway, client, provider_wire=wire)
            if explicit_quota:
                harness.runtime_configuration["quota_proof_root"] = str(provision_test_quotas(quota_plan(harness)))
            monkeypatch.setattr(gateway.cells, "start_cell", harness.activate)
            monkeypatch.setattr(gateway.cells, "service_state", harness.service_state)
            monkeypatch.setattr(gateway.cells, "service_units", harness.service_units)
            try:
                yield harness
            finally:
                harness.close()
    finally:
        try:
            wire.close()
        finally:
            gateway.db.close_pool()
            shutil.rmtree(root)


@pytest.fixture
def two_tenants(real_payload, gateway_harness):
    gateway_harness.payload = real_payload
    return gateway_harness


def denial(response, code):
    value = response.get("error", response.get("result", {}))
    assert value.get("code") == code or value.get("reason") == code, response
    assert response.get("result", {}).get("ok") is not True, response


def box(socket, name="same", kind="part", length=10):
    created = socket.ok("doc_new", {"kind": kind, "name": name})
    ident = created["doc_id"]
    if kind == "part":
        response = socket.ok("doc_apply_ops", {"doc_id": ident, "base_revision": -1, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": name,
             "params": {"length": length, "width": 10, "height": 10}}}]})
        return ident, response
    return ident, created
