"""WS /admin/terminal — admin-only browser terminal running omp in the CAD repo.

Spawns an interactive login shell in a detached systemd user scope (so omp's
builds cannot OOM-kill the gateway cgroup), auto-launches `omp`, and pumps
raw PTY bytes over the WebSocket.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import select
import shutil
import signal
import struct
import termios
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

log = logging.getLogger("billet-web.terminal")

router = APIRouter(tags=["terminal"])

_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24
_READ_SIZE = 65536
_KILL_GRACE_S = 0.5


def _repo_dir() -> Path:
    env = (os.environ.get("BILLET_OMP_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _omp_cmd() -> str:
    return (os.environ.get("BILLET_OMP_CMD") or "omp").strip() or "omp"


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


def _build_shell_argv(repo: Path) -> list[str]:
    """Return argv for the child process (post-pty.fork).

    Preferred: systemd-run --user --scope so the shell (and omp) live outside
    the gateway's MemoryMax cgroup. Fallback: bare interactive login bash.
    """
    if shutil.which("systemd-run"):
        return [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--working-directory={repo}",
            "--",
            "/bin/bash",
            "-il",
        ]
    return ["/bin/bash", "-il"]


def _spawn_shell(repo: Path, cols: int, rows: int) -> tuple[int, int]:
    """Fork a PTY + interactive shell. Returns (pid, master_fd).

    Child either execs systemd-run (scope outside gateway cgroup) or chdirs to
    the repo and execs bash directly.
    """
    argv = _build_shell_argv(repo)
    use_systemd = argv[0] == "systemd-run"
    # Snapshot env for the child before fork.
    child_env = os.environ.copy()
    child_env["TERM"] = "xterm-256color"
    # Ensure ~/.local/bin is on PATH even before login-shell rc files run.
    home = child_env.get("HOME") or str(Path.home())
    local_bin = str(Path(home) / ".local" / "bin")
    path = child_env.get("PATH") or ""
    if local_bin not in path.split(":"):
        child_env["PATH"] = f"{local_bin}:{path}" if path else local_bin

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child — set winsize on the slave (stdin), then exec.
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
            # Last-ditch: bare bash so the PTY is not empty.
            try:
                os.chdir(repo)
            except OSError:
                pass
            os.execvpe("/bin/bash", ["/bin/bash", "-il"], child_env)
        os._exit(127)

    # Parent — keep master blocking; reads run in an executor thread.
    _set_winsize(master_fd, rows, cols)
    log.info(
        "terminal spawned pid=%d master=%d repo=%s argv=%s",
        pid,
        master_fd,
        repo,
        argv,
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


async def _inject_omp(master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
    """Let the shell settle, then type `omp` + Enter so omp starts in-repo."""
    # Brief settle + optional drain so the injection lands near a prompt.
    # Discarded banner bytes are fine — omp's TUI will repaint the screen.
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
    cmd = f"{_omp_cmd()}\r"
    try:
        os.write(master_fd, cmd.encode("utf-8"))
    except OSError as exc:
        log.warning("failed to inject omp: %s", exc)


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
    """SIGHUP → grace → SIGKILL, close master, reap. Returns exit code if known."""
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


@router.websocket("/admin/terminal")
async def admin_terminal(ws: WebSocket) -> None:
    await ws.accept()

    user = getattr(ws.state, "user", None)
    if user is None:
        await ws.close(code=4401)
        return
    if getattr(user, "role", None) != "admin":
        await ws.close(code=4403)
        return

    cols, rows = _parse_size(ws)
    repo = _repo_dir()
    pid = -1
    master_fd = -1
    pump_task: asyncio.Task | None = None
    exit_code: int | None = None
    shell_died = False

    try:
        pid, master_fd = await asyncio.get_running_loop().run_in_executor(
            None, _spawn_shell, repo, cols, rows
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("terminal spawn failed: %s", exc)
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
        return

    loop = asyncio.get_running_loop()

    async def pty_to_ws() -> None:
        nonlocal exit_code, shell_died
        try:
            while True:
                data = await loop.run_in_executor(None, _read_pty, master_fd)
                if not data:
                    shell_died = True
                    exit_code = _reap(pid)
                    if exit_code is None:
                        # EOF without reaped child yet — brief wait.
                        await asyncio.sleep(0.05)
                        exit_code = _reap(pid)
                    break
                if ws.client_state != WebSocketState.CONNECTED:
                    break
                await ws.send_bytes(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("pty→ws pump ended: %s", exc)
            shell_died = True
            if exit_code is None:
                exit_code = _reap(pid)

    try:
        # Inject omp after a short settle so bash is ready for input.
        await _inject_omp(master_fd, loop)
        pump_task = asyncio.create_task(pty_to_ws(), name="term-pty-to-ws")

        while True:
            if pump_task.done():
                break
            recv_task = asyncio.create_task(ws.receive(), name="term-ws-recv")
            done, pending = await asyncio.wait(
                {pump_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if recv_task in pending:
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if pump_task in done:
                # Drain recv if it also finished; ignore errors.
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
        # Close/kill first so a blocked os.read in the pump unblocks.
        code = await _kill_child(pid if pid > 0 else 0, master_fd if master_fd >= 0 else None)
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

        if shell_died and ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_text(
                    json.dumps(
                        {"type": "exit", "code": int(exit_code if exit_code is not None else 0)},
                        separators=(",", ":"),
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        log.info(
            "terminal closed email=%s pid=%s exit=%s",
            getattr(user, "email", "?"),
            pid,
            exit_code,
        )
