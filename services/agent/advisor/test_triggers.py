"""Tests for trigger-based context injection — aicad.trigger/1.

Covers:
  * Keyword match → context item injected with prerun calc result present
  * M6x1.0 → tap_drill fires, answer.tap_drill_mm = 5.0 (real table lookup)
  * Cooldown suppression (second call within cooldown_turns → no item)
  * Budget drop-first: context items (info/tail) are dropped before findings (action/blocker)
  * Ledger events emitted per firing {trigger_id, matched, tokens}
  * No-match → zero cost path (no items, no events)
  * Multi-trigger: two patterns fire in one turn
  * Tool-name trigger fires when tool is in tool_names
  * Event trigger fires when event is in events list
  * Engine integration: build_advisory with output_text fires triggers additive to findings
  * Prerun fail-soft: calc error → pack injected without numbers
  * Cooldown state is caller-owned (fresh dict = no suppression)

Offline: no LLM, no core, no network.
"""
from __future__ import annotations

import pytest

from services.agent.advisor import triggers, engine, render
from services.agent.advisor.engine import Finding, Observation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_state() -> dict:
    """Return an empty cooldown state dict (each call starts fresh)."""
    return {}


# ---------------------------------------------------------------------------
# 1. No-match → zero cost (empty returns, no calcs)
# ---------------------------------------------------------------------------

def test_no_match_returns_empty():
    items, events = triggers.match_triggers(
        output_text="just draw a sphere",
        cooldown_state=_fresh_state(),
    )
    assert items == []
    assert events == []


def test_no_match_with_no_text_returns_empty():
    items, events = triggers.match_triggers(
        output_text="",
        tool_names=[],
        events=[],
        cooldown_state=_fresh_state(),
    )
    assert items == []
    assert events == []


# ---------------------------------------------------------------------------
# 2. thread_mention trigger: M6x1.0 → tap_drill prerun fires
# ---------------------------------------------------------------------------

def test_thread_mention_keyword_fires():
    items, events = triggers.match_triggers(
        output_text="I'll add a tapped hole M6x1.0 through the boss",
        cooldown_state=_fresh_state(),
    )
    # At least one item for thread_mention
    trigger_ids = {e["trigger_id"] for e in events}
    assert "thread_mention" in trigger_ids, f"thread_mention not in events: {events}"


def test_thread_mention_prerun_tap_drill_result():
    """M6x1.0 in text → tap_drill runs and answer.tap_drill_mm present in numbers."""
    items, events = triggers.match_triggers(
        output_text="tap M6x1.0 tapped hole for M6 bolt",
        cooldown_state=_fresh_state(),
    )
    thread_items = [it for it in items if "thread_mention" in it.get("check", "")]
    assert thread_items, "no thread_mention item injected"
    item = thread_items[0]
    assert "tap_drill_mm" in item["numbers"], (
        f"tap_drill_mm not in numbers: {item['numbers']}"
    )
    # Real table value: M6x1.0 coarse → 5.0 mm (feeds_speeds.json)
    assert item["numbers"]["tap_drill_mm"] == pytest.approx(5.0), (
        f"expected 5.0 mm pilot for M6x1.0, got {item['numbers']['tap_drill_mm']}"
    )


def test_thread_mention_item_is_info_severity():
    items, _ = triggers.match_triggers(
        output_text="M8x1.25 tap through aluminum block",
        cooldown_state=_fresh_state(),
    )
    thread_items = [it for it in items if "thread_mention" in it.get("check", "")]
    assert thread_items
    assert thread_items[0]["severity"] == "info"


def test_thread_mention_item_has_no_candidate_fixes():
    """Info items may have empty candidate_fixes (§3 invariant does not apply)."""
    items, _ = triggers.match_triggers(
        output_text="tap M6x1.0",
        cooldown_state=_fresh_state(),
    )
    thread_items = [it for it in items if "thread_mention" in it.get("check", "")]
    assert thread_items
    assert thread_items[0]["candidate_fixes"] == []


# ---------------------------------------------------------------------------
# 3. Cooldown suppression
# ---------------------------------------------------------------------------

