from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pin_chudcode as pin  # noqa: E402


NOTICE_BYTES = {
    "CHUDCODE-NOTICE": b"Chudcode notice fixture\n",
    "CHUDCODE-THIRD-PARTY-NOTICES.txt": b"Chudcode third-party fixture\n",
}


def _binary(target: str) -> bytes:
    if target == "linux-x86_64":
        data = bytearray(128)
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 18, 62)
        return bytes(data)
    if target == "windows-x86_64":
        data = bytearray(256)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 128)
        data[128:132] = b"PE\0\0"
        struct.pack_into("<H", data, 132, 0x8664)
        return bytes(data)
    if target == "macos-x86_64":
        return b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x01000007) + bytes(120)
    if target == "macos-arm64":
        return b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + bytes(120)
    raise AssertionError(f"unexpected release target: {target}")


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
    for name, content in NOTICE_BYTES.items():
        (tmp_path / name).write_bytes(content)
    lock = {
        "artifacts": artifacts,
        "engine_version": "18.0.8",
        "notices": {
            "files": {
                name: {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "source": pin.NOTICE_FILES[name],
                }
                for name, content in NOTICE_BYTES.items()
            },
            "repository_commit": "1" * 40,
        },
        "repository": "https://github.com/Hunter-124/chudcode.git",
        "repository_commit": "1" * 40,
        "rpc_protocol": 2,
        "schema": pin.SCHEMA,
        "version": "0.2.0",
    }
    path = tmp_path / "CHUDCODE_ARTIFACT.lock"
    path.write_bytes(pin._canonical_json(lock))
    return path, binaries


def test_stage_needs_only_the_staged_targets_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock, binaries = _lock(tmp_path)
    partial = json.loads(lock.read_bytes())
    for target in pin.SUPPORTED_TARGETS:
        if target != "linux-x86_64":
            partial["artifacts"][target].update({"sha256": None, "url": None})
    lock.write_bytes(pin._canonical_json(partial))
    monkeypatch.setattr(
        pin, "_verify_executable_version", lambda *_args: "Chudcode v0.2.0"
    )

    staged = pin.stage_binary(
        binaries["linux-x86_64"],
        tmp_path / "out" / "chud",
        "linux-x86_64",
        lock_path=lock,
    )
    assert Path(staged["staged"]).is_file()
    notices = pin.stage_notices(tmp_path / "notices", source=tmp_path, lock_path=lock)
    assert notices["notices"]
    with pytest.raises(pin.ArtifactLockError, match="windows-x86_64 is not pinned"):
        pin.verify_binary(binaries["windows-x86_64"], "windows-x86_64", lock_path=lock)
    with pytest.raises(pin.ArtifactLockError, match="not pinned"):
        pin.load_lock(lock)


def test_retained_legacy_lock_still_verifies_but_cannot_be_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Locks retained from earlier releases may list targets that are no longer
    # published. They must still load for verification; publication must not.
    lock, binaries = _lock(tmp_path)
    legacy = json.loads(lock.read_bytes())
    legacy["artifacts"]["linux-aarch64"] = {
        "filename": "chud",
        "sha256": "a" * 64,
        "url": "https://releases.example.invalid/v0.2.0/linux-aarch64/chud",
    }
    lock.write_bytes(pin._canonical_json(legacy))
    monkeypatch.setattr(pin, "_verify_executable_version", lambda *_args: "Chudcode v0.2.0")

    assert pin.verify_binary(binaries["linux-x86_64"], "linux-x86_64", lock_path=lock)
    assert pin.verify_notices(tmp_path, lock_path=lock, require_pinned=False)["notices"]
    with pytest.raises(pin.ArtifactLockError, match="unexpected: linux-aarch64"):
        pin.load_lock(lock, strict_targets=True)

    legacy["artifacts"]["linux-riscv64"] = {"filename": "chud", "sha256": None, "url": None}
    lock.write_bytes(pin._canonical_json(legacy))
    with pytest.raises(pin.ArtifactLockError, match="unexpected: .*linux-riscv64"):
        pin.load_lock(lock, require_pinned=False)


