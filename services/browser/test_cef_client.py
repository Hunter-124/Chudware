"""Unit tests for services.browser.cef_client's resolution and spawn helpers (no real daemon)."""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from services.browser import cef_client

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX spawn path")


def _script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "chudware"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return script


@posix_only
def test_missing_helper_binary_fails_at_once_and_names_the_path(monkeypatch, tmp_path):
    """2026-10-10 6-axis trace: a missing helper read "CEF daemon spawn timed out
    ([Errno 2] No such file or directory)" after a 30 s wait."""
    missing = tmp_path / "absent" / "chudware"
    monkeypatch.setenv("CHUDWARE_BIN", str(missing))
    monkeypatch.setenv("CHUDWARE_CEF_SOCKET", str(tmp_path / "cefd.sock"))
    started = time.monotonic()
    with pytest.raises(cef_client.CefDaemonError) as caught:
        cef_client.CefDaemon()._connect(budget_s=30.0)
    assert time.monotonic() - started < 5.0
    assert caught.value.reason == "browser_helper_missing"
    text = str(caught.value)
    assert "not installed" in text and str(missing) in text
    assert "timed out" not in text


def test_unresolved_helper_lists_every_path_tried(monkeypatch, tmp_path):
    monkeypatch.delenv("CHUDWARE_BIN", raising=False)
    monkeypatch.delenv("CHUDWARE_DIST", raising=False)
    monkeypatch.delenv("CHUDWARE_CEF_SOCKET", raising=False)
    monkeypatch.setattr(cef_client, "_repo_root", lambda: tmp_path)
    with pytest.raises(cef_client.CefDaemonError) as caught:
        cef_client.CefDaemon()._connect()
    assert caught.value.reason == "browser_helper_missing"
    assert str(tmp_path / "host" / "build-linux" / "bin") in str(caught.value)


@posix_only
def test_helper_that_exits_at_once_is_named_not_timed_out(monkeypatch, tmp_path):
    """A CEF-OFF build refuses --cef-daemon with exit code 2. Report that, with its words."""
    helper = _script(tmp_path, "printf '%s\\n' 'this build has no CEF backend' >&2\nexit 2")
    monkeypatch.setenv("CHUDWARE_BIN", str(helper))
    monkeypatch.setenv("CHUDWARE_CEF_SOCKET", str(tmp_path / "cefd.sock"))
    started = time.monotonic()
    with pytest.raises(cef_client.CefDaemonError) as caught:
        cef_client.CefDaemon()._connect(budget_s=30.0)
    assert time.monotonic() - started < 10.0
    assert caught.value.reason == "browser_helper_exited"
    text = str(caught.value)
    assert "exit code 2" in text and "no CEF backend" in text


@posix_only
def test_spawn_stays_single_flight_while_the_recorded_daemon_lives(tmp_path):
    helper = _script(tmp_path, "while true; do sleep 1; done")
    sock = str(tmp_path / "cefd.sock")
    first = cef_client._spawn_daemon_detached(helper, sock, 5)
    try:
        assert first is not None
        recorded = int(Path(sock).with_suffix(".spawn.lock").read_text())
        assert recorded == first.pid
        assert cef_client._spawn_daemon_detached(helper, sock, 5) is None
    finally:
        os.kill(first.pid, signal.SIGKILL)
        first.wait()


def test_fnv1a64_is_stable_and_hex():
    h = cef_client._fnv1a64_hex("/some/path/Chudware")
    assert h == cef_client._fnv1a64_hex("/some/path/Chudware")  # deterministic
    assert len(h) == 16 and int(h, 16) >= 0                   # 64-bit hex
    assert h != cef_client._fnv1a64_hex("/other/Chudware")      # path-sensitive


def test_resolve_cef_bin_env_override(monkeypatch):
    monkeypatch.setenv("CHUDWARE_BIN", "/custom/Chudware")
    assert cef_client.resolve_cef_bin() == Path("/custom/Chudware")


def test_resolve_cef_bin_finds_canonical_lowercase_build(monkeypatch, tmp_path):
    monkeypatch.delenv("CHUDWARE_BIN", raising=False)
    monkeypatch.delenv("CHUDWARE_DIST", raising=False)
    monkeypatch.setattr(cef_client, "_repo_root", lambda: tmp_path)
    exe = "chudware.exe" if sys.platform == "win32" else "chudware"
    built = tmp_path / "host" / "build-linux" / "bin" / exe
    built.parent.mkdir(parents=True)
    built.touch()

    assert cef_client.resolve_cef_bin() == built


def test_socket_path_env_override_wins(monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_SOCKET", "/tmp/explicit.sock")
    assert cef_client.socket_path(Path("/whatever/Chudware")) == "/tmp/explicit.sock"


def test_socket_path_keyed_by_binary(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CEF_SOCKET", raising=False)
    monkeypatch.delenv("CHUDWARE_CEF_KEY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    sp = cef_client.socket_path(Path("/a/Chudware"))
    assert sp is not None and sp.startswith("/run/user/1000/chudware/cefd-") and sp.endswith(".sock")
    # Different binaries → different sockets (two checkouts don't collide).
    assert sp != cef_client.socket_path(Path("/b/Chudware"))


def test_socket_path_none_without_binary(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CEF_SOCKET", raising=False)
    assert cef_client.socket_path(None) is None


def test_client_singleton():
    assert cef_client.client() is cef_client.client()
