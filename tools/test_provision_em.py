"""Deterministic offline tests for tools.provision_em (no network / no build).

Monkeypatches the import probe and candidate interpreter list so present/absent
cases are reproducible. Prints wall-times (no expected-time gating).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools import provision_em as pe  # noqa: E402


@pytest.fixture
def isolate(monkeypatch, tmp_path):
    """Redirect prefix/src and neuter real import probes by default."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CHUDWARE_OPENEMS", str(tmp_path / "openEMS"))
    monkeypatch.delenv("CHUDWARE_PYTHON", raising=False)
    monkeypatch.delenv("CHUDWARE_PYTHON", raising=False)
    (tmp_path / "home").mkdir()
    yield tmp_path


def test_status_absent(isolate, monkeypatch):
    monkeypatch.setattr(pe, "_candidate_pythons", lambda: [Path("/no/such/python")])
    monkeypatch.setattr(
        pe, "_import_probe",
        lambda py: (False, None, f"interpreter missing: {py}"),
    )
    t0 = time.perf_counter()
    st = pe.status()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_status_absent={dt:.4f}s")
    assert st["tool"] == "openems"
    assert st["installed"] is False
    assert st["hint"]
    assert "PyPI" in st["hint"] or "build" in st["hint"].lower()


def test_report_absent_exits_1(isolate, monkeypatch, capsys):
    monkeypatch.setattr(pe, "_candidate_pythons", lambda: [])
    t0 = time.perf_counter()
    rc = pe.report()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_report_absent_exits_1={dt:.4f}s")
    assert rc == 1
    out = capsys.readouterr().out
    assert "No openEMS" in out


def test_report_present_exits_0(isolate, monkeypatch, capsys):
    fake_py = isolate / "venv" / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_py.chmod(0o755)

    monkeypatch.setattr(pe, "_candidate_pythons", lambda: [fake_py])
    monkeypatch.setattr(
        pe, "_import_probe",
        lambda py: (True, "0.37.0-test", "openEMS + CSXCAD python modules importable"),
    )
    t0 = time.perf_counter()
    rc = pe.report()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_report_present_exits_0={dt:.4f}s")
    assert rc == 0
    out = capsys.readouterr().out
    assert "openEMS detected" in out
    assert "0.37.0-test" in out


def test_status_present(isolate, monkeypatch):
    fake_py = isolate / "py"
    fake_py.write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "_candidate_pythons", lambda: [fake_py])
    monkeypatch.setattr(
        pe, "_import_probe",
        lambda py: (True, "1.2.3", "openEMS + CSXCAD python modules importable"),
    )
    t0 = time.perf_counter()
    st = pe.find_openems()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_status_present={dt:.4f}s")
    assert st["installed"] is True
    assert st["python"] == str(fake_py)
    assert st["version"] == "1.2.3"
    assert st["hint"] is None


def test_main_json_absent(isolate, monkeypatch, capsys):
    monkeypatch.setattr(pe, "_candidate_pythons", lambda: [])
    t0 = time.perf_counter()
    rc = pe.main(["--json"])
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_main_json_absent={dt:.4f}s")
    assert rc == 1
    out = capsys.readouterr().out
    assert "installed" in out


def test_install_idempotent_when_present(isolate, monkeypatch, capsys):
    fake_py = isolate / "py"
    fake_py.write_text("x", encoding="utf-8")
    monkeypatch.setattr(pe, "_candidate_pythons", lambda: [fake_py])
    monkeypatch.setattr(
        pe, "_import_probe",
        lambda py: (True, "0.1", "ok"),
    )
    called = {"n": 0}

    def _no_build():
        called["n"] += 1
        raise AssertionError("must not rebuild when already present")

    monkeypatch.setattr(pe, "install_linux", _no_build)
    monkeypatch.setattr(pe, "install_windows", _no_build)

    t0 = time.perf_counter()
    rc = pe.main(["--install"])
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_install_idempotent_when_present={dt:.4f}s")
    assert rc == 0
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert "already available" in out

def test_windows_install_prints_recipe(isolate, monkeypatch, capsys):
    monkeypatch.setattr(pe, "_candidate_pythons", lambda: [])
    # find_openems returns absent because candidates empty / probe not reached.
    t0 = time.perf_counter()
    ok = pe.install_windows()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_windows_install_prints_recipe={dt:.4f}s")
    assert ok is False
    out = capsys.readouterr().out
    assert "Windows" in out
    assert "docs.openems.de" in out or "MSVC" in out
