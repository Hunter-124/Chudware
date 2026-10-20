"""WS /admin/terminal — admin-only browser terminals running Chudcode.

Sessions are backed by a persistent **tmux** server (socket ``$CHUDWARE_TMUX_SOCKET``,
default ``chudware``) started inside its own systemd user scope, so:

  * they survive browser disconnects *and* gateway restarts (**saved**);
  * several browser clients can attach to the same session at once and see/drive
    the same Chudcode instance (**concurrent**).

Security model (this endpoint spawns a shell as the gateway account, so the
default posture is paranoid):

  * the whole terminal is behind ``settings.terminal_enabled`` (default OFF) —
    every REST route 404s and the WebSocket closes immediately unless the
    operator explicitly opted in;
  * each session records its creating admin (tmux ``@chudware_owner``) and only
    that admin — or anyone, when explicitly created with ``shared: true`` — may
    attach or kill it;
  * session identifiers are unguessable ``secrets.token_urlsafe(16)`` tokens;
    the human label (``chud-N``) is display metadata, not the address.

Each WebSocket is one tmux *client* (its own PTY). Closing a WebSocket only
detaches that client — the session keeps running. Killing a session is explicit
(``DELETE /api/admin/terminal/sessions/{name}``) or happens when the whole tmux
session is destroyed (e.g. the shell exits).

REST (all admin-only):
  GET    /api/admin/terminal/sessions          -> {tmux, sessions:[...]}
  POST   /api/admin/terminal/sessions {label?, shared?} -> {name, id, ...}
  DELETE /api/admin/terminal/sessions/{name}   -> {ok}   (name = id or label)

Without tmux the gateway falls back to a private, ephemeral per-connection shell
(the previous behaviour): still functional, just not shared or persistent.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

try:
    from .auth import AuthError, get_current_user, revalidate_websocket_user
    from .config import settings
except ImportError:  # uvicorn app:app from package dir
    from auth import AuthError, get_current_user, revalidate_websocket_user
    from config import settings

log = logging.getLogger("chudware-web.terminal")

router = APIRouter(tags=["terminal"])

_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24
_READ_SIZE = 65536
_KILL_GRACE_S = 0.5
_AUTH_RECHECK_S = 1.0

# Sustained pty→browser throughput and burst allowance. A `cat /dev/urandom`
# inside a terminal must not be able to saturate the gateway's uplink (the
# gateway unit is also capped at MemoryMax=1G, so unbounded buffering is not an
# option). 512 KiB/s is far above interactive terminal use; throttling applies
# backpressure into the pty instead of dropping bytes.
_OUT_RATE_BYTES = 512 * 1024
_OUT_BURST_BYTES = 1024 * 1024

# Resource ceilings for the systemd user scope that hosts the tmux server (and
# the legacy fallback shell). The scope deliberately lives OUTSIDE the
# gateway's MemoryMax=1G cgroup so a heavy build can't OOM-kill the gateway —
# but without its own ceiling a runaway shell (fork bomb, memory hog) could
# take the whole host down. 2 GiB / 2 CPUs is ample for Chudcode + builds.
_SCOPE_MEMORY_MAX = "2G"
_SCOPE_CPU_QUOTA = "200%"

# tmux backing --------------------------------------------------------------
_TMUX = shutil.which("tmux")
_SOCKET = (os.environ.get("CHUDWARE_TMUX_SOCKET") or "chudware").strip() or "chudware"
# tmux session-name prefix so the shared pool never collides with a personal
# tmux and is trivially filterable in list-sessions.
_PREFIX = "chudware-"
_NAME_RE = re.compile(r"[^a-z0-9-]+")
_LS_FMT = (
    "#{session_name}\t#{session_windows}\t#{session_attached}"
    "\t#{session_created}\t#{session_activity}"
    # Per-session ownership metadata (user options, empty when unset — i.e. on
    # sessions created before ownership existed).
    "\t#{@chudware_owner}\t#{@chudware_label}\t#{@chudware_shared}"
)


def _repo_dir() -> Path:
    env = (os.environ.get("CHUDWARE_REPO_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _chud_cmd() -> str:
    return (
        os.environ.get("CHUDWARE_CHUD_CMD")
        or os.environ.get("CHUDWARE_CHUDCODE_BIN")
        or "chud"
    ).strip() or "chud"


def _parse_size(ws: WebSocket) -> tuple[int, int]:
    try:
        cols = int(ws.query_params.get("cols") or _DEFAULT_COLS)
    except (TypeError, ValueError):
        cols = _DEFAULT_COLS
    try:
        rows = int(ws.query_params.get("rows") or _DEFAULT_ROWS)
    except (TypeError, ValueError):
        rows = _DEFAULT_ROWS
    cols = max(1, min(cols, 512))
    rows = max(1, min(rows, 256))
    return cols, rows


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError as exc:
        log.debug("TIOCSWINSZ failed: %s", exc)


def _child_env(repo: Path) -> dict[str, str]:
    """Env for a forked child (shell or tmux client)."""
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    # Ensure ~/.local/bin is on PATH even before login-shell rc files run.
    home = env.get("HOME") or str(Path.home())
    local_bin = str(Path(home) / ".local" / "bin")
    path = env.get("PATH") or ""
    if local_bin not in path.split(":"):
        env["PATH"] = f"{local_bin}:{path}" if path else local_bin
    return env


# --- shared session name helpers -------------------------------------------
def _slug(name: str) -> str:
    s = _NAME_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s[:40]


def _tmux_name(slug: str) -> str:
    return _PREFIX + slug


def _display(tmux_name: str) -> str:
    return tmux_name[len(_PREFIX) :] if tmux_name.startswith(_PREFIX) else tmux_name


def _tmux_cmd(*args: str) -> list[str]:
    return [_TMUX, "-L", _SOCKET, *args]


def _target(tmux_name: str) -> str:
    """Session target for tmux commands. Plain name, NOT the ``=name``
    exact-match form: tmux 3.6b rejects ``=`` targets for set-option and
    send-keys (rc=1, "no such session") even while has-session/kill-session
    accept them — which silently dropped session options AND the Chudcode launch
    keys. Our names are unique and glob-free (``[a-z0-9-]`` slugs and
    ``token_urlsafe`` ids), and tmux tries an exact name match first, so the
    plain form is exactly equivalent here."""
    return tmux_name


def _run_tmux(*args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Run a tmux control command (client-side, returns quickly)."""
    return subprocess.run(  # noqa: S603
        _tmux_cmd(*args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _ensure_scroll_defaults() -> None:
    """Apply the mouse/scroll tmux options + wheel bindings globally so the wheel
    and two-finger swipe drive copy-mode (reviewing the Chudcode transcript)
    instead of the app (never arrow keys). Global + idempotent, so attaching to a session
    created before this existed — or a server born without our -f config — picks
    up the fix live, without needing to be killed and recreated."""
    if not _TMUX or not _server_running():
        return
    _run_tmux("set-option", "-g", "mouse", "on")
    _run_tmux("set-option", "-g", "window-size", "latest")
    _run_tmux(
        "bind-key",
        "-n",
        "WheelUpPane",
        "if-shell",
        "-F",
        "#{pane_in_mode}",
        "send -X -N3 scroll-up",
        "copy-mode -e ; send -X -N3 scroll-up",
    )
    _run_tmux(
        "bind-key",
        "-n",
        "WheelDownPane",
        "if-shell",
        "-F",
        "#{pane_in_mode}",
        "send -X -N3 scroll-down",
        "",
    )


def _server_running() -> bool:
    if not _TMUX:
        return False
    return _run_tmux("list-sessions").returncode == 0


def _session_exists(tmux_name: str) -> bool:
    if not _TMUX:
        return False
    return _run_tmux("has-session", "-t", _target(tmux_name)).returncode == 0


def _list_sessions() -> list[dict[str, Any]]:
    """All sessions in our pool, oldest first. Empty when no server/tmux.

    Each entry carries the opaque ``id`` (the tmux session suffix), the human
    ``label``, the creating admin's ``owner`` email ("" for pre-ownership
    sessions), and the explicit ``shared`` opt-in flag.
    """
    if not _TMUX:
        return []
    cp = _run_tmux("list-sessions", "-F", _LS_FMT)
    if cp.returncode != 0:  # no server running yet
        return []
    out: list[dict[str, Any]] = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name = parts[0]
        if not name.startswith(_PREFIX):
            continue

        def _int(v: str) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        sid = _display(name)
        owner = parts[5].strip() if len(parts) > 5 else ""
        label = parts[6].strip() if len(parts) > 6 else ""
        shared = (parts[7].strip() if len(parts) > 7 else "") == "1"
        # Legacy sessions have no label option; their tmux suffix IS the label.
        label = label or sid
        out.append(
            {
                # ``name`` mirrors ``label`` for the existing admin UI, which
                # predates the id/label split.
                "name": label,
                "id": sid,
                "label": label,
                "owner": owner,
                "shared": shared,
                "windows": _int(parts[1]),
                "clients": _int(parts[2]),
                "created": _int(parts[3]),
                "activity": _int(parts[4]),
            }
        )
    out.sort(key=lambda s: s["created"])
    return out


def _can_access(session: dict[str, Any], email: str) -> bool:
    """May this admin attach to / kill this session?

    Default deny across admins: only the creator — or every admin when the
    session was explicitly created with ``shared: true``. Sessions created
    before ownership existed have no recorded owner; treating them as shared
    keeps running work reachable instead of silently locking admins out.
    """
    owner = str(session.get("owner") or "")
    return not owner or owner == email or bool(session.get("shared"))


def _resolve_session(name: str, email: str) -> dict[str, Any] | None:
    """Find a session the caller may access, by opaque id OR by label.

    Label lookup is a convenience for the admin UI (tabs remember labels); the
    access check applies either way, so a guessed label never crosses admins.
    """
    want = (name or "").strip()
    if not want:
        return None
    sessions = _list_sessions()
    for key in ("id", "label"):
        for sess in sessions:
            if sess.get(key) == want and _can_access(sess, email):
                return sess
    return None


def _mint_session_id() -> str:
    """Unguessable opaque session identifier (tmux-name-safe alphabet)."""
    return secrets.token_urlsafe(16)


def _next_label(sessions: list[dict[str, Any]]) -> str:
    """Pick the lowest free ``chud-N`` display label (label only — not the id)."""
    existing = {str(s.get("label") or "") for s in sessions}
    i = 1
    while f"chud-{i}" in existing:
        i += 1
    return f"chud-{i}"


def _create_session(
    tmux_name: str,
    cols: int,
    rows: int,
    repo: Path,
    owner: str = "",
    label: str = "",
    shared: bool = False,
) -> None:
    """Create a detached tmux session running Chudcode in the repo.

    The very first session also starts the tmux server; that command is wrapped
    in a systemd user scope so the server (and every Chudcode process it hosts)
    lives outside the gateway's MemoryMax cgroup — a heavy build can't OOM-kill the gateway.
    The scope gets its OWN MemoryMax/CPUQuota ceilings so a runaway shell can't
    OOM the whole host either. Later sessions attach to the existing server and
    inherit that scope.
    """
    env = _child_env(repo)
    conf = Path(__file__).resolve().parent / "chudware-tmux.conf"
    base = _tmux_cmd(
        *(["-f", str(conf)] if conf.exists() else []),
        "new-session",
        "-d",
        "-s",
        tmux_name,
        "-x",
        str(cols),
        "-y",
        str(rows),
        "-c",
        str(repo),
        "/bin/bash",
        "-il",
    )
    scoped: list[str] | None = None
    if not _server_running() and shutil.which("systemd-run"):
        scoped = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "-p",
            f"MemoryMax={_SCOPE_MEMORY_MAX}",
            "-p",
            f"CPUQuota={_SCOPE_CPU_QUOTA}",
            *base,
        ]
    cp = subprocess.run(  # noqa: S603
        scoped or base, env=env, capture_output=True, text=True, timeout=20, check=False
    )
    if not _session_exists(tmux_name):
        # systemd-run --user needs a user bus; if it's absent the scoped create
        # fails silently. Retry unscoped so the terminal still works (just inside
        # the gateway cgroup), then give up loudly rather than returning a phantom
        # session that the WS attach would immediately report as "ended".
        if scoped is not None:
            log.warning(
                "scoped tmux create failed (rc=%s): %s — retrying without systemd-run",
                cp.returncode,
                (cp.stderr or "").strip()[-300:],
            )
            cp = subprocess.run(  # noqa: S603
                base, env=env, capture_output=True, text=True, timeout=20, check=False
            )
        if not _session_exists(tmux_name):
            detail = (cp.stderr or cp.stdout or "").strip()[
                -300:
            ] or f"rc={cp.returncode}"
            raise RuntimeError(f"tmux session create failed: {detail}")
    target = _target(tmux_name)
    # Ownership metadata lives in tmux itself, so it survives gateway restarts
    # exactly as long as the session it describes.
    if owner:
        _run_tmux("set-option", "-t", target, "@chudware_owner", owner)
    if label:
        _run_tmux("set-option", "-t", target, "@chudware_label", label)
    if shared:
        _run_tmux("set-option", "-t", target, "@chudware_shared", "1")
    # Clean full-screen Chudcode session for shared viewing; keep it alive with zero
    # attached clients so work is never lost when everyone closes their browser.
    _run_tmux("set-option", "-t", target, "status", "off")
    _run_tmux("set-option", "-t", target, "destroy-unattached", "off")
    # Mouse/scroll defaults, re-applied defensively for a server that was born
    # without our -f config: the wheel and mobile two-finger swipe drive tmux
    # copy-mode (browser terminals have no native scrollback on the alt screen).
    _run_tmux("set-option", "-g", "mouse", "on")
    # Launch Chudcode in the pane. send-keys buffers into the pane pty; bash reads
    # it at its first prompt, so no settle/injection dance is needed.
    _run_tmux("send-keys", "-t", target, _chud_cmd(), "Enter")
    log.info(
        "terminal session created id=%s label=%s owner=%s shared=%s repo=%s",
        _display(tmux_name),
        label,
        owner or "(legacy)",
        shared,
        repo,
    )


def _kill_session(tmux_name: str) -> bool:
    if not _TMUX:
        return False
    return _run_tmux("kill-session", "-t", _target(tmux_name)).returncode == 0


def _spawn_attach(tmux_name: str, cols: int, rows: int) -> tuple[int, int]:
    """Fork a PTY and exec ``tmux attach`` (plain attach → multi-client)."""
    argv = _tmux_cmd("attach", "-t", _target(tmux_name))
    child_env = _child_env(_repo_dir())
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            _set_winsize(0, rows, cols)
        except Exception:
            pass
        try:
            os.execvpe(argv[0], argv, child_env)
        except OSError:
            os._exit(127)
        os._exit(127)
    _set_winsize(master_fd, rows, cols)
    log.info(
        "terminal attach pid=%d master=%d session=%s",
        pid,
        master_fd,
        _display(tmux_name),
    )
    return pid, master_fd


# --- legacy (no tmux): private ephemeral shell -----------------------------
def _build_shell_argv(repo: Path) -> list[str]:
    """Argv for the fallback child process (post-pty.fork).

    Preferred: systemd-run --user --scope so the shell and Chudcode live outside
    the gateway's MemoryMax cgroup — with its own MemoryMax/CPUQuota ceilings so
    a runaway shell can't OOM the host either. Fallback: bare interactive login
    bash.
    """
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "-p",
            f"MemoryMax={_SCOPE_MEMORY_MAX}",
            "-p",
            f"CPUQuota={_SCOPE_CPU_QUOTA}",
            f"--working-directory={repo}",
            "--",
            "/bin/bash",
            "-il",
        ]
    return ["/bin/bash", "-il"]


