from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import inspect
import json
import math
from pathlib import Path
from typing import get_type_hints

import pytest

from services.manufacturing import metrology as m

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures" / "mfg" / "metrology"
NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _qualification(*, directions=m.DIRECTIONS, agreement=0.004):
    return m.StylusQualification.fit(
        certified_diameter_mm=20.0,
        fitted_diameter_mm=19.998,
        nominal_ball_radius_mm=1.5,
        residual_rms_mm=0.003,
        residual_limit_mm=0.01,
        directional_pretravel_mm={direction: 0.005 for direction in directions},
        passes=2,
        pass_agreement_mm=agreement,
        artifact_ref="res:ring:1",
        established_at=NOW,
    )


def _repeatability(n=10, *, max_sigma=0.01):
    samples = [10.0 + ((i % 3) - 1) * 0.001 for i in range(n)]
    return m.RepeatabilityStudy.run("-Z", samples, max_sigma_mm=max_sigma,
                                    established_at=NOW)


def _budget(expanded=0.02, *, assumed=False):
    return m.UncertaintyBudget.measured((
        m.UncertaintyComponent("repeatability", expanded / 2.0,
                               m.ComponentSource.MEASURED),
        m.UncertaintyComponent("machine_positioning", 0.0,
                               m.ComponentSource.ASSUMED if assumed else m.ComponentSource.MEASURED),
    ))


def _qualified(*, tier=m.QualifiedTier.Q2, value=20.0, machine="machine-1"):
    return m.QualifiedObservation(
        device=m.DeviceIdentity("touch_probe", "p1"),
        tier=tier,
        machine_id=machine,
        value=value,
        uncertainty=_budget(),
        qualification_id="res:qualification:1",
        derived_from=("res:probe_result:1",),
        frame="wcs:54",
        frame_graph_revision=3,
    )


def _advisory(value=20.0):
    return m.AdvisoryObservation(
        device=m.DeviceIdentity("camera", "cam1"),
        tier=m.AdvisoryTier.A1,
        machine_id="machine-1",
        value=value,
        uncertainty_mm=0.1,
        derived_from=("res:frame:1",),
    )


def _locality(valid_for=frozenset((m.LocalityUse.THIS_SETUP,))):
    return m.LocalityBlock((0.0, 0.0, 0.0), (100.0, 100.0, 50.0), valid_for)


def test_evidence_classes_are_nominal_fixed_and_not_caller_settable():
    advisory = _advisory()
    qualified = _qualified()
    assert type(advisory) is not type(qualified)
    assert not isinstance(advisory, m.QualifiedObservation)
    assert advisory.evidence_class is m.EvidenceClass.ADVISORY
    assert qualified.evidence_class is m.EvidenceClass.QUALIFIED
    assert "evidence_class" not in inspect.signature(m.AdvisoryObservation).parameters
    assert "evidence_class" not in inspect.signature(m.QualifiedObservation).parameters
    with pytest.raises(FrozenInstanceError):
        advisory.evidence_class = m.EvidenceClass.QUALIFIED
    with pytest.raises(TypeError):
        m.QualifiedObservation(device=m.DeviceIdentity("camera", "cam1"),
                               tier=m.QualifiedTier.Q2, machine_id="machine-1", value=1.0,
                               uncertainty=_budget(), qualification_id="q", derived_from=("r",))


def test_offset_and_conformance_annotations_and_runtime_choke_points_refuse_advisory():
    offset_hint = get_type_hints(m.propose_offset)["observations"]
    conformance_hint = get_type_hints(m.evaluate_conformance)["observation"]
    assert "QualifiedObservation" in str(offset_hint)
    assert conformance_hint is m.QualifiedObservation
    advisory = _advisory()
    with pytest.raises(TypeError, match="QualifiedObservation"):
        m.propose_wcs_offset(observations=[advisory], machine_id="machine-1", job_id="j",
                             setup_id="s", setup_revision=1, register=54,
                             current_mm={"X": 0.0}, proposed_mm={"X": 0.1},
                             locality=_locality())
    characteristic = m.Characteristic("C1", "bore", "diameter", 20.0,
                                      m.Tolerance(-0.05, 0.05), m.InspectionMethod.PROBE)
    with pytest.raises(TypeError, match="QualifiedObservation"):
        m.evaluate_conformance(characteristic, advisory)


