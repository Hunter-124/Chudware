"""Every ``CATEGORY_PACKS`` entry must be reachable by SOME live dispatch layer.

An ``enable_tools(['<pack>'])` call hands the model a list of tool names it is now
supposed to be able to call. A name in that list that no layer can resolve is worse
than a missing tool: the model spends a call, gets an unknown-tool error, and has no
way to tell "I typed it wrong" from "this capability does not exist". Two real bugs
of that shape shipped in this file and were only found by a manual audit:

  * ``interference_check`` sat one line below the real ``check_interference``;
  * ``check_sketch_d`` sat one line below the real ``check_sketch``.

Both are near-anagram typos of a tool that was already in the same pack.

THE TRAP THIS TEST ALSO GUARDS AGAINST — read before "fixing" a failure here.
Tool resolution is THREE independent layers, and no single one of them sees
everything:

  1. ``reviewer_exec._resolve_fn``   — a hand-maintained module_key list. Misses
     whole families (it has no ``reveng`` or ``delegation`` key), so a probe using
     only this layer reports a large false-positive "unresolved" set.
  2. ``agent_turn._bind_tool``      — layer 1, then the FULL MCP server registry,
     then the in-process ask_user/design_recipe pair. Sees every ``@mcp.tool()``.
  3. ``MissionDispatch.resolve``    — bound METHODS on the dispatcher
     (``mission_dispatch._handlers``): ``delegate``, ``ask_user``, the
     ``corporate_*`` / ``coordinate_*`` coordination surface, the parts
     passthroughs. These are NOT MCP-registered and layer 2 cannot see them, yet
     they run on every lane that binds a dispatcher — which includes the general
     lane, since ``run_linear_role_turn`` always constructs one.

An audit that consulted only layer 1 once reported 59 dead entries when 51 were
fine. An audit that consulted only layers 1+2 then deleted four LIVE coordination
tools from the ``delegate`` pack. So: a name is dead only when ALL THREE layers
miss it. If this test fails, the entry is genuinely unreachable — fix the NAME or
delete it; do not relax the union.
"""
from __future__ import annotations

import pytest

from services.agent import tool_categories as tcat


def _mission_dispatch_handlers() -> set[str]:
    """Names resolvable as MissionDispatch bound methods (layer 3)."""
    from services.agent.mission_dispatch import MissionDispatch

    try:
        from services.agent.delegation import DelegationTree

        tree = DelegationTree(run_id="cat-test", ledger=None, on_event=None)
    except Exception:  # pragma: no cover - delegation import is best-effort
        tree = None
    md = MissionDispatch(
        run_id="cat-test", ledger=None, provider=None, on_event=None,
        delegation_tree=tree,
    )
    return {n for n in _all_pack_names() if md.resolve(n) is not None}


def _all_pack_names() -> set[str]:
    """Every concrete tool name any pack advertises, family tokens expanded."""
    names: set[str] = set()
    for pack in tcat.CATEGORY_PACKS:
        # resolve_categories applies CATEGORY_EXPAND_CAP; expand uncapped so a
        # pack bigger than the cap still gets every entry checked.
        names |= tcat.resolve_categories([pack], cap=10_000)
    return names


@pytest.fixture(scope="module")
def resolvable() -> set[str]:
    """Union of all three live resolution layers."""
    import services.agent.agent_turn as at

    names = _all_pack_names()
    out: set[str] = set()
    for n in names:
        try:
            if at._bind_tool(n) is not None:
                out.add(n)
                continue
        except Exception:  # noqa: BLE001 — a raising binder is a miss, not a crash
            pass
    out |= _mission_dispatch_handlers()
    return out


class TestEveryPackEntryResolves:
    def test_no_pack_advertises_an_unreachable_tool(self, resolvable):
        dead = sorted(_all_pack_names() - resolvable)
        assert dead == [], (
            "these CATEGORY_PACKS entries resolve on NO dispatch layer, so "
            "enable_tools() hands the model names it cannot call: "
            f"{dead}. Check for a near-anagram typo of a real tool already in "
            "the same pack before assuming the capability is missing."
        )

    def test_pack_names_have_descriptions(self):
        missing = sorted(set(tcat.CATEGORY_PACKS) - set(tcat._CATEGORY_DESCS))
        assert missing == [], (
            f"packs with no description in _CATEGORY_DESCS: {missing} — "
            "category_catalog() is what the model reads to choose a pack."
        )

    def test_no_description_without_a_pack(self):
        orphan = sorted(set(tcat._CATEGORY_DESCS) - set(tcat.CATEGORY_PACKS))
        assert orphan == [], f"descriptions for packs that do not exist: {orphan}"