def _spawn_shell(repo: Path, cols: int, rows: int) -> tuple[int, int]:
    """Fork a PTY + interactive shell (fallback path). Returns (pid, master_fd)."""
    argv = _build_shell_argv(repo)
    use_systemd = argv[0] == "systemd-run"
    child_env = _child_env(repo)

    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            _set_winsize(0, rows, cols)
        except Exception:
            pass
        if not use_systemd:
            try:
                os.chdir(repo)
            except OSError:
                pass
        try:
            os.execvpe(argv[0], argv, child_env)
        except OSError:
            try:
                os.chdir(repo)
            except OSError:
                pass
            os.execvpe("/bin/bash", ["/bin/bash", "-il"], child_env)
        os._exit(127)

    _set_winsize(master_fd, rows, cols)
    log.info(
        "terminal spawned pid=%d master=%d repo=%s argv=%s", pid, master_fd, repo, argv
    )
    return pid, master_fd


def _read_pty(master_fd: int) -> bytes:
    """Blocking-friendly read used from a worker thread / executor."""
    try:
        return os.read(master_fd, _READ_SIZE)
    except OSError:
        return b""


def _wait_readable(master_fd: int, timeout: float = 0.05) -> bool:
    try:
        r, _, _ = select.select([master_fd], [], [], timeout)
        return bool(r)
    except (OSError, ValueError):
        return False


