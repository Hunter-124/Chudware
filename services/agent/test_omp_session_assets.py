from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from services.agent import omp_host


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ref(data: bytes) -> str:
    return "blob:sha256:" + _digest(data)


def _journal(path: Path, *records: dict) -> bytes:
    prefix = b"".join(json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records)
    path.write_bytes(prefix)
    return prefix


def _image_entry(ref: str) -> dict:
    return {"type": "message", "message": {"role": "user", "content": [
        {"type": "image", "mimeType": "image/png", "data": ref}
    ]}}

def _branch_image(identifier: str, parent: str | None, ref: str, **extra) -> dict:
    return {**_image_entry(ref), "id": identifier, "parentId": parent, **extra}


class AssetBridge:
    def __init__(self, staging: Path, blobs: dict[str, bytes] | None = None, *, required=None):
        self.staging = staging
        self.blobs = dict(blobs or {})
        self.operations = []
        self.required = required
        self.seen = []

    def call(self, operation, arguments, *, timeout):
        self.operations.append((operation, dict(arguments)))
        if operation == "cad_image_blobs_status":
            self.seen.extend(arguments["hashes"])
            return {"states": [{"hash": digest, "state": (
                "missing" if digest not in self.blobs else
                "present" if _digest(self.blobs[digest]) == digest else "invalid"
            )} for digest in arguments["hashes"]]}
        if operation == "cad_image_context_refs":
            return {"required": self.seen if self.required is None else self.required,
                    "framesSeen": [], "framesSelected": [], "framesUnavailable": [],
                    "malformedRequired": False, "malformedFrame": False}
        assert operation == "cad_image_blob_import"
        digest = arguments["hash"]
        if digest in self.blobs:
            return {"hash": digest, "state": "present"}
        data = (self.staging / digest).read_bytes()
        assert _digest(data) == digest
        self.blobs[digest] = data
        return {"hash": digest, "state": "imported", "ref": "blob:sha256:" + digest}


def _client(staging: Path | None, bridge: AssetBridge, journal: Path):
    return SimpleNamespace(_omp_asset_recovery_available=True, _omp_asset_staging_dir=staging,
                           _omp_asset_journal=journal, _omp_bridge=bridge, _chudware_activity_sandboxed=False)


def _legacy_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = tmp_path / ".chudcode" / "agent" / "blobs"
    root.mkdir(parents=True)
    staging = tmp_path / "private-staging"
    staging.mkdir(mode=0o700)
    return root, staging


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("this platform does not allow creating test symlinks")


def test_imports_exact_deduplicated_image_without_touching_present_or_opaque(monkeypatch, tmp_path):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    restored, existing = b"legacy image bytes\x00\xff", b"already present"
    (legacy / _digest(restored)).write_bytes(restored)
    # This candidate must not be read: the official blob is already good.
    (legacy / _digest(existing)).write_bytes(b"corrupt legacy candidate")
    journal = tmp_path / "history.jsonl"
    prefix = _journal(journal, _image_entry(_ref(restored)), _image_entry(_ref(restored)),
        {"type": "message", "message": {"role": "toolResult", "images": [
            {"mimeType": "image/png", "data": _ref(existing)}]}},
        {"type": "message", "message": {"content": [
            {"type": "thinking", "thinking": "private opaque text", "thinkingSignature": "do-not-touch",
             "image_url": "blob:sha256:not-a-reference"},
            {"type": "text", "text": "blob:sha256:not-an-image"}]}})
    bridge = AssetBridge(staging, {_digest(existing): existing})
    result = omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)
    assert result == {"referenced": 2, "present": 1, "imported": 1,
                      "historical_unavailable": [], "historical_malformed": 0,
                      "frame_unavailable": [], "frame_malformed": 0}
    assert bridge.blobs == {_digest(existing): existing, _digest(restored): restored}
    assert [operation for operation, _ in bridge.operations].count("cad_image_blob_import") == 1
    assert list(staging.iterdir()) == []
    assert journal.read_bytes() == prefix


