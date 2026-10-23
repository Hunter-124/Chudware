"""Tests for services/agent/routing.py — validation authority for aicad.routing/1.

Covers: schema/semantic validation, resolve() against symbolic tiers + pins,
warn-never-block advisories, the view() GUI/delegation contract shape, round-trip
save/load, and the get/set CLI seam the C++ GUI shells.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from services.agent import routing


REPO = Path(__file__).resolve().parents[2]


def _doc(sources=None, assignments=None):
    d = {
        "schema": routing.SCHEMA,
        "sources": sources if sources is not None else [
            {"id": "local", "kind": "llamacpp",
             "models": [{"id": "qwen-7b", "tier": "local"},
                        {"id": "qwen-32b", "tier": "local_large"}]},
        ],
        "assignments": assignments if assignments is not None else {
            "overseer": ["strongest"],
            "sketcher": ["cheap_local", "strong"],
            "default": ["cheap_local", "strong"],
        },
    }
    return d


# --- shipped file ----------------------------------------------------------

def test_shipped_routing_yaml_is_valid_and_covers_all_roles():
    data = routing.load()  # the shipped routing.yaml
    roles_dir = REPO / "services" / "agent" / "roles"
    role_ids = set()
    for f in roles_dir.glob("*.yaml"):
        for line in f.read_text().splitlines():
            if line.startswith("id:"):
                role_ids.add(line.split(":", 1)[1].strip())
                break
    missing = role_ids - set(data["assignments"])
    assert not missing, f"routing.yaml missing assignment rows for: {missing}"
    extra = set(data["assignments"]) - role_ids - {"default"}
    assert not extra, f"routing.yaml has assignments for unknown roles: {extra}"
    assert "default" in data["assignments"]


# --- validation ------------------------------------------------------------

def test_valid_doc_passes():
    routing.validate(_doc())


def test_bad_schema_rejected():
    d = _doc()
    d["schema"] = "nope/1"
    with pytest.raises(routing.RoutingError):
        routing.validate(d)


def test_consumer_oauth_kind_rejected():
    # No consumer-OAuth source kinds allowed (Pillar 12).
    d = _doc(sources=[{"id": "x", "kind": "claude_oauth",
                       "models": [{"id": "m", "tier": "frontier"}]}])
    with pytest.raises(routing.RoutingError):
        routing.validate(d)


def test_allowed_kinds_accepted():
    for kind in routing.ALLOWED_SOURCE_KINDS:
        d = _doc(sources=[{"id": "s", "kind": kind,
                           "models": [{"id": "m", "tier": "strong"}]}])
        routing.validate(d)


def test_duplicate_source_id_rejected():
    d = _doc(sources=[
        {"id": "dup", "kind": "llamacpp", "models": [{"id": "a", "tier": "local"}]},
        {"id": "dup", "kind": "ollama", "models": [{"id": "b", "tier": "local"}]},
    ])
    with pytest.raises(routing.RoutingError):
        routing.validate(d)


def test_missing_default_assignment_rejected():
    d = _doc(assignments={"overseer": ["strongest"]})
    with pytest.raises(routing.RoutingError):
        routing.validate(d)


def test_empty_pref_list_rejected():
    d = _doc(assignments={"overseer": [], "default": ["strong"]})
    with pytest.raises(routing.RoutingError):
        routing.validate(d)


def test_unknown_symbolic_token_rejected():
    d = _doc(assignments={"overseer": ["gigabrain"], "default": ["strong"]})
    with pytest.raises(routing.RoutingError):
        routing.validate(d)


def test_pin_to_missing_source_rejected():
    d = _doc(assignments={"overseer": ["m@ghost"], "default": ["strong"]})
    with pytest.raises(routing.RoutingError):
        routing.validate(d)


def test_pin_to_existing_source_ok():
    d = _doc(assignments={"overseer": ["qwen-32b@local"], "default": ["strong"]})
    routing.validate(d)
    assert routing.resolve(d, "overseer") == ("qwen-32b", "local")


def test_bad_tier_rejected():
    d = _doc(sources=[{"id": "s", "kind": "llamacpp",
                       "models": [{"id": "m", "tier": "megatier"}]}])
    with pytest.raises(routing.RoutingError):
        routing.validate(d)




# --- resolution ------------------------------------------------------------

def test_strongest_picks_highest_tier():
    d = _doc()
    # only local source: strongest = qwen-32b (local_large > local)
    assert routing.resolve(d, "overseer") == ("qwen-32b", "local")


def test_strongest_prefers_cloud_frontier_when_present():
    d = _doc(sources=[
        {"id": "local", "kind": "llamacpp", "models": [{"id": "qwen-7b", "tier": "local"}]},
        {"id": "cloud", "kind": "anthropic", "models": [{"id": "sonnet", "tier": "frontier"}]},
    ], assignments={"overseer": ["strongest"], "default": ["cheap_local", "strong"]})
    assert routing.resolve(d, "overseer") == ("sonnet", "cloud")


def test_cheap_local_prefers_local_source():
    d = _doc(sources=[
        {"id": "local", "kind": "ollama", "models": [{"id": "qwen-7b", "tier": "local"}]},
        {"id": "cloud", "kind": "openai", "models": [{"id": "gpt", "tier": "frontier"}]},
    ], assignments={"sketcher": ["cheap_local", "strong"], "default": ["cheap_local"]})
    assert routing.resolve(d, "sketcher") == ("qwen-7b", "local")


def test_fallthrough_when_no_local_source():
    # cheap_local unmet (no local source) -> falls through to next token.
    d = _doc(sources=[
        {"id": "cloud", "kind": "openai", "models": [{"id": "gpt", "tier": "frontier"}]},
    ], assignments={"sketcher": ["cheap_local", "strong"], "default": ["strong"]})
    assert routing.resolve(d, "sketcher") == ("gpt", "cloud")


def test_unlisted_role_uses_default():
    d = _doc()
    assert routing.resolve(d, "some_new_role") == routing.resolve(d, "default")


def test_single_source_collapses_all_roles():
    # §14: one source -> all roles resolve onto it.
    d = _doc()
    assert routing.resolve(d, "overseer") is not None
    assert routing.resolve(d, "sketcher") is not None


# --- warnings (warn, never block) ------------------------------------------

def test_overseer_on_small_local_warns():
    d = _doc(sources=[{"id": "local", "kind": "llamacpp",
                       "models": [{"id": "qwen-4b", "tier": "local"}]}],
             assignments={"overseer": ["strongest"], "default": ["cheap_local"]})
    warns = routing.warnings_for(d, "overseer")
    assert warns and "gates the whole mission" in warns[0]
    # ...but validation still passes — warn, never block.
    routing.validate(d)


def test_no_warning_for_overseer_on_frontier():
    d = _doc(sources=[{"id": "cloud", "kind": "anthropic",
                       "models": [{"id": "sonnet", "tier": "frontier"}]}],
             assignments={"overseer": ["strongest"], "default": ["strong"]})
    assert routing.warnings_for(d, "overseer") == []


def test_unsatisfiable_preference_warns():
    d = _doc(sources=[{"id": "cloud", "kind": "openai",
                       "models": [{"id": "gpt", "tier": "frontier"}]}],
             assignments={"sketcher": ["cheap_local"], "default": ["strong"]})
    # sketcher only wants cheap_local, none exists -> resolve None -> warning.
    assert routing.resolve(d, "sketcher") is None
    assert routing.warnings_for(d, "sketcher")


# --- view (GUI / delegation contract) -------------------------------------

def test_view_shape():
    v = routing.view(_doc())
    assert v["schema"] == routing.SCHEMA
    assert "sources" in v and "assignments" in v and "resolved" in v
    r = v["resolved"]["overseer"]
    assert set(r) == {"model", "source", "warnings"}
    assert r["model"] == "qwen-32b"


# --- round-trip + CLI ------------------------------------------------------

def test_save_load_roundtrip(tmp_path):
    d = _doc()
    p = routing.save(d, tmp_path / "routing.yaml")
    back = routing.load(p)
    assert back["assignments"] == d["assignments"]


def test_save_rejects_invalid_before_disk(tmp_path):
    bad = _doc()
    bad["schema"] = "bad"
    target = tmp_path / "routing.yaml"
    with pytest.raises(routing.RoutingError):
        routing.save(bad, target)
    assert not target.exists()  # nothing written


def _run_cli(args, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "services.agent.routing", *args],
        input=stdin, capture_output=True, text=True, cwd=REPO,
    )


def test_cli_get_emits_view():
    r = _run_cli(["get"])
    assert r.returncode == 0, r.stderr
    v = json.loads(r.stdout)
    assert v["schema"] == routing.SCHEMA
    assert "resolved" in v


def test_cli_set_writes_and_echoes_view(tmp_path):
    p = tmp_path / "routing.yaml"
    model = {
        "sources": [{"id": "local", "kind": "llamacpp",
                     "models": [{"id": "qwen-7b", "tier": "local"}]}],
        "assignments": {"overseer": ["strongest"], "default": ["cheap_local"]},
    }
    r = _run_cli(["set", "--path", str(p)], stdin=json.dumps(model))
    assert r.returncode == 0, r.stdout + r.stderr
    v = json.loads(r.stdout)
    assert v["resolved"]["overseer"]["model"] == "qwen-7b"
    assert p.exists()
    routing.load(p)  # written file re-validates


def test_cli_set_rejects_invalid(tmp_path):
    p = tmp_path / "routing.yaml"
    bad = {"sources": [{"id": "x", "kind": "claude_oauth",
                        "models": [{"id": "m", "tier": "frontier"}]}],
           "assignments": {"default": ["strong"]}}
    r = _run_cli(["set", "--path", str(p)], stdin=json.dumps(bad))
    assert r.returncode == 2
    out = json.loads(r.stdout)
    assert out["ok"] is False
    assert not p.exists()
