"""Renders the model looks at must reach the USER.

Every render tool used to write its PNG with ``cad_common._tmp``, i.e.
``tempfile.mkstemp(prefix="chudware_mcp_")``.  ``services/agent/agent_events.py``
::``_is_internal_scratch`` deliberately drops any artifact whose basename carries that
prefix, so ``derive_artifacts`` returned ``[]`` and the ``agent.tool_call`` event carried
no ``artifacts`` field at all — observed live on run-138660919763973, where ``render_part``
reported ``artifacts: None``.  The GUI's inline-image path and Artifacts panel read that
field, so the user never saw what the vision model saw.

These tests pin the round trip: a render path from ``render_output_path`` survives
``_is_internal_scratch`` and comes back out of ``derive_artifacts`` as an ``image``, while a
``_tmp`` path is still suppressed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import services.paths as paths
from services.mcp import cad_common


class _FakeImage:
    """The shape ``derive_artifacts`` handles for render returns: an object with ``.path``
    (fastmcp's ``Image``). Faked so the test needs neither PIL nor the engine."""

    def __init__(self, path: str) -> None:
        self.path = path


@pytest.fixture()
def cache(tmp_path, monkeypatch) -> Path:
    """Point ``services.paths.CACHE`` at a throwaway dir.

    ``render_output_path`` reads ``paths.CACHE`` as an ATTRIBUTE on every call (same
    contract conftest relies on for ``paths.CONFIG``), so rebinding it here is sufficient
    and nothing lands in the user's real cache.

    The opportunistic once-per-process prune is also pinned SHUT here, so a render path
    minted by one test cannot silently reap another test's fixture files (which would make
    the prune assertions order-dependent). ``test_render_output_path_prunes_once`` opens it
    back up deliberately.
    """
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(cad_common, "_RENDER_PRUNE_DONE", True)
    return tmp_path / "cache"


def test_render_output_path_is_under_cache_renders_and_unique(cache):
    a = cad_common.render_output_path("mudbucket-iso", ".png")
    b = cad_common.render_output_path("mudbucket-iso", ".png")

    day = time.strftime("%Y-%m-%d")
    assert os.path.isabs(a)
    assert Path(a).parent == cache / "renders" / day
    assert Path(a).parent.is_dir()
    assert not os.path.basename(a).startswith("chudware_mcp_")
    assert os.path.basename(a).startswith("mudbucket-iso-")
    assert a.endswith(".png")
    # Two renders of the same view must not clobber each other.
    assert a != b


def test_stem_is_sanitised(cache):
    dirty = cad_common.render_output_path("../Projects/My Part \u00b5 v2/iso", ".png")
    base = os.path.basename(dirty)
    assert "/" not in base and "\\" not in base
    assert " " not in base
    assert all(c.isascii() and (c.isalnum() or c in "._-") for c in base), base
    assert Path(dirty).parent == cache / "renders" / time.strftime("%Y-%m-%d")

    assert os.path.basename(cad_common.render_output_path("", ".png")).startswith("render-")
    assert os.path.basename(cad_common.render_output_path("///", ".png")).startswith("render-")

    long_stem = "x" * 200
    assert len(os.path.basename(cad_common.render_output_path(long_stem, ".png"))) <= 60 + 1 + 8 + 4


def test_render_path_survives_the_scratch_filter_that_ate_renders(cache):
    """THE regression: _tmp was invisible to the GUI, render_output_path must not be."""
    from services.agent import agent_events

    render = cad_common.render_output_path("mudbucket", ".png")
    scratch = cad_common._tmp(".png")
    try:
        assert agent_events._is_internal_scratch(render) is False
        assert agent_events._is_internal_scratch(scratch) is True

        arts = agent_events.derive_artifacts([_FakeImage(render)])
        assert arts == [{"path": render, "kind": "image"}]
        # ... and the old path really did yield nothing, which is why renders never showed.
        assert agent_events.derive_artifacts([_FakeImage(scratch)]) == []
    finally:
        os.unlink(scratch)


def test_prune_render_cache_drops_stale_keeps_fresh(cache):
    day = cache / "renders" / "2026-01-01"
    day.mkdir(parents=True)
    stale = day / "old-render.png"
    stale.write_bytes(b"old")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    fresh = Path(cad_common.render_output_path("fresh", ".png"))
    fresh.write_bytes(b"new")

    assert cad_common.prune_render_cache() == 1
    assert not stale.exists()
    assert fresh.exists()
    # Emptied day directories are collected too.
    assert not day.exists()


def test_render_output_path_prunes_once(cache, monkeypatch):
    """The cache cannot grow without bound: minting a render path prunes, but only once
    per process (no rglob of the whole cache on every single render)."""
    monkeypatch.setattr(cad_common, "_RENDER_PRUNE_DONE", False)
    day = cache / "renders" / "2026-01-01"
    day.mkdir(parents=True)
    stale = day / "old-render.png"
    stale.write_bytes(b"old")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))

    cad_common.render_output_path("first", ".png")
    assert not stale.exists()
    assert cad_common._RENDER_PRUNE_DONE is True

    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"old again")
    os.utime(stale, (old, old))
    cad_common.render_output_path("second", ".png")
    assert stale.exists()  # flag latched — no second sweep



def test_prune_render_cache_over_file_cap(cache):
    for i in range(5):
        p = Path(cad_common.render_output_path(f"r{i}", ".png"))
        p.write_bytes(b"x")
        os.utime(p, (time.time() - (10 - i), time.time() - (10 - i)))

    assert cad_common.prune_render_cache(max_files=2) == 3
    surviving = sorted(p.name for p in (cache / "renders").rglob("*.png"))
    assert len(surviving) == 2
    # Oldest-first: r0..r2 go, the two newest stay.
    assert all(n.startswith(("r3-", "r4-")) for n in surviving), surviving


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory write permissions")
def test_prune_render_cache_never_raises(cache):
    """An absent (or unreadable) render dir returns 0 instead of breaking a render."""
    assert not (cache / "renders").exists()
    assert cad_common.prune_render_cache() == 0

    (cache / "renders").mkdir(parents=True)
    locked = cache / "renders" / "2026-01-02"
    locked.mkdir()
    victim = locked / "old.png"
    victim.write_bytes(b"x")
    old = time.time() - 30 * 86400
    os.utime(victim, (old, old))
    locked.chmod(0o500)  # unlink denied — must be swallowed, not raised
    try:
        assert cad_common.prune_render_cache() == 0
        assert victim.exists()
    finally:
        locked.chmod(0o700)