@pytest.mark.parametrize("kind", ["missing", "corrupt", "symlink", "directory", "oversized"])
def test_missing_or_unsafe_legacy_asset_is_explicit_and_never_imported(monkeypatch, tmp_path, kind):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    image = b"expected exact asset"
    candidate = legacy / _digest(image)
    if kind == "corrupt":
        candidate.write_bytes(b"different bytes")
    elif kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(image)
        _symlink_or_skip(candidate, outside)
    elif kind == "directory":
        candidate.mkdir()
    elif kind == "oversized":
        candidate.write_bytes(image)
        monkeypatch.setattr(omp_host, "_MAX_SESSION_IMAGE_BYTES", 4)
    journal = tmp_path / "history.jsonl"
    prefix = _journal(journal, _image_entry(_ref(image)))
    bridge = AssetBridge(staging)
    with pytest.raises(omp_host.SessionAssetError, match="session_asset_error.*recoverable") as error:
        omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)
    assert _ref(image) in str(error.value)
    assert not bridge.blobs
    assert not any(operation == "cad_image_blob_import" for operation, _ in bridge.operations)
    assert list(staging.iterdir()) == []
    assert journal.read_bytes() == prefix


def test_symlinked_legacy_ancestor_is_rejected(monkeypatch, tmp_path):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    image = b"unsafe ancestor"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / _digest(image)).write_bytes(image)
    legacy.rmdir()
    _symlink_or_skip(legacy, outside, directory=True)
    journal = tmp_path / "history.jsonl"
    _journal(journal, _image_entry(_ref(image)))
    bridge = AssetBridge(staging)
    with pytest.raises(omp_host.SessionAssetError, match="unsafe"):
        omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)
    assert not bridge.blobs


def test_corrupt_official_asset_is_not_overwritten_from_legacy(monkeypatch, tmp_path):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    image = b"known good bytes"
    (legacy / _digest(image)).write_bytes(image)
    journal = tmp_path / "history.jsonl"
    _journal(journal, _image_entry(_ref(image)))
    bridge = AssetBridge(staging, {_digest(image): b"damaged official bytes"})
    with pytest.raises(omp_host.SessionAssetError, match="corrupt or unsafe current asset"):
        omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)
    assert bridge.blobs[_digest(image)] == b"damaged official bytes"


def test_import_response_must_match_verified_reference(monkeypatch, tmp_path):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    image = b"must match exact returned ref"
    (legacy / _digest(image)).write_bytes(image)
    journal = tmp_path / "history.jsonl"
    prefix = _journal(journal, _image_entry(_ref(image)))

    class WrongReference(AssetBridge):
        def call(self, operation, arguments, *, timeout):
            result = super().call(operation, arguments, timeout=timeout)
            if operation == "cad_image_blob_import":
                result["ref"] = "blob:sha256:" + "0" * 64
            return result

    with pytest.raises(omp_host.SessionAssetError, match="image import failed"):
        omp_host.recover_session_image_assets(_client(staging, WrongReference(staging), journal), journal)
    assert list(staging.iterdir()) == []
    assert journal.read_bytes() == prefix


def test_scoped_and_already_open_clients_never_read_desktop_legacy(monkeypatch, tmp_path):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    image = b"not for another credential"
    (legacy / _digest(image)).write_bytes(image)
    journal = tmp_path / "history.jsonl"
    _journal(journal, _image_entry(_ref(image)))
    bridge = AssetBridge(staging)
    scoped = _client(None, bridge, journal)
    with pytest.raises(omp_host.SessionAssetError, match="credential or tenant scope"):
        omp_host.recover_session_image_assets(scoped, journal)
    assert len(bridge.operations) == 2 and not bridge.blobs
    opened = _client(staging, bridge, journal)
    opened._omp_asset_recovery_available = False
    with pytest.raises(omp_host.SessionAssetError, match="fresh, unopened runtime"):
        omp_host.recover_session_image_assets(opened, journal)
    with pytest.raises(omp_host.SessionAssetError, match="owning journal"):
        omp_host.recover_session_image_assets(_client(staging, bridge, journal), tmp_path / "other.jsonl")
    assert len(bridge.operations) == 2
    monkeypatch.setattr(omp_host.tenancy, "in_cell", lambda: True)
    with pytest.raises(omp_host.SessionAssetError, match="across tenant scope"):
        omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)
    assert len(bridge.operations) == 2 and not bridge.blobs


