from pathlib import Path
import math

import pytest

from .kinematics import (
    UnreachableTargetError, analyze_singularity, forward_kinematics,
    pose_translation, require_reachable,
)
from .urdf import import_srdf, import_urdf

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
