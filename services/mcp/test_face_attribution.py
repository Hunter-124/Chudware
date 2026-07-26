"""Feature -> face attribution, against the REAL engine.

One de-smear implementation now serves everything: cad_common._created_face_matches judges a
single feature's claimed faces, cad_common._face_attribution drives it across a document, and
both the inspect/render targeting join (_feature_owned_matches) and describe_part's narrative
(gsl_tools._digest) read that one result. These tests build actual documents through the
built-in agent's binder (services.agent.agent_turn._bind_tool loads
core/build-occt-linux/bin/libchudware.so) so the owner_map smear they defend against is the
kernel's real one, not a fixture's idea of it.

Also covers the input dialects describe_part / describe_project accept (recorded ledger
failures) and the FastMCP registration path, which a bare **kwargs silently breaks.

Run:  python3 -m pytest services/mcp/test_face_attribution.py -q -p no:randomly
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_common as cc  # noqa: E402
from services.mcp import gsl_tools as gt  # noqa: E402

try:
    from services.agent.agent_turn import _bind_tool  # noqa: E402
    _new_part = _bind_tool("new_part")
    _add_features = _bind_tool("add_features")
except Exception as _e:  # noqa: BLE001 — engine not built/staged
    pytestmark = pytest.mark.skip(reason=f"engine unavailable: {_e}")
    _new_part = _add_features = None


# --------------------------------------------------------------------------- document builders


def _build(name: str, ops: list) -> str:
    """A real document with `ops` applied — fails loudly rather than testing an empty part."""
    doc_id = json.loads(_new_part(name=name))["doc_id"]
    res = json.loads(_add_features(doc_id=doc_id, ops=ops))
    assert res.get("ok"), f"{name}: add_features rejected the build: {json.dumps(res)[:400]}"
    return doc_id


def _prim(name: str, type_: str, params: dict, **rest) -> dict:
    return {"op": "add_feature", "feature": {"type": type_, "name": name, "params": params,
                                             **rest}}


_PLATE = _prim("Plate", "box", {"length": 120, "width": 80, "height": 15})


def _sids(matches) -> list:
    return [m.get("sid") for m in matches]


def _area(matches, sid) -> float:
    return next(m["area_or_len"] for m in matches if m.get("sid") == sid)


def _group(digest: dict, fid: str) -> dict:
    return next(g for g in digest["feature_groups"] if g["id"] == fid)


@pytest.fixture(scope="module")
def bore_doc() -> str:
    """120x80x15 plate pierced by an r8 through-bore. owner_map credits the bore BOTH plate
    faces it punched through (9398.94 mm2 each) plus its own 753.98 mm2 wall."""
    return _build("fa_plate_bore", [
        _PLATE,
        _prim("Bore", "cylinder", {"radius": 8, "height": 60}, at=[-30, 0, 0], mode="cut"),
    ])


@pytest.fixture(scope="module")
def boss_doc() -> str:
    """Same plate with an r18 boss FUSED on top. owner_map credits the boss the plate's whole
    8582.12 mm2 top face on top of its own wall + cap."""
    return _build("fa_plate_boss", [
        _PLATE,
        _prim("Boss", "cylinder", {"radius": 18, "height": 10}, at=[0, 0, 12.5], mode="boss"),
    ])


@pytest.fixture(scope="module")
def spout_doc() -> str:
    """The known-good narrative case: r165 bucket, r155 cavity, r25 spout fused out the side."""
    return _build("fa_mud_saver_bucket", [
        _prim("Body", "cylinder", {"radius": {"value": 165, "expr": None},
                                   "height": {"value": 300, "expr": None}}),
        _prim("Cavity", "cylinder", {"radius": 155, "height": 280}, at=[0, 0, 20], mode="cut"),
        _prim("DrainSpout", "cylinder", {"radius": {"value": 25, "expr": None},
                                         "height": {"value": 60, "expr": None}},
              at=[150, 0, -100], axis=[1, 0, 0], centered=False, mode="boss"),
    ])


# ------------------------------------------------------------- (a) subtractive smear: the bore


class TestSubtractiveSmear:
    def test_owner_map_really_does_smear(self, bore_doc):
        """The premise. Without this the rest proves nothing: the kernel hands the bore three
        faces, two of which are the plate's."""
        om = (cc.cb.call("doc_tree", {"doc_id": bore_doc}) or {}).get("owner_map") or {}
        assert len(om["f2"]["faces"]) == 3

    def test_cad_common_keeps_only_the_bore_wall(self, bore_doc):
        ms = cc._feature_owned_matches(bore_doc, "Bore")
        assert [(m["surf"], round(m["area_or_len"], 2)) for m in ms] == [("cylinder", 753.98)]

    def test_pierced_faces_go_back_to_the_plate(self, bore_doc):
        att = cc._face_attribution(bore_doc)
        assert att["claimed"]["f2"] == 3 and att["reattributed"] == 2
        # Conserved, not deleted: both 9398.94 mm2 faces now belong to the plate.
        plate = [round(m["area_or_len"], 2) for m in att["owned"]["f1"]]
        assert plate.count(9398.94) == 2
        assert not [m for m in att["owned"]["f1"] if m["surf"] != "plane"]

    def test_describe_part_agrees_with_the_targeting_join(self, bore_doc):
        d = gt._digest(bore_doc)
        assert (_group(d, "f2")["faces"], _group(d, "f2")["attributed_faces"]) == (1, 3)
        assert len(cc._feature_owned_matches(bore_doc, "Bore")) == _group(d, "f2")["faces"]