def test_compacted_and_offbranch_images_are_opportunistic_not_required(monkeypatch, tmp_path, caplog):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    old_missing, old_recoverable = b"missing old", b"recoverable old"
    kept, offbranch = b"kept current", b"inactive branch"
    (legacy / _digest(old_recoverable)).write_bytes(old_recoverable)
    bridge = AssetBridge(staging, {_digest(kept): kept}, required=[_digest(kept)])
    journal = tmp_path / "history.jsonl"
    prefix = _journal(journal,
        _branch_image("old", None, _ref(old_missing)),
        _branch_image("old2", "old", _ref(old_recoverable)),
        _branch_image("keep", "old2", _ref(kept)),
        {"type": "compaction", "id": "compact", "parentId": "keep", "firstKeptEntryId": "keep"},
        {"type": "message", "id": "post", "parentId": "compact", "message": {"role": "user", "content": []}},
        _branch_image("other", "old", _ref(offbranch), activeLeafId="post"))
    result = omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)
    assert result["referenced"] == 4 and result["present"] == 1 and result["imported"] == 1
    assert {item["reference"] for item in result["historical_unavailable"]} == {_ref(old_missing), _ref(offbranch)}
    assert bridge.blobs[_digest(old_recoverable)] == old_recoverable
    assert _ref(old_missing) in caplog.text and _ref(offbranch) in caplog.text
    assert journal.read_bytes() == prefix


@pytest.mark.parametrize("position", ["compacted_message", "custom_metadata"])
def test_corrupt_historical_image_is_refused_before_eager_hydration(monkeypatch, tmp_path, position):
    _, staging = _legacy_home(monkeypatch, tmp_path)
    damaged, kept = b"damaged old", b"current"
    old = (_branch_image("old", None, _ref(damaged)) if position == "compacted_message" else
           {"type": "custom", "id": "old", "parentId": None,
            "data": {"image_url": _ref(damaged)}})
    journal = tmp_path / "history.jsonl"
    prefix = _journal(journal, old,
        _branch_image("keep", "old", _ref(kept)),
        {"type": "compaction", "id": "compact", "parentId": "keep", "firstKeptEntryId": "keep"})
    bridge = AssetBridge(staging, {_digest(damaged): b"bad", _digest(kept): kept}, required=[_digest(kept)])
    with pytest.raises(omp_host.SessionAssetError):
        omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)
    assert bridge.blobs[_digest(damaged)] == b"bad"
    assert journal.read_bytes() == prefix


def test_retained_tail_remains_required_even_when_also_historical(monkeypatch, tmp_path):
    legacy, staging = _legacy_home(monkeypatch, tmp_path)
    missing = b"still in retained tail"
    journal = tmp_path / "history.jsonl"
    _journal(journal,
        _branch_image("old", None, _ref(missing)),
        _branch_image("keep", "old", _ref(missing)),
        {"type": "compaction", "id": "compact", "parentId": "keep", "firstKeptEntryId": "keep"})
    bridge = AssetBridge(staging)
    with pytest.raises(omp_host.SessionAssetError, match="missing legacy asset"):
        omp_host.recover_session_image_assets(_client(staging, bridge, journal), journal)




def test_cold_resume_recovers_before_switch_and_failed_resume_is_retryable(monkeypatch, tmp_path):
    from services.agent.test_omp_host import FakeBridge, FakeClient, FakeFactory, run

    legacy, _ = _legacy_home(monkeypatch, tmp_path)
    image, old = b"cold resume image", b"unavailable compacted image"
    journal = tmp_path / "history.jsonl"
    prefix = _journal(journal, _branch_image("old", None, _ref(old)),
                      _branch_image("keep", "old", _ref(image)),
                      {"type": "compaction", "id": "compact", "parentId": "keep",
                       "firstKeptEntryId": "keep"})
    blobs = {}
    order = []

    class RuntimeBridge(FakeBridge):
        def __init__(self, runtime_dir, dispatch):
            super().__init__(runtime_dir, dispatch)
            self.assets = AssetBridge(Path(runtime_dir) / "images", blobs, required=[_digest(image)])

        def call(self, operation, arguments, *, timeout, cancel_event=None):
            if operation.startswith("cad_image_"):
                order.append(operation)
                value = self.assets.call(operation, arguments, timeout=timeout)
                blobs.update(self.assets.blobs)
                return value
            return super().call(operation, arguments, timeout=timeout, cancel_event=cancel_event)

    class RuntimeClient(FakeClient):
        def switch_session(self, path):
            assert blobs[_digest(image)] == image
            order.append("switch_session")
            return super().switch_session(path)

    monkeypatch.setattr("services.agent.omp_bridge.ExtensionBridge", RuntimeBridge)
    monkeypatch.setattr(omp_host, "_keyless_provider_opt_ins", lambda: [])
    factory = FakeFactory()

    def make_client(**kwargs):
        client = RuntimeClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = omp_host.OmpSessionHost(client_factory=make_client)
    try:
        failed, frames = run(host, "asset-session", "first", omp_session_file=str(journal))
        assert failed["ok"] is False and failed["code"] == "session_asset_error"
        assert failed["recoverable"] is True and _ref(image) in failed["reason"]
        assert any(frame.get("type") == "run_end" and "session_asset_error" in frame["reason"] for frame in frames)
        assert factory.clients[0].stop_count == 1
        assert "switch_session" not in order
        (legacy / _digest(image)).write_bytes(image)
        result, frames = run(host, "asset-session", "retry", omp_session_file=str(journal))
        assert result["ok"] is True
        assert result["session_assets"]["historical_unavailable"][0]["reference"] == _ref(old)
        assert any(frame.get("type") == "notice" and "older conversation image" in frame["text"] for frame in frames)
        assert order[-2:] == ["cad_image_context_refs", "switch_session"]
        assert journal.read_bytes() == prefix
    finally:
        host.shutdown()


