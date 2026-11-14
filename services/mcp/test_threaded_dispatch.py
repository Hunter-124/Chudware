"""stdio server dispatch: sync tools run off the event loop, one at a time.

HERC rover audit (2026-11-13): FastMCP ran every sync tool inline on the event loop, so a
long call stalled the session, the client's cancel for a timed-out call could not be
processed and Claude Code dropped the server ("Connection closed").
"""
from __future__ import annotations

import threading
import time

import anyio
import pytest
from mcp.server.fastmcp import FastMCP

from services.mcp import server as srv


def _server():
    mcp = FastMCP("dispatch-test")
    ran: list[str] = []
    release = threading.Event()

    @mcp.tool()
    def slow() -> str:
        release.wait(10)
        ran.append("slow")
        return "slow"

    @mcp.tool()
    def mutate() -> str:
        ran.append("mutate")
        return "mutate"

    @mcp.tool()
    def get_job() -> str:  # ungated: polling must not wait behind a running body
        ran.append("get_job")
        return "job"

    srv._install_threaded_dispatch(mcp)
    return mcp, ran, release


def test_sync_tools_leave_the_event_loop_free():
    mcp, ran, release = _server()
    ticks = []

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp.call_tool, "slow", {})

            async def heartbeat():
                for _ in range(5):
                    ticks.append(time.monotonic())
                    await anyio.sleep(0.02)
            await heartbeat()
            assert await mcp.call_tool("get_job", {}) is not None
            release.set()

    anyio.run(main)
    assert len(ticks) == 5, "the loop must keep running while a sync tool works"
    assert ran.index("get_job") < ran.index("slow"), "get_job is not gated behind slow"


def test_read_tools_run_beside_a_running_mutation():
    mcp, ran, release = _server()

    @mcp.tool()
    def list_parts() -> str:  # read-class: must not queue behind a long rebuild
        ran.append("list_parts")
        return "parts"

    @mcp.tool()
    def cad_guide() -> str:
        ran.append("cad_guide")
        return "guide"

    mcp._tool_manager._chudware_threaded = False
    srv._install_threaded_dispatch(mcp)

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp.call_tool, "slow", {})
            await anyio.sleep(0.05)
            with anyio.fail_after(2):
                await mcp.call_tool("list_parts", {})
                await mcp.call_tool("cad_guide", {})
            release.set()

    anyio.run(main)
    assert ran == ["list_parts", "cad_guide", "slow"]
    assert srv._tool_is_gated("add_features") and srv._tool_is_gated("save_part")
    assert not srv._tool_is_gated("check_interference")


def test_mutations_serialize_per_document_not_globally():
    mcp = FastMCP("doc-gate-test")
    ran: list[str] = []
    release = threading.Event()

    @mcp.tool()
    def add_features(doc_id: str, tag: str) -> str:
        if tag == "first":
            release.wait(10)
        ran.append(tag)
        return tag

    srv._install_threaded_dispatch(mcp)

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp.call_tool, "add_features", {"doc_id": "doc-a", "tag": "first"})
            await anyio.sleep(0.05)
            tg.start_soon(mcp.call_tool, "add_features", {"doc_id": "doc-a", "tag": "same-doc"})
            with anyio.fail_after(2):  # another document is not held up by doc-a
                await mcp.call_tool("add_features", {"doc_id": "doc-b", "tag": "other-doc"})
            release.set()

    anyio.run(main)
    assert ran == ["other-doc", "first", "same-doc"]


def test_a_call_cancelled_while_queued_never_runs():
    mcp, ran, release = _server()

    async def main():
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp.call_tool, "slow", {})
            await anyio.sleep(0.05)
            with anyio.move_on_after(0.1):
                await mcp.call_tool("mutate", {})  # queued behind slow, then cancelled
            release.set()
        await anyio.sleep(0.2)

    anyio.run(main)
    assert ran == ["slow"], "a timed-out queued mutation must not land later"


def test_client_call_budget_follows_mcp_tool_timeout(monkeypatch):
    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    assert srv._client_call_budget_s() == 50.0
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "300000")
    assert srv._client_call_budget_s() == 290.0


def test_wait_job_budget_reader(monkeypatch):
    from services.mcp.cad_doc_tools import _mcp_call_budget_s

    monkeypatch.delenv("CHUDWARE_MCP_CALL_BUDGET_S", raising=False)
    assert _mcp_call_budget_s() is None
    monkeypatch.setenv("CHUDWARE_MCP_CALL_BUDGET_S", "50")
    assert _mcp_call_budget_s() == 50.0


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows candidate order")
def test_windows_spawns_the_staged_hostd_the_gui_uses():
    from services.mcp import daemon_bridge

    names = [str(p).replace("\\", "/") for p in daemon_bridge._hostd_candidates()]
    staged = next(i for i, p in enumerate(names) if p.endswith("dist/Chudware/chudware-hostd.exe"))
    legacy = next(i for i, p in enumerate(names) if "/host/build-win/" in p)
    assert staged < legacy


def test_project_dir_defaults_to_a_project_cwd(monkeypatch, tmp_path):
    for k in srv._PROJECT_ROOT_VARS:
        monkeypatch.setenv(k, "")  # record the prior value so teardown restores it
        monkeypatch.delenv(k)
    project = tmp_path / "HERC-Rover"
    project.mkdir()
    monkeypatch.chdir(project)
    srv._default_project_dir_from_cwd()
    import os
    assert os.environ["CHUDWARE_PROJECT_DIR"] == str(project.resolve())
    assert os.environ["CHUDWARE_PROJECT_SAVE_DIR"] == str(project.resolve())


def test_project_dir_never_adopts_the_checkout_or_an_explicit_setting(monkeypatch, tmp_path):
    import os
    from services.mcp import core_bridge as cb

    for k in srv._PROJECT_ROOT_VARS:
        monkeypatch.setenv(k, "")  # record the prior value so teardown restores it
        monkeypatch.delenv(k)
    monkeypatch.chdir(cb._repo_root())
    srv._default_project_dir_from_cwd()
    assert not any(os.environ.get(k) for k in srv._PROJECT_ROOT_VARS)

    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    srv._default_project_dir_from_cwd()
    assert "CHUDWARE_PROJECT_DIR" not in os.environ
