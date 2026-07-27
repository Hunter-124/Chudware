"""office_oxide preferred extract path (contracts/office.md + T-office)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from services.mcp import office_tools as ot


def test_extract_office_unsupported_kind():
    out = ot.extract_office("/tmp/nope.xyz")
    assert out["ok"] is False
    assert out["reason"] == "unsupported_kind"


def test_oxide_path_used_for_docx(tmp_path, monkeypatch):
    docx = tmp_path / "note.docx"
    docx.write_bytes(b"PK\x03\x04fake")  # not a real docx; oxide call is mocked

    def _fake_oxide(path: str, kind: str):
        assert kind == "docx"
        assert path == str(docx)
        return {"ok": True, "kind": "docx", "text": "hello from oxide", "engine": "office_oxide"}

    monkeypatch.setattr(ot, "_extract_via_office_oxide", _fake_oxide)
    out = ot.extract_office(str(docx))
    assert out["ok"] is True
    assert out["text"] == "hello from oxide"
    assert out.get("engine") == "office_oxide"


def test_oxide_missing_falls_back(monkeypatch, tmp_path):
    docx = tmp_path / "note.docx"
    docx.write_bytes(b"not-real")

    monkeypatch.setattr(ot, "_extract_via_office_oxide", lambda *_a, **_k: None)

    def _boom(_path):
        return {"ok": False, "reason": "open_failed", "hint": "fallback ran"}

    monkeypatch.setattr(ot, "_extract_docx", _boom)
    out = ot.extract_office(str(docx))
    assert out["ok"] is False
    assert out["reason"] == "open_failed"


def test_extract_via_office_oxide_import_error(monkeypatch):
    # Simulate missing wheel — must return None (not raise).
    import builtins

    real_import = builtins.__import__

    def _no_oxide(name, *args, **kwargs):
        if name == "office_oxide" or name.startswith("office_oxide."):
            raise ImportError("no office_oxide")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_oxide)
    assert ot._extract_via_office_oxide("/tmp/x.docx", "docx") is None
