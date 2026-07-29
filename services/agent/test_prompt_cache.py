"""Tests for the precomposed system prompt cache.

The contract that matters: the cached/precomposed prompt must be the bytes a
LIVE turn actually sends. A number derived from a hand-maintained mirror of the
composition is worse than no number, because it looks authoritative and is
wrong — that is exactly what ``prompt_preview.preview_main_chat_system`` did
(it injects a SAMPLE ledger/stage no live turn sends). So the central test here
captures the system prompt a real ``run_turn`` hands its provider and asserts
byte-equality against the composition, rather than asserting the composition is
merely self-consistent.
"""
from __future__ import annotations

import json

import pytest

from services.agent import agent_turn as at
from services.agent import prompt_cache as pc

ROLE_SYS = "ROLE CARD TEXT\n"          # trailing newline: the join's tricky case
TOOLS = "TOOLS BLOCK\n- t1\n- t2"
ALLOW = ["alpha", "beta"]


# --------------------------------------------------------------------------- helpers

class CapturingProvider:
    """Records the system prompt it is handed, then finishes the turn."""

    name = "capture"
    model = "capture-1"
    context_window = 1000000

    def __init__(self) -> None:
        self.systems: list[str] = []

    def complete(self, system, messages, **kw):  # noqa: ANN001, ANN003
        self.systems.append(system)
        return '```json\n{"done": true, "handoff": {"summary": "stop"}}\n```'


def _old_inline(system_prompt, tools_text, allow, *, catalog, project_root,
                mem_blk=""):
    """The composition exactly as ``run_turn`` wrote it inline before the
    refactor. Kept verbatim as the reference the join must reproduce."""
    system = (system_prompt or "") + "\n\n" + at._SYSTEM_BASE
    if catalog:
        from services.agent.role_catalog import (
            format_role_catalog_block,
            harness_context_block,
        )
        system = system.rstrip() + "\n\n" + harness_context_block()
        system = system.rstrip() + "\n\n" + format_role_catalog_block()
    if mem_blk:
        system = system.rstrip() + "\n\n" + mem_blk
    from services.agent.project_context import project_context_block
    blk = project_context_block(project_root)
    if blk:
        system = system.rstrip() + "\n\n" + blk
    from services.agent.autolearn import standing_guidance
    guidance = standing_guidance()
    if guidance:
        system = system.rstrip() + "\n\n" + guidance
    if tools_text:
        system += "\n\n" + tools_text
    elif allow:
        system += "\nAllowed tools: " + ", ".join(sorted(allow))
    return system


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point the on-disk cache at a tmp dir; never touch the user's data root."""
    d = tmp_path / "prompt_cache"
    d.mkdir()
    monkeypatch.setattr(pc, "cache_dir", lambda: d)
    pc._mem.clear()
    pc._fp_cache = None
    yield d
    pc._mem.clear()
    pc._fp_cache = None


# --------------------------------------------------------------------------- join rules

@pytest.mark.parametrize("catalog", [True, False])
@pytest.mark.parametrize("mem", ["", "[MEMORY]\n- recalled"])
def test_render_reproduces_the_live_join_byte_for_byte(catalog, mem):
    segs, _ = pc.static_segments(
        role_system=ROLE_SYS, tools_text=TOOLS, allowed=ALLOW,
        project_root=None, include_catalog=catalog, include_skills=False,
    )
    assert pc.render_segments(segs, memory_block=mem) == _old_inline(
        ROLE_SYS, TOOLS, ALLOW, catalog=catalog, project_root=None, mem_blk=mem)


def test_allowed_tools_fallback_uses_a_single_newline():
    """``run_turn`` writes ``"\\nAllowed tools: "`` — one newline, not a blank
    line. A blank line here would silently change every no-schema turn."""
    segs, _ = pc.static_segments(
        role_system=ROLE_SYS, tools_text="", allowed=ALLOW,
        project_root=None, include_catalog=False, include_skills=False,
    )
    out = pc.render_segments(segs)
    assert "\nAllowed tools: alpha, beta" in out
    assert "\n\nAllowed tools:" not in out
    assert out == _old_inline(ROLE_SYS, "", ALLOW, catalog=False,
                              project_root=None)


def test_empty_role_card_still_contributes_its_separator():
    """``(system_prompt or "") + "\\n\\n" + _SYSTEM_BASE`` — an empty role card
    leaves a leading blank line, and normalising it away would be a silent
    change to every turn that supplies no role text."""
    segs, _ = pc.static_segments(
        role_system="", tools_text=TOOLS, allowed=[],
        project_root=None, include_catalog=False, include_skills=False,
    )
    out = pc.render_segments(segs)
    assert out.startswith("\n\n" + at._SYSTEM_BASE)
    assert out == _old_inline("", TOOLS, [], catalog=False, project_root=None)