def test_retained_notice_integrity_does_not_require_foreign_binary_pins(
    tmp_path: Path,
) -> None:
    lock, _ = _lock(tmp_path)
    partial = json.loads(lock.read_bytes())
    partial["artifacts"]["macos-x86_64"].update({"sha256": None, "url": None})
    lock.write_bytes(pin._canonical_json(partial))
    args = ["--lock", str(lock), "verify-notices", "--source-dir", str(tmp_path)]

    assert pin.main(args) == 0
    # The publication gate still requires every platform; recovery does not.
    assert pin.main(["--lock", str(lock), "check-lock"]) == 1
    (tmp_path / "CHUDCODE-NOTICE").write_bytes(b"untrusted replacement")
    assert pin.main(args) == 1


def test_verified_stage_is_offline_and_uses_canonical_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock, binaries = _lock(tmp_path)
    monkeypatch.setattr(
        pin,
        "_verify_executable_version",
        lambda *_args: "Chudcode v0.2.0",
    )
    destination = tmp_path / "payload" / "chud"
    evidence = pin.stage_binary(
        binaries["linux-x86_64"], destination, "linux-x86_64", lock_path=lock
    )
    assert destination.read_bytes() == binaries["linux-x86_64"].read_bytes()
    assert evidence["rpc_protocol"] == 2
    assert evidence["version"] == "0.2.0"
    assert evidence["engine_version"] == "18.0.8"
    if os.name != "nt":
        assert destination.stat().st_mode & 0o111



def test_notice_staging_has_target_parity_and_no_stale_omp_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock, binaries = _lock(tmp_path)
    monkeypatch.setattr(
        pin,
        "_verify_executable_version",
        lambda *_args: "Chudcode v0.2.0",
    )
    expected = pin.verify_notices(
        tmp_path, lock_path=lock
    )["notices"]
    for target in pin.SUPPORTED_TARGETS:
        payload = tmp_path / "payloads" / target
        pin.stage_binary(
            binaries[target],
            payload / pin.CANONICAL_NAMES[target],
            target,
            lock_path=lock,
        )
        evidence = pin.stage_notices(
            payload, source=tmp_path, lock_path=lock
        )
        assert evidence["notices"] == expected
        assert {
            path.name for path in payload.iterdir()
        } == {
            pin.CANONICAL_NAMES[target],
            *pin.NOTICE_FILES,
        }
        assert not any(path.name.startswith("OMP") for path in payload.iterdir())


def test_missing_tampered_and_mismatched_notice_provenance_fail_closed(
    tmp_path: Path,
) -> None:
    lock, _ = _lock(tmp_path)
    (tmp_path / "CHUDCODE-NOTICE").unlink()
    with pytest.raises(pin.ArtifactLockError, match="missing or unsafe"):
        pin.verify_notices(tmp_path, lock_path=lock)

    (tmp_path / "CHUDCODE-NOTICE").write_bytes(
        NOTICE_BYTES["CHUDCODE-NOTICE"] + b"tamper"
    )
    with pytest.raises(pin.ArtifactLockError, match="sha256 mismatch"):
        pin.verify_notices(tmp_path, lock_path=lock)

    parsed = json.loads(lock.read_text())
    parsed["notices"]["repository_commit"] = "2" * 40
    lock.write_bytes(pin._canonical_json(parsed))
    with pytest.raises(pin.ArtifactLockError, match="repository commit"):
        pin.load_lock(lock)


