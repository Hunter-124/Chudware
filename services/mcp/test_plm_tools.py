"""End-to-end tests for the plm_* MCP tools (T-R2, contract §7).

Exercises the registered tools through a FastMCP instance the way an MCP client
would, asserting the §0 envelope shape and the ease-of-use invariants (§8).
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from mcp.server.fastmcp import FastMCP

from services.mcp.plm_tools import register_plm_tools

_GIT_VOCAB = {
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
}


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("thumbnail.png", b"\x89PNG_stub")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(wall=3.0):
    return {"schema": "aicad.part/1", "id": "u1", "name": "Bracket",
            "features": [{"id": "f1", "params": {"wall": wall}}]}


@pytest.fixture()
def tools():
    """Return a dict of {name: callable} for the registered plm_* tools."""
    mcp = FastMCP("test")
    register_plm_tools(mcp)
    reg = {}

    # FastMCP stores tools on an internal manager; call the underlying fns.
    # We re-register onto a capture shim to grab the raw callables.
    captured = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_plm_tools(_Capture())
    return captured


def _j(s: str) -> dict:
    return json.loads(s)


def test_plm_open_zero_config(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    res = _j(tools["plm_open"](str(tmp_path)))
    assert res["ok"] is True
    assert res["project_id"].startswith("p")
    assert len(res["manifest"]["items"]) == 1


def test_plm_save_and_history(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    tools["plm_open"](str(tmp_path))
    save = _j(tools["plm_checkpoint"](str(tmp_path), "first checkpoint"))
    assert save["ok"] is True
    hist = _j(tools["plm_history"](str(tmp_path)))
    assert hist["ok"] is True
    assert any(e["message"] == "first checkpoint" for e in hist["entries"])


def test_plm_save_rejects_bad_kind(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    tools["plm_open"](str(tmp_path))
    res = _j(tools["plm_save"](str(tmp_path), "release"))
    assert res["ok"] is False
    assert res["reason"] == "invalid_mode"


def test_plm_restore_roundtrip(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(3.0)))
    tools["plm_open"](str(tmp_path))
    cp = _j(tools["plm_checkpoint"](str(tmp_path), "v1"))
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(9.0)))
    tools["plm_save"](str(tmp_path), "auto")
    res = _j(tools["plm_restore"](str(tmp_path), cp["commit"]))
    assert res["ok"] is True
    with zipfile.ZipFile(io.BytesIO((tmp_path / "Bracket.step+").read_bytes())) as zf:
        doc = json.loads(zf.read("document.json"))
    assert doc["features"][0]["params"]["wall"] == 3.0


def test_plm_release_via_tool(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    st = _j(tools["plm_open"](str(tmp_path)))
    iid = st["manifest"]["items"][0]["item_id"]
    res = _j(tools["plm_release"](str(tmp_path), iid, "first release"))
    assert res["ok"] is True
    assert res["rev"] == "A"


def test_plm_status_shape(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    tools["plm_open"](str(tmp_path))
    res = _j(tools["plm_status"](str(tmp_path)))
    assert res["ok"] is True
    for key in ("items", "head", "dirty", "ai_mode", "remotes", "locks"):
        assert key in res


def test_plm_compare_reuses_doc_diff(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(3.0)))
    st = _j(tools["plm_open"](str(tmp_path)))
    iid = st["manifest"]["items"][0]["item_id"]
    a = _j(tools["plm_checkpoint"](str(tmp_path), "a"))
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(7.0)))
    b = _j(tools["plm_checkpoint"](str(tmp_path), "b"))
    res = _j(tools["plm_compare"](str(tmp_path), a["commit"], b["commit"], iid))
    assert res["ok"] is True
    # the wall param change surfaces via the reused doc_diff engine
    params = res["doc_diff"]["params_changed"]
    assert any(p["param"] == "wall" for p in params)


def test_tool_error_hints_no_git_vocab(tmp_path, tools):
    # a not-found restore returns a plain-language hint (contract §8.1)
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    tools["plm_open"](str(tmp_path))
    res = _j(tools["plm_restore"](str(tmp_path), "0" * 40))
    assert res["ok"] is False
    words = set(res["hint"].lower().replace(".", " ").split())
    assert not (words & _GIT_VOCAB), f"git vocab in hint: {res['hint']!r}"


def test_end_to_end_via_real_fastmcp_dispatch(tmp_path):
    """DoD 'plm_* exercised end-to-end via MCP client': dispatch through the real
    FastMCP.call_tool path, not the capture shim — open → checkpoint → history."""
    import asyncio

    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    mcp = FastMCP("plm-e2e")
    register_plm_tools(mcp)

    async def _run():
        _content, opened = await mcp.call_tool("plm_open", {"path": str(tmp_path)})
        opened = json.loads(opened["result"]) if isinstance(opened.get("result"), str) else opened["result"]
        assert opened["ok"] is True
        _content, cp = await mcp.call_tool(
            "plm_checkpoint", {"path": str(tmp_path), "message": "e2e checkpoint"})
        cp = json.loads(cp["result"]) if isinstance(cp.get("result"), str) else cp["result"]
        assert cp["ok"] is True
        _content, h = await mcp.call_tool("plm_history", {"path": str(tmp_path)})
        h = json.loads(h["result"]) if isinstance(h.get("result"), str) else h["result"]
        assert h["ok"] is True
        assert any(e["message"] == "e2e checkpoint" for e in h["entries"])

    asyncio.run(_run())