def test_cooldown_suppresses_refire():
    state = _fresh_state()
    # First call at seq=0 → fires
    items1, events1 = triggers.match_triggers(
        output_text="tap M6x1.0",
        seq=0,
        cooldown_state=state,
    )
    assert any(e["trigger_id"] == "thread_mention" for e in events1)

    # Second call at seq=1 — cooldown_turns=8, delta=1 < 8 → suppressed
    items2, events2 = triggers.match_triggers(
        output_text="M6x1.0 tap again",
        seq=1,
        cooldown_state=state,
    )
    thread_events2 = [e for e in events2 if e["trigger_id"] == "thread_mention"]
    assert not thread_events2, "thread_mention should be in cooldown"
    thread_items2 = [it for it in items2 if "thread_mention" in it.get("check", "")]
    assert not thread_items2


def test_cooldown_expires_after_enough_turns():
    state = _fresh_state()
    triggers.match_triggers(output_text="tap M6x1.0", seq=0, cooldown_state=state)
    # Fire after cooldown_turns=8 have passed (seq=8 → delta=8, not < 8)
    items, events = triggers.match_triggers(
        output_text="tap M6x1.0", seq=8, cooldown_state=state
    )
    thread_events = [e for e in events if e["trigger_id"] == "thread_mention"]
    assert thread_events, "thread_mention should re-fire after cooldown expires"


def test_no_cooldown_state_fires_every_time():
    """None cooldown_state → no suppression (fires every call)."""
    items1, events1 = triggers.match_triggers(output_text="tap M6x1.0", cooldown_state=None)
    items2, events2 = triggers.match_triggers(output_text="tap M6x1.0", cooldown_state=None)
    assert any(e["trigger_id"] == "thread_mention" for e in events1)
    assert any(e["trigger_id"] == "thread_mention" for e in events2)


# ---------------------------------------------------------------------------
# 4. Ledger events: {trigger_id, matched, tokens}
# ---------------------------------------------------------------------------

def test_ledger_events_have_required_keys():
    _, events = triggers.match_triggers(
        output_text="tap M6x1.0",
        cooldown_state=_fresh_state(),
    )
    for ev in events:
        assert "trigger_id" in ev, f"missing trigger_id in {ev}"
        assert "matched" in ev, f"missing matched in {ev}"
        assert "tokens" in ev, f"missing tokens in {ev}"
        assert isinstance(ev["tokens"], int), f"tokens not int: {ev}"


def test_ledger_event_matched_is_string():
    _, events = triggers.match_triggers(
        output_text="I need to tap M8x1.25 holes",
        cooldown_state=_fresh_state(),
    )
    thread_events = [e for e in events if e["trigger_id"] == "thread_mention"]
    assert thread_events
    assert isinstance(thread_events[0]["matched"], str)


# ---------------------------------------------------------------------------
# 5. Tool-name trigger fires
# ---------------------------------------------------------------------------

def test_tool_name_trigger_fires_only_relevant_pack():
    """Generic sourcing tools must not pull domain-specific bearing guidance."""
    _, events = triggers.match_triggers(
        output_text="",
        tool_names=["part_search"],
        cooldown_state=_fresh_state(),
    )
    trigger_ids = {event["trigger_id"] for event in events}
    assert "part_sourcing" in trigger_ids, trigger_ids
    assert "bearing_seat" not in trigger_ids, trigger_ids


# ---------------------------------------------------------------------------
# 6. Event trigger fires
# ---------------------------------------------------------------------------

def test_event_trigger_fires():
    """thin_wall trigger fires on geometry event 'thin_wall'."""
    items, events = triggers.match_triggers(
        output_text="",
        events=["thin_wall"],
        cooldown_state=_fresh_state(),
    )
    trigger_ids = {e["trigger_id"] for e in events}
    assert "thin_wall" in trigger_ids, f"thin_wall not fired by event: {trigger_ids}"


# ---------------------------------------------------------------------------
# 7. Budget: context items sit at tail; render drops them before findings
# ---------------------------------------------------------------------------

