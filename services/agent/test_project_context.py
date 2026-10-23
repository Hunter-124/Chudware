"""test_project_context.py — project-folder awareness block (2026-07-27).

Contracts defended:

- ``project_context_block`` returns "" for None / missing / not-a-directory
  roots and NEVER raises, even when ``os.scandir`` blows up mid-walk.
- Existing CAD files are listed newest-first with size + age, and the header
  tells the model these files ALREADY EXIST and not to duplicate them.
- ``max_files`` truncation is reported, and the block stays bounded.
- The block actually reaches the system prompt of a real ``run_turn`` call
  (scripted provider), via env CHUDWARE_PROJECT_DIR — and stays out when no
  project root is known.
- ``resolve_project_root`` climbs to the OUTERMOST real project marker, but
  NEVER escapes into a system-wide directory (temp dir / $HOME / fs root)
  just because a stray marker was dropped there.
"""
from __future__ import annotations

import os
import time

import pytest


class _ScriptedProvider:
    """Queue of canned replies; records the system prompt per completion."""

    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.seen_system: list[str] = []

    def complete(self, system, messages, max_tokens=8000):
        self.seen_system.append(str(system))
        return self._replies.pop(0) if self._replies else "no more"


# ---------------------------------------------------------------------------
# project_context_block unit contracts
# ---------------------------------------------------------------------------


def test_none_and_missing_roots_return_empty(tmp_path):
    from services.agent.project_context import project_context_block

    assert project_context_block(None) == ""
    assert project_context_block("") == ""
    assert project_context_block(tmp_path / "does-not-exist") == ""
    # A file, not a directory.
    f = tmp_path / "a.step"
    f.write_text("x")
    assert project_context_block(f) == ""


def test_lists_cad_files_newest_first_with_size_and_age(tmp_path):
    from services.agent.project_context import project_context_block

    old = tmp_path / "old_part.step+"
    old.write_bytes(b"x" * 2048)
    new = tmp_path / "new_part.step+"
    new.write_bytes(b"y" * 4096)
    now = time.time()
    os.utime(old, (now - 3 * 86400, now - 3 * 86400))
    os.utime(new, (now - 2 * 3600, now - 2 * 3600))
    # Ignored: non-CAD extension, dot-dir internals, .plm internals.
    (tmp_path / "notes.txt").write_text("nope")
    (tmp_path / ".plm").mkdir()
    (tmp_path / ".plm" / "hidden.step+").write_bytes(b"z" * 1024)

    blk = project_context_block(tmp_path)
    assert blk, "expected a non-empty block"
    assert str(tmp_path) in blk
    assert "already exist" in blk.lower()
    assert "import_part" in blk
    # Both branches must be present: a bare prohibition deadlocked k3 live
    # (it deliberated "import or build?" instead of acting).
    assert "create the new part" in blk
    assert "act" in blk.lower()
    assert "new_part.step+" in blk and "old_part.step+" in blk
    assert blk.index("new_part.step+") < blk.index("old_part.step+"), blk
    assert "4 KB" in blk and "2 KB" in blk
    assert "2h ago" in blk and "3d ago" in blk
    assert "notes.txt" not in blk and "hidden.step+" not in blk
    assert len(blk) < 1600, len(blk)


def test_max_files_truncation_reported(tmp_path):
    from services.agent.project_context import project_context_block

    now = time.time()
    for i in range(5):
        p = tmp_path / f"part{i}.step+"
        p.write_bytes(b"x" * 512)
        os.utime(p, (now - i, now - i))
    blk = project_context_block(tmp_path, max_files=2)
    assert "part0.step+" in blk and "part1.step+" in blk
    assert "part2.step+" not in blk
    assert "3 more" in blk, blk


def test_scandir_failure_does_not_propagate(tmp_path, monkeypatch):
    import os as _os

    from services.agent import project_context as pc

    def _boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(_os, "scandir", _boom)
    assert pc.project_context_block(tmp_path) == ""


# ---------------------------------------------------------------------------
# End-to-end: the block reaches the turn's system prompt
# ---------------------------------------------------------------------------


def test_block_reaches_run_turn_system_prompt(tmp_path, monkeypatch):
    from services.agent import agent_turn as at

    part = tmp_path / "existing_widget.step+"
    part.write_bytes(b"x" * 1024)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))

    p = _ScriptedProvider(["I will look at the existing files first."])
    at.run_turn(
        "make me a widget",
        p,
        allowed_tools=["ledger_get", "ledger_note"],
        max_tool_calls=4,
        tool_dispatch=lambda t, a: {"ok": True},
        actor="general_agent",
        run_id="t-pctx-1",
    )
    sys0 = p.seen_system[0]
    assert "[PROJECT FILES]" in sys0
    assert "existing_widget.step+" in sys0
    assert str(tmp_path) in sys0


def test_no_project_root_no_block(monkeypatch):
    from services.agent import agent_turn as at

    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    # Neutralise the config fallback: no workspace dir known.
    from services.agent import config as _cfg
    monkeypatch.setattr(_cfg, "workspace_dir", lambda *a, **k: None)

    p = _ScriptedProvider(["hello"])
    at.run_turn(
        "hi",
        p,
        allowed_tools=["ledger_get"],
        max_tool_calls=2,
        tool_dispatch=lambda t, a: {"ok": True},
        actor="general_agent",
        run_id="t-pctx-2",
    )
    assert "[PROJECT FILES]" not in p.seen_system[0]


# ---------------------------------------------------------------------------
# resolve_project_root: outermost real project, never a system-wide directory
# ---------------------------------------------------------------------------


def test_climbs_to_outermost_nested_project_marker(tmp_path):
    """Markers nest: a folder opened as a PLM project keeps its marker forever, so
    the document's own folder is usually NOT the project. The outer one wins."""
    from services.agent.project_context import resolve_project_root

    project = tmp_path / "489-final"
    inner = project / "CAD & Design Geometry"
    (project / ".plm").mkdir(parents=True)
    (inner / ".plm").mkdir(parents=True)

    assert resolve_project_root(inner) == project
    assert resolve_project_root(project) == project


def test_stray_marker_in_temp_dir_never_widens_the_jail(tmp_path, monkeypatch):
    """A marker dropped in the SYSTEM TEMP DIR must not make every temp path
    resolve to it — that jail would cover every other process's scratch space.
    Regression: one stray .aicadvault in /tmp resolved /tmp/<x>/489-final to /tmp."""
    import tempfile

    from services.agent.project_context import resolve_project_root

    fake_tmp = tmp_path / "systmp"
    project = fake_tmp / "run-abc" / "489-final"
    (fake_tmp / ".aicadvault").mkdir(parents=True)   # the stray marker
    (project / ".plm").mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmp))

    # The real project still resolves to itself, not up to the temp dir.
    assert resolve_project_root(project) == project
    # And an unmarked temp path does not get adopted by the stray marker either.
    loose = fake_tmp / "run-def" / "parts"
    loose.mkdir(parents=True)
    assert resolve_project_root(loose) == loose


def test_marker_at_home_or_fs_root_is_ignored(tmp_path, monkeypatch):
    """$HOME (and the filesystem root) are never a project — jailing there is the
    same as no jail at all."""
    import pathlib

    from services.agent import project_context as pc

    home = tmp_path / "home" / "user"
    (home / ".plm").mkdir(parents=True)
    work = home / "work" / "widget"
    work.mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))

    assert pc.resolve_project_root(work) == work
    assert pc.resolve_project_root(pathlib.Path("/")) == pathlib.Path("/")
