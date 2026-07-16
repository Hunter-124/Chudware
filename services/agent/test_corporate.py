"""test_corporate.py — aicad.corporate/1 corporate org mode.

Proofs:
  1. Org build from a mixed task set → multi-dept chart with heads + channels.
  2. Worker → director message is REJECTED.
  3. Meeting digest cadence fires every meeting_cadence completions.
  4. Head spawn respects max_subagent_depth.
  5. Flat mode untouched when corporate_mode is off.
  6. Config accessors honour defaults + overrides.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.agent import config as agent_config
from services.agent.corporate import (
    CHANNEL_HEADS,
    CORPORATE_SCHEMA,
    OrgChart,
    build_corporate_org,
    clear_org_charts,
    derive_departments,
    drop_org_chart,
    get_org_chart,
    should_use_corporate,
)
from services.agent.mission_dispatch import MissionDispatch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mixed_tasks() -> list[dict]:
    """Tasks spanning geometry, simulation, manufacturing, documentation."""
    return [
        {"id": "t1", "role": "feature_modeler", "task_class": "multi_feature_part",
         "brief": "model the bracket body"},
        {"id": "t2", "role": "sketcher", "stage": "sketch",
         "brief": "profile the base flange"},
        {"id": "t3", "role": "sim_engineer", "task_class": "analysis",
         "brief": "static FEA on the bracket"},
        {"id": "t4", "role": "cam_programmer", "task_class": "cam_program",
         "brief": "3-axis toolpaths"},
        {"id": "t5", "role": "reviewer", "stage": "deliver",
         "brief": "verify acceptance criteria"},
        {"id": "t6", "role": "spec_writer", "task_class": "report",
         "brief": "write the design report"},
    ]


def _cfg_on(**corp_overrides) -> dict:
    corp = {
        "max_departments": 3,
        "team_leads": True,
        "meeting_cadence": 5,
        "director_review": True,
    }
    corp.update(corp_overrides)
    return {
        "agent": {
            "corporate_mode": True,
            "corporate": corp,
            "max_subagent_depth": 2,
        }
    }


def _cfg_off() -> dict:
    return {"agent": {"corporate_mode": False}}


@pytest.fixture(autouse=True)
def _clean_charts():
    clear_org_charts()
    yield
    clear_org_charts()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestCorporateConfig:
    def test_defaults_when_absent(self):
        assert agent_config.corporate_mode_enabled({}) is False
        c = agent_config.get_corporate_config({})
        assert c["max_departments"] == 3
        assert c["team_leads"] is True
        assert c["meeting_cadence"] == 5
        assert c["director_review"] is True

    def test_overrides(self):
        cfg = _cfg_on(max_departments=2, team_leads=False,
                      meeting_cadence=3, director_review=False)
        assert agent_config.corporate_mode_enabled(cfg) is True
        c = agent_config.get_corporate_config(cfg)
        assert c["max_departments"] == 2
        assert c["team_leads"] is False
        assert c["meeting_cadence"] == 3
        assert c["director_review"] is False

    def test_clamps(self):
        cfg = {"agent": {"corporate": {"max_departments": 0, "meeting_cadence": -1}}}
        c = agent_config.get_corporate_config(cfg)
        assert c["max_departments"] >= 1
        assert c["meeting_cadence"] >= 1


# ---------------------------------------------------------------------------
# Department derivation + org build
# ---------------------------------------------------------------------------

class TestOrgBuild:
    def test_mixed_tasks_multiple_departments(self):
        depts = derive_departments(_mixed_tasks(), max_departments=3)
        ids = {d["id"] for d in depts}
        assert len(depts) == 3
        # Geometry + sim + manufacturing are highest priority; docs may merge.
        assert "geometry" in ids
        assert "simulation" in ids or "manufacturing" in ids

    def test_cap_merges_overflow(self):
        depts = derive_departments(_mixed_tasks(), max_departments=2)
        assert len(depts) == 2

    def test_build_from_tasks_seats_heads_and_channels(self):
        chart = OrgChart(
            "r_corp_build",
            cfg=_cfg_on(),
            mission_goals=["bracket with FEA and CAM"],
            max_subagent_depth=2,
        )
        snap = chart.build_from_tasks(_mixed_tasks())
        assert snap["schema"] == CORPORATE_SCHEMA
        assert snap["active"] is True
        assert snap["chart_revision"] == 1
        assert len(snap["departments"]) >= 2
        # Director + one head per dept
        ranks = {s["rank"] for s in snap["seats"]}
        assert "director" in ranks
        assert "head" in ranks
        # Channels: heads + one dept channel per department
        assert CHANNEL_HEADS in snap["channels"]
        assert snap["channels"][CHANNEL_HEADS]  # director + heads
        for d in snap["departments"]:
            ch = f"dept:{d['id']}"
            assert ch in snap["channels"]
            assert d["head_id"] in snap["channels"][ch]

    def test_single_dept_not_active(self):
        chart = OrgChart("r_one", cfg=_cfg_on())
        snap = chart.build_from_tasks([
            {"role": "feature_modeler"},
            {"role": "sketcher"},
        ])
        assert snap["active"] is False

    def test_build_corporate_org_entry(self):
        out = build_corporate_org(
            "r_entry",
            _mixed_tasks(),
            cfg=_cfg_on(),
            mission_goals=["ship the bracket"],
            force=True,
        )
        assert out["ok"] is True
        assert out["active"] is True
        assert out["chart"] is not None
        assert out["chart"]["schema"] == CORPORATE_SCHEMA


# ---------------------------------------------------------------------------
# Communication enforcement
# ---------------------------------------------------------------------------

class TestCommunication:
    def _ready_chart(self) -> OrgChart:
        chart = OrgChart(
            "r_comm",
            cfg=_cfg_on(meeting_cadence=5),
            mission_goals=["geometry", "cam program", "fea study"],
            max_subagent_depth=2,
        )
        chart.build_from_tasks(_mixed_tasks())
        # Spawn one worker under geometry head
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        head_id = geo["head_id"]
        w = chart.spawn_worker(
            head_id=head_id,
            role="feature_modeler",
            brief="model body",
            worker_id="worker_geo_1",
        )
        assert w["ok"], w
        return chart

    def test_worker_to_director_rejected(self):
        chart = self._ready_chart()
        # Direct heads channel
        r = chart.post_message(
            "worker_geo_1", CHANNEL_HEADS, "hey director, help",
            kind="question",
        )
        assert r["ok"] is False
        assert r.get("status") == "rejected"
        assert "director" in r["error"].lower() or "forbidden" in r["error"].lower()

        # Meta targeting director
        r2 = chart.post_message(
            "worker_geo_1",
            chart.dept_channel("geometry"),
            "please escalate",
            kind="question",
            meta={"to": chart.director_id},
        )
        assert r2["ok"] is False
        assert r2.get("status") == "rejected"

        # Invented director channel name
        r3 = chart.post_message(
            "worker_geo_1", "director", "ping", kind="info",
        )
        assert r3["ok"] is False

    def test_worker_may_post_dept_channel(self):
        chart = self._ready_chart()
        r = chart.post_message(
            "worker_geo_1",
            chart.dept_channel("geometry"),
            "extrude complete",
            kind="progress",
        )
        assert r["ok"] is True
        assert r["message"]["channel"] == "dept:geometry"

    def test_head_may_post_heads_channel(self):
        chart = self._ready_chart()
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        r = chart.post_message(
            geo["head_id"], CHANNEL_HEADS, "geometry status green", kind="info",
        )
        assert r["ok"] is True

    def test_worker_cannot_read_heads(self):
        chart = self._ready_chart()
        r = chart.read_channel(CHANNEL_HEADS, agent_id="worker_geo_1")
        assert r["ok"] is False
        assert r.get("status") == "rejected"


# ---------------------------------------------------------------------------
# Meeting cadence + director review
# ---------------------------------------------------------------------------

class TestMeetings:
    def test_cadence_triggers_digest(self):
        chart = OrgChart(
            "r_meet",
            cfg=_cfg_on(meeting_cadence=3, director_review=True),
            mission_goals=["bracket geometry complete"],
            max_subagent_depth=2,
            meeting_cadence=3,
        )
        chart.build_from_tasks(_mixed_tasks())
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        head_id = geo["head_id"]
        for i in range(3):
            chart.spawn_worker(
                head_id=head_id,
                role="feature_modeler",
                worker_id=f"w_geo_{i}",
            )

        # 1, 2 — no meeting yet
        r1 = chart.record_worker_completion(
            "geometry", worker_id="w_geo_0", summary="done sketch",
        )
        assert r1["ok"]
        assert r1["meeting"] is None
        assert r1["completions_since_meeting"] == 1
        assert r1["posted"] is True
        assert "post_error" not in r1


        r2 = chart.record_worker_completion(
            "geometry", worker_id="w_geo_1", summary="done boss extrude",
        )
        assert r2["meeting"] is None
        assert r2["completions_since_meeting"] == 2

        # 3rd hits cadence
        r3 = chart.record_worker_completion(
            "geometry", worker_id="w_geo_2", summary="done fillets complete",
        )
        assert r3["meeting"] is not None
        digest = r3["meeting"]["digest"]
        assert digest["dept_id"] == "geometry"
        assert digest["trigger"] == "completion"
        assert "done" in digest
        assert "in_progress" in digest
        assert "blocked" in digest
        assert "risks" in digest
        # Counter reset
        assert r3["completions_since_meeting"] == 0
        # Digest posted to heads
        assert r3["meeting"]["post"]["ok"] is True
        assert r3["meeting"]["post"]["message"]["channel"] == CHANNEL_HEADS
        assert r3["meeting"]["post"]["message"]["kind"] == "digest"

    def test_blockage_triggers_meeting_immediately(self):
        chart = OrgChart(
            "r_block",
            cfg=_cfg_on(meeting_cadence=99),
            meeting_cadence=99,
            max_subagent_depth=2,
        )
        chart.build_from_tasks(_mixed_tasks())
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        chart.spawn_worker(
            head_id=geo["head_id"], role="sketcher", worker_id="w_b",
        )
        r = chart.record_worker_completion(
            "geometry",
            worker_id="w_b",
            summary="blocked on missing material",
            blocked=True,
        )
        assert r["meeting"] is not None
        assert r["meeting"]["digest"]["trigger"] == "blockage"

    def test_completion_surfaces_rejected_post(self):
        """Unseated worker_id: completion still counts, but posted=False + post_error."""
        chart = OrgChart(
            "r_post_err",
            cfg=_cfg_on(meeting_cadence=99),
            meeting_cadence=99,
            max_subagent_depth=2,
        )
        chart.build_from_tasks(_mixed_tasks())
        r = chart.record_worker_completion(
            "geometry",
            worker_id="not_on_chart",
            summary="ghost progress",
        )
        assert r["ok"] is True
        assert r["total_completions"] == 1
        assert r["posted"] is False
        assert "post_error" in r
        assert r["post"]["ok"] is False


    def test_director_review_on_blockage(self):
        chart = OrgChart(
            "r_rev",
            cfg=_cfg_on(director_review=True),
            mission_goals=["bracket FEA pass"],
            director_review=True,
            max_subagent_depth=2,
        )
        chart.build_from_tasks(_mixed_tasks())
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        meeting = chart.hold_meeting(
            geo["id"],
            done=[],
            in_progress=["fillets"],
            blocked=["regen failure on cut-extrude"],
            risks=["unclear stock size"],
            trigger="blockage",
        )
        assert meeting["ok"]
        rev = meeting["director_review"]
        assert rev is not None
        assert rev["aligned"] is False
        assert rev["needs_llm"] is True
        assert rev["steering"]
        assert "STEERING" in rev["steering"]


# ---------------------------------------------------------------------------
# Head spawn + depth cap
# ---------------------------------------------------------------------------

class TestSpawnDepth:
    def test_head_spawn_within_depth_cap(self):
        chart = OrgChart(
            "r_depth",
            cfg=_cfg_on(),
            max_subagent_depth=2,
        )
        chart.build_from_tasks(_mixed_tasks())
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        r = chart.spawn_worker(
            head_id=geo["head_id"],
            role="feature_modeler",
            brief="body",
        )
        assert r["ok"] is True
        assert r["depth"] == 2
        assert r["seat"]["rank"] == "worker"
        assert r["seat"]["dept_id"] == "geometry"

    def test_spawn_refused_past_depth_cap(self):
        # max_subagent_depth=1 → heads are depth 1, workers would be depth 2 → refuse
        chart = OrgChart(
            "r_depth1",
            cfg=_cfg_on(),
            max_subagent_depth=1,
        )
        chart.build_from_tasks(_mixed_tasks())
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        r = chart.spawn_worker(
            head_id=geo["head_id"],
            role="feature_modeler",
        )
        assert r["ok"] is False
        assert "depth" in r["error"].lower()
        assert r["max_subagent_depth"] == 1

    def test_non_head_cannot_spawn(self):
        chart = OrgChart("r_nohead", max_subagent_depth=2)
        chart.build_from_tasks(_mixed_tasks())
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        chart.spawn_worker(
            head_id=geo["head_id"], role="sketcher", worker_id="w1",
        )
        r = chart.spawn_worker(head_id="w1", role="feature_modeler")
        assert r["ok"] is False
        assert "head" in r["error"].lower() or "rank" in r["error"].lower()

    def test_team_lead_auto_on_threshold(self):
        chart = OrgChart(
            "r_lead",
            team_leads=True,
            max_subagent_depth=2,
        )
        chart.build_from_tasks(_mixed_tasks())
        geo = next(d for d in chart.departments if d["id"] == "geometry")
        head_id = geo["head_id"]
        last = None
        for i in range(4):  # threshold is 3; 4th worker triggers promotion
            last = chart.spawn_worker(
                head_id=head_id,
                role="feature_modeler",
                worker_id=f"w{i}",
            )
            assert last["ok"], last
        assert last is not None
        assert "auto_team_lead" in last
        assert last["auto_team_lead"]["rank"] == "team_lead"


# ---------------------------------------------------------------------------
# Flat mode untouched when off
# ---------------------------------------------------------------------------

class TestFlatMode:
    def test_should_use_corporate_false_when_off(self):
        assert should_use_corporate(_mixed_tasks(), cfg=_cfg_off()) is False

    def test_build_corporate_org_inactive_when_off(self):
        out = build_corporate_org(
            "r_flat",
            _mixed_tasks(),
            cfg=_cfg_off(),
        )
        assert out["ok"] is True
        assert out["active"] is False
        assert out["chart"] is None
        assert "off" in out["reason"]

    def test_mission_dispatch_flat_without_org(self):
        """coordinate_* still work; corporate_* report no chart — flat path."""
        md = MissionDispatch(run_id="r_flat_md", coordination_board=None)
        # No org bound
        r = md.corporate_chart_snapshot()
        assert r["ok"] is False
        assert "no corporate org" in r["error"]

        # Activate with mode off → inactive
        # Force False explicitly so we don't depend on the user's config.json
        out = md.corporate_activate(tasks=_mixed_tasks(), force=False)
        assert out["ok"] is True
        assert out["active"] is False
        # Dispatch still has no active org
        assert md.org_chart is None or not getattr(md.org_chart, "active", False)

    def test_mission_dispatch_activate_and_route(self):
        md = MissionDispatch(run_id="r_corp_md")
        out = md.corporate_activate(
            tasks=_mixed_tasks(),
            mission_goals=["bracket with cam and fea"],
            force=True,
        )
        assert out["active"] is True
        assert md.org_chart is not None
        assert md.org_chart.active

        snap = md.corporate_chart_snapshot()
        assert snap["ok"] is True
        assert snap["schema"] == CORPORATE_SCHEMA

        # Bind caller as geometry head and post
        geo = next(d for d in md.org_chart.departments if d["id"] == "geometry")
        md.corporate_agent_id = geo["head_id"]
        post = md.corporate_channel_post(
            channel=CHANNEL_HEADS,
            body="geometry kickoff",
            kind="info",
        )
        assert post["ok"] is True

        # Worker spawn via head
        spawned = md.corporate_spawn_worker(
            role="feature_modeler",
            brief="body solids",
            head_id=geo["head_id"],
        )
        assert spawned["ok"] is True
        worker_id = spawned["seat"]["agent_id"]

        # Worker → director rejected through dispatch
        md.corporate_agent_id = worker_id
        bad = md.corporate_channel_post(
            channel=CHANNEL_HEADS,
            body="help me director",
            kind="question",
        )
        assert bad["ok"] is False
        assert bad.get("status") == "rejected"


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_get_drop_chart(self):
        c1 = get_org_chart("r_reg", create=True)
        c2 = get_org_chart("r_reg", create=False)
        assert c1 is c2
        drop_org_chart("r_reg")
        assert get_org_chart("r_reg", create=False) is None