def test_role_card_trailing_newline_is_not_tidied_away():
    """The live path does not rstrip before appending _SYSTEM_BASE, so a role
    card ending in a newline really does contribute a third newline."""
    segs, _ = pc.static_segments(
        role_system="ROLE\n", tools_text="", allowed=[],
        project_root=None, include_catalog=False, include_skills=False,
    )
    assert pc.render_segments(segs).startswith("ROLE\n\n\n")


# --------------------------------------------------------------------------- the live path

def test_static_composition_is_the_live_prompts_exact_prefix(monkeypatch):
    """The load-bearing test: what a real turn sends must BE the composition.

    Memory recall is request-dependent and verified separately, so it is
    disabled here; the project root is pinned to the one the turn resolves.
    """
    monkeypatch.setattr("services.agent.config.memory_enabled",
                        lambda *a, **k: False)
    root = "/nonexistent-project-root-for-test"
    prov = CapturingProvider()
    at.run_turn("say done", prov, name="general_agent",
                system_prompt=ROLE_SYS, tools_text=TOOLS,
                allowed_tools=ALLOW, max_tool_calls=1,
                options={"project_dir": root})
    assert prov.systems, "the provider was never called"
    segs, _ = pc.static_segments(
        role_system=ROLE_SYS, tools_text=TOOLS, allowed=ALLOW,
        project_root=root, include_catalog=True, include_skills=False,
    )
    static = pc.render_segments(segs)
    live = prov.systems[0]
    assert live.startswith(static), (
        "the live system prompt no longer starts with the shared composition; "
        "the block order in run_turn and prompt_cache have drifted"
    )


def test_memory_block_lands_at_its_named_slot():
    """The memory block goes directly after MEMORY_SLOT, not at the end."""
    segs, _ = pc.static_segments(
        role_system=ROLE_SYS, tools_text=TOOLS, allowed=ALLOW,
        project_root=None, include_catalog=True, include_skills=False,
    )
    idx = next(i for i, s in enumerate(segs) if s.label == pc.MEMORY_SLOT)
    head = pc.render_segments(segs[:idx + 1])
    out = pc.render_segments(segs, memory_block="[MEM]")
    assert out.startswith(head.rstrip() + "\n\n[MEM]")
    assert not out.endswith("[MEM]"), "memory must not be appended at the end"


def test_memory_slot_exists_even_without_the_catalog_blocks():
    """A non-first turn suppresses the catalog blocks. Without a placeholder
    slot the memory block would be silently DROPPED rather than spliced."""
    segs, _ = pc.static_segments(
        role_system=ROLE_SYS, tools_text=TOOLS, allowed=ALLOW,
        project_root=None, include_catalog=False, include_skills=False,
    )
    assert any(s.label == pc.MEMORY_SLOT for s in segs)
    assert "[MEM]" in pc.render_segments(segs, memory_block="[MEM]")


def test_skills_block_is_opt_out_for_run_turn():
    """run_turn emits the skills catalog itself (interleaved with activation),
    so the shared list must be able to omit it without omitting the rest."""
    with_skills, _ = pc.static_segments(
        role_system=ROLE_SYS, project_root=None, include_catalog=True,
        include_skills=True)
    without, _ = pc.static_segments(
        role_system=ROLE_SYS, project_root=None, include_catalog=True,
        include_skills=False)
    assert any(s.label == "skills" for s in with_skills)
    assert not any(s.label == "skills" for s in without)


# --------------------------------------------------------------------------- caching

