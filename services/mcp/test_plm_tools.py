"""End-to-end tests for the plm_* MCP tools (contract §7).

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

def test_plm_checkpoint_defaults_to_resolved_project_root(tmp_path, tools, monkeypatch):
    project = tmp_path / "project"
    nested = project / "designs"
    (project / ".chudware").mkdir(parents=True)
    nested.mkdir()
    (nested / "Bracket.step+").write_bytes(_container(_part()))
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(nested))
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)

    checkpoint = _j(tools["plm_checkpoint"](message="resolved checkpoint"))

    assert checkpoint["ok"] is True
    history = _j(tools["plm_history"](str(project)))
    assert any(e["message"] == "resolved checkpoint" for e in history["entries"])


def test_plm_checkpoint_explicit_path_wins(tmp_path, tools, monkeypatch):
    inferred = tmp_path / "inferred"
    explicit = tmp_path / "explicit"
    inferred.mkdir()
    explicit.mkdir()
    (explicit / "Bracket.step+").write_bytes(_container(_part()))
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(inferred))

    checkpoint = _j(tools["plm_checkpoint"](
        path=str(explicit), message="explicit checkpoint"))

    assert checkpoint["ok"] is True
    assert not (inferred / ".plm").exists()
    history = _j(tools["plm_history"](str(explicit)))
    assert any(e["message"] == "explicit checkpoint" for e in history["entries"])


def test_plm_checkpoint_unresolved_root_names_resolution_mechanism(
        tools, monkeypatch):
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)

    result = _j(tools["plm_checkpoint"]())

    assert result["ok"] is False
    assert result["reason"] == "project_root_unavailable"
    assert "CHUDWARE_PROJECT_DIR" in result["hint"]
    assert "project_context.resolve_project_root()" in result["hint"]


def test_vault_backed_plm_family_exposes_optional_project_path():
    names = {
        "plm_init", "plm_open", "plm_save", "plm_checkpoint", "plm_release",
        "plm_item_set", "plm_item_add", "plm_link_repo", "plm_plan",
        "plm_where_used", "plm_history", "plm_restore", "plm_status",
        "plm_compare", "plm_set_ai_mode", "plm_add_remote", "plm_sync",
        "plm_lock", "plm_unlock",
    }
    mcp = FastMCP("plm-schema")
    register_plm_tools(mcp)
    schemas = {tool.name: tool.parameters for tool in mcp._tool_manager.list_tools()}

    for name in names:
        assert schemas[name]["properties"]["path"]["default"] == ""
        assert "path" not in schemas[name].get("required", [])

    assert schemas["plm_lock"]["properties"]["force"]["default"] is False
    assert schemas["plm_add_remote"]["properties"]["display_name"]["default"] == ""
    assert schemas["plm_add_remote"]["properties"]["account_id"]["default"] == ""

def test_plm_item_add_file_as_project_root_teaches_complete_call(tmp_path, tools):
    target = tmp_path / "spec.md"
    target.write_text("# Requirements\n")

    res = _j(tools["plm_item_add"](str(target), doc_type="spec"))

    assert res["ok"] is False
    assert res["reason"] == "invalid_project"
    assert (
        f'plm_item_add(path="{tmp_path}", file_path="spec.md")'
        in res["hint"]
    )
    assert "path is the PROJECT ROOT" in res["hint"]


def test_plm_item_add_empty_target_teaches_both_modes_and_example(tmp_path, tools):
    res = _j(tools["plm_item_add"](str(tmp_path)))

    assert res["ok"] is False
    assert res["reason"] == "invalid_mode"
    assert "file_path" in res["hint"]
    assert "folder_path" in res["hint"]
    assert (
        f'plm_item_add(path="{tmp_path}", file_path="documents/spec.md")'
        in res["hint"]
    )


def test_plm_item_add_already_tracked_is_success(tmp_path, tools):
    (tmp_path / "spec.md").write_text("# Requirements\n")
    first = _j(tools["plm_item_add"](str(tmp_path), file_path="spec.md"))
    second = _j(tools["plm_item_add"](str(tmp_path), file_path="spec.md"))

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["item"]["item_id"] == first["item"]["item_id"]
    assert second["item"]["already_tracked"] is True


def test_plm_item_add_container_is_terminal_for_every_argument_shape(tmp_path, tools):
    target = tmp_path / "Bracket.step+"
    target.write_bytes(_container(_part()))
    calls = [
        {"path": str(target), "doc_type": "part"},
        {
            "path": str(target),
            "file_path": str(target),
            "doc_type": "part",
        },
        {
            "path": str(tmp_path),
            "file_path": "Bracket.step+",
            "doc_type": "part",
        },
    ]

    for supplied in calls:
        res = _j(tools["plm_item_add"](**supplied))
        assert res["ok"] is False, supplied
        assert res["reason"] == "invalid_mode", supplied
        assert "plm_checkpoint" in res["hint"], supplied
        assert "do not register them by hand" in res["hint"], supplied


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

def test_plm_checkpoint_released_item_names_new_revision_call(tmp_path, tools):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(3.0)))
    opened = _j(tools["plm_open"](str(tmp_path)))
    item_id = opened["manifest"]["items"][0]["item_id"]
    released = _j(tools["plm_release"](
        str(tmp_path), item_id, "first release"))
    assert released["ok"] is True
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(9.0)))

    checkpoint = _j(tools["plm_checkpoint"](
        str(tmp_path), "edit after release"))

    assert checkpoint["ok"] is False
    assert checkpoint["reason"] == "locked"
    assert (
        f"plm_item_set(path='<project-root>', item_id={item_id!r}, "
        "start_new_rev=True)"
    ) in checkpoint["hint"]


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


def test_plm_compare_routes_generic_item_to_generic_compare(tmp_path, tools, monkeypatch):
    """A non-CAD item (docs/code/link) must route through compare.generic_compare,
    not the geometric doc_diff path — parity with worker_rpc._plm_compare."""
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    st = _j(tools["plm_open"](str(tmp_path)))
    iid = st["manifest"]["items"][0]["item_id"]
    a = _j(tools["plm_checkpoint"](str(tmp_path), "a"))
    b = _j(tools["plm_checkpoint"](str(tmp_path), "b"))

    from services.plm import compare as compare_mod

    def fake_is_generic(vault, item_id):
        return True

    def fake_generic_compare(vault, commit_a, commit_b, item_id):
        assert (commit_a, commit_b, item_id) == (a["commit"], b["commit"], iid)
        return {"mode": "generic", "summary": "renamed field.txt",
               "text_diff": "--- before\n+++ after\n"}

    def fail_docs_at(*a, **k):
        raise AssertionError("generic item must not call docs_at")

    def fail_semantic_compare(*a, **k):
        raise AssertionError("generic item must not call semantic_compare")

    monkeypatch.setattr(compare_mod, "is_generic_item", fake_is_generic)
    monkeypatch.setattr(compare_mod, "generic_compare", fake_generic_compare)
    monkeypatch.setattr(compare_mod, "docs_at", fail_docs_at)
    monkeypatch.setattr(compare_mod, "semantic_compare", fail_semantic_compare)

    res = _j(tools["plm_compare"](str(tmp_path), a["commit"], b["commit"], iid))
    assert res["ok"] is True
    assert res["mode"] == "generic"
    assert res["summary"] == "renamed field.txt"
    assert res["text_diff"] == "--- before\n+++ after\n"
    assert "doc_diff" not in res


def test_plm_compare_routes_cad_item_through_semantic_compare(tmp_path, tools, monkeypatch):
    """A CAD item — including an ECAD board/schematic, since semantic_compare
    now routes those internally — must go through docs_at + semantic_compare,
    never generic_compare."""
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    st = _j(tools["plm_open"](str(tmp_path)))
    iid = st["manifest"]["items"][0]["item_id"]
    a = _j(tools["plm_checkpoint"](str(tmp_path), "a"))
    b = _j(tools["plm_checkpoint"](str(tmp_path), "b"))

    from services.plm import compare as compare_mod

    ecad_doc_a = {"schema": "aicad.pcb/1", "id": "board1", "nets": []}
    ecad_doc_b = {"schema": "aicad.pcb/1", "id": "board1", "nets": [{"id": "n1"}]}

    def fake_is_generic(vault, item_id):
        return False

    def fake_docs_at(vault, commit_a, commit_b, item_id):
        assert (commit_a, commit_b, item_id) == (a["commit"], b["commit"], iid)
        return ecad_doc_a, ecad_doc_b

    def fake_semantic_compare(doc_a, doc_b):
        assert (doc_a, doc_b) == (ecad_doc_a, ecad_doc_b)
        return {"doc_diff": {"nets_added": ["n1"]}, "summary": "added net n1"}

    def fail_generic_compare(*a, **k):
        raise AssertionError("CAD/ECAD item must not call generic_compare")

    monkeypatch.setattr(compare_mod, "is_generic_item", fake_is_generic)
    monkeypatch.setattr(compare_mod, "docs_at", fake_docs_at)
    monkeypatch.setattr(compare_mod, "semantic_compare", fake_semantic_compare)
    monkeypatch.setattr(compare_mod, "generic_compare", fail_generic_compare)

    res = _j(tools["plm_compare"](str(tmp_path), a["commit"], b["commit"], iid))
    assert res["ok"] is True
    assert res["doc_diff"] == {"nets_added": ["n1"]}
    assert res["summary"] == "added net n1"
    assert "mode" not in res


def test_tool_error_hints_no_git_vocab(tmp_path, tools):
    # a not-found restore returns a plain-language hint (contract §8.1)
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    tools["plm_open"](str(tmp_path))
    res = _j(tools["plm_restore"](str(tmp_path), "0" * 40))
    assert res["ok"] is False
    words = set(res["hint"].lower().replace(".", " ").split())
    assert not (words & _GIT_VOCAB), f"git vocab in hint: {res['hint']!r}"


def test_end_to_end_via_real_fastmcp_dispatch(tmp_path, monkeypatch):
    """DoD 'plm_* exercised end-to-end via MCP client': dispatch through the real
    FastMCP.call_tool path, not the capture shim — open → checkpoint → history."""
    import asyncio

    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    mcp = FastMCP("plm-e2e")
    register_plm_tools(mcp)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))

    async def _run():
        _content, opened = await mcp.call_tool("plm_open", {"path": str(tmp_path)})
        opened = json.loads(opened["result"]) if isinstance(opened.get("result"), str) else opened["result"]
        assert opened["ok"] is True
        _content, cp = await mcp.call_tool(
            "plm_checkpoint", {"message": "e2e checkpoint"})
        cp = json.loads(cp["result"]) if isinstance(cp.get("result"), str) else cp["result"]
        assert cp["ok"] is True
        _content, h = await mcp.call_tool("plm_history", {"path": str(tmp_path)})
        h = json.loads(h["result"]) if isinstance(h.get("result"), str) else h["result"]
        assert h["ok"] is True
        assert any(e["message"] == "e2e checkpoint" for e in h["entries"])

    asyncio.run(_run())


def test_plm_plan_lifecycle_confirms_changes(tmp_path, tools):
    """create/update/delete on plm_plan echoes a count and, for delete, the
    removed id — not just a bare {ok:true}."""
    (tmp_path / "Bracket.step+").write_bytes(_container(_part()))
    tools["plm_open"](str(tmp_path))

    created = _j(tools["plm_plan"](
        str(tmp_path), "create", "task", {"title": "Chamfer the edge"}, ""))
    assert created["ok"] is True
    assert created["entity"]["title"] == "Chamfer the edge"
    assert created["count"] == 1
    task_id = created["entity"]["id"]

    updated = _j(tools["plm_plan"](
        str(tmp_path), "update", "task", {"status": "done"}, task_id))
    assert updated["ok"] is True
    assert updated["entity"]["status"] == "done"
    assert updated["count"] == 1

    deleted = _j(tools["plm_plan"](str(tmp_path), "delete", "task", None, task_id))
    assert deleted["ok"] is True
    assert deleted["deleted_id"] == task_id
    assert deleted["count"] == 0
    assert deleted["items"] == []


def test_plm_lock_unlock_via_tool_confirms_scope(tmp_path, tools):
    """plm_unlock must echo which item/remote it released, not a bare
    {ok:true} indistinguishable from every other call."""
    from services.plm import remote as remote_mod

    src = tmp_path / "alice"
    src.mkdir()
    (src / "Bracket.step+").write_bytes(_container(_part()))
    tools["plm_open"](str(src))
    item_id = _j(tools["plm_status"](str(src)))["items"][0]["item_id"]

    bare = tmp_path / "central.git"
    assert remote_mod.seed_remote(src, bare)["ok"] is True

    lock_res = _j(tools["plm_lock"](str(src), item_id))
    assert lock_res["ok"] is True
    assert lock_res["lock"]["item_id"] == item_id

    unlock_res = _j(tools["plm_unlock"](str(src), item_id))
    assert unlock_res["ok"] is True
    assert unlock_res["item_id"] == item_id
    assert unlock_res["remote_id"] == "origin"

    # lock is actually released: someone else can now acquire it
    import os

    os.environ["CHUDWARE_USER_ID"] = "bob"
    os.environ["CHUDWARE_USER_DISPLAY"] = "Bob"
    try:
        relock = _j(tools["plm_lock"](str(src), item_id))
    finally:
        os.environ.pop("CHUDWARE_USER_ID", None)
        os.environ.pop("CHUDWARE_USER_DISPLAY", None)
    assert relock["ok"] is True
    assert relock["lock"]["holder"] == "bob"


def test_remote_identity_and_force_parameters_reach_mcp_backend(
    tools, monkeypatch
):
    from services.plm import remote

    seen = {}

    def fake_add(project_dir, name, **kwargs):
        seen["add"] = (project_dir, name, kwargs)
        return {"ok": True, "remote": {"id": "r1"}}

    def fake_lock(project_dir, item_id, **kwargs):
        seen["lock"] = (project_dir, item_id, kwargs)
        return {
            "ok": True,
            "lock": {"item_id": item_id},
            "overrode": {"user_id": "alice"},
        }

    monkeypatch.setattr(remote, "add_remote", fake_add)
    monkeypatch.setattr(remote, "plm_lock", fake_lock)

    added = _j(tools["plm_add_remote"](
        "/project",
        "https",
        "team",
        "https://example.invalid/team",
        "Sarah",
        "acct-42",
    ))
    assert added["ok"]
    assert seen["add"] == (
        "/project",
        "team",
        {
            "path": "https://example.invalid/team",
            "kind": "https",
            "url": "https://example.invalid/team",
            "display_name": "Sarah",
            "account_id": "acct-42",
        },
    )

    locked = _j(tools["plm_lock"](
        "/project", "i123", "r123", True
    ))
    assert locked["ok"]
    assert locked["overrode"]["user_id"] == "alice"
    assert seen["lock"] == (
        "/project",
        "i123",
        {"remote": "r123", "force": True},
    )