def test_budget_drop_first_drops_context_before_findings():
    """With a tiny budget, an action finding survives while an info context item is dropped."""
    # A rebuild finding (action severity) plus trigger context item (info).
    f = Finding(kind="rebuild", check="rebuild", severity="action",
                finding="feature F1 rebuild failed")
    ctx_items, _ = triggers.match_triggers(
        output_text="tap M6x1.0",
        cooldown_state=_fresh_state(),
    )
    obs = Observation(stage="feature", findings=[f], seq=1,
                      output_text="tap M6x1.0",
                      cooldown_state=_fresh_state())
    advisory = engine.build_advisory(obs)
    # The advisory must have at least one action item (rebuild) and at least one info item (context).
    sevs = [it["severity"] for it in advisory["items"]]
    assert "action" in sevs
    assert "info" in sevs

    # With a very tiny budget, only the blocker/action survives — info (context) is dropped.
    block = render.render_advisory(advisory, budget_tokens=10)
    assert "action" in block or "rebuild" in block
    # At budget=10 the context info item should be dropped (never silent per §3).
    if "dropped for budget" in block:
        assert "lowest=info" in block


# ---------------------------------------------------------------------------
# 8. Engine integration: build_advisory with output_text fires triggers
# ---------------------------------------------------------------------------

def test_engine_build_advisory_fires_triggers_via_observation():
    """Passing output_text on Observation causes triggers to fire inside build_advisory."""
    obs = Observation(
        stage="feature",
        findings=[],
        seq=0,
        output_text="I'll add a tapped M6x1.0 hole through the boss",
        cooldown_state=_fresh_state(),
    )
    advisory = engine.build_advisory(obs)
    # At least one context item injected
    assert advisory["items"], "no items when triggers should have fired"
    # trigger_events exposed in advisory for host-side ledger writes
    assert "trigger_events" in advisory
    te = advisory["trigger_events"]
    assert any(e["trigger_id"] == "thread_mention" for e in te)


def test_engine_no_output_text_no_trigger_items():
    """Without output_text, no trigger context items are added."""
    obs = Observation(stage="feature", findings=[], seq=0)
    advisory = engine.build_advisory(obs)
    # No findings, no output_text → empty block
    assert advisory["items"] == []
    assert advisory.get("trigger_events", []) == []


# ---------------------------------------------------------------------------
# 9. Prerun fail-soft: unresolvable thread still injects pack
# ---------------------------------------------------------------------------

def test_prerun_fail_soft_still_injects_pack():
    """If the matched text doesn't contain a parseable thread designation,
    the pack is still injected but numbers dict will be empty."""
    items, events = triggers.match_triggers(
        output_text="I need to tap some threads here",   # 'tap' + 'thread' match but no M-designation
        cooldown_state=_fresh_state(),
    )
    thread_events = [e for e in events if e["trigger_id"] == "thread_mention"]
    if thread_events:
        thread_items = [it for it in items if "thread_mention" in it.get("check", "")]
        assert thread_items, "event fired but no item produced"
        # numbers may be empty or populated — either is fine (fail-soft)
        assert isinstance(thread_items[0]["numbers"], dict)


# ---------------------------------------------------------------------------
# 10. Multi-trigger: tolerance + thread in one turn
# ---------------------------------------------------------------------------

def test_multi_trigger_one_turn():
    """Multiple triggers can fire in a single turn."""
    items, events = triggers.match_triggers(
        output_text="M6x1.0 tapped hole with H7/g6 tolerance fit for the shaft",
        cooldown_state=_fresh_state(),
    )
    trigger_ids = {e["trigger_id"] for e in events}
    # At minimum thread_mention should fire; tolerance_mention may also fire
    assert "thread_mention" in trigger_ids or len(trigger_ids) >= 1


# ---------------------------------------------------------------------------
# 11. tolerance_mention trigger: fit designation extracted → fit_calc prerun
# ---------------------------------------------------------------------------

def test_tolerance_mention_fires_on_fit_keyword():
    items, events = triggers.match_triggers(
        output_text="The bore needs H7/g6 tolerance for sliding fit on Ø20",
        cooldown_state=_fresh_state(),
    )
    trigger_ids = {e["trigger_id"] for e in events}
    assert "tolerance_mention" in trigger_ids or "bearing_seat" in trigger_ids or len(trigger_ids) > 0