_RUNTIME_RUNNER = r'''
import json
import os
from pathlib import Path
from services.agent.omp_bridge import BridgeError
from services.agent.omp_host import OmpSessionHost, _json_value

root = Path(os.environ["OMP_ASSET_TEST_ROOT"])
host = OmpSessionHost(binary=os.environ["CHUDWARE_OMP_BIN"])
session = None
try:
    path = root / "sessions" / "history.jsonl"
    session = host._get_or_create_session("synthetic", {"workspace_dir": str(root)}, str(path))
    bridge = session.client._omp_bridge
    present = os.environ["OMP_ASSET_PRESENT_HASH"]
    corrupt = os.environ["OMP_ASSET_CORRUPT_HASH"]
    absent = os.environ["OMP_ASSET_MISSING_HASH"]
    status = bridge.call("cad_image_blobs_status", {"hashes": [present, corrupt, absent]}, timeout=30)
    existing = bridge.call("cad_image_blob_import", {"hash": present}, timeout=30)
    invalid = bridge.call("cad_image_blob_import", {"hash": corrupt}, timeout=30)
    rejected = []
    for arguments in ({"hash": "../outside"}, {"hash": absent, "path": str(root / "outside")}):
        try:
            bridge.call("cad_image_blob_import", arguments, timeout=30)
            rejected.append(None)
        except BridgeError as error:
            rejected.append(error.code)
    messages = _json_value(session.client.get_messages())
    print(json.dumps({"messages": messages, "stderr": session.client.stderr,
                      "status": status, "existing": existing, "invalid": invalid, "rejected": rejected}))
finally:
    if session is not None:
        host._release_live_session(session)
    host.shutdown()
'''


def test_official_runtime_cold_resume_hydrates_exact_image(tmp_path):
    configured = os.environ.get("CHUDWARE_OMP_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_TEST_BIN to the digest-pinned official OMP artifact")
    pytest.importorskip("omp_rpc")
    from scripts.pin_omp import host_target, load_lock

    binary = Path(configured).expanduser().resolve(strict=True)
    target = os.environ.get("CHUDWARE_OMP_TEST_TARGET") or host_target()
    with binary.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == load_lock()["artifacts"][target]["sha256"]
    repo = Path(__file__).resolve().parents[2]
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".chudcode" / "agent" / "blobs"
    legacy.mkdir(parents=True)
    agent = tmp_path / "isolated-agent"
    official = agent / "blobs"
    official.mkdir(parents=True)
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nmQsAAAAASUVORK5CYII=")
    present = b"already in official store"
    (legacy / _digest(image)).write_bytes(image)
    (official / _digest(present)).write_bytes(present)
    corrupt = b"expected official bytes"
    missing = b"not in either blob store"
    (official / _digest(corrupt)).write_bytes(b"damaged official bytes")
    journal = sessions / "history.jsonl"
    opaque = {"type": "custom", "id": "opaque", "parentId": "msg", "timestamp": "2026-01-01T00:00:01.000Z",
              "customType": "opaque-test", "data": {"thinkingSignature": "signature-must-remain-exact"}}
    prefix = _journal(journal,
        {"type": "session", "version": 3, "id": "asset-fixture", "timestamp": "2026-01-01T00:00:00.000Z", "cwd": str(tmp_path)},
        {"type": "message", "id": "msg", "parentId": None, "timestamp": "2026-01-01T00:00:00.000Z",
         "message": {"role": "user", "timestamp": 1767225600000, "content": [
             {"type": "image", "mimeType": "image/png", "data": _ref(image)},
             {"type": "image", "mimeType": "image/png", "data": _ref(present)}]}}, opaque)
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL") if key in os.environ}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PI_CODING_AGENT_DIR": str(agent),
                "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"), "PYTHONPATH": str(repo),
                "CHUDWARE_OMP_BIN": str(binary), "OMP_ASSET_TEST_ROOT": str(tmp_path),
                "OMP_ASSET_PRESENT_HASH": _digest(present), "OMP_ASSET_CORRUPT_HASH": _digest(corrupt),
                "OMP_ASSET_MISSING_HASH": _digest(missing),
                "CHUDWARE_PROJECT_DIR": str(tmp_path), "CHUDWARE_PROJECT_SAVE_DIR": str(tmp_path)})
    completed = subprocess.run([sys.executable, "-c", _RUNTIME_RUNNER], cwd=tmp_path,
                               env=env, capture_output=True, text=True, check=False, timeout=150)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(completed.stdout)
    images = [block for message in report["messages"] for block in message.get("content", [])
              if isinstance(block, dict) and block.get("type") == "image"]
    assert [base64.b64decode(block["data"]) for block in images] == [image, present]
    assert (official / _digest(image)).read_bytes() == image
    assert (official / _digest(present)).read_bytes() == present
    assert (official / _digest(corrupt)).read_bytes() == b"damaged official bytes"
    assert Path(report["status"]["root"]) == official
    assert [row["state"] for row in report["status"]["states"]] == ["present", "invalid", "missing"]
    assert report["existing"] == {"hash": _digest(present), "state": "present"}
    assert report["invalid"] == {"hash": _digest(corrupt), "state": "invalid"}
    assert report["rejected"] == ["invalid_request", "invalid_request"]
    assert "Blob not found" not in report["stderr"]
    assert journal.read_bytes().startswith(prefix)


