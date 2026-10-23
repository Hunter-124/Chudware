"""Gateway-owned admission, filesystem and revocation boundary for tenant cells."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import HTTPException

try:
    from . import accounts, orgs
    from .config import settings
    from .db import tx
    from .limits import user_slug
except ImportError:
    import accounts, orgs
    from config import settings
    from db import tx
    from limits import user_slug

CELL_ID = re.compile(r"[0-9a-f]{32}\Z")
RIGHTS = {"viewer": {"read"}, "editor": {"read", "write", "run"},
          "manager": {"read", "write", "run", "manage"},
          "owner": {"read", "write", "run", "manage"}}
POLICY_PATH = Path(__file__).resolve().parents[2] / "contracts" / "cell_methods.json"


@dataclass(frozen=True)
class CellPrincipal:
    account_uid: str
    workspace_key: str
    actor_key: str
    right: str
    epoch: int
    cell_id: str
    root: Path
    team_id: str
    plan: str

    def hello(self) -> dict:
        return {"workspace_key": self.workspace_key, "actor_key": self.actor_key,
                "right": self.right, "epoch": self.epoch,
                "connection_nonce": secrets.token_hex(16)}


def method_policy() -> dict:
    # The native/worker inventory is the sole authority, never a second list.
    try:
        with POLICY_PATH.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("policy too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or any(
            not isinstance(name, str) or not isinstance(row, dict)
            or row.get("cell") not in {"allow", "refuse"}
            or row.get("right") not in {"read", "write", "run", "manage"}
            or row.get("scope") not in {"workspace", "actor"}
            for name, row in value.items()
        ):
            raise ValueError("invalid policy")
        return value
    except (OSError, ValueError, RecursionError):
        raise HTTPException(503, "cell policy unavailable") from None


def directory_beneath(root: Path, relative: str = "") -> int:
    """Pin a directory using a no-follow walk, including the configured root."""
    root = Path(root).absolute()
    parts = root.parts[1:]
    if relative:
        if (relative.startswith("/") or "\\" in relative
                or any(ord(char) < 32 or ord(char) == 127 for char in relative)):
            raise HTTPException(400, "bad relative path")
        extra = relative.split("/")
        if any(p in {"", ".", ".."} for p in extra):
            raise HTTPException(400, "bad relative path")
        parts += tuple(extra)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except FileNotFoundError:
        os.close(fd)
        raise HTTPException(404, "not found") from None
    except OSError:
        os.close(fd)
        raise HTTPException(403, "unsafe filesystem path") from None


def open_beneath(root: Path, relative: str, *, max_bytes: int | None = None):
    parts = relative.split("/")
    if not relative or relative.startswith("/") or "\\" in relative or any(
        ord(char) < 32 or ord(char) == 127 for char in relative
    ) or any(
        p in {"", ".", ".."} for p in parts
    ):
        raise HTTPException(400, "bad relative path")
    parent = directory_beneath(root, "/".join(parts[:-1]))
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (max_bytes is not None and info.st_size > max_bytes):
            os.close(fd)
            raise HTTPException(403, "unsafe or oversized file")
        return fd, info
    except FileNotFoundError:
        raise HTTPException(404, "not found") from None
    except OSError:
        raise HTTPException(403, "unsafe filesystem path") from None
    finally:
        os.close(parent)


def personal_root(email: str) -> Path:
    return settings.user_workspace_root(user_slug(email))


def personal_bucket_root(email: str) -> Path:
    return settings.user_bucket_root(user_slug(email))


def resolve_workspace(email: str, team_id: str = "") -> CellPrincipal:
    account = accounts.get_account(email)
    if account is None or account.status != "active" or not account.uid:
        raise HTTPException(403, "account unavailable")
    uid = account.uid
    if team_id:
        team = orgs.resolve_team_bucket(email, team_id, write=False)
        key = f"team:{team.org_id}/{team.team_id}"
        right = ("manager" if team.org_role in orgs.ORG_MANAGER_ROLES or team.team_role == "manager"
                 else "viewer" if team.team_role == "viewer" else "editor")
        root, plan = team.root, team.owner_plan
    else:
        key, right, root, plan = f"user:{uid}", "owner", personal_root(email), account.plan
    root.mkdir(parents=True, exist_ok=True)
    fd = directory_beneath(root)
    os.close(fd)
    with tx() as cur:
        cur.execute("INSERT INTO cell_principal_epochs(workspace_key,actor_key,epoch) VALUES(%s,%s,0) "
                    "ON CONFLICT DO NOTHING", (key, uid))
        cur.execute("SELECT epoch FROM cell_principal_epochs WHERE workspace_key=%s AND actor_key=%s",
                    (key, uid))
        epoch = int(cur.fetchone()["epoch"])
    return CellPrincipal(uid, key, uid, right, epoch, hashlib.sha256(key.encode()).hexdigest()[:32],
                         root.absolute(), team_id, plan)


def _validate_root(root: Path) -> None:
    allowed = [settings.workspace_root, settings.bucket_root]
    if settings.university_workspace_root:
        allowed.append(Path(settings.university_workspace_root))
    if not any(root.is_relative_to(Path(base).absolute()) for base in allowed):
        raise HTTPException(403, "cell root refused")
    fd = directory_beneath(root)
    os.close(fd)


def prepare_cell(principal: CellPrincipal) -> Path:
    if not CELL_ID.fullmatch(principal.cell_id):
        raise HTTPException(403, "cell id refused")
    _validate_root(principal.root)
    base = settings.cells_root.absolute()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = directory_beneath(base)
    try:
        try:
            os.mkdir(principal.cell_id, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    directory = base / principal.cell_id
    fd = directory_beneath(directory)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HTTPException(403, "unsafe cell directory")
        expected = {"schema": "chudware-cell-v1", "cell_id": principal.cell_id,
                    "workspace_key": principal.workspace_key, "root": str(principal.root)}
        try:
            config_fd = os.open("cell.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            config_fd = None
        if config_fd is not None:
            with os.fdopen(config_fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise HTTPException(403, "unsafe cell configuration")
                raw = handle.read(8193)
            if len(raw) > 8192 or json.loads(raw) != expected:
                raise HTTPException(403, "cell configuration mismatch")
        else:
            temp = ".cell-" + secrets.token_hex(12)
            config_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=fd)
            try:
                with os.fdopen(config_fd, "w") as handle:
                    json.dump(expected, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(temp, "cell.json", src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
    except (OSError, ValueError):
        raise HTTPException(403, "cell configuration refused") from None
    finally:
        os.close(fd)
    return directory


async def start_cell(cell_id: str) -> None:
    if not CELL_ID.fullmatch(cell_id):
        raise HTTPException(403, "cell id refused")
    process = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "start", f"chudware-cell@{cell_id}.socket",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            result = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(503, "cell activation unavailable") from None
    if result:
        raise HTTPException(503, "cell activation unavailable")


def _connect_pinned(directory: Path, filename: str = "gw.sock") -> socket.socket:
    parent = directory_beneath(directory)
    fd = None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        fd = os.open(filename, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HTTPException(403, "unsafe cell socket")
        sock.settimeout(10)
        sock.connect(f"/proc/self/fd/{fd}")
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


async def connect_cell(principal: CellPrincipal, *, activate: bool = True):
    try:
        from . import deployment_drain, provider_broker
    except ImportError:
        import deployment_drain, provider_broker
    if not activate:
        # Control is retained during maintenance, but an inactive service must
        # not be socket-activated merely to update/revoke a principal.
        unit = await service_state(principal.cell_id)
        if unit["active"] != "active" or unit["sub"] != "running":
            raise HTTPException(503, "cell control unavailable")
        sock = await asyncio.to_thread(_operator_socket, principal.cell_id)
        return await asyncio.open_unix_connection(sock=sock, limit=settings.ws_max_message_bytes)
    with deployment_drain.work():
        directory = prepare_cell(principal)
        await provider_broker.ensure_cell(principal, directory)
        await start_cell(principal.cell_id)
        sock = await asyncio.to_thread(_connect_pinned, directory)
        return await asyncio.open_unix_connection(sock=sock, limit=settings.ws_max_message_bytes)


# Connections register their loop; sync admin/files handlers never touch an ASGI
# WebSocket from their worker thread. Epoch checks also cover other gateway processes.
_live: dict[tuple[str, str], dict[str, tuple[asyncio.AbstractEventLoop, Callable]]] = {}
_live_lock = threading.Lock()


def register(principal: CellPrincipal, callback: Callable) -> str:
    token = secrets.token_hex(16)
    with _live_lock:
        _live.setdefault((principal.workspace_key, principal.actor_key), {})[token] = (
            asyncio.get_running_loop(), callback)
    return token


def unregister(principal: CellPrincipal, token: str) -> None:
    with _live_lock:
        group = _live.get((principal.workspace_key, principal.actor_key), {})
        group.pop(token, None)
        if not group:
            _live.pop((principal.workspace_key, principal.actor_key), None)


def _notify(workspace_key: str, actor_key: str, epoch: int) -> None:
    with _live_lock:
        targets = list(_live.get((workspace_key, actor_key), {}).values())
    for loop, callback in targets:
        loop.call_soon_threadsafe(callback, epoch)
    _send_revocation(workspace_key, actor_key, epoch)


def _send_revocation(workspace_key: str, actor_key: str, epoch: int) -> None:
    """Stop retained actor runs even when no browser connection remains."""
    cell_id = hashlib.sha256(workspace_key.encode()).hexdigest()[:32]
    try:
        fd, _ = open_beneath(settings.cells_root / cell_id, "cell.json", max_bytes=8192)
        with os.fdopen(fd, "rb") as handle:
            config = json.load(handle)
        if config.get("cell_id") != cell_id or config.get("workspace_key") != workspace_key:
            return
        with tx() as cur:
            cur.execute("SELECT email FROM accounts WHERE uid=%s", (actor_key,))
            row = cur.fetchone()
        team_id = workspace_key.split("/", 1)[1] if workspace_key.startswith("team:") else ""
        current = None
        if row:
            try:
                current = resolve_workspace(row["email"], team_id)
                if team_id:
                    orgs.resolve_team_bucket(row["email"], team_id, write=current.right != "viewer")
            except HTTPException:
                current = None
        principal = CellPrincipal(actor_key, workspace_key, actor_key,
                                  current.right if current else "viewer", epoch, cell_id,
                                  Path(config["root"]), team_id, current.plan if current else "trial")
    except (HTTPException, OSError, ValueError, KeyError):
        return  # Never activate a cell merely to revoke a principal.
    try:
        from .engine_bridge import _principal_control
    except ImportError:
        from engine_bridge import _principal_control
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=lambda: asyncio.run(_principal_control(principal, epoch, current)),
                         name="cell-revoke", daemon=True).start()
    else:
        task = loop.create_task(_principal_control(principal, epoch, current))
        _controls.add(task)
        task.add_done_callback(_controls.discard)


_controls: set[asyncio.Task] = set()


def revoke(email: str, team_id: str | None = None) -> None:
    account = accounts.get_account(email)
    if account is None:
        return
    with tx() as cur:
        # Owner funding changes affect every actor in the owner's team cells.
        if team_id is None:
            cur.execute("SELECT e.workspace_key,e.actor_key FROM cell_principal_epochs e WHERE "
                        "e.actor_key=%s OR e.workspace_key IN (SELECT 'team:'||t.org_id::text||'/'||t.id::text "
                        "FROM teams t JOIN org_members m ON m.org_id=t.org_id "
                        "WHERE m.email=%s AND m.role='owner')", (account.uid, email))
        else:
            cur.execute("SELECT workspace_key,actor_key FROM cell_principal_epochs "
                        "WHERE actor_key=%s AND workspace_key LIKE %s", (account.uid, f"team:%/{team_id}"))
        rows = cur.fetchall()
        for row in rows:
            cur.execute("UPDATE cell_principal_epochs SET epoch=epoch+1 "
                        "WHERE workspace_key=%s AND actor_key=%s RETURNING epoch",
                        (row["workspace_key"], row["actor_key"]))
            epoch = int(cur.fetchone()["epoch"])
            _notify(row["workspace_key"], str(row["actor_key"]), epoch)
    _invalidate_grants({(row["workspace_key"], str(row["actor_key"])) for row in rows})


def revoke_team(team_id: str) -> None:
    with tx() as cur:
        cur.execute("UPDATE cell_principal_epochs SET epoch=epoch+1 WHERE workspace_key LIKE %s "
                    "RETURNING workspace_key,actor_key,epoch", (f"team:%/{team_id}",))
        rows = cur.fetchall()
    for row in rows:
        _notify(row["workspace_key"], str(row["actor_key"]), int(row["epoch"]))
    _invalidate_grants({(row["workspace_key"], str(row["actor_key"])) for row in rows})


def _invalidate_grants(keys: set) -> None:
    try:
        from .engine_bridge import invalidate_mesh_grants
    except ImportError:
        from engine_bridge import invalidate_mesh_grants
    invalidate_mesh_grants(keys)


def tombstone_personal(email: str, uid: str | None = None) -> None:
    account = accounts.get_account(email) if uid is None else None
    uid = uid or (account.uid if account else None)
    if not uid:
        raise HTTPException(403, "account unavailable")
    for root in (personal_root(email), personal_bucket_root(email)):
        if not root.exists() and not root.is_symlink():
            continue
        parent = directory_beneath(root.parent)
        try:
            try:
                os.mkdir(".deleted", 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            deleted = os.open(".deleted", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                os.rename(root.name, f"{uid}-{time.time_ns()}-{secrets.token_hex(4)}",
                          src_dir_fd=parent, dst_dir_fd=deleted)
            finally:
                os.close(deleted)
        finally:
            os.close(parent)


_UNIT_RE = re.compile(r"chudware-cell@([0-9a-f]{32})\.service\Z")
_MANAGER_LIMIT = 1 << 20
_CONTROL_LIMIT = 65536


async def _manager(*args):
    """Bound manager-probe I/O, never manipulate a cell unit."""
    process = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "--no-pager", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10):
            chunks, size = [], 0
            while chunk := await process.stdout.read(65536):
                size += len(chunk)
                if size > _MANAGER_LIMIT:
                    raise ValueError("manager response oversized")
                chunks.append(chunk)
            if await process.wait():
                raise ValueError("manager query failed")
            return b"".join(chunks).decode("utf-8")
    except BaseException:
        if process.returncode is None:
            process.kill()  # Our read-only probe, never the cell.
            await process.wait()
        raise


async def service_state(cell_id):
    if not CELL_ID.fullmatch(cell_id):
        raise ValueError("invalid cell unit")
    name = f"chudware-cell@{cell_id}.service"
    text = await _manager("show", name, "--property=Id,LoadState,ActiveState,SubState,MainPID,InvocationID")
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key in fields:
            raise ValueError("malformed manager state")
        fields[key] = value
    if (set(fields) != {"Id", "LoadState", "ActiveState", "SubState", "MainPID", "InvocationID"}
            or fields["Id"] != name or fields["LoadState"] != "loaded"
            or not fields["MainPID"].isascii() or not fields["MainPID"].isdigit()):
        raise ValueError("unavailable manager state")
    return {"cell_id": cell_id, "active": fields["ActiveState"], "sub": fields["SubState"],
            "pid": int(fields["MainPID"]), "invocation": fields["InvocationID"]}


async def service_units():
    text = await _manager("list-units", "--all", "--plain", "--no-legend", "--output=json",
                          "chudware-cell@*.service")
    rows = json.loads(text)
    if not isinstance(rows, list) or len(rows) > 4096:
        raise ValueError("invalid manager unit list")
    identifiers = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("unit"), str):
            raise ValueError("invalid manager unit")
        match = _UNIT_RE.fullmatch(row["unit"])
        if not match or match[1] in identifiers:
            raise ValueError("unknown manager unit")
        identifiers.add(match[1])
    return [await service_state(cell_id) for cell_id in sorted(identifiers)]


def _operator_socket(cell_id):
    directory = settings.cells_root / cell_id
    parent = directory_beneath(directory)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe cell metadata")
        fd = os.open("cell.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 8192):
                raise ValueError("unsafe cell metadata")
            raw = handle.read(8193)
        config = json.loads(raw)
        if (len(raw) > 8192 or not isinstance(config, dict)
                or config.get("schema") != "chudware-cell-v1" or config.get("cell_id") != cell_id
                or not isinstance(config.get("workspace_key"), str)
                or hashlib.sha256(config["workspace_key"].encode()).hexdigest()[:32] != cell_id):
            raise ValueError("cell identity mismatch")
    finally:
        os.close(parent)
    # This is owned by hostd, not systemd. Even if hostd exits after the manager
    # snapshot, connecting here cannot activate a service or create a worker.
    return _connect_pinned(directory / "state" / "run", "operator.sock")


def _validate_census(value):
    count = lambda n: type(n) is int and n >= 0
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
            or not isinstance(value.get("admission"), str) or value["admission"] not in {"open", "closed"}
            or any(not count(value.get(key)) for key in ("generation", "sessions", "rpc_inflight", "active"))
            or any(type(value.get(key)) is not bool for key in ("complete", "quiescent", "exiting"))):
        raise ValueError("invalid cell census")
    for category, names in (("native", ("cache_verification", "profiler", "dirty_documents")),
                            ("workers", ("busy", "background", "unknown"))):
        row = value.get(category)
        if not isinstance(row, dict) or any(not count(row.get(key)) for key in names):
            raise ValueError("incomplete cell census")
    active = (value["active"] + value["sessions"] + value["rpc_inflight"]
              + sum(value["native"][key] for key in ("cache_verification", "profiler", "dirty_documents"))
              + sum(value["workers"][key] for key in ("busy", "background", "unknown")))
    if (value["workers"]["unknown"] and value["complete"]
            or value["quiescent"] and (not value["complete"] or active)):
        raise ValueError("contradictory cell census")
    return value


class _Operator:
    def __init__(self, unit):
        self.unit, self.writer, self.reader, self.ident = unit, None, None, 0

    async def __aenter__(self):
        sock = await asyncio.to_thread(_operator_socket, self.unit["cell_id"])
        try:
            self.reader, self.writer = await asyncio.open_unix_connection(sock=sock, limit=_CONTROL_LIMIT)
            result = await self.call("hello", {"client": "control", "operator": True, "proto": 1})
            if result.get("cell_id") != self.unit["cell_id"] or result.get("operator") is not True:
                raise ValueError("operator cell identity mismatch")
            return self
        except BaseException:
            if self.writer:
                self.writer.close()
            else:
                sock.close()
            raise

    async def __aexit__(self, *_):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass

    async def call(self, method, params=None):
        self.ident += 1
        self.writer.write(json.dumps({"id": self.ident, "method": method, "params": params or {}},
                                     separators=(",", ":"), allow_nan=False).encode() + b"\n")
        async with asyncio.timeout(5):
            await self.writer.drain()
            # A bounded probe deadline is not a deadline on customer work.
            for _ in range(32):
                raw = await self.reader.readline()
                if not raw or len(raw) > _CONTROL_LIMIT:
                    raise ValueError("operator transport unavailable")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("invalid operator reply")
                if value.get("id") != self.ident:
                    continue
                result = value.get("result")
                if "error" in value or not isinstance(result, dict) or result.get("ok", True) is False:
                    raise ValueError("operator capability unavailable")
                return result
        raise ValueError("invalid operator replies")


async def deployment_census(*, close=False, exit_cells=False, reopen=False, fences=None, record=None):
    """Actual manager census plus hostd's authoritative soft-exit handshake."""
    result = {"cell_jobs": 0, "cells_active": 0, "cells_unknown": 0, "cells_exiting": 0,
              "complete": True, "quiescent": True, "diagnostics": []}
    fences = fences or {}
    try:
        units = await service_units()
    except (OSError, ValueError, TimeoutError, HTTPException, RecursionError):
        return {**result, "cells_unknown": 1, "complete": False, "quiescent": False,
                "diagnostics": ["manager_unavailable"]}
    known = []
    for unit in units:
        if unit["active"] == "inactive" and unit["sub"] in {"dead", "exited"} and unit["pid"] == 0:
            continue
        result["cells_active"] += 1
        if unit["active"] == "deactivating":
            result["cells_exiting"] += 1
        try:
            if (unit["active"] != "active" or unit["sub"] != "running" or unit["pid"] <= 0
                    or not CELL_ID.fullmatch(unit["invocation"])):
                raise ValueError("cell unit transitioning")
            async with _Operator(unit) as operator:
                census = _validate_census(await operator.call("cell_census"))
                fence = fences.get(unit["cell_id"])
                if reopen:
                    if census["exiting"]:
                        result["cells_exiting"] += 1
                        raise ValueError("cell already exiting")
                    if census["admission"] == "closed":
                        if not fence or fence["invocation"] != unit["invocation"]:
                            raise ValueError("cell generation unavailable")
                        census = _validate_census(await operator.call("cell_drain", {
                            "admission": "open", "generation": fence["generation"]}))
                        if census["admission"] != "open":
                            raise ValueError("cell reopen refused")
                elif close:
                    generation = census["generation"] + (census["admission"] == "open")
                    if record:
                        record(unit["cell_id"], unit["invocation"], generation)
                    census = _validate_census(await operator.call("cell_drain", {
                        "admission": "closed", "exit": "none"}))
                    if census["admission"] != "closed" or census["generation"] != generation:
                        raise ValueError("cell close refused")
                after = await service_state(unit["cell_id"])
                if after != unit:
                    raise ValueError("cell invocation changed")
                result["cell_jobs"] += census["active"]
                if census["exiting"]:
                    result["cells_exiting"] += 1
                if not census["complete"]:
                    result["complete"] = result["quiescent"] = False
                    if not reopen:
                        raise ValueError("cell activity unknown")
                result["quiescent"] &= census["quiescent"] and not census["exiting"]
                known.append((unit, census))
        except (OSError, ValueError, TimeoutError, HTTPException, RecursionError):
            result["cells_unknown"] += 1
            result["complete"] = result["quiescent"] = False
    if result["cells_unknown"]:
        result["diagnostics"].append("cell_census_unavailable")
    if exit_cells and result["complete"] and result["quiescent"] and close:
        # All units are proven idle before any exit request. Each hostd rechecks
        # under its own admission boundary, replies, then tears itself down.
        for unit, census in known:
            try:
                async with _Operator(unit) as operator:
                    exited = _validate_census(await operator.call("cell_drain", {
                        "admission": "closed", "exit": "when_quiescent", "generation": census["generation"]}))
                    if not exited["complete"] or not exited["quiescent"] and not exited["exiting"]:
                        raise ValueError("cell exit refused")
                    if exited["exiting"]:
                        result["cells_exiting"] += 1
                    else:
                        result["cell_jobs"] += exited["active"]
            except (OSError, ValueError, TimeoutError, HTTPException, RecursionError):
                result["cells_unknown"] += 1
                result["complete"] = False
        # An exit request is never evidence that the service has stopped.
        result["quiescent"] = not known and result["complete"]
    return result