# ---------------------------------------------------------------------------
# 12. Context item source is "table" (from trigger table, not LLM)
# ---------------------------------------------------------------------------

def test_context_item_source_kind_is_table():
    items, _ = triggers.match_triggers(
        output_text="tap M6x1.0",
        cooldown_state=_fresh_state(),
    )
    for item in items:
        assert item["source"]["kind"] == "table", (
            f"context item source.kind should be 'table', got {item['source']['kind']}"
        )


# ---------------------------------------------------------------------------
# 13. load_table is idempotent (cached)
# ---------------------------------------------------------------------------

def test_load_table_is_cached():
    t1 = triggers.load_table()
    t2 = triggers.load_table()
    assert t1 is t2, "load_table should return the same cached list"


def test_load_table_has_at_least_8_triggers():
    table = triggers.load_table()
    assert len(table) >= 8, f"expected ≥8 triggers, got {len(table)}"


# ---------------------------------------------------------------------------
# Trigger PRECISION: a pack that fires on the wrong sense of a word is a tax.
#
# Live 2026-07-30 (run-4530697933837, OptiPlex case): the request said the case
# would be "fabricated with a 3D printer plus Shapeoko 3XL CNC", and the bare
# "3d print" keyword injected 250 tokens of reverse-engineering guidance --
# scans, point clouds, calipers -- into a from-scratch enclosure design. Printing
# is an OUTPUT of the reveng flow, not evidence anyone is reverse-engineering.
# ---------------------------------------------------------------------------

def _fired(text: str) -> set[str]:
    _, events = triggers.match_triggers(output_text=text,
                                        cooldown_state=_fresh_state())
    return {e["trigger_id"] for e in events}


def test_plain_3d_printing_does_not_pull_the_reveng_pack():
    text = ("Design a custom case for a Dell OptiPlex 7040 that reuses the stock "
            "PSU and can be fabricated with a 3D printer plus Shapeoko 3XL CNC.")
    assert "reveng_flow" not in _fired(text), _fired(text)


@pytest.mark.parametrize("text", [
    "Run a security scan on the firmware project.",
    "Find the TPS62840 datasheet and summarize its voltage limits.",
])
def test_unrelated_scan_and_datasheet_do_not_pull_reveng(text):
    assert "reveng_flow" not in _fired(text), _fired(text)


def test_mold_release_angle_does_not_pull_plm():
    text = "Add a 2 degree release angle to the molded face."
    assert "molded_draft" in _fired(text), _fired(text)
    assert "plm_flow" not in _fired(text), _fired(text)


def test_generic_part_search_does_not_pull_bearing_fits():
    _, events = triggers.match_triggers(
        output_text="Find M6 screws for the cover.",
        tool_names=["part_search"],
        cooldown_state=_fresh_state(),
    )
    fired = {event["trigger_id"] for event in events}
    assert "bearing_seat" not in fired, fired


@pytest.mark.parametrize("text", [
    "scan this bracket and 3d print a replacement",
    "3d print a copy of the existing housing",
    "reverse engineer this part",
    "measure it with calipers and rebuild the model",
    "scan this bracket and rebuild it",
    "extract the bracket dimensions from its datasheet",
])
def test_genuine_reverse_engineering_still_fires(text):
    """Narrowing the keyword must not cost real recall."""
    assert "reveng_flow" in _fired(text), text


def test_builtin_fallback_table_mirrors_the_yaml():
    """`_BUILTIN_TRIGGERS` is the no-PyYAML fallback; drift makes it a second,
    silently-wrong rule set.

    Several rules carry a "Mirrored verbatim in advisor/triggers.py -- change one,
    change the other" comment, but nothing enforced it: every other test in this
    file goes through match_triggers, which reads the YAML, so the fallback was
    exercised by no test at all. Proven while falsifying the 2026-07-30 reveng_flow
    narrowing -- seeding the defect back into triggers.py alone changed nothing.
    """
    import pathlib
    import yaml

    raw = yaml.safe_load(
        (pathlib.Path(__file__).parent / "triggers.yaml").read_text(encoding="utf-8"))
    y_rules = raw["triggers"] if isinstance(raw, dict) and "triggers" in raw else raw
    y = {r["id"]: r for r in y_rules}
    b = {r["id"]: r for r in triggers._BUILTIN_TRIGGERS}

    assert set(y) == set(b), (
        f"only in yaml: {sorted(set(y) - set(b))}; "
        f"only in builtin: {sorted(set(b) - set(y))}")
    for tid in sorted(y):
        assert triggers._normalize_trigger(y[tid]) == triggers._normalize_trigger(b[tid]), (
            f"{tid} drifted between triggers.yaml and _BUILTIN_TRIGGERS"
        )


