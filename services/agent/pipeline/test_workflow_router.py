"""Regression coverage for deterministic mission task classification."""
from __future__ import annotations

import pytest

from services.agent.pipeline.workflow_router import (
    TaskClass,
    classify_task,
    classify_task_detailed,
    route,
    structure_for_class,
)




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
        r = route("generate a CAM program for milling", "local-24k")
        assert r.task_class == TaskClass.CAM_PROGRAM
        assert "CAM" in r.reason or "machining" in r.reason.lower()

    def test_measure_capability_label(self):
        _cls, why = classify_task_detailed("measure mass properties of this bracket")
        # "bracket" may win as SIMPLE_PART before the measure rule — either is fine,
        # but the detailed path must return a non-empty why for a real request.
        assert why  # matched something


# ---------------------------------------------------------------------------
# PROJECT_ADMIN (file-org / cleanup / PLM) — must preempt the assembly rule
# ---------------------------------------------------------------------------

class TestProjectAdmin:
    """Regression coverage for the misrouted "organize my files" bug: a
    cleanup/PLM request that happens to mention a CAD document name like
    "general assembly" must classify PROJECT_ADMIN, not ASSEMBLY — and a
    genuine geometry-build request must still classify ASSEMBLY."""

    def test_cleanup_plm_transcript_classifies_project_admin(self):
        transcript = (
            "I want you to look at all documents... clean up & put into PLM... "
            "clean up file system... deprecated folders... move unrelated to "
            "/documents/Chudware... init PLM... rename GA to general assembly"
        )
        assert classify_task(transcript) == TaskClass.PROJECT_ADMIN

    def test_geometry_build_still_classifies_assembly(self):
        assert classify_task("model a bracket assembly with two mates") == TaskClass.ASSEMBLY

    def test_project_admin_routes_to_linear_organizer_turn(self):
        assert structure_for_class(TaskClass.PROJECT_ADMIN) == ("linear", "project_organizer")

    def test_build_classes_stay_on_dag_structure(self):
        assert structure_for_class(TaskClass.ASSEMBLY) == ("dag", None)
        assert structure_for_class(TaskClass.SIMPLE_PART) == ("dag", None)

    def test_route_end_to_end_reports_linear_structure(self):
        transcript = (
            "clean up the file system, put everything into PLM, and rename GA "
            "to general assembly"
        )
        r = route(transcript, "local-24k")
        assert r.task_class == TaskClass.PROJECT_ADMIN
        assert r.structure == "linear"
        assert r.role == "project_organizer"
        d = r.to_dict()
        assert d["structure"] == "linear"
        assert d["role"] == "project_organizer"

    def test_route_build_request_keeps_dag_structure(self):
        r = route("model a bracket assembly with two mates", "local-24k")
        assert r.task_class == TaskClass.ASSEMBLY
        assert r.structure == "dag"
        assert r.role is None

    def test_general_work_and_unknown_default_to_general_agent(self):
        # Inverted default: anything that is not a geometry-build class runs the
        # free linear general_agent lane — never the multi-step build swarm.
        assert structure_for_class(TaskClass.GENERAL_WORK) == ("linear", "general_agent")
        assert structure_for_class(TaskClass.UNKNOWN) == ("linear", "general_agent")

    def test_route_basic_work_runs_general_agent_lane(self):
        r = route("list my documents and summarize the readme", "local-24k")
        assert r.structure == "linear"
        assert r.role == "general_agent"
        d = r.to_dict()
        assert d["structure"] == "linear"
        assert d["role"] == "general_agent"