_RACE_FIXTURE = r'''
import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { getBlobsDir } from "@oh-my-pi/pi-utils";
export default function (pi) {
  pi.on("session_start", (_event, ctx) => {
    if (ctx.agent.kind !== "main") return;
    const manager = ctx.sessionManager;
    const put = manager.putBlob.bind(manager);
    manager.putBlob = async (data, options) => {
      const hash = createHash("sha256").update(data).digest("hex");
      await mkdir(getBlobsDir(), { recursive: true });
      // Appear after the control's last missing check but before OMP's put.
      // OMP's same-size reuse accepts this file without hashing it.
      await writeFile(join(getBlobsDir(), hash), Buffer.alloc(data.length), { flag: "wx" });
      return put(data, options);
    };
  });
}
'''

_RACE_RUNNER = r'''
import json
import os
from pathlib import Path
from omp_rpc import RpcClient
from services.agent.omp_host import OmpSessionHost, SessionAssetError

root = Path(os.environ["OMP_ASSET_TEST_ROOT"])
def factory(**kwargs):
    kwargs["extra_args"] = (*kwargs.get("extra_args", ()), "--trusted-extension", str(root / "race.ts"))
    return RpcClient(**kwargs)

host = OmpSessionHost(client_factory=factory, binary=os.environ["CHUDWARE_OMP_BIN"])
try:
    try:
        host._get_or_create_session("synthetic", {"workspace_dir": str(root)}, str(root / "sessions" / "history.jsonl"))
    except SessionAssetError as error:
        print(json.dumps({"error": str(error)}))
    else:
        raise RuntimeError("corrupt raced blob was accepted")
finally:
    host.shutdown()
'''


