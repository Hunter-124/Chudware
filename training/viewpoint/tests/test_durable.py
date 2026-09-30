"""Crash recovery of campaign artifacts: a hard reset must cost redone work, never a wedged run."""
import json

from training.viewpoint.durable import append_jsonl, durable_replace, nonempty, read_jsonl


def test_torn_tail_is_skipped_and_the_next_append_stays_parseable(tmp_path):
    p = tmp_path / "sets.jsonl"
    p.write_text(json.dumps({"set_key": "a"}) + "\n" + '{"set_key": "b", "ima')  # crash mid-row
    assert [r["set_key"] for r in read_jsonl(p)] == ["a"]
    assert append_jsonl(p, [{"set_key": "c"}, {"set_key": "d"}]) == 2
    assert [r["set_key"] for r in read_jsonl(p)] == ["a", "c", "d"]


def test_nul_filled_rows_from_unflushed_blocks_are_skipped(tmp_path):
    p = tmp_path / "labels.jsonl"
    p.write_bytes(b'{"k": 1}\n' + b"\x00" * 64 + b"\n" + b'{"k": 2}\n')
    assert [r["k"] for r in read_jsonl(p)] == [1, 2]


def test_zero_length_artifact_counts_as_missing(tmp_path):
    empty = tmp_path / "mask.npz"
    empty.write_bytes(b"")
    assert not nonempty(empty) and not nonempty(tmp_path / "absent.npz")
    tmp = tmp_path / ".mask.tmp"
    tmp.write_bytes(b"data")
    durable_replace(tmp, empty)
    assert nonempty(empty) and not tmp.exists()
