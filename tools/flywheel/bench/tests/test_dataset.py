"""Frozen-dataset hashing is deterministic and OS-independent (reproducibility)."""
import os

from tools.flywheel.bench.dataset import DatasetSpec, content_hash


def _mk(tmp_path, files):
    for rel, data in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
    return str(tmp_path)


def test_hash_is_stable_and_order_independent(tmp_path):
    root = _mk(tmp_path, {"b.json": "2", "a.json": "1", "sub/c.json": "3"})
    h1 = content_hash(root)
    h2 = content_hash(root)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_hash_changes_with_content(tmp_path):
    root = _mk(tmp_path, {"a.json": "1"})
    h1 = content_hash(root)
    (tmp_path / "a.json").write_text("2")
    assert content_hash(root) != h1


def test_freeze_verify_roundtrip(tmp_path):
    root = _mk(tmp_path, {"x/document.json": "{}", "y/document.json": "{}"})
    spec = DatasetSpec.freeze("t", root, split="test", n_examples=2,
                              structures=["x", "y"])
    assert spec.verify()
    spec_path = os.path.join(root, "spec.json")
    spec.write(spec_path)
    loaded = DatasetSpec.load(spec_path, root)
    assert loaded.hash == spec.hash
    assert loaded.structures == ["x", "y"]
    assert loaded.verify()


def test_hash_is_eol_invariant_for_text(tmp_path):
    # Cross-OS reproducibility: a CRLF checkout (Windows autocrlf) must hash identically to LF.
    root = _mk(tmp_path, {"document.json": '{"a": 1}\n{"b": 2}\n'})
    h_lf = content_hash(root)
    (tmp_path / "document.json").write_bytes(b'{"a": 1}\r\n{"b": 2}\r\n')
    h_crlf = content_hash(root)
    assert h_lf == h_crlf


def test_verify_detects_tampering(tmp_path):
    root = _mk(tmp_path, {"a.json": "1"})
    spec = DatasetSpec.freeze("t", root)
    (tmp_path / "a.json").write_text("tampered")
    assert not spec.verify()