class TestCoordinationSurfaceIsNotMcpVisible:
    """Pin the layer-3-only class so a future audit cannot mistake it for dead.

    These four are MissionDispatch bound methods. ``_bind_tool`` CANNOT see them.
    If this test starts failing because they became MCP-registered, that is fine —
    update it. If it fails because they vanished from the dispatcher, the
    ``delegate`` pack is advertising dead names again.
    """

    NAMES = (
        "corporate_channel_read",
        "corporate_chart",
        "coordinate_board",
        "coordinate_progress",
    )

    def test_invisible_to_bind_tool_but_live_on_the_dispatcher(self):
        import services.agent.agent_turn as at

        handlers = _mission_dispatch_handlers()
        for name in self.NAMES:
            assert name in handlers, (
                f"{name} no longer resolves via MissionDispatch.resolve — the "
                "delegate category pack now advertises a dead tool."
            )
            try:
                bound = at._bind_tool(name)
            except Exception:  # noqa: BLE001
                bound = None
            assert bound is None, (
                f"{name} is now MCP-visible; this test's premise changed. "
                "Update the docstring in tool_categories.py's delegate pack too."
            )


class TestPreviouslyUnreachableAdvertisedTools:
    """The forward direction: a tool the harness NAMES must be in some pack.

    ``scripts/advertised_tool_audit.py`` sweeps that whole class (and is wired into
    ``scripts/check.py``). This test pins the individual holes the 2026-07-30 sweep
    closed, because each one had a specific instruction the model could not carry out
    and a silent pack edit would put it straight back:

      * ``submit_part_photos`` is step 1 of the numbered procedure ``intake_status``
        returns, and the whole ``intake`` family had no pack;
      * ``plm_timeline`` is named by the ``undo_denied`` refusal — the one place an
        agent is told how to roll a document back — and only ``plm_rollback`` was
        reachable;
      * ``get_part`` is named by every ``_CONCEPT_REDIRECTS`` teaching reply, and
        survived only as an alphabetical accident of ``family:cad`` expansion;
      * ``hydraulic_new`` / ``sketch3d_new`` / ``layout_plan`` / ``office_edit_pdf`` /
        ``req_verify_by_id`` each head a discipline cad_guide walks the model through
        step by step while no pack carried any of it;
      * ``vendor_status`` is what the parts guidance says to check before
        ``scrape_enqueue``;
      * ``request_raise`` is the worker half of a request protocol whose manager half
        (``request_resolve``) was already reachable.
    """

    @pytest.mark.parametrize("tool", [
        "submit_part_photos",
        "request_part_photos",
        "intake_status",
        "make_fit_coupon",
        "plm_timeline",
        "get_part",
        "recover_parts",
        "hydraulic_new",
        "piping_solve",
        "sketch3d_new",
        "sketch3d_pipe",
        "layout_plan",
        "req_verify_by_id",
        "office_edit_pdf",
        "vendor_status",
        "request_raise",
        "fw_debug_halt",
        "struct_estimate",
        "ecad_thermal_handoff",
        "mission_status",
    ])
    def test_named_tool_is_advertised_by_a_pack(self, tool):
        assert tool in _all_pack_names(), (
            f"{tool!r} is named in a prompt_note/hint the model reads but no "
            f"CATEGORY_PACKS pack advertises it — enable_tools cannot reach it. "
            f"See scripts/advertised_tool_audit.py."
        )

    def test_curated_ecad_pack_survives_the_expand_cap(self):
        """No ``family:ecad`` token, and every entry survives resolve_categories.

        The ecad family is 67 tools against ``CATEGORY_EXPAND_CAP`` (40) and the trim is
        alphabetical, so a family token made reachability depend on where the cut landed:
        adding the thermal trio evicted ``ecad_to_3d``/``ecad_update_from_sch``, then
        ``ecad_sweep``/``ecad_thermal_handoff``, then ``ecad_set_net_class``/
        ``ecad_stackup_solve``. Re-introducing the token restarts that cascade.
        """
        pack = tcat.CATEGORY_PACKS["ecad"]
        assert not [e for e in pack if e.startswith("family:")], (
            "the ecad pack must stay fully curated — a family: token reintroduces the "
            "alphabetical eviction cascade"
        )
        resolved = tcat.resolve_categories(["ecad"])
        missing = [e for e in pack if e not in resolved]
        assert missing == [], f"ecad entries trimmed by CATEGORY_EXPAND_CAP: {missing}"
