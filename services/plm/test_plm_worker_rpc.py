"""Tests for the PLM worker-RPC seam (services/plm/worker_rpc.py).

Covers the in-editor GUI surface:
  * every worker method returns the contract §0 dict envelope (never raises)
  * plm_history thumbnail export (thumb_dir -> thumbnail_file per entry)
  * plm_status scheme extras (part_number_prefix + next_part_number)
  * plm_scheme_set (configurable part-number scheme, suggestions only)
  * plm_item_set start_new_rev + plm_discard_draft round trip (auto-branch
    silently -> "back to rev A (discard draft)" -> Released again, forward-only)
  * plm_restore round trip through the worker seam
  * plm_draft: local-model tier via injected hook (validated: git vocabulary
    and invented numerals are rejected), manual fallback when no local model
  * no git vocabulary in any user-facing string
  * graceful degradation on a non-project path

Runs under the gate venv (dulwich installed). No engine C++ dependency, no
live model server — the local tier is exercised through the test hook.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from services.plm import worker_rpc

_GIT_VOCAB = {
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
}


def _emit(_data: dict) -> None:  # worker emit callback stub
    pass


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": "uuid-part-1", "name": "Bracket",
            "features": [{"id": "f1", "params": {"wall": wall}}]}


def _write(project_dir, name, doc):
    (project_dir / name).write_bytes(_container(doc))


def _assert_no_git_vocab(text: str) -> None:
    words = set(text.lower().replace(".", " ").replace(",", " ").split())
    leaked = words & _GIT_VOCAB
    assert not leaked, f"git vocabulary leaked: {leaked} in {text!r}"


@pytest.fixture()
def project(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    r = worker_rpc.WORKER_METHODS["plm_open"]({"path": str(tmp_path)}, _emit)
    assert r["ok"] is True
    return tmp_path


def _call(method: str, **params):
    return worker_rpc.WORKER_METHODS[method](params, _emit)


# ---------------------------------------------------------------------------
# envelopes + degradation
# ---------------------------------------------------------------------------

def test_all_methods_envelope_on_missing_path(tmp_path, monkeypatch):
    from services import paths

    # Never inherit the workstation's real Box credentials in this missing-path
    # contract test.
    monkeypatch.setattr(paths, "CONFIG", tmp_path / "empty-config.json")
    # AI-VC methods are doc_id-scoped (not project-path vault); empty params are
    # valid for plm_timeline (empty list) and invalid for plm_rollback (needs `to`).
    _AI_VC = {"plm_timeline", "plm_rollback"}
    for name, fn in worker_rpc.WORKER_METHODS.items():
        out = fn({}, _emit)
        assert isinstance(out, dict), name
        if name == "plm_timeline":
            assert out.get("ok") is True and "checkpoints" in out, name
            continue
        if name == "plm_rollback":
            assert out.get("ok") is False and "reason" in out, name
            _assert_no_git_vocab(str(out.get("hint", "")))
            continue
        if name == "box_status":
            # box_mirror.status is a path-optional Settings query: an empty
            # configuration is a successful disabled/unconfigured status.
            assert out["ok"] is True
            assert out["enabled"] is False
            assert out["configured"] is False
            continue
        if name == "box_notify_save":
            # The save notification is deliberately best-effort/no-op without
            # a path or enabled mirror (box_mirror.notify_saved_path contract).
            assert out == {"ok": True}
            continue
        if name == "plm_live_leave":
            # Leave is intentionally idempotent. With no joined session (and
            # therefore no project path), it is a successful no-op.
            assert out == {"ok": True, "left": True}
            continue
        if name in ("cad_graph_get", "cad_graph_assembly_tree"):
            # cad_graph_get {project_dir?|doc_id?} — both optional per
            # contracts/cad_graph.md: empty params are the open-docs view (ok True);
            # cad_graph_assembly_tree degrades to an empty tree list the same way.
            assert out.get("ok") is True, name
            continue
        assert out.get("ok") is False, name
        assert "reason" in out, name
        _assert_no_git_vocab(str(out.get("hint", "")))


def test_open_zero_config(project):
    out = _call("plm_open", path=str(project))
    assert out["ok"] and out["project_id"]
    assert out["manifest"]["items"][0]["path"] == "Bracket.step+"


def test_status_carries_scheme_extras(project):
    st = _call("plm_status", path=str(project))
    assert st["ok"]
    assert st["part_number_prefix"]
    assert st["next_part_number"].startswith(st["part_number_prefix"] + "-")
    assert st["items"][0]["lifecycle"] == "In work"


# ---------------------------------------------------------------------------
# history + thumbnails
# ---------------------------------------------------------------------------

def test_history_exports_thumbnails(project, tmp_path_factory):
    _write(project, "Bracket.step+", _part(4.0))
    assert _call("plm_save", path=str(project))["ok"]
    _write(project, "Bracket.step+", _part(5.0))
    assert _call("plm_checkpoint", path=str(project), message="wall to 5")["ok"]

    td = tmp_path_factory.mktemp("thumbs")
    out = _call("plm_history", path=str(project), thumb_dir=str(td))
    assert out["ok"] and len(out["entries"]) >= 3
    kinds = [e["kind"] for e in out["entries"]]
    assert "checkpoint" in kinds and "auto" in kinds
    for e in out["entries"]:
        assert e.get("thumbnail_file"), e
        assert (td / f'{e["commit"]}.png').exists()
        for field in ("message", "summary"):
            _assert_no_git_vocab(str(e.get(field) or ""))


def test_history_selects_immutable_commit(project, tmp_path):
    initial = _call("plm_history", path=str(project))["head"]
    _write(project, "Bracket.step+", _part(4.0))
    assert _call("plm_save", path=str(project))["ok"]
    latest = _call("plm_history", path=str(project))["head"]
    assert initial != latest
    output = _call("plm_history", path=str(project), commit=initial, thumb_dir=str(tmp_path))
    assert output["ok"]
    assert output["head"] == latest
    assert [entry["commit"] for entry in output["entries"]] == [initial]
    assert (tmp_path / f"{initial}.png").exists()
    assert not (tmp_path / f"{latest}.png").exists()

def test_history_and_outside_scan_do_not_query_remote_locks(project, monkeypatch):
    from services.plm import remote
    from services.plm.vault import Vault

    v = Vault.open(project)
    try:
        m = v.manifest()
        m["remotes"] = [{"id": "offline", "url": "/offline"}]
        from services.plm import manifest
        v._commit_sibling("manifest.json", manifest.dumps(m), scope="design")
    finally:
        v.close()
    queries = []

    def offline_locks(*args, **kwargs):
        queries.append(True)
        return {"ok": False, "reason": "offline"}

    monkeypatch.setattr(remote, "list_locks", offline_locks)
    history = _call("plm_history", path=str(project))
    assert history["ok"] and history["entries"][0]["commit"] == history["head"]
    outside = _call("plm_record_outside", path=str(project))
    assert outside == {"ok": True, "committed": False}
    assert queries == []
    assert _call("plm_status", path=str(project))["ok"]
    assert queries == [True]  # Explicit status still asks for fresh remote state.


def test_save_rejects_release_kind(project):
    out = _call("plm_save", path=str(project), kind="release")
    assert out["ok"] is False and out["reason"] == "invalid_mode"


# ---------------------------------------------------------------------------
# restore round trip
# ---------------------------------------------------------------------------

def test_restore_round_trip(project):
    st0 = _call("plm_status", path=str(project))
    base = st0["head"]
    _write(project, "Bracket.step+", _part(9.0))
    saved = _call("plm_save", path=str(project))
    assert saved["ok"]
    out = _call("plm_restore", path=str(project), commit=base)
    assert out["ok"] and out["entry"]["kind"] == "restore"
    # the working file is back to wall=3, and history GREW (forward-only)
    hist = _call("plm_history", path=str(project))["entries"]
    assert hist[0]["kind"] == "restore"
    assert len(hist) >= 3
    with zipfile.ZipFile(io.BytesIO((project / "Bracket.step+").read_bytes())) as zf:
        doc = json.loads(zf.read("document.json"))
    assert doc["features"][0]["params"]["wall"] == 3.0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

def test_compare_semantic(project):
    st0 = _call("plm_status", path=str(project))
    item_id = st0["items"][0]["item_id"]
    a = st0["head"]
    _write(project, "Bracket.step+", _part(7.5))
    b = _call("plm_save", path=str(project))["commit"]
    out = _call("plm_compare", path=str(project), commit_a=a, commit_b=b,
                item_id=item_id)
    assert out["ok"]
    assert "7.5" in json.dumps(out["doc_diff"]) or "7.5" in out["summary"]
    _assert_no_git_vocab(out["summary"])


# ---------------------------------------------------------------------------
# release -> auto-branch draft -> discard (the toast round trip)
# ---------------------------------------------------------------------------

def test_release_then_start_rev_then_discard(project):
    st0 = _call("plm_status", path=str(project))
    item_id = st0["items"][0]["item_id"]
    rel = _call("plm_release", path=str(project), item_id=item_id,
                message="first release", confirm=True)
    assert rel["ok"] and rel["rev"] == "A"

    # the silent auto-branch: first edit of a Released doc -> start_new_rev
    out = _call("plm_item_set", path=str(project), item_id=item_id,
                start_new_rev=True)
    assert out["ok"] and out["working_rev"] == "B"
    assert out["item"]["lifecycle"] == "In work"

    # edit the working file (the draft)
    _write(project, "Bracket.step+", _part(11.0))
    assert _call("plm_save", path=str(project))["ok"]

    # "back to rev A (discard draft)"
    disc = _call("plm_discard_draft", path=str(project), item_id=item_id)
    assert disc["ok"]
    assert disc["item"]["lifecycle"] == "Released"
    assert disc["item"]["rev"] == "A"
    with zipfile.ZipFile(io.BytesIO((project / "Bracket.step+").read_bytes())) as zf:
        doc = json.loads(zf.read("document.json"))
    assert doc["features"][0]["params"]["wall"] == 3.0
    # forward-only: the discard appended entries, nothing was erased
    hist = _call("plm_history", path=str(project))["entries"]
    assert hist[0]["kind"] == "auto"          # lifecycle flip back to Released
    assert any(e["kind"] == "restore" for e in hist)
    assert any(e["kind"] == "release" for e in hist)


def test_discard_without_release_is_refused(project):
    st0 = _call("plm_status", path=str(project))
    item_id = st0["items"][0]["item_id"]
    out = _call("plm_discard_draft", path=str(project), item_id=item_id)
    assert out["ok"] is False and out["reason"] == "not_found"
    _assert_no_git_vocab(out["hint"])


# ---------------------------------------------------------------------------
# part-number scheme
# ---------------------------------------------------------------------------

def test_scheme_set_changes_suggestions_not_items(project):
    st0 = _call("plm_status", path=str(project))
    old_number = st0["items"][0]["part_number"]
    out = _call("plm_scheme_set", path=str(project), prefix="prj")
    assert out["ok"]
    assert out["part_number_prefix"] == "PRJ"
    assert out["next_part_number"].startswith("PRJ-")
    st1 = _call("plm_status", path=str(project))
    assert st1["items"][0]["part_number"] == old_number  # never rewritten
    assert st1["next_part_number"].startswith("PRJ-")


def test_scheme_set_rejects_empty(project):
    out = _call("plm_scheme_set", path=str(project), prefix="  !!")
    assert out["ok"] is False and out["reason"] == "invalid_mode"


def test_item_set_part_number_editable(project):
    st0 = _call("plm_status", path=str(project))
    item_id = st0["items"][0]["item_id"]
    out = _call("plm_item_set", path=str(project), item_id=item_id,
                part_number="CUSTOM-42")
    assert out["ok"] and out["item"]["part_number"] == "CUSTOM-42"


# ---------------------------------------------------------------------------
# plm_draft — local tier + manual fallback
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_hook():
    yield
    worker_rpc.set_local_draft_hook(None)


def test_draft_manual_fallback_without_local_model(project, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PLM_DRAFT_LOCAL", "0")  # simulate: not provisioned
    _write(project, "Bracket.step+", _part(6.0))
    out = _call("plm_draft", path=str(project))
    assert out["ok"]
    assert out["source"] == "manual"
    assert out["message"] == ""          # plain manual box in the GUI
    assert out["summary"]                # change-summary context still shown
    _assert_no_git_vocab(out["summary"])


def test_draft_local_model_phrases_from_summary(project):
    _write(project, "Bracket.step+", _part(6.0))
    worker_rpc.set_local_draft_hook(
        lambda summary: "Increased the wall to 6 for stiffness")
    out = _call("plm_draft", path=str(project))
    assert out["ok"]
    assert out["source"] == "local_model"
    assert out["message"] == "Increased the wall to 6 for stiffness"
    _assert_no_git_vocab(out["message"])


def test_draft_rejects_git_vocab_from_model(project):
    _write(project, "Bracket.step+", _part(6.0))
    worker_rpc.set_local_draft_hook(lambda s: "Commit the new wall of 6.0")
    out = _call("plm_draft", path=str(project))
    assert out["source"] == "manual" and out["message"] == ""


def test_draft_rejects_invented_numerals(project):
    _write(project, "Bracket.step+", _part(6.0))
    worker_rpc.set_local_draft_hook(lambda s: "Changed the wall to 6.2")
    out = _call("plm_draft", path=str(project))
    assert out["source"] == "manual" and out["message"] == ""


def test_draft_no_changes_is_empty(project):
    out = _call("plm_draft", path=str(project))
    assert out["ok"] and out["summary"] == "" and out["source"] == "manual"


def test_draft_release_mode(project):
    st0 = _call("plm_status", path=str(project))
    item_id = st0["items"][0]["item_id"]
    _write(project, "Bracket.step+", _part(6.0))
    # a never-released item's release note is "First version of Bracket." —
    # numeral-free, so the phrased draft must stay numeral-free too
    worker_rpc.set_local_draft_hook(
        lambda s: "First version of the bracket, ready to ship")
    out = _call("plm_draft", path=str(project), mode="release", item_id=item_id)
    assert out["ok"] and out["source"] == "local_model"


def test_draft_invalid_mode(project):
    out = _call("plm_draft", path=str(project), mode="banana")
    assert out["ok"] is False and out["reason"] == "invalid_mode"


# ---------------------------------------------------------------------------
# 2C — auto_commit setting, plm_record_outside, packing
# ---------------------------------------------------------------------------


def test_auto_commit_roundtrip(project):
    got = worker_rpc.WORKER_METHODS["plm_settings_get"](
        {"path": str(project)}, _emit)
    assert got["ok"] and got["auto_commit"] is True  # default on
    setr = worker_rpc.WORKER_METHODS["plm_settings_set"](
        {"path": str(project), "auto_commit": False}, _emit)
    assert setr["ok"] and setr["auto_commit"] is False
    again = worker_rpc.WORKER_METHODS["plm_settings_get"](
        {"path": str(project)}, _emit)
    assert again["auto_commit"] is False


def test_record_outside_noop_when_clean(project):
    out = worker_rpc.WORKER_METHODS["plm_record_outside"](
        {"path": str(project)}, _emit)
    assert out["ok"] is True and out["committed"] is False


def test_record_outside_scan_failure_is_not_a_clean_noop(project, monkeypatch):
    from services.plm.vault import Vault

    def fail_scan(_vault):
        raise OSError("synthetic directory scan failure")

    def must_not_save(_vault, *_args, **_kwargs):
        raise AssertionError("save must not run after a failed scan")

    monkeypatch.setattr(Vault, "_project_containers", fail_scan)
    monkeypatch.setattr(Vault, "save", must_not_save)

    out = worker_rpc.WORKER_METHODS["plm_record_outside"](
        {"path": str(project)}, _emit)
    assert out["ok"] is False
    assert out["reason"] == "scan_failed"
    assert "committed" not in out


def test_record_outside_commits_disk_change(project):
    _write(project, "Bracket.step+", _part(wall=9.0))  # external re-save
    out = worker_rpc.WORKER_METHODS["plm_record_outside"](
        {"path": str(project)}, _emit)
    assert out["ok"] is True and out["committed"] is True
    assert out["commit"]
    # a second call with no further change no-ops
    out2 = worker_rpc.WORKER_METHODS["plm_record_outside"](
        {"path": str(project)}, _emit)
    assert out2["committed"] is False


def test_record_outside_tracks_new_container(project):
    _write(project, "Plate.step+",
           {"schema": "aicad.part/1", "id": "uuid-part-2", "name": "Plate",
            "features": []})
    out = worker_rpc.WORKER_METHODS["plm_record_outside"](
        {"path": str(project)}, _emit)
    assert out["committed"] is True
    st = worker_rpc.WORKER_METHODS["plm_status"]({"path": str(project)}, _emit)
    assert any(it["path"] == "Plate.step+" for it in st["items"])


def test_repack_after_many_saves(project):
    for i in range(30):
        _write(project, "Bracket.step+", _part(wall=float(i)))
        r = worker_rpc.WORKER_METHODS["plm_save"]({"path": str(project)}, _emit)
        assert r["ok"], r
    packs = list((project / ".aicadvault").rglob("*.pack"))
    assert packs, "expected a packfile after >= 25 auto-commits"


def test_remote_identity_and_force_parameters_reach_worker_backend(monkeypatch):
    from services.plm import remote

    seen = {}

    def fake_add(project_dir, name, **kwargs):
        seen["add"] = (project_dir, name, kwargs)
        return {"ok": True, "remote": {"id": "r1"}}

    def fake_lock(project_dir, item_id, **kwargs):
        seen["lock"] = (project_dir, item_id, kwargs)
        return {"ok": True, "lock": {"item_id": item_id}}

    monkeypatch.setattr(remote, "add_remote", fake_add)
    monkeypatch.setattr(remote, "plm_lock", fake_lock)

    added = _call(
        "plm_add_remote",
        path="/project",
        kind="https",
        name="team",
        url="https://example.invalid/team",
        display_name="Sarah",
        account_id="acct-42",
    )
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

    locked = _call(
        "plm_lock",
        path="/project",
        item_id="i123",
        remote_id="r123",
        force=True,
    )
    assert locked["ok"]
    assert seen["lock"] == (
        "/project",
        "i123",
        {"remote": "r123", "force": True},
    )