def test_stale_omp_notices_fail_closed_in_source_and_payload(tmp_path: Path) -> None:
    lock, _ = _lock(tmp_path)
    stale_source = tmp_path / "OMP-NOTICE"
    stale_source.write_bytes(b"obsolete attribution aggregate")
    with pytest.raises(pin.ArtifactLockError, match="stale OMP notice"):
        pin.verify_notices(tmp_path, lock_path=lock)
    stale_source.unlink()

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "OMP-THIRD-PARTY-NOTICES.txt").write_bytes(b"obsolete")
    with pytest.raises(pin.ArtifactLockError, match="stale OMP notice"):
        pin.stage_notices(payload, source=tmp_path, lock_path=lock)


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
            args[0], 0, "Chudcode v0.2.0\n", ""
        ),
    )
    assert pin._verify_executable_version(
        binaries["linux-x86_64"], "linux-x86_64", lock
    ) == "Chudcode v0.2.0"

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


def test_target_selector_requires_exactly_one_complete_mode() -> None:
    assert pin._selected_target(
        pin.argparse.Namespace(target="linux-x86_64", platform=None, arch=None)
    ) == "linux-x86_64"
    assert pin._selected_target(
        pin.argparse.Namespace(target=None, platform="linux", arch="x86_64")
    ) == "linux-x86_64"

    invalid = (
        pin.argparse.Namespace(target=None, platform=None, arch=None),
        pin.argparse.Namespace(target=None, platform="linux", arch=None),
        pin.argparse.Namespace(target=None, platform=None, arch="x86_64"),
        pin.argparse.Namespace(
            target="linux-x86_64", platform="linux", arch="x86_64"
        ),
        pin.argparse.Namespace(
            target="linux-x86_64", platform="linux", arch=None
        ),
    )
    for selector in invalid:
        with pytest.raises(pin.ArtifactLockError):
            pin._selected_target(selector)


def test_committed_lock_loads_and_pins_the_linux_target() -> None:
    # The committed lock must keep the supported target set; a drifted set makes
    # every stage/ensure fail even though the binary itself is fine.
    lock = pin.load_lock(pin.DEFAULT_LOCK, require_pinned=False)
    assert lock["artifacts"]["linux-x86_64"]["sha256"]
    pin.verify_notices(lock_path=pin.DEFAULT_LOCK, require_pinned=False)


def test_ensure_rejects_a_tampered_cache_before_execution_or_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, binaries = _lock(tmp_path)
    lock = pin.load_lock(lock_path)
    cached = (tmp_path / "cache" /
              lock["artifacts"]["linux-x86_64"]["sha256"] / "chud")
    cached.parent.mkdir(parents=True)
    tampered = binaries["linux-x86_64"].read_bytes() + b"tampered"
    cached.write_bytes(tampered)

    def forbidden(*args, **kwargs):
        pytest.fail("unverified cache must not execute or fetch replacement bytes")

    monkeypatch.setattr(pin, "_verify_executable_version", forbidden)
    monkeypatch.setattr(pin, "fetch_artifact", forbidden)
    with pytest.raises(pin.ArtifactLockError, match="sha256 mismatch"):
        pin.ensure_artifact(tmp_path / "cache", "linux-x86_64", lock_path=lock_path)
    assert cached.read_bytes() == tampered


def test_ensure_an_explicit_wrong_target_never_falls_back_to_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, binaries = _lock(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("an explicit invalid installation must fail closed")

    monkeypatch.setattr(pin, "fetch_artifact", forbidden)
    with pytest.raises(pin.ArtifactLockError, match="wrong-target"):
        pin.ensure_artifact(
            tmp_path / "cache", "linux-x86_64",
            source=binaries["windows-x86_64"], lock_path=lock_path,
        )


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, *_args):
        body, self._body = self._body, b""
        return body


