"""Tests for PLM change intelligence (contract aicad.plm/1 §7/§8).

Covers:
  * every history entry gets a deterministic plain-language summary; numerals
    provably copied from doc_diff output (numeral-fidelity property)
  * checkpoint/release messages drafted deterministically via plm machinery;
    user-editable; manual path works with AI fully disabled
  * AI-managed tier: the copilot (PLM bridge) cuts an attributed checkpoint at
    end-of-task in a scripted run; timeline marks it AI; one-click discard
  * plm_compare semantic + visual orchestration (visual degrades gracefully)
  * tier persisted per project; default assisted; manual-only floor verified
  * no git vocabulary in any produced user-facing string
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from services.mcp.doc_diff_tools import doc_diff
from services.plm import assist, compare, manifest, summarize
from services.plm.client import PLMVaultClient
from services.plm.vault import Vault, VaultError

# ---------------------------------------------------------------------------
# fixtures / helpers (mirrors test_plm_vault.py)
# ---------------------------------------------------------------------------

_GIT_VOCAB = {
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
}


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(wall: float = 3.0, extra_features: list | None = None) -> dict:
    feats = [{"id": "f1", "name": "Base", "type": "box",
              "params": {"wall": wall, "width": 40.0}}]
    feats += extra_features or []
    return {"schema": "aicad.part/1", "id": "uuid-part-1", "name": "Bracket",
            "features": feats}


def _write(project_dir, name, doc):
    (project_dir / name).write_bytes(_container(doc))


def _assert_no_git_vocab(text: str):
    words = set(text.lower().replace(".", " ").replace(",", " ").replace(";", " ").split())
    leaked = words & _GIT_VOCAB
    assert not leaked, f"git vocabulary leaked: {leaked} in {text!r}"


def _newest_summary(v: Vault, iid: str) -> str:
    from services.plm import snapshot

    head = v._bind.head_commit()
    raw = v._bind.read_blob_at(head, snapshot.item_prefix(iid) + "summary.txt")
    return (raw or b"").decode("utf-8")


# ---------------------------------------------------------------------------
# summary determinism + numeral fidelity
# ---------------------------------------------------------------------------

def test_summary_is_deterministic():
    a, b = _part(3.0), _part(5.0)
    diff = doc_diff(a, b)
    s1 = summarize.summarize_diff(diff, a, b)
    s2 = summarize.summarize_diff(json.loads(json.dumps(diff)), a, b)
    assert s1 == s2
    assert s1  # non-empty for a real change


@pytest.mark.parametrize("doc_b", [
    _part(5.0),
    _part(3.0, extra_features=[{"id": "f2", "name": "Rib", "params": {"t": 2.5}}]),
    {**_part(3.0), "features": []},  # everything removed
    _part(0.125),
    _part(3.0, extra_features=[{"id": "f9", "name": "M6 boss", "params": {"d": 6.0}}]),
])
def test_numeral_fidelity(doc_b):
    """DoD: no numeral in the summary absent from the diff output."""
    a = _part(3.0)
    diff = doc_diff(a, doc_b)
    s = summarize.summarize_diff(diff, a, doc_b)
    assert summarize.numeral_tokens(s) <= summarize.diff_numerals(diff), \
        f"summary invented numerals: {s!r}"
    _assert_no_git_vocab(s)


def test_digit_bearing_labels_fall_back_to_feature_id():
    # a feature NAME with digits ("M6 boss") must not leak numerals not in diff
    a = _part(3.0)
    b = _part(3.0, extra_features=[{"id": "f9", "name": "M6 boss", "params": {}}])
    diff = doc_diff(a, b)
    s = summarize.summarize_diff(diff, a, b)
    assert "M6" not in s
    assert "f9" in s  # the id (present in the diff) is used instead


def test_summary_covers_mates_suppression_sketch():
    a = {"schema": "aicad.asm/1", "id": "u", "name": "Asm",
         "features": [{"id": "s1", "type": "sketch",
                       "params": {"entities": [{"id": "e1"}], "constraints": []}},
                      {"id": "f2", "name": "Boss", "params": {}, "suppressed": False}],
         "mates": [{"id": "m1", "kind": "coincident"}]}
    b = {"schema": "aicad.asm/1", "id": "u", "name": "Asm",
         "features": [{"id": "s1", "type": "sketch",
                       "params": {"entities": [{"id": "e1"}, {"id": "e2"}], "constraints": []}},
                      {"id": "f2", "name": "Boss", "params": {}, "suppressed": True}],
         "mates": [{"id": "m1", "kind": "concentric"}]}
    diff = doc_diff(a, b)
    s = summarize.summarize_diff(diff, a, b)
    assert "sketch" in s.lower()
    assert "Suppressed Boss" in s
    assert "mate m1" in s
    assert summarize.numeral_tokens(s) <= summarize.diff_numerals(diff)
    _assert_no_git_vocab(s)


def test_empty_diff_summary():
    d = doc_diff(_part(3.0), _part(3.0))
    assert summarize.summarize_diff(d) == "No design changes."


# ---------------------------------------------------------------------------
# every history entry carries a summary (vault integration)
# ---------------------------------------------------------------------------

def test_first_entry_and_edit_summaries(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        assert _newest_summary(v, iid) == "First version of Bracket."

        _write(tmp_path, "Bracket.step+", _part(5.0))
        v.save("auto")
        s = _newest_summary(v, iid)
        assert "wall" in s and "3" in s and "5" in s
        _assert_no_git_vocab(s)

        # unchanged save → explicit "no changes" summary
        v.save("auto")
        assert _newest_summary(v, iid) == "No design changes."

        # The summary is surfaced on timeline entries (plm_history shape). Note
        # the no-op save above is NOT in this item's timeline: a commit belongs
        # there only when it CHANGED the item, so the newest entry is still the
        # wall edit. (This assertion used to expect "No design changes." here,
        # which pinned the old per-item-history bug — it credited a change to
        # the newest commit merely CARRYING that content rather than the one
        # that introduced it, so an unrelated save shadowed the real edit.)
        entries = v.history(iid)
        s0 = entries[0]["summary"]
        assert "wall" in s0 and "3" in s0 and "5" in s0, s0
        assert len(entries) == 2, [e["summary"] for e in entries]
        assert entries[1]["summary"] == "First version of Bracket."
    finally:
        v.close()


# ---------------------------------------------------------------------------
# drafting: checkpoint + release messages per tier
# ---------------------------------------------------------------------------

def test_checkpoint_draft_in_assisted_mode(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        assert v.ai_mode() == "assisted"  # default tier (contract §4)
        _write(tmp_path, "Bracket.step+", _part(5.0))
        res = v.checkpoint("")  # omitted message → deterministic draft
        msg = res["entry"]["message"]
        assert msg, "assisted tier should draft a message"
        assert "3" in msg and "5" in msg
        diff = doc_diff(_part(3.0), _part(5.0))
        assert summarize.numeral_tokens(msg) <= summarize.diff_numerals(diff)
        _assert_no_git_vocab(msg)
    finally:
        v.close()


def test_checkpoint_user_message_wins(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        _write(tmp_path, "Bracket.step+", _part(5.0))
        res = v.checkpoint("my own words")  # user-editable: explicit text wins
        assert res["entry"]["message"] == "my own words"
    finally:
        v.close()


def test_checkpoint_manual_mode_never_drafts(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        v.set_ai_mode("manual")
        _write(tmp_path, "Bracket.step+", _part(5.0))
        res = v.checkpoint("")
        assert res["entry"]["message"] == ""  # manual floor: no assist behavior
        # manual buttons still fully work (checkpoint was cut)
        assert res["entry"]["kind"] == "checkpoint"
    finally:
        v.close()


def test_release_note_drafted_in_assisted_mode(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        rel = v.release(iid, "")  # assisted: note drafted from changes
        assert rel["rev"] == "A"
        assert rel["entry"]["message"]  # drafted, non-empty
        _assert_no_git_vocab(rel["entry"]["message"])
    finally:
        v.close()


def test_release_manual_mode_still_requires_note(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        v.set_ai_mode("manual")
        iid = v.status()["items"][0]["item_id"]
        with pytest.raises(VaultError) as e:
            v.release(iid, "")
        assert e.value.reason == "preflight_failed"
        # explicit note works with AI fully disabled
        rel = v.release(iid, "First release")
        assert rel["rev"] == "A"
    finally:
        v.close()


# ---------------------------------------------------------------------------
# tier selection: persisted per project, default assisted, gating
# ---------------------------------------------------------------------------

def test_tier_persists_across_reopen(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    v.set_ai_mode("managed")
    v.close()
    v2 = Vault.open(tmp_path)
    try:
        assert v2.ai_mode() == "managed"
        assert v2.status()["ai_mode"] == "managed"
    finally:
        v2.close()


def test_invalid_tier_rejected(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        with pytest.raises(VaultError) as e:
            v.set_ai_mode("autopilot")
        assert e.value.reason == "invalid_mode"
        _assert_no_git_vocab(e.value.hint)
    finally:
        v.close()


def test_allowed_matrix():
    assert not assist.allowed("manual", "draft_message")
    assert not assist.allowed("manual", "auto_checkpoint")
    assert assist.allowed("assisted", "draft_message")
    assert assist.allowed("assisted", "suggest_checkpoint")
    assert not assist.allowed("assisted", "auto_checkpoint")
    assert assist.allowed("managed", "auto_checkpoint")
    assert not assist.allowed(None, "draft_message")  # unknown → manual floor
    assert not assist.allowed("weird", "auto_checkpoint")


# ---------------------------------------------------------------------------
# checkpoint-worthiness heuristics (assisted+)
# ---------------------------------------------------------------------------

def test_suggest_checkpoint_fires_on_magnitude():
    diff = doc_diff(_part(3.0), _part(9.0))  # 200% swing
    out = assist.suggest_checkpoint(diff)
    assert out["suggest"] is True
    assert out["reasons"]
    for r in out["reasons"]:
        _assert_no_git_vocab(r)


def test_suggest_checkpoint_quiet_on_tiny_change():
    diff = doc_diff(_part(3.0), _part(3.1))
    assert assist.suggest_checkpoint(diff)["suggest"] is False


def test_suggest_checkpoint_time_and_ops_signals():
    diff = doc_diff(_part(3.0), _part(3.1))  # small but real change
    assert assist.suggest_checkpoint(diff, seconds_since_last=3600)["suggest"]
    assert assist.suggest_checkpoint(diff, ops_since_last=40)["suggest"]
    # no change at all → never suggest, however long it has been
    empty = doc_diff(_part(3.0), _part(3.0))
    assert not assist.suggest_checkpoint(empty, seconds_since_last=7200)["suggest"]


# ---------------------------------------------------------------------------
# stale-reference detection (capability-checked on release freeze data)
# ---------------------------------------------------------------------------

def _manifest_with_freeze(pinned_rev="A", current_rev="B", superseded=True):
    m = manifest.new_manifest("Gearbox", "2026-07-05T00:00:00Z")
    comp = manifest.register_item(m, doc_uuid="u1", kind="part", path="Bracket.step+")
    asm = manifest.register_item(m, doc_uuid="u2", kind="assembly", path="Main.step+")
    comp["rev"] = current_rev
    comp["lifecycle"] = manifest.LIFECYCLE_RELEASED
    comp["released_revs"] = [
        {"rev": pinned_rev, "commit": "c1",
         "lifecycle": (manifest.LIFECYCLE_SUPERSEDED if superseded
                       else manifest.LIFECYCLE_RELEASED)},
        {"rev": current_rev, "commit": "c2", "lifecycle": manifest.LIFECYCLE_RELEASED},
    ]
    m["bom_freezes"] = [{"assembly_item": asm["item_id"], "rev": "A", "commit": "c3",
                         "pins": [{"item_id": comp["item_id"], "rev": pinned_rev}]}]
    return m, asm, comp


def test_stale_refs_flags_superseded_pin():
    m, asm, comp = _manifest_with_freeze()
    flags = assist.stale_refs(m)
    assert len(flags) == 1
    f = flags[0]
    assert f["assembly_item"] == asm["item_id"]
    assert f["component_item"] == comp["item_id"]
    assert f["pinned_rev"] == "A" and f["current_rev"] == "B"
    _assert_no_git_vocab(f["hint"])


def test_stale_refs_quiet_when_pin_current():
    m, _, _ = _manifest_with_freeze(pinned_rev="B", current_rev="B")
    assert assist.stale_refs(m) == []


def test_stale_refs_capability_check_without_t_r4_data():
    m = manifest.new_manifest("Gearbox", "2026-07-05T00:00:00Z")
    manifest.register_item(m, doc_uuid="u1", kind="assembly", path="Main.step+")
    assert assist.stale_refs(m) == []  # no freeze data → no guessing


# ---------------------------------------------------------------------------
# AI-managed tier: scripted end-of-task run, attribution, one-click discard
# ---------------------------------------------------------------------------

def _managed_project(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    pid = v.status()["project_id"]
    v.set_ai_mode("managed")
    v.close()
    c = PLMVaultClient()
    c.register_project(pid, str(tmp_path))
    c.register_run("run_t3", pid)
    return pid, c


def test_managed_scripted_run_cuts_attributed_checkpoint(tmp_path):
    from services.agent.plm_bridge import PLMBridge

    pid, client = _managed_project(tmp_path)
    bridge = PLMBridge("run_t3", vault=client)
    link = bridge.on_milestone(trigger="mission_done", buildgraph_node="bg_9",
                               project_id=pid)
    assert link is not None and link["queued"] is False
    assert link["checkpoint_ref"]

    v = Vault.open(tmp_path)
    try:
        newest = v.history()[0]
        assert newest["kind"] == "checkpoint"
        assert newest["author"]["kind"] == "ai"          # timeline marks it AI
        assert newest["ai"]["run_id"] == "run_t3"        # telemetry join
        assert newest["ai"]["artifact_id"] == "bg_9"
        assert newest["ai"]["reversible"] is True
        _assert_no_git_vocab(newest["message"])

        # one-click discard: restore the state before the AI entry — forward-only
        before = {e["commit"] for e in v.history()}
        res = assist.discard_entry(v, newest["commit"])
        after = v.history()
        assert after[0]["kind"] == "restore"
        assert before <= {e["commit"] for e in after}    # nothing rewritten
        assert res["entry"]["kind"] == "restore"
    finally:
        v.close()


def test_managed_multi_part_checkpoint_attributes_each_item(tmp_path):
    """One boundary preserves each participant's work-unit join."""
    from services.agent.plm_bridge import PLMBridge

    part_a = _part(3.0)
    part_a.update({"id": "uuid-part-a", "name": "Bracket A"})
    part_b = _part(4.0)
    part_b.update({"id": "uuid-part-b", "name": "Bracket B"})
    _write(tmp_path, "BracketA.step+", part_a)
    _write(tmp_path, "BracketB.step+", part_b)

    vault = Vault.open(tmp_path)
    pid = vault.status()["project_id"]
    vault.set_ai_mode("managed")
    items_by_path = {item["path"]: item["item_id"] for item in vault.manifest()["items"]}
    item_a = items_by_path["BracketA.step+"]
    item_b = items_by_path["BracketB.step+"]
    vault.close()

    # Both participants land a real document change before the shared boundary.
    part_a["features"][0]["params"]["wall"] = 5.0
    part_b["features"][0]["params"]["wall"] = 6.0
    _write(tmp_path, "BracketA.step+", part_a)
    _write(tmp_path, "BracketB.step+", part_b)

    client = PLMVaultClient()
    client.register_project(pid, str(tmp_path))
    client.register_run("run_multi", pid)
    bridge = PLMBridge("run_multi", vault=client)
    link = bridge.on_milestone(
        trigger="stage_exit",
        stage="geometry",
        buildgraph_node="phase:geometry",
        item_artifacts={
            item_a: "bg_participant_a",
            item_b: "bg_participant_b",
        },
        project_id=pid,
    )
    assert link is not None and link["queued"] is False

    reopened = Vault.open(tmp_path)
    try:
        newest_a = reopened.history(item_a)[0]
        newest_b = reopened.history(item_b)[0]
        assert newest_a["kind"] == newest_b["kind"] == "checkpoint"
        assert newest_a["ai"]["run_id"] == newest_b["ai"]["run_id"] == "run_multi"
        assert newest_a["ai"]["artifact_id"] == "bg_participant_a"
        assert newest_b["ai"]["artifact_id"] == "bg_participant_b"
    finally:
        reopened.close()


