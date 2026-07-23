"""Tests for services/agent/effort.py (T-W28 harness effort settings).

Covers the DoD test bullets: preset resolution, override precedence, warn
conditions — plus tier aliasing, the freedom rule (never clamp), the ledger
snapshot event, the shared tiers.resolve_effort path, and CLI-shape view().

Offline discipline: no live model, no real config.json — every test passes an
explicit ``config=`` dict; persistence tests monkeypatch services.paths.CONFIG.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.agent import effort
from services.agent import tiers as tiers_mod
from services.agent.effort import (
    EffortError,
    LEVELS,
    load_presets,
    normalize_tier,
    record_effort_snapshot,
    resolve_effort,
    save_effort_settings,
    view,
    warnings_for,
)
from services.agent.tiers import load_all_tiers


def _cfg(level="standard", mode="simple", overrides=None):
    return {"ai_effort": {"level": level, "mode": mode, "overrides": overrides or {}}}


# --------------------------------------------------------------------------- presets file

def test_presets_file_loads_and_has_all_levels():
    data = load_presets()
    assert data["schema"] == "aicad.effort/1"
    assert set(LEVELS) <= set(data["presets"])
    for lvl in LEVELS:
        spec = data["presets"][lvl]
        assert spec.get("label")
        assert spec.get("hint"), f"{lvl}: simple mode needs a cost/benefit hint"


# --------------------------------------------------------------------------- baseline / standard

def test_standard_preset_is_shipped_baseline_inverted_dial():
    """Standard = shipped defaults untouched: the INVERTED dial survives."""
    local = resolve_effort("local-24k", config=_cfg())
    frontier = resolve_effort("frontier", config=_cfg())
    assert local.values["deliberation"] == "full"        # local: tokens free
    assert frontier.values["deliberation"] == "minimal"  # frontier: tokens cost
    assert all(src == "shipped" for src in local.sources.values())
    assert all(src == "shipped" for src in frontier.sources.values())


def test_baseline_numbers_derive_from_shipped_constants():
    from services.agent.ladder import MAX_BOUNCES
    from services.agent.delegation import DEPTH_CAP
    from services.agent.advisor.failures import _DEFAULT_BUDGETS

    prof = resolve_effort("mid", config=_cfg())
    v = prof.values
    assert v["ladder_max_bounces"] == MAX_BOUNCES
    assert v["delegation_depth"] == DEPTH_CAP
    assert v["failure_max_attempts"] == {
        k: b["max_attempts"] for k, b in _DEFAULT_BUDGETS.items()}
    # history budget comes straight from tiers.yaml
    assert v["history_budget_tokens"] == load_all_tiers()["mid"].budgets.history
    # verbosity mirrors tiers.yaml advisory_verbosity
    assert v["context_pack_verbosity"] == load_all_tiers()["mid"].advisory_verbosity


def test_ladder_and_council_follow_deliberation():
    assert resolve_effort("local-24k", config=_cfg()).values["ladder"] == "mandatory"
    assert resolve_effort("frontier", config=_cfg()).values["ladder"] == "optional"
    assert resolve_effort("local-48k", config=_cfg()).values["council"] == "always"
    assert resolve_effort("mid", config=_cfg()).values["council"] == "risk_gated"


# --------------------------------------------------------------------------- preset resolution

def test_eco_preset_dials_everything_down():
    for tier in ("local-24k", "frontier"):
        prof = resolve_effort(tier, config=_cfg(level="eco"))
        assert prof.values["deliberation"] == "minimal"
        assert prof.values["council"] == "off"
        assert prof.values["trigger_injection"] is False
        assert prof.values["delegation_depth"] == 1
        assert prof.sources["council"] == "preset:eco"
        # untouched knobs keep shipped provenance
        assert prof.sources["preflight"] == "shipped"
        assert prof.values["preflight"] is True  # eco never cuts safety nets


def test_deliberate_preset_tier_scoped_overrides():
    mid = resolve_effort("mid", config=_cfg(level="deliberate"))
    frontier = resolve_effort("frontier", config=_cfg(level="deliberate"))
    local = resolve_effort("local-24k", config=_cfg(level="deliberate"))
    assert mid.values["deliberation"] == "full"          # raised from standard
    assert frontier.values["deliberation"] == "standard" # raised from minimal
    assert local.values["deliberation"] == "full"        # already full (shipped)
    assert local.sources["deliberation"] == "shipped"
    assert mid.sources["deliberation"] == "preset:deliberate"
    assert frontier.values["ladder"] == "mandatory"
    assert frontier.values["council"] == "always"


def test_maximum_preset_full_everywhere():
    prof = resolve_effort("frontier", config=_cfg(level="maximum"))
    assert prof.values["deliberation"] == "full"
    assert prof.values["ponder_round2_tokens"] == 1500   # reuses round-1 budget
    assert prof.values["overseer_cadence"] == "every_trigger"


# --------------------------------------------------------------------------- override precedence

def test_user_override_beats_preset_beats_shipped():
    cfg = _cfg(level="eco", overrides={"frontier": {"council": "always"}})
    prof = resolve_effort("frontier", config=cfg)
    assert prof.values["council"] == "always"            # user wins over eco's off
    assert prof.sources["council"] == "override"
    assert prof.values["deliberation"] == "minimal"      # rest of eco intact
    assert prof.sources["deliberation"] == "preset:eco"


def test_overrides_are_per_tier_scoped():
    cfg = _cfg(overrides={"local-24k": {"council_seats": 6}})
    assert resolve_effort("local-24k", config=cfg).values["council_seats"] == 6
    assert resolve_effort("local-48k", config=cfg).values["council_seats"] == 4


def test_unknown_knob_or_bad_enum_is_structural_error():
    with pytest.raises(EffortError):
        resolve_effort("mid", config=_cfg(overrides={"mid": {"nonsense_knob": 1}}))
    with pytest.raises(EffortError):
        resolve_effort("mid", config=_cfg(overrides={"mid": {"council": "sometimes"}}))
    with pytest.raises(EffortError):
        resolve_effort("mid", config=_cfg(level="turbo"))


# --------------------------------------------------------------------------- tier aliases

def test_tier_alias_normalization():
    tiers = load_all_tiers()
    assert normalize_tier("frontier-200k", tiers) == "frontier"   # gym alias
    assert normalize_tier("local-24k", tiers) == "local-24k"
    with pytest.raises(EffortError):
        normalize_tier("quantum-tier", tiers)
    assert resolve_effort("frontier-200k", config=_cfg()).tier == "frontier"


# --------------------------------------------------------------------------- freedom rule + warnings

def test_depth_above_contract_cap_warns_but_never_clamps():
    cfg = _cfg(overrides={"mid": {"delegation_depth": 7}})
    prof = resolve_effort("mid", config=cfg)
    assert prof.values["delegation_depth"] == 7          # stored verbatim
    assert any("§12" in w and "cap" in w for w in prof.warnings)


def test_preflight_off_and_manual_overseer_warn_with_why():
    cfg = _cfg(overrides={"mid": {"preflight": False,
                                  "overseer_cadence": "manual_only"}})
    prof = resolve_effort("mid", config=cfg)
    joined = " ".join(prof.warnings)
    assert "preflight" in joined and "§10" in joined
    assert "manual-only" in joined and "§11" in joined


def test_history_budget_invariant_warning():
    tiers = load_all_tiers()
    t = tiers["local-24k"]
    too_big = t.context_tokens  # guaranteed to break slots+headroom ≤ context
    cfg = _cfg(overrides={"local-24k": {"history_budget_tokens": too_big}})
    prof = resolve_effort("local-24k", config=cfg)
    assert prof.values["history_budget_tokens"] == too_big
    assert any("§8 invariant" in w for w in prof.warnings)


def test_council_on_minimal_dial_mismatch_warns_and_eco_stays_quiet():
    cfg = _cfg(overrides={"frontier": {"council": "always"}})
    prof = resolve_effort("frontier", config=cfg)  # shipped frontier = minimal
    assert any("minimal" in w for w in prof.warnings)
    # eco turns councils OFF → that particular mismatch warning must not fire
    eco = resolve_effort("frontier", config=_cfg(level="eco"))
    assert not any("councils are on" in w for w in eco.warnings)


def test_maximum_on_frontier_costs_warning():
    prof = resolve_effort("frontier", config=_cfg(level="maximum"))
    assert any("cost" in w.lower() for w in prof.warnings)


def test_standard_shipped_config_produces_no_warnings():
    for tier in load_all_tiers():
        assert resolve_effort(tier, config=_cfg()).warnings == []


# --------------------------------------------------------------------------- ledger snapshot

class _FakeLedger:
    def __init__(self):
        self.events = []

    def append(self, actor, kind, payload):
        ev = {"seq": len(self.events), "actor": actor, "kind": kind,
              "payload": payload}
        self.events.append(ev)
        return ev


def test_record_effort_snapshot_is_replayable():
    led = _FakeLedger()
    ev = record_effort_snapshot(led, active_tier="mid", config=_cfg(level="deliberate"))
    assert ev["kind"] == "effort_profile"
    assert ev["actor"] == "harness"
    p = ev["payload"]
    assert p["schema"] == "aicad.effort/1"
    assert p["level"] == "deliberate"
    assert p["active_tier"] == "mid"
    assert set(p["tiers"]) == set(load_all_tiers())
    # replayable: values AND provenance for every knob of every tier
    for tp in p["tiers"].values():
        assert set(tp["values"]) == set(effort.KNOBS)
        assert set(tp["sources"]) == set(effort.KNOBS)
    # payload must be JSON-serializable (it goes into the ledger file)
    json.dumps(p)


# --------------------------------------------------------------------------- one shared path

def test_tiers_resolve_effort_is_the_same_path():
    via_tiers = tiers_mod.resolve_effort("local-48k", config=_cfg(level="eco"))
    direct = resolve_effort("local-48k", config=_cfg(level="eco"))
    assert via_tiers.values == direct.values
    assert via_tiers.sources == direct.sources


# --------------------------------------------------------------------------- view (GUI seam)

def test_view_shape_for_gui():
    v = view(config=_cfg(level="eco"))
    assert v["level"] == "eco"
    assert [lv["id"] for lv in v["levels"]] == list(LEVELS)
    assert all(lv["hint"] for lv in v["levels"])
    assert set(v["tiers"]) == set(load_all_tiers())
    for name, meta in v["knobs"].items():
        assert meta["group"] in v["groups"]
    json.dumps(v)  # GUI gets this over the CLI pipe


# --------------------------------------------------------------------------- persistence

def test_save_effort_settings_roundtrip(tmp_path, monkeypatch):
    from services import paths as engine_paths
    monkeypatch.setattr(engine_paths, "CONFIG", tmp_path / "config.json")

    saved = save_effort_settings({
        "level": "deliberate", "mode": "advanced",
        "overrides": {"frontier-200k": {"council": "off"}},  # alias normalized
    })
    assert saved["overrides"] == {"frontier": {"council": "off"}}
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["ai_effort"]["level"] == "deliberate"
    # read-merge-write: unrelated keys survive a later partial save
    from services.agent.config import save_config
    save_config({"provider": "mock"})
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["ai_effort"]["level"] == "deliberate"
    assert on_disk["provider"] == "mock"


def test_save_rejects_structural_garbage_but_not_absurd_values(tmp_path, monkeypatch):
    from services import paths as engine_paths
    monkeypatch.setattr(engine_paths, "CONFIG", tmp_path / "config.json")

    with pytest.raises(EffortError):
        save_effort_settings({"level": "warp-speed"})
    # absurd-but-typed values save fine (freedom rule: warn, never block)
    save_effort_settings({"level": "eco",
                          "overrides": {"mid": {"delegation_depth": 99}}})
    prof = resolve_effort(
        "mid", config=json.loads((tmp_path / "config.json").read_text()))
    assert prof.values["delegation_depth"] == 99
    assert prof.warnings  # warned, not blocked