def test_advisory_tiers_each_admit_and_refuse_and_uncertainty_is_measured():
    raw = json.loads((FIXTURES / "tier_cases.json").read_text(encoding="utf-8"))
    a1_pass = m.A1Inputs(**raw["a1_pass"])
    a1_fail = m.A1Inputs(**raw["a1_fail"])
    a2_pass = m.A2Inputs(**{**raw["a2_pass"], "artifact_field_errors_mm": tuple(raw["a2_pass"]["artifact_field_errors_mm"])})
    a2_fail = m.A2Inputs(**{**raw["a2_fail"], "artifact_field_errors_mm": tuple(raw["a2_fail"]["artifact_field_errors_mm"])})
    assert m.admits_a0(a1=a1_fail, a2=a2_fail)
    assert not m.admits_a0(a1=a1_pass)
    assert m.admits_a1(a1_pass) and not m.admits_a1(a1_fail)
    assert m.admits_a2(a2_pass) and not m.admits_a2(a2_fail)
    expected_a1 = 2 * math.sqrt((2 * 1.0 / 20.0) ** 2 + (0.02 * 50.0) ** 2 + (1 / 20.0) ** 2)
    assert m.a1_uncertainty(a1_pass) == pytest.approx(expected_a1)
    assert m.a2_uncertainty(a2_pass) == pytest.approx(0.05)  # pixel floor exceeds 2*s


def test_probe_tiers_each_admit_and_refuse_repeatability_gates_q1_and_q2():
    qualification = _qualification()
    incomplete = _qualification(directions=("-Z",))
    repeatability = _repeatability()
    short_study = _repeatability(9)
    artifact = m.MachineArtifactStudy.run(
        (-0.01, 0.0, 0.01), orientations=3, certificate_uncertainty_mm=0.002,
        environment_recorded=True, max_abs_error_mm=0.02, established_at=NOW)
    bad_artifact = m.MachineArtifactStudy.run(
        (-0.01, 0.0, 0.01), orientations=2, certificate_uncertainty_mm=None,
        environment_recorded=False, max_abs_error_mm=0.02, established_at=NOW)
    assert m.admits_q0(None, at=NOW)
    assert not m.admits_q0(qualification, at=NOW)
    assert m.admits_q1(qualification, repeatability, at=NOW)
    assert not m.admits_q1(qualification, short_study, at=NOW)
    assert m.admits_q2(qualification, repeatability, required_directions=m.DIRECTIONS,
                       pass_agreement_limit_mm=0.01, at=NOW)
    assert not m.admits_q2(incomplete, repeatability, required_directions=m.DIRECTIONS,
                           pass_agreement_limit_mm=0.01, at=NOW)
    assert not m.admits_q2(qualification, short_study, required_directions=m.DIRECTIONS,
                           pass_agreement_limit_mm=0.01, at=NOW)
    assert m.admits_q3(qualification, repeatability, artifact,
                       required_directions=m.DIRECTIONS, pass_agreement_limit_mm=0.01, at=NOW)
    assert not m.admits_q3(qualification, repeatability, bad_artifact,
                           required_directions=m.DIRECTIONS, pass_agreement_limit_mm=0.01, at=NOW)
    assert m.evaluate_probe_tier(qualification, repeatability, artifact, at=NOW) is m.QualifiedTier.Q3


