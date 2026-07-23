"""CRUD + merge tests for context-injection triggers (GUI surface).

Offline: no LLM, no core, no network. Uses a temp CHUDWARE_CONTEXT_INJECTION_DIR.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.agent.advisor import triggers


@pytest.fixture()
def user_dir(tmp_path, monkeypatch):
    d = tmp_path / "ctx"
    monkeypatch.setenv("CHUDWARE_CONTEXT_INJECTION_DIR", str(d))
    triggers.clear_cache()
    yield d
    triggers.clear_cache()


def test_list_includes_shipped(user_dir):
    rows = triggers.list_triggers()
    ids = {r["id"] for r in rows}
    assert "thread_mention" in ids
    assert all(r["source"] == "builtin" for r in rows)


def test_save_override_disables_match(user_dir):
    base = next(r for r in triggers.list_triggers() if r["id"] == "thread_mention")
    base["enabled"] = False
    base["purpose"] = "disabled for test"
    out = triggers.save_trigger(base)
    assert out["ok"] is True
    assert out["source"] == "override"

    g = triggers.get_trigger("thread_mention")
    assert g["ok"] is True
    assert g["trigger"]["enabled"] is False
    assert g["trigger"]["source"] == "override"

    items, events = triggers.match_triggers(
        "tap M6x1.0 hole", cooldown_state={},
    )
    assert not any(e["trigger_id"] == "thread_mention" for e in events)
    assert not any("thread_mention" in it.get("check", "") for it in items)


def test_delete_restores_builtin(user_dir):
    base = next(r for r in triggers.list_triggers() if r["id"] == "thread_mention")
    base["enabled"] = False
    triggers.save_trigger(base)
    out = triggers.delete_trigger("thread_mention")
    assert out["ok"] is True
    assert out["restored_builtin"] is True
    g = triggers.get_trigger("thread_mention")
    assert g["trigger"]["enabled"] is True
    assert g["trigger"]["source"] == "builtin"
    items, events = triggers.match_triggers("tap M6x1.0", cooldown_state={})
    assert any(e["trigger_id"] == "thread_mention" for e in events)


def test_user_only_trigger(user_dir):
    out = triggers.save_trigger({
        "id": "custom_foo",
        "enabled": True,
        "purpose": "custom pack for foo designs",
        "match": {"keywords": [r"\bfoo_widget\b"], "tools": [], "events": []},
        "inject": {
            "context_pack": "custom_foo",
            "prerun_calcs": [],
            "exemplar": "always call verify_battery after foo_widget",
        },
        "budget_tokens": 180,
        "cooldown_turns": 4,
    })
    assert out["ok"] is True
    assert out["source"] == "user"
    # pack body
    p = triggers.save_pack("custom_foo", "# Foo\n\nAlways verify.\n")
    assert p["ok"] is True
    items, events = triggers.match_triggers(
        "build a foo_widget bracket", cooldown_state={},
    )
    assert any(e["trigger_id"] == "custom_foo" for e in events)
    assert any("Always verify" in it.get("finding", "") for it in items)

    d = triggers.delete_trigger("custom_foo")
    assert d["ok"] is True
    assert d["restored_builtin"] is False
    assert triggers.get_trigger("custom_foo")["ok"] is False


def test_pack_shadow(user_dir):
    # Override shipped threads pack
    body = "# USER THREADS\n\ncustom pilot note\n"
    out = triggers.save_pack("threads", body)
    assert out["ok"] is True
    assert out["source"] == "override"
    g = triggers.get_pack("threads")
    assert g["body"].startswith("# USER THREADS")
    # match path uses shadowed body
    items, _ = triggers.match_triggers("tap M6x1.0", cooldown_state={})
    thread_items = [it for it in items if "thread_mention" in it.get("check", "")]
    assert thread_items
    assert "USER THREADS" in thread_items[0]["finding"]
    # clear restores shipped
    d = triggers.delete_pack("threads")
    assert d["restored_builtin"] is True
    g2 = triggers.get_pack("threads")
    assert g2["source"] == "builtin"
    assert "USER THREADS" not in g2["body"]


def test_mtime_auto_invalidate(user_dir, monkeypatch):
    """Writing triggers.json outside clear_cache still reloads on next match."""
    # Warm cache
    assert triggers.load_table()
    # External write (simulates other process)
    path = triggers.user_triggers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "aicad.trigger/1",
        "triggers": [{
            "id": "thread_mention",
            "enabled": False,
            "purpose": "external disable",
            "match": {"keywords": [r"\btap\b"], "tools": [], "events": []},
            "inject": {"context_pack": "threads", "prerun_calcs": [], "exemplar": ""},
            "budget_tokens": 250,
            "cooldown_turns": 8,
        }],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    # Do NOT call clear_cache — mtime check must catch it.
    items, events = triggers.match_triggers("tap M6x1.0", cooldown_state={})
    assert not any(e["trigger_id"] == "thread_mention" for e in events)


def test_bad_keyword_rejected(user_dir):
    out = triggers.save_trigger({
        "id": "bad_re",
        "match": {"keywords": ["(unclosed"], "tools": [], "events": []},
        "inject": {"context_pack": "x", "prerun_calcs": [], "exemplar": ""},
    })
    assert out["ok"] is False
    assert "regex" in out["reason"].lower() or "bad keyword" in out["reason"].lower()


def test_rpc_list_shape(user_dir):
    from services.agent.worker_rpc import TRIGGERS_METHODS
    out = TRIGGERS_METHODS["triggers_list"]({}, lambda *a, **k: None)
    assert out["ok"] is True
    assert out["count"] >= 14
    assert out["triggers"][0]["id"]
