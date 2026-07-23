"""test_coordination.py — CoordinationBoard claim exclusivity + sidecar persistence.

Focus: cross-process claim exclusivity (P1 fix). Two independent
CoordinationBoard *instances* sharing only a sidecar file — never Python
state — are the cheapest faithful stand-in for two OS processes: each has
its own ``_lock`` and its own in-memory ``_claims`` dict, exactly like a
board built in a second process. The bug this guards against was staleness,
not a wall-clock race: a board constructed before another board's claim was
flushed never re-read the sidecar, so it happily granted itself the same
resource and clobbered the winner's claim on its own flush. The fix
re-reads the sidecar INSIDE an interprocess file lock before every
conflict check, so this reproduces deterministically without threads.
"""
from __future__ import annotations

from services.agent.coordination import CoordinationBoard, clear_boards, get_board


def test_register_claim_basic_conflict_same_board():
    board = CoordinationBoard("run_basic", persist=False)
    r1 = board.register_claim("agent_a", ["res1"], "editing res1")
    assert r1["ok"]
    r2 = board.register_claim("agent_b", ["res1"], "also editing res1")
    assert not r2["ok"] and r2["status"] == "CONFLICT"
    assert r2["conflicts"][0]["holder"] == "agent_a"


def test_register_claim_same_agent_reclaim_is_idempotent():
    board = CoordinationBoard("run_reclaim", persist=False)
    board.register_claim("agent_a", ["res1"], "first pass")
    r2 = board.register_claim("agent_a", ["res1"], "second pass")
    assert r2["ok"] and r2["claims"][0]["intent"] == "second pass"


def test_cross_process_double_claim_is_impossible(tmp_path):
    """Two boards for the same run, neither aware of the other's memory, must
    never both win the same resource — the sidecar is the sole shared truth."""
    board_a = CoordinationBoard("run_cross", runs_dir=tmp_path, persist=True)
    board_b = CoordinationBoard("run_cross", runs_dir=tmp_path, persist=True)

    ra = board_a.register_claim("agent_a", ["doc1"], "editing doc1")
    assert ra["ok"], "the first claim must succeed"

    rb = board_b.register_claim("agent_b", ["doc1"], "also editing doc1")
    assert not rb["ok"], "a second board must never win a resource already claimed"
    assert rb["status"] == "CONFLICT"
    assert rb["conflicts"][0]["holder"] == "agent_a"

    # A THIRD, freshly-constructed board reading the same sidecar sees exactly
    # one claim, held by the winner — proof nothing was clobbered on disk.
    board_c = CoordinationBoard("run_cross", runs_dir=tmp_path, persist=True)
    snap = board_c.board_snapshot()
    assert snap["claim_count"] == 1
    assert snap["claims"][0]["resource"] == "doc1"
    assert snap["claims"][0]["agent_id"] == "agent_a"


def test_cross_process_disjoint_resources_both_succeed(tmp_path):
    """The interprocess lock must not turn into an accidental global mutex —
    unrelated resources still claim independently across boards."""
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


def test_get_board_registry_dedupes_within_process():
    clear_boards()
    b1 = get_board("run_registry", persist=False)
    b2 = get_board("run_registry", persist=False)
    assert b1 is b2
    clear_boards()
