from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pin_chudcode as pin  # noqa: E402


def _binary(target: str) -> bytes:
    if target.startswith("linux-"):
        machine = 62 if target.endswith("x86_64") else 183
        data = bytearray(128)
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 18, machine)
        return bytes(data)
    if target == "windows-x86_64":
        data = bytearray(256)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 128)
        data[128:132] = b"PE\0\0"
        struct.pack_into("<H", data, 132, 0x8664)
        return bytes(data)
    cpu = 0x01000007 if target.endswith("x86_64") else 0x0100000C
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + bytes(120)


def _lock(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    artifacts = {}
    binaries = {}
    for target in pin.SUPPORTED_TARGETS:
        binary = tmp_path / f"input-{target}"
        binary.write_bytes(_binary(target))
        binaries[target] = binary
        artifacts[target] = {
            "filename": pin.CANONICAL_NAMES[target],
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "url": f"https://releases.example.invalid/v0.2.0/{target}/{pin.CANONICAL_NAMES[target]}",
        }
    lock = {
        "artifacts": artifacts,
        "engine_version": "18.0.8",
        "repository": "https://github.com/Hunter-124/chudcode.git",
        "repository_commit": "1" * 40,
        "rpc_protocol": 2,
        "schema": pin.SCHEMA,
        "version": "0.2.0",
    }
    path = tmp_path / "CHUDCODE_ARTIFACT.lock"
    path.write_bytes(pin._canonical_json(lock))
    return path, binaries


def test_checked_in_lock_declares_every_required_release_input() -> None:
    lock = pin.load_lock(pin.DEFAULT_LOCK, require_pinned=False)
    assert set(lock["artifacts"]) == set(pin.SUPPORTED_TARGETS)
    assert lock["rpc_protocol"] == 2
    assert lock["version"] == "0.2.0"
    assert lock["engine_version"] == "18.0.8"
    with pytest.raises(pin.ArtifactLockError, match="not pinned"):
        pin.load_lock(pin.DEFAULT_LOCK)


def test_verified_stage_is_offline_and_uses_canonical_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock, binaries = _lock(tmp_path)
    monkeypatch.setattr(
        pin,
        "_verify_executable_version",
        lambda *_args: "Chudcode v0.2.0 (based on omp 18.0.8)",
    )
    destination = tmp_path / "payload" / "chud"
    evidence = pin.stage_binary(
        binaries["linux-x86_64"], destination, "linux-x86_64", lock_path=lock
    )
    assert destination.read_bytes() == binaries["linux-x86_64"].read_bytes()
    assert evidence["rpc_protocol"] == 2
    assert evidence["version"] == "0.2.0"
    assert evidence["engine_version"] == "18.0.8"
    assert destination.stat().st_mode & 0o111


def test_missing_tampered_and_wrong_target_binaries_fail_closed(tmp_path: Path) -> None:
    lock, binaries = _lock(tmp_path)
    with pytest.raises(pin.ArtifactLockError, match="missing or unsafe"):
        pin.verify_binary(tmp_path / "missing", "linux-x86_64", lock_path=lock)
    binaries["linux-x86_64"].write_bytes(binaries["linux-x86_64"].read_bytes() + b"tamper")
    with pytest.raises(pin.ArtifactLockError, match="sha256 mismatch"):
        pin.verify_binary(binaries["linux-x86_64"], "linux-x86_64", lock_path=lock)
    with pytest.raises(pin.ArtifactLockError, match="wrong-target"):
        pin.verify_binary(binaries["windows-x86_64"], "linux-x86_64", lock_path=lock)


def test_lock_rejects_noncanonical_unknown_and_partial_artifacts(tmp_path: Path) -> None:
    lock, _ = _lock(tmp_path)
    parsed = json.loads(lock.read_text())
    parsed["surprise"] = True
    lock.write_text(json.dumps(parsed))
    with pytest.raises(pin.ArtifactLockError, match="canonical|top-level"):
        pin.load_lock(lock)

    lock, _ = _lock(tmp_path)
    parsed = json.loads(lock.read_text())
    parsed["artifacts"]["linux-x86_64"]["sha256"] = None
    lock.write_bytes(pin._canonical_json(parsed))
    with pytest.raises(pin.ArtifactLockError, match="set url and sha256 together"):
        pin.load_lock(lock, require_pinned=False)


def test_wrong_target_lock_entry_fails_closed(tmp_path: Path) -> None:
    lock, _ = _lock(tmp_path)
    parsed = json.loads(lock.read_text())
    parsed["artifacts"]["linux-x86_64"] = parsed["artifacts"]["windows-x86_64"]
    lock.write_bytes(pin._canonical_json(parsed))
    with pytest.raises(pin.ArtifactLockError, match="canonical filename"):
        pin.load_lock(lock)


def test_executable_version_separates_product_from_engine_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, binaries = _lock(tmp_path)
    lock = pin.load_lock(lock_path)
    monkeypatch.setattr(pin, "host_target", lambda: "linux-x86_64")
    monkeypatch.setattr(
        pin.subprocess,
        "run",
        lambda *args, **kwargs: pin.subprocess.CompletedProcess(
            args[0], 0, "Chudcode v0.2.0 (based on omp 18.0.8)\n", ""
        ),
    )
    assert pin._verify_executable_version(
        binaries["linux-x86_64"], "linux-x86_64", lock
    ) == "Chudcode v0.2.0 (based on omp 18.0.8)"

    monkeypatch.setattr(
        pin.subprocess,
        "run",
        lambda *args, **kwargs: pin.subprocess.CompletedProcess(
            args[0], 0, "Chudcode v18.0.8\n", ""
        ),
    )
    with pytest.raises(pin.ArtifactLockError, match="product version mismatch"):
        pin._verify_executable_version(
            binaries["linux-x86_64"], "linux-x86_64", lock
        )

    monkeypatch.setattr(
        pin.subprocess,
        "run",
        lambda *args, **kwargs: pin.subprocess.CompletedProcess(
            args[0], 0, "Chudcode v0.2.0 (based on omp 0.2.0)\n", ""
        ),
    )
    with pytest.raises(pin.ArtifactLockError, match="product version mismatch"):
        pin._verify_executable_version(
            binaries["linux-x86_64"], "linux-x86_64", lock
        )

    lock["engine_version"] = None
    monkeypatch.setattr(
        pin.subprocess,
        "run",
        lambda *args, **kwargs: pin.subprocess.CompletedProcess(
            args[0], 0, "Chudcode v0.2.0\n", ""
        ),
    )
    assert pin._verify_executable_version(
        binaries["linux-x86_64"], "linux-x86_64", lock
    ) == "Chudcode v0.2.0"
