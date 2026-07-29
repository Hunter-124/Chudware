"""`/rule` composer slash command — parse + create path (aicad.trigger/1).

Offline: no LLM, no core, no network. Writes into a temp
``CHUDWARE_CONTEXT_INJECTION_DIR`` and a temp skills dir.
"""
from __future__ import annotations

import json
import re

import pytest

from services.agent import skills as skills_mod
from services.agent.advisor import triggers
from services.agent.worker_rpc import TRIGGERS_METHODS


def _emit(*_a, **_k) -> None:
    return None


def _rule_create(**params) -> dict:
    return TRIGGERS_METHODS["rule_create"](params, _emit)


@pytest.fixture()
def user_dir(tmp_path, monkeypatch):
    d = tmp_path / "ctx"
    monkeypatch.setenv("CHUDWARE_CONTEXT_INJECTION_DIR", str(d))
    monkeypatch.setenv("CHUDWARE_SKILLS_USER_DIR", str(tmp_path / "skills"))
    triggers.clear_cache()
    skills_mod.clear_cache()
    yield d
    triggers.clear_cache()
    skills_mod.clear_cache()


# --------------------------------------------------------------------------- parse


def test_parse_returns_none_for_non_rule_text():
    assert skills_mod.parse_rule_slash("/skill:edit-part") is None
    assert skills_mod.parse_rule_slash("make me a bracket") is None


@pytest.mark.parametrize(
    "text",
    [
        "/rule",
        "/rule   ",
        "/rule always use M6 bolts",          # no arrow
        "/rule -> always use M6 bolts",       # no trigger words
        "/rule bolt ->",                      # nothing to inject
        "/rule --- -> body",                  # id cannot be derived
    ],
)
def test_parse_refuses_incomplete_rules(text):
    out = skills_mod.parse_rule_slash(text)
    assert out is not None
    assert out["ok"] is False
    assert out["reason"]
    assert out["usage"] == skills_mod.RULE_SLASH_USAGE


def test_parse_splits_trigger_and_injection():
    out = skills_mod.parse_rule_slash("/rule  press   fit  -> Use H7/p6 for steel hubs.")
    assert out == {
        "ok": True,
        "id": "press_fit",
        "keyword": "press fit",
        "instruction": "Use H7/p6 for steel hubs.",
        "purpose": "When 'press fit' appears: Use H7/p6 for steel hubs.",
    }


# --------------------------------------------------------------------------- create


def test_rule_create_lands_on_disk_and_round_trips(user_dir):
    out = _rule_create(text="/rule press fit -> Use H7/p6 for steel hubs.")
    assert out["ok"] is True, out
    rid = out["id"]
    assert rid == "press_fit"
    assert out["enabled"] is True

    # The FILE landed in the user override dir (not just an ok response).
    tpath = triggers.user_triggers_path()
    assert tpath.is_file(), f"{tpath} missing"
    payload = json.loads(tpath.read_text(encoding="utf-8"))
    assert payload["schema"] == "aicad.trigger/1"
    assert [t["id"] for t in payload["triggers"]] == [rid]
    pack_file = triggers.user_packs_dir() / f"{rid}.md"
    assert pack_file.is_file(), f"{pack_file} missing"
    assert "Use H7/p6 for steel hubs." in pack_file.read_text(encoding="utf-8")

    # triggers_list (the RPC the Rules tab calls) reports it as a user rule.
    listed = TRIGGERS_METHODS["triggers_list"]({}, _emit)
    row = next(r for r in listed["triggers"] if r["id"] == rid)
    assert row["source"] == "user"
    assert row["enabled"] is True

    # get_trigger round-trips the trigger with its match intact + the pack body.
    got = triggers.get_trigger(rid)
    assert got["ok"] is True
    assert got["trigger"]["match"]["keywords"] == [re.escape("press fit")]
    assert got["trigger"]["inject"]["context_pack"] == rid
    assert "H7/p6" in got["pack"]["body"]


