"""Behavioral maintenance boundaries using protected state and disposable IPC."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import tempfile
import threading
import uuid
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import cells
import deployment_drain as drain
import engine_bridge
import mailer
import marketplace_render
import provider_broker
from auth import AuthMiddleware
from config import settings
from db import tx

LEASE = "a" * 64
FOREIGN = "b" * 64


@pytest.fixture
def admission(tmp_path, monkeypatch):
    root = tmp_path / "private-gateway"
    root.mkdir(mode=0o700)
    owner = drain.AdmissionController(root)
    monkeypatch.setattr(drain, "_controller", owner)
    return owner


def test_durable_restart_ownership_and_idempotency(admission):
    admission.begin(LEASE)
    admission.begin(LEASE)
    restart = drain.AdmissionController(admission.root)
    assert restart.closed
    for action in (restart.begin, restart.require, restart.release):
        with pytest.raises(HTTPException) as error:
            action(FOREIGN)
        assert error.value.status_code == 409
        assert restart.closed
    restart.release(LEASE)
    restart.release(LEASE)
    assert not drain.AdmissionController(admission.root).closed
    assert (admission.root / ".deployment-maintenance/lease.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "foreign_owner", "hardlink", "invalid", "oversized", "fifo", "directory_mode"])
def test_unsafe_lease_never_opens_admission(admission, unsafe, monkeypatch):
    admission.begin(LEASE)
    path = admission.root / ".deployment-maintenance/lease.json"
    if unsafe == "symlink":
        path.unlink()
        path.symlink_to(admission.root / "absent")
    elif unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "foreign_owner":
        original = os.fstat
        def foreign_owner(fd):
            info = original(fd)
            if stat.S_ISREG(info.st_mode):
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info
        monkeypatch.setattr(os, "fstat", foreign_owner)
    elif unsafe == "hardlink":
        os.link(path, admission.root / "another-link")
    elif unsafe == "invalid":
        path.write_bytes(b'{"schema":1,"admission":"open"}')
    elif unsafe == "oversized":
        path.write_bytes(b" " * ((1 << 20) + 1))
    elif unsafe == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.parent.chmod(0o755)
    restart = drain.AdmissionController(admission.root)
    assert restart.closed and restart.unsafe
    with pytest.raises(HTTPException) as error:
        restart.begin(LEASE)
    assert error.value.status_code == 503
    with pytest.raises(HTTPException):
        restart.acquire(inherit=False)


def test_missing_and_tenant_mounted_state_fail_closed(admission, monkeypatch):
    admission.begin(LEASE)
    (admission.root / ".deployment-maintenance/lease.json").unlink()
    with pytest.raises(HTTPException):
        admission.require(LEASE)
    assert admission.closed
    monkeypatch.setattr(settings, "workspace_root", admission.root)
    assert drain.AdmissionController(admission.root).unsafe


def test_admission_close_race_has_no_uncounted_acceptance(admission):
    barrier = threading.Barrier(2)
    def close():
        barrier.wait()
        admission.begin(LEASE)
    def accept():
        barrier.wait()
        try:
            return admission.acquire("http_requests", inherit=False)
        except HTTPException:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        closing, accepting = executor.submit(close), executor.submit(accept)
        closing.result(5)
        token = accepting.result(5)
    assert admission.closed
    assert admission.snapshot()["http_requests"] == (token is not None)
    if token:
        with token.bind():
            child = admission.acquire()
        token.close()
        with token.bind(), pytest.raises(HTTPException):
            admission.acquire()
        child.close()
    assert admission.snapshot() == {"http_requests": 0, "gateway_jobs": 0}


def test_admin_routes_do_not_promote_accounts(admission, activate, monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", True)
    admin = activate("drain-admin@test.invalid", role="admin")
    ordinary = activate("drain-user@test.invalid")
    async def empty():
        return []
    monkeypatch.setattr(cells, "service_units", empty)
    application = FastAPI()
    application.add_middleware(AuthMiddleware)
    application.add_middleware(drain.AdmissionMiddleware)
    application.include_router(drain.router)
    with TestClient(application) as client:
        for name in ("begin", "exit-cells", "release"):
            assert client.post(f"/api/_ops/deployment-drain/{name}", json={"lease_id": LEASE}).status_code == 401
            assert client.post(f"/api/_ops/deployment-drain/{name}", json={"lease_id": LEASE},
                               headers={"X-Dev-User": ordinary}).status_code == 403
        assert client.get("/api/_ops/deployment-drain/status", params={"lease_id": LEASE},
                          headers={"X-Dev-User": ordinary}).status_code == 403
        headers = {"X-Dev-User": admin}
        began = client.post("/api/_ops/deployment-drain/begin", json={"lease_id": LEASE}, headers=headers)
        assert began.status_code == 200
        assert began.json()["admission"] == "closed"
        assert began.json()["complete"] and began.json()["quiescent"]
        for name in ("begin", "exit-cells", "release"):
            assert client.post(f"/api/_ops/deployment-drain/{name}", json={"lease_id": FOREIGN},
                               headers=headers).status_code == 409
        assert client.get("/api/_ops/deployment-drain/status", params={"lease_id": FOREIGN},
                          headers=headers).status_code == 409
        assert client.post("/api/_ops/deployment-drain/release", json={"lease_id": LEASE},
                           headers=headers).json()["admission"] == "open"


def test_access_log_never_contains_lease_query():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %s',
                               ("local", "GET", "/api/_ops/deployment-drain/status?lease_id=" + LEASE, "1.1", 200), None)
    assert drain._OpsAccessFilter().filter(record)
    assert LEASE not in record.getMessage()


def test_http_stream_and_websocket_lifetimes(admission):
    async def scenario(kind):
        started, finish, sent = asyncio.Event(), asyncio.Event(), []
        async def application(scope, receive, send):
            started.set()
            if kind == "http":
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"first", "more_body": True})
            else:
                await send({"type": "websocket.accept"})
            await finish.wait()
            if kind == "http":
                await send({"type": "http.response.body", "body": b"last", "more_body": False})
            else:
                await send({"type": "websocket.close", "code": 1000})
        async def send(message):
            sent.append(message)
        async def receive():
            return {"type": "http.request", "body": b""}
        middleware = drain.AdmissionMiddleware(application)
        scope = {"type": kind, "path": "/accepted", "method": "GET", "headers": [], "query_string": b""}
        task = asyncio.create_task(middleware(scope, receive, send))
        await started.wait()
        counter = "http_requests" if kind == "http" else "gateway_jobs"
        assert admission.snapshot()[counter] == 1
        admission.begin(LEASE)
        refused = []
        async def reject_send(message):
            refused.append(message)
        await middleware(scope, receive, reject_send)
        assert refused[0].get("status", refused[0].get("code")) in {503, 1013}
        assert not task.done() and admission.snapshot()[counter] == 1
        finish.set()
        await task
        assert admission.snapshot()[counter] == 0
        assert sent[-1]["type"] == ("http.response.body" if kind == "http" else "websocket.close")
        admission.release(LEASE)
    asyncio.run(scenario("http"))
    asyncio.run(scenario("websocket"))


def test_canceled_http_executor_work_retains_accepted_context(admission):
    started, finish = threading.Event(), threading.Event()
    def write():
        started.set()
        assert finish.wait(5)
        assert admission.accepted()
        return "persisted"
    async def scenario():
        task = asyncio.create_task(drain.run_blocking(write))
        assert await asyncio.to_thread(started.wait, 5)
        admission.begin(LEASE)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert admission.snapshot()["gateway_jobs"] == 1
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admission.snapshot()["gateway_jobs"] == 0
    try:
        asyncio.run(scenario())
    finally:
        finish.set()


def test_accepted_context_can_activate_after_closure(admission, monkeypatch):
    activated = []
    async def ensure(*args):
        pass
    async def start(cell):
        activated.append(cell)
    async def opened(**kwargs):
        return "reader", "writer"
    principal = cells.CellPrincipal("uid", "user:uid", "uid", "owner", 0, "c" * 32,
                                    Path("/work"), "", "studio")
    monkeypatch.setattr(cells, "prepare_cell", lambda p: Path("/private-cell"))
    monkeypatch.setattr(provider_broker, "ensure_cell", ensure)
    monkeypatch.setattr(cells, "start_cell", start)
    monkeypatch.setattr(cells, "_connect_pinned", lambda p: object())
    monkeypatch.setattr(cells.asyncio, "open_unix_connection", opened)
    async def scenario():
        accepted = admission.acquire("http_requests")
        with accepted.bind():
            admission.begin(LEASE)
            assert await cells.connect_cell(principal) == ("reader", "writer")
        accepted.close()
        with pytest.raises(HTTPException):
            await cells.connect_cell(principal)
        assert activated == [principal.cell_id]
        assert admission.snapshot() == {"http_requests": 0, "gateway_jobs": 0}
    asyncio.run(scenario())


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """The real subprocess probe targets our read-only disposable manager binary."""
    state = tmp_path / "manager.json"
    binary = tmp_path / "systemctl"
    binary.write_text('''#!/usr/bin/env python3
import json, os, sys
value = json.load(open(os.environ["CHUDWARE_TEST_MANAGER"]))
if value.get("error"):
    sys.exit(1)
if "list-units" in sys.argv:
    print(json.dumps([{"unit": "chudware-cell@" + key + ".service"} for key in value["units"]]))
elif "show" in sys.argv:
    name = sys.argv[sys.argv.index("show") + 1]
    key = name.split("@", 1)[1].split(".", 1)[0]
    row = value["units"][key]
    fields = {"Id": name, "LoadState": row.get("load", "loaded"), "ActiveState": row["active"],
              "SubState": row["sub"], "MainPID": str(row["pid"]), "InvocationID": row["invocation"]}
    for key, item in fields.items(): print(key + "=" + item)
else:
    sys.exit(99)
''')
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CHUDWARE_TEST_MANAGER", str(state))
    def publish(units, **extra):
        state.write_text(json.dumps({"units": units, **extra}))
    publish({})
    return publish


def census(*, active=0, unknown=0, dirty=0):
    return {"schema": 1, "admission": "open", "generation": 0, "sessions": 0, "rpc_inflight": 0,
            "native": {"cache_verification": 0, "profiler": 0, "dirty_documents": dirty},
            "workers": {"busy": 0, "background": active, "unknown": unknown},
            "active": active + unknown + dirty, "complete": not unknown,
            "quiescent": not (active or unknown or dirty), "exiting": False}


@asynccontextmanager
async def native_cell(monkeypatch, *, activity=None, wrong_identity=False):
    with tempfile.TemporaryDirectory(prefix="cw-drain-", dir="/tmp") as base:
        monkeypatch.setattr(settings, "cells_root", Path(base))
        key = "user:operator-fixture"
        cell = hashlib.sha256(key.encode()).hexdigest()[:32]
        directory = Path(base) / cell
        directory.mkdir(mode=0o700)
        run = directory / "state/run"
        run.mkdir(parents=True, mode=0o700)
        metadata = directory / "cell.json"
        metadata.write_text(json.dumps({"schema": "chudware-cell-v1", "cell_id": cell,
                                        "workspace_key": key, "root": "/work"}))
        metadata.chmod(0o600)
        value, calls = activity or census(), []
        async def client(reader, writer):
            try:
                while raw := await reader.readline():
                    request = json.loads(raw)
                    calls.append(request)
                    if request["method"] == "hello":
                        assert request["params"] == {"client": "control", "operator": True, "proto": 1}
                        result = {"operator": True, "cell_id": "f" * 32 if wrong_identity else cell}
                    else:
                        assert request["method"] in {"cell_census", "cell_drain"}
                        params = request["params"]
                        if request["method"] == "cell_drain":
                            if params["admission"] == "open":
                                assert params["generation"] == value["generation"]
                            if params["admission"] != value["admission"]:
                                value["generation"] += 1
                                value["admission"] = params["admission"]
                            if params.get("exit") == "when_quiescent" and value["quiescent"]:
                                value["exiting"] = True
                        result = dict(value)
                    writer.write(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_unix_server(client, path=str(run / "operator.sock"))
        (run / "operator.sock").chmod(0o600)
        unit = {"active": "active", "sub": "running", "pid": 123, "invocation": "d" * 32}
        try:
            yield cell, unit, value, calls
        finally:
            server.close()
            await server.wait_closed()


def test_actual_manager_enumeration_inactive_transition_and_error(manager, monkeypatch):
    inactive, transition = "1" * 32, "2" * 32
    manager({inactive: {"active": "inactive", "sub": "dead", "pid": 0, "invocation": ""},
             transition: {"active": "activating", "sub": "start", "pid": 99, "invocation": "e" * 32}})
    def forbidden(*args):
        raise AssertionError("inactive/transitioning cell connected")
    monkeypatch.setattr(cells, "_operator_socket", forbidden)
    first = asyncio.run(cells.deployment_census(close=True))
    assert first["cells_active"] == first["cells_unknown"] == 1
    assert not first["complete"] and not first["quiescent"]
    manager({}, error=True)
    unavailable = asyncio.run(cells.deployment_census())
    assert unavailable["cells_unknown"] == 1 and not unavailable["quiescent"]
    manager({inactive: {"active": "inactive", "sub": "dead", "pid": 0, "invocation": ""}})
    quiet = asyncio.run(cells.deployment_census(close=True, exit_cells=True))
    assert quiet["complete"] and quiet["quiescent"] and quiet["cells_active"] == 0


def test_exit_request_replay_remains_idempotent_while_units_exit(admission, manager, monkeypatch):
    class Admin:
        role = "admin"
    request = Request({"type": "http", "state": {"user": Admin()}})
    async def scenario():
        async with native_cell(monkeypatch) as (cell, unit, value, calls):
            manager({cell: unit})
            admission.begin(LEASE)
            first = await drain.exit_cells(request, {"lease_id": LEASE})
            assert first["cells_exiting"] == 1 and not first["quiescent"]
            assert drain.AdmissionController(admission.root).state["exit_requested"]
            repeated = await drain.exit_cells(request, {"lease_id": LEASE})
            assert repeated["admission"] == "closed" and not repeated["quiescent"]
            assert sum(row["params"].get("exit") == "when_quiescent" for row in calls) == 1
    asyncio.run(scenario())


def test_native_close_restart_reopen_generation_and_self_exit(admission, manager, monkeypatch):
    async def scenario():
        async with native_cell(monkeypatch) as (cell, unit, value, calls):
            manager({cell: unit})
            admission.begin(LEASE)
            first = await drain._counters(LEASE)
            assert first["quiescent"] and first["cells_active"] == 1
            assert value["admission"] == "closed" and value["generation"] == 1
            restart = drain.AdmissionController(admission.root)
            monkeypatch.setattr(drain, "_controller", restart)
            assert restart.state["cells"][cell] == {"generation": 1, "invocation": unit["invocation"]}
            reopened = await drain._counters(LEASE, reopen=True)
            assert reopened["cells_unknown"] == 0
            assert value["admission"] == "open" and value["generation"] == 2
            await drain._counters(LEASE)
            exited = await drain._counters(LEASE, exit_cells=True)
            assert not exited["quiescent"] and exited["cells_active"] == exited["cells_exiting"] == 1
            assert any(row["params"].get("exit") == "when_quiescent" for row in calls)
            manager({cell: {"active": "inactive", "sub": "dead", "pid": 0, "invocation": ""}})
            stopped = await drain._counters(LEASE)
            assert stopped["quiescent"] and all(stopped[key] == 0 for key in
                                                ("cells_active", "cells_unknown", "cells_exiting"))
    asyncio.run(scenario())


def test_gateway_counters_hold_native_admission_open_until_producers_finish(admission, manager, monkeypatch):
    bridges, streams = [1], [0]
    monkeypatch.setattr(engine_bridge, "active_bridge_count", lambda: bridges[0])
    monkeypatch.setattr(provider_broker, "active_request_count", lambda: streams[0])
    async def scenario():
        async with native_cell(monkeypatch) as (cell, unit, value, calls):
            manager({cell: unit})
            admission.begin(LEASE)
            assert not (await drain._counters(LEASE))["quiescent"]
            assert value["admission"] == "open"
            bridges[0], streams[0] = 0, 1
            assert not (await drain._counters(LEASE))["quiescent"]
            assert value["admission"] == "open"
            streams[0] = 0
            assert (await drain._counters(LEASE))["quiescent"]
            assert value["admission"] == "closed"
    asyncio.run(scenario())


@pytest.mark.parametrize("activity", [census(active=2), census(unknown=1), census(dirty=1)])
def test_busy_unknown_dirty_cells_never_receive_exit(manager, monkeypatch, activity):
    async def scenario():
        async with native_cell(monkeypatch, activity=activity) as (cell, unit, value, calls):
            manager({cell: unit})
            result = await cells.deployment_census(close=True, exit_cells=True)
            assert not result["quiescent"] and result["cell_jobs"] >= 1
            assert not any(row["params"].get("exit") == "when_quiescent" for row in calls)
            assert value["admission"] == "closed"
    asyncio.run(scenario())


@pytest.mark.parametrize("malformed", [
    {**census(), "admission": []},
    {**census(), "active": False},
    {**census(), "native": {"cache_verification": 0, "profiler": 0}},
    {**census(unknown=1), "complete": True, "quiescent": True},
])
def test_missing_or_malformed_native_counters_are_unknown(manager, monkeypatch, malformed):
    async def scenario():
        async with native_cell(monkeypatch, activity=malformed) as (cell, unit, value, calls):
            manager({cell: unit})
            result = await cells.deployment_census(close=True, exit_cells=True)
            assert result["cells_unknown"] == 1 and not result["complete"] and not result["quiescent"]
            assert not any(row["method"] == "cell_drain" for row in calls)
    asyncio.run(scenario())


def test_foreign_identity_and_invocation_fence_fail_closed(manager, monkeypatch):
    async def scenario():
        async with native_cell(monkeypatch, wrong_identity=True) as (cell, unit, value, calls):
            manager({cell: unit})
            refused = await cells.deployment_census(close=True)
            assert refused["cells_unknown"] == 1 and not refused["complete"]
            assert [row["method"] for row in calls] == ["hello"]
        async with native_cell(monkeypatch) as (cell, unit, value, calls):
            manager({cell: unit})
            await cells.deployment_census(close=True)
            refused = await cells.deployment_census(reopen=True, fences={cell: {
                "generation": value["generation"], "invocation": "e" * 32}})
            assert refused["cells_unknown"] == 1 and value["admission"] == "closed"
    asyncio.run(scenario())


def test_unknown_worker_does_not_prevent_fenced_abort_reopen(manager, monkeypatch):
    async def scenario():
        async with native_cell(monkeypatch, activity=census(unknown=1)) as (cell, unit, value, calls):
            manager({cell: unit})
            fences = {}
            await cells.deployment_census(close=True, record=lambda c, i, g: fences.update({c: {
                "invocation": i, "generation": g}}))
            reopened = await cells.deployment_census(reopen=True, fences=fences)
            assert value["admission"] == "open" and reopened["cells_unknown"] == 0
            assert not reopened["complete"] and not reopened["quiescent"]
    asyncio.run(scenario())


def test_accepted_queued_marketplace_jobs_finish_after_closure(admission, monkeypatch):
    entered, finish, results = threading.Event(), threading.Event(), []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(marketplace_render, "_executor", executor)
    monkeypatch.setattr(marketplace_render, "_jobs", {})
    def render(identifier):
        entered.set()
        assert finish.wait(5)
        assert admission.accepted()
        results.append(identifier)
        return {"ok": True}
    monkeypatch.setattr(marketplace_render, "_render_listing_previews", render)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        jobs = [marketplace_render.queue_render(first), marketplace_render.queue_render(second)]
        assert entered.wait(5)
        admission.begin(LEASE)
        assert admission.snapshot()["gateway_jobs"] >= 2
        assert marketplace_render.queue_render(second) is jobs[1]
        with pytest.raises(HTTPException):
            marketplace_render.queue_render(str(uuid.uuid4()))
        finish.set()
        assert [job.result(5) for job in jobs] == [{"ok": True}, {"ok": True}]
        assert results == [first, second]
        assert admission.snapshot()["gateway_jobs"] == 0
    finally:
        finish.set()
        executor.shutdown(wait=True)


def test_mail_delivery_finishes_and_unclaimed_durable_queue_survives(admission, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    monkeypatch.setattr(settings, "mail_backend", "console")
    def deliver(*args):
        started.set()
        assert finish.wait(5)
    monkeypatch.setattr(mailer, "_deliver", deliver)
    mailer.send("first@test.invalid", "verify_email", {"email": "first@test.invalid",
                "verify_url": "https://test.invalid/verify", "expires_hours": 24})
    async def scenario():
        task = asyncio.create_task(mailer.drain_once())
        assert await asyncio.to_thread(started.wait, 5)
        admission.begin(LEASE)
        assert admission.snapshot()["gateway_jobs"] == 1
        mailer.send("second@test.invalid", "verify_email", {"email": "second@test.invalid",
                    "verify_url": "https://test.invalid/verify", "expires_hours": 24})
        assert await mailer.drain_once() == 0
        finish.set()
        assert await task == 1
        with tx() as cursor:
            cursor.execute("SELECT to_addr,sent_at,attempts FROM mail_outbox ORDER BY id")
            rows = cursor.fetchall()
        assert rows[0]["sent_at"] is not None
        assert rows[1]["sent_at"] is None and rows[1]["attempts"] == 0
        assert admission.snapshot()["gateway_jobs"] == 0
    try:
        asyncio.run(scenario())
    finally:
        finish.set()


def test_shutdown_waits_for_actual_mail_thread_completion(admission, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    monkeypatch.setattr(mailer, "_drain_task", None)
    monkeypatch.setattr(mailer, "_drain_stop", None)
    def deliver(*args):
        started.set()
        assert finish.wait(5)
    monkeypatch.setattr(mailer, "_deliver", deliver)
    mailer.send("shutdown@test.invalid", "verify_email", {"email": "shutdown@test.invalid",
                "verify_url": "https://test.invalid/verify", "expires_hours": 24})
    async def scenario():
        mailer.start_drain()
        assert await asyncio.to_thread(started.wait, 5)
        admission.begin(LEASE)
        stopped = asyncio.create_task(mailer.stop_drain())
        await asyncio.sleep(0)
        assert not stopped.done()
        assert admission.snapshot()["gateway_jobs"] == 1
        finish.set()
        await stopped
        assert admission.snapshot()["gateway_jobs"] == 0
        with tx() as cursor:
            cursor.execute("SELECT sent_at FROM mail_outbox")
            assert cursor.fetchone()["sent_at"] is not None
    try:
        asyncio.run(scenario())
    finally:
        finish.set()