# ------------------------------------------------------------------ (b) additive smear: a fuse


class TestAdditiveFuseSmear:
    def test_owner_map_really_does_smear(self, boss_doc):
        om = (cc.cb.call("doc_tree", {"doc_id": boss_doc}) or {}).get("owner_map") or {}
        assert len(om["f2"]["faces"]) == 3

    def test_cad_common_drops_the_plate_top(self, boss_doc):
        """The regression this consolidation exists for: before it, this returned the plate's
        8582.12 mm2 top face alongside the boss's own wall and cap."""
        ms = cc._feature_owned_matches(boss_doc, "Boss")
        assert [(m["surf"], round(m["area_or_len"], 2)) for m in ms] == \
            [("cylinder", 1130.97), ("plane", 1017.88)]

    def test_the_plate_top_is_credited_to_the_plate(self, boss_doc):
        att = cc._face_attribution(boss_doc)
        assert att["claimed"]["f2"] == 3 and att["reattributed"] == 1
        tops = [round(m["area_or_len"], 2) for m in att["owned"]["f1"]]
        assert 8582.12 in tops
        assert 8582.12 not in [round(m["area_or_len"], 2) for m in att["owned"]["f2"]]

    def test_describe_part_narrates_two_faces(self, boss_doc):
        d = gt._digest(boss_doc)
        g = _group(d, "f2")
        assert (g["faces"], g["attributed_faces"], d["reattributed_faces"]) == (2, 3, 1)
        assert "f2 Boss [cylinder] (2 face(s)): on the top face of f1 Plate" in \
            gt._feature_group_line(g)
        # Both paths now answer the same question with the same number.
        assert len(cc._feature_owned_matches(boss_doc, "Boss")) == g["faces"]

    def test_lateral_smear_needs_the_scale_test_not_the_footprint(self, spout_doc):
        """A sideways spout is handed the bucket's r165 wall — a LATERAL face, so the geometric
        footprint test cannot see it. The params tool-scale test is what catches it."""
        ms = cc._feature_owned_matches(spout_doc, "DrainSpout")
        assert max(m["area_or_len"] for m in ms) < 10_000.0
        assert 309048.51 not in [round(m["area_or_len"], 2) for m in ms]


# --------------------------------------------------- (c) an honest empty answer, not a made-up one