def test_uncertainty_budget_is_root_sum_square_of_inputs_not_tier_table():
    budget = m.UncertaintyBudget.measured((
        m.UncertaintyComponent("repeatability", 0.003, m.ComponentSource.MEASURED),
        m.UncertaintyComponent("pretravel", 0.004, m.ComponentSource.MEASURED),
        m.UncertaintyComponent("machine_positioning", 0.012, m.ComponentSource.ASSUMED),
    ), k=2.0)
    assert budget.u_combined_mm == pytest.approx(0.013)
    assert budget.expanded_mm == pytest.approx(0.026)
    assert budget.basis is m.ComponentSource.ASSUMED


def test_q2_offset_locality_refuses_transfer_traceability_and_outside_envelope():
    observation = _qualified(tier=m.QualifiedTier.Q2)
    for use in (m.LocalityUse.DATUM_TRANSFER, m.LocalityUse.SECOND_SIDE,
                m.LocalityUse.Q3_TRACEABLE_CLAIM):
        with pytest.raises(ValueError, match="locality_scope_exceeded"):
            m.propose_wcs_offset(observations=[observation], machine_id="machine-1",
                                 job_id="j", setup_id="s", setup_revision=2, register=54,
                                 current_mm={"X": 0.0}, proposed_mm={"X": 0.1},
                                 locality=_locality(frozenset(m.LocalityUse)), intended_use=use)
    with pytest.raises(ValueError, match="cancellation envelope"):
        m.propose_wcs_offset(observations=[observation], machine_id="machine-1",
                             job_id="j", setup_id="s", setup_revision=2, register=54,
                             current_mm={"X": 0.0}, proposed_mm={"X": 0.1},
                             locality=_locality(), use_location_machine=(101.0, 0.0, 0.0))
    proposal = m.propose_wcs_offset(observations=[observation], machine_id="machine-1",
                                    job_id="j", setup_id="s", setup_revision=2, register=54,
                                    current_mm={"X": 0.0}, proposed_mm={"X": 0.1},
                                    locality=_locality())
    assert proposal.delta_mm == {"X": pytest.approx(0.1)}
    assert observation.id in proposal.derived_from


@pytest.mark.parametrize("case", json.loads((FIXTURES / "guardband_cases.json").read_text(encoding="utf-8")))
def test_guardbanded_verdict_boundaries(case):
    verdict = m.guardbanded_verdict(measured_mm=case["measured_mm"],
                                    nominal_mm=case["nominal_mm"],
                                    tolerance=m.Tolerance(case["lower_mm"], case["upper_mm"]),
                                    expanded_uncertainty_mm=case["U_mm"])
    assert verdict.value == case["verdict"]


def test_inspection_results_preserve_roots_and_precedence():
    plan = m.InspectionPlan("plan-1", 4, "part:1@7", "setup-1", (
        m.Characteristic("C1", "bore", "diameter", 20.0, m.Tolerance(-0.05, 0.05),
                         m.InspectionMethod.PROBE),
        m.Characteristic("C2", "boss", "diameter", 10.0, m.Tolerance(-0.05, 0.05),
                         m.InspectionMethod.PROBE),
    ))
    one = m.evaluate_conformance(plan.characteristics[0], _qualified(value=20.01))
    two = m.evaluate_conformance(plan.characteristics[1], _qualified(value=10.10))
    result = m.build_inspection_result(plan, (one, two))
    assert result.verdict is m.Verdict.FAIL
    assert set(result.derived_from) >= set(one.derived_from) | set(two.derived_from)


def test_probe_moves_compensation_studies_and_results_are_typed_data():
    move = m.ProbeMove(m.ProbeMoveKind.MEASURE, (0.0, 0.0, -5.0), (0.0, 0.0, -1.0),
                       25.0, 10.0)
    point = m.compensate_surface_point((1.0, 2.0, 3.0), move.approach_direction,
                                       effective_radius_mm=1.5, pretravel_mm=0.01)
    assert point == pytest.approx((1.0, 2.0, 1.51))
    touch = m.ProbeTouch(0, move, (1.0, 2.0, 3.0), point, 1.5, 0.01, 9, 100)
    result = m.ProbeResult("cycle-1", m.ProbeResultStatus.COMPLETED, (touch,), 2, 0.004,
                           "sha256:program", ("res:qualification:1",))
    assert result.evidence_class is m.EvidenceClass.QUALIFIED
    assert result.derived_from == ("res:qualification:1",)
    study = _repeatability()
    assert study.admitted and study.two_sigma_mm == pytest.approx(2 * study.sample_sigma_mm)