async def _inject_chud(master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
    """Let the shell settle, then launch Chudcode (legacy PTY path only)."""
    deadline = loop.time() + 0.4
    while loop.time() < deadline:
        ready = await loop.run_in_executor(None, _wait_readable, master_fd, 0.05)
        if not ready:
            continue
        try:
            data = await loop.run_in_executor(None, _read_pty, master_fd)
        except Exception:
            break
        if not data:
            break
        if len(data) < _READ_SIZE:
            await asyncio.sleep(0.05)
            break
    cmd = f"{_chud_cmd()}\r"
    try:
        os.write(master_fd, cmd.encode("utf-8"))
    except OSError as exc:
        log.warning("failed to inject Chudcode: %s", exc)


def _reap(pid: int) -> int | None:
    """Non-blocking waitpid; returns exit code if reaped, else None."""
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 0
    if wpid == 0:
        return None
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 0


async def _kill_child(pid: int, master_fd: int | None) -> int | None:
    """SIGHUP → grace → SIGKILL, close master, reap. Returns exit code if known.

    For a tmux attach client this just detaches it (the shared session lives on);
    for the legacy private shell it tears the whole thing down.
    """
    code: int | None = None
    if pid > 0:
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            code = _reap(pid)
            pid = 0
        except OSError:
            pass
    if master_fd is not None and master_fd >= 0:
        try:
            os.close(master_fd)
        except OSError:
            pass
    if pid > 0:
        await asyncio.sleep(_KILL_GRACE_S)
        code = _reap(pid)
        if code is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass
            await asyncio.sleep(0.05)
            code = _reap(pid)
    return code


# --- REST: session management ----------------------------------------------
def _require_terminal() -> None:
    """Kill switch. The terminal is an interactive shell as the gateway
    account; a public-facing deployment must not carry it unless the operator
    explicitly opted in (CHUDWARE_WEB_TERMINAL_ENABLED=1). 404 (not 403) so a
    disabled deployment doesn't even confirm the route exists."""
    if not settings.terminal_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


def _require_admin(request: Request):
    user = get_current_user(request)
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


@router.get("/api/admin/terminal/sessions")
async def list_terminal_sessions(request: Request) -> dict[str, Any]:
    _require_terminal()
    user = _require_admin(request)
    email = getattr(user, "email", "") or ""
    loop = asyncio.get_running_loop()
    sessions = await loop.run_in_executor(None, _list_sessions)
    # Each admin sees only their own sessions (+ explicit shared / legacy ones).
    visible = [s for s in sessions if _can_access(s, email)]
    return {"tmux": _TMUX is not None, "sessions": visible}


@router.post("/api/admin/terminal/sessions")
async def create_terminal_session(request: Request) -> dict[str, Any]:
    _require_terminal()
    user = _require_admin(request)
    if not _TMUX:
        raise HTTPException(status_code=400, detail="tmux not available on host")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body if isinstance(body, dict) else {}
    label = _slug(body.get("label") or body.get("name") or "")
    # Explicit cross-admin sharing is opt-in per session; the default is
    # owner-only.
    shared = bool(body.get("shared"))
    owner = getattr(user, "email", "") or ""
    loop = asyncio.get_running_loop()
    sessions = await loop.run_in_executor(None, _list_sessions)
    cap = max(1, settings.terminal_max_sessions)
    if len(sessions) >= cap:
        raise HTTPException(
            status_code=409,
            detail=f"terminal session cap reached ({cap}); kill a session first",
        )
    if not label:
        label = _next_label(sessions)
    # The addressable id is always freshly minted and unguessable; a requested
    # name is only the human label, never the identifier.
    tmux_name = ""
    for _ in range(4):
        candidate = _tmux_name(_mint_session_id())
        if not await loop.run_in_executor(None, _session_exists, candidate):
            tmux_name = candidate
            break
    if not tmux_name:
        raise HTTPException(status_code=500, detail="could not mint a session id")
    try:
        await loop.run_in_executor(
            None,
            _create_session,
            tmux_name,
            _DEFAULT_COLS,
            _DEFAULT_ROWS,
            _repo_dir(),
            owner,
            label,
            shared,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("terminal session create failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"could not create session: {exc}")
    sid = _display(tmux_name)
    return {
        "name": label,
        "id": sid,
        "label": label,
        "owner": owner,
        "shared": shared,
        "created": True,
    }


@router.delete("/api/admin/terminal/sessions/{name}")
async def kill_terminal_session(name: str, request: Request) -> dict[str, Any]:
    _require_terminal()
    user = _require_admin(request)
    if not _TMUX:
        raise HTTPException(status_code=400, detail="tmux not available on host")
    email = getattr(user, "email", "") or ""
    loop = asyncio.get_running_loop()
    sess = await loop.run_in_executor(None, _resolve_session, name, email)
    if sess is None:
        # Indistinguishable whether it's absent or owned by another admin.
        raise HTTPException(status_code=404, detail="session not found")
    ok = await loop.run_in_executor(None, _kill_session, _tmux_name(str(sess["id"])))
    return {"ok": ok}


# --- shared window-manager layout (one doc, all admins) --------------------
def _read_layout() -> dict[str, Any]:
    """Return the persisted shared layout doc, or an empty default."""
    path = settings.terminal_layout_file
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"layout": None, "mtime": None}
    except (OSError, ValueError):
        log.warning("terminal_layout.json unreadable/corrupt; ignoring")
        return {"layout": None, "mtime": None}
    if not isinstance(data, dict):
        return {"layout": None, "mtime": None}
    return {"layout": data.get("layout"), "mtime": data.get("mtime")}


def _write_layout(layout: dict[str, Any], by: str) -> float:
    """Atomically persist the shared layout; returns the new mtime (epoch)."""
    path = settings.terminal_layout_file
    path.parent.mkdir(parents=True, exist_ok=True)
    mtime = time.time()
    payload = {"layout": layout, "mtime": mtime, "by": by}
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    tmp.replace(path)
    return mtime


@router.get("/api/admin/terminal/layout")
async def get_terminal_layout(request: Request) -> dict[str, Any]:
    _require_terminal()
    _require_admin(request)
    doc = _read_layout()
    return {"ok": True, "layout": doc["layout"], "mtime": doc["mtime"]}


@router.put("/api/admin/terminal/layout")
async def put_terminal_layout(request: Request) -> dict[str, Any]:
    _require_terminal()
    user = _require_admin(request)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = None
    layout = body.get("layout") if isinstance(body, dict) else None
    if not isinstance(layout, dict):
        raise HTTPException(status_code=400, detail="layout must be an object")
    by = getattr(user, "email", "") or ""
    loop = asyncio.get_running_loop()
    mtime = await loop.run_in_executor(None, _write_layout, layout, by)
    return {"ok": True, "mtime": mtime}


@router.websocket("/admin/terminal")
async def admin_terminal(ws: WebSocket) -> None:
    await ws.accept()

    # Kill switch first: a disabled deployment doesn't even confirm the route.
    if not settings.terminal_enabled:
        await ws.close(code=4404)
        return

    user = getattr(ws.state, "user", None)
    if user is None:
        await ws.close(code=4401)
        return
    if getattr(user, "role", None) != "admin":
        await ws.close(code=4403)
        return
    try:
        user = revalidate_websocket_user(ws.scope, required_role="admin")
    except AuthError as exc:
        await ws.close(code=4401 if exc.status == 401 else 4403)
        return
    email = getattr(user, "email", "") or ""

    cols, rows = _parse_size(ws)
    repo = _repo_dir()
    loop = asyncio.get_running_loop()
    tmux_pool = _TMUX is not None
    tmux_name: str | None = None
    landed_label = ""
    pid = -1
    master_fd = -1
    pump_task: asyncio.Task | None = None
    authority_task: asyncio.Task | None = None
    recv_task: asyncio.Task | None = None
    authority_close_code: int | None = None
    exit_code: int | None = None
    shell_died = False
    idle_timed_out = False
    # Idle deadline: any traffic in EITHER direction counts as activity. A
    # forgotten browser tab must not pin a shell (and its tmux client) forever.
    idle_timeout = max(30, int(settings.terminal_idle_timeout_sec))
    last_activity = loop.time()

    try:
        if tmux_pool:
            req = (ws.query_params.get("name") or "").strip()
            sess = (
                await loop.run_in_executor(None, _resolve_session, req, email)
                if req
                else None
            )
            if sess is not None:
                tmux_name = _tmux_name(str(sess["id"]))
                landed_label = str(sess.get("label") or sess["id"])
            else:
                # No accessible session by that id/label: create a NEW one owned
                # by the caller (a requested name becomes only the label, so a
                # guessed label can never hijack another admin's session).
                sessions = await loop.run_in_executor(None, _list_sessions)
                if len(sessions) >= max(1, settings.terminal_max_sessions):
                    await ws.close(code=4429, reason="terminal session cap reached")
                    return
                label = _slug(req) or _next_label(sessions)
                landed_label = label
                tmux_name = ""
                for _ in range(4):
                    candidate = _tmux_name(_mint_session_id())
                    if not await loop.run_in_executor(None, _session_exists, candidate):
                        tmux_name = candidate
                        break
                if not tmux_name:
                    await ws.close(code=1011)
                    return
                await loop.run_in_executor(
                    None,
                    _create_session,
                    tmux_name,
                    cols,
                    rows,
                    repo,
                    email,
                    label,
                    False,
                )
            await loop.run_in_executor(None, _ensure_scroll_defaults)
            pid, master_fd = await loop.run_in_executor(
                None, _spawn_attach, tmux_name, cols, rows
            )
        else:
            pid, master_fd = await loop.run_in_executor(
                None, _spawn_shell, repo, cols, rows
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("terminal spawn failed: %s", exc)
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
        return

    async def authorized() -> bool:
        nonlocal authority_close_code
        if authority_close_code is not None or ws.client_state != WebSocketState.CONNECTED:
            return False
        try:
            revalidate_websocket_user(ws.scope, required_role="admin")
        except AuthError as exc:
            authority_close_code = 4401 if exc.status == 401 else 4403
            await ws.close(code=authority_close_code)
            return False
        return True

    async def watch_authority() -> None:
        # Silent browsers must lose access too; no traffic is needed to detach
        # their client (or terminate their private fallback shell).
        while True:
            await asyncio.sleep(_AUTH_RECHECK_S)
            if not await authorized():
                return

    async def pty_to_ws() -> None:
        nonlocal exit_code, shell_died, last_activity
        # Token-bucket throttle: sustained _OUT_RATE_BYTES with a bounded burst.
        # Sleeping applies backpressure into the pty (kernel buffer fills → the
        # producer blocks), so `cat /dev/urandom` can't saturate the link and
        # nothing unbounded ever buffers in the 1 GiB-capped gateway.
        bucket = float(_OUT_BURST_BYTES)
        bucket_ts = loop.time()
        try:
            while True:
                data = await loop.run_in_executor(None, _read_pty, master_fd)
                if not data:
                    shell_died = True
                    exit_code = _reap(pid)
                    if exit_code is None:
                        await asyncio.sleep(0.05)
                        exit_code = _reap(pid)
                    break
                if ws.client_state != WebSocketState.CONNECTED:
                    break
                now = loop.time()
                bucket = min(
                    float(_OUT_BURST_BYTES),
                    bucket + (now - bucket_ts) * _OUT_RATE_BYTES,
                )
                bucket_ts = now
                n = float(len(data))
                if n > bucket:
                    await asyncio.sleep((n - bucket) / _OUT_RATE_BYTES)
                    bucket = 0.0
                else:
                    bucket -= n
                if not await authorized():
                    break
                await ws.send_bytes(data)
                last_activity = loop.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("pty→ws pump ended: %s", exc)
            shell_died = True
            if exit_code is None:
                exit_code = _reap(pid)

    try:
        authority_task = asyncio.create_task(watch_authority(), name="term-auth-watch")
        # Session metadata, PTY bytes and exit metadata all require current
        # authority, not merely the middleware's handshake snapshot.
        if not await authorized():
            return
        if tmux_pool and tmux_name:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "session",
                        "name": landed_label or _display(tmux_name),
                        "id": _display(tmux_name),
                    },
                    separators=(",", ":"),
                )
            )
        # The tmux path launches Chudcode itself; only the legacy shell needs injection.
        if not tmux_pool:
            await _inject_chud(master_fd, loop)
        pump_task = asyncio.create_task(pty_to_ws(), name="term-pty-to-ws")

        while True:
            if pump_task.done() or authority_task.done():
                break
            remaining = last_activity + idle_timeout - loop.time()
            if remaining <= 0:
                idle_timed_out = True
                break
            recv_task = asyncio.create_task(ws.receive(), name="term-ws-recv")
            done, pending = await asyncio.wait(
                {pump_task, recv_task, authority_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if recv_task in pending:
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if not done:
                # asyncio.wait timed out: no traffic in either direction for
                # the whole idle window — drop the client.
                idle_timed_out = True
                break
            if pump_task in done or authority_task in done:
                if recv_task in done:
                    try:
                        recv_task.result()
                    except Exception:  # noqa: BLE001
                        pass
                break
            try:
                msg = recv_task.result()
            except WebSocketDisconnect:
                break
            except Exception:  # noqa: BLE001
                break
            if msg.get("type") == "websocket.disconnect":
                break
            if not await authorized():
                break
            last_activity = loop.time()
            if "bytes" in msg and msg["bytes"] is not None:
                data = msg["bytes"]
                if data:
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        shell_died = True
                        break
            elif "text" in msg and msg["text"] is not None:
                raw = msg["text"]
                try:
                    ctrl = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(ctrl, dict):
                    continue
                if ctrl.get("type") == "resize":
                    try:
                        r = int(ctrl.get("rows") or rows)
                        c = int(ctrl.get("cols") or cols)
                    except (TypeError, ValueError):
                        continue
                    r = max(1, min(r, 256))
                    c = max(1, min(c, 512))
                    rows, cols = r, c
                    _set_winsize(master_fd, rows, cols)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("terminal handler error: %s", exc)
    finally:
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()
            try:
                await recv_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if authority_task is not None:
            authority_task.cancel()
            try:
                await authority_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Close/kill first so a blocked os.read in the pump unblocks. In shared
        # mode this only detaches the tmux client; the session keeps running.
        code = await _kill_child(
            pid if pid > 0 else 0, master_fd if master_fd >= 0 else None
        )
        if exit_code is None:
            exit_code = code

        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        elif pump_task is not None and pump_task.done():
            try:
                pump_task.result()
            except Exception:  # noqa: BLE001
                pass

        # Only report an exit when the SESSION is really gone. `shell_died` just
        # means our `tmux attach` client died — which also happens on a plain
        # detach and, crucially, every time the gateway restarts (deploy). The
        # tmux server lives in its own systemd scope and survives that, so
        # reporting "exit" there makes the browser mark the tab dead and refuse
        # to reconnect, stranding a session that is still running.
        session_gone = not (tmux_name and _session_exists(tmux_name))
        if shell_died and session_gone and ws.client_state == WebSocketState.CONNECTED:
            try:
                if await authorized():
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "exit",
                                "code": int(exit_code if exit_code is not None else 0),
                            },
                            separators=(",", ":"),
                        )
                    )
            except Exception:  # noqa: BLE001
                pass
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close(code=authority_close_code or (4408 if idle_timed_out else 1000))
            except Exception:  # noqa: BLE001
                pass
        log.info(
            "terminal closed email=%s session=%s pid=%s exit=%s idle=%s",
            getattr(user, "email", "?"),
            _display(tmux_name) if tmux_name else "(private)",
            pid,
            exit_code,
            idle_timed_out,
        )
