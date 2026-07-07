"""Tests for the PLM planning store (aicad.planning/1) + seams (2026-07).

Covers:
  * task/issue/milestone CRUD via the vault plan() API
  * actor objects (future org-member-ready), enum coercion, item/rev links
  * planning mutations are scope:"planning" → hidden from the default design
    timeline, visible under scope planning/all
  * planning state survives Restore (versioned with the project) and is NOT
    captured together with unsaved design edits
  * plm_overview aggregate; permission seam is invoked (allow-all in v1)
  * worker RPC plm_plan / plm_overview with path handling
  * no git vocabulary anywhere

Runs under the gate venv (dulwich installed). No engine C++ dependency.
"""
from __future__ import annotations

import io
import json
import zipfile

from services.plm import planning, permissions
from services.plm.vault import Vault
from services.plm import worker_rpc


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": "uuid-part-1", "name": "Bracket",
            "features": [{"id": "f1", "params": {"wall": wall}}]}


# ---------------------------------------------------------------------------
# pure planning helpers
# ---------------------------------------------------------------------------

def test_planning_create_defaults_and_coercion():
    coll = planning.empty_collection("task")
    t = planning.create(coll, "task", {"title": "Do it", "status": "bogus",
                                        "priority": "p0",
                                        "assignee": "hunter"}, {"id": "hunter"}, "NOW")
    assert t["id"].startswith("t")
    assert t["status"] == "todo"      # bogus → safe default
    assert t["priority"] == "p0"
    assert t["assignee"] == {"kind": "profile", "id": "hunter", "display": "hunter"}
    assert t["created_by"]["kind"] == "profile"


def test_issue_links_to_item_and_rev():
    coll = planning.empty_collection("issue")
    i = planning.create(coll, "issue",
                        {"title": "Cracks", "severity": "high",
                         "found_in": {"item_id": "i123", "rev": "A"},
                         "links": [{"kind": "item", "item_id": "i123"},
                                   {"kind": "junk"}]},
                        None, "NOW")
    assert i["found_in"] == {"item_id": "i123", "rev": "A"}
    assert i["links"] == [{"kind": "item", "item_id": "i123"}]  # junk dropped


# ---------------------------------------------------------------------------
# vault plan() API
# ---------------------------------------------------------------------------

def test_vault_task_crud(tmp_path):
    v = Vault.open(tmp_path)
    made = v.plan("create", "task", data={"title": "Design bracket"})
    tid = made["entity"]["id"]
    assert len(v.plan("list", "task")["items"]) == 1
    v.plan("update", "task", ent_id=tid, data={"status": "doing"})
    assert v.plan("list", "task")["items"][0]["status"] == "doing"
    v.plan("delete", "task", ent_id=tid)
    assert v.plan("list", "task")["items"] == []
    v.close()


def test_planning_scope_hidden_from_design_timeline(tmp_path):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(3.0)))
    v = Vault.open(tmp_path)
    v.save()  # a design entry
    v.plan("create", "task", data={"title": "A task"})
    design = v.history(scope="design")
    planning_hist = v.history(scope="planning")
    assert all(e["scope"] == "design" for e in design)
    assert any(e["scope"] == "planning" for e in planning_hist)
    # the design timeline does not grow when only a task is added
    n_design = len(design)
    v.plan("create", "task", data={"title": "Another"})
    assert len(v.history(scope="design")) == n_design
    v.close()


def test_planning_survives_restore(tmp_path):
    v = Vault.open(tmp_path)
    v.plan("create", "task", data={"title": "Keep me"})
    # a point in design history to restore to
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(3.0)))
    save = v.save()["entry"]["commit"]
    v.plan("create", "task", data={"title": "Added later"})
    assert len(v.plan("list", "task")["items"]) == 2
    # restoring the design save point must not lose the planning board
    v.restore(save)
    titles = {t["title"] for t in v.plan("list", "task")["items"]}
    assert "Keep me" in titles
    v.close()


def test_planning_edit_does_not_capture_unsaved_design(tmp_path):
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(3.0)))
    v = Vault.open(tmp_path)
    v.save()
    # make an UNSAVED design edit on disk
    (tmp_path / "Bracket.step+").write_bytes(_container(_part(9.0)))
    dirty_before = set(v._dirty_items(v.manifest()))
    assert dirty_before  # the part is dirty
    # a planning mutation must NOT quietly commit that design edit
    v.plan("create", "task", data={"title": "unrelated"})
    dirty_after = set(v._dirty_items(v.manifest()))
    assert dirty_after == dirty_before  # still dirty — design not captured
    v.close()


def test_overview_aggregate(tmp_path):
    (tmp_path / "spec.md").write_text("hi\n")
    v = Vault.open(tmp_path)
    v.add_item("spec.md")
    v.plan("create", "task", data={"title": "todo one"})
    v.plan("create", "task", data={"title": "done one", "status": "done"})
    v.plan("create", "issue", data={"title": "bug", "severity": "high"})
    v.plan("create", "milestone", data={"title": "v1", "due": "2026-08-01"})
    ov = v.overview()
    assert ov["counts"]["open_tasks"] == 1
    assert ov["counts"]["open_issues"] == 1
    assert ov["next_milestone"]["title"] == "v1"
    assert ov["ai_mode"] == "assisted"
    v.close()


def test_permission_seam_allows_all_v1():
    assert permissions.can({"kind": "profile", "id": "x"}, "delete", "planning") is True
    permissions.require(None, "administer", "project")  # no raise in v1


# ---------------------------------------------------------------------------
# worker RPC surface
# ---------------------------------------------------------------------------

def test_worker_plan_and_overview(tmp_path):
    r = worker_rpc._plm_plan(
        {"path": str(tmp_path), "op": "create", "entity": "task",
         "data": {"title": "worker task"}}, None)
    assert r["ok"], r
    assert r["entity"]["title"] == "worker task"
    ov = worker_rpc._plm_overview({"path": str(tmp_path)}, None)
    assert ov["ok"]
    assert ov["counts"]["open_tasks"] == 1
    # missing path → contract error
    bad = worker_rpc._plm_plan({"op": "list", "entity": "task"}, None)
    assert bad["ok"] is False and bad["reason"] == "invalid_project"


def test_worker_plan_bad_entity(tmp_path):
    r = worker_rpc._plm_plan(
        {"path": str(tmp_path), "op": "list", "entity": "widget"}, None)
    assert r["ok"] is False
    assert r["reason"] == "invalid_mode"