def test_fingerprint_changes_when_settings_change(isolated_cache, tmp_path,
                                                  monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("services.paths.CONFIG", cfg)
    pc._fp_cache = None
    before = pc.fingerprint()
    cfg.write_text('{"agent": {"plan": "always"}}', encoding="utf-8")
    pc._fp_cache = None
    assert pc.fingerprint() != before


def test_project_folder_contents_change_the_fingerprint(tmp_path):
    """The composed prompt lists the project folder's files, so adding one is a
    real change to the bytes and must invalidate."""
    proj = tmp_path / "proj"
    proj.mkdir()
    pc._fp_cache = None
    before = pc.fingerprint(project_root=str(proj))
    (proj / "bracket.step").write_text("x", encoding="utf-8")
    pc._fp_cache = None
    assert pc.fingerprint(project_root=str(proj)) != before


def test_an_entry_with_a_stale_fingerprint_is_never_served(isolated_cache):
    sp = pc.compose(include_tools=False)
    sp.fingerprint = "stale-fingerprint"
    pc._store_disk("k", sp)
    assert pc._load_disk("k", "current-fingerprint") is None


def test_an_entry_from_an_older_cache_version_is_rejected(isolated_cache):
    raw = pc.compose(include_tools=False).to_json()
    raw["version"] = pc.CACHE_VERSION - 1
    assert pc.StaticPrompt.from_json(raw) is None


def test_disk_round_trip_preserves_the_bytes(isolated_cache):
    sp = pc.compose(include_tools=False)
    back = pc.StaticPrompt.from_json(json.loads(json.dumps(sp.to_json())))
    assert back is not None
    assert back.render() == sp.render()


def test_get_serves_from_cache_without_recomposing(isolated_cache, monkeypatch):
    first = pc.get(include_tools=False)
    calls = []
    real = pc.compose
    monkeypatch.setattr(pc, "compose",
                        lambda **kw: calls.append(1) or real(**kw))
    second = pc.get(include_tools=False)
    assert not calls, "a cache hit must not recompose"
    assert second.render() == first.render()


def test_invalidate_drops_memory_and_disk(isolated_cache):
    pc.get(include_tools=False)
    assert list(isolated_cache.glob("*.json")), "nothing was cached to disk"
    pc.invalidate()
    assert not list(isolated_cache.glob("*.json"))
    assert not pc._mem


def test_warm_populates_the_cache(isolated_cache):
    t = pc.warm(background=True)
    t.join(timeout=120)
    assert not t.is_alive(), "warm thread did not finish"
    assert list(isolated_cache.glob("*.json"))


# --------------------------------------------------------------------------- reporting

def test_baseline_reports_real_tokens_and_a_breakdown(isolated_cache):
    b = pc.baseline()
    assert b["ok"] is True
    assert b["tokens"] > 0
    assert b["chars"] > 0
    labels = {row["label"] for row in b["segments"]}
    # The blocks that dominate a fresh chat must be itemised, or the tooltip
    # cannot explain what is filling the window.
    assert {"role", "base", "tools"} <= labels
    assert all(row["tokens"] > 0 for row in b["segments"])


def test_breakdown_is_ordered_largest_first(isolated_cache):
    rows = pc.get().breakdown()
    assert rows == sorted(rows, key=lambda r: -r["tokens"])


def test_breakdown_accounts_for_essentially_all_of_the_prompt(isolated_cache):
    """A breakdown that omits a segment would under-explain the total and let a
    silent block hide in the context window."""
    sp = pc.get()
    itemised = sum(row["tokens"] for row in sp.breakdown())
    # Separators are not attributed to any segment, so allow a small slack.
    assert abs(itemised - sp.total_tokens) <= 2 + len(sp.segments)


def test_baseline_never_raises_when_composition_fails(monkeypatch):
    monkeypatch.setattr(pc, "get",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    b = pc.baseline()
    assert b["ok"] is False
    assert b["tokens"] == 0
    assert "boom" in b["error"]


# --------------------------------------------------------------------------- wiring

def test_every_invalidating_rpc_is_registered_and_wrapped():
    """A rename must not silently switch the fast invalidation path off."""
    from services.agent import worker_rpc as wr

    assert wr.PROMPT_INVALIDATING_METHODS, "the set must not be empty"
    for name in wr.PROMPT_INVALIDATING_METHODS:
        assert name in wr.WORKER_METHODS, f"{name} is not a registered RPC"


def test_an_invalidating_rpc_actually_invalidates(isolated_cache, monkeypatch):
    from services.agent import worker_rpc as wr

    seen = []
    monkeypatch.setattr(pc, "invalidate", lambda: seen.append(1))
    wrapped = wr._with_prompt_invalidation("probe", lambda p, e: {"ok": True})
    assert wrapped({}, lambda *_: None) == {"ok": True}
    assert seen == [1]


def test_baseline_and_warm_rpcs_are_exposed():
    from services.agent import worker_rpc as wr

    assert "ai_prompt_baseline" in wr.WORKER_METHODS
    assert "ai_prompt_warm" in wr.WORKER_METHODS
    out = wr.WORKER_METHODS["ai_prompt_baseline"]({}, lambda *_: None)
    assert out["ok"] is True and out["tokens"] > 0
