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
        self.messages: list[list[dict]] = []

    def complete(self, system, messages, **kw):  # noqa: ANN001, ANN003
        self.systems.append(system)
        self.messages.append([dict(m) for m in messages])
        return '```json\n{"done": true, "handoff": {"summary": "stop"}}\n```'


def _old_inline(system_prompt, tools_text, allow, *, catalog, project_root):
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
    appends so the online Omp turn can see its skill packs. Kept as the
    reference the routed path must reproduce byte for byte."""
    from services.agent import mission_dispatch as md
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
        role, allowed, system_prompt, include_skills=True)
    tools_text = None
    try:
        tools_text, _meta = render_tools_block(
            allowed, resolver=at.resolver_for(dispatcher),
            tier=tier or getattr(dispatcher, "tier", None))
    except Exception:
        tools_text = None
    return system_prompt, allowed, tools_text


def test_preferences_follow_static_toolbox_and_skills(monkeypatch):
    from services.agent import mission_dispatch as md, skills, tool_categories
    from services.agent.preferences import resolve_turn_preferences

    monkeypatch.setattr(md, "_role_base_prompt", lambda *a, **k: (["describe_part"], "ROLE"))
    monkeypatch.setattr(tool_categories, "render_tool_menu", lambda loaded: "STATIC MENU")
    monkeypatch.setattr(skills, "catalog_block", lambda: "STATIC SKILLS")
    prefs = resolve_turn_preferences(
        "", cfg={"copilot": {"work_style": "do_it"}}, task_kind="modelling",
    )
    system, _, witness = md._compose_role_prompt(
        "general_agent", card=None, capability=None, preferences=prefs,
    )
    assert system.index("STATIC MENU") < system.index("STATIC SKILLS")
    assert system.index("STATIC SKILLS") < system.index(prefs.prompt_guidance())
    assert "STATIC SKILLS" not in witness


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
def test_render_reproduces_the_live_join_byte_for_byte(catalog):
    segs, _ = pc.static_segments(
        role_system=ROLE_SYS, tools_text=TOOLS, allowed=ALLOW,
        project_root=None, include_catalog=catalog, include_skills=False,
    )
    assert pc.render_segments(segs) == _old_inline(
        ROLE_SYS, TOOLS, ALLOW, catalog=catalog, project_root=None)


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
    from services.agent import request_context
    original_root_value = request_context.root_value
    monkeypatch.setattr(request_context, "root_value", lambda key, default="": (
        root if key == "CHUDWARE_PROJECT_DIR" else original_root_value(key, default)))
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


def _stable_turn_kwargs():
    return dict(
        name="general_agent", system_prompt=ROLE_SYS, tools_text=TOOLS,
        allowed_tools=ALLOW, max_tool_calls=1, run_id="one-conversation",
    )


def test_system_prompt_is_identical_across_turns_with_different_requests(monkeypatch):
    """A prefix-caching local server only reuses its cache for an identical
    prefix. Project-memory recall and request-triggered skill bodies are keyed on
    the request, so they must ride at the tail of the user message — and the
    catalog must not be sent on the first turn only — or every turn's system
    prompt differs from the last."""
    from services.agent import project_memory as pm

    monkeypatch.setattr("services.agent.config.memory_enabled", lambda *a, **k: True)
    monkeypatch.setattr(pm, "resolve_project_root", lambda *_a, **_k: "/nonexistent-root")
    monkeypatch.setattr(
        pm, "recall", lambda _root, request, **_k: [{"text": request, "tokens": 1}])
    monkeypatch.setattr(
        pm, "format_for_context", lambda hits: "[MEMORY]\n" + hits[0]["text"])

    first, second = CapturingProvider(), CapturingProvider()
    at.run_turn("make me a bracket 50mm long", first, **_stable_turn_kwargs())
    at.run_turn("now make a 2D drawing of it", second, **_stable_turn_kwargs())

    assert first.systems[0] == second.systems[0]
    assert "[MEMORY]" not in first.systems[0]
    # No history, so the request is the first message; trigger advisories the
    # loop injects afterwards are separate messages.
    tail = second.messages[0][0]["content"][0]["text"]
    assert tail.startswith("now make a 2D drawing of it")
    assert "[MEMORY]\nnow make a 2D drawing of it" in tail


def test_system_prompt_ignores_the_per_turn_mode():
    """Copilot mode is enforced at dispatch and announced at the user-message
    tail; it must not rewrite the system prompt."""
    agent, plan = CapturingProvider(), CapturingProvider()
    at.run_turn("say done", agent, mode="agent", **_stable_turn_kwargs())
    at.run_turn("say done", plan, mode="plan", **_stable_turn_kwargs())

    assert agent.systems[0] == plan.systems[0]
    assert "[turn mode: plan]" in plan.messages[0][0]["content"][0]["text"]
    assert "[turn mode" not in agent.messages[0][0]["content"][0]["text"]


def test_enabling_tools_mid_turn_appends_instead_of_rewriting_the_system_prompt():
    """``enable_tools`` used to splice the re-rendered tools block into the
    system prompt mid-turn, invalidating the whole provider cache. The new
    schemas now arrive as one appended message and every earlier byte stays."""

    class Scripted(CapturingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.replies = [
                '```json\n{"tool": "enable_tools", "args": {"tools": ["measure_part"]}}\n```',
                '```json\n{"done": true, "handoff": {"summary": "stop"}}\n```',
            ]

        def complete(self, system, messages, **kw):  # noqa: ANN001, ANN003
            super().complete(system, messages, **kw)
            return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    prov = Scripted()
    at.run_turn(
        "enable the measuring tool", prov, name="general_agent",
        system_prompt=ROLE_SYS, tools_text=TOOLS,
        allowed_tools=["enable_tools", "list_tool_categories"],
        max_tool_calls=4, tool_dispatch=lambda t, a: {"ok": True},
    )

    assert len(prov.systems) >= 2
    assert len(set(prov.systems)) == 1, "the system prompt changed mid-turn"
    before, after = prov.messages[0], prov.messages[1]
    assert after[:len(before)] == before, "an earlier message was rewritten"
    appended = "\n".join(
        m["content"] if isinstance(m["content"], str) else m["content"][0]["text"]
        for m in after[len(before):]
    )
    assert "[tools enabled]" in appended and "measure_part" in appended


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
