"""Unit tests for services.browser.cef_client's pure resolution helpers (no daemon)."""
from __future__ import annotations

from pathlib import Path

from services.browser import cef_client


def test_fnv1a64_is_stable_and_hex():
    h = cef_client._fnv1a64_hex("/some/path/AI-CAD")
    assert h == cef_client._fnv1a64_hex("/some/path/AI-CAD")  # deterministic
    assert len(h) == 16 and int(h, 16) >= 0                   # 64-bit hex
    assert h != cef_client._fnv1a64_hex("/other/AI-CAD")      # path-sensitive


def test_resolve_cef_bin_env_override(monkeypatch):
    monkeypatch.setenv("AI_CAD_BIN", "/custom/AI-CAD")
    assert cef_client.resolve_cef_bin() == Path("/custom/AI-CAD")


def test_socket_path_env_override_wins(monkeypatch):
    monkeypatch.setenv("AICAD_CEF_SOCKET", "/tmp/explicit.sock")
    assert cef_client.socket_path(Path("/whatever/AI-CAD")) == "/tmp/explicit.sock"


def test_socket_path_keyed_by_binary(monkeypatch):
    monkeypatch.delenv("AICAD_CEF_SOCKET", raising=False)
    monkeypatch.delenv("AICAD_CEF_KEY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    sp = cef_client.socket_path(Path("/a/AI-CAD"))
    assert sp is not None and sp.startswith("/run/user/1000/aicad/cefd-") and sp.endswith(".sock")
    # Different binaries → different sockets (two checkouts don't collide).
    assert sp != cef_client.socket_path(Path("/b/AI-CAD"))


def test_socket_path_none_without_binary(monkeypatch):
    monkeypatch.delenv("AICAD_CEF_SOCKET", raising=False)
    assert cef_client.socket_path(None) is None


def test_client_singleton():
    assert cef_client.client() is cef_client.client()
