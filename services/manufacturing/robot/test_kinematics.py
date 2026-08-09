from pathlib import Path
import math

import pytest

from .kinematics import (
    UnreachableTargetError, analyze_singularity, forward_kinematics,
    pose_translation, require_reachable,
)
from .urdf import (
    MAX_ELEMENTS,
    MAX_XML_BYTES,
    RobotXmlRefusal,
    import_srdf,
    import_urdf,
)

FIXTURES = Path(__file__).parents[3] / "fixtures" / "mfg" / "robot"


def test_urdf_and_srdf_parse_multi_joint_chain() -> None:
    chain = import_urdf(FIXTURES / "two_link.urdf")
    semantic = import_srdf(FIXTURES / "two_link.srdf")
    assert chain.links == ("base", "link1", "link2", "tool")
    assert [joint.name for joint in chain.joints] == ["shoulder", "elbow", "tool_fixed"]
    assert chain.joints[1].origin_xyz_mm == (1000.0, 0.0, 0.0)
    assert semantic.groups["arm"] == ("base", "tool")
    assert semantic.group_states["home"] == {"shoulder": 0.0, "elbow": 0.0}
    assert semantic.disabled_collisions == (("base", "link1", "Adjacent"),)


def test_forward_kinematics_matches_hand_computed_pose() -> None:
    chain = import_urdf(FIXTURES / "two_link.urdf")
    straight = pose_translation(forward_kinematics(chain, (0.0, 0.0)).tcp_pose)
    quarter_turn = pose_translation(forward_kinematics(chain, (math.pi / 2.0, 0.0)).tcp_pose)
    bent = pose_translation(forward_kinematics(chain, (math.pi / 2.0, -math.pi / 2.0)).tcp_pose)
    assert straight == pytest.approx((2000.0, 0.0, 0.0), abs=1e-9)
    assert quarter_turn == pytest.approx((0.0, 2000.0, 0.0), abs=1e-9)
    assert bent == pytest.approx((1000.0, 1000.0, 0.0), abs=1e-9)


def test_unreachable_target_refuses_by_name() -> None:
    chain = import_urdf(FIXTURES / "two_link.urdf")
    with pytest.raises(UnreachableTargetError, match="unreachable_target"):
        require_reachable(chain, (2000.001, 0.0, 0.0))
    assert require_reachable(chain, (1500.0, 0.0, 0.0))["reachable"] is True


def test_singularity_detection_distinguishes_stretched_and_bent() -> None:
    chain = import_urdf(FIXTURES / "two_link.urdf")
    assert analyze_singularity(chain, (0.0, 0.0)).singular is True
    assert analyze_singularity(chain, (math.pi / 2.0, -math.pi / 2.0)).singular is False


def test_robot_xml_forbids_doctype_and_entities() -> None:
    hostile = (
        '<!DOCTYPE robot [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<robot name="x">&xxe;</robot>'
    )
    with pytest.raises(RobotXmlRefusal) as caught:
        import_urdf(hostile)
    assert caught.value.code == "robot_xml_declaration"
    assert "/etc/passwd" not in caught.value.reason


def test_robot_xml_refuses_traversal_symlink_and_oversize(tmp_path: Path) -> None:
    robot = tmp_path / "robot.urdf"
    robot.write_text(
        '<robot name="x"><link name="a"/><link name="b"/>'
        '<joint name="j" type="fixed"><parent link="a"/><child link="b"/></joint>'
        '</robot>',
        encoding="utf-8",
    )
    traversing = tmp_path / "nested" / ".." / "robot.urdf"
    with pytest.raises(RobotXmlRefusal) as traversal:
        import_urdf(traversing)
    assert traversal.value.code == "robot_xml_traversal"

    linked = tmp_path / "linked.urdf"
    linked.symlink_to(robot)
    with pytest.raises(RobotXmlRefusal) as symlink:
        import_urdf(linked)
    assert symlink.value.code == "robot_xml_symlink"

    oversized = tmp_path / "oversized.urdf"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_XML_BYTES + 1)
    with pytest.raises(RobotXmlRefusal) as size:
        import_urdf(oversized)
    assert size.value.code == "robot_xml_too_large"


def test_robot_xml_refuses_excessive_element_count() -> None:
    payload = '<robot name="x">' + "<link/>" * MAX_ELEMENTS + "</robot>"
    with pytest.raises(RobotXmlRefusal) as caught:
        import_urdf(payload)
    assert caught.value.code == "robot_xml_element_limit"
