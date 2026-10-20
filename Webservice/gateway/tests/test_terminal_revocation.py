"""Passive terminal authority uses real session rows and inert PTY markers."""
from __future__ import annotations

import asyncio
import os
import pty
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from gateway import accounts, auth, authn, db, terminal


class PassiveWebSocket:
    def __init__(self, user, sid, name):
        self.state = SimpleNamespace(user=user)
        self.scope = {
            "state": {"user": user},
            "headers": [(b"cookie", f"{authn.SESSION_COOKIE}={sid}".encode())],
        }
        self.query_params = {"name": name}
        self.client_state = WebSocketState.CONNECTING
        self.payloads = []
        self.closed = asyncio.Event()
        self.received = asyncio.Queue()
        self.receive_calls = 0
        self.close_code = None

    async def accept(self):
        self.client_state = WebSocketState.CONNECTED

    async def close(self, code=1000, **kwargs):
        self.close_code = code
        self.client_state = WebSocketState.DISCONNECTED
        self.closed.set()

    async def send_text(self, payload):
        self.payloads.append(payload)
        self.received.put_nowait(payload)

    async def send_bytes(self, payload):
        self.payloads.append(payload)
        self.received.put_nowait(payload)

    async def receive(self):
        self.receive_calls += 1
        await asyncio.Future()  # deliberately no inbound frames, even on close


@pytest.mark.parametrize("tmux_pool", [False, True])
@pytest.mark.parametrize("change,code", [("session", 4401), ("demote", 4403), ("suspend", 4403)])
@pytest.mark.parametrize("second_marker", [False, True], ids=["silent", "outbound"])
def test_passive_terminal_revocation_detaches_only_its_endpoint(
    pg, tmp_path, monkeypatch, tmux_pool, change, code, second_marker
):
    settings = terminal.settings
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "terminal_enabled", True)
    monkeypatch.setattr(terminal, "_TMUX", "inert-tmux" if tmux_pool else None)
    monkeypatch.setattr(terminal, "_AUTH_RECHECK_S", 0.05)
    monkeypatch.setattr(terminal, "_repo_dir", lambda: tmp_path)
    for email in ("revoked@example.com", "valid@example.com"):
        accounts.ensure_account(email)
        accounts.set_status(email, "active", decided_by="fixture")
        with db.tx() as cur:
            cur.execute("UPDATE accounts SET role = 'admin' WHERE email = %s", (email,))

    endpoints = {}
    detached = []
    sessions = [{"id": "shared-fixture", "label": "shared-fixture", "shared": True}]
    monkeypatch.setattr(terminal, "_list_sessions", lambda: sessions)
    monkeypatch.setattr(terminal, "_session_exists", lambda name: True)
    monkeypatch.setattr(terminal, "_ensure_scroll_defaults", lambda: None)

    def spawn(*args):
        master, slave = pty.openpty()
        endpoints[master] = slave
        return master + 1000000, master  # inert pid; never passed to os.kill/waitpid

    async def detach(pid, fd):
        detached.append(fd)
        os.close(endpoints.pop(fd))
        os.close(fd)
        return 0

    async def inject(*args):
        pass  # no shell exists; marker producers own these PTYs

    def refuse_session_kill(*args):
        pytest.fail("revocation must never destroy a persistent/shared session")

    monkeypatch.setattr(terminal, "_spawn_shell", spawn)
    monkeypatch.setattr(terminal, "_spawn_attach", spawn)
    monkeypatch.setattr(terminal, "_kill_child", detach)
    monkeypatch.setattr(terminal, "_inject_chud", inject)
    monkeypatch.setattr(terminal, "_reap", lambda pid: 0)
    monkeypatch.setattr(terminal, "_kill_session", refuse_session_kill)

    async def run():
        revoked_user = auth.User(email="revoked@example.com", display="Revoked", role="admin", heavy=False)
        valid_user = auth.User(email="valid@example.com", display="Valid", role="admin", heavy=False)
        revoked_sid = authn.create_session(revoked_user.email)
        sockets = [
            PassiveWebSocket(revoked_user, revoked_sid, "shared-fixture"),
            PassiveWebSocket(valid_user, authn.create_session(valid_user.email), "shared-fixture"),
            PassiveWebSocket(valid_user, authn.create_session(valid_user.email), "shared-fixture"),
        ]
        tasks = []
        fds = []
        try:
            # Sequential attachment makes inert endpoint ownership unambiguous.
            for ws in sockets:
                previous = set(endpoints)
                tasks.append(asyncio.create_task(terminal.admin_terminal(ws)))
                async with asyncio.timeout(2):
                    while set(endpoints) == previous:
                        await asyncio.sleep(0.001)
                fd = (set(endpoints) - previous).pop()
                fds.append(fd)
                if tmux_pool:
                    await asyncio.wait_for(ws.received.get(), 2)  # session metadata
                os.write(endpoints[fd], b"first-marker")
                assert await asyncio.wait_for(ws.received.get(), 2) == b"first-marker"

            started = asyncio.get_running_loop().time()
            if change == "session":
                authn.revoke_session(revoked_sid)
            elif change == "demote":
                with db.tx() as cur:
                    cur.execute("UPDATE accounts SET role = 'user' WHERE email = %s", (revoked_user.email,))
            else:
                # Keep the credential live to isolate the current-account check.
                with db.tx() as cur:
                    cur.execute("UPDATE accounts SET status = 'suspended' WHERE email = %s", (revoked_user.email,))
            if second_marker:
                os.write(endpoints[fds[0]], b"second-marker")
            await asyncio.wait_for(sockets[0].closed.wait(), terminal._AUTH_RECHECK_S + 0.5)
            assert asyncio.get_running_loop().time() - started < terminal._AUTH_RECHECK_S + 0.5
            await asyncio.wait_for(tasks[0], 2)
            assert sockets[0].close_code == code
            assert b"second-marker" not in sockets[0].payloads
            assert detached == [fds[0]]
            assert sockets[0].receive_calls > 0
            # Other endpoint attachments to the same session keep receiving.
            for ws, fd in zip(sockets[1:], fds[1:]):
                assert not ws.closed.is_set()
                os.write(endpoints[fd], b"second-marker")
                assert await asyncio.wait_for(ws.received.get(), 2) == b"second-marker"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