@pytest.mark.parametrize("historical", [False, True])
def test_official_import_rejects_same_length_destination_race(tmp_path, historical):
    configured = os.environ.get("CHUDWARE_OMP_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_TEST_BIN to the digest-pinned official OMP artifact")
    pytest.importorskip("omp_rpc")
    from scripts.pin_omp import host_target, load_lock

    binary = Path(configured).expanduser().resolve(strict=True)
    target = os.environ.get("CHUDWARE_OMP_TEST_TARGET") or host_target()
    with binary.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == load_lock()["artifacts"][target]["sha256"]
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    legacy = home / ".chudcode" / "agent" / "blobs"
    legacy.mkdir(parents=True)
    agent = tmp_path / "isolated-agent"
    agent.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    image = bytes(range(1, 255))
    (legacy / _digest(image)).write_bytes(image)
    (tmp_path / "race.ts").write_text(_RACE_FIXTURE, encoding="utf-8")
    journal = sessions / "history.jsonl"
    entries = [
        {"type": "session", "version": 3, "id": "race-fixture", "timestamp": "2026-01-01T00:00:00.000Z",
         "cwd": str(tmp_path)},
        {"type": "message", "id": "msg", "parentId": None, "timestamp": "2026-01-01T00:00:00.000Z",
         "message": {"role": "user", "timestamp": 1767225600000,
                     "content": [{"type": "image", "mimeType": "image/png", "data": _ref(image)}]}}]
    if historical:
        entries.extend([
            {"type": "message", "id": "keep", "parentId": "msg", "timestamp": "2026-01-01T00:00:01.000Z",
             "message": {"role": "user", "timestamp": 1767225601000,
                         "content": [{"type": "text", "text": "current request"}]}},
            {"type": "compaction", "id": "compact", "parentId": "keep",
             "timestamp": "2026-01-01T00:00:02.000Z", "summary": "summary",
             "tokensBefore": 100, "firstKeptEntryId": "keep"}])
    prefix = _journal(journal, *entries)
    env = {key: os.environ[key] for key in
           ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL")
           if key in os.environ}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PI_CODING_AGENT_DIR": str(agent),
                "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"), "PYTHONPATH": str(repo),
                "CHUDWARE_OMP_BIN": str(binary), "OMP_ASSET_TEST_ROOT": str(tmp_path),
                "CHUDWARE_PROJECT_DIR": str(tmp_path), "CHUDWARE_PROJECT_SAVE_DIR": str(tmp_path)})
    completed = subprocess.run([sys.executable, "-c", _RACE_RUNNER], cwd=tmp_path,
                               env=env, capture_output=True, text=True, check=False, timeout=150)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "session_asset_error" in json.loads(completed.stdout)["error"]
    installed = (agent / "blobs" / _digest(image)).read_bytes()
    assert len(installed) == len(image) and installed != image
    assert journal.read_bytes() == prefix


_PROJECTION_RUNNER = r'''
import json
import os
from pathlib import Path
from services.agent.omp_bridge import BridgeError
from services.agent.omp_host import OmpSessionHost, SessionAssetError, _start_client, _stop_client, recover_session_image_assets

root = Path(os.environ["OMP_ASSET_TEST_ROOT"])
journal = root / "sessions" / "history.jsonl"
host = OmpSessionHost(binary=os.environ["CHUDWARE_OMP_BIN"])
client = host._make_client({"workspace_dir": str(root), "omp_session_dir": str(journal.parent)},
                           no_session=False, resume_file=str(journal))
try:
    _start_client(client)
    bridge = client._omp_bridge
    try:
        bridge.call("cad_image_context_refs", {"path": str(root / "outside")}, timeout=30)
        rejected = None
    except BridgeError as error:
        rejected = error.code
    output = {}
    for name, entries in json.loads((root / "cases.json").read_text()).items():
        prefix = "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries).encode()
        journal.write_bytes(prefix)
        projection = bridge.call("cad_image_context_refs", {}, timeout=30)
        recovery = None
        if name not in {"reset", "remote", "anthropic", "migration"}:
            try:
                recovery = recover_session_image_assets(client, journal)
            except SessionAssetError as error:
                recovery = {"error": str(error)}
        output[name] = {"projection": projection, "recovery": recovery, "unchanged": journal.read_bytes() == prefix}
    print(json.dumps({"cases": output, "rejected": rejected}))
finally:
    _stop_client(client)
    host.shutdown()
'''


