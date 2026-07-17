"""2D — agent working-directory roster (services/agent/config.list_workspace_files).

  * recursive discovery, CAD/doc extensions first
  * dot-directories skipped (.plm / .aicadvault / .git)
  * limit truncation with a "… (+N more)" tail
"""
from __future__ import annotations

from pathlib import Path

from services.agent import config as agent_config


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


def test_workspace_roster_ordering_and_recursion(tmp_path: Path):
    _touch(tmp_path / "readme.txt")
    _touch(tmp_path / "sub" / "Bracket.step+")
    _touch(tmp_path / "notes.md")
    _touch(tmp_path / "deep" / "nested" / "Plate.stp")
    out = agent_config.list_workspace_files(tmp_path)
    # CAD/doc extensions come before the plain .txt
    assert out.index("sub/Bracket.step+") < out.index("readme.txt")
    assert out.index("deep/nested/Plate.stp") < out.index("readme.txt")
    assert "notes.md" in out
    assert "readme.txt" in out


def test_workspace_roster_skips_dot_dirs(tmp_path: Path):
    _touch(tmp_path / "Part.step+")
    _touch(tmp_path / ".plm" / "graph.json")
    _touch(tmp_path / ".aicadvault" / "objects" / "x")
    _touch(tmp_path / ".git" / "config")
    out = agent_config.list_workspace_files(tmp_path)
    assert out == ["Part.step+"]


def test_workspace_roster_limit_truncation(tmp_path: Path):
    for i in range(10):
        _touch(tmp_path / f"f{i:02d}.txt")
    out = agent_config.list_workspace_files(tmp_path, limit=4)
    assert len(out) == 5  # 4 files + the "more" tail
    assert out[-1] == "… (+6 more)"


def test_workspace_roster_missing_dir(tmp_path: Path):
    assert agent_config.list_workspace_files(tmp_path / "nope") == []