class TestHonestlyEmpty:
    def test_created_face_matches_returns_empty_for_wholly_foreign_faces(self, bore_doc):
        """The old helper ended in `kept or matches`: asked which of the PLATE's two faces the
        bore created, it answered "both". The consolidated one answers "none"."""
        feats, _om = cc._target_feature_roster(bore_doc)
        bore = next(f for f in feats if f["name"] == "Bore")
        plate_faces = [m for m in cc._face_attribution(bore_doc)["owned"]["f1"]
                       if round(m["area_or_len"], 2) == 9398.94]
        assert len(plate_faces) == 2
        assert cc._created_face_matches(bore_doc, bore, plate_faces, feats) == []

    def test_a_consumed_feature_owns_nothing(self):
        """A boss eaten by a later, wider cut. Nothing of it survives on the live body, so the
        attribution has no entry for it — rather than a fabricated one."""
        doc = _build("fa_consumed_boss", [
            _PLATE,
            _prim("Boss", "cylinder", {"radius": 18, "height": 10}, at=[0, 0, 12.5],
                  mode="boss"),
            _prim("Facer", "box", {"length": 200, "width": 200, "height": 40},
                  at=[0, 0, 27.5], mode="cut"),
        ])
        att = cc._face_attribution(doc)
        assert att["created"].get("f2", []) == []
        assert att["owned"].get("f2", []) == []
        assert _group(gt._digest(doc), "f2").get("consumed") is True
        with pytest.raises(ValueError, match="owns no faces/edges on the current body"):
            cc._feature_owned_matches(doc, "Boss")

    def test_the_callout_roster_skips_a_feature_with_no_face_of_its_own(self, boss_doc):
        roster = {r["id"]: r["face_topos"] for r in cc._feature_roster(boss_doc)}
        assert roster["f2"] == {7, 8}          # boss wall + boss cap, not the plate's top (3)
        assert 3 not in roster["f2"]


# ----------------------------------------------------- the known-good describe_part narrative


class TestNarrativeUnchanged:
    def test_spout_line_is_byte_for_byte_what_it_was(self, spout_doc):
        d = gt._digest(spout_doc)
        assert gt._feature_group_line(_group(d, "f3")) == \
            "f3 DrainSpout [cylinder] (4 face(s)): on the right face of f1 Body, " \
            "centered 165 mm from -Y edge / 52 mm from -Z edge"
        assert d["reattributed_faces"] == 3

    def test_targeting_join_now_agrees_with_that_narrative(self, spout_doc):
        """It used to disagree loudly: describe_part said 4 faces while the targeting join
        handed back 7, including the bucket's 309048 mm2 wall and the cavity's 261083 mm2 one."""
        assert len(cc._feature_owned_matches(spout_doc, "DrainSpout")) == 4


# ------------------------------------------------------------------ describe_* input dialects


class TestDescribePartDialect:
    def test_canonical_call_carries_no_echo(self, boss_doc):
        r = json.loads(_bind_tool("describe_part")(doc_id=boss_doc))
        assert r["ok"] and "interpreted" not in r

    @pytest.mark.parametrize("key", ["part", "part_name", "name", "target", "document"])
    def test_document_aliases_resolve(self, boss_doc, key):
        r = json.loads(_bind_tool("describe_part")(**{key: boss_doc}))
        assert r["ok"] and "Plate" in r["outline"]
        assert any(key in n for n in r["interpreted"])

    def test_view_filter_args_are_accepted_and_explained(self, boss_doc):
        r = json.loads(_bind_tool("describe_part")(doc_id=boss_doc, attributes=["bbox"],
                                                   parameters={"units": "mm"}))
        assert r["ok"] and r["digest"]["bbox_mm"]
        echo = " ".join(r["interpreted"])
        assert "attributes/parameters" in echo and "digest.bbox_mm" in echo

    def test_unknown_argument_is_named_not_silently_dropped(self, boss_doc):
        r = json.loads(_bind_tool("describe_part")(doc_id=boss_doc, verbosity="high"))
        assert r["ok"]
        assert any("verbosity" in n for n in r["interpreted"])

    def test_empty_document_teaches_instead_of_erroring(self):
        doc = json.loads(_new_part(name="fa_empty_doc"))["doc_id"]
        r = json.loads(_bind_tool("describe_part")(doc_id=doc))
        assert r["ok"] is True and r["empty"] is True
        assert "is EMPTY" in r["outline"] and "not a tool failure" in r["outline"]
        assert f'add_features(doc_id="{doc}"' in r["outline"]


