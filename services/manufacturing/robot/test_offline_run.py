from pathlib import Path
import pytest

from .capability import capability_report
from .collision import CollisionResourceRefusal, check_collisions
from .model import BoxObstacle, CellModel, RobotChain, RobotJoint
from .safety_boundary import RESERVED_METHOD_NAMES, RESERVED_REFUSALS, ReservedMethodRefusal, refuse_reserved_method
from .trajectory import (
    RobotResourceRefusal,
    export_trajectory,
    mock_controller_run,
    preview_trajectory,
)
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


def test_trajectory_point_ceiling_refuses_before_allocation() -> None:
    with pytest.raises(RobotResourceRefusal) as caught:
        preview_trajectory(
            _chain(),
            ((0.0, 0.0), (1.0, 0.0)),
            sample_period_s=1e-9,
        )
    assert caught.value.code == "trajectory_point_limit"


def test_trajectory_export_refuses_symlink_target(tmp_path: Path) -> None:
    trajectory = preview_trajectory(_chain(), ((0.0, 0.0), (0.1, 0.1)))
    protected = tmp_path / "protected.json"
    protected.write_text("keep", encoding="utf-8")
    target = tmp_path / "preview.json"
    target.symlink_to(protected)
    with pytest.raises(RobotResourceRefusal) as caught:
        export_trajectory(trajectory, target)
    assert caught.value.code == "trajectory_path_symlink"
    assert protected.read_text(encoding="utf-8") == "keep"


def test_collision_pair_ceiling_refuses_cartesian_blowup() -> None:
    links = tuple(f"l{index}" for index in range(1_002))
    joints = tuple(
        RobotJoint(f"j{index}", "fixed", links[index], links[index + 1])
        for index in range(1_001)
    )
    chain = RobotChain("large", links[0], links[-1], links, joints)
    obstacle = BoxObstacle("box", (-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
    cell = CellModel("large", chain, obstacles=(obstacle,) * 1_000)
    with pytest.raises(CollisionResourceRefusal) as caught:
        check_collisions(cell, ())
    assert caught.value.code == "collision_pair_limit"


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