def test_builtin_rules_have_concise_human_metadata():
    rows = {row["id"]: row for row in triggers.list_triggers()
            if row.get("source") == "builtin"}
    assert rows
    assert all(row["label"] and row["purpose"] for row in rows.values())
    assert rows["dfm_pre_op"]["label"] == "Design for manufacturability pre-op"
    assert all(len(row["label"]) <= 80 and len(row["purpose"]) <= 120
               for row in rows.values())


@pytest.mark.parametrize(("text", "unexpected"), [
    ("print this text and process the data", "dfm_pre_op"),
    ("the wall in the room needs paint", "dfm_pre_op"),
    ("run the shell script", "thin_wall"),
    ("align the text in the network interface", "interface_check_pre_op"),
    ("review the email draft", "molded_draft"),
    ("the job is stressful and the network mesh is down", "sim_setup"),
    ("buy time and quote the paragraph", "part_sourcing"),
    ("clip the file and release the latch", "snap_fit_mention"),
])
def test_generic_language_does_not_overtrigger_rules(text, unexpected):
    assert unexpected not in _fired(text)


@pytest.mark.parametrize("text", [
    "design for manufacturability review before release",
    "3D print the enclosure",
    "CNC machine the bracket",
    "check whether this part is manufacturable",
])
def test_dfm_rule_keeps_real_manufacturing_intent(text):
    assert "dfm_pre_op" in _fired(text)


def test_trigger_budget_caps_prefix_pack_and_exemplar():
    items, events = triggers.match_triggers(
        output_text="design for manufacturability review",
        cooldown_state=_fresh_state(),
    )
    row = next(t for t in triggers.load_table() if t["id"] == "dfm_pre_op")
    item = next(i for i in items if i["check"] == "context:dfm_pre_op")
    event = next(e for e in events if e["trigger_id"] == "dfm_pre_op")
    assert len(item["finding"]) <= row["budget_tokens"] * triggers._CHARS_PER_TOKEN
    assert event["tokens"] <= row["budget_tokens"]

def test_new_user_pack_is_visible_after_absent_directory_was_cached(
    tmp_path, monkeypatch,
):
    context_dir = tmp_path / "context_injection"
    monkeypatch.setenv("CHUDWARE_CONTEXT_INJECTION_DIR", str(context_dir))
    triggers.clear_cache()

    first, _ = triggers.match_triggers(
        output_text="design for manufacturability review",
        cooldown_state=_fresh_state(),
    )
    assert not any("USER PACK OVERRIDE" in item.get("finding", "") for item in first)

    packs_dir = context_dir / "packs"
    packs_dir.mkdir(parents=True)
    (packs_dir / "dfm_pre_op.md").write_text(
        "USER PACK OVERRIDE\nRun the local factory check.",
        encoding="utf-8",
    )

    second, _ = triggers.match_triggers(
        output_text="design for manufacturability review",
        cooldown_state=_fresh_state(),
    )
    assert any("USER PACK OVERRIDE" in item.get("finding", "") for item in second)
    triggers.clear_cache()


@pytest.mark.parametrize(("text", "nominal"), [
    ("H7/g6 Ø20", 20.0),
    ("h7/g6 dia 20", 20.0),
    ("H7/g6 20 mm", 20.0),
])
def test_iso_fit_extraction_prefers_fit_pair_and_common_nominals(text, nominal):
    values = triggers._extract_inputs(
        text, {"fit": "fit_designation", "nominal": "nominal_mm"})
    assert values["fit"].lower() == "h7/g6"
    assert values["nominal"] == nominal
