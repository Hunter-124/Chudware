"""Tests for the precomposed system prompt cache.

The contract that matters: the cached/precomposed prompt must be the bytes a
LIVE turn actually sends. A hand-maintained mirror is worse than no number,
because it looks authoritative while drifting from the effective global/role
prompt, lane floors, preferences, or tool schemas. The central test captures
the system prompt a real ``run_turn`` hands its provider and asserts equality
against the shared composition rather than merely checking internal consistency.
"""
from __future__ import annotations

import json
import re

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


def _old_linear_inline(role, capability, tier, dispatcher):
    """The composition exactly as ``run_linear_role_turn`` wrote it inline before
    the lane was routed through the cache, plus the skills catalog the lane now
    appends so the online Chudcode turn can see its skill packs. Kept as the
    reference the routed path must reproduce byte for byte."""
    from services.agent import mission_dispatch as md
    from services.agent import skills as _skills
    from services.agent.prompt_tiers import (
        assemble_system_for_capability,
        letter_from_capability,
    )
    from services.agent.roles import load_role
    from services.agent.tool_schemas import render_tools_block

    card = load_role(role)
    allowed = md.linear_role_allowed_tools(card)
    base_system_prompt = md.effective_role_system_prompt(card)
    system_prompt = base_system_prompt
    try:
        system_prompt = assemble_system_for_capability(
            base_system_prompt, letter_from_capability(capability))
    except Exception:
        system_prompt = base_system_prompt
    allowed, system_prompt = md._apply_session_preferences(
        role, allowed, system_prompt)
    catalog = _skills.catalog_block()
    if catalog:
        system_prompt = system_prompt.rstrip() + "\n\n" + catalog
    tools_text = None
    try:
        tools_text, _meta = render_tools_block(
            allowed, resolver=at.resolver_for(dispatcher),
            tier=tier or getattr(dispatcher, "tier", None))
    except Exception:
        tools_text = None
    return system_prompt, allowed, tools_text


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point the on-disk cache at a tmp dir; never touch the user's data root."""
    d = tmp_path / "prompt_cache"
    d.mkdir()
    monkeypatch.setattr(pc, "cache_dir", lambda: d)
    pc._mem.clear()
    pc._online_mem.clear()
    pc._fp_cache = None
    yield d
    pc._mem.clear()
    pc._online_mem.clear()
    pc._fp_cache = None


@pytest.fixture
def linear_tools_memo():
    """Empty the linear lane's in-process tools-slot memo around a test."""
    from services.agent import mission_dispatch as md

    md._TOOLS_SLOT_MEMO.clear()
    yield md._TOOLS_SLOT_MEMO
    md._TOOLS_SLOT_MEMO.clear()


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




# --------------------------------------------------------------------------- the linear lane

@pytest.mark.parametrize("tier", [None, "frontier"])
@pytest.mark.parametrize("role", ["general_agent", "feature_modeler"])
def test_linear_lane_prompt_is_byte_identical_to_the_inline_composition(
        isolated_cache, linear_tools_memo, role, tier):
    """Routing the lane through the cache must not move a single byte.

    ``frontier`` is the interesting tier: it allocates a 10000-token slot with
    ``tool_schema_trim`` off, where the cache's own render (no tier, no
    dispatcher) is a different block entirely — so the routed path must not
    serve it.
    """
    from services.agent import mission_dispatch as md

    dispatcher = md.MissionDispatch(run_id="equiv", tier=tier)
    reference = _old_linear_inline(role, None, tier, dispatcher)
    routed = md.linear_role_prompt(
        role, resolver=at.resolver_for(dispatcher), capability=None,
        tier=tier or getattr(dispatcher, "tier", None))
    assert routed[0] == reference[0], "role prose drifted"
    assert routed[1] == reference[1], "the allowlist drifted"
    assert routed[2] == reference[2], "the tools block drifted"


@pytest.mark.parametrize("role", ["general_agent", "sim_engineer"])
def test_the_online_role_prompt_carries_the_skills_catalog_and_its_fetch_verb(
        isolated_cache, role):
    """The ONLINE lane's only source of skill visibility.

    ``worker_rpc._agent_admin_turn`` and ``chudcode_roles`` both compose their
    system prompt with ``role_prompt`` and hand it straight to Chudcode, which
    never consults ``services.agent.skills``. Before this block, a role card's "full procedure:
    skill://assembly-merge" reached a model whose only ``skill://`` resolver was
    Chudcode's own foreign registry — live mission-18d24905b774203f answered
    "Unknown skill: assembly-merge" and fell back to the card's summary.
    """
    from services.agent import mission_dispatch as md

    system_prompt, allowed = md.role_prompt(role)

    assert "[SKILLS]" in system_prompt, "the online lane lost the skills catalog"
    assert "assembly-merge" in system_prompt and "simulate" in system_prompt
    assert 'cad_skill(name="<name>")' in system_prompt
    # The catalog's own warning names the scheme; a POINTER (skill://<name>) is
    # what sent the model to Chudcode's `read` builtin.
    assert not re.search(r"skill://[a-z0-9]", system_prompt), (
        "a skill:// pointer resolves against Chudcode's own registry, not CAD's")
    assert "cad_skill" in allowed, "the catalog names a tool the lane cannot call"
    if role == "general_agent":
        # Acquisition policy reaches the actual composed Chudcode prompt once;
        # repeating it in the role, global header, and skill catalog invites
        # contradictory search-first behavior on a servo reference request.
        assert system_prompt.count("REFERENCE ACQUISITION ORDER") == 1
        assert system_prompt.index("part_search / make_standard_part") < system_prompt.index(
            "match_model / part_source") < system_prompt.index("fetch_cad_model(url)")
        assert system_prompt.index("fetch_cad_model(url)") < system_prompt.index(
            "Web search only for specs/datasheets")
        # The live MG90S turn sourced a servo, then deliberated for 25 minutes
        # without saving a bracket. The composed prompt must move from a short
        # project artifact to incremental saved parts and then assembly.
        assert system_prompt.count("MULTI-PART MECHANISMS") == 1
        assert system_prompt.index('fs_write(path="design/layout.md"') < system_prompt.index(
            "new_part + add_features") < system_prompt.index(
            "asm_add_component(fixed=True)") < system_prompt.index(
            "simulate_motion")
        assert 'value={"value": 0,' in system_prompt
        assert '"axis_point":' in system_prompt
        assert "mate EVERY joint" in system_prompt
        # YAML's literal block preserves wrapping; the policy is a sentence,
        # not a requirement on where the role card breaks its lines.
        assert "Never use hand-rolled kinematics or throwaway pose assemblies" in (
            " ".join(system_prompt.split()))
        assert "few minutes deliberating without producing a file or a CAD change" in system_prompt