def test_assisted_mode_bridge_cuts_nothing(tmp_path):
    from services.agent.plm_bridge import PLMBridge

    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    pid = v.status()["project_id"]
    n_before = len(v.history())
    v.close()

    c = PLMVaultClient()
    c.register_project(pid, str(tmp_path))
    bridge = PLMBridge("run_a", vault=c)
    link = bridge.on_milestone(trigger="mission_done", project_id=pid)
    assert link is None  # assisted: suggestions only, never AI-cut entries

    v2 = Vault.open(tmp_path)
    try:
        assert len(v2.history()) == n_before
    finally:
        v2.close()


def test_manual_floor_no_ai_entries_anywhere(tmp_path):
    """DoD: manual-only floor — no AI-cut entries via any seam."""
    from services.agent.plm_bridge import PLMBridge

    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    pid = v.status()["project_id"]
    v.set_ai_mode("manual")
    v.close()

    c = PLMVaultClient()
    c.register_project(pid, str(tmp_path))

    # in-process seam refuses
    res = c.checkpoint(project_id=pid, message="ai text", author_kind="ai")
    assert res["ok"] is False and res["reason"] == "invalid_mode"
    _assert_no_git_vocab(res["hint"])

    # bridge seam refuses (no cut, no queue)
    bridge = PLMBridge("run_m", vault=c)
    assert bridge.on_milestone(trigger="mission_done", project_id=pid) is None

    # assist helper refuses
    assert assist.auto_checkpoint(c, pid, "x", mode="manual") is None

    # the timeline holds zero AI-authored entries
    v2 = Vault.open(tmp_path)
    try:
        assert all(e["author"]["kind"] != "ai" for e in v2.history())
        # manual buttons still fully available (contract §8.4)
        cp = v2.checkpoint("done by hand")
        assert cp["entry"]["kind"] == "checkpoint"
    finally:
        v2.close()