class _Opener:
    """Records requested URLs; answers with a redirect or a body per URL."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def open(self, url, timeout=None):
        self.requested.append(url)
        route = self.routes[url]
        if isinstance(route, bytes):
            return _Response(route)
        raise pin.urllib.error.HTTPError(url, 302, "Found", {"Location": route}, None)


def _use_opener(monkeypatch: pytest.MonkeyPatch, routes: dict[str, object]) -> _Opener:
    opener = _Opener(routes)
    monkeypatch.setattr(pin.urllib.request, "build_opener", lambda *_handlers: opener)
    return opener


def test_fetch_follows_one_github_asset_redirect_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, binaries = _lock(tmp_path)
    url = "https://github.com/Hunter-124/chudcode/releases/download/chudcode-v0.2.0/chudcode-linux-x64"
    cdn = "https://release-assets.githubusercontent.com/github-production-release-asset/1/2"
    parsed = json.loads(lock_path.read_bytes())
    parsed["artifacts"]["linux-x86_64"]["url"] = url
    lock_path.write_bytes(pin._canonical_json(parsed))
    opener = _use_opener(monkeypatch, {url: cdn, cdn: binaries["linux-x86_64"].read_bytes()})
    monkeypatch.setattr(pin, "_verify_executable_version", lambda *_args: "Chudcode v0.2.0")

    evidence = pin.ensure_artifact(tmp_path / "cache", "linux-x86_64", lock_path=lock_path)
    assert opener.requested == [url, cdn]
    assert Path(evidence["path"]).read_bytes() == binaries["linux-x86_64"].read_bytes()


@pytest.mark.parametrize("pinned, location", [
    ("https://github.com/Hunter-124/chudcode/releases/download/chudcode-v0.2.0/chudcode-linux-x64",
     "https://attacker.example.invalid/chud"),
    ("https://releases.example.invalid/v0.2.0/linux-x86_64/chud",
     "https://release-assets.githubusercontent.com/github-production-release-asset/1/2"),
])
def test_fetch_refuses_untrusted_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: str, location: str
) -> None:
    lock_path, _ = _lock(tmp_path)
    parsed = json.loads(lock_path.read_bytes())
    parsed["artifacts"]["linux-x86_64"]["url"] = pinned
    lock_path.write_bytes(pin._canonical_json(parsed))
    opener = _use_opener(monkeypatch, {pinned: location})
    with pytest.raises(pin.ArtifactLockError, match="redirected"):
        pin.fetch_artifact(tmp_path / "out" / "chud", "linux-x86_64", lock_path=lock_path)
    assert opener.requested == [pinned]
    assert not (tmp_path / "out" / "chud").exists()


@pytest.mark.parametrize("failure", ["open", "read"])
def test_failed_download_preserves_error_and_destination_without_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    lock, _ = _lock(tmp_path)
    destination = tmp_path / "out" / "chud.exe"
    destination.parent.mkdir()
    destination.write_bytes(b"retained installation")
    error = TimeoutError("interrupted artifact transfer")

    class InterruptedResponse(_Response):
        def read(self, *_args):
            if self._body:
                return super().read()
            raise error

    def open_artifact(_url):
        if failure == "open":
            raise error
        return InterruptedResponse(b"partial download")

    monkeypatch.setattr(pin, "_open_pinned_url", open_artifact)
    with pytest.raises(TimeoutError) as caught:
        pin.fetch_artifact(destination, "windows-x86_64", lock_path=lock)
    assert caught.value is error
    assert destination.read_bytes() == b"retained installation"
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("stage", [False, True])
def test_replaced_input_is_never_executed_or_published(tmp_path, monkeypatch, stage):
    lock, binaries = _lock(tmp_path)
    source = binaries["linux-x86_64"]
    approved = source.read_bytes()
    observed = []
    def verify_private_snapshot(snapshot, _target, _lock):
        source.write_bytes(b"substituted inert input")
        observed.append(snapshot)
        assert snapshot != source
        assert snapshot.read_bytes() == approved
        if os.name != "nt":
            assert snapshot.parent.stat().st_mode & 0o077 == 0
        return "Chudcode v0.2.0"
    monkeypatch.setattr(pin, "_verify_executable_version", verify_private_snapshot)
    if stage:
        destination = tmp_path / "payload" / "chud"
        pin.stage_binary(source, destination, "linux-x86_64", lock_path=lock)
        assert destination.read_bytes() == approved
    else:
        pin.verify_binary(source, "linux-x86_64", lock_path=lock)
    assert not observed[0].exists()