class TestDescribeProjectDialect:
    def test_canonical_no_argument_call_is_unchanged(self):
        r = json.loads(_bind_tool("describe_project")())
        assert r["ok"] and "interpreted" not in r
        assert r["text"].startswith("No DAG run data supplied")

    def test_canonical_run_dict_is_unchanged(self):
        run = {"id": "r1", "name": "Bracket", "nodes": [{"id": "n1", "status": "done",
                                                         "title": "model"}]}
        r = json.loads(_bind_tool("describe_project")(run=run))
        assert r["ok"] and "interpreted" not in r
        assert "1 done/review" in r["text"]

    def test_run_id_string_succeeds(self):
        """The recorded failure: describe_project(run_id="x") -> unexpected keyword argument."""
        r = json.loads(_bind_tool("describe_project")(run_id="fa-no-such-run"))
        assert r["ok"] is True
        assert "fa-no-such-run" in r["text"]
        echo = " ".join(r["interpreted"])
        assert "run_id" in echo and "dag_plan" in echo

    def test_run_given_as_a_bare_string_is_read_as_an_id(self):
        r = json.loads(_bind_tool("describe_project")(run="fa-no-such-run"))
        assert r["ok"] and "fa-no-such-run" in r["text"]

    def test_a_persisted_run_is_loaded_from_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gt, "_runs_dir", lambda: tmp_path)
        (tmp_path / "rid-7.graph.json").write_text(json.dumps(
            {"schema": "aicad.dag/1", "id": "rid-7", "name": "wind-tunnel bracket",
             "nodes": [{"id": "n1", "type": "design", "name": "model bracket"},
                       {"id": "n2", "type": "verify", "name": "check it"}]}), encoding="utf-8")
        r = json.loads(_bind_tool("describe_project")(run_id="rid-7"))
        assert r["ok"] and 'Project "wind-tunnel bracket" (DAG run rid-7)' in r["text"]
        assert "2 node(s) total" in r["text"] and "2 pending" in r["text"]

    def test_semantic_description_fields_are_narrated_back(self):
        r = json.loads(_bind_tool("describe_project")(
            project_name="Bracket", deliverables=["STEP", "drawing"],
            constraints="6061-T6", mission_request="design a wind-tunnel bracket"))
        assert r["ok"]
        assert 'Project "Bracket"' in r["text"]
        for expect in ("STEP, drawing", "6061-T6", "design a wind-tunnel bracket"):
            assert expect in r["text"]
        assert any("dag_plan" in n for n in r["interpreted"])


# --------------------------------------------------------------------- the FastMCP surface
# A bare `**kwargs` on an @mcp.tool() becomes a REQUIRED pydantic field named "kwargs" in
# FastMCP's func_metadata, which breaks EVERY call through that path — including the canonical
# one — while the built-in agent path (_bind_tool -> the raw function) keeps working. These are
# the assertions that catch it.


def _fastmcp_text(tool: str, args: dict) -> str:
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
    mcp = FastMCP("test_face_attribution")
    gt.register_gsl_tools(mcp)
    blocks = asyncio.run(mcp.call_tool(tool, args))
    content = blocks[0] if isinstance(blocks, tuple) else blocks
    return "".join(getattr(b, "text", "") for b in content)


class TestFastMCPPath:
    def test_describe_part_canonical_call(self, boss_doc):
        r = json.loads(_fastmcp_text("describe_part", {"doc_id": boss_doc}))
        assert r["ok"] and "f2 Boss [cylinder] (2 face(s))" in r["outline"]

    def test_describe_project_canonical_call(self):
        r = json.loads(_fastmcp_text("describe_project", {}))
        assert r["ok"] and r["text"].startswith("No DAG run data supplied")

    def test_dialect_reaches_the_tool_through_the_kwargs_field(self, boss_doc):
        """FastMCP drops arguments that are not in the generated schema, so an external client
        addresses the dialect through the var-kw field the schema DOES carry."""
        r = json.loads(_fastmcp_text("describe_part", {"kwargs": {"part": boss_doc}}))
        assert r["ok"] and "Plate" in r["outline"]
