from pathlib import Path

from .capability import capability_report
from .collision import check_collisions
from .model import BoxObstacle, CellModel
from .safety_boundary import RESERVED_METHOD_NAMES, RESERVED_REFUSALS, ReservedMethodRefusal, refuse_reserved_method
from .trajectory import export_trajectory, mock_controller_run, preview_trajectory
from .urdf import import_urdf

FIXTURES = Path(__file__).parents[3] / "fixtures" / "mfg" / "robot"


def _chain():
    return import_urdf(FIXTURES / "two_link.urdf")


def test_collision_detection_on_constructed_cell() -> None:
    colliding = CellModel(
        "cell", _chain(),
        obstacles=(BoxObstacle("fixture", (900.0, -25.0, -25.0), (1100.0, 25.0, 25.0)),),
        link_radius_mm=5.0,
    )
    clear = CellModel(
        "cell", _chain(),
        obstacles=(BoxObstacle("fixture", (900.0, 100.0, -25.0), (1100.0, 125.0, 25.0)),),
        link_radius_mm=5.0,
    )
    result = check_collisions(colliding, (0.0, 0.0))
    assert result.collision is True
    assert [(contact.link_id, contact.obstacle_id) for contact in result.contacts] == [("link2", "fixture"), ("tool", "fixture")]
    assert result.source == "simulated"
    assert result.satisfies_execute_gate is False
    assert check_collisions(clear, (0.0, 0.0)).collision is False


def test_trajectory_preview_export_and_mock_replay_are_deterministic(tmp_path: Path) -> None:
    trajectory = preview_trajectory(_chain(), ((0.0, 0.0), (0.2, -0.1), (0.4, 0.3)), sample_period_s=0.05)
    first = mock_controller_run(trajectory)
    second = mock_controller_run(trajectory)
    assert first == second
    assert first.samples == trajectory.points
    assert first.completed is True
    assert first.source == "simulated"
    assert first.advisory_only is True
    assert first.satisfies_execute_gate is False
    json_path = export_trajectory(trajectory, tmp_path / "preview.json")
    csv_path = export_trajectory(trajectory, tmp_path / "preview.csv")
    assert '"not_for_execution": true' in json_path.read_text(encoding="utf-8")
    assert csv_path.read_text(encoding="utf-8").startswith("# not_for_execution=true")


def test_every_reserved_execute_transport_safety_name_refuses_by_name() -> None:
    assert len(RESERVED_METHOD_NAMES) == 10
    assert set(RESERVED_REFUSALS) == set(RESERVED_METHOD_NAMES)
    for method_name in RESERVED_METHOD_NAMES:
        try:
            refuse_reserved_method(method_name)
        except ReservedMethodRefusal as refusal:
            assert refusal.method_name == method_name
            assert method_name in str(refusal)
            assert "permanently_unimplemented" in refusal.reason
        else:
            raise AssertionError(f"reserved method {method_name} did not refuse")


def test_robot_capability_never_advertises_device_or_safety_levels() -> None:
    report = capability_report()
    levels = {item.level: item for item in report["levels"]}
    assert report["max_advertised"] == "simulate"
    assert report["device_command_owner"] is None
    assert {name for name, item in levels.items() if item.truth == "verified"} == {"plan", "generate", "simulate"}
    for name in ("transport", "execute", "safety_integration"):
        assert levels[name].truth == "unsupported"
        assert levels[name].why