def test_transform_graph_station_and_registration_fail_closed():
    covariance = tuple(tuple(0.001 if r == c else 0.0 for c in range(6)) for r in range(6))
    graph = m.TransformGraph()
    graph.add_edge(m.TransformEdge("e1", "machine", "fixture:a", m.Pose((10, 0, 0)),
                                   covariance, m.EdgeSource.PROBED, 1))
    graph.add_edge(m.TransformEdge("e2", "fixture:a", "stock:s1", m.Pose((0, 5, 0)),
                                   covariance, m.EdgeSource.PROBED, 2))
    resolved = graph.resolve("machine", "stock:s1", at=NOW)
    assert resolved.pose.translation_mm == pytest.approx((10, 5, 0))
    assert resolved.edge_ids == ("e1", "e2")
    with pytest.raises(ValueError, match="cycle"):
        graph.add_edge(m.TransformEdge("e3", "stock:s1", "machine", m.Pose(), covariance,
                                       m.EdgeSource.PROBED, 3))
    stale = m.TransformGraph()
    stale.add_edge(m.TransformEdge("old", "machine", "wcs:54", m.Pose(), covariance,
                                   m.EdgeSource.PROBED, 1, valid_until=NOW - timedelta(seconds=1)))
    with pytest.raises(m.TransformResolutionError) as error:
        stale.resolve("machine", "wcs:54", at=NOW)
    assert error.value.edge == "old"
    stock = m.register_stock(stock_id="s1", observations=[_qualified(tier=m.QualifiedTier.Q1)],
                             pose_machine=m.Pose((1, 2, 3)), measured_extents_mm=(10, 20, 30),
                             cam_extents_mm=(10, 20, 30), max_skew_mm=0.1,
                             measured_skew_mm=0.01)
    assert stock.verdict is m.RegistrationVerdict.ACCEPTED
    assert stock.derived_from


def test_no_metrology_module_function_can_emit_a_controller_write():
    package_dir = Path(m.__file__).parent
    forbidden_imports = {"socket", "serial", "subprocess"}
    forbidden_calls = {"open", "send", "sendall", "write_bytes"}
    for path in package_dir.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ({alias.name.split(".")[0] for alias in node.names}
                         if isinstance(node, ast.Import) else {str(node.module).split(".")[0]})
                assert not names & forbidden_imports, f"transport import in {path.name}"
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
                assert name not in forbidden_calls, f"I/O call {name} in {path.name}"
    exported_functions = {name for name, value in vars(m).items() if inspect.isfunction(value)}
    assert not exported_functions & {"apply_offset", "controller_write", "write_offset"}



def test_metrology_fit_refuses_unbounded_sample_input_by_name():
    with pytest.raises(m.MetrologyResourceRefusal) as caught:
        m.RepeatabilityStudy.run(
            "-Z",
            (float(index) for index in range(m.MAX_METROLOGY_ITEMS + 1)),
            max_sigma_mm=1.0,
            established_at=NOW,
        )
    assert caught.value.code == "repeatability_samples_limit"


def test_metrology_uncertainty_refuses_oversized_field_study():
    raw = json.loads((FIXTURES / "tier_cases.json").read_text(encoding="utf-8"))
    inputs = m.A2Inputs(
        **{
            **raw["a2_pass"],
            "artifact_field_errors_mm": (0.0,) * (m.MAX_METROLOGY_ITEMS + 1),
        }
    )
    with pytest.raises(m.MetrologyResourceRefusal) as caught:
        m.a2_uncertainty(inputs)
    assert caught.value.code == "artifact_field_errors_limit"