def test_created_rule_actually_fires(user_dir):
    assert _rule_create(text="/rule press fit -> Use H7/p6 for steel hubs.")["ok"] is True
    _items, events = triggers.match_triggers("what tolerance for a press fit hub?",
                                             cooldown_state={})
    fired = [e for e in events if e["trigger_id"] == "press_fit"]
    assert fired, [e["trigger_id"] for e in events]
    assert fired[0]["matched"].lower() == "press fit"
    assert fired[0]["tokens"] > 0


def test_rule_with_no_trigger_is_refused_and_not_active(user_dir):
    before = {r["id"] for r in triggers.list_triggers()}
    out = _rule_create(text="/rule -> Use H7/p6 for steel hubs.")
    assert out["ok"] is False
    assert "never fire" in out["reason"] or "no trigger words" in out["reason"]
    out2 = _rule_create(keyword="", instruction="Use H7/p6.")
    assert out2["ok"] is False
    assert "can never fire" in out2["reason"]

    # Nothing was written, and no new rule is reported as active.
    assert not triggers.user_triggers_path().exists()
    after = {r["id"] for r in triggers.list_triggers() if r["enabled"]}
    assert after - before == set()


def test_duplicate_keyword_gets_a_fresh_id(user_dir):
    a = _rule_create(text="/rule press fit -> first")
    b = _rule_create(text="/rule press fit -> second")
    assert a["id"] == "press_fit"
    assert b["id"] == "press_fit_2"
    ids = {r["id"] for r in triggers.list_triggers()}
    assert {"press_fit", "press_fit_2"} <= ids


def test_regex_metacharacters_in_trigger_are_literal(user_dir):
    out = _rule_create(text="/rule H7/g6 (bore) -> Use a slip fit.")
    assert out["ok"] is True
    _items, events = triggers.match_triggers("spec the H7/g6 (bore) here", cooldown_state={})
    assert any(e["trigger_id"] == out["id"] for e in events)
    # The literal-escaped pattern must not match a regex interpretation.
    _items2, events2 = triggers.match_triggers("H7/g6 bore", cooldown_state={})
    assert not any(e["trigger_id"] == out["id"] for e in events2)


# --------------------------------------------------------------------------- skills isolation


def test_resolve_slash_still_resolves_real_skills(user_dir, tmp_path):
    d = tmp_path / "skills" / "press-fit-check"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: press-fit-check\ndescription: check press fits\n---\n\nbody\n",
        encoding="utf-8",
    )
    skills_mod.clear_cache()
    cleaned, hits = skills_mod.resolve_slash("/press-fit-check do it")
    assert hits == [("press-fit-check", "do it")]
    assert cleaned == ""
    cleaned2, hits2 = skills_mod.resolve_slash("/skill:press-fit-check")
    assert hits2 == [("press-fit-check", "")]


def test_rule_is_not_a_skill(user_dir):
    names = {s.name for s in skills_mod.discover_skills()}
    assert "rule" not in names
    assert "rule" in skills_mod.RESERVED_SLASH_COMMANDS
    # /rule is left in the text for the composer/rule path; never a skill hit.
    cleaned, hits = skills_mod.resolve_slash("/rule press fit -> Use H7/p6.")
    assert hits == []
    assert cleaned.startswith("/rule press fit")


def test_reserved_name_wins_over_a_skill_named_rule(user_dir, tmp_path):
    d = tmp_path / "skills" / "rule"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: rule\ndescription: impostor\n---\n\nbody\n", encoding="utf-8",
    )
    skills_mod.clear_cache()
    assert "rule" in {s.name for s in skills_mod.discover_skills()}
    _cleaned, hits = skills_mod.resolve_slash("/rule press fit -> Use H7/p6.")
    assert hits == [], "reserved /rule must never resolve as a skill activation"