def test_official_projection_uses_branch_compaction_reset_native_and_frame_budget(tmp_path):
    configured = os.environ.get("CHUDWARE_OMP_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_TEST_BIN to the digest-pinned official OMP artifact")
    pytest.importorskip("omp_rpc")
    from scripts.pin_omp import host_target, load_lock

    binary = Path(configured).expanduser().resolve(strict=True)
    target = os.environ.get("CHUDWARE_OMP_TEST_TARGET") or host_target()
    with binary.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == load_lock()["artifacts"][target]["sha256"]
    repo = Path(__file__).resolve().parents[2]
    home, sessions, agent = tmp_path / "home", tmp_path / "sessions", tmp_path / "agent"
    home.mkdir()
    sessions.mkdir()
    official = agent / "blobs"
    official.mkdir(parents=True)
    small_frame, missing_frame, large_frame = b"small frame", b"missing frame", b"x" * 2_250_001
    for data in (small_frame, large_frame):
        (official / _digest(data)).write_bytes(data)
    corrupt_frame, corrupt_large = b"corrupt selected frame", b"z" * 2_250_001
    for data in (corrupt_frame, corrupt_large):
        (official / _digest(data)).write_bytes(b"\x00" * len(data))
    unsafe = b"unsafe historical canonical image"
    (official / _digest(unsafe)).mkdir()
    old, kept, post, off, remote, after = (b"old", b"kept", b"post", b"off", b"remote", b"after")
    timestamp = "2026-01-01T00:00:00.000Z"
    header = {"type": "session", "version": 3, "id": "projection-fixture", "timestamp": timestamp, "cwd": str(tmp_path)}

    def message(identifier, parent, data):
        entry = _branch_image(identifier, parent, _ref(data))
        entry["timestamp"] = timestamp
        entry["message"]["timestamp"] = 1767225600000
        return entry

    def compact(identifier, parent, first, **extra):
        return {"type": "compaction", "id": identifier, "parentId": parent, "timestamp": timestamp,
                "summary": "summary", "tokensBefore": 100, "firstKeptEntryId": first, **extra}

    branch = [header, message("old", None, old), message("keep", "old", kept),
              compact("compact", "keep", "keep"), message("post", "compact", post),
              {"type": "custom", "id": "custom", "parentId": "post", "timestamp": timestamp,
               "customType": "opaque", "data": {"image_url": _ref(off), "signature": "opaque-preserved"}},
              {**message("off", "old", off), "activeLeafId": "custom"}]
    reset = [header, message("old", None, old),
             {"type": "reset_boundary", "id": "reset", "parentId": "old", "timestamp": timestamp},
             message("after", "reset", after)]
    remote_case = [header, message("old", None, old), message("keep", "old", kept),
                   compact("compact", "keep", "keep", providerReplayThroughEntryId="old",
                           preserveData={"openaiRemoteCompaction": {"provider": "openai", "replacementHistory": [
                               {"type": "message", "role": "user", "content": [
                                   {"type": "input_image", "image_url": _ref(remote)}]}]}})]
    anthropic = [header, message("old", None, old), message("keep", "old", kept),
                 compact("compact", "keep", "", providerReplayThroughEntryId="old",
                         preserveData={"anthropicCompaction": {"provider": "anthropic", "content": "opaque-native"}})]

    def frame(data):
        return {"data": _ref(data), "mimeType": "image/png", "cols": 20, "rows": 10, "chars": 10}

    archive = [header, {"type": "message", "id": "text", "parentId": None, "timestamp": timestamp,
                        "message": {"role": "user", "timestamp": 1767225600000, "content": []}},
               compact("compact", "text", "", method="snapcompact", preserveData={"snapcompact": {
                   "frames": [frame(large_frame), frame(missing_frame), frame(small_frame)],
                   "totalChars": 30, "truncatedChars": 0}})]
    v1_header = {**header, "version": 1}
    migration = [v1_header,
                 {"type": "message", "timestamp": timestamp,
                  "message": {"role": "user", "timestamp": 1767225600000,
                              "content": [{"type": "image", "mimeType": "image/png", "data": _ref(old)}]}},
                 {"type": "message", "timestamp": timestamp,
                  "message": {"role": "user", "timestamp": 1767225600000,
                              "content": [{"type": "image", "mimeType": "image/png", "data": _ref(kept)}]}},
                 {"type": "compaction", "timestamp": timestamp, "summary": "summary",
                  "tokensBefore": 100, "firstKeptEntryIndex": 2}]
    malformed = [header, {"type": "message", "id": "bad", "parentId": None, "timestamp": timestamp,
                          "message": {"role": "user", "timestamp": 1767225600000,
                                      "content": [{"type": "image", "mimeType": "image/png",
                                                   "data": "blob:sha256:../../private"}]}}]
    screenshot = b"missing native computer screenshot"
    screenshot_entry = {
        "type": "message", "id": "screenshot", "parentId": "text", "timestamp": timestamp,
        "message": {"role": "toolResult", "toolName": "computer", "toolCallId": "screen",
                    "content": [], "isError": False, "timestamp": 1767225600000,
                    "providerMetadata": {"type": "computer", "screenshot": {
                        "type": "computer_screenshot", "image_url": _ref(screenshot)}}}}
    native_screenshot = [header, archive[1], screenshot_entry]
    remote_screenshot = [
        header, archive[1], compact("compact", "text", "text", providerReplayThroughEntryId="text",
            preserveData={"openaiRemoteCompaction": {"provider": "openai", "replacementHistory": [
                {"type": "computer_call_output", "call_id": "screen",
                 "output": {"type": "computer_screenshot", "image_url": _ref(screenshot)}}]}})]
    unsafe_historical = [
        header, message("old", None, unsafe),
        {**archive[1], "parentId": "old"}, compact("compact", "text", "text")]
    unsafe_custom = [header, {
        "type": "custom", "id": "metadata", "parentId": None, "timestamp": timestamp,
        "customType": "opaque", "data": {"image_url": _ref(unsafe)}}]
    missing_custom = [header, {
        "type": "custom", "id": "metadata", "parentId": None, "timestamp": timestamp,
        "customType": "opaque", "data": {"image_url": _ref(b"missing optional metadata")}}]
    corrupt_selected = [
        header, archive[1], compact("compact", "text", "", method="snapcompact",
            preserveData={"snapcompact": {"frames": [frame(corrupt_frame)],
                                         "totalChars": 10, "truncatedChars": 0}})]
    corrupt_omitted = [
        header, archive[1], compact("compact", "text", "", method="snapcompact",
            preserveData={"snapcompact": {"frames": [frame(corrupt_large), frame(small_frame)],
                                         "totalChars": 20, "truncatedChars": 0}})]
    unsafe_frame = [
        header, archive[1], compact("compact", "text", "", method="snapcompact",
            preserveData={"snapcompact": {"frames": [frame(unsafe)],
                                         "totalChars": 10, "truncatedChars": 0}})]
    cases = {"branch": branch, "reset": reset, "remote": remote_case, "anthropic": anthropic,
             "archive": archive, "migration": migration, "malformed": malformed,
             "native_screenshot": native_screenshot, "remote_screenshot": remote_screenshot,
             "unsafe_historical": unsafe_historical, "unsafe_custom": unsafe_custom,
             "missing_custom": missing_custom, "corrupt_selected": corrupt_selected,
             "corrupt_omitted": corrupt_omitted, "unsafe_frame": unsafe_frame}
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (sessions / "history.jsonl").write_text("", encoding="utf-8")
    env = {key: os.environ[key] for key in
           ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL")
           if key in os.environ}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PI_CODING_AGENT_DIR": str(agent),
                "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"), "PYTHONPATH": str(repo),
                "CHUDWARE_OMP_BIN": str(binary), "OMP_ASSET_TEST_ROOT": str(tmp_path),
                "CHUDWARE_PROJECT_DIR": str(tmp_path), "CHUDWARE_PROJECT_SAVE_DIR": str(tmp_path)})
    completed = subprocess.run([sys.executable, "-c", _PROJECTION_RUNNER], cwd=tmp_path,
                               env=env, capture_output=True, text=True, check=False, timeout=150)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(completed.stdout)
    rows = report["cases"]
    assert report["rejected"] == "invalid_request"
    assert all(row["unchanged"] for row in rows.values())
    assert set(rows["branch"]["projection"]["required"]) == {_digest(kept), _digest(post)}
    assert set(rows["reset"]["projection"]["required"]) == {_digest(after)}
    assert set(rows["remote"]["projection"]["required"]) == {_digest(kept), _digest(remote)}
    assert set(rows["anthropic"]["projection"]["required"]) == {_digest(kept)}
    assert set(rows["migration"]["projection"]["required"]) == {_digest(kept)}
    assert "session_asset_error" in rows["branch"]["recovery"]["error"]
    assert rows["malformed"]["projection"]["malformedRequired"] is True
    assert "invalid image reference" in rows["malformed"]["recovery"]["error"]
    archive_projection = rows["archive"]["projection"]
    assert set(archive_projection["framesSeen"]) == {_digest(small_frame), _digest(missing_frame), _digest(large_frame)}
    assert archive_projection["framesSelected"] == [_digest(small_frame)]
    assert archive_projection["framesUnavailable"] == [_digest(missing_frame)]
    frame_status = rows["archive"]["recovery"]
    assert [item["reference"] for item in frame_status["frame_unavailable"]] == [_ref(missing_frame)]
    assert frame_status["historical_unavailable"] == []
    for name in ("native_screenshot", "remote_screenshot"):
        assert rows[name]["projection"]["required"] == [_digest(screenshot)]
        assert _ref(screenshot) in rows[name]["recovery"]["error"]
    for name in ("unsafe_historical", "unsafe_custom", "corrupt_selected", "unsafe_frame"):
        assert "session_asset_error" in rows[name]["recovery"]["error"]
    assert (official / _digest(unsafe)).is_dir()
    assert (official / _digest(corrupt_frame)).read_bytes() == b"\x00" * len(corrupt_frame)
    assert rows["missing_custom"]["projection"]["required"] == []
    assert "error" not in rows["missing_custom"]["recovery"]
    assert rows["corrupt_omitted"]["projection"]["framesSelected"] == [_digest(small_frame)]
    assert rows["corrupt_omitted"]["recovery"]["frame_unavailable"] == [{
        "reference": _ref(corrupt_large), "reason": "corrupt or unsafe current asset"}]