def test_a_repeat_turn_reuses_the_tools_block_but_a_new_fingerprint_recomposes(
        isolated_cache, linear_tools_memo, tmp_path, monkeypatch):
    """The point of the routing: turn two must not re-render the tool schemas.

    Each turn builds its own ``MissionDispatch``, so the reuse cannot depend on
    dispatcher identity — only on what the render actually reads. A fingerprint
    change (here a settings write, the same event that calls ``invalidate``)
    must still recompose, or a Settings change would never reach the model.
    """
    from services.agent import mission_dispatch as md
    from services.agent import tool_schemas as tsch
    from services.agent.preferences import resolve_turn_preferences

    rendered: list[list[str]] = []
    real_render = tsch.render_tools_block

    def counting(allowed, **kw):
        rendered.append(list(allowed))
        return real_render(allowed, **kw)

    monkeypatch.setattr(tsch, "render_tools_block", counting)

    def _turn():
        dispatcher = md.MissionDispatch(run_id="turn", tier="frontier")
        # Reference acquisition mutators belong to Agent mode, not the
        # interactive Plan default's intentionally read-only roster.
        preferences = resolve_turn_preferences(
            "Build an MG90S servo arm",
            cfg={"copilot": {"work_style": "do_it"}},
            task_kind="mission")
        return md.linear_role_prompt(
            "general_agent", resolver=at.resolver_for(dispatcher),
            tier="frontier", preferences=preferences)

    first = _turn()
    assert first[2], "the tools slot must render on the first turn"
    after_first = len(rendered)
    # The MG90S arm turn researched the web without ever loading the sourcing
    # tools. Check the rendered frontier prompt, not just an allowlist: these
    # schemas must be visible in the model's immediate CAD tool surface.
    for name in ("part_search", "part_get", "part_source", "match_model",
                 "make_standard_part", "list_standard_parts", "fetch_cad_model",
                 "import_artifact"):
        assert f"- {name}(" in first[2], name
    assert after_first >= 1

    second = _turn()
    assert second == first, "a repeat turn must hand back the identical block"
    assert len(rendered) == after_first, (
        "the tools block was recomposed on an identical second turn")

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("services.paths.CONFIG", cfg)
    pc._mem.clear()
    pc._fp_cache = None

    third = _turn()
    assert len(rendered) > after_first, (
        "a fingerprint change must recompose the tools block")
    assert third[2], "the recomposed slot must still render"


def test_a_session_preference_override_beats_a_valid_cache_entry(
        isolated_cache, linear_tools_memo, monkeypatch):
    """A cached prompt must not re-enable reviews after double-check is disabled."""
    from services.agent import config as agent_config
    from services.agent import mission_dispatch as md

    monkeypatch.setattr(agent_config, "load_config",
                        lambda: {"copilot": {"double_check": True}})
    entry = pc.get(role="general_agent")
    assert "advise" in entry.allowed

    monkeypatch.setattr(agent_config, "load_config",
                        lambda: {"copilot": {"double_check": False}})
    dispatcher = md.MissionDispatch(run_id="pref")
    system_prompt, allowed, tools_text = md.linear_role_prompt(
        "general_agent", resolver=at.resolver_for(dispatcher))

    assert "advise" not in allowed
    assert "\n- advise(" not in (tools_text or "")

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
    assert not pc._online_mem


def test_warm_composes_the_online_prompt_in_process(isolated_cache):
    t = pc.warm(background=True)
    t.join(timeout=120)
    assert not t.is_alive(), "warm thread did not finish"
    assert "general_agent" in pc._online_mem
    assert not list(isolated_cache.glob("*.json")), "the online prompt has no disk entry"


# --------------------------------------------------------------------------- reporting

def test_baseline_reports_real_tokens_and_a_breakdown(isolated_cache):
    b = pc.baseline()
    assert b["ok"] is True
    assert b["lane"] == "online"
    assert b["tokens"] > 0
    assert b["chars"] > 0
    labels = {row["label"] for row in b["segments"]}
    # The blocks that dominate a fresh chat must be itemised, or the tooltip
    # cannot explain what is filling the window.
    assert labels == {"system", "host_tools"}
    assert all(row["tokens"] > 0 for row in b["segments"])
    assert b["tokens"] == sum(row["tokens"] for row in b["segments"])


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


def test_baseline_never_raises_when_composition_fails(isolated_cache, monkeypatch):
    monkeypatch.setattr(pc, "compose_online",
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