def test_managed_auto_checkpoint_helper(tmp_path):
    pid, client = _managed_project(tmp_path)
    out = assist.auto_checkpoint(client, pid, "Part modeling complete.",
                                 mode="managed", run_id="r1", artifact_id="a1")
    assert out is not None and out["ok"] is True
    assert out["entry"]["author"]["kind"] == "ai"


# ---------------------------------------------------------------------------
# compare: semantic + visual orchestration
# ---------------------------------------------------------------------------

def test_semantic_compare_between_history_points(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        c1 = v.checkpoint("v1")["commit"]
        _write(tmp_path, "Bracket.step+", _part(5.0))
        c2 = v.checkpoint("v2")["commit"]

        doc_a, doc_b = compare.docs_at(v, c1, c2, iid)
        sem = compare.semantic_compare(doc_a, doc_b)
        assert sem["doc_diff"]["params_changed"]
        assert "wall" in sem["summary"]
        assert summarize.numeral_tokens(sem["summary"]) \
            <= summarize.diff_numerals(sem["doc_diff"])
        _assert_no_git_vocab(sem["summary"])
    finally:
        v.close()


def test_compare_unknown_point_reason_code(tmp_path):
    _write(tmp_path, "Bracket.step+", _part())
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        good = v.checkpoint("v1")["commit"]
        with pytest.raises(VaultError) as e:
            compare.docs_at(v, good, "0" * 40, iid)
        assert e.value.reason == "not_found"
    finally:
        v.close()


def test_visual_compare_degrades_without_engine(tmp_path, monkeypatch):
    # force the no-engine path regardless of the machine we run on
    import services.plm.compare as cmp_mod

    class _NoEngine:
        @staticmethod
        def available():
            return False

    monkeypatch.setitem(__import__("sys").modules, "services.mcp", None)
    out = cmp_mod.visual_compare(_part(3.0), _part(5.0), tmp_path / "viz")
    assert out["degraded"] is True
    assert out["reason"] == "engine_unavailable"
    _assert_no_git_vocab(out["hint"])


# ---------------------------------------------------------------------------
# optional learned polish: flag-gated garnish, validated, discardable
# ---------------------------------------------------------------------------

def test_polish_not_called_when_flag_off(monkeypatch):
    calls = []
    summarize.set_polish_hook(lambda text, diff: calls.append(1) or "polished")
    try:
        monkeypatch.delenv("CHUDWARE_PLM_SUMMARY_POLISH", raising=False)
        diff = doc_diff(_part(3.0), _part(5.0))
        s = summarize.summarize_diff(diff)
        assert calls == []  # deterministic-first: no hook without the flag
        assert "polished" not in s
    finally:
        summarize.set_polish_hook(None)


def test_polish_discarded_when_it_invents_numerals(monkeypatch):
    summarize.set_polish_hook(lambda text, diff: "Wall grew by 42 percent")
    try:
        monkeypatch.setenv("CHUDWARE_PLM_SUMMARY_POLISH", "1")
        diff = doc_diff(_part(3.0), _part(5.0))
        s = summarize.summarize_diff(diff)
        assert "42" not in s  # invented numeral → deterministic text kept
    finally:
        summarize.set_polish_hook(None)


def test_polish_discarded_on_git_vocab(monkeypatch):
    summarize.set_polish_hook(lambda text, diff: "Committed the wall change 3 5")
    try:
        monkeypatch.setenv("CHUDWARE_PLM_SUMMARY_POLISH", "1")
        diff = doc_diff(_part(3.0), _part(5.0))
        s = summarize.summarize_diff(diff)
        assert "Committed" not in s
    finally:
        summarize.set_polish_hook(None)


def test_polish_applied_when_valid(monkeypatch):
    summarize.set_polish_hook(lambda text, diff: text.replace("Increased", "Grew"))
    try:
        monkeypatch.setenv("CHUDWARE_PLM_SUMMARY_POLISH", "1")
        diff = doc_diff(_part(3.0), _part(5.0))
        s = summarize.summarize_diff(diff)
        assert "Grew" in s
    finally:
        summarize.set_polish_hook(None)
