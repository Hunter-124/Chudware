"""Shared work intents and lossless coordination across independent workers."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from services.agent.coordination import CoordinationBoard, clear_boards, get_board


def test_overlapping_work_is_advisory_not_exclusive():
    board = CoordinationBoard("run_basic", persist=False)
    first = board.register_claim("agent_a", ["res1"], "editing bore")
    second = board.register_claim("agent_b", ["res1", "res2"], "editing fillet")
    assert first["ok"] and second["ok"]
    assert {(c["agent_id"], c["resource"]) for c in board.board_snapshot()["claims"]} == {
        ("agent_a", "res1"), ("agent_b", "res1"), ("agent_b", "res2"),
    }


def test_register_claim_same_agent_reclaim_is_idempotent():
    board = CoordinationBoard("run_reclaim", persist=False)
    first = board.register_claim("agent_a", ["res1"], "first pass")
    second = board.register_claim("agent_a", ["res1", "res1"], "second pass")
    assert second["ok"]
    assert second["claims"][0]["claim_id"] == first["claims"][0]["claim_id"]
    assert [(c["resource"], c["intent"]) for c in board.board_snapshot()["claims"]] == [
        ("res1", "second pass"),
    ]


def test_independent_workers_preserve_shared_resource_intents(tmp_path):
    board_a = CoordinationBoard("run_cross", runs_dir=tmp_path)
    board_b = CoordinationBoard("run_cross", runs_dir=tmp_path)
    assert board_a.register_claim("agent_a", ["doc1"], "editing bore")["ok"]
    assert board_b.register_claim("agent_b", ["doc1"], "editing fillet")["ok"]
    for board in (board_a, board_b, CoordinationBoard("run_cross", runs_dir=tmp_path)):
        assert {(c["agent_id"], c["resource"]) for c in board.board_snapshot()["claims"]} == {
            ("agent_a", "doc1"), ("agent_b", "doc1"),
        }


def test_cross_process_disjoint_resources_both_succeed(tmp_path):
    """Unrelated work remains visible across independent worker instances."""
    board_a = CoordinationBoard("run_disjoint", runs_dir=tmp_path, persist=True)
    board_b = CoordinationBoard("run_disjoint", runs_dir=tmp_path, persist=True)

    ra = board_a.register_claim("agent_a", ["doc1"], "editing doc1")
    rb = board_b.register_claim("agent_b", ["doc2"], "editing doc2")
    assert ra["ok"] and rb["ok"]

    board_c = CoordinationBoard("run_disjoint", runs_dir=tmp_path, persist=True)
    snap = board_c.board_snapshot()
    assert snap["claim_count"] == 2
    holders = {c["resource"]: c["agent_id"] for c in snap["claims"]}
    assert holders == {"doc1": "agent_a", "doc2": "agent_b"}


def test_release_and_progress_still_work_with_persist(tmp_path):
    board = CoordinationBoard("run_ops", runs_dir=tmp_path, persist=True)
    board.register_claim("agent_a", ["res1"], "working")
    rel = board.release_claim("agent_a", ["res1"])
    assert rel["ok"] and rel["released"] == ["res1"]
    prog = board.post_progress("agent_a", "done with res1")
    assert prog["ok"]

    board2 = CoordinationBoard("run_ops", runs_dir=tmp_path, persist=True)
    snap = board2.board_snapshot()
    assert snap["claim_count"] == 0
    assert snap["progress"][-1]["note"] == "done with res1"


def test_stale_progress_and_release_never_erase_peer_work(tmp_path):
    first = CoordinationBoard("interleaved", runs_dir=tmp_path)
    stale = CoordinationBoard("interleaved", runs_dir=tmp_path)
    first.register_claim("a", ["shared", "a-only"], "bore")
    stale.register_claim("b", ["shared", "b-only"], "fillet")
    first.post_progress("a", "bore ready")
    stale.post_progress("b", "fillet ready")
    first.release_claim("a", ["shared"])
    snapshot = stale.board_snapshot()
    assert {(c["agent_id"], c["resource"]) for c in snapshot["claims"]} == {
        ("a", "a-only"), ("b", "shared"), ("b", "b-only"),
    }
    assert [(p["agent_id"], p["note"]) for p in snapshot["progress"]] == [
        ("a", "bore ready"), ("b", "fillet ready"),
    ]
    assert first.board_snapshot() == snapshot


@pytest.mark.parametrize("selector", ["resources", "claim_ids"])
def test_release_empty_selection_preserves_all_work(selector):
    board = CoordinationBoard("empty", persist=False)
    board.register_claim("a", ["shared"], "bore")
    board.register_claim("b", ["shared"], "fillet")
    before = board.board_snapshot()
    assert board.release_claim("a", **{selector: []})["released"] == []
    assert board.board_snapshot() == before


def test_release_by_id_never_removes_peer_or_unselected_intent():
    board = CoordinationBoard("release", persist=False)
    own = board.register_claim("a", ["shared", "other"], "bore")["claims"]
    peer = board.register_claim("b", ["shared"], "fillet")["claims"]
    board.release_claim("a", claim_ids=[own[0]["claim_id"], peer[0]["claim_id"]])
    assert {(c["agent_id"], c["resource"]) for c in board.board_snapshot()["claims"]} == {
        ("a", "other"), ("b", "shared"),
    }
    board.release_claim("a")
    assert board.board_snapshot()["claims"] == peer


def test_concurrent_board_updates_preserve_all_peers(tmp_path):
    count = 6
    boards = [CoordinationBoard("parallel", runs_dir=tmp_path) for _ in range(count)]
    ready = threading.Barrier(count)

    def work(index):
        board = boards[index]
        agent = f"agent-{index}"
        ready.wait(timeout=5)
        assert board.register_claim(agent, ["shared", agent], "working")["ok"]
        board.post_progress(agent, "saved")
        board.release_claim(agent, [agent])

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(work, range(count)))
    snapshot = CoordinationBoard("parallel", runs_dir=tmp_path).board_snapshot()
    assert {(c["agent_id"], c["resource"]) for c in snapshot["claims"]} == {
        (f"agent-{index}", "shared") for index in range(count)
    }
    assert sorted((p["agent_id"], p["note"]) for p in snapshot["progress"]) == [
        (f"agent-{index}", "saved") for index in range(count)
    ]


def test_get_board_registry_dedupes_within_process():
    clear_boards()
    b1 = get_board("run_registry", persist=False)
    b2 = get_board("run_registry", persist=False)
    assert b1 is b2
    clear_boards()
