"""Deterministic offline tests for tools.provision_renode (no network).

Monkeypatches PATH / known roots / GitHub fetch so present/absent cases are
reproducible. Prints wall-times (no expected-time gating).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# Ensure repo root is importable when pytest is launched from elsewhere.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools import provision_renode as pr  # noqa: E402


@pytest.fixture
def clean_path(monkeypatch, tmp_path):
    """Strip renode from PATH and known roots; isolate HOME/LOCALAPPDATA."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("AICAD_RENODE", raising=False)
    monkeypatch.delenv("RENODE_ROOT", raising=False)
    # Empty PATH so shutil.which cannot find a real renode.
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))
    (tmp_path / "empty_bin").mkdir()
    (tmp_path / "home").mkdir()
    # Rebuild known-root constants against the temp HOME.
    monkeypatch.setattr(
        pr, "_KNOWN_ROOTS_POSIX",
        (
            Path(tmp_path / "home") / ".local" / "opt" / "renode",
            Path(tmp_path / "home") / ".local" / "share" / "AI-CAD" / "renode",
        ),
    )
    monkeypatch.setattr(
        pr, "_KNOWN_ROOTS_WIN",
        (
            Path(tmp_path / "local") / "Programs" / "renode",
        ),
    )
    yield tmp_path

def test_report_absent_exits_1(clean_path, capsys):
    t0 = time.perf_counter()
    rc = pr.report()
    dt = time.perf_counter() - t0
    assert rc == 1
    out = capsys.readouterr().out
    assert "No Renode detected" in out
    print(f"WALL_TIME test_report_absent_exits_1={dt:.4f}s", file=sys.stderr)


def test_status_absent(clean_path):
    t0 = time.perf_counter()
    st = pr.status()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_status_absent={dt:.4f}s", file=sys.stderr)
    assert st["tool"] == "renode"
    assert st["installed"] is False
    assert st["path"] is None
    assert st["hint"]


def test_report_present_via_path(clean_path, monkeypatch, capsys):
    bin_dir = clean_path / "empty_bin"
    fake = bin_dir / ("renode.exe" if os.name == "nt" else "renode")
    fake.write_text("#!/bin/sh\necho 'Renode v9.9.9-test'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    def _fake_version(binary):
        return "Renode v9.9.9-test"

    monkeypatch.setattr(pr, "probe_version", _fake_version)

    t0 = time.perf_counter()
    rc = pr.report()
    dt = time.perf_counter() - t0
    assert rc == 0
    out = capsys.readouterr().out
    assert "Renode detected" in out
    assert "v9.9.9-test" in out
    print(f"WALL_TIME test_report_present_via_path={dt:.4f}s", file=sys.stderr)


def test_status_present_via_env_root(clean_path, monkeypatch):
    root = clean_path / "custom_renode"
    root.mkdir()
    binary = root / ("renode.exe" if os.name == "nt" else "renode")
    binary.write_text("x", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("AICAD_RENODE", str(root))
    monkeypatch.setattr(pr, "probe_version", lambda _b: "Renode v1.0-env")

    t0 = time.perf_counter()
    st = pr.status()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_status_present_via_env_root={dt:.4f}s", file=sys.stderr)
    assert st["installed"] is True
    assert Path(st["path"]) == binary
    assert st["version"] == "Renode v1.0-env"
    assert st["hint"] is None


def test_main_json_absent(clean_path, capsys):
    t0 = time.perf_counter()
    rc = pr.main(["--json"])
    dt = time.perf_counter() - t0
    assert rc == 1
    out = capsys.readouterr().out
    assert '"installed": false' in out or '"installed":false' in out.replace(" ", "")
    print(f"WALL_TIME test_main_json_absent={dt:.4f}s", file=sys.stderr)


def test_install_idempotent_when_present(clean_path, monkeypatch, capsys):
    bin_dir = clean_path / "empty_bin"
    fake = bin_dir / ("renode.exe" if os.name == "nt" else "renode")
    fake.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(pr, "probe_version", lambda _b: "Renode v9.9.9")

    called = {"n": 0}

    def _no_network(*_a, **_k):
        called["n"] += 1
        raise AssertionError("must not hit network when already present")

    monkeypatch.setattr(pr, "_latest_release_asset", _no_network)

    t0 = time.perf_counter()
    ok = pr.install()
    dt = time.perf_counter() - t0
    assert ok is True
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert "already available" in out
    print(f"WALL_TIME test_install_idempotent_when_present={dt:.4f}s", file=sys.stderr)
