"""Unit tests for services.browser.cef_client's pure resolution helpers (no daemon)."""
from __future__ import annotations

import sys
from pathlib import Path

from services.browser import cef_client


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
