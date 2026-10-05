"""Reader framing, errors and borrowed lifetime; no CAD/native runtime required.

Run: python -m pytest -q services/test_aicm_handoff.py
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote
import uuid

import pytest

from services import aicm_handoff as handoff


def _frame(payload: bytes, padding: bytes = b"") -> bytes:
    return len(payload).to_bytes(8, "little") + payload + padding


@pytest.fixture
def posix_object():
    if os.name != "posix":
        pytest.skip("POSIX shared-memory objects require a POSIX kernel")
    from multiprocessing import shared_memory

    owned = []

    def publish(raw: bytes):
        shm = shared_memory.SharedMemory(create=True, size=len(raw))
        owned.append(shm)
        shm.buf[:] = raw
        return shm, handoff.SHM_URI_PREFIX + "/" + shm.name

    try:
        yield publish
    finally:
        for shm in owned:
            shm.close()
            shm.unlink()


@pytest.mark.parametrize("payload", [b"", b"AICM\x00binary\xff", bytes(range(256))])
def test_posix_payload_length_and_padding(posix_object, payload):
    _, uri = posix_object(_frame(payload, b"not part of payload"))
    assert handoff.read_handoff(uri) == payload
    assert handoff.read_handoff(handoff.SHM_URI_PREFIX + quote(uri[len(handoff.SHM_URI_PREFIX):], safe="")) == payload


@pytest.mark.parametrize("raw", [b"x", b"x" * 7])
def test_short_posix_object(posix_object, raw):
    _, uri = posix_object(raw)
    with pytest.raises(ValueError):
        handoff.read_handoff(uri)


@pytest.mark.parametrize("declared", [4, (1 << 64) - 1])
def test_declared_payload_exceeds_object(posix_object, declared):
    _, uri = posix_object(declared.to_bytes(8, "little") + b"abc")
    with pytest.raises(ValueError):
        handoff.read_handoff(uri)


def test_missing_posix_object_preserves_errno():
    if os.name != "posix":
        pytest.skip("POSIX shared-memory objects require a POSIX kernel")
    name = "/cwtest" + uuid.uuid4().hex[:16]
    with pytest.raises(FileNotFoundError) as exc:
        handoff.read_handoff(handoff.SHM_URI_PREFIX + name)
    assert exc.value.errno == errno.ENOENT
    assert exc.value.filename == name




def test_standalone_consumer_exit_does_not_unlink(posix_object):
    payload = b"AICM\x00publisher survives reader exit"
    owner, uri = posix_object(_frame(payload))
    consumer = subprocess.run(
        [sys.executable, "-c",
         "import sys; from services.aicm_handoff import read_handoff; "
         "sys.stdout.buffer.write(read_handoff(sys.argv[1]))", uri],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, check=True, timeout=15,
    )
    assert consumer.stdout == payload
    assert consumer.stderr == b""
    assert bytes(owner.buf[8:]) == payload
    # This reopens the object by name, not merely the publisher's still-mapped view.
    assert handoff.read_handoff(uri) == payload


def test_fallback_file_is_raw_payload_and_literal_path(tmp_path):
    path = tmp_path / "fallback%5C.aicm"
    payload = b"AICM\x00raw file, without shm framing"
    path.write_bytes(payload)
    assert handoff.read_handoff(str(path)) == payload
    with pytest.raises(FileNotFoundError):
        handoff.read_handoff(str(tmp_path / "missing.aicm"))


@pytest.mark.parametrize("uri", ["", "aicm-shm://", "aicm-shm:///", "aicm-shm://%2F",
                                  "aicm-shm://name%00suffix", "aicm-shm://name\0suffix"])
def test_empty_or_nul_uri_is_rejected(uri):
    with pytest.raises(ValueError):
        handoff.read_handoff(uri)


@pytest.mark.skipif(os.name != "nt", reason="Windows named maps require a Windows kernel")
def test_windows_mapping_literal_and_encoded_name():
    import mmap

    name = "Local\\cwtest" + uuid.uuid4().hex
    payload = b"AICM\x00Windows mapping"
    raw = _frame(payload)
    with mmap.mmap(-1, len(raw), tagname=name, access=mmap.ACCESS_WRITE) as owner:
        owner[:] = raw
        assert handoff.read_handoff(handoff.SHM_URI_PREFIX + name) == payload
        assert handoff.read_handoff(handoff.SHM_URI_PREFIX + quote(name, safe="")) == payload
        owner[8:] = b"B" * len(payload)
        assert handoff.read_handoff(handoff.SHM_URI_PREFIX + name) == b"B" * len(payload)


@pytest.mark.skipif(os.name != "nt", reason="Windows named maps require a Windows kernel")
def test_missing_windows_mapping_is_not_created():
    name = "Local\\cwtest" + uuid.uuid4().hex
    for _ in range(2):
        with pytest.raises(OSError) as exc:
            handoff.read_handoff(handoff.SHM_URI_PREFIX + quote(name, safe=""))
        assert exc.value.winerror == 2  # ERROR_FILE_NOT_FOUND
