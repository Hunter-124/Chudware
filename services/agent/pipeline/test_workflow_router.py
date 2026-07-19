"""Tests for the workflow router (T-DP10).

All tests are deterministic (no model, no network, no port 1234).  The tests use
fixture tuning artifacts injected via ``workflow_router._inject_tuning_results`` so
gate.sh never depends on a live llama-server (the port-1234 test_vlm_track flake
is a known pre-existing exception; this module avoids it entirely).

Coverage:
  - classify_task: keyword rules for every TaskClass
  - ScaffoldLevel.tier_default: local→full (maximum), frontier→eco
  - best_scaffold_for_class: no-data fallback, measured winner, tied fallback
  - route: end-to-end with injected tuning
"""
from __future__ import annotations

import pytest

import services.agent.pipeline.workflow_router as wr
from services.agent.pipeline.workflow_router import (
    ScaffoldLevel,
    TaskClass,
    classify_task,
    classify_task_detailed,
    best_scaffold_for_class,
    route,
    _inject_tuning_results,
    _clear_tuning_cache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_tuning_cache():
    """Reset the module-level tuning cache before and after every test."""
    _clear_tuning_cache()
    yield
    _clear_tuning_cache()


# ---------------------------------------------------------------------------
# classify_task
# ---------------------------------------------------------------------------

class TestClassifyTask:
    def test_cam_program(self):
        assert classify_task("generate a CAM program for milling") == TaskClass.CAM_PROGRAM

    def test_cam_toolpath(self):
        assert classify_task("zero-gouge toolpath for the bracket") == TaskClass.CAM_PROGRAM

    def test_assembly(self):
        assert classify_task("design a two-part hinge assembly with clearance fit") == TaskClass.ASSEMBLY

    def test_enclosure(self):
        assert classify_task("clamshell enclosure with snap lid in PETG") == TaskClass.ENCLOSURE

    def test_enclosure_housing(self):
        assert classify_task("a housing for the PCB with screw bosses") == TaskClass.ENCLOSURE

    def test_modification(self):
        assert classify_task("modify the bushing to fit a 25mm shaft") == TaskClass.MODIFICATION

    def test_analysis(self):
        assert classify_task("run FEA on the bearing block under 500N load") == TaskClass.ANALYSIS

    def test_analysis_fea(self):
        assert classify_task("check safety factor with finite element analysis") == TaskClass.ANALYSIS

    def test_multi_feature(self):
        assert classify_task("flanged bushing with press-fit bore and bolt circle") == TaskClass.MULTI_FEATURE_PART

    def test_multi_feature_bearing(self):
        assert classify_task("bearing block with 6202 seat and mounting holes") == TaskClass.MULTI_FEATURE_PART

    def test_simple_part(self):
        assert classify_task("a basic mounting bracket") == TaskClass.SIMPLE_PART

    def test_simple_plate(self):
        assert classify_task("simple aluminum plate 100x50x5 mm") == TaskClass.SIMPLE_PART

    def test_unknown(self):
        assert classify_task("design a nice widget") == TaskClass.UNKNOWN

    def test_empty(self):
        assert classify_task("") == TaskClass.UNKNOWN

    def test_cam_beats_simple(self):
        # CAM rule must win over 'bracket' simple-part rule
        assert classify_task("CAM program for bracket milling") == TaskClass.CAM_PROGRAM

    def test_mcmaster_capability_label(self):
        cls, why = classify_task_detailed(
            "download a model from McMaster to test tooling for the parts library"
        )
        # Class may be UNKNOWN (capability surface) — reason must name the match.
        assert "parts-library" in why or "mcmaster" in why.lower()
        assert cls in (TaskClass.UNKNOWN, TaskClass.SIMPLE_PART, TaskClass.MODIFICATION)

    def test_route_reason_includes_class_match(self):
        r = route("generate a CAM program for milling", "local-24k", tuning={})
        assert r.task_class == TaskClass.CAM_PROGRAM
        assert "CAM" in r.reason or "machining" in r.reason.lower()

    def test_measure_capability_label(self):
        _cls, why = classify_task_detailed("measure mass properties of this bracket")
        # "bracket" may win as SIMPLE_PART before the measure rule — either is fine,
        # but the detailed path must return a non-empty why for a real request.
        assert why  # matched something


# ---------------------------------------------------------------------------
# ScaffoldLevel.tier_default
# ---------------------------------------------------------------------------

class TestTierDefault:
    def test_local_24k_is_full(self):
        assert ScaffoldLevel.tier_default("local-24k") == ScaffoldLevel.MAXIMUM

    def test_local_48k_is_full(self):
        assert ScaffoldLevel.tier_default("local-48k") == ScaffoldLevel.MAXIMUM

    def test_mid_is_full(self):
        # mid is not frontier → full
        assert ScaffoldLevel.tier_default("mid") == ScaffoldLevel.MAXIMUM

    def test_frontier_is_eco(self):
        assert ScaffoldLevel.tier_default("frontier") == ScaffoldLevel.ECO

    def test_frontier_200k_is_eco(self):
        assert ScaffoldLevel.tier_default("frontier-200k") == ScaffoldLevel.ECO


# ---------------------------------------------------------------------------
# best_scaffold_for_class
# ---------------------------------------------------------------------------

class TestBestScaffoldForClass:
    def test_no_tuning_data_local_returns_full(self):
        """No tuning data → tier default (local → full = maximum)."""
        lvl, reason = best_scaffold_for_class(TaskClass.SIMPLE_PART, "local-24k", tuning={})
        assert lvl == ScaffoldLevel.MAXIMUM
        assert "tier default" in reason

    def test_no_tuning_data_frontier_returns_eco(self):
        lvl, reason = best_scaffold_for_class(TaskClass.SIMPLE_PART, "frontier-200k", tuning={})
        assert lvl == ScaffoldLevel.ECO
        assert "tier default" in reason

    def test_tied_all_zero_falls_back_to_default(self):
        """All configs at win_rate=0 → tied, fall back to tier default (full)."""
        tuning = {
            "per_class_winners": {
                "simple_part|local": {
                    "scaffold_level": "eco",
                    "win_rate": 0.0,
                    "n": 2,
                    "tied": True,
                    "all_configs": {
                        "eco": {"win_rate": 0.0, "n": 1},
                        "maximum": {"win_rate": 0.0, "n": 1},
                    },
                }
            }
        }
        lvl, reason = best_scaffold_for_class(TaskClass.SIMPLE_PART, "local-24k", tuning=tuning)
        assert lvl == ScaffoldLevel.MAXIMUM   # falls back to local default
        assert "tied" in reason.lower()

    def test_measured_winner_eco(self):
        """A genuine measured winner (eco beats maximum) → eco selected."""
        tuning = {
            "per_class_winners": {
                "simple_part|local": {
                    "scaffold_level": "eco",
                    "win_rate": 0.8,
                    "n": 5,
                    "tied": False,
                    "all_configs": {
                        "eco": {"win_rate": 0.8, "n": 5},
                        "maximum": {"win_rate": 0.4, "n": 5},
                    },
                }
            }
        }
        lvl, reason = best_scaffold_for_class(TaskClass.SIMPLE_PART, "local-24k", tuning=tuning)
        assert lvl == ScaffoldLevel.ECO
        assert "measured winner" in reason
        assert "0.80" in reason

    def test_measured_winner_maximum(self):
        """maximum wins for multi-feature tasks."""
        tuning = {
            "per_class_winners": {
                "multi_feature_part|local": {
                    "scaffold_level": "maximum",
                    "win_rate": 0.6,
                    "n": 3,
                    "tied": False,
                    "all_configs": {
                        "eco": {"win_rate": 0.1, "n": 3},
                        "maximum": {"win_rate": 0.6, "n": 3},
                    },
                }
            }
        }
        lvl, reason = best_scaffold_for_class(
            TaskClass.MULTI_FEATURE_PART, "local-24k", tuning=tuning
        )
        assert lvl == ScaffoldLevel.MAXIMUM
        assert "measured winner" in reason

    def test_missing_key_falls_back(self):
        """Tuning data exists but not for this class → tier default."""
        tuning = {
            "per_class_winners": {
                "assembly|local": {
                    "scaffold_level": "eco",
                    "win_rate": 0.5,
                    "n": 2,
                    "tied": False,
                }
            }
        }
        lvl, reason = best_scaffold_for_class(
            TaskClass.SIMPLE_PART, "local-24k", tuning=tuning
        )
        assert lvl == ScaffoldLevel.MAXIMUM
        assert "no tuning data" in reason

    def test_unknown_scaffold_level_falls_back(self):
        """Corrupted level string in tuning data → fall back."""
        tuning = {
            "per_class_winners": {
                "simple_part|local": {
                    "scaffold_level": "nonsense",
                    "win_rate": 0.9,
                    "n": 5,
                    "tied": False,
                }
            }
        }
        lvl, reason = best_scaffold_for_class(TaskClass.SIMPLE_PART, "local-24k", tuning=tuning)
        assert lvl == ScaffoldLevel.MAXIMUM
        assert "unknown scaffold level" in reason


# ---------------------------------------------------------------------------
# route (end-to-end)
# ---------------------------------------------------------------------------

class TestRoute:
    def test_cam_local_no_tuning(self):
        """CAM request on local-24k with no tuning → full (maximum)."""
        r = route("generate G-code for the bracket", "local-24k", tuning={})
        assert r.task_class == TaskClass.CAM_PROGRAM
        assert r.scaffold_level == ScaffoldLevel.MAXIMUM
        assert r.tier == "local-24k"
        assert "tier default" in r.reason

    def test_enclosure_local_measured_winner(self):
        """Measured eco winner for enclosure on local → eco selected."""
        tuning = {
            "per_class_winners": {
                "enclosure|local": {
                    "scaffold_level": "eco",
                    "win_rate": 0.75,
                    "n": 4,
                    "tied": False,
                }
            }
        }
        r = route("design a clamshell enclosure", "local-24k", tuning=tuning)
        assert r.task_class == TaskClass.ENCLOSURE
        assert r.scaffold_level == ScaffoldLevel.ECO
        assert "measured winner" in r.reason

    def test_unknown_class_local_defaults_full(self):
        """Unknown class → full (maximum) for local."""
        r = route("make something", "local-48k", tuning={})
        assert r.task_class == TaskClass.UNKNOWN
        assert r.scaffold_level == ScaffoldLevel.MAXIMUM

    def test_unknown_class_frontier_defaults_eco(self):
        """Unknown class → eco (minimal) for frontier."""
        r = route("make something", "frontier-200k", tuning={})
        assert r.task_class == TaskClass.UNKNOWN
        assert r.scaffold_level == ScaffoldLevel.ECO

    def test_result_to_dict(self):
        r = route("mill this bracket", "local-24k", tuning={})
        d = r.to_dict()
        assert d["schema"] == wr.ROUTER_SCHEMA
        assert d["task_class"] == TaskClass.CAM_PROGRAM.value
        assert d["scaffold_level"] == ScaffoldLevel.MAXIMUM.value
        assert d["tier"] == "local-24k"
        assert "reason" in d

    def test_request_snippet_truncated(self):
        long_req = "a " * 200
        r = route(long_req, "local-24k", tuning={})
        assert len(r.request_snippet) <= 120

    def test_module_level_tuning_injection(self):
        """_inject_tuning_results drives route() without passing tuning kwarg."""
        _inject_tuning_results({
            "per_class_winners": {
                "simple_part|local": {
                    "scaffold_level": "eco",
                    "win_rate": 0.9,
                    "n": 3,
                    "tied": False,
                }
            }
        })
        r = route("simple bracket", "local-24k")
        assert r.scaffold_level == ScaffoldLevel.ECO
